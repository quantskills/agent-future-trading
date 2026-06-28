from __future__ import annotations

"""Bounded research-memory retrieval and config overlay helpers.

The researcher exposes structured learning evidence from SQLite. Analysts and portfolio
controls consume only compact, relevant slices from these tables so prompts do
not grow with the backtest length.
"""

from copy import deepcopy
from collections import Counter
from datetime import datetime, timezone
import copy
import threading
from typing import Any, Dict, Iterable, List, Optional

from util.logger import logger
from tools.common.learning_contract import CONTRACT_KEY, contract_prompt_line
from tools.common.alpha_setup import (
    analyst_signal_calibration_prompt_line,
    compact_action_value_for_analyst_trace,
    compact_profile_for_trace,
    profile_prompt_line,
)


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
    "capital_utilization_control.strong_opportunity_target_margin_ratio_confirmed",
    "capital_utilization_control.other_opportunity_reserve_fraction_of_tradable_capital",
    "capital_utilization_control.unverified_probe_fraction_of_remaining_capacity",
    "capital_utilization_control.validated_min_fraction_of_remaining_capacity",
    "capital_utilization_control.validated_max_fraction_of_remaining_capacity",
    "capital_utilization_control.confirmation_allocation_power",
    "capital_utilization_control.stop_protection_allocation_bonus",
    "capital_utilization_control.exceptional_validated_enabled",
    "capital_utilization_control.exceptional_validated_requires_stop_protection",
    "capital_utilization_control.exceptional_validated_min_confirmation_score",
    "capital_utilization_control.exceptional_validated_min_sample_count",
    "capital_utilization_control.exceptional_validated_min_win_rate",
    "capital_utilization_control.exceptional_validated_min_net_pnl",
    "capital_utilization_control.exceptional_other_opportunity_reserve_fraction_of_tradable_capital",
    "capital_utilization_control.exceptional_validated_min_fraction_of_remaining_capacity",
    "capital_utilization_control.exceptional_validated_max_fraction_of_remaining_capacity",
    "capital_utilization_control.exceptional_confirmation_allocation_power",
    "capital_utilization_control.min_confirmation_score_for_scaling",
    "capital_utilization_control.memory_protected_min_confirmation_score",
    "capital_utilization_control.protected_min_sample_count_for_scaling",
    "capital_utilization_control.protected_min_win_rate_for_scaling",
    "capital_utilization_control.protected_min_net_pnl_for_scaling",
    "capital_utilization_control.block_scaling_on_conflicting_weak_memory",
    "capital_utilization_control.allow_memory_protected_scaling",
    "capital_utilization_control.allow_recovering_template_scaling",
    "capital_utilization_control.min_recent_win_rate_for_scaling",
    "capital_utilization_control.min_recent_total_pnl_for_scaling",
    "capital_utilization_control.scale_only_when_recent_pnl_positive",
    "capital_utilization_control.allow_confirmed_same_side_add_on",
    "capital_utilization_control.same_side_add_on_match_tolerance",
    "market_confirmation.min_confirmation_score_for_new_entry",
    "market_confirmation.min_confirmation_score_for_weak_combo",
    "pm_risk_gate.cold_start.max_position_ratio_multiplier",
    "pm_risk_gate.quality_gate.qualified_support_min_confidence",
    "analyst_business_quality.min_score_for_probe",
    "analyst_business_quality.min_score_for_deployable",
    "analyst_business_quality.probe_multiplier",
}


_LEARNING_CONTEXT_CACHE_LOCK = threading.Lock()
_LEARNING_CONTEXT_CACHE: Dict[tuple, Dict[str, Any]] = {}


def clear_learning_context_cache() -> None:
    """Clear process-local prompt-context cache between trading days or tests."""
    with _LEARNING_CONTEXT_CACHE_LOCK:
        _LEARNING_CONTEXT_CACHE.clear()


def _learning_context_cache_key(
    *,
    full_config: Dict[str, Any],
    config_id: str,
    trading_date: Any,
    analyst: str,
    ticker: str,
    context: Optional[Dict[str, Any]],
    horizon_class: Optional[str],
) -> Optional[tuple]:
    context_cfg = (full_config or {}).get("learning_context", {}) or {}
    cache_cfg = context_cfg.get("cache", {}) or {}
    if not bool(cache_cfg.get("enabled", True)):
        return None
    analyst_key = "commodity_news" if str(analyst) == "company_news" else str(analyst)
    horizon = str(horizon_class or DEFAULT_HORIZON_BY_ANALYST.get(analyst_key, "*"))
    sector = _context_sector(context, ticker)
    market_regime = _context_market_regime(context)
    trading_date_text = _date_text(trading_date)
    return (
        str(config_id or ""),
        trading_date_text,
        analyst_key,
        str(ticker or "").upper(),
        horizon,
        sector,
        market_regime,
        int(context_cfg.get("max_items_per_prompt", 5)),
        int(context_cfg.get("max_chars_per_prompt", 1200)),
        bool(context_cfg.get("allow_cross_ticker_sector_fallback", True)),
        bool(context_cfg.get("allow_global_fallback", False)),
        bool(context_cfg.get("allow_wildcard_sector_cross_ticker", False)),
        json_safe_cache_fragment(context_cfg.get("exploratory_memory", {}) or {}),
    )


def json_safe_cache_fragment(value: Any) -> str:
    try:
        import json

        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return str(value)


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
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        contract_text = contract_prompt_line(payload.get(CONTRACT_KEY), max_chars=180)
        suffix = f" {contract_text}" if contract_text else ""
        line = prefix + text.replace("\n", " ") + suffix
        if used + len(line) + 1 > max_chars:
            dropped += 1
            continue
        lines.append(line)
        ids.append(str(item.get("id")))
        used += len(line) + 1
    return lines, ids, dropped


def _budget_plain_lines(lines: Iterable[str], *, max_chars: int, max_items: int) -> tuple[List[str], int]:
    selected: List[str] = []
    used = 0
    dropped = 0
    for raw in lines:
        line = str(raw or "").strip().replace("\n", " ")
        if not line:
            continue
        if len(line) + 1 > max_chars and max_chars > 24:
            line = line[: max(0, max_chars - 4)].rstrip() + "..."
        if len(selected) >= max_items or used + len(line) + 1 > max_chars:
            dropped += 1
            continue
        selected.append(line)
        used += len(line) + 1
    return selected, dropped


def _memory_trace_ref(item: Dict[str, Any], memory_type: str) -> Dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    contract = payload.get(CONTRACT_KEY) if isinstance(payload.get(CONTRACT_KEY), dict) else {}
    return {
        "memory_type": memory_type,
        "id": str(item.get("id") or ""),
        "ticker": str(item.get("ticker") or "*").upper(),
        "side": str(item.get("side") or "*").lower(),
        "sector": str(item.get("sector") or "*"),
        "horizon_class": str(item.get("horizon_class") or "*"),
        "market_regime": str(item.get("market_regime") or "*"),
        "setup_type": str(item.get("setup_type") or "*"),
        "status": str(item.get("status") or contract.get("maturity_state") or ""),
        "sample_count": int(item.get("sample_count") or contract.get("sample_count") or 0),
        "confidence_score": float(item.get("confidence_score") or 0.0),
    }


def _scope_authority_boundary(selected_scopes: Iterable[str]) -> Dict[str, Any]:
    scopes = [str(scope or "") for scope in selected_scopes]
    cross_scope = any(
        scope.startswith("same_sector") or scope.startswith("global") or scope.startswith("wildcard")
        for scope in scopes
    )
    return {
        "same_ticker_scopes": [scope for scope in scopes if not (
            scope.startswith("same_sector") or scope.startswith("global") or scope.startswith("wildcard")
        )],
        "cross_ticker_prior_scopes": [
            scope for scope in scopes
            if scope.startswith("same_sector") or scope.startswith("global") or scope.startswith("wildcard")
        ],
        "contains_cross_ticker_fallback": bool(cross_scope),
        "same_sector_fallback_prior_only": bool(any(scope.startswith("same_sector") for scope in scopes)),
        "global_fallback_prior_only": bool(any(scope.startswith("global") for scope in scopes)),
        "can_create_trade_authority": False,
        "can_override_same_scope_action_value": False,
        "can_size_or_add_position": False,
        "boundary": (
            "broad learning fallback is prompt context only; PM trade authority must come from "
            "same-scope action evidence or today's trigger/invalidation/confirmation"
        ),
    }


def _build_memory_trace(
    *,
    analyst: str,
    ticker: str,
    horizon: str,
    sector: str,
    market_regime: str,
    selected_scopes: List[str],
    selected_ids: List[str],
    items: List[Dict[str, Any]],
    episode_items: List[Dict[str, Any]],
    no_trade_items: List[Dict[str, Any]],
    hypothesis_items: List[Dict[str, Any]],
    lines: List[str],
    episode_lines: List[str],
    no_trade_lines: List[str],
    hypothesis_lines: List[str],
    dropped: int,
    max_items: int,
    max_chars: int,
    alpha_setup_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    selected_digest_items = [item for item in items if str(item.get("id") or "") in set(selected_ids)]
    hypothesis_status_counts = Counter(str(item.get("status") or "candidate") for item in hypothesis_items)
    refs: List[Dict[str, Any]] = []
    for memory_type, collection, limit in (
        ("analyst_learning_digest", selected_digest_items, max_items),
        ("trade_episode_memory", episode_items, 4),
        ("no_trade_opportunity_memory", no_trade_items, 4),
        ("exploratory_hypothesis", hypothesis_items, 4),
        ("alpha_setup_profile", alpha_setup_items or [], 4),
    ):
        for item in collection[:limit]:
            refs.append(_memory_trace_ref(item, memory_type))
    return {
        "analyst": str(analyst),
        "ticker": str(ticker or "").upper(),
        "horizon_class": horizon,
        "sector": sector,
        "market_regime": market_regime,
        "retrieval_scopes": list(selected_scopes),
        "fallback_authority_boundary": _scope_authority_boundary(selected_scopes),
        "selected_digest_ids": list(selected_ids),
        "selected_memory_refs": refs,
        "selected_counts": {
            "digest": len(lines),
            "trade_episode": len(episode_lines),
            "no_trade_opportunity": len(no_trade_lines),
            "exploratory_hypothesis": len(hypothesis_lines),
            "alpha_setup_profile": len(alpha_setup_items or []),
            "alpha_setup_action_value": 0,
        },
        "hypothesis_status_counts": dict(hypothesis_status_counts),
        "candidate_hypothesis_count": int(hypothesis_status_counts.get("candidate", 0)),
        "validated_hypothesis_count": int(hypothesis_status_counts.get("validated", 0)),
        "dropped_count": int(dropped),
        "max_items": int(max_items),
        "max_chars": int(max_chars),
        "current_day_evidence_required": True,
        "candidate_boundary": (
            "candidate_hypotheses_are_prompt_priors_only_no_sizing_add_position_matched_"
            "losing_hold_or_auditor_bypass_without_current_evidence_and_validation"
        ),
    }


def _compact_inline_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip().replace("\n", " ")
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


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

    Research learning is useful only if it gets back into prompts. Analyst
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
    """Return a compact prompt block selected from mature research digests."""
    learning_cfg = (full_config or {}).get("learning", {}) or {}
    context_cfg = (full_config or {}).get("learning_context", {}) or {}
    if not bool(learning_cfg.get("enabled", False)) or not bool(context_cfg.get("enabled", True)):
        return {"enabled": False, "text": "", "items": [], "selected_ids": []}

    if not db or not config_id:
        return {"enabled": False, "text": "", "items": [], "selected_ids": []}

    cache_key = _learning_context_cache_key(
        full_config=full_config,
        config_id=config_id,
        trading_date=trading_date,
        analyst=analyst,
        ticker=ticker,
        context=context,
        horizon_class=horizon_class,
    )
    if cache_key is not None:
        with _LEARNING_CONTEXT_CACHE_LOCK:
            cached = _LEARNING_CONTEXT_CACHE.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)

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

    exploration_cfg = context_cfg.get("exploratory_memory", {}) or {}
    exploration_enabled = bool(exploration_cfg.get("enabled", True))
    episode_lines: List[str] = []
    no_trade_lines: List[str] = []
    hypothesis_lines: List[str] = []
    episode_items: List[Dict[str, Any]] = []
    no_trade_items: List[Dict[str, Any]] = []
    hypothesis_items: List[Dict[str, Any]] = []
    alpha_setup_items: List[Dict[str, Any]] = []
    alpha_setup_lines: List[str] = []
    if exploration_enabled:
        episode_limit = int(exploration_cfg.get("max_episode_items", 3))
        no_trade_limit = int(exploration_cfg.get("max_no_trade_items", 3))
        hypothesis_limit = int(exploration_cfg.get("max_hypothesis_items", 3))
        remaining_chars = max(0, max_chars - sum(len(line) + 1 for line in lines))
        episode_chars = int(exploration_cfg.get("max_episode_chars", max(350, remaining_chars // 2)))
        no_trade_chars = int(exploration_cfg.get("max_no_trade_chars", max(300, remaining_chars // 3)))
        hypothesis_chars = int(exploration_cfg.get("max_hypothesis_chars", max(350, remaining_chars - episode_chars)))
        try:
            if hasattr(db, "get_trade_episode_memory"):
                episode_items = db.get_trade_episode_memory(
                    config_id=config_id,
                    ticker=str(ticker or "").upper(),
                    sector=sector,
                    horizon_class=horizon,
                    market_regime=market_regime,
                    trading_date=trading_date,
                    limit=max(episode_limit * 2, episode_limit),
                )
                if (
                    not episode_items
                    and allow_cross_ticker_sector_fallback
                    and sector
                    and sector != "*"
                ):
                    episode_items = db.get_trade_episode_memory(
                        config_id=config_id,
                        ticker="*",
                        sector=sector,
                        horizon_class=horizon,
                        market_regime=market_regime,
                        trading_date=trading_date,
                        limit=max(episode_limit * 2, episode_limit),
                    )
        except Exception as exc:
            logger.warning(f"{ticker}: trade episode memory retrieval skipped: {exc}")
            episode_items = []
        raw_episode_lines = []
        for item in episode_items:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            contract_text = contract_prompt_line(payload.get(CONTRACT_KEY), max_chars=260)
            contract_suffix = f" {contract_text}" if contract_text else ""
            raw_episode_lines.append(
                "- "
                f"{item.get('ticker')}/{item.get('side')}/{item.get('horizon_class')}/"
                f"{item.get('market_regime')}: pnl={float(item.get('net_pnl') or 0.0):.0f}, "
                f"hold={int(item.get('holding_days') or 0)}d, "
                f"template={item.get('setup_type')}; "
                f"{item.get('lesson_text') or ''}{contract_suffix}"
            )
        episode_lines, episode_dropped = _budget_plain_lines(
            raw_episode_lines,
            max_chars=episode_chars,
            max_items=episode_limit,
        )
        dropped += episode_dropped

        try:
            if hasattr(db, "get_no_trade_opportunity_memory"):
                no_trade_items = db.get_no_trade_opportunity_memory(
                    config_id=config_id,
                    ticker=str(ticker or "").upper(),
                    sector=sector,
                    horizon_class=horizon,
                    market_regime=market_regime,
                    trading_date=trading_date,
                    limit=max(no_trade_limit * 2, no_trade_limit),
                )
                if (
                    not no_trade_items
                    and allow_cross_ticker_sector_fallback
                    and sector
                    and sector != "*"
                ):
                    no_trade_items = db.get_no_trade_opportunity_memory(
                        config_id=config_id,
                        ticker="*",
                        sector=sector,
                        horizon_class=horizon,
                        market_regime=market_regime,
                        trading_date=trading_date,
                        limit=max(no_trade_limit * 2, no_trade_limit),
                    )
        except Exception as exc:
            logger.warning(f"{ticker}: no-trade opportunity memory retrieval skipped: {exc}")
            no_trade_items = []
        raw_no_trade_lines = []
        for item in no_trade_items:
            counterfactual_results = item.get("counterfactual_results") if isinstance(item.get("counterfactual_results"), list) else []
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            contract_text = contract_prompt_line(payload.get(CONTRACT_KEY), max_chars=150)
            contract_text = contract_text.replace("Next-round strategy update: ", "strategy_update: ") if contract_text else ""
            contract_suffix = f"; {contract_text}" if contract_text else ""
            neutral_observations = payload.get("neutral_opportunity_observations") if isinstance(payload.get("neutral_opportunity_observations"), list) else []
            neutral_text = ""
            if neutral_observations:
                first_neutral = neutral_observations[0] if isinstance(neutral_observations[0], dict) else {}
                neutral_text = (
                    f"; neutral={first_neutral.get('analyst')}:{first_neutral.get('bucket')}"
                    f"/{first_neutral.get('watchlist_priority')}"
                    f" trigger={_compact_inline_text(first_neutral.get('trigger_condition') or '', 60)}"
                )
            counterfactual_text = "; ".join(
                f"{int(result.get('horizon_days') or 0)}d={float(result.get('counterfactual_pnl') or 0.0):.0f}"
                for result in counterfactual_results[:3]
                if isinstance(result, dict)
            )
            raw_no_trade_lines.append(
                "- "
                f"{item.get('ticker')}/{item.get('side')}/{item.get('horizon_class')}: "
                f"class={item.get('classification')}; {contract_suffix} "
                f"{neutral_text}; counterfactual=[{counterfactual_text or 'pending'}]; "
                f"reason={_compact_inline_text(item.get('pm_reason') or item.get('execution_reason') or '', 50)}"
            )
        no_trade_lines, no_trade_dropped = _budget_plain_lines(
            raw_no_trade_lines,
            max_chars=no_trade_chars,
            max_items=no_trade_limit,
        )
        dropped += no_trade_dropped

        try:
            if hasattr(db, "get_exploratory_hypotheses"):
                hypothesis_items = db.get_exploratory_hypotheses(
                    config_id=config_id,
                    ticker=str(ticker or "").upper(),
                    sector=sector,
                    horizon_class=horizon,
                    market_regime=market_regime,
                    trading_date=trading_date,
                    limit=max(hypothesis_limit * 2, hypothesis_limit),
                )
                if (
                    not hypothesis_items
                    and allow_cross_ticker_sector_fallback
                    and sector
                    and sector != "*"
                ):
                    hypothesis_items = db.get_exploratory_hypotheses(
                        config_id=config_id,
                        ticker="*",
                        sector=sector,
                        horizon_class=horizon,
                        market_regime=market_regime,
                        trading_date=trading_date,
                        limit=max(hypothesis_limit * 2, hypothesis_limit),
                    )
        except Exception as exc:
            logger.warning(f"{ticker}: exploratory hypothesis retrieval skipped: {exc}")
            hypothesis_items = []
        raw_hypothesis_lines = []
        for item in hypothesis_items:
            suggested_use = _compact_inline_text(
                item.get("suggested_use") or "structured research hypothesis only",
                80,
            )
            if suggested_use and "structured research hypothesis" not in suggested_use.lower():
                suggested_use = f"{suggested_use}; structured research hypothesis only until validated"
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            structured_hints = []
            max_hint_items = int(exploration_cfg.get("max_hypothesis_hint_items", 3) or 3)
            for label, raw_value in (
                ("entry", item.get("entry_timing_hint") or payload.get("entry_timing_hint")),
                ("exit", item.get("exit_timing_hint") or payload.get("exit_timing_hint")),
                ("invalidation", item.get("invalidation_condition") or payload.get("invalidation_condition")),
                ("hold", item.get("holding_period_hint") or payload.get("holding_period_hint")),
                ("family", payload.get("failure_family")),
                ("data", payload.get("data_combo")),
                ("validate", item.get("validation_plan") or payload.get("validation_plan")),
            ):
                if raw_value:
                    hint_limit = 72 if label in {"family", "data"} else 44
                    structured_hints.append(f"{label}={_compact_inline_text(raw_value, hint_limit)}")
                if len(structured_hints) >= max_hint_items:
                    break
            hint_text = (" Hints: " + "; ".join(structured_hints)) if structured_hints else ""
            hypothesis_text = _compact_inline_text(item.get("hypothesis_text") or "", 140)
            contract_text = contract_prompt_line(payload.get(CONTRACT_KEY), max_chars=260)
            contract_suffix = f" {contract_text}" if contract_text else ""
            raw_hypothesis_lines.append(
                "- "
                f"[{item.get('status')}; conf={float(item.get('confidence_score') or 0.0):.2f}; "
                f"n={int(item.get('sample_count') or 0)}] "
                f"{hypothesis_text}{hint_text} "
                f"Use: {suggested_use}. Boundary: structured hypothesis only; no sizing/add/position_matched/"
                "losing-hold/bypass without current evidence and validation. "
                "Before using it, state whether today's data confirms or contradicts it; "
                f"if evidence is missing or conflicting, lower confidence or stay Neutral.{contract_suffix}"
            )
        hypothesis_lines, hypothesis_dropped = _budget_plain_lines(
            raw_hypothesis_lines,
            max_chars=hypothesis_chars,
            max_items=hypothesis_limit,
        )
        dropped += hypothesis_dropped

        alpha_cfg = exploration_cfg.get("alpha_setup_profile", {}) or {}
        if bool(alpha_cfg.get("enabled", True)):
            alpha_limit = int(alpha_cfg.get("max_items", 3) or 3)
            alpha_chars = int(alpha_cfg.get("max_chars", max(300, remaining_chars // 3)) or 300)
            try:
                if hasattr(db, "get_alpha_setup_profiles"):
                    alpha_setup_items = db.get_alpha_setup_profiles(
                        config_id=config_id,
                        ticker=str(ticker or "").upper(),
                        sector=sector,
                        horizon_class=horizon,
                        market_regime=market_regime,
                        trading_date=trading_date,
                        limit=max(alpha_limit * 2, alpha_limit),
                    )
                    if (
                        not alpha_setup_items
                        and allow_cross_ticker_sector_fallback
                        and sector
                        and sector != "*"
                    ):
                        alpha_setup_items = db.get_alpha_setup_profiles(
                            config_id=config_id,
                            ticker="*",
                            sector=sector,
                            horizon_class=horizon,
                            market_regime=market_regime,
                            trading_date=trading_date,
                            limit=max(alpha_limit * 2, alpha_limit),
                        )
            except Exception as exc:
                logger.warning(f"{ticker}: alpha setup profile retrieval skipped: {exc}")
                alpha_setup_items = []
            raw_alpha_lines = [f"- {profile_prompt_line(item)}" for item in alpha_setup_items]
            alpha_setup_lines, alpha_setup_dropped = _budget_plain_lines(
                raw_alpha_lines,
                max_chars=alpha_chars,
                max_items=alpha_limit,
            )
            dropped += alpha_setup_dropped

    try:
        if hasattr(db, "save_learning_context_budget"):
            digest_chars = sum(len(line) + 1 for line in lines)
            episode_chars_used = sum(len(line) + 1 for line in episode_lines)
            hypothesis_chars_used = sum(len(line) + 1 for line in hypothesis_lines)
            alpha_setup_chars_used = sum(len(line) + 1 for line in alpha_setup_lines)
            db.save_learning_context_budget(
                config_id=config_id,
                trading_date=trading_date,
                analyst=analyst_key,
                ticker=ticker,
                selected_digest_ids=selected_ids,
                selected_chars=digest_chars,
                digest_count=len(selected_ids),
                trade_episode_count=len(episode_lines),
                hypothesis_count=len(hypothesis_lines),
                total_context_chars=(
                    digest_chars
                    + episode_chars_used
                    + sum(len(line) + 1 for line in no_trade_lines)
                    + hypothesis_chars_used
                    + alpha_setup_chars_used
                ),
                dropped_count=dropped,
                max_items=max_items,
                max_chars=max_chars,
            )
    except Exception as exc:
        logger.warning(f"{ticker}: learning context budget logging skipped: {exc}")

    if (
        not lines
        and not episode_lines
        and not no_trade_lines
        and not hypothesis_lines
        and not alpha_setup_lines
    ):
        memory_trace = _build_memory_trace(
            analyst=analyst_key,
            ticker=ticker,
            horizon=horizon,
            sector=sector,
            market_regime=market_regime,
            selected_scopes=selected_scopes,
            selected_ids=[],
            items=[],
            episode_items=[],
            no_trade_items=[],
            hypothesis_items=[],
            alpha_setup_items=[],
            lines=[],
            episode_lines=[],
            no_trade_lines=[],
            hypothesis_lines=[],
            dropped=dropped,
            max_items=max_items,
            max_chars=max_chars,
        )
        result = {
            "enabled": True,
            "text": "",
            "items": [],
            "trade_episode_items": [],
            "no_trade_opportunity_items": [],
            "hypothesis_items": [],
            "alpha_setup_items": [],
            "analyst_calibration_items": [],
            "selected_ids": [],
            "horizon_class": horizon,
            "requested_horizon_class": horizon,
            "matched_horizon_classes": [],
            "retrieval_scopes": selected_scopes,
            "fallback_authority_boundary": memory_trace.get("fallback_authority_boundary"),
            "sector": sector,
            "market_regime": market_regime,
            "memory_trace": memory_trace,
        }
        if cache_key is not None:
            with _LEARNING_CONTEXT_CACHE_LOCK:
                _LEARNING_CONTEXT_CACHE[cache_key] = copy.deepcopy(result)
        return result

    matched_horizons = sorted({str(item.get("horizon_class") or "*") for item in items if item.get("id") in selected_ids})
    text_parts = [
        "\n\n=== Research Learning Context (bounded, exploratory, do not overfit) ===",
        "Use these as rebuttable priors and comparable cases. Keep today's data dominant; these notes are not trading authority.",
        "For any prior you cite, explicitly compare it with today's evidence and name the contradiction that would invalidate it.",
    ]
    scope_boundary = _scope_authority_boundary(selected_scopes)
    if scope_boundary["contains_cross_ticker_fallback"]:
        text_parts.append(
            "Scope boundary: same-sector/global fallback rows are broad priors only. "
            "Do not cite them as same-ticker action-value, trade authority, sizing authority, "
            "or permission to bypass today's trigger, invalidation, PM, Auditor, or Trader checks."
        )
    if lines:
        text_parts.append("Mature research digests:")
        text_parts.extend(lines)
    if episode_lines:
        text_parts.append("Similar completed trade episodes:")
        text_parts.extend(episode_lines)
    if no_trade_lines:
        text_parts.append("No-trade opportunity memories with forward counterfactual results:")
        text_parts.append(
            "These show opportunities the system skipped. Use them to question timing and evidence, "
            "not to force a trade without today's confirmation."
        )
        text_parts.extend(no_trade_lines)
    if hypothesis_lines:
        text_parts.append("Exploratory hypotheses under validation:")
        text_parts.append(
            "Candidate hypotheses cannot size, add, justify position_matched, continue losing positions, "
            "or bypass auditor without current evidence and future validation. Treat them as questions to test, not answers."
        )
        text_parts.extend(hypothesis_lines)
    if alpha_setup_lines:
        text_parts.append("Alpha setup profiles:")
        text_parts.append(
            "These profiles summarize same-scope setup outcomes. Deployable/protected profiles may support "
            "controlled opportunity recognition only with today's trigger, invalidation, PM, Auditor, Trader, "
            "and the 20% margin hard cap. Candidate/watchlist/capped/rejected profiles are rebuttable boundaries, "
            "not product bans."
        )
        text_parts.extend(alpha_setup_lines)
    text = "\n".join(text_parts) + "\n"
    hypothesis_status_counts = Counter(str(item.get("status") or "candidate") for item in hypothesis_items)
    memory_trace = _build_memory_trace(
        analyst=analyst_key,
        ticker=ticker,
        horizon=horizon,
        sector=sector,
        market_regime=market_regime,
        selected_scopes=selected_scopes,
        selected_ids=selected_ids,
        items=items,
        episode_items=episode_items,
        no_trade_items=no_trade_items,
        hypothesis_items=hypothesis_items,
        alpha_setup_items=alpha_setup_items,
        lines=lines,
        episode_lines=episode_lines,
        no_trade_lines=no_trade_lines,
        hypothesis_lines=hypothesis_lines,
        dropped=dropped,
        max_items=max_items,
        max_chars=max_chars,
    )
    result = {
        "enabled": True,
        "text": text,
        "items": items,
        "trade_episode_items": episode_items,
        "no_trade_opportunity_items": no_trade_items,
        "hypothesis_items": hypothesis_items,
        "alpha_setup_items": [compact_profile_for_trace(item) for item in alpha_setup_items],
        "analyst_calibration_items": [],
        "selected_ids": selected_ids,
        "horizon_class": horizon,
        "requested_horizon_class": horizon,
        "matched_horizon_classes": matched_horizons,
        "retrieval_scopes": selected_scopes,
        "fallback_authority_boundary": scope_boundary,
        "sector": sector,
        "market_regime": market_regime,
        "hypothesis_status_counts": dict(hypothesis_status_counts),
        "candidate_hypothesis_count": int(hypothesis_status_counts.get("candidate", 0)),
        "validated_hypothesis_count": int(hypothesis_status_counts.get("validated", 0)),
        "memory_trace": memory_trace,
    }
    if cache_key is not None:
        with _LEARNING_CONTEXT_CACHE_LOCK:
            _LEARNING_CONTEXT_CACHE[cache_key] = copy.deepcopy(result)
    return result


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
    """Apply research-learned weak-parameter overlays through a strict allowlist."""
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
            f"Applied {len(applied)} research config overlay(s); skipped {len(skipped)}"
        )

    return config
