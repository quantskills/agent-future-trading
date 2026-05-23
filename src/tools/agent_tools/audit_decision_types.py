from __future__ import annotations

"""Audit decision vocabulary for deterministic trade gating."""

AUDIT_DECISIONS = ("allow", "scale_down", "probe_only", "reduce_only", "block", "hold")


def normalize_audit_decision(decision: str, multiplier: float) -> str:
    value = str(decision or "allow")
    if value == "reduce":
        value = "scale_down"
    if value in {"scale_down", "probe_only"} and float(multiplier or 0.0) <= 1e-12:
        return "block"
    if value not in AUDIT_DECISIONS:
        return "allow"
    return value


def hard_block_or_reduce_only(*, target_ratio: float, current_ratio: float) -> str:
    same_side = (target_ratio > 0 and current_ratio > 0) or (target_ratio < 0 and current_ratio < 0)
    return "reduce_only" if same_side else "block"
