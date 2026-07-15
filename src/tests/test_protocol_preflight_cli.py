import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from run import backtest
from run import pre_backtest_test
from tools.agent_tools.control.pg_preflight import run_preflight_checks


class ProtocolPreflightCliTest(unittest.TestCase):
    def _run_backtest_trace(self):
        trace = []
        daily_calls = []
        args = SimpleNamespace(
            config=str(SRC_ROOT / "config" / "dev.yaml"),
            start_date="2025-01-02",
            end_date="2025-01-02",
            local_db=True,
            reset_config=False,
            run_eval=False,
            skip_eval=True,
            plot=False,
            plot_no_price=False,
            plot_output_dir=None,
        )

        def run_command(command, _env):
            trace.append(Path(command[1]).name)
            return 0

        def daily(config_arg, start_date, end_date, local_db):
            trace.append("backtest_daily_test")
            daily_calls.append((config_arg, start_date, end_date, local_db))
            return 0

        with patch.object(backtest, "parse_args", return_value=args), patch.object(
            backtest,
            "load_yaml_config",
            return_value={"market_type": "china_futures", "exp_name": "pg", "tickers": ["BU"]},
        ), patch.object(
            backtest,
            "run_pre_backtest_test",
            side_effect=lambda *_args: trace.append("pre_backtest_test") or 0,
        ), patch.object(
            backtest,
            "reset_existing_config_if_requested",
            side_effect=lambda *_args: trace.append("reset") or None,
        ), patch.object(
            backtest,
            "resolve_trading_days",
            return_value=["2025-01-02"],
        ), patch.object(
            backtest,
            "get_phase_record",
            return_value=None,
        ), patch.object(
            backtest,
            "run_command",
            side_effect=run_command,
        ), patch.object(
            backtest,
            "run_backtest_daily_test",
            side_effect=daily,
        ):
            return_code = backtest.main()
        return return_code, trace, daily_calls

    def test_preflight_checks_llm_configuration_without_calling_llm(self):
        llm_config = {
            "provider": "CodexOpenAI",
            "model": "switchable-model",
            "codex_openai": {
                "base_url": "http://example.invalid",
                "api_key_env": "PG_TEST_KEY",
            },
        }
        with patch.dict(os.environ, {"PG_TEST_KEY": "present"}, clear=False), patch(
            "llm.inference.get_model",
            side_effect=AssertionError("preflight must not instantiate an LLM"),
        ) as get_model:
            result = run_preflight_checks(repo_root=PROJECT_ROOT, llm_config=llm_config)
        self.assertTrue(result.passed, result.to_dict())
        get_model.assert_not_called()

    def test_pre_backtest_runner_has_no_llm_auth_option(self):
        argv = ["pre_backtest_test.py", "--config", "config/dev.yaml", "--local-db"]
        with patch.object(sys, "argv", argv):
            args = pre_backtest_test.parse_args()
        self.assertFalse(hasattr(args, "check_llm_auth"))

    def test_backtest_calls_precheck_before_reset(self):
        return_code, trace, _daily_calls = self._run_backtest_trace()
        self.assertEqual(return_code, 0)
        self.assertLess(trace.index("pre_backtest_test"), trace.index("reset"))

    def test_backtest_daily_gate_is_single_day_and_has_no_final_duplicate(self):
        return_code, trace, daily_calls = self._run_backtest_trace()
        self.assertEqual(return_code, 0)
        self.assertEqual(trace.count("backtest_daily_test"), 1)
        self.assertEqual(daily_calls[0][1:3], ("2025-01-02", "2025-01-02"))

    def test_integrated_commands_keep_fixed_main_runner_paths(self):
        commands = []
        with patch.object(backtest, "run_command", side_effect=lambda command, _env: commands.append(command) or 0):
            backtest.run_pre_backtest_test("config.yaml", "2025-01-02", "2025-01-03", True)
            backtest.run_backtest_daily_test("config.yaml", "2025-01-02", "2025-01-02", True)
        self.assertTrue(str(commands[0][1]).endswith("pre_backtest_test.py"))
        self.assertTrue(str(commands[1][1]).endswith("backtest_daily_test.py"))


if __name__ == "__main__":
    unittest.main()
