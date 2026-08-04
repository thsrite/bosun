"""交互式 cc 会话：走 Claude Agent SDK(结构化)，接口对齐 PtySession。

优于 pty 刮屏：
- 权限决策经 can_use_tool 回调 → 弹到前端点"允许/拒绝"，不用在终端敲 1/2/3
- token/会话 id/成败结构化，无需正则/扫文件
- 状态精确：等权限/等输入 = waiting_input，无需静默启发式

渲染：把 SDK 消息格式化成文本流 broadcast(与 pty 相同的订阅/日志机制)，xterm 照常显示。
"""
from __future__ import annotations

import os

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from . import engine_settings
from .engines import with_report_directive
from .env import task_env
from .pty_session import _put_drop


def _fmt_input(inp: dict) -> str:
    for k in ("command", "file_path", "path", "pattern", "url", "prompt"):
        if isinstance(inp, dict) and inp.get(k):
            return str(inp[k])[:160]
    return str(inp)[:160]


class SdkSession:
    def __init__(
        self,
        task_id: int,
        prompt: str,
        cwd: str,
        auto_approve: bool,
        log_path: str,
        loop: asyncio.AbstractEventLoop,
        on_status: Callable[[int, str], None],
        on_exit: Callable[[int, int], None],
        on_session: Callable[[int, str], None],
        on_tokens: Callable[[int, int], None],
        on_permission: Callable[[int, dict | None], None],
    ):
        self.task_id = task_id
        self.prompt = prompt
        self.cwd = cwd
        self.auto_approve = auto_approve
        self.log_path = log_path
        self.loop = loop  # 主事件循环(给回调 call_soon_threadsafe 用)
        self.on_status = on_status
        self.on_exit = on_exit
        self.on_session = on_session
        self.on_tokens = on_tokens
        self.on_permission = on_permission

        self.status = "running"
        self._reported = False
        self.subscribers: set[asyncio.Queue] = set()
        self.pending_permission: dict | None = None
        self._perm_event = threading.Event()
        self._perm_result: dict | None = None
        self._sdk_loop: asyncio.AbstractEventLoop | None = None
        self._input_q: asyncio.Queue | None = None
        self._client: ClaudeSDKClient | None = None
        self._log_fh = None
        self._alive = True
        self._finished = False
        self._exit_code = 0

    @property
    def waiting_kind(self) -> str | None:
        if self.status != "waiting_input":
            return None
        # SDK 的 receive_response() 返回代表这一轮已经完整结束；会话继续存活只是
        # 为了支持追问，因此这里是“待核对”，不是仍在运行或向用户索要输入。
        return "permission" if self.pending_permission else "review"

    # ---- 生命周期 ----
    def start(self) -> None:
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(self.log_path, "ab", buffering=0)
        threading.Thread(target=lambda: asyncio.run(self._amain()), daemon=True).start()

    def _build_options(self) -> ClaudeAgentOptions:
        opts_kwargs = {
            "cwd": self.cwd,
            "env": task_env(self.task_id),
            "permission_mode": "bypassPermissions" if self.auto_approve else "default",
            "can_use_tool": None if self.auto_approve else self._can_use_tool,
            # SDK 默认不下发 --setting-sources，用户级 skill 目录整个看不见；
            # 这里只放开 bosun-report 一个，其余 skill 仍不进上下文。
            "skills": ["bosun-report"],
        }
        model = engine_settings.claude_model()
        if model:
            opts_kwargs["model"] = model
        effort = engine_settings.claude_effort()
        if effort:
            opts_kwargs["effort"] = effort
        return ClaudeAgentOptions(**opts_kwargs)

    async def _amain(self) -> None:
        self._sdk_loop = asyncio.get_event_loop()
        self._input_q = asyncio.Queue()
        opts = self._build_options()
        try:
            async with ClaudeSDKClient(options=opts) as client:
                self._client = client
                pending = with_report_directive(self.prompt)
                while self._alive:
                    await client.query(pending)
                    self._set_status("running")
                    async for msg in client.receive_response():
                        if not self._alive:
                            break
                        self._render(msg)
                    if not self._alive:
                        break
                    # 一轮结束, 等你下一条输入
                    self._set_status("waiting_input")
                    pending = await self._input_q.get()
                    if pending is None:
                        break
                    self._reported = False  # 新一轮开始，解除权威锁，恢复正常状态流转
        except Exception as exc:  # noqa: BLE001
            self._event({"t": "error", "msg": str(exc)})
            self._exit_code = 1
        self._finish()

    def _finish(self) -> None:
        if self._finished:  # 一次性: 避免 terminate 后再触发 on_exit
            return
        self._finished = True
        self._alive = False
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.on_exit, self.task_id, self._exit_code)

    # ---- 渲染 SDK 消息 → NDJSON 事件流 ----
    def _render(self, msg) -> None:
        if isinstance(msg, AssistantMessage):
            for blk in msg.content:
                if isinstance(blk, TextBlock) and blk.text:
                    self._event({"t": "text", "text": blk.text})
                elif isinstance(blk, ToolUseBlock):
                    self._event({"t": "tool", "name": blk.name, "input": _fmt_input(blk.input)})
        elif isinstance(msg, ResultMessage):
            if msg.session_id:
                self.loop.call_soon_threadsafe(self.on_session, self.task_id, msg.session_id)
            u = msg.usage if isinstance(msg.usage, dict) else getattr(msg.usage, "__dict__", {})
            # 只算 input+output，与 sessions.count_tokens 口径一致；cache_read/cache_creation
            # 是缓存前缀重读，长会话可达千万级，混入会导致数值暴涨后又被结算重算覆盖
            toks = sum(v for k, v in u.items() if isinstance(v, int) and k in ("input_tokens", "output_tokens"))
            if toks:
                self.loop.call_soon_threadsafe(self.on_tokens, self.task_id, toks)
            self._event({"t": "result", "tokens": toks, "cost": msg.total_cost_usd or 0})

    # ---- 权限回调(在 SDK loop) ----
    async def _can_use_tool(self, tool_name: str, tool_input: dict, ctx):
        self._perm_result = None
        self._perm_event.clear()
        self.pending_permission = {"tool": tool_name, "input": _fmt_input(tool_input)}
        self._set_status("waiting_input")
        self.loop.call_soon_threadsafe(self.on_permission, self.task_id, self.pending_permission)
        self._event({"t": "perm", "name": tool_name, "input": _fmt_input(tool_input)})
        # 阻塞等前端决策: 每 2s 检查一次, 会话终止或 30 分钟无响应则放弃(拒绝),
        # 避免弃置任务永久占用执行器线程 + 子进程
        def _wait() -> bool:
            for _ in range(900):  # 900 * 2s = 30min
                if not self._alive:
                    return False
                if self._perm_event.wait(timeout=2):
                    return True
            return False

        got = await asyncio.get_event_loop().run_in_executor(None, _wait)
        self.pending_permission = None
        self.loop.call_soon_threadsafe(self.on_permission, self.task_id, None)  # 清除卡片
        if got and self._perm_result and self._perm_result.get("allow"):
            return PermissionResultAllow()
        return PermissionResultDeny(message="用户拒绝或超时未响应")

    def respond_permission(self, allow: bool) -> None:
        self._perm_result = {"allow": allow}
        self._perm_event.set()

    # ---- 事件 → NDJSON 一行(写日志 + 广播) ----
    def _event(self, obj: dict) -> None:
        self._emit((json.dumps(obj, ensure_ascii=False) + "\n").encode())

    # ---- 订阅 / 广播(对齐 PtySession) ----
    def _emit(self, data: bytes) -> None:
        if self._log_fh:
            try:
                self._log_fh.write(data)
            except Exception:
                pass
        for q in list(self.subscribers):
            self.loop.call_soon_threadsafe(_put_drop, q, data)

    def mark_reported(self, status: str) -> None:
        """收到 agent 权威回调：锁定本轮状态。"""
        self._reported = True
        self.status = status

    def _set_status(self, status: str) -> None:
        if self._reported:
            return  # 已收到权威回调，本轮状态锁定，SDK 循环不得覆盖
        if self.status == status:
            return
        self.status = status
        self.loop.call_soon_threadsafe(self.on_status, self.task_id, status)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def read_backlog(self) -> bytes:
        try:
            with open(self.log_path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return b""

    # ---- 输入 / 控制(对齐 PtySession) ----
    def write(self, data: str) -> None:
        # 忽略纯控制/回车，把整行文本作为下一轮 query
        text = data.strip("\r\n")
        if text and self._sdk_loop and self._input_q:
            self._event({"t": "user", "text": text})  # 落库+回放: 转录含用户输入
            self._sdk_loop.call_soon_threadsafe(self._input_q.put_nowait, text)

    def resize(self, rows: int, cols: int) -> None:
        pass

    def graceful_stop(self) -> None:
        self.terminate()

    def terminate(self) -> None:
        self._alive = False
        if self._sdk_loop and self._client:
            try:
                asyncio.run_coroutine_threadsafe(self._client.interrupt(), self._sdk_loop)
            except Exception:
                pass
        if self._input_q and self._sdk_loop:
            self._sdk_loop.call_soon_threadsafe(self._input_q.put_nowait, None)
        self._perm_event.set()

    def is_alive(self) -> bool:
        return self._alive
