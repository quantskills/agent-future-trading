"""Evidence calibration from past research records for analyst signals.

This module turns bounded learning context into analyst-side evidence quality
adjustments. It must not create trade authority, sizing authority, or PM bypasses.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.agent_tools.research.adaptive_policy_safety import filter_adaptive_policy_state_for_pm


_DIRECTION_BY_SIDE = {
    "long": str(Signal.BULLISH.value).lower(),
    "short": str(Signal.BEARISH.value).lower(),
}


def retrieve_analyst_policy_calibration(
    db: Any,
    *,
    config_id: str,
    ticker: str,
    trading_date: str,
    side: str | None = None,
    horizon_class: str | None = None,
    market_regime: str | None = None,
    setup_type: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return analyst-safe policy calibration rows from structured research state.

    This is an analysis-side calibration interface. It may read persisted research
    state, but it only returns bounded calibration rows and safety diagnostics; it
    never creates trade authority, lots, margin, PM score/rank, or execution rights.
    """
    if not hasattr(db, "get_adaptive_policy_state"):
        return [], {"available": False, "reason": "db_method_missing"}
    try:
        rows = db.get_adaptive_policy_state(
            config_id=config_id,
            ticker=ticker,
            side=side,
            setup_type=setup_type,
            horizon_class=horizon_class,
            market_regime=market_regime,
            trading_date=trading_date,
        )
    except Exception as exc:
        return [], {"available": False, "error": str(exc)}
    safe_rows, safety = filter_adaptive_policy_state_for_pm(list(rows or []))
    safety = dict(safety or {})
    safety.update(
        {
            "available": True,
            "consumer_scope": "analyst_calibration",
            "authority_boundary": "analysis_calibration_only_no_trade_authority_no_lots_no_margin",
        }
    )
    return [dict(row) for row in safe_rows if isinstance(row, Mapping)], safety


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _signal_direction(signal: AnalystSignal) -> str:
    value = signal.signal.value if hasattr(signal.signal, "value") else str(signal.signal)
    return str(value or "").lower()


def _row_side_matches_signal(row: Mapping[str, Any], direction: str) -> bool:
    side = str(row.get("side") or "").lower()
    return not side or side == "*" or _DIRECTION_BY_SIDE.get(side) == direction


def _row_strength(row: Mapping[str, Any]) -> float:
    samples = _safe_int(row.get("sample_count"))
    confidence = _safe_float(row.get("confidence_score"))
    reward_mean = _safe_float(row.get("reward_mean"))
    win_rate = _safe_float(row.get("win_rate"), 0.5)
    pnl = _safe_float(row.get("net_pnl"))
    sample_component = min(0.20, samples / 50.0)
    confidence_component = min(0.25, confidence * 0.25)
    reward_component = min(0.20, abs(reward_mean) / 10000.0)
    win_component = min(0.15, abs(win_rate - 0.5) * 0.30)
    pnl_component = min(0.10, abs(pnl) / 50000.0)
    return _clip(sample_component + confidence_component + reward_component + win_component + pnl_component, 0.0, 0.55)


def _signal_calibration(row: Mapping[str, Any]) -> Mapping[str, Any]:
    calibration = row.get("signal_calibration")
    if not isinstance(calibration, Mapping):
        return {}
    usable_by = {str(item) for item in calibration.get("usable_by") or []}
    allowed = {str(item) for item in calibration.get("allowed_effects") or []}
    forbidden = {str(item) for item in calibration.get("forbidden_effects") or []}
    if "analysis_team" not in usable_by:
        return {}
    if "evidence_quality_calibration" not in allowed and "setup_reliability_context" not in allowed:
        return {}
    if not {"trade_authority", "lots", "margin_ratio", "direction_override"}.issubset(forbidden):
        return {}
    return calibration


def _analyst_safe_action_value_row(row: Mapping[str, Any]) -> Dict[str, Any] | None:
    calibration = _signal_calibration(row)
    if not calibration:
        return None
    return {
        "ticker": row.get("ticker"),
        "side": row.get("side"),
        "horizon_class": row.get("horizon_class"),
        "market_regime": row.get("market_regime"),
        "setup_type": row.get("setup_type"),
        "data_combo": row.get("data_combo"),
        "action_name": row.get("action_name"),
        "sample_count": row.get("sample_count"),
        "confidence_score": row.get("confidence_score"),
        "signal_calibration": dict(calibration),
    }


def _row_is_negative(row: Mapping[str, Any]) -> bool:
    calibration = _signal_calibration(row)
    if calibration:
        bias = str(calibration.get("calibration_bias") or "").lower()
        return bias in {
            "negative_evidence_calibration",
            "questions_same_side_continuation",
        }
    state = str(row.get("lifecycle_state") or "").lower()
    reward_mean = _safe_float(row.get("reward_mean"))
    net_pnl = _safe_float(row.get("net_pnl"))
    return reward_mean < 0 or net_pnl < 0 or state in {"capped", "rejected"}


def _row_is_positive(row: Mapping[str, Any]) -> bool:
    calibration = _signal_calibration(row)
    if calibration:
        bias = str(calibration.get("calibration_bias") or "").lower()
        return bias == "positive_evidence_calibration"
    state = str(row.get("lifecycle_state") or "").lower()
    reward_mean = _safe_float(row.get("reward_mean"))
    net_pnl = _safe_float(row.get("net_pnl"))
    return reward_mean > 0 or net_pnl > 0 or state in {"deployable", "protected"}


def _same_ticker_rows(rows: Iterable[Mapping[str, Any]], ticker: str) -> List[Mapping[str, Any]]:
    ticker_value = str(ticker or "").upper()
    return [row for row in rows if str(row.get("ticker") or "").upper() == ticker_value]


def _broad_prior_rows(rows: Iterable[Mapping[str, Any]], ticker: str) -> List[Mapping[str, Any]]:
    ticker_value = str(ticker or "").upper()
    return [
        row
        for row in rows
        if str(row.get("ticker") or "").upper() not in {"", ticker_value}
    ]


def _matching_rows(rows: Iterable[Mapping[str, Any]], signal: AnalystSignal, ticker: str) -> List[Mapping[str, Any]]:
    direction = _signal_direction(signal)
    same_ticker = _same_ticker_rows(rows, ticker)
    return [row for row in same_ticker if _row_side_matches_signal(row, direction)]


def _broad_matching_rows(rows: Iterable[Mapping[str, Any]], signal: AnalystSignal, ticker: str) -> List[Mapping[str, Any]]:
    direction = _signal_direction(signal)
    return [row for row in _broad_prior_rows(rows, ticker) if _row_side_matches_signal(row, direction)]


def _append_unique(values: List[str], item: str) -> None:
    if item and item not in values:
        values.append(item)


def _unique_strings(values: Iterable[Any], *, max_items: int = 6) -> List[str]:
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= max_items:
            break
    return result


def _row_scope_label(row: Mapping[str, Any]) -> str:
    parts = [
        str(row.get("ticker") or "").upper(),
        str(row.get("side") or ""),
        str(row.get("setup_type") or row.get("data_combo") or ""),
        str(row.get("market_regime") or ""),
        str(row.get("action_name") or ""),
    ]
    label = ":".join(part for part in parts if part)
    return label or "similar_evidence"


def _analyst_metric_names(analyst: str) -> Dict[str, str]:
    analyst_key = str(analyst or "")
    if analyst_key == "technical":
        return {
            "positive": "trigger_reliability_positive",
            "negative": "trigger_reliability_negative",
            "quality": "setup_quality_adjustment",
        }
    if analyst_key == "fundamental":
        return {
            "positive": "factor_reliability_positive",
            "negative": "factor_reliability_negative",
            "quality": "support_conflict_adjustment",
        }
    if analyst_key in {"commodity_news", "company_news"}:
        return {
            "positive": "event_reliability_positive",
            "negative": "event_reliability_negative",
            "quality": "catalyst_quality_adjustment",
        }
    return {
        "positive": "evidence_reliability_positive",
        "negative": "evidence_reliability_negative",
        "quality": "evidence_quality_adjustment",
    }


def _learning_impact_summary(
    *,
    analyst: str,
    signal: AnalystSignal,
    positive_rows: List[Mapping[str, Any]],
    negative_rows: List[Mapping[str, Any]],
    broad_positive_rows: List[Mapping[str, Any]],
    broad_negative_rows: List[Mapping[str, Any]],
    positive_strength: float,
    negative_strength: float,
    broad_positive_strength: float,
    broad_negative_strength: float,
    net_adjustment: float,
) -> Dict[str, Any]:
    historical_support = _unique_strings(
        [_row_scope_label(row) for row in positive_rows + broad_positive_rows],
        max_items=6,
    )
    historical_contradiction = _unique_strings(
        [_row_scope_label(row) for row in negative_rows + broad_negative_rows],
        max_items=6,
    )
    current_confirmed = _unique_strings(
        list(getattr(signal, "factor_focus", []) or [])
        + list(getattr(signal, "setup_quality_notes", []) or []),
        max_items=8,
    )
    current_missing = _unique_strings(
        list(getattr(signal, "missing_evidence", []) or [])
        + list(getattr(signal, "current_evidence_conflict", []) or [])
        + list(getattr(signal, "conflicting_factors", []) or []),
        max_items=8,
    )
    opportunity_state = str(getattr(signal, "opportunity_state", "") or "watch_for_trigger")
    if net_adjustment > 0:
        state_reason = "past same-scope evidence improved evidence reliability; current evidence still determines opportunity_state"
    elif net_adjustment < 0:
        state_reason = "past same-scope evidence raised reliability concerns; current evidence must re-confirm before stronger opportunity_state"
    else:
        state_reason = "no eligible past signal calibration changed today's opportunity_state"
    return {
        "contract_version": "agentquant.analyst_learning_impact.v1",
        "analyst": str(analyst or ""),
        "historical_support": historical_support,
        "historical_contradiction": historical_contradiction,
        "current_evidence_confirmed": current_confirmed,
        "current_evidence_missing": current_missing,
        "opportunity_state": opportunity_state,
        "opportunity_state_reason": state_reason,
        "positive_strength": round(positive_strength, 4),
        "negative_strength": round(negative_strength, 4),
        "broad_positive_strength": round(broad_positive_strength, 4),
        "broad_negative_strength": round(broad_negative_strength, 4),
        "net_evidence_adjustment": round(net_adjustment, 4),
        "authority_boundary": "evidence_explanation_only_no_trade_authority_no_lots_no_margin_no_execution",
    }


def _factor_calibration_summary(
    signal: AnalystSignal,
    *,
    positive_rows: List[Mapping[str, Any]],
    negative_rows: List[Mapping[str, Any]],
) -> Dict[str, Any]:
    focus = _unique_strings(getattr(signal, "factor_focus", []) or [], max_items=8)
    conflicts = _unique_strings(
        list(getattr(signal, "conflicting_factors", []) or [])
        + list(getattr(signal, "current_evidence_conflict", []) or []),
        max_items=8,
    )
    requiring_confirmation = _unique_strings(
        list(getattr(signal, "missing_evidence", []) or [])
        + [str(getattr(signal, "entry_trigger", "") or "")],
        max_items=6,
    )
    return {
        "contract_version": "agentquant.factor_calibration.v1",
        "effective_factors": focus,
        "stale_or_conflicting_factors": conflicts,
        "factors_requiring_price_confirmation": requiring_confirmation,
        "supporting_learning_scopes": _unique_strings([_row_scope_label(row) for row in positive_rows], max_items=6),
        "contradicting_learning_scopes": _unique_strings([_row_scope_label(row) for row in negative_rows], max_items=6),
        "factor_calibration_reason": "fundamental evidence calibration only; PM still requires current trigger, invalidation, and final contract",
        "authority_boundary": "no_trade_authority_no_lots_no_margin",
    }


def _event_calibration_summary(
    signal: AnalystSignal,
    *,
    positive_rows: List[Mapping[str, Any]],
    negative_rows: List[Mapping[str, Any]],
) -> Dict[str, Any]:
    focus = _unique_strings(getattr(signal, "factor_focus", []) or [], max_items=8)
    conflicts = _unique_strings(
        list(getattr(signal, "conflicting_factors", []) or [])
        + list(getattr(signal, "current_evidence_conflict", []) or []),
        max_items=8,
    )
    confirmation_required = bool(
        not bool(getattr(signal, "trigger_valid", False))
        or getattr(signal, "neutral_trigger_condition", "")
        or getattr(signal, "entry_trigger", "")
    )
    return {
        "contract_version": "agentquant.event_calibration.v1",
        "effective_catalysts": focus,
        "background_noise": conflicts,
        "impact_window_assessment": str(getattr(signal, "impact_window_days", 0) or 0),
        "price_volume_confirmation_required": confirmation_required,
        "supporting_learning_scopes": _unique_strings([_row_scope_label(row) for row in positive_rows], max_items=6),
        "contradicting_learning_scopes": _unique_strings([_row_scope_label(row) for row in negative_rows], max_items=6),
        "event_calibration_reason": "news evidence calibration only; PM still requires current confirmation and final contract",
        "authority_boundary": "no_trade_authority_no_lots_no_margin",
    }


def calibrate_signal_with_learning_context(
    signal: AnalystSignal,
    *,
    analyst: str,
    ticker: str,
    learning_context: Mapping[str, Any] | None,
) -> AnalystSignal:
    """Adjust analyst evidence quality using past-only compact learning context.

    The result remains an analyst signal. It only changes evidence quality fields
    and metadata; it never creates trade authority, lots, margin, or PM bypasses.
    """
    context = dict(learning_context or {})
    alpha_profiles = list(context.get("alpha_setup_items") or [])
    alpha_action_values = [
        safe_row
        for row in list(context.get("alpha_setup_action_values") or [])
        if isinstance(row, Mapping)
        for safe_row in [_analyst_safe_action_value_row(row)]
        if safe_row is not None
    ]
    rows: List[Mapping[str, Any]] = [row for row in alpha_profiles + alpha_action_values if isinstance(row, Mapping)]

    matched = _matching_rows(rows, signal, ticker)
    broad = _broad_matching_rows(rows, signal, ticker)
    positive_rows = [row for row in matched if _row_is_positive(row)]
    negative_rows = [row for row in matched if _row_is_negative(row)]
    broad_positive_rows = [row for row in broad if _row_is_positive(row)]
    broad_negative_rows = [row for row in broad if _row_is_negative(row)]

    positive_strength = min(0.18, sum(_row_strength(row) for row in positive_rows))
    negative_strength = min(0.24, sum(_row_strength(row) for row in negative_rows))
    broad_positive_strength = min(0.06, sum(_row_strength(row) for row in broad_positive_rows))
    broad_negative_strength = min(0.08, sum(_row_strength(row) for row in broad_negative_rows))
    net_adjustment = positive_strength + broad_positive_strength - negative_strength - broad_negative_strength

    setup_notes = list(getattr(signal, "setup_quality_notes", []) or [])
    conflicts = list(getattr(signal, "current_evidence_conflict", []) or [])
    conflicting_factors = list(getattr(signal, "conflicting_factors", []) or [])
    factor_focus = list(getattr(signal, "factor_focus", []) or [])

    metric_names = _analyst_metric_names(analyst)
    if positive_strength > 0:
        _append_unique(setup_notes, metric_names["positive"])
    if negative_strength > 0:
        _append_unique(setup_notes, metric_names["negative"])
        _append_unique(conflicts, f"{analyst}_same_scope_negative_learning")
        _append_unique(conflicting_factors, f"{analyst}_same_scope_negative_learning")
    if broad_positive_strength or broad_negative_strength:
        _append_unique(setup_notes, f"{analyst}_broad_prior_weak_only")
    if rows:
        _append_unique(factor_focus, f"{analyst}_learning_calibration")

    signal.setup_quality_notes = setup_notes
    signal.current_evidence_conflict = conflicts
    signal.conflicting_factors = conflicting_factors
    signal.factor_focus = factor_focus
    signal.business_quality_score = _clip(_safe_float(getattr(signal, "business_quality_score", 0.0)) + net_adjustment)
    signal.factor_alignment_score = _clip(_safe_float(getattr(signal, "factor_alignment_score", 0.0)) + net_adjustment * 0.75)
    signal.confidence = _clip(_safe_float(getattr(signal, "confidence", 0.0)) + net_adjustment * 0.50)

    metadata = dict(getattr(signal, "metadata", {}) or {})
    metadata["analyst_learning_calibration"] = {
        "enabled": bool(rows),
        "analyst": str(analyst or ""),
        "ticker": str(ticker or "").upper(),
        "same_ticker_matched_count": len(matched),
        "broad_prior_matched_count": len(broad),
        "positive_strength": round(positive_strength, 4),
        "negative_strength": round(negative_strength, 4),
        "broad_positive_strength": round(broad_positive_strength, 4),
        "broad_negative_strength": round(broad_negative_strength, 4),
        "net_evidence_adjustment": round(net_adjustment, 4),
        "metric_role": metric_names["quality"],
        "authority_boundary": (
            "evidence_quality_only_no_trade_authority_no_sizing_no_pm_auditor_trader_bypass"
        ),
    }
    impact_summary = _learning_impact_summary(
        analyst=analyst,
        signal=signal,
        positive_rows=positive_rows,
        negative_rows=negative_rows,
        broad_positive_rows=broad_positive_rows,
        broad_negative_rows=broad_negative_rows,
        positive_strength=positive_strength,
        negative_strength=negative_strength,
        broad_positive_strength=broad_positive_strength,
        broad_negative_strength=broad_negative_strength,
        net_adjustment=net_adjustment,
    )
    signal.learning_impact_summary = impact_summary
    metadata["learning_impact_summary"] = impact_summary
    analyst_key = str(analyst or "")
    if analyst_key == "fundamental":
        factor_summary = _factor_calibration_summary(
            signal,
            positive_rows=positive_rows,
            negative_rows=negative_rows,
        )
        signal.factor_calibration_summary = factor_summary
        metadata["factor_calibration_summary"] = factor_summary
    elif analyst_key in {"commodity_news", "company_news"}:
        event_summary = _event_calibration_summary(
            signal,
            positive_rows=positive_rows,
            negative_rows=negative_rows,
        )
        signal.event_calibration_summary = event_summary
        metadata["event_calibration_summary"] = event_summary
    signal.metadata = metadata
    return signal
