"""Codex 派发时禁用缓存的 Superpowers skills——只走每次调用的 -c 覆盖，不落盘。

旧版本会把禁用块持久化写进用户的 ~/.codex/config.toml（marker 包裹的管理块）。
持久化修改 config.toml 的做法已停用：运行时覆盖由 runtime_skills_override() 在每次
codex 调用的命令行上携带（engine_settings.with_codex_runtime_args）；
strip_persisted_disables() 只负责清理历史管理块。Bosun 自有 skills 由 agent_skills 管理。
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import tomllib
from pathlib import Path

logger = logging.getLogger("bosun")

BEGIN_MARKER = "# BEGIN bosun-superpowers-disable"
END_MARKER = "# END bosun-superpowers-disable"
# 旧品牌 marker：升级用户 config.toml 里可能残留，strip 时一并清理
_LEGACY_BEGIN_MARKER = "# BEGIN deckhand-superpowers-disable"
_LEGACY_END_MARKER = "# END deckhand-superpowers-disable"


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def discover_superpowers_skills(home: Path | None = None) -> list[Path]:
    base = home or codex_home()
    cache = base / "plugins" / "cache"
    if not cache.is_dir():
        return []
    paths = cache.glob("*/superpowers/*/skills/**/SKILL.md")
    return sorted(path.resolve() for path in paths if path.is_file())


def _config_path(home: Path) -> Path:
    return home / "config.toml"


def _read_existing_entries(path: Path) -> list[dict[str, object]]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        logger.warning("无法读取 Codex skills.config，运行时仅禁用 Superpowers: %s", exc)
        return []
    skills = data.get("skills", {})
    if not isinstance(skills, dict):
        return []
    raw_entries = skills.get("config", [])
    if not isinstance(raw_entries, list):
        return []
    entries = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        skill_path = entry.get("path")
        enabled = entry.get("enabled")
        if isinstance(skill_path, str) and isinstance(enabled, bool):
            entries.append({"path": skill_path, "enabled": enabled})
    return entries


def runtime_skills_override(home: Path | None = None) -> str | None:
    base = home or codex_home()
    discovered = discover_superpowers_skills(base)
    if not discovered:
        return None
    merged = {
        str(entry["path"]): bool(entry["enabled"])
        for entry in _read_existing_entries(_config_path(base))
    }
    for path in discovered:
        merged[str(path)] = False
    entries = [
        f'{{path={json.dumps(path, ensure_ascii=False)},enabled={str(enabled).lower()}}}'
        for path, enabled in merged.items()
    ]
    return f"[{','.join(entries)}]"


def _strip_managed_block(text: str) -> str:
    # 新旧品牌 marker 都清理
    for begin_marker, end_marker in (
        (BEGIN_MARKER, END_MARKER),
        (_LEGACY_BEGIN_MARKER, _LEGACY_END_MARKER),
    ):
        text = _strip_one_managed_block(text, begin_marker, end_marker)
    return text


def _strip_one_managed_block(text: str, begin_marker: str, end_marker: str) -> str:
    begin = text.find(begin_marker)
    end = text.find(end_marker)
    if begin == -1 and end == -1:
        return text
    if begin == -1 or end == -1 or end < begin:
        raise ValueError("Codex config contains an incomplete managed block")
    end += len(end_marker)
    newline = _newline(text)
    if text.startswith(newline, end):
        end += len(newline)
    prefix = text[:begin]
    if prefix.endswith(newline * 2):
        prefix = prefix[: -len(newline) * 2]
    return prefix + text[end:]


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _config_target(config: Path) -> tuple[Path, bool]:
    try:
        metadata = config.lstat()
    except FileNotFoundError:
        return config, False
    if stat.S_ISLNK(metadata.st_mode):
        target = config.resolve(strict=True)
        target_metadata = target.stat()
        if not stat.S_ISREG(target_metadata.st_mode):
            raise ValueError("Codex config symlink target is not a regular file")
        return target, True
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Codex config path is not a regular file")
    return config, True


def _atomic_write(path: Path, payload: bytes, *, existed: bool) -> bool:
    temporary_path = None
    is_successful = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(path.stat().st_mode) if existed else 0o600
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=".config.toml.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        is_successful = True
    except OSError as exc:
        logger.warning("无法原子更新 Codex config.toml: %s", exc)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("无法清理 Codex config.toml 临时文件: %s", exc)
                is_successful = False
    return is_successful


def strip_persisted_disables(home: Path | None = None) -> bool:
    """清掉历史版本写进用户 config.toml 的管理块；没有残留时不碰文件。

    返回 True = 配置已是干净状态（本来就干净，或本次清理成功）。
    """
    base = home or codex_home()
    config = _config_path(base)
    try:
        target, target_exists = _config_target(config)
        if not target_exists:
            return True
        original = target.read_bytes().decode("utf-8")
        stripped = _strip_managed_block(original)
    except (OSError, UnicodeError, ValueError) as exc:
        logger.warning("无法清理 Codex config.toml 中的历史管理块: %s", exc)
        return False
    if stripped == original:
        return True
    return _atomic_write(target, stripped.encode("utf-8"), existed=True)
