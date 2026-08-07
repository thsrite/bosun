"""把仓库内的 bosun 专属 skill 幂等安装到已安装引擎的 skills 目录。

每次后端启动调用一次，保证 skill 不丢失、且与仓库内版本一致。
按需安装：机器上没有对应 CLI 时不凭空创建 ~/.claude、~/.codex。
"""
from __future__ import annotations

import shutil
import stat
from pathlib import Path

from . import config, sessions

# backend/app/skills_install.py → 仓库根 = parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = _REPO_ROOT / "bosun_skills" / "bosun-report"

# 引擎 → (家目录, 二进制)。omp 不另设目录，它和 claude 一样从
# ~/.claude/skills 解析 skill://，所以只有 omp 的机器也要装 ~/.claude/skills。
# kimi 的用户级 skills 在数据根目录下(默认 ~/.kimi-code，随 KIMI_CODE_HOME 走)。
_ENGINES: dict[str, tuple[str, str]] = {
    "cc": (".claude", config.CLAUDE_BIN),
    "omp": (".claude", config.OMP_BIN),
    "codex": (".codex", config.CODEX_BIN),
    "kimi": ("", config.KIMI_BIN),
}

# 已装过的 skills 目录；没装成的不入表，下次启动引擎时重判一次
_ensured: set[str] = set()


def _cli_installed(binary: str) -> bool:
    if not binary:
        return False
    path = Path(binary).expanduser()
    if path.is_absolute() or "/" in binary:
        return path.is_file()
    return shutil.which(binary) is not None


def _skills_dir(engine: str) -> Path | None:
    """该引擎要用的 skills 目录；引擎没装且目录也不存在时返回 None。"""
    entry = _ENGINES.get(engine)
    if not entry:
        return None
    home_name, binary = entry
    root = sessions.kimi_home() if engine == "kimi" else Path.home() / home_name
    if root.is_dir() or _cli_installed(binary):
        return root / "skills"
    return None


def ensure_engine_skills(engine: str) -> None:
    """启动某引擎前按需补装：后端跑起来之后才装上的 CLI 不必重启后端。

    同一个 skills 目录本进程只装一次（cc 和 omp 共用 ~/.claude/skills）；
    没装的引擎不进缓存，下次启动它时再判一次。
    """
    target = _skills_dir(engine)
    if target is None or str(target) in _ensured:
        return
    install_skills([target])
    _ensured.add(str(target))


def install_installed_engines() -> None:
    """后端启动时给所有「已安装」的引擎装一遍（同一目录只装一次）。"""
    for engine in _ENGINES:
        ensure_engine_skills(engine)


def install_skills(targets: list[Path]) -> None:
    """把 bosun-report skill 复制到每个 target/<skill 名> 下（覆盖式幂等）。"""
    if not _SOURCE.is_dir():
        return
    for base in targets:
        try:
            dest = Path(base) / _SOURCE.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(_SOURCE, dest)
            script = dest / "scripts" / "report.sh"
            if script.exists():
                script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            # 清理旧品牌残留（deckhand-report → bosun-report 改名后）
            legacy = Path(base) / "deckhand-report"
            if legacy.exists():
                shutil.rmtree(legacy, ignore_errors=True)
        except OSError:
            # 安装失败不应阻断后端启动（如某个目标目录不可写）
            continue
