"""Signal collector agent for the decision team."""

from __future__ import annotations

from graph.schema import FundState
from tools.agent_tools.decision.signal_evidence_collection import build_signal_collection_contract


def signal_collector_agent(state: FundState):
    """Collect analyst evidence into one PM-facing structured contract."""
    contract = build_signal_collection_contract(
        ticker=state["ticker"],
        trading_date=state["trading_date"],
        analyst_signals=state.get("analyst_signals", []),
        enabled_analysts=state.get("enabled_analysts", []),
    )
    return {
        "signal_collection_contract": contract,
        "signal_collection_contracts": [contract],
    }
