import asyncio
import sqlite3
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import auth, db, orchestrations, scheduler
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
        # 常驻班组会绕过普通并发槽直接拉起全部真实 CLI；单元测试只验证状态机。
        with patch.object(orchestrations, "_dispatch_roles"):
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

        with patch.object(orchestrations.scheduler, "deliver_message", return_value=True):
            orchestrations.handle_task_report(first["task_id"], "done", "完成", "方案正文")
            orchestrations.handle_task_report(first["task_id"], "done", "重复", "重复正文")

        second = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=2",
            (run["id"],),
        )
        self.assertEqual((second["model"], second["reasoning_effort"]), ("gpt-5.6-sol", "xhigh"))
        self.assertEqual(second["status"], "queued")
        handoff = db.query_one(
            "SELECT body FROM orchestration_message WHERE run_id=? AND kind='handoff'", (run["id"],)
        )
        self.assertIn("方案正文", handoff["body"])

    def _handoff(self) -> tuple[dict, sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        run = self.create_run()
        first = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=1", (run["id"],)
        )
        second = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=2", (run["id"],)
        )
        with patch.object(orchestrations.scheduler, "deliver_message", return_value=True):
            orchestrations.handle_task_report(first["task_id"], "done", "完成", "方案正文")
        message = db.query_one(
            "SELECT * FROM orchestration_message WHERE run_id=? AND kind='handoff'", (run["id"],)
        )
        return run, first, second, message

    def test_handoff_is_not_consumed_until_target_acknowledges_it(self) -> None:
        run = self.create_run()
        first = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=1", (run["id"],)
        )
        second = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=2", (run["id"],)
        )
        old_message_id = db.execute(
            "INSERT INTO orchestration_message(run_id,from_position,to_position,kind,body,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (run["id"], 1, 2, "ask", "旧的滞留问题", time.time()),
        )

        with patch.object(orchestrations.scheduler, "deliver_message", return_value=True):
            orchestrations.handle_task_report(first["task_id"], "done", "完成", "方案正文")

        handoff = db.query_one(
            "SELECT * FROM orchestration_message WHERE run_id=? AND kind='handoff'", (run["id"],)
        )
        old_message = db.query_one("SELECT * FROM orchestration_message WHERE id=?", (old_message_id,))
        current = db.query_one("SELECT * FROM orchestration_step_run WHERE id=?", (second["id"],))
        self.assertEqual(current["status"], "queued")
        self.assertEqual(handoff["delivery_attempts"], 1)
        self.assertIsNotNone(handoff["delivered_at"])
        self.assertIsNone(handoff["acknowledged_at"])
        self.assertIsNone(old_message["delivered_at"])
        self.assertIn(f"消息 #{handoff['id']}", handoff["body"])

    def test_only_target_role_can_acknowledge_and_start_handoff(self) -> None:
        run, first, second, message = self._handoff()

        with self.assertRaises(orchestrations.OrchestrationError):
            orchestrations.acknowledge_message(first["task_id"], message["id"])

        result = orchestrations.acknowledge_message(second["task_id"], message["id"])
        current = db.query_one("SELECT * FROM orchestration_step_run WHERE id=?", (second["id"],))
        acknowledged = db.query_one("SELECT * FROM orchestration_message WHERE id=?", (message["id"],))
        self.assertTrue(result["ok"])
        self.assertEqual(current["status"], "running")
        self.assertIsNotNone(acknowledged["acknowledged_at"])

    def test_ack_endpoint_accepts_the_target_task_token_when_auth_is_enabled(self) -> None:
        _run, _first, second, message = self._handoff()
        token = auth.issue_task_token(second["task_id"])

        class Request:
            headers = {"authorization": f"Bearer {token}"}
            client = None

        auth.set_password("test-password")
        try:
            result = tasks.acknowledge_crew_message(
                second["task_id"], message["id"], tasks.CrewMessageAckBody(), Request(),
            )
        finally:
            auth.clear_password()

        self.assertTrue(result["acknowledged"])

    def test_duplicate_ack_does_not_resume_a_later_human_wait(self) -> None:
        run, _first, second, message = self._handoff()
        orchestrations.acknowledge_message(second["task_id"], message["id"])
        db.execute(
            "UPDATE orchestration_run SET status='waiting_input' WHERE id=?", (run["id"],)
        )
        db.execute(
            "UPDATE orchestration_step_run SET status='waiting_input' WHERE id=?", (second["id"],)
        )

        duplicate = orchestrations.acknowledge_message(second["task_id"], message["id"])

        self.assertFalse(duplicate["newly_acknowledged"])
        self.assertEqual(orchestrations.get_run(run["id"])["status"], "waiting_input")
        current = db.query_one("SELECT status FROM orchestration_step_run WHERE id=?", (second["id"],))
        self.assertEqual(current["status"], "waiting_input")

    def test_ack_cannot_resurrect_a_run_cancelled_after_validation(self) -> None:
        run, _first, second, message = self._handoff()
        real_conn = self.conn

        class CursorProxy:
            def __init__(self, cursor, should_cancel=False):
                self.cursor = cursor
                self.should_cancel = should_cancel

            def fetchone(self):
                row = self.cursor.fetchone()
                if self.should_cancel:
                    real_conn.execute(
                        "UPDATE orchestration_run SET status='cancelled' WHERE id=?", (run["id"],)
                    )
                    real_conn.execute(
                        "UPDATE orchestration_step_run SET status='cancelled' WHERE run_id=?",
                        (run["id"],),
                    )
                return row

            def __getattr__(self, name):
                return getattr(self.cursor, name)

        class ConnectionProxy:
            def execute(self, sql, params=()):
                cursor = real_conn.execute(sql, params)
                return CursorProxy(
                    cursor,
                    sql.startswith("SELECT * FROM orchestration_run WHERE id="),
                )

            def __enter__(self):
                real_conn.__enter__()
                return self

            def __exit__(self, *args):
                return real_conn.__exit__(*args)

        with patch.object(db, "get_conn", return_value=ConnectionProxy()):
            orchestrations.acknowledge_message(second["task_id"], message["id"])

        self.assertEqual(orchestrations.get_run(run["id"])["status"], "cancelled")

    def test_unacknowledged_handoff_retries_recovers_then_waits_for_user(self) -> None:
        run, _first, second, message = self._handoff()
        base = float(message["last_delivered_at"])

        with patch.object(orchestrations.scheduler, "deliver_message", return_value=True), patch.object(
            orchestrations.scheduler, "restart_orchestration_role", return_value=True
        ) as restart:
            orchestrations.sweep_reliable_communications(
                now=base + orchestrations.MESSAGE_ACK_TIMEOUT + 1
            )
            retried = db.query_one("SELECT * FROM orchestration_message WHERE id=?", (message["id"],))
            self.assertEqual(retried["delivery_attempts"], 2)
            restart.assert_not_called()

            orchestrations.sweep_reliable_communications(
                now=float(retried["last_delivered_at"]) + orchestrations.MESSAGE_ACK_TIMEOUT + 1
            )
            recovered = db.query_one("SELECT * FROM orchestration_message WHERE id=?", (message["id"],))
            self.assertEqual(recovered["recovery_count"], 1)
            self.assertEqual(recovered["delivery_attempts"], 3)
            restart.assert_called_once_with(second["task_id"])

            orchestrations.sweep_reliable_communications(
                now=float(recovered["last_delivered_at"]) + orchestrations.MESSAGE_ACK_TIMEOUT + 1
            )

        waiting_run = orchestrations.get_run(run["id"])
        waiting_step = db.query_one("SELECT status FROM orchestration_step_run WHERE id=?", (second["id"],))
        self.assertEqual(waiting_run["status"], "waiting_input")
        self.assertEqual(waiting_step["status"], "waiting_input")

    def test_retry_sweep_does_not_restart_a_message_acknowledged_after_snapshot(self) -> None:
        _run, _first, second, message = self._handoff()
        db.execute(
            "UPDATE orchestration_message SET delivery_attempts=2,last_delivered_at=? WHERE id=?",
            (1.0, message["id"]),
        )
        original_query = db.query

        def acknowledge_after_retry_snapshot(sql, params=()):
            rows = original_query(sql, params)
            if "m.acknowledged_at IS NULL" in sql:
                orchestrations.acknowledge_message(second["task_id"], message["id"])
            return rows

        with patch.object(db, "query", side_effect=acknowledge_after_retry_snapshot), patch.object(
            orchestrations.scheduler, "restart_orchestration_role"
        ) as restart:
            orchestrations.sweep_reliable_communications(now=1000.0)

        restart.assert_not_called()

    def test_supervisor_does_not_restart_a_role_reported_done_after_snapshot(self) -> None:
        run = self.create_run()
        first = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=1", (run["id"],)
        )
        db.execute("UPDATE task SET status='waiting_input' WHERE id=?", (first["task_id"],))
        db.execute(
            "UPDATE orchestration_step_run SET nudge_count=1,last_nudged_at=1 WHERE id=?",
            (first["id"],),
        )
        original_query = db.query

        def report_after_supervisor_snapshot(sql, params=()):
            rows = original_query(sql, params)
            if "JOIN task t ON t.id=s.task_id" in sql:
                with patch.object(orchestrations.scheduler, "deliver_message", return_value=True):
                    orchestrations.handle_task_report(
                        first["task_id"], "done", "完成", "方案正文"
                    )
            return rows

        with patch.object(db, "query", side_effect=report_after_supervisor_snapshot), patch.object(
            orchestrations.scheduler, "get_waiting_kind", return_value="review"
        ), patch.object(orchestrations.scheduler, "restart_orchestration_role") as restart:
            orchestrations.sweep_reliable_communications(now=1000.0)

        restart.assert_not_called()

    def test_manual_resume_rearms_exhausted_delivery_and_online_current_role(self) -> None:
        run, _first, second, message = self._handoff()
        db.execute(
            "UPDATE orchestration_message SET delivery_attempts=3,recovery_count=1,"
            "last_delivered_at=1,acknowledged_at=NULL WHERE id=?",
            (message["id"],),
        )
        db.execute("UPDATE orchestration_run SET status='waiting_input' WHERE id=?", (run["id"],))
        db.execute(
            "UPDATE orchestration_step_run SET status='waiting_input',nudge_count=1,"
            "recovery_count=1,last_nudged_at=1 WHERE id=?",
            (second["id"],),
        )
        db.execute("UPDATE task SET status='waiting_input' WHERE id=?", (second["task_id"],))

        with patch.object(orchestrations.scheduler, "role_online", return_value=True), patch.object(
            orchestrations.scheduler, "deliver_message", return_value=True
        ) as deliver, patch.object(orchestrations, "_dispatch_roles"):
            resumed = orchestrations.resume_run(run["id"])

        current = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE id=?", (second["id"],)
        )
        retried = db.query_one(
            "SELECT * FROM orchestration_message WHERE id=?", (message["id"],)
        )
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(current["status"], "running")
        self.assertEqual((current["nudge_count"], current["recovery_count"]), (0, 0))
        self.assertEqual((retried["delivery_attempts"], retried["recovery_count"]), (1, 0))
        self.assertEqual(deliver.call_args_list[0].args, (second["task_id"], message["body"]))

    def test_resume_keeps_an_explicit_needs_input_waiting_for_the_user(self) -> None:
        run = self.create_run()
        first = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=1", (run["id"],)
        )
        db.execute(
            "UPDATE task SET status='waiting_input',report_result='needs_input' WHERE id=?",
            (first["task_id"],),
        )
        db.execute(
            "UPDATE orchestration_step_run SET status='waiting_input',result='needs_input' WHERE id=?",
            (first["id"],),
        )
        db.execute("UPDATE orchestration_run SET status='waiting_input' WHERE id=?", (run["id"],))

        with patch.object(orchestrations.scheduler, "role_online", return_value=True), patch.object(
            orchestrations.scheduler, "deliver_message"
        ) as deliver, patch.object(orchestrations, "_dispatch_roles"):
            resumed = orchestrations.resume_run(run["id"])

        current = db.query_one("SELECT status FROM orchestration_step_run WHERE id=?", (first["id"],))
        self.assertEqual(resumed["status"], "waiting_input")
        self.assertEqual(current["status"], "waiting_input")
        deliver.assert_not_called()

    def test_failed_transport_attempt_is_counted_instead_of_busy_retrying(self) -> None:
        _run, _first, _second, message = self._handoff()
        base = float(message["last_delivered_at"])

        with patch.object(orchestrations.scheduler, "deliver_message", return_value=False), patch.object(
            orchestrations, "_revive_role"
        ):
            orchestrations.sweep_reliable_communications(
                now=base + orchestrations.MESSAGE_ACK_TIMEOUT + 1
            )

        failed = db.query_one("SELECT * FROM orchestration_message WHERE id=?", (message["id"],))
        self.assertEqual(failed["delivery_attempts"], 2)
        self.assertIsNotNone(failed["last_delivered_at"])

    def test_stalled_baton_is_nudged_then_recovered_before_user_intervention(self) -> None:
        run = self.create_run()
        first = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=1", (run["id"],)
        )
        db.execute("UPDATE task SET status='waiting_input' WHERE id=?", (first["task_id"],))
        base = time.time()

        with patch.object(orchestrations.scheduler, "get_waiting_kind", return_value="review"), patch.object(
            orchestrations.scheduler, "deliver_message", return_value=True
        ), patch.object(orchestrations.scheduler, "restart_orchestration_role", return_value=True) as restart:
            orchestrations.sweep_reliable_communications(now=base)
            nudged = db.query_one("SELECT * FROM orchestration_step_run WHERE id=?", (first["id"],))
            self.assertEqual(nudged["nudge_count"], 1)

            db.execute("UPDATE task SET status='waiting_input' WHERE id=?", (first["task_id"],))
            orchestrations.sweep_reliable_communications(
                now=float(nudged["last_nudged_at"]) + orchestrations.MESSAGE_ACK_TIMEOUT + 1
            )
            recovered = db.query_one("SELECT * FROM orchestration_step_run WHERE id=?", (first["id"],))
            self.assertEqual(recovered["recovery_count"], 1)
            self.assertEqual(recovered["nudge_count"], 0)
            restart.assert_called_once_with(first["task_id"])

            db.execute("UPDATE task SET status='waiting_input' WHERE id=?", (first["task_id"],))
            orchestrations.sweep_reliable_communications(now=base + 200)
            renudged = db.query_one("SELECT * FROM orchestration_step_run WHERE id=?", (first["id"],))
            db.execute("UPDATE task SET status='waiting_input' WHERE id=?", (first["task_id"],))
            orchestrations.sweep_reliable_communications(
                now=float(renudged["last_nudged_at"]) + orchestrations.MESSAGE_ACK_TIMEOUT + 1
            )

        self.assertEqual(orchestrations.get_run(run["id"])["status"], "waiting_input")

    def test_permission_wait_never_triggers_automatic_nudge_or_recovery(self) -> None:
        run = self.create_run()
        first = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=1", (run["id"],)
        )
        db.execute("UPDATE task SET status='waiting_input' WHERE id=?", (first["task_id"],))

        with patch.object(orchestrations.scheduler, "get_waiting_kind", return_value="permission"), patch.object(
            orchestrations.scheduler, "deliver_message"
        ) as deliver, patch.object(orchestrations.scheduler, "restart_orchestration_role") as restart:
            orchestrations.sweep_reliable_communications()

        current = db.query_one("SELECT * FROM orchestration_step_run WHERE id=?", (first["id"],))
        self.assertEqual(orchestrations.get_run(run["id"])["status"], "waiting_input")
        self.assertEqual(current["nudge_count"], 0)
        self.assertEqual(current["recovery_count"], 0)
        deliver.assert_not_called()
        restart.assert_not_called()

    def test_cancelled_role_cannot_be_restarted_by_delivery_race(self) -> None:
        run = self.create_run()
        first = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=1", (run["id"],)
        )
        db.execute("UPDATE orchestration_run SET status='cancelled' WHERE id=?", (run["id"],))
        db.execute("UPDATE orchestration_step_run SET status='cancelled' WHERE id=?", (first["id"],))
        db.execute("UPDATE task SET status='cancelled' WHERE id=?", (first["task_id"],))

        with patch.object(scheduler, "_start_task") as start:
            restarted = scheduler.restart_orchestration_role(first["task_id"])

        self.assertFalse(restarted)
        start.assert_not_called()

    def test_authoritative_report_implicitly_acknowledges_handoff(self) -> None:
        run, _first, second, message = self._handoff()

        with patch.object(orchestrations.scheduler, "deliver_message", return_value=True):
            orchestrations.handle_task_report(second["task_id"], "done", "实施完成", "实施产物")

        acknowledged = db.query_one(
            "SELECT acknowledged_at FROM orchestration_message WHERE id=?", (message["id"],)
        )
        self.assertIsNotNone(acknowledged["acknowledged_at"])

    def test_rework_clears_stale_reports_and_reliability_counters(self) -> None:
        run, first, second, _message = self._handoff()
        db.execute(
            "UPDATE task SET report_result='done',report_summary='旧结论' WHERE id IN (?,?)",
            (first["task_id"], second["task_id"]),
        )
        db.execute(
            "UPDATE orchestration_step_run SET nudge_count=1,recovery_count=1,last_nudged_at=? "
            "WHERE run_id=?",
            (time.time(), run["id"]),
        )

        with patch.object(orchestrations.scheduler, "deliver_message", return_value=True):
            orchestrations.handle_task_report(
                second["task_id"], "rework", "请重做方案", None, target_position=1,
            )

        steps = db.query(
            "SELECT nudge_count,recovery_count,last_nudged_at FROM orchestration_step_run "
            "WHERE run_id=? ORDER BY position",
            (run["id"],),
        )
        reports = db.query(
            "SELECT report_result,report_summary FROM task WHERE id IN (?,?) ORDER BY id",
            (first["task_id"], second["task_id"]),
        )
        self.assertTrue(all(row["nudge_count"] == 0 for row in steps))
        self.assertTrue(all(row["recovery_count"] == 0 for row in steps))
        self.assertTrue(all(row["last_nudged_at"] is None for row in steps))
        self.assertTrue(all(row["report_result"] is None for row in reports))

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

    def test_scheduler_passes_role_runtime_to_claude_pty(self) -> None:
        run = self.create_run()
        step = db.query_one(
            "SELECT task_id FROM orchestration_step_run WHERE run_id=? AND position=1",
            (run["id"],),
        )
        row = db.query_one("SELECT * FROM task WHERE id=?", (step["task_id"],))
        captured: dict = {}

        class FakePtySession:
            def __init__(self, *args, **kwargs) -> None:
                captured["session"] = kwargs

            def start(self) -> None:
                pass

        def fake_build_argv(*args, **kwargs):
            captured["argv"] = kwargs
            return ["claude"]

        with patch.object(scheduler, "_loop", object()), patch.object(
            scheduler.engine_settings, "should_use_claude_sdk", return_value=True
        ), patch.object(scheduler, "PtySession", FakePtySession), patch.object(
            scheduler, "build_argv", side_effect=fake_build_argv
        ), patch.object(scheduler.sessions, "snapshot_claude", return_value=set()), patch.object(
            scheduler.events, "emit"
        ), patch.object(scheduler.threading, "Thread"):
            try:
                scheduler._start_task(row)
            finally:
                scheduler._sessions.pop(row["id"], None)

        self.assertTrue(captured["session"]["artifact_required"])
        self.assertEqual(captured["argv"]["model_override"], "sonnet")
        self.assertEqual(captured["argv"]["reasoning_override"], "high")

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

    def test_clean_current_role_exit_recovers_once_then_waits_for_user(self) -> None:
        run = self.create_run()
        first = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=1", (run["id"],)
        )

        with patch.object(scheduler, "restart_orchestration_role", return_value=True) as restart, patch.object(
            scheduler, "deliver_message", return_value=True
        ):
            orchestrations.handle_task_exit(first["task_id"], 0)

        recovered = db.query_one("SELECT * FROM orchestration_step_run WHERE id=?", (first["id"],))
        self.assertEqual(orchestrations.get_run(run["id"])["status"], "running")
        self.assertEqual(recovered["recovery_count"], 1)
        restart.assert_called_once_with(first["task_id"])

        with patch.object(scheduler, "restart_orchestration_role") as restart_again:
            orchestrations.handle_task_exit(first["task_id"], 0)

        self.assertEqual(orchestrations.get_run(run["id"])["status"], "waiting_input")
        restart_again.assert_not_called()

    def test_needs_input_exit_preserves_human_wait_without_recovery(self) -> None:
        run = self.create_run()
        first = db.query_one(
            "SELECT * FROM orchestration_step_run WHERE run_id=? AND position=1", (run["id"],)
        )
        db.execute(
            "UPDATE task SET status='waiting_input',report_result='needs_input' WHERE id=?",
            (first["task_id"],),
        )
        db.execute(
            "UPDATE orchestration_step_run SET status='waiting_input',result='needs_input' WHERE id=?",
            (first["id"],),
        )
        db.execute(
            "UPDATE orchestration_run SET status='waiting_input' WHERE id=?", (run["id"],)
        )

        with patch.object(scheduler, "restart_orchestration_role") as restart:
            orchestrations.handle_task_exit(first["task_id"], 0)

        self.assertEqual(orchestrations.get_run(run["id"])["status"], "waiting_input")
        restart.assert_not_called()

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

    def test_legacy_delivered_messages_are_trusted_once_during_ack_migration(self) -> None:
        run = self.create_run()
        self.conn.execute("DROP TABLE orchestration_message")
        self.conn.execute(
            "CREATE TABLE orchestration_message("
            "id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL,from_position INTEGER,"
            "to_position INTEGER NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,"
            "created_at REAL NOT NULL,delivered_at REAL)"
        )
        self.conn.execute(
            "INSERT INTO orchestration_message(id,run_id,from_position,to_position,kind,body,"
            "created_at,delivered_at) VALUES(1,?,?,?,?,?,?,?)",
            (run["id"], 1, 2, "handoff", "旧交棒", 10.0, 20.0),
        )

        db._ensure_columns()

        migrated = db.query_one("SELECT * FROM orchestration_message WHERE id=1")
        self.assertEqual(migrated["last_delivered_at"], 20.0)
        self.assertEqual(migrated["delivery_attempts"], 1)
        self.assertEqual(migrated["acknowledged_at"], 20.0)


if __name__ == "__main__":
    unittest.main()
