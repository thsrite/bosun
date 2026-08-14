import asyncio
import sqlite3
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import db, orchestrations, scheduler, sdk_session
from app.routers import tasks


class OrchestrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_conn = db._conn
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        db._conn = self.conn
        db.init_db()
        self.project_id = db.execute(
            "INSERT INTO project(name,path,created_at) VALUES(?,?,?)",
            ("Bosun", "/tmp/bosun", time.time()),
        )

    def tearDown(self) -> None:
        self.conn.close()
        db._conn = self._old_conn

    @staticmethod
    def steps() -> list[dict]:
        return [
            {
                "name": "方案",
                "engine": "claude",
                "model": "sonnet",
                "reasoning_effort": "high",
                "role_prompt": "输出方案",
            },
            {
                "name": "执行",
                "engine": "codex",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
                "role_prompt": "实施并验证",
            },
        ]

    def create_run(self) -> dict:
        template = orchestrations.create_template("交付链", self.steps())
        with patch.object(orchestrations.scheduler, "tick"):
            return orchestrations.create_run(
                template["id"], self.project_id, "完成需求", None, 5, False, start=True
            )

    def test_role_runtime_and_artifact_are_snapshotted_and_advance_once(self) -> None:
        run = self.create_run()
        first = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=1",
            (run["id"],),
        )
        self.assertEqual((first["model"], first["reasoning_effort"]), ("sonnet", "high"))

        with patch.object(orchestrations.scheduler, "tick") as tick:
            orchestrations.handle_task_report(first["task_id"], "done", "完成", "方案正文")
            orchestrations.handle_task_report(first["task_id"], "done", "重复", "重复正文")

        self.assertEqual(tick.call_count, 1)
        second = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=2",
            (run["id"],),
        )
        self.assertEqual((second["model"], second["reasoning_effort"]), ("gpt-5.6-sol", "xhigh"))
        task = db.query_one("SELECT prompt FROM task WHERE id=?", (second["task_id"],))
        self.assertIn("方案正文", task["prompt"])

    def test_scheduler_uses_browser_session_contract(self) -> None:
        task_id = db.execute(
            "INSERT INTO task(project_id,engine,prompt,status,priority,auto_approve,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (self.project_id, "browser", "打开 http://localhost:8000", "queued", 5, 0, time.time()),
        )
        row = db.query_one("SELECT * FROM task WHERE id=?", (task_id,))

        class FakeBrowserSession:
            def __init__(
                self, *, task_id, prompt, log_path, loop, on_status, on_exit,
                on_tokens, on_permission,
            ) -> None:
                self.task_id = task_id

            def start(self) -> None:
                pass

        with patch.object(scheduler, "_loop", object()), patch.object(
            scheduler.browser_computer, "BrowserSession", FakeBrowserSession
        ), patch.object(scheduler.events, "emit"), patch.object(scheduler.threading, "Thread"):
            try:
                scheduler._start_task(row)
            finally:
                scheduler._sessions.pop(task_id, None)

    def test_scheduler_passes_role_runtime_to_claude_sdk(self) -> None:
        run = self.create_run()
        step = db.query_one(
            "SELECT task_id FROM orchestration_step_run WHERE run_id=? AND position=1",
            (run["id"],),
        )
        row = db.query_one("SELECT * FROM task WHERE id=?", (step["task_id"],))
        captured: dict = {}

        class FakeSdkSession:
            def __init__(self, *args, **kwargs) -> None:
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with patch.object(scheduler, "_loop", object()), patch.object(
            scheduler.engine_settings, "should_use_claude_sdk", return_value=True
        ), patch.object(sdk_session, "SdkSession", FakeSdkSession), patch.object(
            scheduler.events, "emit"
        ), patch.object(scheduler.threading, "Thread"):
            try:
                scheduler._start_task(row)
            finally:
                scheduler._sessions.pop(row["id"], None)

        self.assertTrue(captured["artifact_required"])
        self.assertEqual(captured["model_override"], "sonnet")
        self.assertEqual(captured["reasoning_override"], "high")

    def test_manual_task_transitions_cannot_desynchronize_run(self) -> None:
        run = self.create_run()
        step = db.query_one("SELECT task_id FROM orchestration_step_run WHERE run_id=?", (run["id"],))

        with self.assertRaises(scheduler.TaskTransitionError):
            scheduler.complete(step["task_id"])
        with self.assertRaises(scheduler.TaskTransitionError):
            scheduler.pause(step["task_id"])
        with self.assertRaises(scheduler.TaskTransitionError):
            scheduler.to_draft(step["task_id"])

        with patch.object(scheduler, "tick"):
            scheduler.delete(step["task_id"])
        self.assertEqual(orchestrations.get_run(run["id"])["status"], "cancelled")

    def test_start_failure_fails_run(self) -> None:
        run = self.create_run()
        step = db.query_one("SELECT task_id FROM orchestration_step_run WHERE run_id=?", (run["id"],))
        row = db.query_one("SELECT * FROM task WHERE id=?", (step["task_id"],))

        class FailingSession:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def start(self) -> None:
                raise OSError("missing cli")

        with patch.object(scheduler, "_loop", object()), patch.object(
            scheduler.engine_settings, "should_use_claude_sdk", return_value=False
        ), patch.object(scheduler, "build_argv", return_value=["claude"]), patch.object(
            scheduler, "PtySession", FailingSession
        ), patch.object(scheduler.sessions, "snapshot_claude", return_value=set()), patch.object(
            scheduler.events, "emit"
        ), patch.object(scheduler.threading, "Thread"):
            scheduler._start_task(row)

        self.assertEqual(orchestrations.get_run(run["id"])["status"], "failed")

    def test_artifact_reader_stops_at_limit(self) -> None:
        class FakeRequest:
            headers: dict[str, str] = {}

            def __init__(self) -> None:
                self.chunks_read = 0

            async def stream(self):
                for _ in range(4):
                    self.chunks_read += 1
                    yield b"x" * (orchestrations.MAX_ARTIFACT_BYTES // 2 + 1)

        request = FakeRequest()
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(tasks._read_artifact_body(request))
        self.assertEqual(raised.exception.status_code, 413)
        self.assertLess(request.chunks_read, 4)


if __name__ == "__main__":
    unittest.main()
