"""限流等待机制摘除后，历史遗留的 rate_limited 任务不能变成看不见的孤儿。

前端已不再收录 rate_limited，调度器也不再有人把它捞回队列；旧库里停在该状态的任务
如果不迁移，就会永久卡在看板之外（正是「任务凭空消失」这个事故本身）。
"""
import sqlite3
import time
import unittest

from app import db


class RateLimitedMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_conn = db._conn
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        db._conn = self.conn
        db.init_db()
        self.project_id = db.execute(
            "INSERT INTO project(name,path,created_at) VALUES(?,?,?)",
            ("Bosun", "/tmp/bosun", time.time()),
        )

    def tearDown(self) -> None:
        self.conn.close()
        db._conn = self._old_conn

    def _insert(self, status: str, ended_at: float | None = None) -> int:
        return db.execute(
            "INSERT INTO task(project_id,engine,prompt,status,ended_at,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (self.project_id, "claude", "指令", status, ended_at, time.time()),
        )

    def test_legacy_rate_limited_task_becomes_done(self):
        task_id = self._insert("rate_limited")

        db._migrate_rate_limited_tasks(self.conn)

        row = db.query_one("SELECT status, ended_at, exit_code FROM task WHERE id=?", (task_id,))
        self.assertEqual(row["status"], "done")
        self.assertIsNotNone(row["ended_at"])  # 归入终态列必须有结束时间
        self.assertIsNone(row["exit_code"])    # 没真跑完，不伪造成功退出码

    def test_other_statuses_untouched(self):
        running = self._insert("running")
        done = self._insert("done", ended_at=123.0)

        db._migrate_rate_limited_tasks(self.conn)

        self.assertEqual(
            db.query_one("SELECT status FROM task WHERE id=?", (running,))["status"], "running"
        )
        self.assertEqual(
            db.query_one("SELECT ended_at FROM task WHERE id=?", (done,))["ended_at"], 123.0
        )


if __name__ == "__main__":
    unittest.main()
