from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tools.common.execution_trigger_semantics import (
    canonical_entry_invalidation_condition,
    canonical_entry_trigger,
)


def build_test_aec(
    analyst: str,
    *,
    ticker: str = "BU",
    trading_date: str = "2025-03-25",
    signal: str = "Neutral",
    side: str = "flat",
    confidence: float = 0.5,
    opportunity_type: str | None = None,
    opportunity_state: str | None = None,
    setup_type: str | None = None,
    setup_quality_ok: bool | None = None,
    trigger_valid: bool = False,
    current_trigger_confirmed: bool = False,
    invalidation_present: bool | None = None,
    entry_trigger: str | None = None,
    invalidation_condition: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    directional = side in {"long", "short"}
    state = opportunity_state or (
        "tradeable_candidate"
        if analyst == "technical" and directional and trigger_valid
        else "watch_for_trigger"
        if analyst == "technical" and directional
        else "no_opportunity"
    )
    source_name, source, dataset = {
        "technical": ("pandaai_market", "PandaAI", "daily_continuous_candles"),
        "fundamental": (
            "finoview_fundamental",
            "Finoview",
            "local_feather_fundamental",
        ),
        "commodity_news": ("finoview_news_txt", "Finoview", "local_news_txt"),
    }[analyst]
    role = {
        "technical": "entry_timing",
        "fundamental": "direction_context",
        "commodity_news": "event_catalyst",
    }[analyst]
    timing = (
        "breakout"
        if analyst == "technical"
        and directional
        and state in {"watch_for_trigger", "probe_candidate", "tradeable_candidate"}
        else "event_immediate"
        if analyst == "commodity_news"
        and directional
        and state in {"probe_candidate", "tradeable_candidate"}
        else ""
    )
    has_invalidation = bool(timing) if invalidation_present is None else invalidation_present
    default_trigger = canonical_entry_trigger(timing, side) if timing else ""
    contract: dict[str, Any] = {
        "contract_version": "agentquant.action_evidence.v1",
        "analyst": analyst,
        "sector": "test",
        "signal": signal,
        "side": side,
        "confidence": confidence,
        "opportunity_type": opportunity_type or ("trend_continuation" if directional else "no_trade"),
        "opportunity_state": state,
        "setup_type": setup_type or ("trend_continuation" if directional else "no_trade"),
        "setup_quality_ok": directional if setup_quality_ok is None else setup_quality_ok,
        "trigger_valid": trigger_valid,
        "current_trigger_confirmed": current_trigger_confirmed,
        "entry_trigger": entry_trigger if entry_trigger is not None else default_trigger,
        "entry_timing_signal": timing,
        "evidence_role": role,
        "exit_hint": "close_beyond_invalidation" if directional else "",
        "invalidation_present": has_invalidation,
        "invalidation_level": (
            95.0 if side == "long" else 105.0 if side == "short" else None
        ) if has_invalidation else None,
        "position_invalidation_level": (
            94.0 if side == "long" else 106.0 if side == "short" else None
        ) if directional else None,
        "horizon_class": "short" if directional else "flat",
        "expected_horizon_days": 3 if directional else 0,
        "market_regime": "trend" if directional else "unknown",
        "evidence_quality": "high" if directional else "low",
        "evidence_strength": "strong" if directional else "weak",
        "evidence_freshness": "fresh",
        "confirmation_requirements": [],
        "current_evidence_conflict": [],
        "missing_evidence": [],
        "factor_focus": ["test_evidence"],
        "no_lookahead_status": "ok",
        "data_usage_summary": {
            "ticker": ticker.upper(),
            "trading_date": trading_date[:10],
            "analyst": analyst,
            "sources": {
                source_name: {
                    "source": source,
                    "dataset": dataset,
                    "available": True,
                    "used_in_signal": True,
                    "pre_open_only": True,
                    "info_cutoff": "pre_open",
                }
            },
        },
        "learning_scope": {},
        "product_profile_evidence": {},
        "fusion_evidence": {
            "evidence_strength": "strong" if directional else "weak",
            "evidence_freshness": "fresh",
            "confirmation_requirements": [],
        },
    }
    if has_invalidation:
        contract["invalidation_condition"] = canonical_entry_invalidation_condition(
            timing,
            side,
        )
    if extra:
        contract.update(extra)
    return contract


def build_test_signal(
    analyst: str,
    *,
    signal_record_id: str,
    ticker: str = "BU",
    trading_date: str = "2025-03-25",
    **aec_kwargs: Any,
) -> SimpleNamespace:
    return SimpleNamespace(
        agent_name=analyst,
        metadata={
            "action_evidence_contract": build_test_aec(
                analyst,
                ticker=ticker,
                trading_date=trading_date,
                **aec_kwargs,
            ),
            "signal_record_id": signal_record_id,
        },
    )
