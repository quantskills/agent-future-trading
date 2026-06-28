"""Deterministic position sizing result builder."""

from __future__ import annotations

from typing import Any, Mapping


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def build_position_sizing_result(
    *,
    ticker: str,
    current_lots: int,
    target_lots: int,
    target_position_ratio: float,
    target_value: float,
    margin_required: float,
    account_equity: float,
    margin_rate: float,
    current_net_exposure: float,
    projected_net_exposure: float,
    current_ticker_exposure: float,
    max_position_ratio: float,
    max_net_exposure: float,
    risk_level: str,
    lots_to_trade_reason: str | None,
    control_reasons: list[str] | None = None,
    capital_allocation_reason: Mapping[str, Any] | None = None,
) -> dict:
    """Record the sizing math PM will use before it signs the contract.

    This tool does not create trade authority. PM must still sign
    ``final_action_contract`` with the final action fields.
    """
    current_lots = int(current_lots or 0)
    target_lots = int(target_lots or 0)
    lots_delta = target_lots - current_lots
    return {
        "tool": "position_sizing",
        "ticker": ticker,
        "current_lots": current_lots,
        "target_lots": target_lots,
        "lots_delta": int(lots_delta),
        "lots_delta_abs": abs(int(lots_delta)),
        "target_position_ratio": float(target_position_ratio or 0.0),
        "target_value": float(target_value or 0.0),
        "margin_required": float(margin_required or 0.0),
        "account_equity": float(account_equity or 0.0),
        "target_margin_ratio_estimate": (
            abs(float(margin_required or 0.0)) / float(account_equity)
            if _safe_float(account_equity, 0.0) > 0
            else 0.0
        ),
        "margin_rate": float(margin_rate or 0.0),
        "current_net_exposure": float(current_net_exposure or 0.0),
        "projected_net_exposure": float(projected_net_exposure or 0.0),
        "current_ticker_exposure": float(current_ticker_exposure or 0.0),
        "max_position_ratio": float(max_position_ratio or 0.0),
        "max_net_exposure": float(max_net_exposure or 0.0),
        "risk_level": str(risk_level or "unknown"),
        "lots_to_trade_reason": lots_to_trade_reason or "target_plan",
        "control_reasons": sorted({str(reason) for reason in (control_reasons or []) if str(reason)}),
        "capital_allocation_reason": dict(capital_allocation_reason or {}),
        "no_final_action_authority": True,
        "no_direction_override_authority": True,
        "no_llm": True,
    }
