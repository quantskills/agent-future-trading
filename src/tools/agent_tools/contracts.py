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
    return snapshot
