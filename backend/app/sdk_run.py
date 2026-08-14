"""用 Claude Agent SDK 一次性跑 claude(headless)。

替代刮屏解析：token 用量、成败、会话 id 都是结构化的，不再靠正则/事后解析 transcript。
仅用于 claude；codex 无对应 SDK，仍走 subprocess。
"""
from __future__ import annotations

import asyncio

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from . import engine_settings, sessions
from .env import child_env


def _usage_tokens(usage) -> int:
    """只算 input+output（不含 cache_read/cache_creation），与全局 token 口径一致。"""
    if usage is None:
        return 0
    u = usage if isinstance(usage, dict) else getattr(usage, "__dict__", {})
    return sum(v for k, v in u.items() if isinstance(v, int) and k in ("input_tokens", "output_tokens"))


async def _run(prompt: str, cwd: str, permission_mode: str, timeout: int, extra_opts: dict | None = None) -> dict:
    opts_kwargs = {
        "cwd": cwd,
        "env": {**child_env(), **sessions.claude_env_overrides()},
        "permission_mode": permission_mode,  # bypassPermissions=全权限, default=按需
    }
    model = engine_settings.claude_model()
    if model:
        opts_kwargs["model"] = model
    effort = engine_settings.claude_effort()
    if effort:
        opts_kwargs["effort"] = effort
    if extra_opts:
        # 覆盖/补充 ClaudeAgentOptions 字段（如 allowed_tools=[] 禁用工具做纯文本生成）。
        opts_kwargs.update(extra_opts)
    opts = ClaudeAgentOptions(**opts_kwargs)
    parts: list[str] = []
    result = {"text": "", "tokens": 0, "session_id": None, "is_error": False}

    async def go():
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for blk in msg.content:
                    if isinstance(blk, TextBlock):
                        parts.append(blk.text)
            elif isinstance(msg, ResultMessage):
                result["session_id"] = msg.session_id
                result["is_error"] = bool(msg.is_error)
                result["tokens"] = _usage_tokens(msg.usage)
                if msg.result and not parts:
                    parts.append(str(msg.result))

    try:
        await asyncio.wait_for(go(), timeout=timeout)
    except asyncio.TimeoutError:
        result["is_error"] = True
        parts.append("(timeout)")
    except Exception as exc:  # noqa: BLE001
        result["is_error"] = True
        parts.append(f"(sdk error: {exc})")
    result["text"] = "\n".join(parts)
    return result


def run_sync(
    prompt: str,
    cwd: str,
    auto_approve: bool = True,
    timeout: int = 900,
    extra_opts: dict | None = None,
) -> dict:
    """同步封装(供 autopilot 线程调用)。返回 {text, tokens, session_id, is_error}。

    extra_opts 透传给 ClaudeAgentOptions，例如 {"allowed_tools": [], "max_turns": 1}
    可把一次调用退化成无工具、单轮的纯文本生成（不触碰用户仓库）。
    """
    mode = "bypassPermissions" if auto_approve else "default"
    return asyncio.run(_run(prompt, cwd, mode, timeout, extra_opts))
