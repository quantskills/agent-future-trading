"""PM-owned position transition semantics.

This module is a deterministic helper for the portfolio manager. It does not
write DB rows, artifacts, payloads, or trade contracts.
"""

from __future__ import annotations

from typing import Any, Dict


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _authority_type(authority: Dict[str, Any] | None) -> str:
    if not isinstance(authority, dict):
        return ""
    return str(authority.get("authority_type") or "").strip().lower()


def final_action_from_lots(
    *,
    current_lots: int,
    target_lots: int,
    final_entry_authority: Dict[str, Any] | None = None,
) -> str:
    """Return the final action implied by current lots and target lots.

    PM may use this to build its only final_action_contract. Other agents may
    read the resulting contract, but this helper itself is not trade authority.
    """
    current = _as_int(current_lots)
    target = _as_int(target_lots)
    if current == target:
        return "hold" if current else "wait"
    if current == 0 and target != 0:
        return "open_real" if _authority_type(final_entry_authority) == "real_budget_entry" else "open_probe"
    if target == 0 and current != 0:
        return "exit"
    if (current > 0 and target > 0) or (current < 0 and target < 0):
        return "scale" if abs(target) > abs(current) else "reduce"
    return "exit"


def classify_position_transition(
    *,
    current_lots: int,
    target_lots: int,
    final_entry_authority: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Classify PM's lot transition without creating a contract or side effect."""
    current = _as_int(current_lots)
    target = _as_int(target_lots)
    lots_delta = target - current
    final_action = final_action_from_lots(
        current_lots=current,
        target_lots=target,
        final_entry_authority=final_entry_authority,
    )
    if current == target:
        transition_kind = "hold" if current else "wait"
    elif current == 0:
        transition_kind = "open_real" if final_action == "open_real" else "open_probe"
    elif target == 0:
        transition_kind = "exit"
    elif current * target < 0:
        transition_kind = "exit_then_reenter"
    elif abs(target) > abs(current):
        transition_kind = "add"
    else:
        transition_kind = "reduce"
    return {
        "tool": "pm_position_transition",
        "current_lots": current,
        "target_lots": target,
        "lots_delta": int(lots_delta),
        "final_action": final_action,
        "transition_kind": transition_kind,
        "is_new_entry": current == 0 and target != 0,
        "is_existing_position_management": current != 0,
        "requires_pm_contract": True,
        "writes_db": False,
        "writes_artifact": False,
        "writes_payload": False,
    }
