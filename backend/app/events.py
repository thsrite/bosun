"""极简异步事件总线：后端状态变化 → 广播给所有 /ws/events 订阅者。"""
from __future__ import annotations

import asyncio
from typing import Any

_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def _put_drop(q: asyncio.Queue, msg) -> None:
    try:
        q.put_nowait(msg)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
            q.put_nowait(msg)
        except Exception:
            pass


def emit(event_type: str, payload: Any = None) -> None:
    """线程安全：可从 pty 读取线程调用。"""
    msg = {"type": event_type, "payload": payload}
    if _loop is None:
        return
    for q in list(_subscribers):
        _loop.call_soon_threadsafe(_put_drop, q, msg)
