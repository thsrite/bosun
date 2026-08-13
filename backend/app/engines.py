"""引擎适配层：把任务转成要在 pty 里跑的 argv。

cc  = Claude Code CLI (`claude`)：支持 --session-id 钉住会话 id、--resume 恢复
codex = OpenAI Codex CLI (`codex`)：resume <uuid> 恢复；会话 id 需运行后捕获
omp = Oh My Pi CLI (`omp`)：--resume <id 前缀> 恢复；会话 id 需运行后捕获
kimi = Kimi Code CLI (`kimi`)：-S session_<uuid> 恢复；交互模式不收位置参数 prompt，
       首条指令由 PtySession 以括号粘贴写入 TUI（见 uses_stdin_prompt）
"""
from __future__ import annotations

import os

from . import engine_settings, engine_updates, harness_adapter, subtasks
from .config import CLAUDE_BIN, CODEX_BIN, KIMI_BIN, OMP_BIN
from .directives import (  # noqa: F401  兼容既有 engines.REPORT_DIRECTIVE 引用
    ENGINE_ROSTER_TEMPLATE,
    ORCHESTRATION_REPORT_ADDENDUM,
    REPORT_DIRECTIVE,
    SUBTASK_TEMPLATE,
)

CODING_ENGINES = {"cc", "codex", "omp", "kimi"}
ENGINES = {*CODING_ENGINES, "browser"}

# 派发提示里对各引擎的称呼（agent 要照着敲命令，必须是真实可执行名）
_ENGINE_CLI_NAMES = {"cc": "claude", "codex": "codex", "omp": "omp", "kimi": "kimi"}
_ENGINE_ALIASES = {"claude": "cc", "claude-code": "cc", "claude code": "cc"}
_FALSE = {"0", "false", "no", "off"}


def normalize_engine_id(engine: str) -> str:
    """把用户可见的 Claude Code 名称归一化为 Bosun 内部引擎键。"""
    return _ENGINE_ALIASES.get(engine, engine)


def other_engine_names(engine: str) -> list[str]:
    """同机已安装的、除 engine 之外的引擎 CLI 名，按固定顺序。

    顺序固定是硬要求：codex 会话认领靠首条用户消息与 prompt 逐字比对，
    提示词必须可复现。探测失败返回空列表——派发不能因此挂掉。
    """
    try:
        installed = engine_updates.installed_engines()
    except Exception:  # noqa: BLE001  探测失败不阻断派发
        return []
    return [
        _ENGINE_CLI_NAMES[name]
        for name in ("cc", "codex", "omp", "kimi")
        if name != engine and installed.get(name)
    ]


def engine_roster_hint(engine: str) -> str:
    """告诉 agent 同机还装了哪些**别的**引擎 CLI，可按需调用（第二意见/交叉复审）。

    刻意**不注入技能清单**：cc 等 CLI 自己就会把 skill 列表加载进系统提示
    （实测本机 230 个 skill），再注入一份纯冗余且撑爆 prompt。agent 真正缺的
    信息是「同机还有哪些别的引擎」。

    只列已安装且非当前引擎的；一个都没有就返回空串，不浪费 token。
    BOSUN_ENGINE_ROSTER_HINT=0 可关。探测失败一律降级为空——派发不能因此挂掉。
    """
    if os.environ.get("BOSUN_ENGINE_ROSTER_HINT", "1").strip().lower() in _FALSE:
        return ""
    others = other_engine_names(engine)
    if not others:
        return ""
    names = "、".join(others)
    # 开了受控子任务就引导走 spawn（Bosun 看得见、管得了、计额度）；
    # 关了才退回「你自己在终端里调」的说法，免得提示一个用不了的接口。
    if subtasks.enabled():
        return SUBTASK_TEMPLATE.format(engines=names)
    return ENGINE_ROSTER_TEMPLATE.format(engines=names)


def with_report_directive(
    prompt: str,
    engine: str | None = None,
    artifact_required: bool = False,
) -> str:
    """给派发给 agent 的 prompt 追加引擎清单提示 + 收尾回报约定；空 prompt 不加。

    传 engine 时走 harness registry 取当前生效版本（自演进入口，故障自动回退
    静态常量），并在其**之前**插入引擎清单提示——收尾约定必须留在最末，尾部
    显著性是 #524 的既有结论，不能被别的提示挤走。
    不传 engine 保持旧行为一字不变：backfill 脚本按它比对历史会话的首条消息。

    注入点刻意收在这一个函数里：build_argv / build_resume_argv / sdk_session /
    scheduler 的 codex 会话认领都经由它，改在别处会让「派发的 prompt」与
    「认领时比对的 prompt」不一致，导致 codex 会话永远认领不到。
    """
    if not (prompt or "").strip():
        return prompt
    if engine:
        artifact = ORCHESTRATION_REPORT_ADDENDUM if artifact_required else ""
        return f"{prompt}{engine_roster_hint(engine)}{harness_adapter.directive_for(engine)}{artifact}"
    return f"{prompt}{REPORT_DIRECTIVE}"


def uses_stdin_prompt(engine: str) -> bool:
    """交互模式的 prompt 是否要在 TUI 起来后经 PTY stdin 投递(实测 kimi 0.34
    交互模式不接受位置参数 prompt: `kimi "文本"` 报 unknown command)。"""
    return engine == "kimi"


def kimi_session_arg(session_uid: str) -> str:
    """kimi -S 只认完整 `session_<uuid>`(实测裸 uuid 报 not found)；库里统一存裸
    uuid(与其它引擎同构、复用 UUID 校验)，拼参数时补前缀。"""
    if session_uid.startswith("session_"):
        return session_uid
    return f"session_{session_uid}"


def build_argv(
    engine: str,
    prompt: str,
    auto_approve: bool,
    session_uid: str | None = None,
    artifact_required: bool = False,
    model_override: str | None = None,
    reasoning_override: str | None = None,
) -> list[str]:
    """首次运行。会话 id 由引擎自行生成、运行后捕获(--session-id 不落盘，无法 resume)。"""
    prompt = with_report_directive(prompt, engine=engine, artifact_required=artifact_required)
    if engine == "cc":
        argv = engine_settings.with_claude_runtime_args(
            [CLAUDE_BIN], model_override, reasoning_override
        )
        if auto_approve:
            argv.append("--dangerously-skip-permissions")
        argv.append(prompt)
        return argv
    if engine == "codex":
        argv = engine_settings.with_codex_runtime_args(
            [CODEX_BIN], model_override, reasoning_override
        )
        if auto_approve:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        argv.append(prompt)
        return argv
    if engine == "omp":
        argv = engine_settings.with_omp_runtime_args(
            [OMP_BIN], model_override, reasoning_override
        )
        if auto_approve:
            argv.append("--auto-approve")
        argv.append(prompt)
        return argv
    if engine == "kimi":
        # prompt 不进 argv：由 PtySession 在 TUI 就绪后粘贴提交(uses_stdin_prompt)。
        argv = engine_settings.with_kimi_runtime_args([KIMI_BIN], model_override)
        if auto_approve:
            argv.append("--yolo")
        return argv
    raise ValueError(f"unknown engine: {engine}")


def build_resume_argv(
    engine: str,
    session_uid: str,
    prompt: str,
    auto_approve: bool,
    artifact_required: bool = False,
    model_override: str | None = None,
    reasoning_override: str | None = None,
) -> list[str]:
    """恢复已有会话继续。prompt 为空则只加载上下文等待输入。"""
    prompt = with_report_directive(prompt, engine=engine, artifact_required=artifact_required)
    if engine == "cc":
        argv = engine_settings.with_claude_runtime_args(
            [CLAUDE_BIN], model_override, reasoning_override
        )
        if auto_approve:
            argv.append("--dangerously-skip-permissions")
        argv += ["--resume", session_uid]
        if prompt:
            argv.append(prompt)
        return argv
    if engine == "codex":
        argv = engine_settings.with_codex_runtime_args(
            [CODEX_BIN, "resume", session_uid], model_override, reasoning_override
        )
        if auto_approve:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        if prompt:
            argv.append(prompt)
        return argv
    if engine == "omp":
        argv = engine_settings.with_omp_runtime_args(
            [OMP_BIN], model_override, reasoning_override
        )
        if auto_approve:
            argv.append("--auto-approve")
        argv += ["--resume", session_uid]
        if prompt:
            argv.append(prompt)
        return argv
    if engine == "kimi":
        argv = engine_settings.with_kimi_runtime_args([KIMI_BIN], model_override)
        if auto_approve:
            argv.append("--yolo")
        argv += ["-S", kimi_session_arg(session_uid)]
        return argv
    raise ValueError(f"unknown engine: {engine}")


def build_headless_argv(engine: str, prompt: str, auto_approve: bool = True, json_out: bool = False) -> list[str]:
    """headless 一次性执行(跑完即退出)，用于自愈循环里的修复/复审。
    json_out=True 时请求结构化输出以便解析 token 用量。"""
    if engine == "cc":
        argv = [CLAUDE_BIN, "-p"]
        if json_out:
            argv += ["--output-format", "json"]
        argv = engine_settings.with_claude_runtime_args(argv)
        if auto_approve:
            argv.append("--dangerously-skip-permissions")
        argv.append(prompt)
        return argv
    if engine == "codex":
        argv = engine_settings.with_codex_runtime_args([CODEX_BIN, "exec"])
        if json_out:
            argv.append("--json")
        if auto_approve:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        argv.append(prompt)
        return argv
    if engine == "omp":
        argv = engine_settings.with_omp_runtime_args([OMP_BIN, "-p"])
        if json_out:
            # omp 的结构化输出是逐事件 NDJSON(--mode json)，不是单个 JSON 对象。
            argv += ["--mode", "json"]
        if auto_approve:
            argv.append("--auto-approve")
        argv.append(prompt)
        return argv
    if engine == "kimi":
        # kimi 的 -p 是带值选项(非位置参数)；stream-json 为逐行 JSONL。
        # 实测 -p 不能与 --yolo 同用(报 Cannot combine)，headless 模式自带审批语义，
        # auto_approve 在这里没有对应参数。
        argv = engine_settings.with_kimi_runtime_args([KIMI_BIN, "-p", prompt])
        if json_out:
            argv += ["--output-format", "stream-json"]
        return argv
    raise ValueError(f"unknown engine: {engine}")


def build_audit_argv(engine: str, audit_prompt: str) -> list[str]:
    """整体分析用 headless 模式跑，拿结构化输出。"""
    if engine == "cc":
        argv = engine_settings.with_claude_runtime_args([CLAUDE_BIN, "-p"])
        return [*argv, audit_prompt]
    if engine == "codex":
        return engine_settings.with_codex_runtime_args([CODEX_BIN, "exec", audit_prompt])
    if engine == "omp":
        return engine_settings.with_omp_runtime_args([OMP_BIN, "-p", audit_prompt])
    if engine == "kimi":
        return engine_settings.with_kimi_runtime_args([KIMI_BIN, "-p", audit_prompt])
    raise ValueError(f"unknown engine: {engine}")
