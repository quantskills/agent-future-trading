"""Canonical execution-profile and intraday-trigger semantics.

The profile enum, source, and machine-visible trigger text are one contract.
Analyst prose is evidence context and must not become Trader executable logic.
"""

from __future__ import annotations

import math
from typing import Any


CANONICAL_EXECUTION_PROFILES = frozenset(
    {
        "breakout",
        "pullback",
        "vwap_confirmed",
        "event_immediate",
        "exit_immediate",
        "hold",
    }
)
TECHNICAL_ENTRY_PROFILES = frozenset({"breakout", "pullback", "vwap_confirmed"})
NEWS_ENTRY_PROFILES = frozenset({"event_immediate"})
NEW_RISK_ENTRY_PROFILES = TECHNICAL_ENTRY_PROFILES | NEWS_ENTRY_PROFILES


_ENTRY_TRIGGER_BY_PROFILE_AND_SIDE = {
    ("breakout", "long"): "15分钟收盘价向上突破开盘区间上沿且高于VWAP",
    ("breakout", "short"): "15分钟收盘价向下突破开盘区间下沿且低于VWAP",
    ("pullback", "long"): "15分钟先向上扩张，随后回踩开盘区间上沿或VWAP，最终收盘重新严格站上被回踩边界",
    ("pullback", "short"): "15分钟先向下扩张，随后回抽开盘区间下沿或VWAP，最终收盘重新严格跌回被回抽边界下方",
    ("vwap_confirmed", "long"): "15分钟收盘价不低于VWAP",
    ("vwap_confirmed", "short"): "15分钟收盘价不高于VWAP",
    ("event_immediate", "long"): "当前事件已满足即时执行边界，使用首根合法1分钟线执行",
    ("event_immediate", "short"): "当前事件已满足即时执行边界，使用首根合法1分钟线执行",
}
CANONICAL_ENTRY_TRIGGERS = frozenset(_ENTRY_TRIGGER_BY_PROFILE_AND_SIDE.values())


_ENTRY_INVALIDATION_CONDITION_BY_SIDE = {
    "long": "long_price_lte_invalidation_level",
    "short": "short_price_gte_invalidation_level",
}
CANONICAL_ENTRY_INVALIDATION_CONDITIONS = frozenset(
    _ENTRY_INVALIDATION_CONDITION_BY_SIDE.values()
)


_TRIGGER_SOURCE_BY_ANALYST_AND_PROFILE = {
    ("technical", "breakout"): "technical_breakout",
    ("technical", "pullback"): "technical_pullback",
    ("technical", "vwap_confirmed"): "technical_pullback",
    ("commodity_news", "event_immediate"): "commodity_news_event",
}


_TRIGGER_SOURCES_BY_PROFILE = {
    "breakout": frozenset({"technical_breakout"}),
    "pullback": frozenset({"technical_pullback"}),
    "vwap_confirmed": frozenset({"technical_pullback"}),
    "event_immediate": frozenset({"commodity_news_event"}),
    "exit_immediate": frozenset({"position_lifecycle"}),
    "hold": frozenset({"none"}),
}


_EXECUTION_LEARNING_SETUP_TO_PROFILE = {
    "execution_breakout_setup": "breakout",
    "execution_pullback_setup": "pullback",
    "execution_vwap_confirmed_setup": "vwap_confirmed",
}


def normalize_execution_profile(value: Any) -> str:
    profile = str(value or "").strip().lower()
    return profile if profile in CANONICAL_EXECUTION_PROFILES else ""


def execution_profile_from_learning_setup(value: Any) -> str:
    """Read only the registered execution-learning setup, never arbitrary text."""
    return _EXECUTION_LEARNING_SETUP_TO_PROFILE.get(
        str(value or "").strip().lower(),
        "",
    )


def canonical_entry_trigger(profile: Any, side: Any) -> str:
    return _ENTRY_TRIGGER_BY_PROFILE_AND_SIDE.get(
        (normalize_execution_profile(profile), str(side or "").strip().lower()),
        "",
    )


def is_canonical_entry_trigger(value: Any) -> bool:
    return str(value or "").strip() in CANONICAL_ENTRY_TRIGGERS


def canonical_entry_invalidation_condition(profile: Any, side: Any) -> str:
    """Return the registered pre-fill cancellation condition for one entry."""
    normalized = normalize_execution_profile(profile)
    side_text = str(side or "").strip().lower()
    if normalized not in NEW_RISK_ENTRY_PROFILES:
        return ""
    return _ENTRY_INVALIDATION_CONDITION_BY_SIDE.get(side_text, "")


def is_canonical_entry_invalidation_condition(
    value: Any,
    *,
    profile: Any,
    side: Any,
) -> bool:
    expected = canonical_entry_invalidation_condition(profile, side)
    return bool(expected and str(value or "").strip() == expected)


def entry_invalidation_contract_error(
    *,
    profile: Any,
    side: Any,
    invalidation_condition: Any,
    invalidation_level: Any,
) -> str:
    """Validate one machine-executable pre-fill cancellation boundary."""
    normalized = normalize_execution_profile(profile)
    if normalized not in NEW_RISK_ENTRY_PROFILES:
        return "execution_entry_invalidation_profile_invalid"
    if isinstance(invalidation_level, bool) or not isinstance(
        invalidation_level,
        (int, float),
    ):
        return "execution_entry_invalidation_level_invalid"
    if not math.isfinite(float(invalidation_level)) or float(
        invalidation_level
    ) <= 0.0:
        return "execution_entry_invalidation_level_invalid"
    if not is_canonical_entry_invalidation_condition(
        invalidation_condition,
        profile=normalized,
        side=side,
    ):
        return "execution_entry_invalidation_condition_invalid"
    return ""


def execution_profile_allowed_for_analyst(analyst: Any, profile: Any) -> bool:
    analyst_name = str(analyst or "").strip()
    normalized = normalize_execution_profile(profile)
    if analyst_name == "technical":
        return normalized in TECHNICAL_ENTRY_PROFILES
    if analyst_name == "commodity_news":
        return normalized in NEWS_ENTRY_PROFILES
    return False


def trigger_source_for_analyst_profile(analyst: Any, profile: Any) -> str:
    return _TRIGGER_SOURCE_BY_ANALYST_AND_PROFILE.get(
        (str(analyst or "").strip(), normalize_execution_profile(profile)),
        "",
    )


def trigger_source_matches_profile(profile: Any, trigger_source: Any) -> bool:
    normalized = normalize_execution_profile(profile)
    source = str(trigger_source or "").strip()
    return source in _TRIGGER_SOURCES_BY_PROFILE.get(normalized, frozenset())


def execution_trigger_contract_error(
    *,
    profile: Any,
    side: Any,
    entry_trigger: Any,
    trigger_source: Any,
) -> str:
    """Return a stable error code for one PM/Trader execution contract."""
    normalized = normalize_execution_profile(profile)
    if not normalized:
        return "execution_profile_contract_invalid"
    if not trigger_source_matches_profile(normalized, trigger_source):
        return "execution_trigger_source_contract_invalid"
    if normalized in NEW_RISK_ENTRY_PROFILES:
        expected = canonical_entry_trigger(normalized, side)
        if not expected or str(entry_trigger or "").strip() != expected:
            return "execution_entry_trigger_contract_invalid"
    return ""
