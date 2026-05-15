import argparse
import sys
from pathlib import Path
from typing import Dict, Any

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dotenv import load_dotenv
from apis.router import APISource, Router
from graph.workflow import AgentWorkflow
from util.config import ConfigParser
from util.logger import logger
from util.db_helper import db_initialize, get_db
from util.trading_calendar import get_previous_trading_day

# Load environment variables from .env file
load_dotenv()

def load_portfolio_config(cfg: Dict[str, Any], db, reset_portfolio: bool = False):
    """Load and validate config based on experiment configuration.

    Args:
        cfg: Configuration dictionary
        db: Database instance
        reset_portfolio: If True, delete existing portfolio and recreate with new cashflow
    """
    config_id = db.get_config_id_by_name(cfg["exp_name"])

    if reset_portfolio and config_id:
        # Delete existing config and portfolio data
        logger.warning(f"Deleting existing config {config_id[:8]}... and its portfolio data")
        db.delete_config_and_portfolios(config_id)
        config_id = None

    if not config_id:
        logger.info(f"Creating new config for {cfg['exp_name']} with cashflow={cfg['cashflow']}")
        config_id = db.create_config(cfg)
        if not config_id:
            raise RuntimeError(f"Failed to create config for {cfg['exp_name']}")
    else:
        logger.info(f"Using existing config {config_id[:8]}... for {cfg['exp_name']}")

    return config_id


def ensure_seed_settled_portfolio(cfg: Dict[str, Any], db, config_id: str):
    """Create a seed settled portfolio for phase1 if this config has never settled before."""
    if cfg.get("market_type") != "china_futures":
        return

    latest_portfolio = db.get_latest_settled_portfolio(config_id)
    if latest_portfolio:
        return

    tickers = cfg.get("tickers", [])
    if not tickers:
        raise RuntimeError("Seed settled portfolio creation requires at least one configured futures ticker")

    anchor_underlying = tickers[0]
    router = Router(APISource.PANDAAI, market_type="china_futures")
    seed_trading_date = get_previous_trading_day(
        router=router,
        trading_date=cfg["trading_date"],
        underlying_code=anchor_underlying,
    )
    logger.info(f"Creating seed settled portfolio for {cfg['exp_name']} on {seed_trading_date.strftime('%Y-%m-%d')}")
    portfolio = db.create_portfolio(config_id, cfg["cashflow"], seed_trading_date)
    if not portfolio:
        raise RuntimeError(f"Failed to create seed settled portfolio for {cfg['exp_name']}")


def resolve_net_exposure_config(cfg: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    """Resolve the effective net exposure config and its source."""
    top_level_config = cfg.get("net_exposure_control")
    if top_level_config is not None:
        return top_level_config, "net_exposure_control"

    risk_control_config = cfg.get("risk_control", {})
    nested_config = risk_control_config.get("net_exposure_control")
    if nested_config is not None:
        return nested_config, "risk_control.net_exposure_control"

    return {}, "defaults"

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
