from __future__ import annotations

"""Phase4 researcher agent.

The researcher owns post-settlement learning and future-memory generation. It
may call LLM research tools, but it does not validate accounting, write real
transactions, or issue trading instructions.
"""

import sqlite3
from collections import Counter
from typing import Any, Dict, List, Mapping, Optional

from tools.agent_tools.research.research_learning import apply_researcher_learning
from util.logger import logger


def researcher_agent(
    *,
    db: Any,
    cursor: sqlite3.Cursor,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    previous_trading_dates_by_ticker: Mapping[str, str],
    settlement_row: Optional[Dict[str, Any]],
    recommendations: List[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    no_trade_reason_counter: Counter,
    transactions_by_recommendation: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    logger.log_agent_status("researcher", trading_date, "Writing future learning memory and research hypotheses")
    return apply_researcher_learning(
        db=db,
        cursor=cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        previous_trading_dates_by_ticker=previous_trading_dates_by_ticker,
        settlement_row=settlement_row,
        recommendations=recommendations,
        strategy_recommendations=strategy_recommendations,
        no_trade_reason_counter=no_trade_reason_counter,
        transactions_by_recommendation=transactions_by_recommendation or {},
    )
