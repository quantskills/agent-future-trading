from __future__ import annotations

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
    "margin_insufficient",
    "danger_zone_ban",
    "net_exposure_limit",
    "reduce_only",
    "single_position_cap",
    "margin_adjustment_to_zero",
    "cooling_period",
    "trade_frequency_control",
    "weak_signal_combo",
    "side_performance_block",
    "market_confirmation_conflict",
    "drawdown_control",
    "ticker_loss_control",
    "capital_utilization_guard",
    "market_confirmation_quality_gate",
    "minimum_new_entry_threshold",
    "minimum_rebalance_threshold",
    "holding_period_control",
    "fundamental_anchor_rebalance_cap",
    "reverse_requires_stronger_evidence",
    "decision_planner_block",
    "decision_planner_reduce_to_zero",
    "trade_auditor_block",
    "trade_auditor_reduce_to_zero",
    "conditional_performance_block",
    "weak_conditional_combo",
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
}

LEGACY_NO_TRADE_REASON_ALIASES = {
    "decision_planner_block": "trade_auditor_block",
    "decision_planner_reduce_to_zero": "trade_auditor_reduce_to_zero",
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


def infer_target_lots(recommendation: Dict[str, Any]) -> int:
    snapshot = recommendation.get("signal_snapshot")
    if isinstance(snapshot, dict):
        pre_open_plan = snapshot.get("pre_open_plan")
        if isinstance(pre_open_plan, dict) and pre_open_plan.get("target_lots_estimate") is not None:
            return int(pre_open_plan.get("target_lots_estimate") or 0)

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
    result.update(
        {
            "outcome": outcome,
            "status": status,
            "transaction_count": int(transaction_count),
            "actual_transactions": actual_transactions,
            "actual_action": _resolve_actual_action(actual_transactions),
            "actual_lots": _resolve_actual_lots(actual_transactions),
            "no_trade_reason": no_trade_reason,
            "warning_message": warning_message,
        }
    )


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

    pre_open_plan = snapshot.get("pre_open_plan")
    if isinstance(pre_open_plan, dict):
        tradable_reason = normalize_no_trade_reason(pre_open_plan.get("tradable_lots_reason"))
        if tradable_reason:
            return tradable_reason

    lowered = (warning_message or "").lower()
    if "no previous close" in lowered:
        return "missing_previous_close"
    if "no executable basis" in lowered or "missing open-order execution basis" in lowered:
        return "missing_execution_basis"
    if "data error" in lowered:
        return "data_error"
    return normalize_no_trade_reason(default)


def build_audit_payload(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    translation = snapshot.get("execution_translation")
    result = snapshot.get("execution_result")
    phase2_execution = snapshot.get("phase2_execution")
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
