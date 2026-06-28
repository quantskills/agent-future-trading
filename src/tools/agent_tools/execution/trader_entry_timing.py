from __future__ import annotations

"""Entry timing state labels for deterministic Phase2 execution."""

from typing import Any


def opening_range_status(price_context: Any) -> dict[str, Any]:
    base_price = getattr(price_context, "base_price", None)
    source = getattr(price_context, "base_price_source", "") or ""
    warning = getattr(price_context, "warning_message", None)
    return {
        "has_execution_basis": base_price is not None,
        "base_price_source": source,
        "opening_range_complete": source not in {"missing", "unavailable"} and base_price is not None,
        "warning": warning,
    }


def entry_action_family(target_lots: int, current_lots: int) -> str:
    if target_lots == current_lots:
        return "hold"
    if current_lots == 0 and target_lots > 0:
        return "open_long"
    if current_lots == 0 and target_lots < 0:
        return "open_short"
    if target_lots == 0:
        return "exit"
    if (target_lots > 0) == (current_lots > 0):
        return "add" if abs(target_lots) > abs(current_lots) else "reduce"
    return "reverse"


def phase2_entry_audit(*, target_lots: int, current_lots: int, price_context: Any) -> dict[str, Any]:
    return {
        "entry_action_family": entry_action_family(target_lots, current_lots),
        "opening_range": opening_range_status(price_context),
        "target_lots_source": "final_action_contract",
    }
