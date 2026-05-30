from __future__ import annotations

"""Phase4 reviewer agent.

The reviewer is deterministic: it validates the first three phases and writes
the daily transaction report. Post-settlement learning and any LLM research are
delegated to the researcher agent. The reviewer does not place orders, call LLMs,
or rewrite LLM weights.
"""

from typing import Any, Dict

from graph.schema import TradingPhase
from tools.agent_tools.research.reviewer_tools import run_phase4_review
from util.logger import logger


def reviewer_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    cfg = state["config"]
    db = state["db"]
    config_id = state["config_id"]
    trading_date = state["trading_date"]

    logger.set_context(
        exp_name=cfg.get("exp_name"),
        trading_date=trading_date,
        phase=TradingPhase.PHASE4.value,
    )
    logger.log_agent_status("reviewer", trading_date, "Validating phase flow and daily transaction report")
    return run_phase4_review(
        cfg=cfg,
        db=db,
        config_id=config_id,
        trading_date=trading_date,
    )
