"""引擎适配层：把任务转成要在 pty 里跑的 argv。

cc  = Claude Code CLI (`claude`)：支持 --session-id 钉住会话 id、--resume 恢复
codex = OpenAI Codex CLI (`codex`)：resume <uuid> 恢复；会话 id 需运行后捕获
omp = Oh My Pi CLI (`omp`)：--resume <id 前缀> 恢复；会话 id 需运行后捕获
"""
from __future__ import annotations

from . import engine_settings, skills_install
from .config import CLAUDE_BIN, CODEX_BIN, OMP_BIN

ENGINES = {"cc", "codex", "omp"}

# 收尾回报约定：光把 bosun-report skill 装上，模型收尾时未必想得起来调，
# 尤其是以「反问用户」结尾的那一轮。这里在派发的 prompt 末尾显式要求一次。
REPORT_DIRECTIVE = (
    "\n\n---\n"
    "[Bosun 收尾约定] 本轮工作结束前——无论是任务完成、失败无法继续，"
    "还是需要反问用户才能往下走——都必须收尾：先把本轮的完整结论/分析正文"
    "打印到终端（用户只看终端输出，不要只说「见上」或把结论只塞进汇报参数里），"
    "再调用 bosun-report skill 回报状态(done / failed / needs_input)；"
    "summary 只是一句话摘要，不能替代正文。未打印正文或未回报都不算收尾。"
)


def with_report_directive(prompt: str) -> str:
    """给派发给 agent 的 prompt 追加收尾回报约定；空 prompt(只加载上下文)不加。"""
    if not (prompt or "").strip():
        return prompt
    return f"{prompt}{REPORT_DIRECTIVE}"


def build_argv(engine: str, prompt: str, auto_approve: bool, session_uid: str | None = None) -> list[str]:
    """首次运行。会话 id 由引擎自行生成、运行后捕获(--session-id 不落盘，无法 resume)。"""
    skills_install.ensure_engine_skills(engine)
    prompt = with_report_directive(prompt)
    if engine == "cc":
        argv = engine_settings.with_claude_runtime_args([CLAUDE_BIN])
        if auto_approve:
            argv.append("--dangerously-skip-permissions")
        argv.append(prompt)
        return argv
    if engine == "codex":
        argv = engine_settings.with_codex_runtime_args([CODEX_BIN])
        if auto_approve:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        argv.append(prompt)
        return argv
    if engine == "omp":
        argv = engine_settings.with_omp_runtime_args([OMP_BIN])
        if auto_approve:
            argv.append("--auto-approve")
        argv.append(prompt)
        return argv
    raise ValueError(f"unknown engine: {engine}")


def build_resume_argv(engine: str, session_uid: str, prompt: str, auto_approve: bool) -> list[str]:
    """恢复已有会话继续。prompt 为空则只加载上下文等待输入。"""
    skills_install.ensure_engine_skills(engine)
    prompt = with_report_directive(prompt)
    if engine == "cc":
        argv = engine_settings.with_claude_runtime_args([CLAUDE_BIN])
        if auto_approve:
            argv.append("--dangerously-skip-permissions")
        argv += ["--resume", session_uid]
        if prompt:
            argv.append(prompt)
        return argv
    if engine == "codex":
        argv = engine_settings.with_codex_runtime_args([CODEX_BIN, "resume", session_uid])
        if auto_approve:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        if prompt:
            argv.append(prompt)
        return argv
    if engine == "omp":
        argv = engine_settings.with_omp_runtime_args([OMP_BIN])
        if auto_approve:
            argv.append("--auto-approve")
        argv += ["--resume", session_uid]
        if prompt:
            argv.append(prompt)
        return argv
    raise ValueError(f"unknown engine: {engine}")


def build_headless_argv(engine: str, prompt: str, auto_approve: bool = True, json_out: bool = False) -> list[str]:
    """headless 一次性执行(跑完即退出)，用于自愈循环里的修复/复审。
    json_out=True 时请求结构化输出以便解析 token 用量。"""
    skills_install.ensure_engine_skills(engine)
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
    raise ValueError(f"unknown engine: {engine}")


def build_audit_argv(engine: str, audit_prompt: str) -> list[str]:
    """整体分析用 headless 模式跑，拿结构化输出。"""
    skills_install.ensure_engine_skills(engine)
    if engine == "cc":
        argv = engine_settings.with_claude_runtime_args([CLAUDE_BIN, "-p"])
        return [*argv, audit_prompt]
    if engine == "codex":
        return engine_settings.with_codex_runtime_args([CODEX_BIN, "exec", audit_prompt])
    if engine == "omp":
        return engine_settings.with_omp_runtime_args([OMP_BIN, "-p", audit_prompt])
    raise ValueError(f"unknown engine: {engine}")
