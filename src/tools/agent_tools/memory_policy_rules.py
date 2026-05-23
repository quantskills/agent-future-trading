from __future__ import annotations

"""Memory-policy helpers for protected/deployable/watchlist/weak templates."""

from typing import Any, Mapping, Sequence


def strategy_memory_record(strategy_memory: Mapping[str, Any] | None, states: Sequence[str]) -> dict[str, Any]:
    if not isinstance(strategy_memory, Mapping):
        return {}
    wanted = {str(item) for item in states}
    for key in ("combo", "side_memory"):
        row = strategy_memory.get(key)
        if isinstance(row, Mapping) and str(row.get("memory_state") or "") in wanted:
            return dict(row)
    for row in strategy_memory.get("records") or []:
        if isinstance(row, Mapping) and str(row.get("memory_state") or "") in wanted:
            return dict(row)
    return {}
