"""Runtime settings for engine invocation details."""
from __future__ import annotations

import json
import shlex

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


# 会把 transcript 写到 Bosun 找不到的地方的参数：会话目录一旦被改写，
# 捕获/续跑/历史/token 结算会全部静默失效，所以直接拒绝而不是事后排查。
OMP_SESSION_RELOCATING_ARGS = {
    "--session-dir",
    "--profile",
    "--alias",
    "--no-session",
}

# Bosun 自己决定的调用方式：由这里统一拼，用户覆盖会直接改变任务语义。
# 例如配上 --resume，每个新任务都会去续别人的会话，且因为那份 transcript 已在
# snapshot 里，Bosun 永远捕获不到会话 id。
OMP_RUNTIME_OWNED_ARGS = {
    "--resume", "-r",
    "--continue", "-c",
    "--print", "-p",
    "--mode",
    "--auto-approve",
    "--approval-mode",
    "--cwd",
    "--export",
}


# 不带值的开关，用于区分「选项的值」和「位置参数」。名单之外的选项一律按带值处理，
# 顶多把一个开关后面的下一项少校验一次，不会误伤合法配置。
_OMP_BOOLEAN_ARGS = {
    "--advisor", "--no-lsp", "--no-pty", "--no-tools", "--no-extensions",
    "--no-skills", "--no-rules", "--no-title", "--hide-thinking",
    "--prewalk", "--no-prewalk", "--plan-yolo", "--allow-home",
    "--print-thoughts",
}


class OmpExtraArgsError(ValueError):
    """自定义参数无法使用，附带给用户看的原因。"""


def validate_omp_extra_args(value: object) -> str:
    """校验并返回自定义参数原文。不合法时抛 OmpExtraArgsError。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        raise OmpExtraArgsError(f"参数无法解析(引号是否配对？)：{exc}") from exc
    expects_value = False
    for token in argv:
        if expects_value:
            expects_value = False
            continue
        if not token.startswith("-"):
            raise OmpExtraArgsError(
                f"不允许填位置参数 {token!r}：任务指令由任务本身提供，"
                "这里只接受选项"
            )
        name = token.split("=", 1)[0]
        if name in OMP_SESSION_RELOCATING_ARGS:
            raise OmpExtraArgsError(
                f"不允许使用 {name}：它会改变 omp 的会话存储位置，"
                "Bosun 将无法捕获会话 id，续跑、历史与用量统计都会失效"
            )
        if name in OMP_RUNTIME_OWNED_ARGS:
            raise OmpExtraArgsError(
                f"不允许使用 {name}：运行方式(续跑/审批/输出格式)由 Bosun 按任务决定"
            )
        # 形如 `--flag value` 的选项，下一个 token 是它的值，不该当成位置参数
        expects_value = "=" not in token and name not in _OMP_BOOLEAN_ARGS
    return raw


def normalize_omp_extra_args(value: object) -> str:
    """读取侧的宽松归一：拿不下的值当没配，绝不因为一个坏设置就起不了任务。"""
    try:
        return validate_omp_extra_args(value)
    except OmpExtraArgsError:
        return ""


def omp_extra_args() -> str:
    return normalize_omp_extra_args(db.get_setting("omp_extra_args", ""))


def omp_extra_argv() -> list[str]:
    """把设置里的自定义参数拆成 argv。

    参数是拆成 argv 直接 exec 的，不经过 shell，所以不存在命令注入；但仍然禁止
    在这里塞位置参数(prompt)，否则会和任务指令抢位置。
    """
    raw = omp_extra_args()
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError:
        return []


def with_omp_runtime_args(argv: list[str]) -> list[str]:
    """omp 的模型/思考档位/自定义参数插在可执行文件之后、其余参数之前。

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
    # 自定义参数放最后：同名 flag 由用户显式覆盖上面两项
    prefix += omp_extra_argv()
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
