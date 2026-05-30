import argparse
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dotenv import load_dotenv
from graph.workflow import AgentWorkflow
from tools.agent_tools.runtime_setup import (
    ensure_seed_settled_portfolio,
    load_portfolio_config,
    resolve_net_exposure_config,
)
from tools.agent_tools.research.template_prior import load_template_prior_if_enabled
from util.config import ConfigParser
from util.logger import logger
from util.db_helper import db_initialize, get_db

# Load environment variables from .env file
load_dotenv()

def main():
    """Proposal entry point for AgentQuant Phase1 strategy generation."""

    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Run AgentQuant Phase1 proposal generation")
    parser.add_argument("--config", type=str, required=True, help="Path to configuration file")
    parser.add_argument("--trading-date", type=str, required=True, help="Trading date in format YYYY-MM-DD")
    parser.add_argument("--local-db", action="store_true", help="Use local SQLite database")
    parser.add_argument("--reset-config", action="store_true", help="Force reset portfolio with new cashflow from config (deletes existing portfolio data)")
    args = parser.parse_args()

    cfg = ConfigParser(args).get_config()
    if cfg.get("market_type") == "china_futures" and not args.local_db:
        raise RuntimeError("china_futures phase1 currently requires --local-db")

    if cfg.get("market_type") == "china_futures":
        net_exposure_config, net_exposure_source = resolve_net_exposure_config(cfg)
        logger.info(
            "Net exposure config in effect: "
            f"source={net_exposure_source}, "
            f"max_net_exposure={float(net_exposure_config.get('max_net_exposure', 0.50)):.2f}, "
            f"symmetric_scaling={bool(net_exposure_config.get('symmetric_scaling', True))}"
        )

    # Initialize the global database connection based on the local-db flag
    db_initialize(use_local_db=args.local_db)
    db = get_db()

    if args.reset_config:
        logger.warning(f"--reset-config flag detected: will delete existing portfolio and recreate with cashflow={cfg['cashflow']}")

    logger.info(f"Loading config for {cfg['exp_name']}, trading date: {args.trading_date}")
    config_id = load_portfolio_config(cfg, db, reset_portfolio=args.reset_config)
    load_template_prior_if_enabled(cfg, db, config_id)
    ensure_seed_settled_portfolio(cfg, db, config_id)
    logger.info("Init AgentQuant proposal workflow and run")

    # make sure trading date is in chronological order in DB portfolio table
    latest_trading_date = db.get_latest_trading_date(config_id)
    if latest_trading_date and latest_trading_date > cfg["trading_date"]:
        raise RuntimeError(f"Trading date {args.trading_date} is not in chronological order based on current experiment {cfg['exp_name']}")
    
    phase_name = "phase1"
    trading_day_value = cfg["trading_date"].strftime("%Y-%m-%d") if hasattr(cfg["trading_date"], "strftime") else str(cfg["trading_date"])
    logger.set_context(exp_name=cfg["exp_name"], trading_date=trading_day_value, phase=phase_name)
    existing_phase = db.get_trading_day_phase(config_id, trading_day_value, phase_name) if cfg.get("market_type") == "china_futures" else None
    if existing_phase and existing_phase.get("status") == "completed":
        raise RuntimeError(
            f"Phase1 already completed for experiment {cfg['exp_name']} on {trading_day_value}"
        )

    if cfg.get("market_type") == "china_futures":
        logger.info(f"Phase1 started for {cfg['exp_name']} on {trading_day_value}")
        db.start_trading_day_phase(config_id, trading_day_value, phase_name)

    try:
        app = AgentWorkflow(cfg, config_id)
        time_cost = app.run(config_id)
        if cfg.get("market_type") == "china_futures":
            db.complete_trading_day_phase(config_id, trading_day_value, phase_name, "completed", "")
            logger.info(
                f"Phase1 completed for {cfg['exp_name']} on {trading_day_value}: "
                f"elapsed={time_cost:.2f}s"
            )
        logger.info(f"AgentQuant run completed in {time_cost:.2f} seconds")
    except Exception as e:
        if cfg.get("market_type") == "china_futures":
            db.complete_trading_day_phase(config_id, trading_day_value, phase_name, "failed", str(e))
            logger.error(f"Phase1 failed for {cfg['exp_name']} on {trading_day_value}: {e}")
        logger.error(f"Error during portfolio operations: {e}")
        raise


if __name__ == "__main__":
    main()
