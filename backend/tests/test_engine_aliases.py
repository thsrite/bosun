import sqlite3
import json
import time
import unittest
from unittest.mock import patch

from app import db, engines, scheduler, subtasks
from app.routers import tasks as tasks_router


class EngineAliasTest(unittest.TestCase):
    def test_claude_names_and_legacy_cc_normalize_to_claude(self):
        for alias in ("cc", "claude", "claude-code", "claude code"):
            with self.subTest(alias=alias):
                self.assertEqual(engines.normalize_engine_id(alias), "claude")

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

    def test_spawn_accepts_claude_aliases_and_stores_claude(self):
        for alias in ("cc", "claude", "claude-code", "claude code"):
            with self.subTest(alias=alias), patch.object(
                subtasks, "acquire_slot", return_value=True
            ), patch.object(
                subtasks, "max_children", return_value=4
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
            self.assertEqual(result["engine"], "claude")
            self.assertEqual(row["engine"], "claude")

    def test_create_task_accepts_legacy_cc_and_returns_claude(self):
        result = tasks_router.create_task(tasks_router.CreateTask(
            project_id=1,
            engine="cc",
            prompt="执行任务",
        ))

        row = db.query_one("SELECT engine FROM task WHERE id=?", (result["id"],))
        self.assertEqual(result["engine"], "claude")
        self.assertEqual(row["engine"], "claude")


class EngineDataMigrationTest(unittest.TestCase):
    def setUp(self):
        self.old_connection = db._conn
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        db._conn = self.connection
        db.init_db()

    def tearDown(self):
        db._conn = self.old_connection
        self.connection.close()

    def test_init_db_migrates_legacy_engine_values_and_snapshot(self):
        now = time.time()
        db.execute("INSERT INTO project(id,name,path,created_at) VALUES(1,'p','/tmp/p',?)", (now,))
        db.execute(
            "INSERT INTO task(project_id,engine,prompt,status,created_at) VALUES(1,'cc','x','draft',?)",
            (now,),
        )
        db.execute(
            "INSERT INTO orchestration_run(definition_snapshot,project_id,original_prompt,status,created_at,updated_at) "
            "VALUES(?,1,'x','draft',?,?)",
            ('{"id":1,"name":"旧编排","steps":[{"position":1,"engine":"cc"}]}', now, now),
        )

        db.init_db()

        self.assertEqual(db.query_one("SELECT engine FROM task")["engine"], "claude")
        snapshot = db.query_one("SELECT definition_snapshot FROM orchestration_run")["definition_snapshot"]
        self.assertEqual(json.loads(snapshot)["steps"][0]["engine"], "claude")


if __name__ == "__main__":
    unittest.main()
