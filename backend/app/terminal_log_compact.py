"""TUI 终端日志的整屏重绘压缩：丢掉被后续帧整行覆盖掉的重绘帧。

omp 这类 TUI 不用 DECSET 2026 同步帧，而是每帧 `ESC[?25l … ESC[?25h` 包住、用绝对定位
逐行重画：spinner/状态栏每秒约 10 帧，正文一滚动就整屏重画。1.5 小时的会话能落 60MB+
日志，绝大部分是马上就被覆盖的中间画面。

判定只认「可证明无损」的帧：帧内只有绝对定位(CUP)、SGR、清行(EL)、回车、窗口标题/超链接
和文本，且文本前已关自动换行(?7l)、已复位 SGR —— 这样帧画了哪几行是确定的，渲染也不依赖
帧外状态。某帧画过的每一行都被其后的帧整行清掉重写时，它对最终画面毫无贡献，可以丢弃。
换行/滚动/相对移动/看不懂的序列一律当屏障原样保留，屏障前后的帧互不抵消。
只删除整帧，不改写、不重排任何保留下来的字节。
"""
from __future__ import annotations

import re
import time

_FRAME_START_RE = re.compile(rb"\x1b\[\?(?:25l|2026h)")
_FRAME_ENDS = {b"\x1b[?25l": b"\x1b[?25h", b"\x1b[?2026h": b"\x1b[?2026l"}
# 帧与帧之间可随帧一起丢弃的间隙：窗口标题、帧尾恢复的自动换行/光标显隐
_GAP_TOKEN = rb"\x1b\][012];[^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[\?(?:7|25)[hl]"
_GAP_OK_RE = re.compile(rb"(?:" + _GAP_TOKEN + rb")*")
# 末尾尚未收全的转义序列：先扣下来等下一段，免得把帧起始标记劈成两半
_INCOMPLETE_ESCAPE_RE = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*)?\Z")
_TOKEN_RE = re.compile(
    rb"\x1b\[([0-?]*)([ -/]*)([@-~])"  # CSI
    rb"|\x1b\]([^\x07\x1b]*)(?:\x07|\x1b\\)"  # OSC
    rb"|(\x1b)"  # 其他 ESC 序列
    rb"|([\x00-\x1f\x7f])"  # C0 控制字符
    rb"|[^\x00-\x1f\x7f]+"  # 文本
)
_SAFE_OSC = (b"0;", b"1;", b"2;", b"8;")
_SAFE_PRIVATE_MODES = {b"7", b"25", b"2026"}

MAX_CARRY_BYTES = 1024 * 1024


def _analyze_frame(frame: bytes) -> tuple[set[int], set[int]] | None:
    """返回 (画过的行, 被整行清掉的行)；帧里有任何无法证明无损的内容则返回 None。"""
    touched: set[int] = set()
    full: set[int] = set()
    row: int | None = None
    col: int | None = None
    wrap_off = False
    sgr_reset = False
    for m in _TOKEN_RE.finditer(frame):
        params, inter, final, osc, esc, ctrl = m.groups()
        if final is not None:
            if inter:
                return None
            if final in b"Hf":
                if params.startswith(b"?"):
                    return None
                parts = params.split(b";") if params else []
                try:
                    row = int(parts[0]) if parts and parts[0] else 1
                    col = int(parts[1]) if len(parts) > 1 and parts[1] else 1
                except ValueError:
                    return None
            elif final == b"m":
                if params in (b"", b"0") or params.startswith(b"0;"):
                    sgr_reset = True
            elif final == b"K":
                if row is None:
                    return None
                touched.add(row)
                if params == b"2" or (params in (b"", b"0") and col == 1):
                    full.add(row)
                elif params not in (b"", b"0", b"1"):
                    return None
            elif final == b"G":
                try:
                    col = int(params) if params else 1
                except ValueError:
                    return None
            elif final in b"hl":
                if not params.startswith(b"?"):
                    return None
                modes = params[1:].split(b";")
                if any(mode not in _SAFE_PRIVATE_MODES for mode in modes):
                    return None
                if b"7" in modes:
                    wrap_off = final == b"l"
            else:
                return None
        elif osc is not None:
            if not osc.startswith(_SAFE_OSC):
                return None
        elif esc is not None:
            return None
        elif ctrl is not None:
            if ctrl == b"\r":
                col = 1
            elif ctrl != b"\t" or row is None or not wrap_off:
                return None
        else:
            if row is None or not wrap_off or not sgr_reset:
                return None
            touched.add(row)
            col = None
    return touched, full


class TerminalLogCompactor:
    """流式压缩：feed 原始 PTY 输出，返回此刻可以落盘的字节。

    可能被后续帧覆盖的帧先扣在内存里；遇到屏障、扣留超过 max_pending_bytes，或最早一帧
    扣留超过 max_pending_age 秒时整体放出。读日志前先 flush，拿到的就是完整画面。
    """

    def __init__(
        self,
        max_pending_bytes: int = 1024 * 1024,
        max_pending_age: float = 2.0,
    ):
        self.max_pending_bytes = max_pending_bytes
        self.max_pending_age = max_pending_age
        self._carry = b""
        self._pending: list[list] = []  # [帧字节(含其前间隙), 尚未被覆盖的行]
        self._pending_bytes = 0
        self._pending_since = 0.0

    def feed(self, data: bytes, now: float | None = None) -> bytes:
        now = time.monotonic() if now is None else now
        buf = self._carry + data if self._carry else data
        self._carry = b""
        out: list[bytes] = []
        pos = 0
        while True:
            m = _FRAME_START_RE.search(buf, pos)
            if m is None:
                break
            end = buf.find(_FRAME_ENDS[m.group()], m.end())
            if end < 0:
                break
            end += len(_FRAME_ENDS[m.group()])
            self._take(buf[pos : m.start()], buf[m.start() : end], now, out)
            pos = end
        self._stash_rest(buf[pos:], out)
        if self._pending and (
            self._pending_bytes >= self.max_pending_bytes
            or now - self._pending_since >= self.max_pending_age
        ):
            self._release(out)
        return b"".join(out)

    def flush(self) -> bytes:
        out: list[bytes] = []
        self._release(out)
        out.append(self._carry)
        self._carry = b""
        return b"".join(out)

    def flush_if_stale(self, now: float | None = None) -> bytes:
        now = time.monotonic() if now is None else now
        if not self._pending or now - self._pending_since < self.max_pending_age:
            return b""
        out: list[bytes] = []
        self._release(out)
        return b"".join(out)

    def _take(self, gap: bytes, frame: bytes, now: float, out: list[bytes]) -> None:
        if not _GAP_OK_RE.fullmatch(gap):
            self._release(out)
            out.append(gap)
            gap = b""
        info = _analyze_frame(frame)
        if info is None:
            self._release(out)
            out.append(gap)
            out.append(frame)
            return
        touched, full = info
        kept = []
        for item in self._pending:
            item[1] -= full
            if item[1]:
                kept.append(item)
            else:
                self._pending_bytes -= len(item[0])
        self._pending = kept
        if not kept:
            self._pending_since = now
        chunk = gap + frame
        self._pending.append([chunk, set(touched)])
        self._pending_bytes += len(chunk)

    def _stash_rest(self, rest: bytes, out: list[bytes]) -> None:
        if not rest:
            return
        m = _FRAME_START_RE.search(rest)
        if m is not None:
            # 帧还没收全：帧前间隙照常判定，可丢弃的间隙随帧一起扣下等后续字节
            gap = rest[: m.start()]
            if not _GAP_OK_RE.fullmatch(gap):
                self._release(out)
                out.append(gap)
                rest = rest[m.start() :]
            if len(rest) > MAX_CARRY_BYTES:
                self._release(out)
                out.append(rest)
            else:
                self._carry = rest
            return
        tail = _INCOMPLETE_ESCAPE_RE.search(rest)
        head, tail_bytes = (rest[: tail.start()], rest[tail.start() :]) if tail else (rest, b"")
        if head and not _GAP_OK_RE.fullmatch(head):
            self._release(out)
            out.append(head)
            head = b""
        self._carry = head + tail_bytes
        if len(self._carry) > MAX_CARRY_BYTES:
            self._release(out)
            out.append(self._carry)
            self._carry = b""

    def _release(self, out: list[bytes]) -> None:
        out.extend(item[0] for item in self._pending)
        self._pending = []
        self._pending_bytes = 0


def compact_terminal_frames(data: bytes) -> bytes:
    """一次性压缩整段日志（回放、归档用）。"""
    compactor = TerminalLogCompactor(
        max_pending_bytes=len(data) + 1, max_pending_age=float("inf")
    )
    return compactor.feed(data, now=0.0) + compactor.flush()
