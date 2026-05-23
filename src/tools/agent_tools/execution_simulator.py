from __future__ import annotations

"""Execution simulation helpers for Phase2 audit payloads."""

from typing import Any


def execution_price_basis(price_context: Any) -> dict[str, Any]:
    return {
        "base_price": getattr(price_context, "base_price", None),
        "base_price_source": getattr(price_context, "base_price_source", None),
        "base_price_date": getattr(price_context, "base_price_date", None),
        "open_price": getattr(price_context, "open_price", None),
        "prev_close_price": getattr(price_context, "prev_close_price", None),
        "warning_message": getattr(price_context, "warning_message", None),
    }


def tick_slippage_amount(*, ticks: int | None, tick_size: float | None) -> float:
    if ticks is None or tick_size is None:
        return 0.0
    try:
        return float(ticks) * float(tick_size)
    except Exception:
        return 0.0
