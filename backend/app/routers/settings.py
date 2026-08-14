"""全局设置。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from .. import agent_skills, browser_computer, backend_control, config, db, engine_models, engine_settings, log_archive, quota, scheduler
from ..pty_session import script_log_path_for
from ..engines import normalize_engine_id

router = APIRouter(prefix="/api/settings", tags=["settings"])


class Settings(BaseModel):
    max_concurrent: int
    quota_enabled: bool = True
    subtask_skill_enabled: bool = False
    report_skill_enabled: bool = False
    skill_path_overrides: dict[str, str] = Field(default_factory=dict)
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
        "quota_enabled": quota.is_enabled(),
        "subtask_skill_enabled": agent_skills.feature_enabled("subtask"),
        "report_skill_enabled": agent_skills.feature_enabled("report"),
        "skill_path_overrides": agent_skills.path_overrides(),
        "agent_skill_paths": agent_skills.path_info(),
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
        "browser": browser_computer.availability(),
    }


def _tree_stats(path: Path) -> tuple[int, int]:
    """递归统计目录（或单文件）的总字节数与文件数；文件在统计中途消失时跳过。"""
    if path.is_file():
        return path.stat().st_size, 1
    size = 0
    count = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                size += os.path.getsize(os.path.join(dirpath, name))
                count += 1
            except OSError:
                continue
    return size, count


@router.get("/storage")
def get_storage():
    """数据目录占用概览：数据库（含 -wal/-shm）、任务日志、其余文件。"""
    db_size = sum(
        p.stat().st_size
        for suffix in ("", "-wal", "-shm")
        if (p := Path(f"{config.DB_PATH}{suffix}")).exists()
    )
    log_size, log_count = _tree_stats(config.LOG_DIR)
    total_size, total_count = _tree_stats(config.DATA_DIR)
    archived = [p for p in config.LOG_DIR.iterdir() if p.is_file() and p.suffix == ".gz"]
    return {
        "archived_count": len(archived),
        "archived_size": sum(p.stat().st_size for p in archived),
        "data_dir": str(config.DATA_DIR),
        "db_path": str(config.DB_PATH),
        "db_size": db_size,
        "log_dir": str(config.LOG_DIR),
        "log_size": log_size,
        "log_count": log_count,
        "other_size": max(0, total_size - db_size - log_size),
        "total_size": total_size,
        "total_count": total_count,
    }


@router.post("/storage/compress")
def compress_storage():
    """压缩历史任务日志：已终结任务（done/failed/cancelled）的日志 gzip 归档并删原文件；

    进行中或可恢复的任务（queued/running/waiting_input/paused/interrupted）与
    运行中的 autopilot 日志不动。查看历史日志时会自动从压缩包读取。
    """
    protected: set[str] = set()
    for row in db.query(
        "SELECT log_path FROM task "
        "WHERE log_path IS NOT NULL AND status NOT IN ('done','failed','cancelled')"
    ):
        protected.add(row["log_path"])
        protected.add(script_log_path_for(row["log_path"]))
    for row in db.query(
        "SELECT log_path FROM autopilot_run "
        "WHERE log_path IS NOT NULL AND status='running'"
    ):
        protected.add(row["log_path"])
    compressed_count = 0
    saved_size = 0
    for path in config.LOG_DIR.iterdir():
        if not path.is_file() or path.suffix == ".gz" or str(path) in protected:
            continue
        try:
            if path.stat().st_size == 0:
                continue
            saved_size += log_archive.compress(path)
        except OSError:
            continue
        compressed_count += 1
    return {"compressed_count": compressed_count, "saved_size": saved_size, **get_storage()}


@router.post("/models/{engine}/refresh")
def refresh_model_options(engine: str):
    engine = normalize_engine_id(engine)
    binaries = {
        "claude": config.CLAUDE_BIN,
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
    normalized_paths: dict[str, str] = {}
    unknown_engines = set(body.skill_path_overrides) - set(agent_skills.ENGINES)
    if unknown_engines:
        raise HTTPException(400, f"不支持的技能路径引擎: {', '.join(sorted(unknown_engines))}")
    for engine in agent_skills.ENGINES:
        normalized_paths[engine] = normalize_skill_path(body.skill_path_overrides.get(engine, ""))
    old_roots = {engine: agent_skills.skills_dir(engine) for engine in agent_skills.ENGINES}

    db.set_setting("max_concurrent", max(1, body.max_concurrent))
    db.set_setting("quota_enabled", "1" if body.quota_enabled else "0")
    db.set_setting("subtask_skill_enabled", "1" if body.subtask_skill_enabled else "0")
    db.set_setting("report_skill_enabled", "1" if body.report_skill_enabled else "0")
    for engine, path in normalized_paths.items():
        db.set_setting(f"agent_skill_path:{engine}", path)
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
    for engine, old_root in old_roots.items():
        agent_skills.migrate_managed_skills(engine, old_root, agent_skills.skills_dir(engine))
    agent_skills.sync_installed_engines()
    scheduler.tick()  # 提高上限时立即拉起排队任务
    return get_settings()


def normalize_skill_path(raw: str) -> str:
    """校验用户指定的 skills 根目录；空值表示恢复 CLI 默认路径。"""
    value = raw.strip()
    if not value:
        return ""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise HTTPException(400, "技能目录必须是绝对路径")
    path = path.resolve(strict=False)
    if path == Path(path.anchor) or path == Path.home().resolve():
        raise HTTPException(400, "技能目录不能是磁盘根目录或用户主目录")
    return str(path)
