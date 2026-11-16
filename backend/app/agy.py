"""官方 agy CLI 的任务级日志与会话文件；不访问模型接口或读取登录密钥。"""
from __future__ import annotations

import re
import json
from pathlib import Path

from . import config

_CREATED = re.compile(r"Created conversation ([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}) \(backend=")


def headless_result(output: str) -> dict:
    # 自动化命令层把 stderr 追加在 stdout 后；只解码官方 JSON 响应，忽略尾部诊断。
    try:
        result, _ = json.JSONDecoder().raw_decode(output.lstrip())
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def task_log(task_id: int, started_at: float) -> Path:
    # 每轮独立日志，避免重跑误认领上一轮或同目录并发任务的会话。
    return config.LOG_DIR / f"task-{task_id}-agy-{int(started_at * 1_000_000)}.log"


def capture_session(task_id: int, started_at: float) -> str | None:
    try:
        with task_log(task_id, started_at).open(errors="replace") as source:
            for line in source:
                match = _CREATED.search(line)
                if match:
                    return match[1]
    except OSError:
        pass
    return None


def transcript_path(uid: str) -> Path:
    root = Path.home() / ".gemini"
    relative = Path("brain") / uid / ".system_generated" / "logs" / "transcript.jsonl"
    cli_path = root / "antigravity-cli" / relative
    desktop_path = root / "antigravity" / relative
    return cli_path if cli_path.is_file() or not desktop_path.is_file() else desktop_path


def message(obj: dict) -> tuple[str | None, str]:
    role = {"USER_INPUT": "user", "AGENT_RESPONSE": "assistant"}.get(obj.get("type"))
    content = obj.get("content")
    text = content if isinstance(content, str) else ""
    if role == "user":
        match = re.search(r"<USER_REQUEST>\s*\n?(.*?)\n?</USER_REQUEST>", text, re.S)
        if match:
            text = match[1]
    return role, text.strip()
