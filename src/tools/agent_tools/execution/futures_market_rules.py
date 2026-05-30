from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional


BUY_LIKE_ACTIONS = {"open_long", "close_short"}
SELL_LIKE_ACTIONS = {"open_short", "close_long"}
OPEN_ACTIONS = {"open_long", "open_short"}
CLOSE_ACTIONS = {"close_long", "close_short"}


def enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def optional_float(value: Any) -> Optional[float]:
    if value in (None, "", "unknown", "UNKNOWN"):
        return None
    try:
        result = float(value)
    except Exception:
        return None
    return result


def quote_limit_payload(quote: Any) -> Dict[str, Any]:
    if quote is None:
        return {
            "status": "no_quote",
            "limit_up": None,
            "limit_down": None,
            "trade_date": None,
        }
    getter = quote.get if isinstance(quote, dict) else lambda key, default=None: getattr(quote, key, default)
    return {
        "status": "ok",
        "limit_up": optional_float(getter("limit_up")),
        "limit_down": optional_float(getter("limit_down")),
        "trade_date": getter("trade_date"),
        "ticker": getter("ticker", getter("contract_id")),
    }


def check_limit_lock(
    *,
    action: Any,
    execution_price: float,
    quote: Any,
    minimum_tick: float = 0.0,
    tolerance_ticks: int = 0,
    enabled: bool = True,
) -> Dict[str, Any]:
    action_value = str(enum_value(action) or "")
    payload = quote_limit_payload(quote)
    payload.update(
        {
            "enabled": bool(enabled),
            "action": action_value,
            "execution_price": optional_float(execution_price),
            "tolerance_ticks": int(tolerance_ticks or 0),
            "minimum_tick": optional_float(minimum_tick) or 0.0,
            "blocked": False,
            "reason": None,
        }
    )
    if not enabled or payload["status"] != "ok":
        return payload

    tolerance = max(0, int(tolerance_ticks or 0)) * float(minimum_tick or 0.0)
    limit_up = payload.get("limit_up")
    limit_down = payload.get("limit_down")
    price = optional_float(execution_price)
    if price is None:
        return payload

    if action_value in BUY_LIKE_ACTIONS and limit_up is not None and price >= (limit_up - tolerance):
        payload.update(
            {
                "blocked": True,
                "reason": "limit_locked_no_fill",
                "side": "buy_like",
                "limit_price": limit_up,
            }
        )
        return payload

    if action_value in SELL_LIKE_ACTIONS and limit_down is not None and price <= (limit_down + tolerance):
        payload.update(
            {
                "blocked": True,
                "reason": "limit_locked_no_fill",
                "side": "sell_like",
                "limit_price": limit_down,
            }
        )
    return payload


def parse_date(value: Any) -> Optional[date]:
    if value in (None, "", "unknown", "UNKNOWN"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text[:8], fmt).date()
        except Exception:
            continue
    return None


def parse_contract_delivery_month(contract_code: Any, reference_date: Any = None) -> Optional[date]:
    if not contract_code:
        return None
    text = str(contract_code).split(".", 1)[0].replace("_DOMINANT", "")
    match = re.match(r"^([A-Za-z]+)(\d{3,4})$", text)
    if not match:
        return None
    number = match.group(2)
    trade_day = parse_date(reference_date) or date.today()
    if len(number) == 4:
        year = 2000 + int(number[:2])
        month = int(number[2:])
    else:
        year_digit = int(number[0])
        month = int(number[1:])
        candidates = [year for year in range(trade_day.year - 5, trade_day.year + 6) if year % 10 == year_digit]
        if not candidates:
            return None
        year = min(candidates, key=lambda candidate: abs((candidate - trade_day.year) * 12 + (month - trade_day.month)))
    if month < 1 or month > 12:
        return None
    return date(year, month, 1)


def check_contract_expiry_guard(
    *,
    action: Any,
    contract_code: Any,
    trading_date: Any,
    source_type: Any = None,
    config: Optional[Dict[str, Any]] = None,
    contract_detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    config = config or {}
    rule_cfg = (config.get("execution") or {}).get("contract_expiry_guard") or {}
    action_value = str(enum_value(action) or "")
    source_value = str(enum_value(source_type) or "")
    trade_day = parse_date(trading_date)
    audit: Dict[str, Any] = {
        "enabled": bool(rule_cfg.get("enabled", False)),
        "action": action_value,
        "contract_code": contract_code,
        "trading_date": trade_day.isoformat() if trade_day else None,
        "source_type": source_value,
        "blocked": False,
        "reason": None,
    }
    if not audit["enabled"] or action_value not in OPEN_ACTIONS or trade_day is None:
        return audit
    if source_value == "rollover" and not bool(rule_cfg.get("apply_to_rollover", False)):
        audit["status"] = "rollover_exempt"
        return audit
    if action_value in CLOSE_ACTIONS:
        audit["status"] = "close_exempt"
        return audit

    detail = contract_detail or {}
    last_trade_date = (
        parse_date(detail.get("last_trading_date"))
        or parse_date(detail.get("last_trade_date"))
        or parse_date(detail.get("de_listed_date"))
        or parse_date(detail.get("maturity_date"))
    )
    near_last_trade_days = int(rule_cfg.get("near_expiry_days_before_last_trade", 5) or 0)
    if last_trade_date:
        days_to_last_trade = (last_trade_date - trade_day).days
        audit["last_trade_date"] = last_trade_date.isoformat()
        audit["days_to_last_trade"] = days_to_last_trade
        if days_to_last_trade < 0 or days_to_last_trade <= near_last_trade_days:
            audit.update(
                {
                    "blocked": True,
                    "reason": "near_expiry_new_entry_block",
                    "status": "near_last_trade_date",
                }
            )
            return audit

    start_delivery_date = parse_date(detail.get("start_delivery_date"))
    delivery_month = start_delivery_date or parse_contract_delivery_month(contract_code, trading_date)
    if delivery_month is None:
        audit["status"] = "delivery_month_unavailable"
        return audit

    audit["delivery_month"] = delivery_month.strftime("%Y-%m")
    block_delivery_month = bool(rule_cfg.get("block_new_entries_in_delivery_month", True))
    if block_delivery_month and trade_day.year == delivery_month.year and trade_day.month == delivery_month.month:
        audit.update(
            {
                "blocked": True,
                "reason": "near_expiry_new_entry_block",
                "status": "in_delivery_month",
            }
        )
        return audit

    near_month_days = int(rule_cfg.get("near_expiry_days_before_delivery_month", 5) or 0)
    days_to_delivery_month = (delivery_month - trade_day).days
    audit["days_to_delivery_month"] = days_to_delivery_month
    if 0 <= days_to_delivery_month <= near_month_days:
        audit.update(
            {
                "blocked": True,
                "reason": "near_expiry_new_entry_block",
                "status": "near_delivery_month",
            }
        )
    else:
        audit["status"] = "ok"
    return audit


def normalize_margin_rate(value: Any) -> Optional[float]:
    rate = optional_float(value)
    if rate is None or rate <= 0:
        return None
    if rate > 1.0:
        rate = rate / 100.0
    if rate <= 0 or rate > 1.0:
        return None
    return rate
