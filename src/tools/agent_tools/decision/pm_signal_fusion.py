from __future__ import annotations

"""Signal-fusion helpers shared by PM, reviewer tests, and future refactors."""

from datetime import date, datetime
from typing import Any, Iterable, Mapping

from tools.common.evidence_fusion_semantics import build_pm_fusion_diagnostics
from tools.common.final_action_semantics import (
    ACTION_FAMILY_EXECUTION,
    ACTION_FAMILY_OPEN_ADD_NEW_RISK,
    POSITIVE_EXECUTION_ACTION_PREFERENCES,
    POSITIVE_EXIT_ACTION_PREFERENCES,
    POSITIVE_HOLD_ACTION_PREFERENCES,
    POSITIVE_OPEN_ACTION_PREFERENCES,
    PROTECTIVE_ACTION_PREFERENCES,
    validate_action_preference_family_consistency,
)
from tools.common.signal_evidence_collection import has_concrete_entry_trigger


ANALYST_ORDER = ("technical", "fundamental", "commodity_news")


def build_forecast_calibration_summary(
    *,
    analyst_signals: Iterable[Any],
    analyst_performance: Iterable[Mapping[str, Any]] | None,
    target_side: str,
    expected_horizon_days: int | None = None,
) -> dict[str, Any]:
    """Calibrate the current three-analyst forecast at the candidate horizon."""
    target = str(target_side or "").lower()
    signals = list(analyst_signals or [])
    signal_by_analyst = {
        str(getattr(signal, "agent_name", "") or ""): signal
        for signal in signals
    }
    candidate_horizon_days = _safe_int(expected_horizon_days, 0)
    if candidate_horizon_days <= 0:
        for analyst in ("fundamental", "technical", "commodity_news"):
            signal = signal_by_analyst.get(analyst)
            if signal is None or _signal_side(signal) != target:
                continue
            candidate_horizon_days = _safe_int(
                _contract_value(signal, "expected_horizon_days", 0),
                0,
            )
            if candidate_horizon_days > 0:
                break
    matched_horizon_days = _forecast_horizon_days(candidate_horizon_days)
    horizon_class = f"{matched_horizon_days}d" if matched_horizon_days else ""

    forecasts_by_analyst: dict[str, dict[str, Any]] = {}
    if matched_horizon_days:
        for analyst in ANALYST_ORDER:
            signal = signal_by_analyst.get(analyst)
            forecast = _forecast_for_horizon(signal, matched_horizon_days)
            if forecast:
                forecasts_by_analyst[analyst] = forecast

    cold_summary = {
        "status": "cold_start",
        "sample_count": 0,
        "candidate_expected_horizon_days": candidate_horizon_days,
        "matched_horizon_days": matched_horizon_days,
        "direction_probability": 1.0 / 3.0,
        "opposite_direction_probability": 1.0 / 3.0,
        "range_probability": 1.0 / 3.0,
        "direction_accuracy": 0.5,
        "mean_brier_score": 1.0 / 3.0,
        "expected_return_after_fee": 0.0,
        "market_regime_match": 0.5,
        "rank_signal": 0.0,
        "source_rows": [],
        "not_trade_authority": True,
    }
    if target not in {"long", "short"} or not forecasts_by_analyst:
        return cold_summary

    best_candidates: dict[str, dict[str, Any]] = {}
    for raw in analyst_performance or []:
        if not isinstance(raw, Mapping):
            continue
        payload = raw.get("payload") if isinstance(raw.get("payload"), Mapping) else {}
        calibration = payload.get("forecast_calibration_summary") if isinstance(payload.get("forecast_calibration_summary"), Mapping) else {}
        if not calibration:
            continue
        analyst = str(raw.get("analyst") or "")
        signal = signal_by_analyst.get(analyst)
        forecast = forecasts_by_analyst.get(analyst)
        if signal is None or forecast is None:
            continue
        row_horizon_days = _safe_int(calibration.get("horizon_days"), 0)
        if row_horizon_days <= 0:
            row_horizon = str(raw.get("horizon_class") or "").strip().lower()
            row_horizon_days = _safe_int(row_horizon[:-1], 0) if row_horizon.endswith("d") else 0
        if row_horizon_days != matched_horizon_days:
            continue
        current_ticker = str(_contract_value(signal, "ticker", "") or "").upper()
        current_sector = str(_contract_value(signal, "sector", "*") or "*")
        current_regime = str(_contract_value(signal, "market_regime", "*") or "*")
        row_ticker = str(raw.get("ticker") or "*").upper()
        row_sector = str(raw.get("sector") or "*")
        if row_ticker != "*" and current_ticker and row_ticker != current_ticker:
            continue
        if row_ticker == "*" and row_sector != "*" and current_sector != "*" and row_sector != current_sector:
            continue
        current_signal_side = str(_contract_value(signal, "side", _signal_side(signal)) or "flat").lower()
        row_side = str(raw.get("signal_side") or "flat").lower()
        if row_side not in {current_signal_side, "*"}:
            continue
        sample_count = _safe_int(raw.get("sample_count"), 0)
        if sample_count < 2:
            continue
        scope_level = str(calibration.get("scope_level") or "global")
        scope_weight = {"ticker": 1.0, "sector": 0.8, "global": 0.6}.get(scope_level, 0.5)
        confidence = max(0.0, min(1.0, _safe_float(raw.get("confidence_score"), 0.0)))
        regime_performance = calibration.get("market_regime_performance")
        regime_performance = regime_performance if isinstance(regime_performance, Mapping) else {}
        regime_metrics = regime_performance.get(current_regime)
        regime_metrics = regime_metrics if isinstance(regime_metrics, Mapping) else calibration
        regime_match = 1.0 if current_regime in regime_performance else _safe_float(
            calibration.get("market_regime_match"), 0.5
        )
        direction_hit_rate = max(
            0.0,
            min(1.0, _safe_float(regime_metrics.get("direction_hit_rate"), 0.5)),
        )
        mean_brier_score = max(
            0.0,
            min(2.0 / 3.0, _safe_float(regime_metrics.get("mean_brier_score"), 1.0 / 3.0)),
        )
        regime_match = max(0.0, min(1.0, regime_match))
        historical_after_fee = _safe_float(
            regime_metrics.get("mean_predicted_side_return_after_fee"),
            0.0,
        )
        historical_calibration_signal = max(
            -1.0,
            min(
                1.0,
                0.45 * (direction_hit_rate - 0.5) * 2.0
                + 0.35 * max(-1.0, min(1.0, historical_after_fee / 0.02))
                + 0.20 * (regime_match - 0.5) * 2.0
                - 0.15 * max(0.0, min(1.0, mean_brier_score / (2.0 / 3.0))),
            ),
        )
        # Keep the probability distribution valid and use signed calibration
        # strength for the Rank contribution. A negative after-fee history
        # must reverse the current Rank spread, not create negative probabilities.
        calibration_reliability = 0.5 * (historical_calibration_signal + 1.0)
        calibration_strength = historical_calibration_signal
        target_probability = _safe_float(
            forecast.get("up_probability" if target == "long" else "down_probability"),
            1.0 / 3.0,
        )
        opposite_probability = _safe_float(
            forecast.get("down_probability" if target == "long" else "up_probability"),
            1.0 / 3.0,
        )
        range_probability = _safe_float(forecast.get("range_probability"), 1.0 / 3.0)
        calibrated_target_probability = (
            1.0 / 3.0
            + calibration_reliability * (target_probability - 1.0 / 3.0)
        )
        calibrated_opposite_probability = (
            1.0 / 3.0
            + calibration_reliability * (opposite_probability - 1.0 / 3.0)
        )
        calibrated_range_probability = (
            1.0 / 3.0
            + calibration_reliability * (range_probability - 1.0 / 3.0)
        )
        current_target_return = _safe_float(forecast.get("expected_return"), 0.0)
        if target == "short":
            current_target_return = -current_target_return
        analyst_rank_signal = (
            target_probability - opposite_probability
        ) * calibration_strength
        candidate = {
            "analyst": analyst,
            "signal_side": current_signal_side,
            "horizon_class": horizon_class,
            "horizon_days": matched_horizon_days,
            "scope_level": scope_level,
            "sample_count": _safe_int(regime_metrics.get("sample_count"), sample_count),
            "weight": max(0.01, scope_weight * max(0.25, confidence)),
            "direction_hit_rate": direction_hit_rate,
            "mean_brier_score": mean_brier_score,
            "mean_predicted_side_return_after_fee": historical_after_fee,
            "market_regime": current_regime,
            "market_regime_match": regime_match,
            "target_probability": target_probability,
            "opposite_probability": opposite_probability,
            "range_probability": range_probability,
            "historical_calibration_signal": historical_calibration_signal,
            "calibration_reliability": calibration_reliability,
            "calibration_strength": calibration_strength,
            "calibrated_target_probability": calibrated_target_probability,
            "calibrated_opposite_probability": calibrated_opposite_probability,
            "calibrated_range_probability": calibrated_range_probability,
            "current_target_expected_return": current_target_return,
            "calibrated_expected_return_after_fee": historical_after_fee,
            "rank_signal": analyst_rank_signal,
            "calibration_status": "matured",
        }
        previous = best_candidates.get(analyst)
        specificity = {"ticker": 3, "sector": 2, "global": 1}.get(scope_level, 0)
        previous_specificity = {
            "ticker": 3,
            "sector": 2,
            "global": 1,
        }.get(str((previous or {}).get("scope_level") or ""), -1)
        if previous is None or specificity > previous_specificity:
            best_candidates[analyst] = candidate

    source_rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for analyst in ANALYST_ORDER:
        forecast = forecasts_by_analyst.get(analyst)
        if forecast is None:
            continue
        candidate = best_candidates.get(analyst)
        if candidate is not None:
            candidates.append(candidate)
            source_rows.append(candidate)
            continue
        signal = signal_by_analyst[analyst]
        source_rows.append(
            {
                "analyst": analyst,
                "signal_side": str(_contract_value(signal, "side", _signal_side(signal)) or "flat").lower(),
                "horizon_class": horizon_class,
                "horizon_days": matched_horizon_days,
                "scope_level": "none",
                "sample_count": 0,
                "weight": 0.0,
                "target_probability": _safe_float(
                    forecast.get("up_probability" if target == "long" else "down_probability"),
                    1.0 / 3.0,
                ),
                "opposite_probability": _safe_float(
                    forecast.get("down_probability" if target == "long" else "up_probability"),
                    1.0 / 3.0,
                ),
                "range_probability": _safe_float(forecast.get("range_probability"), 1.0 / 3.0),
                "calibration_status": "cold_start",
                "rank_signal": 0.0,
            }
        )
    if not candidates:
        return {**cold_summary, "source_rows": source_rows}
    total_weight = sum(item["weight"] for item in candidates)
    weighted = lambda key: sum(item[key] * item["weight"] for item in candidates) / total_weight
    accuracy = weighted("direction_hit_rate")
    brier = weighted("mean_brier_score")
    expected_after_fee = weighted("calibrated_expected_return_after_fee")
    regime_match = weighted("market_regime_match")
    rank_signal = weighted("rank_signal")
    return {
        "status": "matured",
        "sample_count": sum(item["sample_count"] for item in candidates),
        "candidate_expected_horizon_days": candidate_horizon_days,
        "matched_horizon_days": matched_horizon_days,
        "direction_probability": round(weighted("calibrated_target_probability"), 6),
        "opposite_direction_probability": round(weighted("calibrated_opposite_probability"), 6),
        "range_probability": round(weighted("calibrated_range_probability"), 6),
        "direction_accuracy": round(accuracy, 6),
        "mean_brier_score": round(brier, 6),
        "expected_return_after_fee": round(expected_after_fee, 8),
        "market_regime_match": round(regime_match, 6),
        "rank_signal": round(max(-1.0, min(1.0, rank_signal)), 6),
        "source_rows": source_rows,
        "not_trade_authority": True,
    }


def _forecast_horizon_days(expected_horizon_days: Any) -> int:
    expected = _safe_int(expected_horizon_days, 0)
    if expected <= 0:
        return 0
    if expected == 1:
        return 1
    if expected <= 3:
        return 3
    if expected <= 5:
        return 5
    return 10


def _forecast_for_horizon(signal: Any, horizon_days: int) -> dict[str, Any]:
    if signal is None or horizon_days not in {1, 3, 5, 10}:
        return {}
    forecasts = _contract_value(signal, "forward_forecasts", [])
    for forecast in forecasts if isinstance(forecasts, list) else []:
        if not isinstance(forecast, Mapping):
            continue
        if _safe_int(forecast.get("horizon_days"), 0) == horizon_days:
            return dict(forecast)
    return {}


def build_scc_market_confirmation(
    signal_collection_contract: Mapping[str, Any] | None,
    *,
    target_direction: str,
) -> dict[str, Any]:
    """Translate the validated SCC into PM's existing confirmation input shape."""
    contract = signal_collection_contract if isinstance(signal_collection_contract, Mapping) else {}
    evidence_items = [
        dict(item)
        for item in (contract.get("evidence_items") or [])
        if isinstance(item, Mapping)
    ]
    fusion = contract.get("evidence_fusion") if isinstance(contract.get("evidence_fusion"), Mapping) else {}
    target = str(target_direction or "").strip().lower()
    dominant = str(contract.get("dominant_side") or "flat").strip().lower()
    consensus_score = _safe_float(fusion.get("multi_evidence_consensus_score"), 0.0)
    direction_supported = target in {"long", "short"} and dominant == target
    confirmations = [
        str(item.get("analyst") or "")
        for item in evidence_items
        if str(item.get("side") or "").lower() == target
    ]
    conflicts = [str(item) for item in (fusion.get("cross_analyst_conflicts") or [])]
    if target in {"long", "short"} and dominant not in {target, "flat"}:
        conflicts.append(f"scc_dominant_side:{dominant}")
    return {
        "enabled": bool(contract),
        "target_direction": target,
        "confirmation_score": round(consensus_score if direction_supported else 0.0, 4),
        "features": evidence_items,
        "confirmations": sorted(set(name for name in confirmations if name)),
        "conflicts": sorted(set(conflicts)),
        "data_missing": [],
        "data_unavailable": [],
        "fallback_covered_missing": [],
        "errors": [],
        "parameter_errors": [],
        "status": "supported" if direction_supported else "not_supported",
    }

def normalize_analyst_name(value: Any) -> str:
    return str(value or "")


def signal_to_text(value: Any) -> str:
    return str(value.value) if hasattr(value, "value") else str(value or "Neutral")


def analyst_signal_combo(analyst_signals: Iterable[Any]) -> tuple[str, str, str]:
    signals = {name: "Neutral" for name in ANALYST_ORDER}
    for signal in analyst_signals or []:
        agent_name = normalize_analyst_name(getattr(signal, "agent_name", ""))
        if agent_name in signals:
            signals[agent_name] = signal_to_text(getattr(signal, "signal", "Neutral"))
    return (signals["technical"], signals["fundamental"], signals["commodity_news"])


def resolve_decision_horizon(analyst_signals: Iterable[Any], target_lots: int) -> str:
    if target_lots == 0:
        return "flat"
    target_signal = "Bullish" if target_lots > 0 else "Bearish"
    horizons = []
    for signal in analyst_signals or []:
        if signal_to_text(getattr(signal, "signal", "Neutral")) != target_signal:
            continue
        horizons.append(
            str(
                getattr(signal, "analyst_horizon", None)
                or getattr(signal, "horizon_class", None)
                or "unknown"
            )
        )
    if "medium" in horizons:
        return "medium"
    if "event_short" in horizons:
        return "event_short"
    if "short" in horizons:
        return "short"
    return "unknown"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple) or isinstance(value, set):
        return list(value)
    return [value]


def _signal_side(signal: Any) -> str:
    text = signal_to_text(getattr(signal, "signal", "Neutral")).lower()
    if text == "bullish":
        return "long"
    if text == "bearish":
        return "short"
    return "flat"


def _contract_value(signal: Any, key: str, default: Any = None) -> Any:
    metadata = getattr(signal, "metadata", None)
    contract = metadata.get("action_evidence_contract") if isinstance(metadata, Mapping) else None
    if isinstance(contract, Mapping):
        return contract.get(key, default)
    return default


def _opportunity_state(signal: Any) -> str:
    state = (
        getattr(signal, "opportunity_state", None)
        or _contract_value(signal, "opportunity_state")
    )
    return str(state or "unknown").strip().lower() or "unknown"


def _signal_bool(signal: Any, attr: str, contract_key: str | None = None) -> bool:
    key = contract_key or attr
    contract_value = _contract_value(signal, key, None)
    if contract_value is not None:
        return bool(contract_value)
    if hasattr(signal, attr):
        value = getattr(signal, attr)
        if value is not None:
            return bool(value)
    return False


def _signal_text(signal: Any, attr: str, contract_key: str | None = None) -> str:
    value = _contract_value(signal, contract_key or attr, "")
    if value:
        return str(value)
    value = getattr(signal, attr, None)
    return str(value or "")


def _has_invalidation(signal: Any) -> bool:
    contract_present = _contract_value(signal, "invalidation_present", None)
    if contract_present is not None:
        return bool(contract_present)
    contract_condition = _contract_value(signal, "invalidation_condition", "")
    if contract_condition:
        text = str(contract_condition or "").strip().lower()
        if text and not (text.endswith("_trigger") or text.endswith("_anchor")):
            return True
    invalidation_level = getattr(signal, "invalidation_level", None)
    if invalidation_level is not None:
        return True
    text = str(getattr(signal, "invalidation_condition", "") or "").strip().lower()
    if not text:
        return False
    generic_terms = {
        "wait_for_trigger",
        "technical_price_trigger",
        "fundamental_anchor",
        "news_event_trigger",
        "direction_anchor",
        "initial_or_rebalance",
        "unknown",
        "none",
        "n/a",
    }
    if text in generic_terms or text.endswith("_trigger") or text.endswith("_anchor"):
        return False
    return bool(
        any(
            token in text
            for token in (
                "invalid",
                "fails",
                "failure",
                "below",
                "above",
                "close",
                "stop",
                "exit",
                "reduce",
                "contradict",
                "conflict",
                "reversal",
                "reverses",
                "loses",
                "price fails",
                "volume fails",
                "regime flips",
                "basis",
                "inventory",
                "catalyst expires",
                "失效",
                "止损",
                "跌破",
                "突破失败",
                "反向",
            )
        )
    )


def _has_entry_setup(signal: Any) -> bool:
    return has_concrete_entry_trigger(_signal_text(signal, "entry_trigger"))


def _setup_quality_ok(signal: Any) -> bool:
    value = _contract_value(signal, "setup_quality_ok", None)
    if value is not None:
        return bool(value)
    return _setup_quality(signal) >= 0.42


def _source_analysts(signal: Any) -> list[str]:
    agent = normalize_analyst_name(getattr(signal, "agent_name", ""))
    return [agent] if agent else []


def _setup_quality(signal: Any) -> float:
    try:
        return max(0.0, min(1.0, float(getattr(signal, "setup_quality_score", 0.0) or 0.0)))
    except Exception:
        return 0.0


def _trigger_quality(signal: Any) -> float:
    try:
        return max(0.0, min(1.0, float(getattr(signal, "trigger_quality_score", 0.0) or 0.0)))
    except Exception:
        return 0.0


def _setup_quality_notes(signal: Any) -> list[str]:
    value = getattr(signal, "setup_quality_notes", [])
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    try:
        return [str(item) for item in value if str(item).strip()]
    except Exception:
        return [str(value)]


def _market_regime_from_signals(analyst_signals: Iterable[Any]) -> str:
    for signal in analyst_signals or []:
        regime = str(getattr(signal, "market_regime", "") or getattr(signal, "trend_stage", "") or "").strip()
        if regime:
            return regime
    return "unknown"


def _agent_name(signal: Any) -> str:
    return normalize_analyst_name(getattr(signal, "agent_name", ""))


def _technical_opposes_side(signals: Iterable[Any], side: str, min_confidence: float) -> bool:
    opposite = "short" if side == "long" else "long" if side == "short" else ""
    if not opposite:
        return False
    for signal in signals or []:
        if _agent_name(signal) != "technical":
            continue
        if _signal_side(signal) != opposite:
            continue
        if _safe_float(getattr(signal, "confidence", 0.0), 0.0) >= min_confidence:
            return True
    return False


def _score_weight(config: Mapping[str, Any], key: str, default: float) -> float:
    weights = config.get("score_component_weights") if isinstance(config.get("score_component_weights"), Mapping) else {}
    return _safe_float(weights.get(key), default)


def _score_cap(config: Mapping[str, Any], key: str, default: float) -> float:
    caps = config.get("score_component_caps") if isinstance(config.get("score_component_caps"), Mapping) else {}
    return _safe_float(caps.get(key), default)


def _clean_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _row_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _row_value(row: Mapping[str, Any], payload: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except Exception:
        return None


def _bounded(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _mapping_weight(
    config: Mapping[str, Any],
    mapping_key: str,
    value: Any,
    default_weights: Mapping[str, float],
    default: float,
) -> float:
    text = _clean_key(value)
    weights = config.get(mapping_key) if isinstance(config.get(mapping_key), Mapping) else {}
    if text in weights:
        return _safe_float(weights.get(text), default)
    return _safe_float(default_weights.get(text), default)


def _learning_recency_weight(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    decision_date: Any,
    config: Mapping[str, Any],
) -> float:
    decision = _parse_date(decision_date)
    source_date = _parse_date(
        _row_value(
            row,
            payload,
            "last_sample_date",
            "sample_last_trading_date",
            "source_trading_date",
            "trading_date",
            "updated_at",
            "created_at",
        )
    )
    if not decision or not source_date:
        return _bounded(_safe_float(config.get("learning_unknown_recency_weight"), 0.65), 0.0, 1.0)
    days = (decision - source_date).days
    if days < 0:
        return 0.0
    half_life = max(1.0, _safe_float(config.get("learning_recency_half_life_days"), 12.0))
    floor = _bounded(_safe_float(config.get("learning_recency_floor"), 0.20), 0.0, 1.0)
    return max(floor, 0.5 ** (days / half_life))


_POSITIVE_ACTION_PREFERENCES = (
    POSITIVE_OPEN_ACTION_PREFERENCES
    | POSITIVE_HOLD_ACTION_PREFERENCES
    | POSITIVE_EXIT_ACTION_PREFERENCES
    | POSITIVE_EXECUTION_ACTION_PREFERENCES
)


def _action_value_learning_summary(
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    side: str,
    config: Mapping[str, Any],
    decision_date: Any = None,
    decision_lifecycle: str = "open_add_new_risk",
) -> dict[str, Any]:
    default_scope_weights = {
        "exact_real_state": 1.0,
        "partial_real_state": 0.65,
        "similar_sql_prior": 0.35,
        "observation_only": 0.20,
        "counterfactual_prior": 0.20,
        "unknown": 0.25,
    }
    default_source_weights = {
        "trade_episode": 1.15,
        "episode_trade": 1.15,
        "real_trade": 1.0,
        "trade_pair": 0.85,
        "counterfactual_prior": 0.35,
        "observation_only": 0.25,
        "unknown": 0.45,
    }
    execution_reward_unit = max(1.0, _safe_float(config.get("learning_reward_unit"), 4000.0))
    learning_return_unit = max(
        0.001,
        _safe_float(config.get("learning_return_on_notional_unit"), 0.02),
    )
    full_sample_count = max(1, _safe_int(config.get("learning_full_weight_sample_count"), 3))
    execution_tail_loss_threshold = _safe_float(
        config.get("tail_loss_reward_threshold"),
        -1000.0,
    )
    positive_learning_signal = 0.0
    negative_learning_signal = 0.0
    execution_signal = 0.0
    recent_tail_loss_signal = 0.0
    entry_quality_loss_signal = 0.0
    trigger_quality_positive_signal = 0.0
    trigger_quality_loss_signal = 0.0
    positive_count = 0
    negative_count = 0
    exact_real_count = 0
    episode_count = 0
    strongest_positive: dict[str, Any] = {}
    strongest_negative: dict[str, Any] = {}
    latest_complete_episode_key: tuple[str, str] | None = None
    latest_complete_episode_return_on_notional: float | None = None
    latest_complete_episode_date: str | None = None
    latest_complete_episode_quality_weight = 0.0
    used_lanes: set[str] = set()
    ignored_lanes: set[str] = set()
    lifecycle = _clean_key(decision_lifecycle) or "open_add_new_risk"
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        payload = _row_payload(row)
        canonical_action_value = (
            row.get("canonical_action_value")
            if "canonical_action_value" in row
            else payload.get("canonical_action_value")
        )
        if canonical_action_value is not True:
            ignored_lanes.add("noncanonical_action_value")
            continue
        retrieval_match_level = _clean_key(
            row.get("retrieval_match_level") or payload.get("retrieval_match_level")
        )
        evidence_scope = _clean_key(row.get("evidence_scope") or payload.get("evidence_scope"))
        if retrieval_match_level in {"similar", "weak_prior"} or evidence_scope in {
            "similar_sql_prior",
            "counterfactual_prior",
        }:
            ignored_lanes.add(retrieval_match_level or evidence_scope)
            continue
        semantic_validation = validate_action_preference_family_consistency({**dict(row), "payload": payload})
        if not semantic_validation.get("ok"):
            ignored_lanes.add("semantic_contract_error")
            continue
        product_key = payload.get("product_learning_performance_key")
        if not isinstance(product_key, Mapping):
            product_key = {}
        entry_quality_outcome = product_key.get("entry_quality_outcome")
        if not isinstance(entry_quality_outcome, Mapping):
            entry_quality_outcome = payload.get("entry_quality_outcome")
        if not isinstance(entry_quality_outcome, Mapping):
            entry_quality_outcome = {}
        consumer_scope = _clean_key(
            _row_value(row, payload, "consumer_scope", "learning_consumer_scope", default="")
        )
        if consumer_scope != "pm_learning":
            ignored_lanes.add("non_pm_learning_scope")
            continue
        row_side = _clean_key(_row_value(row, payload, "side", default="*"))
        if row_side not in {side, "*", "both", "any"}:
            continue
        action_preference = _clean_key(
            _row_value(row, payload, "action_preference", "policy_action", default="")
        )
        if not action_preference:
            continue
        family = _clean_key(
            row.get("canonical_action_family")
            or payload.get("canonical_action_family")
            or row.get("source_canonical_action_family")
            or payload.get("source_canonical_action_family")
        )
        action_value_lane = _clean_key(row.get("action_value_lane") or payload.get("action_value_lane"))
        learning_lane = _clean_key(row.get("learning_lane") or payload.get("learning_lane"))
        if not action_value_lane or not learning_lane or action_value_lane != learning_lane:
            ignored_lanes.add("action_value_lane_contract_error")
            continue
        lane = learning_lane
        lane_is_execution_profile = lifecycle == "open_add_new_risk" and family == ACTION_FAMILY_EXECUTION
        if lifecycle == "open_add_new_risk" and family != ACTION_FAMILY_OPEN_ADD_NEW_RISK and not lane_is_execution_profile:
            ignored_lanes.add(lane or family or "unknown")
            continue
        scope = _clean_key(
            _row_value(row, payload, "amplification_scope_quality", "source_quality", "evidence_scope", default="unknown")
        )
        if (
            retrieval_match_level in {
                "same_ticker_side_horizon_setup",
                "same_ticker_side_horizon",
                "same_ticker_side",
            }
            and scope == "exact_real_state"
        ):
            scope = "partial_real_state"
        reward_source = _clean_key(
            _row_value(row, payload, "reward_source", "sample_source", default="unknown")
        )
        scope_weight = _mapping_weight(
            config,
            "learning_scope_weights",
            scope,
            default_scope_weights,
            default_scope_weights["unknown"],
        )
        source_weight = _mapping_weight(
            config,
            "learning_reward_source_weights",
            reward_source,
            default_source_weights,
            default_source_weights["unknown"],
        )
        recency_weight = _learning_recency_weight(
            row,
            payload,
            decision_date=decision_date,
            config=config,
        )
        sample_count = max(
            1,
            _safe_int(
                _row_value(
                    row,
                    payload,
                    "sample_count",
                    "real_trade_reward_count",
                    "episode_trade_reward_count",
                    "exact_state_real_trade_sample_count",
                    default=1,
                ),
                1,
            ),
        )
        sample_weight = _bounded(sample_count / full_sample_count, 0.35, 1.0)
        confidence_weight = _bounded(
            _safe_float(_row_value(row, payload, "confidence_score", "confidence", default=0.5), 0.5),
            0.25,
            1.0,
        )
        reward_mean = _safe_float(_row_value(row, payload, "reward_mean", "avg_reward", default=0.0), 0.0)
        reward_sum = _safe_float(_row_value(row, payload, "reward_sum", "total_reward", default=0.0), 0.0)
        worst_reward = _safe_float(_row_value(row, payload, "worst_reward", "min_reward", default=reward_mean), reward_mean)
        quality_weight = (
            scope_weight
            * source_weight
            * recency_weight
            * sample_weight
            * confidence_weight
        )
        tail_loss_count = _safe_int(_row_value(row, payload, "tail_loss_count", default=0), 0)
        if lane_is_execution_profile:
            ignored_lanes.add("execution")
            reward_magnitude = max(
                abs(reward_mean),
                abs(reward_sum) / max(1, sample_count),
                abs(worst_reward),
            )
            execution_magnitude_weight = _bounded(
                reward_magnitude / execution_reward_unit,
                0.25,
                1.0,
            )
            execution_strength = quality_weight * execution_magnitude_weight
            is_negative = action_preference in PROTECTIVE_ACTION_PREFERENCES
            is_tail_loss = (
                action_preference == "tail_loss_protect"
                or tail_loss_count > 0
                or worst_reward <= execution_tail_loss_threshold
                or reward_mean <= execution_tail_loss_threshold
            )
            if action_preference == "positive_candidate_execution":
                execution_signal += execution_strength
            elif is_negative:
                execution_signal -= execution_strength * (1.20 if is_tail_loss else 1.0)
            continue
        mean_return_value = _row_value(
            row,
            payload,
            "mean_return_on_notional",
            default=None,
        )
        if (
            reward_source not in {"trade_episode", "episode_trade"}
            or mean_return_value is None
        ):
            ignored_lanes.add("missing_episode_return_on_notional")
            continue
        mean_return_on_notional = _safe_float(mean_return_value, 0.0)
        worst_return_on_notional = _safe_float(
            _row_value(
                row,
                payload,
                "worst_return_on_notional",
                default=mean_return_on_notional,
            ),
            mean_return_on_notional,
        )
        # Episode returns are percentages. Normalize once against the existing
        # two-percent learning unit so a sub-percent loss is not numerically
        # invisible to the existing Rank weights.
        positive_return = _bounded(max(mean_return_on_notional, 0.0) / learning_return_unit)
        negative_return = _bounded(max(-mean_return_on_notional, 0.0) / learning_return_unit)
        tail_return = _bounded(max(-worst_return_on_notional, 0.0) / learning_return_unit)
        positive_strength = quality_weight * positive_return
        negative_strength = quality_weight * negative_return
        tail_strength = quality_weight * tail_return
        is_positive = positive_return > 0.0
        is_negative = negative_return > 0.0
        is_tail_loss = tail_return > 0.0
        used_lanes.add(lane or "unknown")
        if scope == "exact_real_state":
            exact_real_count += 1
        if reward_source in {"trade_episode", "episode_trade"}:
            episode_count += 1
        latest_episode_return_value = _row_value(
            row,
            payload,
            "latest_complete_episode_return_on_notional",
            default=None,
        )
        latest_episode_date_value = str(
            _row_value(
                row,
                payload,
                "latest_complete_episode_date",
                default="",
            )
            or ""
        )[:10]
        if (
            scope == "exact_real_state"
            and reward_source in {"trade_episode", "episode_trade"}
            and latest_episode_return_value is not None
            and latest_episode_date_value
        ):
            latest_key = (
                latest_episode_date_value,
                str(_row_value(row, payload, "last_sample_date", default="") or "")[:10],
            )
            if latest_complete_episode_key is None or latest_key > latest_complete_episode_key:
                latest_complete_episode_key = latest_key
                latest_complete_episode_date = latest_episode_date_value
                latest_complete_episode_return_on_notional = _safe_float(
                    latest_episode_return_value,
                    0.0,
                )
                latest_complete_episode_quality_weight = quality_weight
        summary_ref = {
            "action_preference": action_preference,
            "lane": lane or "unknown",
            "scope": scope or "unknown",
            "reward_source": reward_source or "unknown",
            "sample_count": sample_count,
            "reward_mean": round(reward_mean, 4),
            "mean_return_on_notional": round(mean_return_on_notional, 8),
            "normalized_return_strength": round(
                positive_return if is_positive else negative_return,
                8,
            ),
            "worst_return_on_notional": round(worst_return_on_notional, 8),
            "weight": round(
                positive_strength if is_positive else negative_strength,
                8,
            ),
        }
        if is_positive:
            positive_count += 1
            positive_learning_signal += positive_strength
            if not strongest_positive or positive_strength > _safe_float(strongest_positive.get("weight"), 0.0):
                strongest_positive = summary_ref
        if is_tail_loss:
            recent_tail_loss_signal += tail_strength
        if is_negative:
            negative_count += 1
            negative_learning_signal += negative_strength
            if not strongest_negative or negative_strength > _safe_float(strongest_negative.get("weight"), 0.0):
                strongest_negative = {
                    **summary_ref,
                    "weight": round(negative_strength, 4),
                    "tail_loss": is_tail_loss,
                }
        if entry_quality_outcome:
            entry_penalty_weight = _safe_float(
                entry_quality_outcome.get("penalty_weight"),
                0.0,
            )
            trigger_support_weight = _safe_float(
                entry_quality_outcome.get("support_weight"),
                0.0,
            )
            if bool(entry_quality_outcome.get("positive_entry_episode")):
                trigger_quality_positive_signal += positive_strength * max(
                    0.25,
                    trigger_support_weight,
                )
            if bool(entry_quality_outcome.get("loss_episode")):
                entry_quality_loss_signal += negative_strength * max(
                    0.35,
                    entry_penalty_weight,
                )
            if bool(entry_quality_outcome.get("tail_loss_episode")):
                trigger_quality_loss_signal += tail_strength * max(
                    0.55,
                    entry_penalty_weight,
                )
                recent_tail_loss_signal += tail_strength * 0.35
    latest_complete_episode_loss = bool(
        latest_complete_episode_return_on_notional is not None
        and latest_complete_episode_return_on_notional < 0.0
    )
    positive_amplification_suspended = latest_complete_episode_loss
    if positive_amplification_suspended:
        latest_loss_strength = (
            latest_complete_episode_quality_weight
            * _bounded(
                abs(float(latest_complete_episode_return_on_notional or 0.0))
                / learning_return_unit
            )
        )
        positive_learning_signal = 0.0
        trigger_quality_positive_signal = 0.0
        negative_learning_signal = max(
            negative_learning_signal,
            latest_loss_strength,
        )
    positive_learning_signal = _bounded(positive_learning_signal)
    negative_learning_signal = _bounded(negative_learning_signal)
    execution_signal = _bounded(execution_signal, -1.0, 1.0)
    recent_tail_loss_signal = _bounded(recent_tail_loss_signal)
    entry_quality_loss_signal = _bounded(entry_quality_loss_signal)
    trigger_quality_positive_signal = _bounded(trigger_quality_positive_signal)
    trigger_quality_loss_signal = _bounded(trigger_quality_loss_signal)
    net_trigger_quality_loss_signal = _bounded(
        max(0.0, trigger_quality_loss_signal - trigger_quality_positive_signal * 0.50)
    )
    return {
        "positive_learning_signal": positive_learning_signal,
        "negative_learning_signal": negative_learning_signal,
        "execution_profile_signal": execution_signal,
        "recent_tail_loss_signal": recent_tail_loss_signal,
        "entry_quality_loss_signal": entry_quality_loss_signal,
        "trigger_quality_positive_signal": trigger_quality_positive_signal,
        "trigger_quality_loss_signal": trigger_quality_loss_signal,
        "net_trigger_quality_loss_signal": net_trigger_quality_loss_signal,
        "positive_count": positive_count,
        "negative_count": negative_count,
        "exact_real_count": exact_real_count,
        "episode_count": episode_count,
        "strongest_positive": strongest_positive,
        "strongest_negative": strongest_negative,
        "latest_complete_episode_date": latest_complete_episode_date,
        "latest_complete_episode_return_on_notional": (
            latest_complete_episode_return_on_notional
        ),
        "latest_complete_episode_loss": latest_complete_episode_loss,
        "positive_amplification_suspended": positive_amplification_suspended,
        "decision_lifecycle": lifecycle,
        "used_lanes": sorted(used_lanes),
        "ignored_lanes": sorted(ignored_lanes),
        "execution_profile_signal_direct_to_rank": False,
    }


def _capital_allocation_reason(*, row: Mapping[str, Any], deployable_threshold: float, tradeable_threshold: float) -> str:
    state = str(row.get("final_state") or "unknown")
    score = _safe_float(row.get("opportunity_score", row.get("score")), 0.0)
    failures = {str(item) for item in (row.get("gating_failures") or [])}
    if state == "tradeable_candidate" and score >= deployable_threshold:
        return "ranked_deployable_candidate_with_complete_current_evidence"
    if state == "probe_candidate":
        return "ranked_probe_candidate_selected_only_if_pm_capital_queue_allows"
    if state == "watch_for_trigger" and bool(row.get("conditional_monitor_candidate")):
        return "monitorable_conditional_candidate_selected_only_if_pm_capital_queue_allows"
    if "missing_invalidation_boundary" in failures:
        return "not_allocated_missing_invalidation_boundary"
    if "critical_data_gap" in failures:
        return "not_allocated_critical_data_gap"
    if "same_scope_alpha_setup_capped_or_rejected" in failures:
        return "rank_lowered_by_same_scope_learning"
    if score < tradeable_threshold:
        return "rank_lowered_by_insufficient_opportunity_score"
    return "ranked_candidate_requires_pm_final_contract_authority"


def _candidate_layer_hint(final_state: str) -> str:
    state = str(final_state or "").strip().lower()
    if state == "tradeable_candidate":
        return "tradeable_candidate"
    if state == "probe_candidate":
        return "exploration_probe_candidate"
    if state == "watch_for_trigger":
        return "watch_for_trigger_candidate"
    return "not_candidate"


def _candidate_quality_components(
    *,
    opportunity_score: float,
    trigger_valid: bool,
    invalidation_count: int,
) -> dict[str, float]:
    trigger_quality = 0.04 if trigger_valid else 0.0
    invalidation_quality = 0.04 if invalidation_count > 0 else 0.0
    return {
        "opportunity_score": round(_safe_float(opportunity_score), 6),
        "trigger_quality": round(trigger_quality, 6),
        "invalidation_quality": round(invalidation_quality, 6),
    }


def _candidate_quality_score(components: Mapping[str, Any]) -> float:
    return round(_bounded(sum(_safe_float(value) for value in (components or {}).values())), 6)


def _learning_adjustment_summary(
    *,
    policy_counts: Mapping[str, int],
    action_value_learning: Mapping[str, Any],
    alpha_profile_bonus: float,
    alpha_profile_penalty: float,
    best_alpha_profile: Mapping[str, Any],
    capped_profiles: list[Mapping[str, Any]],
) -> dict[str, Any]:
    net_adjustment = alpha_profile_bonus - alpha_profile_penalty
    return {
        "positive_policy_count": int(policy_counts.get("positive", 0) or 0),
        "negative_policy_count": int(policy_counts.get("negative", 0) or 0),
        "positive_action_value_count": int(action_value_learning.get("positive_count", 0) or 0),
        "negative_action_value_count": int(action_value_learning.get("negative_count", 0) or 0),
        "exact_real_action_value_count": int(action_value_learning.get("exact_real_count", 0) or 0),
        "episode_action_value_count": int(action_value_learning.get("episode_count", 0) or 0),
        "positive_learning_signal": round(_safe_float(action_value_learning.get("positive_learning_signal"), 0.0), 4),
        "negative_learning_signal": round(_safe_float(action_value_learning.get("negative_learning_signal"), 0.0), 4),
        "execution_profile_learning_signal": round(
            _safe_float(action_value_learning.get("execution_profile_signal"), 0.0),
            4,
        ),
        "recent_tail_loss_signal": round(
            _safe_float(action_value_learning.get("recent_tail_loss_signal"), 0.0),
            4,
        ),
        "entry_quality_loss_signal": round(
            _safe_float(action_value_learning.get("entry_quality_loss_signal"), 0.0),
            4,
        ),
        "trigger_quality_positive_signal": round(
            _safe_float(action_value_learning.get("trigger_quality_positive_signal"), 0.0),
            4,
        ),
        "trigger_quality_loss_signal": round(
            _safe_float(action_value_learning.get("trigger_quality_loss_signal"), 0.0),
            4,
        ),
        "net_trigger_quality_loss_signal": round(
            _safe_float(action_value_learning.get("net_trigger_quality_loss_signal"), 0.0),
            4,
        ),
        "latest_complete_episode_date": action_value_learning.get(
            "latest_complete_episode_date"
        ),
        "latest_complete_episode_return_on_notional": action_value_learning.get(
            "latest_complete_episode_return_on_notional"
        ),
        "latest_complete_episode_loss": bool(
            action_value_learning.get("latest_complete_episode_loss")
        ),
        "positive_amplification_suspended": bool(
            action_value_learning.get("positive_amplification_suspended")
        ),
        "strongest_positive_action_value": action_value_learning.get("strongest_positive") or {},
        "strongest_negative_action_value": action_value_learning.get("strongest_negative") or {},
        "alpha_setup_score_adjustment": round(net_adjustment, 4),
        "best_profile_state": best_alpha_profile.get("lifecycle_state") if best_alpha_profile else None,
        "best_profile_scope_key": best_alpha_profile.get("scope_key") if best_alpha_profile else None,
        "capped_or_rejected_profile_count": len(capped_profiles),
        "effect": (
            "boosted"
            if net_adjustment > 0 or _safe_float(action_value_learning.get("positive_learning_signal"), 0.0) > _safe_float(action_value_learning.get("negative_learning_signal"), 0.0)
            else "penalized"
            if net_adjustment < 0 or _safe_float(action_value_learning.get("negative_learning_signal"), 0.0) > 0
            else "neutral"
        ),
        "not_trade_authority": True,
    }


def build_opportunity_scorecard(
    *,
    ticker: str,
    analyst_signals: Iterable[Any],
    market_confirmation: Mapping[str, Any] | None = None,
    data_quality_summary: Mapping[str, Any] | None = None,
    adaptive_policy_state: Iterable[Mapping[str, Any]] | None = None,
    alpha_setup_profiles: Iterable[Mapping[str, Any]] | None = None,
    alpha_setup_action_values: Iterable[Mapping[str, Any]] | None = None,
    signal_collection_contract: Mapping[str, Any] | None = None,
    formal_setup_by_side: Mapping[str, Any] | None = None,
    formal_expected_horizon_days_by_side: Mapping[str, Any] | None = None,
    analyst_performance: Iterable[Mapping[str, Any]] | None = None,
    decision_date: Any = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic opportunity scorecard for PM and Researcher.

    It is not a trading rule or product preference. It is an auditable summary of
    current-day evidence, learning support, execution/data penalties, and the
    resulting opportunity state per side.
    """
    signals = list(analyst_signals or [])
    cfg = config or {}
    deployable_threshold = _safe_float(cfg.get("deployable_threshold"), 0.72)
    tradeable_threshold = _safe_float(cfg.get("tradeable_threshold"), 0.58)
    weak_confirmation_threshold = _safe_float(cfg.get("weak_confirmation_threshold"), 0.45)
    min_deployable_setup_quality = _safe_float(cfg.get("min_deployable_setup_quality"), 0.72)
    min_tradeable_candidate_setup_quality = _safe_float(cfg.get("min_tradeable_candidate_setup_quality"), 0.55)
    single_tradeable_candidate_setup_confirmation_score = _safe_float(
        cfg.get("single_tradeable_candidate_setup_confirmation_score"),
        0.68,
    )
    single_tradeable_candidate_setup_min_business_quality = _safe_float(
        cfg.get("single_tradeable_candidate_setup_min_business_quality"),
        0.60,
    )
    single_tradeable_candidate_setup_min_confidence = _safe_float(
        cfg.get("single_tradeable_candidate_setup_min_confidence"),
        0.42,
    )
    technical_opposition_min_confidence = _safe_float(
        cfg.get("technical_opposition_min_confidence"),
        0.45,
    )
    block_single_setup_on_technical_opposition = bool(
        cfg.get("block_single_setup_on_technical_opposition", True)
    )
    confirmation = market_confirmation or {}
    data_quality = data_quality_summary or {}
    pm_fusion_diagnostics = build_pm_fusion_diagnostics(signal_collection_contract)
    fusion_adjustment = _safe_float(pm_fusion_diagnostics.get("fusion_score_adjustment"), 0.0)
    fusion_consensus = _safe_float(pm_fusion_diagnostics.get("multi_evidence_consensus_score"), 0.0)
    confirmation_score = _safe_float(confirmation.get("confirmation_score"), 0.0)
    confirmation_features = _as_list(confirmation.get("features"))
    confirmation_conflicts = _as_list(confirmation.get("conflicts"))
    data_missing = _as_list(confirmation.get("data_missing")) + _as_list(confirmation.get("data_unavailable"))
    critical_gap = str(data_quality.get("status") or "").strip().lower() == "hard_fail"
    fundamental_setup_gap = False

    policy_counts: dict[str, int] = {}
    for row in adaptive_policy_state or []:
        if not isinstance(row, Mapping):
            continue
        action = str(row.get("policy_action") or "").lower()
        if action in {"protect", "allow", "probe"}:
            policy_counts["positive"] = policy_counts.get("positive", 0) + 1
        elif action in {"cap", "demote", "block"}:
            policy_counts["negative"] = policy_counts.get("negative", 0) + 1

    setup_by_side = {
        side: str((formal_setup_by_side or {}).get(side) or "").strip().lower()
        for side in ("long", "short")
    }
    enforce_formal_setup = formal_setup_by_side is not None
    alpha_profiles_by_side: dict[str, list[Mapping[str, Any]]] = {"long": [], "short": []}
    for profile in alpha_setup_profiles or []:
        if not isinstance(profile, Mapping):
            continue
        profile_side = str(profile.get("side") or "*").lower()
        profile_setup = str(profile.get("setup_type") or "").strip().lower()
        target_sides = (
            (profile_side,)
            if profile_side in {"long", "short"}
            else ("long", "short")
            if profile_side == "*"
            else ()
        )
        for target_side in target_sides:
            required_setup = setup_by_side[target_side]
            if enforce_formal_setup and (
                not required_setup or profile_setup != required_setup
            ):
                continue
            alpha_profiles_by_side[target_side].append(profile)

    side_rows: dict[str, dict[str, Any]] = {}
    for side in ("long", "short"):
        forecast_calibration = build_forecast_calibration_summary(
            analyst_signals=signals,
            analyst_performance=analyst_performance,
            target_side=side,
            expected_horizon_days=(
                formal_expected_horizon_days_by_side.get(side)
                if isinstance(formal_expected_horizon_days_by_side, Mapping)
                else None
            ),
        )
        directional_signals = [signal for signal in signals if _signal_side(signal) == side]
        opportunity_state_counts: dict[str, int] = {}
        for signal in directional_signals:
            state = _opportunity_state(signal)
            opportunity_state_counts[state] = opportunity_state_counts.get(state, 0) + 1
        supporting = [
            signal
            for signal in directional_signals
            if _opportunity_state(signal)
            in {"watch_for_trigger", "probe_candidate", "tradeable_candidate"}
        ]
        invalidation_count = 0
        setup_count = 0
        quality_scores: list[float] = []
        setup_quality_scores: list[float] = []
        trigger_quality_scores: list[float] = []
        setup_quality_notes: list[str] = []
        confidence_scores: list[float] = []
        analyst_names: list[str] = []
        trigger_valid_count = 0
        current_trigger_confirmed_count = 0
        setup_quality_ok_count = 0
        source_analysts: list[str] = []
        entry_triggers: list[str] = []
        opportunity_states: list[str] = []
        for signal in supporting:
            state = _opportunity_state(signal)
            opportunity_states.append(state)
            if _has_invalidation(signal):
                invalidation_count += 1
            if _has_entry_setup(signal):
                setup_count += 1
            if _signal_bool(signal, "trigger_valid"):
                trigger_valid_count += 1
            if _signal_bool(signal, "current_trigger_confirmed"):
                current_trigger_confirmed_count += 1
            if _setup_quality_ok(signal):
                setup_quality_ok_count += 1
            trigger_text = _signal_text(signal, "entry_trigger")
            if trigger_text:
                entry_triggers.append(trigger_text)
            quality_scores.append(_safe_float(getattr(signal, "business_quality_score", 0.0), 0.0))
            setup_quality_scores.append(_setup_quality(signal))
            analyst_name = normalize_analyst_name(getattr(signal, "agent_name", ""))
            evidence_role = _signal_text(signal, "evidence_role")
            if (
                _has_entry_setup(signal)
                and (
                    (analyst_name == "technical" and evidence_role == "entry_timing")
                    or (analyst_name == "commodity_news" and evidence_role == "event_catalyst")
                )
            ):
                trigger_quality_scores.append(_trigger_quality(signal))
            setup_quality_notes.extend(_setup_quality_notes(signal))
            confidence_scores.append(_safe_float(getattr(signal, "confidence", 0.0), 0.0))
            analyst_names.append(analyst_name)
            source_analysts.extend(_source_analysts(signal))

        support_count = len(supporting)
        tradeable_states = (
            opportunity_state_counts.get("tradeable_candidate", 0)
            + opportunity_state_counts.get("probe_candidate", 0)
        )
        avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0.0
        max_quality = max(quality_scores, default=0.0)
        avg_setup_quality = sum(setup_quality_scores) / len(setup_quality_scores) if setup_quality_scores else 0.0
        max_setup_quality = max(setup_quality_scores, default=0.0)
        max_trigger_quality = max(trigger_quality_scores, default=0.0)
        avg_confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0.0
        action_value_learning = _action_value_learning_summary(
            alpha_setup_action_values,
            side=side,
            config=cfg,
            decision_date=decision_date,
        )
        policy_positive_signal = min(1.0, float(policy_counts.get("positive", 0) or 0)) * 0.55
        policy_negative_signal = min(1.0, float(policy_counts.get("negative", 0) or 0)) * 0.75
        positive_learning_signal = max(
            _safe_float(action_value_learning.get("positive_learning_signal"), 0.0),
            policy_positive_signal,
        )
        negative_learning_signal = max(
            _safe_float(action_value_learning.get("negative_learning_signal"), 0.0),
            policy_negative_signal,
        )
        execution_profile_signal = _safe_float(action_value_learning.get("execution_profile_signal"), 0.0)
        recent_tail_loss_signal = _safe_float(action_value_learning.get("recent_tail_loss_signal"), 0.0)
        entry_quality_loss_signal = _safe_float(action_value_learning.get("entry_quality_loss_signal"), 0.0)
        trigger_quality_positive_signal = _safe_float(action_value_learning.get("trigger_quality_positive_signal"), 0.0)
        trigger_quality_loss_signal = _safe_float(action_value_learning.get("trigger_quality_loss_signal"), 0.0)
        net_trigger_quality_loss_signal = _safe_float(action_value_learning.get("net_trigger_quality_loss_signal"), trigger_quality_loss_signal)
        positive_amplification_suspended = bool(
            action_value_learning.get("positive_amplification_suspended")
        )
        if positive_amplification_suspended:
            positive_learning_signal = 0.0
            trigger_quality_positive_signal = 0.0
        positive_action_value_support_present = bool(
            int(action_value_learning.get("positive_count", 0) or 0) > 0
            and positive_learning_signal > 1e-9
            and not positive_amplification_suspended
        )
        score_components = {
            "directional_support": min(
                _score_cap(cfg, "directional_support", 0.24),
                _score_weight(cfg, "directional_support_per_signal", 0.08) * support_count,
            ),
            "tradeable_state": min(
                _score_cap(cfg, "tradeable_state", 0.16),
                _score_weight(cfg, "tradeable_state_per_signal", 0.08) * tradeable_states,
            ),
            "business_quality": _score_weight(cfg, "business_quality", 0.12) * max_quality,
            "setup_quality": _score_weight(cfg, "setup_quality", 0.12) * max_setup_quality,
            "confidence": _score_weight(cfg, "confidence", 0.08) * avg_confidence,
            "market_confirmation": _score_weight(cfg, "market_confirmation", 0.12) * confirmation_score,
            "positive_learning": _score_weight(cfg, "positive_learning", 0.12) * positive_learning_signal,
            "negative_learning": -abs(_score_weight(cfg, "negative_learning", 0.16)) * negative_learning_signal,
            "execution_profile_learning": _score_weight(cfg, "execution_profile_learning", 0.10) * execution_profile_signal,
            "recent_tail_loss_penalty": -abs(_score_weight(cfg, "recent_tail_loss_penalty", 0.18)) * recent_tail_loss_signal,
            "entry_quality_loss_penalty": -abs(_score_weight(cfg, "entry_quality_loss_penalty", 0.12)) * entry_quality_loss_signal,
            "trigger_quality_positive_bonus": _score_weight(cfg, "trigger_quality_positive_bonus", 0.08) * trigger_quality_positive_signal,
            "trigger_quality_loss_penalty": -abs(_score_weight(cfg, "trigger_quality_loss_penalty", 0.10)) * net_trigger_quality_loss_signal,
            "fusion_consensus": _score_weight(cfg, "fusion_consensus", 0.08) * fusion_consensus,
            "fusion_score_adjustment": fusion_adjustment,
        }
        side_profiles = alpha_profiles_by_side.get(side, [])
        deployable_profiles = [p for p in side_profiles if str(p.get("lifecycle_state") or "").lower() == "deployable"]
        protected_profiles = [p for p in side_profiles if str(p.get("lifecycle_state") or "").lower() == "protected"]
        watchlist_profiles = [p for p in side_profiles if str(p.get("lifecycle_state") or "").lower() == "watchlist"]
        capped_profiles = [p for p in side_profiles if str(p.get("lifecycle_state") or "").lower() in {"capped", "rejected"}]
        best_alpha_profile = max(
            side_profiles,
            key=lambda p: (
                float(p.get("confidence_score") or 0.0),
                int(p.get("sample_count") or 0),
                float(p.get("net_pnl") or 0.0),
            ),
            default={},
        )
        alpha_profile_bonus = 0.0
        if deployable_profiles and positive_action_value_support_present:
            alpha_profile_bonus = 0.09
        elif protected_profiles and positive_action_value_support_present:
            alpha_profile_bonus = 0.06
        alpha_profile_penalty = 0.08 if capped_profiles else 0.0
        if positive_amplification_suspended:
            alpha_profile_bonus = 0.0
        elif recent_tail_loss_signal > 0 and alpha_profile_bonus > 0:
            alpha_profile_bonus *= max(0.0, 1.0 - recent_tail_loss_signal)
        elif negative_learning_signal > 0 and alpha_profile_bonus > 0:
            alpha_profile_bonus *= max(0.25, 1.0 - negative_learning_signal * 0.5)
        score_components["alpha_profile_adjustment"] = alpha_profile_bonus - alpha_profile_penalty
        score_components["market_conflict_penalty"] = -abs(
            _score_weight(cfg, "market_conflict_penalty", 0.10)
        ) * min(1.0, len(confirmation_conflicts) / 3.0)
        score_components["critical_data_gap_penalty"] = -abs(
            _score_weight(cfg, "critical_data_gap_penalty", 0.12)
        ) if critical_gap else 0.0
        score_components["fundamental_gap_penalty"] = -abs(
            _score_weight(cfg, "fundamental_gap_penalty", 0.06)
        ) if fundamental_setup_gap else 0.0
        current_evidence_component_names = {
            "directional_support",
            "tradeable_state",
            "business_quality",
            "setup_quality",
            "confidence",
            "market_confirmation",
            "fusion_consensus",
            "fusion_score_adjustment",
            "market_conflict_penalty",
            "critical_data_gap_penalty",
            "fundamental_gap_penalty",
        }
        validated_learning_component_names = {
            "positive_learning",
            "negative_learning",
            "recent_tail_loss_penalty",
            "entry_quality_loss_penalty",
            "trigger_quality_positive_bonus",
            "trigger_quality_loss_penalty",
            "alpha_profile_adjustment",
        }
        current_evidence_score = max(
            0.0,
            min(
                1.0,
                sum(
                    value
                    for component, value in score_components.items()
                    if component in current_evidence_component_names
                ),
            ),
        )
        validated_learning_delta = sum(
            value
            for component, value in score_components.items()
            if component in validated_learning_component_names
        )
        score = sum(
            value
            for component, value in score_components.items()
            if component != "execution_profile_learning"
        )
        score = max(0.0, min(1.0, score))

        gating_failures: list[str] = []
        if support_count <= 0:
            gating_failures.append("no_directional_support")
        if tradeable_states <= 0:
            gating_failures.append("no_tradeable_opportunity_state")
        if setup_count <= 0:
            gating_failures.append("missing_entry_setup")
        if invalidation_count <= 0:
            gating_failures.append("missing_invalidation_boundary")
        if max_setup_quality < min_tradeable_candidate_setup_quality and support_count > 0:
            gating_failures.append("weak_entry_setup_quality")
        if side == "long" and "late_long_entry_price_near_upper_range" in setup_quality_notes:
            gating_failures.append("late_long_entry_price_location")
        if side == "short" and "late_short_entry_price_near_lower_range" in setup_quality_notes:
            gating_failures.append("late_short_entry_price_location")
        if critical_gap:
            gating_failures.append("critical_data_gap")
        if fundamental_setup_gap:
            gating_failures.append("fundamental_data_not_enough_for_standalone_setup")
        if confirmation_score < weak_confirmation_threshold:
            gating_failures.append("weak_market_confirmation")
        if capped_profiles:
            gating_failures.append("same_scope_alpha_setup_capped_or_rejected")
        if (
            pm_fusion_diagnostics.get("requires_pm_conflict_resolution")
            and pm_fusion_diagnostics.get("dominant_opposing_evidence_count", 0)
        ):
            gating_failures.append("dominant_opposing_evidence_requires_pm_resolution")

        technical_opposes = _technical_opposes_side(signals, side, technical_opposition_min_confidence)
        single_tradeable_blocking_failures = {
            "critical_data_gap",
            "fundamental_data_not_enough_for_standalone_setup",
            "missing_entry_setup",
            "missing_invalidation_boundary",
            "weak_entry_setup_quality",
            "late_long_entry_price_location",
            "late_short_entry_price_location",
            "same_scope_alpha_setup_capped_or_rejected",
            "dominant_opposing_evidence_requires_pm_resolution",
        }
        single_tradeable_candidate_setup_confirmed = bool(
            tradeable_states > 0
            and setup_count > 0
            and invalidation_count > 0
            and max_setup_quality >= min_tradeable_candidate_setup_quality
            and max_quality >= single_tradeable_candidate_setup_min_business_quality
            and avg_confidence >= single_tradeable_candidate_setup_min_confidence
            and not single_tradeable_blocking_failures.intersection(gating_failures)
            and (
                not block_single_setup_on_technical_opposition
                or not technical_opposes
            )
        )
        single_tradeable_candidate_setup_promoted = bool(
            single_tradeable_candidate_setup_confirmed
            and confirmation_score >= single_tradeable_candidate_setup_confirmation_score
            and negative_learning_signal < _safe_float(cfg.get("single_candidate_negative_learning_soft_cap"), 0.45)
            and recent_tail_loss_signal < _safe_float(cfg.get("single_candidate_tail_loss_soft_cap"), 0.35)
            and entry_quality_loss_signal < _safe_float(cfg.get("single_candidate_entry_quality_loss_soft_cap"), 0.35)
            and net_trigger_quality_loss_signal < _safe_float(cfg.get("single_candidate_trigger_quality_loss_soft_cap"), 0.35)
        )
        scorecard_promotion_reasons: list[str] = []
        if single_tradeable_candidate_setup_promoted:
            scorecard_promotion_reasons.append("single_tradeable_candidate_with_strong_market_confirmation")
            score = max(score, tradeable_threshold)
        elif single_tradeable_candidate_setup_confirmed:
            scorecard_promotion_reasons.append("single_tradeable_candidate_rank_lowered_by_recent_learning")

        current_entry_blocking_failures = {
            "no_directional_support",
            "no_tradeable_opportunity_state",
            "missing_entry_setup",
            "missing_invalidation_boundary",
            "critical_data_gap",
            "fundamental_data_not_enough_for_standalone_setup",
            "dominant_opposing_evidence_requires_pm_resolution",
        }
        current_entry_eligible = not bool(
            current_entry_blocking_failures.intersection(gating_failures)
        )
        current_entry_prerequisite_met = bool(
            current_entry_eligible
            and (
                current_evidence_score >= tradeable_threshold
                or single_tradeable_candidate_setup_confirmed
            )
        )
        if (
            current_entry_prerequisite_met
            and score >= deployable_threshold
            and max_setup_quality >= min_deployable_setup_quality
            and not gating_failures
        ):
            final_state = "tradeable_candidate"
        elif (
            current_entry_prerequisite_met
            and (
                (
                    current_evidence_score >= tradeable_threshold
                    and max_setup_quality >= min_tradeable_candidate_setup_quality
                    and tradeable_states > 0
                    and setup_count > 0
                )
                or single_tradeable_candidate_setup_promoted
                or single_tradeable_candidate_setup_confirmed
            )
        ):
            final_state = "probe_candidate"
        elif support_count > 0:
            final_state = "watch_for_trigger"
        else:
            final_state = "no_opportunity"

        opportunity_score = round(score, 4)
        cold_start_evidence_quality = round(
            _bounded(
                sum(
                    _safe_float(score_components.get(component), 0.0)
                    for component in (
                        "directional_support",
                        "tradeable_state",
                        "business_quality",
                        "setup_quality",
                        "confidence",
                        "market_confirmation",
                        "fusion_consensus",
                    )
                )
            ),
            4,
        )
        candidate_quality_components = _candidate_quality_components(
            opportunity_score=opportunity_score,
            trigger_valid=bool(trigger_valid_count > 0),
            invalidation_count=invalidation_count,
        )
        candidate_quality = _candidate_quality_score(candidate_quality_components)
        side_rows[side] = {
            "side": side,
            "score": opportunity_score,
            "opportunity_score": opportunity_score,
            "current_evidence_quality": round(current_evidence_score, 4),
            "validated_learning_delta": round(validated_learning_delta, 4),
            "current_entry_eligible": current_entry_eligible,
            "current_entry_prerequisite_met": current_entry_prerequisite_met,
            "candidate_quality": candidate_quality,
            "candidate_quality_components": candidate_quality_components,
            "direction_evidence_strength": candidate_quality,
            "setup_quality_score": round(max_setup_quality, 4),
            "trigger_quality_score": round(max_trigger_quality, 4),
            "direction_evidence_components": {
                "opportunity_score": opportunity_score,
                "current_evidence_quality": round(current_evidence_score, 4),
                "validated_learning_delta": round(validated_learning_delta, 4),
                "candidate_quality": candidate_quality,
                "supporting_signal_count": support_count,
                "supporting_analysts": sorted(set(name for name in analyst_names if name)),
                "setup_quality": round(max_setup_quality, 4),
                "trigger_valid": bool(trigger_valid_count > 0),
                "invalidation_present": bool(invalidation_count > 0),
                "conflict_count": len([item for item in gating_failures if str(item or "").strip()]),
            },
            "analyst_direction_evidence": {
                "side": side,
                "source": "pm_signal_fusion",
                "boundary": "structured_direction_evidence_not_pm_side_selection",
                "supporting_signal_count": support_count,
                "supporting_analysts": sorted(set(name for name in analyst_names if name)),
                "candidate_quality": candidate_quality,
                "candidate_layer_hint": _candidate_layer_hint(final_state),
                "opportunity_score": opportunity_score,
            },
            "direction_evidence_boundary": "fusion_preserves_signal_collector_evidence_no_pm_side_selection",
            "candidate_layer_hint": _candidate_layer_hint(final_state),
            "rank_score_input_components": {
                "cold_start_evidence_quality": cold_start_evidence_quality,
                "forecast_calibration": forecast_calibration,
            },
            "forecast_calibration_summary": forecast_calibration,
            "opportunity_score_components": {
                key: round(float(value or 0.0), 4)
                for key, value in score_components.items()
            },
            "final_state": final_state,
            "supporting_signal_count": support_count,
            "supporting_analysts": sorted(set(name for name in analyst_names if name)),
            "tradeable_opportunity_state_count": tradeable_states,
            "opportunity_state_counts": opportunity_state_counts,
            "opportunity_state": final_state,
            "setup_quality_ok": bool(setup_quality_ok_count > 0),
            "trigger_valid": bool(trigger_valid_count > 0),
            "current_trigger_confirmed": bool(current_trigger_confirmed_count > 0),
            "invalidation_present": bool(invalidation_count > 0),
            "entry_trigger": entry_triggers[0] if entry_triggers else "",
            "source_analysts": sorted(set(name for name in source_analysts if name)),
            "entry_setup_count": setup_count,
            "invalidation_count": invalidation_count,
            "avg_business_quality": round(avg_quality, 4),
            "max_business_quality": round(max_quality, 4),
            "avg_setup_quality": round(avg_setup_quality, 4),
            "max_setup_quality": round(max_setup_quality, 4),
            "setup_quality_notes": sorted(set(setup_quality_notes))[:12],
            "avg_confidence": round(avg_confidence, 4),
            "market_confirmation_score": round(confirmation_score, 4),
            "market_confirmation_features": [str(item) for item in confirmation_features[:8]],
            "market_confirmation_conflicts": [str(item) for item in confirmation_conflicts[:8]],
            "data_missing_count": len(data_missing),
            "critical_data_gap": critical_gap,
            "single_tradeable_candidate_setup_confirmed": single_tradeable_candidate_setup_confirmed,
            "single_tradeable_candidate_setup_promoted": single_tradeable_candidate_setup_promoted,
            "technical_opposes_side": technical_opposes,
            "scorecard_promotion_reasons": scorecard_promotion_reasons,
            "learning_positive_count": policy_counts.get("positive", 0),
            "learning_negative_count": policy_counts.get("negative", 0),
            "action_value_learning_summary": {
                "positive_learning_signal": round(_safe_float(action_value_learning.get("positive_learning_signal"), 0.0), 4),
                "negative_learning_signal": round(_safe_float(action_value_learning.get("negative_learning_signal"), 0.0), 4),
                "execution_profile_signal": round(_safe_float(action_value_learning.get("execution_profile_signal"), 0.0), 4),
                "recent_tail_loss_signal": round(_safe_float(action_value_learning.get("recent_tail_loss_signal"), 0.0), 4),
                "entry_quality_loss_signal": round(_safe_float(action_value_learning.get("entry_quality_loss_signal"), 0.0), 4),
                "trigger_quality_positive_signal": round(_safe_float(action_value_learning.get("trigger_quality_positive_signal"), 0.0), 4),
                "trigger_quality_loss_signal": round(_safe_float(action_value_learning.get("trigger_quality_loss_signal"), 0.0), 4),
                "net_trigger_quality_loss_signal": round(_safe_float(action_value_learning.get("net_trigger_quality_loss_signal"), 0.0), 4),
                "positive_count": action_value_learning.get("positive_count", 0),
                "negative_count": action_value_learning.get("negative_count", 0),
                "exact_real_count": action_value_learning.get("exact_real_count", 0),
                "episode_count": action_value_learning.get("episode_count", 0),
                "strongest_positive": action_value_learning.get("strongest_positive") or {},
                "strongest_negative": action_value_learning.get("strongest_negative") or {},
                "decision_lifecycle": action_value_learning.get("decision_lifecycle") or "open_add_new_risk",
                "used_lanes": list(action_value_learning.get("used_lanes") or []),
                "ignored_lanes": list(action_value_learning.get("ignored_lanes") or []),
                "execution_profile_signal_direct_to_rank": bool(
                    action_value_learning.get("execution_profile_signal_direct_to_rank")
                ),
                "latest_complete_episode_date": action_value_learning.get(
                    "latest_complete_episode_date"
                ),
                "latest_complete_episode_return_on_notional": action_value_learning.get(
                    "latest_complete_episode_return_on_notional"
                ),
                "latest_complete_episode_loss": bool(
                    action_value_learning.get("latest_complete_episode_loss")
                ),
                "positive_amplification_suspended": positive_amplification_suspended,
            },
            "alpha_setup_profile_counts": {
                "deployable": len(deployable_profiles),
                "protected": len(protected_profiles),
                "watchlist": len(watchlist_profiles),
                "capped_or_rejected": len(capped_profiles),
            },
            "best_alpha_setup_profile": {
                "scope_key": best_alpha_profile.get("scope_key"),
                "setup_type": best_alpha_profile.get("setup_type"),
                "lifecycle_state": best_alpha_profile.get("lifecycle_state"),
                "profile_state_hint": (
                    best_alpha_profile.get("profile_state_hint")
                    or "profile_observe"
                ),
                "sample_count": best_alpha_profile.get("sample_count"),
                "win_rate": best_alpha_profile.get("win_rate"),
                "profit_factor": best_alpha_profile.get("profit_factor"),
                "net_pnl": best_alpha_profile.get("net_pnl"),
                "confidence_score": best_alpha_profile.get("confidence_score"),
                "max_position_impact": best_alpha_profile.get("max_position_impact"),
            } if best_alpha_profile else {},
            "alpha_setup_score_adjustment": round(alpha_profile_bonus - alpha_profile_penalty, 4),
            "learning_adjustment_summary": _learning_adjustment_summary(
                policy_counts=policy_counts,
                action_value_learning=action_value_learning,
                alpha_profile_bonus=alpha_profile_bonus,
                alpha_profile_penalty=alpha_profile_penalty,
                best_alpha_profile=best_alpha_profile,
                capped_profiles=capped_profiles,
            ),
            "gating_failures": gating_failures,
            "pm_fusion_diagnostics": pm_fusion_diagnostics,
            "pm_conflict_resolution": {
                "handled": not bool(pm_fusion_diagnostics.get("requires_pm_conflict_resolution"))
                or final_state in {"watch_for_trigger", "no_opportunity", "probe_candidate"},
                "resolution_effect": (
                    "downgrade_or_monitor"
                    if pm_fusion_diagnostics.get("requires_pm_conflict_resolution")
                    and final_state in {"watch_for_trigger", "probe_candidate"}
                    else "no_material_conflict"
                    if not pm_fusion_diagnostics.get("requires_pm_conflict_resolution")
                    else "tradeable_requires_auditor_review"
                ),
                "confirmation_requirements_addressed": bool(
                    not pm_fusion_diagnostics.get("requires_pm_confirmation_explanation")
                    or confirmation_score >= weak_confirmation_threshold
                    or final_state in {"watch_for_trigger", "no_opportunity"}
                ),
                "no_trade_authority": True,
            },
        }
        side_rows[side]["conditional_monitor_candidate"] = bool(
            final_state == "watch_for_trigger"
            and side_rows[side]["setup_quality_ok"]
            and not side_rows[side]["trigger_valid"]
            and side_rows[side]["invalidation_present"]
            and bool(side_rows[side]["entry_trigger"])
        )
        side_rows[side]["capital_allocation_reason"] = _capital_allocation_reason(
            row=side_rows[side],
            deployable_threshold=deployable_threshold,
            tradeable_threshold=tradeable_threshold,
        )

    preferred_side = "flat"
    if side_rows["long"]["score"] > side_rows["short"]["score"] + 0.04:
        preferred_side = "long"
    elif side_rows["short"]["score"] > side_rows["long"]["score"] + 0.04:
        preferred_side = "short"
    return {
        "version": "opportunity_scorecard_v1",
        "ticker": ticker,
        "preferred_side": preferred_side,
        "direction_evidence_boundary": "fusion_preserves_signal_collector_evidence_no_pm_side_selection",
        "direction_evidence_summary": {
            side: {
                "direction_evidence_strength": side_rows[side].get("direction_evidence_strength"),
                "candidate_quality": side_rows[side].get("candidate_quality"),
                "opportunity_score": side_rows[side].get("opportunity_score"),
                "supporting_signal_count": side_rows[side].get("supporting_signal_count"),
                "candidate_layer_hint": side_rows[side].get("candidate_layer_hint"),
            }
            for side in ("long", "short")
        },
        "market_regime": _market_regime_from_signals(signals),
        "long": side_rows["long"],
        "short": side_rows["short"],
        "policy_counts": policy_counts,
        "thresholds": {
            "deployable_threshold": deployable_threshold,
            "tradeable_threshold": tradeable_threshold,
            "weak_confirmation_threshold": weak_confirmation_threshold,
            "min_deployable_setup_quality": min_deployable_setup_quality,
            "min_tradeable_candidate_setup_quality": min_tradeable_candidate_setup_quality,
            "single_tradeable_candidate_setup_confirmation_score": single_tradeable_candidate_setup_confirmation_score,
            "single_tradeable_candidate_setup_min_business_quality": single_tradeable_candidate_setup_min_business_quality,
            "single_tradeable_candidate_setup_min_confidence": single_tradeable_candidate_setup_min_confidence,
            "technical_opposition_min_confidence": technical_opposition_min_confidence,
        },
        "alpha_setup_profiles_enabled": True,
        "not_product_preference": True,
        "no_future_data": True,
        "hard_margin_cap_not_overridden": True,
    }
