"""PM contract-building helpers.

These helpers are PM-owned tools. They do not write DB rows, artifacts, or
payloads; only the PM recommendation writer may persist the final contract.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from tools.agent_tools.decision.pm_position_transition import final_action_from_lots
from tools.common.order_semantics import build_lot_intent_consistency


FINAL_ACTION_CONTRACT_VERSION = "agentquant.final_action.v1"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _default_learning_trace(rows: list | None, limit: int = 10) -> list:
    return list(rows or [])[: int(limit or 10)]


def build_final_action_contract(
    *,
    ticker: str,
    current_lots: int,
    target_lots: int,
    position_ratio: float,
    margin_required: float,
    account_equity: float,
    lots_to_trade: int,
    lots_to_trade_reason: str | None,
    recommendation_intent: dict,
    final_entry_authority: dict | None,
    control_reasons: list[str],
    control_diagnostics: dict | None,
    opportunity_scorecard: dict | None,
    market_confirmation: dict | None,
    alpha_setup_action_values: list | None,
    execution_contract_fields: dict | None = None,
    select_learning_trace_action_values: Callable[[list | None, int], List[dict]] | None = None,
    safe_float: Callable[[Any, float], float] | None = None,
    futures_action_cls: Any | None = None,
) -> Dict[str, Any]:
    """Build the PM final_action_contract from already-computed PM components."""
    diagnostics = control_diagnostics if isinstance(control_diagnostics, dict) else {}
    scorecard = opportunity_scorecard if isinstance(opportunity_scorecard, dict) else {}
    execution_contract_payload = dict(execution_contract_fields) if isinstance(execution_contract_fields, dict) else {}
    coerce_float = safe_float or _safe_float
    select_learning_trace = select_learning_trace_action_values or _default_learning_trace
    target_side = "long" if int(target_lots or 0) > 0 else "short" if int(target_lots or 0) < 0 else "flat"
    scorecard_side = scorecard.get(target_side) if target_side in {"long", "short"} and isinstance(scorecard.get(target_side), dict) else {}
    if not scorecard_side:
        preferred_side = str(scorecard.get("preferred_side") or "").lower()
        scorecard_side = (
            scorecard.get(preferred_side)
            if preferred_side in {"long", "short"} and isinstance(scorecard.get(preferred_side), dict)
            else {}
        )
    authority = final_entry_authority if isinstance(final_entry_authority, dict) else {}
    final_action = final_action_from_lots(
        current_lots=current_lots,
        target_lots=target_lots,
        final_entry_authority=authority,
    )
    candidates: list[dict] = []
    scorecard_seed = diagnostics.get("scorecard_current_tradeable_probe_seed")
    if isinstance(scorecard_seed, dict):
        candidates.append({
            "action": "open_probe",
            "source": "opportunity_scorecard",
            "status": scorecard_seed.get("status") or (
                "applied" if "scorecard_current_tradeable_probe_seed" in control_reasons else "candidate"
            ),
            "side": scorecard_seed.get("side"),
            "ratio": scorecard_seed.get("ratio"),
            "scorecard_state": (
                (scorecard_seed.get("scorecard") or {}).get("final_state")
                if isinstance(scorecard_seed.get("scorecard"), dict)
                else None
            ),
        })
    conditional_monitor_seed = diagnostics.get("conditional_monitor_probe_seed")
    if isinstance(conditional_monitor_seed, dict):
        candidates.append({
            "action": "conditional_probe",
            "source": "conditional_monitor",
            "status": conditional_monitor_seed.get("status") or "candidate",
            "side": conditional_monitor_seed.get("side"),
            "ratio": conditional_monitor_seed.get("ratio"),
            "requires_intraday_confirmation": bool(
                conditional_monitor_seed.get("requires_intraday_confirmation")
            ),
            "scorecard_state": (
                (conditional_monitor_seed.get("scorecard") or {}).get("final_state")
                if isinstance(conditional_monitor_seed.get("scorecard"), dict)
                else None
            ),
        })
    learned_seed = diagnostics.get("positive_open_action_value_seed")
    if isinstance(learned_seed, dict):
        candidates.append({
            "action": "open_probe" if final_action == "open_probe" else "open_real",
            "source": "alpha_setup_action_value",
            "status": "applied" if "positive_open_action_value_seed" in control_reasons else "candidate",
            "side": learned_seed.get("target_side"),
            "ratio": learned_seed.get("seed_position_ratio"),
            "reward_mean": (
                (learned_seed.get("selected_action_value") or {}).get("reward_mean")
                if isinstance(learned_seed.get("selected_action_value"), dict)
                else None
            ),
        })
    winning = diagnostics.get("winning_template_continuation")
    if isinstance(winning, dict) and winning.get("decision"):
        candidates.append({
            "action": "exit" if winning.get("protective_exit") else "reduce",
            "source": "hold_exit_profit_protection",
            "status": "applied" if final_action in {"reduce", "exit"} else "candidate",
            "decision": winning.get("decision"),
            "pnl_ratio": winning.get("pnl_ratio"),
            "confirmation_score": winning.get("confirmation_score"),
        })
    lifecycle = diagnostics.get("holding_rebalance_control")
    if isinstance(lifecycle, dict) and lifecycle.get("decision"):
        candidates.append({
            "action": final_action,
            "source": "position_lifecycle",
            "status": "applied",
            "decision": lifecycle.get("decision"),
            "classification": lifecycle.get("lifecycle_classification"),
        })

    selected_action_values = select_learning_trace(alpha_setup_action_values, 10)
    memory_requirements = (
        diagnostics.get("final_action_memory_requirements")
        if isinstance(diagnostics.get("final_action_memory_requirements"), dict)
        else {}
    )
    memory_retrieval = (
        diagnostics.get("final_action_memory_retrieval")
        if isinstance(diagnostics.get("final_action_memory_retrieval"), dict)
        else {}
    )
    margin_ratio_estimate = float(margin_required or 0.0) / max(float(account_equity or 0.0), 1.0)
    reason_codes = {str(item) for item in (control_reasons or []) if item}
    if lots_to_trade_reason:
        reason_codes.add(str(lots_to_trade_reason))
    reason_codes.update(str(item) for item in (authority.get("reason_codes") or []) if item)
    execution_fields = {
        key: execution_contract_payload.get(key)
        for key in (
            "execution_profile",
            "trigger_source",
            "entry_trigger",
            "invalidation",
            "valid_until",
            "requires_intraday_confirmation",
            "can_execute_without_intraday_trigger",
            "execution_action_value_preference",
            "analyst_execution_roles",
        )
        if key in execution_contract_payload
    }
    action_value = recommendation_intent.get("action") if isinstance(recommendation_intent, dict) else "hold"
    if futures_action_cls is not None:
        try:
            action_value = futures_action_cls(str(action_value))
        except Exception:
            pass
    return {
        "contract_version": FINAL_ACTION_CONTRACT_VERSION,
        "ticker": ticker,
        "final_action": final_action,
        "current_lots": int(current_lots or 0),
        "target_lots": int(target_lots or 0),
        "lots_delta": int((target_lots or 0) - (current_lots or 0)),
        "lots_delta_abs": abs(int((target_lots or 0) - (current_lots or 0))),
        "target_position_ratio": float(position_ratio or 0.0),
        "target_margin_ratio_estimate": margin_ratio_estimate,
        "authority_type": authority.get("authority_type") or "not_applicable",
        "authority_decision": authority.get("decision") or "not_applicable",
        "requires_authority": bool(authority.get("requires_authority")),
        "open_action_evidence": bool(authority.get("open_action_evidence")),
        "strong_current_evidence": bool(authority.get("strong_current_evidence")),
        "watch_for_trigger_block": bool(authority.get("watch_for_trigger_block")),
        "conditional_trigger_authority": bool(authority.get("conditional_trigger_authority")),
        "negative_profile": bool(authority.get("negative_profile")),
        "tradeable_state": bool(authority.get("tradeable_state")),
        "weak_conflict_probe": bool(authority.get("weak_conflict_probe")),
        "max_allowed_margin_ratio": float(coerce_float(authority.get("max_allowed_margin_ratio"), 0.0)),
        "reason_codes": sorted(reason_codes),
        "recommendation_intent": recommendation_intent,
        "action_candidates": candidates,
        "evidence_used": {
            "scorecard_preferred_side": scorecard.get("preferred_side"),
            "scorecard_state": scorecard_side.get("final_state"),
            "scorecard_score": scorecard_side.get("score"),
            "opportunity_score": scorecard_side.get("opportunity_score", scorecard_side.get("score")),
            "opportunity_score_components": scorecard_side.get("opportunity_score_components") or {},
            "opportunity_rank": scorecard_side.get("opportunity_rank"),
            "capital_allocation_reason": scorecard_side.get("capital_allocation_reason"),
            "market_confirmation_score": (
                coerce_float((market_confirmation or {}).get("confirmation_score"), 0.0)
                if isinstance(market_confirmation, dict)
                else 0.0
            ),
            "market_confirmation_conflicts": (
                (market_confirmation or {}).get("conflicts")
                if isinstance(market_confirmation, dict)
                else None
            ),
        },
        "learning_used": {
            "alpha_setup_action_values": selected_action_values,
            "memory_requirements": memory_requirements,
            "memory_retrieval": memory_retrieval,
            "positive_open_seed": diagnostics.get("positive_open_action_value_seed") if isinstance(diagnostics.get("positive_open_action_value_seed"), dict) else {},
            "alpha_setup_ev_fusion": diagnostics.get("alpha_setup_ev_fusion") if isinstance(diagnostics.get("alpha_setup_ev_fusion"), dict) else {},
            "capital_utilization_learning": (
                diagnostics.get("capital_utilization_learning")
                if isinstance(diagnostics.get("capital_utilization_learning"), dict)
                else {}
            ),
            "capital_utilization_target": (
                diagnostics.get("capital_utilization_target")
                if isinstance(diagnostics.get("capital_utilization_target"), dict)
                else {}
            ),
            "memory_state": (
                ((diagnostics.get("capital_utilization_learning") or {}).get("protected_memory") or {}).get("memory_state")
                if isinstance(diagnostics.get("capital_utilization_learning"), dict)
                and isinstance((diagnostics.get("capital_utilization_learning") or {}).get("protected_memory"), dict)
                else ""
            ),
            "learning_adjustment_summary": scorecard_side.get("learning_adjustment_summary") or {},
        },
        "risk_flags": sorted(reason_codes),
        **execution_fields,
        "execution_profile": execution_contract_payload.get("execution_profile"),
        "execution_requirement": (
            "intraday_trigger_required"
            if final_action in {"open_probe", "open_real", "scale"}
            else "position_management_or_wait"
        ),
        "consistency": build_lot_intent_consistency(
            current_lots=int(current_lots or 0),
            target_lots=int(target_lots or 0),
            action=action_value,
            lots=int(recommendation_intent.get("lots") or 0) if isinstance(recommendation_intent, dict) else 0,
            mode="final_action_contract",
        ),
        "single_source_of_trade_truth": True,
        "candidate_sources_do_not_bypass_contract": True,
    }
