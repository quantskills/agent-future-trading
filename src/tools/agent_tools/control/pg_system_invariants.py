from __future__ import annotations

"""Runtime system-invariant audits for backtest/simulation acceptance.

These checks are deliberately observational. They read persisted artifacts and
transactions after a run and fail on protocol violations; they never create
trade authority, adjust sizing, or change strategy behavior.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from database.artifact_store import load_externalized_json
from tools.agent_tools.control.pg_db_schema_contract import audit_db_schema_contract
from tools.agent_tools.control.pg_schemas import ProtocolCheckResult
from tools.agent_tools.control.pg_unified_field_audit import find_forbidden_artifact_field_keys
from tools.common.order_semantics import (
    phase2_order_intent_from_lots,
    recommendation_intent_from_lots,
)
from tools.common.final_action_semantics import (
    ACTION_PREFERENCE_VALUES,
    contract_consumes_hold_exit_pm_learning,
    full_market_rank_gate_errors,
    contract_increases_risk_position,
    contract_reduces_or_exits_position,
    derive_memory_requirements,
    has_active_opportunity_rejection,
    has_open_transaction_blocker,
    has_valid_generic_no_change_explanation,
    has_valid_hold_exit_no_change_explanation,
    is_conditional_monitor_contract,
    lane_matches_memory_requirement,
    rank_capital_layer_contract_errors,
    validate_final_action_lot_transition,
)
from tools.common.adaptive_policy_safety import adaptive_policy_runtime_decision


OPEN_ACTIONS = {"open_long", "open_short"}
CLOSE_ACTIONS = {"close_long", "close_short"}
OPEN_FINAL_ACTIONS = {"open_probe", "open_real"}
OPEN_AUTHORITY_TYPES = {"exploration_probe", "real_budget_entry"}
STRATEGY_SOURCE_TYPE = "strategy"
ROLLOVER_SOURCE_TYPE = "rollover"
FORCED_RISK_SOURCE_TYPE = "forced_risk"
OPERATIONAL_SOURCE_TYPES = {ROLLOVER_SOURCE_TYPE, FORCED_RISK_SOURCE_TYPE}
TRIGGER_PASSED_REASONS = {
    "intraday_trigger_confirmed",
    "intraday_pullback_confirmed",
    "intraday_vwap_confirmed",
    "intraday_immediate_execution",
    "intraday_event_immediate_execution",
}
REAL_REWARD_SOURCE_MARKERS = {"episode", "real"}
ACTION_PREFERENCE_LANDING_TERMS = {
    "positive_candidate_open": {"positive_candidate_open", "real_budget_entry", "exploration_probe", "open_real", "open_probe"},
    "positive_candidate_hold": {"positive_candidate_hold", "hold", "position_matched", "continue"},
    "positive_candidate_exit": {"positive_candidate_exit", "exit", "close", "reduce", "scale_down", "protect"},
    "positive_candidate_execution": {
        "positive_candidate_execution",
        "execution_profile",
        "breakout",
        "pullback",
        "vwap",
        "opening_range",
        "event_immediate",
    },
    "negative_revalidate": {"negative_revalidate", "revalidate", "wait", "watchlist", "cap", "demote", "reduce", "exit"},
    "negative_hold_revalidate": {"negative_hold_revalidate", "revalidate", "protect", "reduce", "exit", "scale_down"},
    "tail_loss_protect": {"tail_loss_protect", "tail_loss", "protect", "protective", "reduce", "exit", "scale_down", "stop_loss"},
}
OPPORTUNITY_SCORE_COMPONENT_FIELDS = {
    "positive_learning",
    "negative_learning",
    "execution_profile_learning",
    "recent_tail_loss_penalty",
}
PM_LEARNING_RANKING_AUDIT_BOUNDARIES = [
    "learning_components_only_inside_opportunity_score_components",
    "ranking_fields_cannot_create_trade_authority",
    "final_action_contract_remains_single_trade_truth",
    "recommendation_top_level_action_lots_must_match_final_contract",
    "incomplete_trading_day_cannot_enter_strategy_evaluation",
    "pm_action_value_transport_must_preserve_preference_reward_and_scope",
    "matching_real_action_value_must_land_in_pm_score_components",
    "profile_prior_cannot_substitute_for_action_value_learning",
    "learning_signal_must_explain_contract_no_change",
    "rank_change_must_explain_contract_no_change",
    "hold_exit_learning_must_explain_position_no_change",
]
OPEN_AMPLIFICATION_EFFECTS = {
    "open_amplification",
    "real_budget_entry",
    "real_budget_entry_candidate",
    "scale",
    "scale_candidate",
    "scale_position",
    "change_margin_ratio",
}
EXECUTION_INTENT_MUTATION_EFFECTS = {
    "change_direction",
    "change_lots",
    "change_target_lots",
    "change_margin_ratio",
    "create_trade_authority",
    "direct_trade_authority",
    "open_amplification",
    "real_budget_entry",
    "scale_position",
}
RELEASE_BLOCK_DIAGNOSTIC_FORBIDDEN_FIELDS = {
    "authority_type",
    "execution_profile",
    "final_action",
    "lots",
    "lots_delta",
    "margin_ratio",
    "max_allowed_margin_ratio",
    "target_lots",
    "target_margin_ratio_estimate",
    "target_position_ratio",
}
PM_EXPLANATION_FIELDS = {
    "capital_allocation_reason",
    "learning_adjustment_summary",
    "learning_used",
    "opportunity_rank",
    "opportunity_score",
    "opportunity_score_components",
    "position_sizing_result",
}
PM_INTERNAL_DRAFT_FIELDS = {
    "pm_internal_draft",
    "pm_scoring_draft",
    "pm_ranking_draft",
    "pm_capital_deployment_draft",
    "pm_contract_submission_draft",
    "internal_pm_draft",
}
TRADE_INTENT_FIELDS = {
    "action",
    "can_execute_without_intraday_trigger",
    "current_lots",
    "entry_trigger",
    "execution_profile",
    "final_action",
    "lots",
    "lots_delta",
    "requires_intraday_confirmation",
    "target_lots",
}
ARTIFACT_PHASE_BOUNDARY_ERROR_PREFIXES = {
    "pm_artifact_forbidden_downstream_field",
    "pm_artifact_forbidden_internal_draft_field",
    "auditor_artifact_forbidden_contract_mutation",
    "trader_artifact_forbidden_pm_explanation",
    "transaction_audit_payload_forbidden_pm_contract_mirror",
    "accountant_artifact_forbidden_learning_field",
    "accountant_artifact_forbidden_trade_action_mutation",
    "reviewer_artifact_forbidden_action_value_write",
    "reviewer_artifact_forbidden_research_state_write",
    "researcher_artifact_forbidden_trade_fact_mutation",
}
UNIFIED_FIELD_SEMANTIC_ERROR_PREFIXES = {
    "unified_field_artifact_forbidden_field",
    "release_block_diagnostics_contains_trade_action_fields",
    "action_evidence_contract_pending_trigger_marked_valid",
    "trigger_valid_without_current_trigger_confirmed",
    "setup_quality_ok_used_as_current_trigger",
    "trade_research_action_evidence_trigger_valid_mismatch",
}
PENDING_ENTRY_TRIGGER_MARKERS = {
    "only if",
    "only after",
    "if price",
    "if futures",
    "if volume",
    "if basis",
    "if inventory",
    "would require",
    "becomes tradeable",
    "become tradeable",
    "tradeable only if",
    "make the setup tradeable",
    "move from watchlist to tradeable",
    "convert to a tradeable",
    "requires price",
    "requires technical",
    "requires market",
    "requires current",
    "wait for",
    "waiting for",
    "should confirm",
    "must confirm",
    "must break",
    "must hold",
    "requires confirmation",
    "requires a confirmation",
    "requires confirmed",
    "requires a confirmed",
    "requires break",
    "requires a break",
    "requires breakout",
    "requires a breakout",
    "requires breakdown",
    "requires a breakdown",
    "needs price",
    "needs technical",
    "needs market",
    "needs confirmation",
    "require confirmation",
    "require a confirmation",
    "require confirmed",
    "require a confirmed",
    "require break",
    "require a break",
    "require breakout",
    "require a breakout",
    "require breakdown",
    "require a breakdown",
    "require post-open",
    "requires post-open",
    "needs post-open",
    "after the open",
    "after open",
    "without that confirmation",
    "without confirmation",
    "remain on watch",
    "remains on watch",
    "stay on watch",
    "stays on watch",
    "before entry",
    "before execution",
    "until price",
    "until futures",
    "如果",
    "若",
    "需要",
    "等待",
    "确认后",
    "后再",
    "之后再",
    "才可",
}
CURRENT_ENTRY_TRIGGER_MARKERS = {
    "has broken",
    "has breached",
    "has crossed",
    "has confirmed",
    "is breaking",
    "is below",
    "is above",
    "currently below",
    "currently above",
    "current breakout",
    "current breakdown",
    "trigger is active",
    "trigger is valid",
    "confirmed by current",
    "已突破",
    "已跌破",
    "已站上",
    "已站稳",
    "已经突破",
    "已经跌破",
    "当前突破",
    "当前跌破",
    "触发成立",
}

CURRENT_CONFIRMATION_FIELD_NAMES = {
    "current_trigger_confirmed",
    "short_term_trigger_confirmed",
    "short_term_confirmation_confirmed",
    "technical_confirmation_confirmed",
    "price_trigger_confirmed",
    "intraday_confirmation_confirmed",
    "market_confirmation_confirmed",
    "execution_trigger_confirmed",
    "price_reaction_confirmed",
    "current_entry_confirmed",
}
ERROR_CATEGORY_PREFIXES = {
    "unified_field_semantics": UNIFIED_FIELD_SEMANTIC_ERROR_PREFIXES,
    "pm_opportunity_routing": {
        "trigger_valid_without_current_trigger_confirmed",
        "setup_quality_ok_used_as_current_trigger",
        "conditional_monitor_candidate_silent_wait",
        "high_quality_opportunity_silent_wait",
        "opportunity_ranking_field_top_level_trade_authority",
    },
    "single_trade_exit": {
        "open_transaction_without_open_final_action",
        "open_transaction_without_open_authority",
        "open_transaction_with_blocking_authority",
        "real_open_without_current_contract_evidence",
        "direction_or_watchlist_probe_opened",
        "trade_contract_source_of_truth_failed",
        "recommendation_final_action_contract_missing_fields",
        "recommendation_final_action_contract_lots_delta_mismatch",
        "recommendation_final_action_contract_action_mismatch",
        "recommendation_top_level_action_lots_mismatch_final_action_contract",
        "strategy_recommendation_non_strategy_final_action_contract",
        "opportunity_ranking_field_used_in_execution_trade_intent",
        "pm_artifact_forbidden_downstream_field",
        "auditor_artifact_forbidden_contract_mutation",
        "trader_artifact_forbidden_pm_explanation",
        "transaction_audit_payload_forbidden_pm_contract_mirror",
    },
    "trader_trigger_parity": {
        "open_transaction_without_trigger",
        "open_transaction_without_intraday_trigger",
        "intraday_trigger_audit_mirror_mismatch",
        "open_transaction_without_intraday_record",
    },
    "learning_landing": {
        "action_value_missing_action_preference",
        "action_value_unknown_action_preference",
        "pm_action_value_missing_canonical_fields",
        "pm_consumed_non_pm_learning_action_value",
        "action_value_missing_consumer_scope",
        "trader_execution_learning_trace_missing_scope",
        "trader_execution_learning_trace_wrong_scope",
        "pm_learning_components_zero_despite_prior_real_action_value",
        "profile_adjustment_substituted_for_missing_action_value_learning",
        "pm_learning_signal_without_contract_effect_or_explanation",
        "pm_rank_changed_without_contract_effect",
        "pm_hold_exit_learning_without_contract_effect_or_explanation",
        "positive_open_action_value_not_open_preference",
        "positive_exit_action_value_not_exit_preference",
        "negative_action_value_not_protective_preference",
        "positive_open_from_non_exact_scope",
        "positive_open_from_non_real_reward_source",
        "action_preferences_exist_but_no_final_action_contract_mentions_them",
        "adaptive_policy_release_not_validated",
        "adaptive_policy_unknown_action",
        "adaptive_policy_fast_candidate_not_probe_only",
        "opportunity_learning_component_used_as_trade_intent",
    },
    "structured_io": {
        "unified_field_artifact_forbidden_field",
        "accountant_artifact_forbidden_learning_field",
        "accountant_artifact_forbidden_trade_action_mutation",
        "reviewer_artifact_forbidden_action_value_write",
        "reviewer_artifact_forbidden_research_state_write",
        "researcher_artifact_forbidden_trade_fact_mutation",
    },
    "data_time_boundary": {
        "incomplete_trading_day_phase",
        "future_dated_learning_used",
        "schema_missing_required_table",
        "schema_missing_required_column",
        "schema_missing_date_column",
    },
}


@dataclass
class InvariantAuditReport:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_protocol_result(self) -> ProtocolCheckResult:
        if self.ok:
            return ProtocolCheckResult.pass_result(warnings=self.warnings, metadata={"counts": self.counts, **self.metadata})
        return ProtocolCheckResult.fail_result(
            self.errors,
            warnings=self.warnings,
            metadata={"counts": self.counts, **self.metadata},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "counts": dict(self.counts),
            "metadata": dict(self.metadata),
        }


def categorize_invariant_errors(errors: Iterable[str]) -> Dict[str, List[str]]:
    categories: Dict[str, List[str]] = {}
    for error in errors or []:
        prefix = str(error).split(":", 1)[0]
        matched_categories: List[str] = []
        for category, prefixes in ERROR_CATEGORY_PREFIXES.items():
            if prefix in prefixes:
                categories.setdefault(category, []).append(str(error))
                matched_categories.append(category)
        if not matched_categories:
            categories.setdefault("audit_explainability", []).append(str(error))
    return categories


def _unified_field_semantics_audit_summary(errors: Iterable[str]) -> Dict[str, Any]:
    semantic_errors: List[str] = []
    for error in errors or []:
        prefix = str(error).split(":", 1)[0]
        if prefix in UNIFIED_FIELD_SEMANTIC_ERROR_PREFIXES:
            semantic_errors.append(str(error))
    return {
        "ok": not semantic_errors,
        "source_of_truth": "docs/unified_field_semantics.md",
        "error_count": len(semantic_errors),
        "errors": semantic_errors,
        "checked_boundaries": [
            "runtime_artifacts_must_not_use_deprecated_semantic_keys",
            "action_evidence_contract_is_canonical_pm_input",
            "trigger_valid_requires_current_trigger_confirmed",
            "setup_quality_ok_cannot_imply_trigger_valid",
            "trade_research_contract_and_action_evidence_contract_must_not_disagree",
            "diagnostics_cannot_carry_trade_action_fields",
        ],
    }


def _safe_json(value: Any, artifact_path: Optional[str] = None, sha256: Optional[str] = None) -> Any:
    loaded = load_externalized_json(value, artifact_path, sha256)
    if isinstance(loaded, str):
        try:
            return json.loads(loaded)
        except Exception:
            return {}
    return loaded if loaded is not None else {}


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _nested_dict(value: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _nested_value(value: Dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _iter_nested_dicts(value: Any, *, prefix: str = "") -> Iterable[tuple[str, Dict[str, Any]]]:
    if isinstance(value, dict):
        yield prefix, value
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _iter_nested_dicts(item, prefix=path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_nested_dicts(item, prefix=f"{prefix}[{index}]")


def _entry_trigger_has_pending_confirmation(value: Any) -> bool:
    text = _lower(value)
    if not text:
        return False
    if not any(marker in text for marker in PENDING_ENTRY_TRIGGER_MARKERS):
        return False
    has_current = any(marker in text for marker in CURRENT_ENTRY_TRIGGER_MARKERS)
    strict_pending = any(
        marker in text
        for marker in {
            "only if",
            "only after",
            "would require",
            "requires confirmed",
            "requires a confirmed",
            "requires break",
            "requires a break",
            "requires breakout",
            "requires a breakout",
            "requires breakdown",
            "requires a breakdown",
            "require confirmed",
            "require a confirmed",
            "require break",
            "require a break",
            "require breakout",
            "require a breakout",
            "require breakdown",
            "require a breakdown",
            "after the open",
            "after open",
            "without that confirmation",
            "without confirmation",
            "remain on watch",
            "remains on watch",
            "stay on watch",
            "stays on watch",
            "wait for",
            "waiting for",
            "must confirm",
            "must break",
            "before entry",
            "before execution",
            "确认后",
            "后再",
            "之后再",
            "才可",
        }
    )
    return strict_pending or not has_current


def _entry_trigger_has_current_confirmation(value: Any) -> bool:
    text = _lower(value)
    if not text:
        return False
    return any(marker in text for marker in CURRENT_ENTRY_TRIGGER_MARKERS)


def _node_has_current_confirmation(node: Dict[str, Any]) -> bool:
    if _entry_trigger_has_current_confirmation(node.get("entry_trigger")):
        return True
    for _path, nested in _iter_nested_dicts(node):
        for field_name in CURRENT_CONFIRMATION_FIELD_NAMES:
            if nested.get(field_name) is True:
                return True
    return False


def _date10(value: Any) -> str:
    return str(value or "").strip()[:10]


def _int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    try:
        return int(value)
    except Exception:
        return default


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _fetch_config_id(conn: sqlite3.Connection, *, config_id: Optional[str], exp_name: Optional[str]) -> Optional[str]:
    if config_id:
        return str(config_id)
    if not exp_name or not _table_exists(conn, "config"):
        return None
    row = conn.execute("SELECT id FROM config WHERE exp_name = ?", (exp_name,)).fetchone()
    return str(row["id"]) if row else None


def _has_invariant_records(conn: sqlite3.Connection) -> bool:
    for table_name in (
        "futures_recommendation",
        "futures_transactions",
        "futures_intraday_decision",
        "alpha_setup_action_value",
        "adaptive_policy_state",
        "daily_settlement",
        "researcher_llm_notes",
        "trading_day_phase",
    ):
        if not _table_exists(conn, table_name):
            continue
        try:
            row = conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
        except sqlite3.Error:
            continue
        if row and int(row["count"] or 0) > 0:
            return True
    return False


def _date_filter_sql(alias: str, start_date: Optional[str], end_date: Optional[str]) -> tuple[str, List[Any]]:
    parts: List[str] = []
    params: List[Any] = []
    if start_date:
        parts.append(f"substr({alias}.trading_date, 1, 10) >= ?")
        params.append(start_date)
    if end_date:
        parts.append(f"substr({alias}.trading_date, 1, 10) <= ?")
        params.append(end_date)
    return (" AND " + " AND ".join(parts), params) if parts else ("", params)


def _load_recommendations(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    if not _table_exists(conn, "futures_recommendation"):
        return {}
    date_sql, params = _date_filter_sql("r", start_date, end_date)
    rows = conn.execute(
        f"""
        SELECT *
        FROM futures_recommendation r
        WHERE r.config_id = ?{date_sql}
        ORDER BY r.trading_date ASC, r.created_at ASC
        """,
        (config_id, *params),
    ).fetchall()
    recommendations: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        item["signal_snapshot"] = _safe_json(
            item.get("signal_snapshot"),
            item.get("signal_snapshot_artifact_path"),
            item.get("signal_snapshot_sha256"),
        )
        item["audit_payload"] = _safe_json(
            item.get("audit_payload"),
            item.get("audit_payload_artifact_path"),
            item.get("audit_payload_sha256"),
        )
        recommendations[str(item.get("id"))] = item
    return recommendations


def _load_transactions(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "futures_transactions"):
        return []
    date_sql, params = _date_filter_sql("t", start_date, end_date)
    rows = conn.execute(
        f"""
        SELECT *
        FROM futures_transactions t
        WHERE t.config_id = ?{date_sql}
        ORDER BY t.trading_date ASC, t.created_at ASC
        """,
        (config_id, *params),
    ).fetchall()
    transactions: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["audit_payload"] = _safe_json(
            item.get("audit_payload"),
            item.get("audit_payload_artifact_path"),
            item.get("audit_payload_sha256"),
        )
        transactions.append(item)
    return transactions


def _load_intraday_decisions(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "futures_intraday_decision"):
        return []
    date_sql, params = _date_filter_sql("d", start_date, end_date)
    rows = conn.execute(
        f"""
        SELECT *
        FROM futures_intraday_decision d
        WHERE d.config_id = ?{date_sql}
        ORDER BY d.trading_date ASC, d.created_at ASC
        """,
        (config_id, *params),
    ).fetchall()
    decisions: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["features"] = _safe_json(item.get("features_json"))
        decisions.append(item)
    return decisions


def _load_action_values(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "alpha_setup_action_value"):
        return []
    params: List[Any] = [config_id]
    date_parts: List[str] = []
    if start_date:
        date_parts.append("(last_sample_date IS NULL OR substr(last_sample_date, 1, 10) >= ?)")
        params.append(start_date)
    if end_date:
        date_parts.append("(last_sample_date IS NULL OR substr(last_sample_date, 1, 10) <= ?)")
        params.append(end_date)
    date_sql = (" AND " + " AND ".join(date_parts)) if date_parts else ""
    rows = conn.execute(
        f"""
        SELECT *
        FROM alpha_setup_action_value
        WHERE config_id = ? AND active = 1{date_sql}
        """,
        tuple(params),
    ).fetchall()
    values: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["payload"] = _safe_json(item.get("payload_json"))
        values.append(item)
    return values


def _load_adaptive_policy_states(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "adaptive_policy_state"):
        return []
    params: List[Any] = [config_id]
    date_parts: List[str] = []
    if start_date:
        date_parts.append("(source_trading_date IS NULL OR substr(source_trading_date, 1, 10) >= ?)")
        params.append(start_date)
    if end_date:
        date_parts.append("(source_trading_date IS NULL OR substr(source_trading_date, 1, 10) <= ?)")
        params.append(end_date)
    date_sql = (" AND " + " AND ".join(date_parts)) if date_parts else ""
    rows = conn.execute(
        f"""
        SELECT *
        FROM adaptive_policy_state
        WHERE config_id = ? AND active = 1{date_sql}
        """,
        tuple(params),
    ).fetchall()
    values: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["payload"] = _safe_json(item.get("payload_json"))
        values.append(item)
    return values


def _load_daily_settlements(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "daily_settlement"):
        return []
    if not _table_exists(conn, "portfolio"):
        return []
    date_sql, params = _date_filter_sql("ds", start_date, end_date)
    rows = conn.execute(
        f"""
        SELECT ds.*
        FROM daily_settlement ds
        JOIN portfolio p ON ds.portfolio_id = p.id
        WHERE p.config_id = ?{date_sql}
        ORDER BY ds.trading_date ASC
        """,
        (config_id, *params),
    ).fetchall()
    values: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in ("positions_snapshot", "artifact_payload", "payload_json"):
            if key in item:
                item[key] = _safe_json(item.get(key))
        values.append(item)
    return values


def _load_researcher_llm_notes(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "researcher_llm_notes"):
        return []
    params: List[Any] = [config_id]
    date_parts: List[str] = []
    if start_date:
        date_parts.append("(trading_date IS NULL OR substr(trading_date, 1, 10) >= ?)")
        params.append(start_date)
    if end_date:
        date_parts.append("(trading_date IS NULL OR substr(trading_date, 1, 10) <= ?)")
        params.append(end_date)
    date_sql = (" AND " + " AND ".join(date_parts)) if date_parts else ""
    rows = conn.execute(
        f"""
        SELECT *
        FROM researcher_llm_notes
        WHERE config_id = ?{date_sql}
        ORDER BY trading_date ASC, created_at ASC
        """,
        tuple(params),
    ).fetchall()
    values: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["payload"] = _safe_json(item.get("payload_json") or item.get("payload"))
        values.append(item)
    return values


def _load_trading_day_phases(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "trading_day_phase"):
        return []
    date_sql, params = _date_filter_sql("p", start_date, end_date)
    rows = conn.execute(
        f"""
        SELECT *
        FROM trading_day_phase p
        WHERE p.config_id = ?{date_sql}
        ORDER BY p.trading_date ASC, p.phase ASC
        """,
        (config_id, *params),
    ).fetchall()
    return [dict(row) for row in rows]


def _contract_from_recommendation(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _dict(recommendation.get("signal_snapshot"))
    audit_payload = _dict(recommendation.get("audit_payload"))
    for source in (snapshot, audit_payload):
        contract = _dict(source.get("final_action_contract"))
        if contract:
            return contract
    return {}


def _authority_from_recommendation(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    contract = _contract_from_recommendation(recommendation)
    return contract if contract else {}


def _audit_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _dict(payload.get("trade_contract_audit"))


def _is_open_transaction(transaction: Dict[str, Any]) -> bool:
    return _lower(transaction.get("action")) in OPEN_ACTIONS and _int(transaction.get("lots")) > 0


def _transaction_contract(transaction: Dict[str, Any], recommendation: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    # Transaction payloads may carry execution audit summaries, but the complete
    # PM decision contract remains a recommendation fact.
    return _contract_from_recommendation(recommendation or {})


def _transaction_authority(transaction: Dict[str, Any], recommendation: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    contract = _transaction_contract(transaction, recommendation)
    return contract if contract else {}


def _transaction_trade_contract_audit(transaction: Dict[str, Any]) -> Dict[str, Any]:
    return _audit_from_payload(_dict(transaction.get("audit_payload")))


def _source_type(recommendation: Dict[str, Any]) -> str:
    return _lower(recommendation.get("source_type") or "strategy")


def _transaction_source_type(transaction: Dict[str, Any], recommendation: Optional[Dict[str, Any]]) -> str:
    return _lower(
        transaction.get("source_type")
        or (recommendation or {}).get("source_type")
        or STRATEGY_SOURCE_TYPE
    )


def _strategy_contract_type_is_valid(contract: Dict[str, Any]) -> bool:
    contract_type = _lower(contract.get("contract_type") or "strategy")
    return contract_type in {"", "strategy"}


def _audit_recommendation_final_contract_consistency(
    recommendations: Dict[str, Dict[str, Any]],
    errors: List[str],
) -> None:
    for recommendation_id, recommendation in recommendations.items():
        contract = _contract_from_recommendation(recommendation)
        source_type = _source_type(recommendation)
        action = _lower(recommendation.get("action"))
        ticker = recommendation.get("underlying_code") or recommendation.get("ticker") or ""
        label = f"{recommendation.get('trading_date')}:{ticker}:{recommendation_id}"
        if source_type == ROLLOVER_SOURCE_TYPE:
            trading_day = _date10(recommendation.get("trading_date"))
            effective_day = _date10(recommendation.get("effective_trade_date"))
            if trading_day and effective_day and effective_day <= trading_day:
                errors.append(
                    "rollover_effective_trade_date_not_after_detection:"
                    f"{label}:effective_trade_date={effective_day}"
                )
            continue
        if source_type == FORCED_RISK_SOURCE_TYPE:
            if action in OPEN_ACTIONS:
                errors.append(f"forced_risk_recommendation_cannot_open:{label}:{action}")
            if contract:
                errors.append(f"forced_risk_recommendation_must_not_use_strategy_final_action_contract:{label}")
            continue
        if not contract:
            continue
        ticker = ticker or contract.get("ticker")
        label = f"{recommendation.get('trading_date')}:{ticker}:{recommendation_id}"
        if source_type == STRATEGY_SOURCE_TYPE and not _strategy_contract_type_is_valid(contract):
            errors.append(
                "strategy_recommendation_non_strategy_final_action_contract:"
                f"{label}:contract_type={contract.get('contract_type')}"
            )
            continue
        if source_type in OPERATIONAL_SOURCE_TYPES:
            continue
        required = {"current_lots", "target_lots", "lots_delta", "final_action"}
        missing = sorted(key for key in required if key not in contract)
        if missing:
            errors.append(f"recommendation_final_action_contract_missing_fields:{label}:{missing}")
            continue

        current_lots = _int(contract.get("current_lots"))
        target_lots = _int(contract.get("target_lots"), current_lots)
        lots_delta = _int(contract.get("lots_delta"), target_lots - current_lots)
        if lots_delta != target_lots - current_lots:
            errors.append(
                "recommendation_final_action_contract_lots_delta_mismatch:"
                f"{label}:current={current_lots}:target={target_lots}:delta={lots_delta}"
            )
            continue
        transition = validate_final_action_lot_transition(contract)
        if not transition.get("ok"):
            errors.append(
                "recommendation_final_action_contract_action_mismatch:"
                f"{label}:action={contract.get('final_action')}:"
                f"current={current_lots}:target={target_lots}:delta={lots_delta}"
            )
            continue
        expected_intent = recommendation_intent_from_lots(current_lots=current_lots, target_lots=target_lots)
        expected_action = _lower(expected_intent.get("action"))
        expected_lots = _int(expected_intent.get("lots"))
        actual_lots = _int(recommendation.get("lots"))
        if action != expected_action or actual_lots != expected_lots:
            errors.append(
                "recommendation_top_level_action_lots_mismatch_final_action_contract:"
                f"{label}:expected={expected_action}/{expected_lots}:actual={action}/{actual_lots}:"
                f"current={current_lots}:target={target_lots}"
            )


def _auditor_verdict_from_recommendation(recommendation: Dict[str, Any]) -> str:
    payload = _dict(recommendation.get("audit_payload"))
    verdict = _lower(payload.get("audit_verdict"))
    if verdict:
        return verdict
    independent = _dict(payload.get("independent_auditor"))
    verdict = _lower(independent.get("audit_verdict"))
    if verdict:
        return verdict
    snapshot = _dict(recommendation.get("signal_snapshot"))
    auditor = _dict(snapshot.get("auditor"))
    return _lower(auditor.get("audit_verdict"))


def _audit_independent_auditor_chain(
    recommendations: Dict[str, Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    errors: List[str],
) -> None:
    approved = {"approve", "approve_with_warning"}
    for recommendation_id, recommendation in recommendations.items():
        if _source_type(recommendation) != STRATEGY_SOURCE_TYPE:
            continue
        ticker = recommendation.get("underlying_code") or recommendation.get("ticker") or ""
        label = f"{recommendation.get('trading_date')}:{ticker}:{recommendation_id}"
        payload = _dict(recommendation.get("audit_payload"))
        snapshot_auditor = _dict(_dict(recommendation.get("signal_snapshot")).get("auditor"))
        independent_auditor = _dict(payload.get("independent_auditor"))
        verdict = _auditor_verdict_from_recommendation(recommendation)
        if not payload and not snapshot_auditor:
            errors.append(f"strategy_recommendation_missing_independent_auditor_verdict:{label}")
            continue
        if payload and _lower(payload.get("producer")) not in {"", "auditor"} and not independent_auditor:
            errors.append(
                "strategy_recommendation_audit_payload_not_from_independent_auditor:"
                f"{label}:producer={payload.get('producer')}"
            )
        if payload and _lower(payload.get("producer")) != "auditor" and not independent_auditor and not snapshot_auditor:
            errors.append(f"strategy_recommendation_audit_payload_missing_independent_auditor_summary:{label}")
        if not verdict:
            errors.append(f"strategy_recommendation_empty_independent_auditor_verdict:{label}")

    for tx in transactions:
        if _int(tx.get("lots")) <= 0:
            continue
        recommendation = recommendations.get(str(tx.get("recommendation_id") or ""))
        if _transaction_source_type(tx, recommendation) != STRATEGY_SOURCE_TYPE:
            continue
        verdict = _auditor_verdict_from_recommendation(recommendation or {})
        tx_label = f"{tx.get('trading_date')}:{tx.get('ticker')}:{tx.get('id')}"
        if verdict not in approved:
            errors.append(
                "strategy_transaction_without_approved_independent_auditor_verdict:"
                f"{tx_label}:recommendation_id={tx.get('recommendation_id')}:verdict={verdict or 'missing'}"
            )


def _audit_opportunity_ranking_boundary(
    recommendations: Dict[str, Dict[str, Any]],
    errors: List[str],
    transactions: Optional[List[Dict[str, Any]]] = None,
) -> None:
    ranking_fields = {
        "opportunity_score",
        "opportunity_score_components",
        "opportunity_rank",
        "capital_allocation_reason",
        "learning_adjustment_summary",
    }
    allowed_contract_containers = {"evidence_used", "learning_used"}
    for recommendation_id, recommendation in recommendations.items():
        if _source_type(recommendation) != STRATEGY_SOURCE_TYPE:
            continue
        snapshot = _dict(recommendation.get("signal_snapshot"))
        contract = _contract_from_recommendation(recommendation)
        ticker = recommendation.get("underlying_code") or recommendation.get("ticker") or ""
        label = f"{recommendation.get('trading_date')}:{ticker}:{recommendation_id}"
        contract_top_level = sorted(field for field in ranking_fields if field in contract)
        if contract_top_level:
            errors.append(f"opportunity_ranking_field_top_level_trade_authority:{label}:{contract_top_level}")
        for container_name in allowed_contract_containers:
            container = _dict(contract.get(container_name))
            if not container:
                continue
            for field in ranking_fields.intersection(container.keys()):
                if field in {"opportunity_score", "opportunity_rank"}:
                    continue
                if field in {"opportunity_score_components", "capital_allocation_reason", "learning_adjustment_summary"}:
                    continue
        execution_artifacts = [
            ("execution_result", _dict(snapshot.get("execution_result"))),
            ("phase2_execution", _dict(snapshot.get("phase2_execution"))),
            ("execution_translation", _dict(snapshot.get("execution_translation"))),
        ]
        for artifact_name, artifact in execution_artifacts:
            for path, node in _iter_nested_dicts(artifact):
                dangerous = sorted(
                    field for field in ranking_fields
                    if field in node and any(key in node for key in ("target_lots", "lots", "lots_delta", "action", "final_action"))
                )
                if dangerous:
                    errors.append(
                        "opportunity_ranking_field_used_in_execution_trade_intent:"
                        f"{label}:{artifact_name}:{path}:{dangerous}"
                    )
        for artifact_name, artifact in [
            ("signal_snapshot", snapshot),
            ("final_action_contract", contract),
        ]:
            for path, node in _iter_nested_dicts(artifact):
                components = OPPORTUNITY_SCORE_COMPONENT_FIELDS.intersection(node.keys())
                if not components:
                    continue
                inside_score_components = (
                    path.endswith("opportunity_score_components")
                    or ".opportunity_score_components." in f"{path}."
                )
                if inside_score_components:
                    continue
                if any(key in node for key in ("target_lots", "lots", "lots_delta", "action", "final_action")):
                    errors.append(
                        "opportunity_learning_component_used_as_trade_intent:"
                        f"{label}:{artifact_name}:{path}:{sorted(components)}"
                    )

        execution_trace = _dict(_dict(snapshot.get("execution_result")).get("execution_learning_trace"))
        if execution_trace:
            scope = _lower(execution_trace.get("consumer_scope"))
            if not scope:
                errors.append(f"trader_execution_learning_trace_missing_scope:{label}:execution_result")
            elif scope != "trader_execution_learning":
                errors.append(f"trader_execution_learning_trace_wrong_scope:{label}:execution_result:{scope}")
        setup_execution = _dict(_dict(snapshot.get("phase2_execution")).get("setup_execution_learning"))
        if setup_execution:
            scope = _lower(setup_execution.get("consumer_scope"))
            if not scope:
                errors.append(f"trader_execution_learning_trace_missing_scope:{label}:phase2_execution")
            elif scope != "trader_execution_learning":
                errors.append(f"trader_execution_learning_trace_wrong_scope:{label}:phase2_execution:{scope}")

    for tx in transactions or []:
        payload = _dict(tx.get("audit_payload"))
        if not payload:
            continue
        label = f"{tx.get('trading_date')}:{tx.get('ticker')}:{tx.get('id')}"
        for path, node in _iter_nested_dicts(payload):
            dangerous = sorted(
                field for field in ranking_fields
                if field in node and any(key in node for key in ("target_lots", "lots", "lots_delta", "action", "final_action"))
            )
            if dangerous:
                errors.append(
                    "opportunity_ranking_field_used_in_execution_trade_intent:"
                    f"{label}:transaction_audit_payload:{path}:{dangerous}"
                )


def _contract_learning_components(contract: Dict[str, Any]) -> Dict[str, float]:
    evidence = _dict(contract.get("evidence_used"))
    components = _dict(evidence.get("opportunity_score_components"))
    return {
        field: float(components.get(field) or 0.0)
        for field in OPPORTUNITY_SCORE_COMPONENT_FIELDS
    }


def _contract_action_value_rows(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    learning = _dict(contract.get("learning_used"))
    rows = learning.get("alpha_setup_action_values")
    if not isinstance(rows, list):
        return []
    return [_dict(row) for row in rows if isinstance(row, dict)]


def _action_value_row_has_pm_canonical_fields(row: Dict[str, Any]) -> bool:
    return bool(
        _payload_or_row_value(row, "action_preference")
        and _payload_or_row_value(row, "reward_source")
        and _payload_or_row_value(row, "evidence_scope", "amplification_scope_quality")
        and _payload_or_row_value(row, "action_value_lane", "source_action_value_lane", "action_name")
        and (
            _payload_or_row_value(row, "reward_sum") is not None
            or _payload_or_row_value(row, "reward_mean") is not None
            or _payload_or_row_value(row, "win_rate") is not None
        )
    )


def _action_value_consumer_scope(row: Dict[str, Any]) -> str:
    return _lower(_payload_or_row_value(row, "consumer_scope", "learning_consumer_scope") or "pm_learning")


def _payload_or_row_value(row: Dict[str, Any], key: str, *aliases: str) -> Any:
    payload = _dict(row.get("payload"))
    for name in (key, *aliases):
        value = row.get(name)
        if value not in (None, ""):
            return value
    for name in (key, *aliases):
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return None


def _action_value_lane(row: Dict[str, Any]) -> str:
    return _lower(_payload_or_row_value(row, "learning_lane", "action_value_lane", "action_name"))


def _action_value_memory_side_role(row: Dict[str, Any]) -> str:
    return _lower(_payload_or_row_value(row, "memory_side_role"))


def _action_value_matches_requirement(
    row: Dict[str, Any],
    *,
    ticker: str,
    side: str,
    decision_date: str,
    lane: str,
    memory_side_role: str,
) -> bool:
    target_ticker = _lower(ticker).upper()
    target_side = _lower(side)
    decision_day = _date10(decision_date)
    if not target_ticker or target_side not in {"long", "short"} or not decision_day:
        return False
    row_ticker = str(row.get("ticker") or "").upper()
    row_side = _lower(row.get("side"))
    if row_ticker not in {target_ticker, "*"}:
        return False
    if row_side not in {target_side, "*", "both", "any"}:
        return False
    sample_day = _date10(row.get("last_sample_date"))
    if not sample_day or sample_day >= decision_day:
        return False
    preference = _lower(_payload_or_row_value(row, "action_preference"))
    if preference not in ACTION_PREFERENCE_VALUES:
        return False
    if _action_value_consumer_scope(row) != "pm_learning":
        return False
    payload = _dict(row.get("payload"))
    reward_source = _lower(row.get("reward_source")) or _effective_reward_source(payload)
    if not any(marker in reward_source for marker in REAL_REWARD_SOURCE_MARKERS):
        return False
    if not lane_matches_memory_requirement(lane, _action_value_lane(row)):
        return False
    required_role = _lower(memory_side_role)
    row_role = _action_value_memory_side_role(row)
    if required_role and row_role and row_role != required_role:
        return False
    return True


def _real_action_values_for_memory_requirements(
    action_values: List[Dict[str, Any]],
    *,
    contract: Dict[str, Any],
    ticker: str,
    side: str,
    decision_date: str,
) -> List[Dict[str, Any]]:
    requirements = derive_memory_requirements(contract)
    required_memory = [
        item for item in (requirements.get("must_land_in_pm_contract") or [])
        if isinstance(item, dict) and item.get("must_land_in_pm_contract")
    ]
    matched: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for requirement in required_memory:
        req_side = _lower(requirement.get("side")) or _lower(side)
        req_lane = _lower(requirement.get("lane") or requirement.get("learning_lane"))
        req_role = _lower(requirement.get("memory_side_role"))
        if req_side not in {"long", "short"} or not req_lane:
            continue
        for row in action_values:
            if not _action_value_matches_requirement(
                row,
                ticker=ticker,
                side=req_side,
                decision_date=decision_date,
                lane=req_lane,
                memory_side_role=req_role,
            ):
                continue
            row_id = str(
                row.get("id")
                or _payload_or_row_value(row, "id")
                or "|".join([
                    str(row.get("scope_key") or ""),
                    str(row.get("ticker") or ""),
                    str(row.get("side") or ""),
                    _action_value_lane(row),
                    _lower(_payload_or_row_value(row, "action_preference")),
                ])
            )
            if row_id in seen:
                continue
            seen.add(row_id)
            matched.append(row)
    return matched


def _real_action_value_available_before(
    action_values: List[Dict[str, Any]],
    *,
    ticker: str,
    side: str,
    decision_date: str,
) -> bool:
    target_ticker = _lower(ticker).upper()
    target_side = _lower(side)
    decision_day = _date10(decision_date)
    if not target_ticker or target_side not in {"long", "short"} or not decision_day:
        return False
    for row in action_values:
        row_ticker = str(row.get("ticker") or "").upper()
        row_side = _lower(row.get("side"))
        if row_ticker not in {target_ticker, "*"}:
            continue
        if row_side not in {target_side, "*", "both", "any"}:
            continue
        sample_day = _date10(row.get("last_sample_date"))
        if not sample_day or sample_day >= decision_day:
            continue
        preference = _lower(row.get("action_preference") or _payload_or_row_value(row, "action_preference"))
        if preference not in ACTION_PREFERENCE_VALUES:
            continue
        payload = _dict(row.get("payload"))
        reward_source = _lower(row.get("reward_source")) or _effective_reward_source(payload)
        if any(marker in reward_source for marker in REAL_REWARD_SOURCE_MARKERS):
            return True
    return False


def _contract_increases_risk(contract: Dict[str, Any]) -> bool:
    return contract_increases_risk_position(contract)


def _prior_real_action_value_is_hold_exit(
    action_values: List[Dict[str, Any]],
    *,
    ticker: str,
    side: str,
    decision_date: str,
) -> bool:
    target_ticker = _lower(ticker).upper()
    target_side = _lower(side)
    decision_day = _date10(decision_date)
    if not target_ticker or target_side not in {"long", "short"} or not decision_day:
        return False
    for row in action_values:
        row_ticker = str(row.get("ticker") or "").upper()
        row_side = _lower(row.get("side"))
        if row_ticker not in {target_ticker, "*"}:
            continue
        if row_side not in {target_side, "*", "both", "any"}:
            continue
        sample_day = _date10(row.get("last_sample_date"))
        if not sample_day or sample_day >= decision_day:
            continue
        preference = _lower(row.get("action_preference") or _payload_or_row_value(row, "action_preference"))
        lane = _lower(row.get("action_value_lane") or _payload_or_row_value(row, "action_value_lane", "action_name"))
        if preference in {"tail_loss_protect", "negative_hold_revalidate", "positive_candidate_exit"}:
            return True
        if lane in {"hold", "exit"} and preference in ACTION_PREFERENCE_VALUES:
            return True
    return False


def _prior_rows_include_hold_exit_learning(rows: Iterable[Dict[str, Any]]) -> bool:
    for row in rows:
        preference = _lower(_payload_or_row_value(row, "action_preference"))
        lane = _action_value_lane(row)
        if preference in {"tail_loss_protect", "negative_hold_revalidate", "positive_candidate_exit"}:
            return True
        if lane in {"hold", "exit"} and preference in ACTION_PREFERENCE_VALUES:
            return True
    return False


def _audit_pm_learning_transport_and_contract_effect(
    recommendations: Dict[str, Dict[str, Any]],
    action_values: List[Dict[str, Any]],
    errors: List[str],
    warnings: List[str],
) -> None:
    for recommendation_id, recommendation in recommendations.items():
        if _source_type(recommendation) != STRATEGY_SOURCE_TYPE:
            continue
        contract = _contract_from_recommendation(recommendation)
        if not contract:
            continue
        ticker = recommendation.get("underlying_code") or recommendation.get("ticker") or contract.get("ticker") or ""
        label = f"{recommendation.get('trading_date')}:{ticker}:{recommendation_id}"
        rows = _contract_action_value_rows(contract)
        for row in rows:
            preference = _lower(row.get("action_preference"))
            consumer_scope = _action_value_consumer_scope(row)
            if consumer_scope != "pm_learning":
                errors.append(
                    "pm_consumed_non_pm_learning_action_value:"
                    f"{label}:{consumer_scope or 'missing'}"
                )
            if preference in ACTION_PREFERENCE_VALUES and not _action_value_row_has_pm_canonical_fields(row):
                errors.append(
                    "pm_action_value_missing_canonical_fields:"
                    f"{label}:{preference}:missing_preference_reward_scope_or_source"
                )
        target_lots = _int(contract.get("target_lots"))
        current_lots = _int(contract.get("current_lots"))
        side = "long" if target_lots > 0 else "short" if target_lots < 0 else ""
        if not side and current_lots:
            side = "long" if current_lots > 0 else "short"
        components = _contract_learning_components(contract)
        learning_all_zero = all(abs(value) <= 1e-12 for value in components.values())
        decision_date = str(recommendation.get("trading_date") or recommendation.get("effective_trade_date") or "")
        required_prior_rows = _real_action_values_for_memory_requirements(
            action_values,
            contract=contract,
            ticker=str(ticker),
            side=side,
            decision_date=decision_date,
        )
        prior_real_action_value_available = bool(required_prior_rows)
        prior_hold_exit_learning = _prior_rows_include_hold_exit_learning(required_prior_rows)
        hold_exit_error = (
            "pm_hold_exit_learning_without_contract_effect_or_explanation:"
            f"{label}:target_lots={target_lots}:current_lots={current_lots}"
        )
        if (
            prior_real_action_value_available
            and learning_all_zero
            and (_contract_increases_risk(contract) or not prior_hold_exit_learning)
        ):
            errors.append(
                "pm_learning_components_zero_despite_prior_real_action_value:"
                f"{label}:side={side or 'missing'}"
            )
        if (
            prior_real_action_value_available
            and prior_hold_exit_learning
            and learning_all_zero
            and _hold_exit_learning_has_no_contract_effect(contract)
            and not _hold_exit_learning_has_contract_explanation(contract)
            and hold_exit_error not in errors
        ):
            errors.append(hold_exit_error)
        alpha_profile_adjustment = float(
            _dict(_dict(contract.get("evidence_used")).get("opportunity_score_components")).get("alpha_profile_adjustment")
            or 0.0
        )
        if alpha_profile_adjustment > 0.015 and learning_all_zero:
            errors.append(
                "profile_adjustment_substituted_for_missing_action_value_learning:"
                f"{label}:alpha_profile_adjustment={alpha_profile_adjustment}"
            )
        learning_summary = _dict(_dict(contract.get("learning_used")).get("learning_adjustment_summary"))
        learning_signal_present = any(
            abs(float(learning_summary.get(key) or 0.0)) > 1e-12
            for key in (
                "positive_learning_signal",
                "negative_learning_signal",
                "execution_profile_learning_signal",
                "recent_tail_loss_signal",
            )
        )
        if learning_signal_present and int(contract.get("target_lots") or 0) == int(contract.get("current_lots") or 0):
            if not _learning_no_change_has_contract_explanation(contract):
                errors.append(
                    "pm_learning_signal_without_contract_effect_or_explanation:"
                    f"{label}:final_action={_lower(contract.get('final_action')) or 'missing'}"
                )
        rank_value = _rank_value_from_contract(contract)
        rank_contract_errors = rank_capital_layer_contract_errors(contract)
        if rank_contract_errors:
            errors.append(
                "pm_rank_capital_layer_contract_incomplete:"
                f"{label}:missing={','.join(rank_contract_errors)}"
            )
        rank_gate_errors = full_market_rank_gate_errors(contract)
        if rank_gate_errors:
            errors.append(
                "pm_new_risk_missing_full_market_rank:"
                f"{label}:missing={','.join(rank_gate_errors)}"
            )
        if rank_value is not None and _rank_has_no_contract_effect(contract):
            if not _learning_no_change_has_contract_explanation(contract):
                errors.append(
                    "pm_rank_changed_without_contract_effect:"
                    f"{label}:rank={rank_value}:final_action={_lower(contract.get('final_action')) or 'missing'}"
                )
        if _protective_hold_exit_learning_present(contract) and _hold_exit_learning_has_no_contract_effect(contract):
            if not _hold_exit_learning_has_contract_explanation(contract) and hold_exit_error not in errors:
                errors.append(hold_exit_error)


def _rank_value_from_contract(contract: Dict[str, Any]) -> Optional[int]:
    evidence = _dict(contract.get("evidence_used"))
    deployment = _dict(contract.get("capital_deployment"))
    raw = (
        deployment.get("opportunity_rank")
        if deployment.get("opportunity_rank") not in (None, "")
        else evidence.get("opportunity_rank")
    )
    try:
        return int(raw)
    except Exception:
        return None


def _audit_daily_full_market_rank_uniqueness(
    recommendations: Dict[str, Dict[str, Any]],
    errors: List[str],
) -> None:
    ranks_by_day: Dict[str, List[tuple[int, str]]] = {}
    for recommendation_id, recommendation in recommendations.items():
        if _source_type(recommendation) != STRATEGY_SOURCE_TYPE:
            continue
        contract = _contract_from_recommendation(recommendation)
        rank = _rank_value_from_contract(contract)
        if rank is None:
            continue
        day = str(recommendation.get("trading_date") or recommendation.get("effective_trade_date") or "")[:10]
        ticker = recommendation.get("underlying_code") or recommendation.get("ticker") or contract.get("ticker") or ""
        label = f"{day}:{ticker}:{recommendation_id}"
        ranks_by_day.setdefault(day, []).append((rank, label))
    for day, rows in sorted(ranks_by_day.items()):
        seen: Dict[int, List[str]] = {}
        for rank, label in rows:
            seen.setdefault(rank, []).append(label)
        for rank, labels in sorted(seen.items()):
            if len(labels) > 1:
                errors.append(
                    "pm_full_market_rank_duplicate:"
                    f"{day}:rank={rank}:recommendations={','.join(labels)}"
                )
        rank_one = seen.get(1, [])
        if len(rank_one) > 1:
            errors.append(
                "pm_full_market_rank_one_not_unique:"
                f"{day}:recommendations={','.join(rank_one)}"
            )


def _rank_has_no_contract_effect(contract: Dict[str, Any]) -> bool:
    deployment = _dict(contract.get("capital_deployment"))
    if not deployment:
        return True
    original = _int(deployment.get("original_target_lots"))
    deployed = _int(deployment.get("deployed_target_lots"))
    target = _int(contract.get("target_lots"))
    current = _int(contract.get("current_lots"))
    selected = bool(deployment.get("selected_for_capital_deployment"))
    if original != deployed:
        return False
    if target != current:
        return False
    if selected and target:
        return False
    return True


def _protective_hold_exit_learning_present(contract: Dict[str, Any]) -> bool:
    return contract_consumes_hold_exit_pm_learning(contract)


def _hold_exit_learning_has_no_contract_effect(contract: Dict[str, Any]) -> bool:
    current_lots = _int(contract.get("current_lots"))
    if not current_lots:
        return False
    return not contract_reduces_or_exits_position(contract)


def _hold_exit_learning_has_contract_explanation(contract: Dict[str, Any]) -> bool:
    return has_valid_hold_exit_no_change_explanation(contract)


def _learning_no_change_has_contract_explanation(contract: Dict[str, Any]) -> bool:
    return has_valid_generic_no_change_explanation(contract)


def _find_forbidden_diagnostic_fields(value: Any, *, prefix: str = "") -> List[str]:
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key) in RELEASE_BLOCK_DIAGNOSTIC_FORBIDDEN_FIELDS:
                found.append(path)
            found.extend(_find_forbidden_diagnostic_fields(item, prefix=path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_find_forbidden_diagnostic_fields(item, prefix=f"{prefix}[{index}]"))
    return found


def _audit_release_block_diagnostics(
    recommendations: Dict[str, Dict[str, Any]],
    errors: List[str],
) -> None:
    for recommendation_id, recommendation in recommendations.items():
        snapshot = _dict(recommendation.get("signal_snapshot"))
        diagnostics = _dict(snapshot.get("release_block_diagnostics"))
        if not diagnostics:
            continue
        ticker = recommendation.get("underlying_code") or recommendation.get("ticker") or ""
        label = f"{recommendation.get('trading_date')}:{ticker}:{recommendation_id}"
        if diagnostics.get("observation_only") is not True:
            errors.append(f"release_block_diagnostics_not_observation_only:{label}")
        if diagnostics.get("does_not_modify_trade_authority") is not True:
            errors.append(f"release_block_diagnostics_can_modify_trade_authority:{label}")
        if diagnostics.get("single_source_of_trade_truth_remains") != "final_action_contract":
            errors.append(f"release_block_diagnostics_contract_source_drift:{label}")
        forbidden = _find_forbidden_diagnostic_fields(diagnostics)
        if forbidden:
            errors.append(
                "release_block_diagnostics_contains_trade_action_fields:"
                f"{label}:{sorted(set(forbidden))}"
            )


def _audit_unified_field_artifacts(
    recommendations: Dict[str, Dict[str, Any]],
    errors: List[str],
) -> None:
    for recommendation_id, recommendation in recommendations.items():
        ticker = recommendation.get("underlying_code") or recommendation.get("ticker") or ""
        label = f"{recommendation.get('trading_date')}:{ticker}:{recommendation_id}"
        for artifact_name in ("signal_snapshot", "audit_payload"):
            artifact = recommendation.get(artifact_name)
            forbidden = find_forbidden_artifact_field_keys(artifact)
            if forbidden:
                errors.append(
                    "unified_field_artifact_forbidden_field:"
                    f"{label}:{artifact_name}:{sorted(set(forbidden))}"
                )


def _node_field_hits(value: Any, fields: set[str]) -> List[str]:
    hits: List[str] = []
    for path, node in _iter_nested_dicts(value):
        for field in fields:
            if field in node:
                hits.append(f"{path}.{field}" if path else field)
    return sorted(set(hits))


def _audit_artifact_phase_boundaries(
    recommendations: Dict[str, Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    daily_settlements: List[Dict[str, Any]],
    action_values: List[Dict[str, Any]],
    adaptive_policy_states: List[Dict[str, Any]],
    researcher_llm_notes: List[Dict[str, Any]],
    errors: List[str],
) -> None:
    """Check persisted artifact stage boundaries from mechanism_multiagents.md."""
    pm_forbidden_downstream_fields = {
        "daily_settlement",
        "daily_settlement_pnl",
        "execution_result",
        "execution_learning_trace",
        "phase4_validation",
        "settlement_result",
    }
    auditor_mutation_fields = {
        "modified_final_action_contract",
        "new_final_action_contract",
        "rewritten_final_action_contract",
        "strategy_memory",
        "adaptive_policy_state",
    }
    transaction_forbidden_fields = PM_EXPLANATION_FIELDS | {"final_action_contract"}
    accountant_forbidden_learning_fields = (
        PM_EXPLANATION_FIELDS
        | {
            "action_value",
            "adaptive_policy_state",
            "alpha_setup_action_value",
            "learning_used",
            "llm_notes",
            "raw_prompt",
            "raw_response",
            "researcher_llm_notes",
        }
    )
    accountant_forbidden_trade_mutation_fields = {
        "final_action_contract",
        "new_final_action_contract",
        "rewritten_final_action_contract",
        "target_lots",
        "lots_delta",
        "final_action",
    }
    reviewer_forbidden_action_value_fields = {
        "action_value",
        "alpha_setup_action_value",
        "final_action_value",
        "write_action_value",
    }
    reviewer_forbidden_research_state_fields = {
        "adaptive_policy_state",
        "capital_deployment_state",
        "research_state_write",
        "strategy_memory",
    }
    researcher_forbidden_trade_fact_mutation_fields = {
        "accountant_adjustment",
        "adjust_daily_settlement",
        "adjust_pnl",
        "amended_execution_result",
        "modified_final_action_contract",
        "new_final_action_contract",
        "rewritten_final_action_contract",
        "settlement_override",
        "trade_fact_mutation",
    }

    for recommendation_id, recommendation in recommendations.items():
        ticker = recommendation.get("underlying_code") or recommendation.get("ticker") or ""
        label = f"{recommendation.get('trading_date')}:{ticker}:{recommendation_id}"
        for artifact_name in ("signal_snapshot", "audit_payload"):
            artifact = _dict(recommendation.get(artifact_name))
            if not artifact:
                continue
            pm_hits = _node_field_hits(_dict(artifact.get("final_action_contract")), pm_forbidden_downstream_fields)
            if pm_hits:
                errors.append(f"pm_artifact_forbidden_downstream_field:{label}:{artifact_name}:{pm_hits}")
            pm_draft_hits = _node_field_hits(artifact, PM_INTERNAL_DRAFT_FIELDS)
            if pm_draft_hits:
                errors.append(f"pm_artifact_forbidden_internal_draft_field:{label}:{artifact_name}:{pm_draft_hits}")
            audit_hits = _node_field_hits(_dict(artifact.get("audit_verdict") or artifact.get("audit_payload") or artifact.get("audit")), auditor_mutation_fields)
            if audit_hits:
                errors.append(f"auditor_artifact_forbidden_contract_mutation:{label}:{artifact_name}:{audit_hits}")
            phase2_sections = {
                "phase2_execution": _dict(artifact.get("phase2_execution")),
                "execution_result": _dict(artifact.get("execution_result")),
                "execution_translation": _dict(artifact.get("execution_translation")),
            }
            for section_name, section in phase2_sections.items():
                hits = _node_field_hits(section, PM_EXPLANATION_FIELDS | {"final_action_contract"})
                if hits:
                    errors.append(f"trader_artifact_forbidden_pm_explanation:{label}:{artifact_name}.{section_name}:{hits}")
            phase4_section = _dict(artifact.get("phase4_review") or artifact.get("phase4_validation") or artifact.get("reviewer_artifact"))
            action_hits = _node_field_hits(phase4_section, reviewer_forbidden_action_value_fields)
            if action_hits:
                errors.append(f"reviewer_artifact_forbidden_action_value_write:{label}:{artifact_name}:phase4:{action_hits}")
            state_hits = _node_field_hits(phase4_section, reviewer_forbidden_research_state_fields)
            if state_hits:
                errors.append(f"reviewer_artifact_forbidden_research_state_write:{label}:{artifact_name}:phase4:{state_hits}")

    for tx in transactions:
        label = f"{tx.get('trading_date')}:{tx.get('ticker')}:{tx.get('id')}"
        payload = _dict(tx.get("audit_payload"))
        hits = _node_field_hits(payload, transaction_forbidden_fields)
        if hits:
            errors.append(f"transaction_audit_payload_forbidden_pm_contract_mirror:{label}:{hits}")

    for row in daily_settlements:
        label = f"{row.get('trading_date')}:{row.get('portfolio_id') or row.get('id')}"
        payload = {
            key: value
            for key, value in row.items()
            if isinstance(value, (dict, list))
        }
        learning_hits = _node_field_hits(payload, accountant_forbidden_learning_fields)
        if learning_hits:
            errors.append(f"accountant_artifact_forbidden_learning_field:{label}:{learning_hits}")
        mutation_hits = _node_field_hits(payload, accountant_forbidden_trade_mutation_fields)
        if mutation_hits:
            errors.append(f"accountant_artifact_forbidden_trade_action_mutation:{label}:{mutation_hits}")

    for row in action_values:
        label = f"{row.get('last_sample_date')}:{row.get('ticker')}:{row.get('id')}"
        payload = _dict(row.get("payload"))
        hits = _node_field_hits(payload, researcher_forbidden_trade_fact_mutation_fields)
        if hits:
            errors.append(f"researcher_artifact_forbidden_trade_fact_mutation:{label}:alpha_setup_action_value:{hits}")

    for row in adaptive_policy_states:
        label = f"{row.get('trading_date')}:{row.get('ticker')}:{row.get('id')}"
        payload = _dict(row.get("payload"))
        hits = _node_field_hits(payload, researcher_forbidden_trade_fact_mutation_fields)
        if hits:
            errors.append(f"researcher_artifact_forbidden_trade_fact_mutation:{label}:adaptive_policy_state:{hits}")

    for row in researcher_llm_notes:
        label = f"{row.get('trading_date')}:{row.get('ticker') or '*'}:{row.get('id')}"
        payload = _dict(row.get("payload"))
        hits = _node_field_hits(payload, researcher_forbidden_trade_fact_mutation_fields)
        if hits:
            errors.append(f"researcher_artifact_forbidden_trade_fact_mutation:{label}:researcher_llm_notes:{hits}")


def _audit_action_evidence_trigger_consistency(
    recommendations: Dict[str, Dict[str, Any]],
    errors: List[str],
) -> None:
    for recommendation_id, recommendation in recommendations.items():
        ticker = recommendation.get("underlying_code") or recommendation.get("ticker") or ""
        label = f"{recommendation.get('trading_date')}:{ticker}:{recommendation_id}"
        for artifact_name in ("signal_snapshot", "audit_payload"):
            artifact = recommendation.get(artifact_name)
            if not isinstance(artifact, dict):
                continue
            seen_error_types: set[str] = set()
            for path, node in _iter_nested_dicts(artifact):
                if not ("trigger_valid" in node or "action_evidence_contract" in node):
                    continue
                entry_trigger = node.get("entry_trigger")
                trigger_valid = node.get("trigger_valid")
                if trigger_valid is True and _entry_trigger_has_pending_confirmation(entry_trigger):
                    error_type = "action_evidence_contract_pending_trigger_marked_valid"
                    if error_type not in seen_error_types:
                        errors.append(f"{error_type}:{label}:{artifact_name}:{path}")
                        seen_error_types.add(error_type)
                if (
                    trigger_valid is True
                    and not _node_has_current_confirmation(node)
                ):
                    error_type = "trigger_valid_without_current_trigger_confirmed"
                    if error_type not in seen_error_types:
                        errors.append(f"{error_type}:{label}:{artifact_name}:{path}")
                        seen_error_types.add(error_type)
                if (
                    trigger_valid is True
                    and node.get("setup_quality_ok") is True
                    and not _node_has_current_confirmation(node)
                ):
                    error_type = "setup_quality_ok_used_as_current_trigger"
                    if error_type not in seen_error_types:
                        errors.append(f"{error_type}:{label}:{artifact_name}:{path}")
                        seen_error_types.add(error_type)
                action_contract = _dict(node.get("action_evidence_contract"))
                if action_contract and "trigger_valid" in node and "trigger_valid" in action_contract:
                    if bool(node.get("trigger_valid")) != bool(action_contract.get("trigger_valid")):
                        error_type = "trade_research_action_evidence_trigger_valid_mismatch"
                        if error_type not in seen_error_types:
                            errors.append(f"{error_type}:{label}:{artifact_name}:{path}")
                            seen_error_types.add(error_type)


def _contract_has_conditional_trigger_authority(contract: Dict[str, Any]) -> bool:
    return is_conditional_monitor_contract(contract)


def _active_opportunity_has_explicit_rejection(
    active_audit: Dict[str, Any],
    contract: Dict[str, Any],
) -> bool:
    return has_active_opportunity_rejection(active_audit, contract)


def _audit_active_opportunity_routing(
    recommendations: Dict[str, Dict[str, Any]],
    errors: List[str],
) -> None:
    for recommendation_id, recommendation in recommendations.items():
        if _source_type(recommendation) != STRATEGY_SOURCE_TYPE:
            continue
        snapshot = _dict(recommendation.get("signal_snapshot"))
        active_audit = _dict(snapshot.get("active_opportunity_audit"))
        if not active_audit:
            continue
        opportunity = _dict(active_audit.get("opportunity"))
        contract = _contract_from_recommendation(recommendation)
        ticker = recommendation.get("underlying_code") or recommendation.get("ticker") or ""
        label = f"{recommendation.get('trading_date')}:{ticker}:{recommendation_id}"
        conditional_count = _int(opportunity.get("conditional_monitor_candidate_count"))
        high_quality_present = bool(opportunity.get("high_quality_present"))
        final_action = _lower(contract.get("final_action"))
        if conditional_count > 0:
            if _contract_has_conditional_trigger_authority(contract):
                continue
            if _active_opportunity_has_explicit_rejection(active_audit, contract):
                continue
            errors.append(f"conditional_monitor_candidate_silent_wait:{label}:count={conditional_count}")
            continue
        if high_quality_present and final_action in {"", "wait", "hold"}:
            if _active_opportunity_has_explicit_rejection(active_audit, contract):
                continue
            errors.append(f"high_quality_opportunity_silent_wait:{label}:final_action={final_action or 'missing'}")


def _contract_mentions_preference(contract: Dict[str, Any], preference_names: Iterable[str]) -> bool:
    haystack = json.dumps(contract, ensure_ascii=False, sort_keys=True, default=str).lower()
    return any(name and name.lower() in haystack for name in preference_names)


def _effective_reward_source(payload: Dict[str, Any]) -> str:
    reward_source = _lower(payload.get("reward_source") or payload.get("sample_source"))
    if reward_source:
        return reward_source
    if _int(payload.get("episode_trade_reward_count")) > 0:
        return "trade_episode"
    if (
        _int(payload.get("real_trade_reward_count")) > 0
        or _int(payload.get("exact_state_real_trade_sample_count")) > 0
    ):
        return "real_trade"
    if _int(payload.get("counterfactual_reward_count")) > 0 or bool(payload.get("counterfactual_prior_only")):
        return "counterfactual_prior"
    return ""


def _has_real_reward_facts(payload: Dict[str, Any], reward_source: str) -> bool:
    if reward_source and any(marker in reward_source for marker in REAL_REWARD_SOURCE_MARKERS):
        return True
    return (
        _int(payload.get("episode_trade_reward_count")) > 0
        or _int(payload.get("real_trade_reward_count")) > 0
        or _int(payload.get("exact_state_real_trade_sample_count")) > 0
    )


def _usage_boundary_terms(payload: Dict[str, Any], key: str) -> set[str]:
    boundary = _dict(payload.get("usage_boundary"))
    return {
        _lower(item)
        for item in _list(payload.get(key)) + _list(boundary.get(key))
        if _lower(item)
    }


def _action_value_usage_boundary_label(row: Dict[str, Any], action_name: str) -> str:
    return f"{row.get('ticker')}:{row.get('side')}:{row.get('setup_type')}:{action_name}:{row.get('last_sample_date')}"


def _audit_action_value_usage_boundary(
    row: Dict[str, Any],
    payload: Dict[str, Any],
    action_name: str,
    action_preference: str,
    errors: List[str],
) -> None:
    if not isinstance(payload, dict):
        return
    has_boundary = bool(payload.get("usage_boundary") or payload.get("usable_by") or payload.get("allowed_effects") or payload.get("forbidden_effects"))
    if not has_boundary:
        return
    label = _action_value_usage_boundary_label(row, action_name)
    allowed = _usage_boundary_terms(payload, "allowed_effects")
    forbidden = _usage_boundary_terms(payload, "forbidden_effects")
    usable_by = _usage_boundary_terms(payload, "usable_by")
    lane = _lower(payload.get("action_value_lane") or _nested_value(payload, "usage_boundary", "lane") or action_name)

    if action_preference == "positive_candidate_open" and action_name != "open":
        errors.append(f"action_value_open_preference_on_non_open_lane:{label}:{action_name}")
    if action_preference == "positive_candidate_exit" and action_name not in {"exit", "reduce", "reduce_or_exit", "close", "close_or_reduce", "flatten"}:
        errors.append(f"action_value_exit_preference_on_non_exit_lane:{label}:{action_name}")
    if action_preference == "positive_candidate_execution" and action_name != "execution":
        errors.append(f"action_value_execution_preference_on_non_execution_lane:{label}:{action_name}")

    if action_name in {"exit", "reduce", "reduce_or_exit", "close", "close_or_reduce", "flatten"} or lane == "exit":
        bad = sorted(allowed & OPEN_AMPLIFICATION_EFFECTS)
        for effect in bad:
            errors.append(f"action_value_usage_boundary_forbids_exit_as_open_amplifier:{label}:{effect}")
        if "open_amplification" not in forbidden:
            errors.append(f"action_value_usage_boundary_missing_exit_open_amplification_forbidden:{label}")
    if action_name == "execution" or lane == "execution":
        bad = sorted(allowed & EXECUTION_INTENT_MUTATION_EFFECTS)
        for effect in bad:
            errors.append(f"action_value_usage_boundary_forbids_execution_changing_trade_intent:{label}:{effect}")
        required_forbidden = {"change_direction", "change_lots", "change_target_lots"}
        missing_forbidden = sorted(required_forbidden - forbidden)
        if missing_forbidden:
            errors.append(f"action_value_usage_boundary_missing_execution_intent_forbidden:{label}:{missing_forbidden}")
        if "trader" not in usable_by:
            errors.append(f"action_value_usage_boundary_execution_not_usable_by_trader:{label}")


def _audit_open_transactions(
    transactions: List[Dict[str, Any]],
    recommendations: Dict[str, Dict[str, Any]],
    errors: List[str],
    warnings: List[str],
) -> None:
    for tx in transactions:
        if not _is_open_transaction(tx):
            continue
        recommendation = recommendations.get(str(tx.get("recommendation_id") or ""))
        source_type = _transaction_source_type(tx, recommendation)
        tx_label = f"{tx.get('trading_date')}:{tx.get('ticker')}:{tx.get('id')}"
        if source_type != STRATEGY_SOURCE_TYPE:
            if source_type == FORCED_RISK_SOURCE_TYPE:
                errors.append(f"forced_risk_open_transaction_not_allowed:{tx_label}:{_lower(tx.get('action'))}")
            continue
        contract = _transaction_contract(tx, recommendation)
        authority = _transaction_authority(tx, recommendation)
        audit = _transaction_trade_contract_audit(tx)
        final_action = _lower(contract.get("final_action"))
        authority_type = _lower(authority.get("authority_type") or contract.get("authority_type"))
        reason_codes = {_lower(item) for item in _list(authority.get("reason_codes")) + _list(contract.get("reason_codes"))}

        if final_action not in OPEN_FINAL_ACTIONS:
            errors.append(f"open_transaction_without_open_final_action:{tx_label}:{final_action or 'missing'}")
        if authority_type not in OPEN_AUTHORITY_TYPES:
            errors.append(f"open_transaction_without_open_authority:{tx_label}:{authority_type or 'missing'}")
        if authority_type == "real_budget_entry" and not bool(
            contract.get("open_action_evidence") and contract.get("strong_current_evidence")
        ):
            errors.append(f"real_open_without_current_contract_evidence:{tx_label}")
        if authority_type == "exploration_probe":
            if has_open_transaction_blocker(contract):
                errors.append(f"direction_or_watchlist_probe_opened:{tx_label}:{sorted(reason_codes)}")
        if audit and (
            audit.get("single_source_of_trade_truth") is False
            or audit.get("candidate_sources_do_not_bypass_contract") is False
        ):
            errors.append(f"trade_contract_source_of_truth_failed:{tx_label}")
        if not audit:
            warnings.append(f"transaction_missing_trade_contract_audit_mirror:{tx_label}")


def _audit_transaction_final_contract_consistency(
    transactions: List[Dict[str, Any]],
    recommendations: Dict[str, Dict[str, Any]],
    errors: List[str],
    warnings: List[str],
) -> None:
    for tx in transactions:
        if _int(tx.get("lots")) <= 0:
            continue
        recommendation = recommendations.get(str(tx.get("recommendation_id") or ""))
        if _transaction_source_type(tx, recommendation) != STRATEGY_SOURCE_TYPE:
            continue
        contract = _transaction_contract(tx, recommendation)
        tx_label = f"{tx.get('trading_date')}:{tx.get('ticker')}:{tx.get('id')}"
        if not contract:
            errors.append(f"transaction_missing_final_action_contract:{tx_label}")
            continue

        current_lots = _int(contract.get("current_lots"))
        target_lots = _int(contract.get("target_lots"), current_lots)
        lots_delta = _int(contract.get("lots_delta"), target_lots - current_lots)
        if lots_delta != target_lots - current_lots:
            errors.append(
                "final_action_contract_lots_delta_mismatch:"
                f"{tx_label}:current={current_lots}:target={target_lots}:delta={lots_delta}"
            )
            continue

        expected = phase2_order_intent_from_lots(current_lots=current_lots, target_lots=target_lots)
        actual_action = _lower(tx.get("action"))
        actual_lots = _int(tx.get("lots"))
        if expected["action"] != actual_action or int(expected["lots"] or 0) != actual_lots:
            errors.append(
                "transaction_not_derived_from_final_action_contract:"
                f"{tx_label}:expected={expected['action']}:{expected['lots']}:"
                f"actual={actual_action}:{actual_lots}:"
                f"current={current_lots}:target={target_lots}:delta={lots_delta}"
            )
        if _lower(contract.get("final_action")) in {"hold", "wait"} and actual_lots > 0:
            errors.append(
                "hold_or_wait_contract_generated_transaction:"
                f"{tx_label}:{actual_action}:{actual_lots}"
            )


def _audit_intraday_triggers(
    transactions: List[Dict[str, Any]],
    recommendations: Dict[str, Dict[str, Any]],
    intraday_decisions: List[Dict[str, Any]],
    errors: List[str],
) -> None:
    by_recommendation: Dict[str, List[Dict[str, Any]]] = {}
    for decision in intraday_decisions:
        by_recommendation.setdefault(str(decision.get("recommendation_id") or ""), []).append(decision)

    for tx in transactions:
        if not _is_open_transaction(tx):
            continue
        recommendation = recommendations.get(str(tx.get("recommendation_id") or ""))
        if _transaction_source_type(tx, recommendation) != STRATEGY_SOURCE_TYPE:
            continue
        payload = _dict(tx.get("audit_payload"))
        execution_requirement = _lower(
            _nested_value(payload, "trade_contract_audit", "execution_requirement")
            or _nested_value(recommendation or {}, "signal_snapshot", "final_action_contract", "execution_requirement")
            or _nested_value(recommendation or {}, "audit_payload", "final_action_contract", "execution_requirement")
        )
        if execution_requirement and execution_requirement != "intraday_trigger_required":
            continue

        recommendation_id = str(tx.get("recommendation_id") or "")
        decisions = by_recommendation.get(recommendation_id, [])
        triggered = any(
            _lower(row.get("decision")) == "execute"
            and (
                _lower(row.get("trigger_reason")) in TRIGGER_PASSED_REASONS
                or bool(_dict(row.get("features")).get("trigger_passed"))
            )
            for row in decisions
        )
        payload_triggered = bool(_nested_value(payload, "execution_translation", "intraday_execution", "trigger_passed"))
        if decisions and payload_triggered and not triggered:
            errors.append(
                "intraday_trigger_audit_mirror_mismatch:"
                f"{tx.get('trading_date')}:{tx.get('ticker')}:{tx.get('id')}"
            )
        if not triggered and not payload_triggered:
            errors.append(
                "open_transaction_without_intraday_trigger:"
                f"{tx.get('trading_date')}:{tx.get('ticker')}:{tx.get('id')}"
            )


def _audit_action_values(action_values: List[Dict[str, Any]], errors: List[str], warnings: List[str]) -> None:
    for row in action_values:
        payload = _dict(row.get("payload"))
        action_name = _lower(row.get("action_name"))
        reward_sum = float(row.get("reward_sum") or 0.0)
        sample_count = _int(row.get("sample_count"))
        row_action_preference = _lower(row.get("action_preference"))
        payload_action_preference = _lower(payload.get("action_preference"))
        if row_action_preference and payload_action_preference and row_action_preference != payload_action_preference:
            errors.append(
                "action_preference_column_payload_mismatch:"
                f"{row.get('ticker')}:{row.get('side')}:{row.get('setup_type')}:{action_name}:"
                f"{row.get('last_sample_date')}:{row_action_preference}!={payload_action_preference}"
            )
        action_preference = payload_action_preference
        scope_quality = _lower(payload.get("amplification_scope_quality") or payload.get("sample_scope"))
        reward_source = _effective_reward_source(payload)
        has_real_reward_facts = _has_real_reward_facts(payload, reward_source)
        weak_prior_context = bool(not has_real_reward_facts and (
            _lower(payload.get("prior_role")) == "weak_prior_not_action_preference"
            or reward_source in {"counterfactual_prior", "similar_sql_prior", "unqualified", ""}
        ))
        label = f"{row.get('ticker')}:{row.get('side')}:{row.get('setup_type')}:{action_name}:{row.get('last_sample_date')}"
        consumer_scope = _action_value_consumer_scope(row)
        if consumer_scope not in {"pm_learning", "research_diagnostics"}:
            errors.append(f"action_value_missing_consumer_scope:{label}:{consumer_scope or 'missing'}")

        if sample_count <= 0:
            continue
        if action_name in {"open", "hold", "exit", "execution"} and reward_sum != 0:
            if not action_preference and not weak_prior_context:
                errors.append(f"action_value_missing_action_preference:{label}:missing_action_preference")
            if (
                action_preference
                and action_preference not in ACTION_PREFERENCE_VALUES
            ):
                errors.append(f"action_value_unknown_action_preference:{label}:{action_preference}")
            if (
                action_name == "open"
                and reward_sum > 0
                and action_preference not in {"positive_candidate_open"}
                and has_real_reward_facts
            ):
                errors.append(f"positive_open_action_value_not_open_preference:{label}:{action_preference or 'missing_action_preference'}")
            if (
                action_name == "exit"
                and reward_sum > 0
                and action_preference not in {"positive_candidate_exit"}
                and has_real_reward_facts
            ):
                errors.append(f"positive_exit_action_value_not_exit_preference:{label}:{action_preference or 'missing_action_preference'}")
            if (
                reward_sum < 0
                and action_preference not in {"negative_revalidate", "negative_hold_revalidate", "tail_loss_protect"}
                and has_real_reward_facts
            ):
                errors.append(f"negative_action_value_not_protective_preference:{label}:{action_preference or 'missing_action_preference'}")
        if action_preference == "positive_candidate_open":
            if scope_quality != "exact_real_state":
                errors.append(f"positive_open_from_non_exact_scope:{label}:{scope_quality or 'missing_scope'}")
            if not reward_source or ("episode" not in reward_source and "real" not in reward_source):
                errors.append(f"positive_open_from_non_real_reward_source:{label}:{reward_source or 'missing_reward_source'}")
        _audit_action_value_usage_boundary(row, payload, action_name, action_preference, errors)


def _audit_adaptive_policy_states(policy_rows: List[Dict[str, Any]], errors: List[str], warnings: List[str]) -> None:
    for row in policy_rows:
        label = ":".join(
            str(row.get(key) or "*")
            for key in ("ticker", "side", "setup_type", "horizon_class", "market_regime", "policy_type")
        )
        decision = adaptive_policy_runtime_decision(row)
        action = str(decision.get("policy_action") or "")
        policy_type = str(decision.get("policy_type") or "")
        if action in {"protect", "allow"} and not bool(decision.get("allowed")):
            errors.append(
                "adaptive_policy_release_not_validated:"
                f"{label}:{decision.get('reason') or 'blocked'}"
            )
        if action not in {
            "cap",
            "reduce",
            "block",
            "demote",
            "probe_only",
            "weak_block",
            "protect",
            "allow",
            "probe",
            "watchlist",
            "calibrate",
        }:
            errors.append(f"adaptive_policy_unknown_action:{label}:{action or 'missing'}")
        if policy_type == "fast_candidate_alpha" and action not in {"probe", "watchlist"}:
            errors.append(f"adaptive_policy_fast_candidate_not_probe_only:{label}:{action or 'missing'}")


def _audit_recommendation_preference_landing(
    recommendations: Dict[str, Dict[str, Any]],
    action_values: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    errors: List[str],
    warnings: List[str],
) -> None:
    preference_sample_dates: Dict[str, List[str]] = {}
    for row in action_values:
        preference = _lower(_dict(row.get("payload")).get("action_preference"))
        if preference not in ACTION_PREFERENCE_VALUES:
            continue
        preference_sample_dates.setdefault(preference, []).append(_date10(row.get("last_sample_date")))
    preference_names = set(preference_sample_dates)
    if not preference_names:
        return
    unlanded_preferences: List[str] = []
    deferred_preferences: List[str] = []
    for preference, sample_dates in preference_sample_dates.items():
        terms = ACTION_PREFERENCE_LANDING_TERMS.get(preference, {preference})
        downstream_seen = False
        landed = False
        for recommendation in recommendations.values():
            recommendation_date = _date10(recommendation.get("effective_trade_date") or recommendation.get("trading_date"))
            if not any(sample_date and recommendation_date and recommendation_date > sample_date for sample_date in sample_dates):
                continue
            contract = _contract_from_recommendation(recommendation)
            downstream_seen = downstream_seen or bool(contract)
            if contract and _contract_mentions_preference(contract, terms):
                landed = True
                break
        if not landed:
            downstream_seen = downstream_seen or any(
                any(
                    sample_date
                    and _date10(transaction.get("trading_date"))
                    and _date10(transaction.get("trading_date")) > sample_date
                    for sample_date in sample_dates
                )
                for transaction in transactions
            )
        if landed:
            continue
        if downstream_seen:
            unlanded_preferences.append(preference)
        else:
            deferred_preferences.append(preference)

    if unlanded_preferences:
        known_dates = sorted({date for dates in preference_sample_dates.values() for date in dates if date})
        message = (
            "action_preferences_exist_but_no_final_action_contract_mentions_them:"
            f"preferences={sorted(unlanded_preferences)}:"
            f"sample_window={known_dates[0] if known_dates else 'missing'}..{known_dates[-1] if known_dates else 'missing'}"
        )
        errors.append(message)
    elif deferred_preferences:
        known_dates = sorted({date for dates in preference_sample_dates.values() for date in dates if date})
        warnings.append(
            "action_preferences_exist_but_no_downstream_final_action_contract_yet:"
            f"preferences={sorted(deferred_preferences)}:"
            f"sample_window={known_dates[0] if known_dates else 'missing'}..{known_dates[-1] if known_dates else 'missing'}"
        )


def _audit_trading_day_phase_completion(
    phases: List[Dict[str, Any]],
    recommendations: Dict[str, Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    intraday_decisions: List[Dict[str, Any]],
    action_values: List[Dict[str, Any]],
    errors: List[str],
) -> None:
    if not phases:
        return
    days_with_artifacts = {
        _date10(item.get("trading_date"))
        for item in recommendations.values()
        if _date10(item.get("trading_date"))
    }
    days_with_artifacts.update(
        _date10(item.get("trading_date"))
        for item in transactions
        if _date10(item.get("trading_date"))
    )
    days_with_artifacts.update(
        _date10(item.get("trading_date"))
        for item in intraday_decisions
        if _date10(item.get("trading_date"))
    )
    days_with_artifacts.update(
        _date10(item.get("last_sample_date"))
        for item in action_values
        if _date10(item.get("last_sample_date"))
    )
    if not days_with_artifacts:
        return
    by_day: Dict[str, Dict[str, str]] = {}
    for row in phases:
        day = _date10(row.get("trading_date"))
        phase = str(row.get("phase") or "")
        status = str(row.get("status") or "")
        if not day or not phase:
            continue
        by_day.setdefault(day, {})[phase] = status
    required = {"phase1", "phase2", "phase3", "phase4"}
    for day in sorted(days_with_artifacts):
        phase_status = by_day.get(day, {})
        if not phase_status:
            continue
        non_completed = {
            phase: phase_status.get(phase, "missing")
            for phase in sorted(required)
            if phase_status.get(phase) != "completed"
        }
        if non_completed:
            encoded = ",".join(f"{phase}={status}" for phase, status in non_completed.items())
            errors.append(f"incomplete_trading_day_phase:{day}:{encoded}")


def audit_system_invariants(
    *,
    db_path: str | Path,
    config_id: Optional[str] = None,
    exp_name: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> InvariantAuditReport:
    errors: List[str] = []
    warnings: List[str] = []
    metadata: Dict[str, Any] = {}
    db_path = Path(db_path)
    if not db_path.exists():
        return InvariantAuditReport(
            ok=True,
            warnings=[f"sqlite_missing:{db_path}"],
            counts={},
            metadata={
                "db_path": str(db_path),
                "audit_boundary": "no_trade_records_to_audit",
                "error_categories": {},
                "failed_categories": [],
                "unified_field_semantics_audit": _unified_field_semantics_audit_summary([]),
            },
        )

    conn = _connect(db_path)
    try:
        resolved_config_id = _fetch_config_id(conn, config_id=config_id, exp_name=exp_name)
        if not resolved_config_id:
            if not _has_invariant_records(conn):
                return InvariantAuditReport(
                    ok=True,
                    warnings=[f"config_not_found_empty_db:{exp_name or config_id or 'missing'}"],
                    counts={},
                    metadata={
                        "db_path": str(db_path),
                        "audit_boundary": "no_trade_records_to_audit",
                        "record_boundary": "empty_db_no_invariant_records_to_audit",
                        "error_categories": {},
                        "failed_categories": [],
                        "unified_field_semantics_audit": _unified_field_semantics_audit_summary([]),
                    },
                )
            return InvariantAuditReport(
                ok=False,
                errors=[f"config_not_found:{exp_name or config_id or 'missing'}"],
                metadata={
                    "db_path": str(db_path),
                    "error_categories": categorize_invariant_errors([f"config_not_found:{exp_name or config_id or 'missing'}"]),
                    "failed_categories": ["audit_explainability"],
                    "unified_field_semantics_audit": _unified_field_semantics_audit_summary([]),
                },
            )
        metadata["config_id"] = resolved_config_id
        metadata["db_path"] = str(db_path)
        schema_report = audit_db_schema_contract(db_path)
        if not schema_report.ok:
            schema_errors = list(schema_report.errors)
            categories = categorize_invariant_errors(schema_errors)
            return InvariantAuditReport(
                ok=False,
                errors=schema_errors,
                warnings=list(schema_report.warnings),
                counts={},
                metadata={
                    **metadata,
                    "schema_contract": dict(schema_report.metadata),
                    "error_categories": categories,
                    "failed_categories": sorted(categories),
                    "unified_field_semantics_audit": _unified_field_semantics_audit_summary([]),
                },
            )
        recommendations = _load_recommendations(conn, config_id=resolved_config_id, start_date=start_date, end_date=end_date)
        transactions = _load_transactions(conn, config_id=resolved_config_id, start_date=start_date, end_date=end_date)
        intraday_decisions = _load_intraday_decisions(conn, config_id=resolved_config_id, start_date=start_date, end_date=end_date)
        action_values = _load_action_values(conn, config_id=resolved_config_id, start_date=start_date, end_date=end_date)
        adaptive_policy_states = _load_adaptive_policy_states(conn, config_id=resolved_config_id, start_date=start_date, end_date=end_date)
        daily_settlements = _load_daily_settlements(conn, config_id=resolved_config_id, start_date=start_date, end_date=end_date)
        researcher_llm_notes = _load_researcher_llm_notes(conn, config_id=resolved_config_id, start_date=start_date, end_date=end_date)
        trading_day_phases = _load_trading_day_phases(conn, config_id=resolved_config_id, start_date=start_date, end_date=end_date)
    finally:
        conn.close()

    _audit_trading_day_phase_completion(
        trading_day_phases,
        recommendations,
        transactions,
        intraday_decisions,
        action_values,
        errors,
    )
    _audit_recommendation_final_contract_consistency(recommendations, errors)
    _audit_independent_auditor_chain(recommendations, transactions, errors)
    _audit_artifact_phase_boundaries(
        recommendations,
        transactions,
        daily_settlements,
        action_values,
        adaptive_policy_states,
        researcher_llm_notes,
        errors,
    )
    _audit_opportunity_ranking_boundary(recommendations, errors, transactions)
    _audit_pm_learning_transport_and_contract_effect(recommendations, action_values, errors, warnings)
    _audit_daily_full_market_rank_uniqueness(recommendations, errors)
    _audit_unified_field_artifacts(recommendations, errors)
    _audit_action_evidence_trigger_consistency(recommendations, errors)
    _audit_active_opportunity_routing(recommendations, errors)
    _audit_release_block_diagnostics(recommendations, errors)
    _audit_transaction_final_contract_consistency(transactions, recommendations, errors, warnings)
    _audit_open_transactions(transactions, recommendations, errors, warnings)
    _audit_intraday_triggers(transactions, recommendations, intraday_decisions, errors)
    _audit_action_values(action_values, errors, warnings)
    _audit_adaptive_policy_states(adaptive_policy_states, errors, warnings)
    _audit_recommendation_preference_landing(recommendations, action_values, transactions, errors, warnings)

    counts = {
        "recommendations": len(recommendations),
        "transactions": len(transactions),
        "open_transactions": sum(1 for item in transactions if _is_open_transaction(item)),
        "intraday_decisions": len(intraday_decisions),
        "action_values": len(action_values),
        "adaptive_policy_states": len(adaptive_policy_states),
        "daily_settlements": len(daily_settlements),
        "researcher_llm_notes": len(researcher_llm_notes),
        "trading_day_phases": len(trading_day_phases),
    }
    metadata["audit_boundary"] = (
        "system_invariants_only; no strategy profitability judgment; "
        "does_not_create_trade_authority_or_modify_lots"
    )
    metadata["pm_learning_ranking_audit_boundaries"] = list(PM_LEARNING_RANKING_AUDIT_BOUNDARIES)
    error_categories = categorize_invariant_errors(errors)
    metadata["error_categories"] = error_categories
    metadata["failed_categories"] = sorted(error_categories)
    metadata["unified_field_semantics_audit"] = _unified_field_semantics_audit_summary(errors)
    return InvariantAuditReport(ok=not errors, errors=errors, warnings=warnings, counts=counts, metadata=metadata)



