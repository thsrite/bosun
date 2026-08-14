"""引擎适配层：把任务转成要在 pty 里跑的 argv。

claude  = Claude Code CLI (`claude`)：支持 --session-id 钉住会话 id、--resume 恢复
codex = OpenAI Codex CLI (`codex`)：resume <uuid> 恢复；会话 id 需运行后捕获
omp = Oh My Pi CLI (`omp`)：--resume <id 前缀> 恢复；会话 id 需运行后捕获
kimi = Kimi Code CLI (`kimi`)：-S session_<uuid> 恢复；交互模式不收位置参数 prompt，
       首条指令由 PtySession 以括号粘贴写入 TUI（见 uses_stdin_prompt）
"""
from __future__ import annotations

from . import agent_skills, engine_settings
from .config import CLAUDE_BIN, CODEX_BIN, KIMI_BIN, OMP_BIN
from .directives import (  # noqa: F401  兼容既有 engines.REPORT_DIRECTIVE 引用
    REPORT_DIRECTIVE,
)

CODING_ENGINES = {"claude", "codex", "omp", "kimi"}
ENGINES = {*CODING_ENGINES, "browser"}

_ENGINE_ALIASES = {
    "cc": "claude",  # 旧 API / 历史自动化兼容一个版本周期
    "claude": "claude",
    "claude-code": "claude",
    "claude code": "claude",
}


def normalize_engine_id(engine: str) -> str:
    """把用户可见的 Claude Code 名称归一化为 Bosun 内部引擎键。"""
    return _ENGINE_ALIASES.get(engine, engine)


def with_report_directive(
    prompt: str,
    engine: str | None = None,
    artifact_required: bool = False,
) -> str:
    """派发时同步当前引擎 skills，但保持用户 prompt 逐字不变。

    不传 engine 仅供历史会话 backfill，保留旧版追加 REPORT_DIRECTIVE 的比对口径。
    """
    if not (prompt or "").strip():
        return prompt
    if engine:
        # Bosun 能力由按需安装的 skills 暴露；不再改写用户原始提示。技能安装失败
        # 也只在回合结束后的缺报催报里降级为完整内联约定，避免污染任务要求。
        agent_skills.ensure_for_dispatch(engine)
        return prompt
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
    if engine == "claude":
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
    if engine == "claude":
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
    if engine == "claude":
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
    if engine == "claude":
        argv = engine_settings.with_claude_runtime_args([CLAUDE_BIN, "-p"])
        return [*argv, audit_prompt]
    if engine == "codex":
        return engine_settings.with_codex_runtime_args([CODEX_BIN, "exec", audit_prompt])
    if engine == "omp":
        return engine_settings.with_omp_runtime_args([OMP_BIN, "-p", audit_prompt])
    if engine == "kimi":
        return engine_settings.with_kimi_runtime_args([KIMI_BIN, "-p", audit_prompt])
    raise ValueError(f"unknown engine: {engine}")
