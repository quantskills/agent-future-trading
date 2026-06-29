from __future__ import annotations

"""Shared reason-effect taxonomy for PM, Auditor, and Trader diagnostics.

This module does not introduce new trading rules. It maps existing reason codes
into a small set of effects so dispersed block/cap/probe/watchlist semantics are
auditable before they reach the final trading authority outlet.
"""

from typing import Iterable


HARD_BLOCK_REASONS = {
    "pm_risk_gate_block",
    "pm_risk_gate_reduce_only",
    "pm_opportunity_scorecard_no_trade",
    "pm_text_no_trade_blocks_new_entry",
    "pm_text_no_entry_trigger_blocks_new_entry",
    "pm_text_watchlist_only_blocks_new_entry",
    "danger_zone_ban",
    "net_exposure_limit",
    "margin_insufficient",
    "critical_data_gap",
    "data_price_anomaly",
    "price_anomaly",
    "limit_locked_no_fill",
    "delivery_month_new_entry_block",
    "contract_expiry_hard_block",
    "future_data_contamination",
    "missing_pm_final_action_contract",
    "final_contract_authority_source_mismatch",
    "final_contract_authority_missing_or_not_met",
    "final_contract_authority_not_met",
    "missing_final_contract_authority",
    "final_contract_authority_watchlist_only",
    "final_contract_authority_real_entry_not_allowed",
    "final_action_contract_watch_for_trigger_probe_block",
    "final_contract_authority_probe_lacks_current_evidence",
    "position_budget_authority_not_met",
    "minimum_real_trade_margin_not_reachable",
    "minimum_real_trade_no_feasible_lot",
    "minimum_one_lot_probe_risk_budget_block",
    "exploration_probe_no_feasible_lot",
    "pm_risk_gate_scale_to_zero",
    "market_rule_block",
    "market_rule_or_execution_block",
    "intraday_trigger_not_met",
    "intraday_waiting_for_trigger",
    "limit_locked_no_fill",
    "near_expiry_new_entry_block",
    "delivery_month_new_entry_block",
}

CANDIDATE_REASONS = {
    "pm_watch_for_trigger_probe_cap",
    "scorecard_current_tradeable_probe_seed",
    "conditional_monitor_probe_seed",
}

SOFT_LIMIT_REASONS = {
    "alpha_setup_open_action_value_missing",
    "single_high_quality_probe_only",
    "horizon_consistency_probe_cap",
    "market_confirmation_quality_gate",
    "market_confirmation_conflict",
    "weak_signal_combo_probe_cap",
    "side_performance_probe_cap",
    "business_quality_probe_only",
    "business_quality_deployable",
    "business_quality_observe_or_block",
    "pm_risk_gate_soft_probe_floor",
    "controlled_probe_below_min_entry_kept",
    "unknown_alpha_probe",
    "soft_block_converted_to_probe_only",
    "weak_ticker_side_quality_gate",
    "weak_ticker_side_cap",
    "strategy_memory_weak_block",
    "strict_ticker_side_quality_gate",
    "side_performance_block",
    "conditional_performance_block",
    "adaptive_policy_block",
    "provisional_policy_block",
    "learned_underperformance_block",
    "analyst_quality_low_tradeability",
    "business_quality_below_probe",
    "insufficient_quality_support",
    "low_quality_news_driven_trade",
    "news_only_directional_trade",
    "news_without_fundamental_anchor",
    "cold_start_weak_combo_block",
    "weak_conditional_combo_cap",
    "market_confirmation_soft_limit",
    "market_confirmation_data_gap",
    "trade_frequency_control",
    "trade_churn_cost_control",
    "weak_signal_combo",
    "opportunity_quality_position_sizing",
    "alpha_setup_ev_fusion",
    "market_confirmation_below_probe_threshold",
    "market_confirmation_below_release_threshold",
    "market_confirmation_score_below_probe_threshold",
    "high_quality_learning_evidence_required",
    "confirmation_below_alpha_release_boost_threshold",
    "missing_pretrade_invalidation",
    "missing_explicit_stop_for_alpha_release_boost",
    "generic_memory_cannot_trigger_alpha_release_boost",
    "strategy_memory_watchlist_cap",
    "no_analyst_support_for_target",
    "analyst_signal_conflict",
    "static_side_cap",
    "protected_memory_evidence_rejected",
    "daily_tradeability_watchlist_only",
    "pm_watch_for_trigger_not_tradeable",
    "horizon_consistency_requires_short_timing",
    "minimum_new_entry_threshold",
    "real_probe_qualification_not_met",
    "position_lifecycle_failed",
    "new_position_loss_revalidation_failed",
    "exploration_probe_reconfirm_failed",
    "exploration_probe_reconfirm_reduce",
    "horizon_consistency_failed_losing_hold",
    "position_lifecycle_loss_revalidation_failed",
    "position_lifecycle_probe_expired",
    "minimum_rebalance_threshold",
    "fundamental_anchor_rebalance_cap",
    "reverse_requires_stronger_evidence",
    "ticker_loss_control",
    "drawdown_control",
    "pm_risk_gate_reduce_only",
}

LEARNING_ADJUSTMENT_REASONS = {
    "adaptive_policy_cap",
    "adaptive_policy_protect",
    "adaptive_policy_block",
    "provisional_policy_block",
    "provisional_policy_cap",
    "provisional_policy_probe_only",
    "strategy_memory_weak_block",
    "side_performance_block",
    "conditional_performance_block",
    "learned_underperformance_policy",
    "repeat_loss_watchlist_only",
    "negative_expectancy_cap_or_exit",
    "negative_expectancy_new_entry_watchlist_only",
    "alpha_setup_open_action_value_missing",
    "tail_loss_sentinel",
    "fast_loss_sentinel",
    "loss_template_policy",
    "learned_underperformance_block",
    "strategy_memory_watchlist_cap",
    "winning_template_continuation",
    "winning_template_continuation_protective_reduce",
    "profitable_hold_continuation",
    "position_lifecycle_trend_hold",
    "holding_period_control",
    "existing_lot_hold_preserved",
    "drawdown_recovery_probe",
    "drawdown_control",
    "ticker_loss_control",
    "trade_frequency_control",
    "trade_churn_cost_control",
    "capital_utilization_soft_limit_respected",
    "capital_utilization_guard",
    "capital_utilization_learning",
    "capital_side_performance",
    "hold_exit_action_value_protection",
    "candidate_positive_action_preference",
    "execution_action_value_preference",
}

RELEASE_SIGNAL_REASONS = {
    "conditional_trigger_authority",
    "qualified_positive_expectancy",
    "positive_expectancy_scale",
    "positive_open_action_value_seed",
    "real_probe_positive_or_strong_confirmation_release",
    "fast_candidate_alpha_probe",
    "capital_utilization_memory_protected",
    "capital_utilization_same_side_add_on",
    "mature_alpha_release",
    "mature_alpha_with_invalidation",
    "high_quality_bearish_short_probe",
    "high_quality_news_with_invalidation",
    "high_quality_memory",
    "high_quality_or_triggered_candidate_not_landed",
    "minimum_one_lot_probe",
    "minimum_real_trade_margin_floor_applied",
    "exploration_probe_probe_floor_applied",
    "correct_probe",
}

PROBE_RELEASABLE_SOFT_LIMITS = {
    "alpha_setup_open_action_value_missing",
    "single_high_quality_probe_only",
    "horizon_consistency_probe_cap",
}

WATCHLIST_REQUIRED_REASONS = {
    "repeat_loss_watchlist_only",
    "negative_expectancy_cap_or_exit",
    "negative_expectancy_new_entry_watchlist_only",
    "missing_pretrade_invalidation",
    "pm_text_no_trade_blocks_new_entry",
    "pm_text_no_entry_trigger_blocks_new_entry",
    "pm_text_watchlist_only_blocks_new_entry",
    "missing_final_contract_authority",
    "final_contract_authority_watchlist_only",
    "final_contract_authority_real_entry_not_allowed",
    "final_action_contract_watch_for_trigger_probe_block",
    "final_contract_authority_probe_lacks_current_evidence",
    "daily_tradeability_watchlist_only",
}


def _clean(reason: str | None) -> str:
    return str(reason or "").strip()


def _dedupe(items: Iterable[str]) -> list[str]:
    return sorted({item for item in (_clean(value) for value in items) if item})


def is_hard_block_reason(reason: str, softened_reasons: set[str] | None = None) -> bool:
    text = _clean(reason)
    softened = {_clean(item) for item in (softened_reasons or set())}
    if text in softened:
        return False
    return (
        text in HARD_BLOCK_REASONS
        or text.startswith("hard_")
        or text.startswith("emergency")
    )


def is_hard_zero_reason(reason: str, softened_reasons: set[str] | None = None) -> bool:
    return is_hard_block_reason(reason, softened_reasons=softened_reasons)


def is_soft_limit_reason(reason: str) -> bool:
    text = _clean(reason)
    if is_candidate_reason(text):
        return False
    return (
        text in SOFT_LIMIT_REASONS
        or text.endswith("_probe_cap")
        or text.endswith("_probe_only")
        or text.endswith("_scale_down")
        or text.endswith("_quality_gate")
        or text.endswith("_cap")
    )


def is_candidate_reason(reason: str) -> bool:
    return _clean(reason) in CANDIDATE_REASONS


def is_learning_adjustment_reason(reason: str) -> bool:
    text = _clean(reason)
    return (
        text in LEARNING_ADJUSTMENT_REASONS
        or text.startswith("adaptive_policy_")
        or text.startswith("provisional_policy_")
        or text.startswith("alpha_setup_")
        or "strategy_memory" in text
        or "expectancy" in text
    )


def is_release_signal_reason(reason: str) -> bool:
    text = _clean(reason)
    return text in RELEASE_SIGNAL_REASONS or text.startswith("alpha_release_")


def soft_limit_can_release_probe(reason: str) -> bool:
    return _clean(reason) in PROBE_RELEASABLE_SOFT_LIMITS


def requires_watchlist_reason(reason: str) -> bool:
    return _clean(reason) in WATCHLIST_REQUIRED_REASONS


def _looks_like_trade_effect(reason: str) -> bool:
    text = _clean(reason)
    markers = (
        "block",
        "cap",
        "probe",
        "watchlist",
        "authority",
        "margin",
        "conflict",
        "quality",
        "expectancy",
        "risk",
        "invalidation",
        "trigger",
        "release",
    )
    return bool(text and any(marker in text for marker in markers))


def reason_effect_summary(
    reasons: Iterable[str] | None,
    *,
    softened_reasons: set[str] | None = None,
) -> dict:
    cleaned = _dedupe(reasons or [])
    hard_blocks = [reason for reason in cleaned if is_hard_block_reason(reason, softened_reasons=softened_reasons)]
    candidate_reasons = [reason for reason in cleaned if is_candidate_reason(reason)]
    soft_limits = [
        reason
        for reason in cleaned
        if reason not in hard_blocks and reason not in candidate_reasons and is_soft_limit_reason(reason)
    ]
    learning_adjustments = [reason for reason in cleaned if is_learning_adjustment_reason(reason)]
    release_signals = [reason for reason in cleaned if is_release_signal_reason(reason)]
    known = set(hard_blocks) | set(candidate_reasons) | set(soft_limits) | set(learning_adjustments) | set(release_signals)
    unknown_trade_effects = [
        reason
        for reason in cleaned
        if reason not in known and _looks_like_trade_effect(reason)
    ]
    return {
        "contract": "reason_effects.v1",
        "hard_blocks": hard_blocks,
        "candidate_reasons": candidate_reasons,
        "soft_limits": soft_limits,
        "learning_adjustments": learning_adjustments,
        "release_signals": release_signals,
        "unknown_trade_effects": unknown_trade_effects,
        "hard_zero": bool(hard_blocks),
        "candidate_count": len(candidate_reasons),
        "soft_limit_count": len(soft_limits),
        "learning_adjustment_count": len(learning_adjustments),
        "release_signal_count": len(release_signals),
        "boundary": (
            "hard_blocks may stop new exposure; candidate_reasons only preserve eligibility; "
            "soft_limits can only cap/probe/watchlist; learning_adjustments must be scoped, "
            "expiring, and reversible; release_signals must still pass current evidence and "
            "PM final authority"
        ),
    }
