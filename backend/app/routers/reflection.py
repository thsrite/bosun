"""反思循环：跑反思 / 提案列表 / 采纳(应用白名单动作) / 忽略。"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, events, reflection, reflection_scheduler
from ..services import stats as stats_service

router = APIRouter(prefix="/api/proposals", tags=["reflection"])


class ReflectionSettingsBody(BaseModel):
    auto_enabled: bool = True
    interval_minutes: int = 1440
    min_pending_gap: int = 5


class DismissBody(BaseModel):
    reason: str = ""


@router.post("/reflect")
def run_reflect():
    # 用第一个项目目录作为 cc 的 cwd(反思读的是全局数据, cwd 仅供 SDK 运行)
    proj = db.query_one("SELECT path FROM project LIMIT 1")
    if proj is None:
        raise HTTPException(400, "还没有项目")
    return reflection_scheduler.start_reflection(proj["path"], origin="manual")


@router.get("/settings")
def get_settings():
    return reflection_scheduler.get_settings()


@router.put("/settings")
def update_settings(body: ReflectionSettingsBody):
    return reflection_scheduler.update_settings(
        body.auto_enabled,
        body.interval_minutes,
        body.min_pending_gap,
    )


@router.get("")
def list_proposals(status: str = "pending"):
    if status:
        rows = db.query("SELECT * FROM proposal WHERE status=? ORDER BY created_at DESC", (status,))
    else:
        rows = db.query("SELECT * FROM proposal ORDER BY created_at DESC LIMIT 50")
    out = []
    for r in rows:
        d = dict(r)
        d["action"] = json.loads(r["action"]) if r["action"] else None
        d["task"] = None
        if r["task_id"]:
            task = db.query_one(
                "SELECT id, project_id, title, status, engine FROM task WHERE id=?",
                (r["task_id"],),
            )
            if task:
                d["task"] = dict(task)
        out.append(d)
    return out


@router.post("/{pid}/apply")
def apply_proposal(pid: int):
    p = db.query_one("SELECT * FROM proposal WHERE id=?", (pid,))
    if p is None:
        raise HTTPException(404, "提案不存在")
    if p["task_id"] and db.query_one("SELECT id FROM task WHERE id=?", (p["task_id"],)):
        db.execute(
            "UPDATE proposal SET status='applied', applied_at=COALESCE(applied_at, ?) WHERE id=?",
            (time.time(), pid),
        )
        return {
            "applied": True,
            "note": f"已关联 draft 任务 #{p['task_id']}",
            "task_id": p["task_id"],
        }

    applied, note, task_id = (False, "纯建议，无可应用动作", None)
    old_value = None
    action = json.loads(p["action"]) if p["action"] else None
    if action:
        old_value = reflection.read_current_value(action)
        applied, note, task_id = reflection.apply_action(action)
    if task_id is None and not applied:
        applied, note, task_id = reflection.create_followup_task(p["title"], p["rationale"])
    if applied or task_id is not None:
        # 闭环: 记录应用前旧值 + 健康快照, 供下一轮反思回看这次改动是否奏效
        metrics_before = json.dumps(stats_service.build_dashboard()["summary"], ensure_ascii=False)
        db.execute(
            "UPDATE proposal SET status='applied', applied_at=?, task_id=?, old_value=?, metrics_before=? "
            "WHERE id=?",
            (time.time(), task_id, old_value, metrics_before, pid),
        )
    # 彻底失败(无动作可应用且兜底建任务也失败)则保持 pending, 不伪装成"已采纳"
    events.emit("proposals.updated", {})
    return {"applied": applied, "note": note, "task_id": task_id}


@router.post("/{pid}/dismiss")
def dismiss_proposal(pid: int, body: DismissBody = DismissBody()):
    db.execute(
        "UPDATE proposal SET status='dismissed', dismissed_at=?, dismiss_reason=? WHERE id=?",
        (time.time(), body.reason, pid),
    )
    events.emit("proposals.updated", {})
    return {"ok": True}
