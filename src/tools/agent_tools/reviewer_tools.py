from __future__ import annotations

"""Reviewer tools for Phase4 validation, reporting, and daily learning."""

import json
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from math import isclose
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel, Field

from database.artifact_store import (
    externalize_json_for_db,
    externalize_text_for_db,
    load_externalized_json,
)
from graph.schema import RecommendationSourceType, TradingPhase
from util.futures_audit import (
    build_actual_transactions,
    classify_zero_transaction_day,
    infer_no_trade_reason,
    normalize_no_trade_reason,
)
from util.futures_trade_pairs import build_completed_trade_pairs, summarize_trade_pairs
from util.learning_attribution import (
    learning_effect_counts,
    learning_effects_from_context,
    learning_tags_from_context,
    summarize_pairs_by_learning_effect,
)
from util.logger import logger
from tools.agent_tools.neutral_accountability import build_neutral_accountability_summary


ANALYSTS = ("technical", "fundamental", "commodity_news")
DEFAULT_ANALYST_HORIZON = {
    "technical": "short",
    "fundamental": "medium",
    "commodity_news": "event_short",
}


class CausalReviewLLMOutput(BaseModel):
    primary_cause: str = Field(default="unknown")
    direction_error: bool = Field(default=False)
    horizon_error: bool = Field(default=False)
    entry_error: bool = Field(default=False)
    exit_error: bool = Field(default=False)
    position_sizing_error: bool = Field(default=False)
    auditor_error: bool = Field(default=False)
    pm_error: bool = Field(default=False)
    missed_factors: List[str] = Field(default_factory=list)
    analyst_lessons: List[str] = Field(default_factory=list)
    do_not_trade_reason: str = Field(default="")
    similar_case_key: str = Field(default="")
    confidence_score: float = Field(default=0.0)
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
SRC_ROOT = Path(__file__).resolve().parents[2]


def _normalize_date(value) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)


def _fetchone(cursor, query: str, params: tuple):
    cursor.execute(query, params)
    row = cursor.fetchone()
    return dict(row) if row else None


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


def _position_exposures(positions: Dict[str, Any], account_equity: float) -> tuple[float, Dict[str, float]]:
    if account_equity <= 0:
        return 0.0, {}
    net_exposure = 0.0
    single_exposures: Dict[str, float] = {}
    for ticker, position in (positions or {}).items():
        if not isinstance(position, dict):
            continue
        shares = int(position.get("shares") or 0)
        value = float(position.get("value") or 0.0)
        if shares == 0:
            continue
        signed_ratio = (1.0 if shares > 0 else -1.0) * value / account_equity
        net_exposure += signed_ratio
        single_exposures[ticker] = abs(value) / account_equity
    return net_exposure, single_exposures


def _apply_net_exposure_review(
    *,
    trading_date: str,
    cfg: Dict[str, Any],
    net_exposure: float,
    warnings: List[str],
    errors: List[str],
    recommendations: Optional[List[Dict[str, Any]]] = None,
) -> None:
    net_exposure_config = cfg.get("net_exposure_control") or cfg.get("risk_control", {}).get("net_exposure_control", {})
    base_max_net_exposure = float(net_exposure_config.get("max_net_exposure", 0.50))
    max_net_exposure = base_max_net_exposure
    cap_mode = "base"
    cap_source = ""
    for recommendation in recommendations or []:
        snapshot = _json_loads(recommendation.get("signal_snapshot")) or {}
        if not isinstance(snapshot, dict):
            continue
        pre_open_plan = snapshot.get("pre_open_plan") if isinstance(snapshot.get("pre_open_plan"), dict) else {}
        control_diagnostics = (
            pre_open_plan.get("control_diagnostics")
            if isinstance(pre_open_plan.get("control_diagnostics"), dict)
            else {}
        )
        execution_translation = (
            snapshot.get("execution_translation")
            if isinstance(snapshot.get("execution_translation"), dict)
            else {}
        )
        candidates = [
            execution_translation.get("dynamic_net_exposure_control"),
            snapshot.get("dynamic_net_exposure_control"),
            control_diagnostics.get("net_exposure_control"),
            pre_open_plan.get("dynamic_net_exposure_control"),
        ]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            mode = str(candidate.get("mode") or candidate.get("cap_mode") or "").lower()
            if mode != "strong_opportunity":
                continue
            try:
                candidate_cap = float(candidate.get("max_net_exposure"))
            except (TypeError, ValueError):
                continue
            if candidate_cap > max_net_exposure:
                max_net_exposure = candidate_cap
                cap_mode = "strong_opportunity"
                cap_source = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "")

    drift_tolerance = float(net_exposure_config.get("phase4_drift_tolerance", 0.01))
    hard_limit = max_net_exposure + max(0.001, drift_tolerance)

    if abs(net_exposure) > hard_limit:
        errors.append(
            f"net exposure exceeds cap on {trading_date}: "
            f"{net_exposure:.2%} > {max_net_exposure:.2%}"
        )
    elif abs(net_exposure) > max_net_exposure + 0.001:
        warnings.append(
            f"net exposure drifted above cap on {trading_date} but stayed within tolerance: "
            f"{net_exposure:.2%} <= {hard_limit:.2%}"
        )
    elif cap_mode == "strong_opportunity" and abs(net_exposure) > base_max_net_exposure + 0.001:
        source_text = f" via {cap_source}" if cap_source else ""
        warnings.append(
            f"net exposure above base cap on {trading_date} but within dynamic strong-opportunity cap"
            f"{source_text}: {net_exposure:.2%} <= {max_net_exposure:.2%}"
        )


def _group_transactions_by_recommendation(transactions: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for transaction in transactions:
        recommendation_id = transaction.get("recommendation_id")
        if recommendation_id:
            grouped.setdefault(str(recommendation_id), []).append(transaction)
    return grouped


def _resolve_no_trade_reason(recommendation: Dict[str, Any], has_transactions: bool = False) -> str | None:
    if has_transactions:
        return None
    snapshot = _json_loads(recommendation.get("signal_snapshot")) or {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    reason = infer_no_trade_reason(snapshot, recommendation.get("warning_message"))
    if reason:
        return normalize_no_trade_reason(reason)
    status = str(recommendation.get("status") or "").lower()
    if status in {"cancelled", "rejected", "expired"}:
        return status
    action = str(recommendation.get("action") or "").lower()
    if action == "hold" or int(recommendation.get("lots") or 0) == 0:
        return "position_matched"
    return None


def _actual_transactions_from_recommendation_audit(recommendation: Dict[str, Any]) -> List[Dict[str, Any]]:
    snapshot = _json_loads(recommendation.get("signal_snapshot")) or {}
    if not isinstance(snapshot, dict):
        return []
    execution_result = snapshot.get("execution_result")
    if not isinstance(execution_result, dict):
        return []
    actual_transactions = execution_result.get("actual_transactions") or []
    if isinstance(actual_transactions, str):
        actual_transactions = _json_loads(actual_transactions) or []
    if not isinstance(actual_transactions, list):
        return []
    return build_actual_transactions([item for item in actual_transactions if isinstance(item, dict)])


def _validate_recommendation_execution_audit(
    recommendations: List[Dict[str, Any]],
    transactions_by_recommendation: Dict[str, List[Dict[str, Any]]],
    errors: List[str],
) -> Counter:
    no_trade_reason_counter: Counter = Counter()
    for recommendation in recommendations:
        recommendation_id = str(recommendation.get("id") or "")
        transactions = transactions_by_recommendation.get(recommendation_id, [])
        expected_transactions = _actual_transactions_from_recommendation_audit(recommendation)
        if expected_transactions and not transactions:
            reason = _resolve_no_trade_reason(recommendation, has_transactions=False)
            if reason:
                no_trade_reason_counter[reason] += 1
            else:
                errors.append(
                    f"recommendation {recommendation_id} expected execution but has no transaction and no no-trade reason"
                )
            continue
        if not expected_transactions and transactions:
            errors.append(
                f"recommendation {recommendation_id} has unexpected transactions: {len(transactions)}"
            )
        if not transactions:
            reason = _resolve_no_trade_reason(recommendation, has_transactions=False)
            if reason:
                no_trade_reason_counter[reason] += 1
    return no_trade_reason_counter


def _market_confirmation_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    plan = snapshot.get("pre_open_plan") if isinstance(snapshot.get("pre_open_plan"), dict) else {}
    confirmation = plan.get("market_confirmation") or snapshot.get("market_confirmation")
    return confirmation if isinstance(confirmation, dict) else {}


def _format_compact_list(values: Iterable[Any], limit: int = 8) -> str:
    items = [str(value) for value in values if value is not None]
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", +{len(items) - limit} more"


def _collect_market_confirmation_quality_summary(recommendations: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "ticker_count": 0,
        "missing_by_status": {},
        "missing_by_status_feature": {},
        "missing_by_feature": {},
        "fallback_covered_by_feature": {},
        "unsupported_by_feature": {},
        "tickers_with_actionable_missing": [],
        "tickers_with_parameter_errors": [],
        "tickers_with_provider_errors": [],
    }
    seen_tickers = set()
    missing_by_status: Dict[str, set[str]] = defaultdict(set)
    missing_by_status_feature: Dict[str, Dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    missing_by_feature: Dict[str, set[str]] = defaultdict(set)
    fallback_by_feature: Dict[str, set[str]] = defaultdict(set)
    unsupported_by_feature: Dict[str, set[str]] = defaultdict(set)
    actionable_tickers = set()
    parameter_error_tickers = set()
    provider_error_tickers = set()

    for recommendation in recommendations:
        snapshot = _json_loads(recommendation.get("signal_snapshot")) or {}
        if not isinstance(snapshot, dict):
            continue
        confirmation = _market_confirmation_from_snapshot(snapshot)
        if not confirmation:
            continue
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "").upper()
        if not ticker:
            continue
        seen_tickers.add(ticker)
        feature_status = confirmation.get("feature_status") or {}
        data_missing = set(str(item) for item in (confirmation.get("data_missing") or []))
        fallback_covered = set(str(item) for item in (confirmation.get("fallback_covered_missing") or []))
        unsupported = set(str(item) for item in (confirmation.get("unsupported_features") or []))
        parameter_errors = set(str(item) for item in (confirmation.get("parameter_errors") or []))
        status_groups = confirmation.get("data_status_groups") or {}

        for feature in data_missing:
            status = str(feature_status.get(feature) or "missing")
            missing_by_status[status].add(ticker)
            missing_by_status_feature[status][feature].add(ticker)
            missing_by_feature[feature].add(ticker)
        for feature in fallback_covered:
            fallback_by_feature[feature].add(ticker)
        for feature in unsupported:
            unsupported_by_feature[feature].add(ticker)
        if data_missing:
            actionable_tickers.add(ticker)
        if parameter_errors:
            parameter_error_tickers.add(ticker)
        for status in ("provider_error", "permission_error"):
            if status_groups.get(status):
                provider_error_tickers.add(ticker)

    summary["ticker_count"] = len(seen_tickers)
    summary["missing_by_status"] = {
        status: sorted(tickers)
        for status, tickers in sorted(missing_by_status.items())
    }
    summary["missing_by_status_feature"] = {
        status: {
            feature: sorted(tickers)
            for feature, tickers in sorted(feature_map.items())
        }
        for status, feature_map in sorted(missing_by_status_feature.items())
    }
    summary["missing_by_feature"] = {
        feature: sorted(tickers)
        for feature, tickers in sorted(missing_by_feature.items())
    }
    summary["fallback_covered_by_feature"] = {
        feature: sorted(tickers)
        for feature, tickers in sorted(fallback_by_feature.items())
    }
    summary["unsupported_by_feature"] = {
        feature: sorted(tickers)
        for feature, tickers in sorted(unsupported_by_feature.items())
    }
    summary["tickers_with_actionable_missing"] = sorted(actionable_tickers)
    summary["tickers_with_parameter_errors"] = sorted(parameter_error_tickers)
    summary["tickers_with_provider_errors"] = sorted(provider_error_tickers)
    return summary


def _market_confirmation_quality_warnings(summary: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    parameter_tickers = summary.get("tickers_with_parameter_errors") or []
    provider_tickers = summary.get("tickers_with_provider_errors") or []
    unsupported_by_feature = summary.get("unsupported_by_feature") or {}

    if parameter_tickers:
        warnings.append(
            "market confirmation parameter errors: "
            f"tickers={_format_compact_list(parameter_tickers)}"
        )
    if provider_tickers:
        warnings.append(
            "market confirmation provider/permission errors: "
            f"tickers={_format_compact_list(provider_tickers)}"
        )

    if unsupported_by_feature:
        warnings.append(
            "market confirmation unsupported features: "
            + "; ".join(
                f"{feature}({len(tickers)} tickers)"
                for feature, tickers in sorted(unsupported_by_feature.items())
            )
        )
    return warnings


def _market_confirmation_quality_infos(summary: Dict[str, Any]) -> List[str]:
    infos: List[str] = []
    missing_by_status = summary.get("missing_by_status") or {}
    missing_by_status_feature = summary.get("missing_by_status_feature") or {}
    fallback_by_feature = summary.get("fallback_covered_by_feature") or {}

    no_data_tickers = sorted(set(missing_by_status.get("no_data") or []))
    no_data_feature_map = missing_by_status_feature.get("no_data") or {}
    no_data_features = sorted(no_data_feature_map)
    if no_data_features:
        infos.append(
            "market confirmation optional no data: "
            f"features={_format_compact_list(no_data_features)} | "
            f"tickers={_format_compact_list(no_data_tickers)}"
        )

    if fallback_by_feature:
        infos.append(
            "market confirmation fallback covered optional missing: "
            + "; ".join(
                f"{feature}({len(tickers)} tickers)"
                for feature, tickers in sorted(fallback_by_feature.items())
            )
        )
    return infos


def _collect_recommendation_quality_warnings(recommendations: List[Dict[str, Any]]) -> Tuple[List[str], Dict[str, Any]]:
    warnings: List[str] = []
    market_summary = _collect_market_confirmation_quality_summary(recommendations)
    warnings.extend(_market_confirmation_quality_warnings(market_summary))
    market_summary["info_messages"] = _market_confirmation_quality_infos(market_summary)
    for recommendation in recommendations:
        snapshot = _json_loads(recommendation.get("signal_snapshot")) or {}
        if not isinstance(snapshot, dict):
            continue
        ticker = recommendation.get("underlying_code") or recommendation.get("ticker")
        plan = snapshot.get("pre_open_plan") if isinstance(snapshot.get("pre_open_plan"), dict) else {}
        reasons = plan.get("control_reasons") or plan.get("reasons") or []
        if reasons:
            warnings.append(f"{ticker}: strategy controls recorded: {reasons}")
    return warnings, market_summary


CAPITAL_DEPLOYMENT_REASON_PROFILES: Dict[str, Dict[str, Any]] = {
    "llm_neutral": {
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
    "trade_auditor_block": {
        "category": "auditor_suppression",
        "risk_control_normal": "depends_on_reasons",
        "alpha_expansion_allowed": "conditional",
        "diagnosis": "The deterministic trade auditor suppressed the PM target.",
        "suggested_action": "Keep hard-risk blocks; only soften auditor caps for protected/deployable templates with positive evidence.",
    },
    "trade_auditor_reduce_only": {
        "category": "auditor_suppression",
        "risk_control_normal": "depends_on_reasons",
        "alpha_expansion_allowed": False,
        "diagnosis": "The auditor allowed only risk-reducing exposure.",
        "suggested_action": "Do not increase until the underlying auditor reason is cleared.",
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
    "llm_neutral",
    "intraday_trigger_not_met",
    "intraday_opening_range_incomplete",
    "trade_auditor_block",
    "trade_auditor_reduce_only",
    "minimum_new_entry_threshold",
    "cooling_period",
    "ticker_loss_control",
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


def _control_reasons_from_plan(plan: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    for key in ("control_reasons", "reasons"):
        value = plan.get(key)
        if isinstance(value, list):
            reasons.extend(str(item) for item in value if item)
    rebalance = plan.get("rebalance_summary") if isinstance(plan.get("rebalance_summary"), dict) else {}
    value = rebalance.get("control_reasons")
    if isinstance(value, list):
        reasons.extend(str(item) for item in value if item)
    controls = plan.get("strategy_controls") if isinstance(plan.get("strategy_controls"), dict) else {}
    value = controls.get("reasons")
    if isinstance(value, list):
        reasons.extend(str(item) for item in value if item)
    auditor = plan.get("trade_auditor") or plan.get("decision_planner") or {}
    if isinstance(auditor, dict):
        value = auditor.get("reasons")
        if isinstance(value, list):
            reasons.extend(str(item) for item in value if item)
    return sorted(set(reasons))


def _auditor_decision_from_plan(plan: Dict[str, Any]) -> str:
    auditor = plan.get("trade_auditor") or plan.get("decision_planner") or {}
    if isinstance(auditor, dict):
        return str(auditor.get("decision") or "").lower()
    return ""


def _memory_state_from_plan(plan: Dict[str, Any]) -> str:
    controls = plan.get("strategy_controls") if isinstance(plan.get("strategy_controls"), dict) else {}
    diagnostics = controls.get("diagnostics") if isinstance(controls.get("diagnostics"), dict) else {}
    learning = diagnostics.get("capital_utilization_learning") if isinstance(diagnostics.get("capital_utilization_learning"), dict) else {}
    for key in ("protected_memory", "recovering_memory"):
        row = learning.get(key)
        if isinstance(row, dict) and row.get("memory_state"):
            return str(row.get("memory_state")).lower()
    auditor = plan.get("trade_auditor") or plan.get("decision_planner") or {}
    auditor_diag = auditor.get("diagnostics") if isinstance(auditor, dict) and isinstance(auditor.get("diagnostics"), dict) else {}
    memory = auditor_diag.get("strategy_memory") if isinstance(auditor_diag.get("strategy_memory"), dict) else {}
    for key in ("combo", "side_memory"):
        row = memory.get(key)
        if isinstance(row, dict) and row.get("memory_state"):
            return str(row.get("memory_state")).lower()
    for row in memory.get("records") or []:
        if isinstance(row, dict) and row.get("memory_state"):
            return str(row.get("memory_state")).lower()
    return ""


def _has_hard_capital_reason(reasons: Iterable[str]) -> bool:
    text = " ".join(str(reason).lower() for reason in reasons)
    return any(token in text for token in CAPITAL_HARD_RISK_REASON_TOKENS)


def _recommendation_capital_item(recommendation: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _recommendation_snapshot(recommendation)
    plan = _pre_open_plan(snapshot)
    confirmation = _market_confirmation(snapshot)
    rebalance = plan.get("rebalance_summary") if isinstance(plan.get("rebalance_summary"), dict) else {}
    execution_result = snapshot.get("execution_result") if isinstance(snapshot.get("execution_result"), dict) else {}
    ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "").upper()
    target_ratio = _safe_float(plan.get("target_position_ratio") or recommendation.get("target_position_ratio"))
    current_ratio = _safe_float(plan.get("current_ticker_exposure"))
    target_side = _target_side_from_ratio(target_ratio)
    no_trade_reason = (
        execution_result.get("no_trade_reason")
        or plan.get("tradable_lots_reason")
        or rebalance.get("reason")
        or recommendation.get("warning_message")
    )
    no_trade_reason = normalize_no_trade_reason(no_trade_reason) if no_trade_reason else ""
    control_reasons = _control_reasons_from_plan(plan)
    auditor_decision = _auditor_decision_from_plan(plan)
    memory_state = _memory_state_from_plan(plan)
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
    alpha_release_eligible = (
        target_side in {"long", "short"}
        and abs(target_ratio) > 1e-12
        and confirmation_score >= required_score
        and no_trade_reason not in CAPITAL_NON_RELEASE_NO_TRADE_REASONS
        and not hard_capital_reason
        and auditor_decision not in {"block", "reduce_only"}
        and "weak_block" not in memory_state
    )
    return {
        "ticker": ticker,
        "side": target_side,
        "target_position_ratio": target_ratio,
        "current_position_ratio": current_ratio,
        "target_lots": _safe_int(plan.get("target_lots_estimate")),
        "current_lots": _safe_int(plan.get("current_lots_before_open")),
        "tradable_lots": _safe_int(plan.get("tradable_lots_if_executed_now")),
        "no_trade_reason": no_trade_reason,
        "rebalance_action_type": str(rebalance.get("action_type") or ""),
        "confirmation_score": confirmation_score,
        "signal_confidence": _safe_float(plan.get("signal_confidence")),
        "auditor_decision": auditor_decision,
        "memory_state": memory_state,
        "control_reasons": control_reasons,
        "hard_capital_reason": hard_capital_reason,
        "alpha_release_eligible": alpha_release_eligible,
        "required_confirmation_score": required_score,
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
            "rebalance_action_type": item["rebalance_action_type"],
            "tradable_lots": item["tradable_lots"],
        }
        for item in recommendation_items
        if item["alpha_release_eligible"]
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
    auditor_suppression_cases = [
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
                "trade_auditor_block",
                "trade_auditor_reduce_only",
                "trade_auditor_scale_to_zero",
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
    if no_trade_reason_counter.get("trade_auditor_block"):
        parameter_review.append(
            {
                "scope": "trade_auditor",
                "reason": "trade_auditor_block",
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
        "category_counts": _sorted_counter_dict(category_counts),
        "action_counts": _sorted_counter_dict(action_counts),
        "reason_profiles": reason_profiles,
        "alpha_release_candidate_count": len(alpha_release_candidates),
        "alpha_release_candidates": alpha_release_candidates[:10],
        "execution_gate_candidates": execution_gate_candidates[:10],
        "auditor_suppression_cases": auditor_suppression_cases[:10],
        "position_matched_watchlist": position_matched_watchlist[:10],
        "size_threshold_cases": size_threshold_cases[:10],
        "parameter_review": parameter_review,
        "do_not_force_low_quality_trades": True,
        "capital_release_rule": (
            "Increase utilization only through high-confirmation, protected/deployable/recovering, "
            "non-hard-risk alpha candidates; never fill the margin target with weak/watchlist trades."
        ),
    }


def _build_summary_payload(
    *,
    cfg: Dict[str, Any],
    trading_date: str,
    phase1: Dict[str, Any] | None,
    phase2: Dict[str, Any] | None,
    phase3: Dict[str, Any] | None,
    phase4_status: str,
    strategy_count: int,
    rollover_count: int,
    phase1_transaction_count: int,
    phase2_transaction_count: int,
    no_trade_reason_counter: Counter,
    settlement_row: Dict[str, Any] | None,
    warnings: List[str],
    errors: List[str],
    market_confirmation_quality: Optional[Dict[str, Any]] = None,
    capital_deployment_state: Optional[Dict[str, Any]] = None,
    neutral_accountability: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    capital_diagnostics = {}
    if isinstance(capital_deployment_state, dict):
        capital_diagnostics = capital_deployment_state.get("capital_diagnostics") or {}
    return {
        "exp_name": cfg["exp_name"],
        "trading_date": trading_date,
        "validation_status": phase4_status,
        "phases": {
            "phase1": phase1.get("status") if phase1 else "missing",
            "phase2": phase2.get("status") if phase2 else "missing",
            "phase3": phase3.get("status") if phase3 else "missing",
            "phase4": phase4_status,
        },
        "recommendation_summary": {
            "strategy_count": strategy_count,
            "rollover_count": rollover_count,
            "total_count": strategy_count + rollover_count,
        },
        "transaction_summary": {
            "phase1_transaction_count": phase1_transaction_count,
            "phase2_transaction_count": phase2_transaction_count,
        },
        "no_trade_reason_counts": dict(no_trade_reason_counter),
        "settlement_summary": {
            "current_balance": settlement_row.get("current_balance") if settlement_row else None,
            "current_margin": settlement_row.get("current_margin") if settlement_row else None,
            "account_equity": (
                _futures_account_equity(settlement_row.get("current_balance"), settlement_row.get("current_margin"))
                if settlement_row
                else None
            ),
            "daily_pnl": settlement_row.get("daily_pnl") if settlement_row else None,
            "commission": settlement_row.get("commission") if settlement_row else None,
        },
        "warnings": warnings,
        "errors": errors,
        "market_confirmation_quality": market_confirmation_quality or {},
        "capital_deployment_diagnostics": capital_diagnostics,
        "neutral_accountability": neutral_accountability or {},
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


def _ticker_daily_pnl_rows(cursor, config_id: str, trading_date: str) -> Dict[str, Dict[str, Any]]:
    cursor.execute(
        """
        SELECT tdp.*
        FROM ticker_daily_pnl tdp
        JOIN portfolio p ON tdp.portfolio_id = p.id
        WHERE p.config_id = ?
          AND substr(tdp.trading_date, 1, 10) = ?
        ORDER BY tdp.ticker
        """,
        (config_id, trading_date),
    )
    return {row["ticker"]: dict(row) for row in cursor.fetchall()}


def _build_daily_transaction_report(
    *,
    cfg: Dict[str, Any],
    trading_date: str,
    settlement_row: Dict[str, Any] | None,
    latest_portfolio: Dict[str, Any] | None,
    strategy_recommendations: List[Dict[str, Any]],
    recommendations: List[Dict[str, Any]],
    phase2_transactions: List[Dict[str, Any]],
    ticker_pnl: Dict[str, Dict[str, Any]],
) -> str:
    account_equity = (
        _futures_account_equity(settlement_row.get("current_balance"), settlement_row.get("current_margin"))
        if settlement_row
        else 0.0
    )
    grouped_transactions: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for tx in phase2_transactions:
        grouped_transactions[str(tx.get("ticker") or "UNKNOWN")].append(tx)
    traded_tickers = set(grouped_transactions)
    lines = [
        f"{trading_date} transaction review",
        "",
        "Account settlement",
        f"- current_balance: {_money(settlement_row.get('current_balance') if settlement_row else 0)}",
        f"- current_margin: {_money(settlement_row.get('current_margin') if settlement_row else 0)}",
        f"- account_equity: {_money(account_equity)}",
        f"- daily_pnl: {_signed_money(settlement_row.get('daily_pnl') if settlement_row else 0)}",
        f"- commission: {_money(settlement_row.get('commission') if settlement_row else 0)}",
        "",
        "Executed transactions",
    ]
    if not phase2_transactions:
        lines.append("- none")
    for ticker in sorted(grouped_transactions):
        pnl_row = ticker_pnl.get(ticker, {})
        lines.append(
            f"- {ticker}: daily_pnl={_signed_money(pnl_row.get('daily_pnl'))}; "
            f"holding={_signed_money(pnl_row.get('holding_pnl'))}; "
            f"new={_signed_money(pnl_row.get('new_position_pnl'))}; "
            f"close={_signed_money(pnl_row.get('close_pnl'))}"
        )
        for tx in grouped_transactions[ticker]:
            lines.append(
                "  "
                f"{tx.get('action')} {int(tx.get('lots') or 0)} lots "
                f"contract={tx.get('contract_code')} price={_money(tx.get('execution_price'))} "
                f"commission={_money(tx.get('commission'))} recommendation_id={tx.get('recommendation_id')}"
            )
    lines.extend(["", "Untraded strategy recommendations"])
    recommendations_by_id = {str(item.get("id")): item for item in recommendations if item.get("id")}
    for recommendation in strategy_recommendations:
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "UNKNOWN")
        if ticker in traded_tickers:
            continue
        reason = _resolve_no_trade_reason(recommendation, has_transactions=False) or str(recommendation.get("status") or "unknown")
        snapshot = _json_loads(recommendation.get("signal_snapshot")) or {}
        combo = _signal_combo_from_snapshot(snapshot) if isinstance(snapshot, dict) else ["Neutral", "Neutral", "Neutral"]
        lines.append(
            f"- {ticker}: no trade; reason={reason}; combo={combo}; "
            f"target_ratio={_percent((_pre_open_plan(snapshot) if isinstance(snapshot, dict) else {}).get('target_position_ratio'))}"
        )
    lines.extend(["", "Traceability"])
    lines.append(f"- strategy_recommendations={len(strategy_recommendations)}")
    lines.append(f"- total_recommendations={len(recommendations_by_id)}")
    lines.append(f"- phase2_transactions={len(phase2_transactions)}")
    return "\n".join(lines) + "\n"


def _write_daily_transaction_report(
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    cursor,
    settlement_row: Dict[str, Any] | None,
    latest_portfolio: Dict[str, Any] | None,
    recommendations: List[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    phase2_transactions: List[Dict[str, Any]],
) -> Path:
    report_text = _build_daily_transaction_report(
        cfg=cfg,
        trading_date=trading_date,
        settlement_row=settlement_row,
        latest_portfolio=latest_portfolio,
        strategy_recommendations=strategy_recommendations,
        recommendations=recommendations,
        phase2_transactions=phase2_transactions,
        ticker_pnl=_ticker_daily_pnl_rows(cursor, config_id, trading_date),
    )
    output_path = SRC_ROOT / "logs" / f"{trading_date}_transaction.log"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_text, encoding="utf-8")
    return output_path


def _report_rows(cursor: sqlite3.Cursor, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    cursor.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def _template_report_line(row: Dict[str, Any]) -> str:
    return (
        f"- {row.get('ticker')}/{row.get('side')}/{row.get('horizon_class')}: "
        f"{row.get('signal_template')} | samples={int(row.get('sample_count') or 0)} "
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


def _write_reviewer_learning_report(
    *,
    cursor: sqlite3.Cursor,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    learning_summary: Dict[str, Any],
    output_root: Optional[Path] = None,
    run_id: Optional[str] = None,
) -> Dict[str, str]:
    """Write the Phase4 reviewer learning pack required for audit and replay."""
    run_key = run_id or getattr(logger, "run_id", None) or "manual"
    report_dir = (output_root or (SRC_ROOT / "logs" / "reviewer")) / str(run_key)
    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / f"{trading_date}.md"
    json_path = report_dir / f"{trading_date}.json"

    template_where = (
        "config_id = ? AND sample_count > 0 "
        "AND (valid_until IS NULL OR valid_until >= ?)"
    )
    positive_templates = _report_rows(
        cursor,
        f'''
        SELECT *
        FROM signal_template_performance
        WHERE {template_where}
          AND net_pnl > 0
          AND win_rate >= 0.55
        ORDER BY net_pnl DESC, win_rate DESC, confidence_score DESC, sample_count DESC
        LIMIT 10
        ''',
        (config_id, trading_date),
    )
    weak_templates = _report_rows(
        cursor,
        f'''
        SELECT *
        FROM signal_template_performance
        WHERE {template_where}
          AND (net_pnl < 0 OR win_rate <= 0.45)
        ORDER BY net_pnl ASC, win_rate ASC, confidence_score DESC, sample_count DESC
        LIMIT 10
        ''',
        (config_id, trading_date),
    )
    analyst_digests = _report_rows(
        cursor,
        '''
        SELECT *
        FROM analyst_learning_digest
        WHERE config_id = ?
          AND (valid_until IS NULL OR valid_until >= ?)
        ORDER BY created_at DESC
        LIMIT 20
        ''',
        (config_id, trading_date),
    )
    overlays = _report_rows(
        cursor,
        '''
        SELECT *
        FROM config_learning_overlay
        WHERE config_id = ?
          AND active = 1
          AND (valid_until IS NULL OR valid_until >= ?)
        ORDER BY confidence_score DESC, created_at DESC
        ''',
        (config_id, trading_date),
    )
    adaptive_policies = _report_rows(
        cursor,
        '''
        SELECT *
        FROM adaptive_policy_state
        WHERE config_id = ?
          AND active = 1
          AND (valid_until IS NULL OR valid_until >= ?)
        ORDER BY confidence_score DESC, sample_count DESC, created_at DESC
        LIMIT 20
        ''',
        (config_id, trading_date),
    )
    capital_rows = _report_rows(
        cursor,
        '''
        SELECT *
        FROM capital_deployment_state
        WHERE config_id = ?
          AND trading_date = ?
        LIMIT 1
        ''',
        (config_id, trading_date),
    )
    events = _report_rows(
        cursor,
        '''
        SELECT *
        FROM learning_event_log
        WHERE config_id = ?
          AND trading_date = ?
        ORDER BY created_at DESC
        LIMIT 50
        ''',
        (config_id, trading_date),
    )
    learned_vs_unlearned = _learned_vs_unlearned_trade_performance(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
    )
    causal_rule_validation = _causal_rule_validation_summary(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
    )
    cursor.execute("PRAGMA table_info(futures_recommendation)")
    recommendation_columns = {str(row["name"]) for row in cursor.fetchall()}
    snapshot_artifact_cols = (
        ", signal_snapshot_artifact_path, signal_snapshot_sha256"
        if {"signal_snapshot_artifact_path", "signal_snapshot_sha256"}.issubset(recommendation_columns)
        else ""
    )
    neutral_rows = _report_rows(
        cursor,
        f'''
        SELECT id, underlying_code, signal_snapshot{snapshot_artifact_cols}
        FROM futures_recommendation
        WHERE config_id = ?
          AND substr(trading_date, 1, 10) = ?
          AND source_type = ?
        ORDER BY underlying_code, created_at
        ''',
        (config_id, trading_date, RecommendationSourceType.STRATEGY.value),
    )
    neutral_recommendations = []
    for row in neutral_rows:
        item = dict(row)
        item["signal_snapshot"] = _recommendation_snapshot(item)
        neutral_recommendations.append(item)
    neutral_accountability = build_neutral_accountability_summary(neutral_recommendations, cfg)

    payload = {
        "run_id": run_key,
        "exp_name": cfg.get("exp_name"),
        "config_id": config_id,
        "trading_date": trading_date,
        "learning_summary": learning_summary,
        "positive_templates": positive_templates,
        "weak_templates": weak_templates,
        "adaptive_policies": adaptive_policies,
        "config_overlays": overlays,
        "capital_deployment_state": capital_rows[0] if capital_rows else None,
        "analyst_learning_digests": analyst_digests,
        "causal_rule_validation": causal_rule_validation,
        "learned_vs_unlearned_performance": learned_vs_unlearned,
        "neutral_accountability": neutral_accountability,
        "learning_events": events,
        "written_at": _utc_now(),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        f"# Reviewer Learning Report - {trading_date}",
        "",
        f"- run_id: {run_key}",
        f"- exp_name: {cfg.get('exp_name')}",
        f"- config_id: {config_id}",
        "",
        "## Learning Summary",
    ]
    for key, value in learning_summary.items():
        if key == "capital_deployment_state":
            continue
        lines.append(f"- {key}: {value}")

    capital_state = payload["capital_deployment_state"] or {}
    lines.extend(
        [
            "",
            "## Capital Deployment",
            f"- current_margin_ratio: {_percent(capital_state.get('current_margin_ratio'))}",
            f"- target_margin_ratio_min: {_percent(capital_state.get('target_margin_ratio_min'))}",
            f"- target_margin_ratio_max: {_percent(capital_state.get('target_margin_ratio_max'))}",
            f"- reason_bucket: {capital_state.get('reason_bucket', 'unknown')}",
        ]
    )
    deployment_plan = _json_loads(capital_state.get("deployment_plan_json")) or {}
    diagnostics = deployment_plan.get("diagnostics") if isinstance(deployment_plan, dict) else {}
    if isinstance(diagnostics, dict) and diagnostics:
        lines.extend(
            [
                f"- primary_category: {diagnostics.get('primary_category', 'unknown')}",
                f"- reason_counts: {diagnostics.get('reason_counts', {})}",
                f"- category_counts: {diagnostics.get('category_counts', {})}",
                f"- alpha_release_candidate_count: {diagnostics.get('alpha_release_candidate_count', 0)}",
            ]
        )
        parameter_review = diagnostics.get("parameter_review") or []
        if parameter_review:
            lines.append("- parameter_review:")
            lines.extend(
                f"  - {item.get('scope')}: {item.get('reason')} | {item.get('guardrail')}"
                for item in parameter_review[:8]
                if isinstance(item, dict)
            )
        candidates = diagnostics.get("alpha_release_candidates") or []
        if candidates:
            lines.append("- alpha_release_candidates:")
            lines.extend(
                (
                    f"  - {item.get('ticker')}/{item.get('side')}: "
                    f"confirm={_percent(item.get('confirmation_score'))}, "
                    f"target={_percent(item.get('target_position_ratio'))}, "
                    f"memory={item.get('memory_state') or 'none'}, "
                    f"auditor={item.get('auditor_decision') or 'none'}"
                )
                for item in candidates[:8]
                if isinstance(item, dict)
            )
    lines.extend(["", "## Causal Rule Validation"])
    lines.extend(
        [
            f"- active_causal_rule_count: {causal_rule_validation.get('active_causal_rule_count', 0)}",
            f"- candidate_status_counts: {causal_rule_validation.get('candidate_status_counts', {})}",
        ]
    )
    lines.extend(["", "## Learned vs Unlearned Trade Performance"])
    lines.append(_trade_performance_report_line("learned", learned_vs_unlearned.get("learned") or {}))
    lines.append(_trade_performance_report_line("unlearned", learned_vs_unlearned.get("unlearned") or {}))
    lines.append(f"- learned_reason_counts: {learned_vs_unlearned.get('learned_reason_counts', {})}")
    lines.append(f"- learned_effect_counts: {learned_vs_unlearned.get('learned_effect_counts', {})}")
    effect_summary = learned_vs_unlearned.get("learned_effect_summary") or {}
    if isinstance(effect_summary, dict):
        for effect, payload in effect_summary.items():
            lines.append(_trade_performance_report_line(f"effect:{effect}", payload))
    lines.append(f"- sample_status: {learned_vs_unlearned.get('status', 'unknown')}")
    lines.extend(["", "## Neutral Accountability"])
    lines.extend(
        [
            f"- neutral_ratio: {_percent(neutral_accountability.get('neutral_ratio'))}",
            f"- accountability_complete_rate: {_percent(neutral_accountability.get('accountability_complete_rate'))}",
            f"- category_counts: {neutral_accountability.get('category_counts', {})}",
            f"- missing_field_counts: {neutral_accountability.get('missing_field_counts', {})}",
        ]
    )
    examples = neutral_accountability.get("examples") or []
    if examples:
        lines.append("- review_examples:")
        lines.extend(
            (
                f"  - {item.get('ticker')}/{item.get('analyst')}: {item.get('category')} | "
                f"{item.get('rationale')} | change_if={item.get('would_change_view_if') or 'unknown'}"
            )
            for item in examples[:8]
            if isinstance(item, dict)
        )
    lines.extend(["", "## Positive Templates"])
    lines.extend([_template_report_line(row) for row in positive_templates] or ["- none"])
    lines.extend(["", "## Failed Templates"])
    lines.extend([_template_report_line(row) for row in weak_templates] or ["- none"])
    lines.extend(["", "## Adaptive Policies"])
    lines.extend(
        [
            (
                f"- {row.get('ticker')}/{row.get('side')}/{row.get('horizon_class')}: "
                f"{row.get('policy_action')} multiplier={row.get('multiplier')} "
                f"confidence={_percent(row.get('confidence_score'))} reason={row.get('reason')}"
            )
            for row in adaptive_policies
        ]
        or ["- none"]
    )
    lines.extend(["", "## Config Overlays"])
    lines.extend(
        [
            (
                f"- {row.get('param_key')}: learned={row.get('learned_value_json')} "
                f"rollback={row.get('rollback_value_json')} valid_until={row.get('valid_until')}"
            )
            for row in overlays
        ]
        or ["- none"]
    )
    lines.extend(["", "## Analyst Digests"])
    lines.extend(
        [
            f"- {row.get('analyst')}/{row.get('ticker')}/{row.get('horizon_class')}: {row.get('digest_text')}"
            for row in analyst_digests[:10]
        ]
        or ["- none"]
    )
    lines.extend(["", "## Learning Events"])
    lines.extend(
        [
            f"- {row.get('event_type')} | {row.get('scope_type')}={row.get('scope_key')} | status={row.get('status')}"
            for row in events[:20]
        ]
        or ["- none"]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"markdown": str(md_path), "json": str(json_path)}


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


def _target_side_from_ratio(value: Any) -> str:
    ratio = _safe_float(value)
    if ratio > 0:
        return "long"
    if ratio < 0:
        return "short"
    return "flat"


def _signal_side(signal: Any) -> str:
    return SIDE_BY_SIGNAL.get(str(signal), "neutral")


def _signal_combo_from_snapshot(snapshot: Dict[str, Any]) -> List[str]:
    plan = snapshot.get("pre_open_plan") if isinstance(snapshot.get("pre_open_plan"), dict) else {}
    combo = plan.get("analyst_signal_combo") if isinstance(plan, dict) else None
    if isinstance(combo, list):
        values = [str(item) for item in combo[:3]]
    else:
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
    if analyst == "commodity_news":
        keys.append("company_news")
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


def _pre_open_plan(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    plan = snapshot.get("pre_open_plan")
    return plan if isinstance(plan, dict) else {}


def _market_confirmation(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    plan = _pre_open_plan(snapshot)
    confirmation = plan.get("market_confirmation") or snapshot.get("market_confirmation")
    return confirmation if isinstance(confirmation, dict) else {}


def _market_regime(snapshot: Dict[str, Any]) -> str:
    explicit = _first_analyst_field(snapshot, "market_regime")
    if explicit:
        return str(explicit)
    technical = snapshot.get("technical")
    if isinstance(technical, dict):
        context = ((technical.get("metadata") or {}).get("technical_context") or {})
        if isinstance(context, dict) and context.get("market_regime"):
            return str(context.get("market_regime"))
    plan = _pre_open_plan(snapshot)
    return str(plan.get("market_regime") or "unknown")


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
    plan = _pre_open_plan(snapshot)
    for field_name in names:
        value = plan.get(field_name)
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
    plan = _pre_open_plan(snapshot)
    explicit = _safe_int(plan.get("expected_horizon_days"), 0)
    if explicit > 0:
        return explicit
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
        plan = _pre_open_plan(snapshot)
        explicit = plan.get("decision_horizon") or plan.get("horizon_class")
        if explicit:
            return str(explicit)
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


def _trigger_type(snapshot: Dict[str, Any], side: str) -> str:
    explicit = _first_analyst_field(snapshot, "trigger_type")
    if explicit:
        return str(explicit)
    plan = _pre_open_plan(snapshot)
    trigger = plan.get("trigger_type")
    if trigger:
        return str(trigger)
    confirmation = _market_confirmation(snapshot)
    score = _safe_float(confirmation.get("confirmation_score"), 0.0)
    if score >= 0.70:
        return "confirmed_momentum"
    if side != "flat" and _price_stage(snapshot) in {"oversold", "overbought"}:
        return "reversal_probe"
    return "standard_signal"


def _entry_type(recommendation: Dict[str, Any], snapshot: Dict[str, Any]) -> str:
    explicit = _first_analyst_field(snapshot, "entry_type")
    if explicit:
        return str(explicit)
    plan = _pre_open_plan(snapshot)
    if plan.get("entry_type"):
        return str(plan.get("entry_type"))
    action = str(recommendation.get("action") or "").lower()
    if "hold" in action:
        return "hold"
    if "close" in action or "reduce" in action:
        return "reduce_or_exit"
    return "new_or_adjust"


def _recommendation_side(recommendation: Dict[str, Any], snapshot: Dict[str, Any]) -> str:
    plan = _pre_open_plan(snapshot)
    for key in ("target_side", "raw_target_side", "direction"):
        if plan.get(key):
            value = str(plan.get(key)).lower()
            if value in {"long", "short", "flat"}:
                return value
    return _target_side_from_ratio(plan.get("target_position_ratio") or recommendation.get("target_position_ratio"))


def _signal_template(side: str, combo: Iterable[str], snapshot: Dict[str, Any]) -> str:
    for analyst, payload in _analyst_payloads(snapshot).items():
        if _signal_side(payload.get("signal")) == side:
            template_name = str(payload.get("template_name") or "").strip()
            if template_name and template_name != "unknown":
                horizon = str(payload.get("analyst_horizon") or payload.get("horizon_class") or "unknown")
                return f"{side}_{template_name}_{horizon}"[:160]
    trigger = _trigger_type(snapshot, side)
    regime = _market_regime(snapshot).lower().replace(" ", "_")
    normalized_combo = "_".join(str(item).lower() for item in combo)
    return f"{side}_{trigger}_{regime}_{normalized_combo}"[:160]


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
    if "commodity_news" not in payloads and isinstance(snapshot.get("company_news"), dict):
        payloads["commodity_news"] = snapshot["company_news"]
    return payloads


def _sector_for_ticker(cfg: Dict[str, Any], ticker: str) -> str:
    sector_map = (
        (cfg.get("sector_config") or {}).get("ticker_sector_map")
        or (cfg.get("futures_sector_config") or {}).get("ticker_sector_map")
        or {}
    )
    return str(sector_map.get(ticker.upper()) or SECTOR_BY_TICKER.get(ticker.upper()) or "*")


def _valid_until(trading_date: str, days: int) -> str:
    return (datetime.strptime(trading_date, "%Y-%m-%d") + timedelta(days=max(1, days))).strftime("%Y-%m-%d")


def _confidence_from_summary(summary: Dict[str, Any]) -> float:
    sample_count = _safe_int(summary.get("total_trades") or summary.get("sample_count"), 0)
    win_rate = _safe_float(summary.get("win_rate") or summary.get("hit_rate"), 0.0)
    pnl = abs(_safe_float(summary.get("total_pnl") or summary.get("net_pnl"), 0.0))
    return min(1.0, min(0.45, sample_count / 10.0) + min(0.30, abs(win_rate - 0.50)) + min(0.25, pnl / 20000.0))


def _profit_factor(pairs: List[Dict[str, Any]]) -> float:
    wins = sum(max(0.0, _safe_float(item.get("net_pnl"))) for item in pairs)
    losses = abs(sum(min(0.0, _safe_float(item.get("net_pnl"))) for item in pairs))
    if losses <= 1e-9:
        return wins if wins > 0 else 0.0
    return wins / losses


def _insert_learning_event(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    event_type: str,
    scope_type: str,
    scope_key: str,
    evidence: Dict[str, Any],
    action: Dict[str, Any],
    status: str = "applied",
) -> str:
    event_id = str(uuid.uuid4())
    cursor.execute(
        '''
        INSERT INTO learning_event_log (
            id, config_id, trading_date, event_type, agent, scope_type, scope_key,
            evidence_json, action_json, verifier, created_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            event_id,
            config_id,
            trading_date,
            event_type,
            "reviewer",
            scope_type,
            scope_key,
            _json_dumps(evidence),
            _json_dumps(action),
            "deterministic_reviewer",
            _utc_now(),
            status,
        ),
    )
    return event_id


def _write_signal_context_history(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    recommendations: List[Dict[str, Any]],
) -> int:
    inserted = 0
    now = _utc_now()
    for recommendation in recommendations:
        snapshot = _recommendation_snapshot(recommendation)
        side = _recommendation_side(recommendation, snapshot)
        combo = _signal_combo_from_snapshot(snapshot)
        expected_days = _expected_horizon_days(snapshot, side)
        template = _signal_template(side, combo, snapshot)
        row_id = str(uuid.uuid4())
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "").upper()
        analyst_ext = externalize_json_for_db(
            _analyst_payloads(snapshot),
            category="signal_context",
            record_id=row_id,
            field_name="analyst_signals",
            config_id=config_id,
            trading_date=trading_date,
        )
        market_ext = externalize_json_for_db(
            _market_confirmation(snapshot),
            category="signal_context",
            record_id=row_id,
            field_name="market_confirmation",
            config_id=config_id,
            trading_date=trading_date,
        )
        plan_ext = externalize_json_for_db(
            _pre_open_plan(snapshot),
            category="signal_context",
            record_id=row_id,
            field_name="pre_open_plan",
            config_id=config_id,
            trading_date=trading_date,
        )
        cursor.execute(
            '''
            INSERT INTO signal_context_history (
                id, config_id, trading_date, recommendation_id, ticker, side,
                signal_combo, signal_template, horizon_class, expected_horizon_days,
                market_regime, price_stage, price_percentile, trigger_type, entry_type,
                invalidation_level, target_return,
                analyst_signals_json, market_confirmation_json, pre_open_plan_json,
                analyst_signals_artifact_path, analyst_signals_sha256,
                analyst_signals_size, analyst_signals_summary_json,
                market_confirmation_artifact_path, market_confirmation_sha256,
                market_confirmation_size, market_confirmation_summary_json,
                pre_open_plan_artifact_path, pre_open_plan_sha256,
                pre_open_plan_size, pre_open_plan_summary_json,
                outcome_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                row_id,
                config_id,
                trading_date,
                recommendation.get("id"),
                ticker,
                side,
                _json_dumps(combo),
                template,
                _horizon_class(expected_days, snapshot),
                expected_days,
                _market_regime(snapshot),
                _price_stage(snapshot),
                _price_percentile(snapshot),
                _trigger_type(snapshot, side),
                _entry_type(recommendation, snapshot),
                _invalidation_level(snapshot),
                _target_return(snapshot),
                analyst_ext.inline_value,
                market_ext.inline_value,
                plan_ext.inline_value,
                analyst_ext.artifact_path,
                analyst_ext.sha256,
                analyst_ext.size_bytes,
                analyst_ext.summary_json,
                market_ext.artifact_path,
                market_ext.sha256,
                market_ext.size_bytes,
                market_ext.summary_json,
                plan_ext.artifact_path,
                plan_ext.sha256,
                plan_ext.size_bytes,
                plan_ext.summary_json,
                "pending",
                now,
            ),
        )
        inserted += 1
    _insert_learning_event(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        event_type="signal_context_snapshot",
        scope_type="daily",
        scope_key=trading_date,
        evidence={"recommendations": len(recommendations)},
        action={"inserted": inserted},
    )
    return inserted


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


def _write_template_and_analyst_learning(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Dict[str, int]:
    learning_cfg = cfg.get("learning", {}) or {}
    min_samples = int((learning_cfg.get("anti_overfit") or {}).get("min_samples_for_template", 2))
    expires_after_days = int(learning_cfg.get("memory_expires_after_days", 30))
    pairs = _completed_pairs_up_to(cursor, config_id=config_id, trading_date=trading_date)
    try:
        recommendation_lookup = _recommendations_by_id(
            cursor,
            [pair.get("open_recommendation_id") for pair in pairs if pair.get("open_recommendation_id")],
        )
    except sqlite3.Error:
        recommendation_lookup = {}

    template_groups: Dict[Tuple[str, str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    analyst_groups: Dict[Tuple[str, str, str, str, str], List[float]] = defaultdict(list)

    for pair in pairs:
        recommendation = recommendation_lookup.get(str(pair.get("open_recommendation_id") or ""))
        snapshot = _recommendation_snapshot(recommendation or {})
        ticker = str(pair.get("ticker") or "").upper()
        side = str(pair.get("side") or "").lower()
        combo = _signal_combo_from_snapshot(snapshot)
        expected_days = _expected_horizon_days(snapshot, side)
        horizon = _horizon_class(expected_days, snapshot)
        regime = _market_regime(snapshot)
        template = _signal_template(side, combo, snapshot)
        item = dict(pair)
        item["signal_template"] = template
        item["signal_combo"] = combo
        template_groups[(ticker, side, template, horizon, regime)].append(item)

        sector = _sector_for_ticker(cfg, ticker)
        for analyst, payload in _analyst_payloads(snapshot).items():
            signal_side = _signal_side(payload.get("signal"))
            if signal_side == "neutral":
                continue
            analyst_horizon = _analyst_horizon_class(snapshot, analyst, expected_days)
            pnl = _safe_float(pair.get("net_pnl"))
            attributed = pnl if signal_side == side else -pnl
            analyst_groups[(analyst, ticker, sector, analyst_horizon, signal_side)].append(attributed)

    now = _utc_now()
    valid_until = _valid_until(trading_date, expires_after_days)
    template_rows = 0
    for (ticker, side, template, horizon, regime), rows in template_groups.items():
        if len(rows) < min_samples:
            continue
        summary = summarize_trade_pairs(rows)
        confidence = _confidence_from_summary(summary)
        cursor.execute(
            '''
            INSERT INTO signal_template_performance (
                id, config_id, ticker, side, signal_template, horizon_class, market_regime,
                sample_count, win_rate, net_pnl, avg_pnl, profit_factor,
                confidence_score, last_updated, valid_until, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_id, ticker, side, signal_template, horizon_class, market_regime)
            DO UPDATE SET
                sample_count=excluded.sample_count,
                win_rate=excluded.win_rate,
                net_pnl=excluded.net_pnl,
                avg_pnl=excluded.avg_pnl,
                profit_factor=excluded.profit_factor,
                confidence_score=excluded.confidence_score,
                last_updated=excluded.last_updated,
                valid_until=excluded.valid_until,
                payload_json=excluded.payload_json
            ''',
            (
                str(uuid.uuid4()),
                config_id,
                ticker,
                side,
                template,
                horizon,
                regime,
                int(summary.get("total_trades") or 0),
                _safe_float(summary.get("win_rate")),
                _safe_float(summary.get("total_pnl")),
                _safe_float(summary.get("avg_pnl")),
                _profit_factor(rows),
                confidence,
                now,
                valid_until,
                _json_dumps({"summary": summary, "cutoff_trading_date": trading_date}),
            ),
        )
        template_rows += 1

    analyst_rows = 0
    digest_rows = 0
    for (analyst, ticker, sector, horizon, signal_side), attributed_pnls in analyst_groups.items():
        if len(attributed_pnls) < min_samples:
            continue
        wins = sum(1 for pnl in attributed_pnls if pnl > 0)
        sample_count = len(attributed_pnls)
        net_pnl = sum(attributed_pnls)
        hit_rate = wins / sample_count if sample_count else 0.0
        avg_pnl = net_pnl / sample_count if sample_count else 0.0
        summary = {
            "sample_count": sample_count,
            "hit_rate": hit_rate,
            "net_pnl": net_pnl,
            "avg_pnl": avg_pnl,
        }
        confidence = _confidence_from_summary(summary)
        cursor.execute(
            '''
            INSERT INTO analyst_performance (
                id, config_id, analyst, ticker, sector, horizon_class, signal_side,
                sample_count, hit_rate, avg_pnl, net_pnl, confidence_score,
                last_updated, valid_until, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_id, analyst, ticker, sector, horizon_class, signal_side)
            DO UPDATE SET
                sample_count=excluded.sample_count,
                hit_rate=excluded.hit_rate,
                avg_pnl=excluded.avg_pnl,
                net_pnl=excluded.net_pnl,
                confidence_score=excluded.confidence_score,
                last_updated=excluded.last_updated,
                valid_until=excluded.valid_until,
                payload_json=excluded.payload_json
            ''',
            (
                str(uuid.uuid4()),
                config_id,
                analyst,
                ticker,
                sector,
                horizon,
                signal_side,
                sample_count,
                hit_rate,
                avg_pnl,
                net_pnl,
                confidence,
                now,
                valid_until,
                _json_dumps(summary),
            ),
        )
        analyst_rows += 1
        if confidence >= 0.25:
            if net_pnl >= 0:
                digest = (
                    f"{ticker} {horizon} {signal_side}: recent mature samples support this analyst "
                    f"(hit_rate={hit_rate:.0%}, avg_pnl={avg_pnl:.0f}). Prefer matching this horizon and "
                    "state price stage and invalidation clearly."
                )
            else:
                digest = (
                    f"{ticker} {horizon} {signal_side}: recent mature samples are weak "
                    f"(hit_rate={hit_rate:.0%}, avg_pnl={avg_pnl:.0f}). Treat as a lower-confidence prior "
                    "unless today's evidence is stronger and market confirmation agrees."
                )
            event_id = _insert_learning_event(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                event_type="analyst_learning_digest",
                scope_type="analyst_ticker_horizon",
                scope_key=f"{analyst}:{ticker}:{horizon}:{signal_side}",
                evidence=summary,
                action={"digest": digest},
            )
            cursor.execute(
                '''
                INSERT INTO analyst_learning_digest (
                    id, config_id, analyst, ticker, sector, horizon_class, market_regime,
                    digest_text, confidence_score, sample_count, source_event_id,
                    created_at, valid_until, accepted, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    str(uuid.uuid4()),
                    config_id,
                    analyst,
                    ticker,
                    sector,
                    horizon,
                    "*",
                    digest,
                    confidence,
                    sample_count,
                    event_id,
                    now,
                    valid_until,
                    1,
                    _json_dumps(summary),
                ),
            )
            digest_rows += 1

    _insert_learning_event(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        event_type="performance_attribution",
        scope_type="daily",
        scope_key=trading_date,
        evidence={"completed_pairs": len(pairs), "min_samples": min_samples},
        action={"template_rows": template_rows, "analyst_rows": analyst_rows, "digest_rows": digest_rows},
    )
    return {"template_rows": template_rows, "analyst_rows": analyst_rows, "digest_rows": digest_rows}


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
        template = _signal_template(side, combo, snapshot)
        item = dict(pair)
        item["signal_template"] = template
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


def _causal_candidate_scope(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    tickers = {
        str(candidate.get("ticker") or "").upper()
        for _ in [0]
        if str(candidate.get("ticker") or "*") not in {"", "*"}
    }
    sides = {
        str(candidate.get("side") or "").lower()
        for _ in [0]
        if str(candidate.get("side") or "*") not in {"", "*"}
    }
    try:
        cursor.execute(
            """
            SELECT payload_json, payload_artifact_path, payload_sha256
            FROM reviewer_llm_notes
            WHERE config_id = ?
              AND evidence_pack_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (config_id, candidate.get("evidence_pack_id")),
        )
        row = cursor.fetchone()
    except sqlite3.Error:
        row = None
    evidence = (
        load_externalized_json(
            row["payload_json"],
            row["payload_artifact_path"] if "payload_artifact_path" in row.keys() else None,
            row["payload_sha256"] if "payload_sha256" in row.keys() else None,
        )
        if row
        else {}
    )
    for item in (evidence or {}).get("pre_trade_evidence") or []:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").upper()
        if ticker:
            tickers.add(ticker)
        action = str(item.get("action") or "").lower()
        if "long" in action:
            sides.add("long")
        elif "short" in action:
            sides.add("short")
        else:
            snapshot = item.get("signal_snapshot") if isinstance(item.get("signal_snapshot"), dict) else {}
            plan = _pre_open_plan(snapshot)
            side = str(plan.get("target_side") or plan.get("raw_target_side") or "").lower()
            if side in {"long", "short"}:
                sides.add(side)
            else:
                side = _target_side_from_ratio(plan.get("target_position_ratio"))
                if side in {"long", "short"}:
                    sides.add(side)
    return {
        "tickers": sorted(tickers),
        "sides": sorted(sides),
        "evidence_pack_id": candidate.get("evidence_pack_id"),
    }


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


def _write_validated_causal_policy_rules(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    """Promote notes-only causal candidates only after deterministic rule validation."""
    learning_cfg = cfg.get("learning", {}) or {}
    review_cfg = (learning_cfg.get("reviewer_causal_review") or {})
    if not bool(review_cfg.get("enabled", False)):
        return {"validated_rules": 0, "status_counts": {}}

    anti_overfit = learning_cfg.get("anti_overfit") or {}
    rule_cfg = dict(review_cfg.get("rule_validation") or {})
    rule_cfg.setdefault("min_samples", int(anti_overfit.get("min_samples_for_policy", 3) or 3))
    rule_cfg.setdefault("min_candidate_confidence", 0.35)
    rule_cfg.setdefault("protect_min_win_rate", 0.60)
    rule_cfg.setdefault("protect_min_net_pnl", 0.0)
    rule_cfg.setdefault("cap_max_win_rate", 0.40)
    rule_cfg.setdefault("cap_max_net_pnl", 0.0)
    rule_cfg.setdefault("cap_multiplier", 0.50)

    cursor.execute(
        """
        SELECT *
        FROM causal_review_candidate
        WHERE config_id = ?
          AND rule_validation_status IN (
              'notes_only_pending_rule_validation',
              'insufficient_evidence_pending_rule_validation'
          )
          AND substr(trading_date, 1, 10) <= ?
          AND (valid_until IS NULL OR valid_until >= ?)
        ORDER BY substr(trading_date, 1, 10), created_at
        """,
        (config_id, trading_date, trading_date),
    )
    candidates = [dict(row) for row in cursor.fetchall()]
    if not candidates:
        return {"validated_rules": 0, "status_counts": {}}

    template_groups = _template_groups_from_completed_pairs(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
    )
    expires_after_days = int(
        review_cfg.get("validated_rule_expires_after_days")
        or learning_cfg.get("memory_expires_after_days")
        or 30
    )
    valid_until = _valid_until(trading_date, expires_after_days)
    now = _utc_now()
    inserted = 0
    status_counts: Counter = Counter()

    for candidate in candidates:
        candidate_payload = _json_loads(candidate.get("payload_json")) or {}
        if not isinstance(candidate_payload, dict):
            candidate_payload = {}
        candidate_payload.setdefault("confidence_score", _safe_float(candidate.get("confidence_score"), 0.0))
        scope = _causal_candidate_scope(cursor, config_id=config_id, candidate=candidate)
        candidate_rules: List[Dict[str, Any]] = []
        candidate_rejections: List[Dict[str, Any]] = []
        candidate_insufficient = False
        evaluated = 0

        for (ticker, side, template, horizon, regime), rows in template_groups.items():
            if scope["tickers"] and ticker not in scope["tickers"]:
                continue
            if scope["sides"] and side not in scope["sides"]:
                continue
            evaluated += 1
            summary = summarize_trade_pairs(rows)
            decision = _causal_rule_validation_decision(
                candidate_payload=candidate_payload,
                summary=summary,
                rule_cfg=rule_cfg,
            )
            evidence = {
                "candidate_id": candidate.get("id"),
                "candidate_trading_date": candidate.get("trading_date"),
                "candidate_payload": candidate_payload,
                "template_key": {
                    "ticker": ticker,
                    "side": side,
                    "signal_template": template,
                    "horizon_class": horizon,
                    "market_regime": regime,
                },
                "summary": summary,
                "scope": scope,
                "rule_validation_config": rule_cfg,
            }
            if decision["status"] == "insufficient_evidence_pending_rule_validation":
                candidate_insufficient = True
                candidate_rejections.append({**decision, "template": template, "ticker": ticker, "side": side})
                continue
            if decision["status"] != "validated_rule_applied":
                candidate_rejections.append({**decision, "template": template, "ticker": ticker, "side": side})
                continue

            performance_confidence = _confidence_from_summary(summary)
            confidence = min(
                1.0,
                0.55 * _safe_float(candidate_payload.get("confidence_score"), 0.0)
                + 0.45 * performance_confidence,
            )
            action = {
                "policy_action": decision["policy_action"],
                "multiplier": decision["multiplier"],
                "reason": decision["reason"],
                "confidence_score": confidence,
            }
            event_id = _insert_learning_event(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                event_type="causal_rule_validation",
                scope_type="template",
                scope_key=f"{ticker}:{side}:{template}",
                evidence=evidence,
                action=action,
                status="applied",
            )
            payload = {
                "source": "causal_review_candidate",
                "candidate_id": candidate.get("id"),
                "candidate_trading_date": candidate.get("trading_date"),
                "candidate_payload": candidate_payload,
                "validation": {
                    "validated_at_trading_date": trading_date,
                    "summary": summary,
                    "performance_confidence": performance_confidence,
                    "scope": scope,
                },
                "rollback_value": {"policy_action": "inactive"},
            }
            cursor.execute(
                """
                INSERT INTO adaptive_policy_state (
                    id, config_id, ticker, side, signal_template, horizon_class, market_regime,
                    policy_type, policy_action, multiplier, confidence_score, sample_count,
                    reason, source_event_id, created_at, valid_until, payload_json, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(config_id, ticker, side, signal_template, horizon_class, market_regime, policy_type)
                DO UPDATE SET
                    policy_action=excluded.policy_action,
                    multiplier=excluded.multiplier,
                    confidence_score=excluded.confidence_score,
                    sample_count=excluded.sample_count,
                    reason=excluded.reason,
                    source_event_id=excluded.source_event_id,
                    created_at=excluded.created_at,
                    valid_until=excluded.valid_until,
                    payload_json=excluded.payload_json,
                    active=1
                """,
                (
                    str(uuid.uuid4()),
                    config_id,
                    ticker,
                    side,
                    template,
                    horizon,
                    regime,
                    "causal_review_rule",
                    decision["policy_action"],
                    decision["multiplier"],
                    confidence,
                    _safe_int(summary.get("total_trades")),
                    decision["reason"],
                    event_id,
                    now,
                    valid_until,
                    _json_dumps(payload),
                ),
            )
            inserted += 1
            candidate_rules.append(
                {
                    "ticker": ticker,
                    "side": side,
                    "signal_template": template,
                    "horizon_class": horizon,
                    "market_regime": regime,
                    **action,
                    "sample_count": _safe_int(summary.get("total_trades")),
                    "win_rate": _safe_float(summary.get("win_rate")),
                    "net_pnl": _safe_float(summary.get("total_pnl")),
                }
            )

        if candidate_rules:
            candidate_status = "validated_rule_applied"
        elif candidate_insufficient or evaluated == 0:
            candidate_status = "insufficient_evidence_pending_rule_validation"
        else:
            candidate_status = "validated_rule_rejected"
        status_counts[candidate_status] += 1
        candidate_payload["rule_validation"] = {
            "validated_at_trading_date": trading_date,
            "status": candidate_status,
            "scope": scope,
            "evaluated_template_count": evaluated,
            "applied_rule_count": len(candidate_rules),
            "rules": candidate_rules[:20],
            "rejections": candidate_rejections[:20],
        }
        cursor.execute(
            """
            UPDATE causal_review_candidate
            SET rule_validation_status = ?,
                payload_json = ?
            WHERE id = ?
            """,
            (candidate_status, _json_dumps(candidate_payload), candidate.get("id")),
        )
        _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="causal_candidate_rule_validation",
            scope_type="candidate",
            scope_key=str(candidate.get("id")),
            evidence={"candidate_id": candidate.get("id"), "scope": scope},
            action={
                "status": candidate_status,
                "evaluated_template_count": evaluated,
                "applied_rule_count": len(candidate_rules),
            },
            status="applied" if candidate_rules else "pending" if candidate_status.startswith("insufficient") else "rejected",
        )

    return {"validated_rules": inserted, "status_counts": dict(status_counts)}


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


def _learning_attribution_from_recommendation(recommendation: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    snapshot = _recommendation_snapshot(recommendation or {})
    plan = _pre_open_plan(snapshot)
    auditor = plan.get("trade_auditor") or plan.get("decision_planner") or {}
    diagnostics = auditor.get("diagnostics") if isinstance(auditor, dict) and isinstance(auditor.get("diagnostics"), dict) else {}
    reasons = _control_reasons_from_plan(plan)
    return (
        learning_tags_from_context(reasons, diagnostics),
        learning_effects_from_context(reasons, diagnostics),
    )


def _learning_tags_from_recommendation(recommendation: Dict[str, Any]) -> List[str]:
    tags, _effects = _learning_attribution_from_recommendation(recommendation)
    return tags


def _learned_vs_unlearned_trade_performance(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    try:
        pairs = _completed_pairs_up_to(cursor, config_id=config_id, trading_date=trading_date)
    except sqlite3.Error:
        pairs = []
    recommendation_lookup = _recommendations_by_id(
        cursor,
        [pair.get("open_recommendation_id") for pair in pairs if pair.get("open_recommendation_id")],
    )
    learned_pairs: List[Dict[str, Any]] = []
    unlearned_pairs: List[Dict[str, Any]] = []
    reason_counts: Counter = Counter()
    missing_recommendations = 0
    for pair in pairs:
        recommendation = recommendation_lookup.get(str(pair.get("open_recommendation_id") or ""))
        if not recommendation:
            missing_recommendations += 1
            unlearned_pairs.append(dict(pair))
            continue
        tags, effects = _learning_attribution_from_recommendation(recommendation)
        item = dict(pair)
        item["learning_tags"] = tags
        item["learning_effects"] = effects
        if tags and effects:
            learned_pairs.append(item)
            reason_counts.update(tags)
        else:
            unlearned_pairs.append(item)
    return {
        "status": "ok" if pairs else "no_completed_round_trips",
        "cutoff_trading_date": trading_date,
        "learned": _trade_pair_performance_summary(learned_pairs),
        "unlearned": _trade_pair_performance_summary(unlearned_pairs),
        "learned_reason_counts": _sorted_counter_dict(reason_counts),
        "learned_effect_counts": learning_effect_counts(learned_pairs),
        "learned_effect_summary": summarize_pairs_by_learning_effect(learned_pairs),
        "missing_open_recommendations": missing_recommendations,
    }


def _causal_rule_validation_summary(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    cursor.execute(
        """
        SELECT rule_validation_status, COUNT(*) AS cnt
        FROM causal_review_candidate
        WHERE config_id = ?
        GROUP BY rule_validation_status
        """,
        (config_id,),
    )
    status_counts = {
        str(row["rule_validation_status"] or "unknown"): int(row["cnt"] or 0)
        for row in cursor.fetchall()
    }
    cursor.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM adaptive_policy_state
        WHERE config_id = ?
          AND policy_type = 'causal_review_rule'
          AND active = 1
          AND (valid_until IS NULL OR valid_until >= ?)
        """,
        (config_id, trading_date),
    )
    row = cursor.fetchone()
    return {
        "candidate_status_counts": status_counts,
        "active_causal_rule_count": int(row["cnt"] or 0) if row else 0,
    }


def _write_strategy_memory_history(
    cursor: sqlite3.Cursor,
    *,
    db: Any,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> int:
    if hasattr(db, "_refresh_strategy_memory_with_cursor"):
        db._refresh_strategy_memory_with_cursor(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            memory_config=cfg.get("strategy_memory", {}),
        )
    elif hasattr(db, "refresh_strategy_memory"):
        db.refresh_strategy_memory(config_id=config_id, trading_date=trading_date, memory_config=cfg.get("strategy_memory", {}))
    cursor.execute("SELECT * FROM strategy_memory WHERE config_id = ?", (config_id,))
    now = _utc_now()
    rows = [dict(row) for row in cursor.fetchall()]
    for row in rows:
        cursor.execute(
            '''
            INSERT INTO strategy_memory_history (
                id, config_id, trading_date, ticker, side, signal_combo, memory_state,
                sample_count, win_rate, net_pnl, avg_pnl, confidence_score,
                source, reason, snapshot_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                str(uuid.uuid4()),
                config_id,
                trading_date,
                row.get("ticker"),
                row.get("side"),
                row.get("signal_combo"),
                row.get("memory_state"),
                _safe_int(row.get("sample_count")),
                _safe_float(row.get("win_rate")),
                _safe_float(row.get("net_pnl")),
                _safe_float(row.get("avg_pnl")),
                _safe_float(row.get("confidence_score")),
                "reviewer_snapshot",
                row.get("reason"),
                now,
                row.get("payload_json"),
            ),
        )
    return len(rows)


def _write_adaptive_policy_state(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    cfg: Dict[str, Any],
) -> int:
    learning_cfg = cfg.get("learning", {}) or {}
    policy_cfg = learning_cfg.get("adaptive_policy", {}) or {}
    if not bool(policy_cfg.get("enabled", True)):
        return 0
    min_samples = int((learning_cfg.get("anti_overfit") or {}).get("min_samples_for_policy", 3))
    expires_after_days = int(learning_cfg.get("memory_expires_after_days", 30))
    cursor.execute(
        '''
        SELECT *
        FROM signal_template_performance
        WHERE config_id = ?
          AND sample_count >= ?
        ''',
        (config_id, min_samples),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    now = _utc_now()
    valid_until = _valid_until(trading_date, expires_after_days)
    count = 0
    for row in rows:
        win_rate = _safe_float(row.get("win_rate"))
        net_pnl = _safe_float(row.get("net_pnl"))
        confidence = _safe_float(row.get("confidence_score"))
        sample_count = _safe_int(row.get("sample_count"))
        if win_rate >= 0.60 and net_pnl > 0:
            action = "protect"
            multiplier = 1.0
            reason = "positive mature template"
        elif sample_count >= max(4, min_samples) and (win_rate <= 0.35 or net_pnl < 0):
            action = "cap"
            multiplier = 0.50
            reason = "weak mature template"
        else:
            continue
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="adaptive_policy_state",
            scope_type="template",
            scope_key=f"{row.get('ticker')}:{row.get('side')}:{row.get('signal_template')}",
            evidence=dict(row),
            action={"policy_action": action, "multiplier": multiplier, "reason": reason},
        )
        cursor.execute(
            '''
            INSERT INTO adaptive_policy_state (
                id, config_id, ticker, side, signal_template, horizon_class, market_regime,
                policy_type, policy_action, multiplier, confidence_score, sample_count,
                reason, source_event_id, created_at, valid_until, payload_json, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(config_id, ticker, side, signal_template, horizon_class, market_regime, policy_type)
            DO UPDATE SET
                policy_action=excluded.policy_action,
                multiplier=excluded.multiplier,
                confidence_score=excluded.confidence_score,
                sample_count=excluded.sample_count,
                reason=excluded.reason,
                source_event_id=excluded.source_event_id,
                created_at=excluded.created_at,
                valid_until=excluded.valid_until,
                payload_json=excluded.payload_json,
                active=1
            ''',
            (
                str(uuid.uuid4()),
                config_id,
                row.get("ticker"),
                row.get("side"),
                row.get("signal_template"),
                row.get("horizon_class"),
                row.get("market_regime"),
                "template_quality",
                action,
                multiplier,
                confidence,
                sample_count,
                reason,
                event_id,
                now,
                valid_until,
                row.get("payload_json"),
            ),
        )
        count += 1
    return count


def _write_config_overlay(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    cfg: Dict[str, Any],
    settlement_row: Optional[Dict[str, Any]],
) -> int:
    learning_cfg = cfg.get("learning", {}) or {}
    overlay_cfg = learning_cfg.get("config_overlay", {}) or {}
    if not bool(overlay_cfg.get("enabled", True)):
        return 0
    capital_cfg = cfg.get("capital_utilization_control", {}) or {}
    target_min = float(capital_cfg.get("target_margin_ratio_min", 0.16))
    target_max = float(capital_cfg.get("target_margin_ratio_max", 0.20))
    values = {
        "capital_utilization_control.target_margin_ratio_min": target_min,
        "capital_utilization_control.target_margin_ratio_max": target_max,
        "capital_utilization_control.target_margin_ratio_confirmed": max(target_min, min(target_max, float(capital_cfg.get("target_margin_ratio_confirmed", target_min)))),
    }
    rollback_values = {
        key: _dotted_config_value(cfg, key, default=value)
        for key, value in values.items()
    }
    now = _utc_now()
    valid_until = _valid_until(trading_date, int(learning_cfg.get("overlay_expires_after_days", 10)))
    if settlement_row:
        evidence = {
            "current_margin_ratio": _safe_float(settlement_row.get("margin_ratio")),
            "current_margin": _safe_float(settlement_row.get("current_margin")),
        }
    else:
        evidence = {}
    event_id = _insert_learning_event(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        event_type="config_overlay_refresh",
        scope_type="global",
        scope_key="capital_utilization_control",
        evidence=evidence,
        action={"learned_values": values, "rollback_values": rollback_values},
    )
    inserted = 0
    for key, value in values.items():
        rollback_value = rollback_values.get(key, value)
        cursor.execute(
            '''
            INSERT INTO config_learning_overlay (
                id, config_id, trading_date, param_key, learned_value_json,
                previous_value_json, scope_type, scope_key, source, confidence_score,
                sample_count, reason, source_event_id, rollback_value_json,
                created_at, valid_until, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(config_id, param_key, scope_type, scope_key, source)
            DO UPDATE SET
                trading_date=excluded.trading_date,
                learned_value_json=excluded.learned_value_json,
                previous_value_json=excluded.previous_value_json,
                confidence_score=excluded.confidence_score,
                sample_count=excluded.sample_count,
                reason=excluded.reason,
                source_event_id=excluded.source_event_id,
                rollback_value_json=excluded.rollback_value_json,
                created_at=excluded.created_at,
                valid_until=excluded.valid_until,
                active=1
            ''',
            (
                str(uuid.uuid4()),
                config_id,
                trading_date,
                key,
                _json_dumps(value),
                _json_dumps(rollback_value),
                "global",
                "*",
                "reviewer",
                0.90,
                1,
                "capital utilization hard target is managed as reviewer overlay",
                event_id,
                _json_dumps(rollback_value),
                now,
                valid_until,
            ),
        )
        inserted += 1
    return inserted


def _write_neutral_accountability_state(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    strategy_recommendations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    recommendations = []
    for recommendation in strategy_recommendations:
        item = dict(recommendation)
        item["signal_snapshot"] = _recommendation_snapshot(recommendation)
        recommendations.append(item)
    summary = build_neutral_accountability_summary(recommendations, cfg)
    _insert_learning_event(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        event_type="neutral_accountability_review",
        scope_type="daily",
        scope_key=trading_date,
        evidence=summary,
        action={
            "neutral_ratio": summary.get("neutral_ratio", 0.0),
            "accountability_complete_rate": summary.get("accountability_complete_rate", 1.0),
            "category_counts": summary.get("category_counts", {}),
            "action_items": summary.get("action_items", []),
        },
        status="applied",
    )
    return summary


def _write_capital_deployment_state(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    settlement_row: Optional[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    no_trade_reason_counter: Counter,
    write_learning_event: bool = True,
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
    cursor.execute(
        '''
        INSERT INTO capital_deployment_state (
            id, config_id, trading_date, capital_base, current_margin, current_margin_ratio,
            target_margin_ratio_min, target_margin_ratio_max, target_margin_abs_min,
            target_margin_abs_max, underutilization_breach, overutilization_breach,
            margin_gap_to_min, capital_allocation_tier, reason_bucket, deployment_plan_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(config_id, trading_date)
        DO UPDATE SET
            capital_base=excluded.capital_base,
            current_margin=excluded.current_margin,
            current_margin_ratio=excluded.current_margin_ratio,
            target_margin_ratio_min=excluded.target_margin_ratio_min,
            target_margin_ratio_max=excluded.target_margin_ratio_max,
            target_margin_abs_min=excluded.target_margin_abs_min,
            target_margin_abs_max=excluded.target_margin_abs_max,
            underutilization_breach=excluded.underutilization_breach,
            overutilization_breach=excluded.overutilization_breach,
            margin_gap_to_min=excluded.margin_gap_to_min,
            capital_allocation_tier=excluded.capital_allocation_tier,
            reason_bucket=excluded.reason_bucket,
            deployment_plan_json=excluded.deployment_plan_json,
            created_at=excluded.created_at
        ''',
        (
            str(uuid.uuid4()),
            config_id,
            trading_date,
            capital_base,
            current_margin,
            current_ratio,
            target_min,
            target_max,
            target_abs_min,
            target_abs_max,
            1 if under else 0,
            1 if over else 0,
            margin_gap_to_min,
            allocation_tier,
            reason_bucket,
            _json_dumps(deployment_plan),
            _utc_now(),
        ),
    )
    if write_learning_event:
        _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="capital_deployment_review",
            scope_type="daily",
            scope_key=trading_date,
            evidence=state,
            action={
                "diagnosis": reason_bucket,
                "primary_category": deployment_diagnostics["primary_category"],
                "alpha_release_candidate_count": deployment_diagnostics["alpha_release_candidate_count"],
                "parameter_review": deployment_diagnostics["parameter_review"],
            },
            status="breach" if under or over else "applied",
        )
    return state


def _write_provisional_policy_state(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    cfg: Dict[str, Any],
) -> int:
    """Create short-lived early risk sentinels from adverse template evidence."""
    learning_cfg = cfg.get("learning", {}) or {}
    provisional_cfg = learning_cfg.get("provisional_policy_state", {}) or {}
    if not bool(provisional_cfg.get("enabled", False)):
        return 0
    valid_days = int(provisional_cfg.get("valid_days", 10) or 10)
    loss_cap = float(provisional_cfg.get("anomaly_loss_threshold", -8000) or -8000)
    consecutive_threshold = int(provisional_cfg.get("consecutive_loss_probe_only_threshold", 2) or 2)
    valid_until = (
        datetime.strptime(str(trading_date)[:10], "%Y-%m-%d") + timedelta(days=max(1, valid_days))
    ).strftime("%Y-%m-%d")
    cursor.execute(
        """
        SELECT ticker, side, signal_template, horizon_class, market_regime,
               sample_count, win_rate, net_pnl, avg_pnl, profit_factor, payload_json
        FROM signal_template_performance
        WHERE config_id = ?
          AND sample_count >= ?
          AND (net_pnl <= ? OR win_rate <= 0.25)
        """,
        (config_id, consecutive_threshold, loss_cap),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    inserted = 0
    for row in rows:
        ticker = str(row.get("ticker") or "*").upper()
        side = str(row.get("side") or "*").lower()
        template = str(row.get("signal_template") or "*")
        horizon = str(row.get("horizon_class") or "*")
        net_pnl = _safe_float(row.get("net_pnl"))
        win_rate = _safe_float(row.get("win_rate"))
        if net_pnl <= loss_cap:
            action = "probe_only"
            multiplier = float(provisional_cfg.get("anomaly_loss_cap_multiplier", 0.25) or 0.25)
            trigger_type = "anomaly_loss"
        elif win_rate <= 0.25:
            action = "probe_only"
            multiplier = float(provisional_cfg.get("consecutive_loss_multiplier", 0.35) or 0.35)
            trigger_type = "consecutive_template_losses"
        else:
            continue
        payload = {
            "trading_date": trading_date,
            "ticker": ticker,
            "side": side,
            "signal_template": template,
            "horizon_class": horizon,
            "net_pnl": net_pnl,
            "win_rate": win_rate,
            "sample_count": int(row.get("sample_count") or 0),
            "rollback_value": {"policy_action": "inactive"},
        }
        cursor.execute(
            """
            INSERT INTO provisional_policy_state (
                id, config_id, ticker, side, signal_template, horizon_class,
                policy_action, multiplier, confidence_score, trigger_type,
                sample_count, reason, rollback_value_json, created_at,
                valid_until, active, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(config_id, ticker, side, signal_template, horizon_class, policy_action)
            DO UPDATE SET
                multiplier=excluded.multiplier,
                confidence_score=excluded.confidence_score,
                trigger_type=excluded.trigger_type,
                sample_count=excluded.sample_count,
                reason=excluded.reason,
                rollback_value_json=excluded.rollback_value_json,
                created_at=excluded.created_at,
                valid_until=excluded.valid_until,
                active=1,
                payload_json=excluded.payload_json
            """,
            (
                str(uuid.uuid4()),
                config_id,
                ticker,
                side,
                template,
                horizon,
                action,
                multiplier,
                min(0.85, max(0.35, abs(net_pnl) / 25000.0 + (1.0 - win_rate) * 0.25)),
                trigger_type,
                int(row.get("sample_count") or 0),
                f"early risk sentinel: {trigger_type}, net_pnl={net_pnl:.0f}, win_rate={win_rate:.2%}",
                _json_dumps(payload["rollback_value"]),
                _utc_now(),
                valid_until,
                _json_dumps(payload),
            ),
        )
        inserted += 1
    if inserted:
        _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="provisional_policy_state",
            scope_type="daily",
            scope_key=trading_date,
            evidence={"candidate_count": len(rows)},
            action={"inserted": inserted},
            status="applied",
        )
    return inserted


def _export_template_prior(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Optional[str]:
    learning_cfg = cfg.get("learning", {}) or {}
    prior_cfg = learning_cfg.get("template_prior", {}) or {}
    if not bool(prior_cfg.get("enabled", False)) or not bool(prior_cfg.get("export_on_backtest_end", True)):
        return None
    path_text = str(prior_cfg.get("path") or "src/logs/attribution/template_prior.json")
    path = Path(path_text)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / path
    path.parent.mkdir(parents=True, exist_ok=True)
    cursor.execute(
        """
        SELECT ticker, side, signal_template, horizon_class, market_regime,
               sample_count, win_rate, net_pnl, avg_pnl, profit_factor,
               confidence_score, valid_until, payload_json
        FROM signal_template_performance
        WHERE config_id = ?
          AND sample_count >= 2
          AND (net_pnl != 0 OR win_rate != 0)
        ORDER BY confidence_score DESC, sample_count DESC
        """,
        (config_id,),
    )
    rows = []
    for row in cursor.fetchall():
        item = dict(row)
        item["payload"] = _json_loads(item.pop("payload_json", None)) or {}
        if _safe_float(item.get("net_pnl")) > 0 and _safe_float(item.get("win_rate")) >= 0.55:
            item["prior_state"] = "protected" if int(item.get("sample_count") or 0) >= 3 else "deployable"
        elif _safe_float(item.get("net_pnl")) < 0 and _safe_float(item.get("win_rate")) <= 0.35:
            item["prior_state"] = "weak_block" if int(item.get("sample_count") or 0) >= 4 else "watchlist"
        else:
            item["prior_state"] = "recovering"
        rows.append(item)
    payload = {
        "config_id": config_id,
        "exported_at_trading_date": trading_date,
        "anti_overfit": learning_cfg.get("anti_overfit", {}),
        "templates": rows,
    }
    path.write_text(_json_dumps(payload), encoding="utf-8")
    return str(path)


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
        },
    }


def _run_reviewer_causal_review(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    settlement_row: Optional[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    no_trade_reason_counter: Counter,
) -> int:
    review_cfg = ((cfg.get("learning", {}) or {}).get("reviewer_causal_review") or {})
    if not bool(review_cfg.get("enabled", False)):
        return 0
    evidence = _build_causal_evidence_pack(
        config_id=config_id,
        trading_date=trading_date,
        strategy_recommendations=strategy_recommendations,
        settlement_row=settlement_row,
        no_trade_reason_counter=no_trade_reason_counter,
    )
    prompt = (
        "You are AgentQuant reviewer doing post-trade causal review. "
        "Use only pre_trade_evidence for ex-ante causes and post_trade_outcome for labels. "
        "Return concise structured lessons, not trading authority.\n"
        + _json_dumps(evidence)[:12000]
    )
    raw_response = ""
    output = CausalReviewLLMOutput()
    if bool(review_cfg.get("use_llm", False)):
        try:
            from llm.inference import agent_call

            output = agent_call(
                prompt=prompt,
                llm_config=cfg.get("llm", {}),
                pydantic_model=CausalReviewLLMOutput,
            )
            raw_response = _json_dumps(output.model_dump())
        except Exception as exc:
            raw_response = f"llm_causal_review_failed: {exc}"
            logger.warning(f"Reviewer LLM causal review failed on {trading_date}: {exc}")
    else:
        raw_response = "llm disabled; deterministic candidate only"

    note_id = str(uuid.uuid4())
    prompt_ext = externalize_text_for_db(
        prompt,
        category="reviewer_llm_notes",
        record_id=note_id,
        field_name="raw_prompt",
        config_id=config_id,
        trading_date=trading_date,
    )
    response_ext = externalize_text_for_db(
        raw_response,
        category="reviewer_llm_notes",
        record_id=note_id,
        field_name="raw_response",
        config_id=config_id,
        trading_date=trading_date,
    )
    payload_ext = externalize_json_for_db(
        evidence,
        category="reviewer_llm_notes",
        record_id=note_id,
        field_name="payload",
        config_id=config_id,
        trading_date=trading_date,
    )
    cursor.execute(
        """
        INSERT INTO reviewer_llm_notes (
            id, config_id, trading_date, evidence_pack_id, ticker,
            raw_prompt, raw_response, created_at, payload_json,
            raw_prompt_artifact_path, raw_prompt_sha256,
            raw_prompt_size, raw_prompt_summary_json,
            raw_response_artifact_path, raw_response_sha256,
            raw_response_size, raw_response_summary_json,
            payload_artifact_path, payload_sha256,
            payload_size, payload_summary_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            note_id,
            config_id,
            trading_date,
            evidence["evidence_pack_id"],
            "*",
            prompt_ext.inline_value,
            response_ext.inline_value,
            _utc_now(),
            payload_ext.inline_value,
            prompt_ext.artifact_path,
            prompt_ext.sha256,
            prompt_ext.size_bytes,
            prompt_ext.summary_json,
            response_ext.artifact_path,
            response_ext.sha256,
            response_ext.size_bytes,
            response_ext.summary_json,
            payload_ext.artifact_path,
            payload_ext.sha256,
            payload_ext.size_bytes,
            payload_ext.summary_json,
        ),
    )
    candidate_payload = output.model_dump() if hasattr(output, "model_dump") else {}
    cursor.execute(
        """
        INSERT INTO causal_review_candidate (
            id, config_id, trading_date, evidence_pack_id, ticker, side,
            candidate_type, confidence_score, rule_validation_status,
            created_at, valid_until, payload_json
        ) VALUES (?, ?, ?, ?, '*', '*', ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            config_id,
            trading_date,
            evidence["evidence_pack_id"],
            "post_trade_causal_review",
            _safe_float(candidate_payload.get("confidence_score"), 0.0),
            "notes_only_pending_rule_validation",
            _utc_now(),
            (
                datetime.strptime(str(trading_date)[:10], "%Y-%m-%d") + timedelta(days=10)
            ).strftime("%Y-%m-%d"),
            _json_dumps(candidate_payload),
        ),
    )
    return 1


def apply_reviewer_learning(
    *,
    db: Any,
    cursor: sqlite3.Cursor,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    settlement_row: Optional[Dict[str, Any]],
    recommendations: List[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    no_trade_reason_counter: Counter,
) -> Dict[str, Any]:
    """Persist deterministic reviewer learning after Phase4 validation passes."""
    cursor.execute("PRAGMA foreign_keys = ON")
    if hasattr(db, "_ensure_reviewer_learning_schema"):
        db._ensure_reviewer_learning_schema(cursor)

    context_rows = _write_signal_context_history(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        recommendations=strategy_recommendations,
    )
    memory_rows = _write_strategy_memory_history(
        cursor,
        db=db,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    perf_counts = _write_template_and_analyst_learning(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    adaptive_rows = _write_adaptive_policy_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    provisional_rows = _write_provisional_policy_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    overlay_rows = _write_config_overlay(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
        settlement_row=settlement_row,
    )
    neutral_accountability = _write_neutral_accountability_state(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        strategy_recommendations=strategy_recommendations,
    )
    capital_state = _write_capital_deployment_state(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        settlement_row=settlement_row,
        strategy_recommendations=strategy_recommendations,
        no_trade_reason_counter=no_trade_reason_counter,
    )
    template_prior_path = _export_template_prior(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    causal_review_candidates = _run_reviewer_causal_review(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        settlement_row=settlement_row,
        strategy_recommendations=strategy_recommendations,
        no_trade_reason_counter=no_trade_reason_counter,
    )
    causal_rule_validation = _write_validated_causal_policy_rules(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    return {
        "signal_context_rows": context_rows,
        "strategy_memory_history_rows": memory_rows,
        **perf_counts,
        "adaptive_policy_rows": adaptive_rows,
        "provisional_policy_rows": provisional_rows,
        "config_overlay_rows": overlay_rows,
        "neutral_accountability": {
            "neutral_ratio": neutral_accountability.get("neutral_ratio", 0.0),
            "accountability_complete_rate": neutral_accountability.get("accountability_complete_rate", 1.0),
            "category_counts": neutral_accountability.get("category_counts", {}),
        },
        "capital_deployment_state": capital_state,
        "template_prior_path": template_prior_path,
        "causal_review_candidates": causal_review_candidates,
        "validated_causal_rules": causal_rule_validation.get("validated_rules", 0),
        "causal_rule_validation_status_counts": causal_rule_validation.get("status_counts", {}),
    }


def run_phase4_review(
    *,
    cfg: Dict[str, Any],
    db: Any,
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    """Run deterministic Phase4 reviewer validation, report, and learning."""
    expected_tickers = len(cfg.get("tickers", []))
    errors: List[str] = []
    warnings: List[str] = []

    phase1 = db.get_trading_day_phase(config_id, trading_date, TradingPhase.PHASE1)
    phase2 = db.get_trading_day_phase(config_id, trading_date, TradingPhase.PHASE2)
    phase3 = db.get_trading_day_phase(config_id, trading_date, TradingPhase.PHASE3)
    phase4 = db.get_trading_day_phase(config_id, trading_date, TradingPhase.PHASE4)
    if phase4 and phase4.get("status") == "completed":
        raise RuntimeError(f"Phase4 already completed for {cfg['exp_name']} on {trading_date}")

    db.start_trading_day_phase(config_id, trading_date, TradingPhase.PHASE4)
    conn = None
    try:
        latest_portfolio = db.get_latest_settled_portfolio(config_id)
        phase1_transactions = db.get_futures_transactions_by_date(
            config_id,
            trading_date,
            execution_phase=TradingPhase.PHASE1,
        )
        phase2_transactions = db.get_futures_transactions_by_date(
            config_id,
            trading_date,
            execution_phase=TradingPhase.PHASE2,
        )
        transactions_by_recommendation = _group_transactions_by_recommendation(phase2_transactions)

        recommendations = db.get_futures_recommendations_by_effective_date(config_id, trading_date)
        strategy_recommendations = [
            recommendation
            for recommendation in recommendations
            if recommendation.get("source_type") == RecommendationSourceType.STRATEGY.value
        ]
        rollover_recommendations = [
            recommendation
            for recommendation in recommendations
            if recommendation.get("source_type") == RecommendationSourceType.ROLLOVER.value
        ]
        same_day_rollovers = [
            recommendation
            for recommendation in rollover_recommendations
            if recommendation.get("trading_date") == trading_date
        ]

        conn = sqlite3.connect(db.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        settlement_row = _fetchone(
            cursor,
            """
            SELECT ds.*, p.trading_date AS portfolio_trading_date
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE p.config_id = ?
              AND substr(ds.trading_date, 1, 10) = ?
            ORDER BY ds.created_at DESC
            LIMIT 1
            """,
            (config_id, trading_date),
        )

        if not phase1 or phase1.get("status") != "completed":
            errors.append(f"phase1 not completed on {trading_date}")

        if len(strategy_recommendations) < expected_tickers:
            errors.append(
                f"strategy recommendations are incomplete: expected at least {expected_tickers}, "
                f"got {len(strategy_recommendations)}"
            )

        if phase1_transactions:
            errors.append(f"phase1 should not write real transactions, but found {len(phase1_transactions)} rows")

        if not phase2 or phase2.get("status") != "completed":
            errors.append(f"phase2 not completed on {trading_date}")
        elif not phase2_transactions:
            zero_transaction_day = classify_zero_transaction_day(strategy_recommendations)
            zero_transaction_class = zero_transaction_day["classification"]
            zero_transaction_reasons = zero_transaction_day["reasons"]
            if zero_transaction_class == "expected":
                warnings.append(
                    f"phase2 completed on {trading_date} with 0 transactions, but all strategy recommendations "
                    f"were expected no-trade cases: {dict(Counter(zero_transaction_reasons))}"
                )
            else:
                errors.append(
                    f"phase2 completed on {trading_date} but no transactions were written; "
                    f"classification={zero_transaction_class}, reasons={dict(Counter(zero_transaction_reasons))}"
                )

        if not phase3 or phase3.get("status") != "completed":
            errors.append(f"phase3 not completed on {trading_date}")
        else:
            if settlement_row is None:
                errors.append(f"phase3 completed on {trading_date} but daily_settlement row is missing")
            if not latest_portfolio or _normalize_date(latest_portfolio.get("trading_date")) != trading_date:
                errors.append(
                    f"phase3 completed on {trading_date} but latest settled portfolio trading_date is "
                    f"{latest_portfolio.get('trading_date') if latest_portfolio else 'None'}"
                )

        commission_from_transactions = round(sum(float(tx.get("commission") or 0) for tx in phase2_transactions), 2)
        if settlement_row is not None:
            settlement_commission = round(float(settlement_row.get("commission") or 0), 2)
            if not isclose(commission_from_transactions, settlement_commission, abs_tol=0.01):
                errors.append(
                    f"commission mismatch: transactions={commission_from_transactions:.2f}, "
                    f"daily_settlement={settlement_commission:.2f}"
                )

            actual_balance_change = (
                float(settlement_row.get("current_balance") or 0.0)
                - float(settlement_row.get("previous_balance") or 0.0)
            )
            expected_balance_change = _expected_settlement_balance_change(settlement_row)
            if not isclose(actual_balance_change, expected_balance_change, abs_tol=0.01):
                errors.append(
                    f"settlement balance formula mismatch: actual_change={actual_balance_change:.2f}, "
                    f"expected_change={expected_balance_change:.2f}"
                )

            if latest_portfolio and _normalize_date(latest_portfolio.get("trading_date")) == trading_date:
                account_equity = _futures_account_equity(
                    settlement_row.get("current_balance"),
                    settlement_row.get("current_margin"),
                )
                portfolio_margin = float(latest_portfolio.get("margin_used") or 0.0)
                portfolio_available = float(latest_portfolio.get("available_cash") or 0.0)
                expected_available = (
                    float(settlement_row.get("current_balance") or 0.0)
                    - float(settlement_row.get("current_margin") or 0.0)
                )
                if not isclose(portfolio_margin, float(settlement_row.get("current_margin") or 0.0), abs_tol=0.01):
                    errors.append(
                        f"portfolio margin mismatch: portfolio={portfolio_margin:.2f}, "
                        f"daily_settlement={float(settlement_row.get('current_margin') or 0.0):.2f}"
                    )
                if not isclose(portfolio_available, expected_available, abs_tol=0.01):
                    errors.append(
                        f"available_cash mismatch: portfolio={portfolio_available:.2f}, "
                        f"expected current_balance-current_margin={expected_available:.2f}"
                    )

                positions = latest_portfolio.get("positions") or {}
                net_exposure, single_exposures = _position_exposures(positions, account_equity)
                _apply_net_exposure_review(
                    trading_date=trading_date,
                    cfg=cfg,
                    net_exposure=net_exposure,
                    warnings=warnings,
                    errors=errors,
                    recommendations=strategy_recommendations,
                )

                max_single_config = cfg.get("risk_control", {}).get("max_single_position_ratio", {})
                max_single_position_ratio = max(
                    float(value)
                    for value in (max_single_config or {"safe": 0.12}).values()
                )
                single_breaches = {
                    ticker: ratio
                    for ticker, ratio in single_exposures.items()
                    if ratio > max_single_position_ratio + 0.001
                }
                if single_breaches:
                    formatted = ", ".join(
                        f"{ticker}={ratio:.2%}" for ticker, ratio in sorted(single_breaches.items())
                    )
                    warnings.append(
                        f"single-position exposure exceeds base soft cap {max_single_position_ratio:.2%}: {formatted}; "
                        "review dynamic opportunity budget and stop protection, but only portfolio tradable-capital usage is a hard gate"
                    )

        unbooked = [tx["id"] for tx in phase2_transactions if not tx.get("booked_in_settlement")]
        if unbooked:
            errors.append(f"{len(unbooked)} transactions are still unbooked after completed phase3")

        if same_day_rollovers:
            errors.append(
                f"found {len(same_day_rollovers)} same-day rollover recommendation(s) on {trading_date}"
            )

        no_trade_reason_counter = _validate_recommendation_execution_audit(
            recommendations,
            transactions_by_recommendation,
            errors,
        )
        quality_warnings, market_confirmation_quality = _collect_recommendation_quality_warnings(recommendations)
        warnings.extend(quality_warnings)
        neutral_accountability_preview = build_neutral_accountability_summary(
            [
                {
                    **recommendation,
                    "signal_snapshot": _recommendation_snapshot(recommendation),
                }
                for recommendation in strategy_recommendations
            ],
            cfg,
        )
        if hasattr(db, "_ensure_reviewer_learning_schema"):
            db._ensure_reviewer_learning_schema(cursor)
        capital_preview = _write_capital_deployment_state(
            cursor,
            cfg=cfg,
            config_id=config_id,
            trading_date=trading_date,
            settlement_row=settlement_row,
            strategy_recommendations=strategy_recommendations,
            no_trade_reason_counter=no_trade_reason_counter,
            write_learning_event=False,
        )

        logger.info(
            f"Reviewer summary for {cfg['exp_name']} on {trading_date}: "
            f"phase1={phase1.get('status') if phase1 else 'missing'}, "
            f"phase2={phase2.get('status') if phase2 else 'missing'}, "
            f"phase3={phase3.get('status') if phase3 else 'missing'}, "
            f"strategy_recommendations={len(strategy_recommendations)}, "
            f"rollover_recommendations={len(rollover_recommendations)}, "
            f"phase1_transactions={len(phase1_transactions)}, "
            f"phase2_transactions={len(phase2_transactions)}"
        )

        for warning in warnings:
            logger.warning(warning)
        for info_message in market_confirmation_quality.get("info_messages", []):
            logger.info(info_message)
        for error in errors:
            logger.error(error)

        phase4_status = "failed" if errors else "completed"
        logger.write_daily_summary(
            trading_date,
            _build_summary_payload(
                cfg=cfg,
                trading_date=trading_date,
                phase1=phase1,
                phase2=phase2,
                phase3=phase3,
                phase4_status=phase4_status,
                strategy_count=len(strategy_recommendations),
                rollover_count=len(rollover_recommendations),
                phase1_transaction_count=len(phase1_transactions),
                phase2_transaction_count=len(phase2_transactions),
                no_trade_reason_counter=no_trade_reason_counter,
                settlement_row=settlement_row,
                warnings=warnings,
                errors=errors,
                market_confirmation_quality=market_confirmation_quality,
                capital_deployment_state=capital_preview,
                neutral_accountability=neutral_accountability_preview,
            ),
        )

        try:
            report_path = _write_daily_transaction_report(
                cfg=cfg,
                config_id=config_id,
                trading_date=trading_date,
                cursor=cursor,
                settlement_row=settlement_row,
                latest_portfolio=latest_portfolio,
                recommendations=recommendations,
                strategy_recommendations=strategy_recommendations,
                phase2_transactions=phase2_transactions,
            )
            logger.info(f"Daily transaction report written: {report_path}")
        except Exception as report_exc:
            errors.append(f"daily transaction report generation failed: {report_exc}")
            logger.error(f"Daily transaction report generation failed: {report_exc}")

        if errors:
            raise RuntimeError(f"Phase flow validation failed with {len(errors)} error(s)")

        learning_summary = apply_reviewer_learning(
            db=db,
            cursor=cursor,
            cfg=cfg,
            config_id=config_id,
            trading_date=trading_date,
            settlement_row=settlement_row,
            recommendations=recommendations,
            strategy_recommendations=strategy_recommendations,
            no_trade_reason_counter=no_trade_reason_counter,
        )
        reviewer_report_paths = _write_reviewer_learning_report(
            cursor=cursor,
            cfg=cfg,
            config_id=config_id,
            trading_date=trading_date,
            learning_summary=learning_summary,
        )
        learning_summary["reviewer_report"] = reviewer_report_paths
        conn.commit()
        logger.info(f"Reviewer learning persisted: {learning_summary}")
        logger.info(f"Reviewer learning report written: {reviewer_report_paths}")

        db.complete_trading_day_phase(
            config_id,
            trading_date,
            TradingPhase.PHASE4,
            "completed",
            "reviewer validation and learning passed",
            memory_config=cfg.get("strategy_memory", {}),
        )
        logger.info("Phase4 reviewer validation and learning passed")
        return {
            "status": "completed",
            "warnings": warnings,
            "errors": errors,
            "learning_summary": learning_summary,
        }
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
                conn.close()
                conn = None
            except sqlite3.Error as rollback_exc:
                logger.warning(f"Phase4 rollback before failure status failed: {rollback_exc}")
        db.complete_trading_day_phase(config_id, trading_date, TradingPhase.PHASE4, "failed", str(exc))
        raise
    finally:
        if conn is not None:
            conn.close()
