from __future__ import annotations

"""Deterministic semantics for the PM final_action_contract lifecycle.

This module is intentionally narrow: it does not call an LLM, sign contracts,
submit orders, write accounting facts, or write research memory. It provides one
shared interpretation layer for analyst evidence boundaries, signal collection,
PM contracts, audit, execution, settlement expectations, review, research, and
protocol checks.
"""

from typing import Any, Iterable, Mapping


NO_TRADE_ACTIONS = {"", "wait", "hold", "no_trade", "flat"}
CONDITIONAL_ACTIONS = {"conditional_probe", "conditional_monitor", "watch_trigger"}
OPEN_ACTIONS = {"open", "open_long", "open_short", "open_probe", "open_real"}
INCREASE_ACTIONS = {"add", "scale", "increase"}
DECREASE_ACTIONS = {"reduce", "trim", "decrease", "reduce_position", "scale_down", "reduce_only"}
EXIT_ACTIONS = {"exit", "close", "close_long", "close_short", "close_position", "risk_exit", "flatten"}
TRADE_ACTIONS = OPEN_ACTIONS | INCREASE_ACTIONS | DECREASE_ACTIONS | EXIT_ACTIONS | CONDITIONAL_ACTIONS

MEMORY_LANES = {"open", "add", "scale", "increase", "hold", "reduce", "exit", "execution", "conditional_monitor"}
PM_MEMORY_LANES = {"open", "add", "scale", "increase", "hold", "reduce", "exit", "conditional_monitor"}
MEMORY_SIDE_ROLES = {
    "target_side",
    "current_position_side",
    "trigger_side",
    "historical_sample_side",
}

DIRECT_AUTHORITY_TYPES = {"real_budget_entry", "scale", "add", "reduce", "exit", "risk_exit"}
PROBE_AUTHORITY_TYPES = {"exploration_probe"}
BLOCKING_AUTHORITY_TYPES = {"", "watchlist_only", "no_trade", "not_applicable", "analysis_or_watchlist_only"}

HARD_BLOCK_REASONS = {
    "pm_risk_gate_block",
    "pm_risk_gate_reduce_only",
    "pm_opportunity_scorecard_no_trade",
    "pm_text_no_trade_blocks_new_entry",
    "pm_text_no_entry_trigger_blocks_new_entry",
    "pm_text_watchlist_only_blocks_new_entry",
    "danger_zone_ban",
    "net_exposure_limit",
    "margin_insufficient",
    "critical_data_gap",
    "data_price_anomaly",
    "price_anomaly",
    "limit_locked_no_fill",
    "delivery_month_new_entry_block",
    "contract_expiry_hard_block",
    "future_data_contamination",
    "missing_pm_final_action_contract",
    "final_contract_authority_source_mismatch",
    "final_contract_authority_missing_or_not_met",
    "final_contract_authority_not_met",
    "missing_final_contract_authority",
    "final_contract_authority_watchlist_only",
    "final_contract_authority_real_entry_not_allowed",
    "final_action_contract_watch_for_trigger_probe_block",
    "final_contract_authority_probe_lacks_current_evidence",
    "position_budget_authority_not_met",
    "minimum_real_trade_margin_not_reachable",
    "minimum_real_trade_no_feasible_lot",
    "minimum_one_lot_probe_risk_budget_block",
    "exploration_probe_no_feasible_lot",
    "pm_risk_gate_scale_to_zero",
    "market_rule_block",
    "market_rule_or_execution_block",
    "near_expiry_new_entry_block",
    "watch_for_trigger_cannot_open_position",
}

CANDIDATE_REASONS = {
    "pm_watch_for_trigger_probe_cap",
    "scorecard_current_tradeable_probe_seed",
    "conditional_monitor_probe_seed",
    "conditional_watch",
    "conditional_monitor",
    "candidate_routed_to_conditional_monitor",
}

SOFT_LIMIT_REASONS = {
    "alpha_setup_open_action_value_missing",
    "single_high_quality_probe_only",
    "horizon_consistency_probe_cap",
    "market_confirmation_quality_gate",
    "market_confirmation_conflict",
    "weak_signal_combo_probe_cap",
    "side_performance_probe_cap",
    "business_quality_probe_only",
    "business_quality_deployable",
    "business_quality_observe_or_block",
    "pm_risk_gate_soft_probe_floor",
    "controlled_probe_below_min_entry_kept",
    "unknown_alpha_probe",
    "soft_block_converted_to_probe_only",
    "weak_ticker_side_quality_gate",
    "weak_ticker_side_cap",
    "strategy_memory_weak_block",
    "strict_ticker_side_quality_gate",
    "side_performance_block",
    "conditional_performance_block",
    "adaptive_policy_block",
    "provisional_policy_block",
    "learned_underperformance_block",
    "analyst_quality_low_tradeability",
    "business_quality_below_probe",
    "insufficient_quality_support",
    "low_quality_news_driven_trade",
    "news_only_directional_trade",
    "news_without_fundamental_anchor",
    "cold_start_weak_combo_block",
    "weak_conditional_combo_cap",
    "market_confirmation_soft_limit",
    "market_confirmation_data_gap",
    "trade_frequency_control",
    "trade_churn_cost_control",
    "weak_signal_combo",
    "opportunity_quality_position_sizing",
    "alpha_setup_ev_fusion",
    "market_confirmation_below_probe_threshold",
    "market_confirmation_below_release_threshold",
    "market_confirmation_score_below_probe_threshold",
    "high_quality_learning_evidence_required",
    "confirmation_below_alpha_release_boost_threshold",
    "missing_pretrade_invalidation",
    "missing_explicit_stop_for_alpha_release_boost",
    "generic_memory_cannot_trigger_alpha_release_boost",
    "strategy_memory_watchlist_cap",
    "no_analyst_support_for_target",
    "analyst_signal_conflict",
    "static_side_cap",
    "protected_memory_evidence_rejected",
    "daily_tradeability_watchlist_only",
    "pm_watch_for_trigger_not_tradeable",
    "horizon_consistency_requires_short_timing",
    "minimum_new_entry_threshold",
    "real_probe_qualification_not_met",
    "position_lifecycle_failed",
    "new_position_loss_revalidation_failed",
    "exploration_probe_reconfirm_failed",
    "exploration_probe_reconfirm_reduce",
    "horizon_consistency_failed_losing_hold",
    "position_lifecycle_loss_revalidation_failed",
    "position_lifecycle_probe_expired",
    "minimum_rebalance_threshold",
    "fundamental_anchor_rebalance_cap",
    "reverse_requires_stronger_evidence",
    "ticker_loss_control",
    "drawdown_control",
}

RELEASE_REASONS = {
    "conditional_trigger_authority",
    "qualified_positive_expectancy",
    "positive_expectancy_scale",
    "positive_open_action_value_seed",
    "real_probe_positive_or_strong_confirmation_release",
    "fast_candidate_alpha_probe",
    "capital_utilization_memory_protected",
    "capital_utilization_same_side_add_on",
    "mature_alpha_release",
    "mature_alpha_with_invalidation",
    "high_quality_bearish_short_probe",
    "high_quality_news_with_invalidation",
    "high_quality_memory",
    "high_quality_or_triggered_candidate_not_landed",
    "minimum_one_lot_probe",
    "minimum_real_trade_margin_floor_applied",
    "exploration_probe_probe_floor_applied",
    "correct_probe",
}

EXECUTION_RESULT_REASONS = {
    "intraday_trigger_not_met",
    "intraday_waiting_for_trigger",
    "intraday_trigger_confirmed",
    "intraday_pullback_confirmed",
    "intraday_vwap_confirmed",
    "intraday_immediate_execution",
    "intraday_event_immediate_execution",
    "partial_fill",
    "execution_failed",
    "execution_skipped",
}

FORBIDDEN_ANALYST_TRADE_AUTHORITY_KEYS = {
    "final_action",
    "final_action_contract",
    "target_lots",
    "current_lots",
    "lots_delta",
    "lots_delta_abs",
    "target_position_ratio",
    "target_margin_ratio",
    "margin_required",
    "authority_type",
    "audit_verdict",
    "capital_allocation_reason",
    "opportunity_rank",
    "conditional_trigger_authority",
    "requires_intraday_confirmation",
    "can_execute_without_intraday_trigger",
    "reason_codes",
}


def _clean(value: Any) -> str:
    return str(value or "").strip().lower()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "ok"}
    return bool(value)


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _dedupe(values: Iterable[Any] | None) -> list[str]:
    return sorted({text for text in (_clean(item) for item in (values or [])) if text})


def _side_from_lots(value: int) -> str:
    if value > 0:
        return "long"
    if value < 0:
        return "short"
    return ""


def _compact_requirement(
    *,
    lane: str,
    side: str,
    side_role: str,
    must_land: bool,
    reason: str,
) -> dict[str, Any]:
    lane_value = _clean(lane)
    side_value = _clean(side)
    role_value = _clean(side_role)
    return {
        "lane": lane_value,
        "learning_lane": lane_value,
        "action_value_lane": lane_value,
        "side": side_value,
        "memory_side_role": role_value,
        "must_land_in_pm_contract": bool(must_land),
        "reason": _clean(reason),
    }


def reason_codes_from(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        raw = value.get("reason_codes")
        if raw is None:
            raw = value.get("audit_reason_codes")
        if raw is None:
            raw = value.get("control_reasons")
    else:
        raw = value
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, Iterable):
        return _dedupe(raw)
    return [_clean(raw)]


def classify_reason_codes(reasons: Iterable[Any] | Mapping[str, Any] | None) -> dict[str, Any]:
    cleaned = reason_codes_from(reasons)
    hard_blocks = [reason for reason in cleaned if reason in HARD_BLOCK_REASONS or reason.startswith("hard_")]
    candidates = [reason for reason in cleaned if reason in CANDIDATE_REASONS]
    releases = [reason for reason in cleaned if reason in RELEASE_REASONS or reason.startswith("alpha_release_")]
    execution_results = [reason for reason in cleaned if reason in EXECUTION_RESULT_REASONS]
    soft_limits = [
        reason
        for reason in cleaned
        if reason not in hard_blocks
        and reason not in candidates
        and reason not in releases
        and reason not in execution_results
        and (
            reason in SOFT_LIMIT_REASONS
            or reason.endswith("_probe_cap")
            or reason.endswith("_probe_only")
            or reason.endswith("_scale_down")
            or reason.endswith("_quality_gate")
            or reason.endswith("_cap")
        )
    ]
    known = set(hard_blocks) | set(candidates) | set(releases) | set(execution_results) | set(soft_limits)
    diagnostic_reasons = [reason for reason in cleaned if reason not in known]
    return {
        "contract": "final_action_semantics.reason_codes.v1",
        "reason_codes": cleaned,
        "hard_block_reasons": hard_blocks,
        "candidate_reasons": candidates,
        "soft_limit_reasons": soft_limits,
        "release_reasons": releases,
        "execution_result_reasons": execution_results,
        "diagnostic_reasons": diagnostic_reasons,
        "hard_block": bool(hard_blocks),
    }


def _current_target_delta(contract: Mapping[str, Any]) -> tuple[int, int, int]:
    current_lots = _int(contract.get("current_lots"), 0)
    target_lots = _int(contract.get("target_lots"), current_lots)
    lots_delta = _int(contract.get("lots_delta"), target_lots - current_lots)
    return current_lots, target_lots, lots_delta


def contract_has_lot_change(contract: Mapping[str, Any]) -> bool:
    current_lots, target_lots, lots_delta = _current_target_delta(contract)
    return target_lots != current_lots or lots_delta != 0


def contract_has_trade_intent(contract: Mapping[str, Any]) -> bool:
    if not isinstance(contract, Mapping):
        return False
    action = _clean(contract.get("final_action"))
    if action in TRADE_ACTIONS:
        return True
    return contract_has_lot_change(contract)


def is_conditional_monitor_contract(contract: Mapping[str, Any]) -> bool:
    if not isinstance(contract, Mapping):
        return False
    action = _clean(contract.get("final_action"))
    requires_intraday = _bool(contract.get("requires_intraday_confirmation"))
    can_execute_without_intraday = _bool(contract.get("can_execute_without_intraday_trigger"))
    explicit_authority = _bool(contract.get("conditional_trigger_authority"))
    if action in CONDITIONAL_ACTIONS:
        return True
    return bool(
        explicit_authority
        and requires_intraday
        and not can_execute_without_intraday
    )


def classify_final_action_contract(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    contract = contract if isinstance(contract, Mapping) else {}
    reason_summary = classify_reason_codes(contract)
    action = _clean(contract.get("final_action"))
    authority_type = _clean(contract.get("authority_type"))
    current_lots, target_lots, lots_delta = _current_target_delta(contract)
    lot_change = target_lots != current_lots or lots_delta != 0
    conditional_monitor = is_conditional_monitor_contract(contract)
    hard_blocks = list(reason_summary["hard_block_reasons"])
    semantic_errors: list[str] = []
    if lots_delta != target_lots - current_lots:
        semantic_errors.append("final_action_contract_lots_delta_mismatch")
        hard_blocks.append("final_action_contract_lots_delta_mismatch")
    if authority_type in BLOCKING_AUTHORITY_TYPES and lot_change and not conditional_monitor:
        hard_blocks.append("blocking_authority_type_for_lot_change")

    lifecycle_state = "ordinary_hold"
    execution_permission = "no_trade"
    requires_intraday_result = False
    can_submit_order_now = False

    if hard_blocks:
        lifecycle_state = "hard_block"
        execution_permission = "blocked"
    elif conditional_monitor:
        lifecycle_state = "conditional_monitor"
        execution_permission = "monitor_intraday"
        requires_intraday_result = True
    elif action in OPEN_ACTIONS:
        lifecycle_state = "open"
        execution_permission = "direct_execute" if lot_change else "no_trade"
        can_submit_order_now = lot_change
    elif action in INCREASE_ACTIONS:
        lifecycle_state = "increase"
        execution_permission = "direct_execute" if lot_change else "no_trade"
        can_submit_order_now = lot_change
    elif action in DECREASE_ACTIONS:
        lifecycle_state = "decrease"
        execution_permission = "direct_execute" if lot_change else "no_trade"
        can_submit_order_now = lot_change
    elif action in EXIT_ACTIONS:
        lifecycle_state = "exit"
        execution_permission = "direct_execute" if current_lots != 0 or lot_change else "no_trade"
        can_submit_order_now = execution_permission == "direct_execute"
    elif action in NO_TRADE_ACTIONS:
        lifecycle_state = "ordinary_hold"
        execution_permission = "no_trade"
    elif lot_change:
        lifecycle_state = "unknown_trade_action"
        execution_permission = "blocked"
        hard_blocks.append("unknown_trade_action")

    return {
        "contract": "final_action_semantics.contract.v1",
        "action": action,
        "authority_type": authority_type,
        "current_lots": current_lots,
        "target_lots": target_lots,
        "lots_delta": lots_delta,
        "lot_change": lot_change,
        "lifecycle_state": lifecycle_state,
        "execution_permission": execution_permission,
        "requires_intraday_result": requires_intraday_result,
        "can_submit_order_now": can_submit_order_now,
        "can_monitor_intraday": execution_permission == "monitor_intraday",
        "blocked": execution_permission == "blocked",
        "hard_block_reasons": sorted(set(hard_blocks)),
        "soft_limit_reasons": reason_summary["soft_limit_reasons"],
        "candidate_reasons": reason_summary["candidate_reasons"],
        "release_reasons": reason_summary["release_reasons"],
        "execution_result_reasons": reason_summary["execution_result_reasons"],
        "diagnostic_reasons": reason_summary["diagnostic_reasons"],
        "semantic_errors": sorted(set(semantic_errors)),
    }


def _infer_memory_lifecycle(contract: Mapping[str, Any], semantics: Mapping[str, Any]) -> str:
    action = _clean(semantics.get("action") or contract.get("final_action"))
    current_lots = _int(semantics.get("current_lots"), 0)
    target_lots = _int(semantics.get("target_lots"), current_lots)
    lots_delta = _int(semantics.get("lots_delta"), target_lots - current_lots)
    if is_conditional_monitor_contract(contract):
        return "conditional_monitor"
    if action in OPEN_ACTIONS:
        return "open"
    if action in INCREASE_ACTIONS:
        return "increase"
    if action in DECREASE_ACTIONS:
        return "decrease"
    if action in EXIT_ACTIONS:
        return "exit"
    if action in NO_TRADE_ACTIONS:
        if current_lots and target_lots == current_lots:
            return "ordinary_hold"
        if current_lots and target_lots == 0:
            return "exit"
        return "ordinary_hold"
    if lots_delta:
        current_side = _side_from_lots(current_lots)
        target_side = _side_from_lots(target_lots)
        if current_lots and target_lots == 0:
            return "exit"
        if current_side and target_side == current_side and abs(target_lots) < abs(current_lots):
            return "decrease"
        if current_side and target_side == current_side and abs(target_lots) > abs(current_lots):
            return "increase"
        if target_side:
            return "open"
    return _clean(semantics.get("lifecycle_state") or "ordinary_hold")


def derive_memory_requirements(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the PM memory lanes and side roles required by this contract.

    The contract lifecycle determines what ``side`` means.  For new risk it is
    the target side; for position management it is the current position side; for
    conditional monitoring it is the trigger side.  This function is read-only:
    it does not fetch memory, sign a contract, trade, account, or write research.
    """
    contract = contract if isinstance(contract, Mapping) else {}
    semantics = classify_final_action_contract(contract)
    current_lots = int(semantics.get("current_lots") or 0)
    target_lots = int(semantics.get("target_lots") or 0)
    current_side = _side_from_lots(current_lots)
    target_side = _side_from_lots(target_lots)
    action = _clean(semantics.get("action"))
    lifecycle = _infer_memory_lifecycle(contract, semantics)
    requirements: list[dict[str, Any]] = []
    audit_only: list[dict[str, Any]] = []

    if lifecycle == "conditional_monitor":
        trigger_side = target_side or current_side or _clean(contract.get("side"))
        if trigger_side in {"long", "short"}:
            requirements.append(
                _compact_requirement(
                    lane="conditional_monitor",
                    side=trigger_side,
                    side_role="trigger_side",
                    must_land=True,
                    reason="conditional_monitor_trigger_side_memory",
                )
            )
            audit_only.append(
                _compact_requirement(
                    lane="open",
                    side=trigger_side,
                    side_role="trigger_side",
                    must_land=False,
                    reason="conditional_monitor_open_context",
                )
            )
    elif lifecycle == "open":
        if target_side in {"long", "short"}:
            requirements.append(
                _compact_requirement(
                    lane="open",
                    side=target_side,
                    side_role="target_side",
                    must_land=True,
                    reason="new_open_target_side_memory",
                )
            )
    elif lifecycle == "increase":
        if target_side in {"long", "short"}:
            requirements.append(
                _compact_requirement(
                    lane="add",
                    side=target_side,
                    side_role="target_side",
                    must_land=True,
                    reason="increase_target_side_add_memory",
                )
            )
            requirements.append(
                _compact_requirement(
                    lane="open",
                    side=target_side,
                    side_role="target_side",
                    must_land=True,
                    reason="increase_target_side_open_memory",
                )
            )
        if current_side in {"long", "short"}:
            requirements.append(
                _compact_requirement(
                    lane="hold",
                    side=current_side,
                    side_role="current_position_side",
                    must_land=True,
                    reason="increase_current_position_hold_memory",
                )
            )
    elif lifecycle == "decrease":
        managed_side = current_side or target_side
        if managed_side in {"long", "short"}:
            requirements.append(
                _compact_requirement(
                    lane="reduce",
                    side=managed_side,
                    side_role="current_position_side",
                    must_land=True,
                    reason="decrease_current_position_reduce_memory",
                )
            )
            requirements.append(
                _compact_requirement(
                    lane="hold",
                    side=managed_side,
                    side_role="current_position_side",
                    must_land=True,
                    reason="decrease_current_position_hold_memory",
                )
            )
            requirements.append(
                _compact_requirement(
                    lane="exit",
                    side=managed_side,
                    side_role="current_position_side",
                    must_land=True,
                    reason="decrease_current_position_exit_memory",
                )
            )
    elif lifecycle == "exit":
        managed_side = current_side or target_side
        if managed_side in {"long", "short"}:
            requirements.append(
                _compact_requirement(
                    lane="exit",
                    side=managed_side,
                    side_role="current_position_side",
                    must_land=True,
                    reason="exit_current_position_exit_memory",
                )
            )
            requirements.append(
                _compact_requirement(
                    lane="hold",
                    side=managed_side,
                    side_role="current_position_side",
                    must_land=True,
                    reason="exit_current_position_hold_memory",
                )
            )
            requirements.append(
                _compact_requirement(
                    lane="reduce",
                    side=managed_side,
                    side_role="current_position_side",
                    must_land=True,
                    reason="exit_current_position_reduce_memory",
                )
            )
    elif lifecycle == "ordinary_hold" and current_side in {"long", "short"}:
        requirements.append(
            _compact_requirement(
                lane="hold",
                side=current_side,
                side_role="current_position_side",
                must_land=True,
                reason="hold_current_position_hold_memory",
            )
        )
        audit_only.append(
            _compact_requirement(
                lane="exit",
                side=current_side,
                side_role="current_position_side",
                must_land=False,
                reason="hold_current_position_exit_context",
            )
        )

    # Dedupe while preserving declaration order.  Lane fallback is handled by
    # _lane_matches_requirement so requirement records keep their true lane.
    seen: set[tuple[str, str, str, bool]] = set()
    deduped: list[dict[str, Any]] = []
    for item in requirements:
        key = (
            _clean(item.get("lane")),
            _clean(item.get("side")),
            _clean(item.get("memory_side_role")),
            bool(item.get("must_land_in_pm_contract")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return {
        "contract": "final_action_semantics.memory_requirements.v1",
        "action_lifecycle": lifecycle,
        "action": action,
        "current_position_side": current_side,
        "target_side": target_side,
        "contract_side_role": (
            "trigger_side" if lifecycle == "conditional_monitor"
            else "target_side" if lifecycle in {"open", "increase"}
            else "current_position_side" if lifecycle in {"ordinary_hold", "decrease", "exit"}
            else ""
        ),
        "required_memory_lanes": sorted({item["lane"] for item in deduped}),
        "required_memory_side_roles": sorted({item["memory_side_role"] for item in deduped}),
        "required_pm_memory": deduped,
        "must_land_in_pm_contract": [item for item in deduped if item.get("must_land_in_pm_contract")],
        "audit_only_memory": audit_only,
        "accounting_reads_memory": False,
        "execution_reads_pm_action_value": False,
    }


def _requirement_key(item: Mapping[str, Any] | None) -> tuple[str, str, str]:
    item = item if isinstance(item, Mapping) else {}
    return (
        _clean(item.get("lane") or item.get("learning_lane") or item.get("action_value_lane")),
        _clean(item.get("side")),
        _clean(item.get("memory_side_role")),
    )


def _lane_matches_requirement(row_lane: str, required_lane: str) -> bool:
    row_lane = _clean(row_lane)
    required_lane = _clean(required_lane)
    if not row_lane or not required_lane:
        return False
    aliases = {
        "open": {"open"},
        "add": {"add", "scale", "increase", "open"},
        "scale": {"add", "scale", "increase", "open"},
        "increase": {"add", "scale", "increase", "open"},
        "hold": {"hold", "reduce", "exit"},
        "reduce": {"reduce", "exit", "hold"},
        "exit": {"exit", "reduce", "hold"},
        "conditional_monitor": {"conditional_monitor"},
    }
    return row_lane in aliases.get(required_lane, {required_lane})


def _row_text(row: Mapping[str, Any], key: str) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    return _clean(row.get(key) or payload.get(key))


def _row_covers_requirement(row: Mapping[str, Any], requirement: Mapping[str, Any]) -> bool:
    if not isinstance(row, Mapping) or not isinstance(requirement, Mapping):
        return False
    required_lane, required_side, required_role = _requirement_key(requirement)
    row_lane = _row_text(row, "learning_lane") or _row_text(row, "action_value_lane") or _row_text(row, "action_name")
    row_side = _row_text(row, "side")
    row_role = _row_text(row, "memory_side_role")
    if required_side and row_side and row_side not in {required_side, "*"}:
        return False
    if required_role and row_role and row_role != required_role:
        return False
    return _lane_matches_requirement(row_lane, required_lane)


def audit_pm_memory_consumption(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    """Audit whether a signed PM contract carries its required memory evidence.

    The function is intentionally read-only.  It inspects only the PM contract
    artifact already handed to downstream agents; it does not query research
    tables, mutate the contract, issue orders, or write accounting/research
    facts.
    """
    contract = contract if isinstance(contract, Mapping) else {}
    expected = derive_memory_requirements(contract)
    learning_used = contract.get("learning_used") if isinstance(contract.get("learning_used"), Mapping) else {}
    declared = (
        learning_used.get("memory_requirements")
        if isinstance(learning_used.get("memory_requirements"), Mapping)
        else {}
    )
    retrieval = (
        learning_used.get("memory_retrieval")
        if isinstance(learning_used.get("memory_retrieval"), Mapping)
        else {}
    )
    rows = learning_used.get("alpha_setup_action_values") if isinstance(learning_used.get("alpha_setup_action_values"), list) else []
    expected_required = [
        item for item in (expected.get("must_land_in_pm_contract") or [])
        if isinstance(item, Mapping) and item.get("must_land_in_pm_contract")
    ]
    declared_required = [
        item for item in (declared.get("must_land_in_pm_contract") or [])
        if isinstance(item, Mapping) and item.get("must_land_in_pm_contract")
    ]
    expected_keys = sorted(_requirement_key(item) for item in expected_required)
    declared_keys = sorted(_requirement_key(item) for item in declared_required)
    detail_rows = retrieval.get("requirement_details") if isinstance(retrieval.get("requirement_details"), list) else []
    retrieval_counts: dict[tuple[str, str, str], int] = {}
    for detail in detail_rows:
        if not isinstance(detail, Mapping):
            continue
        key = _requirement_key(detail)
        retrieval_counts[key] = max(retrieval_counts.get(key, 0), _int(detail.get("row_count"), 0))

    uncovered: list[dict[str, Any]] = []
    warnings: list[str] = []
    for requirement in expected_required:
        key = _requirement_key(requirement)
        matched_rows = [row for row in rows if _row_covers_requirement(row, requirement)]
        retrieval_row_count = retrieval_counts.get(key, 0)
        if retrieval_row_count > 0 and not matched_rows:
            uncovered.append({
                "lane": key[0],
                "side": key[1],
                "memory_side_role": key[2],
                "retrieval_row_count": retrieval_row_count,
            })
        elif retrieval_row_count == 0:
            warnings.append(f"pm_required_memory_no_available_rows:{key[0]}:{key[1]}:{key[2]}")

    errors: list[str] = []
    if expected_required and not declared:
        errors.append("pm_memory_requirements_missing_from_contract")
    if expected_keys != declared_keys:
        errors.append("pm_memory_requirements_do_not_match_final_action_semantics")
    if uncovered:
        errors.append("pm_required_memory_not_landed_in_alpha_setup_action_values")

    return {
        "contract": "final_action_semantics.pm_memory_consumption_audit.v1",
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "expected_memory_requirements": expected,
        "declared_memory_requirements": declared,
        "expected_requirement_keys": [":".join(key) for key in expected_keys],
        "declared_requirement_keys": [":".join(key) for key in declared_keys],
        "uncovered_required_pm_memory": uncovered,
        "alpha_setup_action_value_count": len(rows),
        "auditor_reads_research_db": False,
        "trader_reads_pm_action_value": False,
        "accountant_reads_memory": False,
    }


def derive_execution_requirement(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    semantics = classify_final_action_contract(contract)
    memory = derive_memory_requirements(contract)
    return {
        "contract": "final_action_semantics.execution_requirement.v1",
        "lifecycle_state": semantics["lifecycle_state"],
        "execution_permission": semantics["execution_permission"],
        "requires_intraday_result": semantics["requires_intraday_result"],
        "can_monitor_intraday": semantics["can_monitor_intraday"],
        "can_submit_order_now": semantics["can_submit_order_now"],
        "blocked": semantics["blocked"],
        "blocked_reasons": semantics["hard_block_reasons"],
        "soft_limit_reasons": semantics["soft_limit_reasons"],
        "memory_side_role": memory["contract_side_role"],
        "trader_reads_pm_action_value": False,
    }


def requires_intraday_result(contract: Mapping[str, Any] | None) -> bool:
    return bool(classify_final_action_contract(contract).get("requires_intraday_result"))


def authority_allows_entry(authority: Mapping[str, Any] | None) -> bool:
    if not isinstance(authority, Mapping) or not authority:
        return False
    semantics = classify_final_action_contract(authority)
    if semantics["blocked"]:
        return False
    authority_type = _clean(authority.get("authority_type"))
    if authority_type in DIRECT_AUTHORITY_TYPES:
        if authority_type == "real_budget_entry":
            return bool(authority.get("open_action_evidence") and authority.get("strong_current_evidence"))
        return True
    if authority_type in PROBE_AUTHORITY_TYPES:
        if _bool(authority.get("watch_for_trigger_block")):
            return False
        if semantics["can_monitor_intraday"]:
            return True
        return bool(
            authority.get("open_action_evidence")
            or authority.get("strong_current_evidence")
            or authority.get("technical_confirmation")
            or authority.get("event_catalyst_confirmation")
            or authority.get("executable_setup_confirmation")
            or authority.get("market_confirmation")
        )
    if authority_type in BLOCKING_AUTHORITY_TYPES:
        return False
    return semantics["execution_permission"] in {"direct_execute", "monitor_intraday"}


def classify_analyst_evidence(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = payload if isinstance(payload, Mapping) else {}
    forbidden = sorted(key for key in payload.keys() if key in FORBIDDEN_ANALYST_TRADE_AUTHORITY_KEYS)
    opportunity_state = _clean(payload.get("opportunity_state")) or "unknown"
    trigger_valid = _bool(payload.get("trigger_valid"))
    current_trigger_confirmed = _bool(payload.get("current_trigger_confirmed"))
    invalidation_present = _bool(payload.get("invalidation_present"))
    semantic_errors: list[str] = []
    if opportunity_state in {"probe_candidate", "tradeable_candidate"} and not trigger_valid:
        semantic_errors.append("analyst_candidate_without_current_trigger")
    if trigger_valid and not current_trigger_confirmed:
        semantic_errors.append("analyst_trigger_valid_without_current_confirmation")
    if opportunity_state in {"watch_for_trigger", "probe_candidate", "tradeable_candidate"} and not invalidation_present:
        semantic_errors.append("analyst_trade_setup_missing_invalidation")
    semantic_errors.extend(f"analyst_forbidden_trade_authority_field:{key}" for key in forbidden)
    return {
        "contract": "final_action_semantics.analyst_evidence.v1",
        "opportunity_state": opportunity_state,
        "trigger_valid": trigger_valid,
        "current_trigger_confirmed": current_trigger_confirmed,
        "invalidation_present": invalidation_present,
        "forbidden_trade_authority_fields": forbidden,
        "semantic_errors": sorted(set(semantic_errors)),
    }


def validate_signal_collection(collection: Mapping[str, Any] | None) -> dict[str, Any]:
    collection = collection if isinstance(collection, Mapping) else {}
    forbidden = sorted(key for key in collection.keys() if key in FORBIDDEN_ANALYST_TRADE_AUTHORITY_KEYS)
    no_trade_authority = _bool(collection.get("no_trade_authority")) or collection.get("collector_decision_boundary") == "no_trade_authority"
    return {
        "contract": "final_action_semantics.signal_collection.v1",
        "no_trade_authority": no_trade_authority,
        "forbidden_trade_authority_fields": forbidden,
        "semantic_errors": [f"signal_collection_forbidden_trade_authority_field:{key}" for key in forbidden],
    }


def derive_accounting_expectation(
    contract: Mapping[str, Any] | None,
    execution_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    execution_result = execution_result if isinstance(execution_result, Mapping) else {}
    transactions = execution_result.get("actual_transactions")
    has_transaction = bool(transactions) if isinstance(transactions, list) else False
    semantics = classify_final_action_contract(contract)
    memory = derive_memory_requirements(contract)
    return {
        "contract": "final_action_semantics.accounting_expectation.v1",
        "lifecycle_state": semantics["lifecycle_state"],
        "has_transaction": has_transaction,
        "settlement_basis": "actual_execution_facts_only",
        "position_change_allowed": has_transaction,
        "fee_allowed": has_transaction,
        "no_trigger_no_accounting_mutation": semantics["requires_intraday_result"] and not has_transaction,
        "accounting_reads_memory": memory["accounting_reads_memory"],
    }


def derive_review_expectation(
    contract: Mapping[str, Any] | None,
    execution_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    semantics = classify_final_action_contract(contract)
    accounting = derive_accounting_expectation(contract, execution_result)
    memory = derive_memory_requirements(contract)
    memory_audit = audit_pm_memory_consumption(contract)
    return {
        "contract": "final_action_semantics.review_expectation.v1",
        "lifecycle_state": semantics["lifecycle_state"],
        "requires_intraday_result": semantics["requires_intraday_result"],
        "settlement_basis": accounting["settlement_basis"],
        "required_pm_memory": memory["required_pm_memory"],
        "pm_memory_consumption_ok": memory_audit["ok"],
        "pm_memory_consumption_errors": memory_audit["errors"],
        "historical_learning_influenced_contract": bool(
            memory_audit["alpha_setup_action_value_count"] and not memory_audit["errors"]
        ),
    }


def derive_research_fact_state(
    contract: Mapping[str, Any] | None,
    execution_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    semantics = classify_final_action_contract(contract)
    memory = derive_memory_requirements(contract)
    execution_result = execution_result if isinstance(execution_result, Mapping) else {}
    return {
        "contract": "final_action_semantics.research_fact_state.v1",
        "learning_source": "phase4_completed_facts",
        "lifecycle_state": semantics["lifecycle_state"],
        "execution_outcome": execution_result.get("outcome") or execution_result.get("status") or "",
        "memory_side_role": memory["contract_side_role"],
        "required_memory_lanes": memory["required_memory_lanes"],
        "can_mutate_same_day_trade_facts": False,
    }


def derive_protocol_semantic_checks(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    semantics = classify_final_action_contract(contract)
    memory = derive_memory_requirements(contract)
    return {
        "contract": "final_action_semantics.protocol_checks.v1",
        "lifecycle_state": semantics["lifecycle_state"],
        "requires_intraday_result": semantics["requires_intraday_result"],
        "required_pm_memory": memory["required_pm_memory"],
        "required_memory_lanes": memory["required_memory_lanes"],
        "required_memory_side_roles": memory["required_memory_side_roles"],
        "hard_block_reasons": semantics["hard_block_reasons"],
        "soft_limit_reasons": semantics["soft_limit_reasons"],
        "semantic_errors": semantics["semantic_errors"],
    }
