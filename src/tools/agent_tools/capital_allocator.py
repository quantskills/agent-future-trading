from __future__ import annotations

"""Capital deployment helpers for template-aware futures allocation."""

from typing import Any, Iterable, Mapping


HIGH_QUALITY_MEMORY_STATES = {"protected", "deployable"}
WEAK_MEMORY_STATES = {"watchlist", "weak_block"}


def strategy_memory_record(strategy_memory: Mapping[str, Any] | None, states: set[str]) -> dict[str, Any]:
    if not isinstance(strategy_memory, Mapping):
        return {}
    for key in ("combo", "side_memory"):
        row = strategy_memory.get(key)
        if isinstance(row, Mapping) and str(row.get("memory_state") or "") in states:
            return dict(row)
    for row in strategy_memory.get("records") or []:
        if isinstance(row, Mapping) and str(row.get("memory_state") or "") in states:
            return dict(row)
    return {}


def has_adaptive_policy_action(rows: Iterable[Mapping[str, Any]] | None, actions: set[str]) -> bool:
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("policy_action") or "").lower() in actions:
            return True
    return False


def high_quality_learning_context(
    *,
    strategy_memory: Mapping[str, Any] | None,
    adaptive_policy_state: Iterable[Mapping[str, Any]] | None,
    allow_memory_protected_scaling: bool,
    allow_recovering_template_scaling: bool = False,
) -> tuple[bool, dict[str, Any]]:
    protected_memory = strategy_memory_record(strategy_memory, HIGH_QUALITY_MEMORY_STATES)
    recovering_memory = strategy_memory_record(strategy_memory, {"recovering"})
    adaptive_protect = has_adaptive_policy_action(adaptive_policy_state, {"protect", "allow"})
    high_quality_memory = bool(allow_memory_protected_scaling and protected_memory)
    high_quality_memory = high_quality_memory or bool(allow_recovering_template_scaling and recovering_memory)
    high_quality_memory = high_quality_memory or bool(allow_memory_protected_scaling and adaptive_protect)
    diagnostics = {
        "protected_memory": protected_memory,
        "recovering_memory": recovering_memory,
        "adaptive_protect": adaptive_protect,
    }
    return high_quality_memory, diagnostics
