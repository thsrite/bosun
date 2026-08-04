"""子进程环境清理。

bosun 后端若从 Claude Code 会话内启动，会继承 CLAUDECODE / CLAUDE_CODE_SESSION_ID /
CLAUDE_CODE_CHILD_SESSION 等标记。这些若泄漏给被 spawn 的 cc/codex，子进程会以为自己是
"嵌套/子会话" → 不独立持久化会话 transcript → --resume 找不到会话。故一律剥离。
"""
from __future__ import annotations

import os

_STRIP = {
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_EFFORT",
    "AI_AGENT",
}


def child_env(extra: dict | None = None) -> dict:
    e = {
        k: v
        for k, v in os.environ.items()
        if k not in _STRIP and not k.startswith("CLAUDE_CODE_")
    }
    e["TERM"] = "xterm-256color"
    e["COLORTERM"] = "truecolor"
    e["CLICOLOR"] = "1"
    e["FORCE_COLOR"] = "3"
    e.pop("NO_COLOR", None)
    if extra:
        e.update(extra)
    return e


def api_base() -> str:
    """bosun 后端回环地址，供子进程回调用。"""
    port = os.environ.get("BOSUN_PORT", "8770")
    return f"http://127.0.0.1:{port}"
