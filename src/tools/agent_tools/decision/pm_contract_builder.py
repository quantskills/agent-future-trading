"""PM contract-building helpers.

These helpers are PM-owned tools. They do not write DB rows, artifacts, or
payloads; only the PM recommendation writer may persist the final contract.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from tools.agent_tools.decision.pm_lifecycle_learning_router import route_lifecycle_learning
from tools.agent_tools.decision.pm_position_transition import final_action_from_lots
from tools.common.final_action_semantics import (
    canonical_action_value_lane,
    contract_final_learning_lifecycle,
    derive_memory_requirements,
    filter_action_values_for_contract_learning,
)
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
    contract = {
        "id": row.get("id") or payload.get("id"),
        "action_value_id": row.get("id") or payload.get("id"),
        "scope_key": row.get("scope_key") or payload.get("scope_key"),
        "ticker": row.get("ticker") or payload.get("ticker"),
        "side": row.get("side") or payload.get("side"),
        "setup_type": row.get("setup_type") or payload.get("setup_type"),
        "action_name": row.get("action_name") or payload.get("action_name"),
        "canonical_action_family": row.get("canonical_action_family") or payload.get("canonical_action_family"),
        "learning_lane": row.get("learning_lane") or payload.get("learning_lane"),
        "action_value_lane": row.get("action_value_lane") or payload.get("action_value_lane"),
        "canonical_action_value": (
            row.get("canonical_action_value")
            if "canonical_action_value" in row
            else payload.get("canonical_action_value")
        ),
        "consumer_scope": row.get("consumer_scope") or payload.get("consumer_scope"),
        "canonical_action_value_source": (
            row.get("canonical_action_value_source")
            or payload.get("canonical_action_value_source")
        ),
        "evidence_scope": row.get("evidence_scope") or payload.get("evidence_scope"),
        "action_preference": row.get("action_preference") or payload.get("action_preference"),
        "memory_side_role": row.get("memory_side_role") or payload.get("memory_side_role"),
        "reward_mean": row.get("reward_mean") or payload.get("reward_mean"),
        "reward_sum": row.get("reward_sum") or payload.get("reward_sum"),
        "win_rate": row.get("win_rate") or payload.get("win_rate"),
        "sample_count": row.get("sample_count") or payload.get("sample_count"),
        "last_sample_date": row.get("last_sample_date") or payload.get("last_sample_date"),
        "retrieval_match_level": row.get("retrieval_match_level") or payload.get("retrieval_match_level"),
    }
    return contract


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


def _contract_increases_new_risk(current_lots: int, target_lots: int) -> bool:
    current = int(current_lots or 0)
    target = int(target_lots or 0)
    if target == current or target == 0:
        return False
    if current == 0:
        return True
    if (current > 0 and target < 0) or (current < 0 and target > 0):
        return True
    return abs(target) > abs(current)


def _non_rank_capital_deployment_summary(
    *,
    current_lots: int,
    target_lots: int,
    control_reasons: list[str],
) -> dict:
    current = int(current_lots or 0)
    target = int(target_lots or 0)
    reasons = sorted({str(reason) for reason in (control_reasons or []) if str(reason)} | {"non_new_risk_no_capital_rank"})
    return {
        "selected_for_capital_deployment": False,
        "capital_allocation_reason": "non_new_risk_no_capital_rank",
        "original_target_lots": target,
        "deployed_target_lots": target,
        "deployed_lots_delta": target - current,
        "reason_codes": reasons,
    }


def _contract_capital_deployment(
    *,
    execution_fields: dict,
    current_lots: int,
    target_lots: int,
    control_reasons: list[str],
) -> dict | None:
    deployment = execution_fields.get("capital_deployment")
    if not _contract_increases_new_risk(current_lots, target_lots):
        return _non_rank_capital_deployment_summary(
            current_lots=current_lots,
            target_lots=target_lots,
            control_reasons=control_reasons,
        )
    if isinstance(deployment, dict) and deployment:
        return dict(deployment)
    return None


def _contract_position_sizing_result(
    *,
    execution_fields: dict,
    ticker: str,
    current_lots: int,
    target_lots: int,
    position_ratio: float,
    margin_required: float,
    account_equity: float,
    lots_to_trade_reason: str | None,
    control_reasons: list[str],
) -> dict:
    sizing = execution_fields.get("position_sizing_result")
    if isinstance(sizing, dict) and sizing:
        return dict(sizing)
    current = int(current_lots or 0)
    target = int(target_lots or 0)
    return {
        "tool": "pm_contract_builder_position_sizing_summary",
        "ticker": ticker,
        "current_lots": current,
        "target_lots": target,
        "lots_delta": target - current,
        "lots_delta_abs": abs(target - current),
        "target_position_ratio": float(position_ratio or 0.0),
        "margin_required": float(margin_required or 0.0),
        "account_equity": float(account_equity or 0.0),
        "target_margin_ratio_estimate": (
            abs(float(margin_required or 0.0)) / float(account_equity)
            if _safe_float(account_equity, 0.0) > 0
            else 0.0
        ),
        "lots_to_trade_reason": lots_to_trade_reason or "target_plan",
        "control_reasons": sorted({str(reason) for reason in (control_reasons or []) if str(reason)}),
        "no_final_action_authority": True,
        "no_direction_override_authority": True,
    }


def _action_value_lane(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    for key in ("learning_lane", "action_value_lane", "lane"):
        value = _clean_text(row.get(key) or payload.get(key))
        if value:
            return value
    action = _clean_text(row.get("action_name") or payload.get("action_name"))
    if action:
        return canonical_action_value_lane(action)
    return ""


def _contract_lifecycle_port(
    *,
    final_action: str,
    current_lots: int,
    target_lots: int,
    authority: dict,
    reason_codes: list[str] | None = None,
) -> str:
    return contract_final_learning_lifecycle({
        "final_action": final_action,
        "current_lots": int(current_lots or 0),
        "target_lots": int(target_lots or 0),
        "conditional_trigger_authority": bool(authority.get("conditional_trigger_authority")),
        "requires_intraday_confirmation": bool(authority.get("requires_intraday_confirmation")),
        "can_execute_without_intraday_trigger": bool(
            authority.get("can_execute_without_intraday_trigger")
        ),
        "reason_codes": list(reason_codes or []),
    })


def _pm_lifecycle_learning_trace(
    *,
    final_action: str,
    current_lots: int,
    target_lots: int,
    decision_side: str,
    authority: dict,
    diagnostics: dict,
    memory_requirements: dict,
    final_route_action_values: list | None,
    select_learning_trace_action_values: Callable[[list | None, int], List[dict]],
    execution_contract_payload: dict,
    control_reasons: list[str],
) -> tuple[dict, list[dict]]:
    lifecycle_port = _contract_lifecycle_port(
        final_action=final_action,
        current_lots=current_lots,
        target_lots=target_lots,
        authority=authority,
        reason_codes=control_reasons,
    )
    final_route_values = final_route_action_values if isinstance(final_route_action_values, list) else []
    complete_pool_router = route_lifecycle_learning(
        lifecycle_port=lifecycle_port,
        action_values=final_route_values,
    )
    final_contract_learning_filter = filter_action_values_for_contract_learning(
        {
            "final_action": final_action,
            "current_lots": int(current_lots or 0),
            "target_lots": int(target_lots or 0),
            "lots_delta": int((target_lots or 0) - (current_lots or 0)),
            "side": decision_side,
            "conditional_trigger_authority": bool(authority.get("conditional_trigger_authority")),
            "requires_intraday_confirmation": bool(authority.get("requires_intraday_confirmation")),
            "can_execute_without_intraday_trigger": bool(
                authority.get("can_execute_without_intraday_trigger")
            ),
        },
        final_route_values,
    )
    decision_source_rows = [
        row
        for row in (final_contract_learning_filter.get("rows") or [])
        if isinstance(row, dict)
    ]
    selected_action_values = select_learning_trace_action_values(decision_source_rows, 10)
    selected_router = route_lifecycle_learning(
        lifecycle_port=lifecycle_port,
        action_values=selected_action_values,
    )
    selected_decision_indices = {
        int(index)
        for index in (
            selected_router.get("decision_learning_indices")
            or selected_router.get("accepted_indices")
            or []
        )
        if isinstance(index, int)
    }
    selected_action_values = [
        row for index, row in enumerate(selected_action_values)
        if index in selected_decision_indices
    ]
    selected_router = route_lifecycle_learning(
        lifecycle_port=lifecycle_port,
        action_values=selected_action_values,
    )
    lifecycle_router = dict(selected_router)
    for key in (
        "trigger_profile_learning_rows",
        "trigger_profile_learning",
        "trigger_profile_indices",
        "execution_profile_learning",
        "execution_profile_indices",
        "rejected_learning_rows",
        "rejected_learning",
        "rejected_indices",
    ):
        lifecycle_router[key] = complete_pool_router.get(key) or []
    for key in (
        "decision_learning_rows",
        "accepted_learning",
        "trigger_profile_learning_rows",
        "trigger_profile_learning",
        "execution_profile_learning",
    ):
        enriched_rows: list[dict] = []
        for row in lifecycle_router.get(key) or []:
            if not isinstance(row, dict):
                continue
            enriched = dict(row)
            if key in {"decision_learning_rows", "accepted_learning"}:
                source_index = enriched.get("source_index")
                source_row = (
                    selected_action_values[source_index]
                    if isinstance(source_index, int)
                    and 0 <= source_index < len(selected_action_values)
                    and isinstance(selected_action_values[source_index], dict)
                    else {}
                )
                source_payload = (
                    source_row.get("payload")
                    if isinstance(source_row.get("payload"), dict)
                    else {}
                )
                enriched["canonical_action_value"] = (
                    source_row.get("canonical_action_value")
                    if "canonical_action_value" in source_row
                    else source_payload.get("canonical_action_value")
                )
                enriched["consumer_scope"] = (
                    source_row.get("consumer_scope")
                    or source_payload.get("consumer_scope")
                )
            lane = _clean_text(enriched.get("lane"))
            if lane:
                enriched["learning_lane"] = lane
                enriched["action_value_lane"] = lane
            enriched_rows.append(enriched)
        lifecycle_router[key] = enriched_rows
    lifecycle_router["complete_step4_pool_routed_before_formal_selection"] = True
    lifecycle_router["complete_step4_pool_count"] = len(final_route_values)
    used_lanes = sorted({lane for lane in (_action_value_lane(row) for row in selected_action_values) if lane})
    memory_retrieval = diagnostics.get("final_action_memory_retrieval")
    memory_retrieval = memory_retrieval if isinstance(memory_retrieval, dict) else {}
    decision_learning_rows = (
        lifecycle_router.get("decision_learning_rows")
        if isinstance(lifecycle_router.get("decision_learning_rows"), list)
        else lifecycle_router.get("accepted_learning")
    )
    decision_learning_rows = decision_learning_rows if isinstance(decision_learning_rows, list) else []
    trigger_profile_learning_rows = (
        lifecycle_router.get("trigger_profile_learning_rows")
        if isinstance(lifecycle_router.get("trigger_profile_learning_rows"), list)
        else lifecycle_router.get("trigger_profile_learning")
    )
    trigger_profile_learning_rows = (
        trigger_profile_learning_rows if isinstance(trigger_profile_learning_rows, list) else []
    )
    rejected = (
        lifecycle_router.get("rejected_learning_rows")
        if isinstance(lifecycle_router.get("rejected_learning_rows"), list)
        else lifecycle_router.get("rejected_learning")
    )
    if not isinstance(rejected, list):
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
    hold_learning_decision = {}
    reduce_exit_learning_decision = {}
    open_add_learning_decision = {}
    conditional_monitor_learning_decision = {}
    if lifecycle_port == "open_add_new_risk":
        open_add_learning_decision = (
            diagnostics.get("alpha_setup_ev_fusion")
            if isinstance(diagnostics.get("alpha_setup_ev_fusion"), dict)
            else {}
        )
    elif lifecycle_port == "hold":
        hold_learning_decision = _final_lifecycle_control_detail(
            diagnostics,
            lifecycle_port=lifecycle_port,
        )
    elif lifecycle_port == "reduce_exit":
        reduce_exit_learning_decision = _final_lifecycle_control_detail(
            diagnostics,
            lifecycle_port=lifecycle_port,
        )
    elif lifecycle_port == "conditional_monitor":
        conditional_monitor_learning_decision = (
            diagnostics.get("conditional_monitor_probe_plan")
            if isinstance(diagnostics.get("conditional_monitor_probe_plan"), dict)
            else {}
        )
    trace = {
        "trace_version": "agentquant.pm_lifecycle_learning_trace.v1",
        "contract_lifecycle_port": lifecycle_port,
        "pm_lifecycle_action_port": lifecycle_router.get("pm_lifecycle_action_port"),
        "router_source": "step6_final_contract_lifecycle",
        "rank_lifecycle": "open_add_new_risk" if lifecycle_port == "open_add_new_risk" else lifecycle_port,
        "used_lanes": used_lanes,
        "accepted_learning_lanes": accepted_by_port,
        "decision_learning_rows": decision_learning_rows,
        "trigger_profile_learning": trigger_profile_learning_rows,
        "trigger_profile_learning_rows": trigger_profile_learning_rows,
        "trigger_profile_indices": list(lifecycle_router.get("trigger_profile_indices") or []),
        "rejected_learning": rejected if isinstance(rejected, list) else [],
        "rejected_learning_lanes": rejected_lanes,
        "pm_lifecycle_learning_router": lifecycle_router,
        "blocked_learning_lanes": (
            ["hold", "reduce", "exit", "execution", "conditional_monitor"]
            if lifecycle_port == "open_add_new_risk"
            else ["open", "add", "scale", "increase"]
        ),
        "execution_profile_learning_direct_to_rank": False,
        "trigger_profile_learning_direct_to_rank": False,
        "memory_requirement_status": memory_retrieval.get("status"),
        "memory_requirements": dict(memory_requirements or {}),
        "hold_learning_decision": hold_learning_decision,
        "reduce_exit_learning_decision": reduce_exit_learning_decision,
        "open_add_learning_decision": open_add_learning_decision,
        "conditional_monitor_learning_decision": conditional_monitor_learning_decision,
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
    return trace, selected_action_values


def _control_changed_ratio(detail: dict, *, before_key: str, after_key: str) -> bool:
    if not isinstance(detail, dict):
        return False
    if detail.get(before_key) is None or detail.get(after_key) is None:
        return False
    return abs(
        _safe_float(detail.get(after_key), 0.0)
        - _safe_float(detail.get(before_key), 0.0)
    ) > 1e-12


def _final_lifecycle_control_detail(
    diagnostics: dict,
    *,
    lifecycle_port: str,
) -> dict:
    """Return the diagnostic that actually formed the final lifecycle result."""
    holding = diagnostics.get("holding_rebalance_control")
    holding = holding if isinstance(holding, dict) else {}
    continuation = diagnostics.get("winning_template_continuation")
    continuation = continuation if isinstance(continuation, dict) else {}
    holding_has_decision = bool(str(holding.get("decision") or "").strip())
    continuation_has_decision = bool(str(continuation.get("decision") or "").strip())
    if lifecycle_port == "reduce_exit":
        holding_changed = _control_changed_ratio(
            holding,
            before_key="raw_target_ratio",
            after_key="final_target_ratio",
        )
        continuation_changed = _control_changed_ratio(
            continuation,
            before_key="pre_control_ratio",
            after_key="final_ratio",
        )
        if holding_has_decision and holding_changed:
            return holding
        if continuation_has_decision and continuation_changed:
            return continuation
    if holding_has_decision:
        return holding
    if continuation_has_decision:
        return continuation
    return {}


def _pm_lifecycle_learning_impact_delta(
    *,
    current_lots: int,
    target_lots: int,
    position_ratio: float,
    lifecycle_port: str,
    diagnostics: dict,
    capital_deployment: dict | None,
    execution_contract_payload: dict,
) -> dict:
    alpha_ev = diagnostics.get("alpha_setup_ev_fusion")
    alpha_ev = alpha_ev if isinstance(alpha_ev, dict) else {}
    lifecycle_control = _final_lifecycle_control_detail(
        diagnostics,
        lifecycle_port=lifecycle_port,
    )
    conditional = diagnostics.get("conditional_monitor_probe_plan")
    conditional = conditional if isinstance(conditional, dict) else {}
    deployment = capital_deployment if isinstance(capital_deployment, dict) else {}
    deployment_impact = (
        deployment.get("learning_impact_delta")
        if isinstance(deployment.get("learning_impact_delta"), dict)
        else {}
    )
    execution_pref = execution_contract_payload.get("execution_action_value_preference")
    execution_pref = execution_pref if isinstance(execution_pref, dict) else {}
    relevant_detail = (
        alpha_ev
        if lifecycle_port == "open_add_new_risk"
        else lifecycle_control
        if lifecycle_port in {"hold", "reduce_exit"}
        else conditional
        if lifecycle_port == "conditional_monitor"
        else {}
    )
    pre_ratio = _safe_float(
        relevant_detail.get("pre_control_ratio"),
        _safe_float(relevant_detail.get("raw_target_ratio"), position_ratio),
    )
    hold_decision = (
        lifecycle_control.get("decision") if lifecycle_port == "hold" else None
    )
    reduce_exit_decision = (
        lifecycle_control.get("decision") if lifecycle_port == "reduce_exit" else None
    )
    conditional_monitor_decision = (
        conditional.get("decision") if lifecycle_port == "conditional_monitor" else None
    )
    return {
        "trace_version": "agentquant.pm_lifecycle_learning_impact.v1",
        "current_lots": int(current_lots or 0),
        "target_lots": int(target_lots or 0),
        "lots_delta": int((target_lots or 0) - (current_lots or 0)),
        "pre_learning_position_ratio": pre_ratio,
        "final_target_position_ratio": float(position_ratio or 0.0),
        "position_ratio_delta": round(float(position_ratio or 0.0) - pre_ratio, 8),
        "open_add_rank_score_delta": (
            _safe_float(
                deployment_impact.get("rank_score_open_add_learning_delta"),
                0.0,
            )
            if lifecycle_port == "open_add_new_risk"
            else 0.0
        ),
        "alpha_setup_multiplier": (
            alpha_ev.get("multiplier") if lifecycle_port == "open_add_new_risk" else None
        ),
        "alpha_setup_expectancy_lane": (
            alpha_ev.get("expectancy_lane") if lifecycle_port == "open_add_new_risk" else None
        ),
        "hold_decision": hold_decision,
        "hold_changes_position": False,
        "reduce_exit_decision": reduce_exit_decision,
        "reduce_exit_changes_position": bool(
            lifecycle_port == "reduce_exit" and int(current_lots or 0) != int(target_lots or 0)
        ),
        "conditional_monitor_decision": conditional_monitor_decision,
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
    contract_code: str | None = None,
    final_contract_scope: dict | None = None,
    select_learning_trace_action_values: Callable[[list | None, int], List[dict]] | None = None,
    safe_float: Callable[[Any, float], float] | None = None,
    futures_action_cls: Any | None = None,
) -> Dict[str, Any]:
    """Build the PM final_action_contract from already-computed PM components."""
    diagnostics = control_diagnostics if isinstance(control_diagnostics, dict) else {}
    scorecard = opportunity_scorecard if isinstance(opportunity_scorecard, dict) else {}
    execution_contract_payload = dict(execution_contract_fields) if isinstance(execution_contract_fields, dict) else {}
    final_scope = dict(final_contract_scope) if isinstance(final_contract_scope, dict) else {}
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
    decision_side = (
        target_side
        if target_side in {"long", "short"}
        else "long"
        if int(current_lots or 0) > 0
        else "short"
        if int(current_lots or 0) < 0
        else str(scorecard.get("preferred_side") or "").strip().lower()
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
            "invalidation_level",
            "valid_until",
            "requires_intraday_confirmation",
            "can_execute_without_intraday_trigger",
            "execution_action_value_preference",
        )
        if key in execution_contract_payload
    }
    for key in (
        "requires_intraday_confirmation",
        "can_execute_without_intraday_trigger",
    ):
        if key not in execution_fields and key in authority:
            execution_fields[key] = bool(authority.get(key))
    final_lifecycle_contract = {
        "final_action": final_action,
        "current_lots": int(current_lots or 0),
        "target_lots": int(target_lots or 0),
        "lots_delta": int((target_lots or 0) - (current_lots or 0)),
        "side": decision_side,
        "conditional_trigger_authority": bool(authority.get("conditional_trigger_authority")),
        "requires_intraday_confirmation": bool(
            execution_fields.get("requires_intraday_confirmation")
        ),
        "can_execute_without_intraday_trigger": bool(
            execution_fields.get("can_execute_without_intraday_trigger")
        ),
        "reason_codes": sorted(reason_codes),
    }
    memory_requirements = derive_memory_requirements(final_lifecycle_contract)
    memory_retrieval = (
        diagnostics.get("final_action_memory_retrieval")
        if isinstance(diagnostics.get("final_action_memory_retrieval"), dict)
        else {}
    )
    action_value = recommendation_intent.get("action") if isinstance(recommendation_intent, dict) else "hold"
    if futures_action_cls is not None:
        try:
            action_value = futures_action_cls(str(action_value))
        except Exception:
            pass
    pm_lifecycle_trace, selected_action_values = _pm_lifecycle_learning_trace(
        final_action=final_action,
        current_lots=current_lots,
        target_lots=target_lots,
        decision_side=decision_side,
        authority=authority,
        diagnostics=diagnostics,
        memory_requirements=memory_requirements,
        final_route_action_values=alpha_setup_action_values,
        select_learning_trace_action_values=select_learning_trace,
        execution_contract_payload=execution_contract_payload,
        control_reasons=sorted(reason_codes),
    )
    capital_deployment = _contract_capital_deployment(
        execution_fields=execution_contract_payload,
        current_lots=current_lots,
        target_lots=target_lots,
        control_reasons=sorted(reason_codes),
    )
    pm_lifecycle_impact = _pm_lifecycle_learning_impact_delta(
        current_lots=current_lots,
        target_lots=target_lots,
        position_ratio=position_ratio,
        lifecycle_port=str(pm_lifecycle_trace.get("contract_lifecycle_port") or "wait"),
        diagnostics=diagnostics,
        capital_deployment=capital_deployment,
        execution_contract_payload=execution_contract_payload,
    )
    position_sizing_result = _contract_position_sizing_result(
        execution_fields=execution_contract_payload,
        ticker=ticker,
        current_lots=current_lots,
        target_lots=target_lots,
        position_ratio=position_ratio,
        margin_required=margin_required,
        account_equity=account_equity,
        lots_to_trade_reason=lots_to_trade_reason,
        control_reasons=sorted(reason_codes),
    )
    contract = {
        "contract_version": FINAL_ACTION_CONTRACT_VERSION,
        "ticker": ticker,
        "contract_code": contract_code,
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
        "setup_type": final_scope.get("setup_type"),
        "horizon_class": final_scope.get("horizon_class"),
        "expected_horizon_days": final_scope.get("expected_horizon_days"),
        "market_regime": final_scope.get("market_regime"),
        "invalidation_level": final_scope.get("invalidation_level"),
        "position_invalidation_level": final_scope.get("position_invalidation_level"),
        "exit_hint": final_scope.get("exit_hint"),
        "atr_stop_distance": final_scope.get("atr_stop_distance"),
        "evidence_used": {
            "scorecard_preferred_side": scorecard.get("preferred_side"),
            "scorecard_state": scorecard_side.get("final_state"),
            "scorecard_score": scorecard_side.get("score"),
            "opportunity_score": scorecard_side.get("opportunity_score", scorecard_side.get("score")),
            "opportunity_score_components": scorecard_side.get("opportunity_score_components") or {},
            "analyst_direction_evidence": scorecard_side.get("analyst_direction_evidence") or {},
            "direction_evidence_strength": scorecard_side.get("direction_evidence_strength"),
            "direction_evidence_components": scorecard_side.get("direction_evidence_components") or {},
            "direction_evidence_boundary": scorecard_side.get("direction_evidence_boundary"),
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
            "pm_lifecycle_learning_router": (
                pm_lifecycle_trace.get("pm_lifecycle_learning_router")
                if isinstance(pm_lifecycle_trace.get("pm_lifecycle_learning_router"), dict)
                else {}
            ),
            "trigger_profile_learning": pm_lifecycle_trace.get("trigger_profile_learning") or [],
            "pm_lifecycle_learning_trace": pm_lifecycle_trace,
            "pm_lifecycle_learning_impact_delta": pm_lifecycle_impact,
        },
        **execution_fields,
        "execution_profile": execution_contract_payload.get("execution_profile") or "",
        "entry_trigger": execution_contract_payload.get("entry_trigger") or "",
        "invalidation": execution_contract_payload.get("invalidation") or "",
        **({"capital_deployment": capital_deployment} if isinstance(capital_deployment, dict) else {}),
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
    evidence_used = contract["evidence_used"]
    evidence_used["position_sizing_result"] = position_sizing_result
    top_level_side_priority = scorecard.get("side_priority") if isinstance(scorecard.get("side_priority"), dict) else {}
    top_level_ticker_side_priority = (
        scorecard.get("ticker_side_priority") if isinstance(scorecard.get("ticker_side_priority"), dict) else {}
    )
    pm_side_selection_present = bool(
        scorecard_side.get("side_priority") is not None
        or scorecard_side.get("ticker_side_priority") is not None
        or top_level_side_priority
        or top_level_ticker_side_priority
    )
    for key in (
        "side_priority",
        "ticker_side_priority",
        "side_priority_score",
        "candidate_quality",
        "candidate_layer_hint",
        "side_priority_semantics_version",
        "side_priority_meaning",
        "side_priority_is_not_capital_rank",
        "side_priority_is_not_trade_authority",
    ):
        value = scorecard_side.get(key)
        if value is None and key == "side_priority":
            value = top_level_side_priority.get(target_side)
        if value is None and key == "ticker_side_priority":
            value = top_level_ticker_side_priority.get(target_side)
        if value is None and key == "side_priority_score" and pm_side_selection_present:
            value = scorecard_side.get("candidate_quality")
        if value is not None and value != "":
            evidence_used[key] = value
    return contract
