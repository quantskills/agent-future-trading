"""Deterministic multi-evidence fusion semantics.

This module interprets analyst evidence for collection, PM scoring, review, and
governance. It does not call LLMs, sign contracts, size positions, place orders,
write accounting facts, or write research memory.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping


FUSION_SEMANTICS_VERSION = "agentquant.evidence_fusion.v1"


def _text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _lower(value: Any, default: str = "") -> str:
    return _text(value, default).lower()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [value]


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value or 0.0)))


def _quality_score(value: Any) -> float:
    text = _lower(value, "unknown")
    if text in {"high", "strong", "good", "deployable"}:
        return 1.0
    if text in {"medium", "acceptable", "ok", "normal"}:
        return 0.62
    if text in {"low", "weak", "poor"}:
        return 0.25
    return 0.42


def _strength_label(score: float) -> str:
    if score >= 0.78:
        return "strong"
    if score >= 0.58:
        return "medium"
    if score > 0:
        return "weak"
    return "unknown"


def _freshness_label(score: float) -> str:
    if score >= 0.78:
        return "fresh"
    if score >= 0.50:
        return "usable"
    if score > 0:
        return "stale"
    return "unknown"


def _side(value: Any) -> str:
    text = _lower(value)
    if text in {"long", "bullish", "buy"}:
        return "long"
    if text in {"short", "bearish", "sell"}:
        return "short"
    return "flat"


def _contract_from_signal(signal: Any) -> dict:
    metadata = getattr(signal, "metadata", None)
    if isinstance(metadata, Mapping):
        contract = metadata.get("action_evidence_contract")
        if isinstance(contract, Mapping):
            return dict(contract)
    return {}


def _value(signal: Any, contract: Mapping[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(contract, Mapping) and key in contract:
        return contract.get(key)
    if hasattr(signal, key):
        return getattr(signal, key)
    return default


def _formal_freshness_score(
    signal: Any,
    contract: Mapping[str, Any],
    context: Mapping[str, Any],
) -> float:
    data_usage = contract.get("data_usage_summary") if isinstance(contract.get("data_usage_summary"), Mapping) else {}
    metadata = getattr(signal, "metadata", None)
    if not data_usage and isinstance(metadata, Mapping) and isinstance(metadata.get("data_usage_summary"), Mapping):
        data_usage = metadata.get("data_usage_summary") or {}
    data_quality = context.get("data_quality") if isinstance(context.get("data_quality"), Mapping) else {}

    candidates: list[float] = []
    for container, key in (
        (context, "freshness_score"),
        (data_usage, "freshness_score"),
        (data_quality, "freshness_score"),
        (data_quality, "factor_freshness_score"),
    ):
        if key in container and container.get(key) is not None:
            candidates.append(_clip(_safe_float(container.get(key), 0.0)))
    sources = data_usage.get("sources") if isinstance(data_usage.get("sources"), Mapping) else {}
    for source in sources.values():
        if isinstance(source, Mapping) and "freshness_score" in source and source.get("freshness_score") is not None:
            candidates.append(_clip(_safe_float(source.get("freshness_score"), 0.0)))
    if candidates:
        return max(candidates)

    explicit_statuses = {
        _lower(_value(signal, contract, "data_freshness", "")),
        _lower(context.get("data_freshness")),
        _lower(data_quality.get("data_freshness")),
    }
    if "missing" in explicit_statuses:
        return 0.0
    stale_facts = (
        _as_list(data_usage.get("stale_data"))
        + _as_list(context.get("stale_data"))
        + _as_list(data_quality.get("stale_data"))
    )
    if "stale" in explicit_statuses or stale_facts:
        return 0.35
    return 0.68


def build_analyst_fusion_evidence(
    signal: Any,
    quality_context: Mapping[str, Any] | None,
    *,
    analyst: str,
    ticker: str = "",
) -> dict[str, Any]:
    """Build per-analyst evidence fusion fields for action_evidence_contract."""
    context = quality_context if isinstance(quality_context, Mapping) else {}
    contract = _contract_from_signal(signal)
    context_contract = context.get("action_evidence_contract")
    if isinstance(context_contract, Mapping):
        contract = {**dict(context_contract), **contract}
    confidence = _clip(_safe_float(_value(signal, contract, "confidence", 0.0), 0.0))
    business_quality = _clip(_safe_float(getattr(signal, "business_quality_score", 0.0), 0.0))
    setup_quality = _clip(_safe_float(getattr(signal, "setup_quality_score", 0.0), 0.0))
    evidence_quality = _quality_score(_value(signal, contract, "evidence_quality", "unknown"))
    freshness_score = _formal_freshness_score(signal, contract, context)

    conflicts = [
        str(item)
        for item in (
            _as_list(_value(signal, contract, "current_evidence_conflict", []))
            + _as_list(context.get("risk_flags"))
        )
        if str(item)
    ]
    missing = [
        str(item)
        for item in (
            _as_list(_value(signal, contract, "missing_evidence", []))
            + _as_list(context.get("missing_evidence"))
        )
        if str(item)
    ]
    confirmation_requirements = [
        str(item)
        for item in _as_list(contract.get("confirmation_requirements") or context.get("confirmation_requirements"))
        if str(item)
    ]
    required_confirmation = context.get("required_confirmation")
    if required_confirmation and str(required_confirmation) not in confirmation_requirements:
        confirmation_requirements.append(str(required_confirmation))

    strength_score = _clip(
        0.34 * confidence
        + 0.24 * business_quality
        + 0.20 * max(setup_quality, evidence_quality)
        + 0.14 * freshness_score
        - min(0.24, 0.04 * len(conflicts))
        - min(0.18, 0.035 * len(missing))
    )
    analyst_name = str(analyst or "")

    technical_false_breakout_risk = "not_applicable"
    fundamental_opposition_strength = "not_applicable"
    news_impact_window = ""
    one_off_event_risk = "not_applicable"
    if analyst_name == "technical":
        notes = " ".join(str(item) for item in _as_list(getattr(signal, "setup_quality_notes", [])))
        risk_text = " ".join(conflicts + _as_list(context.get("risk_flags")) + [notes]).lower()
        technical_false_breakout_risk = (
            "high"
            if "false_breakout" in risk_text or "choppy" in risk_text or "range" in risk_text
            else "medium"
            if "high_volatility" in risk_text or "weak_trend" in risk_text
            else "low"
        )
        if technical_false_breakout_risk in {"high", "medium"}:
            confirmation_requirements.append("price_volume_follow_through_confirmation")
    elif analyst_name == "fundamental":
        risk_text = " ".join(conflicts + missing).lower()
        fundamental_opposition_strength = (
            "strong"
            if "contradict" in risk_text or "conflict" in risk_text or "stale" in risk_text
            else "medium"
            if missing or conflicts
            else "low"
        )
        confirmation_requirements.append("price_or_technical_timing_confirmation")
    elif analyst_name == "commodity_news":
        news_impact_window = _text(
            context.get("impact_window")
            or context.get("event_window")
            or contract.get("impact_window")
            or "event_short"
        )
        risk_text = " ".join(conflicts + _as_list(context.get("risk_flags"))).lower()
        one_off_event_risk = "high" if "one_off" in risk_text or "rumor" in risk_text else "medium"
        confirmation_requirements.append("price_reaction_and_fresh_catalyst_confirmation")

    confirmation_requirements = sorted(set(item for item in confirmation_requirements if item))
    return {
        "contract_version": FUSION_SEMANTICS_VERSION,
        "ticker": str(ticker or ""),
        "analyst": analyst_name,
        "evidence_strength": _strength_label(strength_score),
        "evidence_strength_score": round(strength_score, 4),
        "evidence_freshness": _freshness_label(freshness_score),
        "evidence_freshness_score": round(_clip(freshness_score), 4),
        "evidence_decay_risk": "high" if freshness_score < 0.45 else "medium" if freshness_score < 0.70 else "low",
        "confirmation_requirements": confirmation_requirements,
        "missing_evidence": sorted(set(missing))[:12],
        "current_evidence_conflict": sorted(set(conflicts))[:12],
        "technical_false_breakout_risk": technical_false_breakout_risk,
        "fundamental_opposition_strength": fundamental_opposition_strength,
        "news_impact_window": news_impact_window,
        "one_off_event_risk": one_off_event_risk,
        "fusion_boundary": "analyst_evidence_only_no_trade_authority",
    }


def build_signal_collection_fusion_summary(
    evidence_items: Iterable[Mapping[str, Any]],
    *,
    dominant_side: str = "flat",
) -> dict[str, Any]:
    """Summarize analyst evidence for PM without creating score/rank/lots."""
    items = [dict(item) for item in evidence_items or [] if isinstance(item, Mapping)]
    strength_by_analyst: dict[str, str] = {}
    freshness_by_analyst: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    missing: list[str] = []
    confirmation_requirements: list[str] = []
    dominant_opposing: list[dict[str, Any]] = []
    side_counts: Counter[str] = Counter()
    side_strength: Counter[str] = Counter()
    for item in items:
        analyst = _text(item.get("analyst"), "unknown")
        side = _side(item.get("side"))
        side_counts[side] += 1
        fusion = item.get("fusion_evidence") if isinstance(item.get("fusion_evidence"), Mapping) else {}
        strength = _text(fusion.get("evidence_strength") or item.get("evidence_quality"), "unknown")
        freshness = _text(fusion.get("evidence_freshness"), "unknown")
        strength_by_analyst[analyst] = strength
        freshness_by_analyst[analyst] = freshness
        side_strength[side] += _safe_float(fusion.get("evidence_strength_score"), 0.0)
        item_conflicts = _as_list(fusion.get("current_evidence_conflict") or item.get("current_evidence_conflict"))
        if item_conflicts:
            conflicts.append({"analyst": analyst, "side": side, "conflicts": [str(v) for v in item_conflicts if str(v)]})
        missing.extend(str(v) for v in _as_list(fusion.get("missing_evidence") or item.get("missing_evidence")) if str(v))
        confirmation_requirements.extend(
            str(v) for v in _as_list(fusion.get("confirmation_requirements")) if str(v)
        )
        if dominant_side in {"long", "short"} and side not in {dominant_side, "flat"}:
            dominant_opposing.append(
                {
                    "analyst": analyst,
                    "side": side,
                    "strength": strength,
                    "freshness": freshness,
                    "conflicts": [str(v) for v in item_conflicts if str(v)],
                }
            )
    directional = [side for side in ("long", "short") if side_counts.get(side)]
    if not directional:
        alignment_state = "no_direction"
    elif side_counts.get("long") and side_counts.get("short"):
        alignment_state = "conflicted"
    elif sum(1 for item in items if _side(item.get("side")) in {"long", "short"}) >= 2:
        alignment_state = "aligned"
    else:
        alignment_state = "single_source"
    consensus_score = 0.0
    if dominant_side in {"long", "short"}:
        support = side_counts.get(dominant_side, 0)
        oppose = side_counts.get("short" if dominant_side == "long" else "long", 0)
        raw = (support + side_strength.get(dominant_side, 0.0)) / max(1.0, len(items) + support + oppose)
        consensus_score = _clip(raw - 0.18 * oppose)
    return {
        "contract_version": FUSION_SEMANTICS_VERSION,
        "evidence_strength_by_analyst": strength_by_analyst,
        "evidence_freshness_by_analyst": freshness_by_analyst,
        "evidence_alignment_state": alignment_state,
        "cross_analyst_conflicts": conflicts,
        "dominant_opposing_evidence": dominant_opposing,
        "confirmation_requirements": sorted(set(confirmation_requirements)),
        "missing_evidence": sorted(set(missing)),
        "multi_evidence_consensus_score": round(consensus_score, 4),
        "fusion_boundary": "signal_collection_only_no_score_no_rank_no_trade_authority",
    }


def build_pm_fusion_diagnostics(signal_collection_contract: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build a PM-facing fusion diagnostic from signal_collection_contract."""
    contract = signal_collection_contract if isinstance(signal_collection_contract, Mapping) else {}
    fusion = contract.get("evidence_fusion") if isinstance(contract.get("evidence_fusion"), Mapping) else {}
    conflicts = fusion.get("cross_analyst_conflicts") or []
    opposing = fusion.get("dominant_opposing_evidence") or []
    requirements = fusion.get("confirmation_requirements") or []
    missing = fusion.get("missing_evidence") or []
    consensus = _safe_float(fusion.get("multi_evidence_consensus_score"), 0.0)
    conflict_count = len(_as_list(conflicts)) + len(_as_list(opposing))
    missing_count = len(_as_list(missing))
    requirement_count = len(_as_list(requirements))
    fusion_penalty = min(0.22, 0.04 * conflict_count + 0.025 * missing_count)
    confirmation_bonus = min(0.08, 0.015 * requirement_count) if consensus >= 0.45 else 0.0
    return {
        "contract_version": FUSION_SEMANTICS_VERSION,
        "pm_fusion_diagnostics": True,
        "evidence_alignment_state": fusion.get("evidence_alignment_state") or contract.get("side_consensus"),
        "multi_evidence_consensus_score": round(consensus, 4),
        "cross_analyst_conflict_count": conflict_count,
        "dominant_opposing_evidence_count": len(_as_list(opposing)),
        "missing_evidence_count": missing_count,
        "confirmation_requirement_count": requirement_count,
        "fusion_score_adjustment": round(confirmation_bonus - fusion_penalty, 4),
        "requires_pm_conflict_resolution": bool(conflict_count or opposing),
        "requires_pm_confirmation_explanation": bool(requirements),
        "no_trade_authority": True,
    }


def audit_pm_fusion_explanation(final_action_contract: Mapping[str, Any] | None) -> dict[str, Any]:
    """Audit whether PM preserved fusion diagnostics inside its contract."""
    contract = final_action_contract if isinstance(final_action_contract, Mapping) else {}
    evidence = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), Mapping) else {}
    diagnostics = evidence.get("pm_fusion_diagnostics") if isinstance(evidence.get("pm_fusion_diagnostics"), Mapping) else {}
    conflict_resolution = evidence.get("pm_conflict_resolution") if isinstance(evidence.get("pm_conflict_resolution"), Mapping) else {}
    errors: list[str] = []
    warnings: list[str] = []
    if evidence and not diagnostics:
        warnings.append("pm_fusion_diagnostics_missing")
    if diagnostics.get("requires_pm_conflict_resolution") and not conflict_resolution.get("handled"):
        errors.append("pm_fusion_conflict_resolution_missing")
    if diagnostics.get("requires_pm_confirmation_explanation") and not conflict_resolution.get("confirmation_requirements_addressed"):
        warnings.append("pm_fusion_confirmation_explanation_missing")
    return {
        "contract_version": FUSION_SEMANTICS_VERSION,
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "pm_fusion_diagnostics": diagnostics,
        "pm_conflict_resolution": conflict_resolution,
        "auditor_boundary": "audit_pm_contract_fusion_explanation_only_no_re_fusion_no_lot_change",
    }


def build_reviewer_fusion_attribution(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build read-only reviewer attribution labels for fusion learning."""
    source = snapshot if isinstance(snapshot, Mapping) else {}
    contract = source.get("final_action_contract") if isinstance(source.get("final_action_contract"), Mapping) else {}
    if not contract and isinstance(source.get("signal_snapshot"), Mapping):
        nested = source.get("signal_snapshot") or {}
        contract = nested.get("final_action_contract") if isinstance(nested.get("final_action_contract"), Mapping) else {}
    evidence = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), Mapping) else {}
    diagnostics = evidence.get("pm_fusion_diagnostics") if isinstance(evidence.get("pm_fusion_diagnostics"), Mapping) else {}
    conflict_resolution = evidence.get("pm_conflict_resolution") if isinstance(evidence.get("pm_conflict_resolution"), Mapping) else {}
    if not diagnostics:
        label = "fusion_not_recorded"
    elif diagnostics.get("cross_analyst_conflict_count", 0) and conflict_resolution.get("handled"):
        label = "fusion_conflict_handled"
    elif diagnostics.get("cross_analyst_conflict_count", 0):
        label = "fusion_conflict_unresolved"
    elif _safe_float(diagnostics.get("multi_evidence_consensus_score"), 0.0) >= 0.58:
        label = "multi_evidence_consensus_supported"
    else:
        label = "fusion_neutral_or_single_source"
    return {
        "contract_version": FUSION_SEMANTICS_VERSION,
        "fusion_attribution_label": label,
        "pm_fusion_diagnostics": diagnostics,
        "pm_conflict_resolution": conflict_resolution,
        "researcher_learning_hint": "future_fusion_learning_context",
        "reviewer_boundary": "read_only_attribution_no_action_value_write_no_trade_mutation",
    }
