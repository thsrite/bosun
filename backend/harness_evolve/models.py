"""核心数据模型。零 Bosun 依赖：本包任何模块不得 import backend.app。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

SURFACES = ("directive", "policy", "skill", "memory")
OPS = ("add", "update", "remove")
# 版本状态机：shadow →(promote)→ active →(被更新版晋升)→ superseded
#                                      └(rollback)→ rolled_back
STATUSES = ("active", "shadow", "superseded", "rolled_back")


class EditError(ValueError):
    """编辑/版本操作违反语义约束（非法 surface、key 冲突、状态机违例等）。"""


@dataclass(frozen=True)
class HarnessEdit:
    surface: str
    op: str
    entry_key: str
    new_value: Optional[str] = None
    old_value: Optional[str] = None
    provenance: Optional[str] = None


@dataclass(frozen=True)
class HarnessVersion:
    id: int
    engine: str
    version: int
    status: str
    parent_id: Optional[int]
    created_at: str


@dataclass(frozen=True)
class RenderedHarness:
    version_id: int
    directive_text: str
    policy: dict
    skills: dict
    memory: list


@dataclass(frozen=True)
class Episode:
    """一次任务执行的抽象（宿主 TraceSource 提供）。"""

    id: str
    engine: str
    succeeded: Optional[bool]
    transcript_path: Optional[str] = None
    summary: str = ""
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OutcomeRecord:
    """独立成败信号（宿主 OutcomeSignals 提供，供 Gate-1 统计）。"""

    episode_id: str
    signal: str
    value: float
