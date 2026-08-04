"""项目问题来源：外部拉取 → finding。CRUD + 立即拉取 + 连接测试。

凭据明文存库(用户知情), 但读取脱敏: list/get 不回传明文; update 凭据留空=保留原值。
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, issue_sources

router = APIRouter(prefix="/api", tags=["sources"])


class SourceBody(BaseModel):
    name: str
    type: str = "http"
    enabled: bool = True
    config: dict = {}


class TestBody(BaseModel):
    type: str = "http"
    config: dict = {}


def _mask(row) -> dict:
    cfg = json.loads(row["config"] or "{}")
    has_cred = bool(cfg.get("auth_credential"))
    cfg["auth_credential"] = ""  # 不回传明文
    cfg["has_credential"] = has_cred
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "name": row["name"],
        "type": row["type"],
        "enabled": bool(row["enabled"]),
        "config": cfg,
        "last_pull_at": row["last_pull_at"],
        "created_at": row["created_at"],
    }


@router.get("/projects/{pid}/sources")
def list_sources(pid: int):
    rows = db.query("SELECT * FROM issue_source WHERE project_id=? ORDER BY created_at ASC", (pid,))
    return [_mask(r) for r in rows]


@router.post("/projects/{pid}/sources")
def create_source(pid: int, body: SourceBody):
    if not body.name.strip():
        raise HTTPException(400, "名称必填")
    cfg = dict(body.config or {})
    cfg.pop("has_credential", None)
    sid = db.execute(
        "INSERT INTO issue_source(project_id,name,type,enabled,config,created_at) VALUES(?,?,?,?,?,?)",
        (pid, body.name.strip(), body.type, int(body.enabled), json.dumps(cfg), time.time()),
    )
    return {"id": sid}


@router.put("/sources/{sid}")
def update_source(sid: int, body: SourceBody):
    row = db.query_one("SELECT * FROM issue_source WHERE id=?", (sid,))
    if row is None:
        raise HTTPException(404, "来源不存在")
    if not body.name.strip():
        raise HTTPException(400, "名称必填")
    cfg = dict(body.config or {})
    cfg.pop("has_credential", None)
    # 凭据留空 = 保留原值(前端脱敏后不回传明文)
    if not cfg.get("auth_credential"):
        old = json.loads(row["config"] or "{}")
        if old.get("auth_credential"):
            cfg["auth_credential"] = old["auth_credential"]
    db.execute(
        "UPDATE issue_source SET name=?, type=?, enabled=?, config=? WHERE id=?",
        (body.name.strip(), body.type, int(body.enabled), json.dumps(cfg), sid),
    )
    return {"ok": True}


@router.delete("/sources/{sid}")
def delete_source(sid: int):
    db.execute("DELETE FROM issue_source WHERE id=?", (sid,))
    return {"ok": True}


@router.post("/sources/{sid}/pull")
def pull_source(sid: int):
    try:
        n = issue_sources.pull(sid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"new_findings": n}


@router.post("/projects/{pid}/sources/test")
def test_source(pid: int, body: TestBody):
    cfg = dict(body.config or {})
    cfg.pop("has_credential", None)
    try:
        return issue_sources.test(body.type, cfg)
    except ValueError as e:
        raise HTTPException(400, str(e))
