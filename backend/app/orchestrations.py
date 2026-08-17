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

# ---- 常驻班组（见 docs/spec-orchestration-crew.md）----
REPORT_ROLE_NAME = "汇报收口"
REPORT_ROLE_PROMPT = (
    "你是本次编排的汇报人，不承担新的实施工作。请通读全部阶段产物与角色间消息，"
    "输出面向用户的最终结论：做了什么、结论是什么、验证到什么程度、遗留风险与建议。"
    "结论必须自洽可读，不要让用户再去翻各步骤产物。"
)
# 失控保护：任何一项触顶都转人工裁决或收口，绝不静默继续
MAX_REWORK_TOTAL = 3        # 单个 run 的返工总次数
MAX_MESSAGES_PER_RUN = 200  # 单个 run 的消息总条数
MAX_MESSAGE_CHARS = 4000    # 单条消息长度
MAX_PINGPONG = 6            # 同一对角色连续往返次数（乒乓熔断）
MESSAGE_KINDS = {"handoff", "rework", "ask", "answer", "system"}
RELIABLE_MESSAGE_KINDS = {"handoff", "rework", "ask", "answer"}
MESSAGE_ACK_TIMEOUT = 60.0
MAX_DELIVERY_ATTEMPTS = 3   # 首投、原会话重投、恢复会话后补投
MAX_MESSAGE_RECOVERIES = 1
MAX_ROLE_NUDGES = 1
MAX_ROLE_RECOVERIES = 1


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
    steps = [
        {
            "position": step["position"],
            "name": step["name"],
            "engine": step["engine"],
            "model": step["model"],
            "reasoning_effort": step["reasoning_effort"],
            "role_prompt": step["role_prompt"],
            "role_kind": "step",
        }
        for step in template["steps"]
    ]
    if template.get("report_enabled", True):
        # 收口角色：没有它，最后一步的产物就默认成了"最终结论"，而最后一步通常是执行者
        # 而不是汇报者。引擎沿用第一步（用户已经为这个编排选过的 CLI），模板可改。
        first = steps[0]
        steps.append({
            "position": len(steps) + 1,
            "name": REPORT_ROLE_NAME,
            "engine": first["engine"],
            "model": first["model"],
            "reasoning_effort": first["reasoning_effort"],
            "role_prompt": REPORT_ROLE_PROMPT,
            "role_kind": "report",
        })
    return {"id": template["id"], "name": template["name"], "steps": steps}


def _roster(snapshot: dict) -> str:
    return "\n".join(
        f"- 第 {item['position']} 位 · {item['name']}（{item['engine']}）"
        + ("（汇报收口）" if item.get("role_kind") == "report" else "")
        for item in snapshot["steps"]
    )


def _render_prompt(run, snapshot: dict, step: dict, previous: list) -> tuple[str, str]:
    """角色会话的开场 prompt。

    班组编排里全体角色一次性拉起，所以开场分两种身份：持棒者直接开工，其余角色待命
    等 Bosun 投递消息。产物不再只在开场传递（交棒和返工意见都靠消息投递进活会话），
    但开场仍带上已有产物，避免角色掉线重拉后丢失上下文。
    """
    if previous:
        prior = "\n\n".join(
            f"### 第 {item['position']} 位 · {item['name']}\n{item['output_artifact'] or '（无产物）'}"
            for item in previous
        )
    else:
        prior = "（无）"
    active = run["current_position"] == step["position"]
    is_report = step.get("role_kind") == "report"
    if active:
        duty = "现在轮到你了，请立即开始你这一环的工作。"
    else:
        duty = (
            "现在还没轮到你，请保持待命：**不要开始任何实施工作、不要回报 done**。"
            "Bosun 会在轮到你、或别的角色点名问你时，把消息投递进这个会话，"
            "收到消息后再行动。"
        )
    prompt = f"""【Bosun 编排控制信息】
编排：{snapshot['name']}
班组成员：
{_roster(snapshot)}
你的位置：第 {step['position']} 位 · {step['name']}
当前持棒者：第 {run['current_position']} 位

【角色要求】
{step['role_prompt']}

【当前状态】
{duty}

【原始任务】
---
{run['original_prompt']}
---

【已有阶段产物】
---
{prior}
---

【协作约定】
- 完成本环工作：先提交完整阶段产物，再回报 done（summary 只写一句话回执）
- 发现前面某一位做错了、需要返工：回报 rework，写明打回到第几位和返工意见
- 缺少用户信息：回报 needs_input；本角色确实无法完成：回报 failed
- 想问班组里另一位：给对方发消息（见 bosun-report skill 的发消息用法），不要替对方做
- 不得自行启动别的 CLI，也不得替 Bosun 决定下一位是谁
{'- 你是收口汇报人：产出面向用户的最终结论，这是整个编排的唯一交付物' if is_report else ''}
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
        "input_artifact,role_kind,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'queued',?,?,?,?)",
        (
            run["id"], position, step["name"], step["engine"], step.get("model", ""),
            step.get("reasoning_effort", ""), step["role_prompt"], task_id,
            input_artifact, step.get("role_kind", "step"), now, now,
        ),
    )
    return task_id


def _create_all_roles(conn, run, snapshot: dict, now: float) -> list[int]:
    """一次性把班组全员建出来：接力棒落在第 1 位，其余角色以待命身份启动。

    全员同时在线是「互相发消息」的前提；分批拉起会互锁——先起的角色在等还没被创建的
    角色答话。已存在的位置跳过，保证重入幂等（恢复路径会重跑本函数）。
    """
    conn.execute(
        "UPDATE orchestration_run SET status='running',current_position=COALESCE(current_position,1),"
        "started_at=COALESCE(started_at,?),updated_at=?,ended_at=NULL WHERE id=?",
        (now, now, run["id"]),
    )
    run = conn.execute("SELECT * FROM orchestration_run WHERE id=?", (run["id"],)).fetchone()
    existing = {
        row["position"] for row in conn.execute(
            "SELECT position FROM orchestration_step_run WHERE run_id=?", (run["id"],)
        )
    }
    task_ids = []
    for position in range(1, len(snapshot["steps"]) + 1):
        if position in existing:
            continue
        task_ids.append(_insert_step(conn, run, snapshot, position, now))
    conn.execute(
        "UPDATE orchestration_step_run SET status='running',started_at=COALESCE(started_at,?),"
        "updated_at=? WHERE run_id=? AND position=? AND status='queued'",
        (now, now, run["id"], run["current_position"]),
    )
    return task_ids


def _dispatch_roles(task_ids: list[int]) -> None:
    """锁外拉起角色进程：绕开并发槽，否则后半个班组永远起不来（见 scheduler 里的说明）。"""
    for task_id in task_ids:
        try:
            scheduler.start_orchestration_role(task_id)
        except Exception:  # 单个角色起不来不该拖垮整班；它的任务会落 failed 并被对账看到
            continue


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
            task_ids = []
            if start:
                run = conn.execute("SELECT * FROM orchestration_run WHERE id=?", (run_id,)).fetchone()
                task_ids = _create_all_roles(conn, run, snapshot, now)
    if start:
        _dispatch_roles(task_ids)
    events.emit("orchestration.run", {"run_id": run_id, "status": "running" if start else "draft"})
    return get_run(run_id)


def start_run(run_id: int) -> dict:
    now = time.time()
    task_ids: list[int] = []
    with db._lock:
        conn = db.get_conn()
        with conn:
            run = conn.execute("SELECT * FROM orchestration_run WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise OrchestrationError("编排运行不存在", 404)
            if run["status"] != "draft":
                raise OrchestrationError("只有待办编排可以启动", 409)
            snapshot = json.loads(run["definition_snapshot"])
            task_ids = _create_all_roles(conn, run, snapshot, now)
    if task_ids:
        _dispatch_roles(task_ids)
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


def _queue_message(conn, run_id: int, from_position: int | None, to_position: int,
                   kind: str, body: str, now: float) -> int:
    """把消息落库（投递是另一步）。落库先于投递：目标掉线时它就是待补投的凭据。"""
    message_id = conn.execute(
        "INSERT INTO orchestration_message(run_id,from_position,to_position,kind,body,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (run_id, from_position, to_position, kind, body[:MAX_MESSAGE_CHARS], now),
    ).lastrowid
    if kind in RELIABLE_MESSAGE_KINDS:
        ack = (
            f"\n\n【可靠投递】这是消息 #{message_id}。处理正文前先确认收件：\n"
            "curl -sS -X POST -H 'Content-Type: application/json' "
            "-H \"Authorization: Bearer $BOSUN_TASK_TOKEN\" "
            '-d "{\\"reporter_pid\\":$$}" '
            f"\"$BOSUN_API/api/tasks/$BOSUN_TASK_ID/messages/{message_id}/ack\"\n"
            "非 2xx 必须说明；ACK 只确认收到，不代表本环完成。"
        )
        prefix = f"【Bosun 消息 #{message_id}】\n"
        available = MAX_MESSAGE_CHARS - len(prefix) - len(ack)
        stored = prefix + body[:max(0, available)] + ack
        conn.execute(
            "UPDATE orchestration_message SET body=? WHERE id=?", (stored, message_id)
        )
    return message_id


def _message_body(conn, message_id: int) -> str:
    row = conn.execute("SELECT body FROM orchestration_message WHERE id=?", (message_id,)).fetchone()
    return row["body"] if row else ""


def _archive_artifact(conn, step, now: float) -> None:
    """返工前把当前产物存成历史版本：打回不抹掉已经做出来的东西。"""
    if not (step["output_artifact"] or "").strip():
        return
    conn.execute(
        "INSERT OR IGNORE INTO orchestration_step_artifact"
        "(step_run_id,attempt,output_artifact,summary,created_at) VALUES(?,?,?,?,?)",
        (step["id"], step["attempt"], step["output_artifact"], step["summary"], now),
    )


def _set_baton(conn, run_id: int, position: int, now: float) -> None:
    conn.execute(
        "UPDATE orchestration_run SET current_position=?,status='running',updated_at=?,ended_at=NULL "
        "WHERE id=? AND status NOT IN ('done','failed','cancelled','interrupted')",
        (position, now, run_id),
    )


def _handoff_body(step, snapshot: dict, output: str | None) -> str:
    total = len(snapshot["steps"])
    return (
        f"【Bosun 交棒】第 {step['position']}/{total} 位 · {step['name']} 已完成，接力棒交给你。\n"
        f"上一位的阶段产物：\n---\n{(output or '（无产物）')}\n---\n"
        f"请开始你这一环的工作；完成后提交阶段产物并回报 done。"
    )


def _advance_after_done(conn, run, step, now: float) -> tuple[int | None, int | None, str | None]:
    """交棒：不再新建下一步（全员早已在线），只移动接力棒并把产物投给下一位。

    返回 (下一位 task_id, 消息 id, 待投递消息)；最后一位返回三个 None。
    """
    snapshot = json.loads(run["definition_snapshot"])
    next_position = int(step["position"]) + 1
    if next_position > len(snapshot["steps"]):
        conn.execute(
            "UPDATE orchestration_run SET status='done',current_position=?,updated_at=?,ended_at=? "
            "WHERE id=? AND status NOT IN ('done','failed','cancelled','interrupted')",
            (step["position"], now, now, run["id"]),
        )
        return None, None, None
    nxt = conn.execute(
        "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=?",
        (run["id"], next_position),
    ).fetchone()
    if nxt is None:  # 老 run（逐步创建的）恢复过来：缺谁补谁
        _insert_step(conn, run, snapshot, next_position, now)
        nxt = conn.execute(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=?",
            (run["id"], next_position),
        ).fetchone()
    message_id = _queue_message(
        conn, run["id"], step["position"], next_position, "handoff",
        _handoff_body(step, snapshot, step["output_artifact"]), now,
    )
    body = _message_body(conn, message_id)
    if nxt["status"] in TERMINAL_STEP_STATUSES:
        # 下一位已经被取消/失败：不能把它就地复活，也不能跳过它继续往下走
        # （跳过等于悄悄砍掉编排里的一环）。交棒消息滞留，交用户裁决。
        conn.execute(
            "UPDATE orchestration_run SET status='waiting_input',updated_at=? WHERE id=?",
            (now, run["id"]),
        )
        return None, None, None
    _set_baton(conn, run["id"], next_position, now)
    conn.execute(
        "UPDATE orchestration_step_run SET status='queued',updated_at=? "
        "WHERE id=?",
        (now, nxt["id"]),
    )
    return nxt["task_id"], message_id, body


def _apply_rework(conn, run, step, target_position: int, note: str, now: float
                  ) -> tuple[int | None, int | None, str | None, bool]:
    """打回返工：接力棒回退到目标位置，目标角色带着意见重跑。

    返回 (目标 task_id, 消息 id, 待投递消息, 是否触发限次熔断)。旧产物归档而不丢弃。
    """
    snapshot = json.loads(run["definition_snapshot"])
    if not 1 <= target_position <= len(snapshot["steps"]):
        raise OrchestrationError(f"返工目标位置不存在: {target_position}")
    if target_position >= int(step["position"]):
        raise OrchestrationError("只能打回给前面的角色")
    if int(run["rework_total"] or 0) + 1 > MAX_REWORK_TOTAL:
        # 熔断：交人工裁决，绝不静默继续转圈
        conn.execute(
            "UPDATE orchestration_run SET status='waiting_input',updated_at=? WHERE id=?",
            (now, run["id"]),
        )
        _queue_message(
            conn, run["id"], step["position"], target_position, "system",
            f"【Bosun】返工次数已达上限（{MAX_REWORK_TOTAL} 次），编排暂停等待用户裁决。", now,
        )
        return None, None, None, True
    target = conn.execute(
        "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=?",
        (run["id"], target_position),
    ).fetchone()
    if target is None:
        raise OrchestrationError(f"返工目标角色不在线: 第 {target_position} 位")
    _archive_artifact(conn, target, now)
    body = (
        f"【Bosun 返工】第 {step['position']} 位 · {step['name']} 把工作打回给你，"
        f"请按以下意见重做，完成后重新提交阶段产物并回报 done。\n"
        f"返工意见：\n---\n{note or '（未填写意见）'}\n---"
    )
    message_id = _queue_message(
        conn, run["id"], step["position"], target_position, "rework", body, now,
    )
    body = _message_body(conn, message_id)
    conn.execute(
        "UPDATE orchestration_step_run SET status='queued',result=NULL,output_artifact=NULL,summary=NULL,"
        "attempt=attempt+1,rework_count=rework_count+1,nudge_count=0,recovery_count=0,"
        "last_nudged_at=NULL,updated_at=?,ended_at=NULL WHERE id=?",
        (now, target["id"]),
    )
    # 打回点之后的角色（含发起者）回到待执行：它们的结论建立在被推翻的产物上
    conn.execute(
        "UPDATE orchestration_step_run SET status='queued',result=NULL,nudge_count=0,recovery_count=0,"
        "last_nudged_at=NULL,updated_at=? "
        "WHERE run_id=? AND position>? AND status NOT IN ('cancelled')",
        (now, run["id"], target_position),
    )
    # 这些常驻 task 上一轮的 done/rework 回报属于已被推翻的执行；不清会让监督器误判
    # “已有权威结论”而跳过催办，也会污染前端当前轮状态。
    conn.execute(
        "UPDATE task SET report_result=NULL,report_summary=NULL WHERE id IN ("
        "SELECT task_id FROM orchestration_step_run WHERE run_id=? AND position>=?"
        ")",
        (run["id"], target_position),
    )
    conn.execute(
        "UPDATE orchestration_run SET rework_total=rework_total+1,updated_at=? WHERE id=?",
        (now, run["id"]),
    )
    _set_baton(conn, run["id"], target_position, now)
    return target["task_id"], message_id, body, False


def handle_task_report(task_id: int, result: str, summary: str, artifact: str | None,
                       target_position: int | None = None) -> dict | None:
    validate_task_report(task_id, result, artifact)
    now = time.time()
    run_id = None
    pending: tuple[int, int, str] | None = None  # (目标 task_id, 消息 id, 待投递消息)
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
            # 接力棒唯一：待命角色的 done 不算数，否则全员在线就意味着谁都能推进流程
            if result in {"done", "rework"} and run["current_position"] != step["position"]:
                raise OrchestrationError(
                    f"当前持棒者是第 {run['current_position']} 位，你（第 {step['position']} 位）"
                    "现在处于待命，请等 Bosun 把接力棒交给你", 409,
                )
            # 权威回报本身足以证明当前角色已经消费交棒；兼容尚未更新 ACK 约定的会话。
            conn.execute(
                "UPDATE orchestration_message SET acknowledged_at=COALESCE(acknowledged_at,?) "
                "WHERE run_id=? AND to_position=? AND kind IN ('handoff','rework')",
                (now, run_id, step["position"]),
            )
            output = artifact if artifact is not None else step["output_artifact"]
            if result == "done":
                conn.execute(
                    "UPDATE orchestration_step_run SET status='done',result='done',summary=?,output_artifact=?,"
                    "updated_at=?,ended_at=? WHERE id=? AND status NOT IN ('done','failed','cancelled','interrupted')",
                    ((summary or "")[:2000], output, now, now, step["id"]),
                )
                step = conn.execute("SELECT * FROM orchestration_step_run WHERE id=?", (step["id"],)).fetchone()
                next_task_id, message_id, body = _advance_after_done(conn, run, step, now)
                if next_task_id and message_id and body:
                    pending = (next_task_id, message_id, body)
            elif result == "rework":
                target_task_id, message_id, body, _tripped = _apply_rework(
                    conn, run, step, int(target_position or 0), summary or "", now,
                )
                if target_task_id and message_id and body:
                    pending = (target_task_id, message_id, body)
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
    if pending:
        _hand_over(run_id, task_id, *pending)
    # 回收的判据是 run 真的落了终态，而不是「某一步结束了」——交棒受阻时 run 还活着
    if get_run(run_id)["status"] in TERMINAL_RUN_STATUSES:
        release_run_sessions(run_id)
    events.emit("orchestration.run", {"run_id": run_id, "status": get_run(run_id)["status"]})
    return get_run(run_id)


def _hand_over(
    run_id: int, from_task_id: int, to_task_id: int, message_id: int, body: str
) -> None:
    """交棒/返工的落地动作：切换双方待命身份，把消息投进目标会话。

    投递失败（目标掉线）不是致命错：消息已落库，`deliver_pending` 会在它回来后补投。
    """
    scheduler.set_standby(from_task_id, True)
    scheduler.set_standby(to_task_id, False)
    if scheduler.deliver_message(to_task_id, body):
        _record_delivery(message_id)
    else:
        _revive_role(run_id, to_task_id)


def _record_delivery(message_id: int, now: float | None = None) -> None:
    delivered_at = time.time() if now is None else now
    db.execute(
        "UPDATE orchestration_message SET delivered_at=COALESCE(delivered_at,?),"
        "last_delivered_at=?,delivery_attempts=delivery_attempts+1 WHERE id=?",
        (delivered_at, delivered_at, message_id),
    )


def _record_failed_delivery(message_id: int, now: float) -> None:
    db.execute(
        "UPDATE orchestration_message SET last_delivered_at=?,"
        "delivery_attempts=delivery_attempts+1 WHERE id=?",
        (now, message_id),
    )


def acknowledge_message(task_id: int, message_id: int) -> dict:
    """目标角色确认消息已进入自己的推理回合；重复 ACK 幂等。"""
    now = time.time()
    with db._lock:
        conn = db.get_conn()
        with conn:
            step = conn.execute(
                "SELECT * FROM orchestration_step_run WHERE task_id=?", (task_id,)
            ).fetchone()
            if step is None:
                raise OrchestrationError("当前任务不是编排角色", 409)
            message = conn.execute(
                "SELECT * FROM orchestration_message WHERE id=?", (message_id,)
            ).fetchone()
            if message is None or message["run_id"] != step["run_id"]:
                raise OrchestrationError("消息不存在", 404)
            if message["to_position"] != step["position"]:
                raise OrchestrationError("只能确认发给自己的消息", 403)
            run = conn.execute(
                "SELECT * FROM orchestration_run WHERE id=?", (step["run_id"],)
            ).fetchone()
            if run is None or run["status"] in TERMINAL_RUN_STATUSES:
                raise OrchestrationError("编排已结束，不能确认消息", 409)
            changed = conn.execute(
                "UPDATE orchestration_message SET acknowledged_at=COALESCE(acknowledged_at,?) "
                "WHERE id=? AND acknowledged_at IS NULL",
                (now, message_id),
            ).rowcount
            if (
                changed and message["kind"] in {"handoff", "rework"}
                and run["current_position"] == step["position"]
            ):
                conn.execute(
                    "UPDATE orchestration_step_run SET status='running',updated_at=?,"
                    "started_at=COALESCE(started_at,?) WHERE id=? AND status IN ('queued','waiting_input')",
                    (now, now, step["id"]),
                )
                conn.execute(
                    "UPDATE orchestration_run SET status='running',updated_at=? WHERE id=? "
                    "AND status NOT IN ('done','failed','cancelled','interrupted')",
                    (now, step["run_id"]),
                )
    events.emit("orchestration.message", {
        "run_id": step["run_id"], "message_id": message_id,
        "acknowledged": True, "newly_acknowledged": bool(changed),
    })
    return {
        "ok": True, "message_id": message_id, "acknowledged": True,
        "newly_acknowledged": bool(changed),
    }


def _revive_role(run_id: int, task_id: int) -> None:
    """目标角色掉线：重新排队拉起它；起来后补投滞留消息。"""
    row = db.query_one("SELECT status FROM task WHERE id=? AND deleted=0", (task_id,))
    if row is None:
        return
    step = db.query_one("SELECT status FROM orchestration_step_run WHERE task_id=?", (task_id,))
    if step is not None and step["status"] in {"failed", "cancelled", "interrupted"}:
        # 人为叫停或失败的角色不自动复活；已完成角色仍可被点名回答后续问题。
        return
    if not scheduler.role_online(task_id):
        db.execute(
            "UPDATE task SET status='queued',ended_at=NULL,exit_code=NULL,resume=CASE "
            "WHEN session_uid IS NULL THEN 0 ELSE 1 END WHERE id=?",
            (task_id,),
        )
        if step is not None and step["status"] == "offline":
            db.execute(
                "UPDATE orchestration_step_run SET status='queued',updated_at=? WHERE task_id=?",
                (time.time(), task_id),
            )
    if db.query_one("SELECT status FROM task WHERE id=?", (task_id,))["status"] == "queued":
        scheduler.start_orchestration_role(task_id)
    deliver_pending(run_id)


def deliver_pending(run_id: int) -> int:
    """把滞留消息补投给已经在线的角色。返回投递成功的条数。"""
    rows = db.query(
        "SELECT m.id, m.to_position, m.body, s.task_id FROM orchestration_message m "
        "JOIN orchestration_step_run s ON s.run_id=m.run_id AND s.position=m.to_position "
        "WHERE m.run_id=? AND (m.delivered_at IS NULL OR "
        "(m.acknowledged_at IS NULL AND m.delivery_attempts=0)) ORDER BY m.id",
        (run_id,),
    )
    delivered = 0
    now = time.time()
    for row in rows:
        if row["task_id"] is None or not scheduler.role_online(row["task_id"]):
            continue
        if scheduler.deliver_message(row["task_id"], row["body"]):
            _record_delivery(row["id"], now)
            delivered += 1
    return delivered


def _pingpong_length(run_id: int, a: int, b: int) -> int:
    """这对角色最近连续往返了几轮：只数结尾这一段 a↔b 的连续消息。"""
    rows = db.query(
        "SELECT from_position, to_position FROM orchestration_message "
        "WHERE run_id=? AND kind IN ('ask','answer') ORDER BY id DESC LIMIT ?",
        (run_id, MAX_PINGPONG + 2),
    )
    pair = {a, b}
    count = 0
    for row in rows:
        if {row["from_position"], row["to_position"]} != pair:
            break
        count += 1
    return count


def send_message(task_id: int, to_position: int, body: str, kind: str = "ask") -> dict:
    """角色之间点名发消息。只在同一个 run 内流转，且受上限与乒乓熔断约束。"""
    if kind not in MESSAGE_KINDS:
        raise OrchestrationError(f"不支持的消息类型: {kind}")
    text = (body or "").strip()
    if not text:
        raise OrchestrationError("消息内容不能为空")
    if len(text) > MAX_MESSAGE_CHARS:
        raise OrchestrationError(f"单条消息过长（上限 {MAX_MESSAGE_CHARS} 字）", 413)
    step = _step_for_task(task_id)
    if step is None:
        raise OrchestrationError("目标任务不是编排角色", 409)
    run = db.query_one("SELECT * FROM orchestration_run WHERE id=?", (step["run_id"],))
    if run is None or run["status"] in TERMINAL_RUN_STATUSES:
        raise OrchestrationError("编排已结束，不能再发消息", 409)
    if to_position == step["position"]:
        raise OrchestrationError("不能给自己发消息")
    target = db.query_one(
        "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=?",
        (step["run_id"], to_position),
    )
    if target is None:
        raise OrchestrationError(f"班组里没有第 {to_position} 位角色", 404)
    total = db.query_one(
        "SELECT COUNT(*) AS n FROM orchestration_message WHERE run_id=?", (step["run_id"],)
    )["n"]
    if total >= MAX_MESSAGES_PER_RUN:
        raise OrchestrationError(
            f"本次编排消息数已达上限（{MAX_MESSAGES_PER_RUN} 条），请直接推进或回报", 429,
        )
    if _pingpong_length(step["run_id"], step["position"], to_position) >= MAX_PINGPONG:
        # 两个角色来回踢皮球是最典型的失控形态：熔断并交人工，而不是让它们继续烧
        db.execute(
            "UPDATE orchestration_run SET status='waiting_input',updated_at=? WHERE id=?",
            (time.time(), step["run_id"]),
        )
        events.emit("orchestration.run", {"run_id": step["run_id"], "status": "waiting_input"})
        raise OrchestrationError(
            f"你与第 {to_position} 位已连续往返 {MAX_PINGPONG} 次，编排已暂停等用户裁决", 429,
        )
    now = time.time()
    content = (
        f"来自第 {step['position']} 位 · {step['name']}：\n{text}\n"
        f"（如需回复，用发消息功能回给第 {step['position']} 位；不要替对方做决定）"
    )
    with db._lock:
        conn = db.get_conn()
        with conn:
            message_id = _queue_message(
                conn, step["run_id"], step["position"], to_position, kind, content, now,
            )
            envelope = _message_body(conn, message_id)
    delivered = False
    if target["task_id"] is not None:
        delivered = scheduler.deliver_message(target["task_id"], envelope)
        if delivered:
            _record_delivery(message_id, now)
        else:
            _revive_role(step["run_id"], target["task_id"])
    events.emit("orchestration.message", {
        "run_id": step["run_id"], "from_position": step["position"],
        "to_position": to_position, "kind": kind, "delivered": delivered,
    })
    return {"ok": True, "id": message_id, "delivered": delivered}


def list_messages(run_id: int, limit: int = 200) -> list[dict]:
    return [dict(row) for row in db.query(
        "SELECT * FROM orchestration_message WHERE run_id=? ORDER BY id LIMIT ?",
        (run_id, max(1, min(limit, MAX_MESSAGES_PER_RUN))),
    )]


def release_run_sessions(run_id: int) -> None:
    """收口：回收全班组的常驻会话，不留孤儿进程。

    每个角色都是绕过并发槽起的独立进程，run 结束时没人回收它们就会一直挂着——
    比旧的串行编排更糟，因为常驻的是 N 个而不是 1 个。
    """
    for row in db.query(
        "SELECT task_id FROM orchestration_step_run WHERE run_id=? AND task_id IS NOT NULL",
        (run_id,),
    ):
        task_id = row["task_id"]
        task = db.query_one("SELECT status FROM task WHERE id=?", (task_id,))
        if task is None or task["status"] in {"done", "failed", "cancelled"}:
            continue
        try:
            scheduler.finish_subtask(task_id)  # 已出结论落 done，其余只收进程
        except Exception:
            continue


def cancel_run(run_id: int) -> dict:
    run = get_run(run_id)
    if run["status"] in TERMINAL_RUN_STATUSES:
        return run
    now = time.time()
    db.execute(
        "UPDATE orchestration_run SET status='cancelled',updated_at=?,ended_at=? WHERE id=?",
        (now, now, run_id),
    )
    # 取消整个班组：每个角色都是独立进程，只取消持棒那个会留下一地待命的孤儿
    for item in run["steps"]:
        if item["status"] not in TERMINAL_STEP_STATUSES:
            db.execute(
                "UPDATE orchestration_step_run SET status='cancelled',result='cancelled',"
                "updated_at=?,ended_at=? WHERE id=?",
                (now, now, item["id"]),
            )
        if item["task_id"] is not None:
            scheduler.cancel(item["task_id"])
    events.emit("orchestration.run", {"run_id": run_id, "status": "cancelled"})
    return get_run(run_id)


def handle_task_cancelled(task_id: int) -> None:
    step = _step_for_task(task_id)
    if step is None:
        return
    run = db.query_one("SELECT * FROM orchestration_run WHERE id=?", (step["run_id"],))
    if run is None or run["status"] in TERMINAL_RUN_STATUSES:
        return
    now = time.time()
    db.execute(
        "UPDATE orchestration_step_run SET status='cancelled',result='cancelled',updated_at=?,ended_at=? WHERE id=?",
        (now, now, step["id"]),
    )
    if run["current_position"] != step["position"]:
        # 待命角色被单独取消：班组少一个人，但流程没断，不该连坐整个编排。
        # 它掉线期间发给它的消息会滞留，接棒时由 _revive_role 重新拉起补投。
        events.emit("orchestration.run", {"run_id": step["run_id"], "status": run["status"]})
        return
    db.execute(
        "UPDATE orchestration_run SET status='cancelled',updated_at=?,ended_at=? WHERE id=?",
        (now, now, step["run_id"]),
    )
    events.emit("orchestration.run", {"run_id": step["run_id"], "status": "cancelled"})
    release_run_sessions(step["run_id"])


def handle_task_exit(task_id: int, exit_code: int) -> None:
    """步骤进程退出但没有权威回报时自动恢复一次，耗尽后转人工。"""
    step = _step_for_task(task_id)
    if step is None or step["status"] in TERMINAL_STEP_STATUSES:
        return
    task = db.query_one("SELECT report_result FROM task WHERE id=?", (task_id,))
    if task is not None and task["report_result"] == "needs_input":
        # 角色已经明确把问题交给用户；会话退出不能把人工等待误当成漏回报并自动重启。
        return
    run = db.query_one("SELECT * FROM orchestration_run WHERE id=?", (step["run_id"],))
    if run is None or run["status"] in TERMINAL_RUN_STATUSES:
        return
    if run["current_position"] != step["position"]:
        # 待命角色的进程自己退了（CLI 自身退出/被清理）：这不是流程失败。
        # 标成掉线，等它被点名时 _revive_role 重新拉起并补投滞留消息。
        db.execute(
            "UPDATE orchestration_step_run SET status='offline',updated_at=? WHERE id=?",
            (time.time(), step["id"]),
        )
        events.emit("orchestration.run", {"run_id": step["run_id"], "status": run["status"]})
        return
    if run["status"] == "waiting_input":
        return
    now = time.time()
    if exit_code == 0:
        if int(step["recovery_count"] or 0) < MAX_ROLE_RECOVERIES:
            db.execute(
                "UPDATE orchestration_step_run SET status='running',recovery_count=recovery_count+1,"
                "nudge_count=0,last_nudged_at=NULL,updated_at=? WHERE id=?",
                (now, step["id"]),
            )
            if scheduler.restart_orchestration_role(task_id):
                scheduler.deliver_message(
                    task_id,
                    "【Bosun 自动恢复】上一会话退出但没有提交权威回报。"
                    "请继续当前环节，完成后提交阶段产物并回报；需要用户决定时回报 needs_input。",
                )
                events.emit("orchestration.run", {
                    "run_id": step["run_id"], "status": "running", "recovered": True,
                })
                return
        _pause_for_attention(
            step["run_id"], step["id"], "当前角色退出且自动恢复失败，请人工检查", now,
        )
        return
    summary = f"CLI 异常退出（exit {exit_code}），未提交阶段产物"
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
    release_run_sessions(step["run_id"])  # 持棒者没了，剩下的待命角色不能继续挂着


def _pause_for_attention(run_id: int, step_id: int, summary: str, now: float) -> None:
    with db._lock:
        conn = db.get_conn()
        with conn:
            conn.execute(
                "UPDATE orchestration_run SET status='waiting_input',updated_at=? "
                "WHERE id=? AND status NOT IN ('done','failed','cancelled','interrupted')",
                (now, run_id),
            )
            conn.execute(
                "UPDATE orchestration_step_run SET status='waiting_input',summary=?,updated_at=? "
                "WHERE id=? AND status NOT IN ('done','failed','cancelled','interrupted')",
                (summary[:2000], now, step_id),
            )
    events.emit("orchestration.run", {
        "run_id": run_id, "status": "waiting_input", "summary": summary,
    })


def _message_is_still_pending(message) -> bool:
    row = db.query_one(
        "SELECT m.acknowledged_at,m.delivery_attempts,m.recovery_count,r.status AS run_status "
        "FROM orchestration_message m JOIN orchestration_run r ON r.id=m.run_id WHERE m.id=?",
        (message["id"],),
    )
    return bool(
        row and row["acknowledged_at"] is None
        and row["run_status"] in {"running", "queued"}
        and int(row["delivery_attempts"] or 0) == int(message["delivery_attempts"] or 0)
        and int(row["recovery_count"] or 0) == int(message["recovery_count"] or 0)
    )


def _restart_for_unacknowledged_message(message) -> bool | None:
    """原子认领一次消息恢复；None 表示快照已过期，不应再动目标会话。"""
    with db._lock:
        conn = db.get_conn()
        current = conn.execute(
            "SELECT m.acknowledged_at,m.delivery_attempts,m.recovery_count,r.status AS run_status,"
            "s.status AS step_status FROM orchestration_message m "
            "JOIN orchestration_run r ON r.id=m.run_id "
            "JOIN orchestration_step_run s ON s.run_id=m.run_id AND s.position=m.to_position "
            "WHERE m.id=?",
            (message["id"],),
        ).fetchone()
        if (
            current is None or current["acknowledged_at"] is not None
            or current["run_status"] not in {"running", "queued"}
            or current["step_status"] in TERMINAL_STEP_STATUSES
            or int(current["delivery_attempts"] or 0) != int(message["delivery_attempts"] or 0)
            or int(current["recovery_count"] or 0) != int(message["recovery_count"] or 0)
        ):
            return None
        conn.execute(
            "UPDATE orchestration_message SET recovery_count=recovery_count+1 WHERE id=?",
            (message["id"],),
        )
        conn.commit()
        # ACK 与回报会在同一把 DB 锁后等待；恢复动作先完成，避免检查后被迟到 ACK 穿透。
        return scheduler.restart_orchestration_role(message["task_id"])


def _pause_for_unacknowledged_message(message, summary: str, now: float) -> bool:
    with db._lock:
        conn = db.get_conn()
        with conn:
            current = conn.execute(
                "SELECT m.acknowledged_at,m.delivery_attempts,m.recovery_count,r.status AS run_status "
                "FROM orchestration_message m JOIN orchestration_run r ON r.id=m.run_id WHERE m.id=?",
                (message["id"],),
            ).fetchone()
            if (
                current is None or current["acknowledged_at"] is not None
                or current["run_status"] not in {"running", "queued"}
                or int(current["delivery_attempts"] or 0) != int(message["delivery_attempts"] or 0)
                or int(current["recovery_count"] or 0) != int(message["recovery_count"] or 0)
            ):
                return False
            conn.execute(
                "UPDATE orchestration_run SET status='waiting_input',updated_at=? WHERE id=?",
                (now, message["run_id"]),
            )
            conn.execute(
                "UPDATE orchestration_step_run SET status='waiting_input',summary=?,updated_at=? "
                "WHERE id=? AND status NOT IN ('done','failed','cancelled','interrupted')",
                (summary[:2000], now, message["step_id"]),
            )
    events.emit("orchestration.run", {
        "run_id": message["run_id"], "status": "waiting_input", "summary": summary,
    })
    return True


def _retry_unacknowledged_messages(now: float) -> int:
    rows = db.query(
        "SELECT m.*,s.id AS step_id,s.task_id FROM orchestration_message m "
        "JOIN orchestration_run r ON r.id=m.run_id "
        "JOIN orchestration_step_run s ON s.run_id=m.run_id AND s.position=m.to_position "
        "WHERE m.kind IN ('handoff','rework','ask','answer') AND m.acknowledged_at IS NULL "
        "AND r.status IN ('running','queued') ORDER BY m.id"
    )
    changed = 0
    for message in rows:
        if not _message_is_still_pending(message):
            continue
        last = message["last_delivered_at"]
        if last is not None and now - float(last) <= MESSAGE_ACK_TIMEOUT:
            continue
        task_id = message["task_id"]
        if task_id is None:
            _pause_for_attention(
                message["run_id"], message["step_id"], "消息目标任务不存在，自动交接已暂停", now,
            )
            changed += 1
            continue
        attempts = int(message["delivery_attempts"] or 0)
        recoveries = int(message["recovery_count"] or 0)
        if attempts < MAX_DELIVERY_ATTEMPTS - 1:
            if scheduler.deliver_message(task_id, message["body"]):
                _record_delivery(message["id"], now)
            else:
                _record_failed_delivery(message["id"], now)
                _revive_role(message["run_id"], task_id)
            changed += 1
            continue
        if recoveries < MAX_MESSAGE_RECOVERIES:
            restarted = _restart_for_unacknowledged_message(message)
            if restarted is None:
                continue
            if restarted and scheduler.deliver_message(task_id, message["body"]):
                _record_delivery(message["id"], now)
            changed += 1
            continue
        if _pause_for_unacknowledged_message(
            message, f"消息 #{message['id']} 多次投递仍未确认，请人工检查目标角色", now,
        ):
            changed += 1
    return changed


def _current_role_is_still_unreported(step) -> bool:
    row = db.query_one(
        "SELECT r.status AS run_status,r.current_position,s.status,t.status AS task_status,"
        "t.report_result,s.nudge_count,s.recovery_count,s.last_nudged_at "
        "FROM orchestration_step_run s JOIN orchestration_run r ON r.id=s.run_id "
        "JOIN task t ON t.id=s.task_id WHERE s.id=?",
        (step["id"],),
    )
    return bool(
        row and row["run_status"] == "running"
        and row["current_position"] == step["position"]
        and row["status"] in {"queued", "running", "waiting_input"}
        and row["task_status"] in {"waiting_input", "interrupted"}
        and not row["report_result"]
        and int(row["nudge_count"] or 0) == int(step["nudge_count"] or 0)
        and int(row["recovery_count"] or 0) == int(step["recovery_count"] or 0)
        and row["last_nudged_at"] == step["last_nudged_at"]
    )


def _restart_current_role_if_unreported(step, now: float) -> bool | None:
    """原子认领当前角色恢复；None 表示回报或接力棒已经改变。"""
    with db._lock:
        conn = db.get_conn()
        current = conn.execute(
            "SELECT r.status AS run_status,r.current_position,s.status,t.status AS task_status,"
            "t.report_result,s.recovery_count FROM orchestration_step_run s "
            "JOIN orchestration_run r ON r.id=s.run_id JOIN task t ON t.id=s.task_id WHERE s.id=?",
            (step["id"],),
        ).fetchone()
        if (
            current is None or current["run_status"] != "running"
            or current["current_position"] != step["position"]
            or current["status"] not in {"queued", "running", "waiting_input"}
            or current["task_status"] not in {"waiting_input", "interrupted"}
            or current["report_result"]
            or int(current["recovery_count"] or 0) != int(step["recovery_count"] or 0)
        ):
            return None
        conn.execute(
            "UPDATE orchestration_step_run SET recovery_count=recovery_count+1,nudge_count=0,"
            "last_nudged_at=NULL,status='running',updated_at=? WHERE id=?",
            (now, step["id"]),
        )
        conn.commit()
        return scheduler.restart_orchestration_role(step["task_id"])


def _record_current_role_nudge(step, now: float) -> bool:
    changed = db.execute_rowcount(
        "UPDATE orchestration_step_run SET nudge_count=nudge_count+1,last_nudged_at=?,"
        "status='running',updated_at=? WHERE id=? AND nudge_count=? AND recovery_count=? "
        "AND EXISTS (SELECT 1 FROM orchestration_run r WHERE r.id=orchestration_step_run.run_id "
        "AND r.status='running' AND r.current_position=orchestration_step_run.position) "
        "AND EXISTS (SELECT 1 FROM task t WHERE t.id=orchestration_step_run.task_id "
        "AND t.status IN ('waiting_input','interrupted') AND t.report_result IS NULL)",
        (
            now, now, step["id"], int(step["nudge_count"] or 0),
            int(step["recovery_count"] or 0),
        ),
    )
    return changed == 1


def _pause_current_role_if_unreported(step, summary: str, now: float) -> bool:
    with db._lock:
        conn = db.get_conn()
        with conn:
            current = conn.execute(
                "SELECT r.status AS run_status,r.current_position,s.status,t.status AS task_status,"
                "t.report_result FROM orchestration_step_run s "
                "JOIN orchestration_run r ON r.id=s.run_id JOIN task t ON t.id=s.task_id "
                "WHERE s.id=?",
                (step["id"],),
            ).fetchone()
            if (
                current is None or current["run_status"] != "running"
                or current["current_position"] != step["position"]
                or current["status"] not in {"queued", "running", "waiting_input"}
                or current["task_status"] not in {"waiting_input", "interrupted"}
                or current["report_result"]
            ):
                return False
            conn.execute(
                "UPDATE orchestration_run SET status='waiting_input',updated_at=? WHERE id=?",
                (now, step["run_id"]),
            )
            conn.execute(
                "UPDATE orchestration_step_run SET status='waiting_input',summary=?,updated_at=? "
                "WHERE id=?",
                (summary[:2000], now, step["id"]),
            )
    events.emit("orchestration.run", {
        "run_id": step["run_id"], "status": "waiting_input", "summary": summary,
    })
    return True


def _supervise_current_roles(now: float) -> int:
    rows = db.query(
        "SELECT r.id AS run_id,s.*,t.status AS task_status,t.report_result "
        "FROM orchestration_run r "
        "JOIN orchestration_step_run s ON s.run_id=r.id AND s.position=r.current_position "
        "JOIN task t ON t.id=s.task_id "
        "WHERE r.status='running' AND s.status IN ('queued','running','waiting_input')"
    )
    changed = 0
    for step in rows:
        if step["report_result"] or not _current_role_is_still_unreported(step):
            continue
        task_status = step["task_status"]
        if task_status not in {"waiting_input", "interrupted"}:
            continue
        waiting_kind = scheduler.get_waiting_kind(step["task_id"])
        if task_status == "waiting_input" and waiting_kind in {"choice", "input", "permission"}:
            if _pause_current_role_if_unreported(
                step, "当前角色需要用户输入或安全确认", now,
            ):
                changed += 1
            continue
        last_nudged = step["last_nudged_at"]
        if int(step["nudge_count"] or 0) < MAX_ROLE_NUDGES:
            message = (
                "【Bosun 催办】你当前持有接力棒，但本轮尚未收到权威回报。"
                "请继续完成当前工作；完成后提交阶段产物并回报 done。"
                "若确实缺少用户信息则回报 needs_input，不要静默停住。"
            )
            if scheduler.deliver_message(step["task_id"], message):
                if _record_current_role_nudge(step, now):
                    changed += 1
            elif int(step["recovery_count"] or 0) < MAX_ROLE_RECOVERIES:
                restarted = _restart_current_role_if_unreported(step, now)
                if restarted is None:
                    continue
                if restarted:
                    scheduler.deliver_message(
                        step["task_id"],
                        "【Bosun 自动恢复】上一会话无法接收催办。请继续本环工作并按协议回报。",
                    )
                else:
                    _pause_for_attention(
                        step["run_id"], step["id"], "当前角色掉线且自动恢复失败，请人工检查", now,
                    )
                changed += 1
            else:
                if _pause_current_role_if_unreported(
                    step, "当前角色无法接收催办，请人工检查", now,
                ):
                    changed += 1
            continue
        if last_nudged is not None and now - float(last_nudged) <= MESSAGE_ACK_TIMEOUT:
            continue
        if int(step["recovery_count"] or 0) < MAX_ROLE_RECOVERIES:
            restarted = _restart_current_role_if_unreported(step, now)
            if restarted is None:
                continue
            if restarted:
                scheduler.deliver_message(
                    step["task_id"],
                    "【Bosun 自动恢复】上一会话停滞且未回报。请从当前上下文继续本环工作，"
                    "完成后提交阶段产物并回报；需要用户决定时回报 needs_input。",
                )
            changed += 1
            continue
        if _pause_current_role_if_unreported(
            step, "当前角色催办并自动恢复后仍未回报，请人工检查", now,
        ):
            changed += 1
    return changed


def sweep_reliable_communications(now: float | None = None) -> int:
    """监督可靠消息与当前接力棒；每项动作都有硬上限，耗尽后转人工。"""
    current = time.time() if now is None else now
    return _retry_unacknowledged_messages(current) + _supervise_current_roles(current)


def sweep_timeouts() -> int:
    """整轮墙钟超时的班组收口为 interrupted，产物全部保留。

    全员常驻意味着「卡住」不再表现为进程退出——大家都活得好好的，只是没人推进。
    没有这道闸，一个死循环的班组可以挂到天荒地老还占着 N 个进程。
    """
    try:
        limit_hours = float(db.get_setting("orchestration_timeout_hours", 6) or 0)
    except (TypeError, ValueError):
        limit_hours = 6.0
    if limit_hours <= 0:
        return 0
    cutoff = time.time() - limit_hours * 3600
    swept = 0
    for run in db.query(
        "SELECT id FROM orchestration_run WHERE status IN ('queued','running','waiting_input') "
        "AND started_at IS NOT NULL AND started_at < ?",
        (cutoff,),
    ):
        now = time.time()
        db.execute(
            "UPDATE orchestration_run SET status='interrupted',updated_at=?,ended_at=? WHERE id=?",
            (now, now, run["id"]),
        )
        db.execute(
            "UPDATE orchestration_step_run SET status='interrupted',updated_at=?,ended_at=? "
            "WHERE run_id=? AND status NOT IN ('done','failed','cancelled','interrupted')",
            (now, now, run["id"]),
        )
        release_run_sessions(run["id"])
        events.emit("orchestration.run", {"run_id": run["id"], "status": "interrupted"})
        swept += 1
    return swept


def resume_run(run_id: int) -> dict:
    """重新拉起掉线的角色（重启后由用户确认再拉，避免一开机就重开一整班烧钱）。"""
    run = db.query_one("SELECT * FROM orchestration_run WHERE id=?", (run_id,))
    if run is None:
        raise OrchestrationError("编排运行不存在", 404)
    if run["status"] in TERMINAL_RUN_STATUSES:
        raise OrchestrationError("编排已结束，不能恢复", 409)
    now = time.time()
    steps = db.query(
        "SELECT * FROM orchestration_step_run WHERE run_id=? ORDER BY position", (run_id,)
    )
    online = {
        step["task_id"]: scheduler.role_online(step["task_id"])
        for step in steps if step["task_id"] is not None
    }
    task_ids: list[int] = []
    current_task_id: int | None = None
    current_needs_input = False
    with db._lock:
        conn = db.get_conn()
        with conn:
            run = conn.execute(
                "SELECT * FROM orchestration_run WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise OrchestrationError("编排运行不存在", 404)
            if run["status"] in TERMINAL_RUN_STATUSES:
                raise OrchestrationError("编排已结束，不能恢复", 409)
            steps = conn.execute(
                "SELECT * FROM orchestration_step_run WHERE run_id=? ORDER BY position", (run_id,)
            ).fetchall()
            for step in steps:
                if step["status"] in TERMINAL_STEP_STATUSES or step["task_id"] is None:
                    continue
                is_current = step["position"] == run["current_position"]
                if is_current:
                    current_task_id = step["task_id"]
                    current_needs_input = step["result"] == "needs_input"
                if not online.get(step["task_id"], False):
                    conn.execute(
                        "UPDATE task SET status='queued',ended_at=NULL,exit_code=NULL,resume=CASE "
                        "WHEN session_uid IS NULL THEN 0 ELSE 1 END WHERE id=?",
                        (step["task_id"],),
                    )
                    task_ids.append(step["task_id"])
                elif is_current and not current_needs_input:
                    conn.execute(
                        "UPDATE task SET status='running',waiting_since=NULL WHERE id=? "
                        "AND report_result IS NULL",
                        (step["task_id"],),
                    )
                conn.execute(
                    "UPDATE orchestration_step_run SET status=?,nudge_count=0,recovery_count=0,"
                    "last_nudged_at=NULL,updated_at=? WHERE id=?",
                    (
                        "waiting_input" if is_current and current_needs_input
                        else "running" if is_current else "queued",
                        now, step["id"],
                    ),
                )
            # 人工恢复是新的投递轮次：耗尽的旧计数必须重新武装，且未确认消息要真正重投。
            conn.execute(
                "UPDATE orchestration_message SET last_delivered_at=NULL,"
                "delivery_attempts=0,recovery_count=0 WHERE run_id=? AND acknowledged_at IS NULL "
                "AND kind IN ('handoff','rework','ask','answer')",
                (run_id,),
            )
            resumed_status = "waiting_input" if current_needs_input else "running"
            conn.execute(
                "UPDATE orchestration_run SET status=?,updated_at=? WHERE id=? "
                "AND status NOT IN ('done','failed','cancelled','interrupted')",
                (resumed_status, now, run_id),
            )
    _dispatch_roles(task_ids)
    deliver_pending(run_id)  # 掉线或熔断期间滞留的交棒/返工/提问，一回来就补投
    if current_task_id is not None and not current_needs_input:
        scheduler.deliver_message(
            current_task_id,
            "【Bosun 人工恢复】恢复计数已经重新武装。请继续当前环节并按协议回报；"
            "需要用户决定时回报 needs_input。",
        )
    events.emit("orchestration.run", {"run_id": run_id, "status": resumed_status})
    return get_run(run_id)


def reconcile_on_startup() -> int:
    """后端重启后对账：进程都随后端死了，先如实标掉线，不自动重开。

    持棒者掉线 = 流程断在这里，转 waiting_input 交用户决定（`resume_run` 重拉，
    或取消）。待命角色掉线只标 offline，等它被点名时按需重拉。
    """
    changed = 0
    now = time.time()
    with db._lock:
        conn = db.get_conn()
        with conn:
            runs = conn.execute(
                "SELECT * FROM orchestration_run WHERE status IN ('queued','running','waiting_input')"
            ).fetchall()
            for run in runs:
                snapshot = json.loads(run["definition_snapshot"])
                steps = conn.execute(
                    "SELECT * FROM orchestration_step_run WHERE run_id=? ORDER BY position",
                    (run["id"],),
                ).fetchall()
                if not steps:
                    _create_all_roles(conn, run, snapshot, now)
                    changed += 1
                    continue
                baton_down = False
                for step in steps:
                    if step["status"] in TERMINAL_STEP_STATUSES:
                        continue
                    task = conn.execute(
                        "SELECT status FROM task WHERE id=?", (step["task_id"],)
                    ).fetchone()
                    alive = task is not None and task["status"] in {"running", "waiting_input", "queued"}
                    if alive:
                        continue
                    conn.execute(
                        "UPDATE orchestration_step_run SET status='offline',updated_at=? WHERE id=?",
                        (now, step["id"]),
                    )
                    changed += 1
                    if step["position"] == run["current_position"]:
                        baton_down = True
                if baton_down:
                    conn.execute(
                        "UPDATE orchestration_run SET status='waiting_input',updated_at=? WHERE id=?",
                        (now, run["id"]),
                    )
    return changed
