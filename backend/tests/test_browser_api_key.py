"""Browser Computer Use 的 OpenAI Key 可在设置页配置（环境变量退化为回退项）。"""
import unittest
from unittest.mock import patch

from app import browser_computer
from app.routers import settings


class ResolveApiKeyTest(unittest.TestCase):
    def test_settings_key_wins_over_env(self):
        with (
            patch.object(browser_computer.db, "get_setting", return_value="sk-from-settings"),
            patch.dict("os.environ", {"BOSUN_OPENAI_API_KEY": "sk-from-env"}, clear=False),
        ):
            self.assertEqual(
                browser_computer.resolve_api_key(),
                ("sk-from-settings", "settings"),
            )

    def test_falls_back_to_env_when_settings_empty(self):
        with (
            patch.object(browser_computer.db, "get_setting", return_value=""),
            patch.dict(
                "os.environ",
                {"BOSUN_OPENAI_API_KEY": "", "OPENAI_API_KEY": "sk-plain-env"},
                clear=False,
            ),
        ):
            self.assertEqual(browser_computer.resolve_api_key(), ("sk-plain-env", "env"))

    def test_missing_everywhere(self):
        with (
            patch.object(browser_computer.db, "get_setting", return_value=None),
            patch.dict(
                "os.environ",
                {"BOSUN_OPENAI_API_KEY": "", "OPENAI_API_KEY": ""},
                clear=False,
            ),
        ):
            self.assertEqual(browser_computer.resolve_api_key(), ("", ""))

    def test_mask_never_leaks_full_key(self):
        masked = browser_computer.mask_api_key("sk-proj-1234567890abcd")
        self.assertNotIn("1234567890", masked)
        self.assertTrue(masked.endswith("abcd"))
        self.assertEqual(browser_computer.mask_api_key(""), "")


class BrowserApiKeySettingTest(unittest.TestCase):
    def _run_update(self, stored: dict, **body_kwargs):
        def get_setting(key, default=None):
            return stored.get(key, default)

        with (
            patch.object(settings.db, "get_setting", side_effect=get_setting),
            patch.object(settings.db, "set_setting", side_effect=stored.__setitem__),
            patch.object(settings.scheduler, "tick"),
            patch.object(settings.agent_skills, "sync_installed_engines"),
            patch.object(settings.agent_skills.engine_updates, "installed_engines", return_value={}),
            patch.object(settings.browser_computer, "availability", return_value={}),
            patch.object(settings.browser_computer, "invalidate_availability") as invalidate,
        ):
            settings.update_settings(settings.Settings(max_concurrent=3, **body_kwargs))
        return invalidate

    def test_saves_key_and_drops_availability_cache(self):
        stored: dict = {}
        invalidate = self._run_update(stored, browser_api_key="  sk-typed-in-ui  ")
        self.assertEqual(stored["browser_openai_api_key"], "sk-typed-in-ui")
        invalidate.assert_called_once()

    def test_omitting_field_keeps_existing_key(self):
        stored = {"browser_openai_api_key": "sk-kept"}
        self._run_update(stored)
        self.assertEqual(stored["browser_openai_api_key"], "sk-kept")

    def test_empty_string_clears_key(self):
        stored = {"browser_openai_api_key": "sk-old"}
        self._run_update(stored, browser_api_key="")
        self.assertEqual(stored["browser_openai_api_key"], "")


if __name__ == "__main__":
    unittest.main()
