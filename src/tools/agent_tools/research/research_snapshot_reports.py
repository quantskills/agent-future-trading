from __future__ import annotations

"""Read-only research snapshot reports produced by the researcher learning entry."""

import json
import os
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from database.artifact_store import write_artifact_text
from graph.schema import RecommendationSourceType
from tools.common.neutral_accountability import build_neutral_accountability_summary
from tools.agent_tools.research import research_review_helpers as _review_helpers
from util.logger import logger


SRC_ROOT = Path(__file__).resolve().parents[3]


def causal_candidate_scope(
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
        _review_helpers.load_externalized_json(
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
            contract = _review_helpers.final_action_contract_from_snapshot(snapshot)
            side = _review_helpers._target_side_from_ratio(contract.get("target_lots"))
            if side in {"long", "short"}:
                sides.add(side)
    return {
        "tickers": sorted(tickers),
        "sides": sorted(sides),
        "evidence_pack_id": candidate.get("evidence_pack_id"),
    }


def learned_vs_unlearned_trade_performance(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    try:
        pairs = _review_helpers._completed_pairs_up_to(cursor, config_id=config_id, trading_date=trading_date)
    except sqlite3.Error:
        pairs = []
    recommendation_lookup = _review_helpers._recommendations_by_id(
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
        tags, effects = _review_helpers._learning_attribution_from_recommendation(recommendation)
        mechanisms = _review_helpers._learning_mechanisms_from_recommendation(recommendation)
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
        "learned": _review_helpers._trade_pair_performance_summary(learned_pairs),
        "unlearned": _review_helpers._trade_pair_performance_summary(unlearned_pairs),
        "learned_reason_counts": _review_helpers._sorted_counter_dict(reason_counts),
        "learned_effect_counts": _review_helpers.learning_effect_counts(learned_pairs),
        "learned_effect_summary": _review_helpers.summarize_pairs_by_learning_effect(learned_pairs),
        "learning_mechanism_counts": _review_helpers.learning_mechanism_counts(learned_pairs),
        "learning_mechanism_summary": _review_helpers.summarize_pairs_by_learning_mechanism(learned_pairs),
        "missing_open_recommendations": missing_recommendations,
    }


def learned_effect_underperformance_groups(
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
        pairs = _review_helpers._completed_pairs_up_to(cursor, config_id=config_id, trading_date=trading_date)
    except sqlite3.Error:
        return []
    recommendation_lookup = _review_helpers._recommendations_by_id(
        cursor,
        [pair.get("open_recommendation_id") for pair in pairs if pair.get("open_recommendation_id")],
    )
    groups: Dict[Tuple[str, str, str, str, str, str], Dict[str, List[Dict[str, Any]]]] = defaultdict(
        lambda: {"learned_effect": [], "benchmark": []}
    )
    tracked_effects = ("alpha_release", "risk_suppression", "evidence_rejection")
    for pair in pairs:
        recommendation = recommendation_lookup.get(str(pair.get("open_recommendation_id") or ""))
        snapshot = _review_helpers._recommendation_snapshot(recommendation or {})
        ticker = str(pair.get("ticker") or "").upper()
        side = str(pair.get("side") or "").lower()
        expected_days = _review_helpers._expected_horizon_days(snapshot, side)
        horizon = _review_helpers._horizon_class(expected_days, snapshot)
        regime = _review_helpers._market_regime(snapshot)
        template = _review_helpers._fac_setup_type(snapshot)
        if not template:
            continue
        key = (ticker, side, template, horizon, regime)
        item = dict(pair)
        item["setup_type"] = template
        item["signal_combo"] = _review_helpers._signal_combo_from_snapshot(snapshot)
        if recommendation:
            tags, effects = _review_helpers._learning_attribution_from_recommendation(recommendation)
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
        learned_summary = _review_helpers._trade_pair_performance_summary(learned_rows)
        benchmark_summary = _review_helpers._trade_pair_performance_summary(benchmark_rows)
        learned_trades = _review_helpers._safe_int(learned_summary.get("total_trades"))
        benchmark_trades = _review_helpers._safe_int(benchmark_summary.get("total_trades"))
        learned_pnl = _review_helpers._safe_float(learned_summary.get("net_pnl"))
        benchmark_pnl = _review_helpers._safe_float(benchmark_summary.get("net_pnl"))
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
            _review_helpers._safe_float(item.get("learned_effect_net_pnl"))
            - _review_helpers._safe_float(item.get("benchmark_net_pnl")),
            -_review_helpers._safe_int(item.get("learned_effect_trades")),
        )
    )
    return underperforming


def causal_rule_validation_summary(
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


def _directional_consensus_from_snapshot(snapshot: Dict[str, Any], neutral_analyst: str) -> Dict[str, Any]:
    counts: Counter = Counter()
    supporters: List[str] = []
    for analyst, payload in _review_helpers._analyst_payloads(snapshot).items():
        if analyst == neutral_analyst:
            continue
        signal = str(payload.get("signal") or "Neutral")
        if signal not in {"Bullish", "Bearish"}:
            continue
        confidence = max(
            _review_helpers._safe_float(payload.get("effective_confidence")),
            _review_helpers._safe_float(payload.get("confidence")),
        )
        if confidence < 0.45:
            continue
        counts[signal] += 1
        supporters.append(f"{analyst}:{signal}")
    if not counts:
        return {"signal": "Neutral", "support_count": 0, "supporters": []}
    signal, support_count = counts.most_common(1)[0]
    return {"signal": signal, "support_count": int(support_count), "supporters": supporters}


def neutral_counterfactual_tracking_summary(
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
        by_ticker = {str(row["ticker"] or "").upper(): _review_helpers._safe_float(row["pnl"]) for row in cursor.fetchall()}
    except sqlite3.Error:
        by_ticker = {}

    observations: List[Dict[str, Any]] = []
    missed_opportunity = 0
    reasonable_avoidance = 0
    for recommendation in recommendations:
        snapshot = recommendation.get("signal_snapshot") if isinstance(recommendation.get("signal_snapshot"), dict) else {}
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "").upper()
        ticker_pnl = by_ticker.get(ticker, 0.0)
        for analyst, payload in _review_helpers._analyst_payloads(snapshot).items():
            if str(payload.get("signal") or "Neutral") != "Neutral":
                continue
            consensus = _directional_consensus_from_snapshot(snapshot, analyst)
            counterfactual_side = consensus.get("signal")
            if counterfactual_side not in {"Bullish", "Bearish"} or _review_helpers._safe_int(consensus.get("support_count")) <= 0:
                continue
            counterfactual_pnl = ticker_pnl if counterfactual_side == "Bullish" else -ticker_pnl
            classification = (
                "missed_opportunity" if counterfactual_pnl > 0
                else "reasonable_avoidance" if counterfactual_pnl < 0
                else "neutral_unresolved"
            )
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
                    "support_count": _review_helpers._safe_int(consensus.get("support_count")),
                    "counterfactual_pnl": counterfactual_pnl,
                    "classification": classification,
                }
            )

    total_counterfactual_pnl = sum(_review_helpers._safe_float(item.get("counterfactual_pnl")) for item in observations)
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
                    str(row["ticker"] or "").upper(): _review_helpers._safe_float(row["pnl"])
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
            for analyst, payload in _review_helpers._analyst_payloads(snapshot).items():
                if str(payload.get("signal") or "Neutral") != "Neutral":
                    continue
                consensus = _directional_consensus_from_snapshot(snapshot, analyst)
                counterfactual_side = consensus.get("signal")
                if counterfactual_side not in {"Bullish", "Bearish"} or _review_helpers._safe_int(consensus.get("support_count")) <= 0:
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
                        "support_count": _review_helpers._safe_int(consensus.get("support_count")),
                        "counterfactual_pnl": counterfactual_pnl,
                        "classification": classification,
                        "window_trading_dates": forward_dates,
                    }
                )
    total_forward_counterfactual_pnl = sum(
        _review_helpers._safe_float(item.get("counterfactual_pnl")) for item in forward_observations
    )
    return {
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


def _write_historical_learning_snapshot_report(
    *,
    cursor: sqlite3.Cursor,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    learning_summary: Dict[str, Any],
    output_root: Optional[Path] = None,
    run_id: Optional[str] = None,
) -> Dict[str, str]:
    """Write a read-only historical learning snapshot for audit and replay."""
    run_key = run_id or getattr(logger, "run_id", None) or "manual"
    configured_log_dir = os.getenv("AGENTQUANT_LOG_DIR")
    default_report_root = Path(configured_log_dir) / "reviewer" if configured_log_dir else SRC_ROOT / "logs" / "reviewer"
    report_dir = (output_root or default_report_root) / str(run_key)
    md_path = report_dir / f"{trading_date}.md"
    json_path = report_dir / f"{trading_date}.json"

    template_where = (
        "config_id = ? AND sample_count > 0 "
        "AND (valid_until IS NULL OR valid_until >= ?)"
    )
    positive_templates = _review_helpers._report_rows(
        cursor,
        f'''
        SELECT *
        FROM setup_type_performance
        WHERE {template_where}
          AND net_pnl > 0
          AND win_rate >= 0.55
        ORDER BY net_pnl DESC, win_rate DESC, confidence_score DESC, sample_count DESC
        LIMIT 10
        ''',
        (config_id, trading_date),
    )
    weak_templates = _review_helpers._report_rows(
        cursor,
        f'''
        SELECT *
        FROM setup_type_performance
        WHERE {template_where}
          AND (net_pnl < 0 OR win_rate <= 0.45)
        ORDER BY net_pnl ASC, win_rate ASC, confidence_score DESC, sample_count DESC
        LIMIT 10
        ''',
        (config_id, trading_date),
    )
    analyst_digests = _review_helpers._report_rows(
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
    overlays = _review_helpers._report_rows(
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
    historical_adaptive_policy_snapshot = _review_helpers._report_rows(
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
    historical_capital_deployment_rows = _review_helpers._report_rows(
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
    events = _review_helpers._report_rows(
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
    learned_vs_unlearned = learned_vs_unlearned_trade_performance(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
    )
    causal_rule_validation = causal_rule_validation_summary(
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
    neutral_rows = _review_helpers._report_rows(
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
        item["signal_snapshot"] = _review_helpers._recommendation_snapshot(item)
        neutral_recommendations.append(item)
    neutral_accountability = build_neutral_accountability_summary(neutral_recommendations, cfg)
    neutral_accountability["counterfactual_tracking"] = neutral_counterfactual_tracking_summary(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        recommendations=neutral_recommendations,
    )

    historical_learning_snapshot = {
        "read_only": True,
        "adaptive_policy_state": historical_adaptive_policy_snapshot,
        "capital_deployment_state": (
            historical_capital_deployment_rows[0] if historical_capital_deployment_rows else None
        ),
        "analyst_learning_digests": analyst_digests,
        "config_learning_overlays": overlays,
        "learning_events": events,
    }

    payload = {
        "run_id": run_key,
        "exp_name": cfg.get("exp_name"),
        "config_id": config_id,
        "trading_date": trading_date,
        "report_boundary": "phase4_read_only_historical_learning_snapshot",
        "learning_summary": learning_summary,
        "positive_templates": positive_templates,
        "weak_templates": weak_templates,
        "historical_learning_snapshot": historical_learning_snapshot,
        "config_overlays": overlays,
        "historical_capital_deployment_snapshot": historical_learning_snapshot["capital_deployment_state"],
        "analyst_learning_digests": analyst_digests,
        "causal_rule_validation": causal_rule_validation,
        "learned_vs_unlearned_performance": learned_vs_unlearned,
        "neutral_accountability": neutral_accountability,
        "learning_events": events,
        "written_at": _review_helpers._utc_now(),
    }
    write_artifact_text(
        json_path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
    )

    lines = [
        f"# Phase4 Historical Learning Snapshot - {trading_date}",
        "",
        f"- run_id: {run_key}",
        f"- exp_name: {cfg.get('exp_name')}",
        f"- config_id: {config_id}",
        "- boundary: read_only_snapshot_for_audit_and_replay",
        "",
        "## Historical Learning Summary",
    ]
    for key, value in learning_summary.items():
        if key == "capital_deployment_state":
            continue
        lines.append(f"- {key}: {value}")

    capital_state = payload["historical_capital_deployment_snapshot"] or {}
    lines.extend(
        [
            "",
            "## Historical Capital Deployment Snapshot",
            f"- current_margin_ratio: {_review_helpers._percent(capital_state.get('current_margin_ratio'))}",
            f"- target_margin_ratio_min: {_review_helpers._percent(capital_state.get('target_margin_ratio_min'))}",
            f"- target_margin_ratio_max: {_review_helpers._percent(capital_state.get('target_margin_ratio_max'))}",
            f"- reason_bucket: {capital_state.get('reason_bucket', 'unknown')}",
        ]
    )
    deployment_plan = _review_helpers._json_loads(capital_state.get("deployment_plan_json")) or {}
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
                    f"confirm={_review_helpers._percent(item.get('confirmation_score'))}, "
                    f"target={_review_helpers._percent(item.get('target_position_ratio'))}, "
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
    lines.append(_review_helpers._trade_performance_report_line("learned", learned_vs_unlearned.get("learned") or {}))
    lines.append(_review_helpers._trade_performance_report_line("unlearned", learned_vs_unlearned.get("unlearned") or {}))
    lines.append(f"- learned_reason_counts: {learned_vs_unlearned.get('learned_reason_counts', {})}")
    lines.append(f"- learned_effect_counts: {learned_vs_unlearned.get('learned_effect_counts', {})}")
    effect_summary = learned_vs_unlearned.get("learned_effect_summary") or {}
    if isinstance(effect_summary, dict):
        for effect, effect_payload in effect_summary.items():
            lines.append(_review_helpers._trade_performance_report_line(f"effect:{effect}", effect_payload))
    mechanism_summary = learned_vs_unlearned.get("learning_mechanism_summary") or {}
    lines.append(f"- learning_mechanism_counts: {learned_vs_unlearned.get('learning_mechanism_counts', {})}")
    if isinstance(mechanism_summary, dict):
        for mechanism, mechanism_payload in mechanism_summary.items():
            lines.append(_review_helpers._trade_performance_report_line(f"mechanism:{mechanism}", mechanism_payload))
    lines.append(f"- sample_status: {learned_vs_unlearned.get('status', 'unknown')}")
    lines.extend(["", "## Neutral Accountability"])
    lines.extend(
        [
            f"- neutral_ratio: {_review_helpers._percent(neutral_accountability.get('neutral_ratio'))}",
            f"- accountability_complete_rate: {_review_helpers._percent(neutral_accountability.get('accountability_complete_rate'))}",
            f"- category_counts: {neutral_accountability.get('category_counts', {})}",
            f"- missing_field_counts: {neutral_accountability.get('missing_field_counts', {})}",
        ]
    )
    counterfactual_tracking = neutral_accountability.get("counterfactual_tracking") or {}
    if isinstance(counterfactual_tracking, dict):
        lines.extend(
            [
                f"- counterfactual_observation_count: {counterfactual_tracking.get('observation_count', 0)}",
                f"- counterfactual_missed_opportunity_count: {counterfactual_tracking.get('missed_opportunity_count', 0)}",
                f"- counterfactual_reasonable_avoidance_count: {counterfactual_tracking.get('reasonable_avoidance_count', 0)}",
                f"- total_counterfactual_pnl: {_review_helpers._signed_money(counterfactual_tracking.get('total_counterfactual_pnl'))}",
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
    lines.extend([_review_helpers._template_report_line(row) for row in positive_templates] or ["- none"])
    lines.extend(["", "## Failed Templates"])
    lines.extend([_review_helpers._template_report_line(row) for row in weak_templates] or ["- none"])
    lines.extend(["", "## Historical Adaptive Policy Snapshot"])
    lines.extend(
        [
            (
                f"- {row.get('ticker')}/{row.get('side')}/{row.get('horizon_class')}: "
                f"{row.get('policy_action')} multiplier={row.get('multiplier')} "
                f"confidence={_review_helpers._percent(row.get('confidence_score'))} reason={row.get('reason')}"
            )
            for row in historical_adaptive_policy_snapshot
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
    write_artifact_text(md_path, "\n".join(lines) + "\n")
    return {"markdown": str(md_path), "json": str(json_path)}
