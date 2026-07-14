import inspect
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from run import backtest
from run import pre_backtest_test
from tools.agent_tools.control.pg_preflight import run_preflight_checks


class ProtocolPreflightCliTest(unittest.TestCase):
    def test_preflight_checks_llm_configuration_without_calling_llm(self):
        llm_config = {
            "provider": "CodexOpenAI",
            "model": "switchable-model",
            "codex_openai": {
                "base_url": "http://example.invalid",
                "api_key_env": "PG_TEST_KEY",
            },
        }
        with patch.dict(os.environ, {"PG_TEST_KEY": "present"}, clear=False):
            result = run_preflight_checks(repo_root=PROJECT_ROOT, llm_config=llm_config)
        self.assertTrue(result.passed, result.to_dict())
        source = inspect.getsource(run_preflight_checks)
        self.assertNotIn("get_model", source)
        self.assertNotIn("invoke", source)

    def test_pre_backtest_runner_has_no_llm_auth_option(self):
        source = inspect.getsource(pre_backtest_test.parse_args)
        self.assertNotIn("check-llm-auth", source)

    def test_backtest_calls_precheck_before_reset(self):
        source = inspect.getsource(backtest.main)
        self.assertLess(
            source.index("run_pre_backtest_test("),
            source.index("reset_existing_config_if_requested("),
        )

    def test_backtest_daily_gate_is_single_day_and_has_no_final_duplicate(self):
        source = inspect.getsource(backtest.main)
        self.assertIn("config_arg,\n            trading_day,\n            trading_day", source)
        self.assertEqual(source.count("run_backtest_daily_test("), 1)

    def test_integrated_commands_keep_fixed_main_runner_paths(self):
        commands = []
        with patch.object(backtest, "run_command", side_effect=lambda command, _env: commands.append(command) or 0):
            backtest.run_pre_backtest_test("config.yaml", "2025-01-02", "2025-01-03", True)
            backtest.run_backtest_daily_test("config.yaml", "2025-01-02", "2025-01-02", True)
        self.assertTrue(str(commands[0][1]).endswith("pre_backtest_test.py"))
        self.assertTrue(str(commands[1][1]).endswith("backtest_daily_test.py"))


if __name__ == "__main__":
    unittest.main()
