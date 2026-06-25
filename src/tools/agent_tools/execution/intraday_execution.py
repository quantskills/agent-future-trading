from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from graph.schema import BasePriceSource, MorningExecutionBasis


_BUY_LIKE_ACTIONS = {"open_long", "close_short"}
_SELL_LIKE_ACTIONS = {"open_short", "close_long"}
_IMMEDIATE_ACTIONS = {"close_long", "close_short"}


@dataclass
class IntradayExecutionSelection:
    """Decision produced by the intraday execution gate."""

    decision: str
    reason: str
    base_price: Optional[float] = None
    base_datetime: Optional[str] = None
    base_price_source: Optional[BasePriceSource] = None
    signal_datetime: Optional[str] = None
    features: Dict[str, Any] = field(default_factory=dict)

    @property
    def should_execute(self) -> bool:
        return self.decision == "execute" and self.base_price is not None

    def to_audit_payload(self) -> Dict[str, Any]:
        features = self.features if isinstance(self.features, dict) else {}
        chase_check = features.get("chase_check") if isinstance(features.get("chase_check"), dict) else {}
        trigger_checked = self.reason not in {"hold_or_zero_lots"}
        trigger_passed = self.decision == "execute" and self.reason in {
            "intraday_trigger_confirmed",
            "intraday_immediate_execution",
            "intraday_event_immediate_execution",
            "intraday_pullback_confirmed",
            "intraday_vwap_confirmed",
        }
        execution_failure_reason = "" if self.decision == "execute" else self.reason
        missed_opportunity_flag = bool(self.decision in {"skip", "wait"} and self.reason not in {"hold_or_zero_lots"})
        return {
            "decision": self.decision,
            "reason": self.reason,
            "base_price": self.base_price,
            "base_datetime": self.base_datetime,
            "base_price_source": self.base_price_source.value if self.base_price_source else None,
            "signal_datetime": self.signal_datetime,
            "features": features,
            "trigger_checked": bool(trigger_checked),
            "trigger_passed": bool(trigger_passed),
            "price_chase_check": chase_check or {"checked": False, "passed": None},
            "execution_failure_reason": execution_failure_reason,
            "missed_opportunity_flag": missed_opportunity_flag,
            "learning_writeback_contract": "trigger_failure_price_chase_and_missed_opportunity_feed_researcher",
        }


def resolve_intraday_execution_basis(
    *,
    router,
    config: Dict[str, Any],
    underlying_code: str,
    trading_date,
    action: Any,
    contract_code: Optional[str] = None,
    decision_context: Optional[Dict[str, Any]] = None,
    cutoff_datetime: Optional[datetime] = None,
    finalize_untriggered: bool = True,
    force_immediate: bool = False,
) -> Tuple[MorningExecutionBasis, IntradayExecutionSelection]:
    """Resolve a Phase2 execution basis from intraday bars."""

    intraday_config = _intraday_config(config)
    normalized_date = _normalize_date(trading_date)
    action_value = _enum_value(action)
    frequency = str(intraday_config.get("decision_frequency", "15m"))
    execution_frequency = str(intraday_config.get("execution_frequency", "1m"))
    time_zone = intraday_config.get("time_zone")

    try:
        signal_bars = router.get_china_futures_minute_bars(
            contract_id=contract_code,
            underlying_code=underlying_code,
            is_main=0 if contract_code else 1,
            start_date=normalized_date,
            end_date=normalized_date,
            frequency=frequency,
            time_zone=time_zone,
            cutoff_datetime=cutoff_datetime,
        )
        execution_bars = router.get_china_futures_minute_bars(
            contract_id=contract_code,
            underlying_code=underlying_code,
            is_main=0 if contract_code else 1,
            start_date=normalized_date,
            end_date=normalized_date,
            frequency=execution_frequency,
            time_zone=time_zone,
            cutoff_datetime=cutoff_datetime,
        )
        selection = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action=action_value,
            config=intraday_config,
            decision_context=decision_context,
            cutoff_datetime=cutoff_datetime,
            finalize_untriggered=finalize_untriggered,
            force_immediate=force_immediate,
        )
    except Exception as exc:
        selection = IntradayExecutionSelection(
            decision="skip" if finalize_untriggered else "wait",
            reason="intraday_no_valid_bar",
            features={"error": str(exc), "underlying_code": underlying_code, "contract_code": contract_code},
        )

    basis = MorningExecutionBasis(
        base_price=selection.base_price,
        base_price_source=selection.base_price_source,
        base_price_date=selection.base_datetime,
        open_price=selection.base_price,
        prev_close_price=None,
        warning_message=None if selection.should_execute else selection.reason,
        intraday_audit=selection.to_audit_payload(),
    )
    return basis, selection


def select_intraday_execution(
    *,
    signal_bars: Iterable[Dict[str, Any]],
    execution_bars: Iterable[Dict[str, Any]],
    action: Any,
    config: Dict[str, Any],
    decision_context: Optional[Dict[str, Any]] = None,
    cutoff_datetime: Optional[datetime] = None,
    finalize_untriggered: bool = True,
    force_immediate: bool = False,
) -> IntradayExecutionSelection:
    """Select the first valid execution bar using completed signal bars only."""

    action_value = _enum_value(action)
    decision_context = decision_context if isinstance(decision_context, dict) else {}
    config = config or {}
    normalized_signal_bars = _normalize_bars(signal_bars, cutoff_datetime=cutoff_datetime)
    normalized_execution_bars = _normalize_bars(execution_bars, cutoff_datetime=cutoff_datetime)
    min_volume = float(config.get("min_execution_volume", 0) or 0)
    execution_contract = (
        decision_context.get("execution_contract")
        if isinstance(decision_context.get("execution_contract"), dict)
        else {}
    )
    execution_profile = _execution_profile_from_context(decision_context)
    can_execute_without_intraday_trigger = bool(execution_contract.get("can_execute_without_intraday_trigger"))

    if not normalized_execution_bars:
        return IntradayExecutionSelection(
            decision="skip" if finalize_untriggered else "wait",
            reason="intraday_no_valid_bar",
            features={"execution_bars": 0, "execution_profile": execution_profile},
        )

    immediate_event = execution_profile == "event_immediate" and can_execute_without_intraday_trigger
    if force_immediate or action_value in _IMMEDIATE_ACTIONS or immediate_event:
        execution_bar = _first_valid_execution_bar(normalized_execution_bars, min_volume=min_volume)
        if execution_bar is None:
            return IntradayExecutionSelection(
                decision="skip" if finalize_untriggered else "wait",
                reason="intraday_no_valid_bar",
                features={
                    "execution_bars": len(normalized_execution_bars),
                    "min_execution_volume": min_volume,
                    "execution_profile": execution_profile,
                },
            )
        reason = "intraday_event_immediate_execution" if immediate_event else "intraday_immediate_execution"
        return _execution_selection(
            reason=reason,
            execution_bar=execution_bar,
            signal_bar=None,
            features={
                "execution_mode": "immediate",
                "execution_profile": execution_profile,
                "execution_contract": execution_contract,
                "execution_bars": len(normalized_execution_bars),
            },
            source=BasePriceSource.INTRADAY_FIRST_VALID_1M_OPEN,
        )

    if action_value not in _BUY_LIKE_ACTIONS | _SELL_LIKE_ACTIONS:
        return IntradayExecutionSelection(
            decision="wait" if not finalize_untriggered else "skip",
            reason="hold_or_zero_lots",
            features={"action": action_value, "execution_profile": execution_profile},
        )

    if not normalized_signal_bars:
        return IntradayExecutionSelection(
            decision="skip" if finalize_untriggered else "wait",
            reason="intraday_no_valid_bar",
            features={
                "signal_bars": 0,
                "execution_bars": len(normalized_execution_bars),
                "execution_profile": execution_profile,
            },
        )

    opening_range, opening_range_complete_at = _opening_range(normalized_execution_bars, config)
    require_complete_opening_range = bool(config.get("require_complete_opening_range", True))
    if require_complete_opening_range and opening_range_complete_at is not None:
        latest_execution_dt = normalized_execution_bars[-1]["dt"]
        if latest_execution_dt < opening_range_complete_at:
            return IntradayExecutionSelection(
                decision="skip" if finalize_untriggered else "wait",
                reason="intraday_opening_range_incomplete",
                features={
                    "action": action_value,
                    "signal_bars": len(normalized_signal_bars),
                    "execution_bars": len(normalized_execution_bars),
                    "opening_range": opening_range,
                    "latest_execution_bar": latest_execution_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "finalize_untriggered": finalize_untriggered,
                    "execution_profile": execution_profile,
                },
            )
        signal_bars_for_trigger = [
            bar for bar in normalized_signal_bars
            if bar["dt"] >= opening_range_complete_at
        ]
    else:
        signal_bars_for_trigger = normalized_signal_bars

    for signal_bar in signal_bars_for_trigger:
        historical_exec_bars = [bar for bar in normalized_execution_bars if bar["dt"] <= signal_bar["dt"]]
        if not historical_exec_bars:
            continue
        vwap_value = _vwap(historical_exec_bars)
        signal_close = _float(signal_bar.get("close"))
        if signal_close is None or vwap_value is None:
            continue

        long_breakout = action_value in _BUY_LIKE_ACTIONS and signal_close >= vwap_value and signal_close >= opening_range["high"]
        short_breakout = action_value in _SELL_LIKE_ACTIONS and signal_close <= vwap_value and signal_close <= opening_range["low"]
        long_pullback = (
            execution_profile == "pullback"
            and action_value in _BUY_LIKE_ACTIONS
            and signal_close >= vwap_value
            and signal_close > opening_range["low"]
        )
        short_pullback = (
            execution_profile == "pullback"
            and action_value in _SELL_LIKE_ACTIONS
            and signal_close <= vwap_value
            and signal_close < opening_range["high"]
        )
        long_vwap_confirmed = execution_profile == "vwap_confirmed" and action_value in _BUY_LIKE_ACTIONS and signal_close >= vwap_value
        short_vwap_confirmed = execution_profile == "vwap_confirmed" and action_value in _SELL_LIKE_ACTIONS and signal_close <= vwap_value
        if execution_profile == "pullback":
            long_trigger = long_pullback
            short_trigger = short_pullback
        elif execution_profile == "vwap_confirmed":
            long_trigger = long_vwap_confirmed
            short_trigger = short_vwap_confirmed
        else:
            long_trigger = long_breakout
            short_trigger = short_breakout
        if not (long_trigger or short_trigger):
            continue

        execution_bar = _next_execution_bar(
            normalized_execution_bars,
            after_dt=signal_bar["dt"],
            min_volume=min_volume,
        )
        if execution_bar is None:
            continue

        chase_check = _passes_chase_filter(
            action_value=action_value,
            signal_close=signal_close,
            execution_open=_float(execution_bar.get("open")),
            config=config,
        )
        if not chase_check["passed"]:
            continue

        if execution_profile == "pullback":
            trigger_reason = "intraday_pullback_confirmed"
            execution_mode = "pullback_confirmed"
            trigger_rule = "vwap_pullback_support"
        elif execution_profile == "vwap_confirmed":
            trigger_reason = "intraday_vwap_confirmed"
            execution_mode = "vwap_confirmed"
            trigger_rule = "vwap_direction_confirmation"
        else:
            trigger_reason = "intraday_trigger_confirmed"
            execution_mode = "confirmed"
            trigger_rule = "opening_range_breakout"
        return _execution_selection(
            reason=trigger_reason,
            execution_bar=execution_bar,
            signal_bar=signal_bar,
            features={
                "execution_mode": execution_mode,
                "execution_profile": execution_profile,
                "execution_contract": execution_contract,
                "action": action_value,
                "signal_close": signal_close,
                "vwap": vwap_value,
                "opening_range": opening_range,
                "trigger_rule": trigger_rule,
                "signal_bars": len(normalized_signal_bars),
                "eligible_signal_bars": len(signal_bars_for_trigger),
                "execution_bars": len(normalized_execution_bars),
                "chase_check": chase_check,
            },
            source=BasePriceSource.INTRADAY_NEXT_1M_OPEN,
        )

    return IntradayExecutionSelection(
        decision="skip" if finalize_untriggered else "wait",
        reason="intraday_trigger_not_met" if finalize_untriggered else "intraday_waiting_for_trigger",
        features={
            "action": action_value,
            "signal_bars": len(normalized_signal_bars),
            "eligible_signal_bars": len(signal_bars_for_trigger),
            "execution_bars": len(normalized_execution_bars),
            "opening_range": opening_range,
            "finalize_untriggered": finalize_untriggered,
            "execution_profile": execution_profile,
            "execution_contract": execution_contract,
        },
    )


def _execution_profile_from_context(decision_context: Dict[str, Any]) -> str:
    execution_contract = (
        decision_context.get("execution_contract")
        if isinstance(decision_context.get("execution_contract"), dict)
        else {}
    )
    profile = str(execution_contract.get("execution_profile") or decision_context.get("execution_profile") or "breakout").lower()
    allowed = {"breakout", "pullback", "vwap_confirmed", "event_immediate", "exit_immediate", "hold"}
    return profile if profile in allowed else "breakout"


def _relative_miss(signal_close: float, barrier: Any, *, direction: str) -> float:
    barrier_value = _float(barrier)
    if barrier_value is None or barrier_value <= 0:
        return float("inf")
    if direction == "long":
        return max(0.0, (barrier_value - signal_close) / barrier_value)
    return max(0.0, (signal_close - barrier_value) / barrier_value)


def intraday_confirmation_enabled(config: Dict[str, Any]) -> bool:
    return bool(_intraday_config(config).get("enabled", False))


def _intraday_config(config: Dict[str, Any]) -> Dict[str, Any]:
    execution_config = (config or {}).get("execution", {}) or {}
    intraday_config = execution_config.get("intraday_confirmation") or execution_config.get("intraday") or {}
    return intraday_config if isinstance(intraday_config, dict) else {}


def _execution_selection(
    *,
    reason: str,
    execution_bar: Dict[str, Any],
    signal_bar: Optional[Dict[str, Any]],
    features: Dict[str, Any],
    source: BasePriceSource,
) -> IntradayExecutionSelection:
    execution_open = _float(execution_bar.get("open"))
    return IntradayExecutionSelection(
        decision="execute",
        reason=reason,
        base_price=execution_open,
        base_datetime=execution_bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
        base_price_source=source,
        signal_datetime=signal_bar["dt"].strftime("%Y-%m-%d %H:%M:%S") if signal_bar else None,
        features=features,
    )


def _normalize_bars(
    rows: Iterable[Dict[str, Any]],
    *,
    cutoff_datetime: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    bars: List[Dict[str, Any]] = []
    for row in rows or []:
        record = dict(row)
        bar_dt = _parse_bar_datetime(record)
        if bar_dt is None:
            continue
        if cutoff_datetime is not None and bar_dt > cutoff_datetime:
            continue
        record["dt"] = bar_dt
        bars.append(record)
    bars.sort(key=lambda item: item["dt"])
    return bars


def _parse_bar_datetime(row: Dict[str, Any]) -> Optional[datetime]:
    value = row.get("datetime")
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    if value is not None and str(value).strip():
        text = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y%m%d %H%M%S"):
            try:
                return datetime.strptime(text[:19], fmt)
            except ValueError:
                continue

    date_text = str(row.get("date") or row.get("trading_date") or "").strip()
    if not date_text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            date_part = datetime.strptime(date_text[:10], fmt)
            break
        except ValueError:
            date_part = None
    if date_part is None:
        return None

    minute_digits = "".join(ch for ch in str(row.get("minute") or "") if ch.isdigit()).zfill(6)[-6:]
    try:
        return date_part.replace(
            hour=int(minute_digits[0:2]),
            minute=int(minute_digits[2:4]),
            second=int(minute_digits[4:6]),
            microsecond=0,
        )
    except Exception:
        return date_part


def _first_valid_execution_bar(
    bars: List[Dict[str, Any]],
    *,
    min_volume: float,
) -> Optional[Dict[str, Any]]:
    for bar in bars:
        if _float(bar.get("open")) is None:
            continue
        if _float(bar.get("volume"), 0.0) < min_volume:
            continue
        return bar
    return None


def _next_execution_bar(
    bars: List[Dict[str, Any]],
    *,
    after_dt: datetime,
    min_volume: float,
) -> Optional[Dict[str, Any]]:
    for bar in bars:
        if bar["dt"] <= after_dt:
            continue
        if _float(bar.get("open")) is None:
            continue
        if _float(bar.get("volume"), 0.0) < min_volume:
            continue
        return bar
    return None


def _opening_range(bars: List[Dict[str, Any]], config: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[datetime]]:
    minutes = int(config.get("opening_range_minutes", 30) or 30)
    if not bars:
        return {"high": 0.0, "low": 0.0, "minutes": minutes, "complete": False}, None
    start_dt = bars[0]["dt"]
    complete_at = start_dt + timedelta(minutes=max(0, minutes - 1))
    range_bars = [
        bar for bar in bars
        if bar["dt"] <= complete_at
    ] or bars[:1]
    complete = bars[-1]["dt"] >= complete_at
    highs = [_float(bar.get("high")) for bar in range_bars]
    lows = [_float(bar.get("low")) for bar in range_bars]
    high_values = [value for value in highs if value is not None]
    low_values = [value for value in lows if value is not None]
    if not high_values or not low_values:
        first_close = _float(range_bars[0].get("close"), 0.0) or 0.0
        high_values = high_values or [first_close]
        low_values = low_values or [first_close]
    return {
        "high": max(high_values),
        "low": min(low_values),
        "minutes": minutes,
        "start": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "complete_at": complete_at.strftime("%Y-%m-%d %H:%M:%S"),
        "complete": complete,
        "bars": len(range_bars),
    }, complete_at


def _vwap(bars: List[Dict[str, Any]]) -> Optional[float]:
    weighted_sum = 0.0
    volume_sum = 0.0
    close_sum = 0.0
    close_count = 0
    for bar in bars:
        close = _float(bar.get("close"))
        if close is None:
            continue
        high = _float(bar.get("high"), close)
        low = _float(bar.get("low"), close)
        typical = (high + low + close) / 3.0
        volume = max(0.0, _float(bar.get("volume"), 0.0))
        if volume > 0:
            weighted_sum += typical * volume
            volume_sum += volume
        close_sum += close
        close_count += 1
    if volume_sum > 0:
        return weighted_sum / volume_sum
    if close_count > 0:
        return close_sum / close_count
    return None


def _passes_chase_filter(
    *,
    action_value: str,
    signal_close: float,
    execution_open: Optional[float],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    threshold = float(config.get("max_chase_ratio", 0.015) or 0.015)
    if execution_open is None or signal_close <= 0:
        return {"passed": False, "reason": "missing_execution_open"}
    gap_ratio = (execution_open - signal_close) / signal_close
    if action_value in _BUY_LIKE_ACTIONS and gap_ratio > threshold:
        return {"passed": False, "gap_ratio": gap_ratio, "threshold": threshold}
    if action_value in _SELL_LIKE_ACTIONS and gap_ratio < -threshold:
        return {"passed": False, "gap_ratio": gap_ratio, "threshold": threshold}
    return {"passed": True, "gap_ratio": gap_ratio, "threshold": threshold}


def _float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _normalize_date(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    return datetime.strptime(str(value)[:10], "%Y-%m-%d")


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value
