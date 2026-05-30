from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict


def resolve_commission_rule(execution_config: Dict[str, Any], underlying_code: str) -> Dict[str, Any]:
    commission_config = execution_config.get("commission", {}) or {}
    if not commission_config.get("enabled", False):
        return {"mode": "by_value", "open": 0.0, "close": 0.0, "close_today": 0.0}

    by_underlying = commission_config.get("by_underlying", {}) or {}
    rule = by_underlying.get(underlying_code)
    if rule is None:
        if commission_config.get("require_explicit_rule_for_all_tickers", False):
            raise RuntimeError(f"Missing commission rule for underlying {underlying_code}")
        rule = commission_config.get("default_rule")

    if not rule:
        raise RuntimeError(f"Commission rule is empty for underlying {underlying_code}")

    mode = rule.get("mode")
    if mode not in {"by_value", "by_lot"}:
        raise RuntimeError(f"Unsupported commission mode for {underlying_code}: {mode}")

    return rule


def classify_offset_scope(action: Any, intraday_close: bool = False) -> str:
    action_value = action.value if hasattr(action, "value") else str(action)

    if action_value in {"open_long", "open_short"}:
        return "open"
    if action_value in {"close_long", "close_short"}:
        return "close_today" if intraday_close else "close"

    raise RuntimeError(f"Unsupported futures action for commission classification: {action_value}")


def calculate_commission(
    rule: Dict[str, Any],
    execution_price: float,
    lots: int,
    contract_multiplier: float,
    offset_scope: str,
    rounding: float = 0.01,
) -> float:
    if lots <= 0:
        return 0.0

    if offset_scope not in rule:
        raise RuntimeError(f"Commission rule missing offset scope: {offset_scope}")

    mode = rule["mode"]
    scope_value = float(rule[offset_scope])
    if scope_value < 0:
        raise RuntimeError(f"Commission value cannot be negative for scope {offset_scope}")

    if mode == "by_value":
        raw_commission = float(execution_price) * int(lots) * float(contract_multiplier) * scope_value
    elif mode == "by_lot":
        raw_commission = int(lots) * scope_value
    else:
        raise RuntimeError(f"Unsupported commission mode: {mode}")

    return round_commission(raw_commission, rounding)


def round_commission(value: float, rounding: float = 0.01) -> float:
    quantum = Decimal(str(rounding))
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))
