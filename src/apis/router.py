"""Router for APIs"""

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from apis.contract_info_cache import FuturesContractInfoCache
from util.logger import logger
from util.text_sanitize import sanitize_visible_text
from util.trading_calendar import get_previous_trading_day
from tools.agent_tools.analysis.analyst_data_usage import read_finoview_feather_cached, read_text_cached

try:
    import pandas as pd
except Exception:  # pragma: no cover - depends on runtime environment
    pd = None

class APISource:
    PANDAAI = "pandaai"

class Router():
    """Router for APIs"""

    def __init__(self, source: APISource, market_type: str = "china_futures", config: Optional[Dict[str, Any]] = None):
        """
        Initialize Router with API source and market type.

        Args:
            source: API source. AgentQuant currently supports PandaAI for futures market data.
            market_type: Current runtime market type. AgentQuant now runs in china_futures mode.
            config: Optional runtime config; used to resolve local Finoview/news paths.
        """
        self.market_type = market_type
        self.config = config or {}
        if source == APISource.PANDAAI:
            from apis.pandaai import PandaAIAPI
            self.api = PandaAIAPI()
        else:
            raise ValueError(f"Unsupported API source for AgentQuant futures mode: {source}")
        self.last_fundamentals_metadata = None
        self.last_news_metadata = None

    def _project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _resolve_project_path(self, path_text: Optional[str], default_relative: str) -> Path:
        raw = Path(path_text or default_relative)
        if raw.is_absolute():
            return raw
        return self._project_root() / raw

    def _require_pandas(self, feature_name: str) -> None:
        if pd is not None:
            return

        raise RuntimeError(
            f"{feature_name}: pandas_runtime_dependency_unavailable"
        )
    
    def get_market_news(self, topic, trading_date, news_count):
        raise RuntimeError(
            "Router.get_market_news is retired. Futures news is loaded from local Finoview txt files."
        )
    
    def get_us_economic_indicators(self):
        raise RuntimeError(
            "Router.get_us_economic_indicators is retired. AgentQuant futures mode uses PandaAI and local Finoview data only."
        )

    # China futures helpers backed by the configured futures market data provider.

    def get_china_futures_contracts(self, exchange=None, underlying_code=None):
        """Get the list of China futures contracts."""
        return self.api.get_china_futures_contracts(exchange, underlying_code)

    def get_china_futures_daily_candles(self, contract_id, start_date, end_date):
        """Get China futures daily candles."""
        return self.api.get_futures_daily_candles(
            contract_id=contract_id,
            start_date=start_date,
            end_date=end_date,
        )

    def get_china_futures_daily_candles_df(self, contract_id, start_date, end_date):
        """Get China futures daily candles as a DataFrame."""
        return self.api.get_futures_daily_candles_df(
            contract_id=contract_id,
            start_date=start_date,
            end_date=end_date,
        )

    def get_china_futures_minute_bars(
        self,
        contract_id=None,
        underlying_code=None,
        is_main=0,
        start_date=None,
        end_date=None,
        frequency="15m",
        time_zone=None,
        cutoff_datetime=None,
    ):
        """Get China futures minute bars from the configured futures provider."""
        if not hasattr(self.api, "get_futures_minute_bars"):
            raise RuntimeError("configured API does not expose futures minute bars")
        return self.api.get_futures_minute_bars(
            contract_id=contract_id,
            underlying_code=underlying_code,
            is_main=is_main,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            time_zone=time_zone,
            cutoff_datetime=cutoff_datetime,
        )

    def get_china_futures_main_contract(self, underlying_code):
        """Get the main futures contract for an underlying code."""
        return self.api.get_main_contract(underlying_code)

    def get_china_futures_last_close_price(self, contract_id, trading_date):
        """Get the last close price for a futures contract."""
        return self.api.get_last_close_price(contract_id, trading_date)

    def get_china_futures_continuous_candles(self, underlying_code, contract_type="main", start_date=None, end_date=None):
        """Get continuous futures candles."""
        return self.api.get_continuous_candles(
            underlying_code=underlying_code,
            start_date=start_date,
            end_date=end_date,
        )

    def get_china_futures_margin(self, contract_id):
        """Get futures margin data for a contract."""
        return self.api.get_futures_margin(contract_id)

    # Generic data access helpers that adapt to stocks and futures.

    def _normalize_trading_date(self, trading_date):
        if isinstance(trading_date, datetime):
            return trading_date
        return datetime.strptime(str(trading_date), "%Y-%m-%d")

    def get_futures_contract_quote_on_date(self, contract_code, trading_date):
        """Get a concrete futures contract quote on the exact trading date."""
        normalized_date = self._normalize_trading_date(trading_date)
        return self.api.get_futures_quote_on_date(
            trading_date=normalized_date,
            contract_id=contract_code,
        )

    def get_futures_main_contract_quote_on_date(self, underlying_code, trading_date):
        """Get the main-contract quote on the exact trading date."""
        normalized_date = self._normalize_trading_date(trading_date)
        return self.api.get_main_contract_quote_on_date(
            underlying_code=underlying_code,
            trading_date=normalized_date,
        )

    def get_pandaai_futures_extra_snapshot(
        self,
        underlying_code,
        reference_date,
        lookback_days=5,
        contract_id=None,
        features=None,
    ):
        """Get optional PandaAI non-market futures data for pre-open confirmation."""
        normalized_date = self._normalize_trading_date(reference_date)
        if not hasattr(self.api, "get_futures_extra_snapshot"):
            return {
                "underlying_code": underlying_code,
                "reference_date": normalized_date.strftime("%Y-%m-%d"),
                "lookback_days": int(lookback_days),
                "records": {},
                "record_counts": {},
                "errors": ["configured API does not expose PandaAI extra futures data"],
            }
        return self.api.get_futures_extra_snapshot(
            underlying_code=underlying_code,
            reference_date=normalized_date,
            lookback_days=lookback_days,
            contract_id=contract_id,
            features=features,
        )

    def resolve_morning_execution_base_price(self, underlying_code, trading_date, contract_code=None):
        """
        Resolve phase2 execution basis strictly as:
        T-day open -> skip.

        Phase2 execution must not use the previous close to fabricate an
        executable intraday price. The T-1 close is valid for Phase1 planning,
        but a missing T-day open is a data problem that Trader must record and
        skip instead of hiding behind a fallback price.
        """
        from graph.schema import MorningExecutionBasis, BasePriceSource

        normalized_date = self._normalize_trading_date(trading_date)
        warning_messages = []

        today_quote = None
        try:
            if contract_code:
                today_quote = self.get_futures_contract_quote_on_date(contract_code, normalized_date)
            if today_quote is None:
                today_quote = self.get_futures_main_contract_quote_on_date(underlying_code, normalized_date)
        except Exception as exc:
            self._append_provider_warning(
                underlying_code=underlying_code,
                trading_date=normalized_date,
                operation="T-day open quote",
                exc=exc,
                warning_messages=warning_messages,
            )

        open_price = today_quote.open_price if today_quote is not None else None
        if open_price is not None and open_price > 0:
            return MorningExecutionBasis(
                base_price=open_price,
                base_price_source=BasePriceSource.T_OPEN,
                base_price_date=normalized_date.strftime("%Y-%m-%d"),
                open_price=open_price,
                prev_close_price=today_quote.pre_close_price if today_quote is not None else None,
                warning_message=None,
            )

        missing_open_message = f"{underlying_code} missing T-day open price on {normalized_date.strftime('%Y-%m-%d')}"
        logger.warning(missing_open_message)
        warning_messages.append(missing_open_message)
        return MorningExecutionBasis(
            base_price=None,
            base_price_source=None,
            base_price_date=None,
            open_price=open_price,
            prev_close_price=today_quote.pre_close_price if today_quote is not None else None,
            warning_message="; ".join(warning_messages),
        )

    def resolve_pre_open_reference_price(self, underlying_code, trading_date, contract_code=None):
        """
        Resolve the pre-open planning reference price strictly as:
        previous trading day's close -> skip.
        """
        from graph.schema import MorningExecutionBasis, BasePriceSource

        normalized_date = self._normalize_trading_date(trading_date)
        warning_messages = []

        prev_quote, previous_trading_day = self._resolve_previous_close_quote(
            underlying_code=underlying_code,
            trading_date=normalized_date,
            contract_code=contract_code,
            warning_messages=warning_messages,
        )
        if prev_quote is not None and prev_quote.close_price is not None and prev_quote.close_price > 0:
            visible_contract_code = str(getattr(prev_quote, "ticker", "") or "").strip().upper()
            if not visible_contract_code or not any(char.isdigit() for char in visible_contract_code):
                return self._build_missing_previous_close_basis(
                    underlying_code=underlying_code,
                    normalized_date=normalized_date,
                    open_price=None,
                    warning_messages=[*warning_messages, "concrete_contract_code_unavailable"],
                )
            static_info = FuturesContractInfoCache.get_contract_info(underlying_code)
            contract_facts = {
                "contract_code": visible_contract_code,
                "underlying_code": str(underlying_code).upper(),
                "as_of_date": previous_trading_day.strftime("%Y-%m-%d"),
                "source": (
                    "pandaai_concrete_contract_quote"
                    if contract_code
                    else "pandaai_main_contract_quote"
                ),
                "exchange": getattr(prev_quote, "exchange_cd", None),
                "main_contract": bool(getattr(prev_quote, "main_con", 0)) if not contract_code else False,
            }
            if isinstance(static_info, dict):
                for field in (
                    "contract_multiplier",
                    "margin_rate_long",
                    "margin_rate_short",
                    "minimum_tick",
                    "contract_type",
                ):
                    if static_info.get(field) is not None:
                        contract_facts[field] = static_info[field]
            return MorningExecutionBasis(
                base_price=prev_quote.close_price,
                base_price_source=BasePriceSource.T_MINUS_1_CLOSE_FALLBACK,
                base_price_date=previous_trading_day.strftime("%Y-%m-%d"),
                open_price=None,
                prev_close_price=prev_quote.close_price,
                contract_code=visible_contract_code,
                contract_facts=contract_facts,
                warning_message="; ".join(warning_messages) if warning_messages else None,
            )

        return self._build_missing_previous_close_basis(
            underlying_code=underlying_code,
            normalized_date=normalized_date,
            open_price=None,
            warning_messages=warning_messages,
        )

    def _resolve_previous_close_quote(self, underlying_code, trading_date, contract_code=None, warning_messages=None):
        try:
            previous_trading_day = get_previous_trading_day(
                router=self,
                trading_date=trading_date,
                underlying_code=underlying_code,
            )
        except Exception as exc:
            self._append_provider_warning(
                underlying_code=underlying_code,
                trading_date=trading_date,
                operation="previous trading day lookup",
                exc=exc,
                warning_messages=warning_messages,
            )
            return None, None

        try:
            if contract_code:
                return (
                    self.get_futures_contract_quote_on_date(contract_code, previous_trading_day),
                    previous_trading_day,
                )
            return (
                self.get_futures_main_contract_quote_on_date(underlying_code, previous_trading_day),
                previous_trading_day,
            )
        except Exception as exc:
            self._append_provider_warning(
                underlying_code=underlying_code,
                trading_date=previous_trading_day,
                operation="previous close quote",
                exc=exc,
                warning_messages=warning_messages,
            )
            return None, previous_trading_day

    def _append_provider_warning(self, underlying_code, trading_date, operation, exc, warning_messages=None):
        normalized_date = self._normalize_trading_date(trading_date) if trading_date is not None else None
        date_text = normalized_date.strftime("%Y-%m-%d") if normalized_date is not None else "unknown"
        message = f"{underlying_code} {operation} provider unavailable on {date_text}"
        logger.warning(message)
        if warning_messages is not None:
            warning_messages.append(message)

    def _build_missing_previous_close_basis(self, underlying_code, normalized_date, open_price, warning_messages):
        from graph.schema import MorningExecutionBasis

        missing_prev_close_message = (
            f"{underlying_code} has no previous close available before {normalized_date.strftime('%Y-%m-%d')}"
        )
        logger.warning(missing_prev_close_message)
        warning_messages.append(missing_prev_close_message)
        return MorningExecutionBasis(
            base_price=None,
            base_price_source=None,
            base_price_date=None,
            open_price=open_price,
            prev_close_price=None,
            warning_message="; ".join(warning_messages),
        )

    def get_daily_candles_df(self, ticker, trading_date):
        """
        Return a China-futures continuous daily-candle DataFrame.

        Args:
            ticker: China futures underlying code.
            trading_date: End date for the candle request.

        Returns:
            A pandas DataFrame containing the continuous OHLCV history.
        """
        if self.market_type != "china_futures":
            raise RuntimeError(
                f"Router.get_daily_candles_df() only supports china_futures, got market_type={self.market_type!r}"
            )

        return self.api.get_continuous_candles_df(
            underlying_code=ticker,
            end_date=trading_date
        )

    def get_last_close_price(self, ticker, trading_date):
        """
        Return the latest China-futures close available on or before the requested date.

        Args:
            ticker: China futures underlying code.
            trading_date: Date used to resolve the last available close.

        Returns:
            The resolved close price, or None when no quote is available.
        """
        if self.market_type != "china_futures":
            raise RuntimeError(
                f"Router.get_last_close_price() only supports china_futures, got market_type={self.market_type!r}"
            )

        quotes = self.api.get_continuous_candles(
            underlying_code=ticker,
            end_date=trading_date
        )
        return quotes[-1].close if quotes else None

    def get_china_futures_fundamentals(self, ticker, trading_date):
        """
        Load local China-futures fundamental indicators and format them for downstream use.

        Args:
            ticker: China futures underlying code such as "RB", "M", or "TA".
            trading_date: Trading date used to filter the latest available indicator values.

        Returns:
            A formatted analysis string, or None when no local data is configured or available.
        """
        self._require_pandas("Local futures fundamental loading")

        import os

        # Fundamental inputs are read from local Finoview feather files.
        factor_cfg = (self.config.get("factor_data") or {}) if isinstance(self.config, dict) else {}
        data_dir = self._resolve_project_path(
            factor_cfg.get("data_dir"),
            "data/Fundamental_data/Finoview_data",
        )
        catalog_path = self._resolve_project_path(
            factor_cfg.get("catalog_path"),
            "src/config/finoview_factor_catalog.yaml",
        )
        finoview_enabled = bool(factor_cfg.get("finoview_enabled", True))

        # Current 15-symbol fundamental coverage backed by Finoview feather files.
        indicator_map = {
            "BU": {
                "spot_price": "bu_spot_price",
                "spot_price_shandong": "bu_spot_price_shandong",
                "futures_close_price": "bu_future_close_price",
                "shandong_profit": "bu_profit_shandong",
                "social_inventory": "bu_social_stock",
                "company_social_inventory": "bu_social_stock_company",
                "factory_inventory": "bu_factory_stock",
                "refinery_inventory": "bu_refinery_stock",
                "futures_inventory": "bu_future_stock",
                "demand": "bu_demand",
                "shipment": "bu_shipment",
                "yield": "bu_yield",
                "shandong_operating_rate": "bu_operate_rate_shandong",
            },
            "C": {
                "spot_price": "c_spot_price",
                "trade_profit": "c_trade_profit",
                "sales_progress": "c_sales_progress",
                "import_profit_quota": "c_jk_profit_quota",
                "import_profit_us_west": "c_jk_profit",
                "alcohol_operating_rate": "c_alcohol_operate_rate",
                "alcohol_profit": "c_alcohol_profit",
                "deep_processing_demand": "c_processing_demand",
                "deep_processing_inventory": "c_processing_stock",
                "feed_enterprise_stock_days": "c_feed_stock_days",
                "port_inventory": "c_port_stock",
                "feed_output": "c_feed_yield",
                "sorghum_spot_price": "c_sorghum_spot_price",
                "barley_spot_price": "c_barley_spot_price",
                "import_volume": "c_jk_volume",
                "us_net_sales": "c_trade_volume_usa",
                "starch_spot_price": "cs_spot_price",
                "starch_profit_heilongjiang": "cs_profit_heilongjiang",
                "starch_profit_shandong": "cs_profit_shandong",
                "starch_shipment": "cs_shipment",
                "starch_factory_inventory": "cs_factory_stock",
                "starch_processing_volume": "cs_processing_volume",
                "starch_operating_rate": "cs_operate_rate",
            },
            "CF": {
                "spot_price": "cf_spot_price",
                "cotton_price_index": "cf_spot_price_index",
                "profit": "cf_profit",
                "processing_profit": "cf_processing_profit",
                "basis_spread": "cf_spread",
                "commercial_inventory": "cf_b_stock",
                "industrial_inventory": "cf_i_stock",
                "port_inventory": "cf_port_stock",
                "textile_raw_material_inventory": "cf_textile_ms_stock",
                "textile_finished_goods_inventory": "cf_textile_stock",
                "trade_volume": "cf_trade_volume",
                "market_trade_volume": "cf_market_trade_volume",
                "freight_fee": "cf_freight_fee",
                "us_weekly_exports": "cf_ck_volume_usa",
                "us_weekly_exports_to_china": "cf_ck_volume_usa_to_china",
                "polyester_bottle_chip_operating_rate": "cf_pb_operate_rate",
                "cotton_yarn_inventory_index": "cf_sx_stock_index",
                "cotton_yarn_spot_price_index": "cf_sx_spot_price_index",
                "cotton_yarn_operating_rate": "cf_sx_operate_rate",
                "india_cotton_yarn_operating_rate": "cf_india_operate_rate",
                "textile_pmi": "cf_textile_PMI",
                "import_price_index": "cf_jk_price_index",
                "import_volume": "cf_jk_volume",
                "textile_operating_rate": "cf_textile_operate_rate",
                "textile_order_days": "cf_textile_order_days",
                "inspection_volume": "cf_inspect_volume",
            },
            "EB": {
                "spot_price": "eb_spot_price",
                "spot_price_east_china": "eb_spot_price_hd",
                "spot_price_north_china": "eb_spot_price_hb",
                "futures_close_price": "eb_future_close_price",
                "benzene_styrene_spread": "eb_bz_spread",
                "unit_equipment_profit": "eb_equipment_profit_unit",
                "nonunit_equipment_profit": "eb_equipment_profit_nonunit",
                "port_inventory": "eb_port_stock",
                "south_china_port_inventory": "eb_port_stock_hn",
                "factory_inventory": "eb_factory_stock",
                "yield": "eb_yield",
                "weekly_yield": "eb_weekly_yield",
                "imports": "eb_jk_volume",
                "capacity_utilization": "eb_capacity_utilization_rate",
                "operating_rate": "eb_operate_rate",
                "downstream_operating_rate": "eb_downstream_operate_rate",
                "abs_profit": "abs_profit",
                "abs_inventory": "abs_stock",
                "abs_yield": "abs_yield",
                "abs_operating_rate": "abs_operate_rate",
                "eps_profit": "eps_profit",
                "eps_inventory": "eps_factory_stock",
                "eps_yield": "eps_yield",
                "eps_operating_rate": "eps_operate_rate",
                "ps_profit": "ps_profit",
                "ps_inventory": "ps_factory_stock",
                "ps_yield": "ps_yield",
                "ps_operating_rate": "ps_operate_rate",
                "pure_benzene_profit": "bz_profit",
                "pure_benzene_port_inventory": "bz_port_stock",
                "pure_benzene_capacity_utilization": "bz_capacity_utilization_rate",
                "pure_benzene_styrene_demand": "bz_eb_demand",
            },
            "HC": {
                "spot_price": "hc_spot_price",
                "profit": "hc_profit",
                "trade_volume": "hc_trade_volume",
                "social_inventory": "hc_social_stock",
                "steel_mill_inventory": "hc_factory_stock",
                "yield": "hc_yield",
                "operating_rate": "hc_operate_rate",
                "capacity_utilization": "hc_capacity_utilization_rate",
                "hot_metal_yield": "iron_yield",
                "blast_furnace_operating_rate": "iron_operate_rate",
                "steel_mill_profit_rate": "iron_factory_profit_rate",
                "steel_mill_operating_rate": "iron_factory_operate_rate",
                "land_transaction": "land_trade_volume",
                "housing_transaction": "house_trade_volume",
            },
            "I": {
                "spot_price": "i_spot_price",
                "port_spot_price": "i_port_spot_price",
                "trade_volume": "i_trade_volume",
                "port_arrivals": "i_arrivals",
                "departures": "i_departures",
                "import_volume": "i_jk_volume",
                "import_volume_days": "i_jk_volume_days",
                "steel_mill_import_demand": "i_jk_demand",
                "steel_mill_total_import_demand": "i_jk_total_demand",
                "port_inventory": "i_port_stock",
                "steel_mill_import_inventory": "i_jk_factory_stock",
                "steel_mill_domestic_inventory": "i_gc_factory_stock",
                "domestic_mine_demand": "i_gc_demand",
                "import_to_domestic_inventory_ratio": "i_jk_is_ratio",
                "yield": "i_yield",
                "capacity_utilization": "i_capacity_utilization_rate",
                "hot_metal_yield": "iron_yield",
                "blast_furnace_operating_rate": "iron_operate_rate",
                "steel_mill_profit_rate": "iron_factory_profit_rate",
                "steel_mill_operating_rate": "iron_factory_operate_rate",
            },
            "J": {
                "spot_price": "j_spot_price_rizhao",
                "spot_price_rizhao": "j_spot_price_rizhao",
                "spot_price_tianjin": "j_spot_price_tianjin",
                "profit": "j_profit",
                "port_inventory": "j_port_stock",
                "steel_mill_inventory": "j_factory_stock",
                "steel_mill_coke_inventory": "j_steel_factory_stock",
                "steel_mill_coke_inventory_days": "j_steel_factory_stock_days",
                "yield": "j_yield",
                "capacity_utilization": "j_capacity_utilization_rate",
                "coke_coking_coal_freight": "j_jm_freight_fee",
            },
            "M": {
                "spot_price": "m_spot_price",
                "basis_spread": "m_spread",
                "factory_inventory": "m_factory_stock",
                "factory_inventory_days": "m_factory_stock_days",
                "trade_volume": "m_trade_volume",
                "spot_trade_volume": "m_spot_trade_volume",
                "contract_volume": "m_contract_volume",
                "usa_trade_volume": "m_trade_volume_usa",
                "shipment": "m_shipment",
                "yield": "m_yield",
                "feed_output": "m_feed_yield",
                "soybean_spot_price_tianjin": "a_spot_price_tianjin",
                "soybean_crush_profit": "a_squeeze_profit",
                "soybean_crush_volume": "a_squeeze_volume",
                "soybean_arrivals": "a_arrivals",
                "soybean_port_inventory": "a_port_stock",
                "soybean_oil_spot_price": "y_spot_price",
                "soybean_oil_factory_inventory": "y_factory_stock",
                "soybean_oil_trade_volume": "y_trade_volume",
                "soybean_oil_yield": "y_yield",
                "soybean_oil_inventory_sales_ratio": "y_is_ratio",
                "us_soybean_stock_end": "y_usa_stock_end",
            },
            "MA": {
                "spot_price": "ma_spot_price",
                "paper_spot_price": "ma_spot_price_paper",
                "profit": "ma_profit",
                "hebei_profit": "ma_profit_hebei",
                "coal_cost_shandong": "ma_c_cost_shandong",
                "coal_profit_shandong": "ma_c_profit_shandong",
                "social_inventory": "ma_social_stock",
                "factory_inventory": "ma_factory_stock",
                "factory_inventory_days": "ma_factory_stock_days",
                "port_inventory": "ma_port_stock",
                "warehouse_receipts": "ma_warehouse",
                "port_arrivals": "ma_arrivals",
                "imports_east_china": "ma_jk_volume_hd",
                "yield": "ma_yield",
                "operating_rate": "ma_operate_rate",
                "downstream_operating_rate": "ma_downstream_operate_rate",
                "price_stock_ratio": "ma_ps_ratio",
                "olefin_operating_rate": "ma_olefin_operate_rate",
                "olefin_downstream_operating_rate": "ma_olefin_downstream_operate_rate",
                "olefin_downstream_profit": "ma_olefin_downstream_profit",
                "olefin_purchase_volume": "ma_olefin_purchase_volume",
                "dme_downstream_operating_rate": "ma_dme_downstream_operate_rate",
                "mtbe_downstream_operating_rate": "ma__mtbe_downstream_operate_rate",
                "mtbe_downstream_profit": "ma_mtbe_downstream_profit",
                "acetic_acid_downstream_operating_rate": "ma_aacid_downstream_operate_rate",
                "acetic_acid_downstream_profit": "ma_aacid_downstream_profit",
                "pp_downstream_profit": "ma_pp_downstream_profit",
                "py_downstream_profit": "ma_py_downstream_profit",
            },
            "P": {
                "spot_price": "p_spot_price",
                "import_profit": "p_jk_profit",
                "basis_inventory": "p_b_stock",
                "trade_volume": "p_trade_volume",
                "imports": "p_jk_volume",
                "arrivals": "p_arrivals",
                "indonesia_exports": "p_ck_volume_indonesia",
                "malaysia_exports": "p_ck_volume_malaysia",
                "indonesia_inventory": "p_stock_indonesia",
                "malaysia_inventory": "p_stock_malaysia",
                "indonesia_yield": "p_yield_indonesia",
                "malaysia_yield": "p_yield_malaysia",
                "malaysia_extraction_rate": "p_oer_malaysia",
            },
            "PB": {
                "spot_price": "pb_spot_price",
                "lead_ingot_spot_price": "pb_ingot_spot_price",
                "bike_scrap_battery_spot_price": "pb_bike_scrap_battery_spot_price",
                "car_scrap_battery_spot_price": "pb_car_scrap_battery_spot_price",
                "reflective_furnace_profit": "pb_profit_reflective_furnace",
                "processing_fee": "pb_processing_fee",
                "social_inventory": "pb_social_stock",
                "lead_ingot_inventory": "pb_ingot_stock",
                "shfe_inventory": "pb_shfe_stock",
                "lme_inventory": "pb_lme_stock",
                "global_refined_yield": "pb_refine_yield",
                "electric_lead_yield": "pb_electric_yield",
                "recycled_lead_yield": "pb_recycle_yield",
                "primary_operating_rate": "pb_primary_operate_rate",
                "recycled_lead_operating_rate": "pb_recycle_operate_rate",
                "smm_recycled_lead_operating_rate": "pb_smm_recycle_operate_rate",
                "smm_recycled_lead_inventory_days": "pb_SMM_recycle_stock_days",
                "lead_acid_battery_operating_rate": "pb_battery_operate_rate",
                "smm_battery_operating_rate": "pb_smm_battery_operate_rate",
            },
            "RB": {
                "spot_price": "rb_spot_price",
                "spot_price_20mm": "rb_20mm_spot_price",
                "billet_spot_price": "rb_billet_spot_price",
                "scrap_spot_price": "rb_scrap_spot_price",
                "profit": "rb_profit",
                "tangshan_profit": "rb_profit_dangshan",
                "trade_volume": "rb_trade_volume",
                "social_inventory": "rb_social_stock",
                "steel_mill_inventory": "rb_factory_stock",
                "yield": "rb_yield",
                "operating_rate": "rb_operate_rate",
                "demand": "rb_demand",
                "short_process_yield": "rb_short_processing_yield",
                "long_process_yield": "rb_long_processing_yield",
                "short_process_operating_rate": "rb_short_processing_operate_rate",
                "long_process_operating_rate": "rb_long_processing_operate_rate",
                "short_process_inventory": "rb_short_processing_factory_stock",
                "long_process_inventory": "rb_long_processing_factory_stock",
                "short_process_capacity_utilization": "rb_short_processing_capacity_utilization_rate",
                "long_process_capacity_utilization": "rb_long_processing_capacity_utilization_rate",
                "hot_metal_yield": "iron_yield",
                "blast_furnace_operating_rate": "iron_operate_rate",
                "steel_mill_profit_rate": "iron_factory_profit_rate",
                "steel_mill_operating_rate": "iron_factory_operate_rate",
                "land_transaction": "land_trade_volume",
                "housing_transaction": "house_trade_volume",
            },
            "SR": {
                "spot_price": "sr_spot_price_nanning",
                "national_spot_context": "sr_spot_price",
                "spot_price_liuzhou": "sr_spot_price_liuzhou",
                "spot_price_kunming": "sr_spot_price_kunming",
                "brazil_import_profit_non_quota": "sr_jk_profit_non_quota_brazil",
                "brazil_import_profit_quota": "sr_jk_profit_quota_brazil",
                "thailand_import_profit_non_quota": "sr_jk_profit_non_quota_thailand",
                "thailand_import_profit_quota": "sr_jk_profit_quota_thailand",
                "cane_sugar_yield_guangxi_cumulative": "sr_yield",
                "brazil_warehouse_receipts": "sr_ck_volume_brazil",
                "cane_sugar_sales_guangxi_cumulative": "sr_trade_volume",
                "domestic_sales_rate": "sr_domestic_sales_rate",
                "demand_balance": "sr_balance_demand",
                "imports": "sr_jk_volume",
                "brazil_to_china_imports": "sr_jk_volume_brazil",
            },
            "TA": {
                "spot_price": "ta_spot_price",
                "spot_price_east_china": "ta_spot_price_hd",
                "processing_fee": "ta_processing_fee",
                "pxn_spread": "ta_PXN",
                "px_spot_price": "ta_px_spot_price",
                "naphtha_spot_price": "ta_naphtha_spot_price",
                "downstream_profit": "ta_downstream_profit",
                "poy_downstream_profit": "ta_poy_downstream_profit",
                "fdy_downstream_profit": "ta_fdy_downstream_profit",
                "dty_downstream_profit": "ta_dty_downstream_profit",
                "dx_downstream_profit": "ta_dx_downstream_profit",
                "social_inventory": "ta_social_stock",
                "factory_inventory": "ta_factory_stock",
                "warehouse_receipts": "ta_warehouse",
                "cs_downstream_inventory": "ta_cs_downstream_stock",
                "dx_downstream_inventory": "ta_dx_downstream_stock",
                "pet_downstream_inventory": "ta_pet_downstream_stock",
                "jzxwdx_downstream_inventory": "ta_jzxwdx_downstream_stock",
                "yield": "ta_yield",
                "monthly_yield": "ta_monthly_yield",
                "operating_rate": "ta_operate_rate",
                "weekly_operating_rate": "ta_weekly_operate_rate",
                "textile_operating_rate": "ta_textile_operate_rate",
                "textile_machine_operating_rate": "ta_textile_machine_operate_rate",
                "textile_raw_stock_days": "ta_textile_raw_stock_days",
                "textile_stock_days": "ta_textile_stock_days",
                "cs_spot_price": "ta_cs_spot_price",
                "cs_downstream_price_stock_ratio": "ta_cs_downstream_ps_ratio",
                "dx_downstream_price_stock_ratio": "ta_dx_downstream_ps_ratio",
                "pe_operating_rate": "pe_operate_rate",
                "px_operating_rate": "px_operate_rate",
                "px_yield": "px_yield",
            },
            "ZN": {
                "spot_price": "zn_spot_price",
                "spot_price_shanghai": "zn_spot_price_shanghai",
                "processing_fee": "zn_processing_fee",
                "cif_spread": "zn_cif_spread",
                "mine_port_inventory": "zn_mine_port_stock",
                "lme_inventory": "zn_lme_social_stock",
                "ingot_inventory": "zn_ingot_stock",
                "social_inventory": "zn_social_stock",
                "domestic_yield": "zn_yield",
                "monthly_yield": "zn_monthly_yield",
                "trade_volume": "zn_trade_volume",
                "imports": "zn_jk_volume",
                "operating_rate": "zn_operate_rate",
                "galvanizing_operating_rate": "zn_galvanize_operate_rate",
                "alloy_operating_rate": "zn_alloy_operate_rate",
                "oxide_operating_rate": "zn_oxide_operate_rate",
            },
        }

        low_confidence_indicators = {
            "c_sorghum_spot_price": (
                "flat during the current validation window; keep as replacement-grain context only"
            ),
            "c_barley_spot_price": (
                "low variation during the current validation window; keep as replacement-grain context only"
            ),
            "pb_processing_fee": (
                "low variation during the current validation window; keep as processing-cost context only"
            ),
            "sr_spot_price": (
                "low variation and not used as the primary SR spot anchor; regional spot prices drive basis context"
            ),
        }
        # Track coverage and freshness so backtest audits can inspect data availability.
        self.last_fundamentals_metadata = {
            "ticker": ticker,
            "trading_date": str(trading_date)[:10],
            "configured_indicator_count": 0,
            "loaded_indicator_count": 0,
            "missing_file_count": 0,
            "empty_frame_count": 0,
            "no_data_before_count": 0,
            "stale_indicator_count": 0,
            "near_stale_indicator_count": 0,
            "formatted_indicator_count": 0,
            "coverage_ratio": 0.0,
            "basis_available": False,
            "basis": None,
            "missing_like_count": 0,
            "missing_ratio": 0.0,
            "stale_ratio": 0.0,
            "near_stale_ratio": 0.0,
            "low_confidence_indicator_count": 0,
            "low_confidence_indicators": [],
            "excluded_stale_indicator_count": 0,
            "excluded_stale_indicators": [],
            "undated_indicator_count": 0,
            "undated_indicators": [],
            "invalid_schema_count": 0,
            "source_read_error_count": 0,
            "indicator_role_counts": {},
            "indicator_frequency_counts": {},
        }

        if ticker not in indicator_map:
            return None

        trading_dt = pd.to_datetime(trading_date)
        indicators = indicator_map[ticker]
        fundamental_data = {}
        self.last_fundamentals_metadata["configured_indicator_count"] = len(indicators)

        for name_cn, filename in indicators.items():
            indicator_role = self._get_indicator_role(filename, name_cn)
            indicator_frequency = self._get_indicator_frequency(filename, name_cn)
            file_path = data_dir / f"{filename}.feather"

            if not file_path.exists():
                logger.warning(f"{ticker}: fundamental_source_file_unavailable")
                self.last_fundamentals_metadata["missing_file_count"] += 1
                continue

            try:
                df = read_finoview_feather_cached(file_path)
                if df.empty:
                    logger.warning(f"{ticker}: fundamental_source_empty")
                    self.last_fundamentals_metadata["empty_frame_count"] += 1
                    continue

                date_col = None
                if 'tradeDate' in df.columns:
                    date_col = 'tradeDate'
                elif 'date' in df.columns:
                    date_col = 'date'

                if date_col:
                    df[date_col] = pd.to_datetime(df[date_col])
                    df_filtered = df[df[date_col] < trading_dt].copy()
                else:
                    logger.error(f"{ticker}: fundamental_source_date_column_missing")
                    self.last_fundamentals_metadata["undated_indicator_count"] += 1
                    self.last_fundamentals_metadata["undated_indicators"].append(
                        {
                            "ticker": ticker,
                            "indicator": filename,
                            "display_name": name_cn,
                            "evidence_policy": "excluded_missing_date_column",
                        }
                    )
                    continue

                if df_filtered.empty:
                    logger.warning(f"{ticker}: fundamental_source_cutoff_data_unavailable")
                    self.last_fundamentals_metadata["no_data_before_count"] += 1
                    continue

                latest = df_filtered.iloc[-1]

                if 'tradeDate' in df.columns:
                    date_str = latest['tradeDate']
                elif 'date' in df.columns:
                    date_str = latest['date']
                else:
                    date_str = 'Unknown'

                if filename in df.columns:
                    value = latest[filename]
                elif 'price' in df.columns:
                    value = latest['price']
                elif 'value' in df.columns:
                    value = latest['value']
                else:
                    logger.error(f"{ticker}: fundamental_source_value_column_missing")
                    self.last_fundamentals_metadata["invalid_schema_count"] += 1
                    continue

                try:
                    numeric_value = float(value)
                except (TypeError, ValueError):
                    logger.error(f"{ticker}: fundamental_source_value_invalid")
                    self.last_fundamentals_metadata["invalid_schema_count"] += 1
                    continue
                if not math.isfinite(numeric_value):
                    logger.error(f"{ticker}: fundamental_source_value_invalid")
                    self.last_fundamentals_metadata["invalid_schema_count"] += 1
                    continue

                if len(df_filtered) >= 5:
                    recent_5 = df_filtered.tail(5)
                    if filename in recent_5.columns:
                        initial_value = recent_5[filename].iloc[0]
                        trend = (
                            (recent_5[filename].iloc[-1] - initial_value) / initial_value
                            if abs(initial_value) > 1e-10
                            else 0
                        )
                    elif 'price' in recent_5.columns:
                        initial_value = recent_5['price'].iloc[0]
                        trend = (
                            (recent_5['price'].iloc[-1] - initial_value) / initial_value
                            if abs(initial_value) > 1e-10
                            else 0
                        )
                    elif 'value' in recent_5.columns:
                        initial_value = recent_5['value'].iloc[0]
                        trend = (
                            (recent_5['value'].iloc[-1] - initial_value) / initial_value
                            if abs(initial_value) > 1e-10
                            else 0
                        )
                    else:
                        trend = 0
                else:
                    trend = 0

                if not math.isfinite(trend):
                    trend = 0

                is_stale = False
                stale_detail = None
                if date_str != 'Unknown':
                    try:
                        data_date = pd.to_datetime(str(date_str))
                        if pd.isna(trading_dt):
                            logger.error(f"{ticker}: fundamental_trading_date_invalid")
                        else:
                            days_diff = (trading_dt - data_date).days
                            if days_diff < 0:
                                logger.warning(f"{ticker}: fundamental_source_future_date")
                                days_diff = 0

                            max_days = self._get_max_days_for_indicator(
                                filename,
                                ticker,
                                indicator_frequency,
                            )
                            if days_diff > max_days:
                                logger.warning(f"{ticker}: fundamental_source_stale")
                                self.last_fundamentals_metadata["stale_indicator_count"] += 1
                                is_stale = True
                                stale_detail = {
                                    "ticker": ticker,
                                    "indicator": filename,
                                    "display_name": name_cn,
                                    "data_date": data_date.strftime("%Y-%m-%d"),
                                    "trading_date": trading_dt.strftime("%Y-%m-%d"),
                                    "age_days": int(days_diff),
                                    "max_age_days": int(max_days),
                                    "role": indicator_role,
                                    "frequency": indicator_frequency,
                                    "evidence_policy": "excluded_from_directional_prompt",
                                }
                            elif days_diff > max_days * 0.7:
                                logger.warning(f"{ticker}: fundamental_source_near_stale")
                                self.last_fundamentals_metadata["near_stale_indicator_count"] += 1
                    except Exception:
                        logger.error(f"{ticker}: fundamental_data_freshness_check_failed")
                else:
                    logger.warning(f"{ticker}: fundamental_source_date_missing")

                if is_stale:
                    self.last_fundamentals_metadata["excluded_stale_indicator_count"] += 1
                    if stale_detail:
                        self.last_fundamentals_metadata["excluded_stale_indicators"].append(stale_detail)
                    continue

                low_confidence_note = low_confidence_indicators.get(filename)
                fundamental_data[name_cn] = {
                    'latest': numeric_value,
                    'date': str(date_str),
                    'trend_5d': float(trend) if pd.notna(trend) else 0,
                    'role': indicator_role,
                    'frequency': indicator_frequency,
                }
                if low_confidence_note:
                    fundamental_data[name_cn]['quality_note'] = low_confidence_note
                    self.last_fundamentals_metadata["low_confidence_indicator_count"] += 1
                    self.last_fundamentals_metadata["low_confidence_indicators"].append(filename)
                role_counts = self.last_fundamentals_metadata["indicator_role_counts"]
                frequency_counts = self.last_fundamentals_metadata["indicator_frequency_counts"]
                role_counts[indicator_role] = role_counts.get(indicator_role, 0) + 1
                frequency_counts[indicator_frequency] = frequency_counts.get(indicator_frequency, 0) + 1
                self.last_fundamentals_metadata["loaded_indicator_count"] += 1

            except Exception:
                logger.error(f"{ticker}: fundamental_source_read_failed")
                self.last_fundamentals_metadata["source_read_error_count"] += 1
                continue

        configured_count = self.last_fundamentals_metadata["configured_indicator_count"]
        loaded_count = self.last_fundamentals_metadata["loaded_indicator_count"]
        missing_like_count = sum(
            int(self.last_fundamentals_metadata.get(field) or 0)
            for field in (
                "missing_file_count",
                "empty_frame_count",
                "no_data_before_count",
                "undated_indicator_count",
                "invalid_schema_count",
                "source_read_error_count",
            )
        )
        self.last_fundamentals_metadata["missing_like_count"] = missing_like_count
        if configured_count:
            self.last_fundamentals_metadata["coverage_ratio"] = loaded_count / configured_count
            self.last_fundamentals_metadata["missing_ratio"] = missing_like_count / configured_count
            self.last_fundamentals_metadata["stale_ratio"] = (
                int(self.last_fundamentals_metadata.get("stale_indicator_count") or 0)
                / configured_count
            )
            self.last_fundamentals_metadata["near_stale_ratio"] = (
                int(self.last_fundamentals_metadata.get("near_stale_indicator_count") or 0)
                / configured_count
            )

        if not fundamental_data:
            logger.error(f"{ticker}: fundamental_data_unavailable")
            self.last_fundamentals_metadata["formatted_indicator_count"] = 0
            return None

        self.last_fundamentals_metadata["formatted_indicator_count"] = len(fundamental_data)

        try:
            if ticker in indicator_map and 'spot_price' in indicator_map[ticker]:
                spot_price_data = fundamental_data.get('spot_price')
                if spot_price_data and spot_price_data['latest'] > 0:
                    spot_price = spot_price_data['latest']
                    try:
                        basis_reference_date = get_previous_trading_day(
                            router=self,
                            trading_date=trading_dt,
                            underlying_code=ticker,
                        )
                    except Exception:
                        logger.warning(f"{ticker}: basis_reference_date_unavailable")
                        basis_reference_date = None
                    if basis_reference_date is None:
                        futures_quotes = []
                    else:
                        futures_quotes = self.api.get_continuous_candles(
                            underlying_code=ticker,
                            start_date=basis_reference_date - timedelta(days=30),
                            end_date=basis_reference_date,
                        )

                    if futures_quotes:
                        futures_price = futures_quotes[-1].close
                        futures_date = futures_quotes[-1].trade_date
                        basis = spot_price - futures_price
                        basis_pct = (basis / futures_price) * 100 if futures_price > 0 else 0

                        basis_trend_5d = 0
                        if len(futures_quotes) >= 6:
                            futures_5d_ago = futures_quotes[-6].close
                            basis_5d_ago = spot_price - futures_5d_ago
                            if basis_5d_ago != 0:
                                basis_trend_5d = ((basis - basis_5d_ago) / abs(basis_5d_ago)) * 100

                        if basis > 0:
                            basis_status = 'backwardation'
                            basis_signal = (
                                'tight spot supply, bullish bias'
                                if basis_trend_5d > 0
                                else 'spot supply tighter than futures'
                            )
                        elif basis < 0:
                            basis_status = 'contango'
                            basis_signal = (
                                'loose spot supply, bearish bias'
                                if basis_trend_5d < 0
                                else 'spot supply looser than futures'
                            )
                        else:
                            basis_status = 'flat'
                            basis_signal = 'spot and futures are roughly aligned'

                        if basis_trend_5d > 0.5:
                            basis_trend_status = 'strengthening'
                            basis_implication = (
                                'basis is strengthening; this favors short hedgers and challenges long hedgers'
                            )
                        elif basis_trend_5d < -0.5:
                            basis_trend_status = 'weakening'
                            basis_implication = (
                                'basis is weakening; this challenges short hedgers and favors long hedgers'
                            )
                        else:
                            basis_trend_status = 'stable'
                            basis_implication = 'basis is broadly stable'

                        fundamental_data['basis'] = {
                            'latest': float(basis),
                            'latest_pct': float(basis_pct),
                            'date': str(futures_date),
                            'trend_5d': float(basis_trend_5d),
                            'status': basis_status,
                            'signal': basis_signal,
                            'trend_status': basis_trend_status,
                            'implication': basis_implication,
                            'spot_price': float(spot_price),
                            'futures_price': float(futures_price),
                            'pre_open_reference_date': basis_reference_date.strftime("%Y-%m-%d")
                            if basis_reference_date is not None
                            else None,
                        }
                        self.last_fundamentals_metadata["basis"] = dict(fundamental_data["basis"])
                        self.last_fundamentals_metadata["basis_available"] = True
                    else:
                        logger.warning(f"{ticker}: No futures data for basis calculation")
                else:
                    logger.warning(f"{ticker}: No spot price data for basis calculation")
            else:
                logger.warning(f"{ticker}: Spot price not configured for basis calculation")
        except Exception:
            logger.error(f"{ticker}: basis_calculation_failed")

        self.last_fundamentals_metadata["formatted_indicator_count"] = len(fundamental_data)
        try:
            from tools.agent_tools.analysis.analyst_finoview_factors import (
                build_factor_attribution_payload,
                build_factor_catalog,
                build_factor_snapshot,
                build_local_finoview_availability_audit,
                map_factor_snapshot_to_judgment,
            )

            if finoview_enabled:
                local_catalog = build_factor_catalog(
                    data_dir=data_dir,
                    limit_to_tickers=[ticker],
                    catalog_config_path=catalog_path,
                )
                factor_snapshot = build_factor_snapshot(
                    ticker,
                    trading_date,
                    data_dir=data_dir,
                    catalog=local_catalog,
                )
                factor_judgment = map_factor_snapshot_to_judgment(factor_snapshot)
                factor_attribution = build_factor_attribution_payload(
                    ticker=ticker,
                    trade_date=trading_date,
                    snapshot=factor_snapshot,
                    judgment=factor_judgment,
                )
                self.last_fundamentals_metadata["finoview_factor_snapshot"] = factor_snapshot
                self.last_fundamentals_metadata["finoview_factor_judgment"] = factor_judgment
                self.last_fundamentals_metadata["finoview_factor_attribution"] = factor_attribution
                self.last_fundamentals_metadata["factor_coverage_score"] = factor_judgment.get("coverage_score", 0.0)
                self.last_fundamentals_metadata["factor_freshness_score"] = factor_judgment.get("freshness_score", 0.0)
                self.last_fundamentals_metadata["no_lookahead_status"] = factor_judgment.get("no_lookahead_status", "unchecked")
                self.last_fundamentals_metadata["local_finoview_availability_audit"] = (
                    build_local_finoview_availability_audit(
                        ticker,
                        trading_date,
                        data_dir=data_dir,
                        catalog_config_path=catalog_path,
                        catalog=local_catalog,
                    )
                )
        except Exception:
            logger.warning(f"{ticker}: finoview_factor_snapshot_unavailable")

        result = f"=== Fundamental Analysis for {ticker} ===\n\n"
        factor_judgment = self.last_fundamentals_metadata.get("finoview_factor_judgment") or {}
        availability_audit = self.last_fundamentals_metadata.get("local_finoview_availability_audit") or {}
        if factor_judgment:
            result += "=== Finoview No-Lookahead Factor Snapshot ===\n"
            result += f"No-lookahead status: {factor_judgment.get('no_lookahead_status')}\n"
            result += f"Coverage score: {float(factor_judgment.get('coverage_score') or 0.0):.2f}\n"
            result += f"Freshness score: {float(factor_judgment.get('freshness_score') or 0.0):.2f}\n"
            result += f"Covered groups: {', '.join(factor_judgment.get('covered_required_groups') or []) or 'none'}\n"
            result += f"Tradable coverage: {factor_judgment.get('tradable_coverage')}\n\n"
        if availability_audit:
            result += "=== Local Finoview Availability Audit ===\n"
            result += f"Runtime boundary: {availability_audit.get('runtime_data_boundary')}\n"
            result += f"Coverage status: {availability_audit.get('coverage_status')}\n"
            result += f"Supports fundamental trade setup: {availability_audit.get('supports_fundamental_trade_setup')}\n"
            result += f"Local feather/index-map ratio: {float(availability_audit.get('local_vs_index_ratio') or 0.0):.2f}\n"
            result += f"Known catalog ratio: {float(availability_audit.get('known_catalog_ratio') or 0.0):.2f}\n"
            result += f"Missing required groups: {', '.join(availability_audit.get('missing_required_groups') or []) or 'none'}\n\n"
        for name_cn, data in fundamental_data.items():
            if name_cn == 'basis':
                result += "\n=== Basis Analysis ===\n"
                result += f"Basis value: {data['latest']:.2f} ({data['latest_pct']:.2f}%)\n"
                result += f"Basis status: {data['status']} - {data['signal']}\n"
                result += f"Basis trend: {data['trend_status']} (5d change: {data['trend_5d']:.2f}%)\n"
                result += f"Trading implication: {data['implication']}\n"
                result += f"Price components: spot={data['spot_price']:.2f}, futures={data['futures_price']:.2f}\n"
                result += f"Data date: {data['date']}\n"
            else:
                if data.get('trend_5d', 0) > 0.01:
                    trend_label = 'up'
                elif data.get('trend_5d', 0) < -0.01:
                    trend_label = 'down'
                else:
                    trend_label = 'flat'
                result += (
                    f"{name_cn}: {data['latest']:.2f} ({data['date']}) "
                    f"[role: {data.get('role', 'context')}; "
                    f"frequency: {data.get('frequency', 'unknown')}; "
                    f"last 5 obs trend: {trend_label} {data.get('trend_5d', 0):.2%}]\n"
                )
                if data.get('quality_note'):
                    result += f"  Low-confidence note: {data['quality_note']}\n"

        return result

    def get_china_futures_news(self, ticker, trading_date, news_count=10, pre_open_only=True):
        """
        Load futures news from the local Future_news directory.
        """
        from datetime import datetime
        from pydantic import BaseModel
        from typing import Optional
        import re

        class NewsItem(BaseModel):
            title: str
            publisher: str
            publish_time: str
            content: Optional[str] = None
            url: Optional[str] = None

        factor_cfg = (self.config.get("factor_data") or {}) if isinstance(self.config, dict) else {}
        news_cfg = factor_cfg.get("news") or {}
        news_dir = self._resolve_project_path(
            news_cfg.get("data_dir"),
            "data/News_data/Future_news",
        )
        news_file = news_dir / f"{ticker}.txt"
        self.last_news_metadata = {
            "ticker": ticker,
            "trading_date": str(trading_date)[:10],
            "file_path": str(news_file),
            "file_exists": news_file.exists(),
            "encoding": None,
            "raw_block_count": 0,
            "parsed_news_count": 0,
            "selected_news_count": 0,
            "latest_news_date": None,
            "news_cutoff": f"{'<' if pre_open_only else '<='}{str(trading_date)[:10]}",
            "errors": [],
        }

        if not news_file.exists():
            logger.error(f"{ticker}: news_source_file_unavailable")
            return []

        try:
            content, encoding = read_text_cached(news_file)
            self.last_news_metadata["encoding"] = encoding

            raw_lines = [line.strip() for line in content.splitlines()]
            news_blocks = []
            current_block = []

            for line in raw_lines:
                if not line:
                    if current_block:
                        news_blocks.append(current_block)
                        current_block = []
                    continue

                is_separator = (
                    re.fullmatch(r"[-\u2013\u2014]+", line) is not None
                    or (len(line) <= 3 and re.search(r"[0-9A-Za-z\u4e00-\u9fff]", line) is None)
                )

                if is_separator:
                    if current_block:
                        news_blocks.append(current_block)
                        current_block = []
                    continue

                current_block.append(line)

            if current_block:
                news_blocks.append(current_block)
            self.last_news_metadata["raw_block_count"] = len(news_blocks)

            news_items = []

            if isinstance(trading_date, datetime):
                trading_dt = trading_date
            else:
                trading_dt = datetime.strptime(trading_date, '%Y-%m-%d')

            for block in news_blocks:
                if len(block) < 3:
                    continue

                try:
                    date_idx = next(
                        (idx for idx, line in enumerate(block[:3]) if re.match(r'^\d{4}-\d{2}-\d{2}$', line)),
                        None
                    )
                    if date_idx is None:
                        raise ValueError(f"no valid date line found in block head: {block[:3]}")

                    news_date = datetime.strptime(block[date_idx], '%Y-%m-%d')
                    if pre_open_only and news_date >= trading_dt:
                        continue

                    if not pre_open_only and news_date > trading_dt:
                        continue

                    title = block[date_idx + 1].strip() if len(block) > date_idx + 1 else ''
                    news_content = block[date_idx + 2].strip() if len(block) > date_idx + 2 else ''
                    category = block[date_idx + 3].strip() if len(block) > date_idx + 3 else 'unknown'
                    source = block[date_idx + 4].strip() if len(block) > date_idx + 4 else 'unknown'

                    if not title:
                        raise ValueError('missing news title')

                    news_items.append(NewsItem(
                        title=sanitize_visible_text(title),
                        publisher=sanitize_visible_text(f"{category} - {source}"),
                        publish_time=news_date.strftime('%Y-%m-%d %H:%M:%S'),
                        content=sanitize_visible_text(news_content),
                        url=None
                    ))
                except (ValueError, IndexError):
                    logger.warning(f"{ticker}: news_block_parse_failed")
                    continue

            self.last_news_metadata["parsed_news_count"] = len(news_items)
            news_items.sort(key=lambda x: x.publish_time, reverse=True)
            news_items = news_items[:news_count]
            self.last_news_metadata["selected_news_count"] = len(news_items)
            if news_items:
                self.last_news_metadata["latest_news_date"] = str(news_items[0].publish_time)[:10]
            return news_items

        except Exception:
            logger.error(f"{ticker}: news_source_read_failed")
            if isinstance(getattr(self, "last_news_metadata", None), dict):
                self.last_news_metadata.setdefault("errors", []).append("news_source_read_failed")
            return []

    def _get_indicator_role(self, filename: str, display_name: str = "") -> str:
        """
        Classify a local fundamental series by how an analyst should use it.
        """
        key = f"{filename}_{display_name}".lower()

        if "future" in key or "futures" in key:
            return "price_anchor"
        if "spot_price" in key or "basis" in key or "spread" in key or "cash_spot" in key:
            return "price_basis"
        if any(token in key for token in ("stock", "inventory", "warehouse", "warrant")):
            return "inventory"
        if any(token in key for token in ("profit", "cost", "fee", "tc_", "processing")):
            return "cost_profit"
        if any(token in key for token in ("operate_rate", "capacity", "yield", "production", "plant")):
            return "supply"
        if any(token in key for token in ("demand", "trade_volume", "shipment", "sales", "arrivals")):
            return "demand"
        if any(token in key for token in ("jk_", "import", "export", "ck_")):
            return "trade_flow"
        if any(token in key for token in ("pmi", "order_days", "land", "house", "global")):
            return "macro_downstream"

        return "context"

    def _get_indicator_frequency(self, filename: str, display_name: str = "") -> str:
        """
        Infer the release cadence for freshness checks and prompt context.
        """
        key = f"{filename}_{display_name}".lower()

        monthly_patterns = (
            "arrivals",
            "balance",
            "battery_operate_rate",
            "departures",
            "demand",
            "domestic_sales_rate",
            "electric_yield",
            "global",
            "is_ratio",
            "season",
            "monthly",
            "import",
            "export",
            "jk_volume",
            "ck_volume",
            "jk_profit",
            "pb_yield",
            "sr_yield",
            "sr_trade_volume",
            "usa_stock",
            "us_stock",
            "y_usa_stock",
            "zn_trade_volume",
            "feed_yield",
            "oer_malaysia",
            "pet_downstream_stock",
            "recycle_yield",
            "smm_",
            "stock_days",
            "stock_indonesia",
            "stock_malaysia",
            "yield_indonesia",
            "yield_malaysia",
            "pmi",
            "order_days",
            "galvanize_operate_rate",
            "alloy_operate_rate",
            "oxide_operate_rate",
            "land_trade_volume",
            "house_trade_volume",
        )
        weekly_patterns = (
            "stock",
            "inventory",
            "operate_rate",
            "yield",
            "profit",
            "demand",
            "arrivals",
            "shipment",
            "sales_progress",
            "trade_volume",
            "warehouse",
        )

        if any(pattern in key for pattern in monthly_patterns):
            return "monthly"
        if any(pattern in key for pattern in weekly_patterns):
            return "weekly"

        return "daily"

    def _get_max_days_for_indicator(self, filename: str, ticker: str, frequency: str | None = None) -> int:
        """
        Return the freshness threshold, in calendar days, for a local fundamental series.

        Heuristics:
        - Explicit inferred frequency wins when available.
        - Monthly-style files allow a longer lag.
        - Weekly-style files use a medium lag.
        - Explicit special cases override the pattern defaults.
        - All other indicators default to a 7-day freshness window.
        """
        # Monthly-style releases can stay valid longer than daily series.
        monthly_patterns = ['_volume', '_us_', '_cn_', '_reserve']
        # Weekly inventory and operating-rate series use a medium freshness window.
        weekly_patterns = ['_stock', '_yield', '_operate_rate', '_profit', '_spread', '_consumption', '_arrivals']
        # Explicit overrides for indicators with known publication cycles.
        special_cases = {
            'au_lease_rate': 7, 'au_comex': 7, 'au_etf': 7, 'au_us_rate': 7,
            'sc_eia': 14, 'sc_us_rig': 14, 'sc_refinery': 14, 'sc_saudi': 45, 'sc_us_production': 30,
            'cu_lme': 7, 'cu_comex': 7,
            'cf_textile_order': 60, 'cf_us_export': 30,
            'c_feed_output': 60,
            'c_hog_inventory': 45, 'c_sow_inventory': 45, 'c_import_volume': 45,
            'y_us_stock': 60,
            'ta_downstream_stock': 60,
        }

        for pattern, max_days in special_cases.items():
            if pattern in filename:
                return max_days

        if frequency == "monthly":
            return 45

        if frequency == "weekly":
            return 14

        if frequency == "daily":
            return 7

        if any(p in filename for p in monthly_patterns):
            return 45

        if any(p in filename for p in weekly_patterns):
            return 14

        return 7

