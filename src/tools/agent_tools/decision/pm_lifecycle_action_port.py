"""PM lifecycle action-port classifier.

This PM-owned deterministic tool classifies the contract lifecycle port before
capital ranking. It does not rank, size, deploy capital, or sign contracts.
"""

from __future__ import annotations

from typing import Any, Mapping


NEW_RISK_PORT = "new_risk"
POSITION_HOLD_PORT = "position_hold"
CAPITAL_RELEASE_PORT = "capital_release"
WAIT_PORT = "wait"
CONDITIONAL_MONITOR_PORT = "conditional_monitor"

def _clean(value: Any) -> str:
    return str(value or "").strip().lower()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def classify_lifecycle_action_port(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    """Classify which PM decision port a candidate belongs to."""
    payload = contract if isinstance(contract, Mapping) else {}
    current_lots = _int(payload.get("current_lots"))
    target_lots = _int(payload.get("target_lots"))
    lots_delta = target_lots - current_lots
    action = _clean(payload.get("final_action"))
    requires_intraday = bool(payload.get("requires_intraday_confirmation"))
    conditional_authority = bool(payload.get("conditional_trigger_authority"))
    reason_codes = {
        _clean(item)
        for item in (payload.get("reason_codes") or [])
        if _clean(item)
    }
    no_new_exposure_reason = bool(
        {
            "no_rank_no_new_exposure",
            "no_rank_or_budget_no_new_exposure",
            "capital_queue_not_selected",
        }
        & reason_codes
    )

    if (conditional_authority or requires_intraday) and target_lots == current_lots and no_new_exposure_reason:
        port = CONDITIONAL_MONITOR_PORT
        requires_full_market_rank = False
    elif current_lots == 0 and target_lots == 0:
        port = WAIT_PORT
        requires_full_market_rank = False
    elif current_lots == 0 and target_lots != 0:
        port = NEW_RISK_PORT
        requires_full_market_rank = True
    elif target_lots == current_lots:
        port = POSITION_HOLD_PORT
        requires_full_market_rank = False
    elif target_lots == 0:
        port = CAPITAL_RELEASE_PORT
        requires_full_market_rank = False
    elif (current_lots > 0 and target_lots < 0) or (current_lots < 0 and target_lots > 0):
        port = NEW_RISK_PORT
        requires_full_market_rank = False
    elif abs(target_lots) > abs(current_lots):
        port = NEW_RISK_PORT
        requires_full_market_rank = True
    elif abs(target_lots) < abs(current_lots):
        port = CAPITAL_RELEASE_PORT
        requires_full_market_rank = False
    elif action in {"open", "open_probe", "open_real", "add", "scale", "increase", "reverse", "conditional_open"}:
        port = NEW_RISK_PORT
        requires_full_market_rank = False
    elif action in {"reduce", "exit", "close", "risk_exit"}:
        port = CAPITAL_RELEASE_PORT
        requires_full_market_rank = False
    else:
        port = WAIT_PORT
        requires_full_market_rank = False

    return {
        "tool": "pm_lifecycle_action_port",
        "pm_lifecycle_action_port": port,
        "current_lots": current_lots,
        "target_lots": target_lots,
        "lots_delta": lots_delta,
        "requires_full_market_rank": requires_full_market_rank,
        "writes_db": False,
        "writes_contract": False,
        "no_llm": True,
    }
