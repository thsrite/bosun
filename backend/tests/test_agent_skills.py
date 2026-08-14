import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import agent_skills, engines, env


class AgentSkillPathTest(unittest.TestCase):
    def test_engine_skill_dirs_honor_cli_home_overrides(self):
        with (
            patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": "/tmp/claude-profile",
                "CODEX_HOME": "/tmp/codex-profile",
                "PI_CODING_AGENT_DIR": "/tmp/omp-profile",
                "KIMI_CODE_HOME": "/tmp/kimi-profile",
            }, clear=False),
            patch.object(agent_skills.db, "get_setting", return_value=""),
        ):
            self.assertEqual(agent_skills.skills_dir("claude"), Path("/tmp/claude-profile/skills"))
            self.assertEqual(agent_skills.skills_dir("codex"), Path("/tmp/codex-profile/skills"))
            self.assertEqual(agent_skills.skills_dir("omp"), Path("/tmp/omp-profile/skills"))
            self.assertEqual(agent_skills.skills_dir("kimi"), Path("/tmp/kimi-profile/skills"))

    def test_omp_skill_dir_honors_config_root_when_agent_dir_is_unset(self):
        with (
            patch.dict(os.environ, {"PI_CONFIG_DIR": ".omp-work"}, clear=False),
            patch.object(agent_skills.db, "get_setting", return_value=""),
        ):
            os.environ.pop("PI_CODING_AGENT_DIR", None)
            self.assertEqual(
                agent_skills.skills_dir("omp"), Path.home() / ".omp-work" / "agent" / "skills"
            )

    def test_user_override_wins_over_cli_environment(self):
        with (
            patch.dict(os.environ, {"CODEX_HOME": "/tmp/codex-profile"}, clear=False),
            patch.object(agent_skills.db, "get_setting", return_value="/tmp/custom-skills"),
        ):
            self.assertEqual(agent_skills.skills_dir("codex"), Path("/tmp/custom-skills"))


class ManagedSkillInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source" / "bosun-report"
        self.source.mkdir(parents=True)
        (self.source / "SKILL.md").write_text("v1", encoding="utf-8")
        (self.source / agent_skills.OWNER_FILE).write_text(
            json.dumps({"owner": agent_skills.OWNER, "version": 1}), encoding="utf-8"
        )
        self.skills = self.root / "target" / "skills"

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_is_idempotent_and_updates_owned_copy(self):
        first = agent_skills.install_managed_skill(self.source, self.skills)
        self.assertTrue(first.ready)
        self.assertEqual((self.skills / "bosun-report" / "SKILL.md").read_text(), "v1")

        (self.source / "SKILL.md").write_text("v2", encoding="utf-8")
        second = agent_skills.install_managed_skill(self.source, self.skills)
        self.assertTrue(second.ready)
        self.assertEqual((self.skills / "bosun-report" / "SKILL.md").read_text(), "v2")

    def test_install_never_overwrites_unmanaged_same_name_skill(self):
        destination = self.skills / "bosun-report"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("user content", encoding="utf-8")

        result = agent_skills.install_managed_skill(self.source, self.skills)

        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "name_conflict")
        self.assertEqual((destination / "SKILL.md").read_text(), "user content")

    def test_uninstall_only_removes_owned_skill(self):
        owned = agent_skills.install_managed_skill(self.source, self.skills)
        self.assertTrue(owned.ready)
        self.assertTrue(agent_skills.uninstall_managed_skill(self.skills / "bosun-report"))
        self.assertFalse((self.skills / "bosun-report").exists())

        unmanaged = self.skills / "bosun-report"
        unmanaged.mkdir(parents=True)
        (unmanaged / "SKILL.md").write_text("user content", encoding="utf-8")
        self.assertFalse(agent_skills.uninstall_managed_skill(unmanaged))
        self.assertTrue(unmanaged.exists())

    def test_path_migration_keeps_old_copy_when_new_path_conflicts(self):
        old_root = self.root / "old"
        new_root = self.root / "new"
        self.assertTrue(agent_skills.install_managed_skill(self.source, old_root).ready)
        conflict = new_root / "bosun-report"
        conflict.mkdir(parents=True)
        (conflict / "SKILL.md").write_text("user content", encoding="utf-8")

        with (
            patch.object(agent_skills, "feature_enabled", return_value=True),
            patch.object(agent_skills, "skills_dir", return_value=new_root),
            patch.object(agent_skills.config, "RESOURCE_ROOT", self.root),
        ):
            resources = self.root / "bosun_skills"
            resources.mkdir()
            self.source.rename(resources / "bosun-report")
            agent_skills.migrate_managed_skills("codex", old_root, new_root)

        self.assertTrue((old_root / "bosun-report").exists())
        self.assertEqual((conflict / "SKILL.md").read_text(), "user content")

    def test_path_migration_moves_owned_skill_after_successful_install(self):
        old_root = self.root / "old"
        new_root = self.root / "new"
        self.assertTrue(agent_skills.install_managed_skill(self.source, old_root).ready)
        resources = self.root / "bosun_skills"
        resources.mkdir()
        self.source.rename(resources / "bosun-report")

        with (
            patch.object(agent_skills, "feature_enabled", return_value=True),
            patch.object(agent_skills, "skills_dir", return_value=new_root),
            patch.object(agent_skills.config, "RESOURCE_ROOT", self.root),
        ):
            agent_skills.migrate_managed_skills("codex", old_root, new_root)

        self.assertFalse((old_root / "bosun-report").exists())
        self.assertTrue((new_root / "bosun-report" / agent_skills.OWNER_FILE).exists())

    def test_skills_are_opt_in_by_default(self):
        with patch.object(agent_skills.db, "get_setting", return_value=None):
            self.assertFalse(agent_skills.feature_enabled("subtask"))
            self.assertFalse(agent_skills.feature_enabled("report"))


class DispatchSkillSyncTest(unittest.TestCase):
    def test_disabled_feature_removes_only_its_owned_skill(self):
        with (
            patch.object(agent_skills, "feature_enabled", side_effect=lambda name: name == "report"),
            patch.object(agent_skills, "install_feature") as install_feature,
            patch.object(agent_skills, "uninstall_feature") as uninstall_feature,
        ):
            result = agent_skills.ensure_for_dispatch("codex")

        install_feature.assert_called_once_with("codex", "report")
        uninstall_feature.assert_called_once_with("codex", "subtask")
        self.assertTrue(result["report"].ready)

    def test_startup_sync_installs_only_engines_detected_later(self):
        with (
            patch.object(agent_skills, "feature_enabled", return_value=True),
            patch.object(agent_skills.engine_updates, "installed_engines", return_value={
                "claude": False, "codex": False, "omp": False, "kimi": True, "browser": False,
            }),
            patch.object(agent_skills, "ensure_for_dispatch", return_value={}) as ensure,
        ):
            agent_skills.sync_installed_engines()

        ensure.assert_called_once_with("kimi")

    def test_sync_removes_disabled_skills_even_when_cli_is_not_installed(self):
        with (
            patch.object(agent_skills, "feature_enabled", return_value=False),
            patch.object(agent_skills, "uninstall_feature") as uninstall,
            patch.object(agent_skills.engine_updates, "installed_engines", return_value={}),
            patch.object(agent_skills, "ensure_for_dispatch") as ensure,
        ):
            agent_skills.sync_installed_engines()

        self.assertEqual(uninstall.call_count, len(agent_skills.ENGINES) * len(agent_skills.FEATURE_SKILLS))
        ensure.assert_not_called()

    def test_dispatch_prompt_is_never_modified_when_skills_are_enabled(self):
        with patch.object(agent_skills, "ensure_for_dispatch", return_value={
            "subtask": agent_skills.InstallResult(True),
            "report": agent_skills.InstallResult(True),
        }) as ensure:
            prompt = engines.with_report_directive("用户原始要求", engine="codex")

        self.assertEqual(prompt, "用户原始要求")
        ensure.assert_called_once_with("codex")

    def test_report_nudge_uses_skill_when_ready_and_curl_only_as_fallback(self):
        with patch.object(agent_skills, "ensure_for_dispatch", return_value={
            "report": agent_skills.InstallResult(True),
        }):
            ready = agent_skills.report_nudge("claude")
        self.assertIn("bosun-report", ready)
        self.assertNotIn("curl", ready)

        with patch.object(agent_skills, "ensure_for_dispatch", return_value={
            "report": agent_skills.InstallResult(False, "name_conflict"),
        }):
            fallback = agent_skills.report_nudge("claude")
        self.assertIn("curl", fallback)

    def test_task_environment_exposes_available_engines_and_artifact_mode(self):
        with (
            patch("app.engine_updates.installed_engines", return_value={
                "claude": True, "codex": True, "omp": False, "kimi": True, "browser": False,
            }),
            patch("app.auth.issue_task_token", return_value="token"),
        ):
            values = env.task_env(7, "codex", artifact_required=True)

        self.assertEqual(values["BOSUN_AVAILABLE_ENGINES"], "claude,kimi")
        self.assertEqual(values["BOSUN_ARTIFACT_REQUIRED"], "1")


if __name__ == "__main__":
    unittest.main()
