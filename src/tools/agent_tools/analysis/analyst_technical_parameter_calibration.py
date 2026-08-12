from __future__ import annotations

"""Context-scoped technical-parameter calibration from researcher memory."""

from copy import deepcopy
from typing import Any, Iterable, Mapping

from tools.agent_tools.decision.pm_contextual_rule_calibration import (
    select_contextual_rule_calibrations,
)


TECHNICAL_RULE_SPECS: dict[str, tuple[tuple[str, str], str, float, float]] = {
    "trend_short_multiplier": (("trend", "short"), "multiplier", 0.85, 1.15),
    "trend_long_multiplier": (("trend", "long"), "multiplier", 0.85, 1.15),
    "rsi_bullish_shift": (("rsi", "bullish"), "shift", -5.0, 5.0),
    "rsi_bearish_shift": (("rsi", "bearish"), "shift", -5.0, 5.0),
    "bollinger_std_multiplier": (("mean_reversion", "bollinger_std"), "multiplier", 0.90, 1.10),
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _bounded(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _nested_get(payload: Mapping[str, Any], section: str, key: str, default: Any = None) -> Any:
    child = payload.get(section)
    if isinstance(child, Mapping):
        return child.get(key, default)
    return default


def _nested_set(payload: dict[str, Any], section: str, key: str, value: Any) -> None:
    child = payload.setdefault(section, {})
    if isinstance(child, dict):
        child[key] = value


def _apply_rule_value(base_value: Any, rule_value: Any, mode: str, lower: float, upper: float) -> Any:
    base = _safe_float(base_value, 0.0)
    raw = _bounded(_safe_float(rule_value, 1.0 if mode == "multiplier" else 0.0), lower, upper)
    adjusted = base * raw if mode == "multiplier" else base + raw
    if isinstance(base_value, int):
        return max(1, int(round(adjusted)))
    return round(adjusted, 4)


def apply_technical_parameter_calibration(
    adaptive_params: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    ticker: str,
    side: str = "*",
    horizon_class: str = "short",
    market_regime: str = "*",
    min_confidence: float = 0.35,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply bounded technical-parameter calibration without mutating input.

    The row selector enforces ticker/side/horizon/regime scope matching. This
    helper only supports small bounded nudges, never wholesale indicator rewrites.
    """

    params = deepcopy(dict(adaptive_params or {}))
    applied: list[dict[str, Any]] = []
    ticker_value = str(ticker or "").upper()
    product_rows = [
        row
        for row in rows or []
        if isinstance(row, Mapping)
        and str(row.get("ticker") or "").upper() == ticker_value
    ]
    selected_rows = select_contextual_rule_calibrations(
        product_rows,
        rule_group="technical_parameters",
        ticker=ticker,
        side=side,
        horizon_class=horizon_class,
        market_regime=market_regime,
        min_confidence=min_confidence,
        min_sample_count=2,
    )
    best_scope_score = max(
        (int(row.get("_rule_scope_score") or 0) for row in selected_rows),
        default=None,
    )
    for row in selected_rows:
        if best_scope_score is not None and int(row.get("_rule_scope_score") or 0) != best_scope_score:
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        rules_by_group = payload.get("rule_adjustments") if isinstance(payload, Mapping) else {}
        rules = rules_by_group.get("technical_parameters") if isinstance(rules_by_group, Mapping) else {}
        if not isinstance(rules, Mapping):
            continue
        changed: dict[str, Any] = {}
        for key, (path, mode, lower, upper) in TECHNICAL_RULE_SPECS.items():
            if key not in rules:
                continue
            section, param_key = path
            original = _nested_get(params, section, param_key)
            if original is None:
                continue
            adjusted = _apply_rule_value(original, rules.get(key), mode, lower, upper)
            _nested_set(params, section, param_key, adjusted)
            if adjusted == original:
                continue
            changed[f"{section}.{param_key}"] = {"from": original, "to": adjusted, "rule": key}
        if changed:
            applied.append(
                {
                    "id": row.get("id"),
                    "policy_type": row.get("policy_type"),
                    "policy_action": row.get("policy_action"),
                    "ticker": row.get("ticker"),
                    "side": row.get("side"),
                    "setup_type": row.get("setup_type"),
                    "horizon_class": row.get("horizon_class"),
                    "market_regime": row.get("market_regime"),
                    "source_trading_date": row.get("source_trading_date"),
                    "valid_until": row.get("valid_until"),
                    "confidence_score": _safe_float(row.get("confidence_score"), 0.0),
                    "sample_count": int(_safe_float(row.get("sample_count"), 0.0)),
                    "reason": row.get("reason"),
                    "changed": changed,
                }
            )

    diagnostics = {
        "enabled": True,
        "rule_group": "technical_parameters",
        "applied": applied,
        "scope": {
            "ticker": ticker,
            "side": side,
            "horizon_class": horizon_class,
            "market_regime": market_regime,
        },
        "bounded": {
            key: {"lower": spec[2], "upper": spec[3]}
            for key, spec in TECHNICAL_RULE_SPECS.items()
        },
    }
    return params, diagnostics
