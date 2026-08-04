"""策略引擎：按间隔定时触发自愈(不同 scope 可配多条)。

轻量后台 ticker，每分钟检查到期的策略并触发 autopilot（同项目已有运行则跳过）。
"""
from __future__ import annotations

import threading
import time

from . import autopilot, db, events, quota

_started = False


def _tick() -> None:
    now = time.time()
    for p in db.query("SELECT * FROM policy WHERE enabled=1"):
        due = p["last_run_at"] is None or (now - p["last_run_at"]) >= p["interval_minutes"] * 60
        if not due or autopilot.has_active_run(p["project_id"]):
            continue
        # 限额预检：超阈值则本轮跳过(推迟到下个间隔), 防定时任务烧爆
        blocked = any(not quota.check_engine(e)[0] for e in {p["fix_engine"], p["review_engine"]})
        if blocked:
            db.execute("UPDATE policy SET last_run_at=? WHERE id=?", (now, p["id"]))
            events.emit("policy.skipped", {"policy_id": p["id"], "reason": "quota"})
            continue
        db.execute("UPDATE policy SET last_run_at=? WHERE id=?", (now, p["id"]))
        try:
            autopilot.start(
                p["project_id"], p["max_iterations"], p["fix_engine"], p["review_engine"],
                p["token_budget"], p["scope"], p["scope_arg"], p["id"],
            )
            events.emit("policy.triggered", {"policy_id": p["id"], "project_id": p["project_id"]})
        except Exception:  # noqa: BLE001
            pass


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(60)


def start_ticker() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
