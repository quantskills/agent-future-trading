from __future__ import annotations

"""Phase4 validation, reporting, and daily transaction log helpers."""

import json
import os
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
    build_execution_learning_trace,
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
    learning_mechanism_counts,
    learning_mechanisms_from_context,
    learning_tags_from_context,
    summarize_pairs_by_learning_effect,
    summarize_pairs_by_learning_mechanism,
)
from util.logger import logger
from util.text_sanitize import sanitize_visible_text
from tools.agent_tools.research.learning_contract import (
    CONTRACT_KEY,
    attach_or_upgrade_next_round_memory_contract,
    attach_next_round_memory_contract,
    build_next_round_memory_contract,
)
from tools.agent_tools.research.neutral_accountability import build_neutral_accountability_summary
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
        execution_translation = (
            snapshot.get("execution_translation")
            if isinstance(snapshot.get("execution_translation"), dict)
            else {}
        )
        candidates = [
            execution_translation.get("dynamic_net_exposure_control"),
            snapshot.get("dynamic_net_exposure_control"),
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
    confirmation = snapshot.get("market_confirmation")
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


def _audit_signal_data_lineage(
    recommendations: List[Dict[str, Any]],
    *,
    expected_analysts: Iterable[str],
) -> Dict[str, Any]:
    analysts = tuple(str(analyst) for analyst in expected_analysts if analyst)
    artifact_missing: List[str] = []
    snapshot_missing: List[str] = []
    data_issue_counts: Counter = Counter()
    data_issue_features: Dict[str, set[str]] = defaultdict(set)
    dates_by_ticker: Dict[str, set[str]] = defaultdict(set)

    for recommendation in recommendations:
        ticker = _ticker_label(recommendation.get("underlying_code") or recommendation.get("ticker"))
        rec_date = _normalize_date(
            recommendation.get("trading_date")
            or recommendation.get("effective_trade_date")
            or ""
        )[:10]
        if rec_date:
            dates_by_ticker[ticker].add(rec_date)
        if not recommendation.get("signal_snapshot_artifact_path") and not recommendation.get("signal_snapshot"):
            artifact_missing.append(ticker)
        snapshot = _recommendation_snapshot(recommendation)
        payloads = _analyst_payloads(snapshot)
        for analyst in analysts:
            if analyst not in payloads:
                snapshot_missing.append(f"{ticker}:{analyst}")

        confirmation = _market_confirmation(snapshot)
        for feature, status in (confirmation.get("feature_status") or {}).items():
            status_text = str(status or "unknown")
            if status_text == "ok":
                continue
            data_issue_counts[status_text] += 1
            data_issue_features[status_text].add(str(feature))

    return {
        "recommendation_count": len(recommendations),
        "artifact_missing": sorted(set(artifact_missing)),
        "snapshot_missing_pairs": sorted(set(snapshot_missing)),
        "data_issue_counts": dict(sorted(data_issue_counts.items())),
        "data_issue_features": {
            status: sorted(features)
            for status, features in sorted(data_issue_features.items())
        },
        "recommendation_dates_by_ticker": {
            ticker: sorted(values)
            for ticker, values in sorted(dates_by_ticker.items())
        },
        "verified": not artifact_missing and not snapshot_missing,
    }


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
        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        reasons = contract.get("reason_codes") or contract.get("risk_flags") or []
        if reasons:
            warnings.append(f"{ticker}: strategy controls recorded: {reasons}")
    return warnings, market_summary


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
    "neutral_signal_no_trade",
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


def _has_hard_capital_reason(reasons: Iterable[str]) -> bool:
    text = " ".join(str(reason).lower() for reason in reasons)
    return any(token in text for token in CAPITAL_HARD_RISK_REASON_TOKENS)


def _recommendation_capital_item(recommendation: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _recommendation_snapshot(recommendation)
    confirmation = _market_confirmation(snapshot)
    execution_result = snapshot.get("execution_result") if isinstance(snapshot.get("execution_result"), dict) else {}
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    position_budget = snapshot.get("position_budget_policy") if isinstance(snapshot.get("position_budget_policy"), dict) else {}
    final_trade_authority = contract
    ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "").upper()
    target_lots = _safe_int(contract.get("target_lots"))
    current_lots = _safe_int(contract.get("current_lots"))
    target_ratio = _safe_float(contract.get("target_position_ratio"))
    current_ratio = 0.0
    target_side = _target_side_from_ratio(target_lots if target_lots else target_ratio)
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
        capital_path_stage = "hard_or_auditor_block"
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
        "tradable_lots": abs(_safe_int(contract.get("lots_delta"))),
        "no_trade_reason": no_trade_reason,
        "rebalance_action_type": str(contract.get("final_action") or ""),
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
    side = _recommendation_side(recommendation, snapshot)
    if side in {"long", "short"}:
        return side
    return side if side == "flat" else "unknown"


def _analyst_signal_line(snapshot: Dict[str, Any], analyst: str) -> str:
    payload = _analyst_payloads(snapshot).get(analyst) or {}
    if not payload:
        return f"    {analyst}: -/-"
    signal = payload.get("signal", "Neutral")
    confidence = _safe_float(payload.get("confidence"), 0.0)
    setup_type = str(payload.get("setup_type") or "")
    tradeability = str(payload.get("tradeability") or "")
    suffix_parts = []
    if tradeability:
        suffix_parts.append(f"tradeability={tradeability}")
    if setup_type:
        suffix_parts.append(f"setup_type={setup_type}")
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


def _setup_type_counts(strategy_recommendations: List[Dict[str, Any]]) -> Counter:
    counts: Counter = Counter()
    for recommendation in strategy_recommendations:
        snapshot = _recommendation_snapshot(recommendation)
        for payload in _analyst_payloads(snapshot).values():
            setup_type = str(payload.get("setup_type") or "").strip()
            if setup_type and setup_type != "unknown":
                counts[setup_type] += 1
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
            contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
            control_notes = contract.get("reason_codes") or []
            if control_notes:
                lines.append(f"  【控制规则】{control_notes}")
            lines.append(
                "  【最终交易契约】"
                f"final_action={contract.get('final_action')}; "
                f"current_lots={contract.get('current_lots')}; "
                f"target_lots={contract.get('target_lots')}; "
                f"lots_delta={contract.get('lots_delta')}; "
                f"reason_codes={contract.get('reason_codes')}"
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
        contract = snapshot.get("final_action_contract") if isinstance(snapshot, dict) and isinstance(snapshot.get("final_action_contract"), dict) else {}
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
            "  【最终交易契约】"
            f"final_action={contract.get('final_action')}; "
            f"current_lots={contract.get('current_lots')}; "
            f"target_lots={contract.get('target_lots')}; "
            f"lots_delta={contract.get('lots_delta')}; "
            f"reason_codes={contract.get('reason_codes')}"
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
    for template, count in _setup_type_counts(strategy_recommendations).most_common():
        lines.append(f"  {template:<48} {count}")
    if not _setup_type_counts(strategy_recommendations):
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
    configured_report_dir = os.getenv("AGENTQUANT_TRANSACTION_REPORT_DIR")
    output_dir = Path(configured_report_dir) if configured_report_dir else SRC_ROOT / "logs"
    output_path = output_dir / f"{trading_date}_transaction.log"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_text, encoding="utf-8")
    return output_path


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


def _final_action_contract_payload(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    if not contract:
        return {}
    return {
        "final_action_contract": contract,
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
    contract = snapshot.get("final_action_contract")
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
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    target_lots = contract.get("target_lots") if isinstance(contract, dict) else None
    if target_lots is not None:
        side = _target_side_from_ratio(target_lots)
        if side in {"long", "short", "flat"}:
            return side
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
    final_contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    evidence_used = final_contract.get("evidence_used") if isinstance(final_contract.get("evidence_used"), dict) else {}
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
            scorecard_side.get("opportunity_rank")
            if scorecard_side.get("opportunity_rank") is not None
            else evidence_used.get("opportunity_rank")
        ),
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


def _loss_template_policy_payload(
    *,
    reason: str,
    scope: Dict[str, Any],
    evidence: Dict[str, Any],
    multiplier: float,
    maturity_state: str = "validated_loss_template_policy",
) -> Dict[str, Any]:
    """Create a bounded policy for repeated loss templates.

    This is deliberately narrower than a blacklist: it can only cap/probe the
    same ticker/side/template/horizon/regime after current-day evidence still
    looks comparable.
    """
    sample_count = _safe_int(evidence.get("sample_count") or evidence.get("total_trades"), 0)
    confidence = _safe_float(evidence.get("confidence_score"), 0.0) or _confidence_from_summary(evidence)
    return attach_or_upgrade_next_round_memory_contract(
        {
            **dict(evidence or {}),
            "source": "loss_template_policy",
            "policy_type": "loss_template_policy",
            "policy_action": "cap",
            "reason": reason,
            "multiplier": multiplier,
            "evidence": evidence,
            "strategy_update_goal": "turn repeated attribution into next-round position discipline",
        },
        memory_type="loss_template_policy",
        maturity_state=maturity_state,
        status="applied",
        scope=scope,
        usable_memory=[
            reason,
            f"same-scope loss template; sample_count={sample_count}; multiplier={multiplier:.2f}",
            f"failure_family={evidence.get('failure_family') or 'unknown'}; data_combo={_compact_text(evidence.get('data_combo') or '', 160)}",
        ],
        analysis_strategy_updates=[
            *(
                (evidence.get("failure_family_actions") or {}).get("analysis", [])
                if isinstance(evidence.get("failure_family_actions"), dict)
                else []
            ),
            "For the same scope, analysts must explicitly compare today's data drivers against the loss template before raising confidence.",
            "If the same weak data mix repeats, downgrade conviction or name the new evidence that invalidates the old loss pattern.",
        ],
        trading_strategy_updates=[
            *(
                (evidence.get("failure_family_actions") or {}).get("trading", [])
                if isinstance(evidence.get("failure_family_actions"), dict)
                else []
            ),
            "PM/Auditor may cap to probe size when the same-scope loss template repeats without fresh confirmation.",
            "The cap is not a product ban: if today's trigger, horizon, market state, and invalidation boundary improve, record the contradiction and allow normal review.",
        ],
        pm_action_conditions=[
            "Apply only when ticker, side, template, horizon, and market regime still match.",
            "Require current-day confirmation and explicit invalidation before any same-scope new/add-on position.",
            "If an existing same-scope position is adverse and current evidence fails, prefer reduce/exit over position_matched.",
        ],
        invalidates_when=[
            "Future same-scope samples repair expectancy or show positive net PnL.",
            "Today's data drivers, market regime, or horizon no longer match the loss template.",
            "A fresh trigger plus explicit stop/invalidation boundary is present and passes PM/Auditor review.",
        ],
        validation_plan=[
            "Track future same-scope trades, no-trade counterfactuals, capped decisions, and realized PnL before extending validity.",
        ],
        position_authority="risk_reduction_conditioned",
        max_position_impact="may_reduce_or_cap_only_through_pm_auditor",
        sample_count=sample_count,
        confidence_score=confidence,
    )


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
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    side = _target_side_from_ratio(contract.get("target_lots"))
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
            FROM researcher_llm_notes
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
            contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
            side = _target_side_from_ratio(contract.get("target_lots"))
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
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
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
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
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
        mechanisms = _learning_mechanisms_from_recommendation(recommendation)
        item = dict(pair)
        item["learning_tags"] = tags
        item["learning_effects"] = effects
        item["learning_mechanisms"] = mechanisms
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
        "learning_mechanism_counts": learning_mechanism_counts(learned_pairs),
        "learning_mechanism_summary": summarize_pairs_by_learning_mechanism(learned_pairs),
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
        template = _setup_type(side, combo, snapshot)
        key = (ticker, side, template, horizon, regime)
        item = dict(pair)
        item["setup_type"] = template
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
                "setup_type": template,
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
        policy_action="diagnostic" if policy_action == "calibrate" else policy_action,
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
    payload["rule_validation_status"] = "validated_rule_applied"
    payload["calibration_boundary"] = (
        "This is a context-scoped weak-parameter adjustment. It cannot override the 20% margin cap, "
        "settlement/accounting checks, no-lookahead gates, limit-lock/expiry business rules, or the need for current evidence."
    )
    return payload


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


def _neutral_counterfactual_tracking_summary(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any] | None = None,
    config_id: str,
    trading_date: str,
    recommendations: List[Dict[str, Any]],
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
            counterfactual_side = consensus.get("signal")
            if counterfactual_side not in {"Bullish", "Bearish"} or _safe_int(consensus.get("support_count")) <= 0:
                continue
            counterfactual_pnl = ticker_pnl if counterfactual_side == "Bullish" else -ticker_pnl
            classification = "missed_opportunity" if counterfactual_pnl > 0 else "reasonable_avoidance" if counterfactual_pnl < 0 else "neutral_unresolved"
            if classification == "missed_opportunity":
                missed_opportunity += 1
            elif classification == "reasonable_avoidance":
                reasonable_avoidance += 1
            observations.append(
                {
                    "ticker": ticker,
                    "recommendation_id": recommendation.get("id"),
                    "analyst": analyst,
                    "counterfactual_side": counterfactual_side,
                    "support_count": _safe_int(consensus.get("support_count")),
                    "counterfactual_pnl": counterfactual_pnl,
                    "classification": classification,
                }
            )

    total_counterfactual_pnl = sum(_safe_float(item.get("counterfactual_pnl")) for item in observations)
    account_cfg = (((cfg or {}).get("signal_quality") or {}).get("neutral_accountability") or {})
    forward_days = max(0, int(account_cfg.get("counterfactual_forward_days", 0) or 0))
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
                counterfactual_side = consensus.get("signal")
                if counterfactual_side not in {"Bullish", "Bearish"} or _safe_int(consensus.get("support_count")) <= 0:
                    continue
                counterfactual_pnl = ticker_pnl if counterfactual_side == "Bullish" else -ticker_pnl
                classification = (
                    "missed_opportunity" if counterfactual_pnl > 0
                    else "reasonable_avoidance" if counterfactual_pnl < 0
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
                        "counterfactual_side": counterfactual_side,
                        "support_count": _safe_int(consensus.get("support_count")),
                        "counterfactual_pnl": counterfactual_pnl,
                        "classification": classification,
                        "window_trading_dates": forward_dates,
                    }
                )
    total_forward_counterfactual_pnl = sum(_safe_float(item.get("counterfactual_pnl")) for item in forward_observations)
    summary = {
        "observation_count": len(observations),
        "missed_opportunity_count": missed_opportunity,
        "reasonable_avoidance_count": reasonable_avoidance,
        "total_counterfactual_pnl": total_counterfactual_pnl,
        "examples": observations[:12],
        "forward_window_days": forward_days,
        "forward_window_dates": forward_dates,
        "forward_status": "applied" if forward_dates else "pending_future_settlements" if forward_days > 0 else "disabled",
        "forward_observation_count": len(forward_observations),
        "forward_missed_opportunity_count": forward_missed,
        "forward_reasonable_avoidance_count": forward_avoided,
        "forward_total_counterfactual_pnl": total_forward_counterfactual_pnl,
        "forward_examples": forward_observations[:12],
    }
    return summary


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
        data_lineage_audit = _audit_signal_data_lineage(
            strategy_recommendations,
            expected_analysts=cfg.get("workflow_analysts") or ANALYSTS,
        )
        if data_lineage_audit.get("artifact_missing"):
            errors.append(
                "recommendation signal artifacts missing: "
                f"{data_lineage_audit.get('artifact_missing')[:12]}"
            )
        if data_lineage_audit.get("snapshot_missing_pairs"):
            errors.append(
                "recommendation signal snapshots missing analyst payloads: "
                f"{data_lineage_audit.get('snapshot_missing_pairs')[:12]}"
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
        capital_preview = _build_capital_deployment_state(
            cfg=cfg,
            settlement_row=settlement_row,
            strategy_recommendations=strategy_recommendations,
            no_trade_reason_counter=no_trade_reason_counter,
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
                    "signal_data_lineage": data_lineage_audit,
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

        conn.commit()

        db.complete_trading_day_phase(
            config_id,
            trading_date,
            TradingPhase.PHASE4,
            "completed",
            "reviewer validation passed",
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
                phase4_message_override="reviewer validation passed",
            )
            logger.info(f"Daily transaction report written: {report_path}")
        except Exception as report_exc:
            logger.error(f"Daily transaction report generation failed: {report_exc}")
            raise RuntimeError(f"daily transaction report generation failed: {report_exc}") from report_exc

        logger.info("Phase4 reviewer validation passed")
        return {
            "status": "completed",
            "warnings": warnings,
            "errors": errors,
            "reviewer_summary": {
                "phase1_status": phase1.get("status") if phase1 else "missing",
                "phase2_status": phase2.get("status") if phase2 else "missing",
                "phase3_status": phase3.get("status") if phase3 else "missing",
                "strategy_recommendations": len(strategy_recommendations),
                "rollover_recommendations": len(rollover_recommendations),
                "phase1_transactions": len(phase1_transactions),
                "phase2_transactions": len(phase2_transactions),
                "no_trade_reason_counts": dict(no_trade_reason_counter),
            },
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


