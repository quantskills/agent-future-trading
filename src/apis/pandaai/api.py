from __future__ import annotations

"""PandaAI API client implementation for Chinese futures market."""

import math
import os
import re
import sqlite3
import threading
import time
import importlib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional

try:
    import pandas as pd
    _PANDAS_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - depends on runtime environment
    pd = None
    _PANDAS_IMPORT_ERROR = exc

from apis.pandaai.api_model import FuturesDailyQuote, FuturesDailyQuoteOptimized, FuturesMainContract, FuturesMargin
from util.logger import logger


_PANDAAI_GATEWAY_STATUS_CODES = frozenset({502, 503, 504})
_PANDAAI_RATE_LIMIT_STATUS_CODES = frozenset({429})
_PANDAAI_RATE_LIMIT_SERVICE_CODES = frozenset({500001, 500002, 500003, 500006})
_PANDAAI_TRANSIENT_SERVICE_CODES = frozenset({400002, 500004, 500005, 900001})


def _exception_chain(exc: BaseException):
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _exception_numeric_values(exc: BaseException) -> set[int]:
    values: set[int] = set()
    for current in _exception_chain(exc):
        candidates = (
            getattr(current, "status_code", None),
            getattr(current, "status", None),
            getattr(current, "code", None),
            getattr(getattr(current, "response", None), "status_code", None),
        )
        for value in candidates:
            try:
                values.add(int(str(value).strip()))
            except (TypeError, ValueError):
                continue
    return values


def _exception_messages(exc: BaseException) -> list[str]:
    return [str(current).lower() for current in _exception_chain(exc)]


def is_pandaai_gateway_error(exc: BaseException) -> bool:
    """Classify retryable PandaAI HTTP gateway failures without exposing details."""
    if _exception_numeric_values(exc) & _PANDAAI_GATEWAY_STATUS_CODES:
        return True
    for lowered in _exception_messages(exc):
        if any(
            marker in lowered
            for marker in (
                "bad gateway",
                "service unavailable",
                "gateway timeout",
            )
        ):
            return True
        if re.search(r"\bhttp(?:\s+error)?\s*[:=]?\s*(?:502|503|504)\b", lowered):
            return True
        if re.search(r"\bstatus(?:\s+code)?\s*[:=]\s*(?:502|503|504)\b", lowered):
            return True
    return False


class PandaAIAPI:
    """PandaAI API wrapper with the futures methods AgentQuant already uses."""

    _request_lock = threading.Lock()
    _last_request_at = 0.0
    _rate_limit_cooldown_until = 0.0
    _shared_history_cache: dict[tuple, list[dict[str, Any]]] = {}
    _shared_quote_cache: dict[tuple, list[dict[str, Any]]] = {}
    _shared_minute_cache: dict[tuple, list[dict[str, Any]]] = {}
    _shared_extra_cache: dict[tuple, list[dict[str, Any]]] = {}
    _shared_extra_diagnostics_cache: dict[tuple, dict[str, Any]] = {}
    _shared_unavailable_extra_feature_cache: dict[tuple, dict[str, Any]] = {}
    _shared_exchange_suffix_cache: dict[tuple, str] = {}
    _shared_sdk_method_aliases: dict[str, str] = {}
    _shared_token_initialized = False
    _shared_sdk_user_cache_configured = False
    _shared_token_lock = threading.Lock()
    _market_cache_db_initialized = False
    _market_cache_db_lock = threading.Lock()

    RATE_LIMIT_ERROR_CODE = "500010"
    RATE_LIMIT_ERROR_TEXT = "\u8bf7\u6c42\u6b21\u6570\u8d85\u9650"

    EXCHANGE_SUFFIX_BY_EXCHANGE = {
        "CFFEX": "CFE",
        "CZCE": "CZC",
        "DCE": "DCE",
        "GFEX": "GFEX",
        "INE": "INE",
        "SHFE": "SHF",
        "XCFE": "CFE",
        "XZCE": "CZC",
        "XDCE": "DCE",
        "XSHG": "SHF",
    }

    FALLBACK_SUFFIX_BY_UNDERLYING = {
        "BU": "SHF",
        "C": "DCE",
        "CF": "CZC",
        "EB": "DCE",
        "HC": "SHF",
        "I": "DCE",
        "J": "DCE",
        "M": "DCE",
        "MA": "CZC",
        "P": "DCE",
        "PB": "SHF",
        "RB": "SHF",
        "SR": "CZC",
        "TA": "CZC",
        "ZN": "SHF",
    }

    def __init__(self):
        from dotenv import load_dotenv

        env_path = os.path.join(os.path.dirname(__file__), "../../../.env")
        load_dotenv(env_path)

        self.username = os.environ.get("PANDAAI_USERNAME")
        self.password = os.environ.get("PANDAAI_PASSWORD")
        if not self.username or not self.password:
            raise RuntimeError("Missing PANDAAI_USERNAME or PANDAAI_PASSWORD")

        self._panda_data = None
        self._token_initialized = False
        self._history_cache = self.__class__._shared_history_cache
        self._quote_cache = self.__class__._shared_quote_cache
        self._minute_cache = self.__class__._shared_minute_cache
        self._extra_cache = self.__class__._shared_extra_cache
        self._extra_diagnostics_cache = self.__class__._shared_extra_diagnostics_cache
        self._unavailable_extra_feature_cache = self.__class__._shared_unavailable_extra_feature_cache
        self._exchange_suffix_cache = self.__class__._shared_exchange_suffix_cache
        self._min_request_interval_seconds = self._env_float("PANDAAI_MIN_REQUEST_INTERVAL_SECONDS", 1.35)
        self._retry_initial_wait_seconds = self._env_float("PANDAAI_RETRY_INITIAL_WAIT_SECONDS", 30.0)
        self._retry_max_wait_seconds = self._env_float("PANDAAI_RETRY_MAX_WAIT_SECONDS", 90.0)
        self._network_retry_initial_wait_seconds = self._env_float("PANDAAI_NETWORK_RETRY_INITIAL_WAIT_SECONDS", 3.0)
        self._network_retry_max_wait_seconds = self._env_float("PANDAAI_NETWORK_RETRY_MAX_WAIT_SECONDS", 12.0)
        self._sdk_method_aliases = self.__class__._shared_sdk_method_aliases
        self._persistent_market_cache_enabled = self._env_bool("PANDAAI_PERSISTENT_MARKET_CACHE", True)
        self._market_cache_db_path = self._resolve_market_cache_db_path()
        self._sdk_user_cache_root = self._resolve_sdk_user_cache_root()

    def _env_float(self, name: str, default: float) -> float:
        try:
            return max(0.0, float(os.environ.get(name, default)))
        except Exception:
            return default

    def _env_bool(self, name: str, default: bool) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}

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

    def _resolve_market_cache_db_path(self) -> Path:
        raw = os.environ.get("PANDAAI_MARKET_CACHE_DB")
        if raw:
            return Path(raw)
        return Path(__file__).resolve().parents[2] / "assets" / "pandaai_market_cache.db"

    def _resolve_sdk_user_cache_root(self) -> Path:
        raw = os.environ.get("PANDAAI_SDK_USER_CACHE_DIR") or os.environ.get("PANDAAI_USER_CACHE_DIR")
        if raw:
            return Path(raw)
        local_app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if local_app_data:
            return Path(local_app_data) / "AgentQuant" / "pandaai_sdk_auth"
        return Path.home() / ".agentquant" / "pandaai_sdk_auth"

    def _configure_panda_data_user_cache(self) -> None:
        """Keep PandaAI SDK token cache inside AgentQuant's writable runtime area."""
        if self.__class__._shared_sdk_user_cache_configured:
            return
        cache_root = getattr(self, "_sdk_user_cache_root", None)
        if cache_root is None:
            cache_root = self._resolve_sdk_user_cache_root()
            self._sdk_user_cache_root = cache_root
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.warning("pandaai_sdk_cache_unavailable")
            return

        cache_root_text = str(cache_root)

        def _agentquant_sdk_project_root(current_path: str, markers: list | None = None) -> str:
            return cache_root_text

        for module_name in (
            "panda_data.utils.common_utils",
            "panda_data.readers.init_token",
            "panda_data.transport.http",
            "panda_data.readers.future_reader",
            "panda_data.client",
        ):
            try:
                module = importlib.import_module(module_name)
            except Exception:
                continue
            if hasattr(module, "find_project_root"):
                setattr(module, "find_project_root", _agentquant_sdk_project_root)

        os.environ.setdefault("PANDAAI_SDK_USER_CACHE_DIR", cache_root_text)
        self.__class__._shared_sdk_user_cache_configured = True

    def _ensure_token(self) -> None:
        if self._panda_data is None:
            try:
                import panda_data
            except ImportError as exc:
                raise ImportError("panda-data SDK not found. Please install it using: pip install panda-data") from exc
            self._panda_data = panda_data
        self._configure_panda_data_user_cache()

        if self._token_initialized or self.__class__._shared_token_initialized:
            self._token_initialized = True
            return

        with self.__class__._shared_token_lock:
            if not self.__class__._shared_token_initialized:
                self._panda_data.init_token(username=self.username, password=self.password)
                self.__class__._shared_token_initialized = True
            self._token_initialized = True

    def _refresh_token_after_expiry(self) -> None:
        """Re-authenticate the shared PandaAI SDK token after provider expiry."""
        if self._panda_data is None:
            try:
                import panda_data
            except ImportError as exc:
                raise ImportError("panda-data SDK not found. Please install it using: pip install panda-data") from exc
            self._panda_data = panda_data
        self._configure_panda_data_user_cache()

        with self.__class__._shared_token_lock:
            self._panda_data.init_token(username=self.username, password=self.password)
            self.__class__._shared_token_initialized = True
            self._token_initialized = True

    def _sdk_cache_namespace(self) -> tuple[str, int, str]:
        module = self._panda_data
        if module is None:
            try:
                import panda_data
            except ImportError as exc:
                raise ImportError("panda-data SDK not found. Please install it using: pip install panda-data") from exc
            self._panda_data = panda_data
            module = self._panda_data
        return (
            str(getattr(module, "__name__", "panda_data")),
            id(module),
            str(getattr(module, "__file__", "")),
        )

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        numeric_values = _exception_numeric_values(exc)
        if numeric_values & (_PANDAAI_RATE_LIMIT_STATUS_CODES | _PANDAAI_RATE_LIMIT_SERVICE_CODES):
            return True
        for lowered in _exception_messages(exc):
            if (
                self.RATE_LIMIT_ERROR_CODE in lowered
                or self.RATE_LIMIT_ERROR_TEXT in lowered
                or any(str(code) in lowered for code in _PANDAAI_RATE_LIMIT_SERVICE_CODES)
                or "rate limit" in lowered
                or "too many requests" in lowered
                or "请求频率超限" in lowered
                or "热点参数限流" in lowered
                or "ip请求频率超限" in lowered
                or "并发请求数超限" in lowered
            ):
                return True
        return False

    def _is_transient_network_error(self, exc: Exception) -> bool:
        if is_pandaai_gateway_error(exc):
            return True
        if _exception_numeric_values(exc) & _PANDAAI_TRANSIENT_SERVICE_CODES:
            return True
        transient_types = (
            TimeoutError,
            ConnectionError,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
        )
        markers = (
            "winerror 10048",
            "only one usage of each socket address",
            "socket address",
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
            "connection refused",
            "remote end closed connection",
            "temporary failure in name resolution",
            "getaddrinfo failed",
            "timed out",
            "urlopen error",
            "broken pipe",
            "数据查询超时",
            "服务降级中",
            "服务熔断中",
            "系统异常，请稍后重试",
        )
        for current in _exception_chain(exc):
            if isinstance(current, transient_types):
                return True
            if any(marker in str(current).lower() for marker in markers):
                return True
        return False

    def _is_token_expired_error(self, exc: Exception) -> bool:
        message = str(exc)
        lowered = message.lower()
        return (
            "200004" in message
            or "token已过期" in lowered
            or "token 已过期" in lowered
            or "token expired" in lowered
            or ("token" in lowered and ("过期" in message or "expired" in lowered))
        )

    def _wait_for_request_slot(self) -> None:
        interval = max(0.0, self._min_request_interval_seconds)

        with self.__class__._request_lock:
            now = time.monotonic()
            next_allowed_at = self.__class__._rate_limit_cooldown_until
            if interval > 0:
                next_allowed_at = max(next_allowed_at, self.__class__._last_request_at + interval)
            wait_seconds = next_allowed_at - now
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self.__class__._last_request_at = time.monotonic()

    def _call_pandaai(self, method_name: str, **kwargs):
        self._ensure_token()
        sdk_method_name = self._resolve_sdk_method_name(method_name)
        method = getattr(self._panda_data, sdk_method_name, None)
        if method is None:
            raise AttributeError(f"PandaAI SDK method is unavailable: {method_name}")

        wait_seconds = self._retry_initial_wait_seconds
        network_wait_seconds = self._network_retry_initial_wait_seconds
        token_refreshed = False
        while True:
            self._wait_for_request_slot()
            try:
                return method(**kwargs)
            except Exception as exc:
                if self._is_token_expired_error(exc) and not token_refreshed:
                    logger.warning("pandaai_token_refresh_required")
                    self._refresh_token_after_expiry()
                    sdk_method_name = self._resolve_sdk_method_name(method_name)
                    method = getattr(self._panda_data, sdk_method_name, None)
                    if method is None:
                        raise AttributeError(f"PandaAI SDK method is unavailable: {method_name}") from exc
                    token_refreshed = True
                    continue
                is_rate_limited = self._is_rate_limit_error(exc)
                is_transient_network = self._is_transient_network_error(exc)
                if not is_rate_limited and not is_transient_network:
                    raise
                if is_transient_network:
                    logger.warning("pandaai_transient_request_retry")
                    with self.__class__._request_lock:
                        self.__class__._rate_limit_cooldown_until = max(
                            self.__class__._rate_limit_cooldown_until,
                            time.monotonic() + network_wait_seconds,
                        )
                    time.sleep(network_wait_seconds)
                    network_wait_seconds = min(
                        self._network_retry_max_wait_seconds,
                        max(network_wait_seconds * 2.0, 1.0),
                    )
                    continue
                logger.warning("pandaai_rate_limit_retry")
                with self.__class__._request_lock:
                    self.__class__._rate_limit_cooldown_until = max(
                        self.__class__._rate_limit_cooldown_until,
                        time.monotonic() + wait_seconds,
                )
                time.sleep(wait_seconds)
                wait_seconds = min(self._retry_max_wait_seconds, max(wait_seconds * 2.0, 1.0))

    def _ensure_market_cache_db(self) -> None:
        if not self._persistent_market_cache_enabled:
            return
        if self.__class__._market_cache_db_initialized and self._market_cache_db_path.exists():
            return
        with self.__class__._market_cache_db_lock:
            if self.__class__._market_cache_db_initialized and self._market_cache_db_path.exists():
                return
            self._market_cache_db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._market_cache_db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pandaai_market_data_cache (
                        symbol TEXT NOT NULL,
                        start_date TEXT NOT NULL,
                        end_date TEXT NOT NULL,
                        records_json TEXT NOT NULL,
                        row_count INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (symbol, start_date, end_date)
                    )
                    """
                )
                conn.commit()
            self.__class__._market_cache_db_initialized = True

    def _market_cache_records_match_key(
        self,
        records: list[dict[str, Any]],
        symbol: str,
        start_key: str,
        end_key: str,
    ) -> bool:
        if not records:
            return False
        try:
            start_date = datetime.strptime(start_key, "%Y%m%d")
            end_date = datetime.strptime(end_key, "%Y%m%d")
        except ValueError:
            return False
        expected_symbol = str(symbol or "").strip().upper()
        if not expected_symbol:
            return False
        for record in records:
            if not isinstance(record, dict):
                return False
            record_symbol = str(record.get("symbol") or "").strip().upper()
            record_date = self._parse_trade_date(record.get("date"))
            if record_symbol != expected_symbol:
                return False
            if record_date is None or record_date < start_date or record_date > end_date:
                return False
        return True

    def _read_persistent_market_cache(self, symbol: str, start_key: str, end_key: str) -> Optional[list[dict[str, Any]]]:
        if not self._persistent_market_cache_enabled:
            return None
        self._ensure_market_cache_db()
        try:
            with sqlite3.connect(self._market_cache_db_path, timeout=30.0) as conn:
                row = conn.execute(
                    """
                    SELECT records_json, row_count
                    FROM pandaai_market_data_cache
                    WHERE symbol = ? AND start_date = ? AND end_date = ?
                    """,
                    (symbol.upper(), start_key, end_key),
                ).fetchone()
            if not row:
                return None
            import json

            records = json.loads(row[0])
            if isinstance(records, list):
                normalized = [dict(item) for item in records if isinstance(item, dict)]
                if int(row[1]) == len(normalized) and self._market_cache_records_match_key(
                    normalized,
                    symbol,
                    start_key,
                    end_key,
                ):
                    return normalized
                logger.warning("pandaai_market_cache_record_invalid")
        except Exception:
            logger.warning("pandaai_market_cache_read_failed")
        return None

    def _write_persistent_market_cache(
        self,
        symbol: str,
        start_key: str,
        end_key: str,
        records: list[dict[str, Any]],
    ) -> None:
        if not self._persistent_market_cache_enabled:
            return
        if not self._market_cache_records_match_key(records, symbol, start_key, end_key):
            logger.warning("pandaai_market_cache_record_invalid")
            return
        self._ensure_market_cache_db()
        try:
            import json

            with sqlite3.connect(self._market_cache_db_path, timeout=30.0) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO pandaai_market_data_cache (
                        symbol, start_date, end_date, records_json, row_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol.upper(),
                        start_key,
                        end_key,
                        json.dumps(records, ensure_ascii=False, default=str),
                        len(records),
                        datetime.utcnow().isoformat(),
                    ),
                )
                conn.commit()
        except Exception:
            logger.warning("pandaai_market_cache_write_failed")

    def _resolve_sdk_method_name(self, method_name: str) -> str:
        """Map documented/legacy PandaAI names to the installed SDK names."""
        self._ensure_token()
        if hasattr(self._panda_data, method_name):
            return method_name
        if method_name in self._sdk_method_aliases:
            return self._sdk_method_aliases[method_name]

        aliases = {
            "get_future_market_post": "get_future_daily_post",
            "get_future_wr": "get_future_warehouse_receipt",
            "get_future_variety_posi_rank": "get_future_variety_posi",
            "get_future_symbol_posi_rank": "get_future_symbol_posi",
            "get_broker_net_margin": "get_broker_netmarg",
            "get_broker_net_margin_change": "get_broker_netmarg_change",
            "get_broker_total_margin": "get_broker_totlmarg",
            "get_future_net_cap_change": "get_future_netcap_change",
            "get_future_contract_daily_indicators": "get_future_contract_indicators",
        }
        candidate = aliases.get(method_name)
        if candidate and hasattr(self._panda_data, candidate):
            self._sdk_method_aliases[method_name] = candidate
            return candidate
        return method_name

    def _query_market_data(self, symbol: str, start_date: datetime, end_date: datetime) -> list[dict[str, Any]]:
        start_key = start_date.strftime("%Y%m%d")
        end_key = end_date.strftime("%Y%m%d")
        cache_key = (self._sdk_cache_namespace(), symbol.upper(), start_key, end_key)
        if cache_key in self._history_cache:
            return list(self._history_cache[cache_key])

        cached_records = self._read_persistent_market_cache(symbol, start_key, end_key)
        if cached_records is not None:
            self._history_cache[cache_key] = cached_records
            return list(cached_records)

        response = self._call_pandaai(
            "get_market_data",
            symbol=symbol,
            start_date=start_key,
            end_date=end_key,
            type="future",
            fields=[],
            indicator="",
            st=None,
        )
        records = self._records_from_response(response)
        self._history_cache[cache_key] = records
        self._write_persistent_market_cache(symbol, start_key, end_key, records)
        return list(records)

    def _query_market_min_data(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        frequency: str = "15m",
        time_zone: Optional[Any] = None,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Query PandaAI futures minute bars and cache the raw records."""
        frequency = str(frequency or "15m").lower()
        if frequency not in {"1m", "5m", "15m", "60m"}:
            raise ValueError(f"Unsupported PandaAI futures minute frequency: {frequency}")

        start_key = start_date.strftime("%Y%m%d")
        end_key = end_date.strftime("%Y%m%d")
        cache_key = (self._sdk_cache_namespace(), symbol.upper(), start_key, end_key, frequency, str(time_zone or ""))
        if not refresh and cache_key in self._minute_cache:
            return list(self._minute_cache[cache_key])

        response = self._call_pandaai(
            "get_market_min_data",
            symbol=[symbol],
            start_date=start_key,
            end_date=end_key,
            symbol_type="future",
            fields=[],
            frequency=frequency,
            time_zone=time_zone,
        )
        records = self._records_from_response(response)
        if not refresh:
            self._minute_cache[cache_key] = records
        return list(records)

    def _query_exact_quote(self, symbol: str, trading_date: datetime) -> list[dict[str, Any]]:
        date_key = trading_date.strftime("%Y%m%d")
        cache_key = (self._sdk_cache_namespace(), symbol.upper(), date_key)
        if cache_key in self._quote_cache:
            return list(self._quote_cache[cache_key])

        rows = self._query_market_data(symbol=symbol, start_date=trading_date, end_date=trading_date)
        target = trading_date.replace(hour=0, minute=0, second=0, microsecond=0)
        exact_rows = [row for row in rows if self._parse_trade_date(row.get("date")) == target]
        self._quote_cache[cache_key] = exact_rows
        return list(exact_rows)

    def _query_extra_data(self, method_name: str, **kwargs) -> list[dict[str, Any]]:
        result = self._query_extra_data_with_diagnostic(method_name, **kwargs)
        return list(result["records"])

    def _query_extra_data_with_diagnostic(self, method_name: str, **kwargs) -> dict[str, Any]:
        normalized_kwargs = tuple(
            sorted(
                (key, tuple(value) if isinstance(value, list) else value)
                for key, value in kwargs.items()
            )
        )
        cache_key = (self._sdk_cache_namespace(), method_name, normalized_kwargs)
        if cache_key in self._extra_cache:
            diagnostic = dict(self._extra_diagnostics_cache.get(cache_key, {}))
            diagnostic["records"] = list(self._extra_cache[cache_key])
            return diagnostic

        unavailable_key = self._extra_unavailable_key(method_name, kwargs)
        if unavailable_key in self._unavailable_extra_feature_cache:
            diagnostic = dict(self._unavailable_extra_feature_cache[unavailable_key])
            diagnostic["records"] = []
            diagnostic["params"] = dict(kwargs)
            self._extra_cache[cache_key] = []
            self._extra_diagnostics_cache[cache_key] = dict(diagnostic)
            return diagnostic

        self._ensure_token()
        sdk_method_name = self._resolve_sdk_method_name(method_name)
        method = getattr(self._panda_data, sdk_method_name, None)
        if method is None:
            logger.warning("pandaai_extra_method_unavailable")
            self._extra_cache[cache_key] = []
            status = "unsupported_feature"
            reason = "sdk_method_unavailable"
            diagnostic = {
                "records": [],
                "status": status,
                "reason": reason,
                "error": f"PandaAI SDK method is unavailable: {method_name}",
                "method": method_name,
                "sdk_method": sdk_method_name,
                "params": dict(kwargs),
                "row_count": 0,
            }
            self._extra_diagnostics_cache[cache_key] = diagnostic
            self._unavailable_extra_feature_cache[unavailable_key] = {
                "records": [],
                "status": status,
                "reason": reason,
                "error": diagnostic["error"],
                "method": method_name,
                "sdk_method": sdk_method_name,
                "row_count": 0,
                "cached_unavailable": True,
            }
            return dict(diagnostic)

        try:
            response = self._call_pandaai(method_name, **kwargs)
            records = self._records_from_response(response)
            status = "ok" if records else "no_data"
            reason = None if records else "empty_response"
            self._extra_cache[cache_key] = records
            diagnostic = {
                "records": list(records),
                "status": status,
                "reason": reason,
                "error": None,
                "method": method_name,
                "sdk_method": sdk_method_name,
                "params": dict(kwargs),
                "row_count": len(records),
            }
            self._extra_diagnostics_cache[cache_key] = diagnostic
            return dict(diagnostic)
        except Exception as exc:
            status, reason = self._classify_extra_data_error(exc)
            logger.warning("pandaai_extra_data_unavailable")
            self._extra_cache[cache_key] = []
            diagnostic = {
                "records": [],
                "status": status,
                "reason": reason,
                "error": str(exc),
                "method": method_name,
                "sdk_method": sdk_method_name,
                "params": dict(kwargs),
                "row_count": 0,
            }
            self._extra_diagnostics_cache[cache_key] = diagnostic
            return dict(diagnostic)

    def _classify_extra_data_error(self, exc: Exception) -> tuple[str, str]:
        message = str(exc)
        lowered = message.lower()
        if "200103" in message or "访问权限不足" in message or "access permission" in lowered or "api permission" in lowered:
            return "permission_error", "account_permission_denied"
        if "参数不能为空" in message or "required" in lowered or "missing" in lowered:
            return "parameter_error", "required_parameter_missing"
        if "unsupported" in lowered or "not support" in lowered or "不支持" in message:
            return "unsupported_feature", "provider_or_symbol_not_supported"
        if "permission" in lowered or "unauthorized" in lowered or "无权限" in message:
            return "permission_error", "account_permission_denied"
        if "403" in message or "forbidden" in lowered:
            return "permission_error", "http_403_or_icp_block"
        if "network" in lowered or "timed out" in lowered or "timeout" in lowered:
            return "provider_error", "network_or_timeout"
        if self._is_token_expired_error(exc):
            return "provider_error", "token_expired_after_reauth"
        if self._is_rate_limit_error(exc):
            return "provider_error", "rate_limited"
        return "provider_error", "provider_exception"

    def _extra_unavailable_key(self, method_name: str, kwargs: dict[str, Any]) -> tuple:
        """Cache hard field unavailability without hiding ordinary date-specific no-data."""
        scope_parts = [self._sdk_cache_namespace(), method_name]
        for key in (
            "underlying_symbol",
            "symbol",
            "position_type",
            "broker",
            "broker_name",
            "rank_type",
            "type",
        ):
            value = kwargs.get(key)
            if value not in (None, "", [], ()):
                if isinstance(value, list):
                    value = tuple(value)
                scope_parts.append((key, value))
        return tuple(scope_parts)

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
                    keys = list(response.keys())
                    return [{key: response[key][index] for key in keys} for index in range(next(iter(lengths), 0))]

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
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())

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

    def _parse_minute_datetime(self, row: dict[str, Any]) -> Optional[datetime]:
        value = row.get("datetime")
        if value is not None and str(value).strip():
            text = str(value).strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y%m%d %H%M%S"):
                try:
                    return datetime.strptime(text[:19], fmt)
                except ValueError:
                    continue

        trade_date = self._parse_trade_date(row.get("date") or row.get("trading_date"))
        if trade_date is None:
            return None

        minute_text = str(row.get("minute") or "").strip()
        minute_digits = re.sub(r"\D", "", minute_text)
        if not minute_digits:
            return trade_date
        minute_digits = minute_digits.zfill(6)[-6:]
        try:
            hour = int(minute_digits[0:2])
            minute = int(minute_digits[2:4])
            second = int(minute_digits[4:6])
            return trade_date.replace(hour=hour, minute=minute, second=second, microsecond=0)
        except Exception:
            return trade_date

    def _normalize_datetime(self, value: Optional[Any], default: Optional[datetime] = None) -> datetime:
        if value is None:
            value = default or datetime.now()
        parsed = self._parse_trade_date(value)
        if parsed is None:
            raise ValueError(f"Invalid date value: {value!r}")
        return parsed

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
        return str(value).strip().lower() in {"", "nan", "none", "null"}

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

    def _calc_pct(self, current: Any, base: Any) -> float:
        current_value = self._coerce_optional_float(current)
        base_value = self._coerce_optional_float(base)
        if current_value is None or base_value in (None, 0):
            return 0.0
        return (current_value - base_value) / base_value

    def _extract_underlying_code(self, contract_id: str) -> Optional[str]:
        if not contract_id:
            return None
        symbol = str(contract_id).split(".", 1)[0]
        symbol = symbol.replace("_DOMINANT", "")
        match = re.match(r"^([A-Za-z_]+)", symbol)
        return match.group(1).upper() if match else None

    def _normalize_internal_contract(self, symbol_or_code: Any) -> str:
        if self._is_missing(symbol_or_code):
            return ""
        symbol = str(symbol_or_code).split(".", 1)[0]
        return symbol.replace("_DOMINANT", "").lower()

    def _canonical_business_contract_code(
        self,
        row: dict[str, Any],
        reference_date: Optional[Any],
    ) -> str:
        """Return the single concrete contract identity exposed to AgentQuant."""
        record = self._coerce_record(row)
        raw_symbol = record.get("symbol")
        symbol_text = "" if self._is_missing(raw_symbol) else str(raw_symbol).strip()
        if symbol_text and "_DOMINANT" not in symbol_text.upper():
            candidate = symbol_text
        elif not self._is_missing(record.get("dominant_id")):
            candidate = str(record.get("dominant_id")).strip()
        elif not self._is_missing(record.get("trading_code")):
            candidate = str(record.get("trading_code")).strip()
        else:
            raise ValueError("PandaAI business contract code is missing")

        raw_code, _, raw_suffix = candidate.partition(".")
        raw_code = raw_code.strip()
        if "_DOMINANT" in raw_code.upper():
            raise ValueError("PandaAI dominant symbol is not a business contract code")
        match = re.fullmatch(r"([A-Za-z]+)(\d{3,4})", raw_code)
        if not match:
            raise ValueError(f"Invalid PandaAI business contract code: {candidate}")

        underlying, contract_number = match.groups()
        underlying = underlying.upper()
        if len(contract_number) == 4:
            return f"{underlying}{contract_number}"

        candidate_exchange = str(raw_suffix or "").upper().strip()
        candidate_exchange = self.EXCHANGE_SUFFIX_BY_EXCHANGE.get(
            candidate_exchange,
            candidate_exchange,
        )
        row_exchange = str(record.get("exchange") or "").upper().strip()
        row_exchange = self.EXCHANGE_SUFFIX_BY_EXCHANGE.get(row_exchange, row_exchange)
        configured_exchange = self.FALLBACK_SUFFIX_BY_UNDERLYING.get(underlying, "")
        if "CZC" not in {candidate_exchange, row_exchange, configured_exchange}:
            raise ValueError(
                f"PandaAI three-digit contract code is only valid for CZCE: {candidate}"
            )

        record_date = self._parse_trade_date(reference_date)
        if record_date is None:
            raise ValueError(
                f"reference_date is required for PandaAI CZCE contract code: {candidate}"
            )
        expanded_number = self._expand_short_contract_number(
            contract_number,
            reference_date=record_date,
        )
        if len(expanded_number) != 4:
            raise ValueError(f"Invalid PandaAI CZCE contract month: {candidate}")
        return f"{underlying}{expanded_number}"

    def _contract_match_keys(
        self,
        symbol_or_code: Any,
        reference_date: Optional[datetime] = None,
    ) -> set[str]:
        """Return equivalent contract keys for PandaAI full and exchange-short codes.

        CZCE rows commonly return trading_code like CF505/TA505 even when the
        query symbol is CF2505.CZC/TA2505.CZC.  These are the same contract and
        must not be filtered out as a missing exact quote.
        """
        normalized = self._normalize_internal_contract(symbol_or_code)
        if not normalized:
            return set()
        keys = {normalized}
        match = re.match(r"^([a-z_]+)(\d+)$", normalized)
        if not match:
            return keys
        prefix, number = match.groups()
        if len(number) == 4 and number[:2].isdigit() and number[2:].isdigit():
            keys.add(f"{prefix}{number[1:]}")
        elif len(number) == 3 and number.isdigit():
            expanded = self._expand_short_contract_number(number, reference_date=reference_date)
            keys.add(f"{prefix}{expanded.lower()}")
        return keys

    def _row_contract_match_keys(
        self,
        row: dict[str, Any],
        reference_date: Optional[datetime] = None,
    ) -> set[str]:
        keys: set[str] = set()
        for value in (row.get("trading_code"), row.get("dominant_id"), row.get("symbol")):
            keys.update(self._contract_match_keys(value, reference_date=reference_date))
        return keys

    def _row_matches_contract(
        self,
        row: dict[str, Any],
        contract_id: str,
        reference_date: Optional[datetime] = None,
    ) -> bool:
        contract_keys = self._contract_match_keys(contract_id, reference_date=reference_date)
        row_keys = self._row_contract_match_keys(row, reference_date=reference_date)
        return bool(contract_keys and row_keys and contract_keys.intersection(row_keys))

    def _load_exchange_suffix_cache(self) -> None:
        namespace = self._sdk_cache_namespace()
        if any(isinstance(key, tuple) and key[0] == namespace for key in self._exchange_suffix_cache):
            return

        try:
            response = self._call_pandaai(
                "get_future_detail",
                symbol=None,
                fields=["symbol", "exchange", "underlying_symbol"],
                is_trading=1,
            )
            for row in self._records_from_response(response):
                underlying = str(row.get("underlying_symbol") or "").upper().strip()
                exchange = str(row.get("exchange") or "").upper().strip()
                suffix = self.EXCHANGE_SUFFIX_BY_EXCHANGE.get(exchange, exchange)
                if underlying and suffix:
                    self._exchange_suffix_cache[(namespace, underlying)] = suffix
        except Exception:
            for key in list(self._exchange_suffix_cache):
                if isinstance(key, tuple) and key[0] == namespace:
                    self._exchange_suffix_cache.pop(key, None)

    def _resolve_pandaai_suffix(self, underlying_code: str) -> str:
        underlying = underlying_code.upper()
        if underlying in self.FALLBACK_SUFFIX_BY_UNDERLYING:
            return self.FALLBACK_SUFFIX_BY_UNDERLYING[underlying]
        self._load_exchange_suffix_cache()
        cache_key = (self._sdk_cache_namespace(), underlying)
        if cache_key in self._exchange_suffix_cache:
            return self._exchange_suffix_cache[cache_key]
        if underlying in self.FALLBACK_SUFFIX_BY_UNDERLYING:
            return self.FALLBACK_SUFFIX_BY_UNDERLYING[underlying]
        raise RuntimeError(f"Unable to resolve PandaAI exchange suffix for {underlying_code}")

    def _dominant_symbol(self, underlying_code: str) -> str:
        underlying = underlying_code.upper()
        return f"{underlying}_DOMINANT.{self._resolve_pandaai_suffix(underlying)}"

    def _expand_short_contract_number(self, number: str, reference_date: Optional[datetime] = None) -> str:
        """Convert exchange short year-month codes like 601 to PandaAI's 2601."""
        if len(number) != 3 or not number.isdigit():
            return number

        ref = reference_date or datetime.now()
        year_digit = int(number[0])
        month = int(number[1:])
        if month < 1 or month > 12:
            return number

        candidates = [year for year in range(ref.year - 5, ref.year + 6) if year % 10 == year_digit]
        if not candidates:
            return number

        def month_distance(year: int) -> int:
            return abs((year - ref.year) * 12 + (month - ref.month))

        full_year = min(candidates, key=month_distance)
        return f"{full_year % 100:02d}{month:02d}"

    def _expand_contract_for_pandaai(self, contract_code: str, reference_date: Optional[datetime] = None) -> str:
        match = re.match(r"^([A-Za-z_]+)(\d+)$", contract_code)
        if not match:
            return contract_code
        prefix, number = match.groups()
        return f"{prefix}{self._expand_short_contract_number(number, reference_date=reference_date)}"

    def _contract_symbol(
        self,
        contract_id: str,
        underlying_code: Optional[str] = None,
        reference_date: Optional[datetime] = None,
    ) -> str:
        if not contract_id:
            raise ValueError("contract_id is required")
        contract_text = str(contract_id).strip()
        if "." in contract_text:
            raw_code, suffix = contract_text.split(".", 1)
            expanded_code = self._expand_contract_for_pandaai(raw_code, reference_date=reference_date)
            return f"{expanded_code.upper()}.{suffix.upper()}"
        underlying = (underlying_code or self._extract_underlying_code(contract_text) or "").upper()
        suffix = self._resolve_pandaai_suffix(underlying)
        expanded_code = self._expand_contract_for_pandaai(contract_text, reference_date=reference_date)
        return f"{expanded_code.upper()}.{suffix}"

    def _symbol_for_query(
        self,
        contract_id: Optional[str] = None,
        underlying_code: Optional[str] = None,
        is_main: int = 0,
        contract_mark: Optional[str] = None,
        reference_date: Optional[datetime] = None,
    ) -> str:
        if contract_id:
            if "_DOMINANT" in str(contract_id).upper():
                return str(contract_id).upper()
            return self._contract_symbol(contract_id, underlying_code=underlying_code, reference_date=reference_date)
        if underlying_code:
            return self._dominant_symbol(underlying_code)
        raise ValueError("Either contract_id or underlying_code is required")

    def get_futures_extra_snapshot(
        self,
        underlying_code: str,
        reference_date: datetime,
        lookback_days: int = 5,
        contract_id: Optional[str] = None,
        features: Optional[dict[str, bool]] = None,
    ) -> dict[str, Any]:
        """Load optional PandaAI futures non-market data up to a pre-open cutoff date."""

        reference_dt = self._normalize_datetime(reference_date)
        start_dt = reference_dt - timedelta(days=max(lookback_days * 3, lookback_days + 2))
        start_key = start_dt.strftime("%Y%m%d")
        end_key = reference_dt.strftime("%Y%m%d")
        underlying = underlying_code.upper()
        features = features or {}

        snapshot: dict[str, Any] = {
            "underlying_code": underlying,
            "reference_date": reference_dt.strftime("%Y-%m-%d"),
            "lookback_days": int(lookback_days),
            "records": {},
            "errors": [],
        }

        contract_symbol = None
        try:
            if contract_id:
                contract_symbol = self._contract_symbol(contract_id, underlying_code=underlying, reference_date=reference_dt)
            else:
                main_code = self.get_main_contract_code(underlying, trading_date=reference_dt)
                contract_symbol = self._contract_symbol(main_code, underlying_code=underlying, reference_date=reference_dt) if main_code else None
        except Exception as exc:
            snapshot["errors"].append(f"contract_symbol: {exc}")

        def store(feature_key: str, method_name: str, **kwargs) -> None:
            diagnostic = self._query_extra_data_with_diagnostic(method_name, **kwargs)
            rows = diagnostic.pop("records", [])
            snapshot["records"][feature_key] = rows
            snapshot.setdefault("feature_status", {})[feature_key] = diagnostic.get("status", "unknown")
            snapshot.setdefault("feature_diagnostics", {})[feature_key] = diagnostic
            if diagnostic.get("error"):
                snapshot["errors"].append(f"{feature_key}: {diagnostic.get('error')}")

        if features.get("basis", False):
            store(
                "basis",
                "get_future_basis",
                underlying_symbol=underlying,
                start_date=start_key,
                end_date=end_key,
                fields=[],
            )

        if features.get("warehouse_receipt", False):
            store(
                "warehouse_receipt",
                "get_future_wr",
                underlying_symbol=underlying,
                start_date=start_key,
                end_date=end_key,
                fields=[],
            )

        if features.get("net_flow", False):
            for position_type in ("long", "short"):
                store(
                    f"net_flow_{position_type}",
                    "get_future_net_flow",
                    symbol=[underlying],
                    start_date=start_key,
                    end_date=end_key,
                    fields=[],
                    broker_name="",
                    position_type=position_type,
                )

        if features.get("variety_position_rank", False):
            for position_type in ("long", "short"):
                store(
                    f"variety_position_rank_{position_type}",
                    "get_future_variety_posi_rank",
                    symbol=[underlying],
                    start_date=start_key,
                    end_date=end_key,
                    position_type=position_type,
                    broker_name="",
                    rank_max=5,
                    fields=[],
                )

        if features.get("symbol_position_rank", False) and contract_symbol:
            for position_type in ("long", "short"):
                store(
                    f"symbol_position_rank_{position_type}",
                    "get_future_symbol_posi_rank",
                    symbol=[contract_symbol],
                    start_date=start_key,
                    end_date=end_key,
                    position_type=position_type,
                    broker_name="",
                    rank_max=5,
                    fields=[],
                )

        if features.get("ls_ratio", False) and contract_symbol:
            store(
                "ls_ratio",
                "get_future_ls_ratio",
                symbol=[contract_symbol],
                start_date=start_key,
                end_date=end_key,
            )

        if features.get("broker_net_margin_change", False):
            store(
                "broker_net_margin_change",
                "get_broker_net_margin_change",
                underlying_symbol=underlying,
                start_date=start_key,
                end_date=end_key,
                fields=[],
                broker="",
            )

        if features.get("broker_variety_profit", False):
            store(
                "broker_variety_profit",
                "get_broker_variety_profit",
                symbol=[underlying],
                start_date=start_key,
                end_date=end_key,
                fields=[],
                broker="",
            )

        # Slow optional methods are exposed but stay off by default in config.
        if features.get("broker_net_margin", False):
            store(
                "broker_net_margin",
                "get_broker_net_margin",
                underlying_symbol=underlying,
                start_date=end_key,
                end_date=end_key,
                fields=["underlying_symbol", "date", "broker", "net_margin"],
                broker="",
            )

        if features.get("netposi_rank", False):
            store(
                "netposi_rank",
                "get_future_netposi_rank",
                underlying_symbol=underlying,
                start_date=end_key,
                end_date=end_key,
                fields=[],
            )

        if features.get("net_cap_change", False) and contract_symbol:
            store(
                "net_cap_change",
                "get_future_net_cap_change",
                symbol=[contract_symbol],
                start_date=start_key,
                end_date=end_key,
                broker=[],
            )

        if features.get("contract_daily_indicators", False) and contract_symbol:
            store(
                "contract_daily_indicators",
                "get_future_contract_daily_indicators",
                symbol=[contract_symbol],
                start_date=start_key,
                end_date=end_key,
                fields=[],
            )

        if features.get("contract_rank", False) and contract_symbol:
            store(
                "contract_rank",
                "get_future_contract_rank",
                symbol="",
                underlying_symbol=[underlying],
                start_date=start_key,
                end_date=end_key,
                max_rank=10,
                type="",
                rank_type="ratio",
            )

        snapshot["contract_symbol"] = contract_symbol
        snapshot["record_counts"] = {
            key: len(value) for key, value in snapshot["records"].items()
        }
        status = snapshot.setdefault("feature_status", {})
        diagnostics = snapshot.setdefault("feature_diagnostics", {})
        for key, count in snapshot["record_counts"].items():
            if key not in status:
                status[key] = "ok" if count > 0 else "no_data"
            diagnostics.setdefault(
                key,
                {
                    "status": status[key],
                    "reason": None if count > 0 else "empty_response",
                    "row_count": count,
                },
            )
        return snapshot

    def _prepare_historical_records(
        self,
        records: list[dict[str, Any]],
        end_date: datetime,
        contract_id: Optional[str] = None,
        end_date_inclusive: bool = False,
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for row in records:
            trade_date = self._parse_trade_date(row.get("date"))
            outside_window = (
                trade_date > end_date
                if end_date_inclusive
                else trade_date >= end_date
            )
            if trade_date is None or outside_window:
                continue
            if contract_id:
                if not self._row_matches_contract(row, contract_id, reference_date=trade_date):
                    continue
            filtered.append(row)

        filtered.sort(key=lambda row: self._parse_trade_date(row.get("date")) or datetime.min)
        return filtered

    def _is_dominant_row(self, row: dict[str, Any], query_is_main: bool = False) -> bool:
        if query_is_main:
            return True
        symbol = str(row.get("symbol") or "").upper()
        return "_DOMINANT" in symbol

    def _build_daily_quote_from_row(self, row: Any) -> FuturesDailyQuote:
        row = self._coerce_record(row)
        trade_date = self._parse_trade_date(row.get("date"))
        open_price = row.get("day_session_open")
        if self._is_missing(open_price):
            open_price = row.get("open")

        return FuturesDailyQuote(
            contract_id=self._canonical_business_contract_code(row, trade_date),
            trade_date=trade_date.strftime("%Y-%m-%d") if trade_date else str(row.get("date", ""))[:10],
            open=self._coerce_float(open_price, 0),
            high=self._coerce_float(row.get("high"), 0),
            low=self._coerce_float(row.get("low"), 0),
            close=self._coerce_float(row.get("close"), 0),
            volume=self._coerce_int(row.get("volume"), 0),
            turnover=self._coerce_float(row.get("amount"), 0),
            open_interest=self._coerce_int(row.get("open_interest"), 0),
            settle_price=self._coerce_optional_float(row.get("settlement")),
            pre_settle_price=self._coerce_optional_float(row.get("pre_settlement")),
            pre_close_price=None,
            limit_up=self._coerce_optional_float(row.get("limit_up")),
            limit_down=self._coerce_optional_float(row.get("limit_down")),
        )

    def _build_optimized_quote_from_row(
        self,
        row: Any,
        query_is_main: bool = False,
    ) -> FuturesDailyQuoteOptimized:
        row = self._coerce_record(row)
        trade_date = self._parse_trade_date(row.get("date"))
        open_price = row.get("day_session_open")
        if self._is_missing(open_price):
            open_price = row.get("open")
        close_price = row.get("close")
        settle_price = row.get("settlement")
        pre_settle_price = row.get("pre_settlement")
        trading_code = row.get("trading_code") or row.get("dominant_id") or row.get("symbol")
        contract_object = str(row.get("underlying_symbol") or "").upper() or self._extract_underlying_code(str(trading_code))

        return FuturesDailyQuoteOptimized(
            ticker=self._canonical_business_contract_code(row, trade_date),
            trade_date=trade_date.strftime("%Y-%m-%d") if trade_date else str(row.get("date", ""))[:10],
            sec_short_name=None,
            exchange_cd=row.get("exchange"),
            pre_settle_price=self._coerce_float(pre_settle_price, 0),
            pre_close_price=None,
            open_price=self._coerce_float(open_price, 0),
            highest_price=self._coerce_float(row.get("high"), 0),
            lowest_price=self._coerce_float(row.get("low"), 0),
            close_price=self._coerce_float(close_price, 0),
            settle_price=self._coerce_optional_float(settle_price),
            limit_up=self._coerce_optional_float(row.get("limit_up")),
            limit_down=self._coerce_optional_float(row.get("limit_down")),
            turnover_vol=self._coerce_int(row.get("volume"), 0),
            turnover_value=self._coerce_float(row.get("amount"), 0),
            open_int=self._coerce_int(row.get("open_interest"), 0),
            chg=self._coerce_float(close_price, 0) - self._coerce_float(pre_settle_price, 0),
            chg1=(
                self._coerce_float(settle_price, 0) - self._coerce_float(pre_settle_price, 0)
                if not self._is_missing(settle_price) and not self._is_missing(pre_settle_price)
                else None
            ),
            chg_pct=self._calc_pct(close_price, pre_settle_price),
            main_con=1 if self._is_dominant_row(row, query_is_main=query_is_main) else 0,
            smain_con=None,
            contract_mark=None,
            contract_object=contract_object,
        )

    def get_futures_daily_candles(
        self,
        contract_id: str = None,
        underlying_code: str = None,
        is_main: int = 0,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        end_date_inclusive: bool = False,
    ) -> List[FuturesDailyQuote]:
        end_date = self._normalize_datetime(end_date, default=datetime.now())
        start_date = self._normalize_datetime(start_date, default=end_date - timedelta(days=365))
        symbol = self._symbol_for_query(
            contract_id=contract_id,
            underlying_code=underlying_code,
            is_main=is_main,
            reference_date=end_date,
        )
        rows = self._query_market_data(symbol=symbol, start_date=start_date, end_date=end_date)
        records = self._prepare_historical_records(
            rows,
            end_date=end_date,
            contract_id=contract_id,
            end_date_inclusive=end_date_inclusive,
        )
        return [self._build_daily_quote_from_row(row) for row in records]

    def get_futures_quote_on_date(
        self,
        trading_date: datetime,
        contract_id: str = None,
        underlying_code: str = None,
        is_main: int = 0,
    ) -> Optional[FuturesDailyQuoteOptimized]:
        trading_date = self._normalize_datetime(trading_date)
        symbol = self._symbol_for_query(
            contract_id=contract_id,
            underlying_code=underlying_code,
            is_main=is_main,
            reference_date=trading_date,
        )
        rows = self._query_exact_quote(symbol=symbol, trading_date=trading_date)
        if contract_id:
            rows = [
                row for row in rows
                if self._row_matches_contract(row, contract_id, reference_date=trading_date)
            ]
        if not rows:
            return None
        rows.sort(key=lambda row: self._parse_trade_date(row.get("date")) or datetime.min)
        return self._build_optimized_quote_from_row(rows[-1], query_is_main=bool(is_main))

    def get_main_contract_quote_on_date(
        self,
        underlying_code: str,
        trading_date: datetime,
    ) -> Optional[FuturesDailyQuoteOptimized]:
        return self.get_futures_quote_on_date(
            trading_date=trading_date,
            underlying_code=underlying_code,
            is_main=1,
        )

    def get_futures_minute_bars(
        self,
        contract_id: str = None,
        underlying_code: str = None,
        is_main: int = 0,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        frequency: str = "15m",
        time_zone: Optional[Any] = None,
        cutoff_datetime: Optional[datetime] = None,
    ) -> List[dict[str, Any]]:
        """Get raw PandaAI futures minute bars sorted by timestamp."""
        end_date = self._normalize_datetime(end_date, default=datetime.now())
        start_date = self._normalize_datetime(start_date, default=end_date)
        symbol = self._symbol_for_query(
            contract_id=contract_id,
            underlying_code=underlying_code,
            is_main=is_main,
            reference_date=end_date,
        )
        rows = self._query_market_min_data(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            time_zone=time_zone,
            refresh=isinstance(cutoff_datetime, datetime),
        )

        normalized_contract = self._normalize_internal_contract(contract_id) if contract_id else None
        cutoff = cutoff_datetime if isinstance(cutoff_datetime, datetime) else None
        filtered: list[dict[str, Any]] = []
        for row in rows:
            record = self._coerce_record(row).copy()
            logical_trading_date = self._parse_trade_date(record.get("trading_date"))
            if (
                logical_trading_date is None
                or logical_trading_date < start_date
                or logical_trading_date > end_date
            ):
                continue
            row_dt = self._parse_minute_datetime(record)
            if row_dt is None:
                continue
            if cutoff is not None and row_dt > cutoff:
                continue
            if normalized_contract:
                row_contract = self._normalize_internal_contract(
                    record.get("trading_code") or record.get("dominant_id") or record.get("symbol")
                )
                if row_contract and row_contract != normalized_contract:
                    continue
            record["datetime"] = row_dt.strftime("%Y-%m-%d %H:%M:%S")
            filtered.append(record)

        filtered.sort(key=lambda item: self._parse_minute_datetime(item) or datetime.min)
        if cutoff is not None and filtered:
            cache_key = (
                self._sdk_cache_namespace(),
                symbol.upper(),
                start_date.strftime("%Y%m%d"),
                end_date.strftime("%Y%m%d"),
                str(frequency or "15m").lower(),
                str(time_zone or ""),
            )
            self._minute_cache[cache_key] = [dict(item) for item in filtered]
        return filtered

    def get_futures_daily_candles_df(
        self,
        contract_id: str = None,
        underlying_code: str = None,
        is_main: int = 0,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        end_date_inclusive: bool = False,
    ):
        self._require_pandas("PandaAI DataFrame candle helpers")
        quotes = self.get_futures_daily_candles(
            contract_id=contract_id,
            underlying_code=underlying_code,
            is_main=is_main,
            start_date=start_date,
            end_date=end_date,
            end_date_inclusive=end_date_inclusive,
        )
        if not quotes:
            return pd.DataFrame()

        data = [
            {
                "date": quote.trade_date,
                "open": quote.open,
                "high": quote.high,
                "low": quote.low,
                "close": quote.close,
                "settle_price": quote.settle_price,
                "pre_settle_price": quote.pre_settle_price,
                "pre_close_price": quote.pre_close_price,
                "volume": quote.volume,
                "turnover": quote.turnover,
                "open_interest": quote.open_interest,
                "limit_up": quote.limit_up,
                "limit_down": quote.limit_down,
            }
            for quote in quotes
        ]
        df = pd.DataFrame(data)
        df["Date"] = pd.to_datetime(df["date"])
        df.set_index("Date", inplace=True)
        df.drop("date", axis=1, inplace=True)
        for col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df.sort_index(inplace=True)
        return df

    def get_main_contract(
        self,
        underlying_code: str,
        trade_date: Optional[datetime] = None,
    ) -> Optional[FuturesMainContract]:
        trade_date = self._normalize_datetime(trade_date, default=datetime.now())
        main_code = self.get_main_contract_code(underlying_code=underlying_code, trading_date=trade_date)
        if main_code is None:
            return None
        return FuturesMainContract(
            underlying_code=underlying_code,
            main_contract=main_code,
            trade_date=trade_date.strftime("%Y-%m-%d"),
        )

    def get_last_close_price(self, contract_id: str, trading_date: datetime) -> Optional[float]:
        quotes = self.get_futures_daily_candles(
            contract_id=contract_id,
            start_date=self._normalize_datetime(trading_date) - timedelta(days=7),
            end_date=trading_date,
        )
        return quotes[-1].close if quotes else None

    def get_continuous_candles(
        self,
        underlying_code: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        end_date_inclusive: bool = False,
    ) -> List[FuturesDailyQuote]:
        return self.get_futures_daily_candles(
            underlying_code=underlying_code,
            is_main=1,
            start_date=start_date,
            end_date=end_date,
            end_date_inclusive=end_date_inclusive,
        )

    def get_continuous_candles_df(
        self,
        underlying_code: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        end_date_inclusive: bool = False,
    ):
        return self.get_futures_daily_candles_df(
            underlying_code=underlying_code,
            is_main=1,
            start_date=start_date,
            end_date=end_date,
            end_date_inclusive=end_date_inclusive,
        )

    def get_china_futures_contracts(
        self,
        exchange: Optional[str] = None,
        underlying_code: Optional[str] = None,
    ) -> List[str]:
        if not underlying_code:
            raise ValueError("underlying_code is required")
        response = self._call_pandaai(
            "get_future_detail",
            symbol=None,
            fields=["symbol", "exchange", "underlying_symbol", "trading_code"],
            is_trading=1,
        )
        contracts = []
        for row in self._records_from_response(response):
            if str(row.get("underlying_symbol") or "").upper() != underlying_code.upper():
                continue
            if exchange and str(row.get("exchange") or "").upper() != str(exchange).upper():
                continue
            contract = self._canonical_business_contract_code(
                row,
                self._parse_trade_date(row.get("trade_date") or row.get("date")),
            )
            if contract:
                contracts.append(contract)
        return sorted(set(contracts))

    def get_futures_contract_detail(self, contract_id: str, reference_date: Optional[datetime] = None) -> Optional[dict[str, Any]]:
        """Return quasi-static contract detail from PandaAI with shared caching."""
        if not contract_id:
            return None
        reference_dt = self._normalize_datetime(reference_date, default=datetime.now())
        symbol = self._contract_symbol(contract_id, reference_date=reference_dt)
        cache_key = (self._sdk_cache_namespace(), "get_future_detail_exact", symbol.upper())
        if cache_key in self._extra_cache:
            records = self._extra_cache[cache_key]
        else:
            response = self._call_pandaai(
                "get_future_detail",
                symbol=[symbol],
                fields=[
                    "symbol",
                    "exchange",
                    "underlying_symbol",
                    "trading_code",
                    "margin_rate",
                    "de_listed_date",
                    "maturity_date",
                    "start_delivery_date",
                    "end_delivery_date",
                    "contract_multiplier",
                    "is_trading",
                ],
                is_trading=None,
            )
            records = self._records_from_response(response)
            self._extra_cache[cache_key] = records
        if not records:
            return None
        normalized = self._normalize_internal_contract(contract_id)
        for row in records:
            record = self._coerce_record(row)
            row_contract = self._normalize_internal_contract(record.get("trading_code") or record.get("symbol"))
            if row_contract == normalized:
                return dict(record)
        return dict(self._coerce_record(records[0]))

    def get_futures_daily_candles_optimized(
        self,
        contract_id: str = None,
        underlying_code: str = None,
        is_main: int = 0,
        contract_mark: str = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        end_date_inclusive: bool = False,
    ) -> List[FuturesDailyQuoteOptimized]:
        end_date = self._normalize_datetime(end_date, default=datetime.now())
        start_date = self._normalize_datetime(start_date, default=end_date - timedelta(days=365))
        query_is_main = bool(is_main or contract_mark)
        symbol = self._symbol_for_query(
            contract_id=contract_id,
            underlying_code=underlying_code,
            is_main=1 if query_is_main else 0,
            contract_mark=contract_mark,
            reference_date=end_date,
        )
        rows = self._query_market_data(symbol=symbol, start_date=start_date, end_date=end_date)
        records = self._prepare_historical_records(
            rows,
            end_date=end_date,
            contract_id=contract_id,
            end_date_inclusive=end_date_inclusive,
        )
        return [self._build_optimized_quote_from_row(row, query_is_main=query_is_main) for row in records]

    def get_main_contract_code(
        self,
        underlying_code: str,
        trading_date: Optional[datetime] = None,
    ) -> Optional[str]:
        trading_date = self._normalize_datetime(trading_date, default=datetime.now())
        quote = self.get_main_contract_quote_on_date(underlying_code=underlying_code, trading_date=trading_date)
        return quote.ticker if quote is not None and quote.ticker else None

    def get_continuous_contract_data(
        self,
        underlying_code: str,
        contract_mark: str = "L1",
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[FuturesDailyQuoteOptimized]:
        return self.get_futures_daily_candles_optimized(
            underlying_code=underlying_code,
            contract_mark=contract_mark,
            start_date=start_date,
            end_date=end_date,
        )

    def get_continuous_settle_price(
        self,
        underlying_code: str,
        trading_date: datetime,
        contract_mark: str = "L1",
    ) -> Optional[float]:
        quotes = self.get_continuous_contract_data(
            underlying_code=underlying_code,
            contract_mark=contract_mark,
            start_date=self._normalize_datetime(trading_date) - timedelta(days=3),
            end_date=trading_date,
        )
        if not quotes:
            return None

        target_date = self._normalize_datetime(trading_date).strftime("%Y-%m-%d")
        for quote in reversed(quotes):
            if quote.trade_date == target_date:
                return quote.settle_price or quote.close_price
        latest_quote = quotes[-1]
        return latest_quote.settle_price or latest_quote.close_price

    def get_futures_margin(self, contract_id: str):
        detail = self.get_futures_contract_detail(contract_id)
        if not detail:
            return None
        margin_rate = self._coerce_optional_float(detail.get("margin_rate"))
        if margin_rate is None or margin_rate <= 0:
            return None
        if margin_rate > 1:
            margin_rate = margin_rate / 100.0
        return FuturesMargin(
            contract_id=self._canonical_business_contract_code(
                detail,
                self._parse_trade_date(
                    detail.get("trade_date") or detail.get("date") or detail.get("update_date")
                ),
            ),
            long_margin_rate=margin_rate,
            short_margin_rate=margin_rate,
            update_date=datetime.now().strftime("%Y-%m-%d"),
        )
