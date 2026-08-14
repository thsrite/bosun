"""调度器 + 会话管理器。

- 持有 task_id -> PtySession
- 后台循环：有空槽时从 queued 里按 priority 挑最高的启动
- running / waiting_input 都占槽；done/failed/cancelled 释放
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from . import agent_skills, browser_computer, db, engine_settings, events, sessions
from .config import DATA_DIR, LOG_DIR
from .engines import build_argv, build_resume_argv, uses_stdin_prompt, with_report_directive
from .pty_session import PtySession, remove_terminal_log_files

_sessions: dict[int, object] = {}
_live_tokens = sessions.LiveTokenCounter()  # 增量统计运行中会话用量, 避免每 15s 重解析整份 transcript
_loop: asyncio.AbstractEventLoop | None = None
_tick_lock = threading.RLock()  # tick 会被请求线程与事件循环线程调用, 串行化避免重复启动
_session_capture_lock = threading.Lock()  # 并发捕获时原子认领 rollout，避免相同任务也串号
_PID = os.getpid()
_OWNER_TTL = 8.0  # 调度心跳过期秒数: 主进程每 2s 续约, 超过则允许别的进程接管
_scheduler_lock_file = None
_scheduler_lock_guard = threading.Lock()

_PAUSABLE_STATUSES = {
    "queued",
    "running",
    "waiting_input",
    "done",
    "failed",
    "cancelled",
    "interrupted",
}
_TERMINAL_STATUSES = {"done", "failed", "cancelled", "interrupted"}


class TaskTransitionError(ValueError):
    """任务状态不允许当前人工操作。"""


def _try_acquire_scheduler_file_lock(path: Path):
    """尝试持有跨进程排他锁；进程退出时由内核自动释放。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(path, "a+", encoding="utf-8")
    try:
        if sys.platform == "win32":
            # Windows 无 flock：锁文件头 1 字节，进程退出由系统释放，语义等价
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        lock_file.close()
        return None
    return lock_file


def _claim_process_lock() -> bool:
    global _scheduler_lock_file
    if _scheduler_lock_file is not None:
        return True
    with _scheduler_lock_guard:
        if _scheduler_lock_file is None:
            _scheduler_lock_file = _try_acquire_scheduler_file_lock(DATA_DIR / "scheduler.lock")
        return _scheduler_lock_file is not None


def _claim_scheduler() -> bool:
    """调度单实例锁(心跳 + CAS)：只有持锁进程跑 reconcile/tick，防多后端共享 DB 时互相误标中断。

    机制：setting['scheduler_owner'] = 'pid:ts'。本进程是主/无主/主心跳过期 → CAS 认领并续约，
    返回 True；否则(别的进程活着持锁) → False，本轮不调度。CAS 保证并发下至多一个进程认领成功。
    """
    # Uvicorn 会先执行应用 startup，再尝试绑定端口。没有文件锁时，一个最终绑定失败的
    # 临时后端也可能运行 reconcile，并把由另一后端持有的活会话误标为 interrupted。
    if not _claim_process_lock():
        return False

    now = time.time()
    val = db.get_setting("scheduler_owner")
    new = f"{_PID}:{now}"
    if val is None:  # 空位: 唯一键 INSERT OR IGNORE 防并发双插
        return db.execute_rowcount(
            "INSERT OR IGNORE INTO setting(key,value) VALUES('scheduler_owner',?)", (new,)
        ) == 1
    opid, ots = None, 0.0
    try:
        opid, s = str(val).rsplit(":", 1)
        ots = float(s)
    except ValueError:
        pass
    if opid == str(_PID) or (now - ots) > _OWNER_TTL:  # 续约自己 / 抢占过期主
        # CAS: 仅当 value 仍是我们读到的 val 才写入(否则说明别的进程已抢先)
        return db.execute_rowcount(
            "UPDATE setting SET value=? WHERE key='scheduler_owner' AND value=?", (new, val)
        ) == 1
    return False


def get_session(task_id: int) -> object | None:
    return _sessions.get(task_id)


def _max_concurrent() -> int:
    try:
        return int(db.get_setting("max_concurrent", 3))
    except (TypeError, ValueError):
        return 3


def _running_count() -> int:
    # 快照: 其它线程可能并发 pop(cancel/complete/delete/on_exit), 直接迭代会崩
    return sum(1 for s in list(_sessions.values()) if s.is_alive())


# ---- 会话状态回调（在事件循环线程执行） ----
def _on_status(task_id: int, status: str) -> None:
    if status in {"running", "waiting_input"}:
        if status == "waiting_input":
            # 同一轮等待可能先后被 PTY 提示识别和 agent 的收尾回报触达。
            # 保留首次进入等待的时间，让前端把它们视作同一条可补发通知。
            changed = db.execute_rowcount(
                "UPDATE task SET status=?, ended_at=NULL, exit_code=NULL, "
                "waiting_since=CASE "
                "WHEN status='waiting_input' AND waiting_since IS NOT NULL THEN waiting_since "
                "ELSE ? END WHERE id=? "
                "AND status IN ('queued','running','waiting_input','interrupted')",
                (status, time.time(), task_id),
            )
        else:
            changed = db.execute_rowcount(
                "UPDATE task SET status=?, ended_at=NULL, exit_code=NULL, waiting_since=NULL "
                "WHERE id=? AND status IN ('queued','running','waiting_input','interrupted')",
                (status, task_id),
            )
        if not changed:
            return
    else:
        db.execute("UPDATE task SET status=?, waiting_since=NULL WHERE id=?", (status, task_id))
    payload = {"task_id": task_id, "status": status}
    if status == "waiting_input":
        payload["waiting_kind"] = get_waiting_kind(task_id)
        row = db.query_one("SELECT waiting_since FROM task WHERE id=?", (task_id,))
        payload["waiting_since"] = row["waiting_since"] if row else None
    events.emit("task.status", payload)


def _on_exit(task_id: int, exit_code: int) -> None:
    status = "done" if exit_code == 0 else "failed"
    # 仅当仍处于活动态时才由进程退出决定终态，避免覆盖手动完成/取消
    db.execute(
        "UPDATE task SET status=?, exit_code=?, ended_at=?, waiting_since=NULL "
        "WHERE id=? AND status IN ('running','waiting_input')",
        (status, exit_code, time.time(), task_id),
    )
    _sessions.pop(task_id, None)
    from . import orchestrations
    orchestrations.handle_task_exit(task_id, exit_code)
    # 仅当确实由本次退出更新了状态(仍是活动态)才推送, 避免覆盖已 cancel/complete/delete
    cur = db.query_one("SELECT status FROM task WHERE id=?", (task_id,))
    if cur and cur["status"] == status:
        events.emit("task.status", {"task_id": task_id, "status": status, "exit_code": exit_code})
        threading.Thread(target=_finalize_tokens, args=(task_id, 3.0), daemon=True).start()
    # 父任务进程退出同样要级联取消活动子任务：cancel/delete 有级联，但「跑完就退」
    # 才是主流路径，漏掉它子任务就成了没人认领、也没人杀的孤儿进程。
    _cancel_active_children(task_id)
    # 空出槽位，立即尝试拉起下一个
    tick()


def _on_session(task_id: int, session_id: str) -> None:
    db.execute("UPDATE task SET session_uid=? WHERE id=?", (session_id, task_id))
    events.emit("task.session", {"task_id": task_id, "session_uid": session_id})


def _on_tokens(task_id: int, tokens: int) -> None:
    db.execute("UPDATE task SET tokens=? WHERE id=?", (tokens, task_id))
    events.emit("task.tokens", {"task_id": task_id, "tokens": tokens})


def _on_permission(task_id: int, info: dict | None) -> None:
    events.emit("task.permission", {"task_id": task_id, "permission": info})


def _capture_session(
    task_id: int,
    engine: str,
    cwd: str,
    prompt: str,
    before: set,
    since: float,
) -> None:
    """运行后轮询捕获引擎真实生成的会话 id(claude/codex/omp 都不支持事前钉 id)。

    引擎何时把 transcript 落盘并不受我们控制，首轮很慢时可能远超最初的 45s 快轮询窗口。
    所以任务还活着就继续以更低频率轮询，任务结束后再补几次，避免会话永远认领不到——
    那会连带让续跑、导出、历史和 token 结算全部失效。
    """
    fast_rounds = 30           # 前 45s 高频轮询，覆盖绝大多数情况
    tail_rounds = 4            # 进程退出后再补几次，等最后的落盘
    max_seconds = 2 * 60 * 60  # 兜底上限，防止任务挂死时线程常驻
    started = time.monotonic()
    rounds = 0
    after_exit = 0
    while True:
        time.sleep(1.5 if rounds < fast_rounds else 5.0)
        rounds += 1
        # 认领要在锁内完成：同项目并发任务可能在任何文件落盘前都完成了 snapshot，
        # 不排掉已被认领的 uid 就会两个任务共用同一个会话。
        with _session_capture_lock:
            claimed = {
                row["session_uid"]
                for row in db.query(
                    "SELECT session_uid FROM task WHERE id<>? AND session_uid IS NOT NULL",
                    (task_id,),
                )
            }
            if engine == "claude":
                uid = sessions.capture_claude_session(cwd, before, since, exclude_uids=claimed, prompt=prompt)
            elif engine == "omp":
                uid = sessions.capture_omp_session(cwd, before, since, exclude_uids=claimed, prompt=prompt)
            elif engine == "kimi":
                uid = sessions.capture_kimi_session(cwd, before, since, exclude_uids=claimed, prompt=prompt)
            else:
                uid = sessions.capture_codex_session(
                    before,
                    since,
                    cwd=cwd,
                    prompt=prompt,
                    exclude_uids=claimed,
                )
            changed = 0
            if uid:
                # 不再限定 status：进程已退出的任务同样需要补上会话 id，
                # started_at 已能保证这是本次运行而不是历史轮次。
                changed = db.execute_rowcount(
                    "UPDATE task SET session_uid=? WHERE id=? AND started_at=? AND session_uid IS NULL",
                    (uid, task_id, since),
                )
        if uid:
            if changed:
                events.emit("task.session", {"task_id": task_id, "session_uid": uid})
                # 任务可能在会话落盘前就结束了：那一轮 _finalize_tokens 因为还没有
                # session_uid 直接返回，用量会永远空着。认领晚于结算时补跑一次。
                row = db.query_one(
                    "SELECT status, tokens FROM task WHERE id=?", (task_id,)
                )
                if row and row["tokens"] is None and row["status"] not in (
                    "running", "waiting_input",
                ):
                    threading.Thread(
                        target=_finalize_tokens, args=(task_id, 0.5), daemon=True
                    ).start()
            return

        row = db.query_one("SELECT status, session_uid FROM task WHERE id=?", (task_id,))
        if row is None or row["session_uid"]:
            return  # 任务已删除，或会话已由别的途径(如 SDK 回调)补上
        if row["status"] not in ("running", "waiting_input"):
            after_exit += 1
            if after_exit >= tail_rounds:
                return
        if time.monotonic() - started > max_seconds:
            return


def _finalize_tokens(task_id: int, delay: float) -> None:
    """任务结束后等 transcript 落盘，解析 token 用量存库。"""
    time.sleep(delay)
    t = db.query_one(
        "SELECT engine, project_id, session_uid, started_at, ended_at FROM task WHERE id=?",
        (task_id,),
    )
    if t is None or not t["session_uid"]:
        return
    project = db.query_one("SELECT path FROM project WHERE id=?", (t["project_id"],))
    if project is None:
        return
    tokens = sessions.count_tokens(
        t["engine"],
        project["path"],
        t["session_uid"],
        since=t["started_at"],
        until=t["ended_at"],
    )
    if tokens is not None:
        db.execute("UPDATE task SET tokens=? WHERE id=?", (tokens, task_id))
        events.emit("task.tokens", {"task_id": task_id, "tokens": tokens})


def _start_task(row) -> None:
    assert _loop is not None
    project = db.query_one("SELECT * FROM project WHERE id=?", (row["project_id"],))
    if project is None:
        db.execute("UPDATE task SET status='failed' WHERE id=?", (row["id"],))
        return
    log_path = str(LOG_DIR / f"task-{row['id']}.log")
    engine, auto = row["engine"], bool(row["auto_approve"])
    orchestration_step = db.query_one(
        "SELECT model,reasoning_effort FROM orchestration_step_run WHERE task_id=?",
        (row["id"],),
    )
    artifact_required = orchestration_step is not None
    model_override = orchestration_step["model"] if orchestration_step else None
    reasoning_override = orchestration_step["reasoning_effort"] if orchestration_step else None
    session_uid = row["session_uid"]
    run_started_at = time.time()
    capture = None  # (before, since)

    if engine == "browser":
        session = browser_computer.BrowserSession(
            task_id=row["id"],
            prompt=row["prompt"],
            log_path=log_path,
            loop=_loop,
            on_status=_on_status,
            on_exit=_on_exit,
            on_tokens=_on_tokens,
            on_permission=_on_permission,
        )
        use_sdk = True
    # claude 首跑(非resume、无post_input) 默认走 SDK；设置可强制 CLI。
    else:
        use_sdk = engine_settings.should_use_claude_sdk(
            engine,
            resume=bool(row["resume"]),
            post_input=row["post_input"],
        )

    if engine == "browser":
        pass
    elif use_sdk:
        from .sdk_session import SdkSession

        session = SdkSession(
            task_id=row["id"],
            prompt=row["prompt"],
            cwd=project["path"],
            auto_approve=auto,
            log_path=log_path,
            loop=_loop,
            on_status=_on_status,
            on_exit=_on_exit,
            on_session=_on_session,
            on_tokens=_on_tokens,
            on_permission=_on_permission,
            artifact_required=artifact_required,
            model_override=model_override,
            reasoning_override=reasoning_override,
        )
    else:
        if row["resume"] and session_uid:
            argv = build_resume_argv(
                engine,
                session_uid,
                row["prompt"],
                auto,
                artifact_required=artifact_required,
                model_override=model_override,
                reasoning_override=reasoning_override,
            )
        else:  # 首跑：引擎自建会话(会落盘)，运行后捕获真实 session id
            argv = build_argv(
                engine,
                row["prompt"],
                auto,
                artifact_required=artifact_required,
                model_override=model_override,
                reasoning_override=reasoning_override,
            )
            if engine == "claude":
                before = sessions.snapshot_claude(project["path"])
            elif engine == "omp":
                before = sessions.snapshot_omp(project["path"])
            elif engine == "kimi":
                before = sessions.snapshot_kimi(project["path"])
            else:
                before = sessions.snapshot_codex()
            capture = (before, run_started_at)
        # kimi 交互模式不收位置参数 prompt：argv 不带 prompt，改由 PtySession 在
        # TUI 就绪后粘贴提交。Bosun 技能不会改写原始 prompt。
        initial_prompt = None
        if uses_stdin_prompt(engine) and (row["prompt"] or "").strip():
            initial_prompt = with_report_directive(
                row["prompt"], engine=engine, artifact_required=artifact_required
            )
        session = PtySession(
            task_id=row["id"],
            argv=argv,
            cwd=project["path"],
            log_path=log_path,
            loop=_loop,
            on_status=_on_status,
            on_exit=_on_exit,
            post_input=row["post_input"],
            initial_prompt=initial_prompt,
            task_engine=engine,
            artifact_required=artifact_required,
            report_nudge=agent_skills.report_nudge(engine, artifact_required),
        )
    _sessions[row["id"]] = session
    db.execute(
        "UPDATE task SET status='running', log_path=?, started_at=?, ended_at=NULL, "
        "exit_code=NULL, render_mode=?, waiting_since=NULL, "
        # 上一轮收尾回报的摘要属于上一轮：新一轮跑起来就清掉，
        # 免得待处理列表和通知里挂着过期结论。
        "report_result=NULL, report_summary=NULL WHERE id=?",
        (log_path, run_started_at, "chat" if use_sdk else "terminal", row["id"]),
    )
    try:
        session.start()
    except Exception as exc:  # 启动失败（如 claude 不在 PATH）
        _sessions.pop(row["id"], None)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"启动失败: {exc}\n")
        except OSError:
            pass
        db.execute(
            "UPDATE task SET status='failed', exit_code=127, ended_at=? WHERE id=?",
            (time.time(), row["id"]),
        )
        events.emit("task.status", {"task_id": row["id"], "status": "failed", "error": str(exc)})
        from . import orchestrations
        orchestrations.handle_task_exit(row["id"], 127)
        return
    events.emit("task.status", {"task_id": row["id"], "status": "running"})
    if capture is not None:
        before, since = capture
        # 认领 Codex 会话靠「首条用户消息==prompt」精确比对。当前 skills 路径不再
        # 改写 prompt；仍统一经同一函数，避免以后两条路径再次分叉。
        dispatched_prompt = with_report_directive(
            row["prompt"], engine=engine, artifact_required=artifact_required
        )
        threading.Thread(
            target=_capture_session,
            args=(row["id"], engine, project["path"], dispatched_prompt, before, since),
            daemon=True,
        ).start()


def tick() -> None:
    """尝试用空槽拉起 queued 任务（按优先级降序）。多线程调用, 加锁串行。"""
    with _tick_lock:
        free = _max_concurrent() - _running_count()
        if free <= 0:
            return
        rows = db.query(
            "SELECT * FROM task WHERE status='queued' AND deleted=0 ORDER BY priority DESC, created_at ASC LIMIT ?",
            (free,),
        )
        for row in rows:
            _start_task(row)


def start_subtask(task_id: int) -> None:
    """立即派发一个受控子任务，**绕过并发槽**（见 subtasks 模块的取舍说明）。

    刻意不走 tick()：tick 按空槽从 queued 里挑，而子任务的父任务正占着槽等它，
    排队会互锁。子任务的放大倍数由「每父任务子任务数上限」兜住，不由槽位兜。
    """
    row = db.query_one("SELECT * FROM task WHERE id=? AND deleted=0", (task_id,))
    if row is None or row["status"] != "queued":
        return
    with _tick_lock:  # 与 tick 串行，避免同一任务被并发启动两次
        if task_id in _sessions:
            return
        _start_task(row)


def finish_subtask(task_id: int) -> None:
    """子任务出结论后收掉它的进程——一次性语义，等价于 `codex exec` 跑完即退。

    不收就是主流路径漏进程：agent 按收尾约定回报后停在 waiting_input 等人核对，
    进程（连同它自己起的 MCP 子进程）常驻不退，还一直被 _running_count 算作占槽。
    父任务已经拿走结论，没人会再理它，也没有任何回收器管 waiting_input。

    已出结论的落 done 而不是 cancelled：结论是有效的，看板和统计不该显示成被取消。
    已是终态的（如 agent 回报 failed）只收进程，状态不动。
    """
    if task_id not in _sessions:
        return
    row = db.query_one("SELECT status FROM task WHERE id=?", (task_id,))
    if row is not None and row["status"] == "waiting_input":
        complete(task_id)  # graceful_stop 让引擎落盘会话，便于日后查阅/续跑
        return
    session = _sessions.pop(task_id, None)
    if session is not None:
        session.graceful_stop()
        threading.Thread(target=_finalize_tokens, args=(task_id, 4.0), daemon=True).start()
    tick()


def finish_orchestration_step(task_id: int) -> None:
    """编排步骤已经把完整 artifact 落库，收掉会话并固定任务终态。"""
    row = db.query_one("SELECT status, report_result FROM task WHERE id=?", (task_id,))
    if row is None:
        return
    if row["report_result"] == "done":
        complete(task_id)
        return
    session = _sessions.pop(task_id, None)
    if session is not None:
        session.graceful_stop()
        threading.Thread(target=_finalize_tokens, args=(task_id, 4.0), daemon=True).start()
    tick()


def send_subtask_reply(task_id: int, message: str) -> bool:
    """把父任务的回复作为新一轮用户消息投递给仍存活的子任务会话。"""
    session = _sessions.get(task_id)
    if session is None or not session.is_alive():
        return False
    submit = getattr(session, "submit_message", None)
    if submit is None:
        return False
    try:
        submit(message)
    except Exception:  # 会话可能恰在投递时退出；调用方会保留上一轮问题供重试
        return False
    return True


def _cancel_active_children(parent_id: int) -> None:
    """父任务终止时级联取消仍在跑的子任务。

    不做级联会留下杀不掉的孤儿：子任务是调度器派发的独立进程，父任务没了它还在跑
    （比现状还糟——agent 自己 spawn 的子进程通常随父进程一起死）。
    已进入终态的子任务保持原状，统计历史不被抹掉。
    """
    rows = db.query(
        "SELECT id FROM task WHERE parent_task_id=? AND deleted=0 "
        "AND status IN ('queued','running','waiting_input')",
        (parent_id,),
    )
    for row in rows:
        cancel(row["id"])


def respond_permission(task_id: int, allow: bool) -> bool:
    s = _sessions.get(task_id)
    if s is not None and hasattr(s, "respond_permission"):
        s.respond_permission(allow)
        return True
    return False


def get_permission(task_id: int) -> dict | None:
    s = _sessions.get(task_id)
    return getattr(s, "pending_permission", None) if s is not None else None


def get_waiting_kind(task_id: int) -> str | None:
    s = _sessions.get(task_id)
    if s is None:
        return None
    if getattr(s, "pending_permission", None) is not None:
        return "permission"
    kind = getattr(s, "waiting_kind", None)
    return kind if kind in {"choice", "input", "permission", "review"} else None


def get_session_cleared(task_id: int) -> bool:
    """当前执行器是否检测到会话被 /clear 冲掉(仅 PTY 会话有此信号)。"""
    s = _sessions.get(task_id)
    return bool(getattr(s, "session_cleared", False))


def cancel(task_id: int) -> bool:
    session = _sessions.get(task_id)
    if session is not None:
        session.terminate()
        _sessions.pop(task_id, None)
    db.execute(
        "UPDATE task SET status='cancelled', ended_at=? WHERE id=? AND status IN "
        "('queued','running','waiting_input')",
        (time.time(), task_id),
    )
    events.emit("task.status", {"task_id": task_id, "status": "cancelled"})
    _cancel_active_children(task_id)
    from . import orchestrations
    orchestrations.handle_task_cancelled(task_id)
    tick()
    return True


def complete(task_id: int) -> bool:
    """手动标记完成：运行中的先停进程，中断/等待执行的任务也允许人工确认完成。"""
    orchestration_step = db.query_one(
        "SELECT 1 FROM orchestration_step_run WHERE task_id=?",
        (task_id,),
    )
    task = db.query_one("SELECT report_result FROM task WHERE id=?", (task_id,))
    if orchestration_step is not None and (task is None or task["report_result"] != "done"):
        raise TaskTransitionError("编排步骤必须提交阶段产物并通过回报完成")
    session = _sessions.get(task_id)
    if session is not None:
        session.graceful_stop()  # 尽量让 claude/codex 落盘会话，便于日后 resume/分享
        _sessions.pop(task_id, None)
    db.execute(
        "UPDATE task SET status='done', ended_at=?, paused_from_status=NULL "
        "WHERE id=? AND status IN ('running','waiting_input','queued','interrupted','paused')",
        (time.time(), task_id),
    )
    events.emit("task.status", {"task_id": task_id, "status": "done"})
    threading.Thread(target=_finalize_tokens, args=(task_id, 4.0), daemon=True).start()
    tick()
    return True


def pause(task_id: int) -> bool:
    """把活动或归档任务移入人工等待区；paused 永不被自动调度。"""
    if db.query_one("SELECT 1 FROM orchestration_step_run WHERE task_id=?", (task_id,)) is not None:
        raise TaskTransitionError("编排步骤不能移入等待执行，请取消整个编排")
    row = db.query_one("SELECT * FROM task WHERE id=? AND deleted=0", (task_id,))
    if row is None:
        raise TaskTransitionError("任务不存在")
    source = row["status"]
    if source not in _PAUSABLE_STATUSES:
        raise TaskTransitionError(f"任务状态 {source} 不能移入等待执行")

    now = time.time()
    ended_at = now if source in {"running", "waiting_input"} else row["ended_at"]
    changed = db.execute_rowcount(
        "UPDATE task SET status='paused', paused_from_status=?, "
        "original_prompt=COALESCE(original_prompt,prompt), ended_at=? "
        "WHERE id=? AND status=? AND deleted=0",
        (source, ended_at, task_id, source),
    )
    if not changed:
        raise TaskTransitionError("任务状态已变化，请刷新后重试")

    session = _sessions.pop(task_id, None)
    if session is not None:
        session.graceful_stop()
        threading.Thread(target=_finalize_tokens, args=(task_id, 4.0), daemon=True).start()
    events.emit("task.status", {"task_id": task_id, "status": "paused"})
    tick()
    return True


def restore_paused(task_id: int) -> bool:
    """把等待任务移回归档；原先是活动态的任务回到不会自动执行的 draft。"""
    row = db.query_one(
        "SELECT status, paused_from_status FROM task WHERE id=? AND deleted=0",
        (task_id,),
    )
    if row is None:
        raise TaskTransitionError("任务不存在")
    if row["status"] != "paused":
        raise TaskTransitionError("只有等待执行的任务可以移回")
    source = row["paused_from_status"]
    target = source if source in _TERMINAL_STATUSES else "draft"
    changed = db.execute_rowcount(
        "UPDATE task SET status=?, prompt=COALESCE(original_prompt,prompt), paused_from_status=NULL "
        "WHERE id=? AND status='paused'",
        (target, task_id),
    )
    if not changed:
        raise TaskTransitionError("任务状态已变化，请刷新后重试")
    events.emit("task.status", {"task_id": task_id, "status": target})
    return True


def resume_paused(task_id: int, extra_prompt: str = "") -> bool:
    """手动执行等待任务：优先恢复有效会话，否则按原始指令开启新会话。

    extra_prompt 为用户追加的指令：恢复会话时它就是唯一要发的内容(留空=只加载上下文)；
    无会话可恢复时并到原始指令后面一起重开。
    """
    row = db.query_one("SELECT * FROM task WHERE id=? AND deleted=0", (task_id,))
    if row is None:
        raise TaskTransitionError("任务不存在")
    if row["status"] != "paused":
        raise TaskTransitionError("只有等待执行的任务可以继续")
    project = db.query_one("SELECT path FROM project WHERE id=?", (row["project_id"],))
    if project is None:
        raise TaskTransitionError("任务所属项目不存在")

    can_resume = bool(
        row["session_uid"]
        and sessions.local_session_info(row["engine"], project["path"], row["session_uid"])
    )
    prior = 0
    if row["started_at"] and row["ended_at"]:
        prior = max(0, int(row["ended_at"] - row["started_at"]))
    extra = (extra_prompt or "").strip()
    if can_resume:
        prompt = extra  # 空 = 只 --resume 加载上下文，不发任何指令
    else:
        origin = row["original_prompt"] or row["prompt"]
        prompt = f"{origin}\n\n补充要求：\n{extra}" if extra else origin
    changed = db.execute_rowcount(
        "UPDATE task SET status='queued', resume=?, prompt=?, session_uid=?, post_input=NULL, "
        "original_prompt=COALESCE(NULLIF(original_prompt,''), NULLIF(prompt,'')), "
        "exit_code=NULL, started_at=NULL, ended_at=NULL, elapsed_accum=elapsed_accum+?, "
        "paused_from_status=NULL WHERE id=? AND status='paused'",
        (int(can_resume), prompt, row["session_uid"] if can_resume else None, prior, task_id),
    )
    if not changed:
        raise TaskTransitionError("任务状态已变化，请刷新后重试")
    events.emit("task.status", {"task_id": task_id, "status": "queued"})
    tick()
    return True


def delete(task_id: int) -> bool:
    """软删除：看板隐藏但保留行(统计历史不丢)；运行中的先停进程、删日志。"""
    session = _sessions.pop(task_id, None)
    if session is not None:
        session.terminate()
    row = db.query_one("SELECT engine, log_path, status, kind FROM task WHERE id=?", (task_id,))
    if row and row["log_path"]:
        remove_terminal_log_files(row["log_path"])
    if row and row["engine"] == "browser":
        browser_computer.remove_browser_assets(task_id)
    # 活动态删除时补一个终态，避免 reconcile 反复处理
    end = "ended_at=COALESCE(ended_at, %f)," % time.time()
    db.execute(
        f"UPDATE task SET deleted=1, {end} "
        "status=CASE WHEN status IN ('running','waiting_input','queued') THEN 'cancelled' ELSE status END "
        "WHERE id=?",
        (task_id,),
    )
    events.emit("task.deleted", {"task_id": task_id})
    _cancel_active_children(task_id)
    if row is not None and "kind" in row.keys() and row["kind"] == "orchestration":
        from . import orchestrations
        orchestrations.handle_task_cancelled(task_id)
    tick()
    return True


def delete_project_tasks(project_id: int) -> int:
    """删除项目时清理关联任务：终止活动会话、移除终端日志，再交给外层删项目级数据。"""
    rows = db.query("SELECT id, engine, log_path FROM task WHERE project_id=?", (project_id,))
    now = time.time()
    for row in rows:
        session = _sessions.pop(row["id"], None)
        if session is not None:
            session.terminate()
        if row["log_path"]:
            remove_terminal_log_files(row["log_path"])
        if row["engine"] == "browser":
            browser_computer.remove_browser_assets(row["id"])
    db.execute(
        "UPDATE task SET deleted=1, ended_at=COALESCE(ended_at, ?), "
        "status=CASE WHEN status IN ('running','waiting_input','queued') THEN 'cancelled' ELSE status END "
        "WHERE project_id=?",
        (now, project_id),
    )
    if rows:
        events.emit("project.tasks_deleted", {"project_id": project_id, "count": len(rows)})
    tick()
    return len(rows)


def to_draft(task_id: int) -> bool:
    """撤回排队：仅对尚未启动的 queued 任务生效。"""
    if db.query_one("SELECT 1 FROM orchestration_step_run WHERE task_id=?", (task_id,)) is not None:
        raise TaskTransitionError("编排步骤不能单独撤回，请取消整个编排")
    db.execute("UPDATE task SET status='draft' WHERE id=? AND status='queued'", (task_id,))
    events.emit("task.status", {"task_id": task_id, "status": "draft"})
    return True


def _reconcile() -> None:
    """对账：DB 标记为运行/待输入、但后端已无 session 的任务 → 标记中断(修状态失同步)。

    宽限 20s：刚启动的任务不参与对账，避免启动竞态(会话尚未注册进 _sessions 时被误标)。
    """
    now = time.time()

    # 若旧版本或短暂的错误调度者曾误标状态，本进程仍实际持有的活会话应恢复为活动态。
    # 只修复 interrupted，避免覆盖用户主动完成/取消的状态。
    for task_id, session in list(_sessions.items()):
        if not session.is_alive():
            continue
        desired = session.status if session.status in {"running", "waiting_input"} else "running"
        if desired == "waiting_input":
            changed = db.execute_rowcount(
                "UPDATE task SET status=?, ended_at=NULL, exit_code=NULL, "
                "waiting_since=COALESCE(waiting_since, ?) "
                "WHERE id=? AND status='interrupted'",
                (desired, now, task_id),
            )
        else:
            changed = db.execute_rowcount(
                "UPDATE task SET status=?, ended_at=NULL, exit_code=NULL, waiting_since=NULL "
                "WHERE id=? AND status='interrupted'",
                (desired, task_id),
            )
        if changed:
            payload = {"task_id": task_id, "status": desired}
            if desired == "waiting_input":
                payload["waiting_kind"] = get_waiting_kind(task_id)
                row = db.query_one("SELECT waiting_since FROM task WHERE id=?", (task_id,))
                payload["waiting_since"] = row["waiting_since"] if row else None
            events.emit("task.status", payload)

    grace = now - 20
    rows = db.query(
        "SELECT id, log_path FROM task WHERE status IN ('running','waiting_input') AND deleted=0 "
        "AND (started_at IS NULL OR started_at < ?)",
        (grace,),
    )
    for r in rows:
        # 只认「活着」的会话：exit 回调失联/后端异常时，死会话对象可能一直挂在注册表里，
        # 仅按成员判断会让任务永卡 running/waiting_input（线上曾卡数小时且 UI 无恢复入口）。
        session = _sessions.get(r["id"])
        if session is not None and session.is_alive():
            continue
        # 保险: 日志近期仍在写 = 有活会话(可能在别的后端进程)，或进程刚退出、
        # 正待 _on_exit 落 done/failed —— 都不抢标中断
        lp = r["log_path"]
        try:
            if lp and (now - os.path.getmtime(lp)) < 15:
                continue
        except OSError:
            pass
        if session is not None:
            # 死会话清出注册表：终端 WS 才能走「回放日志」分支而不是挂在无输出的死会话上
            _sessions.pop(r["id"], None)
        db.execute(
            "UPDATE task SET status='interrupted', ended_at=?, waiting_since=NULL WHERE id=?",
            (now, r["id"]),
        )
        events.emit("task.status", {"task_id": r["id"], "status": "interrupted"})


async def _run_loop() -> None:
    while True:
        try:
            if _claim_scheduler():  # 单实例守卫: 非持锁进程不对账/不调度
                _reconcile()
                tick()
        except Exception:
            pass
        await asyncio.sleep(2.0)


def _refresh_live_tokens() -> None:
    """周期解析运行中任务的 transcript，token 有变化则更新+推送(实时用量)。

    只增量解析上次刷新后新增的字节(见 sessions.LiveTokenCounter)，而非每次重读整份
    持续增长的 transcript——后者是本服务内存随运行时长上涨的主因。
    """
    live_ids: set[int] = set()
    for tid, s in list(_sessions.items()):
        if not s.is_alive():
            continue
        live_ids.add(tid)
        t = db.query_one(
            "SELECT engine, project_id, session_uid, tokens, started_at FROM task WHERE id=?", (tid,)
        )
        if not t or not t["session_uid"]:
            continue
        proj = db.query_one("SELECT path FROM project WHERE id=?", (t["project_id"],))
        if not proj:
            continue
        tok = _live_tokens.update(
            tid,
            t["engine"],
            proj["path"],
            t["session_uid"],
            since=t["started_at"],
        )
        if tok is not None and tok != t["tokens"]:
            db.execute("UPDATE task SET tokens=? WHERE id=?", (tok, tid))
            events.emit("task.tokens", {"task_id": tid, "tokens": tok})
    _live_tokens.retain(live_ids)


def _token_loop() -> None:
    while True:
        time.sleep(15)
        try:
            _refresh_live_tokens()
        except Exception:
            pass


def start(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop
    loop.create_task(_run_loop())
    threading.Thread(target=_token_loop, daemon=True).start()
