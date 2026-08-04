"""项目注册：扫描根目录 / 手动添加 / 列表(带统计)。"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import autopilot, db, events, scheduler

router = APIRouter(prefix="/api/projects", tags=["projects"])


class AddProject(BaseModel):
    path: str
    name: str | None = None


class ScanRoot(BaseModel):
    root: str
    max_depth: int = 3


class ConfigBody(BaseModel):
    test_cmd: str | None = None
    build_cmd: str | None = None
    lint_cmd: str | None = None
    enabled_sources: str | None = None
    cron: str | None = None


def _project_dict(row) -> dict:
    counts = db.query(
        "SELECT status, COUNT(*) c FROM task WHERE project_id=? AND deleted=0 GROUP BY status",
        (row["id"],),
    )
    by_status = {r["status"]: r["c"] for r in counts}
    running = by_status.get("running", 0) + by_status.get("waiting_input", 0)
    total = sum(by_status.values())
    fstats = db.query(
        "SELECT origin, status, COUNT(*) c FROM finding WHERE project_id=? GROUP BY origin, status",
        (row["id"],),
    )
    manual_open = auto_open = fixed = open_findings = 0
    for r in fstats:
        if r["status"] == "open":
            open_findings += r["c"]
            if r["origin"] == "autopilot":
                auto_open += r["c"]
            else:
                manual_open += r["c"]
        if r["status"] == "fixed":
            fixed += r["c"]
    ap = db.query_one(
        "SELECT status, iteration, max_iterations, summary FROM autopilot_run "
        "WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
        (row["id"],),
    )
    analyze_at = db.get_setting(f"analyze_at:{row['id']}")
    policy_count = db.query_one(
        "SELECT COUNT(*) c FROM policy WHERE project_id=? AND enabled=1", (row["id"],)
    )["c"]
    return {
        "id": row["id"],
        "name": row["name"],
        "path": row["path"],
        "task_total": total,
        "running": running,
        "by_status": by_status,
        "open_findings": open_findings,
        "manual_open": manual_open,
        "auto_open": auto_open,
        "fixed_findings": fixed,
        "autopilot": dict(ap) if ap else None,
        "last_analyze_at": float(analyze_at) if analyze_at else None,
        "audit_skipped": db.get_setting(f"audit_skipped:{row['id']}") == "1",
        "policy_count": policy_count,
    }


@router.get("")
def list_projects():
    rows = db.query("SELECT * FROM project ORDER BY created_at ASC")
    return [_project_dict(r) for r in rows]


def _add(path: str, name: str | None) -> int:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise HTTPException(400, f"目录不存在: {p}")
    name = name or p.name
    existing = db.query_one("SELECT id FROM project WHERE path=?", (str(p),))
    if existing:
        return existing["id"]
    pid = db.execute(
        "INSERT INTO project(name, path, created_at) VALUES(?,?,?)",
        (name, str(p), time.time()),
    )
    db.execute("INSERT OR IGNORE INTO project_config(project_id) VALUES(?)", (pid,))
    return pid


@router.post("")
def add_project(body: AddProject):
    pid = _add(body.path, body.name)
    return {"id": pid}


@router.post("/scan")
def scan_root(body: ScanRoot):
    root = Path(body.root).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(400, f"目录不存在: {root}")
    added = []
    # 广度优先找 .git 目录，命中就不再深入
    def walk(d: Path, depth: int):
        if depth > body.max_depth:
            return
        if (d / ".git").exists():
            pid = _add(str(d), None)
            added.append({"id": pid, "path": str(d)})
            return
        try:
            for child in sorted(d.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    walk(child, depth + 1)
        except PermissionError:
            pass

    walk(root, 0)
    return {"added": added, "count": len(added)}


@router.get("/browse")
def browse(path: str | None = None):
    """列目录: 供前端目录浏览器选项目根/仓库(本地工具, 浏览本机文件系统)。"""
    try:
        base = (Path(path).expanduser() if path else Path.home()).resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, "路径无效")
    if not base.is_dir():
        raise HTTPException(400, f"不是目录: {base}")
    entries = []
    try:
        for child in sorted(base.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):  # 隐藏 dotdir, 减噪
                continue
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue  # 断链/无权限的条目跳过
            entries.append({
                "name": child.name,
                "path": str(child),
                "is_git": (child / ".git").exists(),
            })
    except PermissionError:
        raise HTTPException(403, f"无权限读取: {base}")
    parent = str(base.parent)
    return {
        "path": str(base),
        "parent": parent if parent != str(base) else None,  # 根目录无上级
        "is_git": (base / ".git").exists(),
        "entries": entries,
    }


@router.delete("/{project_id}")
def delete_project(project_id: int):
    project = db.query_one("SELECT * FROM project WHERE id=?", (project_id,))
    if project is None:
        raise HTTPException(404, "项目不存在")
    running_autopilot = db.query_one(
        "SELECT id FROM autopilot_run WHERE project_id=? AND status='running'",
        (project_id,),
    )
    if running_autopilot is not None and autopilot.is_live(running_autopilot["id"]):
        raise HTTPException(409, "项目正在自动修复，请先停止自动修复后再删除")
    deleted_tasks = scheduler.delete_project_tasks(project_id)
    db.execute("DELETE FROM fix_memory WHERE project_id=?", (project_id,))
    db.execute(
        "DELETE FROM setting WHERE key IN (?,?,?)",
        (f"analyze_at:{project_id}", f"audit_skipped:{project_id}", f"audit_sig:{project_id}"),
    )
    db.execute("DELETE FROM project WHERE id=?", (project_id,))
    events.emit("project.deleted", {"project_id": project_id})
    return {"ok": True, "deleted_tasks": deleted_tasks}


@router.get("/{project_id}/config")
def get_config(project_id: int):
    row = db.query_one("SELECT * FROM project_config WHERE project_id=?", (project_id,))
    if row is None:
        db.execute("INSERT INTO project_config(project_id) VALUES(?)", (project_id,))
        row = db.query_one("SELECT * FROM project_config WHERE project_id=?", (project_id,))
    return dict(row)


@router.put("/{project_id}/config")
def set_config(project_id: int, body: ConfigBody):
    db.execute("INSERT OR IGNORE INTO project_config(project_id) VALUES(?)", (project_id,))
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(
            f"UPDATE project_config SET {sets} WHERE project_id=?",
            (*fields.values(), project_id),
        )
    return get_config(project_id)
