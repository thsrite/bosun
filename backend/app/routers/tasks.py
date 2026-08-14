"""任务 CRUD + 优先级重排 + 取消 + 日志。"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import auth, browser_computer, db, events, log_archive, nesting, orchestrations, routing, scheduler, sessions, subtasks, uploads
from ..engines import CODING_ENGINES, ENGINES, normalize_engine_id
from ..pty_session import remove_terminal_log_files, script_log_path_for

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def derive_title(prompt: str) -> str:
    """从指令启发式生成简短标题：首个非空行，截断 40 字。"""
    for line in (prompt or "").splitlines():
        line = line.strip()
        if line:
            return line[:40] + ("…" if len(line) > 40 else "")
    return "(空任务)"


class CreateTask(BaseModel):
    project_id: int
    engine: str = "claude"
    prompt: str
    title: str | None = None
    priority: int = 5
    auto_approve: bool = False
    kind: str = "task"
    start: bool = False  # True=创建即排入执行；默认 False=只存为待办(draft)


class UpdateTask(BaseModel):
    title: str | None = None
    prompt: str | None = None
    engine: str | None = None


class Reorder(BaseModel):
    # [{id, priority}, ...]
    items: list[dict]


def _annotate_waiting(out: dict) -> dict:
    if out["status"] != "waiting_input":
        return out
    # 待输入再细分：
    # - pending_perm: SDK 会话结构化授权
    # - waiting_kind: CLI/SDK 的等待类型(permission | choice | input | review)
    pending_perm = scheduler.get_permission(out["id"]) is not None
    out["pending_perm"] = pending_perm
    out["waiting_kind"] = "permission" if pending_perm else scheduler.get_waiting_kind(out["id"])
    return out


@router.get("")
def list_tasks(project_id: int | None = None):
    if project_id is not None:
        rows = db.query(
            "SELECT * FROM task WHERE project_id=? AND deleted=0 ORDER BY priority DESC, created_at ASC",
            (project_id,),
        )
    else:
        rows = db.query("SELECT * FROM task WHERE deleted=0 ORDER BY priority DESC, created_at ASC")
    out = [dict(r) for r in rows]
    for r in out:
        _annotate_waiting(r)
    return out


@router.post("")
def create_task(body: CreateTask):
    reason = None
    engine = normalize_engine_id(body.engine)
    if engine == "auto":
        engine, reason = routing.pick_engine()
    if engine not in ENGINES:
        raise HTTPException(400, f"未知引擎: {engine}")
    if engine == "browser":
        try:
            browser_computer.extract_start_url(body.prompt)
        except browser_computer.BrowserPolicyError as exc:
            raise HTTPException(400, str(exc)) from exc
        if body.start and not browser_computer.availability()["available"]:
            raise HTTPException(409, "；".join(browser_computer.availability()["missing"]))
    if db.query_one("SELECT id FROM project WHERE id=?", (body.project_id,)) is None:
        raise HTTPException(404, "项目不存在")
    status = "queued" if body.start else "draft"
    title = (body.title or "").strip() or derive_title(body.prompt)
    tid = db.execute(
        "INSERT INTO task(project_id,engine,prompt,title,priority,auto_approve,kind,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            body.project_id,
            engine,
            body.prompt,
            title,
            body.priority,
            int(body.auto_approve),
            body.kind,
            status,
            time.time(),
        ),
    )
    if body.start:
        scheduler.tick()  # 显式要求执行时才排入调度
    return {"id": tid, "engine": engine, "auto_reason": reason}


@router.put("/{task_id}")
def update_task(task_id: int, body: UpdateTask):
    """编辑任务标题 / 描述(自定义)。"""
    t = db.query_one("SELECT * FROM task WHERE id=?", (task_id,))
    if t is None:
        raise HTTPException(404, "任务不存在")
    fields = {}
    if body.title is not None:
        fields["title"] = body.title.strip() or derive_title(t["original_prompt"] or t["prompt"])
    if body.prompt is not None:
        fields["prompt"] = body.prompt
    if body.engine is not None:
        engine = normalize_engine_id(body.engine)
        if engine not in ENGINES:
            raise HTTPException(400, f"未知引擎: {body.engine}")
        if t["status"] != "draft":
            raise HTTPException(409, "已启动任务不能直接改引擎，请使用接力")
        fields["engine"] = engine
    next_engine = fields.get("engine", t["engine"])
    next_prompt = fields.get("prompt", t["prompt"])
    if next_engine == "browser":
        try:
            browser_computer.extract_start_url(next_prompt)
        except browser_computer.BrowserPolicyError as exc:
            raise HTTPException(400, str(exc)) from exc
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE task SET {sets} WHERE id=?", (*fields.values(), task_id))
    return get_task(task_id)


@router.post("/reorder")
def reorder(body: Reorder):
    for it in body.items:
        db.execute("UPDATE task SET priority=? WHERE id=?", (int(it["priority"]), int(it["id"])))
    scheduler.tick()
    return {"ok": True}


@router.post("/{task_id}/start")
def start_task(task_id: int):
    """把单个 draft 任务排入执行（draft → queued），交给调度器。"""
    task = db.query_one("SELECT engine,prompt FROM task WHERE id=? AND deleted=0", (task_id,))
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task["engine"] == "browser":
        info = browser_computer.availability()
        if not info["available"]:
            raise HTTPException(409, "；".join(info["missing"]))
        try:
            browser_computer.extract_start_url(task["prompt"])
        except browser_computer.BrowserPolicyError as exc:
            raise HTTPException(400, str(exc)) from exc
    db.execute("UPDATE task SET status='queued' WHERE id=? AND status='draft'", (task_id,))
    scheduler.tick()
    return {"ok": True}


class StartAll(BaseModel):
    project_id: int | None = None


@router.post("/start-all")
def start_all(body: StartAll):
    """把待办任务批量排入执行；不传 project_id 则全部项目。"""
    browser_ready = bool(browser_computer.availability()["available"])
    engine_guard = "" if browser_ready else " AND engine!='browser'"
    if body.project_id is not None:
        db.execute(
            f"UPDATE task SET status='queued' WHERE status='draft' AND project_id=?{engine_guard}",
            (body.project_id,),
        )
    else:
        db.execute(f"UPDATE task SET status='queued' WHERE status='draft'{engine_guard}")
    scheduler.tick()
    return {"ok": True}


class ContinueBody(BaseModel):
    prompt: str = ""       # 追加指令，留空=只加载上下文继续
    compact: bool = False  # True=恢复后自动发 /compact 压缩上下文
    start: bool = True


class ResumePausedBody(BaseModel):
    prompt: str = ""       # 追加指令，留空=只加载上下文继续


class HandoffBody(BaseModel):
    engine: str
    start: bool = True


_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_HANDOFF_CONTEXT_CHARS = 16_000


def _last_user_instruction(task) -> str | None:
    """抠出用户在会话里最后一次输入的指令，供接力置顶为「现在要处理什么」。

    仅 Claude SDK 会话的日志是结构化 NDJSON（每次终端输入落成 t=="user"）；
    Codex(PTY) 裸终端日志没有这个 tag，抠不到就返回 None，让接力退回整段日志。
    """
    if not task["log_path"]:
        return None
    raw = log_archive.read_text(task["log_path"])
    if raw is None:
        return None
    last: str | None = None
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("t") == "user" and item.get("text"):
            text = _ANSI_RE.sub("", str(item["text"])).replace("\r", "").strip()
            if text:
                last = text
    return last


def _handoff_log_context(task) -> str:
    """把最近执行日志压成适合另一个引擎接手的纯文本上下文。"""
    if not task["log_path"]:
        return "（没有可用的执行日志，请先检查当前工作区状态。）"
    log_path = Path(task["log_path"])
    script_path = Path(script_log_path_for(task["log_path"]))
    source = script_path if log_archive.has_content(script_path) else log_path
    raw = log_archive.read_text(source)
    if raw is None:
        return "（没有可用的执行日志，请先检查当前工作区状态。）"

    # Claude SDK 日志是 NDJSON，只保留对接力有意义的文字、工具与错误事件。
    rendered: list[str] = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            rendered.append(line)
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("t")
        if kind in {"text", "user", "raw"} and item.get("text"):
            rendered.append(str(item["text"]))
        elif kind == "tool":
            rendered.append(f"[工具] {item.get('name', '')}: {item.get('input', '')}")
        elif kind == "error" and item.get("msg"):
            rendered.append(f"[错误] {item['msg']}")
    clean = _ANSI_RE.sub("", "\n".join(rendered)).replace("\r", "").strip()
    if not clean:
        return "（执行日志为空，请先检查当前工作区状态。）"
    if len(clean) > _HANDOFF_CONTEXT_CHARS:
        clean = "（仅保留最近日志）\n" + clean[-_HANDOFF_CONTEXT_CHARS:]
    return clean


@router.post("/{task_id}/handoff")
def handoff_task(task_id: int, body: HandoffBody):
    """切换到另一引擎接力：保留原任务，新会话携带目标与最近执行上下文。"""
    t = db.query_one("SELECT * FROM task WHERE id=? AND deleted=0", (task_id,))
    if t is None:
        raise HTTPException(404, "任务不存在")
    engine = normalize_engine_id(body.engine)
    if engine not in CODING_ENGINES:
        raise HTTPException(400, f"未知引擎: {body.engine}")
    if engine == t["engine"]:
        raise HTTPException(400, "接力引擎必须与当前引擎不同")

    context = _handoff_log_context(t)
    recent = _last_user_instruction(t)
    origin = t["original_prompt"] or t["prompt"]
    header = (
        f"你正在从 {t['engine']} 接力 Bosun 任务 #{task_id}。\n\n"
        "原始任务目标：\n"
        f"{origin}\n"
    )
    if recent:
        focus = (
            "\n【重点·现在要处理什么】用户最近的指令：\n"
            "---\n"
            f"{recent}\n"
            "---\n"
        )
        instruction = (
            "\n请优先按【用户最近的指令】继续；先检查当前工作区和 git diff 确认已完成与未完成内容，"
            "历史执行日志只在需要背景时再查阅。"
        )
    else:
        focus = ""
        instruction = (
            "\n请先检查当前工作区和 git diff，确认已完成与未完成内容，然后继续完成原始任务。"
        )
    prompt = (
        header
        + focus
        + "\n历史执行日志（可选参考）：\n"
        "---\n"
        f"{context}\n"
        "---\n"
        + instruction
    )
    if t["status"] in ("queued", "running", "waiting_input"):
        scheduler.cancel(task_id)
    status = "queued" if body.start else "draft"
    label = {"claude": "Claude", "codex": "Codex", "omp": "OMP", "kimi": "Kimi"}.get(engine, engine.upper())
    base_title = (t["title"] or derive_title(origin)).strip()
    title = f"{base_title} · {label} 接力"[:80]
    new_id = db.execute(
        "INSERT INTO task(project_id,engine,prompt,title,priority,auto_approve,kind,status,created_at) "
        "VALUES(?,?,?,?,?,?,'handoff',?,?)",
        (
            t["project_id"], engine, prompt, title, t["priority"],
            t["auto_approve"], status, time.time(),
        ),
    )
    if body.start:
        scheduler.tick()
    return {"id": new_id, "engine": engine, "from_task_id": task_id}


@router.post("/{task_id}/continue")
def continue_task(task_id: int, body: ContinueBody):
    """在原任务上恢复会话继续(继续 / 压缩)：同一张卡片拉回执行中，记录累加，不新建。"""
    t = db.query_one("SELECT * FROM task WHERE id=?", (task_id,))
    if t is None:
        raise HTTPException(404, "任务不存在")
    if not t["session_uid"]:
        raise HTTPException(400, "该任务没有可恢复的会话 id（可能从未运行或运行时未捕获）")
    if t["status"] in ("running", "queued", "waiting_input"):
        raise HTTPException(409, "任务仍在运行/待输入，请先到终端处理或取消后再继续，避免重复运行同一会话")
    proj = db.query_one("SELECT path FROM project WHERE id=?", (t["project_id"],))
    if proj is None:
        raise HTTPException(400, "任务所属项目不存在，无法继续")
    if sessions.local_session_info(t["engine"], proj["path"], t["session_uid"]) is None:
        raise HTTPException(
            400,
            "该会话未落盘、已丢失或不属于当前项目，已阻止继续以避免加载其他任务的会话；请改用「重跑」",
        )
    kind = "compact" if body.compact else "continue"
    # prompt 是真要发给引擎的追加指令：留空就只 --resume 加载上下文，不发任何内容。
    # 原始提示词冻结进 original_prompt，列表/标题都读它，不会被「继续会话」冲掉。
    prompt = (body.prompt or "").strip()
    status = "queued" if body.start else "draft"
    # 累加上一次运行的活跃时长(秒)，避免 reactivate 重置 started_at 后丢失历史时长
    prior = 0
    if t["started_at"] and t["ended_at"]:
        prior = max(0, int(t["ended_at"] - t["started_at"]))
    # 清掉旧日志：resume 以 pty 终端流重开，旧的(可能是 SDK 的 NDJSON)格式不同，避免混叠
    if t["log_path"]:
        remove_terminal_log_files(t["log_path"])
    db.execute(
        "UPDATE task SET status=?, resume=1, post_input=?, kind=?, "
        "original_prompt=COALESCE(NULLIF(original_prompt,''), NULLIF(prompt,'')), prompt=?, "
        "exit_code=NULL, ended_at=NULL, started_at=NULL, "
        "elapsed_accum=elapsed_accum+? WHERE id=?",
        (status, "/compact\r" if body.compact else None, kind, prompt, prior, task_id),
    )
    events.emit("task.status", {"task_id": task_id, "status": status})
    if body.start:
        scheduler.tick()
    return {"id": task_id}


@router.post("/{task_id}/rerun")
def rerun_task(task_id: int):
    """用同样的指令全新跑一遍(新会话，不加载旧上下文)。任何已结束任务都可用。"""
    t = db.query_one("SELECT * FROM task WHERE id=?", (task_id,))
    if t is None:
        raise HTTPException(404, "任务不存在")
    # 继续过会话的任务，prompt 里只剩追加指令(甚至为空)，重跑必须用原始提示词
    origin = t["original_prompt"] or t["prompt"]
    title = t["title"] or derive_title(origin)
    tid = db.execute(
        "INSERT INTO task(project_id,engine,prompt,title,priority,auto_approve,kind,status,created_at) "
        "VALUES(?,?,?,?,?,?,'task','queued',?)",
        (t["project_id"], t["engine"], origin, title, t["priority"], t["auto_approve"], time.time()),
    )
    scheduler.tick()
    return {"id": tid}


@router.get("/{task_id}/export")
def export_session(task_id: int):
    """导出完整会话上下文为可分享 bundle。"""
    t = db.query_one("SELECT * FROM task WHERE id=?", (task_id,))
    if t is None:
        raise HTTPException(404, "任务不存在")
    if not t["session_uid"]:
        raise HTTPException(400, "该任务没有会话可分享")
    project = db.query_one("SELECT * FROM project WHERE id=?", (t["project_id"],))
    content = sessions.read_session(t["engine"], project["path"], t["session_uid"])
    if content is None:
        raise HTTPException(404, "会话文件不存在（可能已被清理）")
    return {
        "format": "bosun-session/1",
        "engine": t["engine"],
        "session_uid": t["session_uid"],
        "prompt": t["original_prompt"] or t["prompt"],
        "project_name": project["name"],
        "jsonl": content,
    }


@router.post("/{task_id}/complete")
def complete_task(task_id: int):
    return _run_transition(task_id, scheduler.complete)


class ReportBody(BaseModel):
    result: Literal["done", "failed", "needs_input"]
    summary: str = ""
    needs_reply: bool = False
    # 回报方 shell 自己的 pid，用于识别嵌套 agent 的冒名回报
    reporter_pid: int | None = None
    artifact: str | None = None


_REPORT_STATUS = {"done": "waiting_input", "failed": "failed", "needs_input": "waiting_input"}


def _is_loopback(request: Request) -> bool:
    return (request.client.host if request.client else "") in {"127.0.0.1", "::1"}


def _report_authorized(task_id: int, request: Request | None) -> bool:
    """回报凭证校验：会话 token(前端手动补报) 或本任务的回调 token(agent)。

    没开访问口令时一律放行；request 为 None 只可能是进程内直接调用。

    例外：升级前就已经跑起来的任务，环境里没有 BOSUN_TASK_TOKEN(注入发生在派发
    时刻，改不了活着的进程)，它们的 report_token 为空。这类**仍在运行**的任务
    只认本机回环的回报，让在飞的会话还能正常收尾；下一轮派发就有 token 了。
    """
    if not auth.is_enabled():
        return True
    if request is None:
        return True
    token = auth.token_from_request(request.headers)
    if auth.validate_token(token) or auth.validate_task_token(task_id, token):
        return True
    row = db.query_one("SELECT status, report_token FROM task WHERE id=?", (task_id,))
    return (
        bool(row)
        and not row["report_token"]
        and row["status"] == "running"
        and _is_loopback(request)
    )


@router.post("/{task_id}/artifact")
async def save_task_artifact(task_id: int, request: Request):
    """编排步骤在收尾回报前以 UTF-8 纯文本提交完整阶段产物。"""
    if not _report_authorized(task_id, request):
        raise HTTPException(status_code=401, detail="回报凭证无效")
    artifact = await _read_artifact_body(request)
    try:
        return orchestrations.save_task_artifact(task_id, artifact)
    except orchestrations.OrchestrationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


async def _read_artifact_body(request: Request) -> str:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > orchestrations.MAX_ARTIFACT_BYTES:
                raise HTTPException(status_code=413, detail="阶段产物过大（上限 200 KiB）")
        except ValueError:
            raise HTTPException(status_code=400, detail="Content-Length 无效")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > orchestrations.MAX_ARTIFACT_BYTES:
            raise HTTPException(status_code=413, detail="阶段产物过大（上限 200 KiB）")
        body.extend(chunk)
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="阶段产物必须是 UTF-8 文本") from exc


@router.post("/{task_id}/report")
def report_task(task_id: int, body: ReportBody, request: Request = None):
    """任务收尾时的权威状态回调（agent 按收尾约定直接 HTTP POST）。"""
    if not _report_authorized(task_id, request):
        raise HTTPException(status_code=401, detail="回报凭证无效")
    t = db.query_one(
        "SELECT id, status, waiting_since FROM task WHERE id=? AND deleted=0",
        (task_id,),
    )
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    # 子 agent 会连 BOSUN_TASK_ID 一起继承，拿父任务的 id 回报。判定放在这里而不是
    # skill 脚本里：后端不受 agent 沙箱限制，读得到完整进程表。
    if nesting.is_nested_report(body.reporter_pid):
        raise HTTPException(status_code=409, detail="嵌套 agent 不能代替父任务回报状态")
    try:
        is_orchestration_step = orchestrations.validate_task_report(task_id, body.result, body.artifact)
    except orchestrations.OrchestrationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    session = scheduler.get_session(task_id)
    status = _REPORT_STATUS[body.result]
    summary = (body.summary or "")[:2000]
    if status == "waiting_input":
        db.execute(
            "UPDATE task SET status=?, report_result=?, report_summary=?, "
            "waiting_since=CASE "
            "WHEN status='waiting_input' AND waiting_since IS NOT NULL THEN waiting_since "
            "ELSE ? END WHERE id=?",
            (status, body.result, summary, time.time(), task_id),
        )
    else:
        db.execute(
            "UPDATE task SET status=?, report_result=?, report_summary=?, waiting_since=NULL "
            "WHERE id=?",
            (status, body.result, summary, task_id),
        )
    if session is not None and hasattr(session, "mark_reported"):
        session.mark_reported(status)
    current = db.query_one("SELECT waiting_since FROM task WHERE id=?", (task_id,))
    events.emit("task.status", {
        "task_id": task_id,
        "status": status,
        "report_result": body.result,
        "report_summary": summary,
        "needs_reply": body.needs_reply,
        "waiting_kind": "review" if body.result == "done" else None,
        "waiting_since": current["waiting_since"] if current else None,
        "notify_user": status == "waiting_input" and (
            t["status"] != "waiting_input" or t["waiting_since"] is None
        ),
    })
    if is_orchestration_step:
        orchestrations.handle_task_report(task_id, body.result, summary, body.artifact)
        if body.result in {"done", "failed"}:
            scheduler.finish_orchestration_step(task_id)
    return {
        "ok": True,
        "status": status,
        "report_result": body.result,
        # 出现在 agent 的工具结果里：在模型正要停下的时刻提醒它补上正文。必须是
        # 无条件命令式——#524 实测条件式（"若还没打印"）会被模型误判「已打印」绕过。
        "hint": "回报已送达。请紧接着把本轮完整结论正文作为你最后一条消息打印出来再停下"
                "（不得再调工具）；summary 只是回执，用户只看正文。",
    }


def _run_transition(task_id: int, transition) -> dict:
    if db.query_one("SELECT id FROM task WHERE id=? AND deleted=0", (task_id,)) is None:
        raise HTTPException(404, "任务不存在")
    try:
        transition(task_id)
    except scheduler.TaskTransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


class SpawnBody(BaseModel):
    engine: str
    prompt: str
    title: str | None = None
    timeout: float | None = None


class SubtaskReplyBody(BaseModel):
    message: str
    timeout: float | None = None


def _spawn_authorized(parent_id: int, request: Request | None) -> bool:
    """派生子任务只认父任务**自己**的回调凭证。

    与 /report 不同，这里不接受会话 token（此接口是给 agent 用的，不是给前端用的），
    也不留「历史任务没有 token」的回环豁免——spawn 是新接口，没有在飞的老任务。
    """
    if not auth.is_enabled():
        return True
    if request is None:
        return True
    return auth.validate_task_token(parent_id, auth.token_from_request(request.headers))


@router.post("/{task_id}/spawn")
def spawn_subtask(task_id: int, body: SpawnBody, request: Request = None):
    """agent 申请派生一个跑在别的引擎上的子任务，**同步返回其结论**。

    同步是刻意的：对 agent 而言等价于直接跑 `codex exec`，迁移成本接近零。
    改成异步 + 轮询反而比直接跑 CLI 更麻烦，agent 会绕开不用。
    """
    if not subtasks.enabled():
        raise HTTPException(403, "子任务功能已关闭")
    if not _spawn_authorized(task_id, request):
        raise HTTPException(401, "派生凭证无效")
    parent = db.query_one("SELECT * FROM task WHERE id=? AND deleted=0", (task_id,))
    if parent is None:
        raise HTTPException(404, "任务不存在")
    if parent["parent_task_id"] is not None:
        raise HTTPException(403, "子任务不能再派生子任务（最多一层）")
    if parent["status"] not in ("running", "waiting_input"):
        raise HTTPException(409, "父任务未在执行，不能派生子任务")
    if subtasks.child_count(task_id) >= subtasks.max_children():
        raise HTTPException(429, f"子任务数已达上限（{subtasks.max_children()}）")

    engine = normalize_engine_id(body.engine)
    if engine == "auto":
        engine, _ = routing.pick_engine()
    if engine not in CODING_ENGINES:
        raise HTTPException(400, f"未知引擎: {engine}")

    # 名额在建行之前占：拿不到就直接回 429，不留一条起不来的子任务记录
    if not subtasks.acquire_slot():
        raise HTTPException(429, f"同时进行的子任务已达上限（{subtasks.concurrency()}），稍后再试")
    try:
        return _spawn_and_wait(parent, engine, body)
    finally:
        subtasks.release_slot()


def _spawn_and_wait(parent, engine: str, body: SpawnBody) -> dict:
    """建行、派发、阻塞等结论。调用方持有 spawn 名额。"""
    task_id = parent["id"]
    title = (body.title or "").strip() or derive_title(body.prompt)
    child_id = db.execute(
        "INSERT INTO task(project_id,engine,prompt,title,priority,auto_approve,kind,status,"
        "parent_task_id,created_at) VALUES(?,?,?,?,?,?,?,'queued',?,?)",
        (
            parent["project_id"],
            engine,
            body.prompt,
            title,
            parent["priority"],
            parent["auto_approve"],  # 审批策略随父任务，子任务不额外放宽
            "task",
            task_id,
            time.time(),
        ),
    )
    events.emit("task.spawned", {"task_id": child_id, "parent_task_id": task_id})
    # 绕过并发槽直接派发：父任务正占着槽等它，排队会互锁
    scheduler.start_subtask(child_id)
    timeout = subtasks.clamp_timeout(
        body.timeout if body.timeout is not None else subtasks.default_timeout()
    )
    result = subtasks.wait_for_result(child_id, timeout)
    return {"id": child_id, "engine": engine, **result}


@router.post("/{task_id}/reply")
def reply_to_subtask(task_id: int, body: SubtaskReplyBody, request: Request = None):
    """父任务回复子任务的提问，并同步等待子任务下一轮回报。"""
    child = db.query_one("SELECT * FROM task WHERE id=? AND deleted=0", (task_id,))
    if child is None:
        raise HTTPException(404, "子任务不存在")
    parent_id = child["parent_task_id"]
    if parent_id is None:
        raise HTTPException(409, "目标任务不是子任务")
    if not _spawn_authorized(parent_id, request):
        raise HTTPException(401, "父任务凭证无效")
    parent = db.query_one("SELECT status FROM task WHERE id=? AND deleted=0", (parent_id,))
    if parent is None or parent["status"] not in ("running", "waiting_input"):
        raise HTTPException(409, "父任务未在执行，不能继续子任务通信")
    if child["status"] != "waiting_input" or child["report_result"] != "needs_input":
        raise HTTPException(409, "子任务当前未等待父任务回复")
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "回复内容不能为空")
    if scheduler.get_session(task_id) is None:
        raise HTTPException(409, "子任务会话已结束，无法回复")
    if not subtasks.acquire_slot():
        raise HTTPException(429, f"同时进行的子任务通信已达上限（{subtasks.concurrency()}），稍后再试")
    try:
        # 必须先清上一轮回报再投递；反过来会有竞态，可能擦掉子任务极速返回的新回报。
        db.execute(
            "UPDATE task SET status='running', report_result=NULL, report_summary=NULL, "
            "waiting_since=NULL WHERE id=?",
            (task_id,),
        )
        if not scheduler.send_subtask_reply(task_id, message):
            db.execute(
                "UPDATE task SET status='waiting_input', report_result='needs_input', "
                "report_summary=?, waiting_since=? WHERE id=?",
                (child["report_summary"], child["waiting_since"], task_id),
            )
            raise HTTPException(409, "子任务会话已结束，无法回复")
        events.emit("task.status", {"task_id": task_id, "status": "running"})
        timeout = subtasks.clamp_timeout(
            body.timeout if body.timeout is not None else subtasks.default_timeout()
        )
        result = subtasks.wait_for_result(task_id, timeout)
        return {"id": task_id, "engine": child["engine"], **result}
    finally:
        subtasks.release_slot()


@router.get("/{task_id}/result")
def get_task_result(task_id: int, request: Request = None):
    """补查子任务结论（/spawn 因超时或连接中断没拿到时的兜底）。

    凭证认本任务或其父任务的——父任务要能读自己派生出来的子任务。
    """
    row = db.query_one("SELECT * FROM task WHERE id=? AND deleted=0", (task_id,))
    if row is None:
        raise HTTPException(404, "任务不存在")
    parent_id = row["parent_task_id"]
    if not (
        _spawn_authorized(task_id, request)
        or (parent_id is not None and _spawn_authorized(parent_id, request))
    ):
        raise HTTPException(401, "凭证无效")
    return {
        "id": task_id,
        "status": row["status"],
        "result": row["report_result"],
        "summary": row["report_summary"] or "",
        "needs_reply": row["report_result"] == "needs_input",
        "finished": subtasks.is_final(row["status"], row["report_result"]),
    }


@router.post("/{task_id}/pause")
def pause_task(task_id: int):
    return _run_transition(task_id, scheduler.pause)


@router.post("/{task_id}/resume-paused")
def resume_paused_task(task_id: int, body: ResumePausedBody | None = None):
    prompt = (body.prompt if body else None) or ""
    return _run_transition(task_id, lambda tid: scheduler.resume_paused(tid, prompt))


@router.post("/{task_id}/restore-paused")
def restore_paused_task(task_id: int):
    return _run_transition(task_id, scheduler.restore_paused)


@router.post("/{task_id}/to-draft")
def task_to_draft(task_id: int):
    return _run_transition(task_id, scheduler.to_draft)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: int):
    scheduler.cancel(task_id)
    return {"ok": True}


class PermissionBody(BaseModel):
    allow: bool


@router.get("/{task_id}/permission")
def get_permission(task_id: int):
    return {"permission": scheduler.get_permission(task_id)}


@router.post("/{task_id}/permission")
def respond_permission(task_id: int, body: PermissionBody):
    ok = scheduler.respond_permission(task_id, body.allow)
    return {"ok": ok}


@router.get("/{task_id}/browser-assets/{asset_id}")
def get_browser_asset(task_id: int, asset_id: str):
    task = db.query_one("SELECT engine FROM task WHERE id=? AND deleted=0", (task_id,))
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task["engine"] != "browser":
        raise HTTPException(404, "该任务没有 Browser 截图")
    try:
        path = browser_computer.asset_path(task_id, asset_id)
    except browser_computer.BrowserPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, "截图不存在")
    return FileResponse(path, media_type="image/png", filename=asset_id)


@router.delete("/{task_id}")
def delete_task(task_id: int):
    scheduler.delete(task_id)
    return {"ok": True}


@router.get("/{task_id}")
def get_task(task_id: int):
    row = db.query_one("SELECT * FROM task WHERE id=?", (task_id,))
    if row is None:
        raise HTTPException(404, "任务不存在")
    out = dict(row)
    _annotate_waiting(out)
    # 会话被 /clear 冲掉时才让详情页露出「恢复会话」，正常运行不显示(误点=停执行器)
    if out["status"] in ("running", "waiting_input"):
        out["session_cleared"] = scheduler.get_session_cleared(task_id)
    return out


MAX_AUDIO_BYTES = 25 * 1024 * 1024  # OpenAI audio transcription request guardrail


@router.post("/{task_id}/upload-file")
async def upload_file(task_id: int, file: UploadFile = File(...)):
    """移动端传文件：存入项目工作目录的 .bosun-uploads/，返回绝对路径供注入终端输入。任意类型。"""
    t = db.query_one("SELECT project_id FROM task WHERE id=?", (task_id,))
    if t is None:
        raise HTTPException(404, "任务不存在")
    project = db.query_one("SELECT path FROM project WHERE id=?", (t["project_id"],))
    if project is None:
        raise HTTPException(404, "项目不存在")

    data = await file.read(uploads.MAX_UPLOAD_BYTES + 1)
    try:
        path = uploads.save_project_upload(project["path"], file.filename or "file", data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(413, str(exc)) from exc
    return {"path": path}


@router.post("/{task_id}/transcribe-audio")
async def transcribe_audio(task_id: int, file: UploadFile = File(...)):
    """应用内录音转文字：避免 iOS 第三方输入法跳转/回调到错误 App。"""
    if db.query_one("SELECT id FROM task WHERE id=?", (task_id,)) is None:
        raise HTTPException(404, "任务不存在")

    api_key = os.environ.get("BOSUN_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(400, "未配置 OPENAI_API_KEY 或 BOSUN_OPENAI_API_KEY，无法语音转文字")

    data = await file.read(MAX_AUDIO_BYTES + 1)
    if not data:
        raise HTTPException(400, "空音频")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "音频过大（上限 25MB）")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise HTTPException(500, "后端缺少 openai 依赖，请重新安装 requirements.txt") from exc

    filename = os.path.basename(file.filename or "voice.webm") or "voice.webm"
    model = os.environ.get("BOSUN_TRANSCRIBE_MODEL", "whisper-1")
    client = OpenAI(api_key=api_key)
    try:
        result = client.audio.transcriptions.create(
            model=model,
            file=(filename, data, file.content_type or "application/octet-stream"),
            language="zh",
            response_format="json",
            timeout=60,
        )
    except Exception as exc:
        raise HTTPException(502, f"语音转文字失败：{exc}") from exc

    text = result if isinstance(result, str) else getattr(result, "text", "")
    return {"text": text or ""}


@router.get("/{task_id}/log")
def get_log(task_id: int, source: str = "auto"):
    row = db.query_one("SELECT log_path FROM task WHERE id=?", (task_id,))
    if row is None or not row["log_path"]:
        return {"log": ""}
    source = source.lower()
    if source not in {"auto", "terminal", "script"}:
        raise HTTPException(400, "source 必须是 auto、terminal 或 script")
    log_path = Path(row["log_path"])
    script_path = Path(script_log_path_for(row["log_path"]))
    chosen = log_path
    chosen_source = "terminal"
    if source == "script":
        if log_archive.has_log(script_path):
            chosen = script_path
            chosen_source = "script"
    elif source == "auto" and log_archive.has_content(script_path):
        chosen = script_path
        chosen_source = "script"
    text = log_archive.read_text(chosen)
    if text is None:
        return {"log": ""}
    return {"log": text, "source": chosen_source}


@router.get("/{task_id}/history")
def get_history(task_id: int):
    task = db.query_one(
        "SELECT engine, project_id, session_uid FROM task WHERE id=? AND deleted=0",
        (task_id,),
    )
    if task is None:
        raise HTTPException(404, "任务不存在")
    if not task["session_uid"]:
        return {"messages": [], "truncated": False}
    project = db.query_one("SELECT path FROM project WHERE id=?", (task["project_id"],))
    if project is None:
        return {"messages": [], "truncated": False}
    return sessions.session_history(task["engine"], project["path"], task["session_uid"])
