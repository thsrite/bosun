import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import agy, autopilot, db, engine_models, engine_policy, engine_settings, engines, routing, scheduler, sessions
from app.routers import tasks, settings


class AgyEngineTest(unittest.TestCase):
    def setUp(self):
        self.old_connection = db._conn
        db._conn = sqlite3.connect(":memory:", check_same_thread=False)
        db._conn.row_factory = sqlite3.Row
        db.init_db()
        db.execute("INSERT INTO project(id,name,path,created_at) VALUES(1,'p','/tmp/p',?)", (time.time(),))

    def tearDown(self):
        db._conn.close()
        db._conn = self.old_connection

    def test_interactive_resume_and_permission_flags(self):
        db.set_setting("agy_model", "gemini-4-carbon-high")
        db.set_setting("agy_effort", "high")
        prompt = "检查包含 $(touch /tmp/no) 的代码"
        with patch.object(engines, "AGY_BIN", "/bin/agy"), patch.object(engines.agent_skills, "ensure_for_dispatch"):
            args = engines.build_argv("agy", prompt, False)
            self.assertEqual(args, ["/bin/agy", "--model", "gemini-4-carbon-high", "--effort", "high", "--prompt-interactive", prompt])
            resumed = engines.build_resume_argv("agy", "session-id", "", True)
            self.assertIn("--dangerously-skip-permissions", resumed)
            self.assertNotIn("--prompt-interactive", resumed)
            self.assertEqual(resumed[-2:], ["--conversation", "session-id"])
            override = engines.build_argv("agy", "任务", False, model_override="custom", reasoning_override="low")
            self.assertEqual(override[1:5], ["--model", "custom", "--effort", "low"])

    def test_headless_output_and_usage(self):
        with patch.object(engines, "AGY_BIN", "agy"):
            self.assertEqual(engines.build_headless_argv("agy", "审查", False, True),
                             ["agy", "--output-format", "json", "--print", "审查"])
        output = json.dumps({"response": "PASS", "usage": {"input_tokens": 11, "output_tokens": 3, "cache_read_tokens": 500}})
        self.assertEqual(autopilot._parse_tokens("agy", output), 14)
        self.assertEqual(autopilot.assistant_text("agy", output), "PASS")
        self.assertEqual(autopilot.assistant_text("agy", output + "\n诊断信息"), "PASS")
        self.assertEqual(autopilot._parse_tokens("agy", output + "\n诊断信息"), 14)

    def test_capture_is_scoped_to_task_and_run(self):
        uid = "12345678-abcd-1234-abcd-123456789012"
        with tempfile.TemporaryDirectory() as tmp, patch.object(agy.config, "LOG_DIR", Path(tmp)):
            agy.task_log(1, 12.0).write_text(f"I client.go:123] Created conversation {uid} (backend=local)\n")
            self.assertEqual(agy.capture_session(1, 12.0), uid)
            self.assertIsNone(agy.capture_session(2, 12.0))
            self.assertIsNone(agy.capture_session(1, 13.0))

    def test_models_from_official_cli_and_failure(self):
        result = subprocess.CompletedProcess([], 0, "Fetching available models...\ngemini-4-carbon-high\tGemini 4 Carbon (High)\ncustom-model  自定义模型\n", "")
        with patch.object(engine_models, "_run", return_value=result):
            self.assertEqual(len(engine_models.refresh_model_options("agy", "agy")), 3)
        result.returncode = 1
        result.stderr = "请先登录"
        with patch.object(engine_models, "_run", return_value=result), self.assertRaises(engine_models.ModelDiscoveryError):
            engine_models.refresh_model_options("agy", "agy")

    def test_history_and_resume_are_bound_to_project(self):
        uid = "12345678-abcd-1234-abcd-123456789012"
        db.execute("INSERT INTO task(project_id,engine,prompt,status,session_uid,created_at) VALUES(1,'agy','你好','done',?,?)", (uid, time.time()))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            path.write_text("\n".join(json.dumps(item) for item in [
                {"type": "USER_INPUT", "content": "<USER_REQUEST>\n你好\n</USER_REQUEST>\n元数据"},
                {"type": "AGENT_RESPONSE", "content": "你好！"},
            ]))
            with patch.object(agy, "transcript_path", return_value=path):
                self.assertIsNotNone(sessions.local_session_info("agy", "/tmp/p", uid))
                self.assertIsNone(sessions.local_session_info("agy", "/tmp/other", uid))
                history = sessions.session_history("agy", "/tmp/p", uid)
                self.assertEqual([m["text"] for m in history["messages"]], ["你好", "你好！"])
        with self.assertRaises(ValueError):
            sessions.write_session("agy", "/tmp/p", uid, "{}")

    def test_disabled_engine_rejected_but_subagent_toggle_keeps_manual_use(self):
        db.set_setting("engine_subagent_enabled:agy", "0")
        self.assertIsNone(engine_policy.rejection("agy"))
        self.assertIsNotNone(engine_policy.rejection("agy", subagent=True))
        created = tasks.create_task(tasks.CreateTask(project_id=1, engine="agy", prompt="任务"))
        db.set_setting("engine_enabled:agy", "0")
        with self.assertRaises(HTTPException):
            tasks.create_task(tasks.CreateTask(project_id=1, engine="agy", prompt="任务"))
        with self.assertRaises(HTTPException):
            tasks.start_task(created["id"])
        self.assertEqual(db.query_one("SELECT status FROM task WHERE id=?", (created["id"],))["status"], "draft")

    def test_disabled_subagent_cannot_bypass_spawn_api(self):
        parent = db.execute("INSERT INTO task(project_id,engine,prompt,status,created_at) VALUES(1,'codex','父任务','running',?)", (time.time(),))
        db.set_setting("engine_subagent_enabled:agy", "0")
        with patch.object(tasks.subtasks, "enabled", return_value=True), self.assertRaises(HTTPException) as error:
            tasks.spawn_subtask(parent, tasks.SpawnBody(engine="agy", prompt="子任务"))
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(db.query_one("SELECT COUNT(*) n FROM task WHERE parent_task_id=?", (parent,))["n"], 0)

    def test_auto_routing_respects_both_switches(self):
        with patch.object(routing.engine_updates, "is_installed", side_effect=lambda e: e == "agy"):
            self.assertEqual(routing.pick_engine()[0], "agy")
            db.set_setting("engine_subagent_enabled:agy", "0")
            with self.assertRaises(HTTPException):
                routing.pick_engine(subagent=True)
            self.assertEqual(routing.pick_engine()[0], "agy")
            db.set_setting("engine_enabled:agy", "0")
            with self.assertRaises(HTTPException):
                routing.pick_engine()

    def test_auto_routing_keeps_existing_tie_order(self):
        with patch.object(routing.engine_updates, "is_installed", return_value=True), \
             patch.object(routing.quota, "get_usage", return_value={}):
            self.assertEqual(routing.pick_engine()[0], "claude")

    def test_unrelated_settings_and_switches_do_not_stop_active_sessions(self):
        task_id = db.execute("INSERT INTO task(project_id,engine,prompt,status,created_at) VALUES(1,'agy','任务','running',?)", (time.time(),))
        db.set_setting("default_tab", "settings")
        db.set_setting("engine_subagent_enabled:codex", "0")
        self.assertIsNone(engine_policy.rejection("agy"))
        db.set_setting("engine_enabled:agy", "0")
        self.assertEqual(db.query_one("SELECT status FROM task WHERE id=?", (task_id,))["status"], "running")

    def test_settings_save_preserves_switches_when_old_client_omits_them(self):
        with patch.object(settings.agent_skills, "sync_installed_engines"), \
             patch.object(settings.agent_skills, "migrate_managed_skills"), \
             patch.object(settings.agent_skills, "path_info", return_value={}), \
             patch.object(settings.browser_computer, "availability", return_value={}), \
             patch.object(settings.scheduler, "tick"):
            saved = settings.update_settings(settings.Settings(max_concurrent=3,
                engine_enabled={"agy": False}, engine_subagent_enabled={"codex": False}))
            self.assertFalse(saved["engine_enabled"]["agy"])
            self.assertFalse(saved["engine_subagent_enabled"]["codex"])
            saved = settings.update_settings(settings.Settings(max_concurrent=4, default_tab="settings"))
            self.assertFalse(saved["engine_enabled"]["agy"])
            self.assertFalse(saved["engine_subagent_enabled"]["codex"])
            self.assertTrue(saved["engine_enabled"]["codex"])

    def test_queue_skips_disabled_engine_without_starving_other_tasks(self):
        blocked = db.execute("INSERT INTO task(project_id,engine,prompt,status,priority,created_at) VALUES(1,'agy','任务','queued',10,?)", (time.time(),))
        allowed = db.execute("INSERT INTO task(project_id,engine,prompt,status,created_at) VALUES(1,'codex','任务','queued',?)", (time.time(),))
        db.set_setting("engine_enabled:agy", "0")
        with patch.object(scheduler, "_max_concurrent", return_value=1), \
             patch.object(scheduler, "_running_count", return_value=0), \
             patch.object(scheduler, "_start_task") as start:
            scheduler.tick()
            self.assertEqual(start.call_args[0][0]["id"], allowed)
            self.assertEqual(db.query_one("SELECT status FROM task WHERE id=?", (blocked,))["status"], "queued")
            db.set_setting("engine_enabled:agy", "1")
            scheduler.tick()
            self.assertEqual(start.call_args[0][0]["id"], blocked)

    def test_settings_change_does_not_terminate_live_task(self):
        task_id = db.execute("INSERT INTO task(project_id,engine,prompt,status,created_at) VALUES(1,'agy','任务','running',?)", (time.time(),))
        from unittest.mock import Mock
        live = Mock()
        with patch.dict(scheduler._sessions, {task_id: live}), \
             patch.object(settings.agent_skills, "sync_installed_engines"), \
             patch.object(settings.agent_skills, "migrate_managed_skills"), \
             patch.object(settings.agent_skills, "path_info", return_value={}), \
             patch.object(settings.browser_computer, "availability", return_value={}), \
             patch.object(scheduler, "_running_count", return_value=1):
            settings.update_settings(settings.Settings(max_concurrent=3, default_tab="settings",
                engine_enabled={"agy": False}, engine_subagent_enabled={"agy": False}))
            live.terminate.assert_not_called()
            live.graceful_stop.assert_not_called()
            self.assertEqual(db.query_one("SELECT status FROM task WHERE id=?", (task_id,))["status"], "running")


if __name__ == "__main__":
    unittest.main()
