"""把仓库内的 bosun 专属 skill 幂等安装到 cc/codex 的 skills 目录。

每次后端启动调用一次，保证 skill 不丢失、且与仓库内版本一致。
"""
from __future__ import annotations

import shutil
import stat
from pathlib import Path

# backend/app/skills_install.py → 仓库根 = parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = _REPO_ROOT / "bosun_skills" / "bosun-report"

_DEFAULT_TARGETS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".codex" / "skills",
]


def install_skills(targets: list[Path] | None = None) -> None:
    """把 bosun-report skill 复制到每个 target/<skill 名> 下（覆盖式幂等）。"""
    if not _SOURCE.is_dir():
        return
    for base in (targets if targets is not None else _DEFAULT_TARGETS):
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
