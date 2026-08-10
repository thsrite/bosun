"""子进程环境清理。

bosun 后端若从 Claude Code 会话内启动，会继承 CLAUDECODE / CLAUDE_CODE_SESSION_ID /
CLAUDE_CODE_CHILD_SESSION 等标记。这些若泄漏给被 spawn 的 cc/codex，子进程会以为自己是
"嵌套/子会话" → 不独立持久化会话 transcript → --resume 找不到会话。故一律剥离。

同理，后端若本身跑在某个 Bosun 任务里，会继承那个任务的 BOSUN_TASK_ID / BOSUN_API，
泄漏下去会让子进程拿着别的任务 id 回报状态，所以也一并剥掉，只保留本层显式注入的值。
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
    # 继承自「跑着 bosun 后端的那个 Bosun 任务」，不能顺着传给本层派发的子进程
    "BOSUN_TASK_ID",
    "BOSUN_TASK_TOKEN",
    "BOSUN_API",
    "BOSUN_BACKEND_PID",
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


def task_env(task_id: int) -> dict:
    """派发给 agent 的环境：任务 id、回调地址与本轮回调凭证。

    BOSUN_TASK_ID 会被 agent 自己拉起的子 agent 继承，这类冒名回报由后端在
    /report 端点按进程链识别(见 nesting.py)，不依赖子进程自证。

    BOSUN_TASK_TOKEN 让 agent 在开了访问口令时也能回报：它只对本任务的
    /report 端点有效(见 auth.issue_task_token)，拿不到别的接口。
    """
    from . import auth

    return child_env({
        "BOSUN_TASK_ID": str(task_id),
        "BOSUN_API": api_base(),
        "BOSUN_TASK_TOKEN": auth.issue_task_token(task_id),
    })


def api_base() -> str:
    """bosun 后端回环地址，供子进程回调用。"""
    port = os.environ.get("BOSUN_PORT", "8770")
    return f"http://127.0.0.1:{port}"
