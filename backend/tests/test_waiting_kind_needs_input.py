"""agent 回报 needs_input 时，列表接口的等待类型应是 input（等你答复），而不是 review（待核对）。"""
import unittest
from unittest.mock import patch

from app import scheduler
from app.routers import tasks as tasks_router


class WaitingKindTest(unittest.TestCase):
    def _annotate(self, report_result, session_kind="review", pending=None):
        with patch.object(scheduler, "get_permission", return_value=pending), patch.object(
            scheduler, "get_waiting_kind", return_value=session_kind
        ):
            return tasks_router._annotate_waiting(
                {"id": 1, "status": "waiting_input", "report_result": report_result}
            )["waiting_kind"]

    def test_needs_input_report_is_input(self):
        self.assertEqual(self._annotate("needs_input"), "input")

    def test_done_report_stays_review(self):
        self.assertEqual(self._annotate("done"), "review")

    def test_pending_permission_wins(self):
        self.assertEqual(self._annotate("needs_input", pending={"tool": "Bash"}), "permission")

    def test_without_report_uses_session_kind(self):
        self.assertEqual(self._annotate(None, session_kind="choice"), "choice")


if __name__ == "__main__":
    unittest.main()
