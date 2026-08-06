"""Bosun 后端入口。"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, JSONResponse, Response

from . import auth as auth_service
from . import codex_skills_guard, db, events, policies, reflection_scheduler, scheduler, self_update
from .routers import (
    auth,
    autopilot,
    claude,
    findings,
    projects,
    quota,
    reflection,
    sessions,
    settings,
    sources,
    stats,
    tasks,
    updates,
    ws,
)


logger = logging.getLogger("bosun")


class SPAStaticFiles(StaticFiles):
    """Serve built assets and fall back to index.html for frontend routes."""

    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not self._should_fallback_to_index(path, scope):
                raise
            index = Path(str(self.directory)) / "index.html"
            if not index.is_file():
                raise
            response = FileResponse(index)
        self._set_html_cache_headers(path, response)
        return response

    @staticmethod
    def _should_fallback_to_index(path: str, scope) -> bool:
        if scope.get("method") not in {"GET", "HEAD"}:
            return False
        first_segment = path.split("/", 1)[0]
        if first_segment in {"api", "ws", "assets", "icons"}:
            return False
        filename = path.rsplit("/", 1)[-1]
        return "." not in filename

    @staticmethod
    def _set_html_cache_headers(path: str, response: Response) -> None:
        media_type = response.media_type or response.headers.get("content-type", "")
        if path in {"", ".", "index.html"} or "text/html" in media_type:
            response.headers["cache-control"] = "no-store"


app = FastAPI(title="Bosun")

# 无需登录即可访问的 API：健康检查与登录流程本身。其余 /api/* 一律校验 token。
_PUBLIC_API_PATHS = {"/api/health", "/api/auth/status", "/api/auth/login"}

# agent 回报端点不走会话 token(agent 登录不了)，改由端点自己校验任务级凭证。
_TASK_REPORT_PATH = re.compile(r"^/api/tasks/\d+/report$")


@app.middleware("http")
async def _require_auth(request, call_next):
    path = request.url.path
    if (
        path.startswith("/api/")
        and path not in _PUBLIC_API_PATHS
        and not _TASK_REPORT_PATH.match(path)
        and auth_service.is_enabled()
    ):
        token = auth_service.token_from_request(request.headers)
        if not auth_service.validate_token(token):
            return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
    return await call_next(request)


# 不挂 CORS：生产由后端同源挂载 dist，开发走 Vite proxy，同源覆盖全部合法访问。
# 放行头(allow_origins=*)只会把 API 暴露给浏览器里的跨源网页——未设口令时
# 等于任意网页可驱动全权限引擎。跨源 WS 由 routers/ws.py 按 Origin 拒绝。

app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(tasks.router)
app.include_router(sessions.router)
app.include_router(autopilot.router)
app.include_router(claude.router)
app.include_router(quota.router)
app.include_router(reflection.router)
app.include_router(findings.router)
app.include_router(sources.router)
app.include_router(settings.router)
app.include_router(stats.router)
app.include_router(updates.router)
app.include_router(ws.router)


def _warn_if_login_disabled() -> None:
    """未设访问口令时在启动日志里警示。

    默认监听 0.0.0.0，同一网络下任何设备都能打开工作台驱动 cc/codex、
    直连终端 PTY。这条警示是「裸奔」状态唯一的提示面。
    """
    if auth_service.is_enabled():
        return
    logger.warning(
        "未设置访问口令，任何能访问本服务的人都可操作终端与任务。"
        "请在「设置 → 访问控制」里设置口令，或配置环境变量 BOSUN_PASSWORD。"
    )


@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
    _warn_if_login_disabled()  # 必须在 init_db 之后：口令哈希存在 DB 里
    from . import autopilot
    autopilot.reconcile_on_startup()  # 重启后把残留 running 的自愈 run 落终态, 防项目自愈永久卡死
    from . import skills_install
    skills_install.install_installed_engines()
    codex_skills_guard.persist_superpowers_disables()
    loop = asyncio.get_running_loop()
    events.set_loop(loop)
    scheduler.start(loop)
    policies.start_ticker()
    reflection_scheduler.start_ticker()


@app.get("/api/health")
def health(request: Request):
    payload: dict = {"ok": True}
    # 任务计数只回给本机：本端点免鉴权，而后端默认监听 0.0.0.0，
    # 不应把任务态泄露给局域网内任意设备。
    client = request.client
    if client and client.host in {"127.0.0.1", "::1"}:
        rows = db.query(
            "SELECT status, COUNT(*) AS n FROM task "
            "WHERE deleted=0 AND status IN ('running','waiting_input','queued') "
            "GROUP BY status"
        )
        payload["tasks"] = {row["status"]: row["n"] for row in rows}
        payload.update(self_update.health_summary())
    return payload


# 生产：挂载前端构建产物（frontend/dist）。开发时前端跑 Vite dev server。
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", SPAStaticFiles(directory=str(_dist), html=True), name="static")
