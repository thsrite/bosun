"""整体分析：采集问题 → 写入 finding 表。人在环，只发现不修。

来源：
- 客观信号：project_config 里配置的 test_cmd / build_cmd / lint_cmd，非零退出 = 问题
- git：未提交改动
- todo：代码里的 TODO / FIXME
- deps：pip-audit / npm audit（best-effort）
- audit：claude/codex 通读代码返回结构化 findings（headless）
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .. import db, events
from ..engines import build_audit_argv

TIMEOUT = 120


import threading as _threading

_tls = _threading.local()  # 线程局部: 并发 analyze() 各自的 origin 不互相污染


def _origin() -> str:
    return getattr(_tls, "origin", "manual")


def _mute_threshold() -> int:
    try:
        return int(db.get_setting("mute_threshold", 3))
    except (TypeError, ValueError):
        return 3


def _add_finding(project_id: int, source: str, severity: str, title: str, detail: str = "") -> None:
    # 去重: 收件箱(open)/已忽略(dismissed)/已降级(muted) 已有同签名 → 不重复添加
    dup = db.query_one(
        "SELECT id FROM finding WHERE project_id=? AND source=? AND title=? "
        "AND status IN ('open','dismissed','muted')",
        (project_id, source, title),
    )
    if dup:
        return
    # 失败记忆: 若同签名曾被修过(task_created/fixed)又复现 → 累计失败次数
    reappeared = db.query_one(
        "SELECT id FROM finding WHERE project_id=? AND source=? AND title=? "
        "AND status IN ('task_created','fixed')",
        (project_id, source, title),
    )
    mem = db.query_one(
        "SELECT attempts, muted FROM fix_memory WHERE project_id=? AND source=? AND title=?",
        (project_id, source, title),
    )
    attempts = (mem["attempts"] if mem else 0) + (1 if reappeared else 0)
    muted = bool(mem and mem["muted"]) or attempts >= _mute_threshold()
    if reappeared or muted:
        db.execute(
            "INSERT INTO fix_memory(project_id,source,title,attempts,muted,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(project_id,source,title) DO UPDATE SET attempts=?, muted=?, updated_at=?",
            (project_id, source, title, attempts, int(muted), time.time(), attempts, int(muted), time.time()),
        )
    status = "muted" if muted else "open"
    db.execute(
        "INSERT INTO finding(project_id,source,severity,title,detail,status,origin,created_at) "
        "VALUES(?,?,?,?,?,?, ?, ?)",
        (project_id, source, severity, title, detail, status, _origin(), time.time()),
    )


def _run(cmd: str, cwd: str) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=TIMEOUT
        )
        return p.returncode, (p.stdout + p.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        return -1, "(timeout)"
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def _objective(project_id: int, path: str, cfg) -> None:
    for source, cmd in (("test", cfg["test_cmd"]), ("build", cfg["build_cmd"]), ("lint", cfg["lint_cmd"])):
        if not cmd:
            continue
        code, out = _run(cmd, path)
        if code != 0:
            _add_finding(project_id, source, "error", f"{source} 失败 (exit {code}): {cmd}", out)


def _git(project_id: int, path: str) -> None:
    code, out = _run("git status --porcelain", path)
    if code == 0 and out.strip():
        n = len(out.strip().splitlines())
        _add_finding(project_id, "git", "info", f"{n} 处未提交改动", out)


# 非 git 项目的退路: 生成物/第三方目录黑名单(git 项目走 git grep, 天然排除已忽略文件)
_EXCLUDE_DIRS = (
    "node_modules", ".venv", "venv", "env", ".git", "dist", "dev-dist", "build",
    "out", "__pycache__", ".next", ".nuxt", ".svelte-kit", ".output", ".turbo",
    "target", "vendor", "coverage", ".mypy_cache", ".pytest_cache", ".idea",
    ".cache", ".claude",
)
_EXCLUDE_FLAGS = " ".join(f"--exclude-dir={d}" for d in _EXCLUDE_DIRS)
_TODO_INCLUDES = "'*.py' '*.ts' '*.tsx' '*.js' '*.go'"


def _todo(project_id: int, path: str) -> None:
    # 只扫 git 跟踪的源码: node_modules/dist/dev-dist/.venv/worktrees 等未跟踪或被忽略的天然排除
    if (Path(path) / ".git").exists():
        cmd = f"git grep -nE 'TODO|FIXME' -- {_TODO_INCLUDES} | head -50"
    else:  # 非 git 项目退回 grep + 目录黑名单
        includes = " ".join(f"--include={g}" for g in _TODO_INCLUDES.split())
        cmd = f"grep -rnE 'TODO|FIXME' {includes} {_EXCLUDE_FLAGS} . | head -50"
    _, out = _run(cmd, path)
    if out.strip():
        n = len(out.strip().splitlines())
        _add_finding(project_id, "todo", "info", f"{n} 处 TODO/FIXME", out)


def _deps(project_id: int, path: str) -> None:
    p = Path(path)
    if (p / "requirements.txt").exists() or (p / "pyproject.toml").exists():
        code, out = _run("pip-audit -f json 2>/dev/null || echo skip", path)
        if code == 0 and out.strip() and out.strip() != "skip":
            _add_finding(project_id, "deps", "warning", "pip-audit 依赖审计结果", out)
    if (p / "package.json").exists():
        code, out = _run("npm audit --json 2>/dev/null || echo skip", path)
        if code == 0 and out.strip() and out.strip() != "skip":
            _add_finding(project_id, "deps", "warning", "npm audit 依赖审计结果", out)


_JSON_TAIL = (
    "只返回一个 JSON 数组，每项 {\"severity\":\"error|warning|info\",\"title\":\"..\",\"detail\":\"..\"}，"
    "不要输出任何其它文字。最多 10 条。"
)
_SCOPE_HINT = (
    "只审查项目自身能修改的源码；忽略 node_modules、构建产物(dist/dev-dist/build/out)、"
    ".venv、.claude/worktrees 等第三方/生成物/副本目录。"
)
AUDIT_PROMPT = "审查这个仓库，找出真实的 bug、安全隐患、明显坏味。" + _SCOPE_HINT + _JSON_TAIL


def _scope_diff(path: str, scope: str, scope_arg: str | None) -> str | None:
    """返回要审查的 diff 文本；full 返回 None(审全仓库)。"""
    def run_full(cmd: str) -> str:
        try:
            p = subprocess.run(cmd, cwd=path, shell=True, capture_output=True, text=True, timeout=30)
            return p.stdout
        except Exception:  # noqa: BLE001
            return ""

    if scope == "recent":
        try:
            n = int(scope_arg or 3)
        except (TypeError, ValueError):
            n = 3
        parts = [run_full(f"git diff HEAD~{n} HEAD"), run_full("git diff")]
        return "\n".join(p for p in parts if p.strip()).strip() or None
    if scope == "commit":
        return run_full(f"git show {scope_arg or 'HEAD'}").strip() or None
    return None


def _repo_sig(path: str) -> str | None:
    """仓库指纹: HEAD + 未提交改动的哈希, 用于判断代码是否变化。"""
    import hashlib

    def run(cmd: str) -> str:
        try:
            return subprocess.run(cmd, cwd=path, shell=True, capture_output=True, text=True, timeout=15).stdout
        except Exception:  # noqa: BLE001
            return ""

    head = run("git rev-parse HEAD")
    if not head.strip():
        return None
    return hashlib.md5((head + run("git status --porcelain")).encode()).hexdigest()


def _audit(project_id: int, path: str, engine: str = "claude", scope: str = "full", scope_arg=None) -> None:
    sig = None
    if scope != "full":
        diff = _scope_diff(path, scope, scope_arg)
        if not diff:
            return  # 无改动，跳过
        prompt = "只审查以下最近改动的 diff，找出其中真实的 bug/安全隐患/坏味。" + _JSON_TAIL + "\n\n" + diff[:12000]
    else:
        # 全仓库: 代码未变(HEAD+未提交改动一致)则跳过重复审查, 省 token
        sig = _repo_sig(path)
        if sig and db.get_setting(f"audit_sig:{project_id}") == sig:
            db.set_setting(f"audit_skipped:{project_id}", "1")
            return
        db.set_setting(f"audit_skipped:{project_id}", "0")
        prompt = AUDIT_PROMPT
    if engine == "claude":
        from .. import sdk_run

        res = sdk_run.run_sync(prompt, path, auto_approve=True, timeout=TIMEOUT * 3)
        if res["is_error"] and not res["text"]:
            _add_finding(project_id, "audit", "info", "审查会话失败(SDK)")
            return
        out = res["text"]
    else:
        from ..env import child_env

        try:
            p = subprocess.run(build_audit_argv(engine, prompt), cwd=path, capture_output=True,
                               text=True, timeout=TIMEOUT * 3, env=child_env())
        except Exception as exc:  # noqa: BLE001
            _add_finding(project_id, "audit", "info", f"审查会话失败: {exc}")
            return
        out = p.stdout
    if sig:  # 审查已执行, 记录指纹, 下次代码没变就跳过
        db.set_setting(f"audit_sig:{project_id}", sig)
    m = re.search(r"\[.*\]", out, re.DOTALL)
    if not m:
        return
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return
    for it in items[:10]:
        _add_finding(
            project_id,
            "audit",
            str(it.get("severity", "info")),
            str(it.get("title", "(无标题)")),
            str(it.get("detail", "")),
        )


def analyze(
    project_id: int,
    sources: list[str] | None = None,
    origin: str = "manual",
    scope: str = "full",
    scope_arg: str | None = None,
) -> int:
    """执行整体分析，返回新增 finding 数。阻塞，调用方放线程池。"""
    _tls.origin = origin
    project = db.query_one("SELECT * FROM project WHERE id=?", (project_id,))
    if project is None:
        return 0
    path = project["path"]
    cfg = db.query_one("SELECT * FROM project_config WHERE project_id=?", (project_id,))
    if cfg is None:
        db.execute("INSERT INTO project_config(project_id) VALUES(?)", (project_id,))
        cfg = db.query_one("SELECT * FROM project_config WHERE project_id=?", (project_id,))
    enabled = sources or (cfg["enabled_sources"] or "").split(",")

    before = db.query_one("SELECT COUNT(*) c FROM finding WHERE project_id=?", (project_id,))["c"]
    if any(s in enabled for s in ("test", "build", "lint")):
        _objective(project_id, path, cfg)
    if "git" in enabled:
        _git(project_id, path)
    if "todo" in enabled:
        _todo(project_id, path)
    if "deps" in enabled:
        _deps(project_id, path)
    if "audit" in enabled:
        _audit(project_id, path, scope=scope, scope_arg=scope_arg)
    after = db.query_one("SELECT COUNT(*) c FROM finding WHERE project_id=?", (project_id,))["c"]
    db.set_setting(f"analyze_at:{project_id}", time.time())
    events.emit("findings.updated", {"project_id": project_id})
    return after - before
