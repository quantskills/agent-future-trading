from __future__ import annotations

"""Read-only research snapshot reports produced by the researcher learning entry."""

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from graph.schema import RecommendationSourceType
from tools.agent_tools.research.neutral_accountability import build_neutral_accountability_summary
from tools.agent_tools.research import phase4_review as _phase4
from util.logger import logger


SRC_ROOT = Path(__file__).resolve().parents[3]


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
    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / f"{trading_date}.md"
    json_path = report_dir / f"{trading_date}.json"

    template_where = (
        "config_id = ? AND sample_count > 0 "
        "AND (valid_until IS NULL OR valid_until >= ?)"
    )
    positive_templates = _phase4._report_rows(
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
    weak_templates = _phase4._report_rows(
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
    analyst_digests = _phase4._report_rows(
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
    overlays = _phase4._report_rows(
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
    historical_adaptive_policy_snapshot = _phase4._report_rows(
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
    historical_capital_deployment_rows = _phase4._report_rows(
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
    events = _phase4._report_rows(
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
    learned_vs_unlearned = _phase4._learned_vs_unlearned_trade_performance(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
    )
    causal_rule_validation = _phase4._causal_rule_validation_summary(
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
    neutral_rows = _phase4._report_rows(
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
        item["signal_snapshot"] = _phase4._recommendation_snapshot(item)
        neutral_recommendations.append(item)
    neutral_accountability = build_neutral_accountability_summary(neutral_recommendations, cfg)
    neutral_accountability["counterfactual_tracking"] = _phase4._neutral_counterfactual_tracking_summary(
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
        "written_at": _phase4._utc_now(),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

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
            f"- current_margin_ratio: {_phase4._percent(capital_state.get('current_margin_ratio'))}",
            f"- target_margin_ratio_min: {_phase4._percent(capital_state.get('target_margin_ratio_min'))}",
            f"- target_margin_ratio_max: {_phase4._percent(capital_state.get('target_margin_ratio_max'))}",
            f"- reason_bucket: {capital_state.get('reason_bucket', 'unknown')}",
        ]
    )
    deployment_plan = _phase4._json_loads(capital_state.get("deployment_plan_json")) or {}
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
                    f"confirm={_phase4._percent(item.get('confirmation_score'))}, "
                    f"target={_phase4._percent(item.get('target_position_ratio'))}, "
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
    lines.append(_phase4._trade_performance_report_line("learned", learned_vs_unlearned.get("learned") or {}))
    lines.append(_phase4._trade_performance_report_line("unlearned", learned_vs_unlearned.get("unlearned") or {}))
    lines.append(f"- learned_reason_counts: {learned_vs_unlearned.get('learned_reason_counts', {})}")
    lines.append(f"- learned_effect_counts: {learned_vs_unlearned.get('learned_effect_counts', {})}")
    effect_summary = learned_vs_unlearned.get("learned_effect_summary") or {}
    if isinstance(effect_summary, dict):
        for effect, effect_payload in effect_summary.items():
            lines.append(_phase4._trade_performance_report_line(f"effect:{effect}", effect_payload))
    mechanism_summary = learned_vs_unlearned.get("learning_mechanism_summary") or {}
    lines.append(f"- learning_mechanism_counts: {learned_vs_unlearned.get('learning_mechanism_counts', {})}")
    if isinstance(mechanism_summary, dict):
        for mechanism, mechanism_payload in mechanism_summary.items():
            lines.append(_phase4._trade_performance_report_line(f"mechanism:{mechanism}", mechanism_payload))
    lines.append(f"- sample_status: {learned_vs_unlearned.get('status', 'unknown')}")
    lines.extend(["", "## Neutral Accountability"])
    lines.extend(
        [
            f"- neutral_ratio: {_phase4._percent(neutral_accountability.get('neutral_ratio'))}",
            f"- accountability_complete_rate: {_phase4._percent(neutral_accountability.get('accountability_complete_rate'))}",
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
                f"- total_counterfactual_pnl: {_phase4._signed_money(counterfactual_tracking.get('total_counterfactual_pnl'))}",
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
    lines.extend([_phase4._template_report_line(row) for row in positive_templates] or ["- none"])
    lines.extend(["", "## Failed Templates"])
    lines.extend([_phase4._template_report_line(row) for row in weak_templates] or ["- none"])
    lines.extend(["", "## Historical Adaptive Policy Snapshot"])
    lines.extend(
        [
            (
                f"- {row.get('ticker')}/{row.get('side')}/{row.get('horizon_class')}: "
                f"{row.get('policy_action')} multiplier={row.get('multiplier')} "
                f"confidence={_phase4._percent(row.get('confidence_score'))} reason={row.get('reason')}"
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
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"markdown": str(md_path), "json": str(json_path)}
