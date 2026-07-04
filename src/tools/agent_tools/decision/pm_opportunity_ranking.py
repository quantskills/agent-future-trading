"""Deterministic opportunity ranking helper."""

from __future__ import annotations

from typing import Any, Mapping

from tools.agent_tools.analysis.analyst_signal_fusion import (
    build_opportunity_scorecard,
)

RANK_CAPITAL_ROLE_EXPLORATION = "best_exploration_probe_candidate"
RANK_CAPITAL_ROLE_REAL_BUDGET = "best_real_budget_candidate"
RANK_CAPITAL_ROLE_ALPHA_SCALE = "best_alpha_scale_candidate"
CAPITAL_LAYER_EXPLORATION = "exploration_probe"
CAPITAL_LAYER_REAL_BUDGET = "real_budget_entry"
CAPITAL_LAYER_ALPHA_SCALE = "alpha_scale_entry"
CAPITAL_RATIO_SOURCE_EXPLORATION = "probe_margin_ratio_0.008"
CAPITAL_RATIO_SOURCE_REAL_BUDGET = "normal_trade_margin_ratio"
CAPITAL_RATIO_SOURCE_ALPHA_SCALE = "strong_opportunity_target_margin_ratio"
SIDE_PRIORITY_SEMANTICS_VERSION = "agentquant.ticker_side_priority.v1"
SIDE_PRIORITY_MEANING = "side_priority_selects_ticker_direction_not_capital_rank"


FINAL_RANK_FIELDS = {
    "opportunity_rank",
    "rank_capital_role",
    "capital_layer",
    "capital_ratio_source",
    "rank_reason",
    "rank_semantics_version",
    "opportunity_rank_meaning",
    "rank_is_capital_priority",
    "rank_is_not_trade_authority",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _side_row(scorecard: Mapping[str, Any], side: str) -> dict:
    row = scorecard.get(side)
    return dict(row) if isinstance(row, Mapping) else {}


def _candidate_state(row: Mapping[str, Any]) -> str:
    return str(row.get("final_state") or row.get("opportunity_state") or "").strip().lower()


def _rankable(row: Mapping[str, Any]) -> bool:
    state = _candidate_state(row)
    if state in {"no_opportunity", "wait", "flat_wait", "blocked", "rejected"}:
        return False
    return bool(state in {"tradeable_candidate", "probe_candidate", "watch_for_trigger"} or _safe_float(row.get("score"), 0.0) > 0.0)


def _capital_priority_score(row: Mapping[str, Any]) -> float:
    value = row.get("capital_priority_score")
    if value not in (None, ""):
        return _safe_float(value, 0.0)
    return _safe_float(row.get("opportunity_score", row.get("score")), 0.0)


def _capital_priority_tier(row: Mapping[str, Any]) -> int:
    value = row.get("capital_priority_tier")
    if value not in (None, ""):
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    state = str(row.get("final_state") or row.get("opportunity_state") or "").lower()
    return {
        "tradeable_candidate": 3,
        "probe_candidate": 2,
        "watch_for_trigger": 1,
        "no_opportunity": 0,
    }.get(state, 0)


def _is_alpha_scale_candidate(row: Mapping[str, Any]) -> bool:
    layer = str(row.get("capital_layer") or "").strip().lower()
    role = str(row.get("rank_capital_role") or "").strip().lower()
    if layer == CAPITAL_LAYER_ALPHA_SCALE or role == RANK_CAPITAL_ROLE_ALPHA_SCALE:
        return True
    return any(
        bool(row.get(field))
        for field in (
            "alpha_scale_candidate",
            "mature_alpha_candidate",
            "repeated_positive_alpha",
            "strong_opportunity_alpha_scale_candidate",
        )
    )


def capital_layer_for_ranked_row(row: Mapping[str, Any]) -> str:
    """Return the capital layer described by the single capital-priority rank."""
    if _is_alpha_scale_candidate(row):
        return CAPITAL_LAYER_ALPHA_SCALE
    state = _candidate_state(row)
    if state == "tradeable_candidate":
        return CAPITAL_LAYER_REAL_BUDGET
    if state in {"probe_candidate", "watch_for_trigger"}:
        return CAPITAL_LAYER_EXPLORATION
    if _safe_float(row.get("score"), 0.0) > 0.0 or _safe_float(row.get("opportunity_score"), 0.0) > 0.0:
        return CAPITAL_LAYER_EXPLORATION
    return "not_capital_rank_candidate"


def rank_capital_role_for_layer(layer: str) -> str:
    value = str(layer or "").strip().lower()
    if value == CAPITAL_LAYER_ALPHA_SCALE:
        return RANK_CAPITAL_ROLE_ALPHA_SCALE
    if value == CAPITAL_LAYER_REAL_BUDGET:
        return RANK_CAPITAL_ROLE_REAL_BUDGET
    if value == CAPITAL_LAYER_EXPLORATION:
        return RANK_CAPITAL_ROLE_EXPLORATION
    return "not_capital_rank_candidate"


def capital_ratio_source_for_layer(layer: str) -> str:
    value = str(layer or "").strip().lower()
    if value == CAPITAL_LAYER_ALPHA_SCALE:
        return CAPITAL_RATIO_SOURCE_ALPHA_SCALE
    if value == CAPITAL_LAYER_REAL_BUDGET:
        return CAPITAL_RATIO_SOURCE_REAL_BUDGET
    if value == CAPITAL_LAYER_EXPLORATION:
        return CAPITAL_RATIO_SOURCE_EXPLORATION
    return "not_applicable"


def rank_reason_for_layer(row: Mapping[str, Any], layer: str) -> str:
    value = str(layer or "").strip().lower()
    if value == CAPITAL_LAYER_ALPHA_SCALE:
        return "tradeable_candidate_with_repeated_positive_product_setup_trigger_evidence_and_controlled_drawdown"
    if value == CAPITAL_LAYER_REAL_BUDGET:
        return "tradeable_candidate_supported_by_current_evidence_and_product_learning"
    if value == CAPITAL_LAYER_EXPLORATION:
        return "best_watch_for_trigger_by_evidence_trigger_learning_and_risk"
    state = _candidate_state(row) or "unknown"
    return f"not_capital_rank_candidate:{state}"


def rank_metadata_for_row(row: Mapping[str, Any]) -> dict[str, str]:
    """Describe how the single rank maps to a capital layer without sizing authority."""
    layer = capital_layer_for_ranked_row(row)
    return {
        "rank_capital_role": rank_capital_role_for_layer(layer),
        "capital_layer": layer,
        "capital_ratio_source": capital_ratio_source_for_layer(layer),
        "rank_reason": rank_reason_for_layer(row, layer),
    }


def side_priority_semantics_payload() -> dict[str, Any]:
    return {
        "side_priority_semantics_version": SIDE_PRIORITY_SEMANTICS_VERSION,
        "side_priority_meaning": SIDE_PRIORITY_MEANING,
        "side_priority_is_not_capital_rank": True,
        "side_priority_is_not_trade_authority": True,
    }


def _count_items(value: Any) -> int:
    if isinstance(value, list):
        return len([item for item in value if item])
    if isinstance(value, Mapping):
        return len(value)
    if value in (None, "", False):
        return 0
    return 1


def _watch_priority_score(row: Mapping[str, Any]) -> float:
    components = row.get("opportunity_score_components")
    components = components if isinstance(components, Mapping) else {}
    evidence_quality = _safe_float(row.get("evidence_quality_score"), 0.0)
    setup_quality = _safe_float(row.get("setup_quality_score"), _safe_float(row.get("max_setup_quality"), 0.0))
    trigger_quality = _safe_float(row.get("trigger_quality_score"), 0.0)
    if bool(row.get("trigger_valid")) or bool(row.get("current_trigger_confirmed")):
        trigger_quality += 0.04
    if _count_items(row.get("entry_trigger") or row.get("entry_setup") or row.get("entry_conditions")):
        trigger_quality += 0.03
    if _count_items(row.get("invalidation") or row.get("invalidation_condition") or row.get("invalidation_boundary")):
        trigger_quality += 0.03
    product_learning = (
        _safe_float(components.get("positive_learning"), 0.0)
        + _safe_float(components.get("product_profile_alignment"), 0.0)
        + _safe_float(components.get("trigger_quality_positive_bonus"), 0.0)
        - abs(min(0.0, _safe_float(components.get("entry_quality_loss_penalty"), 0.0)))
        - abs(min(0.0, _safe_float(components.get("trigger_quality_loss_penalty"), 0.0)))
    )
    conflict_penalty = (
        abs(min(0.0, _safe_float(components.get("fusion_conflict_adjustment"), 0.0)))
        + abs(min(0.0, _safe_float(components.get("negative_learning"), 0.0)))
        + 0.02 * _count_items(row.get("gating_failures") or row.get("cross_analyst_conflicts"))
    )
    return (
        _capital_priority_score(row)
        + 0.25 * _safe_float(row.get("opportunity_score", row.get("score")), 0.0)
        + evidence_quality
        + setup_quality
        + trigger_quality
        + product_learning
        - conflict_penalty
    )


def rank_opportunities(
    *,
    ticker: str,
    analyst_signals: list,
    signal_collection_contract: Mapping[str, Any] | None,
    effective_memory_summary: Mapping[str, Any] | None,
    market_confirmation: Mapping[str, Any] | None,
    data_quality_summary: Mapping[str, Any] | None,
    adaptive_policy_state: list | None,
    alpha_setup_profiles: list | None,
    alpha_setup_action_values: list | None,
    decision_date: Any,
    config: Mapping[str, Any] | None,
    prebuilt_scorecard: Mapping[str, Any] | None = None,
) -> dict:
    """Return a reproducible opportunity scorecard and ticker side priority.

    The tool scores long/short candidates inside one ticker. It does not create
    the final all-market capital rank, size positions, or create final trading
    authority.
    """
    scorecard = (
        dict(prebuilt_scorecard)
        if isinstance(prebuilt_scorecard, Mapping)
        else build_opportunity_scorecard(
            ticker=ticker,
            analyst_signals=analyst_signals,
            market_confirmation=market_confirmation or {},
            data_quality_summary=data_quality_summary or {},
            adaptive_policy_state=adaptive_policy_state or [],
            alpha_setup_profiles=alpha_setup_profiles or [],
            alpha_setup_action_values=alpha_setup_action_values or [],
            signal_collection_contract=signal_collection_contract or {},
            decision_date=decision_date,
            config=config or {},
        )
    )
    candidates = []
    for side in ("long", "short"):
        row = _side_row(scorecard, side)
        if not row:
            continue
        for field in FINAL_RANK_FIELDS:
            row.pop(field, None)
        row.update(side_priority_semantics_payload())
        candidates.append(
            {
                "side": side,
                "score": _safe_float(row.get("score"), 0.0),
                "opportunity_score": _safe_float(row.get("opportunity_score", row.get("score")), 0.0),
                "capital_priority_score": _capital_priority_score(row),
                "capital_priority_tier": _capital_priority_tier(row),
                "watch_priority_score": _watch_priority_score(row),
                "final_state": str(row.get("final_state") or row.get("opportunity_state") or "unknown"),
                "rankable": _rankable(row),
                "opportunity_score_components": dict(row.get("opportunity_score_components") or {}),
                **side_priority_semantics_payload(),
            }
        )
    candidates.sort(
        key=lambda row: (
            0 if row["rankable"] else 1,
            -row["capital_priority_tier"],
            -row["watch_priority_score"],
            -row["capital_priority_score"],
            -row["opportunity_score"],
            row["side"],
        )
    )
    priority_by_side: dict[str, int | None] = {}
    side_priority = 1
    for candidate in candidates:
        if candidate["rankable"]:
            priority_by_side[candidate["side"]] = side_priority
            side_priority += 1
        else:
            priority_by_side[candidate["side"]] = None
    for side, priority in priority_by_side.items():
        row = scorecard.get(side)
        if isinstance(row, dict):
            for field in FINAL_RANK_FIELDS:
                row.pop(field, None)
            row["side_priority"] = priority
            row["ticker_side_priority"] = priority
            row["side_priority_score"] = _watch_priority_score(row)
            row["watch_priority_score"] = _watch_priority_score(row)
            row.update(side_priority_semantics_payload())

    preferred_side = str(scorecard.get("preferred_side") or "flat").lower()
    preferred_row = _side_row(scorecard, preferred_side) if preferred_side in {"long", "short"} else {}
    capital_allocation_reason = {
        "tool": "opportunity_ranking",
        "ticker": ticker,
        "side_priority_semantics_version": SIDE_PRIORITY_SEMANTICS_VERSION,
        "side_priority_meaning": SIDE_PRIORITY_MEANING,
        "side_priority_is_not_capital_rank": True,
        "preferred_side": preferred_side,
        "preferred_score": _safe_float(preferred_row.get("score"), 0.0),
        "preferred_capital_priority_score": _capital_priority_score(preferred_row) if preferred_row else 0.0,
        "preferred_capital_priority_tier": _capital_priority_tier(preferred_row) if preferred_row else 0,
        "preferred_watch_priority_score": _watch_priority_score(preferred_row) if preferred_row else 0.0,
        "preferred_candidate_capital_layer": (
            rank_metadata_for_row(preferred_row).get("capital_layer") if preferred_row else ""
        ),
        "preferred_candidate_capital_ratio_source": (
            rank_metadata_for_row(preferred_row).get("capital_ratio_source") if preferred_row else ""
        ),
        "preferred_candidate_rank_reason": (
            rank_metadata_for_row(preferred_row).get("rank_reason") if preferred_row else ""
        ),
        "preferred_side_priority": priority_by_side.get(preferred_side),
        "signal_collection_summary": {
            "dominant_side": (signal_collection_contract or {}).get("dominant_side"),
            "side_consensus": (signal_collection_contract or {}).get("side_consensus"),
            "trigger_status": (signal_collection_contract or {}).get("trigger_status"),
            "evidence_strength": (signal_collection_contract or {}).get("evidence_strength"),
            "evidence_conflict_level": (signal_collection_contract or {}).get("evidence_conflict_level"),
            "evidence_alignment_state": (signal_collection_contract or {}).get("evidence_alignment_state"),
            "multi_evidence_consensus_score": (signal_collection_contract or {}).get("multi_evidence_consensus_score"),
            "cross_analyst_conflicts": (signal_collection_contract or {}).get("cross_analyst_conflicts") or [],
            "dominant_opposing_evidence": (signal_collection_contract or {}).get("dominant_opposing_evidence") or [],
            "confirmation_requirements": (signal_collection_contract or {}).get("confirmation_requirements") or [],
        },
        "memory_summary": dict(effective_memory_summary or {}),
        "rank_is_not_trade_authority": True,
        "final_opportunity_rank_generated_here": False,
    }
    return {
        "opportunity_scorecard": scorecard,
        "side_priority_semantics_version": SIDE_PRIORITY_SEMANTICS_VERSION,
        "side_priority_meaning": SIDE_PRIORITY_MEANING,
        "side_priority_is_not_capital_rank": True,
        "rank_is_not_trade_authority": True,
        "opportunity_score_components": (
            dict(preferred_row.get("opportunity_score_components") or {}) if preferred_row else {}
        ),
        "ticker_side_priority": priority_by_side,
        "side_priority": priority_by_side,
        "final_opportunity_rank_generated_here": False,
        "capital_allocation_reason": capital_allocation_reason,
        "ranking_tool_trace": {
            "tool": "opportunity_ranking",
            "deterministic": True,
            "no_llm": True,
            "final_opportunity_rank_generated_here": False,
            "candidates": candidates,
        },
    }
