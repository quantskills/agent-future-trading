import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional, List
from database.sqlite_setup import DB_PATH
from util.logger import logger


CORE_COLUMNS = [
    'id', 'config_id', 'updated_at', 'trading_date_start', 'trading_date_end',
    'total_return', 'annualized_return', 'sharpe_ratio', 'max_drawdown',
    'win_rate', 'total_trades', 'winning_trades', 'losing_trades',
    'avg_return_per_trade', 'volatility', 'initial_capital', 'final_capital',
    'peak_margin_ratio', 'avg_margin_ratio', 'warning_days', 'liquidation_events',
    'total_commission', 'avg_daily_pnl', 'total_settlement_pnl',
    'max_margin_usage', 'long_trades', 'short_trades',
    'active_long_positions', 'active_short_positions', 'commission_rate',
    'avg_leverage', 'margin_call_count',
]

QUALITY_COLUMNS = [
    'net_settlement_pnl',
    'account_gross_pnl',
    'account_net_pnl',
    'account_net_win_days',
    'account_net_loss_days',
    'account_net_flat_days',
    'calmar_ratio',
    'return_drawdown_ratio',
    'profit_factor',
    'payoff_ratio',
    'trade_expectancy',
    'avg_win_pnl',
    'avg_loss_pnl',
    'max_trade_gain',
    'max_trade_loss',
    'max_consecutive_losing_trades',
    'max_consecutive_losing_days',
    'return_on_avg_margin',
    'commission_drag_ratio',
    'margin_cap_violation_days',
    'ticker_abs_contribution_top3_ratio',
    'profitable_ticker_count',
    'losing_ticker_count',
    'top_profit_ticker',
    'top_profit_ticker_pnl',
    'worst_loss_ticker',
    'worst_loss_ticker_pnl',
    'long_trade_net_pnl',
    'short_trade_net_pnl',
    'strategy_gross_pnl',
    'strategy_commission',
    'strategy_total_trades',
    'strategy_winning_trades',
    'strategy_losing_trades',
    'strategy_flat_trades',
    'strategy_win_rate',
    'strategy_net_pnl',
    'strategy_profit_factor',
    'trade_episode_memory_count',
    'exploratory_hypothesis_count',
    'learning_context_budget_rows',
    'learning_context_with_episode_rows',
    'learning_context_with_hypothesis_rows',
    'learning_context_with_memory_ratio',
    'avg_learning_context_chars',
]

EXPECTED_COLUMNS = CORE_COLUMNS + QUALITY_COLUMNS

COLUMN_DEFINITIONS = {
    'trading_date_start': 'TIMESTAMP',
    'trading_date_end': 'TIMESTAMP',
    'total_return': 'DECIMAL(10,4)',
    'annualized_return': 'DECIMAL(10,4)',
    'sharpe_ratio': 'DECIMAL(10,4)',
    'max_drawdown': 'DECIMAL(10,4)',
    'win_rate': 'DECIMAL(10,4)',
    'total_trades': 'INTEGER',
    'winning_trades': 'INTEGER',
    'losing_trades': 'INTEGER',
    'avg_return_per_trade': 'DECIMAL(10,4)',
    'volatility': 'DECIMAL(10,4)',
    'initial_capital': 'DECIMAL(15,2)',
    'final_capital': 'DECIMAL(15,2)',
    'peak_margin_ratio': 'DECIMAL(10,4) DEFAULT 0',
    'avg_margin_ratio': 'DECIMAL(10,4) DEFAULT 0',
    'warning_days': 'INTEGER DEFAULT 0',
    'liquidation_events': 'INTEGER DEFAULT 0',
    'total_commission': 'DECIMAL(15,2) DEFAULT 0',
    'avg_daily_pnl': 'DECIMAL(15,2) DEFAULT 0',
    'total_settlement_pnl': 'DECIMAL(15,2) DEFAULT 0',
    'max_margin_usage': 'DECIMAL(10,4) DEFAULT 0',
    'long_trades': 'INTEGER DEFAULT 0',
    'short_trades': 'INTEGER DEFAULT 0',
    'active_long_positions': 'INTEGER DEFAULT 0',
    'active_short_positions': 'INTEGER DEFAULT 0',
    'commission_rate': 'DECIMAL(10,4) DEFAULT 0',
    'avg_leverage': 'DECIMAL(10,2) DEFAULT 1.0',
    'margin_call_count': 'INTEGER DEFAULT 0',
    'net_settlement_pnl': 'DECIMAL(15,2) DEFAULT 0',
    'account_gross_pnl': 'DECIMAL(15,2) DEFAULT 0',
    'account_net_pnl': 'DECIMAL(15,2) DEFAULT 0',
    'account_net_win_days': 'INTEGER DEFAULT 0',
    'account_net_loss_days': 'INTEGER DEFAULT 0',
    'account_net_flat_days': 'INTEGER DEFAULT 0',
    'calmar_ratio': 'DECIMAL(10,4) DEFAULT 0',
    'return_drawdown_ratio': 'DECIMAL(10,4) DEFAULT 0',
    'profit_factor': 'DECIMAL(10,4) DEFAULT 0',
    'payoff_ratio': 'DECIMAL(10,4) DEFAULT 0',
    'trade_expectancy': 'DECIMAL(15,2) DEFAULT 0',
    'avg_win_pnl': 'DECIMAL(15,2) DEFAULT 0',
    'avg_loss_pnl': 'DECIMAL(15,2) DEFAULT 0',
    'max_trade_gain': 'DECIMAL(15,2) DEFAULT 0',
    'max_trade_loss': 'DECIMAL(15,2) DEFAULT 0',
    'max_consecutive_losing_trades': 'INTEGER DEFAULT 0',
    'max_consecutive_losing_days': 'INTEGER DEFAULT 0',
    'return_on_avg_margin': 'DECIMAL(10,4) DEFAULT 0',
    'commission_drag_ratio': 'DECIMAL(10,4) DEFAULT 0',
    'margin_cap_violation_days': 'INTEGER DEFAULT 0',
    'ticker_abs_contribution_top3_ratio': 'DECIMAL(10,4) DEFAULT 0',
    'profitable_ticker_count': 'INTEGER DEFAULT 0',
    'losing_ticker_count': 'INTEGER DEFAULT 0',
    'top_profit_ticker': 'TEXT',
    'top_profit_ticker_pnl': 'DECIMAL(15,2) DEFAULT 0',
    'worst_loss_ticker': 'TEXT',
    'worst_loss_ticker_pnl': 'DECIMAL(15,2) DEFAULT 0',
    'long_trade_net_pnl': 'DECIMAL(15,2) DEFAULT 0',
    'short_trade_net_pnl': 'DECIMAL(15,2) DEFAULT 0',
    'strategy_gross_pnl': 'DECIMAL(15,2) DEFAULT 0',
    'strategy_commission': 'DECIMAL(15,2) DEFAULT 0',
    'strategy_total_trades': 'INTEGER DEFAULT 0',
    'strategy_winning_trades': 'INTEGER DEFAULT 0',
    'strategy_losing_trades': 'INTEGER DEFAULT 0',
    'strategy_flat_trades': 'INTEGER DEFAULT 0',
    'strategy_win_rate': 'DECIMAL(10,4) DEFAULT 0',
    'strategy_net_pnl': 'DECIMAL(15,2) DEFAULT 0',
    'strategy_profit_factor': 'DECIMAL(10,4) DEFAULT 0',
    'trade_episode_memory_count': 'INTEGER DEFAULT 0',
    'exploratory_hypothesis_count': 'INTEGER DEFAULT 0',
    'learning_context_budget_rows': 'INTEGER DEFAULT 0',
    'learning_context_with_episode_rows': 'INTEGER DEFAULT 0',
    'learning_context_with_hypothesis_rows': 'INTEGER DEFAULT 0',
    'learning_context_with_memory_ratio': 'DECIMAL(10,4) DEFAULT 0',
    'avg_learning_context_chars': 'DECIMAL(10,2) DEFAULT 0',
}


class EvaluationHelper:
    """Helper class for managing evaluation results in the database."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._ensure_table_schema()

    def _get_connection(self):
        """Get a database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table_schema(self):
        """Ensure the config_outcome table has the correct schema with futures fields."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            # Check current table schema
            cursor.execute('PRAGMA table_info(config_outcome)')
            columns = cursor.fetchall()
            column_names = [col[1] for col in columns]

            # All expected columns (original futures metrics + strategy-quality diagnostics)
            expected_columns = EXPECTED_COLUMNS

            missing_columns = [col for col in expected_columns if col not in column_names]

            logger.info(f"Database: {self.db_path}")
            logger.info(f"Checking config_outcome table: {len(columns)} columns found (expected {len(expected_columns)})")

            if not columns:
                logger.info("Creating config_outcome table...")
                missing_columns = []
                recreate_required = True
            else:
                recreate_required = any(
                    column in {'id', 'config_id', 'updated_at'} or column not in COLUMN_DEFINITIONS
                    for column in missing_columns
                )

            if missing_columns and not recreate_required:
                logger.warning(f"Adding {len(missing_columns)} config_outcome columns: {missing_columns}")
                for column in missing_columns:
                    cursor.execute(f"ALTER TABLE config_outcome ADD COLUMN {column} {COLUMN_DEFINITIONS[column]}")
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_config_outcome_config_id ON config_outcome(config_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_config_outcome_updated_at ON config_outcome(updated_at)')
                conn.commit()
                logger.info(f"config_outcome table upgraded in place at {self.db_path}")
            elif recreate_required:
                if missing_columns:
                    logger.warning(f"Missing {len(missing_columns)} columns: {missing_columns}")
                logger.info(f"Current columns: {', '.join(column_names)}")
                logger.info("Recreating config_outcome table with correct schema...")

                # Drop and recreate table
                cursor.execute('DROP TABLE IF EXISTS config_outcome')

                cursor.execute('''
                CREATE TABLE config_outcome (
                    id VARCHAR(36) PRIMARY KEY,
                    config_id VARCHAR(36) NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    trading_date_start TIMESTAMP,
                    trading_date_end TIMESTAMP,
                    total_return DECIMAL(10,4),
                    annualized_return DECIMAL(10,4),
                    sharpe_ratio DECIMAL(10,4),
                    max_drawdown DECIMAL(10,4),
                    win_rate DECIMAL(10,4),
                    total_trades INTEGER,
                    winning_trades INTEGER,
                    losing_trades INTEGER,
                    avg_return_per_trade DECIMAL(10,4),
                    volatility DECIMAL(10,4),
                    initial_capital DECIMAL(15,2),
                    final_capital DECIMAL(15,2),
                    peak_margin_ratio DECIMAL(10,4) DEFAULT 0,
                    avg_margin_ratio DECIMAL(10,4) DEFAULT 0,
                    warning_days INTEGER DEFAULT 0,
                    liquidation_events INTEGER DEFAULT 0,
                    total_commission DECIMAL(15,2) DEFAULT 0,
                    avg_daily_pnl DECIMAL(15,2) DEFAULT 0,
                    total_settlement_pnl DECIMAL(15,2) DEFAULT 0,
                    max_margin_usage DECIMAL(10,4) DEFAULT 0,
                    long_trades INTEGER DEFAULT 0,
                    short_trades INTEGER DEFAULT 0,
                    active_long_positions INTEGER DEFAULT 0,
                    active_short_positions INTEGER DEFAULT 0,
                    commission_rate DECIMAL(10,4) DEFAULT 0,
                    avg_leverage DECIMAL(10,2) DEFAULT 1.0,
                    margin_call_count INTEGER DEFAULT 0,
                    net_settlement_pnl DECIMAL(15,2) DEFAULT 0,
                    account_gross_pnl DECIMAL(15,2) DEFAULT 0,
                    account_net_pnl DECIMAL(15,2) DEFAULT 0,
                    account_net_win_days INTEGER DEFAULT 0,
                    account_net_loss_days INTEGER DEFAULT 0,
                    account_net_flat_days INTEGER DEFAULT 0,
                    calmar_ratio DECIMAL(10,4) DEFAULT 0,
                    return_drawdown_ratio DECIMAL(10,4) DEFAULT 0,
                    profit_factor DECIMAL(10,4) DEFAULT 0,
                    payoff_ratio DECIMAL(10,4) DEFAULT 0,
                    trade_expectancy DECIMAL(15,2) DEFAULT 0,
                    avg_win_pnl DECIMAL(15,2) DEFAULT 0,
                    avg_loss_pnl DECIMAL(15,2) DEFAULT 0,
                    max_trade_gain DECIMAL(15,2) DEFAULT 0,
                    max_trade_loss DECIMAL(15,2) DEFAULT 0,
                    max_consecutive_losing_trades INTEGER DEFAULT 0,
                    max_consecutive_losing_days INTEGER DEFAULT 0,
                    return_on_avg_margin DECIMAL(10,4) DEFAULT 0,
                    commission_drag_ratio DECIMAL(10,4) DEFAULT 0,
                    margin_cap_violation_days INTEGER DEFAULT 0,
                    ticker_abs_contribution_top3_ratio DECIMAL(10,4) DEFAULT 0,
                    profitable_ticker_count INTEGER DEFAULT 0,
                    losing_ticker_count INTEGER DEFAULT 0,
                    top_profit_ticker TEXT,
                    top_profit_ticker_pnl DECIMAL(15,2) DEFAULT 0,
                    worst_loss_ticker TEXT,
                    worst_loss_ticker_pnl DECIMAL(15,2) DEFAULT 0,
                    long_trade_net_pnl DECIMAL(15,2) DEFAULT 0,
                    short_trade_net_pnl DECIMAL(15,2) DEFAULT 0,
                    strategy_gross_pnl DECIMAL(15,2) DEFAULT 0,
                    strategy_commission DECIMAL(15,2) DEFAULT 0,
                    strategy_total_trades INTEGER DEFAULT 0,
                    strategy_winning_trades INTEGER DEFAULT 0,
                    strategy_losing_trades INTEGER DEFAULT 0,
                    strategy_flat_trades INTEGER DEFAULT 0,
                    strategy_win_rate DECIMAL(10,4) DEFAULT 0,
                    strategy_net_pnl DECIMAL(15,2) DEFAULT 0,
                    strategy_profit_factor DECIMAL(10,4) DEFAULT 0,
                    trade_episode_memory_count INTEGER DEFAULT 0,
                    exploratory_hypothesis_count INTEGER DEFAULT 0,
                    learning_context_budget_rows INTEGER DEFAULT 0,
                    learning_context_with_episode_rows INTEGER DEFAULT 0,
                    learning_context_with_hypothesis_rows INTEGER DEFAULT 0,
                    learning_context_with_memory_ratio DECIMAL(10,4) DEFAULT 0,
                    avg_learning_context_chars DECIMAL(10,2) DEFAULT 0,
                    FOREIGN KEY (config_id) REFERENCES config(id)
                )
                ''')

                # Create indices
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_config_outcome_config_id ON config_outcome(config_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_config_outcome_updated_at ON config_outcome(updated_at)')

                conn.commit()

                # Verify the new schema
                cursor.execute('PRAGMA table_info(config_outcome)')
                new_columns = cursor.fetchall()
                logger.info(f"✓ config_outcome table recreated with {len(new_columns)} columns at {self.db_path}")
            else:
                logger.debug(f"config_outcome table schema OK ({len(columns)} columns)")

        except Exception as e:
            logger.error(f"Error ensuring table schema: {e}")
            raise
        finally:
            conn.close()

    def save_evaluation_result(self, config_id: str, metrics: Dict) -> Optional[str]:
        """
        Save a new evaluation result to the database with futures support.

        Args:
            config_id: The config ID
            metrics: Dictionary containing evaluation metrics

        Returns:
            The ID of the created record, or None if failed
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            evaluation_id = str(uuid.uuid4())
            values_by_column = {
                'id': evaluation_id,
                'config_id': config_id,
                'updated_at': datetime.now(timezone.utc).isoformat(),
                'avg_leverage': metrics.get('avg_leverage', 1.0),
                'margin_call_count': metrics.get('margin_call_count', 0),
            }
            for column in CORE_COLUMNS[3:] + QUALITY_COLUMNS:
                values_by_column[column] = metrics.get(column)
            values_tuple = tuple(values_by_column.get(column) for column in EXPECTED_COLUMNS)

            # Get actual column names from database for comparison
            cursor_check = conn.cursor()
            cursor_check.execute('PRAGMA table_info(config_outcome)')
            actual_columns = [row[1] for row in cursor_check.fetchall()]
            logger.info(f"Actual table columns ({len(actual_columns)}): {', '.join(actual_columns)}")

            columns_sql = ", ".join(EXPECTED_COLUMNS)
            placeholders_sql = ", ".join("?" for _ in EXPECTED_COLUMNS)
            cursor.execute(
                f"INSERT INTO config_outcome ({columns_sql}) VALUES ({placeholders_sql})",
                values_tuple,
            )

            conn.commit()
            logger.info(f"Saved evaluation result for config_id: {config_id}")
            return evaluation_id

        except Exception as e:
            logger.error(f"Error saving evaluation result: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def get_evaluation_result(self, config_id: str) -> Optional[Dict]:
        """
        Get the latest evaluation result for a config.

        Args:
            config_id: The config ID

        Returns:
            Dictionary containing the evaluation metrics, or None if not found
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT * FROM config_outcome
                WHERE config_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
            ''', (config_id,))

            row = cursor.fetchone()

            if row:
                return dict(row)
            return None

        except Exception as e:
            logger.error(f"Error getting evaluation result: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def get_all_evaluations(self) -> List[Dict]:
        """
        Get all evaluation results with config information.

        Returns:
            List of dictionaries containing evaluation results and config info
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT
                    co.id,
                    co.config_id,
                    co.updated_at,
                    co.trading_date_start,
                    co.trading_date_end,
                    co.total_return,
                    co.annualized_return,
                    co.sharpe_ratio,
                    co.max_drawdown,
                    co.win_rate,
                    co.total_trades,
                    co.winning_trades,
                    co.losing_trades,
                    co.avg_return_per_trade,
                    co.volatility,
                    co.initial_capital,
                    co.final_capital,
                    c.exp_name
                FROM config_outcome co
                LEFT JOIN config c ON co.config_id = c.id
                ORDER BY co.updated_at DESC
            ''')

            rows = cursor.fetchall()
            return [dict(row) for row in rows]

        except Exception as e:
            logger.error(f"Error getting all evaluations: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def get_evaluation_history(self, config_id: str) -> List[Dict]:
        """
        Get all evaluation results for a specific config (history).

        Args:
            config_id: The config ID

        Returns:
            List of dictionaries containing historical evaluation results
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT * FROM config_outcome
                WHERE config_id = ?
                ORDER BY updated_at ASC
            ''', (config_id,))

            rows = cursor.fetchall()
            return [dict(row) for row in rows]

        except Exception as e:
            logger.error(f"Error getting evaluation history: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def update_evaluation_result(self, config_id: str, metrics: Dict) -> bool:
        """
        Update the latest evaluation result for a config with futures support.

        Args:
            config_id: The config ID
            metrics: Dictionary containing updated evaluation metrics

        Returns:
            True if successful, False otherwise
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # First, get the latest evaluation record ID
            cursor.execute('''
                SELECT id FROM config_outcome
                WHERE config_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
            ''', (config_id,))

            row = cursor.fetchone()

            if not row:
                # If no existing record, create a new one
                return self.save_evaluation_result(config_id, metrics) is not None

            # Update the existing record with futures fields and strategy-quality diagnostics.
            evaluation_id = row['id']
            update_columns = EXPECTED_COLUMNS[2:]
            values_by_column = {
                'updated_at': datetime.now(timezone.utc).isoformat(),
                'avg_leverage': metrics.get('avg_leverage', 1.0),
                'margin_call_count': metrics.get('margin_call_count', 0),
            }
            for column in update_columns:
                values_by_column.setdefault(column, metrics.get(column))
            assignments_sql = ", ".join(f"{column} = ?" for column in update_columns)
            values_tuple = tuple(values_by_column.get(column) for column in update_columns) + (evaluation_id,)
            cursor.execute(
                f"UPDATE config_outcome SET {assignments_sql} WHERE id = ?",
                values_tuple,
            )

            conn.commit()
            logger.info(f"Updated evaluation result for config_id: {config_id}")
            return True

        except Exception as e:
            logger.error(f"Error updating evaluation result: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def delete_evaluation_result(self, config_id: str) -> bool:
        """
        Delete all evaluation results for a config.

        Args:
            config_id: The config ID

        Returns:
            True if successful, False otherwise
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute('''
                DELETE FROM config_outcome
                WHERE config_id = ?
            ''', (config_id,))

            conn.commit()
            logger.info(f"Deleted evaluation results for config_id: {config_id}")
            return True

        except Exception as e:
            logger.error(f"Error deleting evaluation result: {e}")
            return False
        finally:
            if conn:
                conn.close()
