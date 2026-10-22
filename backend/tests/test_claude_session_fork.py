"""Claude 会话分叉跟随 + 后台运行冲突。

续聊时 Claude 可能把会话挪到新的会话文件（历史原样复制、换新 id），任务若一直记着旧 id，
下次「继续」会加载旧快照，看起来最近的记录全丢了；会话被转入 Claude 后台运行时，
再 --resume 同一会话会被 Claude 直接拒绝。
"""
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import db, scheduler, sessions
from app.routers import tasks as tasks_router

OLD = "11111111-1111-4111-8111-111111111111"
FORK = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
CLAIMED = "44444444-4444-4444-8444-444444444444"


def _write_session(directory: Path, uid: str, first_user_uuid: str, mtime: float) -> None:
    path = directory / f"{uid}.jsonl"
    lines = [
        {"type": "permission-mode", "sessionId": uid},
        {"type": "user", "uuid": first_user_uuid, "sessionId": uid, "message": {"role": "user", "content": "做个调研"}},
        {"type": "assistant", "uuid": f"a-{uid}", "sessionId": uid, "message": {"role": "assistant", "content": []}},
    ]
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n")
    os.utime(path, (mtime, mtime))


class LatestForkTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._patch = patch.object(sessions, "claude_project_dir", return_value=self.dir)
        self._patch.start()
        now = time.time()
        _write_session(self.dir, OLD, "root-uuid", now - 300)

    def tearDown(self):
        self._patch.stop()

    def test_newer_session_with_same_history_is_the_fork(self):
        now = time.time()
        _write_session(self.dir, FORK, "root-uuid", now - 10)
        _write_session(self.dir, OTHER, "different-root", now)  # 同项目别的会话，更新但不是分叉
        self.assertEqual(sessions.latest_claude_fork("/p", OLD), FORK)

    def test_no_fork_returns_none(self):
        _write_session(self.dir, OTHER, "different-root", time.time())
        self.assertIsNone(sessions.latest_claude_fork("/p", OLD))

    def test_older_copy_is_not_a_fork(self):
        _write_session(self.dir, FORK, "root-uuid", time.time() - 900)
        self.assertIsNone(sessions.latest_claude_fork("/p", OLD))

    def test_session_claimed_by_another_task_is_skipped(self):
        now = time.time()
        _write_session(self.dir, CLAIMED, "root-uuid", now)
        _write_session(self.dir, FORK, "root-uuid", now - 20)
        self.assertEqual(sessions.latest_claude_fork("/p", OLD, exclude_uids={CLAIMED}), FORK)


class BackgroundSessionTest(unittest.TestCase):
    def _run(self, stdout: str):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    def test_live_background_session_is_reported(self):
        listing = json.dumps([{"id": "11111111", "sessionId": OLD, "pid": str(os.getpid()), "name": "发版"}])
        with patch.object(sessions.subprocess, "run", return_value=self._run(listing)):
            self.assertEqual(sessions.claude_background_session(OLD)["name"], "发版")

    def test_entry_without_live_process_is_ignored(self):
        listing = json.dumps([{"sessionId": OLD, "state": "blocked"}, {"sessionId": OTHER, "pid": "999999"}])
        with patch.object(sessions.subprocess, "run", return_value=self._run(listing)):
            self.assertIsNone(sessions.claude_background_session(OLD))

    def test_cli_failure_means_no_conflict(self):
        with patch.object(sessions.subprocess, "run", side_effect=OSError("no claude")):
            self.assertIsNone(sessions.claude_background_session(OLD))
        with patch.object(sessions.subprocess, "run", return_value=self._run("not json")):
            self.assertIsNone(sessions.claude_background_session(OLD))


class TaskSessionTest(unittest.TestCase):
    def setUp(self):
        self._old_conn = db._conn
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        db._conn = self.conn
        db.init_db()
        self.project = db.execute(
            "INSERT INTO project(name,path,created_at) VALUES(?,?,?)", ("P", "/p", time.time())
        )
        self.task = db.execute(
            "INSERT INTO task(project_id,engine,prompt,status,session_uid,created_at,started_at,ended_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (self.project, "claude", "做个调研", "done", OLD, time.time(), time.time() - 60, time.time()),
        )

    def tearDown(self):
        self.conn.close()
        db._conn = self._old_conn

    def _uid(self):
        return db.query_one("SELECT session_uid FROM task WHERE id=?", (self.task,))["session_uid"]

    def test_follow_fork_updates_task(self):
        with patch.object(scheduler.sessions, "latest_claude_fork", return_value=FORK) as fork:
            self.assertEqual(scheduler.follow_claude_fork(self.task), FORK)
        self.assertEqual(self._uid(), FORK)
        self.assertEqual(fork.call_args.kwargs["exclude_uids"], set())

    def test_follow_fork_keeps_uid_when_not_forked(self):
        with patch.object(scheduler.sessions, "latest_claude_fork", return_value=None):
            self.assertEqual(scheduler.follow_claude_fork(self.task), OLD)
        self.assertEqual(self._uid(), OLD)

    def test_continue_refuses_while_session_runs_in_background(self):
        body = tasks_router.ContinueBody(prompt="继续", start=True)
        with patch.object(scheduler.sessions, "latest_claude_fork", return_value=FORK), patch.object(
            scheduler.sessions,
            "claude_background_session",
            return_value={"id": "22222222", "sessionId": FORK, "pid": "1", "name": "发版"},
        ):
            with self.assertRaises(HTTPException) as cm:
                tasks_router.continue_task(self.task, body)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("claude attach 22222222", cm.exception.detail)
        row = db.query_one("SELECT status, session_uid FROM task WHERE id=?", (self.task,))
        self.assertEqual(row["status"], "done")  # 没被拉回执行，不会立刻失败
        self.assertEqual(row["session_uid"], FORK)  # 但会话已跟到最新分叉


if __name__ == "__main__":
    unittest.main()
