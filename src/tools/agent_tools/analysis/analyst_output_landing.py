"""Validate analyst LLM output after it lands in structured fields.

This does not constrain LLM reasoning. It checks that persisted analyst output
does not contain PM-only sizing, final action, or trade-authority fields.
"""

from __future__ import annotations

from typing import Any, List

from tools.common.final_action_semantics import FORBIDDEN_ANALYST_TRADE_AUTHORITY_KEYS


ALLOWED_STRUCTURAL_KEYS = {
    "action_evidence_contract",
    "trade_research_contract",
    "internal_message_contract",
    "learning_impact_summary",
    "factor_calibration_summary",
    "event_calibration_summary",
    "data_usage_summary",
    "setup_quality",
    "state_permissions",
    "learning_scope",
    "execution",
    "product_profile_evidence",
    "fusion_evidence",
}


def _walk_forbidden(value: Any, *, path: str = "") -> List[str]:
    hits: List[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if key_text in FORBIDDEN_ANALYST_TRADE_AUTHORITY_KEYS:
                hits.append(child_path)
            hits.extend(_walk_forbidden(child, path=child_path))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            hits.extend(_walk_forbidden(child, path=f"{path}[{idx}]"))
    return hits


def analyst_output_landing_violations(signal: Any) -> List[str]:
    """Return analyst output landing violations for an AnalystSignal-like object."""
    violations: List[str] = []
    for field in (
        "learning_impact_summary",
        "factor_calibration_summary",
        "event_calibration_summary",
        "metadata",
    ):
        payload = getattr(signal, field, None)
        for hit in _walk_forbidden(payload, path=field):
            violations.append(f"analyst_output_forbidden_trade_authority_field:{hit}")
    metadata = getattr(signal, "metadata", None)
    contract = metadata.get("action_evidence_contract") if isinstance(metadata, dict) else None
    if isinstance(contract, dict):
        for hit in _walk_forbidden(contract, path="action_evidence_contract"):
            violations.append(f"analyst_output_forbidden_trade_authority_field:{hit}")
        state = str(contract.get("opportunity_state") or getattr(signal, "opportunity_state", "") or "").strip().lower()
        trigger_valid = bool(contract.get("trigger_valid"))
        invalidation_present = bool(contract.get("invalidation_present"))
        if state in {"probe_candidate", "tradeable_candidate"} and not trigger_valid:
            violations.append("analyst_output_candidate_without_current_trigger")
        if state in {"watch_for_trigger", "probe_candidate", "tradeable_candidate"} and not invalidation_present:
            violations.append("analyst_output_trade_setup_missing_invalidation")
    top_level_lots = getattr(signal, "lots", None)
    if top_level_lots not in (None, 0, ""):
        violations.append("analyst_output_top_level_lots_not_allowed")
    return sorted(set(violations))


def apply_analyst_output_landing_check(signal: Any) -> Any:
    """Append landing violations to signal.validation_errors and return signal."""
    violations = analyst_output_landing_violations(signal)
    if not violations:
        return signal
    current = list(getattr(signal, "validation_errors", []) or [])
    for violation in violations:
        if violation not in current:
            current.append(violation)
    signal.validation_errors = current
    return signal
