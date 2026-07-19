import os
import sys
import tempfile
import types
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


def _daily_fact(trading_date: str, contract_code: str = "RB2505") -> SimpleNamespace:
    return SimpleNamespace(
        trade_date=trading_date,
        open_price=3500.0,
        highest_price=3520.0,
        lowest_price=3490.0,
        close_price=3510.0,
        settle_price=3508.0,
        turnover_vol=100,
        open_int=1000,
        ticker=contract_code,
    )


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

    def _run_pandaai_minute_readiness(self, *, symbol: str, trading_code: str):
        from apis.pandaai.api import PandaAIAPI
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module

        provider = types.ModuleType("panda_data")
        provider_calls = []
        source_row = {
            "date": "20250309",
            "minute": "210100",
            "datetime": "2025-03-09 21:01:00",
            "symbol": symbol,
            "dominant_id": "",
            "trading_code": trading_code,
            "underlying_symbol": "SR",
            "exchange": "CZCE",
            "open": 5800.0,
            "high": 5810.0,
            "low": 5790.0,
            "close": 5805.0,
            "volume": 10,
            "trading_date": "20250310",
        }

        def init_token(username, password):
            return None

        def get_market_min_data(**kwargs):
            provider_calls.append(dict(kwargs))
            return [source_row]

        provider.init_token = init_token
        provider.get_market_min_data = get_market_min_data
        PandaAIAPI._shared_minute_cache.clear()
        PandaAIAPI._shared_token_initialized = False
        PandaAIAPI._shared_sdk_user_cache_configured = False
        returned_rows = []
        env = {
            "PANDAAI_USERNAME": "user",
            "PANDAAI_PASSWORD": "pass",
            "PANDAAI_PERSISTENT_MARKET_CACHE": "0",
        }
        with patch.dict(os.environ, env), patch.dict(sys.modules, {"panda_data": provider}):
            adapter = PandaAIAPI()

            class FakeAPI:
                def get_futures_daily_candles_optimized(self, **_kwargs):
                    return [
                        _daily_fact("2025-03-07", "SR2505"),
                        _daily_fact("2025-03-10", "SR2505"),
                    ]

            class FakeRouter:
                def __init__(self, **_kwargs):
                    self.api = FakeAPI()

                def get_futures_contract_quote_on_date(self, contract_code, _trading_date):
                    return SimpleNamespace(ticker=contract_code, settle_price=5805.0)

                def get_china_futures_minute_bars(self, **kwargs):
                    rows = adapter.get_futures_minute_bars(**kwargs)
                    returned_rows.append([dict(row) for row in rows])
                    return rows

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
            ), patch(
                "util.trading_calendar.get_previous_trading_day",
                return_value=datetime(2025, 3, 7),
            ):
                result = _data_readiness_check(
                    {
                        "market_type": "china_futures",
                        "tickers": ["SR"],
                        "factor_data": {},
                    },
                    start=datetime(2025, 3, 10),
                    end=datetime(2025, 3, 10),
                )

        return result, provider_calls, returned_rows, source_row

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
                    _daily_fact("2025-03-07"),
                    _daily_fact("2025-03-10"),
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
                        "trading_code": "RB2505",
                        "trading_date": "20250310",
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
        self.assertTrue(any(row[0] == "daily" for row in calls))
        self.assertTrue(any(row[0] == "concrete_contract" for row in calls))
        self.assertTrue(any(row[0] == "fundamental" for row in calls))
        self.assertTrue(any(row[0] == "news" for row in calls))

    def test_data_readiness_uses_one_canonical_contract_for_two_minute_capabilities(self):
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module

        contract_codes = {"CF": "CF2505", "RB": "RB2505", "M": "M2505"}
        minute_calls = []

        class FakeAPI:
            def get_futures_daily_candles_optimized(self, **kwargs):
                ticker = kwargs["underlying_code"]
                return [
                    _daily_fact("2025-03-07", contract_codes[ticker]),
                    _daily_fact("2025-03-10", contract_codes[ticker]),
                ]

        class FakeRouter:
            def __init__(self, **_kwargs):
                self.api = FakeAPI()

            def get_futures_main_contract_quote_on_date(self, underlying_code, _trading_date):
                return SimpleNamespace(ticker=contract_codes[underlying_code], settle_price=3508.0)

            def get_futures_contract_quote_on_date(self, contract_code, _trading_date):
                return SimpleNamespace(ticker=contract_code, settle_price=3508.0)

            def get_china_futures_minute_bars(self, **kwargs):
                minute_calls.append(dict(kwargs))
                return [
                    {
                        "datetime": "2025-03-09 21:01:00",
                        "open": 3500.0,
                        "high": 3501.0,
                        "low": 3499.0,
                        "close": 3500.5,
                        "volume": 10,
                        "trading_code": kwargs["contract_id"],
                        "trading_date": "20250310",
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
                {
                    "market_type": "china_futures",
                    "tickers": ["CF", "RB", "M"],
                    "factor_data": {},
                },
                start=datetime(2025, 3, 10),
                end=datetime(2025, 3, 10),
            )

        self.assertTrue(result.passed, result.to_dict())
        self.assertEqual(len(minute_calls), 2)
        self.assertEqual({call["frequency"] for call in minute_calls}, {"15m", "1m"})
        self.assertEqual({call["contract_id"] for call in minute_calls}, {"CF2505"})
        self.assertEqual({call["underlying_code"] for call in minute_calls}, {"CF"})

    def test_real_pandaai_adapter_canonicalizes_sr505_before_pg_minute_readiness(self):
        result, provider_calls, returned_rows, source_row = self._run_pandaai_minute_readiness(
            symbol="SR2505.CZC",
            trading_code="SR505",
        )

        self.assertTrue(result.passed, result.to_dict())
        self.assertEqual({call["frequency"] for call in provider_calls}, {"15m", "1m"})
        self.assertEqual({tuple(call["symbol"]) for call in provider_calls}, {("SR2505.CZC",)})
        self.assertEqual(len(returned_rows), 2)
        self.assertEqual(
            {row["trading_code"] for rows in returned_rows for row in rows},
            {"SR2505"},
        )
        self.assertEqual(source_row["trading_code"], "SR505")

    def test_real_pandaai_adapter_wrong_month_is_pg_interface_unreadable(self):
        result, provider_calls, returned_rows, _source_row = self._run_pandaai_minute_readiness(
            symbol="SR2506.CZC",
            trading_code="SR506",
        )

        self.assertFalse(result.passed)
        self.assertIn("trader_minute_data_interface_unreadable", result.violation_codes)
        self.assertNotIn("trader_minute_data_missing", result.violation_codes)
        self.assertNotIn("trader_minute_data_contract_mismatch", result.violation_codes)
        self.assertEqual({call["frequency"] for call in provider_calls}, {"15m", "1m"})
        self.assertEqual(returned_rows, [])

    def test_data_readiness_begins_at_formal_previous_start_day(self):
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module

        contract_codes = {"RB": "RB2505", "CF": "CF2505"}
        fact_days = (
            "2025-03-21",
            "2025-03-24",
            "2025-03-25",
            "2025-03-26",
            "2025-03-27",
            "2025-03-28",
            "2025-03-31",
        )
        daily_calls = []
        concrete_calls = []

        class FakeAPI:
            def get_futures_daily_candles_optimized(self, **kwargs):
                daily_calls.append(dict(kwargs))
                query_start = kwargs["start_date"].strftime("%Y-%m-%d")
                query_end = kwargs["end_date"].strftime("%Y-%m-%d")
                ticker = kwargs["underlying_code"]
                return [
                    SimpleNamespace(
                        trade_date=day,
                        open_price=3500.0,
                        highest_price=3520.0,
                        lowest_price=3490.0,
                        close_price=3510.0,
                        settle_price=3508.0,
                        turnover_vol=100,
                        open_int=1000,
                        ticker=contract_codes[ticker],
                    )
                    for day in fact_days
                    if query_start <= day <= query_end
                ]

        class FakeRouter:
            def __init__(self, **_kwargs):
                self.api = FakeAPI()

            def get_futures_contract_quote_on_date(self, contract_code, trading_date):
                concrete_calls.append((contract_code, str(trading_date)[:10]))
                return SimpleNamespace(ticker=contract_code, settle_price=3508.0)

            def get_china_futures_minute_bars(self, **kwargs):
                logical_day = kwargs["start_date"].strftime("%Y-%m-%d")
                return [
                    {
                        "datetime": f"{logical_day} 09:01:00",
                        "open": 3500.0,
                        "high": 3501.0,
                        "low": 3499.0,
                        "close": 3500.5,
                        "volume": 10,
                        "trading_code": kwargs["contract_id"],
                        "trading_date": logical_day,
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
        ), patch(
            "util.trading_calendar.get_previous_trading_day",
            return_value=datetime(2025, 3, 21),
        ) as previous_day:
            result = _data_readiness_check(
                {
                    "market_type": "china_futures",
                    "tickers": ["RB", "CF"],
                    "factor_data": {},
                },
                start=datetime(2025, 3, 24),
                end=datetime(2025, 3, 31),
            )

        self.assertTrue(result.passed, result.to_dict())
        previous_day.assert_called_once()
        self.assertEqual(previous_day.call_args.args[1], datetime(2025, 3, 24))
        self.assertEqual(previous_day.call_args.args[2], "RB")
        self.assertEqual(
            {call["start_date"].strftime("%Y-%m-%d") for call in daily_calls},
            {"2025-03-21"},
        )
        self.assertIn(("RB2505", "2025-03-21"), concrete_calls)
        self.assertIn(("CF2505", "2025-03-21"), concrete_calls)

    def test_minute_readiness_accepts_physical_previous_night_for_logical_trading_day(self):
        result = self._run_minute_readiness_with_rows(
            [
                {
                    "datetime": "2025-03-09 21:01:00",
                    "open": 3500.0,
                    "high": 3501.0,
                    "low": 3499.0,
                    "close": 3500.5,
                    "volume": 10,
                    "trading_code": "RB2505",
                    "trading_date": "20250310",
                }
            ]
        )
        self.assertTrue(result.passed, result.to_dict())

    def test_minute_readiness_rejects_future_logical_trading_date(self):
        result = self._run_minute_readiness_with_rows(
            [
                {
                    "datetime": "2025-03-10 21:01:00",
                    "open": 3500.0,
                    "high": 3501.0,
                    "low": 3499.0,
                    "close": 3500.5,
                    "volume": 10,
                    "trading_code": "RB2505",
                    "trading_date": "20250311",
                }
            ]
        )
        self.assertFalse(result.passed)
        self.assertIn("trader_minute_data_logical_date_invalid", result.violation_codes)

    def test_minute_readiness_rejects_missing_required_field(self):
        result = self._run_minute_readiness_with_rows(
            [
                {
                    "datetime": "2025-03-10 09:01:00",
                    "open": 3500.0,
                    "high": 3501.0,
                    "low": 3499.0,
                    "close": 3500.5,
                    "trading_code": "RB2505",
                    "trading_date": "20250310",
                }
            ]
        )
        self.assertFalse(result.passed)
        self.assertIn("trader_minute_data_required_field_missing", result.violation_codes)

    def test_minute_readiness_rejects_concrete_contract_mismatch(self):
        result = self._run_minute_readiness_with_rows(
            [
                {
                    "datetime": "2025-03-10 09:01:00",
                    "open": 3500.0,
                    "high": 3501.0,
                    "low": 3499.0,
                    "close": 3500.5,
                    "volume": 10,
                    "trading_code": "M2505",
                    "trading_date": "20250310",
                }
            ]
        )
        self.assertFalse(result.passed)
        self.assertIn("trader_minute_data_contract_mismatch", result.violation_codes)

    def test_minute_readiness_distinguishes_unavailable_interface_from_runtime_failure(self):
        unavailable = self._run_minute_readiness_with_rows(
            NotImplementedError("minute frequency is unavailable")
        )
        unreadable = self._run_minute_readiness_with_rows(
            RuntimeError("provider payload processing failed")
        )

        self.assertIn("trader_minute_data_interface_unavailable", unavailable.violation_codes)
        self.assertNotIn("trader_minute_data_interface_unreadable", unavailable.violation_codes)
        self.assertIn("trader_minute_data_interface_unreadable", unreadable.violation_codes)

    def _run_minute_readiness_with_rows(self, minute_result):
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module

        class FakeAPI:
            def get_futures_daily_candles_optimized(self, **_kwargs):
                return [
                    _daily_fact("2025-03-07"),
                    _daily_fact("2025-03-10"),
                ]

        class FakeRouter:
            def __init__(self, **_kwargs):
                self.api = FakeAPI()

            def get_futures_main_contract_quote_on_date(self, *_args, **_kwargs):
                return SimpleNamespace(ticker="RB2505", settle_price=3508.0)

            def get_futures_contract_quote_on_date(self, *_args, **_kwargs):
                return SimpleNamespace(ticker="RB2505", settle_price=3508.0)

            def get_china_futures_minute_bars(self, **_kwargs):
                if isinstance(minute_result, BaseException):
                    raise minute_result
                return list(minute_result)

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
            return _data_readiness_check(
                {"market_type": "china_futures", "tickers": ["RB"], "factor_data": {}},
                start=datetime(2025, 3, 10),
                end=datetime(2025, 3, 10),
            )

    def test_optional_fundamental_and_news_empty_results_do_not_block_readiness(self):
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module

        class FakeAPI:
            def get_futures_daily_candles_optimized(self, **_kwargs):
                return [
                    _daily_fact("2025-03-07"),
                    _daily_fact("2025-03-10"),
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
                        "trading_code": "RB2505",
                        "trading_date": "20250310",
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

    def test_data_readiness_passes_after_adapter_retries_one_gateway_failure(self):
        from apis.pandaai.api import PandaAIAPI
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module

        fake_provider = types.ModuleType("panda_data")
        provider_calls = []

        def init_token(**_kwargs):
            return None

        def get_market_data(**kwargs):
            provider_calls.append(kwargs)
            if len(provider_calls) == 1:
                raise RuntimeError("HTTP 502: Bad Gateway")
            return [{"provider_result": "real"}]

        fake_provider.init_token = init_token
        fake_provider.get_market_data = get_market_data
        PandaAIAPI._shared_token_initialized = False
        PandaAIAPI._shared_sdk_user_cache_configured = False

        env = {
            "PANDAAI_USERNAME": "user",
            "PANDAAI_PASSWORD": "pass",
            "PANDAAI_PERSISTENT_MARKET_CACHE": "0",
        }
        with patch.dict(os.environ, env), patch.dict(sys.modules, {"panda_data": fake_provider}), patch(
            "apis.pandaai.api.time.sleep"
        ):
            adapter = PandaAIAPI()
            adapter._network_retry_initial_wait_seconds = 0.0
            adapter._network_retry_max_wait_seconds = 0.0
            adapter._wait_for_request_slot = lambda: None

            class FakeAPI:
                def get_futures_daily_candles_optimized(self, **_kwargs):
                    return [
                        _daily_fact("2025-03-07"),
                        _daily_fact("2025-03-10"),
                    ]

            class FakeRouter:
                def __init__(self, **_kwargs):
                    self.api = FakeAPI()

                def get_futures_contract_quote_on_date(self, *_args, **_kwargs):
                    adapter._call_pandaai("get_market_data", symbol="RB2505.SHF")
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
                            "trading_code": "RB2505",
                            "trading_date": "20250310",
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
        self.assertEqual(len(provider_calls), 2)

    def test_data_readiness_classifies_escaped_nontransient_contract_failure(self):
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module

        class FakeAPI:
            def get_futures_daily_candles_optimized(self, **_kwargs):
                return [
                    _daily_fact("2025-03-07"),
                    _daily_fact("2025-03-10"),
                ]

        class FakeRouter:
            def __init__(self, **_kwargs):
                self.api = FakeAPI()

            def get_futures_contract_quote_on_date(self, *_args, **_kwargs):
                raise RuntimeError("contract response could not be parsed")

            def get_china_futures_minute_bars(self, **_kwargs):
                return [
                    {
                        "datetime": "2025-03-10 09:01:00",
                        "open": 3500.0,
                        "high": 3501.0,
                        "low": 3499.0,
                        "close": 3500.5,
                        "volume": 10,
                        "trading_code": "RB2505",
                        "trading_date": "20250310",
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

        self.assertFalse(result.passed)
        self.assertIn("concrete_contract_fact_interface_unreadable", result.violation_codes)
        self.assertIn("trader_minute_representative_contract_missing", result.violation_codes)
        self.assertNotIn("pandaai_gateway_error_after_retry", result.violation_codes)

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

    def test_time_boundary_runtime_uses_formal_previous_trading_day_resolver(self):
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module
        from util import trading_calendar

        with patch(
            "util.trading_calendar.get_previous_trading_day",
            wraps=trading_calendar.get_previous_trading_day,
        ) as resolver:
            result = module._runtime_time_boundary_check(
                datetime(2025, 3, 25),
                datetime(2025, 3, 26),
            )

        self.assertTrue(result.passed, result.to_dict())
        resolver.assert_called_once()

    def test_formal_calendar_resolves_requested_window_previous_trading_days(self):
        from util import trading_calendar

        class CalendarAPI:
            @staticmethod
            def get_futures_daily_candles_optimized(**kwargs):
                start_date = kwargs["start_date"].strftime("%Y-%m-%d")
                end_date = kwargs["end_date"].strftime("%Y-%m-%d")
                return [
                    SimpleNamespace(trade_date=value)
                    for value in ("2025-03-24", "2025-03-25", "2025-03-26")
                    if start_date <= value <= end_date
                ]

        router = SimpleNamespace(api=CalendarAPI())
        with patch.dict(trading_calendar._PREVIOUS_TRADING_DAY_CACHE, {}, clear=True), patch.dict(
            trading_calendar._MARKET_PREVIOUS_TRADING_DAY_CACHE,
            {},
            clear=True,
        ):
            previous_t1 = trading_calendar.get_previous_trading_day(
                router,
                datetime(2025, 3, 25),
                "RB",
            )
            previous_t2 = trading_calendar.get_previous_trading_day(
                router,
                datetime(2025, 3, 26),
                "RB",
            )

        self.assertEqual(previous_t1.strftime("%Y-%m-%d"), "2025-03-24")
        self.assertEqual(previous_t2.strftime("%Y-%m-%d"), "2025-03-25")

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

    def test_orchestration_accepts_independent_precheck_and_daily_only_backtest(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            path = root / "src" / "run" / "backtest.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                "def main():\n"
                "    reset_existing_config_if_requested()\n"
                "    run_backtest_daily_test()\n",
                encoding="utf-8",
            )
            result = _static_orchestration_check(root)
        self.assertEqual(result.status, "passed", result.to_dict())

    def test_precheck_uses_no_formal_database_argument(self):
        with self.assertRaises(TypeError):
            self._run(db_path=SRC_ROOT / "assets" / "agentquant.db")


if __name__ == "__main__":
    unittest.main()
