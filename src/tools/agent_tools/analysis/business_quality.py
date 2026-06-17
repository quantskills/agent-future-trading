from __future__ import annotations

"""Business-quality enrichment for analyst signals.

The LLM is allowed to stay Neutral, but Neutral must be accountable. This
module converts structured prechecks and model output into deterministic fields
that PM, auditor, trader, and reviewer can consume without parsing free text.
"""

from typing import Any, Dict, Iterable, List

from graph.constants import Signal
from graph.schema import AnalystSignal


TRADEABILITY_BASE_SCORE = {
    "high": 0.74,
    "medium": 0.58,
    "low": 0.34,
    "unknown": 0.42,
}


def _signal_value(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value or "Neutral")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


def _as_text_list(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, list):
        return [str(item) for item in values if str(item).strip()]
    if isinstance(values, tuple) or isinstance(values, set):
        return [str(item) for item in values if str(item).strip()]
    text = str(values).strip()
    return [text] if text else []


def _first_nonempty(*values: Any, default: str = "unknown") -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text.lower() in {"unknown", "none", "n/a", "null"}:
                continue
            return text
        if not isinstance(value, str):
            text = str(value).strip()
            if text and text.lower() not in {"unknown", "none", "n/a", "null"}:
                return text
    return default


def infer_template_name(signal: AnalystSignal, quality_context: Dict[str, Any], analyst: str) -> str:
    existing = str(getattr(signal, "template_name", "") or "").strip()
    if existing and existing != "unknown":
        return existing
    trigger = str(getattr(signal, "trigger_type", "") or "").lower()
    trend_stage = str(getattr(signal, "trend_stage", "") or quality_context.get("market_regime") or "").lower()
    price_percentile = getattr(signal, "price_percentile", None)
    try:
        percentile = float(price_percentile) if price_percentile is not None else None
    except Exception:
        percentile = None
    side = _signal_value(signal.signal).lower()

    if "break" in trigger or "breakout" in trigger:
        return "breakout_continuation"
    if "rebound" in trigger or "failed" in trigger:
        return "failed_rebound_short" if side == "bearish" else "pullback_recovery_long"
    if trend_stage in {"range", "choppy", "weak_trend"}:
        return "range_filter"
    if percentile is not None:
        if side == "bullish" and percentile <= 0.35:
            return "low_position_reversal_confirmed"
        if side == "bullish" and percentile >= 0.75:
            return "late_chase_long"
        if side == "bearish" and percentile >= 0.65:
            return "high_position_breakdown"
        if side == "bearish" and percentile <= 0.25:
            return "low_position_chase_short"
    if analyst == "fundamental":
        return "fundamental_direction_anchor"
    if analyst == "commodity_news":
        return "news_event_probe"
    return "technical_price_trigger"


def compute_business_quality_score(
    signal: AnalystSignal,
    quality_context: Dict[str, Any],
    analyst: str,
) -> float:
    tradeability = str(quality_context.get("tradeability") or signal.metadata.get("tradeability") or "unknown").lower()
    base = TRADEABILITY_BASE_SCORE.get(tradeability, TRADEABILITY_BASE_SCORE["unknown"])
    confidence = _safe_float(getattr(signal, "confidence", 0.0), 0.0)
    risk_flags = _as_text_list(quality_context.get("risk_flags") or signal.metadata.get("risk_flags"))
    data_quality = quality_context.get("data_quality") if isinstance(quality_context.get("data_quality"), dict) else {}
    coverage = _safe_float(
        getattr(signal, "data_coverage_score", 0.0)
        or data_quality.get("coverage_ratio")
        or quality_context.get("freshness_score")
        or quality_context.get("relevance_score"),
        0.0,
    )
    if coverage <= 0 and tradeability in {"high", "medium"}:
        coverage = 0.65 if tradeability == "high" else 0.50

    score = 0.52 * base + 0.28 * confidence + 0.20 * _clamp(coverage)
    if "stale_fundamental_inputs" in risk_flags or "missing_fundamental_inputs" in risk_flags:
        score -= 0.12
    if "mixed_news_direction" in risk_flags or "conflicting_indicators" in risk_flags:
        score -= 0.08
    if _signal_value(signal.signal) == Signal.NEUTRAL.value:
        score = min(score, 0.56)
    return _clamp(score)


def _business_driver_from_context(quality_context: Dict[str, Any], analyst: str) -> str:
    if analyst == "technical":
        features = quality_context.get("features") or {}
        return _first_nonempty(
            quality_context.get("market_regime"),
            f"trend_strength={features.get('trend_strength')}; volume_ratio={features.get('volume_ratio')}",
        )
    if analyst == "fundamental":
        groups = quality_context.get("factor_group_counts") or quality_context.get("factor_groups") or {}
        return f"fundamental_factor_groups={list(groups)[:6]}" if groups else "fundamental_factor_coverage"
    if analyst == "commodity_news":
        types = quality_context.get("event_type_counts") or {}
        return f"event_types={list(types)[:6]}" if types else "news_event_context"
    return "agent_specific_business_context"


def _secondary_confirmation_from_context(quality_context: Dict[str, Any], analyst: str) -> str:
    if analyst == "technical":
        votes = quality_context.get("indicator_votes") or {}
        return f"indicator_votes={votes}"
    if analyst == "fundamental":
        return f"data_quality={quality_context.get('data_quality') or {}}; basis={quality_context.get('basis') or 'unavailable'}"
    if analyst == "commodity_news":
        return (
            f"freshness={quality_context.get('freshness_score', 0.0)}; "
            f"relevance={quality_context.get('relevance_score', 0.0)}"
        )
    return "secondary_confirmation_unavailable"


def _counter_evidence_from_context(quality_context: Dict[str, Any]) -> str:
    flags = _as_text_list(quality_context.get("risk_flags"))
    if flags:
        return ", ".join(flags)
    return "no explicit counter-evidence flagged by deterministic precheck"


def _shadow_side_from_context(signal: AnalystSignal, quality_context: Dict[str, Any], analyst: str) -> str:
    text_signal = _signal_value(getattr(signal, "signal", Signal.NEUTRAL))
    if text_signal == Signal.BULLISH.value:
        return "long"
    if text_signal == Signal.BEARISH.value:
        return "short"
    if analyst == "technical":
        direction = str(quality_context.get("dominant_direction") or "").lower()
        if direction == "bullish":
            return "long"
        if direction == "bearish":
            return "short"
    if analyst == "commodity_news":
        counts = quality_context.get("direction_counts") or {}
        bullish = int(counts.get("bullish", 0) or 0)
        bearish = int(counts.get("bearish", 0) or 0)
        if bullish > bearish and bullish >= 1:
            return "long"
        if bearish > bullish and bearish >= 1:
            return "short"
    if analyst == "fundamental":
        anchor = str(getattr(signal, "direction_anchor", "") or "").lower()
        if anchor in {"bullish", "long"}:
            return "long"
        if anchor in {"bearish", "short"}:
            return "short"
    return "flat"


def _neutral_missing_evidence(signal: AnalystSignal, quality_context: Dict[str, Any], analyst: str) -> List[str]:
    missing = _as_text_list(getattr(signal, "missing_evidence", []))
    if missing:
        return missing
    if analyst == "technical":
        return ["clear trend stage", "price-location reward/risk confirmation", "volume/open-interest confirmation"]
    if analyst == "fundamental":
        return ["fresh supply-demand anchor", "basis/inventory alignment", "medium-horizon confirmation"]
    if analyst == "commodity_news":
        return ["high-relevance event", "directional impact chain", "fundamental or price confirmation"]
    return ["sufficient directional evidence"]


def _neutral_opportunity_contract(
    signal: AnalystSignal,
    quality_context: Dict[str, Any],
    analyst: str,
) -> Dict[str, Any]:
    tradeability = str(quality_context.get("tradeability") or "").lower()
    risk_flags = _as_text_list(quality_context.get("risk_flags"))
    missing = _neutral_missing_evidence(signal, quality_context, analyst)
    conflicts = _as_text_list(getattr(signal, "conflicting_factors", [])) or risk_flags
    signal_text = _signal_value(getattr(signal, "signal", Signal.NEUTRAL))

    if tradeability == "low":
        bucket = "low_tradeability"
    elif conflicts:
        bucket = "conflict_avoidance"
    elif missing:
        bucket = "evidence_gap"
    elif analyst == "fundamental" and signal_text == Signal.NEUTRAL.value:
        bucket = "horizon_mismatch"
    else:
        bucket = "accountable_observation"

    raw_shadow_side = str(getattr(signal, "neutral_shadow_side", "") or "").lower()
    if raw_shadow_side in {"long", "short", "flat"}:
        shadow_side = raw_shadow_side
    else:
        shadow_side = _shadow_side_from_context(signal, quality_context, analyst)

    trigger = _first_nonempty(
        getattr(signal, "neutral_trigger_condition", ""),
        getattr(signal, "would_change_view_if", ""),
        "primary driver, short timing, and invalidation boundary align",
    )
    priority = "none"
    if bucket in {"evidence_gap", "horizon_mismatch", "accountable_observation"} and shadow_side in {"long", "short"}:
        priority = "medium"
    if bucket == "conflict_avoidance":
        priority = "low"
    if bucket == "low_tradeability":
        priority = "none"

    return {
        "bucket": bucket,
        "trigger_condition": trigger,
        "shadow_side": shadow_side,
        "watchlist_priority": priority,
        "tracking_only": True,
        "trade_permission": "none_without_current_confirmation",
        "missing_evidence": missing,
        "conflicting_factors": conflicts,
    }


def apply_business_quality_enrichment(
    signal: AnalystSignal,
    quality_context: Dict[str, Any],
    full_config: Dict[str, Any],
    analyst: str,
) -> AnalystSignal:
    """Fill deterministic business fields after the LLM and quality gate."""
    analyst_key = "commodity_news" if analyst == "company_news" else str(analyst)
    metadata = getattr(signal, "metadata", {}) or {}
    llm_cfg = (full_config or {}).get("llm", {}) or {}
    analyst_llm_cfg = (full_config or {}).get("analyst_llm", {}) or {}
    signal.llm_provider = (
        signal.llm_provider
        if str(signal.llm_provider or "").strip() and signal.llm_provider != "unknown"
        else str(llm_cfg.get("provider") or "")
    )
    signal.llm_model = signal.llm_model if str(signal.llm_model or "").strip() and signal.llm_model != "unknown" else str(
        analyst_llm_cfg.get("cloud_model") or llm_cfg.get("model") or ""
    )
    signal.analyst_horizon = signal.analyst_horizon if signal.analyst_horizon != "unknown" else signal.horizon_class
    signal.execution_horizon = signal.execution_horizon if signal.execution_horizon != "unknown" else (
        "short" if analyst_key == "technical" else "event_short" if analyst_key == "commodity_news" else "short"
    )
    signal.validation_horizon = signal.validation_horizon if signal.validation_horizon != "unknown" else signal.horizon_class
    signal.horizon_days = signal.horizon_days or signal.expected_horizon_days
    signal.template_name = infer_template_name(signal, quality_context, analyst_key)
    signal.business_quality_score = compute_business_quality_score(signal, quality_context, analyst_key)
    signal.data_coverage_score = max(
        _safe_float(getattr(signal, "data_coverage_score", 0.0), 0.0),
        _safe_float((quality_context.get("data_quality") or {}).get("coverage_ratio"), 0.0),
        _safe_float(quality_context.get("freshness_score"), 0.0),
    )
    signal.factor_alignment_score = max(
        _safe_float(getattr(signal, "factor_alignment_score", 0.0), 0.0),
        signal.business_quality_score,
    )
    signal.primary_business_driver = _first_nonempty(
        signal.primary_business_driver,
        _business_driver_from_context(quality_context, analyst_key),
    )
    signal.secondary_confirmation = _first_nonempty(
        signal.secondary_confirmation,
        _secondary_confirmation_from_context(quality_context, analyst_key),
    )
    signal.counter_evidence = _first_nonempty(
        signal.counter_evidence,
        _counter_evidence_from_context(quality_context),
    )
    signal.tradeability_reason = _first_nonempty(
        signal.tradeability_reason,
        f"tradeability={quality_context.get('tradeability', metadata.get('tradeability', 'unknown'))}; "
        f"score={signal.business_quality_score:.2f}",
    )
    signal.evidence_quality = signal.evidence_quality if signal.evidence_quality != "unknown" else (
        "high" if signal.business_quality_score >= 0.75 else "medium" if signal.business_quality_score >= 0.55 else "low"
    )
    signal.reward_risk_ratio = (
        signal.reward_risk_ratio
        if signal.reward_risk_ratio is not None
        else (1.6 if signal.business_quality_score >= 0.70 else 1.2 if signal.business_quality_score >= 0.55 else 0.8)
    )
    if analyst_key == "fundamental":
        signal.direction_anchor = signal.direction_anchor if signal.direction_anchor != "unknown" else _signal_value(signal.signal).lower()
        signal.supply_demand_state = signal.supply_demand_state if signal.supply_demand_state != "unknown" else str(quality_context.get("tradeability") or "unknown")
        if quality_context.get("basis"):
            signal.basis_state = str((quality_context.get("basis") or {}).get("status") or "unknown")
        data_quality = quality_context.get("data_quality") or {}
        freshness_score = _safe_float(data_quality.get("factor_freshness_score"), 0.0)
        stale_ratio = _safe_float(data_quality.get("stale_ratio"), 0.0)
        if stale_ratio >= 0.35 or freshness_score < 0.45:
            signal.data_freshness = "stale"
        elif stale_ratio >= 0.20 or freshness_score < 0.70:
            signal.data_freshness = "near_stale"
        else:
            signal.data_freshness = "fresh"
        signal.no_lookahead_status = str(data_quality.get("no_lookahead_status") or "ok")
    if analyst_key == "commodity_news":
        direction_counts = quality_context.get("direction_counts") or {}
        event_counts = quality_context.get("event_type_counts") or {}
        signal.event_type = signal.event_type if signal.event_type != "none" else ",".join(list(event_counts)[:3]) or "context"
        signal.impact_window_days = signal.impact_window_days or 2
        signal.requires_fundamental_confirmation = True
        signal.direction_anchor = signal.direction_anchor if signal.direction_anchor != "unknown" else str(direction_counts or "neutral")

    if _signal_value(signal.signal) == Signal.NEUTRAL.value:
        neutral_contract = _neutral_opportunity_contract(signal, quality_context, analyst_key)
        signal.neutral_reason = _first_nonempty(
            signal.neutral_reason,
            signal.do_not_trade_reason,
            _counter_evidence_from_context(quality_context),
        )
        signal.missing_evidence = _neutral_missing_evidence(signal, quality_context, analyst_key)
        signal.conflicting_factors = _as_text_list(signal.conflicting_factors) or _as_text_list(quality_context.get("risk_flags"))
        signal.would_change_view_if = _first_nonempty(
            signal.would_change_view_if,
            "primary driver and secondary confirmation align with acceptable reward/risk",
        )
        signal.opportunity_cost_risk = _first_nonempty(
            signal.opportunity_cost_risk,
            "may miss a valid move if the missing evidence appears after the pre-open cutoff",
        )
        signal.recommended_observation_window = _first_nonempty(
            signal.recommended_observation_window,
            "1-2 trading days" if analyst_key != "fundamental" else "3-5 trading days",
        )
        signal.neutral_opportunity_bucket = _first_nonempty(
            signal.neutral_opportunity_bucket,
            neutral_contract["bucket"],
        )
        signal.neutral_trigger_condition = _first_nonempty(
            signal.neutral_trigger_condition,
            neutral_contract["trigger_condition"],
        )
        signal.neutral_shadow_side = _first_nonempty(
            signal.neutral_shadow_side,
            neutral_contract["shadow_side"],
        )
        signal.neutral_watchlist_priority = _first_nonempty(
            signal.neutral_watchlist_priority,
            neutral_contract["watchlist_priority"],
        )
        signal.accountability_tag = _first_nonempty(
            signal.accountability_tag,
            f"{analyst_key}_neutral_{signal.template_name}",
        )
    else:
        neutral_contract = None

    signal.metadata = {
        **metadata,
        "business_quality": {
            "score": signal.business_quality_score,
            "primary_business_driver": signal.primary_business_driver,
            "secondary_confirmation": signal.secondary_confirmation,
            "counter_evidence": signal.counter_evidence,
            "reward_risk_ratio": signal.reward_risk_ratio,
            "factor_alignment_score": signal.factor_alignment_score,
            "data_coverage_score": signal.data_coverage_score,
            "tradeability_reason": signal.tradeability_reason,
        },
        "template_name": signal.template_name,
        "horizon_scope": {
            "analyst_horizon": signal.analyst_horizon,
            "decision_horizon": signal.decision_horizon,
            "execution_horizon": signal.execution_horizon,
            "validation_horizon": signal.validation_horizon,
        },
    }
    if neutral_contract:
        signal.metadata["neutral_opportunity_contract"] = {
            **neutral_contract,
            "bucket": signal.neutral_opportunity_bucket,
            "trigger_condition": signal.neutral_trigger_condition,
            "shadow_side": signal.neutral_shadow_side,
            "watchlist_priority": signal.neutral_watchlist_priority,
            "observation_window": signal.recommended_observation_window,
            "opportunity_cost_risk": signal.opportunity_cost_risk,
        }
    return signal


def summarize_business_quality(signals: Iterable[AnalystSignal]) -> Dict[str, Any]:
    rows = []
    for signal in signals:
        rows.append(
            {
                "agent_name": getattr(signal, "agent_name", ""),
                "signal": _signal_value(getattr(signal, "signal", "Neutral")),
                "business_quality_score": _safe_float(getattr(signal, "business_quality_score", 0.0), 0.0),
                "template_name": getattr(signal, "template_name", "unknown"),
                "horizon_class": getattr(signal, "horizon_class", "unknown"),
                "analyst_horizon": getattr(signal, "analyst_horizon", "unknown"),
                "tradeability_reason": getattr(signal, "tradeability_reason", ""),
                "neutral_reason": getattr(signal, "neutral_reason", ""),
                "neutral_opportunity_bucket": getattr(signal, "neutral_opportunity_bucket", "unknown"),
                "neutral_trigger_condition": getattr(signal, "neutral_trigger_condition", ""),
                "neutral_shadow_side": getattr(signal, "neutral_shadow_side", "flat"),
                "neutral_watchlist_priority": getattr(signal, "neutral_watchlist_priority", "none"),
            }
        )
    directional = [row for row in rows if row["signal"] in {"Bullish", "Bearish"}]
    avg_quality = sum(row["business_quality_score"] for row in rows) / len(rows) if rows else 0.0
    max_directional_quality = max((row["business_quality_score"] for row in directional), default=0.0)
    return {
        "rows": rows,
        "avg_business_quality_score": avg_quality,
        "max_directional_business_quality_score": max_directional_quality,
        "directional_count": len(directional),
    }
