"""Background scheduler for Bosun self-diagnosis reflection proposals."""
from __future__ import annotations

import threading
import time

from . import db, events, quota, reflection

DEFAULT_INTERVAL_MINUTES = 1440
DEFAULT_MIN_PENDING_GAP = 5
_started = False
_running = False
_lock = threading.RLock()


def _as_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_settings() -> dict:
    with _lock:
        running = _running
    return {
        "auto_enabled": _as_bool(db.get_setting("reflection_auto_enabled", "1")),
        "interval_minutes": _as_int(db.get_setting("reflection_interval_minutes", DEFAULT_INTERVAL_MINUTES), DEFAULT_INTERVAL_MINUTES),
        "min_pending_gap": _as_int(db.get_setting("reflection_min_pending_gap", DEFAULT_MIN_PENDING_GAP), DEFAULT_MIN_PENDING_GAP),
        "last_run_at": _float_or_none(db.get_setting("reflection_last_run_at")),
        "last_skip_reason": db.get_setting("reflection_last_skip_reason", "") or "",
        "last_new_count": _as_int(db.get_setting("reflection_last_new_count", 0), 0),
        "running": running,
    }


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def update_settings(auto_enabled: bool, interval_minutes: int, min_pending_gap: int) -> dict:
    interval = max(60, min(10080, int(interval_minutes)))
    pending_gap = max(1, min(50, int(min_pending_gap)))
    db.set_setting("reflection_auto_enabled", "1" if auto_enabled else "0")
    db.set_setting("reflection_interval_minutes", str(interval))
    db.set_setting("reflection_min_pending_gap", str(pending_gap))
    return get_settings()


def _mark(now: float, reason: str, new_count: int = 0) -> None:
    db.set_setting("reflection_last_run_at", str(now))
    db.set_setting("reflection_last_skip_reason", reason)
    db.set_setting("reflection_last_new_count", str(new_count))


def _run_reflect_sync(cwd: str, origin: str = "scheduled") -> int:
    return reflection.reflect(cwd, origin=origin)


def _finish_running() -> None:
    global _running
    with _lock:
        _running = False


def _background_reflect(cwd: str, origin: str) -> None:
    try:
        n = _run_reflect_sync(cwd, origin=origin)
        _mark(time.time(), "", n)
        events.emit("proposals.updated", {"new": n, "origin": origin})
    except Exception as exc:  # noqa: BLE001
        _mark(time.time(), f"error:{type(exc).__name__}", 0)
        events.emit("proposals.updated", {"error": type(exc).__name__, "origin": origin})
    finally:
        _finish_running()


def start_reflection(cwd: str, origin: str = "manual") -> dict:
    """Start a reflection run in the background, sharing the scheduler lock."""
    global _running
    with _lock:
        if _running:
            return {"started": False, "status": "running"}
        _running = True
    threading.Thread(target=_background_reflect, args=(cwd, origin), daemon=True).start()
    return {"started": True, "status": "started"}


def tick_once(now: float | None = None) -> str:
    """Run one scheduler decision cycle. Return status string for tests/diagnostics."""
    global _running
    now = now or time.time()
    settings = get_settings()
    if not settings["auto_enabled"]:
        return "disabled"
    last = settings["last_run_at"]
    if last is not None and now - last < settings["interval_minutes"] * 60:
        return "not_due"
    proj = db.query_one("SELECT path FROM project ORDER BY id LIMIT 1")
    if proj is None:
        return "no_project"
    pending = db.query_one("SELECT COUNT(*) c FROM proposal WHERE status='pending'")["c"]
    if pending >= settings["min_pending_gap"]:
        _mark(now, "pending_proposals")
        events.emit("proposals.updated", {"skipped": "pending_proposals"})
        return "pending_proposals"
    ok = quota.check_engine("cc")[0]
    if not ok:
        _mark(now, "quota")
        events.emit("proposals.updated", {"skipped": "quota"})
        return "quota"
    with _lock:
        if _running:
            return "running"
        _running = True
    try:
        n = _run_reflect_sync(proj["path"], origin="scheduled")
        _mark(now, "", n)
        events.emit("proposals.updated", {"new": n, "origin": "scheduled"})
        return "ran"
    except Exception as exc:  # noqa: BLE001
        # 标记 last_run_at, 反思持续报错时不再每 60s 背靠背重试, 且 UI 可见原因
        _mark(now, f"error:{type(exc).__name__}", 0)
        events.emit("proposals.updated", {"error": type(exc).__name__, "origin": "scheduled"})
        return "error"
    finally:
        with _lock:
            _running = False


def _loop() -> None:
    import traceback

    while True:
        try:
            tick_once()
        except Exception:  # noqa: BLE001
            # 兜底: tick_once 内已对反思异常做 _mark, 此处只应捕获决策逻辑的意外异常
            traceback.print_exc()
        time.sleep(60)


def start_ticker() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
