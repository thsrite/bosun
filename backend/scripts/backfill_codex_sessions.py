"""回填漏捕获的 Codex 任务 session_uid。

背景：Codex 首跑靠「rollout 首条用户消息 == 派发 prompt」认领会话 id。历史上
若项目里有 AGENTS.md 指令块占了首条 user 消息，会导致比对失配、session_uid
落空，任务只能重跑无法续聊（详见 backend/app/sessions.py 的 _clean_prompt 修复）。
本脚本用当前（已修复的）匹配逻辑，把仍缺失的 codex 任务对回它真实的 rollout。

匹配条件（全部满足，且零歧义才回填）：
  - cwd 为项目路径或其子目录
  - rollout 首条真实用户消息（清洗后）== 派发 prompt（带收尾约定 tail）清洗后
  - rollout 创建时间落在任务运行窗口内 [started-5s, ended+60s]
  - 该 uid 未被任何任务占用
  - 该任务恰好只有 1 个候选（多候选一律跳过，宁缺毋错配）

用法：
  python3 backend/scripts/backfill_codex_sessions.py           # dry-run，只打印计划
  python3 backend/scripts/backfill_codex_sessions.py --apply   # 落库
落库改的是 ~/.bosun/bosun.db（数据层，不需重启后端；前端刷新即见续聊入口）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# 允许 `python3 backend/scripts/xxx.py` 从仓库根直接跑
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import sessions
from backend.app.engines import with_report_directive


def _db_path() -> Path:
    return Path(os.environ.get("BOSUN_DATA", Path.home() / ".bosun")) / "bosun.db"


def build_plan(conn: sqlite3.Connection) -> dict[int, str]:
    """返回 {task_id: session_uid}，仅含零歧义的可回填项。"""
    claimed = {
        r["session_uid"]
        for r in conn.execute("SELECT session_uid FROM task WHERE session_uid IS NOT NULL")
    }
    projpath = {r["id"]: r["path"] for r in conn.execute("SELECT id,path FROM project")}
    rollouts = [m for m in (sessions._session_meta(p, "codex") for p in sessions._codex_rollouts()) if m]

    tasks = conn.execute(
        "SELECT id,project_id,prompt,started_at,ended_at FROM task "
        "WHERE engine='codex' AND session_uid IS NULL AND deleted=0 ORDER BY id"
    ).fetchall()

    plan: dict[int, str] = {}
    for t in tasks:
        cwd = projpath.get(t["project_id"])
        if not cwd or not (t["prompt"] or "").strip():
            continue  # 空 prompt（仅加载上下文）无从按 prompt 认领
        # 两种形态都认：引入引擎清单提示之前派发的任务没有那段提示，
        # 之后的有。只比对其中一种会让另一半任务永远认领不到。
        expected = {
            sessions._clean_prompt(with_report_directive(t["prompt"])),
            sessions._clean_prompt(with_report_directive(t["prompt"], engine="codex")),
        }
        started, ended = t["started_at"], t["ended_at"]
        cand: list[str] = []
        for m in rollouts:
            if m["session_uid"] in claimed:
                continue
            if not sessions._same_or_child(m.get("cwd"), cwd):
                continue
            if m.get("prompt") not in expected:
                continue
            created = m.get("created_at") or 0
            if started and created and not (started - 5 <= created <= (ended or started) + 60):
                continue
            cand.append(m["session_uid"])
        if len(cand) == 1:
            plan[t["id"]] = cand[0]

    dup = [u for u, n in Counter(plan.values()).items() if n > 1]
    if dup:
        raise SystemExit(f"检测到 uid 被多个任务争用，中止以防错配: {dup}")
    return plan


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="真正落库；缺省为 dry-run")
    args = ap.parse_args()

    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    plan = build_plan(conn)

    if not plan:
        print("没有可回填的任务。")
        return

    print(f"{'落库' if args.apply else 'DRY-RUN'}：可回填 {len(plan)} 条")
    for tid, uid in sorted(plan.items()):
        if args.apply:
            n = conn.execute(
                "UPDATE task SET session_uid=? WHERE id=? AND session_uid IS NULL AND engine='codex'",
                (uid, tid),
            ).rowcount
            print(f"  task {tid} -> {uid}  ({'ok' if n else 'skip(已变化)'})")
        else:
            print(f"  task {tid} -> {uid}")
    if args.apply:
        conn.commit()
        print("已提交。")
    else:
        print("这是 dry-run，未写库。加 --apply 落库。")


if __name__ == "__main__":
    main()
