"""Bosun 自身的版本检查与在线更新。"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks

from .. import backend_control, self_update

router = APIRouter(prefix="/api/self-update", tags=["self-update"])


@router.get("")
def get_status():
    return self_update.status()


@router.post("/check")
def check():
    return self_update.check_update()


@router.post("/run")
def run(background_tasks: BackgroundTasks):
    result = self_update.run_update()
    if not (result.get("ok") and result.get("changed")):
        return {**result, "restart": "none"}

    # 新代码要生效必须重启；只有 Bosun.app 托管的后端能自己重启，其余场景交给用户
    try:
        backend_control.require_current_process_managed()
    except backend_control.BackendRestartUnavailable as exc:
        return {**result, "restart": "manual", "restart_hint": f"{exc}，请手动重启后端使新版本生效"}

    background_tasks.add_task(backend_control.restart_current_backend)
    return {**result, "restart": "scheduled"}
