"""Discover model choices exposed by the installed Claude Code / Codex CLI."""
from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import sys
import threading
import time

if sys.platform != "win32":
    import fcntl
    import pty
    import select
    import termios

from . import db
from .env import child_env

_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[()][A-Z0-9]?)"
)
_CLAUDE_PICKER_TIMEOUT_SECONDS = 12
_MAX_PICKER_BYTES = 256_000
_SETTING_KEYS = {
    "cc": "claude_model_options",
    "codex": "codex_model_options",
    "kimi": "kimi_model_options",
}


class ModelDiscoveryError(RuntimeError):
    """The installed CLI did not expose a usable model catalog."""


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=child_env({"NO_COLOR": "1", "FORCE_COLOR": "0"}),
    )


def _without_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\r", "\n")


def parse_claude_model_picker(output: str) -> list[dict[str, str]]:
    """Parse the numbered choices rendered by Claude Code's `/model` picker."""
    text = _without_ansi(output)
    if "Select model" not in text:
        raise ModelDiscoveryError("Claude CLI 未显示模型选择器")
    picker = text.rsplit("Select model", 1)[-1].split("Enter to set", 1)[0]
    options = [{"value": "", "label": "默认"}]
    seen = {""}

    for line in picker.splitlines():
        match = re.search(r"(?:❯\s*)?\d+\.\s+(.+)", line)
        if not match:
            continue
        raw_label = re.sub(r"^\(selected\)\s+", "", match.group(1).strip())
        if raw_label.startswith("Default"):
            continue
        label = re.split(r"\s{2,}|\s+—\s+|✔", raw_label, maxsplit=1)[0].strip()
        alias_label = re.sub(r"\s*\(.*\)\s*$", "", label).strip()
        alias = alias_label.split(maxsplit=1)[0].lower() if alias_label else ""
        if not re.fullmatch(r"[a-z][a-z0-9-]*", alias) or alias in seen:
            continue
        options.append({"value": alias, "label": label})
        seen.add(alias)

    if len(options) == 1:
        raise ModelDiscoveryError("Claude CLI 模型选择器中没有可用模型")
    return options


def _capture_claude_model_picker_windows(binary: str) -> str:
    """Windows 走 pty_compat（ConPTY）：读线程收流，主循环限时等选择器渲染完。"""
    from .pty_compat import PtyProcess

    try:
        proc = PtyProcess.spawn(
            [binary, "--safe-mode", "--ax-screen-reader"],
            env=child_env({"TERM": "xterm-256color", "NO_COLOR": "1", "FORCE_COLOR": "0"}),
            dimensions=(40, 120),
        )
    except Exception as exc:  # noqa: BLE001
        raise ModelDiscoveryError(f"无法启动 Claude CLI：{exc}") from exc

    chunks: list[bytes] = []
    stop = threading.Event()

    def _pump() -> None:
        total = 0
        while not stop.is_set() and total <= _MAX_PICKER_BYTES:
            try:
                chunk = proc.read(16_384)
            except (EOFError, OSError):
                break
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    started_at = time.monotonic()
    command_sent = False
    try:
        while time.monotonic() - started_at < _CLAUDE_PICKER_TIMEOUT_SECONDS:
            if not command_sent and time.monotonic() - started_at >= 1:
                proc.write(b"/model\r")
                command_sent = True
            output = b"".join(chunks).decode("utf-8", errors="replace")
            plain = _without_ansi(output)
            if "Select model" in plain and "Enter to set" in plain:
                return output
            if not proc.isalive():
                break
            time.sleep(0.2)
    finally:
        stop.set()
        if proc.isalive():
            try:
                proc.write(b"\x1b\x03")
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.terminate(force=True)
            except Exception:  # noqa: BLE001
                pass

    raise ModelDiscoveryError("读取 Claude CLI 模型选择器超时或提前退出")


def _capture_claude_model_picker(binary: str) -> str:
    if sys.platform == "win32":
        return _capture_claude_model_picker_windows(binary)
    master_fd, slave_fd = pty.openpty()
    try:
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        process = subprocess.Popen(
            [binary, "--safe-mode", "--ax-screen-reader"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=child_env({"TERM": "xterm-256color", "NO_COLOR": "1", "FORCE_COLOR": "0"}),
            start_new_session=True,
        )
    except OSError as exc:
        os.close(slave_fd)
        os.close(master_fd)
        raise ModelDiscoveryError(f"无法启动 Claude CLI：{exc}") from exc
    os.close(slave_fd)
    chunks: list[bytes] = []
    total_bytes = 0
    command_sent = False
    started_at = time.monotonic()

    try:
        while time.monotonic() - started_at < _CLAUDE_PICKER_TIMEOUT_SECONDS:
            elapsed = time.monotonic() - started_at
            if not command_sent and elapsed >= 1:
                os.write(master_fd, b"/model\r")
                command_sent = True

            readable, _, _ = select.select([master_fd], [], [], 0.2)
            if readable:
                try:
                    chunk = os.read(master_fd, 16_384)
                except OSError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                total_bytes += len(chunk)
                if total_bytes > _MAX_PICKER_BYTES:
                    raise ModelDiscoveryError("Claude CLI 模型选择器输出过大")

            output = b"".join(chunks).decode("utf-8", errors="replace")
            plain = _without_ansi(output)
            if "Select model" in plain and "Enter to set" in plain:
                return output
            if process.poll() is not None:
                break
    finally:
        if process.poll() is None:
            try:
                os.write(master_fd, b"\x1b\x03")
            except OSError:
                pass
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        os.close(master_fd)

    raise ModelDiscoveryError("读取 Claude CLI 模型选择器超时或提前退出")


def discover_claude_model_options(binary: str) -> list[dict[str, str]]:
    return parse_claude_model_picker(_capture_claude_model_picker(binary))


def discover_codex_model_options(binary: str) -> list[dict[str, str]]:
    try:
        result = _run([binary, "debug", "models"], timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise ModelDiscoveryError("读取 Codex CLI 模型目录超时") from exc
    except OSError as exc:
        raise ModelDiscoveryError(f"无法运行 Codex CLI：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "codex debug models 执行失败").strip()
        raise ModelDiscoveryError(detail)
    try:
        models = json.loads(result.stdout).get("models", [])
    except (AttributeError, json.JSONDecodeError) as exc:
        raise ModelDiscoveryError("Codex CLI 返回的模型目录不是有效 JSON") from exc

    options = [{"value": "", "label": "默认"}]
    seen = {""}
    for model in models:
        if not isinstance(model, dict) or model.get("visibility") != "list":
            continue
        slug = str(model.get("slug") or "").strip()
        if not slug or slug in seen:
            continue
        label = str(model.get("display_name") or slug).strip() or slug
        options.append({"value": slug, "label": label})
        seen.add(slug)
    if len(options) == 1:
        raise ModelDiscoveryError("Codex CLI 模型目录中没有可见模型")
    return options


def discover_kimi_model_options() -> list[dict[str, str]]:
    """kimi 的 -m 收 config.toml 里的模型别名；没有 CLI 目录接口，直接读配置。"""
    import tomllib

    from . import sessions

    config_path = sessions.kimi_home() / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text(errors="replace"))
    except OSError as exc:
        raise ModelDiscoveryError("未找到 Kimi 配置(~/.kimi-code/config.toml)，请先运行 kimi 登录") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ModelDiscoveryError("Kimi 配置文件不是有效 TOML") from exc

    models = data.get("models")
    options = [{"value": "", "label": "默认"}]
    seen = {""}
    for alias, spec in (models.items() if isinstance(models, dict) else ()):
        alias = str(alias).strip()
        if not alias or alias in seen:
            continue
        display = spec.get("display_name") if isinstance(spec, dict) else None
        label = str(display or alias).strip() or alias
        options.append({"value": alias, "label": label})
        seen.add(alias)
    if len(options) == 1:
        raise ModelDiscoveryError("Kimi 配置里没有模型别名，可先运行 kimi 登录或配置 provider")
    return options


def refresh_model_options(engine: str, binary: str) -> list[dict[str, str]]:
    if engine == "cc":
        options = discover_claude_model_options(binary)
    elif engine == "codex":
        options = discover_codex_model_options(binary)
    elif engine == "kimi":
        options = discover_kimi_model_options()
    elif engine == "omp":
        # omp 的 --model 是跨 provider 的模糊匹配，没有可直接消费的目录接口；
        # 设置页可以直接填模型 ID，这里明确告知而不是抛未知引擎。
        raise ModelDiscoveryError("omp 暂不支持自动刷新模型列表，可直接填写模型 ID")
    else:
        raise ValueError(f"unknown engine: {engine}")
    db.set_setting(_SETTING_KEYS[engine], json.dumps(options, ensure_ascii=False))
    return options
