from __future__ import annotations

"""Read-only SQLite schema contract for control-team gates.

This module does not define new business fields. It records the existing
runtime SQLite table/column contract that control audits are allowed to read.
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Set


CORE_TABLE_COLUMNS: Mapping[str, Set[str]] = {
    "config": {"id", "exp_name"},
    "futures_recommendation": {
        "id",
        "config_id",
        "trading_date",
        "effective_trade_date",
        "source_type",
        "underlying_code",
        "action",
        "lots",
        "signal_snapshot",
        "audit_payload",
        "status",
        "created_at",
    },
    "futures_transactions": {
        "id",
        "portfolio_id",
        "config_id",
        "recommendation_id",
        "trading_date",
        "ticker",
        "action",
        "lots",
        "audit_payload",
        "created_at",
    },
    "futures_intraday_decision": {
        "id",
        "config_id",
        "trading_date",
        "recommendation_id",
        "ticker",
        "decision",
        "trigger_reason",
        "features_json",
        "created_at",
    },
    "portfolio": {"id", "config_id"},
    "daily_settlement": {
        "id",
        "portfolio_id",
        "trading_date",
        "daily_pnl",
        "commission",
        "current_balance",
        "current_margin",
        "margin_ratio",
        "positions_snapshot",
        "created_at",
    },
    "trading_day_phase": {
        "id",
        "config_id",
        "trading_date",
        "phase",
        "status",
        "started_at",
        "completed_at",
        "message",
    },
    "alpha_setup_action_value": {
        "id",
        "config_id",
        "scope_key",
        "ticker",
        "side",
        "action_name",
        "action_preference",
        "reward_source",
        "evidence_scope",
        "action_value_lane",
        "consumer_scope",
        "memory_side_role",
        "last_sample_date",
        "active",
        "payload_json",
    },
    "adaptive_policy_state": {
        "id",
        "config_id",
        "ticker",
        "side",
        "policy_type",
        "policy_action",
        "source_trading_date",
        "active",
        "payload_json",
        "created_at",
    },
    "researcher_llm_notes": {
        "id",
        "config_id",
        "trading_date",
        "evidence_pack_id",
        "ticker",
        "raw_prompt",
        "raw_response",
        "created_at",
        "payload_json",
    },
}

TABLE_DATE_COLUMNS: Mapping[str, str] = {
    "futures_recommendation": "trading_date",
    "futures_transactions": "trading_date",
    "futures_intraday_decision": "trading_date",
    "daily_settlement": "trading_date",
    "trading_day_phase": "trading_date",
    "alpha_setup_action_value": "last_sample_date",
    "adaptive_policy_state": "source_trading_date",
    "researcher_llm_notes": "trading_date",
}


@dataclass
class DbSchemaContractReport:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, object] = field(default_factory=dict)


def table_columns(conn: sqlite3.Connection, table_name: str) -> Set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def audit_db_schema_contract(
    db_path: str | Path,
    *,
    required_tables: Optional[Iterable[str]] = None,
) -> DbSchemaContractReport:
    db_path = Path(db_path)
    if not db_path.exists():
        return DbSchemaContractReport(
            ok=True,
            warnings=[f"sqlite_missing:{db_path}"],
            metadata={
                "db_path": str(db_path),
                "schema_boundary": "skipped_sqlite_missing",
            },
        )

    errors: List[str] = []
    metadata: Dict[str, object] = {
        "db_path": str(db_path),
        "schema_boundary": "core_runtime_tables_must_match_existing_sqlite_contract",
        "date_columns": dict(TABLE_DATE_COLUMNS),
    }
    tables = list(required_tables or CORE_TABLE_COLUMNS.keys())
    observed: Dict[str, List[str]] = {}

    conn = sqlite3.connect(str(db_path))
    try:
        for table_name in tables:
            expected_columns = CORE_TABLE_COLUMNS.get(table_name, set())
            if not table_exists(conn, table_name):
                errors.append(f"schema_missing_required_table:{table_name}")
                observed[table_name] = []
                continue
            columns = table_columns(conn, table_name)
            observed[table_name] = sorted(columns)
            for column in sorted(expected_columns):
                if column not in columns:
                    errors.append(f"schema_missing_required_column:{table_name}:{column}")
            date_column = TABLE_DATE_COLUMNS.get(table_name)
            if date_column and date_column not in columns:
                errors.append(f"schema_missing_date_column:{table_name}:{date_column}")
    finally:
        conn.close()

    metadata["observed_columns"] = observed
    return DbSchemaContractReport(ok=not errors, errors=errors, metadata=metadata)
