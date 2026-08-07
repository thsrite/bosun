"""全局设置。"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from .. import backend_control, config, db, engine_models, engine_settings, scheduler

router = APIRouter(prefix="/api/settings", tags=["settings"])


class Settings(BaseModel):
    max_concurrent: int
    claude_invocation: str = "auto"
    claude_model: str = ""
    claude_effort: str = ""
    codex_model: str = ""
    codex_effort: str = ""
    omp_model: str = ""
    omp_thinking: str = ""
    kimi_model: str = ""


@router.get("")
def get_settings():
    return {
        "max_concurrent": int(db.get_setting("max_concurrent", 3)),
        "claude_invocation": engine_settings.claude_invocation(),
        "claude_model": engine_settings.claude_model(),
        "claude_model_options": engine_settings.claude_model_options(),
        "claude_effort": engine_settings.claude_effort(),
        "claude_effort_options": engine_settings.claude_effort_options(),
        "codex_model": engine_settings.codex_model(),
        "codex_model_options": engine_settings.codex_model_options(),
        "codex_effort": engine_settings.codex_effort(),
        "codex_effort_options": engine_settings.codex_effort_options(),
        "omp_model": engine_settings.omp_model(),
        "omp_model_options": engine_settings.omp_model_options(),
        "omp_thinking": engine_settings.omp_thinking(),
        "omp_thinking_options": engine_settings.omp_thinking_options(),
        "kimi_model": engine_settings.kimi_model(),
        "kimi_model_options": engine_settings.kimi_model_options(),
    }


@router.post("/models/{engine}/refresh")
def refresh_model_options(engine: str):
    binaries = {
        "cc": config.CLAUDE_BIN,
        "codex": config.CODEX_BIN,
        "omp": config.OMP_BIN,
        "kimi": config.KIMI_BIN,
    }
    binary = binaries.get(engine)
    if not binary:
        raise HTTPException(400, "不支持的引擎")
    try:
        options = engine_models.refresh_model_options(engine, binary)
    except engine_models.ModelDiscoveryError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"engine": engine, "model_options": options}


@router.post("/restart", status_code=202)
def restart_backend(background_tasks: BackgroundTasks):
    try:
        backend_control.require_current_process_managed()
    except backend_control.BackendRestartUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    background_tasks.add_task(backend_control.restart_current_backend)
    return {"accepted": True}


@router.put("")
def update_settings(body: Settings):
    db.set_setting("max_concurrent", max(1, body.max_concurrent))
    invocation = body.claude_invocation.strip().lower()
    if invocation not in engine_settings.CLAUDE_INVOCATIONS:
        invocation = "auto"
    db.set_setting("claude_invocation", invocation)
    db.set_setting("claude_model", engine_settings.normalize_claude_model(body.claude_model))
    db.set_setting("claude_effort", engine_settings.normalize_claude_effort(body.claude_effort))
    db.set_setting("codex_model", engine_settings.normalize_codex_model(body.codex_model))
    db.set_setting("codex_effort", engine_settings.normalize_codex_effort(body.codex_effort))
    db.set_setting("omp_model", engine_settings.normalize_omp_model(body.omp_model))
    db.set_setting("omp_thinking", engine_settings.normalize_omp_thinking(body.omp_thinking))
    db.set_setting("kimi_model", engine_settings.normalize_kimi_model(body.kimi_model))
    scheduler.tick()  # 提高上限时立即拉起排队任务
    return get_settings()
