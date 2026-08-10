"""宿主接入合同：任何项目接入本框架需实现的 5 个 Protocol。

依赖方向单向：宿主 → 核心包；核心包对宿主一无所知。
唯一的主动侵入点是派发前调用 Registry.choose() 选 harness 版本。
"""
from __future__ import annotations

from typing import Iterable, Optional, Protocol

from .models import Episode, HarnessEdit, OutcomeRecord, RenderedHarness


class TraceSource(Protocol):
    """提供失败 episode 及其执行痕迹（Weakness Mining 的输入）。"""

    def failed_episodes(self, days: int) -> Iterable[Episode]: ...

    def read_transcript(self, episode: Episode, max_chars: int) -> str: ...


class OutcomeSignals(Protocol):
    """提供独立于 agent 自报的成败信号（Gate-1 的输入，防自报口径被演进钻空）。"""

    def outcomes(self, version_id: int, since: str) -> Iterable[OutcomeRecord]: ...


class HarnessRenderer(Protocol):
    """把渲染结果落进派发链路（prompt tail / argv / skill 文件等宿主自己的形态）。"""

    def apply(self, engine: str, rendered: RenderedHarness) -> None: ...


class ApprovalChannel(Protocol):
    """人审通道：展示待审编辑（带证据与灰度数据），回收 promote/reject 决定。"""

    def submit(self, version_id: int, edits: list, evidence: dict) -> None: ...

    def decision(self, version_id: int) -> Optional[str]:
        """返回 'promote' | 'reject' | None（未决）。"""
        ...


class LLMClient(Protocol):
    """单轮、无工具、返回 JSON 文本的 LLM 调用（signature 抽取与提案生成用）。"""

    def complete_json(self, prompt: str) -> str: ...
