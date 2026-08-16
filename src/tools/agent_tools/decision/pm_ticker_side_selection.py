"""Deterministic PM ticker side selection helper.

This tool is PM step 2 only: choose the ticker-local side and candidate
quality. It never creates the final all-market capital rank, deploys capital,
sizes positions, or signs the final_action_contract.
"""

from __future__ import annotations

from typing import Any, Mapping

from tools.agent_tools.decision.pm_signal_fusion import build_opportunity_scorecard


SIDE_PRIORITY_SEMANTICS_VERSION = "agentquant.ticker_side_priority.v1"
SIDE_PRIORITY_MEANING = "side_priority_selects_ticker_direction_not_capital_rank"

FINAL_CAPITAL_RANK_FIELDS = {
    "opportunity_rank",
    "rank_source",
    "rank_scope",
    "capital_rank_generated_by",
    "rank_capital_role",
    "capital_layer",
    "capital_ratio_source",
    "rank_reason",
    "rank_semantics_version",
    "opportunity_rank_meaning",
    "rank_is_capital_priority",
    "rank_input_components",
    "rank_score",
    "rank_score_components",
    "capital_priority_score",
    "capital_priority_tier",
    "alpha_scale_eligible",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _bounded(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value or 0.0)))


def _side_row(scorecard: Mapping[str, Any], side: str) -> dict:
    row = scorecard.get(side)
    return dict(row) if isinstance(row, Mapping) else {}


def _candidate_state(row: Mapping[str, Any]) -> str:
    return str(row.get("final_state") or row.get("opportunity_state") or "").strip().lower()


def _candidate_eligible(row: Mapping[str, Any]) -> bool:
    state = _candidate_state(row)
    if state in {"no_opportunity", "wait", "flat_wait", "blocked", "rejected"}:
        return False
    return bool(
        state in {"tradeable_candidate", "probe_candidate", "watch_for_trigger"}
        or _safe_float(row.get("opportunity_score", row.get("score")), 0.0) > 0.0
    )


def _candidate_layer_hint(row: Mapping[str, Any]) -> str:
    state = _candidate_state(row)
    if state == "tradeable_candidate":
        return "tradeable_candidate"
    if state == "probe_candidate":
        return "exploration_probe_candidate"
    if state == "watch_for_trigger":
        return "watch_for_trigger_candidate"
    return "not_candidate"


def side_priority_semantics_payload() -> dict[str, Any]:
    return {
        "side_priority_semantics_version": SIDE_PRIORITY_SEMANTICS_VERSION,
        "side_priority_meaning": SIDE_PRIORITY_MEANING,
        "side_priority_is_not_capital_rank": True,
        "side_priority_is_not_trade_authority": True,
    }


def _strip_final_rank_fields(row: dict) -> None:
    for field in FINAL_CAPITAL_RANK_FIELDS:
        row.pop(field, None)


def _candidate_quality_components(row: Mapping[str, Any]) -> dict[str, float]:
    components = row.get("candidate_quality_components")
    if not isinstance(components, Mapping):
        return {}
    return {
        str(key): round(_safe_float(value), 6)
        for key, value in components.items()
    }


def _candidate_quality(row: Mapping[str, Any]) -> float:
    return round(_bounded(_safe_float(row.get("candidate_quality"), 0.0)), 6)


def _resolved_scc_direction(contract: Mapping[str, Any] | None) -> str:
    scc = contract if isinstance(contract, Mapping) else {}
    dominant_side = str(scc.get("dominant_side") or "flat").strip().lower()
    side_consensus = str(scc.get("side_consensus") or "").strip().lower()
    fusion = scc.get("evidence_fusion") if isinstance(scc.get("evidence_fusion"), Mapping) else {}
    alignment = str(fusion.get("evidence_alignment_state") or "").strip().lower()
    if dominant_side not in {"long", "short"}:
        return "flat"
    if side_consensus == "conflicted" or alignment == "conflicted":
        return "flat"
    return dominant_side


def _legal_scc_candidate_sides(
    contract: Mapping[str, Any] | None,
    scorecard: Mapping[str, Any],
) -> list[str]:
    """Return only sides already represented by legal structured SCC evidence."""

    scc = contract if isinstance(contract, Mapping) else {}
    side_consensus = str(scc.get("side_consensus") or "").strip().lower()
    fusion = (
        scc.get("evidence_fusion")
        if isinstance(scc.get("evidence_fusion"), Mapping)
        else {}
    )
    alignment = str(fusion.get("evidence_alignment_state") or "").strip().lower()
    if side_consensus == "conflicted" or alignment == "conflicted":
        return []
    legal: set[str] = set()
    dominant_side = str(scc.get("dominant_side") or "flat").strip().lower()
    if dominant_side in {"long", "short"} and _candidate_eligible(
        _side_row(scorecard, dominant_side)
    ):
        legal.add(dominant_side)
    for item in scc.get("evidence_items") or []:
        if not isinstance(item, Mapping):
            continue
        side = str(item.get("side") or "flat").strip().lower()
        if side in {"long", "short"} and _candidate_eligible(
            _side_row(scorecard, side)
        ):
            legal.add(side)
    return sorted(legal)


def _economic_preferred_side(
    contract: Mapping[str, Any] | None,
    scorecard: Mapping[str, Any],
) -> str:
    legal = _legal_scc_candidate_sides(contract, scorecard)
    if len(legal) == 1:
        return legal[0]
    if len(legal) > 1:
        matured_economic_sides = [
            side
            for side in legal
            if str(
                (
                    _side_row(scorecard, side).get("forecast_calibration_summary")
                    or {}
                ).get("status")
                or ""
            ).lower()
            == "matured"
        ]
        if len(matured_economic_sides) != len(legal):
            return _resolved_scc_direction(contract)
        ranked = sorted(
            legal,
            key=lambda side: (
                -_safe_float(
                    (_side_row(scorecard, side).get("forecast_calibration_summary") or {}).get(
                        "current_expected_return_after_fee"
                    ),
                    0.0,
                ),
                -_candidate_quality(_side_row(scorecard, side)),
                side,
            ),
        )
        return ranked[0]
    return _resolved_scc_direction(contract)


def select_ticker_side(
    *,
    ticker: str,
    analyst_signals: list,
    signal_collection_contract: Mapping[str, Any] | None,
    market_confirmation: Mapping[str, Any] | None,
    data_quality_summary: Mapping[str, Any] | None,
    decision_date: Any,
    config: Mapping[str, Any] | None,
    prebuilt_scorecard: Mapping[str, Any] | None = None,
) -> dict:
    """Return ticker-local side priority and candidate quality only."""
    scorecard = (
        dict(prebuilt_scorecard)
        if isinstance(prebuilt_scorecard, Mapping)
        else build_opportunity_scorecard(
            ticker=ticker,
            analyst_signals=analyst_signals,
            market_confirmation=market_confirmation or {},
            data_quality_summary=data_quality_summary or {},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=[],
            signal_collection_contract=signal_collection_contract or {},
            decision_date=decision_date,
            config=config or {},
        )
    )
    candidates: list[dict[str, Any]] = []
    for side in ("long", "short"):
        row = _side_row(scorecard, side)
        if not row:
            continue
        _strip_final_rank_fields(row)
        row.update(side_priority_semantics_payload())
        quality_components = _candidate_quality_components(row)
        quality = _candidate_quality(row)
        candidates.append(
            {
                "side": side,
                "score": _safe_float(row.get("score"), 0.0),
                "opportunity_score": _safe_float(row.get("opportunity_score", row.get("score")), 0.0),
                "side_priority_score": quality,
                "candidate_quality": quality,
                "candidate_quality_components": quality_components,
                "candidate_layer_hint": _candidate_layer_hint(row),
                "final_state": str(row.get("final_state") or row.get("opportunity_state") or "unknown"),
                "candidate_eligible": _candidate_eligible(row),
                "opportunity_score_components": dict(row.get("opportunity_score_components") or {}),
                **side_priority_semantics_payload(),
            }
        )
    candidates.sort(
        key=lambda row: (
            0 if row["candidate_eligible"] else 1,
            -row["candidate_quality"],
            -row["opportunity_score"],
            row["side"],
        )
    )
    preferred_side = _economic_preferred_side(
        signal_collection_contract,
        scorecard,
    )
    priority_by_side: dict[str, int | None] = {
        "long": 1 if preferred_side == "long" else None,
        "short": 1 if preferred_side == "short" else None,
    }
    for side, priority in priority_by_side.items():
        row = scorecard.get(side)
        if isinstance(row, dict):
            _strip_final_rank_fields(row)
            row["side_priority"] = priority
            row["ticker_side_priority"] = priority
            row["side_priority_score"] = _candidate_quality(row)
            row.update(side_priority_semantics_payload())

    scorecard["preferred_side"] = preferred_side
    preferred_row = _side_row(scorecard, preferred_side) if preferred_side in {"long", "short"} else {}
    preferred_quality = _candidate_quality(preferred_row) if preferred_row else 0.0
    capital_allocation_reason = {
        "tool": "ticker_side_selection",
        "ticker": ticker,
        "side_priority_semantics_version": SIDE_PRIORITY_SEMANTICS_VERSION,
        "side_priority_meaning": SIDE_PRIORITY_MEANING,
        "side_priority_is_not_capital_rank": True,
        "preferred_side": preferred_side,
        "preferred_score": _safe_float(preferred_row.get("score"), 0.0),
        "preferred_side_priority_score": preferred_quality,
        "preferred_candidate_quality": preferred_quality,
        "preferred_candidate_quality_components": _candidate_quality_components(preferred_row) if preferred_row else {},
        "preferred_candidate_layer_hint": _candidate_layer_hint(preferred_row) if preferred_row else "not_candidate",
        "preferred_side_priority": priority_by_side.get(preferred_side),
        "signal_collection_summary": {
            "dominant_side": (signal_collection_contract or {}).get("dominant_side"),
            "side_consensus": (signal_collection_contract or {}).get("side_consensus"),
            "trigger_status": (signal_collection_contract or {}).get("trigger_status"),
            "evidence_strength": (signal_collection_contract or {}).get("evidence_strength"),
            "evidence_conflict_level": (signal_collection_contract or {}).get("evidence_conflict_level"),
            "evidence_alignment_state": ((signal_collection_contract or {}).get("evidence_fusion") or {}).get("evidence_alignment_state"),
            "multi_evidence_consensus_score": ((signal_collection_contract or {}).get("evidence_fusion") or {}).get("multi_evidence_consensus_score"),
            "cross_analyst_conflicts": ((signal_collection_contract or {}).get("evidence_fusion") or {}).get("cross_analyst_conflicts") or [],
            "dominant_opposing_evidence": ((signal_collection_contract or {}).get("evidence_fusion") or {}).get("dominant_opposing_evidence") or [],
            "confirmation_requirements": (signal_collection_contract or {}).get("confirmation_requirements") or [],
        },
        "side_priority_is_not_trade_authority": True,
        "final_opportunity_rank_generated_here": False,
    }
    return {
        "opportunity_scorecard": scorecard,
        "side_priority_semantics_version": SIDE_PRIORITY_SEMANTICS_VERSION,
        "side_priority_meaning": SIDE_PRIORITY_MEANING,
        "side_priority_is_not_capital_rank": True,
        "side_priority_is_not_trade_authority": True,
        "opportunity_score_components": (
            dict(preferred_row.get("opportunity_score_components") or {}) if preferred_row else {}
        ),
        "ticker_side_priority": priority_by_side,
        "side_priority": priority_by_side,
        "final_opportunity_rank_generated_here": False,
        "capital_allocation_reason": capital_allocation_reason,
        "ticker_side_selection_trace": {
            "tool": "ticker_side_selection",
            "deterministic": True,
            "no_llm": True,
            "final_opportunity_rank_generated_here": False,
            "candidates": candidates,
        },
    }
