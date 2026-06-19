from __future__ import annotations

"""Backtest/simulation interpretation parity checks."""

from typing import Any, Dict, List

from tools.agent_tools.control.schemas import ProtocolCheckResult


PARITY_FIELDS = [
    "final_action",
    "authority_type",
    "current_lots",
    "target_lots",
    "lots_delta",
    "entry_trigger",
]


def compare_contract_interpretation(
    backtest_contract: Dict[str, Any],
    simulation_contract: Dict[str, Any],
) -> ProtocolCheckResult:
    errors: List[str] = []
    metadata: Dict[str, Any] = {"field_comparison": {}}
    if not isinstance(backtest_contract, dict) or not isinstance(simulation_contract, dict):
        return ProtocolCheckResult.fail_result(["contract_payload_missing"])

    for field in PARITY_FIELDS:
        left = backtest_contract.get(field)
        right = simulation_contract.get(field)
        metadata["field_comparison"][field] = {"backtest": left, "simulation": right}
        if left != right:
            errors.append(f"parity_mismatch:{field}")

    return ProtocolCheckResult.fail_result(errors, metadata=metadata) if errors else ProtocolCheckResult.pass_result(
        metadata=metadata
    )
