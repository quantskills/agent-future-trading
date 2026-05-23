from typing import Any, Dict

from apis.router import APISource, Router
from util.logger import logger
from util.trading_calendar import get_previous_trading_day


def load_portfolio_config(cfg: Dict[str, Any], db, reset_portfolio: bool = False):
    """Load and validate the experiment config row used by phase entrypoints."""
    config_id = db.get_config_id_by_name(cfg["exp_name"])

    if reset_portfolio and config_id:
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
