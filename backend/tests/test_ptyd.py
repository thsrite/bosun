"""会话宿主（ptyd）：后端断开后进程继续跑、输出攒着，重新 attach 时补交并能被调度器接管。"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db, ptyd, ptyd_client, scheduler


def _read_until(proc, needle: bytes, timeout: float = 5.0) -> bytes:
    """在读线程里收输出直到出现 needle（RemotePty.read 是阻塞的）。"""
    buf = bytearray()
    done = threading.Event()

    def pump():
        try:
            while needle not in buf:
                buf.extend(proc.read(4096))
        except EOFError:
            pass
        done.set()

    threading.Thread(target=pump, daemon=True).start()
    done.wait(timeout)
    return bytes(buf)


@unittest.skipIf(sys.platform == "win32", "ptyd 仅用于 POSIX")
class PtydTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ptyd")
        self.sock_path = Path(self.tmp) / "p.sock"
        self._env = patch.dict(os.environ, {"BOSUN_PTYD_SOCKET": str(self.sock_path), "BOSUN_PTYD": "1"})
        self._env.start()
        self.host = ptyd.PtyHost(self.sock_path)
        threading.Thread(target=self.host.serve, daemon=True).start()
        deadline = time.time() + 3
        while ptyd_client.ping() is None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(ptyd_client.ping())

    def tearDown(self):
        for item in ptyd_client.list_sessions() or []:
            ptyd_client.forget(item["key"])
        self.host.shutdown()
        self._env.stop()

    def _spawn(self, script: str, task_id: int = 1) -> ptyd_client.RemotePty:
        return ptyd_client.RemotePty.spawn(
            f"task-{task_id}-t",
            ["/bin/sh", "-c", script],
            cwd=self.tmp,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            dimensions=(24, 80),
            meta={"task_id": task_id},
        )


class PtydHostTest(PtydTestBase):
    def test_output_survives_client_disconnect_and_is_replayed_on_attach(self):
        proc = self._spawn("echo ready; read x; echo got-$x; sleep 0.3; echo bye")
        self.assertIn(b"ready", _read_until(proc, b"ready"))
        # 模拟后端重启：连接断了，但进程不能死
        proc.detach()
        time.sleep(0.2)
        [item] = ptyd_client.list_sessions()
        self.assertTrue(item["alive"])
        self.assertFalse(item["attached"])

        again = ptyd_client.RemotePty.attach(item["key"])
        again.write(b"hello\r")
        out = _read_until(again, b"\x00never")  # 读到退出为止
        self.assertIn(b"got-hello", out)
        self.assertIn(b"bye", out)
        self.assertEqual(again.wait(), 0)

    def test_exit_while_detached_is_kept_until_reattached(self):
        proc = self._spawn("sleep 0.3; echo done-output; exit 3")
        proc.detach()
        deadline = time.time() + 3
        while time.time() < deadline:
            items = ptyd_client.list_sessions()
            if items and not items[0]["alive"]:
                break
            time.sleep(0.05)
        [item] = ptyd_client.list_sessions()
        self.assertFalse(item["alive"])

        again = ptyd_client.RemotePty.attach(item["key"])
        self.assertIn(b"done-output", _read_until(again, b"\x00never"))
        self.assertEqual(again.wait(), 3)
        self.assertEqual(ptyd_client.list_sessions(), [])

    def test_kill_drops_session(self):
        proc = self._spawn("sleep 30")
        proc.terminate(force=True)
        _read_until(proc, b"\x00never")
        self.assertFalse(proc.isalive())
        time.sleep(0.1)
        self.assertEqual(ptyd_client.list_sessions(), [])


class PtydAdoptTest(PtydTestBase):
    def setUp(self):
        super().setUp()
        self._old_conn = db._conn
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        db._conn = self.conn
        db.init_db()
        self.project_id = db.execute(
            "INSERT INTO project(name,path,created_at) VALUES(?,?,?)", ("P", self.tmp, time.time())
        )
        self.loop = asyncio.new_event_loop()
        self._old_loop = scheduler._loop
        scheduler._loop = self.loop
        self._old_sessions = dict(scheduler._sessions)
        scheduler._sessions.clear()

    def tearDown(self):
        for session in list(scheduler._sessions.values()):
            session.terminate()
        scheduler._sessions.clear()
        scheduler._sessions.update(self._old_sessions)
        scheduler._loop = self._old_loop
        self.loop.close()
        self.conn.close()
        db._conn = self._old_conn
        super().tearDown()

    def _task(self, status: str) -> int:
        return db.execute(
            "INSERT INTO task(project_id,engine,prompt,status,priority,auto_approve,created_at,started_at,log_path) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (self.project_id, "codex", "work", status, 5, 0, time.time(), time.time() - 60, None),
        )

    def test_live_session_is_adopted_and_keeps_logging(self):
        task_id = self._task("running")
        log_path = str(Path(self.tmp) / "task.log")
        db.execute("UPDATE task SET log_path=? WHERE id=?", (log_path, task_id))
        proc = self._spawn("echo before; read x; echo after-$x; sleep 5", task_id)
        _read_until(proc, b"before")
        proc.detach()  # 旧后端退出

        scheduler._adopt_hosted_sessions()

        session = scheduler.get_session(task_id)
        self.assertIsNotNone(session)
        self.assertTrue(session.is_alive())
        session.write("x\r")
        deadline = time.time() + 5
        while time.time() < deadline:
            if b"after-x" in Path(log_path).read_bytes():
                break
            time.sleep(0.05)
        self.assertIn(b"after-x", Path(log_path).read_bytes())

        # 已接管的会话不会被对账标成中断
        scheduler._reconcile()
        self.assertEqual(db.query_one("SELECT status FROM task WHERE id=?", (task_id,))["status"], "running")

    def test_orphan_for_finished_task_is_killed(self):
        task_id = self._task("cancelled")
        proc = self._spawn("sleep 30", task_id)
        proc.detach()

        scheduler._adopt_hosted_sessions()

        self.assertIsNone(scheduler.get_session(task_id))
        deadline = time.time() + 3
        while ptyd_client.list_sessions() and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(ptyd_client.list_sessions(), [])


class ResumeAfterRestartTest(unittest.TestCase):
    def setUp(self):
        self._old_conn = db._conn
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        db._conn = self.conn
        db.init_db()
        self.project_id = db.execute(
            "INSERT INTO project(name,path,created_at) VALUES(?,?,?)", ("P", "/tmp", time.time())
        )

    def tearDown(self):
        self.conn.close()
        db._conn = self._old_conn

    def _task(self, status: str, render_mode: str = "chat", session_uid: str | None = "sid-1") -> int:
        return db.execute(
            "INSERT INTO task(project_id,engine,prompt,status,priority,auto_approve,created_at,started_at,"
            "render_mode,session_uid) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.project_id, "claude", "原始指令", status, 5, 0, time.time(),
             scheduler._BOOT_TIME - 120, render_mode, session_uid),
        )

    def test_sdk_task_from_previous_backend_is_resumed(self):
        task_id = self._task("running")
        scheduler._reconcile()
        row = db.query_one("SELECT * FROM task WHERE id=?", (task_id,))
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["resume"], 1)
        self.assertEqual(row["prompt"], scheduler.RESTART_RESUME_PROMPT)
        self.assertEqual(row["original_prompt"], "原始指令")

    def test_waiting_sdk_task_resumes_without_new_instruction(self):
        task_id = self._task("waiting_input")
        scheduler._reconcile()
        row = db.query_one("SELECT status, prompt FROM task WHERE id=?", (task_id,))
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["prompt"], "")

    def test_terminal_task_or_missing_session_is_still_interrupted(self):
        pty_task = self._task("running", render_mode="terminal")
        no_uid = self._task("running", session_uid=None)
        scheduler._reconcile()
        for task_id in (pty_task, no_uid):
            self.assertEqual(
                db.query_one("SELECT status FROM task WHERE id=?", (task_id,))["status"], "interrupted"
            )


if __name__ == "__main__":
    unittest.main()
