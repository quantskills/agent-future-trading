from __future__ import annotations

"""Position lifecycle and signed-ratio helpers.

These helpers keep PM and trader behavior consistent: scale only incremental
risk, preserve existing same-side exposure when a soft cap is applied, and
avoid hidden direction changes in sizing code.
"""


def same_sign(lhs: float, rhs: float) -> bool:
    return (lhs > 0 and rhs > 0) or (lhs < 0 and rhs < 0)


def target_side_from_ratio(position_ratio: float) -> str:
    if position_ratio > 0:
        return "long"
    if position_ratio < 0:
        return "short"
    return "flat"


def is_new_or_increasing_exposure(target_ratio: float, current_ratio: float) -> bool:
    if abs(target_ratio) <= 1e-12:
        return False
    if abs(current_ratio) <= 1e-12:
        return True
    if not same_sign(target_ratio, current_ratio):
        return True
    return abs(target_ratio) > abs(current_ratio)


def scale_signed_ratio(position_ratio: float, multiplier: float) -> float:
    return (1.0 if position_ratio >= 0 else -1.0) * abs(position_ratio) * max(0.0, float(multiplier or 0.0))


def apply_trade_plan_multiplier(*, target_ratio: float, current_ratio: float, multiplier: float) -> float:
    scaled_ratio = scale_signed_ratio(target_ratio, multiplier)
    if same_sign(target_ratio, current_ratio) and abs(current_ratio) > abs(scaled_ratio):
        return current_ratio
    return scaled_ratio


def cap_signed_lots_by_abs_limit(target_lots: int, abs_limit: int) -> int:
    abs_limit = abs(int(abs_limit or 0))
    if target_lots > abs_limit:
        return abs_limit
    if target_lots < -abs_limit:
        return -abs_limit
    return int(target_lots or 0)
