"""可配置的线性多 CLI 编排。

模板只描述「角色名 + 引擎 + 角色提示词」。运行时保存模板快照，每一步仍由普通 task
承载；本模块只负责顺序状态机和阶段产物，不直接管理 PTY/SDK。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from . import db, engine_settings, events, scheduler
from .engines import CODING_ENGINES, normalize_engine_id

MIN_STEPS = 2
MAX_STEPS = 5
MAX_ARTIFACT_BYTES = 200 * 1024
TERMINAL_RUN_STATUSES = {"done", "failed", "cancelled", "interrupted"}
TERMINAL_STEP_STATUSES = {"done", "failed", "cancelled", "interrupted"}


class OrchestrationError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _normalize_runtime(engine: str, model: object, reasoning_effort: object) -> tuple[str, str]:
    raw_reasoning = str(reasoning_effort or "").strip()
    if engine == "claude":
        normalized_model = engine_settings.normalize_claude_model(model)
        normalized_reasoning = engine_settings.normalize_claude_effort(raw_reasoning)
    elif engine == "codex":
        normalized_model = engine_settings.normalize_codex_model(model)
        normalized_reasoning = engine_settings.normalize_codex_effort(raw_reasoning)
    elif engine == "omp":
        normalized_model = engine_settings.normalize_omp_model(model)
        normalized_reasoning = engine_settings.normalize_omp_thinking(raw_reasoning)
    else:
        normalized_model = engine_settings.normalize_kimi_model(model)
        normalized_reasoning = ""
    if raw_reasoning and not normalized_reasoning:
        raise OrchestrationError(f"{engine} 不支持思考强度: {raw_reasoning}")
    return normalized_model, normalized_reasoning


def _normalize_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not MIN_STEPS <= len(steps) <= MAX_STEPS:
        raise OrchestrationError(f"编排步骤数必须为 {MIN_STEPS}～{MAX_STEPS}")
    normalized = []
    for position, raw in enumerate(steps, 1):
        name = str(raw.get("name") or "").strip()
        engine = normalize_engine_id(str(raw.get("engine") or "").strip())
        role_prompt = str(raw.get("role_prompt") or "").strip()
        if not name:
            raise OrchestrationError(f"第 {position} 步角色名称不能为空")
        if engine not in CODING_ENGINES:
            raise OrchestrationError(f"第 {position} 步引擎不可用: {engine}")
        if not role_prompt:
            raise OrchestrationError(f"第 {position} 步角色提示词不能为空")
        model, reasoning_effort = _normalize_runtime(
            engine,
            raw.get("model"),
            raw.get("reasoning_effort"),
        )
        normalized.append({
            "position": position,
            "name": name,
            "engine": engine,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "role_prompt": role_prompt,
        })
    return normalized


def _template_dict(row) -> dict:
    out = dict(row)
    out["enabled"] = bool(out["enabled"])
    out["steps"] = [
        dict(step) for step in db.query(
            "SELECT id,position,name,engine,model,reasoning_effort,role_prompt,created_at,updated_at "
            "FROM orchestration_step WHERE orchestration_id=? ORDER BY position",
            (out["id"],),
        )
    ]
    return out


def list_templates(include_disabled: bool = True) -> list[dict]:
    condition = "deleted=0" if include_disabled else "deleted=0 AND enabled=1"
    return [_template_dict(row) for row in db.query(
        f"SELECT * FROM orchestration WHERE {condition} ORDER BY created_at,id"
    )]


def get_template(template_id: int) -> dict:
    row = db.query_one("SELECT * FROM orchestration WHERE id=? AND deleted=0", (template_id,))
    if row is None:
        raise OrchestrationError("编排不存在", 404)
    return _template_dict(row)


def create_template(name: str, steps: list[dict[str, Any]], enabled: bool = True) -> dict:
    clean_name = (name or "").strip()
    if not clean_name:
        raise OrchestrationError("编排名称不能为空")
    normalized = _normalize_steps(steps)
    now = time.time()
    try:
        with db._lock:
            conn = db.get_conn()
            with conn:
                cur = conn.execute(
                    "INSERT INTO orchestration(name,enabled,deleted,created_at,updated_at) "
                    "VALUES(?,?,0,?,?)",
                    (clean_name, int(enabled), now, now),
                )
                template_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO orchestration_step(orchestration_id,position,name,engine,model,reasoning_effort,role_prompt,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            template_id, step["position"], step["name"], step["engine"], step["model"],
                            step["reasoning_effort"], step["role_prompt"], now, now,
                        )
                        for step in normalized
                    ],
                )
    except sqlite3.IntegrityError as exc:
        raise OrchestrationError("编排名称已存在", 409) from exc
    events.emit("orchestration.changed", {"orchestration_id": template_id})
    return get_template(template_id)


def update_template(template_id: int, name: str, steps: list[dict[str, Any]], enabled: bool = True) -> dict:
    get_template(template_id)
    clean_name = (name or "").strip()
    if not clean_name:
        raise OrchestrationError("编排名称不能为空")
    normalized = _normalize_steps(steps)
    now = time.time()
    try:
        with db._lock:
            conn = db.get_conn()
            with conn:
                conn.execute(
                    "UPDATE orchestration SET name=?,enabled=?,updated_at=? WHERE id=? AND deleted=0",
                    (clean_name, int(enabled), now, template_id),
                )
                conn.execute("DELETE FROM orchestration_step WHERE orchestration_id=?", (template_id,))
                conn.executemany(
                    "INSERT INTO orchestration_step(orchestration_id,position,name,engine,model,reasoning_effort,role_prompt,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            template_id, step["position"], step["name"], step["engine"], step["model"],
                            step["reasoning_effort"], step["role_prompt"], now, now,
                        )
                        for step in normalized
                    ],
                )
    except sqlite3.IntegrityError as exc:
        raise OrchestrationError("编排名称已存在", 409) from exc
    events.emit("orchestration.changed", {"orchestration_id": template_id})
    return get_template(template_id)


def delete_template(template_id: int) -> None:
    if db.execute_rowcount(
        "UPDATE orchestration SET deleted=1,enabled=0,updated_at=? WHERE id=? AND deleted=0",
        (time.time(), template_id),
    ) == 0:
        raise OrchestrationError("编排不存在", 404)
    events.emit("orchestration.changed", {"orchestration_id": template_id})


def _snapshot(template: dict) -> dict:
    return {
        "id": template["id"],
        "name": template["name"],
        "steps": [
            {
                "position": step["position"],
                "name": step["name"],
                "engine": step["engine"],
                "model": step["model"],
                "reasoning_effort": step["reasoning_effort"],
                "role_prompt": step["role_prompt"],
            }
            for step in template["steps"]
        ],
    }


def _render_prompt(run, snapshot: dict, step: dict, previous: list) -> tuple[str, str]:
    if previous:
        blocks = []
        for item in previous:
            blocks.append(
                f"### 第 {item['position']} 步 · {item['name']}\n{item['output_artifact'] or '（无产物）'}"
            )
        prior = "\n\n".join(blocks)
    else:
        prior = "（无）"
    prompt = f"""【Bosun 编排控制信息】
编排：{snapshot['name']}
当前步骤：{step['position']}/{len(snapshot['steps'])}
当前角色：{step['name']}

【角色要求】
{step['role_prompt']}

【原始任务】
---
{run['original_prompt']}
---

【前序角色产物】
---
{prior}
---

【步骤完成约定】
- 可以进入下一步：回报 done
- 缺少用户信息或认为当前不应继续：回报 needs_input
- 无法完成当前角色：回报 failed
- 按收尾约定先提交完整阶段产物，再用 summary 写一句话回执
- 当前 CLI 不得自行启动下一 CLI，后续步骤由 Bosun 调度
"""
    return prompt, prior


def _insert_step(conn, run, snapshot: dict, position: int, now: float) -> int:
    step = snapshot["steps"][position - 1]
    previous = conn.execute(
        "SELECT position,name,output_artifact FROM orchestration_step_run "
        "WHERE run_id=? AND position<? ORDER BY position",
        (run["id"], position),
    ).fetchall()
    prompt, input_artifact = _render_prompt(run, snapshot, step, previous)
    title = f"{run['title'] or run['original_prompt'][:40]} · {step['name']}"
    task_cur = conn.execute(
        "INSERT INTO task(project_id,engine,prompt,title,priority,auto_approve,kind,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,'queued',?)",
        (
            run["project_id"], step["engine"], prompt, title, run["priority"],
            run["auto_approve"], "orchestration", now,
        ),
    )
    task_id = task_cur.lastrowid
    conn.execute(
        "INSERT INTO orchestration_step_run(run_id,position,name,engine,model,reasoning_effort,role_prompt,task_id,status,"
        "input_artifact,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'queued',?,?,?)",
        (
            run["id"], position, step["name"], step["engine"], step.get("model", ""),
            step.get("reasoning_effort", ""), step["role_prompt"], task_id,
            input_artifact, now, now,
        ),
    )
    conn.execute(
        "UPDATE orchestration_run SET status='running',current_position=?,started_at=COALESCE(started_at,?),"
        "updated_at=?,ended_at=NULL WHERE id=?",
        (position, now, now, run["id"]),
    )
    return task_id


def create_run(
    template_id: int,
    project_id: int,
    prompt: str,
    title: str | None,
    priority: int,
    auto_approve: bool,
    start: bool = False,
) -> dict:
    template = get_template(template_id)
    if not template["enabled"]:
        raise OrchestrationError("编排已停用", 409)
    if db.query_one("SELECT id FROM project WHERE id=?", (project_id,)) is None:
        raise OrchestrationError("项目不存在", 404)
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        raise OrchestrationError("任务指令不能为空")
    snapshot = _snapshot(template)
    snapshot_text = json.dumps(snapshot, ensure_ascii=False)
    now = time.time()
    with db._lock:
        conn = db.get_conn()
        with conn:
            cur = conn.execute(
                "INSERT INTO orchestration_run(orchestration_id,definition_snapshot,project_id,original_prompt,"
                "title,priority,auto_approve,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,? ,?,?)",
                (
                    template_id, snapshot_text, project_id, clean_prompt, (title or "").strip() or None,
                    max(1, min(10, int(priority))), int(auto_approve), "queued" if start else "draft", now, now,
                ),
            )
            run_id = cur.lastrowid
            if start:
                run = conn.execute("SELECT * FROM orchestration_run WHERE id=?", (run_id,)).fetchone()
                _insert_step(conn, run, snapshot, 1, now)
    if start:
        scheduler.tick()
    events.emit("orchestration.run", {"run_id": run_id, "status": "running" if start else "draft"})
    return get_run(run_id)


def start_run(run_id: int) -> dict:
    now = time.time()
    created = False
    with db._lock:
        conn = db.get_conn()
        with conn:
            run = conn.execute("SELECT * FROM orchestration_run WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise OrchestrationError("编排运行不存在", 404)
            if run["status"] != "draft":
                raise OrchestrationError("只有待办编排可以启动", 409)
            snapshot = json.loads(run["definition_snapshot"])
            _insert_step(conn, run, snapshot, 1, now)
            created = True
    if created:
        scheduler.tick()
        events.emit("orchestration.run", {"run_id": run_id, "status": "running"})
    return get_run(run_id)


def _run_dict(row) -> dict:
    out = dict(row)
    out["auto_approve"] = bool(out["auto_approve"])
    steps = []
    for step in db.query("SELECT * FROM orchestration_step_run WHERE run_id=? ORDER BY position", (out["id"],)):
        item = dict(step)
        if item["task_id"] is not None:
            task = db.query_one("SELECT status FROM task WHERE id=?", (item["task_id"],))
            if task and item["status"] not in TERMINAL_STEP_STATUSES:
                item["task_status"] = task["status"]
        steps.append(item)
    out["steps"] = steps
    return out


def get_run(run_id: int) -> dict:
    row = db.query_one("SELECT * FROM orchestration_run WHERE id=?", (run_id,))
    if row is None:
        raise OrchestrationError("编排运行不存在", 404)
    return _run_dict(row)


def list_runs(project_id: int | None = None) -> list[dict]:
    if project_id is None:
        rows = db.query("SELECT * FROM orchestration_run ORDER BY created_at DESC,id DESC")
    else:
        rows = db.query(
            "SELECT * FROM orchestration_run WHERE project_id=? ORDER BY created_at DESC,id DESC",
            (project_id,),
        )
    return [_run_dict(row) for row in rows]


def _step_for_task(task_id: int):
    return db.query_one("SELECT * FROM orchestration_step_run WHERE task_id=?", (task_id,))


def save_task_artifact(task_id: int, artifact: str) -> dict:
    step = _step_for_task(task_id)
    if step is None:
        raise OrchestrationError("目标任务不是编排步骤", 409)
    if not artifact.strip():
        raise OrchestrationError("阶段产物不能为空")
    size = len(artifact.encode("utf-8"))
    if size > MAX_ARTIFACT_BYTES:
        raise OrchestrationError("阶段产物过大（上限 200 KiB）", 413)
    placeholders = ",".join("?" for _ in TERMINAL_RUN_STATUSES)
    step_placeholders = ",".join("?" for _ in TERMINAL_STEP_STATUSES)
    changed = db.execute_rowcount(
        f"UPDATE orchestration_step_run SET output_artifact=?,updated_at=? "
        f"WHERE id=? AND status NOT IN ({step_placeholders}) AND EXISTS ("
        f"SELECT 1 FROM orchestration_run WHERE id=? AND status NOT IN ({placeholders}))",
        (
            artifact, time.time(), step["id"], *sorted(TERMINAL_STEP_STATUSES),
            step["run_id"], *sorted(TERMINAL_RUN_STATUSES),
        ),
    )
    if changed == 0:
        raise OrchestrationError("当前编排步骤已结束，不能再提交产物", 409)
    return {"ok": True, "bytes": size}


def validate_task_report(task_id: int, result: str, artifact: str | None) -> bool:
    step = _step_for_task(task_id)
    if step is None:
        return False
    if artifact is not None and len(artifact.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise OrchestrationError("阶段产物过大（上限 200 KiB）", 413)
    if result == "done" and not (artifact or "").strip() and not (step["output_artifact"] or "").strip():
        raise OrchestrationError("编排步骤完成前必须提交完整阶段产物")
    return True


def _advance_after_done(conn, run, step, now: float) -> bool:
    snapshot = json.loads(run["definition_snapshot"])
    next_position = int(step["position"]) + 1
    if next_position > len(snapshot["steps"]):
        conn.execute(
            "UPDATE orchestration_run SET status='done',updated_at=?,ended_at=? "
            "WHERE id=? AND status NOT IN ('done','failed','cancelled','interrupted')",
            (now, now, run["id"]),
        )
        return False
    exists = conn.execute(
        "SELECT id FROM orchestration_step_run WHERE run_id=? AND position=?",
        (run["id"], next_position),
    ).fetchone()
    if exists is not None:
        return False
    _insert_step(conn, run, snapshot, next_position, now)
    return True


def handle_task_report(task_id: int, result: str, summary: str, artifact: str | None) -> dict | None:
    validate_task_report(task_id, result, artifact)
    now = time.time()
    should_tick = False
    run_id = None
    with db._lock:
        conn = db.get_conn()
        with conn:
            step = conn.execute("SELECT * FROM orchestration_step_run WHERE task_id=?", (task_id,)).fetchone()
            if step is None:
                return None
            run_id = step["run_id"]
            if step["status"] in TERMINAL_STEP_STATUSES:
                return get_run(run_id)
            run = conn.execute("SELECT * FROM orchestration_run WHERE id=?", (run_id,)).fetchone()
            if run is None or run["status"] in TERMINAL_RUN_STATUSES:
                return get_run(run_id) if run else None
            output = artifact if artifact is not None else step["output_artifact"]
            if result == "done":
                conn.execute(
                    "UPDATE orchestration_step_run SET status='done',result='done',summary=?,output_artifact=?,"
                    "updated_at=?,ended_at=? WHERE id=? AND status NOT IN ('done','failed','cancelled','interrupted')",
                    ((summary or "")[:2000], output, now, now, step["id"]),
                )
                should_tick = _advance_after_done(conn, run, step, now)
            elif result == "needs_input":
                conn.execute(
                    "UPDATE orchestration_step_run SET status='waiting_input',result=?,summary=?,output_artifact=?,"
                    "updated_at=? WHERE id=?",
                    (result, (summary or "")[:2000], output, now, step["id"]),
                )
                conn.execute(
                    "UPDATE orchestration_run SET status='waiting_input',updated_at=? WHERE id=?",
                    (now, run_id),
                )
            else:
                conn.execute(
                    "UPDATE orchestration_step_run SET status='failed',result='failed',summary=?,output_artifact=?,"
                    "updated_at=?,ended_at=? WHERE id=?",
                    ((summary or "")[:2000], output, now, now, step["id"]),
                )
                conn.execute(
                    "UPDATE orchestration_run SET status='failed',updated_at=?,ended_at=? WHERE id=?",
                    (now, now, run_id),
                )
    if should_tick:
        scheduler.tick()
    events.emit("orchestration.run", {"run_id": run_id, "status": get_run(run_id)["status"]})
    return get_run(run_id)


def cancel_run(run_id: int) -> dict:
    run = get_run(run_id)
    if run["status"] in TERMINAL_RUN_STATUSES:
        return run
    now = time.time()
    db.execute(
        "UPDATE orchestration_run SET status='cancelled',updated_at=?,ended_at=? WHERE id=?",
        (now, now, run_id),
    )
    current = next((step for step in reversed(run["steps"]) if step["status"] not in TERMINAL_STEP_STATUSES), None)
    if current and current["task_id"] is not None:
        db.execute(
            "UPDATE orchestration_step_run SET status='cancelled',result='cancelled',updated_at=?,ended_at=? WHERE id=?",
            (now, now, current["id"]),
        )
        scheduler.cancel(current["task_id"])
    events.emit("orchestration.run", {"run_id": run_id, "status": "cancelled"})
    return get_run(run_id)


def handle_task_cancelled(task_id: int) -> None:
    step = _step_for_task(task_id)
    if step is None:
        return
    run = db.query_one("SELECT status FROM orchestration_run WHERE id=?", (step["run_id"],))
    if run is None or run["status"] in TERMINAL_RUN_STATUSES:
        return
    now = time.time()
    db.execute(
        "UPDATE orchestration_step_run SET status='cancelled',result='cancelled',updated_at=?,ended_at=? WHERE id=?",
        (now, now, step["id"]),
    )
    db.execute(
        "UPDATE orchestration_run SET status='cancelled',updated_at=?,ended_at=? WHERE id=?",
        (now, now, step["run_id"]),
    )
    events.emit("orchestration.run", {"run_id": step["run_id"], "status": "cancelled"})


def handle_task_exit(task_id: int, exit_code: int) -> None:
    """步骤进程退出但没有权威回报时让 run 明确失败，不能永久挂 running。"""
    step = _step_for_task(task_id)
    if step is None or step["status"] in TERMINAL_STEP_STATUSES:
        return
    run = db.query_one("SELECT status FROM orchestration_run WHERE id=?", (step["run_id"],))
    if run is None or run["status"] in TERMINAL_RUN_STATUSES:
        return
    now = time.time()
    summary = "CLI 已退出但未提交阶段产物" if exit_code == 0 else f"CLI 异常退出（exit {exit_code}），未提交阶段产物"
    with db._lock:
        conn = db.get_conn()
        with conn:
            conn.execute(
                "UPDATE orchestration_step_run SET status='failed',result='failed',summary=?,updated_at=?,ended_at=? WHERE id=?",
                (summary, now, now, step["id"]),
            )
            conn.execute(
                "UPDATE orchestration_run SET status='failed',updated_at=?,ended_at=? WHERE id=?",
                (now, now, step["run_id"]),
            )
    events.emit("orchestration.run", {"run_id": step["run_id"], "status": "failed"})


def reconcile_on_startup() -> int:
    changed = 0
    should_tick = False
    now = time.time()
    with db._lock:
        conn = db.get_conn()
        with conn:
            runs = conn.execute(
                "SELECT * FROM orchestration_run WHERE status IN ('queued','running','waiting_input')"
            ).fetchall()
            for run in runs:
                step = conn.execute(
                    "SELECT * FROM orchestration_step_run WHERE run_id=? ORDER BY position DESC LIMIT 1",
                    (run["id"],),
                ).fetchone()
                if step is None:
                    snapshot = json.loads(run["definition_snapshot"])
                    _insert_step(conn, run, snapshot, 1, now)
                    changed += 1
                    should_tick = True
                    continue
                if step["status"] == "done":
                    created = _advance_after_done(conn, run, step, now)
                    changed += 1
                    should_tick = should_tick or created
                    continue
                task = conn.execute("SELECT status FROM task WHERE id=?", (step["task_id"],)).fetchone()
                if task is None or task["status"] in {"done", "failed", "cancelled", "interrupted"}:
                    task_status = task["status"] if task else "interrupted"
                    if task_status in {"done", "failed"}:
                        step_status = run_status = "failed"
                        summary = "CLI 已结束但未提交阶段产物"
                    else:
                        step_status = run_status = task_status
                        summary = "编排步骤在服务恢复前已中止"
                    conn.execute(
                        "UPDATE orchestration_step_run SET status=?,result=?,summary=?,updated_at=?,ended_at=? WHERE id=?",
                        (step_status, step_status, summary, now, now, step["id"]),
                    )
                    conn.execute(
                        "UPDATE orchestration_run SET status=?,updated_at=?,ended_at=? WHERE id=?",
                        (run_status, now, now, run["id"]),
                    )
                    changed += 1
    if should_tick:
        scheduler.tick()
    return changed
