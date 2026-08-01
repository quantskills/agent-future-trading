from __future__ import annotations

"""Safety boundaries for learned adaptive policy rows.

Researcher may create candidate memories and adaptive policies after Phase4,
but PM can only let them affect future strategy through bounded, validated
uses.  This module centralizes that boundary so PM and audits do not drift into
separate interpretations.
"""

from typing import Any, Dict, Iterable, List, Mapping, Tuple


VALIDATED_RULE_STATUSES = {
    "validated_rule_applied",
    "validated_policy",
    "validated",
    "applied",
}

PM_ACTIONS_BY_POLICY_TYPE = {
    "alpha_promotion": {"protect", "allow"},
    "fast_candidate_alpha": {"probe", "watchlist"},
    "template_quality": {"protect", "allow", "cap"},
    "learned_vs_unlearned": {"cap", "demote"},
    "loss_template_policy": {"cap"},
    "fast_loss_sentinel": {"cap"},
    "tail_loss_sentinel": {"cap", "reduce", "protect"},
    "contextual_rule_calibration": {"cap", "protect", "allow", "calibrate"},
}

PM_ACTIONS_BY_POLICY_PREFIX = {
    "learning_mechanism:": {"cap", "protect", "allow"},
    "contextual_rule_calibration:": {"cap", "protect", "allow", "calibrate"},
}

CANDIDATE_POLICY_TYPES = {
    "fast_candidate_alpha",
}

REDUCTION_ACTIONS = {"cap", "reduce", "block", "demote", "probe_only", "weak_block"}
RELEASE_ACTIONS = {"protect", "allow"}
PROBE_ACTIONS = {"probe", "watchlist"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _payload(row: Mapping[str, Any]) -> Dict[str, Any]:
    value = row.get("payload")
    return dict(value) if isinstance(value, Mapping) else {}


def _memory_contract(row: Mapping[str, Any]) -> Dict[str, Any]:
    payload = _payload(row)
    contract = payload.get("next_round_memory_contract")
    return dict(contract) if isinstance(contract, Mapping) else {}


def _policy_type_allowed_action(policy_type: str, action: str) -> bool:
    if policy_type in PM_ACTIONS_BY_POLICY_TYPE:
        return action in PM_ACTIONS_BY_POLICY_TYPE[policy_type]
    for prefix, actions in PM_ACTIONS_BY_POLICY_PREFIX.items():
        if policy_type.startswith(prefix):
            return action in actions
    return False


def _contract_forbids_position_impact(contract: Mapping[str, Any]) -> bool:
    authority = _lower(contract.get("position_authority"))
    max_impact = _lower(contract.get("max_position_impact"))
    boundary = " ".join(_text(item).lower() for item in contract.get("usage_boundary") or [])
    if authority in {"analysis_or_watchlist_only", "analysis_calibration_only", "analysis_prior_only"}:
        return True
    if "no_direct_position_impact" in max_impact:
        return True
    if "cannot by themselves authorize sizing" in boundary:
        return True
    return False


def _evidence_source(payload: Mapping[str, Any]) -> str:
    evidence = payload.get("evidence")
    return _lower(evidence.get("source")) if isinstance(evidence, Mapping) else ""


def adaptive_policy_runtime_decision(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Classify whether one adaptive-policy row may affect PM behavior.

    Return fields intentionally use the existing semantic vocabulary: status,
    policy_type, policy_action, validation_plan/rule_validation_status, and
    max_position_impact.  No new runtime field is required to interpret this
    result.
    """

    policy_type = _text(row.get("policy_type"))
    action = _lower(row.get("policy_action"))
    payload = _payload(row)
    contract = _memory_contract(row)
    status = _lower(payload.get("status") or contract.get("status") or "")
    maturity = _lower(contract.get("maturity_state") or payload.get("maturity_state") or "")
    validation_status = _lower(
        row.get("rule_validation_status")
        or payload.get("rule_validation_status")
        or payload.get("validation_status")
        or contract.get("rule_validation_status")
        or ""
    )
    sample_count = _int_value(
        row.get("sample_count")
        or payload.get("sample_count")
        or contract.get("sample_count")
        or 0
    )
    result = {
        "allowed": False,
        "policy_type": policy_type,
        "policy_action": action,
        "status": status,
        "maturity_state": maturity,
        "rule_validation_status": validation_status,
        "sample_count": sample_count,
        "decision": "blocked",
        "reason": "",
    }

    if not action:
        result["reason"] = "missing_policy_type_or_action"
        return result
    if not policy_type:
        if action in REDUCTION_ACTIONS:
            result.update({"allowed": True, "decision": "legacy_risk_reduction_allowed", "reason": "legacy_bounded_risk_reduction_policy"})
            return result
        if action in RELEASE_ACTIONS:
            result.update({"allowed": True, "decision": "legacy_release_candidate_allowed", "reason": "legacy_release_policy_requires_downstream_quality_gates"})
            return result
        result["reason"] = "missing_policy_type_or_action"
        return result
    if not _policy_type_allowed_action(policy_type, action):
        result["reason"] = "unknown_or_disallowed_policy_action"
        return result
    evidence_source = _evidence_source(payload)
    if policy_type == "alpha_promotion" and evidence_source == "no_trade_counterfactual_results":
        result["reason"] = "counterfactual_no_trade_cannot_create_mature_alpha_promotion"
        return result
    if policy_type.startswith("contextual_rule_calibration") and action == "calibrate":
        if validation_status and validation_status not in VALIDATED_RULE_STATUSES:
            result["reason"] = "contextual_rule_calibration_not_validated"
            return result
        result.update({"allowed": True, "decision": "bounded_calibration_allowed", "reason": "bounded_contextual_rule_calibration"})
        return result
    if _contract_forbids_position_impact(contract) and action not in REDUCTION_ACTIONS:
        result["reason"] = "memory_contract_forbids_position_impact"
        return result
    if policy_type in CANDIDATE_POLICY_TYPES:
        if action not in PROBE_ACTIONS:
            result["reason"] = "candidate_policy_not_probe_or_watchlist"
            return result
        if (
            evidence_source != "missed_opportunity_counterfactual"
            or _lower(contract.get("memory_type")) != "missed_alpha_accountability"
            or maturity != "fast_candidate_alpha"
            or _lower(contract.get("position_authority"))
            != "probe_or_small_setup_only_after_current_confirmation"
            or _lower(contract.get("max_position_impact"))
            != "same_scope_probe_or_small_trade_only"
        ):
            result["reason"] = "fast_candidate_alpha_invalid_provenance_or_authority"
            return result
        result.update({"allowed": True, "decision": "candidate_probe_only", "reason": "fast_candidate_alpha_probe_only"})
        return result
    if action in REDUCTION_ACTIONS:
        result.update({"allowed": True, "decision": "risk_reduction_allowed", "reason": "risk_reduction_policy"})
        return result
    if action in RELEASE_ACTIONS:
        if validation_status and validation_status not in VALIDATED_RULE_STATUSES:
            result["reason"] = "release_policy_not_validated"
            return result
        if status in {"candidate", "guarded", "rejected", "pending", "watchlist"}:
            result["reason"] = "candidate_status_cannot_release"
            return result
        result.update({"allowed": True, "decision": "validated_release_allowed", "reason": "validated_policy_release"})
        return result
    result["reason"] = "unsupported_policy_action"
    return result


def filter_adaptive_policy_state_for_pm(rows: Iterable[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return PM-usable rows and a compact audit trace."""

    allowed: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        decision = adaptive_policy_runtime_decision(row)
        counts[decision["decision"]] = counts.get(decision["decision"], 0) + 1
        item = dict(row)
        item.setdefault("payload", _payload(row))
        item["adaptive_policy_runtime_decision"] = decision
        if decision["allowed"]:
            allowed.append(item)
        else:
            blocked.append({
                "policy_type": decision["policy_type"],
                "policy_action": decision["policy_action"],
                "reason": decision["reason"],
                "status": decision["status"],
                "rule_validation_status": decision["rule_validation_status"],
            })
    return allowed, {
        "input_count": len([row for row in rows or [] if isinstance(row, Mapping)]),
        "allowed_count": len(allowed),
        "blocked_count": len(blocked),
        "decision_counts": counts,
        "blocked_examples": blocked[:8],
        "boundary": "candidate_preferences_must_validate_before_release; fast_candidate_alpha_probe_only",
    }
