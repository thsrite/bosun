import unittest
from unittest.mock import patch

from backend.app import quota
from backend.app.routers import settings


class QuotaToggleTest(unittest.TestCase):
    def test_quota_protection_is_enabled_by_default(self):
        with patch.object(quota.db, "get_setting", return_value=None):
            self.assertTrue(quota.is_enabled())

    def test_disabled_quota_protection_bypasses_usage_check(self):
        with (
            patch.object(quota, "is_enabled", return_value=False),
            patch.object(quota, "_provider_usage") as provider_usage,
        ):
            self.assertEqual(quota.check_engine("cc"), (True, None, "额度保护已关闭，放行"))

        provider_usage.assert_not_called()

    def test_settings_persist_quota_toggle(self):
        stored = {}

        def get_setting(key, default=None):
            return stored.get(key, default)

        with (
            patch.object(settings.db, "get_setting", side_effect=get_setting),
            patch.object(settings.db, "set_setting", side_effect=lambda key, value: stored.__setitem__(key, value)),
            patch.object(settings.scheduler, "tick"),
            patch.object(settings.browser_computer, "availability", return_value={}),
        ):
            result = settings.update_settings(settings.Settings(max_concurrent=3, quota_enabled=False))

        self.assertEqual(stored["quota_enabled"], "0")
        self.assertFalse(result["quota_enabled"])


if __name__ == "__main__":
    unittest.main()
