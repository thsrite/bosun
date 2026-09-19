import unittest
from unittest.mock import patch

from app import engines


class OmpInvocationTest(unittest.TestCase):
    def test_interactive_omp_invocation_disables_nested_pty_and_title_generation(self):
        with patch.object(engines, "OMP_BIN", "omp"), patch.object(
            engines.agent_skills, "ensure_for_dispatch"
        ), patch.object(engines.engine_settings, "omp_model", return_value=""), patch.object(
            engines.engine_settings, "omp_thinking", return_value=""
        ):
            argv = engines.build_argv("omp", "执行任务", auto_approve=True)

        self.assertEqual(
            argv,
            ["omp", "--no-pty", "--no-title", "--auto-approve", "执行任务"],
        )

    def test_resume_omp_invocation_keeps_optimization_flags_before_prompt(self):
        with patch.object(engines, "OMP_BIN", "omp"), patch.object(
            engines.agent_skills, "ensure_for_dispatch"
        ), patch.object(engines.engine_settings, "omp_model", return_value=""), patch.object(
            engines.engine_settings, "omp_thinking", return_value=""
        ):
            argv = engines.build_resume_argv(
                "omp", "session-123", "继续处理", auto_approve=False
            )

        self.assertEqual(
            argv,
            ["omp", "--no-pty", "--no-title", "--resume", "session-123", "继续处理"],
        )

    def test_headless_omp_invocation_uses_the_same_runtime_optimizations(self):
        with patch.object(engines, "OMP_BIN", "omp"), patch.object(
            engines.engine_settings, "omp_model", return_value=""
        ), patch.object(engines.engine_settings, "omp_thinking", return_value=""):
            argv = engines.build_headless_argv("omp", "检查结果", auto_approve=True)

        self.assertEqual(
            argv,
            ["omp", "--no-pty", "--no-title", "-p", "--auto-approve", "检查结果"],
        )


if __name__ == "__main__":
    unittest.main()
