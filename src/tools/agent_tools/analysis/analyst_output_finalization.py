"""Shared final landing path for the three analyst agents."""

from __future__ import annotations

from typing import Any, Mapping

from graph.constants import Signal
from tools.agent_tools.analysis.analyst_business_quality import apply_business_quality_enrichment
from tools.agent_tools.analysis.analyst_learning_calibration import calibrate_signal_with_learning_context
from tools.agent_tools.analysis.analyst_output_landing import analyst_output_landing_violations
from tools.agent_tools.analysis.analyst_product_price_behavior_profile import (
    apply_profile_usage_to_signal,
    evaluate_profile_usage_contract,
)
from tools.agent_tools.analysis.analyst_quality import apply_signal_quality_gate, apply_trade_research_contract


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
    violations = analyst_output_landing_violations(signal)
    if violations:
        raise ValueError("analyst_final_output_contract_invalid:" + ",".join(violations))
    return signal


def persist_analyst_signal(
    db: Any,
    *,
    portfolio_id: str,
    analyst: str,
    ticker: str,
    prompt: str,
    signal: Any,
) -> Any:
    """Persist one analyst signal and retain its DB record id for lineage."""
    signal_id = db.save_signal(portfolio_id, analyst, ticker, prompt, signal)
    if signal_id:
        metadata = dict(getattr(signal, "metadata", {}) or {})
        metadata["signal_record_id"] = signal_id
        signal.metadata = metadata
    return signal_id
