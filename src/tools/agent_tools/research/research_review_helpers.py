from __future__ import annotations

"""Read-only helpers shared by Phase4 reviewer and researcher learning/report tools.

This module contains formatting, snapshot parsing, trade-pair statistics, and
research-report helper functions. It does not run Phase4, write DB rows, write
artifacts, or create agent permissions.
"""

import json
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from database.artifact_store import load_externalized_json
from util.futures_audit import categorize_no_trade_reason, normalize_no_trade_reason
from util.futures_trade_pairs import build_completed_trade_pairs, summarize_trade_pairs
from util.learning_attribution import (
    learning_effect_counts,
    learning_effects_from_context,
    learning_mechanism_counts,
    learning_mechanisms_from_context,
    learning_tags_from_context,
    summarize_pairs_by_learning_effect,
    summarize_pairs_by_learning_mechanism,
)
from tools.common.contracts import final_action_contract_from_snapshot
from tools.common.evidence_fusion_semantics import build_reviewer_fusion_attribution
from tools.common.final_action_semantics import (
    canonical_action_family,
    canonical_action_value_lane,
    classify_final_action_contract,
    derive_memory_requirements,
    derive_review_expectation,
)
from tools.common.learning_contract import CONTRACT_KEY

ANALYSTS = ("technical", "fundamental", "commodity_news")
DEFAULT_ANALYST_HORIZON = {
    "technical": "short",
    "fundamental": "medium",
    "commodity_news": "event_short",
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
SIDE_BY_SIGNAL = {"Bullish": "long", "BULLISH": "long", "Bearish": "short", "BEARISH": "short"}


def _normalize_date(value) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)


def _futures_account_equity(cash_balance: float, reserved_margin: float) -> float:
    return float(cash_balance or 0.0) + float(reserved_margin or 0.0)


def _expected_settlement_balance_change(settlement_row: Dict[str, Any]) -> float:
    return (
        float(settlement_row.get("daily_pnl") or 0.0)
        - float(settlement_row.get("commission") or 0.0)
        - (
            float(settlement_row.get("current_margin") or 0.0)
            - float(settlement_row.get("previous_margin") or 0.0)
        )
        + float(settlement_row.get("deposit") or 0.0)
        - float(settlement_row.get("withdraw") or 0.0)
    )


def _expected_settlement_equity_change(settlement_row: Dict[str, Any]) -> float:
    return (
        float(settlement_row.get("daily_pnl") or 0.0)
        - float(settlement_row.get("commission") or 0.0)
        + float(settlement_row.get("deposit") or 0.0)
        - float(settlement_row.get("withdraw") or 0.0)
    )


CAPITAL_DEPLOYMENT_REASON_PROFILES: Dict[str, Dict[str, Any]] = {
    "neutral_signal_no_trade": {
        "category": "alpha_signal_shortage",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "Signals are not directional enough to justify more margin.",
        "suggested_action": "Do not add leverage; improve analyst evidence and neutral accountability first.",
    },
    "high_score_signal_shortage": {
        "category": "alpha_signal_shortage",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "No non-flat strategy target survived Phase1.",
        "suggested_action": "Treat this as alpha-capacity limited unless missed opportunities later prove otherwise.",
    },
    "position_matched": {
        "category": "position_already_matched",
        "risk_control_normal": True,
        "alpha_expansion_allowed": "conditional",
        "diagnosis": "The current position already matches the PM target.",
        "suggested_action": "Only allow same-side add-on when confirmation and learned template quality are both strong.",
    },
    "intraday_trigger_not_met": {
        "category": "execution_timing_gate",
        "risk_control_normal": True,
        "alpha_expansion_allowed": "conditional",
        "diagnosis": "Phase2 did not find a valid intraday execution trigger.",
        "suggested_action": "Review missed follow-through before loosening opening-range, chase, or trigger thresholds.",
    },
    "horizon_consistency_requires_short_timing": {
        "category": "strategy_timing_gate",
        "risk_control_normal": True,
        "alpha_expansion_allowed": "conditional",
        "diagnosis": "A medium-term thesis lacked short-term timing confirmation for entry or continued exposure.",
        "suggested_action": "Do not deploy more capital until current technical/intraday evidence and invalidation are explicit.",
    },
    "pm_risk_gate_block": {
        "category": "pm_risk_gate_suppression",
        "risk_control_normal": "depends_on_reasons",
        "alpha_expansion_allowed": "conditional",
        "diagnosis": "The deterministic PM risk gate suppressed the PM target.",
        "suggested_action": "Keep hard-risk blocks; only soften PM risk gate caps for protected/deployable templates with positive evidence.",
    },
    "pm_risk_gate_reduce_only": {
        "category": "pm_risk_gate_suppression",
        "risk_control_normal": "depends_on_reasons",
        "alpha_expansion_allowed": False,
        "diagnosis": "The PM risk gate allowed only risk-reducing exposure.",
        "suggested_action": "Do not increase until the underlying PM risk gate reason is cleared.",
    },
    "minimum_new_entry_threshold": {
        "category": "size_threshold",
        "risk_control_normal": True,
        "alpha_expansion_allowed": "conditional",
        "diagnosis": "The proposed new entry was smaller than the minimum tradable threshold.",
        "suggested_action": "Lower the threshold only for high-confirmation protected/deployable candidates.",
    },
    "cooling_period": {
        "category": "position_lifecycle_guard",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "A newly opened position is inside its cooling period.",
        "suggested_action": "Do not force turnover or add leverage until lifecycle evidence matures.",
    },
    "ticker_loss_control": {
        "category": "risk_control_active",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "Recent ticker-level losses triggered protection.",
        "suggested_action": "Do not add exposure until the loss-control window improves.",
    },
    "learned_underperformance_policy": {
        "category": "learning_risk_control",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "A learned policy or template underperformed its benchmark and capped the target.",
        "suggested_action": "Do not release capital until the learned combo improves or expires.",
    },
    "provisional_policy_probe_only": {
        "category": "learning_risk_control",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "A provisional learning sentinel downgraded the target to probe-only after weak recent evidence.",
        "suggested_action": "Keep the trade as a small probe until the provisional policy expires or future evidence improves.",
    },
    "provisional_policy_cap": {
        "category": "learning_risk_control",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "A provisional learning sentinel capped the target after weak recent evidence.",
        "suggested_action": "Do not expand capital while the provisional cap is active.",
    },
    "soft_block_converted_to_probe_only": {
        "category": "pm_risk_gate_suppression",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "The auditor converted a soft block into probe-only execution instead of allowing full exposure.",
        "suggested_action": "Do not treat this as unused capacity; require stronger current evidence before expansion.",
    },
    "business_quality_probe_only": {
        "category": "alpha_signal_shortage",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "Business-quality checks only supported a probe-sized trade.",
        "suggested_action": "Improve analyst evidence quality before releasing more capital.",
    },
    "weak_ticker_side_history": {
        "category": "learning_risk_control",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "Ticker-side historical evidence is weak, so the candidate was not mature enough for deployment.",
        "suggested_action": "Keep it as a learning/watchlist case until current evidence and future validation improve.",
    },
    "drawdown_control": {
        "category": "risk_control_active",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "Portfolio drawdown control is active.",
        "suggested_action": "Do not raise utilization while drawdown controls are active.",
    },
    "margin_insufficient": {
        "category": "hard_risk_or_capacity_limit",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "Available margin cannot support the requested target.",
        "suggested_action": "Do not override hard margin limits.",
    },
    "net_exposure_limit": {
        "category": "hard_risk_or_capacity_limit",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "Net exposure cap prevented more directional risk.",
        "suggested_action": "Only revisit the cap after portfolio-level risk review.",
    },
    "missing_execution_basis": {
        "category": "data_or_execution_basis",
        "risk_control_normal": False,
        "alpha_expansion_allowed": False,
        "diagnosis": "The open-order phase lacked an executable price basis.",
        "suggested_action": "Fix data/execution basis before drawing strategy-quality conclusions.",
    },
    "soft_cap_binding_or_execution_miss": {
        "category": "soft_cap_or_execution_miss",
        "risk_control_normal": "needs_review",
        "alpha_expansion_allowed": "conditional",
        "diagnosis": "Utilization was below target but the precise blocking reason is mixed or unknown.",
        "suggested_action": "Inspect recommendation-level diagnostics before changing sizing parameters.",
    },
    "target_met": {
        "category": "target_met",
        "risk_control_normal": True,
        "alpha_expansion_allowed": False,
        "diagnosis": "Margin utilization is within the target band.",
        "suggested_action": "No capital-utilization adjustment is needed.",
    },
}

CAPITAL_HARD_RISK_REASON_TOKENS = (
    "drawdown",
    "ticker_loss",
    "margin_insufficient",
    "net_exposure",
    "danger",
    "emergency",
    "weak_block",
    "reduce_only",
)

CAPITAL_NON_RELEASE_NO_TRADE_REASONS = {
    "neutral_signal_no_trade",
    "intraday_trigger_not_met",
    "intraday_opening_range_incomplete",
    "horizon_consistency_requires_short_timing",
    "pm_risk_gate_block",
    "pm_risk_gate_reduce_only",
    "minimum_new_entry_threshold",
    "cooling_period",
    "ticker_loss_control",
    "learned_underperformance_policy",
    "provisional_policy_probe_only",
    "provisional_policy_cap",
    "soft_block_converted_to_probe_only",
    "business_quality_probe_only",
    "weak_ticker_side_history",
    "drawdown_control",
    "margin_insufficient",
    "net_exposure_limit",
    "missing_execution_basis",
}


def _capital_reason_profile(reason: Any) -> Dict[str, Any]:
    key = str(reason or "unknown")
    profile = dict(CAPITAL_DEPLOYMENT_REASON_PROFILES.get(key) or {})
    if not profile:
        profile = {
            "category": "other",
            "risk_control_normal": "unknown",
            "alpha_expansion_allowed": "unknown",
            "diagnosis": "No specialized capital-utilization diagnosis is available.",
            "suggested_action": "Inspect recommendation-level controls before changing parameters.",
        }
    profile["reason"] = key
    return profile


def _sorted_counter_dict(counter: Counter) -> Dict[str, int]:
    return {str(key): int(value) for key, value in counter.most_common()}


def _no_trade_reason_category_counts(no_trade_reason_counter: Counter) -> Dict[str, int]:
    category_counter: Counter = Counter()
    for reason, count in (no_trade_reason_counter or Counter()).items():
        category = categorize_no_trade_reason(reason)["category"]
        category_counter[category] += int(count or 0)
    return _sorted_counter_dict(category_counter)


def _has_hard_capital_reason(reasons: Iterable[str]) -> bool:
    text = " ".join(str(reason).lower() for reason in reasons)
    return any(token in text for token in CAPITAL_HARD_RISK_REASON_TOKENS)


def _recommendation_capital_item(recommendation: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _recommendation_snapshot(recommendation)
    confirmation = _market_confirmation(snapshot)
    execution_result = snapshot.get("execution_result") if isinstance(snapshot.get("execution_result"), dict) else {}
    contract = final_action_contract_from_snapshot(snapshot)
    position_budget = snapshot.get("position_budget_policy") if isinstance(snapshot.get("position_budget_policy"), dict) else {}
    final_trade_authority = contract
    ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "").upper()
    semantic_view = _final_action_semantic_view(contract, execution_result)
    target_lots = _safe_int(semantic_view.get("target_lots"))
    current_lots = _safe_int(semantic_view.get("current_lots"))
    target_ratio = _safe_float(contract.get("target_position_ratio"))
    current_ratio = 0.0
    target_side = str(semantic_view.get("contract_side") or "flat")
    if target_side not in {"long", "short"} and abs(target_ratio) > 1e-12:
        target_side = _target_side_from_ratio(target_ratio)
    no_trade_reason = (
        execution_result.get("no_trade_reason")
        or ((contract.get("reason_codes") or [None])[-1] if isinstance(contract.get("reason_codes"), list) else None)
        or recommendation.get("warning_message")
    )
    no_trade_reason = normalize_no_trade_reason(no_trade_reason) if no_trade_reason else ""
    control_reasons = [str(item) for item in (contract.get("reason_codes") or []) if item]
    auditor_decision = str(contract.get("authority_type") or final_trade_authority.get("authority_type") or "")
    learning_used = contract.get("learning_used") if isinstance(contract.get("learning_used"), dict) else {}
    capital_learning = (
        learning_used.get("capital_utilization_learning")
        if isinstance(learning_used.get("capital_utilization_learning"), dict)
        else {}
    )
    capital_target = (
        learning_used.get("capital_utilization_target")
        if isinstance(learning_used.get("capital_utilization_target"), dict)
        else {}
    )
    alpha_release = (
        capital_target.get("alpha_release")
        if isinstance(capital_target.get("alpha_release"), dict)
        else {}
    )
    memory_state = str(
        learning_used.get("memory_state")
        or ((capital_learning.get("protected_memory") or {}).get("memory_state") if isinstance(capital_learning.get("protected_memory"), dict) else "")
        or ""
    )
    confirmation_score = _safe_float(confirmation.get("confirmation_score"))
    min_scale_score = _safe_float(
        (cfg.get("capital_utilization_control") or {}).get("min_confirmation_score_for_scaling"),
        0.60,
    )
    protected_min_score = _safe_float(
        (cfg.get("capital_utilization_control") or {}).get("memory_protected_min_confirmation_score"),
        0.45,
    )
    learned_quality = memory_state in {"protected", "deployable", "recovering"}
    required_score = protected_min_score if learned_quality else min_scale_score
    hard_capital_reason = _has_hard_capital_reason(control_reasons)
    alpha_release_tier = str(capital_target.get("alpha_release_tier") or ("normal" if learned_quality else "probe"))
    alpha_release_requirements = {
        "tier": alpha_release_tier,
        "source": "final_action_contract",
        "stop_protected": bool(capital_target.get("stop_protected")),
        "structured_invalidation": bool(capital_target.get("structured_invalidation")),
        "specific_signal_combo": bool(alpha_release.get("specific_signal_combo")),
        "limiting_reasons": alpha_release.get("limiting_reasons") or [],
    }
    alpha_release_eligible = (
        target_side in {"long", "short"}
        and abs(target_ratio) > 1e-12
        and confirmation_score >= required_score
        and no_trade_reason not in CAPITAL_NON_RELEASE_NO_TRADE_REASONS
        and not hard_capital_reason
        and auditor_decision not in {"block", "reduce_only"}
        and "weak_block" not in memory_state
        and alpha_release_tier in {"normal", "boost", "max_boost"}
    )
    capital_utilization_skip = ""
    position_budget_decision = str(position_budget.get("decision") or "")
    final_trade_authority_decision = str(final_trade_authority.get("decision") or "")
    if target_side not in {"long", "short"} or abs(target_ratio) <= 1e-12:
        capital_path_stage = "no_directional_target"
    elif hard_capital_reason or auditor_decision in {"block", "reduce_only"}:
        capital_path_stage = "hard_or_pm_risk_gate_block"
    elif final_trade_authority_decision and final_trade_authority_decision != "allow_real_new_entry":
        capital_path_stage = "final_trade_authority"
    elif position_budget_decision in {
        "minimum_margin_not_reachable_watchlist",
        "no_feasible_lot",
        "final_entry_authority_not_met",
    }:
        capital_path_stage = "position_budget"
    elif capital_utilization_skip:
        capital_path_stage = "capital_utilization"
    elif no_trade_reason in {"intraday_trigger_not_met", "intraday_opening_range_incomplete"}:
        capital_path_stage = "execution_timing"
    elif no_trade_reason:
        capital_path_stage = "pm_or_execution_no_trade"
    elif alpha_release_eligible:
        capital_path_stage = "capital_release_candidate"
    else:
        capital_path_stage = "directional_target_unclassified"
    return {
        "ticker": ticker,
        "side": target_side,
        "target_position_ratio": target_ratio,
        "current_position_ratio": current_ratio,
        "target_margin_ratio_estimate": _safe_float(contract.get("target_margin_ratio_estimate")),
        "margin_required": 0.0,
        "target_lots": target_lots,
        "current_lots": current_lots,
        "tradable_lots": abs(_safe_int(semantic_view.get("lots_delta"))),
        "no_trade_reason": no_trade_reason,
        "rebalance_action_type": str(semantic_view.get("action") or ""),
        "final_action_semantics": semantic_view,
        "confirmation_score": confirmation_score,
        "signal_confidence": _safe_float(_first_analyst_field(snapshot, "confidence")),
        "auditor_decision": auditor_decision,
        "memory_state": memory_state,
        "control_reasons": control_reasons,
        "hard_capital_reason": hard_capital_reason,
        "alpha_release_eligible": alpha_release_eligible,
        "alpha_release_tier": alpha_release_tier,
        "alpha_release_requirements": alpha_release_requirements,
        "required_confirmation_score": required_score,
        "capital_path_stage": capital_path_stage,
        "capital_utilization_skip": capital_utilization_skip,
        "capital_target_mode": "",
        "dynamic_allocation_tier": "",
        "dynamic_opportunity_margin_ratio_budget": 0.0,
        "position_budget_decision": position_budget_decision,
        "position_budget_target_margin_after": _safe_float(position_budget.get("target_margin_after")),
        "position_budget_min_required_margin": _safe_float(position_budget.get("min_required_margin")),
        "final_trade_authority_decision": final_trade_authority_decision,
        "final_trade_authority_requires_authority": bool(final_trade_authority.get("requires_authority")),
    }


def _build_capital_deployment_diagnostics(
    *,
    cfg: Dict[str, Any],
    allocation_tier: str,
    reason_bucket: str,
    current_ratio: float,
    target_min: float,
    target_max: float,
    margin_gap_to_min: float,
    strategy_recommendations: List[Dict[str, Any]],
    no_trade_reason_counter: Counter,
) -> Dict[str, Any]:
    reason_profiles: Dict[str, Dict[str, Any]] = {}
    category_counts: Counter = Counter()
    action_counts: Counter = Counter()
    for reason, count in (no_trade_reason_counter or Counter()).items():
        profile = _capital_reason_profile(reason)
        profile["count"] = int(count or 0)
        reason_profiles[str(reason)] = profile
        category_counts[profile["category"]] += int(count or 0)
        action_counts[profile["suggested_action"]] += int(count or 0)

    if reason_bucket and reason_bucket not in reason_profiles:
        profile = _capital_reason_profile(reason_bucket)
        profile["count"] = 0
        reason_profiles[str(reason_bucket)] = profile
        if not category_counts:
            category_counts[profile["category"]] += 1
            action_counts[profile["suggested_action"]] += 1

    recommendation_items = [
        _recommendation_capital_item(recommendation, cfg)
        for recommendation in strategy_recommendations
    ]
    capital_path_stage_counts: Counter = Counter(
        item["capital_path_stage"] for item in recommendation_items
    )
    capital_utilization_skip_counts: Counter = Counter(
        item["capital_utilization_skip"]
        for item in recommendation_items
        if item.get("capital_utilization_skip")
    )
    position_budget_decision_counts: Counter = Counter(
        item["position_budget_decision"]
        for item in recommendation_items
        if item.get("position_budget_decision")
    )
    final_trade_authority_decision_counts: Counter = Counter(
        item["final_trade_authority_decision"]
        for item in recommendation_items
        if item.get("final_trade_authority_decision")
    )
    directional_items = [
        item for item in recommendation_items
        if item["side"] in {"long", "short"} and abs(item["target_position_ratio"]) > 1e-12
    ]
    tradable_items = [
        item for item in directional_items
        if item["tradable_lots"] != 0
    ]
    blocked_directional_items = [
        item for item in directional_items
        if item["tradable_lots"] == 0 or item["no_trade_reason"]
    ]
    capital_path_cases = [
        {
            "ticker": item["ticker"],
            "side": item["side"],
            "capital_path_stage": item["capital_path_stage"],
            "no_trade_reason": item["no_trade_reason"],
            "target_position_ratio": item["target_position_ratio"],
            "target_margin_ratio_estimate": item["target_margin_ratio_estimate"],
            "confirmation_score": item["confirmation_score"],
            "capital_utilization_skip": item["capital_utilization_skip"],
            "position_budget_decision": item["position_budget_decision"],
            "final_trade_authority_decision": item["final_trade_authority_decision"],
            "capital_target_mode": item["capital_target_mode"],
            "dynamic_allocation_tier": item["dynamic_allocation_tier"],
        }
        for item in blocked_directional_items[:15]
    ]
    alpha_release_candidates = [
        {
            "ticker": item["ticker"],
            "side": item["side"],
            "target_position_ratio": item["target_position_ratio"],
            "current_position_ratio": item["current_position_ratio"],
            "confirmation_score": item["confirmation_score"],
            "signal_confidence": item["signal_confidence"],
            "auditor_decision": item["auditor_decision"],
            "memory_state": item["memory_state"],
            "alpha_release_tier": item["alpha_release_tier"],
            "alpha_release_requirements": item["alpha_release_requirements"],
            "rebalance_action_type": item["rebalance_action_type"],
            "tradable_lots": item["tradable_lots"],
        }
        for item in recommendation_items
        if item["alpha_release_eligible"]
    ]
    recovery_probe_candidates = [
        {
            "ticker": item["ticker"],
            "side": item["side"],
            "target_position_ratio": item["target_position_ratio"],
            "current_position_ratio": item["current_position_ratio"],
            "confirmation_score": item["confirmation_score"],
            "signal_confidence": item["signal_confidence"],
            "auditor_decision": item["auditor_decision"],
            "memory_state": item["memory_state"],
            "alpha_release_tier": item["alpha_release_tier"],
            "alpha_release_requirements": item["alpha_release_requirements"],
            "blocked_by_reason": item["no_trade_reason"],
            "rebalance_action_type": item["rebalance_action_type"],
            "tradable_lots": item["tradable_lots"],
        }
        for item in recommendation_items
        if allocation_tier == "under_deployed"
        and item["side"] in {"long", "short"}
        and item["target_position_ratio"] != 0
        and item["confirmation_score"] >= max(0.60, item["required_confirmation_score"])
        and item["alpha_release_tier"] in {"normal", "boost", "max_boost"}
        and item["no_trade_reason"] in {
            "minimum_new_entry_threshold",
            "position_matched",
            "learned_underperformance_policy",
        }
        and not item["hard_capital_reason"]
        and item["auditor_decision"] not in {"block", "reduce_only"}
        and item["memory_state"] in {"protected", "deployable", "recovering", ""}
        and bool((item["alpha_release_requirements"] or {}).get("structured_invalidation"))
        and bool((item["alpha_release_requirements"] or {}).get("specific_signal_combo"))
    ]
    execution_gate_candidates = [
        {
            "ticker": item["ticker"],
            "side": item["side"],
            "confirmation_score": item["confirmation_score"],
            "target_position_ratio": item["target_position_ratio"],
        }
        for item in recommendation_items
        if item["no_trade_reason"] in {"intraday_trigger_not_met", "intraday_opening_range_incomplete"}
    ]
    pm_risk_gate_suppression_cases = [
        {
            "ticker": item["ticker"],
            "side": item["side"],
            "auditor_decision": item["auditor_decision"],
            "control_reasons": item["control_reasons"],
            "hard_capital_reason": item["hard_capital_reason"],
            "confirmation_score": item["confirmation_score"],
        }
        for item in recommendation_items
        if item["auditor_decision"] in {"block", "reduce_only", "probe_only", "scale_down"}
        or any(
            str(reason) in {
                "pm_risk_gate_block",
                "pm_risk_gate_reduce_only",
                "pm_risk_gate_scale_to_zero",
            }
            for reason in item["control_reasons"]
        )
    ]
    position_matched_watchlist = [
        {
            "ticker": item["ticker"],
            "side": item["side"],
            "current_position_ratio": item["current_position_ratio"],
            "confirmation_score": item["confirmation_score"],
            "memory_state": item["memory_state"],
        }
        for item in recommendation_items
        if item["no_trade_reason"] == "position_matched"
    ]
    size_threshold_cases = [
        {
            "ticker": item["ticker"],
            "side": item["side"],
            "target_position_ratio": item["target_position_ratio"],
            "confirmation_score": item["confirmation_score"],
            "required_confirmation_score": item["required_confirmation_score"],
        }
        for item in recommendation_items
        if item["no_trade_reason"] == "minimum_new_entry_threshold"
    ]

    primary_profile = _capital_reason_profile(reason_bucket)
    primary_category = primary_profile["category"]
    parameter_review: List[Dict[str, Any]] = []
    if no_trade_reason_counter.get("intraday_trigger_not_met"):
        intraday_cfg = ((cfg.get("execution") or {}).get("intraday_confirmation") or {})
        parameter_review.append(
            {
                "scope": "execution.intraday_confirmation",
                "reason": "intraday_trigger_not_met",
                "current_values": {
                    "opening_range_minutes": intraday_cfg.get("opening_range_minutes"),
                    "require_complete_opening_range": intraday_cfg.get("require_complete_opening_range"),
                    "max_chase_ratio": intraday_cfg.get("max_chase_ratio"),
                },
                "guardrail": "loosen only after missed-trigger candidates show favorable follow-through",
            }
        )
    if no_trade_reason_counter.get("minimum_new_entry_threshold"):
        holding_cfg = (((cfg.get("portfolio_manager") or {}).get("holding_rebalance_control")) or {})
        parameter_review.append(
            {
                "scope": "portfolio_manager.holding_rebalance_control.min_new_entry_ratio",
                "reason": "minimum_new_entry_threshold",
                "current_values": {"min_new_entry_ratio": holding_cfg.get("min_new_entry_ratio")},
                "guardrail": "lower only for high-confirmation protected/deployable candidates",
            }
        )
    if no_trade_reason_counter.get("pm_risk_gate_block"):
        parameter_review.append(
            {
                "scope": "pm_risk_gate",
                "reason": "pm_risk_gate_block",
                "current_values": {},
                "guardrail": "keep hard-risk blocks; review soft blocks against realized alpha before relaxing",
            }
        )

    return {
        "allocation_tier": allocation_tier,
        "primary_reason": reason_bucket,
        "primary_category": primary_category,
        "current_margin_ratio": float(current_ratio),
        "target_margin_ratio_min": float(target_min),
        "target_margin_ratio_max": float(target_max),
        "margin_gap_to_min": float(margin_gap_to_min),
        "reason_counts": _sorted_counter_dict(no_trade_reason_counter or Counter()),
        "no_trade_reason_category_counts": _no_trade_reason_category_counts(no_trade_reason_counter),
        "category_counts": _sorted_counter_dict(category_counts),
        "action_counts": _sorted_counter_dict(action_counts),
        "reason_profiles": reason_profiles,
        "capital_path_stage_counts": _sorted_counter_dict(capital_path_stage_counts),
        "capital_utilization_skip_counts": _sorted_counter_dict(capital_utilization_skip_counts),
        "position_budget_decision_counts": _sorted_counter_dict(position_budget_decision_counts),
        "final_trade_authority_decision_counts": _sorted_counter_dict(final_trade_authority_decision_counts),
        "directional_candidate_count": len(directional_items),
        "tradable_directional_candidate_count": len(tradable_items),
        "blocked_directional_candidate_count": len(blocked_directional_items),
        "capital_path_cases": capital_path_cases,
        "alpha_release_candidate_count": len(alpha_release_candidates),
        "alpha_release_candidates": alpha_release_candidates[:10],
        "recovery_probe_candidate_count": len(recovery_probe_candidates),
        "recovery_probe_candidates": recovery_probe_candidates[:10],
        "execution_gate_candidates": execution_gate_candidates[:10],
        "pm_risk_gate_suppression_cases": pm_risk_gate_suppression_cases[:10],
        "position_matched_watchlist": position_matched_watchlist[:10],
        "size_threshold_cases": size_threshold_cases[:10],
        "parameter_review": parameter_review,
        "do_not_force_low_quality_trades": True,
        "capital_release_rule": (
            "Increase utilization only through high-confirmation, protected/deployable/recovering, "
            "non-hard-risk alpha candidates; never fill the margin target with weak/watchlist trades."
        ),
    }



def _money(value: Any, digits: int = 2) -> str:
    try:
        number = float(value or 0.0)
    except Exception:
        number = 0.0
    return f"{number:,.{digits}f}"


def _signed_money(value: Any, digits: int = 2) -> str:
    try:
        number = float(value or 0.0)
    except Exception:
        number = 0.0
    return f"{number:+,.{digits}f}"


def _percent(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value or 0.0) * 100:.{digits}f}%"
    except Exception:
        return "0.0%"



def _report_rows(cursor: sqlite3.Cursor, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    cursor.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def _template_report_line(row: Dict[str, Any]) -> str:
    return (
        f"- {row.get('ticker')}/{row.get('side')}/{row.get('horizon_class')}: "
        f"{row.get('setup_type')} | samples={int(row.get('sample_count') or 0)} "
        f"win_rate={_percent(row.get('win_rate'))} "
        f"net_pnl={_signed_money(row.get('net_pnl'))} "
        f"confidence={_percent(row.get('confidence_score'))}"
    )


def _trade_performance_report_line(label: str, row: Dict[str, Any]) -> str:
    return (
        f"- {label}: trades={int(row.get('total_trades') or 0)} "
        f"win_rate={_percent(row.get('win_rate'))} "
        f"net_pnl={_signed_money(row.get('net_pnl'))} "
        f"avg_pnl={_signed_money(row.get('avg_pnl'))}"
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: Any) -> Any:
    try:
        return load_externalized_json(value)
    except Exception:
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _compact_text(value: Any, max_chars: int = 160) -> str:
    text = str(value or "").strip().replace("\n", " ")
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def _walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _target_side_from_ratio(value: Any) -> str:
    ratio = _safe_float(value)
    if ratio > 0:
        return "long"
    if ratio < 0:
        return "short"
    return "flat"


def _final_action_semantic_view(
    contract: Dict[str, Any],
    execution_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the read-only common semantic view used by review/research tools."""
    contract = contract if isinstance(contract, dict) else {}
    execution_result = execution_result if isinstance(execution_result, dict) else {}
    semantics = classify_final_action_contract(contract)
    memory = derive_memory_requirements(contract)
    review = derive_review_expectation(contract, execution_result)
    contract_side = memory.get("target_side") or memory.get("current_position_side") or "flat"
    action_family = canonical_action_family(
        semantics.get("action"),
        current_lots=semantics.get("current_lots"),
        target_lots=semantics.get("target_lots"),
    )
    action_value_lane = canonical_action_value_lane(
        semantics.get("action"),
        current_lots=semantics.get("current_lots"),
        target_lots=semantics.get("target_lots"),
    )
    return {
        "source": "final_action_semantics",
        "action": semantics.get("action"),
        "canonical_action_family": action_family,
        "action_value_lane": action_value_lane,
        "learning_lane": action_value_lane,
        "current_lots": semantics.get("current_lots"),
        "target_lots": semantics.get("target_lots"),
        "lots_delta": semantics.get("lots_delta"),
        "lifecycle_state": semantics.get("lifecycle_state"),
        "execution_permission": semantics.get("execution_permission"),
        "requires_intraday_result": semantics.get("requires_intraday_result"),
        "target_side": memory.get("target_side"),
        "current_position_side": memory.get("current_position_side"),
        "contract_side": contract_side,
        "contract_side_role": memory.get("contract_side_role"),
        "required_memory_lanes": memory.get("required_memory_lanes") or [],
        "required_memory_side_roles": memory.get("required_memory_side_roles") or [],
        "review_expectation": review,
    }


def _signal_side(signal: Any) -> str:
    return SIDE_BY_SIGNAL.get(str(signal), "neutral")


def _signal_combo_from_snapshot(snapshot: Dict[str, Any]) -> List[str]:
    values = []
    for analyst in ANALYSTS:
        item = snapshot.get(analyst)
        values.append(str(item.get("signal") if isinstance(item, dict) else "Neutral"))
    while len(values) < 3:
        values.append("Neutral")
    return values[:3]


def _first_analyst_field(snapshot: Dict[str, Any], field_name: str, default: Any = None) -> Any:
    for analyst in ANALYSTS:
        item = snapshot.get(analyst)
        if isinstance(item, dict) and item.get(field_name) not in (None, "", "unknown"):
            return item.get(field_name)
    return default


def _analyst_field(snapshot: Dict[str, Any], analyst: str, field_name: str, default: Any = None) -> Any:
    keys = [analyst]
    for key in keys:
        item = snapshot.get(key)
        if not isinstance(item, dict):
            continue
        value = item.get(field_name)
        if value not in (None, "", "unknown"):
            return value
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        candidate_contexts = [
            metadata,
            metadata.get(f"{analyst}_context") if isinstance(metadata.get(f"{analyst}_context"), dict) else {},
            metadata.get("technical_context") if isinstance(metadata.get("technical_context"), dict) else {},
            metadata.get("fundamental_context") if isinstance(metadata.get("fundamental_context"), dict) else {},
            metadata.get("news_context") if isinstance(metadata.get("news_context"), dict) else {},
            metadata.get("signal_context") if isinstance(metadata.get("signal_context"), dict) else {},
        ]
        for context in candidate_contexts:
            if not isinstance(context, dict):
                continue
            value = context.get(field_name)
            if value not in (None, "", "unknown"):
                return value
    return default


def _recommendation_snapshot(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = load_externalized_json(
        recommendation.get("signal_snapshot"),
        recommendation.get("signal_snapshot_artifact_path"),
        recommendation.get("signal_snapshot_sha256"),
    ) or {}
    return snapshot if isinstance(snapshot, dict) else {}


def _final_action_contract_payload(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    contract = final_action_contract_from_snapshot(snapshot)
    if not contract:
        return {}
    return {
        "final_action_contract": contract,
        "fusion_attribution": build_reviewer_fusion_attribution({"final_action_contract": contract}),
        "source": "final_action_contract",
        "not_pm_draft": True,
    }


def _learning_safe_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return dict(snapshot or {})


def _market_confirmation(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    confirmation = snapshot.get("market_confirmation")
    return confirmation if isinstance(confirmation, dict) else {}


def _market_regime(snapshot: Dict[str, Any]) -> str:
    explicit = _first_analyst_field(snapshot, "market_regime")
    if explicit:
        return str(explicit)
    contract = final_action_contract_from_snapshot(snapshot)
    if isinstance(contract, dict) and contract.get("market_regime") not in (None, "", "unknown"):
        return str(contract.get("market_regime"))
    technical = snapshot.get("technical")
    if isinstance(technical, dict):
        context = ((technical.get("metadata") or {}).get("technical_context") or {})
        if isinstance(context, dict) and context.get("market_regime"):
            return str(context.get("market_regime"))
    summary = _opportunity_contract_summary(snapshot)
    if summary.get("market_regime"):
        return str(summary.get("market_regime"))
    return "unknown"


def _price_stage(snapshot: Dict[str, Any]) -> str:
    explicit = _first_analyst_field(snapshot, "trend_stage")
    if explicit:
        return str(explicit)
    technical = snapshot.get("technical")
    if isinstance(technical, dict):
        context = ((technical.get("metadata") or {}).get("technical_context") or {})
        if isinstance(context, dict):
            stage = context.get("price_stage") or context.get("trend_stage") or context.get("tradeability")
            if stage:
                return str(stage)
    return "unknown"


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, "", "unknown", "UNKNOWN"):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _first_structured_signal_value(snapshot: Dict[str, Any], field_names: Iterable[str]) -> Any:
    names = tuple(field_names)
    for field_name in names:
        value = _first_analyst_field(snapshot, field_name)
        if value not in (None, "", "unknown"):
            return value
    for analyst in ANALYSTS:
        item = snapshot.get(analyst)
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        candidate_contexts = [
            metadata,
            metadata.get("technical_context") if isinstance(metadata.get("technical_context"), dict) else {},
            metadata.get("market_context") if isinstance(metadata.get("market_context"), dict) else {},
            metadata.get("signal_context") if isinstance(metadata.get("signal_context"), dict) else {},
        ]
        for context in candidate_contexts:
            for field_name in names:
                value = context.get(field_name) if isinstance(context, dict) else None
                if value not in (None, "", "unknown"):
                    return value
    return None


def _price_percentile(snapshot: Dict[str, Any]) -> Optional[float]:
    return _optional_float(
        _first_structured_signal_value(
            snapshot,
            ("price_percentile", "price_percentile_lookback", "current_price_percentile"),
        )
    )


def _invalidation_level(snapshot: Dict[str, Any]) -> Optional[float]:
    return _optional_float(
        _first_structured_signal_value(
            snapshot,
            ("invalidation_level", "stop_level", "stop_loss_level", "invalid_price"),
        )
    )


def _target_return(snapshot: Dict[str, Any]) -> Optional[float]:
    return _optional_float(
        _first_structured_signal_value(
            snapshot,
            ("target_return", "expected_return", "target_return_ratio", "expected_return_ratio"),
        )
    )


def _expected_horizon_days(snapshot: Dict[str, Any], side: str) -> int:
    explicit_from_analyst = _safe_int(_first_analyst_field(snapshot, "expected_horizon_days"), 0)
    if explicit_from_analyst > 0:
        return explicit_from_analyst
    combo = _signal_combo_from_snapshot(snapshot)
    if side == "flat":
        return 0
    if _signal_side(combo[1]) == side:
        return 5
    if _signal_side(combo[2]) == side:
        return 2
    return 1


def _horizon_class(days: int, snapshot: Optional[Dict[str, Any]] = None) -> str:
    if snapshot:
        scope = snapshot.get("horizon_scope") if isinstance(snapshot.get("horizon_scope"), dict) else {}
        decision_horizon = scope.get("decision_horizon")
        if decision_horizon and str(decision_horizon) != "unknown":
            return str(decision_horizon)
        # Preserve analyst natural horizons instead of defaulting to the first
        # analyst (usually technical short). This keeps medium fundamental
        # anchors and event_short news windows from being flattened.
        analyst_horizons = scope.get("analyst_horizons") if isinstance(scope, dict) else {}
        if isinstance(analyst_horizons, dict):
            values = [
                str((payload or {}).get("analyst_horizon") or (payload or {}).get("horizon_class") or "")
                for payload in analyst_horizons.values()
                if isinstance(payload, dict)
            ]
            if "medium" in values:
                return "medium"
            if "event_short" in values:
                return "event_short"
            if "short" in values:
                return "short"
    if days <= 0:
        return "flat"
    if days <= 2:
        return "short"
    if days <= 5:
        return "medium"
    return "long"


def _analyst_horizon_class(snapshot: Dict[str, Any], analyst: str, fallback_days: int = 0) -> str:
    explicit = _analyst_field(snapshot, analyst, "horizon_class")
    if explicit:
        return str(explicit)
    explicit_days = _safe_int(_analyst_field(snapshot, analyst, "expected_horizon_days"), 0)
    if explicit_days > 0:
        return _horizon_class(explicit_days)
    default_horizon = DEFAULT_ANALYST_HORIZON.get(analyst)
    if default_horizon:
        return default_horizon
    return _horizon_class(fallback_days)


def _entry_trigger_label(snapshot: Dict[str, Any], side: str) -> str:
    explicit = _first_analyst_field(snapshot, "entry_trigger")
    if explicit:
        return str(explicit)
    confirmation = _market_confirmation(snapshot)
    score = _safe_float(confirmation.get("confirmation_score"), 0.0)
    if score >= 0.70:
        return "confirmed_momentum"
    if side != "flat" and _price_stage(snapshot) in {"oversold", "overbought"}:
        return "reversal_probe"
    return "standard_signal"


def _action_name(recommendation: Dict[str, Any], snapshot: Dict[str, Any]) -> str:
    explicit = _first_analyst_field(snapshot, "action_name")
    if explicit:
        return str(explicit)
    action = str(recommendation.get("action") or "").lower()
    if "hold" in action:
        return "hold"
    if "close" in action or "reduce" in action:
        return "reduce_or_exit"
    return "new_or_adjust"


def _recommendation_side(recommendation: Dict[str, Any], snapshot: Dict[str, Any]) -> str:
    contract = final_action_contract_from_snapshot(snapshot)
    side = _final_action_semantic_view(contract).get("contract_side")
    if side in {"long", "short", "flat"}:
        return str(side)
    return "unknown"


def _setup_type(side: str, combo: Iterable[str], snapshot: Dict[str, Any]) -> str:
    for analyst, payload in _analyst_payloads(snapshot).items():
        if _signal_side(payload.get("signal")) == side:
            setup_type = str(payload.get("setup_type") or "").strip()
            if setup_type and setup_type != "unknown":
                horizon = str(payload.get("analyst_horizon") or payload.get("horizon_class") or "unknown")
                return f"{side}_{setup_type}_{horizon}"[:160]
    trigger = _entry_trigger_label(snapshot, side)
    regime = _market_regime(snapshot).lower().replace(" ", "_")
    normalized_combo = "_".join(str(item).lower() for item in combo)
    return f"{side}_{trigger}_{regime}_{normalized_combo}"[:160]


def _data_combo_key(data_usage: Dict[str, Any]) -> str:
    """Build a compact data-combination key for same-scope research.

    This is descriptive evidence for future comparison, not a new product rule.
    """
    if not isinstance(data_usage, dict):
        return "data_unknown"
    pieces: List[str] = []
    analysts = data_usage.get("analysts") if isinstance(data_usage.get("analysts"), dict) else {}
    for analyst, usage in sorted((analysts or {}).items()):
        sources = usage.get("sources") if isinstance(usage, dict) else {}
        for source_name, source in sorted((sources or {}).items()):
            if not isinstance(source, dict):
                continue
            available = "ok" if source.get("available") else "missing"
            used = "used" if source.get("used_in_signal") else "unused"
            stale = "stale" if _safe_float(source.get("stale_ratio"), 0.0) > 0 or _safe_int(source.get("stale_indicator_count"), 0) > 0 else "fresh"
            pieces.append(f"{analyst}.{source_name}:{available}:{used}:{stale}")
    pm_sources = data_usage.get("pm_sources") if isinstance(data_usage.get("pm_sources"), dict) else {}
    for source_name, source in sorted((pm_sources or {}).items()):
        if isinstance(source, dict):
            available = "ok" if source.get("available") else "missing"
            used = "used" if source.get("used_in_signal") else "unused"
            pieces.append(f"pm.{source_name}:{available}:{used}")
    return "|".join(pieces[:8]) or "data_unknown"


def _loss_failure_family(template: str, horizon: str, regime: str, data_combo: str) -> str:
    text = f"{template} {horizon} {regime} {data_combo}".lower()
    if "news_event" in text or "event_short" in text or "catalyst" in text:
        return "news_event_probe_failure"
    if "trend_continuation" in text and any(marker in text for marker in ("range", "choppy", "sideways", "oscillat")):
        return "trend_continuation_choppy_failure"
    if "fundamental_direction_anchor" in text or ("fundamental" in text and "medium" in text):
        return "medium_fundamental_timing_failure"
    if "missing" in text or "stale" in text:
        return "data_gap_signal_failure"
    return "general_same_scope_loss_failure"


def _failure_family_actions(failure_family: str) -> Dict[str, List[str]]:
    if failure_family == "news_event_probe_failure":
        return {
            "analysis": [
                "News/event setups need catalyst relevance, price reaction, and market confirmation before confidence rises.",
                "If event evidence is noisy or unconfirmed, describe the trigger that would turn watchlist into probe/open.",
            ],
            "trading": [
                "PM should keep same-scope event probes small unless current price reaction and invalidation are explicit.",
                "A news memory cannot justify continuing an adverse position without current confirmation.",
            ],
        }
    if failure_family == "trend_continuation_choppy_failure":
        return {
            "analysis": [
                "Trend-continuation setups in range/choppy regimes need breakout, volatility, and volume/open-interest confirmation.",
                "If the regime is still choppy, analysts should lower trend confidence or name the breakout condition.",
            ],
            "trading": [
                "PM should avoid full trend sizing in same-scope choppy setups without breakout and invalidation evidence.",
                "Existing adverse positions need current trend repair evidence before position_matched is acceptable.",
            ],
        }
    if failure_family == "medium_fundamental_timing_failure":
        return {
            "analysis": [
                "Medium fundamental anchors must be bridged by short-term timing evidence before short-horizon trades.",
                "Analysts should state the short-term trigger and the condition that invalidates the medium thesis today.",
            ],
            "trading": [
                "PM should not use a medium thesis alone to open, add, or hold an adverse short-term position.",
                "Same-scope trades need timing confirmation plus a price/ATR stop or structured invalidation boundary.",
            ],
        }
    if failure_family == "data_gap_signal_failure":
        return {
            "analysis": [
                "When key data are missing/stale, separate missing evidence from directional evidence.",
                "Analysts should explain what data field would confirm or contradict the setup once available.",
            ],
            "trading": [
                "PM should keep same-scope data-gap setups at observe/probe unless current non-missing evidence is strong.",
                "Data gaps alone are not bearish/bullish evidence and cannot justify adverse holds.",
            ],
        }
    return {
        "analysis": [
            "Compare today's signal combo, data drivers, horizon, and market state with the same-scope loss cases.",
            "Name the current evidence that confirms or contradicts the remembered loss pattern.",
        ],
        "trading": [
            "PM may demand clearer trigger and invalidation for same-scope repeats.",
            "Do not convert this observation into a product blacklist or unconditional position cap.",
        ],
    }


def _dotted_config_value(cfg: Dict[str, Any], key: str, default: Any = None) -> Any:
    current: Any = cfg
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _analyst_payloads(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    payloads = {}
    for analyst in ANALYSTS:
        item = snapshot.get(analyst)
        if isinstance(item, dict):
            payloads[analyst] = item
    return payloads


def _neutral_contract_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    contract = metadata.get("neutral_opportunity_contract") if isinstance(metadata.get("neutral_opportunity_contract"), dict) else {}
    bucket = str(payload.get("neutral_opportunity_bucket") or contract.get("bucket") or "unknown")
    trigger = str(
        payload.get("neutral_trigger_condition")
        or contract.get("trigger_condition")
        or payload.get("would_change_view_if")
        or ""
    )
    counterfactual_side = str(payload.get("counterfactual_side") or contract.get("counterfactual_side") or "flat").lower()
    if counterfactual_side not in {"long", "short", "flat"}:
        counterfactual_side = "flat"
    priority = str(payload.get("neutral_watchlist_priority") or contract.get("watchlist_priority") or "none")
    return {
        "bucket": bucket,
        "trigger_condition": trigger,
        "counterfactual_side": counterfactual_side,
        "watchlist_priority": priority,
        "observation_window": str(
            payload.get("recommended_observation_window") or contract.get("observation_window") or ""
        ),
        "opportunity_cost_risk": str(payload.get("opportunity_cost_risk") or contract.get("opportunity_cost_risk") or ""),
        "tracking_only": bool(contract.get("tracking_only", True)),
        "opportunity_state": str(contract.get("opportunity_state") or "watch_for_trigger"),
        "trigger_valid": bool(contract.get("trigger_valid", False)),
        "action_preference": str(contract.get("action_preference") or "watch_for_trigger"),
    }


def _neutral_opportunity_observations(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    observations: List[Dict[str, Any]] = []
    for analyst, payload in _analyst_payloads(snapshot).items():
        if str(payload.get("signal") or "Neutral") != "Neutral":
            continue
        contract = _neutral_contract_from_payload(payload)
        if contract["bucket"] in {"unknown", "low_tradeability"} and contract["counterfactual_side"] == "flat":
            continue
        observations.append(
            {
                "analyst": analyst,
                "bucket": contract["bucket"],
                "trigger_condition": contract["trigger_condition"],
                "counterfactual_side": contract["counterfactual_side"],
                "watchlist_priority": contract["watchlist_priority"],
                "observation_window": contract["observation_window"],
                "opportunity_cost_risk": contract["opportunity_cost_risk"],
                "neutral_reason": str(payload.get("neutral_reason") or ""),
                "tracking_only": True,
                "opportunity_state": contract["opportunity_state"],
                "trigger_valid": contract["trigger_valid"],
                "action_preference": contract["action_preference"],
            }
        )
    return observations


def _sector_for_ticker(cfg: Dict[str, Any], ticker: str) -> str:
    sector_map = (
        (cfg.get("sector_config") or {}).get("ticker_sector_map")
        or (cfg.get("futures_sector_config") or {}).get("ticker_sector_map")
        or {}
    )
    return str(sector_map.get(ticker.upper()) or SECTOR_BY_TICKER.get(ticker.upper()) or "*")


def _opportunity_contract_summary(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    summary = snapshot.get("pm_research_contract_summary") if isinstance(snapshot.get("pm_research_contract_summary"), dict) else {}
    if summary:
        return summary
    contracts = snapshot.get("trade_research_contracts") if isinstance(snapshot.get("trade_research_contracts"), dict) else {}
    opportunity_types = []
    opportunity_states = []
    factor_focus = []
    conflicts = []
    for contract in contracts.values():
        if not isinstance(contract, dict):
            continue
        if contract.get("opportunity_type"):
            opportunity_types.append(str(contract.get("opportunity_type")))
        if contract.get("opportunity_state"):
            opportunity_states.append(str(contract.get("opportunity_state")))
        factor_focus.extend(str(item) for item in (contract.get("factor_focus") or []))
        conflicts.extend(str(item) for item in (contract.get("current_evidence_conflict") or []))
    if contracts:
        return {
            "contract_version": "agentquant.research.v1",
            "dominant_opportunity_types": sorted(set(opportunity_types)),
            "opportunity_states": sorted(set(opportunity_states)),
            "factor_focus": sorted(set(factor_focus))[:12],
            "current_evidence_conflict": sorted(set(conflicts))[:12],
        }
    return {
        "contract_version": "agentquant.research.v1",
        "dominant_opportunity_types": [],
        "opportunity_states": [],
        "factor_focus": [],
        "current_evidence_conflict": [],
    }


def _scorecard_side_row(snapshot: Dict[str, Any], side: str = "") -> Dict[str, Any]:
    scorecard = (
        snapshot.get("opportunity_scorecard") if isinstance(snapshot.get("opportunity_scorecard"), dict) else {}
    )
    if not isinstance(scorecard, dict):
        return {}
    normalized_side = str(side or "").lower()
    if normalized_side in {"long", "short"} and isinstance(scorecard.get(normalized_side), dict):
        return scorecard[normalized_side]
    preferred = str(scorecard.get("preferred_side") or "").lower()
    if preferred in {"long", "short"} and isinstance(scorecard.get(preferred), dict):
        return scorecard[preferred]
    return {}


def _opportunity_ranking_trace(snapshot: Dict[str, Any], side: str = "") -> Dict[str, Any]:
    scorecard_side = _scorecard_side_row(snapshot, side)
    final_contract = final_action_contract_from_snapshot(snapshot)
    evidence_used = final_contract.get("evidence_used") if isinstance(final_contract.get("evidence_used"), dict) else {}
    deployment = final_contract.get("capital_deployment") if isinstance(final_contract.get("capital_deployment"), dict) else {}
    learning_used = final_contract.get("learning_used") if isinstance(final_contract.get("learning_used"), dict) else {}
    active_audit = snapshot.get("active_opportunity_audit") if isinstance(snapshot.get("active_opportunity_audit"), dict) else {}
    active_opportunity = active_audit.get("opportunity") if isinstance(active_audit.get("opportunity"), dict) else {}
    return {
        "opportunity_score": (
            scorecard_side.get("opportunity_score")
            if scorecard_side.get("opportunity_score") is not None
            else evidence_used.get("opportunity_score")
        ),
        "opportunity_score_components": (
            scorecard_side.get("opportunity_score_components")
            if isinstance(scorecard_side.get("opportunity_score_components"), dict)
            else evidence_used.get("opportunity_score_components") if isinstance(evidence_used.get("opportunity_score_components"), dict) else {}
        ),
        "opportunity_rank": (
            deployment.get("opportunity_rank")
            if deployment.get("opportunity_rank") is not None
            else evidence_used.get("opportunity_rank")
        ),
        "side_priority": scorecard_side.get("side_priority"),
        "ticker_side_priority": scorecard_side.get("ticker_side_priority"),
        "capital_allocation_reason": (
            scorecard_side.get("capital_allocation_reason")
            or evidence_used.get("capital_allocation_reason")
            or active_opportunity.get("capital_allocation_reason")
            or ""
        ),
        "learning_adjustment_summary": (
            scorecard_side.get("learning_adjustment_summary")
            if isinstance(scorecard_side.get("learning_adjustment_summary"), dict)
            else learning_used.get("learning_adjustment_summary") if isinstance(learning_used.get("learning_adjustment_summary"), dict) else {}
        ),
        "fusion_attribution": build_reviewer_fusion_attribution({"final_action_contract": final_contract}),
        "not_trade_authority": True,
        "learning_use": "review_ranking_effectiveness_not_trade_command",
    }


def _primary_opportunity_type(snapshot: Dict[str, Any], side: str = "") -> str:
    scorecard_side = _scorecard_side_row(snapshot, side)
    scorecard_type = str(scorecard_side.get("dominant_opportunity_type") or scorecard_side.get("opportunity_type") or "")
    if scorecard_type and scorecard_type not in {"unknown", "no_trade"}:
        return scorecard_type
    summary = _opportunity_contract_summary(snapshot)
    values = summary.get("dominant_opportunity_types") or []
    for value in values:
        if str(value) not in {"unknown", "no_trade"}:
            return str(value)
    return str(values[0]) if values else "unknown"


def _primary_opportunity_state(snapshot: Dict[str, Any], side: str = "") -> str:
    scorecard_side = _scorecard_side_row(snapshot, side)
    scorecard_state = str(scorecard_side.get("final_state") or "").lower()
    if scorecard_state in {"tradeable_candidate", "probe_candidate", "risk_reduction_candidate", "watch_for_trigger", "no_opportunity"}:
        return scorecard_state
    summary = _opportunity_contract_summary(snapshot)
    values = summary.get("opportunity_states") or []
    for preferred in ("tradeable_candidate", "probe_candidate", "risk_reduction_candidate", "watch_for_trigger", "no_opportunity"):
        if preferred in values:
            return preferred
    return str(values[0]) if values else "watch_for_trigger"


def _evidence_summary(snapshot: Dict[str, Any]) -> str:
    combo = _signal_combo_from_snapshot(snapshot)
    summary = _opportunity_contract_summary(snapshot)
    focus = ", ".join((summary.get("factor_focus") or [])[:5])
    conflicts = ", ".join((summary.get("current_evidence_conflict") or [])[:5])
    return (
        f"signals={combo}; opportunity={_primary_opportunity_type(snapshot)}; "
        f"focus={focus or 'unknown'}; conflicts={conflicts or 'none'}"
    )[:500]


def _valid_until(trading_date: str, days: int) -> str:
    return (datetime.strptime(trading_date, "%Y-%m-%d") + timedelta(days=max(1, days))).strftime("%Y-%m-%d")


def _confidence_from_summary(summary: Dict[str, Any]) -> float:
    sample_count = _safe_int(summary.get("total_trades") or summary.get("sample_count"), 0)
    win_rate = _safe_float(summary.get("win_rate") or summary.get("hit_rate"), 0.0)
    pnl = abs(_safe_float(summary.get("total_pnl") or summary.get("net_pnl"), 0.0))
    return min(1.0, min(0.45, sample_count / 10.0) + min(0.30, abs(win_rate - 0.50)) + min(0.25, pnl / 20000.0))


def _policy_guard_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    guard = ((cfg.get("learning") or {}).get("policy_promotion_guard") or {})
    return guard if bool(guard.get("enabled", False)) else {}


def _pair_distinct_days(rows: Iterable[Dict[str, Any]]) -> int:
    days = {
        str(row.get("close_date") or row.get("trading_date") or row.get("open_date") or "")[:10]
        for row in rows
        if str(row.get("close_date") or row.get("trading_date") or row.get("open_date") or "").strip()
    }
    return len(days)


def _pair_calendar_span_days(rows: Iterable[Dict[str, Any]]) -> int:
    days = sorted(
        {
            str(row.get("close_date") or row.get("trading_date") or row.get("open_date") or "")[:10]
            for row in rows
            if str(row.get("close_date") or row.get("trading_date") or row.get("open_date") or "").strip()
        }
    )
    if not days:
        return 0
    try:
        first = datetime.strptime(days[0], "%Y-%m-%d")
        last = datetime.strptime(days[-1], "%Y-%m-%d")
    except ValueError:
        return len(days)
    return (last - first).days + 1


def _max_abs_pnl_share(rows: Iterable[Dict[str, Any]]) -> float:
    values = [abs(_safe_float(row.get("net_pnl"))) for row in rows]
    total = sum(values)
    if total <= 1e-9:
        return 0.0
    return max(values or [0.0]) / total


def _policy_promotion_gate(
    *,
    cfg: Dict[str, Any],
    rows: List[Dict[str, Any]],
    action: str,
) -> Dict[str, Any]:
    """Bound policy promotion so small or concentrated samples stay provisional."""
    guard = _policy_guard_config(cfg)
    sample_count = len(rows)
    distinct_days = _pair_distinct_days(rows)
    span_days = _pair_calendar_span_days(rows)
    max_share = _max_abs_pnl_share(rows)
    if not guard:
        return {
            "allowed": True,
            "enabled": False,
            "sample_count": sample_count,
            "distinct_trade_days": distinct_days,
            "calendar_span_days": span_days,
            "max_single_trade_pnl_share": max_share,
            "reasons": [],
        }

    action_value = str(action or "").lower()
    is_positive = action_value in {"protect", "allow"}
    min_days_key = "min_distinct_trade_days_for_protect" if is_positive else "min_distinct_trade_days_for_cap"
    min_span_key = "min_calendar_span_days_for_protect" if is_positive else "min_calendar_span_days_for_cap"
    min_days = _safe_int(guard.get(min_days_key), 5 if is_positive else 3)
    min_span = _safe_int(guard.get(min_span_key), 10 if is_positive else 5)
    max_allowed_share = _safe_float(guard.get("max_single_trade_pnl_share"), 0.65)
    reasons: List[str] = []
    if distinct_days < min_days:
        reasons.append(f"distinct_trade_days={distinct_days}<{min_days}")
    if span_days < min_span:
        reasons.append(f"calendar_span_days={span_days}<{min_span}")
    if sample_count > 1 and max_allowed_share > 0 and max_share > max_allowed_share:
        reasons.append(f"single_trade_pnl_share={max_share:.2f}>{max_allowed_share:.2f}")
    return {
        "allowed": not reasons,
        "enabled": True,
        "sample_count": sample_count,
        "distinct_trade_days": distinct_days,
        "calendar_span_days": span_days,
        "max_single_trade_pnl_share": max_share,
        "min_distinct_trade_days": min_days,
        "min_calendar_span_days": min_span,
        "max_allowed_single_trade_pnl_share": max_allowed_share,
        "reasons": reasons,
    }


def _scope_is_exact(scope: Dict[str, Any]) -> bool:
    return all(str(scope.get(key) or "*") not in {"", "*", "unknown"} for key in (
        "ticker",
        "side",
        "setup_type",
        "horizon_class",
        "market_regime",
    ))


def _counterfactual_reversal_stats(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    scope: Dict[str, Any],
) -> Dict[str, Any]:
    """Detect whether a suppressive policy would have repeatedly missed alpha."""
    guard = _policy_guard_config(cfg)
    counterfactual_cfg = guard.get("counterfactual_reversal") if isinstance(guard.get("counterfactual_reversal"), dict) else {}
    if not guard or not bool(counterfactual_cfg.get("enabled", True)) or not _scope_is_exact(scope):
        return {"reversal": False, "enabled": bool(guard), "samples": 0, "net_counterfactual_pnl": 0.0}
    min_samples = _safe_int(counterfactual_cfg.get("min_samples"), 2)
    min_net_pnl = _safe_float(counterfactual_cfg.get("min_net_pnl"), 3000.0)
    cursor.execute(
        """
        SELECT id, trading_date, counterfactual_results_json
        FROM no_trade_opportunity_memory
        WHERE config_id = ?
          AND ticker = ?
          AND side = ?
          AND setup_type = ?
          AND horizon_class = ?
          AND market_regime = ?
          AND substr(trading_date, 1, 10) <= ?
          AND counterfactual_results_json IS NOT NULL
        """,
        (
            config_id,
            str(scope.get("ticker") or "").upper(),
            str(scope.get("side") or "").lower(),
            str(scope.get("setup_type") or ""),
            str(scope.get("horizon_class") or ""),
            str(scope.get("market_regime") or ""),
            trading_date,
        ),
    )
    positive: List[Dict[str, Any]] = []
    for row in cursor.fetchall():
        results = _json_loads(row["counterfactual_results_json"]) or []
        if not isinstance(results, list):
            continue
        latest = None
        for result in results:
            if not isinstance(result, dict):
                continue
            if latest is None or str(result.get("evaluation_date") or "") >= str(latest.get("evaluation_date") or ""):
                latest = result
        if not latest:
            continue
        pnl = _safe_float(latest.get("counterfactual_pnl"))
        if pnl > 0:
            positive.append({"memory_id": row["id"], "trading_date": row["trading_date"], "counterfactual_pnl": pnl})
    net_counterfactual = sum(_safe_float(item.get("counterfactual_pnl")) for item in positive)
    return {
        "reversal": len(positive) >= min_samples and net_counterfactual >= min_net_pnl,
        "enabled": True,
        "samples": len(positive),
        "net_counterfactual_pnl": net_counterfactual,
        "min_samples": min_samples,
        "min_net_pnl": min_net_pnl,
        "examples": positive[:8],
    }


def _profit_factor(pairs: List[Dict[str, Any]]) -> float:
    wins = sum(max(0.0, _safe_float(item.get("net_pnl"))) for item in pairs)
    losses = abs(sum(min(0.0, _safe_float(item.get("net_pnl"))) for item in pairs))
    if losses <= 1e-9:
        return wins if wins > 0 else 0.0
    return wins / losses


def _recommendations_by_id(cursor: sqlite3.Cursor, recommendation_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    ids = sorted({str(item) for item in recommendation_ids if item})
    if not ids:
        return {}
    placeholders = ", ".join(["?"] * len(ids))
    cursor.execute(
        f"SELECT * FROM futures_recommendation WHERE id IN ({placeholders})",
        tuple(ids),
    )
    return {str(row["id"]): dict(row) for row in cursor.fetchall()}


def _transactions_by_id(cursor: sqlite3.Cursor, transaction_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    ids = sorted({str(item) for item in transaction_ids if item})
    if not ids:
        return {}
    placeholders = ", ".join(["?"] * len(ids))
    cursor.execute(
        f"SELECT * FROM futures_transactions WHERE id IN ({placeholders})",
        tuple(ids),
    )
    return {str(row["id"]): dict(row) for row in cursor.fetchall()}


def _completed_pairs_up_to(cursor: sqlite3.Cursor, *, config_id: str, trading_date: str) -> List[Dict[str, Any]]:
    cursor.execute(
        '''
        SELECT *
        FROM futures_transactions
        WHERE config_id = ?
          AND substr(trading_date, 1, 10) <= ?
        ORDER BY substr(trading_date, 1, 10), created_at, id
        ''',
        (config_id, trading_date),
    )
    pairs = build_completed_trade_pairs([dict(row) for row in cursor.fetchall()], include_rollover=False)
    return [pair for pair in pairs if str(pair.get("close_date") or "") <= trading_date]


def _completed_pairs_for_scope(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    scope: Dict[str, Any],
) -> List[Dict[str, Any]]:
    pairs = _completed_pairs_up_to(cursor, config_id=config_id, trading_date=trading_date)
    recommendation_lookup = _recommendations_by_id(
        cursor,
        [pair.get("open_recommendation_id") for pair in pairs if pair.get("open_recommendation_id")],
    )
    expected = {
        "ticker": str(scope.get("ticker") or "").upper(),
        "side": str(scope.get("side") or "").lower(),
        "setup_type": str(scope.get("setup_type") or ""),
        "horizon_class": str(scope.get("horizon_class") or ""),
        "market_regime": str(scope.get("market_regime") or ""),
    }
    rows: List[Dict[str, Any]] = []
    for pair in pairs:
        ticker = str(pair.get("ticker") or "").upper()
        side = str(pair.get("side") or "").lower()
        if ticker != expected["ticker"] or side != expected["side"]:
            continue
        recommendation = recommendation_lookup.get(str(pair.get("open_recommendation_id") or ""))
        snapshot = _recommendation_snapshot(recommendation or {})
        combo = _signal_combo_from_snapshot(snapshot)
        horizon = _horizon_class(_expected_horizon_days(snapshot, side), snapshot)
        regime = _market_regime(snapshot)
        template = _setup_type(side, combo, snapshot)
        if (
            template == expected["setup_type"]
            and horizon == expected["horizon_class"]
            and regime == expected["market_regime"]
        ):
            rows.append(pair)
    return rows


def _episode_lesson_text(
    *,
    ticker: str,
    side: str,
    template: str,
    horizon: str,
    regime: str,
    pair: Dict[str, Any],
    snapshot: Dict[str, Any],
) -> str:
    pnl = _safe_float(pair.get("net_pnl"))
    outcome = "winner" if pnl > 0 else "loser" if pnl < 0 else "flat"
    analyst_bits = []
    for analyst, payload in _analyst_payloads(snapshot).items():
        signal = str(payload.get("signal") or "Neutral")
        confidence = max(_safe_float(payload.get("effective_confidence")), _safe_float(payload.get("confidence")))
        analyst_bits.append(f"{analyst}={signal}:{confidence:.2f}")
    invalidation = _invalidation_level(snapshot)
    no_trade_reason = (
        ((snapshot.get("execution_result") or {}) if isinstance(snapshot.get("execution_result"), dict) else {})
        .get("no_trade_reason")
    )
    return (
        f"{ticker} {side} {outcome}: template={template}, horizon={horizon}, regime={regime}, "
        f"hold={_safe_int(pair.get('holding_days'))}d, pnl={pnl:.0f}. "
        f"Analysts: {', '.join(analyst_bits) or 'unavailable'}. "
        f"Invalidation={invalidation if invalidation is not None else 'missing'}; "
        f"execution_note={no_trade_reason or 'executed_or_closed'}."
    )


def _policy_ref(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    contract = payload.get(CONTRACT_KEY) if isinstance(payload.get(CONTRACT_KEY), dict) else {}
    return {
        "policy_type": str(row.get("policy_type") or ""),
        "policy_action": str(row.get("policy_action") or ""),
        "ticker": str(row.get("ticker") or "*").upper(),
        "side": str(row.get("side") or "*").lower(),
        "setup_type": str(row.get("setup_type") or "*"),
        "horizon_class": str(row.get("horizon_class") or "*"),
        "market_regime": str(row.get("market_regime") or "*"),
        "sample_count": _safe_int(row.get("sample_count")),
        "confidence_score": _safe_float(row.get("confidence_score")),
        "position_authority": str(contract.get("position_authority") or ""),
    }


def _feedback_learning_refs(trace: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    source = trace.get("learning_used") if isinstance(trace.get("learning_used"), dict) else trace
    context = source.get("learning_context") if isinstance(source.get("learning_context"), dict) else {}
    memory_trace = context.get("memory_trace") if isinstance(context.get("memory_trace"), dict) else {}
    memory_refs = memory_trace.get("selected_memory_refs")
    if not isinstance(memory_refs, list):
        memory_refs = []
    policies = ((source.get("adaptive_policy_state") or {}).get("policies") or [])
    if not policies and isinstance(source.get("adaptive_policy_applied"), list):
        policies = source.get("adaptive_policy_applied")
    if not isinstance(policies, list):
        policies = []
    return (
        [item for item in memory_refs if isinstance(item, dict)],
        [_policy_ref(item) for item in policies if isinstance(item, dict)],
    )


def _feedback_label(
    *,
    memory_refs: List[Dict[str, Any]],
    policy_refs: List[Dict[str, Any]],
    target_lots: int,
    executed_lots: int,
    pnl: float,
    no_trade_reason: str,
) -> str:
    if not memory_refs and not policy_refs:
        return "no_learning_context_observed"
    if target_lots == 0:
        return "learning_observed_no_position"
    if executed_lots <= 0:
        return f"learning_position_not_executed:{no_trade_reason or 'unknown'}"
    if pnl > 0:
        return "learning_position_executed_profit"
    if pnl < 0:
        return "learning_position_executed_loss"
    return "learning_position_executed_flat"


def _candidate_side_from_snapshot(snapshot: Dict[str, Any]) -> str:
    neutral_observations = _neutral_opportunity_observations(snapshot)
    side_votes = Counter(
        str(item.get("counterfactual_side") or "flat")
        for item in neutral_observations
        if item.get("counterfactual_side") in {"long", "short"}
    )
    if side_votes:
        side, _ = side_votes.most_common(1)[0]
        return side
    combo = _signal_combo_from_snapshot(snapshot)
    long_votes = sum(1 for item in combo if _signal_side(item) == "long")
    short_votes = sum(1 for item in combo if _signal_side(item) == "short")
    if long_votes > short_votes and long_votes > 0:
        return "long"
    if short_votes > long_votes and short_votes > 0:
        return "short"
    contract = final_action_contract_from_snapshot(snapshot)
    side = _final_action_semantic_view(contract).get("contract_side")
    return side if side in {"long", "short"} else "flat"


def _candidate_side_from_action(action: Any) -> str:
    action_value = str(action or "").lower()
    if action_value in {"open_long", "close_long"}:
        return "long"
    if action_value in {"open_short", "close_short"}:
        return "short"
    return "flat"


def _execution_result_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    result = snapshot.get("execution_result")
    return result if isinstance(result, dict) else {}


def _market_rule_block_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    translation = snapshot.get("execution_translation")
    if not isinstance(translation, dict):
        return {}
    block = translation.get("market_rule_block")
    return block if isinstance(block, dict) else {}


def _no_trade_category_strategy_note(category: str) -> str:
    notes = {
        "signal": (
            "No-trade category=signal: next analysis should identify what data or evidence would convert this "
            "from no-trade to tradable, rather than treating Neutral/weak evidence as permission."
        ),
        "risk": (
            "No-trade category=risk: next PM review should separate hard risk protection from soft sizing friction "
            "before relaxing any limit."
        ),
        "timing": (
            "No-trade category=timing: next analysis should test whether a different intraday trigger, pullback, "
            "or confirmation window would improve entry without future-data leakage."
        ),
        "execution": (
            "No-trade category=execution: next review should distinguish market infeasibility/data basis problems "
            "from strategy weakness before changing signal logic."
        ),
        "business": (
            "No-trade category=business: next PM review should confirm whether the existing position, rollover, "
            "delivery, or lifecycle state already expressed the intended exposure."
        ),
        "learning": (
            "No-trade category=learning: next Researcher review should verify whether the memory boundary helped "
            "avoid loss or suppressed valid alpha before promoting or relaxing it."
        ),
    }
    return notes.get(category, notes["signal"])


def _settled_trading_days(cursor: sqlite3.Cursor, config_id: str, after_date: str, through_date: str) -> List[str]:
    cursor.execute(
        '''
        SELECT DISTINCT substr(ds.trading_date, 1, 10) AS trading_day
        FROM daily_settlement ds
        JOIN portfolio p ON ds.portfolio_id = p.id
        WHERE p.config_id = ?
          AND substr(ds.trading_date, 1, 10) > ?
          AND substr(ds.trading_date, 1, 10) <= ?
        ORDER BY trading_day
        ''',
        (config_id, after_date, through_date),
    )
    return [str(row["trading_day"]) for row in cursor.fetchall() if row["trading_day"]]


def _ticker_base_price_on_day(cursor: sqlite3.Cursor, config_id: str, ticker: str, trading_day: str) -> float:
    cursor.execute(
        '''
        SELECT base_price, execution_price, open_price, prev_close_price
        FROM futures_recommendation
        WHERE config_id = ?
          AND underlying_code = ?
          AND substr(effective_trade_date, 1, 10) = ?
        ORDER BY created_at DESC
        LIMIT 1
        ''',
        (config_id, ticker, trading_day),
    )
    row = cursor.fetchone()
    if not row:
        return 0.0
    return _safe_float(row["base_price"] or row["execution_price"] or row["open_price"] or row["prev_close_price"], 0.0)


def _template_groups_from_completed_pairs(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
) -> Dict[Tuple[str, str, str, str, str], List[Dict[str, Any]]]:
    pairs = _completed_pairs_up_to(cursor, config_id=config_id, trading_date=trading_date)
    recommendation_lookup = _recommendations_by_id(
        cursor,
        [pair.get("open_recommendation_id") for pair in pairs if pair.get("open_recommendation_id")],
    )
    groups: Dict[Tuple[str, str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        recommendation = recommendation_lookup.get(str(pair.get("open_recommendation_id") or ""))
        snapshot = _recommendation_snapshot(recommendation or {})
        ticker = str(pair.get("ticker") or "").upper()
        side = str(pair.get("side") or "").lower()
        combo = _signal_combo_from_snapshot(snapshot)
        expected_days = _expected_horizon_days(snapshot, side)
        horizon = _horizon_class(expected_days, snapshot)
        regime = _market_regime(snapshot)
        template = _setup_type(side, combo, snapshot)
        item = dict(pair)
        item["setup_type"] = template
        item["signal_combo"] = combo
        groups[(ticker, side, template, horizon, regime)].append(item)
    return groups


def _causal_error_flags(payload: Dict[str, Any]) -> List[str]:
    names = (
        "direction_error",
        "horizon_error",
        "entry_error",
        "exit_error",
        "position_sizing_error",
        "auditor_error",
        "pm_error",
    )
    return [name for name in names if bool(payload.get(name))]


def _causal_rule_validation_decision(
    *,
    candidate_payload: Dict[str, Any],
    summary: Dict[str, Any],
    rule_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    sample_count = _safe_int(summary.get("total_trades"), 0)
    win_rate = _safe_float(summary.get("win_rate"), 0.0)
    net_pnl = _safe_float(summary.get("total_pnl"), 0.0)
    candidate_confidence = _safe_float(candidate_payload.get("confidence_score"), 0.0)
    min_samples = int(rule_cfg.get("min_samples", 0) or 0)
    min_candidate_confidence = _safe_float(rule_cfg.get("min_candidate_confidence"), 0.35)
    if sample_count < min_samples:
        return {
            "status": "insufficient_evidence_pending_rule_validation",
            "reason": f"sample_count {sample_count} < min_samples {min_samples}",
        }
    if candidate_confidence < min_candidate_confidence:
        return {
            "status": "validated_rule_rejected",
            "reason": (
                f"candidate_confidence {candidate_confidence:.2f} "
                f"< min_candidate_confidence {min_candidate_confidence:.2f}"
            ),
        }

    error_flags = _causal_error_flags(candidate_payload)
    has_do_not_trade = bool(str(candidate_payload.get("do_not_trade_reason") or "").strip())
    protect_min_win_rate = _safe_float(rule_cfg.get("protect_min_win_rate"), 0.60)
    protect_min_net_pnl = _safe_float(rule_cfg.get("protect_min_net_pnl"), 0.0)
    cap_max_win_rate = _safe_float(rule_cfg.get("cap_max_win_rate"), 0.40)
    cap_max_net_pnl = _safe_float(rule_cfg.get("cap_max_net_pnl"), 0.0)
    cap_multiplier = max(0.0, min(1.0, _safe_float(rule_cfg.get("cap_multiplier"), 0.50)))

    strong_performance = win_rate >= protect_min_win_rate and net_pnl > protect_min_net_pnl
    weak_performance = win_rate <= cap_max_win_rate or net_pnl < cap_max_net_pnl

    if strong_performance and not error_flags and not has_do_not_trade:
        return {
            "status": "validated_rule_applied",
            "policy_action": "protect",
            "multiplier": 1.0,
            "reason": "validated causal rule: positive mature template",
        }
    if weak_performance:
        reason = "validated causal rule: weak mature template"
        if error_flags or has_do_not_trade:
            reason = "validated causal rule: weak template with matching causal error note"
        return {
            "status": "validated_rule_applied",
            "policy_action": "cap",
            "multiplier": cap_multiplier,
            "reason": reason,
        }
    return {
        "status": "validated_rule_rejected",
        "reason": "completed samples do not support a protect/cap rule yet",
    }


def _trade_pair_performance_summary(pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary = summarize_trade_pairs(pairs)
    return {
        "total_trades": _safe_int(summary.get("total_trades")),
        "winning_trades": _safe_int(summary.get("winning_trades")),
        "losing_trades": _safe_int(summary.get("losing_trades")),
        "flat_trades": _safe_int(summary.get("flat_trades")),
        "win_rate": _safe_float(summary.get("win_rate")),
        "net_pnl": _safe_float(summary.get("total_pnl")),
        "avg_pnl": _safe_float(summary.get("avg_pnl")),
        "avg_return": _safe_float(summary.get("avg_return")),
    }


def _with_policy_performance_columns(payload: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
    """Expose performance evidence both in payload and top-level row columns."""
    result = dict(payload or {})
    result["sample_count"] = _safe_int(summary.get("total_trades"))
    result["win_rate"] = _safe_float(summary.get("win_rate"))
    result["net_pnl"] = _safe_float(summary.get("net_pnl"))
    result["avg_pnl"] = _safe_float(summary.get("avg_pnl"))
    return result


def _learning_attribution_from_recommendation(recommendation: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    snapshot = _recommendation_snapshot(recommendation or {})
    contract = final_action_contract_from_snapshot(snapshot)
    diagnostics = contract.get("learning_used") if isinstance(contract.get("learning_used"), dict) else {}
    reasons = [str(item) for item in (contract.get("reason_codes") or []) + (contract.get("risk_flags") or []) if item]
    return (
        learning_tags_from_context(reasons, diagnostics),
        learning_effects_from_context(reasons, diagnostics),
    )


def _learning_mechanisms_from_recommendation(
    recommendation: Dict[str, Any],
    *,
    infer_from_full_trace: bool = True,
) -> List[str]:
    snapshot = _recommendation_snapshot(recommendation or {})
    contract = final_action_contract_from_snapshot(snapshot)
    diagnostics = contract.get("learning_used") if isinstance(contract.get("learning_used"), dict) else {}
    reasons = [str(item) for item in (contract.get("reason_codes") or []) + (contract.get("risk_flags") or []) if item]
    return learning_mechanisms_from_context(
        reasons,
        diagnostics,
        snapshot=snapshot if infer_from_full_trace else None,
    )


def _learning_tags_from_recommendation(recommendation: Dict[str, Any]) -> List[str]:
    tags, _effects = _learning_attribution_from_recommendation(recommendation)
    return tags


def _technical_calibration_rules_from_performance(
    *,
    horizon: str,
    hit_rate: float,
    net_pnl: float,
    positive_hit_rate: float,
    weak_hit_rate: float,
) -> Dict[str, Any]:
    if horizon != "short":
        return {}
    if hit_rate >= positive_hit_rate and net_pnl > 0:
        return {
            "trend_short_multiplier": 0.95,
            "trend_long_multiplier": 1.05,
            "rsi_bullish_shift": -2,
            "rsi_bearish_shift": 2,
            "bollinger_std_multiplier": 1.03,
        }
    if hit_rate <= weak_hit_rate or net_pnl < 0:
        return {
            "trend_short_multiplier": 1.05,
            "trend_long_multiplier": 0.95,
            "rsi_bullish_shift": 2,
            "rsi_bearish_shift": -2,
            "bollinger_std_multiplier": 0.97,
        }
    return {}


def _neutral_accountability_digest_text(
    analyst: str,
    dominant_category: str,
    category_counts: Dict[str, Any],
    counterfactual_counts: Dict[str, Any] | None = None,
) -> str:
    counterfactual_counts = counterfactual_counts or {}
    counterfactual_suffix = ""
    observations = _safe_int(counterfactual_counts.get("observation_count"), 0)
    if observations:
        missed = _safe_int(counterfactual_counts.get("missed_opportunity_count"), 0)
        avoided = _safe_int(counterfactual_counts.get("reasonable_avoidance_count"), 0)
        counterfactual_pnl = _safe_float(counterfactual_counts.get("total_counterfactual_pnl"), 0.0)
        counterfactual_suffix = (
            f" counterfactual tracking: observations={observations}, missed={missed}, "
            f"reasonable_avoidance={avoided}, counterfactual_pnl={counterfactual_pnl:.0f}."
        )
    if dominant_category == "reasonable_avoidance":
        return (
            f"{analyst}: Neutral mostly avoided low-quality or conflicted setups. "
            "Keep requiring explicit evidence and a clear condition that would change the view."
            + counterfactual_suffix
        )
    if dominant_category == "evidence_gap_conservative":
        return (
            f"{analyst}: Neutral was mainly caused by evidence gaps. Improve evidence coverage, "
            "and do not convert missing optional data into directional conviction."
            + counterfactual_suffix
        )
    if dominant_category == "conservative_against_consensus":
        return (
            f"{analyst}: Neutral may have missed aligned directional evidence. In similar future cases, "
            "prefer a small probe only when market confirmation and invalidation are clear."
            + counterfactual_suffix
        )
    if dominant_category == "unaccountable_neutral":
        return (
            f"{analyst}: Neutral lacked required accountability fields. Future Neutral output must state "
            "missing evidence, conflicting factors, and the condition that would change the view."
            + counterfactual_suffix
        )
    return (
        f"{analyst}: Neutral accountability recorded with categories {dict(category_counts)}. "
        "Use this as a structured prior for future signal discipline."
        + counterfactual_suffix
    )


def _build_capital_deployment_state(
    *,
    cfg: Dict[str, Any],
    settlement_row: Optional[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    no_trade_reason_counter: Counter,
) -> Dict[str, Any]:
    capital_cfg = cfg.get("capital_utilization_control", {}) or {}
    target_min = float(capital_cfg.get("target_margin_ratio_min", 0.16))
    target_max = float(capital_cfg.get("target_margin_ratio_max", 0.20))
    if settlement_row:
        current_margin = _safe_float(settlement_row.get("current_margin"))
        capital_base = _futures_account_equity(
            _safe_float(settlement_row.get("current_balance")),
            current_margin,
        )
    else:
        capital_base = _safe_float(cfg.get("cashflow"))
        current_margin = 0.0
    current_ratio = current_margin / capital_base if capital_base > 0 else 0.0
    target_abs_min = capital_base * target_min
    target_abs_max = capital_base * target_max
    under = current_ratio < target_min
    over = current_ratio > target_max
    if under:
        allocation_tier = "under_deployed"
    elif over:
        allocation_tier = "over_deployed"
    else:
        allocation_tier = "target_band"
    non_flat_recommendations = 0
    for recommendation in strategy_recommendations:
        snapshot = _recommendation_snapshot(recommendation)
        if _recommendation_side(recommendation, snapshot) in {"long", "short"}:
            non_flat_recommendations += 1
    if not under:
        reason_bucket = "target_met"
    elif non_flat_recommendations <= 0:
        reason_bucket = "high_score_signal_shortage"
    elif no_trade_reason_counter:
        reason_bucket = str(no_trade_reason_counter.most_common(1)[0][0])
    else:
        reason_bucket = "soft_cap_binding_or_execution_miss"

    margin_gap_to_min = max(0.0, target_abs_min - current_margin)
    deployment_diagnostics = _build_capital_deployment_diagnostics(
        cfg=cfg,
        allocation_tier=allocation_tier,
        reason_bucket=reason_bucket,
        current_ratio=current_ratio,
        target_min=target_min,
        target_max=target_max,
        margin_gap_to_min=margin_gap_to_min,
        strategy_recommendations=strategy_recommendations,
        no_trade_reason_counter=no_trade_reason_counter,
    )
    deployment_plan = {
        "release_order": [
            "release high-template cold-start cap",
            "allow confirmed same-side add-on",
            "add low-correlation probe positions",
            "only then soften non-hard thresholds",
        ],
        "must_not_force_low_quality_trades": True,
        "diagnostics": deployment_diagnostics,
        "alpha_release_candidates": deployment_diagnostics["alpha_release_candidates"],
        "parameter_review": deployment_diagnostics["parameter_review"],
    }
    state = {
        "capital_base": capital_base,
        "current_margin": current_margin,
        "current_margin_ratio": current_ratio,
        "target_margin_ratio_min": target_min,
        "target_margin_ratio_max": target_max,
        "target_margin_abs_min": target_abs_min,
        "target_margin_abs_max": target_abs_max,
        "underutilization_breach": under,
        "overutilization_breach": over,
        "capital_allocation_tier": allocation_tier,
        "margin_gap_to_min": margin_gap_to_min,
        "reason_bucket": reason_bucket,
        "deployment_plan": deployment_plan,
        "capital_diagnostics": deployment_diagnostics,
    }
    return state


def _build_causal_evidence_pack(
    *,
    config_id: str,
    trading_date: str,
    strategy_recommendations: List[Dict[str, Any]],
    settlement_row: Optional[Dict[str, Any]],
    no_trade_reason_counter: Counter,
) -> Dict[str, Any]:
    return {
        "evidence_pack_id": str(uuid.uuid4()),
        "config_id": config_id,
        "trading_date": trading_date,
        "pre_trade_evidence": [
            {
                "recommendation_id": row.get("id"),
                "ticker": row.get("underlying_code"),
                "action": row.get("action"),
                "lots": row.get("lots"),
                "signal_snapshot": _recommendation_snapshot(row),
            }
            for row in strategy_recommendations
        ],
        "post_trade_outcome": {
            "daily_pnl": _safe_float((settlement_row or {}).get("daily_pnl")),
            "commission": _safe_float((settlement_row or {}).get("commission")),
            "current_margin_ratio": _safe_float((settlement_row or {}).get("margin_ratio")),
            "no_trade_reasons": dict(no_trade_reason_counter),
            "no_trade_reason_categories": _no_trade_reason_category_counts(no_trade_reason_counter),
        },
    }



EXPORTED_RESEARCH_REVIEW_HELPERS = tuple(
    name
    for name, value in globals().items()
    if name.startswith("_") and callable(value) and not name.startswith("__")
)

