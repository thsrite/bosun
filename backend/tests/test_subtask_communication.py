import sqlite3
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, db, scheduler, subtasks
from app.main import app
from app.pty_session import PtySession
from app.routers import tasks as tasks_router
from app.sdk_session import SdkSession


class _Request:
    def __init__(self, token):
        self.headers = {"authorization": f"Bearer {token}"}


class SubtaskCommunicationTest(unittest.TestCase):
    def setUp(self):
        self.old_connection = db._conn
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        db._conn = self.connection
        db.init_db()
        db.execute(
            "INSERT INTO project(id,name,path,created_at) VALUES(1,'p','/tmp/p',?)",
            (time.time(),),
        )
        self.parent_id = self._task("running")
        self.parent_token = auth.issue_task_token(self.parent_id)

    def tearDown(self):
        db._conn = self.old_connection
        self.connection.close()
        while subtasks.inflight():
            subtasks.release_slot()

    def _task(self, status, parent_id=None):
        return db.execute(
            "INSERT INTO task(project_id,engine,prompt,status,parent_task_id,created_at) "
            "VALUES(1,'claude','任务',?,?,?)",
            (status, parent_id, time.time()),
        )

    def _waiting_child(self):
        child_id = self._task("waiting_input", self.parent_id)
        db.execute(
            "UPDATE task SET report_result='needs_input', report_summary='选 A 还是 B？' "
            "WHERE id=?",
            (child_id,),
        )
        return child_id

    def test_needs_input_keeps_child_alive_for_parent_reply(self):
        child_id = self._waiting_child()
        with patch.object(scheduler, "finish_subtask") as finish:
            result = subtasks.wait_for_result(child_id, 1.0)

        finish.assert_not_called()
        self.assertTrue(result["needs_reply"])
        self.assertFalse(subtasks.is_final(result["status"], result["result"]))

    def test_parent_can_reply_and_receive_the_next_child_report(self):
        child_id = self._waiting_child()
        delivered = []

        def deliver(task_id, message):
            delivered.append((task_id, message))
            return True

        with patch.object(scheduler, "get_session", return_value=object()), patch.object(
            scheduler, "send_subtask_reply", side_effect=deliver
        ), patch.object(
            subtasks,
            "wait_for_result",
            return_value={
                "status": "done", "result": "done", "summary": "完成",
                "needs_reply": False, "timed_out": False,
            },
        ):
            result = tasks_router.reply_to_subtask(
                child_id,
                tasks_router.SubtaskReplyBody(message="选 A，继续"),
                _Request(self.parent_token),
            )

        self.assertEqual(delivered, [(child_id, "选 A，继续")])
        self.assertEqual(result["summary"], "完成")
        row = db.query_one("SELECT report_result, report_summary FROM task WHERE id=?", (child_id,))
        self.assertIsNone(row["report_result"])
        self.assertIsNone(row["report_summary"])

    def test_child_token_cannot_reply_as_parent(self):
        child_id = self._waiting_child()
        child_token = auth.issue_task_token(child_id)
        auth.set_password("test-password")
        try:
            with self.assertRaises(tasks_router.HTTPException) as raised:
                tasks_router.reply_to_subtask(
                    child_id,
                    tasks_router.SubtaskReplyBody(message="冒充父任务"),
                    _Request(child_token),
                )
        finally:
            auth.clear_password()

        self.assertEqual(raised.exception.status_code, 401)

    def test_failed_delivery_preserves_the_child_question(self):
        child_id = self._waiting_child()
        with patch.object(scheduler, "get_session", return_value=object()), patch.object(
            scheduler, "send_subtask_reply", return_value=False
        ):
            with self.assertRaises(tasks_router.HTTPException) as raised:
                tasks_router.reply_to_subtask(
                    child_id,
                    tasks_router.SubtaskReplyBody(message="选 A"),
                    _Request(self.parent_token),
                )

        self.assertEqual(raised.exception.status_code, 409)
        row = db.query_one(
            "SELECT status, report_result, report_summary FROM task WHERE id=?", (child_id,)
        )
        self.assertEqual(row["status"], "waiting_input")
        self.assertEqual(row["report_result"], "needs_input")
        self.assertEqual(row["report_summary"], "选 A 还是 B？")

    def test_parent_acknowledgement_closes_child_without_losing_result(self):
        child_id = self._waiting_child()
        db.execute(
            "UPDATE task SET report_result='done', report_summary='验证完成' WHERE id=?",
            (child_id,),
        )

        class Session:
            stopped = 0

            def graceful_stop(self):
                self.stopped += 1

        session = Session()
        with patch.object(scheduler, "_sessions", {child_id: session}), patch.object(
            scheduler, "tick"
        ), patch.object(scheduler, "_finalize_tokens"):
            first = tasks_router.acknowledge_subtask_result(child_id, _Request(self.parent_token))
            second = tasks_router.acknowledge_subtask_result(child_id, _Request(self.parent_token))
            self.assertIsNone(scheduler.get_session(child_id))

        self.assertEqual(session.stopped, 1)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "done")
        result = tasks_router.get_task_result(child_id, _Request(self.parent_token))
        self.assertEqual(result["summary"], "验证完成")
        self.assertTrue(result["finished"])

    def test_acknowledgement_settles_result_when_process_already_exited(self):
        child_id = self._waiting_child()
        db.execute("UPDATE task SET report_result='done' WHERE id=?", (child_id,))
        with patch.object(scheduler, "_sessions", {}), patch.object(
            scheduler, "tick"
        ), patch.object(scheduler, "_finalize_tokens"):
            result = tasks_router.acknowledge_subtask_result(child_id, _Request(self.parent_token))
        self.assertEqual(result["status"], "done")

    def test_acknowledgement_does_not_close_a_child_awaiting_an_answer(self):
        child_id = self._waiting_child()
        with self.assertRaises(tasks_router.HTTPException) as raised:
            tasks_router.acknowledge_subtask_result(child_id, _Request(self.parent_token))
        self.assertEqual(raised.exception.status_code, 409)
        result = tasks_router.get_task_result(child_id, _Request(self.parent_token))
        self.assertTrue(result["needs_reply"])
        self.assertEqual(result["summary"], "选 A 还是 B？")

    def test_exited_child_with_unanswered_question_cannot_be_acknowledged(self):
        child_id = self._waiting_child()
        db.execute("UPDATE task SET status='done' WHERE id=?", (child_id,))
        result = tasks_router.get_task_result(child_id, _Request(self.parent_token))
        self.assertTrue(result["needs_reply"])
        self.assertFalse(result["finished"])
        with self.assertRaises(tasks_router.HTTPException) as raised:
            tasks_router.acknowledge_subtask_result(child_id, _Request(self.parent_token))
        self.assertEqual(raised.exception.status_code, 409)

    def test_session_cleanup_preserves_an_unanswered_question_without_process(self):
        child_id = self._waiting_child()
        with (
            patch.object(scheduler, "_sessions", {}),
            patch.object(scheduler, "tick"),
            patch.object(scheduler, "_finalize_tokens"),
        ):
            scheduler.finish_subtask(child_id)
        result = tasks_router.get_task_result(child_id, _Request(self.parent_token))
        self.assertEqual(result["status"], "waiting_input")
        self.assertTrue(result["needs_reply"])
        self.assertEqual(result["summary"], "选 A 还是 B？")

    def test_acknowledgement_preserves_failure_status(self):
        child_id = self._task("failed", self.parent_id)
        db.execute("UPDATE task SET report_result='failed' WHERE id=?", (child_id,))
        with patch.object(scheduler, "_sessions", {}), patch.object(scheduler, "tick"):
            result = tasks_router.acknowledge_subtask_result(child_id, _Request(self.parent_token))
        self.assertEqual(result["status"], "failed")

    def test_acknowledgement_http_route_accepts_only_the_owning_parent(self):
        child_id = self._task("done", self.parent_id)
        child_token = auth.issue_task_token(child_id)
        other_parent = self._task("running")
        other_token = auth.issue_task_token(other_parent)
        client = TestClient(app)
        with patch.object(auth, "is_enabled", return_value=True), patch.object(
            scheduler, "_sessions", {}
        ), patch.object(scheduler, "tick"):
            for token in (child_token, other_token):
                response = client.post(
                    f"/api/tasks/{child_id}/result/ack",
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(response.status_code, 401)
            response = client.post(
                f"/api/tasks/{child_id}/result/ack",
                headers={"Authorization": f"Bearer {self.parent_token}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "done")

    def test_completed_parent_cannot_keep_driving_child(self):
        child_id = self._waiting_child()
        db.execute("UPDATE task SET status='done' WHERE id=?", (self.parent_id,))

        with self.assertRaises(tasks_router.HTTPException) as raised:
            tasks_router.reply_to_subtask(
                child_id,
                tasks_router.SubtaskReplyBody(message="继续"),
                _Request(self.parent_token),
            )

        self.assertEqual(raised.exception.status_code, 409)


class SessionMessageDeliveryTest(unittest.TestCase):
    class _Loop:
        def call_soon_threadsafe(self, function, *args):
            function(*args)

    def test_pty_multiline_message_is_submitted_as_one_bracketed_paste(self):
        writes = []

        class _Process:
            def isalive(self):
                return True

            def write(self, data):
                writes.append(data)

        session = PtySession.__new__(PtySession)
        session.proc = _Process()
        session.loop = self._Loop()
        session.task_id = 1
        session.on_status = lambda *args: None
        session.status = "waiting_input"
        session._reported = True
        session._wait_reason = "reported"
        session._waiting_kind = "review"
        session._buf = ""
        session._nudge_sent = True
        session.last_output = 0.0

        session.submit_message("第一行\n第二行")

        self.assertEqual(
            writes,
            [b"\x1b[200~\xe7\xac\xac\xe4\xb8\x80\xe8\xa1\x8c\n\xe7\xac\xac\xe4\xba\x8c\xe8\xa1\x8c\x1b[201~\r"],
        )
        self.assertFalse(session._reported)
        self.assertEqual(session.status, "running")

    def test_sdk_message_starts_the_next_turn(self):
        queued = []

        class _Queue:
            def put_nowait(self, item):
                queued.append(item)

        session = SdkSession.__new__(SdkSession)
        session.status = "waiting_input"
        session._reported = True
        session._sdk_loop = self._Loop()
        session._input_q = _Queue()
        session.loop = self._Loop()
        session.task_id = 1
        session.on_status = lambda *args: None
        session._event = lambda event: None

        session.submit_message("选 A\n继续")

        self.assertEqual(queued, ["选 A\n继续"])
        self.assertFalse(session._reported)
        self.assertEqual(session.status, "running")


if __name__ == "__main__":
    unittest.main()
