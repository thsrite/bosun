"""cc / codex CLI version and online update helpers."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config, engine_models
from .env import child_env

_VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)")
_MAX_LOG_CHARS = 5000
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolSpec:
    engine: str
    label: str
    binary: Callable[[], str]
    npm_packages: tuple[str, ...]
    cli_update_args: tuple[str, ...] = ()
    # 该引擎是否有可消费的模型目录。没有的话更新后别去刷，免得把「本来就没有」
    # 报成刷新失败。
    has_model_catalog: bool = True


@dataclass(frozen=True)
class NpmPackage:
    name: str
    version: str | None


_SPECS: dict[str, ToolSpec] = {
    "cc": ToolSpec(
        engine="cc",
        label="Claude Code",
        binary=lambda: config.CLAUDE_BIN,
        npm_packages=("@anthropic-ai/claude-code",),
        cli_update_args=("update",),
    ),
    "codex": ToolSpec(
        engine="codex",
        label="Codex CLI",
        binary=lambda: config.CODEX_BIN,
        npm_packages=("@openai/codex",),
    ),
    "omp": ToolSpec(
        engine="omp",
        label="Oh My Pi",
        binary=lambda: config.OMP_BIN,
        npm_packages=("@oh-my-pi/pi-coding-agent",),
        cli_update_args=("update",),
        has_model_catalog=False,
    ),
    # 官方 npm 包是 @moonshot-ai/kimi-code；npm registry 上的裸 `kimi-cli` 是
    # 不相干的占名包，绝不能配进来(会误装)。官方安装脚本装的是单二进制，
    # npm 探测不到时走 `kimi upgrade` 自更新。
    "kimi": ToolSpec(
        engine="kimi",
        label="Kimi Code",
        binary=lambda: config.KIMI_BIN,
        npm_packages=("@moonshot-ai/kimi-code",),
        cli_update_args=("upgrade",),
    ),
}

_UPDATE_LOCKS = {engine: threading.Lock() for engine in _SPECS}


def _spec(engine: str) -> ToolSpec:
    try:
        return _SPECS[engine]
    except KeyError as exc:
        raise ValueError(f"unknown engine: {engine}") from exc


def _tool_env() -> dict:
    return child_env(
        {
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "npm_config_color": "false",
            "npm_config_update_notifier": "false",
        }
    )


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_tool_env(),
    )


def _tail(text: str | None) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= _MAX_LOG_CHARS:
        return text
    return text[-_MAX_LOG_CHARS:]


def _resolve_binary(binary: str) -> str | None:
    if not binary:
        return None
    path = Path(binary).expanduser()
    if path.is_absolute() or "/" in binary:
        return str(path) if path.is_file() else None
    return shutil.which(binary)


def _refresh_model_options(result: dict, engine: str, binary: str | None) -> dict:
    if not result.get("ok") or not binary:
        return result
    if not _spec(engine).has_model_catalog:
        return result
    try:
        result["model_options"] = engine_models.refresh_model_options(engine, binary)
        result["model_options_error"] = None
    except engine_models.ModelDiscoveryError as exc:
        result["model_options_error"] = str(exc) or "模型列表刷新失败"
    except Exception:  # noqa: BLE001
        logger.exception("刷新 %s 模型列表失败", engine)
        result["model_options_error"] = "模型列表刷新失败"
    return result


def _npm_bin() -> str | None:
    return shutil.which("npm")


def _parse_version(text: str | None) -> str | None:
    if not text:
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def _version_cmp(current: str | None, latest: str | None) -> int | None:
    """Return -1/0/1 when both versions are comparable, otherwise None."""
    if not current or not latest:
        return None
    current_nums = [int(p) for p in re.findall(r"\d+", current.split("-", 1)[0])[:3]]
    latest_nums = [int(p) for p in re.findall(r"\d+", latest.split("-", 1)[0])[:3]]
    if not current_nums or not latest_nums:
        return None
    while len(current_nums) < 3:
        current_nums.append(0)
    while len(latest_nums) < 3:
        latest_nums.append(0)
    if current_nums < latest_nums:
        return -1
    if current_nums > latest_nums:
        return 1
    return 0


def _npm_global_package(package_name: str) -> NpmPackage | None:
    npm = _npm_bin()
    if not npm:
        return None
    try:
        result = _run([npm, "-g", "ls", package_name, "--depth=0", "--json"], timeout=20)
    except Exception:  # noqa: BLE001
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    dep = (data.get("dependencies") or {}).get(package_name)
    if not isinstance(dep, dict):
        return None
    return NpmPackage(name=package_name, version=dep.get("version"))


def _installed_npm_package(spec: ToolSpec) -> NpmPackage | None:
    for package_name in spec.npm_packages:
        package = _npm_global_package(package_name)
        if package:
            return package
    return None


def _known_latest_package(spec: ToolSpec) -> str | None:
    return spec.npm_packages[0] if spec.npm_packages else None


def _latest_npm_version(package_name: str) -> tuple[str | None, str | None]:
    npm = _npm_bin()
    if not npm:
        return None, "未找到 npm，无法在线检测更新"
    try:
        result = _run([npm, "view", package_name, "version", "--json"], timeout=45)
    except subprocess.TimeoutExpired:
        return None, "检测更新超时"
    except Exception as exc:  # noqa: BLE001
        return None, f"检测更新失败：{exc}"
    if result.returncode != 0:
        return None, _tail(result.stderr) or "npm view 执行失败"
    raw = (result.stdout or "").strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, str):
            return parsed, None
    except json.JSONDecodeError:
        pass
    version = _parse_version(raw)
    return version, None if version else "无法解析最新版本"


def version_info(engine: str) -> dict:
    spec = _spec(engine)
    configured_binary = spec.binary()
    resolved_binary = _resolve_binary(configured_binary)
    raw_version = ""
    cli_version = None
    error = None

    if resolved_binary:
        try:
            result = _run([resolved_binary, "--version"], timeout=15)
            raw_version = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
            cli_version = _parse_version(raw_version)
            if result.returncode != 0:
                error = _tail(result.stderr) or f"{spec.label} --version 退出码 {result.returncode}"
            elif not cli_version:
                error = "无法解析版本号"
        except subprocess.TimeoutExpired:
            error = "读取版本超时"
        except Exception as exc:  # noqa: BLE001
            error = f"读取版本失败：{exc}"
    else:
        error = f"未找到命令：{configured_binary}"

    npm = _npm_bin()
    package = _installed_npm_package(spec)
    package_name = package.name if package else None
    package_version = package.version if package else None
    effective_version = cli_version or package_version
    cli_update_supported = bool(resolved_binary and spec.cli_update_args)
    latest_package_name = package_name or _known_latest_package(spec)

    update_supported = bool((npm and package_name) or cli_update_supported)
    update_error = None
    if not update_supported:
        if not npm:
            update_error = "未找到 npm"
        else:
            update_error = "未识别 npm 全局安装包，暂不支持在线更新"
    elif not package_name and not cli_update_supported:
        update_error = "未识别 npm 全局安装包，暂不支持在线更新"

    return {
        "engine": engine,
        "label": spec.label,
        "configured_binary": configured_binary,
        "binary": resolved_binary,
        "installed": bool(resolved_binary or package_version),
        "version": effective_version,
        "cli_version": cli_version,
        "version_raw": raw_version,
        "package_manager": "npm" if package_name else ("claude" if cli_update_supported else None),
        "package_name": package_name,
        "package_version": package_version,
        "latest_package_name": latest_package_name,
        "update_command": " ".join([resolved_binary, *spec.cli_update_args]) if cli_update_supported else None,
        "update_supported": update_supported,
        "can_check_update": bool(npm and latest_package_name),
        "can_update": update_supported,
        "update_error": update_error,
        "latest_version": None,
        "update_available": None,
        "checked_at": None,
        "error": error,
    }


def all_version_info() -> dict:
    return {engine: version_info(engine) for engine in _SPECS}


def is_installed(engine: str) -> bool:
    """引擎的可执行文件是否存在。只做 which/文件探测，不跑 --version。"""
    return bool(_resolve_binary(_spec(engine).binary()))


def installed_engines() -> dict[str, bool]:
    """各引擎是否已安装，供界面按需隐藏与自动路由使用（够轻，可高频调用）。"""
    from . import browser_computer

    return {
        **{engine: is_installed(engine) for engine in _SPECS},
        "browser": bool(browser_computer.availability()["available"]),
    }


def check_update(engine: str) -> dict:
    info = version_info(engine)
    package_name = info.get("package_name") or info.get("latest_package_name")
    if not package_name:
        return {
            **info,
            "can_check_update": False,
            "update_error": info.get("update_error") or "未识别可更新的安装包",
            "checked_at": time.time(),
        }

    latest, error = _latest_npm_version(str(package_name))
    cmp_result = _version_cmp(info.get("version"), latest)
    return {
        **info,
        "latest_version": latest,
        "update_available": None if cmp_result is None else cmp_result < 0,
        "update_error": error,
        "checked_at": time.time(),
    }


def update_tool(engine: str) -> dict:
    spec = _spec(engine)
    lock = _UPDATE_LOCKS[engine]
    if not lock.acquire(blocking=False):
        return {"ok": False, "engine": engine, "error": "正在更新，请稍后再试"}
    try:
        before = version_info(engine)
        package_name = before.get("package_name")
        update_command = before.get("update_command")
        if update_command and before.get("binary"):
            args = [str(before["binary"]), *spec.cli_update_args]
            try:
                result = _run(args, timeout=300)
            except subprocess.TimeoutExpired:
                return {"ok": False, "engine": engine, "before": before, "error": "在线更新超时"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "engine": engine, "before": before, "error": f"在线更新失败：{exc}"}

            after = version_info(engine)
            return _refresh_model_options({
                "ok": result.returncode == 0,
                "engine": engine,
                "package_name": package_name,
                "update_command": update_command,
                "exit_code": result.returncode,
                "before_version": before.get("version"),
                "after_version": after.get("version"),
                "before": before,
                "after": after,
                "stdout": _tail(result.stdout),
                "stderr": _tail(result.stderr),
                "error": None if result.returncode == 0 else (_tail(result.stderr) or "claude update 执行失败"),
            }, engine, after.get("binary"))

        if not package_name:
            return {
                "ok": False,
                "engine": engine,
                "before": before,
                "error": before.get("update_error") or "未识别可更新的安装包",
            }
        npm = _npm_bin()
        if not npm:
            return {"ok": False, "engine": engine, "before": before, "error": "未找到 npm"}

        try:
            result = _run([npm, "install", "-g", f"{package_name}@latest"], timeout=300)
        except subprocess.TimeoutExpired:
            return {"ok": False, "engine": engine, "before": before, "error": "在线更新超时"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "engine": engine, "before": before, "error": f"在线更新失败：{exc}"}

        after = version_info(engine)
        return _refresh_model_options({
            "ok": result.returncode == 0,
            "engine": engine,
            "package_name": package_name,
            "exit_code": result.returncode,
            "before_version": before.get("version"),
            "after_version": after.get("version"),
            "before": before,
            "after": after,
            "stdout": _tail(result.stdout),
            "stderr": _tail(result.stderr),
            "error": None if result.returncode == 0 else (_tail(result.stderr) or "npm install 执行失败"),
        }, engine, after.get("binary") or spec.binary())
    finally:
        lock.release()
