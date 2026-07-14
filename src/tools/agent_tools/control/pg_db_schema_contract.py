from __future__ import annotations

"""Read-only contract for physical SQLite facts used by PG."""

import sqlite3
from pathlib import Path
from typing import Iterable, Mapping, Optional, Set

from tools.agent_tools.control.pg_schemas import ProtocolCheckResult


CORE_TABLE_COLUMNS: Mapping[str, Set[str]] = {
    "config": {"id", "exp_name"},
    "portfolio": {"id", "config_id", "trading_date", "account_equity", "positions", "margin_used", "is_settled"},
    "signal": {"id", "portfolio_id", "ticker", "analyst", "artifact_json"},
    "futures_recommendation": {
        "id", "config_id", "reference_portfolio_id", "trading_date", "effective_trade_date",
        "source_type", "underlying_code", "action", "lots", "signal_snapshot", "audit_payload", "status",
    },
    "futures_intraday_decision": {
        "id", "config_id", "trading_date", "recommendation_id", "ticker", "decision", "features_json",
    },
    "futures_transactions": {
        "id", "portfolio_id", "config_id", "recommendation_id", "trading_date", "ticker", "contract_code",
        "action", "lots", "commission", "source_type", "booked_in_settlement",
    },
    "daily_settlement": {
        "id", "portfolio_id", "trading_date", "previous_balance", "current_balance",
        "previous_account_equity", "current_account_equity", "previous_margin", "current_margin",
        "daily_pnl", "commission",
    },
    "ticker_daily_pnl": {"id", "portfolio_id", "trading_date", "ticker", "daily_pnl", "commission"},
    "trading_day_phase": {"id", "config_id", "trading_date", "phase", "status", "started_at", "completed_at"},
    "learning_event_log": {"id", "config_id", "trading_date", "event_type", "agent", "status"},
    "alpha_setup_action_value": {
        "id", "config_id", "action_name", "canonical_action_family", "action_preference",
        "action_value_lane", "learning_lane", "consumer_scope", "last_sample_date",
    },
}


def audit_db_schema_contract(
    db_path: str | Path,
    *,
    required_tables: Optional[Iterable[str]] = None,
) -> ProtocolCheckResult:
    path = Path(db_path)
    violations: list[str] = []
    if not path.exists():
        return ProtocolCheckResult.fail_result(
            "formal_temporary_database",
            ["sqlite_schema_database_missing"],
        )
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            tables = list(required_tables or CORE_TABLE_COLUMNS)
            for table in tables:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    violations.append("sqlite_schema_required_table_missing")
                    continue
                columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
                if not CORE_TABLE_COLUMNS.get(table, set()).issubset(columns):
                    violations.append("sqlite_schema_required_column_missing")
        finally:
            connection.close()
    except sqlite3.Error:
        violations.append("sqlite_schema_database_unreadable")
    if violations:
        return ProtocolCheckResult.fail_result("formal_temporary_database", violations)
    return ProtocolCheckResult.pass_result("formal_temporary_database")
