from __future__ import annotations

"""Machine-readable formal AnalystSignal artifact helpers."""

from copy import deepcopy
from typing import Any, Dict, Mapping

from tools.common.signal_evidence_collection import validate_action_evidence_contract


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def build_signal_artifact_payload(signal: Any) -> Dict[str, Any]:
    """Persist only the validated AEC, never analyst working state."""
    metadata = _as_dict(getattr(signal, "metadata", {}))
    contract = metadata.get("action_evidence_contract")
    analyst = str(getattr(signal, "agent_name", "") or "")
    validated = validate_action_evidence_contract(contract, analyst=analyst)
    return {
        "metadata": {"action_evidence_contract": deepcopy(validated)},
        "signal_artifact_metadata": {
            "contract_version": "agentquant.signal_artifact.v1",
        },
    }
