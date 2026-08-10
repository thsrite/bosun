"""Weakness Mining：失败 trace → signature 三元组 → 精确匹配聚类。

方法取自 Self-Harness（arXiv:2606.09498）：signature = (cause 终态判因,
causal 因果地位, mechanism 抽象机制)，按三元组精确一致聚类而非 embedding
相似度，避免把表面症状误当可复用机制；causal 维度把「harness 该背的锅」与
「模型/环境/用户的锅」分开——只有 harness_gap 簇才值得进提案。
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from .models import Episode
from .protocols import LLMClient

CAUSAL_KINDS = ("harness_gap", "model_limit", "env_issue", "user_input")

_SIG_PROMPT = """你是 agent 执行失败分析器。根据以下失败任务的信息，输出唯一一个 JSON 对象（无其它文字）：
{{"cause": "终态失败判因，一句短语", "causal": "harness_gap|model_limit|env_issue|user_input", "mechanism": "抽象失败机制，规范化短语（同类失败必须给出完全相同的措辞）", "evidence": "trace 中支撑判断的关键片段摘录"}}

causal 判定标准：harness_gap=通过修改给 agent 的指令/策略/技能/记忆可避免；model_limit=模型能力不足，改 harness 无济于事；env_issue=环境/依赖/网络问题；user_input=任务本身信息不足或用户输入导致。

任务摘要：{summary}
引擎：{engine}
执行痕迹（截取）：
{transcript}
"""


@dataclass(frozen=True)
class FailureSignature:
    cause: str
    causal: str
    mechanism: str


@dataclass(frozen=True)
class FailureCluster:
    signature: FailureSignature
    episode_ids: List[str]

    @property
    def support(self) -> int:
        return len(self.episode_ids)


def _parse_json(text: str) -> Optional[dict]:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _parse_json_array(text: str) -> Optional[list]:
    text = text.strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def extract_signature(llm: LLMClient, episode: Episode, transcript: str) -> Optional[FailureSignature]:
    """单条失败 episode 抽 signature；任何异常/格式问题返回 None（挖掘容错，不外溢）。"""
    prompt = _SIG_PROMPT.format(summary=episode.summary or "(无)", engine=episode.engine,
                                transcript=transcript or "(无)")
    try:
        data = _parse_json(llm.complete_json(prompt))
    except Exception:
        return None
    if not data:
        return None
    cause, causal, mechanism = _norm(data.get("cause")), _norm(data.get("causal")), _norm(data.get("mechanism"))
    if not cause or not mechanism or causal not in CAUSAL_KINDS:
        return None
    return FailureSignature(cause, causal, mechanism)


_CANON_PROMPT = """你是失败模式归并器。以下是从不同失败任务独立抽取的 signature 清单，
同一失败机制常因措辞不同而无法精确匹配。请把描述同一机制的条目归并，输出唯一一个 JSON 数组（无其它文字）：
[{{"cause": "规范判因措辞", "causal": "harness_gap|model_limit|env_issue|user_input", "mechanism": "规范机制措辞", "members": [条目编号...]}}]

硬约束：causal 不同的条目绝不可归并到一组；每个编号最多出现在一组；确实独特的条目单独成组；不确定是否同机制时不归并。

条目清单：
{lines}
"""


def canonicalize_signatures(llm: LLMClient,
                            items: Iterable[Tuple[str, FailureSignature]]
                            ) -> List[Tuple[str, FailureSignature]]:
    """第二遍归一化：独立抽取导致同机制措辞各异、精确匹配聚不拢（实测 30 条全 support=1），
    用一次 LLM 调用把同义 signature 合并为规范措辞。任何异常/非法输出回退原样（宁可不聚，
    不错聚）；跨 causal 归并一律拒绝，该成员保留原 signature。"""
    items = list(items)
    if len(items) < 2:
        return items
    lines = "\n".join(f"{i}. causal={s.causal} | cause={s.cause} | mechanism={s.mechanism}"
                      for i, (_, s) in enumerate(items))
    try:
        groups = _parse_json_array(llm.complete_json(_CANON_PROMPT.format(lines=lines)))
    except Exception:
        return items
    if not groups:
        return items
    mapping: dict = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        cause, causal, mechanism = (_norm(group.get("cause")), _norm(group.get("causal")),
                                    _norm(group.get("mechanism")))
        if not cause or not mechanism or causal not in CAUSAL_KINDS:
            continue
        canonical = FailureSignature(cause, causal, mechanism)
        for member in group.get("members") or []:
            if (isinstance(member, int) and 0 <= member < len(items)
                    and member not in mapping and items[member][1].causal == causal):
                mapping[member] = canonical
    return [(episode_id, mapping.get(i, sig)) for i, (episode_id, sig) in enumerate(items)]


def cluster_signatures(items: Iterable[Tuple[str, FailureSignature]]) -> List[FailureCluster]:
    """(episode_id, signature) → 按三元组精确一致聚类，支持度降序（同支持度按首现序）。"""
    groups: "OrderedDict[FailureSignature, List[str]]" = OrderedDict()
    for episode_id, signature in items:
        groups.setdefault(signature, []).append(episode_id)
    return sorted((FailureCluster(sig, ids) for sig, ids in groups.items()),
                  key=lambda c: -c.support)


def actionable_clusters(clusters: Iterable[FailureCluster], min_support: int = 2) -> List[FailureCluster]:
    """只有 harness_gap 且达到支持度阈值的簇才进提案；其余留给宿主单独呈现。"""
    return [c for c in clusters if c.signature.causal == "harness_gap" and c.support >= min_support]
