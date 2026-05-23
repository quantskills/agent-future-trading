from __future__ import annotations

"""Deterministic trader exit policy helpers."""

from datetime import datetime
from typing import Any, Dict, Optional


SECTOR_BY_TICKER = {
    "BU": "chemical",
    "EB": "chemical",
    "MA": "chemical",
    "TA": "chemical",
    "HC": "ferrous",
    "I": "ferrous",
    "J": "ferrous",
    "RB": "ferrous",
    "PB": "nonferrous",
    "ZN": "nonferrous",
    "C": "agricultural",
    "CF": "agricultural",
    "M": "agricultural",
    "P": "agricultural",
    "SR": "agricultural",
}

DEFAULT_SECTOR_STOPS = {
    "ferrous": {"atr_multiplier": 2.2, "probe_time_stop_days": 2, "trend_time_stop_days": 7},
    "agricultural": {"atr_multiplier": 1.7, "probe_time_stop_days": 2, "trend_time_stop_days": 5},
    "chemical": {"atr_multiplier": 1.9, "probe_time_stop_days": 2, "trend_time_stop_days": 6},
    "nonferrous": {"atr_multiplier": 2.0, "probe_time_stop_days": 2, "trend_time_stop_days": 5},
    "generic": {"atr_multiplier": 1.8, "probe_time_stop_days": 2, "trend_time_stop_days": 5},
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _days_held(entry_date: Any, trading_date: Any) -> Optional[int]:
    if not entry_date or not trading_date:
        return None
    try:
        start = datetime.strptime(str(entry_date)[:10], "%Y-%m-%d")
        end = datetime.strptime(str(trading_date)[:10], "%Y-%m-%d")
        return max(0, (end - start).days)
    except Exception:
        return None


def resolve_exit_policy_config(config: Dict[str, Any], ticker: str, template_name: str = "") -> Dict[str, Any]:
    execution_cfg = ((config or {}).get("execution") or {}).get("exit_policy") or {}
    sector = SECTOR_BY_TICKER.get(str(ticker).upper(), "generic")
    resolved = dict(DEFAULT_SECTOR_STOPS.get(sector, DEFAULT_SECTOR_STOPS["generic"]))
    resolved.update(execution_cfg.get("defaults") or {})
    resolved.update((execution_cfg.get("sector_overrides") or {}).get(sector) or {})
    if template_name:
        resolved.update((execution_cfg.get("template_overrides") or {}).get(template_name) or {})
    resolved["sector"] = sector
    resolved["enabled"] = bool(execution_cfg.get("enabled", True))
    return resolved


def evaluate_exit_policy(
    *,
    ticker: str,
    current_price: float,
    current_lots: int,
    target_lots: int,
    lifecycle: Dict[str, Any],
    current_position: Any,
    trading_date: Any,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Return deterministic exit override hints.

    The caller still owns order translation. This function only decides whether
    an existing exposure should be reduced to zero because the structured
    lifecycle says the trade is invalidated or stale.
    """
    template_name = str(lifecycle.get("template_name") or "")
    policy = resolve_exit_policy_config(config, ticker, template_name)
    result = {
        "enabled": policy.get("enabled", True),
        "exit_required": False,
        "target_lots": target_lots,
        "reason": None,
        "policy": policy,
    }
    if not result["enabled"] or current_lots == 0:
        return result

    invalidation = lifecycle.get("invalidation_level")
    if invalidation is not None:
        level = _safe_float(invalidation)
        if current_lots > 0 and current_price <= level:
            result.update({"exit_required": True, "target_lots": 0, "reason": "invalidation_level_long"})
            return result
        if current_lots < 0 and current_price >= level:
            result.update({"exit_required": True, "target_lots": 0, "reason": "invalidation_level_short"})
            return result

    atr_distance = _safe_float(lifecycle.get("atr_stop_distance"), 0.0)
    entry_price = _safe_float(getattr(current_position, "entry_price", None), 0.0)
    if atr_distance > 0 and entry_price > 0:
        stop_distance = atr_distance * _safe_float(policy.get("atr_multiplier"), 1.8)
        if current_lots > 0 and current_price <= entry_price - stop_distance:
            result.update({"exit_required": True, "target_lots": 0, "reason": "atr_trailing_stop_long"})
            return result
        if current_lots < 0 and current_price >= entry_price + stop_distance:
            result.update({"exit_required": True, "target_lots": 0, "reason": "atr_trailing_stop_short"})
            return result

    days = _days_held(getattr(current_position, "entry_date", None), trading_date)
    if days is not None:
        template_state = str((lifecycle.get("template_state") or lifecycle.get("memory_state") or "")).lower()
        is_probe = template_state in {"probe_only", "recovering", "watchlist"} or "probe" in template_name
        max_days = int(policy.get("probe_time_stop_days" if is_probe else "trend_time_stop_days") or 0)
        if max_days > 0 and days >= max_days and abs(target_lots) >= abs(current_lots):
            result.update({"exit_required": True, "target_lots": 0, "reason": "time_stop"})
            return result
    return result
