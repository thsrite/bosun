"""harness_evolve —— 自我演进 Harness 框架核心包（零宿主依赖，可整体抽离）。

闭环：Weakness Mining → Harness Proposal → Proposal Validation（Gate-0/1/2）→ 晋升/回滚。
方案与架构决策见 docs/spec-self-evolving-harness.md。
"""
from .gates import Gate0Config, gate0_validate, gray_verdict, proportion_diff_ci
from .models import (OPS, SURFACES, EditError, Episode, HarnessEdit, HarnessVersion,
                     OutcomeRecord, RenderedHarness)
from .protocols import ApprovalChannel, HarnessRenderer, LLMClient, OutcomeSignals, TraceSource
from .registry import Registry
from .store import Store

__all__ = [
    "OPS", "SURFACES", "EditError", "Episode", "HarnessEdit", "HarnessVersion",
    "OutcomeRecord", "RenderedHarness", "Registry", "Store",
    "Gate0Config", "gate0_validate", "gray_verdict", "proportion_diff_ci",
    "TraceSource", "OutcomeSignals", "HarnessRenderer", "ApprovalChannel", "LLMClient",
]
