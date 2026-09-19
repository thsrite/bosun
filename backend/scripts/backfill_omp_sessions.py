"""回填 OMP 历史任务缺失的 session_uid 和 Token 用量。

修复会话目录发现后运行；本工具不重启后端、不重新执行任务、不改写会话文件。
只处理已有起止时间的已结束/暂停任务，不处理恢复轮次。项目、原始指令和
会话创建时间必须匹配；多候选、多个任务争用、已认领会话一律跳过。
Token 沿用 Bosun 的 input+output 口径，限定在该任务运行窗口内。

用法：
  python3 backend/scripts/backfill_omp_sessions.py           # 只预览
  python3 backend/scripts/backfill_omp_sessions.py --apply   # 重新核对后原子写入

数据位置沿用 BOSUN_DATA（默认 ~/.bosun）；OMP 目录沿用 PI_CODING_AGENT_DIR。
重启后端加载目录修复后，回填的任务即可恢复原会话；本工具不会自动重启。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import sessions


@dataclass(frozen=True)
class Recovery:
    task: sqlite3.Row
    session_uid: str
    tokens: int | None


def build_plan(conn: sqlite3.Connection) -> dict[int, Recovery]:
    """只返回双向唯一匹配；活动任务也参与争用检查，但不回填。"""
    claimed = {row[0] for row in conn.execute(
        "SELECT session_uid FROM task WHERE session_uid IS NOT NULL")}
    tasks = conn.execute(
        "SELECT t.id,t.project_id,t.prompt,t.original_prompt,t.started_at,t.ended_at,"
        "t.status,t.resume,t.tokens,p.path FROM task t JOIN project p ON p.id=t.project_id "
        "WHERE t.engine='omp' AND t.session_uid IS NULL AND t.deleted=0 "
        "AND t.started_at IS NOT NULL ORDER BY t.id"
    ).fetchall()
    metadata: dict[str, list[dict]] = {}
    candidates: dict[int, set[str]] = {}
    for task in tasks:
        cwd = task["path"]
        if cwd not in metadata:
            metadata[cwd] = [
                meta for directory in sessions.omp_project_dirs(cwd)
                for path in directory.glob("*.jsonl")
                if (meta := sessions._session_meta(path, "omp", cwd)) is not None
            ]
        prompt = sessions._clean_prompt(task["original_prompt"] or task["prompt"] or "")
        if not prompt:
            continue
        started, ended = task["started_at"], task["ended_at"]
        candidates[task["id"]] = {
            meta["session_uid"] for meta in metadata[cwd]
            if meta["session_uid"] not in claimed
            and meta["prompt"] == prompt
            and meta["created_at"] is not None
            and started - 5 <= meta["created_at"]
            and (ended is None or meta["created_at"] <= ended + 60)
        }
    contenders = Counter(uid for uids in candidates.values() for uid in uids)
    plan = {}
    for task in tasks:
        if task["status"] not in {"done", "failed", "cancelled", "interrupted", "paused"}:
            continue
        if task["resume"] or task["ended_at"] is None or task["ended_at"] < task["started_at"]:
            continue
        uids = candidates.get(task["id"], set())
        if len(uids) != 1:
            continue
        uid = next(iter(uids))
        if contenders[uid] != 1:
            continue
        tokens = sessions.count_tokens("omp", task["path"], uid,
                                       since=task["started_at"], until=task["ended_at"])
        plan[task["id"]] = Recovery(task, uid, tokens)
    return plan


def apply_plan(conn: sqlite3.Connection, plan: dict[int, Recovery]) -> list[int]:
    """写锁内重新匹配，拒绝预览后被修改、重新运行或被其它任务认领的条目。"""
    applied = []
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        current = build_plan(conn)
        for task_id, item in plan.items():
            if current.get(task_id) != item:
                continue
            conn.execute(
                "UPDATE task SET session_uid=?,tokens=COALESCE(?,tokens) WHERE id=?",
                (item.session_uid, item.tokens, task_id),
            )
            applied.append(task_id)
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正落库；默认仅预览")
    args = parser.parse_args()
    path = Path(os.environ.get("BOSUN_DATA", Path.home() / ".bosun")) / "bosun.db"
    # mode=rw 避免路径配置错误时悄悄创建空数据库；dry-run 使用只读连接。
    mode = "rw" if args.apply else "ro"
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        plan = build_plan(conn)
        print(f"可回填 {len(plan)} 条（仅零歧义且已结束的 OMP 任务）")
        for task_id, item in plan.items():
            tokens = item.tokens if item.tokens is not None else "未发现用量，保留原值"
            print(f"  task {task_id} -> {item.session_uid}  tokens={tokens}")
        if args.apply:
            applied = apply_plan(conn, plan)
            print(f"已提交 {len(applied)} 条；重新核对后跳过 {len(plan) - len(applied)} 条")
        else:
            print("DRY-RUN：未写库。加 --apply 落库；本工具不会重启后端。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
