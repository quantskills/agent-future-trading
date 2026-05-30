from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List

from util.logger import logger

_TRADE_DATES_CACHE: dict[tuple[str, str, str], List[datetime]] = {}
_PREVIOUS_TRADING_DAY_CACHE: dict[tuple[str, str, int, int], datetime] = {}
_NEXT_TRADING_DAY_CACHE: dict[tuple[str, str, int], datetime] = {}
_MARKET_PREVIOUS_TRADING_DAY_CACHE: dict[tuple[str, int, int], datetime] = {}
_MARKET_NEXT_TRADING_DAY_CACHE: dict[tuple[str, int], datetime] = {}


def map_datetime_to_futures_trading_day(
    router,
    timestamp,
    underlying_code: str,
    *,
    night_session_start_hour: int = 21,
    lookahead_days: int = 14,
) -> datetime:
    """Map a clock timestamp to the exchange trading day used by futures settlement."""
    event_dt = _normalize_datetime(timestamp)
    event_date = datetime.combine(event_dt.date(), datetime.min.time())
    if event_dt.hour >= int(night_session_start_hour):
        return get_next_trading_day(
            router=router,
            trading_date=event_date,
            underlying_code=underlying_code,
            lookahead_days=lookahead_days,
        )
    return event_date


def get_previous_trading_day(
    router,
    trading_date,
    underlying_code: str,
    lookback_days: int = 14,
    max_lookback_days: int = 90,
) -> datetime:
    target_date = _normalize_date(trading_date)
    window = max(lookback_days, 1)
    underlying_key = str(underlying_code or "").upper()
    target_key = target_date.strftime("%Y-%m-%d")
    cache_key = (underlying_key, target_key, window, max_lookback_days)
    market_cache_key = (target_key, window, max_lookback_days)

    if cache_key in _PREVIOUS_TRADING_DAY_CACHE:
        return _PREVIOUS_TRADING_DAY_CACHE[cache_key]
    if market_cache_key in _MARKET_PREVIOUS_TRADING_DAY_CACHE:
        previous_trading_day = _MARKET_PREVIOUS_TRADING_DAY_CACHE[market_cache_key]
        _PREVIOUS_TRADING_DAY_CACHE[cache_key] = previous_trading_day
        logger.info(
            f"Resolved previous trading day for {underlying_code} from market-calendar cache: "
            f"{previous_trading_day.strftime('%Y-%m-%d')} "
            f"(anchor={target_key}, lookback={window}d)"
        )
        return previous_trading_day

    while window <= max_lookback_days:
        trade_dates = _get_trade_dates(
            router=router,
            underlying_code=underlying_code,
            start_date=target_date - timedelta(days=window),
            end_date=target_date,
        )
        previous_dates = [trade_day for trade_day in trade_dates if trade_day < target_date]
        if previous_dates:
            previous_trading_day = previous_dates[-1]
            logger.info(
                f"Resolved previous trading day for {underlying_code}: "
                f"{previous_trading_day.strftime('%Y-%m-%d')} "
                f"(anchor={target_date.strftime('%Y-%m-%d')}, lookback={window}d)"
            )
            _PREVIOUS_TRADING_DAY_CACHE[cache_key] = previous_trading_day
            _MARKET_PREVIOUS_TRADING_DAY_CACHE[market_cache_key] = previous_trading_day
            return previous_trading_day
        window *= 2

    logger.warning(
        f"Unable to resolve previous trading day from market data provider quotes for {underlying_code} before "
        f"{target_date.strftime('%Y-%m-%d')} within {max_lookback_days} calendar days"
    )
    raise RuntimeError(
        f"Unable to resolve previous trading day for {underlying_code} before {target_date.strftime('%Y-%m-%d')}"
    )


def get_next_trading_day(router, trading_date, underlying_code: str, lookahead_days: int = 14) -> datetime:
    target_date = _normalize_date(trading_date)
    underlying_key = str(underlying_code or "").upper()
    target_key = target_date.strftime("%Y-%m-%d")
    cache_key = (underlying_key, target_key, lookahead_days)
    market_cache_key = (target_key, lookahead_days)
    if cache_key in _NEXT_TRADING_DAY_CACHE:
        return _NEXT_TRADING_DAY_CACHE[cache_key]
    if market_cache_key in _MARKET_NEXT_TRADING_DAY_CACHE:
        next_trading_day = _MARKET_NEXT_TRADING_DAY_CACHE[market_cache_key]
        _NEXT_TRADING_DAY_CACHE[cache_key] = next_trading_day
        logger.info(
            f"Resolved next trading day for {underlying_code} from market-calendar cache: "
            f"{next_trading_day.strftime('%Y-%m-%d')} (anchor={target_key})"
        )
        return next_trading_day

    if target_date.date() >= datetime.now().date():
        raise RuntimeError(
            "Quote-based next-trading-day resolution is only valid for historical backtests. "
            "Live/paper phase2 rollover scheduling requires an explicit trading-calendar source."
        )
    trade_dates = _get_trade_dates(
        router=router,
        underlying_code=underlying_code,
        start_date=target_date,
        end_date=target_date + timedelta(days=lookahead_days),
    )
    next_dates = [date for date in trade_dates if date > target_date]
    if next_dates:
        next_trading_day = next_dates[0]
        logger.info(
            f"Resolved next trading day for {underlying_code}: "
            f"{next_trading_day.strftime('%Y-%m-%d')} (anchor={target_date.strftime('%Y-%m-%d')})"
        )
        _NEXT_TRADING_DAY_CACHE[cache_key] = next_trading_day
        _MARKET_NEXT_TRADING_DAY_CACHE[market_cache_key] = next_trading_day
        return next_trading_day

    logger.warning(
        f"Unable to resolve next trading day from market data provider quotes for {underlying_code} after "
        f"{target_date.strftime('%Y-%m-%d')} within {lookahead_days} calendar days"
    )
    raise RuntimeError(
        f"Unable to resolve next trading day for {underlying_code} after {target_date.strftime('%Y-%m-%d')}"
    )


def _get_trade_dates(router, underlying_code: str, start_date: datetime, end_date: datetime) -> List[datetime]:
    cache_key = (
        str(underlying_code or "").upper(),
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
    )
    if cache_key in _TRADE_DATES_CACHE:
        return list(_TRADE_DATES_CACHE[cache_key])

    quotes = router.api.get_futures_daily_candles_optimized(
        underlying_code=underlying_code,
        is_main=1,
        start_date=start_date,
        end_date=end_date,
    )
    if not quotes:
        logger.warning(
            f"Market data provider returned no main-contract daily quotes for {underlying_code} between "
            f"{start_date.strftime('%Y-%m-%d')} and {end_date.strftime('%Y-%m-%d')}"
        )
        _TRADE_DATES_CACHE[cache_key] = []
        return []
    trade_dates = sorted(
        {
            _parse_trade_date(quote.trade_date)
            for quote in quotes
            if getattr(quote, "trade_date", None)
        }
    )
    _TRADE_DATES_CACHE[cache_key] = list(trade_dates)
    return trade_dates


def _normalize_date(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    return _parse_trade_date(value)


def _normalize_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())

    text = str(value).strip()
    if not text:
        raise ValueError("Empty datetime value")
    normalized = text.replace("T", " ").replace("Z", "")
    try:
        return datetime.fromisoformat(normalized).replace(microsecond=0)
    except ValueError:
        return datetime.strptime(normalized[:19], "%Y-%m-%d %H:%M:%S")


def _parse_trade_date(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())

    text = str(value).strip()
    if not text:
        raise ValueError("Empty trade date value")

    return datetime.strptime(text[:10], "%Y-%m-%d")
