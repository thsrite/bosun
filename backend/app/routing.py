"""智能引擎路由：按配额余量 + 历史成功率选择已启用的本机编码引擎。

规则透明、无 ML：
- 配额余量 = 100 - max(5 小时用量%, 周用量%)（越高越优先）；拿不到用量记 50(中性)
- 成功率 = 该引擎 done/(done+failed)，无数据记 0.5(中性)
- 分数 = 配额余量 + 成功率*40；高者胜。任一额度窗口达到阈值直接排除。
"""
from __future__ import annotations

from . import db, engine_policy, engine_updates, quota
from fastapi import HTTPException

_PROVIDER = {"claude": "claude", "codex": "codex"}


def _headroom(usage: dict, engine: str) -> float:
    u = usage.get(_PROVIDER.get(engine, engine)) if usage else None
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
    pcts = _window_pcts(usage.get(_PROVIDER.get(engine, engine)) if usage else None)
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


def pick_engine(*, subagent: bool = False) -> tuple[str, str]:
    """返回 (engine, reason)。"""
    # 没装、已关闭或不允许本次派发类型的引擎都不参与自动分配。
    # 分数相同时保留既有 Claude / Codex 优先级，新增引擎不抢占原有默认选择。
    eligible = [e for e in ("claude", "codex", "omp", "kimi", "agy")
                if not engine_policy.rejection(e, subagent=subagent)]
    cands = [e for e in eligible if engine_updates.is_installed(e)]
    if not cands:
        raise HTTPException(409, "没有已安装且允许派发的执行引擎，请检查引擎设置")
    if len(cands) == 1:
        return cands[0], f"只有 {cands[0]} 可用，直接选它"
    usage = quota.get_usage()
    limit = float(usage.get("block_pct", 90))
    # 任一窗口达到阈值的引擎排除(除非全部超限，交给启动守卫确认)
    ok = [e for e in cands if _blocked_window(usage, e, limit) is None]
    if not ok:
        ok = cands
    scores = {}
    for e in ok:
        scores[e] = _headroom(usage, e) + _success_rate(e) * 40
    best = max(ok, key=lambda e: scores[e])
    if not any(_blocked_window(usage, e, limit) is None for e in cands):
        reason = f"可用引擎均达到配额阈值，按配额余量+成功率暂选 {best}"
    else:
        reason = f"综合短时/周配额余量+成功率，选 {best}"
    return best, reason
