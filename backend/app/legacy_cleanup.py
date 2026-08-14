"""清理历史版本注入到本机的收尾回报残留。

新版 skill 带所有权文件并由 agent_skills 管理。本模块只清理已明确识别的旧品牌 skill
与短暂存在过的 DATA_DIR/libexec 脚本；同名但无所有权标记的用户目录不能删除。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import config, sessions


def cleanup_legacy_installs() -> None:
    """删除旧版本创建的目录（只动我们自己创建的，引擎家目录本身不动）。

    skills 目录因此变空时一并移除（rmdir 只删空目录，装了其它 skill 的用户
    不受影响）。
    """
    roots = [
        Path.home() / ".claude",
        Path.home() / ".codex",
        sessions.kimi_home(),
    ]
    for root in roots:
        skills = root / "skills"
        for name in ("deckhand-report",):
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
    # 上一版把脚本装进过 DATA_DIR/libexec，同样退役
    libexec = config.DATA_DIR / "libexec"
    try:
        if (libexec / "bosun-report").is_dir():
            shutil.rmtree(libexec / "bosun-report", ignore_errors=True)
        libexec.rmdir()  # 仅当已空
    except OSError:
        pass
