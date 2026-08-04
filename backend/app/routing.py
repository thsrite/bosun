"""智能引擎路由：按配额余量 + 历史成功率 自动选 cc / codex。

规则透明、无 ML：
- 配额余量 = 100 - max(5 小时用量%, 周用量%)（越高越优先）；拿不到用量记 50(中性)
- 成功率 = 该引擎 done/(done+failed)，无数据记 0.5(中性)
- 分数 = 配额余量 + 成功率*40；高者胜。任一额度窗口达到阈值直接排除。
"""
from __future__ import annotations

from . import db, quota

_PROVIDER = {"cc": "claude", "codex": "codex"}


def _headroom(usage: dict, engine: str) -> float:
    u = usage.get(_PROVIDER[engine]) if usage else None
    pcts = _window_pcts(u)
    if pcts:
        return 100.0 - max(pcts.values())
    return 50.0


def _window_pcts(provider_usage: dict | None) -> dict[str, float]:
    if not provider_usage or not provider_usage.get("available"):
        return {}
    out = {}
    for key in ("five_hour_pct", "weekly_pct"):
        value = provider_usage.get(key)
        if isinstance(value, (int, float)):
            out[key] = float(value)
    return out


def _blocked_window(usage: dict, engine: str, limit: float) -> tuple[str, float] | None:
    pcts = _window_pcts(usage.get(_PROVIDER[engine]) if usage else None)
    blocked = [(key, pct) for key, pct in pcts.items() if pct >= limit]
    return max(blocked, key=lambda item: item[1]) if blocked else None


def _success_rate(engine: str) -> float:
    row = db.query_one(
        "SELECT SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) d, "
        "SUM(CASE WHEN status IN ('done','failed') THEN 1 ELSE 0 END) t "
        "FROM task WHERE engine=?",
        (engine,),
    )
    if row and row["t"]:
        return row["d"] / row["t"]
    return 0.5


def pick_engine() -> tuple[str, str]:
    """返回 (engine, reason)。"""
    usage = quota.get_usage()
    cands = ["cc", "codex"]
    limit = float(usage.get("block_pct", 90))
    # 任一窗口达到阈值的引擎排除(除非全部超限，交给启动守卫确认)
    ok = [e for e in cands if _blocked_window(usage, e, limit) is None]
    if not ok:
        ok = cands
    scores = {}
    for e in ok:
        scores[e] = _headroom(usage, e) + _success_rate(e) * 40
    best = max(ok, key=lambda e: scores[e])
    other = "codex" if best == "cc" else "cc"
    blocked = _blocked_window(usage, other, limit)
    if blocked and len(ok) == 1:
        window, pct = blocked
        label = "5 小时" if window == "five_hour_pct" else "周"
        reason = f"{other} {label}用量 {pct:.0f}% 已达阈值，选 {best}"
    elif not any(_blocked_window(usage, e, limit) is None for e in cands):
        reason = f"两个引擎均达到配额阈值，按配额余量+成功率暂选 {best}"
    else:
        reason = f"综合短时/周配额余量+成功率，选 {best}"
    return best, reason
