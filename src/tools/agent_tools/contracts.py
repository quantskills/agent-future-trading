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
    "opportunity_layer",
    "opportunity_state",
    "entry_trigger",
    "exit_hint",
    "holding_period_hint",
    "factor_focus",
    "current_evidence_conflict",
    "invalidation_level",
]


def date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    text = str(value or "")
    return text[:10] if len(text) >= 10 else text


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
    opportunity_layer: str = "direction_only",
    opportunity_state: str = "watch_for_trigger",
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
        "opportunity_layer": str(opportunity_layer or "direction_only"),
        "opportunity_state": str(opportunity_state or "watch_for_trigger"),
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
