import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from llm.inference import llm_audit_metadata
from llm.provider import Provider
from run.pre_backtest_test import _load_config, _run_protocol_preflight
from tools.agent_tools.control.preflight import run_llm_preflight_check


class ProtocolPreflightCliRegressionTest(unittest.TestCase):
    def _load_dev_llm_config(self):
        cfg = yaml.safe_load((SRC_ROOT / "config" / "dev.yaml").read_text(encoding="utf-8"))
        return cfg["llm"]

    def test_dev_yaml_llm_route_is_codex_only_with_tqxai_commented_backup(self):
        llm_config = self._load_dev_llm_config()
        metadata = llm_audit_metadata(llm_config)
        raw_config = (SRC_ROOT / "config" / "dev.yaml").read_text(encoding="utf-8")

        self.assertEqual(llm_config.get("provider"), "CodexOpenAI")
        self.assertEqual(llm_config.get("model"), "gpt-5.5")
        self.assertNotIn("deepseek", llm_config)
        self.assertNotIn("tqxai", llm_config)
        self.assertEqual(metadata.get("base_url"), "http://47.74.0.65/v1")
        self.assertEqual(metadata.get("reasoning_effort"), "medium")
        self.assertIn('# provider: "TQXAI"', raw_config)
        self.assertIn("# tqxai:", raw_config)

    def test_deepseek_provider_remains_available_for_non_runtime_configs(self):
        self.assertEqual(Provider.DEEPSEEK.value, "DeepSeek")
        llm_config = {
            "provider": "DeepSeek",
            "model": "deepseek-chat",
            "deepseek": {"thinking": {"enabled": False}, "reasoning_effort": None},
        }
        metadata = llm_audit_metadata(llm_config)

        self.assertEqual(metadata.get("provider"), "DeepSeek")
        self.assertEqual(metadata.get("base_url"), "https://api.deepseek.com")
        self.assertEqual(metadata.get("api_key_env"), "DEEPSEEK_API_KEY")

    def test_llm_preflight_rejects_runtime_deepseek_block_when_codex_is_selected(self):
        llm_config = {
            "provider": "CodexOpenAI",
            "model": "gpt-5.5",
            "codex_openai": {
                "base_url": "http://47.74.0.65",
                "reasoning_effort": "medium",
                "api_key_env": "CODEX_OPENAI_API_KEY",
                "api_key_env_fallbacks": [],
            },
            "deepseek": {"thinking": {"enabled": False}, "reasoning_effort": None},
        }
        with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
            result = run_llm_preflight_check(llm_config, check_auth=False)

        self.assertFalse(result.ok)
        self.assertIn("llm_runtime_provider_block_not_allowed:deepseek", result.errors)

    def test_llm_preflight_rejects_active_tqxai_block_when_codex_is_selected(self):
        llm_config = {
            "provider": "CodexOpenAI",
            "model": "gpt-5.5",
            "codex_openai": {
                "base_url": "http://47.74.0.65",
                "reasoning_effort": "medium",
                "api_key_env": "CODEX_OPENAI_API_KEY",
                "api_key_env_fallbacks": [],
            },
            "tqxai": {
                "base_url": "https://llm.tqx.ai",
                "reasoning_effort": None,
                "api_key_env": "TQX_LLM_API_KEY",
                "api_key_env_fallbacks": [],
            },
        }
        with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
            result = run_llm_preflight_check(llm_config, check_auth=False)

        self.assertFalse(result.ok)
        self.assertIn("llm_provider_block_mismatch:tqxai:selected=CodexOpenAI", result.errors)

    def test_llm_preflight_rejects_old_codex_gateway(self):
        llm_config = {
            "provider": "CodexOpenAI",
            "model": "gpt-5.5",
            "codex_openai": {
                "base_url": "http://47.245.121.52",
                "reasoning_effort": "medium",
                "api_key_env": "CODEX_OPENAI_API_KEY",
                "api_key_env_fallbacks": [],
            },
        }
        with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
            result = run_llm_preflight_check(llm_config, check_auth=False)

        self.assertFalse(result.ok)
        self.assertIn("llm_codex_gateway_mismatch:http://47.245.121.52/v1", result.errors)

    def test_pre_backtest_gate_protocol_preflight_runs_without_real_api_call(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            fake_python = tmp / "deepfund" / "python.exe"
            fake_python.parent.mkdir()
            fake_python.write_text("", encoding="utf-8")
            config_path = SRC_ROOT / "config" / "dev.yaml"
            args = SimpleNamespace(
                local_db=False,
                deepfund_python=str(fake_python),
                check_llm_auth=False,
            )
            with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
                report = _run_protocol_preflight(args, config_path, _load_config(config_path))

        self.assertTrue(report["ok"], report)
        self.assertTrue(report["preflight"]["ok"], report)

    def test_pre_backtest_gate_protocol_preflight_reports_missing_deepfund(self):
        config_path = SRC_ROOT / "config" / "dev.yaml"
        args = SimpleNamespace(
            local_db=False,
            deepfund_python=str(PROJECT_ROOT / "missing_deepfund" / "python.exe"),
            check_llm_auth=False,
        )
        with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
            report = _run_protocol_preflight(args, config_path, _load_config(config_path))

        self.assertFalse(report["ok"])
        self.assertFalse(report["preflight"]["ok"])
        self.assertIn("deepfund_python_missing", "\n".join(report["errors"]))

    def test_backtest_pre_backtest_command_uses_integrated_gate(self):
        import run.backtest as backtest

        command = []

        def fake_run_command(raw_command, env):
            command.extend(raw_command)
            return 0

        with patch.object(backtest, "run_command", side_effect=fake_run_command):
            result = backtest.run_pre_backtest_test(
                str(SRC_ROOT / "config" / "dev.yaml"),
                "2025-03-01",
                "2025-03-10",
                local_db=True,
            )

        self.assertEqual(result, 0)
        self.assertIn(str(SRC_ROOT / "run" / "pre_backtest_test.py"), command)
        self.assertIn("--config", command)
        self.assertIn("--start-date", command)
        self.assertIn("2025-03-01", command)
        self.assertIn("--end-date", command)
        self.assertIn("2025-03-10", command)
        self.assertIn("--local-db", command)

    def test_backtest_daily_command_uses_integrated_gate(self):
        import run.backtest as backtest

        command = []

        def fake_run_command(raw_command, env):
            command.extend(raw_command)
            return 0

        with patch.object(backtest, "run_command", side_effect=fake_run_command):
            result = backtest.run_backtest_daily_test(
                str(SRC_ROOT / "config" / "dev.yaml"),
                "2025-03-01",
                "2025-03-10",
                local_db=True,
            )

        self.assertEqual(result, 0)
        self.assertIn(str(SRC_ROOT / "run" / "backtest_daily_test.py"), command)
        self.assertIn("--config", command)
        self.assertIn("--start-date", command)
        self.assertIn("2025-03-01", command)
        self.assertIn("--end-date", command)
        self.assertIn("2025-03-10", command)
        self.assertIn("--local-db", command)

    def test_backtest_main_stops_before_trading_loop_when_pre_backtest_gate_fails(self):
        import run.backtest as backtest

        argv = [
            "backtest.py",
            "--config",
            str(SRC_ROOT / "config" / "dev.yaml"),
            "--start-date",
            "2025-03-01",
            "--end-date",
            "2025-03-10",
            "--local-db",
        ]

        with patch.object(sys, "argv", argv), patch.object(
            backtest,
            "load_yaml_config",
            return_value={"market_type": "china_futures", "tickers": ["RB"], "exp_name": "agentquant-test"},
        ), patch.object(backtest, "run_pre_backtest_test", return_value=1) as pre_gate, patch.object(
            backtest, "resolve_trading_days"
        ) as resolve_days:
            result = backtest.main()

        self.assertEqual(result, 1)
        pre_gate.assert_called_once_with(
            str((SRC_ROOT / "config" / "dev.yaml").resolve()),
            "2025-03-01",
            "2025-03-10",
            True,
        )
        resolve_days.assert_not_called()

    def test_backtest_main_stops_on_first_daily_gate_failure(self):
        import run.backtest as backtest

        argv = [
            "backtest.py",
            "--config",
            str(SRC_ROOT / "config" / "dev.yaml"),
            "--start-date",
            "2025-03-06",
            "--end-date",
            "2025-04-30",
            "--local-db",
        ]
        trading_days = ["2025-03-27", "2025-03-28", "2025-03-29"]
        executed_scripts = []
        daily_windows = []

        def fake_phase_record(*_args, **_kwargs):
            return None

        def fake_run_command(command, env):
            script_name = Path(command[1]).name
            trading_date = None
            if "--trading-date" in command:
                trading_date = command[command.index("--trading-date") + 1]
            executed_scripts.append((script_name, trading_date))
            return 0

        def fake_daily_gate(config_arg, start_date, trading_day, local_db):
            daily_windows.append((start_date, trading_day, local_db))
            return 7 if trading_day == "2025-03-28" else 0

        with patch.object(sys, "argv", argv), patch.object(
            backtest,
            "load_yaml_config",
            return_value={"market_type": "china_futures", "tickers": ["RB"], "exp_name": "agentquant-test"},
        ), patch.object(backtest, "run_pre_backtest_test", return_value=0), patch.object(
            backtest,
            "resolve_trading_days",
            return_value=trading_days,
        ), patch.object(backtest, "get_phase_record", side_effect=fake_phase_record), patch.object(
            backtest,
            "run_command",
            side_effect=fake_run_command,
        ), patch.object(
            backtest,
            "run_daily_cumulative_backtest_test",
            side_effect=fake_daily_gate,
        ), patch.object(backtest, "run_backtest_daily_test") as final_daily_gate:
            result = backtest.main()

        self.assertEqual(result, 7)
        self.assertEqual(
            daily_windows,
            [
                ("2025-03-27", "2025-03-27", True),
                ("2025-03-27", "2025-03-28", True),
            ],
        )
        self.assertIn(("validate_phase_flow.py", "2025-03-28"), executed_scripts)
        self.assertNotIn(("proposal.py", "2025-03-29"), executed_scripts)
        self.assertNotIn(("evaluate_config.py", None), executed_scripts)
        final_daily_gate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
