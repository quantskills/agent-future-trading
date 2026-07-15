from __future__ import annotations

"""Read-only daily post-backtest checks over persisted physical facts only."""

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from math import isclose
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Optional

from agents.decision_team.auditor import audit_verdict_allows_trader
from database.artifact_store import load_externalized_json
from tools.agent_tools.control.pg_schemas import ProtocolCheckResult, ProtocolGovernorReport
from tools.common.contracts import (
    validate_auditor_artifact_boundary,
    validate_execution_artifact_boundary,
    validate_final_action_contract,
)
from tools.common.final_action_semantics import (
    contract_requires_conditional_intraday_result,
    validate_action_value_write_consistency,
    validate_final_action_lot_transition,
)
from tools.common.signal_evidence_collection import (
    validate_action_evidence_contract,
    validate_signal_collection_contract,
)


DAILY_CHECK_NAMES = (
    "daily_phase_completion",
    "physical_result_landing",
    "single_trade_fact_source",
    "audit_release_and_execution_result",
    "execution_and_transaction_fact",
    "settlement_and_account_fact",
    "learning_record_landing_boundary",
)

LEGAL_SOURCE_TYPES = {"strategy", "rollover", "forced_risk"}
EXPECTED_ANALYSTS = ("technical", "fundamental", "commodity_news")
FORBIDDEN_INTERNAL_KEYS = {
    "prompt",
    "raw_prompt",
    "raw_response",
    "llm_prompt",
    "llm_response",
    "internal_reasoning",
    "hidden_context",
    "intermediate_work_state",
    "tool_raw_result",
    "pm_internal_draft",
    "internal_state",
    "reasoning_trace",
}


def _date_text(value: Any) -> str:
    return str(value or "")[:10]


def _parse_timestamp(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _timestamp_after(left: Any, right: Any) -> bool:
    left_value = _parse_timestamp(left)
    right_value = _parse_timestamp(right)
    if left_value is None or right_value is None:
        return False
    if left_value.tzinfo is not None and right_value.tzinfo is None:
        left_value = left_value.replace(tzinfo=None)
    elif left_value.tzinfo is None and right_value.tzinfo is not None:
        right_value = right_value.replace(tzinfo=None)
    return left_value + timedelta(seconds=1) >= right_value


def _contains_internal_information(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_INTERNAL_KEYS and child not in (None, "", [], {}):
                return True
            if _contains_internal_information(child):
                return True
    elif isinstance(value, list):
        return any(_contains_internal_information(item) for item in value)
    return False


def _signed_transaction_delta(action: Any, lots: Any) -> int:
    try:
        quantity = abs(int(lots or 0))
    except (TypeError, ValueError):
        return 0
    action_text = str(getattr(action, "value", action) or "")
    if action_text in {"open_long", "close_short"}:
        return quantity
    if action_text in {"open_short", "close_long"}:
        return -quantity
    return 0


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _signed_positions(value: Any, *, settlement_snapshot: bool = False) -> dict[str, tuple[int, str]]:
    payload = _json_object(value)
    result: dict[str, tuple[int, str]] = {}
    for raw_ticker, raw_position in payload.items():
        if not isinstance(raw_position, dict):
            continue
        ticker = str(raw_ticker or raw_position.get("ticker") or "").upper()
        if not ticker:
            continue
        if settlement_snapshot:
            lots = int(raw_position.get("lots") or 0)
            position_type = str(raw_position.get("position_type") or "").upper()
            shares = -lots if position_type == "SHORT" else lots if position_type == "LONG" else 0
        else:
            shares = int(raw_position.get("shares") or 0)
        contract_code = str(raw_position.get("contract_code") or "")
        result[ticker] = (shares, contract_code)
    return result


def _passed(name: str, diagnostics: Iterable[str] = ()) -> ProtocolCheckResult:
    return ProtocolCheckResult.pass_result(name, diagnostic_codes=diagnostics)


def _failed(name: str, violations: Iterable[str], diagnostics: Iterable[str] = ()) -> ProtocolCheckResult:
    return ProtocolCheckResult.fail_result(name, violations, diagnostic_codes=diagnostics)


def _skipped(name: str, diagnostic: str) -> ProtocolCheckResult:
    return ProtocolCheckResult.skipped_result(name, diagnostic_codes=[diagnostic])


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _json_value(value: Any, artifact_path: Any = None, sha256: Any = None) -> dict[str, Any]:
    loaded = load_externalized_json(value, artifact_path, sha256)
    if isinstance(loaded, str):
        try:
            loaded = json.loads(loaded)
        except (TypeError, ValueError):
            return {}
    return loaded if isinstance(loaded, dict) else {}


def _resolve_config_id(conn: sqlite3.Connection, config_id: Optional[str], exp_name: Optional[str]) -> Optional[str]:
    if config_id:
        return str(config_id)
    if not exp_name or not _table_exists(conn, "config"):
        return None
    row = conn.execute("SELECT id FROM config WHERE exp_name=?", (exp_name,)).fetchone()
    return str(row[0]) if row else None


def _date_clause(column: str, start_date: Optional[str], end_date: Optional[str]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if start_date:
        clauses.append(f"substr({column}, 1, 10) >= ?")
        values.append(start_date)
    if end_date:
        clauses.append(f"substr({column}, 1, 10) <= ?")
        values.append(end_date)
    return ((" AND " + " AND ".join(clauses)) if clauses else "", values)


def _rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    config_id: str,
    date_column: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, table):
        return []
    table_columns = _columns(conn, table)
    if "config_id" not in table_columns:
        return []
    date_sql, params = ("", [])
    if date_column and date_column in table_columns:
        date_sql, params = _date_clause(date_column, start_date, end_date)
    result = conn.execute(
        f"SELECT * FROM {table} WHERE config_id=?{date_sql}",
        (config_id, *params),
    ).fetchall()
    return [dict(row) for row in result]


def _target_dates(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> list[str]:
    if start_date and end_date and start_date == end_date:
        return [start_date]
    phases = _rows(
        conn,
        "trading_day_phase",
        config_id=config_id,
        date_column="trading_date",
        start_date=start_date,
        end_date=end_date,
    )
    dates = sorted({str(row.get("trading_date") or "")[:10] for row in phases if row.get("trading_date")})
    if dates:
        return dates
    if start_date and end_date:
        return [start_date] if start_date == end_date else []
    return []


def _load_recommendations(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> list[dict[str, Any]]:
    rows = _rows(
        conn,
        "futures_recommendation",
        config_id=config_id,
        date_column="trading_date",
        start_date=start_date,
        end_date=end_date,
    )
    loaded: list[dict[str, Any]] = []
    for row in rows:
        try:
            row["signal_snapshot"] = _json_value(
                row.get("signal_snapshot"),
                row.get("signal_snapshot_artifact_path"),
                row.get("signal_snapshot_sha256"),
            )
            row["audit_payload"] = _json_value(
                row.get("audit_payload"),
                row.get("audit_payload_artifact_path"),
                row.get("audit_payload_sha256"),
            )
        except Exception:
            row["artifact_read_failed"] = True
            row["signal_snapshot"] = {}
            row["audit_payload"] = {}
        loaded.append(row)
    return loaded


def _load_transactions(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "futures_transactions",
        config_id=config_id,
        date_column="trading_date",
        start_date=start_date,
        end_date=end_date,
    )


def _load_signals_for_recommendations(
    conn: sqlite3.Connection,
    recommendations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "signal"):
        return []
    portfolio_ids = sorted(
        {str(row.get("reference_portfolio_id") or "") for row in recommendations if row.get("reference_portfolio_id")}
    )
    if not portfolio_ids:
        return []
    placeholders = ",".join("?" for _ in portfolio_ids)
    rows = conn.execute(
        f"SELECT * FROM signal WHERE portfolio_id IN ({placeholders})",
        portfolio_ids,
    ).fetchall()
    loaded: list[dict[str, Any]] = []
    for raw_row in rows:
        row = dict(raw_row)
        try:
            row["artifact"] = _json_value(
                row.get("artifact_json"),
                row.get("artifact_json_artifact_path"),
                row.get("artifact_json_sha256"),
            )
        except Exception:
            row["artifact_read_failed"] = True
            row["artifact"] = {}
        loaded.append(row)
    return loaded


def _audit_verdict(recommendation: dict[str, Any]) -> str:
    payload = recommendation.get("audit_payload")
    snapshot = recommendation.get("signal_snapshot")
    if isinstance(payload, dict) and payload.get("audit_verdict"):
        return str(payload.get("audit_verdict") or "")
    auditor = snapshot.get("auditor") if isinstance(snapshot, dict) else None
    if isinstance(auditor, dict):
        return str(auditor.get("audit_verdict") or "")
    return ""


def _auditor_payload_complete(
    payload: Any,
    *,
    recommendation: dict[str, Any],
) -> bool:
    if not isinstance(payload, dict):
        return False
    required_top = {
        "contract_version",
        "producer",
        "agent_name",
        "recommendation_id",
        "ticker",
        "trading_date",
        "config_id",
        "audit_status",
        "audit_verdict",
        "audit_reason_codes",
        "hard_risk_reasons",
        "soft_risk_reasons",
        "audited_by",
        "audited_at",
        "source",
        "boundary",
        "contract_summary",
        "semantic_state",
    }
    if not required_top.issubset(payload):
        return False
    if payload.get("contract_version") != "agentquant.audit_verdict.v1":
        return False
    if str(payload.get("producer") or "") != "auditor" or str(payload.get("agent_name") or "") != "auditor":
        return False
    if str(payload.get("recommendation_id") or "") != str(recommendation.get("id") or ""):
        return False
    if str(payload.get("ticker") or "").upper() != str(recommendation.get("underlying_code") or "").upper():
        return False
    source = payload.get("source")
    boundary = payload.get("boundary")
    contract_summary = payload.get("contract_summary")
    semantic_state = payload.get("semantic_state")
    if not isinstance(source, dict) or not {
        "pm_recommendation_id",
        "final_action_contract_hash_source",
        "contract_state_source",
        "data_quality_source",
    }.issubset(source):
        return False
    if str(source.get("pm_recommendation_id") or "") != str(recommendation.get("id") or ""):
        return False
    if not isinstance(boundary, dict) or not {
        "auditor_does_not_modify_final_action_contract",
        "auditor_does_not_create_trade_authority",
        "trader_requires_approved_audit_verdict",
        "research_memory_not_consumed",
        "auditor_reads_research_db",
    }.issubset(boundary):
        return False
    if not (
        boundary.get("auditor_does_not_modify_final_action_contract") is True
        and boundary.get("auditor_does_not_create_trade_authority") is True
        and boundary.get("trader_requires_approved_audit_verdict") is True
        and boundary.get("research_memory_not_consumed") is True
        and boundary.get("auditor_reads_research_db") is False
    ):
        return False
    if not isinstance(contract_summary, dict) or not {
        "final_action",
        "current_lots",
        "target_lots",
        "lots_delta",
        "contract_code",
        "invalidation_present",
        "requires_intraday_confirmation",
        "can_execute_without_intraday_trigger",
    }.issubset(contract_summary):
        return False
    if not isinstance(semantic_state, dict) or not {
        "lifecycle_state",
        "requires_intraday_result",
        "hard_block_reasons",
        "soft_limit_reasons",
        "semantic_errors",
    }.issubset(semantic_state):
        return False
    auditor_projection = {key: payload[key] for key in required_top}
    try:
        validate_auditor_artifact_boundary(auditor_projection)
    except ValueError:
        return False
    return True


def _phase_completion_check(
    phases: list[dict[str, Any]],
    learning_events: list[dict[str, Any]],
    target_dates: list[str],
) -> ProtocolCheckResult:
    violations: list[str] = []
    by_day: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in phases:
        by_day[str(row.get("trading_date") or "")[:10]][str(row.get("phase") or "")] = row
    research_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in learning_events:
        if row.get("event_type") == "researcher_learning_completed" and row.get("status") == "applied":
            research_by_day[_date_text(row.get("trading_date"))].append(row)
    for day in target_dates:
        day_phases = by_day.get(day, {})
        for phase in ("phase1", "phase2", "phase3", "phase4"):
            if str((day_phases.get(phase) or {}).get("status") or "") != "completed":
                violations.append("daily_phase_not_completed")
        completed_times = [
            (day_phases.get(phase) or {}).get("completed_at")
            for phase in ("phase1", "phase2", "phase3", "phase4")
        ]
        parsed_times = [_parse_timestamp(value) for value in completed_times]
        if any(value is None for value in parsed_times):
            violations.append("daily_phase_timestamp_invalid")
        elif any(
            not _timestamp_after(completed_times[index], completed_times[index - 1])
            for index in range(1, len(completed_times))
        ):
            violations.append("daily_phase_order_invalid")
        research_events = research_by_day.get(day, [])
        if not research_events:
            violations.append("researcher_completion_event_missing")
        else:
            phase4_completed_at = (day_phases.get("phase4") or {}).get("completed_at")
            for event in research_events:
                if not _timestamp_after(event.get("created_at"), phase4_completed_at):
                    violations.append("researcher_completed_before_phase4")
    return _failed("daily_phase_completion", violations) if violations else _passed("daily_phase_completion")


def _physical_landing_check(
    *,
    conn: sqlite3.Connection,
    recommendations: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    settlements: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    phases: list[dict[str, Any]],
    target_dates: list[str],
) -> ProtocolCheckResult:
    violations: list[str] = []
    diagnostics: list[str] = []
    recs_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    tx_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    settlement_days = {_date_text(row.get("trading_date")) for row in settlements}
    for row in recommendations:
        recs_by_day[_date_text(row.get("trading_date"))].append(row)
        if row.get("artifact_read_failed"):
            violations.append("recommendation_artifact_unreadable")
        if _contains_internal_information(row.get("signal_snapshot")):
            violations.append("agent_internal_information_persisted")
        try:
            validate_execution_artifact_boundary(row.get("signal_snapshot") or {})
        except ValueError:
            violations.append("execution_artifact_boundary_invalid")
    for row in transactions:
        tx_by_day[_date_text(row.get("trading_date"))].append(row)
        if str(row.get("llm_prompt") or "").strip() or _contains_internal_information(
            _json_object(row.get("audit_payload"))
        ):
            violations.append("agent_internal_information_persisted")
    phase_status = {(str(row.get("trading_date") or "")[:10], str(row.get("phase") or "")): str(row.get("status") or "") for row in phases}
    signals_by_id: dict[str, dict[str, Any]] = {}
    signals_by_portfolio_ticker: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        signal_id = str(signal.get("id") or "")
        if signal_id in signals_by_id:
            violations.append("strategy_signal_record_id_duplicate")
        signals_by_id[signal_id] = signal
        signals_by_portfolio_ticker[
            (str(signal.get("portfolio_id") or ""), str(signal.get("ticker") or "").upper())
        ].append(signal)
        if str(signal.get("llm_prompt") or "").strip() or _contains_internal_information(
            signal.get("artifact")
        ):
            violations.append("agent_internal_information_persisted")
    for day in target_dates:
        day_recs = recs_by_day.get(day, [])
        if phase_status.get((day, "phase1")) == "completed" and not day_recs:
            violations.append("phase1_recommendation_missing")
        for recommendation in day_recs:
            if str(recommendation.get("source_type") or "") != "strategy":
                continue
            snapshot = recommendation.get("signal_snapshot") or {}
            scc = snapshot.get("signal_collection_contract")
            fac = snapshot.get("final_action_contract")
            if not isinstance(scc, dict):
                violations.append("strategy_signal_collection_contract_missing")
                continue
            if not isinstance(fac, dict):
                violations.append("strategy_final_action_contract_missing")
            else:
                if validate_final_action_contract(fac):
                    violations.append("strategy_final_action_contract_invalid")
                lot_transition = validate_final_action_lot_transition(fac)
                if not lot_transition.get("ok"):
                    violations.append("strategy_final_action_contract_invalid")

            try:
                validate_signal_collection_contract(
                    scc,
                    ticker=str(recommendation.get("underlying_code") or ""),
                    trading_date=day,
                    enabled_analysts=EXPECTED_ANALYSTS,
                    require_signal_record_ids=True,
                )
            except ValueError:
                violations.append("strategy_signal_collection_contract_invalid")

            source_signals = signals_by_portfolio_ticker.get(
                (
                    str(recommendation.get("reference_portfolio_id") or ""),
                    str(recommendation.get("underlying_code") or "").upper(),
                ),
                [],
            )
            analyst_counts = Counter(str(signal.get("analyst") or "") for signal in source_signals)
            if set(analyst_counts) != set(EXPECTED_ANALYSTS):
                violations.append("strategy_analyst_signal_set_incomplete")
            if any(count != 1 for count in analyst_counts.values()):
                violations.append("strategy_analyst_signal_duplicate")

            source_contracts = scc.get("source_contracts") if isinstance(scc.get("source_contracts"), list) else []
            source_ids = [
                str(source.get("signal_record_id") or "")
                for source in source_contracts
                if isinstance(source, dict)
            ]
            if len(source_ids) != len(set(source_ids)):
                violations.append("strategy_scc_signal_record_id_duplicate")
            lineage_signals: list[Any] = []
            for source in source_contracts:
                if not isinstance(source, dict):
                    continue
                source_id = str(source.get("signal_record_id") or "")
                signal = signals_by_id.get(source_id)
                if signal is None:
                    violations.append("strategy_scc_signal_record_id_mismatch")
                    continue
                expected_portfolio = str(recommendation.get("reference_portfolio_id") or "")
                expected_ticker = str(recommendation.get("underlying_code") or "").upper()
                source_analyst = str(source.get("analyst") or "")
                if (
                    str(signal.get("portfolio_id") or "") != expected_portfolio
                    or str(signal.get("ticker") or "").upper() != expected_ticker
                    or str(signal.get("analyst") or "") != source_analyst
                ):
                    violations.append("strategy_scc_signal_record_id_mismatch")
                artifact = signal.get("artifact") if isinstance(signal.get("artifact"), dict) else {}
                metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
                aec = metadata.get("action_evidence_contract")
                if signal.get("artifact_read_failed") or not isinstance(aec, dict):
                    violations.append("analyst_action_evidence_artifact_missing")
                    continue
                try:
                    validate_action_evidence_contract(aec, analyst=source_analyst)
                except ValueError:
                    violations.append("analyst_action_evidence_contract_invalid")
                if aec != source.get("action_evidence_contract"):
                    violations.append("strategy_scc_aec_lineage_mismatch")
                usage = aec.get("data_usage_summary") if isinstance(aec.get("data_usage_summary"), dict) else {}
                if (
                    str(usage.get("ticker") or "").upper() != expected_ticker
                    or _date_text(usage.get("trading_date")) != day
                    or str(usage.get("analyst") or "") != source_analyst
                ):
                    violations.append("strategy_scc_aec_scope_mismatch")
                lineage_signals.append(
                    SimpleNamespace(
                        agent_name=source_analyst,
                        metadata={
                            "signal_record_id": source_id,
                            "action_evidence_contract": aec,
                        },
                    )
                )
                report_path = metadata.get("decision_report_path")
                if report_path and not Path(str(report_path)).exists():
                    violations.append("analyst_decision_report_missing")
            if len(lineage_signals) == len(EXPECTED_ANALYSTS):
                try:
                    validate_signal_collection_contract(
                        scc,
                        ticker=str(recommendation.get("underlying_code") or ""),
                        trading_date=day,
                        enabled_analysts=EXPECTED_ANALYSTS,
                        analyst_signals=lineage_signals,
                        require_signal_record_ids=True,
                    )
                except ValueError:
                    violations.append("strategy_signal_collection_contract_invalid")
            if audit_verdict_allows_trader(_audit_verdict(recommendation)):
                if not isinstance(snapshot.get("execution_result"), dict):
                    violations.append("approved_strategy_execution_result_missing")
            if phase_status.get((day, "phase2")) == "completed" and not isinstance(
                snapshot.get("execution_result"),
                dict,
            ):
                violations.append("strategy_execution_result_missing")
        if phase_status.get((day, "phase3")) == "completed" and day not in settlement_days:
            violations.append("daily_settlement_missing")
        if not tx_by_day.get(day):
            diagnostics.append("legal_no_transaction_day")
    if decisions:
        diagnostics.append("intraday_decision_path_present")
    return _failed("physical_result_landing", violations, diagnostics) if violations else _passed(
        "physical_result_landing", diagnostics
    )


def _single_trade_source_check(
    recommendations: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
) -> ProtocolCheckResult:
    violations: list[str] = []
    rec_by_id = {str(row.get("id") or ""): row for row in recommendations}
    strategy_counts = Counter(
        (_date_text(row.get("trading_date")), str(row.get("underlying_code") or "").upper())
        for row in recommendations
        if str(row.get("source_type") or "") == "strategy"
    )
    if any(count != 1 for count in strategy_counts.values()):
        violations.append("strategy_recommendation_not_unique")
    for transaction in transactions:
        source_type = str(transaction.get("source_type") or "")
        if not source_type:
            violations.append("transaction_source_type_missing")
            continue
        if source_type not in LEGAL_SOURCE_TYPES:
            violations.append("transaction_source_type_invalid")
            continue
        recommendation_id = str(transaction.get("recommendation_id") or "")
        recommendation = rec_by_id.get(recommendation_id)
        if recommendation is None:
            violations.append("transaction_recommendation_missing")
            continue
        recommendation_source = str(recommendation.get("source_type") or "")
        if not recommendation_source:
            violations.append("transaction_recommendation_source_type_missing")
        elif recommendation_source != source_type:
            violations.append("transaction_recommendation_source_type_mismatch")
        if source_type == "strategy":
            snapshot = recommendation.get("signal_snapshot") or {}
            if not isinstance(snapshot.get("final_action_contract"), dict):
                violations.append("strategy_transaction_final_action_contract_missing")
        elif source_type == "rollover":
            snapshot = recommendation.get("signal_snapshot") or {}
            if not isinstance(snapshot.get("rollover_policy"), dict):
                violations.append("rollover_transaction_policy_missing")
        else:
            snapshot = recommendation.get("signal_snapshot") or {}
            if not isinstance(snapshot.get("forced_risk_boundary"), dict):
                violations.append("forced_risk_transaction_boundary_missing")
            if str(transaction.get("action") or "").startswith("open_"):
                violations.append("forced_risk_transaction_cannot_open")
    return _failed("single_trade_fact_source", violations) if violations else _passed("single_trade_fact_source")


def _audit_execution_check(
    recommendations: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
) -> ProtocolCheckResult:
    violations: list[str] = []
    diagnostics: list[str] = []
    tx_by_rec = Counter(str(row.get("recommendation_id") or "") for row in transactions)
    for recommendation in recommendations:
        if str(recommendation.get("source_type") or "") != "strategy":
            continue
        rec_id = str(recommendation.get("id") or "")
        audit_payload = recommendation.get("audit_payload")
        if not isinstance(audit_payload, dict) or not audit_payload:
            violations.append("strategy_auditor_payload_missing")
        elif not _auditor_payload_complete(audit_payload, recommendation=recommendation):
            violations.append("strategy_auditor_payload_incomplete")
        verdict = _audit_verdict(recommendation)
        tx_count = tx_by_rec.get(rec_id, 0)
        if tx_count and not verdict:
            violations.append("strategy_transaction_missing_auditor_verdict")
        if verdict in {"block", "require_review"} and tx_count:
            violations.append("blocked_strategy_recommendation_has_transaction")
        if verdict and not audit_verdict_allows_trader(verdict) and verdict not in {"block", "require_review"}:
            violations.append("strategy_audit_verdict_invalid")
        if tx_count and not audit_verdict_allows_trader(verdict):
            violations.append("strategy_transaction_without_execution_permission")
        if audit_verdict_allows_trader(verdict) and tx_count == 0:
            diagnostics.append("approved_strategy_without_transaction")
        snapshot = recommendation.get("signal_snapshot") or {}
        snapshot_auditor = snapshot.get("auditor") if isinstance(snapshot, dict) else None
        if isinstance(snapshot_auditor, dict) and isinstance(audit_payload, dict):
            for key in (
                "contract_version",
                "producer",
                "recommendation_id",
                "audit_status",
                "audit_verdict",
                "audit_reason_codes",
                "hard_risk_reasons",
                "soft_risk_reasons",
                "source",
                "boundary",
                "contract_summary",
                "semantic_state",
            ):
                if key in snapshot_auditor and snapshot_auditor.get(key) != audit_payload.get(key):
                    violations.append("strategy_auditor_payload_not_preserved")
                    break
        if isinstance(snapshot, dict) and isinstance(snapshot.get("execution_result"), dict):
            for field in (
                "trade_contract_audit",
                "execution_translation",
                "execution_result",
                "phase2_execution",
            ):
                if not isinstance((audit_payload or {}).get(field), dict):
                    violations.append("strategy_auditor_execution_append_missing")
                    break
    return _failed("audit_release_and_execution_result", violations, diagnostics) if violations else _passed(
        "audit_release_and_execution_result", diagnostics
    )


def _transaction_signature(value: dict[str, Any]) -> tuple[str, int, str, float, str]:
    return (
        str(value.get("action") or ""),
        int(value.get("lots") or 0),
        str(value.get("contract_code") or ""),
        round(float(value.get("execution_price") or 0.0), 8),
        str(value.get("execution_phase") or ""),
    )


def _execution_transaction_check(
    recommendations: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> ProtocolCheckResult:
    violations: list[str] = []
    diagnostics: list[str] = []
    tx_by_rec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for transaction in transactions:
        tx_by_rec[str(transaction.get("recommendation_id") or "")].append(transaction)
    decisions_by_rec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        decisions_by_rec[str(decision.get("recommendation_id") or "")].append(decision)
    for recommendation in recommendations:
        rec_id = str(recommendation.get("id") or "")
        snapshot = recommendation.get("signal_snapshot") or {}
        result = snapshot.get("execution_result") if isinstance(snapshot, dict) else None
        persisted = tx_by_rec.get(rec_id, [])
        if not isinstance(result, dict):
            if persisted:
                violations.append("transaction_without_execution_result")
            continue
        actual = result.get("actual_transactions") if isinstance(result.get("actual_transactions"), list) else []
        declared_count = int(result.get("transaction_count") or 0)
        if declared_count != len(actual):
            violations.append("execution_result_transaction_count_mismatch")
        if any(
            not isinstance(row, dict)
            or not {"action", "lots", "contract_code", "execution_price", "execution_phase"}.issubset(row)
            for row in actual
        ):
            violations.append("execution_result_transaction_required_field_missing")
        if Counter(_transaction_signature(row) for row in actual) != Counter(_transaction_signature(row) for row in persisted):
            violations.append("execution_result_transaction_fact_mismatch")
        outcome = str(result.get("outcome") or "")
        if outcome == "executed" and not actual:
            violations.append("executed_outcome_without_transaction")
        if not actual:
            diagnostics.append("legal_untriggered_or_no_trade_result")
        source_type = str(recommendation.get("source_type") or "")
        if source_type == "strategy":
            fac = snapshot.get("final_action_contract") if isinstance(snapshot, dict) else None
            if not isinstance(fac, dict):
                continue
            authorized_delta = int(fac.get("target_lots") or 0) - int(fac.get("current_lots") or 0)
            executed_delta = sum(
                _signed_transaction_delta(row.get("action"), row.get("lots"))
                for row in persisted
            )
            if persisted and any(
                _signed_transaction_delta(row.get("action"), row.get("lots")) == 0
                for row in persisted
            ):
                violations.append("strategy_execution_action_not_canonical")
            if abs(executed_delta) > abs(authorized_delta):
                violations.append("strategy_execution_exceeds_fac_authorized_lots")
            if executed_delta and (not authorized_delta or (executed_delta > 0) != (authorized_delta > 0)):
                violations.append("strategy_execution_direction_not_authorized")
            authorized_contract = str(fac.get("contract_code") or "")
            if persisted and (
                not authorized_contract
                or any(str(row.get("contract_code") or "") != authorized_contract for row in persisted)
            ):
                violations.append("strategy_execution_contract_not_authorized")

            requires_intraday = contract_requires_conditional_intraday_result(fac)
            intraday_rows = decisions_by_rec.get(rec_id, [])
            if requires_intraday and not intraday_rows:
                violations.append("conditional_fac_intraday_decision_missing")
            if requires_intraday and intraday_rows:
                decision_values = {str(row.get("decision") or "") for row in intraday_rows}
                if not decision_values.issubset({"execute", "wait", "skip"}):
                    violations.append("conditional_fac_intraday_decision_invalid")
                if persisted and "execute" not in decision_values:
                    violations.append("conditional_fac_transaction_without_execute_decision")
    return _failed("execution_and_transaction_fact", violations, diagnostics) if violations else _passed(
        "execution_and_transaction_fact", diagnostics
    )


def _settlement_account_check(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    transactions: list[dict[str, Any]],
    settlements: list[dict[str, Any]],
    target_dates: list[str],
) -> ProtocolCheckResult:
    violations: list[str] = []
    diagnostics: list[str] = []
    tx_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for transaction in transactions:
        tx_by_day[str(transaction.get("trading_date") or "")[:10]].append(transaction)
    settlements_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for settlement in settlements:
        settlements_by_day[_date_text(settlement.get("trading_date"))].append(settlement)
    for day in target_dates:
        day_settlements = settlements_by_day.get(day, [])
        if not day_settlements:
            continue
        if len(day_settlements) != 1:
            violations.append("daily_settlement_not_unique")
        settlement = day_settlements[-1]
        day_transactions = tx_by_day.get(day, [])
        if any(int(row.get("booked_in_settlement") or 0) != 1 for row in day_transactions):
            violations.append("transaction_not_booked_exactly_once")
        commission = sum(float(row.get("commission") or 0.0) for row in day_transactions)
        if not isclose(commission, float(settlement.get("commission") or 0.0), abs_tol=0.01):
            violations.append("settlement_commission_mismatch")
        previous_equity = float(settlement.get("previous_account_equity") or 0.0)
        current_equity = float(settlement.get("current_account_equity") or 0.0)
        previous_balance = float(settlement.get("previous_balance") or 0.0)
        current_balance = float(settlement.get("current_balance") or 0.0)
        previous_margin = float(settlement.get("previous_margin") or 0.0)
        current_margin = float(settlement.get("current_margin") or 0.0)
        daily_pnl = float(settlement.get("daily_pnl") or 0.0)
        settlement_commission = float(settlement.get("commission") or 0.0)
        if not previous_equity:
            previous_equity = previous_balance + previous_margin
        if not current_equity:
            current_equity = current_balance + current_margin
        if not isclose(current_equity - previous_equity, daily_pnl - settlement_commission, abs_tol=0.01):
            violations.append("settlement_equity_formula_mismatch")
        expected_cash_change = daily_pnl - settlement_commission - (current_margin - previous_margin)
        if not isclose(current_balance - previous_balance, expected_cash_change, abs_tol=0.01):
            violations.append("settlement_cash_formula_mismatch")
        if not isclose(current_balance + current_margin, current_equity, abs_tol=0.01):
            violations.append("settlement_account_split_mismatch")
        if not isclose(float(settlement.get("cash_available") or 0.0), current_balance, abs_tol=0.01):
            violations.append("settlement_cash_available_mismatch")
        if not isclose(float(settlement.get("reserved_margin") or 0.0), current_margin, abs_tol=0.01):
            violations.append("settlement_reserved_margin_mismatch")
        expected_margin_ratio = current_margin / current_equity if current_equity > 0 else (1.0 if current_margin > 0 else 0.0)
        if not isclose(float(settlement.get("margin_ratio") or 0.0), expected_margin_ratio, abs_tol=0.000001):
            violations.append("settlement_margin_ratio_mismatch")

        ticker_rows: list[sqlite3.Row] = []
        if _table_exists(conn, "ticker_daily_pnl"):
            ticker_rows = conn.execute(
                "SELECT * FROM ticker_daily_pnl WHERE portfolio_id=? AND substr(trading_date,1,10)=?",
                (str(settlement.get("portfolio_id") or ""), day),
            ).fetchall()
            snapshot_has_positions = bool(
                _signed_positions(
                    settlement.get("positions_snapshot"),
                    settlement_snapshot=True,
                )
            )
            if not ticker_rows and (day_transactions or snapshot_has_positions):
                violations.append("ticker_daily_pnl_missing")
            elif ticker_rows:
                if not isclose(sum(float(row["daily_pnl"] or 0.0) for row in ticker_rows), daily_pnl, abs_tol=0.01):
                    violations.append("ticker_daily_pnl_total_mismatch")
                if not isclose(sum(float(row["commission"] or 0.0) for row in ticker_rows), settlement_commission, abs_tol=0.01):
                    violations.append("ticker_daily_pnl_commission_mismatch")

        portfolios = _rows(
            conn,
            "portfolio",
            config_id=config_id,
            date_column="trading_date",
            start_date=day,
            end_date=day,
        )
        settled = [row for row in portfolios if int(row.get("is_settled") or 0) == 1]
        if not settled:
            violations.append("settled_portfolio_missing")
        else:
            latest = settled[-1]
            if not isclose(float(latest.get("account_equity") or 0.0), current_equity, abs_tol=0.01):
                violations.append("portfolio_account_equity_mismatch")
            if not isclose(float(latest.get("margin_used") or 0.0), current_margin, abs_tol=0.01):
                violations.append("portfolio_margin_mismatch")
            if not isclose(float(latest.get("cash_available") or 0.0), current_balance, abs_tol=0.01):
                violations.append("portfolio_cash_available_mismatch")
            portfolio_positions = _signed_positions(latest.get("positions"))
            settlement_positions = _signed_positions(
                settlement.get("positions_snapshot"),
                settlement_snapshot=True,
            )
            if portfolio_positions != {
                ticker: value
                for ticker, value in settlement_positions.items()
                if value[0] != 0
            }:
                violations.append("settlement_positions_snapshot_mismatch")

            transaction_delta_by_ticker: dict[str, int] = defaultdict(int)
            for transaction in day_transactions:
                transaction_delta_by_ticker[str(transaction.get("ticker") or "").upper()] += _signed_transaction_delta(
                    transaction.get("action"), transaction.get("lots")
                )
            recommendation_rows = _load_recommendations(
                conn,
                config_id=config_id,
                start_date=day,
                end_date=day,
            )
            strategy_by_ticker = {
                str(row.get("underlying_code") or "").upper(): row
                for row in recommendation_rows
                if str(row.get("source_type") or "") == "strategy"
            }
            for ticker, delta in transaction_delta_by_ticker.items():
                final_lots = settlement_positions.get(ticker, (0, ""))[0]
                strategy_recommendation = strategy_by_ticker.get(ticker)
                if strategy_recommendation is None:
                    continue
                fac = (strategy_recommendation.get("signal_snapshot") or {}).get("final_action_contract")
                if not isinstance(fac, dict):
                    continue
                current_lots = int(fac.get("current_lots") or 0)
                if final_lots != current_lots + delta:
                    violations.append("settlement_ticker_lot_transition_mismatch")

            ticker_rows_by_ticker = {str(row["ticker"] or "").upper(): row for row in ticker_rows}
            for ticker, (signed_lots, _contract_code) in settlement_positions.items():
                ticker_row = ticker_rows_by_ticker.get(ticker)
                if ticker_row is None:
                    violations.append("ticker_daily_pnl_missing")
                    continue
                if int(float(ticker_row["lots"] or 0.0)) != abs(signed_lots):
                    violations.append("ticker_daily_pnl_lots_mismatch")
                expected_type = "LONG" if signed_lots > 0 else "SHORT" if signed_lots < 0 else "FLAT"
                if str(ticker_row["position_type"] or "").upper() != expected_type:
                    violations.append("ticker_daily_pnl_position_type_mismatch")
        if not day_transactions:
            diagnostics.append("settlement_day_without_transaction")
    return _failed("settlement_and_account_fact", violations, diagnostics) if violations else _passed(
        "settlement_and_account_fact", diagnostics
    )


def _learning_boundary_check(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    end_date: Optional[str],
    learning_events: list[dict[str, Any]],
) -> ProtocolCheckResult:
    violations: list[str] = []
    diagnostics: list[str] = []
    if not end_date:
        diagnostics.append("learning_date_upper_bound_not_requested")
        return _passed("learning_record_landing_boundary", diagnostics)
    checks = (
        ("alpha_setup_sample", "trading_date"),
        ("alpha_setup_profile", "last_sample_date"),
        ("alpha_setup_action_value", "last_sample_date"),
        ("adaptive_policy_state", "source_trading_date"),
        ("provisional_policy_state", "source_trading_date"),
        ("config_learning_overlay", "trading_date"),
        ("trade_episode_memory", "trading_date"),
        ("no_trade_opportunity_memory", "trading_date"),
    )
    structured_rows: list[tuple[str, str, dict[str, Any]]] = []
    for table, date_column in checks:
        columns = _columns(conn, table)
        if not columns or date_column not in columns or "config_id" not in columns:
            continue
        rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM {table} WHERE config_id=? AND {date_column} IS NOT NULL",
                (config_id,),
            ).fetchall()
        ]
        for row in rows:
            source_day = _date_text(row.get(date_column))
            structured_rows.append((table, source_day, row))
            if source_day > end_date:
                violations.append("future_dated_learning_record_detected")
            for column, value in row.items():
                if column.endswith("_json") and value:
                    if _contains_internal_information(_json_object(value)):
                        violations.append("agent_internal_information_persisted")

    phase4_rows = _rows(
        conn,
        "trading_day_phase",
        config_id=config_id,
        date_column="trading_date",
    )
    phase4_by_day = {
        _date_text(row.get("trading_date")): row
        for row in phase4_rows
        if str(row.get("phase") or "") == "phase4" and str(row.get("status") or "") == "completed"
    }
    settlement_days: set[str] = set()
    if _table_exists(conn, "daily_settlement") and _table_exists(conn, "portfolio"):
        settlement_days = {
            _date_text(row[0])
            for row in conn.execute(
                "SELECT ds.trading_date FROM daily_settlement ds "
                "JOIN portfolio p ON p.id=ds.portfolio_id WHERE p.config_id=?",
                (config_id,),
            ).fetchall()
        }
    recommendation_ids = {
        str(row[0])
        for row in conn.execute(
            "SELECT id FROM futures_recommendation WHERE config_id=?",
            (config_id,),
        ).fetchall()
    } if _table_exists(conn, "futures_recommendation") else set()
    transaction_recommendation_ids = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT recommendation_id FROM futures_transactions "
            "WHERE config_id=? AND recommendation_id IS NOT NULL",
            (config_id,),
        ).fetchall()
    } if _table_exists(conn, "futures_transactions") else set()

    for table, source_day, row in structured_rows:
        phase4 = phase4_by_day.get(source_day)
        if phase4 is None or source_day not in settlement_days:
            violations.append("learning_source_phase4_or_settlement_missing")
        completion_time = (phase4 or {}).get("completed_at")
        write_time = row.get("updated_at") or row.get("created_at")
        if write_time and completion_time and not _timestamp_after(write_time, completion_time):
            violations.append("learning_record_written_before_phase4")
        recommendation_id = str(row.get("recommendation_id") or "")
        source_type = str(row.get("source_type") or "")
        if recommendation_id and not recommendation_id.startswith("counterfactual:"):
            if recommendation_id not in recommendation_ids:
                violations.append("learning_recommendation_id_missing")
        if table == "alpha_setup_sample" and source_type in {"trade", "trade_episode"}:
            executed_lots = int(row.get("executed_lots") or 0)
            if not recommendation_id:
                violations.append("trade_learning_recommendation_id_missing")
            elif executed_lots > 0 and recommendation_id not in transaction_recommendation_ids:
                ticker = str(row.get("ticker") or "").upper()
                prior_transaction = conn.execute(
                    "SELECT 1 FROM futures_transactions WHERE config_id=? AND ticker=? "
                    "AND substr(trading_date,1,10)<=? LIMIT 1",
                    (config_id, ticker, source_day),
                ).fetchone()
                if prior_transaction is None:
                    violations.append("trade_learning_transaction_lineage_missing")

    if _table_exists(conn, "alpha_setup_action_value"):
        for raw_row in conn.execute(
            "SELECT * FROM alpha_setup_action_value WHERE config_id=?",
            (config_id,),
        ).fetchall():
            row = dict(raw_row)
            row["payload"] = _json_object(row.get("payload_json"))
            if not validate_action_value_write_consistency(row).get("ok"):
                violations.append("learning_action_value_contract_invalid")

    if _table_exists(conn, "researcher_llm_notes"):
        note_columns = _columns(conn, "researcher_llm_notes")
        raw_fields = [field for field in ("raw_prompt", "raw_response") if field in note_columns]
        if raw_fields:
            predicate = " OR ".join(f"COALESCE({field}, '') <> ''" for field in raw_fields)
            if conn.execute(
                f"SELECT 1 FROM researcher_llm_notes WHERE config_id=? AND ({predicate}) LIMIT 1",
                (config_id,),
            ).fetchone():
                violations.append("agent_internal_information_persisted")
    for event in learning_events:
        if str(event.get("trading_date") or "")[:10] > end_date:
            violations.append("future_dated_learning_event_detected")
    if not structured_rows:
        diagnostics.append("no_learning_record_generated")
    return _failed("learning_record_landing_boundary", violations, diagnostics) if violations else _passed(
        "learning_record_landing_boundary", diagnostics
    )


def audit_system_invariants(
    *,
    db_path: str | Path,
    config_id: Optional[str] = None,
    exp_name: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> ProtocolGovernorReport:
    """Audit one completed backtest day or an explicitly requested date range."""

    db_path = Path(db_path)
    if not db_path.exists():
        checks = [_failed(DAILY_CHECK_NAMES[0], ["daily_database_missing"])]
        checks.extend(_skipped(name, "daily_database_unavailable") for name in DAILY_CHECK_NAMES[1:])
        return ProtocolGovernorReport(checks=checks)

    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        checks = [_failed(DAILY_CHECK_NAMES[0], ["daily_database_unreadable"])]
        checks.extend(_skipped(name, "daily_database_unavailable") for name in DAILY_CHECK_NAMES[1:])
        return ProtocolGovernorReport(checks=checks)

    try:
        resolved_config_id = _resolve_config_id(conn, config_id, exp_name)
        if not resolved_config_id:
            checks = [_failed(DAILY_CHECK_NAMES[0], ["daily_config_not_found"])]
            checks.extend(_skipped(name, "daily_config_unavailable") for name in DAILY_CHECK_NAMES[1:])
            return ProtocolGovernorReport(checks=checks)

        target_dates = _target_dates(
            conn,
            config_id=resolved_config_id,
            start_date=start_date,
            end_date=end_date,
        )
        if not target_dates:
            checks = [_failed(DAILY_CHECK_NAMES[0], ["daily_target_date_not_found"])]
            checks.extend(_skipped(name, "daily_target_date_unavailable") for name in DAILY_CHECK_NAMES[1:])
            return ProtocolGovernorReport(checks=checks)
        effective_start = start_date or target_dates[0]
        effective_end = end_date or target_dates[-1]
        phases = _rows(
            conn,
            "trading_day_phase",
            config_id=resolved_config_id,
            date_column="trading_date",
            start_date=effective_start,
            end_date=effective_end,
        )
        learning_events = _rows(
            conn,
            "learning_event_log",
            config_id=resolved_config_id,
            date_column="trading_date",
            start_date=effective_start,
            end_date=effective_end,
        )
        recommendations = _load_recommendations(
            conn,
            config_id=resolved_config_id,
            start_date=effective_start,
            end_date=effective_end,
        )
        transactions = _load_transactions(
            conn,
            config_id=resolved_config_id,
            start_date=effective_start,
            end_date=effective_end,
        )
        signals = _load_signals_for_recommendations(conn, recommendations)
        decisions = _rows(
            conn,
            "futures_intraday_decision",
            config_id=resolved_config_id,
            date_column="trading_date",
            start_date=effective_start,
            end_date=effective_end,
        )
        settlements = []
        if _table_exists(conn, "daily_settlement") and _table_exists(conn, "portfolio"):
            date_sql, params = _date_clause("s.trading_date", effective_start, effective_end)
            settlements = [
                dict(row)
                for row in conn.execute(
                    f"SELECT s.* FROM daily_settlement s JOIN portfolio p ON p.id=s.portfolio_id "
                    f"WHERE p.config_id=?{date_sql}",
                    (resolved_config_id, *params),
                ).fetchall()
            ]

        checks = [
            _phase_completion_check(phases, learning_events, target_dates),
            _physical_landing_check(
                conn=conn,
                recommendations=recommendations,
                transactions=transactions,
                decisions=decisions,
                settlements=settlements,
                signals=signals,
                phases=phases,
                target_dates=target_dates,
            ),
            _single_trade_source_check(recommendations, transactions),
            _audit_execution_check(recommendations, transactions),
            _execution_transaction_check(recommendations, transactions, decisions),
            _settlement_account_check(
                conn,
                config_id=resolved_config_id,
                transactions=transactions,
                settlements=settlements,
                target_dates=target_dates,
            ),
            _learning_boundary_check(
                conn,
                config_id=resolved_config_id,
                end_date=effective_end,
                learning_events=learning_events,
            ),
        ]
        return ProtocolGovernorReport(checks=checks)
    finally:
        conn.close()
