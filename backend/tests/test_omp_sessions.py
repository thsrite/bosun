import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import db, sessions
from app.routers import sessions as sessions_router


class OmpSessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.project = self.home / "PycharmProjects" / "license-server"
        self.project.mkdir(parents=True)
        self.store = self.root / "sessions"
        for mocked in (
            patch.object(Path, "home", return_value=self.home),
            patch.object(tempfile, "gettempdir", return_value=str(self.root / "tmp")),
            patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.root)}),
            patch.object(sessions, "claude_projects", return_value=self.root / "claude"),
            patch.object(sessions, "CODEX_SESSIONS", self.root / "codex"),
            patch.object(sessions, "kimi_home", return_value=self.root / "kimi"),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        connection_patch = patch.object(db, "_conn", self.connection)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)
        db.init_db()
        db.execute(
            "INSERT INTO project(id,name,path,created_at) VALUES(1,'license-server',?,?)",
            (str(self.project), time.time()),
        )
        self.uid = "01a0b854-1d8c-76d5-9a4e-62a1a0b772ca"

    def test_config_root_discovers_sessions_without_agent_override(self):
        with patch.dict(os.environ, {"PI_CONFIG_DIR": ".omp-work"}):
            os.environ.pop("PI_CODING_AGENT_DIR", None)
            self.write_transcript(self.home / ".omp-work" / "agent" / "sessions" /
                                  "-PycharmProjects-license-server")
            self.assertEqual(sessions.local_session_info("omp", str(self.project), self.uid)["cwd"],
                             str(self.project))

    def transcript(self, cwd=None):
        return "\n".join(json.dumps(row) for row in (
            {"type": "title", "v": 1, "title": "OMP planning"},
            {"type": "session", "version": 3, "id": self.uid,
             "cwd": str(cwd or self.project), "timestamp": "2026-09-19T06:21:55.468Z"},
            {"type": "message", "message": {"role": "user", "content": [
                {"type": "text", "text": "Implement the agreed plan"}]}},
        )) + "\n"

    def write_transcript(self, directory, cwd=None):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"2026-09-19T06-21-55-468Z_{self.uid}.jsonl"
        path.write_text(self.transcript(cwd))
        return path

    def test_discover_attach_and_read_current_home_relative_session(self):
        self.write_transcript(self.store / "-PycharmProjects-license-server")
        items = sessions_router.discover_sessions(1)["sessions"]
        self.assertEqual([(s["engine"], s["session_uid"], s["prompt"]) for s in items], [
            ("omp", self.uid, "Implement the agreed plan")])
        body = sessions_router.AttachBody(project_id=1, engine="omp", session_uid=self.uid)
        result = sessions_router.attach_local_session(body)
        task = db.query_one("SELECT * FROM task WHERE id=?", (result["task_id"],))
        self.assertEqual((task["engine"], task["resume"], task["status"]), ("omp", 1, "draft"))
        self.assertEqual(sessions_router.attach_local_session(body)["created"], False)
        self.assertEqual(sessions_router.discover_sessions(1)["sessions"][0]["task_id"], task["id"])
        self.assertEqual(sessions.read_session("omp", str(self.project), self.uid), self.transcript())

    def test_share_import_writes_current_bucket_and_reroots_header(self):
        result = sessions_router.import_session(sessions_router.ImportBody(
            project_id=1, bundle=sessions_router.Bundle(
                engine="omp", session_uid=self.uid, jsonl=self.transcript("/other/project"))))
        files = list((self.store / "-PycharmProjects-license-server").glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        header = json.loads(files[0].read_text().splitlines()[1])
        self.assertEqual(header["cwd"], str(self.project))
        self.assertEqual(sessions_router.discover_sessions(1)["sessions"][0]["task_id"], result["task_id"])

    def test_temp_absolute_and_symlink_projects_use_omp_buckets(self):
        cases = (
            (self.root / "tmp" / "build", "-tmp-build"),
            (self.root / "outside" / "repo", f"--{str(self.root).lstrip('/').replace('/', '-')}-outside-repo--"),
            (self.home, "-"),
        )
        for cwd, bucket in cases:
            with self.subTest(cwd=cwd):
                cwd.mkdir(parents=True, exist_ok=True)
                self.write_transcript(self.store / bucket, cwd)
                self.assertEqual(sessions.local_session_info("omp", str(cwd), self.uid)["cwd"], str(cwd))
        alias = self.root / "alias"
        alias.symlink_to(self.project, target_is_directory=True)
        self.write_transcript(self.store / "-PycharmProjects-license-server")
        self.assertEqual(sessions.local_session_info("omp", str(alias), self.uid)["cwd"], str(self.project))

    def test_legacy_absolute_and_hashed_buckets_remain_readable(self):
        absolute = f"--{str(self.project).lstrip('/').replace('/', '-')}--"
        for bucket in (absolute, f"home-license-server-{sessions.omp_dir_digest(str(self.project))}"):
            with self.subTest(bucket=bucket):
                path = self.write_transcript(self.store / bucket)
                self.assertEqual(sessions.local_session_info("omp", str(self.project), self.uid)["prompt"],
                                 "Implement the agreed plan")
                path.unlink()

    def test_flat_shared_root_rejects_another_projects_session(self):
        self.write_transcript(self.store, self.home / "unrelated")
        self.assertEqual(sessions_router.discover_sessions(1)["sessions"], [])
        with self.assertRaises(HTTPException) as caught:
            sessions_router.attach_local_session(sessions_router.AttachBody(
                project_id=1, engine="omp", session_uid=self.uid))
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIsNone(sessions.capture_omp_session(str(self.project), set(), 0,
                          prompt="Implement the agreed plan"))
        self.write_transcript(self.store)
        self.assertEqual(sessions.local_session_info("omp", str(self.project), self.uid)["cwd"],
                         str(self.project))

    def test_colliding_path_names_do_not_import_foreign_session(self):
        self.write_transcript(self.store / "-PycharmProjects-license-server", self.home / "PycharmProjects-license-server")
        self.assertEqual(sessions.discover_local_sessions(str(self.project)), [])
        self.assertIsNone(sessions.local_session_info("omp", str(self.project), self.uid))

    def test_capture_finds_new_current_layout_session(self):
        before = sessions.snapshot_omp(str(self.project))
        self.write_transcript(self.store / "-PycharmProjects-license-server")
        self.assertEqual(sessions.capture_omp_session(str(self.project), before, 0,
                         prompt="Implement the agreed plan"), self.uid)


if __name__ == "__main__":
    unittest.main()
