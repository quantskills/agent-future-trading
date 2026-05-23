from __future__ import annotations

"""Bounded reviewer-memory retrieval and config overlay helpers.

The reviewer writes full learning evidence into SQLite. Analysts and portfolio
controls consume only compact, relevant slices from these tables so prompts do
not grow with the backtest length.
"""

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from util.logger import logger


DEFAULT_HORIZON_BY_ANALYST = {
    "technical": "short",
    "fundamental": "medium",
    "commodity_news": "event_short",
    "company_news": "event_short",
}

SECTOR_BY_TICKER = {
    "BU": "energy",
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

ALLOWED_OVERLAY_KEYS = {
    "capital_utilization_control.target_margin_ratio_min",
    "capital_utilization_control.target_margin_ratio_max",
    "capital_utilization_control.target_margin_ratio_confirmed",
    "capital_utilization_control.strong_opportunity_target_margin_ratio_min",
    "capital_utilization_control.strong_opportunity_target_margin_ratio_max",
    "capital_utilization_control.min_confirmation_score_for_scaling",
    "capital_utilization_control.memory_protected_min_confirmation_score",
    "capital_utilization_control.allow_memory_protected_scaling",
    "capital_utilization_control.allow_recovering_template_scaling",
    "capital_utilization_control.min_recent_win_rate_for_scaling",
    "capital_utilization_control.min_recent_total_pnl_for_scaling",
    "capital_utilization_control.scale_only_when_recent_pnl_positive",
    "capital_utilization_control.allow_confirmed_same_side_add_on",
    "capital_utilization_control.same_side_add_on_match_tolerance",
    "market_confirmation.min_confirmation_score_for_new_entry",
    "market_confirmation.min_confirmation_score_for_weak_combo",
    "trade_auditor.cold_start.max_position_ratio_multiplier",
    "trade_auditor.quality_gate.qualified_support_min_confidence",
    "analyst_business_quality.min_score_for_probe",
    "analyst_business_quality.min_score_for_deployable",
    "analyst_business_quality.probe_multiplier",
}


def _date_text(value: Any) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value or "")


def resolve_config_id(db: Any, full_config: Dict[str, Any], explicit_config_id: Optional[str] = None) -> str:
    if explicit_config_id:
        return str(explicit_config_id)
    if not db or not full_config:
        return ""
    try:
        exp_name = full_config.get("exp_name")
        return db.get_config_id_by_name(exp_name) if exp_name else ""
    except Exception as exc:
        logger.warning(f"Unable to resolve config_id for learning context: {exc}")
        return ""


def _context_sector(context: Optional[Dict[str, Any]], ticker: str = "") -> str:
    if not isinstance(context, dict):
        return SECTOR_BY_TICKER.get(str(ticker or "").upper(), "*")
    return str(
        context.get("sector")
        or context.get("sector_group")
        or SECTOR_BY_TICKER.get(str(ticker or "").upper())
        or "*"
    )


def _context_market_regime(context: Optional[Dict[str, Any]]) -> str:
    if not isinstance(context, dict):
        return "*"
    regime = context.get("market_regime") or context.get("regime") or context.get("trend_stage")
    return str(regime or "*")


def _budgeted_lines(items: Iterable[Dict[str, Any]], *, max_chars: int) -> tuple[List[str], List[str], int]:
    lines: List[str] = []
    ids: List[str] = []
    used = 0
    dropped = 0
    for item in items:
        text = str(item.get("digest_text") or "").strip()
        if not text:
            continue
        prefix = (
            f"- [{item.get('ticker', '*')}/{item.get('horizon_class', '*')}/"
            f"{item.get('market_regime', '*')}; n={int(item.get('sample_count') or 0)}, "
            f"conf={float(item.get('confidence_score') or 0.0):.2f}] "
        )
        line = prefix + text.replace("\n", " ")
        if used + len(line) + 1 > max_chars:
            dropped += 1
            continue
        lines.append(line)
        ids.append(str(item.get("id")))
        used += len(line) + 1
    return lines, ids, dropped


def _learning_digest_lookup_attempts(
    *,
    ticker: str,
    sector: str,
    horizon_class: str,
    market_regime: str,
    allow_cross_ticker_sector_fallback: bool = True,
    allow_global_fallback: bool = False,
) -> List[Dict[str, str]]:
    """Return increasingly broad digest lookups.

    Reviewer learning is useful only if it gets back into prompts. Analyst
    horizons are sometimes refined over time, so exact horizon misses should
    gracefully fall back to same-ticker mature observations before returning an
    empty prompt block.
    """
    attempts = [
        {
            "scope": "exact",
            "ticker": ticker,
            "sector": sector,
            "horizon_class": horizon_class,
            "market_regime": market_regime,
        },
        {
            "scope": "any_horizon",
            "ticker": ticker,
            "sector": sector,
            "horizon_class": "*",
            "market_regime": market_regime,
        },
        {
            "scope": "any_regime",
            "ticker": ticker,
            "sector": sector,
            "horizon_class": horizon_class,
            "market_regime": "*",
        },
        {
            "scope": "same_ticker_broad",
            "ticker": ticker,
            "sector": sector,
            "horizon_class": "*",
            "market_regime": "*",
        },
    ]
    if allow_cross_ticker_sector_fallback and sector != "*":
        attempts.extend(
            [
                {
                    "scope": "same_sector_exact_horizon",
                    "ticker": "*",
                    "sector": sector,
                    "horizon_class": horizon_class,
                    "market_regime": market_regime,
                },
                {
                    "scope": "same_sector_any_horizon",
                    "ticker": "*",
                    "sector": sector,
                    "horizon_class": "*",
                    "market_regime": market_regime,
                },
                {
                    "scope": "same_sector_broad",
                    "ticker": "*",
                    "sector": sector,
                    "horizon_class": "*",
                    "market_regime": "*",
                },
            ]
        )
    if allow_global_fallback:
        attempts.append(
            {
                "scope": "global_broad",
                "ticker": "*",
                "sector": "*",
                "horizon_class": "*",
                "market_regime": "*",
            }
        )
    deduped: List[Dict[str, str]] = []
    seen = set()
    for attempt in attempts:
        key = (attempt["ticker"], attempt["sector"], attempt["horizon_class"], attempt["market_regime"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(attempt)
    return deduped


def build_learning_context(
    *,
    db: Any,
    full_config: Dict[str, Any],
    config_id: str,
    trading_date: Any,
    analyst: str,
    ticker: str,
    context: Optional[Dict[str, Any]] = None,
    horizon_class: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a compact prompt block selected from mature reviewer digests."""
    learning_cfg = (full_config or {}).get("learning", {}) or {}
    context_cfg = (full_config or {}).get("learning_context", {}) or {}
    if not bool(learning_cfg.get("enabled", False)) or not bool(context_cfg.get("enabled", True)):
        return {"enabled": False, "text": "", "items": [], "selected_ids": []}

    if not db or not config_id:
        return {"enabled": False, "text": "", "items": [], "selected_ids": []}

    analyst_key = "commodity_news" if str(analyst) == "company_news" else str(analyst)
    horizon = str(horizon_class or DEFAULT_HORIZON_BY_ANALYST.get(analyst_key, "*"))
    sector = _context_sector(context, ticker)
    market_regime = _context_market_regime(context)
    max_items = int(context_cfg.get("max_items_per_prompt", 5))
    max_chars = int(context_cfg.get("max_chars_per_prompt", 1200))
    allow_cross_ticker_sector_fallback = bool(context_cfg.get("allow_cross_ticker_sector_fallback", True))
    allow_global_fallback = bool(context_cfg.get("allow_global_fallback", False))
    allow_wildcard_sector_cross_ticker = bool(context_cfg.get("allow_wildcard_sector_cross_ticker", False))

    items: List[Dict[str, Any]] = []
    selected_scopes: List[str] = []
    seen_ids = set()
    for attempt in _learning_digest_lookup_attempts(
        ticker=str(ticker or "").upper(),
        sector=sector,
        horizon_class=horizon,
        market_regime=market_regime,
        allow_cross_ticker_sector_fallback=allow_cross_ticker_sector_fallback,
        allow_global_fallback=allow_global_fallback,
    ):
        try:
            rows = db.get_analyst_learning_digest(
                config_id=config_id,
                analyst=analyst_key,
                ticker=attempt["ticker"],
                sector=attempt["sector"],
                horizon_class=attempt["horizon_class"],
                market_regime=attempt["market_regime"],
                trading_date=trading_date,
                max_items=max_items * 2,
            )
        except TypeError:
            # Some unit fakes predate the broad lookup parameters.
            rows = db.get_analyst_learning_digest(
                config_id=config_id,
                analyst=analyst_key,
                ticker=attempt["ticker"],
                sector=sector,
                horizon_class=horizon,
                market_regime=market_regime,
                trading_date=trading_date,
                max_items=max_items * 2,
            )
        added = 0
        for row in rows or []:
            if (
                attempt["ticker"] == "*"
                and not allow_wildcard_sector_cross_ticker
                and str(row.get("sector") or "*") == "*"
                and str(row.get("ticker") or "").upper() != str(ticker or "").upper()
            ):
                continue
            digest_id = str(row.get("id") or "")
            key = digest_id or (
                str(row.get("ticker")),
                str(row.get("horizon_class")),
                str(row.get("market_regime")),
                str(row.get("digest_text")),
            )
            if key in seen_ids:
                continue
            seen_ids.add(key)
            items.append(row)
            added += 1
        if added:
            selected_scopes.append(attempt["scope"])
        if len(items) >= max_items * 2:
            break
    lines, selected_ids, dropped = _budgeted_lines(items, max_chars=max_chars)
    if len(lines) > max_items:
        dropped += len(lines) - max_items
        lines = lines[:max_items]
        selected_ids = selected_ids[:max_items]

    try:
        if hasattr(db, "save_learning_context_budget"):
            db.save_learning_context_budget(
                config_id=config_id,
                trading_date=trading_date,
                analyst=analyst_key,
                ticker=ticker,
                selected_digest_ids=selected_ids,
                selected_chars=sum(len(line) + 1 for line in lines),
                dropped_count=dropped,
                max_items=max_items,
                max_chars=max_chars,
            )
    except Exception as exc:
        logger.warning(f"{ticker}: learning context budget logging skipped: {exc}")

    if not lines:
        return {
            "enabled": True,
            "text": "",
            "items": [],
            "selected_ids": [],
            "horizon_class": horizon,
            "requested_horizon_class": horizon,
            "matched_horizon_classes": [],
            "retrieval_scopes": selected_scopes,
            "sector": sector,
            "market_regime": market_regime,
        }

    matched_horizons = sorted({str(item.get("horizon_class") or "*") for item in items if item.get("id") in selected_ids})
    text = (
        "\n\n=== Reviewer Learning Context (bounded, do not overfit) ===\n"
        "Use these mature observations only as priors. Keep today's data dominant.\n"
        + "\n".join(lines)
        + "\n"
    )
    return {
        "enabled": True,
        "text": text,
        "items": items,
        "selected_ids": selected_ids,
        "horizon_class": horizon,
        "requested_horizon_class": horizon,
        "matched_horizon_classes": matched_horizons,
        "retrieval_scopes": selected_scopes,
        "sector": sector,
        "market_regime": market_regime,
    }


def _set_dotted(config: Dict[str, Any], dotted_key: str, value: Any) -> None:
    current = config
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def apply_config_learning_overlay(
    full_config: Dict[str, Any],
    *,
    db: Any,
    config_id: str,
    trading_date: Any,
) -> Dict[str, Any]:
    """Apply reviewer-learned weak-parameter overlays through a strict allowlist."""
    config = deepcopy(full_config or {})
    learning_cfg = config.get("learning", {}) or {}
    overlay_cfg = (learning_cfg.get("config_overlay") or {})
    if not bool(learning_cfg.get("enabled", False)) or not bool(overlay_cfg.get("enabled", True)):
        return config
    if not db or not config_id:
        return config

    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    try:
        overlays = db.get_config_learning_overlay(config_id=config_id, trading_date=trading_date)
    except Exception as exc:
        logger.warning(f"Config learning overlay load failed: {exc}")
        return config

    for row in overlays:
        key = str(row.get("param_key") or "")
        if key not in ALLOWED_OVERLAY_KEYS:
            skipped.append({"param_key": key, "reason": "not_allowlisted"})
            continue
        value = row.get("learned_value")
        _set_dotted(config, key, value)
        applied.append(
            {
                "id": row.get("id"),
                "param_key": key,
                "value": value,
                "source": row.get("source"),
                "confidence_score": row.get("confidence_score"),
            }
        )

    if applied or skipped:
        runtime = config.setdefault("runtime_learning_overlay", {})
        runtime["applied_at"] = datetime.now(timezone.utc).isoformat()
        runtime["trading_date"] = _date_text(trading_date)
        runtime["applied"] = applied
        runtime["skipped"] = skipped
        logger.info(
            f"Applied {len(applied)} reviewer config overlay(s); skipped {len(skipped)}"
        )

    return config
