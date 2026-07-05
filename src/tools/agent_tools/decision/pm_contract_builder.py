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


def _compact_learning_trace_row(row: Any) -> dict:
    if not isinstance(row, dict):
        return {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return {
        "action_value_id": row.get("id") or payload.get("id"),
        "scope_key": row.get("scope_key") or payload.get("scope_key"),
        "ticker": row.get("ticker") or payload.get("ticker"),
        "side": row.get("side") or payload.get("side"),
        "setup_type": row.get("setup_type") or payload.get("setup_type"),
        "action_name": row.get("action_name") or payload.get("action_name"),
        "learning_lane": row.get("learning_lane") or payload.get("learning_lane"),
        "action_preference": row.get("action_preference") or payload.get("action_preference"),
        "memory_side_role": row.get("memory_side_role") or payload.get("memory_side_role"),
        "reward_mean": row.get("reward_mean") or payload.get("reward_mean"),
        "reward_sum": row.get("reward_sum") or payload.get("reward_sum"),
        "win_rate": row.get("win_rate") or payload.get("win_rate"),
        "sample_count": row.get("sample_count") or payload.get("sample_count"),
        "last_sample_date": row.get("last_sample_date") or payload.get("last_sample_date"),
        "retrieval_match_level": row.get("retrieval_match_level") or payload.get("retrieval_match_level"),
    }


def _default_learning_trace(rows: list | None, limit: int = 10) -> list:
    compacted: list[dict] = []
    for row in rows or []:
        compact = _compact_learning_trace_row(row)
        if compact:
            compacted.append(compact)
        if len(compacted) >= int(limit or 10):
            break
    return compacted


def _clean_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _action_value_lane(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    for key in ("learning_lane", "action_value_lane", "lane", "action_name"):
        value = _clean_text(row.get(key) or payload.get(key))
        if value:
            if value in {"scale", "increase"}:
                return "add"
            if value in {"close", "decrease"}:
                return "exit"
            if "execution" in value or "trigger" in value or "fill" in value:
                return "execution"
            return value
    return ""


def _contract_lifecycle_port(
    *,
    final_action: str,
    current_lots: int,
    target_lots: int,
    authority: dict,
) -> str:
    action = _clean_text(final_action)
    current = int(current_lots or 0)
    target = int(target_lots or 0)
    if bool(authority.get("conditional_trigger_authority")) or bool(authority.get("requires_intraday_confirmation")):
        return "conditional_monitor"
    if current == 0 and target != 0:
        return "open_add_new_risk"
    if current != 0 and target != 0 and (
        (current > 0 and target > current)
        or (current < 0 and target < current)
        or (current > 0 and target < 0)
        or (current < 0 and target > 0)
    ):
        return "open_add_new_risk"
    if current != 0 and target == current:
        return "hold"
    if current != 0 and (target == 0 or abs(target) < abs(current) or action in {"reduce", "exit", "close"}):
        return "reduce_exit"
    if action in {"open", "open_probe", "open_real", "scale", "add", "increase", "reverse"}:
        return "open_add_new_risk"
    return "wait"


def _pm_lifecycle_learning_trace(
    *,
    final_action: str,
    current_lots: int,
    target_lots: int,
    authority: dict,
    diagnostics: dict,
    selected_action_values: list,
    execution_contract_payload: dict,
) -> dict:
    lifecycle_port = _contract_lifecycle_port(
        final_action=final_action,
        current_lots=current_lots,
        target_lots=target_lots,
        authority=authority,
    )
    used_lanes = sorted({lane for lane in (_action_value_lane(row) for row in selected_action_values or []) if lane})
    memory_retrieval = diagnostics.get("final_action_memory_retrieval")
    memory_retrieval = memory_retrieval if isinstance(memory_retrieval, dict) else {}
    rejected = memory_retrieval.get("rejected_action_values")
    rejected_lanes = sorted({
        lane for lane in (_action_value_lane(row) for row in (rejected or [])) if lane
    })
    accepted_by_port = {
        "open_add_new_risk": ["open", "add", "scale", "increase"],
        "hold": ["hold"],
        "reduce_exit": ["reduce", "exit"],
        "conditional_monitor": ["conditional_monitor"],
        "wait": [],
    }.get(lifecycle_port, [])
    trace = {
        "trace_version": "agentquant.pm_lifecycle_learning_trace.v1",
        "contract_lifecycle_port": lifecycle_port,
        "rank_lifecycle": "open_add_new_risk" if lifecycle_port == "open_add_new_risk" else lifecycle_port,
        "used_lanes": used_lanes,
        "accepted_learning_lanes": accepted_by_port,
        "rejected_learning_lanes": rejected_lanes,
        "blocked_learning_lanes": (
            ["hold", "reduce", "exit", "execution", "conditional_monitor"]
            if lifecycle_port == "open_add_new_risk"
            else ["open", "add", "scale", "increase"]
        ),
        "memory_requirement_status": memory_retrieval.get("status"),
        "memory_requirements": diagnostics.get("final_action_memory_requirements")
        if isinstance(diagnostics.get("final_action_memory_requirements"), dict)
        else {},
        "hold_learning_decision": (
            diagnostics.get("holding_rebalance_control")
            if isinstance(diagnostics.get("holding_rebalance_control"), dict)
            else {}
        ),
        "reduce_exit_learning_decision": (
            diagnostics.get("winning_template_continuation")
            if isinstance(diagnostics.get("winning_template_continuation"), dict)
            else {}
        ),
        "open_add_learning_decision": (
            diagnostics.get("alpha_setup_ev_fusion")
            if isinstance(diagnostics.get("alpha_setup_ev_fusion"), dict)
            else {}
        ),
        "conditional_monitor_learning_decision": (
            diagnostics.get("conditional_monitor_probe_plan")
            if isinstance(diagnostics.get("conditional_monitor_probe_plan"), dict)
            else {}
        ),
        "execution_profile_learning_decision": (
            execution_contract_payload.get("execution_action_value_preference")
            if isinstance(execution_contract_payload.get("execution_action_value_preference"), dict)
            else {}
        ),
        "execution_profile_signal_direct_to_rank": False,
        "final_contract_effect_fields": [
            "final_action",
            "target_lots",
            "lots_delta",
            "reason_codes",
            "execution_profile",
            "requires_intraday_confirmation",
        ],
    }
    return trace


def _pm_lifecycle_learning_impact_delta(
    *,
    current_lots: int,
    target_lots: int,
    position_ratio: float,
    diagnostics: dict,
    execution_contract_payload: dict,
) -> dict:
    alpha_ev = diagnostics.get("alpha_setup_ev_fusion")
    alpha_ev = alpha_ev if isinstance(alpha_ev, dict) else {}
    holding = diagnostics.get("holding_rebalance_control")
    holding = holding if isinstance(holding, dict) else {}
    reduce_exit = diagnostics.get("winning_template_continuation")
    reduce_exit = reduce_exit if isinstance(reduce_exit, dict) else {}
    conditional = diagnostics.get("conditional_monitor_probe_plan")
    conditional = conditional if isinstance(conditional, dict) else {}
    execution_pref = execution_contract_payload.get("execution_action_value_preference")
    execution_pref = execution_pref if isinstance(execution_pref, dict) else {}
    pre_ratio = _safe_float(alpha_ev.get("pre_control_ratio"), _safe_float(holding.get("pre_control_ratio"), position_ratio))
    final_ratio = _safe_float(alpha_ev.get("final_ratio"), position_ratio)
    return {
        "trace_version": "agentquant.pm_lifecycle_learning_impact.v1",
        "current_lots": int(current_lots or 0),
        "target_lots": int(target_lots or 0),
        "lots_delta": int((target_lots or 0) - (current_lots or 0)),
        "pre_learning_position_ratio": pre_ratio,
        "final_target_position_ratio": float(position_ratio or 0.0),
        "position_ratio_delta": round(float(position_ratio or 0.0) - pre_ratio, 8),
        "open_add_rank_score_delta": _safe_float(
            alpha_ev.get("rank_score_open_add_learning_delta"),
            _safe_float(alpha_ev.get("learning_impact_delta"), 0.0),
        ),
        "alpha_setup_multiplier": alpha_ev.get("multiplier"),
        "alpha_setup_expectancy_lane": alpha_ev.get("expectancy_lane"),
        "hold_decision": holding.get("decision"),
        "hold_changes_position": bool(holding and holding.get("final_ratio") != holding.get("pre_control_ratio")),
        "reduce_exit_decision": reduce_exit.get("decision"),
        "reduce_exit_changes_position": bool(
            reduce_exit and reduce_exit.get("final_ratio") != reduce_exit.get("pre_control_ratio")
        ),
        "conditional_monitor_decision": conditional.get("decision"),
        "execution_profile_changed": bool(execution_pref.get("enabled")),
        "execution_profile_learning_direct_to_rank": False,
    }


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
    pm_lifecycle_trace = _pm_lifecycle_learning_trace(
        final_action=final_action,
        current_lots=current_lots,
        target_lots=target_lots,
        authority=authority,
        diagnostics=diagnostics,
        selected_action_values=selected_action_values,
        execution_contract_payload=execution_contract_payload,
    )
    pm_lifecycle_impact = _pm_lifecycle_learning_impact_delta(
        current_lots=current_lots,
        target_lots=target_lots,
        position_ratio=position_ratio,
        diagnostics=diagnostics,
        execution_contract_payload=execution_contract_payload,
    )
    scorecard_rank_trace = scorecard_side.get("lifecycle_learning_trace") or {}
    scorecard_learning_impact = scorecard_side.get("learning_impact_delta") or {}
    is_new_capital_port = pm_lifecycle_trace.get("contract_lifecycle_port") == "open_add_new_risk"
    lifecycle_learning_trace = (
        {**scorecard_rank_trace, "pm_final_contract_lifecycle_trace": pm_lifecycle_trace}
        if is_new_capital_port and isinstance(scorecard_rank_trace, dict) and scorecard_rank_trace
        else pm_lifecycle_trace
    )
    learning_impact_delta = (
        {**scorecard_learning_impact, "pm_lifecycle_impact_delta": pm_lifecycle_impact}
        if is_new_capital_port and isinstance(scorecard_learning_impact, dict) and scorecard_learning_impact
        else pm_lifecycle_impact
    )
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
            "capital_priority_score": scorecard_side.get("capital_priority_score"),
            "capital_priority_tier": scorecard_side.get("capital_priority_tier"),
            "rank_input_components": scorecard_side.get("rank_input_components") or {},
            "lifecycle_learning_trace": lifecycle_learning_trace,
            "learning_impact_delta": learning_impact_delta,
            "opportunity_score_components": scorecard_side.get("opportunity_score_components") or {},
            "side_priority": scorecard_side.get("side_priority"),
            "ticker_side_priority": scorecard_side.get("ticker_side_priority"),
            "side_priority_score": scorecard_side.get("side_priority_score"),
            "side_priority_semantics_version": scorecard_side.get("side_priority_semantics_version"),
            "side_priority_meaning": scorecard_side.get("side_priority_meaning"),
            "side_priority_is_not_capital_rank": bool(scorecard_side.get("side_priority_is_not_capital_rank", True)),
            "side_priority_is_not_trade_authority": bool(scorecard_side.get("side_priority_is_not_trade_authority", True)),
            "rank_capital_priority_real_budget_release": bool(
                authority.get("rank_capital_priority_real_budget_release")
            ),
            "rank_capital_priority_release_detail": (
                authority.get("rank_capital_priority_release_detail")
                if isinstance(authority.get("rank_capital_priority_release_detail"), dict)
                else {}
            ),
            "capital_allocation_reason": scorecard_side.get("capital_allocation_reason"),
            "pm_fusion_diagnostics": scorecard_side.get("pm_fusion_diagnostics") or {},
            "pm_conflict_resolution": scorecard_side.get("pm_conflict_resolution") or {},
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
            "pm_lifecycle_learning_trace": pm_lifecycle_trace,
            "pm_lifecycle_learning_impact_delta": pm_lifecycle_impact,
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
