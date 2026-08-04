"""引擎会话文件的定位 / 读写 / 捕获。

cc:    ~/.claude/projects/<cwd 编码(/→-)>/<session-id>.jsonl
codex: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
omp:   ~/.omp/agent/sessions/abs-<目录名>-<sha256(真实路径)>/<时间戳>_<uuid>.jsonl
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
# omp 允许用 PI_CODING_AGENT_SESSION_DIR 改会话根目录；agent 是后端 spawn 的，
# 继承的正是这份环境，所以这里读同一个变量才能跟它对上。
_OMP_SESSIONS_ENV = "PI_CODING_AGENT_SESSION_DIR"
OMP_SESSIONS = Path.home() / ".omp" / "agent" / "sessions"


def omp_sessions_root() -> Path:
    configured = os.environ.get(_OMP_SESSIONS_ENV)
    return Path(configured).expanduser() if configured else OMP_SESSIONS

_UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def encode_cwd(cwd: str) -> str:
    """cc 的项目目录编码：路径分隔符和下划线都归一为 '-'。"""
    return str(Path(cwd).resolve()).replace("/", "-").replace("_", "-")


def cc_project_dir(cwd: str) -> Path:
    return CLAUDE_PROJECTS / encode_cwd(cwd)


def cc_session_path(cwd: str, uid: str) -> Path:
    return cc_project_dir(cwd) / f"{uid}.jsonl"


def snapshot_cc(cwd: str) -> set[str]:
    return _snapshot_dirs([cc_project_dir(cwd)])


def _capture_new_session(
    directories: list[Path],
    before: set[str],
    since_ts: float,
    uid_of: Callable[[Path], str | None],
    exclude_uids: set[str] | None = None,
    engine: str | None = None,
    prompt: str | None = None,
) -> str | None:
    """在按 cwd 隔离的会话目录里，取本次运行新出现的最新会话 uid。

    cc 和 omp 都是「一个项目一个目录」，跨项目不会串号。但**同一项目并发跑两个任务**
    时，两边都可能在任一文件落盘前完成 snapshot：
    - 排掉已被别的任务认领的 uid(exclude_uids)防重复认领；
    - 只排重还不够——两个任务仍可能各自认领对方的会话(互换)，所以给了 prompt 时
      还要核对 transcript 的首条用户指令，和 capture_codex_session 同一套判据。
    """
    excluded = exclude_uids or set()
    expected_prompt = _clean_prompt(prompt) if prompt is not None else None
    best: tuple[float, str] | None = None  # (与启动时刻的距离, uid)
    for directory in directories:
        if not directory.is_dir():
            continue
        for p in directory.glob("*.jsonl"):
            if str(p) in before:
                continue  # 只认新文件，排除已存在(如其它会话)
            uid = uid_of(p)
            if uid is None or uid in excluded:
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime < since_ts - 2:
                continue
            created = mtime
            if engine is not None:
                meta = _session_meta(p, engine)
                if expected_prompt is not None:
                    # 首条指令还没落盘时 meta 为空/无 prompt：这轮先不认领，下轮再看，
                    # 不能抢一个还不知道属于谁的会话。
                    if meta is None or meta.get("prompt") != expected_prompt:
                        continue
                if meta is not None and meta.get("created_at"):
                    created = meta["created_at"]
            # 并发任务的 prompt 可能完全相同(比对分不开)，此时按「创建时刻最贴近本次
            # 启动」归属。必须用 transcript 自己记录的创建时间：mtime 会随会话继续
            # 输出而后移，先起的任务反而会显得更贴近后起任务的启动时刻，导致互换。
            distance = abs(created - since_ts)
            if best is None or distance < best[0]:
                best = (distance, uid)
    return best[1] if best else None


def _snapshot_dirs(directories: list[Path]) -> set[str]:
    """快照现存会话文件的绝对路径，用于事后比对出本次新建的那个。"""
    return {
        str(p)
        for directory in directories
        if directory.is_dir()
        for p in directory.glob("*.jsonl")
    }


def capture_cc_session(
    cwd: str,
    before: set[str],
    since_ts: float,
    exclude_uids: set[str] | None = None,
    prompt: str | None = None,
) -> str | None:
    """返回本次运行新生成的 cc 会话 uuid(项目目录里新出现的 <uuid>.jsonl)。"""
    return _capture_new_session(
        [cc_project_dir(cwd)], before, since_ts, lambda p: p.stem, exclude_uids,
        engine="cc", prompt=prompt,
    )


def _codex_rollouts() -> list[Path]:
    if not CODEX_SESSIONS.is_dir():
        return []
    return list(CODEX_SESSIONS.rglob("rollout-*.jsonl"))


def codex_session_path(uid: str) -> Path | None:
    for p in _codex_rollouts():
        if uid in p.name:
            return p
    return None


def omp_dir_digest(cwd: str) -> str:
    """omp 会话目录名里的哈希：sha256(解析后的真实路径)。"""
    return hashlib.sha256(str(Path(cwd).expanduser().resolve()).encode()).hexdigest()


def omp_project_dirs(cwd: str) -> list[Path]:
    """同一 cwd 下所有已存在的 omp 会话目录，按名字排序。

    目录名是 `<scope>-<可读名>-<sha256(真实路径)>`，scope 由 omp 自己决定(实测 abs，
    家目录/临时目录下可能是别的前缀)。哈希只认路径，所以同一项目理论上可能同时存在
    多个前缀的桶——读取一律扫全部，避免「导入的会话看不见」或「新会话捕获不到」。
    """
    digest = omp_dir_digest(cwd)
    root = omp_sessions_root()
    if not root.is_dir():
        return []
    return sorted((p for p in root.glob(f"*-{digest}") if p.is_dir()), key=lambda p: p.name)


def omp_project_dir(cwd: str) -> Path:
    """写入用的单一目标目录：优先复用 omp 已建好的桶，没有才按 abs 约定新建。"""
    existing = omp_project_dirs(cwd)
    if existing:
        return existing[0]
    resolved = Path(cwd).expanduser().resolve()
    return omp_sessions_root() / f"abs-{resolved.name}-{omp_dir_digest(cwd)}"


def _omp_uid(path: Path) -> str | None:
    """omp 会话文件名形如 <时间戳>_<uuid>.jsonl。"""
    uid = path.stem.rsplit("_", 1)[-1]
    return uid if _UUID_RE.fullmatch(uid) else None


def omp_session_path(cwd: str, uid: str) -> Path | None:
    for d in omp_project_dirs(cwd):
        found = next(iter(sorted(d.glob(f"*_{uid}.jsonl"))), None)
        if found is not None:
            return found
    return None


def snapshot_omp(cwd: str) -> set[str]:
    return _snapshot_dirs(omp_project_dirs(cwd))


def capture_omp_session(
    cwd: str,
    before: set[str],
    since_ts: float,
    exclude_uids: set[str] | None = None,
    prompt: str | None = None,
) -> str | None:
    """返回本次运行新生成的 omp 会话 uuid。"""
    return _capture_new_session(
        omp_project_dirs(cwd), before, since_ts, _omp_uid, exclude_uids,
        engine="omp", prompt=prompt,
    )


def _ts(value) -> float | None:
    if isinstance(value, (int, float)):
        # omp 的消息时间戳是毫秒 epoch；cc/codex 是秒。1e11 秒 ≈ 公元 5138 年，
        # 超过就只可能是毫秒。
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        return text if isinstance(text, str) else ""
    return ""


# Codex 首个用户回合不是用户 prompt，而是引擎塞进来的上下文：项目里有
# agent-rules-sync 托管的 AGENTS.md 时是 `# AGENTS.md instructions` 指令块，
# 之后跟 `<environment_context>`。真正的 prompt 落在下一条 user 消息。这些前缀
# 都要判空，否则按「首条用户消息==prompt」认领会话会永远比对不上(会话未捕获)。
_INJECTED_PROMPT_PREFIXES = ("<environment_context>", "# AGENTS.md instructions")


def _clean_prompt(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if text.startswith(_INJECTED_PROMPT_PREFIXES):
        return ""
    return text[:500]


def _short_title(text: str, fallback: str) -> str:
    line = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
    line = re.sub(r"\s+", " ", line)
    if not line:
        return fallback
    return line[:60] + ("…" if len(line) > 60 else "")


def _same_or_child(cwd: str | None, project_path: str) -> bool:
    if not cwd:
        return False
    try:
        current = Path(cwd).expanduser().resolve()
        project = Path(project_path).expanduser().resolve()
        return current == project or project in current.parents
    except (OSError, RuntimeError):
        return False


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _session_meta(path: Path, engine: str, project_path: str | None = None) -> dict | None:
    """Read bounded transcript metadata for local-session discovery."""
    try:
        stat = path.stat()
    except OSError:
        return None

    uid = path.stem if engine == "cc" else None
    if engine == "codex":
        m = _UUID_RE.search(path.name)
        if not m:
            return None
        uid = m.group(1)
    elif engine == "omp":
        uid = _omp_uid(path)
    if not uid or not _UUID_RE.fullmatch(uid):
        return None

    cwd = None
    first_user = ""
    summary = ""
    created_at = None
    last_at = None
    turns = 0
    line_count = 0
    try:
        with path.open(errors="replace") as f:
            for line in f:
                line_count += 1
                if line_count > 4000:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
                if not isinstance(payload, dict):
                    continue
                cwd = cwd or payload.get("cwd") or obj.get("cwd")
                t = _ts(obj.get("timestamp") or payload.get("timestamp"))
                if t is not None:
                    created_at = t if created_at is None else min(created_at, t)
                    last_at = t if last_at is None else max(last_at, t)
                if not summary and isinstance(payload.get("summary"), str):
                    summary = payload["summary"].strip()
                msg = payload.get("message")
                role = payload.get("role")
                content = payload.get("content")
                if isinstance(msg, dict):
                    role = msg.get("role", role)
                    content = msg.get("content", content)
                if role == "user":
                    text = _clean_prompt(_text_from_content(content))
                    if text:
                        turns += 1
                        if not first_user:
                            first_user = text
    except OSError:
        return None

    if engine == "codex" and project_path and not _same_or_child(cwd, project_path):
        return None

    prompt = first_user or summary
    return {
        "engine": engine,
        "session_uid": uid,
        "title": _short_title(prompt, f"{engine} {uid[:8]}"),
        "prompt": prompt,
        "path": str(path),
        "cwd": cwd,
        "created_at": created_at,
        "updated_at": last_at or stat.st_mtime,
        "size": stat.st_size,
        "turns": turns,
    }


def local_session_info(engine: str, cwd: str, uid: str) -> dict | None:
    if not _UUID_RE.fullmatch(uid or ""):
        return None
    if engine == "cc":
        path = cc_session_path(cwd, uid)
        if not path.exists():
            return None
        return _session_meta(path, engine, cwd)
    if engine == "codex":
        path = codex_session_path(uid)
        if path is None or not path.exists():
            return None
        return _session_meta(path, engine, cwd)
    if engine == "omp":
        path = omp_session_path(cwd, uid)
        if path is None or not path.exists():
            return None
        return _session_meta(path, engine, cwd)
    return None


def discover_local_sessions(cwd: str, limit: int = 50) -> list[dict]:
    """Discover resumable local cc/codex/omp transcript files for a project path."""
    limit = max(1, min(int(limit or 50), 200))
    found: list[dict] = []

    cc_dir = cc_project_dir(cwd)
    if cc_dir.is_dir():
        cc_files = sorted(cc_dir.glob("*.jsonl"), key=_mtime, reverse=True)
        for path in cc_files[:limit]:
            meta = _session_meta(path, "cc", cwd)
            if meta:
                found.append(meta)

    codex_files = sorted(
        _codex_rollouts(),
        key=_mtime,
        reverse=True,
    )
    for path in codex_files[: max(limit * 10, 200)]:
        meta = _session_meta(path, "codex", cwd)
        if meta:
            found.append(meta)
            if len(found) >= limit * 2:
                break

    omp_files = [p for d in omp_project_dirs(cwd) for p in d.glob("*.jsonl")]
    for path in sorted(omp_files, key=_mtime, reverse=True)[:limit]:
        meta = _session_meta(path, "omp", cwd)
        if meta:
            found.append(meta)

    found.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return found[:limit]


def snapshot_codex() -> set[str]:
    """记录当前所有 codex rollout 文件名，用于事后对比出新会话。"""
    return {p.name for p in _codex_rollouts()}


def capture_codex_session(
    before: set[str],
    since_ts: float,
    cwd: str | None = None,
    prompt: str | None = None,
    exclude_uids: set[str] | None = None,
) -> str | None:
    """返回本次 Codex 运行新建的 rollout uuid。

    Codex 的 transcript 目录是全局共享的。只按 mtime 取最新文件时，并发运行的
    多个 Codex 会互相串号，所以还要核对 rollout 内记录的 cwd 和首条用户指令。
    """
    newest: tuple[float, str] | None = None
    expected_prompt = _clean_prompt(prompt or "") if prompt is not None else None
    excluded = exclude_uids or set()
    for p in _codex_rollouts():
        if p.name in before:
            # 首次运行一定创建新 rollout；其它 Codex 正在更新的旧会话不能算作候选。
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < since_ts - 2:
            continue
        m = _UUID_RE.search(p.name)
        if not m:
            continue
        uid = m.group(1)
        if uid in excluded:
            continue
        if cwd is not None or expected_prompt is not None:
            meta = _session_meta(p, "codex")
            if meta is None:
                continue
            if cwd is not None and not _same_or_child(meta.get("cwd"), cwd):
                continue
            if expected_prompt is not None and meta.get("prompt") != expected_prompt:
                continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, uid)
    return newest[1] if newest else None


def _session_path(engine: str, cwd: str, uid: str) -> Path | None:
    if engine == "cc":
        return cc_session_path(cwd, uid)
    if engine == "omp":
        return omp_session_path(cwd, uid)
    return codex_session_path(uid)


# ---- 分享：读/写会话文件 ----
def read_session(engine: str, cwd: str, uid: str) -> str | None:
    p = _session_path(engine, cwd, uid)
    if p is None or not p.exists():
        return None
    try:
        return p.read_text(errors="replace")
    except OSError:
        return None


def _history_content_text(content) -> str:
    """Extract only human-readable chat text, excluding tool payloads and reasoning."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {None, "text", "input_text", "output_text"}:
            continue
        text = item.get("text") or item.get("content")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts).strip()


def session_history(
    engine: str,
    cwd: str,
    uid: str,
    *,
    max_messages: int = 300,
    max_chars: int = 500_000,
) -> dict:
    """Return a bounded, responsive transcript for completed or resumable CLI sessions.

    Raw PTY logs reproduce the final TUI screen, including a final clear-screen on
    Codex exit. The engine JSONL is the durable source for readable conversation
    history and also exists before a queued resume task starts a new PTY.
    """
    path = _session_path(engine, cwd, uid)
    if path is None or not path.exists():
        return {"messages": [], "truncated": False}

    messages: list[dict] = []

    def append(role, text, timestamp=None) -> None:
        if role not in {"user", "assistant"} or not isinstance(text, str):
            return
        text = text.strip()
        if not text or text.startswith("<environment_context>"):
            return
        # Codex records the same visible message as response_item + event_msg.
        if messages and messages[-1]["role"] == role and messages[-1]["text"] == text:
            return
        messages.append({"role": role, "text": text, "timestamp": timestamp})

    try:
        with path.open(errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                timestamp = obj.get("timestamp")
                if engine == "cc":
                    if obj.get("type") not in {"user", "assistant"}:
                        continue
                    msg = obj.get("message")
                    if not isinstance(msg, dict):
                        continue
                    append(msg.get("role") or obj.get("type"), _history_content_text(msg.get("content")), timestamp)
                    continue

                if engine == "omp":
                    # omp: {"type":"message","message":{"role":...,"content":[...]}}
                    if obj.get("type") != "message":
                        continue
                    msg = obj.get("message")
                    if not isinstance(msg, dict):
                        continue
                    append(msg.get("role"), _history_content_text(msg.get("content")), timestamp)
                    continue

                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                payload_type = payload.get("type")
                if obj.get("type") == "event_msg" and payload_type in {"user_message", "agent_message"}:
                    role = "user" if payload_type == "user_message" else "assistant"
                    append(role, payload.get("message"), timestamp)
                elif obj.get("type") == "response_item" and payload_type == "message":
                    append(payload.get("role"), _history_content_text(payload.get("content")), timestamp)
    except OSError:
        return {"messages": [], "truncated": False}

    truncated = len(messages) > max_messages
    if truncated:
        messages = messages[-max_messages:]
    total = sum(len(item["text"]) for item in messages)
    while len(messages) > 1 and total > max_chars:
        total -= len(messages.pop(0)["text"])
        truncated = True
    if messages and len(messages[0]["text"]) > max_chars:
        messages[0]["text"] = "…\n" + messages[0]["text"][-max_chars:]
        truncated = True
    return {"messages": messages, "truncated": truncated}


def _usage_tokens(usage: dict) -> int | None:
    """input+output 口径。cc/codex 的键是 *_tokens，omp 用的是 input/output。"""
    for keys in (("input_tokens", "output_tokens"), ("input", "output")):
        parts = [usage[k] for k in keys if isinstance(usage.get(k), int)]
        if parts:
            return sum(parts)
    for k in ("total_tokens", "totalTokens"):
        v = usage.get(k)
        if isinstance(v, int):
            return v
    return None


class LiveTokenCounter:
    """运行中会话的增量 token 统计。

    调度器每 15s 刷新一次运行中任务的用量。直接调 count_tokens 会把持续增长的整份
    transcript(实测可达上百 MB)重新读入并逐行 JSON 解析——会话跑得越久，单次刷新的
    瞬时分配越大，是本服务 RSS 随运行时长阶梯上涨的主因(macOS 分配器不会把峰值归还
    OS)。这里为每个任务记住已解析到的字节偏移，每次只解析新增部分，把单次刷新的峰值
    从"整份文件"降到"两次刷新间的新增输出"。

    语义对齐 count_tokens(since=<started_at>, until=None) 的有界统计，只是改为流式累加。
    实时值即使近似，任务结束时 _finalize_tokens 会用权威的 count_tokens 覆盖。
    """

    def __init__(self) -> None:
        # task_id -> {uid, offset, found, cc_total, codex_before, codex_in}
        self._state: dict[int, dict] = {}

    def retain(self, live_ids: set[int]) -> None:
        """丢弃已不在运行的任务状态，避免状态字典自身无界增长。"""
        for tid in [t for t in self._state if t not in live_ids]:
            self._state.pop(tid, None)

    def reset(self, task_id: int) -> None:
        self._state.pop(task_id, None)

    def update(self, task_id: int, engine: str, cwd: str, uid: str, since: float | None) -> int | None:
        p = _session_path(engine, cwd, uid)
        if p is None or not p.exists():
            return None
        try:
            size = p.stat().st_size
        except OSError:
            return None
        st = self._state.get(task_id)
        # 首次、换了会话文件(resume 新会话)、或文件被截断 → 从头重算
        if st is None or st.get("uid") != uid or size < st["offset"]:
            st = {"uid": uid, "offset": 0, "found": False, "cc_total": 0,
                  "codex_before": 0, "codex_in": None}
            self._state[task_id] = st
        if size > st["offset"]:
            try:
                with open(p, "rb") as f:
                    f.seek(st["offset"])
                    chunk = f.read()
            except OSError:
                return self._value(st)
            # 只解析完整行；末尾半行留到下次(偏移只推进到最后一个换行)
            consumed = chunk.rfind(b"\n") + 1
            if consumed > 0:
                st["offset"] += consumed
                for raw in chunk[:consumed].decode("utf-8", "replace").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self._accumulate(engine, obj, st, since)
        return self._value(st)

    @staticmethod
    def _accumulate(engine: str, obj: dict, st: dict, since: float | None) -> None:
        payload = obj.get("payload")
        event_ts = _ts(
            obj.get("timestamp")
            or (payload.get("timestamp") if isinstance(payload, dict) else None)
        )
        in_window = since is None or (event_ts is not None and event_ts >= since)
        usage = None
        msg = obj.get("message")
        if isinstance(msg, dict):
            usage = msg.get("usage")
        if usage is None:
            usage = obj.get("usage")
        if isinstance(usage, dict):
            toks = _usage_tokens(usage)
            if toks is not None and in_window:
                st["cc_total"] += toks
                st["found"] = True
        # codex TUI: payload.info.total_token_usage 是累计值，取窗口内最后一个
        if engine == "codex" and isinstance(payload, dict) and payload.get("type") == "token_count":
            info = payload.get("info")
            u = info.get("total_token_usage") if isinstance(info, dict) else None
            if isinstance(u, dict) and event_ts is not None:
                toks = _usage_tokens(u)
                if toks is not None:
                    if since is not None and event_ts < since:
                        st["codex_before"] = toks
                    else:
                        st["codex_in"] = toks
                        st["found"] = True

    @staticmethod
    def _value(st: dict) -> int | None:
        if st["codex_in"] is not None:
            return max(0, st["codex_in"] - st["codex_before"])
        return st["cc_total"] if st["found"] else None


def count_tokens(
    engine: str,
    cwd: str,
    uid: str,
    *,
    since: float | None = None,
    until: float | None = None,
) -> int | None:
    """从会话 transcript 汇总 token 用量(input+output)。会话未落盘则返回 None。

    cc / omp 的 usage 是逐条消息的增量，直接累加；codex 的 token_count 是累计值，
    走下面的窗口差值分支。
    """
    p = _session_path(engine, cwd, uid)
    if p is None or not p.exists():
        return None

    usage_tokens = _usage_tokens

    def in_window(ts: float | None) -> bool:
        if since is None and until is None:
            return True
        if ts is None:
            return False
        if since is not None and ts < since:
            return False
        if until is not None and ts > until:
            return False
        return True

    total = 0
    found = False
    codex_cumulative: int | None = None
    codex_before_window: int | None = None
    codex_in_window: int | None = None
    bounded = since is not None or until is not None
    try:
        with open(p, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # cc: {message:{usage:{input_tokens,output_tokens,...}}}
                payload = obj.get("payload")
                event_ts = _ts(
                    obj.get("timestamp")
                    or (payload.get("timestamp") if isinstance(payload, dict) else None)
                )
                usage = None
                msg = obj.get("message")
                if isinstance(msg, dict):
                    usage = msg.get("usage")
                if usage is None:
                    usage = obj.get("usage")
                if isinstance(usage, dict):
                    toks = usage_tokens(usage)
                    if toks is not None and in_window(event_ts):
                        total += toks
                        found = True
                # codex TUI transcript: token_count events carry cumulative
                # session usage under payload.info.total_token_usage.
                if engine == "codex" and isinstance(payload, dict) and payload.get("type") == "token_count":
                    info = payload.get("info")
                    usage = info.get("total_token_usage") if isinstance(info, dict) else None
                    if isinstance(usage, dict):
                        toks = usage_tokens(usage)
                        if toks is not None:
                            if not bounded:
                                codex_cumulative = toks
                                found = True
                            elif event_ts is not None:
                                if since is not None and event_ts < since:
                                    codex_before_window = toks
                                elif until is None or event_ts <= until:
                                    codex_in_window = toks
                                    found = True
    except OSError:
        return None
    if bounded and codex_in_window is not None:
        return max(0, codex_in_window - (codex_before_window or 0))
    if codex_cumulative is not None:
        return codex_cumulative
    return total if found else None


def _reroot_omp_content(content: str, cwd: str) -> str:
    """把导入会话头部记录的 cwd 改写成目标项目路径。

    omp 的会话头带着原始 cwd。原样落到目标项目的目录里，头里却指向源仓库，
    resume 时可能切回源仓库继续改代码(或卡在 omp 的重定位询问上)。
    """
    target = str(Path(cwd).expanduser().resolve())
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "session" and "cwd" in obj:
            obj["cwd"] = target
            lines[i] = json.dumps(obj, ensure_ascii=False)
            break
    return "\n".join(lines) + "\n"


def write_session(engine: str, cwd: str, uid: str, content: str) -> Path:
    """把分享来的会话写入本机对应位置，供 resume 加载。"""
    # 安全: uid 必须是合法 UUID，防止路径穿越(如 ../../.zshenv)写任意文件
    if not _UUID_RE.fullmatch(uid or ""):
        raise ValueError(f"非法 session_uid: {uid!r}")
    if engine == "cc":
        p = cc_session_path(cwd, uid)
    elif engine == "omp":
        # omp 按 cwd 分目录，文件名 <时间戳>_<uuid> 供 --resume 按 id 前缀检索
        ts = time.strftime("%Y-%m-%dT%H-%M-%S-000Z")
        p = omp_project_dir(cwd) / f"{ts}_{uid}.jsonl"
        content = _reroot_omp_content(content, cwd)
    else:
        # 放到今天的日期目录，文件名带上 uuid 供 codex 按 id 检索
        day = time.strftime("%Y/%m/%d")
        ts = time.strftime("%Y-%m-%dT%H-%M-%S")
        p = CODEX_SESSIONS / day / f"rollout-{ts}-{uid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p
