"""把收尾回报脚本装进 Bosun 自己的数据目录，并清理旧版对引擎家目录的注入。

旧方案把 bosun-report 做成 skill 复制进 ~/.claude/skills、~/.codex/skills 等
用户全局目录，属于对外部 agent 环境的持久注入（用户反馈：没装过 claude 的机器
也被建出 ~/.claude）。现方案脚本只落在 DATA_DIR(~/.bosun)/libexec 下，派发时经
环境变量 BOSUN_REPORT_DIR 告知 agent 路径，引擎家目录零写入。
"""
from __future__ import annotations

import shutil
import stat
from pathlib import Path

from . import config, sessions

# 随包只读资源，源码运行 = 仓库根，冻结包 = _MEIPASS
_SOURCE = config.RESOURCE_ROOT / "bosun_skills" / "bosun-report" / "scripts"

_ensured = False


def scripts_dir() -> Path:
    return config.DATA_DIR / "libexec" / "bosun-report"


def ensure_installed() -> None:
    """覆盖式幂等安装到 DATA_DIR（本进程只做一次）。

    不装在 RESOURCE_ROOT 原地是因为冻结包的 _MEIPASS 是临时目录：后端重启后
    路径失效，还在跑的 agent 拿着旧路径就回报不了。
    """
    global _ensured
    if _ensured or not _SOURCE.is_dir():
        return
    dest = scripts_dir()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(_SOURCE, dest, ignore=shutil.ignore_patterns("__pycache__"))
        script = dest / "report.sh"
        if script.exists():
            script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _ensured = True
    except OSError:
        # 安装失败不阻断派发；_ensured 保持 False，下次调用重试
        return


def cleanup_legacy_installs() -> None:
    """删除旧版本装进引擎家目录的 skill 副本（只动我们自己创建的目录）。

    skills 目录因此变空时一并移除（rmdir 只删空目录，装了其它 skill 的用户不受
    影响）；引擎家目录本身不动。
    """
    roots = [
        Path.home() / ".claude",
        Path.home() / ".codex",
        sessions.kimi_home(),
    ]
    for root in roots:
        skills = root / "skills"
        for name in ("bosun-report", "deckhand-report"):
            target = skills / name
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
            except OSError:
                continue
        try:
            skills.rmdir()  # 仅当已空
        except OSError:
            pass
