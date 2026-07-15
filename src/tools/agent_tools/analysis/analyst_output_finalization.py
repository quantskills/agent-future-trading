"""Shared final landing path for the three analyst agents."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.agent_tools.analysis.analyst_business_quality import apply_business_quality_enrichment
from tools.agent_tools.analysis.analyst_learning_calibration import calibrate_signal_with_learning_context
from tools.agent_tools.analysis.analyst_output_landing import analyst_output_landing_violations
from tools.agent_tools.analysis.analyst_product_price_behavior_profile import (
    apply_profile_usage_to_signal,
    build_profile_usage_contract,
    evaluate_profile_usage_contract,
    get_product_price_behavior_profile,
)
from tools.agent_tools.analysis.analyst_quality import apply_signal_quality_gate, apply_trade_research_contract
from tools.common.signal_evidence_collection import ACTION_EVIDENCE_EXCLUDED_SIGNAL_FIELDS


def resolve_analyst_llm_config(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the system main LLM config without analyst-private model overrides."""
    raw = state.get("llm_config")
    if not isinstance(raw, Mapping):
        raise ValueError("analyst_main_llm_config_missing")
    config = dict(raw)
    if not str(config.get("provider") or "").strip():
        raise ValueError("analyst_main_llm_provider_missing")
    if not str(config.get("model") or "").strip():
        raise ValueError("analyst_main_llm_model_missing")
    return config


def _preserve_data_unavailable_boundary(signal: Any) -> None:
    metadata = dict(getattr(signal, "metadata", {}) or {})
    data_usage = metadata.get("data_usage_summary") if isinstance(metadata.get("data_usage_summary"), Mapping) else {}
    if str(getattr(signal, "opportunity_type", "") or "") != "no_trade" or data_usage.get("data_available") is not False:
        return
    signal.signal = Signal.NEUTRAL
    signal.confidence = 0.0
    signal.opportunity_type = "no_trade"
    signal.opportunity_state = "no_opportunity"
    signal.trigger_valid = False
    signal.invalidation_present = False
    signal.data_freshness = "missing"
    signal.evidence_quality = "low"
    signal.business_quality_score = 0.0
    signal.setup_quality_score = 0.0
    signal.entry_quality = "poor"
    signal.setup_type = "data_unavailable_no_trade"
    signal.horizon_class = "flat"
    signal.expected_horizon_days = 0
    signal.market_regime = "data_unavailable"
    signal.entry_trigger = ""
    signal.exit_hint = ""
    signal.holding_period_hint = ""
    signal.would_change_view_if = ""
    signal.neutral_trigger_condition = ""
    signal.counterfactual_side = "flat"
    signal.invalidation_level = None
    signal.atr_stop_distance = None
    contract = metadata.get("action_evidence_contract")
    if isinstance(contract, dict):
        contract.update(
            {
                "side": "flat",
                "signal": Signal.NEUTRAL.value,
                "confidence": 0.0,
                "opportunity_type": "no_trade",
                "opportunity_state": "no_opportunity",
                "setup_type": "data_unavailable_no_trade",
                "setup_quality_ok": False,
                "trigger_valid": False,
                "current_trigger_confirmed": False,
                "invalidation_present": False,
                "entry_trigger": "",
                "exit_hint": "",
                "horizon_class": "flat",
                "expected_horizon_days": 0,
                "market_regime": "data_unavailable",
                "data_freshness": "missing",
                "evidence_quality": "low",
                "evidence_strength": "weak",
                "evidence_freshness": "missing",
                "invalidation_level": None,
                "atr_stop_distance": None,
                "would_change_view_if": "",
                "neutral_trigger_condition": "",
                "counterfactual_side": "flat",
            }
        )
        contract.pop("invalidation_condition", None)
        metadata["action_evidence_contract"] = contract
        signal.metadata = metadata


def build_required_market_data_unavailable_signal(
    *,
    analyst: str,
    ticker: str,
    trading_date: Any,
    full_config: Mapping[str, Any],
    info_cutoff: str = "pre_open",
) -> AnalystSignal:
    """Build one formal analyst-owned AEC when required market facts are absent."""
    profile = get_product_price_behavior_profile(ticker, full_config)
    profile_usage = build_profile_usage_contract(ticker, analyst, profile)
    date_text = trading_date.strftime("%Y-%m-%d") if hasattr(trading_date, "strftime") else str(trading_date)[:10]
    source_name = "pandaai_pre_open_reference"
    data_usage_summary = {
        "ticker": str(ticker).upper(),
        "trading_date": date_text,
        "analyst": analyst,
        "data_available": False,
        "sources": {
            source_name: {
                "source": "PandaAI",
                "dataset": "previous_trading_day_main_contract_quote",
                "available": False,
                "used_in_signal": False,
                "pre_open_only": True,
                "info_cutoff": info_cutoff,
                "missing_data": ["pre_open_reference_price"],
                "data_quality_flags": ["pre_open_reference_price_unavailable"],
                "reason": "pre_open_reference_price_unavailable",
            }
        },
    }
    signal = AnalystSignal(
        agent_name=analyst,
        signal=Signal.NEUTRAL,
        confidence=0.0,
        justification=(
            f"{ticker}: required pre-open market data is unavailable; "
            f"{analyst} records a neutral no-risk evidence contract."
        ),
        data_cutoff=info_cutoff,
        no_lookahead_status="ok",
        determinism_mode="deterministic_required_market_data_unavailable",
        source_artifacts=["data_quality:pre_open_reference_price"],
        horizon_class="flat",
        analyst_horizon="flat",
        expected_horizon_days=0,
        market_regime="data_unavailable",
        setup_type="data_unavailable_no_trade",
        data_freshness="missing",
        evidence_quality="low",
        evidence_strength="weak",
        evidence_freshness="missing",
        business_quality_score=0.0,
        data_coverage_score=0.0,
        tradeability_reason="required_pre_open_market_data_unavailable",
        opportunity_type="no_trade",
        opportunity_state="no_opportunity",
        setup_quality_score=0.0,
        entry_quality="poor",
        setup_quality_notes=["pre_open_reference_price_unavailable"],
        trigger_valid=False,
        invalidation_present=False,
        factor_focus=["required_market_data"],
        neutral_reason="pre_open_reference_price_unavailable",
        missing_evidence=["pre_open_reference_price"],
        neutral_opportunity_bucket="evidence_gap",
        neutral_watchlist_priority="none",
        do_not_trade_reason="pre_open_reference_price_unavailable",
        confirmation_requirements=["valid_pre_open_reference_price"],
        metadata={"data_usage_summary": data_usage_summary},
    )
    signal.entry_trigger = ""
    signal.exit_hint = ""
    signal.holding_period_hint = ""
    signal.would_change_view_if = ""
    signal.neutral_trigger_condition = ""
    signal.counterfactual_side = "flat"
    return finalize_analyst_signal(
        signal,
        quality_context={
            "sector": str(profile.get("sector") or ""),
            "tradeability": "low",
            "risk_flags": ["pre_open_reference_price_unavailable"],
            "data_quality": {
                "coverage_ratio": 0.0,
                "factor_freshness_score": 0.0,
                "no_lookahead_status": "ok",
            },
        },
        full_config=full_config,
        analyst=analyst,
        ticker=ticker,
        trading_date=trading_date,
        learning_context={},
        product_profile=profile,
        product_profile_usage=profile_usage,
    )


def finalize_analyst_signal(
    signal: Any,
    *,
    quality_context: Mapping[str, Any],
    full_config: Mapping[str, Any],
    analyst: str,
    ticker: str,
    trading_date: Any,
    learning_context: Mapping[str, Any] | None,
    product_profile: Mapping[str, Any],
    product_profile_usage: Mapping[str, Any],
) -> Any:
    """Calibrate, quality-check, profile, contract and validate one analyst signal."""
    context = dict(quality_context or {})
    config = dict(full_config or {})
    signal = calibrate_signal_with_learning_context(
        signal,
        analyst=analyst,
        ticker=ticker,
        learning_context=learning_context or {},
    )
    signal = apply_signal_quality_gate(signal, context, config, analyst)
    signal = apply_business_quality_enrichment(signal, context, config, analyst)
    _preserve_data_unavailable_boundary(signal)
    evaluated_profile = evaluate_profile_usage_contract(
        signal,
        context,
        product_profile_usage,
        product_profile,
    )
    signal = apply_profile_usage_to_signal(signal, evaluated_profile)
    signal = apply_trade_research_contract(
        signal,
        context,
        analyst=analyst,
        trading_date=trading_date,
        ticker=ticker,
    )
    _preserve_data_unavailable_boundary(signal)
    violations = analyst_output_landing_violations(signal)
    if violations:
        raise ValueError("analyst_final_output_contract_invalid:" + ",".join(violations))
    contract = deepcopy((getattr(signal, "metadata", {}) or {})["action_evidence_contract"])
    formal_fields = {
        field: deepcopy(contract[field])
        for field in AnalystSignal.model_fields
        if field in contract and field not in ACTION_EVIDENCE_EXCLUDED_SIGNAL_FIELDS
    }
    formal_fields.update(
        {
            "agent_name": analyst,
            "signal": contract["signal"],
            "confidence": contract["confidence"],
            "justification": "",
            "metadata": {"action_evidence_contract": contract},
        }
    )
    return AnalystSignal(**formal_fields)
