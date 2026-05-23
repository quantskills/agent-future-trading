from __future__ import annotations

"""CLI wrapper for the Phase4 reviewer agent."""

import argparse
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dotenv import load_dotenv

from agents.reviewer import reviewer_agent
from graph.schema import TradingPhase
from tools.agent_tools.reviewer_tools import _expected_settlement_balance_change, _normalize_date
from util.config import ConfigParser
from util.db_helper import db_initialize, get_db
from util.logger import logger


load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the futures Phase4 reviewer")
    parser.add_argument("--config", type=str, required=True, help="Path to configuration file")
    parser.add_argument("--trading-date", type=str, required=True, help="Trading date in format YYYY-MM-DD")
    parser.add_argument("--local-db", action="store_true", help="Use local SQLite database")
    args = parser.parse_args()

    cfg = ConfigParser(args).get_config()
    if cfg.get("market_type") != "china_futures":
        raise RuntimeError("validate_phase_flow.py only supports china_futures")
    if not args.local_db:
        raise RuntimeError("validate_phase_flow.py currently requires --local-db")

    db_initialize(use_local_db=True)
    db = get_db()
    config_id = db.get_config_id_by_name(cfg["exp_name"])
    if not config_id:
        raise RuntimeError(f"Config {cfg['exp_name']} does not exist in local database")

    trading_date = _normalize_date(cfg["trading_date"])
    logger.set_context(
        exp_name=cfg["exp_name"],
        trading_date=trading_date,
        phase=TradingPhase.PHASE4.value,
    )
    reviewer_agent(
        {
            "config": cfg,
            "db": db,
            "config_id": config_id,
            "trading_date": trading_date,
        }
    )


if __name__ == "__main__":
    main()
