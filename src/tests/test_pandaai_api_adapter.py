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

from apis.pandaai.api import PandaAIAPI


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
        self.assertEqual(quotes[-1].ticker, "m2505")
        self.assertEqual(quotes[-1].open_price, 3110.0)
        self.assertEqual(quotes[-1].settle_price, 3130.0)
        self.assertEqual(quotes[-1].pre_settle_price, 3090.0)
        self.assertEqual(
            next(call for call in fake.calls if call["func"] == "get_market_data")["symbol"],
            "M_DOMINANT.DCE",
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
        self.assertEqual(quote.ticker, "m2505")
        self.assertFalse(any(call["func"] == "get_future_detail" for call in fake.calls))

    def test_market_data_call_does_not_log_provider_request_or_result_details(self):
        fake, env_patch, module_patch = self._build_api()
        with env_patch, module_patch, patch("apis.pandaai.api.logger") as logger_mock:
            api = PandaAIAPI()
            api.get_main_contract_quote_on_date("M", datetime(2025, 1, 4))

        logger_mock.info.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
