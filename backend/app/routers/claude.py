"""Manage safe, global Claude Code configuration files."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import sessions

router = APIRouter(prefix="/api/claude", tags=["claude"])

CLAUDE_HOME = Path.home() / ".claude"
DISABLED_DIR = ".bosun-disabled"
_LEGACY_DISABLED_DIR = ".deckhand-disabled"
MAX_FILE_BYTES = 512 * 1024
MAX_WALK_DEPTH = 6
ROOT_FILES = {"CLAUDE.md", "settings.json", "settings.local.json"}
ALLOWED_DIRS = {"skills", "rules", "commands", "agents", "hooks"}
ALLOWED_SUFFIXES = {".md", ".mdc", ".txt", ".json", ".jsonc", ".yaml", ".yml", ".toml"}
SKIP_DIRS = {"projects", "todos", "statsig", "ide", "shell-snapshots"}

CATEGORY_LABELS = {
    "memory": "CLAUDE.md",
    "settings": "设置",
    "skill": "Skills",
    "rule": "Rules",
    "command": "Commands",
    "agent": "Agents",
    "hook": "Hooks",
    "file": "文件",
}


class FileBody(BaseModel):
    path: str
    content: str


class CreateBody(BaseModel):
    kind: str
    name: str = ""
    content: str | None = None


class ToggleBody(BaseModel):
    path: str
    enabled: bool


def _home() -> Path:
    override = os.environ.get("BOSUN_CLAUDE_HOME")
    if override:
        return Path(override).expanduser()
    # 没装 claude CLI 时跟随内置 SDK 的重定向家目录，避免经本页在
    # 没装 claude 的机器上凭空创建 ~/.claude
    if sessions.claude_cli_installed():
        return CLAUDE_HOME
    return sessions.claude_home()


def _disabled_root() -> Path:
    root = _home() / DISABLED_DIR
    # 升级兼容：旧品牌禁用目录一次性迁移到新目录，否则旧禁用项无法在 UI 里重新启用
    legacy = _home() / _LEGACY_DISABLED_DIR
    if legacy.is_dir() and not root.exists():
        try:
            legacy.rename(root)
        except OSError:
            pass
    return root


def _display(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def _parts(rel_path: str) -> tuple[str, ...]:
    raw = rel_path.replace("\\", "/").strip()
    if not raw:
        raise HTTPException(400, "path 不能为空")
    posix = PurePosixPath(raw)
    if posix.is_absolute():
        raise HTTPException(400, "不允许绝对路径")
    parts = tuple(p for p in posix.parts if p not in {"", "."})
    if not parts or any(p == ".." for p in parts):
        raise HTTPException(400, "路径不合法")
    if any(p.startswith(".") for p in parts):
        raise HTTPException(400, "不允许隐藏文件或隐藏目录")
    return parts


def _category(parts: tuple[str, ...]) -> str:
    if len(parts) == 1 and parts[0] == "CLAUDE.md":
        return "memory"
    if len(parts) == 1 and parts[0].startswith("settings"):
        return "settings"
    if parts[0] == "skills":
        return "skill"
    if parts[0] == "rules":
        return "rule"
    if parts[0] == "commands":
        return "command"
    if parts[0] == "agents":
        return "agent"
    if parts[0] == "hooks":
        return "hook"
    return "file"


def _validate_rel(rel_path: str) -> tuple[str, ...]:
    parts = _parts(rel_path)
    if len(parts) == 1:
        if parts[0] not in ROOT_FILES:
            raise HTTPException(400, "只允许管理 CLAUDE.md 和 Claude 全局设置文件")
        return parts

    if parts[0] not in ALLOWED_DIRS:
        raise HTTPException(400, "只允许管理 skills/rules/commands/agents/hooks 下的文件")
    if len(parts) > MAX_WALK_DEPTH + 1:
        raise HTTPException(400, "目录层级过深")
    suffix = Path(parts[-1]).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, f"只允许这些文件类型: {', '.join(sorted(ALLOWED_SUFFIXES))}")
    return parts


def _resolve(rel_path: str) -> Path:
    parts = _validate_rel(rel_path)
    root = _home().expanduser().resolve()
    target = (root.joinpath(*parts)).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(400, "路径越界") from exc
    return target


def _resolve_disabled(rel_path: str) -> Path:
    parts = _validate_rel(rel_path)
    root = _disabled_root().expanduser().resolve(strict=False)
    target = (root.joinpath(*parts)).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(400, "路径越界") from exc
    return target


def _is_toggleable(parts: tuple[str, ...]) -> bool:
    return _category(parts) != "settings"


def _existing_target(parts: tuple[str, ...]) -> Path | None:
    active = _resolve("/".join(parts))
    if active.is_file():
        return active
    disabled = _resolve_disabled("/".join(parts))
    if disabled.is_file():
        return disabled
    return None


def _entry(rel_path: str) -> dict:
    parts = _validate_rel(rel_path)
    active = _resolve(rel_path)
    disabled = _resolve_disabled(rel_path)
    enabled = active.is_file()
    disabled_exists = disabled.is_file()
    exists = enabled or disabled_exists
    target = active if enabled or not disabled_exists else disabled
    stat = target.stat() if exists else None
    category = _category(parts)
    return {
        "path": "/".join(parts),
        "name": _name_for(parts),
        "label": _label_for(parts),
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, "文件"),
        "disk_path": _display(target),
        "enabled_path": _display(active),
        "disabled_path": _display(disabled),
        "enabled": enabled,
        "toggleable": _is_toggleable(parts) and exists,
        "exists": exists,
        "size": stat.st_size if stat else 0,
        "updated_at": stat.st_mtime if stat else None,
        "deletable": len(parts) > 1,
    }


def _name_for(parts: tuple[str, ...]) -> str:
    if len(parts) == 1:
        return parts[0]
    if parts[0] == "skills" and len(parts) >= 3:
        return parts[1] if parts[-1] == "SKILL.md" else "/".join(parts[1:])
    return "/".join(parts[1:])


def _label_for(parts: tuple[str, ...]) -> str:
    if len(parts) == 1 and parts[0] == "CLAUDE.md":
        return "全局 CLAUDE.md"
    if len(parts) == 1:
        return parts[0]
    if parts[0] == "skills":
        return _name_for(parts)
    return "/".join(parts[1:])


def _walk_dir(dirname: str) -> list[str]:
    root = _home()
    base = root / dirname
    return _walk_files(root, base)


def _walk_disabled_dir(dirname: str) -> list[str]:
    root = _disabled_root()
    base = root / dirname
    return _walk_files(root, base)


def _walk_files(root: Path, base: Path) -> list[str]:
    if not base.is_dir():
        return []
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        current = Path(dirpath)
        try:
            rel_dir = current.relative_to(base)
        except ValueError:
            continue
        depth = len(rel_dir.parts)
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".") and d not in SKIP_DIRS and depth < MAX_WALK_DEPTH
        ]
        for filename in filenames:
            if filename.startswith(".") or Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            rel = current.joinpath(filename).relative_to(root).as_posix()
            try:
                _validate_rel(rel)
            except HTTPException:
                continue
            found.append(rel)
    return found


def _sort_key(item: dict) -> tuple[int, str]:
    order = {"memory": 0, "settings": 1, "skill": 2, "rule": 3, "command": 4, "agent": 5, "hook": 6}
    return (order.get(item["category"], 99), item["path"].lower())


@router.get("/resources")
def list_resources():
    root = _home()
    rels = ["CLAUDE.md"]
    for name in ("settings.json", "settings.local.json"):
        if (root / name).is_file():
            rels.append(name)
    for dirname in sorted(ALLOWED_DIRS):
        rels.extend(_walk_dir(dirname))
        rels.extend(_walk_disabled_dir(dirname))
    disabled_root = _disabled_root()
    for name in ROOT_FILES:
        if (disabled_root / name).is_file():
            rels.append(name)

    entries = []
    for rel in sorted(set(rels)):
        try:
            entries.append(_entry(rel))
        except HTTPException:
            continue
    entries.sort(key=_sort_key)
    return {
        "root": _display(root),
        "resources": entries,
        "categories": CATEGORY_LABELS,
    }


@router.get("/resource")
def get_resource(path: str):
    entry = _entry(path)
    target = _existing_target(tuple(_validate_rel(path)))
    if target is None:
        target = _resolve(path)
    if not target.exists():
        return {**entry, "content": ""}
    if not target.is_file():
        raise HTTPException(400, "不是文件")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise HTTPException(413, f"文件超过 {MAX_FILE_BYTES // 1024}KB，拒绝在页面中编辑")
    return {**entry, "content": target.read_text(encoding="utf-8")}


@router.put("/resource")
def save_resource(body: FileBody):
    parts = _validate_rel(body.path)
    target = _existing_target(parts) or _resolve("/".join(parts))
    if len(body.content.encode("utf-8")) > MAX_FILE_BYTES:
        raise HTTPException(413, f"内容超过 {MAX_FILE_BYTES // 1024}KB")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    return get_resource("/".join(_validate_rel(body.path)))


@router.post("/resource")
def create_resource(body: CreateBody):
    rel = _new_resource_path(body.kind, body.name)
    target = _resolve(rel)
    disabled = _resolve_disabled(rel)
    if target.exists() or disabled.exists():
        raise HTTPException(409, "文件已存在")
    content = body.content if body.content is not None else _template(body.kind, body.name)
    return save_resource(FileBody(path=rel, content=content))


@router.delete("/resource")
def delete_resource(path: str):
    parts = _validate_rel(path)
    if len(parts) == 1:
        raise HTTPException(400, "根级 Claude 配置不能在这里删除")
    target = _existing_target(parts)
    if target is None:
        target = _resolve("/".join(parts))
    deleted = False
    if parts[0] == "skills" and len(parts) == 3 and parts[-1] == "SKILL.md":
        skill_dir = target.parent
        if skill_dir.is_dir():
            shutil.rmtree(skill_dir)
            deleted = True
    elif target.is_file():
        target.unlink()
        deleted = True
        _prune_empty_parents(target.parent, _home() / parts[0])
    return {"ok": True, "deleted": deleted}


@router.put("/resource/enabled")
def set_resource_enabled(body: ToggleBody):
    parts = _validate_rel(body.path)
    if not _is_toggleable(parts):
        raise HTTPException(400, "这个文件不支持启用/关闭")

    active_file = _resolve("/".join(parts))
    disabled_file = _resolve_disabled("/".join(parts))
    active_entity, disabled_entity = _toggle_entities(parts)
    source = disabled_entity if body.enabled else active_entity
    dest = active_entity if body.enabled else disabled_entity

    if body.enabled and active_file.is_file():
        return get_resource("/".join(parts))
    if not body.enabled and disabled_file.is_file():
        return get_resource("/".join(parts))
    if not source.exists():
        raise HTTPException(404, "文件不存在")
    if dest.exists():
        raise HTTPException(409, "目标位置已有同名文件，请先处理冲突")

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    if body.enabled:
        _prune_empty_parents(source.parent, _disabled_root())
    elif parts[0] in ALLOWED_DIRS:
        _prune_empty_parents(source.parent, _home() / parts[0])
    return get_resource("/".join(parts))


def _toggle_entities(parts: tuple[str, ...]) -> tuple[Path, Path]:
    if parts[0] == "skills" and len(parts) == 3 and parts[-1] == "SKILL.md":
        return _resolve("/".join(parts)).parent, _resolve_disabled("/".join(parts)).parent
    rel = "/".join(parts)
    return _resolve(rel), _resolve_disabled(rel)


def _prune_empty_parents(start: Path, stop: Path) -> None:
    stop = stop.resolve(strict=False)
    current = start.resolve(strict=False)
    while True:
        try:
            current.relative_to(stop)
        except ValueError:
            return
        if current == stop:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _clean_segment(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", value.strip())
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = cleaned.strip(".-_")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "untitled"
    return cleaned[:96]


def _clean_nested_name(name: str, default_ext: str) -> str:
    parts = [_clean_segment(part) for part in name.replace("\\", "/").split("/") if part.strip()]
    if not parts:
        parts = ["untitled"]
    suffix = Path(parts[-1]).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        parts[-1] += default_ext
    return "/".join(parts)


def _new_resource_path(kind: str, name: str) -> str:
    kind = kind.strip().lower()
    if kind == "memory":
        return "CLAUDE.md"
    if kind == "skill":
        skill = _clean_segment(name or "untitled-skill")
        return f"skills/{skill}/SKILL.md"
    if kind == "rule":
        return f"rules/{_clean_nested_name(name or 'untitled-rule', '.md')}"
    if kind == "command":
        return f"commands/{_clean_nested_name(name or 'untitled-command', '.md')}"
    if kind == "agent":
        return f"agents/{_clean_nested_name(name or 'untitled-agent', '.md')}"
    if kind == "hook":
        return f"hooks/{_clean_nested_name(name or 'untitled-hook', '.json')}"
    raise HTTPException(400, "不支持的类型")


def _template(kind: str, name: str) -> str:
    title = name.strip() or "Untitled"
    kind = kind.strip().lower()
    if kind == "skill":
        return (
            f"# {title}\n\n"
            "## Description\n"
            "Use this skill when the task matches this capability.\n\n"
            "## Instructions\n"
            "- Add concrete operating steps here.\n"
        )
    if kind == "rule":
        return f"# {title}\n\n- \n"
    if kind in {"command", "agent"}:
        return f"# {title}\n\n"
    if kind == "hook":
        return "{\n  \"hooks\": []\n}\n"
    return ""
