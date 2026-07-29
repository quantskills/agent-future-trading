import json
import re
import math
from copy import deepcopy
from datetime import datetime
from enum import Enum
from typing import Any
from graph.constants import AgentKey
from graph.schema import (
    FundState,
    PositionRisk,
    FuturesDecision,
    FuturesAction,
    FuturesRecommendation,
    RecommendationAction,
    RecommendationSourceType,
    RecommendationStatus,
    TradingPhase,
)
from apis.contract_info_cache import FuturesContractInfoCache
from util.db_helper import get_db
from util.text_sanitize import sanitize_visible_text
from tools.agent_tools.analysis.analyst_dynamic_weights import DynamicWeightCalculator
from tools.agent_tools.analysis.analyst_learning_context import apply_config_learning_overlay
from tools.agent_tools.analysis.analyst_business_quality import summarize_business_quality
from tools.common.order_semantics import (
    build_lot_intent_consistency,
    recommendation_intent_from_lots,
)
from tools.common.final_action_semantics import (
    authority_allows_entry as _semantic_authority_allows_entry,
    contract_consumes_hold_exit_pm_learning,
    contract_reduces_or_exits_position,
    contract_requires_full_market_capital_rank,
    derive_memory_requirements,
    filter_action_values_for_contract_learning,
    has_valid_hold_exit_no_change_explanation,
    is_conditional_monitor_contract,
    validate_action_preference_family_consistency,
)
from tools.agent_tools.decision.pm_capital_allocator import (
    conflicting_weak_memory_record as _capital_conflicting_weak_memory_record,
    strategy_memory_record as _capital_strategy_memory_record,
)
from tools.agent_tools.decision.pm_contextual_rule_calibration import apply_pm_contextual_calibration
from tools.common.position_lifecycle import (
    apply_trade_plan_multiplier as _position_apply_trade_plan_multiplier,
    is_new_or_increasing_exposure as _position_is_new_or_increasing_exposure,
    same_sign as _position_same_sign,
    scale_signed_ratio as _position_scale_signed_ratio,
    target_side_from_ratio as _position_target_side_from_ratio,
)
from tools.agent_tools.decision.pm_capital_deployment_policy import (
    _apply_capital_utilization_control,
)
from tools.agent_tools.decision.pm_invalidation_policy import (
    _apply_pretrade_invalidation_control,
    _has_explicit_stop_protection,
    _has_position_exit_boundary,
    _has_structured_invalidation_condition,
)
from tools.agent_tools.decision.pm_reason_effects import (
    is_hard_zero_reason as _reason_effect_is_hard_zero,
    reason_effect_summary,
    requires_watchlist_reason,
    soft_limit_can_release_probe,
)
from tools.agent_tools.decision.pm_risk_controls import business_quality_position_gate
from tools.agent_tools.decision.pm_decision_memory_retrieval import retrieve_pm_memory
from tools.agent_tools.execution.trader_execution_exit_policy import (
    resolve_atr_protection,
    resolve_exit_policy_config,
)
from tools.agent_tools.decision.pm_full_market_capital_deployment import (
    CAPITAL_LAYER_ALPHA_SCALE,
    CAPITAL_LAYER_EXPLORATION,
    CAPITAL_LAYER_REAL_BUDGET,
    apply_full_market_capital_deployment,
)
from tools.agent_tools.decision.pm_lifecycle_action_port import classify_lifecycle_action_port
from tools.agent_tools.decision.pm_lifecycle_learning_router import route_lifecycle_learning
from tools.agent_tools.decision.pm_ticker_side_selection import select_ticker_side
from tools.agent_tools.decision.pm_risk_gate import PMRiskGate, PMRiskGateInput
from tools.agent_tools.decision.pm_contract_builder import (
    build_final_action_contract as _pm_tool_build_final_action_contract,
)
from tools.agent_tools.decision.pm_contract_self_check import check_final_action_contract
from tools.agent_tools.decision.pm_position_transition import (
    final_action_from_lots as _pm_tool_final_action_from_lots,
)
from tools.agent_tools.decision.pm_state_transition import classify_pm_decision_state
from tools.agent_tools.decision.pm_signal_fusion import (
    analyst_signal_combo as _fusion_analyst_signal_combo,
    build_scc_market_confirmation,
    build_opportunity_scorecard,
    resolve_decision_horizon as _fusion_resolve_decision_horizon,
)
from tools.common.alpha_setup import compact_profile_for_trace
from tools.common.signal_evidence_collection import (
    build_pm_evidence_signals_from_scc,
    build_scc_data_quality_summary,
    scc_news_quality_scores_from_metadata,
    validate_signal_collection_contract,
)
from tools.common.execution_trigger_semantics import (
    entry_invalidation_contract_error,
    execution_profile_allowed_for_analyst,
    execution_trigger_contract_error,
    normalize_execution_profile,
    normalize_trigger_confirmation_adjustment,
    trigger_source_for_analyst_profile,
)


def finalize_pm_full_market_contracts(*, generated, config, portfolio):
    """Run PM step 5 where required, then atomically sign every PM state in step 6."""
    for _, pm_state in generated:
        current_lots = int(pm_state.get("current_lots") or 0)
        target_lots = int(pm_state.get("target_lots") or 0)
        if current_lots and target_lots and (current_lots > 0) != (target_lots > 0):
            pm_state["target_lots"] = 0
            pm_state["lots_delta"] = -current_lots
            pm_state["lots_delta_abs"] = abs(current_lots)
            pm_state["lots_to_trade"] = abs(current_lots)
            reasons = {str(item) for item in (pm_state.get("control_reasons") or []) if str(item)}
            reasons.add("reverse_exit_first")
            pm_state["control_reasons"] = sorted(reasons)
            pm_state["reason_codes"] = sorted(reasons)
    apply_full_market_capital_deployment(
        generated=generated,
        config=config,
        portfolio=portfolio,
    )
    signed = []
    expected_count = 0
    for ticker, pm_state in generated:
        expected_count += 1
        recommendation = _sign_pm_memory_state(pm_state)
        if not isinstance(recommendation, FuturesRecommendation):
            raise RuntimeError(f"pm_step6_signer_did_not_create_recommendation:{ticker}")
        signed.append((ticker, recommendation))
    if len(signed) != expected_count:
        raise RuntimeError(
            f"pm_step6_incomplete_signed_recommendations:signed={len(signed)}:expected={expected_count}"
        )
    return signed


class RiskLevel(Enum):
    """Risk level classification for futures portfolio control."""
    SAFE = "SAFE"
    WARNING = "WARNING"      # Cashflow ratio in the 50%-70% band.
    DANGER = "DANGER"        # Cashflow ratio in the 30%-50% band.
    EMERGENCY = "EMERGENCY"  # Cashflow ratio below 30%.

def _sanitize_visible_text(text: str) -> str:
    """Normalize visible mojibake fragments before they reach logs or persisted justifications."""
    return sanitize_visible_text(text)

def _portfolio_account_equity(portfolio) -> float:
    """Return futures account equity using cash balance plus reserved margin."""
    cash_balance = float(getattr(portfolio, "cashflow", 0.0) or 0.0)
    positions = getattr(portfolio, "positions", {}) or {}
    reserved_margin = float(
        getattr(portfolio, "margin_used", None)
        if getattr(portfolio, "margin_used", None) is not None
        else sum(float(getattr(pos, "margin_used", 0.0) or 0.0) for pos in positions.values())
    )
    return cash_balance + reserved_margin


def _coerce_positive_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _price_limit_bounds_from_context(morning_price_context) -> tuple[float | None, float | None]:
    quote = getattr(morning_price_context, "quote", None) or getattr(morning_price_context, "raw_quote", None)
    if isinstance(quote, dict):
        upper = (
            quote.get("limit_up")
            or quote.get("upper_limit")
            or quote.get("up_limit")
            or quote.get("high_limit")
        )
        lower = (
            quote.get("limit_down")
            or quote.get("lower_limit")
            or quote.get("down_limit")
            or quote.get("low_limit")
        )
        return _coerce_positive_float(lower), _coerce_positive_float(upper)
    return None, None


def _dynamic_price_sanity_result(
    *,
    ticker: str,
    current_price: float,
    morning_price_context,
    router,
    trading_date,
) -> dict:
    """Validate price plausibility without hard-coded per-product ranges."""
    diagnostics = {
        "mode": "dynamic",
        "status": "ok",
        "reason": None,
        "reference_price": None,
        "lower_bound": None,
        "upper_bound": None,
        "limit_down": None,
        "limit_up": None,
    }
    if current_price <= 0:
        diagnostics.update({"status": "invalid", "reason": "non_positive_price"})
        return diagnostics

    limit_down, limit_up = _price_limit_bounds_from_context(morning_price_context)
    diagnostics["limit_down"] = limit_down
    diagnostics["limit_up"] = limit_up
    tolerance = max(0.0005 * current_price, 1e-8)
    if limit_down is not None and current_price < limit_down - tolerance:
        diagnostics.update({"status": "invalid", "reason": "below_exchange_limit", "lower_bound": limit_down})
        return diagnostics
    if limit_up is not None and current_price > limit_up + tolerance:
        diagnostics.update({"status": "invalid", "reason": "above_exchange_limit", "upper_bound": limit_up})
        return diagnostics

    _ = (ticker, router, trading_date)
    reference_price = _coerce_positive_float(getattr(morning_price_context, "prev_close_price", None))
    diagnostics["reference_price"] = reference_price
    if reference_price is None:
        diagnostics.update({"status": "unchecked", "reason": "reference_price_unavailable"})
        return diagnostics

    max_gap_ratio = 0.35
    lower_bound = reference_price * (1.0 - max_gap_ratio)
    upper_bound = reference_price * (1.0 + max_gap_ratio)
    if limit_down is not None:
        lower_bound = max(lower_bound, limit_down)
    if limit_up is not None:
        upper_bound = min(upper_bound, limit_up)
    diagnostics["lower_bound"] = lower_bound
    diagnostics["upper_bound"] = upper_bound
    if current_price < lower_bound - tolerance:
        diagnostics.update({"status": "invalid", "reason": "discontinuous_drop_vs_reference"})
    elif current_price > upper_bound + tolerance:
        diagnostics.update({"status": "invalid", "reason": "discontinuous_jump_vs_reference"})
    return diagnostics


def check_risk_level(portfolio, config) -> tuple[RiskLevel, float]:
    """Return the current risk level and account-equity ratio."""

    # Use configured cashflow as the capital base for risk classification.
    capital_base = config.get('cashflow', 0)
    risk_control = config.get('risk_control', {})
    warning_ratio = risk_control.get('warning_ratio', 0.7)
    danger_ratio = risk_control.get('danger_ratio', 0.5)
    emergency_ratio = risk_control.get('emergency_ratio', 0.3)

    # Futures risk state uses account equity, not cash-only balance or notional total_assets.
    account_equity = _portfolio_account_equity(portfolio)
    cashflow_ratio = account_equity / capital_base if capital_base > 0 else 1.0

    if cashflow_ratio >= warning_ratio:
        return RiskLevel.SAFE, cashflow_ratio
    elif cashflow_ratio >= danger_ratio:
        return RiskLevel.WARNING, cashflow_ratio
    elif cashflow_ratio >= emergency_ratio:
        return RiskLevel.DANGER, cashflow_ratio
    else:
        return RiskLevel.EMERGENCY, cashflow_ratio

def get_position_scaling_factor(risk_level: RiskLevel, config) -> float:
    """
    Return the position-scaling multiplier for the current risk level.

    Args:
        risk_level: Current portfolio risk classification.
        config: Runtime configuration.

    Returns:
        A multiplier applied to the target position ratio.
    """
    risk_control = config.get('risk_control', {})
    position_scaling = risk_control.get('position_scaling', {
        'safe': 1.0,
        'warning': 0.6,
        'danger': 0.3
    })

    return position_scaling.get(risk_level.value.lower(), 1.0)

def get_max_single_position_ratio(risk_level: RiskLevel, config) -> float:
    """
    Return the base per-opportunity sizing anchor for the current risk level.

    Args:
        risk_level: Current portfolio risk classification.
        config: Runtime configuration.

    Returns:
        A starting notional ratio anchor for weak or unverified opportunities.
        Validated opportunities can receive a larger dynamic budget, while the
        portfolio margin ceiling remains the hard capital constraint.
    """
    risk_control = config.get('risk_control', {})
    max_single_position = risk_control.get('max_single_position_ratio', {
        'safe': 0.15,
        'warning': 0.10,
        'danger': 0.05
    })

    return max_single_position.get(risk_level.value.lower(), 0.15)


def get_hard_allocation_margin_ratio(config: dict) -> float:
    """Return the active portfolio margin ceiling for new or increasing exposure.

    ``max_total_margin_ratio`` is the portfolio-level hard gate. Learning
    overlays may make the active target more conservative, but cannot lift the
    hard tradable-capital ceiling above this value.
    """
    def _ratio(value, default):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    hard_cap = _ratio((config or {}).get("max_total_margin_ratio"), 0.20)
    capital_control = (config or {}).get("capital_utilization_control", {}) or {}
    learned_cap = _ratio(capital_control.get("max_margin_ratio_after_scaling"), hard_cap)
    return min(hard_cap, learned_cap)


# Portfolio Manager Thresholds
thresholds = {
    "decision_memory_limit": 10
}

ANALYST_ORDER = ("technical", "fundamental", "commodity_news")

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

DEFAULT_SECTOR_WEIGHTS = {
    "energy": {"technical": 0.40, "fundamental": 0.40, "commodity_news": 0.20},
    "chemical": {"technical": 0.30, "fundamental": 0.50, "commodity_news": 0.20},
    "ferrous": {"technical": 0.30, "fundamental": 0.50, "commodity_news": 0.20},
    "nonferrous": {"technical": 0.35, "fundamental": 0.35, "commodity_news": 0.30},
    "agricultural": {"technical": 0.20, "fundamental": 0.45, "commodity_news": 0.35},
    "generic": {"technical": 0.40, "fundamental": 0.35, "commodity_news": 0.25},
}

DEFAULT_QUALITY_MULTIPLIERS = {
    "high": 1.00,
    "medium": 0.65,
    "low": 0.15,
    "unknown": 0.75,
}

DEFAULT_OPPORTUNITY_STATE_MULTIPLIERS = {
    "tradeable_candidate": 1.10,
    "probe_candidate": 0.90,
    "risk_reduction_candidate": 0.80,
    "watch_for_trigger": 0.30,
    "no_opportunity": 0.05,
    "unknown": 0.50,
}

DEFAULT_HOLDING_REBALANCE_CONTROL = {
    "enabled": True,
    "min_rebalance_ratio": 0.008,
    "min_new_entry_ratio": 0.004,
    "max_daily_reduction_ratio": 0.40,
    "reverse_min_signal_strength": 0.55,
    "reverse_min_confirmation_score": 0.65,
    "exit_min_signal_strength": 0.35,
    "exit_min_confirmation_score": 0.45,
    "min_fundamental_anchor_confidence": 0.40,
    "min_signal_support_confidence": 0.35,
    "fundamental_anchor_tradeability": ["high", "medium"],
    "news_override_tradeability": "high",
    "news_override_min_confidence": 0.60,
    "news_override_min_freshness": 0.70,
    "news_override_min_relevance": 0.70,
    "position_lifecycle": {
        "enabled": True,
        "trend_min_profit_ratio": 0.03,
        "trend_min_confirmation_score": 0.60,
        "trend_min_signal_strength": 0.30,
        "failed_loss_ratio": -0.05,
        "failed_max_confirmation_score": 0.45,
        "loss_revalidation_min_hold_days": 1,
        "loss_revalidation_ratio": -0.02,
        "loss_revalidation_min_confirmation_score": 0.55,
        "loss_revalidation_min_signal_strength": 0.25,
        "loss_revalidation_anchor_min_confirmation_score": 0.50,
        "loss_revalidation_anchor_min_signal_strength": 0.20,
        "loss_revalidation_reduction_multiplier": 0.50,
        "loss_revalidation_exit_ratio": -0.04,
        "loss_revalidation_exit_confirmation_score": 0.45,
        "new_loss_revalidation_enabled": True,
        "new_loss_revalidation_max_hold_days": 2,
        "new_loss_revalidation_ratio": -0.005,
        "new_loss_revalidation_exit_ratio": -0.02,
        "new_loss_revalidation_min_confirmation_score": 0.55,
        "new_loss_revalidation_min_signal_strength": 0.25,
        "new_loss_revalidation_reduction_multiplier": 0.50,
        "profitable_hold_continuation_enabled": True,
        "profitable_hold_min_pnl_ratio": 0.003,
        "profitable_hold_min_confirmation_score": 0.45,
        "profitable_hold_min_signal_strength": 0.20,
        "profitable_hold_anchor_min_confirmation_score": 0.35,
        "profitable_hold_anchor_min_signal_strength": 0.12,
        "profitable_hold_min_supporting_signals": 1,
        "profitable_hold_max_opposite_signals": 0,
        "probe_max_hold_days": 2,
        "probe_min_profit_ratio": 0.01,
        "probe_min_confirmation_score": 0.55,
        "exploration_reconfirm_enabled": True,
        "exploration_reconfirm_min_hold_days": 1,
        "exploration_reconfirm_states": ["watch_for_trigger", "unknown"],
        "exploration_reconfirm_min_confirmation_score": 0.55,
        "exploration_reconfirm_exit_confirmation_score": 0.48,
        "exploration_reconfirm_min_signal_strength": 0.30,
        "exploration_reconfirm_exit_ratio": -0.003,
        "exploration_reconfirm_reduction_multiplier": 0.50,
        "exploration_reconfirm_profit_keep_ratio": 0.005,
        "exploration_reconfirm_profit_min_confirmation": 0.45,
        "probe_one_lot_risk_budget": {
            "enabled": True,
            "max_one_lot_notional_ratio": 0.025,
            "max_one_lot_margin_ratio": 0.003,
            "max_price_limit_risk_ratio": 0.003,
            "max_price_gap_risk_ratio": 0.002,
            "min_positive_expectancy_multiplier": 1.5,
            "allow_positive_expectancy_override": True,
        },
        "require_pretrade_invalidation_for_new_entry": True,
        "missing_invalidation_cap_multiplier": 0.35,
        "missing_invalidation_probe_max_ratio": 0.02,
    },
    "horizon_consistency": {
        "enabled": True,
        "apply_to_new_entries": True,
        "apply_to_losing_holds": True,
        "medium_requires_short_timing": True,
        "medium_requires_invalidation": True,
        "min_short_timing_confidence": 0.45,
        "min_confirmation_score": 0.55,
        "allow_mismatch_probe": True,
        "mismatch_probe_max_ratio": 0.008,
        "mismatch_probe_floor_ratio": 0.004,
        "losing_hold_exit_ratio": -0.02,
        "losing_hold_reduction_multiplier": 0.50,
    },
    "watch_for_trigger_new_entry": {
        "enabled": True,
        "semantic_role": "observation_candidate_only",
        "audit_name": "watch_for_trigger_observation_candidate",
        "can_create_trade_authority": False,
        "requires_final_contract_authority": True,
        "allow_probe": True,
        "probe_max_ratio": 0.01,
        "probe_floor_ratio": 0.005,
        "scorecard_probe_min_supporting_signals": 2,
        "scorecard_probe_min_score": 0.35,
        "scorecard_probe_block_on_critical_data_gap": True,
    },
    "daily_tradeability_gate": {
        "enabled": True,
        "apply_to_new_entries": True,
        "block_watch_for_trigger_medium_in_choppy": True,
        "regimes_requiring_short_timing": ["choppy", "range", "ranging", "range_bound", "weak_trend"],
        "horizons_requiring_short_timing": ["medium", "long"],
        "allow_with_tradeable_support": True,
        "allow_with_strong_market_confirmation": True,
        "strong_market_confirmation_score": 0.68,
        "allow_with_mature_alpha": True,
        "min_mature_alpha_confidence": 0.60,
        "min_mature_alpha_samples": 5,
        "allow_with_high_quality_news": True,
        "allow_with_technical_timing": True,
    },
    "mature_alpha_release": {
        "enabled": True,
        "min_confirmation_score": 0.60,
        "min_policy_confidence": 0.60,
        "min_policy_samples": 5,
        "min_target_ratio": 0.015,
        "max_target_ratio": 0.045,
        "release_multiplier": 1.20,
        "require_tradeable_support": True,
        "require_invalidation": True,
    },
    "fast_candidate_alpha_probe": {
        "enabled": True,
        "min_confirmation_score": 0.58,
        "min_policy_confidence": 0.50,
        "probe_ratio": 0.012,
        "require_tradeable_support": True,
        "require_invalidation": True,
    },
    "min_hold_days_by_sector": {
        "energy": 3,
        "chemical": 5,
        "ferrous": 5,
        "nonferrous": 4,
        "agricultural": 7,
        "generic": 4,
    },
}


def _probe_ratio_from_soft_gate(
    *,
    side: str,
    current_ratio: float,
    raw_ratio: float,
    cap_ratio: float,
    floor_ratio: float,
) -> float:
    if side not in {"long", "short"} or cap_ratio <= 0:
        return raw_ratio
    if abs(current_ratio) > 1e-12 and _same_sign(raw_ratio, current_ratio):
        return _signed_abs(side, min(abs(raw_ratio), cap_ratio))
    probe_abs = min(cap_ratio, max(abs(raw_ratio), max(0.0, floor_ratio)))
    return _signed_abs(side, probe_abs) if probe_abs > 0 else raw_ratio


def _scorecard_probe_seed(
    *,
    opportunity_scorecard: dict,
    control: dict,
) -> tuple[str, float, dict]:
    watch_for_trigger_cfg = control.get("watch_for_trigger_new_entry") or {}
    if not bool(watch_for_trigger_cfg.get("allow_probe", False)):
        return "flat", 0.0, {}
    min_support = int(watch_for_trigger_cfg.get("scorecard_probe_min_supporting_signals", 2) or 2)
    min_score = _safe_float(watch_for_trigger_cfg.get("scorecard_probe_min_score"), 0.35)
    block_critical_gap = bool(watch_for_trigger_cfg.get("scorecard_probe_block_on_critical_data_gap", True))
    allow_single_high_quality = bool(watch_for_trigger_cfg.get("allow_single_high_quality_probe", True))
    single_min_score = _safe_float(watch_for_trigger_cfg.get("single_high_quality_probe_min_score"), 0.52)
    single_min_setup_quality = _safe_float(
        watch_for_trigger_cfg.get("single_high_quality_probe_min_setup_quality"),
        0.60,
    )
    single_min_business_quality = _safe_float(
        watch_for_trigger_cfg.get("single_high_quality_probe_min_business_quality"),
        0.60,
    )
    single_min_confirmation_score = _safe_float(
        watch_for_trigger_cfg.get("single_high_quality_probe_min_confirmation_score"),
        0.45,
    )
    scorecard_tradeable_candidate_min_confirmation = _safe_float(
        watch_for_trigger_cfg.get("scorecard_tradeable_candidate_probe_min_confirmation_score"),
        0.68,
    )
    probe_cap = max(0.0, _safe_float(watch_for_trigger_cfg.get("probe_max_ratio"), 0.01))
    probe_floor = max(0.0, _safe_float(watch_for_trigger_cfg.get("probe_floor_ratio"), 0.005))
    candidates: list[tuple[str, dict]] = []
    scorecard = opportunity_scorecard if isinstance(opportunity_scorecard, dict) else {}
    preferred_side = str(scorecard.get("preferred_side") or "").strip().lower()
    if preferred_side not in {"long", "short"}:
        return "flat", 0.0, {}
    for side in (preferred_side,):
        row = scorecard.get(side) if isinstance(scorecard.get(side), dict) else {}
        if not row:
            continue
        state = str(row.get("final_state") or "").lower()
        if state not in {"watch_for_trigger", "probe_candidate", "tradeable_candidate"}:
            continue
        failures = [str(item) for item in (row.get("gating_failures") or [])]
        if "no_directional_support" in failures or "missing_entry_setup" in failures:
            continue
        if block_critical_gap and "critical_data_gap" in failures:
            continue
        support_count = int(row.get("supporting_signal_count") or 0)
        score = _safe_float(row.get("score"), 0.0)
        max_setup_quality = _safe_float(row.get("max_setup_quality"), 0.0)
        max_business_quality = _safe_float(row.get("max_business_quality"), 0.0)
        confirmation_score = _safe_float(row.get("market_confirmation_score"), 0.0)
        conditional_monitor_candidate = _scorecard_conditional_monitor_candidate(row)
        scorecard_confirmed_tradeable_candidate = bool(
            state in {"probe_candidate", "tradeable_candidate"}
            and support_count >= 1
            and confirmation_score >= scorecard_tradeable_candidate_min_confirmation
            and "missing_entry_setup" not in failures
            and "missing_invalidation_boundary" not in failures
            and "weak_entry_setup_quality" not in failures
            and "fundamental_data_not_enough_for_standalone_setup" not in failures
            and "same_scope_alpha_setup_capped_or_rejected" not in failures
            and not row.get("technical_opposes_side")
        )
        structurally_confirmed_probe = bool(
            state in {"probe_candidate", "tradeable_candidate"}
            and support_count >= 1
            and bool(row.get("trigger_valid"))
            and bool(row.get("current_trigger_confirmed"))
            and bool(row.get("invalidation_present"))
            and int(row.get("entry_setup_count") or 0) > 0
            and bool(row.get("setup_quality_ok"))
            and "missing_entry_setup" not in failures
            and "missing_invalidation_boundary" not in failures
            and "weak_entry_setup_quality" not in failures
            and "critical_data_gap" not in failures
            and "same_scope_alpha_setup_capped_or_rejected" not in failures
            and not row.get("technical_opposes_side")
        )
        regular_probe = support_count >= min_support and score >= min_score
        single_high_quality_probe = (
            allow_single_high_quality
            and support_count >= 1
            and score >= single_min_score
            and max_setup_quality >= single_min_setup_quality
            and max_business_quality >= single_min_business_quality
            and confirmation_score >= single_min_confirmation_score
            and "weak_entry_setup_quality" not in failures
            and "missing_invalidation_boundary" not in failures
            and "same_scope_alpha_setup_capped_or_rejected" not in failures
        )
        if (
            regular_probe
            or single_high_quality_probe
            or scorecard_confirmed_tradeable_candidate
            or structurally_confirmed_probe
            or conditional_monitor_candidate
        ):
            candidates.append((side, row))
    if not candidates or probe_cap <= 0:
        return "flat", 0.0, {}
    candidates.sort(
        key=lambda item: (
            _safe_float(item[1].get("candidate_quality", item[1].get("score")), 0.0),
            int(item[1].get("supporting_signal_count") or 0),
            _safe_float(item[1].get("max_setup_quality"), 0.0),
        ),
        reverse=True,
    )
    side, row = candidates[0]
    return side, _signed_abs(side, min(probe_cap, max(probe_floor, 0.0))), row


def _scorecard_setup_quality_ok(row: dict | None) -> bool:
    if not isinstance(row, dict):
        return False
    if "setup_quality_ok" in row:
        return bool(row.get("setup_quality_ok"))
    failures = {str(item) for item in (row.get("gating_failures") or [])}
    if "missing_entry_setup" in failures or "weak_entry_setup_quality" in failures:
        return False
    return int(row.get("entry_setup_count") or 0) > 0


def _scorecard_trigger_valid(row: dict | None) -> bool:
    if not isinstance(row, dict):
        return False
    if "trigger_valid" in row:
        return bool(row.get("trigger_valid"))
    if "current_trigger_confirmed" in row:
        return bool(row.get("current_trigger_confirmed"))
    return False


def _scorecard_invalidation_present(row: dict | None) -> bool:
    if not isinstance(row, dict):
        return False
    if "invalidation_present" in row:
        return bool(row.get("invalidation_present"))
    failures = {str(item) for item in (row.get("gating_failures") or [])}
    if "missing_invalidation_boundary" in failures:
        return False
    return bool(row.get("entry_trigger") and _scorecard_setup_quality_ok(row))


def _scorecard_conditional_monitor_candidate(row: dict | None) -> bool:
    if not isinstance(row, dict):
        return False
    state = str(row.get("final_state") or "").lower()
    return bool(
        state == "watch_for_trigger"
        and _scorecard_setup_quality_ok(row)
        and not _scorecard_trigger_valid(row)
        and _scorecard_invalidation_present(row)
        and str(row.get("entry_trigger") or "").strip()
    )


def _scorecard_monitorable_setup(row: dict | None) -> bool:
    """A clean conditional setup is monitorable, but not a current trigger."""
    return bool(_scorecard_conditional_monitor_candidate(row))


def _is_controlled_probe_reason(reason: str) -> bool:
    return str(reason or "") in {
        "business_quality_probe_only",
        "business_quality_observe_or_block",
        "scorecard_current_tradeable_probe_seed",
        "single_high_quality_probe_only",
        "pm_watch_for_trigger_probe_cap",
        "horizon_consistency_probe_cap",
        "market_confirmation_quality_gate",
        "market_confirmation_conflict",
        "weak_signal_combo_probe_cap",
        "side_performance_probe_cap",
        "pm_risk_gate_soft_probe_floor",
        "missing_pretrade_invalidation",
        "alpha_setup_ev_fusion",
        "fast_candidate_alpha_probe",
    }


def _has_controlled_probe_reason(reasons: list[str]) -> bool:
    return any(_is_controlled_probe_reason(reason) for reason in reasons or [])


def _probe_risk_budget_config(full_config: dict | None) -> dict:
    control = _get_holding_rebalance_config(full_config or {})
    configured = (control.get("probe_one_lot_risk_budget") or {})
    defaults = {
        "enabled": True,
        "max_one_lot_notional_ratio": 0.025,
        "max_one_lot_margin_ratio": 0.003,
        "max_price_limit_risk_ratio": 0.003,
        "max_price_gap_risk_ratio": 0.002,
        "min_positive_expectancy_multiplier": 1.5,
        "allow_positive_expectancy_override": True,
    }
    defaults.update(configured if isinstance(configured, dict) else {})
    return defaults


def _position_budget_policy_config(full_config: dict | None) -> dict:
    configured = (full_config or {}).get("position_budget_policy") or {}
    if not isinstance(configured, dict):
        configured = {}
    return {
        "enabled": bool(configured.get("enabled", True)),
        "min_real_trade_margin_ratio": _safe_float(configured.get("min_real_trade_margin_ratio"), 0.0025),
        "min_real_trade_margin_abs": _safe_float(configured.get("min_real_trade_margin_abs"), 15000.0),
        "probe_margin_ratio": _safe_float(configured.get("probe_margin_ratio"), 0.006),
        "probe_margin_max_ratio": _safe_float(configured.get("probe_margin_max_ratio"), 0.012),
        "normal_trade_margin_ratio": _safe_float(configured.get("normal_trade_margin_ratio"), 0.03),
        "normal_trade_margin_max_ratio": _safe_float(configured.get("normal_trade_margin_max_ratio"), 0.06),
        "deployable_margin_ratio": _safe_float(configured.get("deployable_margin_ratio"), 0.06),
        "deployable_margin_max_ratio": _safe_float(configured.get("deployable_margin_max_ratio"), 0.12),
        "exceptional_margin_ratio": _safe_float(configured.get("exceptional_margin_ratio"), 0.075),
        "exceptional_margin_max_ratio": _safe_float(configured.get("exceptional_margin_max_ratio"), 0.13),
        "hard_max_total_margin_ratio": _safe_float(
            configured.get("hard_max_total_margin_ratio"),
            _safe_float((full_config or {}).get("max_total_margin_ratio"), 0.20),
        ),
        "max_single_ticker_margin_ratio": _safe_float(configured.get("max_single_ticker_margin_ratio"), 0.08),
        "apply_min_to_new_entries_only": bool(configured.get("apply_min_to_new_entries_only", True)),
        "require_final_trade_authority": bool(configured.get("require_final_trade_authority", True)),
        "block_below_min_when_cannot_scale": bool(configured.get("block_below_min_when_cannot_scale", True)),
    }


def _structured_new_entry_block_reason(final_entry_authority: dict | None) -> str | None:
    """Reject new-entry recommendations whose final structured authority is not tradeable.

    Natural language rationale is audit material only. The final action must be
    justified by PM's structured authority contract. Exploration probes remain
    allowed, but they must carry current action evidence; a bare probe label is
    not enough to create a real order.
    """
    if not isinstance(final_entry_authority, dict) or not final_entry_authority:
        return "missing_final_contract_authority"
    authority_type = str(final_entry_authority.get("authority_type") or "").strip().lower()
    reason_codes = {str(item or "") for item in (final_entry_authority.get("reason_codes") or [])}
    if authority_type in {"watchlist_only", "no_trade", "not_applicable", ""}:
        return "final_contract_authority_watchlist_only"
    if authority_type == "real_budget_entry":
        if not bool(
            final_entry_authority.get("open_action_evidence")
            and final_entry_authority.get("strong_current_evidence")
        ):
            return "final_contract_authority_real_entry_not_allowed"
        return None
    if authority_type == "exploration_probe":
        if bool(final_entry_authority.get("watch_for_trigger_block")):
            return "final_action_contract_watch_for_trigger_probe_block"
        if is_conditional_monitor_contract(final_entry_authority):
            return None
        hard_watchlist_codes = {
            "watch_for_trigger_cannot_open_position",
            "daily_tradeability_watchlist_only",
        }
        if reason_codes & hard_watchlist_codes:
            return "final_contract_authority_watchlist_only"
        if not _semantic_authority_allows_entry(final_entry_authority):
            return "final_contract_authority_probe_lacks_current_evidence"
        return None
    return "final_contract_authority_not_met"


def _enrich_final_authority_with_analyst_evidence(
    final_entry_authority: dict | None,
    analyst_signals: list,
    *,
    target_side: str = "",
) -> dict:
    """Mirror analyst action evidence into PM's final authority contract.

    The final authority remains the single trading outlet. This helper only
    makes sure already-structured analyst evidence is visible there, so Trader
    does not need to inspect analyst prose or raw signals.
    """
    authority = dict(final_entry_authority or {})
    if not authority:
        return authority
    evidence_by_agent: dict[str, dict] = {}
    has_open_evidence = False
    has_technical_confirmation = False
    has_event_catalyst = False
    has_entry_invalidation = False
    has_position_exit_boundary = False
    for signal in analyst_signals or []:
        agent = _normalize_agent_name(str(getattr(signal, "agent_name", "") or ""))
        if agent not in {"technical", "fundamental", "commodity_news"}:
            continue
        fields = _derive_signal_contract_fields(signal, agent)
        action_contract = _canonical_action_evidence_contract(signal)
        trigger_valid = _canonical_trigger_valid(signal)
        invalidation_present = _canonical_invalidation_present(signal)
        evidence_role = str(fields.get("evidence_role") or "")
        side = _signal_side_text(getattr(signal, "signal", None))
        state = _signal_opportunity_state(signal)
        current_evidence = bool(
            trigger_valid
            and _canonical_current_trigger_confirmed(signal)
            and invalidation_present
            and side in {"long", "short"}
            and (not target_side or side == target_side)
            and state in {"probe_candidate", "tradeable_candidate"}
        )
        if agent == "technical" and current_evidence and evidence_role == "entry_timing":
            has_technical_confirmation = True
            has_open_evidence = True
        if agent == "commodity_news" and current_evidence and evidence_role == "event_catalyst":
            has_event_catalyst = True
            has_open_evidence = True
        if invalidation_present and (not target_side or side == target_side):
            has_entry_invalidation = True
        if _has_position_exit_boundary([signal], target_side=target_side):
            has_position_exit_boundary = True
        evidence_by_agent[agent] = {
            "evidence_role": evidence_role,
            "side": side,
            "opportunity_state": state,
            "trigger_valid": trigger_valid,
            "current_trigger_confirmed": _canonical_current_trigger_confirmed(signal),
            "invalidation_present": invalidation_present,
            "entry_trigger": fields.get("entry_trigger"),
            "entry_timing_signal": fields.get("entry_timing_signal"),
            "action_evidence_contract": action_contract if isinstance(action_contract, dict) else {},
        }
    if evidence_by_agent:
        authority["analyst_action_evidence"] = evidence_by_agent
    authority["open_action_evidence"] = bool(authority.get("open_action_evidence") or has_open_evidence)
    authority["strong_current_evidence"] = bool(
        authority.get("strong_current_evidence")
        or authority.get("technical_confirmation")
        or authority.get("event_catalyst_confirmation")
        or has_technical_confirmation
        or has_event_catalyst
    )
    authority["technical_confirmation"] = bool(authority.get("technical_confirmation") or has_technical_confirmation)
    authority["event_catalyst_confirmation"] = bool(authority.get("event_catalyst_confirmation") or has_event_catalyst)
    authority["has_entry_invalidation"] = bool(
        authority.get("has_entry_invalidation") or has_entry_invalidation
    )
    authority["has_position_exit_boundary"] = bool(
        authority.get("has_position_exit_boundary") or has_position_exit_boundary
    )
    return authority


def _build_structured_pm_justification(
    *,
    ticker: str,
    decision: FuturesDecision,
    signal_snapshot: dict,
) -> str:
    """Render PM text from the final structured outlet, not from raw rationale."""
    action = decision.action.value if hasattr(decision.action, "value") else str(decision.action)
    lots = int(getattr(decision, "lots", 0) or 0)
    action_contract = (
        signal_snapshot.get("final_action_contract")
        if isinstance(signal_snapshot.get("final_action_contract"), dict)
        else {}
    )
    final_action = action_contract.get("final_action")
    target_lots = action_contract.get("target_lots")
    target_ratio = action_contract.get("target_position_ratio")
    lots_delta = action_contract.get("lots_delta")
    authority_type = action_contract.get("authority_type") or "not_applicable"
    reason_codes = action_contract.get("reason_codes") or []
    consistency = action_contract.get("consistency") if isinstance(action_contract.get("consistency"), dict) else {}
    consistency_status = consistency.get("status") or "unknown"
    return _sanitize_visible_text(
        (
            f"PM final structured outlet for {ticker}: action={action}, lots={lots}; "
            f"final_action={final_action or 'not_available'}; "
            f"authority_type={authority_type}; target_lots={target_lots}; "
            f"target_position_ratio={target_ratio}; "
            f"lots_delta={lots_delta}; "
            f"reason_codes={','.join(str(item) for item in reason_codes) if reason_codes else 'none'}; "
            f"recommendation_position_consistency={consistency_status}."
        )
    )


_FINAL_ACTION_CONTRACT_VERSION = "agentquant.final_action.v1"


def _final_action_from_lots(
    *,
    current_lots: int,
    target_lots: int,
    final_entry_authority: dict | None = None,
) -> str:
    return _pm_tool_final_action_from_lots(
        current_lots=current_lots,
        target_lots=target_lots,
        final_entry_authority=final_entry_authority,
    )


def _build_final_action_contract(
    *,
    ticker: str,
    current_lots: int,
    target_lots: int,
    position_ratio: float,
    margin_required: float,
    account_equity: float,
    lots_to_trade: int,
    lots_to_trade_reason: str | None,
    recommendation_intent: dict,
    final_entry_authority: dict | None,
    control_reasons: list[str],
    control_diagnostics: dict | None,
    opportunity_scorecard: dict | None,
    market_confirmation: dict | None,
    alpha_setup_action_values: list | None,
    execution_contract_fields: dict | None = None,
    contract_code: str | None = None,
    final_contract_scope: dict | None = None,
) -> dict:
    return _pm_tool_build_final_action_contract(
        ticker=ticker,
        current_lots=current_lots,
        target_lots=target_lots,
        position_ratio=position_ratio,
        margin_required=margin_required,
        account_equity=account_equity,
        lots_to_trade=lots_to_trade,
        lots_to_trade_reason=lots_to_trade_reason,
        recommendation_intent=recommendation_intent,
        final_entry_authority=final_entry_authority,
        control_reasons=control_reasons,
        control_diagnostics=control_diagnostics,
        opportunity_scorecard=opportunity_scorecard,
        market_confirmation=market_confirmation,
        alpha_setup_action_values=alpha_setup_action_values,
        execution_contract_fields=execution_contract_fields,
        contract_code=contract_code,
        final_contract_scope=final_contract_scope,
        select_learning_trace_action_values=_select_learning_trace_action_values,
        safe_float=_safe_float,
        futures_action_cls=FuturesAction,
    )


def _final_contract_scope_from_scc(
    *,
    signal_collection_contract: dict,
    current_lots: int,
    target_lots: int,
    final_action: str,
    execution_contract_fields: dict | None = None,
) -> dict:
    """Select final execution-scope facts from direction-aligned formal AECs."""
    execution_fields = (
        execution_contract_fields
        if isinstance(execution_contract_fields, dict)
        else {}
    )
    if final_action in {"open_probe", "open_real", "scale"}:
        return {
            "setup_type": execution_fields.get("setup_type"),
            "horizon_class": execution_fields.get("horizon_class"),
            "expected_horizon_days": execution_fields.get("expected_horizon_days"),
            "market_regime": execution_fields.get("market_regime"),
            "invalidation_level": execution_fields.get("invalidation_level"),
            "position_invalidation_level": execution_fields.get("position_invalidation_level"),
            "exit_hint": execution_fields.get("exit_hint"),
            "atr_stop_distance": execution_fields.get("atr_stop_distance"),
        }
    side = "long" if target_lots > 0 else "short" if target_lots < 0 else ""
    if not side and final_action in {"reduce", "exit"}:
        side = "long" if current_lots > 0 else "short" if current_lots < 0 else ""
    if not side:
        return {}
    aligned: list[tuple[int, float, dict]] = []
    order = {name: index for index, name in enumerate(ANALYST_ORDER)}
    for source in signal_collection_contract.get("source_contracts") or []:
        if not isinstance(source, dict):
            continue
        contract = source.get("action_evidence_contract")
        if not isinstance(contract, dict) or str(contract.get("side") or "").lower() != side:
            continue
        analyst = str(source.get("analyst") or contract.get("analyst") or "")
        aligned.append(
            (
                order.get(analyst, len(order)),
                -_safe_float(contract.get("confidence"), 0.0),
                contract,
            )
        )
    if not aligned:
        return {}
    aligned.sort(key=lambda row: (row[1], row[0]))
    primary = aligned[0][2]
    return {
        "setup_type": primary.get("setup_type"),
        "horizon_class": primary.get("horizon_class"),
        "expected_horizon_days": primary.get("expected_horizon_days"),
        "market_regime": primary.get("market_regime"),
        "invalidation_level": None,
        "position_invalidation_level": primary.get("position_invalidation_level"),
        "exit_hint": primary.get("exit_hint"),
        "atr_stop_distance": primary.get("atr_stop_distance"),
    }


def _build_pm_memory_state_update(
    *,
    ticker: str,
    current_lots: int,
    target_lots: int,
    position_ratio: float,
    margin_required: float,
    account_equity: float,
    lots_to_trade: int,
    lots_to_trade_reason: str | None,
    recommendation_intent: dict,
    final_entry_authority: dict | None,
    control_reasons: list[str],
    candidate_status: str = "normal",
    candidate_block_type: str | None = None,
    candidate_block_reason: str | None = None,
) -> dict:
    """Build the mutable PM memory state updated by steps 1-5."""
    authority = dict(final_entry_authority or {})
    current = int(current_lots or 0)
    target = int(target_lots or 0)
    lots_delta = target - current
    return {
        "pm_step": "steps_1_4_complete",
        "candidate_status": str(candidate_status or "normal"),
        "candidate_block_type": str(candidate_block_type or ""),
        "candidate_block_reason": str(candidate_block_reason or ""),
        "early_block_contract": bool(candidate_status == "blocked"),
        "ticker": ticker,
        "current_lots": current,
        "target_lots": target,
        "lots_delta": lots_delta,
        "lots_delta_abs": abs(lots_delta),
        "target_position_ratio": float(position_ratio or 0.0),
        "margin_required": float(margin_required or 0.0),
        "account_equity": float(account_equity or 0.0),
        "lots_to_trade": int(lots_to_trade or 0),
        "lots_to_trade_reason": lots_to_trade_reason or "",
        "recommendation_intent": dict(recommendation_intent or {}),
        "authority_type": str(authority.get("authority_type") or ""),
        "authority_decision": str(authority.get("authority_decision") or authority.get("decision") or ""),
        "requires_intraday_confirmation": bool(authority.get("requires_intraday_confirmation")),
        "conditional_trigger_authority": bool(authority.get("conditional_trigger_authority")),
        "can_execute_without_intraday_trigger": bool(
            authority.get("can_execute_without_intraday_trigger")
            if authority.get("can_execute_without_intraday_trigger") is not None
            else not bool(authority.get("requires_intraday_confirmation"))
        ),
        "entry_trigger": authority.get("entry_trigger") or authority.get("trigger") or "",
        "reason_codes": sorted(set(str(item) for item in (control_reasons or []) if item)),
    }


def _build_blocked_pm_memory_state_update(
    *,
    ticker: str,
    current_lots: int,
    target_lots: int,
    reason: str,
    authority_type: str,
    account_equity: float,
    execution_contract_fields: dict | None = None,
    signal_collection_contract: dict | None = None,
    control_diagnostics: dict | None = None,
    opportunity_scorecard: dict | None = None,
    market_confirmation: dict | None = None,
    alpha_setup_action_values: list | None = None,
) -> dict:
    """Update the same PM memory state for a hard-block path."""
    reason_value = str(reason or "pm_blocked_candidate")
    current = int(current_lots or 0)
    target = int(target_lots if target_lots is not None else current)
    memory_state = _build_pm_memory_state_update(
        ticker=ticker,
        current_lots=current,
        target_lots=target,
        position_ratio=0.0,
        margin_required=0.0,
        account_equity=float(account_equity or 1.0),
        lots_to_trade=abs(target - current),
        lots_to_trade_reason=reason_value,
        recommendation_intent=recommendation_intent_from_lots(current, target),
        final_entry_authority={
            "authority_type": str(authority_type or "not_applicable"),
            "authority_decision": "blocked",
            "decision": "blocked",
            "reason_codes": [reason_value],
        },
        control_reasons=[reason_value],
        candidate_status="blocked",
        candidate_block_type=str(authority_type or "blocked"),
        candidate_block_reason=reason_value,
    )
    execution_fields = dict(execution_contract_fields or {})
    if signal_collection_contract is not None:
        execution_fields["signal_collection_contract"] = deepcopy(signal_collection_contract)
    execution_fields["pm_six_step_stage"] = "steps_1_4_blocked_candidate_generated"
    execution_fields["candidate_status"] = "blocked"
    execution_fields["candidate_block_type"] = str(authority_type or "blocked")
    execution_fields["candidate_block_reason"] = reason_value
    memory_state.update(
        {
            "position_ratio": 0.0,
            "margin_required": 0.0,
            "account_equity": float(account_equity or 1.0),
            "lots_to_trade": abs(target - current),
            "lots_to_trade_reason": reason_value,
            "recommendation_intent": recommendation_intent_from_lots(current, target),
            "final_entry_authority": {
                "authority_type": str(authority_type or "not_applicable"),
                "authority_decision": "blocked",
                "decision": "blocked",
                "reason_codes": [reason_value],
            },
            "control_reasons": [reason_value],
            "control_diagnostics": dict(control_diagnostics or {}),
            "opportunity_scorecard": dict(opportunity_scorecard or {}),
            "market_confirmation": dict(market_confirmation or {}),
            "alpha_setup_action_values": list(alpha_setup_action_values or []),
            "execution_contract_fields": execution_fields,
        }
    )
    return memory_state


def _require_step6_signal_collection_contract(signal_collection_contract: object) -> dict:
    contract = signal_collection_contract if isinstance(signal_collection_contract, dict) else {}
    if not contract:
        raise ValueError("pm_step6_missing_signal_collection_contract_from_signal_collector")
    try:
        validate_signal_collection_contract(contract)
    except ValueError as exc:
        error = str(exc)
        if "invalid_decision_boundary" in error:
            raise ValueError("pm_step6_invalid_signal_collection_contract_boundary") from None
        if "invalid_source_agent" in error:
            raise ValueError("pm_step6_invalid_signal_collection_contract_source_agent") from None
        raise ValueError("pm_step6_invalid_signal_collection_contract") from None
    return contract


def _pm_step6_non_rank_capital_deployment(pm_state: dict, control_reasons: list[str]) -> dict:
    current_lots = int(pm_state.get("current_lots") or 0)
    target_lots = int(pm_state.get("target_lots") or 0)
    reason_codes = sorted(
        {str(reason) for reason in (control_reasons or []) if str(reason)}
        | {"non_new_risk_no_capital_rank"}
    )
    return {
        "selected_for_capital_deployment": False,
        "capital_allocation_reason": "non_new_risk_no_capital_rank",
        "original_target_lots": target_lots,
        "deployed_target_lots": target_lots,
        "deployed_lots_delta": target_lots - current_lots,
        "reason_codes": reason_codes,
    }


def _pm_step6_is_undeployed_new_risk_no_exposure(deployment: dict, *, current_lots: int, target_lots: int) -> bool:
    if not isinstance(deployment, dict) or deployment.get("selected_for_capital_deployment") is not False:
        return False
    if int(target_lots or 0) != int(current_lots or 0):
        return False
    reason = str(deployment.get("capital_allocation_reason") or "").strip().lower()
    return (
        reason == "no_rank_no_new_exposure"
        or reason.startswith("no_rank_no_new_exposure:")
        or reason == "no_rank_or_budget_no_new_exposure"
        or reason.startswith("no_rank_or_budget_no_new_exposure:")
    )


def _pm_step6_clear_undeployed_conditional_authority(
    *,
    contract_state: dict,
    execution_fields: dict,
    control_reasons: list[str],
) -> list[str]:
    authority = contract_state.get("final_entry_authority")
    authority = dict(authority) if isinstance(authority, dict) else {}
    authority["conditional_trigger_authority"] = False
    authority["requires_intraday_confirmation"] = False
    authority["can_execute_without_intraday_trigger"] = False
    authority["authority_type"] = "not_applicable"
    authority["authority_decision"] = "not_selected_for_capital_deployment"
    authority["decision"] = "not_selected_for_capital_deployment"
    authority["requires_authority"] = False
    authority["open_action_evidence"] = False
    authority["strong_current_evidence"] = False
    authority["reason_codes"] = [
        str(reason)
        for reason in (authority.get("reason_codes") or [])
        if str(reason) != "conditional_trigger_authority"
    ]
    contract_state["final_entry_authority"] = authority
    execution_fields["conditional_trigger_authority"] = False
    execution_fields["requires_intraday_confirmation"] = False
    execution_fields["can_execute_without_intraday_trigger"] = False
    execution_fields["undeployed_new_risk_no_intraday_trigger_required"] = True
    return [str(reason) for reason in control_reasons if str(reason) != "conditional_trigger_authority"]


def _pm_step6_expected_final_action(current_lots: int, target_lots: int, authority_type: str = "") -> str:
    current = int(current_lots or 0)
    target = int(target_lots or 0)
    authority = str(authority_type or "").strip().lower()
    if current == target:
        return "hold" if current else "wait"
    if current == 0 and target != 0:
        return "open_real" if authority == "real_budget_entry" else "open_probe"
    if target == 0 and current != 0:
        return "exit"
    if (current > 0 and target > 0) or (current < 0 and target < 0):
        return "scale" if abs(target) > abs(current) else "reduce"
    return "exit"


def _pm_step6_has_final_rank_trace(final_action_contract: dict) -> bool:
    evidence = final_action_contract.get("evidence_used")
    evidence = evidence if isinstance(evidence, dict) else {}
    deployment = final_action_contract.get("capital_deployment")
    deployment = deployment if isinstance(deployment, dict) else {}
    rank_fields = {
        "opportunity_rank",
        "rank_score",
        "rank_source",
        "rank_scope",
        "capital_rank_generated_by",
        "rank_capital_role",
        "capital_layer",
        "capital_ratio_source",
        "rank_reason",
        "rank_input_components",
        "rank_semantics_version",
        "opportunity_rank_meaning",
        "rank_is_capital_priority",
        "rank_is_not_trade_authority",
    }
    for container in (final_action_contract, evidence, deployment):
        for field in rank_fields:
            value = container.get(field)
            if value not in (None, "", [], {}):
                return True
    return False


def _pm_step6_build_contract_generation_check(
    *,
    final_action_contract: dict,
    deployment: dict,
    has_step5_deployment: bool,
) -> dict:
    errors: list[str] = []
    current_lots = int(final_action_contract.get("current_lots") or 0)
    target_lots = int(final_action_contract.get("target_lots") or 0)
    lots_delta = int(final_action_contract.get("lots_delta") or 0)
    final_action = str(final_action_contract.get("final_action") or "").strip().lower()
    authority_type = str(final_action_contract.get("authority_type") or "").strip().lower()
    expected_action = _pm_step6_expected_final_action(current_lots, target_lots, authority_type)
    final_port = classify_lifecycle_action_port(final_action_contract)
    final_requires_capital_deployment = bool(final_port.get("requires_full_market_rank"))
    deployment_reason = str(deployment.get("capital_allocation_reason") or "").strip().lower()
    undeployed_new_risk_no_exposure = _pm_step6_is_undeployed_new_risk_no_exposure(
        deployment,
        current_lots=current_lots,
        target_lots=target_lots,
    )

    if lots_delta != target_lots - current_lots:
        errors.append("step6_generation_lots_delta_mismatch")
    if final_action != expected_action:
        errors.append("step6_generation_final_action_mismatch")
    if final_requires_capital_deployment and not has_step5_deployment:
        errors.append("step6_generation_new_risk_missing_step5_deployment")
    if not final_requires_capital_deployment and not has_step5_deployment and _pm_step6_has_final_rank_trace(final_action_contract):
        errors.append("step6_generation_non_rank_contract_has_rank_trace")
    if not isinstance(deployment, dict) or not deployment:
        errors.append("step6_generation_capital_deployment_missing")
    elif not deployment_reason:
        errors.append("step6_generation_capital_deployment_reason_missing")

    if undeployed_new_risk_no_exposure:
        if final_action not in {"wait", "hold"}:
            errors.append("step6_generation_undeployed_new_risk_action_not_wait_or_hold")
        if target_lots != current_lots:
            errors.append("step6_generation_undeployed_new_risk_target_not_restored")
        if lots_delta != 0:
            errors.append("step6_generation_undeployed_new_risk_lots_delta_not_zero")
        if bool(final_action_contract.get("requires_intraday_confirmation")):
            errors.append("step6_generation_undeployed_new_risk_intraday_confirmation_residue")
        if bool(final_action_contract.get("conditional_trigger_authority")):
            errors.append("step6_generation_undeployed_new_risk_trigger_authority_residue")
        if bool(final_action_contract.get("can_execute_without_intraday_trigger")):
            errors.append("step6_generation_undeployed_new_risk_direct_execute_residue")
    elif has_step5_deployment and deployment.get("selected_for_capital_deployment") is False:
        errors.append("step6_generation_rejected_step5_deployment_without_no_exposure_reason")

    if any(
        field in final_action_contract
        for field in (
            "pm_internal_draft",
            "pm_scoring_draft",
            "pm_ranking_draft",
            "pm_capital_deployment_draft",
        )
    ):
        errors.append("step6_generation_final_contract_contains_pm_internal_state")

    return {
        "tool": "pm_step6_contract_generation_check",
        "ok": not errors,
        "errors": errors,
        "expected_final_action": expected_action,
        "actual_final_action": final_action,
        "current_lots": current_lots,
        "target_lots": target_lots,
        "lots_delta": lots_delta,
        "writes_db": False,
        "writes_contract": False,
        "no_llm": True,
    }


def _rebuild_recommendation_decision_from_contract(
    recommendation: FuturesRecommendation,
    contract: dict,
) -> FuturesDecision:
    current_lots = int(contract.get("current_lots") or 0)
    target_lots = int(contract.get("target_lots") or 0)
    intent = recommendation_intent_from_lots(current_lots, target_lots)
    action_value = str(intent.get("action") or "hold")
    try:
        action = FuturesAction(action_value)
    except Exception:
        action = FuturesAction.HOLD
    return FuturesDecision(
        ticker=recommendation.underlying_code,
        action=action,
        lots=int(intent.get("lots") or 0),
        price=float(recommendation.open_price or recommendation.base_price or 0.0),
        settle_price=float(recommendation.prev_close_price or recommendation.base_price or 0.0),
        margin_rate=0.0,
        contract_multiplier=1.0,
        contract_code=recommendation.contract_code,
        justification="PM step 6 final_action_contract signed after full-market capital deployment",
    )


def _sign_pm_memory_state(pm_state: dict) -> FuturesRecommendation:
    """Step 6 atomically creates the sole contract and recommendation."""
    if not isinstance(pm_state, dict) or not pm_state:
        raise ValueError("pm_step6_missing_pm_state")
    snapshot = {}
    execution_fields = dict(pm_state.get("execution_contract_fields") or {})
    signal_collection_contract = _require_step6_signal_collection_contract(
        pm_state.get("signal_collection_contract")
        or
        execution_fields.get("signal_collection_contract")
    )
    current_lots = int(pm_state.get("current_lots") or 0)
    target_lots = int(pm_state.get("target_lots") or 0)
    state_requires_capital_deployment = bool(
        classify_lifecycle_action_port(
            {"current_lots": current_lots, "target_lots": target_lots}
        ).get("requires_full_market_rank")
    )
    raw_deployment = pm_state.get("capital_deployment")
    has_step5_deployment = isinstance(raw_deployment, dict) and bool(raw_deployment)
    if has_step5_deployment:
        deployment = dict(raw_deployment)
    elif state_requires_capital_deployment:
        raise ValueError("pm_step6_missing_capital_deployment")
    else:
        deployment = _pm_step6_non_rank_capital_deployment(
            pm_state,
            list(pm_state.get("control_reasons") or []),
        )
    deployed_target = deployment.get("deployed_target_lots") if has_step5_deployment else target_lots
    target_lots = int(deployed_target if deployed_target is not None else target_lots)
    intent = recommendation_intent_from_lots(current_lots, target_lots)
    control_reasons = list(pm_state.get("control_reasons") or [])
    for reason in deployment.get("reason_codes") or []:
        if reason and reason not in control_reasons:
            control_reasons.append(str(reason))

    final_entry_authority = dict(pm_state.get("final_entry_authority") or {})
    contract_state = {
        "final_entry_authority": final_entry_authority,
    }
    if _pm_step6_is_undeployed_new_risk_no_exposure(
        deployment,
        current_lots=current_lots,
        target_lots=target_lots,
    ):
        control_reasons = _pm_step6_clear_undeployed_conditional_authority(
            contract_state=contract_state,
            execution_fields=execution_fields,
            control_reasons=control_reasons,
        )
        final_entry_authority = dict(contract_state.get("final_entry_authority") or {})

    if isinstance(execution_fields.get("rebalance_summary"), dict):
        execution_fields["rebalance_summary"] = {
            **execution_fields["rebalance_summary"],
            "target_lots": target_lots,
            "lots_delta": target_lots - current_lots,
            "reason": deployment.get("capital_allocation_reason")
            or execution_fields["rebalance_summary"].get("reason"),
            "capital_deployment": deployment,
        }
    execution_fields["capital_deployment"] = deployment
    recommendation_context = dict(pm_state.get("recommendation_context") or {})
    final_action = _final_action_from_lots(
        current_lots=current_lots,
        target_lots=target_lots,
        final_entry_authority=final_entry_authority,
    )
    if final_action in {"wait", "hold", "reduce", "exit"}:
        execution_fields.update(
            _build_execution_contract_fields(
                ticker=str(pm_state.get("ticker") or ""),
                current_lots=current_lots,
                target_lots=target_lots,
                analyst_signals=[],
                final_entry_authority=final_entry_authority,
                trading_date=recommendation_context.get("trading_date"),
                recommendation_intent=intent,
                control_reasons=control_reasons,
                alpha_setup_action_values=list(pm_state.get("alpha_setup_action_values") or []),
            )
        )
    final_contract_scope = _final_contract_scope_from_scc(
        signal_collection_contract=signal_collection_contract,
        current_lots=current_lots,
        target_lots=target_lots,
        final_action=final_action,
        execution_contract_fields=execution_fields,
    )
    contract_inputs = {
        "ticker": str(pm_state.get("ticker") or ""),
        "current_lots": current_lots,
        "target_lots": target_lots,
        "position_ratio": float(pm_state.get("position_ratio") or 0.0),
        "margin_required": float(pm_state.get("margin_required") or 0.0),
        "account_equity": float(pm_state.get("account_equity") or 0.0),
        "lots_to_trade": int(intent.get("lots") or 0),
        "lots_to_trade_reason": pm_state.get("lots_to_trade_reason"),
        "recommendation_intent": intent,
        "final_entry_authority": final_entry_authority,
        "control_reasons": control_reasons,
        "control_diagnostics": dict(pm_state.get("control_diagnostics") or {}),
        "opportunity_scorecard": dict(pm_state.get("opportunity_scorecard") or {}),
        "market_confirmation": dict(pm_state.get("market_confirmation") or {}),
        "alpha_setup_action_values": list(pm_state.get("alpha_setup_action_values") or []),
        "execution_contract_fields": execution_fields,
        "contract_code": recommendation_context.get("contract_code"),
        "final_contract_scope": final_contract_scope,
    }
    contract_inputs = _attach_incomplete_prior_diagnostics_to_contract_state(contract_inputs)

    final_action_contract = _build_final_action_contract(**contract_inputs)
    final_action_contract = _finalize_hold_exit_learning_explanation(final_action_contract)
    final_action_contract["signal_collection_contract_ref"] = {
        "ticker": signal_collection_contract.get("ticker"),
        "trading_date": signal_collection_contract.get("trading_date"),
        "source_contract_count": len(signal_collection_contract.get("source_contracts") or []),
        "collector_decision_boundary": signal_collection_contract.get("collector_decision_boundary"),
    }
    learning_trace = execution_fields.get("learning_to_position_trace")
    if isinstance(learning_trace, dict):
        learning_used = (
            final_action_contract.get("learning_used")
            if isinstance(final_action_contract.get("learning_used"), dict)
            else {}
        )
        learning_used.pop("learning_to_position_trace", None)
        learning_used["learning_to_position_summary"] = _contract_safe_learning_to_position_summary(learning_trace)
        if isinstance(execution_fields.get("pm_landing_consistency_audit"), dict):
            learning_used["pm_landing_consistency_audit"] = execution_fields["pm_landing_consistency_audit"]
        final_action_contract["learning_used"] = learning_used
    final_action_contract["capital_deployment"] = dict(deployment)
    step6_contract_generation_check = _pm_step6_build_contract_generation_check(
        final_action_contract=final_action_contract,
        deployment=deployment,
        has_step5_deployment=has_step5_deployment,
    )
    if not step6_contract_generation_check.get("ok"):
        raise ValueError(
            f"pm_step6_contract_generation_check_failed:{step6_contract_generation_check.get('errors')}"
        )
    snapshot["signal_collection_contract"] = deepcopy(signal_collection_contract)
    pm_contract_self_check = check_final_action_contract(final_action_contract)
    if not pm_contract_self_check.get("ok"):
        raise ValueError(
            f"pm_final_action_contract_self_check_failed:{pm_contract_self_check.get('errors')}"
        )
    snapshot["final_action_contract"] = final_action_contract
    snapshot["pm_six_step_trace"] = {
        "step6_contract_generation_check": step6_contract_generation_check,
        "pm_contract_self_check": pm_contract_self_check,
    }
    context = dict(pm_state.get("recommendation_context") or {})
    if not context:
        raise ValueError("pm_step6_missing_recommendation_context")
    recommendation = FuturesRecommendation(
        **context,
        action=RecommendationAction.HOLD,
        lots=0,
        signal_snapshot=snapshot,
    )
    decision = _rebuild_recommendation_decision_from_contract(recommendation, final_action_contract)
    recommendation.action = _to_recommendation_action(decision.action)
    recommendation.lots = int(decision.lots or 0)
    recommendation.justification = _build_structured_pm_justification(
        ticker=recommendation.underlying_code,
        decision=decision,
        signal_snapshot=snapshot,
    )
    return recommendation


def _release_block_category(primary_reason: str, reason_summary: dict) -> str:
    reason = str(primary_reason or "").lower()
    if reason_summary.get("hard_blocks"):
        return "hard_risk_or_authority"
    if requires_watchlist_reason(reason):
        return "watchlist_or_watch_for_trigger"
    if "confirmation" in reason or "trigger" in reason:
        return "current_confirmation_missing"
    if "invalidation" in reason or "stop" in reason:
        return "invalidation_missing"
    if "margin" in reason or "budget" in reason or "capital" in reason or "lot" in reason:
        return "capital_capacity"
    if "alpha_setup" in reason or "learning" in reason or "memory" in reason or "expectancy" in reason:
        return "learning_evidence_insufficient"
    if reason_summary.get("soft_limits"):
        return "soft_limit"
    if reason_summary.get("release_signals"):
        return "release_signal_present"
    return "no_release_block_recorded"


def _diagnostic_config_value(config: dict, path: tuple[str, ...], default=None):
    current = config if isinstance(config, dict) else {}
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def _build_release_ladder_diagnostics(full_config: dict | None) -> dict:
    config = full_config if isinstance(full_config, dict) else {}
    return {
        "probe": {
            "source": "config_snapshot",
            "configured_budget_band": {
                "min": _diagnostic_config_value(config, ("position_budget_policy", "probe_margin_ratio")),
                "max": _diagnostic_config_value(config, ("position_budget_policy", "probe_margin_max_ratio")),
            },
            "current_confirmation_floor": _diagnostic_config_value(
                config,
                ("portfolio", "watch_for_trigger_new_entry", "scorecard_tradeable_candidate_probe_min_confirmation_score"),
            ),
            "purpose": "small_real_trade_for_current_tradeable_probe",
        },
        "real_budget_entry": {
            "source": "config_snapshot",
            "configured_minimum_budget": _diagnostic_config_value(
                config,
                ("position_budget_policy", "min_real_trade_margin_ratio"),
            ),
            "current_confirmation_floor": _diagnostic_config_value(
                config,
                ("portfolio", "alpha_setup_ev_fusion", "min_confirmation_score"),
            ),
            "requires_current_tradeable_evidence": _diagnostic_config_value(
                config,
                ("portfolio", "alpha_setup_ev_fusion", "require_tradeable_support_for_release"),
                True,
            ),
            "requires_invalidation_boundary": _diagnostic_config_value(
                config,
                ("portfolio", "alpha_setup_ev_fusion", "require_invalidation_for_release"),
                True,
            ),
            "purpose": "qualified_positive_alpha_release",
        },
        "scale": {
            "source": "config_snapshot",
            "configured_budget_band": {
                "min": _diagnostic_config_value(
                    config,
                    ("capital_utilization_control", "strong_opportunity_target_margin_ratio_min"),
                ),
                "max": _diagnostic_config_value(
                    config,
                    ("capital_utilization_control", "strong_opportunity_target_margin_ratio_max"),
                ),
            },
            "current_confirmation_floor": _diagnostic_config_value(
                config,
                ("portfolio", "mature_alpha_release", "min_confirmation_score"),
            ),
            "purpose": "validated_alpha_add_or_scale",
        },
    }


def _build_release_block_diagnostics(
    *,
    ticker: str,
    decision_state: dict | None,
    final_entry_authority: dict | None,
    control_reasons: list[str] | None,
    lots_to_trade_reason: str | None,
    control_diagnostics: dict | None,
    opportunity_scorecard: dict | None,
    market_confirmation: dict | None,
    full_config: dict | None,
) -> dict:
    """Explain why release did or did not happen without creating trade authority."""
    state_view = dict(decision_state) if isinstance(decision_state, dict) else {}
    contract_reasons = state_view.get("reason_codes")
    if isinstance(contract_reasons, (list, tuple, set)):
        contract_reason_items = list(contract_reasons)
    elif contract_reasons:
        contract_reason_items = [contract_reasons]
    else:
        contract_reason_items = []
    reasons = sorted(
        {
            str(item)
            for item in [
                *(control_reasons or []),
                lots_to_trade_reason,
                *contract_reason_items,
            ]
            if item
        }
    )
    reason_summary = reason_effect_summary(reasons)
    primary_reason = (
        (reason_summary.get("hard_blocks") or [])
        or [item for item in reasons if requires_watchlist_reason(item)]
        or ([lots_to_trade_reason] if lots_to_trade_reason else [])
        or (reason_summary.get("soft_limits") or [])
        or reasons
        or ["none"]
    )[0]
    scorecard = opportunity_scorecard if isinstance(opportunity_scorecard, dict) else {}
    preferred_side = str(scorecard.get("preferred_side") or "").lower()
    preferred_side_card = scorecard.get(preferred_side) if preferred_side in {"long", "short"} else {}
    if not isinstance(preferred_side_card, dict):
        preferred_side_card = {}
    authority = final_entry_authority if isinstance(final_entry_authority, dict) else {}
    diagnostics = control_diagnostics if isinstance(control_diagnostics, dict) else {}
    confirmation = market_confirmation if isinstance(market_confirmation, dict) else {}
    category = _release_block_category(str(primary_reason), reason_summary)
    return {
        "contract_version": "agentquant.release_block_diagnostics.v1",
        "ticker": ticker,
        "observation_only": True,
        "does_not_modify_trade_authority": True,
        "cannot_create_or_change_lots": True,
        "single_source_of_trade_truth_remains": "final_action_contract",
        "primary_block_reason": str(primary_reason),
        "blocking_category": category,
        "reason_effect_summary": reason_summary,
        "evidence_snapshot": {
            "preferred_side": preferred_side or "flat",
            "preferred_side_state": preferred_side_card.get("final_state"),
            "preferred_side_score": preferred_side_card.get("score"),
            "market_confirmation_score": confirmation.get("confirmation_score"),
            "market_confirmation_status": confirmation.get("status") or confirmation.get("confirmation_label"),
            "has_release_signal": bool(reason_summary.get("release_signals")),
            "has_hard_block": bool(reason_summary.get("hard_blocks")),
            "has_watchlist_required_reason": any(requires_watchlist_reason(item) for item in reasons),
            "watch_for_trigger_block": bool(authority.get("watch_for_trigger_block")),
            "current_evidence_present": bool(
                authority.get("current_evidence")
                or authority.get("strong_current_evidence")
                or preferred_side_card.get("trigger_valid")
            ),
            "invalidation_present": bool(
                authority.get("invalidation_present")
                or preferred_side_card.get("invalidation_present")
                or diagnostics.get("pretrade_invalidation_present")
            ),
        },
        "release_ladder_diagnostics": _build_release_ladder_diagnostics(full_config),
        "next_evidence_needed": {
            "hard_risk_or_authority": ["remove_hard_block_or_wait_for_pm_risk_gate_clearance"],
            "watchlist_or_watch_for_trigger": ["current_tradeable_candidate_evidence"],
            "current_confirmation_missing": ["current_price_or_volume_confirmation"],
            "invalidation_missing": ["explicit_invalidation_or_stop_boundary"],
            "capital_capacity": ["feasible_budget_and_lot_capacity"],
            "learning_evidence_insufficient": ["exact_state_episode_reward_or_current_confirmation"],
            "soft_limit": ["stronger_current_evidence_or_probe_qualification"],
            "release_signal_present": ["downstream_contract_landing_check"],
            "no_release_block_recorded": ["no_additional_release_evidence_needed"],
        }.get(category, ["review_release_block_reason"]),
        "audit_boundary": (
            "diagnostic_only; not consumed by Trader; does not alter final_action_contract, "
            "final_action_contract authority, lots, budget, or execution"
        ),
    }


def _finalize_hold_exit_learning_explanation(contract: dict | None) -> dict:
    """Ensure consumed hold/exit learning has an explicit no-change explanation.

    This only amends PM reason codes. It does not change final_action,
    target_lots, lots_delta, authority, or any trade execution field.
    """
    contract = dict(contract or {})
    if not contract_consumes_hold_exit_pm_learning(contract):
        return contract
    current_lots = int(contract.get("current_lots") or 0)
    target_lots = int(contract.get("target_lots") or current_lots)
    if not current_lots or target_lots != current_lots:
        return contract
    if contract_reduces_or_exits_position(contract):
        return contract
    if has_valid_hold_exit_no_change_explanation(contract):
        return contract
    reason_codes = list(contract.get("reason_codes") or [])
    if "holding_period_control" not in reason_codes:
        reason_codes.append("holding_period_control")
    contract["reason_codes"] = sorted(set(str(item) for item in reason_codes if item))
    return contract


def _apply_position_budget_policy_for_new_entry(
    *,
    ticker: str,
    target_lots: int,
    current_lots: int,
    current_price: float,
    multiplier: float,
    margin_rate: float,
    account_equity: float,
    margin_available: float,
    max_net_exposure: float,
    current_net_exposure: float,
    current_ticker_exposure: float,
    final_entry_authority: dict,
    full_config: dict,
    control_reasons: list[str],
    control_notes: list[str],
    control_diagnostics: dict,
) -> tuple[int, float, float, float, float, float, str | None]:
    """Scale qualified new entries to a meaningful minimum margin budget."""
    cfg = _position_budget_policy_config(full_config)
    diagnostics = {
        "enabled": bool(cfg.get("enabled")),
        "ticker": ticker,
        "target_lots_before": int(target_lots),
        "current_lots": int(current_lots),
        "money_objective": "qualified_real_entries_should_not_be_too_small_to_matter",
    }
    if not cfg.get("enabled"):
        target_value = float(current_price) * int(target_lots) * float(multiplier)
        target_ratio = target_value / max(float(account_equity or 0.0), 1.0)
        control_diagnostics["position_budget_policy"] = diagnostics | {"decision": "disabled"}
        return target_lots, target_value, abs(target_value) * float(margin_rate), current_net_exposure, target_ratio, margin_rate, None

    increasing_same_side = bool(
        current_lots
        and target_lots
        and (current_lots > 0) == (target_lots > 0)
        and abs(target_lots) > abs(current_lots)
    )
    if current_lots != 0 and not increasing_same_side:
        target_value = float(current_price) * int(target_lots) * float(multiplier)
        target_ratio = target_value / max(float(account_equity or 0.0), 1.0)
        control_diagnostics["position_budget_policy"] = diagnostics | {"decision": "non_increasing_position_not_applicable"}
        return target_lots, target_value, abs(target_value) * float(margin_rate), current_net_exposure, target_ratio, margin_rate, None

    if target_lots == 0:
        control_diagnostics["position_budget_policy"] = diagnostics | {"decision": "flat_not_applicable"}
        return 0, 0.0, 0.0, current_net_exposure - current_ticker_exposure, 0.0, margin_rate, None

    if (
        cfg.get("require_final_trade_authority")
        and final_entry_authority.get("requires_authority")
        and final_entry_authority.get("authority_type") not in {"real_budget_entry", "exploration_probe"}
    ):
        diagnostics["decision"] = "final_entry_authority_not_met"
        control_diagnostics["position_budget_policy"] = diagnostics
        return 0, 0.0, 0.0, current_net_exposure - current_ticker_exposure, 0.0, margin_rate, "position_budget_authority_not_met"

    equity = max(float(account_equity or 0.0), 1.0)
    one_lot_notional = float(current_price) * float(multiplier)
    one_lot_margin = one_lot_notional * float(margin_rate)
    if one_lot_notional <= 0 or one_lot_margin <= 0:
        diagnostics["decision"] = "invalid_contract_budget_inputs"
        control_diagnostics["position_budget_policy"] = diagnostics
        return target_lots, 0.0, 0.0, current_net_exposure - current_ticker_exposure, 0.0, margin_rate, None

    side_sign = 1 if target_lots > 0 else -1
    current_abs_lots = abs(int(target_lots))
    current_margin = current_abs_lots * one_lot_margin
    planned_margin_ratio = max(
        0.0,
        min(
            _safe_float(final_entry_authority.get("target_margin_ratio"), 0.0),
            _safe_float(final_entry_authority.get("max_allowed_margin_ratio"), 0.0),
        ),
    )
    planned_margin = equity * planned_margin_ratio
    min_required_margin = max(
        equity * float(cfg.get("min_real_trade_margin_ratio") or 0.0),
        float(cfg.get("min_real_trade_margin_abs") or 0.0),
        planned_margin,
    )
    diagnostics.update({
        "one_lot_margin": one_lot_margin,
        "current_margin": current_margin,
        "min_required_margin": min_required_margin,
        "target_lots_before_abs": current_abs_lots,
        "final_authority_type": final_entry_authority.get("authority_type"),
        "capital_layer": final_entry_authority.get("capital_layer"),
        "capital_ratio_source": final_entry_authority.get("capital_ratio_source"),
        "candidate_quality": final_entry_authority.get("candidate_quality"),
        "planned_margin_ratio": planned_margin_ratio,
        "planned_margin": planned_margin,
        "max_allowed_margin_ratio": final_entry_authority.get("max_allowed_margin_ratio"),
    })
    if str(final_entry_authority.get("authority_type") or "") != "real_budget_entry":
        max_allowed_margin_ratio = _safe_float(final_entry_authority.get("max_allowed_margin_ratio"), 0.0)
        max_allowed_margin = equity * max(0.0, max_allowed_margin_ratio)
        probe_floor_margin = equity * max(0.0, float(cfg.get("probe_margin_ratio") or 0.0))
        desired_probe_margin = max(probe_floor_margin, planned_margin)
        desired_abs_lots = int(math.ceil(desired_probe_margin / one_lot_margin)) if desired_probe_margin > 0 else current_abs_lots
        if increasing_same_side:
            desired_abs_lots = max(abs(int(current_lots)), desired_abs_lots)
        one_lot_ratio = one_lot_notional / equity
        max_lots_by_margin = (
            abs(int(current_lots)) + int(max(0.0, margin_available) // one_lot_margin)
            if increasing_same_side
            else int(max(0.0, margin_available) // one_lot_margin)
        )
        max_lots_by_probe_margin = int(max_allowed_margin // one_lot_margin) if max_allowed_margin > 0 else desired_abs_lots
        net_without_ticker = float(current_net_exposure or 0.0) - float(current_ticker_exposure or 0.0)
        if side_sign > 0:
            max_lots_by_net = int(max(0.0, float(max_net_exposure) - net_without_ticker) // one_lot_ratio)
        else:
            max_lots_by_net = int(max(0.0, net_without_ticker + float(max_net_exposure)) // one_lot_ratio)
        feasible_abs_lots = min(
            desired_abs_lots,
            max_lots_by_margin,
            max_lots_by_probe_margin if max_lots_by_probe_margin > 0 else desired_abs_lots,
            max_lots_by_net,
        )
        if increasing_same_side:
            feasible_abs_lots = max(abs(int(current_lots)), feasible_abs_lots)
        target_lots_after = side_sign * max(0, feasible_abs_lots)
        target_value = target_lots_after * one_lot_notional
        margin_required_after = abs(target_lots_after) * one_lot_margin
        target_ratio = target_value / equity
        projected_net = current_net_exposure - current_ticker_exposure + target_ratio
        diagnostics.update({
            "decision": "exploration_probe_probe_floor_applied",
            "probe_floor_margin": probe_floor_margin,
            "desired_probe_margin": desired_probe_margin,
            "desired_abs_lots": desired_abs_lots,
            "max_lots_by_margin": max_lots_by_margin,
            "max_lots_by_probe_margin": max_lots_by_probe_margin,
            "max_lots_by_net": max_lots_by_net,
            "feasible_abs_lots": feasible_abs_lots,
            "target_lots_after": int(target_lots_after),
            "target_margin_after": margin_required_after,
            "target_ratio_after": target_ratio,
            "projected_net_exposure_after": projected_net,
            "why": "Step4 candidate quality selects the probe margin inside its configured range before lot rounding",
        })
        control_diagnostics["position_budget_policy"] = diagnostics
        if increasing_same_side and abs(target_lots_after) <= abs(int(current_lots)):
            current_value = int(current_lots) * one_lot_notional
            current_margin_total = abs(int(current_lots)) * one_lot_margin
            return (
                int(current_lots),
                current_value,
                current_margin_total,
                float(current_net_exposure),
                float(current_ticker_exposure),
                margin_rate,
                "step4_add_plan_no_incremental_capacity",
            )
        if target_lots_after == 0:
            return 0, 0.0, 0.0, current_net_exposure - current_ticker_exposure, 0.0, margin_rate, "exploration_probe_no_feasible_lot"
        control_reasons.append("exploration_probe_probe_floor_applied")
        return target_lots_after, target_value, margin_required_after, projected_net, target_ratio, margin_rate, None
    if (
        not increasing_same_side
        and current_margin + 1e-9 >= min_required_margin
        and abs(current_margin - planned_margin) < one_lot_margin
    ):
        target_value = side_sign * current_abs_lots * one_lot_notional
        target_ratio = target_value / equity
        control_diagnostics["position_budget_policy"] = diagnostics | {"decision": "already_meets_minimum"}
        return (
            side_sign * current_abs_lots,
            target_value,
            current_margin,
            current_net_exposure - current_ticker_exposure + target_ratio,
            target_ratio,
            margin_rate,
            None,
        )

    desired_abs_lots = int(math.ceil(min_required_margin / one_lot_margin))
    if increasing_same_side:
        desired_abs_lots = max(abs(int(current_lots)), desired_abs_lots)
    one_lot_ratio = one_lot_notional / equity
    max_lots_by_margin = (
        abs(int(current_lots)) + int(max(0.0, margin_available) // one_lot_margin)
        if increasing_same_side
        else int(max(0.0, margin_available) // one_lot_margin)
    )
    max_single_margin = equity * max(0.0, float(cfg.get("max_single_ticker_margin_ratio") or 0.0))
    max_lots_by_single_margin = int(max_single_margin // one_lot_margin) if one_lot_margin > 0 else 0
    net_without_ticker = float(current_net_exposure or 0.0) - float(current_ticker_exposure or 0.0)
    if side_sign > 0:
        max_lots_by_net = int(max(0.0, float(max_net_exposure) - net_without_ticker) // one_lot_ratio)
    else:
        max_lots_by_net = int(max(0.0, net_without_ticker + float(max_net_exposure)) // one_lot_ratio)
    feasible_abs_lots = min(
        desired_abs_lots,
        max_lots_by_margin,
        max_lots_by_single_margin,
        max_lots_by_net,
    )
    if increasing_same_side:
        feasible_abs_lots = max(abs(int(current_lots)), feasible_abs_lots)
    diagnostics.update({
        "desired_abs_lots": desired_abs_lots,
        "max_lots_by_margin": max_lots_by_margin,
        "max_lots_by_single_margin": max_lots_by_single_margin,
        "max_lots_by_net": max_lots_by_net,
        "feasible_abs_lots": feasible_abs_lots,
    })
    if feasible_abs_lots < desired_abs_lots:
        diagnostics["step4_layer_plan_shrunk_by_hard_limits"] = True
        control_reasons.append("step4_layer_plan_shrunk_by_hard_limits")
        control_notes.append(
            f"{ticker} Step4 layer plan shrunk by existing lot, margin, single-ticker and net-exposure caps: "
            f"{desired_abs_lots}->{feasible_abs_lots} lot(s)."
        )
        desired_abs_lots = max(0, feasible_abs_lots)

    if increasing_same_side and desired_abs_lots <= abs(int(current_lots)):
        current_value = int(current_lots) * one_lot_notional
        current_margin_total = abs(int(current_lots)) * one_lot_margin
        current_ratio = current_value / equity
        diagnostics.update({
            "decision": "add_plan_no_incremental_capacity",
            "target_lots_after": int(current_lots),
            "target_margin_after": current_margin_total,
            "target_ratio_after": current_ratio,
        })
        control_diagnostics["position_budget_policy"] = diagnostics
        return (
            int(current_lots),
            current_value,
            current_margin_total,
            float(current_net_exposure),
            current_ratio,
            margin_rate,
            "step4_add_plan_no_incremental_capacity",
        )

    if desired_abs_lots <= 0:
        diagnostics["decision"] = "no_feasible_lot"
        control_diagnostics["position_budget_policy"] = diagnostics
        return 0, 0.0, 0.0, current_net_exposure - current_ticker_exposure, 0.0, margin_rate, "minimum_real_trade_no_feasible_lot"

    target_lots_after = side_sign * desired_abs_lots
    target_value = target_lots_after * one_lot_notional
    target_ratio = target_value / equity
    margin_required_after = desired_abs_lots * one_lot_margin
    projected_net = current_net_exposure - current_ticker_exposure + target_ratio
    diagnostics.update({
        "decision": "minimum_margin_floor_applied",
        "target_lots_after": int(target_lots_after),
        "target_margin_after": margin_required_after,
        "target_ratio_after": target_ratio,
        "projected_net_exposure_after": projected_net,
    })
    control_diagnostics["position_budget_policy"] = diagnostics
    control_reasons.append("minimum_real_trade_margin_floor_applied")
    control_notes.append(
        f"{ticker} qualified new entry scaled to minimum meaningful margin: "
        f"{current_abs_lots}->{desired_abs_lots} lot(s), margin {current_margin:.0f}->{margin_required_after:.0f}."
    )
    return target_lots_after, target_value, margin_required_after, projected_net, target_ratio, margin_rate, None


def _price_gap_ratio_from_context(morning_price_context, current_price: float) -> float:
    prev_close = _coerce_positive_float(getattr(morning_price_context, "prev_close_price", None))
    if prev_close is None or current_price <= 0:
        return 0.0
    return abs(float(current_price) - prev_close) / max(prev_close, 1e-9)


def _one_lot_probe_risk_check(
    *,
    ticker: str,
    probe_side: str,
    current_price: float,
    multiplier: float,
    margin_rate: float,
    account_equity: float,
    morning_price_context,
    control_reasons: list[str],
    control_diagnostics: dict,
    full_config: dict,
) -> dict:
    """Check whether a soft probe can be converted to one real lot.

    This does not ban exploration; it prevents a nominally tiny ratio from
    becoming a high-risk one-lot position in volatile contracts.
    """
    cfg = _probe_risk_budget_config(full_config)
    one_lot_notional = float(current_price) * float(multiplier)
    one_lot_margin = one_lot_notional * float(margin_rate)
    equity = max(float(account_equity or 0.0), 1.0)
    notional_ratio = one_lot_notional / equity
    margin_ratio = one_lot_margin / equity
    limit_down, limit_up = _price_limit_bounds_from_context(morning_price_context)
    limit_risk_ratio = 0.0
    if probe_side == "long" and limit_down is not None:
        limit_risk_ratio = max(0.0, current_price - limit_down) * float(multiplier) / equity
    elif probe_side == "short" and limit_up is not None:
        limit_risk_ratio = max(0.0, limit_up - current_price) * float(multiplier) / equity
    gap_risk_ratio = _price_gap_ratio_from_context(morning_price_context, current_price) * notional_ratio
    positive_expectancy = "positive_expectancy_scale" in set(control_reasons or [])
    override_multiplier = max(1.0, _safe_float(cfg.get("min_positive_expectancy_multiplier"), 1.5))
    scale = override_multiplier if positive_expectancy and bool(cfg.get("allow_positive_expectancy_override", True)) else 1.0
    failures = []
    if notional_ratio > _safe_float(cfg.get("max_one_lot_notional_ratio"), 0.025) * scale:
        failures.append("one_lot_notional_risk_budget")
    if margin_ratio > _safe_float(cfg.get("max_one_lot_margin_ratio"), 0.003) * scale:
        failures.append("one_lot_margin_risk_budget")
    if limit_risk_ratio > _safe_float(cfg.get("max_price_limit_risk_ratio"), 0.003) * scale:
        failures.append("one_lot_limit_risk_budget")
    if gap_risk_ratio > _safe_float(cfg.get("max_price_gap_risk_ratio"), 0.002) * scale:
        failures.append("one_lot_gap_risk_budget")
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "ticker": ticker,
        "probe_side": probe_side,
        "one_lot_notional": one_lot_notional,
        "one_lot_margin": one_lot_margin,
        "notional_ratio": notional_ratio,
        "margin_ratio": margin_ratio,
        "limit_risk_ratio": limit_risk_ratio,
        "gap_risk_ratio": gap_risk_ratio,
        "positive_expectancy_override": bool(positive_expectancy),
        "budget_scale": scale,
        "failures": failures,
        "passed": (not bool(cfg.get("enabled", True))) or not failures,
    }


def _probe_like_control_reason_present(reasons: list[str]) -> bool:
    probe_markers = {
        "unknown_alpha_probe",
        "minimum_one_lot_probe",
        "controlled_probe_below_min_entry_kept",
        "pm_watch_for_trigger_probe_cap",
        "horizon_consistency_probe_cap",
        "scorecard_current_tradeable_probe_seed",
        "pm_risk_gate_soft_probe_floor",
        "fast_candidate_alpha_probe",
        "soft_block_converted_to_probe_only",
    }
    return any(str(reason or "") in probe_markers for reason in reasons or [])


def _hard_zero_reason(reason: str) -> bool:
    return _reason_effect_is_hard_zero(str(reason or ""))


def _is_lifecycle_exit_required_reason(control_reasons: list[str]) -> bool:
    """Reasons that may bypass cooling-period/min-hold deferral."""
    reason_set = {str(reason or "") for reason in control_reasons or []}
    return bool(
        "position_lifecycle_failed" in reason_set
        or "position_lifecycle_probe_expired" in reason_set
        or "position_lifecycle_loss_revalidation_failed" in reason_set
        or "new_position_loss_revalidation_failed" in reason_set
        or "exploration_probe_reconfirm_failed" in reason_set
        or "exploration_probe_reconfirm_reduce" in reason_set
        or "fundamental_medium_opposition" in reason_set
        or "winning_template_continuation_protective_reduce" in reason_set
        or "hold_exit_action_value_protection" in reason_set
    )


def _real_probe_soft_block_can_be_overridden(reason: str) -> bool:
    """Soft blocks that can yield to same-day evidence or same-scope positive EV."""
    return soft_limit_can_release_probe(str(reason or ""))


def _real_probe_requires_watchlist(reason: str) -> bool:
    """Blocks that should not be bypassed by a one-lot probe."""
    return requires_watchlist_reason(str(reason or ""))


def _alpha_ev_trade_authority(alpha_ev: dict) -> dict:
    """Return whether current evidence can justify a new-entry action.

    Analyst weights and medium-term direction are priors only. New futures
    entries need action evidence: a technical timing trigger, an event/current
    market catalyst, or a same-scope positive expectancy that is reconfirmed by
    current evidence. This is the final outlet contract PM must enforce.
    """
    if not isinstance(alpha_ev, dict):
        alpha_ev = {}
    scorecard_state = str(alpha_ev.get("scorecard_state") or "").lower()
    strong_realtime = bool(alpha_ev.get("strong_realtime_evidence"))
    strong_market = bool(alpha_ev.get("strong_market_confirmation"))
    technical_support = bool(alpha_ev.get("technical_supports_side"))
    technical_entry_timing_support = bool(alpha_ev.get("technical_entry_timing_supports_side"))
    technical_opposes = bool(alpha_ev.get("technical_opposes_side"))
    has_tradeable_support = bool(alpha_ev.get("has_tradeable_support"))
    has_monitorable_setup = bool(alpha_ev.get("has_monitorable_setup"))
    has_entry_invalidation = bool(alpha_ev.get("has_entry_invalidation"))
    has_position_exit_boundary = bool(alpha_ev.get("has_position_exit_boundary"))
    event_catalyst = bool(
        alpha_ev.get("event_catalyst_supports_side")
        or alpha_ev.get("news_event_catalyst_supports_side")
        or alpha_ev.get("news_high_quality_override")
    )
    qualified_positive = bool(
        alpha_ev.get("qualified_positive_expectancy")
        or alpha_ev.get("positive_action_value")
        or alpha_ev.get("positive_profile")
    )
    analyst_tradeable_probe = bool(alpha_ev.get("analyst_tradeable_probe_candidate"))
    confirmation_score = _safe_float(alpha_ev.get("current_confirmation_score"), 0.0)
    independent_support_count = int(alpha_ev.get("independent_support_count") or 0)

    technical_confirmation = bool(
        technical_support
        and technical_entry_timing_support
        and not technical_opposes
        and strong_realtime
        and independent_support_count >= 1
        and has_entry_invalidation
    )
    event_catalyst_confirmation = bool(
        event_catalyst
        and has_entry_invalidation
        and not technical_opposes
        and (strong_realtime or strong_market or confirmation_score >= 0.60)
    )
    current_setup_confirmation = bool(
        has_tradeable_support
        and scorecard_state in {"probe_candidate", "tradeable_candidate"}
        and confirmation_score >= 0.60
        and not technical_opposes
        and has_entry_invalidation
    )
    monitorable_setup_confirmation = bool(
        has_monitorable_setup
        and scorecard_state == "watch_for_trigger"
        and technical_support
        and technical_entry_timing_support
        and confirmation_score >= 0.60
        and not technical_opposes
        and has_entry_invalidation
    )
    watch_for_trigger_without_setup = bool(
        scorecard_state in {"watch_for_trigger", "no_opportunity", "unknown", ""}
        and not has_tradeable_support
        and not has_monitorable_setup
        and not technical_confirmation
        and not event_catalyst_confirmation
        and not current_setup_confirmation
        and not monitorable_setup_confirmation
    )
    market_confirmation = bool(
        strong_market
        and confirmation_score >= 0.68
        and independent_support_count >= 1
        and not watch_for_trigger_without_setup
        and not technical_opposes
        and has_entry_invalidation
    )
    watch_for_trigger_without_confirmation = bool(
        watch_for_trigger_without_setup and not market_confirmation
    )
    open_action_evidence = bool(
        technical_confirmation
        or event_catalyst_confirmation
        or current_setup_confirmation
        or monitorable_setup_confirmation
        or market_confirmation
        or analyst_tradeable_probe
    )
    current_trade_authority = bool(
        has_position_exit_boundary
        and (
            open_action_evidence
            or (
                qualified_positive
                and (technical_confirmation or event_catalyst_confirmation or market_confirmation)
            )
        )
    )
    return {
        "scorecard_state": scorecard_state,
        "qualified_positive": qualified_positive,
        "analyst_tradeable_probe_candidate": analyst_tradeable_probe,
        "strong_realtime_evidence": strong_realtime,
        "strong_market_confirmation": strong_market,
        "technical_supports_side": technical_support,
        "technical_entry_timing_supports_side": technical_entry_timing_support,
        "technical_opposes_side": technical_opposes,
        "has_tradeable_support": has_tradeable_support,
        "has_monitorable_setup": has_monitorable_setup,
        "has_entry_invalidation": has_entry_invalidation,
        "has_position_exit_boundary": has_position_exit_boundary,
        "event_catalyst_supports_side": event_catalyst,
        "confirmation_score": confirmation_score,
        "independent_support_count": independent_support_count,
        "market_confirmation": market_confirmation,
        "technical_confirmation": technical_confirmation,
        "event_catalyst_confirmation": event_catalyst_confirmation,
        "current_setup_confirmation": current_setup_confirmation,
        "monitorable_setup_confirmation": monitorable_setup_confirmation,
        "executable_setup_confirmation": current_setup_confirmation,
        "open_action_evidence": open_action_evidence,
        "watch_for_trigger_without_setup": watch_for_trigger_without_setup,
        "watch_for_trigger_without_confirmation": watch_for_trigger_without_confirmation,
        "current_trade_authority": current_trade_authority,
        "action_evidence_router": {
            "open": {
                "required": [
                    "technical_trigger_or_event_catalyst",
                    "entry_invalidation_boundary",
                    "position_exit_boundary",
                ],
                "technical_trigger": technical_confirmation,
                "event_catalyst": event_catalyst_confirmation,
                "current_setup_confirmation": current_setup_confirmation,
                "monitorable_setup_confirmation": monitorable_setup_confirmation,
                "market_confirmation": market_confirmation,
                "analyst_tradeable_probe_candidate": analyst_tradeable_probe,
                "positive_open_action_value": qualified_positive,
                "static_weights_role": "prior_only",
                "static_weights_can_create_trade_authority": False,
            },
            "hold": {"learning": "hold_action_value"},
            "exit": {"learning": "exit_action_value"},
            "scale": {"learning": "open_action_value_plus_current_confirmation"},
        },
    }


def _qualified_real_probe_release(
    *,
    control_reasons: list[str],
    control_diagnostics: dict,
) -> tuple[bool, dict]:
    """Decide whether a soft-gated setup can remain a real one-lot probe.

    This keeps the money objective explicit: strong current evidence or
    action-matched positive expectancy can override soft blocks, but not hard
    risk, missing invalidation, or repeat negative expectancy.
    """
    alpha_ev = control_diagnostics.get("alpha_setup_ev_fusion") if isinstance(control_diagnostics, dict) else {}
    if not isinstance(alpha_ev, dict):
        alpha_ev = {}
    reasons = [str(reason or "") for reason in control_reasons or []]
    hard_zero = any(_hard_zero_reason(reason) for reason in reasons)
    hard_watchlist = any(_real_probe_requires_watchlist(reason) for reason in reasons)
    soft_blocks = sorted({reason for reason in reasons if _real_probe_soft_block_can_be_overridden(reason)})
    trade_authority = _alpha_ev_trade_authority(alpha_ev)
    scorecard_state = str(trade_authority.get("scorecard_state") or "").strip().lower()
    watch_for_trigger_semantic_block = bool(
        "pm_watch_for_trigger_probe_cap" in reasons
        or "watch_for_trigger_cannot_open_position" in reasons
        or "daily_tradeability_watchlist_only" in reasons
        or scorecard_state in {"watch_for_trigger", "no_opportunity", "unknown", ""}
        or trade_authority.get("watch_for_trigger_without_setup")
    )
    strong_realtime = bool(trade_authority.get("strong_realtime_evidence"))
    strong_market = bool(trade_authority.get("strong_market_confirmation"))
    qualified_positive = bool(trade_authority.get("qualified_positive"))
    positive_action_value = bool(alpha_ev.get("positive_action_value"))
    positive_profile = bool(alpha_ev.get("positive_profile"))
    confirmation_score = float(trade_authority.get("confirmation_score") or 0.0)
    independent_support_count = int(trade_authority.get("independent_support_count") or 0)
    release = bool(
        soft_blocks
        and not hard_zero
        and not hard_watchlist
        and not watch_for_trigger_semantic_block
        and trade_authority.get("current_trade_authority")
    )
    return release, {
        "release": release,
        "soft_blocks": soft_blocks,
        "hard_zero": hard_zero,
        "hard_watchlist": hard_watchlist,
        "watch_for_trigger_semantic_block": watch_for_trigger_semantic_block,
        "scorecard_state": scorecard_state,
        "qualified_positive_expectancy": qualified_positive,
        "positive_action_value": positive_action_value,
        "positive_profile": positive_profile,
        "strong_realtime_evidence": strong_realtime,
        "strong_market_confirmation": strong_market,
        "confirmation_score": confirmation_score,
        "independent_support_count": independent_support_count,
        "market_confirmation": trade_authority.get("market_confirmation"),
        "technical_confirmation": trade_authority.get("technical_confirmation"),
        "executable_setup_confirmation": trade_authority.get("executable_setup_confirmation"),
        "watch_for_trigger_without_setup": trade_authority.get("watch_for_trigger_without_setup"),
        "watch_for_trigger_without_confirmation": trade_authority.get("watch_for_trigger_without_confirmation"),
        "current_trade_authority": trade_authority.get("current_trade_authority"),
        "money_objective": "allow_positive_or_strong_confirmed_probe_without_releasing_hard_risk",
    }


_MINIMUM_REAL_PROBE_SOFT_REASONS = {
    "business_quality_probe_only",
    "business_quality_observe_or_block",
    "scorecard_current_tradeable_probe_seed",
    "market_confirmation_quality_gate",
    "weak_signal_combo_probe_cap",
    "side_performance_probe_cap",
    "pm_risk_gate_soft_probe_floor",
    "controlled_probe_below_min_entry_kept",
    "fast_candidate_alpha_probe",
    "qualified_positive_expectancy",
    "positive_expectancy_scale",
}

_MINIMUM_REAL_PROBE_DISQUALIFIED_REASONS = {
    "repeat_loss_watchlist_only",
    "negative_expectancy_cap_or_exit",
    "negative_expectancy_new_entry_watchlist_only",
    "alpha_setup_open_action_value_missing",
    "pm_watch_for_trigger_probe_cap",
    "single_high_quality_probe_only",
    "horizon_consistency_probe_cap",
    "missing_pretrade_invalidation",
}


def _should_attempt_minimum_real_probe(
    *,
    current_lots: int,
    target_lots: int,
    target_ratio: float,
    control_reasons: list[str],
    probe_release: bool,
    alpha_ev_blocks_real_probe: bool,
    analyst_tradeable_probe: bool = False,
) -> bool:
    reasons = [str(reason or "") for reason in control_reasons or []]
    direction_or_watchlist_semantics = {
        "pm_watch_for_trigger_probe_cap",
        "watch_for_trigger_cannot_open_position",
        "daily_tradeability_watchlist_only",
    }
    return bool(
        current_lots == 0
        and target_lots == 0
        and abs(float(target_ratio or 0.0)) > 1e-12
        and not any(reason in direction_or_watchlist_semantics for reason in reasons)
        and (
            any(reason in _MINIMUM_REAL_PROBE_SOFT_REASONS for reason in reasons)
            or probe_release
            or analyst_tradeable_probe
        )
        and (
            not any(reason in _MINIMUM_REAL_PROBE_DISQUALIFIED_REASONS for reason in reasons)
            or probe_release
            or analyst_tradeable_probe
        )
        and not alpha_ev_blocks_real_probe
        and not any(_hard_zero_reason(reason) for reason in reasons)
    )


def _minimum_real_probe_candidate_ratio(
    *,
    current_ratio: float,
    pre_control_ratio: float,
    probe_release: bool,
    analyst_tradeable_probe: bool = False,
) -> float:
    """Keep released probe direction available after soft gates shrink ratio to zero."""
    current = float(current_ratio or 0.0)
    if abs(current) > 1e-12:
        return current
    if probe_release or analyst_tradeable_probe:
        previous = float(pre_control_ratio or 0.0)
        if abs(previous) > 1e-12:
            return previous
    return current


def _conditional_monitor_probe_seed_plan(
    *,
    ticker: str,
    current_lots: int,
    target_lots: int,
    target_ratio: float,
    current_ticker_exposure: float,
    current_net_exposure: float,
    account_equity: float,
    current_price: float,
    multiplier: float,
    margin_rate: float,
    margin_available: float,
    max_position_ratio: float,
    max_net_exposure: float,
    morning_price_context: dict | None,
    control_reasons: list[str],
    control_diagnostics: dict,
    full_config: dict | None = None,
) -> dict:
    """Keep a clean watch_for_trigger setup as an intraday-only monitor target."""
    reasons = {str(reason or "") for reason in (control_reasons or [])}
    diagnostics = control_diagnostics if isinstance(control_diagnostics, dict) else {}
    alpha_ev = diagnostics.get("alpha_setup_ev_fusion") if isinstance(diagnostics.get("alpha_setup_ev_fusion"), dict) else {}
    seed = diagnostics.get("conditional_monitor_probe_seed") if isinstance(diagnostics.get("conditional_monitor_probe_seed"), dict) else {}
    trade_authority = _alpha_ev_trade_authority(alpha_ev)
    reason_effects = reason_effect_summary(list(reasons))
    hard_blocks = sorted(
        set(reason_effects.get("hard_blocks") or [])
        | (reasons & _FINAL_ACTION_AUTHORITY_HARD_BLOCK_REASONS)
    )
    hard_zero = bool(reason_effects.get("hard_zero"))
    negative_profile = bool(
        alpha_ev.get("negative_action_value")
        or alpha_ev.get("negative_profile")
        or alpha_ev.get("repeat_loss_without_new_evidence")
    )
    blocked_reasons: list[str] = []
    if int(current_lots or 0) != 0 or int(target_lots or 0) != 0:
        blocked_reasons.append("not_flat_zero_target")
    if "pm_watch_for_trigger_probe_cap" not in reasons:
        blocked_reasons.append("missing_watch_for_trigger_candidate")
    if str(trade_authority.get("scorecard_state") or "").lower() != "watch_for_trigger":
        blocked_reasons.append("not_watch_for_trigger_state")
    if not bool(alpha_ev.get("setup_quality_ok")):
        blocked_reasons.append("setup_quality_not_met")
    if not bool(alpha_ev.get("has_monitorable_setup") or seed):
        blocked_reasons.append("missing_monitorable_setup")
    if bool(trade_authority.get("watch_for_trigger_without_setup")):
        blocked_reasons.append("watch_for_trigger_without_setup")
    if not bool(trade_authority.get("has_entry_invalidation")):
        blocked_reasons.append("missing_entry_invalidation")
    if not bool(trade_authority.get("has_position_exit_boundary")):
        blocked_reasons.append("missing_position_exit_boundary")
    if hard_zero or hard_blocks:
        blocked_reasons.append("hard_block_present")
    if negative_profile:
        blocked_reasons.append("negative_learning_profile_present")

    probe_side = _target_side_from_ratio(target_ratio)
    if probe_side not in {"long", "short"}:
        blocked_reasons.append("missing_target_side")
    one_lot_notional = float(current_price or 0.0) * abs(float(multiplier or 0.0))
    one_lot_position_ratio = one_lot_notional / max(float(account_equity or 0.0), 1.0)
    one_lot_margin = one_lot_notional * float(margin_rate or 0.0)
    if one_lot_notional <= 0 or float(account_equity or 0.0) <= 0:
        blocked_reasons.append("invalid_contract_or_equity")
    if one_lot_margin <= 0 or one_lot_margin > float(margin_available or 0.0) + 1e-12:
        blocked_reasons.append("one_lot_margin_not_feasible")
    signed_one_lot_ratio = one_lot_position_ratio if probe_side == "long" else -one_lot_position_ratio
    projected_net_after_probe = (
        float(current_net_exposure or 0.0)
        - float(current_ticker_exposure or 0.0)
        + signed_one_lot_ratio
    )
    if one_lot_position_ratio > float(max_position_ratio or 0.0) + 1e-12:
        blocked_reasons.append("one_lot_exceeds_position_ratio")
    if abs(projected_net_after_probe) > float(max_net_exposure or 0.0) + 1e-12:
        blocked_reasons.append("one_lot_exceeds_net_exposure")
    risk_budget = {}
    if not blocked_reasons and probe_side in {"long", "short"}:
        risk_budget = _one_lot_probe_risk_check(
            ticker=ticker,
            probe_side=probe_side,
            current_price=float(current_price),
            multiplier=float(multiplier),
            margin_rate=float(margin_rate),
            account_equity=float(account_equity),
            morning_price_context=morning_price_context,
            control_reasons=list(control_reasons or []),
            control_diagnostics=diagnostics,
            full_config=full_config,
        )
        if not risk_budget.get("passed", True):
            blocked_reasons.append("one_lot_risk_budget_not_feasible")

    allowed = not blocked_reasons
    return {
        "allowed": allowed,
        "decision": "allow_conditional_monitor_probe" if allowed else "watch_for_trigger",
        "blocked_reasons": blocked_reasons,
        "target_lots": (1 if probe_side == "long" else -1) if allowed else 0,
        "probe_side": probe_side,
        "signed_one_lot_ratio": signed_one_lot_ratio if allowed else 0.0,
        "target_value": one_lot_notional if probe_side == "long" and allowed else -one_lot_notional if allowed else 0.0,
        "margin_required": one_lot_margin if allowed else 0.0,
        "new_net_exposure": projected_net_after_probe if allowed else current_net_exposure,
        "requires_intraday_confirmation": True,
        "can_execute_without_intraday_trigger": False,
        "does_not_create_unconditional_execution": True,
        "risk_budget": risk_budget,
    }


def _qualified_analyst_tradeable_probe_candidate(
    *,
    analyst_signals: list,
    target_side: str,
    control_reasons: list[str],
    control_diagnostics: dict,
    account_equity: float,
    current_price: float,
    multiplier: float,
    margin_rate: float,
    margin_available: float,
) -> tuple[bool, dict]:
    """Preserve a current tradeable analyst candidate from being soft-gated to zero.

    This is not a trading authority and does not bypass final_action_contract.
    It only lets an already structured current setup remain eligible for the
    existing one-lot probe path after soft controls shrink the ratio to zero.
    """
    reasons = {str(reason or "") for reason in (control_reasons or [])}
    hard_blocks = set(reason_effect_summary(list(reasons)).get("hard_blocks") or [])
    negative_reasons = {
        "repeat_loss_watchlist_only",
        "negative_expectancy_cap_or_exit",
        "negative_expectancy_new_entry_watchlist_only",
        "tail_loss_protect",
        "negative_revalidate",
        "negative_hold_revalidate",
    }
    detail = {
        "enabled": True,
        "target_side": target_side,
        "matched_analysts": [],
        "blocked_reasons": [],
        "does_not_create_trade_authority": True,
        "requires_final_contract_authority": True,
        "keeps_watch_for_trigger_boundary": True,
    }
    if target_side not in {"long", "short"}:
        detail["blocked_reasons"].append("missing_target_side")
        return False, detail
    if hard_blocks or any(_hard_zero_reason(reason) for reason in reasons):
        detail["blocked_reasons"].append("hard_block_present")
    if reasons & negative_reasons:
        detail["blocked_reasons"].append("negative_or_tail_loss_present")
    alpha_ev = control_diagnostics.get("alpha_setup_ev_fusion") if isinstance(control_diagnostics, dict) else {}
    if isinstance(alpha_ev, dict) and (
        alpha_ev.get("negative_action_value")
        or alpha_ev.get("negative_profile")
        or alpha_ev.get("repeat_loss_without_new_evidence")
        or alpha_ev.get("tail_loss_blocks_real_amplification")
    ):
        detail["blocked_reasons"].append("negative_learning_profile_present")
    one_lot_margin = float(current_price or 0.0) * abs(float(multiplier or 0.0)) * float(margin_rate or 0.0)
    if one_lot_margin <= 0 or one_lot_margin > float(margin_available or 0.0) + 1e-12:
        detail["blocked_reasons"].append("one_lot_margin_not_feasible")
    if float(account_equity or 0.0) <= 0:
        detail["blocked_reasons"].append("account_equity_invalid")

    position_exit_boundary_present = any(
        _signal_side_text(getattr(signal, "signal", None)) == target_side
        and _has_position_exit_boundary([signal])
        for signal in analyst_signals or []
    )
    for signal in analyst_signals or []:
        signal_side = _signal_side_text(getattr(signal, "signal", None))
        if signal_side != target_side:
            continue
        state = str(getattr(signal, "opportunity_state", "") or "").strip().lower()
        trigger_valid = _canonical_trigger_valid(signal)
        entry_invalidation_present = _canonical_invalidation_present(signal)
        action_contract = _canonical_action_evidence_contract(signal)
        if action_contract:
            state = str(action_contract.get("opportunity_state") or state or "").strip().lower()
        tradeable_candidate = bool(
            state in {"probe_candidate", "tradeable_candidate"}
        )
        if (
            tradeable_candidate
            and trigger_valid
            and entry_invalidation_present
            and position_exit_boundary_present
        ):
            detail["matched_analysts"].append(
                {
                    "analyst": _normalize_agent_name(str(getattr(signal, "agent_name", "") or "unknown")),
                    "opportunity_state": state,
                    "trigger_valid": True,
                    "entry_invalidation_present": True,
                    "position_exit_boundary_present": True,
                    "side": signal_side,
                }
            )

    if not detail["matched_analysts"]:
        detail["blocked_reasons"].append("no_same_side_tradeable_triggered_analyst")
    allowed = not detail["blocked_reasons"]
    detail["decision"] = "allow_controlled_probe_candidate" if allowed else "watch_for_trigger"
    return allowed, detail


_FINAL_ACTION_AUTHORITY_WEAK_REASONS = {
    "alpha_setup_open_action_value_missing",
    "single_high_quality_probe_only",
    "horizon_consistency_probe_cap",
    "market_confirmation_quality_gate",
    "market_confirmation_conflict",
    "weak_signal_combo_probe_cap",
    "side_performance_probe_cap",
    "business_quality_probe_only",
    "business_quality_observe_or_block",
    "pm_risk_gate_soft_probe_floor",
    "controlled_probe_below_min_entry_kept",
    "unknown_alpha_probe",
}

_FINAL_ACTION_AUTHORITY_HARD_BLOCK_REASONS = {
    "repeat_loss_watchlist_only",
    "negative_expectancy_cap_or_exit",
    "negative_expectancy_new_entry_watchlist_only",
    "missing_pretrade_invalidation",
    "missing_position_exit_boundary",
}


def _step4_capital_plan(
    *,
    alpha_ev: dict,
    trade_authority: dict,
    control_diagnostics: dict,
    full_config: dict,
    can_real: bool,
    can_explore: bool,
) -> dict:
    """Choose the Step4 capital layer and its continuous margin plan.

    This runs before the all-market rank.  It uses only the final scorecard,
    current execution/invalidation facts and the frozen canonical open/add
    learning pool already summarized by ``alpha_setup_ev_fusion``.
    """
    budget = _position_budget_policy_config(full_config)
    ev_cfg = (_get_portfolio_manager_config(full_config).get("alpha_setup_ev_fusion") or {})
    mature_sample_count = int(
        ev_cfg.get(
            "real_trade_min_action_value_samples",
            ev_cfg.get("min_action_value_samples", 2),
        )
        or 2
    )
    quality = max(0.0, min(1.0, _safe_float(alpha_ev.get("candidate_quality"), 0.0)))
    action_stats = alpha_ev.get("action_value_stats") if isinstance(alpha_ev.get("action_value_stats"), dict) else {}
    capital_target = (
        control_diagnostics.get("capital_utilization_target")
        if isinstance(control_diagnostics.get("capital_utilization_target"), dict)
        else {}
    )
    scorecard_state = str(trade_authority.get("scorecard_state") or "").strip().lower()
    qualified_positive = bool(alpha_ev.get("positive_action_value"))
    mature_repeated_positive = bool(
        qualified_positive
        and alpha_ev.get("qualified_positive_expectancy")
        and int(action_stats.get("sample_count") or 0) >= mature_sample_count
        and _safe_float(action_stats.get("reward_sum"), 0.0) > 0.0
    )
    strong_confirmation = bool(
        trade_authority.get("strong_market_confirmation")
        or trade_authority.get("technical_confirmation")
        or trade_authority.get("current_setup_confirmation")
        or trade_authority.get("monitorable_setup_confirmation")
    )
    investment_setup_ready = bool(
        scorecard_state == "tradeable_candidate"
        or (
            scorecard_state == "watch_for_trigger"
            and trade_authority.get("monitorable_setup_confirmation")
        )
    )
    alpha_scale = bool(
        can_real
        and mature_repeated_positive
        and investment_setup_ready
        and strong_confirmation
        and trade_authority.get("has_entry_invalidation")
        and trade_authority.get("has_position_exit_boundary")
        and not trade_authority.get("technical_opposes_side")
        and alpha_ev.get("fundamental_supports_side")
        and not alpha_ev.get("fundamental_opposes_side")
    )
    exceptional = bool(
        alpha_scale
        and str(capital_target.get("target_mode") or "").lower() == "alpha_release_max_boost"
    )
    if alpha_scale:
        layer = CAPITAL_LAYER_ALPHA_SCALE
        if exceptional:
            lower = _safe_float(budget.get("exceptional_margin_ratio"), 0.075)
            upper = _safe_float(budget.get("exceptional_margin_max_ratio"), 0.130)
            ratio_source = "exceptional_margin_ratio"
        else:
            lower = _safe_float(budget.get("deployable_margin_ratio"), 0.060)
            upper = _safe_float(budget.get("deployable_margin_max_ratio"), 0.120)
            ratio_source = "deployable_margin_ratio"
    elif can_real and qualified_positive:
        layer = CAPITAL_LAYER_REAL_BUDGET
        lower = _safe_float(budget.get("normal_trade_margin_ratio"), 0.030)
        upper = _safe_float(budget.get("normal_trade_margin_max_ratio"), 0.060)
        ratio_source = "normal_trade_margin_ratio"
    elif can_explore:
        layer = CAPITAL_LAYER_EXPLORATION
        lower = _safe_float(budget.get("probe_margin_ratio"), 0.008)
        upper = _safe_float(budget.get("probe_margin_max_ratio"), 0.015)
        ratio_source = "probe_margin_ratio"
    else:
        return {
            "capital_layer": "",
            "capital_ratio_source": "",
            "candidate_quality": quality,
            "target_margin_ratio": 0.0,
            "max_margin_ratio": 0.0,
            "alpha_scale_eligible": False,
            "exceptional_validated": False,
        }
    lower = max(0.0, lower)
    upper = max(lower, upper)
    return {
        "capital_layer": layer,
        "capital_ratio_source": ratio_source,
        "candidate_quality": quality,
        "target_margin_ratio": lower + quality * (upper - lower),
        "max_margin_ratio": upper,
        "alpha_scale_eligible": alpha_scale,
        "exceptional_validated": exceptional,
    }


def _final_contract_authority(
    *,
    control_reasons: list[str],
    control_diagnostics: dict,
    full_config: dict | None = None,
) -> tuple[bool, dict]:
    """Final PM outlet gate for turning a new idea into real lots.

    Upstream controls can cap or seed probes, but the final action must still
    prove it has trading authority. This prevents direction-only ideas from
    leaking into real one-lot opens while preserving confirmed/positive setups.
    """
    reasons = [str(reason or "") for reason in control_reasons or []]
    reason_set = set(reasons)
    budget_cfg = _position_budget_policy_config(full_config or {})
    market_cfg = (full_config or {}).get("market_confirmation") or {}
    analyst_policy = (full_config or {}).get("analyst_weight_policy") or {}
    alpha_ev = control_diagnostics.get("alpha_setup_ev_fusion") if isinstance(control_diagnostics, dict) else {}
    if not isinstance(alpha_ev, dict):
        alpha_ev = {}
    analyst_probe_detail = (
        control_diagnostics.get("analyst_tradeable_probe_candidate")
        if isinstance(control_diagnostics, dict)
        else {}
    )
    analyst_tradeable_probe_candidate = bool(
        isinstance(analyst_probe_detail, dict)
        and analyst_probe_detail.get("decision") == "allow_controlled_probe_candidate"
        and analyst_probe_detail.get("matched_analysts")
    )
    if analyst_tradeable_probe_candidate:
        alpha_ev = dict(alpha_ev)
        alpha_ev["analyst_tradeable_probe_candidate"] = True
        alpha_ev["has_tradeable_support"] = True
        alpha_ev["has_entry_invalidation"] = True
        alpha_ev["has_position_exit_boundary"] = True
        if not alpha_ev.get("scorecard_state"):
            alpha_ev["scorecard_state"] = "probe_candidate"
    reason_effects = reason_effect_summary(reasons)
    weak_markers = sorted(reason_set & _FINAL_ACTION_AUTHORITY_WEAK_REASONS)
    hard_blocks = sorted(set(reason_set & _FINAL_ACTION_AUTHORITY_HARD_BLOCK_REASONS) | set(reason_effects.get("hard_blocks") or []))
    hard_zero = bool(reason_effects.get("hard_zero"))
    negative_profile = bool(
        alpha_ev.get("negative_action_value")
        or alpha_ev.get("negative_profile")
        or alpha_ev.get("repeat_loss_without_new_evidence")
    )
    qualified_positive = bool(
        alpha_ev.get("qualified_positive_expectancy")
        or alpha_ev.get("positive_action_value")
        or alpha_ev.get("positive_profile")
        or "qualified_positive_expectancy" in reason_set
        or "positive_expectancy_scale" in reason_set
    )
    open_action_missing = bool(alpha_ev.get("open_action_value_missing"))
    release = "real_probe_positive_or_strong_confirmation_release" in reason_set
    trade_authority = _alpha_ev_trade_authority(alpha_ev)
    strong_realtime = bool(trade_authority.get("strong_realtime_evidence"))
    strong_market = bool(trade_authority.get("strong_market_confirmation"))
    confirmation_score = float(trade_authority.get("confirmation_score") or 0.0)
    independent_support_count = int(trade_authority.get("independent_support_count") or 0)
    current_setup_confirmation = bool(trade_authority.get("current_setup_confirmation"))
    event_catalyst_confirmation = bool(trade_authority.get("event_catalyst_confirmation"))
    strong_current_evidence = bool(
        trade_authority.get("market_confirmation")
        or trade_authority.get("technical_confirmation")
        or trade_authority.get("executable_setup_confirmation")
        or trade_authority.get("monitorable_setup_confirmation")
        or event_catalyst_confirmation
    )
    open_action_evidence = bool(trade_authority.get("open_action_evidence"))
    watch_for_trigger_without_setup = bool(trade_authority.get("watch_for_trigger_without_setup"))
    scorecard_state = str(trade_authority.get("scorecard_state") or "").lower()
    market_conflict = "market_confirmation_conflict" in reason_set
    weak_conflict_probe = bool(
        market_conflict
        and not trade_authority.get("market_confirmation")
        and scorecard_state in {"watch_for_trigger", "no_opportunity", "unknown", ""}
        and not current_setup_confirmation
        and not event_catalyst_confirmation
    )
    conditional_trigger_authority = bool(
        "pm_watch_for_trigger_probe_cap" in reason_set
        and scorecard_state == "watch_for_trigger"
        and bool(alpha_ev.get("setup_quality_ok"))
        and bool(alpha_ev.get("has_monitorable_setup") or control_diagnostics.get("conditional_monitor_probe_seed"))
        and not watch_for_trigger_without_setup
        and bool(trade_authority.get("has_entry_invalidation"))
        and bool(trade_authority.get("has_position_exit_boundary"))
        and not hard_zero
        and not hard_blocks
        and not negative_profile
    )
    tradeable_state = scorecard_state in {"probe_candidate", "tradeable_candidate"} or conditional_trigger_authority
    watch_for_trigger_semantic_block = bool(
        analyst_policy.get("watch_for_trigger_cannot_open_position", True)
        and not conditional_trigger_authority
        and (
            "pm_watch_for_trigger_probe_cap" in reason_set
            or "watch_for_trigger_cannot_open_position" in reason_set
            or "daily_tradeability_watchlist_only" in reason_set
            or scorecard_state in {"watch_for_trigger", "no_opportunity", "unknown", ""}
            or watch_for_trigger_without_setup
        )
    )
    watch_for_trigger_block = bool(
        watch_for_trigger_semantic_block
    )
    prior_only_mode = str(analyst_policy.get("static_weights_mode") or analyst_policy.get("mode") or "").lower() in {
        "prior_only",
        "evidence_router",
    }
    static_weights_can_open = bool(analyst_policy.get("static_weights_can_create_trade_authority", False))
    watch_for_trigger_cfg = (
        ((_get_holding_rebalance_config(full_config or {}) or {}).get("watch_for_trigger_new_entry") or {})
        if isinstance(full_config, dict)
        else {}
    )
    watch_for_trigger_semantic_audit = {
        "config_key": "portfolio_manager.holding_rebalance_control.watch_for_trigger_new_entry",
        "audit_name": str(watch_for_trigger_cfg.get("audit_name") or "watch_for_trigger_observation_candidate"),
        "semantic_role": str(watch_for_trigger_cfg.get("semantic_role") or "observation_candidate_only"),
        "allow_probe": bool(watch_for_trigger_cfg.get("allow_probe", False)),
        "can_create_trade_authority": bool(watch_for_trigger_cfg.get("can_create_trade_authority", False)),
        "requires_final_contract_authority": bool(
            watch_for_trigger_cfg.get("requires_final_contract_authority", True)
        ),
        "boundary": (
            "direction-only may create only an audited candidate/probe-sized intent; "
            "real lots still require final_action_contract authority and cannot be created by this key alone"
        ),
    }
    weak_only = bool(weak_markers or open_action_missing or watch_for_trigger_block)
    release_qualified = bool(release and (qualified_positive or strong_current_evidence))
    requires_authority = bool(hard_zero or hard_blocks or weak_markers or negative_profile or watch_for_trigger_block or open_action_missing)
    can_real = bool(
        not hard_zero
        and not hard_blocks
        and not negative_profile
        and not weak_conflict_probe
        and not open_action_missing
        and not watch_for_trigger_block
        and open_action_evidence
        and tradeable_state
        and bool(alpha_ev.get("positive_action_value"))
        and strong_current_evidence
        and trade_authority.get("has_entry_invalidation")
        and trade_authority.get("has_position_exit_boundary")
        and not alpha_ev.get("fundamental_opposes_side")
    )
    can_explore = bool(
        not hard_zero
        and not hard_blocks
        and (not weak_conflict_probe or conditional_trigger_authority)
        and not watch_for_trigger_block
        and tradeable_state
        and trade_authority.get("has_entry_invalidation")
        and trade_authority.get("has_position_exit_boundary")
        and (
            (release_qualified and open_action_evidence)
            or strong_current_evidence
            or analyst_tradeable_probe_candidate
            or conditional_trigger_authority
        )
    )
    if (
        prior_only_mode
        and not static_weights_can_open
        and not open_action_evidence
        and not conditional_trigger_authority
    ):
        can_real = False
        can_explore = False
    capital_plan = _step4_capital_plan(
        alpha_ev=alpha_ev,
        trade_authority=trade_authority,
        control_diagnostics=control_diagnostics if isinstance(control_diagnostics, dict) else {},
        full_config=full_config or {},
        can_real=can_real,
        can_explore=can_explore,
    )
    analyst_prior_audit = {
        "config_file": "analyst_prior_profiles.yaml",
        "runtime_compat_fields": ["portfolio_manager.sector_weights", "portfolio_manager.strategic_view_weights"],
        "semantic_role": "cold_start_prior_only",
        "can_create_trade_authority": bool(static_weights_can_open),
        "can_open_position_directly": False,
        "static_weights_mode": analyst_policy.get("static_weights_mode") or analyst_policy.get("mode") or "prior_only",
        "prior_only_mode": prior_only_mode,
        "reason": (
            "analyst priors can rank/contextualize signals, but PM entry authority "
            "must come from action evidence, current confirmation, and risk controls"
        ),
    }
    if can_real:
        authority_type = "real_budget_entry"
        decision = (
            "allow_alpha_scale_new_risk"
            if capital_plan.get("capital_layer") == CAPITAL_LAYER_ALPHA_SCALE
            else "allow_real_new_entry"
        )
        max_allowed_margin_ratio = min(
            _safe_float(capital_plan.get("max_margin_ratio"), 0.0),
            float(budget_cfg.get("hard_max_total_margin_ratio") or 0.20),
        )
    elif can_explore:
        authority_type = "exploration_probe"
        decision = "allow_exploration_probe"
        max_allowed_margin_ratio = min(
            _safe_float(capital_plan.get("max_margin_ratio"), 0.0),
            float(budget_cfg.get("hard_max_total_margin_ratio") or 0.20),
        )
    else:
        authority_type = "watchlist_only" if requires_authority else "not_applicable"
        decision = "watchlist_only" if requires_authority else "not_applicable"
        max_allowed_margin_ratio = 0.0
    has_authority = bool(can_real or can_explore)
    reason_codes = sorted(
        set(
            weak_markers
            + hard_blocks
            + list(reason_effects.get("hard_blocks") or [])
            + list(reason_effects.get("candidate_reasons") or [])
            + list(reason_effects.get("soft_limits") or [])
            + list(reason_effects.get("learning_adjustments") or [])
            + list(reason_effects.get("release_signals") or [])
            + [reason for reason in reasons if reason in {
                "qualified_positive_expectancy",
                "positive_expectancy_scale",
                "real_probe_positive_or_strong_confirmation_release",
                "alpha_setup_open_action_value_missing",
                "market_confirmation_quality_gate",
                "market_confirmation_conflict",
                "horizon_consistency_probe_cap",
            }]
            + (["watch_for_trigger_cannot_open_position"] if watch_for_trigger_block else [])
            + (["watch_for_trigger_semantic_release_block"] if watch_for_trigger_semantic_block and release else [])
            + (["weak_conflict_probe_requires_stronger_confirmation"] if weak_conflict_probe else [])
            + (["negative_expectancy"] if negative_profile else [])
            + (["hard_zero"] if hard_zero else [])
            + (["analyst_tradeable_probe_candidate"] if analyst_tradeable_probe_candidate else [])
            + (["conditional_trigger_authority"] if conditional_trigger_authority else [])
            + (["step4_alpha_scale_release"] if capital_plan.get("capital_layer") == CAPITAL_LAYER_ALPHA_SCALE else [])
        )
    )
    return has_authority, {
        "requires_authority": requires_authority,
        "decision": decision,
        "authority_type": authority_type,
        "max_allowed_margin_ratio": float(max_allowed_margin_ratio),
        "reason_codes": reason_codes,
        "source_parameters": {
            "position_budget_policy": {
                "min_real_trade_margin_ratio": budget_cfg.get("min_real_trade_margin_ratio"),
                "min_real_trade_margin_abs": budget_cfg.get("min_real_trade_margin_abs"),
                "probe_margin_ratio": budget_cfg.get("probe_margin_ratio"),
                "probe_margin_max_ratio": budget_cfg.get("probe_margin_max_ratio"),
                "normal_trade_margin_ratio": budget_cfg.get("normal_trade_margin_ratio"),
                "normal_trade_margin_max_ratio": budget_cfg.get("normal_trade_margin_max_ratio"),
                "hard_max_total_margin_ratio": budget_cfg.get("hard_max_total_margin_ratio"),
                "block_below_min_when_cannot_scale": budget_cfg.get("block_below_min_when_cannot_scale"),
            },
            "analyst_weight_policy": {
                "watch_for_trigger_cannot_open_position": bool(analyst_policy.get("watch_for_trigger_cannot_open_position", True)),
                "strategic_view_cannot_open_position": bool(analyst_policy.get("strategic_view_cannot_open_position", True)),
                "static_weights_mode": analyst_policy.get("static_weights_mode") or analyst_policy.get("mode") or "prior_only",
                "static_weights_can_create_trade_authority": bool(
                    analyst_policy.get("static_weights_can_create_trade_authority", False)
                ),
            },
            "market_confirmation": {
                "min_confirmation_score_for_new_entry": market_cfg.get("min_confirmation_score_for_new_entry"),
            },
            "watch_for_trigger_new_entry": watch_for_trigger_semantic_audit,
        },
        "watch_for_trigger_cannot_open_position": bool(analyst_policy.get("watch_for_trigger_cannot_open_position", True)),
        "strategic_view_cannot_open_position": bool(analyst_policy.get("strategic_view_cannot_open_position", True)),
        "market_confirmation_policy": {
            "min_confirmation_score_for_new_entry": market_cfg.get("min_confirmation_score_for_new_entry"),
            "current_confirmation_score": confirmation_score,
        },
        "probe_margin_ratio": budget_cfg.get("probe_margin_ratio"),
        "min_real_trade_margin_ratio": budget_cfg.get("min_real_trade_margin_ratio"),
        "weak_markers": weak_markers,
        "hard_blocks": hard_blocks,
        "hard_zero": hard_zero,
        "reason_effects": reason_effects,
        "unknown_trade_effects": reason_effects.get("unknown_trade_effects") or [],
        "negative_profile": negative_profile,
        "open_action_value_missing": open_action_missing,
        "watch_for_trigger_block": watch_for_trigger_block,
        "watch_for_trigger_semantic_block": watch_for_trigger_semantic_block,
        "conditional_trigger_authority": conditional_trigger_authority,
        "requires_intraday_confirmation": bool(conditional_trigger_authority),
        "can_execute_without_intraday_trigger": False if conditional_trigger_authority else None,
        "tradeable_state": tradeable_state,
        "opportunity_state_required": True,
        "weak_conflict_probe": weak_conflict_probe,
        "weak_only": weak_only,
        "release": release,
        "release_qualified": release_qualified,
        "capital_layer": capital_plan.get("capital_layer") or "",
        "capital_ratio_source": capital_plan.get("capital_ratio_source") or "",
        "candidate_quality": _safe_float(capital_plan.get("candidate_quality"), 0.0),
        "target_margin_ratio": _safe_float(capital_plan.get("target_margin_ratio"), 0.0),
        "alpha_scale_eligible": bool(capital_plan.get("alpha_scale_eligible")),
        "exceptional_validated": bool(capital_plan.get("exceptional_validated")),
        "qualified_positive": qualified_positive,
        "analyst_tradeable_probe_candidate": analyst_tradeable_probe_candidate,
        "strong_realtime_evidence": strong_realtime,
        "strong_market_confirmation": strong_market,
        "strong_current_evidence": strong_current_evidence,
        "has_entry_invalidation": bool(trade_authority.get("has_entry_invalidation")),
        "has_position_exit_boundary": bool(trade_authority.get("has_position_exit_boundary")),
        "confirmation_score": confirmation_score,
        "independent_support_count": independent_support_count,
        "market_confirmation": trade_authority.get("market_confirmation"),
        "technical_confirmation": trade_authority.get("technical_confirmation"),
        "monitorable_setup_confirmation": trade_authority.get("monitorable_setup_confirmation"),
        "event_catalyst_confirmation": trade_authority.get("event_catalyst_confirmation"),
        "open_action_evidence": open_action_evidence,
        "executable_setup_confirmation": trade_authority.get("executable_setup_confirmation"),
        "watch_for_trigger_without_setup": trade_authority.get("watch_for_trigger_without_setup"),
        "watch_for_trigger_without_confirmation": trade_authority.get("watch_for_trigger_without_confirmation"),
        "watch_for_trigger_semantic_audit": watch_for_trigger_semantic_audit,
        "analyst_prior_policy": {
            "static_weights_mode": analyst_policy.get("static_weights_mode") or analyst_policy.get("mode") or "prior_only",
            "static_weights_can_create_trade_authority": static_weights_can_open,
            "prior_only_mode": prior_only_mode,
        },
        "analyst_prior_audit": analyst_prior_audit,
        "action_evidence_router": trade_authority.get("action_evidence_router"),
        "money_objective": "route_entries_by_action_evidence_not_static_weighted_direction",
    }


def extract_underlying_code(ticker: str) -> str:
    """Extract the underlying futures code from a ticker."""
    match = re.match(r'^([A-Z]+)', ticker.upper())
    return match.group(1) if match else ticker


def _normalize_agent_name(agent_name: str) -> str:
    return str(agent_name or "")


def _normalize_weights(weights: dict) -> dict:
    cleaned = {key: max(0.0, float(weights.get(key, 0.0) or 0.0)) for key in ANALYST_ORDER}
    total = sum(cleaned.values())
    if total <= 0:
        return DEFAULT_SECTOR_WEIGHTS["generic"].copy()
    return {key: value / total for key, value in cleaned.items()}


def _sector_for_ticker(ticker: str) -> str:
    return SECTOR_BY_TICKER.get(extract_underlying_code(ticker).upper(), "generic")


def _get_portfolio_manager_config(full_config: dict) -> dict:
    return full_config.get("portfolio_manager", {}) or {}


def _get_holding_rebalance_config(full_config: dict) -> dict:
    configured = (_get_portfolio_manager_config(full_config).get("holding_rebalance_control") or {})
    merged = {
        key: value
        for key, value in DEFAULT_HOLDING_REBALANCE_CONTROL.items()
        if key != "min_hold_days_by_sector"
    }
    merged.update({key: value for key, value in configured.items() if key != "min_hold_days_by_sector"})
    if isinstance(DEFAULT_HOLDING_REBALANCE_CONTROL.get("position_lifecycle"), dict):
        lifecycle_defaults = DEFAULT_HOLDING_REBALANCE_CONTROL["position_lifecycle"].copy()
        lifecycle_defaults.update(configured.get("position_lifecycle") or {})
        merged["position_lifecycle"] = lifecycle_defaults
    if isinstance(DEFAULT_HOLDING_REBALANCE_CONTROL.get("horizon_consistency"), dict):
        horizon_defaults = DEFAULT_HOLDING_REBALANCE_CONTROL["horizon_consistency"].copy()
        horizon_defaults.update(configured.get("horizon_consistency") or {})
        merged["horizon_consistency"] = horizon_defaults
    if isinstance(DEFAULT_HOLDING_REBALANCE_CONTROL.get("daily_tradeability_gate"), dict):
        daily_gate_defaults = DEFAULT_HOLDING_REBALANCE_CONTROL["daily_tradeability_gate"].copy()
        daily_gate_defaults.update(configured.get("daily_tradeability_gate") or {})
        merged["daily_tradeability_gate"] = daily_gate_defaults
    sector_days = DEFAULT_HOLDING_REBALANCE_CONTROL["min_hold_days_by_sector"].copy()
    sector_days.update(configured.get("min_hold_days_by_sector") or {})
    merged["min_hold_days_by_sector"] = sector_days
    return merged


def _sector_base_weights(ticker: str, full_config: dict) -> dict:
    sector = _sector_for_ticker(ticker)
    configured = (
        (_get_portfolio_manager_config(full_config).get("sector_weights") or {}).get(sector)
        or DEFAULT_SECTOR_WEIGHTS.get(sector)
        or DEFAULT_SECTOR_WEIGHTS["generic"]
    )
    return _normalize_weights(configured)


def _signal_to_text(value) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _signal_metadata(signal) -> dict:
    metadata = getattr(signal, "metadata", {}) or {}
    return metadata if isinstance(metadata, dict) else {}


def _nested_context(metadata: dict, agent_name: str) -> dict:
    for key in (
        f"{agent_name}_context",
        "technical_context",
        "fundamental_context",
        "news_context",
    ):
        value = metadata.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _canonical_action_evidence_contract(signal) -> dict:
    metadata = _signal_metadata(signal)
    contract = metadata.get("action_evidence_contract")
    return contract if isinstance(contract, dict) else {}


def _canonical_trigger_valid(signal) -> bool:
    contract = _canonical_action_evidence_contract(signal)
    return bool(contract.get("trigger_valid")) if "trigger_valid" in contract else False


def _canonical_current_trigger_confirmed(signal) -> bool:
    contract = _canonical_action_evidence_contract(signal)
    return bool(contract.get("current_trigger_confirmed")) if "current_trigger_confirmed" in contract else False


def _canonical_invalidation_present(signal) -> bool:
    contract = _canonical_action_evidence_contract(signal)
    if "invalidation_present" in contract:
        return bool(contract.get("invalidation_present"))
    return bool(getattr(signal, "invalidation_present", False))


def _canonical_contract_text(signal, key: str, default: str = "") -> str:
    contract = _canonical_action_evidence_contract(signal)
    value = contract.get(key) if isinstance(contract, dict) else None
    if value is None or value == "":
        return default
    return str(value)


def _pm_has_invalidation_contract(signal, metadata: dict | None = None) -> bool:
    metadata = metadata if isinstance(metadata, dict) else _signal_metadata(signal)
    action_contract = metadata.get("action_evidence_contract") if isinstance(metadata.get("action_evidence_contract"), dict) else {}
    return bool(action_contract.get("invalidation_present"))


def _derive_signal_contract_fields(signal, agent_name: str) -> dict:
    metadata = _signal_metadata(signal)
    context = _nested_context(metadata, agent_name)
    action_contract = _canonical_action_evidence_contract(signal)
    learning_scope = (
        action_contract.get("learning_scope")
        if isinstance(action_contract.get("learning_scope"), dict)
        else {}
    )
    entry_trigger = action_contract.get("entry_trigger") or ""
    invalidation_condition = (
        action_contract.get("invalidation_condition")
        or ""
    )
    invalidation_present = _pm_has_invalidation_contract(signal, metadata)
    signal_text = _signal_to_text(getattr(signal, "signal", "Neutral"))
    direction_context = getattr(signal, "direction_context", "") or (
        "long" if signal_text.upper() == "BULLISH" else "short" if signal_text.upper() == "BEARISH" else "neutral"
    )
    horizon_class = (
        action_contract.get("horizon_class")
        or getattr(signal, "horizon_class", "")
        or getattr(signal, "holding_period_hint", "")
        or "unknown"
    )
    if agent_name == "technical":
        evidence_role = getattr(signal, "evidence_role", "") or "entry_timing"
        trend_direction = (
            getattr(signal, "trend_direction", "")
            or context.get("dominant_direction")
            or getattr(signal, "trend_stage", "")
            or direction_context
        )
        trigger_valid = _canonical_trigger_valid(signal)
        entry_timing_signal = action_contract.get("entry_timing_signal") or ""
        price_location = (
            getattr(signal, "price_location", "")
            or str(getattr(signal, "price_percentile", "") or "")
            or str((context.get("features") or {}).get("price_range") if isinstance(context.get("features"), dict) else "")
        )
    elif agent_name == "fundamental":
        evidence_role = getattr(signal, "evidence_role", "") or "direction_context"
        trend_direction = getattr(signal, "trend_direction", "") or direction_context
        trigger_valid = _canonical_trigger_valid(signal)
        entry_timing_signal = action_contract.get("entry_timing_signal") or ""
        price_location = getattr(signal, "price_location", "") or ""
    else:
        evidence_role = getattr(signal, "evidence_role", "") or "event_catalyst"
        trend_direction = getattr(signal, "trend_direction", "") or direction_context
        trigger_valid = _canonical_trigger_valid(signal)
        entry_timing_signal = action_contract.get("entry_timing_signal") or ""
        price_location = getattr(signal, "price_location", "") or ""
    return {
        "evidence_role": evidence_role,
        "direction_context": direction_context,
        "opportunity_state": _signal_opportunity_state(signal),
        "entry_trigger": entry_trigger,
        "invalidation": invalidation_condition,
        "invalidation_condition": invalidation_condition,
        "invalidation_level": action_contract.get("invalidation_level"),
        "position_invalidation_level": action_contract.get("position_invalidation_level"),
        "exit_hint": action_contract.get("exit_hint"),
        "atr_stop_distance": action_contract.get("atr_stop_distance"),
        "expected_horizon_days": action_contract.get("expected_horizon_days"),
        "horizon_class": horizon_class,
        "trend_direction": trend_direction,
        "entry_timing_signal": entry_timing_signal,
        "price_location": price_location,
        "trigger_valid": bool(trigger_valid),
        "current_trigger_confirmed": _canonical_current_trigger_confirmed(signal),
        "invalidation_present": bool(invalidation_present),
    }


def _signal_tradeability(signal, agent_name: str) -> str:
    metadata = _signal_metadata(signal)
    context = _nested_context(metadata, agent_name)
    return str(metadata.get("tradeability") or context.get("tradeability") or "unknown").lower()


def _signal_risk_flags(signal, agent_name: str) -> list:
    metadata = _signal_metadata(signal)
    context = _nested_context(metadata, agent_name)
    flags = metadata.get("risk_flags") or context.get("risk_flags") or []
    return [str(flag) for flag in flags] if isinstance(flags, list) else []


def _signal_opportunity_state(signal) -> str:
    metadata = _signal_metadata(signal)
    action_contract = metadata.get("action_evidence_contract") if isinstance(metadata.get("action_evidence_contract"), dict) else {}
    state = (
        action_contract.get("opportunity_state")
        or getattr(signal, "opportunity_state", None)
        or ""
    )
    text = str(state or "").strip().lower()
    if text in {
        "no_opportunity",
        "watch_for_trigger",
        "probe_candidate",
        "tradeable_candidate",
        "risk_reduction_candidate",
    }:
        return text
    return "watch_for_trigger"


def _opportunity_state_multiplier(signal, full_config: dict) -> tuple[float, str]:
    pm_config = _get_portfolio_manager_config(full_config)
    quality_config = pm_config.get("quality_aware_fusion", {}) or {}
    configured = quality_config.get("opportunity_state_multipliers") or {}
    multipliers = {
        "tradeable_candidate": 1.10,
        "probe_candidate": 0.90,
        "watch_for_trigger": 0.30,
        "risk_reduction_candidate": 0.80,
        "no_opportunity": 0.05,
        "unknown": 0.50,
        **configured,
    }
    state = _signal_opportunity_state(signal)
    return float(multipliers.get(state, multipliers.get("unknown", 0.50))), state


def _quality_multiplier(signal, agent_name: str, full_config: dict) -> tuple[float, str, list]:
    pm_config = _get_portfolio_manager_config(full_config)
    quality_config = pm_config.get("quality_aware_fusion", {}) or {}
    multipliers = {**DEFAULT_QUALITY_MULTIPLIERS, **(quality_config.get("tradeability_multipliers") or {})}
    tradeability = _signal_tradeability(signal, agent_name)
    risk_flags = _signal_risk_flags(signal, agent_name)
    multiplier = float(multipliers.get(tradeability, multipliers.get("unknown", 0.75)))

    severe_flags = set(
        quality_config.get(
            "severe_risk_flags",
            [
                "low_coverage",
                "stale_fundamental_inputs",
                "missing_fundamental_inputs",
                "conflicting_indicators",
                "mixed_news_direction",
                "thin_news_sample",
            ],
        )
    )
    if severe_flags.intersection(risk_flags):
        multiplier *= float(quality_config.get("severe_risk_flag_multiplier", 0.80))

    state_multiplier, state = _opportunity_state_multiplier(signal, full_config)
    multiplier *= state_multiplier
    if state in {"watch_for_trigger", "no_opportunity"}:
        risk_flags = list(risk_flags)
        note = f"opportunity_state_{state}"
        if note not in risk_flags:
            risk_flags.append(note)

    business_cfg = full_config.get("analyst_business_quality", {}) or {}
    if business_cfg.get("enabled", True):
        business_score = _safe_float(getattr(signal, "business_quality_score", 0.0), 0.0)
        min_probe = _safe_float(business_cfg.get("min_score_for_probe"), 0.45)
        min_deploy = _safe_float(business_cfg.get("min_score_for_deployable"), 0.60)
        if business_score < min_probe:
            multiplier *= 0.10
        elif business_score < min_deploy:
            multiplier *= 0.55

    min_multiplier = float(quality_config.get("min_quality_multiplier", 0.05))
    return max(min_multiplier, multiplier), tradeability, risk_flags


def _effective_signal_confidence(signal, agent_name: str, quality_summary: dict, full_config: dict) -> float:
    raw_confidence = float(getattr(signal, "confidence", 0.0) or 0.0)
    pm_config = _get_portfolio_manager_config(full_config)
    quality_config = pm_config.get("quality_aware_fusion", {}) or {}
    caps = quality_config.get("confidence_caps") or {
        "high": 1.00,
        "medium": 0.65,
        "low": 0.35,
        "unknown": 0.55,
    }
    tradeability = quality_summary.get(agent_name, {}).get("tradeability", "unknown")
    cap = float(caps.get(tradeability, caps.get("unknown", 0.55)))
    state_multiplier, _state = _opportunity_state_multiplier(signal, full_config)
    return min(raw_confidence, cap) * max(0.0, min(1.25, state_multiplier))


def _quality_aware_fusion_context(
    ticker: str,
    analyst_signals: list,
    dynamic_weights: dict,
    full_config: dict,
) -> dict:
    pm_config = _get_portfolio_manager_config(full_config)
    adaptive_config = pm_config.get("adaptive_fusion", {}) or {}
    sector = _sector_for_ticker(ticker)
    sector_weights = _sector_base_weights(ticker, full_config)
    dynamic_weights = _normalize_weights(dynamic_weights or sector_weights)
    sector_weight = float(adaptive_config.get("sector_weight", 0.40))
    sector_weight = max(0.0, min(1.0, sector_weight))
    blended = {
        key: sector_weights[key] * sector_weight + dynamic_weights[key] * (1.0 - sector_weight)
        for key in ANALYST_ORDER
    }
    blended = _normalize_weights(blended)

    signals_by_agent = {}
    for signal in analyst_signals:
        if hasattr(signal, "agent_name"):
            agent_name = _normalize_agent_name(signal.agent_name)
            if agent_name in ANALYST_ORDER:
                signals_by_agent[agent_name] = signal

    applicability_profile = full_config.get("analyst_applicability_profile", {}) or {}
    applicability_adjustments = {}
    if applicability_profile.get("enabled", False):
        sector = _sector_for_ticker(ticker)
        for agent_name in ANALYST_ORDER:
            signal = signals_by_agent.get(agent_name)
            profile = (applicability_profile.get(agent_name) or {})
            multiplier = 1.0
            horizon = str(getattr(signal, "horizon_class", "") or profile.get("default_horizon") or "unknown")
            market_regime = str(getattr(signal, "market_regime", "") or "unknown")
            multiplier *= float((profile.get("horizon_multipliers") or {}).get(horizon, 1.0))
            multiplier *= float((profile.get("sector_multipliers") or {}).get(sector, 1.0))
            multiplier *= float((profile.get("market_regime_multipliers") or {}).get(market_regime, 1.0))
            if agent_name == "commodity_news":
                event_window_days = int(profile.get("event_window_days", 3) or 3)
                expected_days = int(getattr(signal, "expected_horizon_days", 0) or 0) if signal else 0
                if expected_days and expected_days > event_window_days:
                    multiplier *= float(profile.get("outside_event_window_multiplier", 0.60))
            if abs(multiplier - 1.0) > 1e-9:
                before = blended.get(agent_name, 0.0)
                blended[agent_name] = before * multiplier
                applicability_adjustments[agent_name] = {
                    "horizon_class": horizon,
                    "market_regime": market_regime,
                    "sector": sector,
                    "multiplier": multiplier,
                    "weight_before": before,
                    "weight_after": blended[agent_name],
                }
        blended = _normalize_weights(blended)

    quality_summary = {}
    adjusted = {}
    for agent_name in ANALYST_ORDER:
        signal = signals_by_agent.get(agent_name)
        if not signal:
            quality_summary[agent_name] = {
                "signal": "Neutral",
                "raw_confidence": 0.0,
                "effective_confidence": 0.0,
                "tradeability": "missing",
                "risk_flags": ["missing_signal"],
                "quality_multiplier": 0.0,
            }
            adjusted[agent_name] = 0.0
            continue

        quality_multiplier, tradeability, risk_flags = _quality_multiplier(signal, agent_name, full_config)
        effective_confidence = _effective_signal_confidence(
            signal=signal,
            agent_name=agent_name,
            quality_summary={agent_name: {"tradeability": tradeability}},
            full_config=full_config,
        )
        adjusted[agent_name] = blended.get(agent_name, 0.0) * quality_multiplier
        quality_summary[agent_name] = {
            "signal": _signal_to_text(getattr(signal, "signal", "Neutral")),
            "raw_confidence": float(getattr(signal, "confidence", 0.0) or 0.0),
            "effective_confidence": effective_confidence,
            "tradeability": tradeability,
            "business_quality_score": _safe_float(getattr(signal, "business_quality_score", 0.0), 0.0),
            "setup_type": getattr(signal, "setup_type", "unknown"),
            "horizon_class": getattr(signal, "horizon_class", "unknown"),
            "opportunity_state": _signal_opportunity_state(signal),
            "opportunity_type": getattr(signal, "opportunity_type", "unknown"),
            "risk_flags": risk_flags,
            "quality_multiplier": quality_multiplier,
        }

    adjusted = _normalize_weights(adjusted)
    return {
        "sector": sector,
        "sector_weights": sector_weights,
        "dynamic_weights": dynamic_weights,
        "quality_adjusted_weights": adjusted,
        "analyst_quality": quality_summary,
        "analyst_applicability_profile": applicability_adjustments,
        "llm_path": {
            "portfolio_manager": "cloud_only",
            "model": (full_config.get("llm") or {}).get("model"),
            "auditor": "non_llm_deterministic",
        },
    }


def _signed_position_ratio(position, total_portfolio_value: float) -> float:
    """Return the current signed portfolio ratio for one ticker."""
    if not position or total_portfolio_value <= 0:
        return 0.0
    position_value = float(getattr(position, "value", 0.0) or 0.0)
    shares = int(getattr(position, "shares", 0) or 0)
    if shares > 0:
        return position_value / total_portfolio_value
    if shares < 0:
        return -(position_value / total_portfolio_value)
    return 0.0


def _current_net_exposure(portfolio, account_equity: float) -> float:
    """Return signed notional exposure divided by futures account equity."""
    if account_equity <= 0:
        return 0.0

    net_exposure = 0.0
    for position in portfolio.positions.values():
        net_exposure += _signed_position_ratio(position, account_equity)
    return net_exposure


def _resolve_net_exposure_control(full_config: dict, control_diagnostics: dict | None = None) -> tuple[float, bool, str]:
    net_exposure_config = full_config.get('net_exposure_control')
    if net_exposure_config is None:
        risk_control = full_config.get('risk_control', {})
        net_exposure_config = risk_control.get('net_exposure_control', {})
    max_net_exposure = float(net_exposure_config.get('max_net_exposure', 0.50))
    symmetric_scaling = bool(net_exposure_config.get('symmetric_scaling', True))
    cap_mode = "base"

    target = (control_diagnostics or {}).get("capital_utilization_target")
    if (
        isinstance(target, dict)
        and target.get("target_mode") in {"strong_opportunity", "alpha_release_boost", "alpha_release_max_boost"}
        and target.get("high_quality_memory")
    ):
        strong_cap = net_exposure_config.get("strong_opportunity_max_net_exposure")
        if strong_cap is not None:
            max_net_exposure = max(max_net_exposure, float(strong_cap))
            cap_mode = "alpha_release"

    return max_net_exposure, symmetric_scaling, cap_mode


def _days_held(entry_date: str, trading_date) -> int:
    """Best-effort holding-period calculation using normalized YYYY-MM-DD strings."""
    if not entry_date or trading_date is None:
        return 999
    try:
        entry_dt = datetime.strptime(str(entry_date)[:10], "%Y-%m-%d")
        trading_dt = datetime.strptime(str(trading_date)[:10], "%Y-%m-%d")
        return max(0, (trading_dt - entry_dt).days)
    except (TypeError, ValueError):
        return 999

def _to_recommendation_action(action) -> RecommendationAction:
    action_value = action.value if hasattr(action, "value") else str(action)
    mapping = {
        FuturesAction.OPEN_LONG.value: RecommendationAction.OPEN_LONG,
        FuturesAction.OPEN_SHORT.value: RecommendationAction.OPEN_SHORT,
        FuturesAction.CLOSE_LONG.value: RecommendationAction.CLOSE_LONG,
        FuturesAction.CLOSE_SHORT.value: RecommendationAction.CLOSE_SHORT,
        FuturesAction.HOLD.value: RecommendationAction.HOLD,
        "open_long": RecommendationAction.OPEN_LONG,
        "open_short": RecommendationAction.OPEN_SHORT,
        "close_long": RecommendationAction.CLOSE_LONG,
        "close_short": RecommendationAction.CLOSE_SHORT,
        "hold": RecommendationAction.HOLD,
    }
    return mapping.get(action_value, RecommendationAction.HOLD)

def _build_pm_memory_state(
    config_id: str,
    portfolio,
    ticker: str,
    trading_date,
    contract_code: str,
    decision: FuturesDecision,
    morning_price_context,
    analyst_signals,
    plan_snapshot=None,
    pm_state_update=None,
    market_confirmation=None,
    full_config=None,
):
    """Return the single Step1-5 PM memory state without a snapshot or artifact draft."""
    _ = (analyst_signals, market_confirmation, full_config)
    if not isinstance(pm_state_update, dict) or not pm_state_update:
        raise ValueError("pm_steps_1_5_missing_pm_state_update")
    trading_date_value = (
        trading_date.strftime("%Y-%m-%d")
        if hasattr(trading_date, "strftime")
        else str(trading_date)
    )
    plan_state = dict(plan_snapshot or {})
    memory_state = dict(pm_state_update)
    execution_fields = (
        dict(memory_state.get("execution_contract_fields") or {})
        if isinstance(memory_state.get("execution_contract_fields"), dict)
        else {}
    )
    collection_contract = (
        plan_state.get("signal_collection_contract")
        if isinstance(plan_state.get("signal_collection_contract"), dict)
        else execution_fields.get("signal_collection_contract")
        if isinstance(execution_fields.get("signal_collection_contract"), dict)
        else memory_state.get("signal_collection_contract")
        if isinstance(memory_state.get("signal_collection_contract"), dict)
        else None
    )
    if not isinstance(collection_contract, dict) or not collection_contract:
        raise ValueError("pm_steps_1_5_missing_signal_collection_contract")

    decision_action = decision.action.value if hasattr(decision.action, "value") else str(decision.action)
    semantic_block_reason = None
    if str(decision_action).lower() in {"open_long", "open_short"} and int(decision.lots or 0) > 0:
        semantic_block_reason = _structured_new_entry_block_reason(memory_state)
    if semantic_block_reason:
        current_lots = int(memory_state.get("current_lots") or 0)
        memory_state = _build_blocked_pm_memory_state_update(
            ticker=ticker,
            current_lots=current_lots,
            target_lots=current_lots,
            reason=semantic_block_reason,
            authority_type="watchlist_only",
            account_equity=_portfolio_account_equity(portfolio),
            signal_collection_contract=collection_contract,
            execution_contract_fields={
                **execution_fields,
                "semantic_consistency_gate": {
                    "passed": False,
                    "block_reason": semantic_block_reason,
                },
            },
        )

    memory_state["ticker"] = ticker
    memory_state["signal_collection_contract"] = deepcopy(collection_contract)
    memory_state["recommendation_context"] = {
        "config_id": config_id,
        "reference_portfolio_id": portfolio.id,
        "trading_date": trading_date_value,
        "effective_trade_date": trading_date_value,
        "source_type": RecommendationSourceType.STRATEGY,
        "underlying_code": ticker,
        "contract_code": contract_code,
        "base_price": morning_price_context.base_price if morning_price_context else None,
        "base_price_source": morning_price_context.base_price_source if morning_price_context else None,
        "base_price_date": morning_price_context.base_price_date if morning_price_context else None,
        "open_price": morning_price_context.open_price if morning_price_context else None,
        "prev_close_price": morning_price_context.prev_close_price if morning_price_context else None,
        "slippage_model": "tick",
        "slippage_ticks": None,
        "slippage_amount": None,
        "execution_price": None,
        "justification": "",
        "warning_message": morning_price_context.warning_message if morning_price_context else None,
        "status": RecommendationStatus.PENDING,
    }
    return memory_state
def _phase1_return_with_pm_state(
    *,
    config_id: str,
    full_config: dict,
    portfolio,
    ticker: str,
    trading_date,
    contract_code: str | None,
    decision: FuturesDecision,
    morning_price_context,
    analyst_signals,
    plan_snapshot=None,
    pm_state_update=None,
    market_confirmation=None,
):
    """Return the same PM memory state after an early business-path update."""
    pm_state = _build_pm_memory_state(
        config_id=config_id,
        full_config=full_config,
        portfolio=portfolio,
        ticker=ticker,
        trading_date=trading_date,
        contract_code=contract_code,
        decision=decision,
        morning_price_context=morning_price_context,
        analyst_signals=analyst_signals,
        plan_snapshot=plan_snapshot,
        pm_state_update=pm_state_update,
        market_confirmation=market_confirmation,
    )
    return pm_state

def _resolve_pre_open_signal_direction(target_lots: int) -> str:
    if target_lots > 0:
        return "long"
    if target_lots < 0:
        return "short"
    return "flat"

def _resolve_pre_open_signal_confidence(direction: str, long_scores: dict, short_scores: dict) -> float:
    if direction == "long":
        return float(long_scores.get("confidence", 0.0) or 0.0)
    if direction == "short":
        return float(short_scores.get("confidence", 0.0) or 0.0)
    return 0.0


def _resolve_decision_horizon(analyst_signals: list, target_lots: int) -> str:
    return _fusion_resolve_decision_horizon(analyst_signals, target_lots)

def _build_pm_decision_context(
    target_lots: int,
    current_price: float,
    position_ratio: float,
    risk_level: RiskLevel,
    long_scores: dict,
    short_scores: dict,
    margin_rate: float = 0.0,
    current_lots: int = 0,
    analyst_signals: list | None = None,
    final_entry_authority: dict | None = None,
    trading_date=None,
    recommendation_intent: dict | None = None,
    control_reasons: list[str] | None = None,
    alpha_setup_action_values: list | None = None,
    ticker: str = "",
) -> dict:
    direction = _resolve_pre_open_signal_direction(target_lots)
    plan = {
        "signal_direction": direction,
        "signal_confidence": _resolve_pre_open_signal_confidence(direction, long_scores, short_scores),
        "target_position_ratio": float(position_ratio),
        "target_margin_ratio_estimate": abs(float(position_ratio)) * max(0.0, float(margin_rate or 0.0)),
        "target_lots": int(target_lots),
        "reference_price": float(current_price),
        "risk_level": risk_level.value,
    }
    execution_fields = _build_execution_contract_fields(
        ticker=ticker,
        current_lots=int(current_lots or 0),
        target_lots=int(target_lots or 0),
        analyst_signals=analyst_signals or [],
        final_entry_authority=final_entry_authority or {},
        trading_date=trading_date,
        recommendation_intent=recommendation_intent or {},
        control_reasons=control_reasons or [],
        alpha_setup_action_values=alpha_setup_action_values,
        reference_price=float(current_price),
    )
    plan.update(execution_fields)
    return plan


def _execution_signal_payloads(analyst_signals: list | None, target_side: str) -> dict:
    payloads = {}
    for signal in analyst_signals or []:
        agent_name = _normalize_agent_name(str(getattr(signal, "agent_name", "") or ""))
        if agent_name not in {"technical", "fundamental", "commodity_news"}:
            continue
        action_contract = _canonical_action_evidence_contract(signal)
        if not action_contract:
            continue
        payload = {
            "agent_name": agent_name,
            "side": str(action_contract.get("side") or "").strip().lower(),
            "signal": str(action_contract.get("signal") or ""),
            "evidence_role": action_contract.get("evidence_role"),
            "entry_trigger": str(action_contract.get("entry_trigger") or "").strip(),
            "trigger_quality_score": max(
                0.0,
                min(1.0, _safe_float(action_contract.get("trigger_quality_score"), 0.0)),
            ),
            "invalidation": str(action_contract.get("invalidation_condition") or "").strip(),
            "invalidation_level": action_contract.get("invalidation_level"),
            "position_invalidation_level": action_contract.get("position_invalidation_level"),
            "exit_hint": str(action_contract.get("exit_hint") or "").strip(),
            "atr_stop_distance": action_contract.get("atr_stop_distance"),
            "setup_type": str(action_contract.get("setup_type") or "").strip(),
            "horizon_class": action_contract.get("horizon_class"),
            "expected_horizon_days": action_contract.get("expected_horizon_days"),
            "market_regime": action_contract.get("market_regime"),
            "entry_timing_signal": str(action_contract.get("entry_timing_signal") or "").strip(),
            "trigger_valid": action_contract.get("trigger_valid") is True,
            "current_trigger_confirmed": action_contract.get("current_trigger_confirmed") is True,
            "invalidation_present": action_contract.get("invalidation_present") is True,
            "opportunity_state": str(action_contract.get("opportunity_state") or "").strip().lower(),
            "opportunity_type": str(action_contract.get("opportunity_type") or "").strip(),
            "event_type": str(action_contract.get("event_type") or "").strip(),
            "confidence": _safe_float(action_contract.get("confidence"), 0.0),
        }
        existing = payloads.get(agent_name)
        if existing is None or (payload["side"] == target_side and existing.get("side") != target_side):
            payloads[agent_name] = payload
    return payloads


def _target_execution_lifecycle_boundaries(
    analyst_signals: list | None,
    target_side: str,
) -> tuple[bool, bool]:
    """Read entry and post-fill boundaries from aligned formal AECs."""
    payloads = _execution_signal_payloads(analyst_signals, target_side)
    entry_invalidation_present = False
    position_exit_boundary_present = _has_position_exit_boundary(
        analyst_signals or [],
        target_side=target_side,
    )
    for analyst in ANALYST_ORDER:
        payload = payloads.get(analyst)
        if not isinstance(payload, dict) or payload.get("side") != target_side:
            continue
        if analyst not in {"technical", "commodity_news"}:
            continue
        profile = normalize_execution_profile(payload.get("entry_timing_signal"))
        if not execution_profile_allowed_for_analyst(analyst, profile):
            continue
        if _execution_payload_has_invalidation(payload):
            entry_invalidation_present = True
    # Unit-level helpers also accept an SCC-rebuilt evidence object directly.
    # The production PM path has already replaced state analyst payloads with
    # ``build_pm_evidence_signals_from_scc`` before reaching this function.
    for signal in analyst_signals or []:
        if _signal_side_text(getattr(signal, "signal", None)) != target_side:
            continue
        agent = _normalize_agent_name(str(getattr(signal, "agent_name", "") or ""))
        if agent in {"technical", "commodity_news"} and _has_structured_invalidation_condition(
            [signal],
            target_side=target_side,
        ):
            entry_invalidation_present = True
    return entry_invalidation_present, position_exit_boundary_present


def _execution_payload_has_invalidation(payload: dict) -> bool:
    if not payload.get("invalidation_present"):
        return False
    return not entry_invalidation_contract_error(
        profile=payload.get("entry_timing_signal") or payload.get("execution_profile"),
        side=payload.get("side"),
        invalidation_condition=payload.get("invalidation"),
        invalidation_level=payload.get("invalidation_level"),
    )


def _positive_finite_float(value) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0.0 else None


def _position_level_for_reference(
    payload: dict | None,
    *,
    target_side: str,
    reference_price: float,
) -> float | None:
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("side") != target_side:
        return None
    level = _positive_finite_float(payload.get("position_invalidation_level"))
    reference = _positive_finite_float(reference_price)
    if level is None or reference is None:
        return None
    if target_side == "long" and level < reference:
        return level
    if target_side == "short" and level > reference:
        return level
    return None


def _horizon_pair(payload: dict | None) -> tuple[str | None, int | None]:
    payload = payload if isinstance(payload, dict) else {}
    horizon_class = str(payload.get("horizon_class") or "").strip()
    expected_days = _safe_int(payload.get("expected_horizon_days"), 0)
    if not horizon_class or horizon_class.lower() in {"unknown", "flat", "none"}:
        return None, None
    if expected_days <= 0:
        return None, None
    return horizon_class, expected_days


def _select_execution_evidence_payload(
    payloads: dict,
    *,
    target_side: str,
    conditional_path: bool,
) -> dict:
    eligible = []
    for analyst in ANALYST_ORDER:
        payload = payloads.get(analyst)
        if not isinstance(payload, dict) or payload.get("side") != target_side:
            continue
        profile = normalize_execution_profile(payload.get("entry_timing_signal"))
        if not execution_profile_allowed_for_analyst(analyst, profile):
            continue
        if not _execution_payload_has_invalidation(payload):
            continue
        state = str(payload.get("opportunity_state") or "").strip().lower()
        if conditional_path:
            if state != "watch_for_trigger":
                continue
            if payload.get("trigger_valid") or payload.get("current_trigger_confirmed"):
                continue
        else:
            if state not in {"probe_candidate", "tradeable_candidate"}:
                continue
            if not payload.get("trigger_valid") or not payload.get("current_trigger_confirmed"):
                continue
        if conditional_path and analyst != "technical":
            continue
        if not conditional_path and analyst == "commodity_news" and profile != "event_immediate":
            continue
        trigger_source = trigger_source_for_analyst_profile(analyst, profile)
        contract_error = execution_trigger_contract_error(
            profile=profile,
            side=target_side,
            entry_trigger=payload.get("entry_trigger"),
            trigger_source=trigger_source,
        )
        if contract_error:
            continue
        payload = {
            **payload,
            "execution_profile": profile,
            "trigger_source": trigger_source,
        }
        eligible.append(payload)
    if not eligible:
        raise ValueError("pm_execution_evidence_not_found")
    eligible.sort(
        key=lambda payload: (
            0
            if payload.get("execution_profile") == "event_immediate"
            else 1,
            -_safe_float(payload.get("confidence"), 0.0),
        )
    )
    return eligible[0]


def _select_position_lifecycle_evidence_fields(
    payloads: dict,
    *,
    target_side: str,
    selected_execution_evidence: dict,
    reference_price: float,
) -> dict:
    """Assemble post-fill lifecycle facts by analyst role.

    The input payloads are PM-internal evidence rebuilt from the already
    validated SCC in the production path.  Entry execution remains single
    source; post-fill structure, volatility and horizon keep their distinct
    analyst responsibilities.
    """
    technical = payloads.get("technical") if isinstance(payloads.get("technical"), dict) else {}
    fundamental = payloads.get("fundamental") if isinstance(payloads.get("fundamental"), dict) else {}

    structure_source: dict = {}
    position_level = _position_level_for_reference(
        technical,
        target_side=target_side,
        reference_price=reference_price,
    )
    if position_level is not None:
        structure_source = technical
    elif (
        selected_execution_evidence.get("agent_name") == "commodity_news"
        and selected_execution_evidence.get("execution_profile") == "event_immediate"
    ):
        position_level = _position_level_for_reference(
            selected_execution_evidence,
            target_side=target_side,
            reference_price=reference_price,
        )
        if position_level is not None:
            structure_source = selected_execution_evidence

    # ATR is a direction-neutral deterministic technical fact.  No other
    # analyst source can supply this executable stop distance.
    atr_stop_distance = _positive_finite_float(technical.get("atr_stop_distance"))

    horizon_class = None
    expected_horizon_days = None
    if fundamental.get("side") == target_side:
        horizon_class, expected_horizon_days = _horizon_pair(fundamental)
    if horizon_class is None:
        horizon_class, expected_horizon_days = _horizon_pair(selected_execution_evidence)

    if position_level is None and atr_stop_distance is None:
        raise ValueError("pm_position_lifecycle_evidence_not_found")

    exit_hint = str(structure_source.get("exit_hint") or "").strip()
    if not exit_hint and fundamental.get("side") == target_side:
        exit_hint = str(fundamental.get("exit_hint") or "").strip()
    if not exit_hint:
        exit_hint = str(selected_execution_evidence.get("exit_hint") or "").strip()

    return {
        "position_invalidation_level": position_level,
        "atr_stop_distance": atr_stop_distance,
        "horizon_class": horizon_class,
        "expected_horizon_days": expected_horizon_days,
        "exit_hint": exit_hint or None,
    }


def _execution_profile_and_source(payload: dict, *, authority_type: str) -> tuple[str, str]:
    _ = authority_type
    profile = normalize_execution_profile(payload.get("execution_profile"))
    source = str(payload.get("trigger_source") or "").strip()
    contract_error = execution_trigger_contract_error(
        profile=profile,
        side=payload.get("side"),
        entry_trigger=payload.get("entry_trigger"),
        trigger_source=source,
    )
    if contract_error:
        raise ValueError(f"pm_{contract_error}")
    return profile, source


def _entry_trigger_confirmation_adjustment(
    *,
    ticker: str,
    side: str,
    setup_type: str,
    entry_trigger: str,
    execution_profile: str,
    final_entry_authority: dict,
    alpha_setup_action_values: list | None,
) -> str:
    """Route only structured weak-conflict/formal entry learning to execution."""
    if execution_profile not in {"breakout", "pullback", "vwap_confirmed"}:
        return "not_applicable"
    severity = {
        "not_applicable": 0,
        "neutral": 1,
        "standard_confirmation_supported": 2,
        "stronger_confirmation_required": 3,
        "strict_confirmation_required": 4,
    }
    selected = (
        "stronger_confirmation_required"
        if bool(final_entry_authority.get("weak_conflict_probe"))
        else "not_applicable"
    )
    ticker_key = str(ticker or "").strip().upper()
    side_key = str(side or "").strip().lower()
    setup_key = str(setup_type or "").strip().lower()
    trigger_key = str(entry_trigger or "").strip()
    for row in _formal_pm_learning_action_values(alpha_setup_action_values):
        if str(row.get("ticker") or "").strip().upper() != ticker_key:
            continue
        if str(row.get("side") or "").strip().lower() != side_key:
            continue
        if str(row.get("setup_type") or "").strip().lower() != setup_key:
            continue
        if str(row.get("canonical_action_family") or "").strip().lower() != "open_add_new_risk":
            continue
        lane = str(
            row.get("action_value_lane")
            or row.get("learning_lane")
            or row.get("action_name")
            or ""
        ).strip().lower()
        if lane not in {"open", "add", "scale", "increase"}:
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        product_key = (
            payload.get("product_learning_performance_key")
            if isinstance(payload.get("product_learning_performance_key"), dict)
            else {}
        )
        outcome = (
            payload.get("entry_quality_outcome")
            if isinstance(payload.get("entry_quality_outcome"), dict)
            else product_key.get("entry_quality_outcome")
            if isinstance(product_key.get("entry_quality_outcome"), dict)
            else {}
        )
        if str(outcome.get("contract_version") or "").strip() != "agentquant.entry_quality_outcome.v1":
            continue
        historical_trigger = str(outcome.get("entry_trigger") or "").strip()
        if not trigger_key or historical_trigger != trigger_key:
            continue
        adjustment = normalize_trigger_confirmation_adjustment(
            outcome.get("trigger_confirmation_adjustment")
        )
        if severity[adjustment] > severity[selected]:
            selected = adjustment
    return selected


def _build_execution_contract_fields(
    *,
    ticker: str = "",
    current_lots: int,
    target_lots: int,
    analyst_signals: list,
    final_entry_authority: dict,
    trading_date,
    recommendation_intent: dict,
    control_reasons: list[str],
    alpha_setup_action_values: list | None = None,
    reference_price: float = 0.0,
) -> dict:
    """Translate PM target lots into a Trader-readable execution contract.

    This does not create a new strategy. It preserves the PM decision and tells
    Phase2 which execution style is allowed for the existing target.
    """
    target_side = "long" if target_lots > 0 else "short" if target_lots < 0 else "flat"
    lots_delta = int(target_lots - current_lots)
    authority_type = str(final_entry_authority.get("authority_type") or "not_applicable")
    signal_payloads = _execution_signal_payloads(analyst_signals, target_side)
    selected_execution_evidence = None
    selected_position_lifecycle_evidence = None

    if target_lots == current_lots or lots_delta == 0:
        profile = "hold"
        trigger_source = "none"
    elif target_lots == 0 or (
        current_lots != 0
        and (
            (current_lots > 0) != (target_lots > 0)
            or abs(target_lots) < abs(current_lots)
        )
    ):
        profile = "exit_immediate"
        trigger_source = "position_lifecycle"
    else:
        selected_execution_evidence = _select_execution_evidence_payload(
            signal_payloads,
            target_side=target_side,
            conditional_path=bool(final_entry_authority.get("conditional_trigger_authority")),
        )
        profile, trigger_source = _execution_profile_and_source(
            selected_execution_evidence,
            authority_type=authority_type,
        )
        selected_position_lifecycle_evidence = _select_position_lifecycle_evidence_fields(
            signal_payloads,
            target_side=target_side,
            selected_execution_evidence=selected_execution_evidence,
            reference_price=reference_price,
        )

    entry_trigger = (
        str(selected_execution_evidence.get("entry_trigger") or "").strip()
        if selected_execution_evidence
        else ""
    )
    invalidation = (
        str(selected_execution_evidence.get("invalidation") or "").strip()
        if selected_execution_evidence
        else ""
    )

    date_text = trading_date.strftime("%Y-%m-%d") if hasattr(trading_date, "strftime") else str(trading_date or "")
    aligned_current_trigger = bool(
        selected_execution_evidence
        and selected_execution_evidence.get("trigger_valid")
        and selected_execution_evidence.get("current_trigger_confirmed")
        and selected_execution_evidence.get("invalidation_present")
        and selected_execution_evidence.get("opportunity_state")
        in {"probe_candidate", "tradeable_candidate"}
    )
    direct_entry_authorized = bool(
        target_lots != current_lots
        and target_lots != 0
        and aligned_current_trigger
        and selected_execution_evidence
        and selected_execution_evidence.get("agent_name") == "commodity_news"
        and profile == "event_immediate"
        and _semantic_authority_allows_entry(final_entry_authority)
    )
    can_execute_without_intraday_trigger = bool(
        profile == "exit_immediate" or direct_entry_authorized
    )
    if final_entry_authority.get("conditional_trigger_authority"):
        can_execute_without_intraday_trigger = False
    proposed_execution_preference = _execution_action_value_preference(
        ticker=ticker,
        side=target_side,
        base_profile=profile,
        alpha_setup_action_values=alpha_setup_action_values,
        final_entry_authority=final_entry_authority,
    )
    execution_preference = (
        {
            **proposed_execution_preference,
            "applied": False,
            "diagnostic_only": True,
            "not_formal_trigger_profile_consumption": True,
        }
        if proposed_execution_preference
        else {}
    )
    trigger_confirmation_adjustment = _entry_trigger_confirmation_adjustment(
        ticker=ticker,
        side=target_side,
        setup_type=(
            selected_execution_evidence.get("setup_type")
            if selected_execution_evidence
            else ""
        ),
        entry_trigger=entry_trigger,
        execution_profile=profile,
        final_entry_authority=final_entry_authority,
        alpha_setup_action_values=alpha_setup_action_values,
    )

    return {
        "contract_version": "agentquant.execution_contract_fields.v1",
        "execution_profile": profile,
        "trigger_source": trigger_source,
        "trigger_confirmation_adjustment": trigger_confirmation_adjustment,
        "target_side": target_side,
        "current_lots": int(current_lots),
        "target_lots": int(target_lots),
        "lots_delta": int(lots_delta),
        "entry_trigger": entry_trigger,
        "trigger_quality_score": (
            selected_execution_evidence.get("trigger_quality_score")
            if selected_execution_evidence
            else 0.0
        ),
        "invalidation": invalidation,
        "setup_type": selected_execution_evidence.get("setup_type") if selected_execution_evidence else None,
        "horizon_class": (
            selected_position_lifecycle_evidence.get("horizon_class")
            if selected_position_lifecycle_evidence
            else None
        ),
        "expected_horizon_days": (
            selected_position_lifecycle_evidence.get("expected_horizon_days")
            if selected_position_lifecycle_evidence
            else None
        ),
        "market_regime": selected_execution_evidence.get("market_regime") if selected_execution_evidence else None,
        "invalidation_level": (
            selected_execution_evidence.get("invalidation_level")
            if selected_execution_evidence
            else None
        ),
        "position_invalidation_level": (
            selected_position_lifecycle_evidence.get("position_invalidation_level")
            if selected_position_lifecycle_evidence
            else None
        ),
        "exit_hint": (
            selected_position_lifecycle_evidence.get("exit_hint")
            if selected_position_lifecycle_evidence
            else None
        ),
        "atr_stop_distance": (
            selected_position_lifecycle_evidence.get("atr_stop_distance")
            if selected_position_lifecycle_evidence
            else None
        ),
        "valid_until": f"{date_text} 15:00:00" if date_text else "",
        "requires_intraday_confirmation": not can_execute_without_intraday_trigger and profile != "hold",
        "can_execute_without_intraday_trigger": can_execute_without_intraday_trigger,
        "authority_type": authority_type,
        "max_allowed_margin_ratio": _safe_float(final_entry_authority.get("max_allowed_margin_ratio"), 0.0),
        "reason_codes": sorted(set([str(item) for item in (control_reasons or [])] + [
            str(item) for item in (final_entry_authority.get("reason_codes") or [])
        ])),
        "execution_action_value_preference": execution_preference,
        "recommendation_action": str((recommendation_intent or {}).get("action") or ""),
        "business_boundary": "trader_executes_pm_plan_only_no_strategy_creation",
    }


def _compact_policy_row(row: dict) -> dict:
    if not isinstance(row, dict):
        return {}
    return {
        "policy_type": row.get("policy_type"),
        "policy_action": row.get("policy_action"),
        "ticker": row.get("ticker"),
        "side": row.get("side"),
        "setup_type": row.get("setup_type"),
        "horizon_class": row.get("horizon_class"),
        "market_regime": row.get("market_regime"),
        "confidence_score": row.get("confidence_score"),
        "sample_count": row.get("sample_count"),
        "win_rate": row.get("win_rate"),
        "net_pnl": row.get("net_pnl"),
        "cap_multiplier": row.get("cap_multiplier"),
        "valid_until": row.get("valid_until"),
    }


def _adaptive_policy_trace(adaptive_policy_state: list | None) -> dict:
    rows = [_compact_policy_row(row) for row in (adaptive_policy_state or []) if isinstance(row, dict)]
    rows = [row for row in rows if row]
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("policy_type") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return {
        "policy_count": len(rows),
        "policy_type_counts": counts,
        "scope": "adaptive_policy_state_summary",
        "status": "summary_only_no_policy_rows",
        "policy_rows_omitted": True,
        "candidate_boundary": "candidate_or_observation_rows_are_context_only_until_promoted_by_same_scope_validation",
        "not_product_blacklist": True,
    }


def _compact_alpha_setup_profile(row: dict) -> dict:
    return compact_profile_for_trace(row) if isinstance(row, dict) else {}


def _alpha_setup_profile_trace(alpha_setup_profiles: list | None) -> dict:
    rows = [_compact_alpha_setup_profile(row) for row in (alpha_setup_profiles or []) if isinstance(row, dict)]
    rows = [row for row in rows if row]
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("lifecycle_state") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return {
        "profile_count": len(rows),
        "lifecycle_counts": counts,
        "profiles": rows[:6],
        "same_scope_required": True,
        "candidate_prior_only": True,
        "not_product_blacklist": True,
    }


def _compact_alpha_setup_action_value(row: dict) -> dict:
    if not isinstance(row, dict):
        return {}
    row = _normalize_alpha_setup_action_value(row)
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return {
        "id": row.get("id") or payload.get("id"),
        "scope_key": row.get("scope_key"),
        "ticker": row.get("ticker"),
        "side": row.get("side"),
        "horizon_class": row.get("horizon_class"),
        "market_regime": row.get("market_regime"),
        "setup_type": row.get("setup_type"),
        "action_name": row.get("action_name"),
        "sample_count": row.get("sample_count"),
        "reward_sum": row.get("reward_sum"),
        "reward_mean": row.get("reward_mean"),
        "win_rate": row.get("win_rate"),
        "confidence_score": row.get("confidence_score"),
        "action_preference": _action_value_preference(row),
        "canonical_action_preference_source": (
            row.get("canonical_action_preference_source")
            or payload.get("canonical_action_preference_source")
            or "canonical_action_value"
        ),
        "max_position_impact": row.get("max_position_impact"),
        "last_sample_date": row.get("last_sample_date"),
        "valid_until": row.get("valid_until"),
        "source": payload.get("source") or row.get("source"),
        "reward_source": row.get("reward_source") or payload.get("reward_source"),
        "consumer_scope": row.get("consumer_scope") or payload.get("consumer_scope"),
        "canonical_action_family": row.get("canonical_action_family") or payload.get("canonical_action_family"),
        "learning_lane": row.get("learning_lane") or payload.get("learning_lane"),
        "memory_side_role": row.get("memory_side_role") or payload.get("memory_side_role"),
        "memory_requirement_reason": row.get("memory_requirement_reason") or payload.get("memory_requirement_reason"),
        "retrieval_key": row.get("retrieval_key") or payload.get("retrieval_key"),
        "fallback_retrieval_key": row.get("fallback_retrieval_key") or payload.get("fallback_retrieval_key"),
        "execution_retrieval_key": row.get("execution_retrieval_key") or payload.get("execution_retrieval_key"),
        "retrieval_match_level": row.get("retrieval_match_level") or payload.get("retrieval_match_level"),
        "retrieval_match_reason": row.get("retrieval_match_reason") or payload.get("retrieval_match_reason"),
        "strict_no_lookahead": payload.get("strict_no_lookahead"),
        "evidence_scope": row.get("evidence_scope") or payload.get("evidence_scope"),
        "amplification_scope_quality": (
            row.get("evidence_scope")
            or payload.get("amplification_scope_quality")
            or payload.get("evidence_scope")
        ),
        "action_value_lane": row.get("action_value_lane") or payload.get("action_value_lane"),
        "exact_state_real_trade_sample_count": payload.get("exact_state_real_trade_sample_count"),
        "partial_state_real_trade_sample_count": payload.get("partial_state_real_trade_sample_count"),
        "similar_real_trade_sample_count": payload.get("similar_real_trade_sample_count"),
        "exact_ticker_sample_count": payload.get("exact_ticker_sample_count"),
        "exact_ticker_real_trade_sample_count": payload.get("exact_ticker_real_trade_sample_count"),
        "real_trade_reward_count": payload.get("real_trade_reward_count"),
        "counterfactual_prior_only": payload.get("counterfactual_prior_only"),
        "counterfactual_reward_count": payload.get("counterfactual_reward_count"),
        "loss_reward_count": payload.get("loss_reward_count"),
        "tail_loss_count": payload.get("tail_loss_count"),
        "worst_reward": payload.get("worst_reward"),
        "canonical_action_value": row.get("canonical_action_value"),
        "canonical_action_value_source": row.get("canonical_action_value_source"),
    }


def _action_value_key(row: dict) -> tuple[str, str, str, str, str, str, str]:
    action_lane = (
        row.get("action_name")
        or row.get("action_value_lane")
        or row.get("learning_lane")
        or ""
    )
    return (
        str(row.get("scope_key") or ""),
        str(action_lane),
        str(row.get("ticker") or ""),
        str(row.get("side") or ""),
        str(row.get("horizon_class") or ""),
        str(row.get("market_regime") or ""),
        str(row.get("setup_type") or ""),
    )


def _action_value_row_completeness(row: dict) -> tuple[int, int, int, int, int, int, int, int]:
    if not isinstance(row, dict):
        return (0, 0, 0, 0, 0, 0, 0, 0)
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    row_id = str(row.get("id") or payload.get("id") or "").strip()
    preference = str(
        row.get("action_preference")
        or payload.get("action_preference")
        or ""
    ).strip()
    reward_present = any(
        (row.get(key) not in (None, "") or payload.get(key) not in (None, ""))
        for key in ("reward_sum", "reward_mean", "win_rate")
    )
    scope = str(
        row.get("evidence_scope")
        or row.get("amplification_scope_quality")
        or payload.get("evidence_scope")
        or payload.get("amplification_scope_quality")
        or ""
    ).strip().lower()
    reward_source = str(row.get("reward_source") or payload.get("reward_source") or "").strip().lower()
    action_value_lane = str(
        row.get("action_value_lane")
        or row.get("learning_lane")
        or row.get("action_name")
        or payload.get("action_value_lane")
        or payload.get("learning_lane")
        or payload.get("action_name")
        or ""
    ).strip().lower()
    has_payload = int(bool(payload))
    canonical = bool(
        preference
        and reward_present
        and scope
        and reward_source
        and action_value_lane
    )
    return (
        int(canonical),
        int(bool(row_id)),
        int(bool(preference)),
        int(bool(reward_present)),
        int(scope == "exact_real_state"),
        int(reward_source in {"real_trade", "trade_episode", "episode_trade", "complete_episode"}),
        int(bool(action_value_lane)),
        has_payload,
    )


def _normalize_alpha_setup_action_value(row: dict) -> dict:
    """Return a PM-readable canonical action-value row.

    Analyst learning context can carry compact trace rows. PM scoring needs the
    machine fields that describe reward, evidence scope and action preference.
    This normalizer keeps top-level canonical fields first and falls back to
    legacy payload fields without creating trade authority.
    """
    if not isinstance(row, dict):
        return {}
    normalized = dict(row)
    payload = normalized.get("payload") if isinstance(normalized.get("payload"), dict) else {}
    canonical_flag_present = (
        "canonical_action_value" in normalized
        or "canonical_action_value" in payload
    )
    explicit_canonical = (
        normalized.get("canonical_action_value")
        if "canonical_action_value" in normalized
        else payload.get("canonical_action_value")
        if "canonical_action_value" in payload
        else None
    )
    if payload:
        normalized["payload"] = dict(payload)
    if not normalized.get("id") and payload.get("id"):
        normalized["id"] = payload.get("id")
    def pick(*keys, default=None):
        for key in keys:
            value = normalized.get(key)
            if value not in (None, ""):
                return value
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
        return default

    action_preference = _canonical_action_preference(pick("action_preference", "policy_action", default=""))
    if action_preference:
        normalized["action_preference"] = action_preference
    reward_source = str(pick("reward_source", "sample_source", default="") or "").strip().lower()
    if reward_source:
        normalized["reward_source"] = reward_source
    evidence_scope = str(
        pick("evidence_scope", "amplification_scope_quality", "source_quality", default="")
        or ""
    ).strip().lower()
    if evidence_scope:
        normalized["evidence_scope"] = evidence_scope
    action_value_lane = str(
        pick("action_value_lane", "source_action_value_lane", "action_name", default="")
        or ""
    ).strip().lower()
    if action_value_lane:
        normalized["action_value_lane"] = action_value_lane
    canonical_action_family = str(
        pick("canonical_action_family", "source_canonical_action_family", default="") or ""
    ).strip().lower()
    if canonical_action_family:
        normalized["canonical_action_family"] = canonical_action_family
    action_name = str(pick("action_name", default="") or "").strip().lower()
    if action_name:
        normalized["action_name"] = action_name
    consumer_scope = str(
        pick("consumer_scope", "learning_consumer_scope", default="") or ""
    ).strip().lower()
    if consumer_scope:
        normalized["consumer_scope"] = consumer_scope
    learning_lane = str(
        pick("learning_lane", "action_value_lane", "source_action_value_lane", "action_name", default="")
        or ""
    ).strip().lower()
    if learning_lane:
        normalized["learning_lane"] = learning_lane
    memory_side_role = str(pick("memory_side_role", default="") or "").strip().lower()
    if memory_side_role:
        normalized["memory_side_role"] = memory_side_role
    memory_requirement_reason = str(pick("memory_requirement_reason", default="") or "").strip().lower()
    if memory_requirement_reason:
        normalized["memory_requirement_reason"] = memory_requirement_reason
    for key in (
        "retrieval_key",
        "fallback_retrieval_key",
        "execution_retrieval_key",
        "retrieval_match_level",
        "retrieval_match_reason",
    ):
        value = pick(key)
        if value not in (None, ""):
            normalized[key] = value
    for key in (
        "reward_sum",
        "reward_mean",
        "win_rate",
        "sample_count",
        "confidence_score",
        "max_position_impact",
        "last_sample_date",
        "valid_until",
        "worst_reward",
        "tail_loss_count",
        "real_trade_reward_count",
        "counterfactual_reward_count",
        "exact_state_real_trade_sample_count",
        "partial_state_real_trade_sample_count",
        "similar_real_trade_sample_count",
        "exact_ticker_sample_count",
        "exact_ticker_real_trade_sample_count",
    ):
        value = pick(key)
        if value not in (None, ""):
            normalized[key] = value
    derived_canonical = bool(
        normalized.get("action_preference")
        and normalized.get("canonical_action_family")
        and normalized.get("reward_source")
        and normalized.get("evidence_scope")
        and normalized.get("action_value_lane")
        and normalized.get("learning_lane")
        and normalized.get("consumer_scope") == "pm_learning"
        and (
            normalized.get("reward_sum") not in (None, "")
            or normalized.get("reward_mean") not in (None, "")
            or normalized.get("win_rate") not in (None, "")
        )
    )
    semantic_validation = validate_action_preference_family_consistency(normalized)
    derived_canonical = derived_canonical and bool(semantic_validation.get("ok"))
    explicit_canonical_false = canonical_flag_present and (
        explicit_canonical is False
        or explicit_canonical == 0
        or str(explicit_canonical).strip().lower() in {"false", "no", "off"}
    )
    if explicit_canonical_false:
        normalized["canonical_action_value"] = False
        normalized["canonical_action_value_source"] = "incomplete_trace_not_for_pm_scoring"
    else:
        normalized["canonical_action_value"] = derived_canonical
        normalized["canonical_action_value_source"] = (
            "top_level_first_payload_compatible"
            if derived_canonical
            else "incomplete_trace_not_for_pm_scoring"
        )
    return normalized


class _ExplicitPMLearningScopeDBView:
    """Expose PM memory rows only when their stored consumer scope is explicit."""

    def __init__(self, db) -> None:
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def get_alpha_setup_action_values(self, **kwargs):
        rows = self._db.get_alpha_setup_action_values(**kwargs)
        scoped_rows: list[dict] = []
        requested_setup = str(kwargs.get("setup_type") or "").strip().lower()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            consumer_scope = str(
                row.get("consumer_scope")
                or payload.get("consumer_scope")
                or ""
            ).strip().lower()
            if consumer_scope != "pm_learning":
                continue
            if requested_setup and requested_setup != "*":
                row_setup = str(
                    row.get("setup_type")
                    or payload.get("setup_type")
                    or ""
                ).strip().lower()
                if row_setup != requested_setup:
                    continue
            scoped_rows.append(row)
        return scoped_rows


def _is_pm_learning_action_value(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    normalized = _normalize_alpha_setup_action_value(row)
    return str(normalized.get("consumer_scope") or "").lower() == "pm_learning"


def _annotate_pm_action_value_retrieval(row: dict, *, match_level: str, match_reason: str) -> dict:
    normalized = _normalize_alpha_setup_action_value(row)
    normalized["retrieval_match_level"] = match_level
    normalized["retrieval_match_reason"] = match_reason
    payload = normalized.get("payload") if isinstance(normalized.get("payload"), dict) else {}
    if payload:
        payload = dict(payload)
        payload["retrieval_match_level"] = match_level
        payload["retrieval_match_reason"] = match_reason
        normalized["payload"] = payload
    return normalized


def _pm_canonical_action_value_rank(row: dict) -> tuple[int, int, int, int, int, float, int]:
    normalized = _normalize_alpha_setup_action_value(row)
    scope = str(normalized.get("evidence_scope") or "").lower()
    reward_source = str(normalized.get("reward_source") or "").lower()
    match_level = str(normalized.get("retrieval_match_level") or "").lower()
    preference = str(normalized.get("action_preference") or "").lower()
    canonical = bool(normalized.get("canonical_action_value"))
    return (
        0 if canonical else 1,
        0 if any(marker in reward_source for marker in ("episode", "real_trade", "complete_episode")) else 1,
        0 if scope == "exact_real_state" else 1 if scope == "partial_real_state" else 2 if scope == "similar_sql_prior" else 3,
        0 if preference else 1,
        0 if match_level == "exact_state" else 1 if match_level == "same_ticker_side_horizon" else 2 if match_level == "same_ticker_side" else 3,
        -abs(_safe_float(normalized.get("reward_sum"), 0.0)),
        -int(_safe_int(normalized.get("sample_count"), 0)),
    )


def _select_learning_trace_action_values(rows: list | None, limit: int = 10) -> list[dict]:
    normalized = [
        row
        for row in _normalize_alpha_setup_action_values(rows)
        if _is_complete_pm_scoring_action_value(row)
    ]
    normalized.sort(key=_pm_canonical_action_value_rank)
    compacted: list[dict] = []
    for row in normalized:
        compact = _compact_alpha_setup_action_value(row)
        if compact:
            compacted.append(compact)
        if len(compacted) >= int(limit):
            break
    return compacted


def _is_complete_pm_scoring_action_value(row: dict) -> bool:
    normalized = _normalize_alpha_setup_action_value(row)
    if not normalized:
        return False
    semantic_validation = validate_action_preference_family_consistency(normalized)
    action_value_lane = str(normalized.get("action_value_lane") or "").strip().lower()
    learning_lane = str(normalized.get("learning_lane") or "").strip().lower()
    return (
        normalized.get("canonical_action_value") is True
        and str(normalized.get("consumer_scope") or "").strip().lower() == "pm_learning"
        and bool(str(normalized.get("canonical_action_family") or "").strip())
        and bool(str(normalized.get("action_preference") or "").strip())
        and bool(action_value_lane)
        and action_value_lane == learning_lane
        and bool(semantic_validation.get("ok"))
        and str(normalized.get("canonical_action_value_source") or "").strip().lower()
        != "incomplete_trace_not_for_pm_scoring"
    )


def _is_incomplete_pm_prior_action_value(row: dict) -> bool:
    normalized = _normalize_alpha_setup_action_value(row)
    if not normalized or _is_complete_pm_scoring_action_value(normalized):
        return False
    payload = normalized.get("payload") if isinstance(normalized.get("payload"), dict) else {}
    evidence_scope = str(normalized.get("evidence_scope") or payload.get("evidence_scope") or "").strip().lower()
    match_level = str(normalized.get("retrieval_match_level") or payload.get("retrieval_match_level") or "").strip().lower()
    match_reason = str(normalized.get("retrieval_match_reason") or payload.get("retrieval_match_reason") or "").strip().lower()
    prior_role = str(payload.get("prior_role") or normalized.get("prior_role") or "").strip().lower()
    canonical_source = str(
        normalized.get("canonical_action_value_source")
        or payload.get("canonical_action_value_source")
        or ""
    ).strip().lower()
    return (
        normalized.get("canonical_action_value") is False
        and (
            evidence_scope in {"similar_sql_prior", "counterfactual_prior"}
            or match_level in {"similar", "weak_prior"}
            or "fallback" in match_reason
            or prior_role == "weak_prior_not_action_preference"
            or canonical_source == "incomplete_trace_not_for_pm_scoring"
        )
    )


def _compact_rejected_pm_prior_action_value(row: dict) -> dict:
    compact = _compact_alpha_setup_action_value(row)
    if not compact:
        return {}
    compact["reason"] = "incomplete_prior_not_pm_scoring_evidence"
    compact["diagnostic_only"] = True
    return compact


def _select_rejected_pm_prior_action_values(rows: list | None, limit: int = 20) -> list[dict]:
    rejected: list[dict] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for row in _normalize_alpha_setup_action_values(rows):
        if not _is_incomplete_pm_prior_action_value(row):
            continue
        compact = _compact_rejected_pm_prior_action_value(row)
        if not compact:
            continue
        key = (
            str(compact.get("id") or ""),
            str(compact.get("ticker") or ""),
            str(compact.get("side") or ""),
            str(compact.get("setup_type") or ""),
            str(compact.get("action_name") or ""),
            str(compact.get("reason") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        rejected.append(compact)
        if len(rejected) >= int(limit):
            break
    return rejected


def _merge_rejected_or_downgraded(existing: list | None, additions: list | None) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for item in list(existing or []) + list(additions or []):
        if not isinstance(item, dict) or not item:
            continue
        key = (
            str(item.get("id") or ""),
            str(item.get("ticker") or ""),
            str(item.get("side") or ""),
            str(item.get("setup_type") or ""),
            str(item.get("action_name") or ""),
            str(item.get("reason") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(item))
    return merged


def _attach_incomplete_prior_diagnostics_to_contract_state(contract_state: dict) -> dict:
    if not isinstance(contract_state, dict):
        return contract_state
    rejected_priors = _select_rejected_pm_prior_action_values(
        contract_state.get("alpha_setup_action_values")
    )
    if not rejected_priors:
        return contract_state
    updated = dict(contract_state)
    diagnostics = (
        dict(updated.get("control_diagnostics"))
        if isinstance(updated.get("control_diagnostics"), dict)
        else {}
    )
    memory_retrieval = (
        dict(diagnostics.get("final_action_memory_retrieval"))
        if isinstance(diagnostics.get("final_action_memory_retrieval"), dict)
        else {"tool": "decision_memory_retrieval"}
    )
    memory_retrieval["rejected_or_downgraded"] = _merge_rejected_or_downgraded(
        memory_retrieval.get("rejected_or_downgraded"),
        rejected_priors,
    )
    diagnostics["final_action_memory_retrieval"] = memory_retrieval
    updated["control_diagnostics"] = diagnostics
    return updated


def _normalize_alpha_setup_action_values(rows: list | None) -> list[dict]:
    return [
        normalized
        for normalized in (_normalize_alpha_setup_action_value(row) for row in (rows or []))
        if normalized
    ]


def _formal_pm_learning_action_values(rows: list | None) -> list[dict]:
    formal: list[dict] = []
    for normalized in _normalize_alpha_setup_action_values(rows):
        payload = normalized.get("payload") if isinstance(normalized.get("payload"), dict) else {}
        retrieval_match_level = str(
            normalized.get("retrieval_match_level")
            or payload.get("retrieval_match_level")
            or ""
        ).strip().lower()
        evidence_scope = str(
            normalized.get("evidence_scope")
            or payload.get("evidence_scope")
            or ""
        ).strip().lower()
        if not _is_complete_pm_scoring_action_value(normalized):
            continue
        if retrieval_match_level in {"similar", "weak_prior"}:
            continue
        if evidence_scope in {"similar_sql_prior", "counterfactual_prior"}:
            continue
        formal.append(normalized)
    return formal


def _append_unique_action_values(base_rows: list | None, extra_rows: list | None) -> list[dict]:
    rows: list[dict] = _normalize_alpha_setup_action_values(base_rows)
    index = {_action_value_key(row): pos for pos, row in enumerate(rows)}
    for row in extra_rows or []:
        if not isinstance(row, dict):
            continue
        normalized = _normalize_alpha_setup_action_value(row)
        key = _action_value_key(normalized)
        if key in index:
            existing_pos = index[key]
            if _action_value_row_completeness(normalized) > _action_value_row_completeness(rows[existing_pos]):
                rows[existing_pos] = normalized
            continue
        rows.append(normalized)
        index[key] = len(rows) - 1
    return rows


def _audit_frozen_step4_pm_memory(
    *,
    contract: dict,
    alpha_setup_action_values: list | None,
) -> tuple[list[dict], dict]:
    """Audit final-lifecycle routing without extending the frozen Step4 pool."""
    requirements = derive_memory_requirements(contract)
    frozen_rows = _formal_pm_learning_action_values(alpha_setup_action_values)
    lifecycle_filter = filter_action_values_for_contract_learning(
        contract,
        frozen_rows,
    )
    audit = {
        "tool": "decision_memory_retrieval",
        "boundary": "frozen_step4_complete_canonical_pool_routed_without_late_retrieval",
        "status": "frozen_step4_pool",
        "memory_requirements": requirements,
        "alpha_setup_action_value_count_after_lifecycle": len(frozen_rows),
        "lifecycle_matching_row_count": len(lifecycle_filter.get("rows") or []),
        "rejected_action_values": lifecycle_filter.get("rejected_action_values") or [],
        "late_retrieval_performed": False,
        "late_action_value_append_count": 0,
    }
    return frozen_rows, audit


_OPEN_OR_ADD_ACTION_NAMES = {
    "open",
    "add",
    "add_or_open",
    "increase",
    "increase_position",
    "probe",
    "open_probe",
}
_HOLD_OR_OBSERVE_ACTION_NAMES = {
    "hold",
    "hold_position",
    "continue_hold",
    "observe",
    "watchlist",
}
_REDUCE_OR_EXIT_ACTION_NAMES = {
    "reduce",
    "reduce_or_exit",
    "exit",
    "close",
    "close_or_reduce",
    "flatten",
}
_POSITIVE_OPEN_ACTION_PREFERENCES = {
    "positive_candidate_open",
}
_POSITIVE_HOLD_ACTION_PREFERENCES = {
    "positive_candidate_hold",
}
_POSITIVE_EXIT_ACTION_PREFERENCES = {
    "positive_candidate_exit",
}
_POSITIVE_EXECUTION_ACTION_PREFERENCES = {
    "positive_candidate_execution",
}
_NEGATIVE_ACTION_PREFERENCES = {
    "negative_revalidate",
    "negative_hold_revalidate",
    "tail_loss_protect",
}
_CANONICAL_ACTION_PREFERENCES = (
    _POSITIVE_OPEN_ACTION_PREFERENCES
    | _POSITIVE_HOLD_ACTION_PREFERENCES
    | _POSITIVE_EXIT_ACTION_PREFERENCES
    | _POSITIVE_EXECUTION_ACTION_PREFERENCES
    | _NEGATIVE_ACTION_PREFERENCES
)


def _canonical_action_preference(value: object) -> str:
    preference = str(value or "").strip().lower()
    return preference if preference in _CANONICAL_ACTION_PREFERENCES else ""


def _action_value_preference(row: dict) -> str:
    if isinstance(row, dict):
        preference = _canonical_action_preference(row.get("action_preference"))
        if preference:
            return preference
    payload = _action_value_payload(row)
    preference = _canonical_action_preference(payload.get("action_preference"))
    if preference:
        return preference
    return ""


def _intended_alpha_setup_action(position_ratio: float, current_ratio: float) -> str:
    if abs(position_ratio) <= 1e-12:
        return "exit" if abs(current_ratio) > 1e-12 else "observe"
    if abs(current_ratio) <= 1e-12:
        return "open"
    if not _same_sign(position_ratio, current_ratio):
        return "reverse"
    if abs(position_ratio) > abs(current_ratio) + 1e-12:
        return "add"
    if abs(position_ratio) < abs(current_ratio) - 1e-12:
        return "reduce"
    return "hold"


def _alpha_action_value_matches_intent(action_name: str, intended_action: str) -> bool:
    text = str(action_name or "").strip().lower()
    if not text:
        return False
    if intended_action in {"open", "add", "reverse"}:
        return text in _OPEN_OR_ADD_ACTION_NAMES
    if intended_action in {"reduce", "exit"}:
        return text in _REDUCE_OR_EXIT_ACTION_NAMES
    if intended_action == "hold":
        return text in _HOLD_OR_OBSERVE_ACTION_NAMES
    if intended_action == "observe":
        return text in _HOLD_OR_OBSERVE_ACTION_NAMES
    return False


def _action_value_rows_for_side(
    rows: list | None,
    *,
    ticker: str | None = None,
    side: str,
    actions: set[str],
    require_ticker_specific: bool = False,
) -> list[dict]:
    matched: list[dict] = []
    target_ticker = str(ticker or "").strip().upper()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        row_ticker = str(row.get("ticker") or "").strip().upper()
        if require_ticker_specific and (not target_ticker or row_ticker != target_ticker):
            continue
        if target_ticker and row_ticker not in {"", "*", target_ticker}:
            continue
        row_side = str(row.get("side") or "*").strip().lower()
        if row_side not in {"*", side}:
            continue
        action_name = str(row.get("action_name") or "").strip().lower()
        if action_name in actions:
            matched.append(row)
    return matched


_ACTION_VALUE_SCOPE_QUALITIES = {
    "exact_real_state",
    "partial_real_state",
    "similar_sql_prior",
    "counterfactual_prior",
    "unqualified",
}
_INCOMPLETE_ACTION_VALUE_SETUP_TYPES = {
    "",
    "*",
    "unknown",
    "generic_trade_setup",
    "watch_for_trigger",
    "probe_candidate",
    "tradeable_candidate",
    "no_opportunity",
    "risk_reduction_candidate",
}


def _action_value_payload(row: dict) -> dict:
    return row.get("payload") if isinstance(row, dict) and isinstance(row.get("payload"), dict) else {}


def _action_value_int(row: dict, key: str, default: int = 0) -> int:
    payload = _action_value_payload(row)
    value = payload.get(key)
    if value is None and isinstance(row, dict):
        value = row.get(key)
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _action_value_has_complete_state(row: dict, ticker: str | None = None, side: str | None = None) -> bool:
    if not isinstance(row, dict):
        return False
    target_ticker = str(ticker or "").strip().upper()
    target_side = str(side or "").strip().lower()
    row_ticker = str(row.get("ticker") or "").strip().upper()
    row_side = str(row.get("side") or "").strip().lower()
    row_horizon = str(row.get("horizon_class") or "").strip().lower()
    row_regime = str(row.get("market_regime") or "").strip().lower()
    row_setup = str(row.get("setup_type") or "").strip().lower()
    row_action = str(row.get("action_name") or "").strip().lower()
    if not row_ticker or row_ticker == "*":
        return False
    if target_ticker and row_ticker != target_ticker:
        return False
    if row_side in {"", "*", "unknown"}:
        return False
    if target_side and row_side != target_side:
        return False
    if row_horizon in {"", "*", "unknown"}:
        return False
    if row_regime in {"", "*", "unknown"}:
        return False
    if row_setup in _INCOMPLETE_ACTION_VALUE_SETUP_TYPES:
        return False
    if row_action in {"", "*", "unknown"}:
        return False
    return True


def _action_value_scope_quality(row: dict, ticker: str | None = None, side: str | None = None) -> str:
    if not isinstance(row, dict):
        return "unqualified"
    target_ticker = str(ticker or "").strip().upper()
    target_side = str(side or "").strip().lower()
    row_ticker = str(row.get("ticker") or "").strip().upper()
    row_side = str(row.get("side") or "").strip().lower()
    payload = _action_value_payload(row)
    explicit_quality = str(
        row.get("evidence_scope")
        or row.get("amplification_scope_quality")
        or payload.get("evidence_scope")
        or payload.get("amplification_scope_quality")
        or ""
    ).strip().lower()
    retrieval_match_level = str(
        row.get("retrieval_match_level")
        or payload.get("retrieval_match_level")
        or ""
    ).strip().lower()
    if (
        retrieval_match_level in {"same_ticker_side_horizon", "same_ticker_side"}
        and explicit_quality == "exact_real_state"
    ):
        return "partial_real_state"
    if explicit_quality in _ACTION_VALUE_SCOPE_QUALITIES:
        if explicit_quality == "exact_real_state":
            if not _action_value_has_complete_state(row, ticker=ticker, side=side):
                return "partial_real_state"
            if target_ticker and row_ticker not in {target_ticker, ""}:
                return "partial_real_state"
            if target_side and row_side not in {target_side, ""}:
                return "partial_real_state"
        return explicit_quality
    if bool(payload.get("counterfactual_prior_only")):
        return "counterfactual_prior"
    source = str(payload.get("source") or row.get("source") or "").strip().lower()
    if source == "similar_alpha_setup_sql":
        if _action_value_int(row, "exact_state_real_trade_sample_count") > 0:
            return "exact_real_state"
        if (
            _action_value_int(row, "partial_state_real_trade_sample_count") > 0
            or _action_value_int(row, "exact_ticker_real_trade_sample_count") > 0
            or _action_value_int(row, "exact_ticker_sample_count") > 0
        ):
            return "partial_real_state"
        if _action_value_int(row, "real_trade_reward_count") > 0:
            return "similar_sql_prior"
        if _action_value_int(row, "counterfactual_reward_count") > 0:
            return "counterfactual_prior"
        return "similar_sql_prior"
    if _action_value_int(row, "real_trade_reward_count") > 0:
        if target_ticker and row_ticker and row_ticker != target_ticker:
            return "similar_sql_prior"
        if target_side and row_side and row_side not in {target_side, "*"}:
            return "similar_sql_prior"
        return "partial_real_state"
    if _action_value_int(row, "counterfactual_reward_count") > 0:
        return "counterfactual_prior"
    if source:
        return "unqualified"
    if row_ticker and target_ticker and row_ticker not in {target_ticker, "*"}:
        return "similar_sql_prior"
    if row_ticker == "*" and target_ticker:
        return "partial_real_state"
    return "unqualified"


def _action_value_has_exact_ticker_support(row: dict, ticker: str | None, side: str | None = None) -> bool:
    return _action_value_scope_quality(row, ticker=ticker, side=side) in {
        "exact_real_state",
        "partial_real_state",
    }


def _action_value_can_support_real_amplification(row: dict, ticker: str | None, side: str | None = None) -> bool:
    return _action_value_scope_quality(row, ticker=ticker, side=side) in {
        "exact_real_state",
    }


def _execution_profile_from_action_value(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    retrieval_key = str(
        row.get("execution_retrieval_key")
        or payload.get("execution_retrieval_key")
        or ""
    ).strip()
    parts = retrieval_key.split("|")
    if len(parts) == 4 and str(parts[-1]).strip().lower() == "execution":
        return normalize_execution_profile(parts[1])
    return ""


def _execution_action_value_preference(
    *,
    ticker: str,
    side: str,
    base_profile: str,
    alpha_setup_action_values: list | None,
    final_entry_authority: dict,
) -> dict:
    """Convert execution action-value into a Trader profile preference.

    The preference never creates authority and never bypasses PM/Auditor. It
    only changes how an already-authorized ordinary entry waits for confirmation.
    """
    if side not in {"long", "short"} or base_profile not in {"breakout", "pullback"}:
        return {}
    authority_type = str((final_entry_authority or {}).get("authority_type") or "")
    if authority_type not in {"exploration_probe", "real_budget_entry"}:
        return {}

    min_conf = 0.35
    min_samples = 2
    candidates: list[dict] = []
    for row in _action_value_rows_for_side(
        alpha_setup_action_values,
        ticker=ticker,
        side=side,
        actions={"execution"},
        require_ticker_specific=True,
    ):
        if not _action_value_can_support_real_amplification(row, ticker, side):
            continue
        profile = _execution_profile_from_action_value(row)
        if profile not in {"breakout", "pullback", "vwap_confirmed"}:
            continue
        sample_count = int(row.get("sample_count") or 0)
        confidence = _safe_float(row.get("confidence_score"), 0.0)
        preference = _action_value_preference(row)
        exact_positive_candidate = bool(
            preference in _POSITIVE_EXECUTION_ACTION_PREFERENCES
            and _action_value_can_support_real_amplification(row, ticker, side)
            and _safe_float(row.get("reward_mean"), 0.0) > 0
            and _safe_float(row.get("reward_sum"), 0.0) > 0
        )
        if not exact_positive_candidate and (sample_count < min_samples or confidence < min_conf):
            continue
        reward_mean = _safe_float(row.get("reward_mean"), 0.0)
        reward_sum = _safe_float(row.get("reward_sum"), 0.0)
        win_rate = _safe_float(row.get("win_rate"), 0.0)
        candidate = dict(row)
        candidate["_execution_profile"] = profile
        candidate["_action_value_preference"] = preference
        candidate["_rank"] = (
            1 if exact_positive_candidate else 0,
            reward_mean,
            reward_sum,
            win_rate,
            confidence,
            sample_count,
        )
        candidates.append(candidate)

    if not candidates:
        return {}

    best_by_profile: dict[str, dict] = {}
    for row in candidates:
        profile = row["_execution_profile"]
        current = best_by_profile.get(profile)
        if current is None or row["_rank"] > current["_rank"]:
            best_by_profile[profile] = row

    positive = [
        row for row in best_by_profile.values()
        if _safe_float(row.get("reward_mean"), 0.0) > 0 or _safe_float(row.get("reward_sum"), 0.0) > 0
    ]
    selected = max(positive, key=lambda row: row["_rank"]) if positive else None

    current_profile_row = best_by_profile.get(base_profile)
    if selected is None:
        if not current_profile_row:
            return {}
        current_bad = (
            _safe_float(current_profile_row.get("reward_mean"), 0.0) < 0
            or _safe_float(current_profile_row.get("reward_sum"), 0.0) < 0
        )
        if not current_bad:
            return {}
        target_profile = "pullback" if base_profile == "breakout" else "vwap_confirmed"
        selected = dict(current_profile_row)
        selected["_execution_profile"] = target_profile
    else:
        target_profile = selected["_execution_profile"]
        if target_profile == base_profile:
            return {}

    selected.pop("_rank", None)
    return {
        "enabled": True,
        "source": "execution_action_value",
        "execution_profile": target_profile,
        "trigger_source": f"execution_action_value_{target_profile}",
        "base_execution_profile": base_profile,
        "reason_codes": ["execution_action_value_preference"],
        "selected_action_value": _compact_alpha_setup_action_value(selected),
        "same_scope_required": True,
        "does_not_create_trade_authority": True,
        "keeps_pm_authority_boundary": True,
    }


def _current_open_evidence_snapshot(
    *,
    side: str,
    analyst_signals: list,
    opportunity_scorecard: dict | None,
    market_confirmation: dict | None,
    ev_cfg: dict,
) -> dict:
    """Build the same current-day evidence fields the final PM outlet consumes."""
    scorecard = opportunity_scorecard if isinstance(opportunity_scorecard, dict) else {}
    side_scorecard = scorecard.get(side) if isinstance(scorecard.get(side), dict) else {}
    layer = str(side_scorecard.get("final_state") or "unknown").lower()
    confirmation_score = _safe_float((market_confirmation or {}).get("confirmation_score"), 0.0)
    has_tradeable_support = layer in {"tradeable_candidate", "probe_candidate"}
    setup_quality_ok = _scorecard_setup_quality_ok(side_scorecard)
    has_monitorable_setup = _scorecard_monitorable_setup(side_scorecard)
    (
        has_entry_invalidation,
        has_position_exit_boundary,
    ) = _target_execution_lifecycle_boundaries(
        analyst_signals or [],
        side,
    )
    payloads = _analyst_signal_payloads(analyst_signals or {})
    min_support_confidence = float(ev_cfg.get("real_trade_min_analyst_confidence", 0.45) or 0.45)
    technical_payload = payloads.get("technical", {})
    fundamental_payload = payloads.get("fundamental", {})
    news_payload = payloads.get("commodity_news", {})
    technical_direction_supports_side = _payload_supports_side(
        technical_payload,
        side,
        min_support_confidence,
    )
    technical_entry_timing_supports_side = _technical_payload_has_entry_timing(
        technical_payload,
        side,
        min_support_confidence,
    )
    technical_opposes_side = _payload_opposes_side(technical_payload, side, min_support_confidence)
    fundamental_supports_side = _payload_supports_side(fundamental_payload, side, min_support_confidence)
    fundamental_opposes_side = _payload_opposes_side(fundamental_payload, side, min_support_confidence)
    news_supports_side = _payload_supports_side(news_payload, side, min_support_confidence)
    news_override = _news_high_quality_override(news_payload, side, ev_cfg)
    independent_support_count = sum(
        1 for item in (
            technical_entry_timing_supports_side,
            fundamental_supports_side,
            news_supports_side,
        )
        if item
    )
    min_confirmation = float(ev_cfg.get("min_confirmation_score", 0.52) or 0.52)
    strong_confirmation_score = float(ev_cfg.get("real_trade_strong_confirmation_score", 0.65) or 0.65)
    strong_market_confirmation = confirmation_score >= strong_confirmation_score
    strong_realtime_evidence = bool(
        technical_entry_timing_supports_side
        or news_override
        or (
            independent_support_count >= 2
            and has_entry_invalidation
            and confirmation_score >= min_confirmation
        )
        or (
            strong_market_confirmation
            and has_tradeable_support
            and has_entry_invalidation
        )
    )
    alpha_ev = {
        "scorecard_state": layer,
        "strong_realtime_evidence": strong_realtime_evidence,
        "strong_market_confirmation": strong_market_confirmation,
        "technical_supports_side": technical_entry_timing_supports_side,
        "technical_direction_supports_side": technical_direction_supports_side,
        "technical_entry_timing_supports_side": technical_entry_timing_supports_side,
        "technical_opposes_side": technical_opposes_side,
        "fundamental_supports_side": fundamental_supports_side,
        "fundamental_opposes_side": fundamental_opposes_side,
        "news_supports_side": news_supports_side,
        "news_high_quality_override": news_override,
        "has_tradeable_support": has_tradeable_support,
        "has_monitorable_setup": has_monitorable_setup,
        "setup_quality_ok": setup_quality_ok,
        "has_entry_invalidation": has_entry_invalidation,
        "has_position_exit_boundary": has_position_exit_boundary,
        "current_confirmation_score": confirmation_score,
        "independent_support_count": independent_support_count,
    }
    alpha_ev["trade_authority"] = _alpha_ev_trade_authority(alpha_ev)
    return alpha_ev


def _positive_open_action_value_seed(
    *,
    ticker: str,
    alpha_setup_action_values: list | None,
    analyst_signals: list,
    opportunity_scorecard: dict | None,
    market_confirmation: dict | None,
    full_config: dict,
    max_position_ratio: float,
) -> dict:
    """Let learned positive open reward create a candidate only with current evidence."""
    pm_config = _get_portfolio_manager_config(full_config)
    ev_cfg = (pm_config.get("alpha_setup_ev_fusion") or {})
    min_action_conf = float(ev_cfg.get("min_action_value_confidence", 0.35) or 0.35)
    min_action_samples = int(ev_cfg.get("real_trade_min_action_value_samples", ev_cfg.get("min_action_value_samples", 2)) or 2)
    positive_reward_min = float(ev_cfg.get("positive_reward_mean_min", 0.0) or 0.0)
    positive_sum_min = float(ev_cfg.get("real_trade_positive_reward_sum_min", 0.0) or 0.0)
    scorecard = opportunity_scorecard if isinstance(opportunity_scorecard, dict) else {}
    preferred_side = str(scorecard.get("preferred_side") or "").strip().lower()
    if preferred_side not in {"long", "short"}:
        return {}
    candidates: list[dict] = []
    for side in (preferred_side,):
        for row in _action_value_rows_for_side(
            alpha_setup_action_values,
            ticker=ticker,
            side=side,
            actions=_OPEN_OR_ADD_ACTION_NAMES | {"reverse"},
            require_ticker_specific=True,
        ):
            preference = _action_value_preference(row)
            if preference not in _POSITIVE_OPEN_ACTION_PREFERENCES:
                continue
            if not _action_value_can_support_real_amplification(row, ticker, side):
                continue
            exact_positive_candidate = bool(
                preference in _POSITIVE_OPEN_ACTION_PREFERENCES
                and _safe_float(row.get("reward_mean"), 0.0) >= positive_reward_min
                and _safe_float(row.get("reward_sum"), 0.0) > 0
            )
            mature_positive = bool(
                int(row.get("sample_count") or 0) >= min_action_samples
                and _safe_float(row.get("confidence_score"), 0.0) >= min_action_conf
            )
            if not (mature_positive or exact_positive_candidate):
                continue
            if _safe_float(row.get("reward_mean"), 0.0) < positive_reward_min:
                continue
            if _safe_float(row.get("reward_sum"), 0.0) < positive_sum_min:
                continue
            evidence = _current_open_evidence_snapshot(
                side=side,
                analyst_signals=analyst_signals,
                opportunity_scorecard=opportunity_scorecard,
                market_confirmation=market_confirmation,
                ev_cfg=ev_cfg,
            )
            authority = evidence.get("trade_authority") if isinstance(evidence.get("trade_authority"), dict) else {}
            if not bool(authority.get("open_action_evidence")):
                continue
            candidates.append({
                "side": side,
                "row": row,
                "evidence": evidence,
                "mature_positive_action_value": mature_positive,
                "candidate_positive_action_preference": bool(exact_positive_candidate and not mature_positive),
                "rank": (
                    1 if mature_positive else 0,
                    _safe_float(row.get("reward_sum"), 0.0),
                    _safe_float(row.get("reward_mean"), 0.0),
                    _safe_float(row.get("confidence_score"), 0.0),
                    int(row.get("sample_count") or 0),
                ),
            })
    if not candidates:
        return {}
    selected = max(candidates, key=lambda item: item["rank"])
    impact = _safe_float(selected["row"].get("max_position_impact"), 0.0)
    if impact <= 0:
        impact = abs(float(max_position_ratio or 0.0))
    selected["seed_position_ratio"] = min(abs(float(max_position_ratio or 0.0)), abs(impact))
    return selected


def _negative_hold_or_positive_exit_action_value(
    *,
    ticker: str,
    alpha_setup_action_values: list | None,
    side: str,
    ev_cfg: dict,
) -> dict:
    """Find same-action learning that says continuing the current hold is weak."""
    min_conf = float(ev_cfg.get("min_action_value_confidence", 0.35) or 0.35)
    min_samples = int(ev_cfg.get("min_action_value_samples", 2) or 2)
    negative_reward_max = float(ev_cfg.get("negative_reward_mean_max", -1e-9) or -1e-9)
    negative_sum_max = float(ev_cfg.get("negative_reward_sum_max", -500.0) or -500.0)
    candidates: list[dict] = []
    for row in _action_value_rows_for_side(
        alpha_setup_action_values,
        ticker=ticker,
        side=side,
        actions=_HOLD_OR_OBSERVE_ACTION_NAMES | _REDUCE_OR_EXIT_ACTION_NAMES,
        require_ticker_specific=True,
    ):
        action_name = str(row.get("action_name") or "").strip().lower()
        preference = _action_value_preference(row)
        sample_count = int(row.get("sample_count") or 0)
        confidence = _safe_float(row.get("confidence_score"), 0.0)
        reward_mean = _safe_float(row.get("reward_mean"), 0.0)
        reward_sum = _safe_float(row.get("reward_sum"), 0.0)
        exact_action_preference = bool(
            _action_value_can_support_real_amplification(row, ticker, side)
            and preference in (
                _POSITIVE_EXIT_ACTION_PREFERENCES
                | _NEGATIVE_ACTION_PREFERENCES
            )
        )
        if not exact_action_preference and (sample_count < min_samples or confidence < min_conf):
            continue
        bad_hold = (
            action_name in _HOLD_OR_OBSERVE_ACTION_NAMES
            and (
                preference in {"negative_hold_revalidate", "tail_loss_protect"}
                or reward_mean <= negative_reward_max
                or reward_sum <= negative_sum_max
            )
        )
        helpful_exit = (
            action_name in _REDUCE_OR_EXIT_ACTION_NAMES
            and (
                preference in _POSITIVE_EXIT_ACTION_PREFERENCES
                or preference == "tail_loss_protect"
                or reward_mean > 0
                or reward_sum > 0
            )
        )
        if not (bad_hold or helpful_exit):
            continue
        candidate = dict(row)
        candidate["_action_value_preference"] = "bad_hold" if bad_hold else "helpful_exit"
        candidate["_action_preference"] = preference
        candidate["_rank"] = (
            1 if exact_action_preference else 0,
            confidence,
            sample_count,
            abs(reward_sum),
            abs(reward_mean),
        )
        candidates.append(candidate)
    if not candidates:
        return {}
    selected = max(candidates, key=lambda row: row["_rank"])
    selected.pop("_rank", None)
    return selected


def _alpha_setup_action_value_trace(alpha_setup_action_values: list | None) -> dict:
    rows = [
        _compact_alpha_setup_action_value(row)
        for row in (alpha_setup_action_values or [])
        if isinstance(row, dict)
    ]
    rows = [row for row in rows if row]
    preference_counts: dict[str, int] = {}
    canonical_count = 0
    incomplete_count = 0
    for row in rows:
        key = str(row.get("action_preference") or "none")
        preference_counts[key] = preference_counts.get(key, 0) + 1
        if (
            row.get("action_preference")
            and row.get("reward_source")
            and row.get("evidence_scope")
            and (
                row.get("reward_sum") is not None
                or row.get("reward_mean") is not None
                or row.get("win_rate") is not None
            )
        ):
            canonical_count += 1
        else:
            incomplete_count += 1
    action_groups = {
        "open_action_value": [],
        "hold_action_value": [],
        "exit_action_value": [],
        "execution_action_value": [],
    }
    for row in rows:
        action_name = str(row.get("action_name") or "").strip().lower()
        if action_name in _OPEN_OR_ADD_ACTION_NAMES or action_name == "reverse":
            action_groups["open_action_value"].append(row)
        elif action_name in _HOLD_OR_OBSERVE_ACTION_NAMES:
            action_groups["hold_action_value"].append(row)
        elif action_name in _REDUCE_OR_EXIT_ACTION_NAMES:
            action_groups["exit_action_value"].append(row)
        elif "execution" in action_name or "trigger" in action_name or "fill" in action_name:
            action_groups["execution_action_value"].append(row)
    return {
        "action_value_count": len(rows),
        "canonical_action_value_count": canonical_count,
        "incomplete_trace_action_value_count": incomplete_count,
        "action_preference_counts": preference_counts,
        "canonical_action_preference_source": "top_level_first_payload_compatible",
        "action_values": rows[:8],
        "open_action_value": action_groups["open_action_value"][:4],
        "hold_action_value": action_groups["hold_action_value"][:4],
        "exit_action_value": action_groups["exit_action_value"][:4],
        "execution_action_value": action_groups["execution_action_value"][:4],
        "action_value_contract": "open_hold_exit_execution_are_separate_and_must_not_substitute_for_each_other",
        "same_scope_required": True,
        "not_product_blacklist": True,
        "position_authority": "positive_scale_unknown_probe_negative_cap_exit_only_after_current_evidence",
    }


def _learning_to_position_trace(
    *,
    pm_learning_audit: dict,
    adaptive_policy_state: list | None,
    strategy_memory: dict,
    current_lots: int,
    target_lots: int,
    pre_open_action: FuturesAction,
    pre_open_lots: int,
    lots_to_trade_reason,
    pre_control_ratio: float,
    final_position_ratio: float,
    control_reasons: list,
    holding_diagnostics: dict,
    market_confirmation: dict,
    pm_risk_gate_payload: dict | None,
    analyst_signals: list,
    opportunity_scorecard: dict | None = None,
    alpha_setup_profiles: list | None = None,
    alpha_setup_action_values: list | None = None,
) -> dict:
    final_side = _target_side_from_ratio(final_position_ratio)
    state_summary = _side_opportunity_state_summary(analyst_signals or [], final_side) if final_side in {"long", "short"} else {}
    mature_alpha_records = _mature_alpha_policy_records(adaptive_policy_state, final_side) if final_side in {"long", "short"} else []
    fast_alpha_records = _fast_candidate_alpha_records(adaptive_policy_state, final_side) if final_side in {"long", "short"} else []
    scorecard = opportunity_scorecard if isinstance(opportunity_scorecard, dict) else {}
    scorecard_side = scorecard.get(final_side) if final_side in {"long", "short"} and isinstance(scorecard.get(final_side), dict) else {}
    trace = {
        "learning_context": {
            "enabled": bool(pm_learning_audit.get("enabled")),
            "selected_digest_ids": pm_learning_audit.get("selected_digest_ids", []),
            "memory_trace": pm_learning_audit.get("memory_trace", {}),
            "candidate_hypothesis_count": int(pm_learning_audit.get("candidate_hypothesis_count", 0) or 0),
            "validated_hypothesis_count": int(pm_learning_audit.get("validated_hypothesis_count", 0) or 0),
            "candidate_hypothesis_authority": pm_learning_audit.get("candidate_hypothesis_authority"),
        },
        "adaptive_policy_state": _adaptive_policy_trace(adaptive_policy_state),
        "alpha_setup_profiles": _alpha_setup_profile_trace(alpha_setup_profiles),
        "alpha_setup_action_values": _alpha_setup_action_value_trace(alpha_setup_action_values),
        "strategy_memory": strategy_memory if isinstance(strategy_memory, dict) else {},
        "position_effect": {
            "current_lots": int(current_lots),
            "target_lots": int(target_lots),
            "lots_delta": int(target_lots - current_lots),
            "pre_control_position_ratio": float(pre_control_ratio),
            "final_target_position_ratio": float(final_position_ratio),
            "action": pre_open_action.value if hasattr(pre_open_action, "value") else str(pre_open_action),
            "action_lots": int(pre_open_lots),
            "reason": lots_to_trade_reason,
            "control_reasons": sorted(set(control_reasons or [])),
        },
        "opportunity_to_position": {
            "target_side": final_side,
            "opportunity_state_summary": state_summary,
            "opportunity_scorecard_side": scorecard_side,
            "scorecard_preferred_side": scorecard.get("preferred_side"),
            "mature_alpha_policy_count": len(mature_alpha_records),
            "fast_candidate_alpha_count": len(fast_alpha_records),
            "high_quality_opportunity_present": bool(
                state_summary.get("has_tradeable_support")
                or mature_alpha_records
                or fast_alpha_records
                or str(scorecard_side.get("final_state") or "") in {"tradeable_candidate", "probe_candidate"}
            ),
            "high_quality_opportunity_executed_or_targeted": bool(
                abs(final_position_ratio) > 1e-12 or target_lots != current_lots
            ),
            "if_not_targeted_requires_accountability": bool(
                (
                    state_summary.get("has_tradeable_support")
                    or mature_alpha_records
                    or fast_alpha_records
                    or str(scorecard_side.get("final_state") or "") in {"tradeable_candidate", "probe_candidate"}
                )
                and abs(final_position_ratio) <= 1e-12
                and target_lots == current_lots
            ),
        },
        "current_day_validation": {
            "market_confirmation_score": (
                market_confirmation.get("confirmation_score")
                if isinstance(market_confirmation, dict)
                else None
            ),
            "has_structured_invalidation": _has_structured_invalidation_condition(
                analyst_signals or [],
                target_side=final_side,
            ),
            "has_explicit_stop_protection": _has_explicit_stop_protection(
                analyst_signals or [],
                target_side=final_side,
            ),
            "candidate_memory_cannot_hold_losing_position": True,
            "requires_today_signal_market_state_and_invalidation": True,
        },
        "holding_lifecycle": holding_diagnostics if isinstance(holding_diagnostics, dict) else {},
        "pm_risk_gate_decision": pm_risk_gate_payload or {},
        "trader_execution_pending": True,
        "future_outcome_pending_phase4": True,
        "anti_overfit_guardrail": {
            "not_product_blacklist": True,
            "no_future_outcome_used_for_current_decision": True,
            "candidate_memory_is_prior_only": True,
        },
    }
    return trace


def _contract_safe_learning_to_position_summary(trace: dict | None) -> dict:
    trace = trace if isinstance(trace, dict) else {}
    learning_context = trace.get("learning_context") if isinstance(trace.get("learning_context"), dict) else {}
    adaptive_policy = trace.get("adaptive_policy_state") if isinstance(trace.get("adaptive_policy_state"), dict) else {}
    alpha_profiles = trace.get("alpha_setup_profiles") if isinstance(trace.get("alpha_setup_profiles"), dict) else {}
    action_values = trace.get("alpha_setup_action_values") if isinstance(trace.get("alpha_setup_action_values"), dict) else {}
    position_effect = trace.get("position_effect") if isinstance(trace.get("position_effect"), dict) else {}
    opportunity = trace.get("opportunity_to_position") if isinstance(trace.get("opportunity_to_position"), dict) else {}
    current_day = trace.get("current_day_validation") if isinstance(trace.get("current_day_validation"), dict) else {}
    holding = trace.get("holding_lifecycle") if isinstance(trace.get("holding_lifecycle"), dict) else {}
    selected_digest_ids = learning_context.get("selected_digest_ids")
    selected_digest_count = len(selected_digest_ids) if isinstance(selected_digest_ids, list) else 0
    return {
        "trace_version": "agentquant.pm_learning_contract_summary.v1",
        "learning_context": {
            "enabled": bool(learning_context.get("enabled")),
            "selected_digest_count": selected_digest_count,
            "candidate_hypothesis_count": int(learning_context.get("candidate_hypothesis_count") or 0),
            "validated_hypothesis_count": int(learning_context.get("validated_hypothesis_count") or 0),
            "candidate_hypothesis_authority": learning_context.get("candidate_hypothesis_authority"),
        },
        "learning_source_summary": {
            "adaptive_policy_summary": {
                "policy_count": int(adaptive_policy.get("policy_count") or 0),
                "policy_type_counts": adaptive_policy.get("policy_type_counts") or {},
                "scope": adaptive_policy.get("scope") or "adaptive_policy_state_summary",
                "status": adaptive_policy.get("status") or "summary_only_no_policy_rows",
            },
            "alpha_setup_profile_summary": {
                "profile_count": int(alpha_profiles.get("profile_count") or 0),
                "lifecycle_counts": alpha_profiles.get("lifecycle_counts") or {},
                "status": "summary_only_no_profile_rows",
            },
            "action_value_summary": {
                "action_value_count": int(action_values.get("action_value_count") or 0),
                "canonical_action_value_count": int(action_values.get("canonical_action_value_count") or 0),
                "incomplete_trace_action_value_count": int(
                    action_values.get("incomplete_trace_action_value_count") or 0
                ),
                "action_preference_counts": action_values.get("action_preference_counts") or {},
                "status": "summary_only_no_action_value_rows",
            },
            "strategy_memory_summary": {
                "status": "summary_only_no_raw_strategy_memory",
                "raw_object_omitted": True,
            },
        },
        "position_effect": {
            "current_lots": position_effect.get("current_lots"),
            "target_lots": position_effect.get("target_lots"),
            "lots_delta": position_effect.get("lots_delta"),
            "pre_control_position_ratio": position_effect.get("pre_control_position_ratio"),
            "final_target_position_ratio": position_effect.get("final_target_position_ratio"),
            "action": position_effect.get("action"),
            "action_lots": position_effect.get("action_lots"),
            "reason": position_effect.get("reason"),
            "control_reasons": position_effect.get("control_reasons") or [],
        },
        "opportunity_to_position": {
            "target_side": opportunity.get("target_side"),
            "scorecard_preferred_side": opportunity.get("scorecard_preferred_side"),
            "mature_alpha_policy_count": int(opportunity.get("mature_alpha_policy_count") or 0),
            "fast_candidate_alpha_count": int(opportunity.get("fast_candidate_alpha_count") or 0),
            "high_quality_opportunity_present": bool(opportunity.get("high_quality_opportunity_present")),
            "high_quality_opportunity_executed_or_targeted": bool(
                opportunity.get("high_quality_opportunity_executed_or_targeted")
            ),
            "if_not_targeted_requires_accountability": bool(
                opportunity.get("if_not_targeted_requires_accountability")
            ),
        },
        "current_day_validation": {
            "market_confirmation_score": current_day.get("market_confirmation_score"),
            "has_structured_invalidation": bool(current_day.get("has_structured_invalidation")),
            "has_explicit_stop_protection": bool(current_day.get("has_explicit_stop_protection")),
            "requires_today_signal_market_state_and_invalidation": bool(
                current_day.get("requires_today_signal_market_state_and_invalidation")
            ),
        },
        "holding_lifecycle": {
            "decision": holding.get("decision"),
            "lifecycle_classification": holding.get("lifecycle_classification"),
            "holding_days": holding.get("held_days"),
            "current_side": holding.get("current_side"),
            "target_side": holding.get("raw_target_side"),
            "loss_revalidation_due": holding.get("loss_revalidation_due"),
            "loss_revalidation_failed": holding.get("loss_revalidation_failed"),
            "market_confirmation_score": holding.get("confirmation_score"),
        },
        "artifact_boundary": {
            "summary_only": True,
            "research_fact_objects_omitted": [
                "adaptive_policy_state",
                "strategy_memory",
                "adaptive_policy_scope.policies",
            ],
        },
    }


def _contract_safe_pm_risk_gate_alignment(payload: dict | None) -> dict:
    """Project the internal PM RiskGate verdict into a FAC-safe summary."""
    payload = payload if isinstance(payload, dict) else {}
    if not payload:
        return {}
    reasons = payload.get("reasons")
    return {
        "decision": payload.get("decision"),
        "target_side": payload.get("target_side"),
        "position_ratio_multiplier": payload.get("position_ratio_multiplier"),
        "confidence_multiplier": payload.get("confidence_multiplier"),
        "cap_multiplier": payload.get("cap_multiplier"),
        "reasons": [str(reason) for reason in reasons] if isinstance(reasons, list) else [],
        "policy_version": payload.get("policy_version"),
        "learning_mode": payload.get("learning_mode"),
    }


def _build_pm_landing_consistency_audit(
    *,
    ticker: str,
    current_lots: int,
    target_lots: int,
    current_position_ratio: float,
    final_position_ratio: float,
    recommendation_intent: dict,
    lots_to_trade: int,
    lots_to_trade_reason: str | None,
    opportunity_scorecard: dict | None,
    analyst_signals: list,
    pm_learning_audit: dict,
    adaptive_policy_state: list | None,
    alpha_setup_profiles: list | None,
    alpha_setup_action_values: list | None,
    pm_risk_gate_payload: dict | None,
    control_reasons: list,
    margin_required: float,
    margin_available: float,
    market_confirmation: dict,
) -> dict:
    scorecard = opportunity_scorecard if isinstance(opportunity_scorecard, dict) else {}
    side = _target_side_from_ratio(final_position_ratio)
    side_scorecard = scorecard.get(side) if side in {"long", "short"} and isinstance(scorecard.get(side), dict) else {}
    if not side_scorecard:
        preferred = str(scorecard.get("preferred_side") or "flat")
        side_scorecard = scorecard.get(preferred) if preferred in {"long", "short"} and isinstance(scorecard.get(preferred), dict) else {}
    layer = str(side_scorecard.get("final_state") or "unknown")
    setup_count = int(side_scorecard.get("entry_setup_count") or 0)
    invalidation_count = int(side_scorecard.get("invalidation_count") or 0)
    high_quality_state = layer in {"tradeable_candidate", "probe_candidate"}
    action_type = str((recommendation_intent or {}).get("action_type") or "unknown")
    lots_delta = int(target_lots - current_lots)
    targeted_or_executed = abs(float(final_position_ratio or 0.0)) > 1e-12 or lots_delta != 0
    policies = [
        row
        for row in (adaptive_policy_state or [])
        if isinstance(row, dict)
        and (
            str(row.get("policy_type") or "").startswith("learning_mechanism:")
            or str(row.get("policy_type") or "") in {
                "alpha_promotion",
                "fast_candidate_alpha",
                "fast_loss_sentinel",
                "tail_loss_sentinel",
                "loss_template_policy",
                "technical_parameter_calibration",
            }
        )
    ]
    setup_profile_trace = _alpha_setup_profile_trace(alpha_setup_profiles)
    action_value_trace = _alpha_setup_action_value_trace(alpha_setup_action_values)
    consistency_flags: list[str] = []
    if not scorecard:
        consistency_flags.append("missing_opportunity_scorecard")
    if layer == "no_opportunity" and abs(lots_delta) > 0 and action_type not in {"close_long", "close_short"}:
        consistency_flags.append("new_or_incremental_trade_against_no_trade_scorecard")
    if layer == "watch_for_trigger" and abs(lots_delta) > 0 and abs(float(final_position_ratio or 0.0)) > 0.035:
        consistency_flags.append("watch_for_trigger_position_above_probe_scope")
    if high_quality_state and not targeted_or_executed and not (control_reasons or lots_to_trade_reason):
        consistency_flags.append("high_quality_opportunity_not_targeted_without_reason")
    if high_quality_state and setup_count <= 0:
        consistency_flags.append("high_quality_scorecard_missing_setup")
    if high_quality_state and invalidation_count <= 0:
        consistency_flags.append("high_quality_scorecard_missing_invalidation")
    if lots_to_trade > 0 and margin_required > margin_available:
        consistency_flags.append("pre_execution_margin_insufficient")
    pm_risk_gate_decision = None
    if isinstance(pm_risk_gate_payload, dict):
        pm_risk_gate_decision = pm_risk_gate_payload.get("decision") or pm_risk_gate_payload.get("action")
    if pm_risk_gate_decision and str(pm_risk_gate_decision).lower() in {"block", "scale_down", "reduce_only"}:
        consistency_flags.append(f"pm_risk_gate_{str(pm_risk_gate_decision).lower()}")
    analyst_setup_summary = {}
    for signal in analyst_signals or []:
        agent = str(getattr(signal, "agent_name", "") or "unknown")
        metadata = getattr(signal, "metadata", {}) or {}
        analyst_setup_summary[agent] = {
            "signal": _signal_to_text(getattr(signal, "signal", None)),
            "opportunity_state": getattr(signal, "opportunity_state", None),
            "trade_setup_contract_status": metadata.get("trade_setup_contract_status"),
            "entry_trigger": getattr(signal, "entry_trigger", ""),
            "exit_hint": getattr(signal, "exit_hint", ""),
            "holding_period_hint": getattr(signal, "holding_period_hint", ""),
        }
    return {
        "version": "pm_landing_consistency_v1",
        "ticker": str(ticker).upper(),
        "decision": {
            "current_lots": int(current_lots),
            "target_lots": int(target_lots),
            "lots_delta": int(lots_delta),
            "current_position_ratio": float(current_position_ratio or 0.0),
            "final_position_ratio": float(final_position_ratio or 0.0),
            "recommendation_action": (recommendation_intent or {}).get("action"),
            "action_type": action_type,
            "lots_to_trade": int(lots_to_trade),
            "lots_to_trade_reason": lots_to_trade_reason,
            "control_reasons": sorted(set(control_reasons or [])),
        },
        "opportunity_scorecard_alignment": {
            "preferred_side": scorecard.get("preferred_side"),
            "target_side": side,
            "side_final_state": layer,
            "side_score": side_scorecard.get("score"),
            "opportunity_score": side_scorecard.get("opportunity_score", side_scorecard.get("score")),
            "opportunity_score_components": side_scorecard.get("opportunity_score_components") or {},
            "side_priority": side_scorecard.get("side_priority"),
            "ticker_side_priority": side_scorecard.get("ticker_side_priority"),
            "capital_allocation_reason": side_scorecard.get("capital_allocation_reason"),
            "learning_adjustment_summary": side_scorecard.get("learning_adjustment_summary") or {},
            "gating_failures": side_scorecard.get("gating_failures") or [],
            "entry_setup_count": setup_count,
            "invalidation_count": invalidation_count,
        },
        "analyst_setup_alignment": analyst_setup_summary,
        "learning_alignment": {
            "learning_enabled": bool(pm_learning_audit.get("enabled")) if isinstance(pm_learning_audit, dict) else False,
            "selected_digest_ids": pm_learning_audit.get("selected_digest_ids", []) if isinstance(pm_learning_audit, dict) else [],
            "candidate_hypothesis_count": int(pm_learning_audit.get("candidate_hypothesis_count", 0) or 0) if isinstance(pm_learning_audit, dict) else 0,
            "validated_hypothesis_count": int(pm_learning_audit.get("validated_hypothesis_count", 0) or 0) if isinstance(pm_learning_audit, dict) else 0,
            "policy_count": len(policies),
            "policy_types": sorted({str(row.get("policy_type") or "unknown") for row in policies})[:12],
            "alpha_setup_profile_count": setup_profile_trace.get("profile_count", 0),
            "alpha_setup_lifecycle_counts": setup_profile_trace.get("lifecycle_counts", {}),
            "alpha_setup_action_value_count": action_value_trace.get("action_value_count", 0),
            "alpha_setup_action_preference_counts": action_value_trace.get("action_preference_counts", {}),
            "money_decision_trace_required": True,
        },
        "pm_risk_gate_alignment": _contract_safe_pm_risk_gate_alignment(pm_risk_gate_payload),
        "trader_pre_execution_feasibility": {
            "margin_required": float(margin_required or 0.0),
            "margin_available": float(margin_available or 0.0),
            "margin_feasible": float(margin_required or 0.0) <= float(margin_available or 0.0),
            "market_confirmation_score": (
                market_confirmation.get("confirmation_score")
                if isinstance(market_confirmation, dict)
                else None
            ),
            "actual_trader_result_pending_phase2": True,
        },
        "consistency_flags": consistency_flags,
        "consistent_enough_for_phase1": not bool(consistency_flags),
        "not_product_rule": True,
        "no_future_data": True,
    }


def _analyst_signal_combo(analyst_signals: list) -> tuple[str, str, str]:
    return _fusion_analyst_signal_combo(analyst_signals)


def _normalize_required_analyst_name(name: str) -> str:
    return str(name or "").strip()


def _validate_required_analyst_signals(ticker: str, enabled_analysts: list, analyst_signals: list) -> None:
    expected = [_normalize_required_analyst_name(name) for name in enabled_analysts or []]
    if not expected:
        return
    seen: dict[str, int] = {}
    for signal in analyst_signals or []:
        analyst = _normalize_required_analyst_name(getattr(signal, "agent_name", ""))
        if analyst:
            seen[analyst] = seen.get(analyst, 0) + 1
    missing = [analyst for analyst in expected if seen.get(analyst, 0) < 1]
    duplicate = [analyst for analyst, count in seen.items() if analyst in expected and count > 1]
    extra = [analyst for analyst in seen if analyst not in expected]
    if missing or duplicate or extra:
        raise RuntimeError(
            f"{ticker} phase1 analyst signals incomplete in PM: "
            f"expected={expected}, seen={seen}, missing={missing}, "
            f"duplicate={duplicate}, extra={extra}"
        )


def _signal_side_text(value) -> str:
    text = _signal_to_text(value)
    if text == "Bullish":
        return "long"
    if text == "Bearish":
        return "short"
    return "flat"


def _market_regime_from_signals(analyst_signals: list, target_side: str) -> str:
    for signal in analyst_signals or []:
        if _signal_side_text(getattr(signal, "signal", None)) == target_side:
            regime = str(getattr(signal, "market_regime", "") or "unknown")
            if regime and regime != "unknown":
                return regime
    for signal in analyst_signals or []:
        regime = str(getattr(signal, "market_regime", "") or "unknown")
        if regime and regime != "unknown":
            return regime
    return "unknown"


def _current_canonical_setup_type_from_signals(
    target_side: str,
    analyst_signals: list,
) -> str:
    """Return the current executable setup carried by validated SCC evidence.

    Production ``analyst_signals`` are rebuilt from the validated SCC before
    entering PM.  Exact action-value retrieval must therefore use only the
    current technical/event execution source selected from that evidence.  A
    historical profile, a Step4 lifecycle state, or a synthesized signal
    combination is not a current canonical setup.
    """
    payloads = _execution_signal_payloads(analyst_signals, target_side)
    for conditional_path in (False, True):
        try:
            selected = _select_execution_evidence_payload(
                payloads,
                target_side=target_side,
                conditional_path=conditional_path,
            )
        except ValueError as exc:
            if str(exc) != "pm_execution_evidence_not_found":
                raise
            continue
        setup_type = str(selected.get("setup_type") or "").strip()
        if setup_type and setup_type.lower() not in {"unknown", "none", "null"}:
            return setup_type
    return ""


def _target_side_from_ratio(position_ratio: float) -> str:
    return _position_target_side_from_ratio(position_ratio)


def _same_sign(lhs: float, rhs: float) -> bool:
    return _position_same_sign(lhs, rhs)


def _is_new_or_increasing_exposure(target_ratio: float, current_ratio: float) -> bool:
    return _position_is_new_or_increasing_exposure(target_ratio, current_ratio)


def _provisional_target_lots_for_lifecycle_port(
    *,
    current_lots: int,
    current_ratio: float,
    target_ratio: float,
) -> int:
    """Infer lifecycle intent before final sizing.

    This value is only for PM step 2 action-port routing. It never becomes the
    final target_lots and never changes position sizing parameters.
    """
    current_lots = int(current_lots or 0)
    target_ratio = float(target_ratio or 0.0)
    current_ratio = float(current_ratio or 0.0)
    if abs(target_ratio) <= 1e-12:
        return 0 if current_lots else 0
    target_sign = 1 if target_ratio > 0 else -1
    if current_lots == 0:
        return target_sign
    if abs(current_ratio) > 1e-12 and not _same_sign(target_ratio, current_ratio):
        return target_sign
    if abs(current_ratio) > 1e-12 and _same_sign(target_ratio, current_ratio):
        if abs(target_ratio) > abs(current_ratio) + 1e-6:
            return current_lots + (1 if current_lots > 0 else -1)
        if abs(target_ratio) < abs(current_ratio) - 1e-6:
            if abs(current_lots) <= 1:
                return 0
            return current_lots - (1 if current_lots > 0 else -1)
    return current_lots


def _scale_signed_ratio(position_ratio: float, multiplier: float) -> float:
    return _position_scale_signed_ratio(position_ratio, multiplier)


def _strategy_memory_record(strategy_memory: dict, states: set[str]) -> dict:
    return _capital_strategy_memory_record(strategy_memory, states)


def _conflicting_weak_memory_record(strategy_memory: dict, signal_combo: tuple[str, str, str]) -> dict:
    return _capital_conflicting_weak_memory_record(strategy_memory, signal_combo)


def _normalize_trading_day_value(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _block_new_or_incremental_exposure(target_ratio: float, current_ratio: float) -> float:
    """Block only new/incremental risk while preserving existing same-side exposure."""
    if abs(current_ratio) > 1e-12 and _same_sign(target_ratio, current_ratio):
        return float(current_ratio)
    return 0.0


def _cap_new_or_incremental_exposure(
    *,
    target_ratio: float,
    current_ratio: float,
    max_abs_target_ratio: float,
) -> float:
    """Cap the target without turning a same-side add-on into a forced reduction."""
    max_abs_target_ratio = max(0.0, float(max_abs_target_ratio or 0.0))
    if abs(target_ratio) <= 1e-12:
        return 0.0
    target_sign = 1.0 if target_ratio > 0 else -1.0
    if abs(current_ratio) > 1e-12 and _same_sign(target_ratio, current_ratio):
        capped_abs = min(abs(target_ratio), max(abs(current_ratio), max_abs_target_ratio))
    else:
        capped_abs = min(abs(target_ratio), max_abs_target_ratio)
    return target_sign * capped_abs


def _cap_by_incremental_margin_budget(
    *,
    target_ratio: float,
    current_ratio: float,
    margin_rate: float,
    allowed_increment_margin_ratio: float,
) -> float:
    if margin_rate <= 0:
        return target_ratio
    allowed_increment_margin_ratio = max(0.0, float(allowed_increment_margin_ratio or 0.0))
    target_sign = 1.0 if target_ratio > 0 else -1.0
    if abs(current_ratio) > 1e-12 and _same_sign(target_ratio, current_ratio):
        current_margin = abs(current_ratio) * margin_rate
        max_target_margin = current_margin + allowed_increment_margin_ratio
    else:
        max_target_margin = allowed_increment_margin_ratio
    max_abs_target_ratio = max_target_margin / margin_rate
    return _cap_new_or_incremental_exposure(
        target_ratio=target_ratio,
        current_ratio=current_ratio,
        max_abs_target_ratio=max_abs_target_ratio,
    )


def _drawdown_hard_streak_state(
    *,
    db,
    config_id: str,
    trading_date,
    initial_capital: float,
    hard_drawdown: float,
) -> dict:
    trading_day_value = _normalize_trading_day_value(trading_date)
    if not db or not config_id or not trading_day_value:
        return {"consecutive_hard_days": 0, "latest_hard_date": None}
    conn = None
    try:
        conn = db._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT ds.trading_date, ds.current_balance, ds.current_margin
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE p.config_id = ?
              AND substr(ds.trading_date, 1, 10) < ?
            ORDER BY substr(ds.trading_date, 1, 10), ds.created_at
            ''',
            (config_id, trading_day_value),
        )
        peak_equity = float(initial_capital or 0.0)
        hard_streak = 0
        latest_hard_date = None
        for row in cursor.fetchall():
            equity = float(row["current_balance"] or 0.0) + float(row["current_margin"] or 0.0)
            peak_equity = max(peak_equity, equity)
            drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            if drawdown >= hard_drawdown:
                hard_streak += 1
                latest_hard_date = _normalize_trading_day_value(row["trading_date"])
            else:
                hard_streak = 0
                latest_hard_date = None
        return {
            "consecutive_hard_days": hard_streak,
            "latest_hard_date": latest_hard_date,
        }
    except Exception:
        pass
        return {
            "consecutive_hard_days": 0,
            "latest_hard_date": None,
            "error": "drawdown_state_query_failed",
        }
    finally:
        if conn:
            conn.close()


def _load_drawdown_control_from_recommendation(db, recommendation_row: dict) -> dict:
    try:
        loader = getattr(db, "_deserialize_external_json", None)
        if callable(loader):
            snapshot = loader(recommendation_row, "signal_snapshot")
        else:
            raw_snapshot = recommendation_row.get("signal_snapshot")
            snapshot = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) and raw_snapshot.strip() else {}
    except Exception:
        snapshot = {}
    if not isinstance(snapshot, dict):
        return {}
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    risk = contract.get("risk_controls") if isinstance(contract.get("risk_controls"), dict) else {}
    drawdown = risk.get("drawdown_control") if isinstance(risk.get("drawdown_control"), dict) else {}
    return drawdown if isinstance(drawdown, dict) else {}


def _load_opening_fac_context(
    *,
    db,
    config_id: str,
    ticker: str,
    trading_date,
    current_lots: int,
) -> dict:
    """Resolve the still-open strategy lot lineage to its opening FAC."""
    current_side = "long" if int(current_lots or 0) > 0 else "short" if int(current_lots or 0) < 0 else ""
    decision_day = _normalize_trading_day_value(trading_date)
    if not db or not config_id or not ticker or not current_side or not decision_day:
        if int(current_lots or 0) != 0:
            raise RuntimeError("pm_opening_fac_context_inputs_missing")
        return {}
    conn = None
    try:
        conn = db._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT ft.id,
                   ft.trading_date,
                   ft.action,
                   ft.lots,
                   ft.source_type,
                   ft.recommendation_id,
                   COALESCE(ft.execution_price, ft.price) AS execution_price,
                   fr.signal_snapshot,
                   fr.signal_snapshot_artifact_path,
                   fr.signal_snapshot_sha256
            FROM futures_transactions ft
            LEFT JOIN futures_recommendation fr ON fr.id = ft.recommendation_id
            WHERE ft.config_id = ?
              AND upper(ft.ticker) = upper(?)
              AND substr(ft.trading_date, 1, 10) < ?
            ORDER BY substr(ft.trading_date, 1, 10),
                     ft.created_at,
                     CASE
                         WHEN lower(COALESCE(ft.source_type, '')) = 'rollover'
                              AND lower(ft.action) IN ('close_long', 'close_short') THEN 0
                         WHEN lower(COALESCE(ft.source_type, '')) = 'rollover'
                              AND lower(ft.action) IN ('open_long', 'open_short') THEN 1
                         ELSE 0
                     END,
                     ft.id
            ''',
            (config_id, ticker, decision_day),
        )
        active: dict[str, list[dict]] = {"long": [], "short": []}
        rollover_transfers: dict[tuple[str, str], list[dict]] = {}

        def consume_active(side: str, lots: int) -> list[dict]:
            consumed_segments: list[dict] = []
            remaining = lots
            while remaining > 0 and active[side]:
                first = active[side][0]
                consumed = min(remaining, int(first.get("remaining_lots") or 0))
                consumed_segments.append({
                    "remaining_lots": consumed,
                    "row": dict(first.get("row") or {}),
                })
                first["remaining_lots"] = int(first.get("remaining_lots") or 0) - consumed
                remaining -= consumed
                if int(first.get("remaining_lots") or 0) <= 0:
                    active[side].pop(0)
            if remaining > 0:
                raise RuntimeError("pm_opening_fac_lineage_missing")
            return consumed_segments

        for raw_row in cursor.fetchall():
            row = dict(raw_row)
            source_type = str(row.get("source_type") or "strategy").strip().lower()
            action = str(row.get("action") or "").strip().lower()
            lots = max(0, abs(_safe_int(row.get("lots"), 0)))
            if lots <= 0:
                continue
            if action in {"open_long", "open_short"}:
                side = "long" if action == "open_long" else "short"
                if source_type == "rollover":
                    recommendation_id = str(row.get("recommendation_id") or "").strip()
                    if not recommendation_id:
                        raise RuntimeError("pm_rollover_recommendation_missing")
                    transfer_key = (recommendation_id, side)
                    transfer_queue = rollover_transfers.get(transfer_key) or []
                    origin_row = dict((transfer_queue[0].get("row") if transfer_queue else {}) or {})
                    if not origin_row:
                        raise RuntimeError("pm_rollover_open_lineage_missing")
                    remaining = lots
                    while remaining > 0 and transfer_queue:
                        first = transfer_queue[0]
                        transferred = min(remaining, int(first.get("remaining_lots") or 0))
                        active[side].append({
                            "remaining_lots": transferred,
                            "row": dict(first.get("row") or {}),
                        })
                        first["remaining_lots"] = int(first.get("remaining_lots") or 0) - transferred
                        remaining -= transferred
                        if int(first.get("remaining_lots") or 0) <= 0:
                            transfer_queue.pop(0)
                    if remaining > 0:
                        active[side].append({
                            "remaining_lots": remaining,
                            "row": origin_row,
                        })
                    continue
                if source_type != "strategy":
                    continue
                active[side].append({"remaining_lots": lots, "row": row})
                continue
            if action not in {"close_long", "close_short"}:
                continue
            side = "long" if action == "close_long" else "short"
            consumed_segments = consume_active(side, lots)
            if source_type == "rollover":
                recommendation_id = str(row.get("recommendation_id") or "").strip()
                if not recommendation_id:
                    raise RuntimeError("pm_rollover_recommendation_missing")
                rollover_transfers.setdefault((recommendation_id, side), []).extend(consumed_segments)
        candidates = [item for item in active[current_side] if int(item.get("remaining_lots") or 0) > 0]
        if not candidates:
            raise RuntimeError("pm_opening_fac_lineage_missing")
        opening_row = dict(candidates[0].get("row") or {})
        if not str(opening_row.get("recommendation_id") or "").strip():
            raise RuntimeError("pm_opening_fac_recommendation_missing")
        loader = getattr(db, "_deserialize_external_json", None)
        if callable(loader):
            snapshot = loader(opening_row, "signal_snapshot")
        else:
            raw_snapshot = opening_row.get("signal_snapshot")
            snapshot = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) and raw_snapshot.strip() else {}
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        if not contract:
            raise RuntimeError("pm_opening_fac_contract_missing")
        opening_execution_price = _safe_float(opening_row.get("execution_price"), 0.0)
        if opening_execution_price <= 0.0:
            raise RuntimeError("pm_opening_fac_execution_price_missing")
        opening_day = _normalize_trading_day_value(opening_row.get("trading_date"))
        held_trading_days = 0
        best_prior_settlement_price = 0.0
        if opening_day and opening_day < decision_day:
            cursor.execute(
                '''
                SELECT COUNT(DISTINCT substr(ds.trading_date, 1, 10)) AS day_count
                FROM daily_settlement ds
                JOIN portfolio p ON ds.portfolio_id = p.id
                WHERE p.config_id = ?
                  AND substr(ds.trading_date, 1, 10) > ?
                  AND substr(ds.trading_date, 1, 10) < ?
                ''',
                (config_id, opening_day, decision_day),
            )
            day_row = cursor.fetchone()
            held_trading_days = 1 + int((day_row["day_count"] if day_row else 0) or 0)
            cursor.execute(
                '''
                SELECT MAX(tdp.settle_price) AS highest_settlement,
                       MIN(tdp.settle_price) AS lowest_settlement
                FROM ticker_daily_pnl tdp
                JOIN portfolio p ON tdp.portfolio_id = p.id
                WHERE p.config_id = ?
                  AND upper(tdp.ticker) = upper(?)
                  AND substr(tdp.trading_date, 1, 10) >= ?
                  AND substr(tdp.trading_date, 1, 10) < ?
                  AND tdp.settle_price > 0
                ''',
                (config_id, ticker, opening_day, decision_day),
            )
            settlement_row = cursor.fetchone()
            if settlement_row:
                settlement_value = (
                    settlement_row["highest_settlement"]
                    if current_side == "long"
                    else settlement_row["lowest_settlement"]
                )
                best_prior_settlement_price = _safe_float(settlement_value, 0.0)
        return {
            "recommendation_id": str(opening_row.get("recommendation_id") or ""),
            "opening_trading_date": opening_day,
            "held_trading_days": held_trading_days,
            "expected_horizon_days": _safe_int(contract.get("expected_horizon_days"), 0),
            "position_invalidation_level": _safe_float(
                contract.get("position_invalidation_level"),
                0.0,
            ),
            "exit_hint": str(contract.get("exit_hint") or ""),
            "atr_stop_distance": _safe_float(contract.get("atr_stop_distance"), 0.0),
            "opening_execution_price": opening_execution_price,
            "best_prior_settlement_price": best_prior_settlement_price,
            "setup_type": str(contract.get("setup_type") or ""),
            "final_action": str(contract.get("final_action") or ""),
        }
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("pm_opening_fac_context_failed") from exc
    finally:
        if conn:
            conn.close()


def _opening_fac_position_invalidation_breached(
    *,
    opening_fac_context: dict | None,
    ticker: str,
    current_side: str,
    current_price: float,
    current_position,
    full_config: dict | None,
) -> bool:
    context = opening_fac_context if isinstance(opening_fac_context, dict) else {}
    level = _safe_float(context.get("position_invalidation_level"), 0.0)
    price = _safe_float(current_price, 0.0)
    if price <= 0.0:
        return False
    atr_distance = _safe_float(context.get("atr_stop_distance"), 0.0)
    entry_price = _safe_float(context.get("opening_execution_price"), 0.0)
    structure_breached = False
    if level > 0.0 and entry_price > 0.0:
        if current_side == "long" and level < entry_price:
            structure_breached = price <= level
        elif current_side == "short" and level > entry_price:
            structure_breached = price >= level

    atr_breached = False
    if atr_distance > 0.0 and entry_price > 0.0:
        policy = resolve_exit_policy_config(
            full_config or {},
            ticker,
            str(context.get("setup_type") or ""),
        )
        signed_lots = 1 if current_side == "long" else -1 if current_side == "short" else 0
        atr_protection = resolve_atr_protection(
            current_lots=signed_lots,
            current_price=price,
            entry_price=entry_price,
            atr_distance=atr_distance,
            atr_multiplier=_safe_float(policy.get("atr_multiplier"), 1.8),
            best_prior_settlement_price=_safe_float(
                context.get("best_prior_settlement_price"),
                0.0,
            ),
        )
        atr_breached = bool(atr_protection.get("breached"))
    return bool(structure_breached or atr_breached)


def _drawdown_recovery_probe_history(
    *,
    db,
    config_id: str,
    trading_date,
    control: dict,
) -> dict:
    trading_day_value = _normalize_trading_day_value(trading_date)
    if not db or not config_id or not trading_day_value:
        return {"probe_days": 0, "loss_count": 0, "consecutive_profit_days": 0, "cooldown_active": False}
    conn = None
    try:
        conn = db._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT tdp.trading_date, tdp.ticker, tdp.daily_pnl
            FROM ticker_daily_pnl tdp
            JOIN portfolio p ON tdp.portfolio_id = p.id
            WHERE p.config_id = ?
              AND substr(tdp.trading_date, 1, 10) < ?
            ''',
            (config_id, trading_day_value),
        )
        ticker_daily_pnl = {
            (_normalize_trading_day_value(row["trading_date"]), str(row["ticker"] or "").upper()):
            float(row["daily_pnl"] or 0.0)
            for row in cursor.fetchall()
        }
        cursor.execute(
            '''
            SELECT ft.trading_date,
                   ft.ticker,
                   ft.action,
                   ft.recommendation_id,
                   fr.signal_snapshot,
                   fr.signal_snapshot_artifact_path,
                   fr.signal_snapshot_sha256
            FROM futures_transactions ft
            JOIN futures_recommendation fr ON fr.id = ft.recommendation_id
            WHERE ft.config_id = ?
              AND substr(ft.trading_date, 1, 10) < ?
              AND ft.action IN ('open_long', 'open_short')
            ORDER BY substr(ft.trading_date, 1, 10), ft.created_at, ft.id
            ''',
            (config_id, trading_day_value),
        )
        probe_by_day: dict[tuple[str, str], float] = {}
        for row in cursor.fetchall():
            record = dict(row)
            drawdown_diag = _load_drawdown_control_from_recommendation(db, record)
            if str(drawdown_diag.get("mode") or "") != "hard_recovery_probe":
                continue
            day = _normalize_trading_day_value(record.get("trading_date"))
            ticker = str(record.get("ticker") or "").upper()
            cumulative_pnl = sum(
                pnl
                for (pnl_day, pnl_ticker), pnl in ticker_daily_pnl.items()
                if pnl_ticker == ticker and day <= pnl_day < trading_day_value
            )
            probe_by_day[(day, ticker)] = cumulative_pnl

        ordered_probe_days = sorted(
            [
                {"trading_date": day, "ticker": ticker, "probe_cumulative_pnl": pnl}
                for (day, ticker), pnl in probe_by_day.items()
            ],
            key=lambda item: (item["trading_date"], item["ticker"]),
        )
        loss_days = [item for item in ordered_probe_days if float(item.get("probe_cumulative_pnl") or 0.0) < 0]
        last_loss_date = loss_days[-1]["trading_date"] if loss_days else None
        consecutive_profit_days = 0
        for item in reversed(ordered_probe_days):
            if last_loss_date and item["trading_date"] <= last_loss_date:
                break
            if float(item.get("probe_cumulative_pnl") or 0.0) > 0:
                consecutive_profit_days += 1
            elif float(item.get("probe_cumulative_pnl") or 0.0) < 0:
                break

        loss_count = len(loss_days)
        first_loss_cooldown = int(control.get("first_probe_loss_cooldown_days", 2))
        second_loss_cooldown = int(control.get("second_probe_loss_cooldown_days", 3))
        increment = int(control.get("cooldown_increment_days", 1))
        cooldown_days = 0
        days_elapsed = 0
        cooldown_active = False
        if last_loss_date:
            if loss_count <= 1:
                cooldown_days = first_loss_cooldown
            elif loss_count == 2:
                cooldown_days = second_loss_cooldown
            else:
                cooldown_days = second_loss_cooldown + (loss_count - 2) * increment
            days_elapsed = _trading_days_between_settlements(
                db=db,
                config_id=config_id,
                after_date=last_loss_date,
                before_date=trading_day_value,
            )
            cooldown_active = days_elapsed < cooldown_days

        return {
            "probe_days": len(ordered_probe_days),
            "loss_count": loss_count,
            "last_loss_date": last_loss_date,
            "cooldown_days": cooldown_days,
            "cooldown_days_elapsed": days_elapsed,
            "cooldown_active": cooldown_active,
            "observation_only": cooldown_active and loss_count >= 2,
            "consecutive_profit_days": consecutive_profit_days,
            "recent_probe_days": ordered_probe_days[-5:],
        }
    except Exception:
        pass
        return {
            "probe_days": 0,
            "loss_count": 0,
            "consecutive_profit_days": 0,
            "cooldown_active": False,
            "error": "drawdown_probe_history_query_failed",
        }
    finally:
        if conn:
            conn.close()


def _recovery_probe_budget(control: dict, recovery_history: dict) -> float:
    steps = control.get("recovery_restore_step_margin_ratios") or []
    parsed_steps = []
    for item in steps:
        value = _safe_float(item, 0.0)
        if value > 0:
            parsed_steps.append(value)
    if not parsed_steps:
        parsed_steps = [float(control.get("recovery_probe_margin_ratio_max", 0.02))]
    index = min(int(recovery_history.get("consecutive_profit_days", 0) or 0), len(parsed_steps) - 1)
    return max(0.0, parsed_steps[index])


def _apply_trade_plan_multiplier(
    *,
    target_ratio: float,
    current_ratio: float,
    multiplier: float,
) -> float:
    """Scale new risk without forcing an existing same-side position to shrink."""
    return _position_apply_trade_plan_multiplier(
        target_ratio=target_ratio,
        current_ratio=current_ratio,
        multiplier=multiplier,
    )


def _apply_market_confirmation_control(
    *,
    position_ratio: float,
    current_ratio: float,
    signal_strength: float,
    market_confirmation: dict,
    full_config: dict,
) -> tuple[float, list[str], list[str]]:
    reasons: list[str] = []
    notes: list[str] = []
    if not _is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, reasons, notes

    target_side = _target_side_from_ratio(position_ratio)
    if target_side not in {"long", "short"}:
        return position_ratio, reasons, notes

    control = full_config.get("market_confirmation", {}) or {}
    if not control.get("enabled", True) or not market_confirmation.get("enabled"):
        return position_ratio, reasons, notes

    conflicts = market_confirmation.get("conflicts") or []
    confirmations = market_confirmation.get("confirmations") or []
    features = market_confirmation.get("features") or []
    confirmation_score = float(market_confirmation.get("confirmation_score", 0.0) or 0.0)

    if bool(control.get("quality_gate_enabled", False)):
        if features:
            gate_failures = []
            min_score = float(control.get("min_confirmation_score_for_new_entry", 0.45))
            if confirmation_score < min_score:
                gate_failures.append(f"score={confirmation_score:.2f}<{min_score:.2f}")

            max_conflicts = control.get("max_conflicts_for_new_entry")
            if max_conflicts is not None and len(conflicts) > int(max_conflicts):
                gate_failures.append(f"conflicts={len(conflicts)}>{int(max_conflicts)}")

            if gate_failures:
                original_ratio = position_ratio
                weak_signal_strength = float(
                    control.get("quality_gate_weak_signal_strength", control.get("weak_signal_strength", 0.25))
                )
                if bool(control.get("quality_gate_block_weak_signal", True)) and signal_strength <= weak_signal_strength:
                    position_ratio = 0.0
                    reasons.append("market_confirmation_quality_gate")
                    notes.append(
                        f"blocked weak {target_side} signal by SCC evidence quality gate: {', '.join(gate_failures)}"
                    )
                    return position_ratio, reasons, notes

                position_ratio = _scale_signed_ratio(
                    position_ratio,
                    float(control.get("quality_gate_cap_multiplier", 0.50)),
                )
                reasons.append("market_confirmation_quality_gate")
                notes.append(
                    f"scaled {target_side} ratio {original_ratio:.2%}->{position_ratio:.2%} "
                    f"by SCC evidence quality gate: {', '.join(gate_failures)}"
                )
        else:
            notes.append("SCC evidence quality gate skipped: no usable confirmation evidence")

    if conflicts:
        original_ratio = position_ratio
        weak_signal_strength = float(control.get("weak_signal_strength", 0.25))
        allow_conflicted_probe = bool(control.get("allow_conflicted_probe_with_strong_confirmation", False))
        conflicted_probe_score = float(control.get("conflicted_probe_min_confirmation_score", 0.65))
        conflicted_probe_confirmations = int(control.get("conflicted_probe_min_confirmations", 3))
        strong_conflicted_probe = (
            allow_conflicted_probe
            and confirmation_score >= conflicted_probe_score
            and len(confirmations) >= conflicted_probe_confirmations
        )
        if (
            bool(control.get("block_weak_conflicting_signal", True))
            and signal_strength <= weak_signal_strength
            and not strong_conflicted_probe
        ):
            position_ratio = 0.0
            reasons.append("market_confirmation_conflict")
            notes.append(
                f"blocked weak {target_side} signal due to SCC evidence conflicts={conflicts}"
            )
        else:
            position_ratio = _scale_signed_ratio(
                position_ratio,
                float(control.get("conflict_cap_multiplier", 0.50)),
            )
            reasons.append("market_confirmation_conflict")
            if strong_conflicted_probe and signal_strength <= weak_signal_strength:
                notes.append(
                    f"scaled conflicted {target_side} ratio {original_ratio:.2%}->{position_ratio:.2%} "
                    f"despite weak signal because confirmation_score={confirmation_score:.2f}>={conflicted_probe_score:.2f} "
                    f"and confirmations={len(confirmations)}>={conflicted_probe_confirmations}; conflicts={conflicts}"
                )
            else:
                notes.append(
                    f"scaled {target_side} ratio {original_ratio:.2%}->{position_ratio:.2%} "
                    f"due to SCC evidence conflicts={conflicts}"
                )
    elif confirmations:
        notes.append(f"SCC evidence supports {target_side}: {confirmations}")

    return position_ratio, reasons, notes


def _apply_market_data_gap_control(
    *,
    ticker: str,
    position_ratio: float,
    current_ratio: float,
    signal_strength: float,
    market_confirmation: dict,
    full_config: dict,
) -> tuple[float, list[str], list[str], dict]:
    """Degrade new risk when formal SCC evidence is incomplete, without stopping the day."""
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    if not _is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, reasons, notes, diagnostics

    target_side = _target_side_from_ratio(position_ratio)
    if target_side not in {"long", "short"}:
        return position_ratio, reasons, notes, diagnostics

    control = full_config.get("market_confirmation", {}) or {}
    if not control.get("enabled", True) or not control.get("missing_data_degradation_enabled", True):
        return position_ratio, reasons, notes, diagnostics
    if not market_confirmation.get("enabled"):
        return position_ratio, reasons, notes, diagnostics

    actionable_missing = [str(item) for item in (market_confirmation.get("data_missing") or [])]
    parameter_errors = [str(item) for item in (market_confirmation.get("parameter_errors") or [])]
    provider_errors = [str(item) for item in (market_confirmation.get("errors") or [])]
    unavailable = [str(item) for item in (market_confirmation.get("data_unavailable") or [])]
    fallback_covered = {str(item) for item in (market_confirmation.get("fallback_covered_missing") or [])}
    actionable_missing = [item for item in actionable_missing if item not in fallback_covered]
    gap_count = len(actionable_missing) + len(parameter_errors) + len(provider_errors)
    features = market_confirmation.get("features") or []
    if gap_count <= 0:
        diagnostics["market_data_gap_control"] = {
            "enabled": True,
            "decision": "no_actionable_gap",
            "fallback_covered_missing": sorted(fallback_covered),
        }
        return position_ratio, reasons, notes, diagnostics

    min_features = int(control.get("min_usable_features_after_data_gap", 1) or 1)
    max_missing = int(control.get("max_actionable_missing_for_normal_entry", 2) or 2)
    cap_multiplier = float(control.get("data_gap_cap_multiplier", 0.50) or 0.50)
    block_without_features = bool(control.get("data_gap_block_when_no_features", True))
    weak_signal_strength = float(control.get("weak_signal_strength", 0.25) or 0.25)
    original_ratio = position_ratio
    decision = "allow_with_gap"

    if len(features) < min_features and block_without_features:
        position_ratio = 0.0
        decision = "block_no_usable_features"
        reasons.append("market_confirmation_data_gap")
        notes.append(
            f"{ticker} {target_side} blocked: SCC evidence gap leaves "
            f"{len(features)} usable features; missing={actionable_missing}, errors={parameter_errors or provider_errors}"
        )
    elif gap_count > max_missing or parameter_errors or provider_errors:
        if signal_strength <= weak_signal_strength and bool(control.get("data_gap_block_weak_signal", False)):
            position_ratio = 0.0
            decision = "block_weak_data_gap"
        else:
            position_ratio = _scale_signed_ratio(position_ratio, cap_multiplier)
            decision = "scale_data_gap"
        reasons.append("market_confirmation_data_gap")
        notes.append(
            f"{ticker} {target_side} degraded by SCC evidence gap: "
            f"{original_ratio:.2%}->{position_ratio:.2%}, "
            f"missing={actionable_missing}, parameter_errors={parameter_errors}, provider_errors={provider_errors}"
        )

    diagnostics["market_data_gap_control"] = {
        "enabled": True,
        "decision": decision,
        "actionable_missing": actionable_missing,
        "data_unavailable": unavailable,
        "parameter_errors": parameter_errors,
        "provider_errors": provider_errors,
        "fallback_covered_missing": sorted(fallback_covered),
        "usable_feature_count": len(features),
        "original_ratio": float(original_ratio),
        "final_ratio": float(position_ratio),
    }
    return position_ratio, reasons, notes, diagnostics


def _policy_row_applies(row: dict, *, ticker: str, side: str, horizon: str, regime: str, template: str = "*") -> bool:
    if not isinstance(row, dict):
        return False
    checks = {
        "ticker": ticker,
        "side": side,
        "horizon_class": horizon,
        "market_regime": regime,
    }
    for key, value in checks.items():
        row_value = str(row.get(key) or "*")
        if row_value not in {"*", "", "unknown", str(value)}:
            return False
    row_template = str(row.get("setup_type") or "*")
    if row_template not in {"*", "", "unknown", template} and template != "*":
        return False
    return True


def _apply_adaptive_policy_position_control(
    *,
    ticker: str,
    position_ratio: float,
    current_ratio: float,
    analyst_signals: list,
    market_confirmation: dict,
    full_config: dict,
    adaptive_policy_state: list | None,
) -> tuple[float, list[str], list[str], dict]:
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    if not _is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, reasons, notes, diagnostics
    target_side = _target_side_from_ratio(position_ratio)
    if target_side not in {"long", "short"}:
        return position_ratio, reasons, notes, diagnostics
    horizon = _resolve_decision_horizon(analyst_signals, 1 if target_side == "long" else -1)
    regime = _market_regime_from_signals(analyst_signals, target_side)
    template = _current_canonical_setup_type_from_signals(target_side, analyst_signals)
    confirmation_score = _safe_float((market_confirmation or {}).get("confirmation_score"), 0.0)
    matching: list[dict] = []
    for row in adaptive_policy_state or []:
        if not _policy_row_applies(row, ticker=ticker, side=target_side, horizon=horizon, regime=regime, template=template):
            continue
        policy_type = str(row.get("policy_type") or "")
        action = str(row.get("policy_action") or "").lower()
        if policy_type in {"loss_template_policy", "fast_loss_sentinel", "tail_loss_sentinel", "template_quality"} and action == "cap":
            matching.append(row)
        elif policy_type.startswith("learning_mechanism:") and action == "cap":
            matching.append(row)
    diagnostics["adaptive_policy_position_control"] = {
        "enabled": True,
        "target_side": target_side,
        "horizon_class": horizon,
        "market_regime": regime,
        "setup_type": template,
        "matching_cap_count": len(matching),
        "confirmation_score": confirmation_score,
        "pre_control_ratio": float(position_ratio),
    }
    if not matching:
        diagnostics["adaptive_policy_position_control"]["decision"] = "no_cap_policy"
        return position_ratio, reasons, notes, diagnostics
    strongest = min(matching, key=lambda row: _safe_float(row.get("multiplier"), 1.0))
    multiplier = max(0.0, min(1.0, _safe_float(strongest.get("multiplier"), 0.50)))
    before = position_ratio
    position_ratio = _scale_signed_ratio(position_ratio, multiplier)
    reasons.append(str(strongest.get("policy_type") or "adaptive_policy_cap"))
    notes.append(
        f"{ticker} {target_side} same-scope adaptive cap applied: "
        f"{before:.2%}->{position_ratio:.2%}; policy={strongest.get('policy_type')}, "
        f"sample_count={strongest.get('sample_count')}"
    )
    diagnostics["adaptive_policy_position_control"].update({
        "decision": "cap_applied",
        "final_ratio": float(position_ratio),
        "applied_policy": _compact_policy_row(strongest),
        "not_product_blacklist": True,
        "requires_current_scope_match": True,
    })
    return position_ratio, reasons, notes, diagnostics


def _apply_trade_frequency_control(
    *,
    db,
    config_id: str,
    ticker: str,
    trading_date,
    position_ratio: float,
    current_ratio: float,
    signal_combo: tuple[str, str, str],
    market_confirmation: dict,
    full_config: dict,
) -> tuple[float, list[str], list[str], dict]:
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    control = full_config.get("trade_frequency_control", {}) or {}
    if not control.get("enabled", False):
        return position_ratio, reasons, notes, diagnostics

    target_side = _target_side_from_ratio(position_ratio)
    if target_side not in {"long", "short"}:
        return position_ratio, reasons, notes, diagnostics
    if not _is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, reasons, notes, diagnostics

    original_ratio = position_ratio
    side_override = ((control.get("side_overrides") or {}).get(ticker) or {})
    override_key = f"{target_side}_cap_multiplier"
    if override_key in side_override:
        multiplier = float(side_override.get(override_key, 1.0))
        position_ratio = _scale_signed_ratio(position_ratio, multiplier)
        reasons.append("trade_frequency_control")
        notes.append(
            f"{ticker} {target_side} static attribution cap: {original_ratio:.2%}->{position_ratio:.2%}"
        )

    side_perf = {}
    if db and config_id:
        side_perf = db.get_futures_trade_pair_performance(
            config_id=config_id,
            ticker=ticker,
            side=target_side,
            trading_date=trading_date,
            lookback_trades=int(control.get("lookback_trades", 20)),
        )
    diagnostics["side_performance"] = side_perf

    if int(side_perf.get("total_trades", 0) or 0) >= int(control.get("min_completed_trades", 8)):
        win_rate = float(side_perf.get("win_rate", 0.0) or 0.0)
        total_pnl = float(side_perf.get("total_pnl", 0.0) or 0.0)
        severe_pnl_threshold = control.get("severe_total_pnl_below")
        weak_pnl_threshold = control.get("weak_total_pnl_below")
        if severe_pnl_threshold is not None and total_pnl <= float(severe_pnl_threshold):
            reasons.append("side_performance_block")
            if bool(control.get("severe_block_new_entries", False)):
                notes.append(
                    f"{ticker} {target_side} blocked: recent completed-trade PnL={total_pnl:.0f}"
                )
                position_ratio = 0.0
            else:
                before = position_ratio
                position_ratio = _scale_signed_ratio(position_ratio, float(control.get("weak_cap_multiplier", 0.50)))
                reasons.append("side_performance_probe_cap")
                notes.append(
                    f"{ticker} {target_side} capped by severe recent completed-trade PnL={total_pnl:.0f}: "
                    f"{before:.2%}->{position_ratio:.2%}"
                )
        elif win_rate <= float(control.get("severe_win_rate_below", 0.30)):
            reasons.append("side_performance_block")
            if bool(control.get("severe_block_new_entries", False)):
                notes.append(
                    f"{ticker} {target_side} blocked: recent completed-trade win_rate={win_rate:.2%}"
                )
                position_ratio = 0.0
            else:
                before = position_ratio
                position_ratio = _scale_signed_ratio(position_ratio, float(control.get("weak_cap_multiplier", 0.50)))
                reasons.append("side_performance_probe_cap")
                notes.append(
                    f"{ticker} {target_side} capped by severe recent completed-trade win_rate={win_rate:.2%}: "
                    f"{before:.2%}->{position_ratio:.2%}"
                )
        elif weak_pnl_threshold is not None and total_pnl <= float(weak_pnl_threshold):
            before = position_ratio
            position_ratio = _scale_signed_ratio(position_ratio, float(control.get("weak_cap_multiplier", 0.50)))
            reasons.append("trade_frequency_control")
            notes.append(
                f"{ticker} {target_side} scaled by recent PnL={total_pnl:.0f}: "
                f"{before:.2%}->{position_ratio:.2%}"
            )
        elif win_rate <= float(control.get("weak_win_rate_below", 0.38)):
            before = position_ratio
            position_ratio = _scale_signed_ratio(position_ratio, float(control.get("weak_cap_multiplier", 0.50)))
            reasons.append("trade_frequency_control")
            notes.append(
                f"{ticker} {target_side} scaled by recent win_rate={win_rate:.2%}: "
                f"{before:.2%}->{position_ratio:.2%}"
            )
        if bool(control.get("churn_control_enabled", True)):
            commission = _safe_float(
                side_perf.get("total_commission", side_perf.get("commission")),
                0.0,
            )
            trade_count = int(side_perf.get("total_trades", 0) or 0)
            churn_loss_threshold = float(control.get("churn_total_pnl_below", -1000) or -1000)
            min_churn_trades = int(control.get("churn_min_completed_trades", 6) or 6)
            if trade_count >= min_churn_trades and total_pnl <= churn_loss_threshold:
                before = position_ratio
                position_ratio = _scale_signed_ratio(
                    position_ratio,
                    float(control.get("churn_cap_multiplier", control.get("weak_cap_multiplier", 0.50)) or 0.50),
                )
                reasons.append("trade_churn_cost_control")
                notes.append(
                    f"{ticker} {target_side} scaled by churn/cost profile: "
                    f"trades={trade_count}, pnl={total_pnl:.0f}, commission={commission:.0f}, "
                    f"{before:.2%}->{position_ratio:.2%}"
                )
                diagnostics["churn_cost_control"] = {
                    "enabled": True,
                    "decision": "cap_applied",
                    "trade_count": trade_count,
                    "total_pnl": total_pnl,
                    "commission": commission,
                    "not_product_blacklist": True,
                }

    weak_combos = [tuple(item) for item in (control.get("weak_signal_combos") or [])]
    if signal_combo in weak_combos and target_side in {"long", "short"}:
        if not market_confirmation.get("features"):
            diagnostics["weak_signal_combo_skip"] = "no_market_confirmation_features"
            return position_ratio, reasons, notes, diagnostics
        confirmation_control = full_config.get("market_confirmation", {}) or {}
        min_confirmations = int(confirmation_control.get("min_confirmations_for_new_entry", 2))
        min_score = float(confirmation_control.get("min_confirmation_score_for_weak_combo", 0.0) or 0.0)
        confirmations = market_confirmation.get("confirmations") or []
        confirmation_score = float(market_confirmation.get("confirmation_score", 0.0) or 0.0)
        if len(confirmations) < min_confirmations or confirmation_score < min_score:
            before = position_ratio
            if abs(current_ratio) <= 1e-12:
                weak_probe_cap = max(
                    0.0,
                    float(control.get("weak_combo_probe_max_ratio", control.get("weak_probe_max_ratio", 0.01)) or 0.01),
                )
                weak_probe_floor = max(
                    0.0,
                    float(control.get("weak_combo_probe_floor_ratio", control.get("weak_probe_floor_ratio", 0.005)) or 0.005),
                )
                position_ratio = _probe_ratio_from_soft_gate(
                    side=target_side,
                    current_ratio=current_ratio,
                    raw_ratio=position_ratio,
                    cap_ratio=weak_probe_cap,
                    floor_ratio=weak_probe_floor,
                )
                reasons.append("weak_signal_combo_probe_cap")
            else:
                position_ratio = _scale_signed_ratio(position_ratio, float(control.get("weak_cap_multiplier", 0.50)))
            reasons.append("weak_signal_combo")
            notes.append(
                f"weak analyst combo {signal_combo} requires {min_confirmations} confirmations "
                f"and score>={min_score:.2f}; got confirmations={len(confirmations)}, "
                f"score={confirmation_score:.2f}, ratio {before:.2%}->{position_ratio:.2%}"
            )

    return position_ratio, reasons, notes, diagnostics


def _apply_opportunity_quality_position_control(
    *,
    ticker: str,
    position_ratio: float,
    current_ratio: float,
    opportunity_scorecard: dict | None,
    full_config: dict,
) -> tuple[float, list[str], list[str], dict]:
    """Bind target size to setup quality without changing the 20% hard cap."""
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    if not _is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, reasons, notes, diagnostics
    target_side = _target_side_from_ratio(position_ratio)
    if target_side not in {"long", "short"}:
        return position_ratio, reasons, notes, diagnostics
    pm_config = _get_portfolio_manager_config(full_config)
    quality_cfg = (pm_config.get("quality_aware_fusion") or {}).get("position_quality_sizing") or {}
    if not bool(quality_cfg.get("enabled", True)):
        return position_ratio, reasons, notes, diagnostics
    scorecard = opportunity_scorecard if isinstance(opportunity_scorecard, dict) else {}
    side_scorecard = scorecard.get(target_side) if isinstance(scorecard.get(target_side), dict) else {}
    layer = str(side_scorecard.get("final_state") or "unknown").lower()
    score = _safe_float(side_scorecard.get("score"), 0.0)
    setup_quality = _safe_float(side_scorecard.get("max_setup_quality"), 0.0)
    gating_failures = [str(item) for item in (side_scorecard.get("gating_failures") or [])]
    state_multipliers = quality_cfg.get("state_multipliers") or {
        "tradeable_candidate": 1.10,
        "probe_candidate": 0.90,
        "watch_for_trigger": 0.30,
        "no_opportunity": 0.0,
        "unknown": 0.45,
    }
    multiplier = float(state_multipliers.get(layer, state_multipliers.get("unknown", 0.45)) or 0.45)
    weak_setup_threshold = float(quality_cfg.get("weak_setup_quality_below", 0.42) or 0.42)
    if setup_quality and setup_quality < weak_setup_threshold:
        multiplier = min(multiplier, float(quality_cfg.get("weak_setup_multiplier", 0.35) or 0.35))
        gating_failures = sorted(set(gating_failures + ["weak_setup_quality_position_cap"]))
    if layer == "no_opportunity":
        multiplier = 0.0
    before = position_ratio
    position_ratio = _scale_signed_ratio(position_ratio, multiplier)
    diagnostics["opportunity_quality_position_sizing"] = {
        "enabled": True,
        "target_side": target_side,
        "scorecard_state": layer,
        "score": score,
        "setup_quality": setup_quality,
        "multiplier": multiplier,
        "gating_failures": gating_failures,
        "pre_control_ratio": float(before),
        "final_ratio": float(position_ratio),
        "not_product_rule": True,
    }
    if abs(position_ratio - before) > 1e-12:
        reasons.append("opportunity_quality_position_sizing")
        notes.append(
            f"{ticker} {target_side} sized by opportunity/setup quality: "
            f"state={layer}, score={score:.2f}, setup_quality={setup_quality:.2f}, "
            f"{before:.2%}->{position_ratio:.2%}"
        )
    return position_ratio, reasons, notes, diagnostics


def _apply_alpha_setup_ev_position_control(
    *,
    ticker: str,
    position_ratio: float,
    current_ratio: float,
    opportunity_scorecard: dict | None,
    alpha_setup_profiles: list | None,
    alpha_setup_action_values: list | None,
    analyst_signals: list,
    market_confirmation: dict,
    full_config: dict,
    max_position_ratio: float,
) -> tuple[float, list[str], list[str], dict]:
    """Translate setup expectancy into PM sizing without hard product rules.

    This is the money-facing link: same-scope action values and profiles can
    scale positive expectancy, keep unknown alpha as a tiny probe, or cap/exit
    negative expectancy. They never override current evidence or hard risk.
    """
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    target_side = _target_side_from_ratio(position_ratio)
    if target_side not in {"long", "short"}:
        return position_ratio, reasons, notes, diagnostics
    pm_config = _get_portfolio_manager_config(full_config)
    ev_cfg = (pm_config.get("alpha_setup_ev_fusion") or {})
    if not bool(ev_cfg.get("enabled", True)):
        return position_ratio, reasons, notes, diagnostics
    profiles = [
        row for row in (alpha_setup_profiles or [])
        if isinstance(row, dict) and str(row.get("side") or "*").lower() in {target_side, "*"}
    ]
    action_values = [
        row for row in (alpha_setup_action_values or [])
        if isinstance(row, dict) and str(row.get("side") or "*").lower() in {target_side, "*"}
    ]
    if not profiles and not action_values:
        confirmation_score = _safe_float((market_confirmation or {}).get("confirmation_score"), 0.0)
        scorecard = opportunity_scorecard if isinstance(opportunity_scorecard, dict) else {}
        side_scorecard = scorecard.get(target_side) if isinstance(scorecard.get(target_side), dict) else {}
        layer = str(side_scorecard.get("final_state") or "unknown").lower()
        has_tradeable_support = layer in {"tradeable_candidate", "probe_candidate"}
        setup_quality_ok = _scorecard_setup_quality_ok(side_scorecard)
        has_monitorable_setup = _scorecard_monitorable_setup(side_scorecard)
        (
            has_entry_invalidation,
            has_position_exit_boundary,
        ) = _target_execution_lifecycle_boundaries(
            analyst_signals or [],
            target_side,
        )
        payloads = _analyst_signal_payloads(analyst_signals or {})
        min_support_confidence = float(ev_cfg.get("real_trade_min_analyst_confidence", 0.45) or 0.45)
        technical_payload = payloads.get("technical", {})
        fundamental_payload = payloads.get("fundamental", {})
        news_payload = payloads.get("commodity_news", {})
        technical_direction_supports_side = _payload_supports_side(technical_payload, target_side, min_support_confidence)
        technical_entry_timing_supports_side = _technical_payload_has_entry_timing(
            technical_payload,
            target_side,
            min_support_confidence,
        )
        technical_opposes_side = _payload_opposes_side(technical_payload, target_side, min_support_confidence)
        fundamental_supports_side = _payload_supports_side(fundamental_payload, target_side, min_support_confidence)
        news_supports_side = _payload_supports_side(news_payload, target_side, min_support_confidence)
        independent_support_count = sum(
            1 for item in (
                technical_entry_timing_supports_side,
                fundamental_supports_side,
                news_supports_side,
            )
            if item
        )
        strong_confirmation_score = float(ev_cfg.get("real_trade_strong_confirmation_score", 0.65) or 0.65)
        strong_market_confirmation = confirmation_score >= strong_confirmation_score
        strong_realtime_evidence = bool(
            technical_entry_timing_supports_side
            or _news_high_quality_override(news_payload, target_side, ev_cfg)
            or (
                independent_support_count >= 2
                and has_entry_invalidation
                and confirmation_score >= float(ev_cfg.get("min_confirmation_score", 0.52) or 0.52)
            )
            or (
                strong_market_confirmation
                and has_tradeable_support
                and has_entry_invalidation
            )
        )
        diagnostics["alpha_setup_ev_fusion"] = {
            "enabled": True,
            "decision": "no_expectancy_evidence",
            "target_side": target_side,
            "scorecard_state": layer,
            "side_priority": side_scorecard.get("side_priority"),
            "ticker_side_priority": side_scorecard.get("ticker_side_priority"),
            "side_priority_score": side_scorecard.get("side_priority_score"),
            "candidate_quality": side_scorecard.get("candidate_quality"),
            "candidate_layer_hint": side_scorecard.get("candidate_layer_hint"),
            "side_priority_semantics_version": side_scorecard.get("side_priority_semantics_version"),
            "side_priority_is_not_capital_rank": bool(side_scorecard.get("side_priority_is_not_capital_rank", True)),
            "current_confirmation_score": confirmation_score,
            "has_tradeable_support": has_tradeable_support,
            "has_monitorable_setup": has_monitorable_setup,
            "setup_quality_ok": setup_quality_ok,
            "has_entry_invalidation": has_entry_invalidation,
            "has_position_exit_boundary": has_position_exit_boundary,
            "strong_realtime_evidence": strong_realtime_evidence,
            "strong_market_confirmation": strong_market_confirmation,
            "technical_supports_side": technical_entry_timing_supports_side,
            "technical_direction_supports_side": technical_direction_supports_side,
            "technical_entry_timing_supports_side": technical_entry_timing_supports_side,
            "technical_opposes_side": technical_opposes_side,
            "fundamental_supports_side": fundamental_supports_side,
            "news_supports_side": news_supports_side,
            "independent_support_count": independent_support_count,
            "open_action_value_missing": False,
            "qualified_positive_expectancy": False,
            "positive_action_value": False,
            "negative_action_value": False,
            "positive_profile": False,
            "negative_profile": False,
        }
        return position_ratio, reasons, notes, diagnostics
    intended_action = _intended_alpha_setup_action(position_ratio, current_ratio)

    def _rank(row: dict) -> tuple:
        state = str(row.get("lifecycle_state") or "candidate").lower()
        state_rank = {
            "deployable": 5,
            "protected": 4,
            "watchlist": 3,
            "candidate": 2,
            "capped": 1,
            "rejected": 0,
        }.get(state, 2)
        return (
            state_rank,
            _safe_float(row.get("confidence_score"), 0.0),
            int(row.get("sample_count") or 0),
            _safe_float(row.get("net_pnl"), 0.0),
        )

    best_profile = max(profiles, key=_rank) if profiles else {}
    state = str(best_profile.get("lifecycle_state") or "candidate").lower()
    confidence = _safe_float(best_profile.get("confidence_score"), 0.0)
    sample_count = int(best_profile.get("sample_count") or 0)
    net_pnl = _safe_float(best_profile.get("net_pnl"), 0.0)
    profit_factor = _safe_float(best_profile.get("profit_factor"), 0.0)
    win_rate = _safe_float(best_profile.get("win_rate"), 0.0)

    def _action_rank(row: dict) -> tuple:
        preference = _action_value_preference(row)
        if preference in (
            _POSITIVE_OPEN_ACTION_PREFERENCES
            | _POSITIVE_HOLD_ACTION_PREFERENCES
            | _POSITIVE_EXIT_ACTION_PREFERENCES
            | _POSITIVE_EXECUTION_ACTION_PREFERENCES
        ):
            preference_rank = 5
        elif preference in _NEGATIVE_ACTION_PREFERENCES:
            preference_rank = 4
        else:
            preference_rank = 0
        scope_rank = {
            "exact_real_state": 3,
            "partial_real_state": 2,
            "similar_sql_prior": 1,
            "counterfactual_prior": 0,
        }.get(_action_value_scope_quality(row, ticker=ticker, side=target_side), 0)
        return (
            preference_rank,
            scope_rank,
            _safe_float(row.get("confidence_score"), 0.0),
            int(row.get("sample_count") or 0),
            _safe_float(row.get("reward_mean"), 0.0),
            _safe_float(row.get("reward_sum"), 0.0),
        )

    intent_matched_action_values = [
        row for row in action_values
        if _alpha_action_value_matches_intent(str(row.get("action_name") or ""), intended_action)
    ]
    open_like_intent = intended_action in {"open", "add", "reverse"}
    open_action_value_missing = bool(open_like_intent and action_values and not intent_matched_action_values)
    best_action_value = max(intent_matched_action_values, key=_action_rank) if intent_matched_action_values else {}
    action_name = str(best_action_value.get("action_name") or "").lower()
    action_reward_mean = _safe_float(best_action_value.get("reward_mean"), 0.0)
    action_reward_sum = _safe_float(best_action_value.get("reward_sum"), 0.0)
    action_win_rate = _safe_float(best_action_value.get("win_rate"), 0.0)
    action_confidence = _safe_float(best_action_value.get("confidence_score"), 0.0)
    action_sample_count = int(best_action_value.get("sample_count") or 0)
    action_scope_quality = _action_value_scope_quality(best_action_value, ticker=ticker, side=target_side)
    action_exact_ticker_support = _action_value_has_exact_ticker_support(best_action_value, ticker, target_side)
    action_real_amplification_support = _action_value_can_support_real_amplification(
        best_action_value,
        ticker,
        target_side,
    )
    action_tail_loss_count = _action_value_int(best_action_value, "tail_loss_count")
    action_loss_reward_count = _action_value_int(best_action_value, "loss_reward_count")
    action_worst_reward = _safe_float(_action_value_payload(best_action_value).get("worst_reward"), 0.0)
    action_preference = _action_value_preference(best_action_value)
    confirmation_score = _safe_float((market_confirmation or {}).get("confirmation_score"), 0.0)
    scorecard = opportunity_scorecard if isinstance(opportunity_scorecard, dict) else {}
    side_scorecard = scorecard.get(target_side) if isinstance(scorecard.get(target_side), dict) else {}
    layer = str(side_scorecard.get("final_state") or "unknown").lower()
    gating_failures = [str(item) for item in (side_scorecard.get("gating_failures") or [])]
    has_tradeable_support = layer in {"tradeable_candidate", "probe_candidate"}
    setup_quality_ok = _scorecard_setup_quality_ok(side_scorecard)
    has_monitorable_setup = _scorecard_monitorable_setup(side_scorecard)
    (
        has_entry_invalidation,
        has_position_exit_boundary,
    ) = _target_execution_lifecycle_boundaries(
        analyst_signals or [],
        target_side,
    )
    payloads = _analyst_signal_payloads(analyst_signals or {})
    technical_payload = payloads.get("technical", {})
    fundamental_payload = payloads.get("fundamental", {})
    news_payload = payloads.get("commodity_news", {})

    min_conf = float(ev_cfg.get("min_profile_confidence", 0.45) or 0.45)
    min_confirmation = float(ev_cfg.get("min_confirmation_score", 0.52) or 0.52)
    min_action_conf = float(ev_cfg.get("min_action_value_confidence", 0.35) or 0.35)
    min_action_samples = int(ev_cfg.get("min_action_value_samples", 2) or 2)
    real_trade_min_samples = int(ev_cfg.get("real_trade_min_action_value_samples", min_action_samples) or min_action_samples)
    real_trade_negative_sum = float(
        ev_cfg.get("real_trade_negative_reward_sum_max", ev_cfg.get("negative_reward_sum_max", -500.0)) or -500.0
    )
    real_trade_positive_sum = float(ev_cfg.get("real_trade_positive_reward_sum_min", 0.0) or 0.0)
    min_support_confidence = float(ev_cfg.get("real_trade_min_analyst_confidence", 0.45) or 0.45)
    positive_reward_min = float(ev_cfg.get("positive_reward_mean_min", 0.0) or 0.0)
    negative_reward_max = float(ev_cfg.get("negative_reward_mean_max", -1e-9) or -1e-9)
    negative_sum_max = float(ev_cfg.get("negative_reward_sum_max", -500.0) or -500.0)
    positive_scale_multiplier = float(ev_cfg.get("positive_expectancy_multiplier", 1.15) or 1.15)
    unknown_probe_multiplier = float(ev_cfg.get("unknown_expectancy_probe_multiplier", 0.50) or 0.50)
    negative_cap_multiplier = float(ev_cfg.get("negative_expectancy_multiplier", 0.20) or 0.20)
    exit_negative_existing = bool(ev_cfg.get("exit_negative_existing_when_current_weak", True))
    require_support = bool(ev_cfg.get("require_tradeable_support_for_release", True))
    require_invalidation = bool(ev_cfg.get("require_invalidation_for_release", True))
    multipliers = ev_cfg.get("lifecycle_multipliers") or {
        "deployable": 1.15,
        "protected": 1.05,
        "watchlist": 0.75,
        "candidate": 0.50,
        "capped": 0.35,
        "rejected": 0.0,
    }
    multiplier = float(multipliers.get(state, multipliers.get("candidate", 0.50)) or 0.50)
    technical_direction_supports_side = _payload_supports_side(technical_payload, target_side, min_support_confidence)
    technical_entry_timing_supports_side = _technical_payload_has_entry_timing(
        technical_payload,
        target_side,
        min_support_confidence,
    )
    technical_supports_side = technical_entry_timing_supports_side
    technical_opposes_side = _payload_opposes_side(technical_payload, target_side, min_support_confidence)
    fundamental_supports_side = _payload_supports_side(fundamental_payload, target_side, min_support_confidence)
    fundamental_opposes_side = _payload_opposes_side(
        fundamental_payload,
        target_side,
        min_support_confidence,
    )
    news_supports_side = _payload_supports_side(news_payload, target_side, min_support_confidence)
    independent_support_count = sum(
        1 for item in (technical_supports_side, fundamental_supports_side, news_supports_side) if item
    )
    strong_confirmation_score = float(ev_cfg.get("real_trade_strong_confirmation_score", 0.65) or 0.65)
    strong_market_confirmation = confirmation_score >= strong_confirmation_score
    strong_realtime_evidence = bool(
        technical_supports_side
        or _news_high_quality_override(news_payload, target_side, ev_cfg)
        or (
            independent_support_count >= 2
            and has_entry_invalidation
            and confirmation_score >= min_confirmation
        )
        or (
            strong_market_confirmation
            and has_tradeable_support
            and has_entry_invalidation
        )
    )

    gate_failures: list[str] = []
    if confidence < min_conf:
        gate_failures.append("alpha_setup_profile_confidence_low")
    if confirmation_score < min_confirmation:
        gate_failures.append("alpha_setup_current_confirmation_low")
    if require_support and state in {"deployable", "protected"} and not has_tradeable_support:
        gate_failures.append("alpha_setup_release_requires_tradeable_support")
    if require_invalidation and state in {"deployable", "protected"} and not has_entry_invalidation:
        gate_failures.append("alpha_setup_release_requires_invalidation")
    if state in {"deployable", "protected"} and not has_position_exit_boundary:
        gate_failures.append("alpha_setup_release_requires_position_exit_boundary")
    if state in {"capped", "rejected"}:
        gate_failures.append(f"alpha_setup_{state}")
    if "critical_data_gap" in gating_failures:
        gate_failures.append("alpha_setup_current_data_gap")
    if open_action_value_missing:
        gate_failures.append("alpha_setup_open_action_value_missing")

    before = position_ratio
    profile_impact_raw = _safe_float(best_profile.get("max_position_impact"), 0.0) if best_profile else 0.0
    action_impact_raw = _safe_float(best_action_value.get("max_position_impact"), 0.0) if best_action_value else 0.0
    learned_impact = max(profile_impact_raw, action_impact_raw)
    if learned_impact <= 0:
        learned_impact = abs(max_position_ratio)
    max_profile_impact = min(abs(max_position_ratio), learned_impact)
    action_value_usable = (
        bool(best_action_value)
        and action_sample_count >= min_action_samples
        and action_confidence >= min_action_conf
    )
    exact_candidate_positive_action_value = bool(
        best_action_value
        and action_real_amplification_support
        and action_preference in _POSITIVE_OPEN_ACTION_PREFERENCES
        and action_reward_mean >= positive_reward_min
        and action_reward_sum > 0
    )
    positive_action_value_candidate = (
        (action_value_usable or exact_candidate_positive_action_value)
        and action_exact_ticker_support
        and action_preference in _POSITIVE_OPEN_ACTION_PREFERENCES
        and action_reward_mean >= positive_reward_min
        and action_reward_sum >= 0
    )
    tail_loss_blocks_real_amplification = bool(action_tail_loss_count > 0 and not strong_realtime_evidence)
    positive_action_value = (
        positive_action_value_candidate
        and action_value_usable
        and action_real_amplification_support
        and not tail_loss_blocks_real_amplification
    )
    negative_action_value = (
        action_value_usable
        and action_exact_ticker_support
        and (
            action_reward_mean <= negative_reward_max
            or action_reward_sum <= negative_sum_max
            or action_tail_loss_count > 0
        )
    )
    positive_profile_raw = state in {"deployable", "protected"} and net_pnl > 0 and profit_factor >= 1.0
    positive_profile = positive_profile_raw and not open_action_value_missing
    negative_profile = state in {"capped", "rejected"} or (sample_count >= 2 and net_pnl < 0 and profit_factor < 1.0)
    qualified_positive_expectancy = bool(
        (
            positive_action_value
            and action_sample_count >= real_trade_min_samples
            and action_reward_sum >= real_trade_positive_sum
            and not tail_loss_blocks_real_amplification
        )
        or (
            positive_profile
            and sample_count >= real_trade_min_samples
            and net_pnl >= real_trade_positive_sum
            and not open_action_value_missing
        )
    )
    repeat_loss_without_new_evidence = bool(
        (
            negative_action_value
            and action_sample_count >= real_trade_min_samples
            and action_reward_sum <= real_trade_negative_sum
        )
        or negative_profile
    ) and not strong_realtime_evidence
    if qualified_positive_expectancy and positive_action_value:
        gate_failures = [
            failure for failure in gate_failures
            if failure != "alpha_setup_profile_confidence_low"
        ]

    expectancy_lane = "unknown_alpha_probe"
    if positive_action_value or positive_profile:
        expectancy_lane = "positive_expectancy_scale"
    elif positive_action_value_candidate:
        expectancy_lane = "candidate_positive_action_preference"
    if negative_action_value or negative_profile:
        expectancy_lane = "negative_expectancy_cap_or_exit"
    if open_action_value_missing and expectancy_lane == "positive_expectancy_scale":
        expectancy_lane = "unknown_alpha_probe"

    if gate_failures and not qualified_positive_expectancy:
        if state in {"deployable", "protected"}:
            multiplier = min(multiplier, float(ev_cfg.get("failed_release_multiplier", 0.70) or 0.70))
        elif state in {"capped", "rejected"}:
            multiplier = min(multiplier, float(ev_cfg.get("negative_profile_multiplier", 0.25) or 0.25))
        else:
            multiplier = min(multiplier, float(ev_cfg.get("candidate_profile_multiplier", 0.45) or 0.45))
    if expectancy_lane == "positive_expectancy_scale" and not gate_failures:
        multiplier = max(multiplier, positive_scale_multiplier)
    elif expectancy_lane == "negative_expectancy_cap_or_exit":
        multiplier = min(multiplier, negative_cap_multiplier)
        if abs(current_ratio) <= 1e-12 and repeat_loss_without_new_evidence:
            position_ratio = 0.0
            gate_failures.append("negative_expectancy_new_entry_watchlist_only")
        if exit_negative_existing and abs(current_ratio) > 1e-12 and _same_sign(position_ratio, current_ratio):
            weak_current = confirmation_score < min_confirmation or not has_tradeable_support
            if weak_current:
                position_ratio = 0.0
                gate_failures.append("negative_expectancy_existing_position_exit")
    elif expectancy_lane in {"unknown_alpha_probe", "candidate_positive_action_preference"}:
        multiplier = min(multiplier, unknown_probe_multiplier)
    position_ratio = _scale_signed_ratio(position_ratio, multiplier)
    if expectancy_lane == "positive_expectancy_scale" and not gate_failures and max_profile_impact > 0:
        position_ratio = _signed_abs(target_side, min(abs(position_ratio), max_profile_impact))
    elif expectancy_lane == "negative_expectancy_cap_or_exit":
        position_ratio = _signed_abs(target_side, min(abs(position_ratio), max_profile_impact))

    diagnostics["alpha_setup_ev_fusion"] = {
        "enabled": True,
        "target_side": target_side,
        "intended_action": intended_action,
        "selected_profile": _compact_alpha_setup_profile(best_profile),
        "selected_action_value": _compact_alpha_setup_action_value(best_action_value),
        "profile_count": len(profiles),
        "action_value_count": len(action_values),
        "matched_action_value_count": len(intent_matched_action_values),
        "ignored_action_value_count": max(0, len(action_values) - len(intent_matched_action_values)),
        "scorecard_state": layer,
        "side_priority": side_scorecard.get("side_priority"),
        "ticker_side_priority": side_scorecard.get("ticker_side_priority"),
        "side_priority_score": side_scorecard.get("side_priority_score"),
        "candidate_quality": side_scorecard.get("candidate_quality"),
        "candidate_layer_hint": side_scorecard.get("candidate_layer_hint"),
        "side_priority_semantics_version": side_scorecard.get("side_priority_semantics_version"),
        "side_priority_is_not_capital_rank": bool(side_scorecard.get("side_priority_is_not_capital_rank", True)),
        "scorecard_gating_failures": gating_failures,
        "current_confirmation_score": confirmation_score,
        "has_tradeable_support": has_tradeable_support,
        "has_monitorable_setup": has_monitorable_setup,
        "setup_quality_ok": setup_quality_ok,
        "has_entry_invalidation": has_entry_invalidation,
        "has_position_exit_boundary": has_position_exit_boundary,
        "expectancy_lane": expectancy_lane,
        "positive_action_value": positive_action_value,
        "positive_action_value_candidate": positive_action_value_candidate,
        "candidate_positive_action_preference": bool(
            positive_action_value_candidate and not positive_action_value
        ),
        "negative_action_value": negative_action_value,
        "positive_profile": positive_profile,
        "positive_profile_raw": positive_profile_raw,
        "negative_profile": negative_profile,
        "open_action_value_missing": open_action_value_missing,
        "qualified_positive_expectancy": qualified_positive_expectancy,
        "repeat_loss_without_new_evidence": repeat_loss_without_new_evidence,
        "tail_loss_blocks_real_amplification": tail_loss_blocks_real_amplification,
        "strong_realtime_evidence": strong_realtime_evidence,
        "strong_market_confirmation": strong_market_confirmation,
        "technical_supports_side": technical_supports_side,
        "technical_direction_supports_side": technical_direction_supports_side,
        "technical_entry_timing_supports_side": technical_entry_timing_supports_side,
        "technical_opposes_side": technical_opposes_side,
        "fundamental_supports_side": fundamental_supports_side,
        "fundamental_opposes_side": fundamental_opposes_side,
        "news_supports_side": news_supports_side,
        "independent_support_count": independent_support_count,
        "profile_stats": {
            "sample_count": sample_count,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "net_pnl": net_pnl,
        },
        "action_value_stats": {
            "action_name": action_name,
            "sample_count": action_sample_count,
            "reward_mean": action_reward_mean,
            "reward_sum": action_reward_sum,
            "win_rate": action_win_rate,
            "confidence_score": action_confidence,
            "action_preference": action_preference,
            "canonical_action_preference_source": "payload.action_preference",
            "exact_ticker_support": action_exact_ticker_support,
            "scope_quality": action_scope_quality,
            "real_amplification_support": action_real_amplification_support,
            "loss_reward_count": action_loss_reward_count,
            "tail_loss_count": action_tail_loss_count,
            "worst_reward": action_worst_reward,
        },
        "multiplier": multiplier,
        "max_profile_impact": max_profile_impact,
        "gate_failures": gate_failures,
        "pre_control_ratio": float(before),
        "final_ratio": float(position_ratio),
        "not_product_blacklist": True,
        "same_scope_required": True,
        "candidate_prior_only": state in {"candidate", "watchlist"},
        "money_objective": "positive_expectancy_scale_unknown_probe_negative_cap_or_exit",
    }
    if abs(position_ratio - before) > 1e-12:
        reasons.append("alpha_setup_ev_fusion")
        reasons.append(expectancy_lane)
        if open_action_value_missing:
            reasons.append("alpha_setup_open_action_value_missing")
        if qualified_positive_expectancy:
            reasons.append("qualified_positive_expectancy")
        if positive_action_value_candidate and not positive_action_value:
            reasons.append("candidate_positive_action_preference")
        if repeat_loss_without_new_evidence:
            reasons.append("repeat_loss_watchlist_only")
        notes.append(
            f"{ticker} {target_side} alpha setup EV fusion: state={state}, "
            f"n={sample_count}, conf={confidence:.2f}, action={action_name or 'n/a'}, "
            f"reward_mean={action_reward_mean:.0f}, state={layer}, lane={expectancy_lane}, "
            f"gates={gate_failures or ['passed']}, {before:.2%}->{position_ratio:.2%}"
        )
    return position_ratio, reasons, notes, diagnostics


def _apply_winning_template_continuation_control(
    *,
    ticker: str,
    position_ratio: float,
    current_ratio: float,
    current_position,
    alpha_setup_action_values: list | None = None,
    analyst_signals: list,
    market_confirmation: dict,
    opportunity_scorecard: dict | None,
    full_config: dict,
) -> tuple[float, list[str], list[str], dict]:
    """Preserve profitable same-side alpha when current evidence still supports it."""
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    current_side = _target_side_from_ratio(current_ratio)
    target_side = _target_side_from_ratio(position_ratio)
    if current_side not in {"long", "short"}:
        return position_ratio, reasons, notes, diagnostics
    if target_side in {"long", "short"} and target_side != current_side:
        return position_ratio, reasons, notes, diagnostics
    if abs(position_ratio) > abs(current_ratio) + 1e-12:
        return position_ratio, reasons, notes, diagnostics
    control = (_get_holding_rebalance_config(full_config).get("winning_template_continuation") or {})
    if not bool(control.get("enabled", True)):
        return position_ratio, reasons, notes, diagnostics
    pnl_ratio = _position_pnl_ratio(current_position)
    min_profit_ratio = float(control.get("min_profit_ratio", 0.015) or 0.015)
    if pnl_ratio < min_profit_ratio:
        return position_ratio, reasons, notes, diagnostics
    confirmation_score = _safe_float((market_confirmation or {}).get("confirmation_score"), 0.0)
    min_confirmation = float(control.get("min_confirmation_score", 0.55) or 0.55)
    state_summary = _side_opportunity_state_summary(analyst_signals, current_side)
    scorecard = opportunity_scorecard if isinstance(opportunity_scorecard, dict) else {}
    side_scorecard = scorecard.get(current_side) if isinstance(scorecard.get(current_side), dict) else {}
    layer = str(side_scorecard.get("final_state") or "").lower()
    has_support = bool(state_summary.get("has_tradeable_support") or layer in {"tradeable_candidate", "probe_candidate"})
    if confirmation_score < min_confirmation or not has_support:
        diagnostics["winning_template_continuation"] = {
            "enabled": True,
            "decision": "no_continuation",
            "pnl_ratio": float(pnl_ratio),
            "confirmation_score": confirmation_score,
            "has_tradeable_support": has_support,
            "scorecard_state": layer,
        }
        ev_cfg = (_get_portfolio_manager_config(full_config).get("alpha_setup_ev_fusion") or {})
        learned_exit = _negative_hold_or_positive_exit_action_value(
            ticker=ticker,
            alpha_setup_action_values=alpha_setup_action_values,
            side=current_side,
            ev_cfg=ev_cfg,
        )
        if learned_exit:
            current_abs = abs(float(current_ratio or 0.0))
            reduce_multiplier = max(
                0.0,
                min(1.0, float(control.get("learned_exit_reduce_multiplier", 0.50) or 0.50)),
            )
            before = position_ratio
            learned_preference = str(learned_exit.get("_action_value_preference") or "")
            learned_action_name = str(learned_exit.get("action_name") or "").strip().lower()
            if learned_preference == "helpful_exit" and learned_action_name in {"exit", "close", "close_or_reduce", "flatten"}:
                position_ratio = 0.0
                protection_decision = "learned_exit_action_value_protective_exit"
            elif current_abs > 1e-12:
                position_ratio = _signed_abs(current_side, current_abs * reduce_multiplier)
                protection_decision = "learned_hold_exit_reduce"
            else:
                position_ratio = 0.0
                protection_decision = "learned_hold_exit_reduce"
            reasons.append("hold_exit_action_value_protection")
            notes.append(
                f"{ticker} {current_side} learned hold/exit action-value protects profit or limits giveback: "
                f"action={learned_exit.get('action_name')}, preference={learned_exit.get('_action_value_preference')}, "
                f"reward_mean={_safe_float(learned_exit.get('reward_mean'), 0.0):.0f}, "
                f"{before:.2%}->{position_ratio:.2%}"
            )
            diagnostics["winning_template_continuation"].update({
                "decision": protection_decision,
                "selected_action_value": _compact_alpha_setup_action_value(learned_exit),
                "action_value_preference": learned_preference,
                "reduce_multiplier": reduce_multiplier,
                "pre_control_ratio": float(before),
                "final_ratio": float(position_ratio),
                "protective_exit": bool(protection_decision == "learned_exit_action_value_protective_exit"),
                "same_scope_required": True,
                "does_not_override_strong_current_confirmation": True,
            })
        else:
            current_abs = abs(float(current_ratio or 0.0))
            reduce_multiplier = max(
                0.0,
                min(1.0, float(control.get("unconfirmed_profit_reduce_multiplier", 0.50) or 0.50)),
            )
            before = position_ratio
            if current_abs > 1e-12:
                position_ratio = _signed_abs(current_side, current_abs * reduce_multiplier)
            else:
                position_ratio = 0.0
            reasons.append("winning_template_continuation_protective_reduce")
            notes.append(
                f"{ticker} {current_side} profitable template lacks continuation support; "
                f"protecting profit instead of position_matched: pnl_ratio={pnl_ratio:.2%}, "
                f"confirmation={confirmation_score:.2f}, support={has_support}, "
                f"{before:.2%}->{position_ratio:.2%}"
            )
            diagnostics["winning_template_continuation"].update({
                "decision": "protective_reduce_no_continuation",
                "reduce_multiplier": reduce_multiplier,
                "pre_control_ratio": float(before),
                "final_ratio": float(position_ratio),
                "does_not_override_strong_current_confirmation": True,
                "prevents_position_matched_profit_giveback": True,
            })
        return position_ratio, reasons, notes, diagnostics
    preserve_multiplier = max(0.0, min(1.0, float(control.get("preserve_current_multiplier", 0.75) or 0.75)))
    min_abs = abs(current_ratio) * preserve_multiplier
    if abs(position_ratio) >= min_abs:
        return position_ratio, reasons, notes, diagnostics
    before = position_ratio
    position_ratio = _signed_abs(current_side, min_abs)
    reasons.append("winning_template_continuation")
    notes.append(
        f"{ticker} {current_side} profitable template retained under current confirmation: "
        f"pnl_ratio={pnl_ratio:.2%}, confirmation={confirmation_score:.2f}, "
        f"{before:.2%}->{position_ratio:.2%}"
    )
    diagnostics["winning_template_continuation"] = {
        "enabled": True,
        "decision": "preserve_profitable_same_scope_position",
        "pnl_ratio": float(pnl_ratio),
        "confirmation_score": confirmation_score,
        "scorecard_state": layer,
        "preserve_current_multiplier": preserve_multiplier,
        "pre_control_ratio": float(before),
        "final_ratio": float(position_ratio),
        "does_not_override_loss_revalidation": True,
    }
    return position_ratio, reasons, notes, diagnostics


def _apply_drawdown_and_ticker_loss_control(
    *,
    db,
    config_id: str,
    ticker: str,
    trading_date,
    position_ratio: float,
    current_ratio: float,
    current_margin_ratio: float = 0.0,
    margin_rate: float = 0.0,
    market_confirmation: dict | None = None,
    signal_combo: tuple[str, str, str] | None = None,
    strategy_memory: dict | None = None,
    analyst_signals: list | None = None,
    full_config: dict,
) -> tuple[float, list[str], list[str], dict]:
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    if not _is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, reasons, notes, diagnostics

    drawdown_control = full_config.get("drawdown_control", {}) or {}
    if drawdown_control.get("enabled", False) and db and config_id:
        drawdown_state = db.get_account_drawdown_state(
            config_id=config_id,
            trading_date=trading_date,
            initial_capital=float(full_config.get("cashflow", 0.0) or 0.0),
        )
        diagnostics["drawdown_state"] = drawdown_state
        drawdown = _safe_float(drawdown_state.get("drawdown"), 0.0)
        warning_drawdown = _safe_float(
            drawdown_control.get("warning_drawdown", drawdown_control.get("soft_drawdown")),
            0.04,
        )
        hard_drawdown = _safe_float(drawdown_control.get("hard_drawdown"), 0.05)
        confirmation = market_confirmation or {}
        confirmations = confirmation.get("confirmations") or []
        confirmation_score = _safe_float(confirmation.get("confirmation_score"), 0.0)
        target_side = _target_side_from_ratio(position_ratio)
        stop_protected = _has_explicit_stop_protection(
            analyst_signals or [],
            target_side=target_side,
        )
        drawdown_diag = {
            "enabled": True,
            "drawdown": drawdown,
            "warning_drawdown": warning_drawdown,
            "hard_drawdown": hard_drawdown,
            "current_margin_ratio": float(current_margin_ratio or 0.0),
            "margin_rate": float(margin_rate or 0.0),
            "target_side": target_side,
            "stop_protected": bool(stop_protected),
            "confirmation_score": confirmation_score,
            "confirmation_count": len(confirmations),
            "pre_control_ratio": float(position_ratio),
            "current_ratio": float(current_ratio),
        }
        diagnostics["drawdown_control"] = drawdown_diag

        if drawdown >= hard_drawdown:
            hard_streak = _drawdown_hard_streak_state(
                db=db,
                config_id=config_id,
                trading_date=trading_date,
                initial_capital=float(full_config.get("cashflow", 0.0) or 0.0),
                hard_drawdown=hard_drawdown,
            )
            recovery_history = _drawdown_recovery_probe_history(
                db=db,
                config_id=config_id,
                trading_date=trading_date,
                control=drawdown_control,
            )
            initial_cooldown_days = max(0, int(drawdown_control.get("initial_hard_cooldown_days", 1)))
            initial_cooldown_active = (
                initial_cooldown_days > 0
                and int(hard_streak.get("consecutive_hard_days", 0) or 0) <= initial_cooldown_days
            )
            drawdown_diag.update({
                "state": "hard_protection",
                "hard_streak": hard_streak,
                "recovery_history": recovery_history,
                "initial_cooldown_days": initial_cooldown_days,
                "initial_cooldown_active": initial_cooldown_active,
            })

            if initial_cooldown_active or bool(recovery_history.get("cooldown_active")):
                before = position_ratio
                position_ratio = _block_new_or_incremental_exposure(position_ratio, current_ratio)
                reasons.append("drawdown_control")
                mode = "hard_initial_cooldown" if initial_cooldown_active else "hard_observation_only"
                drawdown_diag.update({
                    "mode": mode,
                    "counterfactual_recommendation": True,
                    "final_ratio": float(position_ratio),
                })
                notes.append(
                    f"hard drawdown {mode}: drawdown={drawdown:.2%}; "
                    f"new/incremental exposure {before:.2%}->{position_ratio:.2%}; "
                    "agents continue analysis and counterfactual recommendation logging"
                )
            else:
                min_score = _safe_float(drawdown_control.get("recovery_probe_min_confirmation_score"), 0.65)
                min_confirmations = int(drawdown_control.get("recovery_probe_min_confirmations", 3))
                require_stop = bool(drawdown_control.get("recovery_probe_require_stop_protection", True))
                weak_conflict = (
                    _conflicting_weak_memory_record(strategy_memory or {}, signal_combo or ("*", "*", "*"))
                    if bool(drawdown_control.get("recovery_probe_block_weak_memory", True))
                    else {}
                )
                gate_failures = []
                if target_side not in {"long", "short"}:
                    gate_failures.append("no_directional_new_entry")
                if len(confirmations) < min_confirmations:
                    gate_failures.append("insufficient_market_confirmations")
                if confirmation_score < min_score:
                    gate_failures.append("market_confirmation_score_below_probe_threshold")
                if require_stop and not stop_protected:
                    gate_failures.append("missing_stop_or_invalidation_boundary")
                if weak_conflict:
                    gate_failures.append("conflicting_weak_memory")

                drawdown_diag.update({
                    "mode": "hard_recovery_probe_candidate",
                    "recovery_probe_min_confirmation_score": min_score,
                    "recovery_probe_min_confirmations": min_confirmations,
                    "recovery_probe_require_stop_protection": require_stop,
                    "weak_conflict": weak_conflict,
                    "gate_failures": gate_failures,
                })

                if gate_failures:
                    before = position_ratio
                    position_ratio = _block_new_or_incremental_exposure(position_ratio, current_ratio)
                    reasons.append("drawdown_control")
                    drawdown_diag.update({
                        "mode": "hard_recovery_counterfactual_only",
                        "counterfactual_recommendation": True,
                        "final_ratio": float(position_ratio),
                    })
                    notes.append(
                        f"hard drawdown recovery counterfactual-only: failures={gate_failures}; "
                        f"new/incremental exposure {before:.2%}->{position_ratio:.2%}"
                    )
                else:
                    before = position_ratio
                    recovery_budget = _recovery_probe_budget(drawdown_control, recovery_history)
                    recovery_budget = min(
                        recovery_budget,
                        _safe_float(drawdown_control.get("recovery_probe_margin_ratio_max"), recovery_budget or 0.02),
                    )
                    if recovery_budget <= 0:
                        recovery_budget = _safe_float(drawdown_control.get("recovery_probe_margin_ratio_max"), 0.02)
                    allowed_increment_margin = max(0.0, recovery_budget - float(current_margin_ratio or 0.0))
                    if allowed_increment_margin <= 0 and not (
                        abs(current_ratio) > 1e-12 and _same_sign(position_ratio, current_ratio)
                    ):
                        position_ratio = _block_new_or_incremental_exposure(position_ratio, current_ratio)
                        reasons.append("drawdown_control")
                        drawdown_diag.update({
                            "mode": "hard_recovery_counterfactual_only",
                            "counterfactual_recommendation": True,
                            "recovery_probe_margin_ratio_budget": recovery_budget,
                            "allowed_increment_margin_ratio": allowed_increment_margin,
                            "gate_failures": ["no_capacity_under_recovery_probe_budget"],
                            "final_ratio": float(position_ratio),
                        })
                        notes.append(
                            f"hard drawdown recovery counterfactual-only: current_margin={float(current_margin_ratio or 0.0):.2%} "
                            f">= recovery_budget={recovery_budget:.2%}; "
                            f"new/incremental exposure {before:.2%}->{position_ratio:.2%}"
                        )
                    else:
                        position_ratio = _cap_by_incremental_margin_budget(
                            target_ratio=position_ratio,
                            current_ratio=current_ratio,
                            margin_rate=margin_rate,
                            allowed_increment_margin_ratio=allowed_increment_margin,
                        )
                        if abs(position_ratio - before) > 1e-12:
                            reasons.append("drawdown_recovery_probe")
                        drawdown_diag.update({
                            "mode": "hard_recovery_probe",
                            "counterfactual_recommendation": False,
                            "recovery_probe_margin_ratio_budget": recovery_budget,
                            "allowed_increment_margin_ratio": allowed_increment_margin,
                            "final_ratio": float(position_ratio),
                        })
                        notes.append(
                            f"hard drawdown recovery probe allowed: drawdown={drawdown:.2%}, "
                            f"budget={recovery_budget:.2%}, ratio {before:.2%}->{position_ratio:.2%}"
                        )
        elif drawdown >= warning_drawdown:
            before = position_ratio
            warning_multiplier = _safe_float(
                drawdown_control.get("warning_cap_multiplier", drawdown_control.get("soft_cap_multiplier")),
                0.60,
            )
            position_ratio = _scale_signed_ratio(position_ratio, warning_multiplier)
            warning_target = _safe_float(drawdown_control.get("warning_target_margin_ratio_max"), 0.04)
            if warning_target > 0 and margin_rate > 0:
                allowed_increment_margin = max(0.0, warning_target - float(current_margin_ratio or 0.0))
                if allowed_increment_margin <= 0 and not (
                    abs(current_ratio) > 1e-12 and _same_sign(position_ratio, current_ratio)
                ):
                    position_ratio = _block_new_or_incremental_exposure(position_ratio, current_ratio)
                else:
                    position_ratio = _cap_by_incremental_margin_budget(
                        target_ratio=position_ratio,
                        current_ratio=current_ratio,
                        margin_rate=margin_rate,
                        allowed_increment_margin_ratio=allowed_increment_margin,
                    )
            reasons.append("drawdown_control")
            drawdown_diag.update({
                "state": "warning",
                "mode": "warning_scaled_risk",
                "warning_cap_multiplier": warning_multiplier,
                "warning_target_margin_ratio_max": warning_target,
                "final_ratio": float(position_ratio),
            })
            notes.append(
                f"warning drawdown control: drawdown={drawdown:.2%}, "
                f"ratio {before:.2%}->{position_ratio:.2%}; high-conviction scaling disabled"
            )
        else:
            drawdown_diag.update({
                "state": "normal",
                "mode": "normal",
                "final_ratio": float(position_ratio),
            })

    ticker_loss_control = full_config.get("ticker_loss_control", {}) or {}
    if ticker_loss_control.get("enabled", False) and db and config_id and abs(position_ratio) > 0:
        loss_state = db.get_ticker_loss_state(
            config_id=config_id,
            ticker=ticker,
            trading_date=trading_date,
            lookback_days=int(ticker_loss_control.get("lookback_days", 5)),
        )
        diagnostics["ticker_loss_state"] = loss_state
        loss_threshold = float(ticker_loss_control.get("loss_threshold", -8000))
        consecutive_limit = int(ticker_loss_control.get("block_new_entries_after_consecutive_losses", 3))
        if int(loss_state.get("consecutive_loss_days", 0) or 0) >= consecutive_limit:
            position_ratio = _block_new_or_incremental_exposure(position_ratio, current_ratio)
            reasons.append("ticker_loss_control")
            notes.append(
                f"{ticker} blocked by consecutive loss days={loss_state.get('consecutive_loss_days')}"
            )
        elif float(loss_state.get("cumulative_pnl", 0.0) or 0.0) <= loss_threshold:
            before = position_ratio
            position_ratio = _scale_signed_ratio(position_ratio, float(ticker_loss_control.get("cap_multiplier", 0.50)))
            reasons.append("ticker_loss_control")
            notes.append(
                f"{ticker} recent loss control: cumulative_pnl={loss_state.get('cumulative_pnl'):.0f}, "
                f"ratio {before:.2%}->{position_ratio:.2%}"
            )

    return position_ratio, reasons, notes, diagnostics


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _analyst_signal_payloads(analyst_signals: list, fusion_context=None) -> dict:
    fusion_context = fusion_context or {}
    quality_summary = fusion_context.get("analyst_quality") or {}
    payloads = {}
    for signal in analyst_signals:
        agent_name = _normalize_agent_name(getattr(signal, "agent_name", ""))
        if agent_name not in ANALYST_ORDER:
            continue
        signal_text = _signal_to_text(getattr(signal, "signal", "Neutral"))
        side = "neutral"
        if signal_text.upper() == "BULLISH":
            side = "long"
        elif signal_text.upper() == "BEARISH":
            side = "short"

        metadata = _signal_metadata(signal)
        context = _nested_context(metadata, agent_name)
        business = metadata.get("business_quality") if isinstance(metadata.get("business_quality"), dict) else {}
        quality = quality_summary.get(agent_name, {})
        contract_fields = _derive_signal_contract_fields(signal, agent_name)
        effective_confidence = _safe_float(
            quality.get("effective_confidence"),
            _safe_float(getattr(signal, "confidence", 0.0), 0.0),
        )
        payloads[agent_name] = {
            "signal": signal_text,
            "side": side,
            "raw_confidence": _safe_float(getattr(signal, "confidence", 0.0), 0.0),
            "effective_confidence": effective_confidence,
            "tradeability": str(
                quality.get("tradeability")
                or metadata.get("tradeability")
                or context.get("tradeability")
                or "unknown"
            ).lower(),
            "business_quality_score": _safe_float(
                getattr(signal, "business_quality_score", business.get("score", 0.0)),
                0.0,
            ),
            "setup_type": str(getattr(signal, "setup_type", metadata.get("setup_type", "unknown")) or "unknown"),
            "horizon_class": str(getattr(signal, "horizon_class", "unknown") or "unknown"),
            "analyst_horizon": str(getattr(signal, "analyst_horizon", getattr(signal, "horizon_class", "unknown")) or "unknown"),
            "evidence_role": contract_fields.get("evidence_role"),
            "direction_context": contract_fields.get("direction_context"),
            "entry_trigger": contract_fields.get("entry_trigger"),
            "invalidation": contract_fields.get("invalidation"),
            "horizon_class": contract_fields.get("horizon_class"),
            "trend_direction": contract_fields.get("trend_direction"),
            "entry_timing_signal": contract_fields.get("entry_timing_signal"),
            "price_location": contract_fields.get("price_location"),
            "trigger_valid": _canonical_trigger_valid(signal),
            "invalidation_present": _canonical_invalidation_present(signal),
            "primary_business_driver": getattr(signal, "primary_business_driver", business.get("primary_business_driver", "")),
            "counter_evidence": getattr(signal, "counter_evidence", business.get("counter_evidence", "")),
            "risk_flags": quality.get("risk_flags") or metadata.get("risk_flags") or context.get("risk_flags") or [],
            "metadata": metadata,
            "context": context,
        }
    return payloads


def _payload_supports_side(payload, side: str, min_confidence: float) -> bool:
    if not payload or side not in {"long", "short"}:
        return False
    return (
        payload.get("side") == side
        and _safe_float(payload.get("effective_confidence"), 0.0) >= min_confidence
    )


def _technical_payload_has_entry_timing(payload, side: str, min_confidence: float) -> bool:
    if not _payload_supports_side(payload, side, min_confidence):
        return False
    profile = normalize_execution_profile(payload.get("entry_timing_signal"))
    trigger_source = trigger_source_for_analyst_profile("technical", profile)
    return bool(
        payload.get("evidence_role") == "entry_timing"
        and payload.get("trigger_valid")
        and payload.get("invalidation_present")
        and not execution_trigger_contract_error(
            profile=profile,
            side=side,
            entry_trigger=payload.get("entry_trigger"),
            trigger_source=trigger_source,
        )
    )


def _opposite_side(side: str) -> str:
    return "short" if side == "long" else "long" if side == "short" else ""


def _payload_opposes_side(payload, side: str, min_confidence: float) -> bool:
    opposite = _opposite_side(side)
    if not payload or not opposite:
        return False
    return _payload_supports_side(payload, opposite, min_confidence)


def _fundamental_anchor_supports(payload, side: str, control: dict) -> bool:
    if not payload or side not in {"long", "short"}:
        return False
    allowed_tradeability = {
        str(item).lower()
        for item in (control.get("fundamental_anchor_tradeability") or ["high", "medium"])
    }
    min_confidence = float(control.get("min_fundamental_anchor_confidence", 0.40))
    min_business_quality = float(control.get("min_fundamental_anchor_business_quality", 0.55))
    return (
        _payload_supports_side(payload, side, min_confidence)
        and str(payload.get("tradeability", "unknown")).lower() in allowed_tradeability
        and _safe_float(payload.get("business_quality_score"), 0.0) >= min_business_quality
    )


def _hold_anchor_supports_side(payload, side: str, lifecycle_config: dict) -> bool:
    if not payload or side not in {"long", "short"}:
        return False
    allowed_tradeability = {
        str(item).lower()
        for item in (lifecycle_config.get("profitable_hold_anchor_tradeability") or ["high", "medium"])
    }
    min_confidence = float(lifecycle_config.get("profitable_hold_anchor_min_analyst_confidence", 0.35))
    return (
        payload.get("side") == side
        and _safe_float(payload.get("effective_confidence"), 0.0) >= min_confidence
        and str(payload.get("tradeability", "unknown")).lower() in allowed_tradeability
    )


def _news_high_quality_override(payload, side: str, control: dict) -> bool:
    if not payload or side not in {"long", "short"}:
        return False
    required_tradeability = str(control.get("news_override_tradeability", "high")).lower()
    min_confidence = float(control.get("news_override_min_confidence", 0.60))
    if not _payload_supports_side(payload, side, min_confidence):
        return False
    if str(payload.get("tradeability", "unknown")).lower() != required_tradeability:
        return False

    metadata = payload.get("metadata") or {}
    freshness, relevance = scc_news_quality_scores_from_metadata(metadata)
    return (
        freshness >= float(control.get("news_override_min_freshness", 0.70))
        and relevance >= float(control.get("news_override_min_relevance", 0.70))
    )


def _side_signal_strength(side: str, long_scores: dict, short_scores: dict) -> float:
    scores = long_scores if side == "long" else short_scores if side == "short" else {}
    return _safe_float(scores.get("score"), 0.0) * _safe_float(scores.get("confidence"), 0.0)


def _apply_business_quality_position_gate(
    *,
    position_ratio: float,
    current_ratio: float,
    analyst_signals: list,
    full_config: dict,
) -> tuple[float, list[str], list[str], dict]:
    return business_quality_position_gate(
        position_ratio=position_ratio,
        current_ratio=current_ratio,
        analyst_signals=analyst_signals,
        config=full_config,
    )


def _signed_abs(side: str, abs_ratio: float) -> float:
    return abs(abs_ratio) if side == "long" else -abs(abs_ratio)


def _position_pnl_ratio(current_position) -> float:
    if not current_position:
        return 0.0
    margin_used = _safe_float(getattr(current_position, "margin_used", 0.0), 0.0)
    if margin_used <= 0:
        return 0.0
    return _safe_float(getattr(current_position, "unrealized_pnl", 0.0), 0.0) / margin_used


def _lifecycle_revalidated_by_current_evidence(
    *,
    current_side: str,
    current_strength: float,
    confirmation_score: float,
    fundamental_supports_current: bool,
    technical_supports_current: bool,
    news_supports_current: bool,
    lifecycle_config: dict,
) -> bool:
    if current_side not in {"long", "short"}:
        return False
    anchor_min_confirmation = float(
        lifecycle_config.get(
            "loss_revalidation_anchor_min_confirmation_score",
            lifecycle_config.get("loss_revalidation_min_confirmation_score", 0.55),
        )
    )
    anchor_min_strength = float(
        lifecycle_config.get(
            "loss_revalidation_anchor_min_signal_strength",
            lifecycle_config.get("loss_revalidation_min_signal_strength", 0.25),
        )
    )
    anchor_confirmed = (
        confirmation_score >= anchor_min_confirmation
        or current_strength >= anchor_min_strength
    )
    if (fundamental_supports_current or news_supports_current) and anchor_confirmed:
        return True
    min_confirmation = float(lifecycle_config.get("loss_revalidation_min_confirmation_score", 0.55))
    min_strength = float(lifecycle_config.get("loss_revalidation_min_signal_strength", 0.25))
    return (
        technical_supports_current
        and confirmation_score >= min_confirmation
        and current_strength >= min_strength
    )


def _profitable_hold_still_supported(
    *,
    current_side: str,
    position_pnl_ratio: float,
    current_strength: float,
    confirmation_score: float,
    signal_counts_current: dict,
    fundamental_supports_current: bool,
    technical_supports_current: bool,
    news_supports_current: bool,
    current_state: str,
    lifecycle_config: dict,
) -> bool:
    """Protect profitable positions only when current same-side evidence remains alive."""
    if not bool(lifecycle_config.get("profitable_hold_continuation_enabled", True)):
        return False
    if current_side not in {"long", "short"}:
        return False
    if position_pnl_ratio < float(lifecycle_config.get("profitable_hold_min_pnl_ratio", 0.003)):
        return False
    if int(signal_counts_current.get("opposite", 0) or 0) > int(
        lifecycle_config.get("profitable_hold_max_opposite_signals", 0) or 0
    ):
        return False
    if int(signal_counts_current.get("same", 0) or 0) < int(
        lifecycle_config.get("profitable_hold_min_supporting_signals", 1) or 1
    ):
        return False
    min_confirmation = float(lifecycle_config.get("profitable_hold_min_confirmation_score", 0.45))
    min_strength = float(lifecycle_config.get("profitable_hold_min_signal_strength", 0.20))
    anchor_min_confirmation = float(
        lifecycle_config.get("profitable_hold_anchor_min_confirmation_score", min_confirmation)
    )
    anchor_min_strength = float(
        lifecycle_config.get("profitable_hold_anchor_min_signal_strength", min_strength)
    )
    has_current_support = (
        fundamental_supports_current
        or technical_supports_current
        or news_supports_current
        or str(current_state or "").lower() in {"probe_candidate", "tradeable_candidate"}
    )
    anchor_supported = fundamental_supports_current or news_supports_current
    if anchor_supported:
        return bool(
            has_current_support
            and confirmation_score >= anchor_min_confirmation
            and current_strength >= anchor_min_strength
        )
    return bool(
        has_current_support
        and confirmation_score >= min_confirmation
        and current_strength >= min_strength
    )


def _preserve_existing_lot_when_hold_ratio_survives(
    *,
    target_lots: int,
    current_lots: int,
    target_ratio: float,
    current_ratio: float,
    control_reasons: list[str],
) -> tuple[int, bool]:
    """Keep exact lots when the final lifecycle decision preserved the position."""
    if current_lots == 0 or target_lots == current_lots:
        return target_lots, False
    if abs(target_ratio) <= 1e-12 or abs(current_ratio) <= 1e-12:
        return target_lots, False
    if abs(float(target_ratio) - float(current_ratio)) > 1e-12:
        return target_lots, False
    same_side_ratio = (current_lots > 0 and target_ratio > 0) or (current_lots < 0 and target_ratio < 0)
    if not same_side_ratio:
        return target_lots, False
    hold_reasons = {
        "profitable_hold_continuation",
        "position_lifecycle_trend_hold",
        "holding_lifecycle_not_invalidated",
        "holding_add_requires_current_technical_trigger",
        "holding_period_control",
        "fundamental_anchor_rebalance_cap",
        "minimum_rebalance_threshold",
    }
    if not any(reason in hold_reasons for reason in control_reasons or []):
        return target_lots, False
    return current_lots, True


def _side_signal_counts(payloads: dict, side: str) -> dict:
    counts = {"same": 0, "opposite": 0, "neutral": 0}
    opposite = "short" if side == "long" else "long" if side == "short" else ""
    for payload in (payloads or {}).values():
        payload_side = str((payload or {}).get("side") or "neutral").lower()
        if payload_side == side:
            counts["same"] += 1
        elif payload_side == opposite:
            counts["opposite"] += 1
        elif payload_side == "neutral":
            counts["neutral"] += 1
    return counts


def _side_opportunity_state_summary(analyst_signals: list, side: str) -> dict:
    target_signal = "Bullish" if side == "long" else "Bearish" if side == "short" else ""
    opportunity_state_counts: dict[str, int] = {}
    supporting = 0
    tradeable_support = 0
    probe_candidate_support = 0
    watch_for_trigger_support = 0
    risk_reduction_support = 0
    states: list[str] = []
    for signal in analyst_signals or []:
        signal_side = _signal_to_text(getattr(signal, "signal", "Neutral"))
        state = _signal_opportunity_state(signal)
        counterfactual_side = str(getattr(signal, "counterfactual_side", "") or "").lower()
        state_targets_side = (
            signal_side == target_signal
            or (signal_side == "Neutral" and state == "watch_for_trigger" and counterfactual_side == side)
            or (state == "risk_reduction_candidate" and signal_side in {target_signal, "Neutral"})
        )
        if not state_targets_side:
            continue
        states.append(state)
        opportunity_state_counts[state] = opportunity_state_counts.get(state, 0) + 1
        if state == "risk_reduction_candidate":
            risk_reduction_support += 1
            continue
        supporting += 1
        if state in {"probe_candidate", "tradeable_candidate"}:
            tradeable_support += 1
        if state in {"probe_candidate", "tradeable_candidate"}:
            probe_candidate_support += 1
        if state == "watch_for_trigger":
            watch_for_trigger_support += 1
    return {
        "side": side,
        "supporting_signal_count": supporting,
        "tradeable_support_count": tradeable_support,
        "probe_candidate_support_count": probe_candidate_support,
        "watch_for_trigger_support_count": watch_for_trigger_support,
        "risk_reduction_support_count": risk_reduction_support,
        "opportunity_state_counts": opportunity_state_counts,
        "opportunity_states": states,
        "has_tradeable_support": tradeable_support > 0,
        "has_probe_candidate_support": probe_candidate_support > 0,
        "has_watch_for_trigger_support": watch_for_trigger_support > 0,
        "has_risk_reduction_support": risk_reduction_support > 0,
    }


def _daily_tradeability_mature_alpha_present(
    adaptive_policy_state: list | None,
    side: str,
    control: dict,
) -> bool:
    if side not in {"long", "short"}:
        return False
    min_confidence = _safe_float(control.get("min_mature_alpha_confidence"), 0.60)
    min_samples = int(control.get("min_mature_alpha_samples", 5) or 5)
    for row in adaptive_policy_state or []:
        if not isinstance(row, dict):
            continue
        row_side = str(row.get("side") or "*").lower()
        if row_side not in {"*", side}:
            continue
        action = str(row.get("policy_action") or "").lower()
        if action not in {"protect", "allow"}:
            continue
        policy_type = str(row.get("policy_type") or "")
        if policy_type != "alpha_promotion" and not policy_type.startswith("learning_mechanism:"):
            continue
        if _safe_float(row.get("confidence_score"), 0.0) < min_confidence:
            continue
        if _safe_policy_int(row.get("sample_count"), 0) < min_samples:
            continue
        return True
    return False


def _daily_tradeability_gate_result(
    *,
    side: str,
    current_ratio: float,
    analyst_signals: list,
    technical_payload: dict | None,
    news_payload: dict | None,
    market_confirmation: dict | None,
    opportunity_scorecard_side: dict,
    state_summary: dict,
    horizon_result: dict | None,
    has_invalidation: bool,
    adaptive_policy_state: list | None,
    control: dict,
) -> dict:
    """Decide whether a new daily entry is a tradable setup or only a watchlist idea.

    This is not a product rule. It prevents medium/long directional views from
    becoming daily futures entries in choppy/range markets unless current-day
    timing, catalyst, confirmation, or mature same-scope alpha is present.
    """
    detail = {
        "enabled": bool(control.get("enabled", True)),
        "decision": "allow",
        "side": side,
        "reason": None,
        "failures": [],
        "allowances": [],
        "current_ratio": float(current_ratio),
        "not_product_rule": True,
        "new_entries_only": True,
    }
    if not detail["enabled"] or side not in {"long", "short"}:
        return detail
    if abs(float(current_ratio or 0.0)) > 1e-12:
        detail["decision"] = "allow_existing_position"
        return detail

    scorecard_state = str((opportunity_scorecard_side or {}).get("final_state") or "").lower()
    regime = str((opportunity_scorecard_side or {}).get("market_regime") or "").lower()
    if not regime:
        regime = str(_market_regime_from_signals(analyst_signals, side) or "").lower()
    horizon = str((horizon_result or {}).get("decision_horizon") or "").lower()
    if not horizon:
        horizon = str(_resolve_decision_horizon(analyst_signals, 1 if side == "long" else -1) or "").lower()
    fail_reasons = [str(item) for item in ((horizon_result or {}).get("fail_reasons") or [])]
    scorecard_failures = [str(item) for item in ((opportunity_scorecard_side or {}).get("gating_failures") or [])]
    confirmation_score = _safe_float((market_confirmation or {}).get("confirmation_score"), 0.0)
    regimes = {str(item).lower() for item in (control.get("regimes_requiring_short_timing") or [])}
    horizons = {str(item).lower() for item in (control.get("horizons_requiring_short_timing") or [])}
    technical_timing = _has_short_timing_support(
        side=side,
        technical_payload=technical_payload,
        news_payload=None,
        market_confirmation=market_confirmation,
        control={
            **control,
            "min_short_timing_confidence": control.get("min_short_timing_confidence", 0.45),
            "min_confirmation_score": control.get("min_confirmation_score", 0.55),
        },
    )
    high_quality_news = _news_high_quality_override(news_payload, side, control)
    mature_alpha = _daily_tradeability_mature_alpha_present(adaptive_policy_state, side, control)
    strong_confirmation = confirmation_score >= _safe_float(control.get("strong_market_confirmation_score"), 0.68)
    has_tradeable_support = bool((state_summary or {}).get("has_tradeable_support"))
    detail.update({
        "scorecard_state": scorecard_state,
        "market_regime": regime,
        "decision_horizon": horizon,
        "horizon_failures": fail_reasons,
        "scorecard_failures": scorecard_failures,
        "confirmation_score": float(confirmation_score),
        "has_invalidation": bool(has_invalidation),
        "has_tradeable_support": has_tradeable_support,
        "technical_timing_support": bool(technical_timing),
        "high_quality_news_support": bool(high_quality_news),
        "mature_alpha_support": bool(mature_alpha),
        "strong_market_confirmation": bool(strong_confirmation),
    })

    if bool(control.get("allow_with_tradeable_support", True)) and has_tradeable_support and has_invalidation:
        detail["allowances"].append("tradeable_support_with_invalidation")
    if bool(control.get("allow_with_technical_timing", True)) and technical_timing and has_invalidation:
        detail["allowances"].append("technical_timing_with_invalidation")
    if bool(control.get("allow_with_high_quality_news", True)) and high_quality_news and has_invalidation:
        detail["allowances"].append("high_quality_news_with_invalidation")
    if bool(control.get("allow_with_strong_market_confirmation", True)) and strong_confirmation and has_invalidation:
        detail["allowances"].append("strong_market_confirmation_with_invalidation")
    if bool(control.get("allow_with_mature_alpha", True)) and mature_alpha and has_invalidation:
        detail["allowances"].append("mature_alpha_with_invalidation")

    risky_daily_mismatch = (
        bool(control.get("block_watch_for_trigger_medium_in_choppy", True))
        and scorecard_state in {"watch_for_trigger", "unknown", ""}
        and (regime in regimes or any(token in regime for token in ("choppy", "range")))
        and horizon in horizons
        and (
            "missing_short_timing_confirmation" in fail_reasons
            or "no_tradeable_candidate_state" in scorecard_failures
            or "weak_entry_setup_quality" in scorecard_failures
            or not has_tradeable_support
        )
    )
    if risky_daily_mismatch and not detail["allowances"]:
        detail["decision"] = "watch_for_trigger"
        detail["reason"] = "daily_tradeability_requires_short_timing"
        detail["failures"] = sorted(set([
            "watch_for_trigger_medium_or_long",
            "choppy_or_range_regime",
            "missing_daily_timing_or_tradeable_support",
            *fail_reasons,
            *scorecard_failures,
        ]))
    return detail


def _mature_alpha_policy_records(adaptive_policy_state: list | None, side: str) -> list[dict]:
    records: list[dict] = []
    for row in adaptive_policy_state or []:
        if not isinstance(row, dict):
            continue
        policy_type = str(row.get("policy_type") or "")
        policy_action = str(row.get("policy_action") or "").lower()
        row_side = str(row.get("side") or "*").lower()
        if row_side not in {"*", side}:
            continue
        if policy_action not in {"protect", "allow"}:
            continue
        if policy_type == "alpha_promotion" or policy_type.startswith("learning_mechanism:"):
            records.append(row)
    return records


def _fast_candidate_alpha_records(adaptive_policy_state: list | None, side: str) -> list[dict]:
    records: list[dict] = []
    for row in adaptive_policy_state or []:
        if not isinstance(row, dict):
            continue
        row_side = str(row.get("side") or "*").lower()
        if row_side not in {"*", side}:
            continue
        if str(row.get("policy_type") or "") == "fast_candidate_alpha" and str(row.get("policy_action") or "").lower() in {"probe", "watchlist"}:
            records.append(row)
    return records


def _safe_policy_int(value, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _apply_mature_alpha_release_control(
    *,
    ticker: str,
    position_ratio: float,
    current_ratio: float,
    analyst_signals: list,
    market_confirmation: dict,
    full_config: dict,
    adaptive_policy_state: list | None,
    max_position_ratio: float,
    margin_rate: float,
) -> tuple[float, list[str], list[str], dict]:
    """Let verified same-scope alpha increase exposure modestly, never by itself."""
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    control = (_get_holding_rebalance_config(full_config).get("mature_alpha_release") or {})
    if not bool(control.get("enabled", True)):
        return position_ratio, reasons, notes, diagnostics
    if not _is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, reasons, notes, diagnostics

    target_side = _target_side_from_ratio(position_ratio)
    if target_side not in {"long", "short"}:
        return position_ratio, reasons, notes, diagnostics

    confirmation_score = _safe_float((market_confirmation or {}).get("confirmation_score"), 0.0)
    state_summary = _side_opportunity_state_summary(analyst_signals, target_side)
    has_invalidation, has_position_exit_boundary = _target_execution_lifecycle_boundaries(
        analyst_signals or [],
        target_side,
    )
    policy_records = _mature_alpha_policy_records(adaptive_policy_state, target_side)
    fast_candidate_records = _fast_candidate_alpha_records(adaptive_policy_state, target_side)
    min_confidence = _safe_float(control.get("min_policy_confidence"), 0.60)
    min_samples = int(control.get("min_policy_samples", 5) or 5)
    eligible_records = [
        row
        for row in policy_records
        if _safe_float(row.get("confidence_score"), 0.0) >= min_confidence
        and _safe_policy_int(row.get("sample_count"), 0) >= min_samples
    ]
    min_confirmation = _safe_float(control.get("min_confirmation_score"), 0.60)
    gate_failures: list[str] = []
    if confirmation_score < min_confirmation:
        gate_failures.append("market_confirmation_below_release_threshold")
    if bool(control.get("require_tradeable_support", True)) and not (
        state_summary.get("has_tradeable_support") or state_summary.get("has_probe_candidate_support")
    ):
        gate_failures.append("no_tradeable_candidate_support")
    if bool(control.get("require_invalidation", True)) and not has_invalidation:
        gate_failures.append("missing_invalidation_boundary")
    if not has_position_exit_boundary:
        gate_failures.append("missing_position_exit_boundary")
    if not eligible_records:
        gate_failures.append("no_eligible_mature_alpha_policy")

    diagnostics["mature_alpha_release"] = {
        "enabled": True,
        "target_side": target_side,
        "confirmation_score": confirmation_score,
        "min_confirmation_score": min_confirmation,
        "has_invalidation": bool(has_invalidation),
        "has_position_exit_boundary": bool(has_position_exit_boundary),
        "opportunity_state_summary": state_summary,
        "policy_count": len(policy_records),
        "eligible_policy_count": len(eligible_records),
        "fast_candidate_alpha_count": len(fast_candidate_records),
        "gate_failures": gate_failures,
        "pre_control_ratio": float(position_ratio),
        "current_ratio": float(current_ratio),
    }
    if gate_failures:
        diagnostics["mature_alpha_release"]["decision"] = "no_release"
        return position_ratio, reasons, notes, diagnostics

    multiplier = max(1.0, _safe_float(control.get("release_multiplier"), 1.20))
    min_ratio = max(0.0, _safe_float(control.get("min_target_ratio"), 0.015))
    max_ratio = max(min_ratio, _safe_float(control.get("max_target_ratio"), 0.045))
    desired_abs = max(abs(position_ratio) * multiplier, min_ratio)
    desired_abs = min(desired_abs, max_ratio, max_position_ratio)
    hard_margin_cap = get_hard_allocation_margin_ratio(full_config)
    if margin_rate > 0:
        desired_abs = min(desired_abs, hard_margin_cap / margin_rate)
    before = position_ratio
    released = _signed_abs(target_side, desired_abs)
    if abs(released) <= abs(position_ratio) + 1e-12:
        diagnostics["mature_alpha_release"]["decision"] = "already_sized"
        diagnostics["mature_alpha_release"]["final_ratio"] = float(position_ratio)
        return position_ratio, reasons, notes, diagnostics

    position_ratio = released
    reasons.append("mature_alpha_release")
    notes.append(
        f"{ticker} {target_side} mature alpha released target ratio: "
        f"{before:.2%}->{position_ratio:.2%}; "
        f"eligible_policies={len(eligible_records)}, confirmation_score={confirmation_score:.2f}"
    )
    diagnostics["mature_alpha_release"].update({
        "decision": "released",
        "release_multiplier": multiplier,
        "min_target_ratio": min_ratio,
        "max_target_ratio": max_ratio,
        "final_ratio": float(position_ratio),
        "policy_refs": [_compact_policy_row(row) for row in eligible_records[:5]],
        "hard_margin_cap": hard_margin_cap,
    })
    return position_ratio, reasons, notes, diagnostics


def _apply_fast_candidate_alpha_probe_control(
    *,
    ticker: str,
    position_ratio: float,
    current_ratio: float,
    analyst_signals: list,
    market_confirmation: dict,
    full_config: dict,
    adaptive_policy_state: list | None,
    max_position_ratio: float,
    margin_rate: float,
) -> tuple[float, list[str], list[str], dict]:
    """Let missed-alpha candidates get a tiny current-confirmed probe, not a full position."""
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    control = (_get_holding_rebalance_config(full_config).get("fast_candidate_alpha_probe") or {})
    if not bool(control.get("enabled", True)):
        return position_ratio, reasons, notes, diagnostics
    if current_ratio != 0 or abs(position_ratio) > 1e-12:
        return position_ratio, reasons, notes, diagnostics

    long_summary = _side_opportunity_state_summary(analyst_signals, "long")
    short_summary = _side_opportunity_state_summary(analyst_signals, "short")
    long_records = _fast_candidate_alpha_records(adaptive_policy_state, "long")
    short_records = _fast_candidate_alpha_records(adaptive_policy_state, "short")
    min_policy_confidence = _safe_float(control.get("min_policy_confidence"), 0.50)
    long_records = [row for row in long_records if _safe_float(row.get("confidence_score"), 0.0) >= min_policy_confidence]
    short_records = [row for row in short_records if _safe_float(row.get("confidence_score"), 0.0) >= min_policy_confidence]
    confirmation_score = _safe_float((market_confirmation or {}).get("confirmation_score"), 0.0)
    min_confirmation = _safe_float(control.get("min_confirmation_score"), 0.58)

    candidates: list[tuple[str, list[dict], dict]] = []
    if long_records:
        candidates.append(("long", long_records, long_summary))
    if short_records:
        candidates.append(("short", short_records, short_summary))

    diagnostics["fast_candidate_alpha_probe"] = {
        "enabled": True,
        "confirmation_score": confirmation_score,
        "min_confirmation_score": min_confirmation,
        "long_candidate_count": len(long_records),
        "short_candidate_count": len(short_records),
        "long_state_summary": long_summary,
        "short_state_summary": short_summary,
        "pre_control_ratio": float(position_ratio),
        "current_ratio": float(current_ratio),
    }
    if not candidates:
        diagnostics["fast_candidate_alpha_probe"]["decision"] = "no_candidate"
        return position_ratio, reasons, notes, diagnostics

    chosen: tuple[str, list[dict], dict] | None = None
    for side, records, summary in candidates:
        has_invalidation, has_position_exit_boundary = _target_execution_lifecycle_boundaries(
            analyst_signals or [],
            side,
        )
        gate_failures: list[str] = []
        if confirmation_score < min_confirmation:
            gate_failures.append("market_confirmation_below_probe_threshold")
        if bool(control.get("require_tradeable_support", True)) and not (
            summary.get("has_tradeable_support") or summary.get("has_probe_candidate_support")
        ):
            gate_failures.append("no_tradeable_candidate_support")
        if bool(control.get("require_invalidation", True)) and not has_invalidation:
            gate_failures.append("missing_invalidation_boundary")
        if not has_position_exit_boundary:
            gate_failures.append("missing_position_exit_boundary")
        diagnostics["fast_candidate_alpha_probe"][f"{side}_entry_invalidation"] = bool(
            has_invalidation
        )
        diagnostics["fast_candidate_alpha_probe"][f"{side}_position_exit_boundary"] = bool(
            has_position_exit_boundary
        )
        if not gate_failures:
            if chosen is None or len(records) > len(chosen[1]):
                chosen = (side, records, summary)
    if chosen is None:
        diagnostics["fast_candidate_alpha_probe"]["decision"] = "gate_failed"
        return position_ratio, reasons, notes, diagnostics

    side, records, summary = chosen
    probe_ratio = max(0.0, _safe_float(control.get("probe_ratio"), 0.012))
    probe_ratio = min(probe_ratio, max_position_ratio)
    hard_margin_cap = get_hard_allocation_margin_ratio(full_config)
    if margin_rate > 0:
        probe_ratio = min(probe_ratio, hard_margin_cap / margin_rate)
    if probe_ratio <= 1e-12:
        diagnostics["fast_candidate_alpha_probe"]["decision"] = "zero_after_caps"
        return position_ratio, reasons, notes, diagnostics

    position_ratio = _signed_abs(side, probe_ratio)
    reasons.append("fast_candidate_alpha_probe")
    notes.append(
        f"{ticker} {side} fast candidate alpha probe: target={position_ratio:.2%}; "
        f"candidates={len(records)}, confirmation_score={confirmation_score:.2f}"
    )
    diagnostics["fast_candidate_alpha_probe"].update({
        "decision": "probe",
        "target_side": side,
        "final_ratio": float(position_ratio),
        "policy_refs": [_compact_policy_row(row) for row in records[:5]],
        "opportunity_state_summary": summary,
        "hard_margin_cap": hard_margin_cap,
    })
    return position_ratio, reasons, notes, diagnostics


def _has_short_timing_support(
    *,
    side: str,
    technical_payload: dict | None,
    news_payload: dict | None,
    market_confirmation: dict | None,
    control: dict,
) -> bool:
    if side not in {"long", "short"}:
        return False
    min_confidence = float(control.get("min_short_timing_confidence", 0.45))
    min_confirmation = float(control.get("min_confirmation_score", 0.55))
    confirmation_score = _safe_float((market_confirmation or {}).get("confirmation_score"), 0.0)
    technical_ok = _payload_supports_side(technical_payload, side, min_confidence)
    news_ok = (
        _payload_supports_side(news_payload, side, min_confidence)
        and str((news_payload or {}).get("horizon_class", "")).lower() in {"short", "event_short", "unknown"}
    )
    return bool((technical_ok or news_ok) and confirmation_score >= min_confirmation)


def _horizon_consistency_result(
    *,
    side: str,
    target_lots_hint: int,
    analyst_signals: list,
    technical_payload: dict | None,
    news_payload: dict | None,
    market_confirmation: dict | None,
    control: dict,
    has_entry_invalidation: bool,
    has_position_exit_boundary: bool,
    lifecycle: str,
) -> dict:
    side_signal = "Bullish" if side == "long" else "Bearish" if side == "short" else "Neutral"
    matching_horizons = []
    for signal in analyst_signals or []:
        if _signal_to_text(getattr(signal, "signal", "Neutral")) != side_signal:
            continue
        analyst_horizon = str(getattr(signal, "analyst_horizon", "") or "").lower()
        horizon_class = str(getattr(signal, "horizon_class", "") or "").lower()
        matching_horizons.append(horizon_class if analyst_horizon in {"", "unknown"} else analyst_horizon)
    decision_horizon = _resolve_decision_horizon(analyst_signals, target_lots_hint)
    if str(decision_horizon).lower() in {"unknown", "flat"}:
        if "medium" in matching_horizons:
            decision_horizon = "medium"
        elif "long" in matching_horizons:
            decision_horizon = "long"
        elif "event_short" in matching_horizons:
            decision_horizon = "event_short"
        elif "short" in matching_horizons:
            decision_horizon = "short"
    detail = {
        "enabled": bool(control.get("enabled", True)),
        "decision_horizon": decision_horizon,
        "requires_short_timing": False,
        "short_timing_support": False,
        "lifecycle": str(lifecycle or "new_entry"),
        "has_entry_invalidation": bool(has_entry_invalidation),
        "has_position_exit_boundary": bool(has_position_exit_boundary),
        "fail_reasons": [],
    }
    if not detail["enabled"] or side not in {"long", "short"}:
        detail["passed"] = True
        return detail

    requires_short_timing = (
        bool(control.get("medium_requires_short_timing", True))
        and str(decision_horizon).lower() in {"medium", "long"}
    )
    detail["requires_short_timing"] = bool(requires_short_timing)
    short_timing_support = _has_short_timing_support(
        side=side,
        technical_payload=technical_payload,
        news_payload=news_payload,
        market_confirmation=market_confirmation,
        control=control,
    )
    detail["short_timing_support"] = bool(short_timing_support)
    if requires_short_timing and not short_timing_support:
        detail["fail_reasons"].append("missing_short_timing_confirmation")
    if requires_short_timing and bool(control.get("medium_requires_invalidation", True)):
        if detail["lifecycle"] == "new_entry" and not has_entry_invalidation:
            detail["fail_reasons"].append("missing_entry_invalidation_boundary")
        if not has_position_exit_boundary:
            detail["fail_reasons"].append("missing_position_exit_boundary")
    detail["passed"] = not detail["fail_reasons"]
    return detail


def _apply_directional_override(
    *,
    ticker: str,
    position_risk: PositionRisk,
    long_scores: dict,
    short_scores: dict,
    max_position_ratio: float,
    risk_level: RiskLevel,
    full_config: dict,
) -> tuple[list[str], list[str], dict]:
    """Let high-quality blended bearish evidence create a small short probe."""
    control = full_config.get("directional_override_control", {}) or {}
    if not control.get("enabled", True):
        return [], [], {}

    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    long_strength = _safe_float(long_scores.get("score"), 0.0) * _safe_float(long_scores.get("confidence"), 0.0)
    short_strength = _safe_float(short_scores.get("score"), 0.0) * _safe_float(short_scores.get("confidence"), 0.0)
    diagnostics["directional_override"] = {
        "long_strength": float(long_strength),
        "short_strength": float(short_strength),
        "raw_position_ratio": float(position_risk.optimal_position_ratio or 0.0),
    }

    if not bool(control.get("allow_high_quality_short", True)):
        return reasons, notes, diagnostics
    if position_risk.optimal_position_ratio < 0:
        return reasons, notes, diagnostics

    min_score = float(control.get("min_short_score", 0.45))
    min_confidence = float(control.get("min_short_confidence", 0.55))
    min_strength = float(control.get("min_short_strength", 0.30))
    min_edge = float(control.get("min_short_edge", 0.08))
    if (
        _safe_float(short_scores.get("score"), 0.0) < min_score
        or _safe_float(short_scores.get("confidence"), 0.0) < min_confidence
        or short_strength < min_strength
        or short_strength - long_strength < min_edge
    ):
        diagnostics["directional_override"]["decision"] = "skip_short_threshold"
        return reasons, notes, diagnostics

    blended_ratio, blended_direction = calculate_position_ratio_with_balance(
        ticker=ticker,
        long_scores=long_scores,
        short_scores=short_scores,
        max_position_ratio=max_position_ratio,
        risk_level=risk_level,
        full_config=full_config,
    )
    if blended_direction != "SHORT" or blended_ratio <= 0:
        diagnostics["directional_override"]["decision"] = "skip_non_short_blend"
        return reasons, notes, diagnostics

    max_probe_ratio = float(control.get("short_probe_max_ratio", 0.03))
    before = float(position_risk.optimal_position_ratio or 0.0)
    position_risk.optimal_position_ratio = -min(abs(blended_ratio), max_probe_ratio, max_position_ratio)
    reasons.append("high_quality_bearish_short_probe")
    notes.append(
        f"{ticker} high-quality bearish blend overrides non-short target: "
        f"{before:.2%}->{position_risk.optimal_position_ratio:.2%}, "
        f"short_strength={short_strength:.2f}, long_strength={long_strength:.2f}"
    )
    position_risk.justification += (
        f"\n[Directional override: high-quality bearish blend created a small SHORT probe; "
        f"short_strength={short_strength:.2f}, long_strength={long_strength:.2f}]"
    )
    diagnostics["directional_override"]["decision"] = "short_probe"
    diagnostics["directional_override"]["final_position_ratio"] = float(position_risk.optimal_position_ratio)
    return reasons, notes, diagnostics


def _apply_holding_rebalance_control(
    *,
    ticker: str,
    trading_date,
    position_ratio: float,
    current_ratio: float,
    current_position,
    analyst_signals: list,
    long_scores: dict,
    short_scores: dict,
    market_confirmation: dict,
    full_config: dict,
    fusion_context: dict,
    risk_level: RiskLevel,
    adaptive_policy_state: list | None = None,
    prior_control_reasons: list[str] | None = None,
    opening_fac_context: dict | None = None,
    current_price: float = 0.0,
) -> tuple[float, list[str], list[str], dict]:
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    prior_reasons = {str(reason) for reason in (prior_control_reasons or [])}
    control = _get_holding_rebalance_config(full_config)
    if not control.get("enabled", True):
        return position_ratio, reasons, notes, diagnostics

    current_side = _target_side_from_ratio(current_ratio)
    target_side = _target_side_from_ratio(position_ratio)
    calibration_side = target_side if target_side in {"long", "short"} else current_side
    calibration_horizon = _resolve_decision_horizon(
        analyst_signals,
        1 if calibration_side == "long" else -1 if calibration_side == "short" else 0,
    )
    calibration_regime = _market_regime_from_signals(analyst_signals, calibration_side)
    contextual_cfg = (((full_config.get("learning") or {}).get("contextual_rule_calibration")) or {})
    contextual_diag = {}
    if bool(contextual_cfg.get("enabled", True)) and calibration_side in {"long", "short"}:
        control, contextual_diag = apply_pm_contextual_calibration(
            control,
            adaptive_policy_state or [],
            ticker=ticker,
            side=calibration_side,
            horizon_class=calibration_horizon,
            market_regime=calibration_regime,
            min_confidence=float(contextual_cfg.get("min_confidence", 0.35) or 0.35),
        )
    sector = _sector_for_ticker(ticker)
    min_hold_days = int((control.get("min_hold_days_by_sector") or {}).get(sector, 4))
    min_rebalance_ratio = float(control.get("min_rebalance_ratio", 0.008))
    min_new_entry_ratio = float(control.get("min_new_entry_ratio", 0.004))
    min_support_confidence = float(control.get("min_signal_support_confidence", 0.35))
    confirmation_score = _safe_float(market_confirmation.get("confirmation_score"), 0.0)
    payloads = _analyst_signal_payloads(analyst_signals, fusion_context)
    fundamental_payload = payloads.get("fundamental")
    technical_payload = payloads.get("technical")
    news_payload = payloads.get("commodity_news")
    lifecycle_config = control.get("position_lifecycle") or {}
    horizon_config = control.get("horizon_consistency") or {}
    has_entry_invalidation_boundary = _has_structured_invalidation_condition(
        analyst_signals,
        target_side=calibration_side,
    )
    has_current_position_exit_boundary = _has_position_exit_boundary(
        analyst_signals,
        target_side=calibration_side,
    )

    current_lots = int(getattr(current_position, "shares", 0) or 0) if current_position else 0
    opening_context = opening_fac_context if isinstance(opening_fac_context, dict) else {}
    held_days = (
        _safe_int(opening_context.get("held_trading_days"), 0)
        if current_position and current_lots != 0 and opening_context
        else _days_held(getattr(current_position, "entry_date", None), trading_date)
        if current_position and current_lots != 0
        else None
    )
    position_pnl_ratio = _position_pnl_ratio(current_position)

    diagnostics = {
        "holding_rebalance_control": {
            "enabled": True,
            "sector": sector,
            "held_days": held_days,
            "min_hold_days": min_hold_days,
            "current_side": current_side,
            "raw_target_side": target_side,
            "current_ratio": float(current_ratio),
            "raw_target_ratio": float(position_ratio),
            "position_pnl_ratio": float(position_pnl_ratio),
            "confirmation_score": float(confirmation_score),
            "min_rebalance_ratio": float(min_rebalance_ratio),
            "min_new_entry_ratio": float(min_new_entry_ratio),
            "has_entry_invalidation_boundary": bool(has_entry_invalidation_boundary),
            "has_current_position_exit_boundary": bool(
                has_current_position_exit_boundary
            ),
            "opening_recommendation_id": opening_context.get("recommendation_id"),
            "opening_expected_horizon_days": opening_context.get("expected_horizon_days"),
            "opening_position_invalidation_level": opening_context.get(
                "position_invalidation_level"
            ),
            "contextual_rule_calibration": contextual_diag,
        }
    }
    detail = diagnostics["holding_rebalance_control"]

    if risk_level in (RiskLevel.DANGER, RiskLevel.EMERGENCY):
        detail["decision"] = "skip_for_risk_state"
        return position_ratio, reasons, notes, diagnostics

    if current_side == "flat":
        watch_for_trigger_cfg = control.get("watch_for_trigger_new_entry") or {}
        watch_for_trigger_audit = {
            "config_key": "portfolio_manager.holding_rebalance_control.watch_for_trigger_new_entry",
            "audit_name": str(watch_for_trigger_cfg.get("audit_name") or "watch_for_trigger_observation_candidate"),
            "semantic_role": str(watch_for_trigger_cfg.get("semantic_role") or "observation_candidate_only"),
            "allow_probe": bool(watch_for_trigger_cfg.get("allow_probe", False)),
            "can_create_trade_authority": bool(watch_for_trigger_cfg.get("can_create_trade_authority", False)),
            "requires_final_contract_authority": bool(
                watch_for_trigger_cfg.get("requires_final_contract_authority", True)
            ),
            "boundary": (
                "direction-only can only seed an audited candidate intent; final new-entry "
                "final_action_contract authority decides whether lots can reach Trader"
            ),
        }
        detail["watch_for_trigger_new_entry_audit"] = watch_for_trigger_audit
        scorecard_side = {}
        state_summary = {}
        new_entry_horizon = None
        if target_side in {"long", "short"} and bool(watch_for_trigger_cfg.get("enabled", True)):
            state_summary = _side_opportunity_state_summary(analyst_signals, target_side)
            scorecard = fusion_context.get("opportunity_scorecard") if isinstance(fusion_context, dict) else {}
            scorecard_side = (
                scorecard.get(target_side)
                if isinstance(scorecard, dict) and isinstance(scorecard.get(target_side), dict)
                else {}
            )
            scorecard_state = str(scorecard_side.get("final_state") or "").lower()
            detail["opportunity_state_summary"] = state_summary
            detail["opportunity_scorecard_side"] = scorecard_side
            if scorecard_state == "no_opportunity":
                reasons.append("pm_opportunity_scorecard_no_trade")
                notes.append(
                    f"{ticker} new {target_side} entry skipped by opportunity scorecard: "
                    f"score={scorecard_side.get('score')}, failures={scorecard_side.get('gating_failures')}"
                )
                detail["decision"] = "skip_scorecard_no_trade"
                detail["final_target_ratio"] = 0.0
                return 0.0, reasons, notes, diagnostics
            if not state_summary.get("has_tradeable_support") or scorecard_state == "watch_for_trigger":
                if bool(watch_for_trigger_cfg.get("allow_probe", False)):
                    probe_cap = max(0.0, float(watch_for_trigger_cfg.get("probe_max_ratio", 0.01) or 0.01))
                    probe_floor = max(0.0, float(watch_for_trigger_cfg.get("probe_floor_ratio", 0.005) or 0.005))
                    capped_ratio = _probe_ratio_from_soft_gate(
                        side=target_side,
                        current_ratio=current_ratio,
                        raw_ratio=position_ratio,
                        cap_ratio=probe_cap,
                        floor_ratio=probe_floor,
                    )
                    reasons.append("pm_watch_for_trigger_probe_cap")
                    notes.append(
                        f"{ticker} new {target_side} direction-only setup capped to probe: "
                        f"{position_ratio:.2%}->{capped_ratio:.2%}; "
                        f"states={state_summary.get('opportunity_state_counts')}, scorecard={scorecard_side.get('final_state')}"
                    )
                    position_ratio = capped_ratio
                    target_side = _target_side_from_ratio(position_ratio)
                    detail["watch_for_trigger_probe_cap"] = probe_cap
                    detail["watch_for_trigger_probe_floor"] = probe_floor
                    detail["raw_target_ratio_after_watch_for_trigger_gate"] = float(position_ratio)
                else:
                    reasons.append("pm_watch_for_trigger_not_tradeable")
                    notes.append(
                        f"{ticker} new {target_side} entry skipped: analyst support is direction-only; "
                        f"states={state_summary.get('opportunity_state_counts')}"
                    )
                    detail["decision"] = "skip_watch_for_trigger_new_entry"
                    detail["final_target_ratio"] = 0.0
                    return 0.0, reasons, notes, diagnostics
        if target_side in {"long", "short"} and bool(horizon_config.get("apply_to_new_entries", True)):
            new_entry_horizon = _horizon_consistency_result(
                side=target_side,
                target_lots_hint=1 if target_side == "long" else -1,
                analyst_signals=analyst_signals,
                technical_payload=technical_payload,
                news_payload=news_payload,
                market_confirmation=market_confirmation,
                control=horizon_config,
                has_entry_invalidation=has_entry_invalidation_boundary,
                has_position_exit_boundary=has_current_position_exit_boundary,
                lifecycle="new_entry",
            )
            detail["horizon_consistency"] = new_entry_horizon
            if not new_entry_horizon.get("passed", True):
                probe_cap = max(0.0, float(horizon_config.get("mismatch_probe_max_ratio", 0.008) or 0.008))
                probe_floor = max(0.0, float(horizon_config.get("mismatch_probe_floor_ratio", 0.004) or 0.004))
                if bool(horizon_config.get("allow_mismatch_probe", True)) and probe_cap > 0:
                    capped_ratio = _probe_ratio_from_soft_gate(
                        side=target_side,
                        current_ratio=current_ratio,
                        raw_ratio=position_ratio,
                        cap_ratio=probe_cap,
                        floor_ratio=probe_floor,
                    )
                    reasons.append("horizon_consistency_probe_cap")
                    notes.append(
                        f"{ticker} new {target_side} horizon mismatch capped to probe: "
                        f"{position_ratio:.2%}->{capped_ratio:.2%}; "
                        f"horizon={new_entry_horizon.get('decision_horizon')}, "
                        f"failures={new_entry_horizon.get('fail_reasons', [])}"
                    )
                    position_ratio = capped_ratio
                    target_side = _target_side_from_ratio(position_ratio)
                    detail["horizon_mismatch_probe_cap"] = probe_cap
                    detail["horizon_mismatch_probe_floor"] = probe_floor
                    detail["raw_target_ratio_after_horizon_gate"] = float(position_ratio)
                else:
                    reasons.append("horizon_consistency_requires_short_timing")
                    notes.append(
                        f"{ticker} new {target_side} entry skipped: horizon={new_entry_horizon.get('decision_horizon')} "
                        f"requires short timing and invalidation; failures={new_entry_horizon.get('fail_reasons', [])}"
                    )
                    detail["decision"] = "skip_horizon_mismatch_new_entry"
                    detail["final_target_ratio"] = 0.0
                    return 0.0, reasons, notes, diagnostics
        if target_side in {"long", "short"}:
            daily_gate = _daily_tradeability_gate_result(
                side=target_side,
                current_ratio=current_ratio,
                analyst_signals=analyst_signals,
                technical_payload=technical_payload,
                news_payload=news_payload,
                market_confirmation=market_confirmation,
                opportunity_scorecard_side=scorecard_side,
                state_summary=state_summary,
                horizon_result=new_entry_horizon,
                has_invalidation=has_entry_invalidation_boundary,
                adaptive_policy_state=adaptive_policy_state,
                control=control.get("daily_tradeability_gate") or {},
            )
            detail["daily_tradeability_gate"] = daily_gate
            if daily_gate.get("decision") == "watch_for_trigger":
                reasons.append("daily_tradeability_watchlist_only")
                notes.append(
                    f"{ticker} new {target_side} kept on watchlist: medium/long direction-only idea "
                    f"in {daily_gate.get('market_regime')} lacks daily timing/confirmation; "
                    f"failures={daily_gate.get('failures', [])}"
                )
                detail["decision"] = "watchlist_daily_tradeability_gate"
                detail["final_target_ratio"] = 0.0
                return 0.0, reasons, notes, diagnostics
        controlled_probe = _has_controlled_probe_reason(reasons)
        hard_zero = any(_hard_zero_reason(reason) for reason in reasons)
        if target_side in {"long", "short"} and abs(position_ratio) < min_new_entry_ratio and (not controlled_probe or hard_zero):
            reasons.append("minimum_new_entry_threshold")
            notes.append(
                f"{ticker} new {target_side} entry skipped: target ratio "
                f"{position_ratio:.2%} below {min_new_entry_ratio:.2%}"
            )
            detail["decision"] = "skip_small_new_entry"
            detail["final_target_ratio"] = 0.0
            return 0.0, reasons, notes, diagnostics
        if target_side in {"long", "short"} and abs(position_ratio) < min_new_entry_ratio and controlled_probe and not hard_zero:
            reasons.append("controlled_probe_below_min_entry_kept")
            notes.append(
                f"{ticker} new {target_side} controlled probe kept below normal min entry: "
                f"target ratio {position_ratio:.2%} < {min_new_entry_ratio:.2%}; "
                "final lot conversion still requires margin, net exposure, and execution feasibility"
            )
            detail["controlled_probe_below_min_entry_kept"] = True
            detail["normal_min_new_entry_ratio"] = float(min_new_entry_ratio)
        detail["decision"] = "no_existing_position"
        detail["final_target_ratio"] = float(position_ratio)
        return position_ratio, reasons, notes, diagnostics

    opposite_side = "short" if current_side == "long" else "long"
    reversal_side = opposite_side
    target_strength = _side_signal_strength(reversal_side, long_scores, short_scores)
    current_strength = _side_signal_strength(current_side, long_scores, short_scores)
    fundamental_supports_current = _fundamental_anchor_supports(fundamental_payload, current_side, control)
    fundamental_supports_target = _fundamental_anchor_supports(fundamental_payload, reversal_side, control)
    fundamental_hold_anchor_current = _hold_anchor_supports_side(fundamental_payload, current_side, lifecycle_config)
    news_hold_anchor_current = _hold_anchor_supports_side(news_payload, current_side, lifecycle_config)
    technical_supports_current = _payload_supports_side(technical_payload, current_side, min_support_confidence)
    technical_supports_target = _payload_supports_side(technical_payload, reversal_side, min_support_confidence)
    news_supports_current = _news_high_quality_override(news_payload, current_side, control)
    news_override = _news_high_quality_override(news_payload, reversal_side, control)
    signal_counts_current = _side_signal_counts(payloads, current_side)
    opening_position_invalidation_breached = _opening_fac_position_invalidation_breached(
        opening_fac_context=opening_context,
        ticker=ticker,
        current_side=current_side,
        current_price=current_price,
        current_position=current_position,
        full_config=full_config,
    )
    technical_invalidation_confirmed = bool(
        opening_position_invalidation_breached
        or (
            technical_supports_target
            and not technical_supports_current
            and signal_counts_current.get("opposite", 0) > 0
        )
    )
    fundamental_medium_opposition = bool(
        fundamental_supports_target and not fundamental_supports_current
    )
    expected_horizon_days = _safe_int(opening_context.get("expected_horizon_days"), 0)
    opening_horizon_due = bool(
        expected_horizon_days > 0
        and held_days is not None
        and held_days >= expected_horizon_days
    )
    explicit_lifecycle_break = bool(
        technical_invalidation_confirmed or fundamental_medium_opposition
    )
    current_horizon_consistency = _horizon_consistency_result(
        side=current_side,
        target_lots_hint=1 if current_side == "long" else -1,
        analyst_signals=analyst_signals,
        technical_payload=technical_payload,
        news_payload=news_payload,
        market_confirmation=market_confirmation,
        control=horizon_config,
        has_entry_invalidation=True,
        has_position_exit_boundary=bool(
            has_current_position_exit_boundary
            or _safe_float(opening_context.get("position_invalidation_level"), 0.0) > 0.0
            or _safe_float(opening_context.get("atr_stop_distance"), 0.0) > 0.0
        ),
        lifecycle="holding",
    )

    lifecycle_enabled = bool(lifecycle_config.get("enabled", True))
    loss_revalidated = _lifecycle_revalidated_by_current_evidence(
        current_side=current_side,
        current_strength=current_strength,
        confirmation_score=confirmation_score,
        fundamental_supports_current=fundamental_supports_current,
        technical_supports_current=technical_supports_current,
        news_supports_current=news_supports_current,
        lifecycle_config=lifecycle_config,
    )
    trend_position = (
        lifecycle_enabled
        and position_pnl_ratio >= float(lifecycle_config.get("trend_min_profit_ratio", 0.03))
        and confirmation_score >= float(lifecycle_config.get("trend_min_confirmation_score", 0.60))
        and (
            fundamental_supports_current
            or technical_supports_current
            or news_supports_current
            or current_strength >= float(lifecycle_config.get("trend_min_signal_strength", 0.30))
        )
    )
    failed_position = (
        lifecycle_enabled
        and position_pnl_ratio <= float(lifecycle_config.get("failed_loss_ratio", -0.05))
        and not fundamental_supports_current
        and confirmation_score <= float(lifecycle_config.get("failed_max_confirmation_score", 0.45))
        and explicit_lifecycle_break
    )
    loss_revalidation_due = (
        lifecycle_enabled
        and held_days is not None
        and held_days >= int(lifecycle_config.get("loss_revalidation_min_hold_days", 1))
        and position_pnl_ratio <= float(lifecycle_config.get("loss_revalidation_ratio", -0.02))
        and not trend_position
    )
    loss_revalidation_failed = (
        loss_revalidation_due
        and not loss_revalidated
        and explicit_lifecycle_break
    )
    new_loss_revalidation_due = (
        lifecycle_enabled
        and bool(lifecycle_config.get("new_loss_revalidation_enabled", True))
        and current_side in {"long", "short"}
        and held_days is not None
        and held_days <= int(lifecycle_config.get("new_loss_revalidation_max_hold_days", 2))
        and position_pnl_ratio <= float(lifecycle_config.get("new_loss_revalidation_ratio", -0.005))
        and not trend_position
    )
    new_loss_failures: list[str] = []
    if new_loss_revalidation_due:
        min_new_loss_confirmation = float(
            lifecycle_config.get(
                "new_loss_revalidation_min_confirmation_score",
                lifecycle_config.get("loss_revalidation_min_confirmation_score", 0.55),
            )
        )
        min_new_loss_strength = float(
            lifecycle_config.get(
                "new_loss_revalidation_min_signal_strength",
                lifecycle_config.get("loss_revalidation_min_signal_strength", 0.25),
            )
        )
        if signal_counts_current.get("same", 0) <= 0 or not technical_supports_current:
            new_loss_failures.append("current_signal_neutral_or_absent")
        if signal_counts_current.get("opposite", 0) > 0:
            new_loss_failures.append("analyst_conflict")
        if current_strength < min_new_loss_strength or confirmation_score < min_new_loss_confirmation:
            new_loss_failures.append("insufficient_same_day_evidence")
        if not current_horizon_consistency.get("has_position_exit_boundary"):
            new_loss_failures.append("missing_position_exit_boundary")
        if bool(horizon_config.get("apply_to_losing_holds", True)) and not current_horizon_consistency.get("passed", True):
            new_loss_failures.extend(
                f"horizon_{reason}" for reason in current_horizon_consistency.get("fail_reasons", [])
            )
    new_loss_revalidated = new_loss_revalidation_due and not new_loss_failures
    new_loss_revalidation_failed = (
        new_loss_revalidation_due
        and bool(new_loss_failures)
        and not loss_revalidated
    )
    new_loss_revalidation_exit = (
        new_loss_revalidation_failed
        and position_pnl_ratio <= float(lifecycle_config.get("new_loss_revalidation_exit_ratio", -0.02))
    )
    scorecard = fusion_context.get("opportunity_scorecard") if isinstance(fusion_context, dict) else {}
    current_scorecard = (
        scorecard.get(current_side)
        if isinstance(scorecard, dict) and isinstance(scorecard.get(current_side), dict)
        else {}
    )
    current_state = str(current_scorecard.get("final_state") or "").lower()
    current_state_failures = [str(item) for item in (current_scorecard.get("gating_failures") or [])]
    exploration_layers = set(
        str(item).lower()
        for item in (lifecycle_config.get("exploration_reconfirm_states") or ["watch_for_trigger", "unknown"])
    )
    exploration_reconfirm_due = (
        lifecycle_enabled
        and bool(lifecycle_config.get("exploration_reconfirm_enabled", True))
        and current_side in {"long", "short"}
        and held_days is not None
        and held_days >= int(lifecycle_config.get("exploration_reconfirm_min_hold_days", 1))
        and not trend_position
        and (
            current_state in exploration_layers
            or "no_tradeable_candidate_state" in current_state_failures
            or "weak_entry_setup_quality" in current_state_failures
        )
    )
    profitable_hold_supported = _profitable_hold_still_supported(
        current_side=current_side,
        position_pnl_ratio=position_pnl_ratio,
        current_strength=current_strength,
        confirmation_score=confirmation_score,
        signal_counts_current=signal_counts_current,
        fundamental_supports_current=fundamental_supports_current or fundamental_hold_anchor_current,
        technical_supports_current=technical_supports_current,
        news_supports_current=news_supports_current or news_hold_anchor_current,
        current_state=current_state,
        lifecycle_config=lifecycle_config,
    )
    exploration_positive_hold = (
        position_pnl_ratio >= float(lifecycle_config.get("exploration_reconfirm_profit_keep_ratio", 0.005))
        and signal_counts_current.get("opposite", 0) <= 0
        and confirmation_score >= float(lifecycle_config.get("exploration_reconfirm_profit_min_confirmation", 0.45))
    )
    exploration_reconfirmed = (
        current_state in {"probe_candidate", "tradeable_candidate"}
        or profitable_hold_supported
        or (
            current_strength >= float(lifecycle_config.get("exploration_reconfirm_min_signal_strength", 0.30))
            and confirmation_score >= float(lifecycle_config.get("exploration_reconfirm_min_confirmation_score", 0.55))
            and signal_counts_current.get("opposite", 0) <= 0
            and (
                technical_supports_current
                or fundamental_supports_current
                or news_supports_current
                or fundamental_hold_anchor_current
                or news_hold_anchor_current
            )
        )
        or exploration_positive_hold
    )
    exploration_reconfirm_failures: list[str] = []
    if exploration_reconfirm_due and not exploration_reconfirmed and explicit_lifecycle_break:
        if current_state not in {"probe_candidate", "tradeable_candidate"}:
            exploration_reconfirm_failures.append(f"layer_{current_state or 'missing'}")
        if confirmation_score < float(lifecycle_config.get("exploration_reconfirm_min_confirmation_score", 0.55)):
            exploration_reconfirm_failures.append("confirmation_low")
        if signal_counts_current.get("opposite", 0) > 0:
            exploration_reconfirm_failures.append("opposite_signal_present")
        if not (
            technical_supports_current
            or fundamental_supports_current
            or news_supports_current
            or fundamental_hold_anchor_current
            or news_hold_anchor_current
        ):
            exploration_reconfirm_failures.append("no_current_support")
        if position_pnl_ratio < float(lifecycle_config.get("exploration_reconfirm_profit_keep_ratio", 0.005)):
            exploration_reconfirm_failures.append("not_profit_protected")
    exploration_reconfirm_exit = (
        exploration_reconfirm_due
        and not exploration_reconfirmed
        and explicit_lifecycle_break
        and (
            position_pnl_ratio <= float(lifecycle_config.get("exploration_reconfirm_exit_ratio", -0.003))
            or confirmation_score <= float(lifecycle_config.get("exploration_reconfirm_exit_confirmation_score", 0.48))
            or "opposite_signal_present" in exploration_reconfirm_failures
        )
    )
    loss_revalidation_exit = (
        loss_revalidation_failed
        and (
            position_pnl_ratio <= float(lifecycle_config.get("loss_revalidation_exit_ratio", -0.04))
            or confirmation_score <= float(lifecycle_config.get("loss_revalidation_exit_confirmation_score", 0.45))
        )
    )
    probe_expired = (
        lifecycle_enabled
        and held_days is not None
        and held_days >= int(lifecycle_config.get("probe_max_hold_days", 2))
        and (expected_horizon_days <= 0 or opening_horizon_due)
        and position_pnl_ratio < float(lifecycle_config.get("probe_min_profit_ratio", 0.01))
        and confirmation_score < float(lifecycle_config.get("probe_min_confirmation_score", 0.55))
        and not trend_position
        and not profitable_hold_supported
        and not fundamental_supports_current
        and not fundamental_hold_anchor_current
    )
    strong_reversal = (
        target_side in {"long", "short"}
        and target_side != current_side
        and target_strength >= float(control.get("reverse_min_signal_strength", 0.55))
        and (
            confirmation_score >= float(control.get("reverse_min_confirmation_score", 0.65))
            or fundamental_supports_target
            or news_override
        )
        and not fundamental_supports_current
        and (fundamental_supports_target or (technical_supports_target and news_override))
    )
    horizon_losing_hold_failed = (
        lifecycle_enabled
        and bool(horizon_config.get("apply_to_losing_holds", True))
        and current_side in {"long", "short"}
        and position_pnl_ratio < 0
        and not current_horizon_consistency.get("passed", True)
        and not trend_position
        and explicit_lifecycle_break
    )
    horizon_losing_hold_exit = (
        horizon_losing_hold_failed
        and position_pnl_ratio <= float(horizon_config.get("losing_hold_exit_ratio", -0.02))
    )
    strong_exit = (
        target_strength >= float(control.get("exit_min_signal_strength", 0.35))
        and not fundamental_supports_current
        and (
            confirmation_score >= float(control.get("exit_min_confirmation_score", 0.45))
            or fundamental_supports_target
            or news_override
        )
    )
    detail.update({
        "reversal_side": reversal_side,
        "reversal_or_exit_strength": float(target_strength),
        "current_side_strength": float(current_strength),
        "fundamental_supports_current": bool(fundamental_supports_current),
        "fundamental_hold_anchor_current": bool(fundamental_hold_anchor_current),
        "fundamental_supports_target": bool(fundamental_supports_target),
        "technical_supports_current": bool(technical_supports_current),
        "technical_supports_target": bool(technical_supports_target),
        "opening_position_invalidation_breached": bool(
            opening_position_invalidation_breached
        ),
        "technical_invalidation_confirmed": bool(technical_invalidation_confirmed),
        "fundamental_medium_opposition": bool(fundamental_medium_opposition),
        "opening_horizon_due": bool(opening_horizon_due),
        "explicit_lifecycle_break": bool(explicit_lifecycle_break),
        "news_supports_current": bool(news_supports_current),
        "news_hold_anchor_current": bool(news_hold_anchor_current),
        "news_high_quality_override": bool(news_override),
        "signal_counts_current": signal_counts_current,
        "horizon_consistency": current_horizon_consistency,
        "lifecycle_classification": (
            "failed_position" if failed_position else
            "new_loss_revalidation_failed" if new_loss_revalidation_failed else
            "horizon_consistency_failed" if horizon_losing_hold_failed else
            "loss_revalidation_failed" if loss_revalidation_failed else
            "trend_position" if trend_position else "probe_position" if probe_expired else "normal"
        ),
        "trend_position": bool(trend_position),
        "failed_position": bool(failed_position),
        "loss_revalidation_due": bool(loss_revalidation_due),
        "loss_revalidated": bool(loss_revalidated),
        "loss_revalidation_failed": bool(loss_revalidation_failed),
        "loss_revalidation_exit": bool(loss_revalidation_exit),
        "new_loss_revalidation_due": bool(new_loss_revalidation_due),
        "new_loss_revalidated": bool(new_loss_revalidated),
        "new_loss_revalidation_failed": bool(new_loss_revalidation_failed),
        "new_loss_revalidation_exit": bool(new_loss_revalidation_exit),
        "new_loss_revalidation_failures": new_loss_failures,
        "exploration_reconfirm_due": bool(exploration_reconfirm_due),
        "exploration_reconfirmed": bool(exploration_reconfirmed),
        "exploration_reconfirm_exit": bool(exploration_reconfirm_exit),
        "exploration_reconfirm_failures": exploration_reconfirm_failures,
        "profitable_hold_supported": bool(profitable_hold_supported),
        "current_opportunity_state": current_state,
        "horizon_losing_hold_failed": bool(horizon_losing_hold_failed),
        "horizon_losing_hold_exit": bool(horizon_losing_hold_exit),
        "probe_expired": bool(probe_expired),
        "strong_reversal": bool(strong_reversal),
        "strong_exit": bool(strong_exit),
    })

    if opening_position_invalidation_breached:
        reasons.append("position_lifecycle_failed")
        notes.append(
            f"{ticker} {current_side} opening FAC position invalidation breached "
            f"at price={current_price:.6g}."
        )
        detail["decision"] = "exit_opening_fac_position_invalidation"
        detail["final_target_ratio"] = 0.0
        return 0.0, reasons, notes, diagnostics

    if technical_invalidation_confirmed:
        reasons.append("position_lifecycle_failed")
        notes.append(
            f"{ticker} {current_side} current technical evidence confirms the opposite side; exiting."
        )
        detail["decision"] = "exit_current_technical_invalidation"
        detail["final_target_ratio"] = 0.0
        return 0.0, reasons, notes, diagnostics

    if fundamental_medium_opposition:
        max_reduction = max(0.0, min(1.0, float(control.get("max_daily_reduction_ratio", 0.40))))
        reduced_ratio = _signed_abs(current_side, abs(current_ratio) * (1.0 - max_reduction))
        reasons.append("fundamental_medium_opposition")
        notes.append(
            f"{ticker} {current_side} medium-horizon fundamental evidence opposes the hold; "
            f"ratio {current_ratio:.2%}->{reduced_ratio:.2%}."
        )
        detail["decision"] = "reduce_fundamental_medium_opposition"
        detail["final_target_ratio"] = float(reduced_ratio)
        return reduced_ratio, reasons, notes, diagnostics

    if failed_position:
        reasons.append("position_lifecycle_failed")
        notes.append(
            f"{ticker} {current_side} failed position exit: pnl_ratio={position_pnl_ratio:.2%}, "
            f"confirmation={confirmation_score:.2f}, held_days={held_days}"
        )
        detail["decision"] = "force_exit_failed_position"
        detail["final_target_ratio"] = 0.0
        return 0.0, reasons, notes, diagnostics

    if new_loss_revalidation_failed:
        if new_loss_revalidation_exit:
            reasons.append("new_position_loss_revalidation_failed")
            notes.append(
                f"{ticker} new {current_side} loss revalidation failed; exiting: "
                f"held_days={held_days}, pnl_ratio={position_pnl_ratio:.2%}, "
                f"failures={new_loss_failures}"
            )
            detail["decision"] = "exit_failed_new_loss_revalidation"
            detail["final_target_ratio"] = 0.0
            return 0.0, reasons, notes, diagnostics

        reduction_multiplier = max(
            0.0,
            min(1.0, float(lifecycle_config.get("new_loss_revalidation_reduction_multiplier", 0.50) or 0.50)),
        )
        reduced_ratio = _signed_abs(current_side, abs(current_ratio) * reduction_multiplier)
        if target_side == current_side:
            reduced_ratio = _signed_abs(current_side, min(abs(position_ratio), abs(reduced_ratio)))
        reasons.append("new_position_loss_revalidation_failed")
        notes.append(
            f"{ticker} new {current_side} loss revalidation failed; reducing exposure: "
            f"held_days={held_days}, pnl_ratio={position_pnl_ratio:.2%}, "
            f"failures={new_loss_failures}, ratio {current_ratio:.2%}->{reduced_ratio:.2%}"
        )
        detail["decision"] = "reduce_failed_new_loss_revalidation"
        detail["final_target_ratio"] = float(reduced_ratio)
        return reduced_ratio, reasons, notes, diagnostics

    if exploration_reconfirm_exit:
        reasons.append("exploration_probe_reconfirm_failed")
        notes.append(
            f"{ticker} {current_side} exploration probe failed daily reconfirmation; exiting: "
            f"held_days={held_days}, pnl_ratio={position_pnl_ratio:.2%}, "
            f"state={current_state or 'unknown'}, confirmation={confirmation_score:.2f}, "
            f"failures={exploration_reconfirm_failures}"
        )
        detail["decision"] = "exit_failed_exploration_reconfirm"
        detail["final_target_ratio"] = 0.0
        return 0.0, reasons, notes, diagnostics

    if exploration_reconfirm_due and not exploration_reconfirmed and explicit_lifecycle_break:
        reduction_multiplier = max(
            0.0,
            min(1.0, float(lifecycle_config.get("exploration_reconfirm_reduction_multiplier", 0.50) or 0.50)),
        )
        reduced_ratio = _signed_abs(current_side, abs(current_ratio) * reduction_multiplier)
        if target_side == current_side:
            reduced_ratio = _signed_abs(current_side, min(abs(position_ratio), abs(reduced_ratio)))
        reasons.append("exploration_probe_reconfirm_reduce")
        notes.append(
            f"{ticker} {current_side} exploration probe not reconfirmed; reducing: "
            f"held_days={held_days}, pnl_ratio={position_pnl_ratio:.2%}, "
            f"state={current_state or 'unknown'}, confirmation={confirmation_score:.2f}, "
            f"failures={exploration_reconfirm_failures}, ratio {current_ratio:.2%}->{reduced_ratio:.2%}"
        )
        detail["decision"] = "reduce_unconfirmed_exploration_probe"
        detail["final_target_ratio"] = float(reduced_ratio)
        return reduced_ratio, reasons, notes, diagnostics

    if horizon_losing_hold_failed:
        if horizon_losing_hold_exit:
            reasons.append("horizon_consistency_failed_losing_hold")
            notes.append(
                f"{ticker} {current_side} losing hold failed horizon consistency; exiting: "
                f"horizon={current_horizon_consistency.get('decision_horizon')}, "
                f"failures={current_horizon_consistency.get('fail_reasons', [])}"
            )
            detail["decision"] = "exit_horizon_mismatch_losing_hold"
            detail["final_target_ratio"] = 0.0
            return 0.0, reasons, notes, diagnostics
        reduction_multiplier = max(
            0.0,
            min(1.0, float(horizon_config.get("losing_hold_reduction_multiplier", 0.50) or 0.50)),
        )
        reduced_ratio = _signed_abs(current_side, abs(current_ratio) * reduction_multiplier)
        if target_side == current_side:
            reduced_ratio = _signed_abs(current_side, min(abs(position_ratio), abs(reduced_ratio)))
        reasons.append("horizon_consistency_failed_losing_hold")
        notes.append(
            f"{ticker} {current_side} losing hold lacks short-horizon confirmation; "
            f"ratio {current_ratio:.2%}->{reduced_ratio:.2%}, "
            f"failures={current_horizon_consistency.get('fail_reasons', [])}"
        )
        detail["decision"] = "reduce_horizon_mismatch_losing_hold"
        detail["final_target_ratio"] = float(reduced_ratio)
        return reduced_ratio, reasons, notes, diagnostics

    if loss_revalidation_failed:
        if loss_revalidation_exit:
            reasons.append("position_lifecycle_loss_revalidation_failed")
            notes.append(
                f"{ticker} {current_side} loss revalidation failed; exiting: "
                f"held_days={held_days}, pnl_ratio={position_pnl_ratio:.2%}, "
                f"confirmation={confirmation_score:.2f}, current_strength={current_strength:.2f}"
            )
            detail["decision"] = "exit_failed_loss_revalidation"
            detail["final_target_ratio"] = 0.0
            return 0.0, reasons, notes, diagnostics

        reduction_multiplier = max(
            0.0,
            min(1.0, float(lifecycle_config.get("loss_revalidation_reduction_multiplier", 0.50) or 0.50)),
        )
        reduced_ratio = _signed_abs(current_side, abs(current_ratio) * reduction_multiplier)
        if target_side == current_side:
            reduced_ratio = _signed_abs(current_side, min(abs(position_ratio), abs(reduced_ratio)))
        reasons.append("position_lifecycle_loss_revalidation_failed")
        notes.append(
            f"{ticker} {current_side} loss revalidation failed; reducing exposure: "
            f"held_days={held_days}, pnl_ratio={position_pnl_ratio:.2%}, "
            f"confirmation={confirmation_score:.2f}, ratio {current_ratio:.2%}->{reduced_ratio:.2%}"
        )
        detail["decision"] = "reduce_failed_loss_revalidation"
        detail["final_target_ratio"] = float(reduced_ratio)
        return reduced_ratio, reasons, notes, diagnostics

    if probe_expired and not trend_position and explicit_lifecycle_break:
        reasons.append("position_lifecycle_probe_expired")
        notes.append(
            f"{ticker} {current_side} probe expired: held_days={held_days}, "
            f"pnl_ratio={position_pnl_ratio:.2%}, confirmation={confirmation_score:.2f}, "
            f"target_side={target_side}"
        )
        detail["decision"] = "exit_unvalidated_probe"
        detail["final_target_ratio"] = 0.0
        return 0.0, reasons, notes, diagnostics

    if target_side == current_side:
        delta = abs(position_ratio - current_ratio)
        if delta < min_rebalance_ratio:
            reasons.append("minimum_rebalance_threshold")
            notes.append(
                f"{ticker} same-side rebalance skipped: ratio change {delta:.2%} "
                f"below {min_rebalance_ratio:.2%}"
            )
            detail["decision"] = "keep_current_small_delta"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics

        increasing = abs(position_ratio) > abs(current_ratio)
        if increasing and not technical_supports_current:
            reasons.append("holding_add_requires_current_technical_trigger")
            notes.append(
                f"{ticker} {current_side} add deferred: the existing position remains valid, "
                "but incremental risk has no current technical entry trigger."
            )
            detail["decision"] = "keep_current_without_add_trigger"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics

        reducing = abs(position_ratio) < abs(current_ratio)
        protective_reduce_requested = any(
            reason in reasons
            for reason in (
                "winning_template_continuation_protective_reduce",
                "hold_exit_action_value_protection",
            )
        ) or bool(
            prior_reasons
            & {
                "winning_template_continuation_protective_reduce",
                "hold_exit_action_value_protection",
            }
        )
        if reducing and not strong_exit and not explicit_lifecycle_break and not protective_reduce_requested:
            reasons.append("holding_lifecycle_not_invalidated")
            notes.append(
                f"{ticker} {current_side} position retained: no opening invalidation, "
                "explicit reversal, medium-horizon fundamental opposition or hard risk."
            )
            detail["decision"] = "keep_current_without_lifecycle_break"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics
        if profitable_hold_supported and reducing and not strong_exit and not protective_reduce_requested:
            reasons.append("profitable_hold_continuation")
            notes.append(
                f"{ticker} {current_side} profitable hold retained: pnl_ratio={position_pnl_ratio:.2%}, "
                f"confirmation={confirmation_score:.2f}, current_strength={current_strength:.2f}, "
                f"state={current_state or 'unknown'}"
            )
            detail["decision"] = "keep_profitable_supported_hold"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics
        if trend_position and reducing and not strong_exit and not protective_reduce_requested:
            reasons.append("position_lifecycle_trend_hold")
            notes.append(
                f"{ticker} {current_side} trend position held: pnl_ratio={position_pnl_ratio:.2%}, "
                f"confirmation={confirmation_score:.2f}"
            )
            detail["decision"] = "keep_trend_position"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics

        if reducing and held_days is not None and held_days < min_hold_days and not strong_exit:
            if protective_reduce_requested:
                detail["decision"] = "allow_profit_protective_reduce_before_min_hold"
                detail["final_target_ratio"] = float(position_ratio)
                return position_ratio, reasons, notes, diagnostics
            reasons.append("holding_period_control")
            notes.append(
                f"{ticker} {current_side} reduction deferred: held {held_days}d < "
                f"sector min {min_hold_days}d and no strong exit evidence"
            )
            detail["decision"] = "keep_current_min_hold"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics

        if reducing and fundamental_supports_current and not strong_exit:
            max_reduction = float(control.get("max_daily_reduction_ratio", 0.40))
            min_abs_after_reduction = abs(current_ratio) * max(0.0, 1.0 - max_reduction)
            capped_abs = max(abs(position_ratio), min_abs_after_reduction)
            capped_ratio = _signed_abs(current_side, capped_abs)
            if abs(capped_ratio - position_ratio) > 1e-12:
                reasons.append("fundamental_anchor_rebalance_cap")
                notes.append(
                    f"{ticker} {current_side} reduction capped by fundamental anchor: "
                    f"{position_ratio:.2%}->{capped_ratio:.2%}"
                )
                detail["decision"] = "cap_same_side_reduction"
                detail["final_target_ratio"] = float(capped_ratio)
                return capped_ratio, reasons, notes, diagnostics

        detail["decision"] = "allow_same_side_rebalance"
        detail["final_target_ratio"] = float(position_ratio)
        return position_ratio, reasons, notes, diagnostics

    if target_side == "flat":
        protective_exit_requested = any(
            reason in reasons
            for reason in (
                "hold_exit_action_value_protection",
                "winning_template_continuation_protective_reduce",
            )
        ) or bool(
            prior_reasons
            & {
                "hold_exit_action_value_protection",
                "winning_template_continuation_protective_reduce",
            }
        )
        if not strong_exit and not explicit_lifecycle_break and not protective_exit_requested:
            if profitable_hold_supported:
                reasons.append("profitable_hold_continuation")
                notes.append(
                    f"{ticker} {current_side} profitable position retained without a lifecycle break."
                )
                detail["decision"] = "keep_profitable_supported_exit_deferred"
            elif trend_position:
                reasons.append("position_lifecycle_trend_hold")
                notes.append(
                    f"{ticker} {current_side} trend position retained without a lifecycle break."
                )
                detail["decision"] = "keep_trend_position_exit_deferred"
            else:
                reasons.append("holding_lifecycle_not_invalidated")
                notes.append(
                    f"{ticker} {current_side} position retained: absence of a new entry trigger "
                    "is not an exit condition."
                )
                detail["decision"] = "keep_current_without_lifecycle_break"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics
        if profitable_hold_supported and not strong_exit and not protective_exit_requested:
            reasons.append("profitable_hold_continuation")
            notes.append(
                f"{ticker} {current_side} profitable exit deferred: pnl_ratio={position_pnl_ratio:.2%}, "
                f"confirmation={confirmation_score:.2f}, current_strength={current_strength:.2f}, "
                f"state={current_state or 'unknown'}"
            )
            detail["decision"] = "keep_profitable_supported_exit_deferred"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics
        if trend_position and not strong_exit and not protective_exit_requested:
            reasons.append("position_lifecycle_trend_hold")
            notes.append(
                f"{ticker} {current_side} trend exit deferred: pnl_ratio={position_pnl_ratio:.2%}, "
                f"confirmation={confirmation_score:.2f}"
            )
            detail["decision"] = "keep_trend_position_exit_deferred"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics

        if (
            not protective_exit_requested
            and (
                (held_days is not None and held_days < min_hold_days and not strong_exit)
                or (fundamental_supports_current and not strong_exit)
            )
        ):
            reasons.append("holding_period_control")
            notes.append(
                f"{ticker} {current_side} exit deferred: held_days={held_days}, "
                f"sector_min={min_hold_days}, fundamental_anchor={fundamental_supports_current}"
            )
            detail["decision"] = "keep_current_exit_deferred"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics

        detail["decision"] = "allow_exit"
        detail["final_target_ratio"] = 0.0
        return 0.0, reasons, notes, diagnostics

    if target_side != current_side:
        if strong_reversal:
            detail["decision"] = "allow_reversal"
            detail["final_target_ratio"] = float(position_ratio)
            return position_ratio, reasons, notes, diagnostics

        if profitable_hold_supported and not strong_exit:
            reasons.append("profitable_hold_continuation")
            notes.append(
                f"{ticker} weak reversal ignored for profitable supported hold: current={current_side}, "
                f"target={target_side}, pnl_ratio={position_pnl_ratio:.2%}, "
                f"target_strength={target_strength:.2f}, confirmation={confirmation_score:.2f}"
            )
            detail["decision"] = "keep_profitable_supported_reversal_blocked"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics

        if strong_exit and not fundamental_supports_current:
            reasons.append("reverse_requires_stronger_evidence")
            notes.append(
                f"{ticker} reversal downgraded to flat: target={target_side}, "
                f"strength={target_strength:.2f}, confirmation={confirmation_score:.2f}"
            )
            detail["decision"] = "downgrade_reversal_to_exit"
            detail["final_target_ratio"] = 0.0
            return 0.0, reasons, notes, diagnostics

        reasons.append("reverse_requires_stronger_evidence")
        notes.append(
            f"{ticker} reversal blocked: current={current_side}, target={target_side}, "
            f"held_days={held_days}, strength={target_strength:.2f}, "
            f"fundamental_anchor={fundamental_supports_current}"
        )
        detail["decision"] = "keep_current_reversal_blocked"
        detail["final_target_ratio"] = float(current_ratio)
        return current_ratio, reasons, notes, diagnostics

    detail["decision"] = "allow"
    detail["final_target_ratio"] = float(position_ratio)
    return position_ratio, reasons, notes, diagnostics


def _resolve_phase1_contract_code(existing_position: Any, morning_price_context: Any) -> str | None:
    """Bind the actual held contract, then the cutoff-visible Router contract."""
    current_lots = int(getattr(existing_position, "shares", 0) or 0) if existing_position is not None else 0
    if current_lots != 0:
        held_contract = str(getattr(existing_position, "contract_code", "") or "").strip().upper()
        return held_contract or None
    visible_contract = str(getattr(morning_price_context, "contract_code", "") or "").strip().upper()
    return visible_contract or None


def _run_pm_six_step_decision(state: FundState):
    """Run PM steps 1-4 and return the single mutable memory state."""
    agent_name = AgentKey.PORTFOLIO
    portfolio = state["portfolio"]
    ticker = state["ticker"]
    trading_date = state["trading_date"]
    source_analyst_signals = state["analyst_signals"]
    num_tickers = state["num_tickers"]
    enabled_analysts = state.get("enabled_analysts", [])
    config_id = state.get("config_id", "")
    phase = state.get("phase")
    morning_price_context = state.get("morning_price_context")
    phase_value = getattr(phase, "value", phase)
    if phase_value != TradingPhase.PHASE1.value:
        raise RuntimeError(
            "portfolio_agent_futures only supports phase1 pre-open recommendation flow."
        )
    _validate_required_analyst_signals(ticker, enabled_analysts, source_analyst_signals)
    signal_collection_contract = state.get("signal_collection_contract")
    if not isinstance(signal_collection_contract, dict) or not signal_collection_contract:
        raise RuntimeError("pm_missing_signal_collection_contract_from_signal_collector")
    try:
        validate_signal_collection_contract(
            signal_collection_contract,
            ticker=ticker,
            trading_date=trading_date,
            enabled_analysts=enabled_analysts,
            analyst_signals=source_analyst_signals,
            require_signal_record_ids=True,
        )
    except ValueError as exc:
        error = str(exc)
        if "invalid_decision_boundary" in error:
            raise RuntimeError("pm_invalid_signal_collection_contract_boundary") from None
        if "invalid_source_agent" in error:
            raise RuntimeError("pm_invalid_signal_collection_contract_source_agent") from None
        raise RuntimeError("pm_invalid_signal_collection_contract") from None
    analyst_signals = build_pm_evidence_signals_from_scc(signal_collection_contract)

    cfg = state.get("config", {})
    db = get_db()
    router = state.get("router")

    full_config = state.get("full_config", cfg)

    max_total_margin_ratio = get_hard_allocation_margin_ratio(full_config)
    risk_buffer_ratio = full_config.get("risk_buffer_ratio", cfg.get("risk_buffer_ratio", 0.10))
    risk_level, cashflow_ratio = check_risk_level(portfolio, full_config)

    # First apply the risk-level scaling, then decide LONG, SHORT, or NEUTRAL.
    position_scaling = get_position_scaling_factor(risk_level, full_config)
    max_single_margin_ratio = get_max_single_position_ratio(risk_level, full_config)

    # Risk-state logging for the phase1 futures flow.
    if risk_level == RiskLevel.WARNING:
        pass
    elif risk_level == RiskLevel.DANGER:
        pass
    elif risk_level == RiskLevel.EMERGENCY:
        pass
        # Emergency mode only allows de-risking recommendations.
        pass

    # Translate the signed target ratio into lots using current price and multiplier.
    account_equity = _portfolio_account_equity(portfolio)
    if account_equity <= 0:
        account_equity = float(portfolio.cashflow or 0.0)

    # Calculate current portfolio margin usage from all open positions.
    current_margin_used = sum(p.margin_used for p in portfolio.positions.values())
    current_margin_ratio = current_margin_used / account_equity if account_equity > 0 else 0

    max_allowed_margin = account_equity * max_total_margin_ratio
    remaining_margin = max_allowed_margin - current_margin_used
    max_single_margin = account_equity * max_single_margin_ratio

    pass

    # Enter reduce-only mode when margin usage breaches the configured cap.
    if current_margin_ratio >= max_total_margin_ratio:
        pass
        # Once the cap is breached, only position-reducing actions are allowed.
        force_reduce_only = True
    else:
        force_reduce_only = False

    underlying_code = extract_underlying_code(ticker)
    contract_info = FuturesContractInfoCache.get_contract_info(underlying_code)

    if not contract_info:
        raise RuntimeError(
            f"Missing contract info for {ticker}; phase1 cannot size, margin-check, or audit this trade."
        )

    multiplier = contract_info['contract_multiplier']
    existing_position = portfolio.positions.get(ticker)
    contract_code = _resolve_phase1_contract_code(existing_position, morning_price_context)

    if morning_price_context is None or morning_price_context.base_price is None:
        current_lots_for_missing_basis = (
            int(portfolio.positions[ticker].shares)
            if ticker in portfolio.positions and portfolio.positions[ticker] is not None
            else 0
        )
        hold_decision = FuturesDecision(
            ticker=ticker,
            action=FuturesAction.HOLD,
            lots=0,
            price=0,
            justification=f"{ticker} missing phase1 execution basis"
        )
        skipped_pm_state = _build_pm_memory_state(
            config_id=config_id,
            full_config=full_config,
            portfolio=portfolio,
            ticker=ticker,
            trading_date=trading_date,
            contract_code=contract_code,
            decision=hold_decision,
            morning_price_context=morning_price_context,
            analyst_signals=analyst_signals,
            pm_state_update=_build_blocked_pm_memory_state_update(
                ticker=ticker,
                current_lots=current_lots_for_missing_basis,
                target_lots=current_lots_for_missing_basis,
                reason="missing_phase1_execution_basis",
                authority_type="data_quality_block",
                account_equity=account_equity,
                signal_collection_contract=signal_collection_contract,
                execution_contract_fields={
                    "candidate_status": "blocked",
                    "candidate_block_reason": "missing_phase1_execution_basis",
                },
            ),
        )
        skipped_pm_state["recommendation_context"]["status"] = RecommendationStatus.SKIPPED
        skipped_pm_state["recommendation_context"]["warning_message"] = (
            morning_price_context.warning_message
            if morning_price_context
            else f"{ticker} missing phase1 execution basis"
        )
        return skipped_pm_state

    # Legacy pre-settlement rollover execution was removed.
    # Rollover detection now happens in phase2, and execution happens in next-day phase1.

    try:
        current_price = morning_price_context.base_price
        settle_price = current_price

        price_sanity = _dynamic_price_sanity_result(
            ticker=underlying_code,
            current_price=float(current_price),
            morning_price_context=morning_price_context,
            router=router,
            trading_date=trading_date,
        )
        if price_sanity.get("status") == "invalid":
            current_lots_for_price_anomaly = (
                int(portfolio.positions[ticker].shares)
                if ticker in portfolio.positions and portfolio.positions[ticker] is not None
                else 0
            )
            pass
            hold_decision = FuturesDecision(
                ticker=ticker,
                action=FuturesAction.HOLD,
                lots=0,
                price=current_price,
                settle_price=settle_price,
                margin_rate=contract_info['margin_rate_long'],
                contract_multiplier=multiplier,
                contract_code=contract_code,
                justification=(
                    f"{underlying_code} price {current_price:.2f} failed dynamic sanity check "
                    f"({price_sanity.get('reason')}); data_price_anomaly prevents a tradable recommendation."
                ),
            )
            price_anomaly_snapshot = {
                "signal_direction": "flat",
                "signal_confidence": 0.0,
                "target_position_ratio": 0.0,
                "target_margin_ratio_estimate": 0.0,
                "target_lots": 0,
                "reference_price": float(current_price),
                "risk_level": "data_quality",
                "decision_horizon": "none",
                "validation_horizon": "same_day",
                "reason_codes": ["data_price_anomaly"],
                "no_trade_reason": "data_price_anomaly",
                "data_price_anomaly": {
                    "status": price_sanity.get("status"),
                    "reason": price_sanity.get("reason"),
                    "lower_bound": price_sanity.get("lower_bound"),
                    "upper_bound": price_sanity.get("upper_bound"),
                    "base_price_source": getattr(
                        morning_price_context.base_price_source,
                        "value",
                        morning_price_context.base_price_source,
                    ),
                },
            }
            return _phase1_return_with_pm_state(
                config_id=config_id,
                full_config=full_config,
                portfolio=portfolio,
                ticker=ticker,
                trading_date=trading_date,
                contract_code=contract_code,
                decision=hold_decision,
                morning_price_context=morning_price_context,
                analyst_signals=analyst_signals,
                plan_snapshot=price_anomaly_snapshot,
                pm_state_update=_build_blocked_pm_memory_state_update(
                    ticker=ticker,
                    current_lots=current_lots_for_price_anomaly,
                    target_lots=current_lots_for_price_anomaly,
                    reason="data_price_anomaly",
                    authority_type="data_quality_block",
                    account_equity=account_equity,
                    signal_collection_contract=signal_collection_contract,
                    execution_contract_fields=price_anomaly_snapshot,
                    control_diagnostics={"data_price_anomaly": price_anomaly_snapshot.get("data_price_anomaly")},
                ),
            )
        elif price_sanity.get("status") == "unchecked":
            pass
        else:
            pass

        if ticker in portfolio.positions:
            position = portfolio.positions[ticker]
            if position.shares != 0:
                reference_price = position.settle_price or position.entry_price
                if position.shares > 0:  # Long position.
                    position.unrealized_pnl = (current_price - reference_price) * position.shares * multiplier
                else:  # Short position.
                    position.unrealized_pnl = (reference_price - current_price) * abs(position.shares) * multiplier

        if ticker in portfolio.positions:
            position = portfolio.positions[ticker]
            if position.shares != 0 and position.margin_used > 0:
                single_loss_ratio = position.unrealized_pnl / position.margin_used

                if single_loss_ratio <= -0.10:
                    pass

                    if position.shares > 0:
                        pass
                        close_decision = FuturesDecision(
                            ticker=ticker,
                            action=FuturesAction.CLOSE_LONG,
                            lots=abs(position.shares),
                            price=current_price,
                            settle_price=settle_price,
                            margin_rate=contract_info['margin_rate_long'],
                            contract_multiplier=multiplier,
                            contract_code=contract_code,
                            justification=f"Single-position loss threshold reached ({single_loss_ratio*100:.1f}% <= -10.0%); closing long position.",
                        )
                        return _phase1_return_with_pm_state(
                            config_id=config_id,
                            full_config=full_config,
                            portfolio=portfolio,
                            ticker=ticker,
                            trading_date=trading_date,
                            contract_code=contract_code,
                            decision=close_decision,
                            morning_price_context=morning_price_context,
                            analyst_signals=analyst_signals,
                            pm_state_update=_build_blocked_pm_memory_state_update(
                                ticker=ticker,
                                current_lots=int(position.shares),
                                target_lots=0,
                                reason="single_position_loss_threshold",
                                authority_type="risk_exit",
                                account_equity=account_equity,
                                signal_collection_contract=signal_collection_contract,
                                execution_contract_fields={
                                    "candidate_status": "blocked",
                                    "candidate_block_reason": "single_position_loss_threshold",
                                },
                            ),
                        )
                    else:
                        pass
                        close_decision = FuturesDecision(
                            ticker=ticker,
                            action=FuturesAction.CLOSE_SHORT,
                            lots=abs(position.shares),
                            price=current_price,
                            settle_price=settle_price,
                            margin_rate=contract_info['margin_rate_short'],
                            contract_multiplier=multiplier,
                            contract_code=contract_code,
                            justification=f"Single-position loss threshold reached ({single_loss_ratio*100:.1f}% <= -10.0%); closing short position.",
                        )
                        return _phase1_return_with_pm_state(
                            config_id=config_id,
                            full_config=full_config,
                            portfolio=portfolio,
                            ticker=ticker,
                            trading_date=trading_date,
                            contract_code=contract_code,
                            decision=close_decision,
                            morning_price_context=morning_price_context,
                            analyst_signals=analyst_signals,
                            pm_state_update=_build_blocked_pm_memory_state_update(
                                ticker=ticker,
                                current_lots=int(position.shares),
                                target_lots=0,
                                reason="single_position_loss_threshold",
                                authority_type="risk_exit",
                                account_equity=account_equity,
                                signal_collection_contract=signal_collection_contract,
                                execution_contract_fields={
                                    "candidate_status": "blocked",
                                    "candidate_block_reason": "single_position_loss_threshold",
                                },
                            ),
                        )

    except Exception:
        raise RuntimeError(f"{ticker}: pm_risk_evaluation_failed") from None

    analyst_count = len(enabled_analysts)
    max_position_ratio = 1
    if num_tickers > 1:
        base_max_ratio = round(2 / num_tickers * 20) / 20

        margin_max_ratio = max_single_margin_ratio

        # Apply the tighter of the diversification cap and the margin cap.
        max_position_ratio = min(base_max_ratio, margin_max_ratio)

    # Group analyst outputs by agent name for downstream weighting.
    signals_by_agent = {}
    for signal in analyst_signals:
        if hasattr(signal, 'agent_name') and signal.agent_name:
            signals_by_agent[signal.agent_name] = signal

    weights = {}
    fusion_context = {}
    if analyst_count > 1:
        # Extract the basis percentage from the fundamental analyst output.
        fundamental_signal = signals_by_agent.get('fundamental')
        basis_pct = 0.0
        fundamental_quality = {}
        if fundamental_signal:
            basis_pct = DynamicWeightCalculator.extract_basis_from_signal(fundamental_signal)
            fundamental_quality = DynamicWeightCalculator.extract_quality_from_signal(fundamental_signal)

        # Build dynamic weights from the current basis and structured quality context.
        calculator = DynamicWeightCalculator(full_config)
        weights = calculator.calculate(
            basis_pct,
            fundamental_quality=fundamental_quality,
        )
        fusion_context = _quality_aware_fusion_context(
            ticker=ticker,
            analyst_signals=analyst_signals,
            dynamic_weights=weights,
            full_config=full_config,
        )
        weights = fusion_context["quality_adjusted_weights"]

        pass

    pm_learning_audit = {
        "enabled": bool(db and config_id),
        "selected_digest_ids": [],
        "memory_trace": {},
        "trade_episode_count": 0,
        "no_trade_opportunity_count": 0,
        "hypothesis_count": 0,
        "candidate_hypothesis_count": 0,
        "validated_hypothesis_count": 0,
        "hypothesis_status_counts": {},
        "sector": fusion_context.get("sector") if isinstance(fusion_context, dict) else None,
        "market_regime": None,
        "structured_tool_only": True,
        "decision_memory_retrieval": "decision_memory_retrieval",
        "candidate_hypothesis_authority": "not_consumed_by_pm_without_structured_action_value",
        "hard_margin_cap_not_overridden": True,
    }
    alpha_setup_profiles: list[dict] = []
    alpha_setup_action_values: list[dict] = []
    step4_rejected_or_downgraded: list[dict] = []
    pm_memory_db = _ExplicitPMLearningScopeDBView(db) if db else db
    pm_learning_audit["alpha_setup_profile_count"] = len(alpha_setup_profiles)
    pm_learning_audit["alpha_setup_profiles"] = alpha_setup_profiles[:6]
    pm_learning_audit["alpha_setup_action_value_count"] = len(alpha_setup_action_values)
    pm_learning_audit["alpha_setup_action_values"] = alpha_setup_action_values[:8]
    effective_memory_summary: dict = {
        "status": "not_retrieved_yet",
        "consumer_scope": "pm_learning",
        "effective_row_count": len(alpha_setup_action_values),
        "quality_first": True,
        "empty_history_cannot_block_real_history": True,
        "tool_boundary": "pm_research_memory_single_entrypoint",
    }

    # Prefer structured basis metadata; text parsing is only a compatibility fallback.
    fundamental_basis = 0.0
    fundamental_quality = {}
    for signal in analyst_signals:
        if hasattr(signal, 'agent_name') and signal.agent_name == 'fundamental':
            fundamental_basis = DynamicWeightCalculator.extract_basis_from_signal(signal)
            fundamental_quality = DynamicWeightCalculator.extract_quality_from_signal(signal)
            if fundamental_basis:
                pass
            break
    # Reuse the same quality-aware adaptive weights used by deterministic PM scoring.
    dynamic_weights = weights if weights else None
    if not fusion_context:
        calculator = DynamicWeightCalculator(full_config)
        if calculator.enabled:
            dynamic_weights = calculator.calculate(
                fundamental_basis,
                fundamental_quality=fundamental_quality,
            )
        fusion_context = _quality_aware_fusion_context(
            ticker=ticker,
            analyst_signals=analyst_signals,
            dynamic_weights=dynamic_weights,
            full_config=full_config,
        )
        dynamic_weights = fusion_context["quality_adjusted_weights"]

    long_scores, short_scores = calculate_long_short_signals(
        ticker=ticker,
        analyst_signals=analyst_signals,
        fundamental_basis=fundamental_basis,
        weights=dynamic_weights,
        fusion_context=fusion_context,
        full_config=full_config,
    )

    pass

    initial_abs_ratio, initial_direction = calculate_position_ratio_with_balance(
        ticker=ticker,
        long_scores=long_scores,
        short_scores=short_scores,
        max_position_ratio=max_position_ratio,
        risk_level=risk_level,
        full_config=full_config,
    )
    if initial_direction == "LONG":
        initial_signed_ratio = float(initial_abs_ratio)
    elif initial_direction == "SHORT":
        initial_signed_ratio = -float(initial_abs_ratio)
    else:
        initial_signed_ratio = 0.0
    position_risk = PositionRisk(
        optimal_position_ratio=initial_signed_ratio,
        justification=_sanitize_visible_text(
            "Deterministic PM initial target from structured analyst scores; "
            f"direction={initial_direction}, long_strength="
            f"{float(long_scores.get('score', 0.0) or 0.0) * float(long_scores.get('confidence', 0.0) or 0.0):.3f}, "
            f"short_strength="
            f"{float(short_scores.get('score', 0.0) or 0.0) * float(short_scores.get('confidence', 0.0) or 0.0):.3f}, "
            f"max_position_ratio={max_position_ratio:.2%}, risk_level={risk_level.value}; "
            "PM does not call LLM."
        ),
    )

    pass
    pass

    # Allow a strong explicit basis signal to override a conflicting deterministic direction.

    bullish_strength = long_scores['score'] * long_scores['confidence']
    bearish_strength = short_scores['score'] * short_scores['confidence']

    if long_scores['has_strong_basis'] and position_risk.optimal_position_ratio < 0:
        pass
        pass
        pass
        pass

    elif short_scores['has_strong_basis'] and position_risk.optimal_position_ratio > 0:
        pass
        pass
        pass
        pass

    directional_reasons, directional_notes, directional_diagnostics = _apply_directional_override(
        ticker=ticker,
        position_risk=position_risk,
        long_scores=long_scores,
        short_scores=short_scores,
        max_position_ratio=max_position_ratio,
        risk_level=risk_level,
        full_config=full_config,
    )
    if directional_notes:
        pass

    if position_scaling < 1.0:
        original_ratio = position_risk.optimal_position_ratio
        position_risk.optimal_position_ratio *= position_scaling
        position_risk.justification += f"\n[Risk adjustment: {risk_level.value}; position ratio scaled from {original_ratio:.2%} to {position_risk.optimal_position_ratio:.2%}]"
        pass

    # Clamp the signed target ratio to the allowed absolute cap.
    if position_risk.optimal_position_ratio > max_position_ratio:
        position_risk.optimal_position_ratio = max_position_ratio
    elif position_risk.optimal_position_ratio < -max_position_ratio:
        position_risk.optimal_position_ratio = -max_position_ratio

    current_position = portfolio.positions.get(ticker) if ticker in portfolio.positions else None
    current_net_exposure = _current_net_exposure(portfolio, account_equity)
    current_ticker_exposure = _signed_position_ratio(current_position, account_equity)
    current_lots_for_control = int(getattr(current_position, "shares", 0) or 0)
    opening_fac_context = _load_opening_fac_context(
        db=db,
        config_id=config_id,
        ticker=ticker,
        trading_date=trading_date,
        current_lots=current_lots_for_control,
    )
    pre_control_ratio = position_risk.optimal_position_ratio

    bq_ratio, bq_reasons, bq_notes, bq_diagnostics = _apply_business_quality_position_gate(
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        analyst_signals=analyst_signals,
        full_config=full_config,
    )
    if bq_reasons or bq_notes:
        before_ratio = position_risk.optimal_position_ratio
        position_risk.optimal_position_ratio = bq_ratio
        position_risk.justification += (
            f"\n[Business quality gate: {before_ratio:.2%}->{bq_ratio:.2%}; "
            f"reasons={bq_reasons}]"
        )
        pass

    signal_strength = max(
        float(long_scores.get("score", 0.0) or 0.0) * float(long_scores.get("confidence", 0.0) or 0.0),
        float(short_scores.get("score", 0.0) or 0.0) * float(short_scores.get("confidence", 0.0) or 0.0),
    )
    target_side_for_confirmation = _target_side_from_ratio(position_risk.optimal_position_ratio)
    if target_side_for_confirmation not in {"long", "short"}:
        long_strength_for_confirmation = (
            float(long_scores.get("score", 0.0) or 0.0)
            * float(long_scores.get("confidence", 0.0) or 0.0)
        )
        short_strength_for_confirmation = (
            float(short_scores.get("score", 0.0) or 0.0)
            * float(short_scores.get("confidence", 0.0) or 0.0)
        )
        if long_strength_for_confirmation > short_strength_for_confirmation + 0.03:
            target_side_for_confirmation = "long"
        elif short_strength_for_confirmation > long_strength_for_confirmation + 0.03:
            target_side_for_confirmation = "short"
    market_confirmation = build_scc_market_confirmation(
        signal_collection_contract,
        target_direction=target_side_for_confirmation,
    )

    signal_combo = _analyst_signal_combo(analyst_signals)
    early_adaptive_policy_state = []
    adaptive_policy_safety_trace = {}
    early_horizon = "*"
    early_market_regime = "*"
    early_setup_type = "*"
    data_quality_summary_for_pm = build_scc_data_quality_summary(signal_collection_contract)
    opportunity_scorecard_cfg = (
        (_get_portfolio_manager_config(full_config).get("quality_aware_fusion") or {}).get("opportunity_scorecard") or {}
    )
    scorecard_alpha_setup_action_values = []
    opportunity_scorecard = build_opportunity_scorecard(
        ticker=ticker,
        analyst_signals=analyst_signals,
        market_confirmation=market_confirmation,
        data_quality_summary=data_quality_summary_for_pm,
        adaptive_policy_state=early_adaptive_policy_state,
        alpha_setup_profiles=alpha_setup_profiles,
        alpha_setup_action_values=scorecard_alpha_setup_action_values,
        signal_collection_contract=signal_collection_contract,
        decision_date=trading_date,
        config=opportunity_scorecard_cfg,
    )
    ticker_side_selection_result = select_ticker_side(
        ticker=ticker,
        analyst_signals=analyst_signals,
        signal_collection_contract=signal_collection_contract,
        market_confirmation=market_confirmation,
        data_quality_summary=data_quality_summary_for_pm,
        decision_date=trading_date,
        config=opportunity_scorecard_cfg,
        prebuilt_scorecard=opportunity_scorecard,
    )
    opportunity_scorecard = ticker_side_selection_result["opportunity_scorecard"]
    fusion_context["opportunity_scorecard"] = opportunity_scorecard
    preferred_side = str(opportunity_scorecard.get("preferred_side") or "flat").strip().lower()
    target_side_for_confirmation = preferred_side
    market_confirmation = build_scc_market_confirmation(
        signal_collection_contract,
        target_direction=preferred_side,
    )
    if position_risk.optimal_position_ratio:
        if preferred_side == "long":
            position_risk.optimal_position_ratio = abs(position_risk.optimal_position_ratio)
        elif preferred_side == "short":
            position_risk.optimal_position_ratio = -abs(position_risk.optimal_position_ratio)
        elif preferred_side == "flat" and current_lots_for_control == 0:
            position_risk.optimal_position_ratio = 0.0
    initial_lifecycle_target_lots = _provisional_target_lots_for_lifecycle_port(
        current_lots=current_lots_for_control,
        current_ratio=current_ticker_exposure,
        target_ratio=position_risk.optimal_position_ratio,
    )
    initial_lifecycle_action_state = {
        "current_lots": int(current_lots_for_control or 0),
        "target_lots": int(initial_lifecycle_target_lots or 0),
        "lots_delta": int(initial_lifecycle_target_lots or 0) - int(current_lots_for_control or 0),
        "requires_intraday_confirmation": False,
        "conditional_trigger_authority": False,
        "reason_codes": [],
        "pm_step": "step_3_primary_lifecycle_action_port_after_side_selection",
    }
    primary_lifecycle_action_port = classify_lifecycle_action_port(initial_lifecycle_action_state)
    initial_lifecycle_learning_router = {
        "tool": "pm_lifecycle_learning_router",
        "status": "deferred_to_single_final_pm_lifecycle_learning_router",
        "primary_lifecycle_action_port": primary_lifecycle_action_port,
        "pm_lifecycle_action_port": primary_lifecycle_action_port.get("pm_lifecycle_action_port"),
        "scorecard_consumption_boundary": (
            "scorecard consumes normalized PM learning components only; lifecycle lane acceptance, "
            "trigger/profile routing, and rejection are decided once by the PM lifecycle learning router"
        ),
        "writes_contract": False,
        "writes_db": False,
    }
    pm_learning_audit["primary_lifecycle_action_port"] = primary_lifecycle_action_port
    pm_learning_audit["initial_lifecycle_learning_router"] = initial_lifecycle_learning_router

    full_config = apply_config_learning_overlay(
        full_config,
        db=db,
        config_id=config_id,
        trading_date=trading_date,
    )
    state["full_config"] = full_config
    step4_memory_side = preferred_side if preferred_side in {"long", "short"} else ""
    if step4_memory_side:
        early_horizon = _resolve_decision_horizon(
            analyst_signals,
            1 if step4_memory_side == "long" else -1,
        )
        early_market_regime = _market_regime_from_signals(analyst_signals, step4_memory_side)
        early_setup_type = _current_canonical_setup_type_from_signals(
            step4_memory_side,
            analyst_signals,
        )
    if db and config_id and step4_memory_side:
        try:
            early_memory_result = retrieve_pm_memory(
                db=pm_memory_db,
                config_id=config_id,
                ticker=ticker,
                side=step4_memory_side,
                horizon_class=early_horizon,
                market_regime=early_market_regime,
                setup_type=early_setup_type,
                sector=fusion_context.get("sector") if isinstance(fusion_context, dict) else None,
                signal_combo=list(signal_combo),
                trading_date=trading_date,
                include_profiles=True,
                include_adaptive_policy_state=True,
                limit=12,
            )
            early_adaptive_policy_state = early_memory_result.get("adaptive_policy_state") or []
            adaptive_policy_safety_trace = early_memory_result.get("adaptive_policy_safety_trace") or {}
            alpha_setup_profiles = early_memory_result.get("alpha_setup_profiles") or []
            effective_memory_summary = early_memory_result.get("effective_memory_summary") or effective_memory_summary
            pm_learning_audit["decision_memory_retrieval_initial"] = {
                "tool": "decision_memory_retrieval",
                "side": step4_memory_side,
                "horizon_class": early_horizon,
                "market_regime": early_market_regime,
                "setup_type": early_setup_type,
                "effective_memory_summary": effective_memory_summary,
                "retrieval_attempts": early_memory_result.get("retrieval_attempts") or [],
                "rejected_or_downgraded": early_memory_result.get("rejected_or_downgraded") or [],
            }
            step4_rejected_or_downgraded = _merge_rejected_or_downgraded(
                step4_rejected_or_downgraded,
                early_memory_result.get("rejected_or_downgraded") or [],
            )
            pm_learning_audit["alpha_setup_profile_count"] = len(alpha_setup_profiles)
            pm_learning_audit["alpha_setup_profiles"] = alpha_setup_profiles[:6]
            pm_learning_audit["alpha_setup_action_value_count"] = 0
            pm_learning_audit["alpha_setup_action_values"] = []
        except Exception:
            pm_learning_audit["decision_memory_retrieval_initial"] = {
                "tool": "decision_memory_retrieval",
                "status": "unavailable",
                "reason": "decision_memory_retrieval_failed",
            }

    step2_opportunity_scorecard = opportunity_scorecard
    opportunity_scorecard_cfg = (
        (_get_portfolio_manager_config(full_config).get("quality_aware_fusion") or {}).get("opportunity_scorecard") or {}
    )
    # Resolve the exact setup from current evidence only. Formal action values
    # are consumed once, after the complete Step4 pool has been assembled.
    scorecard_alpha_setup_action_values = []
    opportunity_scorecard = build_opportunity_scorecard(
        ticker=ticker,
        analyst_signals=analyst_signals,
        market_confirmation=market_confirmation,
        data_quality_summary=data_quality_summary_for_pm,
        adaptive_policy_state=early_adaptive_policy_state,
        alpha_setup_profiles=alpha_setup_profiles,
        alpha_setup_action_values=scorecard_alpha_setup_action_values,
        signal_collection_contract=signal_collection_contract,
        decision_date=trading_date,
        config=opportunity_scorecard_cfg,
    )
    opportunity_scorecard["preferred_side"] = preferred_side
    for side in ("long", "short"):
        step2_row = (
            step2_opportunity_scorecard.get(side)
            if isinstance(step2_opportunity_scorecard.get(side), dict)
            else {}
        )
        step4_row = opportunity_scorecard.get(side) if isinstance(opportunity_scorecard.get(side), dict) else {}
        for field in (
            "side_priority",
            "ticker_side_priority",
            "side_priority_semantics_version",
            "side_priority_meaning",
            "side_priority_is_not_capital_rank",
            "side_priority_is_not_trade_authority",
        ):
            if field in step2_row:
                step4_row[field] = step2_row[field]
    fusion_context["opportunity_scorecard"] = opportunity_scorecard
    if db and config_id:
        try:
            candidate_sides_for_exact: list[str] = []
            current_position_side_for_learning = (
                "long" if current_lots_for_control > 0
                else "short" if current_lots_for_control < 0
                else ""
            )
            for side_name in (
                target_side_for_confirmation,
                str(opportunity_scorecard.get("preferred_side") or ""),
                current_position_side_for_learning,
                "long",
                "short",
            ):
                side_name = str(side_name or "").lower()
                if side_name not in {"long", "short"} or side_name in candidate_sides_for_exact:
                    continue
                side_card = (
                    opportunity_scorecard.get(side_name)
                    if isinstance(opportunity_scorecard.get(side_name), dict)
                    else {}
                )
                score = float(side_card.get("score") or 0.0) if side_card else 0.0
                opportunity_state = str(side_card.get("opportunity_state") or "").lower()
                candidate_like = (
                    side_name == target_side_for_confirmation
                    or side_name == str(opportunity_scorecard.get("preferred_side") or "").lower()
                    or side_name == current_position_side_for_learning
                    or score > 0.01
                    or bool(side_card.get("setup_quality_ok"))
                    or opportunity_state in {"tradeable_candidate", "probe_candidate", "watch_for_trigger"}
                )
                if candidate_like:
                    candidate_sides_for_exact.append(side_name)

            exact_alpha_action_values: list[dict] = []
            exact_side_details: list[dict] = []
            for exact_side in candidate_sides_for_exact:
                exact_horizon = _resolve_decision_horizon(
                    analyst_signals,
                    1 if exact_side == "long" else -1,
                )
                exact_regime = _market_regime_from_signals(analyst_signals, exact_side)
                exact_setup_type = _current_canonical_setup_type_from_signals(
                    exact_side,
                    analyst_signals,
                )
                side_memory_result = retrieve_pm_memory(
                    db=pm_memory_db,
                    config_id=config_id,
                    ticker=ticker,
                    side=exact_side,
                    horizon_class=exact_horizon,
                    market_regime=exact_regime,
                    setup_type=(exact_setup_type or None),
                    trading_date=trading_date,
                    limit=12,
                )
                exact_retrieval_failed = any(
                    str(attempt.get("match_level") or "").strip().lower()
                    == "exact_state"
                    and bool(str(attempt.get("error") or "").strip())
                    for attempt in (
                        side_memory_result.get("retrieval_attempts") or []
                    )
                    if isinstance(attempt, dict)
                )
                if exact_setup_type and exact_retrieval_failed:
                    raise RuntimeError(
                        "pm_exact_setup_action_value_query_failed"
                    )
                side_action_values = side_memory_result.get("action_values") or []
                side_retrieval_detail = {
                    "tool": "decision_memory_retrieval",
                    "effective_memory_summary": side_memory_result.get("effective_memory_summary") or {},
                    "retrieval_attempts": side_memory_result.get("retrieval_attempts") or [],
                    "rejected_or_downgraded": side_memory_result.get("rejected_or_downgraded") or [],
                }
                step4_rejected_or_downgraded = _merge_rejected_or_downgraded(
                    step4_rejected_or_downgraded,
                    side_memory_result.get("rejected_or_downgraded") or [],
                )
                if side_action_values:
                    exact_alpha_action_values.extend(side_action_values)
                exact_side_details.append({
                    "side": exact_side,
                    "horizon_class": exact_horizon,
                    "market_regime": exact_regime,
                    "setup_type": exact_setup_type,
                    "row_count": len(side_action_values or []),
                    "retrieval_detail": side_retrieval_detail,
                })
            pm_learning_audit["pm_action_value_retrieval_attempts"] = exact_side_details
            if exact_side_details:
                effective_memory_summary = {
                    "status": "available" if exact_alpha_action_values else "empty",
                    "consumer_scope": "pm_learning",
                    "effective_row_count": len(exact_alpha_action_values),
                    "quality_first": True,
                    "empty_history_cannot_block_real_history": True,
                    "side_summaries": [
                        detail.get("retrieval_detail", {}).get("effective_memory_summary", {})
                        for detail in exact_side_details
                    ],
                    "rejected_or_downgraded": [
                        item
                        for detail in exact_side_details
                        for item in detail.get("retrieval_detail", {}).get("rejected_or_downgraded", [])
                    ],
                }
                pm_learning_audit["decision_memory_retrieval"] = effective_memory_summary
            if exact_alpha_action_values:
                before_count = len(alpha_setup_action_values)
                alpha_setup_action_values = _append_unique_action_values(
                    alpha_setup_action_values,
                    exact_alpha_action_values,
                )
                pm_learning_audit["pm_exact_alpha_setup_action_value_count"] = len(exact_alpha_action_values)
                pm_learning_audit["pm_exact_alpha_setup_candidate_sides"] = exact_side_details
                pm_learning_audit["pm_exact_alpha_setup_action_values"] = [
                    _compact_alpha_setup_action_value(row) for row in exact_alpha_action_values[:8]
                ]
                pm_learning_audit["pm_exact_alpha_setup_action_value_added_count"] = (
                    len(alpha_setup_action_values) - before_count
                )
                pm_learning_audit["pm_exact_alpha_setup_boundary"] = (
                    "pm_reads_pm_learning_canonical_action_value_by_exact_then_fallback_layers_after_setup_resolution"
                )
                initial_lifecycle_learning_router["post_step3_exact_rows_added_before_step4_consumption"] = True
                pm_learning_audit["initial_lifecycle_learning_router"] = initial_lifecycle_learning_router
        except Exception as exc:
            raise RuntimeError(
                f"{ticker}: pm_exact_setup_learning_retrieval_failed"
            ) from exc
    if db and config_id:
        try:
            similar_horizon = _resolve_decision_horizon(
                analyst_signals,
                1 if target_side_for_confirmation == "long" else -1 if target_side_for_confirmation == "short" else 0,
            )
            similar_regime = _market_regime_from_signals(analyst_signals, target_side_for_confirmation)
            preferred_side_for_setup = str(opportunity_scorecard.get("preferred_side") or target_side_for_confirmation)
            preferred_card = (
                opportunity_scorecard.get(preferred_side_for_setup)
                if preferred_side_for_setup in {"long", "short"} and isinstance(opportunity_scorecard.get(preferred_side_for_setup), dict)
                else {}
            )
            best_profile = preferred_card.get("best_alpha_setup_profile") if isinstance(preferred_card.get("best_alpha_setup_profile"), dict) else {}
            similar_setup_type = str(best_profile.get("setup_type") or preferred_card.get("final_state") or "*")
            similar_memory_result = retrieve_pm_memory(
                db=pm_memory_db,
                config_id=config_id,
                ticker=ticker,
                side=(preferred_side_for_setup if preferred_side_for_setup in {"long", "short"} else target_side_for_confirmation),
                trading_date=trading_date,
                sector=fusion_context.get("sector"),
                horizon_class=similar_horizon,
                market_regime=similar_regime,
                setup_type=similar_setup_type,
                include_similar=True,
                limit=6,
            )
            similar_alpha_action_values = similar_memory_result.get("action_values") or []
            if similar_alpha_action_values:
                similar_diagnostics: list[dict] = []
                for similar_row in similar_alpha_action_values:
                    compact = _compact_alpha_setup_action_value(similar_row)
                    if compact:
                        compact["reason"] = "similar_or_weak_prior_diagnostic_only"
                        compact["diagnostic_only"] = True
                        similar_diagnostics.append(compact)
                step4_rejected_or_downgraded = _merge_rejected_or_downgraded(
                    step4_rejected_or_downgraded,
                    list(similar_memory_result.get("rejected_or_downgraded") or [])
                    + similar_diagnostics,
                )
                similar_summary = similar_memory_result.get("effective_memory_summary") or {}
                effective_memory_summary = {
                    **effective_memory_summary,
                    "similar_memory_summary": similar_summary,
                    "effective_row_count": len(alpha_setup_action_values),
                    "quality_first": True,
                    "empty_history_cannot_block_real_history": True,
                }
                pm_learning_audit["similar_alpha_setup_action_value_count"] = len(similar_alpha_action_values)
                pm_learning_audit["similar_alpha_setup_action_values"] = [
                    _compact_alpha_setup_action_value(row) for row in similar_alpha_action_values[:6]
                ]
                pm_learning_audit["similar_alpha_setup_retrieval"] = {
                    "tool": "decision_memory_retrieval",
                    "effective_memory_summary": similar_summary,
                    "retrieval_attempts": similar_memory_result.get("retrieval_attempts") or [],
                    "rejected_or_downgraded": similar_memory_result.get("rejected_or_downgraded") or [],
                }
                pm_learning_audit["similar_alpha_setup_boundary"] = (
                    "strict_history_only_prior_not_trade_authority"
                )
                initial_lifecycle_learning_router["post_step3_similar_rows_diagnostic_only"] = True
                pm_learning_audit["initial_lifecycle_learning_router"] = initial_lifecycle_learning_router
        except Exception as exc:
            pass
    step4_rejected_or_downgraded = _merge_rejected_or_downgraded(
        step4_rejected_or_downgraded,
        _select_rejected_pm_prior_action_values(alpha_setup_action_values),
    )
    scorecard_alpha_setup_action_values = _formal_pm_learning_action_values(alpha_setup_action_values)
    opportunity_scorecard = build_opportunity_scorecard(
        ticker=ticker,
        analyst_signals=analyst_signals,
        market_confirmation=market_confirmation,
        data_quality_summary=data_quality_summary_for_pm,
        adaptive_policy_state=early_adaptive_policy_state,
        alpha_setup_profiles=alpha_setup_profiles,
        alpha_setup_action_values=scorecard_alpha_setup_action_values,
        signal_collection_contract=signal_collection_contract,
        decision_date=trading_date,
        config=opportunity_scorecard_cfg,
    )
    opportunity_scorecard["preferred_side"] = preferred_side
    for side in ("long", "short"):
        step2_row = (
            step2_opportunity_scorecard.get(side)
            if isinstance(step2_opportunity_scorecard.get(side), dict)
            else {}
        )
        step4_row = opportunity_scorecard.get(side) if isinstance(opportunity_scorecard.get(side), dict) else {}
        for field in (
            "side_priority",
            "ticker_side_priority",
            "side_priority_semantics_version",
            "side_priority_meaning",
            "side_priority_is_not_capital_rank",
            "side_priority_is_not_trade_authority",
        ):
            if field in step2_row:
                step4_row[field] = step2_row[field]
    fusion_context["opportunity_scorecard"] = opportunity_scorecard
    initial_lifecycle_learning_router["scorecard_consumed_count"] = len(scorecard_alpha_setup_action_values)
    initial_lifecycle_learning_router["formal_pool_complete_before_first_consumption"] = True
    initial_lifecycle_learning_router["formal_pool_frozen_after_scorecard"] = True
    pm_learning_audit["initial_lifecycle_learning_router"] = initial_lifecycle_learning_router
    alpha_setup_action_values = scorecard_alpha_setup_action_values
    holding_control_cfg = _get_holding_rebalance_config(full_config)
    learned_open_seed = {}
    learned_open_seed_applied = False
    if (
        abs(position_risk.optimal_position_ratio) <= 1e-12
        and abs(current_ticker_exposure) <= 1e-12
    ):
        learned_open_seed = _positive_open_action_value_seed(
            ticker=ticker,
            alpha_setup_action_values=scorecard_alpha_setup_action_values,
            analyst_signals=analyst_signals,
            opportunity_scorecard=opportunity_scorecard,
            market_confirmation=market_confirmation,
            full_config=full_config,
            max_position_ratio=max_position_ratio,
        )
        learned_side = str((learned_open_seed or {}).get("side") or "").lower()
        learned_ratio = _safe_float((learned_open_seed or {}).get("seed_position_ratio"), 0.0)
        if learned_side in {"long", "short"} and learned_ratio > 0:
            position_risk.optimal_position_ratio = _signed_abs(learned_side, learned_ratio)
            learned_open_seed_applied = True
            position_risk.justification += (
                f"\n[Action-value open seed: {learned_side} same-scope positive open reward "
                f"with current evidence -> target ratio {position_risk.optimal_position_ratio:.2%}]"
            )
            pass
    scorecard_probe_side, scorecard_probe_ratio, scorecard_probe_row = _scorecard_probe_seed(
        opportunity_scorecard=opportunity_scorecard,
        control=holding_control_cfg,
    )
    scorecard_probe_seed_applied = False
    scorecard_conditional_monitor_seed_applied = False
    scorecard_probe_seed_not_applied: dict = {}
    scorecard_conditional_monitor = bool(
        scorecard_probe_side in {"long", "short"}
        and _scorecard_conditional_monitor_candidate(scorecard_probe_row)
    )
    if (
        abs(position_risk.optimal_position_ratio) <= 1e-12
        and abs(current_ticker_exposure) <= 1e-12
        and scorecard_probe_side in {"long", "short"}
    ):
        position_risk.optimal_position_ratio = scorecard_probe_ratio
        if scorecard_conditional_monitor:
            scorecard_conditional_monitor_seed_applied = True
        else:
            scorecard_probe_seed_applied = True
        position_risk.justification += (
            f"\n[Opportunity {'conditional monitor' if scorecard_conditional_monitor else 'probe'} seed: scorecard {scorecard_probe_side} "
            f"{scorecard_probe_row.get('final_state')} score={scorecard_probe_row.get('score')} "
            f"-> target ratio {scorecard_probe_ratio:.2%}]"
        )
        pass

    control_reasons: list[str] = []
    control_notes: list[str] = []
    control_diagnostics: dict = {}
    if scorecard_conditional_monitor:
        control_reasons.append("pm_watch_for_trigger_probe_cap")
        control_notes.append(
            f"{ticker} scorecard routed clean {scorecard_probe_side} watch-for-trigger opportunity "
            f"to conditional monitor probe: score={scorecard_probe_row.get('score')}, "
            f"ratio={scorecard_probe_ratio:.2%}"
        )
        control_diagnostics["conditional_monitor_probe_seed"] = {
            "side": scorecard_probe_side,
            "ratio": float(scorecard_probe_ratio),
            "scorecard": scorecard_probe_row,
            "status": (
                "candidate_routed_to_conditional_monitor"
                if scorecard_conditional_monitor_seed_applied
                else "candidate_preserved_for_post_control_evaluation"
            ),
            "not_product_rule": True,
            "soft_probe_only": True,
            "requires_final_contract_authority": True,
            "requires_intraday_confirmation": True,
        }
    elif scorecard_probe_seed_applied:
        control_reasons.append("scorecard_current_tradeable_probe_seed")
        control_notes.append(
            f"{ticker} scorecard converted qualified {scorecard_probe_side} opportunity to probe seed: "
            f"state={scorecard_probe_row.get('final_state')}, score={scorecard_probe_row.get('score')}, "
            f"ratio={scorecard_probe_ratio:.2%}"
        )
        control_diagnostics["scorecard_current_tradeable_probe_seed"] = {
            "side": scorecard_probe_side,
            "ratio": float(scorecard_probe_ratio),
            "scorecard": scorecard_probe_row,
            "not_product_rule": True,
            "soft_probe_only": True,
        }
    elif scorecard_probe_seed_not_applied:
        control_diagnostics["scorecard_current_tradeable_probe_seed"] = scorecard_probe_seed_not_applied
    if learned_open_seed_applied:
        selected_row = learned_open_seed.get("row") if isinstance(learned_open_seed.get("row"), dict) else {}
        selected_evidence = learned_open_seed.get("evidence") if isinstance(learned_open_seed.get("evidence"), dict) else {}
        control_reasons.append("positive_open_action_value_seed")
        control_notes.append(
            f"{ticker} same-scope positive open action-value seeded current-evidence candidate: "
            f"side={learned_open_seed.get('side')}, ratio={position_risk.optimal_position_ratio:.2%}, "
            f"reward_mean={_safe_float(selected_row.get('reward_mean'), 0.0):.0f}"
        )
        control_diagnostics["positive_open_action_value_seed"] = {
            "enabled": True,
            "decision": "seed_candidate",
            "target_side": learned_open_seed.get("side"),
            "seed_position_ratio": float(position_risk.optimal_position_ratio),
            "selected_action_value": _compact_alpha_setup_action_value(selected_row),
            "current_evidence": {
                key: selected_evidence.get(key)
                for key in (
                    "scorecard_state",
                    "strong_realtime_evidence",
                    "strong_market_confirmation",
                    "technical_entry_timing_supports_side",
                    "technical_opposes_side",
                    "has_tradeable_support",
                    "has_entry_invalidation",
                    "has_position_exit_boundary",
                    "current_confirmation_score",
                    "independent_support_count",
                )
            },
            "not_product_rule": True,
            "does_not_bypass_final_contract_authority": True,
        }
    control_reasons.extend(directional_reasons)
    control_notes.extend(directional_notes)
    control_diagnostics.update(directional_diagnostics)
    control_reasons.extend(bq_reasons)
    control_notes.extend(bq_notes)
    control_diagnostics.update(bq_diagnostics)
    control_block_reason = None
    pm_risk_gate_output = None
    strategy_memory = {}
    adaptive_policy_state = list(early_adaptive_policy_state or [])
    provisional_policy_state = []
    pm_risk_gate = PMRiskGate(full_config)

    if pm_risk_gate.enabled:
        pm_risk_gate_config = full_config.get("pm_risk_gate") or {}
        feedback_config = pm_risk_gate_config.get("attribution_feedback", {}) or {}
        legacy_trade_config = full_config.get("trade_frequency_control", {}) or {}
        pm_risk_gate_lookback = int(
            feedback_config.get(
                "lookback_trades",
                legacy_trade_config.get("lookback_trades", 30),
            )
        )
        pm_risk_gate_side = _target_side_from_ratio(position_risk.optimal_position_ratio)
        recent_side_performance = {}
        recent_conditional_performance = {}
        if db and config_id and pm_risk_gate_side in {"long", "short"}:
            decision_horizon = _resolve_decision_horizon(
                analyst_signals,
                1 if pm_risk_gate_side == "long" else -1,
            )
            market_regime_key = _market_regime_from_signals(analyst_signals, pm_risk_gate_side)
            setup_type_key = _current_canonical_setup_type_from_signals(
                pm_risk_gate_side,
                analyst_signals,
            )
            recent_side_performance = db.get_futures_trade_pair_performance(
                config_id=config_id,
                ticker=ticker,
                side=pm_risk_gate_side,
                trading_date=trading_date,
                lookback_trades=pm_risk_gate_lookback,
            )
            if hasattr(db, "get_futures_conditional_trade_performance"):
                recent_conditional_performance = db.get_futures_conditional_trade_performance(
                    config_id=config_id,
                    ticker=ticker,
                    side=pm_risk_gate_side,
                    trading_date=trading_date,
                    signal_combo=list(signal_combo),
                    lookback_trades=pm_risk_gate_lookback,
                    include_rollover=False,
                )
            pm_risk_gate_memory_result = retrieve_pm_memory(
                db=pm_memory_db,
                config_id=config_id,
                ticker=ticker,
                side=pm_risk_gate_side,
                horizon_class=decision_horizon,
                market_regime=market_regime_key,
                setup_type=setup_type_key,
                sector=fusion_context.get("sector") if isinstance(fusion_context, dict) else None,
                signal_combo=list(signal_combo),
                trading_date=trading_date,
                include_strategy_memory=bool((full_config.get("strategy_memory", {}) or {}).get("enabled", False)),
                include_adaptive_policy_state=True,
                include_provisional_policy_state=True,
                limit=12,
            )
            strategy_memory = pm_risk_gate_memory_result.get("strategy_memory") or {}
            adaptive_policy_state = pm_risk_gate_memory_result.get("adaptive_policy_state") or []
            adaptive_policy_safety_trace = pm_risk_gate_memory_result.get("adaptive_policy_safety_trace") or adaptive_policy_safety_trace
            provisional_policy_state = pm_risk_gate_memory_result.get("provisional_policy_state") or []
            pm_learning_audit["decision_memory_retrieval_policy"] = {
                "tool": "decision_memory_retrieval",
                "side": pm_risk_gate_side,
                "horizon_class": decision_horizon,
                "market_regime": market_regime_key,
                "setup_type": setup_type_key,
                "effective_memory_summary": pm_risk_gate_memory_result.get("effective_memory_summary") or {},
                "retrieval_attempts": pm_risk_gate_memory_result.get("retrieval_attempts") or [],
                "rejected_or_downgraded": pm_risk_gate_memory_result.get("rejected_or_downgraded") or [],
                "scorecard_rerun": False,
                "boundary": "policy_memory_controls_pm_risk_gate_and_trace_without_rerunning_step3",
            }

        analyst_payload = [
            signal.model_dump() if hasattr(signal, "model_dump") else dict(signal)
            for signal in analyst_signals
        ]
        pm_risk_gate_input = PMRiskGateInput(
            ticker=ticker,
            trading_date=trading_date,
            config_id=config_id,
            analyst_signals=analyst_payload,
            signal_combo=list(signal_combo),
            raw_long_score=long_scores,
            raw_short_score=short_scores,
            raw_target_side=pm_risk_gate_side,
            raw_position_ratio=position_risk.optimal_position_ratio,
            current_position_ratio=current_ticker_exposure,
            signal_strength=signal_strength,
            market_confirmation=market_confirmation,
            fundamental_quality=fundamental_quality,
            recent_ticker_side_performance=recent_side_performance,
            recent_conditional_performance=recent_conditional_performance,
            provisional_policy_state=provisional_policy_state,
            risk_level=risk_level.value,
            full_config=full_config,
        )
        pm_risk_gate_output = pm_risk_gate.plan(pm_risk_gate_input)
        before_ratio = position_risk.optimal_position_ratio
        if pm_risk_gate_output.decision == "block":
            if _same_sign(before_ratio, current_ticker_exposure):
                position_risk.optimal_position_ratio = current_ticker_exposure
            else:
                position_risk.optimal_position_ratio = 0.0
            control_reasons.extend(pm_risk_gate_output.reasons)
            control_reasons.append("pm_risk_gate_block")
            control_notes.extend(pm_risk_gate_output.notes)
            control_notes.append(
                f"trade pm risk gate blocked new {pm_risk_gate_output.target_side} exposure: "
                f"{before_ratio:.2%}->{position_risk.optimal_position_ratio:.2%}"
            )
        elif pm_risk_gate_output.decision == "reduce_only":
            if abs(current_ticker_exposure) > 1e-12 and _same_sign(before_ratio, current_ticker_exposure):
                position_risk.optimal_position_ratio = min(
                    abs(before_ratio),
                    abs(current_ticker_exposure),
                ) * (1.0 if current_ticker_exposure > 0 else -1.0)
            else:
                position_risk.optimal_position_ratio = 0.0
            control_reasons.extend(pm_risk_gate_output.reasons)
            control_reasons.append("pm_risk_gate_reduce_only")
            control_notes.extend(pm_risk_gate_output.notes)
            control_notes.append(
                f"trade pm risk gate reduce-only {pm_risk_gate_output.target_side}: "
                f"{before_ratio:.2%}->{position_risk.optimal_position_ratio:.2%}"
            )
        elif pm_risk_gate_output.decision in {"reduce", "scale_down", "probe_only"}:
            position_risk.optimal_position_ratio = _apply_trade_plan_multiplier(
                target_ratio=position_risk.optimal_position_ratio,
                current_ratio=current_ticker_exposure,
                multiplier=pm_risk_gate_output.position_ratio_multiplier,
            )
            if (
                pm_risk_gate_output.decision in {"scale_down", "probe_only"}
                and abs(position_risk.optimal_position_ratio) <= 1e-12
                and abs(before_ratio) > 1e-12
                and abs(current_ticker_exposure) <= 1e-12
            ):
                audit_cfg = (pm_risk_gate_config.get("quality_gate") or {}) if isinstance(pm_risk_gate_config, dict) else {}
                floor_ratio = max(0.0, _safe_float(audit_cfg.get("soft_probe_floor_ratio"), 0.005))
                cap_ratio = max(floor_ratio, _safe_float(audit_cfg.get("soft_probe_max_ratio"), 0.010))
                floor_candidate = _probe_ratio_from_soft_gate(
                    side=pm_risk_gate_output.target_side,
                    current_ratio=current_ticker_exposure,
                    raw_ratio=before_ratio,
                    cap_ratio=cap_ratio,
                    floor_ratio=floor_ratio,
                )
                if abs(floor_candidate) > 1e-12:
                    position_risk.optimal_position_ratio = floor_candidate
                    control_reasons.append("pm_risk_gate_soft_probe_floor")
                    control_notes.append(
                        f"trade pm risk gate soft {pm_risk_gate_output.decision} retained real probe floor: "
                        f"{before_ratio:.2%}->{floor_candidate:.2%}"
                    )
            control_reasons.extend(pm_risk_gate_output.reasons)
            if abs(position_risk.optimal_position_ratio) <= 1e-12 and abs(before_ratio) > 1e-12:
                control_reasons.append("pm_risk_gate_scale_to_zero")
            control_notes.extend(pm_risk_gate_output.notes)
            control_notes.append(
                f"trade pm risk gate {pm_risk_gate_output.decision} {pm_risk_gate_output.target_side} ratio "
                f"{before_ratio:.2%}->{position_risk.optimal_position_ratio:.2%}"
            )
        else:
            control_reasons.extend(pm_risk_gate_output.reasons)
            control_notes.extend(pm_risk_gate_output.notes)
        pm_risk_gate_payload = (
            pm_risk_gate_output.model_dump() if hasattr(pm_risk_gate_output, "model_dump") else dict(pm_risk_gate_output)
        )
        control_diagnostics["pm_risk_gate"] = pm_risk_gate_payload
    else:
        position_risk.optimal_position_ratio, reasons, notes = _apply_market_confirmation_control(
            position_ratio=position_risk.optimal_position_ratio,
            current_ratio=current_ticker_exposure,
            signal_strength=signal_strength,
            market_confirmation=market_confirmation,
            full_config=full_config,
        )
        control_reasons.extend(reasons)
        control_notes.extend(notes)

        position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_trade_frequency_control(
            db=db,
            config_id=config_id,
            ticker=ticker,
            trading_date=trading_date,
            position_ratio=position_risk.optimal_position_ratio,
            current_ratio=current_ticker_exposure,
            signal_combo=signal_combo,
            market_confirmation=market_confirmation,
            full_config=full_config,
        )
        control_reasons.extend(reasons)
        control_notes.extend(notes)
        control_diagnostics.update(diagnostics)

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_market_data_gap_control(
        ticker=ticker,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        signal_strength=signal_strength,
        market_confirmation=market_confirmation,
        full_config=full_config,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    prospective_margin_rate = float(
        contract_info['margin_rate_long']
        if position_risk.optimal_position_ratio >= 0
        else contract_info['margin_rate_short']
    )
    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_drawdown_and_ticker_loss_control(
        db=db,
        config_id=config_id,
        ticker=ticker,
        trading_date=trading_date,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        current_margin_ratio=current_margin_ratio,
        margin_rate=prospective_margin_rate,
        market_confirmation=market_confirmation,
        signal_combo=signal_combo,
        strategy_memory=strategy_memory,
        analyst_signals=analyst_signals,
        full_config=full_config,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_adaptive_policy_position_control(
        ticker=ticker,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        analyst_signals=analyst_signals,
        market_confirmation=market_confirmation,
        full_config=full_config,
        adaptive_policy_state=adaptive_policy_state,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
        db=db,
        config_id=config_id,
        ticker=ticker,
        trading_date=trading_date,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        current_margin_ratio=current_margin_ratio,
        margin_rate=prospective_margin_rate,
        max_position_ratio=max_position_ratio,
        market_confirmation=market_confirmation,
        full_config=full_config,
        signal_combo=signal_combo,
        strategy_memory=strategy_memory,
        adaptive_policy_state=adaptive_policy_state,
        analyst_signals=analyst_signals,
        pre_control_reasons=control_reasons,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_mature_alpha_release_control(
        ticker=ticker,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        analyst_signals=analyst_signals,
        market_confirmation=market_confirmation,
        full_config=full_config,
        adaptive_policy_state=adaptive_policy_state,
        max_position_ratio=max_position_ratio,
        margin_rate=prospective_margin_rate,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_fast_candidate_alpha_probe_control(
        ticker=ticker,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        analyst_signals=analyst_signals,
        market_confirmation=market_confirmation,
        full_config=full_config,
        adaptive_policy_state=adaptive_policy_state,
        max_position_ratio=max_position_ratio,
        margin_rate=prospective_margin_rate,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_opportunity_quality_position_control(
        ticker=ticker,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        opportunity_scorecard=opportunity_scorecard,
        full_config=full_config,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    conditional_monitor_seed = (
        control_diagnostics.get("conditional_monitor_probe_seed")
        if isinstance(control_diagnostics.get("conditional_monitor_probe_seed"), dict)
        else {}
    )
    alpha_control_input_ratio = float(position_risk.optimal_position_ratio or 0.0)
    alpha_candidate_evaluation_only = bool(
        abs(alpha_control_input_ratio) <= 1e-12
        and abs(current_ticker_exposure) <= 1e-12
        and str(conditional_monitor_seed.get("side") or "").lower() in {"long", "short"}
        and abs(_safe_float(conditional_monitor_seed.get("ratio"), 0.0)) > 1e-12
    )
    if alpha_candidate_evaluation_only:
        alpha_control_input_ratio = _signed_abs(
            str(conditional_monitor_seed.get("side") or "").lower(),
            _safe_float(conditional_monitor_seed.get("ratio"), 0.0),
        )
    alpha_control_ratio, reasons, notes, diagnostics = _apply_alpha_setup_ev_position_control(
        ticker=ticker,
        position_ratio=alpha_control_input_ratio,
        current_ratio=current_ticker_exposure,
        opportunity_scorecard=opportunity_scorecard,
        alpha_setup_profiles=alpha_setup_profiles,
        alpha_setup_action_values=alpha_setup_action_values,
        analyst_signals=analyst_signals,
        market_confirmation=market_confirmation,
        full_config=full_config,
        max_position_ratio=max_position_ratio,
    )
    if not alpha_candidate_evaluation_only:
        position_risk.optimal_position_ratio = alpha_control_ratio
    elif isinstance(diagnostics.get("alpha_setup_ev_fusion"), dict):
        diagnostics["alpha_setup_ev_fusion"]["candidate_evaluation_only"] = True
        diagnostics["alpha_setup_ev_fusion"]["does_not_restore_unconditional_position"] = True
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_pretrade_invalidation_control(
        ticker=ticker,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        max_position_ratio=max_position_ratio,
        analyst_signals=analyst_signals,
        full_config=full_config,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_winning_template_continuation_control(
        ticker=ticker,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        current_position=current_position,
        alpha_setup_action_values=alpha_setup_action_values,
        analyst_signals=analyst_signals,
        market_confirmation=market_confirmation,
        opportunity_scorecard=opportunity_scorecard,
        full_config=full_config,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
        ticker=ticker,
        trading_date=trading_date,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        current_position=current_position,
        analyst_signals=analyst_signals,
        long_scores=long_scores,
        short_scores=short_scores,
        market_confirmation=market_confirmation,
        full_config=full_config,
        fusion_context=fusion_context,
        risk_level=risk_level,
        adaptive_policy_state=adaptive_policy_state,
        prior_control_reasons=control_reasons,
        opening_fac_context=opening_fac_context,
        current_price=float(current_price),
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    if control_notes:
        position_risk.justification += "\n" + "\n".join(f"[Strategy control: {note}]" for note in control_notes)
        pass

    if (
        abs(pre_control_ratio) > 1e-12
        and abs(position_risk.optimal_position_ratio) <= 1e-12
        and current_lots_for_control == 0
        and control_reasons
    ):
        control_block_reason = control_reasons[-1]

    target_exposure = position_risk.optimal_position_ratio
    new_net_exposure = current_net_exposure - current_ticker_exposure + target_exposure

    max_net_exposure, symmetric_scaling, net_exposure_cap_mode = _resolve_net_exposure_control(
        full_config,
        control_diagnostics,
    )
    control_diagnostics["net_exposure_control"] = {
        "max_net_exposure": float(max_net_exposure),
        "cap_mode": net_exposure_cap_mode,
        "projected_net_exposure_before_cap": float(new_net_exposure),
    }

    # Apply a symmetric net-exposure cap before translating the target into lots.
    if new_net_exposure > max_net_exposure:
        net_exposure_without_ticker = current_net_exposure - current_ticker_exposure
        scale_factor = (max_net_exposure - net_exposure_without_ticker) / target_exposure if target_exposure > 0 else 0
        original_ratio = position_risk.optimal_position_ratio
        position_risk.optimal_position_ratio *= max(0, scale_factor)
        position_risk.justification += (
            f"\n[Net exposure cap: projected net exposure {new_net_exposure:.1%} exceeds +{max_net_exposure:.1%}; "
            f"position ratio scaled to {position_risk.optimal_position_ratio:.2%}]"
        )
        pass
    elif new_net_exposure < -max_net_exposure:
        net_exposure_without_ticker = current_net_exposure - current_ticker_exposure
        scale_factor = (-max_net_exposure - net_exposure_without_ticker) / target_exposure if target_exposure < 0 else 0
        original_ratio = position_risk.optimal_position_ratio

        if symmetric_scaling:
            position_risk.optimal_position_ratio = -abs(original_ratio) * max(0, scale_factor)
        else:
            position_risk.optimal_position_ratio *= min(0, scale_factor)

        position_risk.justification += (
            f"\n[Net exposure cap: projected net exposure {new_net_exposure:.1%} exceeds -{max_net_exposure:.1%}; "
            f"position ratio scaled to {position_risk.optimal_position_ratio:.2%}]"
        )
        pass

    # In DANGER, new exposure is blocked when no position already exists.
    if risk_level == RiskLevel.DANGER:
        current_lots_check = portfolio.positions[ticker].shares if ticker in portfolio.positions else 0
        if current_lots_check == 0:
            # No new exposure is allowed in DANGER without an existing position.
            # Hold is mandatory in DANGER when no position exists.
            pass
            hold_decision = FuturesDecision(
                ticker=ticker,
                action=FuturesAction.HOLD,
                lots=0,
                price=current_price,
                settle_price=settle_price,
                margin_rate=contract_info['margin_rate_long'],
                contract_multiplier=multiplier,
                contract_code=contract_code,
                justification=f"DANGER risk state (cashflow ratio={cashflow_ratio*100:.1f}%) with no existing position; forcing HOLD.",
            )
            return _phase1_return_with_pm_state(
                config_id=config_id,
                full_config=full_config,
                portfolio=portfolio,
                ticker=ticker,
                trading_date=trading_date,
                contract_code=contract_code,
                decision=hold_decision,
                morning_price_context=morning_price_context,
                analyst_signals=analyst_signals,
                pm_state_update=_build_blocked_pm_memory_state_update(
                    ticker=ticker,
                    current_lots=0,
                    target_lots=0,
                    reason="danger_zone_ban",
                    authority_type="risk_block",
                    account_equity=account_equity,
                    signal_collection_contract=signal_collection_contract,
                    execution_contract_fields={
                        "candidate_status": "blocked",
                        "candidate_block_reason": "danger_zone_ban",
                    },
                ),
            )

    target_value = account_equity * position_risk.optimal_position_ratio

    target_lots = int(target_value / (current_price * multiplier))

    is_long_target = target_lots >= 0
    margin_rate = contract_info[
        'margin_rate_long' if is_long_target else 'margin_rate_short'
    ]

    # Cap the target by remaining margin availability.
    margin_available = remaining_margin
    margin_required = current_price * abs(target_lots) * multiplier * margin_rate

    if margin_required > margin_available:
        max_lots = int(margin_available / (current_price * multiplier * margin_rate))
        pass
        target_lots = max_lots if is_long_target else -max_lots

    current_lots = 0
    if ticker in portfolio.positions:
        current_lots = portfolio.positions[ticker].shares

    preserved_existing_lot = False
    target_lots, preserved_existing_lot = _preserve_existing_lot_when_hold_ratio_survives(
        target_lots=target_lots,
        current_lots=current_lots,
        target_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        control_reasons=control_reasons,
    )
    if preserved_existing_lot:
        preserved_side = "long" if target_lots > 0 else "short"
        margin_rate = contract_info[
            'margin_rate_long' if preserved_side == "long" else 'margin_rate_short'
        ]
        target_value = current_price * target_lots * multiplier
        margin_required = current_price * abs(target_lots) * multiplier * margin_rate
        position_risk.optimal_position_ratio = current_ticker_exposure
        new_net_exposure = current_net_exposure
        control_reasons.append("existing_lot_hold_preserved")
        control_notes.append(
            f"{ticker} existing {preserved_side} position preserved at {target_lots} lot(s): "
            f"the final lifecycle decision kept ratio {current_ticker_exposure:.2%}, so price-only "
            "integer lot translation must not create a reduction."
        )
        control_diagnostics["existing_lot_hold_preserved"] = {
            "current_lots": int(current_lots),
            "target_lots": int(target_lots),
            "current_ratio": float(current_ticker_exposure),
            "reason": "unchanged_hold_ratio_preserves_current_lots",
        }

    probe_release, probe_release_detail = _qualified_real_probe_release(
        control_reasons=control_reasons,
        control_diagnostics=control_diagnostics,
    )
    control_diagnostics["real_probe_release_qualification"] = probe_release_detail
    if probe_release:
        control_reasons.append("real_probe_positive_or_strong_confirmation_release")
        control_notes.append(
            f"{ticker} soft probe preserved as real candidate: "
            f"soft_blocks={probe_release_detail.get('soft_blocks')}, "
            f"confirmation={probe_release_detail.get('confirmation_score', 0.0):.2f}, "
            f"qualified_positive={probe_release_detail.get('qualified_positive_expectancy')}, "
            f"strong_realtime={probe_release_detail.get('strong_realtime_evidence')}"
        )
    analyst_tradeable_probe, analyst_tradeable_probe_detail = _qualified_analyst_tradeable_probe_candidate(
        analyst_signals=analyst_signals,
        target_side=_target_side_from_ratio(pre_control_ratio),
        control_reasons=control_reasons,
        control_diagnostics=control_diagnostics,
        account_equity=account_equity,
        current_price=float(current_price),
        multiplier=float(multiplier),
        margin_rate=float(margin_rate),
        margin_available=float(margin_available),
    )
    control_diagnostics["analyst_tradeable_probe_candidate"] = analyst_tradeable_probe_detail
    if analyst_tradeable_probe:
        control_reasons.append("analyst_tradeable_probe_candidate")
        control_notes.append(
            f"{ticker} same-side analyst tradeable candidate preserved for controlled probe: "
            f"side={analyst_tradeable_probe_detail.get('target_side')}, "
            f"analysts={[item.get('analyst') for item in analyst_tradeable_probe_detail.get('matched_analysts', [])]}"
        )
    alpha_ev = control_diagnostics.get("alpha_setup_ev_fusion")
    alpha_ev_blocks_real_probe = bool(
        isinstance(alpha_ev, dict)
        and alpha_ev.get("repeat_loss_without_new_evidence")
        and not alpha_ev.get("strong_realtime_evidence")
    )
    minimum_probe_candidate_ratio = _minimum_real_probe_candidate_ratio(
        current_ratio=position_risk.optimal_position_ratio,
        pre_control_ratio=pre_control_ratio,
        probe_release=probe_release,
        analyst_tradeable_probe=analyst_tradeable_probe,
    )
    conditional_monitor_seed = (
        control_diagnostics.get("conditional_monitor_probe_seed")
        if isinstance(control_diagnostics.get("conditional_monitor_probe_seed"), dict)
        else {}
    )
    if (
        abs(minimum_probe_candidate_ratio) <= 1e-12
        and abs(current_ticker_exposure) <= 1e-12
        and str(conditional_monitor_seed.get("side") or "").lower() in {"long", "short"}
    ):
        minimum_probe_candidate_ratio = _signed_abs(
            str(conditional_monitor_seed.get("side") or "").lower(),
            _safe_float(conditional_monitor_seed.get("ratio"), 0.0),
        )
    if (
        _should_attempt_minimum_real_probe(
            current_lots=current_lots,
            target_lots=target_lots,
            target_ratio=minimum_probe_candidate_ratio,
            control_reasons=control_reasons,
            probe_release=probe_release,
            analyst_tradeable_probe=analyst_tradeable_probe,
            alpha_ev_blocks_real_probe=alpha_ev_blocks_real_probe,
        )
    ):
        probe_side = _target_side_from_ratio(minimum_probe_candidate_ratio)
        if probe_side in {"long", "short"}:
            probe_margin_rate = contract_info[
                'margin_rate_long' if probe_side == "long" else 'margin_rate_short'
            ]
            one_lot_notional = current_price * multiplier
            one_lot_position_ratio = one_lot_notional / max(float(account_equity), 1.0)
            one_lot_margin = one_lot_notional * probe_margin_rate
            signed_one_lot_ratio = one_lot_position_ratio if probe_side == "long" else -one_lot_position_ratio
            projected_net_after_probe = current_net_exposure - current_ticker_exposure + signed_one_lot_ratio
            probe_risk = _one_lot_probe_risk_check(
                ticker=ticker,
                probe_side=probe_side,
                current_price=float(current_price),
                multiplier=float(multiplier),
                margin_rate=float(probe_margin_rate),
                account_equity=float(account_equity),
                morning_price_context=morning_price_context,
                control_reasons=control_reasons,
                control_diagnostics=control_diagnostics,
                full_config=full_config,
            )
            control_diagnostics["probe_one_lot_risk_budget"] = probe_risk
            if (
                probe_risk.get("passed", True)
                and one_lot_margin <= margin_available
                and one_lot_position_ratio <= max_position_ratio + 1e-12
                and abs(projected_net_after_probe) <= max_net_exposure + 1e-12
            ):
                target_lots = 1 if probe_side == "long" else -1
                is_long_target = target_lots >= 0
                margin_rate = probe_margin_rate
                margin_required = one_lot_margin
                target_value = one_lot_notional if probe_side == "long" else -one_lot_notional
                position_risk.optimal_position_ratio = signed_one_lot_ratio
                new_net_exposure = projected_net_after_probe
                control_reasons.append("minimum_one_lot_probe")
                control_notes.append(
                    f"{ticker} soft-gated setup converted to minimum real probe lot: "
                    f"side={probe_side}, ratio={position_risk.optimal_position_ratio:.2%}, "
                    f"margin={one_lot_margin:.2f}"
                )
            elif not probe_risk.get("passed", True):
                position_risk.optimal_position_ratio = 0.0
                target_value = 0.0
                target_lots = 0
                margin_required = 0.0
                new_net_exposure = current_net_exposure - current_ticker_exposure
                control_reasons.append("minimum_one_lot_probe_risk_budget_block")
                control_notes.append(
                    f"{ticker} soft-gated {probe_side} probe kept as watchlist: one-lot risk budget failed "
                    f"{probe_risk.get('failures')}; notional_ratio={probe_risk.get('notional_ratio', 0.0):.2%}, "
                    f"margin_ratio={probe_risk.get('margin_ratio', 0.0):.2%}, "
                    f"limit_risk={probe_risk.get('limit_risk_ratio', 0.0):.2%}"
                )
    elif (
        current_lots == 0
        and target_lots == 0
        and abs(position_risk.optimal_position_ratio) > 1e-12
        and (
            (
                any(reason in _MINIMUM_REAL_PROBE_DISQUALIFIED_REASONS for reason in control_reasons)
                and not probe_release
            )
            or alpha_ev_blocks_real_probe
        )
        and not any(_hard_zero_reason(reason) for reason in control_reasons)
    ):
        control_reasons.append("real_probe_qualification_not_met")
        control_notes.append(
            f"{ticker} soft-gated idea kept as watchlist: real probe qualification not met; "
            f"reasons={sorted(set(control_reasons) & _MINIMUM_REAL_PROBE_DISQUALIFIED_REASONS)}"
        )
        control_diagnostics["real_probe_qualification"] = {
            "decision": "watch_for_trigger",
            "blocked_reasons": sorted(set(control_reasons) & _MINIMUM_REAL_PROBE_DISQUALIFIED_REASONS),
            "alpha_ev_blocks_real_probe": alpha_ev_blocks_real_probe,
            "money_objective": "avoid_repeating_negative_or_watch_for_trigger_real_lot",
            "does_not_block_qualified_positive_or_fast_candidate_probe": True,
        }
        position_risk.optimal_position_ratio = 0.0
        target_value = 0.0
        target_lots = 0
        margin_required = 0.0
        new_net_exposure = current_net_exposure - current_ticker_exposure

    conditional_monitor_side = _target_side_from_ratio(minimum_probe_candidate_ratio)
    conditional_monitor_margin_rate = (
        contract_info['margin_rate_long' if conditional_monitor_side == "long" else 'margin_rate_short']
        if conditional_monitor_side in {"long", "short"}
        else margin_rate
    )
    conditional_monitor_plan = _conditional_monitor_probe_seed_plan(
        ticker=ticker,
        current_lots=current_lots,
        target_lots=target_lots,
        target_ratio=minimum_probe_candidate_ratio,
        current_ticker_exposure=current_ticker_exposure,
        current_net_exposure=current_net_exposure,
        account_equity=account_equity,
        current_price=float(current_price),
        multiplier=float(multiplier),
        margin_rate=float(conditional_monitor_margin_rate),
        margin_available=float(margin_available),
        max_position_ratio=float(max_position_ratio),
        max_net_exposure=float(max_net_exposure),
        morning_price_context=morning_price_context,
        control_reasons=control_reasons,
        control_diagnostics=control_diagnostics,
        full_config=full_config,
    )
    control_diagnostics["conditional_monitor_probe_plan"] = conditional_monitor_plan
    if conditional_monitor_plan.get("allowed"):
        target_lots = int(conditional_monitor_plan.get("target_lots") or 0)
        margin_rate = conditional_monitor_margin_rate
        margin_required = float(conditional_monitor_plan.get("margin_required") or 0.0)
        target_value = float(conditional_monitor_plan.get("target_value") or 0.0)
        position_risk.optimal_position_ratio = float(
            conditional_monitor_plan.get("signed_one_lot_ratio") or 0.0
        )
        new_net_exposure = float(conditional_monitor_plan.get("new_net_exposure") or 0.0)
        if "conditional_trigger_authority" not in control_reasons:
            control_reasons.append("conditional_trigger_authority")
        seed = (
            control_diagnostics.get("conditional_monitor_probe_seed")
            if isinstance(control_diagnostics.get("conditional_monitor_probe_seed"), dict)
            else {}
        )
        if seed:
            seed["status"] = "applied_to_final_contract"
            seed["target_lots"] = int(target_lots)
            seed["requires_intraday_confirmation"] = True
            seed["can_execute_without_intraday_trigger"] = False
        control_notes.append(
            f"{ticker} watch-for-trigger candidate preserved as conditional monitor contract: "
            f"side={conditional_monitor_plan.get('probe_side')}, lots={target_lots}, "
            "requires_intraday_confirmation=True"
        )

    # Cooling period: block voluntary reductions within two days of opening unless hard loss or risk pressure applies.
    cooling_period_note = ""
    if (
        current_position is not None
        and current_lots != 0
        and risk_level not in (RiskLevel.DANGER, RiskLevel.EMERGENCY)
        and getattr(current_position, "entry_date", None)
    ):
        held_days = (
            _safe_int(opening_fac_context.get("held_trading_days"), 0)
            if opening_fac_context
            else _days_held(current_position.entry_date, trading_date)
        )
        reducing_exposure = (
            target_lots == 0
            or (current_lots > 0 and target_lots < current_lots)
            or (current_lots < 0 and target_lots > current_lots)
            or (current_lots > 0 and target_lots < 0)
            or (current_lots < 0 and target_lots > 0)
        )
        lifecycle_exit_required = _is_lifecycle_exit_required_reason(control_reasons)
        if held_days < 2 and reducing_exposure and not lifecycle_exit_required:
            unrealized_loss_ratio = (
                float(getattr(current_position, "unrealized_pnl", 0.0) or 0.0)
                / float(getattr(current_position, "margin_used", 0.0) or 1.0)
                if float(getattr(current_position, "margin_used", 0.0) or 0.0) > 0
                else 0.0
            )
            if unrealized_loss_ratio > -0.05:
                pass
                target_lots = current_lots
                current_ratio = _signed_position_ratio(current_position, account_equity)
                position_risk.optimal_position_ratio = current_ratio
                target_value = account_equity * current_ratio
                margin_required = current_price * abs(target_lots) * multiplier * margin_rate
                cooling_period_note = (
                    f"[Cooling period: held {held_days} day(s), voluntary reduction blocked "
                    f"unless loss exceeds 5% of margin used]"
                )
        elif held_days < 2 and reducing_exposure and lifecycle_exit_required:
            pass

    if risk_level == RiskLevel.EMERGENCY:
        if current_lots > 0:
            emergency_decision = FuturesDecision(
                ticker=ticker,
                action=FuturesAction.CLOSE_LONG,
                lots=abs(current_lots),
                price=current_price,
                settle_price=settle_price,
                margin_rate=contract_info['margin_rate_long'],
                contract_multiplier=multiplier,
                contract_code=contract_code,
                justification=f"EMERGENCY risk level: flatten long position in {ticker}"
            )
        elif current_lots < 0:
            emergency_decision = FuturesDecision(
                ticker=ticker,
                action=FuturesAction.CLOSE_SHORT,
                lots=abs(current_lots),
                price=current_price,
                settle_price=settle_price,
                margin_rate=contract_info['margin_rate_short'],
                contract_multiplier=multiplier,
                contract_code=contract_code,
                justification=f"EMERGENCY risk level: flatten short position in {ticker}"
            )
        else:
            emergency_decision = FuturesDecision(
                ticker=ticker,
                action=FuturesAction.HOLD,
                lots=0,
                price=current_price,
                settle_price=settle_price,
                margin_rate=contract_info['margin_rate_long'],
                contract_multiplier=multiplier,
                contract_code=contract_code,
                justification=f"EMERGENCY risk level: block new positions in {ticker}"
            )

        pass
        pm_state = _build_pm_memory_state(
            config_id=config_id,
            full_config=full_config,
            portfolio=portfolio,
            ticker=ticker,
            trading_date=trading_date,
            contract_code=contract_code,
            decision=emergency_decision,
            morning_price_context=morning_price_context,
            analyst_signals=analyst_signals,
            pm_state_update=_build_blocked_pm_memory_state_update(
                ticker=ticker,
                current_lots=int(current_lots or 0),
                target_lots=0,
                reason="emergency_risk_flatten",
                authority_type="risk_exit" if current_lots else "risk_block",
                account_equity=account_equity,
                signal_collection_contract=signal_collection_contract,
                execution_contract_fields={
                    "candidate_status": "blocked",
                    "candidate_block_reason": "emergency_risk_flatten",
                },
            ),
        )
        return pm_state

    final_entry_authority = {}
    if contract_requires_full_market_capital_rank(
        {"current_lots": current_lots, "target_lots": target_lots}
    ):
        has_final_entry_authority, final_entry_authority = _final_contract_authority(
            control_reasons=control_reasons,
            control_diagnostics=control_diagnostics,
            full_config=full_config,
        )
        final_entry_authority["target_lots_before_gate"] = int(target_lots)
        final_entry_authority["target_ratio_before_gate"] = float(position_risk.optimal_position_ratio)
        step4_capital_side = "long" if target_lots > 0 else "short"
        step4_capital_row = (
            opportunity_scorecard.get(step4_capital_side)
            if isinstance(opportunity_scorecard.get(step4_capital_side), dict)
            else {}
        )
        if step4_capital_row and final_entry_authority.get("capital_layer"):
            step4_capital_row["capital_layer"] = final_entry_authority.get("capital_layer")
            step4_capital_row["capital_ratio_source"] = final_entry_authority.get("capital_ratio_source")
            step4_capital_row["alpha_scale_eligible"] = bool(
                final_entry_authority.get("alpha_scale_eligible")
            )
        control_diagnostics["final_action_authority"] = final_entry_authority
        if (
            final_entry_authority.get("requires_authority")
            and not has_final_entry_authority
            and final_entry_authority.get("authority_type") != "exploration_probe"
        ):
            control_reasons.append("final_contract_authority_not_met")
            control_notes.append(
                f"{ticker} incremental risk kept at current exposure: final trading authority not met; "
                f"weak_markers={final_entry_authority.get('weak_markers')}, "
                f"hard_blocks={final_entry_authority.get('hard_blocks')}, "
                f"negative_profile={final_entry_authority.get('negative_profile')}, "
                f"qualified_positive={final_entry_authority.get('qualified_positive')}, "
                f"strong_current={final_entry_authority.get('strong_current_evidence')}"
            )
            position_risk.optimal_position_ratio = current_ticker_exposure
            target_value = account_equity * current_ticker_exposure
            target_lots = current_lots
            margin_required = float(getattr(current_position, "margin_used", 0.0) or 0.0)
            new_net_exposure = current_net_exposure
            control_block_reason = "final_contract_authority_not_met"

    if contract_requires_full_market_capital_rank(
        {"current_lots": current_lots, "target_lots": target_lots}
    ):
        (
            target_lots,
            target_value,
            margin_required,
            new_net_exposure,
            adjusted_position_ratio,
            margin_rate,
            budget_block_reason,
        ) = _apply_position_budget_policy_for_new_entry(
            ticker=ticker,
            target_lots=target_lots,
            current_lots=current_lots,
            current_price=float(current_price),
            multiplier=float(multiplier),
            margin_rate=float(margin_rate),
            account_equity=float(account_equity),
            margin_available=float(margin_available),
            max_net_exposure=float(max_net_exposure),
            current_net_exposure=float(current_net_exposure),
            current_ticker_exposure=float(current_ticker_exposure),
            final_entry_authority=final_entry_authority,
            full_config=full_config,
            control_reasons=control_reasons,
            control_notes=control_notes,
            control_diagnostics=control_diagnostics,
        )
        position_risk.optimal_position_ratio = adjusted_position_ratio
        if budget_block_reason:
            control_block_reason = budget_block_reason

    if contract_requires_full_market_capital_rank(
        {"current_lots": current_lots, "target_lots": target_lots}
    ):
        target_side = "long" if target_lots > 0 else "short"
        try:
            selected_execution_evidence = _select_execution_evidence_payload(
                _execution_signal_payloads(analyst_signals, target_side),
                target_side=target_side,
                conditional_path=bool(
                    final_entry_authority.get("conditional_trigger_authority")
                ),
            )
            selected_scorecard_row = (
                opportunity_scorecard.get(target_side)
                if isinstance(opportunity_scorecard.get(target_side), dict)
                else {}
            )
            if selected_scorecard_row:
                selected_scorecard_row["trigger_quality_score"] = max(
                    0.0,
                    min(
                        1.0,
                        _safe_float(
                            selected_execution_evidence.get("trigger_quality_score"),
                            0.0,
                        ),
                    ),
                )
        except ValueError as exc:
            if str(exc) != "pm_execution_evidence_not_found":
                raise
            control_block_reason = "no_qualified_execution_evidence"
            if control_block_reason not in control_reasons:
                control_reasons.append(control_block_reason)
            position_risk.optimal_position_ratio = current_ticker_exposure
            target_value = account_equity * current_ticker_exposure
            target_lots = current_lots
            margin_required = float(
                getattr(current_position, "margin_used", 0.0) or 0.0
            )
            new_net_exposure = current_net_exposure
            final_entry_authority.update(
                {
                    "authority_type": "watchlist_only",
                    "authority_decision": control_block_reason,
                    "open_action_evidence": False,
                    "strong_current_evidence": False,
                    "conditional_trigger_authority": False,
                    "requires_intraday_confirmation": False,
                    "can_execute_without_intraday_trigger": False,
                }
            )

    lots_to_trade = abs(target_lots - current_lots) if abs(target_lots - current_lots) > 0 else 0

    # Track why no tradable lots remain after risk and margin constraints.
    lots_to_trade_reason = None
    if lots_to_trade == 0:
        if control_block_reason:
            lots_to_trade_reason = control_block_reason
        elif target_lots == 0 and control_reasons and abs(pre_control_ratio) > 1e-12:
            lots_to_trade_reason = control_reasons[-1]
        elif target_lots == 0:
            lots_to_trade_reason = "deterministic_neutral"
        elif abs(target_lots - current_lots) < 0.01:
            lots_to_trade_reason = "position_matched"
        elif margin_required > margin_available:
            lots_to_trade_reason = "margin_insufficient"
        elif risk_level == RiskLevel.DANGER:
            lots_to_trade_reason = "danger_zone_ban"
        elif abs(new_net_exposure) > max_net_exposure:
            lots_to_trade_reason = "net_exposure_limit"
        else:
            lots_to_trade_reason = "unknown"
    if cooling_period_note:
        lots_to_trade_reason = "cooling_period"

    recommendation_intent = recommendation_intent_from_lots(current_lots, target_lots)
    pre_open_action = FuturesAction(recommendation_intent["action"])
    pre_open_lots = int(recommendation_intent["lots"])
    lifecycle_memory_state = {
        "ticker": ticker,
        "current_lots": int(current_lots or 0),
        "target_lots": int(target_lots or 0),
        "lots_delta": int((target_lots or 0) - (current_lots or 0)),
        "reason_codes": sorted(set(str(item) for item in control_reasons if item)),
        "conditional_trigger_authority": bool(
            final_entry_authority.get("conditional_trigger_authority")
            if isinstance(final_entry_authority, dict)
            else "conditional_trigger_authority" in control_reasons
        ),
        "requires_intraday_confirmation": bool(
            final_entry_authority.get("requires_intraday_confirmation")
            if isinstance(final_entry_authority, dict)
            else "conditional_trigger_authority" in control_reasons
        ),
        "can_execute_without_intraday_trigger": (
            bool(final_entry_authority.get("can_execute_without_intraday_trigger"))
            if isinstance(final_entry_authority, dict)
            and final_entry_authority.get("can_execute_without_intraday_trigger") is not None
            else False if "conditional_trigger_authority" in control_reasons else True
        ),
    }
    lifecycle_memory_state["primary_lifecycle_action_port"] = primary_lifecycle_action_port.get("pm_lifecycle_action_port")
    lifecycle_memory_state["requires_full_market_rank"] = primary_lifecycle_action_port.get("requires_full_market_rank")
    alpha_setup_action_values, lifecycle_memory_audit = _audit_frozen_step4_pm_memory(
        contract=lifecycle_memory_state,
        alpha_setup_action_values=alpha_setup_action_values,
    )
    lifecycle_memory_audit["rejected_or_downgraded"] = _merge_rejected_or_downgraded(
        lifecycle_memory_audit.get("rejected_or_downgraded"),
        step4_rejected_or_downgraded,
    )
    final_lifecycle_route_action_values = alpha_setup_action_values
    lifecycle_learning_router = route_lifecycle_learning(
        lifecycle_port=str(primary_lifecycle_action_port.get("pm_lifecycle_action_port") or ""),
        action_values=final_lifecycle_route_action_values,
    )
    decision_learning_indices = {
        int(index)
        for index in (
            lifecycle_learning_router.get("decision_learning_indices")
            or lifecycle_learning_router.get("accepted_indices")
            or []
        )
        if isinstance(index, int)
    }
    trigger_profile_indices = {
        int(index)
        for index in (lifecycle_learning_router.get("trigger_profile_indices") or [])
        if isinstance(index, int)
    }
    consumed_learning_indices = decision_learning_indices | trigger_profile_indices
    step4_consumed_action_values = [
        row
        for index, row in enumerate(final_lifecycle_route_action_values)
        if index in consumed_learning_indices
    ]
    alpha_setup_action_values = final_lifecycle_route_action_values
    lifecycle_learning_router["contract_consumed_indices"] = sorted(consumed_learning_indices)
    lifecycle_learning_router["contract_consumed_count"] = len(step4_consumed_action_values)
    lifecycle_learning_router["contract_consumption_boundary"] = (
        "Step4 lifecycle router is internal routing only; Step6 final contract reroutes "
        "decision_learning_rows from the final contract lifecycle"
    )
    lifecycle_memory_audit["primary_lifecycle_action_port"] = primary_lifecycle_action_port
    lifecycle_memory_audit["pm_lifecycle_learning_router"] = lifecycle_learning_router
    pm_learning_audit["final_action_memory_requirements"] = lifecycle_memory_audit.get("memory_requirements", {})
    pm_learning_audit["final_action_memory_retrieval"] = lifecycle_memory_audit
    pm_learning_audit["primary_lifecycle_action_port"] = primary_lifecycle_action_port
    pm_learning_audit["pm_lifecycle_learning_router"] = lifecycle_learning_router
    pm_learning_audit["alpha_setup_action_value_count"] = len(alpha_setup_action_values)
    pm_learning_audit["alpha_setup_action_values"] = alpha_setup_action_values[:8]
    control_diagnostics["final_action_memory_requirements"] = lifecycle_memory_audit.get("memory_requirements", {})
    control_diagnostics["final_action_memory_retrieval"] = lifecycle_memory_audit
    control_diagnostics["primary_lifecycle_action_port"] = primary_lifecycle_action_port
    control_diagnostics["pm_lifecycle_learning_router"] = lifecycle_learning_router

    plan_snapshot = _build_pm_decision_context(
        target_lots=target_lots,
        current_price=current_price,
        position_ratio=position_risk.optimal_position_ratio,
        risk_level=risk_level,
        long_scores=long_scores,
        short_scores=short_scores,
        margin_rate=margin_rate,
        current_lots=current_lots,
        analyst_signals=analyst_signals,
        final_entry_authority=final_entry_authority,
        trading_date=trading_date,
        recommendation_intent=recommendation_intent,
        control_reasons=control_reasons,
        alpha_setup_action_values=alpha_setup_action_values,
        ticker=ticker,
    )
    plan_snapshot["current_lots_before_open"] = int(current_lots)
    plan_snapshot["primary_lifecycle_action_port"] = primary_lifecycle_action_port
    plan_snapshot["pm_lifecycle_learning_router"] = lifecycle_learning_router
    plan_snapshot["target_value"] = float(target_value)
    plan_snapshot["account_equity"] = float(account_equity)
    plan_snapshot["current_net_exposure"] = float(current_net_exposure)
    plan_snapshot["current_ticker_exposure"] = float(current_ticker_exposure)
    plan_snapshot["projected_net_exposure"] = float(new_net_exposure)
    plan_snapshot["analyst_signal_combo"] = list(signal_combo)
    plan_snapshot["adaptive_fusion"] = fusion_context
    plan_snapshot["decision_horizon"] = _resolve_decision_horizon(analyst_signals, target_lots)
    plan_snapshot["execution_horizon"] = "short"
    plan_snapshot["validation_horizon"] = plan_snapshot["decision_horizon"]
    plan_snapshot["business_quality_summary"] = summarize_business_quality(analyst_signals)
    plan_snapshot["signal_collection_contract"] = signal_collection_contract
    plan_snapshot["opportunity_scorecard"] = opportunity_scorecard
    plan_snapshot["ticker_side_selection"] = ticker_side_selection_result
    plan_snapshot["capital_allocation_reason"] = ticker_side_selection_result.get("capital_allocation_reason", {})
    plan_snapshot["position_quality_controls"] = {
        "opportunity_quality_position_sizing": (
            control_diagnostics.get("opportunity_quality_position_sizing")
            if isinstance(control_diagnostics.get("opportunity_quality_position_sizing"), dict)
            else {}
        ),
        "winning_template_continuation": (
            control_diagnostics.get("winning_template_continuation")
            if isinstance(control_diagnostics.get("winning_template_continuation"), dict)
            else {}
        ),
        "alpha_setup_ev_fusion": (
            control_diagnostics.get("alpha_setup_ev_fusion")
            if isinstance(control_diagnostics.get("alpha_setup_ev_fusion"), dict)
            else {}
        ),
        "trade_churn_cost_control": (
            control_diagnostics.get("churn_cost_control")
            if isinstance(control_diagnostics.get("churn_cost_control"), dict)
            else {}
        ),
    }
    rebalance_action_type = str(recommendation_intent.get("action_type") or "rebalance")
    plan_snapshot["rebalance_summary"] = {
        "action_type": rebalance_action_type,
        "current_lots": int(current_lots),
        "target_lots": int(target_lots),
        "lots_delta": int(target_lots - current_lots),
        "holding_days": (
            _safe_int(opening_fac_context.get("held_trading_days"), 0)
            if opening_fac_context
            else _days_held(getattr(current_position, "entry_date", None), trading_date)
            if current_position and current_lots != 0
            else None
        ),
        "turnover_notional_estimate": float(lots_to_trade * current_price * multiplier),
        "reason": lots_to_trade_reason or (control_reasons[-1] if control_reasons else "target_plan"),
        "control_reasons": sorted(set(control_reasons)),
    }
    pm_state_update = _build_pm_memory_state_update(
        ticker=ticker,
        current_lots=current_lots,
        target_lots=target_lots,
        position_ratio=position_risk.optimal_position_ratio,
        margin_required=margin_required,
        account_equity=account_equity,
        lots_to_trade=lots_to_trade,
        lots_to_trade_reason=lots_to_trade_reason,
        recommendation_intent=recommendation_intent,
        final_entry_authority=final_entry_authority,
        control_reasons=control_reasons,
    )
    plan_snapshot["release_block_diagnostics"] = _build_release_block_diagnostics(
        ticker=ticker,
        decision_state=pm_state_update,
        final_entry_authority=final_entry_authority,
        control_reasons=control_reasons,
        lots_to_trade_reason=lots_to_trade_reason,
        control_diagnostics=control_diagnostics,
        opportunity_scorecard=opportunity_scorecard,
        market_confirmation=market_confirmation,
        full_config=full_config,
    )
    holding_diagnostics = {}
    if isinstance(control_diagnostics, dict):
        holding_diagnostics = (
            control_diagnostics.get("holding_rebalance_control")
            if isinstance(control_diagnostics.get("holding_rebalance_control"), dict)
            else {}
        )
    if holding_diagnostics:
        plan_snapshot["rebalance_summary"]["lifecycle_decision"] = holding_diagnostics.get("decision")
        plan_snapshot["rebalance_summary"]["lifecycle_classification"] = holding_diagnostics.get("lifecycle_classification")
        plan_snapshot["rebalance_summary"]["position_pnl_ratio"] = holding_diagnostics.get("position_pnl_ratio")
        plan_snapshot["rebalance_summary"]["market_confirmation_score"] = holding_diagnostics.get("confirmation_score")
        plan_snapshot["rebalance_summary"]["current_side_strength"] = holding_diagnostics.get("current_side_strength")
        plan_snapshot["rebalance_summary"]["target_side_strength"] = holding_diagnostics.get("reversal_or_exit_strength")
        plan_snapshot["position_lifecycle_trace"] = {
            "lifecycle_classification": holding_diagnostics.get("lifecycle_classification"),
            "decision": holding_diagnostics.get("decision"),
            "position_pnl_ratio": holding_diagnostics.get("position_pnl_ratio"),
            "holding_days": holding_diagnostics.get("holding_days"),
            "current_side": holding_diagnostics.get("current_side"),
            "target_side": holding_diagnostics.get("target_side"),
            "new_loss_revalidation_due": holding_diagnostics.get("new_loss_revalidation_due"),
            "new_loss_revalidation_failed": holding_diagnostics.get("new_loss_revalidation_failed"),
            "loss_revalidation_due": holding_diagnostics.get("loss_revalidation_due"),
            "loss_revalidation_failed": holding_diagnostics.get("loss_revalidation_failed"),
            "horizon_losing_hold_failed": holding_diagnostics.get("horizon_losing_hold_failed"),
            "market_confirmation_score": holding_diagnostics.get("confirmation_score"),
            "current_side_strength": holding_diagnostics.get("current_side_strength"),
            "reversal_or_exit_strength": holding_diagnostics.get("reversal_or_exit_strength"),
            "verification_basis": (
                "losing positions must be rechecked with current-day signal, market state, "
                "and invalidation evidence; historical memory alone is not position authority"
            ),
        }
    alpha_protect_records = [
        row
        for row in (adaptive_policy_state or [])
        if str(row.get("policy_type") or "") == "alpha_promotion"
        and str(row.get("policy_action") or "").lower() in {"protect", "allow"}
    ]
    tail_loss_records = [
        row
        for row in (adaptive_policy_state or [])
        if str(row.get("policy_type") or "") == "tail_loss_sentinel"
    ]
    scorecard_target_side = _target_side_from_ratio(position_risk.optimal_position_ratio)
    scorecard_side = (
        opportunity_scorecard.get(scorecard_target_side)
        if scorecard_target_side in {"long", "short"} and isinstance(opportunity_scorecard.get(scorecard_target_side), dict)
        else {}
    )
    scorecard_state = str(scorecard_side.get("final_state") or "").lower()
    pm_decision_state = classify_pm_decision_state(
        current_lots=current_lots,
        target_lots=target_lots,
        scorecard_state=scorecard_state,
        has_alpha_protect_records=bool(alpha_protect_records),
    )
    plan_snapshot["pm_decision_state"] = pm_decision_state
    plan_snapshot["research_memory_maturity"] = {
        "candidate_hypothesis_count": pm_learning_audit.get("candidate_hypothesis_count", 0),
        "validated_hypothesis_count": pm_learning_audit.get("validated_hypothesis_count", 0),
        "alpha_promotion_records": len(alpha_protect_records),
        "tail_loss_sentinel_records": len(tail_loss_records),
        "candidate_hypotheses_prior_only": True,
        "mature_alpha_can_scale_only_with_current_confirmation": True,
        "adaptive_policy_safety": adaptive_policy_safety_trace,
    }
    if pm_learning_audit.get("enabled"):
        if lots_to_trade_reason == "position_matched" and pm_learning_audit.get("candidate_hypothesis_count", 0):
            pm_learning_audit["position_matched_boundary"] = (
                "candidate_hypotheses_not_counted_as_position_support"
            )
        control_diagnostics["exploratory_learning_context"] = pm_learning_audit
    if pm_risk_gate_output:
        pm_risk_gate_payload = (
            pm_risk_gate_output.model_dump() if hasattr(pm_risk_gate_output, "model_dump") else dict(pm_risk_gate_output)
        )
        plan_snapshot["pm_risk_gate"] = pm_risk_gate_payload
    else:
        pm_risk_gate_payload = None
    plan_snapshot["loss_template_research_trace"] = {
        "adaptive_policy_scope": _adaptive_policy_trace(adaptive_policy_state),
        "adaptive_policy_safety": adaptive_policy_safety_trace,
        "research_mode": "scope_pattern_not_ticker_blacklist",
        "scope_keys": [
            "ticker",
            "side",
            "setup_type",
            "horizon_class",
            "market_regime",
            "data_combination",
        ],
        "requires_same_scope_validation": True,
    }
    plan_snapshot["learning_to_position_trace"] = _learning_to_position_trace(
        pm_learning_audit=pm_learning_audit,
        adaptive_policy_state=adaptive_policy_state,
        strategy_memory=strategy_memory,
        current_lots=current_lots,
        target_lots=target_lots,
        pre_open_action=pre_open_action,
        pre_open_lots=pre_open_lots,
        lots_to_trade_reason=lots_to_trade_reason,
        pre_control_ratio=pre_control_ratio,
        final_position_ratio=position_risk.optimal_position_ratio,
        control_reasons=control_reasons,
        holding_diagnostics=holding_diagnostics,
        market_confirmation=market_confirmation,
        pm_risk_gate_payload=pm_risk_gate_payload,
        analyst_signals=analyst_signals,
        opportunity_scorecard=opportunity_scorecard,
        alpha_setup_profiles=alpha_setup_profiles,
        alpha_setup_action_values=alpha_setup_action_values,
    )
    plan_snapshot["pm_landing_consistency_audit"] = _build_pm_landing_consistency_audit(
        ticker=ticker,
        current_lots=current_lots,
        target_lots=target_lots,
        current_position_ratio=current_ticker_exposure,
        final_position_ratio=position_risk.optimal_position_ratio,
        recommendation_intent=recommendation_intent,
        lots_to_trade=lots_to_trade,
        lots_to_trade_reason=lots_to_trade_reason,
        opportunity_scorecard=opportunity_scorecard,
        analyst_signals=analyst_signals,
        pm_learning_audit=pm_learning_audit,
        adaptive_policy_state=adaptive_policy_state,
        alpha_setup_profiles=alpha_setup_profiles,
        alpha_setup_action_values=alpha_setup_action_values,
        pm_risk_gate_payload=pm_risk_gate_payload,
        control_reasons=control_reasons,
        margin_required=margin_required,
        margin_available=margin_available,
        market_confirmation=market_confirmation,
    )
    if control_reasons or control_notes or control_diagnostics:
        plan_snapshot["strategy_controls"] = {
            "reasons": sorted(set(control_reasons)),
            "notes": control_notes,
            "diagnostics": control_diagnostics,
        }
    pm_state_update.update(
        {
            "position_ratio": position_risk.optimal_position_ratio,
            "target_value": target_value,
            "margin_required": margin_required,
            "account_equity": account_equity,
            "margin_rate": margin_rate,
            "current_net_exposure": current_net_exposure,
            "projected_net_exposure": new_net_exposure,
            "current_ticker_exposure": current_ticker_exposure,
            "max_position_ratio": max_position_ratio,
            "max_net_exposure": max_net_exposure,
            "risk_level": risk_level.value,
            "lots_to_trade": lots_to_trade,
            "lots_to_trade_reason": lots_to_trade_reason,
            "recommendation_intent": recommendation_intent,
            "final_entry_authority": final_entry_authority,
            "control_reasons": list(control_reasons),
            "control_diagnostics": control_diagnostics,
            "opportunity_scorecard": opportunity_scorecard,
            "market_confirmation": market_confirmation,
            "alpha_setup_action_values": alpha_setup_action_values,
            "execution_contract_fields": dict(plan_snapshot),
        }
    )

    pre_open_decision = FuturesDecision(
        ticker=ticker,
        action=pre_open_action,
        lots=pre_open_lots,
        price=current_price,
        settle_price=settle_price,
        margin_rate=margin_rate,
        contract_multiplier=multiplier,
        contract_code=contract_code,
        justification=(
            f"{position_risk.justification}\n"
            f"[Pre-open target plan: target_lots={target_lots}, "
            f"target_position_ratio={position_risk.optimal_position_ratio:.2%}, "
            f"lots_delta={target_lots - current_lots}, "
            f"reason={lots_to_trade_reason or 'target_plan'}, "
            f"recommendation_action={pre_open_action.value}, recommendation_lots={pre_open_lots}]"
            + (f"\n{cooling_period_note}" if cooling_period_note else "")
        ),
    )
    pending_state_decision = FuturesDecision(
        ticker=ticker,
        action=FuturesAction.HOLD,
        lots=0,
        price=current_price,
        settle_price=settle_price,
        margin_rate=margin_rate,
        contract_multiplier=multiplier,
        contract_code=contract_code,
        justification=(
            f"{position_risk.justification}\n"
            "[PM six-step state: steps 1-4 complete; final action/lots will be signed "
            "after PM full-market rank and capital deployment.]"
        ),
    )
    pm_state = _build_pm_memory_state(
        config_id=config_id,
        full_config=full_config,
        portfolio=portfolio,
        ticker=ticker,
        trading_date=trading_date,
        contract_code=contract_code,
        decision=pending_state_decision,
        morning_price_context=morning_price_context,
        analyst_signals=analyst_signals,
        plan_snapshot=plan_snapshot,
        pm_state_update=pm_state_update,
        market_confirmation=market_confirmation if market_confirmation.get("enabled") else None,
    )
    return pm_state


def portfolio_agent_futures(state: FundState):
    """Return only the mutable PM memory state before full-market Step5/Step6."""
    return {"pm_state": _run_pm_six_step_decision(state)}


def calculate_long_short_signals(
    ticker: str,
    analyst_signals: list,
    fundamental_basis: float,
    weights: dict = None,
    fusion_context: dict = None,
    full_config: dict = None,
) -> tuple[dict, dict]:
    """
    Combine analyst outputs into comparable long-side and short-side scores.

    Args:
        ticker: Underlying code.
        analyst_signals: Analyst outputs collected for the ticker.
        fundamental_basis: Parsed basis percentage from the fundamental analyst.
        weights: Optional analyst-weight override.

    Returns:
        A pair of dictionaries for long and short scoring.
    """
    long_score = 0.0
    short_score = 0.0
    long_confidence = 0.0
    short_confidence = 0.0

    fusion_context = fusion_context or {}
    full_config = full_config or {}
    quality_summary = fusion_context.get("analyst_quality") or {}

    # Group signals by analyst for weighted fusion.
    signals_by_agent = {}
    for signal in analyst_signals:
        if hasattr(signal, 'agent_name'):
            signals_by_agent[signal.agent_name] = signal

    # Start from the strongest basis-driven directional bias when available.
    fundamental = signals_by_agent.get('fundamental')
    technical = signals_by_agent.get('technical')
    news = signals_by_agent.get('commodity_news')

    if fundamental:
        fundamental_confidence = _effective_signal_confidence(
            fundamental,
            "fundamental",
            quality_summary,
            full_config,
        )
        if fundamental_basis >= 10:
            long_score = 0.95 * fundamental_confidence
            long_confidence = fundamental_confidence
        elif fundamental_basis <= -10:
            short_score = 0.95 * fundamental_confidence
            short_confidence = fundamental_confidence

    # Fall back to weighted multi-analyst fusion when basis alone is not decisive.
    if long_score < 0.9 or short_score < 0.9:
        if weights is None:
            # Default weights lean on fundamentals when basis is strong; otherwise use the balanced mix.
            if abs(fundamental_basis) >= 10:
                weights = {'fundamental': 0.7, 'technical': 0.2, 'commodity_news': 0.1}
            else:
                weights = {'technical': 0.5, 'commodity_news': 0.3, 'fundamental': 0.2}
        # Otherwise, keep the dynamic weights computed above.

        for agent_name, weight in weights.items():
            signal = signals_by_agent.get(agent_name)
            if signal:
                effective_confidence = _effective_signal_confidence(
                    signal,
                    agent_name,
                    quality_summary,
                    full_config,
                )
                if _signal_to_text(signal.signal).upper() == 'BULLISH':
                    long_score += weight * effective_confidence
                elif _signal_to_text(signal.signal).upper() == 'BEARISH':
                    short_score += weight * effective_confidence

        # Average confidence across the active analyst signals.
        active_signals = [s for s in [fundamental, technical, news] if s]
        if active_signals:
            active_confidences = []
            for signal in active_signals:
                agent_name = _normalize_agent_name(signal.agent_name)
                active_confidences.append(
                    _effective_signal_confidence(signal, agent_name, quality_summary, full_config)
                )
            long_confidence = sum(active_confidences) / len(active_confidences)
            short_confidence = long_confidence

    return {
        'score': max(0, long_score),
        'confidence': long_confidence,
        'has_strong_basis': fundamental_basis >= 10,
        'weights_used': weights or {},
        'fusion_sector': fusion_context.get("sector"),
    }, {
        'score': max(0, short_score),
        'confidence': short_confidence,
        'has_strong_basis': fundamental_basis <= -10,
        'weights_used': weights or {},
        'fusion_sector': fusion_context.get("sector"),
    }

def calculate_position_ratio_with_balance(
    ticker: str,
    long_scores: dict,
    short_scores: dict,
    max_position_ratio: float,
    risk_level: RiskLevel,
    full_config: dict
) -> tuple[float, str]:
    """
    Convert blended long/short scores into a signed target position ratio.

    Args:
        ticker: Underlying code.
        long_scores: Aggregated bullish score payload.
        short_scores: Aggregated bearish score payload.
        max_position_ratio: Base sizing anchor for the initial absolute position size.
        risk_level: Current portfolio risk classification.
        full_config: Runtime configuration.

    Returns:
        A tuple of (absolute_ratio, direction).
    """
    position_scaling = get_position_scaling_factor(risk_level, full_config)

    if long_scores['score'] > short_scores['score']:
        # Long bias.
        direction = 'LONG'
        base_ratio = max_position_ratio

        final_ratio = base_ratio * position_scaling * long_scores['confidence']

        final_ratio = min(final_ratio, 0.30)

    elif short_scores['score'] > long_scores['score']:
        # Short bias.
        direction = 'SHORT'
        base_ratio = max_position_ratio

        final_ratio = base_ratio * position_scaling * short_scores['confidence']

        final_ratio = min(final_ratio, 0.30)

    else:
        # No directional edge remains.
        direction = 'NEUTRAL'
        final_ratio = 0

    return final_ratio, direction
