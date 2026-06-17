from __future__ import annotations

"""Bootstrap Alpha Setup profiles from already-settled backtest history.

This script only writes research-memory tables: alpha_setup_sample,
alpha_setup_profile, and alpha_setup_action_value.  It does not touch
recommendations, transactions, settlement, reports, evaluation, or plots.
"""

import argparse
import json
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dotenv import load_dotenv

from tools.agent_tools.research.researcher_tools import backfill_alpha_setup_profiles_from_history
from util.config import ConfigParser
from util.db_helper import db_initialize, get_db
from util.logger import logger


load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill Alpha Setup research profiles from settled futures backtest history."
    )
    parser.add_argument("--config", type=str, required=True, help="Path to configuration file")
    parser.add_argument("--start-date", type=str, default=None, help="Start date, YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, default=None, help="End date, YYYY-MM-DD")
    parser.add_argument(
        "--trading-date",
        type=str,
        default=None,
        help="ConfigParser compatibility date; defaults to --end-date or --start-date",
    )
    parser.add_argument("--local-db", action="store_true", help="Use local SQLite database")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear existing alpha_setup_* rows for this config before rebuilding them.",
    )
    args = parser.parse_args()

    if not args.local_db:
        raise RuntimeError("bootstrap_alpha_setup.py currently requires --local-db")
    if not args.trading_date:
        args.trading_date = args.end_date or args.start_date
    if not args.trading_date:
        raise RuntimeError("--trading-date is required when neither --start-date nor --end-date is provided")

    cfg = ConfigParser(args).get_config()
    if cfg.get("market_type") != "china_futures":
        raise RuntimeError("bootstrap_alpha_setup.py only supports china_futures")

    db_initialize(use_local_db=True)
    db = get_db()
    config_id = db.get_config_id_by_name(cfg["exp_name"])
    if not config_id:
        raise RuntimeError(f"Config {cfg['exp_name']} does not exist in local database")

    conn = db._get_connection()
    try:
        cursor = conn.cursor()
        result = backfill_alpha_setup_profiles_from_history(
            cursor,
            cfg=cfg,
            config_id=config_id,
            start_date=args.start_date,
            end_date=args.end_date,
            reset=bool(args.reset),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"Alpha Setup bootstrap completed: {result}")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
