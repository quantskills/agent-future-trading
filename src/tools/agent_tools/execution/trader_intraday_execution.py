from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from graph.schema import BasePriceSource, MorningExecutionBasis
from tools.common.contracts import sanitize_execution_contract
from tools.common.execution_trigger_semantics import (
    entry_invalidation_contract_error,
    execution_trigger_contract_error,
    normalize_execution_profile,
    normalize_trigger_confirmation_adjustment,
    requires_strict_trigger_confirmation,
    requires_stronger_trigger_confirmation,
)


_BUY_LIKE_ACTIONS = {"open_long", "close_short"}
_SELL_LIKE_ACTIONS = {"open_short", "close_long"}
_OPEN_ACTIONS = {"open_long", "open_short"}
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
        trigger_checked = self.reason not in {
            "hold_or_zero_lots",
            "intraday_immediate_execution",
            "intraday_event_immediate_execution",
        }
        trigger_passed = self.decision == "execute" and self.reason in {
            "intraday_trigger_confirmed",
            "intraday_immediate_execution",
            "intraday_event_immediate_execution",
            "intraday_pullback_confirmed",
            "intraday_vwap_confirmed",
        }
        execution_failure_reason = "" if self.decision == "execute" else self.reason
        missed_opportunity_flag = bool(
            self.decision in {"skip", "wait"}
            and self.reason
            not in {
                "hold_or_zero_lots",
                "fac_invalidated_before_entry",
                "fac_expired_before_entry",
            }
        )
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
    except Exception:
        raise RuntimeError("intraday_market_data_fetch_failed") from None

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
        sanitize_execution_contract(decision_context.get("execution_contract"))
        if isinstance(decision_context.get("execution_contract"), dict)
        else {}
    )
    execution_profile = _execution_profile_from_context(decision_context)
    can_execute_without_intraday_trigger = bool(execution_contract.get("can_execute_without_intraday_trigger"))
    execution_side = (
        "long"
        if action_value in _BUY_LIKE_ACTIONS
        else "short"
        if action_value in _SELL_LIKE_ACTIONS
        else "flat"
    )
    entry_action = action_value in _OPEN_ACTIONS
    trigger_confirmation_adjustment = normalize_trigger_confirmation_adjustment(
        execution_contract.get("trigger_confirmation_adjustment")
    )
    contract_error = execution_trigger_contract_error(
        profile=execution_profile,
        side=execution_side,
        entry_trigger=execution_contract.get("entry_trigger"),
        trigger_source=execution_contract.get("trigger_source"),
        trigger_confirmation_adjustment=execution_contract.get(
            "trigger_confirmation_adjustment"
        ),
    )
    if contract_error:
        return IntradayExecutionSelection(
            decision="skip" if finalize_untriggered else "wait",
            reason=contract_error,
            features={
                "execution_profile": execution_profile,
                "contract_validation": "failed",
            },
        )
    if entry_action:
        invalidation_error = entry_invalidation_contract_error(
            profile=execution_profile,
            side=execution_side,
            invalidation_condition=execution_contract.get("invalidation"),
            invalidation_level=execution_contract.get("invalidation_level"),
        )
        if invalidation_error:
            return IntradayExecutionSelection(
                decision="skip" if finalize_untriggered else "wait",
                reason=invalidation_error,
                features={
                    "execution_profile": execution_profile,
                    "contract_validation": "failed",
                },
            )
        valid_until = _parse_valid_until(execution_contract.get("valid_until"))
        if valid_until is None:
            return IntradayExecutionSelection(
                decision="skip" if finalize_untriggered else "wait",
                reason="execution_valid_until_invalid",
                features={
                    "execution_profile": execution_profile,
                    "contract_validation": "failed",
                },
            )
        invalidation_level = float(execution_contract["invalidation_level"])
    else:
        valid_until = None
        invalidation_level = None

    if not normalized_execution_bars:
        return IntradayExecutionSelection(
            decision="skip" if finalize_untriggered else "wait",
            reason="intraday_no_valid_bar",
            features={"execution_bars": 0, "execution_profile": execution_profile},
        )

    direct_contract_execution = bool(
        can_execute_without_intraday_trigger
        and action_value in _BUY_LIKE_ACTIONS | _SELL_LIKE_ACTIONS
    )
    if force_immediate or action_value in _IMMEDIATE_ACTIONS or direct_contract_execution:
        execution_bar = _first_valid_execution_bar(normalized_execution_bars, min_volume=min_volume)
        if entry_action:
            bars_before_fill = [
                bar
                for bar in normalized_execution_bars
                if execution_bar is None or bar["dt"] < execution_bar["dt"]
            ]
            invalidation_before_fill = _first_entry_invalidation_bar(
                bars_before_fill,
                side=execution_side,
                invalidation_level=invalidation_level,
                valid_until=valid_until,
            )
            if invalidation_before_fill is not None:
                return _entry_non_execution_selection(
                    reason="fac_invalidated_before_entry",
                    execution_profile=execution_profile,
                    execution_contract=execution_contract,
                    observed_bar=invalidation_before_fill,
                )
            expiry_observation = next(
                (
                    bar
                    for bar in normalized_execution_bars
                    if bar["dt"] > valid_until
                    and (execution_bar is None or bar["dt"] <= execution_bar["dt"])
                ),
                None,
            )
            if expiry_observation is not None:
                return _entry_non_execution_selection(
                    reason="fac_expired_before_entry",
                    execution_profile=execution_profile,
                    execution_contract=execution_contract,
                    observed_bar=expiry_observation,
                )
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
        if entry_action:
            if _entry_invalidation_at_open(
                execution_bar,
                side=execution_side,
                invalidation_level=invalidation_level,
            ):
                return _entry_non_execution_selection(
                    reason="fac_invalidated_before_entry",
                    execution_profile=execution_profile,
                    execution_contract=execution_contract,
                    observed_bar=execution_bar,
                )
        reason = (
            "intraday_event_immediate_execution"
            if execution_profile == "event_immediate" and direct_contract_execution
            else "intraday_immediate_execution"
        )
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
    first_invalidation_bar = _first_entry_invalidation_bar(
        normalized_execution_bars,
        side=execution_side,
        invalidation_level=invalidation_level,
        valid_until=valid_until,
    )
    if require_complete_opening_range and opening_range_complete_at is not None:
        latest_execution_dt = normalized_execution_bars[-1]["dt"]
        if latest_execution_dt < opening_range_complete_at:
            if first_invalidation_bar is not None:
                return _entry_non_execution_selection(
                    reason="fac_invalidated_before_entry",
                    execution_profile=execution_profile,
                    execution_contract=execution_contract,
                    observed_bar=first_invalidation_bar,
                )
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

    long_expansion_seen = False
    short_expansion_seen = False
    for signal_index, signal_bar in enumerate(signal_bars_for_trigger):
        if signal_bar["dt"] > valid_until:
            return _entry_non_execution_selection(
                reason="fac_expired_before_entry",
                execution_profile=execution_profile,
                execution_contract=execution_contract,
                observed_bar=signal_bar,
            )
        if (
            first_invalidation_bar is not None
            and first_invalidation_bar["dt"] <= signal_bar["dt"]
        ):
            return _entry_non_execution_selection(
                reason="fac_invalidated_before_entry",
                execution_profile=execution_profile,
                execution_contract=execution_contract,
                observed_bar=first_invalidation_bar,
            )
        historical_exec_bars = [bar for bar in normalized_execution_bars if bar["dt"] <= signal_bar["dt"]]
        if not historical_exec_bars:
            continue
        vwap_value = _vwap(historical_exec_bars)
        signal_close = _float(signal_bar.get("close"))
        signal_high = _float(signal_bar.get("high"), signal_close)
        signal_low = _float(signal_bar.get("low"), signal_close)
        if (
            signal_close is None
            or signal_high is None
            or signal_low is None
            or vwap_value is None
        ):
            continue

        long_breakout = (
            action_value in _BUY_LIKE_ACTIONS
            and signal_close > vwap_value
            and signal_close > opening_range["high"]
        )
        short_breakout = (
            action_value in _SELL_LIKE_ACTIONS
            and signal_close < vwap_value
            and signal_close < opening_range["low"]
        )
        long_reclaimed_opening = bool(
            signal_low <= opening_range["high"] and signal_close > opening_range["high"]
        )
        long_reclaimed_vwap = bool(
            signal_low <= vwap_value and signal_close > vwap_value
        )
        short_reclaimed_opening = bool(
            signal_high >= opening_range["low"] and signal_close < opening_range["low"]
        )
        short_reclaimed_vwap = bool(
            signal_high >= vwap_value and signal_close < vwap_value
        )
        long_reclaimed_boundary = bool(long_reclaimed_opening or long_reclaimed_vwap)
        short_reclaimed_boundary = bool(short_reclaimed_opening or short_reclaimed_vwap)
        long_pullback = (
            execution_profile == "pullback"
            and action_value in _BUY_LIKE_ACTIONS
            and long_expansion_seen
            and long_reclaimed_boundary
        )
        short_pullback = (
            execution_profile == "pullback"
            and action_value in _SELL_LIKE_ACTIONS
            and short_expansion_seen
            and short_reclaimed_boundary
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
            long_expansion_seen = bool(
                long_expansion_seen
                or (
                    signal_close > opening_range["high"]
                    and signal_close > vwap_value
                )
            )
            short_expansion_seen = bool(
                short_expansion_seen
                or (
                    signal_close < opening_range["low"]
                    and signal_close < vwap_value
                )
            )
            continue

        initial_trigger_bar = signal_bar
        confirmed_signal_bar = signal_bar
        volume_confirmation: Dict[str, Any] = {
            "required": False,
            "passed": None,
        }
        if requires_stronger_trigger_confirmation(trigger_confirmation_adjustment):
            required_follow_through_bars = (
                2
                if requires_strict_trigger_confirmation(
                    trigger_confirmation_adjustment
                )
                else 1
            )
            next_index = signal_index + 1
            if next_index + required_follow_through_bars > len(signal_bars_for_trigger):
                return IntradayExecutionSelection(
                    decision="skip" if finalize_untriggered else "wait",
                    reason=(
                        "intraday_trigger_not_met"
                        if finalize_untriggered
                        else "intraday_waiting_for_trigger"
                    ),
                    features={
                        "execution_profile": execution_profile,
                        "execution_contract": execution_contract,
                        "trigger_confirmation_adjustment": trigger_confirmation_adjustment,
                        "initial_trigger_datetime": signal_bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                        "waiting_for_follow_through": True,
                        "required_follow_through_bars": required_follow_through_bars,
                    },
                )
            follow_bars = signal_bars_for_trigger[
                next_index : next_index + required_follow_through_bars
            ]
            confirmation_failed = False
            for follow_bar in follow_bars:
                if follow_bar["dt"] > valid_until:
                    return _entry_non_execution_selection(
                        reason="fac_expired_before_entry",
                        execution_profile=execution_profile,
                        execution_contract=execution_contract,
                        observed_bar=follow_bar,
                    )
                if (
                    first_invalidation_bar is not None
                    and first_invalidation_bar["dt"] <= follow_bar["dt"]
                ):
                    return _entry_non_execution_selection(
                        reason="fac_invalidated_before_entry",
                        execution_profile=execution_profile,
                        execution_contract=execution_contract,
                        observed_bar=first_invalidation_bar,
                    )
                follow_exec_bars = [
                    bar
                    for bar in normalized_execution_bars
                    if bar["dt"] <= follow_bar["dt"]
                ]
                follow_vwap = _vwap(follow_exec_bars) if follow_exec_bars else None
                follow_close = _float(follow_bar.get("close"))
                follow_through = False
                if follow_close is not None and follow_vwap is not None:
                    if execution_profile == "breakout":
                        follow_through = bool(
                            (
                                long_trigger
                                and follow_close > opening_range["high"]
                                and follow_close > follow_vwap
                            )
                            or (
                                short_trigger
                                and follow_close < opening_range["low"]
                                and follow_close < follow_vwap
                            )
                        )
                    elif execution_profile == "vwap_confirmed":
                        follow_through = bool(
                            (long_trigger and follow_close > follow_vwap)
                            or (short_trigger and follow_close < follow_vwap)
                        )
                    elif long_trigger:
                        required_checks = []
                        if long_reclaimed_opening:
                            required_checks.append(
                                follow_close > opening_range["high"]
                            )
                        if long_reclaimed_vwap:
                            required_checks.append(follow_close > follow_vwap)
                        follow_through = bool(
                            required_checks and all(required_checks)
                        )
                    elif short_trigger:
                        required_checks = []
                        if short_reclaimed_opening:
                            required_checks.append(
                                follow_close < opening_range["low"]
                            )
                        if short_reclaimed_vwap:
                            required_checks.append(follow_close < follow_vwap)
                        follow_through = bool(
                            required_checks and all(required_checks)
                        )
                if not follow_through:
                    confirmation_failed = True
                    break
                confirmed_signal_bar = follow_bar
                signal_close = follow_close
                vwap_value = follow_vwap
            if confirmation_failed:
                continue
            volume_confirmation = _stronger_confirmation_volume_check(
                signal_bars=normalized_signal_bars,
                initial_trigger_bar=initial_trigger_bar,
                follow_bars=follow_bars,
                config=config,
            )
            if not bool(volume_confirmation.get("passed")):
                continue

        execution_bar = _next_execution_bar(
            normalized_execution_bars,
            after_dt=confirmed_signal_bar["dt"],
            min_volume=min_volume,
        )
        if execution_bar is None:
            if requires_stronger_trigger_confirmation(trigger_confirmation_adjustment):
                return IntradayExecutionSelection(
                    decision="skip" if finalize_untriggered else "wait",
                    reason=(
                        "intraday_trigger_not_met"
                        if finalize_untriggered
                        else "intraday_waiting_for_trigger"
                    ),
                    signal_datetime=confirmed_signal_bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                    features={
                        "execution_profile": execution_profile,
                        "execution_contract": execution_contract,
                        "trigger_confirmation_adjustment": trigger_confirmation_adjustment,
                        "initial_trigger_datetime": initial_trigger_bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                        "follow_through_datetime": confirmed_signal_bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                        "waiting_for_execution_bar": True,
                    },
                )
            continue
        if execution_bar["dt"] > valid_until:
            return _entry_non_execution_selection(
                reason="fac_expired_before_entry",
                execution_profile=execution_profile,
                execution_contract=execution_contract,
                observed_bar=execution_bar,
            )
        invalidation_before_fill = _entry_invalidation_before_fill(
            normalized_execution_bars,
            trigger_dt=confirmed_signal_bar["dt"],
            execution_bar=execution_bar,
            side=execution_side,
            invalidation_level=invalidation_level,
        )
        if invalidation_before_fill is not None:
            return _entry_non_execution_selection(
                reason="fac_invalidated_before_entry",
                execution_profile=execution_profile,
                execution_contract=execution_contract,
                observed_bar=invalidation_before_fill,
            )

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
            trigger_rule = "directional_expansion_then_boundary_or_vwap_reclaim"
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
            signal_bar=confirmed_signal_bar,
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
                "trigger_confirmation_adjustment": trigger_confirmation_adjustment,
                "volume_confirmation": volume_confirmation,
                "initial_trigger_datetime": initial_trigger_bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
            },
            source=BasePriceSource.INTRADAY_NEXT_1M_OPEN,
        )

    if first_invalidation_bar is not None:
        return _entry_non_execution_selection(
            reason="fac_invalidated_before_entry",
            execution_profile=execution_profile,
            execution_contract=execution_contract,
            observed_bar=first_invalidation_bar,
        )
    if normalized_execution_bars[-1]["dt"] > valid_until:
        return _entry_non_execution_selection(
            reason="fac_expired_before_entry",
            execution_profile=execution_profile,
            execution_contract=execution_contract,
            observed_bar=normalized_execution_bars[-1],
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
        sanitize_execution_contract(decision_context.get("execution_contract"))
        if isinstance(decision_context.get("execution_contract"), dict)
        else {}
    )
    return normalize_execution_profile(execution_contract.get("execution_profile"))


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


def _entry_non_execution_selection(
    *,
    reason: str,
    execution_profile: str,
    execution_contract: Dict[str, Any],
    observed_bar: Dict[str, Any],
) -> IntradayExecutionSelection:
    observed_price = (
        _float(observed_bar.get("low"))
        if str(execution_contract.get("invalidation") or "").startswith("long_")
        else _float(observed_bar.get("high"))
    )
    return IntradayExecutionSelection(
        decision="skip",
        reason=reason,
        signal_datetime=observed_bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
        features={
            "execution_profile": execution_profile,
            "execution_contract": execution_contract,
            "entry_contract_state": "permanently_invalidated" if reason == "fac_invalidated_before_entry" else "expired",
            "observed_datetime": observed_bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
            "observed_price": observed_price,
            "invalidation_level": execution_contract.get("invalidation_level"),
            "valid_until": execution_contract.get("valid_until"),
        },
    )


def _entry_bar_breaches_level(
    bar: Dict[str, Any],
    *,
    side: str,
    invalidation_level: float,
) -> bool:
    if side == "long":
        low = _float(bar.get("low"), _float(bar.get("open")))
        return low is not None and low <= invalidation_level
    if side == "short":
        high = _float(bar.get("high"), _float(bar.get("open")))
        return high is not None and high >= invalidation_level
    return False


def _entry_invalidation_at_open(
    bar: Dict[str, Any],
    *,
    side: str,
    invalidation_level: float,
) -> bool:
    open_price = _float(bar.get("open"))
    if open_price is None:
        return False
    if side == "long":
        return open_price <= invalidation_level
    if side == "short":
        return open_price >= invalidation_level
    return False


def _first_entry_invalidation_bar(
    bars: List[Dict[str, Any]],
    *,
    side: str,
    invalidation_level: float,
    valid_until: datetime,
) -> Optional[Dict[str, Any]]:
    for bar in bars:
        if bar["dt"] > valid_until:
            break
        if _entry_bar_breaches_level(
            bar,
            side=side,
            invalidation_level=invalidation_level,
        ):
            return bar
    return None


def _entry_invalidation_before_fill(
    bars: List[Dict[str, Any]],
    *,
    trigger_dt: datetime,
    execution_bar: Dict[str, Any],
    side: str,
    invalidation_level: float,
) -> Optional[Dict[str, Any]]:
    for bar in bars:
        if bar["dt"] <= trigger_dt or bar["dt"] >= execution_bar["dt"]:
            continue
        if _entry_bar_breaches_level(
            bar,
            side=side,
            invalidation_level=invalidation_level,
        ):
            return bar
    if _entry_invalidation_at_open(
        execution_bar,
        side=side,
        invalidation_level=invalidation_level,
    ):
        return execution_bar
    return None


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


def _stronger_confirmation_volume_check(
    *,
    signal_bars: List[Dict[str, Any]],
    initial_trigger_bar: Dict[str, Any],
    follow_bars: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Require participated follow-through only on PM-strengthened entries."""
    lookback_bars = max(
        1,
        int(config.get("stronger_confirmation_volume_lookback_bars", 4) or 4),
    )
    min_ratio = max(
        0.0,
        float(config.get("stronger_confirmation_min_volume_ratio", 1.0) or 1.0),
    )
    prior_volumes = [
        max(0.0, _float(bar.get("volume"), 0.0) or 0.0)
        for bar in signal_bars
        if bar["dt"] < initial_trigger_bar["dt"]
    ][-lookback_bars:]
    prior_volumes = [value for value in prior_volumes if value > 0.0]
    initial_volume = max(
        0.0,
        _float(initial_trigger_bar.get("volume"), 0.0) or 0.0,
    )
    confirmation_volumes = [
        max(0.0, _float(bar.get("volume"), 0.0) or 0.0)
        for bar in follow_bars
    ]
    follow_volume = confirmation_volumes[-1] if confirmation_volumes else 0.0
    reference_volume = (
        sum(prior_volumes) / len(prior_volumes)
        if prior_volumes
        else None
    )
    confirmation_average_volume = (
        (initial_volume + sum(confirmation_volumes))
        / (1 + len(confirmation_volumes))
        if confirmation_volumes
        else 0.0
    )
    comparable_baseline_available = bool(
        reference_volume is not None and reference_volume > 0.0
    )
    passed = bool(
        initial_volume > 0.0
        and bool(confirmation_volumes)
        and all(value > 0.0 for value in confirmation_volumes)
        and (
            not comparable_baseline_available
            or confirmation_average_volume >= float(reference_volume) * min_ratio
        )
    )
    return {
        "required": True,
        "passed": passed,
        "lookback_bars": lookback_bars,
        "reference_volume": reference_volume,
        "initial_trigger_volume": initial_volume,
        "follow_through_volume": follow_volume,
        "follow_through_volumes": confirmation_volumes,
        "confirmation_bar_count": len(confirmation_volumes),
        "two_bar_average_volume": confirmation_average_volume,
        "confirmation_average_volume": confirmation_average_volume,
        "min_volume_ratio": min_ratio,
        "comparable_baseline_available": comparable_baseline_available,
        "method": (
            "initial_and_confirmation_average_vs_prior_completed_signal_average"
            if comparable_baseline_available
            else "nonzero_initial_and_follow_without_comparable_prior_signal_bar"
        ),
    }


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


def _parse_valid_until(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    if len(text) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed.replace(microsecond=0)


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value
