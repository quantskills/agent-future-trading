"""PM final_action_contract self-check.

This helper validates PM's own contract before downstream Auditor/Trader use it.
It is side-effect free and must not repair or rewrite the contract.
"""

from __future__ import annotations

from typing import Any, Dict, List


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _expected_action(current: int, target: int, authority_type: str = "") -> str:
    if current == target:
        return "hold" if current else "wait"
    if current == 0 and target != 0:
        return "open_real" if authority_type == "real_budget_entry" else "open_probe"
    if target == 0 and current != 0:
        return "exit"
    if (current > 0 and target > 0) or (current < 0 and target < 0):
        return "scale" if abs(target) > abs(current) else "reduce"
    return "exit"


def check_final_action_contract(contract: Dict[str, Any] | None) -> Dict[str, Any]:
    """Check final_action_contract consistency without mutating it."""
    if not isinstance(contract, dict):
        return {"ok": False, "errors": ["missing_final_action_contract"], "tool": "pm_contract_self_check"}
    current = _as_int(contract.get("current_lots"))
    target = _as_int(contract.get("target_lots"))
    lots_delta = _as_int(contract.get("lots_delta"))
    authority_type = str(contract.get("authority_type") or "").strip().lower()
    final_action = str(contract.get("final_action") or "").strip().lower()
    errors: List[str] = []
    if lots_delta != target - current:
        errors.append("lots_delta_mismatch")
    expected = _expected_action(current, target, authority_type=authority_type)
    if final_action != expected:
        errors.append("final_action_mismatch")
    requires_intraday = bool(contract.get("requires_intraday_confirmation"))
    can_execute_without_trigger = bool(contract.get("can_execute_without_intraday_trigger"))
    if requires_intraday and can_execute_without_trigger:
        errors.append("conditional_trigger_conflict")
    if requires_intraday and target == current:
        errors.append("conditional_trigger_without_lot_delta")
    if final_action in {"open_probe", "open_real", "scale"} and requires_intraday:
        entry_trigger = str(contract.get("entry_trigger") or "").strip()
        if not entry_trigger or entry_trigger == "unknown":
            errors.append("conditional_trigger_missing_entry_trigger")
    return {
        "tool": "pm_contract_self_check",
        "ok": not errors,
        "errors": errors,
        "expected_final_action": expected,
        "actual_final_action": final_action,
        "current_lots": current,
        "target_lots": target,
        "lots_delta": lots_delta,
        "writes_db": False,
        "writes_artifact": False,
        "writes_payload": False,
    }


def assert_final_action_contract(contract: Dict[str, Any] | None) -> None:
    result = check_final_action_contract(contract)
    if not result.get("ok"):
        raise ValueError(f"pm_final_action_contract_self_check_failed:{result.get('errors')}")
