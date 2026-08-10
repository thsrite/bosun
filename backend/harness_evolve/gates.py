"""Gate-0 静态护栏（循环外纯代码，非 LLM）与 Gate-1 灰度统计判定。

Gate-0 的禁区（forbidden_patterns / protected_keys）由宿主注入——核心包不硬编码
任何宿主语义，但校验逻辑本身永远在演进循环之外，编辑白名单不含本模块。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Set, Tuple

from .models import OPS, SURFACES, HarnessEdit


@dataclass(frozen=True)
class Gate0Config:
    allowed_surfaces: Tuple[str, ...] = SURFACES
    allowed_ops: Tuple[str, ...] = OPS
    forbidden_patterns: List[str] = field(default_factory=list)
    protected_keys: Set[Tuple[str, str]] = field(default_factory=set)
    max_value_len: int = 2000
    max_directive_total: int = 8000
    max_edits: int = 4


def gate0_validate(edits: Iterable[HarnessEdit], snapshot: dict, config: Gate0Config) -> List[str]:
    """返回违规清单；空列表 = 通过。校验在模拟应用的副本上进行，不改动传入快照。"""
    edits = list(edits)
    violations: List[str] = []
    if len(edits) > config.max_edits:
        violations.append(f"编辑数 {len(edits)} 超过上限 {config.max_edits}")
    patterns = [re.compile(p, re.IGNORECASE) for p in config.forbidden_patterns]
    sim = {surface: dict(entries) for surface, entries in snapshot.items()}
    for i, edit in enumerate(edits):
        tag = f"edit[{i}] {edit.surface}/{edit.entry_key}"
        if edit.surface not in config.allowed_surfaces:
            violations.append(f"{tag}: surface 不在白名单")
            continue
        if edit.op not in config.allowed_ops:
            violations.append(f"{tag}: op 不在白名单")
            continue
        if (edit.surface, edit.entry_key) in config.protected_keys:
            violations.append(f"{tag}: 受保护条目禁止修改")
            continue
        text = f"{edit.entry_key}\n{edit.new_value or ''}"
        hit = next((p.pattern for p in patterns if p.search(text)), None)
        if hit:
            violations.append(f"{tag}: 命中禁区模式 {hit}")
            continue
        if edit.new_value is not None and len(edit.new_value) > config.max_value_len:
            violations.append(f"{tag}: 值长度 {len(edit.new_value)} 超过上限 {config.max_value_len}")
            continue
        entries = sim.setdefault(edit.surface, {})
        exists = edit.entry_key in entries
        if edit.op == "add" and exists:
            violations.append(f"{tag}: add 的 key 已存在")
        elif edit.op in ("update", "remove") and not exists:
            violations.append(f"{tag}: {edit.op} 的 key 不存在")
        elif edit.op == "remove":
            del entries[edit.entry_key]
        else:
            entries[edit.entry_key] = edit.new_value or ""
    total = sum(len(v) for v in sim.get("directive", {}).values())
    if total > config.max_directive_total:
        violations.append(f"应用后 directive 总长 {total} 超过预算 {config.max_directive_total}")
    return violations


def _wilson(successes: int, n: int, z: float) -> Tuple[float, float]:
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - margin, center + margin


def proportion_diff_ci(control_success: int, control_n: int,
                       shadow_success: int, shadow_n: int,
                       z: float = 1.96) -> Tuple[float, float]:
    """shadow − control 成功率之差的 Newcombe 置信区间（基于 Wilson 分数区间）。"""
    p1, (l1, u1) = control_success / control_n, _wilson(control_success, control_n, z)
    p2, (l2, u2) = shadow_success / shadow_n, _wilson(shadow_success, shadow_n, z)
    diff = p2 - p1
    lo = diff - math.sqrt((p2 - l2) ** 2 + (u1 - p1) ** 2)
    hi = diff + math.sqrt((u2 - p2) ** 2 + (p1 - l1) ** 2)
    return lo, hi


def gray_verdict(control_success: int, control_n: int,
                 shadow_success: int, shadow_n: int,
                 min_samples: int = 20, z: float = 1.96) -> str:
    """灰度判定：insufficient | pass | fail | inconclusive。

    pass/fail 均要求统计显著（区间不跨零）；inconclusive 交宿主策略层
    （继续放量观察或人工裁决），核心包不越权替宿主拍板。
    """
    if min(control_n, shadow_n) < min_samples:
        return "insufficient"
    lo, hi = proportion_diff_ci(control_success, control_n, shadow_success, shadow_n, z)
    if lo > 0:
        return "pass"
    if hi < 0:
        return "fail"
    return "inconclusive"
