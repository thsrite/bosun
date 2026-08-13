"""自愈循环：启动 / 停止 / 查询 / 日志。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import autopilot, db, quota
from ..engines import CODING_ENGINES

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])


class StartBody(BaseModel):
    project_id: int
    max_iterations: int = 3
    fix_engine: str = "cc"
    review_engine: str = "codex"
    token_budget: int = 0  # 0=不限；>0 累计到额自动停
    scope: str = "full"    # full | recent | commit
    scope_arg: str | None = None
    force: bool = False    # 忽略限额预检强制启动


@router.post("/start")
def start(body: StartBody):
    if body.fix_engine not in CODING_ENGINES or body.review_engine not in CODING_ENGINES:
        raise HTTPException(400, "未知引擎")
    running = db.query_one(
        "SELECT id FROM autopilot_run WHERE project_id=? AND status='running'", (body.project_id,)
    )
    if running:
        raise HTTPException(409, "该项目已有自愈在运行")
    if not body.force:  # 限额预检：修复/复审任一引擎任一用量窗口超阈值则拦截
        for eng in {body.fix_engine, body.review_engine}:
            ok, wk, reason = quota.check_engine(eng)
            if not ok:
                raise HTTPException(429, reason + "。可勾选强制启动忽略。")
    try:
        run = autopilot.start(
            body.project_id,
            max(1, min(body.max_iterations, 10)),
            body.fix_engine,
            body.review_engine,
            max(0, body.token_budget),
            body.scope,
            body.scope_arg,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return run


@router.post("/{run_id:int}/stop")
def stop(run_id: int):
    autopilot.stop(run_id)
    return {"ok": True}


@router.get("")
def list_runs(project_id: int | None = None):
    if project_id is not None:
        rows = db.query(
            "SELECT * FROM autopilot_run WHERE project_id=? ORDER BY created_at DESC LIMIT 20",
            (project_id,),
        )
    else:
        rows = db.query("SELECT * FROM autopilot_run ORDER BY created_at DESC LIMIT 20")
    return [dict(r) for r in rows]


@router.get("/{run_id:int}")
def get_run(run_id: int):
    row = db.query_one("SELECT * FROM autopilot_run WHERE id=?", (run_id,))
    if row is None:
        raise HTTPException(404, "run 不存在")
    return dict(row)


class PolicyBody(BaseModel):
    project_id: int
    name: str
    scope: str = "recent"
    scope_arg: str | None = None
    fix_engine: str = "cc"
    review_engine: str = "codex"
    max_iterations: int = 2
    token_budget: int = 0
    interval_minutes: int = 60
    enabled: bool = True


class PolicyPatch(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None


@router.get("/policies")
def list_policies(project_id: int | None = None):
    import time

    sql = "SELECT * FROM policy"
    params: tuple = ()
    if project_id is not None:
        sql += " WHERE project_id=?"
        params = (project_id,)
    sql += " ORDER BY created_at DESC"
    rows = [dict(r) for r in db.query(sql, params)]
    now = time.time()
    for r in rows:  # 附带下次触发的估算(秒)
        if r["last_run_at"] is None:
            r["next_in"] = 0
        else:
            r["next_in"] = max(0, int(r["interval_minutes"] * 60 - (now - r["last_run_at"])))
    return rows


@router.post("/policies")
def create_policy(body: PolicyBody):
    import time

    if body.fix_engine not in CODING_ENGINES or body.review_engine not in CODING_ENGINES:
        raise HTTPException(400, "未知引擎")
    now = time.time()
    # last_run_at 置为现在 → 首次自动运行发生在一个完整间隔之后，避免刚建就烧 token
    pid = db.execute(
        "INSERT INTO policy(project_id,name,scope,scope_arg,fix_engine,review_engine,"
        "max_iterations,token_budget,interval_minutes,enabled,last_run_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (body.project_id, body.name, body.scope, body.scope_arg, body.fix_engine,
         body.review_engine, max(1, min(body.max_iterations, 10)), max(0, body.token_budget),
         max(5, body.interval_minutes), int(body.enabled), now, now),
    )
    return dict(db.query_one("SELECT * FROM policy WHERE id=?", (pid,)))


@router.patch("/policies/{policy_id}")
def patch_policy(policy_id: int, body: PolicyPatch):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "enabled" in fields:
        fields["enabled"] = int(fields["enabled"])
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE policy SET {sets} WHERE id=?", (*fields.values(), policy_id))
    return dict(db.query_one("SELECT * FROM policy WHERE id=?", (policy_id,)))


@router.delete("/policies/{policy_id}")
def delete_policy(policy_id: int):
    db.execute("DELETE FROM policy WHERE id=?", (policy_id,))
    return {"ok": True}


@router.post("/policies/{policy_id}/run-now")
def run_policy_now(policy_id: int):
    import time

    p = db.query_one("SELECT * FROM policy WHERE id=?", (policy_id,))
    if p is None:
        raise HTTPException(404, "策略不存在")
    if autopilot.has_active_run(p["project_id"]):
        raise HTTPException(409, "该项目已有自愈在运行")
    db.execute("UPDATE policy SET last_run_at=? WHERE id=?", (time.time(), policy_id))
    run = autopilot.start(
        p["project_id"], p["max_iterations"], p["fix_engine"], p["review_engine"],
        p["token_budget"], p["scope"], p["scope_arg"], policy_id,
    )
    return run


@router.get("/{run_id:int}/spans")
def get_spans(run_id: int):
    rows = db.query(
        "SELECT * FROM autopilot_span WHERE run_id=? ORDER BY iteration, id", (run_id,)
    )
    return [dict(r) for r in rows]


@router.get("/{run_id:int}/log")
def get_log(run_id: int):
    row = db.query_one("SELECT log_path FROM autopilot_run WHERE id=?", (run_id,))
    if row is None or not row["log_path"]:
        return {"log": ""}
    try:
        with open(row["log_path"], errors="replace") as f:
            return {"log": f.read()}
    except FileNotFoundError:
        return {"log": ""}
