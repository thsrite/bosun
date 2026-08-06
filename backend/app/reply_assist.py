"""Reply Assist: diagnose a waiting task and propose an editable user reply.

The suggestion is intentionally conservative: it is never sent automatically and
does not mutate task state. LLM-backed suggestions can be layered on later via
the reflection pipeline; this module stays deterministic and cheap.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

from fastapi import HTTPException

from . import db, scheduler, sdk_run, sessions

MAX_LOG_BYTES = 16 * 1024
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CONFIRM_RE = re.compile(
    r"(\(y/n\)|\(y/N\)|\[y/N\]|\[Y/n\]|\byes/no\b|\bcontinue\b|\bproceed\b|"
    r"\bapply\b|是否|继续|确认|批准|允许运行|要我)",
    re.IGNORECASE,
)
_TEST_RE = re.compile(r"(\btest(s|ing)?\b|\bbuild\b|\blint\b|pytest|npm test|验证|测试|构建|检查)", re.IGNORECASE)
_CLARIFY_RE = re.compile(
    r"(\bwhich\b|\bwhat\b|\bhow should\b|\bchoose\b|\bselect\b|\bclarify\b|"
    r"\bneed .*input\b|请选择|选择|哪种|哪个|需要.*信息|请提供|补充)",
    re.IGNORECASE,
)


def _empty(reason: str) -> dict:
    return {
        "available": False,
        "text": "",
        "reason": reason,
        "source": "none",
        "confidence": 0,
        "updated_at": time.time(),
    }


def _suggest(text: str, reason: str, source: str, confidence: float, now: float) -> dict:
    return {
        "available": True,
        "text": text,
        "reason": reason,
        "source": source,
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "updated_at": now,
    }


def _value(task: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        return task[key]
    except (KeyError, IndexError, TypeError):
        return default


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text).replace("\r", "\n")


def _read_recent_log(log_path: str | None) -> str:
    if not log_path:
        return ""
    path = Path(log_path)
    try:
        with path.open("rb") as f:
            if path.stat().st_size > MAX_LOG_BYTES:
                f.seek(-MAX_LOG_BYTES, 2)
            data = f.read()
    except OSError:
        return ""
    return _normalize_log(data.decode("utf-8", errors="replace"))


def _normalize_log(text: str) -> str:
    """Extract human-readable content from terminal logs and SDK NDJSON logs."""
    chunks: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            chunks.append(line)
            continue
        if not isinstance(obj, dict):
            continue
        t = obj.get("t")
        if t in {"text", "raw", "user"} and obj.get("text"):
            chunks.append(str(obj["text"]))
        elif t == "error" and obj.get("msg"):
            chunks.append(str(obj["msg"]))
        elif t in {"tool", "perm"}:
            chunks.append(f"{obj.get('name', '')} {obj.get('input', '')}".strip())
    return _strip_ansi("\n".join(chunks))[-MAX_LOG_BYTES:]


def _started_age(task: Mapping[str, Any], now: float) -> float:
    started = _value(task, "started_at")
    try:
        return max(0.0, now - float(started))
    except (TypeError, ValueError):
        return 0.0


def build_suggestion(
    task: Mapping[str, Any],
    log_text: str,
    pending_permission: dict | None,
    now: float | None = None,
) -> dict:
    now = now or time.time()
    status = str(_value(task, "status", ""))
    if status != "waiting_input":
        result = _empty("任务未处于待输入状态。")
        result["updated_at"] = now
        return result

    if pending_permission:
        tool = pending_permission.get("tool") or pending_permission.get("name") or "工具调用"
        return _suggest(
            "先根据权限卡片判断是否允许；如果允许，继续保持最小改动，完成后汇报验证结果。",
            f"任务正在等待 {tool} 权限卡片处理。",
            "permission",
            0.9,
            now,
        )

    recent = _strip_ansi(log_text)[-MAX_LOG_BYTES:]
    compact = re.sub(r"\s+", " ", recent).strip()
    question_like = "?" in compact or "？" in compact

    # Test/build/lint questions are common and deserve a specific answer before
    # generic confirmation handling.
    if _TEST_RE.search(compact) and (question_like or "should" in compact.lower() or "是否" in compact):
        return _suggest(
            "可以，先运行项目现有的测试/构建/检查命令；如果失败，只修和本任务直接相关的问题，并汇报结果。",
            "最近输出在询问是否运行验证命令。",
            "tests",
            0.82,
            now,
        )

    if _CONFIRM_RE.search(compact) and (question_like or re.search(r"\b(y|n)\b", compact, re.IGNORECASE)):
        return _suggest(
            "继续，但只做必要的最小改动；完成后运行现有验证命令并汇报结果。",
            "任务处于待输入状态，最近输出在请求继续确认。",
            "confirm",
            0.78,
            now,
        )

    if _CLARIFY_RE.search(compact) and question_like:
        return _suggest(
            "按最保守、最小改动的方案处理；如果有多个方案，先选风险最低且不影响无关功能的方案。",
            "最近输出在请求补充实现选择或产品细节。",
            "clarify",
            0.72,
            now,
        )

    age = _started_age(task, now)
    if status == "waiting_input" and age >= 1800:
        return _suggest(
            "请先说明当前卡住点和你需要我补充的信息；不要继续猜测或扩大改动范围。",
            "任务已等待较久，但最近输出没有明确问题。",
            "blocked",
            0.62,
            now,
        )

    if compact:
        return _suggest(
            "继续按原任务目标推进；保持改动最小，遇到不确定选择先说明取舍再继续。",
            "任务处于待输入状态，最近输出可用于生成保守继续建议。",
            "default",
            0.52,
            now,
        )

    result = _empty("暂无可分析的近期输出。")
    result["updated_at"] = now
    return result


def suggest_reply(task_id: int) -> dict:
    task = db.query_one("SELECT * FROM task WHERE id=?", (task_id,))
    if task is None:
        raise HTTPException(404, "任务不存在")
    pending_permission = scheduler.get_permission(task_id)
    log_text = _read_recent_log(task["log_path"])
    return build_suggestion(task, log_text, pending_permission)


# --- 智能推荐：让 Claude 读当前任务上下文，按需生成针对性回复 ---

_SMART_TIMEOUT = 90
# 终端尾部只用来识别「当前在问什么」，压小以免 TUI 重绘残留淹没真正的问题；
# 对话摘录才是判断意图的主上下文。
_SMART_LOG_CHARS = 4000
_SMART_HISTORY_CHARS = 6000
_SMART_HISTORY_MESSAGES = 30
_SMART_PROMPT = """你在帮用户处理一个卡在“待输入”状态的 AI 编码任务（由 Bosun 编排 Claude Code/Codex 运行）。\
任务已暂停，正等用户回复。请结合任务目标、此前对话和终端最近输出，判断 AI 当前到底卡在哪、在问什么，\
然后**替用户拟一条可以直接发送的简短回复**。

要求：
- 直接输出这条回复本身，不要任何解释、前后缀、标题或引号包裹。
- 只回应当前正在问的具体问题，不要泛泛地说「继续推进」「保持最小改动」这类空话。
- 若 AI 在等一个菜单/编号选择，回复就是要选的那一项（编号或选项内容），可附一句理由。
- 若 AI 列出多个方案，选风险最低、不影响无关功能的那一个。
- 中文，简洁（通常 1-3 句），像用户本人在打字。
{permission}{kind}
任务目标：
{prompt}
{history}
终端最近输出（可能含界面残留，仅用于识别当前提问）：
{log}
"""

_KIND_HINTS = {
    "choice": "\n注意：AI 正停在一个选择提示上，回复应直接给出所选项。\n",
    "input": "\n注意：AI 正在等用户补充信息，回复应直接给出该信息或明确的处理指示。\n",
    "review": "\n注意：本轮执行已结束，AI 在等用户核对结果；回复应针对结果给出下一步指示。\n",
}

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")


def _clean_reply(text: str) -> str:
    """去掉模型可能加的代码块围栏与成对引号，返回可直接发送的纯文本。"""
    cleaned = text.strip()
    cleaned = _FENCE_RE.sub("", cleaned).strip()
    for quote in ('"', "“”", "'", "「」", "『』"):
        open_q, close_q = (quote[0], quote[-1])
        if len(cleaned) >= 2 and cleaned[0] == open_q and cleaned[-1] == close_q:
            cleaned = cleaned[1:-1].strip()
    return cleaned


def _conversation_excerpt(engine: str, cwd: str, uid: str | None) -> str:
    """结构化对话史（session transcript）摘录：比原始终端日志干净得多的意图上下文。"""
    if not uid:
        return ""
    try:
        history = sessions.session_history(engine, cwd, uid, max_messages=_SMART_HISTORY_MESSAGES)
    except Exception:  # noqa: BLE001 - 摘录只是增强，任何读取失败都不阻断建议生成
        return ""
    lines = [
        f"{'用户' if m.get('role') == 'user' else 'AI'}：{m.get('text', '')}"
        for m in history.get("messages", [])
        if m.get("text")
    ]
    return "\n\n".join(lines)[-_SMART_HISTORY_CHARS:]


def _smart_prompt(
    task: Mapping[str, Any],
    log_text: str,
    pending_permission: dict | None,
    history: str = "",
    waiting_kind: str | None = None,
) -> str:
    goal = str(_value(task, "prompt", "")).strip() or "（未记录）"
    log = _strip_ansi(log_text)[-_SMART_LOG_CHARS:].strip() or "（暂无近期输出）"
    permission = ""
    if pending_permission:
        tool = pending_permission.get("tool") or pending_permission.get("name") or "工具调用"
        permission = f"\n注意：任务正在等待 {tool} 权限确认，回复应针对是否允许该操作。\n"
    kind = "" if pending_permission else _KIND_HINTS.get(waiting_kind or "", "")
    history_block = f"\n此前对话摘录（时间正序）：\n{history}\n" if history.strip() else ""
    return _SMART_PROMPT.format(
        prompt=goal, log=log, permission=permission, kind=kind, history=history_block
    )


def build_smart_suggestion(
    task: Mapping[str, Any],
    log_text: str,
    pending_permission: dict | None,
    cwd: str,
    runner,
    now: float | None = None,
    history: str = "",
    waiting_kind: str | None = None,
) -> dict:
    """用可注入的 runner(prompt, cwd)->dict 生成智能回复；失败/空则回退规则建议。"""
    now = now or time.time()
    if str(_value(task, "status", "")) != "waiting_input":
        result = _empty("任务未处于待输入状态。")
        result["updated_at"] = now
        return result

    prompt = _smart_prompt(task, log_text, pending_permission, history, waiting_kind)
    try:
        res = runner(prompt, cwd) or {}
    except Exception:  # noqa: BLE001 - LLM 通道任何异常都回退到规则建议
        res = {"is_error": True, "text": ""}

    text = _clean_reply(str(res.get("text") or ""))
    if res.get("is_error") or not text:
        return build_suggestion(task, log_text, pending_permission, now=now)

    return _suggest(text, "Claude 已读取当前任务日志，生成针对性回复。", "llm", 0.85, now)


def smart_suggest_reply(task_id: int) -> dict:
    task = db.query_one("SELECT * FROM task WHERE id=?", (task_id,))
    if task is None:
        raise HTTPException(404, "任务不存在")
    pending_permission = scheduler.get_permission(task_id)
    log_text = _read_recent_log(task["log_path"])
    project = db.query_one("SELECT path FROM project WHERE id=?", (task["project_id"],))
    cwd = project["path"] if project else "."
    history = _conversation_excerpt(
        str(_value(task, "engine", "cc")), cwd, _value(task, "session_uid")
    )
    waiting_kind = scheduler.get_waiting_kind(task_id)

    def runner(prompt: str, run_cwd: str) -> dict:
        # 禁用工具 + 单轮：退化成纯文本生成，不触碰用户仓库。
        return sdk_run.run_sync(
            prompt,
            run_cwd,
            auto_approve=True,
            timeout=_SMART_TIMEOUT,
            extra_opts={"allowed_tools": [], "max_turns": 1},
        )

    return build_smart_suggestion(
        task, log_text, pending_permission, cwd, runner,
        history=history, waiting_kind=waiting_kind,
    )
