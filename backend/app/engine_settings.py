"""Runtime settings for engine invocation details."""
from __future__ import annotations

import json

from . import codex_skills_guard, db

CLAUDE_INVOCATIONS = {"auto", "sdk", "cli"}
CLAUDE_MODEL_OPTIONS = [
    {"value": "", "label": "默认"},
    {"value": "fable", "label": "Fable"},
    {"value": "sonnet", "label": "Sonnet"},
    {"value": "opus", "label": "Opus"},
]
CLAUDE_EFFORT_OPTIONS = [
    {"value": "", "label": "默认"},
    {"value": "low", "label": "Low"},
    {"value": "medium", "label": "Medium"},
    {"value": "high", "label": "High"},
    {"value": "xhigh", "label": "XHigh"},
    {"value": "max", "label": "Max"},
]
_CLAUDE_EFFORT_VALUES = {opt["value"] for opt in CLAUDE_EFFORT_OPTIONS}


def claude_invocation() -> str:
    raw = str(db.get_setting("claude_invocation", "auto") or "auto").strip().lower()
    return raw if raw in CLAUDE_INVOCATIONS else "auto"


def claude_model() -> str:
    return normalize_claude_model(db.get_setting("claude_model", ""))


def normalize_claude_model(value: object) -> str:
    # Claude CLI 接受别名，也接受完整/自定义模型 ID。
    return str(value or "").strip()


def claude_model_options() -> list[dict[str, str]]:
    return _cached_model_options("claude_model_options", CLAUDE_MODEL_OPTIONS)


def claude_effort() -> str:
    return normalize_claude_effort(db.get_setting("claude_effort", ""))


def normalize_claude_effort(value: object) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in _CLAUDE_EFFORT_VALUES else ""


def claude_effort_options() -> list[dict[str, str]]:
    return [dict(opt) for opt in CLAUDE_EFFORT_OPTIONS]


def with_claude_model_arg(argv: list[str]) -> list[str]:
    model = claude_model()
    if not model:
        return argv
    return [*argv, "--model", model]


def with_claude_runtime_args(argv: list[str]) -> list[str]:
    argv = with_claude_model_arg(argv)
    effort = claude_effort()
    if effort:
        argv = [*argv, "--effort", effort]
    return argv


# ---- Codex 模型（内置建议 + 可填写自定义 provider 的模型 ID）----
CODEX_MODEL_OPTIONS = [
    {"value": "", "label": "默认"},
    {"value": "gpt-5.6-sol", "label": "gpt-5.6-sol"},
    {"value": "gpt-5.6-terra", "label": "gpt-5.6-terra"},
    {"value": "gpt-5.6-luna", "label": "gpt-5.6-luna"},
    {"value": "gpt-5.5", "label": "gpt-5.5"},
]
CODEX_EFFORT_OPTIONS = [
    {"value": "", "label": "默认"},
    {"value": "low", "label": "Low"},
    {"value": "medium", "label": "Medium"},
    {"value": "high", "label": "High"},
    {"value": "xhigh", "label": "XHigh"},
    {"value": "max", "label": "Max"},
    {"value": "ultra", "label": "Ultra"},
]
_CODEX_EFFORT_VALUES = {opt["value"] for opt in CODEX_EFFORT_OPTIONS}


def normalize_codex_model(value: object) -> str:
    # Codex 的 -m 接受内置目录之外的模型 ID（例如自定义 provider 的模型）。
    return str(value or "").strip()


def codex_model() -> str:
    return normalize_codex_model(db.get_setting("codex_model", ""))


def codex_model_options() -> list[dict[str, str]]:
    return _cached_model_options("codex_model_options", CODEX_MODEL_OPTIONS)


def _cached_model_options(key: str, fallback: list[dict[str, str]]) -> list[dict[str, str]]:
    raw = db.get_setting(key, "")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else None
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, list) or not parsed:
        return [dict(opt) for opt in fallback]
    options = []
    for option in parsed:
        if not isinstance(option, dict):
            return [dict(opt) for opt in fallback]
        value = option.get("value")
        label = option.get("label")
        if not isinstance(value, str) or not isinstance(label, str):
            return [dict(opt) for opt in fallback]
        options.append({"value": value, "label": label})
    if options[0].get("value") != "":
        return [dict(opt) for opt in fallback]
    return options


def codex_effort() -> str:
    return normalize_codex_effort(db.get_setting("codex_effort", ""))


def normalize_codex_effort(value: object) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in _CODEX_EFFORT_VALUES else ""


def codex_effort_options() -> list[dict[str, str]]:
    return [dict(opt) for opt in CODEX_EFFORT_OPTIONS]


def with_codex_model_arg(argv: list[str]) -> list[str]:
    """codex 的 -m 是全局选项, 插在 CODEX_BIN 之后、子命令(exec/resume)之前。"""
    model = codex_model()
    if not model or not argv:
        return argv
    return [argv[0], "-m", model, *argv[1:]]


def with_codex_runtime_args(argv: list[str]) -> list[str]:
    """Codex 的模型与 config override 都是全局参数，必须放在子命令前。"""
    if not argv:
        return argv
    prefix = [argv[0]]
    model = codex_model()
    if model:
        prefix += ["-m", model]
    effort = codex_effort()
    if effort:
        # -c 的 value 按 TOML 解析，显式加双引号保证它是字符串。
        prefix += ["-c", f'model_reasoning_effort="{effort}"']
    skills_override = codex_skills_guard.runtime_skills_override()
    if skills_override:
        prefix += ["-c", f"skills.config={skills_override}"]
    return [*prefix, *argv[1:]]


# ---- omp(Oh My Pi)模型与思考档位 ----
# omp 的 --model 做模糊匹配("opus" / "gpt-5.6" / "openai/gpt-5.2" 都接受)，
# 所以内置项只是常用建议，settings 里同样允许填任意自定义模型 ID。
OMP_MODEL_OPTIONS = [
    {"value": "", "label": "默认"},
    {"value": "opus", "label": "Opus"},
    {"value": "sonnet", "label": "Sonnet"},
    {"value": "gpt-5.6", "label": "gpt-5.6"},
    {"value": "gemini", "label": "Gemini"},
]
# 对应 omp --thinking 的取值
OMP_THINKING_OPTIONS = [
    {"value": "", "label": "默认"},
    {"value": "off", "label": "Off"},
    {"value": "minimal", "label": "Minimal"},
    {"value": "low", "label": "Low"},
    {"value": "medium", "label": "Medium"},
    {"value": "high", "label": "High"},
    {"value": "xhigh", "label": "XHigh"},
    {"value": "max", "label": "Max"},
    {"value": "auto", "label": "Auto"},
]
_OMP_THINKING_VALUES = {opt["value"] for opt in OMP_THINKING_OPTIONS}


def normalize_omp_model(value: object) -> str:
    return str(value or "").strip()


def omp_model() -> str:
    return normalize_omp_model(db.get_setting("omp_model", ""))


def omp_model_options() -> list[dict[str, str]]:
    return _cached_model_options("omp_model_options", OMP_MODEL_OPTIONS)


def normalize_omp_thinking(value: object) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in _OMP_THINKING_VALUES else ""


def omp_thinking() -> str:
    return normalize_omp_thinking(db.get_setting("omp_thinking", ""))


def omp_thinking_options() -> list[dict[str, str]]:
    return [dict(opt) for opt in OMP_THINKING_OPTIONS]


def with_omp_runtime_args(argv: list[str]) -> list[str]:
    """omp 的模型与思考档位插在可执行文件之后、其余参数之前。

    argv 里可能已经带上了 prompt(如 build_audit_argv)，追加到末尾会让 flag 落在
    位置参数后面，所以统一按前缀插入。
    """
    if not argv:
        return argv
    prefix = [argv[0]]
    model = omp_model()
    if model:
        prefix += ["--model", model]
    thinking = omp_thinking()
    if thinking:
        prefix += ["--thinking", thinking]
    return [*prefix, *argv[1:]]


def should_use_claude_sdk(
    engine: str,
    resume: bool,
    post_input: str | None,
    invocation: str | None = None,
) -> bool:
    mode = (invocation or claude_invocation()).strip().lower()
    if mode not in CLAUDE_INVOCATIONS:
        mode = "auto"
    if engine != "cc" or resume or post_input:
        return False
    return mode in {"auto", "sdk"}
