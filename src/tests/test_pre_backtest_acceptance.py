import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.pg_pre_backtest_acceptance import (
    PRE_BACKTEST_CHECK_NAMES,
    _data_readiness_check,
    _static_orchestration_check,
    run_pre_backtest_acceptance,
)
from tools.agent_tools.control.pg_schemas import ProtocolCheckResult


class PreBacktestAcceptanceTest(unittest.TestCase):
    def _run(self, **overrides):
        kwargs = {
            "config_path": SRC_ROOT / "config" / "dev.yaml",
            "repo_root": PROJECT_ROOT,
            "assets_dir": SRC_ROOT / "assets",
            "deepfund_python": Path(sys.executable),
            "run_test_modules": False,
        }
        kwargs.update(overrides)
        with patch.dict(os.environ, {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
            return run_pre_backtest_acceptance(**kwargs)

    def test_report_has_exactly_ten_pre_backtest_categories(self):
        report = self._run()
        self.assertEqual([check.check_name for check in report.checks], list(PRE_BACKTEST_CHECK_NAMES))
        self.assertEqual(len(report.checks), 10)

    def test_without_window_data_check_is_explicitly_skipped(self):
        report = self._run()
        data_check = next(check for check in report.checks if check.check_name == "data_readiness")
        self.assertEqual(data_check.status, "skipped")
        self.assertIn("data_readiness_window_not_requested", data_check.diagnostic_codes)

    def test_invalid_window_fails_time_boundary(self):
        report = self._run(start_date="2025-02-02", end_date="2025-01-01")
        check = next(check for check in report.checks if check.check_name == "time_boundary")
        self.assertEqual(check.status, "failed")
        self.assertIn("backtest_window_end_before_start", check.violation_codes)

    def test_missing_deepfund_fails_environment_check(self):
        report = self._run(deepfund_python=PROJECT_ROOT / "missing" / "python.exe")
        check = next(check for check in report.checks if check.check_name == "environment_and_entry")
        self.assertIn("deepfund_python_missing", check.violation_codes)

    def test_formal_temporary_database_is_created_by_sqlite_setup(self):
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module

        original = module._initialize_formal_temp_database
        calls = []

        def wrapped(path):
            calls.append(path)
            return original(path)

        with patch.object(module, "_initialize_formal_temp_database", side_effect=wrapped):
            report = self._run()
        self.assertTrue(calls)
        check = next(check for check in report.checks if check.check_name == "formal_temporary_database")
        self.assertEqual(check.status, "passed", check.to_dict())

    def test_data_readiness_calls_real_daily_contract_minute_and_optional_data_interfaces(self):
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module

        calls = []

        class FakeAPI:
            def get_futures_daily_candles_optimized(self, **kwargs):
                calls.append(("daily", kwargs["underlying_code"]))
                return [
                    SimpleNamespace(
                        trade_date="2025-03-10",
                        open_price=3500.0,
                        highest_price=3520.0,
                        lowest_price=3490.0,
                        close_price=3510.0,
                        settle_price=3508.0,
                        turnover_vol=100,
                        open_int=1000,
                        ticker="RB2505",
                    )
                ]

        class FakeRouter:
            def __init__(self, **_kwargs):
                self.api = FakeAPI()

            def get_futures_main_contract_quote_on_date(self, underlying_code, trading_date):
                calls.append(("main_contract", underlying_code, str(trading_date)[:10]))
                return SimpleNamespace(ticker="RB2505", settle_price=3508.0)

            def get_futures_contract_quote_on_date(self, contract_code, trading_date):
                calls.append(("concrete_contract", contract_code, str(trading_date)[:10]))
                return SimpleNamespace(ticker=contract_code, settle_price=3508.0)

            def get_china_futures_minute_bars(self, **kwargs):
                calls.append(("minute", kwargs["frequency"]))
                return [
                    {
                        "datetime": "2025-03-10 09:01:00",
                        "open": 3500.0,
                        "high": 3501.0,
                        "low": 3499.0,
                        "close": 3500.5,
                        "volume": 10,
                    }
                ]

            def get_china_futures_fundamentals(self, ticker, trading_date):
                calls.append(("fundamental", ticker, str(trading_date)[:10]))
                return None

            def get_china_futures_news(self, ticker, trading_date, news_count=10, pre_open_only=True):
                calls.append(("news", ticker, str(trading_date)[:10], pre_open_only))
                return []

        cfg = {
            "market_type": "china_futures",
            "tickers": ["RB"],
            "factor_data": {},
        }
        contract_info = {
            "contract_multiplier": 10.0,
            "margin_rate_long": 0.1,
            "margin_rate_short": 0.1,
        }
        with patch("apis.router.Router", FakeRouter), patch.object(
            module,
            "_optional_data_directories_ready",
            return_value=([], []),
            create=True,
        ), patch(
            "apis.contract_info_cache.FuturesContractInfoCache.get_contract_info",
            return_value=contract_info,
        ):
            result = _data_readiness_check(
                cfg,
                start=datetime(2025, 3, 10),
                end=datetime(2025, 3, 10),
            )

        self.assertTrue(result.passed, result.to_dict())
        self.assertIn(("minute", "15m"), calls)
        self.assertIn(("minute", "1m"), calls)
        self.assertTrue(any(row[0] == "main_contract" for row in calls))
        self.assertTrue(any(row[0] == "concrete_contract" for row in calls))
        self.assertTrue(any(row[0] == "fundamental" for row in calls))
        self.assertTrue(any(row[0] == "news" for row in calls))

    def test_optional_fundamental_and_news_empty_results_do_not_block_readiness(self):
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module

        class FakeAPI:
            def get_futures_daily_candles_optimized(self, **_kwargs):
                return [
                    SimpleNamespace(
                        trade_date="2025-03-10",
                        open_price=3500.0,
                        highest_price=3520.0,
                        lowest_price=3490.0,
                        close_price=3510.0,
                        settle_price=3508.0,
                        turnover_vol=100,
                        open_int=1000,
                        ticker="RB2505",
                    )
                ]

        class FakeRouter:
            def __init__(self, **_kwargs):
                self.api = FakeAPI()

            def get_futures_main_contract_quote_on_date(self, *_args, **_kwargs):
                return SimpleNamespace(ticker="RB2505", settle_price=3508.0)

            def get_futures_contract_quote_on_date(self, *_args, **_kwargs):
                return SimpleNamespace(ticker="RB2505", settle_price=3508.0)

            def get_china_futures_minute_bars(self, **_kwargs):
                return [
                    {
                        "datetime": "2025-03-10 09:01:00",
                        "open": 3500.0,
                        "high": 3501.0,
                        "low": 3499.0,
                        "close": 3500.5,
                        "volume": 10,
                    }
                ]

            def get_china_futures_fundamentals(self, *_args, **_kwargs):
                return None

            def get_china_futures_news(self, *_args, **_kwargs):
                return []

        with patch("apis.router.Router", FakeRouter), patch.object(
            module,
            "_optional_data_directories_ready",
            return_value=([], []),
            create=True,
        ), patch(
            "apis.contract_info_cache.FuturesContractInfoCache.get_contract_info",
            return_value={
                "contract_multiplier": 10.0,
                "margin_rate_long": 0.1,
                "margin_rate_short": 0.1,
            },
        ):
            result = _data_readiness_check(
                {"market_type": "china_futures", "tickers": ["RB"], "factor_data": {}},
                start=datetime(2025, 3, 10),
                end=datetime(2025, 3, 10),
            )
        self.assertTrue(result.passed, result.to_dict())
        self.assertIn("finoview_optional_records_unavailable", result.diagnostic_codes)
        self.assertIn("news_optional_records_unavailable", result.diagnostic_codes)

    def test_time_boundary_runs_one_runtime_wiring_acceptance(self):
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module

        runtime_check = ProtocolCheckResult.pass_result("time_boundary")
        with patch.object(
            module,
            "_runtime_time_boundary_check",
            return_value=runtime_check,
            create=True,
        ) as wired:
            report = self._run(start_date="2025-03-10", end_date="2025-03-10")
        wired.assert_called_once()
        self.assertEqual(
            next(check for check in report.checks if check.check_name == "time_boundary").status,
            "passed",
        )

    def test_same_formal_temporary_database_runs_no_llm_full_chain(self):
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module

        observed_paths = []

        def dry_run(*, db_path, cfg, artifact_root):
            observed_paths.append(Path(db_path))
            self.assertTrue(Path(db_path).is_file())
            self.assertTrue(artifact_root.parent.is_dir())
            return ProtocolCheckResult.pass_result("no_llm_full_chain_dry_run")

        with patch.object(
            module,
            "run_no_llm_full_chain_dry_run",
            side_effect=dry_run,
            create=True,
        ) as full_chain:
            report = self._run()
        full_chain.assert_called_once()
        self.assertEqual(len(observed_paths), 1)
        self.assertEqual(
            next(check for check in report.checks if check.check_name == "no_llm_full_chain_dry_run").status,
            "passed",
        )

    def test_string_mentions_are_not_orchestration_evidence(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            path = root / "src" / "run" / "backtest.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                'TEXT = "run_pre_backtest_test run_backtest_daily_test"\n',
                encoding="utf-8",
            )
            result = _static_orchestration_check(root)
        self.assertEqual(result.status, "failed")
        self.assertIn("backtest_pg_orchestration_missing", result.violation_codes)

    def test_precheck_uses_no_formal_database_argument(self):
        with self.assertRaises(TypeError):
            self._run(db_path=SRC_ROOT / "assets" / "agentquant.db")


if __name__ == "__main__":
    unittest.main()
