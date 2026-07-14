from __future__ import annotations

"""Read-only daily post-backtest checks over persisted physical facts only."""

import json
import sqlite3
from collections import Counter, defaultdict
from math import isclose
from pathlib import Path
from typing import Any, Iterable, Optional

from agents.decision_team.auditor import audit_verdict_allows_trader
from database.artifact_store import load_externalized_json
from tools.agent_tools.control.pg_schemas import ProtocolCheckResult, ProtocolGovernorReport


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


def _phase_completion_check(
    phases: list[dict[str, Any]],
    learning_events: list[dict[str, Any]],
    target_dates: list[str],
) -> ProtocolCheckResult:
    violations: list[str] = []
    by_day: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in phases:
        by_day[str(row.get("trading_date") or "")[:10]][str(row.get("phase") or "")] = row
    research_days = {
        str(row.get("trading_date") or "")[:10]
        for row in learning_events
        if row.get("event_type") == "researcher_learning_completed" and row.get("status") == "applied"
    }
    for day in target_dates:
        day_phases = by_day.get(day, {})
        for phase in ("phase1", "phase2", "phase3", "phase4"):
            if str((day_phases.get(phase) or {}).get("status") or "") != "completed":
                violations.append("daily_phase_not_completed")
        completed_times = [str((day_phases.get(phase) or {}).get("completed_at") or "") for phase in ("phase1", "phase2", "phase3", "phase4")]
        if all(completed_times) and completed_times != sorted(completed_times):
            violations.append("daily_phase_order_invalid")
        if day not in research_days:
            violations.append("researcher_completion_event_missing")
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
    settlement_days = {str(row.get("trading_date") or "")[:10] for row in settlements}
    for row in recommendations:
        recs_by_day[str(row.get("trading_date") or "")[:10]].append(row)
        if row.get("artifact_read_failed"):
            violations.append("recommendation_artifact_unreadable")
    for row in transactions:
        tx_by_day[str(row.get("trading_date") or "")[:10]].append(row)
    phase_status = {(str(row.get("trading_date") or "")[:10], str(row.get("phase") or "")): str(row.get("status") or "") for row in phases}
    signals_by_portfolio_ticker: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        signals_by_portfolio_ticker[
            (str(signal.get("portfolio_id") or ""), str(signal.get("ticker") or "").upper())
        ].append(signal)
    for day in target_dates:
        day_recs = recs_by_day.get(day, [])
        if phase_status.get((day, "phase1")) == "completed" and not day_recs:
            violations.append("phase1_recommendation_missing")
        for recommendation in day_recs:
            if str(recommendation.get("source_type") or "strategy") != "strategy":
                continue
            snapshot = recommendation.get("signal_snapshot") or {}
            if not isinstance(snapshot.get("signal_collection_contract"), dict):
                violations.append("strategy_signal_collection_contract_missing")
            if not isinstance(snapshot.get("final_action_contract"), dict):
                violations.append("strategy_final_action_contract_missing")
            source_signals = signals_by_portfolio_ticker.get(
                (
                    str(recommendation.get("reference_portfolio_id") or ""),
                    str(recommendation.get("underlying_code") or "").upper(),
                ),
                [],
            )
            expected_analysts = {"technical", "fundamental", "commodity_news"}
            if {str(signal.get("analyst") or "") for signal in source_signals} != expected_analysts:
                violations.append("strategy_analyst_signal_set_incomplete")
            for signal in source_signals:
                artifact = signal.get("artifact") if isinstance(signal.get("artifact"), dict) else {}
                metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
                if signal.get("artifact_read_failed") or not isinstance(metadata.get("action_evidence_contract"), dict):
                    violations.append("analyst_action_evidence_artifact_missing")
                report_path = metadata.get("decision_report_path")
                if report_path and not Path(str(report_path)).exists():
                    violations.append("analyst_decision_report_missing")
            if audit_verdict_allows_trader(_audit_verdict(recommendation)):
                if not isinstance(snapshot.get("execution_result"), dict):
                    violations.append("approved_strategy_execution_result_missing")
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
    for transaction in transactions:
        source_type = str(transaction.get("source_type") or "strategy")
        if source_type not in LEGAL_SOURCE_TYPES:
            violations.append("transaction_source_type_invalid")
            continue
        recommendation_id = str(transaction.get("recommendation_id") or "")
        recommendation = rec_by_id.get(recommendation_id)
        if recommendation is None:
            violations.append("transaction_recommendation_missing")
            continue
        if str(recommendation.get("source_type") or "strategy") != source_type:
            violations.append("transaction_recommendation_source_type_mismatch")
        if source_type == "strategy":
            snapshot = recommendation.get("signal_snapshot") or {}
            if not isinstance(snapshot.get("final_action_contract"), dict):
                violations.append("strategy_transaction_final_action_contract_missing")
        elif source_type == "rollover":
            snapshot = recommendation.get("signal_snapshot") or {}
            if not isinstance(snapshot.get("rollover_policy"), dict):
                violations.append("rollover_transaction_policy_missing")
        elif str(transaction.get("action") or "").startswith("open_"):
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
        if str(recommendation.get("source_type") or "strategy") != "strategy":
            continue
        rec_id = str(recommendation.get("id") or "")
        verdict = _audit_verdict(recommendation)
        tx_count = tx_by_rec.get(rec_id, 0)
        if verdict in {"block", "require_review"} and tx_count:
            violations.append("blocked_strategy_recommendation_has_transaction")
        if verdict and not audit_verdict_allows_trader(verdict) and verdict not in {"block", "require_review"}:
            violations.append("strategy_audit_verdict_invalid")
        if audit_verdict_allows_trader(verdict) and tx_count == 0:
            diagnostics.append("approved_strategy_without_transaction")
    return _failed("audit_release_and_execution_result", violations, diagnostics) if violations else _passed(
        "audit_release_and_execution_result", diagnostics
    )


def _transaction_signature(value: dict[str, Any]) -> tuple[str, int, str]:
    return (
        str(value.get("action") or ""),
        int(value.get("lots") or 0),
        str(value.get("contract_code") or ""),
    )


def _execution_transaction_check(
    recommendations: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
) -> ProtocolCheckResult:
    violations: list[str] = []
    diagnostics: list[str] = []
    tx_by_rec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for transaction in transactions:
        tx_by_rec[str(transaction.get("recommendation_id") or "")].append(transaction)
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
        if Counter(_transaction_signature(row) for row in actual) != Counter(_transaction_signature(row) for row in persisted):
            violations.append("execution_result_transaction_fact_mismatch")
        outcome = str(result.get("outcome") or "")
        if outcome == "executed" and not actual:
            violations.append("executed_outcome_without_transaction")
        if not actual:
            diagnostics.append("legal_untriggered_or_no_trade_result")
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
    settlement_by_day = {str(row.get("trading_date") or "")[:10]: row for row in settlements}
    for day in target_dates:
        settlement = settlement_by_day.get(day)
        if settlement is None:
            continue
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

        if _table_exists(conn, "ticker_daily_pnl"):
            ticker_rows = conn.execute(
                "SELECT daily_pnl, commission FROM ticker_daily_pnl WHERE portfolio_id=? AND substr(trading_date,1,10)=?",
                (str(settlement.get("portfolio_id") or ""), day),
            ).fetchall()
            if not ticker_rows:
                violations.append("ticker_daily_pnl_missing")
            else:
                if not isclose(sum(float(row[0] or 0.0) for row in ticker_rows), daily_pnl, abs_tol=0.01):
                    violations.append("ticker_daily_pnl_total_mismatch")
                if not isclose(sum(float(row[1] or 0.0) for row in ticker_rows), settlement_commission, abs_tol=0.01):
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
    for table, date_column in checks:
        columns = _columns(conn, table)
        if not columns or date_column not in columns or "config_id" not in columns:
            continue
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE config_id=? AND {date_column} IS NOT NULL "
            f"AND substr({date_column}, 1, 10) > ? LIMIT 1",
            (config_id, end_date),
        ).fetchone()
        if row:
            violations.append("future_dated_learning_record_detected")
    for event in learning_events:
        if str(event.get("trading_date") or "")[:10] > end_date:
            violations.append("future_dated_learning_event_detected")
    if not learning_events:
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
            _execution_transaction_check(recommendations, transactions),
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
