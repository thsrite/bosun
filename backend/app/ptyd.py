"""会话宿主（ptyd）：持有任务 CLI 的 PTY，让后端重启不打断正在跑的任务。

PTY 的 master 端由谁持有，CLI 就跟谁同生共死：master 关闭时内核给 CLI 发 SIGHUP。
以前 master 握在后端进程里，重启后端 = 杀掉所有任务。这里把 master 挪到一个独立的
常驻进程：后端只是它的客户端，经 Unix socket 收发字节；后端退出只断开连接，
CLI 继续跑，宿主把这段时间的输出攒着，后端起来后重新 attach 时一次性补交。

刻意保持「小而笨」：只做 spawn / attach / 转发 / resize / kill / list，
不碰日志、不判状态、不认识任务语义——这些都留在后端（PtySession），
这样宿主几乎不需要随版本升级而重启，升级后端也就不会打断任务。
只 import 标准库和 pty_compat，避免拖进后端的重依赖。

协议：每帧 = 1 字节类型 + 4 字节大端长度 + 负载。连接上的第一帧必须是 REQ(JSON)，
宿主回 RESP(JSON)。spawn/attach 成功后该连接绑定到会话，转为流模式：
宿主推 DATA / EXIT，客户端发 INPUT / CTRL。同一会话同一时刻只认一个客户端，
新 attach 会顶掉旧连接（后端重启后旧连接其实早已断开）。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path

PROTOCOL = 1

T_REQ = 1
T_RESP = 2
T_DATA = 3
T_EXIT = 4
T_INPUT = 5
T_CTRL = 6

_HEADER = struct.Struct(">BI")
MAX_FRAME = 64 * 1024 * 1024
# 没有客户端时攒的输出上限：后端长时间不在也不能把宿主内存撑爆。超出丢最旧的，
# 后端回放日志时会少一段，但 TUI 下一次重绘会把画面补齐。
MAX_PENDING_BYTES = 16 * 1024 * 1024
# 没有任何会话时空等多久自行退出；后端下次需要时会重新拉起。
IDLE_EXIT_SECONDS = 30 * 60


def default_socket_path(data_dir: Path) -> Path:
    path = data_dir / "ptyd.sock"
    # AF_UNIX 路径上限 macOS 104 字节、Linux 108 字节；数据目录过深时退到 /tmp
    if len(str(path).encode()) > 100:
        path = Path("/tmp") / f"bosun-ptyd-{os.getuid()}.sock"
    return path


def send_frame(sock: socket.socket, ftype: int, payload: bytes) -> None:
    sock.sendall(_HEADER.pack(ftype, len(payload)) + payload)


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def recv_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    """读一帧；连接关闭返回 None。"""
    header = _recv_exact(sock, _HEADER.size)
    if header is None:
        return None
    ftype, length = _HEADER.unpack(header)
    if length > MAX_FRAME:
        raise ValueError(f"frame too large: {length}")
    payload = _recv_exact(sock, length) if length else b""
    if payload is None:
        return None
    return ftype, payload


def send_json(sock: socket.socket, ftype: int, obj: dict) -> None:
    send_frame(sock, ftype, json.dumps(obj, ensure_ascii=False).encode())


class _Session:
    def __init__(self, key: str, proc, meta: dict):
        self.key = key
        self.proc = proc
        self.meta = meta
        self.created_at = time.time()
        self.lock = threading.Lock()
        self.client: socket.socket | None = None
        self.pending = bytearray()
        self.exit_code: int | None = None
        self.exited = False
        # 后端主动 kill 的会话退出后不必留着等认领
        self.forget_on_exit = False

    def info(self) -> dict:
        return {
            "key": self.key,
            "meta": self.meta,
            "pid": getattr(self.proc, "pid", None),
            "alive": not self.exited,
            "exit_code": self.exit_code,
            "attached": self.client is not None,
            "created_at": self.created_at,
        }


class PtyHost:
    def __init__(self, socket_path: Path):
        self.socket_path = socket_path
        self.sessions: dict[str, _Session] = {}
        self.lock = threading.Lock()
        self.last_activity = time.time()
        self._server: socket.socket | None = None
        self._stop = threading.Event()

    # ---- 生命周期 ----
    def serve(self) -> int:
        if not self._bind():
            return 0  # 已有宿主在跑，安静退出
        threading.Thread(target=self._idle_watch, daemon=True).start()
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        self._cleanup_socket()
        return 0

    def _bind(self) -> bool:
        path = self.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(path))
                return False  # 活的宿主占着
            except OSError:
                path.unlink(missing_ok=True)  # 上一个宿主崩溃留下的死 socket
            finally:
                probe.close()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)  # socket 文件只给本用户读写：能连上就能往终端里打字
        try:
            server.bind(str(path))
        except OSError:
            server.close()
            return False
        finally:
            os.umask(old_umask)
        server.listen(64)
        self._server = server
        return True

    def _cleanup_socket(self) -> None:
        try:
            if self._server is not None:
                self._server.close()
        except OSError:
            pass
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _idle_watch(self) -> None:
        while not self._stop.is_set():
            time.sleep(30)
            with self.lock:
                empty = not self.sessions
            if empty and time.time() - self.last_activity > IDLE_EXIT_SECONDS:
                self.shutdown()

    def shutdown(self) -> None:
        self._stop.set()
        self._cleanup_socket()

    # ---- 连接处理 ----
    def _handle(self, conn: socket.socket) -> None:
        self.last_activity = time.time()
        try:
            frame = recv_frame(conn)
            if frame is None or frame[0] != T_REQ:
                conn.close()
                return
            req = json.loads(frame[1].decode())
            op = req.get("op")
            if op == "ping":
                send_json(conn, T_RESP, {"ok": True, "protocol": PROTOCOL, "pid": os.getpid()})
            elif op == "list":
                with self.lock:
                    items = [s.info() for s in self.sessions.values()]
                send_json(conn, T_RESP, {"ok": True, "sessions": items})
            elif op == "forget":
                self._forget(str(req.get("key", "")))
                send_json(conn, T_RESP, {"ok": True})
            elif op == "shutdown":
                with self.lock:
                    busy = any(not s.exited for s in self.sessions.values())
                if busy and not req.get("force"):
                    send_json(conn, T_RESP, {"ok": False, "error": "sessions alive"})
                else:
                    send_json(conn, T_RESP, {"ok": True})
                    conn.close()
                    self.shutdown()
                    return
            elif op == "spawn":
                session = self._spawn(req)
                send_json(conn, T_RESP, {"ok": True, "pid": getattr(session.proc, "pid", None)})
                self._stream(conn, session)
                return
            elif op == "attach":
                with self.lock:
                    session = self.sessions.get(str(req.get("key", "")))
                if session is None:
                    send_json(conn, T_RESP, {"ok": False, "error": "no such session"})
                else:
                    send_json(conn, T_RESP, {"ok": True, "pid": getattr(session.proc, "pid", None)})
                    self._stream(conn, session)
                    return
            else:
                send_json(conn, T_RESP, {"ok": False, "error": f"unknown op: {op}"})
        except Exception as exc:  # 单个连接出错不能拖垮宿主
            try:
                send_json(conn, T_RESP, {"ok": False, "error": str(exc)})
            except OSError:
                pass
        try:
            conn.close()
        except OSError:
            pass

    def _spawn(self, req: dict) -> _Session:
        from .pty_compat import PtyProcess

        key = str(req["key"])
        with self.lock:
            if key in self.sessions:
                raise ValueError(f"session exists: {key}")
        rows, cols = req.get("dimensions") or (30, 100)
        proc = PtyProcess.spawn(
            list(req["argv"]),
            cwd=req.get("cwd") or None,
            env=req.get("env") or None,
            dimensions=(int(rows), int(cols)),
        )
        session = _Session(key, proc, dict(req.get("meta") or {}))
        with self.lock:
            self.sessions[key] = session
        threading.Thread(target=self._pump, args=(session,), daemon=True).start()
        return session

    def _forget(self, key: str) -> None:
        with self.lock:
            session = self.sessions.get(key)
        if session is None:
            return
        if session.exited:
            self._drop(session)
            return
        session.forget_on_exit = True
        try:
            session.proc.terminate(force=True)
        except Exception:
            pass

    def _drop(self, session: _Session) -> None:
        with self.lock:
            if self.sessions.get(session.key) is session:
                del self.sessions[session.key]
        self.last_activity = time.time()

    # ---- PTY → 客户端 ----
    def _pump(self, session: _Session) -> None:
        proc = session.proc
        while True:
            try:
                data = proc.read(65536)
            except EOFError:
                break
            except Exception:
                break
            if not data:
                break
            with session.lock:
                self._deliver(session, T_DATA, data)
        code = -1
        try:
            proc.wait()
            code = proc.exitstatus if proc.exitstatus is not None else -1
        except Exception:
            pass
        with session.lock:
            session.exit_code = code
            session.exited = True
            delivered = self._deliver(session, T_EXIT, json.dumps({"code": code}).encode())
            if delivered and session.client is not None:
                try:
                    session.client.close()
                except OSError:
                    pass
                session.client = None
        # 已经把退出告知客户端，或后端明确不要了 → 不再保留；
        # 否则留着等后端重新 attach 时认领输出和退出码。
        if delivered or session.forget_on_exit:
            self._drop(session)

    def _deliver(self, session: _Session, ftype: int, payload: bytes) -> bool:
        """持 session.lock 调用：有客户端就直发，没有（或发送失败）就攒起来。"""
        client = session.client
        if client is not None:
            try:
                send_frame(client, ftype, payload)
                return True
            except OSError:
                session.client = None
        if ftype == T_DATA:
            session.pending += payload
            overflow = len(session.pending) - MAX_PENDING_BYTES
            if overflow > 0:
                del session.pending[:overflow]
        return False

    # ---- 客户端 → PTY ----
    def _stream(self, conn: socket.socket, session: _Session) -> None:
        with session.lock:
            old = session.client
            session.client = conn
            if old is not None:
                try:
                    old.close()
                except OSError:
                    pass
            if session.pending:
                try:
                    send_frame(conn, T_DATA, bytes(session.pending))
                    session.pending.clear()
                except OSError:
                    session.client = None
            if session.exited and session.client is conn:
                try:
                    send_frame(conn, T_EXIT, json.dumps({"code": session.exit_code}).encode())
                except OSError:
                    pass
                session.client = None
                conn.close()
                self._drop(session)
                return
        try:
            while True:
                frame = recv_frame(conn)
                if frame is None:
                    break
                ftype, payload = frame
                self.last_activity = time.time()
                if ftype == T_INPUT:
                    try:
                        session.proc.write(payload)
                    except Exception:
                        pass
                elif ftype == T_CTRL:
                    self._control(session, json.loads(payload.decode()))
        except (OSError, ValueError):
            pass
        with session.lock:
            if session.client is conn:
                session.client = None
        try:
            conn.close()
        except OSError:
            pass

    def _control(self, session: _Session, ctrl: dict) -> None:
        op = ctrl.get("op")
        try:
            if op == "resize":
                session.proc.setwinsize(int(ctrl["rows"]), int(ctrl["cols"]))
            elif op == "kill":
                session.forget_on_exit = True
                session.proc.terminate(force=bool(ctrl.get("force", True)))
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    from .config import DATA_DIR

    # 与后端彻底脱钩：不受终端挂断影响，后端被杀时也不会连带收到信号
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    path = Path(os.environ.get("BOSUN_PTYD_SOCKET") or default_socket_path(DATA_DIR))
    return PtyHost(path).serve()


if __name__ == "__main__":
    sys.exit(main())
