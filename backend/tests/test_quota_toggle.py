import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend.app import quota, routing
from backend.app.routers import settings


class QuotaToggleTest(unittest.TestCase):
    def test_legacy_settings_cannot_enable_quota_protection(self):
        for value in (None, "1", "0"):
            with patch.object(quota.db, "get_setting", return_value=value):
                self.assertFalse(quota.is_enabled())

    def test_high_usage_does_not_block_execution(self):
        with patch.object(quota, "_provider_usage", return_value={
            "available": True, "five_hour_pct": 100, "weekly_pct": 100,
        }):
            self.assertTrue(quota.check_engine("claude")[0])
            self.assertTrue(quota.check_engine("codex")[0])

    def test_routing_keeps_high_usage_engine_eligible(self):
        with (
            patch.object(routing.engine_policy, "rejection", return_value=None),
            patch.object(routing.engine_updates, "is_installed", side_effect=lambda e: e in {"claude", "codex"}),
            patch.object(routing.quota, "get_usage", return_value={
                "claude": {"available": True, "weekly_pct": 95},
                "codex": {"available": True, "weekly_pct": 80},
            }),
            patch.object(routing, "_success_rate", side_effect=lambda e: 1 if e == "claude" else 0),
        ):
            self.assertEqual(routing.pick_engine()[0], "claude")

    def test_disabled_quota_protection_bypasses_usage_check(self):
        with (
            patch.object(quota, "is_enabled", return_value=False),
            patch.object(quota, "_provider_usage") as provider_usage,
        ):
            self.assertEqual(quota.check_engine("claude"), (True, None, "额度保护已关闭，放行"))

        provider_usage.assert_not_called()

    def test_settings_persist_quota_toggle(self):
        stored = {}

        def get_setting(key, default=None):
            return stored.get(key, default)

        with (
            patch.object(settings.db, "get_setting", side_effect=get_setting),
            patch.object(settings.db, "set_setting", side_effect=lambda key, value: stored.__setitem__(key, value)),
            patch.object(settings.scheduler, "tick"),
            patch.object(settings.agent_skills, "sync_installed_engines") as sync_skills,
            patch.object(settings.agent_skills.engine_updates, "installed_engines", return_value={}),
            patch.object(settings.browser_computer, "availability", return_value={}),
        ):
            result = settings.update_settings(settings.Settings(
                max_concurrent=3,
                quota_enabled=True,
                subtask_skill_enabled=False,
                report_skill_enabled=True,
                skill_path_overrides={"codex": "/tmp/bosun-codex-skills"},
            ))

        self.assertEqual(stored["quota_enabled"], "0")
        self.assertEqual(stored["subtask_skill_enabled"], "0")
        self.assertEqual(stored["report_skill_enabled"], "1")
        self.assertEqual(stored["agent_skill_path:codex"], str(Path("/tmp/bosun-codex-skills").resolve()))
        self.assertFalse(result["quota_enabled"])
        sync_skills.assert_called_once_with()

    def test_skill_path_override_rejects_home_directory(self):
        with self.assertRaises(HTTPException) as raised:
            settings.normalize_skill_path("~")

        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
