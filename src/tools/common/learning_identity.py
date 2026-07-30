from __future__ import annotations

"""Canonical identity helpers for formal FAC-scoped learning."""

import re
from typing import Any, Mapping


_MISSING_IDENTITY_TOKENS = {"", "*", "unknown", "none", "null", "nan"}


def canonical_market_regime(value: Any, default: str = "unknown") -> str:
    """Return the one persisted/query form of a market-regime token."""

    text = str(value or "").strip().lower()
    if text in {"", "none", "null", "nan"}:
        return default
    text = re.sub(r"[\s/]+", "_", text)
    text = "".join(ch for ch in text if ch.isalnum() or ch in {"_", "-", "*"})
    return re.sub(r"_+", "_", text).strip("_") or default


def formal_fac_learning_identity(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read the complete formal-learning identity directly from one FAC."""

    fac = contract if isinstance(contract, Mapping) else {}
    setup_type = str(fac.get("setup_type") or "").strip()
    horizon_class = str(fac.get("horizon_class") or "").strip().lower()
    try:
        expected_horizon_days = int(fac.get("expected_horizon_days") or 0)
    except (TypeError, ValueError):
        expected_horizon_days = 0
    market_regime = canonical_market_regime(fac.get("market_regime"), "")
    missing_fields: list[str] = []
    if setup_type.lower() in _MISSING_IDENTITY_TOKENS:
        missing_fields.append("setup_type")
    if horizon_class in _MISSING_IDENTITY_TOKENS:
        missing_fields.append("horizon_class")
    if expected_horizon_days <= 0:
        missing_fields.append("expected_horizon_days")
    if market_regime in _MISSING_IDENTITY_TOKENS:
        missing_fields.append("market_regime")
    return {
        "setup_type": setup_type,
        "horizon_class": horizon_class,
        "expected_horizon_days": expected_horizon_days,
        "market_regime": market_regime,
        "complete": not missing_fields,
        "missing_fields": sorted(missing_fields),
        "source": "final_action_contract",
    }
