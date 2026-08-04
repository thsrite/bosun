"""cc / codex 限额展示。"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException

from .. import engine_updates, quota

router = APIRouter(prefix="/api/quota", tags=["quota"])


def _engine_call(fn: Callable[[str], dict], engine: str) -> dict:
    try:
        return fn(engine)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("")
def get_quota(engine: str | None = None):
    return quota.get_usage(engine)


@router.get("/tools")
def get_tools():
    return engine_updates.all_version_info()


@router.get("/tools/{engine}")
def get_tool(engine: str):
    return _engine_call(engine_updates.version_info, engine)


@router.post("/tools/{engine}/check-update")
def check_tool_update(engine: str):
    return _engine_call(engine_updates.check_update, engine)


@router.post("/tools/{engine}/update")
def update_tool(engine: str):
    return _engine_call(engine_updates.update_tool, engine)
