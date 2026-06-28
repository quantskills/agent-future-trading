from __future__ import annotations

"""Shared data cache and data-usage summaries for analyst evidence."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from util.logger import logger
from util.trading_calendar import get_previous_trading_day


_CACHE_LOCK = threading.RLock()
_FEATHER_CACHE: Dict[Tuple[str, int, int], pd.DataFrame] = {}
_TEXT_CACHE: Dict[Tuple[str, int, int], Tuple[str, str]] = {}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _resolve_project_path(path_text: Optional[str], default_relative: str) -> Path:
    raw = Path(path_text or default_relative)
    if raw.is_absolute():
        return raw
    return _project_root() / raw


def _file_key(path: Path) -> Tuple[str, int, int]:
    stat = path.stat()
    return (str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))


def read_finoview_feather_cached(path: Path) -> pd.DataFrame:
    """Read a Finoview feather file with a process-local mtime-aware cache."""
    key = _file_key(path)
    with _CACHE_LOCK:
        cached = _FEATHER_CACHE.get(key)
        if cached is not None:
            return cached.copy()
    df = pd.read_feather(path)
    with _CACHE_LOCK:
        _FEATHER_CACHE[key] = df.copy()
    return df


def read_text_cached(path: Path, encodings: Iterable[str] = ("utf-8-sig", "utf-8", "gb18030")) -> Tuple[str, str]:
    """Read a local text file with encoding fallback and a process-local cache."""
    key = _file_key(path)
    with _CACHE_LOCK:
        cached = _TEXT_CACHE.get(key)
        if cached is not None:
            return cached
    last_error: Optional[Exception] = None
    for encoding in encodings:
        try:
            text = path.read_text(encoding=encoding)
            result = (text, encoding)
            with _CACHE_LOCK:
                _TEXT_CACHE[key] = result
            return result
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise UnicodeDecodeError("text_file", b"", 0, 1, "unable to decode with supported encodings")


def clear_data_runtime_cache() -> None:
    with _CACHE_LOCK:
        _FEATHER_CACHE.clear()
        _TEXT_CACHE.clear()


def prefetch_local_daily_data(config: Dict[str, Any], tickers: Iterable[str]) -> Dict[str, Any]:
    """Warm local Finoview/news caches for one trading day without changing data visibility."""
    runtime_cfg = (config.get("runtime") or {}).get("data_cache", {}) or {}
    if not bool(runtime_cfg.get("enabled", True)):
        return {"enabled": False}
    factor_cfg = config.get("factor_data") or {}
    tickers_set = {str(item).upper() for item in tickers or []}
    stats = {
        "enabled": True,
        "finoview_files_loaded": 0,
        "finoview_files_failed": 0,
        "news_files_loaded": 0,
        "news_files_failed": 0,
        "tickers": sorted(tickers_set),
    }
    if bool(runtime_cfg.get("prefetch_finoview_feather", True)):
        data_dir = _resolve_project_path(factor_cfg.get("data_dir"), "data/Fundamental_data/Finoview_data")
        if data_dir.exists():
            all_files = bool(runtime_cfg.get("prefetch_all_finoview_feathers", False))
            for path in sorted(data_dir.glob("*.feather")):
                stem = path.stem.upper()
                if not all_files and tickers_set and not any(stem == t or stem.startswith(f"{t}_") for t in tickers_set):
                    continue
                try:
                    read_finoview_feather_cached(path)
                    stats["finoview_files_loaded"] += 1
                except Exception as exc:
                    stats["finoview_files_failed"] += 1
                    logger.warning(f"Finoview cache prefetch failed for {path}: {exc}")
    news_cfg = factor_cfg.get("news") or {}
    if bool(runtime_cfg.get("prefetch_news_txt", True)) and bool(news_cfg.get("enabled", True)):
        news_dir = _resolve_project_path(news_cfg.get("data_dir"), "data/News_data/Future_news")
        for ticker in sorted(tickers_set):
            path = news_dir / f"{ticker}.txt"
            if not path.exists():
                continue
            try:
                read_text_cached(path)
                stats["news_files_loaded"] += 1
            except Exception as exc:
                stats["news_files_failed"] += 1
                logger.warning(f"News cache prefetch failed for {path}: {exc}")
    return stats


def prefetch_pandaai_daily_data(router: Any, config: Dict[str, Any], tickers: Iterable[str], trading_date: Any) -> Dict[str, Any]:
    """Warm PandaAI shared caches for daily market and derivative snapshots."""
    runtime_cfg = (config.get("runtime") or {}).get("data_cache", {}) or {}
    if not bool(runtime_cfg.get("enabled", True)):
        return {"enabled": False}
    stats = {
        "enabled": True,
        "market_requests": 0,
        "market_failed": 0,
        "extra_requests": 0,
        "extra_failed": 0,
    }
    ticker_list = [str(item).upper() for item in tickers or []]
    if bool(runtime_cfg.get("prefetch_pandaai_market", False)) and hasattr(router, "get_daily_candles_df"):
        for ticker in ticker_list:
            try:
                router.get_daily_candles_df(ticker=ticker, trading_date=trading_date)
                stats["market_requests"] += 1
            except Exception as exc:
                stats["market_failed"] += 1
                logger.warning(f"{ticker}: PandaAI market cache prefetch failed: {exc}")
    elif bool(runtime_cfg.get("prefetch_pandaai_market", False)):
        stats["market_skipped"] = "router_missing_get_daily_candles_df"
    extra_cfg = config.get("pandaai_extra_data", {}) or {}
    if (
        bool(runtime_cfg.get("prefetch_pandaai_extra", False))
        and bool(extra_cfg.get("enabled", False))
        and hasattr(router, "get_pandaai_futures_extra_snapshot")
    ):
        try:
            reference_date = trading_date
            for _ in range(max(1, int(extra_cfg.get("reference_lag_days", 1)))):
                reference_date = get_previous_trading_day(
                    router=router,
                    trading_date=reference_date,
                    underlying_code=ticker_list[0] if ticker_list else None,
                )
        except Exception as exc:
            logger.warning(f"PandaAI extra cache prefetch skipped: reference date unavailable: {exc}")
            return stats
        features = extra_cfg.get("features", {}) or {}
        lookback_days = int(extra_cfg.get("lookback_days", 5))
        for ticker in ticker_list:
            try:
                router.get_pandaai_futures_extra_snapshot(
                    underlying_code=ticker,
                    reference_date=reference_date,
                    lookback_days=lookback_days,
                    features=features,
                )
                stats["extra_requests"] += 1
            except Exception as exc:
                stats["extra_failed"] += 1
                logger.warning(f"{ticker}: PandaAI extra cache prefetch failed: {exc}")
    elif bool(runtime_cfg.get("prefetch_pandaai_extra", False)) and bool(extra_cfg.get("enabled", False)):
        stats["extra_skipped"] = "router_missing_get_pandaai_futures_extra_snapshot"
    return stats


def _as_date_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return pd.to_datetime(value).strftime("%Y-%m-%d")
    except Exception:
        text = str(value)
        return text[:10] if text else None


def _columns_used(df: Any, preferred: Optional[List[str]] = None, limit: int = 20) -> List[str]:
    if df is None or not hasattr(df, "columns"):
        return []
    columns = [str(col) for col in list(df.columns)]
    if preferred:
        preferred_set = {str(item) for item in preferred}
        ordered = [col for col in columns if col in preferred_set]
        ordered.extend([col for col in columns if col not in preferred_set])
        columns = ordered
    return columns[:limit]


def build_technical_data_usage(
    *,
    ticker: str,
    trading_date: Any,
    prices_df: Any,
    indicators_used: Iterable[str],
    pre_open_only: bool = True,
    info_cutoff: str = "pre_open",
) -> Dict[str, Any]:
    latest_data_date = None
    row_count = 0
    if prices_df is not None and hasattr(prices_df, "empty") and not prices_df.empty:
        row_count = int(len(prices_df))
        for candidate in ("date", "trade_date", "tradeDate", "datetime"):
            if candidate in prices_df.columns:
                latest_data_date = _as_date_text(prices_df[candidate].iloc[-1])
                break
        if latest_data_date is None and getattr(prices_df, "index", None) is not None:
            latest_data_date = _as_date_text(prices_df.index[-1])
    return {
        "ticker": str(ticker).upper(),
        "trading_date": _as_date_text(trading_date),
        "analyst": "technical",
        "sources": {
            "pandaai_market": {
                "source": "PandaAI",
                "dataset": "daily_continuous_candles",
                "available": row_count > 0,
                "used_in_signal": True,
                "pre_open_only": bool(pre_open_only),
                "info_cutoff": info_cutoff,
                "latest_data_date": latest_data_date,
                "row_count": row_count,
                "fields_used": _columns_used(prices_df, ["open", "high", "low", "close", "volume", "open_interest"]),
                "indicators_used": [str(item) for item in indicators_used or []],
            }
        },
    }


def build_fundamental_data_usage(
    *,
    ticker: str,
    trading_date: Any,
    fundamentals_metadata: Optional[Dict[str, Any]],
    pandaai_extra_context: Optional[Dict[str, Any]],
    pre_open_only: bool = True,
    info_cutoff: str = "pre_open",
) -> Dict[str, Any]:
    metadata = fundamentals_metadata or {}
    extra = pandaai_extra_context or {}
    availability_audit = metadata.get("local_finoview_availability_audit") or {}
    finoview_available = int(metadata.get("loaded_indicator_count") or 0) > 0
    extra_features = extra.get("features") if isinstance(extra.get("features"), list) else []
    return {
        "ticker": str(ticker).upper(),
        "trading_date": _as_date_text(trading_date),
        "analyst": "fundamental",
        "sources": {
            "finoview_fundamental": {
                "source": "Finoview",
                "dataset": "local_feather_fundamental",
                "available": finoview_available,
                "used_in_signal": True,
                "pre_open_only": bool(pre_open_only),
                "info_cutoff": info_cutoff,
                "configured_indicator_count": int(metadata.get("configured_indicator_count") or 0),
                "loaded_indicator_count": int(metadata.get("loaded_indicator_count") or 0),
                "missing_like_count": int(metadata.get("missing_like_count") or 0),
                "stale_indicator_count": int(metadata.get("stale_indicator_count") or 0),
                "near_stale_indicator_count": int(metadata.get("near_stale_indicator_count") or 0),
                "coverage_ratio": float(metadata.get("coverage_ratio") or 0.0),
                "stale_ratio": float(metadata.get("stale_ratio") or 0.0),
                "factor_groups": metadata.get("indicator_role_counts") or {},
                "freshness_score": metadata.get("factor_freshness_score"),
                "no_lookahead_status": metadata.get("no_lookahead_status", "unchecked"),
                "local_availability_audit": availability_audit,
                "coverage_status": (
                    availability_audit.get("coverage_status")
                    if isinstance(availability_audit, dict)
                    else None
                ),
                "supports_trade_setup": bool(
                    availability_audit.get("supports_fundamental_trade_setup", False)
                )
                if isinstance(availability_audit, dict)
                else False,
                "runtime_data_boundary": (
                    availability_audit.get("runtime_data_boundary")
                    if isinstance(availability_audit, dict)
                    else "local_feather_only"
                ),
            },
            "pandaai_extra": {
                "source": "PandaAI",
                "dataset": "futures_derivative_snapshot",
                "available": bool(extra.get("enabled")) and bool(extra_features),
                "used_in_signal": bool(extra.get("enabled")),
                "pre_open_only": bool(pre_open_only),
                "info_cutoff": extra.get("info_cutoff") or "T-1_or_earlier",
                "reference_date": extra.get("reference_date"),
                "lookback_days": extra.get("lookback_days"),
                "feature_count": len(extra_features),
                "record_counts": extra.get("record_counts") or {},
                "feature_status": extra.get("feature_status") or {},
                "data_missing": extra.get("data_missing") or [],
                "errors": extra.get("errors") or [],
                "direction_hint": extra.get("direction_hint"),
                "tradeability": extra.get("tradeability"),
            },
        },
    }


def build_news_data_usage(
    *,
    ticker: str,
    trading_date: Any,
    news_metadata: Optional[Dict[str, Any]],
    news_context: Optional[Dict[str, Any]],
    pre_open_only: bool = True,
    info_cutoff: str = "pre_open",
) -> Dict[str, Any]:
    metadata = news_metadata or {}
    context = news_context or {}
    return {
        "ticker": str(ticker).upper(),
        "trading_date": _as_date_text(trading_date),
        "analyst": "commodity_news",
        "sources": {
            "finoview_news_txt": {
                "source": "Finoview",
                "dataset": "local_news_txt",
                "available": bool(metadata.get("file_exists", False)),
                "used_in_signal": int(metadata.get("selected_news_count") or 0) > 0,
                "pre_open_only": bool(pre_open_only),
                "info_cutoff": info_cutoff,
                "news_cutoff": metadata.get("news_cutoff"),
                "file_path": metadata.get("file_path"),
                "encoding": metadata.get("encoding"),
                "raw_block_count": int(metadata.get("raw_block_count") or 0),
                "parsed_news_count": int(metadata.get("parsed_news_count") or 0),
                "selected_news_count": int(metadata.get("selected_news_count") or 0),
                "latest_news_date": metadata.get("latest_news_date"),
                "freshness_score": context.get("freshness_score"),
                "relevance_score": context.get("relevance_score"),
                "event_type_counts": context.get("event_type_counts") or {},
                "direction_counts": context.get("direction_counts") or {},
            }
        },
    }


def extract_signal_data_usage(signal_payload: Dict[str, Any]) -> Dict[str, Any]:
    metadata = signal_payload.get("metadata") if isinstance(signal_payload, dict) else {}
    if isinstance(metadata, dict):
        data_usage = metadata.get("data_usage_summary")
        return data_usage if isinstance(data_usage, dict) else {}
    return {}


def build_pm_data_quality_summary(
    analyst_signals: Iterable[Any],
    market_confirmation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    analysts: Dict[str, Any] = {}
    for idx, signal in enumerate(analyst_signals or []):
        payload = signal.model_dump() if hasattr(signal, "model_dump") else dict(signal)
        analyst = str(payload.get("agent_name") or f"signal_{idx}")
        analysts[analyst] = extract_signal_data_usage(payload)
    confirmation = market_confirmation or {}
    pandaai_extra_available = bool(confirmation.get("enabled")) and bool(confirmation.get("features"))
    pm_sources = {
        "pandaai_market_confirmation": {
            "source": "PandaAI",
            "dataset": "futures_derivative_market_confirmation",
            "available": pandaai_extra_available,
            "used_in_signal": bool(confirmation.get("enabled")),
            "reference_date": confirmation.get("reference_date"),
            "feature_count": len(confirmation.get("features") or []),
            "feature_status": confirmation.get("feature_status") or {},
            "data_missing": confirmation.get("data_missing") or [],
            "data_unavailable": confirmation.get("data_unavailable") or [],
            "fallback_covered_missing": confirmation.get("fallback_covered_missing") or [],
            "errors": confirmation.get("errors") or [],
            "confirmation_score": confirmation.get("confirmation_score"),
            "direction": confirmation.get("direction"),
        }
    }
    unavailable_sources: List[str] = []
    stale_sources: List[str] = []
    used_sources: List[str] = []
    fundamental_trade_setup_gaps: List[str] = []
    for analyst, usage in analysts.items():
        sources = usage.get("sources") if isinstance(usage, dict) else {}
        for name, source in (sources or {}).items():
            if source.get("used_in_signal"):
                used_sources.append(f"{analyst}.{name}")
            if not source.get("available"):
                unavailable_sources.append(f"{analyst}.{name}")
            if source.get("stale_indicator_count") or source.get("stale_ratio"):
                if float(source.get("stale_ratio") or 0.0) > 0:
                    stale_sources.append(f"{analyst}.{name}")
            if name == "finoview_fundamental" and source.get("used_in_signal"):
                if not bool(source.get("supports_trade_setup", True)):
                    fundamental_trade_setup_gaps.append(f"{analyst}.{name}")
    for name, source in pm_sources.items():
        if source.get("used_in_signal"):
            used_sources.append(f"pm.{name}")
        if source.get("used_in_signal") and not source.get("available"):
            unavailable_sources.append(f"pm.{name}")
    return {
        "analysts": analysts,
        "pm_sources": pm_sources,
        "used_sources": sorted(set(used_sources)),
        "unavailable_sources": sorted(set(unavailable_sources)),
        "stale_sources": sorted(set(stale_sources)),
        "fundamental_trade_setup_gaps": sorted(set(fundamental_trade_setup_gaps)),
        "fundamental_trade_setup_gap": bool(fundamental_trade_setup_gaps),
    }


def write_daily_data_quality_summary(
    *,
    config: Dict[str, Any],
    config_id: str,
    trading_date: Any,
    ticker: str,
    data_quality_summary: Dict[str, Any],
) -> Optional[str]:
    runtime_cfg = ((config.get("runtime") or {}).get("data_quality_summary") or {})
    if not bool(runtime_cfg.get("enabled", True)):
        return None
    root = _project_root()
    out_dir = _resolve_project_path(runtime_cfg.get("output_dir"), "src/logs/data_quality")
    out_dir.mkdir(parents=True, exist_ok=True)
    date_text = _as_date_text(trading_date) or str(trading_date)[:10]
    path = out_dir / f"{date_text}.json"
    payload: Dict[str, Any] = {
        "trading_date": date_text,
        "config_id": config_id,
        "exp_name": config.get("exp_name"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tickers": {},
    }
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload.setdefault("tickers", {})[str(ticker).upper()] = data_quality_summary
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def data_usage_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    usage = snapshot.get("data_quality_summary")
    if isinstance(usage, dict):
        return usage
    analysts: Dict[str, Any] = {}
    for analyst in ("technical", "fundamental", "commodity_news"):
        payload = snapshot.get(analyst)
        if isinstance(payload, dict):
            analysts[analyst] = extract_signal_data_usage(payload)
    return {"analysts": analysts, "pm_sources": {}}


def compact_data_usage_notes(data_usage: Dict[str, Any], max_items: int = 6) -> List[str]:
    notes: List[str] = []
    analysts = data_usage.get("analysts") if isinstance(data_usage, dict) else {}
    for analyst, usage in (analysts or {}).items():
        sources = usage.get("sources") if isinstance(usage, dict) else {}
        for source_name, source in (sources or {}).items():
            notes.append(
                f"{analyst}.{source_name}: available={bool(source.get('available'))}, "
                f"used={bool(source.get('used_in_signal'))}, cutoff={source.get('info_cutoff') or 'unknown'}"
            )
    pm_sources = data_usage.get("pm_sources") if isinstance(data_usage, dict) else {}
    for source_name, source in (pm_sources or {}).items():
        notes.append(
            f"pm.{source_name}: available={bool(source.get('available'))}, "
            f"used={bool(source.get('used_in_signal'))}, score={source.get('confirmation_score')}"
        )
    return notes[:max_items]
