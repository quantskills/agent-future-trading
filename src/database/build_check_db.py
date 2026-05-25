from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from database.sqlite_setup import DB_PATH


DEFAULT_CHECK_DB = PROJECT_ROOT / os.getenv("CHECK_DB_PATH", "src/assets/agentquantcheck.db")


def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT name FROM src.sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _source_columns(cursor: sqlite3.Cursor, table_name: str) -> set[str]:
    cursor.execute(f"PRAGMA src.table_info({table_name})")
    return {str(row[1]) for row in cursor.fetchall()}


def _column_exprs(cursor: sqlite3.Cursor, source_table: str, columns: Iterable[str]) -> list[str]:
    existing = _source_columns(cursor, source_table)
    return [column if column in existing else f"NULL AS {column}" for column in columns]


def _create_from_source(cursor: sqlite3.Cursor, target_table: str, source_table: str, columns: Iterable[str]) -> None:
    if not _table_exists(cursor, source_table):
        return
    cursor.execute(f"DROP TABLE IF EXISTS {target_table}")
    select_columns = _column_exprs(cursor, source_table, columns)
    cursor.execute(
        f"""
        CREATE TABLE {target_table} AS
        SELECT {', '.join(select_columns)}
        FROM src.{source_table}
        """
    )


def rebuild_check_db(source_db: str | Path = DB_PATH, check_db: str | Path = DEFAULT_CHECK_DB) -> Path:
    source_path = Path(source_db)
    target_path = Path(check_db)
    if not source_path.exists():
        raise FileNotFoundError(f"source database not found: {source_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        target_path.unlink()

    conn = sqlite3.connect(target_path)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("ATTACH DATABASE ? AS src", (str(source_path),))

        _create_from_source(
            cursor,
            "check_config",
            "config",
            ["id", "exp_name", "updated_at", "tickers", "llm_model", "llm_provider"],
        )
        _create_from_source(
            cursor,
            "check_portfolio",
            "portfolio",
            [
                "id",
                "config_id",
                "trading_date",
                "cashflow",
                "total_assets",
                "margin_used",
                "available_cash",
                "daily_settlement_pnl",
                "leverage",
            ],
        )
        _create_from_source(
            cursor,
            "check_daily_settlement",
            "daily_settlement",
            [
                "portfolio_id",
                "trading_date",
                "previous_balance",
                "current_balance",
                "previous_margin",
                "current_margin",
                "daily_pnl",
                "commission",
                "margin_ratio",
                "is_warning",
                "is_liquidation",
                "created_at",
            ],
        )
        _create_from_source(
            cursor,
            "check_ticker_daily_pnl",
            "ticker_daily_pnl",
            [
                "portfolio_id",
                "trading_date",
                "ticker",
                "daily_pnl",
                "commission",
                "holding_pnl",
                "new_position_pnl",
                "close_pnl",
                "position_type",
                "lots",
                "entry_price",
                "settle_price",
                "created_at",
            ],
        )
        _create_from_source(
            cursor,
            "check_transactions",
            "futures_transactions",
            [
                "id",
                "config_id",
                "recommendation_id",
                "trading_date",
                "ticker",
                "contract_code",
                "action",
                "lots",
                "execution_price",
                "settle_price",
                "margin_used",
                "daily_pnl",
                "commission",
                "source_type",
                "execution_phase",
                "audit_payload_artifact_path",
                "audit_payload_sha256",
                "audit_payload_size",
                "audit_payload_summary_json",
                "llm_prompt_artifact_path",
                "llm_prompt_sha256",
                "llm_prompt_size",
                "llm_prompt_summary_json",
                "warning_message",
                "created_at",
            ],
        )
        if _table_exists(cursor, "futures_recommendation"):
            recommendation_columns = _source_columns(cursor, "futures_recommendation")

            def rec_col(column: str) -> str:
                return column if column in recommendation_columns else f"NULL AS {column}"

            justification_expr = (
                "substr(justification, 1, 800) AS justification_summary"
                if "justification" in recommendation_columns
                else "NULL AS justification_summary"
            )
            cursor.execute("DROP TABLE IF EXISTS check_recommendations")
            cursor.execute(
                f"""
                CREATE TABLE check_recommendations AS
                SELECT
                    {rec_col("id")}, {rec_col("config_id")}, {rec_col("reference_portfolio_id")},
                    {rec_col("trading_date")}, {rec_col("effective_trade_date")},
                    {rec_col("source_type")}, {rec_col("underlying_code")},
                    {rec_col("contract_code")}, {rec_col("action")}, {rec_col("lots")},
                    {rec_col("base_price")}, {rec_col("execution_price")},
                    {rec_col("status")}, {rec_col("warning_message")},
                    {justification_expr},
                    {rec_col("signal_snapshot_artifact_path")},
                    {rec_col("signal_snapshot_sha256")},
                    {rec_col("signal_snapshot_size")},
                    {rec_col("signal_snapshot_summary_json")},
                    {rec_col("audit_payload_artifact_path")},
                    {rec_col("audit_payload_sha256")},
                    {rec_col("audit_payload_size")},
                    {rec_col("audit_payload_summary_json")},
                    {rec_col("created_at")}
                FROM src.futures_recommendation
                """
            )
        _create_from_source(
            cursor,
            "check_strategy_memory",
            "strategy_memory",
            [
                "config_id",
                "ticker",
                "side",
                "signal_combo",
                "memory_state",
                "sample_count",
                "win_rate",
                "net_pnl",
                "avg_pnl",
                "confidence_score",
                "source",
                "reason",
                "updated_at",
                "valid_until",
            ],
        )
        _create_from_source(
            cursor,
            "check_adaptive_policy_state",
            "adaptive_policy_state",
            [
                "config_id",
                "ticker",
                "side",
                "signal_template",
                "horizon_class",
                "market_regime",
                "policy_type",
                "policy_action",
                "multiplier",
                "confidence_score",
                "sample_count",
                "reason",
                "created_at",
                "valid_until",
                "active",
            ],
        )
        _create_from_source(
            cursor,
            "check_capital_deployment_state",
            "capital_deployment_state",
            [
                "config_id",
                "trading_date",
                "capital_base",
                "current_margin",
                "current_margin_ratio",
                "target_margin_ratio_min",
                "target_margin_ratio_max",
                "underutilization_breach",
                "overutilization_breach",
                "margin_gap_to_min",
                "capital_allocation_tier",
                "reason_bucket",
                "created_at",
            ],
        )
        _create_from_source(
            cursor,
            "check_trading_day_phase",
            "trading_day_phase",
            ["config_id", "trading_date", "phase", "status", "started_at", "completed_at", "message"],
        )
        cursor.execute("DETACH DATABASE src")
        conn.commit()
    finally:
        conn.close()
    return target_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a lightweight read-only inspection copy of agentquant.db.")
    parser.add_argument("--source", default=DB_PATH, help="Runtime agentquant.db path.")
    parser.add_argument("--target", default=str(DEFAULT_CHECK_DB), help="Lightweight check DB output path.")
    args = parser.parse_args()
    path = rebuild_check_db(args.source, args.target)
    print(f"agentquantcheck.db rebuilt: {path}")


if __name__ == "__main__":
    main()
