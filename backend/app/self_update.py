"""Bosun 自身的版本检查与在线更新。

以 GitHub Release 的 tag 为准（不看 main 分支的滚动 commit），更新落在本地 git 工作区上：
fetch → 快进合并到 tag → 按变更文件决定是否装依赖 / 重建前端。

安全边界：工作区有未提交改动、或本地有 release 之外的提交时一律拒绝更新，绝不覆盖用户改动。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import certifi

from .env import child_env
from .version import VERSION

logger = logging.getLogger(__name__)

REPO = "thsrite/bosun"
RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"

# backend/app/self_update.py → backend/app → backend → 仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_MAX_LOG_CHARS = 4000
_UPDATE_LOCK = threading.Lock()


# ---- 基础工具 ----
def _tail(text: str | None) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= _MAX_LOG_CHARS else text[-_MAX_LOG_CHARS:]


def _tag_version(tag: str | None) -> str | None:
    """v0.2.0 → 0.2.0。"""
    if not tag:
        return None
    return tag.strip().lstrip("vV") or None


def _version_cmp(current: str | None, latest: str | None) -> int | None:
    """可比时返回 -1/0/1，任一侧解析不出数字段则返回 None。"""
    if not current or not latest:
        return None
    current_nums = [int(p) for p in re.findall(r"\d+", current.split("-", 1)[0])[:3]]
    latest_nums = [int(p) for p in re.findall(r"\d+", latest.split("-", 1)[0])[:3]]
    if not current_nums or not latest_nums:
        return None
    current_nums += [0] * (3 - len(current_nums))
    latest_nums += [0] * (3 - len(latest_nums))
    if current_nums < latest_nums:
        return -1
    return 1 if current_nums > latest_nums else 0


def _env() -> dict:
    # 更新全程不可交互：git 弹凭据框会把请求挂死到超时
    return child_env({"NO_COLOR": "1", "FORCE_COLOR": "0", "GIT_TERMINAL_PROMPT": "0"})


def _run(args: list[str], timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env(),
    )


def _run_git(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], timeout=timeout)


def _npm_bin() -> str | None:
    return shutil.which("npm")


def _dist_exists() -> bool:
    return (REPO_ROOT / "frontend" / "dist" / "index.html").is_file()


# ---- 仓库状态 ----
def _repo_state() -> dict:
    state = {"is_git": False, "branch": None, "detached": False, "dirty": False, "head": None, "error": None}
    try:
        inside = _run_git(["rev-parse", "--is-inside-work-tree"], timeout=10)
    except Exception as exc:  # noqa: BLE001
        state["error"] = f"读取 git 状态失败：{exc}"
        return state
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        state["error"] = "当前部署不是 git 工作区"
        return state

    state["is_git"] = True
    try:
        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=10).stdout.strip()
        state["branch"] = None if branch == "HEAD" else branch
        state["detached"] = branch == "HEAD"
        state["head"] = _run_git(["rev-parse", "--short", "HEAD"], timeout=10).stdout.strip() or None
        status = _run_git(["status", "--porcelain"], timeout=20)
        state["dirty"] = bool(status.stdout.strip())
    except Exception as exc:  # noqa: BLE001
        state["error"] = f"读取 git 状态失败：{exc}"
    return state


def _blockers(state: dict) -> list[str]:
    if not state["is_git"]:
        return [state.get("error") or "当前部署不是 git 工作区，无法在线更新"]
    blockers = []
    if state.get("error"):
        blockers.append(state["error"])
    if state["dirty"]:
        blockers.append("工作区有未提交改动，请先提交或清理后再更新")
    return blockers


# ---- GitHub Release ----
def _latest_release() -> tuple[dict | None, str | None]:
    request = urllib.request.Request(
        RELEASE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"bosun/{VERSION}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15, context=_SSL_CTX) as response:
            return json.loads(response.read().decode()), None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, "仓库还没有发布任何 release"
        if exc.code == 403:
            return None, "GitHub API 限流，请稍后再试"
        return None, f"检查更新失败：HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return None, f"检查更新失败：{exc}"


def _base_info(state: dict) -> dict:
    blockers = _blockers(state)
    return {
        "repo": REPO,
        "releases_url": RELEASES_URL,
        "current_version": VERSION,
        "branch": state.get("branch"),
        "detached": state.get("detached"),
        "dirty": state.get("dirty"),
        "head": state.get("head"),
        "is_git": state.get("is_git"),
        "blockers": blockers,
        "can_update": not blockers,
        "latest_version": None,
        "latest_tag": None,
        "update_available": None,
        "release_notes": None,
        "release_url": RELEASES_URL,
        "published_at": None,
        "check_error": None,
        "checked_at": None,
    }


def status() -> dict:
    """本地版本与仓库状态，不联网。"""
    return _base_info(_repo_state())


def check_update() -> dict:
    global _cached_check
    info = _base_info(_repo_state())
    release, error = _latest_release()
    info["checked_at"] = time.time()
    if error or not release:
        info["check_error"] = error or "未获取到 release 信息"
    else:
        tag = release.get("tag_name")
        latest = _tag_version(tag)
        cmp_result = _version_cmp(VERSION, latest)
        info.update(
            {
                "latest_version": latest,
                "latest_tag": tag,
                "update_available": None if cmp_result is None else cmp_result < 0,
                "release_notes": _tail(release.get("body")),
                "release_url": release.get("html_url") or RELEASES_URL,
                "published_at": release.get("published_at"),
            }
        )
        if cmp_result is None:
            info["check_error"] = "无法比较版本号，请手动核对 release"
    with _cache_lock:
        _cached_check = info
    return info


# ---- 健康探测用的轻量摘要（带缓存，绝不阻塞） ----

_CHECK_TTL_OK = 6 * 3600  # 检查成功后隔这么久才复查
_CHECK_TTL_FAIL = 15 * 60  # 失败（断网/限流）后更快重试
_cache_lock = threading.Lock()
_cached_check: dict | None = None
_check_in_flight = False


def health_summary() -> dict:
    """版本与更新摘要，供 /api/health 附带返回（菜单栏 15s 一轮询）。

    GitHub 查询在后台线程完成并写入缓存，本函数只读缓存、立即返回；
    手动「检查更新」（check_update）也会刷新同一份缓存。
    """
    global _check_in_flight
    with _cache_lock:
        cached = _cached_check
        ttl = _CHECK_TTL_FAIL if (cached and cached.get("check_error")) else _CHECK_TTL_OK
        stale = cached is None or time.time() - (cached.get("checked_at") or 0) > ttl
        if stale and not _check_in_flight:
            _check_in_flight = True
            threading.Thread(target=_refresh_check_cache, name="bosun-update-check", daemon=True).start()
    summary = {
        "version": VERSION,
        "latest_version": None,
        "update_available": None,
        "release_url": RELEASES_URL,
    }
    if cached and not cached.get("check_error"):
        summary["latest_version"] = cached.get("latest_version")
        summary["update_available"] = cached.get("update_available")
        summary["release_url"] = cached.get("release_url") or RELEASES_URL
    return summary


def _refresh_check_cache() -> None:
    global _check_in_flight
    try:
        check_update()
    except Exception:  # noqa: BLE001
        logger.exception("后台检查更新失败")
    finally:
        with _cache_lock:
            _check_in_flight = False


# ---- 执行更新 ----
def _record(steps: list[dict], name: str, command: list[str], result: subprocess.CompletedProcess[str]) -> bool:
    steps.append(
        {
            "name": name,
            "command": " ".join(command),
            "exit_code": result.returncode,
            "ok": result.returncode == 0,
            "output": _tail("\n".join(part for part in [result.stdout, result.stderr] if part and part.strip())),
        }
    )
    return result.returncode == 0


def _skip(steps: list[dict], name: str, reason: str) -> None:
    steps.append({"name": name, "command": None, "exit_code": None, "ok": True, "skipped": True, "output": reason})


def _fail(steps: list[dict], error: str, **extra) -> dict:
    return {"ok": False, "changed": False, "steps": steps, "error": error, **extra}


def run_update() -> dict:
    """拉取最新 release 并在本地 git 工作区落地。返回逐步日志。"""
    if not _UPDATE_LOCK.acquire(blocking=False):
        return {"ok": False, "changed": False, "steps": [], "error": "正在更新，请稍后再试"}
    try:
        return _run_update()
    finally:
        _UPDATE_LOCK.release()


def _run_update() -> dict:
    steps: list[dict] = []
    state = _repo_state()
    blockers = _blockers(state)
    if blockers:
        return _fail(steps, blockers[0], from_version=VERSION, to_version=None)

    release, error = _latest_release()
    if error or not release:
        return _fail(steps, error or "未获取到 release 信息", from_version=VERSION, to_version=None)

    tag = release.get("tag_name")
    latest = _tag_version(tag)
    cmp_result = _version_cmp(VERSION, latest)
    if cmp_result is None:
        return _fail(steps, "无法比较版本号，请手动核对 release", from_version=VERSION, to_version=latest)
    if cmp_result >= 0:
        return {
            "ok": True,
            "changed": False,
            "steps": steps,
            "error": None,
            "from_version": VERSION,
            "to_version": latest,
            "message": "已是最新版本",
        }

    result = _run_git(["fetch", "--tags", "--prune", "origin"], timeout=180)
    if not _record(steps, "git fetch", ["git", "fetch", "--tags", "--prune", "origin"], result):
        return _fail(steps, _tail(result.stderr) or "拉取远端失败", from_version=VERSION, to_version=latest)

    target = f"refs/tags/{tag}^{{commit}}"
    result = _run_git(["rev-parse", "--verify", target], timeout=20)
    if not _record(steps, "定位 release commit", ["git", "rev-parse", "--verify", target], result):
        return _fail(steps, f"远端没有 tag {tag}", from_version=VERSION, to_version=latest)
    target_commit = result.stdout.strip()

    # HEAD 必须是目标 commit 的祖先，否则快进合并会变成真正的 merge，可能吞掉本地提交
    ancestor = _run_git(["merge-base", "--is-ancestor", "HEAD", target_commit], timeout=20)
    _record(steps, "检查本地提交", ["git", "merge-base", "--is-ancestor", "HEAD", target_commit], ancestor)
    if ancestor.returncode != 0:
        return _fail(
            steps,
            f"本地存在 {tag} 之外的提交，无法快进更新；请先自行合并或重置",
            from_version=VERSION,
            to_version=latest,
        )

    changed_files = _changed_files(steps, target_commit)

    result = _run_git(["merge", "--ff-only", target_commit], timeout=120)
    if not _record(steps, "更新代码", ["git", "merge", "--ff-only", target_commit], result):
        return _fail(steps, _tail(result.stderr) or "更新代码失败", from_version=VERSION, to_version=latest)

    error = _install_dependencies(steps, changed_files)
    if error:
        return _fail(steps, error, from_version=VERSION, to_version=latest)

    return {
        "ok": True,
        "changed": True,
        "steps": steps,
        "error": None,
        "from_version": VERSION,
        "to_version": latest,
        "tag": tag,
        "message": f"已更新到 {tag}",
    }


def _changed_files(steps: list[dict], target_commit: str) -> list[str]:
    """本次更新涉及哪些文件——决定要不要装依赖、要不要重建前端。读不到就当全变了。"""
    result = _run_git(["diff", "--name-only", "HEAD", target_commit], timeout=60)
    if result.returncode != 0:
        _record(steps, "对比变更文件", ["git", "diff", "--name-only", "HEAD", target_commit], result)
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _install_dependencies(steps: list[dict], changed_files: list[str]) -> str | None:
    """按变更范围装依赖 / 重建前端；返回错误信息，成功返回 None。

    changed_files 为空表示没拿到变更清单，此时保守地全跑一遍。
    """
    unknown_scope = not changed_files
    backend_changed = unknown_scope or any(f == "backend/requirements.txt" for f in changed_files)
    npm_changed = unknown_scope or any(
        f in {"frontend/package.json", "frontend/package-lock.json"} for f in changed_files
    )
    frontend_changed = unknown_scope or any(f.startswith("frontend/") for f in changed_files)

    if backend_changed:
        command = [sys.executable, "-m", "pip", "install", "-r", str(REPO_ROOT / "backend" / "requirements.txt")]
        try:
            result = _run(command, timeout=600)
        except Exception as exc:  # noqa: BLE001
            return f"安装后端依赖失败：{exc}"
        if not _record(steps, "安装后端依赖", command, result):
            return _tail(result.stderr) or "安装后端依赖失败"
    else:
        _skip(steps, "安装后端依赖", "requirements.txt 未变更，跳过")

    frontend_dir = REPO_ROOT / "frontend"
    npm = _npm_bin()
    if npm_changed or frontend_changed:
        if not npm:
            return "未找到 npm，无法更新前端依赖；请手动执行 npm install && npm run build"

    if npm_changed and npm:
        command = [npm, "install"]
        try:
            result = _run(command, timeout=900, cwd=frontend_dir)
        except Exception as exc:  # noqa: BLE001
            return f"安装前端依赖失败：{exc}"
        if not _record(steps, "安装前端依赖", command, result):
            return _tail(result.stderr) or "安装前端依赖失败"
    else:
        _skip(steps, "安装前端依赖", "前端依赖未变更，跳过")

    # 只有生产模式（后端托管 frontend/dist）才需要重建；开发模式下 Vite 直接跑源码
    if frontend_changed and npm and _dist_exists():
        command = [npm, "run", "build"]
        try:
            result = _run(command, timeout=900, cwd=frontend_dir)
        except Exception as exc:  # noqa: BLE001
            return f"构建前端失败：{exc}"
        if not _record(steps, "构建前端", command, result):
            return _tail(result.stderr) or "构建前端失败"
    else:
        _skip(steps, "构建前端", "前端未变更或未使用构建产物，跳过")

    return None
