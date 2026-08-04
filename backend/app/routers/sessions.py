"""分享会话导入：把别人导出的 bundle 落到本机对应引擎会话目录，建一个可恢复的任务。"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, sessions
from ..engines import ENGINES

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class Bundle(BaseModel):
    format: str = "bosun-session/1"
    engine: str
    session_uid: str
    prompt: str = ""
    project_name: str = ""
    jsonl: str


class ImportBody(BaseModel):
    project_id: int
    bundle: Bundle


class AttachBody(BaseModel):
    project_id: int
    engine: str
    session_uid: str


def _existing_task(project_id: int, engine: str, session_uid: str):
    return db.query_one(
        "SELECT id, status FROM task WHERE project_id=? AND engine=? AND session_uid=? AND deleted=0 "
        "ORDER BY created_at DESC LIMIT 1",
        (project_id, engine, session_uid),
    )


def _create_resume_task(project_id: int, engine: str, session_uid: str, title: str, prompt: str, kind: str):
    existing = _existing_task(project_id, engine, session_uid)
    if existing is not None:
        return {"task_id": existing["id"], "created": False, "status": existing["status"]}
    tid = db.execute(
        "INSERT INTO task(project_id,engine,prompt,title,priority,auto_approve,kind,"
        "session_uid,resume,status,created_at) "
        "VALUES(?,?,?,?,?,0,?,?,1,'draft',?)",
        (
            project_id,
            engine,
            prompt[:500],
            title[:60],
            7,
            kind,
            session_uid,
            time.time(),
        ),
    )
    return {"task_id": tid, "created": True, "status": "draft"}


@router.post("/import")
def import_session(body: ImportBody):
    b = body.bundle
    if b.engine not in ENGINES:
        raise HTTPException(400, f"未知引擎: {b.engine}")
    project = db.query_one("SELECT * FROM project WHERE id=?", (body.project_id,))
    if project is None:
        raise HTTPException(404, "目标项目不存在")
    # 落盘到本机引擎会话目录
    sessions.write_session(b.engine, project["path"], b.session_uid, b.jsonl)
    # 建一个 draft 的可恢复任务
    title = f"分享: {b.prompt or b.project_name or b.session_uid[:8]}"[:60]
    result = _create_resume_task(
        body.project_id,
        b.engine,
        b.session_uid,
        title,
        f"(导入分享会话) {b.prompt}",
        "shared",
    )
    return {"task_id": result["task_id"]}


@router.get("/discover")
def discover_sessions(project_id: int, limit: int = 50):
    project = db.query_one("SELECT * FROM project WHERE id=?", (project_id,))
    if project is None:
        raise HTTPException(404, "目标项目不存在")
    items = sessions.discover_local_sessions(project["path"], limit)
    for item in items:
        existing = _existing_task(project_id, item["engine"], item["session_uid"])
        item["task_id"] = existing["id"] if existing else None
        item["task_status"] = existing["status"] if existing else None
    return {"sessions": items}


@router.post("/attach")
def attach_local_session(body: AttachBody):
    if body.engine not in ENGINES:
        raise HTTPException(400, f"未知引擎: {body.engine}")
    project = db.query_one("SELECT * FROM project WHERE id=?", (body.project_id,))
    if project is None:
        raise HTTPException(404, "目标项目不存在")
    meta = sessions.local_session_info(body.engine, project["path"], body.session_uid)
    if meta is None:
        raise HTTPException(404, "未找到属于该项目的本地会话")
    result = _create_resume_task(
        body.project_id,
        body.engine,
        body.session_uid,
        f"本地会话: {meta['title']}",
        f"(本地会话) {meta.get('prompt') or meta['session_uid']}",
        "shared",
    )
    return result
