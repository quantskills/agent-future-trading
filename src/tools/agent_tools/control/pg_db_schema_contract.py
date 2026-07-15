from __future__ import annotations

"""Read-only comparison against the schema produced by formal sqlite_setup."""

import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from tools.agent_tools.control.pg_schemas import ProtocolCheckResult


def _schema(connection: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    return {
        table: {
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        for table in tables
    }


def _formal_reference_schema() -> dict[str, set[str]]:
    from database import sqlite_setup

    with tempfile.TemporaryDirectory(prefix="agentquant_pg_schema_reference_") as raw_tmp:
        reference_path = Path(raw_tmp) / "agentquant.db"
        old_path = sqlite_setup.DB_PATH
        try:
            sqlite_setup.DB_PATH = str(reference_path)
            sqlite_setup.init_database()
        finally:
            sqlite_setup.DB_PATH = old_path
        connection = sqlite3.connect(f"file:{reference_path.resolve()}?mode=ro", uri=True)
        try:
            return _schema(connection)
        finally:
            connection.close()


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
        reference = _formal_reference_schema()
        requested = set(required_tables or reference)
        if requested - set(reference):
            violations.append("sqlite_schema_unknown_required_table")
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            actual = _schema(connection)
        finally:
            connection.close()
        for table in sorted(requested & set(reference)):
            if table not in actual:
                violations.append("sqlite_schema_required_table_missing")
            elif not reference[table].issubset(actual[table]):
                violations.append("sqlite_schema_required_column_missing")
    except sqlite3.Error:
        violations.append("sqlite_schema_database_unreadable")
    except Exception:
        violations.append("sqlite_schema_reference_unavailable")
    if violations:
        return ProtocolCheckResult.fail_result("formal_temporary_database", violations)
    return ProtocolCheckResult.pass_result("formal_temporary_database")
