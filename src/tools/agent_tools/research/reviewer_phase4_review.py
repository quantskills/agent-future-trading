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
from tools.common.contracts import final_action_contract_from_snapshot, validate_reviewer_artifact_boundary
from tools.common.final_action_semantics import derive_review_expectation
from tools.common.evidence_fusion_semantics import build_reviewer_fusion_attribution
from tools.common.learning_contract import (
    CONTRACT_KEY,
    attach_or_upgrade_next_round_memory_contract,
    attach_next_round_memory_contract,
    build_next_round_memory_contract,
)
from tools.common.neutral_accountability import build_neutral_accountability_summary
from tools.agent_tools.analysis.analyst_data_usage import data_usage_from_snapshot, compact_data_usage_notes
from tools.agent_tools.research import research_review_helpers as _review_helpers


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


def _final_action_semantic_summary(recommendations: List[Dict[str, Any]]) -> Dict[str, Any]:
    lifecycle_counts: Counter = Counter()
    memory_influenced_counts: Counter = Counter()
    memory_error_count = 0
    intraday_required = 0
    for recommendation in recommendations:
        raw_snapshot = recommendation.get("signal_snapshot") if isinstance(recommendation, dict) else {}
        snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else _review_helpers._recommendation_snapshot(recommendation)
        if not isinstance(snapshot, dict):
            continue
        contract = final_action_contract_from_snapshot(snapshot)
        execution_result = snapshot.get("execution_result") if isinstance(snapshot.get("execution_result"), dict) else {}
        semantic_state = derive_review_expectation(contract, execution_result)
        lifecycle_counts[str(semantic_state.get("lifecycle_state") or "unknown")] += 1
        if semantic_state.get("historical_learning_influenced_contract"):
            memory_influenced_counts[str(semantic_state.get("lifecycle_state") or "unknown")] += 1
        memory_error_count += len(semantic_state.get("pm_memory_consumption_errors") or [])
        if semantic_state.get("requires_intraday_result"):
            intraday_required += 1
    return {
        "contract": "final_action_semantics.reviewer_summary.v1",
        "lifecycle_counts": dict(sorted(lifecycle_counts.items())),
        "historical_learning_influenced_contract_counts": dict(sorted(memory_influenced_counts.items())),
        "pm_memory_consumption_error_count": memory_error_count,
        "intraday_result_required_count": intraday_required,
        "reviewer_does_not_modify_trade_facts": True,
        "reviewer_writes_action_value": False,
    }


def _fusion_attribution_summary(recommendations: List[Dict[str, Any]]) -> Dict[str, Any]:
    label_counts: Counter = Counter()
    conflict_count = 0
    consensus_count = 0
    for recommendation in recommendations:
        raw_snapshot = recommendation.get("signal_snapshot") if isinstance(recommendation, dict) else {}
        snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else _review_helpers._recommendation_snapshot(recommendation)
        if not isinstance(snapshot, dict):
            continue
        contract = final_action_contract_from_snapshot(snapshot)
        attribution = build_reviewer_fusion_attribution({"final_action_contract": contract})
        label = str(attribution.get("fusion_attribution_label") or "fusion_not_recorded")
        label_counts[label] += 1
        diagnostics = attribution.get("pm_fusion_diagnostics") if isinstance(attribution.get("pm_fusion_diagnostics"), dict) else {}
        conflict_count += int(diagnostics.get("cross_analyst_conflict_count") or 0)
        if float(diagnostics.get("multi_evidence_consensus_score") or 0.0) >= 0.58:
            consensus_count += 1
    return {
        "contract": "evidence_fusion_semantics.reviewer_summary.v1",
        "fusion_attribution_label_counts": dict(sorted(label_counts.items())),
        "cross_analyst_conflict_count": conflict_count,
        "multi_evidence_consensus_supported_count": consensus_count,
        "reviewer_does_not_modify_trade_facts": True,
        "reviewer_writes_action_value": False,
    }


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
        contract = final_action_contract_from_snapshot(snapshot)
        reasons = contract.get("reason_codes") or contract.get("risk_flags") or []
        if reasons:
            warnings.append(f"{ticker}: strategy controls recorded: {reasons}")
    return warnings, market_summary


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
        f"  {'#':>2}  {'ticker':<8} {'action':<12} {'lots':>5} "
        f"{'execution':>12} {'settle':>12} {'commission':>12} {'pnl':>12}"
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

    _report_section(lines, "3. Trade Reason Details")
    if not phase2_transactions:
        lines.append("  no actual transactions")
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
                lines.append("  [decision_basis]")
                lines.append(f"  {justification}")
            contract = final_action_contract_from_snapshot(snapshot)
            control_notes = contract.get("reason_codes") or []
            if control_notes:
                lines.append(f"  [control_rules] {control_notes}")
            lines.append(
                "  [final_action_contract] "
                f"final_action={contract.get('final_action')}; "
                f"current_lots={contract.get('current_lots')}; "
                f"target_lots={contract.get('target_lots')}; "
                f"lots_delta={contract.get('lots_delta')}; "
                f"reason_codes={contract.get('reason_codes')}"
            )
            analyst_lines = _analyst_reason_lines(snapshot, limit=420)
            if analyst_lines:
                lines.append("  [analyst_signals]")
                lines.extend(analyst_lines)
            execution_translation = snapshot.get("execution_translation") if isinstance(snapshot.get("execution_translation"), dict) else {}
            if execution_translation:
                final_basis = execution_translation.get("final_execution_basis") or {}
                lines.append(
                    "  [execution_details] "
                    f"{_action_label(tx.get('action'))} {int(tx.get('lots') or 0)} lot(s) "
                    f"@ {_money(tx.get('execution_price'))}; "
                    f"basis={execution_translation.get('base_price_source') or final_basis.get('base_price_source')}; "
                    f"slippage={execution_translation.get('slippage_amount') or final_basis.get('slippage_amount')}"
                )
            if tx.get("justification"):
                lines.append(f"  [transaction_note] {_report_text(tx.get('justification'), limit=520)}")
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
        contract = final_action_contract_from_snapshot(snapshot if isinstance(snapshot, dict) else {})
        direction = _recommendation_direction(recommendation, snapshot if isinstance(snapshot, dict) else {})
        lines.append(
            f"  {ticker} - direction={direction}; no_trade_reason={reason}; "
            f"reason_category={reason_category['category_label']}({reason_category['category']}); combo={combo}"
        )
        justification = _report_text(recommendation.get("justification"), limit=900)
        if justification:
            lines.append("  [decision_basis]")
            lines.append(f"  {justification}")
        lines.append(
            "  [final_action_contract] "
            f"final_action={contract.get('final_action')}; "
            f"current_lots={contract.get('current_lots')}; "
            f"target_lots={contract.get('target_lots')}; "
            f"lots_delta={contract.get('lots_delta')}; "
            f"reason_codes={contract.get('reason_codes')}"
        )
        analyst_lines = _analyst_reason_lines(snapshot if isinstance(snapshot, dict) else {}, limit=360)
        if analyst_lines:
            lines.append("  [analyst_signals]")
            lines.extend(analyst_lines)
        lines.append("")

    _report_section(lines, "5. Signal Summary")
    lines.append(
        f"  5.1 analyst signal matrix ({len(strategy_recommendations)} tickers x 3 analysts)"
    )
    lines.append("")
    lines.append(
        f"  {'ticker':<6} {'news':<22} {'fundamental':<22} {'technical':<22} {'fusion':<8} status"
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

    _report_section(lines, "6. System Decision Flow")
    lines.append(f"  {'phase':<18} {'status':<10} {'started_at':<28} {'completed_at':<28} message")
    for row in phase_rows:
        lines.append(
            f"  {str(row.get('phase') or ''):<18} "
            f"{str(row.get('status') or ''):<10} "
            f"{str(row.get('started_at') or ''):<28} "
            f"{str(row.get('completed_at') or ''):<28} "
            f"{_report_inline(row.get('message'), limit=120)}"
        )
    executed_tickers = ", ".join(sorted(traded_tickers)) if traded_tickers else "none"
    lines.extend(["", f"  key decision point: executed_tickers={executed_tickers}"])

    _report_section(lines, "7. Closing Positions")
    if not positions:
        lines.append("  no closing positions")
    else:
        lines.append(
            f"  {'ticker':<8} {'side':<8} {'lots':>6} {'entry':>12} "
            f"{'settle':>12} {'margin':>12} {'unrealized':>14}"
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
        f"\n  current_margin={_money((settlement_row or {}).get('current_margin'))}, "
        f"margin_ratio={margin_ratio:.2%}, leverage={leverage:.2f}x."
    )

    _report_section(lines, "8. Daily Key Features")
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
        f"  1. defense/reduction: {close_or_reduce} of {len(phase2_transactions)} transactions are close/reduce."
    )
    lines.append(
        f"  2. daily_pnl={_signed_money((settlement_row or {}).get('daily_pnl'))}; "
        f"commission={_money((settlement_row or {}).get('commission'))}."
    )
    lines.append(
        f"  3. directional_signals={directional_count}/{total_signals}; "
        f"Bullish={sorted(set(directional['Bullish']))}; Bearish={sorted(set(directional['Bearish']))}."
    )
    lines.append(f"  4. executed_tickers={executed_tickers}.")
    lines.append(f"  5. closing_position_count={len([p for p in positions.values() if _safe_int(_position_dict(p).get('shares')) != 0])}.")
    lines.extend(["", "Trace"])
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


for _name in _review_helpers.EXPORTED_RESEARCH_REVIEW_HELPERS:
    globals()[_name] = getattr(_review_helpers, _name)


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
        summary_payload = _build_summary_payload(
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
                "final_action_semantics": _final_action_semantic_summary(strategy_recommendations),
                "evidence_fusion_semantics": _fusion_attribution_summary(strategy_recommendations),
            },
        )
        validate_reviewer_artifact_boundary(summary_payload)
        logger.write_daily_summary(trading_date, summary_payload)

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
