from __future__ import annotations

"""Local artifact-contract helpers.

AgentQuant is not migrating to an A2A runtime. These helpers apply the useful
part of A2A for this codebase: each phase writes structured, auditable
artifacts with a stable header.
"""

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional


REQUIRED_AUDIT_HEADER = [
    "contract_version",
    "agent_name",
    "trading_date",
    "ticker",
    "config_id",
    "data_cutoff",
    "no_lookahead_status",
    "determinism_mode",
    "source_artifacts",
    "validation_errors",
]

REQUIRED_INTERNAL_MESSAGE_FIELDS = [
    "contract_version",
    "agent",
    "trading_date",
    "ticker",
    "message_type",
    "data_cutoff",
    "no_lookahead_status",
    "source_artifacts",
    "validation_errors",
]

REQUIRED_TRADE_RESEARCH_FIELDS = [
    "opportunity_type",
    "opportunity_state",
    "setup_type",
    "setup_quality_ok",
    "trigger_valid",
    "invalidation_present",
    "entry_trigger",
    "exit_hint",
    "holding_period_hint",
    "factor_focus",
    "current_evidence_conflict",
    "invalidation_level",
]

PM_EXPLANATION_FIELDS = {
    "capital_allocation_reason",
    "learning_adjustment_summary",
    "learning_used",
    "opportunity_rank",
    "opportunity_score",
    "opportunity_score_components",
    "position_sizing_result",
}

TRADE_AUTHORITY_FIELDS = {
    "final_action",
    "target_lots",
    "lots_delta",
    "current_lots",
    "target_position_ratio",
    "final_action_contract",
}

RESEARCH_LEARNING_FIELDS = {
    "alpha_setup_action_value",
    "alpha_setup_profile",
    "adaptive_policy_state",
    "strategy_memory",
    "researcher_llm_notes",
    "capital_deployment_state",
    "provisional_policy_state",
    "config_learning_overlay",
    "action_value",
}

PM_DOWNSTREAM_FACT_FIELDS = {
    "execution_result",
    "execution_learning_trace",
    "daily_settlement",
    "settlement_result",
}

PM_RESEARCH_FACT_OBJECT_FIELDS = {
    "researcher_llm_notes",
    "alpha_setup_action_value",
    "adaptive_policy_state",
}

PM_INTERNAL_DRAFT_FIELDS = {
    "pm_internal_draft",
    "pm_scoring_draft",
    "pm_ranking_draft",
    "pm_capital_deployment_draft",
    "pm_contract_submission_draft",
    "internal_pm_draft",
}

EXECUTION_ARTIFACT_CONTAINERS = (
    "execution_translation",
    "execution_result",
    "phase2_execution",
)

EXECUTION_CONTRACT_KEYS = {
    "execution_profile",
    "trigger_source",
    "entry_trigger",
    "invalidation",
    "valid_until",
    "requires_intraday_confirmation",
    "can_execute_without_intraday_trigger",
    "authority_type",
    "max_allowed_margin_ratio",
    "reason_codes",
    "execution_action_value_preference",
    "analyst_execution_roles",
}

FINAL_CONTRACT_EXECUTION_FIELD_KEYS = {
    "contract_version",
    "contract_type",
    "ticker",
    "underlying_code",
    "contract_code",
    "final_action",
    "current_lots",
    "target_lots",
    "lots_delta",
    "entry_trigger",
    "invalidation",
    "invalidation_condition",
    "requires_intraday_confirmation",
    "can_execute_without_intraday_trigger",
    "execution_profile",
    "execution_requirement",
    "trigger_source",
    "authority_type",
    "authority_decision",
    "reason_codes",
    "single_source_of_trade_truth",
    "candidate_sources_do_not_bypass_contract",
}

FINAL_ACTION_CONTRACT_REQUIRED_FIELDS = (
    "current_lots",
    "target_lots",
    "lots_delta",
    "final_action",
)


def date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    text = str(value or "")
    return text[:10] if len(text) >= 10 else text


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def final_action_contract_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Return the PM final_action_contract without exposing mutable ownership."""
    if not isinstance(snapshot, dict):
        return {}
    contract = snapshot.get("final_action_contract")
    if isinstance(contract, dict) and contract:
        return dict(contract)
    return {}


def validate_final_action_contract(contract: Dict[str, Any]) -> List[str]:
    """Validate the PM contract fields consumed by downstream deterministic code."""
    if not isinstance(contract, dict) or not contract:
        return ["missing_final_action_contract"]

    errors: List[str] = []
    for field in FINAL_ACTION_CONTRACT_REQUIRED_FIELDS:
        if field not in contract:
            errors.append(f"missing_final_action_contract_{field}")

    current_lots = _safe_int(contract.get("current_lots"))
    target_lots = _safe_int(contract.get("target_lots"))
    lots_delta = _safe_int(contract.get("lots_delta"))
    if "current_lots" in contract and current_lots is None:
        errors.append("invalid_final_action_contract_current_lots")
    if "target_lots" in contract and target_lots is None:
        errors.append("invalid_final_action_contract_target_lots")
    if "lots_delta" in contract and lots_delta is None:
        errors.append("invalid_final_action_contract_lots_delta")
    if current_lots is not None and target_lots is not None and lots_delta is not None:
        if lots_delta != target_lots - current_lots:
            errors.append(
                "final_action_contract_lots_delta_mismatch:"
                f"current={current_lots}:target={target_lots}:delta={lots_delta}"
            )

    if "final_action" in contract and not str(contract.get("final_action") or "").strip():
        errors.append("invalid_final_action_contract_final_action")
    return errors


def execution_contract_from_final_action_contract(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Extract only execution-rule fields; this is not a second trade contract."""
    if not isinstance(contract, dict) or not contract:
        return {}
    return {key: contract.get(key) for key in EXECUTION_CONTRACT_KEYS if key in contract}


def execution_contract_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return execution_contract_from_final_action_contract(final_action_contract_from_snapshot(snapshot))


def sanitize_execution_contract(value: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only execution-rule fields from an execution summary payload."""
    if not isinstance(value, dict) or not value:
        return {}
    return {key: value.get(key) for key in EXECUTION_CONTRACT_KEYS if key in value}


def final_contract_execution_fields(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the final contract fields a Phase2 artifact may summarize."""
    if not isinstance(contract, dict) or not contract:
        return {}
    return {key: contract.get(key) for key in FINAL_CONTRACT_EXECUTION_FIELD_KEYS if key in contract}


def final_contract_execution_fields_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return final_contract_execution_fields(final_action_contract_from_snapshot(snapshot))


def final_entry_authority_from_final_action_contract(contract: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(contract, dict) or not contract:
        return {}
    reason_codes = contract.get("reason_codes") if isinstance(contract.get("reason_codes"), list) else []
    return {
        "authority_type": contract.get("authority_type") or "not_applicable",
        "authority_decision": contract.get("authority_decision") or "not_applicable",
        "max_allowed_margin_ratio": contract.get("max_allowed_margin_ratio"),
        "reason_codes": list(reason_codes),
        "open_action_evidence": bool(contract.get("open_action_evidence")),
        "strong_current_evidence": bool(contract.get("strong_current_evidence")),
        "watch_for_trigger_block": bool(contract.get("watch_for_trigger_block")),
        "conditional_trigger_authority": bool(contract.get("conditional_trigger_authority")),
        "requires_intraday_confirmation": bool(contract.get("requires_intraday_confirmation")),
        "can_execute_without_intraday_trigger": bool(contract.get("can_execute_without_intraday_trigger")),
        "negative_profile": bool(contract.get("negative_profile")),
        "tradeable_state": bool(contract.get("tradeable_state")),
        "weak_conflict_probe": bool(contract.get("weak_conflict_probe")),
    }


def final_entry_authority_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return final_entry_authority_from_final_action_contract(final_action_contract_from_snapshot(snapshot))


def _iter_nested_dicts(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        yield prefix, value
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from _iter_nested_dicts(child, child_prefix)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
            yield from _iter_nested_dicts(child, child_prefix)


def _is_fact_object(value: Any) -> bool:
    return isinstance(value, (dict, list)) and bool(value)


def _present_forbidden_fields(node: Dict[str, Any], fields: set[str]) -> List[str]:
    return sorted(fields.intersection(node.keys()))


def _fact_object_forbidden_fields(node: Dict[str, Any], fields: set[str]) -> List[str]:
    return sorted(field for field in fields.intersection(node.keys()) if _is_fact_object(node.get(field)))


def _pm_research_alias_forbidden_fields(node: Dict[str, Any]) -> List[str]:
    fields: List[str] = []
    adaptive_scope = node.get("adaptive_policy_scope")
    if isinstance(adaptive_scope, dict) and _is_fact_object(adaptive_scope.get("policies")):
        fields.append("adaptive_policy_scope.policies")
    return fields


def execution_artifact_boundary_violations(payload: Dict[str, Any]) -> List[str]:
    """Return execution payload paths that persist PM explanation fields."""
    violations: List[str] = []
    payload = payload if isinstance(payload, dict) else {}
    object_forbidden = RESEARCH_LEARNING_FIELDS | {"daily_settlement", "settlement_result"}
    for container_name in EXECUTION_ARTIFACT_CONTAINERS:
        container = payload.get(container_name)
        if not isinstance(container, dict):
            continue
        for path, node in _iter_nested_dicts(container):
            fields = _present_forbidden_fields(node, PM_EXPLANATION_FIELDS | {"final_action_contract"})
            fields.extend(field for field in _fact_object_forbidden_fields(node, object_forbidden) if field not in fields)
            if fields:
                violations.append(f"{container_name}:{path or '<root>'}:{fields}")
    return violations


def validate_execution_artifact_boundary(payload: Dict[str, Any]) -> None:
    """Fail fast if an execution artifact tries to persist PM decision facts."""
    violations = execution_artifact_boundary_violations(payload)
    if violations:
        raise ValueError(f"execution_artifact_forbidden_pm_fields:{violations}")


def pm_artifact_boundary_violations(payload: Dict[str, Any]) -> List[str]:
    """Return PM artifact paths that persist downstream facts or PM drafts."""
    violations: List[str] = []
    payload = payload if isinstance(payload, dict) else {}
    object_forbidden = PM_DOWNSTREAM_FACT_FIELDS | RESEARCH_LEARNING_FIELDS
    for path, node in _iter_nested_dicts(payload):
        fields = _fact_object_forbidden_fields(node, object_forbidden)
        fields.extend(field for field in _pm_research_alias_forbidden_fields(node) if field not in fields)
        fields.extend(field for field in _present_forbidden_fields(node, PM_INTERNAL_DRAFT_FIELDS) if field not in fields)
        if fields:
            violations.append(f"{path or '<root>'}:{fields}")
    return violations


def validate_pm_artifact_boundary(payload: Dict[str, Any]) -> None:
    violations = pm_artifact_boundary_violations(payload)
    if violations:
        raise ValueError(f"pm_artifact_forbidden_downstream_fields:{violations}")


def auditor_artifact_boundary_violations(payload: Dict[str, Any]) -> List[str]:
    """Return Auditor artifact paths that try to mutate PM trade authority."""
    violations: List[str] = []
    payload = payload if isinstance(payload, dict) else {}
    mutation_fields = {
        "final_action_contract",
        "new_final_action_contract",
        "rewritten_final_action_contract",
        "modified_final_action_contract",
        "new_final_action",
        "new_target_lots",
        "modified_target_lots",
        "new_lots_delta",
        "modified_lots_delta",
        "trade_instruction",
        "trader_permission",
    }
    trade_authority_summary_fields = {
        "final_action",
        "current_lots",
        "target_lots",
        "lots_delta",
    }
    for path, node in _iter_nested_dicts(payload):
        fields = _present_forbidden_fields(node, mutation_fields)
        if path != "contract_summary":
            fields.extend(
                field
                for field in _present_forbidden_fields(node, trade_authority_summary_fields)
                if field not in fields
            )
        fields.extend(field for field in _fact_object_forbidden_fields(node, RESEARCH_LEARNING_FIELDS) if field not in fields)
        if fields:
            violations.append(f"{path or '<root>'}:{fields}")
    return violations


def validate_auditor_artifact_boundary(payload: Dict[str, Any]) -> None:
    violations = auditor_artifact_boundary_violations(payload)
    if violations:
        raise ValueError(f"auditor_artifact_forbidden_contract_mutation:{violations}")


def accountant_artifact_boundary_violations(payload: Dict[str, Any]) -> List[str]:
    """Return settlement artifact paths that persist learning or trade-authority fields."""
    violations: List[str] = []
    payload = payload if isinstance(payload, dict) else {}
    presence_forbidden = PM_EXPLANATION_FIELDS | {
        "llm_prompt",
        "llm_response",
        "raw_prompt",
        "raw_response",
        "final_action_contract",
        "final_action",
        "target_lots",
        "lots_delta",
    }
    for path, node in _iter_nested_dicts(payload):
        fields = _present_forbidden_fields(node, presence_forbidden)
        fields.extend(field for field in _fact_object_forbidden_fields(node, RESEARCH_LEARNING_FIELDS) if field not in fields)
        if fields:
            violations.append(f"{path or '<root>'}:{fields}")
    return violations


def validate_accountant_artifact_boundary(payload: Dict[str, Any]) -> None:
    violations = accountant_artifact_boundary_violations(payload)
    if violations:
        raise ValueError(f"accountant_artifact_forbidden_trade_or_learning_fields:{violations}")


def reviewer_artifact_boundary_violations(payload: Dict[str, Any]) -> List[str]:
    """Return Phase4 artifact paths that write research facts or mutate trade facts."""
    violations: List[str] = []
    payload = payload if isinstance(payload, dict) else {}
    presence_forbidden = {
        "new_final_action_contract",
        "rewritten_final_action_contract",
        "modified_final_action_contract",
        "modified_execution_result",
        "modified_daily_settlement",
        "write_action_value",
        "final_action_value",
    }
    for path, node in _iter_nested_dicts(payload):
        fields = _present_forbidden_fields(node, presence_forbidden)
        fields.extend(field for field in _fact_object_forbidden_fields(node, RESEARCH_LEARNING_FIELDS) if field not in fields)
        if fields:
            violations.append(f"{path or '<root>'}:{fields}")
    return violations


def validate_reviewer_artifact_boundary(payload: Dict[str, Any]) -> None:
    violations = reviewer_artifact_boundary_violations(payload)
    if violations:
        raise ValueError(f"reviewer_artifact_forbidden_research_or_mutation_fields:{violations}")


def researcher_artifact_boundary_violations(payload: Dict[str, Any]) -> List[str]:
    """Return researcher artifact paths that mutate same-day trading facts."""
    violations: List[str] = []
    payload = payload if isinstance(payload, dict) else {}
    forbidden = {
        "new_final_action_contract",
        "rewritten_final_action_contract",
        "modified_final_action_contract",
        "modified_execution_result",
        "modified_daily_settlement",
        "trade_instruction",
        "trader_permission",
        "accounting_adjustment",
    }
    for path, node in _iter_nested_dicts(payload):
        fields = sorted(forbidden.intersection(node.keys()))
        if fields:
            violations.append(f"{path or '<root>'}:{fields}")
    return violations


def validate_researcher_artifact_boundary(payload: Dict[str, Any]) -> None:
    violations = researcher_artifact_boundary_violations(payload)
    if violations:
        raise ValueError(f"researcher_artifact_forbidden_trade_fact_mutation:{violations}")


def build_artifact_header(
    *,
    contract_version: str,
    agent_name: str,
    trading_date: Any,
    ticker: str,
    config_id: str = "",
    recommendation_id: Optional[str] = None,
    evidence_pack_id: Optional[str] = None,
    data_cutoff: str = "pre_open",
    no_lookahead_status: str = "ok",
    determinism_mode: str = "deterministic",
    llm_provider: str = "",
    llm_model: str = "",
    source_artifacts: Optional[Iterable[str]] = None,
    validation_errors: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    return {
        "contract_version": contract_version,
        "agent_name": agent_name,
        "trading_date": date_text(trading_date),
        "ticker": str(ticker or "").upper(),
        "config_id": str(config_id or ""),
        "recommendation_id": recommendation_id,
        "evidence_pack_id": evidence_pack_id,
        "data_cutoff": data_cutoff,
        "no_lookahead_status": no_lookahead_status,
        "determinism_mode": determinism_mode,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "source_artifacts": list(source_artifacts or []),
        "validation_errors": list(validation_errors or []),
    }


def validate_artifact_header(header: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not isinstance(header, dict):
        return ["artifact_header_missing"]
    for field in REQUIRED_AUDIT_HEADER:
        if field not in header:
            errors.append(f"missing_header_{field}")
    if str(header.get("no_lookahead_status") or "") not in {"ok", "warning", "violation", "unchecked"}:
        errors.append("invalid_no_lookahead_status")
    if not isinstance(header.get("source_artifacts", []), list):
        errors.append("source_artifacts_not_list")
    if not isinstance(header.get("validation_errors", []), list):
        errors.append("validation_errors_not_list")
    return errors


def build_internal_message_contract(
    *,
    agent: str,
    trading_date: Any,
    ticker: str,
    message_type: str,
    data_cutoff: str = "pre_open",
    no_lookahead_status: str = "ok",
    source_artifacts: Optional[Iterable[str]] = None,
    validation_errors: Optional[Iterable[str]] = None,
    contract_version: str = "agentquant.message.v1",
) -> Dict[str, Any]:
    return {
        "contract_version": contract_version,
        "agent": str(agent or ""),
        "trading_date": date_text(trading_date),
        "ticker": str(ticker or "").upper(),
        "message_type": str(message_type or "generic"),
        "data_cutoff": data_cutoff,
        "no_lookahead_status": no_lookahead_status,
        "source_artifacts": list(source_artifacts or []),
        "validation_errors": list(validation_errors or []),
    }


def validate_internal_message_contract(contract: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not isinstance(contract, dict):
        return ["internal_message_contract_missing"]
    for field in REQUIRED_INTERNAL_MESSAGE_FIELDS:
        if field not in contract:
            errors.append(f"missing_message_{field}")
    if str(contract.get("no_lookahead_status") or "") not in {"ok", "warning", "violation", "unchecked"}:
        errors.append("invalid_message_no_lookahead_status")
    if not isinstance(contract.get("source_artifacts", []), list):
        errors.append("message_source_artifacts_not_list")
    if not isinstance(contract.get("validation_errors", []), list):
        errors.append("message_validation_errors_not_list")
    return errors


def build_trade_research_contract(
    *,
    opportunity_type: str = "unknown",
    opportunity_state: str = "watch_for_trigger",
    setup_type: str = "unknown",
    setup_quality_ok: bool = False,
    trigger_valid: bool = False,
    invalidation_present: bool = False,
    entry_trigger: str = "",
    exit_hint: str = "",
    holding_period_hint: str = "",
    factor_focus: Optional[Iterable[str]] = None,
    current_evidence_conflict: Optional[Iterable[str]] = None,
    invalidation_level: Any = None,
    atr_stop_distance: Any = None,
    sample_state: str = "current_day_evidence",
    maturity: str = "candidate",
    action_evidence_contract: Optional[Dict[str, Any]] = None,
    product_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "contract_version": "agentquant.research.v1",
        "opportunity_type": str(opportunity_type or "unknown"),
        "opportunity_state": str(opportunity_state or "watch_for_trigger"),
        "setup_type": str(setup_type or "unknown"),
        "setup_quality_ok": bool(setup_quality_ok),
        "trigger_valid": bool(trigger_valid),
        "invalidation_present": bool(invalidation_present),
        "entry_trigger": str(entry_trigger or ""),
        "exit_hint": str(exit_hint or ""),
        "holding_period_hint": str(holding_period_hint or ""),
        "factor_focus": list(factor_focus or []),
        "current_evidence_conflict": list(current_evidence_conflict or []),
        "invalidation_level": invalidation_level,
        "atr_stop_distance": atr_stop_distance,
        "sample_state": str(sample_state or "current_day_evidence"),
        "maturity": str(maturity or "candidate"),
        "action_evidence_contract": action_evidence_contract or {},
        "product_context": product_context or {},
    }


def validate_trade_research_contract(contract: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not isinstance(contract, dict):
        return ["trade_research_contract_missing"]
    for field in REQUIRED_TRADE_RESEARCH_FIELDS:
        if field not in contract:
            errors.append(f"missing_research_{field}")
    if not isinstance(contract.get("factor_focus", []), list):
        errors.append("research_factor_focus_not_list")
    if not isinstance(contract.get("current_evidence_conflict", []), list):
        errors.append("research_conflicts_not_list")
    return errors


def wrap_artifact(
    artifact_type: str,
    payload: Dict[str, Any],
    *,
    header: Dict[str, Any],
) -> Dict[str, Any]:
    errors = validate_artifact_header(header)
    if errors:
        header = {**header, "validation_errors": list(header.get("validation_errors") or []) + errors}
    return {
        "artifact_type": artifact_type,
        "header": header,
        "payload": payload or {},
    }


def attach_snapshot_contract(
    snapshot: Dict[str, Any],
    *,
    trading_date: Any,
    ticker: str,
    config_id: str = "",
    source_artifacts: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        snapshot = {}
    header = build_artifact_header(
        contract_version="agentquant.snapshot.v2",
        agent_name="portfolio_manager",
        trading_date=trading_date,
        ticker=ticker,
        config_id=config_id,
        data_cutoff="pre_open",
        no_lookahead_status="ok",
        determinism_mode="llm_with_deterministic_controls",
        source_artifacts=source_artifacts,
    )
    snapshot["artifact_contract"] = header
    snapshot["artifact_validation_errors"] = validate_artifact_header(header)
    message_contract = build_internal_message_contract(
        agent="portfolio_manager",
        trading_date=trading_date,
        ticker=ticker,
        message_type="PMDecisionArtifact",
        source_artifacts=source_artifacts,
    )
    snapshot["internal_message_contract"] = message_contract
    snapshot["internal_message_validation_errors"] = validate_internal_message_contract(message_contract)
    return snapshot
