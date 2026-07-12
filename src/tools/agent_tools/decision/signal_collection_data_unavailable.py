"""Deterministic data-unavailable signal package for signal_collector.

This module belongs to the signal_collector boundary.  It creates structured
no-trade evidence when required pre-open reference data is unavailable; it does
not create PM decisions, lots, ranks, capital deployment, or final contracts.
"""

from __future__ import annotations

from typing import Any, Iterable

from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.common.signal_evidence_collection import build_signal_collection_contract


def _normalize_analyst_name(name: Any) -> str:
    return str(name or "").strip()


def _enabled_analysts(enabled_analysts: Iterable[Any] | None) -> list[str]:
    names = [_normalize_analyst_name(name) for name in (enabled_analysts or [])]
    names = [name for name in names if name]
    return names or ["technical", "fundamental", "commodity_news"]


def _data_unavailable_signal(*, ticker: str, analyst: str, reason: str, warning_message: str | None) -> AnalystSignal:
    action_evidence_contract = {
        "contract_version": "agentquant.action_evidence.v1",
        "analyst": analyst,
        "side": "flat",
        "signal": "Neutral",
        "confidence": 0.0,
        "opportunity_type": "no_trade",
        "opportunity_state": "no_opportunity",
        "setup_type": "data_unavailable_no_trade",
        "setup_quality_ok": False,
        "trigger_valid": False,
        "current_trigger_confirmed": False,
        "invalidation_present": False,
        "entry_trigger": "none",
        "exit_hint": "none",
        "horizon_class": "flat",
        "evidence_quality": "low",
        "evidence_strength": "weak",
        "missing_evidence": ["pre_open_reference_price"],
        "confirmation_requirements": ["valid_pre_open_reference_price"],
        "current_evidence_conflict": [],
        "no_lookahead_status": "ok",
        "data_usage_summary": {
            "ticker": ticker,
            "analyst": analyst,
            "pandaai_pre_open_reference": {
                "available": False,
                "used_in_signal": True,
                "reason": reason,
            },
            "missing_data": ["pre_open_reference_price"],
            "data_quality_flags": ["pre_open_reference_price_unavailable"],
        },
    }
    trade_research_contract = {
        "contract_version": "agentquant.research.v1",
        "opportunity_type": "no_trade",
        "opportunity_state": "no_opportunity",
        "setup_type": "data_unavailable_no_trade",
        "setup_quality_ok": False,
        "trigger_valid": False,
        "invalidation_present": False,
        "entry_trigger": "none",
        "exit_hint": "none",
        "holding_period_hint": "flat",
        "factor_focus": ["pandaai_market_data"],
        "current_evidence_conflict": [],
        "invalidation_level": None,
        "sample_state": "current_day_evidence",
        "maturity": "data_unavailable",
    }
    return AnalystSignal(
        agent_name=analyst,
        signal=Signal.NEUTRAL,
        confidence=0.0,
        justification=(
            f"{ticker} cannot form a tradable Phase1 setup because the pre-open "
            f"reference price is unavailable: {reason}"
        ),
        data_cutoff="pre_open",
        no_lookahead_status="ok",
        determinism_mode="deterministic_signal_collector_data_gate",
        source_artifacts=["data_quality:pre_open_reference_price"],
        horizon_class="flat",
        analyst_horizon="flat",
        decision_horizon="flat",
        execution_horizon="flat",
        validation_horizon="flat",
        expected_horizon_days=0,
        market_regime="unknown",
        setup_type="data_unavailable_no_trade",
        data_freshness="missing",
        evidence_quality="low",
        evidence_strength="weak",
        evidence_freshness="stale",
        business_quality_score=0.0,
        data_coverage_score=0.0,
        tradeability_reason="pre_open_reference_price_unavailable",
        opportunity_type="no_trade",
        opportunity_state="no_opportunity",
        setup_quality_score=0.0,
        entry_quality="poor",
        setup_quality_notes=["pre_open_reference_price_unavailable"],
        entry_trigger="none",
        exit_hint="none",
        holding_period_hint="flat",
        trigger_valid=False,
        invalidation_present=False,
        factor_focus=["pandaai_market_data"],
        neutral_reason="pre_open_reference_price_unavailable",
        missing_evidence=["pre_open_reference_price"],
        would_change_view_if="PandaAI returns a valid previous trading day close for Phase1 planning",
        neutral_opportunity_bucket="low_tradeability",
        neutral_trigger_condition="valid_pre_open_reference_price",
        counterfactual_side="flat",
        neutral_watchlist_priority="none",
        do_not_trade_reason="pre_open_reference_price_unavailable",
        metadata={
            "action_evidence_contract": action_evidence_contract,
            "trade_research_contract": trade_research_contract,
            "data_usage_summary": action_evidence_contract["data_usage_summary"],
            "no_trade_reason": "pre_open_reference_price_unavailable",
            "no_trade_category": "data",
            "signal_collector_contract": "data_unavailable_no_trade",
            "warning_message": warning_message,
        },
    )


def build_data_unavailable_signal_package(
    *,
    ticker: str,
    trading_date: Any,
    enabled_analysts: Iterable[Any] | None,
    reason: str | None = None,
    warning_message: str | None = None,
) -> dict:
    reason_text = str(reason or warning_message or "pre_open_reference_price_unavailable")
    analysts = _enabled_analysts(enabled_analysts)
    analyst_signals = [
        _data_unavailable_signal(
            ticker=ticker,
            analyst=analyst,
            reason=reason_text,
            warning_message=warning_message,
        )
        for analyst in analysts
    ]
    contract = build_signal_collection_contract(
        ticker=ticker,
        trading_date=trading_date,
        analyst_signals=analyst_signals,
        enabled_analysts=analysts,
    )
    return {
        "analyst_signals": analyst_signals,
        "signal_collection_contract": contract,
    }
