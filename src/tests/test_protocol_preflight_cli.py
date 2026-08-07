import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from run import backtest
from run import pre_backtest_test
from llm.inference import LLMConfig, _build_provider_kwargs, llm_audit_metadata
from llm.provider import Provider
from tools.agent_tools.control.pg_preflight import run_preflight_checks


class ProtocolPreflightCliTest(unittest.TestCase):
    def _run_backtest_trace(
        self,
        *,
        trading_days=None,
        daily_return_code=0,
        script_return_codes=None,
    ):
        trace = []
        daily_calls = []
        args = SimpleNamespace(
            config=str(SRC_ROOT / "config" / "dev.yaml"),
            start_date="2025-01-02",
            end_date="2025-01-02",
            local_db=True,
            reset_config=False,
            run_eval=False,
            skip_eval=False,
            plot=True,
            plot_no_price=False,
            plot_output_dir=None,
        )

        def run_command(command, _env):
            script_name = Path(command[1]).name
            trace.append(script_name)
            return (script_return_codes or {}).get(script_name, 0)

        def daily(config_arg, start_date, end_date, local_db):
            trace.append("backtest_daily_test")
            daily_calls.append((config_arg, start_date, end_date, local_db))
            return daily_return_code

        with patch.object(backtest, "parse_args", return_value=args), patch.object(
            backtest,
            "load_yaml_config",
            return_value={"market_type": "china_futures", "exp_name": "pg", "tickers": ["BU"]},
        ), patch.object(
            backtest,
            "run_pre_backtest_test",
            side_effect=lambda *_args: trace.append("pre_backtest_test") or 0,
            create=True,
        ), patch.object(
            backtest,
            "reset_existing_config_if_requested",
            side_effect=lambda *_args: trace.append("reset") or None,
        ), patch.object(
            backtest,
            "resolve_trading_days",
            return_value=trading_days or ["2025-01-02"],
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

    def test_preflight_accepts_provider_default_url_and_api_key_env(self):
        cases = (
            (
                "CodexOpenAI",
                "switchable-gpt",
                "CODEX_OPENAI_API_KEY",
                {"codex_openai": {"reasoning_effort": "medium"}},
            ),
            (
                "TQXAI",
                "switchable-claude",
                "TQX_LLM_API_KEY",
                {},
            ),
            (
                "DeepSeek",
                "deepseek-v4-pro",
                "DEEPSEEK_API_KEY",
                {
                    "deepseek": {
                        "thinking": {"enabled": True},
                        "reasoning_effort": "medium",
                    }
                },
            ),
        )
        for provider, model, env_name, provider_overrides in cases:
            with self.subTest(provider=provider), patch.dict(
                os.environ,
                {env_name: "present"},
                clear=False,
            ), patch(
                "llm.inference.get_model",
                side_effect=AssertionError("preflight must not instantiate an LLM"),
            ) as get_model:
                result = run_preflight_checks(
                    repo_root=PROJECT_ROOT,
                    llm_config={
                        "provider": provider,
                        "model": model,
                        **provider_overrides,
                    },
                )
            self.assertTrue(result.passed, result.to_dict())
            get_model.assert_not_called()

    def test_deepseek_v4_pro_medium_thinking_route_is_canonical(self):
        raw_config = {
            "provider": "DeepSeek",
            "model": "deepseek-v4-pro",
            "temperature": None,
            "structured_output_method": "json_mode",
            "deepseek": {
                "thinking": {"enabled": True},
                "reasoning_effort": "medium",
            },
        }
        config = LLMConfig(**raw_config)

        self.assertEqual(
            _build_provider_kwargs(Provider.DEEPSEEK, config),
            {
                "extra_body": {"thinking": {"type": "enabled"}},
                "reasoning_effort": "medium",
            },
        )
        self.assertEqual(
            llm_audit_metadata(raw_config),
            {
                "provider": "DeepSeek",
                "model": "deepseek-v4-pro",
                "base_url": "https://api.deepseek.com",
                "api_key_env": "DEEPSEEK_API_KEY",
                "reasoning_effort": "medium",
            },
        )

    def test_dev_config_activates_gpt_5_6_sol_medium_reasoning(self):
        with (SRC_ROOT / "config" / "dev.yaml").open("r", encoding="utf-8") as fh:
            llm_config = yaml.safe_load(fh)["llm"]

        self.assertEqual(llm_config["provider"], "CodexOpenAI")
        self.assertEqual(llm_config["model"], "gpt-5.6-sol")
        self.assertIsNone(llm_config["temperature"])
        self.assertEqual(llm_config["structured_output_method"], "json_mode")
        self.assertEqual(llm_config["codex_openai"]["reasoning_effort"], "medium")

    def test_pre_backtest_runner_has_no_llm_auth_option(self):
        argv = ["pre_backtest_test.py", "--config", "config/dev.yaml", "--local-db"]
        with patch.object(sys, "argv", argv):
            args = pre_backtest_test.parse_args()
        self.assertFalse(hasattr(args, "check_llm_auth"))

    def test_pre_backtest_runner_accepts_window_independently(self):
        captured = {}
        report = SimpleNamespace(
            to_dict=lambda: {
                "status": "passed",
                "checks": [],
            }
        )

        class FakeGovernor:
            def run_pre_backtest_acceptance(self, **kwargs):
                captured.update(kwargs)
                return report

        args = SimpleNamespace(
            config="config/dev.yaml",
            local_db=True,
            start_date="2025-03-24",
            end_date="2025-03-31",
            deepfund_python=str(sys.executable),
            json=False,
        )
        with patch.object(pre_backtest_test, "parse_args", return_value=args), patch.object(
            pre_backtest_test,
            "ProtocolGovernor",
            return_value=FakeGovernor(),
        ):
            return_code = pre_backtest_test.main()

        self.assertEqual(return_code, 0)
        self.assertEqual(captured["start_date"], "2025-03-24")
        self.assertEqual(captured["end_date"], "2025-03-31")

    def test_backtest_does_not_embed_precheck_evaluation_or_plotting(self):
        return_code, trace, _daily_calls = self._run_backtest_trace()
        self.assertEqual(return_code, 0)
        self.assertEqual(
            trace,
            [
                "reset",
                "proposal.py",
                "order.py",
                "settlement.py",
                "validate_phase_flow.py",
                "researcher_learning.py",
                "backtest_daily_test",
            ],
        )

    def test_backtest_cli_has_no_precheck_evaluation_or_plot_options(self):
        argv = [
            "backtest.py",
            "--config",
            "config/dev.yaml",
            "--start-date",
            "2025-03-24",
            "--end-date",
            "2025-03-31",
            "--local-db",
        ]
        with patch.object(sys, "argv", argv):
            args = backtest.parse_args()
        for name in ("run_eval", "skip_eval", "plot", "plot_no_price", "plot_output_dir"):
            self.assertFalse(hasattr(args, name), name)

    def test_backtest_exposes_no_pre_backtest_runner(self):
        self.assertFalse(hasattr(backtest, "run_pre_backtest_test"))

    def test_backtest_daily_gate_is_single_day_and_has_no_final_duplicate(self):
        return_code, trace, daily_calls = self._run_backtest_trace()
        self.assertEqual(return_code, 0)
        self.assertEqual(trace.count("backtest_daily_test"), 1)
        self.assertEqual(daily_calls[0][1:3], ("2025-01-02", "2025-01-02"))

    def test_daily_gate_failure_stops_before_following_trading_day(self):
        return_code, trace, daily_calls = self._run_backtest_trace(
            trading_days=["2025-01-02", "2025-01-03"],
            daily_return_code=9,
        )
        self.assertEqual(return_code, 9)
        self.assertEqual(len(daily_calls), 1)
        self.assertNotIn("2025-01-03", str(daily_calls))
        self.assertEqual(trace.count("proposal.py"), 1)

    def test_each_production_stage_failure_stops_before_daily_gate_and_next_day(self):
        scripts = (
            "proposal.py",
            "order.py",
            "settlement.py",
            "validate_phase_flow.py",
            "researcher_learning.py",
        )
        for script_name in scripts:
            with self.subTest(script_name=script_name):
                return_code, trace, daily_calls = self._run_backtest_trace(
                    trading_days=["2025-01-02", "2025-01-03"],
                    script_return_codes={script_name: 7},
                )
                self.assertEqual(return_code, 7)
                self.assertEqual(trace.count(script_name), 1)
                self.assertNotIn("backtest_daily_test", trace)
                self.assertEqual(daily_calls, [])

    def test_daily_command_keeps_fixed_main_runner_path(self):
        commands = []
        with patch.object(backtest, "run_command", side_effect=lambda command, _env: commands.append(command) or 0):
            backtest.run_backtest_daily_test("config.yaml", "2025-01-02", "2025-01-02", True)
        self.assertTrue(str(commands[0][1]).endswith("backtest_daily_test.py"))


if __name__ == "__main__":
    unittest.main()
