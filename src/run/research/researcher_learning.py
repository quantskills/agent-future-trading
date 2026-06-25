from __future__ import annotations

"""CLI wrapper for the Phase4 researcher learning agent."""

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dotenv import load_dotenv

from agents.research_team.researcher import researcher_agent
from graph.schema import RecommendationSourceType, TradingPhase
from tools.agent_tools.research.phase4_review import (
    _fetchone,
    _group_transactions_by_recommendation,
    _normalize_date,
    _validate_recommendation_execution_audit,
    _write_reviewer_learning_report,
)
from util.config import ConfigParser
from util.db_helper import db_initialize, get_db
from util.logger import logger


load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the futures Phase4 researcher learning agent")
    parser.add_argument("--config", type=str, required=True, help="Path to configuration file")
    parser.add_argument("--trading-date", type=str, required=True, help="Trading date in format YYYY-MM-DD")
    parser.add_argument("--local-db", action="store_true", help="Use local SQLite database")
    args = parser.parse_args()

    cfg = ConfigParser(args).get_config()
    if cfg.get("market_type") != "china_futures":
        raise RuntimeError("researcher_learning.py only supports china_futures")
    if not args.local_db:
        raise RuntimeError("researcher_learning.py currently requires --local-db")

    db_initialize(use_local_db=True)
    db = get_db()
    config_id = db.get_config_id_by_name(cfg["exp_name"])
    if not config_id:
        raise RuntimeError(f"Config {cfg['exp_name']} does not exist in local database")

    trading_date = _normalize_date(cfg["trading_date"])
    phase4 = db.get_trading_day_phase(config_id, trading_date, TradingPhase.PHASE4)
    if not phase4 or phase4.get("status") != "completed":
        raise RuntimeError(f"Researcher learning requires completed Phase4 reviewer validation on {trading_date}")

    logger.set_context(
        exp_name=cfg["exp_name"],
        trading_date=trading_date,
        phase="researcher_learning",
    )

    recommendations = db.get_futures_recommendations_by_effective_date(config_id, trading_date)
    strategy_recommendations = [
        row
        for row in recommendations
        if row.get("source_type") == RecommendationSourceType.STRATEGY.value
    ]
    phase2_transactions = db.get_futures_transactions_by_date(
        config_id,
        trading_date,
        execution_phase=TradingPhase.PHASE2,
    )
    transactions_by_recommendation = _group_transactions_by_recommendation(phase2_transactions)
    no_trade_reason_counter: Counter = _validate_recommendation_execution_audit(
        recommendations,
        transactions_by_recommendation,
        [],
    )

    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        if hasattr(db, "_ensure_reviewer_learning_schema"):
            db._ensure_reviewer_learning_schema(cursor)
        cursor.execute(
            """
            SELECT id
            FROM learning_event_log
            WHERE config_id = ?
              AND trading_date = ?
              AND event_type = 'researcher_learning_completed'
              AND agent = 'researcher'
              AND status = 'applied'
            LIMIT 1
            """,
            (config_id, trading_date),
        )
        if cursor.fetchone():
            logger.info(f"Researcher learning already completed for {trading_date}; skipping")
            return

        settlement_row = _fetchone(
            cursor,
            """
            SELECT ds.*, p.trading_date AS portfolio_trading_date
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE p.config_id = ?
              AND substr(ds.trading_date, 1, 10) = ?
            ORDER BY ds.created_at DESC
            LIMIT 1
            """,
            (config_id, trading_date),
        )
        learning_summary = researcher_agent(
            db=db,
            cursor=cursor,
            cfg=cfg,
            config_id=config_id,
            trading_date=trading_date,
            settlement_row=settlement_row,
            recommendations=recommendations,
            strategy_recommendations=strategy_recommendations,
            no_trade_reason_counter=no_trade_reason_counter,
            transactions_by_recommendation=transactions_by_recommendation,
        )
        reviewer_report_paths = _write_reviewer_learning_report(
            cursor=cursor,
            cfg=cfg,
            config_id=config_id,
            trading_date=trading_date,
            learning_summary=learning_summary,
        )
        learning_summary["reviewer_report"] = reviewer_report_paths
        cursor.execute(
            """
            INSERT INTO learning_event_log (
                id, config_id, trading_date, event_type, agent, scope_type, scope_key,
                evidence_json, action_json, verifier, created_at, status
            ) VALUES (?, ?, ?, 'researcher_learning_completed', 'researcher', 'trading_day', ?, ?, ?, 'deterministic_researcher_entry', datetime('now'), 'applied')
            """,
            (
                f"researcher_learning_completed:{config_id}:{trading_date}",
                config_id,
                trading_date,
                trading_date,
                "{}",
                "{}",
            ),
        )
        conn.commit()
        logger.info(f"Researcher learning persisted: {learning_summary}")
        logger.info(f"Researcher learning report written: {reviewer_report_paths}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
