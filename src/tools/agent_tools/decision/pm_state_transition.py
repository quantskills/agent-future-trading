"""PM-owned opportunity state transition helper.

The helper codifies the state semantics described in
docs/mechanism_agent_internal_rules.md. It is deterministic and side-effect
free: no DB writes, no artifact writes, and no contract signing.
"""

from __future__ import annotations

from typing import Any, Dict


def _state(value: Any) -> str:
    return str(value or "").strip().lower()


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
