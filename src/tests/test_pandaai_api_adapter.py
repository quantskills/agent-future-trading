import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from apis.pandaai.api import PandaAIAPI, PandaAIDailyQuotaExhausted
from tools.agent_tools.analysis.analyst_data_usage import prefetch_pandaai_daily_data


class FakeProviderError(RuntimeError):
    def __init__(self, message, *, code=None, status_code=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class PandaAIAdapterTest(unittest.TestCase):
    def setUp(self):
        PandaAIAPI._shared_history_cache.clear()
        PandaAIAPI._shared_quote_cache.clear()
        PandaAIAPI._shared_minute_cache.clear()
        PandaAIAPI._shared_extra_cache.clear()
        PandaAIAPI._shared_extra_diagnostics_cache.clear()
        PandaAIAPI._shared_unavailable_extra_feature_cache.clear()
        PandaAIAPI._shared_exchange_suffix_cache.clear()
        PandaAIAPI._shared_sdk_method_aliases.clear()
        PandaAIAPI._shared_token_initialized = False
        PandaAIAPI._shared_sdk_user_cache_configured = False
        PandaAIAPI._market_cache_db_initialized = False
        PandaAIAPI._daily_quota_exhausted_on = None
        PandaAIAPI._last_request_at = 0.0
        PandaAIAPI._rate_limit_cooldown_until = 0.0

    def _build_api(self):
        fake = types.ModuleType("panda_data")
        fake.calls = []

        def init_token(username, password):
            fake.calls.append({"func": "init_token", "username": username, "password": password})

        def get_future_detail(symbol=None, fields=None, is_trading=1):
            fake.calls.append(
                {
                    "func": "get_future_detail",
                    "symbol": symbol,
                    "fields": fields,
                    "is_trading": is_trading,
                }
            )
            return [
                {"underlying_symbol": "M", "exchange": "DCE", "symbol": "M2505.DCE"},
                {"underlying_symbol": "RB", "exchange": "SHFE", "symbol": "RB2505.SHF"},
            ]

        def get_market_data(**kwargs):
            fake.calls.append({"func": "get_market_data", **kwargs})
            return [
                {
                    "date": "2025-01-04",
                    "symbol": kwargs["symbol"],
                    "trading_code": "M2505",
                    "underlying_symbol": "M",
                    "exchange": "DCE",
                    "open": 999.0,
                    "day_session_open": 3210.0,
                    "high": 3260.0,
                    "low": 3200.0,
                    "close": 3240.0,
                    "settlement": 3235.0,
                    "pre_settlement": 3180.0,
                    "volume": 120,
                    "amount": 388200.0,
                    "open_interest": 900,
                    "limit_up": 3500.0,
                    "limit_down": 2900.0,
                },
                {
                    "date": "2025-01-03",
                    "symbol": kwargs["symbol"],
                    "trading_code": "M2505",
                    "underlying_symbol": "M",
                    "exchange": "DCE",
                    "open": 3100.0,
                    "day_session_open": 3110.0,
                    "high": 3160.0,
                    "low": 3090.0,
                    "close": 3140.0,
                    "settlement": 3130.0,
                    "pre_settlement": 3090.0,
                    "volume": 100,
                    "amount": 313000.0,
                    "open_interest": 800,
                    "limit_up": 3400.0,
                    "limit_down": 2800.0,
                },
                {
                    "date": "2025-01-02",
                    "symbol": kwargs["symbol"],
                    "trading_code": "M2505",
                    "underlying_symbol": "M",
                    "exchange": "DCE",
                    "open": 3000.0,
                    "day_session_open": None,
                    "high": 3090.0,
                    "low": 2990.0,
                    "close": 3080.0,
                    "settlement": 3090.0,
                    "pre_settlement": 3010.0,
                    "volume": 90,
                    "amount": 278100.0,
                    "open_interest": 700,
                    "limit_up": 3310.0,
                    "limit_down": 2710.0,
                },
            ]

        def get_market_min_data(**kwargs):
            fake.calls.append({"func": "get_market_min_data", **kwargs})
            symbol = kwargs["symbol"][0] if isinstance(kwargs.get("symbol"), list) else kwargs.get("symbol")
            return [
                {
                    "date": "20250104",
                    "minute": "100000",
                    "datetime": "2025-01-04 10:00:00",
                    "symbol": symbol,
                    "trading_code": "M2505",
                    "underlying_symbol": "M",
                    "exchange": "DCE",
                    "open": 3200.0,
                    "high": 3220.0,
                    "low": 3190.0,
                    "close": 3215.0,
                    "volume": 100,
                    "amount": 320000.0,
                    "open_interest": 900,
                    "trading_date": "20250104",
                },
                {
                    "date": "20250104",
                    "minute": "101500",
                    "datetime": "2025-01-04 10:15:00",
                    "symbol": symbol,
                    "trading_code": "M2505",
                    "underlying_symbol": "M",
                    "exchange": "DCE",
                    "open": 3215.0,
                    "high": 3230.0,
                    "low": 3210.0,
                    "close": 3225.0,
                    "volume": 120,
                    "amount": 386400.0,
                    "open_interest": 920,
                    "trading_date": "20250104",
                },
            ]

        def get_future_basis(**kwargs):
            fake.calls.append({"func": "get_future_basis", **kwargs})
            return [{"underlying_symbol": "M", "date": "2025-01-03", "basis": 80.0, "basis_ratio": 0.03}]

        def get_future_warehouse_receipt(**kwargs):
            fake.calls.append({"func": "get_future_warehouse_receipt", **kwargs})
            return [{"underlying_symbol": "M", "date": "2025-01-03", "wr_lot_change": -10, "wr_lot_quantity": 100}]

        def get_future_ls_ratio(**kwargs):
            fake.calls.append({"func": "get_future_ls_ratio", **kwargs})
            return [{"symbol": "M2505.DCE", "date": "2025-01-03", "ls_ratio": 1.2}]

        def get_future_variety_posi(**kwargs):
            fake.calls.append({"func": "get_future_variety_posi", **kwargs})
            return [{"underlying_symbol": "M", "date": "2025-01-03", "change_oi": 12, "open_interest": 100, "position_type": kwargs.get("position_type")}]

        def get_future_symbol_posi(**kwargs):
            fake.calls.append({"func": "get_future_symbol_posi", **kwargs})
            return [{"symbol": "M2505.DCE", "date": "2025-01-03", "change_oi": 8, "open_interest": 80, "position": kwargs.get("position_type")}]

        def get_broker_netmarg_change(**kwargs):
            fake.calls.append({"func": "get_broker_netmarg_change", **kwargs})
            return [{"underlying_symbol": "M", "date": "2025-01-03", "margin_change": 1000.0}]

        def get_broker_netmarg(**kwargs):
            fake.calls.append({"func": "get_broker_netmarg", **kwargs})
            return [{"underlying_symbol": "M", "date": "2025-01-03", "net_margin": 2000.0}]

        def get_future_netcap_change(**kwargs):
            fake.calls.append({"func": "get_future_netcap_change", **kwargs})
            return [{"symbol": "M2505.DCE", "date": "2025-01-03", "net_cap_value": 3000.0}]

        def get_future_contract_indicators(**kwargs):
            fake.calls.append({"func": "get_future_contract_indicators", **kwargs})
            return [{"symbol": "M2505.DCE", "date": "2025-01-03", "ratio": 1.1}]

        def get_future_contract_rank(**kwargs):
            fake.calls.append({"func": "get_future_contract_rank", **kwargs})
            if not kwargs.get("rank_type"):
                raise RuntimeError("rank_type参数不能为空")
            return [
                {
                    "symbol": "M2505.DCE",
                    "underlying_symbol": "M",
                    "date": "2025-01-03",
                    "position_type": "long",
                    "rank": 1,
                    "ratio": 1.2,
                }
            ]

        fake.init_token = init_token
        fake.get_future_detail = get_future_detail
        fake.get_market_data = get_market_data
        fake.get_market_min_data = get_market_min_data
        fake.get_future_basis = get_future_basis
        fake.get_future_warehouse_receipt = get_future_warehouse_receipt
        fake.get_future_ls_ratio = get_future_ls_ratio
        fake.get_future_variety_posi = get_future_variety_posi
        fake.get_future_symbol_posi = get_future_symbol_posi
        fake.get_broker_netmarg_change = get_broker_netmarg_change
        fake.get_broker_netmarg = get_broker_netmarg
        fake.get_future_netcap_change = get_future_netcap_change
        fake.get_future_contract_indicators = get_future_contract_indicators
        fake.get_future_contract_rank = get_future_contract_rank

        env = {
            "PANDAAI_USERNAME": "user",
            "PANDAAI_PASSWORD": "pass",
            "PANDAAI_PERSISTENT_MARKET_CACHE": "0",
        }
        modules = {"panda_data": fake}
        return fake, patch.dict(os.environ, env), patch.dict(sys.modules, modules)

    @staticmethod
    def _minute_row(
        *,
        timestamp: datetime,
        trading_date: str,
        symbol: str,
        trading_code: str,
        exchange: str,
        underlying_code: str,
        dominant_id: str = "",
    ) -> dict:
        return {
            "date": timestamp.strftime("%Y%m%d"),
            "minute": timestamp.strftime("%H%M%S"),
            "datetime": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "dominant_id": dominant_id,
            "trading_code": trading_code,
            "underlying_symbol": underlying_code,
            "exchange": exchange,
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "volume": 10,
            "amount": 1010.0,
            "open_interest": 100,
            "trading_date": trading_date,
        }

    def _build_api_with_minute_rows(self, rows):
        fake, env_patch, module_patch = self._build_api()
        source_rows = list(rows)

        def get_market_min_data(**kwargs):
            fake.calls.append({"func": "get_market_min_data", **kwargs})
            return source_rows

        fake.get_market_min_data = get_market_min_data
        return fake, source_rows, env_patch, module_patch

    def test_sdk_user_cache_is_redirected_to_writable_runtime_dir(self):
        fake, env_patch, module_patch = self._build_api()
        common_utils = types.ModuleType("panda_data.utils.common_utils")
        init_token_module = types.ModuleType("panda_data.readers.init_token")
        http_module = types.ModuleType("panda_data.transport.http")
        future_reader_module = types.ModuleType("panda_data.readers.future_reader")
        client_module = types.ModuleType("panda_data.client")

        for module in (common_utils, init_token_module, http_module, future_reader_module, client_module):
            module.find_project_root = lambda current_path, markers=None: r"C:\ProgramData\miniconda3"

        modules = {
            "panda_data": fake,
            "panda_data.utils.common_utils": common_utils,
            "panda_data.readers.init_token": init_token_module,
            "panda_data.transport.http": http_module,
            "panda_data.readers.future_reader": future_reader_module,
            "panda_data.client": client_module,
        }
        env = {
            "PANDAAI_USERNAME": "user",
            "PANDAAI_PASSWORD": "pass",
            "PANDAAI_PERSISTENT_MARKET_CACHE": "0",
        }
        original_configured = PandaAIAPI._shared_sdk_user_cache_configured
        original_token_initialized = PandaAIAPI._shared_token_initialized
        PandaAIAPI._shared_sdk_user_cache_configured = False
        PandaAIAPI._shared_token_initialized = False
        try:
            with tempfile.TemporaryDirectory(prefix="agentquant_pandaai_sdk_auth_") as tmpdir:
                cache_root = Path(tmpdir) / "sdk_auth"
                env["PANDAAI_SDK_USER_CACHE_DIR"] = str(cache_root)
                with env_patch, module_patch, patch.dict(os.environ, env), patch.dict(sys.modules, modules):
                    api = PandaAIAPI()
                    api._ensure_token()

                    self.assertEqual(common_utils.find_project_root("ignored"), str(cache_root))
                    self.assertEqual(init_token_module.find_project_root("ignored"), str(cache_root))
                    self.assertEqual(http_module.find_project_root("ignored"), str(cache_root))
                    self.assertEqual(future_reader_module.find_project_root("ignored"), str(cache_root))
                    self.assertEqual(client_module.find_project_root("ignored"), str(cache_root))
                    self.assertEqual(os.environ["PANDAAI_SDK_USER_CACHE_DIR"], str(cache_root))
                    self.assertTrue(cache_root.exists())
                    self.assertEqual(fake.calls[0]["func"], "init_token")
        finally:
            PandaAIAPI._shared_sdk_user_cache_configured = original_configured
            PandaAIAPI._shared_token_initialized = original_token_initialized

    def test_historical_window_excludes_end_date_and_sorts_ascending(self):
        fake, env_patch, module_patch = self._build_api()
        with env_patch, module_patch:
            api = PandaAIAPI()
            quotes = api.get_futures_daily_candles_optimized(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 2),
                end_date=datetime(2025, 1, 4),
            )

        self.assertEqual([quote.trade_date for quote in quotes], ["2025-01-02", "2025-01-03"])
        self.assertEqual(quotes[-1].ticker, "M2505")
        self.assertEqual(quotes[-1].open_price, 3110.0)
        self.assertEqual(quotes[-1].settle_price, 3130.0)
        self.assertEqual(quotes[-1].pre_settle_price, 3090.0)
        self.assertEqual(
            next(call for call in fake.calls if call["func"] == "get_market_data")["symbol"],
            "M_DOMINANT.DCE",
        )

    def test_historical_window_can_explicitly_include_end_date(self):
        fake, env_patch, module_patch = self._build_api()
        with env_patch, module_patch:
            api = PandaAIAPI()
            quotes = api.get_futures_daily_candles_optimized(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 2),
                end_date=datetime(2025, 1, 4),
                end_date_inclusive=True,
            )

        self.assertEqual(
            [quote.trade_date for quote in quotes],
            ["2025-01-02", "2025-01-03", "2025-01-04"],
        )

    def test_exact_quote_includes_target_date(self):
        fake, env_patch, module_patch = self._build_api()
        with env_patch, module_patch:
            api = PandaAIAPI()
            quote = api.get_main_contract_quote_on_date("M", datetime(2025, 1, 4))

        self.assertIsNotNone(quote)
        self.assertEqual(quote.trade_date, "2025-01-04")
        self.assertEqual(quote.open_price, 3210.0)
        self.assertEqual(quote.close_price, 3240.0)

    def test_concrete_contract_query_uses_pandaai_symbol_boundary(self):
        fake, env_patch, module_patch = self._build_api()
        with env_patch, module_patch:
            api = PandaAIAPI()
            quote = api.get_futures_quote_on_date(datetime(2025, 1, 3), contract_id="m2505")

        self.assertIsNotNone(quote)
        market_call = next(call for call in fake.calls if call["func"] == "get_market_data")
        self.assertEqual(market_call["symbol"], "M2505.DCE")
        self.assertEqual(quote.ticker, "M2505")
        self.assertFalse(any(call["func"] == "get_future_detail" for call in fake.calls))

    def test_business_contract_code_uses_one_public_format_and_record_date(self):
        api = PandaAIAPI.__new__(PandaAIAPI)
        cases = (
            (
                {
                    "date": "2025-03-27",
                    "symbol": "BU2506.SHF",
                    "dominant_id": "BU2510",
                    "trading_code": "bu2512",
                },
                "BU2506",
            ),
            ({"date": "2025-03-27", "trading_code": "bu2506"}, "BU2506"),
            (
                {
                    "date": "2025-03-27",
                    "symbol": "SR2505.CZC",
                    "trading_code": "SR505",
                },
                "SR2505",
            ),
            (
                {
                    "date": "2025-03-27",
                    "symbol": "SR_DOMINANT.CZC",
                    "dominant_id": "SR2505",
                    "trading_code": "SR506",
                },
                "SR2505",
            ),
            ({"date": "2025-03-27", "trading_code": "sr505"}, "SR2505"),
            ({"date": "2019-03-27", "trading_code": "SR905"}, "SR1905"),
            ({"date": "2029-03-27", "trading_code": "SR905"}, "SR2905"),
        )

        for row, expected in cases:
            with self.subTest(row=row):
                reference_date = api._parse_trade_date(row["date"])
                self.assertEqual(
                    api._canonical_business_contract_code(row, reference_date),
                    expected,
                )

        may_contract = api._canonical_business_contract_code(
            {"trading_code": "SR505"},
            datetime(2025, 3, 27),
        )
        june_contract = api._canonical_business_contract_code(
            {"trading_code": "SR506"},
            datetime(2025, 3, 27),
        )
        self.assertNotEqual(may_contract, june_contract)
        with self.assertRaisesRegex(ValueError, "reference_date"):
            api._canonical_business_contract_code(
                {"trading_code": "SR505"},
                None,
            )
        with self.assertRaisesRegex(ValueError, "three-digit"):
            api._canonical_business_contract_code(
                {"trading_code": "BU506"},
                datetime(2025, 3, 27),
            )

    def test_daily_quote_outputs_use_canonical_business_contract_code(self):
        api = PandaAIAPI.__new__(PandaAIAPI)
        daily_quote = api._build_daily_quote_from_row(
            {
                "date": "2025-03-27",
                "symbol": "BU2506.SHF",
                "trading_code": "bu2506",
            }
        )
        optimized_quote = api._build_optimized_quote_from_row(
            {
                "date": "2025-03-27",
                "symbol": "SR_DOMINANT.CZC",
                "dominant_id": "SR2505",
                "trading_code": "SR505",
                "underlying_symbol": "SR",
            },
            query_is_main=True,
        )

        self.assertEqual(daily_quote.contract_id, "BU2506")
        self.assertEqual(optimized_quote.ticker, "SR2505")

    def test_public_contract_list_main_code_and_margin_use_canonical_business_code(self):
        fake, env_patch, module_patch = self._build_api()
        with env_patch, module_patch:
            api = PandaAIAPI()
            main_code = api.get_main_contract_code("M", datetime(2025, 1, 4))
            contracts = api.get_china_futures_contracts(underlying_code="M")
            with patch.object(
                api,
                "get_futures_contract_detail",
                return_value={
                    "symbol": "SR2505.CZC",
                    "trading_code": "SR505",
                    "margin_rate": 12.0,
                },
            ):
                margin = api.get_futures_margin("sr505")

        self.assertEqual(main_code, "M2505")
        self.assertEqual(contracts, ["M2505"])
        self.assertIsNotNone(margin)
        self.assertEqual(margin.contract_id, "SR2505")

    def test_market_data_call_does_not_log_provider_request_or_result_details(self):
        fake, env_patch, module_patch = self._build_api()
        with env_patch, module_patch, patch("apis.pandaai.api.logger") as logger_mock:
            api = PandaAIAPI()
            api.get_main_contract_quote_on_date("M", datetime(2025, 1, 4))

        logger_mock.info.assert_not_called()

    def test_gateway_502_retries_then_returns_real_provider_result(self):
        fake, env_patch, module_patch = self._build_api()
        calls = []

        def get_market_data(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("HTTP 502: Bad Gateway")
            return [{"provider_result": "real"}]

        fake.get_market_data = get_market_data
        with env_patch, module_patch, patch("apis.pandaai.api.time.sleep"):
            api = PandaAIAPI()
            api._network_retry_initial_wait_seconds = 0.0
            api._network_retry_max_wait_seconds = 0.0
            api._wait_for_request_slot = lambda: None
            result = api._call_pandaai("get_market_data", symbol="M_DOMINANT.DCE")

        self.assertEqual(result, [{"provider_result": "real"}])
        self.assertEqual(len(calls), 2)

    def test_gateway_502_retries_beyond_previous_attempt_limit_until_success(self):
        fake, env_patch, module_patch = self._build_api()
        calls = []

        def get_market_data(**kwargs):
            calls.append(kwargs)
            if len(calls) <= 6:
                raise RuntimeError("HTTP 502: Bad Gateway")
            return [{"provider_result": "real"}]

        fake.get_market_data = get_market_data
        with env_patch, module_patch, patch("apis.pandaai.api.time.sleep"):
            api = PandaAIAPI()
            api._network_retry_initial_wait_seconds = 0.0
            api._network_retry_max_wait_seconds = 0.0
            api._wait_for_request_slot = lambda: None
            result = api._call_pandaai("get_market_data", symbol="M_DOMINANT.DCE")

        self.assertEqual(result, [{"provider_result": "real"}])
        self.assertEqual(len(calls), 7)

    def test_rate_limit_retries_beyond_previous_attempt_limit_until_success(self):
        fake, env_patch, module_patch = self._build_api()
        calls = []

        def get_market_data(**kwargs):
            calls.append(kwargs)
            if len(calls) <= 6:
                raise RuntimeError("500010 rate limit")
            return [{"provider_result": "real"}]

        fake.get_market_data = get_market_data
        with env_patch, module_patch, patch("apis.pandaai.api.time.sleep"):
            api = PandaAIAPI()
            api._retry_initial_wait_seconds = 0.0
            api._retry_max_wait_seconds = 0.0
            api._wait_for_request_slot = lambda: None
            result = api._call_pandaai("get_market_data", symbol="M_DOMINANT.DCE")

        self.assertEqual(result, [{"provider_result": "real"}])
        self.assertEqual(len(calls), 7)

    def test_documented_pandaai_transient_codes_retry_until_success(self):
        transient_cases = (
            (400002, "数据查询超时"),
            (500001, "请求频率超限"),
            (500002, "热点参数限流"),
            (500003, "IP请求频率超限"),
            (500004, "服务降级中"),
            (500005, "服务熔断中"),
            (500006, "并发请求数超限"),
            (900001, "系统异常，请稍后重试"),
        )
        for code, message in transient_cases:
            with self.subTest(code=code):
                self.setUp()
                fake, env_patch, module_patch = self._build_api()
                calls = []

                def get_market_data(**kwargs):
                    calls.append(kwargs)
                    if len(calls) <= 6:
                        raise FakeProviderError(message, code=code)
                    return [{"provider_result": "real"}]

                fake.get_market_data = get_market_data
                with env_patch, module_patch, patch("apis.pandaai.api.time.sleep"):
                    api = PandaAIAPI()
                    api._retry_initial_wait_seconds = 0.0
                    api._retry_max_wait_seconds = 0.0
                    api._network_retry_initial_wait_seconds = 0.0
                    api._network_retry_max_wait_seconds = 0.0
                    api._wait_for_request_slot = lambda: None
                    result = api._call_pandaai("get_market_data", symbol="M_DOMINANT.DCE")

                self.assertEqual(result, [{"provider_result": "real"}])
                self.assertEqual(len(calls), 7)

    def test_http_429_status_retries_until_success(self):
        fake, env_patch, module_patch = self._build_api()
        calls = []

        def get_market_data(**kwargs):
            calls.append(kwargs)
            if len(calls) <= 6:
                raise FakeProviderError("provider throttled", status_code=429)
            return [{"provider_result": "real"}]

        fake.get_market_data = get_market_data
        with env_patch, module_patch, patch("apis.pandaai.api.time.sleep"):
            api = PandaAIAPI()
            api._retry_initial_wait_seconds = 0.0
            api._retry_max_wait_seconds = 0.0
            api._wait_for_request_slot = lambda: None
            result = api._call_pandaai("get_market_data", symbol="M_DOMINANT.DCE")

        self.assertEqual(result, [{"provider_result": "real"}])
        self.assertEqual(len(calls), 7)

    def test_wrapped_connection_error_retries_until_success(self):
        fake, env_patch, module_patch = self._build_api()
        calls = []

        def get_market_data(**kwargs):
            calls.append(kwargs)
            if len(calls) <= 6:
                wrapped = RuntimeError("PandaAI SDK request failed")
                wrapped.__cause__ = ConnectionResetError("connection reset by peer")
                raise wrapped
            return [{"provider_result": "real"}]

        fake.get_market_data = get_market_data
        with env_patch, module_patch, patch("apis.pandaai.api.time.sleep"):
            api = PandaAIAPI()
            api._network_retry_initial_wait_seconds = 0.0
            api._network_retry_max_wait_seconds = 0.0
            api._wait_for_request_slot = lambda: None
            result = api._call_pandaai("get_market_data", symbol="M_DOMINANT.DCE")

        self.assertEqual(result, [{"provider_result": "real"}])
        self.assertEqual(len(calls), 7)

    def test_documented_non_transient_codes_do_not_retry(self):
        for code in (100000, 200001, 200101, 600001, 600002):
            with self.subTest(code=code):
                self.setUp()
                fake, env_patch, module_patch = self._build_api()
                calls = []

                def get_market_data(**kwargs):
                    calls.append(kwargs)
                    raise FakeProviderError("non-transient provider error", code=code)

                fake.get_market_data = get_market_data
                with env_patch, module_patch, patch("apis.pandaai.api.time.sleep"):
                    api = PandaAIAPI()
                    api._wait_for_request_slot = lambda: None
                    with self.assertRaises(FakeProviderError):
                        api._call_pandaai("get_market_data", symbol="M_DOMINANT.DCE")

                self.assertEqual(len(calls), 1)

    def test_gateway_503_and_504_share_existing_transient_retry_path(self):
        for error_text in (
            "HTTP 503: Service Unavailable",
            "HTTP 504: Gateway Timeout",
        ):
            with self.subTest(error_text=error_text):
                self.setUp()
                fake, env_patch, module_patch = self._build_api()
                calls = []

                def get_market_data(**kwargs):
                    calls.append(kwargs)
                    if len(calls) == 1:
                        raise RuntimeError(error_text)
                    return [{"provider_result": "real"}]

                fake.get_market_data = get_market_data
                with env_patch, module_patch, patch("apis.pandaai.api.time.sleep"):
                    api = PandaAIAPI()
                    api._network_retry_initial_wait_seconds = 0.0
                    api._network_retry_max_wait_seconds = 0.0
                    api._wait_for_request_slot = lambda: None
                    result = api._call_pandaai("get_market_data", symbol="M_DOMINANT.DCE")

                self.assertEqual(result, [{"provider_result": "real"}])
                self.assertEqual(len(calls), 2)

    def test_non_transient_client_errors_do_not_retry(self):
        for error_text in (
            "HTTP 401: authentication failed",
            "HTTP 403: permission denied",
            "authentication failed",
            "HTTP 400: invalid parameter",
        ):
            with self.subTest(error_text=error_text):
                self.setUp()
                fake, env_patch, module_patch = self._build_api()
                calls = []

                def get_market_data(**kwargs):
                    calls.append(kwargs)
                    raise RuntimeError(error_text)

                fake.get_market_data = get_market_data
                with env_patch, module_patch, patch("apis.pandaai.api.time.sleep"):
                    api = PandaAIAPI()
                    api._network_retry_initial_wait_seconds = 0.0
                    api._network_retry_max_wait_seconds = 0.0
                    api._wait_for_request_slot = lambda: None
                    with self.assertRaises(RuntimeError):
                        api._call_pandaai("get_market_data", symbol="M_DOMINANT.DCE")

                self.assertEqual(len(calls), 1)

    def test_daily_quota_exhaustion_stops_followup_provider_calls(self):
        fake, env_patch, module_patch = self._build_api()
        calls = []

        def get_future_basis(**kwargs):
            calls.append(kwargs)
            raise FakeProviderError("单日总流量超限", code=500009)

        fake.get_future_basis = get_future_basis
        with env_patch, module_patch:
            api = PandaAIAPI()
            api._wait_for_request_slot = lambda: None
            with self.assertRaisesRegex(PandaAIDailyQuotaExhausted, "pandaai_daily_quota_exhausted: 500009"):
                api._query_extra_data_with_diagnostic(
                    "get_future_basis",
                    underlying_symbol="BU",
                    start_date="20250806",
                    end_date="20250821",
                    fields=[],
                )
            with self.assertRaises(PandaAIDailyQuotaExhausted):
                api._query_extra_data_with_diagnostic(
                    "get_future_basis",
                    underlying_symbol="BU",
                    start_date="20250806",
                    end_date="20250821",
                    fields=[],
                )

        self.assertEqual(len(calls), 1)

    def test_daily_extra_prefetch_stops_after_first_quota_error(self):
        class QuotaRouter:
            def __init__(self):
                self.calls = []

            def get_pandaai_futures_extra_snapshot(self, **kwargs):
                self.calls.append(kwargs["underlying_code"])
                raise PandaAIDailyQuotaExhausted()

        router = QuotaRouter()
        config = {
            "runtime": {
                "data_cache": {
                    "enabled": True,
                    "prefetch_pandaai_market": False,
                    "prefetch_pandaai_extra": True,
                }
            },
            "pandaai_extra_data": {
                "enabled": True,
                "reference_lag_days": 1,
                "lookback_days": 5,
                "features": {"basis": True},
            },
        }
        with patch(
            "tools.agent_tools.analysis.analyst_data_usage.get_previous_trading_day",
            return_value=datetime(2025, 8, 21),
        ):
            with self.assertRaises(PandaAIDailyQuotaExhausted):
                prefetch_pandaai_daily_data(
                    router,
                    config,
                    ["BU", "C", "CF"],
                    datetime(2025, 8, 22),
                )

        self.assertEqual(router.calls, ["BU"])

    def test_short_zhengzhou_contract_code_expands_at_pandaai_boundary(self):
        fake, env_patch, module_patch = self._build_api()
        with env_patch, module_patch:
            api = PandaAIAPI()
            symbol = api._contract_symbol("cf601", reference_date=datetime(2025, 10, 13))

        self.assertEqual(symbol, "CF2601.CZC")

    def test_extra_snapshot_uses_pre_open_reference_date_and_selected_features(self):
        fake, env_patch, module_patch = self._build_api()
        with env_patch, module_patch:
            api = PandaAIAPI()
            snapshot = api.get_futures_extra_snapshot(
                underlying_code="M",
                reference_date=datetime(2025, 1, 3),
                lookback_days=5,
                contract_id="m2505",
                features={
                    "basis": True,
                    "warehouse_receipt": True,
                    "ls_ratio": True,
                },
            )

        self.assertEqual(snapshot["reference_date"], "2025-01-03")
        self.assertEqual(snapshot["contract_symbol"], "M2505.DCE")
        self.assertEqual(snapshot["record_counts"]["basis"], 1)
        self.assertEqual(snapshot["record_counts"]["warehouse_receipt"], 1)
        self.assertEqual(snapshot["record_counts"]["ls_ratio"], 1)
        basis_call = next(call for call in fake.calls if call["func"] == "get_future_basis")
        self.assertEqual(basis_call["end_date"], "20250103")
        self.assertTrue(any(call["func"] == "get_future_warehouse_receipt" for call in fake.calls))

    def test_extra_snapshot_maps_legacy_names_to_installed_sdk_names(self):
        fake, env_patch, module_patch = self._build_api()
        with env_patch, module_patch:
            api = PandaAIAPI()
            snapshot = api.get_futures_extra_snapshot(
                underlying_code="M",
                reference_date=datetime(2025, 1, 3),
                lookback_days=5,
                contract_id="m2505",
                features={
                    "warehouse_receipt": True,
                    "variety_position_rank": True,
                    "symbol_position_rank": True,
                    "broker_net_margin_change": True,
                    "broker_net_margin": True,
                    "net_cap_change": True,
                    "contract_daily_indicators": True,
                },
            )

        called = {call["func"] for call in fake.calls}
        self.assertIn("get_future_warehouse_receipt", called)
        self.assertIn("get_future_variety_posi", called)
        self.assertIn("get_future_symbol_posi", called)
        self.assertIn("get_broker_netmarg_change", called)
        self.assertIn("get_broker_netmarg", called)
        self.assertIn("get_future_netcap_change", called)
        self.assertIn("get_future_contract_indicators", called)
        self.assertEqual(snapshot["feature_status"]["warehouse_receipt"], "ok")
        self.assertEqual(snapshot["feature_diagnostics"]["warehouse_receipt"]["sdk_method"], "get_future_warehouse_receipt")
        self.assertEqual(snapshot["record_counts"]["variety_position_rank_long"], 1)
        self.assertEqual(snapshot["record_counts"]["symbol_position_rank_short"], 1)
        self.assertEqual(snapshot["record_counts"]["broker_net_margin_change"], 1)
        self.assertEqual(snapshot["record_counts"]["contract_daily_indicators"], 1)

    def test_contract_rank_extra_snapshot_supplies_required_rank_type(self):
        fake, env_patch, module_patch = self._build_api()
        with env_patch, module_patch:
            api = PandaAIAPI()
            snapshot = api.get_futures_extra_snapshot(
                underlying_code="M",
                reference_date=datetime(2025, 1, 3),
                lookback_days=5,
                contract_id="m2505",
                features={"contract_rank": True},
            )

        rank_call = next(call for call in fake.calls if call["func"] == "get_future_contract_rank")
        self.assertEqual(rank_call["rank_type"], "ratio")
        self.assertEqual(rank_call["type"], "")
        self.assertEqual(rank_call["max_rank"], 10)
        self.assertEqual(rank_call["symbol"], "")
        self.assertEqual(rank_call["underlying_symbol"], ["M"])
        self.assertEqual(snapshot["record_counts"]["contract_rank"], 1)
        self.assertEqual(snapshot["feature_status"]["contract_rank"], "ok")
        self.assertEqual(snapshot["feature_diagnostics"]["contract_rank"]["status"], "ok")

    def test_minute_bars_use_pandaai_minute_api_and_sort_by_datetime(self):
        fake, env_patch, module_patch = self._build_api()
        with env_patch, module_patch:
            api = PandaAIAPI()
            bars = api.get_futures_minute_bars(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 4),
                end_date=datetime(2025, 1, 4),
                frequency="15m",
            )

        self.assertEqual([bar["datetime"] for bar in bars], ["2025-01-04 10:00:00", "2025-01-04 10:15:00"])
        min_call = next(call for call in fake.calls if call["func"] == "get_market_min_data")
        self.assertEqual(min_call["symbol"], ["M_DOMINANT.DCE"])
        self.assertEqual(min_call["symbol_type"], "future")
        self.assertEqual(min_call["frequency"], "15m")

    def test_concrete_minute_bars_canonicalize_provider_codes_for_both_frequencies(self):
        cases = (
            ("SR", "SR2505", "SR2505.CZC", "sr505", "CZCE"),
            ("CF", "CF2505", "CF2505.CZC", "CF505", "CZCE"),
            ("TA", "TA2505", "TA2505.CZC", "TA505", "CZCE"),
            ("BU", "BU2506", "BU2506.SHF", "bu2506", "SHFE"),
            ("C", "C2505", "C2505.DCE", "c2505", "DCE"),
        )
        for frequency in ("15m", "1m"):
            for underlying, contract, provider_symbol, provider_code, exchange in cases:
                with self.subTest(frequency=frequency, contract=contract):
                    source_row = self._minute_row(
                        timestamp=datetime(2025, 3, 27, 10, 0),
                        trading_date="20250327",
                        symbol=provider_symbol,
                        trading_code=provider_code,
                        exchange=exchange,
                        underlying_code=underlying,
                        dominant_id=contract,
                    )
                    fake, source_rows, env_patch, module_patch = self._build_api_with_minute_rows(
                        [source_row]
                    )
                    with env_patch, module_patch:
                        bars = PandaAIAPI().get_futures_minute_bars(
                            contract_id=contract,
                            underlying_code=underlying,
                            is_main=0,
                            start_date=datetime(2025, 3, 27),
                            end_date=datetime(2025, 3, 27),
                            frequency=frequency,
                        )

                    minute_call = next(
                        call for call in fake.calls if call["func"] == "get_market_min_data"
                    )
                    self.assertEqual(minute_call["symbol"], [provider_symbol])
                    self.assertEqual(minute_call["frequency"], frequency)
                    self.assertEqual(len(bars), 1)
                    self.assertEqual(bars[0]["trading_code"], contract)
                    self.assertEqual(bars[0]["symbol"], provider_symbol)
                    self.assertEqual(bars[0]["dominant_id"], contract)
                    self.assertEqual(bars[0]["exchange"], exchange)
                    self.assertEqual(source_rows[0]["trading_code"], provider_code)

    def test_main_minute_bars_canonicalize_each_row_from_dominant_id(self):
        source_rows = [
            self._minute_row(
                timestamp=datetime(2025, 3, 27, 10, 0),
                trading_date="20250327",
                symbol="SR_DOMINANT.CZC",
                dominant_id="SR505",
                trading_code="SR506",
                exchange="CZCE",
                underlying_code="SR",
            ),
            self._minute_row(
                timestamp=datetime(2025, 3, 28, 10, 0),
                trading_date="20250328",
                symbol="SR_DOMINANT.CZC",
                dominant_id="SR2509",
                trading_code="SR509",
                exchange="CZCE",
                underlying_code="SR",
            ),
        ]
        fake, raw_rows, env_patch, module_patch = self._build_api_with_minute_rows(source_rows)
        with env_patch, module_patch:
            bars = PandaAIAPI().get_futures_minute_bars(
                underlying_code="SR",
                is_main=1,
                start_date=datetime(2025, 3, 27),
                end_date=datetime(2025, 3, 28),
                frequency="15m",
            )

        minute_call = next(call for call in fake.calls if call["func"] == "get_market_min_data")
        self.assertEqual(minute_call["symbol"], ["SR_DOMINANT.CZC"])
        self.assertEqual([bar["trading_code"] for bar in bars], ["SR2505", "SR2509"])
        self.assertEqual([bar["dominant_id"] for bar in bars], ["SR505", "SR2509"])
        self.assertEqual([bar["symbol"] for bar in bars], ["SR_DOMINANT.CZC"] * 2)
        self.assertEqual([row["trading_code"] for row in raw_rows], ["SR506", "SR509"])

    def test_czce_night_minute_contract_expansion_uses_logical_trading_date(self):
        cases = (
            (datetime(2019, 3, 26, 21, 0), "20190327", "SR1905"),
            (datetime(2029, 3, 26, 21, 0), "20290327", "SR2905"),
        )
        for timestamp, logical_date, expected_contract in cases:
            with self.subTest(logical_date=logical_date):
                PandaAIAPI._shared_minute_cache.clear()
                source_row = self._minute_row(
                    timestamp=timestamp,
                    trading_date=logical_date,
                    symbol="",
                    dominant_id="",
                    trading_code="SR905",
                    exchange="CZCE",
                    underlying_code="SR",
                )
                fake, raw_rows, env_patch, module_patch = self._build_api_with_minute_rows(
                    [source_row]
                )
                logical_datetime = datetime.strptime(logical_date, "%Y%m%d")
                with env_patch, module_patch:
                    bars = PandaAIAPI().get_futures_minute_bars(
                        contract_id=expected_contract,
                        underlying_code="SR",
                        is_main=0,
                        start_date=logical_datetime,
                        end_date=logical_datetime,
                        frequency="1m",
                    )

                minute_call = next(
                    call for call in fake.calls if call["func"] == "get_market_min_data"
                )
                self.assertEqual(minute_call["symbol"], [f"{expected_contract}.CZC"])
                self.assertEqual(len(bars), 1)
                self.assertEqual(bars[0]["trading_code"], expected_contract)
                self.assertEqual(bars[0]["trading_date"], logical_date)
                self.assertEqual(bars[0]["datetime"], timestamp.strftime("%Y-%m-%d %H:%M:%S"))
                self.assertEqual(raw_rows[0]["trading_code"], "SR905")

    def test_minute_contract_identity_errors_are_not_silently_filtered(self):
        cases = (
            (
                "SR",
                "SR2505",
                self._minute_row(
                    timestamp=datetime(2025, 3, 27, 10, 0),
                    trading_date="20250327",
                    symbol="SR2506.CZC",
                    trading_code="SR506",
                    exchange="CZCE",
                    underlying_code="SR",
                ),
                "contract mismatch",
            ),
            (
                "BU",
                "BU2506",
                self._minute_row(
                    timestamp=datetime(2025, 3, 27, 10, 0),
                    trading_date="20250327",
                    symbol="",
                    trading_code="BU506",
                    exchange="SHFE",
                    underlying_code="BU",
                ),
                "three-digit",
            ),
            (
                "M",
                "M2505",
                self._minute_row(
                    timestamp=datetime(2025, 3, 27, 10, 0),
                    trading_date="20250327",
                    symbol="",
                    trading_code="M505",
                    exchange="DCE",
                    underlying_code="M",
                ),
                "three-digit",
            ),
        )
        for underlying, contract, source_row, error_pattern in cases:
            with self.subTest(contract=contract, provider_code=source_row["trading_code"]):
                PandaAIAPI._shared_minute_cache.clear()
                _fake, _rows, env_patch, module_patch = self._build_api_with_minute_rows(
                    [source_row]
                )
                with env_patch, module_patch:
                    with self.assertRaisesRegex(ValueError, error_pattern):
                        PandaAIAPI().get_futures_minute_bars(
                            contract_id=contract,
                            underlying_code=underlying,
                            is_main=0,
                            start_date=datetime(2025, 3, 27),
                            end_date=datetime(2025, 3, 27),
                            frequency="15m",
                        )

    def test_concrete_minute_true_provider_empty_remains_empty(self):
        fake, _source_rows, env_patch, module_patch = self._build_api_with_minute_rows([])
        with env_patch, module_patch:
            bars = PandaAIAPI().get_futures_minute_bars(
                contract_id="SR2505",
                underlying_code="SR",
                is_main=0,
                start_date=datetime(2025, 3, 27),
                end_date=datetime(2025, 3, 27),
                frequency="15m",
            )

        minute_call = next(call for call in fake.calls if call["func"] == "get_market_min_data")
        self.assertEqual(minute_call["symbol"], ["SR2505.CZC"])
        self.assertEqual(bars, [])

    def test_minute_bars_filter_by_logical_trading_date_not_physical_night_date(self):
        fake, env_patch, module_patch = self._build_api()

        def get_market_min_data(**kwargs):
            fake.calls.append({"func": "get_market_min_data", **kwargs})
            return [
                {
                    "date": "20250103",
                    "minute": "210000",
                    "datetime": "2025-01-03 21:00:00",
                    "symbol": "M_DOMINANT.DCE",
                    "trading_code": "M2505",
                    "open": 3200.0,
                    "high": 3210.0,
                    "low": 3190.0,
                    "close": 3205.0,
                    "volume": 10,
                    "trading_date": "20250104",
                },
                {
                    "date": "20250104",
                    "minute": "100000",
                    "datetime": "2025-01-04 10:00:00",
                    "symbol": "M_DOMINANT.DCE",
                    "trading_code": "M2505",
                    "open": 3210.0,
                    "high": 3220.0,
                    "low": 3200.0,
                    "close": 3215.0,
                    "volume": 10,
                    "trading_date": "20250103",
                },
            ]

        fake.get_market_min_data = get_market_min_data
        with env_patch, module_patch:
            api = PandaAIAPI()
            bars = api.get_futures_minute_bars(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 4),
                end_date=datetime(2025, 1, 4),
                frequency="15m",
            )

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["datetime"], "2025-01-03 21:00:00")
        self.assertEqual(bars[0]["trading_date"], "20250104")

    def test_exact_minute_cache_key_avoids_duplicate_provider_request(self):
        fake, env_patch, module_patch = self._build_api()
        with env_patch, module_patch:
            api = PandaAIAPI()
            first = api.get_futures_minute_bars(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 4),
                end_date=datetime(2025, 1, 4),
                frequency="15m",
            )
            second = api.get_futures_minute_bars(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 4),
                end_date=datetime(2025, 1, 4),
                frequency="15m",
            )
            api.get_futures_minute_bars(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 4),
                end_date=datetime(2025, 1, 4),
                frequency="1m",
            )

        minute_calls = [call for call in fake.calls if call["func"] == "get_market_min_data"]
        self.assertEqual(first, second)
        self.assertEqual(len(minute_calls), 2)
        self.assertEqual({call["frequency"] for call in minute_calls}, {"15m", "1m"})

    def test_live_minute_refresh_reads_new_bars_on_each_cutoff(self):
        fake, env_patch, module_patch = self._build_api()
        calls = []

        def minute_row(timestamp, close):
            return {
                "date": "20250104",
                "minute": timestamp.strftime("%H%M%S"),
                "datetime": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": "M_DOMINANT.DCE",
                "trading_code": "M2505",
                "underlying_symbol": "M",
                "exchange": "DCE",
                "open": close - 1.0,
                "high": close + 1.0,
                "low": close - 2.0,
                "close": close,
                "volume": 10,
                "trading_date": "20250104",
            }

        first_bar = minute_row(datetime(2025, 1, 4, 10, 0), 3200.0)
        second_bar = minute_row(datetime(2025, 1, 4, 10, 15), 3210.0)

        def get_market_min_data(**kwargs):
            calls.append(kwargs)
            return [first_bar] if len(calls) == 1 else [first_bar, second_bar]

        fake.get_market_min_data = get_market_min_data
        with env_patch, module_patch:
            api = PandaAIAPI()
            first = api.get_futures_minute_bars(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 4),
                end_date=datetime(2025, 1, 4),
                frequency="15m",
                cutoff_datetime=datetime(2025, 1, 4, 10, 5),
            )
            second = api.get_futures_minute_bars(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 4),
                end_date=datetime(2025, 1, 4),
                frequency="15m",
                cutoff_datetime=datetime(2025, 1, 4, 10, 20),
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual([bar["datetime"] for bar in first], ["2025-01-04 10:00:00"])
        self.assertEqual(
            [bar["datetime"] for bar in second],
            ["2025-01-04 10:00:00", "2025-01-04 10:15:00"],
        )

    def test_live_empty_refresh_does_not_return_or_replace_cached_bars(self):
        fake, env_patch, module_patch = self._build_api()
        calls = []
        cached_bar = {
            "date": "20250104",
            "minute": "100000",
            "datetime": "2025-01-04 10:00:00",
            "symbol": "M_DOMINANT.DCE",
            "trading_code": "M2505",
            "underlying_symbol": "M",
            "exchange": "DCE",
            "open": 3199.0,
            "high": 3201.0,
            "low": 3198.0,
            "close": 3200.0,
            "volume": 10,
            "trading_date": "20250104",
        }

        def get_market_min_data(**kwargs):
            calls.append(kwargs)
            return [cached_bar] if len(calls) == 1 else []

        fake.get_market_min_data = get_market_min_data
        with env_patch, module_patch:
            api = PandaAIAPI()
            first = api.get_futures_minute_bars(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 4),
                end_date=datetime(2025, 1, 4),
                frequency="15m",
                cutoff_datetime=datetime(2025, 1, 4, 10, 5),
            )
            empty_refresh = api.get_futures_minute_bars(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 4),
                end_date=datetime(2025, 1, 4),
                frequency="15m",
                cutoff_datetime=datetime(2025, 1, 4, 10, 20),
            )
            cached_replay = api.get_futures_minute_bars(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 4),
                end_date=datetime(2025, 1, 4),
                frequency="15m",
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(first), 1)
        self.assertEqual(empty_refresh, [])
        self.assertEqual(cached_replay, first)

    def test_live_failed_refresh_does_not_return_or_replace_cached_bars(self):
        fake, env_patch, module_patch = self._build_api()
        calls = []
        cached_bar = {
            "date": "20250104",
            "minute": "100000",
            "datetime": "2025-01-04 10:00:00",
            "symbol": "M_DOMINANT.DCE",
            "trading_code": "M2505",
            "underlying_symbol": "M",
            "exchange": "DCE",
            "open": 3199.0,
            "high": 3201.0,
            "low": 3198.0,
            "close": 3200.0,
            "volume": 10,
            "trading_date": "20250104",
        }

        def get_market_min_data(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return [cached_bar]
            raise ValueError("invalid minute request")

        fake.get_market_min_data = get_market_min_data
        with env_patch, module_patch:
            api = PandaAIAPI()
            first = api.get_futures_minute_bars(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 4),
                end_date=datetime(2025, 1, 4),
                frequency="15m",
                cutoff_datetime=datetime(2025, 1, 4, 10, 5),
            )
            with self.assertRaisesRegex(ValueError, "invalid minute request"):
                api.get_futures_minute_bars(
                    underlying_code="M",
                    is_main=1,
                    start_date=datetime(2025, 1, 4),
                    end_date=datetime(2025, 1, 4),
                    frequency="15m",
                    cutoff_datetime=datetime(2025, 1, 4, 10, 20),
                )
            cached_replay = api.get_futures_minute_bars(
                underlying_code="M",
                is_main=1,
                start_date=datetime(2025, 1, 4),
                end_date=datetime(2025, 1, 4),
                frequency="15m",
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(cached_replay, first)

    def test_persistent_market_cache_exact_hit_avoids_provider_request(self):
        fake, env_patch, module_patch = self._build_api()
        market_calls = []

        def get_market_data(**kwargs):
            market_calls.append(kwargs)
            return [
                {
                    "date": "20250103",
                    "symbol": kwargs["symbol"],
                    "trading_code": "M2505",
                    "underlying_symbol": "M",
                    "open": 3100.0,
                    "close": 3140.0,
                    "settlement": 3130.0,
                }
            ]

        fake.get_market_data = get_market_data
        with tempfile.TemporaryDirectory(
            prefix="agentquant_pandaai_market_cache_",
            ignore_cleanup_errors=True,
        ) as tmpdir:
            env = {
                "PANDAAI_PERSISTENT_MARKET_CACHE": "1",
                "PANDAAI_MARKET_CACHE_DB": str(Path(tmpdir) / "market.db"),
            }
            with env_patch, module_patch, patch.dict(os.environ, env, clear=False):
                api = PandaAIAPI()
                first = api._query_market_data(
                    "M_DOMINANT.DCE",
                    datetime(2025, 1, 3),
                    datetime(2025, 1, 3),
                )
                PandaAIAPI._shared_history_cache.clear()
                second = PandaAIAPI()._query_market_data(
                    "M_DOMINANT.DCE",
                    datetime(2025, 1, 3),
                    datetime(2025, 1, 3),
                )

        self.assertEqual(first, second)
        self.assertEqual(len(market_calls), 1)

    def test_persistent_extra_cache_exact_hit_avoids_provider_request(self):
        fake, env_patch, module_patch = self._build_api()
        with tempfile.TemporaryDirectory(
            prefix="agentquant_pandaai_extra_cache_",
            ignore_cleanup_errors=True,
        ) as tmpdir:
            env = {
                "PANDAAI_PERSISTENT_MARKET_CACHE": "1",
                "PANDAAI_MARKET_CACHE_DB": str(Path(tmpdir) / "market.db"),
            }
            kwargs = {
                "underlying_symbol": "BU",
                "start_date": "20250806",
                "end_date": "20250821",
                "fields": [],
            }
            with env_patch, module_patch, patch.dict(os.environ, env, clear=False):
                api = PandaAIAPI()
                first = api._query_extra_data_with_diagnostic("get_future_basis", **kwargs)
                PandaAIAPI._shared_extra_cache.clear()
                PandaAIAPI._shared_extra_diagnostics_cache.clear()
                second = PandaAIAPI()._query_extra_data_with_diagnostic("get_future_basis", **kwargs)

        basis_calls = [call for call in fake.calls if call["func"] == "get_future_basis"]
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(len(basis_calls), 1)

    def test_persistent_extra_cache_reuses_deterministic_empty_response(self):
        fake, env_patch, module_patch = self._build_api()
        basis_calls = []

        def get_future_basis(**kwargs):
            basis_calls.append(kwargs)
            return []

        fake.get_future_basis = get_future_basis
        with tempfile.TemporaryDirectory(
            prefix="agentquant_pandaai_empty_extra_cache_",
            ignore_cleanup_errors=True,
        ) as tmpdir:
            env = {
                "PANDAAI_PERSISTENT_MARKET_CACHE": "1",
                "PANDAAI_MARKET_CACHE_DB": str(Path(tmpdir) / "market.db"),
            }
            kwargs = {
                "underlying_symbol": "BU",
                "start_date": "20250806",
                "end_date": "20250821",
                "fields": [],
            }
            with env_patch, module_patch, patch.dict(os.environ, env, clear=False):
                first = PandaAIAPI()._query_extra_data_with_diagnostic("get_future_basis", **kwargs)
                PandaAIAPI._shared_extra_cache.clear()
                PandaAIAPI._shared_extra_diagnostics_cache.clear()
                second = PandaAIAPI()._query_extra_data_with_diagnostic("get_future_basis", **kwargs)

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "no_data")
        self.assertEqual(len(basis_calls), 1)

    def test_invalid_or_empty_persistent_cache_is_not_returned_as_data(self):
        invalid_cases = (
            [],
            [{"date": "20250104", "symbol": "M_DOMINANT.DCE"}],
            [{"date": "20250103", "symbol": "RB_DOMINANT.SHF"}],
        )
        for invalid_records in invalid_cases:
            with self.subTest(invalid_records=invalid_records):
                self.setUp()
                fake, env_patch, module_patch = self._build_api()
                market_calls = []

                def get_market_data(**kwargs):
                    market_calls.append(kwargs)
                    return [
                        {
                            "date": "20250103",
                            "symbol": kwargs["symbol"],
                            "trading_code": "M2505",
                            "underlying_symbol": "M",
                            "open": 3100.0,
                            "close": 3140.0,
                            "settlement": 3130.0,
                        }
                    ]

                fake.get_market_data = get_market_data
                with tempfile.TemporaryDirectory(
                    prefix="agentquant_pandaai_bad_cache_",
                    ignore_cleanup_errors=True,
                ) as tmpdir:
                    env = {
                        "PANDAAI_PERSISTENT_MARKET_CACHE": "1",
                        "PANDAAI_MARKET_CACHE_DB": str(Path(tmpdir) / "market.db"),
                    }
                    with env_patch, module_patch, patch.dict(os.environ, env, clear=False):
                        api = PandaAIAPI()
                        api._write_persistent_market_cache(
                            "M_DOMINANT.DCE",
                            "20250103",
                            "20250103",
                            invalid_records,
                        )
                        PandaAIAPI._shared_history_cache.clear()
                        rows = api._query_market_data(
                            "M_DOMINANT.DCE",
                            datetime(2025, 1, 3),
                            datetime(2025, 1, 3),
                        )

                self.assertEqual(len(market_calls), 1)
                self.assertEqual(rows[0]["symbol"], "M_DOMINANT.DCE")
                self.assertEqual(rows[0]["date"], "20250103")


if __name__ == "__main__":
    unittest.main()
