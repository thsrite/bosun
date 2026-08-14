"""rate_limited 是活动态：能人工取消，且各视图都必须显示它。

事故：编排第 4 步的任务撞限流转 rate_limited 后，前端活动区/看板都没收录这个状态，
任务从界面上凭空消失（后端仍在正常等待自动续跑）。
"""
import os
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.app import db, scheduler


class CancelRateLimitedTaskTest(unittest.TestCase):
    def setUp(self):
        self._old_conn = db._conn
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        db._conn = self.conn
        db.init_db()
        db.execute(
            "INSERT INTO project(id,name,path,created_at) VALUES(1,'p','/tmp/p',?)",
            (time.time(),),
        )

    def tearDown(self):
        db._conn = self._old_conn
        self.conn.close()

    def test_cancel_stops_rate_limited_task(self):
        """限流等待中的任务必须能人工取消，且不能再被自动续跑捞回队列。"""
        db.execute(
            "INSERT INTO task(id,project_id,engine,prompt,status,session_uid,resume_after,created_at) "
            "VALUES(1,1,'cc','原始指令','rate_limited','s-1',?,?)",
            (time.time() - 1, time.time()),
        )
        with patch.object(scheduler, "tick"):
            scheduler.cancel(1)
        row = db.query_one("SELECT * FROM task WHERE id=1")
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["resume_after"])
        scheduler._resume_rate_limited()
        self.assertEqual(db.query_one("SELECT status FROM task WHERE id=1")["status"], "cancelled")


class RateLimitedFrontendVisibilityTest(unittest.TestCase):
    SRC = os.path.join(ROOT, "frontend", "src")

    def _read(self, *parts):
        with open(os.path.join(self.SRC, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_active_tasks_view_lists_rate_limited(self):
        text = self._read("components", "ActiveTasksView.tsx")
        self.assertIn(
            'const ACTIVE_STATUSES = new Set(["queued", "running", "waiting_input", "rate_limited"])', text
        )

    def test_task_board_active_column_includes_rate_limited(self):
        text = self._read("components", "TaskBoard.tsx")
        self.assertIn('statuses: ["queued", "running", "waiting_input", "rate_limited"]', text)

    def test_task_card_allows_cancel_when_rate_limited(self):
        text = self._read("components", "TaskCard.tsx")
        self.assertIn('const active = ["running", "waiting_input", "queued", "rate_limited"]', text)

    def test_project_view_terminal_switch_includes_rate_limited(self):
        text = self._read("components", "ProjectView.tsx")
        self.assertIn('["queued", "running", "waiting_input", "rate_limited"].includes(t.status)', text)


if __name__ == "__main__":
    unittest.main()
