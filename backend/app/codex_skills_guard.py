"""Keep cached Superpowers skills disabled for every Codex invocation."""
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
# 旧品牌 marker：升级用户 config.toml 里可能残留，strip 时一并清理，避免重复追加
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


def _managed_block(paths: list[Path], newline: str) -> str:
    return _entries_block(
        [{"path": str(path), "enabled": False} for path in paths],
        newline,
        begin=BEGIN_MARKER,
        end=END_MARKER,
    )


def _entries_block(
    entries: list[dict[str, object]],
    newline: str,
    *,
    begin: str | None = None,
    end: str | None = None,
) -> str:
    lines = [begin] if begin else []
    for entry in entries:
        lines.extend(
            [
                "[[skills.config]]",
                f"path = {json.dumps(str(entry['path']), ensure_ascii=False)}",
                f"enabled = {str(bool(entry['enabled'])).lower()}",
                "",
            ]
        )
    if end:
        lines.append(end)
    return newline.join(lines)


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _uncomment(line: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(line):
        if quote:
            if quote == '"' and character == "\\" and not escaped:
                escaped = True
                continue
            if character == quote and not escaped:
                quote = ""
            escaped = False
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "#":
            return line[:index]
    return line


def _inline_array_end(text: str, start: int) -> int | None:
    depth = 0
    quote = ""
    escaped = False
    index = start
    while index < len(text):
        character = text[index]
        if quote:
            if quote == '"' and character == "\\" and not escaped:
                escaped = True
                index += 1
                continue
            if character == quote and not escaped:
                quote = ""
            escaped = False
            index += 1
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "#":
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
            continue
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _key_path(expression: str) -> tuple[str, ...] | None:
    try:
        parsed: object = tomllib.loads(f"{expression} = 0")
    except tomllib.TOMLDecodeError:
        return None
    path = []
    while isinstance(parsed, dict) and len(parsed) == 1:
        key, parsed = next(iter(parsed.items()))
        path.append(key)
    return tuple(path) if parsed == 0 else None


def _table_header_path(line: str, *, is_array: bool) -> tuple[str, ...] | None:
    stripped = _uncomment(line).strip()
    if is_array:
        if not stripped.startswith("[[") or not stripped.endswith("]]"):
            return None
        expression = stripped[2:-2].strip()
    else:
        if (
            not stripped.startswith("[")
            or stripped.startswith("[[")
            or not stripped.endswith("]")
        ):
            return None
        expression = stripped[1:-1].strip()
    return _key_path(expression)


def _inline_skills_config_span(text: str) -> tuple[int, int] | None:
    offset = 0
    table: tuple[str, ...] = ()
    for line in text.splitlines(keepends=True):
        uncommented = _uncomment(line).strip()
        array_table = _table_header_path(line, is_array=True)
        regular_table = _table_header_path(line, is_array=False)
        if array_table is not None:
            table = array_table
        elif regular_table is not None:
            table = regular_table
        elif "=" in uncommented:
            key, _separator, _value = uncommented.partition("=")
            key_path = _key_path(key.strip())
            if key_path is not None and table + key_path == ("skills", "config"):
                assignment = offset + line.index("=") + 1
                value_start = assignment
                while value_start < len(text) and text[value_start] in " \t\r\n":
                    value_start += 1
                if value_start < len(text) and text[value_start] == "[":
                    end = _inline_array_end(text, value_start)
                    if end is None:
                        raise ValueError("unterminated inline skills.config array")
                    return offset, end
        offset += len(line)
    return None


def _has_skills_config_array_table(text: str) -> bool:
    return any(
        _table_header_path(line, is_array=True) == ("skills", "config")
        for line in text.splitlines()
    )


def _validated_entries(data: dict[str, object]) -> list[dict[str, object]]:
    skills = data.get("skills", {})
    raw_entries = skills.get("config", []) if isinstance(skills, dict) else []
    if not isinstance(raw_entries, list):
        raise ValueError("skills.config is not an array")
    entries = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise ValueError("skills.config contains an invalid entry")
        if set(entry) != {"path", "enabled"}:
            raise ValueError("inline skills.config entry contains unsupported fields")
        skill_path = entry.get("path")
        enabled = entry.get("enabled")
        if not isinstance(skill_path, str) or not isinstance(enabled, bool):
            raise ValueError("skills.config entry lacks path/enabled")
        entries.append({"path": skill_path, "enabled": enabled})
    return entries


def _strip_managed_block(text: str) -> str:
    # 新旧品牌 marker 都清理，避免升级后旧块被当作用户配置保留、导致重复追加
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


def persist_superpowers_disables(home: Path | None = None) -> bool:
    base = home or codex_home()
    try:
        paths = discover_superpowers_skills(base)
    except (OSError, RuntimeError) as exc:
        logger.warning("无法扫描 Superpowers skills 缓存: %s", exc)
        return False
    if not paths:
        return False
    config = _config_path(base)
    try:
        target, target_exists = _config_target(config)
        original = target.read_bytes().decode("utf-8") if target_exists else ""
        data = tomllib.loads(original) if original else {}
        skills = data.get("skills", {})
        if "skills" in data and not isinstance(skills, dict):
            raise ValueError("skills is not a table")
        retained = _strip_managed_block(original)
        inline_span = _inline_skills_config_span(original)
        user_entries = []
        if isinstance(skills, dict) and "config" in skills:
            if inline_span is not None:
                user_entries = _validated_entries(data)
                retained = retained[: inline_span[0]] + retained[inline_span[1] :]
            elif not _has_skills_config_array_table(original):
                raise ValueError("skills.config is not an inline array or array table")
    except (
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
        tomllib.TOMLDecodeError,
    ) as exc:
        logger.warning("拒绝覆盖无效 Codex config.toml: %s", exc)
        return False
    newline = _newline(original)
    blocks = [block for block in (retained, _entries_block(user_entries, newline)) if block]
    blocks.append(_managed_block(paths, newline))
    updated = (newline * 2).join(blocks) + newline
    if updated == original:
        return True
    try:
        payload = updated.encode("utf-8")
    except UnicodeError as exc:
        logger.warning("无法编码 Codex config.toml: %s", exc)
        return False
    return _atomic_write(target, payload, existed=target_exists)
