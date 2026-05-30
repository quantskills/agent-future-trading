from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from database.sqlite_setup import DB_PATH


ARTIFACT_TABLES = {
    "futures_recommendation": ["signal_snapshot", "audit_payload"],
    "futures_transactions": ["audit_payload", "llm_prompt"],
    "signal": ["llm_prompt", "artifact_json"],
    "signal_context_history": ["analyst_signals", "market_confirmation", "pre_open_plan"],
    "reviewer_llm_notes": ["raw_prompt", "raw_response", "payload"],
}


def _resolve_path(path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _table_columns(cursor: sqlite3.Cursor, table_name: str) -> set[str]:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {str(row[1]) for row in cursor.fetchall()}


def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _date_column(columns: set[str]) -> str | None:
    for column in ("trading_date", "effective_trade_date", "updated_at", "created_at"):
        if column in columns:
            return column
    return None


def _iter_artifact_rows(
    cursor: sqlite3.Cursor,
    table_name: str,
    fields: Iterable[str],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
):
    columns = _table_columns(cursor, table_name)
    table_alias = "t"
    from_clause = f"{table_name} {table_alias}"
    if table_name == "signal" and "portfolio_id" in columns and _table_exists(cursor, "portfolio"):
        from_clause = f"{table_name} {table_alias} JOIN portfolio p ON {table_alias}.portfolio_id = p.id"
        date_expr = "p.trading_date"
    else:
        date_column = _date_column(columns)
        date_expr = f"{table_alias}.{date_column}" if date_column else None

    id_expr = f"{table_alias}.id AS id" if "id" in columns else f"{table_alias}.rowid AS id"
    select_parts = [id_expr]
    valid_fields = []
    for field in fields:
        required = {
            f"{field}_artifact_path",
            f"{field}_sha256",
            f"{field}_size",
        }
        if required.issubset(columns):
            valid_fields.append(field)
            select_parts.extend(
                [
                    f"{table_alias}.{field}_artifact_path AS {field}_artifact_path",
                    f"{table_alias}.{field}_sha256 AS {field}_sha256",
                    f"{table_alias}.{field}_size AS {field}_size",
                ]
            )
    if not valid_fields:
        return []
    where_clause = " OR ".join(f"{table_alias}.{field}_artifact_path IS NOT NULL" for field in valid_fields)
    params: list[str] = []
    if date_expr and (start_date or end_date):
        if start_date:
            where_clause = f"({where_clause}) AND substr({date_expr}, 1, 10) >= ?"
            params.append(start_date)
        if end_date:
            where_clause = f"({where_clause}) AND substr({date_expr}, 1, 10) <= ?"
            params.append(end_date)
    cursor.execute(
        f"SELECT {', '.join(select_parts)} FROM {from_clause} WHERE {where_clause}",
        params,
    )
    return [(dict(row), valid_fields) for row in cursor.fetchall()]


def validate_artifacts(
    db_path: str | Path = DB_PATH,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    report = {
        "db_path": str(db_path),
        "start_date": start_date,
        "end_date": end_date,
        "checked": 0,
        "missing": [],
        "hash_mismatch": [],
        "size_mismatch": [],
        "tables": {},
    }
    try:
        cursor = conn.cursor()
        for table_name, fields in ARTIFACT_TABLES.items():
            if not _table_exists(cursor, table_name):
                continue
            table_report = {"checked": 0, "fields": {}}
            for row, valid_fields in _iter_artifact_rows(
                cursor,
                table_name,
                fields,
                start_date=start_date,
                end_date=end_date,
            ):
                row_id = str(row.get("id"))
                for field in valid_fields:
                    path_value = row.get(f"{field}_artifact_path")
                    if not path_value:
                        continue
                    table_report["checked"] += 1
                    report["checked"] += 1
                    table_report["fields"][field] = table_report["fields"].get(field, 0) + 1
                    path = _resolve_path(str(path_value))
                    item = {"table": table_name, "id": row_id, "field": field, "path": str(path_value)}
                    if not path.exists():
                        report["missing"].append(item)
                        continue
                    expected_sha = row.get(f"{field}_sha256")
                    if expected_sha and _digest(path) != expected_sha:
                        report["hash_mismatch"].append(item)
                    expected_size = row.get(f"{field}_size")
                    if expected_size is not None and path.stat().st_size != int(expected_size):
                        report["size_mismatch"].append(item)
            if table_report["checked"]:
                report["tables"][table_name] = table_report
    finally:
        conn.close()
    report["ok"] = not report["missing"] and not report["hash_mismatch"] and not report["size_mismatch"]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate AgentQuant external artifact paths and checksums.")
    parser.add_argument("--db", default=DB_PATH, help="Runtime agentquant.db path.")
    parser.add_argument("--start-date", default=None, help="Only validate artifacts from this trading date or later.")
    parser.add_argument("--end-date", default=None, help="Only validate artifacts through this trading date.")
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    args = parser.parse_args()
    report = validate_artifacts(args.db, start_date=args.start_date, end_date=args.end_date)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "artifact validation "
            f"{'ok' if report['ok'] else 'failed'}: "
            f"checked={report['checked']} "
            f"missing={len(report['missing'])} "
            f"hash_mismatch={len(report['hash_mismatch'])} "
            f"size_mismatch={len(report['size_mismatch'])}"
        )
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
