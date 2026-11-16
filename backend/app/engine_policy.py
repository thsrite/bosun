"""引擎派发开关：只约束新派发，不取消已有会话。"""
from __future__ import annotations

from fastapi import HTTPException

from . import db


def enabled(engine: str) -> bool:
    return str(db.get_setting(f"engine_enabled:{engine}", "1")).lower() not in {"0", "false"}


def subagent_allowed(engine: str) -> bool:
    return str(db.get_setting(f"engine_subagent_enabled:{engine}", "1")).lower() not in {"0", "false"}


def rejection(engine: str, *, subagent: bool = False) -> str | None:
    if not enabled(engine):
        return f"引擎 {engine} 已关闭，请在设置中启用"
    if subagent and not subagent_allowed(engine):
        return f"引擎 {engine} 不允许作为子代理，请在设置中开启"
    return None


def require(engine: str, *, subagent: bool = False) -> None:
    reason = rejection(engine, subagent=subagent)
    if reason:
        raise HTTPException(409, reason)
