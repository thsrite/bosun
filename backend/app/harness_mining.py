"""Weakness Mining 的 Bosun 落地：失败任务 → signature 聚类落库 → harness 编辑提案。

流程（方案见 docs/spec-self-evolving-harness.md §3.2/3.3）：
  mine()    失败任务日志尾部 → core.extract_signature → 精确聚类 → harness_cluster 表
            （每次全量重算窗口内数据，幂等；表是挖掘产物缓存而非台账）
  propose() actionable 簇 + 当前 harness 快照 → LLM 生成 harness_edit 提案 → proposal 表
            （与 reflection 提案共用人审 UI；Gate-0 在应用时强制校验）
  apply_harness_edit()  人审通过后：Gate-0 → registry.propose → promote 生效（MVP 无灰度，
            人审即 Gate-2；可随时 rollback）

依赖方向：本模块 → reflection(去重助手)/harness_adapter/core；reflection 不得反向 import
本模块（harness_edit 动作由 routers/reflection.py 分发过来）。
"""
from __future__ import annotations

import json
import re
import time

# 同 harness_adapter：运行态路径根是 backend/，tests 路径根是仓库根
try:
    from harness_evolve import (Episode, Gate0Config, HarnessEdit, canonicalize_signatures,
                                cluster_signatures, extract_signature, gate0_validate)
except ModuleNotFoundError:
    from backend.harness_evolve import (Episode, Gate0Config, HarnessEdit, canonicalize_signatures,
                                        cluster_signatures, extract_signature, gate0_validate)

from . import db, log_archive, reflection, sdk_run
from .harness_adapter import REPORT_KEY, get_registry

_CLUSTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_cluster (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engine TEXT NOT NULL,
    cause TEXT NOT NULL,
    causal TEXT NOT NULL,
    mechanism TEXT NOT NULL,
    episode_ids TEXT NOT NULL,
    support INTEGER NOT NULL,
    created_at REAL NOT NULL
);
"""

# Gate-0 禁区：收尾回报契约与任务凭证不许被任何提案触碰（DGM 教训——自演进系统
# 最先攻击的就是自己的评测/回报通道）。
GATE0 = Gate0Config(
    forbidden_patterns=[r"bosun[-_]report", r"BOSUN_TASK", r"不要.{0,6}回报", r"跳过.{0,6}收尾"],
    protected_keys={("directive", REPORT_KEY)},
    max_value_len=600,
    max_directive_total=4000,
    max_edits=1,  # 一条提案一个编辑，粒度最小化
)

_ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|[\r\x00]")
_TRANSCRIPT_BYTES = 60_000
_TRANSCRIPT_CHARS = 6_000


class _SdkLLM:
    """core.LLMClient 的 Bosun 实现：claude 无头、禁工具、单轮（同 reflection 的做法）。"""

    def __init__(self, cwd: str, account_key: str):
        self.cwd = cwd
        self.account_key = account_key

    def complete_json(self, prompt: str) -> str:
        res = sdk_run.run_sync(prompt, self.cwd, auto_approve=True, timeout=300,
                               extra_opts={"allowed_tools": [], "max_turns": 1})
        reflection._account_tokens(self.account_key, res.get("tokens") or 0)
        return res.get("text") or ""


def _transcript_tail(log_path: str | None) -> str:
    if not log_path:
        return ""
    raw = log_archive.read_tail(log_path, _TRANSCRIPT_BYTES) or b""
    text = _ANSI.sub(b"", raw).decode("utf-8", errors="ignore")
    return text[-_TRANSCRIPT_CHARS:]


def _failed_rows(days: int, limit: int):
    """失败口径 = 自报 failed ∪ 跑完但未回报（收尾契约失守，实测库里最主要的失败形态；
    needs_input 是正常反问、cancelled/interrupted 是人为中止，均不算）。"""
    cutoff = time.time() - days * 86400
    return db.query(
        "SELECT id, engine, report_summary, report_result, log_path FROM task "
        "WHERE (report_result='failed' OR (report_result IS NULL AND status='done')) "
        "AND COALESCE(ended_at, created_at) >= ? "
        "ORDER BY id DESC LIMIT ?", (cutoff, limit))


# 挖掘运行状态（供 UI 轮询；单机单进程，模块级即可）
state: dict = {"running": False, "last_run_at": None, "last_clusters": None,
               "last_proposals": None, "last_error": None}


def run_round(cwd: str) -> None:
    """跑一轮完整的挖掘+提案（在后台线程调用），状态记入 state。"""
    state.update(running=True, last_error=None)
    try:
        clusters = mine(cwd)
        proposals = propose(cwd)
        state.update(last_clusters=clusters, last_proposals=proposals)
    except Exception as e:
        state.update(last_error=str(e)[:200])
        raise
    finally:
        state.update(running=False, last_run_at=time.time())


def mine(cwd: str, days: int = 14, limit: int = 100) -> int:
    """跑一轮挖掘，harness_cluster 全量重建。返回簇数。"""
    db.get_conn().executescript(_CLUSTER_SCHEMA)
    llm = _SdkLLM(cwd, "harness_mining_tokens_total")
    per_engine: dict[str, list] = {}
    for row in _failed_rows(days, limit):
        summary = row["report_summary"] or (
            "(任务结束但未按收尾约定回报)" if row["report_result"] is None else "")
        episode = Episode(id=str(row["id"]), engine=row["engine"] or "claude", succeeded=False,
                          summary=summary)
        sig = extract_signature(llm, episode, _transcript_tail(row["log_path"]))
        if sig:
            per_engine.setdefault(episode.engine, []).append((episode.id, sig))
    now = time.time()
    db.execute("DELETE FROM harness_cluster")
    n = 0
    for engine, items in per_engine.items():
        # 二遍归一化：独立抽取的措辞各异，先同义归并再精确聚类，否则全是 support=1
        items = canonicalize_signatures(llm, items)
        for c in cluster_signatures(items):
            db.execute(
                "INSERT INTO harness_cluster(engine,cause,causal,mechanism,episode_ids,support,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (engine, c.signature.cause, c.signature.causal, c.signature.mechanism,
                 json.dumps(c.episode_ids), c.support, now))
            n += 1
    return n


_PROPOSE_PROMPT = """你是 agent harness 演进提案器。以下失败簇均判定为 harness_gap（可通过修改派发给 agent 的指令避免）。
针对每个簇最多提 1 条提案，总共最多 {max_n} 条。只返回一个 JSON 数组，不要任何其它文字。
每条提案：{{"title": "簇的一句话概括", "rationale": "为什么这条编辑能治这个簇（引用证据）",
 "action": {{"type": "harness_edit", "engine": "<簇的 engine>", "surface": "directive",
  "op": "add|update|remove", "entry_key": "3xx-短横线小写slug", "value": "指令文本",
  "cluster_id": <簇 id>}}}}

硬约束：
- 编辑必须针对簇的失败机制，最小化：一条提案只动一个条目；新增条目 key 用 3xx- 前缀。
- 禁止触碰收尾回报约定（{report_key}）；禁止大段重写；value ≤500 字。
- 指令写给执行任务的 agent 看，要具体可执行，不要"注意""尽量"这类空话。

当前 directive 条目（key: 内容摘要）：
{snapshot}

失败簇：
{clusters}
"""


def propose(cwd: str, min_support: int = 2, max_n: int = 2) -> int:
    """基于 actionable 簇生成 harness_edit 提案入 proposal 表。返回新增提案数。"""
    rows = db.query("SELECT * FROM harness_cluster WHERE causal='harness_gap' AND support>=? "
                    "ORDER BY support DESC LIMIT 3", (min_support,))
    if not rows:
        return 0
    registry = get_registry()
    snapshots = {}
    for r in rows:
        engine = r["engine"]
        if engine not in snapshots:
            active = registry.active(engine)
            snap = registry.snapshot(active.id).get("directive", {}) if active else {}
            snapshots[engine] = "\n".join(f"- {k}: {v[:80]}" for k, v in sorted(snap.items()))
    clusters_text = "\n".join(
        f"- 簇 id={r['id']} engine={r['engine']} support={r['support']} "
        f"判因: {r['cause']} | 机制: {r['mechanism']} | 涉及任务: {r['episode_ids']}"
        for r in rows)
    prompt = _PROPOSE_PROMPT.format(max_n=max_n, report_key=REPORT_KEY,
                                    snapshot="\n\n".join(snapshots.values()) or "(空)",
                                    clusters=clusters_text)
    text = _SdkLLM(cwd, "harness_mining_tokens_total").complete_json(prompt)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        return 0
    try:
        items = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return 0
    n, now = 0, time.time()
    for it in items[:max_n]:
        if not isinstance(it, dict) or not it.get("title") or not isinstance(it.get("action"), dict):
            continue
        if it["action"].get("type") != "harness_edit":
            continue
        if reflection._is_duplicate_title(str(it["title"]), now):
            continue
        db.execute(
            "INSERT INTO proposal(title,rationale,action,status,created_at) VALUES(?,?,?,'pending',?)",
            (str(it["title"])[:200], "[harness] " + str(it.get("rationale", ""))[:970],
             json.dumps(it["action"], ensure_ascii=False), now))
        n += 1
    return n


def read_current_value(action: dict) -> str | None:
    """harness_edit 应用前的旧值（供 proposal.old_value 闭环回看）。"""
    try:
        registry = get_registry()
        active = registry.active(str(action.get("engine") or ""))
        if not active:
            return None
        return registry.snapshot(active.id).get(str(action.get("surface") or ""), {}) \
            .get(str(action.get("entry_key") or ""))
    except Exception:
        return None


def apply_harness_edit(action: dict) -> tuple[bool, str, int | None]:
    """人审通过后的应用：Gate-0 校验 → 新版本 → 晋升生效。失败一律不落任何变更。"""
    engine = str(action.get("engine") or "").strip().lower()
    registry = get_registry()
    if not registry.active(engine):
        return False, f"引擎 {engine} 的 harness 未初始化（尚无派发记录），仅作建议", None
    edit = HarnessEdit(surface=str(action.get("surface") or ""), op=str(action.get("op") or ""),
                       entry_key=str(action.get("entry_key") or ""),
                       new_value=(str(action["value"]) if action.get("value") is not None else None),
                       provenance=f"cluster-{action.get('cluster_id')}")
    snapshot = registry.snapshot(registry.active(engine).id)
    violations = gate0_validate([edit], snapshot, GATE0)
    if violations:
        return False, "Gate-0 拒绝：" + "；".join(violations), None
    version = registry.promote(registry.propose(engine, [edit]).id)
    return True, f"harness[{engine}] v{version.version} 已生效（{edit.op} {edit.surface}/{edit.entry_key}，可回滚）", None
