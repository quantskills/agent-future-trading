from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from agents.registry import AgentRegistry
from graph.constants import AgentKey
from llm.inference import agent_call
from llm.prompt import PLANNER_PROMPT
from util.logger import logger


class AnalystPlannerOutput(BaseModel):
    """Output for the legacy LLM analyst selector."""

    analysts: List[str] = Field(description="Name list of selected analysts")
    justification: str = Field(
        description="Explanation for the analyst selection",
        default="No justification provided due to error",
    )


# Backward-compatible name used by the legacy planner_agent call.
PlannerOutput = AnalystPlannerOutput


def planner_agent(ticker: str, llm_config: Dict[str, Any], workflow_analysts: List[str]) -> List[str]:
    """
    Legacy LLM analyst selector.

    This is not the deterministic futures trade auditor. It is controlled only
    by planner_mode and decides which analyst agents to run.
    """

    logger.log_agent_status(AgentKey.PLANNER, ticker, "Planning")
    analyst_info = [
        {
            "analyst_name": key,
            "analyst_info": AgentRegistry.get_analyst_info(key),
        }
        for key in workflow_analysts
    ]

    prompt = PLANNER_PROMPT.format(ticker=ticker, analysts=analyst_info)

    result = agent_call(
        prompt=prompt,
        llm_config=llm_config,
        pydantic_model=AnalystPlannerOutput,
    )

    logger.info(f"Planner agent selected {result.analysts} | Justification: {result.justification}")
    return result.analysts

