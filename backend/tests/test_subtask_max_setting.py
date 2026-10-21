"""子任务上限：设置页的值优先于环境变量，每次现读，改完立即生效。"""
import os
import sqlite3
import unittest
from unittest.mock import patch

from app import subtasks


class SubtaskMaxSettingTest(unittest.TestCase):
    def test_stored_setting_wins_over_env(self):
        with patch.dict(os.environ, {"BOSUN_SUBTASK_MAX": "5"}), patch.object(
            subtasks.db, "get_setting", return_value="2"
        ):
            self.assertEqual(subtasks.max_children(), 2)

    def test_env_used_when_setting_absent(self):
        with patch.dict(os.environ, {"BOSUN_SUBTASK_MAX": "5"}), patch.object(
            subtasks.db, "get_setting", return_value=None
        ):
            self.assertEqual(subtasks.max_children(), 5)

    def test_default_when_db_unavailable(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(
            subtasks.db, "get_setting", side_effect=sqlite3.OperationalError("no table")
        ):
            os.environ.pop("BOSUN_SUBTASK_MAX", None)
            self.assertEqual(subtasks.max_children(), subtasks.DEFAULT_MAX_CHILDREN)

    def test_setting_is_clamped(self):
        self.assertEqual(subtasks.clamp_max_children(0), 1)
        self.assertEqual(subtasks.clamp_max_children(999), subtasks.MAX_CHILDREN_CAP)
        self.assertEqual(subtasks.clamp_max_children("x"), subtasks.DEFAULT_MAX_CHILDREN)


if __name__ == "__main__":
    unittest.main()
