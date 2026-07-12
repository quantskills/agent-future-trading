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
    "learning_scope",
    "product_profile_evidence",
    "fusion_evidence",
}

_LEGACY_ACTION_CONTRACT_KEYS = {
    "open",
    "hold",
    "exit",
    "execution",
    "state_permissions",
    "money_objective",
    "has_invalidation",
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
    if not isinstance(contract, dict):
        violations.append("analyst_output_action_evidence_contract_missing")
    else:
        for hit in _walk_forbidden(contract, path="action_evidence_contract"):
            violations.append(f"analyst_output_forbidden_trade_authority_field:{hit}")
        if contract.get("contract_version") != "agentquant.action_evidence.v1":
            violations.append("analyst_output_action_evidence_contract_version_invalid")
        agent_name = str(getattr(signal, "agent_name", "") or "")
        if str(contract.get("analyst") or "") != agent_name:
            violations.append("analyst_output_action_evidence_contract_analyst_mismatch")
        signal_value = getattr(getattr(signal, "signal", None), "value", getattr(signal, "signal", ""))
        if str(contract.get("signal") or "") != str(signal_value or ""):
            violations.append("analyst_output_action_evidence_contract_signal_mismatch")
        expected_side = "long" if str(signal_value) == "Bullish" else "short" if str(signal_value) == "Bearish" else "flat"
        if str(contract.get("side") or "") != expected_side:
            violations.append("analyst_output_action_evidence_contract_side_mismatch")
        legacy_keys = sorted(_LEGACY_ACTION_CONTRACT_KEYS.intersection(contract))
        for key in legacy_keys:
            violations.append(f"analyst_output_legacy_action_contract_field:{key}")
        state = str(contract.get("opportunity_state") or getattr(signal, "opportunity_state", "") or "").strip().lower()
        trigger_valid = bool(contract.get("trigger_valid"))
        invalidation_present = bool(contract.get("invalidation_present"))
        if state in {"probe_candidate", "tradeable_candidate"} and not trigger_valid:
            violations.append("analyst_output_candidate_without_current_trigger")
        if state in {"watch_for_trigger", "probe_candidate", "tradeable_candidate"} and not invalidation_present:
            violations.append("analyst_output_trade_setup_missing_invalidation")
        if list(contract.get("factor_focus") or []) != list(getattr(signal, "factor_focus", []) or []):
            violations.append("analyst_output_action_evidence_contract_factor_focus_mismatch")
        profile = metadata.get("product_profile_evidence") if isinstance(metadata, dict) else None
        if isinstance(profile, dict) and contract.get("product_profile_evidence") != profile:
            violations.append("analyst_output_action_evidence_contract_product_profile_mismatch")
        fusion = metadata.get("fusion_evidence") if isinstance(metadata, dict) else None
        if isinstance(fusion, dict) and contract.get("fusion_evidence") != fusion:
            violations.append("analyst_output_action_evidence_contract_fusion_evidence_mismatch")
    trade_research_contract = metadata.get("trade_research_contract") if isinstance(metadata, dict) else None
    if isinstance(trade_research_contract, dict) and "action_evidence_contract" in trade_research_contract:
        violations.append("analyst_output_duplicate_action_evidence_contract")
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
