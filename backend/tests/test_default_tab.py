import unittest
from unittest.mock import patch

from backend.app.routers import settings


class DefaultTabTest(unittest.TestCase):
    def test_default_tab_falls_back_to_projects(self):
        with patch.object(settings.db, "get_setting", side_effect=lambda key, default=None: default):
            self.assertEqual(settings.default_tab(), "projects")

    def test_unknown_tab_falls_back_to_projects(self):
        with patch.object(settings.db, "get_setting", return_value="dashboard"):
            self.assertEqual(settings.default_tab(), "projects")

    def test_stored_tab_is_returned(self):
        with patch.object(settings.db, "get_setting", return_value="tasks"):
            self.assertEqual(settings.default_tab(), "tasks")

    def test_settings_persist_default_tab(self):
        stored = {}

        def get_setting(key, default=None):
            return stored.get(key, default)

        with (
            patch.object(settings.db, "get_setting", side_effect=get_setting),
            patch.object(settings.db, "set_setting", side_effect=lambda key, value: stored.__setitem__(key, value)),
            patch.object(settings.scheduler, "tick"),
            patch.object(settings.agent_skills, "sync_installed_engines"),
            patch.object(settings.agent_skills.engine_updates, "installed_engines", return_value={}),
            patch.object(settings.browser_computer, "availability", return_value={}),
        ):
            result = settings.update_settings(settings.Settings(
                max_concurrent=3,
                default_tab="tasks",
            ))

        self.assertEqual(stored["default_tab"], "tasks")
        self.assertEqual(result["default_tab"], "tasks")


if __name__ == "__main__":
    unittest.main()
