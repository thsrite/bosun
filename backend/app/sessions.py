"""引擎会话文件的定位 / 读写 / 捕获。

cc:    ~/.claude/projects/<cwd 编码(/→-)>/<session-id>.jsonl
codex: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
omp:   ~/.omp/agent/sessions/abs-<目录名>-<sha256(真实路径)>/<时间戳>_<uuid>.jsonl
kimi:  ~/.kimi-code/sessions/wd_<slug>_<sha256(真实路径)[:12]>/session_<uuid>/
       (state.json 存 cwd/时间，事件流在 agents/main/wire.jsonl)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

# 装了 claude CLI 时共享用户自己的 ~/.claude；没装时内置 SDK agent（claude_agent_sdk
# 捆绑 CLI）把家目录指到 DATA_DIR 下——Bosun 不在没装 claude 的机器上凭空创建
# ~/.claude（用户反馈），也因此无须迁移登录凭证（macOS 凭证在 Keychain，不随目录走）。
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def claude_cli_installed() -> bool:
    from . import config

    binary = config.CLAUDE_BIN
    if "/" in binary:
        return Path(binary).expanduser().is_file()
    return shutil.which(binary) is not None


def claude_home() -> Path:
    """内置 SDK agent 生效的 claude 家目录。"""
    if claude_cli_installed():
        return Path.home() / ".claude"
    from . import config

    return config.DATA_DIR / "claude-home"


def claude_projects() -> Path:
    return CLAUDE_PROJECTS if claude_cli_installed() else claude_home() / "projects"


def claude_env_overrides() -> dict[str, str]:
    """SDK 派发环境补丁：没装 claude CLI 时重定向捆绑 CLI 的家目录。"""
    if claude_cli_installed():
        return {}
    return {"CLAUDE_CONFIG_DIR": str(claude_home())}
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
# omp 允许用 PI_CODING_AGENT_SESSION_DIR 改会话根目录；agent 是后端 spawn 的，
# 继承的正是这份环境，所以这里读同一个变量才能跟它对上。
_OMP_SESSIONS_ENV = "PI_CODING_AGENT_SESSION_DIR"
OMP_SESSIONS = Path.home() / ".omp" / "agent" / "sessions"


def omp_sessions_root() -> Path:
    configured = os.environ.get(_OMP_SESSIONS_ENV)
    return Path(configured).expanduser() if configured else OMP_SESSIONS


# kimi 用 KIMI_CODE_HOME 改数据根目录(默认 ~/.kimi-code)；agent 由后端 spawn、
# 继承同一份环境，读同一变量才对得上。
_KIMI_HOME_ENV = "KIMI_CODE_HOME"
KIMI_HOME = Path.home() / ".kimi-code"


def kimi_home() -> Path:
    configured = os.environ.get(_KIMI_HOME_ENV)
    return Path(configured).expanduser() if configured else KIMI_HOME

_UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def encode_cwd(cwd: str) -> str:
    """cc 的项目目录编码：路径分隔符和下划线都归一为 '-'。"""
    return str(Path(cwd).resolve()).replace("/", "-").replace("_", "-")


def cc_project_dir(cwd: str) -> Path:
    return claude_projects() / encode_cwd(cwd)


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
    pattern: str = "*.jsonl",
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
        for p in directory.glob(pattern):
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


def _snapshot_dirs(directories: list[Path], pattern: str = "*.jsonl") -> set[str]:
    """快照现存会话文件的绝对路径，用于事后比对出本次新建的那个。"""
    return {
        str(p)
        for directory in directories
        if directory.is_dir()
        for p in directory.glob(pattern)
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
    """同一 cwd 下所有可能存放会话的 omp 目录，按名字排序。

    默认布局下目录名是 `<scope>-<可读名>-<sha256(真实路径)>`，scope 由 omp 自己决定
    (实测 abs，家目录/临时目录下可能是别的前缀)。哈希只认路径，所以同一项目理论上
    可能同时存在多个前缀的桶——读取一律扫全部，避免「导入的会话看不见」或
    「新会话捕获不到」。

    另外 PI_CODING_AGENT_SESSION_DIR 被设置时，omp 可能直接把会话文件写在该目录下
    而不再分桶。两种语义都扫：文件名本身要过 _omp_uid 校验，多扫一个目录不会误认。
    """
    root = omp_sessions_root()
    if not root.is_dir():
        return []
    digest = omp_dir_digest(cwd)
    dirs = sorted((p for p in root.glob(f"*-{digest}") if p.is_dir()), key=lambda p: p.name)
    if any(_omp_uid(p) for p in root.glob("*.jsonl")):
        dirs.append(root)
    return dirs


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


def _kimi_slug(name: str) -> str:
    """kimi 会话桶目录名里的 slug 段：小写、非 [a-z0-9._-] 归并为 '-'、截 40。"""
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-")[:40].strip("-")
    return slug if slug not in ("", ".", "..") else "workspace"


def kimi_workdir_key(cwd: str) -> str:
    """kimi 的按 cwd 分桶目录名：wd_<slug(目录名)>_<sha256(真实路径)[:12]>。"""
    resolved = Path(cwd).expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:12]
    return f"wd_{_kimi_slug(resolved.name)}_{digest}"


def kimi_project_dir(cwd: str) -> Path:
    return kimi_home() / "sessions" / kimi_workdir_key(cwd)


def _kimi_uid(path: Path) -> str | None:
    """主 agent wire.jsonl 路径 → 会话 uuid(会话目录名形如 session_<uuid>)。"""
    if len(path.parents) < 3:
        return None
    uid = path.parents[2].name.removeprefix("session_")
    return uid if _UUID_RE.fullmatch(uid) else None


def kimi_wire_path(cwd: str, uid: str) -> Path:
    return kimi_project_dir(cwd) / f"session_{uid}" / "agents" / "main" / "wire.jsonl"


# 会话的权威事件流固定在主 agent 的 wire.jsonl；子 agent 各有自己的 wire，不扫。
_KIMI_WIRE_GLOB = "session_*/agents/main/wire.jsonl"


def snapshot_kimi(cwd: str) -> set[str]:
    return _snapshot_dirs([kimi_project_dir(cwd)], pattern=_KIMI_WIRE_GLOB)


def capture_kimi_session(
    cwd: str,
    before: set[str],
    since_ts: float,
    exclude_uids: set[str] | None = None,
    prompt: str | None = None,
) -> str | None:
    """返回本次运行新生成的 kimi 会话 uuid。"""
    return _capture_new_session(
        [kimi_project_dir(cwd)], before, since_ts, _kimi_uid, exclude_uids,
        engine="kimi", prompt=prompt, pattern=_KIMI_WIRE_GLOB,
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


def _kimi_session_meta(path: Path) -> dict | None:
    """kimi 主 agent wire.jsonl → 与 _session_meta 同构的元数据。

    用户回合只认 turn.prompt(origin.kind=user)：kimi 会把 system-reminder 等注入
    内容也记成 role=user 的 context.append_message，按消息扫会把注入当成用户指令。
    cwd/创建时间从同目录 state.json 读(wire 里没有 cwd)。
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    uid = _kimi_uid(path)
    if not uid:
        return None

    cwd = None
    created_at = None
    updated_at = None
    try:
        state = json.loads((path.parents[2] / "state.json").read_text(errors="replace"))
        if isinstance(state, dict):
            cwd = state.get("cwd")
            created_at = _ts(state.get("createdAt"))
            updated_at = _ts(state.get("updatedAt"))
    except (OSError, json.JSONDecodeError):
        pass

    first_user = ""
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
                if not isinstance(obj, dict):
                    continue
                t = _ts(obj.get("time"))
                if t is not None:
                    created_at = t if created_at is None else min(created_at, t)
                    updated_at = t if updated_at is None else max(updated_at, t)
                if obj.get("type") != "turn.prompt":
                    continue
                origin = obj.get("origin")
                if isinstance(origin, dict) and origin.get("kind") not in (None, "user"):
                    continue
                text = _clean_prompt(_text_from_content(obj.get("input")))
                if text:
                    turns += 1
                    if not first_user:
                        first_user = text
    except OSError:
        return None

    return {
        "engine": "kimi",
        "session_uid": uid,
        "title": _short_title(first_user, f"kimi {uid[:8]}"),
        "prompt": first_user,
        "path": str(path),
        "cwd": cwd,
        "created_at": created_at,
        "updated_at": updated_at or stat.st_mtime,
        "size": stat.st_size,
        "turns": turns,
    }


def _session_meta(path: Path, engine: str, project_path: str | None = None) -> dict | None:
    """Read bounded transcript metadata for local-session discovery."""
    if engine == "kimi":
        return _kimi_session_meta(path)
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
    if engine == "kimi":
        path = kimi_wire_path(cwd, uid)
        if not path.exists():
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

    kimi_files = list(kimi_project_dir(cwd).glob(_KIMI_WIRE_GLOB)) if kimi_project_dir(cwd).is_dir() else []
    for path in sorted(kimi_files, key=_mtime, reverse=True)[:limit]:
        meta = _session_meta(path, "kimi", cwd)
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
    if engine == "kimi":
        return kimi_wire_path(cwd, uid)
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

    # kimi: 用户回合是 turn.prompt；助手正文在 context.append_loop_event 的
    # content.part 里按 step 分片，攒到 step.end/turn.ended 再落成一条消息。
    kimi_parts: list[str] = []
    kimi_time = None

    def flush_kimi() -> None:
        nonlocal kimi_parts, kimi_time
        if kimi_parts:
            append("assistant", "\n".join(kimi_parts), kimi_time)
        kimi_parts = []
        kimi_time = None

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
                if engine == "kimi":
                    kind = obj.get("type")
                    if kind == "turn.prompt":
                        flush_kimi()
                        origin = obj.get("origin")
                        if not isinstance(origin, dict) or origin.get("kind") in (None, "user"):
                            append("user", _history_content_text(obj.get("input")), obj.get("time"))
                        continue
                    if kind == "context.append_loop_event":
                        event = obj.get("event")
                        if not isinstance(event, dict):
                            continue
                        if event.get("type") == "content.part":
                            part = event.get("part")
                            if isinstance(part, dict) and part.get("type") == "text":
                                text = part.get("text")
                                if isinstance(text, str) and text.strip():
                                    kimi_parts.append(text.strip())
                                    kimi_time = kimi_time or obj.get("time")
                        elif event.get("type") == "step.end":
                            flush_kimi()
                    continue
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
        flush_kimi()
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
    """input+output 口径。cc/codex 的键是 *_tokens，omp 用 input/output，
    kimi(usage.record) 用 inputOther/output——与 cc 一致不含 cache 读写。

    kimi 与 omp 共用 output 键，泛化元组会部分匹配漏掉输入侧，
    所以 kimi 按特征键 inputOther 先行判定。
    """
    if isinstance(usage.get("inputOther"), int):
        out = usage.get("output")
        return usage["inputOther"] + (out if isinstance(out, int) else 0)
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
        # kimi wire 事件的时间键是 time(毫秒)，其余引擎是 timestamp
        event_ts = _ts(
            obj.get("timestamp")
            or obj.get("time")
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
                # kimi wire 事件的时间键是 time(毫秒)，其余引擎是 timestamp
                event_ts = _ts(
                    obj.get("timestamp")
                    or obj.get("time")
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


def _write_kimi_session(cwd: str, uid: str, content: str) -> Path:
    """把分享来的 kimi 主 agent wire.jsonl 重建为完整会话。

    kimi 的会话不是单文件：resume 还需要 state.json(cwd/agents 元数据)和
    session_index.jsonl 里的登记行，缺一 -S 都找不到会话。cwd 直接写目标项目
    路径，等价于 omp 导入时的 reroot。
    """
    session_dir = kimi_project_dir(cwd) / f"session_{uid}"
    wire = session_dir / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True, exist_ok=True)
    wire.write_text(content)
    now_ms = int(time.time() * 1000)
    target = str(Path(cwd).expanduser().resolve())
    state = {
        "id": f"session_{uid}",
        "version": 2,
        "cwd": target,
        "createdAt": now_ms,
        "updatedAt": now_ms,
        "archived": False,
        "agents": {"main": {"homedir": str(wire.parent), "type": "main"}},
        "custom": {},
    }
    (session_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    entry = {"sessionId": f"session_{uid}", "sessionDir": str(session_dir), "workDir": target}
    with (kimi_home() / "session_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return wire


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
    elif engine == "kimi":
        return _write_kimi_session(cwd, uid, content)
    else:
        # 放到今天的日期目录，文件名带上 uuid 供 codex 按 id 检索
        day = time.strftime("%Y/%m/%d")
        ts = time.strftime("%Y-%m-%dT%H-%M-%S")
        p = CODEX_SESSIONS / day / f"rollout-{ts}-{uid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p
