"""Deterministic opportunity ranking helper."""

from __future__ import annotations

from typing import Any, Mapping

from tools.agent_tools.analysis.analyst_signal_fusion import (
    CAPITAL_PRIORITY_RANK_MEANING,
    CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION,
    build_opportunity_scorecard,
    rank_semantics_payload,
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _side_row(scorecard: Mapping[str, Any], side: str) -> dict:
    row = scorecard.get(side)
    return dict(row) if isinstance(row, Mapping) else {}


def _rankable(row: Mapping[str, Any]) -> bool:
    state = str(row.get("final_state") or row.get("opportunity_state") or "").lower()
    return bool(
        _safe_float(row.get("score"), 0.0) > 0.0
        or state in {"tradeable_candidate", "probe_candidate", "watch_for_trigger"}
    )


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
    """Return a reproducible opportunity scorecard and capital-priority rank.

    The tool scores and ranks opportunity candidates. It does not size positions
    and does not create final trading authority.
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
        row.update(rank_semantics_payload())
        candidates.append(
            {
                "side": side,
                "score": _safe_float(row.get("score"), 0.0),
                "opportunity_score": _safe_float(row.get("opportunity_score", row.get("score")), 0.0),
                "capital_priority_score": _capital_priority_score(row),
                "capital_priority_tier": _capital_priority_tier(row),
                "final_state": str(row.get("final_state") or row.get("opportunity_state") or "unknown"),
                "rankable": _rankable(row),
                "opportunity_score_components": dict(row.get("opportunity_score_components") or {}),
            }
        )
    candidates.sort(
        key=lambda row: (
            0 if row["rankable"] else 1,
            -row["capital_priority_score"],
            -row["opportunity_score"],
            -row["capital_priority_tier"],
            row["side"],
        )
    )
    rank_by_side: dict[str, int | None] = {}
    rank = 1
    for candidate in candidates:
        if candidate["rankable"]:
            rank_by_side[candidate["side"]] = rank
            rank += 1
        else:
            rank_by_side[candidate["side"]] = None
    for side, side_rank in rank_by_side.items():
        row = scorecard.get(side)
        if isinstance(row, dict):
            row["opportunity_rank"] = side_rank
            row.update(rank_semantics_payload())

    preferred_side = str(scorecard.get("preferred_side") or "flat").lower()
    preferred_row = _side_row(scorecard, preferred_side) if preferred_side in {"long", "short"} else {}
    capital_allocation_reason = {
        "tool": "opportunity_ranking",
        "ticker": ticker,
        "rank_semantics_version": CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION,
        "opportunity_rank_meaning": CAPITAL_PRIORITY_RANK_MEANING,
        "rank_is_capital_priority": True,
        "preferred_side": preferred_side,
        "preferred_score": _safe_float(preferred_row.get("score"), 0.0),
        "preferred_capital_priority_score": _capital_priority_score(preferred_row) if preferred_row else 0.0,
        "preferred_capital_priority_tier": _capital_priority_tier(preferred_row) if preferred_row else 0,
        "preferred_rank": rank_by_side.get(preferred_side),
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
    }
    return {
        "opportunity_scorecard": scorecard,
        "rank_semantics_version": CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION,
        "opportunity_rank_meaning": CAPITAL_PRIORITY_RANK_MEANING,
        "rank_is_capital_priority": True,
        "rank_is_not_trade_authority": True,
        "opportunity_score_components": (
            dict(preferred_row.get("opportunity_score_components") or {}) if preferred_row else {}
        ),
        "opportunity_rank": rank_by_side,
        "capital_allocation_reason": capital_allocation_reason,
        "ranking_tool_trace": {
            "tool": "opportunity_ranking",
            "deterministic": True,
            "no_llm": True,
            "candidates": candidates,
        },
    }
