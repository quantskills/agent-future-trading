from __future__ import annotations

"""Hard-risk reason taxonomy for the deterministic auditor."""

HARD_BLOCK_REASONS = {
    "strategy_memory_weak_block",
    "adaptive_policy_block",
    "provisional_policy_block",
    "side_performance_block",
    "conditional_performance_block",
    "weak_ticker_side_quality_gate",
    "news_only_directional_trade",
    "low_quality_news_driven_trade",
    "strict_ticker_side_quality_gate",
}


def has_hard_block_reason(reasons: list[str], softened_reasons: set[str] | None = None) -> bool:
    softened = {str(reason) for reason in (softened_reasons or set())}
    return any(str(reason) in HARD_BLOCK_REASONS and str(reason) not in softened for reason in reasons or [])
