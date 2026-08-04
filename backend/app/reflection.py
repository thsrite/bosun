"""反思循环(自进化)：cc 读 Bosun 运行数据 → 产出改进提案 → 人批准 → 白名单应用。

安全：提案的 action 只能是有限、可逆的白名单动作；其余为纯建议(不自动应用)。
"""
from __future__ import annotations

import json
import re
import time

from . import db, sdk_run
from .services import stats as stats_service


def _rows(sql, p=()):
    return [dict(r) for r in db.query(sql, p)]


def gather_data(days: int = 30) -> dict:
    dashboard = stats_service.build_dashboard(days)
    projects = _rows("SELECT id, name, path FROM project ORDER BY id")
    policies = _rows(
        "SELECT po.id, po.name, po.scope, po.scope_arg, po.interval_minutes, po.enabled, p.name project "
        "FROM policy po JOIN project p ON p.id=po.project_id"
    )
    proposal_counts = _rows("SELECT status, COUNT(*) n FROM proposal GROUP BY status")
    settings = {
        "quota_block_pct": db.get_setting("quota_block_pct", "90"),
        "mute_threshold": db.get_setting("mute_threshold", "3"),
        "max_concurrent": db.get_setting("max_concurrent", "3"),
    }
    reflection_settings = {
        "auto_enabled": db.get_setting("reflection_auto_enabled", "1"),
        "interval_minutes": db.get_setting("reflection_interval_minutes", "1440"),
        "min_pending_gap": db.get_setting("reflection_min_pending_gap", "5"),
        "last_run_at": db.get_setting("reflection_last_run_at"),
        "last_skip_reason": db.get_setting("reflection_last_skip_reason", ""),
    }
    # 闭环反馈信号: 让 LLM 看到失败原因、被否决的方向、上次改动前后对比
    recent_failures = _rows(
        "SELECT title, report_summary, engine FROM task "
        "WHERE status='failed' AND COALESCE(deleted,0)=0 ORDER BY created_at DESC LIMIT 8"
    )
    recent_dismissed = _rows(
        "SELECT title, dismiss_reason FROM proposal WHERE status='dismissed' "
        "ORDER BY COALESCE(dismissed_at, created_at) DESC LIMIT 8"
    )
    recent_applied = _rows(
        "SELECT title, action, old_value, metrics_before, applied_at FROM proposal "
        "WHERE status='applied' AND applied_at IS NOT NULL ORDER BY applied_at DESC LIMIT 5"
    )
    # 顺序有意义: 白名单动作依赖的 id 表(projects/policies)与反馈信号靠前,
    # dashboard 体量最大放最后 —— reflect() 截断时优先保住这些而非砍掉 id 表。
    return {
        "projects": projects,
        "policies": policies,
        "settings": settings,
        "reflection_settings": reflection_settings,
        "proposal_counts": proposal_counts,
        "recent_failures": recent_failures,
        "recent_dismissed": recent_dismissed,
        "recent_applied": recent_applied,
        "dashboard": dashboard,
    }



_PROMPT = """你是 Bosun（一个编排 Claude Code/Codex 的本地工具）的运维分析师。
下面是它的自诊断 dashboard 数据。请产出 3-6 条**具体、可执行**的改进提案，聚焦：
发现本系统自己的问题、给出初步解决方案、降低 token 浪费、提高自愈成功率、优化定时策略、减少反复失败。

每条提案是一个对象 {{"title": "..", "rationale": "为什么(引用数据)", "action": <可选> }}。
action 只能是以下白名单之一（拿不准就省略 action，表示纯建议）：
- {{"type":"set_setting","key":"quota_block_pct|mute_threshold|max_concurrent","value":<整数>}}
- {{"type":"set_policy","policy_id":<id>,"field":"scope|scope_arg|interval_minutes|enabled","value":<值>}}
- {{"type":"create_task","project_id":<projects 中的 id>,"engine":"cc|codex|omp","title":"..","prompt":"..","priority":1-10}}

需要改代码、修复项目问题或调查某个项目时，优先使用 create_task；采纳后只会创建 draft 任务，不会自动执行。
只有安全的全局配置调整才使用 set_setting 或 set_policy。

利用反馈信号提高提案质量：
- recent_failures 给出失败任务的错误摘要 —— 针对具体错误提方案，不要泛泛而谈。
- recent_dismissed 是用户已否决的方向 —— 不要重复提出，除非有新证据表明问题已恶化。
- recent_applied 含上次改动的 old_value 与应用前健康快照 —— 对照当前数据判断上次改动是否奏效，无效则提修正而非同类重复。

只返回一个 JSON 数组，不要任何其它文字。

运行数据：
{data}
"""


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def _is_duplicate_title(title: str, now: float) -> bool:
    norm = _normalize_title(title)
    if not norm:
        return True
    cutoff = now - 7 * 86400
    # 排除 dismissed: 用户否决 ≠ 问题消失, 恶化后应能再次提出(否则被 7 天窗口误杀)。
    # 否决信号改由 gather_data 的 recent_dismissed 回流给 LLM, 让它主动避免无脑重复。
    for row in db.query(
        "SELECT title,status,created_at,applied_at FROM proposal "
        "WHERE (status='pending' OR created_at>=? OR COALESCE(applied_at,0)>=?) "
        "AND status != 'dismissed'",
        (cutoff, cutoff),
    ):
        if _normalize_title(row["title"]) == norm:
            return True
    return False


def _account_tokens(key: str, toks: int) -> None:
    """把自诊断自身(反思/审计)烧的 token 累加入账, 否则系统对自己的开销是盲区。"""
    if toks:
        prev = int(db.get_setting(key, "0") or 0)
        db.set_setting(key, str(prev + int(toks)))


def reflect(cwd: str, origin: str = "manual") -> int:
    """跑一次反思，生成提案入库。返回新增提案数。"""
    data = json.dumps(gather_data(), ensure_ascii=False)[:12000]
    # 纯 JSON 生成任务: 禁工具 + 单轮, 省 token 并去掉 bypassPermissions 的执行面
    res = sdk_run.run_sync(
        _PROMPT.format(data=data), cwd, auto_approve=True, timeout=300,
        extra_opts={"allowed_tools": [], "max_turns": 1},
    )
    _account_tokens("reflection_tokens_total", res.get("tokens") or 0)
    text = res.get("text") or ""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        return 0
    try:
        items = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return 0
    n = 0
    now = time.time()
    for it in items[:8]:
        if not isinstance(it, dict) or not it.get("title"):
            continue
        if _is_duplicate_title(str(it["title"]), now):
            continue
        action = it.get("action")
        db.execute(
            "INSERT INTO proposal(title,rationale,action,status,created_at) VALUES(?,?,?, 'pending', ?)",
            (
                str(it["title"])[:200],
                (f"[{origin}] " if origin != "manual" else "") + str(it.get("rationale", ""))[:980],
                json.dumps(action, ensure_ascii=False) if isinstance(action, dict) else None,
                now,
            ),
        )
        n += 1
    return n


_SETTING_KEYS = {"quota_block_pct", "mute_threshold", "max_concurrent"}
_POLICY_FIELDS = {"scope", "scope_arg", "interval_minutes", "enabled"}
_TASK_ENGINES = {"cc", "codex", "omp"}

# 值域护栏: 自进化路径与 UI 侧同一套下限, 防 LLM 提的畸形值绕过护栏设成危险配置。
_SETTING_BOUNDS = {
    "quota_block_pct": (1, 100),   # 0=自锁死, >100=配额失效烧额度
    "mute_threshold": (1, 50),
    "max_concurrent": (1, 20),     # 无上限=一次性拉起海量会话
}
_POLICY_INT_BOUNDS = {"interval_minutes": (5, 10080)}  # 0=每 60s 背靠背触发 autopilot
_POLICY_SCOPES = {"full", "recent", "commit"}


def _coerce_clamp_int(value, lo: int, hi: int) -> int | None:
    """数值→夹取到 [lo,hi]；非数字→None(拒绝, 降级为纯建议)。"""
    if isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def _coerce_enabled(value) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if value in (0, 1, "0", "1"):
        return int(value)
    return None


def _bounded_int(value, default: int, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(low, min(high, n))


def _create_task(action: dict) -> tuple[bool, str, int | None]:
    project_id = action.get("project_id")
    if not db.query_one("SELECT id FROM project WHERE id=?", (project_id,)):
        return False, "目标项目不存在，仅作建议", None

    engine = str(action.get("engine") or "codex").strip().lower()
    if engine not in _TASK_ENGINES:
        return False, "任务引擎不在白名单，仅作建议", None

    prompt = str(action.get("prompt") or "").strip()
    if not prompt:
        return False, "任务 prompt 为空，仅作建议", None

    title = str(action.get("title") or prompt).strip()[:80]
    priority = _bounded_int(action.get("priority"), 5, 1, 10)
    task_id = db.execute(
        "INSERT INTO task(project_id,engine,prompt,title,priority,auto_approve,kind,status,created_at) "
        "VALUES(?,?,?,?,?,0,'task','draft',?)",
        (project_id, engine, prompt, title, priority, time.time()),
    )
    return True, f"已创建 draft 任务 #{task_id}", task_id


def _infer_followup_project_id(text: str) -> int | None:
    projects = _rows("SELECT id, name, path FROM project ORDER BY id")
    if not projects:
        return None
    if len(projects) == 1:
        return int(projects[0]["id"])

    haystack = text.lower()
    for p in sorted(projects, key=lambda row: len(str(row.get("path") or "")), reverse=True):
        path = str(p.get("path") or "").lower()
        if path and path in haystack:
            return int(p["id"])
    for p in sorted(projects, key=lambda row: len(str(row.get("name") or "")), reverse=True):
        name = str(p.get("name") or "").strip().lower()
        if len(name) >= 3 and name in haystack:
            return int(p["id"])
    for p in projects:
        name = str(p.get("name") or "").strip().lower()
        path = str(p.get("path") or "").strip().lower()
        # 仓库物理路径/项目名仍是 deckhand（品牌改名未改物理目录），两者都匹配
        if name in ("bosun", "deckhand") or path.endswith(("/bosun", "/deckhand")):
            return int(p["id"])

    row = db.query_one(
        "SELECT p.id FROM project p "
        "LEFT JOIN task t ON t.project_id=p.id AND COALESCE(t.deleted,0)=0 "
        "GROUP BY p.id ORDER BY COUNT(t.id) DESC, p.id ASC LIMIT 1"
    )
    return int(row["id"]) if row else int(projects[0]["id"])


def create_followup_task(title: str, rationale: str | None) -> tuple[bool, str, int | None]:
    """Turn an unapplied advice proposal into a safe draft task."""
    clean_title = (title or "").strip() or "自进化提案跟进"
    clean_rationale = (rationale or "").strip()
    project_id = _infer_followup_project_id(f"{clean_title}\n{clean_rationale}")
    if project_id is None:
        return False, "无可用项目，仅作建议", None

    prompt = (
        "请处理这个 Bosun 自进化提案。\n\n"
        f"提案标题：\n{clean_title}\n\n"
        f"提案依据：\n{clean_rationale or '(无)'}\n\n"
        "要求：\n"
        "1. 先核实问题是否仍存在。\n"
        "2. 如果需要改代码，做最小安全修改并运行相关验证。\n"
        "3. 完成后说明处理结果。"
    )
    task_id = db.execute(
        "INSERT INTO task(project_id,engine,prompt,title,priority,auto_approve,kind,status,created_at) "
        "VALUES(?,?,?,?,?,0,'task','draft',?)",
        (project_id, "codex", prompt, clean_title[:80], 5, time.time()),
    )
    return True, f"已创建 draft 跟进任务 #{task_id}", task_id


def read_current_value(action: dict) -> str | None:
    """读取 set_setting/set_policy 应用前的当前值，记入 proposal.old_value 便于事后回看/回滚。"""
    t = action.get("type")
    if t == "set_setting" and action.get("key") in _SETTING_KEYS:
        return db.get_setting(action["key"])
    if t == "set_policy" and action.get("field") in _POLICY_FIELDS:
        row = db.query_one(f"SELECT {action['field']} v FROM policy WHERE id=?", (action.get("policy_id"),))
        return str(row["v"]) if row else None
    return None


def apply_action(action: dict) -> tuple[bool, str, int | None]:
    """执行白名单动作。返回 (是否应用, 说明, 关联任务 id)。"""
    t = action.get("type")
    if t == "set_setting" and action.get("key") in _SETTING_KEYS:
        key = action["key"]
        lo, hi = _SETTING_BOUNDS[key]
        val = _coerce_clamp_int(action.get("value"), lo, hi)
        if val is None:
            return False, f"{key} 值非法（需 {lo}-{hi} 的整数），仅作建议", None
        db.set_setting(key, val)
        return True, f"设置 {key} = {val}", None
    if t == "set_policy" and action.get("field") in _POLICY_FIELDS:
        pid = action.get("policy_id")
        if not db.query_one("SELECT id FROM policy WHERE id=?", (pid,)):
            return False, "目标策略不存在，仅作建议", None
        field = action["field"]
        raw = action.get("value")
        if field in _POLICY_INT_BOUNDS:
            lo, hi = _POLICY_INT_BOUNDS[field]
            val = _coerce_clamp_int(raw, lo, hi)
            if val is None:
                return False, f"策略 {field} 值非法（需 {lo}-{hi} 的整数），仅作建议", None
        elif field == "enabled":
            val = _coerce_enabled(raw)
            if val is None:
                return False, "策略 enabled 需为 0/1，仅作建议", None
        elif field == "scope":
            if raw not in _POLICY_SCOPES:
                return False, "策略 scope 非白名单值，仅作建议", None
            val = raw
        else:  # scope_arg: 自由文本(如 commit ref)
            val = str(raw) if raw is not None else None
        db.execute(f"UPDATE policy SET {field}=? WHERE id=?", (val, pid))
        return True, f"策略 #{pid}.{field} = {val}", None
    if t == "create_task":
        return _create_task(action)
    return False, "非白名单动作，仅作建议", None
