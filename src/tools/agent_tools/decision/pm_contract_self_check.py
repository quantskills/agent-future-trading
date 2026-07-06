"""PM final_action_contract self-check.

This helper validates PM's own contract before downstream Auditor/Trader use it.
It is side-effect free and must not repair or rewrite the contract.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from tools.common.contracts import pm_artifact_boundary_violations
from tools.common.final_action_semantics import (
    contract_has_full_market_capital_rank,
    full_market_rank_gate_errors,
    lifecycle_learning_decision_contract_errors,
    rank_capital_layer_contract_errors,
    rank_lifecycle_learning_route_errors,
)


FINAL_ACTION_CONTRACT_REQUIRED_FIELDS = (
    "final_action",
    "current_lots",
    "target_lots",
    "lots_delta",
    "reason_codes",
    "execution_profile",
    "entry_trigger",
    "invalidation",
    "learning_used",
    "evidence_used",
    "capital_deployment",
    "position_sizing_result",
)

FINAL_ACTION_CONTRACT_DICT_FIELDS = (
    "learning_used",
    "evidence_used",
    "capital_deployment",
    "position_sizing_result",
)

FINAL_ACTION_CONTRACT_LIST_FIELDS = ("reason_codes",)


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


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _contract_has_any_rank(contract: Mapping[str, Any]) -> bool:
    evidence = _mapping(contract.get("evidence_used"))
    deployment = _mapping(contract.get("capital_deployment"))
    return any(
        _present(value)
        for value in (
            contract.get("opportunity_rank"),
            evidence.get("opportunity_rank"),
            deployment.get("opportunity_rank"),
        )
    )


def _base_field_errors(contract: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    for field in FINAL_ACTION_CONTRACT_REQUIRED_FIELDS:
        if field not in contract:
            errors.append(f"missing_final_action_contract_{field}")
    for field in FINAL_ACTION_CONTRACT_DICT_FIELDS:
        if field in contract and not isinstance(contract.get(field), dict):
            errors.append(f"invalid_final_action_contract_{field}")
    for field in FINAL_ACTION_CONTRACT_LIST_FIELDS:
        if field in contract and not isinstance(contract.get(field), list):
            errors.append(f"invalid_final_action_contract_{field}")
    return errors


def _rank_score_trace_errors(contract: Mapping[str, Any]) -> List[str]:
    if not _contract_has_any_rank(contract):
        return []
    evidence = _mapping(contract.get("evidence_used"))
    deployment = _mapping(contract.get("capital_deployment"))
    evidence_inputs = _mapping(evidence.get("rank_input_components"))
    deployment_inputs = _mapping(deployment.get("rank_input_components"))
    if any(
        _present(value)
        for value in (
            deployment.get("rank_score"),
            evidence.get("rank_score"),
            deployment_inputs.get("rank_score"),
            evidence_inputs.get("rank_score"),
        )
    ):
        return []
    return ["rank_trace.rank_score_missing"]


def _learning_rows_present(value: Any) -> bool:
    if isinstance(value, list):
        return any(isinstance(row, Mapping) and bool(row) for row in value)
    return False


def _contract_consumes_lifecycle_learning(contract: Mapping[str, Any]) -> bool:
    learning_used = _mapping(contract.get("learning_used"))
    if _learning_rows_present(learning_used.get("alpha_setup_action_values")):
        return True
    if _learning_rows_present(learning_used.get("trigger_profile_learning")):
        return True
    router = _mapping(learning_used.get("pm_lifecycle_learning_router"))
    for field in (
        "decision_learning_rows",
        "accepted_learning",
        "trigger_profile_learning_rows",
        "trigger_profile_learning",
    ):
        if _learning_rows_present(router.get(field)):
            return True
    return False


def _pm_lifecycle_trace_errors(contract: Mapping[str, Any]) -> List[str]:
    requires_trace = (
        _contract_has_any_rank(contract)
        or contract_has_full_market_capital_rank(contract)
        or _contract_consumes_lifecycle_learning(contract)
        or bool(lifecycle_learning_decision_contract_errors(contract))
    )
    if not requires_trace:
        return []

    evidence = _mapping(contract.get("evidence_used"))
    learning_used = _mapping(contract.get("learning_used"))
    trace = evidence.get("pm_lifecycle_learning_trace")
    if not isinstance(trace, Mapping):
        trace = learning_used.get("pm_lifecycle_learning_trace")
    impact = evidence.get("pm_lifecycle_learning_impact_delta")
    if not isinstance(impact, Mapping):
        impact = learning_used.get("pm_lifecycle_learning_impact_delta")

    errors: List[str] = []
    if not isinstance(trace, Mapping):
        errors.append("pm_lifecycle_learning_trace_missing")
    if not isinstance(impact, Mapping):
        errors.append("pm_lifecycle_learning_impact_delta_missing")
    return errors


def _artifact_boundary_errors(pm_artifact: Dict[str, Any] | None) -> List[str]:
    if pm_artifact is None:
        return []
    if not isinstance(pm_artifact, dict):
        return ["invalid_pm_artifact"]
    return [
        f"pm_artifact_boundary_violation:{violation}"
        for violation in pm_artifact_boundary_violations(pm_artifact)
    ]


def check_final_action_contract(
    contract: Dict[str, Any] | None,
    *,
    pm_artifact: Dict[str, Any] | None = None,
    snapshot: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Check final_action_contract consistency without mutating it."""
    if not isinstance(contract, dict):
        return {"ok": False, "errors": ["missing_final_action_contract"], "tool": "pm_contract_self_check"}
    current = _as_int(contract.get("current_lots"))
    target = _as_int(contract.get("target_lots"))
    lots_delta = _as_int(contract.get("lots_delta"))
    authority_type = str(contract.get("authority_type") or "").strip().lower()
    final_action = str(contract.get("final_action") or "").strip().lower()
    errors: List[str] = []
    errors.extend(_base_field_errors(contract))
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
    evidence = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
    lifecycle_trace = (
        evidence.get("pm_lifecycle_learning_trace")
        if isinstance(evidence.get("pm_lifecycle_learning_trace"), dict)
        else evidence.get("lifecycle_learning_trace")
        if isinstance(evidence.get("lifecycle_learning_trace"), dict)
        else {}
    )
    contract_lifecycle_self_check = (
        evidence.get("contract_lifecycle_self_check")
        if isinstance(evidence.get("contract_lifecycle_self_check"), dict)
        else lifecycle_trace.get("contract_lifecycle_self_check")
        if isinstance(lifecycle_trace.get("contract_lifecycle_self_check"), dict)
        else {}
    )
    if contract_lifecycle_self_check and not bool(contract_lifecycle_self_check.get("ok")):
        errors.append("contract_lifecycle_self_check_failed")
    errors.extend(full_market_rank_gate_errors(contract))
    errors.extend(rank_capital_layer_contract_errors(contract))
    errors.extend(_rank_score_trace_errors(contract))
    errors.extend(rank_lifecycle_learning_route_errors(contract))
    errors.extend(lifecycle_learning_decision_contract_errors(contract))
    errors.extend(_pm_lifecycle_trace_errors(contract))
    errors.extend(_artifact_boundary_errors(pm_artifact))
    errors.extend(_artifact_boundary_errors(snapshot))
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


def assert_final_action_contract(
    contract: Dict[str, Any] | None,
    *,
    pm_artifact: Dict[str, Any] | None = None,
    snapshot: Dict[str, Any] | None = None,
) -> None:
    result = check_final_action_contract(contract, pm_artifact=pm_artifact, snapshot=snapshot)
    if not result.get("ok"):
        raise ValueError(f"pm_final_action_contract_self_check_failed:{result.get('errors')}")
