"""Research memory writer entrypoints used by researcher learning.

This module owns future-learning persistence. Reviewer/Researcher shared
read-only helpers live in research_review_helpers; research tables and future
policy state are written only through this module and the researcher learning
entrypoint.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from apis.contract_info_cache import FuturesContractInfoCache
from database.artifact_store import externalize_json_for_db, load_externalized_json, write_artifact_text
from graph.schema import RecommendationSourceType
from tools.agent_tools.research import research_review_helpers as _review_helpers
from tools.agent_tools.research import research_snapshot_reports as _research_snapshots
from tools.agent_tools.analysis.analyst_data_usage import data_usage_from_snapshot, compact_data_usage_notes
from tools.common.learning_contract import (
    CONTRACT_KEY,
    attach_or_upgrade_next_round_memory_contract,
    attach_next_round_memory_contract,
    build_next_round_memory_contract,
    build_event_memory_contract,
)
from tools.common.contracts import validate_researcher_artifact_boundary
from tools.common.evidence_fusion_semantics import build_reviewer_fusion_attribution
from util.futures_audit import (
    build_execution_learning_trace,
    categorize_no_trade_reason,
    infer_no_trade_reason,
    normalize_no_trade_reason,
)
from util.futures_trade_pairs import (
    build_strategy_originated_trade_pairs_with_diagnostics,
    summarize_trade_pairs,
)
from util.logger import logger
from tools.common.neutral_accountability import build_neutral_accountability_summary
from tools.common.final_action_semantics import (
    canonical_action_family,
    canonical_action_preference_for_action_value,
    canonical_action_value_lane,
    derive_research_fact_state,
    validate_action_preference_family_consistency,
    validate_action_value_write_consistency,
)


PM_LIFECYCLE_CALIBRATION_FIELDS = (
    "trace_version",
    "current_lots",
    "target_lots",
    "lots_delta",
    "pre_learning_position_ratio",
    "final_target_position_ratio",
    "position_ratio_delta",
    "open_add_rank_score_delta",
    "alpha_setup_multiplier",
    "alpha_setup_expectancy_lane",
    "hold_decision",
    "hold_changes_position",
    "reduce_exit_decision",
    "reduce_exit_changes_position",
    "conditional_monitor_decision",
    "execution_profile_changed",
    "execution_profile_learning_direct_to_rank",
)

# Reuse deterministic parsing/report helpers without depending on the Reviewer
# main tool or letting Phase4 own research persistence entrypoints.
for _name in _review_helpers.EXPORTED_RESEARCH_REVIEW_HELPERS:
    globals().setdefault(_name, getattr(_review_helpers, _name))

_causal_candidate_scope = _research_snapshots.causal_candidate_scope
_learned_vs_unlearned_trade_performance = _research_snapshots.learned_vs_unlearned_trade_performance
_learned_effect_underperformance_groups = _research_snapshots.learned_effect_underperformance_groups
_causal_rule_validation_summary = _research_snapshots.causal_rule_validation_summary
_neutral_counterfactual_tracking_summary = _research_snapshots.neutral_counterfactual_tracking_summary


def _json_dumps(value: Any) -> str:
    if isinstance(value, dict):
        validate_researcher_artifact_boundary(value)
    elif isinstance(value, list):
        validate_researcher_artifact_boundary({"research_payload": value})
    return _review_helpers._json_dumps(value)


def build_policy_memory_payload(
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
    """Create future policy-memory payloads from the researcher writer boundary."""
    action = str(policy_action or "").lower()
    is_protect = action in {"protect", "allow"}
    is_reduce = action in {"cap", "reduce", "block", "demote", "probe_only", "weak_block"}
    sample_count = _safe_int((evidence or {}).get("sample_count") or (evidence or {}).get("total_trades"), 0)
    confidence = _safe_float((evidence or {}).get("confidence_score"), 0.0)
    if not confidence:
        confidence = _confidence_from_summary(evidence or {})
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
            "evidence": evidence or {},
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
    return build_policy_memory_payload(
        policy_type=policy_type,
        policy_action=policy_action,
        reason=reason,
        scope=scope,
        evidence=evidence,
        multiplier=multiplier,
        maturity_state=maturity_state,
        status=status,
    )


def _loss_template_policy_payload(
    *,
    reason: str,
    scope: Dict[str, Any],
    evidence: Dict[str, Any],
    multiplier: float,
    maturity_state: str = "validated_loss_template_policy",
) -> Dict[str, Any]:
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
    payload = build_policy_memory_payload(
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
            "researcher",
            scope_type,
            scope_key,
            _json_dumps(evidence),
            _json_dumps(action_payload),
            "deterministic_research_writer",
            _utc_now(),
            status,
        ),
    )
    return event_id


def _insert_researcher_learning_completion_event(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
) -> str:
    event_id = f"researcher_learning_completed:{config_id}:{trading_date}"
    cursor.execute(
        '''
        INSERT INTO learning_event_log (
            id, config_id, trading_date, event_type, agent, scope_type, scope_key,
            evidence_json, action_json, verifier, created_at, status
        ) VALUES (?, ?, ?, 'researcher_learning_completed', 'researcher', 'trading_day', ?, ?, ?, 'deterministic_researcher_entry', datetime('now'), 'applied')
        ''',
        (
            event_id,
            config_id,
            trading_date,
            trading_date,
            "{}",
            "{}",
        ),
    )
    return event_id


def _insert_causal_review_candidate(
    cursor: sqlite3.Cursor,
    *,
    candidate_id: str,
    config_id: str,
    trading_date: str,
    evidence_pack_id: str,
    candidate_type: str,
    confidence_score: float,
    rule_validation_status: str,
    created_at: str,
    valid_until: str,
    payload_json: str,
    ticker: str = "*",
    side: str = "*",
) -> None:
    cursor.execute(
        """
        INSERT INTO causal_review_candidate (
            id, config_id, trading_date, evidence_pack_id, ticker, side,
            candidate_type, confidence_score, rule_validation_status,
            created_at, valid_until, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            config_id,
            trading_date,
            evidence_pack_id,
            ticker,
            side,
            candidate_type,
            confidence_score,
            rule_validation_status,
            created_at,
            valid_until,
            payload_json,
        ),
    )


def _insert_exploratory_hypothesis(
    cursor: sqlite3.Cursor,
    *,
    hypothesis_id: str,
    config_id: str,
    trading_date: str,
    scope_type: str,
    scope_key: str,
    ticker: str,
    sector: str,
    side: str,
    horizon_class: str,
    market_regime: str,
    hypothesis_text: str,
    evidence_summary: str,
    suggested_use: str,
    confidence_score: float,
    sample_count: int,
    status: str,
    created_at: str,
    valid_until: str,
    payload_json: str,
    payload_artifact_path: Optional[str],
    payload_sha256: Optional[str],
    payload_size: Optional[int],
    payload_summary_json: Optional[str],
) -> None:
    cursor.execute(
        """
        INSERT INTO exploratory_hypothesis (
            id, config_id, trading_date, scope_type, scope_key, ticker, sector,
            side, horizon_class, market_regime, hypothesis_text,
            evidence_summary, suggested_use, confidence_score, sample_count,
            status, created_at, valid_until, payload_json,
            payload_artifact_path, payload_sha256, payload_size, payload_summary_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            hypothesis_id,
            config_id,
            trading_date,
            scope_type,
            scope_key,
            ticker,
            sector,
            side,
            horizon_class,
            market_regime,
            hypothesis_text,
            evidence_summary,
            suggested_use,
            confidence_score,
            sample_count,
            status,
            created_at,
            valid_until,
            payload_json,
            payload_artifact_path,
            payload_sha256,
            payload_size,
            payload_summary_json,
        ),
    )


def _upsert_alpha_setup_sample(cursor: sqlite3.Cursor, *, record: Mapping[str, Any]) -> None:
    cursor.execute(
        """
        INSERT INTO alpha_setup_sample (
            id, config_id, trading_date, ticker, side, sector, horizon_class,
            market_regime, setup_type, data_combo, scope_key, source_type,
            recommendation_id, action_taken, pm_action, auditor_decision,
            trader_status, target_lots, executed_lots, net_pnl, commission,
            holding_days, outcome_label, setup_quality_score, opportunity_state,
            evidence_json, result_json, created_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(config_id, trading_date, ticker, side, setup_type, source_type, recommendation_id)
        DO UPDATE SET
            sector=excluded.sector,
            horizon_class=excluded.horizon_class,
            market_regime=excluded.market_regime,
            data_combo=excluded.data_combo,
            scope_key=excluded.scope_key,
            action_taken=excluded.action_taken,
            pm_action=excluded.pm_action,
            auditor_decision=excluded.auditor_decision,
            trader_status=excluded.trader_status,
            target_lots=excluded.target_lots,
            executed_lots=excluded.executed_lots,
            net_pnl=excluded.net_pnl,
            commission=excluded.commission,
            holding_days=excluded.holding_days,
            outcome_label=excluded.outcome_label,
            setup_quality_score=excluded.setup_quality_score,
            opportunity_state=excluded.opportunity_state,
            evidence_json=excluded.evidence_json,
            result_json=excluded.result_json,
            payload_json=excluded.payload_json
        """,
        (
            record.get("id"),
            record.get("config_id"),
            record.get("trading_date"),
            record.get("ticker"),
            record.get("side"),
            record.get("sector"),
            record.get("horizon_class"),
            record.get("market_regime"),
            record.get("setup_type"),
            record.get("data_combo"),
            record.get("scope_key"),
            record.get("source_type"),
            record.get("recommendation_id"),
            record.get("action_taken"),
            record.get("pm_action"),
            record.get("auditor_decision"),
            record.get("trader_status"),
            record.get("target_lots"),
            record.get("executed_lots"),
            record.get("net_pnl"),
            record.get("commission"),
            record.get("holding_days"),
            record.get("outcome_label"),
            record.get("setup_quality_score"),
            record.get("opportunity_state"),
            record.get("evidence_json"),
            record.get("result_json"),
            record.get("created_at"),
            record.get("payload_json"),
        ),
    )


def _upsert_alpha_setup_profile(cursor: sqlite3.Cursor, *, record: Mapping[str, Any]) -> None:
    cursor.execute(
        """
        INSERT INTO alpha_setup_profile (
            id, config_id, ticker, side, sector, horizon_class, market_regime,
            setup_type, data_combo, scope_key, lifecycle_state, profile_state_hint,
            sample_count, trade_count, no_trade_count, win_count, loss_count,
            gross_profit, gross_loss, net_pnl, total_commission, profit_factor,
            win_rate, max_loss, avg_holding_days, confidence_score,
            max_position_impact, last_sample_date, created_at, updated_at,
            valid_until, active, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(config_id, scope_key)
        DO UPDATE SET
            ticker=excluded.ticker,
            side=excluded.side,
            sector=excluded.sector,
            horizon_class=excluded.horizon_class,
            market_regime=excluded.market_regime,
            setup_type=excluded.setup_type,
            data_combo=excluded.data_combo,
            lifecycle_state=excluded.lifecycle_state,
            profile_state_hint=excluded.profile_state_hint,
            sample_count=excluded.sample_count,
            trade_count=excluded.trade_count,
            no_trade_count=excluded.no_trade_count,
            win_count=excluded.win_count,
            loss_count=excluded.loss_count,
            gross_profit=excluded.gross_profit,
            gross_loss=excluded.gross_loss,
            net_pnl=excluded.net_pnl,
            total_commission=excluded.total_commission,
            profit_factor=excluded.profit_factor,
            win_rate=excluded.win_rate,
            max_loss=excluded.max_loss,
            avg_holding_days=excluded.avg_holding_days,
            confidence_score=excluded.confidence_score,
            max_position_impact=excluded.max_position_impact,
            last_sample_date=excluded.last_sample_date,
            updated_at=excluded.updated_at,
            valid_until=excluded.valid_until,
            active=1,
            payload_json=excluded.payload_json
        """,
        (
            record.get("id"),
            record.get("config_id"),
            record.get("ticker"),
            record.get("side"),
            record.get("sector"),
            record.get("horizon_class"),
            record.get("market_regime"),
            record.get("setup_type"),
            record.get("data_combo"),
            record.get("scope_key"),
            record.get("lifecycle_state"),
            record.get("profile_state_hint"),
            record.get("sample_count"),
            record.get("trade_count"),
            record.get("no_trade_count"),
            record.get("win_count"),
            record.get("loss_count"),
            record.get("gross_profit"),
            record.get("gross_loss"),
            record.get("net_pnl"),
            record.get("total_commission"),
            record.get("profit_factor"),
            record.get("win_rate"),
            record.get("max_loss"),
            record.get("avg_holding_days"),
            record.get("confidence_score"),
            record.get("max_position_impact"),
            record.get("last_sample_date"),
            record.get("created_at"),
            record.get("updated_at"),
            record.get("valid_until"),
            record.get("payload_json"),
        ),
    )


PM_CONSUMABLE_ACTION_VALUE_REQUIRED_FIELDS = (
    "canonical_action_family",
    "action_value_lane",
    "learning_lane",
    "consumer_scope",
    "memory_side_role",
    "last_sample_date",
    "valid_until",
    "reward_source",
    "evidence_scope",
)


def _pm_consumable_action_value_missing_fields(record: Mapping[str, Any]) -> list[str]:
    payload = _review_helpers._json_loads(record.get("payload_json")) if isinstance(record, Mapping) else {}
    payload = payload if isinstance(payload, Mapping) else {}
    missing: list[str] = []
    for field in PM_CONSUMABLE_ACTION_VALUE_REQUIRED_FIELDS:
        value = record.get(field) if isinstance(record, Mapping) else None
        if value in (None, ""):
            value = payload.get(field)
        if value in (None, ""):
            missing.append(field)
    return missing


def _normalize_pm_consumable_action_value_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = dict(record or {})
    consumer_scope = str(normalized.get("consumer_scope") or "").strip().lower()
    if consumer_scope != "pm_learning":
        return normalized
    payload = _review_helpers._json_loads(normalized.get("payload_json"))
    payload = payload if isinstance(payload, dict) else {}
    action_name = normalized.get("action_name") or payload.get("action_name")
    family = normalized.get("canonical_action_family") or payload.get("canonical_action_family")
    if not family:
        family = canonical_action_family(action_name)
    lane = (
        normalized.get("action_value_lane")
        or normalized.get("learning_lane")
        or payload.get("action_value_lane")
        or payload.get("learning_lane")
    )
    if not lane:
        lane = canonical_action_value_lane(action_name)
    normalized["canonical_action_family"] = family
    normalized["action_value_lane"] = lane
    normalized["learning_lane"] = normalized.get("learning_lane") or payload.get("learning_lane") or lane
    payload["canonical_action_family"] = family
    payload["action_value_lane"] = normalized["action_value_lane"]
    payload["learning_lane"] = normalized["learning_lane"]
    canonical_preference = canonical_action_preference_for_action_value(normalized)
    current_preference = str(
        normalized.get("action_preference")
        or payload.get("action_preference")
        or ""
    ).strip().lower()
    if canonical_preference and canonical_preference != current_preference:
        payload["original_action_preference"] = current_preference
        payload["action_preference"] = canonical_preference
        payload["action_preference_canonicalized_by"] = "final_action_semantics"
        normalized["action_preference"] = canonical_preference
        normalized["payload_json"] = _json_dumps(payload)

    missing = _pm_consumable_action_value_missing_fields(normalized)
    consistency = validate_action_value_write_consistency(normalized)
    errors = list(consistency.get("errors") or [])
    if missing or errors:
        raise ValueError(
            "pm_consumable_action_value_contract_invalid:"
            f"missing={missing}:errors={errors}"
        )
    normalized["payload_json"] = _json_dumps(payload)
    return normalized


def _upsert_alpha_setup_action_value(cursor: sqlite3.Cursor, *, record: Mapping[str, Any]) -> None:
    record = _normalize_pm_consumable_action_value_record(record)
    cursor.execute(
        """
        INSERT INTO alpha_setup_action_value (
            id, config_id, scope_key, ticker, side, horizon_class, market_regime,
            setup_type, data_combo, action_name, sample_count, reward_sum,
            reward_mean, win_rate, confidence_score, action_preference,
            canonical_action_family,
            reward_source, evidence_scope, action_value_lane,
            consumer_scope, learning_lane, memory_side_role, retrieval_key,
            fallback_retrieval_key, execution_retrieval_key,
            max_position_impact, last_sample_date, created_at, updated_at,
            valid_until, active, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(config_id, scope_key, action_name)
        DO UPDATE SET
            sample_count=excluded.sample_count,
            reward_sum=excluded.reward_sum,
            reward_mean=excluded.reward_mean,
            win_rate=excluded.win_rate,
            confidence_score=excluded.confidence_score,
            action_preference=excluded.action_preference,
            canonical_action_family=excluded.canonical_action_family,
            reward_source=excluded.reward_source,
            evidence_scope=excluded.evidence_scope,
            action_value_lane=excluded.action_value_lane,
            consumer_scope=excluded.consumer_scope,
            learning_lane=excluded.learning_lane,
            memory_side_role=excluded.memory_side_role,
            retrieval_key=excluded.retrieval_key,
            fallback_retrieval_key=excluded.fallback_retrieval_key,
            execution_retrieval_key=excluded.execution_retrieval_key,
            max_position_impact=excluded.max_position_impact,
            last_sample_date=excluded.last_sample_date,
            updated_at=excluded.updated_at,
            valid_until=excluded.valid_until,
            active=1,
            payload_json=excluded.payload_json
        """,
        (
            record.get("id"),
            record.get("config_id"),
            record.get("scope_key"),
            record.get("ticker"),
            record.get("side"),
            record.get("horizon_class"),
            record.get("market_regime"),
            record.get("setup_type"),
            record.get("data_combo"),
            record.get("action_name"),
            record.get("sample_count"),
            record.get("reward_sum"),
            record.get("reward_mean"),
            record.get("win_rate"),
            record.get("confidence_score"),
            record.get("action_preference"),
            record.get("canonical_action_family"),
            record.get("reward_source"),
            record.get("evidence_scope"),
            record.get("action_value_lane"),
            record.get("consumer_scope"),
            record.get("learning_lane"),
            record.get("memory_side_role"),
            record.get("retrieval_key"),
            record.get("fallback_retrieval_key"),
            record.get("execution_retrieval_key"),
            record.get("max_position_impact"),
            record.get("last_sample_date"),
            record.get("created_at"),
            record.get("updated_at"),
            record.get("valid_until"),
            record.get("payload_json"),
        ),
    )


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
    template = str(scope.get("setup_type") or "*")
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
            "setup_type": template,
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
            id, config_id, ticker, side, setup_type, horizon_class, market_regime,
            policy_type, policy_action, multiplier, confidence_score, sample_count,
            reason, source_event_id, created_at, valid_until, payload_json, active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'calibrate', 1.0, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, market_regime, policy_type)
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


def _ensure_research_learning_schema(cursor: sqlite3.Cursor) -> None:
    """Ensure Phase4 research tables exist before writing newer feedback rows."""
    try:
        from database.sqlite_setup import _ensure_reviewer_learning_schema

        _ensure_reviewer_learning_schema(cursor)
    except Exception:
        logger.warning("reviewer_learning_schema_ensure_failed")

def _deactivate_adaptive_policy_state(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    scope: Dict[str, Any],
    policy_type: str,
    reason: str,
) -> int:
    if not policy_type:
        return 0
    try:
        cursor.execute(
            """
            UPDATE adaptive_policy_state
            SET active = 0,
                reason = ?
            WHERE config_id = ?
              AND ticker = ?
              AND side = ?
              AND setup_type = ?
              AND horizon_class = ?
              AND market_regime = ?
              AND policy_type = ?
              AND active = 1
            """,
            (
                reason,
                config_id,
                str(scope.get("ticker") or "*").upper(),
                str(scope.get("side") or "*").lower(),
                str(scope.get("setup_type") or "*"),
                str(scope.get("horizon_class") or "*"),
                str(scope.get("market_regime") or "*"),
                policy_type,
            ),
        )
        return int(cursor.rowcount or 0)
    except sqlite3.Error:
        return 0

def _deactivate_adaptive_policy_state_from_counterfactual_reversal(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    scope: Dict[str, Any],
    policy_type: str,
    counterfactual_reversal: Dict[str, Any],
    reason: str,
) -> int:
    changed = _deactivate_adaptive_policy_state(
        cursor,
        config_id=config_id,
        scope=scope,
        policy_type=policy_type,
        reason=reason,
    )
    examples = counterfactual_reversal.get("examples") if isinstance(counterfactual_reversal, dict) else []
    for item in examples or []:
        if not isinstance(item, dict):
            continue
        memory_id = item.get("memory_id")
        if not memory_id:
            continue
        try:
            cursor.execute(
                """
                SELECT ticker, side, setup_type, horizon_class, market_regime
                FROM no_trade_opportunity_memory
                WHERE config_id = ? AND id = ?
                LIMIT 1
                """,
                (config_id, memory_id),
            )
            row = cursor.fetchone()
        except sqlite3.Error:
            row = None
        if not row:
            continue
        changed += _deactivate_adaptive_policy_state(
            cursor,
            config_id=config_id,
            scope=dict(row),
            policy_type=policy_type,
            reason=reason,
        )
    return changed


def _episode_payload_pair(payload: Dict[str, Any]) -> Dict[str, Any]:
    pair = payload.get("pair") if isinstance(payload.get("pair"), dict) else {}
    return pair


def _episode_payload_trace(payload: Dict[str, Any]) -> Dict[str, Any]:
    trace = payload.get("opportunity_ranking_trace") if isinstance(payload.get("opportunity_ranking_trace"), dict) else {}
    return trace


def _episode_payload_net_pnl(payload: Dict[str, Any]) -> float:
    return _safe_float(_episode_payload_pair(payload).get("net_pnl"))


def _episode_payload_opportunity_score(payload: Dict[str, Any]) -> float:
    return _safe_float(_episode_payload_trace(payload).get("opportunity_score"), -1.0)


def _episode_payload_recommendation_id(payload: Dict[str, Any]) -> str:
    pair = _episode_payload_pair(payload)
    return str(payload.get("open_recommendation_id") or pair.get("open_recommendation_id") or "")


def _select_representative_episode_payload(
    rows: List[Dict[str, Any]],
    effect: str,
) -> Tuple[Dict[str, Any], str]:
    valid_rows = [row for row in rows or [] if isinstance(row, dict)]
    if not valid_rows:
        return {}, "no_representative_episode"
    normalized_effect = str(effect or "").lower()
    if normalized_effect == "lower_priority":
        return min(
            valid_rows,
            key=lambda row: (
                _episode_payload_net_pnl(row),
                -_episode_payload_opportunity_score(row),
                _episode_payload_recommendation_id(row),
            ),
        ), "largest_loss_for_lower_priority"
    if normalized_effect == "raise_priority":
        return max(
            valid_rows,
            key=lambda row: (
                _episode_payload_net_pnl(row),
                _episode_payload_opportunity_score(row),
                _episode_payload_recommendation_id(row),
            ),
        ), "largest_gain_for_raise_priority"
    return max(
        valid_rows,
        key=lambda row: (
            _episode_payload_opportunity_score(row),
            abs(_episode_payload_net_pnl(row)),
            _episode_payload_recommendation_id(row),
        ),
    ), "highest_score_for_observe"


def _write_opportunity_ranking_learning_events(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    cfg: Dict[str, Any],
    episode_payloads: List[Dict[str, Any]],
) -> int:
    policy = cfg.get("opportunity_ranking_learning_policy", {}) or {}
    if not bool(policy.get("enabled", True)):
        return 0
    min_samples = int(policy.get("min_samples_for_ranking_preference", 3) or 3)
    grouped: Dict[Tuple[str, str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for payload in episode_payloads:
        pair = payload.get("pair") if isinstance(payload.get("pair"), dict) else {}
        trace = payload.get("opportunity_ranking_trace") if isinstance(payload.get("opportunity_ranking_trace"), dict) else {}
        score = _safe_float(trace.get("opportunity_score"), -1.0)
        if score < 0:
            continue
        side = str(pair.get("side") or payload.get("candidate_side") or "").lower()
        ticker = str(pair.get("ticker") or "").upper()
        if not ticker or side not in {"long", "short"}:
            continue
        key = (
            ticker,
            side,
            str(payload.get("opportunity_type") or _setup_type(side, _signal_combo_from_snapshot(payload.get("signal_snapshot") or {}), payload.get("signal_snapshot") or {}) or "unknown"),
            str(payload.get("opportunity_state") or "unknown"),
            str((payload.get("signal_snapshot") or {}).get("market_regime") or "unknown"),
        )
        grouped[key].append(payload)
    inserted = 0
    for key, rows in grouped.items():
        if len(rows) < min_samples:
            continue
        net_pnl = sum(_episode_payload_net_pnl(row) for row in rows)
        wins = sum(1 for row in rows if _episode_payload_net_pnl(row) > 0)
        avg_score = sum(_episode_payload_opportunity_score(row) for row in rows) / len(rows)
        win_rate = wins / len(rows) if rows else 0.0
        ticker, side, setup_type, opportunity_state, regime = key
        confidence = min(0.85, 0.35 + 0.08 * len(rows) + min(0.25, abs(net_pnl) / 80000.0))
        effect = "raise_priority" if net_pnl > 0 and win_rate >= 0.55 else "lower_priority" if net_pnl < 0 and win_rate <= 0.45 else "observe"
        multiplier = 1.10 if effect == "raise_priority" else 0.75 if effect == "lower_priority" else 1.0
        representative, representative_selection_reason = _select_representative_episode_payload(rows, effect)
        representative_snapshot = (
            representative.get("signal_snapshot")
            if isinstance(representative.get("signal_snapshot"), dict)
            else {}
        )
        representative_trace = _episode_payload_trace(representative)
        representative_recommendation_id = _episode_payload_recommendation_id(representative)
        evidence = {
            "ticker": ticker,
            "side": side,
            "setup_type": setup_type,
            "opportunity_state": opportunity_state,
            "market_regime": regime,
            "sample_count": len(rows),
            "win_rate": win_rate,
            "net_pnl": net_pnl,
            "profit_factor": _profit_factor([row.get("pair") or {} for row in rows]),
            "avg_opportunity_score": avg_score,
            "source_trading_date": trading_date,
            "source_fields": policy.get("source_fields") or [],
            "not_trade_authority": True,
        }
        action = {
            "policy_type": "opportunity_ranking_preference",
            "policy_action": effect,
            "policy_multiplier": multiplier,
            "action_preference": "positive_candidate_open" if effect == "raise_priority" else "negative_revalidate" if effect == "lower_priority" else "",
            "usage_boundary": {
                "usable_by": ["portfolio_manager", "reviewer", "researcher"],
                "allowed_effects": policy.get("allowed_policy_effects") or [
                    "adjust_pm_opportunity_score",
                    "adjust_capital_allocation_priority",
                ],
                "forbidden_effects": policy.get("forbidden_policy_effects") or [
                    "create_trade_authority",
                    "change_trader_lots",
                    "change_trader_direction",
                    "bypass_final_action_contract",
                ],
            },
            "reason": (
                f"opportunity ranking preference {effect}: samples={len(rows)}, "
                f"win_rate={win_rate:.2%}, net_pnl={net_pnl:.0f}, avg_score={avg_score:.3f}"
            ),
            "confidence_score": confidence,
        }
        _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="opportunity_ranking_preference",
            scope_type="ticker_side_setup_state",
            scope_key=f"{ticker}:{side}:{setup_type}:{opportunity_state}:{regime}",
            evidence=evidence,
            action=action,
            status="candidate",
        )
        fusion_attribution = build_reviewer_fusion_attribution(representative_snapshot)
        _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="evidence_fusion_attribution",
            scope_type="ticker_side",
            scope_key=f"{ticker}:{side}",
            evidence={
                "recommendation_id": representative_recommendation_id,
                "representative_recommendation_id": representative_recommendation_id,
                "ticker": ticker,
                "side": side,
                "aggregation_scope": "opportunity_ranking_group",
                "attribution_scope": "representative_episode",
                "source_episode_count": len(rows),
                "representative_selection_reason": representative_selection_reason,
                "representative_net_pnl": _episode_payload_net_pnl(representative),
                "representative_opportunity_score": _safe_float(
                    representative_trace.get("opportunity_score"),
                    -1.0,
                ),
                "fusion_attribution_label": fusion_attribution.get("fusion_attribution_label"),
                "pm_fusion_diagnostics": fusion_attribution.get("pm_fusion_diagnostics") or {},
                "pm_conflict_resolution": fusion_attribution.get("pm_conflict_resolution") or {},
                "not_trade_authority": True,
            },
            action={
                "consumer_scope": "future_analyst_and_pm_fusion_learning",
                "does_not_modify_same_day_trade_facts": True,
                "does_not_create_trade_authority": True,
                "aggregation_scope": "opportunity_ranking_group",
                "attribution_scope": "representative_episode",
            },
        )
        inserted += 1
    return inserted

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
        template = _setup_type(side, combo, snapshot)
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
        final_contract_ext = externalize_json_for_db(
            _final_action_contract_payload(snapshot),
            category="signal_context",
            record_id=row_id,
            field_name="final_action_contract",
            config_id=config_id,
            trading_date=trading_date,
        )
        cursor.execute(
            '''
            INSERT INTO signal_context_history (
                id, config_id, trading_date, recommendation_id, ticker, side,
                signal_combo, setup_type, horizon_class, expected_horizon_days,
                market_regime, price_stage, price_percentile, entry_trigger, action_name,
                invalidation_level, target_return,
                analyst_signals_json, market_confirmation_json, final_action_contract_json,
                analyst_signals_artifact_path, analyst_signals_sha256,
                analyst_signals_size, analyst_signals_summary_json,
                market_confirmation_artifact_path, market_confirmation_sha256,
                market_confirmation_size, market_confirmation_summary_json,
                final_action_contract_artifact_path, final_action_contract_sha256,
                final_action_contract_size, final_action_contract_summary_json,
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
                _entry_trigger_label(snapshot, side),
                _action_name(recommendation, snapshot),
                _invalidation_level(snapshot),
                None,
                analyst_ext.inline_value,
                market_ext.inline_value,
                final_contract_ext.inline_value,
                analyst_ext.artifact_path,
                analyst_ext.sha256,
                analyst_ext.size_bytes,
                analyst_ext.summary_json,
                market_ext.artifact_path,
                market_ext.sha256,
                market_ext.size_bytes,
                market_ext.summary_json,
                final_contract_ext.artifact_path,
                final_contract_ext.sha256,
                final_contract_ext.size_bytes,
                final_contract_ext.summary_json,
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
        template = _setup_type(side, combo, snapshot)
        item = dict(pair)
        item["setup_type"] = template
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
            INSERT INTO setup_type_performance (
                id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                sample_count, win_rate, net_pnl, avg_pnl, profit_factor,
                confidence_score, last_sample_date, last_updated, valid_until, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, market_regime)
            DO UPDATE SET
                sample_count=excluded.sample_count,
                win_rate=excluded.win_rate,
                net_pnl=excluded.net_pnl,
                avg_pnl=excluded.avg_pnl,
                profit_factor=excluded.profit_factor,
                confidence_score=excluded.confidence_score,
                last_sample_date=excluded.last_sample_date,
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
                trading_date,
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
                last_sample_date, last_updated, valid_until, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_id, analyst, ticker, sector, horizon_class, signal_side)
            DO UPDATE SET
                sample_count=excluded.sample_count,
                hit_rate=excluded.hit_rate,
                avg_pnl=excluded.avg_pnl,
                net_pnl=excluded.net_pnl,
                confidence_score=excluded.confidence_score,
                last_sample_date=excluded.last_sample_date,
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
                trading_date,
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


def _table_columns(cursor: sqlite3.Cursor, table_name: str) -> set[str]:
    try:
        cursor.execute(f'PRAGMA table_info("{table_name}")')
        return {str(row[1]) for row in cursor.fetchall()}
    except sqlite3.Error:
        return set()


def _episode_fact_fields(row: Mapping[str, Any], fields: Tuple[str, ...]) -> Dict[str, Any]:
    return {key: row.get(key) for key in fields if key in row}


def _episode_signal_states(snapshot: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    scc = (
        snapshot.get("signal_collection_contract")
        if isinstance(snapshot.get("signal_collection_contract"), dict)
        else {}
    )
    final_contract = (
        snapshot.get("final_action_contract")
        if isinstance(snapshot.get("final_action_contract"), dict)
        else {}
    )
    technical_contract: Dict[str, Any] = {}
    source_contracts = scc.get("source_contracts") if isinstance(scc.get("source_contracts"), list) else []
    for source in source_contracts:
        if not isinstance(source, dict):
            continue
        action_contract = (
            source.get("action_evidence_contract")
            if isinstance(source.get("action_evidence_contract"), dict)
            else {}
        )
        analyst = str(
            action_contract.get("analyst")
            or source.get("analyst")
            or source.get("source_agent")
            or ""
        ).strip().lower()
        if analyst == "technical":
            technical_contract = action_contract
            break
    return (
        {
            "scc_evidence_fusion": (
                scc.get("evidence_fusion")
                if isinstance(scc.get("evidence_fusion"), dict)
                else {}
            ),
            "fac_evidence_used": (
                final_contract.get("evidence_used")
                if isinstance(final_contract.get("evidence_used"), dict)
                else {}
            ),
        },
        {
            "fac_entry_invalidation_condition": final_contract.get("invalidation"),
            "fac_entry_invalidation_level": final_contract.get("invalidation_level"),
            "fac_position_invalidation_level": final_contract.get("position_invalidation_level"),
            "fac_atr_stop_distance": final_contract.get("atr_stop_distance"),
            "fac_expected_horizon_days": final_contract.get("expected_horizon_days"),
            "fac_exit_hint": final_contract.get("exit_hint"),
            "technical_entry_invalidation_present": technical_contract.get("invalidation_present"),
            "technical_entry_invalidation_condition": technical_contract.get("invalidation_condition"),
            "technical_entry_invalidation_level": technical_contract.get("invalidation_level"),
            "technical_position_invalidation_level": technical_contract.get("position_invalidation_level"),
            "technical_atr_stop_distance": technical_contract.get("atr_stop_distance"),
            "technical_expected_horizon_days": technical_contract.get("expected_horizon_days"),
            "technical_exit_hint": technical_contract.get("exit_hint"),
        },
    )


def _episode_fact_change(
    previous: Optional[Mapping[str, Any]],
    current: Mapping[str, Any],
    *,
    previous_trading_date: Optional[str],
) -> Dict[str, Any]:
    if previous is None:
        return {
            "previous_trading_date": None,
            "changed": False,
            "changed_fields": {},
        }
    changed_fields = {
        key: {"previous": previous.get(key), "current": current.get(key)}
        for key in sorted(set(previous) | set(current))
        if previous.get(key) != current.get(key)
    }
    return {
        "previous_trading_date": previous_trading_date,
        "changed": bool(changed_fields),
        "changed_fields": changed_fields,
    }


def _strategy_originated_pairs_up_to(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
) -> List[Dict[str, Any]]:
    """Read physical economics for strategy-originated rollover lineages.

    This deliberately remains private to complete episode construction.
    Rollover fills must contribute to the original strategy lifecycle's lots,
    costs, and PnL, but must not become standalone template/action learning in
    the other pair-based research writers.
    """
    cursor.execute(
        """
        SELECT *
        FROM futures_transactions
        WHERE config_id = ?
          AND substr(trading_date, 1, 10) <= ?
        ORDER BY substr(trading_date, 1, 10), created_at, id
        """,
        (config_id, str(trading_date)[:10]),
    )
    pairs, _ = build_strategy_originated_trade_pairs_with_diagnostics(
        [dict(row) for row in cursor.fetchall()]
    )
    return [
        pair
        for pair in pairs
        if str(pair.get("close_date") or "") <= str(trading_date)[:10]
        and not bool(pair.get("contains_forced_risk"))
    ]


def _completed_strategy_position_cycles(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    ticker: str,
    side: str,
    trading_date: str,
) -> List[Dict[str, Any]]:
    """Replay strategy-originated exposure into complete 0 -> position -> 0 cycles.

    Rollover fills are operational rather than learning actions.  They are
    nevertheless part of the original strategy position's physical lineage:
    a balanced close/open transfer leaves exposure unchanged, while an
    unmatched rollover close can finish the existing cycle.
    """
    transaction_columns = _table_columns(cursor, "futures_transactions")
    required_columns = {
        "id",
        "config_id",
        "ticker",
        "trading_date",
        "action",
        "lots",
        "source_type",
        "created_at",
    }
    if not required_columns.issubset(transaction_columns):
        return []
    open_action = "open_long" if side == "long" else "open_short"
    close_action = "close_long" if side == "long" else "close_short"
    if side not in {"long", "short"}:
        return []
    cursor.execute(
        '''
        SELECT *
        FROM futures_transactions
        WHERE config_id = ?
          AND UPPER(ticker) = ?
          AND substr(trading_date, 1, 10) <= ?
          AND LOWER(COALESCE(source_type, 'strategy')) IN ('strategy', 'rollover')
          AND action IN (?, ?)
        ORDER BY substr(trading_date, 1, 10), created_at, id
        ''',
        (config_id, ticker, str(trading_date)[:10], open_action, close_action),
    )
    rows = [dict(raw_row) for raw_row in cursor.fetchall()]
    rollover_groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row.get("source_type") or "strategy").strip().lower() != "rollover":
            continue
        rollover_groups[
            (
                str(row.get("trading_date") or "")[:10],
                str(row.get("recommendation_id") or row.get("id") or ""),
            )
        ].append(row)

    position_lots = 0
    active_cycle: Optional[Dict[str, Any]] = None
    completed: List[Dict[str, Any]] = []
    consumed_rollover_groups: set[Tuple[str, str]] = set()
    for row in rows:
        action = str(row.get("action") or "")
        lots = abs(_safe_int(row.get("lots"), 0))
        if lots <= 0:
            continue
        source_type = str(row.get("source_type") or "strategy").strip().lower()
        if source_type == "rollover":
            group_key = (
                str(row.get("trading_date") or "")[:10],
                str(row.get("recommendation_id") or row.get("id") or ""),
            )
            if group_key in consumed_rollover_groups:
                continue
            consumed_rollover_groups.add(group_key)
            group_rows = rollover_groups.get(group_key) or [row]
            if active_cycle is None or position_lots <= 0:
                continue
            active_cycle["transactions"].extend(group_rows)
            opened = sum(
                abs(_safe_int(item.get("lots"), 0))
                for item in group_rows
                if str(item.get("action") or "") == open_action
            )
            closed = sum(
                abs(_safe_int(item.get("lots"), 0))
                for item in group_rows
                if str(item.get("action") or "") == close_action
            )
            position_lots = max(0, position_lots + opened - closed)
            if position_lots == 0:
                final_row = max(
                    group_rows,
                    key=lambda item: (
                        str(item.get("created_at") or ""),
                        str(item.get("id") or ""),
                    ),
                )
                active_cycle["close_date"] = str(final_row.get("trading_date") or "")[:10]
                active_cycle["transaction_ids"] = tuple(
                    str(item.get("id") or "")
                    for item in active_cycle["transactions"]
                    if item.get("id")
                )
                completed.append(active_cycle)
                active_cycle = None
            continue
        if action == open_action:
            if position_lots <= 0:
                active_cycle = {
                    "side": side,
                    "open_date": str(row.get("trading_date") or "")[:10],
                    "open_transaction_id": row.get("id"),
                    "open_recommendation_id": row.get("recommendation_id"),
                    "close_date": None,
                    "transactions": [],
                }
                position_lots = 0
            position_lots += lots
            if active_cycle is not None:
                active_cycle["transactions"].append(row)
            continue
        if action != close_action or position_lots <= 0 or active_cycle is None:
            continue
        active_cycle["transactions"].append(row)
        position_lots = max(0, position_lots - lots)
        if position_lots == 0:
            active_cycle["close_date"] = str(row.get("trading_date") or "")[:10]
            active_cycle["transaction_ids"] = tuple(
                str(item.get("id") or "")
                for item in active_cycle["transactions"]
                if item.get("id")
            )
            completed.append(active_cycle)
            active_cycle = None
    return completed


def _aggregate_cycle_trade_pairs(
    position_cycle: Mapping[str, Any],
    physical_pairs: List[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Aggregate physical closes into one strategy lifecycle outcome."""
    transaction_ids = {
        str(item)
        for item in (position_cycle.get("transaction_ids") or ())
        if item
    }
    cycle_pairs = [
        dict(pair)
        for pair in physical_pairs
        if str(pair.get("open_transaction_id") or "") in transaction_ids
        and str(pair.get("close_transaction_id") or "") in transaction_ids
        and bool(pair.get("strategy_originated", True))
        and not bool(pair.get("contains_forced_risk"))
    ]
    if not cycle_pairs:
        return None
    cycle_pairs.sort(
        key=lambda pair: (
            str(pair.get("close_date") or ""),
            str(pair.get("close_transaction_id") or ""),
        )
    )
    opening_transaction_id = str(position_cycle.get("open_transaction_id") or "")
    opening_pair = next(
        (
            pair
            for pair in cycle_pairs
            if str(pair.get("origin_open_transaction_id") or pair.get("open_transaction_id") or "")
            == opening_transaction_id
        ),
        cycle_pairs[0],
    )
    closing_pair = cycle_pairs[-1]
    strategy_open_lots = sum(
        abs(_safe_int(item.get("lots"), 0))
        for item in (position_cycle.get("transactions") or [])
        if str(item.get("source_type") or "strategy").strip().lower() == "strategy"
        and str(item.get("action") or "") in {"open_long", "open_short"}
    )
    gross_pnl = sum(_safe_float(pair.get("gross_pnl")) for pair in cycle_pairs)
    commission = sum(_safe_float(pair.get("commission")) for pair in cycle_pairs)
    net_pnl = sum(_safe_float(pair.get("net_pnl")) for pair in cycle_pairs)
    total_notional = sum(
        abs(
            _safe_float(pair.get("open_price"))
            * _safe_int(pair.get("lots"), 0)
            * _safe_float(pair.get("contract_multiplier"), 1.0)
        )
        for pair in cycle_pairs
    )
    open_date = str(position_cycle.get("open_date") or opening_pair.get("origin_open_date") or "")[:10]
    close_date = str(position_cycle.get("close_date") or closing_pair.get("close_date") or "")[:10]
    try:
        holding_days = max(
            0,
            (datetime.strptime(close_date, "%Y-%m-%d") - datetime.strptime(open_date, "%Y-%m-%d")).days,
        )
    except (TypeError, ValueError):
        holding_days = 0
    return {
        "ticker": str(opening_pair.get("ticker") or "").upper(),
        "contract_code": opening_pair.get("contract_code"),
        "side": position_cycle.get("side") or opening_pair.get("side"),
        "lots": strategy_open_lots or sum(_safe_int(pair.get("lots"), 0) for pair in cycle_pairs),
        "open_transaction_id": opening_transaction_id or opening_pair.get("origin_open_transaction_id") or opening_pair.get("open_transaction_id"),
        "close_transaction_id": closing_pair.get("close_transaction_id"),
        "open_recommendation_id": position_cycle.get("open_recommendation_id") or opening_pair.get("origin_recommendation_id") or opening_pair.get("open_recommendation_id"),
        "close_recommendation_id": closing_pair.get("close_recommendation_id"),
        "open_source_type": "strategy",
        "close_source_type": closing_pair.get("close_source_type"),
        "origin_source_type": "strategy",
        "origin_recommendation_id": position_cycle.get("open_recommendation_id") or opening_pair.get("origin_recommendation_id"),
        "origin_open_transaction_id": opening_transaction_id,
        "strategy_originated": True,
        "contains_rollover": any(bool(pair.get("contains_rollover")) for pair in cycle_pairs),
        "contains_forced_risk": False,
        "contains_non_strategy": any(bool(pair.get("contains_non_strategy")) for pair in cycle_pairs),
        "open_date": open_date,
        "close_date": close_date,
        "holding_days": holding_days,
        "open_price": opening_pair.get("open_price"),
        "close_price": closing_pair.get("close_price"),
        "contract_multiplier": opening_pair.get("contract_multiplier"),
        "gross_pnl": gross_pnl,
        "commission": commission,
        "net_pnl": net_pnl,
        "return_on_notional": (net_pnl / total_notional) if total_notional else 0.0,
        "physical_pairs": cycle_pairs,
        "physical_pair_count": len(cycle_pairs),
        "episode_economics_aggregated": True,
    }


def _completed_cycle_for_pair(
    cycles: List[Dict[str, Any]],
    pair: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    open_transaction_id = str(
        pair.get("origin_open_transaction_id")
        or pair.get("open_transaction_id")
        or ""
    )
    close_transaction_id = str(pair.get("close_transaction_id") or "")
    if not open_transaction_id or not close_transaction_id:
        return None
    for cycle in cycles:
        transaction_ids = set(cycle.get("transaction_ids") or ())
        if open_transaction_id in transaction_ids and close_transaction_id in transaction_ids:
            return cycle
    return None


def _episode_position_lifecycle_trace(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    ticker: str,
    pair: Mapping[str, Any],
    position_cycle: Mapping[str, Any],
) -> Dict[str, Any]:
    """Collect settled, formal facts for the physical open/close episode.

    The trace is descriptive only.  Pair economics remain owned by
    ``build_completed_trade_pairs`` and are not recomputed from these rows.
    """
    open_date = str(position_cycle.get("open_date") or "")[:10]
    close_date = str(position_cycle.get("close_date") or "")[:10]
    if not open_date or not close_date or close_date < open_date:
        return {}

    recommendations_by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    recommendation_columns = _table_columns(cursor, "futures_recommendation")
    required_recommendation_columns = {
        "id",
        "config_id",
        "underlying_code",
        "trading_date",
        "effective_trade_date",
        "source_type",
        "created_at",
    }
    if required_recommendation_columns.issubset(recommendation_columns):
        cursor.execute(
            '''
            SELECT *
            FROM futures_recommendation
            WHERE config_id = ?
              AND UPPER(underlying_code) = ?
              AND LOWER(COALESCE(source_type, '')) = 'strategy'
              AND substr(COALESCE(NULLIF(effective_trade_date, ''), trading_date), 1, 10)
                  BETWEEN ? AND ?
            ORDER BY substr(COALESCE(NULLIF(effective_trade_date, ''), trading_date), 1, 10),
                     created_at, id
            ''',
            (config_id, ticker, open_date, close_date),
        )
        for raw_row in cursor.fetchall():
            row = dict(raw_row)
            fact_date = str(row.get("effective_trade_date") or row.get("trading_date") or "")[:10]
            if fact_date:
                recommendations_by_date[fact_date].append(row)

    transactions_by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for raw_row in position_cycle.get("transactions") or []:
        if not isinstance(raw_row, Mapping):
            continue
        fact_date = str(raw_row.get("trading_date") or "")[:10]
        if fact_date:
            transactions_by_date[fact_date].append(
                _episode_fact_fields(
                    raw_row,
                    (
                        "id", "recommendation_id", "trading_date", "ticker",
                        "contract_code", "action", "lots", "execution_price",
                        "settle_price", "contract_multiplier", "margin_rate",
                        "margin_used", "commission", "source_type", "execution_phase",
                        "slippage_ticks", "slippage_amount",
                    ),
                )
            )

    ticker_settlements_by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    ticker_pnl_columns = _table_columns(cursor, "ticker_daily_pnl")
    portfolio_columns = _table_columns(cursor, "portfolio")
    if (
        {"portfolio_id", "trading_date", "ticker"}.issubset(ticker_pnl_columns)
        and {"id", "config_id"}.issubset(portfolio_columns)
    ):
        cursor.execute(
            '''
            SELECT tdp.*
            FROM ticker_daily_pnl tdp
            JOIN portfolio p ON p.id = tdp.portfolio_id
            WHERE p.config_id = ?
              AND UPPER(tdp.ticker) = ?
              AND substr(tdp.trading_date, 1, 10) BETWEEN ? AND ?
            ORDER BY substr(tdp.trading_date, 1, 10), tdp.rowid
            ''',
            (config_id, ticker, open_date, close_date),
        )
        for raw_row in cursor.fetchall():
            row = dict(raw_row)
            fact_date = str(row.get("trading_date") or "")[:10]
            if fact_date:
                ticker_settlements_by_date[fact_date].append(
                    _episode_fact_fields(
                        row,
                        (
                            "trading_date", "ticker", "daily_pnl", "commission",
                            "holding_pnl", "new_position_pnl", "close_pnl",
                            "position_type", "lots", "entry_price", "settle_price",
                        ),
                    )
                )

    fact_dates = sorted(
        {
            open_date,
            close_date,
            *recommendations_by_date.keys(),
            *transactions_by_date.keys(),
            *ticker_settlements_by_date.keys(),
        }
    )
    daily_facts: List[Dict[str, Any]] = []
    previous_evidence: Optional[Dict[str, Any]] = None
    previous_invalidation: Optional[Dict[str, Any]] = None
    previous_date: Optional[str] = None
    for fact_date in fact_dates:
        recommendation_facts: List[Dict[str, Any]] = []
        primary_snapshot: Dict[str, Any] = {}
        for recommendation in recommendations_by_date.get(fact_date, []):
            snapshot = _recommendation_snapshot(recommendation)
            if snapshot:
                primary_snapshot = snapshot
            recommendation_facts.append(
                {
                    "recommendation": {
                        key: recommendation.get(key)
                        for key in (
                            "id",
                            "trading_date",
                            "effective_trade_date",
                            "source_type",
                            "underlying_code",
                            "contract_code",
                            "action",
                            "lots",
                            "status",
                        )
                        if key in recommendation
                    },
                    "signal_collection_contract": (
                        snapshot.get("signal_collection_contract")
                        if isinstance(snapshot.get("signal_collection_contract"), dict)
                        else {}
                    ),
                    "final_action_contract": (
                        snapshot.get("final_action_contract")
                        if isinstance(snapshot.get("final_action_contract"), dict)
                        else {}
                    ),
                    "execution_result": _execution_result_from_snapshot(snapshot),
                }
            )
        if primary_snapshot:
            evidence_state, invalidation_state = _episode_signal_states(primary_snapshot)
        else:
            evidence_state = dict(previous_evidence or {})
            invalidation_state = dict(previous_invalidation or {})
        daily_facts.append(
            {
                "trading_date": fact_date,
                "recommendations": recommendation_facts,
                "transactions": transactions_by_date.get(fact_date, []),
                "ticker_settlement_facts": ticker_settlements_by_date.get(fact_date, []),
                "evidence_state": evidence_state,
                "evidence_change": _episode_fact_change(
                    previous_evidence,
                    evidence_state,
                    previous_trading_date=previous_date,
                ),
                "invalidation_state": invalidation_state,
                "invalidation_change": _episode_fact_change(
                    previous_invalidation,
                    invalidation_state,
                    previous_trading_date=previous_date,
                ),
            }
        )
        if primary_snapshot:
            previous_evidence = evidence_state
            previous_invalidation = invalidation_state
            previous_date = fact_date

    return {
        "fact_source": "settled_aec_scc_fac_transaction_and_settlement_records",
        "open_date": open_date,
        "close_date": close_date,
        "open_transaction_id": pair.get("open_transaction_id"),
        "close_transaction_id": pair.get("close_transaction_id"),
        "position_cycle_transaction_ids": list(position_cycle.get("transaction_ids") or ()),
        "daily_facts": daily_facts,
        "economic_result_source": "completed_trade_pair",
        "economic_result_recalculated": False,
    }

def _write_trade_episode_memory(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> int:
    learning_cfg = cfg.get("learning", {}) or {}
    episode_cfg = learning_cfg.get("trade_episode_memory", {}) or {}
    setattr(_write_trade_episode_memory, "last_payloads", [])
    if not bool(episode_cfg.get("enabled", True)):
        return 0
    pairs = _strategy_originated_pairs_up_to(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
    )
    if not pairs:
        return 0
    now = _utc_now()
    inserted = 0
    episode_payloads: List[Dict[str, Any]] = []
    cycle_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    completed_episodes: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for ticker, side in sorted(
        {
            (str(pair.get("ticker") or "").upper(), str(pair.get("side") or "").lower())
            for pair in pairs
            if pair.get("ticker") and str(pair.get("side") or "").lower() in {"long", "short"}
        }
    ):
        cycle_key = (ticker, side)
        cycle_cache[cycle_key] = _completed_strategy_position_cycles(
            cursor,
            config_id=config_id,
            ticker=ticker,
            side=side,
            trading_date=trading_date,
        )
        for position_cycle in cycle_cache[cycle_key]:
            pair = _aggregate_cycle_trade_pairs(position_cycle, pairs)
            if pair and str(pair.get("close_date") or "") <= trading_date:
                completed_episodes.append((position_cycle, pair))

    recommendation_lookup = _recommendations_by_id(
        cursor,
        [pair.get("open_recommendation_id") for _, pair in completed_episodes if pair.get("open_recommendation_id")],
    )
    transaction_lookup = _transactions_by_id(
        cursor,
        [
            tx_id
            for _, pair in completed_episodes
            for tx_id in (pair.get("open_transaction_id"), pair.get("close_transaction_id"))
            if tx_id
        ],
    )
    for position_cycle, pair in completed_episodes:
        recommendation = recommendation_lookup.get(str(pair.get("open_recommendation_id") or ""))
        snapshot = _recommendation_snapshot(recommendation or {})
        ticker = str(pair.get("ticker") or "").upper()
        side = str(pair.get("side") or "").lower()
        combo = _signal_combo_from_snapshot(snapshot)
        expected_days = _expected_horizon_days(snapshot, side)
        horizon = _horizon_class(expected_days, snapshot)
        regime = _market_regime(snapshot)
        final_contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        template = str(final_contract.get("setup_type") or "").strip()
        if not template or template.lower() in {"unknown", "*"}:
            # A completed strategy episode must keep the setup identity frozen by
            # the opening FAC.  Do not infer a replacement from the surrounding
            # SCC, because unrelated news/fundamental evidence can then relabel a
            # technical entry.  A missing identity remains generic, so it can
            # be retained as an episode fact without becoming canonical setup
            # learning.
            template = "generic_trade_setup"
        entry_trigger = str(final_contract.get("entry_trigger") or "").strip()
        trigger_source = str(final_contract.get("trigger_source") or "").strip()
        sector = _sector_for_ticker(cfg, ticker)
        net_pnl = _safe_float(pair.get("net_pnl"))
        episode_date = str(position_cycle.get("close_date") or trading_date or "")
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
        safe_snapshot = _learning_safe_snapshot(snapshot)
        opportunity_ranking_trace = _opportunity_ranking_trace(snapshot, side)
        opening_lifecycle_trace = (
            (final_contract.get("learning_used") or {}).get("pm_lifecycle_learning_trace") or {}
            if isinstance(final_contract.get("learning_used"), dict)
            else {}
        )
        position_lifecycle_trace = dict(opening_lifecycle_trace)
        position_lifecycle_trace.update(
            _episode_position_lifecycle_trace(
                cursor,
                config_id=config_id,
                ticker=ticker,
                pair=pair,
                position_cycle=position_cycle,
            )
        )
        payload = {
            "pair": pair,
            "open_transaction": open_tx,
            "close_transaction": close_tx,
            "open_recommendation_id": pair.get("open_recommendation_id"),
            "setup_type": template,
            "entry_trigger": entry_trigger,
            "trigger_source": trigger_source,
            "signal_snapshot": safe_snapshot,
            "trade_research_contract_summary": _opportunity_contract_summary(snapshot),
            "opportunity_type": _primary_opportunity_type(snapshot, side),
            "opportunity_state": _primary_opportunity_state(snapshot, side),
            "lesson_text": lesson,
            "analyst_payloads": _analyst_payloads(snapshot),
            "data_usage_summary": data_usage,
            "data_usage_notes": data_usage_notes,
            "final_action_contract": final_contract,
            "learning_source": "final_action_contract",
            "opportunity_ranking_trace": opportunity_ranking_trace,
            "learning_to_position_trace": (
                (final_contract.get("learning_used") or {})
                if isinstance(final_contract.get("learning_used"), dict)
                else {}
            ),
            "position_lifecycle_trace": position_lifecycle_trace,
            "loss_template_research_trace": (
                {"reason_codes": final_contract.get("reason_codes") or []}
                if final_contract
                else {}
            ),
            "execution_result": _execution_result_from_snapshot(snapshot),
            "market_confirmation": _market_confirmation(snapshot),
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
                "setup_type": template,
                "horizon_class": horizon,
                "market_regime": regime,
            },
            usable_memory=[
                lesson,
                f"outcome={payload['pair'].get('net_pnl')}; holding_days={payload['pair'].get('holding_days')}",
                f"opportunity_state={payload.get('opportunity_state')}",
                f"opportunity_score={opportunity_ranking_trace.get('opportunity_score')}; rank={opportunity_ranking_trace.get('opportunity_rank')}; allocation_reason={opportunity_ranking_trace.get('capital_allocation_reason')}",
                *data_usage_notes[:3],
            ],
            analysis_strategy_updates=[
                "Use as a comparable case when today's ticker/sector, side, horizon, and signal template are similar.",
                "Ask whether today's analyst evidence repeats or contradicts the drivers in this episode.",
                "Recheck setup quality: entry location, trigger, invalidation, market regime, and data quality before repeating the template.",
                "Compare whether PM opportunity_score/opportunity_rank actually separated this episode from weaker candidates.",
            ],
            trading_strategy_updates=[
                "Use the episode to refine entry/exit/hold reasoning, not as a standalone trade command.",
                "Winning episodes preserve what worked; losing episodes identify what must be rechecked before repeating.",
                "For winning same-scope templates, PM may consider controlled continuation only with current confirmation; for losing templates, PM must revalidate before new/add-on risk.",
                "Ranking feedback may change future PM allocation priority, but cannot create trade authority without a final_action_contract.",
            ],
            validation_plan=[
                "Accumulate same-scope future episodes before treating this as mature template evidence.",
            ],
            sample_count=1,
        )
        episode_payloads.append(payload)
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
                id, config_id, trading_date, ticker, side, sector, setup_type,
                signal_combo, horizon_class, market_regime, episode_date, first_seen_at,
                last_reviewed_at, open_date, close_date, holding_days, net_pnl,
                return_on_notional, outcome_label,
                lesson_text, payload_json, payload_artifact_path, payload_sha256,
                payload_size, payload_summary_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_id, ticker, side, open_date, close_date, setup_type)
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
            evidence={
                "completed_pairs": len(pairs),
                "completed_position_episodes": len(completed_episodes),
            },
            action={"episode_rows": inserted},
            status="applied",
        )
    setattr(_write_trade_episode_memory, "last_payloads", episode_payloads)
    return inserted

def _feedback_memory_ids(rows: Any) -> tuple[str, ...]:
    if not isinstance(rows, list):
        return ()
    identities = {
        str(
            row.get("id")
            or row.get("action_value_id")
            or ""
        ).strip()
        for row in rows
        if isinstance(row, dict)
    }
    identities.discard("")
    return tuple(sorted(identities))


def _strict_fac_feedback_learning_id_sets(
    learning_used: Dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    formal_rows = (
        learning_used.get("alpha_setup_action_values")
        if isinstance(learning_used.get("alpha_setup_action_values"), list)
        else []
    )
    lifecycle_trace = (
        learning_used.get("pm_lifecycle_learning_trace")
        if isinstance(learning_used.get("pm_lifecycle_learning_trace"), dict)
        else {}
    )
    decision_rows = (
        lifecycle_trace.get("decision_learning_rows")
        if isinstance(lifecycle_trace.get("decision_learning_rows"), list)
        else []
    )
    legal_family_lanes = {
        "open_add_new_risk": {"open", "add", "scale", "increase"},
        "hold": {"hold"},
        "reduce_exit": {"reduce", "exit"},
        "conditional_monitor": {"conditional_monitor"},
    }

    def value(row: Dict[str, Any], key: str) -> Any:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        return row.get(key) if row.get(key) not in (None, "") else payload.get(key)

    def identity(row: Dict[str, Any]) -> str:
        return str(value(row, "id") or value(row, "action_value_id") or "").strip()

    def family_and_lane(row: Dict[str, Any]) -> tuple[str, str]:
        family = str(value(row, "canonical_action_family") or "").strip().lower()
        lane = str(
            value(row, "learning_lane")
            or value(row, "action_value_lane")
            or value(row, "lane")
            or ""
        ).strip().lower()
        return family, lane

    def common_legal(row: Any) -> bool:
        if not isinstance(row, dict) or not identity(row):
            return False
        family, lane = family_and_lane(row)
        return bool(
            value(row, "canonical_action_value") is True
            and str(value(row, "consumer_scope") or "").strip().lower() == "pm_learning"
            and lane in legal_family_lanes.get(family, set())
        )

    if (
        not formal_rows
        or not decision_rows
        or not all(
            common_legal(row)
            and validate_action_preference_family_consistency(row).get("ok")
            for row in formal_rows
        )
        or not all(common_legal(row) for row in decision_rows)
    ):
        return (), ()
    formal_id_list = [identity(row) for row in formal_rows]
    decision_id_list = [identity(row) for row in decision_rows]
    if (
        len(set(formal_id_list)) != len(formal_id_list)
        or len(set(decision_id_list)) != len(decision_id_list)
    ):
        return (), ()
    return tuple(sorted(formal_id_list)), tuple(sorted(decision_id_list))


def _backfill_research_position_feedback_from_completed_episodes(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    completed_episode_payloads: Optional[List[Dict[str, Any]]],
) -> int:
    """Backfill the opening feedback row from its settled episode facts.

    The feedback row remains the sole attribution record.  Completed pairs are
    deduplicated by their physical open/close transaction IDs and recomputed as
    a full set so reruns cannot add the same realised result twice.
    """
    grouped_pairs: Dict[Tuple[str, str, str], Dict[Tuple[str, str], Dict[str, Any]]] = defaultdict(dict)
    invalid_groups: set[Tuple[str, str, str]] = set()
    formal_ids_by_group: Dict[Tuple[str, str, str], tuple[str, ...]] = {}
    for episode in completed_episode_payloads or []:
        if not isinstance(episode, dict):
            continue
        pair = episode.get("pair") if isinstance(episode.get("pair"), dict) else {}
        recommendation_id = str(
            episode.get("open_recommendation_id")
            or pair.get("open_recommendation_id")
            or ""
        ).strip()
        ticker = str(pair.get("ticker") or episode.get("ticker") or "").strip().upper()
        open_date = str(pair.get("open_date") or "").strip()[:10]
        close_date = str(pair.get("close_date") or "").strip()[:10]
        open_transaction_id = str(pair.get("open_transaction_id") or "").strip()
        close_transaction_id = str(pair.get("close_transaction_id") or "").strip()
        if (
            not recommendation_id
            or not ticker
            or not open_date
            or not close_date
            or close_date > str(trading_date)[:10]
            or not open_transaction_id
            or not close_transaction_id
        ):
            continue

        final_contract = (
            episode.get("final_action_contract")
            if isinstance(episode.get("final_action_contract"), dict)
            else {}
        )
        learning_used = (
            final_contract.get("learning_used")
            if isinstance(final_contract.get("learning_used"), dict)
            else {}
        )
        formal_alpha_ids, decision_ids = _strict_fac_feedback_learning_id_sets(learning_used)
        if not formal_alpha_ids or formal_alpha_ids != decision_ids:
            continue
        formal_refs, _ = _feedback_learning_refs({"learning_used": learning_used})
        formal_ids = _feedback_memory_ids(formal_refs)
        if formal_ids != formal_alpha_ids:
            continue

        group_key = (recommendation_id, ticker, open_date)
        prior_formal_ids = formal_ids_by_group.get(group_key)
        if prior_formal_ids is not None and prior_formal_ids != formal_ids:
            invalid_groups.add(group_key)
            continue
        formal_ids_by_group[group_key] = formal_ids
        physical_pairs = (
            pair.get("physical_pairs")
            if isinstance(pair.get("physical_pairs"), list)
            else [pair]
        )
        for physical_pair in physical_pairs:
            if not isinstance(physical_pair, dict):
                invalid_groups.add(group_key)
                continue
            physical_open_id = str(physical_pair.get("open_transaction_id") or "").strip()
            physical_close_id = str(physical_pair.get("close_transaction_id") or "").strip()
            if not physical_open_id or not physical_close_id:
                invalid_groups.add(group_key)
                continue
            gross_pnl = _safe_float(physical_pair.get("gross_pnl"))
            commission = _safe_float(physical_pair.get("commission"))
            net_pnl = _safe_float(physical_pair.get("net_pnl"))
            tolerance = max(1e-6, 1e-9 * max(abs(gross_pnl), abs(commission), abs(net_pnl), 1.0))
            if abs((gross_pnl - commission) - net_pnl) > tolerance:
                invalid_groups.add(group_key)
                continue
            pair_key = (physical_open_id, physical_close_id)
            pair_result = {
                "gross_pnl": gross_pnl,
                "commission": commission,
                "net_pnl": net_pnl,
            }
            prior_pair = grouped_pairs[group_key].get(pair_key)
            if prior_pair is not None and prior_pair != pair_result:
                invalid_groups.add(group_key)
                continue
            grouped_pairs[group_key][pair_key] = pair_result

    updated = 0
    for group_key in sorted(grouped_pairs):
        if group_key in invalid_groups:
            continue
        recommendation_id, ticker, open_date = group_key
        cursor.execute(
            '''
            SELECT *
            FROM research_position_feedback
            WHERE config_id = ?
              AND trading_date = ?
              AND ticker = ?
              AND recommendation_id = ?
            LIMIT 1
            ''',
            (config_id, open_date, ticker, recommendation_id),
        )
        row = cursor.fetchone()
        if row is None:
            continue
        feedback = dict(row)
        memory_refs = _review_helpers._json_loads(feedback.get("memory_refs_json")) or []
        expected_ids = formal_ids_by_group.get(group_key, ())
        if not expected_ids or _feedback_memory_ids(memory_refs) != expected_ids:
            continue
        payload = _review_helpers._json_loads(feedback.get("payload_json")) or {}
        if not isinstance(payload, dict):
            continue
        payload_memory_refs = payload.get("memory_refs")
        if not isinstance(payload_memory_refs, list) or _feedback_memory_ids(payload_memory_refs) != expected_ids:
            continue
        outcome = _review_helpers._json_loads(feedback.get("outcome_json")) or {}
        payload_outcome = payload.get("outcome")
        required_outcome_fields = {
            "transaction_pnl",
            "transaction_commission",
            "daily_settlement_pnl",
            "feedback_label",
        }
        if (
            not isinstance(outcome, dict)
            or not isinstance(payload_outcome, dict)
            or not required_outcome_fields.issubset(outcome)
            or not required_outcome_fields.issubset(payload_outcome)
        ):
            continue

        ordered_results = [
            grouped_pairs[group_key][pair_key]
            for pair_key in sorted(grouped_pairs[group_key])
        ]
        gross_pnl = sum(item["gross_pnl"] for item in ordered_results)
        commission = sum(item["commission"] for item in ordered_results)
        net_pnl = sum(item["net_pnl"] for item in ordered_results)
        policy_refs = _review_helpers._json_loads(feedback.get("policy_refs_json")) or []
        trader_effect = _review_helpers._json_loads(feedback.get("trader_effect_json")) or {}
        no_trade_reason = (
            str(trader_effect.get("no_trade_reason") or "")
            if isinstance(trader_effect, dict)
            else ""
        )
        label = _feedback_label(
            memory_refs=memory_refs,
            policy_refs=policy_refs if isinstance(policy_refs, list) else [],
            target_lots=_safe_int(feedback.get("target_lots")),
            executed_lots=_safe_int(feedback.get("executed_lots")),
            pnl=net_pnl,
            no_trade_reason=no_trade_reason,
        )
        for target in (outcome, payload_outcome):
            target["transaction_pnl"] = gross_pnl
            target["transaction_commission"] = commission
            target["feedback_label"] = label
        payload["outcome"] = payload_outcome
        cursor.execute(
            '''
            UPDATE research_position_feedback
            SET outcome_json = ?, feedback_label = ?, payload_json = ?
            WHERE id = ?
            ''',
            (
                _json_dumps(outcome),
                label,
                _json_dumps(payload),
                feedback.get("id"),
            ),
        )
        updated += int(cursor.rowcount > 0)
    return updated


def _write_research_position_feedback(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    strategy_recommendations: List[Dict[str, Any]],
    transactions_by_recommendation: Dict[str, List[Dict[str, Any]]],
    settlement_row: Optional[Dict[str, Any]],
    completed_episode_payloads: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, int]:
    learning_cfg = cfg.get("learning", {}) or {}
    feedback_cfg = learning_cfg.get("position_feedback_loop", {}) or {}
    if not bool(feedback_cfg.get("enabled", True)):
        return {"feedback_rows": 0, "digest_rows": 0}
    _ensure_research_learning_schema(cursor)
    valid_days = int(feedback_cfg.get("valid_days", learning_cfg.get("memory_expires_after_days", 30)) or 30)
    max_digest_rows = int(feedback_cfg.get("max_digest_rows_per_day", 8) or 8)
    min_digest_confidence = float(feedback_cfg.get("min_digest_confidence", 0.20) or 0.20)
    now = _utc_now()
    valid_until = _valid_until(trading_date, valid_days)
    feedback_rows = 0
    digest_rows = 0
    total_settlement_pnl = _safe_float((settlement_row or {}).get("daily_pnl"))
    for recommendation in strategy_recommendations:
        snapshot = _recommendation_snapshot(recommendation)
        final_contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        learning_used = final_contract.get("learning_used") if isinstance(final_contract.get("learning_used"), dict) else {}
        trace = {
            "learning_used": learning_used,
            "source": "final_action_contract",
        }
        memory_refs, policy_refs = _feedback_learning_refs(trace)
        if not memory_refs and not policy_refs:
            continue
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "").upper()
        if not ticker:
            continue
        side = _recommendation_side(recommendation, snapshot)
        combo = _signal_combo_from_snapshot(snapshot)
        horizon = _horizon_class(_expected_horizon_days(snapshot, side), snapshot)
        regime = _market_regime(snapshot)
        template = _setup_type(side, combo, snapshot)
        rec_id = str(recommendation.get("id") or "")
        txs = transactions_by_recommendation.get(rec_id, [])
        executed_lots = sum(abs(_safe_int(tx.get("lots"))) for tx in txs if isinstance(tx, dict))
        # Phase2 rows establish execution facts only.  Final realised outcome is
        # written later from deterministic, fully settled episode pairs.
        tx_pnl = 0.0
        tx_commission = sum(_safe_float(tx.get("commission")) for tx in txs if isinstance(tx, dict))
        execution_result = _execution_result_from_snapshot(snapshot)
        semantic_state = derive_research_fact_state(final_contract, execution_result)
        position_effect = {
            "current_lots": semantic_state.get("current_lots"),
            "target_lots": semantic_state.get("target_lots"),
            "lots_delta": semantic_state.get("lots_delta"),
            "final_target_position_ratio": final_contract.get("target_position_ratio"),
            "final_action": semantic_state.get("action"),
            "lifecycle_state": semantic_state.get("lifecycle_state"),
            "contract_side": semantic_state.get("contract_side"),
            "memory_side_role": semantic_state.get("memory_side_role"),
            "required_memory_lanes": semantic_state.get("required_memory_lanes") or [],
            "source": "final_action_semantics.research_fact_state",
        }
        opportunity_to_position = {
            "pm_lifecycle_learning_trace": learning_used.get("pm_lifecycle_learning_trace") or {},
            "source": "final_action_contract.learning_used",
        }
        target_lots = _safe_int(position_effect.get("target_lots"), 0)
        current_lots = _safe_int(position_effect.get("current_lots"), 0)
        delta_lots = _safe_int(position_effect.get("lots_delta"), target_lots - current_lots)
        target_ratio = _safe_float(position_effect.get("final_target_position_ratio"), 0.0)
        no_trade_reason = str(
            execution_result.get("no_trade_reason")
            or ((final_contract.get("reason_codes") or [None])[-1] if isinstance(final_contract.get("reason_codes"), list) else "")
            or ""
        )
        label = _feedback_label(
            memory_refs=memory_refs,
            policy_refs=policy_refs,
            target_lots=target_lots,
            executed_lots=executed_lots,
            pnl=tx_pnl,
            no_trade_reason=no_trade_reason,
        )
        payload = {
            "learning_to_position_trace": trace,
            "recommendation": {
                "id": rec_id,
                "action": recommendation.get("action"),
                "lots": recommendation.get("lots"),
                "status": recommendation.get("status"),
                "target_position_ratio": target_ratio,
            },
            "memory_refs": memory_refs,
            "policy_refs": policy_refs,
            "pm_effect": position_effect,
            "opportunity_to_position": opportunity_to_position,
            "auditor_effect": {
                "decision": final_contract.get("authority_type") or semantic_state.get("action"),
                "action": semantic_state.get("action"),
                "position_ratio_multiplier": None,
                "diagnostics": learning_used,
                "source": "final_action_semantics.research_fact_state",
            },
            "trader_effect": {
                "transaction_count": len(txs),
                "executed_lots": executed_lots,
                "no_trade_reason": no_trade_reason,
                "execution_result": execution_result,
            },
            "outcome": {
                "transaction_pnl": tx_pnl,
                "transaction_commission": tx_commission,
                "daily_settlement_pnl": total_settlement_pnl,
                "feedback_label": label,
            },
            "anti_overfit_boundary": {
                "same_scope_required_for_policy": True,
                "not_product_blacklist": True,
                "future_only": True,
                "hard_margin_cap_not_overridden": True,
            },
        }
        if opportunity_to_position.get("if_not_targeted_requires_accountability"):
            payload["missed_high_quality_opportunity"] = {
                "requires_counterfactual_followup": True,
                "likely_blocking_reasons": sorted(set(position_effect.get("control_reasons") or [])),
                "opportunity_state_summary": opportunity_to_position.get("opportunity_state_summary") or {},
                "mature_alpha_policy_count": opportunity_to_position.get("mature_alpha_policy_count"),
                "fast_candidate_alpha_count": opportunity_to_position.get("fast_candidate_alpha_count"),
                "next_step": "track same-scope counterfactual and relax only if future settled results are positive",
            }
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="research_position_feedback",
            scope_type="ticker_side_template_horizon_regime",
            scope_key=f"{ticker}:{side}:{template}:{horizon}:{regime}",
            evidence={
                "memory_ref_count": len(memory_refs),
                "policy_ref_count": len(policy_refs),
                "target_lots": target_lots,
                "executed_lots": executed_lots,
                "transaction_pnl": tx_pnl,
                "feedback_label": label,
                "opportunity_to_position": opportunity_to_position,
            },
            action={
                "next_step": "feed_back_into_same_scope_future_analysis_and_pm_review",
                "position_authority": "diagnostic_feedback_only_until_mature_policy",
            },
            status="observed",
        )
        feedback_id = str(uuid.uuid4())
        cursor.execute(
            '''
            INSERT INTO research_position_feedback (
                id, config_id, trading_date, ticker, side, setup_type, horizon_class,
                market_regime, recommendation_id, transaction_count, executed_lots,
                target_lots, current_lots, position_delta_lots, target_position_ratio,
                memory_refs_json, policy_refs_json, pm_effect_json, auditor_effect_json,
                trader_effect_json, outcome_json, feedback_label, created_at, valid_until,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_id, trading_date, ticker, recommendation_id)
            DO UPDATE SET
                side=excluded.side,
                setup_type=excluded.setup_type,
                horizon_class=excluded.horizon_class,
                market_regime=excluded.market_regime,
                transaction_count=excluded.transaction_count,
                executed_lots=excluded.executed_lots,
                target_lots=excluded.target_lots,
                current_lots=excluded.current_lots,
                position_delta_lots=excluded.position_delta_lots,
                target_position_ratio=excluded.target_position_ratio,
                memory_refs_json=excluded.memory_refs_json,
                policy_refs_json=excluded.policy_refs_json,
                pm_effect_json=excluded.pm_effect_json,
                auditor_effect_json=excluded.auditor_effect_json,
                trader_effect_json=excluded.trader_effect_json,
                outcome_json=excluded.outcome_json,
                feedback_label=excluded.feedback_label,
                created_at=excluded.created_at,
                valid_until=excluded.valid_until,
                payload_json=excluded.payload_json
            ''',
            (
                feedback_id,
                config_id,
                trading_date,
                ticker,
                side,
                template,
                horizon,
                regime,
                rec_id,
                len(txs),
                executed_lots,
                target_lots,
                current_lots,
                delta_lots,
                target_ratio,
                _json_dumps(memory_refs),
                _json_dumps(policy_refs),
                _json_dumps(payload["pm_effect"]),
                _json_dumps(payload["auditor_effect"]),
                _json_dumps(payload["trader_effect"]),
                _json_dumps(payload["outcome"]),
                label,
                now,
                valid_until,
                _json_dumps({**payload, "source_event_id": event_id}),
            ),
        )
        feedback_rows += 1
        if digest_rows < max_digest_rows:
            confidence = max(min_digest_confidence, min(0.90, 0.20 + 0.05 * len(memory_refs) + 0.08 * len(policy_refs)))
            digest = (
                f"{ticker} {horizon} {side}: learning-to-position feedback={label}; "
                f"target_lots={target_lots}, executed_lots={executed_lots}, pnl={tx_pnl:.0f}. "
                "Use this only as same-scope feedback; compare today's evidence before changing position."
            )
            digest_contract = build_next_round_memory_contract(
                memory_type="research_position_feedback",
                maturity_state="feedback_observation",
                scope={
                    "analyst": "portfolio_manager",
                    "ticker": ticker,
                    "sector": _sector_for_ticker(cfg, ticker),
                    "side": side,
                    "setup_type": template,
                    "horizon_class": horizon,
                    "market_regime": regime,
                },
                usable_memory=digest,
                analysis_strategy_updates=[
                    "Treat as feedback on how prior memory affected the position chain, not as a standalone signal.",
                    "Compare current data drivers, analyst conflict, and market state with this same-scope record.",
                ],
                trading_strategy_updates=[
                    "PM may use repeated same-scope feedback to refine open/add/reduce/exit reasoning.",
                    "This feedback alone cannot authorize sizing; mature adaptive policy and current evidence are required.",
                ],
                validation_plan=[
                    "Accumulate same-scope feedback rows and compare PnL, no-trade reason, and policy refs before promotion.",
                ],
                position_authority="analysis_or_watchlist_only",
                max_position_impact="no_direct_position_impact_until_promoted_policy",
                sample_count=1,
                confidence_score=confidence,
            )
            digest_event_id = _insert_learning_event(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                event_type="research_position_feedback_digest",
                scope_type="pm_feedback",
                scope_key=f"portfolio_manager:{ticker}:{horizon}:{side}",
                evidence=payload["outcome"],
                action={"digest": digest, CONTRACT_KEY: digest_contract},
                status="observed",
            )
            cursor.execute(
                '''
                INSERT INTO analyst_learning_digest (
                    id, config_id, analyst, ticker, sector, horizon_class, market_regime,
                    digest_text, confidence_score, sample_count, source_event_id,
                    created_at, valid_until, accepted, payload_json
                ) VALUES (?, ?, 'portfolio_manager', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 1, ?)
                ''',
                (
                    str(uuid.uuid4()),
                    config_id,
                    ticker,
                    _sector_for_ticker(cfg, ticker),
                    horizon,
                    regime,
                    digest,
                    confidence,
                    digest_event_id,
                    now,
                    valid_until,
                    _json_dumps({
                        "feedback_id": feedback_id,
                        "feedback_label": label,
                        "transaction_pnl": tx_pnl,
                        CONTRACT_KEY: digest_contract,
                    }),
                ),
            )
            digest_rows += 1
    _backfill_research_position_feedback_from_completed_episodes(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        completed_episode_payloads=completed_episode_payloads,
    )
    return {"feedback_rows": feedback_rows, "digest_rows": digest_rows}

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
    setattr(_write_loss_template_observation_research, "last_policy_rows", 0)
    learning_cfg = cfg.get("learning", {}) or {}
    research_cfg = learning_cfg.get("loss_template_observation", {}) or {}
    if not bool(research_cfg.get("enabled", True)):
        return 0
    _ensure_research_learning_schema(cursor)

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
    policy_cfg = research_cfg.get("policy_promotion", {}) or {}
    policy_enabled = bool(policy_cfg.get("enabled", True))
    policy_min_samples = int(policy_cfg.get("min_loss_samples", max(3, min_samples)) or max(3, min_samples))
    policy_min_loss_abs = abs(_safe_float(policy_cfg.get("min_cumulative_loss_abs"), max(min_loss_abs, 20000.0)))
    policy_multiplier = max(0.0, min(1.0, _safe_float(policy_cfg.get("cap_multiplier"), 0.35)))
    policy_min_confidence = max(0.0, min(1.0, _safe_float(policy_cfg.get("min_confidence"), 0.55)))
    policy_valid_days = int(policy_cfg.get("valid_days", min(valid_days, 10)) or min(valid_days, 10))
    family_cfg = research_cfg.get("failure_family_policy", {}) or {}
    include_failure_family = bool(family_cfg.get("enabled", True))
    include_data_combo = bool(family_cfg.get("include_data_combo", True))
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
        template = _setup_type(side, combo, snapshot)
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
    policy_inserted = 0
    for _, key, rows in candidates[:max_rows]:
        ticker, side, template, horizon, regime = key
        snapshot = representative_snapshot.get(key) or {}
        sector = _sector_for_ticker(cfg, ticker)
        data_usage = data_usage_from_snapshot(snapshot)
        data_notes = compact_data_usage_notes(data_usage)
        data_combo = _data_combo_key(data_usage) if include_data_combo else "data_combo_disabled"
        failure_family = (
            _loss_failure_family(template, horizon, regime, data_combo)
            if include_failure_family
            else "same_scope_loss_failure"
        )
        failure_actions = _failure_family_actions(failure_family)
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
            f"horizon={horizon}, regime={regime}, failure_family={failure_family}, samples={len(rows)}, "
            f"net_pnl={_safe_float(summary.get('total_pnl')):.0f}. "
            "Next comparable setups should test whether the data mix and market state still justify the trade."
        )
        suggested_use = (
            "observation-only structured research hypothesis; do not block, blacklist, size down, add, "
            "or hold a losing position from this memory alone; require current confirmation and invalidation"
        )
        contract = build_next_round_memory_contract(
            memory_type="loss_template_observation",
            maturity_state="candidate_observation",
            status="candidate",
            scope={
                "ticker": ticker,
                "sector": sector,
                "side": side,
                "setup_type": template,
                "horizon_class": horizon,
                "market_regime": regime,
                "failure_family": failure_family,
            },
            usable_memory=[
                hypothesis_text,
                f"loss_examples={len(rows)}; cumulative_pnl={_safe_float(summary.get('total_pnl')):.0f}",
                f"data_combo={_compact_text(data_combo, 180)}",
            ],
            data_focus=data_focus,
            analysis_strategy_updates=[
                *failure_actions["analysis"],
                "Before issuing the same direction/template, verify which data fields actually confirm it today.",
                "Treat analyst conflict, stale data, horizon mismatch, and missing invalidation as questions to resolve, not as automatic vetoes.",
                "Check whether the current market state differs enough to invalidate this loss observation.",
            ],
            trading_strategy_updates=[
                *failure_actions["trading"],
                "PM may use this only to demand clearer current evidence, trigger, and invalidation for comparable setups.",
                "This candidate memory cannot authorize position_match, add-on sizing, or continued losing exposure by itself.",
            ],
            pm_action_conditions=[
                *failure_actions["trading"][:1],
                "If today's same-scope setup repeats the weak data mix and lacks a valid trigger/invalidation, PM should prefer probe/observe/reduce logic.",
                "If today's evidence clearly contradicts the old loss pattern, PM should record the contradiction instead of mechanically suppressing the trade.",
            ],
            invalidates_when=[
                "Future same-scope samples show positive expectancy.",
                "Today's data mix, market regime, or horizon differs from the remembered loss template.",
                "A current trigger and explicit invalidation boundary are present and confirmed by market data.",
            ],
            validation_plan=[
                "Track future same-scope trades and no-trade counterfactuals before promoting, weakening, or discarding this observation.",
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
            "data_combo": data_combo,
            "failure_family": failure_family,
            "failure_family_actions": failure_actions,
            "failure_family_policy_config": {
                "enabled": include_failure_family,
                "include_data_combo": include_data_combo,
                "require_news_price_reaction_for_event_probe": bool(
                    family_cfg.get("require_news_price_reaction_for_event_probe", True)
                ),
                "require_choppy_trend_breakout_confirmation": bool(
                    family_cfg.get("require_choppy_trend_breakout_confirmation", True)
                ),
                "require_medium_anchor_short_timing": bool(
                    family_cfg.get("require_medium_anchor_short_timing", True)
                ),
            },
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
                "trigger_valid": False,
                "opportunity_state": "watch_for_trigger",
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
                    f"failure_family={failure_family}; data_focus={'; '.join(data_focus[:3])}"
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
        cumulative_loss_abs = abs(_safe_float(summary.get("total_pnl")))
        policy_confidence = min(0.85, 0.40 + 0.08 * len(rows) + min(0.25, cumulative_loss_abs / 80000.0))
        if (
            policy_enabled
            and len(rows) >= policy_min_samples
            and cumulative_loss_abs >= policy_min_loss_abs
            and policy_confidence >= policy_min_confidence
        ):
            policy_scope = {
                "ticker": ticker,
                "sector": sector,
                "side": side,
                "setup_type": template,
                "horizon_class": horizon,
                "market_regime": regime,
            }
            promotion_gate = _policy_promotion_gate(cfg=cfg, rows=rows, action="cap")
            reversal_stats = _counterfactual_reversal_stats(
                cursor,
                cfg=cfg,
                config_id=config_id,
                trading_date=trading_date,
                scope=policy_scope,
            )
            if not promotion_gate["allowed"] or reversal_stats.get("reversal"):
                if reversal_stats.get("reversal"):
                    _deactivate_adaptive_policy_state(
                        cursor,
                        config_id=config_id,
                        scope=policy_scope,
                        policy_type="loss_template_policy",
                        reason="loss template policy deactivated by positive same-scope counterfactual reversal",
                    )
                _insert_learning_event(
                    cursor,
                    config_id=config_id,
                    trading_date=trading_date,
                    event_type="loss_template_policy_guard",
                    scope_type="ticker_side_template",
                    scope_key=f"{ticker}:{side}:{template}:{horizon}:{regime}",
                    evidence={
                        "policy_source_hypothesis_id": hypothesis_id,
                        "summary": summary,
                        "promotion_gate": promotion_gate,
                        "counterfactual_reversal": reversal_stats,
                    },
                    action={
                        "policy_action": "keep_candidate_observation",
                        "reason": (
                            "loss template stayed candidate because promotion gate failed"
                            if not promotion_gate["allowed"]
                            else "loss template cap was reversed by positive same-scope counterfactual results"
                        ),
                    },
                    status="rejected",
                )
                continue
            policy_reason = (
                f"validated repeated loss template: {ticker} {side} {template} "
                f"horizon={horizon}, regime={regime}, samples={len(rows)}, "
                f"net_pnl={_safe_float(summary.get('total_pnl')):.0f}"
            )
            policy_evidence = {
                **evidence,
                "sample_count": len(rows),
                "confidence_score": policy_confidence,
                "total_trades": len(rows),
                "total_pnl": _safe_float(summary.get("total_pnl")),
                "policy_source_hypothesis_id": hypothesis_id,
                "policy_promotion_gate": promotion_gate,
                "counterfactual_reversal": reversal_stats,
                "policy_promotion_thresholds": {
                    "min_loss_samples": policy_min_samples,
                    "min_cumulative_loss_abs": policy_min_loss_abs,
                    "min_confidence": policy_min_confidence,
                    "cap_multiplier": policy_multiplier,
                },
                "failure_family": failure_family,
                "data_combo": data_combo,
                "failure_family_actions": failure_actions,
            }
            policy_payload = _loss_template_policy_payload(
                reason=policy_reason,
                scope=policy_scope,
                evidence=policy_evidence,
                multiplier=policy_multiplier,
            )
            policy_event_id = _insert_learning_event(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                event_type="loss_template_policy",
                scope_type="ticker_side_template",
                scope_key=f"{ticker}:{side}:{template}:{horizon}:{regime}",
                evidence=policy_evidence,
                action={
                    "policy_action": "cap",
                    "multiplier": policy_multiplier,
                    "valid_until": _valid_until(trading_date, policy_valid_days),
                    CONTRACT_KEY: policy_payload[CONTRACT_KEY],
                },
                status="applied",
            )
            cursor.execute(
                """
                INSERT INTO adaptive_policy_state (
                    id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                    policy_type, policy_action, multiplier, confidence_score, sample_count,
                    reason, source_event_id, created_at, valid_until, payload_json, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'loss_template_policy', 'cap', ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, market_regime, policy_type)
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
                """,
                (
                    str(uuid.uuid4()),
                    config_id,
                    ticker,
                    side,
                    template,
                    horizon,
                    regime,
                    policy_multiplier,
                    policy_confidence,
                    len(rows),
                    policy_reason,
                    policy_event_id,
                    now,
                    _valid_until(trading_date, policy_valid_days),
                    _json_dumps(policy_payload),
                ),
            )
            policy_inserted += 1
    setattr(_write_loss_template_observation_research, "last_policy_rows", policy_inserted)
    return inserted

def _write_fast_loss_sentinel_state(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    cfg: Dict[str, Any],
) -> int:
    learning_cfg = cfg.get("learning", {}) or {}
    control = learning_cfg.get("fast_loss_sentinel", {}) or {}
    if not bool(control.get("enabled", True)):
        return 0
    _ensure_research_learning_schema(cursor)
    lookback_days = int(control.get("lookback_days", 5) or 5)
    min_loss_samples = int(control.get("min_loss_samples", 1) or 1)
    min_net_loss = -abs(_safe_float(control.get("min_net_loss_abs"), 3000.0))
    cap_multiplier = max(0.0, min(1.0, _safe_float(control.get("cap_multiplier"), 0.50)))
    valid_days = int(control.get("valid_days", 5) or 5)
    max_rows = int(control.get("max_rows_per_day", 6) or 6)
    lookback_start = (datetime.strptime(trading_date[:10], "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    pairs = [
        pair
        for pair in _completed_pairs_up_to(cursor, config_id=config_id, trading_date=trading_date)
        if str(pair.get("close_date") or "") >= lookback_start and _safe_float(pair.get("net_pnl")) < 0
    ]
    if not pairs:
        return 0
    recommendation_lookup = _recommendations_by_id(
        cursor,
        [pair.get("open_recommendation_id") for pair in pairs if pair.get("open_recommendation_id")],
    )
    groups: Dict[Tuple[str, str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    snapshots: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    for pair in pairs:
        ticker = str(pair.get("ticker") or "").upper()
        side = str(pair.get("side") or "").lower()
        if not ticker or side not in {"long", "short"}:
            continue
        snapshot = _recommendation_snapshot(recommendation_lookup.get(str(pair.get("open_recommendation_id") or "")) or {})
        combo = _signal_combo_from_snapshot(snapshot)
        horizon = _horizon_class(_expected_horizon_days(snapshot, side), snapshot)
        regime = _market_regime(snapshot)
        template = _setup_type(side, combo, snapshot)
        key = (ticker, side, template, horizon, regime)
        groups[key].append(pair)
        snapshots.setdefault(key, snapshot)

    candidates: List[Tuple[float, Tuple[str, str, str, str, str], List[Dict[str, Any]]]] = []
    for key, rows in groups.items():
        net_pnl = sum(_safe_float(row.get("net_pnl")) for row in rows)
        if len(rows) >= min_loss_samples and net_pnl <= min_net_loss:
            candidates.append((abs(net_pnl), key, rows))
    candidates.sort(reverse=True, key=lambda item: (item[0], len(item[2])))
    now = _utc_now()
    valid_until = _valid_until(trading_date, valid_days)
    inserted = 0
    for loss_abs, key, rows in candidates[:max_rows]:
        ticker, side, template, horizon, regime = key
        snapshot = snapshots.get(key) or {}
        data_usage = data_usage_from_snapshot(snapshot)
        data_combo = _data_combo_key(data_usage)
        failure_family = _loss_failure_family(template, horizon, regime, data_combo)
        summary = summarize_trade_pairs(rows)
        confidence = min(0.75, 0.35 + 0.10 * len(rows) + min(0.20, loss_abs / 30000.0))
        evidence = {
            "source": "fast_loss_sentinel",
            "summary": summary,
            "sample_count": len(rows),
            "net_pnl": _safe_float(summary.get("total_pnl")),
            "failure_family": failure_family,
            "data_combo": data_combo,
            "lookback_days": lookback_days,
            "fast_protection_only": True,
        }
        contract = build_next_round_memory_contract(
            memory_type="fast_loss_sentinel",
            maturity_state="fast_loss_protection",
            status="candidate",
            scope={
                "ticker": ticker,
                "sector": _sector_for_ticker(cfg, ticker),
                "side": side,
                "setup_type": template,
                "horizon_class": horizon,
                "market_regime": regime,
                "failure_family": failure_family,
            },
            usable_memory=[
                f"Recent same-scope loss appeared quickly: samples={len(rows)}, net_pnl={_safe_float(summary.get('total_pnl')):.0f}.",
                "Use as fast protection: demand current trigger/invalidation; do not blacklist the ticker.",
            ],
            analysis_strategy_updates=[
                "Analysts should verify whether today's data mix still repeats this fast-loss setup.",
                "If current evidence contradicts the loss pattern, explicitly record the contradiction.",
            ],
            trading_strategy_updates=[
                "PM may cap to probe/reduce-only for same-scope repeated setup until future evidence refutes it.",
                "Fast protection expires quickly and cannot become permanent without mature loss-template validation.",
            ],
            pm_action_conditions=[
                "Cap only if same side/template/regime repeats and current trigger or invalidation remains weak.",
            ],
            invalidates_when=[
                "Future same-scope counterfactual or executed trades show positive expectancy.",
                "Current signal has strong trigger, data confirmation, and explicit invalidation.",
            ],
            validation_plan=["Track future same-scope outcomes before promotion or removal."],
            position_authority="same_scope_probe_cap_or_reduce_only",
            max_position_impact="short_lived_same_scope_cap",
            sample_count=len(rows),
            confidence_score=confidence,
        )
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="fast_loss_sentinel",
            scope_type="ticker_side_template",
            scope_key=f"{ticker}:{side}:{template}:{horizon}:{regime}",
            evidence=evidence,
            action={"policy_action": "cap", "multiplier": cap_multiplier, CONTRACT_KEY: contract},
            status="applied",
        )
        cursor.execute(
            """
            INSERT INTO adaptive_policy_state (
                id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                policy_type, policy_action, multiplier, confidence_score, sample_count,
                reason, source_event_id, created_at, valid_until, payload_json, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'fast_loss_sentinel', 'cap', ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, market_regime, policy_type)
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
                cap_multiplier,
                confidence,
                len(rows),
                "fast same-scope loss protection; expires quickly and is not a product blacklist",
                event_id,
                now,
                valid_until,
                _json_dumps({
                    "policy_type": "fast_loss_sentinel",
                    "scope": {
                        "ticker": ticker,
                        "side": side,
                        "setup_type": template,
                        "horizon_class": horizon,
                        "market_regime": regime,
                    },
                    "evidence": evidence,
                    CONTRACT_KEY: contract,
                    "boundary": {
                        "short_lived": True,
                        "not_product_blacklist": True,
                        "trigger_valid": False,
                        "opportunity_state": "watch_for_trigger",
                    },
                }),
            ),
        )
        inserted += 1
    return inserted

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
        final_contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        auditor = snapshot.get("auditor") if isinstance(snapshot.get("auditor"), dict) else {}
        auditor_reasons = [
            str(item)
            for item in (auditor.get("audit_reason_codes") or [])
            if item
        ]
        auditor_reasons = sorted(set(auditor_reasons))
        execution_result = _execution_result_from_snapshot(snapshot)
        execution_no_trade_reason = normalize_no_trade_reason(execution_result.get("no_trade_reason"))
        limit_locked_execution = execution_no_trade_reason == "limit_locked_no_fill"
        inferred_no_trade_reason = infer_no_trade_reason(snapshot, recommendation.get("warning_message"))
        reason = str(
            execution_no_trade_reason
            or inferred_no_trade_reason
            or ((final_contract.get("reason_codes") or [None])[-1] if isinstance(final_contract.get("reason_codes"), list) else None)
            or recommendation.get("warning_message")
            or ""
        )
        normalized_reason = normalize_no_trade_reason(reason) or "unknown"
        no_trade_category = categorize_no_trade_reason(normalized_reason)
        if lots > 0 and action not in {"hold", "none"} and not limit_locked_execution and not execution_no_trade_reason:
            continue
        fac_identity = _fac_no_trade_learning_identity(snapshot)
        if not fac_identity.get("complete"):
            logger.warning(
                "Skip no-trade opportunity memory with incomplete FAC identity: "
                f"ticker={ticker}, recommendation_id={recommendation.get('id')}, "
                f"missing_fields={fac_identity.get('missing_fields') or []}"
            )
            continue
        side = str(fac_identity["side"])
        neutral_observations = _neutral_opportunity_observations(snapshot)
        combo = _signal_combo_from_snapshot(snapshot)
        template = str(fac_identity["setup_type"])
        entry_trigger = str(fac_identity["entry_trigger"])
        horizon = str(fac_identity["horizon_class"])
        regime = str(fac_identity["market_regime"])
        sector = _sector_for_ticker(cfg, ticker)
        counterfactual_entry_price = _safe_float(
            recommendation.get("base_price")
            or recommendation.get("execution_price")
            or recommendation.get("open_price")
            or recommendation.get("prev_close_price"),
            0.0,
        )
        if counterfactual_entry_price <= 0:
            continue
        candidate_lots = max(1, abs(lots))
        counterfactual_lots = 1
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
                "Only promote a timing adjustment after forward counterfactual results show same-scope missed alpha or avoided loss.",
            ]
            validation_updates = [
                "Backfill no-trade counterfactual windows to test whether the limit-locked skipped trade was a real missed alpha or a correctly avoided unfilled order.",
            ]
        opportunity_ranking_trace = _opportunity_ranking_trace(snapshot, side)
        if opportunity_ranking_trace.get("opportunity_score") is not None:
            validation_updates.append(
                "Compare skipped candidate forward outcome by opportunity_score/opportunity_rank before changing PM allocation priority."
            )
        safe_snapshot = _learning_safe_snapshot(snapshot)
        payload = {
            "recommendation_id": recommendation.get("id"),
            "signal_snapshot": safe_snapshot,
            "trade_research_contract_summary": _opportunity_contract_summary(snapshot),
            "opportunity_ranking_trace": opportunity_ranking_trace,
            "data_usage_summary": data_usage,
            "data_usage_notes": data_usage_notes,
            "final_action_contract": final_contract,
            "learning_source": "final_action_contract",
            "learning_to_position_trace": (
                final_contract.get("learning_used")
                if isinstance(final_contract.get("learning_used"), dict)
                else {}
            ),
            "position_lifecycle_trace": (
                (final_contract.get("learning_used") or {}).get("pm_lifecycle_learning_trace") or {}
                if isinstance(final_contract.get("learning_used"), dict)
                else {}
            ),
            "loss_template_research_trace": (
                {"reason_codes": final_contract.get("reason_codes") or []}
                if final_contract
                else {}
            ),
            "action": recommendation.get("action"),
            "lots": lots,
            "candidate_side": side,
            "entry_trigger": entry_trigger,
            "neutral_opportunity_observations": neutral_observations,
            "counterfactual_entry_price": counterfactual_entry_price,
            "no_trade_reason": normalized_reason,
            "no_trade_reason_category": no_trade_category,
            "execution_no_trade_reason": execution_no_trade_reason,
            "execution_learning_trace": (
                execution_result.get("execution_learning_trace")
                if isinstance(execution_result.get("execution_learning_trace"), dict)
                else build_execution_learning_trace(
                    snapshot,
                    outcome="skipped",
                    status=str(recommendation.get("status") or "skipped"),
                    no_trade_reason=normalized_reason or execution_no_trade_reason or "no_trade_opportunity",
                    no_trade_reason_category=no_trade_category,
                    transaction_count=0,
                    execution_learning_type="phase4_no_trade_opportunity_memory",
                    turn_into_memory=True,
                )
            ),
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
            maturity_state="counterfactual_tracking",
            scope={
                "ticker": ticker,
                "sector": sector,
                "side": side,
                "setup_type": template,
                "entry_trigger": entry_trigger,
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
                (
                    f"opportunity_score={opportunity_ranking_trace.get('opportunity_score')}; "
                    f"rank={opportunity_ranking_trace.get('opportunity_rank')}; "
                    f"allocation_reason={opportunity_ranking_trace.get('capital_allocation_reason')}"
                ),
                *data_usage_notes[:3],
            ],
            analysis_strategy_updates=[
                _no_trade_category_strategy_note(no_trade_category["category"]),
                *execution_timing_updates,
                "Treat skipped opportunities as watchlist questions: what evidence would have made them tradable?",
                "If forward counterfactual confirms missed alpha, convert this record into conditional setup requirements, not a blanket signal boost.",
                "Use forward counterfactual results to distinguish reasonable avoidance from missed opportunity only after settlement.",
            ],
            trading_strategy_updates=[
                *execution_strategy_updates,
                "Do not convert a skipped or Neutral opportunity into a trade unless the current trigger, market confirmation, and invalidation are explicit.",
                "A validated missed-alpha pattern may allow only same-scope probe/open under current confirmation and PM/Auditor approval.",
                "If future counterfactual results validate repeated missed opportunities, promote only through same-scope validation.",
            ],
            validation_plan=[
                *validation_updates,
                "Backfill configured forward counterfactual windows and compare same-scope outcomes before promotion.",
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
                id, config_id, trading_date, ticker, side, sector, setup_type,
                signal_combo, horizon_class, market_regime, opportunity_type,
                opportunity_state, candidate_lots, counterfactual_lots, counterfactual_entry_price,
                pm_reason, auditor_reason, execution_reason, evidence_summary,
                status, classification, counterfactual_results_json, payload_json,
                payload_artifact_path, payload_sha256, payload_size,
                payload_summary_json, created_at, last_reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_id, trading_date, ticker, side, setup_type)
            DO UPDATE SET
                sector=excluded.sector,
                signal_combo=excluded.signal_combo,
                horizon_class=excluded.horizon_class,
                market_regime=excluded.market_regime,
                opportunity_type=excluded.opportunity_type,
                opportunity_state=excluded.opportunity_state,
                counterfactual_entry_price=excluded.counterfactual_entry_price,
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
                _primary_opportunity_type(snapshot, side),
                _primary_opportunity_state(snapshot, side),
                candidate_lots,
                counterfactual_lots,
                counterfactual_entry_price,
                reason,
                "; ".join(auditor_reasons),
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

def _backfill_no_trade_opportunity_counterfactual_results(
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
    horizons = sorted({int(item) for item in (no_trade_cfg.get("counterfactual_forward_days") or [3, 5, 10]) if int(item) > 0})
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
        existing_results = _json_loads(row.get("counterfactual_results_json")) or []
        existing_by_horizon = {
            int(item.get("horizon_days") or 0): item
            for item in existing_results
            if isinstance(item, dict)
        }
        entry_price = _safe_float(row.get("counterfactual_entry_price"), 0.0)
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
            counterfactual_pnl = (exit_price - entry_price) * direction * multiplier * max(1, _safe_int(row.get("counterfactual_lots"), 1))
            new_results.append(
                {
                    "horizon_days": horizon,
                    "entry_date": memory_date,
                    "exit_date": exit_date,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "counterfactual_pnl": counterfactual_pnl,
                    "counterfactual_return": ((exit_price - entry_price) * direction / entry_price) if entry_price else 0.0,
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
            pnl = _safe_float((latest or {}).get("counterfactual_pnl"))
            classification = "missed_opportunity" if pnl > 0 else "correct_avoidance" if pnl < 0 else "unresolved"
        status = "closed" if all(horizon in completed_horizons for horizon in horizons) else "open"
        cursor.execute(
            '''
            UPDATE no_trade_opportunity_memory
            SET counterfactual_results_json = ?,
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
            event_type="no_trade_counterfactual_backfill",
            scope_type="daily",
            scope_key=trading_date,
            evidence={"candidate_rows": len(rows), "horizons": horizons},
            action={"updated_rows": updated},
            status="applied",
        )
    return {"updated_rows": updated, "status": "applied" if updated else "no_ready_rows", "horizons": horizons}

def _write_missed_alpha_accountability_state(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    """Promote repeated positive missed-opportunity counterfactuals into fast candidates.

    This is intentionally a future-only, same-scope policy. It never rewrites the
    original no-trade day and never grants mature alpha authority by itself.
    """
    learning_cfg = cfg.get("learning", {}) or {}
    control = learning_cfg.get("missed_alpha_accountability", {}) or {}
    if not bool(control.get("enabled", True)):
        return {"rows": 0, "status": "disabled"}
    _ensure_research_learning_schema(cursor)

    fixed_horizon_days = int(control.get("fixed_horizon_days", 5) or 5)
    min_samples = int(control.get("min_counterfactual_samples", 2) or 2)
    min_counterfactual_pnl = _safe_float(control.get("min_net_counterfactual_pnl"), 1500.0)
    min_positive_rate = _safe_float(control.get("min_positive_rate"), 0.55)
    max_rows = int(control.get("max_rows_per_day", 6) or 6)
    valid_days = int(control.get("valid_days", 8) or 8)
    probe_multiplier = max(0.0, min(1.0, _safe_float(control.get("probe_multiplier"), 0.75)))
    allowed_states = {
        str(item)
        for item in (control.get("eligible_opportunity_states") or ["probe_candidate", "tradeable_candidate"])
        if str(item or "").strip()
    }
    cursor.execute(
        '''
        SELECT *
        FROM no_trade_opportunity_memory
        WHERE config_id = ?
          AND trading_date < ?
          AND counterfactual_results_json IS NOT NULL
        ORDER BY trading_date DESC
        LIMIT 500
        ''',
        (config_id, trading_date),
    )
    groups: Dict[Tuple[str, str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    excluded_invalidated = 0
    excluded_incomplete_execution_basis = 0
    for row in cursor.fetchall():
        item = dict(row)
        state = str(item.get("opportunity_state") or "watch_for_trigger")
        if allowed_states and state not in allowed_states:
            continue
        results = _json_loads(item.get("counterfactual_results_json")) or []
        fixed_result = next(
            (
                result
                for result in results
                if isinstance(result, dict)
                and _safe_int(result.get("horizon_days"), 0) == fixed_horizon_days
            ),
            None,
        )
        if not isinstance(fixed_result, dict):
            continue
        execution_reason = normalize_no_trade_reason(item.get("execution_reason")) or ""
        if execution_reason in {"fac_invalidated_before_entry", "fac_expired_before_entry"}:
            excluded_invalidated += 1
            continue
        payload = load_externalized_json(
            item.get("payload_json"),
            item.get("payload_artifact_path"),
            item.get("payload_sha256"),
        ) or {}
        final_contract = (
            payload.get("final_action_contract")
            if isinstance(payload, dict) and isinstance(payload.get("final_action_contract"), dict)
            else {}
        )
        fac_identity = _fac_no_trade_learning_identity({"final_action_contract": final_contract})
        execution_profile = str(final_contract.get("execution_profile") or "").strip()
        trigger_source = str(final_contract.get("trigger_source") or "").strip().lower()
        execution_basis_complete = all(
            final_contract.get(field_name) not in (None, "", "unknown")
            for field_name in (
                "setup_type",
                "horizon_class",
                "market_regime",
                "entry_trigger",
                "execution_profile",
                "trigger_source",
                "invalidation",
                "invalidation_level",
            )
        ) and execution_profile in {"breakout", "pullback", "vwap_confirmed", "event_immediate"}
        execution_basis_complete = execution_basis_complete and trigger_source not in {"", "none", "unknown"}
        execution_basis_complete = execution_basis_complete and _safe_float(
            final_contract.get("invalidation_level"), 0.0
        ) > 0.0
        execution_basis_complete = execution_basis_complete and bool(fac_identity.get("complete"))
        execution_basis_complete = execution_basis_complete and all(
            str(fac_identity.get(field_name) or "") == str(item.get(field_name) or "")
            for field_name in ("side", "setup_type", "horizon_class", "market_regime")
        )
        if not execution_basis_complete:
            excluded_incomplete_execution_basis += 1
            continue
        item["fixed_horizon_counterfactual_pnl"] = _safe_float(fixed_result.get("counterfactual_pnl"))
        key = (
            str(item.get("ticker") or "*"),
            str(item.get("side") or "*"),
            str(item.get("setup_type") or "*"),
            str(item.get("horizon_class") or "*"),
            str(item.get("market_regime") or "*"),
        )
        groups[key].append(item)

    now = _utc_now()
    valid_until = _valid_until(trading_date, valid_days)
    inserted = 0
    guarded = 0
    candidates: List[Tuple[float, Tuple[str, str, str, str, str], List[Dict[str, Any]]]] = []
    for key, items in groups.items():
        net_counterfactual = sum(_safe_float(item.get("fixed_horizon_counterfactual_pnl")) for item in items)
        positive_rate = sum(
            1
            for item in items
            if _safe_float(item.get("fixed_horizon_counterfactual_pnl")) > 0
        ) / max(1, len(items))
        if len(items) >= min_samples and net_counterfactual >= min_counterfactual_pnl and positive_rate >= min_positive_rate:
            candidates.append((net_counterfactual, key, items))
    candidates.sort(reverse=True, key=lambda item: (item[0], len(item[2])))

    qualified_scope_keys = {key for _, key, _ in candidates}
    cursor.execute(
        """
        SELECT id, ticker, side, setup_type, horizon_class, market_regime
        FROM adaptive_policy_state
        WHERE config_id = ?
          AND policy_type = 'fast_candidate_alpha'
          AND active = 1
        """,
        (config_id,),
    )
    deactivated_rows = 0
    for policy_row in cursor.fetchall():
        policy = dict(policy_row)
        policy_scope_key = (
            str(policy.get("ticker") or "*"),
            str(policy.get("side") or "*"),
            str(policy.get("setup_type") or "*"),
            str(policy.get("horizon_class") or "*"),
            str(policy.get("market_regime") or "*"),
        )
        if policy_scope_key in qualified_scope_keys:
            continue
        deactivated_rows += _deactivate_adaptive_policy_state(
            cursor,
            config_id=config_id,
            scope={
                "ticker": policy_scope_key[0],
                "side": policy_scope_key[1],
                "setup_type": policy_scope_key[2],
                "horizon_class": policy_scope_key[3],
                "market_regime": policy_scope_key[4],
            },
            policy_type="fast_candidate_alpha",
            reason="fixed-horizon signed evidence no longer qualifies this fast candidate",
        )

    for net_counterfactual, key, items in candidates[:max_rows]:
        ticker, side, template, horizon, regime = key
        scope = {
            "ticker": ticker,
            "side": side,
            "setup_type": template,
            "horizon_class": horizon,
            "market_regime": regime,
        }
        confidence = min(0.85, 0.35 + 0.08 * len(items) + min(0.25, net_counterfactual / 50000.0))
        evidence = {
            "source": "missed_opportunity_counterfactual",
            "fixed_horizon_days": fixed_horizon_days,
            "sample_count": len(items),
            "net_counterfactual_pnl": net_counterfactual,
            "positive_rate": sum(
                1
                for item in items
                if _safe_float(item.get("fixed_horizon_counterfactual_pnl")) > 0
            ) / max(1, len(items)),
            "memory_ids": [item.get("id") for item in items[:20]],
            "opportunity_states": sorted({str(item.get("opportunity_state") or "unknown") for item in items}),
            "counterfactual_results_are_future_settled": True,
        }
        contract = build_next_round_memory_contract(
            memory_type="missed_alpha_accountability",
            maturity_state="fast_candidate_alpha",
            status="candidate",
            scope={**scope, "sector": _sector_for_ticker(cfg, ticker)},
            usable_memory=[
                f"Repeated same-scope missed opportunities showed positive counterfactual PnL={net_counterfactual:.0f}.",
                "Treat as a fast alpha candidate: look for current trigger, invalidation, and execution feasibility.",
            ],
            analysis_strategy_updates=[
                "Analysts should explain whether today's same-scope setup has become a real trade setup or remains only a direction view.",
                "Technical/news timing must confirm; do not promote solely from counterfactual history.",
            ],
            trading_strategy_updates=[
                "PM may reduce same-scope soft blocking and allow probe/small setup only when today's evidence confirms.",
                "This candidate cannot authorize normal sizing or add-on exposure without mature alpha promotion.",
            ],
            pm_action_conditions=[
                "If current opportunity_state is probe_candidate/tradeable_candidate with invalidation and market confirmation, allow probe or small trade.",
                "If current setup is watch_for_trigger/no_opportunity or data quality is weak, keep watchlist/counterfactual.",
            ],
            invalidates_when=[
                "Future same-scope executed or counterfactual samples turn negative.",
                "Current setup lacks trigger, invalidation, or execution basis.",
            ],
            validation_plan=[
                "Track whether future PM/Auditor/Trader actions convert this candidate into profitable executed trades.",
            ],
            position_authority="probe_or_small_setup_only_after_current_confirmation",
            max_position_impact="same_scope_probe_or_small_trade_only",
            sample_count=len(items),
            confidence_score=confidence,
        )
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="missed_alpha_accountability",
            scope_type="ticker_side_template",
            scope_key=f"{ticker}:{side}:{template}:{horizon}:{regime}",
            evidence=evidence,
            action={
                "policy_action": "fast_candidate_alpha",
                "multiplier": probe_multiplier,
                CONTRACT_KEY: contract,
            },
            status="applied",
        )
        cursor.execute(
            """
            INSERT INTO adaptive_policy_state (
                id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                policy_type, policy_action, multiplier, confidence_score, sample_count,
                reason, source_event_id, created_at, valid_until, payload_json, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'fast_candidate_alpha', 'probe', ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, market_regime, policy_type)
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
                probe_multiplier,
                confidence,
                len(items),
                "positive same-scope missed-opportunity counterfactual created fast alpha candidate",
                event_id,
                now,
                valid_until,
                _json_dumps({
                    "policy_type": "fast_candidate_alpha",
                    "scope": scope,
                    "evidence": evidence,
                    CONTRACT_KEY: contract,
                    "boundary": {
                        "not_mature_alpha": True,
                        "trigger_valid": False,
                        "opportunity_state": "watch_for_trigger",
                        "no_future_pollution": True,
                        "not_product_whitelist": True,
                    },
                }),
            ),
        )
        inserted += 1

    if not inserted and candidates:
        guarded = len(candidates)
    return {
        "rows": inserted,
        "guarded": guarded,
        "deactivated_rows": deactivated_rows,
        "excluded_invalidated": excluded_invalidated,
        "excluded_incomplete_execution_basis": excluded_incomplete_execution_basis,
        "fixed_horizon_days": fixed_horizon_days,
        "status": "applied" if inserted else "no_ready_candidates",
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
    review_cfg = learning_cfg.get("researcher_causal_review") or {}
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
                    "setup_type": template,
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
                    "setup_type": template,
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
                    id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                    policy_type, policy_action, multiplier, confidence_score, sample_count,
                    reason, source_event_id, created_at, valid_until, payload_json, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, market_regime, policy_type)
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
                    "setup_type": template,
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

def _learning_mechanism_policy_groups(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    min_samples: int,
    min_positive_net_pnl: float,
    min_positive_win_rate: float,
    max_negative_net_pnl: float,
    max_negative_win_rate: float,
    infer_from_full_trace: bool = True,
) -> List[Dict[str, Any]]:
    try:
        pairs = _completed_pairs_up_to(cursor, config_id=config_id, trading_date=trading_date)
    except sqlite3.Error:
        return []
    recommendation_lookup = _recommendations_by_id(
        cursor,
        [pair.get("open_recommendation_id") for pair in pairs if pair.get("open_recommendation_id")],
    )
    groups: Dict[Tuple[str, str, str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        recommendation = recommendation_lookup.get(str(pair.get("open_recommendation_id") or ""))
        if not recommendation:
            continue
        snapshot = _recommendation_snapshot(recommendation or {})
        mechanisms = _learning_mechanisms_from_recommendation(
            recommendation,
            infer_from_full_trace=infer_from_full_trace,
        )
        if not mechanisms:
            continue
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
        item["learning_mechanisms"] = mechanisms
        for mechanism in mechanisms:
            groups[(ticker, side, template, horizon, regime, str(mechanism))].append(item)

    rows: List[Dict[str, Any]] = []
    for (ticker, side, template, horizon, regime, mechanism), pairs_for_mechanism in groups.items():
        summary = _trade_pair_performance_summary(pairs_for_mechanism)
        total = _safe_int(summary.get("total_trades"))
        if total < min_samples:
            continue
        net_pnl = _safe_float(summary.get("net_pnl"))
        win_rate = _safe_float(summary.get("win_rate"))
        if net_pnl >= min_positive_net_pnl and win_rate >= min_positive_win_rate:
            action = "protect"
            multiplier = 1.0
            reason = f"{mechanism} same-scope performance positive"
            maturity = "mechanism_performance_promoted"
        elif net_pnl <= max_negative_net_pnl or win_rate <= max_negative_win_rate:
            action = "cap"
            multiplier = 0.50
            reason = f"{mechanism} same-scope performance weak"
            maturity = "mechanism_performance_demoted"
        else:
            continue
        scope = {
            "ticker": ticker,
            "side": side,
            "setup_type": template,
            "horizon_class": horizon,
            "market_regime": regime,
        }
        promotion_gate = _policy_promotion_gate(cfg=cfg, rows=pairs_for_mechanism, action=action)
        counterfactual_reversal = (
            _counterfactual_reversal_stats(
                cursor,
                cfg=cfg,
                config_id=config_id,
                trading_date=trading_date,
                scope=scope,
            )
            if action == "cap"
            else {"reversal": False, "enabled": bool(_policy_guard_config(cfg))}
        )
        if not promotion_gate["allowed"] or counterfactual_reversal.get("reversal"):
            if counterfactual_reversal.get("reversal"):
                _deactivate_adaptive_policy_state_from_counterfactual_reversal(
                    cursor,
                    config_id=config_id,
                    scope=scope,
                    policy_type=f"learning_mechanism:{mechanism}",
                    counterfactual_reversal=counterfactual_reversal,
                    reason="learning mechanism cap deactivated by positive same-scope counterfactual reversal",
                )
            rows.append(
                {
                    **scope,
                    "learning_mechanism": mechanism,
                    "policy_action": "watchlist",
                    "multiplier": 1.0,
                    "reason": (
                        f"{mechanism} stayed watchlist: promotion gate failed"
                        if not promotion_gate["allowed"]
                        else f"{mechanism} cap reversed by same-scope counterfactual results"
                    ),
                    "maturity_state": "mechanism_performance_watchlist",
                    "summary": summary,
                    "promotion_gate": promotion_gate,
                    "counterfactual_reversal": counterfactual_reversal,
                    "guarded": True,
                }
            )
            continue
        rows.append(
            {
                **scope,
                "learning_mechanism": mechanism,
                "policy_action": action,
                "multiplier": multiplier,
                "reason": reason,
                "maturity_state": maturity,
                "summary": summary,
                "promotion_gate": promotion_gate,
                "counterfactual_reversal": counterfactual_reversal,
                "guarded": False,
            }
        )
    rows.sort(
        key=lambda item: (
            -abs(_safe_float((item.get("summary") or {}).get("net_pnl"))),
            -_safe_int((item.get("summary") or {}).get("total_trades")),
            str(item.get("learning_mechanism") or ""),
        )
    )
    return rows


def _write_learning_mechanism_policy_state(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    learning_cfg = cfg.get("learning", {}) or {}
    control = learning_cfg.get("learning_mechanism_policy", {}) or {}
    if not bool(control.get("enabled", True)):
        return {"rows": 0, "status": "disabled"}
    _ensure_research_learning_schema(cursor)

    min_samples = _safe_int(
        control.get("min_samples"),
        _safe_int((learning_cfg.get("anti_overfit") or {}).get("min_samples_for_policy"), 4),
    )
    policy_rows = _learning_mechanism_policy_groups(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        min_samples=min_samples,
        min_positive_net_pnl=_safe_float(control.get("min_positive_net_pnl"), 1000.0),
        min_positive_win_rate=_safe_float(control.get("min_positive_win_rate"), 0.58),
        max_negative_net_pnl=_safe_float(control.get("max_negative_net_pnl"), -1000.0),
        max_negative_win_rate=_safe_float(control.get("max_negative_win_rate"), 0.42),
        infer_from_full_trace=bool(control.get("infer_from_full_trace", True)),
    )
    if not policy_rows:
        return {"rows": 0, "status": "no_scoped_mechanism_policy", "min_samples": min_samples}

    valid_until = _valid_until(
        trading_date,
        int(control.get("valid_days") or learning_cfg.get("overlay_expires_after_days") or 10),
    )
    now = _utc_now()
    inserted = 0
    guarded = 0
    max_rows = max(1, _safe_int(control.get("max_rows_per_day"), 12))
    cap_multiplier = max(0.0, min(1.0, _safe_float(control.get("cap_multiplier"), 0.50)))
    for row in policy_rows[:max_rows]:
        summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
        action = str(row.get("policy_action") or "cap")
        if action == "watchlist" or row.get("guarded"):
            _insert_learning_event(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                event_type="learning_mechanism_policy_guard",
                scope_type="template",
                scope_key=f"{row.get('ticker')}:{row.get('side')}:{row.get('setup_type')}:{row.get('learning_mechanism')}",
                evidence={
                    "learning_mechanism": row.get("learning_mechanism"),
                    "summary": summary,
                    "promotion_gate": row.get("promotion_gate"),
                    "counterfactual_reversal": row.get("counterfactual_reversal"),
                },
                action={"policy_action": "watchlist_only", "reason": row.get("reason")},
                status="guarded",
            )
            guarded += 1
            continue
        multiplier = 1.0 if action == "protect" else cap_multiplier
        confidence = _confidence_from_summary(
            {
                "total_trades": summary.get("total_trades"),
                "win_rate": summary.get("win_rate"),
                "total_pnl": summary.get("net_pnl"),
            }
        )
        evidence = _with_policy_performance_columns(
            {
                "learning_mechanism": row.get("learning_mechanism"),
                "summary": summary,
                "sample_count": summary.get("total_trades"),
                "confidence_score": confidence,
                "policy_promotion_gate": row.get("promotion_gate") or {},
                "counterfactual_reversal": row.get("counterfactual_reversal") or {},
            },
            summary,
        )
        reason = str(row.get("reason") or "learning mechanism same-scope performance")
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="learning_mechanism_policy",
            scope_type="template",
            scope_key=f"{row.get('ticker')}:{row.get('side')}:{row.get('setup_type')}:{row.get('learning_mechanism')}",
            evidence=evidence,
            action={
                "policy_action": action,
                "multiplier": multiplier,
                "reason": reason,
                "learning_mechanism": row.get("learning_mechanism"),
                "confidence_score": confidence,
            },
            status="applied",
        )
        policy_payload = _policy_contract_payload(
            policy_type=f"learning_mechanism:{row.get('learning_mechanism')}",
            policy_action=action,
            reason=reason,
            multiplier=multiplier,
            maturity_state=str(row.get("maturity_state") or "mechanism_performance_policy"),
            scope={
                "ticker": row.get("ticker"),
                "side": row.get("side"),
                "setup_type": row.get("setup_type"),
                "horizon_class": row.get("horizon_class"),
                "market_regime": row.get("market_regime"),
            },
            evidence=evidence,
        )
        cursor.execute(
            """
            INSERT INTO adaptive_policy_state (
                id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                policy_type, policy_action, multiplier, confidence_score, sample_count,
                reason, source_event_id, created_at, valid_until, payload_json, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, market_regime, policy_type)
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
                row.get("ticker") or "*",
                row.get("side") or "*",
                row.get("setup_type") or "*",
                row.get("horizon_class") or "*",
                row.get("market_regime") or "*",
                f"learning_mechanism:{row.get('learning_mechanism')}",
                action,
                multiplier,
                confidence,
                _safe_int(summary.get("total_trades")),
                reason,
                event_id,
                now,
                valid_until,
                _json_dumps(policy_payload),
            ),
        )
        inserted += 1
    return {"rows": inserted, "guarded_rows": guarded, "status": "applied", "candidate_rows": len(policy_rows)}

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
                "setup_type": group.get("setup_type") or "*",
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
                id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                policy_type, policy_action, multiplier, confidence_score, sample_count,
                reason, source_event_id, created_at, valid_until, payload_json, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, market_regime, policy_type)
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
                group.get("setup_type") or "*",
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
                "researcher_snapshot",
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
        FROM setup_type_performance
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
        pair_scope = {
            "ticker": str(row.get("ticker") or "").upper(),
            "side": str(row.get("side") or "").lower(),
            "setup_type": str(row.get("setup_type") or ""),
            "horizon_class": str(row.get("horizon_class") or ""),
            "market_regime": str(row.get("market_regime") or ""),
        }
        try:
            pair_rows = _completed_pairs_for_scope(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                scope=pair_scope,
            )
        except sqlite3.Error:
            pair_rows = [{"net_pnl": net_pnl, "close_date": trading_date} for _ in range(sample_count)]
        promotion_gate = _policy_promotion_gate(cfg=cfg, rows=pair_rows[:sample_count] or [{"net_pnl": net_pnl, "close_date": trading_date} for _ in range(sample_count)], action=action)
        counterfactual_reversal = (
            _counterfactual_reversal_stats(
                cursor,
                cfg=cfg,
                config_id=config_id,
                trading_date=trading_date,
                scope=pair_scope,
            )
            if action == "cap"
            else {"reversal": False, "enabled": bool(_policy_guard_config(cfg))}
        )
        if not promotion_gate["allowed"] or counterfactual_reversal.get("reversal"):
            if counterfactual_reversal.get("reversal"):
                _deactivate_adaptive_policy_state(
                    cursor,
                    config_id=config_id,
                    scope=pair_scope,
                    policy_type="template_quality",
                    reason="template quality cap deactivated by positive same-scope counterfactual reversal",
                )
            _insert_learning_event(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                event_type="adaptive_policy_guard",
                scope_type="template",
                scope_key=f"{row.get('ticker')}:{row.get('side')}:{row.get('setup_type')}",
                evidence={"template_row": dict(row), "promotion_gate": promotion_gate, "counterfactual_reversal": counterfactual_reversal},
                action={"policy_action": "keep_watchlist", "reason": "template policy guard blocked premature policy state"},
                status="guarded",
            )
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
                "setup_type": row.get("setup_type"),
                "horizon_class": row.get("horizon_class"),
                "market_regime": row.get("market_regime"),
            },
            evidence={
                **row,
                "sample_count": sample_count,
                "win_rate": win_rate,
                "net_pnl": net_pnl,
                "confidence_score": confidence,
                "policy_promotion_gate": promotion_gate,
                "counterfactual_reversal": counterfactual_reversal,
            },
        )
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="adaptive_policy_state",
            scope_type="template",
            scope_key=f"{row.get('ticker')}:{row.get('side')}:{row.get('setup_type')}",
            evidence=dict(row),
            action={"policy_action": action, "multiplier": multiplier, "reason": reason, CONTRACT_KEY: policy_payload[CONTRACT_KEY]},
        )
        cursor.execute(
            '''
            INSERT INTO adaptive_policy_state (
                id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                policy_type, policy_action, multiplier, confidence_score, sample_count,
                reason, source_event_id, created_at, valid_until, payload_json, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, market_regime, policy_type)
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
                row.get("setup_type"),
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
        template = _setup_type(side, combo, snapshot)
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
                "setup_type": template,
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
                id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                policy_type, policy_action, multiplier, confidence_score, sample_count,
                reason, source_event_id, created_at, valid_until, payload_json, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'tail_loss_sentinel', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, market_regime, policy_type)
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
        FROM setup_type_performance
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
        scope = {
            "ticker": row.get("ticker"),
            "side": row.get("side"),
            "setup_type": row.get("setup_type"),
            "horizon_class": row.get("horizon_class"),
            "market_regime": row.get("market_regime"),
        }
        try:
            scoped_pairs = _completed_pairs_for_scope(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                scope=scope,
            )
        except sqlite3.Error:
            scoped_pairs = [
                {"net_pnl": _safe_float(row.get("net_pnl")), "close_date": trading_date}
                for _ in range(_safe_int(row.get("sample_count")))
            ]
        promotion_gate = _policy_promotion_gate(cfg=cfg, rows=scoped_pairs, action="protect")
        if not promotion_gate["allowed"]:
            _insert_learning_event(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                event_type="alpha_promotion_guard",
                scope_type="ticker_side_template",
                scope_key=f"{row.get('ticker')}:{row.get('side')}:{row.get('setup_type')}",
                evidence={"template_row": dict(row), "promotion_gate": promotion_gate},
                action={"policy_action": "watchlist_only", "reason": "alpha promotion stayed watchlist until samples are less fragile"},
                status="guarded",
            )
            continue
        evidence = {
            "source": "setup_type_performance",
            "sample_count": _safe_int(row.get("sample_count")),
            "win_rate": _safe_float(row.get("win_rate")),
            "net_pnl": _safe_float(row.get("net_pnl")),
            "avg_pnl": _safe_float(row.get("avg_pnl")),
            "confidence_score": _safe_float(row.get("confidence_score")),
            "policy_promotion_gate": promotion_gate,
        }
        policy_payload = _policy_contract_payload(
            policy_type="alpha_promotion",
            policy_action="protect",
            reason="positive alpha promotion from verified template performance",
            multiplier=1.0,
            maturity_state="verified_alpha_memory",
            scope=scope,
            evidence=evidence,
        )
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="alpha_promotion",
            scope_type="ticker_side_template",
            scope_key=f"{row.get('ticker')}:{row.get('side')}:{row.get('setup_type')}",
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
                id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                policy_type, policy_action, multiplier, confidence_score, sample_count,
                reason, source_event_id, created_at, valid_until, payload_json, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'alpha_promotion', 'protect', 1.0, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, market_regime, policy_type)
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
                row.get("setup_type"),
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

    counterfactual_min_pnl = _safe_float(alpha_cfg.get("min_counterfactual_pnl"), min_net_pnl)
    cursor.execute(
        '''
        SELECT *
        FROM no_trade_opportunity_memory
        WHERE config_id = ?
          AND classification = 'missed_opportunity'
          AND counterfactual_results_json IS NOT NULL
        ORDER BY trading_date DESC
        LIMIT 200
        ''',
        (config_id,),
    )
    counterfactual_groups: Dict[Tuple[str, str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in cursor.fetchall():
        item = dict(row)
        results = _json_loads(item.get("counterfactual_results_json")) or []
        best_pnl = max([_safe_float(result.get("counterfactual_pnl")) for result in results if isinstance(result, dict)] or [0.0])
        if best_pnl < counterfactual_min_pnl:
            continue
        counterfactual_groups[
            (
                str(item.get("ticker") or "*"),
                str(item.get("side") or "*"),
                str(item.get("setup_type") or "*"),
                str(item.get("horizon_class") or "*"),
                str(item.get("market_regime") or "*"),
            )
        ].append({**item, "best_counterfactual_pnl": best_pnl})
    for (ticker, side, template, horizon, regime), items in counterfactual_groups.items():
        if len(items) < min_samples:
            continue
        net_counterfactual = sum(_safe_float(item.get("best_counterfactual_pnl")) for item in items)
        if net_counterfactual < min_net_pnl:
            continue
        confidence = min(0.90, 0.45 + len(items) / 20.0 + min(0.20, net_counterfactual / 50000.0))
        evidence = {
            "source": "no_trade_counterfactual_results",
            "sample_count": len(items),
            "net_counterfactual_pnl": net_counterfactual,
            "counterfactual_memory_ids": [item.get("id") for item in items[:20]],
            "confidence_score": confidence,
        }
        policy_payload = _policy_contract_payload(
            policy_type="alpha_promotion",
            policy_action="protect",
            reason="positive alpha promotion from missed-opportunity counterfactual results",
            multiplier=1.0,
            maturity_state="validated_counterfactual_alpha_memory",
            scope={
                "ticker": ticker,
                "side": side,
                "setup_type": template,
                "horizon_class": horizon,
                "market_regime": regime,
            },
            evidence=evidence,
        )
        event_id = _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="alpha_promotion_counterfactual",
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
                id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                policy_type, policy_action, multiplier, confidence_score, sample_count,
                reason, source_event_id, created_at, valid_until, payload_json, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'alpha_promotion', 'protect', 1.0, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, market_regime, policy_type)
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
                "positive alpha promotion from missed-opportunity counterfactual results",
                event_id,
                now,
                valid_until,
                _json_dumps(policy_payload),
            ),
        )
        inserted += 1
    return inserted

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
    min_counterfactual_pnl = _safe_float(calibration_cfg.get("min_counterfactual_pnl_for_relaxation"), 1200.0)
    min_counterfactual_loss = abs(_safe_float(calibration_cfg.get("min_counterfactual_loss_for_tightening"), 1200.0))
    max_rows = int(calibration_cfg.get("max_rows_per_day", 10) or 10)
    inserted = 0

    cursor.execute(
        '''
        SELECT *
        FROM no_trade_opportunity_memory
        WHERE config_id = ?
          AND status = 'closed'
          AND classification IN ('missed_opportunity', 'correct_avoidance')
          AND counterfactual_results_json IS NOT NULL
        ORDER BY last_reviewed_at DESC, trading_date DESC
        LIMIT ?
        ''',
        (config_id, max_rows * 3),
    )
    for row in cursor.fetchall():
        if inserted >= max_rows:
            break
        item = dict(row)
        results = _json_loads(item.get("counterfactual_results_json")) or []
        pnl_values = [_safe_float(result.get("counterfactual_pnl")) for result in results if isinstance(result, dict)]
        if not pnl_values:
            continue
        latest_pnl = pnl_values[-1]
        reason_text = normalize_no_trade_reason(item.get("execution_reason") or item.get("pm_reason") or "")
        category = categorize_no_trade_reason(reason_text)["category"]
        scope = {
            "ticker": item.get("ticker"),
            "side": item.get("side"),
            "setup_type": item.get("setup_type"),
            "horizon_class": item.get("horizon_class"),
            "market_regime": item.get("market_regime"),
        }
        evidence = {
            "source": "no_trade_opportunity_memory_counterfactual",
            "memory_id": item.get("id"),
            "classification": item.get("classification"),
            "no_trade_reason": reason_text,
            "no_trade_reason_category": category,
            "counterfactual_pnl": latest_pnl,
            "counterfactual_results": results,
        }
    for recommendation in strategy_recommendations:
        if inserted >= max_rows:
            break
        snapshot = _recommendation_snapshot(recommendation)
        final_contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        learning_used = final_contract.get("learning_used") if isinstance(final_contract.get("learning_used"), dict) else {}
        impact = learning_used.get("pm_lifecycle_learning_impact_delta")
        impact = impact if isinstance(impact, dict) else {}
        formal_impact = {
            field: impact.get(field)
            for field in PM_LIFECYCLE_CALIBRATION_FIELDS
            if field in impact
        }
        decision = str(
            formal_impact.get("hold_decision")
            or formal_impact.get("reduce_exit_decision")
            or ""
        )
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
            "setup_type": _setup_type(side, combo, snapshot),
            "horizon_class": _horizon_class(_expected_horizon_days(snapshot, side), snapshot),
            "market_regime": _market_regime(snapshot),
        }
        rules = {}
        reason = "same-scope PM lifecycle/horizon observation keeps strict validation until future evidence improves"
        if "horizon_mismatch" in decision:
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
                "source": "final_action_contract.learning_used.pm_lifecycle_learning_impact_delta",
                "recommendation_id": recommendation.get("id"),
                "decision": decision,
                "pm_lifecycle_learning_impact_delta": formal_impact,
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
            "setup_type": "*",
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
                        "setup_type": "*",
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
    # No validated parameter-optimization producer exists yet. Copying the
    # current configuration is not learning and must not create refresh events
    # or active overlays that PM will consume on the next trading day.
    _ensure_research_learning_schema(cursor)
    cursor.execute(
        """
        UPDATE config_learning_overlay
        SET active = 0
        WHERE config_id = ?
          AND active = 1
          AND source = 'reviewer'
          AND reason = 'capital utilization hard target is managed as reviewer overlay'
        """,
        (config_id,),
    )
    return 0

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
    counterfactual_summary = _neutral_counterfactual_tracking_summary(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        recommendations=recommendations,
    )
    summary["counterfactual_tracking"] = counterfactual_summary
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
            "counterfactual_tracking": counterfactual_summary,
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

def _backfill_neutral_forward_counterfactual_tracking(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    account_cfg = (((cfg or {}).get("signal_quality") or {}).get("neutral_accountability") or {})
    forward_days = max(0, int(account_cfg.get("counterfactual_forward_days", 0) or 0))
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
              AND event_type = 'neutral_forward_counterfactual_tracking'
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
        summary = _neutral_counterfactual_tracking_summary(
            cursor,
            cfg=cfg,
            config_id=config_id,
            trading_date=day,
            recommendations=recommendations,
        )
        if summary.get("forward_status") != "applied":
            continue
        _insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=day,
            event_type="neutral_forward_counterfactual_tracking",
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

def _write_neutral_accountability_digests(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    summary: Dict[str, Any],
) -> int:
    account_cfg = (((cfg or {}).get("signal_quality") or {}).get("neutral_accountability") or {})
    if not bool(account_cfg.get("write_structured_learning", True)):
        return 0

    by_analyst = summary.get("by_analyst") or {}
    if not isinstance(by_analyst, dict):
        return 0
    counterfactual_by_analyst: Dict[str, Counter] = defaultdict(Counter)
    counterfactual_summary = summary.get("counterfactual_tracking") if isinstance(summary.get("counterfactual_tracking"), dict) else {}
    for item in (counterfactual_summary.get("examples") if isinstance(counterfactual_summary, dict) else []) or []:
        if not isinstance(item, dict):
            continue
        analyst = str(item.get("analyst") or "")
        if not analyst:
            continue
        counterfactual_by_analyst[analyst]["observation_count"] += 1
        classification = str(item.get("classification") or "")
        if classification == "missed_opportunity":
            counterfactual_by_analyst[analyst]["missed_opportunity_count"] += 1
        elif classification == "reasonable_avoidance":
            counterfactual_by_analyst[analyst]["reasonable_avoidance_count"] += 1
        counterfactual_by_analyst[analyst]["total_counterfactual_pnl"] += _safe_float(item.get("counterfactual_pnl"), 0.0)

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
            "counterfactual_tracking": dict(counterfactual_by_analyst.get(str(analyst), Counter())),
        }
        digest = _neutral_accountability_digest_text(
            analyst,
            dominant_category,
            category_counts,
            counterfactual_counts=evidence["counterfactual_tracking"],
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
                "If repeated forward counterfactual results show missed opportunities, promote through same-scope validation before PM sizing impact.",
            ],
            validation_plan=[
                "Use same-day and configured forward counterfactual tracking after settlement to classify future Neutral outcomes.",
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
    state = _build_capital_deployment_state(
        cfg=cfg,
        settlement_row=settlement_row,
        strategy_recommendations=strategy_recommendations,
        no_trade_reason_counter=no_trade_reason_counter,
    )
    deployment_plan = state["deployment_plan"]
    deployment_diagnostics = state["capital_diagnostics"]
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
            state["capital_base"],
            state["current_margin"],
            state["current_margin_ratio"],
            state["target_margin_ratio_min"],
            state["target_margin_ratio_max"],
            state["target_margin_abs_min"],
            state["target_margin_abs_max"],
            1 if state["underutilization_breach"] else 0,
            1 if state["overutilization_breach"] else 0,
            state["margin_gap_to_min"],
            state["capital_allocation_tier"],
            state["reason_bucket"],
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
                "diagnosis": state["reason_bucket"],
                "primary_category": deployment_diagnostics["primary_category"],
                "alpha_release_candidate_count": deployment_diagnostics["alpha_release_candidate_count"],
                "recovery_probe_candidate_count": deployment_diagnostics["recovery_probe_candidate_count"],
                "parameter_review": deployment_diagnostics["parameter_review"],
            },
            status=(
                "breach"
                if state["underutilization_breach"] or state["overutilization_breach"]
                else "applied"
            ),
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
        SELECT ticker, side, setup_type, horizon_class, market_regime,
               sample_count, win_rate, net_pnl, avg_pnl, profit_factor,
               last_sample_date, payload_json
        FROM setup_type_performance
        WHERE config_id = ?
          AND sample_count >= ?
          AND last_sample_date IS NOT NULL
          AND last_sample_date <= ?
          AND (net_pnl <= ? OR win_rate <= 0.25)
        """,
        (config_id, consecutive_threshold, trading_date, loss_cap),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    inserted = 0
    for row in rows:
        ticker = str(row.get("ticker") or "*").upper()
        side = str(row.get("side") or "*").lower()
        template = str(row.get("setup_type") or "*")
        horizon = str(row.get("horizon_class") or "*")
        net_pnl = _safe_float(row.get("net_pnl"))
        win_rate = _safe_float(row.get("win_rate"))
        if net_pnl <= loss_cap:
            action = "probe_only"
            multiplier = float(provisional_cfg.get("anomaly_loss_cap_multiplier", 0.25) or 0.25)
            event_type = "anomaly_loss"
        elif win_rate <= 0.25:
            action = "probe_only"
            multiplier = float(provisional_cfg.get("consecutive_loss_multiplier", 0.35) or 0.35)
            event_type = "consecutive_setup_losses"
        else:
            continue
        payload = {
            "trading_date": trading_date,
            "source_trading_date": str(row.get("last_sample_date") or "")[:10],
            "ticker": ticker,
            "side": side,
            "setup_type": template,
            "horizon_class": horizon,
            "net_pnl": net_pnl,
            "win_rate": win_rate,
            "sample_count": int(row.get("sample_count") or 0),
            "rollback_value": {"policy_action": "inactive"},
        }
        payload = _policy_contract_payload(
            policy_type="provisional_policy_state",
            policy_action=action,
            reason=f"early risk sentinel: {event_type}, net_pnl={net_pnl:.0f}, win_rate={win_rate:.2%}",
            multiplier=multiplier,
            maturity_state="provisional_risk_sentinel",
            scope={
                "ticker": ticker,
                "side": side,
                "setup_type": template,
                "horizon_class": horizon,
                "market_regime": str(row.get("market_regime") or "*"),
            },
            evidence={
                **payload,
                "event_type": event_type,
                "confidence_score": min(0.85, max(0.35, abs(net_pnl) / 25000.0 + (1.0 - win_rate) * 0.25)),
            },
        )
        payload["rollback_value"] = {"policy_action": "inactive"}
        cursor.execute(
            """
            INSERT INTO provisional_policy_state (
                id, config_id, ticker, side, setup_type, horizon_class,
                policy_action, multiplier, confidence_score, event_type,
                sample_count, reason, source_trading_date, rollback_value_json, created_at,
                valid_until, active, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, policy_action)
            DO UPDATE SET
                multiplier=excluded.multiplier,
                confidence_score=excluded.confidence_score,
                event_type=excluded.event_type,
                sample_count=excluded.sample_count,
                reason=excluded.reason,
                source_trading_date=excluded.source_trading_date,
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
                event_type,
                int(row.get("sample_count") or 0),
                f"early risk sentinel: {event_type}, net_pnl={net_pnl:.0f}, win_rate={win_rate:.2%}",
                str(row.get("last_sample_date") or "")[:10],
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
    cursor.execute(
        """
        SELECT ticker, side, setup_type, horizon_class, market_regime,
               sample_count, win_rate, net_pnl, avg_pnl, profit_factor,
               confidence_score, valid_until, payload_json
        FROM setup_type_performance
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
    write_artifact_text(path, _json_dumps(payload))
    return str(path)


def _upsert_alpha_setup_policy_state(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    ticker: str,
    side: str,
    horizon_class: str,
    market_regime: str,
    policy_type: str,
    policy_action: str,
    multiplier: float,
    confidence_score: float,
    sample_count: int,
    reason: str,
    source_event_id: str,
    created_at: str,
    valid_until: str,
    payload_json: str,
) -> None:
    cursor.execute(
        """
        INSERT INTO adaptive_policy_state (
            id, config_id, ticker, side, setup_type, horizon_class, market_regime,
            policy_type, policy_action, multiplier, confidence_score, sample_count,
            reason, source_event_id, created_at, valid_until, payload_json, active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(config_id, ticker, side, setup_type, horizon_class, market_regime, policy_type)
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
        """,
        (
            str(uuid.uuid4()),
            config_id,
            ticker,
            side,
            "*",
            horizon_class,
            market_regime,
            policy_type,
            policy_action,
            multiplier,
            confidence_score,
            sample_count,
            reason,
            source_event_id,
            created_at,
            valid_until,
            payload_json,
        ),
    )


def _insert_researcher_llm_note(
    cursor: sqlite3.Cursor,
    *,
    note_id: str,
    config_id: str,
    trading_date: str,
    evidence_pack_id: str,
    ticker: str,
    raw_prompt: str,
    raw_response: str,
    created_at: str,
    payload_json: str,
    raw_prompt_artifact_path: Optional[str],
    raw_prompt_sha256: Optional[str],
    raw_prompt_size: Optional[int],
    raw_prompt_summary_json: Optional[str],
    raw_response_artifact_path: Optional[str],
    raw_response_sha256: Optional[str],
    raw_response_size: Optional[int],
    raw_response_summary_json: Optional[str],
    payload_artifact_path: Optional[str],
    payload_sha256: Optional[str],
    payload_size: Optional[int],
    payload_summary_json: Optional[str],
) -> None:
    forbidden_values = (
        raw_prompt,
        raw_response,
        raw_prompt_artifact_path,
        raw_prompt_sha256,
        raw_prompt_summary_json,
        raw_response_artifact_path,
        raw_response_sha256,
        raw_response_summary_json,
    )
    if any(value not in (None, "") for value in forbidden_values) or raw_prompt_size not in (None, 0) or raw_response_size not in (None, 0):
        raise ValueError("researcher_raw_llm_content_forbidden")
    cursor.execute(
        """
        INSERT INTO researcher_llm_notes (
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
            evidence_pack_id,
            ticker,
            raw_prompt,
            raw_response,
            created_at,
            payload_json,
            raw_prompt_artifact_path,
            raw_prompt_sha256,
            raw_prompt_size,
            raw_prompt_summary_json,
            raw_response_artifact_path,
            raw_response_sha256,
            raw_response_size,
            raw_response_summary_json,
            payload_artifact_path,
            payload_sha256,
            payload_size,
            payload_summary_json,
        ),
    )


def _reset_alpha_setup_memory(cursor: sqlite3.Cursor, *, config_id: str) -> None:
    cursor.execute("DELETE FROM alpha_setup_action_value WHERE config_id = ?", (config_id,))
    cursor.execute("DELETE FROM alpha_setup_profile WHERE config_id = ?", (config_id,))
    cursor.execute("DELETE FROM alpha_setup_sample WHERE config_id = ?", (config_id,))


ensure_research_learning_schema = _ensure_research_learning_schema
insert_learning_event = _insert_learning_event
insert_researcher_learning_completion_event = _insert_researcher_learning_completion_event
insert_causal_review_candidate = _insert_causal_review_candidate
insert_exploratory_hypothesis = _insert_exploratory_hypothesis
upsert_alpha_setup_sample = _upsert_alpha_setup_sample
upsert_alpha_setup_profile = _upsert_alpha_setup_profile
upsert_alpha_setup_action_value = _upsert_alpha_setup_action_value
upsert_alpha_setup_policy_state = _upsert_alpha_setup_policy_state
insert_researcher_llm_note = _insert_researcher_llm_note
reset_alpha_setup_memory = _reset_alpha_setup_memory
write_signal_context_history = _write_signal_context_history
write_strategy_memory_history = _write_strategy_memory_history
write_template_and_analyst_learning = _write_template_and_analyst_learning
write_trade_episode_memory = _write_trade_episode_memory
write_opportunity_ranking_learning_events = _write_opportunity_ranking_learning_events
write_no_trade_opportunity_memory = _write_no_trade_opportunity_memory
backfill_no_trade_opportunity_counterfactual_results = _backfill_no_trade_opportunity_counterfactual_results
backfill_neutral_forward_counterfactual_tracking = _backfill_neutral_forward_counterfactual_tracking
write_missed_alpha_accountability_state = _write_missed_alpha_accountability_state
write_research_position_feedback = _write_research_position_feedback
write_adaptive_policy_state = _write_adaptive_policy_state
write_tail_loss_sentinel_state = _write_tail_loss_sentinel_state
write_alpha_promotion_state = _write_alpha_promotion_state
write_contextual_rule_calibration_state = _write_contextual_rule_calibration_state
write_loss_template_observation_research = _write_loss_template_observation_research
write_fast_loss_sentinel_state = _write_fast_loss_sentinel_state
write_learned_vs_unlearned_policy_state = _write_learned_vs_unlearned_policy_state
write_learning_mechanism_policy_state = _write_learning_mechanism_policy_state
write_provisional_policy_state = _write_provisional_policy_state
write_config_overlay = _write_config_overlay
write_neutral_accountability_state = _write_neutral_accountability_state
write_neutral_accountability_digests = _write_neutral_accountability_digests
write_capital_deployment_state = _write_capital_deployment_state
write_validated_causal_policy_rules = _write_validated_causal_policy_rules
export_template_prior = _export_template_prior
