"""Evidence calibration from past research records for analyst signals.

This module turns bounded learning context into analyst-side evidence quality
adjustments. It must not create trade authority, sizing authority, or PM bypasses.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping

from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.common.adaptive_policy_safety import filter_adaptive_policy_state_for_pm
from tools.common.execution_trigger_semantics import (
    CANONICAL_ENTRY_TRIGGERS,
    canonical_entry_trigger,
    normalize_execution_profile,
)
from tools.common.final_action_semantics import (
    ACTION_FAMILY_OPEN_ADD_NEW_RISK,
    canonical_action_family,
    canonical_action_value_lane,
)
from tools.common.learning_identity import canonical_market_regime


_DIRECTION_BY_SIDE = {
    "long": str(Signal.BULLISH.value).lower(),
    "short": str(Signal.BEARISH.value).lower(),
}

_CROSS_REGIME_RETRIEVAL_MATCH = "cross_regime_same_ticker_side_horizon"
_CROSS_REGIME_CALIBRATION_WEIGHT = 0.25
_CANONICAL_TRIGGER_IDENTITIES = frozenset(
    "".join(
        character
        for character in str(trigger or "").strip().lower().replace(" ", "_").replace("/", "_")
        if character.isalnum() or character in {"_", "-", "*"}
    )
    for trigger in CANONICAL_ENTRY_TRIGGERS
)


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
    market_regime = canonical_market_regime(market_regime, "*")
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


def _signal_side(signal: AnalystSignal) -> str:
    direction = _signal_direction(signal)
    for side, signal_value in _DIRECTION_BY_SIDE.items():
        if direction == signal_value:
            return side
    counterfactual_side = str(
        getattr(signal, "counterfactual_side", "") or ""
    ).strip().lower()
    return counterfactual_side if counterfactual_side in _DIRECTION_BY_SIDE else ""


def _row_side_matches_signal(row: Mapping[str, Any], direction: str) -> bool:
    side = str(row.get("side") or "").lower()
    return not side or side == "*" or _DIRECTION_BY_SIDE.get(side) == direction


def _row_strength(row: Mapping[str, Any]) -> float:
    samples = _safe_int(row.get("sample_count"))
    confidence = _safe_float(row.get("confidence_score"))
    win_rate = _safe_float(row.get("win_rate"), 0.5)
    sample_component = min(0.20, samples / 50.0)
    confidence_component = min(0.25, confidence * 0.25)
    win_component = min(0.15, abs(win_rate - 0.5) * 0.30)
    mean_return_on_notional = row.get("mean_return_on_notional")
    return_component = (
        min(0.30, abs(_safe_float(mean_return_on_notional)) * 10.0)
        if mean_return_on_notional is not None
        else 0.0
    )
    strength = _clip(
        sample_component + confidence_component + win_component + return_component,
        0.0,
        0.55,
    )
    if str(row.get("retrieval_match_level") or "") == _CROSS_REGIME_RETRIEVAL_MATCH:
        strength *= _CROSS_REGIME_CALIBRATION_WEIGHT
    return strength


def _bounded_same_ticker_strength(
    rows: Iterable[Mapping[str, Any]],
    *,
    total_cap: float,
    cross_regime_cap: float,
) -> float:
    exact_strength = 0.0
    cross_regime_strength = 0.0
    for row in rows:
        strength = _row_strength(row)
        if str(row.get("retrieval_match_level") or "") == _CROSS_REGIME_RETRIEVAL_MATCH:
            cross_regime_strength += strength
        else:
            exact_strength += strength
    return min(total_cap, exact_strength + min(cross_regime_cap, cross_regime_strength))


def _signal_calibration(row: Mapping[str, Any]) -> Mapping[str, Any]:
    calibration = row.get("signal_calibration")
    if not isinstance(calibration, Mapping):
        return {}
    usable_by = {str(item) for item in calibration.get("usable_by") or []}
    allowed = {str(item) for item in calibration.get("allowed_effects") or []}
    forbidden = {str(item) for item in calibration.get("forbidden_effects") or []}
    if calibration.get("contract_version") != "agentquant.analysis_signal_calibration.v1":
        return {}
    if str(calibration.get("consumer_scope") or "").strip() != "analyst_calibration":
        return {}
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
    product_view = row.get("product_learning_calibration_view")
    if not isinstance(product_view, Mapping):
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        product_view = payload.get("product_learning_calibration_view")
    def calibration_value(name: str) -> Any:
        value = row.get(name)
        return value if value is not None else calibration.get(name)

    safe_row = {
        "source_learning_record_id": row.get("source_learning_record_id") or row.get("id"),
        "ticker": row.get("ticker"),
        "side": row.get("side"),
        "horizon_class": row.get("horizon_class"),
        "market_regime": row.get("market_regime"),
        "setup_type": row.get("setup_type"),
        "data_combo": row.get("data_combo"),
        "action_name": row.get("action_name"),
        "action_value_lane": (
            row.get("action_value_lane")
            or calibration.get("source_action_value_lane")
        ),
        "learning_lane": row.get("learning_lane"),
        "canonical_action_family": row.get("canonical_action_family"),
        "sample_count": row.get("sample_count"),
        "confidence_score": row.get("confidence_score"),
        "mean_return_on_notional": calibration_value("mean_return_on_notional"),
        "latest_complete_episode_return_on_notional": calibration_value(
            "latest_complete_episode_return_on_notional"
        ),
        "latest_complete_episode_date": calibration_value(
            "latest_complete_episode_date"
        ),
        "latest_complete_episode_outcome": calibration_value(
            "latest_complete_episode_outcome"
        ),
        "signal_calibration": dict(calibration),
    }
    if str(row.get("retrieval_match_level") or "") == _CROSS_REGIME_RETRIEVAL_MATCH:
        safe_row["retrieval_match_level"] = _CROSS_REGIME_RETRIEVAL_MATCH
    if isinstance(product_view, Mapping):
        safe_row["product_learning_calibration_view"] = dict(product_view)
    return safe_row


def _row_is_negative(row: Mapping[str, Any]) -> bool:
    calibration = _signal_calibration(row)
    latest_return = row.get("latest_complete_episode_return_on_notional")
    mean_return = row.get("mean_return_on_notional")
    entry_return_economics = bool(
        calibration
        and str(calibration.get("learning_economics_basis") or "").strip()
        == "after_fee_return_on_notional"
        and mean_return is not None
    )
    if entry_return_economics:
        if latest_return is not None and _safe_float(latest_return) < 0.0:
            return True
        return _safe_float(mean_return) < 0.0
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
    latest_return = row.get("latest_complete_episode_return_on_notional")
    mean_return = row.get("mean_return_on_notional")
    entry_return_economics = bool(
        calibration
        and str(calibration.get("learning_economics_basis") or "").strip()
        == "after_fee_return_on_notional"
        and mean_return is not None
    )
    if entry_return_economics:
        if latest_return is not None and _safe_float(latest_return) < 0.0:
            return False
        return _safe_float(mean_return) > 0.0
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
    side = _signal_side(signal)
    same_ticker = _same_ticker_rows(rows, ticker)
    direction = _DIRECTION_BY_SIDE.get(side, "")
    return [row for row in same_ticker if direction and _row_side_matches_signal(row, direction)]


def _broad_matching_rows(rows: Iterable[Mapping[str, Any]], signal: AnalystSignal, ticker: str) -> List[Mapping[str, Any]]:
    side = _signal_side(signal)
    direction = _DIRECTION_BY_SIDE.get(side, "")
    return [
        row
        for row in _broad_prior_rows(rows, ticker)
        if direction and _row_side_matches_signal(row, direction)
    ]


def _normalized_identity(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "_").replace("/", "_")
    return "".join(
        character
        for character in text
        if character.isalnum() or character in {"_", "-", "*"}
    )


def _product_learning_view(row: Mapping[str, Any]) -> Mapping[str, Any]:
    product_view = row.get("product_learning_calibration_view")
    if isinstance(product_view, Mapping):
        return product_view
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    product_view = payload.get("product_learning_calibration_view")
    return product_view if isinstance(product_view, Mapping) else {}


def _row_entry_lane(row: Mapping[str, Any]) -> tuple[str, bool]:
    calibration = _signal_calibration(row)
    product_view = _product_learning_view(row)
    lanes = {
        canonical_action_value_lane(value)
        for value in (
            calibration.get("source_action_value_lane"),
            row.get("action_value_lane"),
            row.get("learning_lane"),
            row.get("action_name"),
            product_view.get("action_name"),
        )
        if _normalized_identity(value)
    }
    if len(lanes) != 1:
        return "", False
    lane = next(iter(lanes))
    action_names = [
        value
        for value in (row.get("action_name"), product_view.get("action_name"))
        if _normalized_identity(value)
    ]
    families = {
        _normalized_identity(value)
        for value in (
            row.get("canonical_action_family"),
            calibration.get("source_canonical_action_family"),
        )
        if _normalized_identity(value)
    } | {
        canonical_action_family(value)
        for value in action_names
    }
    family_valid = (
        len(families) == 1
        and next(iter(families)) == ACTION_FAMILY_OPEN_ADD_NEW_RISK
    )
    return lane, family_valid and canonical_action_family(lane) == ACTION_FAMILY_OPEN_ADD_NEW_RISK


def _row_setup_identity(row: Mapping[str, Any]) -> tuple[str, bool]:
    product_view = _product_learning_view(row)
    values = {
        _normalized_identity(value)
        for value in (row.get("setup_type"), product_view.get("setup_type"))
        if _normalized_identity(value) not in {"", "*", "unknown"}
    }
    if len(values) != 1:
        return "", False
    return next(iter(values)), True


def _row_trigger_identity(row: Mapping[str, Any]) -> tuple[str, bool]:
    product_view = _product_learning_view(row)
    entry_view = product_view.get("entry_quality_calibration")
    if not isinstance(entry_view, Mapping):
        entry_view = {}
    values = {
        _normalized_identity(value)
        for value in (
            entry_view.get("trigger_key"),
            product_view.get("trigger_key"),
        )
        if _normalized_identity(value) not in {"", "unknown_trigger"}
    }
    if len(values) != 1:
        return "", False
    trigger = next(iter(values))
    return trigger, trigger in _CANONICAL_TRIGGER_IDENTITIES


def _entry_learning_scope_identity(row: Mapping[str, Any]) -> tuple[str, ...]:
    product_view = _product_learning_view(row)
    setup_type, setup_valid = _row_setup_identity(row)
    trigger_key, trigger_valid = _row_trigger_identity(row)
    if not setup_valid or not trigger_valid:
        return ()
    return (
        str(row.get("ticker") or product_view.get("ticker") or "").strip().upper(),
        _normalized_identity(row.get("side") or product_view.get("side")),
        _normalized_identity(
            row.get("horizon_class") or product_view.get("horizon_class")
        ),
        canonical_market_regime(
            row.get("market_regime") or product_view.get("market_regime"),
            "*",
        ),
        setup_type,
        _normalized_identity(
            row.get("data_combo") or product_view.get("evidence_combo") or "*"
        ),
        trigger_key,
    )


def _latest_exact_entry_loss(row: Mapping[str, Any]) -> bool:
    calibration = _signal_calibration(row)
    if not calibration:
        return False
    latest_return = row.get("latest_complete_episode_return_on_notional")
    return bool(
        str(calibration.get("learning_economics_basis") or "").strip()
        == "after_fee_return_on_notional"
        and str(calibration.get("source_quality") or "").strip()
        == "exact_real_state"
        and latest_return is not None
        and _safe_float(latest_return) < 0.0
    )


def _current_entry_identity(signal: AnalystSignal) -> tuple[str, str]:
    setup_type = _normalized_identity(getattr(signal, "setup_type", ""))
    if setup_type in {"", "*", "unknown"}:
        setup_type = ""
    side = _signal_side(signal)
    profile = normalize_execution_profile(
        getattr(signal, "entry_timing_signal", "")
    )
    trigger = _normalized_identity(canonical_entry_trigger(profile, side))
    if trigger not in _CANONICAL_TRIGGER_IDENTITIES:
        trigger = ""
    return setup_type, trigger


def _entry_calibration_rejection_reason(
    row: Mapping[str, Any],
    *,
    current_setup: str,
    current_trigger: str,
) -> str:
    lane, valid_lane = _row_entry_lane(row)
    if not lane:
        return "learning_lane_missing_or_conflicting"
    if not valid_lane:
        return "learning_lane_not_open_add"
    row_setup, valid_setup = _row_setup_identity(row)
    if not valid_setup or not current_setup:
        return "setup_identity_missing_or_conflicting"
    if row_setup != current_setup:
        return "setup_mismatch"
    row_trigger, valid_trigger = _row_trigger_identity(row)
    if not valid_trigger or not current_trigger:
        return "canonical_trigger_missing_or_conflicting"
    if row_trigger != current_trigger:
        return "canonical_trigger_mismatch"
    return ""


def _eligible_entry_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    current_setup: str,
    current_trigger: str,
) -> tuple[List[Mapping[str, Any]], Counter[str]]:
    eligible: List[Mapping[str, Any]] = []
    rejected: Counter[str] = Counter()
    for row in rows:
        reason = _entry_calibration_rejection_reason(
            row,
            current_setup=current_setup,
            current_trigger=current_trigger,
        )
        if reason:
            rejected[reason] += 1
        else:
            eligible.append(row)
    return eligible, rejected


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
    product_view = row.get("product_learning_calibration_view")
    if not isinstance(product_view, Mapping):
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        product_view = payload.get("product_learning_calibration_view")
    if isinstance(product_view, Mapping):
        key = str(product_view.get("performance_scope_key") or "").strip()
        if key:
            return key
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
    if analyst_key == "commodity_news":
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
    prompt_learning_record_ids: List[str],
    evidence_calibration_record_ids: List[str],
    technical_parameter_calibrations: List[Mapping[str, Any]],
) -> Dict[str, Any]:
    historical_support = _unique_strings(
        [_row_scope_label(row) for row in positive_rows + broad_positive_rows],
        max_items=6,
    )
    historical_contradiction = _unique_strings(
        [_row_scope_label(row) for row in negative_rows + broad_negative_rows],
        max_items=6,
    )
    product_learning_scopes = _unique_strings(
        [
            str(view.get("performance_scope_key") or "")
            for row in positive_rows + negative_rows + broad_positive_rows + broad_negative_rows
            for view in [
                row.get("product_learning_calibration_view")
                if isinstance(row.get("product_learning_calibration_view"), Mapping)
                else {}
            ]
        ],
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
        "product_learning_scopes": product_learning_scopes,
        "current_evidence_confirmed": current_confirmed,
        "current_evidence_missing": current_missing,
        "opportunity_state": opportunity_state,
        "opportunity_state_reason": state_reason,
        "positive_strength": round(positive_strength, 4),
        "negative_strength": round(negative_strength, 4),
        "broad_positive_strength": round(broad_positive_strength, 4),
        "broad_negative_strength": round(broad_negative_strength, 4),
        "net_evidence_adjustment": round(net_adjustment, 4),
        "prompt_calibration_applied": bool(prompt_learning_record_ids),
        "prompt_learning_record_ids": prompt_learning_record_ids,
        "evidence_calibration_applied": bool(evidence_calibration_record_ids),
        "evidence_calibration_record_ids": evidence_calibration_record_ids,
        "technical_parameter_calibration_applied": bool(technical_parameter_calibrations),
        "technical_parameter_calibrations": technical_parameter_calibrations,
        "authority_boundary": "evidence_explanation_only_no_trade_authority_no_lots_no_margin_no_execution",
    }


def _learning_row_ids(rows: Iterable[Mapping[str, Any]]) -> List[str]:
    return _unique_strings(
        [
            str(row.get("source_learning_record_id") or row.get("id") or "")
            for row in rows
            if isinstance(row, Mapping)
        ],
        max_items=12,
    )


def _technical_parameter_calibration_summary(
    diagnostics: Mapping[str, Any] | None,
) -> List[Dict[str, Any]]:
    summary: List[Dict[str, Any]] = []
    source = diagnostics if isinstance(diagnostics, Mapping) else {}
    for row in list(source.get("applied") or []):
        if not isinstance(row, Mapping):
            continue
        changed = row.get("changed") if isinstance(row.get("changed"), Mapping) else {}
        parameter_changes: Dict[str, Dict[str, Any]] = {}
        for parameter, change in changed.items():
            if not isinstance(change, Mapping):
                continue
            parameter_changes[str(parameter)] = {
                "from": change.get("from"),
                "to": change.get("to"),
            }
        if not parameter_changes:
            continue
        summary.append(
            {
                "policy_id": str(row.get("id") or ""),
                "parameter_changes": parameter_changes,
            }
        )
        if len(summary) >= 8:
            break
    return summary


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
    alpha_profiles = [
        row
        for row in list(context.get("alpha_setup_items") or [])
        if isinstance(row, Mapping)
    ]
    raw_analyst_calibration_items = [
        row
        for row in list(context.get("analyst_calibration_items") or [])
        if isinstance(row, Mapping)
    ]
    analyst_calibration_items = [
        safe_row
        for row in raw_analyst_calibration_items
        for safe_row in [_analyst_safe_action_value_row(row)]
        if safe_row is not None
    ]
    rows: List[Mapping[str, Any]] = [row for row in alpha_profiles + analyst_calibration_items if isinstance(row, Mapping)]

    current_setup, current_trigger = _current_entry_identity(signal)
    matched_candidates = _matching_rows(rows, signal, ticker)
    broad_candidates = _broad_matching_rows(rows, signal, ticker)
    matched, matched_rejections = _eligible_entry_rows(
        matched_candidates,
        current_setup=current_setup,
        current_trigger=current_trigger,
    )
    broad, broad_rejections = _eligible_entry_rows(
        broad_candidates,
        current_setup=current_setup,
        current_trigger=current_trigger,
    )
    rejected_reasons = matched_rejections + broad_rejections
    unsafe_contract_count = len(raw_analyst_calibration_items) - len(
        analyst_calibration_items
    )
    if unsafe_contract_count > 0:
        rejected_reasons["analyst_calibration_contract_invalid"] += unsafe_contract_count
    latest_loss_scope_identities = {
        scope_identity
        for row in matched
        if _latest_exact_entry_loss(row)
        for scope_identity in [_entry_learning_scope_identity(row)]
        if scope_identity
    }
    positive_rows = [
        row
        for row in matched
        if _row_is_positive(row)
        and _entry_learning_scope_identity(row) not in latest_loss_scope_identities
    ]
    negative_rows = [row for row in matched if _row_is_negative(row)]
    broad_positive_rows = [row for row in broad if _row_is_positive(row)]
    broad_negative_rows = [row for row in broad if _row_is_negative(row)]

    positive_strength = _bounded_same_ticker_strength(
        positive_rows,
        total_cap=0.18,
        cross_regime_cap=0.06,
    )
    negative_strength = _bounded_same_ticker_strength(
        negative_rows,
        total_cap=0.24,
        cross_regime_cap=0.08,
    )
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
    if matched or broad:
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
        "enabled": bool(matched or broad),
        "analyst": str(analyst or ""),
        "ticker": str(ticker or "").upper(),
        "same_ticker_matched_count": len(matched),
        "broad_prior_matched_count": len(broad),
        "eligible_entry_learning_count": len(matched) + len(broad),
        "rejected_entry_learning_count": sum(rejected_reasons.values()),
        "rejected_entry_learning_reason_counts": dict(rejected_reasons),
        "current_setup_type": current_setup,
        "current_canonical_trigger": current_trigger,
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
        prompt_learning_record_ids=_unique_strings(
            [
                str(item)
                for item in list(
                    context.get("prompt_learning_record_ids")
                    or context.get("selected_ids")
                    or []
                )
            ],
            max_items=12,
        ),
        evidence_calibration_record_ids=_learning_row_ids(matched + broad),
        technical_parameter_calibrations=_technical_parameter_calibration_summary(
            context.get("technical_parameter_calibration")
            if isinstance(context.get("technical_parameter_calibration"), Mapping)
            else {}
        ),
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
    elif analyst_key == "commodity_news":
        event_summary = _event_calibration_summary(
            signal,
            positive_rows=positive_rows,
            negative_rows=negative_rows,
        )
        signal.event_calibration_summary = event_summary
        metadata["event_calibration_summary"] = event_summary
    signal.metadata = metadata
    return signal
