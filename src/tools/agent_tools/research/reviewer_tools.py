from __future__ import annotations

"""Reviewer tools for Phase4 validation, reporting, and daily transaction logs."""

import json
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from math import isclose
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from apis.contract_info_cache import FuturesContractInfoCache
from database.artifact_store import (
    externalize_json_for_db,
    externalize_text_for_db,
    load_externalized_json,
)
from graph.schema import RecommendationSourceType, TradingPhase
from util.futures_audit import (
    build_actual_transactions,
    categorize_no_trade_reason,
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
from util.text_sanitize import sanitize_visible_text
from tools.agent_tools.research.learning_contract import (
    CONTRACT_KEY,
    attach_or_upgrade_next_round_memory_contract,
    attach_next_round_memory_contract,
    build_event_memory_contract,
    build_next_round_memory_contract,
)
from tools.agent_tools.research.neutral_accountability import build_neutral_accountability_summary
from tools.agent_tools.research.researcher_tools import (
    CausalReviewLLMOutput,
    ExploratoryHypothesisItem,
    ExploratoryHypothesisLLMOutput,
    apply_researcher_learning,
    run_researcher_causal_review,
    write_exploratory_hypotheses,
)
from tools.agent_tools.analysis.data_usage import data_usage_from_snapshot, compact_data_usage_notes


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
SRC_ROOT = Path(__file__).resolve().parents[3]


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


def _expected_settlement_equity_change(settlement_row: Dict[str, Any]) -> float:
    return (
        float(settlement_row.get("daily_pnl") or 0.0)
        - float(settlement_row.get("commission") or 0.0)
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
            if mode not in {"strong_opportunity", "alpha_release"}:
                continue
            try:
                candidate_cap = float(candidate.get("max_net_exposure"))
            except (TypeError, ValueError):
                continue
            if candidate_cap > max_net_exposure:
                max_net_exposure = candidate_cap
                cap_mode = "alpha_release"
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
    elif cap_mode == "alpha_release" and abs(net_exposure) > base_max_net_exposure + 0.001:
        source_text = f" via {cap_source}" if cap_source else ""
        warnings.append(
            f"net exposure above base cap on {trading_date} but within dynamic alpha-release cap"
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
    "horizon_consistency_requires_short_timing": {
        "category": "strategy_timing_gate",
        "risk_control_normal": True,
        "alpha_expansion_allowed": "conditional",
        "diagnosis": "A medium-term thesis lacked short-term timing confirmation for entry or continued exposure.",
        "suggested_action": "Do not deploy more capital until current technical/intraday evidence and invalidation are explicit.",
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
        "category": "auditor_suppression",
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
    "llm_neutral",
    "intraday_trigger_not_met",
    "intraday_opening_range_incomplete",
    "horizon_consistency_requires_short_timing",
    "trade_auditor_block",
    "trade_auditor_reduce_only",
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


def _memory_row_from_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    controls = plan.get("strategy_controls") if isinstance(plan.get("strategy_controls"), dict) else {}
    diagnostics = controls.get("diagnostics") if isinstance(controls.get("diagnostics"), dict) else {}
    learning = diagnostics.get("capital_utilization_learning") if isinstance(diagnostics.get("capital_utilization_learning"), dict) else {}
    for key in ("protected_memory", "adaptive_protect_record", "recovering_memory"):
        row = learning.get(key)
        if isinstance(row, dict) and row.get("memory_state"):
            return row
    auditor = plan.get("trade_auditor") or plan.get("decision_planner") or {}
    auditor_diag = auditor.get("diagnostics") if isinstance(auditor, dict) and isinstance(auditor.get("diagnostics"), dict) else {}
    memory = auditor_diag.get("strategy_memory") if isinstance(auditor_diag.get("strategy_memory"), dict) else {}
    for key in ("combo", "side_memory"):
        row = memory.get(key)
        if isinstance(row, dict) and row.get("memory_state"):
            return row
    for row in memory.get("records") or []:
        if isinstance(row, dict) and row.get("memory_state"):
            return row
    return {}


def _plan_has_explicit_stop(plan: Dict[str, Any]) -> bool:
    candidates = [
        plan,
        plan.get("signal_lifecycle") if isinstance(plan.get("signal_lifecycle"), dict) else {},
        plan.get("pretrade_invalidation") if isinstance(plan.get("pretrade_invalidation"), dict) else {},
    ]
    controls = plan.get("strategy_controls") if isinstance(plan.get("strategy_controls"), dict) else {}
    diagnostics = controls.get("diagnostics") if isinstance(controls.get("diagnostics"), dict) else {}
    pretrade = diagnostics.get("pretrade_invalidation_control")
    if isinstance(pretrade, dict):
        candidates.append(pretrade)
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if item.get("invalidation_level") is not None:
            return True
        if item.get("explicit_stop_protection") is True or item.get("stop_protected") is True:
            return True
        try:
            if float(item.get("atr_stop_distance") or 0.0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _plan_has_structured_invalidation(plan: Dict[str, Any]) -> bool:
    if _plan_has_explicit_stop(plan):
        return True
    structured_fields = (
        "counter_evidence",
        "would_change_view_if",
        "do_not_trade_reason",
        "invalidation_condition",
        "risk_boundary",
    )
    candidates = [
        plan,
        plan.get("signal_lifecycle") if isinstance(plan.get("signal_lifecycle"), dict) else {},
        plan.get("pretrade_invalidation") if isinstance(plan.get("pretrade_invalidation"), dict) else {},
    ]
    controls = plan.get("strategy_controls") if isinstance(plan.get("strategy_controls"), dict) else {}
    diagnostics = controls.get("diagnostics") if isinstance(controls.get("diagnostics"), dict) else {}
    pretrade = diagnostics.get("pretrade_invalidation_control")
    if isinstance(pretrade, dict):
        candidates.append(pretrade)
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if item.get("structured_invalidation") is True:
            return True
        for field in structured_fields:
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, (list, tuple, set)) and any(str(part).strip() for part in value):
                return True
    return False


def _plan_memory_combo_is_specific(row: Dict[str, Any]) -> bool:
    raw_combo = (row or {}).get("signal_combo")
    if isinstance(raw_combo, (list, tuple)):
        return bool(raw_combo) and "*" not in {str(item).strip() for item in raw_combo}
    text = str(raw_combo or "").strip()
    return bool(text) and text != "*"


def _recommendation_alpha_release_tier(
    *,
    plan: Dict[str, Any],
    cfg: Dict[str, Any],
    learned_quality: bool,
    confirmation_score: float,
) -> tuple[str, Dict[str, Any]]:
    control = cfg.get("capital_utilization_control") or {}
    row = _memory_row_from_plan(plan)
    combo_specific = _plan_memory_combo_is_specific(row)
    stop_protected = _plan_has_explicit_stop(plan)
    structured_invalidation = stop_protected or _plan_has_structured_invalidation(plan)
    tier = "boost" if learned_quality else "probe"
    limiting_reasons: List[str] = []
    min_boost_score = _safe_float(
        control.get("alpha_release_boost_min_confirmation_score"),
        _safe_float(control.get("memory_protected_min_confirmation_score"), 0.45),
    )
    if confirmation_score < min_boost_score:
        tier = "normal" if learned_quality else "probe"
        limiting_reasons.append("confirmation_below_alpha_release_boost_threshold")
    if not structured_invalidation:
        tier = "probe"
        limiting_reasons.append("missing_pretrade_invalidation")
    if structured_invalidation and not stop_protected:
        tier = "normal"
        limiting_reasons.append("missing_explicit_stop_for_alpha_release_boost")
    if not combo_specific:
        tier = "normal" if tier in {"boost", "max_boost"} else tier
        limiting_reasons.append("generic_memory_cannot_trigger_alpha_release_boost")
    max_score = _safe_float(
        control.get("alpha_release_max_boost_min_confirmation_score"),
        _safe_float(control.get("exceptional_validated_min_confirmation_score"), 0.85),
    )
    if tier == "boost" and confirmation_score >= max_score:
        tier = "max_boost"
    return tier, {
        "tier": tier,
        "stop_protected": stop_protected,
        "structured_invalidation": structured_invalidation,
        "specific_signal_combo": combo_specific,
        "limiting_reasons": limiting_reasons,
    }


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
    alpha_release_tier, alpha_release_requirements = _recommendation_alpha_release_tier(
        plan=plan,
        cfg=cfg,
        learned_quality=learned_quality,
        confirmation_score=confirmation_score,
    )
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
        "alpha_release_tier": alpha_release_tier,
        "alpha_release_requirements": alpha_release_requirements,
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
        "no_trade_reason_category_counts": _no_trade_reason_category_counts(no_trade_reason_counter),
        "category_counts": _sorted_counter_dict(category_counts),
        "action_counts": _sorted_counter_dict(action_counts),
        "reason_profiles": reason_profiles,
        "alpha_release_candidate_count": len(alpha_release_candidates),
        "alpha_release_candidates": alpha_release_candidates[:10],
        "recovery_probe_candidate_count": len(recovery_probe_candidates),
        "recovery_probe_candidates": recovery_probe_candidates[:10],
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
    extra_audit: Optional[Dict[str, Any]] = None,
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
        "extra_audit": extra_audit or {},
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


def _report_text(value: Any, limit: int = 900) -> str:
    text = sanitize_visible_text(str(value or "")).strip()
    if not text:
        return ""
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if len(text) > limit:
        return text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _report_inline(value: Any, limit: int = 180) -> str:
    text = _report_text(value, limit=limit).replace("\n", " ")
    return " ".join(text.split())


def _report_section(lines: List[str], title: str) -> None:
    lines.extend(["=" * 80, "", title, ""])


def _ticker_label(ticker: str) -> str:
    return str(ticker or "UNKNOWN").upper()


def _position_dict(position: Any) -> Dict[str, Any]:
    if isinstance(position, dict):
        return position
    if hasattr(position, "model_dump"):
        return position.model_dump()
    if hasattr(position, "__dict__"):
        return dict(position.__dict__)
    return {}


def _action_label(action: Any) -> str:
    value = str(action or "").lower()
    labels = {
        "open_long": "open long",
        "open_short": "open short",
        "close_long": "close long",
        "close_short": "close short",
        "hold": "hold",
    }
    return labels.get(value, value or "unknown")


def _recommendation_direction(recommendation: Dict[str, Any], snapshot: Dict[str, Any]) -> str:
    plan = _pre_open_plan(snapshot)
    side = _recommendation_side(recommendation, snapshot)
    if side in {"long", "short"}:
        return side
    action = str(recommendation.get("action") or "").lower()
    if "long" in action:
        return "long"
    if "short" in action:
        return "short"
    target_ratio = plan.get("target_position_ratio") or recommendation.get("target_position_ratio")
    return _target_side_from_ratio(target_ratio)


def _analyst_signal_line(snapshot: Dict[str, Any], analyst: str) -> str:
    payload = _analyst_payloads(snapshot).get(analyst) or {}
    if not payload:
        return f"    {analyst}: -/-"
    signal = payload.get("signal", "Neutral")
    confidence = _safe_float(payload.get("confidence"), 0.0)
    template = str(payload.get("template_name") or "")
    tradeability = str(payload.get("tradeability") or "")
    suffix_parts = []
    if tradeability:
        suffix_parts.append(f"tradeability={tradeability}")
    if template:
        suffix_parts.append(f"template={template}")
    suffix = f" ({'; '.join(suffix_parts)})" if suffix_parts else ""
    return f"    {analyst}: {signal}/{confidence:.2f}{suffix}"


def _analyst_reason_lines(snapshot: Dict[str, Any], *, limit: int = 520) -> List[str]:
    lines: List[str] = []
    for analyst in ANALYSTS:
        payload = _analyst_payloads(snapshot).get(analyst) or {}
        if not payload:
            continue
        lines.append(_analyst_signal_line(snapshot, analyst))
        reason = _report_text(payload.get("justification"), limit=limit)
        if reason:
            lines.append(f"      {reason}")
    return lines


def _signal_matrix_row(ticker: str, recommendation: Dict[str, Any], snapshot: Dict[str, Any], traded: bool) -> str:
    payloads = _analyst_payloads(snapshot)

    def cell(analyst: str) -> str:
        payload = payloads.get(analyst) or {}
        if not payload:
            return "-/-"
        return f"{payload.get('signal', 'Neutral')}/{_safe_float(payload.get('confidence'), 0.0):.2f}"

    direction = _recommendation_direction(recommendation, snapshot)
    status = "executed" if traded else "hold/no trade"
    return (
        f"  {ticker:<6} "
        f"{cell('commodity_news'):<22} "
        f"{cell('fundamental'):<22} "
        f"{cell('technical'):<22} "
        f"{direction:<8} {status}"
    )


def _signal_template_counts(strategy_recommendations: List[Dict[str, Any]]) -> Counter:
    counts: Counter = Counter()
    for recommendation in strategy_recommendations:
        snapshot = _recommendation_snapshot(recommendation)
        for payload in _analyst_payloads(snapshot).values():
            template = str(payload.get("template_name") or "").strip()
            if template and template != "unknown":
                counts[template] += 1
    return counts


def _validate_phase1_signal_persistence(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    strategy_recommendations: List[Dict[str, Any]],
    expected_tickers: int,
    expected_analysts: Iterable[str],
    errors: List[str],
    warnings: List[str],
) -> Dict[str, Any]:
    analysts = tuple(str(analyst) for analyst in expected_analysts if analyst)
    expected_pairs = max(0, int(expected_tickers)) * len(analysts)
    snapshot_pairs = set()
    missing_snapshot_pairs = []
    reference_portfolio_ids = set()
    for recommendation in strategy_recommendations:
        ticker = _ticker_label(recommendation.get("underlying_code") or recommendation.get("ticker"))
        reference_portfolio_id = recommendation.get("reference_portfolio_id")
        if reference_portfolio_id:
            reference_portfolio_ids.add(str(reference_portfolio_id))
        else:
            warnings.append(f"{ticker}: recommendation missing reference_portfolio_id for signal persistence validation")
        snapshot = _recommendation_snapshot(recommendation)
        payloads = _analyst_payloads(snapshot)
        for analyst in analysts:
            if payloads.get(analyst):
                snapshot_pairs.add((ticker, analyst))
            else:
                missing_snapshot_pairs.append(f"{ticker}:{analyst}")

    if expected_pairs and not reference_portfolio_ids:
        errors.append(
            "signal table persistence cannot be validated: "
            "phase1 recommendations do not contain reference_portfolio_id"
        )
        signal_rows = []
    else:
        placeholders = ",".join("?" for _ in reference_portfolio_ids)
        cursor.execute(
            f"""
            SELECT s.ticker, s.analyst, COUNT(*) AS row_count
            FROM signal s
            WHERE s.portfolio_id IN ({placeholders})
            GROUP BY s.ticker, s.analyst
            ORDER BY s.ticker, s.analyst
            """,
            tuple(sorted(reference_portfolio_ids)),
        )
        signal_rows = [dict(row) for row in cursor.fetchall()]
    db_pairs = {(str(row.get("ticker") or "").upper(), str(row.get("analyst") or "")) for row in signal_rows}
    duplicate_rows = [
        f"{str(row.get('ticker') or '').upper()}:{row.get('analyst')}={int(row.get('row_count') or 0)}"
        for row in signal_rows
        if int(row.get("row_count") or 0) > 1
    ]
    row_total = sum(int(row.get("row_count") or 0) for row in signal_rows)
    missing_db_pairs = [
        f"{ticker}:{analyst}"
        for ticker, analyst in sorted(snapshot_pairs)
        if (ticker, analyst) not in db_pairs
    ]
    extra_db_pairs = [
        f"{ticker}:{analyst}"
        for ticker, analyst in sorted(db_pairs)
        if (ticker, analyst) not in snapshot_pairs
    ]

    if expected_pairs and len(snapshot_pairs) != expected_pairs:
        errors.append(
            "phase1 analyst signal snapshot incomplete: "
            f"expected={expected_pairs}, found={len(snapshot_pairs)}, "
            f"missing={missing_snapshot_pairs[:12]}"
        )
    if expected_pairs and len(db_pairs) != expected_pairs:
        errors.append(
            "signal table persistence incomplete: "
            f"expected={expected_pairs}, distinct={len(db_pairs)}, rows={row_total}, "
            f"missing={missing_db_pairs[:12]}, extra={extra_db_pairs[:12]}"
        )
    if duplicate_rows:
        errors.append(
            "signal table contains duplicate analyst rows for the same trading day: "
            f"{duplicate_rows[:12]}"
        )
    verified = bool(expected_pairs and not missing_snapshot_pairs and not missing_db_pairs and not duplicate_rows)
    return {
        "expected_pairs": expected_pairs,
        "snapshot_pairs": len(snapshot_pairs),
        "db_pairs": len(db_pairs),
        "db_rows": row_total,
        "missing_snapshot_pairs": missing_snapshot_pairs,
        "missing_db_pairs": missing_db_pairs,
        "extra_db_pairs": extra_db_pairs,
        "duplicate_rows": duplicate_rows,
        "verified": verified,
        "reference_portfolio_ids": sorted(reference_portfolio_ids),
    }


def _phase_rows(cursor, config_id: str, trading_date: str) -> List[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT phase, status, started_at, completed_at, message
        FROM trading_day_phase
        WHERE config_id = ?
          AND substr(trading_date, 1, 10) = ?
        ORDER BY phase
        """,
        (config_id, trading_date),
    )
    return [dict(row) for row in cursor.fetchall()]


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
    config_id: str,
    trading_date: str,
    cursor,
    settlement_row: Dict[str, Any] | None,
    latest_portfolio: Dict[str, Any] | None,
    strategy_recommendations: List[Dict[str, Any]],
    recommendations: List[Dict[str, Any]],
    phase2_transactions: List[Dict[str, Any]],
    ticker_pnl: Dict[str, Dict[str, Any]],
    phase4_status_override: Optional[str] = None,
    phase4_completed_at_override: Optional[str] = None,
    phase4_message_override: Optional[str] = None,
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
    snapshots_by_id = {
        str(recommendation.get("id")): _recommendation_snapshot(recommendation)
        for recommendation in strategy_recommendations
        if recommendation.get("id")
    }
    recommendations_by_ticker = {
        _ticker_label(recommendation.get("underlying_code") or recommendation.get("ticker")): recommendation
        for recommendation in strategy_recommendations
    }
    phase_rows = _phase_rows(cursor, config_id, trading_date)
    if phase4_status_override:
        phase4_found = False
        for row in phase_rows:
            if str(row.get("phase") or "") == TradingPhase.PHASE4.value:
                phase4_found = True
                row["status"] = phase4_status_override
                if phase4_completed_at_override is not None:
                    row["completed_at"] = phase4_completed_at_override
                if phase4_message_override is not None:
                    row["message"] = phase4_message_override
        if not phase4_found:
            phase_rows.append(
                {
                    "phase": TradingPhase.PHASE4.value,
                    "status": phase4_status_override,
                    "started_at": "",
                    "completed_at": phase4_completed_at_override or "",
                    "message": phase4_message_override or "",
                }
            )
    previous_equity = (
        _futures_account_equity(settlement_row.get("previous_balance"), settlement_row.get("previous_margin"))
        if settlement_row
        else 0.0
    )
    margin_ratio = _safe_float((settlement_row or {}).get("margin_ratio"), 0.0)
    total_assets = _safe_float((latest_portfolio or {}).get("total_assets"), account_equity)
    leverage = _safe_float((latest_portfolio or {}).get("leverage"), 1.0)
    positions = (latest_portfolio or {}).get("positions") or {}
    warnings_text = "yes" if (settlement_row and (settlement_row.get("is_warning") or settlement_row.get("is_liquidation"))) else "no"

    lines = [
        f"AgentQuant {trading_date} 完整交易日志",
        "=" * 80,
        "",
    ]

    lines.extend(
        [
            "一、账户总览",
            "",
            f"  期初权益:      {_money(previous_equity)}",
            f"  期末权益:      {_money(account_equity)}",
            f"  当日盈亏:      {_signed_money((settlement_row or {}).get('daily_pnl'))}",
            f"  手续费:        {_money((settlement_row or {}).get('commission'))}",
            f"  保证金占用:    {_money((settlement_row or {}).get('current_margin'))} "
            f"(保证金率 {margin_ratio:.2%})",
            f"  可用资金:      {_money((settlement_row or {}).get('cash_available') or (settlement_row or {}).get('current_balance'))}",
            f"  总资产:        {_money(total_assets)}",
            f"  杠杆倍数:      {leverage:.2f}x",
            f"  预警/强平:     {warnings_text}",
            "",
        ]
    )

    _report_section(lines, "二、当日交易执行")
    lines.append(f"共 {len(phase2_transactions)} 笔交易，在 phase2 阶段完成。")
    lines.append("")
    lines.append(
        f"  {'#':>2}  {'品种':<8} {'方向':<12} {'手数':>5} "
        f"{'成交价':>12} {'结算价':>12} {'手续费':>12} {'盈亏':>12}"
    )
    if not phase2_transactions:
        lines.append("- none")
    tx_index = 0
    for ticker in sorted(grouped_transactions):
        pnl_row = ticker_pnl.get(ticker, {})
        for tx in grouped_transactions[ticker]:
            tx_index += 1
            lines.append(
                f"  {tx_index:>2}  {ticker:<8} {_action_label(tx.get('action')):<12} "
                f"{int(tx.get('lots') or 0):>5} "
                f"{_money(tx.get('execution_price')):>12} "
                f"{_money(tx.get('settle_price')):>12} "
                f"{_money(tx.get('commission')):>12} "
                f"{_signed_money(tx.get('daily_pnl')):>12}"
            )
        lines.append(
            f"      {ticker} PnL breakdown: daily={_signed_money(pnl_row.get('daily_pnl'))}; "
            f"holding={_signed_money(pnl_row.get('holding_pnl'))}; "
            f"new={_signed_money(pnl_row.get('new_position_pnl'))}; "
            f"close={_signed_money(pnl_row.get('close_pnl'))}"
        )

    _report_section(lines, "三、交易原因详述")
    if not phase2_transactions:
        lines.append("  无实际成交。")
    for ticker in sorted(grouped_transactions):
        recommendation = recommendations_by_ticker.get(_ticker_label(ticker)) or {}
        snapshot = snapshots_by_id.get(str(recommendation.get("id") or "")) or _recommendation_snapshot(recommendation)
        plan = _pre_open_plan(snapshot)
        for tx in grouped_transactions[ticker]:
            lines.append(
                f"  {ticker} - {_action_label(tx.get('action'))} {int(tx.get('lots') or 0)} lot(s), "
                f"transaction_pnl={_signed_money(tx.get('daily_pnl'))}"
            )
            justification = _report_text(recommendation.get("justification"), limit=1100)
            if justification:
                lines.append("")
                lines.append("  【融合决策依据】")
                lines.append(f"  {justification}")
            control_notes = plan.get("control_notes") or plan.get("control_reasons") or []
            if control_notes:
                lines.append(f"  【控制规则】{control_notes}")
            lines.append(
                "  【盘前目标计划】"
                f"target_lots={plan.get('target_lots_estimate')}; "
                f"target_position_ratio={_percent(plan.get('target_position_ratio'))}; "
                f"tradable_lots_if_executed_now={plan.get('tradable_lots_if_executed_now')} "
                f"({plan.get('tradable_lots_reason')})"
            )
            analyst_lines = _analyst_reason_lines(snapshot, limit=420)
            if analyst_lines:
                lines.append("  【各分析师信号】")
                lines.extend(analyst_lines)
            execution_translation = snapshot.get("execution_translation") if isinstance(snapshot.get("execution_translation"), dict) else {}
            if execution_translation:
                final_basis = execution_translation.get("final_execution_basis") or {}
                lines.append(
                    "  【执行细节】"
                    f"{_action_label(tx.get('action'))} {int(tx.get('lots') or 0)} lot(s) "
                    f"@ {_money(tx.get('execution_price'))}; "
                    f"basis={execution_translation.get('base_price_source') or final_basis.get('base_price_source')}; "
                    f"slippage={execution_translation.get('slippage_amount') or final_basis.get('slippage_amount')}"
                )
            if tx.get("justification"):
                lines.append(f"  【交易备注】{_report_text(tx.get('justification'), limit=520)}")
            lines.append("")

    _report_section(lines, "四、未交易品种原因详述")
    recommendations_by_id = {str(item.get("id")): item for item in recommendations if item.get("id")}
    for recommendation in strategy_recommendations:
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "UNKNOWN")
        if ticker in traded_tickers:
            continue
        reason = _resolve_no_trade_reason(recommendation, has_transactions=False) or str(recommendation.get("status") or "unknown")
        reason_category = categorize_no_trade_reason(reason)
        snapshot = _json_loads(recommendation.get("signal_snapshot")) or {}
        combo = _signal_combo_from_snapshot(snapshot) if isinstance(snapshot, dict) else ["Neutral", "Neutral", "Neutral"]
        plan = _pre_open_plan(snapshot) if isinstance(snapshot, dict) else {}
        direction = _recommendation_direction(recommendation, snapshot if isinstance(snapshot, dict) else {})
        lines.append(
            f"  {ticker} - 融合方向={direction}; 未交易原因={reason}; "
            f"原因大类={reason_category['category_label']}({reason_category['category']}); combo={combo}"
        )
        justification = _report_text(recommendation.get("justification"), limit=900)
        if justification:
            lines.append("  【融合决策依据】")
            lines.append(f"  {justification}")
        lines.append(
            "  【盘前目标计划】"
            f"target_lots={plan.get('target_lots_estimate')}; "
            f"target_position_ratio={_percent(plan.get('target_position_ratio'))}; "
            f"tradable_lots_if_executed_now={plan.get('tradable_lots_if_executed_now')} "
            f"({plan.get('tradable_lots_reason')})"
        )
        analyst_lines = _analyst_reason_lines(snapshot if isinstance(snapshot, dict) else {}, limit=360)
        if analyst_lines:
            lines.append("  【各分析师信号】")
            lines.extend(analyst_lines)
        lines.append("")

    _report_section(lines, "五、信号汇总")
    lines.append(
        f"  5.1 三分析师信号矩阵（{len(strategy_recommendations)} 品种 x 3 分析师）"
    )
    lines.append("")
    lines.append(
        f"  {'品种':<6} {'新闻面':<22} {'基本面':<22} {'技术面':<22} {'融合':<8} 状态"
    )
    for recommendation in strategy_recommendations:
        ticker = _ticker_label(recommendation.get("underlying_code") or recommendation.get("ticker"))
        snapshot = snapshots_by_id.get(str(recommendation.get("id") or "")) or _recommendation_snapshot(recommendation)
        lines.append(_signal_matrix_row(ticker, recommendation, snapshot, ticker in traded_tickers))
    lines.append("")
    lines.append("  5.2 信号模板分布")
    for template, count in _signal_template_counts(strategy_recommendations).most_common():
        lines.append(f"  {template:<48} {count}")
    if not _signal_template_counts(strategy_recommendations):
        lines.append("  none")

    _report_section(lines, "六、系统决策流程")
    lines.append(f"  {'阶段':<18} {'状态':<10} {'开始时间':<28} {'完成时间':<28} 说明")
    for row in phase_rows:
        lines.append(
            f"  {str(row.get('phase') or ''):<18} "
            f"{str(row.get('status') or ''):<10} "
            f"{str(row.get('started_at') or ''):<28} "
            f"{str(row.get('completed_at') or ''):<28} "
            f"{_report_inline(row.get('message'), limit=120)}"
        )
    executed_tickers = ", ".join(sorted(traded_tickers)) if traded_tickers else "none"
    lines.extend(["", f"  关键决策节点：实际执行={executed_tickers}"])

    _report_section(lines, "七、收盘持仓")
    if not positions:
        lines.append("  收盘无持仓。")
    else:
        lines.append(
            f"  {'品种':<8} {'方向':<8} {'手数':>6} {'入场均价':>12} "
            f"{'结算价':>12} {'保证金':>12} {'持仓浮盈/亏':>14}"
        )
        for ticker in sorted(positions):
            pos = _position_dict(positions[ticker])
            shares = _safe_int(pos.get("shares"))
            if shares == 0:
                continue
            side = "LONG" if shares > 0 else "SHORT"
            lines.append(
                f"  {ticker:<8} {side:<8} {shares:>6} "
                f"{_money(pos.get('entry_price')):>12} "
                f"{_money(pos.get('current_settle_price') or pos.get('settle_price')):>12} "
                f"{_money(pos.get('margin_used')):>12} "
                f"{_signed_money(pos.get('unrealized_pnl')):>14}"
            )
    lines.append(
        f"\n  保证金合计={_money((settlement_row or {}).get('current_margin'))}, "
        f"保证金率={margin_ratio:.2%}, 杠杆={leverage:.2f}x."
    )

    _report_section(lines, "八、当日关键特征")
    directional: Dict[str, List[str]] = {"Bullish": [], "Bearish": []}
    for recommendation in strategy_recommendations:
        ticker = _ticker_label(recommendation.get("underlying_code") or recommendation.get("ticker"))
        snapshot = snapshots_by_id.get(str(recommendation.get("id") or "")) or _recommendation_snapshot(recommendation)
        for payload in _analyst_payloads(snapshot).values():
            signal = str(payload.get("signal") or "")
            if signal in directional:
                directional[signal].append(ticker)
    total_signals = len(strategy_recommendations) * len(ANALYSTS)
    directional_count = len(directional["Bullish"]) + len(directional["Bearish"])
    close_or_reduce = sum(1 for tx in phase2_transactions if str(tx.get("action") or "").lower().startswith("close"))
    lines.append(
        f"  1. 防守/收缩日：{len(phase2_transactions)} 笔交易中 {close_or_reduce} 笔为平仓/减仓。"
    )
    lines.append(
        f"  2. 当日盈亏 {_signed_money((settlement_row or {}).get('daily_pnl'))}，"
        f"手续费 {_money((settlement_row or {}).get('commission'))}。"
    )
    lines.append(
        f"  3. 方向性信号：{total_signals} 个信号中 {directional_count} 个非 Neutral；"
        f"Bullish={sorted(set(directional['Bullish']))}; Bearish={sorted(set(directional['Bearish']))}."
    )
    lines.append(f"  4. 日内执行品种：{executed_tickers}。")
    lines.append(f"  5. 收盘持仓品种数：{len([p for p in positions.values() if _safe_int(_position_dict(p).get('shares')) != 0])}。")
    lines.extend(["", "追溯信息"])
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
    phase4_status_override: Optional[str] = None,
    phase4_completed_at_override: Optional[str] = None,
    phase4_message_override: Optional[str] = None,
) -> Path:
    report_text = _build_daily_transaction_report(
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        cursor=cursor,
        settlement_row=settlement_row,
        latest_portfolio=latest_portfolio,
        strategy_recommendations=strategy_recommendations,
        recommendations=recommendations,
        phase2_transactions=phase2_transactions,
        ticker_pnl=_ticker_daily_pnl_rows(cursor, config_id, trading_date),
        phase4_status_override=phase4_status_override,
        phase4_completed_at_override=phase4_completed_at_override,
        phase4_message_override=phase4_message_override,
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
    neutral_accountability["shadow_tracking"] = _neutral_shadow_tracking_summary(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        recommendations=neutral_recommendations,
        write_event=False,
    )

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
    shadow_tracking = neutral_accountability.get("shadow_tracking") or {}
    if isinstance(shadow_tracking, dict):
        lines.extend(
            [
                f"- shadow_observation_count: {shadow_tracking.get('observation_count', 0)}",
                f"- shadow_missed_opportunity_count: {shadow_tracking.get('missed_opportunity_count', 0)}",
                f"- shadow_reasonable_avoidance_count: {shadow_tracking.get('reasonable_avoidance_count', 0)}",
                f"- total_shadow_pnl: {_signed_money(shadow_tracking.get('total_shadow_pnl'))}",
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
    shadow_side = str(payload.get("neutral_shadow_side") or contract.get("shadow_side") or "flat").lower()
    if shadow_side not in {"long", "short", "flat"}:
        shadow_side = "flat"
    priority = str(payload.get("neutral_watchlist_priority") or contract.get("watchlist_priority") or "none")
    return {
        "bucket": bucket,
        "trigger_condition": trigger,
        "shadow_side": shadow_side,
        "watchlist_priority": priority,
        "observation_window": str(
            payload.get("recommended_observation_window") or contract.get("observation_window") or ""
        ),
        "opportunity_cost_risk": str(payload.get("opportunity_cost_risk") or contract.get("opportunity_cost_risk") or ""),
        "tracking_only": bool(contract.get("tracking_only", True)),
        "trade_permission": str(contract.get("trade_permission") or "none_without_current_confirmation"),
    }


def _neutral_opportunity_observations(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    observations: List[Dict[str, Any]] = []
    for analyst, payload in _analyst_payloads(snapshot).items():
        if str(payload.get("signal") or "Neutral") != "Neutral":
            continue
        contract = _neutral_contract_from_payload(payload)
        if contract["bucket"] in {"unknown", "low_tradeability"} and contract["shadow_side"] == "flat":
            continue
        observations.append(
            {
                "analyst": analyst,
                "bucket": contract["bucket"],
                "trigger_condition": contract["trigger_condition"],
                "shadow_side": contract["shadow_side"],
                "watchlist_priority": contract["watchlist_priority"],
                "observation_window": contract["observation_window"],
                "opportunity_cost_risk": contract["opportunity_cost_risk"],
                "neutral_reason": str(payload.get("neutral_reason") or ""),
                "tracking_only": True,
                "trade_permission": contract["trade_permission"],
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
    opportunity_layers = []
    factor_focus = []
    conflicts = []
    for contract in contracts.values():
        if not isinstance(contract, dict):
            continue
        if contract.get("opportunity_type"):
            opportunity_types.append(str(contract.get("opportunity_type")))
        if contract.get("opportunity_layer"):
            opportunity_layers.append(str(contract.get("opportunity_layer")))
        factor_focus.extend(str(item) for item in (contract.get("factor_focus") or []))
        conflicts.extend(str(item) for item in (contract.get("current_evidence_conflict") or []))
    if contracts:
        return {
            "contract_version": "agentquant.research.v1",
            "dominant_opportunity_types": sorted(set(opportunity_types)),
            "opportunity_layers": sorted(set(opportunity_layers)),
            "factor_focus": sorted(set(factor_focus))[:12],
            "current_evidence_conflict": sorted(set(conflicts))[:12],
        }
    return {
        "contract_version": "agentquant.research.v1",
        "dominant_opportunity_types": [],
        "opportunity_layers": [],
        "factor_focus": [],
        "current_evidence_conflict": [],
    }


def _primary_opportunity_type(snapshot: Dict[str, Any]) -> str:
    summary = _opportunity_contract_summary(snapshot)
    values = summary.get("dominant_opportunity_types") or []
    for value in values:
        if str(value) not in {"unknown", "no_trade"}:
            return str(value)
    return str(values[0]) if values else "unknown"


def _primary_opportunity_layer(snapshot: Dict[str, Any]) -> str:
    plan = _pre_open_plan(snapshot)
    if plan.get("pm_decision_layer"):
        return str(plan.get("pm_decision_layer"))
    summary = _opportunity_contract_summary(snapshot)
    values = summary.get("opportunity_layers") or []
    for preferred in ("deployable_alpha", "tradeable_setup", "risk_reduction", "direction_only", "no_trade"):
        if preferred in values:
            return preferred
    return str(values[0]) if values else "direction_only"


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


def _policy_contract_payload(
    *,
    policy_type: str,
    policy_action: str,
    reason: str,
    scope: Dict[str, Any],
    evidence: Dict[str, Any],
    multiplier: float = 1.0,
    maturity_state: str = "validated_policy",
    status: str = "applied",
) -> Dict[str, Any]:
    """Create adaptive-policy payload that is usable as next-round strategy memory."""
    action = str(policy_action or "").lower()
    is_protect = action in {"protect", "allow"}
    is_reduce = action in {"cap", "reduce", "block", "demote", "probe_only", "weak_block"}
    sample_count = _safe_int(evidence.get("sample_count") or evidence.get("total_trades"), 0)
    confidence = _safe_float(evidence.get("confidence_score"), 0.0)
    if not confidence:
        confidence = _confidence_from_summary(evidence)
    if is_protect:
        pm_condition = (
            f"May support protected/deployable sizing in this scope only if today's signal, "
            f"market confirmation, horizon, and invalidation boundary agree; multiplier={multiplier:.2f}."
        )
        position_authority = "pm_auditor_conditioned"
        max_impact = "may_support_alpha_scaling_inside_20pct_cap"
    elif is_reduce:
        pm_condition = (
            f"Cap, reduce, or probe-only same-scope exposure until fresh evidence repairs the setup; "
            f"multiplier={multiplier:.2f}."
        )
        position_authority = "risk_reduction_conditioned"
        max_impact = "may_reduce_or_cap_only_through_pm_auditor"
    else:
        pm_condition = "Use as diagnostic strategy memory; no direct sizing impact without separate validation."
        position_authority = "analysis_prior_only"
        max_impact = "no_direct_position_impact"
    return attach_or_upgrade_next_round_memory_contract(
        {
            **dict(evidence or {}),
            "source": policy_type,
            "policy_type": policy_type,
            "policy_action": action,
            "reason": reason,
            "multiplier": multiplier,
            "evidence": evidence,
        },
        memory_type=policy_type,
        maturity_state=maturity_state,
        status=status,
        scope=scope,
        usable_memory=[
            reason,
            f"policy_action={action}; multiplier={multiplier:.2f}; sample_count={sample_count}",
        ],
        analysis_strategy_updates=[
            "Use the policy as same-scope strategy memory, not a product-wide shortcut.",
            "Before citing it, compare today's data drivers, signal template, horizon, and market regime.",
        ],
        trading_strategy_updates=[
            pm_condition,
            "Never use this policy to override hard risk limits, auditor, or current-day evidence.",
        ],
        pm_action_conditions=[pm_condition],
        invalidates_when=[
            "Today's same-scope data contradicts the remembered setup.",
            "The policy validity window has expired or the ticker/side/template/horizon/regime no longer match.",
            "A new/add-on trade lacks explicit current confirmation and invalidation boundary.",
        ],
        validation_plan=[
            "Refresh same-scope sample count, win rate, net PnL, and benchmark comparison after future settlements.",
        ],
        position_authority=position_authority,
        max_position_impact=max_impact,
        sample_count=sample_count,
        confidence_score=confidence,
    )


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
    action_payload = dict(action or {})
    if CONTRACT_KEY not in action_payload:
        action_payload[CONTRACT_KEY] = build_event_memory_contract(
            event_type=event_type,
            scope_type=scope_type,
            scope_key=scope_key,
            evidence=evidence,
            action=action_payload,
            status=status,
        )
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
            _json_dumps(action_payload),
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
            digest_contract = build_next_round_memory_contract(
                memory_type="analyst_learning_digest",
                maturity_state="mature_digest",
                scope={
                    "analyst": analyst,
                    "ticker": ticker,
                    "sector": sector,
                    "side": signal_side,
                    "horizon_class": horizon,
                    "market_regime": "*",
                },
                usable_memory=digest,
                analysis_strategy_updates=[
                    "Use this digest to calibrate the analyst's future confidence in the same ticker/side/horizon scope.",
                    "State whether today's evidence is stronger, weaker, or contradictory before citing it.",
                ],
                trading_strategy_updates=[
                    "Positive mature digests may support deployable/protected treatment only when current confirmation and invalidation are clear.",
                    "Weak mature digests should lower confidence unless today's evidence has stronger same-scope support.",
                ],
                validation_plan=[
                    "Continue updating hit rate, net PnL, and same-scope sample count before changing confidence strongly.",
                ],
                sample_count=sample_count,
                confidence_score=confidence,
            )
            event_id = _insert_learning_event(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                event_type="analyst_learning_digest",
                scope_type="analyst_ticker_horizon",
                scope_key=f"{analyst}:{ticker}:{horizon}:{signal_side}",
                evidence=summary,
                action={"digest": digest, CONTRACT_KEY: digest_contract},
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
                    _json_dumps({**summary, CONTRACT_KEY: digest_contract}),
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
    plan = _pre_open_plan(snapshot)
    invalidation = plan.get("invalidation_level") or _invalidation_level(snapshot)
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


def _write_trade_episode_memory(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> int:
    learning_cfg = cfg.get("learning", {}) or {}
    episode_cfg = learning_cfg.get("trade_episode_memory", {}) or {}
    if not bool(episode_cfg.get("enabled", True)):
        return 0
    pairs = _completed_pairs_up_to(cursor, config_id=config_id, trading_date=trading_date)
    if not pairs:
        return 0
    recommendation_lookup = _recommendations_by_id(
        cursor,
        [pair.get("open_recommendation_id") for pair in pairs if pair.get("open_recommendation_id")],
    )
    transaction_lookup = _transactions_by_id(
        cursor,
        [
            tx_id
            for pair in pairs
            for tx_id in (pair.get("open_transaction_id"), pair.get("close_transaction_id"))
            if tx_id
        ],
    )
    now = _utc_now()
    inserted = 0
    for pair in pairs:
        if str(pair.get("close_date") or "") > trading_date:
            continue
        recommendation = recommendation_lookup.get(str(pair.get("open_recommendation_id") or ""))
        snapshot = _recommendation_snapshot(recommendation or {})
        ticker = str(pair.get("ticker") or "").upper()
        side = str(pair.get("side") or "").lower()
        if not ticker or side not in {"long", "short"}:
            continue
        combo = _signal_combo_from_snapshot(snapshot)
        expected_days = _expected_horizon_days(snapshot, side)
        horizon = _horizon_class(expected_days, snapshot)
        regime = _market_regime(snapshot)
        template = _signal_template(side, combo, snapshot)
        sector = _sector_for_ticker(cfg, ticker)
        net_pnl = _safe_float(pair.get("net_pnl"))
        episode_date = str(pair.get("close_date") or trading_date or "")
        lesson = _episode_lesson_text(
            ticker=ticker,
            side=side,
            template=template,
            horizon=horizon,
            regime=regime,
            pair=pair,
            snapshot=snapshot,
        )
        data_usage = data_usage_from_snapshot(snapshot)
        data_usage_notes = compact_data_usage_notes(data_usage)
        open_tx = transaction_lookup.get(str(pair.get("open_transaction_id") or "")) or {}
        close_tx = transaction_lookup.get(str(pair.get("close_transaction_id") or "")) or {}
        payload = {
            "pair": pair,
            "open_transaction": open_tx,
            "close_transaction": close_tx,
            "open_recommendation_id": pair.get("open_recommendation_id"),
            "signal_snapshot": snapshot,
            "trade_research_contract_summary": _opportunity_contract_summary(snapshot),
            "opportunity_type": _primary_opportunity_type(snapshot),
            "opportunity_layer": _primary_opportunity_layer(snapshot),
            "lesson_text": lesson,
            "analyst_payloads": _analyst_payloads(snapshot),
            "data_usage_summary": data_usage,
            "data_usage_notes": data_usage_notes,
            "pre_open_plan": _pre_open_plan(snapshot),
            "market_confirmation": snapshot.get("market_confirmation") if isinstance(snapshot.get("market_confirmation"), dict) else {},
            "created_from": "phase4_reviewer",
            "episode_date": episode_date,
            "review_trading_date": trading_date,
        }
        payload = attach_next_round_memory_contract(
            payload,
            memory_type="trade_episode_memory",
            maturity_state="episode_case",
            scope={
                "ticker": ticker,
                "sector": sector,
                "side": side,
                "signal_template": template,
                "horizon_class": horizon,
                "market_regime": regime,
            },
            usable_memory=[
                lesson,
                f"outcome={payload['pair'].get('net_pnl')}; holding_days={payload['pair'].get('holding_days')}",
                *data_usage_notes[:3],
            ],
            analysis_strategy_updates=[
                "Use as a comparable case when today's ticker/sector, side, horizon, and signal template are similar.",
                "Ask whether today's analyst evidence repeats or contradicts the drivers in this episode.",
            ],
            trading_strategy_updates=[
                "Use the episode to refine entry/exit/hold reasoning, not as a standalone trade command.",
                "Winning episodes preserve what worked; losing episodes identify what must be rechecked before repeating.",
            ],
            validation_plan=[
                "Accumulate same-scope future episodes before treating this as mature template evidence.",
            ],
            sample_count=1,
        )
        episode_id = str(uuid.uuid4())
        payload_ext = externalize_json_for_db(
            payload,
            category="trade_episode_memory",
            record_id=episode_id,
            field_name="payload",
            config_id=config_id,
            trading_date=trading_date,
        )
        cursor.execute(
            '''
            INSERT INTO trade_episode_memory (
                id, config_id, trading_date, ticker, side, sector, signal_template,
                signal_combo, horizon_class, market_regime, episode_date, first_seen_at,
                last_reviewed_at, open_date, close_date, holding_days, net_pnl,
                return_on_notional, outcome_label,
                lesson_text, payload_json, payload_artifact_path, payload_sha256,
                payload_size, payload_summary_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_id, ticker, side, open_date, close_date, signal_template)
            DO UPDATE SET
                trading_date=COALESCE(trade_episode_memory.trading_date, excluded.trading_date),
                sector=excluded.sector,
                signal_combo=excluded.signal_combo,
                horizon_class=excluded.horizon_class,
                market_regime=excluded.market_regime,
                episode_date=excluded.episode_date,
                first_seen_at=COALESCE(trade_episode_memory.first_seen_at, excluded.first_seen_at),
                last_reviewed_at=excluded.last_reviewed_at,
                holding_days=excluded.holding_days,
                net_pnl=excluded.net_pnl,
                return_on_notional=excluded.return_on_notional,
                outcome_label=excluded.outcome_label,
                lesson_text=excluded.lesson_text,
                payload_json=excluded.payload_json,
                payload_artifact_path=excluded.payload_artifact_path,
                payload_sha256=excluded.payload_sha256,
                payload_size=excluded.payload_size,
                payload_summary_json=excluded.payload_summary_json,
                created_at=excluded.created_at
            ''',
            (
                episode_id,
                config_id,
                trading_date,
                ticker,
                side,
                sector,
                template,
                _json_dumps(combo),
                horizon,
                regime,
                episode_date,
                now,
                now,
                pair.get("open_date"),
                pair.get("close_date"),
                _safe_int(pair.get("holding_days")),
                net_pnl,
                _safe_float(pair.get("return_on_notional")),
                "winner" if net_pnl > 0 else "loser" if net_pnl < 0 else "flat",
                lesson,
                payload_ext.inline_value,
                payload_ext.artifact_path,
                payload_ext.sha256,
                payload_ext.size_bytes,
                payload_ext.summary_json,
                now,
            ),
        )
        inserted += 1
    if inserted:
        _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="trade_episode_memory",
            scope_type="daily",
            scope_key=trading_date,
            evidence={"completed_pairs": len(pairs)},
            action={"episode_rows": inserted},
            status="applied",
        )
    return inserted


def _write_loss_template_observation_research(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> int:
    """Write observation-only loss-template research as candidate memory.

    This deliberately does not create adaptive policies or ticker-specific
    restrictions. It only makes repeated loss patterns visible to analysts and
    PM as next-round questions with clear usage boundaries.
    """
    learning_cfg = cfg.get("learning", {}) or {}
    research_cfg = learning_cfg.get("loss_template_observation", {}) or {}
    if not bool(research_cfg.get("enabled", True)):
        return 0

    pairs = [
        pair
        for pair in _completed_pairs_up_to(cursor, config_id=config_id, trading_date=trading_date)
        if _safe_float(pair.get("net_pnl")) < 0
    ]
    if not pairs:
        return 0

    lookback_days = int(research_cfg.get("lookback_days", 30) or 30)
    min_samples = int(research_cfg.get("min_loss_samples", 1) or 1)
    min_loss_abs = abs(_safe_float(research_cfg.get("min_cumulative_loss_abs"), 1.0))
    max_rows = int(research_cfg.get("max_rows_per_day", 4) or 4)
    valid_days = int(research_cfg.get("valid_days", learning_cfg.get("memory_expires_after_days", 30)) or 30)
    focus_tickers = {
        str(item).upper()
        for item in (research_cfg.get("focus_tickers") or [])
        if str(item or "").strip()
    }
    lookback_start = (datetime.strptime(trading_date[:10], "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    pairs = [
        pair
        for pair in pairs
        if str(pair.get("close_date") or "") >= lookback_start
        and (not focus_tickers or str(pair.get("ticker") or "").upper() in focus_tickers)
    ]
    if not pairs:
        return 0

    recommendation_lookup = _recommendations_by_id(
        cursor,
        [pair.get("open_recommendation_id") for pair in pairs if pair.get("open_recommendation_id")],
    )
    grouped: Dict[Tuple[str, str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    representative_snapshot: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    for pair in pairs:
        ticker = str(pair.get("ticker") or "").upper()
        side = str(pair.get("side") or "").lower()
        if not ticker or side not in {"long", "short"}:
            continue
        snapshot = _recommendation_snapshot(
            recommendation_lookup.get(str(pair.get("open_recommendation_id") or "")) or {}
        )
        combo = _signal_combo_from_snapshot(snapshot)
        horizon = _horizon_class(_expected_horizon_days(snapshot, side), snapshot)
        regime = _market_regime(snapshot)
        template = _signal_template(side, combo, snapshot)
        key = (ticker, side, template, horizon, regime)
        grouped[key].append(pair)
        representative_snapshot.setdefault(key, snapshot)

    candidates: List[Tuple[float, Tuple[str, str, str, str, str], List[Dict[str, Any]]]] = []
    for key, rows in grouped.items():
        if not any(str(row.get("close_date") or "") == trading_date for row in rows):
            continue
        cumulative_loss = sum(_safe_float(row.get("net_pnl")) for row in rows)
        if len(rows) < min_samples or abs(cumulative_loss) < min_loss_abs:
            continue
        candidates.append((abs(cumulative_loss), key, rows))
    candidates.sort(reverse=True, key=lambda item: (item[0], len(item[2])))

    now = _utc_now()
    valid_until = _valid_until(trading_date, valid_days)
    inserted = 0
    for _, key, rows in candidates[:max_rows]:
        ticker, side, template, horizon, regime = key
        snapshot = representative_snapshot.get(key) or {}
        sector = _sector_for_ticker(cfg, ticker)
        data_usage = data_usage_from_snapshot(snapshot)
        data_notes = compact_data_usage_notes(data_usage)
        summary = summarize_trade_pairs(rows)
        analyst_payloads = _analyst_payloads(snapshot)
        loss_examples = [
            {
                "open_date": row.get("open_date"),
                "close_date": row.get("close_date"),
                "holding_days": row.get("holding_days"),
                "net_pnl": row.get("net_pnl"),
                "return_on_notional": row.get("return_on_notional"),
            }
            for row in rows[:5]
        ]
        data_focus = data_notes[:4] or [
            "compare price trend, market confirmation, data freshness, and analyst conflict before repeating setup"
        ]
        hypothesis_text = (
            f"Observation-only loss template: {ticker} {side} {template} "
            f"horizon={horizon}, regime={regime}, samples={len(rows)}, "
            f"net_pnl={_safe_float(summary.get('total_pnl')):.0f}. "
            "Next comparable setups should test whether the data mix and market state still justify the trade."
        )
        suggested_use = (
            "observation-only prompt prior; do not block, blacklist, size down, add, or hold a losing position "
            "from this memory alone; require current confirmation and invalidation"
        )
        contract = build_next_round_memory_contract(
            memory_type="loss_template_observation",
            maturity_state="candidate_observation",
            status="candidate",
            scope={
                "ticker": ticker,
                "sector": sector,
                "side": side,
                "signal_template": template,
                "horizon_class": horizon,
                "market_regime": regime,
            },
            usable_memory=[
                hypothesis_text,
                f"loss_examples={len(rows)}; cumulative_pnl={_safe_float(summary.get('total_pnl')):.0f}",
            ],
            data_focus=data_focus,
            analysis_strategy_updates=[
                "Before issuing the same direction/template, verify which data fields actually confirm it today.",
                "Treat analyst conflict, stale data, horizon mismatch, and missing invalidation as questions to resolve, not as automatic vetoes.",
                "Check whether the current market state differs enough to invalidate this loss observation.",
            ],
            trading_strategy_updates=[
                "PM may use this only to demand clearer current evidence, trigger, and invalidation for comparable setups.",
                "This candidate memory cannot authorize position_match, add-on sizing, or continued losing exposure by itself.",
            ],
            pm_action_conditions=[
                "If today's same-scope setup repeats the weak data mix and lacks a valid trigger/invalidation, PM should prefer probe/observe/reduce logic.",
                "If today's evidence clearly contradicts the old loss pattern, PM should record the contradiction instead of mechanically suppressing the trade.",
            ],
            invalidates_when=[
                "Future same-scope samples show positive expectancy.",
                "Today's data mix, market regime, or horizon differs from the remembered loss template.",
                "A current trigger and explicit invalidation boundary are present and confirmed by market data.",
            ],
            validation_plan=[
                "Track future same-scope trades and no-trade shadows before promoting, weakening, or discarding this observation.",
            ],
            position_authority="analysis_or_watchlist_only",
            max_position_impact="no_direct_position_impact",
            sample_count=len(rows),
            confidence_score=min(0.75, 0.35 + 0.10 * len(rows)),
        )
        evidence = {
            "agent_name": "researcher",
            "observation_only": True,
            "samples": len(rows),
            "summary": summary,
            "loss_examples": loss_examples,
            "data_usage_summary": data_usage,
            "analyst_payloads": analyst_payloads,
            "focus_tickers": sorted(focus_tickers),
        }
        action = {
            "hypothesis_text": hypothesis_text,
            "suggested_use": suggested_use,
            "data_focus": data_focus,
            "position_authority": "analysis_or_watchlist_only",
            "max_position_impact": "no_direct_position_impact",
            CONTRACT_KEY: contract,
        }
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="loss_template_observation",
            scope_type="research",
            scope_key=f"{ticker}:{side}:{template}:{horizon}:{regime}",
            evidence=evidence,
            action=action,
            status="candidate",
        )
        payload = {
            **evidence,
            **action,
            "source_event_id": event_id,
            "hard_constraints": {
                "observation_only": True,
                "candidate_memory_cannot_control_position": True,
                "no_product_blacklist": True,
                "requires_current_confirmation": True,
            },
        }
        hypothesis_id = str(uuid.uuid4())
        payload_ext = externalize_json_for_db(
            payload,
            category="exploratory_hypothesis",
            record_id=hypothesis_id,
            field_name="payload",
            config_id=config_id,
            trading_date=trading_date,
        )
        cursor.execute(
            """
            INSERT INTO exploratory_hypothesis (
                id, config_id, trading_date, scope_type, scope_key, ticker, sector,
                side, horizon_class, market_regime, hypothesis_text,
                evidence_summary, suggested_use, confidence_score, sample_count,
                status, created_at, valid_until, payload_json,
                payload_artifact_path, payload_sha256, payload_size, payload_summary_json
            ) VALUES (?, ?, ?, 'research', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hypothesis_id,
                config_id,
                trading_date,
                f"{ticker}:{side}:{template}:{horizon}:{regime}",
                ticker,
                sector,
                side,
                horizon,
                regime,
                hypothesis_text,
                (
                    f"samples={len(rows)}, net_pnl={_safe_float(summary.get('total_pnl')):.0f}; "
                    f"data_focus={'; '.join(data_focus[:3])}"
                )[:500],
                suggested_use,
                min(0.75, 0.35 + 0.10 * len(rows)),
                len(rows),
                now,
                valid_until,
                payload_ext.inline_value,
                payload_ext.artifact_path,
                payload_ext.sha256,
                payload_ext.size_bytes,
                payload_ext.summary_json,
            ),
        )
        inserted += 1
    return inserted


def _candidate_side_from_snapshot(snapshot: Dict[str, Any]) -> str:
    neutral_observations = _neutral_opportunity_observations(snapshot)
    side_votes = Counter(
        str(item.get("shadow_side") or "flat")
        for item in neutral_observations
        if item.get("shadow_side") in {"long", "short"}
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
    plan = _pre_open_plan(snapshot)
    side = str(plan.get("signal_direction") or plan.get("target_side") or "").lower()
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


def _write_no_trade_opportunity_memory(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    strategy_recommendations: List[Dict[str, Any]],
) -> int:
    learning_cfg = cfg.get("learning", {}) or {}
    no_trade_cfg = learning_cfg.get("no_trade_opportunity_memory", {}) or {}
    if not bool(no_trade_cfg.get("enabled", True)):
        return 0
    max_rows = int(no_trade_cfg.get("max_rows_per_day", 30) or 30)
    now = _utc_now()
    rows = 0
    category_counter: Counter = Counter()
    for recommendation in strategy_recommendations:
        if rows >= max_rows:
            break
        snapshot = _recommendation_snapshot(recommendation)
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "").upper()
        if not ticker:
            continue
        lots = _safe_int(recommendation.get("lots"), 0)
        action = str(recommendation.get("action") or "").lower()
        plan = _pre_open_plan(snapshot)
        execution_result = _execution_result_from_snapshot(snapshot)
        execution_no_trade_reason = normalize_no_trade_reason(execution_result.get("no_trade_reason"))
        limit_locked_execution = execution_no_trade_reason == "limit_locked_no_fill"
        inferred_no_trade_reason = infer_no_trade_reason(snapshot, recommendation.get("warning_message"))
        reason = str(
            execution_no_trade_reason
            or inferred_no_trade_reason
            or plan.get("tradable_lots_reason")
            or ((plan.get("rebalance_summary") or {}) if isinstance(plan.get("rebalance_summary"), dict) else {}).get("reason")
            or recommendation.get("warning_message")
            or ""
        )
        normalized_reason = normalize_no_trade_reason(reason) or "unknown"
        no_trade_category = categorize_no_trade_reason(normalized_reason)
        if lots > 0 and action not in {"hold", "none"} and not limit_locked_execution:
            continue
        side = _candidate_side_from_snapshot(snapshot)
        if side not in {"long", "short"} and limit_locked_execution:
            side = _candidate_side_from_action(action)
        if side not in {"long", "short"}:
            continue
        neutral_observations = _neutral_opportunity_observations(snapshot)
        combo = _signal_combo_from_snapshot(snapshot)
        template = _signal_template(side, combo, snapshot)
        horizon = _horizon_class(_expected_horizon_days(snapshot, side), snapshot)
        regime = _market_regime(snapshot)
        sector = _sector_for_ticker(cfg, ticker)
        shadow_entry_price = _safe_float(
            recommendation.get("base_price")
            or recommendation.get("execution_price")
            or recommendation.get("open_price")
            or recommendation.get("prev_close_price"),
            0.0,
        )
        if shadow_entry_price <= 0:
            continue
        candidate_lots = max(1, abs(lots))
        shadow_lots = 1
        auditor_payload = plan.get("trade_auditor") if isinstance(plan.get("trade_auditor"), dict) else {}
        data_usage = data_usage_from_snapshot(snapshot)
        data_usage_notes = compact_data_usage_notes(data_usage)
        market_rule_block = _market_rule_block_from_snapshot(snapshot)
        limit_lock_audit = (
            market_rule_block.get("limit_lock")
            if isinstance(market_rule_block.get("limit_lock"), dict)
            else {}
        )
        usable_prefix = f"skipped_or_neutral_candidate reason={reason or recommendation.get('warning_message') or 'unknown'}"
        execution_timing_updates = []
        execution_strategy_updates = []
        validation_updates = []
        if limit_locked_execution:
            limit_price = limit_lock_audit.get("limit_price") or limit_lock_audit.get("limit_up") or limit_lock_audit.get("limit_down")
            execution_price = limit_lock_audit.get("execution_price") or recommendation.get("execution_price")
            usable_prefix = (
                "limit_locked_no_fill timing_case "
                f"action={action}, lots={lots}, execution_price={execution_price}, limit_price={limit_price}"
            )
            execution_timing_updates = [
                "Treat repeated limit-locked skips as an entry/exit timing research question, not as a direction rule.",
                "Compare whether earlier intraday confirmation, pullback entry, or avoid-chasing logic would have preserved the same setup without touching the limit price.",
            ]
            execution_strategy_updates = [
                "Do not chase at the limit price; next trade still needs current confirmation, explicit invalidation, and feasible execution basis.",
                "Only promote a timing adjustment after forward shadow results show same-scope missed alpha or avoided loss.",
            ]
            validation_updates = [
                "Backfill no-trade shadow windows to test whether the limit-locked skipped trade was a real missed alpha or a correctly avoided unfilled order.",
            ]
        payload = {
            "recommendation_id": recommendation.get("id"),
            "signal_snapshot": snapshot,
            "trade_research_contract_summary": _opportunity_contract_summary(snapshot),
            "data_usage_summary": data_usage,
            "data_usage_notes": data_usage_notes,
            "pre_open_plan": plan,
            "action": recommendation.get("action"),
            "lots": lots,
            "candidate_side": side,
            "neutral_opportunity_observations": neutral_observations,
            "shadow_entry_price": shadow_entry_price,
            "no_trade_reason": normalized_reason,
            "no_trade_reason_category": no_trade_category,
            "execution_no_trade_reason": execution_no_trade_reason,
            "market_rule_block": market_rule_block,
            "limit_lock_audit": limit_lock_audit,
            "created_from": "phase4_no_trade_opportunity_memory",
            "tracking_boundary": (
                "Neutral, skipped, and limit-locked opportunities are tracking-only; they cannot authorize sizing, "
                "add-on trades, or losing-position holds without current confirmation and invalidation."
            ),
        }
        payload = attach_next_round_memory_contract(
            payload,
            memory_type="no_trade_opportunity_memory",
            maturity_state="shadow_tracking",
            scope={
                "ticker": ticker,
                "sector": sector,
                "side": side,
                "signal_template": template,
                "horizon_class": horizon,
                "market_regime": regime,
            },
            usable_memory=[
                usable_prefix,
                (
                    "no_trade_category="
                    f"{no_trade_category['category_label']}({no_trade_category['category']}): "
                    f"{no_trade_category['category_description']}"
                ),
                "; ".join(
                    f"{item.get('analyst')}:{item.get('bucket')} trigger={item.get('trigger_condition')}"
                    for item in neutral_observations[:3]
                ),
                *data_usage_notes[:3],
            ],
            analysis_strategy_updates=[
                _no_trade_category_strategy_note(no_trade_category["category"]),
                *execution_timing_updates,
                "Treat skipped opportunities as watchlist questions: what evidence would have made them tradable?",
                "Use forward shadow results to distinguish reasonable avoidance from missed opportunity only after settlement.",
            ],
            trading_strategy_updates=[
                *execution_strategy_updates,
                "Do not convert a skipped or Neutral opportunity into a trade unless the current trigger, market confirmation, and invalidation are explicit.",
                "If future shadow results validate repeated missed opportunities, promote only through same-scope validation.",
            ],
            validation_plan=[
                *validation_updates,
                "Backfill configured forward shadow windows and compare same-scope outcomes before promotion.",
            ],
        )
        memory_id = str(uuid.uuid4())
        payload_ext = externalize_json_for_db(
            payload,
            category="no_trade_opportunity_memory",
            record_id=memory_id,
            field_name="payload",
            config_id=config_id,
            trading_date=trading_date,
        )
        cursor.execute(
            '''
            INSERT INTO no_trade_opportunity_memory (
                id, config_id, trading_date, ticker, side, sector, signal_template,
                signal_combo, horizon_class, market_regime, opportunity_type,
                opportunity_layer, candidate_lots, shadow_lots, shadow_entry_price,
                pm_reason, auditor_reason, execution_reason, evidence_summary,
                status, classification, shadow_results_json, payload_json,
                payload_artifact_path, payload_sha256, payload_size,
                payload_summary_json, created_at, last_reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_id, trading_date, ticker, side, signal_template)
            DO UPDATE SET
                sector=excluded.sector,
                signal_combo=excluded.signal_combo,
                horizon_class=excluded.horizon_class,
                market_regime=excluded.market_regime,
                opportunity_type=excluded.opportunity_type,
                opportunity_layer=excluded.opportunity_layer,
                shadow_entry_price=excluded.shadow_entry_price,
                pm_reason=excluded.pm_reason,
                auditor_reason=excluded.auditor_reason,
                execution_reason=excluded.execution_reason,
                evidence_summary=excluded.evidence_summary,
                payload_json=excluded.payload_json,
                payload_artifact_path=excluded.payload_artifact_path,
                payload_sha256=excluded.payload_sha256,
                payload_size=excluded.payload_size,
                payload_summary_json=excluded.payload_summary_json,
                last_reviewed_at=excluded.last_reviewed_at
            ''',
            (
                memory_id,
                config_id,
                trading_date,
                ticker,
                side,
                sector,
                template,
                _json_dumps(combo),
                horizon,
                regime,
                _primary_opportunity_type(snapshot),
                _primary_opportunity_layer(snapshot),
                candidate_lots,
                shadow_lots,
                shadow_entry_price,
                reason,
                "; ".join(auditor_payload.get("reasons") or []) if isinstance(auditor_payload, dict) else "",
                str(execution_no_trade_reason or normalized_reason or recommendation.get("warning_message") or ""),
                (
                    (
                        "no_trade_category="
                        f"{no_trade_category['category_label']}:{no_trade_category['category']}; "
                    )
                    + _evidence_summary(snapshot)
                    + (
                        "; neutral_condition="
                        + "; ".join(
                            f"{item.get('analyst')}:{item.get('bucket')}:{item.get('trigger_condition')}"
                            for item in neutral_observations[:3]
                        )
                        if neutral_observations
                        else ""
                    )
                    + (
                        f"; execution_timing_case=limit_locked_no_fill action={action}"
                        if limit_locked_execution
                        else ""
                    )
                )[:500],
                _json_dumps([]),
                payload_ext.inline_value,
                payload_ext.artifact_path,
                payload_ext.sha256,
                payload_ext.size_bytes,
                payload_ext.summary_json,
                now,
                now,
            ),
        )
        rows += 1
        category_counter[no_trade_category["category"]] += 1
    if rows:
        _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="no_trade_opportunity_memory",
            scope_type="daily",
            scope_key=trading_date,
            evidence={
                "strategy_recommendations": len(strategy_recommendations),
                "no_trade_reason_categories": _sorted_counter_dict(category_counter),
            },
            action={"memory_rows": rows, "no_trade_reason_categories": _sorted_counter_dict(category_counter)},
            status="applied",
        )
    return rows


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


def _backfill_no_trade_opportunity_shadow_results(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    learning_cfg = cfg.get("learning", {}) or {}
    no_trade_cfg = learning_cfg.get("no_trade_opportunity_memory", {}) or {}
    if not bool(no_trade_cfg.get("enabled", True)):
        return {"updated_rows": 0, "status": "disabled"}
    horizons = sorted({int(item) for item in (no_trade_cfg.get("shadow_forward_days") or [3, 5, 10]) if int(item) > 0})
    if not horizons:
        return {"updated_rows": 0, "status": "no_horizons"}
    cursor.execute(
        '''
        SELECT *
        FROM no_trade_opportunity_memory
        WHERE config_id = ?
          AND status = 'open'
          AND trading_date < ?
        ORDER BY trading_date, ticker
        ''',
        (config_id, trading_date),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    updated = 0
    now = _utc_now()
    for row in rows:
        memory_date = str(row.get("trading_date") or "")[:10]
        settled_days = _settled_trading_days(cursor, config_id, memory_date, trading_date)
        if not settled_days:
            continue
        existing_results = _json_loads(row.get("shadow_results_json")) or []
        existing_by_horizon = {
            int(item.get("horizon_days") or 0): item
            for item in existing_results
            if isinstance(item, dict)
        }
        entry_price = _safe_float(row.get("shadow_entry_price"), 0.0)
        if entry_price <= 0:
            continue
        new_results = list(existing_by_horizon.values())
        for horizon in horizons:
            if horizon in existing_by_horizon or len(settled_days) < horizon:
                continue
            exit_date = settled_days[horizon - 1]
            exit_price = _ticker_base_price_on_day(cursor, config_id, str(row.get("ticker") or "").upper(), exit_date)
            if exit_price <= 0:
                continue
            try:
                multiplier_info = FuturesContractInfoCache.get_contract_info(str(row.get("ticker") or "").upper())
                multiplier = _safe_float((multiplier_info or {}).get("contract_multiplier"), 1.0)
            except Exception:
                multiplier = 1.0
            direction = 1.0 if str(row.get("side") or "").lower() == "long" else -1.0
            shadow_pnl = (exit_price - entry_price) * direction * multiplier * max(1, _safe_int(row.get("shadow_lots"), 1))
            new_results.append(
                {
                    "horizon_days": horizon,
                    "entry_date": memory_date,
                    "exit_date": exit_date,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "shadow_pnl": shadow_pnl,
                    "shadow_return": ((exit_price - entry_price) * direction / entry_price) if entry_price else 0.0,
                    "price_source": "future_recommendation_base_price",
                }
            )
        if len(new_results) == len(existing_results):
            continue
        completed_horizons = {int(item.get("horizon_days") or 0) for item in new_results if isinstance(item, dict)}
        classification = str(row.get("classification") or "pending")
        if completed_horizons:
            max_horizon = max(completed_horizons)
            latest = next((item for item in new_results if int(item.get("horizon_days") or 0) == max_horizon), None)
            pnl = _safe_float((latest or {}).get("shadow_pnl"))
            classification = "missed_opportunity" if pnl > 0 else "correct_avoidance" if pnl < 0 else "unresolved"
        status = "closed" if all(horizon in completed_horizons for horizon in horizons) else "open"
        cursor.execute(
            '''
            UPDATE no_trade_opportunity_memory
            SET shadow_results_json = ?,
                classification = ?,
                status = ?,
                last_reviewed_at = ?
            WHERE id = ?
            ''',
            (_json_dumps(sorted(new_results, key=lambda item: int(item.get("horizon_days") or 0))), classification, status, now, row.get("id")),
        )
        updated += 1
    if updated:
        _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="no_trade_shadow_backfill",
            scope_type="daily",
            scope_key=trading_date,
            evidence={"candidate_rows": len(rows), "horizons": horizons},
            action={"updated_rows": updated},
            status="applied",
        )
    return {"updated_rows": updated, "status": "applied" if updated else "no_ready_rows", "horizons": horizons}


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
            payload = _policy_contract_payload(
                policy_type="causal_review_rule",
                policy_action=decision["policy_action"],
                reason=decision["reason"],
                multiplier=decision["multiplier"],
                maturity_state="validated_causal_policy",
                scope={
                    "ticker": ticker,
                    "side": side,
                    "signal_template": template,
                    "horizon_class": horizon,
                    "market_regime": regime,
                },
                evidence={
                    **payload,
                    "sample_count": _safe_int(summary.get("total_trades")),
                    "win_rate": _safe_float(summary.get("win_rate")),
                    "net_pnl": _safe_float(summary.get("total_pnl")),
                    "confidence_score": confidence,
                },
            )
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


def _learned_effect_underperformance_groups(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    min_samples: int,
    min_gap: float,
    allow_self_loss_demote_without_benchmark: bool = True,
    min_self_loss_net_pnl: float = -1000.0,
) -> List[Dict[str, Any]]:
    try:
        pairs = _completed_pairs_up_to(cursor, config_id=config_id, trading_date=trading_date)
    except sqlite3.Error:
        return []
    recommendation_lookup = _recommendations_by_id(
        cursor,
        [pair.get("open_recommendation_id") for pair in pairs if pair.get("open_recommendation_id")],
    )
    groups: Dict[Tuple[str, str, str, str, str, str], Dict[str, List[Dict[str, Any]]]] = defaultdict(
        lambda: {"learned_effect": [], "benchmark": []}
    )
    tracked_effects = ("alpha_release", "risk_suppression", "evidence_rejection")
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
        key = (ticker, side, template, horizon, regime)
        item = dict(pair)
        item["signal_template"] = template
        item["signal_combo"] = combo
        if recommendation:
            tags, effects = _learning_attribution_from_recommendation(recommendation)
            item["learning_tags"] = tags
            item["learning_effects"] = effects
            scoped_effects = [str(effect) for effect in effects or [] if effect in tracked_effects]
            if tags and scoped_effects:
                for effect in scoped_effects:
                    groups[(*key, effect)]["learned_effect"].append(item)
                continue
        for effect in tracked_effects:
            groups[(*key, effect)]["benchmark"].append(item)

    underperforming: List[Dict[str, Any]] = []
    for (ticker, side, template, horizon, regime, effect), rows in groups.items():
        learned_rows = rows["learned_effect"]
        benchmark_rows = rows["benchmark"]
        learned_summary = _trade_pair_performance_summary(learned_rows)
        benchmark_summary = _trade_pair_performance_summary(benchmark_rows)
        learned_trades = _safe_int(learned_summary.get("total_trades"))
        benchmark_trades = _safe_int(benchmark_summary.get("total_trades"))
        learned_pnl = _safe_float(learned_summary.get("net_pnl"))
        benchmark_pnl = _safe_float(benchmark_summary.get("net_pnl"))
        if learned_trades < min_samples:
            continue
        comparison_status = "same_scope_benchmark_underperformed"
        if benchmark_trades < min_samples:
            if not allow_self_loss_demote_without_benchmark:
                continue
            if learned_pnl > min_self_loss_net_pnl:
                continue
            comparison_status = "same_scope_self_loss_without_benchmark"
        elif learned_pnl + min_gap >= benchmark_pnl:
            continue
        underperforming.append(
            {
                "ticker": ticker,
                "side": side,
                "signal_template": template,
                "horizon_class": horizon,
                "market_regime": regime,
                "learning_effect": effect,
                "comparison_status": comparison_status,
                "learned_effect": learned_summary,
                "benchmark": benchmark_summary,
                "learned_effect_trades": learned_trades,
                "benchmark_trades": benchmark_trades,
                "learned_effect_net_pnl": learned_pnl,
                "benchmark_net_pnl": benchmark_pnl,
            }
        )
    underperforming.sort(
        key=lambda item: (
            _safe_float(item.get("learned_effect_net_pnl")) - _safe_float(item.get("benchmark_net_pnl")),
            -_safe_int(item.get("learned_effect_trades")),
        )
    )
    return underperforming


def _write_learned_vs_unlearned_policy_state(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    learning_cfg = cfg.get("learning", {}) or {}
    control = learning_cfg.get("learned_vs_unlearned_policy", {}) or {}
    if not bool(control.get("enabled", True)):
        return {"rows": 0, "status": "disabled"}

    summary = _learned_vs_unlearned_trade_performance(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
    )
    learned = summary.get("learned") or {}
    unlearned = summary.get("unlearned") or {}
    min_samples = _safe_int(control.get("min_learned_samples"), 3)
    min_gap = _safe_float(control.get("min_net_pnl_underperformance"), 1.0)
    learned_trades = _safe_int(learned.get("total_trades"))
    unlearned_trades = _safe_int(unlearned.get("total_trades"))
    learned_pnl = _safe_float(learned.get("net_pnl"))
    unlearned_pnl = _safe_float(unlearned.get("net_pnl"))
    scoped_groups = _learned_effect_underperformance_groups(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        min_samples=_safe_int(control.get("min_scoped_alpha_samples"), min_samples),
        min_gap=min_gap,
        allow_self_loss_demote_without_benchmark=bool(
            control.get("allow_self_loss_demote_without_benchmark", True)
        ),
        min_self_loss_net_pnl=_safe_float(
            control.get("min_self_loss_net_pnl"),
            -1000.0,
        ),
    )
    if (learned_trades < min_samples or unlearned_trades < min_samples) and not scoped_groups:
        return {
            "rows": 0,
            "status": "insufficient_samples",
            "learned_trades": learned_trades,
            "unlearned_trades": unlearned_trades,
            "min_samples": min_samples,
        }
    if learned_pnl + min_gap >= unlearned_pnl and not scoped_groups:
        return {
            "rows": 0,
            "status": "benchmark_not_underperformed",
            "learned_net_pnl": learned_pnl,
            "unlearned_net_pnl": unlearned_pnl,
        }

    valid_until = _valid_until(trading_date, int(learning_cfg.get("memory_expires_after_days", 30)))
    now = _utc_now()
    event_id = _insert_learning_event(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        event_type="learned_vs_unlearned_policy",
        scope_type="global",
        scope_key="learned_underperformance",
        evidence=summary,
        action={
            "policy_action": "diagnostic_only" if not scoped_groups else "demote_scoped",
            "multiplier": _safe_float(control.get("demote_multiplier"), 0.50),
            "reason": "learned effect scope underperformed benchmark"
            if scoped_groups
            else "learned trades underperformed unlearned benchmark",
            "scoped_group_count": len(scoped_groups),
        },
        status="diagnostic" if not scoped_groups else "applied",
    )
    inserted = 0
    max_scoped_rows = max(1, _safe_int(control.get("max_scoped_demote_rows"), 20))
    for group in scoped_groups[:max_scoped_rows]:
        learned_effect_trades = _safe_int(group.get("learned_effect_trades"))
        benchmark_trades = _safe_int(group.get("benchmark_trades"))
        confidence = min(1.0, learned_effect_trades / max(learned_effect_trades + benchmark_trades, 1))
        learning_effect = str(group.get("learning_effect") or "learned_effect")
        payload = _policy_contract_payload(
            policy_type="learned_vs_unlearned",
            policy_action="demote",
            reason=f"learned {learning_effect} trades underperformed same-scope benchmark",
            multiplier=_safe_float(control.get("demote_multiplier"), 0.50),
            maturity_state="scoped_demote_policy",
            scope={
                "ticker": group.get("ticker") or "*",
                "side": group.get("side") or "*",
                "signal_template": group.get("signal_template") or "*",
                "horizon_class": group.get("horizon_class") or "*",
                "market_regime": group.get("market_regime") or "*",
            },
            evidence={
                **summary,
                "scoped_underperformance": group,
                "global_demote_is_diagnostic_only": True,
                "learning_effect_scope": learning_effect,
                "sample_count": learned_effect_trades,
                "confidence_score": confidence,
            },
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
                group.get("ticker") or "*",
                group.get("side") or "*",
                group.get("signal_template") or "*",
                group.get("horizon_class") or "*",
                group.get("market_regime") or "*",
                "learned_vs_unlearned",
                "demote",
                _safe_float(control.get("demote_multiplier"), 0.50),
                confidence,
                learned_effect_trades,
                f"learned {learning_effect} trades underperformed same-scope benchmark",
                event_id,
                now,
                valid_until,
                _json_dumps(payload),
            ),
        )
        inserted += 1
    return {
        "rows": inserted,
        "status": "scoped_demote_applied" if inserted else "global_underperformance_diagnostic_only",
        "learned_net_pnl": learned_pnl,
        "unlearned_net_pnl": unlearned_pnl,
        "learned_trades": learned_trades,
        "unlearned_trades": unlearned_trades,
        "scoped_group_count": len(scoped_groups),
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
        policy_payload = _policy_contract_payload(
            policy_type="template_quality",
            policy_action=action,
            reason=reason,
            multiplier=multiplier,
            maturity_state="validated_template_policy",
            scope={
                "ticker": row.get("ticker"),
                "side": row.get("side"),
                "signal_template": row.get("signal_template"),
                "horizon_class": row.get("horizon_class"),
                "market_regime": row.get("market_regime"),
            },
            evidence={
                **row,
                "sample_count": sample_count,
                "win_rate": win_rate,
                "net_pnl": net_pnl,
                "confidence_score": confidence,
            },
        )
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="adaptive_policy_state",
            scope_type="template",
            scope_key=f"{row.get('ticker')}:{row.get('side')}:{row.get('signal_template')}",
            evidence=dict(row),
            action={"policy_action": action, "multiplier": multiplier, "reason": reason, CONTRACT_KEY: policy_payload[CONTRACT_KEY]},
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
                _json_dumps(policy_payload),
            ),
        )
        count += 1
    return count


def _write_tail_loss_sentinel_state(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    cfg: Dict[str, Any],
) -> int:
    learning_cfg = cfg.get("learning", {}) or {}
    sentinel_cfg = learning_cfg.get("tail_loss_sentinel", {}) or {}
    if not bool(sentinel_cfg.get("enabled", True)):
        return 0
    min_abs_loss = abs(_safe_float(sentinel_cfg.get("min_abs_loss"), 25000.0))
    min_equity_loss_ratio = abs(_safe_float(sentinel_cfg.get("min_equity_loss_ratio"), 0.005))
    valid_days = int(sentinel_cfg.get("valid_days", 5) or 5)
    multiplier = max(0.0, min(1.0, _safe_float(sentinel_cfg.get("cap_multiplier"), 0.35)))
    policy_action = str(sentinel_cfg.get("policy_action") or "cap")
    cursor.execute(
        '''
        SELECT current_account_equity, current_balance
        FROM daily_settlement ds
        JOIN portfolio p ON ds.portfolio_id = p.id
        WHERE p.config_id = ?
          AND substr(ds.trading_date, 1, 10) = ?
        ORDER BY ds.created_at DESC
        LIMIT 1
        ''',
        (config_id, trading_date),
    )
    settlement = cursor.fetchone()
    equity = _safe_float(settlement["current_account_equity"] if settlement else 0.0) or _safe_float(
        settlement["current_balance"] if settlement else 0.0,
        0.0,
    )
    loss_threshold = max(min_abs_loss, equity * min_equity_loss_ratio if equity > 0 else min_abs_loss)
    cursor.execute(
        '''
        SELECT tdp.*, p.config_id
        FROM ticker_daily_pnl tdp
        JOIN portfolio p ON tdp.portfolio_id = p.id
        WHERE p.config_id = ?
          AND substr(tdp.trading_date, 1, 10) = ?
          AND (tdp.new_position_pnl <= ? OR tdp.daily_pnl <= ?)
        ''',
        (config_id, trading_date, -loss_threshold, -loss_threshold),
    )
    pnl_rows = [dict(row) for row in cursor.fetchall()]
    if not pnl_rows:
        return 0

    cursor.execute(
        '''
        SELECT *
        FROM futures_recommendation
        WHERE config_id = ?
          AND substr(effective_trade_date, 1, 10) = ?
          AND source_type = ?
        ''',
        (config_id, trading_date, RecommendationSourceType.STRATEGY.value),
    )
    recs_by_ticker = {str(row["underlying_code"] or "").upper(): dict(row) for row in cursor.fetchall()}
    now = _utc_now()
    valid_until = _valid_until(trading_date, valid_days)
    inserted = 0
    for pnl_row in pnl_rows:
        ticker = str(pnl_row.get("ticker") or "").upper()
        recommendation = recs_by_ticker.get(ticker) or {}
        snapshot = _recommendation_snapshot(recommendation)
        side = _recommendation_side(recommendation, snapshot)
        if side not in {"long", "short"}:
            position_type = str(pnl_row.get("position_type") or "").lower()
            side = "short" if "short" in position_type else "long"
        combo = _signal_combo_from_snapshot(snapshot)
        template = _signal_template(side, combo, snapshot)
        horizon = _horizon_class(_expected_horizon_days(snapshot, side), snapshot)
        regime = _market_regime(snapshot)
        evidence = {
            "ticker_daily_pnl": pnl_row,
            "loss_threshold": loss_threshold,
            "recommendation_id": recommendation.get("id"),
            "trade_research_contract_summary": _opportunity_contract_summary(snapshot),
        }
        policy_payload = _policy_contract_payload(
            policy_type="tail_loss_sentinel",
            policy_action=policy_action,
            reason=f"tail loss sentinel: new/daily pnl breached {loss_threshold:.0f}",
            multiplier=multiplier,
            maturity_state="short_lived_risk_sentinel",
            scope={
                "ticker": ticker,
                "side": side,
                "signal_template": template,
                "horizon_class": horizon,
                "market_regime": regime,
            },
            evidence={
                **evidence,
                "sample_count": 1,
                "confidence_score": _safe_float(sentinel_cfg.get("confidence_score"), 0.70),
            },
        )
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="tail_loss_sentinel",
            scope_type="ticker_side_template",
            scope_key=f"{ticker}:{side}:{template}",
            evidence=evidence,
            action={
                "policy_action": policy_action,
                "multiplier": multiplier,
                "valid_until": valid_until,
                CONTRACT_KEY: policy_payload[CONTRACT_KEY],
            },
            status="applied",
        )
        cursor.execute(
            '''
            INSERT INTO adaptive_policy_state (
                id, config_id, ticker, side, signal_template, horizon_class, market_regime,
                policy_type, policy_action, multiplier, confidence_score, sample_count,
                reason, source_event_id, created_at, valid_until, payload_json, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'tail_loss_sentinel', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
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
                ticker,
                side,
                template,
                horizon,
                regime,
                policy_action,
                multiplier,
                _safe_float(sentinel_cfg.get("confidence_score"), 0.70),
                1,
                f"tail loss sentinel: new/daily pnl breached {loss_threshold:.0f}",
                event_id,
                now,
                valid_until,
                _json_dumps(policy_payload),
            ),
        )
        inserted += 1
    return inserted


def _write_alpha_promotion_state(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    cfg: Dict[str, Any],
) -> int:
    learning_cfg = cfg.get("learning", {}) or {}
    alpha_cfg = learning_cfg.get("alpha_promotion", {}) or {}
    if not bool(alpha_cfg.get("enabled", True)):
        return 0
    min_samples = int(alpha_cfg.get("min_sample_count", 5) or 5)
    min_win_rate = _safe_float(alpha_cfg.get("min_win_rate"), 0.60)
    min_net_pnl = _safe_float(alpha_cfg.get("min_net_pnl"), 1000.0)
    valid_days = int(alpha_cfg.get("valid_days", 10) or 10)
    cursor.execute(
        '''
        SELECT *
        FROM signal_template_performance
        WHERE config_id = ?
          AND sample_count >= ?
          AND win_rate >= ?
          AND net_pnl >= ?
        ORDER BY confidence_score DESC, net_pnl DESC, sample_count DESC
        ''',
        (config_id, min_samples, min_win_rate, min_net_pnl),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    now = _utc_now()
    valid_until = _valid_until(trading_date, valid_days)
    inserted = 0
    for row in rows:
        evidence = {
            "source": "signal_template_performance",
            "sample_count": _safe_int(row.get("sample_count")),
            "win_rate": _safe_float(row.get("win_rate")),
            "net_pnl": _safe_float(row.get("net_pnl")),
            "avg_pnl": _safe_float(row.get("avg_pnl")),
            "confidence_score": _safe_float(row.get("confidence_score")),
        }
        policy_payload = _policy_contract_payload(
            policy_type="alpha_promotion",
            policy_action="protect",
            reason="positive alpha promotion from verified template performance",
            multiplier=1.0,
            maturity_state="verified_alpha_memory",
            scope={
                "ticker": row.get("ticker"),
                "side": row.get("side"),
                "signal_template": row.get("signal_template"),
                "horizon_class": row.get("horizon_class"),
                "market_regime": row.get("market_regime"),
            },
            evidence=evidence,
        )
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="alpha_promotion",
            scope_type="ticker_side_template",
            scope_key=f"{row.get('ticker')}:{row.get('side')}:{row.get('signal_template')}",
            evidence={**row, **evidence},
            action={
                "policy_action": "protect",
                "multiplier": 1.0,
                "valid_until": valid_until,
                CONTRACT_KEY: policy_payload[CONTRACT_KEY],
            },
            status="applied",
        )
        cursor.execute(
            '''
            INSERT INTO adaptive_policy_state (
                id, config_id, ticker, side, signal_template, horizon_class, market_regime,
                policy_type, policy_action, multiplier, confidence_score, sample_count,
                reason, source_event_id, created_at, valid_until, payload_json, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'alpha_promotion', 'protect', 1.0, ?, ?, ?, ?, ?, ?, ?, 1)
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
                _safe_float(row.get("confidence_score"), 0.60),
                _safe_int(row.get("sample_count")),
                "positive alpha promotion from verified template performance",
                event_id,
                now,
                valid_until,
                _json_dumps(policy_payload),
            ),
        )
        inserted += 1

    shadow_min_pnl = _safe_float(alpha_cfg.get("min_shadow_pnl"), min_net_pnl)
    cursor.execute(
        '''
        SELECT *
        FROM no_trade_opportunity_memory
        WHERE config_id = ?
          AND classification = 'missed_opportunity'
          AND shadow_results_json IS NOT NULL
        ORDER BY trading_date DESC
        LIMIT 200
        ''',
        (config_id,),
    )
    shadow_groups: Dict[Tuple[str, str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in cursor.fetchall():
        item = dict(row)
        results = _json_loads(item.get("shadow_results_json")) or []
        best_pnl = max([_safe_float(result.get("shadow_pnl")) for result in results if isinstance(result, dict)] or [0.0])
        if best_pnl < shadow_min_pnl:
            continue
        shadow_groups[
            (
                str(item.get("ticker") or "*"),
                str(item.get("side") or "*"),
                str(item.get("signal_template") or "*"),
                str(item.get("horizon_class") or "*"),
                str(item.get("market_regime") or "*"),
            )
        ].append({**item, "best_shadow_pnl": best_pnl})
    for (ticker, side, template, horizon, regime), items in shadow_groups.items():
        if len(items) < min_samples:
            continue
        net_shadow = sum(_safe_float(item.get("best_shadow_pnl")) for item in items)
        if net_shadow < min_net_pnl:
            continue
        confidence = min(0.90, 0.45 + len(items) / 20.0 + min(0.20, net_shadow / 50000.0))
        evidence = {
            "source": "no_trade_shadow_results",
            "sample_count": len(items),
            "net_shadow_pnl": net_shadow,
            "shadow_memory_ids": [item.get("id") for item in items[:20]],
            "confidence_score": confidence,
        }
        policy_payload = _policy_contract_payload(
            policy_type="alpha_promotion",
            policy_action="protect",
            reason="positive alpha promotion from missed-opportunity shadow results",
            multiplier=1.0,
            maturity_state="validated_shadow_alpha_memory",
            scope={
                "ticker": ticker,
                "side": side,
                "signal_template": template,
                "horizon_class": horizon,
                "market_regime": regime,
            },
            evidence=evidence,
        )
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="alpha_promotion_shadow",
            scope_type="ticker_side_template",
            scope_key=f"{ticker}:{side}:{template}",
            evidence=evidence,
            action={
                "policy_action": "protect",
                "multiplier": 1.0,
                "valid_until": valid_until,
                CONTRACT_KEY: policy_payload[CONTRACT_KEY],
            },
            status="applied",
        )
        cursor.execute(
            '''
            INSERT INTO adaptive_policy_state (
                id, config_id, ticker, side, signal_template, horizon_class, market_regime,
                policy_type, policy_action, multiplier, confidence_score, sample_count,
                reason, source_event_id, created_at, valid_until, payload_json, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'alpha_promotion', 'protect', 1.0, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(config_id, ticker, side, signal_template, horizon_class, market_regime, policy_type)
            DO UPDATE SET
                policy_action=excluded.policy_action,
                multiplier=excluded.multiplier,
                confidence_score=CASE
                    WHEN adaptive_policy_state.confidence_score > excluded.confidence_score
                    THEN adaptive_policy_state.confidence_score
                    ELSE excluded.confidence_score
                END,
                sample_count=CASE
                    WHEN adaptive_policy_state.sample_count > excluded.sample_count
                    THEN adaptive_policy_state.sample_count
                    ELSE excluded.sample_count
                END,
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
                ticker,
                side,
                template,
                horizon,
                regime,
                confidence,
                len(items),
                "positive alpha promotion from missed-opportunity shadow results",
                event_id,
                now,
                valid_until,
                _json_dumps(policy_payload),
            ),
        )
        inserted += 1
    return inserted


def _contextual_rule_policy_payload(
    *,
    rule_group: str,
    reason: str,
    rules: Dict[str, Any],
    maturity_state: str,
    scope: Dict[str, Any],
    evidence: Dict[str, Any],
    policy_action: str = "calibrate",
    multiplier: float = 1.0,
) -> Dict[str, Any]:
    payload = _policy_contract_payload(
            policy_type=f"contextual_rule_calibration:{rule_group}",
        policy_action=policy_action,
        reason=reason,
        multiplier=multiplier,
        maturity_state=maturity_state,
        scope=scope,
        evidence={
            **(evidence or {}),
            "rule_group": rule_group,
            "rule_adjustments": rules,
        },
    )
    payload["rule_group"] = rule_group
    payload["rule_adjustments"] = {rule_group: rules}
    payload["calibration_boundary"] = (
        "This is a context-scoped weak-parameter adjustment. It cannot override the 20% margin cap, "
        "settlement/accounting checks, no-lookahead gates, limit-lock/expiry business rules, or the need for current evidence."
    )
    return payload


def _insert_contextual_rule_calibration(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    scope: Dict[str, Any],
    rule_group: str,
    rules: Dict[str, Any],
    reason: str,
    evidence: Dict[str, Any],
    confidence_score: float,
    sample_count: int,
    valid_days: int,
    maturity_state: str = "research_calibrated_weak_param",
) -> int:
    if not rules:
        return 0
    ticker = str(scope.get("ticker") or "*").upper()
    side = str(scope.get("side") or "*").lower()
    template = str(scope.get("signal_template") or "*")
    horizon = str(scope.get("horizon_class") or "*")
    regime = str(scope.get("market_regime") or "*")
    payload = _contextual_rule_policy_payload(
        rule_group=rule_group,
        reason=reason,
        rules=rules,
        maturity_state=maturity_state,
        scope={
            "ticker": ticker,
            "side": side,
            "signal_template": template,
            "horizon_class": horizon,
            "market_regime": regime,
        },
        evidence=evidence,
    )
    event_id = _insert_learning_event(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        event_type="contextual_rule_calibration",
        scope_type=f"{rule_group}:{ticker}:{side}:{horizon}",
        scope_key=f"{ticker}:{side}:{template}:{horizon}:{regime}",
        evidence=evidence,
        action={
            "policy_type": f"contextual_rule_calibration:{rule_group}",
            "policy_action": "calibrate",
            "rule_group": rule_group,
            "rule_adjustments": rules,
            CONTRACT_KEY: payload[CONTRACT_KEY],
        },
        status="applied",
    )
    cursor.execute(
        '''
        INSERT INTO adaptive_policy_state (
            id, config_id, ticker, side, signal_template, horizon_class, market_regime,
            policy_type, policy_action, multiplier, confidence_score, sample_count,
            reason, source_event_id, created_at, valid_until, payload_json, active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'calibrate', 1.0, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(config_id, ticker, side, signal_template, horizon_class, market_regime, policy_type)
        DO UPDATE SET
            policy_action=excluded.policy_action,
            multiplier=excluded.multiplier,
            confidence_score=CASE
                WHEN adaptive_policy_state.confidence_score > excluded.confidence_score
                THEN adaptive_policy_state.confidence_score
                ELSE excluded.confidence_score
            END,
            sample_count=CASE
                WHEN adaptive_policy_state.sample_count > excluded.sample_count
                THEN adaptive_policy_state.sample_count
                ELSE excluded.sample_count
            END,
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
            ticker,
            side,
            template,
            horizon,
            regime,
            f"contextual_rule_calibration:{rule_group}",
            max(0.0, min(1.0, _safe_float(confidence_score))),
            max(1, _safe_int(sample_count, 1)),
            reason,
            event_id,
            _utc_now(),
            _valid_until(trading_date, valid_days),
            _json_dumps(payload),
        ),
    )
    return 1


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


def _write_contextual_rule_calibration_state(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    cfg: Dict[str, Any],
    strategy_recommendations: List[Dict[str, Any]],
    no_trade_reason_counter: Counter,
) -> int:
    learning_cfg = cfg.get("learning", {}) or {}
    calibration_cfg = learning_cfg.get("contextual_rule_calibration", {}) or {}
    if not bool(calibration_cfg.get("enabled", True)):
        return 0
    valid_days = int(calibration_cfg.get("valid_days", 10) or 10)
    min_shadow_pnl = _safe_float(calibration_cfg.get("min_shadow_pnl_for_relaxation"), 1200.0)
    min_shadow_loss = abs(_safe_float(calibration_cfg.get("min_shadow_loss_for_tightening"), 1200.0))
    max_rows = int(calibration_cfg.get("max_rows_per_day", 10) or 10)
    inserted = 0

    cursor.execute(
        '''
        SELECT *
        FROM no_trade_opportunity_memory
        WHERE config_id = ?
          AND status = 'closed'
          AND classification IN ('missed_opportunity', 'correct_avoidance')
          AND shadow_results_json IS NOT NULL
        ORDER BY last_reviewed_at DESC, trading_date DESC
        LIMIT ?
        ''',
        (config_id, max_rows * 3),
    )
    for row in cursor.fetchall():
        if inserted >= max_rows:
            break
        item = dict(row)
        results = _json_loads(item.get("shadow_results_json")) or []
        pnl_values = [_safe_float(result.get("shadow_pnl")) for result in results if isinstance(result, dict)]
        if not pnl_values:
            continue
        latest_pnl = pnl_values[-1]
        reason_text = normalize_no_trade_reason(item.get("execution_reason") or item.get("pm_reason") or "")
        category = categorize_no_trade_reason(reason_text)["category"]
        scope = {
            "ticker": item.get("ticker"),
            "side": item.get("side"),
            "signal_template": item.get("signal_template"),
            "horizon_class": item.get("horizon_class"),
            "market_regime": item.get("market_regime"),
        }
        evidence = {
            "source": "no_trade_opportunity_memory_shadow",
            "memory_id": item.get("id"),
            "classification": item.get("classification"),
            "no_trade_reason": reason_text,
            "no_trade_reason_category": category,
            "shadow_pnl": latest_pnl,
            "shadow_results": results,
        }
        if category == "timing" and item.get("classification") == "missed_opportunity" and latest_pnl >= min_shadow_pnl:
            inserted += _insert_contextual_rule_calibration(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                scope=scope,
                rule_group="intraday_confirmation",
                rules={
                    "confirmed_memory_max_opening_range_miss": float(calibration_cfg.get("relaxed_opening_range_miss", 0.003)),
                    "confirmed_memory_min_market_confirmation_score": float(calibration_cfg.get("relaxed_intraday_confirmation_score", 0.65)),
                },
                reason="same-scope no-trade shadow suggests timing gate may be too strict",
                evidence=evidence,
                confidence_score=min(0.75, 0.45 + latest_pnl / 20000.0),
                sample_count=len(pnl_values),
                valid_days=valid_days,
            )
        elif category == "timing" and item.get("classification") == "correct_avoidance" and latest_pnl <= -min_shadow_loss:
            inserted += _insert_contextual_rule_calibration(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                scope=scope,
                rule_group="intraday_confirmation",
                rules={
                    "confirmed_memory_max_opening_range_miss": float(calibration_cfg.get("tightened_opening_range_miss", 0.001)),
                    "confirmed_memory_min_market_confirmation_score": float(calibration_cfg.get("tightened_intraday_confirmation_score", 0.72)),
                },
                reason="same-scope no-trade shadow suggests timing gate correctly avoided loss",
                evidence=evidence,
                confidence_score=min(0.75, 0.45 + abs(latest_pnl) / 20000.0),
                sample_count=len(pnl_values),
                valid_days=valid_days,
            )

    for recommendation in strategy_recommendations:
        if inserted >= max_rows:
            break
        snapshot = _recommendation_snapshot(recommendation)
        plan = _pre_open_plan(snapshot)
        holding = (
            ((plan.get("strategy_controls") or {}).get("diagnostics") or {}).get("holding_rebalance_control")
            if isinstance(plan.get("strategy_controls"), dict)
            else {}
        )
        holding = holding if isinstance(holding, dict) else {}
        decision = str(holding.get("decision") or "")
        if decision not in {
            "skip_horizon_mismatch_new_entry",
            "reduce_failed_new_loss_revalidation",
            "exit_failed_new_loss_revalidation",
            "reduce_horizon_mismatch_losing_hold",
            "exit_horizon_mismatch_losing_hold",
        }:
            continue
        side = _recommendation_side(recommendation, snapshot)
        combo = _signal_combo_from_snapshot(snapshot)
        scope = {
            "ticker": recommendation.get("underlying_code"),
            "side": side,
            "signal_template": _signal_template(side, combo, snapshot),
            "horizon_class": _horizon_class(_expected_horizon_days(snapshot, side), snapshot),
            "market_regime": _market_regime(snapshot),
        }
        rules = {}
        reason = "same-scope PM lifecycle/horizon observation keeps strict validation until future evidence improves"
        if "horizon_mismatch" in decision or "horizon_consistency" in str(holding.get("lifecycle_classification") or ""):
            rules["min_confirmation_score"] = float(calibration_cfg.get("horizon_confirm_score_after_failure", 0.58))
            rules["min_short_timing_confidence"] = float(calibration_cfg.get("short_timing_confidence_after_failure", 0.48))
            rules["losing_hold_reduction_multiplier"] = float(calibration_cfg.get("losing_hold_reduction_after_failure", 0.45))
        if "new_loss_revalidation" in decision:
            rules["new_loss_revalidation_min_confirmation_score"] = float(calibration_cfg.get("new_loss_confirm_score_after_failure", 0.58))
            rules["new_loss_revalidation_min_signal_strength"] = float(calibration_cfg.get("new_loss_signal_strength_after_failure", 0.28))
            rules["new_loss_revalidation_reduction_multiplier"] = float(calibration_cfg.get("new_loss_reduction_after_failure", 0.45))
        if not rules:
            continue
        inserted += _insert_contextual_rule_calibration(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            scope=scope,
            rule_group="portfolio_manager",
            rules=rules,
            reason=reason,
            evidence={
                "source": "pm_holding_rebalance_diagnostics",
                "recommendation_id": recommendation.get("id"),
                "decision": decision,
                "holding_rebalance_control": holding,
            },
            confidence_score=float(calibration_cfg.get("pm_observation_confidence", 0.45)),
            sample_count=1,
            valid_days=valid_days,
            maturity_state="short_lived_contextual_pm_calibration",
        )

    cursor.execute(
        '''
        SELECT *
        FROM analyst_performance
        WHERE config_id = ?
          AND sample_count >= ?
          AND confidence_score >= ?
        ORDER BY last_updated DESC, confidence_score DESC
        LIMIT ?
        ''',
        (
            config_id,
            int(calibration_cfg.get("min_analyst_samples", 3) or 3),
            float(calibration_cfg.get("min_analyst_confidence", 0.35) or 0.35),
            max_rows,
        ),
    )
    for row in cursor.fetchall():
        if inserted >= max_rows:
            break
        item = dict(row)
        hit_rate = _safe_float(item.get("hit_rate"), 0.0)
        net_pnl = _safe_float(item.get("net_pnl"), 0.0)
        analyst = str(item.get("analyst") or "")
        horizon = str(item.get("horizon_class") or "*")
        side = str(item.get("signal_side") or "*")
        ticker = str(item.get("ticker") or "*").upper()
        scope = {
            "ticker": ticker,
            "side": side,
            "signal_template": "*",
            "horizon_class": horizon,
            "market_regime": "*",
        }
        if hit_rate >= float(calibration_cfg.get("analyst_positive_hit_rate", 0.60) or 0.60) and net_pnl > 0:
            rules = {}
            if horizon in {"medium", "long"}:
                rules["min_short_timing_confidence"] = float(calibration_cfg.get("positive_medium_short_timing_confidence", 0.42))
                rules["min_confirmation_score"] = float(calibration_cfg.get("positive_medium_confirm_score", 0.52))
            else:
                rules["probe_min_confirmation_score"] = float(calibration_cfg.get("positive_probe_confirm_score", 0.50))
            inserted += _insert_contextual_rule_calibration(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                scope=scope,
                rule_group="portfolio_manager",
                rules=rules,
                reason="same-scope analyst performance supports modestly less restrictive PM validation",
                evidence={"source": "analyst_performance", **item},
                confidence_score=min(0.70, _safe_float(item.get("confidence_score"), 0.35)),
                sample_count=_safe_int(item.get("sample_count"), 1),
                valid_days=valid_days,
                maturity_state="analyst_performance_contextual_calibration",
            )
        elif hit_rate <= float(calibration_cfg.get("analyst_weak_hit_rate", 0.40) or 0.40) or net_pnl < 0:
            rules = {}
            if horizon in {"medium", "long"}:
                rules["min_short_timing_confidence"] = float(calibration_cfg.get("weak_medium_short_timing_confidence", 0.50))
                rules["min_confirmation_score"] = float(calibration_cfg.get("weak_medium_confirm_score", 0.60))
            else:
                rules["probe_min_confirmation_score"] = float(calibration_cfg.get("weak_probe_confirm_score", 0.58))
            inserted += _insert_contextual_rule_calibration(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                scope=scope,
                rule_group="portfolio_manager",
                rules=rules,
                reason="same-scope analyst performance is weak; PM validation stays tighter until evidence improves",
                evidence={"source": "analyst_performance", **item},
                confidence_score=min(0.70, _safe_float(item.get("confidence_score"), 0.35)),
                sample_count=_safe_int(item.get("sample_count"), 1),
                valid_days=valid_days,
                maturity_state="analyst_performance_contextual_calibration",
            )
        if inserted >= max_rows:
            break
        if analyst in {"technical", "AgentKey.TECHNICAL"} and horizon == "short":
            technical_rules = _technical_calibration_rules_from_performance(
                horizon=horizon,
                hit_rate=hit_rate,
                net_pnl=net_pnl,
                positive_hit_rate=float(calibration_cfg.get("technical_positive_hit_rate", calibration_cfg.get("analyst_positive_hit_rate", 0.60)) or 0.60),
                weak_hit_rate=float(calibration_cfg.get("technical_weak_hit_rate", calibration_cfg.get("analyst_weak_hit_rate", 0.40)) or 0.40),
            )
            if technical_rules:
                inserted += _insert_contextual_rule_calibration(
                    cursor,
                    config_id=config_id,
                    trading_date=trading_date,
                    scope={
                        "ticker": ticker,
                        "side": "*",
                        "signal_template": "*",
                        "horizon_class": "short",
                        "market_regime": "*",
                    },
                    rule_group="technical_parameters",
                    rules=technical_rules,
                    reason=(
                        "same-scope technical analyst performance suggests a bounded indicator-parameter "
                        "calibration for future short-horizon analysis"
                    ),
                    evidence={"source": "technical_analyst_performance", **item},
                    confidence_score=min(0.65, _safe_float(item.get("confidence_score"), 0.35)),
                    sample_count=_safe_int(item.get("sample_count"), 1),
                    valid_days=int(calibration_cfg.get("technical_valid_days", valid_days) or valid_days),
                    maturity_state="technical_parameter_contextual_calibration",
                )
    return inserted


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
    shadow_summary = _neutral_shadow_tracking_summary(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        recommendations=recommendations,
    )
    summary["shadow_tracking"] = shadow_summary
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
            "shadow_tracking": shadow_summary,
            "action_items": summary.get("action_items", []),
        },
        status="applied",
    )
    digest_rows = _write_neutral_accountability_digests(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        summary=summary,
    )
    summary["structured_learning_rows"] = digest_rows
    return summary


def _neutral_shadow_tracking_summary(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any] | None = None,
    config_id: str,
    trading_date: str,
    recommendations: List[Dict[str, Any]],
    write_event: bool = True,
) -> Dict[str, Any]:
    by_ticker: Dict[str, float] = {}
    try:
        cursor.execute(
            """
            SELECT ticker, SUM(daily_pnl) AS pnl
            FROM ticker_daily_pnl tdp
            JOIN portfolio p ON tdp.portfolio_id = p.id
            WHERE p.config_id = ?
              AND substr(tdp.trading_date, 1, 10) = ?
            GROUP BY ticker
            """,
            (config_id, trading_date),
        )
        by_ticker = {str(row["ticker"] or "").upper(): _safe_float(row["pnl"]) for row in cursor.fetchall()}
    except sqlite3.Error:
        by_ticker = {}

    observations: List[Dict[str, Any]] = []
    missed_opportunity = 0
    reasonable_avoidance = 0
    for recommendation in recommendations:
        snapshot = recommendation.get("signal_snapshot") if isinstance(recommendation.get("signal_snapshot"), dict) else {}
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "").upper()
        ticker_pnl = by_ticker.get(ticker, 0.0)
        for analyst, payload in _analyst_payloads(snapshot).items():
            if str(payload.get("signal") or "Neutral") != "Neutral":
                continue
            consensus = _directional_consensus_from_snapshot(snapshot, analyst)
            shadow_side = consensus.get("signal")
            if shadow_side not in {"Bullish", "Bearish"} or _safe_int(consensus.get("support_count")) <= 0:
                continue
            shadow_pnl = ticker_pnl if shadow_side == "Bullish" else -ticker_pnl
            classification = "missed_opportunity" if shadow_pnl > 0 else "reasonable_avoidance" if shadow_pnl < 0 else "neutral_unresolved"
            if classification == "missed_opportunity":
                missed_opportunity += 1
            elif classification == "reasonable_avoidance":
                reasonable_avoidance += 1
            observations.append(
                {
                    "ticker": ticker,
                    "recommendation_id": recommendation.get("id"),
                    "analyst": analyst,
                    "shadow_side": shadow_side,
                    "support_count": _safe_int(consensus.get("support_count")),
                    "shadow_pnl": shadow_pnl,
                    "classification": classification,
                }
            )

    total_shadow_pnl = sum(_safe_float(item.get("shadow_pnl")) for item in observations)
    account_cfg = (((cfg or {}).get("llm_signal_quality") or {}).get("neutral_accountability") or {})
    forward_days = max(0, int(account_cfg.get("shadow_forward_days", 0) or 0))
    forward_dates: List[str] = []
    forward_by_ticker: Dict[str, float] = {}
    if forward_days > 0:
        try:
            cursor.execute(
                """
                SELECT DISTINCT substr(ds.trading_date, 1, 10) AS trading_day
                FROM daily_settlement ds
                JOIN portfolio p ON ds.portfolio_id = p.id
                WHERE p.config_id = ?
                  AND substr(ds.trading_date, 1, 10) > ?
                ORDER BY trading_day
                LIMIT ?
                """,
                (config_id, trading_date, forward_days),
            )
            forward_dates = [str(row["trading_day"]) for row in cursor.fetchall() if row["trading_day"]]
            if forward_dates:
                placeholders = ",".join("?" for _ in forward_dates)
                cursor.execute(
                    f"""
                    SELECT ticker, SUM(daily_pnl) AS pnl
                    FROM ticker_daily_pnl tdp
                    JOIN portfolio p ON tdp.portfolio_id = p.id
                    WHERE p.config_id = ?
                      AND substr(tdp.trading_date, 1, 10) IN ({placeholders})
                    GROUP BY ticker
                    """,
                    [config_id, *forward_dates],
                )
                forward_by_ticker = {
                    str(row["ticker"] or "").upper(): _safe_float(row["pnl"])
                    for row in cursor.fetchall()
                }
        except sqlite3.Error:
            forward_dates = []
            forward_by_ticker = {}

    forward_observations: List[Dict[str, Any]] = []
    forward_missed = 0
    forward_avoided = 0
    if forward_by_ticker:
        for recommendation in recommendations:
            snapshot = recommendation.get("signal_snapshot") if isinstance(recommendation.get("signal_snapshot"), dict) else {}
            ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "").upper()
            ticker_pnl = forward_by_ticker.get(ticker, 0.0)
            for analyst, payload in _analyst_payloads(snapshot).items():
                if str(payload.get("signal") or "Neutral") != "Neutral":
                    continue
                consensus = _directional_consensus_from_snapshot(snapshot, analyst)
                shadow_side = consensus.get("signal")
                if shadow_side not in {"Bullish", "Bearish"} or _safe_int(consensus.get("support_count")) <= 0:
                    continue
                shadow_pnl = ticker_pnl if shadow_side == "Bullish" else -ticker_pnl
                classification = (
                    "missed_opportunity" if shadow_pnl > 0
                    else "reasonable_avoidance" if shadow_pnl < 0
                    else "neutral_unresolved"
                )
                if classification == "missed_opportunity":
                    forward_missed += 1
                elif classification == "reasonable_avoidance":
                    forward_avoided += 1
                forward_observations.append(
                    {
                        "ticker": ticker,
                        "recommendation_id": recommendation.get("id"),
                        "analyst": analyst,
                        "shadow_side": shadow_side,
                        "support_count": _safe_int(consensus.get("support_count")),
                        "shadow_pnl": shadow_pnl,
                        "classification": classification,
                        "window_trading_dates": forward_dates,
                    }
                )
    total_forward_shadow_pnl = sum(_safe_float(item.get("shadow_pnl")) for item in forward_observations)
    summary = {
        "observation_count": len(observations),
        "missed_opportunity_count": missed_opportunity,
        "reasonable_avoidance_count": reasonable_avoidance,
        "total_shadow_pnl": total_shadow_pnl,
        "examples": observations[:12],
        "forward_window_days": forward_days,
        "forward_window_dates": forward_dates,
        "forward_status": "applied" if forward_dates else "pending_future_settlements" if forward_days > 0 else "disabled",
        "forward_observation_count": len(forward_observations),
        "forward_missed_opportunity_count": forward_missed,
        "forward_reasonable_avoidance_count": forward_avoided,
        "forward_total_shadow_pnl": total_forward_shadow_pnl,
        "forward_examples": forward_observations[:12],
    }
    if write_event:
        _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="neutral_shadow_tracking",
            scope_type="daily",
            scope_key=trading_date,
            evidence=summary,
            action={"tracking_only": True},
            status="applied",
        )
    return summary


def _backfill_neutral_forward_shadow_tracking(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    account_cfg = (((cfg or {}).get("llm_signal_quality") or {}).get("neutral_accountability") or {})
    forward_days = max(0, int(account_cfg.get("shadow_forward_days", 0) or 0))
    if forward_days <= 0:
        return {"status": "disabled", "rows": 0}

    try:
        settled_dates = _report_rows(
            cursor,
            """
            SELECT DISTINCT substr(ds.trading_date, 1, 10) AS trading_day
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE p.config_id = ?
              AND substr(ds.trading_date, 1, 10) <= ?
            ORDER BY trading_day
            """,
            (config_id, trading_date),
        )
    except sqlite3.Error:
        return {"status": "settlement_lookup_failed", "rows": 0}

    settled = [str(row.get("trading_day")) for row in settled_dates if row.get("trading_day")]
    eligible_dates: List[str] = []
    for index, day in enumerate(settled):
        if day >= trading_date:
            continue
        future_window = [candidate for candidate in settled[index + 1:index + 1 + forward_days] if candidate <= trading_date]
        if len(future_window) < forward_days:
            continue
        eligible_dates.append(day)
    if not eligible_dates:
        return {"status": "no_eligible_past_neutral_days", "rows": 0}

    rows_written = 0
    for day in eligible_dates[-forward_days * 2:]:
        already = _report_rows(
            cursor,
            """
            SELECT id
            FROM learning_event_log
            WHERE config_id = ?
              AND trading_date = ?
              AND event_type = 'neutral_forward_shadow_tracking'
            LIMIT 1
            """,
            (config_id, day),
        )
        if already:
            continue
        neutral_rows = _report_rows(
            cursor,
            """
            SELECT id, underlying_code, signal_snapshot,
                   signal_snapshot_artifact_path, signal_snapshot_sha256
            FROM futures_recommendation
            WHERE config_id = ?
              AND substr(trading_date, 1, 10) = ?
              AND source_type = ?
            ORDER BY underlying_code, created_at
            """,
            (config_id, day, RecommendationSourceType.STRATEGY.value),
        )
        if not neutral_rows:
            continue
        recommendations = []
        for row in neutral_rows:
            item = dict(row)
            item["signal_snapshot"] = _recommendation_snapshot(item)
            recommendations.append(item)
        summary = _neutral_shadow_tracking_summary(
            cursor,
            cfg=cfg,
            config_id=config_id,
            trading_date=day,
            recommendations=recommendations,
            write_event=False,
        )
        if summary.get("forward_status") != "applied":
            continue
        _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=day,
            event_type="neutral_forward_shadow_tracking",
            scope_type="daily",
            scope_key=day,
            evidence=summary,
            action={
                "tracking_only": True,
                "backfilled_on": trading_date,
                "forward_window_days": summary.get("forward_window_days"),
                "forward_window_dates": summary.get("forward_window_dates", []),
            },
            status="applied",
        )
        rows_written += 1
    return {"status": "applied" if rows_written else "no_new_rows", "rows": rows_written}


def _directional_consensus_from_snapshot(snapshot: Dict[str, Any], neutral_analyst: str) -> Dict[str, Any]:
    counts: Counter = Counter()
    supporters: List[str] = []
    for analyst, payload in _analyst_payloads(snapshot).items():
        if analyst == neutral_analyst:
            continue
        signal = str(payload.get("signal") or "Neutral")
        if signal not in {"Bullish", "Bearish"}:
            continue
        confidence = max(
            _safe_float(payload.get("effective_confidence")),
            _safe_float(payload.get("confidence")),
        )
        if confidence < 0.45:
            continue
        counts[signal] += 1
        supporters.append(f"{analyst}:{signal}")
    if not counts:
        return {"signal": "Neutral", "support_count": 0, "supporters": []}
    signal, support_count = counts.most_common(1)[0]
    return {"signal": signal, "support_count": int(support_count), "supporters": supporters}


def _neutral_accountability_digest_text(
    analyst: str,
    dominant_category: str,
    category_counts: Dict[str, Any],
    shadow_counts: Dict[str, Any] | None = None,
) -> str:
    shadow_counts = shadow_counts or {}
    shadow_suffix = ""
    observations = _safe_int(shadow_counts.get("observation_count"), 0)
    if observations:
        missed = _safe_int(shadow_counts.get("missed_opportunity_count"), 0)
        avoided = _safe_int(shadow_counts.get("reasonable_avoidance_count"), 0)
        shadow_pnl = _safe_float(shadow_counts.get("total_shadow_pnl"), 0.0)
        shadow_suffix = (
            f" Shadow tracking: observations={observations}, missed={missed}, "
            f"reasonable_avoidance={avoided}, shadow_pnl={shadow_pnl:.0f}."
        )
    if dominant_category == "reasonable_avoidance":
        return (
            f"{analyst}: Neutral mostly avoided low-quality or conflicted setups. "
            "Keep requiring explicit evidence and a clear condition that would change the view."
            + shadow_suffix
        )
    if dominant_category == "evidence_gap_conservative":
        return (
            f"{analyst}: Neutral was mainly caused by evidence gaps. Improve evidence coverage, "
            "and do not convert missing optional data into directional conviction."
            + shadow_suffix
        )
    if dominant_category == "conservative_against_consensus":
        return (
            f"{analyst}: Neutral may have missed aligned directional evidence. In similar future cases, "
            "prefer a small probe only when market confirmation and invalidation are clear."
            + shadow_suffix
        )
    if dominant_category == "unaccountable_neutral":
        return (
            f"{analyst}: Neutral lacked required accountability fields. Future Neutral output must state "
            "missing evidence, conflicting factors, and the condition that would change the view."
            + shadow_suffix
        )
    return (
        f"{analyst}: Neutral accountability recorded with categories {dict(category_counts)}. "
        "Use this as a structured prior for future signal discipline."
        + shadow_suffix
    )


def _write_neutral_accountability_digests(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    summary: Dict[str, Any],
) -> int:
    account_cfg = (((cfg or {}).get("llm_signal_quality") or {}).get("neutral_accountability") or {})
    if not bool(account_cfg.get("write_structured_learning", True)):
        return 0

    by_analyst = summary.get("by_analyst") or {}
    if not isinstance(by_analyst, dict):
        return 0
    shadow_by_analyst: Dict[str, Counter] = defaultdict(Counter)
    shadow_summary = summary.get("shadow_tracking") if isinstance(summary.get("shadow_tracking"), dict) else {}
    for item in (shadow_summary.get("examples") if isinstance(shadow_summary, dict) else []) or []:
        if not isinstance(item, dict):
            continue
        analyst = str(item.get("analyst") or "")
        if not analyst:
            continue
        shadow_by_analyst[analyst]["observation_count"] += 1
        classification = str(item.get("classification") or "")
        if classification == "missed_opportunity":
            shadow_by_analyst[analyst]["missed_opportunity_count"] += 1
        elif classification == "reasonable_avoidance":
            shadow_by_analyst[analyst]["reasonable_avoidance_count"] += 1
        shadow_by_analyst[analyst]["total_shadow_pnl"] += _safe_float(item.get("shadow_pnl"), 0.0)

    now = _utc_now()
    learning_cfg = (cfg or {}).get("learning", {}) or {}
    valid_until = _valid_until(trading_date, int(learning_cfg.get("memory_expires_after_days", 30) or 30))
    rows = 0
    for analyst, payload in sorted(by_analyst.items()):
        if not isinstance(payload, dict):
            continue
        neutral_count = _safe_int(payload.get("neutral_count"), 0)
        signal_count = max(1, _safe_int(payload.get("signal_count"), 0))
        if neutral_count <= 0:
            continue
        category_counts = payload.get("category_counts") or {}
        if not isinstance(category_counts, dict) or not category_counts:
            dominant_category = "accountable_observation"
        else:
            dominant_category = str(max(category_counts.items(), key=lambda item: _safe_int(item[1], 0))[0])
        evidence = {
            "trading_date": trading_date,
            "analyst": analyst,
            "neutral_count": neutral_count,
            "signal_count": signal_count,
            "neutral_ratio": neutral_count / signal_count,
            "dominant_category": dominant_category,
            "category_counts": category_counts,
            "missing_field_counts": payload.get("missing_field_counts") or {},
            "shadow_tracking": dict(shadow_by_analyst.get(str(analyst), Counter())),
        }
        digest = _neutral_accountability_digest_text(
            analyst,
            dominant_category,
            category_counts,
            shadow_counts=evidence["shadow_tracking"],
        )
        digest_contract = build_next_round_memory_contract(
            memory_type="neutral_accountability_digest",
            maturity_state="discipline_digest",
            scope={
                "analyst": analyst,
                "ticker": "*",
                "sector": "*",
                "horizon_class": "neutral_accountability",
                "market_regime": dominant_category,
            },
            usable_memory=digest,
            analysis_strategy_updates=[
                "Use Neutral accountability to make the next Neutral explicit: reasonable avoidance, evidence gap, watchlist trigger, or missed-opportunity risk.",
                "When Neutral hides a conditional opportunity, state the trigger that would change the view.",
            ],
            trading_strategy_updates=[
                "Neutral accountability is not trade permission; it can only create watchlist/probe questions until current evidence confirms.",
                "If repeated forward shadow results show missed opportunities, promote through same-scope validation before PM sizing impact.",
            ],
            validation_plan=[
                "Use same-day and configured forward shadow tracking after settlement to classify future Neutral outcomes.",
            ],
            sample_count=neutral_count,
            confidence_score=min(1.0, neutral_count / signal_count),
        )
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="neutral_accountability_digest",
            scope_type="analyst_neutral",
            scope_key=f"{analyst}:neutral:{dominant_category}",
            evidence=evidence,
            action={"digest": digest, "dominant_category": dominant_category, CONTRACT_KEY: digest_contract},
            status="applied",
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
                str(analyst),
                "*",
                "*",
                "neutral_accountability",
                dominant_category,
                digest,
                min(1.0, neutral_count / signal_count),
                neutral_count,
                event_id,
                now,
                valid_until,
                1,
                _json_dumps({**evidence, CONTRACT_KEY: digest_contract}),
            ),
        )
        rows += 1
    return rows


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
                "recovery_probe_candidate_count": deployment_diagnostics["recovery_probe_candidate_count"],
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
        payload = _policy_contract_payload(
            policy_type="provisional_policy_state",
            policy_action=action,
            reason=f"early risk sentinel: {trigger_type}, net_pnl={net_pnl:.0f}, win_rate={win_rate:.2%}",
            multiplier=multiplier,
            maturity_state="provisional_risk_sentinel",
            scope={
                "ticker": ticker,
                "side": side,
                "signal_template": template,
                "horizon_class": horizon,
                "market_regime": str(row.get("market_regime") or "*"),
            },
            evidence={
                **payload,
                "trigger_type": trigger_type,
                "confidence_score": min(0.85, max(0.35, abs(net_pnl) / 25000.0 + (1.0 - win_rate) * 0.25)),
            },
        )
        payload["rollback_value"] = {"policy_action": "inactive"}
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
        project_root = Path(__file__).resolve().parents[4]
        path = project_root / path
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
            "no_trade_reason_categories": _no_trade_reason_category_counts(no_trade_reason_counter),
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
    return run_researcher_causal_review(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        settlement_row=settlement_row,
        strategy_recommendations=strategy_recommendations,
        no_trade_reason_counter=no_trade_reason_counter,
    )


def _recent_trade_episodes_for_research(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    limit: int,
) -> List[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, ticker, side, sector, signal_template, horizon_class,
               market_regime, open_date, close_date, holding_days, net_pnl,
               return_on_notional, outcome_label, lesson_text
        FROM trade_episode_memory
        WHERE config_id = ?
          AND (close_date IS NULL OR close_date <= ?)
        ORDER BY ABS(net_pnl) DESC, close_date DESC, created_at DESC
        LIMIT ?
        """,
        (config_id, trading_date, int(limit)),
    )
    return [dict(row) for row in cursor.fetchall()]


def _write_exploratory_hypotheses(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    return write_exploratory_hypotheses(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )


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
    """Backward-compatible wrapper; Phase4 learning now belongs to Researcher."""
    return apply_researcher_learning(
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


def run_phase4_review(
    *,
    cfg: Dict[str, Any],
    db: Any,
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    """Run deterministic Phase4 reviewer validation and daily reporting."""
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

        signal_persistence_audit = _validate_phase1_signal_persistence(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            strategy_recommendations=strategy_recommendations,
            expected_tickers=expected_tickers,
            expected_analysts=cfg.get("workflow_analysts") or ANALYSTS,
            errors=errors,
            warnings=warnings,
        )

        if phase1_transactions:
            errors.append(f"phase1 should not write real transactions, but found {len(phase1_transactions)} rows")

        if not phase2 or phase2.get("status") != "completed":
            errors.append(f"phase2 not completed on {trading_date}")
        elif not phase2_transactions:
            zero_transaction_day = classify_zero_transaction_day(strategy_recommendations)
            zero_transaction_class = zero_transaction_day["classification"]
            zero_transaction_reasons = zero_transaction_day["reasons"]
            zero_transaction_categories = zero_transaction_day.get("reason_categories") or {}
            if zero_transaction_class == "expected":
                warnings.append(
                    f"phase2 completed on {trading_date} with 0 transactions, but all strategy recommendations "
                    f"were expected no-trade cases: {dict(Counter(zero_transaction_reasons))}; "
                    f"categories={zero_transaction_categories}"
                )
            else:
                warnings.append(
                    f"phase2 completed on {trading_date} but no transactions were written; "
                    f"classification={zero_transaction_class}, reasons={dict(Counter(zero_transaction_reasons))}, "
                    f"categories={zero_transaction_categories}; treated as a no-trade learning day, not a phase-flow failure"
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
            previous_account_equity = float(
                settlement_row.get("previous_account_equity")
                or _futures_account_equity(
                    settlement_row.get("previous_balance"),
                    settlement_row.get("previous_margin"),
                )
            )
            current_account_equity = float(
                settlement_row.get("current_account_equity")
                or _futures_account_equity(
                    settlement_row.get("current_balance"),
                    settlement_row.get("current_margin"),
                )
            )
            actual_equity_change = current_account_equity - previous_account_equity
            expected_equity_change = _expected_settlement_equity_change(settlement_row)
            if not isclose(actual_equity_change, expected_equity_change, abs_tol=0.01):
                errors.append(
                    f"settlement equity formula mismatch: actual_change={actual_equity_change:.2f}, "
                    f"expected_change={expected_equity_change:.2f}"
                )

            if latest_portfolio and _normalize_date(latest_portfolio.get("trading_date")) == trading_date:
                account_equity = current_account_equity
                portfolio_margin = float(latest_portfolio.get("margin_used") or 0.0)
                portfolio_available = float(latest_portfolio.get("available_cash") or 0.0)
                expected_available = float(
                    settlement_row.get("cash_available")
                    or settlement_row.get("current_balance")
                    or 0.0
                )
                if not isclose(portfolio_margin, float(settlement_row.get("current_margin") or 0.0), abs_tol=0.01):
                    errors.append(
                        f"portfolio margin mismatch: portfolio={portfolio_margin:.2f}, "
                        f"daily_settlement={float(settlement_row.get('current_margin') or 0.0):.2f}"
                    )
                if not isclose(portfolio_available, expected_available, abs_tol=0.01):
                    errors.append(
                        f"available_cash mismatch: portfolio={portfolio_available:.2f}, "
                        f"expected cash_available={expected_available:.2f}"
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
                extra_audit={
                    "signal_persistence": signal_persistence_audit,
                },
            ),
        )

        if errors:
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
                    phase4_status_override="failed",
                    phase4_completed_at_override="",
                    phase4_message_override=f"Phase flow validation failed with {len(errors)} error(s)",
                )
                logger.info(f"Daily transaction report written: {report_path}")
            except Exception as report_exc:
                errors.append(f"daily transaction report generation failed: {report_exc}")
                logger.error(f"Daily transaction report generation failed: {report_exc}")
            raise RuntimeError(f"Phase flow validation failed with {len(errors)} error(s)")

        from agents.research_team.researcher import researcher_agent

        learning_summary = researcher_agent(
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
        logger.info(f"Researcher learning persisted: {learning_summary}")
        logger.info(f"Researcher learning report written: {reviewer_report_paths}")

        db.complete_trading_day_phase(
            config_id,
            trading_date,
            TradingPhase.PHASE4,
            "completed",
            "reviewer validation and researcher learning passed",
            memory_config=cfg.get("strategy_memory", {}),
        )
        phase4_completed_at = datetime.now(timezone.utc).isoformat()
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
                phase4_status_override="completed",
                phase4_completed_at_override=phase4_completed_at,
                phase4_message_override="reviewer validation and researcher learning passed",
            )
            logger.info(f"Daily transaction report written: {report_path}")
        except Exception as report_exc:
            logger.error(f"Daily transaction report generation failed: {report_exc}")
            raise RuntimeError(f"daily transaction report generation failed: {report_exc}") from report_exc

        logger.info("Phase4 reviewer validation and researcher learning passed")
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
