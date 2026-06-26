from __future__ import annotations

"""Explicit research initialization entry for template-prior cold-start memory."""

import argparse
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dotenv import load_dotenv

from tools.agent_tools.research.template_prior import load_template_prior_if_enabled
from tools.common.runtime_setup import load_portfolio_config
from util.config import ConfigParser
from util.db_helper import db_initialize, get_db
from util.logger import logger


load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="Load template-prior cold-start research memory")
    parser.add_argument("--config", type=str, required=True, help="Path to configuration file")
    parser.add_argument("--trading-date", type=str, required=True, help="Trading date in format YYYY-MM-DD")
    parser.add_argument("--local-db", action="store_true", help="Use local SQLite database")
    args = parser.parse_args()

    cfg = ConfigParser(args).get_config()
    if cfg.get("market_type") != "china_futures":
        raise RuntimeError("load_template_prior.py only supports china_futures")
    if not args.local_db:
        raise RuntimeError("load_template_prior.py currently requires --local-db")

    db_initialize(use_local_db=True)
    db = get_db()
    config_id = load_portfolio_config(cfg, db, reset_portfolio=False)

    trading_day_value = (
        cfg["trading_date"].strftime("%Y-%m-%d")
        if hasattr(cfg.get("trading_date"), "strftime")
        else str(cfg.get("trading_date") or args.trading_date)[:10]
    )
    logger.set_context(
        exp_name=cfg["exp_name"],
        trading_date=trading_day_value,
        phase="research_init",
    )
    loaded_count = load_template_prior_if_enabled(cfg, db, config_id)
    logger.info(
        "Template-prior research initialization completed: "
        f"config_id={config_id}, trading_date={trading_day_value}, loaded={loaded_count}"
    )


if __name__ == "__main__":
    main()
