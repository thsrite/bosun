import unittest
from unittest.mock import patch

from backend.app.routers import settings


class StatusBadgeToggleTest(unittest.TestCase):
    def test_status_badge_is_enabled_by_default(self):
        with patch.object(settings.db, "get_setting", return_value=None):
            self.assertTrue(settings.status_badge_enabled())

    def test_status_badge_disabled_value(self):
        with patch.object(settings.db, "get_setting", return_value="0"):
            self.assertFalse(settings.status_badge_enabled())

    def test_settings_persist_status_badge_toggle(self):
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
                status_badge_enabled=False,
            ))

        self.assertEqual(stored["status_badge_enabled"], "0")
        self.assertFalse(result["status_badge_enabled"])


if __name__ == "__main__":
    unittest.main()
