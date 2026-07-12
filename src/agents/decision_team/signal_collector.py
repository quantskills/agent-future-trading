"""Signal collector agent for the decision team."""

from __future__ import annotations

from graph.schema import FundState
from tools.agent_tools.decision.signal_collection_data_unavailable import (
    build_data_unavailable_signal_package,
)
from tools.common.signal_evidence_collection import (
    build_signal_collection_contract,
    validate_signal_collection_contract,
)


def signal_collector_agent(state: FundState):
    """Collect analyst evidence into one PM-facing structured contract."""
    if state.get("pre_open_reference_price_unavailable"):
        reason = (
            state.get("pre_open_reference_price_unavailable_reason")
            or state.get("pre_open_reference_price_unavailable_warning")
            or "pre_open_reference_price_unavailable"
        )
        return build_data_unavailable_signal_package(
            ticker=state["ticker"],
            trading_date=state["trading_date"],
            enabled_analysts=state.get("enabled_analysts", []),
            reason=str(reason),
            warning_message=state.get("pre_open_reference_price_unavailable_warning"),
        )

    contract = build_signal_collection_contract(
        ticker=state["ticker"],
        trading_date=state["trading_date"],
        analyst_signals=state.get("analyst_signals", []),
        enabled_analysts=state.get("enabled_analysts", []),
    )
    return {
        "signal_collection_contract": validate_signal_collection_contract(
            contract,
            ticker=state["ticker"],
            trading_date=state["trading_date"],
            enabled_analysts=state.get("enabled_analysts", []),
            analyst_signals=state.get("analyst_signals", []),
        )
    }
