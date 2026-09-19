import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import sessions
from scripts import backfill_omp_sessions as recovery


class OmpSessionRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE project(id INTEGER PRIMARY KEY, path TEXT);
            CREATE TABLE task(id INTEGER PRIMARY KEY, project_id INTEGER, engine TEXT,
                prompt TEXT, original_prompt TEXT, started_at REAL, ended_at REAL,
                status TEXT, session_uid TEXT, tokens INTEGER, deleted INTEGER DEFAULT 0,
                resume INTEGER DEFAULT 0);
        """)
        self.conn.execute("INSERT INTO project VALUES(1,?)", (str(self.project),))
        self.env = patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.root / "omp")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.uid = "01a0b854-1d8c-76d5-9a4e-62a1a0b772ca"
        self.started = 1789798900.0

    def task(self, task_id=1, **overrides):
        values = dict(id=task_id, project_id=1, engine="omp", prompt="Implement plan",
                      original_prompt=None, started_at=self.started, ended_at=self.started + 40,
                      status="done", session_uid=None, tokens=None, deleted=0, resume=0)
        values.update(overrides)
        keys = ",".join(values)
        self.conn.execute(f"INSERT INTO task({keys}) VALUES({','.join('?' for _ in values)})",
                          tuple(values.values()))
        self.conn.commit()

    def transcript(self, uid=None, prompt="Implement plan", created=None, cwd=None):
        uid = uid or self.uid
        created = self.started + 1 if created is None else created
        directory = sessions.omp_project_dir(str(self.project))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"2026-09-19T06-21-55-468Z_{uid}.jsonl"
        rows = [
            {"type": "session", "id": uid, "cwd": str(cwd or self.project), "timestamp": created},
            {"type": "message", "timestamp": created, "message": {
                "role": "user", "content": [{"type": "text", "text": prompt}]}},
            {"type": "message", "timestamp": created + 9, "message": {
                "role": "assistant", "usage": {"input": 100, "output": 20, "cacheRead": 50}}},
            {"type": "message", "timestamp": created + 99, "message": {
                "role": "assistant", "usage": {"input": 900, "output": 90}}},
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return path

    def test_recovers_original_session_and_only_task_window_usage(self):
        self.task(prompt="", original_prompt="Implement plan")
        self.transcript()
        plan = recovery.build_plan(self.conn)
        self.assertEqual(set(plan), {1})
        self.assertIsNone(self.conn.execute("SELECT session_uid FROM task").fetchone()[0])
        self.assertEqual(recovery.apply_plan(self.conn, plan), [1])
        row = self.conn.execute("SELECT * FROM task").fetchone()
        self.assertEqual((row["session_uid"], row["tokens"], row["status"]), (self.uid, 120, "done"))
        self.assertEqual(recovery.build_plan(self.conn), {})
        self.assertIsNotNone(sessions.local_session_info("omp", str(self.project), row["session_uid"]))

    def test_never_guesses_between_two_sessions_or_two_tasks(self):
        self.task()
        self.transcript()
        other = self.transcript("01a0b854-1d8c-76d5-9a4e-62a1a0b772cb")
        self.assertEqual(recovery.build_plan(self.conn), {})
        other.unlink()
        self.task(2, status="running", ended_at=None)
        self.assertEqual(recovery.build_plan(self.conn), {})

    def test_rejects_foreign_prompt_cwd_time_and_missing_start(self):
        self.task()
        for changes in ({"prompt": "Other work"}, {"cwd": self.root / "other"},
                        {"created": self.started - 60}, {"created": self.started + 200}):
            with self.subTest(changes=changes):
                self.transcript(**changes)
                self.assertEqual(recovery.build_plan(self.conn), {})
        self.transcript()
        self.conn.execute("UPDATE task SET started_at=NULL")
        self.assertEqual(recovery.build_plan(self.conn), {})

    def test_claimed_deleted_active_and_resume_tasks_are_not_reassigned(self):
        self.transcript()
        self.task()
        self.task(2, session_uid=self.uid, deleted=1)
        self.assertEqual(recovery.build_plan(self.conn), {})
        self.conn.execute("DELETE FROM task WHERE id=2")
        for changes in ("deleted=1", "status='running'", "resume=1", "ended_at=NULL"):
            with self.subTest(changes=changes):
                self.conn.execute("UPDATE task SET deleted=0,status='done',resume=0,ended_at=?",
                                  (self.started + 40,))
                self.conn.execute(f"UPDATE task SET {changes}")
                self.assertEqual(recovery.build_plan(self.conn), {})

    def test_missing_usage_is_not_replaced_with_a_fabricated_zero(self):
        self.task()
        path = self.transcript()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            row.get("message", {}).pop("usage", None)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.assertEqual(recovery.apply_plan(self.conn, recovery.build_plan(self.conn)), [1])
        row = self.conn.execute("SELECT session_uid,tokens FROM task").fetchone()
        self.assertEqual(row["session_uid"], self.uid)
        self.assertIsNone(row["tokens"])

    def test_apply_rejects_stale_plan_and_new_claim(self):
        self.task()
        self.transcript()
        plan = recovery.build_plan(self.conn)
        self.conn.execute("UPDATE task SET started_at=started_at+1")
        self.conn.commit()
        self.assertEqual(recovery.apply_plan(self.conn, plan), [])
        self.conn.execute("UPDATE task SET started_at=started_at-1")
        self.conn.commit()
        self.task(2, session_uid=self.uid)
        self.assertEqual(recovery.apply_plan(self.conn, plan), [])
        self.assertIsNone(self.conn.execute("SELECT session_uid FROM task WHERE id=1").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
