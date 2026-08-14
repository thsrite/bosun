"""SQLite 访问层。无 ORM，薄封装 + 全局连接（WAL + 线程安全锁）。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .config import DB_PATH, DEFAULT_MAX_CONCURRENT

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS project_config (
    project_id INTEGER PRIMARY KEY REFERENCES project(id) ON DELETE CASCADE,
    test_cmd TEXT,
    build_cmd TEXT,
    lint_cmd TEXT,
    enabled_sources TEXT DEFAULT 'test,build,lint,audit,git,todo,deps',
    cron TEXT
);
CREATE TABLE IF NOT EXISTS task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    engine TEXT NOT NULL,
    prompt TEXT NOT NULL,
    title TEXT,
    priority INTEGER NOT NULL DEFAULT 5,
    status TEXT NOT NULL DEFAULT 'draft',
    waiting_since REAL,                  -- 当前 waiting_input 轮次起点
    auto_approve INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'task',   -- task | analysis | repair | continue | shared | orchestration
    session_uid TEXT,                    -- 引擎会话 id(claude 钉住 / codex 捕获)
    resume INTEGER NOT NULL DEFAULT 0,   -- 1=以 --resume 方式恢复已有会话
    post_input TEXT,                     -- 启动后自动发给 pty 的输入(如 /compact)
    log_path TEXT,
    exit_code INTEGER,
    created_at REAL NOT NULL,
    started_at REAL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS finding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    title TEXT NOT NULL,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- open | dismissed | task_created | fixed
    origin TEXT NOT NULL DEFAULT 'manual', -- manual | autopilot
    task_id INTEGER REFERENCES task(id) ON DELETE SET NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_session (
    token TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS autopilot_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'running',  -- running | done | stopped | failed
    branch TEXT,
    iteration INTEGER NOT NULL DEFAULT 0,
    max_iterations INTEGER NOT NULL DEFAULT 3,
    fix_engine TEXT NOT NULL DEFAULT 'claude',
    review_engine TEXT NOT NULL DEFAULT 'codex',
    token_budget INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    scope TEXT NOT NULL DEFAULT 'full',   -- full | recent | commit
    scope_arg TEXT,                       -- recent=提交数N; commit=ref/范围
    policy_id INTEGER,
    log_path TEXT,
    summary TEXT,
    created_at REAL NOT NULL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS policy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'recent',
    scope_arg TEXT,
    fix_engine TEXT NOT NULL DEFAULT 'claude',
    review_engine TEXT NOT NULL DEFAULT 'codex',
    max_iterations INTEGER NOT NULL DEFAULT 2,
    token_budget INTEGER NOT NULL DEFAULT 0,
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proposal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    rationale TEXT,
    action TEXT,                          -- JSON 白名单动作, 空=纯建议
    status TEXT NOT NULL DEFAULT 'pending', -- pending | applied | dismissed
    created_at REAL NOT NULL,
    applied_at REAL,
    task_id INTEGER REFERENCES task(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS fix_memory (
    project_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,   -- 修复后又复现的次数
    muted INTEGER NOT NULL DEFAULT 0,      -- 1=已降级为仅报告(不再自动修)
    updated_at REAL NOT NULL,
    PRIMARY KEY (project_id, source, title)
);
CREATE TABLE IF NOT EXISTS autopilot_span (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES autopilot_run(id) ON DELETE CASCADE,
    iteration INTEGER NOT NULL,
    stage TEXT NOT NULL,        -- analyze | fix | verify | review | commit
    label TEXT,
    status TEXT NOT NULL DEFAULT 'running',  -- running | ok | warn | fail
    tokens INTEGER NOT NULL DEFAULT 0,
    detail TEXT,
    started_at REAL NOT NULL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS issue_source (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'http',   -- http (v1); redis/db 后续
    enabled INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL DEFAULT '{}',   -- 连接器专属 JSON(含明文凭据, 读取时脱敏)
    last_pull_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orchestration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orchestration_name_active
    ON orchestration(name) WHERE deleted=0;
CREATE TABLE IF NOT EXISTS orchestration_step (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    orchestration_id INTEGER NOT NULL REFERENCES orchestration(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    engine TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    reasoning_effort TEXT NOT NULL DEFAULT '',
    role_prompt TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(orchestration_id, position)
);
CREATE TABLE IF NOT EXISTS orchestration_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    orchestration_id INTEGER REFERENCES orchestration(id) ON DELETE SET NULL,
    definition_snapshot TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    original_prompt TEXT NOT NULL,
    title TEXT,
    priority INTEGER NOT NULL DEFAULT 5,
    auto_approve INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    current_position INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS orchestration_step_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES orchestration_run(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    engine TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    reasoning_effort TEXT NOT NULL DEFAULT '',
    role_prompt TEXT NOT NULL,
    task_id INTEGER REFERENCES task(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    input_artifact TEXT,
    output_artifact TEXT,
    result TEXT,
    summary TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    ended_at REAL,
    UNIQUE(run_id, position)
);
"""


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def _ensure_table_columns(conn, table: str, adds: dict) -> None:
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, decl in adds.items():
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _ensure_columns() -> None:
    """兼容已有库：缺列则补(SQLite 逐列 ALTER)。"""
    conn = get_conn()
    _ensure_table_columns(conn, "task", {
        "session_uid": "TEXT",
        "resume": "INTEGER NOT NULL DEFAULT 0",
        "post_input": "TEXT",
        "title": "TEXT",
        "tokens": "INTEGER",
        "deleted": "INTEGER NOT NULL DEFAULT 0",  # 软删除: 看板隐藏但统计保留
        "render_mode": "TEXT NOT NULL DEFAULT 'terminal'",  # chat(SDK结构化) | terminal(xterm)
        "elapsed_accum": "INTEGER NOT NULL DEFAULT 0",  # 续聊累计: 历次运行的活跃时长(秒)
        "original_prompt": "TEXT",  # 人工暂停后无会话可恢复时，用原始指令重新执行
        "paused_from_status": "TEXT",  # paused 前的状态，用于移回归档/待办
        "report_result": "TEXT",       # agent 回调结果: done | failed | needs_input
        "report_summary": "TEXT",      # agent 回调的一句话结论
        "waiting_since": "REAL",       # 当前 waiting_input 轮次起点；用于通知补发/去重
        "report_token": "TEXT",        # 本轮派发给 agent 的 /report 回调凭证
        # 受控子任务的父任务 id。SET NULL 而非 CASCADE：父任务被硬删时子任务的
        # 统计历史不该跟着消失（与 task 表既有的软删除取向一致）。
        "parent_task_id": "INTEGER REFERENCES task(id) ON DELETE SET NULL",
    })
    _ensure_table_columns(conn, "finding", {
        "origin": "TEXT NOT NULL DEFAULT 'manual'",  # manual | autopilot
    })
    _ensure_table_columns(conn, "autopilot_run", {
        "token_budget": "INTEGER NOT NULL DEFAULT 0",
        "tokens_used": "INTEGER NOT NULL DEFAULT 0",
        "scope": "TEXT NOT NULL DEFAULT 'full'",
        "scope_arg": "TEXT",
        "policy_id": "INTEGER",
    })
    _ensure_table_columns(conn, "proposal", {
        "task_id": "INTEGER REFERENCES task(id) ON DELETE SET NULL",
        "old_value": "TEXT",        # set_setting/set_policy 应用前的旧值(可回滚/效果对比)
        "metrics_before": "TEXT",   # 应用时的健康快照 JSON(闭环: 事后回看是否变好)
        "dismissed_at": "REAL",
        "dismiss_reason": "TEXT",   # 否决理由, 回流给下一轮反思避免重复提出
    })
    _ensure_table_columns(conn, "orchestration_step", {
        "model": "TEXT NOT NULL DEFAULT ''",
        "reasoning_effort": "TEXT NOT NULL DEFAULT ''",
    })
    _ensure_table_columns(conn, "orchestration_step_run", {
        "model": "TEXT NOT NULL DEFAULT ''",
        "reasoning_effort": "TEXT NOT NULL DEFAULT ''",
    })
    conn.commit()


_PLACEHOLDER_PROMPTS = ("(继续会话)", "(压缩上下文)")


def _clear_placeholder_prompts(conn: sqlite3.Connection) -> None:
    """洗掉历史占位 prompt：旧版继续会话会把「(继续会话)」写进 prompt，
    既污染任务列表显示，又会在下次 resume 时当成真指令发给引擎。
    有原始提示词的挪回 prompt；没有的(原始已丢)直接清空，列表回落到标题。"""
    placeholders = ",".join("?" * len(_PLACEHOLDER_PROMPTS))
    conn.execute(
        f"UPDATE task SET prompt=COALESCE(NULLIF(original_prompt,''),'') "
        f"WHERE prompt IN ({placeholders})",
        _PLACEHOLDER_PROMPTS,
    )
    conn.execute(
        f"UPDATE task SET original_prompt=NULL WHERE original_prompt IN ({placeholders})",
        _PLACEHOLDER_PROMPTS,
    )
    conn.commit()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _rewrite_legacy_engine(value: Any) -> bool:
    """原地递归改写 JSON 中名为 engine 的旧值，返回是否发生变化。"""
    changed = False
    if isinstance(value, dict):
        if value.get("engine") == "cc":
            value["engine"] = "claude"
            changed = True
        for child in value.values():
            changed = _rewrite_legacy_engine(child) or changed
    elif isinstance(value, list):
        for child in value:
            changed = _rewrite_legacy_engine(child) or changed
    return changed


def _migrate_engine_ids(conn: sqlite3.Connection) -> None:
    """把历史内部标识 cc 迁成 claude；重复执行安全。"""
    columns = {
        "task": ("engine",),
        "autopilot_run": ("fix_engine", "review_engine"),
        "policy": ("fix_engine", "review_engine"),
        "orchestration_step": ("engine",),
        "orchestration_step_run": ("engine",),
        "harness_cluster": ("engine",),
        "he_version": ("engine",),
    }
    for table, names in columns.items():
        if not _table_exists(conn, table):
            continue
        for name in names:
            conn.execute(f"UPDATE {table} SET {name}='claude' WHERE {name}='cc'")

    if _table_exists(conn, "orchestration_run"):
        rows = conn.execute(
            "SELECT id, definition_snapshot FROM orchestration_run WHERE definition_snapshot LIKE '%\"cc\"%'"
        ).fetchall()
        for row in rows:
            try:
                snapshot = json.loads(row["definition_snapshot"])
            except (TypeError, json.JSONDecodeError):
                continue
            if _rewrite_legacy_engine(snapshot):
                conn.execute(
                    "UPDATE orchestration_run SET definition_snapshot=? WHERE id=?",
                    (json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), row["id"]),
                )
    conn.commit()


def _migrate_rate_limited_tasks(conn: sqlite3.Connection) -> None:
    """限流等待机制已摘除：历史遗留的 rate_limited 任务收进 done。

    不迁的话这些任务谁也看不见——前端已不再收录该状态，调度器也不再有人把它们捞回
    队列，任务就永久卡死在看板之外。exit_code 保持不动：这些任务并没有真的跑完，
    只是人工归档为已完成，不伪造一个成功的退出码。重复执行安全。
    """
    conn.execute(
        "UPDATE task SET status='done', ended_at=COALESCE(ended_at, ?) "
        "WHERE status='rate_limited'",
        (time.time(),),
    )
    conn.commit()


def init_db() -> None:
    with _lock:
        conn = get_conn()
        conn.executescript(SCHEMA)
        conn.commit()
        _ensure_columns()
        _migrate_engine_ids(conn)
        _migrate_rate_limited_tasks(conn)
        _clear_placeholder_prompts(conn)
        # 默认设置
        cur = conn.execute("SELECT value FROM setting WHERE key='max_concurrent'")
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO setting(key, value) VALUES('max_concurrent', ?)",
                (str(DEFAULT_MAX_CONCURRENT),),
            )
            conn.commit()
        # Stale active tasks are reconciled by scheduler._reconcile(), after the
        # single scheduler owner is claimed and recent log activity is checked.


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return list(get_conn().execute(sql, params).fetchall())


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> int:
    """执行写操作，返回 lastrowid。"""
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def execute_rowcount(sql: str, params: tuple = ()) -> int:
    """执行写操作，返回受影响行数(用于 CAS 原子抢锁)。"""
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount


def get_setting(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM setting WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO setting(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
