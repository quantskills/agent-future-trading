import os
import sqlite3
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables from .env file
# Find .env file in parent directories if not in current directory
env_path = Path(__file__).resolve().parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path)

# Get DB_PATH and convert to absolute path relative to project root
db_path_relative = os.getenv("DB_PATH", "src/assets/agentquant.db")
project_root = Path(__file__).resolve().parent.parent.parent
DB_PATH = str(project_root / db_path_relative)

# Ensure database directory exists
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _get_column_info(cursor: sqlite3.Cursor, table_name: str) -> dict:
    cursor.execute(f"PRAGMA table_info({table_name})")
    rows = cursor.fetchall()
    return {
        row[1]: {
            "type": row[2],
            "notnull": row[3],
            "default": row[4],
            "pk": row[5],
        }
        for row in rows
    }


def _ensure_columns(cursor: sqlite3.Cursor, table_name: str, column_defs: dict) -> None:
    existing = _get_column_info(cursor, table_name)
    for column_name, column_sql in column_defs.items():
        if column_name not in existing:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def _json_artifact_columns(prefix: str) -> dict:
    return {
        f"{prefix}_artifact_path": "TEXT",
        f"{prefix}_sha256": "TEXT",
        f"{prefix}_size": "INTEGER",
        f"{prefix}_summary_json": "TEXT",
    }


def _text_artifact_columns(prefix: str) -> dict:
    return {
        f"{prefix}_artifact_path": "TEXT",
        f"{prefix}_sha256": "TEXT",
        f"{prefix}_size": "INTEGER",
        f"{prefix}_summary_json": "TEXT",
    }


def _create_futures_transactions_table(cursor: sqlite3.Cursor, table_name: str = "futures_transactions") -> None:
    cursor.execute(f'''
    CREATE TABLE IF NOT EXISTS {table_name} (
        id TEXT PRIMARY KEY,
        portfolio_id TEXT NOT NULL,
        config_id TEXT,
        recommendation_id TEXT,
        trading_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        contract_code TEXT,
        action TEXT NOT NULL,
        lots INTEGER NOT NULL,
        price REAL,
        execution_price REAL NOT NULL,
        settle_price REAL,
        contract_multiplier REAL NOT NULL,
        margin_rate REAL NOT NULL,
        margin_used REAL NOT NULL,
        daily_pnl REAL DEFAULT 0,
        commission REAL DEFAULT 0,
        source_type TEXT,
        execution_phase TEXT,
        execution_price_basis TEXT,
        base_price REAL,
        base_price_source TEXT,
        base_price_date TEXT,
        open_price REAL,
        prev_close_price REAL,
        slippage_model TEXT,
        slippage_ticks INTEGER,
        slippage_amount REAL,
        released_margin REAL,
        margin_delta REAL,
        post_trade_margin_used REAL,
        audit_payload TEXT,
        audit_payload_artifact_path TEXT,
        audit_payload_sha256 TEXT,
        audit_payload_size INTEGER,
        audit_payload_summary_json TEXT,
        warning_message TEXT,
        booked_in_settlement BOOLEAN DEFAULT 0,
        justification TEXT,
        llm_prompt TEXT,
        llm_prompt_artifact_path TEXT,
        llm_prompt_sha256 TEXT,
        llm_prompt_size INTEGER,
        llm_prompt_summary_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (portfolio_id) REFERENCES portfolio(id),
        FOREIGN KEY (config_id) REFERENCES config(id)
    )
    ''')


def _rebuild_futures_transactions_table(cursor: sqlite3.Cursor) -> None:
    cursor.execute("ALTER TABLE futures_transactions RENAME TO futures_transactions_legacy")
    _create_futures_transactions_table(cursor, "futures_transactions")

    legacy_columns = _get_column_info(cursor, "futures_transactions_legacy")

    def legacy_expr(column_name: str, default_sql: str = "NULL") -> str:
        return f"ft.{column_name}" if column_name in legacy_columns else default_sql

    price_expr = legacy_expr("price", legacy_expr("execution_price", "0"))
    execution_price_expr = legacy_expr("execution_price", legacy_expr("price", "0"))
    config_id_expr = legacy_expr("config_id", "p.config_id")

    cursor.execute(f'''
    INSERT INTO futures_transactions (
        id, portfolio_id, config_id, recommendation_id, trading_date, ticker, contract_code,
        action, lots, price, execution_price, settle_price, contract_multiplier,
        margin_rate, margin_used, daily_pnl, commission, source_type, execution_phase,
        execution_price_basis, base_price, base_price_source, base_price_date, open_price,
        prev_close_price, slippage_model, slippage_ticks, slippage_amount, released_margin,
        margin_delta, post_trade_margin_used, audit_payload,
        audit_payload_artifact_path, audit_payload_sha256,
        audit_payload_size, audit_payload_summary_json,
        warning_message, booked_in_settlement, justification, llm_prompt,
        llm_prompt_artifact_path, llm_prompt_sha256,
        llm_prompt_size, llm_prompt_summary_json,
        created_at
    )
    SELECT
        ft.id,
        ft.portfolio_id,
        {config_id_expr} AS config_id,
        {legacy_expr("recommendation_id")} AS recommendation_id,
        ft.trading_date,
        ft.ticker,
        {legacy_expr("contract_code")} AS contract_code,
        ft.action,
        ft.lots,
        {price_expr} AS price,
        {execution_price_expr} AS execution_price,
        {legacy_expr("settle_price")} AS settle_price,
        ft.contract_multiplier,
        ft.margin_rate,
        ft.margin_used,
        {legacy_expr("daily_pnl", "0")} AS daily_pnl,
        {legacy_expr("commission", "0")} AS commission,
        {legacy_expr("source_type", "'strategy'")} AS source_type,
        {legacy_expr("execution_phase", "'legacy'")} AS execution_phase,
        {legacy_expr("execution_price_basis")} AS execution_price_basis,
        {legacy_expr("base_price")} AS base_price,
        {legacy_expr("base_price_source")} AS base_price_source,
        {legacy_expr("base_price_date")} AS base_price_date,
        {legacy_expr("open_price")} AS open_price,
        {legacy_expr("prev_close_price")} AS prev_close_price,
        {legacy_expr("slippage_model")} AS slippage_model,
        {legacy_expr("slippage_ticks")} AS slippage_ticks,
        {legacy_expr("slippage_amount")} AS slippage_amount,
        {legacy_expr("released_margin")} AS released_margin,
        {legacy_expr("margin_delta")} AS margin_delta,
        {legacy_expr("post_trade_margin_used")} AS post_trade_margin_used,
        {legacy_expr("audit_payload")} AS audit_payload,
        {legacy_expr("audit_payload_artifact_path")} AS audit_payload_artifact_path,
        {legacy_expr("audit_payload_sha256")} AS audit_payload_sha256,
        {legacy_expr("audit_payload_size")} AS audit_payload_size,
        {legacy_expr("audit_payload_summary_json")} AS audit_payload_summary_json,
        {legacy_expr("warning_message")} AS warning_message,
        {legacy_expr("booked_in_settlement", "0")} AS booked_in_settlement,
        {legacy_expr("justification")} AS justification,
        {legacy_expr("llm_prompt")} AS llm_prompt,
        {legacy_expr("llm_prompt_artifact_path")} AS llm_prompt_artifact_path,
        {legacy_expr("llm_prompt_sha256")} AS llm_prompt_sha256,
        {legacy_expr("llm_prompt_size")} AS llm_prompt_size,
        {legacy_expr("llm_prompt_summary_json")} AS llm_prompt_summary_json,
        ft.created_at
    FROM futures_transactions_legacy ft
    LEFT JOIN portfolio p ON ft.portfolio_id = p.id
    ''')

    cursor.execute("DROP TABLE futures_transactions_legacy")


def _ensure_futures_transactions_schema(cursor: sqlite3.Cursor) -> None:
    if not _table_exists(cursor, "futures_transactions"):
        _create_futures_transactions_table(cursor)
    else:
        existing = _get_column_info(cursor, "futures_transactions")
        required_columns = {
            "config_id",
            "recommendation_id",
            "contract_code",
            "price",
            "execution_price",
            "source_type",
            "execution_phase",
            "execution_price_basis",
            "base_price",
            "base_price_source",
            "base_price_date",
            "open_price",
            "prev_close_price",
            "slippage_model",
            "slippage_ticks",
            "slippage_amount",
            "released_margin",
            "margin_delta",
            "post_trade_margin_used",
                "audit_payload",
                "warning_message",
                "booked_in_settlement",
            }
        settle_notnull = existing.get("settle_price", {}).get("notnull", 0) == 1
        if settle_notnull or not required_columns.issubset(existing):
            _rebuild_futures_transactions_table(cursor)

        _ensure_columns(
            cursor,
            "futures_transactions",
            {
                "config_id": "TEXT",
                "recommendation_id": "TEXT",
                "contract_code": "TEXT",
                "price": "REAL",
                "execution_price": "REAL NOT NULL DEFAULT 0",
                "source_type": "TEXT",
                "execution_phase": "TEXT",
                "execution_price_basis": "TEXT",
                "base_price": "REAL",
                "base_price_source": "TEXT",
                "base_price_date": "TEXT",
                "open_price": "REAL",
                "prev_close_price": "REAL",
                "slippage_model": "TEXT",
                "slippage_ticks": "INTEGER",
                "slippage_amount": "REAL",
                "released_margin": "REAL",
                "margin_delta": "REAL",
                "post_trade_margin_used": "REAL",
                "audit_payload": "TEXT",
                **_json_artifact_columns("audit_payload"),
                "warning_message": "TEXT",
                "booked_in_settlement": "BOOLEAN DEFAULT 0",
                **_text_artifact_columns("llm_prompt"),
            },
        )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_futures_transactions_config_date "
        "ON futures_transactions(config_id, trading_date)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_futures_transactions_portfolio_date "
        "ON futures_transactions(portfolio_id, trading_date)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_futures_transactions_recommendation "
        "ON futures_transactions(recommendation_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_futures_transactions_ticker "
        "ON futures_transactions(ticker)"
    )


def _ensure_futures_recommendation_schema(cursor: sqlite3.Cursor) -> None:
    if not _table_exists(cursor, "futures_recommendation"):
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS futures_recommendation (
                id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                reference_portfolio_id TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                effective_trade_date TEXT NOT NULL,
                source_type TEXT NOT NULL,
                underlying_code TEXT NOT NULL,
                from_contract TEXT,
                to_contract TEXT,
                contract_code TEXT,
                action TEXT NOT NULL,
                lots INTEGER NOT NULL,
                base_price REAL,
                base_price_source TEXT,
                base_price_date TEXT,
                open_price REAL,
                prev_close_price REAL,
                slippage_model TEXT,
                slippage_ticks INTEGER,
                slippage_amount REAL,
                execution_price REAL,
                justification TEXT,
                signal_snapshot TEXT,
                audit_payload TEXT,
                warning_message TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                FOREIGN KEY (config_id) REFERENCES config(id),
                FOREIGN KEY (reference_portfolio_id) REFERENCES portfolio(id)
            )
            '''
        )
    else:
        _ensure_columns(
            cursor,
            "futures_recommendation",
            {
                "audit_payload": "TEXT",
            },
        )
    artifact_columns = {}
    artifact_columns.update(_json_artifact_columns("signal_snapshot"))
    artifact_columns.update(_json_artifact_columns("audit_payload"))
    _ensure_columns(cursor, "futures_recommendation", artifact_columns)


def _ensure_futures_intraday_decision_schema(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS futures_intraday_decision (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            recommendation_id TEXT,
            ticker TEXT NOT NULL,
            contract_code TEXT,
            slot_datetime TEXT,
            mode TEXT,
            cutoff_datetime TEXT,
            decision TEXT NOT NULL,
            trigger_reason TEXT,
            base_price REAL,
            execution_price_candidate REAL,
            features_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(config_id, trading_date, recommendation_id, slot_datetime),
            FOREIGN KEY (config_id) REFERENCES config(id),
            FOREIGN KEY (recommendation_id) REFERENCES futures_recommendation(id)
        )
        '''
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_futures_intraday_decision_lookup "
        "ON futures_intraday_decision(config_id, trading_date, ticker)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_futures_intraday_decision_recommendation "
        "ON futures_intraday_decision(recommendation_id)"
    )


def _ensure_strategy_memory_schema(cursor: sqlite3.Cursor) -> None:
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
            updated_at TEXT NOT NULL,
            valid_until TEXT,
            payload_json TEXT,
            UNIQUE(config_id, ticker, side, signal_combo, source),
            FOREIGN KEY (config_id) REFERENCES config(id)
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


def _ensure_reviewer_learning_schema(cursor: sqlite3.Cursor) -> None:
    """Create reviewer-owned memory, learning, and overlay tables."""
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS strategy_memory_history (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            signal_combo TEXT NOT NULL DEFAULT '*',
            memory_state TEXT NOT NULL,
            sample_count INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0,
            net_pnl REAL DEFAULT 0,
            avg_pnl REAL DEFAULT 0,
            confidence_score REAL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'reviewer_snapshot',
            reason TEXT,
            snapshot_at TEXT NOT NULL,
            payload_json TEXT,
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS signal_context_history (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            recommendation_id TEXT,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            signal_combo TEXT NOT NULL DEFAULT '[]',
            signal_template TEXT NOT NULL,
            horizon_class TEXT DEFAULT 'unknown',
            expected_horizon_days INTEGER DEFAULT 0,
            market_regime TEXT DEFAULT 'unknown',
            price_stage TEXT DEFAULT 'unknown',
            price_percentile REAL,
            trigger_type TEXT DEFAULT 'unknown',
            entry_type TEXT DEFAULT 'unknown',
            invalidation_level REAL,
            target_return REAL,
            analyst_signals_json TEXT,
            market_confirmation_json TEXT,
            pre_open_plan_json TEXT,
            outcome_status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS signal_template_performance (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            signal_template TEXT NOT NULL,
            horizon_class TEXT DEFAULT 'unknown',
            market_regime TEXT DEFAULT 'unknown',
            sample_count INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0,
            net_pnl REAL DEFAULT 0,
            avg_pnl REAL DEFAULT 0,
            profit_factor REAL DEFAULT 0,
            confidence_score REAL DEFAULT 0,
            last_updated TEXT NOT NULL,
            valid_until TEXT,
            payload_json TEXT,
            UNIQUE(config_id, ticker, side, signal_template, horizon_class, market_regime),
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS analyst_performance (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            analyst TEXT NOT NULL,
            ticker TEXT NOT NULL,
            sector TEXT DEFAULT 'unknown',
            horizon_class TEXT DEFAULT 'unknown',
            signal_side TEXT DEFAULT 'neutral',
            sample_count INTEGER DEFAULT 0,
            hit_rate REAL DEFAULT 0,
            avg_pnl REAL DEFAULT 0,
            net_pnl REAL DEFAULT 0,
            confidence_score REAL DEFAULT 0,
            last_updated TEXT NOT NULL,
            valid_until TEXT,
            payload_json TEXT,
            UNIQUE(config_id, analyst, ticker, sector, horizon_class, signal_side),
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS adaptive_policy_state (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            ticker TEXT NOT NULL DEFAULT '*',
            side TEXT NOT NULL DEFAULT '*',
            signal_template TEXT NOT NULL DEFAULT '*',
            horizon_class TEXT NOT NULL DEFAULT '*',
            market_regime TEXT NOT NULL DEFAULT '*',
            policy_type TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            multiplier REAL DEFAULT 1,
            confidence_score REAL DEFAULT 0,
            sample_count INTEGER DEFAULT 0,
            reason TEXT,
            source_event_id TEXT,
            created_at TEXT NOT NULL,
            valid_until TEXT,
            payload_json TEXT,
            active INTEGER DEFAULT 1,
            UNIQUE(config_id, ticker, side, signal_template, horizon_class, market_regime, policy_type),
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS capital_deployment_state (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            capital_base REAL DEFAULT 0,
            current_margin REAL DEFAULT 0,
            current_margin_ratio REAL DEFAULT 0,
            target_margin_ratio_min REAL DEFAULT 0.16,
            target_margin_ratio_max REAL DEFAULT 0.20,
            target_margin_abs_min REAL DEFAULT 0,
            target_margin_abs_max REAL DEFAULT 0,
            underutilization_breach INTEGER DEFAULT 0,
            overutilization_breach INTEGER DEFAULT 0,
            margin_gap_to_min REAL DEFAULT 0,
            capital_allocation_tier TEXT DEFAULT 'unknown',
            reason_bucket TEXT DEFAULT 'unknown',
            deployment_plan_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(config_id, trading_date),
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    _ensure_columns(
        cursor,
        "signal_context_history",
        {
            "price_percentile": "REAL",
            "invalidation_level": "REAL",
            "target_return": "REAL",
            **_json_artifact_columns("analyst_signals"),
            **_json_artifact_columns("market_confirmation"),
            **_json_artifact_columns("pre_open_plan"),
        },
    )
    _ensure_columns(
        cursor,
        "capital_deployment_state",
        {
            "capital_allocation_tier": "TEXT DEFAULT 'unknown'",
        },
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS config_learning_overlay (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            param_key TEXT NOT NULL,
            learned_value_json TEXT NOT NULL,
            previous_value_json TEXT,
            scope_type TEXT NOT NULL DEFAULT 'global',
            scope_key TEXT NOT NULL DEFAULT '*',
            source TEXT NOT NULL DEFAULT 'reviewer',
            confidence_score REAL DEFAULT 0,
            sample_count INTEGER DEFAULT 0,
            reason TEXT,
            source_event_id TEXT,
            rollback_value_json TEXT,
            created_at TEXT NOT NULL,
            valid_until TEXT,
            active INTEGER DEFAULT 1,
            UNIQUE(config_id, param_key, scope_type, scope_key, source),
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    _ensure_columns(
        cursor,
        "adaptive_policy_state",
        {
            "source_event_id": "TEXT",
        },
    )
    _ensure_columns(
        cursor,
        "config_learning_overlay",
        {
            "source_event_id": "TEXT",
            "rollback_value_json": "TEXT",
        },
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS analyst_learning_digest (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            analyst TEXT NOT NULL,
            ticker TEXT NOT NULL DEFAULT '*',
            sector TEXT NOT NULL DEFAULT '*',
            horizon_class TEXT NOT NULL DEFAULT '*',
            market_regime TEXT NOT NULL DEFAULT '*',
            digest_text TEXT NOT NULL,
            confidence_score REAL DEFAULT 0,
            sample_count INTEGER DEFAULT 0,
            source_event_id TEXT,
            created_at TEXT NOT NULL,
            valid_until TEXT,
            accepted INTEGER DEFAULT 1,
            payload_json TEXT,
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS learning_event_log (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            event_type TEXT NOT NULL,
            agent TEXT NOT NULL DEFAULT 'reviewer',
            scope_type TEXT NOT NULL DEFAULT 'global',
            scope_key TEXT NOT NULL DEFAULT '*',
            evidence_json TEXT,
            action_json TEXT,
            verifier TEXT DEFAULT 'deterministic_reviewer',
            created_at TEXT NOT NULL,
            status TEXT DEFAULT 'applied',
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS reviewer_llm_notes (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            evidence_pack_id TEXT NOT NULL,
            ticker TEXT DEFAULT '*',
            raw_prompt TEXT,
            raw_response TEXT,
            created_at TEXT NOT NULL,
            payload_json TEXT,
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    _ensure_columns(
        cursor,
        "reviewer_llm_notes",
        {
            **_text_artifact_columns("raw_prompt"),
            **_text_artifact_columns("raw_response"),
            **_json_artifact_columns("payload"),
        },
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS causal_review_candidate (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            evidence_pack_id TEXT NOT NULL,
            ticker TEXT DEFAULT '*',
            side TEXT DEFAULT '*',
            candidate_type TEXT NOT NULL,
            confidence_score REAL DEFAULT 0,
            rule_validation_status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            valid_until TEXT,
            payload_json TEXT,
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS provisional_policy_state (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            ticker TEXT NOT NULL DEFAULT '*',
            side TEXT NOT NULL DEFAULT '*',
            signal_template TEXT NOT NULL DEFAULT '*',
            horizon_class TEXT NOT NULL DEFAULT '*',
            policy_action TEXT NOT NULL,
            multiplier REAL DEFAULT 1,
            confidence_score REAL DEFAULT 0,
            trigger_type TEXT,
            sample_count INTEGER DEFAULT 0,
            reason TEXT,
            rollback_value_json TEXT,
            created_at TEXT NOT NULL,
            valid_until TEXT,
            active INTEGER DEFAULT 1,
            payload_json TEXT,
            UNIQUE(config_id, ticker, side, signal_template, horizon_class, policy_action),
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS learning_context_budget (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            analyst TEXT NOT NULL,
            ticker TEXT NOT NULL,
            selected_digest_ids TEXT,
            selected_chars INTEGER DEFAULT 0,
            digest_count INTEGER DEFAULT 0,
            trade_episode_count INTEGER DEFAULT 0,
            hypothesis_count INTEGER DEFAULT 0,
            total_context_chars INTEGER DEFAULT 0,
            dropped_count INTEGER DEFAULT 0,
            max_items INTEGER DEFAULT 0,
            max_chars INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS trade_episode_memory (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            sector TEXT DEFAULT 'unknown',
            signal_template TEXT NOT NULL DEFAULT '*',
            signal_combo TEXT NOT NULL DEFAULT '*',
            horizon_class TEXT DEFAULT 'unknown',
            market_regime TEXT DEFAULT 'unknown',
            episode_date TEXT,
            first_seen_at TEXT,
            last_reviewed_at TEXT,
            open_date TEXT,
            close_date TEXT,
            holding_days INTEGER DEFAULT 0,
            net_pnl REAL DEFAULT 0,
            return_on_notional REAL DEFAULT 0,
            outcome_label TEXT DEFAULT 'flat',
            lesson_text TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(config_id, ticker, side, open_date, close_date, signal_template),
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS no_trade_opportunity_memory (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            sector TEXT DEFAULT 'unknown',
            signal_template TEXT NOT NULL DEFAULT '*',
            signal_combo TEXT NOT NULL DEFAULT '*',
            horizon_class TEXT DEFAULT 'unknown',
            market_regime TEXT DEFAULT 'unknown',
            opportunity_type TEXT DEFAULT 'unknown',
            opportunity_layer TEXT DEFAULT 'direction_only',
            candidate_lots INTEGER DEFAULT 1,
            shadow_lots INTEGER DEFAULT 1,
            shadow_entry_price REAL,
            pm_reason TEXT,
            auditor_reason TEXT,
            execution_reason TEXT,
            evidence_summary TEXT,
            status TEXT DEFAULT 'open',
            classification TEXT DEFAULT 'pending',
            shadow_results_json TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            last_reviewed_at TEXT,
            UNIQUE(config_id, trading_date, ticker, side, signal_template),
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS exploratory_hypothesis (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            scope_type TEXT NOT NULL DEFAULT 'research',
            scope_key TEXT NOT NULL DEFAULT '*',
            ticker TEXT NOT NULL DEFAULT '*',
            sector TEXT NOT NULL DEFAULT '*',
            side TEXT NOT NULL DEFAULT '*',
            horizon_class TEXT NOT NULL DEFAULT '*',
            market_regime TEXT NOT NULL DEFAULT '*',
            hypothesis_text TEXT NOT NULL,
            evidence_summary TEXT,
            suggested_use TEXT,
            confidence_score REAL DEFAULT 0,
            sample_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'candidate',
            created_at TEXT NOT NULL,
            valid_until TEXT,
            payload_json TEXT,
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        '''
    )
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
    _ensure_columns(
        cursor,
        "no_trade_opportunity_memory",
        {
            "opportunity_type": "TEXT DEFAULT 'unknown'",
            "opportunity_layer": "TEXT DEFAULT 'direction_only'",
            "candidate_lots": "INTEGER DEFAULT 1",
            "shadow_lots": "INTEGER DEFAULT 1",
            "shadow_entry_price": "REAL",
            "pm_reason": "TEXT",
            "auditor_reason": "TEXT",
            "execution_reason": "TEXT",
            "evidence_summary": "TEXT",
            "status": "TEXT DEFAULT 'open'",
            "classification": "TEXT DEFAULT 'pending'",
            "shadow_results_json": "TEXT",
            "last_reviewed_at": "TEXT",
            **_json_artifact_columns("payload"),
        },
    )
    _ensure_columns(cursor, "exploratory_hypothesis", _json_artifact_columns("payload"))
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_memory_history_lookup "
        "ON strategy_memory_history(config_id, trading_date, ticker, side)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_signal_context_lookup "
        "ON signal_context_history(config_id, trading_date, ticker, side)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_template_perf_lookup "
        "ON signal_template_performance(config_id, ticker, side, signal_template)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_analyst_perf_lookup "
        "ON analyst_performance(config_id, analyst, ticker, horizon_class)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_adaptive_policy_lookup "
        "ON adaptive_policy_state(config_id, ticker, side, policy_type, active)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_config_overlay_lookup "
        "ON config_learning_overlay(config_id, param_key, active, valid_until)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_learning_digest_lookup "
        "ON analyst_learning_digest(config_id, analyst, ticker, sector, horizon_class, accepted, valid_until)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_learning_event_lookup "
        "ON learning_event_log(config_id, trading_date, event_type)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_trade_episode_lookup "
        "ON trade_episode_memory(config_id, ticker, sector, horizon_class, market_regime, close_date)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_no_trade_opportunity_lookup "
        "ON no_trade_opportunity_memory(config_id, ticker, sector, horizon_class, market_regime, trading_date)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_no_trade_opportunity_shadow "
        "ON no_trade_opportunity_memory(config_id, status, classification, trading_date)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_exploratory_hypothesis_lookup "
        "ON exploratory_hypothesis(config_id, ticker, sector, horizon_class, market_regime, status, valid_until)"
    )


def init_database():
    """Initialize the SQLite database and create tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        # Create config table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS config (
            id VARCHAR(36) PRIMARY KEY,
            exp_name VARCHAR(100) NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tickers JSON NOT NULL,
            has_planner BOOLEAN NOT NULL DEFAULT FALSE,
            llm_model VARCHAR(50) NOT NULL,
            llm_provider VARCHAR(50) NOT NULL
        )
        ''')

        # Create portfolio table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS portfolio (
            id VARCHAR(36) PRIMARY KEY,
            config_id VARCHAR(36) NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            trading_date TIMESTAMP NOT NULL,
            cashflow DECIMAL(15,2) NOT NULL,
            account_equity DECIMAL(15,2) DEFAULT 0,
            cash_available DECIMAL(15,2) DEFAULT 0,
            total_assets DECIMAL(15,2) NOT NULL,
            positions JSON NOT NULL,
            previous_portfolio_id TEXT,
            is_recovery_portfolio BOOLEAN DEFAULT 0,
            settlement_event_id TEXT,
            margin_used DECIMAL(15,2) DEFAULT 0,
            available_cash DECIMAL(15,2) DEFAULT 0,
            daily_settlement_pnl DECIMAL(15,2) DEFAULT 0,
            leverage DECIMAL(10,2) DEFAULT 1.0,
            FOREIGN KEY (config_id) REFERENCES config(id),
            FOREIGN KEY (previous_portfolio_id) REFERENCES portfolio(id),
            FOREIGN KEY (settlement_event_id) REFERENCES portfolio_forced_settlement(id)
        )
        ''')
        _ensure_columns(
            cursor,
            "portfolio",
            {
                "account_equity": "DECIMAL(15,2) DEFAULT 0",
                "cash_available": "DECIMAL(15,2) DEFAULT 0",
            },
        )

        # Create signal table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS signal (
            id VARCHAR(36) PRIMARY KEY,
            portfolio_id VARCHAR(36) NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ticker VARCHAR(10) NOT NULL,
            llm_prompt TEXT NOT NULL,
            analyst VARCHAR(50) NOT NULL,
            signal VARCHAR(10) NOT NULL,
            justification TEXT NOT NULL,
            artifact_json TEXT,
            business_quality_score REAL DEFAULT 0,
            horizon_class TEXT DEFAULT 'unknown',
            template_name TEXT DEFAULT 'unknown',
            FOREIGN KEY (portfolio_id) REFERENCES portfolio(id)
        )
        ''')
        _ensure_columns(
            cursor,
            "signal",
            {
                "artifact_json": "TEXT",
                "business_quality_score": "REAL DEFAULT 0",
                "horizon_class": "TEXT DEFAULT 'unknown'",
                "template_name": "TEXT DEFAULT 'unknown'",
                **_text_artifact_columns("llm_prompt"),
                **_json_artifact_columns("artifact_json"),
            },
        )

        # Create portfolio_forced_settlement table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS portfolio_forced_settlement (
            id TEXT PRIMARY KEY,
            original_portfolio_id TEXT NOT NULL,
            settlement_date TEXT NOT NULL,
            settlement_reason TEXT NOT NULL,
            pre_settlement_cashflow REAL NOT NULL,
            pre_settlement_positions TEXT NOT NULL,
            forced_liquidation_details TEXT NOT NULL,
            post_settlement_cashflow REAL NOT NULL,
            total_realized_pnl REAL DEFAULT 0,
            total_commission REAL DEFAULT 0,
            new_portfolio_id TEXT,
            remaining_capital REAL NOT NULL,
            is_forced_settlement BOOLEAN DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (original_portfolio_id) REFERENCES portfolio(id),
            FOREIGN KEY (new_portfolio_id) REFERENCES portfolio(id)
        )
        ''')

        # New dual-phase tables
        _ensure_futures_recommendation_schema(cursor)

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS trading_day_phase (
            id TEXT PRIMARY KEY,
            config_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            phase TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            message TEXT,
            UNIQUE(config_id, trading_date, phase),
            FOREIGN KEY (config_id) REFERENCES config(id)
        )
        ''')

        _ensure_futures_transactions_schema(cursor)
        _ensure_futures_intraday_decision_schema(cursor)
        _ensure_strategy_memory_schema(cursor)
        _ensure_reviewer_learning_schema(cursor)

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_settlement (
            id TEXT PRIMARY KEY,
            portfolio_id TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            previous_balance REAL NOT NULL,
            current_balance REAL NOT NULL,
            previous_account_equity REAL DEFAULT 0,
            current_account_equity REAL DEFAULT 0,
            cash_available REAL DEFAULT 0,
            reserved_margin REAL DEFAULT 0,
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

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticker_daily_pnl (
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

        # Create indices for better query performance
        _ensure_columns(
            cursor,
            "ticker_daily_pnl",
            {
                "holding_pnl": "REAL DEFAULT 0",
                "new_position_pnl": "REAL DEFAULT 0",
                "close_pnl": "REAL DEFAULT 0",
            },
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_config_exp_name ON config(exp_name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_portfolio_updated ON portfolio(updated_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_portfolio_trading_date ON portfolio(trading_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_signal_portfolio ON signal(portfolio_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_signal_updated ON signal(updated_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_signal_analyst ON signal(analyst)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_forced_settlement_original ON portfolio_forced_settlement(original_portfolio_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_forced_settlement_date ON portfolio_forced_settlement(settlement_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_futures_recommendation_effective_date ON futures_recommendation(config_id, effective_trade_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_futures_recommendation_reference_portfolio ON futures_recommendation(reference_portfolio_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trading_day_phase_lookup ON trading_day_phase(config_id, trading_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_daily_settlement_portfolio_date ON daily_settlement(portfolio_id, trading_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ticker_daily_pnl_portfolio ON ticker_daily_pnl(portfolio_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ticker_daily_pnl_date ON ticker_daily_pnl(trading_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ticker_daily_pnl_ticker ON ticker_daily_pnl(ticker)')

        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_database()
    print(f"Database initialized at {DB_PATH}")
