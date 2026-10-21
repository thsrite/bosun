"""会话宿主（ptyd）的后端客户端。

RemotePty 的接口对齐 pty_compat.PtyProcess（read/write/setwinsize/isalive/wait/
exitstatus/terminate/pid），PtySession 不必关心 PTY 是本进程开的还是宿主托管的。
宿主不可用（Windows、BOSUN_PTYD=0、拉起失败）时调用方退回本进程 PTY，
行为与引入宿主之前完全一致——只是重启后端会打断任务。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import ptyd
from .config import DATA_DIR, IS_FROZEN

_FALSE = {"0", "false", "no", "off"}
_CONNECT_TIMEOUT = 2.0
_SPAWN_WAIT_SECONDS = 5.0
_spawn_lock = threading.Lock()


class PtydUnavailable(RuntimeError):
    """宿主连不上或拒绝请求。"""


def enabled() -> bool:
    if sys.platform == "win32":  # 无 AF_UNIX / pty fork 语义，维持本进程 PTY
        return False
    return os.environ.get("BOSUN_PTYD", "1").strip().lower() not in _FALSE


def socket_path() -> Path:
    override = os.environ.get("BOSUN_PTYD_SOCKET")
    return Path(override) if override else ptyd.default_socket_path(DATA_DIR)


def _connect() -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_CONNECT_TIMEOUT)
    try:
        sock.connect(str(socket_path()))
    except OSError as exc:
        sock.close()
        raise PtydUnavailable(str(exc)) from exc
    return sock


def _request(req: dict, keep_open: bool = False) -> tuple[dict, socket.socket | None]:
    sock = _connect()
    try:
        ptyd.send_json(sock, ptyd.T_REQ, req)
        frame = ptyd.recv_frame(sock)
    except (OSError, ValueError) as exc:
        sock.close()
        raise PtydUnavailable(str(exc)) from exc
    if frame is None or frame[0] != ptyd.T_RESP:
        sock.close()
        raise PtydUnavailable("宿主无响应")
    resp = json.loads(frame[1].decode())
    if not resp.get("ok"):
        sock.close()
        raise PtydUnavailable(resp.get("error") or "宿主拒绝请求")
    if keep_open:
        sock.settimeout(None)  # 流模式：读线程长期阻塞等输出
        return resp, sock
    sock.close()
    return resp, None


def ping() -> dict | None:
    try:
        resp, _ = _request({"op": "ping"})
    except PtydUnavailable:
        return None
    return resp if resp.get("protocol") == ptyd.PROTOCOL else None


def _daemon_argv() -> list[str]:
    if IS_FROZEN:  # PyInstaller 包：同一个可执行文件，靠参数分流（见 run.py）
        return [sys.executable, "--ptyd"]
    return [sys.executable, "-m", "app.ptyd"]


def ensure_running() -> bool:
    """宿主在跑就直接用；不在就拉起一个与后端脱钩的进程（独立会话，后端被杀不连带）。"""
    if ping() is not None:
        return True
    with _spawn_lock:
        if ping() is not None:
            return True
        backend_dir = Path(__file__).resolve().parents[1]
        log = open(DATA_DIR / "ptyd.log", "ab")
        try:
            subprocess.Popen(
                _daemon_argv(),
                cwd=str(backend_dir) if not IS_FROZEN else None,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,  # 脱离后端的进程组：launchd 收后端时不会顺手杀掉
                close_fds=True,
            )
        except OSError:
            return False
        finally:
            log.close()
        deadline = time.monotonic() + _SPAWN_WAIT_SECONDS
        while time.monotonic() < deadline:
            if ping() is not None:
                return True
            time.sleep(0.1)
    return False


def list_sessions() -> list[dict] | None:
    """宿主里的会话；宿主不在返回 None（与「宿主在但没有会话」区分开）。"""
    try:
        resp, _ = _request({"op": "list"})
    except PtydUnavailable:
        return None
    return list(resp.get("sessions") or [])


def forget(key: str) -> None:
    """杀掉并丢弃一个会话（任务已不需要它，如后端不在时被取消）。"""
    try:
        _request({"op": "forget", "key": key})
    except PtydUnavailable:
        pass


class RemotePty:
    """宿主托管的 PTY 进程在后端这一侧的代理。"""

    def __init__(self, key: str, sock: socket.socket, pid: int | None):
        self.key = key
        self.pid = pid
        self._sock = sock
        self._send_lock = threading.Lock()
        self._exited = threading.Event()
        self._exitstatus: int | None = None
        self._closed = False

    @classmethod
    def spawn(
        cls,
        key: str,
        argv: list[str],
        cwd: str | None,
        env: dict | None,
        dimensions: tuple[int, int],
        meta: dict,
    ) -> "RemotePty":
        resp, sock = _request(
            {
                "op": "spawn",
                "key": key,
                "argv": list(argv),
                "cwd": cwd,
                "env": {str(k): str(v) for k, v in (env or {}).items()},
                "dimensions": list(dimensions),
                "meta": meta,
            },
            keep_open=True,
        )
        assert sock is not None
        return cls(key, sock, resp.get("pid"))

    @classmethod
    def attach(cls, key: str, pid: int | None = None) -> "RemotePty":
        resp, sock = _request({"op": "attach", "key": key}, keep_open=True)
        assert sock is not None
        return cls(key, sock, resp.get("pid") or pid)

    # ---- PtyProcess 兼容接口 ----
    def read(self, size: int = 1024) -> bytes:
        """只由 PtySession 的读线程调用；进程退出或连接断开时抛 EOFError。"""
        while True:
            try:
                frame = ptyd.recv_frame(self._sock)
            except (OSError, ValueError):
                frame = None
            if frame is None:
                self._mark_exit(None)
                raise EOFError
            ftype, payload = frame
            if ftype == ptyd.T_DATA:
                return payload
            if ftype == ptyd.T_EXIT:
                try:
                    code = json.loads(payload.decode()).get("code")
                except ValueError:
                    code = None
                self._mark_exit(code)
                raise EOFError

    def _mark_exit(self, code: int | None) -> None:
        if not self._exited.is_set():
            self._exitstatus = code
            self._exited.set()
        self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    def _send(self, ftype: int, payload: bytes) -> None:
        if self._exited.is_set():
            return
        with self._send_lock:
            try:
                ptyd.send_frame(self._sock, ftype, payload)
            except OSError:
                pass

    def write(self, data: bytes) -> int:
        self._send(ptyd.T_INPUT, data)
        return len(data)

    def setwinsize(self, rows: int, cols: int) -> None:
        self._send(ptyd.T_CTRL, json.dumps({"op": "resize", "rows": rows, "cols": cols}).encode())

    def isalive(self) -> bool:
        return not self._exited.is_set()

    def wait(self) -> int | None:
        self._exited.wait()
        return self._exitstatus

    @property
    def exitstatus(self) -> int | None:
        return self._exitstatus

    def terminate(self, force: bool = False) -> bool:
        self._send(ptyd.T_CTRL, json.dumps({"op": "kill", "force": force}).encode())
        return True

    def detach(self) -> None:
        """只断开连接、不动进程（测试 / 主动交还给宿主用）。"""
        self._close()
