from __future__ import annotations

"""Order-sizing utilities shared by trader and tests."""


def lots_from_target_ratio(*, account_equity: float, target_position_ratio: float, current_price: float, multiplier: float) -> int:
    if current_price <= 0 or multiplier <= 0:
        return 0
    target_value = float(account_equity or 0.0) * float(target_position_ratio or 0.0)
    return int(target_value / (float(current_price) * float(multiplier)))
