from __future__ import annotations

"""Hard-risk reason taxonomy for the deterministic auditor."""

from tools.agent_tools.decision.pm_reason_effects import HARD_BLOCK_REASONS, is_hard_block_reason


def has_hard_block_reason(reasons: list[str], softened_reasons: set[str] | None = None) -> bool:
    return any(is_hard_block_reason(str(reason), softened_reasons=softened_reasons) for reason in reasons or [])
