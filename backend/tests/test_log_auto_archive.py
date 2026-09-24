import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.app import config, db, log_archive


class ArchiveEndedLogsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_conn = db._conn
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        db._conn = self.conn
        db.init_db()
        db.execute(
            "INSERT INTO project(id,name,path,created_at) VALUES(1,'p','/tmp/p',?)",
            (time.time(),),
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)
        patcher = mock.patch.object(config, "LOG_DIR", self.log_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.conn.close()
        db._conn = self._old_conn

    def _task_log(self, status: str, age_seconds: float) -> Path:
        path = self.log_dir / f"task-{status}-{age_seconds:.0f}.log"
        path.write_bytes(b"\x1b[?25lspinner\x1b[?25h" * 100)
        mtime = time.time() - age_seconds
        os.utime(path, (mtime, mtime))
        db.execute(
            "INSERT INTO task(project_id,engine,prompt,status,log_path,created_at) "
            "VALUES(1,'omp','x',?,?,?)",
            (status, str(path), time.time()),
        )
        return path

    def test_auto_archive_skips_recent_and_live_logs(self):
        old_done = self._task_log("done", 4 * 86400)
        recent_done = self._task_log("done", 3600)
        old_running = self._task_log("running", 4 * 86400)

        count, saved = log_archive.archive_ended_logs(log_archive.AUTO_ARCHIVE_IDLE_SECONDS)

        self.assertEqual(count, 1)
        self.assertGreater(saved, 0)
        self.assertFalse(old_done.exists())
        self.assertTrue(log_archive.gz_path(old_done).exists())
        self.assertIn("spinner", log_archive.read_text(old_done))
        self.assertTrue(recent_done.exists())
        self.assertTrue(old_running.exists())

    def test_manual_compress_ignores_idle_age(self):
        recent_done = self._task_log("done", 60)
        count, _ = log_archive.archive_ended_logs()
        self.assertEqual(count, 1)
        self.assertFalse(recent_done.exists())


if __name__ == "__main__":
    unittest.main()
