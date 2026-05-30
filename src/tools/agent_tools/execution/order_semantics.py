"""Shared signed-lot action semantics for recommendation and execution audits."""

from typing import Any, Dict


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _rebalance_action_type(current_lots: int, target_lots: int) -> str:
    if current_lots == target_lots:
        return "keep"
    if current_lots == 0 and target_lots != 0:
        return "new_entry"
    if target_lots == 0 and current_lots != 0:
        return "exit"
    if current_lots * target_lots < 0:
        return "reverse"
    if abs(target_lots) > abs(current_lots):
        return "increase"
    if abs(target_lots) < abs(current_lots):
        return "reduce"
    return "rebalance"


def recommendation_intent_from_lots(current_lots: int, target_lots: int) -> Dict[str, Any]:
    """Return the semantic Phase1 recommendation action for current -> target lots."""
    current_lots = _as_int(current_lots)
    target_lots = _as_int(target_lots)
    lots_delta = target_lots - current_lots
    action_type = _rebalance_action_type(current_lots, target_lots)

    if lots_delta == 0:
        action = "hold"
        lots = 0
    elif current_lots == 0:
        action = "open_long" if target_lots > 0 else "open_short"
        lots = abs(target_lots)
    elif target_lots == 0:
        action = "close_long" if current_lots > 0 else "close_short"
        lots = abs(current_lots)
    elif current_lots * target_lots < 0:
        action = "close_long" if current_lots > 0 else "close_short"
        lots = abs(current_lots)
    elif abs(target_lots) > abs(current_lots):
        action = "open_long" if target_lots > 0 else "open_short"
        lots = abs(lots_delta)
    else:
        action = "close_long" if current_lots > 0 else "close_short"
        lots = abs(lots_delta)

    return {
        "mode": "recommendation",
        "action": action,
        "lots": int(lots),
        "action_type": action_type,
        "current_lots": current_lots,
        "target_lots": target_lots,
        "lots_delta": int(lots_delta),
        "position_matched": lots_delta == 0,
        "requires_two_step_reversal": current_lots * target_lots < 0,
        "first_leg_action": "close_long" if current_lots > 0 else "close_short" if current_lots < 0 else None,
        "first_leg_lots": abs(current_lots) if current_lots * target_lots < 0 else 0,
        "follow_up_action": "open_long" if target_lots > 0 else "open_short" if target_lots < 0 else None,
        "follow_up_lots": abs(target_lots) if current_lots * target_lots < 0 else 0,
    }


def phase2_order_intent_from_lots(current_lots: int, target_lots: int) -> Dict[str, Any]:
    """Return the expected executable Phase2 order for current -> target lots."""
    current_lots = _as_int(current_lots)
    target_lots = _as_int(target_lots)
    lots_delta = target_lots - current_lots
    action_type = _rebalance_action_type(current_lots, target_lots)

    if lots_delta == 0:
        action = "hold"
        lots = 0
    elif target_lots > current_lots:
        action = "open_long" if current_lots >= 0 else "close_short"
        lots = abs(lots_delta)
    else:
        action = "close_long" if current_lots > 0 else "open_short"
        lots = abs(lots_delta)

    return {
        "mode": "phase2_execution",
        "action": action,
        "lots": int(lots),
        "action_type": action_type,
        "current_lots": current_lots,
        "target_lots": target_lots,
        "lots_delta": int(lots_delta),
        "position_matched": lots_delta == 0,
        "requires_two_step_reversal": current_lots * target_lots < 0,
        "first_leg_action": "close_long" if current_lots > 0 else "close_short" if current_lots < 0 else None,
        "first_leg_lots": abs(current_lots) if current_lots * target_lots < 0 else 0,
        "follow_up_action": "open_long" if target_lots > 0 else "open_short" if target_lots < 0 else None,
        "follow_up_lots": abs(target_lots) if current_lots * target_lots < 0 else 0,
    }


def build_lot_intent_consistency(
    *,
    current_lots: int,
    target_lots: int,
    action: Any,
    lots: Any,
    mode: str,
) -> Dict[str, Any]:
    """Compare stored action/lots with the expected signed-lot intent."""
    expected = (
        phase2_order_intent_from_lots(current_lots, target_lots)
        if mode == "phase2_execution"
        else recommendation_intent_from_lots(current_lots, target_lots)
    )
    actual_action = _enum_value(action)
    actual_lots = _as_int(lots)
    issues = []
    if actual_action != expected["action"]:
        issues.append("action_mismatch")
    if actual_lots != expected["lots"]:
        issues.append("lots_mismatch")

    return {
        "status": "ok" if not issues else "warning",
        "mode": expected["mode"],
        "issues": issues,
        "actual": {"action": actual_action, "lots": actual_lots},
        "expected": expected,
    }
