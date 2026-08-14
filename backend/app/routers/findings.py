"""问题收件箱：列表 / 忽略 / 一键转修复任务 / 触发整体分析。"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, scheduler
from ..engines import CODING_ENGINES, normalize_engine_id
from ..services import discovery

router = APIRouter(prefix="/api/findings", tags=["findings"])


class AnalyzeBody(BaseModel):
    project_id: int
    sources: list[str] | None = None


class ToTaskBody(BaseModel):
    engine: str = "claude"
    priority: int = 7
    auto_approve: bool = False


@router.get("")
def list_findings(project_id: int | None = None, status: str = "open"):
    sql = "SELECT * FROM finding WHERE 1=1"
    params: list = []
    if project_id is not None:
        sql += " AND project_id=?"
        params.append(project_id)
    if status == "active":  # 收件箱: 待处理(open) + 已降级仅报告(muted)
        sql += " AND status IN ('open','muted')"
    elif status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY (status='muted'), created_at DESC"
    return [dict(r) for r in db.query(sql, tuple(params))]


@router.post("/{finding_id}/unmute")
def unmute(finding_id: int):
    """重新启用: 清失败记忆, 把 muted 问题放回 open, 允许再次自动修。"""
    f = db.query_one("SELECT * FROM finding WHERE id=?", (finding_id,))
    if f is None:
        raise HTTPException(404, "finding 不存在")
    db.execute(
        "DELETE FROM fix_memory WHERE project_id=? AND source=? AND title=?",
        (f["project_id"], f["source"], f["title"]),
    )
    db.execute("UPDATE finding SET status='open' WHERE id=? AND status='muted'", (finding_id,))
    return {"ok": True}


@router.get("/meta")
def meta(project_id: int):
    at = db.get_setting(f"analyze_at:{project_id}")
    return {
        "last_analyze_at": float(at) if at else None,
        "audit_skipped": db.get_setting(f"audit_skipped:{project_id}") == "1",
    }


@router.post("/analyze")
async def analyze(body: AnalyzeBody):
    import anyio

    count = await anyio.to_thread.run_sync(discovery.analyze, body.project_id, body.sources)
    return {"new_findings": count}


@router.post("/{finding_id}/dismiss")
def dismiss(finding_id: int):
    db.execute("UPDATE finding SET status='dismissed' WHERE id=?", (finding_id,))
    return {"ok": True}


@router.post("/{finding_id}/to-task")
def to_task(finding_id: int, body: ToTaskBody):
    f = db.query_one("SELECT * FROM finding WHERE id=?", (finding_id,))
    if f is None:
        raise HTTPException(404, "finding 不存在")
    prompt = (
        f"修复以下问题（来源: {f['source']}, 严重度: {f['severity']}）：\n"
        f"{f['title']}"
    )
    detail = (f["detail"] or "").strip()
    # 外部单字段源 title 与 detail 是同一段文字，相同时不重复拼「详情」
    if detail and detail != (f["title"] or "").strip():
        prompt += f"\n\n详情:\n{detail}"
    engine = normalize_engine_id(body.engine)
    if engine not in CODING_ENGINES:
        raise HTTPException(400, f"未知引擎: {body.engine}")
    tid = db.execute(
        "INSERT INTO task(project_id,engine,prompt,priority,auto_approve,kind,created_at) "
        "VALUES(?,?,?,?,?, 'repair', ?)",
        (f["project_id"], engine, prompt, body.priority, int(body.auto_approve), time.time()),
    )
    db.execute("UPDATE finding SET status='task_created', task_id=? WHERE id=?", (tid, finding_id))
    scheduler.tick()
    return {"task_id": tid}
