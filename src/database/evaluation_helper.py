import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional, List
from database.sqlite_setup import DB_PATH
from util.logger import logger


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

            # All expected columns (original + futures)
            expected_columns = [
                'id', 'config_id', 'updated_at', 'trading_date_start', 'trading_date_end',
                'total_return', 'annualized_return', 'sharpe_ratio', 'max_drawdown',
                'win_rate', 'total_trades', 'winning_trades', 'losing_trades',
                'avg_return_per_trade', 'volatility', 'initial_capital', 'final_capital',
                'peak_margin_ratio', 'avg_margin_ratio', 'warning_days', 'liquidation_events',
                'total_commission', 'avg_daily_pnl', 'total_settlement_pnl',
                'max_margin_usage', 'long_trades', 'short_trades',
                'active_long_positions', 'active_short_positions', 'commission_rate',
                'avg_leverage', 'margin_call_count'
            ]

            missing_columns = [col for col in expected_columns if col not in column_names]

            logger.info(f"Database: {self.db_path}")
            logger.info(f"Checking config_outcome table: {len(columns)} columns found (expected {len(expected_columns)})")

            # Always recreate if column count doesn't match exactly
            if len(columns) != len(expected_columns) or missing_columns:
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

            # Prepare values tuple
            values_tuple = (
                evaluation_id,
                config_id,
                datetime.now(timezone.utc).isoformat(),
                metrics.get('trading_date_start'),
                metrics.get('trading_date_end'),
                metrics.get('total_return'),
                metrics.get('annualized_return'),
                metrics.get('sharpe_ratio'),
                metrics.get('max_drawdown'),
                metrics.get('win_rate'),
                metrics.get('total_trades'),
                metrics.get('winning_trades'),
                metrics.get('losing_trades'),
                metrics.get('avg_return_per_trade'),
                metrics.get('volatility'),
                metrics.get('initial_capital'),
                metrics.get('final_capital'),
                metrics.get('peak_margin_ratio'),
                metrics.get('avg_margin_ratio'),
                metrics.get('warning_days'),
                metrics.get('liquidation_events'),
                metrics.get('total_commission'),
                metrics.get('avg_daily_pnl'),
                metrics.get('total_settlement_pnl'),
                metrics.get('max_margin_usage'),
                metrics.get('long_trades'),
                metrics.get('short_trades'),
                metrics.get('active_long_positions'),
                metrics.get('active_short_positions'),
                metrics.get('commission_rate'),
                metrics.get('avg_leverage', 1.0),
                metrics.get('margin_call_count', 0)
            )

            # Log parameter count
            num_params = len(values_tuple)
            num_placeholders = 32  # Count of ? placeholders in VALUES clause
            logger.info(f"INSERT: tuple has {num_params} values, VALUES clause has {num_placeholders} placeholders")

            # Count actual placeholders in SQL
            sql_text = '''
                INSERT INTO config_outcome (
                    id, config_id, updated_at, trading_date_start, trading_date_end,
                    total_return, annualized_return, sharpe_ratio, max_drawdown,
                    win_rate, total_trades, winning_trades, losing_trades,
                    avg_return_per_trade, volatility, initial_capital, final_capital,
                    peak_margin_ratio, avg_margin_ratio, warning_days, liquidation_events,
                    total_commission, avg_daily_pnl, total_settlement_pnl,
                    max_margin_usage, long_trades, short_trades,
                    active_long_positions, active_short_positions, commission_rate,
                    avg_leverage, margin_call_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            '''
            actual_placeholders = sql_text.count('?')
            logger.info(f"Actual placeholders in SQL: {actual_placeholders}")

            if num_params != 32 or actual_placeholders != 32:
                logger.error(f"ERROR: Expected 32 values but got {num_params}! SQL has {actual_placeholders} placeholders!")
                for i, val in enumerate(values_tuple):
                    logger.error(f"  Value {i}: {type(val).__name__} = {repr(val)[:100]}")

            # Get actual column names from database for comparison
            cursor_check = conn.cursor()
            cursor_check.execute('PRAGMA table_info(config_outcome)')
            actual_columns = [row[1] for row in cursor_check.fetchall()]
            logger.info(f"Actual table columns ({len(actual_columns)}): {', '.join(actual_columns)}")

            cursor.execute('''
                INSERT INTO config_outcome (
                    id, config_id, updated_at, trading_date_start, trading_date_end,
                    total_return, annualized_return, sharpe_ratio, max_drawdown,
                    win_rate, total_trades, winning_trades, losing_trades,
                    avg_return_per_trade, volatility, initial_capital, final_capital,
                    peak_margin_ratio, avg_margin_ratio, warning_days, liquidation_events,
                    total_commission, avg_daily_pnl, total_settlement_pnl,
                    max_margin_usage, long_trades, short_trades,
                    active_long_positions, active_short_positions, commission_rate,
                    avg_leverage, margin_call_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', values_tuple)

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

            # Update the existing record with futures fields
            evaluation_id = row['id']
            cursor.execute('''
                UPDATE config_outcome SET
                    updated_at = ?,
                    trading_date_start = ?,
                    trading_date_end = ?,
                    total_return = ?,
                    annualized_return = ?,
                    sharpe_ratio = ?,
                    max_drawdown = ?,
                    win_rate = ?,
                    total_trades = ?,
                    winning_trades = ?,
                    losing_trades = ?,
                    avg_return_per_trade = ?,
                    volatility = ?,
                    initial_capital = ?,
                    final_capital = ?,
                    peak_margin_ratio = ?,
                    avg_margin_ratio = ?,
                    warning_days = ?,
                    liquidation_events = ?,
                    total_commission = ?,
                    avg_daily_pnl = ?,
                    total_settlement_pnl = ?,
                    max_margin_usage = ?,
                    long_trades = ?,
                    short_trades = ?,
                    active_long_positions = ?,
                    active_short_positions = ?,
                    commission_rate = ?,
                    avg_leverage = ?,
                    margin_call_count = ?
                WHERE id = ?
            ''', (
                datetime.now(timezone.utc).isoformat(),
                metrics.get('trading_date_start'),
                metrics.get('trading_date_end'),
                metrics.get('total_return'),
                metrics.get('annualized_return'),
                metrics.get('sharpe_ratio'),
                metrics.get('max_drawdown'),
                metrics.get('win_rate'),
                metrics.get('total_trades'),
                metrics.get('winning_trades'),
                metrics.get('losing_trades'),
                metrics.get('avg_return_per_trade'),
                metrics.get('volatility'),
                metrics.get('initial_capital'),
                metrics.get('final_capital'),
                metrics.get('peak_margin_ratio'),
                metrics.get('avg_margin_ratio'),
                metrics.get('warning_days'),
                metrics.get('liquidation_events'),
                metrics.get('total_commission'),
                metrics.get('avg_daily_pnl'),
                metrics.get('total_settlement_pnl'),
                metrics.get('max_margin_usage'),
                metrics.get('long_trades'),
                metrics.get('short_trades'),
                metrics.get('active_long_positions'),
                metrics.get('active_short_positions'),
                metrics.get('commission_rate'),
                metrics.get('avg_leverage', 1.0),
                metrics.get('margin_call_count', 0),
                evaluation_id
            ))

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
