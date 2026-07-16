import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.common.contracts import (
    build_internal_message_contract,
    build_trade_research_contract,
    validate_internal_message_contract,
    validate_trade_research_contract,
)
from tools.common.evidence_fusion_semantics import build_analyst_fusion_evidence
from tools.common.signal_evidence_collection import (
    ACTION_EVIDENCE_EXCLUDED_SIGNAL_FIELDS,
    has_concrete_entry_trigger,
    validate_action_evidence_contract,
)
from tools.agent_tools.analysis.analyst_market_confirmation import score_pandaai_extra_records
from tools.agent_tools.analysis.analyst_output_landing import apply_analyst_output_landing_check
from util.logger import logger
from util.text_sanitize import sanitize_visible_text


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

SECTOR_GUIDANCE = {
    "energy": {
        "technical": "Emphasize trend, volatility, volume confirmation, and crude-oil cost disturbance. Avoid chasing after sharp cost-driven spikes.",
        "fundamental": "Focus on crude-oil cost, refinery operating rates, bitumen shipments, road-construction demand, refinery inventory, and social inventory.",
        "news": "Treat crude oil, refinery output, road construction, policy, and inventory news as the highest-impact event classes.",
    },
    "chemical": {
        "technical": "Chemical contracts often oscillate around cost, margin, and inventory cycles. Require trend confirmation before following breakouts.",
        "fundamental": "Focus on upstream costs, plant operating rates, processing margins, port or factory inventory, and downstream operating rates.",
        "news": "Prioritize plant outages, maintenance, operating rates, port inventory, feedstock shocks, and downstream order news.",
    },
    "ferrous": {
        "technical": "Ferrous signals should prefer chain confirmation. Isolated single-contract breakouts deserve lower confidence.",
        "fundamental": "Focus on hot metal output, steel margins, mill inventories, social inventories, property/infrastructure demand, and ore/coke cost chains.",
        "news": "Prioritize property, infrastructure, hot metal, steel-mill margins, production restrictions, inventories, and chain-wide policy news.",
    },
    "nonferrous": {
        "technical": "Nonferrous signals are more sensitive to overseas inventory, macro risk, and funding-driven breakouts. Guard against false breakouts in high volatility.",
        "fundamental": "Focus on mine supply, treatment charges, smelter margins, social inventory, SHFE/LME inventory, imports, and downstream operating rates.",
        "news": "Prioritize overseas supply, LME/SHFE inventory, treatment charges, smelter disruptions, macro rates, and FX news.",
    },
    "agricultural": {
        "technical": "Agricultural contracts need stricter confirmation because seasonal, policy, weather, and import news can create noisy short-term reversals.",
        "fundamental": "Focus on production, sales progress, imports, inventories, weather, policy, consumption, crushing/processing margins, and substitution effects.",
        "news": "Prioritize weather, crop progress, import/export, policy, inventory, consumption, and substitution news. De-emphasize stale generic macro headlines.",
    },
}

SECTOR_TECHNICAL_SETUP_POLICY = {
    "energy": {
        "preferred_setups": ["trend_breakout", "trend_pullback", "volatility_breakout"],
        "caution_setups": ["range_reversal"],
        "primary_confirmation": ["volume_ratio", "open_interest", "crude_cost_context"],
        "execution_focus": "avoid_chasing_after_cost_spikes",
    },
    "chemical": {
        "preferred_setups": ["range_reversal", "trend_breakout", "trend_pullback"],
        "caution_setups": ["volatility_breakout"],
        "primary_confirmation": ["cost_margin", "inventory_cycle", "volume_ratio"],
        "execution_focus": "confirm_cost_inventory_regime_before_breakout",
    },
    "ferrous": {
        "preferred_setups": ["trend_breakout", "trend_pullback"],
        "caution_setups": ["range_reversal", "volatility_breakout"],
        "primary_confirmation": ["chain_confirmation", "volume_ratio", "open_interest"],
        "execution_focus": "prefer_chain_confirmed_breakouts",
    },
    "nonferrous": {
        "preferred_setups": ["trend_breakout", "volatility_breakout", "trend_pullback"],
        "caution_setups": ["range_reversal"],
        "primary_confirmation": ["macro_inventory_confirmation", "volume_ratio", "external_market_alignment"],
        "execution_focus": "guard_false_breakout_under_macro_volatility",
    },
    "agricultural": {
        "preferred_setups": ["range_reversal", "trend_breakout"],
        "caution_setups": ["volatility_breakout"],
        "primary_confirmation": ["seasonal_weather_policy_context", "volume_ratio", "price_location"],
        "execution_focus": "require_current_confirmation_against_weather_policy_noise",
    },
    "generic": {
        "preferred_setups": ["trend_breakout", "range_reversal", "volatility_breakout"],
        "caution_setups": [],
        "primary_confirmation": ["volume_ratio", "market_confirmation"],
        "execution_focus": "use_current_confirmation",
    },
}

SECTOR_FUNDAMENTAL_FACTOR_TREE = {
    "energy": {
        "primary_drivers": ["cost", "supply", "inventory", "demand"],
        "supporting_drivers": ["basis", "profit", "import_export"],
        "risk_drivers": ["macro_policy", "context"],
        "short_trigger_groups": ["inventory", "basis", "demand"],
    },
    "chemical": {
        "primary_drivers": ["profit", "inventory", "supply", "demand"],
        "supporting_drivers": ["basis", "cost", "import_export"],
        "risk_drivers": ["macro_policy", "context"],
        "short_trigger_groups": ["profit", "inventory", "demand", "basis"],
    },
    "ferrous": {
        "primary_drivers": ["demand", "inventory", "profit", "supply"],
        "supporting_drivers": ["basis", "import_export", "macro_policy"],
        "risk_drivers": ["macro_policy", "context"],
        "short_trigger_groups": ["demand", "inventory", "basis"],
    },
    "nonferrous": {
        "primary_drivers": ["supply", "inventory", "import_export", "basis"],
        "supporting_drivers": ["profit", "macro_policy", "capital_flow"],
        "risk_drivers": ["macro_policy", "context", "positioning"],
        "short_trigger_groups": ["inventory", "basis", "capital_flow"],
    },
    "agricultural": {
        "primary_drivers": ["supply", "demand", "inventory", "import_export"],
        "supporting_drivers": ["basis", "profit", "macro_policy"],
        "risk_drivers": ["weather", "policy", "context"],
        "short_trigger_groups": ["inventory", "basis", "import_export", "weather"],
    },
    "generic": {
        "primary_drivers": ["supply", "demand", "inventory", "basis"],
        "supporting_drivers": ["profit", "import_export", "macro_policy"],
        "risk_drivers": ["context"],
        "short_trigger_groups": ["inventory", "basis"],
    },
}

SECTOR_NEWS_CATALYST_POLICY = {
    "energy": {
        "tradable_catalysts": ["supply", "inventory", "policy", "macro"],
        "risk_events": ["policy", "macro"],
        "noise_events": ["context"],
        "event_window_days": 3,
    },
    "chemical": {
        "tradable_catalysts": ["supply", "inventory", "demand"],
        "risk_events": ["policy", "macro"],
        "noise_events": ["context"],
        "event_window_days": 3,
    },
    "ferrous": {
        "tradable_catalysts": ["demand", "inventory", "policy", "supply"],
        "risk_events": ["policy", "macro"],
        "noise_events": ["context"],
        "event_window_days": 3,
    },
    "nonferrous": {
        "tradable_catalysts": ["overseas", "inventory", "supply", "macro"],
        "risk_events": ["macro", "overseas", "policy"],
        "noise_events": ["context"],
        "event_window_days": 3,
    },
    "agricultural": {
        "tradable_catalysts": ["weather", "import_export", "policy", "inventory"],
        "risk_events": ["weather", "policy"],
        "noise_events": ["macro", "context"],
        "event_window_days": 5,
    },
    "generic": {
        "tradable_catalysts": ["supply", "demand", "inventory", "policy"],
        "risk_events": ["policy", "macro"],
        "noise_events": ["context"],
        "event_window_days": 3,
    },
}

def get_sector(ticker: str) -> str:
    return SECTOR_BY_TICKER.get(str(ticker).upper(), "generic")


def get_sector_guidance(ticker: str, analyst: str) -> str:
    sector = get_sector(ticker)
    guidance = SECTOR_GUIDANCE.get(sector, {})
    return guidance.get(analyst, "Use commodity-specific evidence and prefer Neutral when evidence is mixed.")


def _technical_setup_policy(ticker: str) -> Dict[str, Any]:
    return dict(SECTOR_TECHNICAL_SETUP_POLICY.get(get_sector(ticker), SECTOR_TECHNICAL_SETUP_POLICY["generic"]))


def _fundamental_factor_tree(ticker: str) -> Dict[str, Any]:
    return dict(SECTOR_FUNDAMENTAL_FACTOR_TREE.get(get_sector(ticker), SECTOR_FUNDAMENTAL_FACTOR_TREE["generic"]))


def _news_catalyst_policy(ticker: str) -> Dict[str, Any]:
    return dict(SECTOR_NEWS_CATALYST_POLICY.get(get_sector(ticker), SECTOR_NEWS_CATALYST_POLICY["generic"]))


def _group_names_with_direction(group_counts: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for group, counts in (group_counts or {}).items():
        if not isinstance(counts, Counter):
            counts = Counter(counts or {})
        if counts.get("up", 0) or counts.get("down", 0):
            names.append(str(group))
    return sorted(set(names))


def _intersect_ordered(items: Iterable[Any], available: Iterable[Any]) -> List[str]:
    available_set = {str(item) for item in available}
    return [str(item) for item in items or [] if str(item) in available_set]


def get_analyst_llm_config(full_config: Dict[str, Any], analyst: str) -> Dict[str, Any]:
    cfg = full_config.get("analyst_llm", {}) or {}
    llm_cfg = full_config.get("llm", {}) or {}
    del analyst
    return {
        "provider": llm_cfg.get("provider"),
        "model": llm_cfg.get("model"),
        "write_decision_reports": bool(cfg.get("write_decision_reports", True)),
        "force_neutral_low_tradeability": bool(cfg.get("force_neutral_low_tradeability", True)),
        "cap_medium_tradeability_confidence": float(cfg.get("cap_medium_tradeability_confidence", 0.65)),
        "cap_low_tradeability_confidence": float(cfg.get("cap_low_tradeability_confidence", 0.35)),
        "force_neutral_stale_fundamental": bool(cfg.get("force_neutral_stale_fundamental", True)),
        "stale_fundamental_stale_ratio": float(cfg.get("stale_fundamental_stale_ratio", 0.35)),
        "stale_fundamental_freshness_score": float(cfg.get("stale_fundamental_freshness_score", 0.45)),
        "cap_stale_fundamental_confidence": float(cfg.get("cap_stale_fundamental_confidence", 0.30)),
    }


def llm_path_label(full_config: Dict[str, Any], analyst: str) -> str:
    base = get_analyst_llm_config(full_config, analyst)
    llm_cfg = full_config.get("llm", {}) or {}
    provider_key = str(llm_cfg.get("provider") or "").lower()
    provider_cfg_key = {
        "codexopenai": "codex_openai",
        "tqxai": "tqxai",
    }.get(provider_key)
    provider_cfg = llm_cfg.get(provider_cfg_key, {}) if provider_cfg_key else {}
    reasoning_cfg = provider_cfg.get("reasoning", {}) or {}
    parts = []
    if llm_cfg.get("provider"):
        parts.append(f"provider={llm_cfg.get('provider')}")
    if base.get("model"):
        parts.append(f"model={base.get('model')}")
    effort = provider_cfg.get("reasoning_effort") or reasoning_cfg.get("effort")
    if effort:
        parts.append(f"reasoning_effort={effort}")
    return "; ".join(parts)


def signal_value(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def normalize_trading_date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value)
    return text[:10] if len(text) >= 10 else text


def _clean_list(value: Any, *, max_items: int = 8) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = [part.strip() for part in re.split(r"[,;|]\s*", value) if part.strip()]
    elif isinstance(value, dict):
        raw = [str(key) for key, enabled in value.items() if enabled not in (False, None, "", 0)]
    else:
        try:
            raw = [str(item).strip() for item in value if str(item).strip()]
        except Exception:
            raw = [str(value).strip()] if str(value).strip() else []
    seen = set()
    items: List[str] = []
    for item in raw:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(item[:120])
        if len(items) >= max_items:
            break
    return items


def _factor_focus_from_context(quality_context: Dict[str, Any], analyst: str) -> List[str]:
    candidates: List[Any] = []
    if analyst == "technical":
        votes = quality_context.get("indicator_votes") or {}
        details = votes.get("details") if isinstance(votes, dict) else {}
        if isinstance(details, dict):
            candidates.extend(details.keys())
        features = quality_context.get("features") or {}
        if isinstance(features, dict):
            candidates.extend(features.keys())
    elif analyst == "fundamental":
        group_counts = quality_context.get("factor_group_counts") or {}
        if isinstance(group_counts, dict):
            candidates.extend([key for key, count in group_counts.items() if count])
        for key in ("basis", "pandaai_extra_factors", "data_quality"):
            if quality_context.get(key):
                candidates.append(key)
    else:
        event_counts = quality_context.get("event_type_counts") or {}
        if isinstance(event_counts, dict):
            candidates.extend([key for key, count in event_counts.items() if count])
        candidates.extend(["event_direction", "event_freshness"])
    if not candidates:
        candidates.extend(_clean_list(quality_context.get("risk_flags"), max_items=4))
    return _clean_list(candidates, max_items=8)


def infer_opportunity_type(signal: AnalystSignal, quality_context: Dict[str, Any], analyst: str) -> str:
    if signal_value(signal.signal) == Signal.NEUTRAL.value:
        return "no_trade"
    trigger = str(getattr(signal, "entry_trigger", "") or "").lower()
    setup_type = str(getattr(signal, "setup_type", "") or "").lower()
    regime = str(getattr(signal, "market_regime", "") or quality_context.get("market_regime") or "").lower()
    if analyst == "commodity_news" or "event" in trigger or "news" in trigger:
        return "event_driven"
    if analyst == "fundamental":
        return "medium_fundamental"
    if "continuation" in trigger or "trend" in trigger or "trend" in setup_type or regime in {"trend", "trending"}:
        return "trend_continuation"
    if "reversal" in trigger or "reversal" in setup_type or "reversal" in regime:
        return "reversal"
    if "breakout" in trigger or "breakout" in setup_type:
        return "range_breakout"
    return "short_timing" if analyst == "technical" else "probe"


def _holding_period_hint(signal: AnalystSignal) -> str:
    horizon = str(getattr(signal, "horizon_class", "") or "unknown")
    days = int(getattr(signal, "expected_horizon_days", 0) or 0)
    if days > 0:
        return f"{horizon}:{days} trading day(s)"
    return horizon


def _has_actionable_text(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text or text in {"unknown", "none", "n/a", "null"}:
        return False
    generic_markers = {
        "wait_for_trigger",
        "primary driver and secondary confirmation align with acceptable reward/risk",
        "exit/reduce if current confirmation fails",
    }
    return text not in generic_markers


_PENDING_ENTRY_TRIGGER_MARKERS = (
    "only if",
    "only after",
    "if price",
    "if futures",
    "if volume",
    "if basis",
    "if inventory",
    "would require",
    "becomes tradeable",
    "become tradeable",
    "tradeable only if",
    "make the setup tradeable",
    "move from watchlist to tradeable",
    "convert to a tradeable",
    "requires price",
    "requires technical",
    "requires market",
    "requires current",
    "requires confirmation",
    "requires a confirmation",
    "requires confirmed",
    "requires a confirmed",
    "requires break",
    "requires a break",
    "requires breakout",
    "requires a breakout",
    "requires breakdown",
    "requires a breakdown",
    "require price",
    "require technical",
    "require market",
    "require confirmation",
    "require a confirmation",
    "require confirmed",
    "require a confirmed",
    "require break",
    "require a break",
    "require breakout",
    "require a breakout",
    "require breakdown",
    "require a breakdown",
    "require post-open",
    "requires post-open",
    "needs price",
    "needs technical",
    "needs market",
    "needs confirmation",
    "needs post-open",
    "need confirmation",
    "wait for",
    "waiting for",
    "should confirm",
    "must confirm",
    "must break",
    "must hold",
    "becomes actionable",
    "become actionable",
    "actionable only if",
    "before entry",
    "before execution",
    "after the open",
    "after open",
    "without that confirmation",
    "without confirmation",
    "remain on watch",
    "remains on watch",
    "stay on watch",
    "stays on watch",
    "until price",
    "until futures",
    "如果",
    "若",
    "等待",
    "确认后",
    "后再",
    "之后再",
    "需要",
    "才可",
)


_CURRENT_ENTRY_TRIGGER_MARKERS = (
    "has broken",
    "has breached",
    "has crossed",
    "has confirmed",
    "is breaking",
    "is below",
    "is above",
    "currently below",
    "currently above",
    "current breakout",
    "current breakdown",
    "trigger is active",
    "trigger is valid",
    "confirmed by current",
    "已突破",
    "已跌破",
    "已站上",
    "已站稳",
    "已经突破",
    "已经跌破",
    "当前突破",
    "当前跌破",
    "触发成立",
)


_STRICT_PENDING_ENTRY_MARKERS = (
    "only if",
    "only after",
    "would require",
    "becomes tradeable",
    "become tradeable",
    "tradeable only if",
    "make the setup tradeable",
    "move from watchlist to tradeable",
    "convert to a tradeable",
    "wait for",
    "waiting for",
    "should confirm",
    "must confirm",
    "must break",
    "must hold",
    "becomes actionable",
    "become actionable",
    "actionable only if",
    "requires confirmed",
    "requires a confirmed",
    "requires break",
    "requires a break",
    "requires breakout",
    "requires a breakout",
    "requires breakdown",
    "requires a breakdown",
    "require confirmed",
    "require a confirmed",
    "require break",
    "require a break",
    "require breakout",
    "require a breakout",
    "require breakdown",
    "require a breakdown",
    "require post-open",
    "requires post-open",
    "needs post-open",
    "before entry",
    "before execution",
    "after the open",
    "after open",
    "without that confirmation",
    "without confirmation",
    "remain on watch",
    "remains on watch",
    "stay on watch",
    "stays on watch",
    "等待",
    "确认后",
    "后再",
    "之后再",
    "才可",
)


def _has_current_entry_trigger_text(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return any(marker in text for marker in _CURRENT_ENTRY_TRIGGER_MARKERS)


def _is_pending_conditional_entry_trigger(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not _has_actionable_text(text):
        return False
    pending = any(marker in text for marker in _PENDING_ENTRY_TRIGGER_MARKERS)
    if not pending:
        return False
    strict_pending = any(marker in text for marker in _STRICT_PENDING_ENTRY_MARKERS)
    if _has_current_entry_trigger_text(text) and not strict_pending:
        return False
    return True


_FUNDAMENTAL_DERIVED_SHORT_TRIGGER_MARKERS = (
    "short-term technical/price confirmation aligns with fundamental factor groups",
)


def _is_derived_fundamental_short_trigger(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in _FUNDAMENTAL_DERIVED_SHORT_TRIGGER_MARKERS)


def _has_explicit_fundamental_short_trigger(signal: AnalystSignal, quality_context: Dict[str, Any]) -> bool:
    """True only when a fundamental anchor has a real short-horizon entry condition.

    The auto-derived "wait for short-term confirmation" wording is an auditable
    condition, not proof that confirmation already exists. Treating it as a
    completed trigger lets medium fundamental direction views become real probes.
    """
    metadata = getattr(signal, "metadata", {}) or {}
    current_confirmation_flags = (
        "short_term_trigger_confirmed",
        "short_term_confirmation_confirmed",
        "technical_confirmation_confirmed",
        "price_trigger_confirmed",
        "intraday_confirmation_confirmed",
        "market_confirmation_confirmed",
        "execution_trigger_confirmed",
    )
    for flag in current_confirmation_flags:
        if bool(quality_context.get(flag)) or bool(metadata.get(flag)):
            return True

    entry_trigger = getattr(signal, "entry_trigger", "")
    if _is_derived_fundamental_short_trigger(entry_trigger):
        return False
    if _is_pending_conditional_entry_trigger(entry_trigger):
        return False
    return _has_current_entry_trigger_text(entry_trigger)


def _current_confirmation_flag(signal: AnalystSignal, quality_context: Dict[str, Any], *names: str) -> bool:
    metadata = getattr(signal, "metadata", {}) or {}
    for name in names:
        if bool(quality_context.get(name)) or bool(metadata.get(name)):
            return True
    return False


def _current_entry_confirmation_available(
    signal: AnalystSignal,
    quality_context: Dict[str, Any],
    analyst: str,
) -> bool:
    if _current_confirmation_flag(
        signal,
        quality_context,
        "current_trigger_confirmed",
        "short_term_trigger_confirmed",
        "short_term_confirmation_confirmed",
        "technical_confirmation_confirmed",
        "price_trigger_confirmed",
        "intraday_confirmation_confirmed",
        "market_confirmation_confirmed",
        "execution_trigger_confirmed",
        "price_reaction_confirmed",
    ):
        return True
    if analyst == "technical":
        # The setup-quality flags below used to mean "tradeable now". They now
        # only mean the pattern is worth attention; current entry confirmation
        # must come from explicit current-confirmation fields or trigger text
        # that has already been normalized into signal.trigger_valid.
        return False
    if analyst == "commodity_news":
        return bool(
            quality_context.get("tradable_event")
            and not quality_context.get("price_reaction_required", True)
        )
    return False


def _current_entry_trigger_confirmed(
    signal: AnalystSignal,
    quality_context: Dict[str, Any],
    analyst: str,
    entry_trigger: Any,
) -> bool:
    if _current_entry_confirmation_available(signal, quality_context, analyst):
        return True
    if _is_pending_conditional_entry_trigger(entry_trigger):
        return False
    if bool(getattr(signal, "trigger_valid", False)):
        return True
    return _has_current_entry_trigger_text(entry_trigger)


def _technical_setup_scope(quality_context: Dict[str, Any], opportunity_type: str = "") -> Dict[str, Any]:
    learning_scope = quality_context.get("learning_scope")
    learning_scope = learning_scope if isinstance(learning_scope, dict) else {}
    return {
        "setup_family": (
            learning_scope.get("setup_family")
            or quality_context.get("setup_type")
            or opportunity_type
            or "unknown"
        ),
        "sector_setup_alignment": learning_scope.get("sector_setup_alignment") or quality_context.get("sector_setup_alignment"),
        "market_regime": learning_scope.get("market_regime") or quality_context.get("market_regime"),
        "required_confirmation": (
            quality_context.get("required_confirmation")
            or "current technical confirmation"
        ),
        "invalidation_template": (
            quality_context.get("invalidation_condition")
            or "T-day confirmation fails, price closes back inside the failed trigger area, or opposite momentum/volume confirmation appears"
        ),
    }


def _sync_signal_fields_to_action_evidence_contract(
    contract: Dict[str, Any],
    signal: AnalystSignal,
    metadata: Dict[str, Any],
) -> None:
    """Land final registered analyst evidence in the formal action contract."""
    contract["signal"] = signal_value(signal.signal)
    for field in AnalystSignal.model_fields:
        if field in ACTION_EVIDENCE_EXCLUDED_SIGNAL_FIELDS or field == "signal":
            continue
        contract[field] = getattr(signal, field)
    data_usage_summary = metadata.get("data_usage_summary")
    if isinstance(data_usage_summary, dict):
        contract["data_usage_summary"] = dict(data_usage_summary)
    invalidation_condition = str(metadata.get("invalidation_condition") or "").strip()
    if invalidation_condition:
        contract["invalidation_condition"] = invalidation_condition


def _build_action_evidence_contract(
    signal: AnalystSignal,
    quality_context: Dict[str, Any],
    *,
    analyst: str,
    opportunity_type: str,
    opportunity_state: str,
    entry_trigger: str,
    exit_hint: str,
    has_invalidation: bool,
) -> Dict[str, Any]:
    contract: Dict[str, Any] = {}
    sector = str(quality_context.get("sector") or "")
    side = _signal_side(signal)
    contract.update(
        {
            "contract_version": "agentquant.action_evidence.v1",
            "analyst": analyst,
            "sector": sector,
            "side": side,
            "opportunity_type": opportunity_type,
            "opportunity_state": str(opportunity_state or "watch_for_trigger"),
            "setup_type": str(opportunity_type or "unknown"),
            "setup_quality_ok": bool(quality_context.get("setup_quality_ok")),
            "trigger_valid": bool(getattr(signal, "trigger_valid", False)),
            "entry_trigger": entry_trigger,
            "exit_hint": exit_hint,
            "invalidation_present": bool(has_invalidation),
        }
    )
    metadata = getattr(signal, "metadata", {}) or {}
    _sync_signal_fields_to_action_evidence_contract(contract, signal, metadata)
    learning_scope = dict(quality_context.get("learning_scope") or {})
    learning_scope.update(dict(metadata.get("learning_scope") or {}))
    if analyst == "technical":
        setup = _technical_setup_scope(quality_context, opportunity_type)
        learning_scope.update(
            {
                "setup_family": setup.get("setup_family") or opportunity_type,
                "sector_setup_alignment": setup.get("sector_setup_alignment"),
                "market_regime": quality_context.get("market_regime"),
            }
        )
    elif analyst == "fundamental":
        roles = quality_context.get("fundamental_driver_roles") if isinstance(quality_context.get("fundamental_driver_roles"), dict) else {}
        learning_scope.update(
            {
                "primary_driver_groups": roles.get("primary_driver_groups") or [],
                "short_trigger_groups": roles.get("short_trigger_groups") or [],
                "conflict_groups": roles.get("conflict_groups") or [],
            }
        )
    elif analyst == "commodity_news":
        classification = quality_context.get("catalyst_classification") if isinstance(quality_context.get("catalyst_classification"), dict) else {}
        learning_scope.update(
            {
                "event_regime": quality_context.get("event_regime"),
                "catalyst_classification": classification,
            }
        )
    contract["learning_scope"] = learning_scope
    profile_evidence = metadata.get("product_profile_evidence")
    if isinstance(profile_evidence, dict):
        contract["product_profile_evidence"] = dict(profile_evidence)
    return contract


def _has_specific_invalidation_text(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not _has_actionable_text(text):
        return False
    if text in {"exit/reduce if current confirmation fails"}:
        return False
    if text.startswith("would change view"):
        return False
    return any(
        token in text
        for token in (
            "invalid",
            "fails",
            "failure",
            "breaks",
            "below",
            "above",
            "close",
            "stop",
            "exit",
            "reduce",
            "contradict",
            "conflict",
            "reverses",
            "loses",
            "regime flips",
            "price fails",
            "volume fails",
        )
    )


def _signal_side(signal: AnalystSignal) -> str:
    value = signal_value(getattr(signal, "signal", Signal.NEUTRAL))
    if value == Signal.BULLISH.value:
        return "long"
    if value == Signal.BEARISH.value:
        return "short"
    return "flat"


def _has_trade_setup_text(signal: AnalystSignal) -> bool:
    return has_concrete_entry_trigger(getattr(signal, "entry_trigger", ""))


def _has_numeric_invalidation(value: Any, *, positive: bool = False) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return parsed > 0.0 if positive else True


def _land_canonical_invalidation_condition(
    signal: AnalystSignal,
    metadata: Dict[str, Any],
) -> tuple[Dict[str, Any], List[str]]:
    """Land an explicit producer condition before shared validation.

    ``exit_hint`` is an analyst output field, but downstream consumers only
    recognize the registered ``invalidation_condition``. Generic exit hints
    and view-change observations are intentionally not promoted.
    """
    notes: List[str] = []
    existing = str(metadata.get("invalidation_condition") or "").strip()
    if _has_specific_invalidation_text(existing):
        return metadata, notes
    explicit_exit = str(getattr(signal, "exit_hint", "") or "").strip()
    if _has_specific_invalidation_text(explicit_exit):
        metadata = {**metadata, "invalidation_condition": explicit_exit}
        notes.append("producer_invalidation_landed_in_canonical_field")
    else:
        metadata = dict(metadata)
        metadata.pop("invalidation_condition", None)
    return metadata, notes


def _trade_setup_contract_presence(signal: AnalystSignal) -> Dict[str, bool]:
    entry_present = has_concrete_entry_trigger(getattr(signal, "entry_trigger", ""))
    metadata = getattr(signal, "metadata", {}) or {}
    invalidation_present = bool(
        _has_numeric_invalidation(getattr(signal, "invalidation_level", None))
        or _has_numeric_invalidation(
            getattr(signal, "atr_stop_distance", None),
            positive=True,
        )
        or _has_specific_invalidation_text(metadata.get("invalidation_condition"))
    )
    exit_present = (
        _has_actionable_text(getattr(signal, "exit_hint", ""))
        or _has_actionable_text(getattr(signal, "counter_evidence", ""))
        or invalidation_present
    )
    holding_present = _has_actionable_text(getattr(signal, "holding_period_hint", "")) or _has_actionable_text(
        _holding_period_hint(signal)
    )
    tradable_why_present = bool(
        _has_actionable_text(getattr(signal, "setup_type", ""))
        or _clean_list(getattr(signal, "factor_focus", []), max_items=2)
        or _has_actionable_text(getattr(signal, "justification", ""))
    )
    return {
        "why_tradable_present": tradable_why_present,
        "entry_trigger_present": entry_present,
        "invalidation_present": invalidation_present,
        "exit_hint_present": exit_present,
        "holding_period_present": holding_present,
    }


def _trade_setup_missing_fields(presence: Dict[str, bool]) -> List[str]:
    return [key for key, ok in presence.items() if not ok]


def _derive_analyst_trade_setup_fields(
    signal: AnalystSignal,
    quality_context: Dict[str, Any],
    analyst: str,
) -> Dict[str, Any]:
    """Return only registered producer fields; never manufacture a setup.

    This compatibility-shaped helper remains local to the finalizer so callers
    keep one path. It may land an explicit producer invalidation into its
    canonical field, but it never creates entry, direction, or market facts.
    """
    _ = quality_context, analyst
    metadata, notes = _land_canonical_invalidation_condition(
        signal,
        dict(getattr(signal, "metadata", {}) or {}),
    )
    return {
        "invalidation_condition": metadata.get("invalidation_condition", ""),
        "notes": notes,
    }


def _price_percentile(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return None


def _setup_quality_assessment(
    signal: AnalystSignal,
    quality_context: Dict[str, Any],
    *,
    analyst: str,
    setup_presence: Dict[str, bool],
    opportunity_type: str,
    has_invalidation: bool,
) -> Dict[str, Any]:
    """Score whether a directional signal is a tradable setup, not just a view.

    This is deliberately generic: it scores price location, entry trigger,
    invalidation, market regime, data coverage, and conflict. It does not
    blacklist or boost any specific product.
    """
    if signal_value(signal.signal) == Signal.NEUTRAL.value:
        return {
            "score": 0.0,
            "entry_quality": "unknown",
            "notes": ["neutral_signal_no_trade_setup"],
        }

    notes: List[str] = []
    score = 0.0
    if setup_presence.get("why_tradable_present"):
        score += 0.12
    else:
        notes.append("missing_why_tradable")
    if setup_presence.get("entry_trigger_present"):
        score += 0.18
    else:
        notes.append("missing_entry_trigger")
    if has_invalidation:
        score += 0.18
    else:
        notes.append("missing_invalidation_boundary")
    if setup_presence.get("exit_hint_present"):
        score += 0.08
    else:
        notes.append("missing_exit_hint")
    if setup_presence.get("holding_period_present"):
        score += 0.08
    else:
        notes.append("missing_holding_period")

    business_score = _safe_float(getattr(signal, "business_quality_score", 0.0), 0.0)
    data_score = max(
        _safe_float(getattr(signal, "data_coverage_score", 0.0), 0.0),
        _safe_float((quality_context.get("data_quality") or {}).get("coverage_ratio"), 0.0)
        if isinstance(quality_context.get("data_quality"), dict)
        else 0.0,
        _safe_float(quality_context.get("freshness_score"), 0.0),
    )
    score += 0.18 * business_score
    score += 0.10 * data_score

    conflicts = _clean_list(getattr(signal, "current_evidence_conflict", []), max_items=8)
    risk_flags = _clean_list(quality_context.get("risk_flags"), max_items=8)
    soft_risk_flags = {"high_volatility"}
    conflicts.extend(item for item in risk_flags if item not in conflicts)
    score_conflicts = [item for item in conflicts if str(item) not in soft_risk_flags]
    if score_conflicts:
        score -= min(0.18, 0.04 * len(score_conflicts))
        notes.append("current_evidence_conflict")
    elif risk_flags:
        notes.append("soft_risk_flag_for_pm_sizing")

    regime = str(quality_context.get("market_regime") or getattr(signal, "market_regime", "") or "").lower()
    trend_like = "trend" in str(opportunity_type or "").lower()
    if trend_like and regime in {"choppy", "range", "weak_trend", "high_volatility"}:
        score -= 0.16
        notes.append("trend_setup_in_choppy_or_range_regime")

    percentile = _price_percentile(getattr(signal, "price_percentile", None))
    side = signal_value(signal.signal)
    if percentile is not None:
        if side == Signal.BULLISH.value and percentile >= 0.82:
            score -= 0.12
            notes.append("late_long_entry_price_near_upper_range")
        elif side == Signal.BEARISH.value and percentile <= 0.18:
            score -= 0.12
            notes.append("late_short_entry_price_near_lower_range")
        elif side == Signal.BULLISH.value and 0.25 <= percentile <= 0.70:
            score += 0.04
            notes.append("long_entry_price_location_reasonable")
        elif side == Signal.BEARISH.value and 0.30 <= percentile <= 0.75:
            score += 0.04
            notes.append("short_entry_price_location_reasonable")

    if analyst == "commodity_news" and not bool(quality_context.get("price_reaction_confirmed")):
        score -= 0.08
        notes.append("news_lacks_price_reaction_confirmation")
    if analyst == "fundamental" and str(getattr(signal, "horizon_class", "") or "").lower() in {"medium", "long"}:
        if not _has_actionable_text(getattr(signal, "entry_trigger", "")):
            score -= 0.10
            notes.append("medium_fundamental_missing_short_timing_trigger")

    score = max(0.0, min(1.0, score))
    if score >= 0.78:
        entry_quality = "strong"
    elif score >= 0.60:
        entry_quality = "acceptable"
    elif score >= 0.42:
        entry_quality = "weak"
    else:
        entry_quality = "poor"
    return {
        "score": score,
        "entry_quality": entry_quality,
        "notes": sorted(set(notes)),
    }


def _resolve_opportunity_candidate_state(
    signal: AnalystSignal,
    quality_context: Dict[str, Any],
    *,
    analyst: str,
    base_state: str,
    opportunity_type: str,
    has_invalidation: bool,
) -> tuple[str, List[str]]:
    """Convert analyst output into a unified opportunity_state candidate."""
    risk_flags = _clean_list(quality_context.get("risk_flags"), max_items=12)
    state_notes: List[str] = []
    metadata = getattr(signal, "metadata", {}) or {}
    data_usage = metadata.get("data_usage_summary") if isinstance(metadata.get("data_usage_summary"), dict) else {}
    if str(opportunity_type or "") == "no_trade" and data_usage.get("data_available") is False:
        state_notes.append("current_professional_data_unavailable_no_opportunity")
        return "no_opportunity", state_notes
    signal_text = signal_value(signal.signal)
    if signal_text == Signal.NEUTRAL.value:
        return base_state, state_notes

    if analyst == "commodity_news":
        tradable_event = bool(quality_context.get("tradable_event"))
        price_reaction_confirmed = bool(quality_context.get("price_reaction_confirmed"))
        if not tradable_event:
            state_notes.append("news_event_not_tradable_catalyst")
            return "watch_for_trigger", state_notes
        if quality_context.get("price_reaction_required", True) and not price_reaction_confirmed:
            state_notes.append("news_event_requires_price_or_intraday_confirmation")
            return "watch_for_trigger", state_notes

    if analyst == "technical":
        regime = str(quality_context.get("market_regime") or "").lower()
        trend_like = "trend" in str(opportunity_type or "").lower()
        if trend_like and regime in {"choppy", "range", "weak_trend", "high_volatility"}:
            if not bool(quality_context.get("setup_quality_ok")):
                state_notes.append("technical_trend_requires_regime_confirmation")
                return "watch_for_trigger", state_notes

    if analyst == "fundamental":
        # Fundamental evidence is a medium anchor. It may support PM sizing, but
        # it should not be treated as a short-horizon setup by itself.
        entry_trigger = getattr(signal, "entry_trigger", "")
        data_quality = quality_context.get("data_quality") if isinstance(quality_context, dict) else {}
        if isinstance(data_quality, dict) and not bool(data_quality.get("supports_fundamental_trade_setup", True)):
            state_notes.append("fundamental_local_data_insufficient_for_trade_setup")
            return "watch_for_trigger", state_notes
        if str(getattr(signal, "horizon_class", "") or "").lower() in {"medium", "long", "unknown"}:
            if not has_invalidation or not _has_explicit_fundamental_short_trigger(signal, quality_context):
                state_notes.append("fundamental_anchor_requires_short_trigger_and_invalidation")
                return "watch_for_trigger", state_notes
            state_notes.append("fundamental_anchor_has_short_trigger_and_invalidation")
            if base_state == "tradeable_candidate" and not bool(quality_context.get("fundamental_deployable_confirmed")):
                state_notes.append("fundamental_anchor_tradeable_until_pm_confirmation")
                return "probe_candidate", state_notes
            return base_state, state_notes

    return base_state, state_notes


VALID_OPPORTUNITY_STATES = {
    "no_opportunity",
    "watch_for_trigger",
    "probe_candidate",
    "tradeable_candidate",
    "risk_reduction_candidate",
}


def _sync_learning_impact_summary(signal: AnalystSignal, *, opportunity_state: str) -> Dict[str, Any]:
    summary = dict(getattr(signal, "learning_impact_summary", {}) or {})
    if not summary:
        summary = {
            "contract_version": "agentquant.analyst_learning_impact.v1",
            "historical_support": [],
            "historical_contradiction": [],
            "current_evidence_confirmed": [],
            "current_evidence_missing": [],
            "authority_boundary": "evidence_explanation_only_no_trade_authority_no_lots_no_margin_no_execution",
        }
    summary["opportunity_state"] = opportunity_state
    if not summary.get("opportunity_state_reason"):
        summary["opportunity_state_reason"] = (
            f"current structured evidence resolved opportunity_state={opportunity_state}; "
            "PM/Auditor/final_action_contract still decide any trade"
        )
    summary["authority_boundary"] = "evidence_explanation_only_no_trade_authority_no_lots_no_margin_no_execution"
    signal.learning_impact_summary = summary
    return summary


def apply_trade_research_contract(
    signal: AnalystSignal,
    quality_context: Dict[str, Any],
    *,
    analyst: str,
    trading_date: Any = None,
    ticker: str = "",
) -> AnalystSignal:
    """Attach the next-round research contract consumed by PM, Auditor, and memory."""
    metadata = getattr(signal, "metadata", {}) or {}
    tradeability = str(metadata.get("tradeability") or quality_context.get("tradeability") or "unknown")
    risk_flags = _clean_list(metadata.get("risk_flags") or quality_context.get("risk_flags"), max_items=8)
    conflicts = _clean_list(getattr(signal, "conflicting_factors", []), max_items=8)
    conflicts.extend(item for item in risk_flags if item not in conflicts)
    focus = _clean_list(getattr(signal, "factor_focus", []), max_items=8) or _factor_focus_from_context(quality_context, analyst)

    is_neutral = signal_value(signal.signal) == Signal.NEUTRAL.value
    input_trigger_valid = bool(getattr(signal, "trigger_valid", False))
    original_opportunity_state = str(
        getattr(signal, "opportunity_state", "") or ""
    ).strip().lower()
    is_risk_reduction = original_opportunity_state == "risk_reduction_candidate"
    derived_setup = _derive_analyst_trade_setup_fields(signal, quality_context, analyst)
    derived_invalidation = str(derived_setup.get("invalidation_condition") or "").strip()
    if derived_invalidation:
        metadata = {**metadata, "invalidation_condition": derived_invalidation}
    else:
        metadata = dict(metadata)
        metadata.pop("invalidation_condition", None)
    signal.metadata = metadata
    signal.entry_trigger = (
        str(getattr(signal, "entry_trigger", "") or "").strip()
        if has_concrete_entry_trigger(getattr(signal, "entry_trigger", ""))
        else ""
    )
    derived_notes = _clean_list(derived_setup.get("notes"), max_items=12)
    if derived_notes:
        existing_notes = _clean_list(getattr(signal, "setup_quality_notes", []), max_items=12)
        signal.setup_quality_notes = sorted(set(existing_notes + derived_notes))

    setup_presence = _trade_setup_contract_presence(signal)
    missing_setup_fields = _trade_setup_missing_fields(setup_presence)
    has_invalidation = setup_presence.get("invalidation_present", False)
    if is_risk_reduction:
        candidate_state = "risk_reduction_candidate"
    elif is_neutral:
        candidate_state = "no_opportunity"
    elif (
        tradeability == "high"
        and has_invalidation
        and setup_presence.get("entry_trigger_present", False)
        and float(getattr(signal, "business_quality_score", 0.0) or 0.0) >= 0.75
        and float(getattr(signal, "confidence", 0.0) or 0.0) >= 0.65
        and not _clean_list(conflicts, max_items=4)
    ):
        candidate_state = "tradeable_candidate"
    elif (
        tradeability == "high"
        and has_invalidation
        and setup_presence.get("entry_trigger_present", False)
        and float(getattr(signal, "business_quality_score", 0.0) or 0.0) >= 0.60
    ):
        candidate_state = "probe_candidate"
    elif tradeability in {"medium", "high"} and float(getattr(signal, "confidence", 0.0) or 0.0) >= 0.45:
        candidate_state = "watch_for_trigger"
    else:
        candidate_state = "watch_for_trigger"

    opportunity_type = getattr(signal, "opportunity_type", "unknown")
    if not opportunity_type or opportunity_type == "unknown":
        opportunity_type = infer_opportunity_type(signal, quality_context, analyst)

    setup_quality = _setup_quality_assessment(
        signal,
        quality_context,
        analyst=analyst,
        setup_presence=setup_presence,
        opportunity_type=opportunity_type,
        has_invalidation=has_invalidation,
    )
    entry_trigger = str(getattr(signal, "entry_trigger", "") or "").strip()
    pending_conditional_trigger = (
        _is_pending_conditional_entry_trigger(entry_trigger)
        and not _current_entry_confirmation_available(signal, quality_context, analyst)
    )
    current_trigger_confirmed = (
        False
        if is_neutral
        else _current_entry_trigger_confirmed(
            signal,
            quality_context,
            analyst,
            entry_trigger,
        )
    )
    if current_trigger_confirmed:
        pending_conditional_trigger = False
    exit_hint = (
        str(getattr(signal, "exit_hint", "") or "").strip()
        if _has_actionable_text(getattr(signal, "exit_hint", ""))
        else ""
    )
    holding_hint = getattr(signal, "holding_period_hint", "") or _holding_period_hint(signal)
    if is_risk_reduction:
        state_notes = ["risk_reduction_candidate_preserved"]
    else:
        candidate_state, state_notes = _resolve_opportunity_candidate_state(
            signal,
            quality_context,
            analyst=analyst,
            base_state=candidate_state,
            opportunity_type=opportunity_type,
            has_invalidation=has_invalidation,
        )
    if state_notes:
        conflicts = list(conflicts or [])
        for note in state_notes:
            if note not in conflicts:
                conflicts.append(note)
    if not is_neutral and not is_risk_reduction:
        setup_score = _safe_float(setup_quality.get("score"), 0.0)
        if setup_score < 0.42:
            if "setup_quality_poor_downgrade" not in state_notes:
                state_notes.append("setup_quality_poor_downgrade")
            if "setup_quality_poor_downgrade" not in conflicts:
                conflicts.append("setup_quality_poor_downgrade")
            candidate_state = "watch_for_trigger"
        elif setup_score < 0.60 and candidate_state == "tradeable_candidate":
            candidate_state = "probe_candidate"
            if "setup_quality_not_strong_enough_for_tradeable_candidate" not in state_notes:
                state_notes.append("setup_quality_not_strong_enough_for_tradeable_candidate")
    critical_missing_setup_fields = [
        field
        for field in missing_setup_fields
        if field in {"entry_trigger_present", "invalidation_present"}
    ]
    if not is_neutral and not is_risk_reduction and missing_setup_fields:
        missing_note = "missing_trade_setup_contract:" + ",".join(missing_setup_fields)
        if missing_note not in conflicts:
            conflicts.append(missing_note)
        if critical_missing_setup_fields:
            candidate_state = "no_opportunity"
            if "trade_setup_contract_incomplete_no_opportunity" not in state_notes:
                state_notes.append("trade_setup_contract_incomplete_no_opportunity")
    if pending_conditional_trigger and not is_risk_reduction:
        if "conditional_entry_trigger_pending" not in state_notes:
            state_notes.append("conditional_entry_trigger_pending")
        if "conditional_entry_trigger_pending" not in conflicts:
            conflicts.append("conditional_entry_trigger_pending")
        if candidate_state in {"tradeable_candidate", "probe_candidate"}:
            candidate_state = "watch_for_trigger"
    if (
        not is_neutral
        and not is_risk_reduction
        and not current_trigger_confirmed
        and candidate_state in {"tradeable_candidate", "probe_candidate"}
    ):
        candidate_state = "watch_for_trigger"
        if "current_entry_trigger_not_confirmed" not in state_notes:
            state_notes.append("current_entry_trigger_not_confirmed")
        if "current_entry_trigger_not_confirmed" not in conflicts:
            conflicts.append("current_entry_trigger_not_confirmed")
    if is_risk_reduction:
        candidate_state = "risk_reduction_candidate"
    elif is_neutral:
        counterfactual_side = str(
            getattr(signal, "counterfactual_side", "") or ""
        ).strip().lower()
        neutral_watch_complete = bool(
            counterfactual_side in {"long", "short"}
            and setup_presence.get("entry_trigger_present", False)
            and has_invalidation
            and not input_trigger_valid
        )
        candidate_state = (
            "watch_for_trigger" if neutral_watch_complete else "no_opportunity"
        )
        current_trigger_confirmed = False
        pending_conditional_trigger = neutral_watch_complete
        if neutral_watch_complete:
            state_notes.append("neutral_complete_counterfactual_watch")
        else:
            state_notes.append("neutral_incomplete_setup_no_opportunity")
    elif not setup_presence.get("entry_trigger_present", False) or not has_invalidation:
        candidate_state = "no_opportunity"
    elif not current_trigger_confirmed:
        candidate_state = "watch_for_trigger"
    action_evidence_contract = _build_action_evidence_contract(
        signal,
        quality_context,
        analyst=analyst,
        opportunity_type=opportunity_type,
        opportunity_state=candidate_state,
        entry_trigger=entry_trigger,
        exit_hint=exit_hint,
        has_invalidation=has_invalidation,
    )
    opportunity_state = candidate_state
    if pending_conditional_trigger and opportunity_state in {"probe_candidate", "tradeable_candidate"}:
        opportunity_state = "watch_for_trigger"
    action_evidence_contract["opportunity_state"] = opportunity_state
    signal.opportunity_type = opportunity_type
    signal.opportunity_state = opportunity_state
    signal.setup_quality_score = _safe_float(setup_quality.get("score"), 0.0)
    signal.entry_quality = str(setup_quality.get("entry_quality") or "unknown")
    signal.setup_quality_notes = sorted(set(list(setup_quality.get("notes") or []) + derived_notes))
    signal.entry_trigger = entry_trigger
    signal.exit_hint = exit_hint
    signal.holding_period_hint = holding_hint
    signal.evidence_role = {
        "technical": "entry_timing",
        "fundamental": "direction_context",
        "commodity_news": "event_catalyst",
    }.get(str(analyst), "risk_context")
    signal.direction_context = (
        "long" if signal_value(signal.signal) == Signal.BULLISH.value
        else "short" if signal_value(signal.signal) == Signal.BEARISH.value
        else "neutral"
    )
    technical_setup_scope = _technical_setup_scope(quality_context, opportunity_type)
    if str(analyst) == "technical":
        signal.trend_direction = str(
            quality_context.get("dominant_direction")
            or getattr(signal, "trend_stage", "")
            or signal.direction_context
            or "unknown"
        )
        setup_family = str(technical_setup_scope.get("setup_family") or opportunity_type or "unknown")
        signal.entry_timing_signal = (
            setup_family
            if setup_family in {"trend_breakout", "range_reversal", "volatility_breakout"}
            else "trend_watch_for_trigger"
        )
        signal.price_location = str(getattr(signal, "price_percentile", "") or "")
        signal.trigger_valid = bool(
            current_trigger_confirmed
            and has_invalidation
            and setup_presence.get("entry_trigger_present", False)
            and signal.entry_quality not in {"poor", "weak"}
        )
    else:
        signal.trend_direction = signal.direction_context
        signal.entry_timing_signal = (
            "event_requires_market_confirmation"
            if str(analyst) == "commodity_news"
            else "requires_technical_or_market_timing"
        )
        signal.price_location = str(getattr(signal, "price_percentile", "") or "")
        if str(analyst) == "commodity_news":
            signal.trigger_valid = bool(
                current_trigger_confirmed
            ) and bool(has_invalidation) and bool(
                setup_presence.get("entry_trigger_present", False)
            )
        else:
            signal.trigger_valid = bool(
                current_trigger_confirmed
                and has_invalidation
                and setup_presence.get("entry_trigger_present", False)
            )
    signal.invalidation_present = bool(has_invalidation)
    if pending_conditional_trigger or is_neutral:
        signal.trigger_valid = False
    action_evidence_contract["trigger_valid"] = bool(signal.trigger_valid)
    action_evidence_contract["current_trigger_confirmed"] = bool(current_trigger_confirmed)
    action_evidence_contract["invalidation_present"] = bool(signal.invalidation_present)
    action_evidence_contract["setup_quality_ok"] = bool(
        action_evidence_contract.get("setup_quality_ok")
        or signal.setup_quality_score >= 0.42
    )
    action_evidence_contract["setup_type"] = str(opportunity_type or action_evidence_contract.get("setup_type") or "unknown")
    fusion_evidence = build_analyst_fusion_evidence(
        signal,
        {**dict(quality_context or {}), "action_evidence_contract": action_evidence_contract},
        analyst=analyst,
        ticker=ticker,
    )
    action_evidence_contract["fusion_evidence"] = fusion_evidence
    action_evidence_contract["evidence_strength"] = fusion_evidence.get("evidence_strength")
    action_evidence_contract["evidence_freshness"] = fusion_evidence.get("evidence_freshness")
    action_evidence_contract["evidence_decay_risk"] = fusion_evidence.get("evidence_decay_risk")
    action_evidence_contract["confirmation_requirements"] = fusion_evidence.get("confirmation_requirements") or []
    action_evidence_contract["technical_false_breakout_risk"] = fusion_evidence.get("technical_false_breakout_risk")
    action_evidence_contract["fundamental_opposition_strength"] = fusion_evidence.get("fundamental_opposition_strength")
    action_evidence_contract["news_impact_window"] = fusion_evidence.get("news_impact_window")
    action_evidence_contract["one_off_event_risk"] = fusion_evidence.get("one_off_event_risk")
    signal.evidence_strength = str(fusion_evidence.get("evidence_strength") or "unknown")
    signal.evidence_freshness = str(fusion_evidence.get("evidence_freshness") or "unknown")
    signal.evidence_decay_risk = str(fusion_evidence.get("evidence_decay_risk") or "unknown")
    signal.confirmation_requirements = list(fusion_evidence.get("confirmation_requirements") or [])
    signal.technical_false_breakout_risk = str(fusion_evidence.get("technical_false_breakout_risk") or "not_applicable")
    signal.fundamental_opposition_strength = str(fusion_evidence.get("fundamental_opposition_strength") or "not_applicable")
    signal.news_impact_window = str(fusion_evidence.get("news_impact_window") or "")
    signal.one_off_event_risk = str(fusion_evidence.get("one_off_event_risk") or "not_applicable")
    product_context = {
        "ticker": str(ticker or ""),
        "sector": str(quality_context.get("sector") or ""),
        "analyst": str(analyst or ""),
        "setup_family": (action_evidence_contract.get("learning_scope") or {}).get("setup_family"),
        "market_regime": (action_evidence_contract.get("learning_scope") or {}).get("market_regime")
        or quality_context.get("market_regime"),
        "differentiation_role": (
            "technical_setup_selection"
            if analyst == "technical"
            else "fundamental_driver_selection"
            if analyst == "fundamental"
            else "news_catalyst_selection"
            if analyst == "commodity_news"
            else "generic_evidence"
        ),
    }
    research_contract = build_trade_research_contract(
        opportunity_type=opportunity_type,
        opportunity_state=opportunity_state,
        setup_type=str(opportunity_type or action_evidence_contract.get("setup_type") or "unknown"),
        setup_quality_ok=bool(action_evidence_contract.get("setup_quality_ok") or setup_quality.get("score", 0.0) >= 0.42),
        trigger_valid=bool(action_evidence_contract.get("trigger_valid")),
        invalidation_present=bool(has_invalidation),
        entry_trigger=entry_trigger,
        exit_hint=exit_hint,
        holding_period_hint=holding_hint,
        factor_focus=focus,
        current_evidence_conflict=conflicts,
        invalidation_level=getattr(signal, "invalidation_level", None),
        atr_stop_distance=getattr(signal, "atr_stop_distance", None),
        sample_state="current_day_evidence",
        maturity="candidate" if opportunity_state != "tradeable_candidate" else "validated",
        product_context=product_context,
    )
    research_errors = validate_trade_research_contract(research_contract)
    message_contract = build_internal_message_contract(
        agent=str(getattr(signal, "agent_name", "") or analyst),
        trading_date=trading_date,
        ticker=ticker,
        message_type="AnalystSignalArtifact",
        data_cutoff=str(getattr(signal, "data_cutoff", "") or "pre_open"),
        no_lookahead_status=str(getattr(signal, "no_lookahead_status", "") or "ok"),
        source_artifacts=getattr(signal, "source_artifacts", []) or [],
        validation_errors=research_errors,
    )
    message_errors = validate_internal_message_contract(message_contract)

    signal.factor_focus = focus
    signal.current_evidence_conflict = conflicts
    signal.research_contract_version = research_contract["contract_version"]
    signal.message_contract_version = message_contract["contract_version"]
    learning_impact_summary = _sync_learning_impact_summary(
        signal,
        opportunity_state=opportunity_state,
    )
    _sync_signal_fields_to_action_evidence_contract(
        action_evidence_contract,
        signal,
        metadata,
    )
    action_evidence_contract["setup_type"] = str(opportunity_type or "unknown")
    validation_errors = list(getattr(signal, "validation_errors", []) or [])
    for error in research_errors + message_errors:
        if error not in validation_errors:
            validation_errors.append(error)
    signal.validation_errors = validation_errors
    signal.metadata = {
        **metadata,
        "trade_research_contract": research_contract,
        "action_evidence_contract": action_evidence_contract,
        "internal_message_contract": message_contract,
        "research_contract_validation_errors": research_errors,
        "internal_message_validation_errors": message_errors,
        "tradeability": tradeability,
        "risk_flags": risk_flags,
        "opportunity_state_notes": state_notes,
        "opportunity_state": opportunity_state,
        "learning_impact_summary": learning_impact_summary,
        "fusion_evidence": fusion_evidence,
        "setup_quality": {
            "score": signal.setup_quality_score,
            "entry_quality": signal.entry_quality,
            "notes": signal.setup_quality_notes,
            "price_percentile": getattr(signal, "price_percentile", None),
            "not_product_rule": True,
        },
        "trade_setup_contract_status": {
            "presence": setup_presence,
            "missing_fields": missing_setup_fields,
            "critical_missing_fields": critical_missing_setup_fields,
            "non_neutral_requires_complete_setup": not is_neutral,
            "if_critical_missing_then_watch_for_trigger": True,
            "derived_setup_notes": derived_notes,
        },
        "learning_scope": action_evidence_contract.get("learning_scope") or {},
    }
    return apply_analyst_output_landing_check(signal)


def build_vote_summary(signals: Dict[str, Any]) -> Dict[str, Any]:
    counts = Counter()
    details = {}
    for name, value in signals.items():
        if isinstance(value, Signal):
            direction = signal_value(value)
            details[name] = direction
            counts[direction] += 1
    return {
        "counts": dict(counts),
        "details": details,
        "bullish_count": counts.get(Signal.BULLISH.value, 0),
        "bearish_count": counts.get(Signal.BEARISH.value, 0),
        "neutral_count": counts.get(Signal.NEUTRAL.value, 0),
    }


def build_technical_context(
    ticker: str,
    signal_results: Dict[str, Any],
    features: Dict[str, Any],
) -> Dict[str, Any]:
    votes = build_vote_summary(signal_results)
    bullish = int(votes["bullish_count"])
    bearish = int(votes["bearish_count"])
    neutral = int(votes["neutral_count"])
    conflict_count = min(bullish, bearish)
    dominant_direction = "neutral"
    if bullish > bearish and bullish >= 3:
        dominant_direction = "bullish"
    elif bearish > bullish and bearish >= 3:
        dominant_direction = "bearish"

    trend_strength = float(features.get("trend_strength") or 0.0)
    volatility = float(features.get("volatility") or 0.0)
    volume_ratio = float(features.get("volume_ratio") or 1.0)

    if volatility >= 0.35:
        regime = "high_volatility"
    elif trend_strength < 18:
        regime = "weak_trend"
    elif conflict_count >= 2:
        regime = "choppy"
    elif dominant_direction in {"bullish", "bearish"} and trend_strength >= 25:
        regime = "trend"
    else:
        regime = "range"

    aligned = max(bullish, bearish)
    required_aligned = 4
    if volatility >= 0.35 or conflict_count >= 2:
        required_aligned = 5
    risk_flags: List[str] = []
    if trend_strength < 18:
        risk_flags.append("weak_adx")
    if conflict_count >= 2:
        risk_flags.append("conflicting_indicators")
    if volatility >= 0.35:
        risk_flags.append("high_volatility")
    if volatility >= 0.35 and aligned < required_aligned:
        risk_flags.append("high_volatility_requires_extra_alignment")
    if volume_ratio < 0.8:
        risk_flags.append("weak_volume_confirmation")
    if dominant_direction == "bullish" and trend_strength < 22 and volume_ratio < 0.9:
        risk_flags.append("bullish_setup_lacks_trend_volume_confirmation")
    trend_continuation_setup_ok = (
        dominant_direction in {"bullish", "bearish"}
        and trend_strength >= 25
        and conflict_count <= 1
        and volume_ratio >= 0.8
        and aligned >= required_aligned
    )
    if regime in {"choppy", "range", "weak_trend", "high_volatility"} and not trend_continuation_setup_ok:
        risk_flags.append("trend_continuation_requires_breakout_confirmation")

    if aligned >= required_aligned and conflict_count <= 1 and trend_strength >= 20 and volume_ratio >= 0.8:
        tradeability = "high"
    elif aligned >= 3 and conflict_count <= 2 and trend_strength >= 18:
        tradeability = "medium"
    else:
        tradeability = "low"

    if volatility >= 0.35 and tradeability == "high" and trend_strength < 25:
        tradeability = "medium"
        risk_flags.append("high_volatility_caps_tradeability")
    if dominant_direction == "bullish" and trend_strength < 22 and volume_ratio < 0.9:
        tradeability = "low"

    range_reversal_setup_ok = (
        regime in {"range", "weak_trend", "choppy"}
        and dominant_direction in {"bullish", "bearish"}
        and conflict_count <= 2
        and aligned >= 3
        and volume_ratio >= 0.75
    )
    volatility_breakout_setup_ok = (
        regime == "high_volatility"
        and dominant_direction in {"bullish", "bearish"}
        and aligned >= required_aligned
        and conflict_count <= 1
        and volume_ratio >= 1.0
        and trend_strength >= 22
    )
    if trend_continuation_setup_ok:
        setup_family = "trend_breakout"
        required_confirmation = "trend/MACD/DI alignment plus volume or open-interest confirmation"
        invalidation_template = "price closes back inside the failed breakout area or opposite momentum/volume confirmation appears"
    elif range_reversal_setup_ok:
        setup_family = "range_reversal"
        required_confirmation = "RSI/Stochastic/mean-reversion signal aligns with nearby support/resistance and volume is not weak"
        invalidation_template = "price fails to hold the reversal area or resumes breakout against the reversal side"
    elif volatility_breakout_setup_ok:
        setup_family = "volatility_breakout"
        required_confirmation = "high-volatility breakout keeps direction with strong volume and no opposite technical vote"
        invalidation_template = "breakout fails on volume or closes back through the trigger zone"
    elif dominant_direction in {"bullish", "bearish"}:
        setup_family = "direction_watchlist"
        required_confirmation = "wait for current price/volume confirmation before any real position"
        invalidation_template = "opposite technical vote or failed trigger keeps the idea on watchlist"
    else:
        setup_family = "no_trade"
        required_confirmation = "no directional technical setup"
        invalidation_template = "not applicable"

    sector_policy = _technical_setup_policy(ticker)
    preferred_setups = set(str(item) for item in sector_policy.get("preferred_setups") or [])
    caution_setups = set(str(item) for item in sector_policy.get("caution_setups") or [])
    sector_setup_alignment = (
        "preferred"
        if setup_family in preferred_setups
        else "caution"
        if setup_family in caution_setups
        else "neutral"
    )
    if sector_setup_alignment == "caution" and setup_family not in {"direction_watchlist", "no_trade"}:
        risk_flags.append("sector_caution_setup_requires_stronger_confirmation")

    setup_quality_ok = bool(
        setup_family in {"trend_breakout", "range_reversal", "volatility_breakout"}
        and tradeability in {"medium", "high"}
    )
    opportunity_state = (
        "probe_candidate"
        if setup_quality_ok
        else "watch_for_trigger"
        if dominant_direction in {"bullish", "bearish"}
        else "no_opportunity"
    )

    return {
        "sector": get_sector(ticker),
        "sector_guidance": get_sector_guidance(ticker, "technical"),
        "sector_setup_policy": sector_policy,
        "sector_setup_alignment": sector_setup_alignment,
        "market_regime": regime,
        "dominant_direction": dominant_direction,
        "tradeability": tradeability,
        "setup_type": setup_family,
        "setup_quality_ok": setup_quality_ok,
        "required_confirmation": required_confirmation,
        "invalidation_condition": invalidation_template,
        "learning_scope": {
            "setup_family": setup_family,
            "sector_setup_alignment": sector_setup_alignment,
            "sector_preferred_setups": sorted(preferred_setups),
            "sector_caution_setups": sorted(caution_setups),
            "primary_confirmation": sector_policy.get("primary_confirmation") or [],
            "execution_focus": sector_policy.get("execution_focus"),
            "market_regime": regime,
        },
        "range_or_choppy_state": regime in {"choppy", "range", "weak_trend"},
        "indicator_votes": votes,
        "features": {
            "volatility": volatility,
            "trend_strength": trend_strength,
            "price_range": float(features.get("price_range") or 0.0),
            "volume_ratio": volume_ratio,
        },
        "risk_flags": risk_flags,
    }


def parse_fundamental_factors(fundamentals: str, metadata: Optional[Dict[str, Any]], ticker: str) -> Dict[str, Any]:
    role_to_group = {
        "inventory": "inventory",
        "supply": "supply",
        "demand": "demand",
        "trade_flow": "import_export",
        "cost_profit": "profit",
        "cost": "cost",
        "weather": "weather",
        "policy": "policy",
        "price_basis": "basis",
        "price_anchor": "basis",
        "macro_downstream": "macro_policy",
        "macro": "macro_policy",
        "context": "context",
    }
    groups: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    pattern = re.compile(
        r"^(?P<name>.+?):\s+.+?\[role:\s*(?P<role>[^;]+);\s*frequency:\s*(?P<freq>[^;]+);\s*last 5 obs trend:\s*(?P<trend>\w+)",
        re.IGNORECASE,
    )
    for line in fundamentals.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        role = match.group("role").strip()
        group = role_to_group.get(role, "context")
        groups[group].append(
            {
                "name": match.group("name").strip(),
                "role": role,
                "frequency": match.group("freq").strip(),
                "trend": match.group("trend").strip().lower(),
            }
        )

    quality = metadata or {}
    factor_judgment = quality.get("finoview_factor_judgment") or {}
    factor_snapshot = quality.get("finoview_factor_snapshot") or {}
    factor_attribution = quality.get("finoview_factor_attribution") or {}
    availability_audit = quality.get("local_finoview_availability_audit") or {}
    configured = int(quality.get("configured_indicator_count") or 0)
    loaded = int(quality.get("loaded_indicator_count") or 0)
    missing_like = int(quality.get("missing_like_count") or 0)
    if not missing_like:
        missing_like = (
            int(quality.get("missing_file_count") or 0)
            + int(quality.get("empty_frame_count") or 0)
            + int(quality.get("no_data_before_count") or 0)
        )
    stale = int(quality.get("stale_indicator_count") or 0)
    near_stale = int(quality.get("near_stale_indicator_count") or 0)
    low_confidence = int(quality.get("low_confidence_indicator_count") or 0)
    coverage_ratio = float(quality.get("coverage_ratio") or (loaded / configured if configured else 0.0))
    missing_ratio = float(quality.get("missing_ratio") or (missing_like / configured if configured else 0.0))
    stale_ratio = float(quality.get("stale_ratio") or (stale / configured if configured else 0.0))
    near_stale_ratio = float(quality.get("near_stale_ratio") or (near_stale / configured if configured else 0.0))

    risk_flags: List[str] = []
    if coverage_ratio < 0.65:
        risk_flags.append("low_coverage")
    if missing_ratio >= 0.25:
        risk_flags.append("missing_fundamental_inputs")
    if stale_ratio >= 0.20:
        risk_flags.append("stale_fundamental_inputs")
    if near_stale_ratio >= 0.30:
        risk_flags.append("near_stale_fundamental_inputs")
    if low_confidence > 0:
        risk_flags.append("low_confidence_inputs")
    excluded_stale = quality.get("excluded_stale_indicators") or []
    excluded_stale_count = int(quality.get("excluded_stale_indicator_count") or len(excluded_stale or []))
    if excluded_stale_count > 0:
        risk_flags.append("stale_fundamental_inputs_excluded_from_direction")
    if isinstance(availability_audit, dict) and not availability_audit.get("supports_fundamental_trade_setup", True):
        risk_flags.append("fundamental_local_data_not_enough_for_trade_setup")

    group_counts = {
        group: Counter(item["trend"] for item in items)
        for group, items in groups.items()
    }
    sector_factor_tree = _fundamental_factor_tree(ticker)
    directional_group_names = _group_names_with_direction(group_counts)
    primary_driver_groups = _intersect_ordered(
        sector_factor_tree.get("primary_drivers") or [],
        directional_group_names,
    )
    supporting_driver_groups = _intersect_ordered(
        sector_factor_tree.get("supporting_drivers") or [],
        directional_group_names,
    )
    short_trigger_groups = _intersect_ordered(
        sector_factor_tree.get("short_trigger_groups") or [],
        directional_group_names,
    )
    risk_driver_groups = _intersect_ordered(
        sector_factor_tree.get("risk_drivers") or [],
        list(group_counts.keys()),
    )
    directional_groups = 0
    conflicting_groups = 0
    conflict_groups: List[str] = []
    for group, counts in group_counts.items():
        if counts.get("up", 0) or counts.get("down", 0):
            directional_groups += 1
        if counts.get("up", 0) and counts.get("down", 0):
            conflicting_groups += 1
            conflict_groups.append(str(group))

    factor_tradable = bool(factor_judgment.get("tradable_coverage"))
    if risk_flags and ("low_coverage" in risk_flags or "stale_fundamental_inputs" in risk_flags):
        tradeability = "low"
    elif factor_tradable and directional_groups >= 2 and conflicting_groups <= 1:
        tradeability = "high"
    elif directional_groups >= 3 and conflicting_groups <= 1:
        tradeability = "high"
    elif directional_groups >= 2 and conflicting_groups <= 2:
        tradeability = "medium"
    else:
        tradeability = "low"

    return {
        "sector": get_sector(ticker),
        "sector_guidance": get_sector_guidance(ticker, "fundamental"),
        "sector_factor_tree": sector_factor_tree,
        "fundamental_driver_roles": {
            "primary_driver_groups": primary_driver_groups,
            "supporting_driver_groups": supporting_driver_groups,
            "risk_driver_groups": risk_driver_groups,
            "short_trigger_groups": short_trigger_groups,
            "conflict_groups": sorted(set(conflict_groups)),
            "directional_group_names": directional_group_names,
        },
        "factor_groups": {group: items[:8] for group, items in groups.items()},
        "factor_group_counts": {group: dict(counts) for group, counts in group_counts.items()},
        "finoview_factor_snapshot": factor_snapshot,
        "finoview_factor_judgment": factor_judgment,
        "finoview_factor_attribution": factor_attribution,
        "local_finoview_availability_audit": availability_audit,
        "data_quality": {
            "configured": configured,
            "loaded": loaded,
            "coverage_ratio": coverage_ratio,
            "missing_ratio": missing_ratio,
            "stale_ratio": stale_ratio,
            "near_stale_ratio": near_stale_ratio,
            "low_confidence_count": low_confidence,
            "excluded_stale_indicator_count": excluded_stale_count,
            "excluded_stale_indicators": excluded_stale[:12] if isinstance(excluded_stale, list) else [],
            "factor_coverage_score": float(factor_judgment.get("coverage_score") or 0.0),
            "usable_factor_count": int(factor_judgment.get("usable_factor_count") or 0),
            "usable_coverage_score": float(factor_judgment.get("usable_coverage_score") or 0.0),
            "factor_freshness_score": float(factor_judgment.get("freshness_score") or 0.0),
            "no_lookahead_status": factor_judgment.get("no_lookahead_status", "unchecked"),
            "local_finoview_coverage_status": (
                availability_audit.get("coverage_status")
                if isinstance(availability_audit, dict)
                else None
            ),
            "supports_fundamental_trade_setup": bool(
                availability_audit.get("supports_fundamental_trade_setup", False)
            )
            if isinstance(availability_audit, dict)
            else False,
        },
        "basis": quality.get("basis"),
        "tradeability": tradeability,
        "learning_scope": {
            "factor_tree": get_sector(ticker),
            "primary_driver_groups": primary_driver_groups,
            "short_trigger_groups": short_trigger_groups,
        },
        "risk_flags": risk_flags,
    }


_PANDAAI_FACTOR_GROUPS = {
    "basis": "price_basis",
    "warehouse_receipt": "inventory",
    "net_flow": "capital_flow",
    "variety_position_rank": "positioning",
    "symbol_position_rank": "positioning",
    "ls_ratio": "sentiment_positioning",
    "broker_net_margin_change": "capital_flow",
    "broker_net_margin": "capital_flow",
    "netposi_rank": "positioning",
    "net_cap_change": "capital_flow",
    "contract_daily_indicators": "sentiment_positioning",
    "contract_rank": "sentiment_positioning",
    "broker_variety_profit": "broker_profit",
}


def summarize_pandaai_extra_factors(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize PandaAI futures non-market data as business factor evidence."""
    if not isinstance(snapshot, dict):
        return {
            "enabled": False,
            "tradeability": "low",
            "direction_hint": "neutral",
            "features": [],
            "factor_group_counts": {},
            "record_counts": {},
            "data_missing": [],
            "feature_status": {},
            "feature_diagnostics": {},
            "data_status_groups": {},
            "errors": [],
        }

    records = snapshot.get("records") or {}
    record_counts = snapshot.get("record_counts") or {}
    feature_status = dict(snapshot.get("feature_status") or {})
    features = score_pandaai_extra_records(records)
    directional = [item for item in features if item.get("direction") in {"long", "short"}]
    long_features = [item["feature"] for item in directional if item.get("direction") == "long"]
    short_features = [item["feature"] for item in directional if item.get("direction") == "short"]
    avg_score = (
        sum(float(item.get("score") or 0.0) for item in directional) / len(directional)
        if directional
        else 0.0
    )
    if avg_score > 0.10:
        direction_hint = "long"
    elif avg_score < -0.10:
        direction_hint = "short"
    else:
        direction_hint = "neutral"

    group_counts: Dict[str, int] = {}
    for item in features:
        group = _PANDAAI_FACTOR_GROUPS.get(str(item.get("feature")), "context")
        group_counts[group] = group_counts.get(group, 0) + 1

    normalized_status: Dict[str, str] = {}
    for key, value in (record_counts or {}).items():
        try:
            count = int(value or 0)
        except Exception:
            count = 0
        normalized_status[key] = "ok" if count > 0 else str(feature_status.get(key) or "no_data")
    for key, value in feature_status.items():
        normalized_status.setdefault(str(key), str(value or "unknown"))
    data_missing = [key for key, status in normalized_status.items() if status not in {"ok", "unsupported_feature"}]
    data_status_groups: Dict[str, List[str]] = {}
    for key, status in sorted(normalized_status.items()):
        data_status_groups.setdefault(status, []).append(key)
    errors = snapshot.get("errors") or []
    if len(directional) >= 3 and len(group_counts) >= 3 and len(errors) == 0:
        tradeability = "high"
    elif len(directional) >= 1 and len(errors) <= 2:
        tradeability = "medium"
    else:
        tradeability = "low"

    return {
        "enabled": True,
        "source": "PandaAI",
        "info_cutoff": "T-1_or_earlier",
        "underlying_code": snapshot.get("underlying_code"),
        "contract_symbol": snapshot.get("contract_symbol"),
        "reference_date": snapshot.get("reference_date"),
        "lookback_days": snapshot.get("lookback_days"),
        "tradeability": tradeability,
        "direction_hint": direction_hint,
        "avg_score": avg_score,
        "long_features": long_features,
        "short_features": short_features,
        "features": features,
        "factor_group_counts": group_counts,
        "record_counts": record_counts,
        "data_missing": data_missing,
        "feature_status": normalized_status,
        "feature_diagnostics": snapshot.get("feature_diagnostics") or {},
        "data_status_groups": data_status_groups,
        "parameter_errors": data_status_groups.get("parameter_error", []),
        "no_data": data_status_groups.get("no_data", []),
        "unsupported_features": data_status_groups.get("unsupported_feature", []),
        "errors": errors,
    }


NEWS_EVENT_RULES = {
    "supply": ["产量", "供应", "开工", "检修", "装置", "减产", "增产", "发运", "到港", "矿山"],
    "demand": ["需求", "消费", "成交", "订单", "开工率", "基建", "地产", "饲料", "纺织", "聚酯"],
    "inventory": ["库存", "累库", "去库", "仓单", "库容"],
    "policy": ["政策", "关税", "配额", "限产", "补贴", "监管", "收储", "抛储"],
    "import_export": ["进口", "出口", "到港", "船货", "巴西", "印尼", "马来", "澳洲"],
    "weather": ["天气", "降雨", "干旱", "洪水", "寒潮", "霜冻", "台风"],
    "overseas": ["LME", "美元", "美联储", "海外", "外盘", "国际"],
    "macro": ["宏观", "利率", "汇率", "PMI", "通胀", "美元", "经济"],
}

BULLISH_NEWS_WORDS = ["减产", "去库", "下降", "收紧", "短缺", "上涨", "检修", "限产", "需求改善", "进口减少"]
BEARISH_NEWS_WORDS = ["增产", "累库", "上升", "宽松", "下跌", "进口增加", "需求疲弱", "库存增加", "供应增加"]
STRONG_EVENT_WORDS = ["大幅", "显著", "紧张", "短缺", "创", "暴跌", "暴涨", "政策", "限产", "检修"]


def _parse_date(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def summarize_news_events(news_items: Iterable[Any], ticker: str, trading_date: Any = None) -> Dict[str, Any]:
    seen_titles = set()
    events = []
    direction_counts = Counter()
    type_counts = Counter()
    latest_event_dt: Optional[datetime] = None
    trading_dt = _parse_date(trading_date)
    sector_policy = _news_catalyst_policy(ticker)
    tradable_event_types = set(str(item) for item in sector_policy.get("tradable_catalysts") or [])
    risk_event_types = set(str(item) for item in sector_policy.get("risk_events") or [])
    noise_event_types = set(str(item) for item in sector_policy.get("noise_events") or [])
    for item in news_items:
        title = getattr(item, "title", "") or ""
        content = getattr(item, "content", "") or ""
        publish_time = getattr(item, "publish_time", "") or ""
        publish_dt = _parse_date(publish_time)
        if publish_dt is not None and (latest_event_dt is None or publish_dt > latest_event_dt):
            latest_event_dt = publish_dt
        normalized_title = re.sub(r"\s+", " ", title).strip()
        if not normalized_title or normalized_title in seen_titles:
            continue
        seen_titles.add(normalized_title)
        text = f"{title} {content}"
        event_types = [
            event_type
            for event_type, keywords in NEWS_EVENT_RULES.items()
            if any(keyword.lower() in text.lower() for keyword in keywords)
        ] or ["context"]
        bullish_hits = sum(1 for word in BULLISH_NEWS_WORDS if word in text)
        bearish_hits = sum(1 for word in BEARISH_NEWS_WORDS if word in text)
        if bullish_hits > bearish_hits:
            direction = "bullish"
        elif bearish_hits > bullish_hits:
            direction = "bearish"
        else:
            direction = "neutral"
        strength = "strong" if any(word in text for word in STRONG_EVENT_WORDS) else "medium" if direction != "neutral" else "weak"
        for event_type in event_types:
            type_counts[event_type] += 1
        direction_counts[direction] += 1
        events.append(
            {
                "title": normalized_title,
                "publish_time": str(publish_time),
                "event_types": event_types,
                "sector_tradable_event_match": bool(set(event_types) & tradable_event_types),
                "sector_risk_event_match": bool(set(event_types) & risk_event_types),
                "sector_noise_event_match": bool(set(event_types) & noise_event_types),
                "direction_hint": direction,
                "strength": strength,
            }
        )

    latest_news_date = latest_event_dt.strftime("%Y-%m-%d") if latest_event_dt is not None else None
    age_days = None
    if trading_dt is not None and latest_event_dt is not None:
        age_days = max(0, (trading_dt.date() - latest_event_dt.date()).days)
    if age_days is None:
        freshness_score = 1.0 if events else 0.0
    elif age_days <= 2:
        freshness_score = 1.0
    elif age_days <= 5:
        freshness_score = 0.7
    elif age_days <= 10:
        freshness_score = 0.4
    else:
        freshness_score = 0.1

    mixed = bool(direction_counts.get("bullish", 0) and direction_counts.get("bearish", 0))
    strong_direction = max(direction_counts.get("bullish", 0), direction_counts.get("bearish", 0))
    strong_event_count = sum(1 for item in events if item.get("strength") == "strong")
    directional_event_count = direction_counts.get("bullish", 0) + direction_counts.get("bearish", 0)
    sector_tradable_event_count = sum(1 for item in events if item.get("sector_tradable_event_match"))
    sector_risk_event_count = sum(1 for item in events if item.get("sector_risk_event_match"))
    risk_flags: List[str] = []
    if not events:
        risk_flags.append("no_news")
    if age_days is not None and age_days > 5:
        risk_flags.append("stale_news_window")
    if mixed:
        risk_flags.append("mixed_news_direction")
    if strong_direction == 0:
        risk_flags.append("no_directional_news")
    if len(events) <= 2:
        risk_flags.append("thin_news_sample")
    if strong_event_count <= 0 and directional_event_count > 0:
        risk_flags.append("news_direction_lacks_strong_catalyst")

    event_window_days = int(sector_policy.get("event_window_days") or 3)
    fresh_enough_for_event = freshness_score >= 0.7 and (age_days is None or age_days <= event_window_days)
    tradable_event = (
        strong_direction >= 1
        and strong_event_count >= 1
        and sector_tradable_event_count >= 1
        and not mixed
        and fresh_enough_for_event
    )
    if not fresh_enough_for_event and events:
        tradeability = "low"
    elif tradable_event and strong_direction >= 3:
        tradeability = "high"
    elif tradable_event or (strong_direction >= 2 and len(risk_flags) <= 1):
        tradeability = "medium"
    else:
        tradeability = "low"
    event_regime = (
        "mixed_event"
        if mixed
        else "tradable_catalyst" if tradable_event
        else "directional_noise" if directional_event_count > 0
        else "no_trade_news"
    )
    catalyst_classification = {
        "tradable_catalyst": bool(tradable_event),
        "background_news": bool(events and not tradable_event and not mixed),
        "risk_event": bool(sector_risk_event_count > 0 or mixed),
        "stale_or_noise": bool((age_days is not None and age_days > event_window_days) or not events),
        "conflict_event": bool(mixed),
        "sector_tradable_event_count": sector_tradable_event_count,
        "sector_risk_event_count": sector_risk_event_count,
        "event_window_days": event_window_days,
    }

    return {
        "sector": get_sector(ticker),
        "sector_guidance": get_sector_guidance(ticker, "news"),
        "sector_catalyst_policy": sector_policy,
        "catalyst_classification": catalyst_classification,
        "events": events[:10],
        "event_type_counts": dict(type_counts),
        "direction_counts": dict(direction_counts),
        "freshness_score": freshness_score,
        "latest_news_date": latest_news_date,
        "news_age_days": age_days,
        "relevance_score": 0.8 if events else 0.0,
        "strong_event_count": strong_event_count,
        "directional_event_count": directional_event_count,
        "event_regime": event_regime,
        "tradable_event": tradable_event,
        "price_reaction_required": True,
        "price_reaction_confirmed": False,
        "tradeability": tradeability,
        "learning_scope": {
            "event_regime": event_regime,
            "event_type_counts": dict(type_counts),
            "catalyst_classification": catalyst_classification,
        },
        "risk_flags": risk_flags,
    }


def apply_signal_quality_gate(
    signal: AnalystSignal,
    quality_context: Dict[str, Any],
    full_config: Dict[str, Any],
    analyst: str,
) -> AnalystSignal:
    cfg = get_analyst_llm_config(full_config, analyst)
    tradeability = str(quality_context.get("tradeability") or "medium")
    risk_flags = list(quality_context.get("risk_flags") or [])
    original_signal = signal_value(signal.signal)
    original_confidence = float(signal.confidence or 0.0)
    reason = None

    data_quality = quality_context.get("data_quality") if isinstance(quality_context.get("data_quality"), dict) else {}
    stale_ratio = _safe_float(data_quality.get("stale_ratio"), 0.0)
    freshness_score = _safe_float(data_quality.get("factor_freshness_score"), 0.0)
    stale_fundamental_direction = (
        str(analyst) == "fundamental"
        and original_signal != Signal.NEUTRAL.value
        and cfg.get("force_neutral_stale_fundamental", True)
        and (
            "stale_fundamental_inputs" in risk_flags
            or stale_ratio >= float(cfg.get("stale_fundamental_stale_ratio", 0.35))
            or (freshness_score > 0 and freshness_score < float(cfg.get("stale_fundamental_freshness_score", 0.45)))
        )
    )
    if stale_fundamental_direction:
        signal.signal = Signal.NEUTRAL
        signal.confidence = min(original_confidence, _safe_float(cfg.get("cap_stale_fundamental_confidence"), 0.30))
        reason = (
            "stale fundamental inputs; forced Neutral until fresh supply-demand evidence confirms; "
            f"stale_ratio={stale_ratio:.2f}; freshness_score={freshness_score:.2f}; flags={risk_flags}"
        )
        signal.neutral_reason = signal.neutral_reason or "stale fundamental inputs; directional anchor withheld"
        missing = list(getattr(signal, "missing_evidence", []) or [])
        for item in ("fresh supply-demand anchor", "fresh inventory/basis confirmation", "current positioning flow"):
            if item not in missing:
                missing.append(item)
        signal.missing_evidence = missing
        conflicts = list(getattr(signal, "conflicting_factors", []) or [])
        if "stale_fundamental_inputs" not in conflicts:
            conflicts.append("stale_fundamental_inputs")
        signal.conflicting_factors = conflicts
        signal.do_not_trade_reason = signal.do_not_trade_reason or "fundamental_direction_blocked_by_stale_data"

    if tradeability == "low":
        signal.confidence = min(float(signal.confidence or original_confidence), cfg["cap_low_tradeability_confidence"])
        if cfg["force_neutral_low_tradeability"]:
            signal.signal = Signal.NEUTRAL
            reason = reason or f"low tradeability; forced Neutral; flags={risk_flags}"
        else:
            reason = reason or f"low tradeability; confidence capped; flags={risk_flags}"
    elif tradeability == "medium":
        signal.confidence = min(float(signal.confidence or original_confidence), cfg["cap_medium_tradeability_confidence"])
        reason = reason or "medium tradeability; confidence capped"

    if reason:
        signal.justification += (
            f"\n[Analyst quality gate: {original_signal}/{original_confidence:.2f} -> "
            f"{signal_value(signal.signal)}/{signal.confidence:.2f}; {reason}]"
        )

    if stale_fundamental_direction and "stale_fundamental_direction_block" not in risk_flags:
        risk_flags.append("stale_fundamental_direction_block")
    signal.metadata = {
        **(getattr(signal, "metadata", {}) or {}),
        "tradeability": tradeability,
        "risk_flags": risk_flags,
        "quality_gate": {
            "original_signal": original_signal,
            "original_confidence": original_confidence,
            "final_signal": signal_value(signal.signal),
            "final_confidence": float(signal.confidence or 0.0),
            "stale_fundamental_direction_block": stale_fundamental_direction,
            "data_quality": data_quality,
        },
    }
    return signal


def _format_json_block(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def write_analyst_report(
    *,
    analyst: str,
    ticker: str,
    trading_date: Any,
    signal: AnalystSignal,
    full_config: Dict[str, Any],
) -> Optional[str]:
    cfg = get_analyst_llm_config(full_config, analyst)
    if not cfg.get("write_decision_reports", True):
        return None
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    contract = validate_action_evidence_contract(
        metadata.get("action_evidence_contract"),
        analyst=analyst,
    )

    trading_date_value = normalize_trading_date(trading_date)
    report_dir = Path(logger.log_dir) / "analyst_decisions" / logger.run_id
    report_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{trading_date_value}_{str(ticker).upper()}_{analyst}.md"
    path = report_dir / file_name

    title = analyst.replace("_", " ").title()
    lines = [
        f"# {title} Action Evidence Contract",
        "",
        f"Ticker: {ticker}",
        f"Trading Date: {trading_date_value}",
        "",
        "```json",
        sanitize_visible_text(_format_json_block(contract)),
        "```",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")
    rel_path = os.path.relpath(path, logger.log_dir)
    logger.info(
        f"[ANALYST SUMMARY] {ticker} {analyst} signal={signal_value(signal.signal)} "
        f"confidence={float(signal.confidence or 0.0):.2f} "
        f"report=logs/{rel_path}"
    )
    return str(path)


def format_technical_summary_for_prompt(context: Dict[str, Any]) -> str:
    return (
        "=== Structured Technical Precheck ===\n"
        f"Sector: {context.get('sector')}\n"
        f"Sector guidance: {context.get('sector_guidance')}\n"
        f"Market regime: {context.get('market_regime')}\n"
        f"Dominant direction: {context.get('dominant_direction')}\n"
        f"Tradeability: {context.get('tradeability')}\n"
        f"Indicator votes: {_format_json_block(context.get('indicator_votes'))}\n"
        f"Market features: {_format_json_block(context.get('features'))}\n"
        f"Risk flags: {', '.join(context.get('risk_flags') or []) or 'none'}\n"
        "Decision rule: do not force a directional signal. If tradeability is low, prefer Neutral.\n"
    )


def format_fundamental_summary_for_prompt(context: Dict[str, Any]) -> str:
    return (
        "\n\n=== Structured Fundamental Precheck ===\n"
        f"Sector: {context.get('sector')}\n"
        f"Sector guidance: {context.get('sector_guidance')}\n"
        f"Tradeability: {context.get('tradeability')}\n"
        f"Data quality: {_format_json_block(context.get('data_quality'))}\n"
        f"Factor group counts: {_format_json_block(context.get('factor_group_counts'))}\n"
        f"Basis: {_format_json_block(context.get('basis'))}\n"
        f"PandaAI extra factor context: {_format_json_block(context.get('pandaai_extra_factors'))}\n"
        f"Risk flags: {', '.join(context.get('risk_flags') or []) or 'none'}\n"
        "Stale-data discipline: indicators listed as excluded_stale_indicators are audit context only; "
        "do not use them as bullish/bearish evidence or as a tradeable setup trigger.\n"
        "Directional discipline: require at least two independent factor groups supporting a direction. "
        "If factors or data quality are weak, prefer Neutral.\n"
    )


def format_news_summary_for_prompt(context: Dict[str, Any]) -> str:
    return (
        "\n\n=== Structured News Event Precheck ===\n"
        f"Sector: {context.get('sector')}\n"
        f"Sector guidance: {context.get('sector_guidance')}\n"
        f"Tradeability: {context.get('tradeability')}\n"
        f"Freshness score: {context.get('freshness_score')}\n"
        f"Latest news date: {context.get('latest_news_date')}\n"
        f"News age days: {context.get('news_age_days')}\n"
        f"Relevance score: {context.get('relevance_score')}\n"
        f"Event type counts: {_format_json_block(context.get('event_type_counts'))}\n"
        f"Direction counts: {_format_json_block(context.get('direction_counts'))}\n"
        f"Risk flags: {', '.join(context.get('risk_flags') or []) or 'none'}\n"
        f"Events: {_format_json_block(context.get('events'))}\n"
        "News discipline: separate informative news from tradable news. "
        "If events are stale, mixed, weak, or weakly related, prefer Neutral.\n"
    )
