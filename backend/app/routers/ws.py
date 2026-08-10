"""WebSocket：
- /ws/session/{task_id}  终端双向 I/O（先回放日志 backlog，再实时流）
- /ws/events             全局事件（任务状态 / findings 变化）→ 前端刷新看板
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import auth, events, scheduler
from ..config import LOG_DIR
from ..pty_session import read_terminal_backlog

router = APIRouter()

# 未通过鉴权时的关闭码（4000+ 为应用自定义区间），前端据此提示重新登录
WS_UNAUTHORIZED = 4401
# 回放前发给前端的控制帧：日志超出扫描/回放预算，本次只回放了最近的输出。
# 以 \x00 开头与终端字节流区分（前端在写入 xterm 前拦截）。
BACKLOG_TRUNCATED_META = "\x00meta:backlog_truncated"
# 跨源页面直连被拒（同源部署下浏览器里只有恶意网页会跨源连 WS）
WS_FORBIDDEN_ORIGIN = 4403


def _same_origin(ws: WebSocket) -> bool:
    """浏览器跨源页面能直连本机 WS——未设口令时等于把 PTY 交给任意网页，
    故凡带 Origin 的连接都要求与 Host 同源（scheme 不参与：反代终结 TLS 后
    upstream 看到的是 http，比 scheme 会误伤 https 部署）。
    非浏览器客户端（curl/脚本/测试）不带 Origin，放行。
    注意：反代需保留原始 Host（nginx 场景 proxy_set_header Host $host），
    否则合法前端也会被判跨源。
    """
    origin = ws.headers.get("origin")
    if not origin:
        return True
    netloc = urlsplit(origin).netloc  # "null"/畸形值 → 空 netloc → 拒绝
    return bool(netloc) and netloc == (ws.headers.get("host") or "")


async def _authorize(ws: WebSocket) -> tuple[bool, str | None]:
    """连接校验，返回 (是否放行, 握手要回选的子协议)；未通过时连接已自行关闭。

    token 走 Sec-WebSocket-Protocol 而非 query：query 会被 uvicorn 的 access log
    连同 URL 整条记进日志，等于把长期有效的凭据写到磁盘上。

    未通过时先 accept 再以 4401 关闭：握手阶段直接拒会让浏览器只看到 1006，
    前端分不清「会话失效」和「网络抖动」，会一直退避重连。accept 后立即关闭，
    期间不读不写、不订阅任何会话，没有实质暴露。
    """
    requested = ws.headers.get("sec-websocket-protocol")
    ticket = auth.token_from_subprotocols(requested)
    # 客户端提了子协议，服务端就必须回选一个，否则浏览器判定握手失败
    subprotocol = auth.WS_AUTH_SUBPROTOCOL if ticket else None
    if not _same_origin(ws):
        await ws.accept(subprotocol=subprotocol)
        await ws.close(code=WS_FORBIDDEN_ORIGIN)
        return False, subprotocol
    if not auth.is_enabled():
        return True, subprotocol
    if auth.validate_token(auth.token_from_request(ws.headers) or ticket):
        return True, subprotocol
    await ws.accept(subprotocol=subprotocol)
    await ws.close(code=WS_UNAUTHORIZED)
    return False, subprotocol

# 「/clear 暴走」诊断哨兵：默认关闭、零开销。它把每条回灌进 PTY 的原始输入（连同毫秒
# 到达时刻）以 repr（保留控制字节）dump 到 logs/task-<id>.input.log，用来看清那波机器
# 速度的合成字节到底是什么、从哪来。抓到现行、定位并修复后即可删除本段。
# 两种开法：① 环境变量 BOSUN_LOG_PTY_INPUT=1（需重启后端）；② 运行时 touch 标志文件
# logs/.log-pty-input 即时开启、rm 即时关闭（无需重启，方便蹲偶发 storm）。
_LOG_PTY_INPUT = os.environ.get("BOSUN_LOG_PTY_INPUT") in ("1", "true", "True")
_PTY_INPUT_FLAG = LOG_DIR / ".log-pty-input"


def _pty_input_logging_enabled() -> bool:
    return _LOG_PTY_INPUT or _PTY_INPUT_FLAG.exists()


def _log_pty_input(task_id: int, text: str) -> None:
    if not _pty_input_logging_enabled():
        return
    try:
        with open(LOG_DIR / f"task-{task_id}.input.log", "a", encoding="utf-8") as f:
            f.write(f"{time.time():.3f} {text!r}\n")
    except OSError:
        pass


@router.websocket("/ws/session/{task_id}")
async def session_ws(ws: WebSocket, task_id: int):
    allowed, subprotocol = await _authorize(ws)
    if not allowed:
        return
    await ws.accept(subprotocol=subprotocol)
    session = scheduler.get_session(task_id)
    if session is None:
        # 任务已结束：回放日志后关闭
        from .. import db

        row = db.query_one("SELECT log_path FROM task WHERE id=?", (task_id,))
        if row and row["log_path"]:
            backlog = read_terminal_backlog(row["log_path"])
            if backlog.truncated:
                await ws.send_text(BACKLOG_TRUNCATED_META)
            if backlog.data:
                await ws.send_bytes(backlog.data)
        await ws.send_text("\r\n\x1b[90m[会话已结束]\x1b[0m\r\n")
        await ws.close()
        return

    # 回放 backlog
    backlog = session.read_backlog()
    if backlog.truncated:
        await ws.send_text(BACKLOG_TRUNCATED_META)
    if backlog.data:
        await ws.send_bytes(backlog.data)
    q = session.subscribe()

    async def pump_out():
        try:
            while True:
                data = await q.get()
                await ws.send_bytes(data)
        except Exception:
            pass

    out_task = asyncio.create_task(pump_out())
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if "text" in msg and msg["text"] is not None:
                text = msg["text"]
                if text.startswith("\x00redraw:"):
                    try:
                        _, rc = text.split(":", 1)
                        rows, cols = rc.split(",")
                        session.redraw(int(rows), int(cols))
                    except ValueError:
                        pass
                elif text.startswith("\x00resize:"):
                    try:
                        _, rc = text.split(":", 1)
                        rows, cols = rc.split(",")
                        session.resize(int(rows), int(cols))
                    except ValueError:
                        pass
                else:
                    _log_pty_input(task_id, text)
                    session.write(text)
            elif "bytes" in msg and msg["bytes"] is not None:
                decoded = msg["bytes"].decode(errors="replace")
                _log_pty_input(task_id, decoded)
                session.write(decoded)
    except WebSocketDisconnect:
        pass
    finally:
        out_task.cancel()
        with suppress(asyncio.CancelledError):
            await out_task
        session.unsubscribe(q)


@router.websocket("/ws/events")
async def events_ws(ws: WebSocket):
    allowed, subprotocol = await _authorize(ws)
    if not allowed:
        return
    await ws.accept(subprotocol=subprotocol)
    q = events.subscribe()
    try:
        while True:
            msg = await q.get()
            await ws.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        events.unsubscribe(q)
