import sqlite3
import json
import uuid
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from graph.schema import AnalystSignal
from database.interface import BaseDB
from database.artifact_store import (
    artifact_write_transaction,
    externalize_json_for_db,
    externalize_text_for_db,
    load_externalized_json,
)
from database.signal_artifact import build_signal_artifact_payload
from database.sqlite_setup import (
    DB_PATH,
    _ensure_columns,
    _ensure_reviewer_learning_schema,
    _json_artifact_columns,
    _text_artifact_columns,
)
from tools.common.contracts import (
    validate_auditor_artifact_boundary,
    validate_accountant_artifact_boundary,
    validate_execution_artifact_boundary,
    validate_pm_artifact_boundary,
)
from util.logger import logger
from tools.common.learning_contract import attach_or_upgrade_next_round_memory_contract
from tools.common.final_action_semantics import (
    canonical_action_family,
    canonical_action_value_lane,
)

class SQLiteDB(BaseDB):
    def __init__(self):
        self.db_path = DB_PATH
        self._runtime_schema_ready = False
        self._runtime_schema_lock = threading.Lock()
        self._phase1_write_connection = None

    @contextmanager
    def phase1_write_scope(self):
        """Commit Phase1 signal/recommendation writes and artifacts together."""
        if self._phase1_write_connection is not None:
            raise RuntimeError("nested_phase1_write_scope_not_supported")
        conn = self._get_connection()
        self._phase1_write_connection = conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            with artifact_write_transaction():
                yield
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            self._phase1_write_connection = None
            conn.close()

    def _phase1_or_new_connection(self):
        active = self._phase1_write_connection
        return (active, False) if active is not None else (self._get_connection(), True)

    @staticmethod
    def _validate_recommendation_audit_payload(audit_payload: Dict[str, Any]) -> None:
        if not isinstance(audit_payload, dict) or not audit_payload:
            return
        if any(
            key in audit_payload
            for key in ("execution_translation", "execution_result", "phase2_execution")
        ):
            validate_execution_artifact_boundary(audit_payload)
            return
        if str(audit_payload.get("producer") or audit_payload.get("agent_name") or "") == "auditor":
            validate_auditor_artifact_boundary(audit_payload)
            return
        validate_pm_artifact_boundary(audit_payload)

    def _get_connection(self):
        """Get a database connection with row factory."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row # access columns by name
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
        except Exception:
            pass
        if not self._runtime_schema_ready:
            with self._runtime_schema_lock:
                if self._runtime_schema_ready:
                    return conn
                try:
                    # Old local backtest DBs may miss newly added auxiliary tables.
                    # Patch them lazily on first connect so new controls actually participate.
                    cursor = conn.cursor()
                    self._ensure_strategy_memory_schema(cursor)
                    self._ensure_reviewer_learning_schema(cursor)
                    if self._table_exists(cursor, "strategy_memory"):
                        _ensure_columns(cursor, "strategy_memory", {"source_trading_date": "TEXT"})
                    if self._table_exists(cursor, "portfolio"):
                        _ensure_columns(
                            cursor,
                            "portfolio",
                            {
                                "account_equity": "REAL DEFAULT 0",
                                "cash_available": "REAL DEFAULT 0",
                                "margin_ratio": "REAL DEFAULT 0",
                                "risk_status": "TEXT DEFAULT 'NORMAL'",
                                "last_settle_date": "TEXT",
                                "is_settled": "INTEGER DEFAULT 0",
                            },
                        )
                    if self._table_exists(cursor, "daily_settlement"):
                        _ensure_columns(
                            cursor,
                            "daily_settlement",
                            {
                                "previous_account_equity": "REAL DEFAULT 0",
                                "current_account_equity": "REAL DEFAULT 0",
                                "cash_available": "REAL DEFAULT 0",
                                "reserved_margin": "REAL DEFAULT 0",
                            },
                        )
                    if self._table_exists(cursor, "signal"):
                        _ensure_columns(
                            cursor,
                            "signal",
                            {
                                "artifact_json": "TEXT",
                                "business_quality_score": "REAL DEFAULT 0",
                                "horizon_class": "TEXT DEFAULT 'unknown'",
                                "setup_type": "TEXT DEFAULT 'unknown'",
                            },
                        )
                    self._ensure_artifact_runtime_schema(cursor)
                    conn.commit()
                    self._runtime_schema_ready = True
                except Exception as exc:
                    logger.warning(f"Runtime schema bootstrap skipped: {exc}")
        return conn

    def _table_exists(self, cursor: sqlite3.Cursor, table_name: str) -> bool:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        )
        return cursor.fetchone() is not None

    def _ensure_artifact_runtime_schema(self, cursor: sqlite3.Cursor) -> None:
        if self._table_exists(cursor, "ticker_daily_pnl"):
            _ensure_columns(
                cursor,
                "ticker_daily_pnl",
                {
                    "holding_pnl": "REAL DEFAULT 0",
                    "new_position_pnl": "REAL DEFAULT 0",
                    "close_pnl": "REAL DEFAULT 0",
                },
            )
        if self._table_exists(cursor, "signal"):
            _ensure_columns(
                cursor,
                "signal",
                {
                    **_text_artifact_columns("llm_prompt"),
                    **_json_artifact_columns("artifact_json"),
                },
            )
        if self._table_exists(cursor, "futures_recommendation"):
            _ensure_columns(
                cursor,
                "futures_recommendation",
                {
                    **_json_artifact_columns("signal_snapshot"),
                    **_json_artifact_columns("audit_payload"),
                },
            )
        if self._table_exists(cursor, "futures_transactions"):
            _ensure_columns(
                cursor,
                "futures_transactions",
                {
                    **_json_artifact_columns("audit_payload"),
                    **_text_artifact_columns("llm_prompt"),
                },
            )
        if self._table_exists(cursor, "signal_context_history"):
            _ensure_columns(
                cursor,
                "signal_context_history",
                {
                    **_json_artifact_columns("analyst_signals"),
                    **_json_artifact_columns("market_confirmation"),
                    **_json_artifact_columns("final_action_contract"),
                },
            )
        if self._table_exists(cursor, "researcher_llm_notes"):
            _ensure_columns(
                cursor,
                "researcher_llm_notes",
                {
                    **_text_artifact_columns("raw_prompt"),
                    **_text_artifact_columns("raw_response"),
                    **_json_artifact_columns("payload"),
                },
            )
        if self._table_exists(cursor, "trade_episode_memory"):
            _ensure_columns(
                cursor,
                "trade_episode_memory",
                {
                    "episode_date": "TEXT",
                    "first_seen_at": "TEXT",
                    "last_reviewed_at": "TEXT",
                    **_json_artifact_columns("payload"),
                },
            )
        if self._table_exists(cursor, "learning_context_budget"):
            _ensure_columns(
                cursor,
                "learning_context_budget",
                {
                    "digest_count": "INTEGER DEFAULT 0",
                    "trade_episode_count": "INTEGER DEFAULT 0",
                    "hypothesis_count": "INTEGER DEFAULT 0",
                    "total_context_chars": "INTEGER DEFAULT 0",
                },
            )
        if self._table_exists(cursor, "exploratory_hypothesis"):
            _ensure_columns(
                cursor,
                "exploratory_hypothesis",
                {
                    **_json_artifact_columns("payload"),
                },
            )
        if self._table_exists(cursor, "no_trade_opportunity_memory"):
            _ensure_columns(
                cursor,
                "no_trade_opportunity_memory",
                {
                    **_json_artifact_columns("payload"),
                },
            )
        if self._table_exists(cursor, "alpha_setup_profile"):
            _ensure_columns(
                cursor,
                "alpha_setup_profile",
                {
                    "payload_json": "TEXT",
                    "active": "INTEGER DEFAULT 1",
                    "max_position_impact": "REAL DEFAULT 0",
                    "last_sample_date": "TEXT",
                    "profile_state_hint": "TEXT DEFAULT 'profile_watchlist'",
                },
            )
        if self._table_exists(cursor, "alpha_setup_sample"):
            _ensure_columns(cursor, "alpha_setup_sample", {"payload_json": "TEXT"})
        if self._table_exists(cursor, "alpha_setup_action_value"):
            _ensure_columns(
                cursor,
                "alpha_setup_action_value",
                {
                    "payload_json": "TEXT",
                    "active": "INTEGER DEFAULT 1",
                    "max_position_impact": "REAL DEFAULT 0",
                    "last_sample_date": "TEXT",
                    "action_preference": "TEXT DEFAULT ''",
                    "canonical_action_family": "TEXT DEFAULT ''",
                    "reward_source": "TEXT DEFAULT ''",
                    "evidence_scope": "TEXT DEFAULT ''",
                    "action_value_lane": "TEXT DEFAULT ''",
                    "consumer_scope": "TEXT DEFAULT 'pm_learning'",
                    "learning_lane": "TEXT DEFAULT ''",
                    "memory_side_role": "TEXT DEFAULT ''",
                    "retrieval_key": "TEXT DEFAULT ''",
                    "fallback_retrieval_key": "TEXT DEFAULT ''",
                    "execution_retrieval_key": "TEXT DEFAULT ''",
                },
            )
        if self._table_exists(cursor, "adaptive_policy_state"):
            _ensure_columns(
                cursor,
                "adaptive_policy_state",
                {
                    "source_trading_date": "TEXT",
                },
            )
        if self._table_exists(cursor, "setup_type_performance"):
            _ensure_columns(cursor, "setup_type_performance", {"last_sample_date": "TEXT"})
        if self._table_exists(cursor, "analyst_performance"):
            _ensure_columns(cursor, "analyst_performance", {"last_sample_date": "TEXT"})
        if self._table_exists(cursor, "provisional_policy_state"):
            _ensure_columns(cursor, "provisional_policy_state", {"source_trading_date": "TEXT"})

    def _model_to_dict(self, obj: Any) -> Dict[str, Any]:
        """Convert pydantic models / sqlite rows / dict-like objects into plain dicts."""
        if obj is None:
            return {}
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, sqlite3.Row):
            return dict(obj)
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        try:
            return dict(obj)
        except Exception:
            return obj.__dict__.copy() if hasattr(obj, "__dict__") else {}

    def _serialize_json(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    def _deserialize_json(self, value: Any) -> Any:
        return load_externalized_json(value)

    def _deserialize_external_json(self, record: Dict[str, Any], field_name: str, prefix: Optional[str] = None) -> Any:
        prefix = prefix or field_name
        return load_externalized_json(
            record.get(field_name),
            record.get(f"{prefix}_artifact_path"),
            record.get(f"{prefix}_sha256"),
        )

    def _normalize_db_value(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def _normalize_trading_day_value(self, value: Any) -> Optional[str]:
        """Normalize trading-day fields to YYYY-MM-DD instead of full timestamps."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d")

        value_str = str(value)
        if len(value_str) >= 10:
            return value_str[:10]
        return value_str

    def _enum_value(self, value: Any) -> Any:
        return value.value if hasattr(value, "value") else value

    def _row_to_portfolio_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        positions_raw = row["positions"] if "positions" in row.keys() else "{}"
        try:
            positions = json.loads(positions_raw) if positions_raw else {}
        except Exception:
            positions = {}

        margin_used = row["margin_used"] if "margin_used" in row.keys() else 0
        available_cash = (
            row["cash_available"]
            if "cash_available" in row.keys() and row["cash_available"] not in (None, 0)
            else row["available_cash"] if "available_cash" in row.keys() else row["cashflow"]
        )
        account_equity = (
            row["account_equity"]
            if "account_equity" in row.keys() and row["account_equity"] not in (None, 0)
            else float(row["cashflow"] or 0.0) + float(margin_used or 0.0)
        )
        margin_ratio = row["margin_ratio"] if "margin_ratio" in row.keys() and row["margin_ratio"] is not None else 0
        risk_status = row["risk_status"] if "risk_status" in row.keys() and row["risk_status"] else "NORMAL"
        last_settle_date = (
            self._normalize_trading_day_value(row["last_settle_date"])
            if "last_settle_date" in row.keys()
            else None
        )
        is_settled = bool(row["is_settled"]) if "is_settled" in row.keys() else False
        return {
            "id": row["id"],
            "config_id": row["config_id"] if "config_id" in row.keys() else None,
            "updated_at": row["updated_at"] if "updated_at" in row.keys() else None,
            "trading_date": self._normalize_trading_day_value(row["trading_date"]) if "trading_date" in row.keys() else None,
            "cashflow": row["cashflow"],
            "account_equity": account_equity,
            "cash_available": available_cash,
            "total_assets": row["total_assets"] if "total_assets" in row.keys() else row["cashflow"],
            "positions": positions,
            "margin_used": margin_used,
            "available_cash": available_cash,
            "margin_available": available_cash,
            "daily_settlement_pnl": row["daily_settlement_pnl"] if "daily_settlement_pnl" in row.keys() else 0,
            "margin_ratio": margin_ratio,
            "risk_status": risk_status,
            "last_settle_date": last_settle_date,
            "is_settled": is_settled,
            "leverage": row["leverage"] if "leverage" in row.keys() else 1.0,
        }

    def _ensure_strategy_memory_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS strategy_memory (
                id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                signal_combo TEXT NOT NULL DEFAULT '*',
                memory_state TEXT NOT NULL,
                sample_count INTEGER DEFAULT 0,
                win_rate REAL DEFAULT 0,
                net_pnl REAL DEFAULT 0,
                avg_pnl REAL DEFAULT 0,
                confidence_score REAL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'attribution_auto',
                reason TEXT,
                source_trading_date TEXT,
                updated_at TEXT NOT NULL,
                valid_until TEXT,
                payload_json TEXT,
                UNIQUE(config_id, ticker, side, signal_combo, source)
            )
            '''
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategy_memory_lookup "
            "ON strategy_memory(config_id, ticker, side, signal_combo)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategy_memory_state "
            "ON strategy_memory(config_id, memory_state)"
        )
        _ensure_columns(cursor, "strategy_memory", {"source_trading_date": "TEXT"})

    def _ensure_reviewer_learning_schema(self, cursor: sqlite3.Cursor) -> None:
        _ensure_reviewer_learning_schema(cursor)

    def _strategy_memory_signal_combo_key(self, signal_combo: Optional[List[str]]) -> str:
        normalized = self._normalize_signal_combo(signal_combo)
        if normalized is None:
            return "*"
        return json.dumps(list(normalized), ensure_ascii=False)

    def _strategy_memory_thresholds(self, memory_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        memory_config = memory_config or {}
        return {
            "expires_after_days": int(memory_config.get("expires_after_days", 30)),
            "min_samples_watchlist": int(memory_config.get("min_samples_watchlist", 2)),
            "min_samples_weak_block": int(memory_config.get("min_samples_weak_block", 4)),
            "min_samples_protected": int(memory_config.get("min_samples_protected", 3)),
            "protected_win_rate": float(memory_config.get("protected_win_rate", 0.60)),
            "protected_total_pnl": float(memory_config.get("protected_total_pnl", 1000)),
            "watchlist_win_rate_below": float(memory_config.get("watchlist_win_rate_below", 0.45)),
            "watchlist_total_pnl_below": float(memory_config.get("watchlist_total_pnl_below", -500)),
            "weak_block_win_rate_below": float(memory_config.get("weak_block_win_rate_below", 0.30)),
            "weak_block_total_pnl_below": float(memory_config.get("weak_block_total_pnl_below", -2500)),
        }

    def _classify_strategy_memory(
        self,
        summary: Dict[str, Any],
        memory_config: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, str]:
        thresholds = self._strategy_memory_thresholds(memory_config)
        sample_count = int(summary.get("total_trades") or 0)
        win_rate = float(summary.get("win_rate") or 0.0)
        net_pnl = float(summary.get("total_pnl") or 0.0)

        if sample_count >= thresholds["min_samples_weak_block"] and (
            win_rate <= thresholds["weak_block_win_rate_below"]
            or net_pnl <= thresholds["weak_block_total_pnl_below"]
        ):
            return "weak_block", "hard weak attribution"
        if sample_count >= thresholds["min_samples_watchlist"] and (
            win_rate <= thresholds["watchlist_win_rate_below"]
            or net_pnl <= thresholds["watchlist_total_pnl_below"]
        ):
            return "watchlist", "soft weak attribution"
        if sample_count >= thresholds["min_samples_protected"] and (
            win_rate >= thresholds["protected_win_rate"]
            and net_pnl >= thresholds["protected_total_pnl"]
        ):
            return "protected", "positive attribution"
        if sample_count >= 2 and win_rate >= 0.50 and net_pnl > 0:
            return "recovering", "positive but not yet protected"
        return "neutral", "insufficient edge"

    def _strategy_memory_confidence(self, summary: Dict[str, Any]) -> float:
        sample_count = int(summary.get("total_trades") or 0)
        win_rate = float(summary.get("win_rate") or 0.0)
        net_pnl = abs(float(summary.get("total_pnl") or 0.0))
        sample_score = min(0.45, sample_count / 10.0)
        win_score = min(0.30, abs(win_rate - 0.50) * 0.75)
        pnl_score = min(0.25, net_pnl / 20000.0)
        return min(1.0, sample_score + win_score + pnl_score)

    def _learning_retention_enabled(self, retention_config: Optional[Dict[str, Any]]) -> bool:
        return bool(
            retention_config
            and retention_config.get("enabled")
            and retention_config.get("run_after_researcher_learning", True)
        )

    def _table_columns(self, cursor: sqlite3.Cursor, table_name: str) -> set[str]:
        if not self._table_exists(cursor, table_name):
            return set()
        cursor.execute(f"PRAGMA table_info({table_name})")
        return {str(row[1]) for row in cursor.fetchall()}

    def _cleanup_learning_rows_by_date(
        self,
        cursor: sqlite3.Cursor,
        *,
        table_name: str,
        config_id: str,
        cutoff_date: str,
        date_columns: list[str],
    ) -> int:
        columns = self._table_columns(cursor, table_name)
        if not columns or "config_id" not in columns:
            return 0
        deleted = 0
        for column in date_columns:
            if column not in columns:
                continue
            cursor.execute(
                f"DELETE FROM {table_name} WHERE config_id = ? AND substr({column}, 1, 10) < ?",
                (config_id, cutoff_date),
            )
            deleted += max(0, cursor.rowcount or 0)
            break
        return deleted

    def _trim_learning_table_rows(
        self,
        cursor: sqlite3.Cursor,
        *,
        table_name: str,
        config_id: str,
        max_rows: int,
    ) -> int:
        if max_rows <= 0:
            return 0
        columns = self._table_columns(cursor, table_name)
        if not columns or "config_id" not in columns:
            return 0
        order_column = "trading_date" if "trading_date" in columns else "created_at" if "created_at" in columns else "rowid"
        cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE config_id = ?", (config_id,))
        row_count = int(cursor.fetchone()[0] or 0)
        if row_count <= max_rows:
            return 0
        cursor.execute(
            f"""
            DELETE FROM {table_name}
            WHERE rowid IN (
                SELECT rowid FROM {table_name}
                WHERE config_id = ?
                ORDER BY substr({order_column}, 1, 10) DESC, rowid DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (config_id, max_rows),
        )
        return max(0, cursor.rowcount or 0)

    def _cleanup_learning_retention_with_cursor(
        self,
        cursor: sqlite3.Cursor,
        *,
        config_id: str,
        trading_date,
        retention_config: Optional[Dict[str, Any]],
    ) -> Dict[str, int]:
        """Clean short-lived learning details after researcher learning without touching trade facts."""
        if not self._learning_retention_enabled(retention_config):
            return {}
        trading_day = self._normalize_trading_day_value(trading_date)
        try:
            base_dt = datetime.strptime(str(trading_day), "%Y-%m-%d")
        except Exception:
            logger.warning(f"Learning retention skipped: invalid trading_date={trading_date}")
            return {}

        detail_days = int(retention_config.get("detail_retention_days", 90) or 90)
        aggregate_days = int(retention_config.get("aggregate_retention_days", 180) or 180)
        detail_cutoff = (base_dt - timedelta(days=detail_days)).strftime("%Y-%m-%d")
        aggregate_cutoff = (base_dt - timedelta(days=aggregate_days)).strftime("%Y-%m-%d")
        max_detail_rows = int(retention_config.get("max_detail_rows_per_config", 50000) or 50000)

        detail_tables = retention_config.get("detail_tables") or [
            "research_position_feedback",
            "analyst_learning_digest",
            "learning_context_budget",
            "researcher_llm_notes",
            "learning_event_log",
            "config_learning_overlay",
            "alpha_setup_sample",
        ]
        aggregate_tables = retention_config.get("aggregate_tables") or [
            "alpha_setup_profile",
            "alpha_setup_action_value",
            "adaptive_policy_state",
        ]
        deleted: Dict[str, int] = {}
        for table_name in detail_tables:
            table = str(table_name)
            date_columns = ["trading_date", "valid_until", "created_at", "updated_at"]
            if table in {"analyst_learning_digest", "config_learning_overlay"}:
                date_columns = ["valid_until", "trading_date", "created_at", "updated_at"]
            count = self._cleanup_learning_rows_by_date(
                cursor,
                table_name=table,
                config_id=config_id,
                cutoff_date=detail_cutoff,
                date_columns=date_columns,
            )
            count += self._trim_learning_table_rows(
                cursor,
                table_name=table,
                config_id=config_id,
                max_rows=max_detail_rows,
            )
            if count:
                deleted[table] = count

        for table_name in aggregate_tables:
            table = str(table_name)
            columns = self._table_columns(cursor, table)
            if not columns or "config_id" not in columns:
                continue
            if "active" in columns and "valid_until" in columns:
                cursor.execute(
                    f"""
                    UPDATE {table}
                    SET active = 0
                    WHERE config_id = ?
                      AND active = 1
                      AND valid_until IS NOT NULL
                      AND substr(valid_until, 1, 10) < ?
                    """,
                    (config_id, trading_day),
                )
            date_column = "updated_at" if "updated_at" in columns else "created_at" if "created_at" in columns else None
            if date_column and "active" in columns:
                cursor.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE config_id = ?
                      AND active = 0
                      AND substr({date_column}, 1, 10) < ?
                    """,
                    (config_id, aggregate_cutoff),
                )
                if cursor.rowcount:
                    deleted[table] = deleted.get(table, 0) + max(0, cursor.rowcount or 0)
        if deleted:
            logger.info(
                f"Learning retention cleanup for {config_id[:8]} on {trading_day}: "
                f"detail_cutoff={detail_cutoff}, aggregate_cutoff={aggregate_cutoff}, deleted={deleted}"
            )
        return deleted

    def _refresh_strategy_memory_with_cursor(
        self,
        cursor: sqlite3.Cursor,
        *,
        config_id: str,
        trading_date,
        updated_at: Optional[str] = None,
        memory_config: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Refresh DB-backed strategy memory from completed pairs up to trading_date."""
        from collections import defaultdict
        from util.futures_trade_pairs import build_completed_trade_pairs, summarize_trade_pairs

        self._ensure_strategy_memory_schema(cursor)
        if memory_config is not None and not bool(memory_config.get("enabled", True)):
            return 0
        thresholds = self._strategy_memory_thresholds(memory_config)
        source = str((memory_config or {}).get("source") or "attribution_auto")
        trading_day_value = self._normalize_trading_day_value(trading_date)
        updated_at = updated_at or datetime.now(timezone.utc).isoformat()
        valid_until = (
            datetime.strptime(trading_day_value, "%Y-%m-%d")
            + timedelta(days=max(1, thresholds["expires_after_days"]))
        ).strftime("%Y-%m-%d")

        cursor.execute(
            '''
            SELECT *
            FROM futures_transactions
            WHERE config_id = ?
              AND substr(trading_date, 1, 10) <= ?
            ORDER BY substr(trading_date, 1, 10), created_at, id
            ''',
            (config_id, trading_day_value),
        )
        transactions = [dict(row) for row in cursor.fetchall()]
        pairs = [
            pair
            for pair in build_completed_trade_pairs(transactions, include_rollover=False)
            if str(pair.get("close_date") or "") <= trading_day_value
        ]

        recommendation_ids = [
            str(pair.get("open_recommendation_id"))
            for pair in pairs
            if pair.get("open_recommendation_id")
        ]
        recommendations_by_id: Dict[str, Dict[str, Any]] = {}
        if recommendation_ids:
            placeholders = ", ".join(["?"] * len(set(recommendation_ids)))
            cursor.execute(
                f'''
                SELECT *
                FROM futures_recommendation
                WHERE id IN ({placeholders})
                ''',
                tuple(sorted(set(recommendation_ids))),
            )
            recommendations_by_id = {str(row["id"]): dict(row) for row in cursor.fetchall()}

        grouped: Dict[tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
        for pair in pairs:
            ticker = str(pair.get("ticker") or "").upper()
            side = str(pair.get("side") or "").lower()
            if not ticker or side not in {"long", "short"}:
                continue
            recommendation = recommendations_by_id.get(str(pair.get("open_recommendation_id") or ""))
            snapshot = {}
            if recommendation:
                snapshot = self._deserialize_external_json(recommendation, "signal_snapshot") or {}
                if not isinstance(snapshot, dict):
                    snapshot = {}
            combo = list(self._signal_combo_from_snapshot(snapshot))
            item = dict(pair)
            item["signal_combo"] = combo
            grouped[(ticker, side, "*")].append(item)
            grouped[(ticker, side, self._strategy_memory_signal_combo_key(combo))].append(item)

        cursor.execute(
            "DELETE FROM strategy_memory WHERE config_id = ? AND source = ?",
            (config_id, source),
        )

        inserted = 0
        for (ticker, side, combo_key), rows in grouped.items():
            summary = summarize_trade_pairs(rows)
            state, reason = self._classify_strategy_memory(summary, memory_config)
            if state == "neutral":
                continue
            sample_count = int(summary.get("total_trades") or 0)
            payload = attach_or_upgrade_next_round_memory_contract(
                {
                    "ticker": ticker,
                    "side": side,
                    "signal_combo": "*" if combo_key == "*" else self._deserialize_json(combo_key),
                    "state": state,
                    "reason": reason,
                    "summary": summary,
                    "cutoff_trading_date": trading_day_value,
                    "source": source,
                },
                memory_type="strategy_memory",
                maturity_state=state,
                scope={
                    "ticker": ticker,
                    "side": side,
                    "setup_type": "*",
                    "horizon_class": "*",
                    "market_regime": "*",
                },
                usable_memory=[
                    f"state={state}; reason={reason}",
                    f"sample_count={sample_count}; win_rate={float(summary.get('win_rate') or 0.0):.2f}; net_pnl={float(summary.get('total_pnl') or 0.0):.0f}",
                ],
                analysis_strategy_updates=[
                    "Use as same ticker/side strategy memory; verify today's signal combo and data drivers before citing.",
                    "Treat wildcard signal-combo memory as weaker than exact combo memory.",
                ],
                trading_strategy_updates=[
                    "Protected/deployable memory can support sizing only through PM/Auditor and current confirmation.",
                    "Watchlist/weak memory can cap or reduce confidence but is not a permanent product ban.",
                ],
                pm_action_conditions=[
                    "If protected/recovering, require current market confirmation, explicit invalidation, and no weak conflicting same-scope memory before scaling.",
                    "If watchlist/weak_block, cap/probe/reduce unless current evidence explicitly repairs the same-scope weakness.",
                ],
                validation_plan=[
                    "Refresh from completed same-scope trade pairs after each settlement.",
                ],
                sample_count=sample_count,
                confidence_score=self._strategy_memory_confidence(summary),
            )
            cursor.execute(
                '''
                INSERT OR REPLACE INTO strategy_memory (
                    id, config_id, ticker, side, signal_combo, memory_state,
                    sample_count, win_rate, net_pnl, avg_pnl, confidence_score,
                    source, reason, source_trading_date, updated_at, valid_until, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    str(uuid.uuid4()),
                    config_id,
                    ticker,
                    side,
                    combo_key,
                    state,
                    sample_count,
                    float(summary.get("win_rate") or 0.0),
                    float(summary.get("total_pnl") or 0.0),
                    float(summary.get("avg_pnl") or 0.0),
                    self._strategy_memory_confidence(summary),
                    source,
                    reason,
                    trading_day_value,
                    updated_at,
                    valid_until,
                    self._serialize_json(payload),
                ),
            )
            inserted += 1

        return inserted


    def get_config(self, config_id: str) -> Optional[Dict]:
        """Get config by id."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT * FROM config WHERE id = ?', (config_id,))
            row = cursor.fetchone()
            
            if row:
                return row
            return None
        except Exception as e:
            logger.error(f"Error getting config: {e}")
            return None
        finally:
            if conn:
                conn.close()
            
    def get_config_id_by_name(self, exp_name: str) -> Optional[str]:
        """Get config id by experiment name."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT * FROM config WHERE exp_name = ?', (exp_name,))
            row = cursor.fetchone()
            
            if row:
                return row['id']
            return None
        except Exception as e:
            logger.error(f"Config not found: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def delete_config_and_portfolios(self, config_id: str) -> bool:
        """Delete a config and all its associated data (portfolios, decisions, signals, etc.).

        Args:
            config_id: The config ID to delete

        Returns:
            True if successful, False otherwise
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Get all portfolio IDs for this config
            cursor.execute('SELECT id FROM portfolio WHERE config_id = ?', (config_id,))
            portfolio_ids = [row['id'] for row in cursor.fetchall()]

            logger.info(f"Deleting {len(portfolio_ids)} portfolios for config {config_id[:8]}...")

            # Delete config-wide records first.
            try:
                cursor.execute('DELETE FROM futures_recommendation WHERE config_id = ?', (config_id,))
            except Exception:
                pass

            try:
                cursor.execute('DELETE FROM trading_day_phase WHERE config_id = ?', (config_id,))
            except Exception:
                pass

            try:
                cursor.execute('DELETE FROM futures_transactions WHERE config_id = ?', (config_id,))
            except Exception:
                pass

            try:
                cursor.execute('DELETE FROM futures_intraday_decision WHERE config_id = ?', (config_id,))
            except Exception:
                pass

            try:
                cursor.execute('DELETE FROM strategy_memory WHERE config_id = ?', (config_id,))
            except Exception:
                pass

            for table_name in (
                "strategy_memory_history",
                "signal_context_history",
                "setup_type_performance",
                "analyst_performance",
                "adaptive_policy_state",
                "research_position_feedback",
                "alpha_setup_profile",
                "alpha_setup_sample",
                "alpha_setup_action_value",
                "capital_deployment_state",
                "config_learning_overlay",
                "researcher_llm_notes",
                "causal_review_candidate",
                "provisional_policy_state",
                "analyst_learning_digest",
                "learning_event_log",
                "learning_context_budget",
            ):
                try:
                    cursor.execute(f"DELETE FROM {table_name} WHERE config_id = ?", (config_id,))
                except Exception:
                    pass

            # Delete in correct order (respect foreign key dependencies)
            for portfolio_id in portfolio_ids:
                # Delete futures transactions
                cursor.execute('DELETE FROM futures_transactions WHERE portfolio_id = ?', (portfolio_id,))
                # Delete daily settlement
                cursor.execute('DELETE FROM daily_settlement WHERE portfolio_id = ?', (portfolio_id,))
                # Delete ticker daily pnl
                try:
                    cursor.execute('DELETE FROM ticker_daily_pnl WHERE portfolio_id = ?', (portfolio_id,))
                except Exception:
                    pass
                # Delete signals
                cursor.execute('DELETE FROM signal WHERE portfolio_id = ?', (portfolio_id,))
                # Delete portfolio
                cursor.execute('DELETE FROM portfolio WHERE id = ?', (portfolio_id,))

            # Delete evaluation results for this config (check if tables exist first)
            try:
                cursor.execute('DELETE FROM config_evaluation WHERE config_id = ?', (config_id,))
            except Exception:
                pass  # Table may not exist, ignore

            try:
                cursor.execute('DELETE FROM config_outcome WHERE config_id = ?', (config_id,))
            except Exception:
                pass  # Table may not exist, ignore

            # Delete the config itself
            cursor.execute('DELETE FROM config WHERE id = ?', (config_id,))

            conn.commit()
            logger.info(f"Successfully deleted config {config_id[:8]}... and all associated data")
            return True

        except Exception as e:
            logger.error(f"Error deleting config and portfolios: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
        finally:
            if conn:
                conn.close()

    def create_config(self, config: Dict) -> Optional[str]:
        """Create a new config entry or return existing config_id if exp_name already exists."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # 棣栧厛妫€鏌?exp_name 鏄惁宸插瓨鍦?
            cursor.execute('SELECT id FROM config WHERE exp_name = ?', (config['exp_name'],))
            existing = cursor.fetchone()

            if existing:
                existing_id = existing['id']
                logger.info(f"Config for exp_name '{config['exp_name']}' already exists: {existing_id[:8]}...")
                logger.info(f"Returning existing config_id instead of creating duplicate")
                return existing_id

            # 涓嶅瓨鍦ㄥ垯鍒涘缓鏂伴厤缃?
            config_id = str(uuid.uuid4())
            logger.info(f"Creating config with id: {config_id[:8]}... for exp_name: {config['exp_name']}")

            cursor.execute('''
                INSERT INTO config (id, exp_name, updated_at, tickers, has_planner, llm_model, llm_provider)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                config_id,
                config["exp_name"],
                datetime.now(timezone.utc).isoformat(), # UTC time
                json.dumps(config["tickers"]),
                config["planner_mode"],
                config["llm"]["model"],
                config["llm"]["provider"]
            ))

            conn.commit()
            logger.info(f"Config {config_id[:8]}... created successfully")
            return config_id
        except Exception as e:
            logger.error(f"Error creating config: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
        finally:
            if conn:
                conn.close()

    def sync_config_runtime_metadata(self, config_id: str, config: Dict) -> bool:
        """Keep reusable config rows aligned with current non-state runtime metadata."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE config
                SET updated_at = ?,
                    tickers = ?,
                    has_planner = ?,
                    llm_model = ?,
                    llm_provider = ?
                WHERE id = ?
                ''',
                (
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(config["tickers"]),
                    config["planner_mode"],
                    config["llm"]["model"],
                    config["llm"]["provider"],
                    config_id,
                ),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.warning(f"Config runtime metadata sync skipped for {config_id[:8]}...: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def get_latest_trading_date(self, config_id: str) -> Optional[datetime]:
        """Get the latest trading date for a config."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT p.trading_date FROM portfolio p
                WHERE config_id = ? AND trading_date IS NOT NULL
                  AND (
                    COALESCE(p.is_settled, 0) = 1
                    OR EXISTS (SELECT 1 FROM daily_settlement ds WHERE ds.portfolio_id = p.id)
                  )
                ORDER BY substr(trading_date, 1, 10) DESC, updated_at DESC 
                LIMIT 1
            ''', (config_id,))
            
            row = cursor.fetchone()
            
            if row:
                return datetime.fromisoformat(self._normalize_trading_day_value(row['trading_date']))
            return None
        except Exception as e:
            logger.error(f"Error getting latest trading date: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def get_latest_settled_portfolio(self, config_id: str) -> Optional[Dict]:
        """Get the latest settled portfolio for a config."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT p.* FROM portfolio p
                WHERE config_id = ? AND trading_date IS NOT NULL
                  AND (
                    COALESCE(p.is_settled, 0) = 1
                    OR EXISTS (SELECT 1 FROM daily_settlement ds WHERE ds.portfolio_id = p.id)
                  )
                ORDER BY substr(trading_date, 1, 10) DESC, updated_at DESC 
                LIMIT 1
            ''', (config_id,))
            
            row = cursor.fetchone()
            
            if row:
                return self._row_to_portfolio_dict(row)
            return None
        except Exception as e:
            logger.error(f"Portfolio not found: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def get_latest_portfolio(self, config_id: str) -> Optional[Dict]:
        """Compatibility wrapper for legacy callers."""
        return self.get_latest_settled_portfolio(config_id)

    def create_portfolio(self, config_id: str, cashflow: float, trading_date: datetime) -> Optional[Dict]:
        """Create a new portfolio."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            trading_day_value = self._normalize_trading_day_value(trading_date)

            portfolio_id = str(uuid.uuid4())
            logger.info(f"Creating portfolio with id: {portfolio_id[:8]}... for config: {config_id[:8]}...")

            # 淇锛氭坊鍔犳湡璐т笓鐢ㄥ瓧娈?
            cursor.execute('''
                INSERT INTO portfolio (id, config_id, updated_at, trading_date, cashflow,
                                      account_equity, cash_available, total_assets, positions,
                                      margin_used, available_cash, daily_settlement_pnl,
                                      margin_ratio, risk_status, last_settle_date, is_settled, leverage)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                portfolio_id,
                config_id,
                datetime.now(timezone.utc).isoformat(), # UTC time
                trading_day_value,
                cashflow,
                cashflow,
                cashflow,
                cashflow,
                json.dumps({}),
                0,  # margin_used
                cashflow,  # available_cash
                0,  # daily_settlement_pnl
                0,  # margin_ratio
                "NORMAL",  # risk_status
                trading_day_value,  # last_settle_date
                1,  # is_settled
                1.0  # leverage
            ))

            conn.commit()
            logger.info(f"Portfolio {portfolio_id[:8]}... created successfully")
            return {
                'id': portfolio_id,
                'cashflow': cashflow,
                'account_equity': cashflow,
                'cash_available': cashflow,
                'total_assets': cashflow,
                'positions': {},
                'margin_used': 0,
                'available_cash': cashflow,
                'daily_settlement_pnl': 0,
                'margin_ratio': 0,
                'risk_status': "NORMAL",
                'last_settle_date': trading_day_value,
                'is_settled': True,
                'leverage': 1.0
            }
        except Exception as e:
            logger.error(f"Error creating portfolio: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
        finally:
            if conn:
                conn.close()

    def copy_portfolio(self, config_id: str, portfolio: Dict, trading_date: datetime) -> Optional[Dict]:
        """Copy a portfolio."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            trading_day_value = self._normalize_trading_day_value(trading_date)

            portfolio_id = str(uuid.uuid4())
            total_assets = portfolio['cashflow'] + sum(position['value'] for position in portfolio['positions'].values())
            logger.info(f"Copying portfolio {portfolio['id'][:8]}... -> new id: {portfolio_id[:8]}...")

            # 淇锛氭坊鍔犳湡璐т笓鐢ㄥ瓧娈?
            margin_used = portfolio.get('margin_used', 0)
            available_cash = portfolio.get(
                'cash_available',
                portfolio.get('margin_available', portfolio['cashflow'])
            )
            account_equity = portfolio.get('account_equity', portfolio['cashflow'] + margin_used)
            daily_pnl = portfolio.get('daily_settlement_pnl', 0)
            leverage = portfolio.get('leverage', 1.0)
            margin_ratio = portfolio.get('margin_ratio', (margin_used / account_equity if account_equity else 0.0))
            risk_status = portfolio.get('risk_status', "NORMAL")
            last_settle_date = self._normalize_trading_day_value(portfolio.get('last_settle_date') or trading_day_value)
            is_settled = 1 if portfolio.get('is_settled', False) else 0

            cursor.execute('''
                INSERT INTO portfolio (id, config_id, updated_at, trading_date, cashflow,
                                      account_equity, cash_available, total_assets, positions,
                                      margin_used, available_cash, daily_settlement_pnl,
                                      margin_ratio, risk_status, last_settle_date, is_settled, leverage)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                portfolio_id,
                config_id,
                datetime.now(timezone.utc).isoformat(), # UTC time
                trading_day_value,
                portfolio['cashflow'],
                account_equity,
                available_cash,
                total_assets,
                json.dumps(portfolio['positions']),
                margin_used,
                available_cash,
                daily_pnl,
                margin_ratio,
                risk_status,
                last_settle_date,
                is_settled,
                leverage
            ))

            conn.commit()
            logger.info(f"Portfolio {portfolio_id[:8]}... copied successfully")
            return {
                'id': portfolio_id,
                'cashflow': portfolio['cashflow'],
                'account_equity': account_equity,
                'cash_available': available_cash,
                'positions': portfolio['positions'],
                'margin_used': margin_used,
                'margin_available': available_cash,
                'daily_settlement_pnl': daily_pnl,
                'margin_ratio': margin_ratio,
                'risk_status': risk_status,
                'last_settle_date': last_settle_date,
                'is_settled': bool(is_settled),
                'leverage': leverage
            }
        except Exception as e:
            logger.error(f"Error copying portfolio: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
        finally:
            if conn:
                conn.close()

    def get_or_create_portfolio_for_date(self, config_id: str, portfolio: Dict, trading_date: datetime) -> Optional[Dict]:
        """
        Get existing portfolio for the given trading date, or create a new one based on the latest portfolio.

        This method prevents duplicate portfolio records for the same trading date.
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            trading_day_value = self._normalize_trading_day_value(trading_date)

            # 棣栧厛妫€鏌ユ槸鍚﹀凡瀛樺湪璇ヤ氦鏄撴棩鏈熺殑 portfolio 璁板綍
            cursor.execute('''
                SELECT id, cashflow, positions FROM portfolio
                WHERE config_id = ? AND substr(trading_date, 1, 10) = ?
                ORDER BY updated_at DESC
                LIMIT 1
            ''', (config_id, trading_day_value))

            existing_row = cursor.fetchone()
            if existing_row:
                # 宸插瓨鍦ㄨ鏃ユ湡鐨勮褰曪紝杩斿洖瀹冪敤浜庢洿鏂?
                logger.info(f"Found existing portfolio {existing_row['id'][:8]}... for trading date {trading_day_value}")
                margin_used = portfolio.get('margin_used', 0)
                available_cash = portfolio.get(
                    'cash_available',
                    portfolio.get('margin_available', portfolio.get('cashflow', existing_row['cashflow']))
                )
                account_equity = portfolio.get('account_equity', portfolio.get('cashflow', existing_row['cashflow']) + margin_used)
                margin_ratio = portfolio.get('margin_ratio', (margin_used / account_equity if account_equity else 0.0))
                risk_status = portfolio.get('risk_status', "NORMAL")
                last_settle_date = self._normalize_trading_day_value(portfolio.get('last_settle_date') or trading_day_value)
                is_settled = bool(portfolio.get('is_settled', False))
                return {
                    'id': existing_row['id'],
                    'cashflow': existing_row['cashflow'],
                    'account_equity': account_equity,
                    'cash_available': available_cash,
                    'margin_used': margin_used,
                    'margin_available': available_cash,
                    'margin_ratio': margin_ratio,
                    'risk_status': risk_status,
                    'last_settle_date': last_settle_date,
                    'is_settled': is_settled,
                    'positions': json.loads(existing_row['positions']) if existing_row['positions'] else {}
                }

            # 涓嶅瓨鍦紝鍒欏垱寤烘柊鐨?portfolio 璁板綍
            portfolio_id = str(uuid.uuid4())
            total_assets = portfolio['cashflow'] + sum(position['value'] for position in portfolio['positions'].values())
            margin_used = portfolio.get('margin_used', 0)
            available_cash = portfolio.get(
                'cash_available',
                portfolio.get('margin_available', portfolio['cashflow'])
            )
            account_equity = portfolio.get('account_equity', portfolio['cashflow'] + margin_used)
            margin_ratio = portfolio.get('margin_ratio', (margin_used / account_equity if account_equity else 0.0))
            risk_status = portfolio.get('risk_status', "NORMAL")
            last_settle_date = self._normalize_trading_day_value(portfolio.get('last_settle_date') or trading_day_value)
            is_settled = 1 if portfolio.get('is_settled', False) else 0
            logger.info(f"Creating new portfolio {portfolio_id[:8]}... for trading date {trading_day_value}")

            cursor.execute('''
                INSERT INTO portfolio (id, config_id, updated_at, trading_date, cashflow,
                                      account_equity, cash_available, total_assets, positions,
                                      margin_used, available_cash, margin_ratio, risk_status,
                                      last_settle_date, is_settled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                portfolio_id,
                config_id,
                datetime.now(timezone.utc).isoformat(), # UTC time
                trading_day_value,
                portfolio['cashflow'],
                account_equity,
                available_cash,
                total_assets,
                json.dumps(portfolio['positions']),
                margin_used,
                available_cash,
                margin_ratio,
                risk_status,
                last_settle_date,
                is_settled
            ))

            conn.commit()
            logger.info(f"Portfolio {portfolio_id[:8]}... created successfully")
            return {
                'id': portfolio_id,
                'cashflow': portfolio['cashflow'],
                'account_equity': account_equity,
                'cash_available': available_cash,
                'positions': portfolio['positions'],
                'margin_used': margin_used,
                'margin_available': available_cash,
                'margin_ratio': margin_ratio,
                'risk_status': risk_status,
                'last_settle_date': last_settle_date,
                'is_settled': bool(is_settled),
            }
        except Exception as e:
            logger.error(f"Error in get_or_create_portfolio_for_date: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
        finally:
            if conn:
                conn.close()

    def update_portfolio(self, config_id: str, portfolio: Dict, trading_date: datetime) -> bool:
        """update portfolio."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            trading_day_value = self._normalize_trading_day_value(trading_date)
            total_assets = portfolio['cashflow'] + sum(position['value'] for position in portfolio['positions'].values())

            # 淇锛氭坊鍔犳湡璐т笓鐢ㄥ瓧娈电殑鏇存柊
            # 浠巔ortfolio瀛楀吀涓幏鍙栨湡璐у瓧娈碉紙濡傛灉瀛樺湪锛?
            margin_used = portfolio.get('margin_used', 0)
            available_cash = portfolio.get(
                'cash_available',
                portfolio.get('margin_available', portfolio['cashflow'])
            )
            account_equity = portfolio.get('account_equity', portfolio['cashflow'] + margin_used)
            daily_pnl = portfolio.get('daily_settlement_pnl', 0)
            leverage = portfolio.get('leverage', 1.0)
            margin_ratio = portfolio.get('margin_ratio', (margin_used / account_equity if account_equity else 0.0))
            risk_status = portfolio.get('risk_status', "NORMAL")
            last_settle_date = self._normalize_trading_day_value(portfolio.get('last_settle_date') or trading_day_value)
            is_settled = 1 if portfolio.get('is_settled', False) else 0

            cursor.execute('''
                UPDATE portfolio
                SET config_id = ?, updated_at = ?, trading_date = ?, cashflow = ?,
                    account_equity = ?, cash_available = ?, total_assets = ?, positions = ?,
                    margin_used = ?, available_cash = ?, daily_settlement_pnl = ?,
                    margin_ratio = ?, risk_status = ?, last_settle_date = ?, is_settled = ?, leverage = ?
                WHERE id = ?
            ''', (
                config_id,
                datetime.now(timezone.utc).isoformat(), # UTC time
                trading_day_value,
                portfolio['cashflow'],
                account_equity,
                available_cash,
                total_assets,
                json.dumps(portfolio['positions']),
                margin_used,
                available_cash,
                daily_pnl,
                margin_ratio,
                risk_status,
                last_settle_date,
                is_settled,
                leverage,
                portfolio['id']
            ))

            if cursor.rowcount == 0:
                logger.warning(f"No portfolio found with id {portfolio['id']} to update")
                conn.commit()
                return False

            conn.commit()
            logger.info(f"Successfully updated portfolio {portfolio['id'][:8]}...")
            return True
        except Exception as e:
            logger.error(f"Error updating portfolio: {e}")
            return False
        finally:
            if conn:
                conn.close()
        
    def save_signal(self, portfolio_id: str, analyst: str, ticker: str, signal: AnalystSignal) -> Optional[str]:
        """Save the final signal for one portfolio/ticker/analyst scope."""
        conn = None
        try:
            analyst_key = getattr(analyst, "value", analyst)
            analyst_key = str(analyst_key)
            ticker_key = str(ticker).upper()
            conn, owns_connection = self._phase1_or_new_connection()
            cursor = conn.cursor()
            
            signal_id = str(uuid.uuid4())
            cursor.execute("SELECT config_id, trading_date FROM portfolio WHERE id = ?", (portfolio_id,))
            portfolio_row = cursor.fetchone()
            config_id = portfolio_row["config_id"] if portfolio_row and "config_id" in portfolio_row.keys() else None
            trading_date = (
                self._normalize_trading_day_value(portfolio_row["trading_date"])
                if portfolio_row and "trading_date" in portfolio_row.keys()
                else None
            )
            artifact_payload = build_signal_artifact_payload(signal)
            artifact_ext = externalize_json_for_db(
                artifact_payload,
                category="signal",
                record_id=signal_id,
                field_name="artifact_json",
                config_id=config_id,
                trading_date=trading_date,
            )
            cursor.execute(
                '''
                DELETE FROM signal
                WHERE portfolio_id = ?
                  AND ticker = ?
                  AND analyst = ?
                ''',
                (portfolio_id, ticker_key, analyst_key),
            )
            cursor.execute('''
                INSERT INTO signal (id, portfolio_id, updated_at, ticker, llm_prompt,
                                  analyst, signal, justification, artifact_json,
                                  business_quality_score, horizon_class, setup_type,
                                  llm_prompt_artifact_path, llm_prompt_sha256,
                                  llm_prompt_size, llm_prompt_summary_json,
                                  artifact_json_artifact_path, artifact_json_sha256,
                                  artifact_json_size, artifact_json_summary_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id,
                portfolio_id,
                datetime.now(timezone.utc).isoformat(), # UTC time 
                ticker_key,
                "",
                analyst_key,
                str(signal.signal),
                "",
                artifact_ext.inline_value,
                float(getattr(signal, "business_quality_score", 0.0) or 0.0),
                str(getattr(signal, "horizon_class", "unknown") or "unknown"),
                str(getattr(signal, "setup_type", "unknown") or "unknown"),
                None,
                None,
                0,
                None,
                artifact_ext.artifact_path,
                artifact_ext.sha256,
                artifact_ext.size_bytes,
                artifact_ext.summary_json,
            ))
            
            if owns_connection:
                conn.commit()
            return signal_id
        except Exception:
            logger.error("signal_persistence_failed")
            return None
        finally:
            if conn and locals().get("owns_connection", True):
                conn.close()

    def get_signal_persistence_counts(
        self,
        portfolio_id: str,
        tickers: Optional[List[str]] = None,
        analysts: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Return signal persistence counts for one Phase1 portfolio snapshot."""
        conn = None
        try:
            conn, owns_connection = self._phase1_or_new_connection()
            cursor = conn.cursor()
            params: List[Any] = [portfolio_id]
            filters = ["portfolio_id = ?"]
            if tickers:
                ticker_values = [str(ticker).upper() for ticker in tickers]
                filters.append(f"ticker IN ({','.join('?' for _ in ticker_values)})")
                params.extend(ticker_values)
            if analysts:
                analyst_values = [str(analyst) for analyst in analysts]
                filters.append(f"analyst IN ({','.join('?' for _ in analyst_values)})")
                params.extend(analyst_values)

            where = " AND ".join(filters)
            cursor.execute(
                f"""
                SELECT ticker, analyst, COUNT(*) AS row_count
                FROM signal
                WHERE {where}
                GROUP BY ticker, analyst
                ORDER BY ticker, analyst
                """,
                tuple(params),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            return {
                "rows": rows,
                "distinct_pairs": len(rows),
                "row_total": sum(int(row.get("row_count") or 0) for row in rows),
                "duplicate_pairs": [
                    f"{row.get('ticker')}:{row.get('analyst')}={int(row.get('row_count') or 0)}"
                    for row in rows
                    if int(row.get("row_count") or 0) > 1
                ],
            }
        except Exception as exc:
            logger.error(f"Error reading signal persistence counts: {exc}")
            return {"rows": [], "distinct_pairs": 0, "row_total": 0, "duplicate_pairs": [], "error": str(exc)}
        finally:
            if conn and locals().get("owns_connection", True):
                conn.close()

    # ==================== 鏈熻揣涓撶敤鏂规硶 ====================

    def save_futures_recommendation(self, recommendation: Any) -> Optional[str]:
        """Save a futures recommendation for later execution or audit."""
        conn = None
        try:
            recommendation_dict = self._model_to_dict(recommendation)
            validate_pm_artifact_boundary(recommendation_dict.get("signal_snapshot") or {})
            self._validate_recommendation_audit_payload(recommendation_dict.get("audit_payload") or {})
            recommendation_id = recommendation_dict.get("id") or str(uuid.uuid4())
            created_at = recommendation_dict.get("created_at") or datetime.now(timezone.utc).isoformat()
            config_id = recommendation_dict.get("config_id")
            trading_date = self._normalize_trading_day_value(recommendation_dict.get("trading_date"))
            effective_trade_date = self._normalize_trading_day_value(recommendation_dict.get("effective_trade_date"))
            artifact_date = effective_trade_date or trading_date
            snapshot_ext = externalize_json_for_db(
                recommendation_dict.get("signal_snapshot"),
                category="recommendation",
                record_id=recommendation_id,
                field_name="signal_snapshot",
                config_id=config_id,
                trading_date=artifact_date,
            )
            audit_ext = externalize_json_for_db(
                recommendation_dict.get("audit_payload"),
                category="recommendation",
                record_id=recommendation_id,
                field_name="audit_payload",
                config_id=config_id,
                trading_date=artifact_date,
            )

            conn, owns_connection = self._phase1_or_new_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO futures_recommendation (
                    id, config_id, reference_portfolio_id, trading_date, effective_trade_date,
                    source_type, underlying_code, from_contract, to_contract, contract_code,
                    action, lots, base_price, base_price_source, base_price_date,
                    open_price, prev_close_price, slippage_model, slippage_ticks,
                    slippage_amount, execution_price, justification, signal_snapshot,
                    audit_payload, warning_message, status, created_at,
                    signal_snapshot_artifact_path, signal_snapshot_sha256,
                    signal_snapshot_size, signal_snapshot_summary_json,
                    audit_payload_artifact_path, audit_payload_sha256,
                    audit_payload_size, audit_payload_summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    recommendation_id,
                    config_id,
                    recommendation_dict.get("reference_portfolio_id"),
                    trading_date,
                    effective_trade_date,
                    self._enum_value(recommendation_dict.get("source_type")),
                    recommendation_dict.get("underlying_code"),
                    recommendation_dict.get("from_contract"),
                    recommendation_dict.get("to_contract"),
                    recommendation_dict.get("contract_code"),
                    self._enum_value(recommendation_dict.get("action")),
                    recommendation_dict.get("lots", 0),
                    recommendation_dict.get("base_price"),
                    self._enum_value(recommendation_dict.get("base_price_source")),
                    self._normalize_db_value(recommendation_dict.get("base_price_date")),
                    recommendation_dict.get("open_price"),
                    recommendation_dict.get("prev_close_price"),
                    recommendation_dict.get("slippage_model"),
                    recommendation_dict.get("slippage_ticks", 0),
                    recommendation_dict.get("slippage_amount", 0),
                    recommendation_dict.get("execution_price"),
                    recommendation_dict.get("justification"),
                    snapshot_ext.inline_value,
                    audit_ext.inline_value,
                    recommendation_dict.get("warning_message"),
                    self._enum_value(recommendation_dict.get("status")),
                    created_at,
                    snapshot_ext.artifact_path,
                    snapshot_ext.sha256,
                    snapshot_ext.size_bytes,
                    snapshot_ext.summary_json,
                    audit_ext.artifact_path,
                    audit_ext.sha256,
                    audit_ext.size_bytes,
                    audit_ext.summary_json,
                ),
            )
            if owns_connection:
                conn.commit()
            return recommendation_id
        except Exception as e:
            logger.error(f"Error saving futures recommendation: {e}")
            return None
        finally:
            if conn and locals().get("owns_connection", True):
                conn.close()

    def get_futures_recommendations_by_effective_date(
        self,
        config_id: str,
        effective_trade_date,
        source_type: Optional[Any] = None,
        status: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Get futures recommendations by effective trading date."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            query = '''
                SELECT * FROM futures_recommendation
                WHERE config_id = ? AND effective_trade_date = ?
            '''
            params: List[Any] = [config_id, self._normalize_trading_day_value(effective_trade_date)]

            if source_type is not None:
                query += ' AND source_type = ?'
                params.append(self._enum_value(source_type))

            if status is not None:
                query += ' AND status = ?'
                params.append(self._enum_value(status))

            query += ' ORDER BY created_at ASC'
            cursor.execute(query, tuple(params))

            recommendations: List[Dict[str, Any]] = []
            for row in cursor.fetchall():
                record = dict(row)
                record["signal_snapshot"] = self._deserialize_external_json(record, "signal_snapshot")
                record["audit_payload"] = self._deserialize_external_json(record, "audit_payload")
                recommendations.append(record)
            return recommendations
        except Exception as e:
            logger.error(f"Error getting futures recommendations: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def update_futures_recommendation_status(
        self,
        recommendation_id: str,
        status: Any,
        action: Optional[Any] = None,
        lots: Optional[int] = None,
        execution_price: Optional[float] = None,
        warning_message: Optional[str] = None,
        signal_snapshot: Optional[Dict[str, Any]] = None,
        audit_payload: Optional[Dict[str, Any]] = None,
        base_price: Optional[float] = None,
        base_price_source: Optional[Any] = None,
        base_price_date: Optional[Any] = None,
        open_price: Optional[float] = None,
        prev_close_price: Optional[float] = None,
        slippage_model: Optional[str] = None,
        slippage_ticks: Optional[int] = None,
        slippage_amount: Optional[float] = None,
    ) -> bool:
        """Update futures recommendation execution status."""
        conn = None
        try:
            conn, owns_connection = self._phase1_or_new_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT config_id, trading_date, effective_trade_date FROM futures_recommendation WHERE id = ?",
                (recommendation_id,),
            )
            recommendation_row = cursor.fetchone()
            config_id = recommendation_row["config_id"] if recommendation_row and "config_id" in recommendation_row.keys() else None
            artifact_date = (
                self._normalize_trading_day_value(recommendation_row["effective_trade_date"])
                if recommendation_row and "effective_trade_date" in recommendation_row.keys()
                else None
            ) or (
                self._normalize_trading_day_value(recommendation_row["trading_date"])
                if recommendation_row and "trading_date" in recommendation_row.keys()
                else None
            )

            fields = ['status = ?']
            params: List[Any] = [self._enum_value(status)]

            if action is not None:
                fields.append('action = ?')
                params.append(self._enum_value(action))

            if lots is not None:
                fields.append('lots = ?')
                params.append(int(lots or 0))

            if execution_price is not None:
                fields.append('execution_price = ?')
                params.append(execution_price)

            if warning_message is not None:
                fields.append('warning_message = ?')
                params.append(warning_message)

            if signal_snapshot is not None:
                if any(
                    key in signal_snapshot
                    for key in ("execution_translation", "execution_result", "phase2_execution")
                ):
                    validate_execution_artifact_boundary(signal_snapshot)
                else:
                    validate_pm_artifact_boundary(signal_snapshot)
                snapshot_ext = externalize_json_for_db(
                    signal_snapshot,
                    category="recommendation",
                    record_id=recommendation_id,
                    field_name="signal_snapshot",
                    config_id=config_id,
                    trading_date=artifact_date,
                )
                fields.extend([
                    'signal_snapshot = ?',
                    'signal_snapshot_artifact_path = ?',
                    'signal_snapshot_sha256 = ?',
                    'signal_snapshot_size = ?',
                    'signal_snapshot_summary_json = ?',
                ])
                params.extend([
                    snapshot_ext.inline_value,
                    snapshot_ext.artifact_path,
                    snapshot_ext.sha256,
                    snapshot_ext.size_bytes,
                    snapshot_ext.summary_json,
                ])

            if audit_payload is not None:
                self._validate_recommendation_audit_payload(audit_payload)
                audit_ext = externalize_json_for_db(
                    audit_payload,
                    category="recommendation",
                    record_id=recommendation_id,
                    field_name="audit_payload",
                    config_id=config_id,
                    trading_date=artifact_date,
                )
                fields.extend([
                    'audit_payload = ?',
                    'audit_payload_artifact_path = ?',
                    'audit_payload_sha256 = ?',
                    'audit_payload_size = ?',
                    'audit_payload_summary_json = ?',
                ])
                params.extend([
                    audit_ext.inline_value,
                    audit_ext.artifact_path,
                    audit_ext.sha256,
                    audit_ext.size_bytes,
                    audit_ext.summary_json,
                ])

            if base_price is not None:
                fields.append('base_price = ?')
                params.append(base_price)

            if base_price_source is not None:
                fields.append('base_price_source = ?')
                params.append(self._enum_value(base_price_source))

            if base_price_date is not None:
                fields.append('base_price_date = ?')
                params.append(self._normalize_db_value(base_price_date))

            if open_price is not None:
                fields.append('open_price = ?')
                params.append(open_price)

            if prev_close_price is not None:
                fields.append('prev_close_price = ?')
                params.append(prev_close_price)

            if slippage_model is not None:
                fields.append('slippage_model = ?')
                params.append(slippage_model)

            if slippage_ticks is not None:
                fields.append('slippage_ticks = ?')
                params.append(slippage_ticks)

            if slippage_amount is not None:
                fields.append('slippage_amount = ?')
                params.append(slippage_amount)

            params.append(recommendation_id)
            cursor.execute(
                f"UPDATE futures_recommendation SET {', '.join(fields)} WHERE id = ?",
                tuple(params),
            )
            if owns_connection:
                conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating futures recommendation status: {e}")
            return False
        finally:
            if conn and locals().get("owns_connection", True):
                conn.close()

    def save_futures_intraday_decision(self, decision: Dict[str, Any]) -> Optional[str]:
        """Save one intraday execution-gate audit decision."""
        conn = None
        try:
            decision_id = decision.get("id") or str(uuid.uuid4())
            created_at = decision.get("created_at") or datetime.now(timezone.utc).isoformat()
            trading_date = self._normalize_trading_day_value(decision.get("trading_date"))
            slot_datetime = (
                decision.get("slot_datetime")
                or decision.get("base_datetime")
                or decision.get("cutoff_datetime")
                or trading_date
            )
            features = decision.get("features_json", decision.get("features"))

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT OR REPLACE INTO futures_intraday_decision (
                    id, config_id, trading_date, recommendation_id, ticker, contract_code,
                    slot_datetime, mode, cutoff_datetime, decision, trigger_reason,
                    base_price, execution_price_candidate, features_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    decision_id,
                    decision.get("config_id"),
                    trading_date,
                    decision.get("recommendation_id"),
                    decision.get("ticker"),
                    decision.get("contract_code"),
                    self._normalize_db_value(slot_datetime),
                    decision.get("mode"),
                    self._normalize_db_value(decision.get("cutoff_datetime")),
                    decision.get("decision"),
                    decision.get("trigger_reason"),
                    decision.get("base_price"),
                    decision.get("execution_price_candidate"),
                    self._serialize_json(features),
                    created_at,
                ),
            )
            conn.commit()
            return decision_id
        except Exception as e:
            logger.error(f"Error saving futures intraday decision: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def save_futures_transaction(self, transaction: Any) -> Optional[str]:
        """Save a futures transaction under the new execution schema."""
        transaction_dict = self._model_to_dict(transaction)
        if transaction_dict.get("llm_prompt") not in (None, ""):
            raise ValueError("transaction_raw_prompt_persistence_forbidden")
        conn = None
        try:
            validate_execution_artifact_boundary(transaction_dict.get("audit_payload") or {})
            transaction_id = transaction_dict.get("id") or str(uuid.uuid4())
            created_at = transaction_dict.get("created_at") or datetime.now(timezone.utc).isoformat()
            execution_price = transaction_dict.get("execution_price", transaction_dict.get("price"))
            lots = transaction_dict.get("lots", 0)
            contract_multiplier = transaction_dict.get("contract_multiplier", 0) or 0
            margin_rate = transaction_dict.get("margin_rate", 0) or 0
            execution_phase = self._enum_value(transaction_dict.get("execution_phase"))
            if not execution_phase:
                raise ValueError("save_futures_transaction requires an explicit execution_phase")
            margin_used = transaction_dict.get("margin_used")
            if margin_used is None and execution_price is not None:
                margin_used = execution_price * abs(lots) * contract_multiplier * margin_rate

            conn = self._get_connection()
            cursor = conn.cursor()

            config_id = transaction_dict.get("config_id")
            if not config_id and transaction_dict.get("portfolio_id"):
                cursor.execute('SELECT config_id FROM portfolio WHERE id = ?', (transaction_dict["portfolio_id"],))
                config_row = cursor.fetchone()
                config_id = config_row["config_id"] if config_row else None
            trading_date = self._normalize_trading_day_value(transaction_dict.get("trading_date"))
            audit_ext = externalize_json_for_db(
                transaction_dict.get("audit_payload"),
                category="transaction",
                record_id=transaction_id,
                field_name="audit_payload",
                config_id=config_id,
                trading_date=trading_date,
            )
            cursor.execute(
                '''
                INSERT INTO futures_transactions (
                    id, portfolio_id, config_id, recommendation_id, trading_date, ticker, contract_code,
                    action, lots, price, execution_price, settle_price, contract_multiplier, margin_rate,
                    margin_used, daily_pnl, commission, source_type, execution_phase,
                    execution_price_basis, base_price, base_price_source, base_price_date,
                    open_price, prev_close_price, slippage_model, slippage_ticks, slippage_amount,
                    released_margin, margin_delta, post_trade_margin_used, audit_payload,
                    audit_payload_artifact_path, audit_payload_sha256,
                    audit_payload_size, audit_payload_summary_json,
                    warning_message, justification, llm_prompt,
                    llm_prompt_artifact_path, llm_prompt_sha256,
                    llm_prompt_size, llm_prompt_summary_json,
                    booked_in_settlement, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    transaction_id,
                    transaction_dict.get("portfolio_id"),
                    config_id,
                    transaction_dict.get("recommendation_id"),
                    self._normalize_db_value(transaction_dict.get("trading_date")),
                    transaction_dict.get("ticker"),
                    transaction_dict.get("contract_code") or transaction_dict.get("ticker"),
                    self._enum_value(transaction_dict.get("action")),
                    lots,
                    execution_price,
                    execution_price,
                    transaction_dict.get("settle_price"),
                    contract_multiplier,
                    margin_rate,
                    margin_used if margin_used is not None else 0,
                    transaction_dict.get("daily_pnl", 0),
                    transaction_dict.get("commission", 0),
                    self._enum_value(transaction_dict.get("source_type", "strategy")),
                    execution_phase,
                    transaction_dict.get("execution_price_basis"),
                    transaction_dict.get("base_price"),
                    self._enum_value(transaction_dict.get("base_price_source")),
                    self._normalize_db_value(transaction_dict.get("base_price_date")),
                    transaction_dict.get("open_price"),
                    transaction_dict.get("prev_close_price"),
                    transaction_dict.get("slippage_model"),
                    transaction_dict.get("slippage_ticks", 0),
                    transaction_dict.get("slippage_amount", 0),
                    transaction_dict.get("released_margin"),
                    transaction_dict.get("margin_delta"),
                    transaction_dict.get("post_trade_margin_used"),
                    audit_ext.inline_value,
                    audit_ext.artifact_path,
                    audit_ext.sha256,
                    audit_ext.size_bytes,
                    audit_ext.summary_json,
                    transaction_dict.get("warning_message"),
                    transaction_dict.get("justification"),
                    "",
                    None,
                    None,
                    0,
                    None,
                    1 if transaction_dict.get("booked_in_settlement", False) else 0,
                    created_at,
                ),
            )
            conn.commit()
            return transaction_id
        except Exception as e:
            logger.error(f"Error saving futures transaction: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def get_futures_transactions_by_date(
        self,
        config_id: str,
        trading_date,
        execution_phase: Optional[Any] = None,
        booked_in_settlement: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Get futures transactions by config and trading date."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            normalized_trading_date = self._normalize_db_value(trading_date)
            query = '''
                SELECT * FROM futures_transactions
                WHERE config_id = ?
                  AND (trading_date = ? OR substr(trading_date, 1, 10) = substr(?, 1, 10))
            '''
            params: List[Any] = [config_id, normalized_trading_date, normalized_trading_date]

            if execution_phase is not None:
                query += ' AND execution_phase = ?'
                params.append(self._enum_value(execution_phase))

            if booked_in_settlement is not None:
                query += ' AND booked_in_settlement = ?'
                params.append(1 if booked_in_settlement else 0)

            query += ' ORDER BY created_at ASC'
            cursor.execute(query, tuple(params))
            transactions: List[Dict[str, Any]] = []
            for row in cursor.fetchall():
                record = dict(row)
                record["audit_payload"] = self._deserialize_external_json(record, "audit_payload")
                transactions.append(record)
            return transactions
        except Exception as e:
            logger.error(f"Error getting futures transactions: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def mark_futures_transactions_booked(self, transaction_ids: List[str]) -> bool:
        """Mark transactions as booked by phase2 settlement."""
        if not transaction_ids:
            return True

        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            placeholders = ', '.join(['?'] * len(transaction_ids))
            cursor.execute(
                f'UPDATE futures_transactions SET booked_in_settlement = 1 WHERE id IN ({placeholders})',
                tuple(transaction_ids),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error marking futures transactions as booked: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def update_futures_transactions_settle_prices(
        self,
        settle_price_updates: List[Dict[str, Any]],
    ) -> bool:
        """Backfill settle prices into futures transaction rows after settlement."""
        if not settle_price_updates:
            return True

        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            for update in settle_price_updates:
                transaction_id = update.get("id")
                settle_price = update.get("settle_price")
                if not transaction_id or settle_price is None:
                    continue
                cursor.execute(
                    'UPDATE futures_transactions SET settle_price = ? WHERE id = ?',
                    (settle_price, transaction_id),
                )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error updating futures transaction settle prices: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def start_trading_day_phase(
        self,
        config_id: str,
        trading_date,
        phase: Any,
        message: str = "",
    ) -> bool:
        """Create or restart a trading day phase record."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            phase_value = self._enum_value(phase)
            trading_date_value = self._normalize_db_value(trading_date)
            now = datetime.now(timezone.utc).isoformat()

            cursor.execute(
                '''
                SELECT id FROM trading_day_phase
                WHERE config_id = ? AND trading_date = ? AND phase = ?
                ''',
                (config_id, trading_date_value, phase_value),
            )
            existing = cursor.fetchone()

            if existing:
                cursor.execute(
                    '''
                    UPDATE trading_day_phase
                    SET status = ?, started_at = ?, completed_at = NULL, message = ?
                    WHERE id = ?
                    ''',
                    ("running", now, message, existing["id"]),
                )
            else:
                cursor.execute(
                    '''
                    INSERT INTO trading_day_phase (
                        id, config_id, trading_date, phase, status, started_at, completed_at, message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (str(uuid.uuid4()), config_id, trading_date_value, phase_value, "running", now, None, message),
                )

            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error starting trading day phase: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def complete_trading_day_phase(
        self,
        config_id: str,
        trading_date,
        phase: Any,
        status: Any,
        message: str = "",
    ) -> bool:
        """Complete a trading day phase record.

        Phase completion is a status write only. Research memory refresh and
        learning retention are handled by the explicit researcher learning
        entrypoint, not by Phase4 reviewer validation.
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            phase_value = self._enum_value(phase)
            trading_date_value = self._normalize_db_value(trading_date)
            status_value = self._enum_value(status)
            now = datetime.now(timezone.utc).isoformat()

            cursor.execute(
                '''
                UPDATE trading_day_phase
                SET status = ?, completed_at = ?, message = ?
                WHERE config_id = ? AND trading_date = ? AND phase = ?
                ''',
                (status_value, now, message, config_id, trading_date_value, phase_value),
            )

            if cursor.rowcount == 0:
                cursor.execute(
                    '''
                    INSERT INTO trading_day_phase (
                        id, config_id, trading_date, phase, status, started_at, completed_at, message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (str(uuid.uuid4()), config_id, trading_date_value, phase_value, status_value, now, now, message),
                )

            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error completing trading day phase: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def get_trading_day_phase(
        self,
        config_id: str,
        trading_date,
        phase: Any,
    ) -> Optional[Dict[str, Any]]:
        """Get a trading day phase record."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT * FROM trading_day_phase
                WHERE config_id = ? AND trading_date = ? AND phase = ?
                ''',
                (config_id, self._normalize_db_value(trading_date), self._enum_value(phase)),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting trading day phase: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def get_futures_transaction_memory(
        self,
        config_id: str,
        ticker: str,
        limit: int = 20,
        trading_date=None,
    ) -> List[str]:
        """Get recent transaction memory for futures PM prompts."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            params: List[Any] = [config_id, ticker]
            where = ["config_id = ?", "ticker = ?"]
            trading_day_value = self._normalize_trading_day_value(trading_date)
            if trading_day_value:
                where.append("substr(trading_date, 1, 10) < ?")
                params.append(trading_day_value)
            cursor.execute(
                f'''
                SELECT trading_date, action, lots, COALESCE(execution_price, price) AS execution_price
                FROM futures_transactions
                WHERE {' AND '.join(where)}
                ORDER BY trading_date DESC, created_at DESC
                LIMIT ?
                ''',
                tuple(params + [int(limit)]),
            )

            memory = []
            for row in cursor.fetchall():
                memory.append(f"{row['trading_date']} {row['action']} {row['lots']}@{row['execution_price']}")
            return memory
        except Exception:
            logger.warning("futures_transaction_memory_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def get_signal_history(
        self,
        config_id: str,
        ticker: str,
        trading_date,
        lookback_days: int,
    ) -> List[Dict[str, Any]]:
        """Return recent signal history for one ticker using a trading-date window."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            trading_day_value = self._normalize_trading_day_value(trading_date)
            cursor.execute(
                '''
                SELECT s.analyst, s.signal, substr(p.trading_date, 1, 10) AS trading_day
                FROM signal s
                JOIN portfolio p ON s.portfolio_id = p.id
                WHERE p.config_id = ?
                  AND s.ticker = ?
                  AND substr(p.trading_date, 1, 10) < ?
                  AND substr(p.trading_date, 1, 10) >= date(?, ?)
                ORDER BY substr(p.trading_date, 1, 10) DESC, s.updated_at DESC
                ''',
                (config_id, ticker, trading_day_value, trading_day_value, f"-{lookback_days} days"),
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception:
            logger.error("signal_history_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def get_ticker_performance(
        self,
        config_id: str,
        ticker: str,
        trading_date,
        lookback_days: int = 30,
    ) -> Dict[str, Any]:
        """Return recent aggregated ticker performance up to the current backtest day."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            trading_day_value = self._normalize_trading_day_value(trading_date)
            cursor.execute(
                '''
                SELECT
                    COALESCE(SUM(tdp.daily_pnl), 0) AS cumulative_pnl,
                    COUNT(*) AS trade_days,
                    COALESCE(SUM(CASE WHEN tdp.daily_pnl > 0 THEN 1 ELSE 0 END), 0) AS win_days
                FROM ticker_daily_pnl tdp
                JOIN portfolio p ON tdp.portfolio_id = p.id
                WHERE p.config_id = ?
                  AND tdp.ticker = ?
                  AND substr(tdp.trading_date, 1, 10) < ?
                  AND substr(tdp.trading_date, 1, 10) >= date(?, ?)
                ''',
                (config_id, ticker, trading_day_value, trading_day_value, f"-{lookback_days} days"),
            )
            row = cursor.fetchone()
            trade_days = int(row["trade_days"] or 0) if row else 0
            cumulative_pnl = float(row["cumulative_pnl"] or 0.0) if row else 0.0
            win_days = int(row["win_days"] or 0) if row else 0
            return {
                "cumulative_pnl": cumulative_pnl,
                "trade_days": trade_days,
                "win_days": win_days,
                "win_rate": (win_days / trade_days) if trade_days > 0 else 0.0,
                "avg_daily_pnl": (cumulative_pnl / trade_days) if trade_days > 0 else 0.0,
            }
        except Exception:
            logger.error("ticker_performance_unavailable")
            return {
                "cumulative_pnl": 0.0,
                "trade_days": 0,
                "win_days": 0,
                "win_rate": 0.0,
                "avg_daily_pnl": 0.0,
            }
        finally:
            if conn:
                conn.close()

    def get_futures_trade_pair_performance(
        self,
        config_id: str,
        ticker: str,
        side: str,
        trading_date,
        lookback_trades: int = 20,
    ) -> Dict[str, Any]:
        """Return recent completed round-trip performance for ticker + side before trading_date."""
        conn = None
        try:
            from util.futures_trade_pairs import build_completed_trade_pairs, summarize_trade_pairs

            conn = self._get_connection()
            cursor = conn.cursor()
            trading_day_value = self._normalize_trading_day_value(trading_date)
            cursor.execute(
                '''
                SELECT *
                FROM futures_transactions
                WHERE config_id = ?
                  AND ticker = ?
                  AND substr(trading_date, 1, 10) < ?
                ORDER BY substr(trading_date, 1, 10), created_at, id
                ''',
                (config_id, ticker, trading_day_value),
            )
            pairs = [
                pair for pair in build_completed_trade_pairs([dict(row) for row in cursor.fetchall()])
                if pair.get("ticker") == ticker.upper() and pair.get("side") == str(side).lower()
            ]
            recent_pairs = pairs[-int(lookback_trades):] if lookback_trades else pairs
            summary = summarize_trade_pairs(recent_pairs)
            summary["lookback_trades"] = int(lookback_trades)
            summary["side"] = str(side).lower()
            summary["ticker"] = ticker.upper()
            return summary
        except Exception:
            logger.error("trade_pair_performance_unavailable")
            return {
                "ticker": ticker.upper(),
                "side": str(side).lower(),
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "flat_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "avg_return": 0.0,
                "lookback_trades": int(lookback_trades),
            }
        finally:
            if conn:
                conn.close()

    def get_futures_conditional_trade_performance(
        self,
        config_id: str,
        ticker: str,
        side: str,
        trading_date,
        signal_combo: Optional[List[str]] = None,
        lookback_trades: int = 30,
        include_rollover: bool = False,
    ) -> Dict[str, Any]:
        """Return completed trade-pair performance for ticker + side + signal_combo.

        The method is read-only and intentionally uses only rows before the
        current trading day so planner feedback cannot leak future outcomes.
        """
        conn = None
        normalized_combo = self._normalize_signal_combo(signal_combo)
        try:
            from util.futures_trade_pairs import build_completed_trade_pairs, summarize_trade_pairs

            conn = self._get_connection()
            cursor = conn.cursor()
            trading_day_value = self._normalize_trading_day_value(trading_date)
            cursor.execute(
                '''
                SELECT *
                FROM futures_transactions
                WHERE config_id = ?
                  AND ticker = ?
                  AND substr(trading_date, 1, 10) < ?
                ORDER BY substr(trading_date, 1, 10), created_at, id
                ''',
                (config_id, ticker, trading_day_value),
            )
            transactions = [dict(row) for row in cursor.fetchall()]
            pairs = [
                pair for pair in build_completed_trade_pairs(
                    transactions,
                    include_rollover=bool(include_rollover),
                )
                if pair.get("ticker") == ticker.upper()
                and pair.get("side") == str(side).lower()
            ]

            recommendation_ids = [
                str(pair.get("open_recommendation_id"))
                for pair in pairs
                if pair.get("open_recommendation_id")
            ]
            recommendations_by_id: Dict[str, Dict[str, Any]] = {}
            if recommendation_ids:
                placeholders = ", ".join(["?"] * len(set(recommendation_ids)))
                cursor.execute(
                    f'''
                    SELECT *
                    FROM futures_recommendation
                    WHERE id IN ({placeholders})
                    ''',
                    tuple(sorted(set(recommendation_ids))),
                )
                recommendations_by_id = {
                    str(row["id"]): dict(row)
                    for row in cursor.fetchall()
                }

            matched_pairs = []
            for pair in pairs:
                item = dict(pair)
                recommendation = recommendations_by_id.get(str(item.get("open_recommendation_id") or ""))
                snapshot = {}
                if recommendation:
                    snapshot = self._deserialize_external_json(recommendation, "signal_snapshot") or {}
                    if not isinstance(snapshot, dict):
                        snapshot = {}
                item_combo = self._signal_combo_from_snapshot(snapshot)
                item["signal_combo"] = list(item_combo)
                final_contract = snapshot.get("final_action_contract") if isinstance(snapshot, dict) else {}
                evidence = final_contract.get("evidence_used") if isinstance(final_contract, dict) else {}
                item["market_confirmation"] = {
                    "confirmation_score": evidence.get("market_confirmation_score"),
                    "conflicts": evidence.get("market_confirmation_conflicts") or [],
                    "source": "final_action_contract.evidence_used",
                } if isinstance(evidence, dict) and evidence else None
                item["pm_risk_gate"] = self._pm_risk_gate_from_snapshot(snapshot)
                if normalized_combo is not None and item_combo != normalized_combo:
                    continue
                matched_pairs.append(item)

            recent_pairs = matched_pairs[-int(lookback_trades):] if lookback_trades else matched_pairs
            summary = summarize_trade_pairs(recent_pairs)
            summary["lookback_trades"] = int(lookback_trades)
            summary["side"] = str(side).lower()
            summary["ticker"] = ticker.upper()
            summary["signal_combo"] = list(normalized_combo) if normalized_combo is not None else None
            summary["cutoff_trading_date"] = trading_day_value
            summary["include_rollover"] = bool(include_rollover)
            return summary
        except Exception:
            logger.error("conditional_trade_performance_unavailable")
            return {
                "ticker": ticker.upper(),
                "side": str(side).lower(),
                "signal_combo": list(normalized_combo) if normalized_combo is not None else None,
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "flat_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "avg_return": 0.0,
                "lookback_trades": int(lookback_trades),
                "cutoff_trading_date": self._normalize_trading_day_value(trading_date),
                "include_rollover": bool(include_rollover),
            }
        finally:
            if conn:
                conn.close()

    def refresh_strategy_memory(
        self,
        config_id: str,
        trading_date,
        memory_config: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Refresh DB-backed strategy memory up to trading_date."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            count = self._refresh_strategy_memory_with_cursor(
                cursor,
                config_id=config_id,
                trading_date=trading_date,
                memory_config=memory_config,
            )
            conn.commit()
            return count
        except Exception:
            logger.error("strategy_memory_refresh_failed")
            return 0
        finally:
            if conn:
                conn.close()

    def get_strategy_memory(
        self,
        config_id: str,
        ticker: str,
        side: str,
        trading_date=None,
        signal_combo: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Read the latest DB-backed strategy memory for ticker-side and combo.

        The table is updated after Phase4. This reader does not derive future
        data; it only returns rows whose valid_until has not expired as of the
        current trading day when a date is provided.
        """
        conn = None
        side_value = str(side or "").lower()
        combo_key = self._strategy_memory_signal_combo_key(signal_combo)
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_strategy_memory_schema(cursor)
            trading_day_value = self._normalize_trading_day_value(trading_date)
            if trading_day_value:
                cursor.execute(
                    '''
                    SELECT *
                    FROM strategy_memory
                    WHERE config_id = ?
                      AND ticker = ?
                      AND side = ?
                      AND signal_combo IN ('*', ?)
                      AND (valid_until IS NULL OR valid_until >= ?)
                      AND source_trading_date IS NOT NULL
                      AND substr(source_trading_date, 1, 10) < ?
                    ORDER BY CASE WHEN signal_combo = ? THEN 0 ELSE 1 END, updated_at DESC
                    ''',
                    (
                        config_id,
                        ticker.upper(),
                        side_value,
                        combo_key,
                        trading_day_value,
                        trading_day_value,
                        combo_key,
                    ),
                )
            else:
                cursor.execute(
                    '''
                    SELECT *
                    FROM strategy_memory
                    WHERE config_id = ?
                      AND ticker = ?
                      AND side = ?
                      AND signal_combo IN ('*', ?)
                    ORDER BY CASE WHEN signal_combo = ? THEN 0 ELSE 1 END, updated_at DESC
                    ''',
                    (config_id, ticker.upper(), side_value, combo_key, combo_key),
                )
            rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                row["payload"] = self._deserialize_json(row.get("payload_json")) or {}
            combo_row = next((row for row in rows if row.get("signal_combo") == combo_key), None)
            side_row = next((row for row in rows if row.get("signal_combo") == "*"), None)
            return {
                "enabled": True,
                "ticker": ticker.upper(),
                "side": side_value,
                "signal_combo": self._deserialize_json(combo_key) if combo_key != "*" else "*",
                "combo": combo_row,
                "side_memory": side_row,
                "records": rows,
            }
        except Exception:
            logger.error("strategy_memory_unavailable")
            return {
                "enabled": False,
                "ticker": ticker.upper(),
                "side": side_value,
                "signal_combo": self._deserialize_json(combo_key) if combo_key != "*" else "*",
                "combo": None,
                "side_memory": None,
                "records": [],
                "error": "strategy_memory_unavailable",
            }
        finally:
            if conn:
                conn.close()

    def get_config_learning_overlay(
        self,
        config_id: str,
        trading_date=None,
        param_prefix: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return active researcher-learned config overlays that are valid today."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_reviewer_learning_schema(cursor)
            trading_day_value = self._normalize_trading_day_value(trading_date)
            params: List[Any] = [config_id]
            where = [
                "config_id = ?",
                "active = 1",
            ]
            if trading_day_value:
                where.append("(valid_until IS NULL OR valid_until >= ?)")
                where.append(
                    """substr(COALESCE(
                        trading_date,
                        (
                            SELECT le.trading_date
                            FROM learning_event_log le
                            WHERE le.id = config_learning_overlay.source_event_id
                              AND le.config_id = config_learning_overlay.config_id
                            LIMIT 1
                        )
                    ), 1, 10) < ?"""
                )
                params.extend([trading_day_value, trading_day_value])
            if param_prefix:
                where.append("param_key LIKE ?")
                params.append(f"{param_prefix}%")
            cursor.execute(
                f'''
                SELECT *
                FROM config_learning_overlay
                WHERE {' AND '.join(where)}
                ORDER BY confidence_score DESC, created_at DESC
                ''',
                tuple(params),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                item["learned_value"] = self._deserialize_json(item.get("learned_value_json"))
                item["previous_value"] = self._deserialize_json(item.get("previous_value_json"))
                rows.append(item)
            return rows
        except Exception:
            logger.warning("config_learning_overlay_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def get_setup_type_performance(
        self,
        config_id: str,
        ticker: str,
        side: Optional[str] = None,
        trading_date=None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Read matured signal-template performance for one ticker."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_reviewer_learning_schema(cursor)
            params: List[Any] = [config_id, ticker.upper()]
            where = ["config_id = ?", "ticker IN (?, '*')"]
            if side:
                where.append("side IN (?, '*')")
                params.append(str(side).lower())
            trading_day_value = self._normalize_trading_day_value(trading_date)
            if trading_day_value:
                where.append("last_sample_date IS NOT NULL")
                where.append("last_sample_date < ?")
                where.append("(valid_until IS NULL OR valid_until >= ?)")
                params.extend([trading_day_value, trading_day_value])
            cursor.execute(
                f'''
                SELECT *
                FROM setup_type_performance
                WHERE {' AND '.join(where)}
                ORDER BY confidence_score DESC, sample_count DESC, last_updated DESC
                LIMIT ?
                ''',
                tuple(params + [int(limit)]),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                item["payload"] = self._deserialize_json(item.get("payload_json")) or {}
                rows.append(item)
            return rows
        except Exception:
            logger.warning("signal_template_performance_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def get_adaptive_policy_state(
        self,
        config_id: str,
        ticker: str,
        side: Optional[str] = None,
        setup_type: Optional[str] = None,
        horizon_class: Optional[str] = None,
        market_regime: Optional[str] = None,
        trading_date=None,
    ) -> List[Dict[str, Any]]:
        """Read researcher-learned policy state for PM and analyst-safe calibration."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_reviewer_learning_schema(cursor)
            params: List[Any] = [config_id, ticker.upper()]
            where = [
                "config_id = ?",
                "active = 1",
                "ticker IN (?, '*')",
            ]
            if side:
                where.append("side IN (?, '*')")
                params.append(str(side).lower())
            if setup_type:
                where.append("setup_type IN (?, '*')")
                params.append(str(setup_type))
            if horizon_class:
                where.append("horizon_class IN (?, '*')")
                params.append(str(horizon_class))
            if market_regime:
                where.append("market_regime IN (?, '*')")
                params.append(str(market_regime))
            trading_day_value = self._normalize_trading_day_value(trading_date)
            if trading_day_value:
                where.append("(valid_until IS NULL OR valid_until >= ?)")
                where.append(
                    """substr(COALESCE(
                        source_trading_date,
                        (
                            SELECT le.trading_date
                            FROM learning_event_log le
                            WHERE le.id = adaptive_policy_state.source_event_id
                              AND le.config_id = adaptive_policy_state.config_id
                            LIMIT 1
                        )
                    ), 1, 10) < ?"""
                )
                params.extend([trading_day_value, trading_day_value])
            cursor.execute(
                f'''
                SELECT *
                FROM adaptive_policy_state
                WHERE {' AND '.join(where)}
                ORDER BY confidence_score DESC, sample_count DESC, created_at DESC
                ''',
                tuple(params),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                item["payload"] = self._deserialize_json(item.get("payload_json")) or {}
                rows.append(item)
            return rows
        except Exception:
            logger.warning("adaptive_policy_state_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def get_alpha_setup_profiles(
        self,
        config_id: str,
        ticker: str,
        sector: Optional[str] = None,
        side: Optional[str] = None,
        horizon_class: Optional[str] = None,
        market_regime: Optional[str] = None,
        setup_type: Optional[str] = None,
        trading_date=None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Read future-usable setup profiles for analyst prompts and PM sizing.

        The rows are Phase4 products from previous days. The query allows exact
        ticker and same-sector fallback, but never returns inactive or expired
        profiles and never reaches into future samples.
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_reviewer_learning_schema(cursor)
            ticker_value = str(ticker or "").upper()
            sector_value = str(sector or "*")
            params: List[Any] = [config_id]
            where = ["config_id = ?", "active = 1"]
            if ticker_value and ticker_value != "*":
                where.append("ticker IN (?, '*')")
                params.append(ticker_value)
            if sector_value and sector_value != "*":
                where.append("sector IN (?, '*')")
                params.append(sector_value)
            if side and str(side) != "*":
                where.append("side IN (?, '*')")
                params.append(str(side).lower())
            if horizon_class and str(horizon_class) != "*":
                where.append("horizon_class IN (?, '*')")
                params.append(str(horizon_class))
            if market_regime and str(market_regime) != "*":
                where.append("market_regime IN (?, '*')")
                params.append(str(market_regime))
            if setup_type and str(setup_type) != "*":
                where.append("setup_type = ?")
                params.append(str(setup_type))
            trading_day_value = self._normalize_trading_day_value(trading_date)
            if trading_day_value:
                where.append("last_sample_date IS NOT NULL")
                where.append("last_sample_date < ?")
                where.append("(valid_until IS NULL OR valid_until >= ?)")
                params.extend([trading_day_value, trading_day_value])
            cursor.execute(
                f'''
                SELECT *
                FROM alpha_setup_profile
                WHERE {' AND '.join(where)}
                ORDER BY
                    CASE WHEN ticker = ? THEN 0 WHEN ticker = '*' THEN 1 ELSE 2 END,
                    CASE WHEN sector = ? THEN 0 WHEN sector = '*' THEN 1 ELSE 2 END,
                    CASE lifecycle_state
                        WHEN 'deployable' THEN 0
                        WHEN 'protected' THEN 1
                        WHEN 'watchlist' THEN 2
                        WHEN 'candidate' THEN 3
                        WHEN 'capped' THEN 4
                        WHEN 'rejected' THEN 5
                        ELSE 6
                    END,
                    confidence_score DESC,
                    sample_count DESC,
                    updated_at DESC
                LIMIT ?
                ''',
                tuple(params + [ticker_value or "*", sector_value or "*", int(limit)]),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                item["payload"] = self._deserialize_json(item.get("payload_json")) or {}
                rows.append(item)
            return rows
        except Exception:
            logger.warning("alpha_setup_profiles_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def _promote_action_value_payload_fields(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Promote legacy payload action-value fields into canonical top-level keys."""
        if not isinstance(item, dict):
            return {}
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if payload:
            item["payload"] = payload
        if not item.get("reward_source"):
            item["reward_source"] = (
                payload.get("reward_source")
                or payload.get("sample_source")
                or payload.get("source_reward_source")
                or ""
            )
        if not item.get("evidence_scope"):
            item["evidence_scope"] = (
                payload.get("evidence_scope")
                or payload.get("amplification_scope_quality")
                or payload.get("source_quality")
                or ""
            )
        if not item.get("action_value_lane"):
            item["action_value_lane"] = (
                payload.get("action_value_lane")
                or payload.get("source_action_value_lane")
                or item.get("action_name")
                or ""
            )
        if not item.get("canonical_action_family"):
            item["canonical_action_family"] = (
                payload.get("canonical_action_family")
                or payload.get("source_canonical_action_family")
                or ""
            )
        if not item.get("consumer_scope") and payload.get("consumer_scope"):
            item["consumer_scope"] = payload.get("consumer_scope")
        if not item.get("learning_lane"):
            item["learning_lane"] = (
                payload.get("learning_lane")
                or payload.get("action_value_lane")
                or item.get("action_value_lane")
                or item.get("action_name")
                or ""
            )
        if not item.get("memory_side_role"):
            item["memory_side_role"] = (
                payload.get("memory_side_role")
                or payload.get("side_role")
                or payload.get("source_memory_side_role")
                or ""
            )
        if not item.get("retrieval_key"):
            item["retrieval_key"] = payload.get("retrieval_key") or ""
        if not item.get("fallback_retrieval_key"):
            item["fallback_retrieval_key"] = payload.get("fallback_retrieval_key") or ""
        if not item.get("execution_retrieval_key"):
            item["execution_retrieval_key"] = payload.get("execution_retrieval_key") or ""
        return item

    def get_alpha_setup_action_values(
        self,
        config_id: str,
        ticker: str,
        side: Optional[str] = None,
        horizon_class: Optional[str] = None,
        market_regime: Optional[str] = None,
        setup_type: Optional[str] = None,
        trading_date=None,
        limit: int = 5,
        consumer_scope: Optional[str] = "pm_learning",
        learning_lane: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Read setup action-value hints written after settled future samples."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_reviewer_learning_schema(cursor)
            ticker_value = str(ticker or "").upper()
            params: List[Any] = [config_id]
            where = ["config_id = ?", "active = 1"]
            if ticker_value and ticker_value != "*":
                where.append("ticker IN (?, '*')")
                params.append(ticker_value)
            if side and str(side) != "*":
                where.append("side IN (?, '*')")
                params.append(str(side).lower())
            if horizon_class and str(horizon_class) != "*":
                where.append("horizon_class IN (?, '*')")
                params.append(str(horizon_class))
            if market_regime and str(market_regime) != "*":
                where.append("market_regime IN (?, '*')")
                params.append(str(market_regime))
            if setup_type and str(setup_type) != "*":
                where.append("setup_type IN (?, '*')")
                params.append(str(setup_type))
            if consumer_scope and str(consumer_scope) != "*":
                where.append("(consumer_scope = ? OR consumer_scope IS NULL OR consumer_scope = '')")
                params.append(str(consumer_scope))
            if learning_lane and str(learning_lane) != "*":
                where.append("(learning_lane = ? OR action_value_lane = ?)")
                params.extend([str(learning_lane), str(learning_lane)])
            trading_day_value = self._normalize_trading_day_value(trading_date)
            if trading_day_value:
                where.append("last_sample_date IS NOT NULL")
                where.append("last_sample_date < ?")
                where.append("(valid_until IS NULL OR valid_until >= ?)")
                params.extend([trading_day_value, trading_day_value])
            cursor.execute(
                f'''
                SELECT *
                FROM alpha_setup_action_value
                WHERE {' AND '.join(where)}
                ORDER BY
                    CASE WHEN ticker = ? THEN 0 WHEN ticker = '*' THEN 1 ELSE 2 END,
                    CASE WHEN ? != '' AND side = ? THEN 0 WHEN side = '*' THEN 1 ELSE 2 END,
                    CASE WHEN ? != '' AND horizon_class = ? THEN 0 WHEN horizon_class = '*' THEN 1 ELSE 2 END,
                    CASE WHEN ? != '' AND market_regime = ? THEN 0 WHEN market_regime = '*' THEN 1 ELSE 2 END,
                    CASE WHEN ? != '' AND setup_type = ? THEN 0 WHEN setup_type = '*' THEN 1 ELSE 2 END,
                    CASE WHEN consumer_scope = 'pm_learning' THEN 0 ELSE 1 END,
                    CASE WHEN learning_lane = action_value_lane THEN 0 ELSE 1 END,
                    CASE WHEN action_preference IS NOT NULL AND action_preference != '' THEN 0 ELSE 1 END,
                    CASE
                        WHEN evidence_scope = 'exact_real_state'
                          OR payload_json LIKE '%"amplification_scope_quality": "exact_real_state"%'
                          OR payload_json LIKE '%"evidence_scope": "exact_real_state"%'
                        THEN 0
                        WHEN evidence_scope = 'partial_real_state'
                          OR payload_json LIKE '%"amplification_scope_quality": "partial_real_state"%'
                          OR payload_json LIKE '%"evidence_scope": "partial_real_state"%'
                        THEN 1
                        ELSE 2
                    END,
                    CASE
                        WHEN reward_source IN ('trade_episode', 'episode_trade', 'real_trade', 'complete_episode')
                          OR payload_json LIKE '%"reward_source": "trade_episode"%'
                          OR payload_json LIKE '%"reward_source": "episode_trade"%'
                          OR payload_json LIKE '%"reward_source": "real_trade"%'
                          OR payload_json LIKE '%"reward_source": "complete_episode"%'
                        THEN 0
                        WHEN reward_source = 'counterfactual_prior'
                          OR payload_json LIKE '%"reward_source": "counterfactual_prior"%'
                        THEN 2
                        ELSE 1
                    END,
                    confidence_score DESC,
                    sample_count DESC,
                    updated_at DESC
                LIMIT ?
                ''',
                tuple(
                    params
                    + [
                        ticker_value or "*",
                        str(side or "").lower(),
                        str(side or "").lower(),
                        str(horizon_class or ""),
                        str(horizon_class or ""),
                        str(market_regime or ""),
                        str(market_regime or ""),
                        str(setup_type or ""),
                        str(setup_type or ""),
                        int(limit),
                    ]
                ),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                item["payload"] = self._deserialize_json(item.get("payload_json")) or {}
                self._promote_action_value_payload_fields(item)
                rows.append(item)
            return rows
        except Exception:
            logger.warning("alpha_setup_action_values_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def get_provisional_policy_state(
        self,
        config_id: str,
        ticker: str,
        side: Optional[str] = None,
        setup_type: Optional[str] = None,
        horizon_class: Optional[str] = None,
        trading_date=None,
    ) -> List[Dict[str, Any]]:
        """Read short-lived reviewer risk sentinels."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_reviewer_learning_schema(cursor)
            params: List[Any] = [config_id, ticker.upper()]
            where = [
                "config_id = ?",
                "active = 1",
                "ticker IN (?, '*')",
            ]
            if side:
                where.append("side IN (?, '*')")
                params.append(str(side).lower())
            if setup_type:
                where.append("setup_type IN (?, '*')")
                params.append(str(setup_type))
            if horizon_class:
                where.append("horizon_class IN (?, '*')")
                params.append(str(horizon_class))
            trading_day_value = self._normalize_trading_day_value(trading_date)
            if trading_day_value:
                where.append("(valid_until IS NULL OR valid_until >= ?)")
                where.append("source_trading_date IS NOT NULL")
                where.append("source_trading_date < ?")
                params.extend([trading_day_value, trading_day_value])
            cursor.execute(
                f'''
                SELECT *
                FROM provisional_policy_state
                WHERE {' AND '.join(where)}
                ORDER BY confidence_score DESC, created_at DESC
                ''',
                tuple(params),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                item["payload"] = self._deserialize_json(item.get("payload_json")) or {}
                item["rollback_value"] = self._deserialize_json(item.get("rollback_value_json")) or {}
                rows.append(item)
            return rows
        except Exception:
            logger.warning("provisional_policy_state_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def get_analyst_learning_digest(
        self,
        config_id: str,
        analyst: str,
        ticker: str,
        sector: Optional[str] = None,
        horizon_class: Optional[str] = None,
        market_regime: Optional[str] = None,
        trading_date=None,
        max_items: int = 5,
    ) -> List[Dict[str, Any]]:
        """Retrieve compact, mature reviewer digests for one analyst prompt."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_reviewer_learning_schema(cursor)
            trading_day_value = self._normalize_trading_day_value(trading_date)
            ticker_value = str(ticker or "*").upper()
            sector_value = str(sector or "*")
            horizon_value = str(horizon_class or "*")
            regime_value = str(market_regime or "*")
            params: List[Any] = [config_id, str(analyst)]
            where = ["config_id = ?", "analyst = ?", "accepted = 1"]
            if ticker_value != "*":
                where.append("ticker IN (?, '*')")
                params.append(ticker_value)
            if sector_value != "*":
                where.append("sector IN (?, '*')")
                params.append(sector_value)
            if horizon_value != "*":
                where.append("horizon_class IN (?, '*')")
                params.append(horizon_value)
            if regime_value != "*":
                where.append("market_regime IN (?, '*')")
                params.append(regime_value)
            if trading_day_value:
                where.append("(valid_until IS NULL OR valid_until >= ?)")
                where.append(
                    """substr((
                        SELECT le.trading_date
                        FROM learning_event_log le
                        WHERE le.id = analyst_learning_digest.source_event_id
                          AND le.config_id = analyst_learning_digest.config_id
                        LIMIT 1
                    ), 1, 10) < ?"""
                )
                params.extend([trading_day_value, trading_day_value])
            cursor.execute(
                f'''
                SELECT *
                FROM analyst_learning_digest
                WHERE {' AND '.join(where)}
                ORDER BY
                    CASE WHEN ticker = ? THEN 0 WHEN ticker = '*' THEN 1 ELSE 2 END,
                    CASE WHEN sector = ? THEN 0 WHEN sector = '*' THEN 1 ELSE 2 END,
                    CASE WHEN horizon_class = ? THEN 0 WHEN horizon_class = '*' THEN 1 ELSE 2 END,
                    CASE WHEN market_regime = ? THEN 0 WHEN market_regime = '*' THEN 1 ELSE 2 END,
                    confidence_score DESC,
                    sample_count DESC,
                    created_at DESC
                LIMIT ?
                ''',
                tuple(
                    params
                    + [
                        ticker_value,
                        sector_value,
                        horizon_value,
                        regime_value,
                        int(max_items),
                    ]
                ),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                item["payload"] = self._deserialize_json(item.get("payload_json")) or {}
                rows.append(item)
            return rows
        except Exception:
            logger.warning("analyst_learning_digest_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def get_trade_episode_memory(
        self,
        config_id: str,
        ticker: str,
        sector: Optional[str] = None,
        side: Optional[str] = None,
        horizon_class: Optional[str] = None,
        market_regime: Optional[str] = None,
        trading_date=None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Retrieve compact historical trade episodes for exploratory learning."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_reviewer_learning_schema(cursor)
            ticker_value = str(ticker or "").upper()
            sector_value = str(sector or "*")
            params: List[Any] = [config_id]
            where = ["config_id = ?"]
            if ticker_value and ticker_value != "*":
                where.append("ticker IN (?, '*')")
                params.append(ticker_value)
            if sector_value and sector_value != "*":
                where.append("sector IN (?, '*')")
                params.append(sector_value)
            if side and str(side) != "*":
                where.append("side IN (?, '*')")
                params.append(str(side).lower())
            if horizon_class and str(horizon_class) != "*":
                where.append("horizon_class IN (?, '*')")
                params.append(str(horizon_class))
            if market_regime and str(market_regime) != "*":
                where.append("market_regime IN (?, '*')")
                params.append(str(market_regime))
            trading_day_value = self._normalize_trading_day_value(trading_date)
            if trading_day_value:
                where.append("(close_date IS NULL OR close_date < ?)")
                params.append(trading_day_value)
            cursor.execute(
                f'''
                SELECT *
                FROM trade_episode_memory
                WHERE {' AND '.join(where)}
                ORDER BY
                    CASE WHEN ticker = ? THEN 0 WHEN ticker = '*' THEN 1 ELSE 2 END,
                    CASE WHEN sector = ? THEN 0 WHEN sector = '*' THEN 1 ELSE 2 END,
                    ABS(net_pnl) DESC,
                    close_date DESC,
                    created_at DESC
                LIMIT ?
                ''',
                tuple(params + [ticker_value or "*", sector_value or "*", int(limit)]),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                item["payload"] = self._deserialize_external_json(item, "payload")
                rows.append(item)
            return rows
        except Exception:
            logger.warning("trade_episode_memory_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def get_similar_alpha_setup_action_values(
        self,
        config_id: str,
        ticker: str,
        sector: Optional[str] = None,
        side: Optional[str] = None,
        horizon_class: Optional[str] = None,
        market_regime: Optional[str] = None,
        setup_type: Optional[str] = None,
        trading_date=None,
        limit: int = 6,
    ) -> List[Dict[str, Any]]:
        """Aggregate strictly historical setup samples into action-value-like priors.

        This is a lightweight SQL-RAG layer: it reads settled samples only,
        never writes, and uses business dates rather than created_at so a
        backtest day cannot see future samples.
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_reviewer_learning_schema(cursor)
            if not self._table_exists(cursor, "alpha_setup_sample"):
                return []
            trading_day_value = self._normalize_trading_day_value(trading_date)
            if not trading_day_value:
                return []

            ticker_value = str(ticker or "").upper()
            sector_value = str(sector or "")
            side_value = str(side or "").lower()
            horizon_value = str(horizon_class or "")
            regime_value = str(market_regime or "")
            setup_value = str(setup_type or "")
            has_explicit_state_request = bool(
                ticker_value
                and side_value
                and side_value != "*"
                and horizon_value
                and horizon_value != "*"
                and regime_value
                and regime_value not in {"*", "unknown"}
                and setup_value
                and setup_value not in {"*", "unknown", "generic_trade_setup"}
            )

            params: List[Any] = [config_id, trading_day_value]
            where = [
                "config_id = ?",
                "trading_date < ?",
            ]
            if ticker_value:
                if sector_value:
                    where.append("(ticker = ? OR sector = ?)")
                    params.extend([ticker_value, sector_value])
                else:
                    where.append("ticker = ?")
                    params.append(ticker_value)
            elif sector_value:
                where.append("sector = ?")
                params.append(sector_value)
            if side_value and side_value != "*":
                where.append("side IN (?, '*')")
                params.append(side_value)
            if horizon_value and horizon_value != "*":
                where.append("horizon_class IN (?, '*', 'unknown')")
                params.append(horizon_value)
            if regime_value and regime_value != "*":
                where.append("market_regime IN (?, '*', 'unknown')")
                params.append(regime_value)
            if setup_value and setup_value != "*":
                where.append("setup_type IN (?, '*', 'unknown', 'generic_trade_setup')")
                params.append(setup_value)

            cursor.execute(
                f'''
                SELECT *
                FROM alpha_setup_sample
                WHERE {' AND '.join(where)}
                ORDER BY
                    CASE WHEN ticker = ? THEN 0 ELSE 1 END,
                    CASE WHEN setup_type = ? THEN 0 ELSE 1 END,
                    CASE WHEN market_regime = ? THEN 0 ELSE 1 END,
                    trading_date DESC,
                    created_at DESC
                LIMIT ?
                ''',
                tuple(params + [ticker_value or "*", setup_value or "*", regime_value or "*", max(int(limit) * 8, int(limit))]),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                payload = self._deserialize_json(item.get("payload_json"))
                item["payload"] = payload if isinstance(payload, dict) else {}
                rows.append(item)
            if not rows:
                return []

            def _safe_float(value, default: float = 0.0) -> float:
                try:
                    if value is None:
                        return default
                    return float(value)
                except Exception:
                    return default

            def _safe_int(value, default: int = 0) -> int:
                try:
                    if value is None:
                        return default
                    return int(float(value))
                except Exception:
                    return default

            def _classify_action(row: Dict[str, Any]) -> tuple[str, str]:
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                deployment = (
                    payload.get("deployment_outcome")
                    if isinstance(payload.get("deployment_outcome"), dict)
                    else {}
                )
                text = str(row.get("action_taken") or row.get("pm_action") or "").lower()
                current_lots = _safe_int(
                    payload.get("current_lots"),
                    _safe_int(deployment.get("current_lots")),
                )
                target_lots = _safe_int(
                    row.get("target_lots"),
                    _safe_int(payload.get("target_lots"), _safe_int(deployment.get("target_lots"))),
                )
                if "execution" in text or "fill" in text:
                    transition_action = "execution"
                elif current_lots == 0 and target_lots != 0:
                    transition_action = "open"
                elif current_lots != 0 and target_lots == 0:
                    transition_action = "exit"
                elif current_lots and target_lots and current_lots * target_lots < 0:
                    transition_action = "exit"
                elif current_lots and target_lots and abs(target_lots) > abs(current_lots):
                    transition_action = "add"
                elif current_lots and target_lots and abs(target_lots) < abs(current_lots):
                    transition_action = "reduce"
                elif current_lots == target_lots:
                    transition_action = (
                        "conditional_monitor"
                        if (
                            "conditional" in text
                            or "monitor" in text
                            or "watch" in text
                            or "trigger" in text
                        )
                        else "hold" if current_lots else text or "hold"
                    )
                else:
                    transition_action = text or "hold"
                family = canonical_action_family(
                    transition_action,
                    current_lots=current_lots,
                    target_lots=target_lots,
                )
                lane = canonical_action_value_lane(
                    transition_action,
                    current_lots=current_lots,
                    target_lots=target_lots,
                )
                return family, lane

            def _reward_signal_for_row(row: Dict[str, Any]) -> tuple[Optional[float], str]:
                reward = _safe_float(row.get("net_pnl")) - _safe_float(row.get("commission"))
                source_type = str(row.get("source_type") or "").strip().lower()
                if _safe_int(row.get("executed_lots")) > 0 or source_type == "trade":
                    return reward, "real_trade"
                if source_type.startswith("counterfactual_"):
                    return reward * 0.35, "counterfactual_prior"
                return None, "ignored"

            def _is_real_trade_row(row: Dict[str, Any]) -> bool:
                return _safe_int(row.get("executed_lots")) > 0 or str(row.get("source_type") or "").strip().lower() == "trade"

            def _matches_requested_state(row: Dict[str, Any]) -> bool:
                if not has_explicit_state_request:
                    return False
                if ticker_value and str(row.get("ticker") or "").upper() != ticker_value:
                    return False
                if side_value and side_value != "*" and str(row.get("side") or "").lower() != side_value:
                    return False
                if horizon_value and horizon_value != "*" and str(row.get("horizon_class") or "") != horizon_value:
                    return False
                if regime_value and regime_value != "*" and str(row.get("market_regime") or "") != regime_value:
                    return False
                if setup_value and setup_value != "*" and str(row.get("setup_type") or "") != setup_value:
                    return False
                return True

            grouped: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
            for row in rows:
                grouped.setdefault(_classify_action(row), []).append(row)

            result: List[Dict[str, Any]] = []
            for (action_family, action_name), action_rows in grouped.items():
                reward_values: List[float] = []
                real_trade_reward_count = 0
                counterfactual_reward_count = 0
                for row in action_rows:
                    reward, reward_source = _reward_signal_for_row(row)
                    if reward is None:
                        continue
                    reward_values.append(reward)
                    if reward_source == "real_trade":
                        real_trade_reward_count += 1
                    elif reward_source == "counterfactual_prior":
                        counterfactual_reward_count += 1
                sample_count = len(action_rows)
                reward_sum = sum(reward_values)
                reward_mean = reward_sum / len(reward_values) if reward_values else 0.0
                win_rate = (sum(1 for value in reward_values if value > 0) / len(reward_values)) if reward_values else 0.0
                confidence_score = min(0.85, 0.12 + min(0.35, sample_count / 12.0) + min(0.25, abs(win_rate - 0.5)) + min(0.13, abs(reward_sum) / 50000.0))
                prior_direction_hint = (
                    "positive"
                    if reward_mean > 0 and reward_sum > 0
                    else "negative"
                    if reward_mean < 0 or reward_sum < 0
                    else "neutral"
                )
                action_preference = ""
                exact_rows = [
                    row for row in action_rows
                    if str(row.get("ticker") or "").upper() == ticker_value
                ]
                exact_real_rows = [
                    row for row in exact_rows
                    if _is_real_trade_row(row)
                ]
                exact_state_real_rows = [
                    row for row in exact_real_rows
                    if _matches_requested_state(row)
                ]
                partial_state_real_rows = [
                    row for row in exact_real_rows
                    if row not in exact_state_real_rows
                ]
                similar_real_rows = [
                    row for row in action_rows
                    if _is_real_trade_row(row) and row not in exact_real_rows
                ]
                exact_counterfactual_rows = [
                    row for row in exact_rows
                    if str(row.get("source_type") or "").strip().lower().startswith("counterfactual_")
                ]
                loss_reward_count = sum(1 for value in reward_values if value < 0)
                tail_loss_count = sum(1 for value in reward_values if value <= -1000.0)
                worst_reward = min(reward_values) if reward_values else 0.0
                if exact_state_real_rows:
                    scope_quality = "exact_real_state"
                elif partial_state_real_rows:
                    scope_quality = "partial_real_state"
                elif similar_real_rows or real_trade_reward_count > 0:
                    scope_quality = "similar_sql_prior"
                elif counterfactual_reward_count > 0:
                    scope_quality = "counterfactual_prior"
                else:
                    scope_quality = "unqualified"
                usage_boundary = {
                    "contract_version": "agentquant.research_action_value.v1",
                    "lane": action_name,
                    "usable_by": ["analysis_team", "portfolio_manager", "protocol_governor"],
                    "allowed_effects": ["similar_setup_prior", "probe_or_revalidation_context"],
                    "forbidden_effects": [
                        "direct_trade_authority",
                        "real_budget_entry",
                        "scale_position",
                        "open_amplification",
                        "change_lots",
                        "change_direction",
                        "change_target_lots",
                        "change_margin_ratio",
                        "bypass_final_action_contract",
                        "bypass_auditor",
                        "bypass_trader",
                    ],
                    "source_quality": scope_quality,
                    "reward_source": "similar_sql_prior",
                    "must_flow_through_final_action_contract": True,
                    "does_not_create_trade_authority": True,
                }
                signal_calibration = {
                    "contract_version": "agentquant.analysis_signal_calibration.v1",
                    "source_action_value_contract": "agentquant.research_action_value.v1",
                    "source_action_value_lane": action_name,
                    "source_quality": scope_quality,
                    "reward_source": "similar_sql_prior",
                    "usable_by": ["analysis_team"],
                    "allowed_effects": ["evidence_quality_calibration", "setup_reliability_context"],
                    "forbidden_effects": [
                        "trade_authority",
                        "lots",
                        "margin_ratio",
                        "direction_override",
                        "bypass_pm",
                        "bypass_auditor",
                        "bypass_trader",
                    ],
                    "current_data_must_dominate": True,
                }
                item = {
                    "scope_key": (
                        f"similar_sql_rag|{ticker_value or '*'}|{sector_value or '*'}|"
                        f"{side_value or '*'}|{horizon_value or '*'}|{regime_value or '*'}|"
                        f"{setup_value or '*'}|{action_name}"
                    ),
                    "ticker": ticker_value if exact_rows else "*",
                    "side": side_value or "*",
                    "horizon_class": horizon_value or "*",
                    "market_regime": regime_value or "*",
                    "setup_type": setup_value or "*",
                    "data_combo": "similar_alpha_setup_sql",
                    "action_name": action_name,
                    "canonical_action_family": action_family,
                    "sample_count": sample_count,
                    "reward_sum": reward_sum,
                    "reward_mean": reward_mean,
                    "win_rate": win_rate,
                    "confidence_score": confidence_score,
                    "action_preference": action_preference,
                    "canonical_action_value": False,
                    "canonical_action_value_source": "incomplete_trace_not_for_pm_scoring",
                    "consumer_scope": "pm_learning",
                    "action_value_lane": action_name,
                    "learning_lane": action_name,
                    "retrieval_key": (
                        f"{ticker_value or '*'}|{side_value or '*'}|{horizon_value or '*'}|"
                        f"{regime_value or '*'}|{setup_value or '*'}|{action_name}"
                    ),
                    "fallback_retrieval_key": (
                        f"{ticker_value or '*'}|{side_value or '*'}|{horizon_value or '*'}|{action_name}"
                    ),
                    "execution_retrieval_key": f"{ticker_value or '*'}|*|*|{action_name}",
                    "max_position_impact": 0.0,
                    "valid_until": trading_day_value,
                    "payload": {
                        "research_output_contract_version": "agentquant.research_action_value.v1",
                        "source": "similar_alpha_setup_sql",
                        "action_value_lane": action_name,
                        "canonical_action_family": action_family,
                        "canonical_action_value": False,
                        "canonical_action_value_source": "incomplete_trace_not_for_pm_scoring",
                        "consumer_scope": "pm_learning",
                        "learning_lane": action_name,
                        "strict_no_lookahead": True,
                        "date_filter": "alpha_setup_sample.trading_date < decision_date",
                        "decision_date": trading_day_value,
                        "exact_ticker_sample_count": len(exact_real_rows),
                        "exact_ticker_real_trade_sample_count": len(exact_real_rows),
                        "exact_state_real_trade_sample_count": len(exact_state_real_rows),
                        "partial_state_real_trade_sample_count": len(partial_state_real_rows),
                        "similar_real_trade_sample_count": len(similar_real_rows),
                        "amplification_scope_quality": scope_quality,
                        "exact_ticker_counterfactual_sample_count": len(exact_counterfactual_rows),
                        "total_sample_count": sample_count,
                        "real_trade_reward_count": real_trade_reward_count,
                        "counterfactual_reward_count": counterfactual_reward_count,
                        "loss_reward_count": loss_reward_count,
                        "tail_loss_count": tail_loss_count,
                        "worst_reward": worst_reward,
                        "counterfactual_reward_weight": 0.35,
                        "has_counterfactual_samples": counterfactual_reward_count > 0,
                        "counterfactual_prior_only": counterfactual_reward_count > 0 and real_trade_reward_count <= 0,
                        "episode_dates": sorted({str(row.get("trading_date") or "")[:10] for row in action_rows if row.get("trading_date")})[-6:],
                        "prior_only_no_direct_authority": True,
                        "prior_role": "weak_prior_not_action_preference",
                        "prior_direction_hint": prior_direction_hint,
                        "action_preference": "",
                        "canonical_action_preference_source": "none_for_similar_sql_prior",
                        "usage_boundary": usage_boundary,
                        "usable_by": usage_boundary["usable_by"],
                        "allowed_effects": usage_boundary["allowed_effects"],
                        "forbidden_effects": usage_boundary["forbidden_effects"],
                        "signal_calibration": signal_calibration,
                    },
                }
                self._promote_action_value_payload_fields(item)
                result.append(item)
            result.sort(
                key=lambda row: (
                    0 if str(row.get("ticker") or "").upper() == ticker_value else 1,
                    -abs(_safe_float(row.get("reward_sum"))),
                    -_safe_int(row.get("sample_count")),
                )
            )
            return result[: int(limit)]
        except Exception:
            logger.warning("similar_alpha_setup_action_value_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def get_no_trade_opportunity_memory(
        self,
        config_id: str,
        ticker: str,
        sector: Optional[str] = None,
        side: Optional[str] = None,
        horizon_class: Optional[str] = None,
        market_regime: Optional[str] = None,
        trading_date=None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Retrieve no-trade candidate memories with forward counterfactual results."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_reviewer_learning_schema(cursor)
            ticker_value = str(ticker or "").upper()
            sector_value = str(sector or "*")
            params: List[Any] = [config_id]
            where = ["config_id = ?"]
            if ticker_value and ticker_value != "*":
                where.append("ticker IN (?, '*')")
                params.append(ticker_value)
            if sector_value and sector_value != "*":
                where.append("sector IN (?, '*')")
                params.append(sector_value)
            if side and str(side) != "*":
                where.append("side IN (?, '*')")
                params.append(str(side).lower())
            if horizon_class and str(horizon_class) != "*":
                where.append("horizon_class IN (?, '*')")
                params.append(str(horizon_class))
            if market_regime and str(market_regime) != "*":
                where.append("market_regime IN (?, '*')")
                params.append(str(market_regime))
            trading_day_value = self._normalize_trading_day_value(trading_date)
            if trading_day_value:
                where.append("trading_date < ?")
                params.append(trading_day_value)
            cursor.execute(
                f'''
                SELECT *
                FROM no_trade_opportunity_memory
                WHERE {' AND '.join(where)}
                ORDER BY
                    CASE WHEN ticker = ? THEN 0 WHEN ticker = '*' THEN 1 ELSE 2 END,
                    CASE WHEN sector = ? THEN 0 WHEN sector = '*' THEN 1 ELSE 2 END,
                    CASE classification
                        WHEN 'missed_opportunity' THEN 0
                        WHEN 'correct_avoidance' THEN 1
                        ELSE 2
                    END,
                    trading_date DESC,
                    created_at DESC
                LIMIT ?
                ''',
                tuple(params + [ticker_value or "*", sector_value or "*", int(limit)]),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                item["counterfactual_results"] = self._deserialize_json(item.get("counterfactual_results_json")) or []
                item["payload"] = self._deserialize_external_json(item, "payload")
                rows.append(item)
            return rows
        except Exception:
            logger.warning("no_trade_opportunity_memory_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def get_exploratory_hypotheses(
        self,
        config_id: str,
        ticker: str,
        sector: Optional[str] = None,
        side: Optional[str] = None,
        horizon_class: Optional[str] = None,
        market_regime: Optional[str] = None,
        trading_date=None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Retrieve reviewer research hypotheses as non-authoritative priors."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_reviewer_learning_schema(cursor)
            ticker_value = str(ticker or "").upper()
            sector_value = str(sector or "*")
            params: List[Any] = [config_id]
            where = ["config_id = ?", "status IN ('candidate', 'monitoring', 'validated')"]
            if ticker_value and ticker_value != "*":
                where.append("ticker IN (?, '*')")
                params.append(ticker_value)
            if sector_value and sector_value != "*":
                where.append("sector IN (?, '*')")
                params.append(sector_value)
            if side and str(side) != "*":
                where.append("side IN (?, '*')")
                params.append(str(side).lower())
            if horizon_class and str(horizon_class) != "*":
                where.append("horizon_class IN (?, '*')")
                params.append(str(horizon_class))
            if market_regime and str(market_regime) != "*":
                where.append("market_regime IN (?, '*')")
                params.append(str(market_regime))
            trading_day_value = self._normalize_trading_day_value(trading_date)
            if trading_day_value:
                where.append("trading_date < ?")
                where.append("(valid_until IS NULL OR valid_until >= ?)")
                params.extend([trading_day_value, trading_day_value])
            cursor.execute(
                f'''
                SELECT *
                FROM exploratory_hypothesis
                WHERE {' AND '.join(where)}
                ORDER BY
                    CASE WHEN ticker = ? THEN 0 WHEN ticker = '*' THEN 1 ELSE 2 END,
                    CASE WHEN sector = ? THEN 0 WHEN sector = '*' THEN 1 ELSE 2 END,
                    confidence_score DESC,
                    sample_count DESC,
                    created_at DESC
                LIMIT ?
                ''',
                tuple(params + [ticker_value or "*", sector_value or "*", int(limit)]),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                item["payload"] = self._deserialize_external_json(item, "payload")
                rows.append(item)
            return rows
        except Exception:
            logger.warning("exploratory_hypotheses_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def get_analyst_performance(
        self,
        config_id: str,
        ticker: str,
        trading_date=None,
        horizon_class: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Read reviewer-attributed analyst performance for dynamic weighting."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_reviewer_learning_schema(cursor)
            params: List[Any] = [config_id, ticker.upper()]
            where = ["config_id = ?", "ticker IN (?, '*')"]
            if horizon_class:
                where.append("horizon_class IN (?, '*')")
                params.append(str(horizon_class))
            trading_day_value = self._normalize_trading_day_value(trading_date)
            if trading_day_value:
                where.append("last_sample_date IS NOT NULL")
                where.append("last_sample_date < ?")
                where.append("(valid_until IS NULL OR valid_until >= ?)")
                params.extend([trading_day_value, trading_day_value])
            cursor.execute(
                f'''
                SELECT *
                FROM analyst_performance
                WHERE {' AND '.join(where)}
                ORDER BY confidence_score DESC, sample_count DESC, last_updated DESC
                LIMIT ?
                ''',
                tuple(params + [int(limit)]),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                item["payload"] = self._deserialize_json(item.get("payload_json")) or {}
                rows.append(item)
            return rows
        except Exception:
            logger.warning("analyst_performance_unavailable")
            return []
        finally:
            if conn:
                conn.close()

    def save_learning_context_budget(
        self,
        *,
        config_id: str,
        trading_date,
        analyst: str,
        ticker: str,
        selected_digest_ids: List[str],
        selected_chars: int,
        digest_count: int = 0,
        trade_episode_count: int = 0,
        hypothesis_count: int = 0,
        total_context_chars: int = 0,
        dropped_count: int = 0,
        max_items: int = 0,
        max_chars: int = 0,
    ) -> bool:
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            self._ensure_reviewer_learning_schema(cursor)
            cursor.execute(
                '''
                INSERT INTO learning_context_budget (
                    id, config_id, trading_date, analyst, ticker, selected_digest_ids,
                    selected_chars, digest_count, trade_episode_count, hypothesis_count,
                    total_context_chars, dropped_count, max_items, max_chars, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    str(uuid.uuid4()),
                    config_id,
                    self._normalize_trading_day_value(trading_date),
                    str(analyst),
                    ticker.upper(),
                    json.dumps(selected_digest_ids or [], ensure_ascii=False),
                    int(selected_chars),
                    int(digest_count),
                    int(trade_episode_count),
                    int(hypothesis_count),
                    int(total_context_chars),
                    int(dropped_count),
                    int(max_items),
                    int(max_chars),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            return True
        except Exception:
            logger.warning("learning_context_budget_persistence_failed")
            return False
        finally:
            if conn:
                conn.close()

    def _normalize_signal_combo(self, signal_combo: Optional[List[str]]) -> Optional[tuple[str, str, str]]:
        if signal_combo is None:
            return None
        values = [str(item) for item in list(signal_combo or [])]
        while len(values) < 3:
            values.append("Neutral")
        return (values[0], values[1], values[2])

    def _signal_combo_from_snapshot(self, snapshot: Dict[str, Any]) -> tuple[str, str, str]:
        def signal_value(analyst: str) -> str:
            contract = snapshot.get("signal_collection_contract")
            if isinstance(contract, dict):
                for source in contract.get("source_contracts") or []:
                    if not isinstance(source, dict) or source.get("analyst") != analyst:
                        continue
                    item = source.get("action_evidence_contract")
                    if isinstance(item, dict) and item.get("signal"):
                        return str(item.get("signal"))
            return "Neutral"

        return (
            signal_value("technical"),
            signal_value("fundamental"),
            signal_value("commodity_news"),
        )

    def _pm_risk_gate_from_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        contract = snapshot.get("final_action_contract")
        if isinstance(contract, dict):
            return {
                "decision": contract.get("authority_type") or contract.get("final_action"),
                "reasons": contract.get("reason_codes") or [],
                "source": "final_action_contract",
            }
        return {}

    def get_account_drawdown_state(
        self,
        config_id: str,
        trading_date,
        initial_capital: float,
    ) -> Dict[str, Any]:
        """Return account-equity drawdown state before the current trading date."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            trading_day_value = self._normalize_trading_day_value(trading_date)
            cursor.execute(
                '''
                SELECT ds.trading_date,
                       ds.current_balance,
                       ds.current_margin
                FROM daily_settlement ds
                JOIN portfolio p ON ds.portfolio_id = p.id
                WHERE p.config_id = ?
                  AND substr(ds.trading_date, 1, 10) < ?
                ORDER BY substr(ds.trading_date, 1, 10), ds.created_at
                ''',
                (config_id, trading_day_value),
            )
            equities = [float(initial_capital or 0.0)]
            latest_equity = float(initial_capital or 0.0)
            latest_date = None
            for row in cursor.fetchall():
                latest_equity = float(row["current_balance"] or 0.0) + float(row["current_margin"] or 0.0)
                latest_date = self._normalize_trading_day_value(row["trading_date"])
                equities.append(latest_equity)

            peak_equity = max(equities) if equities else latest_equity
            drawdown = (peak_equity - latest_equity) / peak_equity if peak_equity > 0 else 0.0
            return {
                "latest_date": latest_date,
                "latest_equity": latest_equity,
                "peak_equity": peak_equity,
                "drawdown": drawdown,
            }
        except Exception as e:
            logger.error(f"Error getting account drawdown state: {e}")
            return {
                "latest_date": None,
                "latest_equity": float(initial_capital or 0.0),
                "peak_equity": float(initial_capital or 0.0),
                "drawdown": 0.0,
            }
        finally:
            if conn:
                conn.close()

    def get_ticker_loss_state(
        self,
        config_id: str,
        ticker: str,
        trading_date,
        lookback_days: int = 5,
    ) -> Dict[str, Any]:
        """Return recent ticker loss state before the current trading date."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            trading_day_value = self._normalize_trading_day_value(trading_date)
            cursor.execute(
                '''
                SELECT tdp.trading_date, tdp.daily_pnl
                FROM ticker_daily_pnl tdp
                JOIN portfolio p ON tdp.portfolio_id = p.id
                WHERE p.config_id = ?
                  AND tdp.ticker = ?
                  AND substr(tdp.trading_date, 1, 10) < ?
                  AND substr(tdp.trading_date, 1, 10) >= date(?, ?)
                ORDER BY substr(tdp.trading_date, 1, 10) DESC
                ''',
                (config_id, ticker, trading_day_value, trading_day_value, f"-{lookback_days} days"),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            cumulative_pnl = sum(float(row.get("daily_pnl") or 0.0) for row in rows)
            consecutive_losses = 0
            for row in rows:
                if float(row.get("daily_pnl") or 0.0) < 0:
                    consecutive_losses += 1
                else:
                    break
            return {
                "lookback_days": int(lookback_days),
                "trade_days": len(rows),
                "cumulative_pnl": cumulative_pnl,
                "consecutive_loss_days": consecutive_losses,
            }
        except Exception as e:
            logger.error(f"Error getting ticker loss state for {ticker}: {e}")
            return {
                "lookback_days": int(lookback_days),
                "trade_days": 0,
                "cumulative_pnl": 0.0,
                "consecutive_loss_days": 0,
            }
        finally:
            if conn:
                conn.close()

    def get_previous_settlement(
        self,
        portfolio_id: str,
        trading_date: datetime
    ) -> Optional["FuturesSettlementRecord"]:
        """Get the latest settlement record before the given trading date."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # 妫€鏌ヨ〃鏄惁瀛樺湪
            cursor.execute('''
                SELECT name FROM sqlite_master WHERE type='table' AND name='daily_settlement'
            ''')
            if not cursor.fetchone():
                return None

            # 淇锛氶€氳繃portfolio_id鎵惧埌config_id锛岀劧鍚庣敤config_id鏌ヨ涓婁竴浜ゆ槗鏃ョ殑settlement
            # 鍥犱负姣忓ぉ鐨刾ortfolio_id閮戒笉鍚岋紝浣哻onfig_id鐩稿悓
            cursor.execute('''
                SELECT config_id FROM portfolio WHERE id = ?
            ''', (portfolio_id,))
            config_row = cursor.fetchone()
            if not config_row:
                return None
            config_id = config_row[0]

            cursor.execute('''
                SELECT ds.* FROM daily_settlement ds
                JOIN portfolio p ON ds.portfolio_id = p.id
                WHERE p.config_id = ? AND ds.trading_date < ?
                ORDER BY ds.trading_date DESC
                LIMIT 1
            ''', (config_id, trading_date.isoformat()))

            row = cursor.fetchone()
            if row:
                from apis.pandaai.api_model import FuturesSettlementRecord
                # Convert sqlite3.Row to dict for easier access
                row_dict = dict(row)
                positions_detail_val = row_dict.get('positions_snapshot')
                positions_detail = {}
                if positions_detail_val not in (None, '', 'None', '{}'):
                    try:
                        positions_detail = json.loads(positions_detail_val)
                    except Exception:
                        logger.warning("Failed to parse positions_snapshot from daily_settlement")
                return FuturesSettlementRecord(
                    trading_date=row['trading_date'],
                    previous_balance=row['previous_balance'],
                    current_balance=row['current_balance'],
                    previous_account_equity=row_dict.get(
                        'previous_account_equity',
                        row['previous_balance'] + row['previous_margin'],
                    ),
                    current_account_equity=row_dict.get(
                        'current_account_equity',
                        row['current_balance'] + row['current_margin'],
                    ),
                    cash_available=row_dict.get('cash_available', row['current_balance']),
                    reserved_margin=row_dict.get('reserved_margin', row['current_margin']),
                    previous_margin=row['previous_margin'],
                    current_margin=row['current_margin'],
                    margin_as_asset_prev=row['margin_as_asset_prev'],
                    margin_as_asset_curr=row['margin_as_asset_curr'],
                    daily_pnl=row['daily_pnl'],
                    deposit=row['deposit'],
                    withdraw=row['withdraw'],
                    commission=row['commission'],
                    margin_ratio=row['margin_ratio'],
                    is_warning=bool(row['is_warning']),
                    is_liquidation=bool(row['is_liquidation']),
                    positions_detail=positions_detail
                )
            return None
        except Exception as e:
            logger.error(f"Error getting previous settlement: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def save_daily_settlement(
        self,
        portfolio_id: str,
        settlement
    ) -> bool:
        """Save a daily settlement record."""
        conn = None
        try:
            settlement_payload = {
                "trading_date": getattr(settlement, "trading_date", None),
                "previous_balance": getattr(settlement, "previous_balance", None),
                "current_balance": getattr(settlement, "current_balance", None),
                "previous_margin": getattr(settlement, "previous_margin", None),
                "current_margin": getattr(settlement, "current_margin", None),
                "daily_pnl": getattr(settlement, "daily_pnl", None),
                "commission": getattr(settlement, "commission", None),
                "positions_snapshot": getattr(settlement, "positions_detail", None),
            }
            validate_accountant_artifact_boundary(settlement_payload)
            conn = self._get_connection()
            cursor = conn.cursor()

            # 妫€鏌ヨ〃鏄惁瀛樺湪锛屼笉瀛樺湪鍒欏垱寤?
            cursor.execute('''
                SELECT name FROM sqlite_master WHERE type='table' AND name='daily_settlement'
            ''')
            table_exists = cursor.fetchone()

            if not table_exists:
                # 琛ㄤ笉瀛樺湪锛屽垱寤烘柊琛紙浣跨敤澶嶅悎鍞竴绾︽潫锛?
                cursor.execute('''
                    CREATE TABLE daily_settlement (
                        id TEXT PRIMARY KEY,
                        portfolio_id TEXT NOT NULL,
                        trading_date TEXT NOT NULL,
                        previous_balance REAL NOT NULL,
                        current_balance REAL NOT NULL,
                        previous_margin REAL DEFAULT 0,
                        current_margin REAL DEFAULT 0,
                        margin_as_asset_prev REAL DEFAULT 0,
                        margin_as_asset_curr REAL DEFAULT 0,
                        daily_pnl REAL DEFAULT 0,
                        deposit REAL DEFAULT 0,
                        withdraw REAL DEFAULT 0,
                        commission REAL DEFAULT 0,
                        margin_ratio REAL DEFAULT 0,
                        is_warning BOOLEAN DEFAULT 0,
                        is_liquidation BOOLEAN DEFAULT 0,
                        positions_snapshot TEXT,
                        created_at TEXT NOT NULL,
                        UNIQUE(portfolio_id, trading_date),
                        FOREIGN KEY (portfolio_id) REFERENCES portfolio(id)
                    )
                ''')
            else:
                # 琛ㄥ凡瀛樺湪锛屾鏌ユ槸鍚﹂渶瑕佽縼绉伙紙鏃ц〃鍙湁 trading_date UNIQUE锛?
                cursor.execute('''
                    SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_settlement'
                ''')
                result = cursor.fetchone()
                table_sql = result['sql'] if result else ''

                # 濡傛灉鏃х害鏉熷瓨鍦紙trading_date 鍗曠嫭 UNIQUE锛夛紝闇€瑕侀噸寤鸿〃
                if 'trading_date TEXT NOT NULL UNIQUE' in table_sql:
                    logger.info("妫€娴嬪埌鏃х増 daily_settlement 琛ㄧ粨鏋勶紝姝ｅ湪杩佺Щ...")

                    # 澶囦唤鏁版嵁
                    cursor.execute('''
                        CREATE TABLE daily_settlement_backup AS
                        SELECT * FROM daily_settlement
                    ''')

                    # 鍒犻櫎鏃ц〃
                    cursor.execute('DROP TABLE daily_settlement')

                    # 鍒涘缓鏂拌〃锛堜娇鐢ㄥ鍚堝敮涓€绾︽潫锛?
                    cursor.execute('''
                        CREATE TABLE daily_settlement (
                            id TEXT PRIMARY KEY,
                            portfolio_id TEXT NOT NULL,
                            trading_date TEXT NOT NULL,
                            previous_balance REAL NOT NULL,
                            current_balance REAL NOT NULL,
                            previous_margin REAL DEFAULT 0,
                            current_margin REAL DEFAULT 0,
                            margin_as_asset_prev REAL DEFAULT 0,
                            margin_as_asset_curr REAL DEFAULT 0,
                            daily_pnl REAL DEFAULT 0,
                            deposit REAL DEFAULT 0,
                            withdraw REAL DEFAULT 0,
                            commission REAL DEFAULT 0,
                            margin_ratio REAL DEFAULT 0,
                            is_warning BOOLEAN DEFAULT 0,
                            is_liquidation BOOLEAN DEFAULT 0,
                            positions_snapshot TEXT,
                            created_at TEXT NOT NULL,
                            UNIQUE(portfolio_id, trading_date),
                            FOREIGN KEY (portfolio_id) REFERENCES portfolio(id)
                        )
                    ''')

                    # 鎭㈠鏁版嵁
                    cursor.execute('''
                        INSERT INTO daily_settlement
                        SELECT * FROM daily_settlement_backup
                    ''')

                    # 鍒犻櫎澶囦唤琛?
                    cursor.execute('DROP TABLE daily_settlement_backup')

                    logger.info("daily_settlement table migrated to the latest schema")

            _ensure_columns(
                cursor,
                "daily_settlement",
                {
                    "previous_account_equity": "REAL DEFAULT 0",
                    "current_account_equity": "REAL DEFAULT 0",
                    "cash_available": "REAL DEFAULT 0",
                    "reserved_margin": "REAL DEFAULT 0",
                },
            )

            # 灏唒ositions_detail杞崲涓篔SON瀛楃涓插瓨鍌?
            import json
            positions_json = json.dumps(settlement.positions_detail) if settlement.positions_detail else None
            previous_account_equity = getattr(
                settlement,
                "previous_account_equity",
                settlement.previous_balance + settlement.previous_margin,
            )
            current_account_equity = getattr(
                settlement,
                "current_account_equity",
                settlement.current_balance + settlement.current_margin,
            )
            cash_available = getattr(settlement, "cash_available", settlement.current_balance)
            reserved_margin = getattr(settlement, "reserved_margin", settlement.current_margin)

            # 妫€鏌ユ槸鍚﹀凡瀛樺湪鐩稿悓 portfolio_id 鍜?trading_date 鐨勮褰?
            cursor.execute('''
                SELECT id FROM daily_settlement
                WHERE portfolio_id = ? AND trading_date = ?
            ''', (portfolio_id, settlement.trading_date))

            existing_row = cursor.fetchone()

            if existing_row:
                # 璁板綍宸插瓨鍦紝浣跨敤 UPDATE
                settlement_id = existing_row['id']
                cursor.execute('''
                    UPDATE daily_settlement
                    SET previous_balance = ?,
                        current_balance = ?,
                        previous_account_equity = ?,
                        current_account_equity = ?,
                        cash_available = ?,
                        reserved_margin = ?,
                        previous_margin = ?,
                        current_margin = ?,
                        margin_as_asset_prev = ?,
                        margin_as_asset_curr = ?,
                        daily_pnl = ?,
                        deposit = ?,
                        withdraw = ?,
                        commission = ?,
                        margin_ratio = ?,
                        is_warning = ?,
                        is_liquidation = ?,
                        positions_snapshot = ?,
                        created_at = ?
                    WHERE id = ?
                ''', (
                    settlement.previous_balance,
                    settlement.current_balance,
                    previous_account_equity,
                    current_account_equity,
                    cash_available,
                    reserved_margin,
                    settlement.previous_margin,
                    settlement.current_margin,
                    settlement.margin_as_asset_prev,
                    settlement.margin_as_asset_curr,
                    settlement.daily_pnl,
                    settlement.deposit,
                    settlement.withdraw,
                    settlement.commission,
                    settlement.margin_ratio,
                    1 if settlement.is_warning else 0,
                    1 if settlement.is_liquidation else 0,
                    positions_json,
                    datetime.now(timezone.utc).isoformat(),
                    settlement_id
                ))
            else:
                # 璁板綍涓嶅瓨鍦紝浣跨敤 INSERT
                settlement_id = str(uuid.uuid4())
                cursor.execute('''
                    INSERT INTO daily_settlement
                    (id, portfolio_id, trading_date, previous_balance, current_balance,
                     previous_account_equity, current_account_equity, cash_available, reserved_margin,
                     previous_margin, current_margin, margin_as_asset_prev, margin_as_asset_curr,
                     daily_pnl, deposit, withdraw, commission, margin_ratio,
                     is_warning, is_liquidation, positions_snapshot, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    settlement_id,
                    portfolio_id,
                    settlement.trading_date,
                    settlement.previous_balance,
                    settlement.current_balance,
                    previous_account_equity,
                    current_account_equity,
                    cash_available,
                    reserved_margin,
                    settlement.previous_margin,
                    settlement.current_margin,
                    settlement.margin_as_asset_prev,
                    settlement.margin_as_asset_curr,
                    settlement.daily_pnl,
                    settlement.deposit,
                    settlement.withdraw,
                    settlement.commission,
                    settlement.margin_ratio,
                    1 if settlement.is_warning else 0,
                    1 if settlement.is_liquidation else 0,
                    positions_json,
                    datetime.now(timezone.utc).isoformat()
                ))

            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error saving daily settlement: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def save_ticker_daily_pnl(
        self,
        settlement_record: dict
    ) -> bool:
        """Save a single ticker daily pnl record into ticker_daily_pnl."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # 妫€鏌ヨ〃鏄惁瀛樺湪锛屼笉瀛樺湪鍒欏垱寤?
            cursor.execute('''
                SELECT name FROM sqlite_master WHERE type='table' AND name='ticker_daily_pnl'
            ''')
            table_exists = cursor.fetchone()

            if not table_exists:
                # 琛ㄤ笉瀛樺湪锛屽垱寤烘柊琛?
                cursor.execute('''
                    CREATE TABLE ticker_daily_pnl (
                        id TEXT PRIMARY KEY,
                        portfolio_id TEXT NOT NULL,
                        trading_date TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        daily_pnl REAL NOT NULL,
                        commission REAL DEFAULT 0,
                        holding_pnl REAL DEFAULT 0,
                        new_position_pnl REAL DEFAULT 0,
                        close_pnl REAL DEFAULT 0,
                        position_type TEXT,
                        lots REAL NOT NULL,
                        entry_price REAL NOT NULL,
                        settle_price REAL NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(portfolio_id, ticker, trading_date),
                        FOREIGN KEY (portfolio_id) REFERENCES portfolio(id)
                    )
                ''')
                # 鍒涘缓绱㈠紩浠ユ彁楂樻煡璇㈡€ц兘
                cursor.execute('''
                    CREATE INDEX idx_ticker_daily_pnl_portfolio ON ticker_daily_pnl(portfolio_id)
                ''')
                cursor.execute('''
                    CREATE INDEX idx_ticker_daily_pnl_date ON ticker_daily_pnl(trading_date)
                ''')
                cursor.execute('''
                    CREATE INDEX idx_ticker_daily_pnl_ticker ON ticker_daily_pnl(ticker)
                ''')
                conn.commit()
            else:
                _ensure_columns(
                    cursor,
                    "ticker_daily_pnl",
                    {
                        "holding_pnl": "REAL DEFAULT 0",
                        "new_position_pnl": "REAL DEFAULT 0",
                        "close_pnl": "REAL DEFAULT 0",
                    },
                )

            # 妫€鏌ユ槸鍚﹀凡瀛樺湪鐩稿悓 portfolio_id, ticker 鍜?trading_date 鐨勮褰?
            cursor.execute('''
                SELECT id FROM ticker_daily_pnl
                WHERE portfolio_id = ? AND ticker = ? AND trading_date = ?
            ''', (
                settlement_record['portfolio_id'],
                settlement_record['ticker'],
                settlement_record['trading_date']
            ))

            existing_row = cursor.fetchone()

            if existing_row:
                # 璁板綍宸插瓨鍦紝浣跨敤 UPDATE
                record_id = existing_row['id']
                cursor.execute('''
                    UPDATE ticker_daily_pnl
                    SET daily_pnl = ?,
                        commission = ?,
                        holding_pnl = ?,
                        new_position_pnl = ?,
                        close_pnl = ?,
                        position_type = ?,
                        lots = ?,
                        entry_price = ?,
                        settle_price = ?,
                        created_at = ?
                    WHERE id = ?
                ''', (
                    settlement_record['daily_pnl'],
                    settlement_record['commission'],
                    settlement_record.get('holding_pnl', 0.0),
                    settlement_record.get('new_position_pnl', 0.0),
                    settlement_record.get('close_pnl', 0.0),
                    settlement_record['position_type'],
                    settlement_record['lots'],
                    settlement_record['entry_price'],
                    settlement_record['settle_price'],
                    datetime.now(timezone.utc).isoformat(),
                    record_id
                ))
            else:
                # 璁板綍涓嶅瓨鍦紝浣跨敤 INSERT
                record_id = str(uuid.uuid4())
                cursor.execute('''
                    INSERT INTO ticker_daily_pnl
                    (id, portfolio_id, trading_date, ticker, daily_pnl,
                     commission, holding_pnl, new_position_pnl, close_pnl,
                     position_type, lots, entry_price, settle_price, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    record_id,
                    settlement_record['portfolio_id'],
                    settlement_record['trading_date'],
                    settlement_record['ticker'],
                    settlement_record['daily_pnl'],
                    settlement_record['commission'],
                    settlement_record.get('holding_pnl', 0.0),
                    settlement_record.get('new_position_pnl', 0.0),
                    settlement_record.get('close_pnl', 0.0),
                    settlement_record['position_type'],
                    settlement_record['lots'],
                    settlement_record['entry_price'],
                    settlement_record['settle_price'],
                    datetime.now(timezone.utc).isoformat()
                ))

            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error saving ticker daily pnl: {e}")
            return False
        finally:
            if conn:
                conn.close()


## init global instance
# sqlite_db = SQLiteDB()

