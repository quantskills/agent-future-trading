import sqlite3
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from graph.schema import AnalystSignal
from database.interface import BaseDB
from database.sqlite_setup import DB_PATH
from util.logger import logger

class SQLiteDB(BaseDB):
    def __init__(self):
        self.db_path = DB_PATH
        self._runtime_schema_ready = False

    def _get_connection(self):
        """Get a database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row # access columns by name
        if not self._runtime_schema_ready:
            try:
                # Old local backtest DBs may miss newly added auxiliary tables.
                # Patch them lazily on first connect so new controls actually participate.
                cursor = conn.cursor()
                self._ensure_strategy_memory_schema(cursor)
                conn.commit()
                self._runtime_schema_ready = True
            except Exception as exc:
                logger.warning(f"Runtime schema bootstrap skipped: {exc}")
        return conn

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
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except Exception:
            return value

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

        available_cash = row["available_cash"] if "available_cash" in row.keys() else 0
        return {
            "id": row["id"],
            "config_id": row["config_id"] if "config_id" in row.keys() else None,
            "updated_at": row["updated_at"] if "updated_at" in row.keys() else None,
            "trading_date": self._normalize_trading_day_value(row["trading_date"]) if "trading_date" in row.keys() else None,
            "cashflow": row["cashflow"],
            "total_assets": row["total_assets"] if "total_assets" in row.keys() else row["cashflow"],
            "positions": positions,
            "margin_used": row["margin_used"] if "margin_used" in row.keys() else 0,
            "available_cash": available_cash,
            "margin_available": available_cash,
            "daily_settlement_pnl": row["daily_settlement_pnl"] if "daily_settlement_pnl" in row.keys() else 0,
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
                SELECT id, signal_snapshot
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
                snapshot = self._deserialize_json(recommendation.get("signal_snapshot")) or {}
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
            payload = {
                "ticker": ticker,
                "side": side,
                "signal_combo": "*" if combo_key == "*" else self._deserialize_json(combo_key),
                "state": state,
                "reason": reason,
                "summary": summary,
                "cutoff_trading_date": trading_day_value,
                "source": source,
            }
            cursor.execute(
                '''
                INSERT OR REPLACE INTO strategy_memory (
                    id, config_id, ticker, side, signal_combo, memory_state,
                    sample_count, win_rate, net_pnl, avg_pnl, confidence_score,
                    source, reason, updated_at, valid_until, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def get_latest_trading_date(self, config_id: str) -> Optional[datetime]:
        """Get the latest trading date for a config."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT trading_date FROM portfolio 
                WHERE config_id = ? AND trading_date IS NOT NULL
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
                SELECT * FROM portfolio 
                WHERE config_id = ? AND trading_date IS NOT NULL
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
                INSERT INTO portfolio (id, config_id, updated_at, trading_date, cashflow, total_assets, positions,
                                      margin_used, available_cash, daily_settlement_pnl, leverage)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                portfolio_id,
                config_id,
                datetime.now(timezone.utc).isoformat(), # UTC time
                trading_day_value,
                cashflow,
                cashflow,
                json.dumps({}),
                0,  # margin_used
                cashflow,  # available_cash
                0,  # daily_settlement_pnl
                1.0  # leverage
            ))

            conn.commit()
            logger.info(f"Portfolio {portfolio_id[:8]}... created successfully")
            return {
                'id': portfolio_id,
                'cashflow': cashflow,
                'total_assets': cashflow,
                'positions': {},
                'margin_used': 0,
                'available_cash': cashflow,
                'daily_settlement_pnl': 0,
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
            available_cash = portfolio.get('margin_available', portfolio['cashflow'] - margin_used)
            daily_pnl = portfolio.get('daily_settlement_pnl', 0)
            leverage = portfolio.get('leverage', 1.0)

            cursor.execute('''
                INSERT INTO portfolio (id, config_id, updated_at, trading_date, cashflow, total_assets, positions,
                                      margin_used, available_cash, daily_settlement_pnl, leverage)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                portfolio_id,
                config_id,
                datetime.now(timezone.utc).isoformat(), # UTC time
                trading_day_value,
                portfolio['cashflow'],
                total_assets,
                json.dumps(portfolio['positions']),
                margin_used,
                available_cash,
                daily_pnl,
                leverage
            ))

            conn.commit()
            logger.info(f"Portfolio {portfolio_id[:8]}... copied successfully")
            return {
                'id': portfolio_id,
                'cashflow': portfolio['cashflow'],
                'positions': portfolio['positions'],
                'margin_used': margin_used,
                'margin_available': available_cash,
                'daily_settlement_pnl': daily_pnl,
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
                return {
                    'id': existing_row['id'],
                    'cashflow': existing_row['cashflow'],
                    'positions': json.loads(existing_row['positions']) if existing_row['positions'] else {}
                }

            # 涓嶅瓨鍦紝鍒欏垱寤烘柊鐨?portfolio 璁板綍
            portfolio_id = str(uuid.uuid4())
            total_assets = portfolio['cashflow'] + sum(position['value'] for position in portfolio['positions'].values())
            logger.info(f"Creating new portfolio {portfolio_id[:8]}... for trading date {trading_day_value}")

            cursor.execute('''
                INSERT INTO portfolio (id, config_id, updated_at, trading_date, cashflow, total_assets, positions)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                portfolio_id,
                config_id,
                datetime.now(timezone.utc).isoformat(), # UTC time
                trading_day_value,
                portfolio['cashflow'],
                total_assets,
                json.dumps(portfolio['positions'])
            ))

            conn.commit()
            logger.info(f"Portfolio {portfolio_id[:8]}... created successfully")
            return {
                'id': portfolio_id,
                'cashflow': portfolio['cashflow'],
                'positions': portfolio['positions']
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
            available_cash = portfolio.get('margin_available', portfolio['cashflow'] - margin_used)
            daily_pnl = portfolio.get('daily_settlement_pnl', 0)
            leverage = portfolio.get('leverage', 1.0)

            cursor.execute('''
                UPDATE portfolio
                SET config_id = ?, updated_at = ?, trading_date = ?, cashflow = ?, total_assets = ?, positions = ?,
                    margin_used = ?, available_cash = ?, daily_settlement_pnl = ?, leverage = ?
                WHERE id = ?
            ''', (
                config_id,
                datetime.now(timezone.utc).isoformat(), # UTC time
                trading_day_value,
                portfolio['cashflow'],
                total_assets,
                json.dumps(portfolio['positions']),
                margin_used,
                available_cash,
                daily_pnl,
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
        
    def save_signal(self, portfolio_id: str, analyst: str, ticker: str, prompt: str, signal: AnalystSignal) -> Optional[str]:
        """Save a new signal."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            signal_id = str(uuid.uuid4())
            cursor.execute('''
                INSERT INTO signal (id, portfolio_id, updated_at, ticker, llm_prompt,
                                  analyst, signal, justification)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id,
                portfolio_id,
                datetime.now(timezone.utc).isoformat(), # UTC time 
                ticker,
                prompt,
                analyst,
                str(signal.signal),
                signal.justification
            ))
            
            conn.commit()
            return signal_id
        except Exception as e:
            logger.error(f"Error saving signal: {e}")
            return None
        finally:
            if conn:
                conn.close()

    # ==================== 鏈熻揣涓撶敤鏂规硶 ====================

    def save_futures_recommendation(self, recommendation: Any) -> Optional[str]:
        """Save a futures recommendation for later execution or audit."""
        conn = None
        try:
            recommendation_dict = self._model_to_dict(recommendation)
            recommendation_id = recommendation_dict.get("id") or str(uuid.uuid4())
            created_at = recommendation_dict.get("created_at") or datetime.now(timezone.utc).isoformat()

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO futures_recommendation (
                    id, config_id, reference_portfolio_id, trading_date, effective_trade_date,
                    source_type, underlying_code, from_contract, to_contract, contract_code,
                    action, lots, base_price, base_price_source, base_price_date,
                    open_price, prev_close_price, slippage_model, slippage_ticks,
                    slippage_amount, execution_price, justification, signal_snapshot,
                    audit_payload, warning_message, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    recommendation_id,
                    recommendation_dict.get("config_id"),
                    recommendation_dict.get("reference_portfolio_id"),
                    self._normalize_trading_day_value(recommendation_dict.get("trading_date")),
                    self._normalize_trading_day_value(recommendation_dict.get("effective_trade_date")),
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
                    self._serialize_json(recommendation_dict.get("signal_snapshot")),
                    self._serialize_json(recommendation_dict.get("audit_payload")),
                    recommendation_dict.get("warning_message"),
                    self._enum_value(recommendation_dict.get("status")),
                    created_at,
                ),
            )
            conn.commit()
            return recommendation_id
        except Exception as e:
            logger.error(f"Error saving futures recommendation: {e}")
            return None
        finally:
            if conn:
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
                record["signal_snapshot"] = self._deserialize_json(record.get("signal_snapshot"))
                record["audit_payload"] = self._deserialize_json(record.get("audit_payload"))
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
            conn = self._get_connection()
            cursor = conn.cursor()

            fields = ['status = ?']
            params: List[Any] = [self._enum_value(status)]

            if execution_price is not None:
                fields.append('execution_price = ?')
                params.append(execution_price)

            if warning_message is not None:
                fields.append('warning_message = ?')
                params.append(warning_message)

            if signal_snapshot is not None:
                fields.append('signal_snapshot = ?')
                params.append(self._serialize_json(signal_snapshot))

            if audit_payload is not None:
                fields.append('audit_payload = ?')
                params.append(self._serialize_json(audit_payload))

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
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating futures recommendation status: {e}")
            return False
        finally:
            if conn:
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
        conn = None
        try:
            transaction_dict = self._model_to_dict(transaction)
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

            cursor.execute(
                '''
                INSERT INTO futures_transactions (
                    id, portfolio_id, config_id, recommendation_id, trading_date, ticker, contract_code,
                    action, lots, price, execution_price, settle_price, contract_multiplier, margin_rate,
                    margin_used, daily_pnl, commission, source_type, execution_phase,
                    execution_price_basis, base_price, base_price_source, base_price_date,
                    open_price, prev_close_price, slippage_model, slippage_ticks, slippage_amount,
                    released_margin, margin_delta, post_trade_margin_used, audit_payload,
                    warning_message, justification, llm_prompt, booked_in_settlement, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    self._serialize_json(transaction_dict.get("audit_payload")),
                    transaction_dict.get("warning_message"),
                    transaction_dict.get("justification"),
                    transaction_dict.get("llm_prompt"),
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
                record["audit_payload"] = self._deserialize_json(record.get("audit_payload"))
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
        memory_config: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Complete a trading day phase record."""
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

            if str(phase_value).lower() == "phase4" and str(status_value).lower() == "completed":
                update_on_phase4 = True
                if memory_config is not None:
                    update_on_phase4 = bool(memory_config.get("update_on_phase4", True))
                if update_on_phase4:
                    try:
                        refreshed = self._refresh_strategy_memory_with_cursor(
                            cursor,
                            config_id=config_id,
                            trading_date=trading_date_value,
                            updated_at=now,
                            memory_config=memory_config,
                        )
                        logger.info(
                            f"Strategy memory refreshed for {config_id[:8]} on {trading_date_value}: {refreshed} rows"
                        )
                    except Exception as memory_exc:
                        logger.warning(f"Strategy memory refresh skipped: {memory_exc}")

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

    def get_futures_transaction_memory(self, config_id: str, ticker: str, limit: int = 20) -> List[str]:
        """Get recent transaction memory for futures PM prompts."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT trading_date, action, lots, COALESCE(execution_price, price) AS execution_price
                FROM futures_transactions
                WHERE config_id = ? AND ticker = ?
                ORDER BY trading_date DESC, created_at DESC
                LIMIT ?
                ''',
                (config_id, ticker, limit),
            )

            memory = []
            for row in cursor.fetchall():
                memory.append(f"{row['trading_date']} {row['action']} {row['lots']}@{row['execution_price']}")
            return memory
        except Exception as e:
            logger.warning(f"No futures transaction memory found for {ticker}: {e}")
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
        except Exception as e:
            logger.error(f"Error getting signal history for {ticker}: {e}")
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
        except Exception as e:
            logger.error(f"Error getting ticker performance for {ticker}: {e}")
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
        except Exception as e:
            logger.error(f"Error getting trade-pair performance for {ticker} {side}: {e}")
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
                    SELECT id, signal_snapshot
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
                    snapshot = self._deserialize_json(recommendation.get("signal_snapshot")) or {}
                    if not isinstance(snapshot, dict):
                        snapshot = {}
                item_combo = self._signal_combo_from_snapshot(snapshot)
                item["signal_combo"] = list(item_combo)
                item["market_confirmation"] = snapshot.get("market_confirmation") if isinstance(snapshot, dict) else None
                item["decision_planner"] = self._decision_planner_from_snapshot(snapshot)
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
        except Exception as e:
            logger.error(f"Error getting conditional trade performance for {ticker} {side}: {e}")
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
        except Exception as e:
            logger.error(f"Error refreshing strategy memory: {e}")
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
                    ORDER BY CASE WHEN signal_combo = ? THEN 0 ELSE 1 END, updated_at DESC
                    ''',
                    (config_id, ticker.upper(), side_value, combo_key, trading_day_value, combo_key),
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
        except Exception as e:
            logger.error(f"Error getting strategy memory for {ticker} {side}: {e}")
            return {
                "enabled": False,
                "ticker": ticker.upper(),
                "side": side_value,
                "signal_combo": self._deserialize_json(combo_key) if combo_key != "*" else "*",
                "combo": None,
                "side_memory": None,
                "records": [],
                "error": str(e),
            }
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
        plan = snapshot.get("pre_open_plan") if isinstance(snapshot.get("pre_open_plan"), dict) else {}
        combo = plan.get("analyst_signal_combo") if isinstance(plan, dict) else None
        normalized = self._normalize_signal_combo(combo)
        if normalized is not None:
            return normalized

        def signal_value(analyst: str) -> str:
            item = snapshot.get(analyst)
            if isinstance(item, dict) and item.get("signal"):
                return str(item.get("signal"))
            return "Neutral"

        return (
            signal_value("technical"),
            signal_value("fundamental"),
            signal_value("commodity_news")
            if signal_value("commodity_news") != "Neutral"
            else signal_value("company_news"),
        )

    def _decision_planner_from_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        plan = snapshot.get("pre_open_plan") if isinstance(snapshot.get("pre_open_plan"), dict) else {}
        planner = (plan.get("trade_auditor") or plan.get("decision_planner")) if isinstance(plan, dict) else None
        return planner if isinstance(planner, dict) else {}

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
                from apis.datayes.api_model import FuturesSettlementRecord
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

            # 灏唒ositions_detail杞崲涓篔SON瀛楃涓插瓨鍌?
            import json
            positions_json = json.dumps(settlement.positions_detail) if settlement.positions_detail else None

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
                     previous_margin, current_margin, margin_as_asset_prev, margin_as_asset_curr,
                     daily_pnl, deposit, withdraw, commission, margin_ratio,
                     is_warning, is_liquidation, positions_snapshot, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    settlement_id,
                    portfolio_id,
                    settlement.trading_date,
                    settlement.previous_balance,
                    settlement.current_balance,
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
                        position_type = ?,
                        lots = ?,
                        entry_price = ?,
                        settle_price = ?,
                        created_at = ?
                    WHERE id = ?
                ''', (
                    settlement_record['daily_pnl'],
                    settlement_record['commission'],
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
                     commission, position_type, lots, entry_price, settle_price, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    record_id,
                    settlement_record['portfolio_id'],
                    settlement_record['trading_date'],
                    settlement_record['ticker'],
                    settlement_record['daily_pnl'],
                    settlement_record['commission'],
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


