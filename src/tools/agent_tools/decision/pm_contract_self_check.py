"""PM final_action_contract self-check.

This helper validates PM's own contract before downstream Auditor/Trader use it.
It is side-effect free and must not repair or rewrite the contract.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from tools.common.final_action_semantics import (
    contract_has_full_market_capital_rank,
    contract_increases_risk_position,
    contract_requires_full_market_capital_rank,
    full_market_rank_gate_errors,
    lifecycle_learning_decision_contract_errors,
    rank_capital_layer_contract_errors,
    rank_lifecycle_learning_route_errors,
)
from tools.common.execution_trigger_semantics import (
    CANONICAL_EXECUTION_PROFILES,
    execution_trigger_contract_error,
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
)

FINAL_ACTION_CONTRACT_DICT_FIELDS = (
    "learning_used",
    "evidence_used",
    "capital_deployment",
)

FINAL_ACTION_CONTRACT_LIST_FIELDS = ("reason_codes",)
STEP5_UNDEPLOYED_NO_NEW_EXPOSURE_REASON_PREFIXES = (
    "no_rank_no_new_exposure",
    "no_rank_or_budget_no_new_exposure",
)
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


def _is_step5_undeployed_no_new_exposure_reason(value: Any) -> bool:
    reason = str(value or "").strip().lower()
    return any(
        reason == prefix or reason.startswith(f"{prefix}:")
        for prefix in STEP5_UNDEPLOYED_NO_NEW_EXPOSURE_REASON_PREFIXES
    )


def _step5_undeployed_no_new_exposure_errors(
    contract: Mapping[str, Any],
    deployment: Mapping[str, Any],
) -> List[str]:
    errors: List[str] = []
    if deployment.get("selected_for_capital_deployment") is not False:
        errors.append("undeployed_new_risk_selected_flag_invalid")
    if contract_increases_risk_position(contract):
        errors.append("undeployed_new_risk_contract_still_increases_risk")
    if not _is_step5_undeployed_no_new_exposure_reason(deployment.get("capital_allocation_reason")):
        errors.append("undeployed_new_risk_capital_deployment_reason_invalid")
    return errors


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


def _semantic_object_errors(contract: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    deployment = contract.get("capital_deployment")
    if isinstance(deployment, Mapping):
        if not deployment:
            errors.append("capital_deployment_missing")
        elif _is_step5_undeployed_no_new_exposure_reason(deployment.get("capital_allocation_reason")):
            errors.extend(_step5_undeployed_no_new_exposure_errors(contract, deployment))
        elif not _contract_has_any_rank(contract) and not contract_requires_full_market_capital_rank(contract):
            if deployment.get("selected_for_capital_deployment") is not False:
                errors.append("non_rank_capital_deployment_selected_flag_invalid")
            reason = deployment.get("capital_allocation_reason")
            if not _present(reason):
                errors.append("non_rank_capital_deployment_reason_missing")
            elif str(reason) != "non_new_risk_no_capital_rank":
                errors.append("non_rank_capital_deployment_reason_invalid")
        if contract_requires_full_market_capital_rank(contract):
            if deployment.get("selected_for_capital_deployment") is not True:
                errors.append("approved_new_risk_not_selected_for_capital_deployment")
            if not _present(deployment.get("capital_allocation_reason")):
                errors.append("approved_new_risk_capital_allocation_reason_missing")
    evidence = _mapping(contract.get("evidence_used"))
    sizing = evidence.get("position_sizing_result")
    if not isinstance(sizing, Mapping) or not sizing:
        errors.append("position_sizing_result_missing")
    else:
        current = _as_int(contract.get("current_lots"))
        target = _as_int(contract.get("target_lots"))
        delta = _as_int(contract.get("lots_delta"))
        if _as_int(sizing.get("current_lots")) != current:
            errors.append("position_sizing_result_current_lots_mismatch")
        if _as_int(sizing.get("target_lots")) != target:
            errors.append("position_sizing_result_target_lots_mismatch")
        if _as_int(sizing.get("lots_delta")) != delta:
            errors.append("position_sizing_result_lots_delta_mismatch")
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


def _row_value(row: Mapping[str, Any], field: str) -> Any:
    payload = _mapping(row.get("payload"))
    value = row.get(field)
    return value if value not in (None, "") else payload.get(field)


def _alpha_setup_action_value_purity_errors(contract: Mapping[str, Any]) -> List[str]:
    learning_used = _mapping(contract.get("learning_used"))
    rows = learning_used.get("alpha_setup_action_values")
    if rows in (None, ""):
        return []
    if not isinstance(rows, list):
        return ["alpha_setup_action_values_invalid"]

    errors: List[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            errors.append(f"alpha_setup_action_value_invalid:{index}")
            continue
        if _row_value(row, "canonical_action_value") is not True:
            errors.append(f"alpha_setup_action_value_not_canonical:{index}")
        for field in (
            "canonical_action_family",
            "action_preference",
            "action_value_lane",
            "learning_lane",
        ):
            if not _present(_row_value(row, field)):
                errors.append(f"alpha_setup_action_value_missing_{field}:{index}")
        evidence_scope = str(_row_value(row, "evidence_scope") or "").strip().lower()
        canonical_source = str(_row_value(row, "canonical_action_value_source") or "").strip().lower()
        if evidence_scope == "similar_sql_prior" and _row_value(row, "canonical_action_value") is not True:
            errors.append(f"alpha_setup_action_value_incomplete_similar_sql_prior:{index}")
        if canonical_source == "incomplete_trace_not_for_pm_scoring":
            errors.append(f"alpha_setup_action_value_incomplete_trace_not_for_pm_scoring:{index}")
    return errors


def _pm_lifecycle_trace_errors(contract: Mapping[str, Any]) -> List[str]:
    requires_trace = (
        _contract_has_any_rank(contract)
        or contract_has_full_market_capital_rank(contract)
        or _contract_consumes_lifecycle_learning(contract)
        or bool(lifecycle_learning_decision_contract_errors(contract))
    )
    if not requires_trace:
        return []

    learning_used = _mapping(contract.get("learning_used"))
    trace = learning_used.get("pm_lifecycle_learning_trace")
    impact = learning_used.get("pm_lifecycle_learning_impact_delta")

    errors: List[str] = []
    if not isinstance(trace, Mapping):
        errors.append("pm_lifecycle_learning_trace_missing")
    if not isinstance(impact, Mapping):
        errors.append("pm_lifecycle_learning_impact_delta_missing")
    return errors


def _execution_contract_errors(contract: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    profile = str(contract.get("execution_profile") or "").strip().lower()
    trigger_source = str(contract.get("trigger_source") or "").strip()
    if profile not in CANONICAL_EXECUTION_PROFILES:
        errors.append("execution_profile_not_canonical")
    if not trigger_source:
        errors.append("trigger_source_missing")
    elif execution_trigger_contract_error(
        profile=profile,
        side=("long" if _as_int(contract.get("target_lots")) > 0 else "short"),
        entry_trigger=contract.get("entry_trigger"),
        trigger_source=trigger_source,
    ) == "execution_trigger_source_contract_invalid":
        errors.append("execution_profile_trigger_source_mismatch")

    final_action = str(contract.get("final_action") or "").strip().lower()
    if final_action not in {"open_probe", "open_real", "scale"}:
        return errors
    if not str(contract.get("entry_trigger") or "").strip():
        errors.append("new_risk_execution_missing_entry_trigger")
    elif execution_trigger_contract_error(
        profile=profile,
        side=("long" if _as_int(contract.get("target_lots")) > 0 else "short"),
        entry_trigger=contract.get("entry_trigger"),
        trigger_source=trigger_source,
    ) == "execution_entry_trigger_contract_invalid":
        errors.append("execution_entry_trigger_contract_invalid")
    invalidation = str(contract.get("invalidation") or "").strip().lower()
    invalidation_condition_present = invalidation not in {"", "unknown", "none", "n/a", "null"}
    invalidation_level = contract.get("invalidation_level")
    try:
        invalidation_level_present = (
            not isinstance(invalidation_level, bool)
            and invalidation_level not in (None, "")
            and float(invalidation_level) > 0
        )
    except (TypeError, ValueError):
        invalidation_level_present = False
    atr_stop_distance = contract.get("atr_stop_distance")
    try:
        atr_stop_present = (
            not isinstance(atr_stop_distance, bool)
            and atr_stop_distance not in (None, "")
            and float(atr_stop_distance) > 0
        )
    except (TypeError, ValueError):
        atr_stop_present = False
    if not (invalidation_condition_present or invalidation_level_present or atr_stop_present):
        errors.append("new_risk_execution_missing_invalidation")
    return errors


def check_final_action_contract(
    contract: Dict[str, Any] | None,
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
    errors.extend(_semantic_object_errors(contract))
    errors.extend(_alpha_setup_action_value_purity_errors(contract))
    errors.extend(_execution_contract_errors(contract))
    if lots_delta != target - current:
        errors.append("lots_delta_mismatch")
    expected = _expected_action(current, target, authority_type=authority_type)
    if final_action != expected:
        errors.append("final_action_mismatch")
    requires_intraday = bool(contract.get("requires_intraday_confirmation"))
    conditional_authority = bool(contract.get("conditional_trigger_authority"))
    can_execute_without_trigger = bool(contract.get("can_execute_without_intraday_trigger"))
    if requires_intraday and can_execute_without_trigger:
        errors.append("conditional_trigger_conflict")
    if requires_intraday and target == current:
        errors.append("conditional_trigger_without_lot_delta")
    if conditional_authority and target == current:
        errors.append("conditional_trigger_authority_without_lot_delta")
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
    errors.extend(full_market_rank_gate_errors(contract))
    errors.extend(rank_capital_layer_contract_errors(contract))
    errors.extend(_rank_score_trace_errors(contract))
    errors.extend(rank_lifecycle_learning_route_errors(contract))
    errors.extend(lifecycle_learning_decision_contract_errors(contract))
    errors.extend(_pm_lifecycle_trace_errors(contract))
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
