from __future__ import annotations

"""Soft-risk utilities for analyst-quality and cold-start checks."""


def fallback_business_quality_score(tradeability: str, confidence: float) -> float:
    base = {
        "high": 0.75,
        "medium": 0.62,
        "low": 0.32,
        "unknown": 0.45,
    }.get(str(tradeability or "unknown").lower(), 0.45)
    return max(0.0, min(1.0, 0.70 * base + 0.30 * max(0.0, min(1.0, float(confidence or 0.0)))))
