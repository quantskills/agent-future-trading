from __future__ import annotations

"""
DataYes API client implementation for Chinese futures market.
Link: https://uqer.datayes.com/

Uses uqer Python SDK for accessing futures data.
Documentation: https://uqer.datayes.com/v3/
"""

import math
import os
import re
from datetime import datetime, timedelta
from typing import Any, List, Optional

try:
    import pandas as pd
    _PANDAS_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - depends on runtime environment
    pd = None
    _PANDAS_IMPORT_ERROR = exc
from apis.common_model import OHLCVCandle
from .api_model import (
    FuturesDailyQuote,
    FuturesMainContract,
    FuturesDailyQuoteOptimized
)


class DataYesAPI:
    """DataYes API Wrapper for Chinese Futures Market."""

    def __init__(self):
        """初始化 DataYes API 客户端"""
        # 加载环境变量
        from dotenv import load_dotenv
        env_path = os.path.join(os.path.dirname(__file__), "../../../.env")
        load_dotenv(env_path)

        self.api_key = os.environ.get("DATAYES_API_KEY")

        # 初始化 uqer 客户端
        try:
            import uqer
            self.client = uqer.Client(token=self.api_key)
            self.DataAPI = uqer.DataAPI
        except ImportError:
            raise ImportError(
                "uqer SDK not found.\n"
                "Please install it using: pip install uqer"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to initialize uqer client: {e}")

    def _dependency_error_message(self, feature_name: str) -> str:
        detail = str(_PANDAS_IMPORT_ERROR) if _PANDAS_IMPORT_ERROR else "pandas is unavailable"
        return (
            f"{feature_name} requires pandas/numpy support, but those native dependencies could not be loaded. "
            f"Original error: {detail}. If you are on Windows, an application-control policy may be blocking the "
            f"numpy DLLs inside the current environment."
        )

    def _require_pandas(self, feature_name: str) -> None:
        if pd is None:
            raise RuntimeError(self._dependency_error_message(feature_name))

    def _query_market_data(self, **params: Any) -> Any:
        actual_params = dict(params)
        if pd is not None:
            actual_params["pandas"] = "1"
        else:
            actual_params.pop("pandas", None)
        return self.DataAPI.MktMFutdGet(**actual_params)

    def _coerce_record(self, row: Any) -> dict[str, Any]:
        if row is None:
            return {}
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            try:
                return row.to_dict()
            except Exception:
                pass
        if hasattr(row, "items"):
            try:
                return dict(row.items())
            except Exception:
                pass
        try:
            return dict(row)
        except Exception:
            return row.__dict__.copy() if hasattr(row, "__dict__") else {}

    def _records_from_response(self, response: Any) -> List[dict[str, Any]]:
        if response is None:
            return []

        if pd is not None and isinstance(response, pd.DataFrame):
            return response.to_dict(orient="records")

        if hasattr(response, "to_dict") and not isinstance(response, dict):
            try:
                return response.to_dict(orient="records")
            except Exception:
                pass

        if isinstance(response, list):
            return [self._coerce_record(item) for item in response if item is not None]

        if isinstance(response, tuple):
            return [self._coerce_record(item) for item in response if item is not None]

        if isinstance(response, dict):
            for key in ("data", "retData", "Data", "records", "items"):
                value = response.get(key)
                if isinstance(value, list):
                    return [self._coerce_record(item) for item in value if item is not None]

            if response and all(isinstance(value, (list, tuple)) for value in response.values()):
                lengths = {len(value) for value in response.values()}
                if len(lengths) == 1:
                    rows: List[dict[str, Any]] = []
                    keys = list(response.keys())
                    for index in range(next(iter(lengths), 0)):
                        rows.append({key: response[key][index] for key in keys})
                    return rows

            return [response]

        if hasattr(response, "__iter__") and not isinstance(response, (str, bytes)):
            try:
                return [self._coerce_record(item) for item in list(response)]
            except Exception:
                return []

        return []

    def _parse_trade_date(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(hour=0, minute=0, second=0, microsecond=0)

        text = str(value).strip()
        if not text or text.lower() in {"nan", "none", "null"}:
            return None

        candidates = [text[:10], text]
        for candidate in candidates:
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
                try:
                    return datetime.strptime(candidate, fmt)
                except ValueError:
                    continue
        return None

    def _is_missing(self, value: Any) -> bool:
        if value is None:
            return True
        if pd is not None:
            try:
                if pd.isna(value):
                    return True
            except Exception:
                pass
        if isinstance(value, float):
            try:
                if math.isnan(value):
                    return True
            except ValueError:
                pass
        text = str(value).strip().lower()
        return text in {"", "nan", "none", "null"}

    def _coerce_float(self, value: Any, default: float = 0.0) -> float:
        if self._is_missing(value):
            return default
        try:
            return float(value)
        except Exception:
            return default

    def _coerce_optional_float(self, value: Any) -> Optional[float]:
        if self._is_missing(value):
            return None
        try:
            return float(value)
        except Exception:
            return None

    def _coerce_int(self, value: Any, default: int = 0) -> int:
        if self._is_missing(value):
            return default
        try:
            return int(float(value))
        except Exception:
            return default

    def _prepare_historical_records(
        self,
        response: Any,
        end_date: datetime,
        contract_id: Optional[str] = None,
    ) -> List[dict[str, Any]]:
        records = self._records_from_response(response)
        if contract_id:
            records = [row for row in records if str(row.get("ticker", "")).upper() == contract_id.upper()]

        filtered: List[dict[str, Any]] = []
        for row in records:
            trade_date = self._parse_trade_date(row.get("tradeDate"))
            if trade_date is None or trade_date >= end_date:
                continue
            filtered.append(row)

        filtered.sort(key=lambda row: self._parse_trade_date(row.get("tradeDate")) or datetime.min)
        return filtered

    def _build_daily_quote_from_row(self, row: Any) -> FuturesDailyQuote:
        row = self._coerce_record(row)
        trade_date = self._parse_trade_date(row.get("tradeDate"))
        return FuturesDailyQuote(
            contract_id=str(row.get("ticker", "")),
            trade_date=trade_date.strftime("%Y-%m-%d") if trade_date else str(row.get("tradeDate", ""))[:10],
            open=self._coerce_float(row.get("openPrice", 0)),
            high=self._coerce_float(row.get("highestPrice", 0)),
            low=self._coerce_float(row.get("lowestPrice", 0)),
            close=self._coerce_float(row.get("closePrice", 0)),
            volume=self._coerce_int(row.get("turnoverVol", 0)),
            turnover=self._coerce_float(row.get("turnoverValue", 0)),
            open_interest=self._coerce_int(row.get("openInt", 0)),
            settle_price=self._coerce_optional_float(row.get("settlePrice")),
            pre_settle_price=self._coerce_optional_float(row.get("preSettlePrice")),
            pre_close_price=self._coerce_optional_float(row.get("preClosePrice")),
            limit_up=self._coerce_optional_float(row.get("limitUpPrice")),
            limit_down=self._coerce_optional_float(row.get("limitDownPrice")),
        )

    def get_futures_daily_candles(
        self,
        contract_id: str = None,
        underlying_code: str = None,
        is_main: int = 0,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> List[FuturesDailyQuote]:
        """
        获取期货日频行情数据

        使用 DataAPI.MktMFutdGet 获取期货日行情

        Args:
            contract_id: 合约代码（如 IF2501），与 underlying_code 二选一
            underlying_code: 标的代码（如 IF, IC, IH, IM），与 contract_id 二选一
            is_main: 是否主力合约，1-是，0-否
            start_date: 开始日期
            end_date: 结束日期，默认为当前日期

        Returns:
            日频行情数据列表
        """
        if end_date is None:
            end_date = datetime.now()
        if start_date is None:
            start_date = end_date - timedelta(days=365)

        # 如果提供了 end_date，过滤掉此日期之后的数据
        # 注意：这里依赖 API 返回的数据中包含 tradeDate 字段

        # 构建参数 - 使用 uqer 的参数命名
        params: dict[str, Any] = {}

        # 根据输入参数设置查询条件
        if contract_id:
            # 通过具体合约代码查询
            query_underlying = underlying_code or self._extract_underlying_code(contract_id)
            if query_underlying:
                params["contractObject"] = query_underlying
        elif underlying_code:
            # 通过标的代码查询
            params["contractObject"] = underlying_code
            if is_main:
                params["mainCon"] = 1

        # 设置日期参数 - 根据文档使用 startDate 而不是 beginDate
        params["startDate"] = start_date.strftime("%Y%m%d")
        params["endDate"] = end_date.strftime("%Y%m%d")

        try:
            # 调用 DataYes API
            response = self._query_market_data(**params)
            records = self._prepare_historical_records(
                response,
                end_date=end_date,
                contract_id=contract_id,
            )

            if not records:
                return []

            return [self._build_daily_quote_from_row(row) for row in records]

            # 过滤数据：确保只返回 end_date 之前的数据（不包括 end_date 当天）
            # 在回测中，T 日做决策时只能看到 T-1 日及之前的数据
            df['tradeDate'] = pd.to_datetime(df['tradeDate'])
            end_dt = pd.to_datetime(end_date)
            df_filtered = df[df['tradeDate'] < end_dt]

            if df_filtered.empty:
                return []

            # 转换为 FuturesDailyQuote 列表
            quotes = []
            for _, row in df_filtered.iterrows():
                quote = FuturesDailyQuote(
                    contract_id=row.get('ticker', ''),
                    trade_date=str(row.get('tradeDate', '')),
                    open=float(row.get('openPrice', 0)),
                    high=float(row.get('highestPrice', 0)),
                    low=float(row.get('lowestPrice', 0)),
                    close=float(row.get('closePrice', 0)),
                    volume=int(row.get('turnoverVol', 0)),
                    turnover=float(row.get('turnoverValue', 0)),
                    open_interest=int(row.get('openInt', 0)),
                    settle_price=float(row.get('settlePrice', 0)) if pd.notna(row.get('settlePrice')) else None,
                    pre_settle_price=float(row.get('preSettlePrice', 0)) if pd.notna(row.get('preSettlePrice')) else None,
                    pre_close_price=float(row.get('preClosePrice', 0)) if pd.notna(row.get('preClosePrice')) else None,
                    limit_up=float(row.get('limitUpPrice', 0)) if 'limitUpPrice' in row and pd.notna(row.get('limitUpPrice')) else None,
                    limit_down=float(row.get('limitDownPrice', 0)) if 'limitDownPrice' in row and pd.notna(row.get('limitDownPrice')) else None
                )
                quotes.append(quote)

            return quotes

        except Exception as e:
            raise RuntimeError(f"Failed to fetch futures daily data: {e}")

    def _extract_underlying_code(self, contract_id: str) -> Optional[str]:
        """Extract the alphabetical underlying code from a concrete contract id."""
        if not contract_id:
            return None
        match = re.match(r"^([A-Za-z]+)", contract_id)
        return match.group(1).upper() if match else None

    def _build_optimized_quote_from_row(self, row) -> FuturesDailyQuoteOptimized:
        """Convert a DataYes row into the optimized futures quote model."""
        row = self._coerce_record(row)
        trade_date = self._parse_trade_date(row.get('tradeDate'))
        return FuturesDailyQuoteOptimized(
            ticker=str(row.get('ticker', '')),
            trade_date=trade_date.strftime("%Y-%m-%d") if trade_date else str(row.get('tradeDate', ''))[:10],
            sec_short_name=row.get('secShortName'),
            exchange_cd=row.get('exchangeCD'),
            pre_settle_price=self._coerce_float(row.get('preSettlePrice', 0)),
            pre_close_price=self._coerce_optional_float(row.get('preClosePrice')),
            open_price=self._coerce_float(row.get('openPrice', 0)),
            highest_price=self._coerce_float(row.get('highestPrice', 0)),
            lowest_price=self._coerce_float(row.get('lowestPrice', 0)),
            close_price=self._coerce_float(row.get('closePrice', 0)),
            settle_price=self._coerce_optional_float(row.get('settlePrice')),
            turnover_vol=self._coerce_int(row.get('turnoverVol', 0)),
            turnover_value=self._coerce_float(row.get('turnoverValue', 0)),
            open_int=self._coerce_int(row.get('openInt', 0)),
            chg=self._coerce_float(row.get('chg', 0)),
            chg1=self._coerce_optional_float(row.get('chg1')),
            chg_pct=self._coerce_float(row.get('chgPct', 0)),
            main_con=self._coerce_int(row.get('mainCon', 0)),
            smain_con=self._coerce_int(row.get('smainCon')) if not self._is_missing(row.get('smainCon')) else None,
            contract_mark=str(row.get('contractMark')) if not self._is_missing(row.get('contractMark')) else None,
            contract_object=str(row.get('contractObject')) if not self._is_missing(row.get('contractObject')) else None,
        )

    def get_futures_quote_on_date(
        self,
        trading_date: datetime,
        contract_id: str = None,
        underlying_code: str = None,
        is_main: int = 0,
    ) -> Optional[FuturesDailyQuoteOptimized]:
        """
        Fetch a single-day futures quote for a concrete contract or the main contract.

        Unlike the historical candle helpers, this method reads the exact `trading_date`
        and therefore may include T-day `open/close/settle` fields.
        """
        params = {
            "tradeDate": trading_date.strftime("%Y%m%d"),
        }

        query_underlying = underlying_code
        if contract_id and not query_underlying:
            query_underlying = self._extract_underlying_code(contract_id)

        if query_underlying:
            params["contractObject"] = query_underlying

        if is_main:
            params["mainCon"] = 1

        try:
            records = self._records_from_response(self._query_market_data(**params))
            if contract_id:
                records = [row for row in records if str(row.get("ticker", "")).upper() == contract_id.upper()]
            if not records:
                return None

            return self._build_optimized_quote_from_row(records[0])
        except Exception as e:
            raise RuntimeError(f"Failed to fetch futures quote on date: {e}")

    def get_main_contract_quote_on_date(
        self,
        underlying_code: str,
        trading_date: datetime,
    ) -> Optional[FuturesDailyQuoteOptimized]:
        """Fetch the main-contract quote on the exact trading date."""
        return self.get_futures_quote_on_date(
            trading_date=trading_date,
            underlying_code=underlying_code,
            is_main=1,
        )

    def get_futures_daily_candles_df(
        self,
        contract_id: str = None,
        underlying_code: str = None,
        is_main: int = 0,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> pd.DataFrame:
        """
        获取期货日频行情数据，返回 DataFrame 格式

        Args:
            contract_id: 合约代码
            underlying_code: 标的代码
            is_main: 是否主力合约
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            包含 OHLCV 数据的 DataFrame，索引为日期
        """
        self._require_pandas("DataYes DataFrame candle helpers")

        quotes = self.get_futures_daily_candles(
            contract_id=contract_id,
            underlying_code=underlying_code,
            is_main=is_main,
            start_date=start_date,
            end_date=end_date
        )

        if not quotes:
            return pd.DataFrame()

        # 转换为 DataFrame
        data = []
        for quote in quotes:
            data.append({
                'date': quote.trade_date,
                'open': quote.open,
                'high': quote.high,
                'low': quote.low,
                'close': quote.close,
                'settle_price': quote.settle_price,
                'pre_settle_price': quote.pre_settle_price,
                'pre_close_price': quote.pre_close_price,
                'volume': quote.volume,
                'turnover': quote.turnover,
                'open_interest': quote.open_interest,
                'limit_up': quote.limit_up,
                'limit_down': quote.limit_down
            })

        df = pd.DataFrame(data)

        # 转换日期列并设置为索引
        df['Date'] = pd.to_datetime(df['date'])
        df.set_index('Date', inplace=True)
        df.drop('date', axis=1, inplace=True)

        # 转换为数值类型
        for col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df.sort_index(inplace=True)
        return df

    def get_main_contract(
        self,
        underlying_code: str,
        trade_date: Optional[datetime] = None
    ) -> Optional[FuturesMainContract]:
        """
        获取期货主力合约信息

        Args:
            underlying_code: 标的代码（如 IF, IC, IH, IM）
            trade_date: 交易日期，默认为最新

        Returns:
            主力合约信息
        """
        if trade_date is None:
            trade_date = datetime.now()

        try:
            # 获取主力合约数据
            records = self._records_from_response(self._query_market_data(
                contractObject=underlying_code,
                mainCon=1,
                tradeDate=trade_date.strftime("%Y%m%d"),
            ))

            if not records:
                return None

            # 获取第一条记录的主力合约代码
            main_contract_code = str(records[0].get('ticker', ''))

            return FuturesMainContract(
                underlying_code=underlying_code,
                main_contract=main_contract_code,
                trade_date=trade_date.strftime("%Y-%m-%d")
            )

        except Exception as e:
            raise RuntimeError(f"Failed to fetch main contract: {e}")

    def get_last_close_price(
        self,
        contract_id: str,
        trading_date: datetime
    ) -> Optional[float]:
        """
        获取合约最后收盘价

        Args:
            contract_id: 合约代码
            trading_date: 交易日期

        Returns:
            最后收盘价，如果无数据返回 None
        """
        quotes = self.get_futures_daily_candles(
            contract_id=contract_id,
            start_date=trading_date - timedelta(days=7),
            end_date=trading_date
        )

        if quotes:
            return quotes[-1].close
        return None

    def get_continuous_candles(
        self,
        underlying_code: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> List[FuturesDailyQuote]:
        """
        获取期货主力连续合约数据

        Args:
            underlying_code: 标的代码
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            主力连续合约日频数据
        """
        return self.get_futures_daily_candles(
            underlying_code=underlying_code,
            is_main=1,
            start_date=start_date,
            end_date=end_date
        )

    def get_continuous_candles_df(
        self,
        underlying_code: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> pd.DataFrame:
        """
        获取期货主力连续合约数据（DataFrame 格式）

        Args:
            underlying_code: 标的代码
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            主力连续合约 DataFrame
        """
        return self.get_futures_daily_candles_df(
            underlying_code=underlying_code,
            is_main=1,
            start_date=start_date,
            end_date=end_date
        )

    # 以下是兼容性方法，保持与原接口一致

    def get_china_futures_contracts(
        self,
        exchange: Optional[str] = None,
        underlying_code: Optional[str] = None
    ) -> List[str]:
        """
        获取期货合约列表（简化版，返回合约代码列表）

        注意：此方法主要用于向后兼容，实际使用建议直接指定 underlying_code

        Args:
            exchange: 交易所代码（暂未使用）
            underlying_code: 标的代码

        Returns:
            合约代码列表
        """
        if not underlying_code:
            raise ValueError("underlying_code is required")

        try:
            # 获取最近的数据来推断合约列表
            end_date = datetime.now()
            start_date = end_date - timedelta(days=30)

            records = self._records_from_response(self._query_market_data(
                contractObject=underlying_code,
                startDate=start_date.strftime("%Y%m%d"),
                endDate=end_date.strftime("%Y%m%d"),
            ))

            if not records:
                return []

            # 返回唯一合约代码列表
            unique_tickers: List[str] = []
            seen = set()
            for row in records:
                ticker = str(row.get("ticker", "")).strip()
                if ticker and ticker not in seen:
                    seen.add(ticker)
                    unique_tickers.append(ticker)

            return unique_tickers

        except Exception as e:
            raise RuntimeError(f"Failed to fetch contracts: {e}")

    # ========== 优化后的方法（支持双价格机制和连续合约） ==========

    def get_futures_daily_candles_optimized(
        self,
        contract_id: str = None,
        underlying_code: str = None,
        is_main: int = 0,
        contract_mark: str = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> List[FuturesDailyQuoteOptimized]:
        """
        获取期货日频行情数据（优化版 - 支持双价格机制和连续合约）

        Args:
            contract_id: 具体合约代码（如 IF2501）
            underlying_code: 标的代码（如 IF）
            is_main: 是否主力合约，1-是，0-否
            contract_mark: 连续合约标志（L0/L1/L2/L3/L4/L6/L9）
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            期货日频行情列表（优化版）
        """
        if end_date is None:
            end_date = datetime.now()
        if start_date is None:
            start_date = end_date - timedelta(days=365)

        params: dict[str, Any] = {}

        # 构建查询参数（根据DataYes API文档）
        if contract_mark:
            # 查询连续合约
            params["contractMark"] = contract_mark
            if underlying_code:
                params["contractObject"] = underlying_code
        elif is_main:
            # 查询主力合约
            params["mainCon"] = 1
            if underlying_code:
                params["contractObject"] = underlying_code
        elif contract_id:
            # 查询具体合约：DataYes API不支持ticker参数
            # 改用contractObject + tradeDate方式查询，然后在结果中筛选
            # 从contract_id中提取品种代码（如m2601 -> M）
            if len(contract_id) > 0 and contract_id[0].isalpha():
                query_underlying = underlying_code or self._extract_underlying_code(contract_id)
                if query_underlying:
                    params["contractObject"] = query_underlying
            else:
                # 如果无法提取，使用underlying_code参数
                if underlying_code:
                    params["contractObject"] = underlying_code
        elif underlying_code:
            # 查询标的下的所有合约
            params["contractObject"] = underlying_code

        params["startDate"] = start_date.strftime("%Y%m%d")
        params["endDate"] = end_date.strftime("%Y%m%d")

        try:
            response = self._query_market_data(**params)

            records = self._prepare_historical_records(response, end_date=end_date, contract_id=contract_id)
            if not records:
                return []

            return [self._build_optimized_quote_from_row(row) for row in records]

            # 过滤数据：T日做决策时只能看到T-1日及之前的数据
            df['tradeDate'] = pd.to_datetime(df['tradeDate'])
            end_dt = pd.to_datetime(end_date)
            df_filtered = df[df['tradeDate'] < end_dt]

            if df_filtered.empty:
                return []

            quotes = []
            for _, row in df_filtered.iterrows():
                quote = FuturesDailyQuoteOptimized(
                    ticker=row.get('ticker', ''),
                    trade_date=str(row.get('tradeDate', '')),
                    sec_short_name=row.get('secShortName'),
                    exchange_cd=row.get('exchangeCD'),
                    pre_settle_price=float(row.get('preSettlePrice', 0)),
                    pre_close_price=float(row.get('preClosePrice', 0)) if pd.notna(row.get('preClosePrice')) else None,
                    open_price=float(row.get('openPrice', 0)),
                    highest_price=float(row.get('highestPrice', 0)),
                    lowest_price=float(row.get('lowestPrice', 0)),
                    close_price=float(row.get('closePrice', 0)),
                    settle_price=float(row.get('settlePrice', 0)) if pd.notna(row.get('settlePrice')) else None,
                    turnover_vol=int(row.get('turnoverVol', 0)),
                    turnover_value=float(row.get('turnoverValue', 0)),
                    open_int=int(row.get('openInt', 0)),
                    chg=float(row.get('chg', 0)),
                    chg1=float(row.get('chg1', 0)) if pd.notna(row.get('chg1')) else None,
                    chg_pct=float(row.get('chgPct', 0)),
                    main_con=int(row.get('mainCon', 0)),
                    smain_con=int(row.get('smainCon', 0)) if pd.notna(row.get('smainCon')) else None,
                    contract_mark=str(row.get('contractMark')) if pd.notna(row.get('contractMark')) else None,
                    contract_object=str(row.get('contractObject')) if pd.notna(row.get('contractObject')) else None
                )
                quotes.append(quote)

            # 如果指定了contract_id，筛选出该合约的数据
            if contract_id and quotes:
                filtered_quotes = [q for q in quotes if q.ticker == contract_id]
                if filtered_quotes:
                    return filtered_quotes
                # 如果没找到匹配的合约，返回所有数据（兼容性处理）

            return quotes

        except Exception as e:
            raise RuntimeError(f"Failed to fetch futures data: {e}")

    def get_main_contract_code(
        self,
        underlying_code: str,
        trading_date: Optional[datetime] = None
    ) -> Optional[str]:
        """
        获取主力合约代码

        Args:
            underlying_code: 标的代码（如 IF、RB、M）
            trading_date: 查询日期

        Returns:
            主力合约代码（如 IF2501）
        """
        if trading_date is None:
            trading_date = datetime.now()

        try:
            # 使用主力标记查询
            records = self._records_from_response(self._query_market_data(
                contractObject=underlying_code,
                mainCon=1,
                tradeDate=trading_date.strftime("%Y%m%d"),
            ))

            if not records:
                return None

            # 返回第一个主力合约代码
            return str(records[0].get('ticker', ''))

        except Exception as e:
            from util.logger import logger
            logger.error(f"Failed to get main contract for {underlying_code}: {e}")
            return None

    def get_continuous_contract_data(
        self,
        underlying_code: str,
        contract_mark: str = "L1",  # 默认使用L1连续合约
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> List[FuturesDailyQuoteOptimized]:
        """
        获取连续合约数据

        Args:
            underlying_code: 标的代码
            contract_mark: 连续标志（L0/L1/L2/L3/L4/L6/L9）
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            连续合约行情数据
        """
        return self.get_futures_daily_candles_optimized(
            underlying_code=underlying_code,
            contract_mark=contract_mark,
            start_date=start_date,
            end_date=end_date
        )

    def get_continuous_settle_price(
        self,
        underlying_code: str,
        trading_date: datetime,
        contract_mark: str = "L1"
    ) -> Optional[float]:
        """
        获取连续合约的结算价

        Args:
            underlying_code: 标的代码
            trading_date: 交易日期
            contract_mark: 连续标志

        Returns:
            结算价（优先），如果没有则返回收盘价
        """
        quotes = self.get_continuous_contract_data(
            underlying_code=underlying_code,
            contract_mark=contract_mark,
            start_date=trading_date - timedelta(days=3),
            end_date=trading_date
        )

        if not quotes:
            return None

        # 查找交易日的数据
        target_date_str = trading_date.strftime("%Y-%m-%d")
        for quote in reversed(quotes):  # 从最新开始查找
            if quote.trade_date == target_date_str:
                # 优先使用结算价
                return quote.settle_price or quote.close_price

        # 如果没找到确切日期，使用最新数据
        latest_quote = quotes[-1]
        return latest_quote.settle_price or latest_quote.close_price
