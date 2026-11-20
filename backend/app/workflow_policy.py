"""白盒工作流策略引擎、Diff 反作弊扫描器与可视化拓扑图投影器（纯确定性代码，无 LLM）。"""
from __future__ import annotations

from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from . import acceptance, config, db, specialist

TRUST_LEVELS: dict[int, dict[str, str]] = {
    0: {
        "code": "L0",
        "name": "影子观察",
        "summary": "只读分析与影子记录，所有交付与合并由人执行",
    },
    1: {
        "code": "L1",
        "name": "协助开发",
        "summary": "人工确认任务单与合并，AI 在隔离分支开发与测试",
    },
    2: {
        "code": "L2",
        "name": "自动提审",
        "summary": "就绪单自动派工、复测与提审，人工决定合并",
    },
    3: {
        "code": "L3",
        "name": "有限自动合并",
        "summary": "白名单内、低风险且可回退改动自动入队合并",
    },
    4: {
        "code": "L4",
        "name": "授权域自治",
        "summary": "授权域内按预设样例签收并自动合并回告",
    },
}

DEFAULT_POLICY = {
    "trust_level": 1,
    "diff_integrity_gate": True,
    "require_product_signoff": True,
    "auto_merge_globs": [],
}

# 反作弊正则：只检查 diff 新增行（以 '+' 开头，排除 '+++' 文件头）
_ANTI_CHEAT_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "test_skip",
        "新增跳过测试标记",
        re.compile(r"\b(?:pytest\.mark\.skip(?:if)?|unittest\.skip(?:If|Unless)?|(?:it|test|describe)\.skip)\b"),
    ),
    (
        "lint_suppress",
        "新增静态检查忽略标记",
        re.compile(r"(?:#\s*noqa\b|@ts-ignore\b|@ts-expect-error\b|eslint-disable\b)"),
    ),
    (
        "empty_except",
        "新增空异常吞没",
        re.compile(r"\bexcept\b[^:]*:\s*(?:pass|\.\.\.)\s*(?:#.*)?$"),
    ),
)


def get_project_policy(project_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM orchestration_policy WHERE project_id=?", (project_id,))
    if row is None:
        meta = TRUST_LEVELS[DEFAULT_POLICY["trust_level"]]
        return {
            "project_id": project_id,
            **DEFAULT_POLICY,
            "level_code": meta["code"],
            "level_name": meta["name"],
            "level_summary": meta["summary"],
            "updated_at": None,
        }
    level = max(0, min(4, int(row["trust_level"])))
    meta = TRUST_LEVELS[level]
    try:
        globs = json.loads(row["auto_merge_globs"] or "[]")
        if not isinstance(globs, list):
            globs = []
    except (TypeError, json.JSONDecodeError):
        globs = []
    return {
        "project_id": project_id,
        "trust_level": level,
        "diff_integrity_gate": bool(row["diff_integrity_gate"]),
        "require_product_signoff": bool(row["require_product_signoff"]),
        "auto_merge_globs": [str(g).strip() for g in globs if str(g).strip()],
        "level_code": meta["code"],
        "level_name": meta["name"],
        "level_summary": meta["summary"],
        "updated_at": row["updated_at"],
    }


def update_project_policy(
    project_id: int,
    *,
    trust_level: int | None = None,
    diff_integrity_gate: bool | None = None,
    require_product_signoff: bool | None = None,
    auto_merge_globs: list[str] | None = None,
) -> dict[str, Any]:
    current = get_project_policy(project_id)
    new_level = current["trust_level"] if trust_level is None else int(trust_level)
    if not 0 <= new_level <= 4:
        raise ValueError("信任等级必须为 0～4")
    new_diff_gate = current["diff_integrity_gate"] if diff_integrity_gate is None else bool(diff_integrity_gate)
    new_signoff = current["require_product_signoff"] if require_product_signoff is None else bool(require_product_signoff)
    if auto_merge_globs is None:
        new_globs = current["auto_merge_globs"]
    else:
        new_globs = [str(g).strip() for g in auto_merge_globs if str(g).strip()]
    now = time.time()
    db.execute(
        "INSERT INTO orchestration_policy(project_id,trust_level,diff_integrity_gate,require_product_signoff,auto_merge_globs,updated_at) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(project_id) DO UPDATE SET "
        "trust_level=excluded.trust_level, diff_integrity_gate=excluded.diff_integrity_gate, "
        "require_product_signoff=excluded.require_product_signoff, auto_merge_globs=excluded.auto_merge_globs, "
        "updated_at=excluded.updated_at",
        (project_id, new_level, int(new_diff_gate), int(new_signoff), json.dumps(new_globs, ensure_ascii=False), now),
    )
    return get_project_policy(project_id)


def evaluate_merge_policy(policy: dict[str, Any], changed_files: list[str] | None = None) -> dict[str, Any]:
    """根据信任等级评估合并动作是否需要人工放行。"""
    level = int(policy.get("trust_level", 1))
    meta = TRUST_LEVELS.get(level, TRUST_LEVELS[1])
    if level == 0:
        return {
            "mode": "shadow",
            "auto_merge": False,
            "requires_human": True,
            "reason": f"{meta['code']} {meta['name']}：仅影子推演，所有提交与合并由人工执行",
        }
    if level in (1, 2):
        return {
            "mode": "human_merge",
            "auto_merge": False,
            "requires_human": True,
            "reason": f"{meta['code']} {meta['name']}：AI 完成验证与提审，合并需仓库维护者确认",
        }
    if level == 3:
        globs = policy.get("auto_merge_globs") or []
        if not globs:
            return {
                "mode": "human_merge",
                "auto_merge": False,
                "requires_human": True,
                "reason": "L3 有限自动合并：尚未配置自动合并白名单路径，默认交人工合并",
            }
        if changed_files:
            outside = [p for p in changed_files if not any(fnmatchcase(p, g) for g in globs)]
            if outside:
                return {
                    "mode": "human_merge",
                    "auto_merge": False,
                    "requires_human": True,
                    "reason": f"L3 有限自动合并：改动超出白名单（{outside[0]} 等 {len(outside)} 个文件），转人工合并",
                }
        return {
            "mode": "auto_merge_scoped",
            "auto_merge": True,
            "requires_human": False,
            "reason": "L3 有限自动合并：改动全部命中白名单且检查通过，允许自动入队合并",
        }
    return {
        "mode": "autonomous",
        "auto_merge": True,
        "requires_human": False,
        "reason": "L4 授权域自治：满足全部门禁与预设样例后可自动签收并合并",
    }


def scan_diff_integrity(
    project_path: str,
    baseline: str,
    candidate: str,
    allowed_write_globs: list[str] | None = None,
) -> dict[str, Any]:
    """确定性扫描 baseline..candidate 的路径越界与反作弊模式。"""
    if not specialist.SHA.fullmatch(baseline or "") or not specialist.SHA.fullmatch(candidate or ""):
        return {
            "checked": False,
            "passed": True,
            "changed_files": [],
            "out_of_scope": [],
            "findings": [],
            "summary": "缺少完整基线或候选提交 SHA，跳过 Diff 完整性扫描",
        }
    try:
        names_proc = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", baseline, candidate],
            cwd=project_path,
            capture_output=True,
            timeout=10,
        )
        if names_proc.returncode != 0:
            return {
                "checked": False,
                "passed": False,
                "changed_files": [],
                "out_of_scope": [],
                "findings": [{"kind": "git_error", "label": "无法读取 Git 差异", "file": "", "snippet": ""}],
                "summary": "无法读取 baseline..candidate 的 Git 差异",
            }
        changed_files = [p for p in names_proc.stdout.decode(errors="replace").split("\0") if p]
        out_of_scope: list[str] = []
        if allowed_write_globs:
            out_of_scope = [
                p for p in changed_files
                if not any(fnmatchcase(p, g) for g in allowed_write_globs)
            ]

        diff_proc = subprocess.run(
            ["git", "diff", "-U0", "--no-color", baseline, candidate],
            cwd=project_path,
            capture_output=True,
            timeout=15,
        )
        findings: list[dict[str, str]] = []
        current_file = ""
        for raw_line in diff_proc.stdout.decode(errors="replace").splitlines():
            if raw_line.startswith("+++ b/"):
                current_file = raw_line[6:]
                continue
            if not raw_line.startswith("+") or raw_line.startswith("+++"):
                continue
            added = raw_line[1:]
            for kind, label, pattern in _ANTI_CHEAT_PATTERNS:
                if pattern.search(added):
                    findings.append({
                        "kind": kind,
                        "label": label,
                        "file": current_file,
                        "snippet": added.strip()[:140],
                    })
                    if len(findings) >= 20:
                        break
            if len(findings) >= 20:
                break

        passed = not out_of_scope and not findings
        reasons: list[str] = []
        if out_of_scope:
            reasons.append(f"越界修改 {len(out_of_scope)} 个文件（如 {out_of_scope[0]}）")
        if findings:
            reasons.append(f"命中 {len(findings)} 处检查规避/吞异常标记（如 {findings[0]['label']}）")
        return {
            "checked": True,
            "passed": passed,
            "changed_files": changed_files,
            "out_of_scope": out_of_scope,
            "findings": findings,
            "summary": "Diff 完整性检查通过" if passed else "；".join(reasons),
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "checked": False,
            "passed": False,
            "changed_files": [],
            "out_of_scope": [],
            "findings": [{"kind": "git_error", "label": str(exc), "file": "", "snippet": ""}],
            "summary": f"Diff 扫描异常：{exc}",
        }


def list_signoffs(run_id: int) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM orchestration_signoff WHERE run_id=? ORDER BY id DESC",
        (run_id,),
    )
    return [dict(r) for r in rows]


def run_workspace_dir(run_id: int) -> Path:
    """每个编排主任务独占一个共享产物黑板目录（Pass-by-Reference 数据面）。"""
    path = config.DATA_DIR / "runs" / f"run-{run_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def summarize_artifact(artifact_text: str | None, summary: str | None = None, max_chars: int = 160) -> str:
    clean_summary = (summary or "").strip()
    if clean_summary:
        return clean_summary[:max_chars]
    for raw in (artifact_text or "").splitlines():
        line = raw.strip().lstrip("#-*>` ").strip()
        if not line or line.startswith("```"):
            continue
        return line[:max_chars]
    return "已完成本阶段产物提交"


def persist_step_artifact(
    run_id: int,
    position: int,
    attempt: int,
    step_name: str,
    artifact_text: str,
    summary: str | None = None,
) -> dict[str, Any]:
    """将阶段产物落盘到共享黑板目录，并提取结构化 JSON 与更新 manifest.json。"""
    base_dir = run_workspace_dir(run_id)
    md_name = f"step-{position}-r{attempt}.md"
    md_path = base_dir / md_name
    raw_bytes = (artifact_text or "").encode("utf-8")
    md_path.write_bytes(raw_bytes)
    sha = hashlib.sha256(raw_bytes).hexdigest()[:12]

    json_path_str: str | None = None
    structured: dict[str, Any] | None = None
    if "bosun-acceptance" in (artifact_text or ""):
        try:
            structured = acceptance.record(artifact_text)
        except ValueError:
            structured = None
    if structured is not None:
        json_name = f"step-{position}-r{attempt}.json"
        json_path = base_dir / json_name
        json_path.write_text(json.dumps(structured, ensure_ascii=False, indent=2), encoding="utf-8")
        json_path_str = str(json_path)

    entry = {
        "position": position,
        "attempt": attempt,
        "step_name": step_name,
        "md_path": str(md_path),
        "json_path": json_path_str,
        "bytes": len(raw_bytes),
        "sha": sha,
        "summary": summarize_artifact(artifact_text, summary),
        "updated_at": time.time(),
    }

    manifest_path = base_dir / "manifest.json"
    manifest: dict[str, Any] = {"run_id": run_id, "run_dir": str(base_dir), "artifacts": []}
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("artifacts"), list):
                manifest = loaded
        except (OSError, json.JSONDecodeError):
            pass
    artifacts = [
        a for a in manifest["artifacts"]
        if not (a.get("position") == position and a.get("attempt") == attempt)
    ]
    artifacts.append(entry)
    artifacts.sort(key=lambda a: (int(a.get("position", 0)), int(a.get("attempt", 0))))
    manifest["run_dir"] = str(base_dir)
    manifest["artifacts"] = artifacts
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry


def list_run_artifacts(run_id: int) -> dict[str, Any]:
    base_dir = config.DATA_DIR / "runs" / f"run-{run_id}"
    manifest_path = base_dir / "manifest.json"
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {"run_dir": str(base_dir), "artifacts": data.get("artifacts") or []}
        except (OSError, json.JSONDecodeError):
            pass
    return {"run_dir": str(base_dir), "artifacts": []}


def build_tickets_progress(
    trace_data: dict[str, Any],
    spec: dict[str, Any] | None,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """实时构建拆分后的 Ticket 清单与完成进度。

    优先级：
    1. 需求单显式声明的 tickets[]（结合验收表与执行状态实时更新）
    2. 验收表中的业务必验项 rows[]
    3. 编排拆出的各角色子任务步骤
    """
    run = trace_data["run"]
    steps = run.get("steps") or []
    planned_steps = plan.get("steps") or steps
    actual_by_pos = {int(s["position"]): s for s in steps}
    acceptance_rows = (run.get("acceptance") or {}).get("rows") or []
    row_by_id = {str(r.get("id")): r for r in acceptance_rows}

    def _map_row_status(st: str | None) -> str:
        if st == "通过":
            return "done"
        if st == "失败":
            return "failed"
        return "running" if run.get("status") == "running" else "pending"

    items: list[dict[str, Any]] = []
    source = "steps"

    explicit_tickets = spec.get("tickets") if isinstance(spec, dict) and isinstance(spec.get("tickets"), list) else None
    if explicit_tickets:
        source = "explicit"
        for idx, raw in enumerate(explicit_tickets, 1):
            if not isinstance(raw, dict):
                continue
            tid = str(raw.get("id") or f"T{idx}")
            title = str(raw.get("title") or raw.get("name") or tid)
            owner = str(raw.get("owner") or "开发 AI")
            depends_on = [str(d) for d in (raw.get("depends_on") or []) if str(d).strip()]
            criteria_ids = [str(c) for c in (raw.get("criteria_ids") or []) if str(c).strip()]

            # 根据关联必验项实时推导状态
            derived = str(raw.get("status") or "").strip().lower()
            if derived not in ("pending", "running", "blocked", "done", "failed"):
                if criteria_ids and row_by_id:
                    statuses = [_map_row_status(row_by_id.get(cid, {}).get("status")) for cid in criteria_ids]
                    if any(s == "failed" for s in statuses):
                        derived = "failed"
                    elif all(s == "done" for s in statuses):
                        derived = "done"
                    elif run.get("status") == "running":
                        derived = "running"
                    else:
                        derived = "pending"
                elif run.get("status") == "done":
                    derived = "done"
                elif run.get("status") == "running":
                    derived = "running"
                else:
                    derived = "pending"

            items.append({
                "id": tid,
                "title": title,
                "owner": owner,
                "status": derived,
                "depends_on": depends_on,
                "criteria_ids": criteria_ids,
                "task_id": None,
                "detail": str(raw.get("detail") or raw.get("scope") or ""),
            })
    elif acceptance_rows:
        source = "acceptance"
        for idx, row in enumerate(acceptance_rows, 1):
            rid = str(row.get("id") or f"A{idx}")
            st = _map_row_status(row.get("status"))
            items.append({
                "id": rid,
                "title": str(row.get("expected") or row.get("input") or rid),
                "owner": "验收项",
                "status": st,
                "depends_on": [],
                "criteria_ids": [rid],
                "task_id": None,
                "detail": str(row.get("actual") or row.get("input") or ""),
            })
    else:
        source = "steps"
        for st in planned_steps:
            pos = int(st["position"])
            actual = actual_by_pos.get(pos)
            raw_status = actual["status"] if actual else "pending"
            mapped = (
                "done" if raw_status == "done"
                else "failed" if raw_status in ("failed", "cancelled", "interrupted")
                else "blocked" if raw_status in ("waiting_input", "offline")
                else "running" if raw_status in ("running", "queued")
                else "pending"
            )
            items.append({
                "id": f"S{pos}",
                "title": str(st.get("name") or f"步骤 {pos}"),
                "owner": str(actual["engine"] if actual else st.get("engine", "")),
                "status": mapped,
                "depends_on": [f"S{pos - 1}"] if pos > 1 else [],
                "criteria_ids": [],
                "task_id": actual["task_id"] if actual else None,
                "detail": str((actual or {}).get("summary") or ""),
            })

    total = len(items)
    done = sum(1 for it in items if it["status"] == "done")
    running = sum(1 for it in items if it["status"] == "running")
    failed = sum(1 for it in items if it["status"] == "failed")
    blocked = sum(1 for it in items if it["status"] == "blocked")
    percent = round(done * 100 / total) if total > 0 else 0
    return {
        "source": source,
        "total": total,
        "done": done,
        "running": running,
        "failed": failed,
        "blocked": blocked,
        "percent": percent,
        "items": items,
    }


def _extract_spec_and_version(steps: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    spec: dict[str, Any] | None = None
    version = ""
    for step in steps:
        art = step.get("output_artifact") or ""
        if "bosun-acceptance" not in art:
            continue
        try:
            rec = acceptance.record(art)
        except ValueError:
            continue
        if step.get("position") == 1 and spec is None:
            spec = rec
        if step.get("position") == 2 and isinstance(rec.get("version"), str):
            version = rec["version"].strip()
    return spec, version


def _step_lane(position: int, total: int, name: str) -> str:
    lower = name.lower()
    if position == 1 or any(k in lower for k in ("需求", "收件", "分诊", "方案", "分析", "规划")):
        return "intake"
    if position >= total and any(k in lower for k in ("交付", "合并", "发布", "汇报", "验收")):
        return "deliver"
    return "build"


def _step_gates(
    step: dict[str, Any] | None,
    position: int,
    plan: dict[str, Any],
    decisions: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    acceptance_view: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    if step is None:
        return [{"id": "scheduled", "label": "待派发", "status": "pending", "detail": "前序节点完成后按需创建"}]

    has_output = bool((step.get("output_artifact") or "").strip())
    gates.append({
        "id": "artifact",
        "label": "阶段产物",
        "status": "passed" if has_output else ("running" if step.get("status") == "running" else "pending"),
        "detail": "已提交结构化阶段产物" if has_output else "尚未提交阶段产物",
    })

    if plan.get("acceptance_gate"):
        has_block = "bosun-acceptance" in (step.get("output_artifact") or "")
        gates.append({
            "id": "schema",
            "label": "验收协议",
            "status": "passed" if has_block else ("pending" if not has_output else "failed"),
            "detail": "已包含 bosun-acceptance 结构化协议" if has_block else "等待提交 bosun-acceptance 协议区块",
        })

    if plan.get("remote_checks") and position in (2, 3):
        step_checks = [c for c in checks if c.get("position") == position and c.get("attempt") == step.get("attempt")]
        if not step_checks:
            gates.append({
                "id": "remote_check",
                "label": "开发自测" if position == 2 else "独立复测",
                "status": "pending",
                "detail": "尚无本轮远程实测回执",
            })
        else:
            has_fail = any(c.get("state") == "failed" for c in step_checks)
            all_pass = all(c.get("state") == "passed" for c in step_checks)
            gates.append({
                "id": "remote_check",
                "label": "开发自测" if position == 2 else "独立复测",
                "status": "failed" if has_fail else ("passed" if all_pass else "running"),
                "detail": f"本轮共 {len(step_checks)} 条远程回执（最新 #{step_checks[0]['id']}）",
            })

    if plan.get("specialist_checks") and position in (2, 3):
        step_checks = [c for c in checks if c.get("position") == position and c.get("attempt") == step.get("attempt")]
        with_report = [c for c in step_checks if c.get("outcomes")]
        gates.append({
            "id": "specialist",
            "label": "专项报告",
            "status": "passed" if with_report else "pending",
            "detail": f"已绑定 {len(with_report)} 份专项报告与证据包" if with_report else "等待带用例结果的专项证据包",
        })

    step_decisions = [d for d in decisions if d.get("position") == position and d.get("attempt") == step.get("attempt")]
    if step_decisions:
        latest = step_decisions[-1]
        res = latest.get("result")
        gates.append({
            "id": "handoff_gate",
            "label": "交接门禁",
            "status": "passed" if res == "done" else ("failed" if res in ("rejected", "rework", "failed") else "waiting"),
            "detail": latest.get("reason") or latest.get("basis") or f"编排判定：{res}",
        })

    if position == 3 and acceptance_view and acceptance_view.get("rows"):
        review_row = next((r for r in acceptance_view["rows"] if r.get("id") == "代码评审"), None)
        if review_row:
            st = review_row.get("status")
            gates.append({
                "id": "code_review",
                "label": "代码评审",
                "status": "passed" if st == "通过" else ("failed" if st == "失败" else "pending"),
                "detail": review_row.get("actual") or "等待代码评审结论",
            })
    return gates


def build_workflow_graph(
    trace_data: dict[str, Any],
    policy: dict[str, Any],
    signoffs: list[dict[str, Any]],
) -> dict[str, Any]:
    """将运行轨迹投影为前端白盒可视化画布所需的标准化图谱。"""
    run = trace_data["run"]
    messages = trace_data.get("messages", [])
    decisions = trace_data.get("decisions", [])
    checks = trace_data.get("checks", [])
    try:
        plan = json.loads(run.get("definition_snapshot") or "{}")
    except (TypeError, json.JSONDecodeError):
        plan = {}
    planned_steps = plan.get("steps") or run.get("steps") or []
    actual_by_pos = {s["position"]: s for s in run.get("steps", [])}
    acceptance_view = run.get("acceptance")
    spec, candidate_version = _extract_spec_and_version(run.get("steps", []))

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # 1. 系统入口节点（确定性控制面）
    nodes.append({
        "id": "sys:intake",
        "kind": "system",
        "lane": "intake",
        "position": 0,
        "title": "线索收件与策略",
        "subtitle": f"{policy['level_code']} · {policy['level_name']}",
        "status": "passed" if run.get("status") != "draft" else "pending",
        "attempt": 1,
        "rework_count": 0,
        "duration_sec": 0,
        "engine": "system",
        "model": "",
        "task_id": None,
        "gates": [
            {
                "id": "policy_loaded",
                "label": "信任授权",
                "status": "passed",
                "detail": f"{policy['level_code']} {policy['level_name']}：{policy['level_summary']}",
            },
            {
                "id": "diff_gate",
                "label": "Diff 护栏",
                "status": "passed" if policy.get("diff_integrity_gate") else "skipped",
                "detail": "已开启路径越界与反作弊扫描" if policy.get("diff_integrity_gate") else "未开启",
            },
        ],
    })

    # 2. 各角色编排节点
    total_steps = len(planned_steps)
    prev_node_id = "sys:intake"
    for idx, st in enumerate(planned_steps):
        pos = int(st["position"])
        actual = actual_by_pos.get(pos)
        node_id = f"step:{pos}"
        status = actual["status"] if actual else "pending"
        # 若最近一次同轮次判定被拦截且仍在运行，高亮门禁拦截状态
        step_decisions = [d for d in decisions if d["position"] == pos and actual and d["attempt"] == actual["attempt"]]
        latest_dec = step_decisions[-1] if step_decisions else None
        display_status = status
        if latest_dec and latest_dec["result"] == "rejected" and status in ("running", "waiting_input"):
            display_status = "rejected"

        duration_sec = None
        if actual and actual.get("started_at"):
            end_ts = actual.get("ended_at") or time.time()
            duration_sec = max(0, int(end_ts - actual["started_at"]))

        gates = _step_gates(actual, pos, plan, decisions, checks, acceptance_view)
        nodes.append({
            "id": node_id,
            "kind": "agent",
            "lane": _step_lane(pos, total_steps, st.get("name", "")),
            "position": pos,
            "title": st.get("name") or f"步骤 {pos}",
            "subtitle": actual["engine"] if actual else st.get("engine", ""),
            "status": display_status,
            "attempt": actual["attempt"] if actual else 0,
            "rework_count": actual["rework_count"] if actual else 0,
            "duration_sec": duration_sec,
            "engine": actual["engine"] if actual else st.get("engine", ""),
            "model": actual["model"] if actual else st.get("model", ""),
            "task_id": actual["task_id"] if actual else None,
            "summary": actual.get("summary") if actual else None,
            "gates": gates,
        })

        # 主线连线
        handoff_msgs = [m for m in messages if m.get("to_position") == pos and m.get("kind") == "handoff"]
        edge_active = run.get("current_position") == pos and run.get("status") in ("running", "queued")
        edges.append({
            "id": f"e:{prev_node_id}->{node_id}",
            "source": prev_node_id,
            "target": node_id,
            "kind": "handoff",
            "label": f"交接 ×{len(handoff_msgs)}" if len(handoff_msgs) > 1 else "交接",
            "count": len(handoff_msgs) or 1,
            "active": edge_active,
            "message_ids": [m["id"] for m in handoff_msgs],
        })
        prev_node_id = node_id

    # 3. 人工产品验收节点 + 系统合并回告节点（白盒闭环）
    latest_signoff = signoffs[0] if signoffs else None
    last_step = actual_by_pos.get(int(planned_steps[-1]["position"])) if planned_steps else None
    qa_passed = bool(last_step and last_step.get("status") == "done")
    if latest_signoff:
        if latest_signoff["decision"] == "approved":
            signoff_status = "passed"
        else:
            signoff_status = "rework"
    elif qa_passed and policy.get("require_product_signoff", True):
        signoff_status = "waiting_input"
    elif not policy.get("require_product_signoff", True):
        signoff_status = "skipped"
    else:
        signoff_status = "pending"

    signoff_node_id = "human:signoff"
    nodes.append({
        "id": signoff_node_id,
        "kind": "human",
        "lane": "deliver",
        "position": total_steps + 1,
        "title": "产品验收卡",
        "subtitle": "人工看图确认" if policy.get("require_product_signoff", True) else "已按策略跳过",
        "status": signoff_status,
        "attempt": len(signoffs),
        "rework_count": sum(1 for s in signoffs if s["decision"] != "approved"),
        "duration_sec": None,
        "engine": "human",
        "model": "",
        "task_id": None,
        "gates": [
            {
                "id": "product_signoff",
                "label": "产品签收",
                "status": "passed" if signoff_status == "passed" else ("failed" if signoff_status == "rework" else ("waiting" if signoff_status == "waiting_input" else "pending")),
                "detail": (
                    f"{latest_signoff['signer']}：{latest_signoff['decision']}"
                    + (f"（{latest_signoff['comment']}）" if latest_signoff.get("comment") else "")
                ) if latest_signoff else "等待独立验收通过后由产品确认效果",
            },
        ],
    })
    edges.append({
        "id": f"e:{prev_node_id}->{signoff_node_id}",
        "source": prev_node_id,
        "target": signoff_node_id,
        "kind": "handoff",
        "label": "交付验收卡",
        "count": 1,
        "active": signoff_status == "waiting_input",
        "message_ids": [],
    })

    # 4. 合并与回告（确定性策略执行器）
    merge_policy = evaluate_merge_policy(policy)
    verified_merge = None
    if last_step and last_step.get("verified_merge"):
        try:
            verified_merge = json.loads(last_step["verified_merge"])
        except (TypeError, json.JSONDecodeError):
            verified_merge = None
    merge_status = "passed" if verified_merge else ("waiting_input" if signoff_status == "passed" else "pending")
    merge_node_id = "sys:merge"
    nodes.append({
        "id": merge_node_id,
        "kind": "system",
        "lane": "deliver",
        "position": total_steps + 2,
        "title": "策略合并与回告",
        "subtitle": "自动入队" if merge_policy["auto_merge"] else "人工放行合并",
        "status": merge_status,
        "attempt": 1 if verified_merge else 0,
        "rework_count": 0,
        "duration_sec": None,
        "engine": "system",
        "model": "",
        "task_id": None,
        "gates": [
            {
                "id": "merge_auth",
                "label": "合并策略",
                "status": "passed" if merge_policy["auto_merge"] else "waiting",
                "detail": merge_policy["reason"],
            },
            {
                "id": "merge_receipt",
                "label": "合并回执",
                "status": "passed" if verified_merge else "pending",
                "detail": f"PR #{verified_merge.get('number')} 已合入 {verified_merge.get('baseRefName')}" if verified_merge else "尚未读取到目标分支实际合并回执",
            },
        ],
    })
    edges.append({
        "id": f"e:{signoff_node_id}->{merge_node_id}",
        "source": signoff_node_id,
        "target": merge_node_id,
        "kind": "handoff",
        "label": "放行合并" if merge_policy["auto_merge"] else "待人合并",
        "count": 1,
        "active": signoff_status == "passed" and not verified_merge,
        "message_ids": [],
    })

    # 5. 动态叠加真实发生的返工与问答边
    rework_pairs: dict[tuple[int, int], list[dict[str, Any]]] = {}
    ask_pairs: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for msg in messages:
        frm = msg.get("from_position")
        to = msg.get("to_position")
        if frm is None or to is None:
            continue
        if msg.get("kind") == "rework":
            rework_pairs.setdefault((int(frm), int(to)), []).append(msg)
        elif msg.get("kind") == "ask":
            ask_pairs.setdefault((int(frm), int(to)), []).append(msg)

    for (frm, to), group in rework_pairs.items():
        edges.append({
            "id": f"e:rework:{frm}->{to}",
            "source": f"step:{frm}",
            "target": f"step:{to}",
            "kind": "rework",
            "label": f"返工 ×{len(group)}",
            "count": len(group),
            "active": run.get("current_position") == to and run.get("status") == "running",
            "message_ids": [m["id"] for m in group],
        })
    for (frm, to), group in ask_pairs.items():
        edges.append({
            "id": f"e:ask:{frm}->{to}",
            "source": f"step:{frm}",
            "target": f"step:{to}",
            "kind": "ask",
            "label": f"追问 ×{len(group)}",
            "count": len(group),
            "active": False,
            "message_ids": [m["id"] for m in group],
        })

    # 产品退回边
    for decision_type, target_pos, label in (
        ("rejected_impl", 2 if total_steps >= 2 else 1, "产品退回开发"),
        ("rejected_spec", 1, "产品改口径"),
    ):
        matched = [s for s in signoffs if s["decision"] == decision_type]
        if matched:
            edges.append({
                "id": f"e:signoff:{decision_type}",
                "source": signoff_node_id,
                "target": f"step:{target_pos}",
                "kind": "rework",
                "label": f"{label} ×{len(matched)}",
                "count": len(matched),
                "active": False,
                "message_ids": [],
            })

    # 汇总白盒健康度
    all_gates = [g for n in nodes for g in n.get("gates", [])]
    passed_gates = sum(1 for g in all_gates if g["status"] == "passed")
    failed_gates = sum(1 for g in all_gates if g["status"] == "failed")

    return {
        "lanes": [
            {"id": "intake", "title": "弄清需求"},
            {"id": "build", "title": "开发与检查"},
            {"id": "deliver", "title": "验收与交付"},
        ],
        "nodes": nodes,
        "edges": edges,
        "policy": policy,
        "merge_policy": merge_policy,
        "signoffs": signoffs,
        "candidate_version": candidate_version,
        "baseline_version": (spec or {}).get("baseline", ""),
        "tickets": build_tickets_progress(trace_data, spec, plan),
        "artifacts": list_run_artifacts(int(run["id"])),
        "summary": {
            "total_gates": len(all_gates),
            "passed_gates": passed_gates,
            "failed_gates": failed_gates,
            "rework_total": run.get("rework_total", 0),
        },
    }
