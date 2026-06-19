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
from tools.agent_tools.control.schemas import ProtocolCheckResult
from tools.agent_tools.control.unified_field_audit import find_forbidden_artifact_field_keys
from tools.agent_tools.execution.order_semantics import phase2_order_intent_from_lots


OPEN_ACTIONS = {"open_long", "open_short"}
OPEN_FINAL_ACTIONS = {"open_probe", "open_real"}
OPEN_AUTHORITY_TYPES = {"exploration_probe", "real_budget_entry"}
BLOCKING_AUTHORITY_TYPES = {"", "watchlist_only", "no_trade", "not_applicable", "analysis_or_watchlist_only"}
TRIGGER_PASSED_REASONS = {
    "intraday_trigger_confirmed",
    "intraday_pullback_confirmed",
    "intraday_vwap_confirmed",
    "intraday_immediate_execution",
    "intraday_event_immediate_execution",
    "intraday_confirmed_memory_vwap_fallback",
}
ACTION_PREFERENCE_VALUES = {
    "positive_candidate_open",
    "positive_candidate_hold",
    "positive_candidate_exit",
    "positive_candidate_execution",
    "negative_revalidate",
    "negative_hold_revalidate",
    "tail_loss_protect",
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
    payload = _dict(transaction.get("audit_payload"))
    contract = _dict(payload.get("final_action_contract"))
    if contract:
        return contract
    return _contract_from_recommendation(recommendation or {})


def _transaction_authority(transaction: Dict[str, Any], recommendation: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    contract = _transaction_contract(transaction, recommendation)
    return contract if contract else {}


def _transaction_trade_contract_audit(transaction: Dict[str, Any]) -> Dict[str, Any]:
    return _audit_from_payload(_dict(transaction.get("audit_payload")))


def _final_action_allowed_by_lots(current_lots: int, target_lots: int, final_action: str) -> bool:
    action = _lower(final_action)
    current = int(current_lots or 0)
    target = int(target_lots or 0)
    if current == target:
        return action == ("hold" if current else "wait")
    if current == 0 and target != 0:
        return action in OPEN_FINAL_ACTIONS
    if target == 0 and current != 0:
        return action == "exit"
    if (current > 0 and target > 0) or (current < 0 and target < 0):
        return action == ("scale" if abs(target) > abs(current) else "reduce")
    return action == "exit"


def _source_type(recommendation: Dict[str, Any]) -> str:
    return _lower(recommendation.get("source_type") or "strategy")


def _strategy_contract_type_is_valid(contract: Dict[str, Any]) -> bool:
    contract_type = _lower(contract.get("contract_type") or "strategy")
    return contract_type in {"", "strategy"}


def _audit_recommendation_final_contract_consistency(
    recommendations: Dict[str, Dict[str, Any]],
    errors: List[str],
) -> None:
    for recommendation_id, recommendation in recommendations.items():
        contract = _contract_from_recommendation(recommendation)
        if not contract:
            continue
        ticker = recommendation.get("underlying_code") or recommendation.get("ticker") or contract.get("ticker")
        label = f"{recommendation.get('trading_date')}:{ticker}:{recommendation_id}"
        if _source_type(recommendation) == "strategy" and not _strategy_contract_type_is_valid(contract):
            errors.append(
                "strategy_recommendation_non_strategy_final_action_contract:"
                f"{label}:contract_type={contract.get('contract_type')}"
            )
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
        if not _final_action_allowed_by_lots(current_lots, target_lots, str(contract.get("final_action") or "")):
            errors.append(
                "recommendation_final_action_contract_action_mismatch:"
                f"{label}:action={contract.get('final_action')}:"
                f"current={current_lots}:target={target_lots}:delta={lots_delta}"
            )


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
        contract = _transaction_contract(tx, recommendation)
        authority = _transaction_authority(tx, recommendation)
        audit = _transaction_trade_contract_audit(tx)
        final_action = _lower(contract.get("final_action"))
        authority_type = _lower(authority.get("authority_type") or contract.get("authority_type"))
        reason_codes = {_lower(item) for item in _list(authority.get("reason_codes")) + _list(contract.get("reason_codes"))}
        tx_label = f"{tx.get('trading_date')}:{tx.get('ticker')}:{tx.get('id')}"

        if final_action not in OPEN_FINAL_ACTIONS:
            errors.append(f"open_transaction_without_open_final_action:{tx_label}:{final_action or 'missing'}")
        if authority_type not in OPEN_AUTHORITY_TYPES:
            errors.append(f"open_transaction_without_open_authority:{tx_label}:{authority_type or 'missing'}")
        if authority_type == "real_budget_entry" and not bool(
            contract.get("open_action_evidence") and contract.get("strong_current_evidence")
        ):
            errors.append(f"real_open_without_current_contract_evidence:{tx_label}")
        if authority_type == "exploration_probe":
            blocking = {
                "pm_watch_for_trigger_probe_cap",
                "watch_for_trigger_cannot_open_position",
                "final_action_contract_watch_for_trigger_probe_block",
                "daily_tradeability_watchlist_only",
                "pm_text_watchlist_only_blocks_new_entry",
                "pm_text_no_trade_blocks_new_entry",
            }
            if reason_codes & blocking or bool(authority.get("watch_for_trigger_block")):
                errors.append(f"direction_or_watchlist_probe_opened:{tx_label}:{sorted(reason_codes & blocking)}")
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
    intraday_decisions: List[Dict[str, Any]],
    errors: List[str],
) -> None:
    by_recommendation: Dict[str, List[Dict[str, Any]]] = {}
    for decision in intraday_decisions:
        by_recommendation.setdefault(str(decision.get("recommendation_id") or ""), []).append(decision)

    for tx in transactions:
        if not _is_open_transaction(tx):
            continue
        payload = _dict(tx.get("audit_payload"))
        contract = _dict(payload.get("final_action_contract"))
        execution_requirement = _lower(
            contract.get("execution_requirement")
            or _nested_value(payload, "trade_contract_audit", "execution_requirement")
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
            metadata={"db_path": str(db_path), "audit_boundary": "no_trade_records_to_audit"},
        )

    conn = _connect(db_path)
    try:
        resolved_config_id = _fetch_config_id(conn, config_id=config_id, exp_name=exp_name)
        if not resolved_config_id:
            return InvariantAuditReport(
                ok=False,
                errors=[f"config_not_found:{exp_name or config_id or 'missing'}"],
                metadata={"db_path": str(db_path)},
            )
        metadata["config_id"] = resolved_config_id
        metadata["db_path"] = str(db_path)
        recommendations = _load_recommendations(conn, config_id=resolved_config_id, start_date=start_date, end_date=end_date)
        transactions = _load_transactions(conn, config_id=resolved_config_id, start_date=start_date, end_date=end_date)
        intraday_decisions = _load_intraday_decisions(conn, config_id=resolved_config_id, start_date=start_date, end_date=end_date)
        action_values = _load_action_values(conn, config_id=resolved_config_id, start_date=start_date, end_date=end_date)
    finally:
        conn.close()

    _audit_recommendation_final_contract_consistency(recommendations, errors)
    _audit_unified_field_artifacts(recommendations, errors)
    _audit_release_block_diagnostics(recommendations, errors)
    _audit_transaction_final_contract_consistency(transactions, recommendations, errors, warnings)
    _audit_open_transactions(transactions, recommendations, errors, warnings)
    _audit_intraday_triggers(transactions, intraday_decisions, errors)
    _audit_action_values(action_values, errors, warnings)
    _audit_recommendation_preference_landing(recommendations, action_values, transactions, errors, warnings)

    counts = {
        "recommendations": len(recommendations),
        "transactions": len(transactions),
        "open_transactions": sum(1 for item in transactions if _is_open_transaction(item)),
        "intraday_decisions": len(intraday_decisions),
        "action_values": len(action_values),
    }
    metadata["audit_boundary"] = (
        "system_invariants_only; no strategy profitability judgment; "
        "does_not_create_trade_authority_or_modify_lots"
    )
    return InvariantAuditReport(ok=not errors, errors=errors, warnings=warnings, counts=counts, metadata=metadata)



