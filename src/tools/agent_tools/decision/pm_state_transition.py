"""PM-owned opportunity state transition helper.

The helper codifies the state semantics described in
docs/mechanism_agent_internal_rules.md. It is deterministic and side-effect
free: no DB writes, no artifact writes, and no contract signing.
"""

from __future__ import annotations

from typing import Any, Dict


OPPORTUNITY_STATES = {
    "no_opportunity",
    "watch_for_trigger",
    "probe_candidate",
    "tradeable_candidate",
}


def _state(value: Any) -> str:
    return str(value or "").strip().lower()


def classify_new_entry_transition(
    *,
    opportunity_state: str,
    setup_complete: bool,
    entry_trigger_present: bool,
    invalidation_present: bool,
    current_trigger_confirmed: bool,
    risk_budget_ok: bool,
    evidence_strength: str = "normal",
    hard_block: bool = False,
    negative_learning_block: bool = False,
    positive_learning: bool = False,
    rank_priority: bool = False,
    scale_allowed: bool = False,
) -> Dict[str, Any]:
    """Classify a flat-position PM new-entry candidate.

    The output is a PM-internal transition decision. PM still has to run sizing,
    risk gate, contract build, self-check, and independent Auditor before Trader.
    """
    state = _state(opportunity_state)
    missing = []
    if not setup_complete:
        missing.append("setup")
    if not entry_trigger_present:
        missing.append("entry_trigger")
    if not invalidation_present:
        missing.append("invalidation_condition")
    if hard_block:
        decision = "wait"
        final_action_hint = "wait"
        reason = "hard_block"
    elif negative_learning_block:
        decision = "wait"
        final_action_hint = "wait"
        reason = "negative_learning_block"
    elif state == "no_opportunity" or state not in OPPORTUNITY_STATES:
        decision = "wait"
        final_action_hint = "wait"
        reason = "no_opportunity"
    elif missing:
        decision = "wait"
        final_action_hint = "wait"
        reason = "missing_required_setup_fields"
    elif state == "watch_for_trigger" and not current_trigger_confirmed:
        if risk_budget_ok:
            decision = "conditional_trigger_contract"
            final_action_hint = "open_probe"
            reason = "watch_for_trigger_waiting_intraday_confirmation"
        else:
            decision = "wait"
            final_action_hint = "wait"
            reason = "risk_budget_not_ok"
    elif state == "watch_for_trigger":
        decision = "open_probe" if risk_budget_ok else "wait"
        final_action_hint = decision
        reason = "watch_for_trigger_currently_confirmed" if risk_budget_ok else "risk_budget_not_ok"
    elif state == "probe_candidate":
        decision = "open_probe" if current_trigger_confirmed and risk_budget_ok else "watch_for_trigger"
        final_action_hint = "open_probe" if decision == "open_probe" else "wait"
        reason = "probe_candidate_confirmed" if decision == "open_probe" else "probe_candidate_missing_confirmation_or_budget"
    elif state == "tradeable_candidate":
        strong = _state(evidence_strength) in {"strong", "high", "deployable"}
        can_real = current_trigger_confirmed and risk_budget_ok and (strong or positive_learning or rank_priority)
        if can_real and scale_allowed:
            decision = "scale"
            final_action_hint = "scale"
            reason = "tradeable_candidate_scale_allowed"
        elif can_real:
            decision = "open_real"
            final_action_hint = "open_real"
            reason = "tradeable_candidate_real_entry"
        elif current_trigger_confirmed and risk_budget_ok:
            decision = "open_probe"
            final_action_hint = "open_probe"
            reason = "tradeable_candidate_probe_only"
        else:
            decision = "watch_for_trigger"
            final_action_hint = "wait"
            reason = "tradeable_candidate_missing_confirmation_or_budget"
    else:
        decision = "wait"
        final_action_hint = "wait"
        reason = "unknown_state"
    return {
        "tool": "pm_state_transition",
        "opportunity_state": state,
        "decision": decision,
        "final_action_hint": final_action_hint,
        "reason": reason,
        "missing_required_fields": missing,
        "requires_intraday_confirmation": decision == "conditional_trigger_contract",
        "can_execute_without_intraday_trigger": decision in {"open_probe", "open_real", "scale"},
        "writes_db": False,
        "writes_artifact": False,
        "writes_payload": False,
    }


def classify_pm_decision_state(
    *,
    current_lots: int,
    target_lots: int,
    scorecard_state: str = "",
    has_alpha_protect_records: bool = False,
) -> str:
    """Return the PM decision state label used in recommendation snapshots."""
    current = int(current_lots or 0)
    target = int(target_lots or 0)
    state = _state(scorecard_state)
    if target == 0:
        pm_state = "no_opportunity"
    elif has_alpha_protect_records:
        pm_state = "tradeable_candidate"
    elif abs(target) < abs(current):
        pm_state = "risk_reduction_candidate"
    elif current == 0 and target != 0:
        pm_state = "probe_candidate"
    elif abs(target) > abs(current):
        pm_state = "probe_candidate"
    else:
        pm_state = "watch_for_trigger"
    if state in {"tradeable_candidate", "probe_candidate", "watch_for_trigger", "no_opportunity"}:
        if pm_state not in {"risk_reduction_candidate", "no_opportunity"}:
            pm_state = state
    return pm_state
