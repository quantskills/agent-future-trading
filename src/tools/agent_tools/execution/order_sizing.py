from __future__ import annotations

"""Order-sizing utilities shared by trader and tests."""

from typing import Any, Mapping

from tools.agent_tools.decision.position_lifecycle import cap_signed_lots_by_abs_limit


def cap_target_lots_by_phase1_plan(target_lots: int, pre_open_plan: Mapping[str, Any] | None) -> int:
    if not isinstance(pre_open_plan, Mapping):
        return int(target_lots or 0)
    planned_target = pre_open_plan.get("target_lots_estimate")
    if planned_target is None:
        return int(target_lots or 0)

    planned_target_lots = int(planned_target or 0)
    target_lots = int(target_lots or 0)
    if planned_target_lots == 0:
        return 0 if target_lots != 0 else target_lots
    if target_lots == 0 or (target_lots > 0) != (planned_target_lots > 0):
        return target_lots
    return cap_signed_lots_by_abs_limit(target_lots, abs(planned_target_lots))


def lots_from_target_ratio(*, account_equity: float, target_position_ratio: float, current_price: float, multiplier: float) -> int:
    if current_price <= 0 or multiplier <= 0:
        return 0
    target_value = float(account_equity or 0.0) * float(target_position_ratio or 0.0)
    return int(target_value / (float(current_price) * float(multiplier)))
