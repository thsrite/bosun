"""单个任务的 pty 会话：拉起 cc/codex 交互进程，流式读写 + 日志落盘。

- 后台线程读 pty 输出：追加到日志文件 + 推给所有订阅的 asyncio.Queue
- write() 把用户输入写回 pty
- 任务状态只认 agent 的权威回报（bosun-report → mark_reported）；基于终端输出的
  正则/静默启发式判定默认关闭，需要时用 BOSUN_WAIT_HEURISTICS=1 回退开启
- 进程退出 → 触发 on_exit 回调（调度器据此释放槽位）
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .config import IDLE_SECONDS
from .pty_compat import PtyProcess

# PTY logs contain every TUI repaint. A long Codex/Claude session can therefore
# grow to tens of megabytes even though xterm only needs the latest screen and
# useful recent scrollback when a panel is opened or reconnected.
MAX_TERMINAL_BACKLOG_BYTES = 2 * 1024 * 1024

# 回放 backlog 时必须剥掉「会让终端回话」的查询序列。日志记录的是 CC 发出的原始输出流，
# 里面每一条光标位置查询(ESC[?6n)、设备属性查询(ESC[c)在重放时都会被 xterm 当成实时提问
# 并逐条应答，把成千上万条 `ESC[?41;3R` 沿 WS 回灌进 PTY(实测一次重连 1 秒内 3114 条)。
# 这些序列不画任何东西，剥掉不影响回放画面。仅剥查询，不碰同字母的设置类序列
# (如 CSI 22;0t 存窗口标题)。
_TERMINAL_QUERY_RE = re.compile(
    rb"\x1b\[[?>=]?[0-9;]*n"  # DSR：光标位置/状态查询
    rb"|\x1b\[[?>=]?[0-9;]*c"  # DA：主/次/三级设备属性查询
    rb"|\x1b\[\??[0-9;]*\$p"  # DECRQM：模式状态查询
    rb"|\x1b\[(?:11|13|14|15|16|18|19)(?:;[0-9]+)*t"  # XTWINOPS 查询型
    rb"|\x1b\][0-9]+;\?(?:\x07|\x1b\\)"  # OSC 颜色查询
    rb"|\x1bP\+q[^\x1b]*(?:\x1b\\)?"  # DCS XTGETTCAP
    rb"|\x1bZ"  # DECID
)


def strip_terminal_queries(data: bytes) -> bytes:
    """Remove sequences that would make a replaying terminal answer back."""
    return _TERMINAL_QUERY_RE.sub(b"", data)


def read_terminal_backlog(
    log_path: str, max_bytes: int = MAX_TERMINAL_BACKLOG_BYTES
) -> bytes:
    """Read a bounded terminal tail without truncating the log stored on disk."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size <= max_bytes:
                f.seek(0)
                return strip_terminal_queries(f.read())

            f.seek(-max_bytes, 2)
            data = f.read()
    except FileNotFoundError:
        return b""

    # Avoid beginning in the middle of a CSI sequence or a huge UTF-8/TUI line.
    # Never discard a large part of the bounded tail merely to find a boundary.
    search_end = min(len(data), 64 * 1024)
    boundary = data.find(b"\x1b[", 0, search_end)
    if boundary < 0:
        boundary = data.find(b"\n", 0, search_end)
        if boundary >= 0:
            boundary += 1
    return strip_terminal_queries(data[max(boundary, 0) :])

# 去除 ANSI/OSC/光标形态等终端控制序列，便于文本匹配。
# Codex 会在单词字符之间插入 `ESC [ 0 q` 这类 CSI 序列；若不清掉，
# `Working` 会变成 `Work\x1b[0 qing`，导致忙碌检测失效。
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[\]PX^_][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[@-Z\\-_]"
    r"|[\r]"
)
_BUSY_GRACE_SECONDS = max(IDLE_SECONDS * 2, 20.0)
# Semantic prompt words are common in source code, diffs and documentation (for
# example ``getByText('请选择云盘实例')``).  Only treat them as prompts when they
# begin a terminal/output line, optionally after a normal list/prompt marker.
_PROMPT_LINE_PREFIX = r"(?:^|\n)[ \t]*(?:[❯›?•*+-][ \t]*)?"
# cc/codex 等待用户决策(权限/选择/确认)的输出特征
_CHOICE_PROMPT_RES = [
    re.compile(_PROMPT_LINE_PREFIX + r"do you want to proceed", re.I),
    re.compile(_PROMPT_LINE_PREFIX + r"(?:.+?\s+)?requires approval", re.I),
    re.compile(_PROMPT_LINE_PREFIX + r"1\.\s*(yes|allow)", re.I),
    re.compile(_PROMPT_LINE_PREFIX + r"1\.\s*yes.{0,120}(?:2\.|\bno\b)", re.I | re.S),
    re.compile(
        _PROMPT_LINE_PREFIX + r"(?:select|choose)\s+(?:an?\s+)?(?:option|choice)",
        re.I,
    ),
    re.compile(
        _PROMPT_LINE_PREFIX + r"[^\n]{0,120}(?:\[y/n\]|\(y/n\)|\by/N\b)",
        re.I,
    ),
    re.compile(_PROMPT_LINE_PREFIX + r"press\s+enter\s+to", re.I),
    re.compile(_PROMPT_LINE_PREFIX + r"(?:是否继续|请选择|需要确认)", re.I),
]
_INPUT_PROMPT_RES = [
    re.compile(
        _PROMPT_LINE_PREFIX
        + r"(?:please\s+)(?:provide|enter|input|paste|upload|attach|tell\s+me)\b",
        re.I,
    ),
    re.compile(
        _PROMPT_LINE_PREFIX + r"请(?:提供|输入|粘贴|上传|补充|告知|告诉我|回复)",
        re.I,
    ),
]
# Codex 在一轮 agent 执行结束、输出最终答复前绘制该分隔线。进程此时仍存活，
# 但任务已经不再执行，应该交给人核对结果，而不是继续显示“运行中”。
_REVIEW_PROMPT_RES = [
    re.compile(r"(?:^|\n)\s*[─━-]+\s*Worked\s+for\s+\d", re.I),
]
# Codex/Claude TUI often keeps an input prompt visible while it is still busy.
# Idle wait detection must not treat that as user input being required.
_BUSY_RES = [
    re.compile(r"[•·●]\s*(?:working|thinking)", re.I),
    re.compile(r"\b(?:working|thinking)\s*\(", re.I),
    re.compile(r"\b(working|thinking)\b.{0,120}\b(esc|interrupt)\b", re.I | re.S),
    # `esc to interrupt` 是 cc/codex 活跃转圈的确定锚点：出现即正在生成，真等待用户
    # 决策时不会显示它。用它把“忙碌”判定与俏皮结束词（worked/baked/…）彻底解耦。
    re.compile(r"esc\s+to\s+interrupt", re.I),
]
# cc/codex 回合已结束但后台仍有 shell 在跑：真结果还在产出，不该催用户核对。
# 两种形态：结束摘要行 `N shell still running`、底栏 `· N shell ·`。刻意不匹配随处
# 可见的 `Ran N shell command`（那是已完成的单次命令，非存活的后台进程）。
_BG_SHELL_RES = [
    re.compile(r"\d+\s+shell[s]?\s+still\s+running", re.I),
    re.compile(r"[·•]\s*\d+\s+shell[s]?\s*(?:[·•]|$)", re.I | re.M),
]
# 会话被 `/clear` 冲掉（手机端合成 Cmd+K 暴走，或用户自己敲）的结构信号：CC 把提交的
# 命令回显在输入行上，`❯ /clear` 之后是补白到行尾的空格。刻意不匹配正文里出现的
# `/clear` 字面量（聊天内容/文档/底栏提示 `new task? /clear to save …` 都会命中它），
# 只认输入行这一种渲染，避免把「聊到 /clear」误判成「会话被清」。
_SESSION_CLEAR_RES = [
    re.compile(r"[❯›]\s*/clear(?:[ \t]{10,}|[ \t]*$)", re.M),
]
_IDLE_PROMPT_RES = [
    re.compile(r"(?:^|\n)\s*›\s*(?:\n|$)", re.M),
    re.compile(r"use\s+/[a-z0-9_-]+\s+to\b", re.I),
    re.compile(r"type\s+(?:a\s+)?(?:message|prompt|reply)", re.I),
]
_HEURISTIC_TRUE = {"1", "true", "yes", "on"}
_SCRIPT_FALSE = {"0", "false", "no", "off", "pty", "none", "auto", ""}
_SCRIPT_TRUE = {"1", "true", "yes", "on", "script"}
_BRACKETED_PASTE_START = "\x1b[200~"
_BRACKETED_PASTE_END = "\x1b[201~"


def wait_heuristics_enabled() -> bool:
    """是否允许用终端输出猜等待状态。

    默认关闭：waiting_input 只由 agent 的 bosun-report 回报产生，避免正则/静默判定
    把「运行中」误标成「待输入」。BOSUN_WAIT_HEURISTICS=1 可回退到旧行为
    （代价：不装 bosun-report 的引擎不再被检测到阻塞提示）。
    """
    return os.environ.get("BOSUN_WAIT_HEURISTICS", "").strip().lower() in _HEURISTIC_TRUE


def _prompt_kind(tail: str) -> str | None:
    if any(p.search(tail) for p in _CHOICE_PROMPT_RES):
        return "choice"
    if any(p.search(tail) for p in _INPUT_PROMPT_RES):
        return "input"
    if any(p.search(tail) for p in _REVIEW_PROMPT_RES):
        return "review"
    return None


def _looks_like_prompt(tail: str) -> bool:
    return _prompt_kind(tail) is not None


def _looks_like_busy(tail: str) -> bool:
    return any(p.search(tail) for p in _BUSY_RES)


def _looks_like_idle_prompt(tail: str) -> bool:
    return not _looks_like_busy(tail) and any(p.search(tail) for p in _IDLE_PROMPT_RES)


def _blocking_prompt_kind(tail: str) -> str | None:
    """真正阻塞、需要用户决策的提示（选择/输入）；不含“回合结束待核对”的软提示。"""
    if any(p.search(tail) for p in _CHOICE_PROMPT_RES):
        return "choice"
    if any(p.search(tail) for p in _INPUT_PROMPT_RES):
        return "input"
    return None


def _looks_like_review(tail: str) -> bool:
    return any(p.search(tail) for p in _REVIEW_PROMPT_RES)


def _has_active_background_shell(tail: str) -> bool:
    return any(p.search(tail) for p in _BG_SHELL_RES)


def _looks_like_session_clear(tail: str) -> bool:
    return any(p.search(tail) for p in _SESSION_CLEAR_RES)


def _put_drop(q: "asyncio.Queue", data) -> None:
    """有界队列: 满了丢最旧再放, 防慢/死消费者撑爆内存。"""
    try:
        q.put_nowait(data)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
            q.put_nowait(data)
        except Exception:
            pass


def script_log_path_for(log_path: str) -> str:
    """Derived system `script` capture path for a task terminal log."""
    p = Path(log_path)
    suffix = p.suffix or ".log"
    return str(p.with_name(f"{p.stem}.script{suffix}"))


def remove_terminal_log_files(log_path: str) -> None:
    # 连同「压缩历史日志」产生的 .gz 归档一并删除
    for base in (log_path, script_log_path_for(log_path)):
        for path in (Path(base), Path(f"{base}.gz")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _script_mode_enabled() -> bool:
    # Off by default: system `script` nests another PTY and can prevent resize
    # propagation to full-screen CLIs. Enable only for troubleshooting captures.
    mode = os.environ.get("BOSUN_SCRIPT_LOG", "0").strip().lower()
    if mode in _SCRIPT_FALSE:
        return False
    return mode in _SCRIPT_TRUE


@lru_cache(maxsize=8)
def _detect_script_variant(script_bin: str) -> str | None:
    """Return a supported `script` flavor, or None when syntax is unknown."""
    try:
        probe = subprocess.run(
            [script_bin, "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1.0,
            check=False,
        )
    except Exception:
        probe = None
    if probe is not None and probe.returncode == 0:
        out = f"{probe.stdout}\n{probe.stderr}".lower()
        if "util-linux" in out:
            return "util-linux"

    if sys.platform == "darwin" or "bsd" in sys.platform:
        return "bsd"
    return None


def _script_argv_for_variant(
    variant: str,
    script_bin: str,
    argv: list[str],
    script_log_path: str,
) -> list[str] | None:
    if variant == "bsd":
        return [script_bin, "-q", "-e", "-F", script_log_path, *argv]
    if variant == "util-linux":
        return [script_bin, "-q", "-e", "-f", "-c", shlex.join(argv), script_log_path]
    return None


def _build_script_argv(argv: list[str], script_log_path: str) -> list[str] | None:
    if not _script_mode_enabled():
        return None
    script_bin = os.environ.get("BOSUN_SCRIPT_BIN", "script")
    resolved = shutil.which(script_bin)
    if resolved is None:
        return None
    variant = _detect_script_variant(resolved)
    if variant is None:
        return None
    return _script_argv_for_variant(variant, resolved, argv, script_log_path)


class PtySession:
    def __init__(
        self,
        task_id: int,
        argv: list[str],
        cwd: str,
        log_path: str,
        loop: asyncio.AbstractEventLoop,
        on_status: Callable[[int, str], None],
        on_exit: Callable[[int, int], None],
        post_input: str | None = None,
        initial_prompt: str | None = None,
    ):
        self.task_id = task_id
        self.argv = argv
        self.cwd = cwd
        self.log_path = log_path
        self.loop = loop
        self.post_input = post_input
        self.initial_prompt = initial_prompt
        self.on_status = on_status  # (task_id, status)
        self.on_exit = on_exit      # (task_id, exit_code)
        self.script_log_path: str | None = None

        self.proc: PtyProcess | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self.last_output = time.time()
        self._busy_until = 0.0
        self._input_in_bracketed_paste = False
        self.status = "running"
        self._reported = False  # 收到 agent 权威回调后置真，正则不得再翻转状态
        self._heuristics = wait_heuristics_enabled()
        # 记录进入 waiting_input 的来源: "prompt"(明确提示/完成标记,粘性) /
        # "idle"(静默输入提示,可恢复)
        self._wait_reason: str | None = None
        self._waiting_kind: str | None = None
        # 本次运行里检测到会话被 /clear 冲掉（粘性）：前端据此才显示「恢复会话」
        self._session_cleared = False
        self._buf = ""
        self._log_fh = None
        self._reader: threading.Thread | None = None
        self._idle_watch: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(self.log_path, "ab", buffering=0)
        from .env import task_env

        env = task_env(self.task_id)
        spawn_argv = self.argv
        script_log_path = script_log_path_for(self.log_path)
        script_argv = _build_script_argv(self.argv, script_log_path)
        if script_argv is not None:
            try:
                Path(script_log_path).unlink(missing_ok=True)
            except OSError:
                pass
            self.script_log_path = script_log_path
            spawn_argv = script_argv
        else:
            self.script_log_path = None
            try:
                Path(script_log_path).unlink(missing_ok=True)
            except OSError:
                pass
        self.proc = PtyProcess.spawn(
            spawn_argv, cwd=self.cwd, env=env, dimensions=(30, 100)
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        if self._heuristics:
            self._idle_watch = threading.Thread(target=self._idle_loop, daemon=True)
            self._idle_watch.start()
        if self.initial_prompt:
            threading.Thread(target=self._send_initial_prompt, daemon=True).start()
        if self.post_input:
            threading.Thread(target=self._send_post_input, daemon=True).start()

    @property
    def waiting_kind(self) -> str | None:
        return self._waiting_kind if self.status == "waiting_input" else None

    @property
    def session_cleared(self) -> bool:
        return self._session_cleared

    def _note_session_clear(self, tail: str) -> None:
        if not self._session_cleared and _looks_like_session_clear(tail):
            self._session_cleared = True

    def _send_post_input(self) -> None:
        # 等 TUI 起来再发(如 /compact)。启发式延时，MVP 够用。
        # 有 initial_prompt 时再多等一拍，避免和首条指令在输入框里交错。
        time.sleep(12.0 if self.initial_prompt else 5.0)
        if self.proc is not None and self.proc.isalive():
            try:
                self.proc.write(self.post_input.encode())
            except Exception:
                pass

    def _send_initial_prompt(self) -> None:
        """把首条指令投进不收位置参数 prompt 的 TUI(如 kimi)。

        多行 prompt 直接打字会被逐行提交，必须走括号粘贴；粘贴与回车之间留间隔，
        否则 TUI 可能把回车并进粘贴内容。补发的第二个回车是保险：TUI 尚未就绪吞掉
        首个回车时兜底提交，已提交时输入框为空、空回车是无操作(实测 kimi 0.34)。
        """
        time.sleep(6.0)
        if self.proc is None or not self.proc.isalive():
            return
        try:
            self.proc.write(b"\x1b[200~" + self.initial_prompt.encode() + b"\x1b[201~")
            time.sleep(1.5)
            self.proc.write(b"\r")
            time.sleep(2.0)
            if self.proc.isalive():
                self.proc.write(b"\r")
        except Exception:
            pass

    # ---- 读线程 ----
    def _read_loop(self) -> None:
        assert self.proc is not None
        while not self._stop.is_set():
            try:
                data = self.proc.read(4096)
            except EOFError:
                break
            except Exception:
                break
            if not data:
                break
            self.last_output = time.time()
            if self._log_fh:
                self._log_fh.write(data)
            self._broadcast(data)
            # 滚动文本缓冲 + 决策提示检测(claude TUI 有动画, 不能只靠静默)
            text = _ANSI_RE.sub("", data.decode(errors="replace"))
            self._buf = (self._buf + text)[-4000:]
            # 用缓冲尾部判，避免 `❯ /clear` 被拆在两段输出里而漏检
            self._note_session_clear(self._buf[-800:])
            if _looks_like_busy(text) or _looks_like_busy(self._buf[-800:]):
                self._busy_until = max(self._busy_until, time.time() + _BUSY_GRACE_SECONDS)
            self._set_status(self._next_status_for_output(self._buf[-800:]))
        self._finish()

    def _next_status_for_output(self, tail: str) -> str:
        """收到一段输出后应处的状态。

        以“活着的结构信号”为准，而非枚举 cc/codex 的俏皮结束词（worked/baked/cooked/
        churned/…，还会再加）。按优先级：
        1. `esc to interrupt` 转圈 → 正在生成 → running，并解开任何被误命中粘住的旧提示
           （治 #214：diff 里的“请选择”把状态粘成待选择、转圈也翻不回来）；
        2. 真实阻塞提示（选择/输入）→ waiting_input（即便后台有 shell，批准优先，不被吞）；
        3. `N shell still running` / 底栏 `· N shell ·` → 后台真结果还在跑 → 仍算 running
           （治后台审查/type-check 未完就被判“待复核/停”）；
        4. 回合结束分隔线（Worked for …）→ waiting_input(review)；
        5. 已因真提示等待、本段是动画/重绘 → 保持 waiting_input（只有用户输入才回 running）；
        6. 其余（正常运行 / 从静默输入提示中恢复）→ running。

        启发式关闭时（默认）整段不生效：状态维持不变，只由 mark_reported 改写。
        """
        if not self._heuristics:
            return self.status
        if self._reported:
            return self.status
        # 活跃转圈，或仍在忙碌宽限内（内容爆发时忙碌锚点可能不在这 800 字窗口，
        # 但 _busy_until 记得刚才在转圈）→ running，并解开任何被误命中粘住的旧提示。
        # 与 idle 路径共用同一 _busy_until 宽限，避免只有输出路径把干活中的 diff 文本
        # （如“请选择”）当成真提示而反复抖动。
        if _looks_like_busy(tail) or time.time() < self._busy_until:
            self._wait_reason = None
            self._waiting_kind = None
            return "running"
        kind = _blocking_prompt_kind(tail)
        if kind:
            self._wait_reason = "prompt"
            self._waiting_kind = kind
            return "waiting_input"
        if _has_active_background_shell(tail):
            self._wait_reason = None
            self._waiting_kind = None
            return "running"
        if _looks_like_review(tail):
            self._wait_reason = "prompt"
            self._waiting_kind = "review"
            return "waiting_input"
        if self.status == "waiting_input" and self._wait_reason == "prompt":
            return "waiting_input"
        self._wait_reason = None
        self._waiting_kind = None
        return "running"

    def _idle_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(1.0)
            self._maybe_set_idle_waiting()

    def _maybe_set_idle_waiting(self, now: float | None = None) -> bool:
        if not self._heuristics:
            return False
        if self.proc is None or not self.proc.isalive():
            return False
        if self._reported:
            return False
        now = now or time.time()
        if self.status != "running" or now - self.last_output <= IDLE_SECONDS:
            return False
        if now < self._busy_until:
            return False
        if _has_active_background_shell(self._buf[-800:]):
            # 回合虽结束，但后台 shell 仍在跑（审查/type-check/测试）：真结果还在产出，
            # 不能因 8 秒静默就误判空闲、催用户核对。
            return False
        if not _looks_like_idle_prompt(self._buf[-800:]):
            return False
        # 静默停在空输入栏说明本轮已不再执行，但没有强完成标记；低置信地标为
        # 待核对，有新输出时仍可翻回 running。
        self._wait_reason = "idle"
        self._waiting_kind = "review"
        self._set_status("waiting_input")
        return True

    def _finish(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        code = -1
        if self.proc is not None:
            try:
                self.proc.wait()
                code = self.proc.exitstatus if self.proc.exitstatus is not None else -1
            except Exception:
                pass
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.on_exit, self.task_id, code)

    def _set_status(self, status: str) -> None:
        if self.status == status:
            return
        self.status = status
        self.loop.call_soon_threadsafe(self.on_status, self.task_id, status)

    def mark_reported(self, status: str) -> None:
        """收到 agent 权威回调：锁定状态，令正则启发式不再翻转。"""
        self._reported = True
        self.status = status
        self._wait_reason = "reported"
        self._waiting_kind = "review" if status == "waiting_input" else None

    # ---- 订阅 / 广播 ----
    def _broadcast(self, data: bytes) -> None:
        for q in list(self.subscribers):
            self.loop.call_soon_threadsafe(_put_drop, q, data)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def read_backlog(self) -> bytes:
        return read_terminal_backlog(self.log_path)

    # ---- 写 / 控制 ----
    def _input_submits_reply(self, data: str) -> bool:
        """Return whether terminal input commits the edited reply.

        xterm streams ordinary typing one key at a time.  Only an Enter/newline outside
        bracketed-paste content means that the user has actually submitted a new turn.
        """
        in_paste = getattr(self, "_input_in_bracketed_paste", False)
        offset = 0
        submitted = False
        while offset < len(data):
            if in_paste:
                end = data.find(_BRACKETED_PASTE_END, offset)
                if end < 0:
                    offset = len(data)
                    break
                in_paste = False
                offset = end + len(_BRACKETED_PASTE_END)
                continue

            start = data.find(_BRACKETED_PASTE_START, offset)
            segment_end = len(data) if start < 0 else start
            segment = data[offset:segment_end]
            submitted = submitted or "\r" in segment or "\n" in segment
            if start < 0:
                offset = len(data)
                break
            in_paste = True
            offset = start + len(_BRACKETED_PASTE_START)

        self._input_in_bracketed_paste = in_paste
        return submitted

    def write(self, data: str) -> None:
        if self.proc is not None and self.proc.isalive():
            self.proc.write(data.encode())
            # xterm 逐键发送输入。普通字符/空格仍处于“编辑回复”阶段，不能让状态在
            # waiting_input/running 之间来回抖动并为每个按键触发一条等待通知。
            self.last_output = time.time()
            if not self._input_submits_reply(data):
                return
            # Enter 提交回复 → 恢复运行(解除等待)；给一段静默宽限，避免立刻被 idle 重新标等待
            self._wait_reason = None
            self._waiting_kind = None
            # 上一轮的提示仍在滚动检测缓冲中；不清空的话，新一轮的第一段正常
            # 输出会和旧提示一起再次命中，状态立刻反弹成 waiting_input。
            self._buf = ""
            self._reported = False  # 新一轮开始，解除权威锁，启发式重新生效
            self._set_status("running")

    def resize(self, rows: int, cols: int) -> None:
        if self.proc is not None and self.proc.isalive():
            try:
                self.proc.setwinsize(rows, cols)
            except Exception:
                pass

    def redraw(self, rows: int, cols: int) -> None:
        """Force a TUI repaint after replaying a bounded, partial terminal log."""
        temporary_rows = rows - 1 if rows > 1 else rows + 1
        self.resize(temporary_rows, cols)
        self.resize(rows, cols)

    def graceful_stop(self) -> None:
        """先发退出键(Ctrl-C 两次)让 cc/codex 落盘会话，再强杀。阻塞最多 ~3.5s。"""
        if self.proc is not None and self.proc.isalive():
            try:
                self.proc.write(b"\x03")
                time.sleep(0.3)
                self.proc.write(b"\x03")
            except Exception:
                pass
            for _ in range(20):
                if not self.proc.isalive():
                    break
                time.sleep(0.15)
        self.terminate()

    def terminate(self) -> None:
        self._stop.set()
        if self.proc is not None and self.proc.isalive():
            try:
                self.proc.terminate(force=True)
            except Exception:
                pass

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.isalive()
