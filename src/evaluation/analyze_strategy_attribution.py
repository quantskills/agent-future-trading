from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterable as IterableABC
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import yaml
from dotenv import load_dotenv

from database.artifact_store import load_externalized_json
from database.sqlite_setup import DB_PATH
from util.db_helper import db_initialize, get_db
from util.futures_trade_pairs import build_completed_trade_pairs, summarize_trade_pairs
from util.logger import logger


def resolve_config_path(config_path: str) -> str:
    path = Path(config_path)
    if path.is_absolute() or path.exists():
        return str(path)

    for candidate in (SRC_ROOT / path, SRC_ROOT.parent / path):
        if candidate.exists():
            return str(candidate)

    return str(path)


def _normalize_date(value: Any) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)[:10]


def _date_in_window(value: Any, start_date: str | None, end_date: str | None) -> bool:
    date_value = _normalize_date(value)
    if start_date and date_value < start_date:
        return False
    if end_date and date_value > end_date:
        return False
    return True


def _fetch_rows(db_path: str, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def _deserialize_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    return load_externalized_json(value)


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _fundamental_context_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    analyst_payload = _as_dict(snapshot.get("fundamental"))
    metadata = _as_dict(analyst_payload.get("metadata"))
    context = analyst_payload.get("fundamental_context")
    if not isinstance(context, dict):
        context = metadata.get("fundamental_context")
    return context if isinstance(context, dict) else {}


def _source_type(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    return str(value or "strategy").lower()


def _signal_value(snapshot: Dict[str, Any], analyst: str) -> str:
    item = snapshot.get(analyst)
    if isinstance(item, dict):
        value = item.get("signal")
        if value:
            return str(value)
    return "Neutral"


def _signal_combo(snapshot: Dict[str, Any]) -> str:
    commodity_news = _signal_value(snapshot, "commodity_news")
    if commodity_news == "Neutral":
        commodity_news = _signal_value(snapshot, "company_news")
    return "|".join(
        [
            _signal_value(snapshot, "technical"),
            _signal_value(snapshot, "fundamental"),
            commodity_news,
        ]
    )


def _trade_auditor(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    audit = snapshot.get("active_opportunity_audit")
    if isinstance(audit, dict):
        decision = audit.get("decision") if isinstance(audit.get("decision"), dict) else {}
        if decision:
            return {
                "decision": decision.get("audit_decision") or decision.get("decision") or decision.get("authority_type"),
                "reasons": audit.get("reason_codes") or decision.get("reason_codes") or [],
                "source": "active_opportunity_audit",
            }
    contract = snapshot.get("final_action_contract")
    if isinstance(contract, dict):
        return {
            "decision": contract.get("authority_type") or contract.get("final_action"),
            "reasons": contract.get("reason_codes") or [],
            "source": "final_action_contract",
        }
    return {}


def _rebalance_summary_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    contract = snapshot.get("final_action_contract")
    if not isinstance(contract, dict):
        return {}
    current_lots = _safe_int(contract.get("current_lots"))
    target_lots = _safe_int(contract.get("target_lots"))
    lots_delta_present = "lots_delta" in contract and contract.get("lots_delta") is not None
    lots_delta = _safe_int(contract.get("lots_delta")) if lots_delta_present else None
    expected_lots_delta = (
        target_lots - current_lots
        if current_lots is not None and target_lots is not None
        else None
    )
    contract_field_issues: List[str] = []
    if current_lots is None:
        contract_field_issues.append("missing_current_lots")
    if target_lots is None:
        contract_field_issues.append("missing_target_lots")
    if lots_delta is None:
        contract_field_issues.append("missing_lots_delta")
    elif expected_lots_delta is not None and lots_delta != expected_lots_delta:
        contract_field_issues.append("lots_delta_mismatch")
    return {
        "action_type": contract.get("final_action") or "unknown",
        "reason": ",".join(str(item) for item in (contract.get("reason_codes") or []) if item) or "none",
        "control_reasons": contract.get("reason_codes") or [],
        "holding_days": None,
        "turnover_notional_estimate": 0.0,
        "current_lots": current_lots,
        "target_lots": target_lots,
        "lots_delta": lots_delta,
        "expected_lots_delta": expected_lots_delta,
        "contract_field_issues": contract_field_issues,
        "source": "final_action_contract",
        "single_source_of_trade_truth": bool(contract.get("single_source_of_trade_truth")),
    }


def _safe_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _group_summary(rows: Iterable[Dict[str, Any]], key_fields: List[str]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = tuple(row.get(field) for field in key_fields)
        grouped[key].append(row)

    summary = []
    for key, items in grouped.items():
        payload = {field: key[idx] for idx, field in enumerate(key_fields)}
        payload.update(summarize_trade_pairs(items))
        summary.append(payload)
    return sorted(summary, key=lambda row: float(row.get("total_pnl") or 0.0))


def _attach_open_recommendation_context(
    pairs: List[Dict[str, Any]],
    recommendations_by_id: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    enriched = []
    for pair in pairs:
        item = dict(pair)
        recommendation = recommendations_by_id.get(str(pair.get("open_recommendation_id") or ""))
        snapshot = {}
        if recommendation:
            snapshot = _deserialize_json(recommendation.get("signal_snapshot")) or {}
            if not isinstance(snapshot, dict):
                snapshot = {}
        item["signal_combo"] = _signal_combo(snapshot)
        item["market_confirmation"] = snapshot.get("market_confirmation") if isinstance(snapshot, dict) else None
        auditor = _trade_auditor(snapshot)
        item["trade_auditor"] = auditor
        item["decision_planner"] = auditor
        item["trade_auditor_decision"] = auditor.get("decision", "none") if auditor else "none"
        item["planner_decision"] = item["trade_auditor_decision"]
        rebalance_summary = _rebalance_summary_from_snapshot(snapshot)
        item["rebalance_summary"] = rebalance_summary
        item["rebalance_action_type"] = (
            rebalance_summary.get("action_type", "unknown") if rebalance_summary else "unknown"
        )
        item["rebalance_reason"] = rebalance_summary.get("reason", "none") if rebalance_summary else "none"
        item["holding_days"] = rebalance_summary.get("holding_days") if rebalance_summary else None
        item["rebalance_turnover_notional"] = _safe_float(
            rebalance_summary.get("turnover_notional_estimate") if rebalance_summary else 0.0
        )
        enriched.append(item)
    return enriched


def _recommendation_diagnostics(recommendations: List[Dict[str, Any]]) -> Dict[str, Any]:
    control_reason_counts: Counter = Counter()
    no_trade_reason_counts: Counter = Counter()
    market_scores: List[float] = []
    market_feature_counts: Counter = Counter()
    market_confirmation_count = 0
    trade_auditor_decision_counts: Counter = Counter()
    trade_auditor_reason_counts: Counter = Counter()
    rebalance_action_counts: Counter = Counter()
    rebalance_reason_counts: Counter = Counter()
    rebalance_control_reason_counts: Counter = Counter()
    rebalance_contract_field_issue_counts: Counter = Counter()
    rebalance_holding_days: List[float] = []
    rebalance_turnover_notional: List[float] = []
    finoview_group_counts: Counter = Counter()
    finoview_attribution_count = 0
    finoview_missing_attribution_count = 0
    finoview_invalid_attribution_count = 0
    finoview_empty_group_count = 0
    artifact_validation_total = 0
    artifact_validation_pass = 0
    free_text_control_violation_count = 0
    strategy_recommendation_count = 0

    for recommendation in recommendations:
        if _source_type(recommendation.get("source_type")) != "strategy":
            continue
        strategy_recommendation_count += 1

        snapshot = _deserialize_json(recommendation.get("signal_snapshot")) or {}
        if not isinstance(snapshot, dict):
            continue

        if "artifact_validation_errors" in snapshot:
            artifact_validation_total += 1
            if not snapshot.get("artifact_validation_errors"):
                artifact_validation_pass += 1

        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        if isinstance(contract.get("strategy_controls"), str):
            free_text_control_violation_count += 1
        for reason in (contract.get("reason_codes") or []) + (contract.get("risk_flags") or []):
            control_reason_counts[str(reason)] += 1

        context = _fundamental_context_from_snapshot(snapshot)
        attribution = context.get("finoview_factor_attribution") if context else None
        if not isinstance(attribution, dict) or not attribution:
            finoview_missing_attribution_count += 1
            if attribution is not None and not isinstance(attribution, dict):
                finoview_invalid_attribution_count += 1
        else:
            finoview_attribution_count += 1
            groups = attribution.get("covered_required_groups") or []
            if isinstance(groups, (str, bytes)):
                groups = [groups]
            elif not isinstance(groups, IterableABC):
                groups = [groups]
            group_values = [group for group in groups if group]
            if not group_values:
                finoview_empty_group_count += 1
            for group in group_values:
                finoview_group_counts[str(group)] += 1

        rebalance_summary = _rebalance_summary_from_snapshot(snapshot)
        if rebalance_summary:
            rebalance_action_counts[str(rebalance_summary.get("action_type") or "unknown")] += 1
            rebalance_reason_counts[str(rebalance_summary.get("reason") or "none")] += 1
            for reason in rebalance_summary.get("control_reasons") or []:
                rebalance_control_reason_counts[str(reason)] += 1
            for issue in rebalance_summary.get("contract_field_issues") or []:
                rebalance_contract_field_issue_counts[str(issue)] += 1
            if rebalance_summary.get("holding_days") is not None:
                rebalance_holding_days.append(_safe_float(rebalance_summary.get("holding_days")))
            rebalance_turnover_notional.append(
                _safe_float(rebalance_summary.get("turnover_notional_estimate"))
            )

        auditor = _trade_auditor(snapshot)
        if auditor:
            trade_auditor_decision_counts[str(auditor.get("decision") or "unknown")] += 1
            for reason in auditor.get("reasons") or []:
                trade_auditor_reason_counts[str(reason)] += 1

        execution_result = snapshot.get("execution_result") if isinstance(snapshot.get("execution_result"), dict) else {}
        no_trade_reason = execution_result.get("no_trade_reason")
        if no_trade_reason:
            no_trade_reason_counts[str(no_trade_reason)] += 1

        market_confirmation = snapshot.get("market_confirmation")
        if isinstance(market_confirmation, dict) and market_confirmation.get("enabled"):
            market_confirmation_count += 1
            market_scores.append(float(market_confirmation.get("confirmation_score") or 0.0))
            for feature in market_confirmation.get("features") or []:
                if isinstance(feature, dict):
                    feature_name = feature.get("feature") or feature.get("name") or str(feature)
                else:
                    feature_name = feature
                market_feature_counts[str(feature_name)] += 1

    avg_score = sum(market_scores) / len(market_scores) if market_scores else 0.0
    return {
        "strategy_recommendations": strategy_recommendation_count,
        "market_confirmation_count": market_confirmation_count,
        "avg_confirmation_score": avg_score,
        "control_reason_counts": dict(control_reason_counts.most_common()),
        "no_trade_reason_counts": dict(no_trade_reason_counts.most_common()),
        "market_feature_counts": dict(market_feature_counts.most_common()),
        "trade_auditor_decision_counts": dict(trade_auditor_decision_counts.most_common()),
        "trade_auditor_reason_counts": dict(trade_auditor_reason_counts.most_common()),
        "planner_decision_counts": dict(trade_auditor_decision_counts.most_common()),
        "planner_reason_counts": dict(trade_auditor_reason_counts.most_common()),
        "rebalance_action_counts": dict(rebalance_action_counts.most_common()),
        "rebalance_reason_counts": dict(rebalance_reason_counts.most_common()),
        "rebalance_control_reason_counts": dict(rebalance_control_reason_counts.most_common()),
        "rebalance_contract_field_issue_counts": dict(rebalance_contract_field_issue_counts.most_common()),
        "finoview_factor_group_counts": dict(finoview_group_counts.most_common()),
        "finoview_factor_attribution_count": finoview_attribution_count,
        "finoview_factor_missing_attribution_count": finoview_missing_attribution_count,
        "finoview_factor_invalid_attribution_count": finoview_invalid_attribution_count,
        "finoview_factor_empty_group_count": finoview_empty_group_count,
        "finoview_factor_attribution_coverage_rate": (
            finoview_attribution_count / strategy_recommendation_count
            if strategy_recommendation_count else 1.0
        ),
        "artifact_contract_validation_pass_rate": (
            artifact_validation_pass / artifact_validation_total if artifact_validation_total else 1.0
        ),
        "free_text_control_violation_count": free_text_control_violation_count,
        "avg_holding_days": (
            sum(rebalance_holding_days) / len(rebalance_holding_days)
            if rebalance_holding_days else 0.0
        ),
        "avg_rebalance_turnover_notional": (
            sum(rebalance_turnover_notional) / len(rebalance_turnover_notional)
            if rebalance_turnover_notional else 0.0
        ),
        "total_rebalance_turnover_notional": sum(rebalance_turnover_notional),
    }


def _rollover_summary(transactions: List[Dict[str, Any]]) -> Dict[str, Any]:
    rollover_transactions = [
        row for row in transactions
        if _source_type(row.get("source_type")) == "rollover"
    ]
    by_action: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "lots": 0, "commission": 0.0})
    for row in rollover_transactions:
        action = str(row.get("action") or "")
        by_action[action]["count"] += 1
        by_action[action]["lots"] += int(row.get("lots") or 0)
        by_action[action]["commission"] += float(row.get("commission") or 0.0)

    return {
        "transaction_count": len(rollover_transactions),
        "total_lots": sum(int(row.get("lots") or 0) for row in rollover_transactions),
        "total_commission": sum(float(row.get("commission") or 0.0) for row in rollover_transactions),
        "by_action": dict(by_action),
    }


def _forced_risk_summary(transactions: List[Dict[str, Any]]) -> Dict[str, Any]:
    forced_risk_transactions = [
        row for row in transactions
        if _source_type(row.get("source_type")) == "forced_risk"
    ]
    by_action: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "lots": 0, "commission": 0.0})
    for row in forced_risk_transactions:
        action = str(row.get("action") or "")
        by_action[action]["count"] += 1
        by_action[action]["lots"] += int(row.get("lots") or 0)
        by_action[action]["commission"] += float(row.get("commission") or 0.0)

    return {
        "transaction_count": len(forced_risk_transactions),
        "total_lots": sum(int(row.get("lots") or 0) for row in forced_risk_transactions),
        "total_commission": sum(float(row.get("commission") or 0.0) for row in forced_risk_transactions),
        "by_action": dict(by_action),
    }


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _format_table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "_No data_"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def _top_counter_rows(counter: Dict[str, Any], limit: int = 12) -> List[List[Any]]:
    rows = []
    for key, value in list(counter.items())[:limit]:
        rows.append([key, value])
    return rows


def _readable_weak_suggestion(row: Dict[str, Any], scope: str) -> str:
    ticker = row.get("ticker", "*")
    side = row.get("side", "*")
    combo = row.get("signal_combo")
    win_rate = _safe_float(row.get("win_rate"))
    total_pnl = _safe_float(row.get("total_pnl"))
    if scope == "combo":
        return (
            f"Review {ticker} {side} / {combo} as a weak setup candidate; Researcher should verify "
            "whether future PM evidence needs stronger current confirmation."
        )
    if win_rate < 0.30 or total_pnl <= -2500:
        return (
            f"Review {ticker} {side} as a severe weak-side candidate; do not convert this report into "
            f"a trading rule without Researcher action-value evidence. Recent win_rate={win_rate:.2%}, "
            f"net_pnl={total_pnl:.2f}."
        )
    return (
        f"Review {ticker} {side} as a soft weak-side candidate; PM may use only structured "
        f"action-value evidence and current confirmation, not this text. Recent win_rate={win_rate:.2%}, "
        f"net_pnl={total_pnl:.2f}."
    )


def _append_weak_suggestion(
    suggestions: List[Dict[str, Any]],
    seen: set,
    row: Dict[str, Any],
    *,
    scope: str,
    min_trades: int,
) -> None:
    total_trades = int(row.get("total_trades") or 0)
    if total_trades < min_trades:
        return
    if _safe_float(row.get("win_rate")) >= 0.40 and _safe_float(row.get("total_pnl")) >= 0:
        return
    key = (
        scope,
        row.get("ticker"),
        row.get("side"),
        row.get("signal_combo", "*"),
    )
    if key in seen:
        return
    seen.add(key)
    suggestions.append(
        {
            **row,
            "scope": scope,
            "signal_combo": row.get("signal_combo", "*"),
            "suggestion": _readable_weak_suggestion(row, scope),
        }
    )


def _release_block_summary_from_recommendations(recommendations: List[Dict[str, Any]]) -> Dict[str, Any]:
    primary_reason_counts: Counter = Counter()
    category_counts: Counter = Counter()
    next_evidence_counts: Counter = Counter()
    evidence_counts: Counter = Counter()
    observation_only_violations = 0
    total = 0
    samples: List[Dict[str, Any]] = []

    for recommendation in recommendations:
        if _source_type(recommendation.get("source_type")) != "strategy":
            continue
        snapshot = _deserialize_json(recommendation.get("signal_snapshot")) or {}
        if not isinstance(snapshot, dict):
            continue
        diagnostics = snapshot.get("release_block_diagnostics")
        if not isinstance(diagnostics, dict) or not diagnostics:
            continue
        total += 1
        if not diagnostics.get("observation_only") or not diagnostics.get("does_not_modify_trade_authority"):
            observation_only_violations += 1
        primary_reason = str(diagnostics.get("primary_block_reason") or "none")
        category = str(diagnostics.get("blocking_category") or "unknown")
        primary_reason_counts[primary_reason] += 1
        category_counts[category] += 1
        for item in diagnostics.get("next_evidence_needed") or []:
            next_evidence_counts[str(item)] += 1
        evidence = diagnostics.get("evidence_snapshot") if isinstance(diagnostics.get("evidence_snapshot"), dict) else {}
        if evidence.get("current_evidence_present"):
            evidence_counts["current_evidence_present"] += 1
        if evidence.get("invalidation_present"):
            evidence_counts["invalidation_present"] += 1
        if evidence.get("watch_for_trigger_block"):
            evidence_counts["watch_for_trigger_block"] += 1
        if len(samples) < 12:
            samples.append({
                "trading_date": _normalize_date(recommendation.get("trading_date")),
                "ticker": diagnostics.get("ticker"),
                "primary_block_reason": primary_reason,
                "blocking_category": category,
                "preferred_side": evidence.get("preferred_side"),
                "preferred_side_state": evidence.get("preferred_side_state"),
                "current_evidence_present": bool(evidence.get("current_evidence_present")),
                "invalidation_present": bool(evidence.get("invalidation_present")),
            })

    return {
        "total": total,
        "observation_only_violations": observation_only_violations,
        "primary_reason_counts": dict(primary_reason_counts.most_common()),
        "category_counts": dict(category_counts.most_common()),
        "next_evidence_needed_counts": dict(next_evidence_counts.most_common()),
        "evidence_counts": dict(evidence_counts.most_common()),
        "samples": samples,
        "audit_boundary": "read_only_attribution; does_not_create_trade_authority_or_modify_lots",
    }


def _action_value_summary_from_recommendations(recommendations: List[Dict[str, Any]]) -> Dict[str, Any]:
    lane_counts: Counter = Counter()
    preference_counts: Counter = Counter()
    scope_quality_counts: Counter = Counter()
    reward_source_counts: Counter = Counter()
    source_counts: Counter = Counter()
    missing_preference_count = 0
    total = 0
    samples: List[Dict[str, Any]] = []

    for recommendation in recommendations:
        if _source_type(recommendation.get("source_type")) != "strategy":
            continue
        snapshot = _deserialize_json(recommendation.get("signal_snapshot")) or {}
        if not isinstance(snapshot, dict):
            continue
        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        learning_used = contract.get("learning_used") if isinstance(contract.get("learning_used"), dict) else {}
        action_values = learning_used.get("alpha_setup_action_values") if isinstance(learning_used, dict) else []
        if not isinstance(action_values, list):
            continue
        for item in action_values:
            if not isinstance(item, dict):
                continue
            total += 1
            lane = str(item.get("action_name") or "unknown")
            preference = str(item.get("action_preference") or "").strip()
            scope_quality = str(item.get("amplification_scope_quality") or "unknown")
            reward_source = str(item.get("reward_source") or item.get("source") or "unknown")
            source = str(item.get("source") or "unknown")
            lane_counts[lane] += 1
            scope_quality_counts[scope_quality] += 1
            reward_source_counts[reward_source] += 1
            source_counts[source] += 1
            if preference:
                preference_counts[preference] += 1
            else:
                missing_preference_count += 1
            if len(samples) < 12:
                samples.append({
                    "trading_date": _normalize_date(recommendation.get("trading_date")),
                    "ticker": item.get("ticker"),
                    "side": item.get("side"),
                    "action_name": lane,
                    "action_preference": preference or "none",
                    "scope_quality": scope_quality,
                    "reward_source": reward_source,
                    "sample_count": item.get("sample_count"),
                    "reward_mean": item.get("reward_mean"),
                })

    return {
        "total": total,
        "lane_counts": dict(lane_counts.most_common()),
        "action_preference_counts": dict(preference_counts.most_common()),
        "scope_quality_counts": dict(scope_quality_counts.most_common()),
        "reward_source_counts": dict(reward_source_counts.most_common()),
        "source_counts": dict(source_counts.most_common()),
        "missing_action_preference_count": missing_preference_count,
        "samples": samples,
        "audit_boundary": "read_only_attribution; action_values_are_diagnostic_not_trade_authority",
    }


def _rebalance_pair_summary(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    action_counts: Counter = Counter()
    reason_counts: Counter = Counter()
    control_reason_counts: Counter = Counter()
    holding_days: List[float] = []
    total_turnover_notional = 0.0
    count = 0

    for row in rows:
        rebalance = row.get("rebalance_summary")
        if not isinstance(rebalance, dict) or not rebalance:
            continue
        count += 1
        action_counts[str(rebalance.get("action_type") or row.get("rebalance_action_type") or "unknown")] += 1
        reason_counts[str(rebalance.get("reason") or row.get("rebalance_reason") or "none")] += 1
        for reason in rebalance.get("control_reasons") or []:
            control_reason_counts[str(reason)] += 1
        if rebalance.get("holding_days") is not None:
            holding_days.append(_safe_float(rebalance.get("holding_days")))
        total_turnover_notional += _safe_float(rebalance.get("turnover_notional_estimate"))

    return {
        "count": count,
        "action_counts": dict(action_counts.most_common()),
        "reason_counts": dict(reason_counts.most_common()),
        "control_reason_counts": dict(control_reason_counts.most_common()),
        "avg_holding_days": sum(holding_days) / len(holding_days) if holding_days else 0.0,
        "total_turnover_notional": total_turnover_notional,
    }


def _write_markdown_legacy(path: Path, payload: Dict[str, Any]) -> None:
    diagnostics = payload["recommendation_diagnostics"]
    rollover = payload["rollover_summary"]
    lines = [
        f"# 策略归因报告 - {payload['exp_name']}",
        "",
        f"- 配置 ID：`{payload['config_id']}`",
        f"- 评估区间：{payload['date_range']['start']} 至 {payload['date_range']['end']}",
        f"- 生成时间：{payload['generated_at']}",
        "",
        "## 总览",
        "",
        _format_table(
            ["指标", "数值"],
            [
                ["完整交易对数", payload["overall"]["total_trades"]],
                ["盈利交易", payload["overall"]["winning_trades"]],
                ["亏损交易", payload["overall"]["losing_trades"]],
                ["胜率", f"{payload['overall']['win_rate']:.2%}"],
                ["净 PnL", f"{payload['overall']['total_pnl']:.2f}"],
                ["平均每笔收益率", f"{payload['overall']['avg_return']:.2%}"],
                ["非换约交易对数", payload["strategy_only_overall"]["total_trades"]],
                ["非换约交易对胜率", f"{payload['strategy_only_overall']['win_rate']:.2%}"],
                ["非换约交易对净 PnL", f"{payload['strategy_only_overall']['total_pnl']:.2f}"],
            ],
        ),
        "",
        "## 分品种方向",
        "",
        _format_table(
            ["品种", "方向", "交易对数", "胜率", "净 PnL"],
            [
                [
                    row["ticker"],
                    row["side"],
                    row["total_trades"],
                    f"{row['win_rate']:.2%}",
                    f"{row['total_pnl']:.2f}",
                ]
                for row in payload["by_ticker_side"]
            ],
        ),
        "",
        "## 三分析师信号组合",
        "",
        _format_table(
            ["信号组合 technical|fundamental|commodity_news", "交易对数", "胜率", "净 PnL"],
            [
                [
                    row["signal_combo"],
                    row["total_trades"],
                    f"{row['win_rate']:.2%}",
                    f"{row['total_pnl']:.2f}",
                ]
                for row in payload["by_signal_combo"]
            ],
        ),
        "",
        "## 策略控制审计",
        "",
        _format_table(
            ["项目", "数值"],
            [
                ["策略 recommendation 数", diagnostics["strategy_recommendations"]],
                ["PandaAI 确认记录数", diagnostics["market_confirmation_count"]],
                ["平均确认分数", f"{diagnostics['avg_confirmation_score']:.2f}"],
            ],
        ),
        "",
        "### 控制原因分布",
        "",
        _format_table(["原因", "次数"], _top_counter_rows(diagnostics["control_reason_counts"])),
        "",
        "### 零成交原因分布",
        "",
        _format_table(["原因", "次数"], _top_counter_rows(diagnostics["no_trade_reason_counts"])),
        "",
        "### PandaAI 扩展特征使用分布",
        "",
        _format_table(["特征", "次数"], _top_counter_rows(diagnostics["market_feature_counts"])),
        "",
        "### Trade auditor 决策分布",
        "",
        _format_table(["决策", "次数"], _top_counter_rows(diagnostics.get("trade_auditor_decision_counts", {}))),
        "",
        "### Trade auditor 原因分布",
        "",
        _format_table(["原因", "次数"], _top_counter_rows(diagnostics.get("trade_auditor_reason_counts", {}))),
        "",
        "### Finoview 因子归因覆盖",
        "",
        _format_table(["因子组", "出现次数"], _top_counter_rows(diagnostics.get("finoview_factor_group_counts", {}))),
        "",
        "### Artifact 契约与自由文本控制",
        "",
        _format_table(
            ["项目", "数值"],
            [
                ["Artifact contract pass rate", f"{diagnostics.get('artifact_contract_validation_pass_rate', 1.0):.2%}"],
                ["Free-text control violations", diagnostics.get("free_text_control_violation_count", 0)],
            ],
        ),
        "",
        "## Trade auditor 后交易表现",
        "",
        _format_table(
            ["trade auditor 决策", "交易对数", "胜率", "净 PnL"],
            [
                [
                    row.get("trade_auditor_decision", row.get("planner_decision")),
                    row["total_trades"],
                    f"{row['win_rate']:.2%}",
                    f"{row['total_pnl']:.2f}",
                ]
                for row in payload.get("by_trade_auditor_decision", payload.get("by_planner_decision", []))
            ],
        ),
        "",
        "## 条件化品种方向信号组合",
        "",
        _format_table(
            ["品种", "方向", "信号组合", "交易对数", "胜率", "净 PnL"],
            [
                [
                    row["ticker"],
                    row["side"],
                    row["signal_combo"],
                    row["total_trades"],
                    f"{row['win_rate']:.2%}",
                    f"{row['total_pnl']:.2f}",
                ]
                for row in payload.get("by_ticker_side_signal_combo", [])
            ],
        ),
        "",
        "## 换约流水摘要",
        "",
        _format_table(
            ["指标", "数值"],
            [
                ["换约流水笔数", rollover["transaction_count"]],
                ["换约总手数", rollover["total_lots"]],
                ["换约手续费", f"{rollover['total_commission']:.2f}"],
            ],
        ),
        "",
        _format_table(
            ["动作", "笔数", "手数", "手续费"],
            [
                [action, row["count"], row["lots"], f"{row['commission']:.2f}"]
                for action, row in rollover["by_action"].items()
            ],
        ),
        "",
        "## 弱方向建议",
        "",
        _format_table(
            ["品种", "方向", "交易对数", "胜率", "净 PnL", "建议"],
            [
                [
                    row["ticker"],
                    row["side"],
                    row["total_trades"],
                    f"{row['win_rate']:.2%}",
                    f"{row['total_pnl']:.2f}",
                    row["suggestion"],
                ]
                for row in payload["weak_side_suggestions"]
            ],
        ),
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _format_table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "_No data_"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def _performance_rows(rows: List[Dict[str, Any]], keys: List[str]) -> List[List[Any]]:
    table_rows: List[List[Any]] = []
    for row in rows:
        prefix = [row.get(key, "") for key in keys]
        table_rows.append(
            prefix
            + [
                row.get("total_trades", 0),
                f"{_safe_float(row.get('win_rate')):.2%}",
                f"{_safe_float(row.get('total_pnl')):.2f}",
                f"{_safe_float(row.get('avg_pnl')):.2f}",
            ]
        )
    return table_rows


def _write_markdown(path: Path, payload: Dict[str, Any]) -> None:
    diagnostics = payload["recommendation_diagnostics"]
    rollover = payload["rollover_summary"]
    overall = payload["overall"]
    strategy_overall = payload["strategy_only_overall"]
    rebalance_pairs = payload.get("rebalance_pair_summary", {})

    lines = [
        f"# 策略归因报告 - {payload['exp_name']}",
        "",
        f"- 配置 ID：`{payload['config_id']}`",
        f"- 评估区间：{payload['date_range']['start']} 至 {payload['date_range']['end']}",
        f"- 生成时间：{payload['generated_at']}",
        "",
        "## 总览",
        "",
        _format_table(
            ["指标", "数值"],
            [
                ["全部完成交易对", overall.get("total_trades", 0)],
                ["全部胜率", f"{_safe_float(overall.get('win_rate')):.2%}"],
                ["全部净 PnL", f"{_safe_float(overall.get('total_pnl')):.2f}"],
                ["策略交易对", strategy_overall.get("total_trades", 0)],
                ["策略胜率", f"{_safe_float(strategy_overall.get('win_rate')):.2%}"],
                ["策略净 PnL", f"{_safe_float(strategy_overall.get('total_pnl')):.2f}"],
            ],
        ),
        "",
        "## 按品种与方向",
        "",
        _format_table(
            ["品种", "方向", "交易对", "胜率", "净 PnL", "平均 PnL"],
            _performance_rows(payload.get("by_ticker_side", []), ["ticker", "side"]),
        ),
        "",
        "## 信号组合表现",
        "",
        _format_table(
            ["信号组合", "交易对", "胜率", "净 PnL", "平均 PnL"],
            _performance_rows(payload.get("by_signal_combo", []), ["signal_combo"]),
        ),
        "",
        "## 持仓与再平衡归因",
        "",
        _format_table(
            ["指标", "数值"],
            [
                ["写入再平衡摘要的策略建议数", rebalance_pairs.get("count", 0)],
                ["平均持仓天数", f"{_safe_float(diagnostics.get('avg_holding_days')):.2f}"],
                [
                    "估算总换手名义金额",
                    f"{_safe_float(diagnostics.get('total_rebalance_turnover_notional')):.2f}",
                ],
                [
                    "估算平均换手名义金额",
                    f"{_safe_float(diagnostics.get('avg_rebalance_turnover_notional')):.2f}",
                ],
            ],
        ),
        "",
        "### 全部建议的再平衡动作",
        "",
        _format_table(["动作", "次数"], _top_counter_rows(diagnostics.get("rebalance_action_counts", {}))),
        "",
        "### 全部建议的再平衡原因",
        "",
        _format_table(["原因", "次数"], _top_counter_rows(diagnostics.get("rebalance_reason_counts", {}))),
        "",
        "### 再平衡控制原因",
        "",
        _format_table(
            ["控制原因", "次数"],
            _top_counter_rows(diagnostics.get("rebalance_control_reason_counts", {})),
        ),
        "",
        "### 完成交易按再平衡动作",
        "",
        _format_table(
            ["再平衡动作", "交易对", "胜率", "净 PnL", "平均 PnL"],
            _performance_rows(payload.get("by_rebalance_action_type", []), ["rebalance_action_type"]),
        ),
        "",
        "### 完成交易按再平衡原因",
        "",
        _format_table(
            ["再平衡原因", "交易对", "胜率", "净 PnL", "平均 PnL"],
            _performance_rows(payload.get("by_rebalance_reason", []), ["rebalance_reason"]),
        ),
        "",
        "## 控制与审计",
        "",
        _format_table(
            ["指标", "数值"],
            [
                ["策略建议数", diagnostics.get("strategy_recommendations", 0)],
                ["PandaAI 市场确认次数", diagnostics.get("market_confirmation_count", 0)],
                ["PandaAI 平均确认分", f"{_safe_float(diagnostics.get('avg_confirmation_score')):.4f}"],
            ],
        ),
        "",
        "### 策略控制原因",
        "",
        _format_table(["原因", "次数"], _top_counter_rows(diagnostics.get("control_reason_counts", {}))),
        "",
        "### 未交易原因",
        "",
        _format_table(["原因", "次数"], _top_counter_rows(diagnostics.get("no_trade_reason_counts", {}))),
        "",
        "### PandaAI 特征使用",
        "",
        _format_table(["特征", "次数"], _top_counter_rows(diagnostics.get("market_feature_counts", {}))),
        "",
        "### Auditor 决策",
        "",
        _format_table(["决策", "交易对", "胜率", "净 PnL", "平均 PnL"], _performance_rows(payload.get("by_trade_auditor_decision", []), ["trade_auditor_decision"])),
        "",
        _format_table(["原因", "次数"], _top_counter_rows(diagnostics.get("trade_auditor_reason_counts", {}))),
        "",
        "## 条件组合",
        "",
        _format_table(
            ["品种", "方向", "信号组合", "交易对", "胜率", "净 PnL", "平均 PnL"],
            _performance_rows(payload.get("by_ticker_side_signal_combo", []), ["ticker", "side", "signal_combo"]),
        ),
        "",
        "## 换月流水摘要",
        "",
        _format_table(
            ["指标", "数值"],
            [
                ["换月流水笔数", rollover["transaction_count"]],
                ["换月总手数", rollover["total_lots"]],
                ["换月手续费", f"{_safe_float(rollover['total_commission']):.2f}"],
            ],
        ),
        "",
        _format_table(
            ["动作", "笔数", "手数", "手续费"],
            [
                [action, row["count"], row["lots"], f"{_safe_float(row['commission']):.2f}"]
                for action, row in rollover["by_action"].items()
            ],
        ),
        "",
        "## 弱方向建议",
        "",
        _format_table(
            ["品种", "方向", "交易对", "胜率", "净 PnL", "建议"],
            [
                [
                    row["ticker"],
                    row["side"],
                    row["total_trades"],
                    f"{_safe_float(row['win_rate']):.2%}",
                    f"{_safe_float(row['total_pnl']):.2f}",
                    row["suggestion"],
                ]
                for row in payload["weak_side_suggestions"]
            ],
        ),
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_markdown_readable(path: Path, payload: Dict[str, Any]) -> None:
    diagnostics = payload["recommendation_diagnostics"]
    rollover = payload["rollover_summary"]
    forced_risk = payload.get("forced_risk_summary", {})
    overall = payload["overall"]
    strategy_overall = payload["strategy_only_overall"]
    rebalance_pairs = payload.get("rebalance_pair_summary", {})
    release_summary = payload.get("release_block_summary", {})
    action_value_summary = payload.get("action_value_summary", {})

    lines = [
        f"# Strategy Attribution Report - {payload['exp_name']}",
        "",
        f"- Config ID: `{payload['config_id']}`",
        f"- Evaluation window: {payload['date_range']['start']} to {payload['date_range']['end']}",
        f"- Generated at: {payload['generated_at']}",
        "",
        "## Overview",
        "",
        _format_table(
            ["Metric", "Value"],
            [
                ["All completed trade pairs", overall.get("total_trades", 0)],
                ["All win rate", f"{_safe_float(overall.get('win_rate')):.2%}"],
                ["All net PnL", f"{_safe_float(overall.get('total_pnl')):.2f}"],
                ["Strategy trade pairs", strategy_overall.get("total_trades", 0)],
                ["Strategy win rate", f"{_safe_float(strategy_overall.get('win_rate')):.2%}"],
                ["Strategy net PnL", f"{_safe_float(strategy_overall.get('total_pnl')):.2f}"],
            ],
        ),
        "",
        "## Performance By Ticker And Side",
        "",
        "Strategy-only view; rollover and forced_risk operational pairs are excluded.",
        "",
        _format_table(
            ["Ticker", "Side", "Trade Pairs", "Win Rate", "Net PnL", "Avg PnL"],
            _performance_rows(payload.get("by_ticker_side", []), ["ticker", "side"]),
        ),
        "",
        "## Performance By Signal Combo",
        "",
        "Strategy-only view; operational actions do not become analyst or PM attribution.",
        "",
        _format_table(
            ["Signal Combo", "Trade Pairs", "Win Rate", "Net PnL", "Avg PnL"],
            _performance_rows(payload.get("by_signal_combo", []), ["signal_combo"]),
        ),
        "",
        "## Holding And Rebalance Attribution",
        "",
        _format_table(
            ["Metric", "Value"],
            [
                ["Recommendations with rebalance summary", rebalance_pairs.get("count", 0)],
                ["Average holding days", f"{_safe_float(diagnostics.get('avg_holding_days')):.2f}"],
                ["Estimated total rebalance turnover notional", f"{_safe_float(diagnostics.get('total_rebalance_turnover_notional')):.2f}"],
                ["Estimated average rebalance turnover notional", f"{_safe_float(diagnostics.get('avg_rebalance_turnover_notional')):.2f}"],
            ],
        ),
        "",
        "### Rebalance Actions",
        "",
        _format_table(["Action", "Count"], _top_counter_rows(diagnostics.get("rebalance_action_counts", {}))),
        "",
        "### Rebalance Reasons",
        "",
        _format_table(["Reason", "Count"], _top_counter_rows(diagnostics.get("rebalance_reason_counts", {}))),
        "",
        "### Control Reasons",
        "",
        _format_table(["Control Reason", "Count"], _top_counter_rows(diagnostics.get("rebalance_control_reason_counts", {}))),
        "",
        "### Final Contract Field Issues",
        "",
        _format_table(["Issue", "Count"], _top_counter_rows(diagnostics.get("rebalance_contract_field_issue_counts", {}))),
        "",
        "## Release Block Diagnostics",
        "",
        _format_table(
            ["Metric", "Value"],
            [
                ["Diagnostics rows", release_summary.get("total", 0)],
                ["Observation-only violations", release_summary.get("observation_only_violations", 0)],
            ],
        ),
        "",
        "### Release Block Categories",
        "",
        _format_table(["Category", "Count"], _top_counter_rows(release_summary.get("category_counts", {}))),
        "",
        "### Release Primary Reasons",
        "",
        _format_table(["Reason", "Count"], _top_counter_rows(release_summary.get("primary_reason_counts", {}))),
        "",
        "### Next Evidence Needed",
        "",
        _format_table(["Evidence", "Count"], _top_counter_rows(release_summary.get("next_evidence_needed_counts", {}))),
        "",
        "## Action-Value Usage",
        "",
        _format_table(
            ["Metric", "Value"],
            [
                ["Action-value rows seen by final contracts", action_value_summary.get("total", 0)],
                ["Missing action_preference rows", action_value_summary.get("missing_action_preference_count", 0)],
            ],
        ),
        "",
        "### Action-Value Lanes",
        "",
        _format_table(["Lane", "Count"], _top_counter_rows(action_value_summary.get("lane_counts", {}))),
        "",
        "### Action Preferences",
        "",
        _format_table(["Preference", "Count"], _top_counter_rows(action_value_summary.get("action_preference_counts", {}))),
        "",
        "### Scope Quality",
        "",
        _format_table(["Scope", "Count"], _top_counter_rows(action_value_summary.get("scope_quality_counts", {}))),
        "",
        "### Reward Sources",
        "",
        _format_table(["Reward Source", "Count"], _top_counter_rows(action_value_summary.get("reward_source_counts", {}))),
        "",
        "## Control And Audit",
        "",
        _format_table(
            ["Metric", "Value"],
            [
                ["Strategy recommendations", diagnostics.get("strategy_recommendations", 0)],
                ["PandaAI market confirmations", diagnostics.get("market_confirmation_count", 0)],
                ["Average PandaAI confirmation score", f"{_safe_float(diagnostics.get('avg_confirmation_score')):.4f}"],
                [
                    "Finoview attribution coverage",
                    f"{_safe_float(diagnostics.get('finoview_factor_attribution_coverage_rate')):.2%}",
                ],
                [
                    "Finoview attribution missing/invalid",
                    (
                        f"{diagnostics.get('finoview_factor_missing_attribution_count', 0)}"
                        f"/{diagnostics.get('finoview_factor_invalid_attribution_count', 0)}"
                    ),
                ],
                [
                    "Finoview attribution with no covered groups",
                    diagnostics.get("finoview_factor_empty_group_count", 0),
                ],
            ],
        ),
        "",
        "### Strategy Control Reasons",
        "",
        _format_table(["Reason", "Count"], _top_counter_rows(diagnostics.get("control_reason_counts", {}))),
        "",
        "### No-Trade Reasons",
        "",
        _format_table(["Reason", "Count"], _top_counter_rows(diagnostics.get("no_trade_reason_counts", {}))),
        "",
        "### PandaAI Feature Usage",
        "",
        _format_table(["Feature", "Count"], _top_counter_rows(diagnostics.get("market_feature_counts", {}))),
        "",
        "### Finoview Factor Coverage",
        "",
        _format_table(["Factor Group", "Count"], _top_counter_rows(diagnostics.get("finoview_factor_group_counts", {}))),
        "",
        "### Trade Auditor",
        "",
        _format_table(
            ["Decision", "Trade Pairs", "Win Rate", "Net PnL", "Avg PnL"],
            _performance_rows(payload.get("by_trade_auditor_decision", []), ["trade_auditor_decision"]),
        ),
        "",
        _format_table(["Reason", "Count"], _top_counter_rows(diagnostics.get("trade_auditor_reason_counts", {}))),
        "",
        "## Conditional Setups",
        "",
        _format_table(
            ["Ticker", "Side", "Signal Combo", "Trade Pairs", "Win Rate", "Net PnL", "Avg PnL"],
            _performance_rows(payload.get("by_ticker_side_signal_combo", []), ["ticker", "side", "signal_combo"]),
        ),
        "",
        "## Rollover Summary",
        "",
        _format_table(
            ["Metric", "Value"],
            [
                ["Rollover transaction count", rollover["transaction_count"]],
                ["Rollover lots", rollover["total_lots"]],
                ["Rollover commission", f"{_safe_float(rollover['total_commission']):.2f}"],
            ],
        ),
        "",
        "## Forced Risk Summary",
        "",
        _format_table(
            ["Metric", "Value"],
            [
                ["Forced-risk transaction count", forced_risk.get("transaction_count", 0)],
                ["Forced-risk lots", forced_risk.get("total_lots", 0)],
                ["Forced-risk commission", f"{_safe_float(forced_risk.get('total_commission')):.2f}"],
            ],
        ),
        "",
        "## Weak Side Suggestions",
        "",
        "These are review candidates only; they are not PM rules, risk blocks, or trade authority.",
        "",
        _format_table(
            ["Scope", "Ticker", "Side", "Signal Combo", "Trade Pairs", "Win Rate", "Net PnL", "Suggestion"],
            [
                [
                    row.get("scope", "side"),
                    row.get("ticker", "*"),
                    row.get("side", "*"),
                    row.get("signal_combo", "*"),
                    row.get("total_trades", 0),
                    f"{_safe_float(row.get('win_rate')):.2%}",
                    f"{_safe_float(row.get('total_pnl')):.2f}",
                    row.get("suggestion", ""),
                ]
                for row in payload["weak_side_suggestions"]
            ],
        ),
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def build_attribution_report(
    *,
    config_id: str,
    exp_name: str,
    db_path: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Dict[str, Any]:
    transactions = _fetch_rows(
        db_path,
        """
        SELECT *
        FROM futures_transactions
        WHERE config_id = ?
        ORDER BY substr(trading_date, 1, 10), created_at, id
        """,
        (config_id,),
    )
    transactions = [
        row for row in transactions
        if _date_in_window(row.get("trading_date"), start_date, end_date)
    ]

    recommendation_columns = {
        row["name"]
        for row in _fetch_rows(db_path, "PRAGMA table_info(futures_recommendation)")
    }
    snapshot_artifact_cols = (
        ", signal_snapshot_artifact_path, signal_snapshot_sha256"
        if {"signal_snapshot_artifact_path", "signal_snapshot_sha256"}.issubset(recommendation_columns)
        else ""
    )
    recommendations = _fetch_rows(
        db_path,
        f"""
        SELECT id, trading_date, source_type, status, action, lots, signal_snapshot{snapshot_artifact_cols}
        FROM futures_recommendation
        WHERE config_id = ?
        """,
        (config_id,),
    )
    recommendations = [
        row for row in recommendations
        if _date_in_window(row.get("trading_date"), start_date, end_date)
    ]
    recommendations_by_id = {str(row["id"]): row for row in recommendations}

    pairs = build_completed_trade_pairs(transactions)
    pairs = _attach_open_recommendation_context(pairs, recommendations_by_id)
    strategy_only_pairs = [pair for pair in pairs if not pair.get("contains_non_strategy")]

    overall = summarize_trade_pairs(pairs)
    strategy_only_overall = summarize_trade_pairs(strategy_only_pairs)
    by_ticker_side = _group_summary(strategy_only_pairs, ["ticker", "side"])
    by_signal_combo = _group_summary(strategy_only_pairs, ["signal_combo"])
    by_trade_auditor_decision = _group_summary(strategy_only_pairs, ["trade_auditor_decision"])
    by_planner_decision = by_trade_auditor_decision
    by_ticker_side_signal_combo = _group_summary(strategy_only_pairs, ["ticker", "side", "signal_combo"])
    by_rebalance_action_type = _group_summary(strategy_only_pairs, ["rebalance_action_type"])
    by_rebalance_reason = _group_summary(strategy_only_pairs, ["rebalance_reason"])
    rebalance_pair_summary = _rebalance_pair_summary(strategy_only_pairs)
    release_block_summary = _release_block_summary_from_recommendations(recommendations)
    action_value_summary = _action_value_summary_from_recommendations(recommendations)

    weak_side_suggestions = []
    for row in by_ticker_side:
        if int(row["total_trades"]) < 5:
            continue
        if float(row["win_rate"]) < 0.40 or float(row["total_pnl"]) < 0:
            suggestion = "降低新开仓频率；需要 PandaAI 盘前确认后再开仓"
            if float(row["win_rate"]) < 0.30:
                suggestion = "严重低胜率方向；默认阻止弱信号新开仓"
            weak_side_suggestions.append({**row, "suggestion": suggestion})

    weak_seen = set()
    weak_side_suggestions = []
    for row in by_ticker_side:
        _append_weak_suggestion(
            weak_side_suggestions,
            weak_seen,
            row,
            scope="side",
            min_trades=2,
        )
    for row in by_ticker_side_signal_combo:
        _append_weak_suggestion(
            weak_side_suggestions,
            weak_seen,
            row,
            scope="combo",
            min_trades=1,
        )

    transaction_dates = [_normalize_date(row.get("trading_date")) for row in transactions if row.get("trading_date")]
    close_dates = [_normalize_date(row.get("close_date")) for row in pairs if row.get("close_date")]
    date_start = start_date or (min(transaction_dates) if transaction_dates else (min(close_dates) if close_dates else ""))
    date_end = end_date or (max(transaction_dates) if transaction_dates else (max(close_dates) if close_dates else ""))

    return {
        "exp_name": exp_name,
        "config_id": config_id,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date_range": {"start": date_start, "end": date_end},
        "overall": overall,
        "strategy_only_overall": strategy_only_overall,
        "by_ticker_side": by_ticker_side,
        "by_signal_combo": by_signal_combo,
        "by_trade_auditor_decision": by_trade_auditor_decision,
        "by_planner_decision": by_planner_decision,
        "by_ticker_side_signal_combo": by_ticker_side_signal_combo,
        "by_rebalance_action_type": by_rebalance_action_type,
        "by_rebalance_reason": by_rebalance_reason,
        "rebalance_pair_summary": rebalance_pair_summary,
        "release_block_summary": release_block_summary,
        "action_value_summary": action_value_summary,
        "weak_side_suggestions": weak_side_suggestions,
        "recommendation_diagnostics": _recommendation_diagnostics(recommendations),
        "rollover_summary": _rollover_summary(transactions),
        "forced_risk_summary": _forced_risk_summary(transactions),
        "trade_pairs": pairs,
        "strategy_only_trade_pairs": strategy_only_pairs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a read-only futures strategy attribution report")
    parser.add_argument("--config", type=str, required=True, help="Path to configuration file")
    parser.add_argument("--local-db", action="store_true", help="Use local SQLite database")
    parser.add_argument("--start-date", type=str, default=None, help="Optional YYYY-MM-DD start date")
    parser.add_argument("--end-date", type=str, default=None, help="Optional YYYY-MM-DD end date")
    args = parser.parse_args()
    args.config = resolve_config_path(args.config)

    load_dotenv()
    with open(args.config, "r", encoding="utf-8") as config_file:
        cfg = yaml.safe_load(config_file)
    if cfg.get("market_type") != "china_futures":
        raise RuntimeError("analyze_strategy_attribution.py only supports china_futures")
    if not args.local_db:
        raise RuntimeError("analyze_strategy_attribution.py requires --local-db")

    db_initialize(use_local_db=True)
    db = get_db()
    config_id = db.get_config_id_by_name(cfg["exp_name"])
    if not config_id:
        raise RuntimeError(f"Config {cfg['exp_name']} does not exist in local database")

    report = build_attribution_report(
        config_id=config_id,
        exp_name=cfg["exp_name"],
        db_path=DB_PATH,
        start_date=args.start_date,
        end_date=args.end_date,
    )

    out_dir = Path(__file__).resolve().parents[1] / "logs" / "attribution"
    json_path = out_dir / f"{config_id}.json"
    md_path = out_dir / f"{config_id}.md"
    _write_json(json_path, report)
    _write_markdown_readable(md_path, report)
    logger.info(f"Attribution report written: {json_path}")
    logger.info(f"Attribution report written: {md_path}")
    print(f"Wrote attribution report:\n{json_path}\n{md_path}")


if __name__ == "__main__":
    main()
