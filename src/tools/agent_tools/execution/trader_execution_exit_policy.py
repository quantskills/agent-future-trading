from __future__ import annotations

"""Deterministic execution exit policy helpers."""

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


def resolve_exit_policy_config(config: Dict[str, Any], ticker: str, setup_type: str = "") -> Dict[str, Any]:
    execution_cfg = ((config or {}).get("execution") or {}).get("exit_policy") or {}
    sector = SECTOR_BY_TICKER.get(str(ticker).upper(), "generic")
    resolved = dict(DEFAULT_SECTOR_STOPS.get(sector, DEFAULT_SECTOR_STOPS["generic"]))
    resolved.update(execution_cfg.get("defaults") or {})
    resolved.update((execution_cfg.get("sector_overrides") or {}).get(sector) or {})
    if setup_type:
        resolved.update((execution_cfg.get("template_overrides") or {}).get(setup_type) or {})
    resolved["sector"] = sector
    resolved["enabled"] = bool(execution_cfg.get("enabled", True))
    return resolved


def resolve_atr_protection(
    *,
    current_lots: int,
    current_price: float,
    entry_price: float,
    atr_distance: float,
    atr_multiplier: float,
    best_prior_settlement_price: float = 0.0,
) -> Dict[str, Any]:
    """Resolve the initial ATR stop and the activated profit trailing stop.

    The favorable settlement is restricted by the caller to completed sessions
    before the decision date.  The initial stop keeps the configured sector or
    setup multiplier; the profit trail activates after a one-ATR favorable move
    and then stays one raw ATR behind the best completed settlement.
    """
    lots = int(current_lots or 0)
    price = _safe_float(current_price, 0.0)
    entry = _safe_float(entry_price, 0.0)
    atr = _safe_float(atr_distance, 0.0)
    multiplier = _safe_float(atr_multiplier, 0.0)
    best_settlement = _safe_float(best_prior_settlement_price, 0.0)
    result: Dict[str, Any] = {
        "enabled": bool(lots and price > 0.0 and entry > 0.0 and atr > 0.0 and multiplier > 0.0),
        "activated": False,
        "initial_stop_level": None,
        "trailing_stop_level": None,
        "effective_stop_level": None,
        "best_prior_settlement_price": best_settlement if best_settlement > 0.0 else None,
        "breached": False,
        "mode": "none",
    }
    if not result["enabled"]:
        return result

    initial_distance = atr * multiplier
    if lots > 0:
        initial_stop = entry - initial_distance
        activated = best_settlement >= entry + atr
        trailing_stop = best_settlement - atr if activated else None
        effective_stop = max(initial_stop, trailing_stop) if trailing_stop is not None else initial_stop
        breached = price <= effective_stop
    else:
        initial_stop = entry + initial_distance
        activated = 0.0 < best_settlement <= entry - atr
        trailing_stop = best_settlement + atr if activated else None
        effective_stop = min(initial_stop, trailing_stop) if trailing_stop is not None else initial_stop
        breached = price >= effective_stop

    result.update({
        "activated": bool(activated),
        "initial_stop_level": float(initial_stop),
        "trailing_stop_level": float(trailing_stop) if trailing_stop is not None else None,
        "effective_stop_level": float(effective_stop),
        "breached": bool(breached),
        "mode": "profit_trailing" if activated else "initial",
    })
    return result


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
    setup_type = str(lifecycle.get("setup_type") or "")
    policy = resolve_exit_policy_config(config, ticker, setup_type)
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
        atr_protection = resolve_atr_protection(
            current_lots=current_lots,
            current_price=current_price,
            entry_price=entry_price,
            atr_distance=atr_distance,
            atr_multiplier=_safe_float(policy.get("atr_multiplier"), 1.8),
            best_prior_settlement_price=_safe_float(
                lifecycle.get("best_prior_settlement_price"),
                0.0,
            ),
        )
        result["atr_protection"] = atr_protection
        if current_lots > 0 and atr_protection.get("breached"):
            result.update({"exit_required": True, "target_lots": 0, "reason": "atr_trailing_stop_long"})
            return result
        if current_lots < 0 and atr_protection.get("breached"):
            result.update({"exit_required": True, "target_lots": 0, "reason": "atr_trailing_stop_short"})
            return result

    days = _days_held(getattr(current_position, "entry_date", None), trading_date)
    if days is not None:
        template_state = str((lifecycle.get("template_state") or lifecycle.get("memory_state") or "")).lower()
        opening_authority_type = str(
            lifecycle.get("opening_authority_type") or ""
        ).strip().lower()
        is_probe = (
            opening_authority_type == "exploration_probe"
            if opening_authority_type
            else template_state in {"probe_only", "recovering", "watchlist"}
            or "probe" in setup_type
        )
        max_days = int(policy.get("probe_time_stop_days" if is_probe else "trend_time_stop_days") or 0)
        same_direction_supported = (
            target_lots != 0
            and current_lots != 0
            and ((target_lots > 0) == (current_lots > 0))
            and abs(target_lots) >= abs(current_lots)
        )
        result["same_direction_supported"] = same_direction_supported
        result["days_held"] = days
        result["is_probe"] = is_probe
        if max_days > 0 and days >= max_days and not same_direction_supported:
            result.update({"exit_required": True, "target_lots": 0, "reason": "time_stop"})
            return result
    return result

