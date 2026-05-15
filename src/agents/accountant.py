"""Accountant agent for futures Phase3 daily settlement.

The accountant owns the daily settlement role: it verifies Phase2 completion,
runs the futures settlement tool, persists the official portfolio, writes daily
settlement records, and marks Phase3 status.
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dotenv import load_dotenv

from graph.schema import TradingPhase
from run.proposal import ensure_seed_settled_portfolio, load_portfolio_config
from tools.agent_tools.futures_settlement import FuturesDailySettlement
from util.config import ConfigParser
from util.db_helper import db_initialize, get_db
from util.logger import logger


load_dotenv()


def accountant_agent(argv: Optional[List[str]] = None) -> None:
    """Run the futures accountant agent from CLI-style arguments."""
    parser = argparse.ArgumentParser(description="Run AgentQuant futures accountant agent for Phase3 settlement")
    parser.add_argument("--config", type=str, required=True, help="Path to configuration file")
    parser.add_argument("--trading-date", type=str, required=True, help="Trading date in format YYYY-MM-DD")
    parser.add_argument("--local-db", action="store_true", help="Use local SQLite database")
    args = parser.parse_args(argv)

    cfg = ConfigParser(args).get_config()
    if cfg.get("market_type") != "china_futures":
        raise RuntimeError("accountant agent only supports china_futures")
    if not args.local_db:
        raise RuntimeError("china_futures phase3 currently requires --local-db")

    db_initialize(use_local_db=args.local_db)
    db = get_db()
    config_id = load_portfolio_config(cfg, db, reset_portfolio=False)
    ensure_seed_settled_portfolio(cfg, db, config_id)

    trading_date_value = cfg["trading_date"].strftime("%Y-%m-%d")
    logger.set_context(exp_name=cfg["exp_name"], trading_date=trading_date_value, phase=TradingPhase.PHASE3.value)
    phase2_record = db.get_trading_day_phase(config_id, trading_date_value, TradingPhase.PHASE2)
    if not phase2_record or phase2_record.get("status") != "completed":
        raise RuntimeError(f"Phase2 is not completed for {cfg['exp_name']} on {trading_date_value}")

    phase3_record = db.get_trading_day_phase(config_id, trading_date_value, TradingPhase.PHASE3)
    if phase3_record and phase3_record.get("status") == "completed":
        raise RuntimeError(f"Phase3 already completed for {cfg['exp_name']} on {trading_date_value}")

    logger.info(f"Phase3 started for {cfg['exp_name']} on {trading_date_value}")
    db.start_trading_day_phase(config_id, trading_date_value, TradingPhase.PHASE3)

    try:
        engine = FuturesDailySettlement("china_futures")
        settlement_record = engine.run_phase3(config_id=config_id, trading_date=cfg["trading_date"])
        logger.log_settlement(trading_date_value, settlement_record)
        db.complete_trading_day_phase(
            config_id,
            trading_date_value,
            TradingPhase.PHASE3,
            "completed",
            f"balance={settlement_record.current_balance:.2f}",
        )
        logger.info(
            f"Phase3 completed for {cfg['exp_name']} on {trading_date_value}: "
            f"balance={settlement_record.current_balance:,.2f}"
        )
    except Exception as exc:
        db.complete_trading_day_phase(config_id, trading_date_value, TradingPhase.PHASE3, "failed", str(exc))
        logger.error(f"Phase3 accountant settlement failed: {exc}")
        raise


def main(argv: Optional[List[str]] = None) -> None:
    accountant_agent(argv)


if __name__ == "__main__":
    main()
