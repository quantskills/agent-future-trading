from __future__ import annotations

"""Protocol lineage wrappers around existing artifact contracts."""

from typing import Any, Dict, Iterable, List, Optional

from tools.common.contracts import build_artifact_header, validate_artifact_header


PROTOCOL_LINEAGE_FIELDS = ["task_id", "context_id", "phase"]


def build_protocol_artifact_header(
    *,
    task_id: str,
    context_id: str,
    phase: str,
    contract_version: str,
    agent_name: str,
    trading_date: Any,
    ticker: str,
    config_id: str = "",
    data_cutoff: str = "pre_open",
    no_lookahead_status: str = "ok",
    determinism_mode: str = "deterministic",
    source_artifacts: Optional[Iterable[str]] = None,
    validation_errors: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    header = build_artifact_header(
        contract_version=contract_version,
        agent_name=agent_name,
        trading_date=trading_date,
        ticker=ticker,
        config_id=config_id,
        data_cutoff=data_cutoff,
        no_lookahead_status=no_lookahead_status,
        determinism_mode=determinism_mode,
        source_artifacts=source_artifacts,
        validation_errors=validation_errors,
    )
    header.update({"task_id": str(task_id or ""), "context_id": str(context_id or ""), "phase": str(phase or "")})
    return header


def validate_protocol_artifact(artifact: Dict[str, Any]) -> List[str]:
    if not isinstance(artifact, dict):
        return ["protocol_artifact_missing"]
    header = artifact.get("header") if "header" in artifact else artifact.get("artifact_contract")
    errors = validate_artifact_header(header or {})
    for field in PROTOCOL_LINEAGE_FIELDS:
        if not (header or {}).get(field):
            errors.append(f"missing_protocol_{field}")
    if artifact.get("payload") is not None and not isinstance(artifact.get("payload"), dict):
        errors.append("protocol_payload_not_dict")
    return errors
