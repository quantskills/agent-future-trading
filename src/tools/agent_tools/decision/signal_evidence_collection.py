"""Deterministic signal collection for the decision team.

The signal collector is not an analyst and does not call an LLM.  It preserves
the structured evidence produced by analysts and emits a single
``signal_collection_contract`` for PM consumption.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping


ANALYST_ORDER = ("technical", "fundamental", "commodity_news")


def _agent_name(signal: Any) -> str:
    name = str(getattr(signal, "agent_name", "") or "").strip()
    return "commodity_news" if name == "company_news" else name


def _metadata(signal: Any) -> dict:
    value = getattr(signal, "metadata", {}) or {}
    return dict(value) if isinstance(value, Mapping) else {}


def _action_contract(signal: Any) -> dict:
    metadata = _metadata(signal)
    value = metadata.get("action_evidence_contract")
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "ok"}
    return bool(value)


def _list(value: Any) -> list:
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


def _side_from_signal(signal: Any, contract: Mapping[str, Any]) -> str:
    side = _text(contract.get("side")).lower()
    if side in {"long", "short", "flat"}:
        return side
    raw_signal = _text(getattr(signal, "signal", "")).lower()
    if "bull" in raw_signal:
        return "long"
    if "bear" in raw_signal:
        return "short"
    return "flat"


def _confidence(signal: Any, contract: Mapping[str, Any]) -> float:
    value = contract.get("confidence", getattr(signal, "confidence", 0.0))
    try:
        parsed = float(value if value is not None else 0.0)
    except (TypeError, ValueError):
        parsed = 0.0
    return max(0.0, min(1.0, parsed))


def _evidence_quality_score(value: Any) -> float:
    text = _text(value, "unknown").lower()
    if text in {"high", "strong", "good"}:
        return 1.0
    if text in {"medium", "acceptable", "ok"}:
        return 0.6
    if text in {"low", "weak", "poor"}:
        return 0.25
    return 0.4


def _quality_label(score: float) -> str:
    if score >= 0.72:
        return "high"
    if score >= 0.45:
        return "medium"
    if score > 0:
        return "low"
    return "unknown"


def build_signal_collection_contract(
    *,
    ticker: str,
    trading_date: Any,
    analyst_signals: Iterable[Any],
    enabled_analysts: Iterable[str] | None = None,
) -> dict:
    """Build the PM-facing signal collection contract.

    The result is evidence only.  It never contains lots, rank, action, or
    position authority.
    """
    enabled = [
        "commodity_news" if str(name) == "company_news" else str(name)
        for name in (enabled_analysts or ANALYST_ORDER)
    ]
    evidence_items: list[dict] = []
    source_contracts: list[dict] = []
    missing_evidence: list[str] = []
    data_quality_flags: list[str] = []
    invalidation_summary: list[dict] = []
    setup_types: list[str] = []
    horizons: list[str] = []
    side_counts: Counter[str] = Counter()
    side_confidence: Counter[str] = Counter()
    trigger_states: Counter[str] = Counter()

    seen_agents: set[str] = set()
    for signal in analyst_signals or []:
        agent = _agent_name(signal)
        if not agent:
            agent = "unknown"
        seen_agents.add(agent)
        contract = _action_contract(signal)
        side = _side_from_signal(signal, contract)
        confidence = _confidence(signal, contract)
        trigger_valid = _bool(contract.get("trigger_valid", getattr(signal, "trigger_valid", False)))
        trigger_confirmed = _bool(
            contract.get("current_trigger_confirmed", getattr(signal, "current_trigger_confirmed", False))
        )
        trigger_status = (
            "confirmed"
            if trigger_valid and trigger_confirmed
            else "valid_unconfirmed"
            if trigger_valid
            else "watch_for_trigger"
        )
        side_counts[side] += 1
        side_confidence[side] += confidence
        trigger_states[trigger_status] += 1

        setup_type = _text(contract.get("setup_type", getattr(signal, "setup_type", "")), "unknown")
        if setup_type and setup_type != "unknown":
            setup_types.append(setup_type)
        horizon = _text(contract.get("horizon_class", getattr(signal, "horizon_class", "")), "unknown")
        if horizon and horizon != "unknown":
            horizons.append(horizon)

        missing_evidence.extend(str(item) for item in _list(contract.get("missing_evidence", getattr(signal, "missing_evidence", []))) if str(item))
        data_usage = contract.get("data_usage_summary") or _metadata(signal).get("data_usage_summary") or {}
        if isinstance(data_usage, Mapping):
            for key in ("data_quality_flags", "risk_flags", "missing_data", "stale_data"):
                data_quality_flags.extend(str(item) for item in _list(data_usage.get(key)) if str(item))
        no_lookahead_status = _text(contract.get("no_lookahead_status", getattr(signal, "no_lookahead_status", "")), "unchecked")
        if no_lookahead_status not in {"ok", "pass", "clean"}:
            data_quality_flags.append(f"no_lookahead_status:{no_lookahead_status}")

        if _bool(contract.get("invalidation_present", getattr(signal, "invalidation_present", False))):
            invalidation_summary.append({
                "analyst": agent,
                "condition": _text(contract.get("invalidation_condition", getattr(signal, "invalidation_condition", ""))),
                "level": contract.get("invalidation_level", getattr(signal, "invalidation_level", None)),
            })

        item = {
            "analyst": agent,
            "side": side,
            "confidence": confidence,
            "signal": _text(contract.get("signal", getattr(signal, "signal", ""))),
            "opportunity_state": _text(contract.get("opportunity_state", getattr(signal, "opportunity_state", "")), "unknown"),
            "trigger_valid": trigger_valid,
            "current_trigger_confirmed": trigger_confirmed,
            "trigger_status": trigger_status,
            "entry_trigger": _text(contract.get("entry_trigger", getattr(signal, "entry_trigger", ""))),
            "setup_type": setup_type,
            "setup_quality_ok": _bool(contract.get("setup_quality_ok", getattr(signal, "setup_quality_ok", False))),
            "horizon_class": horizon,
            "market_regime": _text(contract.get("market_regime", getattr(signal, "market_regime", "")), "unknown"),
            "evidence_quality": _text(contract.get("evidence_quality", getattr(signal, "evidence_quality", "")), "unknown"),
            "current_evidence_conflict": _list(contract.get("current_evidence_conflict", getattr(signal, "current_evidence_conflict", []))),
            "missing_evidence": _list(contract.get("missing_evidence", getattr(signal, "missing_evidence", []))),
            "source_contract_index": len(source_contracts),
        }
        evidence_items.append(item)
        source_contracts.append({
            "analyst": agent,
            "action_evidence_contract": contract,
            "signal_record_id": _metadata(signal).get("signal_record_id"),
        })

    missing_agents = [name for name in enabled if name not in seen_agents]
    missing_evidence.extend(f"missing_analyst:{name}" for name in missing_agents)

    directional_sides = [side for side in ("long", "short") if side_counts.get(side)]
    if directional_sides:
        dominant_side = max(directional_sides, key=lambda side: (side_counts[side], side_confidence[side], side))
    else:
        dominant_side = "flat"
    supporting = [item["analyst"] for item in evidence_items if item.get("side") == dominant_side and dominant_side != "flat"]
    opposing_side = "short" if dominant_side == "long" else "long" if dominant_side == "short" else ""
    opposing = [item["analyst"] for item in evidence_items if item.get("side") == opposing_side]
    neutral = [item["analyst"] for item in evidence_items if item.get("side") == "flat"]

    if dominant_side == "flat":
        consensus = "no_direction"
    elif opposing:
        consensus = "conflicted"
    elif len(set(supporting)) >= 2:
        consensus = "multi_analyst_support"
    else:
        consensus = "single_analyst_support"

    strength_scores = [
        float(item.get("confidence") or 0.0) * _evidence_quality_score(item.get("evidence_quality"))
        for item in evidence_items
        if item.get("side") == dominant_side and dominant_side != "flat"
    ]
    strength = _quality_label(sum(strength_scores) / len(strength_scores)) if strength_scores else "unknown"
    conflict_level = "high" if len(opposing) >= 2 else "medium" if opposing else "low"
    aggregate_trigger = (
        "confirmed"
        if trigger_states.get("confirmed")
        else "valid_unconfirmed"
        if trigger_states.get("valid_unconfirmed")
        else "watch_for_trigger"
    )

    return {
        "contract_version": "agentquant.signal_collection.v1",
        "ticker": ticker,
        "trading_date": str(trading_date),
        "source_contracts": source_contracts,
        "evidence_items": evidence_items,
        "dominant_side": dominant_side,
        "side_consensus": consensus,
        "trigger_status": aggregate_trigger,
        "supporting_analysts": sorted(set(supporting)),
        "opposing_analysts": sorted(set(opposing)),
        "neutral_analysts": sorted(set(neutral)),
        "evidence_strength": strength,
        "evidence_conflict_level": conflict_level,
        "missing_evidence": sorted(set(missing_evidence)),
        "data_quality_flags": sorted(set(data_quality_flags)),
        "setup_types": sorted(set(setup_types)),
        "horizon_scope": sorted(set(horizons)),
        "invalidation_summary": invalidation_summary,
        "collector_decision_boundary": "no_trade_authority",
        "no_trade_authority": True,
    }

