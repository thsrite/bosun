"""按功能把 Bosun 自有 Agent Skills 同步到当前引擎的用户技能目录。

只管理带所有权文件的目录；同名用户技能永不覆盖。派发前再次核对当前引擎，覆盖
Bosun 启动后才安装 CLI 的场景。安装故障只影响技能发现，调用方可退回内联指令。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config, db, engine_updates, sessions

OWNER = "bosun"
OWNER_FILE = ".bosun-managed.json"
FEATURE_SKILLS = {
    "subtask": "bosun-subtask",
    "report": "bosun-report",
}
_SETTING_KEYS = {
    "subtask": "subtask_skill_enabled",
    "report": "report_skill_enabled",
}
ENGINES = ("claude", "codex", "omp", "kimi")
_PATH_SETTING_PREFIX = "agent_skill_path:"


@dataclass(frozen=True)
class InstallResult:
    ready: bool
    reason: str = ""
    path: Path | None = None


def feature_enabled(feature: str) -> bool:
    key = _SETTING_KEYS[feature]
    return str(db.get_setting(key, "0") or "0").strip().lower() not in {"0", "false", "no", "off"}


def path_override(engine: str) -> str:
    if engine not in ENGINES:
        raise ValueError(f"unknown engine: {engine}")
    return str(db.get_setting(f"{_PATH_SETTING_PREFIX}{engine}", "") or "").strip()


def skills_dir(engine: str) -> Path:
    override = path_override(engine)
    if override:
        return Path(override).expanduser()
    if engine == "claude":
        root = Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() if os.environ.get("CLAUDE_CONFIG_DIR") else sessions.claude_home()
    elif engine == "codex":
        root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    elif engine == "omp":
        configured = os.environ.get("PI_CODING_AGENT_DIR")
        root = (
            Path(configured).expanduser()
            if configured
            else Path.home() / os.environ.get("PI_CONFIG_DIR", ".omp") / "agent"
        )
    elif engine == "kimi":
        root = sessions.kimi_home()
    else:
        raise ValueError(f"unknown engine: {engine}")
    return root / "skills"


def _owned(path: Path) -> bool:
    marker = path / OWNER_FILE
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("owner") == OWNER


def _same_tree(left: Path, right: Path) -> bool:
    try:
        left_files = sorted(p.relative_to(left) for p in left.rglob("*") if p.is_file())
        right_files = sorted(p.relative_to(right) for p in right.rglob("*") if p.is_file())
        return left_files == right_files and all(
            (left / rel).read_bytes() == (right / rel).read_bytes() for rel in left_files
        )
    except OSError:
        return False


def install_managed_skill(source: Path, target_root: Path) -> InstallResult:
    destination = target_root / source.name
    if not source.is_dir() or not _owned(source):
        return InstallResult(False, "invalid_source", destination)
    if destination.exists() and not _owned(destination):
        return InstallResult(False, "name_conflict", destination)
    if destination.is_dir() and _same_tree(source, destination):
        return InstallResult(True, path=destination)

    staged_root: Path | None = None
    backup: Path | None = None
    try:
        target_root.mkdir(parents=True, exist_ok=True)
        staged_root = Path(tempfile.mkdtemp(prefix=".bosun-skill-", dir=target_root))
        staged = staged_root / source.name
        shutil.copytree(source, staged)
        if destination.exists():
            backup = staged_root / ".previous"
            destination.rename(backup)
        staged.rename(destination)
        if backup is not None:
            shutil.rmtree(backup)
        return InstallResult(True, path=destination)
    except OSError:
        if backup is not None and backup.exists() and not destination.exists():
            try:
                backup.rename(destination)
            except OSError:
                pass
        return InstallResult(False, "write_failed", destination)
    finally:
        if staged_root is not None:
            shutil.rmtree(staged_root, ignore_errors=True)


def uninstall_managed_skill(path: Path) -> bool:
    if not path.exists() or not _owned(path):
        return False
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        return False


def install_feature(engine: str, feature: str) -> InstallResult:
    source = config.RESOURCE_ROOT / "bosun_skills" / FEATURE_SKILLS[feature]
    return install_managed_skill(source, skills_dir(engine))


def uninstall_feature(engine: str, feature: str) -> bool:
    return uninstall_managed_skill(skills_dir(engine) / FEATURE_SKILLS[feature])


def migrate_managed_skills(engine: str, old_root: Path, new_root: Path) -> None:
    """路径变化时安全搬迁 Bosun 自有技能；新位置冲突或写失败则保留旧副本。"""
    if old_root == new_root:
        return
    for feature, skill_name in FEATURE_SKILLS.items():
        old_path = old_root / skill_name
        if not _owned(old_path):
            continue
        if not feature_enabled(feature):
            uninstall_managed_skill(old_path)
            continue
        result = install_feature(engine, feature)
        if result.ready:
            uninstall_managed_skill(old_path)


def path_overrides() -> dict[str, str]:
    return {engine: value for engine in ENGINES if (value := path_override(engine))}


def path_info() -> dict[str, dict[str, object]]:
    installed = engine_updates.installed_engines()
    return {
        engine: {
            "path": str(skills_dir(engine)),
            "override": path_override(engine),
            "installed": bool(installed.get(engine)),
        }
        for engine in ENGINES
    }


def ensure_for_dispatch(engine: str) -> dict[str, InstallResult]:
    results: dict[str, InstallResult] = {}
    for feature in FEATURE_SKILLS:
        if feature_enabled(feature):
            results[feature] = install_feature(engine, feature)
        else:
            uninstall_feature(engine, feature)
            results[feature] = InstallResult(False, "disabled")
    return results


def sync_installed_engines() -> dict[str, dict[str, InstallResult]]:
    # 关闭功能时清理所有已知位置，不依赖 CLI 目前是否仍安装；这里只读取/删除带
    # Bosun 所有权标记的目录，不会为了未安装 CLI 创建配置目录。
    for engine in ENGINES:
        for feature in FEATURE_SKILLS:
            if not feature_enabled(feature):
                uninstall_feature(engine, feature)

    results: dict[str, dict[str, InstallResult]] = {}
    for engine, installed in engine_updates.installed_engines().items():
        if installed and engine in ENGINES:
            results[engine] = ensure_for_dispatch(engine)
    return results


def report_nudge(engine: str, artifact_required: bool = False) -> str:
    """回合结束仍未回报时才生成提醒；正常用户提示不携带 Bosun 契约。"""
    result = ensure_for_dispatch(engine).get("report")
    if result and result.ready:
        return (
            "[Bosun 提醒] 本轮尚未回报：请立即调用 bosun-report skill；"
            "回报成功后把本轮完整结论正文作为最后一条消息输出再停下。"
        )
    from .directives import ORCHESTRATION_REPORT_ADDENDUM, REPORT_DIRECTIVE

    artifact = ORCHESTRATION_REPORT_ADDENDUM if artifact_required else ""
    return f"[Bosun 提醒] 当前引擎未能加载 bosun-report skill，请按以下约定补报：{REPORT_DIRECTIVE}{artifact}"
