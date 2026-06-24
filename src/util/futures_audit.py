from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any, Dict, List, Optional


ERROR_NO_TRADE_REASONS = {
    "missing_execution_basis",
    "missing_previous_close",
    "no_executable_basis",
    "data_error",
}

EXPECTED_NO_TRADE_REASONS = {
    "llm_neutral",
    "position_matched",
    "cold_start_small_cap",
    "hold_or_zero_lots",
    "pending_rollover_required",
    "margin_insufficient",
    "danger_zone_ban",
    "net_exposure_limit",
    "reduce_only",
    "single_position_cap",
    "base_sizing_anchor_cap",
    "margin_adjustment_to_zero",
    "cooling_period",
    "trade_frequency_control",
    "trade_churn_cost_control",
    "weak_signal_combo",
    "opportunity_quality_position_sizing",
    "side_performance_block",
    "market_confirmation_conflict",
    "drawdown_control",
    "ticker_loss_control",
    "capital_utilization_guard",
    "capital_utilization_memory_protected",
    "learned_underperformance_policy",
    "provisional_policy_probe_only",
    "provisional_policy_cap",
    "market_confirmation_quality_gate",
    "minimum_new_entry_threshold",
    "minimum_rebalance_threshold",
    "holding_period_control",
    "winning_template_continuation",
    "fundamental_anchor_rebalance_cap",
    "horizon_consistency_requires_short_timing",
    "reverse_requires_stronger_evidence",
    "decision_planner_block",
    "decision_planner_reduce_to_zero",
    "trade_auditor_block",
    "trade_auditor_reduce_to_zero",
    "trade_auditor_scale_to_zero",
    "trade_auditor_reduce_only",
    "soft_block_converted_to_probe_only",
    "business_quality_observe_or_block",
    "business_quality_probe_only",
    "business_quality_below_probe",
    "conditional_performance_block",
    "weak_conditional_combo",
    "weak_ticker_side_history",
    "weak_ticker_side_quality_gate",
    "weak_ticker_side_cap",
    "news_only_directional_trade",
    "news_without_fundamental_anchor",
    "protected_ticker_side_weak_combo",
    "protected_ticker_side_cold_start",
    "strategy_memory_weak_block",
    "strategy_memory_watchlist_cap",
    "intraday_trigger_not_met",
    "intraday_waiting_for_trigger",
    "intraday_opening_range_incomplete",
    "intraday_no_valid_bar",
    "after_last_entry_time",
    "duplicate_execution_prevented",
    "signal_invalidation_level",
    "invalidation_level_long",
    "invalidation_level_short",
    "atr_trailing_stop_long",
    "atr_trailing_stop_short",
    "time_stop",
    "signal_horizon_audit",
    "limit_locked_no_fill",
    "near_expiry_new_entry_block",
}

LEGACY_NO_TRADE_REASON_ALIASES = {
    "decision_planner_block": "trade_auditor_block",
    "decision_planner_reduce_to_zero": "trade_auditor_reduce_to_zero",
    "trade_auditor_reduce_to_zero": "trade_auditor_scale_to_zero",
}

NO_TRADE_REASON_CATEGORY_LABELS = {
    "signal": "信号",
    "risk": "风控",
    "timing": "择时",
    "execution": "执行",
    "business": "业务",
    "learning": "学习",
}

NO_TRADE_REASON_CATEGORY_DESCRIPTIONS = {
    "signal": "不会看或暂时没有可交易信号：方向、证据、质量或分析一致性不足。",
    "risk": "不敢做：组合风险、资金、回撤、频率、仓位或审计边界阻止交易。",
    "timing": "没等到：盘中触发、短线确认或交易时间窗没有满足。",
    "execution": "做不了：数据、执行基准、成交可行性、涨跌停或系统执行状态阻止成交。",
    "business": "本来不该做：已有仓位、换约、交割、持仓生命周期等业务状态不要求成交。",
    "learning": "学习边界：历史经验、候选假设、弱样本或策略状态限制交易权限。",
}

NO_TRADE_REASON_CATEGORY_MAP = {
    "llm_neutral": "signal",
    "weak_signal_combo": "signal",
    "market_confirmation_conflict": "signal",
    "market_confirmation_quality_gate": "signal",
    "business_quality_observe_or_block": "signal",
    "business_quality_probe_only": "signal",
    "business_quality_below_probe": "signal",
    "news_only_directional_trade": "signal",
    "news_without_fundamental_anchor": "signal",
    "fundamental_anchor_rebalance_cap": "signal",
    "reverse_requires_stronger_evidence": "signal",
    "signal_invalidation_level": "signal",
    "invalidation_level_long": "signal",
    "invalidation_level_short": "signal",
    "signal_horizon_audit": "signal",
    "margin_insufficient": "risk",
    "danger_zone_ban": "risk",
    "net_exposure_limit": "risk",
    "reduce_only": "risk",
    "single_position_cap": "risk",
    "base_sizing_anchor_cap": "risk",
    "margin_adjustment_to_zero": "risk",
    "cooling_period": "risk",
    "trade_frequency_control": "risk",
    "trade_churn_cost_control": "risk",
    "opportunity_quality_position_sizing": "signal",
    "drawdown_control": "risk",
    "ticker_loss_control": "risk",
    "capital_utilization_guard": "risk",
    "minimum_new_entry_threshold": "risk",
    "minimum_rebalance_threshold": "risk",
    "trade_auditor_block": "risk",
    "trade_auditor_scale_to_zero": "risk",
    "trade_auditor_reduce_only": "risk",
    "soft_block_converted_to_probe_only": "risk",
    "atr_trailing_stop_long": "risk",
    "atr_trailing_stop_short": "risk",
    "time_stop": "risk",
    "horizon_consistency_requires_short_timing": "timing",
    "intraday_trigger_not_met": "timing",
    "intraday_waiting_for_trigger": "timing",
    "intraday_opening_range_incomplete": "timing",
    "intraday_no_valid_bar": "timing",
    "after_last_entry_time": "timing",
    "missing_execution_basis": "execution",
    "missing_previous_close": "execution",
    "no_executable_basis": "execution",
    "data_error": "execution",
    "duplicate_execution_prevented": "execution",
    "limit_locked_no_fill": "execution",
    "cancelled": "execution",
    "rejected": "execution",
    "expired": "execution",
    "position_matched": "business",
    "hold_or_zero_lots": "business",
    "pending_rollover_required": "business",
    "holding_period_control": "business",
    "winning_template_continuation": "business",
    "near_expiry_new_entry_block": "business",
    "cold_start_small_cap": "learning",
    "side_performance_block": "learning",
    "capital_utilization_memory_protected": "learning",
    "learned_underperformance_policy": "learning",
    "provisional_policy_probe_only": "learning",
    "provisional_policy_cap": "learning",
    "conditional_performance_block": "learning",
    "weak_conditional_combo": "learning",
    "weak_ticker_side_history": "learning",
    "weak_ticker_side_quality_gate": "learning",
    "weak_ticker_side_cap": "learning",
    "protected_ticker_side_weak_combo": "learning",
    "protected_ticker_side_cold_start": "learning",
    "strategy_memory_weak_block": "learning",
    "strategy_memory_watchlist_cap": "learning",
}

_EMPTY_NO_TRADE_REASONS = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
}


def normalize_no_trade_reason(reason: Optional[str]) -> Optional[str]:
    if reason is None:
        return None
    normalized = str(reason).strip()
    if not normalized:
        return None
    if normalized.lower() in _EMPTY_NO_TRADE_REASONS:
        return None
    return LEGACY_NO_TRADE_REASON_ALIASES.get(normalized, normalized)


def _infer_no_trade_reason_category(reason: Optional[str]) -> str:
    text = str(reason or "").strip().lower()
    if not text:
        return "signal"
    if any(token in text for token in ("rollover", "expiry", "delivery", "position_matched", "matched", "hold_or_zero")):
        return "business"
    if any(token in text for token in ("learn", "memory", "hypothesis", "template", "history", "policy", "protected", "deployable", "watchlist")):
        return "learning"
    if any(token in text for token in ("trigger", "timing", "opening", "intraday", "after_last_entry", "horizon")):
        return "timing"
    if any(token in text for token in ("execution", "basis", "data", "limit", "cancel", "reject", "expired", "duplicate", "fill")):
        return "execution"
    if any(token in text for token in ("margin", "risk", "drawdown", "cooling", "frequency", "cap", "threshold", "auditor", "reduce", "danger", "loss", "exposure")):
        return "risk"
    return "signal"


def categorize_no_trade_reason(reason: Optional[str]) -> Dict[str, Any]:
    normalized = normalize_no_trade_reason(reason)
    category = NO_TRADE_REASON_CATEGORY_MAP.get(normalized or "")
    source = "explicit_map" if category else "keyword_fallback"
    if not category:
        category = _infer_no_trade_reason_category(normalized)
        if not normalized:
            source = "default_signal_missing_reason"
    return {
        "reason": normalized or "unknown",
        "category": category,
        "category_label": NO_TRADE_REASON_CATEGORY_LABELS[category],
        "category_description": NO_TRADE_REASON_CATEGORY_DESCRIPTIONS[category],
        "source": source,
    }


def ensure_signal_snapshot(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    return {}


def ensure_execution_translation(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    translation = snapshot.get("execution_translation")
    if not isinstance(translation, dict):
        translation = {}
    translation.setdefault("translated_orders", [])
    translation.setdefault("rewrite_reasons", [])
    snapshot["execution_translation"] = translation
    return translation


def ensure_execution_result(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    result = snapshot.get("execution_result")
    if not isinstance(result, dict):
        result = {}
        snapshot["execution_result"] = result
    return result


def build_execution_learning_trace(
    snapshot: Dict[str, Any],
    *,
    outcome: Optional[str] = None,
    status: Optional[str] = None,
    no_trade_reason: Optional[str] = None,
    no_trade_reason_category: Optional[Dict[str, Any]] = None,
    transaction_count: int = 0,
    execution_learning_type: Optional[str] = None,
    turn_into_memory: Optional[bool] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    execution_translation = snapshot.get("execution_translation") if isinstance(snapshot.get("execution_translation"), dict) else {}
    execution_contract = (
        execution_translation.get("execution_contract")
        if isinstance(execution_translation.get("execution_contract"), dict)
        else {}
    )
    ticker = (
        snapshot.get("ticker")
        or snapshot.get("underlying_code")
        or contract.get("underlying_code")
        or contract.get("ticker")
        or execution_translation.get("underlying_code")
        or ""
    )
    execution_profile = (
        contract.get("execution_profile")
        or execution_contract.get("execution_profile")
        or execution_translation.get("execution_profile")
        or "unknown"
    )
    trigger_reason = (
        contract.get("trigger_reason")
        or contract.get("trigger_source")
        or execution_contract.get("trigger_reason")
        or execution_contract.get("trigger_source")
        or no_trade_reason
        or outcome
        or "unknown"
    )
    trace: Dict[str, Any] = {
        "consumer_scope": "trader_execution_learning",
        "learning_lane": "execution",
        "execution_retrieval_key": "|".join(
            str(part or "unknown")
            for part in (ticker, execution_profile, trigger_reason, "execution")
        ),
        "outcome": outcome,
        "status": status,
        "no_trade_reason": no_trade_reason,
        "no_trade_reason_category": no_trade_reason_category,
        "actual_transaction_count": int(transaction_count),
        "turn_into_memory": (
            bool(no_trade_reason and int(transaction_count) == 0)
            if turn_into_memory is None
            else bool(turn_into_memory)
        ),
        "not_direction_evidence": True,
    }
    if execution_learning_type:
        trace["execution_learning_type"] = execution_learning_type
    if extra:
        trace.update({k: v for k, v in extra.items() if v is not None})
    return trace


def add_rewrite_reason(snapshot: Dict[str, Any], reason: Optional[str]) -> None:
    reason = normalize_no_trade_reason(reason)
    if not reason:
        return
    translation = ensure_execution_translation(snapshot)
    reasons = translation.setdefault("rewrite_reasons", [])
    if reason not in reasons:
        reasons.append(reason)


def append_translated_order(
    snapshot: Dict[str, Any],
    *,
    action: Any,
    lots: int,
    contract_code: Optional[str] = None,
    price: Optional[float] = None,
    stage: str = "translation",
) -> None:
    translation = ensure_execution_translation(snapshot)
    action_value = enum_value(action)
    translation["translated_orders"].append(
        {
            "stage": stage,
            "action": action_value,
            "lots": int(lots or 0),
            "contract_code": contract_code,
            "price": price,
        }
    )


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, "", "unknown", "UNKNOWN"):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, "", "unknown", "UNKNOWN"):
        return None
    try:
        return max(0, int(value))
    except Exception:
        return None


def _first_signal_value(snapshot: Dict[str, Any], field_names: List[str]) -> Any:
    analyst_keys = ("technical", "fundamental", "commodity_news")
    for analyst in analyst_keys:
        item = snapshot.get(analyst)
        if not isinstance(item, dict):
            continue
        for field_name in field_names:
            value = item.get(field_name)
            if value not in (None, "", "unknown"):
                return value
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        for context_name in ("technical_context", "market_context", "signal_context"):
            context = metadata.get(context_name) if isinstance(metadata.get(context_name), dict) else {}
            for field_name in field_names:
                value = context.get(field_name)
                if value not in (None, "", "unknown"):
                    return value
    return None


def extract_signal_lifecycle(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Return execution-relevant lifecycle fields emitted by analysts or Phase1 planning."""
    if not isinstance(snapshot, dict):
        return {}
    expected_days = _optional_int(_first_signal_value(snapshot, ["expected_horizon_days"]))
    horizon_class = _first_signal_value(snapshot, ["horizon_class"])
    if not horizon_class and expected_days is not None:
        if expected_days <= 0:
            horizon_class = "flat"
        elif expected_days <= 2:
            horizon_class = "short"
        elif expected_days <= 5:
            horizon_class = "medium"
        else:
            horizon_class = "long"
    lifecycle = {
        "horizon_class": str(horizon_class) if horizon_class else None,
        "expected_horizon_days": expected_days,
        "price_percentile": _optional_float(
            _first_signal_value(snapshot, ["price_percentile", "price_percentile_lookback", "current_price_percentile"])
        ),
        "entry_trigger": _first_signal_value(snapshot, ["entry_trigger"]),
        "action_name": _first_signal_value(snapshot, ["action_name"]),
        "invalidation_level": _optional_float(
            _first_signal_value(snapshot, ["invalidation_level", "stop_level", "stop_loss_level", "invalid_price"])
        ),
        "target_return": _optional_float(
            _first_signal_value(snapshot, ["target_return", "expected_return", "target_return_ratio", "expected_return_ratio"])
        ),
        "atr_stop_distance": _optional_float(
            _first_signal_value(snapshot, ["atr_stop_distance", "atr_stop", "atr_distance"])
        ),
        "setup_type": _first_signal_value(snapshot, ["setup_type"]),
        "business_quality_score": _optional_float(_first_signal_value(snapshot, ["business_quality_score"])),
    }
    return {key: value for key, value in lifecycle.items() if value is not None}


def infer_target_lots(recommendation: Dict[str, Any]) -> int:
    snapshot = recommendation.get("signal_snapshot")
    if isinstance(snapshot, dict):
        contract = snapshot.get("final_action_contract")
        if isinstance(contract, dict) and contract.get("target_lots") is not None:
            return int(contract.get("target_lots") or 0)

    action_value = enum_value(recommendation.get("action"))
    lots = int(recommendation.get("lots", 0) or 0)
    if action_value == "open_long":
        return lots
    if action_value == "open_short":
        return -lots
    return 0


def build_actual_transactions(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    actual: List[Dict[str, Any]] = []
    for transaction in transactions:
        actual.append(
            {
                "action": enum_value(transaction.get("action")),
                "lots": int(transaction.get("lots", 0) or 0),
                "contract_code": transaction.get("contract_code"),
                "execution_price": transaction.get("execution_price"),
                "execution_phase": enum_value(transaction.get("execution_phase")),
            }
        )
    return actual


def _result_consistency_against_phase2_plan(snapshot: Dict[str, Any], result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    translation = snapshot.get("execution_translation")
    if not isinstance(translation, dict):
        return None
    plan = translation.get("phase2_order_plan")
    if not isinstance(plan, dict):
        return None

    plan_action = enum_value(plan.get("action"))
    plan_lots = int(plan.get("lots", 0) or 0)
    actual_action = result.get("actual_action")
    actual_lots = result.get("actual_lots")
    plan_consistency = plan.get("consistency_diagnostics") if isinstance(plan.get("consistency_diagnostics"), dict) else {}
    expected = plan_consistency.get("expected") if isinstance(plan_consistency.get("expected"), dict) else {}
    issues: List[str] = []

    if result.get("outcome") == "executed":
        if actual_action == "multi":
            if actual_lots is not None and int(actual_lots or 0) != plan_lots:
                issues.append("multi_transaction_lots_mismatch")
        else:
            if actual_action != plan_action:
                issues.append("actual_action_mismatch")
            allowed_lots = {plan_lots}
            if expected.get("requires_two_step_reversal") and int(expected.get("first_leg_lots") or 0) > 0:
                allowed_lots.add(int(expected.get("first_leg_lots") or 0))
            if actual_lots is not None and int(actual_lots or 0) not in allowed_lots:
                issues.append("actual_lots_mismatch")
    elif result.get("outcome") == "executed_without_transaction":
        if plan_action != "hold" and plan_lots > 0:
            issues.append("planned_trade_without_transaction")

    return {
        "status": "ok" if not issues else "warning",
        "issues": issues,
        "phase2_plan_action": plan_action,
        "phase2_plan_lots": plan_lots,
        "actual_action": actual_action,
        "actual_lots": actual_lots,
        "no_trade_reason": result.get("no_trade_reason"),
    }


def set_execution_result(
    snapshot: Dict[str, Any],
    *,
    outcome: str,
    status: str,
    transaction_count: int,
    actual_transactions: Optional[List[Dict[str, Any]]] = None,
    no_trade_reason: Optional[str] = None,
    warning_message: Optional[str] = None,
) -> None:
    result = ensure_execution_result(snapshot)
    actual_transactions = actual_transactions or []
    normalized_reason = normalize_no_trade_reason(no_trade_reason)
    reason_category = categorize_no_trade_reason(normalized_reason) if normalized_reason else None
    result.update(
        {
            "outcome": outcome,
            "status": status,
            "transaction_count": int(transaction_count),
            "actual_transactions": actual_transactions,
            "actual_action": _resolve_actual_action(actual_transactions),
            "actual_lots": _resolve_actual_lots(actual_transactions),
            "no_trade_reason": normalized_reason,
            "no_trade_reason_category": reason_category,
            "execution_learning_trace": build_execution_learning_trace(
                snapshot,
                outcome=outcome,
                status=status,
                no_trade_reason=normalized_reason,
                no_trade_reason_category=reason_category,
                transaction_count=int(transaction_count),
            ),
            "warning_message": warning_message,
        }
    )
    consistency = _result_consistency_against_phase2_plan(snapshot, result)
    if consistency is not None:
        result["consistency_diagnostics"] = consistency


def classify_no_trade_reason(reason: Optional[str]) -> str:
    reason = normalize_no_trade_reason(reason)
    if reason in ERROR_NO_TRADE_REASONS:
        return "error"
    if reason in EXPECTED_NO_TRADE_REASONS:
        return "expected"
    return "unknown"


def classify_no_trade_reasons(reasons: List[Optional[str]]) -> str:
    normalized = [reason for reason in reasons if reason]
    if not normalized:
        return "unknown"

    classes = [classify_no_trade_reason(reason) for reason in normalized]
    if any(item == "error" for item in classes):
        return "error"
    if all(item == "expected" for item in classes):
        return "expected"
    return "unknown"


def summarize_no_trade_reason_categories(reasons: List[Optional[str]]) -> Dict[str, int]:
    counter: Counter = Counter()
    for reason in reasons:
        normalized = normalize_no_trade_reason(reason)
        if not normalized:
            continue
        category = categorize_no_trade_reason(normalized)["category"]
        counter[category] += 1
    return {str(key): int(value) for key, value in counter.most_common()}


def infer_no_trade_reason(
    snapshot: Dict[str, Any],
    warning_message: Optional[str] = None,
    default: Optional[str] = None,
) -> Optional[str]:
    result = snapshot.get("execution_result")
    if isinstance(result, dict):
        no_trade_reason = normalize_no_trade_reason(result.get("no_trade_reason"))
        if no_trade_reason:
            return no_trade_reason

    translation = snapshot.get("execution_translation")
    if isinstance(translation, dict):
        rewrite_reasons = translation.get("rewrite_reasons") or []
        for reason in reversed(rewrite_reasons):
            if classify_no_trade_reason(reason) != "unknown":
                return reason

    lowered = (warning_message or "").lower()
    if "no previous close" in lowered:
        return "missing_previous_close"
    if "no executable basis" in lowered or "missing open-order execution basis" in lowered:
        return "missing_execution_basis"
    if "data error" in lowered:
        return "data_error"
    return normalize_no_trade_reason(default)


def _nested_dict(root: Dict[str, Any], *path: str) -> Dict[str, Any]:
    current: Any = root
    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _first_dict(*values: Any) -> Dict[str, Any]:
    for value in values:
        if isinstance(value, dict) and value:
            return value
    return {}


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _compact_action_value_preferences(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    learning_used = contract.get("learning_used") if isinstance(contract.get("learning_used"), dict) else {}
    rows = learning_used.get("alpha_setup_action_values") if isinstance(learning_used.get("alpha_setup_action_values"), list) else []
    compact: List[Dict[str, Any]] = []
    for row in rows[:3]:
        if not isinstance(row, dict):
            continue
        compact.append(
            {
                "action_name": row.get("action_name"),
                "action_preference": row.get("action_preference"),
                "canonical_action_preference_source": (
                    row.get("canonical_action_preference_source") or "payload.action_preference"
                ),
                "action_preference": row.get("action_preference"),
                "sample_scope": row.get("sample_scope"),
                "memory_quality": row.get("memory_quality"),
                "reward_mean": row.get("reward_mean"),
            }
        )
    return compact


def _build_trade_contract_audit(snapshot: Dict[str, Any], contract: Dict[str, Any], authority: Dict[str, Any]) -> Dict[str, Any]:
    phase2_execution = (
        snapshot.get("phase2_execution")
        if isinstance(snapshot.get("phase2_execution"), dict)
        else {}
    )
    pm_plan_validation = (
        phase2_execution.get("pm_plan_validation")
        if isinstance(phase2_execution.get("pm_plan_validation"), dict)
        else {}
    )
    authority_consistency = (
        pm_plan_validation.get("authority_consistency")
        if isinstance(pm_plan_validation.get("authority_consistency"), dict)
        else {}
    )
    reason_codes = contract.get("reason_codes") if isinstance(contract.get("reason_codes"), list) else None
    if reason_codes is None:
        reason_codes = authority.get("reason_codes") if isinstance(authority.get("reason_codes"), list) else []
    return {
        "audit_boundary": (
            "transaction audit mirror only; final_action_contract remains the executable source of truth"
        ),
        "single_source_of_trade_truth": bool(contract.get("single_source_of_trade_truth")),
        "candidate_sources_do_not_bypass_contract": bool(
            contract.get("candidate_sources_do_not_bypass_contract")
        ),
        "contract_version": contract.get("contract_version"),
        "final_action": contract.get("final_action"),
        "authority_type": contract.get("authority_type"),
        "authority_decision": contract.get("authority_decision"),
        "open_action_evidence": bool(contract.get("open_action_evidence")),
        "strong_current_evidence": bool(contract.get("strong_current_evidence")),
        "current_lots": contract.get("current_lots"),
        "target_lots": contract.get("target_lots"),
        "lots_delta": contract.get("lots_delta"),
        "target_margin_ratio_estimate": contract.get("target_margin_ratio_estimate"),
        "max_allowed_margin_ratio": _first_non_empty(
            contract.get("max_allowed_margin_ratio"),
            authority.get("max_allowed_margin_ratio"),
        ),
        "reason_codes": reason_codes,
        "execution_profile": contract.get("execution_profile"),
        "execution_requirement": contract.get("execution_requirement"),
        "pm_plan_validation_passed": pm_plan_validation.get("passed"),
        "pm_plan_validation_reason": pm_plan_validation.get("reason"),
        "authority_consistency_reason": authority_consistency.get("reason"),
        "business_boundary": _first_non_empty(
            pm_plan_validation.get("business_boundary"),
            authority_consistency.get("business_boundary"),
        ),
        "selected_action_preferences": _compact_action_value_preferences(contract),
    }


def build_audit_payload(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    translation = snapshot.get("execution_translation")
    result = snapshot.get("execution_result")
    phase2_execution = snapshot.get("phase2_execution")
    contract = _first_dict(snapshot.get("final_action_contract"))
    authority = contract
    if contract:
        payload["final_action_contract"] = deepcopy(contract)
    if contract:
        payload["trade_contract_audit"] = _build_trade_contract_audit(snapshot, contract, authority)
    if isinstance(translation, dict):
        payload["execution_translation"] = deepcopy(translation)
    if isinstance(result, dict):
        payload["execution_result"] = deepcopy(result)
    if isinstance(phase2_execution, dict):
        payload["phase2_execution"] = deepcopy(phase2_execution)
    return payload


def classify_zero_transaction_day(recommendations: List[Dict[str, Any]]) -> Dict[str, Any]:
    reasons: List[str] = []
    for recommendation in recommendations:
        source_type = recommendation.get("source_type")
        source_value = enum_value(source_type)
        if source_value not in (None, "strategy"):
            continue

        snapshot = (
            recommendation.get("signal_snapshot")
            if isinstance(recommendation.get("signal_snapshot"), dict)
            else {}
        )
        reason = infer_no_trade_reason(
            snapshot,
            warning_message=recommendation.get("warning_message"),
        )
        if reason:
            reasons.append(reason)

    return {
        "classification": classify_no_trade_reasons(reasons),
        "reasons": reasons,
        "reason_categories": summarize_no_trade_reason_categories(reasons),
    }


def calculate_margin_audit(
    *,
    action: Any,
    lots: int,
    current_shares: int,
    current_margin_used: float,
) -> Dict[str, Any]:
    action_value = enum_value(action)
    lots_value = abs(int(lots or 0))
    current_shares_value = int(current_shares or 0)
    current_margin_value = float(current_margin_used or 0.0)
    post_trade_shares = current_shares_value
    released_margin = 0.0
    post_trade_margin_used = current_margin_value

    if action_value == "open_long":
        post_trade_shares = current_shares_value + lots_value
        post_trade_margin_used = current_margin_value
    elif action_value == "open_short":
        post_trade_shares = current_shares_value - lots_value
        post_trade_margin_used = current_margin_value
    elif action_value == "close_long":
        available_lots = max(current_shares_value, 0)
        if available_lots < lots_value:
            raise RuntimeError(
                f"Cannot close {lots_value} long lot(s); only {available_lots} lot(s) are available"
            )
        released_margin = _release_margin(current_margin_value, lots_value, available_lots)
        post_trade_shares = current_shares_value - lots_value
        post_trade_margin_used = max(0.0, current_margin_value - released_margin)
    elif action_value == "close_short":
        available_lots = abs(min(current_shares_value, 0))
        if available_lots < lots_value:
            raise RuntimeError(
                f"Cannot close {lots_value} short lot(s); only {available_lots} lot(s) are available"
            )
        released_margin = _release_margin(current_margin_value, lots_value, available_lots)
        post_trade_shares = current_shares_value + lots_value
        post_trade_margin_used = max(0.0, current_margin_value - released_margin)

    if post_trade_shares == 0:
        post_trade_margin_used = 0.0

    return {
        "pre_trade_shares": current_shares_value,
        "post_trade_shares": post_trade_shares,
        "pre_trade_margin_used": current_margin_value,
        "released_margin": released_margin,
        "margin_delta": post_trade_margin_used - current_margin_value,
        "post_trade_margin_used": post_trade_margin_used,
    }


def enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _resolve_actual_action(actual_transactions: List[Dict[str, Any]]) -> Optional[str]:
    if not actual_transactions:
        return None
    if len(actual_transactions) == 1:
        return actual_transactions[0].get("action")
    return "multi_leg"


def _resolve_actual_lots(actual_transactions: List[Dict[str, Any]]) -> Optional[int]:
    if not actual_transactions:
        return None
    if len(actual_transactions) == 1:
        return int(actual_transactions[0].get("lots", 0) or 0)
    return sum(int(item.get("lots", 0) or 0) for item in actual_transactions)


def _release_margin(current_margin_used: float, closed_lots: int, previous_lots: int) -> float:
    if previous_lots <= 0:
        return current_margin_used
    return current_margin_used * (closed_lots / previous_lots)

