from __future__ import annotations

"""Machine-readable signal artifact helpers.

The analyst report is useful for humans, but evaluation and Researcher learning
need stable keys in the persisted signal artifact. This module builds that
payload without changing the analyst decision itself.
"""

from typing import Any, Dict, Mapping


STABLE_SIGNAL_METADATA_KEYS = (
    "llm_path",
    "data_usage_summary",
    "technical_parameter_calibration",
    "adaptive_params",
)


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def build_signal_artifact_payload(signal: Any) -> Dict[str, Any]:
    """Return a stable, machine-readable artifact payload for a signal row."""
    if hasattr(signal, "model_dump"):
        payload = signal.model_dump()
    elif isinstance(signal, Mapping):
        payload = dict(signal)
    else:
        payload = {}

    metadata = _as_dict(payload.get("metadata"))
    metadata.update(_as_dict(getattr(signal, "metadata", {})))
    payload["metadata"] = metadata

    audit_metadata: Dict[str, Any] = {}
    for key in STABLE_SIGNAL_METADATA_KEYS:
        if key in metadata:
            payload[key] = metadata.get(key)
            audit_metadata[key] = metadata.get(key)
        else:
            payload.setdefault(key, {} if key != "llm_path" else "")

    payload["signal_artifact_metadata"] = {
        "contract_version": "agentquant.signal_artifact.v1",
        "stable_keys": list(STABLE_SIGNAL_METADATA_KEYS),
        "audit_metadata": audit_metadata,
        "metadata_available": bool(audit_metadata),
    }
    return payload
