import sqlite3
import time
import unittest
from unittest.mock import patch

from app import db, engines, scheduler, subtasks
from app.routers import tasks as tasks_router


class EngineAliasTest(unittest.TestCase):
    def test_claude_names_normalize_to_cc(self):
        for alias in ("cc", "claude", "claude-code", "claude code"):
            with self.subTest(alias=alias):
                self.assertEqual(engines.normalize_engine_id(alias), "cc")

    def test_other_engine_ids_are_unchanged(self):
        for engine in ("codex", "omp", "kimi", "unknown"):
            with self.subTest(engine=engine):
                self.assertEqual(engines.normalize_engine_id(engine), engine)


class SpawnAliasTest(unittest.TestCase):
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
        self.parent_id = db.execute(
            "INSERT INTO task(project_id,engine,prompt,status,created_at) "
            "VALUES(1,'codex','父任务','running',?)",
            (time.time(),),
        )

    def tearDown(self):
        db._conn = self.old_connection
        self.connection.close()

    def test_spawn_accepts_claude_aliases_and_stores_cc(self):
        for alias in ("claude", "claude-code", "claude code"):
            with self.subTest(alias=alias), patch.object(
                subtasks, "acquire_slot", return_value=True
            ), patch.object(subtasks, "release_slot"), patch.object(
                scheduler, "start_subtask"
            ), patch.object(
                subtasks, "wait_for_result", return_value={"status": "done", "summary": "ok"}
            ):
                result = tasks_router.spawn_subtask(
                    self.parent_id,
                    tasks_router.SpawnBody(engine=alias, prompt="复审"),
                    None,
                )

            row = db.query_one("SELECT engine FROM task WHERE id=?", (result["id"],))
            self.assertEqual(result["engine"], "cc")
            self.assertEqual(row["engine"], "cc")


if __name__ == "__main__":
    unittest.main()
