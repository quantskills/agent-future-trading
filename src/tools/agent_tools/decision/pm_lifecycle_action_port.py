"""PM lifecycle action-port classifier.

This PM-owned deterministic tool classifies the contract lifecycle port before
capital ranking. It does not rank, size, deploy capital, or sign contracts.
"""

from __future__ import annotations

from typing import Any, Mapping


NEW_RISK_PORT = "new_risk"
POSITION_HOLD_PORT = "position_hold"
CAPITAL_RELEASE_PORT = "capital_release"
WAIT_PORT = "wait"
CONDITIONAL_MONITOR_PORT = "conditional_monitor"

CONTRACT_LIFECYCLE_BY_PRIMARY_PORT = {
    NEW_RISK_PORT: "open_add_new_risk",
    POSITION_HOLD_PORT: "hold",
    CAPITAL_RELEASE_PORT: "reduce_exit",
    WAIT_PORT: "wait",
    CONDITIONAL_MONITOR_PORT: "conditional_monitor",
}

_EXPLICIT_TRANSITION_REASONS = {
    "risk_gate_flat_target_no_new_exposure": "risk_gate_flat_target_no_new_exposure",
    "no_rank_no_new_exposure": "no_rank_no_new_exposure",
    "no_rank_or_budget_no_new_exposure": "no_rank_or_budget_no_new_exposure",
    "capital_queue_not_selected": "capital_queue_not_selected",
    "budget_insufficient": "budget_insufficient",
    "pm_risk_gate_block": "pm_risk_gate_block",
    "hard_risk_block": "hard_risk_block",
}


def _clean(value: Any) -> str:
    return str(value or "").strip().lower()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def classify_lifecycle_action_port(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    """Classify which PM decision port a candidate belongs to."""
    payload = contract if isinstance(contract, Mapping) else {}
    current_lots = _int(payload.get("current_lots"))
    target_lots = _int(payload.get("target_lots"))
    lots_delta = target_lots - current_lots
    action = _clean(payload.get("final_action"))
    requires_intraday = bool(payload.get("requires_intraday_confirmation"))
    conditional_authority = bool(payload.get("conditional_trigger_authority"))
    reason_codes = {
        _clean(item)
        for item in (payload.get("reason_codes") or [])
        if _clean(item)
    }
    no_new_exposure_reason = bool(
        {
            "no_rank_no_new_exposure",
            "no_rank_or_budget_no_new_exposure",
            "capital_queue_not_selected",
        }
        & reason_codes
    )

    if (conditional_authority or requires_intraday) and target_lots == current_lots and no_new_exposure_reason:
        port = CONDITIONAL_MONITOR_PORT
        requires_full_market_rank = False
    elif current_lots == 0 and target_lots == 0:
        port = WAIT_PORT
        requires_full_market_rank = False
    elif current_lots == 0 and target_lots != 0:
        port = NEW_RISK_PORT
        requires_full_market_rank = True
    elif target_lots == current_lots:
        port = POSITION_HOLD_PORT
        requires_full_market_rank = False
    elif target_lots == 0:
        port = CAPITAL_RELEASE_PORT
        requires_full_market_rank = False
    elif (current_lots > 0 and target_lots < 0) or (current_lots < 0 and target_lots > 0):
        port = NEW_RISK_PORT
        requires_full_market_rank = False
    elif abs(target_lots) > abs(current_lots):
        port = NEW_RISK_PORT
        requires_full_market_rank = False
    elif abs(target_lots) < abs(current_lots):
        port = CAPITAL_RELEASE_PORT
        requires_full_market_rank = False
    elif action in {"open", "open_probe", "open_real", "add", "scale", "increase", "reverse", "conditional_open"}:
        port = NEW_RISK_PORT
        requires_full_market_rank = False
    elif action in {"reduce", "exit", "close", "risk_exit"}:
        port = CAPITAL_RELEASE_PORT
        requires_full_market_rank = False
    else:
        port = WAIT_PORT
        requires_full_market_rank = False

    return {
        "tool": "pm_lifecycle_action_port",
        "pm_lifecycle_action_port": port,
        "current_lots": current_lots,
        "target_lots": target_lots,
        "lots_delta": lots_delta,
        "requires_full_market_rank": requires_full_market_rank,
        "writes_db": False,
        "writes_contract": False,
        "no_llm": True,
    }


def primary_port_to_contract_lifecycle(primary_port: Any) -> str:
    """Return the final-contract lifecycle name corresponding to the PM primary port."""
    return CONTRACT_LIFECYCLE_BY_PRIMARY_PORT.get(_clean(primary_port), WAIT_PORT)


def _reason_set(*values: Any) -> set[str]:
    reasons: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            reasons.update(_clean(item) for item in value.get("reason_codes") or [] if _clean(item))
            reasons.update(_clean(item) for item in value.get("control_reasons") or [] if _clean(item))
            continue
        if isinstance(value, (list, tuple, set)):
            reasons.update(_clean(item) for item in value if _clean(item))
            continue
        cleaned = _clean(value)
        if cleaned:
            reasons.add(cleaned)
    return reasons


def build_lifecycle_transition_diagnostic(
    *,
    primary_lifecycle_action_port: Mapping[str, Any] | str | None,
    contract_lifecycle_port: Mapping[str, Any] | str | None,
    reason_codes: Any = None,
    control_reasons: Any = None,
) -> dict[str, Any]:
    """Build a PM-internal lifecycle transition diagnostic.

    This is provenance for PM's learning/diagnostic path only. It does not
    gate final contracts, route learning, rank candidates, deploy capital,
    mutate lots, or sign contracts.
    """
    primary_payload = (
        primary_lifecycle_action_port
        if isinstance(primary_lifecycle_action_port, Mapping)
        else {"pm_lifecycle_action_port": primary_lifecycle_action_port}
    )
    contract_payload = (
        contract_lifecycle_port
        if isinstance(contract_lifecycle_port, Mapping)
        else {"pm_lifecycle_action_port": contract_lifecycle_port}
    )
    primary_port = _clean(primary_payload.get("pm_lifecycle_action_port"))
    contract_port = _clean(contract_payload.get("pm_lifecycle_action_port"))
    expected_contract_lifecycle = primary_port_to_contract_lifecycle(primary_port)
    actual_contract_lifecycle = primary_port_to_contract_lifecycle(contract_port)
    if contract_port in {"open_add_new_risk", "hold", "reduce_exit"}:
        actual_contract_lifecycle = contract_port
    if expected_contract_lifecycle == actual_contract_lifecycle:
        transition_reason = "consistent"
        ok = True
    else:
        reasons = _reason_set(reason_codes, control_reasons)
        transition_reason = "unexplained_lifecycle_port_transition"
        for reason in sorted(reasons):
            matched_reason = next(
                (
                    accepted
                    for accepted in _EXPLICIT_TRANSITION_REASONS
                    if reason == accepted or reason.startswith(f"{accepted}:")
                ),
                "",
            )
            if matched_reason:
                transition_reason = _EXPLICIT_TRANSITION_REASONS[matched_reason]
                break
        ok = transition_reason != "unexplained_lifecycle_port_transition"
    return {
        "tool": "pm_lifecycle_action_port",
        "diagnostic_type": "lifecycle_transition_diagnostic",
        "primary_lifecycle_action_port": primary_port,
        "expected_contract_lifecycle_port": expected_contract_lifecycle,
        "actual_contract_lifecycle_port": actual_contract_lifecycle,
        "consistent": expected_contract_lifecycle == actual_contract_lifecycle,
        "ok": ok,
        "transition_reason": transition_reason,
        "diagnostic_only": True,
        "not_final_contract_gate": True,
        "does_not_route_learning": True,
        "does_not_generate_lifecycle_semantics": True,
        "writes_db": False,
        "writes_contract": False,
        "no_llm": True,
    }
