"""Deterministic signal collection for the decision team.

The signal collector is not an analyst and does not call an LLM.  It preserves
the structured evidence produced by analysts and emits a single
``signal_collection_contract`` for PM consumption.
"""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from graph.constants import Signal
from tools.common.evidence_fusion_semantics import build_signal_collection_fusion_summary
from tools.common.final_action_semantics import FORBIDDEN_ANALYST_TRADE_AUTHORITY_KEYS


ANALYST_ORDER = ("technical", "fundamental", "commodity_news")
SCC_CONTRACT_VERSION = "agentquant.signal_collection.v1"
ACTION_EVIDENCE_CONTRACT_VERSION = "agentquant.action_evidence.v1"

SCC_ALLOWED_TOP_LEVEL_FIELDS = {
    "contract_version",
    "source_agent",
    "ticker",
    "trading_date",
    "source_contracts",
    "evidence_items",
    "dominant_side",
    "side_consensus",
    "trigger_status",
    "supporting_analysts",
    "opposing_analysts",
    "neutral_analysts",
    "evidence_strength",
    "evidence_conflict_level",
    "confirmation_requirements",
    "missing_evidence",
    "data_quality_flags",
    "setup_types",
    "horizon_scope",
    "invalidation_summary",
    "evidence_fusion",
    "collector_decision_boundary",
}
SCC_SOURCE_CONTRACT_FIELDS = {
    "analyst",
    "action_evidence_contract",
    "product_profile_evidence",
    "fusion_evidence",
    "signal_record_id",
}
SCC_EVIDENCE_ITEM_FIELDS = {
    "analyst",
    "side",
    "confidence",
    "signal",
    "opportunity_state",
    "trigger_valid",
    "current_trigger_confirmed",
    "trigger_status",
    "entry_trigger",
    "setup_type",
    "setup_quality_ok",
    "horizon_class",
    "market_regime",
    "evidence_quality",
    "current_evidence_conflict",
    "missing_evidence",
    "fusion_evidence",
    "evidence_strength",
    "evidence_freshness",
    "confirmation_requirements",
    "product_profile_id",
    "product_profile_used",
    "product_profile_analysis_boundary",
}
SCC_EVIDENCE_FUSION_FIELDS = {
    "contract_version",
    "evidence_strength_by_analyst",
    "evidence_freshness_by_analyst",
    "evidence_alignment_state",
    "direction_alignment",
    "cross_analyst_conflicts",
    "dominant_opposing_evidence",
    "confirmation_requirements",
    "missing_evidence",
    "multi_evidence_consensus_score",
    "fusion_boundary",
}

SCC_FORBIDDEN_TRADE_FIELDS = FORBIDDEN_ANALYST_TRADE_AUTHORITY_KEYS | {
    "opportunity_score",
    "rank_score",
    "position_sizing_result",
    "capital_deployment",
    "pm_six_step_trace",
}


class _SCCEvidenceSignal(SimpleNamespace):
    """Read-only-shaped PM view reconstructed only from formal SCC evidence."""

    def model_dump(self) -> dict:
        return dict(vars(self))


def _agent_name(signal: Any) -> str:
    return str(getattr(signal, "agent_name", "") or "").strip()


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


def _side_from_contract(contract: Mapping[str, Any]) -> str:
    side = _text(contract.get("side")).lower()
    if side in {"long", "short", "flat"}:
        return side
    raise ValueError(f"signal_collection_invalid_action_evidence_side:{side or 'missing'}")


def _confidence(contract: Mapping[str, Any]) -> float:
    value = contract.get("confidence", 0.0)
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


def _nested_forbidden_paths(value: Any, *, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if key_text in SCC_FORBIDDEN_TRADE_FIELDS:
                hits.append(child_path)
            hits.extend(_nested_forbidden_paths(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            hits.extend(_nested_forbidden_paths(child, path=child_path))
    return hits


def _source_analyst_names(contract: Mapping[str, Any]) -> list[str]:
    return [
        _text(row.get("analyst"))
        for row in contract.get("source_contracts") or []
        if isinstance(row, Mapping)
    ]


def validate_signal_collection_contract(
    contract: Any,
    *,
    ticker: str | None = None,
    trading_date: Any = None,
    enabled_analysts: Iterable[str] | None = None,
    analyst_signals: Iterable[Any] | None = None,
) -> dict:
    """Validate the one SCC contract at producer and PM-consumer boundaries."""
    if not isinstance(contract, dict) or not contract:
        raise ValueError("signal_collection_contract_missing")
    forbidden = _nested_forbidden_paths(contract)
    if forbidden:
        raise ValueError(f"signal_collection_forbidden_trade_field:{','.join(forbidden)}")
    extras = sorted(set(contract) - SCC_ALLOWED_TOP_LEVEL_FIELDS)
    if extras:
        raise ValueError(f"signal_collection_unregistered_top_level_field:{','.join(extras)}")
    if contract.get("contract_version") != SCC_CONTRACT_VERSION:
        raise ValueError("signal_collection_invalid_contract_version")
    if _text(contract.get("source_agent")) != "signal_collector":
        raise ValueError("signal_collection_invalid_source_agent")
    if _text(contract.get("collector_decision_boundary")) != "no_trade_authority":
        raise ValueError("signal_collection_invalid_decision_boundary")
    if ticker is not None and _text(contract.get("ticker")).upper() != _text(ticker).upper():
        raise ValueError("signal_collection_ticker_mismatch")
    if trading_date is not None and _text(contract.get("trading_date"))[:10] != _text(trading_date)[:10]:
        raise ValueError("signal_collection_trading_date_mismatch")
    source_contracts = contract.get("source_contracts")
    evidence_items = contract.get("evidence_items")
    if not isinstance(source_contracts, list) or not source_contracts:
        raise ValueError("signal_collection_missing_source_contracts")
    if not isinstance(evidence_items, list) or not evidence_items:
        raise ValueError("signal_collection_missing_evidence_items")
    for source in source_contracts:
        if not isinstance(source, dict):
            raise ValueError("signal_collection_invalid_source_contract")
        source_extras = sorted(set(source) - SCC_SOURCE_CONTRACT_FIELDS)
        if source_extras:
            raise ValueError(
                f"signal_collection_unregistered_source_contract_field:{','.join(source_extras)}"
            )
    for item in evidence_items:
        if not isinstance(item, dict):
            raise ValueError("signal_collection_invalid_evidence_item")
        item_extras = sorted(set(item) - SCC_EVIDENCE_ITEM_FIELDS)
        if item_extras:
            raise ValueError(
                f"signal_collection_unregistered_evidence_item_field:{','.join(item_extras)}"
            )
    source_names = _source_analyst_names(contract)
    if any(not name for name in source_names):
        raise ValueError("signal_collection_missing_source_analyst")
    duplicates = sorted(name for name, count in Counter(source_names).items() if count > 1)
    if duplicates:
        raise ValueError(f"signal_collection_duplicate_analyst:{','.join(duplicates)}")
    item_names = [
        _text(row.get("analyst"))
        for row in evidence_items
        if isinstance(row, Mapping)
    ]
    if item_names != source_names:
        raise ValueError("signal_collection_evidence_source_order_mismatch")
    expected = [_text(name) for name in (enabled_analysts or []) if _text(name)]
    unexpected = sorted(set(source_names) - set(expected)) if expected else []
    if unexpected:
        raise ValueError(f"signal_collection_unexpected_analyst:{','.join(unexpected)}")
    missing = sorted(set(expected) - set(source_names)) if expected else []
    missing_evidence = {_text(value) for value in contract.get("missing_evidence") or []}
    for name in missing:
        if f"missing_analyst:{name}" not in missing_evidence:
            raise ValueError(f"signal_collection_missing_analyst_not_recorded:{name}")
    fusion = contract.get("evidence_fusion")
    if not isinstance(fusion, dict):
        raise ValueError("signal_collection_missing_evidence_fusion")
    fusion_extras = sorted(set(fusion) - SCC_EVIDENCE_FUSION_FIELDS)
    if fusion_extras:
        raise ValueError(
            f"signal_collection_unregistered_evidence_fusion_field:{','.join(fusion_extras)}"
        )
    for source in source_contracts:
        source_name = _text(source.get("analyst")) if isinstance(source, Mapping) else ""
        action_contract = source.get("action_evidence_contract") if isinstance(source, Mapping) else None
        if not isinstance(action_contract, dict) or not action_contract:
            raise ValueError("signal_collection_missing_action_evidence_contract")
        if action_contract.get("contract_version") != ACTION_EVIDENCE_CONTRACT_VERSION:
            raise ValueError("signal_collection_invalid_action_evidence_contract_version")
        if _text(action_contract.get("analyst")) != source_name:
            raise ValueError(f"signal_collection_action_contract_analyst_mismatch:{source_name}")
    if analyst_signals is not None:
        raw_by_agent: dict[str, Any] = {}
        for signal in analyst_signals or []:
            name = _agent_name(signal)
            if name in raw_by_agent:
                raise ValueError(f"signal_collection_duplicate_analyst:{name}")
            raw_by_agent[name] = signal
        if set(raw_by_agent) != set(source_names):
            raise ValueError("signal_collection_source_lineage_mismatch")
        for source in source_contracts:
            name = _text(source.get("analyst"))
            raw = raw_by_agent[name]
            if _action_contract(raw) != source.get("action_evidence_contract"):
                raise ValueError(f"signal_collection_action_contract_lineage_mismatch:{name}")
            raw_id = _metadata(raw).get("signal_record_id")
            source_id = source.get("signal_record_id")
            if raw_id not in (None, "") and source_id != raw_id:
                raise ValueError(f"signal_collection_record_id_lineage_mismatch:{name}")
    return contract


def build_pm_evidence_signals_from_scc(contract: Mapping[str, Any]) -> list[Any]:
    """Build PM's internal evidence view solely from the already validated SCC."""
    validate_signal_collection_contract(dict(contract))
    evidence_signals: list[Any] = []
    for source in contract.get("source_contracts") or []:
        analyst = _text(source.get("analyst"))
        action_contract = dict(source.get("action_evidence_contract") or {})
        side = _side_from_contract(action_contract)
        payload = dict(action_contract)
        payload["agent_name"] = analyst
        payload["signal"] = (
            Signal.BULLISH
            if side == "long"
            else Signal.BEARISH
            if side == "short"
            else Signal.NEUTRAL
        )
        payload["metadata"] = {
            "action_evidence_contract": action_contract,
            "product_profile_evidence": dict(source.get("product_profile_evidence") or {}),
            "fusion_evidence": dict(source.get("fusion_evidence") or {}),
            "signal_record_id": source.get("signal_record_id"),
            "data_usage_summary": dict(action_contract.get("data_usage_summary") or {}),
        }
        evidence_signals.append(_SCCEvidenceSignal(**payload))
    return evidence_signals


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
    analyst_signals = list(analyst_signals or [])
    enabled = [_text(name) for name in (enabled_analysts or ANALYST_ORDER) if _text(name)]
    duplicate_enabled = sorted(name for name, count in Counter(enabled).items() if count > 1)
    if duplicate_enabled:
        raise ValueError(f"signal_collection_duplicate_enabled_analyst:{','.join(duplicate_enabled)}")
    evidence_items: list[dict] = []
    source_contracts: list[dict] = []
    missing_evidence: list[str] = []
    data_quality_flags: list[str] = []
    invalidation_summary: list[dict] = []
    setup_types: list[str] = []
    horizons: list[str] = []
    side_counts: Counter[str] = Counter()
    side_confidence: Counter[str] = Counter()
    trigger_states_by_side: dict[str, Counter[str]] = {
        "long": Counter(),
        "short": Counter(),
        "flat": Counter(),
    }

    seen_agents: set[str] = set()
    for signal in analyst_signals or []:
        agent = _agent_name(signal)
        if not agent:
            raise ValueError("signal_collection_missing_analyst_name")
        if agent not in enabled:
            raise ValueError(f"signal_collection_unexpected_analyst:{agent}")
        if agent in seen_agents:
            raise ValueError(f"signal_collection_duplicate_analyst:{agent}")
        seen_agents.add(agent)
        contract = _action_contract(signal)
        if not contract:
            raise ValueError(f"signal_collection_missing_action_evidence_contract:{agent}")
        if contract.get("contract_version") != ACTION_EVIDENCE_CONTRACT_VERSION:
            raise ValueError(f"signal_collection_invalid_action_evidence_contract_version:{agent}")
        if _text(contract.get("analyst")) != agent:
            raise ValueError(f"signal_collection_action_contract_analyst_mismatch:{agent}")
        side = _side_from_contract(contract)
        confidence = _confidence(contract)
        trigger_valid = _bool(contract.get("trigger_valid"))
        trigger_confirmed = _bool(contract.get("current_trigger_confirmed"))
        trigger_status = (
            "confirmed"
            if trigger_valid and trigger_confirmed
            else "valid_unconfirmed"
            if trigger_valid
            else "watch_for_trigger"
        )
        side_counts[side] += 1
        side_confidence[side] += confidence
        trigger_states_by_side[side][trigger_status] += 1

        setup_type = _text(contract.get("setup_type"), "unknown")
        if setup_type and setup_type != "unknown":
            setup_types.append(setup_type)
        horizon = _text(contract.get("horizon_class"), "unknown")
        if horizon and horizon != "unknown":
            horizons.append(horizon)

        missing_evidence.extend(str(item) for item in _list(contract.get("missing_evidence")) if str(item))
        data_usage = contract.get("data_usage_summary") or {}
        if isinstance(data_usage, Mapping):
            for key in ("data_quality_flags", "risk_flags", "missing_data", "stale_data"):
                data_quality_flags.extend(str(item) for item in _list(data_usage.get(key)) if str(item))
        no_lookahead_status = _text(contract.get("no_lookahead_status"), "unchecked")
        if no_lookahead_status not in {"ok", "pass", "clean"}:
            data_quality_flags.append(f"no_lookahead_status:{no_lookahead_status}")

        if _bool(contract.get("invalidation_present")):
            invalidation_summary.append({
                "analyst": agent,
                "condition": _text(contract.get("invalidation_condition")),
                "level": contract.get("invalidation_level"),
            })
        product_profile_evidence = contract.get("product_profile_evidence")
        product_profile_evidence = (
            dict(product_profile_evidence)
            if isinstance(product_profile_evidence, Mapping)
            else {}
        )
        fusion_evidence = contract.get("fusion_evidence")
        fusion_evidence = dict(fusion_evidence) if isinstance(fusion_evidence, Mapping) else {}

        item = {
            "analyst": agent,
            "side": side,
            "confidence": confidence,
            "signal": _text(contract.get("signal")),
            "opportunity_state": _text(contract.get("opportunity_state"), "unknown"),
            "trigger_valid": trigger_valid,
            "current_trigger_confirmed": trigger_confirmed,
            "trigger_status": trigger_status,
            "entry_trigger": _text(contract.get("entry_trigger")),
            "setup_type": setup_type,
            "setup_quality_ok": _bool(contract.get("setup_quality_ok")),
            "horizon_class": horizon,
            "market_regime": _text(contract.get("market_regime"), "unknown"),
            "evidence_quality": _text(contract.get("evidence_quality"), "unknown"),
            "current_evidence_conflict": _list(contract.get("current_evidence_conflict")),
            "missing_evidence": _list(contract.get("missing_evidence")),
            "fusion_evidence": fusion_evidence,
            "evidence_strength": _text(fusion_evidence.get("evidence_strength") or contract.get("evidence_strength")),
            "evidence_freshness": _text(fusion_evidence.get("evidence_freshness")),
            "confirmation_requirements": _list(fusion_evidence.get("confirmation_requirements") or contract.get("confirmation_requirements")),
            "product_profile_id": _text(product_profile_evidence.get("product_profile_id")),
            "product_profile_used": _bool(product_profile_evidence.get("product_profile_used")),
            "product_profile_analysis_boundary": _text(product_profile_evidence.get("profile_analysis_boundary")),
        }
        evidence_items.append(item)
        source_contracts.append({
            "analyst": agent,
            "action_evidence_contract": contract,
            "product_profile_evidence": product_profile_evidence,
            "fusion_evidence": fusion_evidence,
            "signal_record_id": _metadata(signal).get("signal_record_id"),
        })

    missing_agents = [name for name in enabled if name not in seen_agents]
    missing_evidence.extend(f"missing_analyst:{name}" for name in missing_agents)

    long_key = (side_counts.get("long", 0), side_confidence.get("long", 0.0))
    short_key = (side_counts.get("short", 0), side_confidence.get("short", 0.0))
    if long_key == short_key and long_key[0] > 0:
        dominant_side = "mixed"
    elif long_key > short_key:
        dominant_side = "long"
    elif short_key > long_key:
        dominant_side = "short"
    else:
        dominant_side = "flat"
    supporting = [item["analyst"] for item in evidence_items if item.get("side") == dominant_side and dominant_side != "flat"]
    opposing_side = "short" if dominant_side == "long" else "long" if dominant_side == "short" else ""
    opposing = [item["analyst"] for item in evidence_items if item.get("side") == opposing_side]
    neutral = [item["analyst"] for item in evidence_items if item.get("side") == "flat"]

    if dominant_side == "mixed":
        consensus = "conflicted"
    elif dominant_side == "flat":
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
    dominant_trigger_states = trigger_states_by_side.get(dominant_side, Counter())
    aggregate_trigger = (
        "confirmed"
        if dominant_trigger_states.get("confirmed")
        else "valid_unconfirmed"
        if dominant_trigger_states.get("valid_unconfirmed")
        else "watch_for_trigger"
    )
    fusion_summary = build_signal_collection_fusion_summary(
        evidence_items,
        dominant_side=dominant_side,
    )
    merged_missing_evidence = sorted(set(missing_evidence) | set(fusion_summary.get("missing_evidence") or []))

    result = {
        "contract_version": SCC_CONTRACT_VERSION,
        "source_agent": "signal_collector",
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
        "confirmation_requirements": fusion_summary.get("confirmation_requirements") or [],
        "missing_evidence": merged_missing_evidence,
        "data_quality_flags": sorted(set(data_quality_flags)),
        "setup_types": sorted(set(setup_types)),
        "horizon_scope": sorted(set(horizons)),
        "invalidation_summary": invalidation_summary,
        "evidence_fusion": fusion_summary,
        "collector_decision_boundary": "no_trade_authority",
    }
    return validate_signal_collection_contract(
        result,
        ticker=ticker,
        trading_date=trading_date,
        enabled_analysts=enabled,
        analyst_signals=analyst_signals,
    )
