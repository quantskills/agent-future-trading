from __future__ import annotations

"""Context-scoped weak-parameter calibration from researcher policy state.

These helpers keep deterministic trading rules intact by default, while
allowing validated or provisional researcher observations to tune soft
thresholds by ticker, side, horizon, signal template, and market regime.
"""

from copy import deepcopy
from typing import Any, Iterable, Mapping


POLICY_TYPE = "contextual_rule_calibration"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _normalize_side(value: Any) -> str:
    text = str(value or "*").strip().lower()
    return text if text in {"long", "short", "flat", "*"} else "*"


def _normalize_text(value: Any, default: str = "*") -> str:
    text = str(value or default).strip()
    return text if text else default


def _row_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _row_rules(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = _row_payload(row)
    rules = payload.get("rule_adjustments")
    return rules if isinstance(rules, Mapping) else {}


def _rule_scope_score(row: Mapping[str, Any], *, ticker: str, side: str, horizon_class: str, market_regime: str) -> int:
    score = 0
    row_ticker = str(row.get("ticker") or "*").upper()
    row_side = _normalize_side(row.get("side"))
    row_horizon = _normalize_text(row.get("horizon_class"))
    row_regime = _normalize_text(row.get("market_regime"))
    if row_ticker == str(ticker or "").upper():
        score += 8
    elif row_ticker != "*":
        return -1
    if row_side == _normalize_side(side):
        score += 4
    elif row_side != "*":
        return -1
    if row_horizon == _normalize_text(horizon_class):
        score += 2
    elif row_horizon != "*":
        return -1
    if row_regime == _normalize_text(market_regime):
        score += 1
    elif row_regime != "*":
        return -1
    return score


def select_contextual_rule_calibrations(
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    rule_group: str,
    ticker: str,
    side: str = "*",
    horizon_class: str = "*",
    market_regime: str = "*",
    min_confidence: float = 0.35,
    min_sample_count: int = 1,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        if not str(row.get("policy_type") or "").startswith(POLICY_TYPE):
            continue
        if _safe_float(row.get("confidence_score"), 0.0) < min_confidence:
            continue
        if _safe_int(row.get("sample_count"), 0) < min_sample_count:
            continue
        rules = _row_rules(row)
        if rule_group not in rules:
            continue
        score = _rule_scope_score(
            row,
            ticker=ticker,
            side=side,
            horizon_class=horizon_class,
            market_regime=market_regime,
        )
        if score < 0:
            continue
        candidate = dict(row)
        candidate["_rule_scope_score"] = score
        selected.append(candidate)
    selected.sort(
        key=lambda item: (
            int(item.get("_rule_scope_score") or 0),
            _safe_int(item.get("sample_count"), 0),
            _safe_float(item.get("confidence_score"), 0.0),
        ),
        reverse=True,
    )
    return selected


def _bounded(value: float, *, lower: float | None = None, upper: float | None = None) -> float:
    result = value
    if lower is not None:
        result = max(lower, result)
    if upper is not None:
        result = min(upper, result)
    return result


def _set_nested_rule(target: dict[str, Any], path: tuple[str, str], value: Any) -> None:
    section, key = path
    child = target.setdefault(section, {})
    if isinstance(child, dict):
        child[key] = value


PM_RULE_SPECS: dict[str, tuple[tuple[str, str], str, float | None, float | None]] = {
    "new_loss_revalidation_min_confirmation_score": (("position_lifecycle", "new_loss_revalidation_min_confirmation_score"), "float", 0.35, 0.85),
    "new_loss_revalidation_min_signal_strength": (("position_lifecycle", "new_loss_revalidation_min_signal_strength"), "float", 0.10, 0.60),
    "new_loss_revalidation_reduction_multiplier": (("position_lifecycle", "new_loss_revalidation_reduction_multiplier"), "float", 0.10, 0.85),
    "loss_revalidation_min_confirmation_score": (("position_lifecycle", "loss_revalidation_min_confirmation_score"), "float", 0.35, 0.85),
    "loss_revalidation_min_signal_strength": (("position_lifecycle", "loss_revalidation_min_signal_strength"), "float", 0.10, 0.60),
    "loss_revalidation_reduction_multiplier": (("position_lifecycle", "loss_revalidation_reduction_multiplier"), "float", 0.10, 0.85),
    "probe_max_hold_days": (("position_lifecycle", "probe_max_hold_days"), "int", 1, 10),
    "probe_min_confirmation_score": (("position_lifecycle", "probe_min_confirmation_score"), "float", 0.35, 0.85),
    "medium_requires_short_timing": (("horizon_consistency", "medium_requires_short_timing"), "bool", None, None),
    "medium_requires_invalidation": (("horizon_consistency", "medium_requires_invalidation"), "bool", None, None),
    "min_short_timing_confidence": (("horizon_consistency", "min_short_timing_confidence"), "float", 0.25, 0.75),
    "min_confirmation_score": (("horizon_consistency", "min_confirmation_score"), "float", 0.35, 0.85),
    "losing_hold_reduction_multiplier": (("horizon_consistency", "losing_hold_reduction_multiplier"), "float", 0.10, 0.85),
}


INTRADAY_RULE_SPECS: dict[str, tuple[str, float | None, float | None]] = {
    "confirmed_memory_max_opening_range_miss": ("float", 0.0, 0.006),
    "confirmed_memory_min_market_confirmation_score": ("float", 0.55, 0.90),
    "confirmed_memory_min_confirmations": ("int", 1, 5),
    "max_chase_ratio": ("float", 0.005, 0.03),
    "opening_range_minutes": ("int", 5, 60),
}


def _coerce_rule_value(value: Any, kind: str, lower: float | None, upper: float | None, default: Any) -> Any:
    if kind == "bool":
        return bool(value)
    if kind == "int":
        return int(_bounded(float(_safe_int(value, default)), lower=lower, upper=upper))
    return _bounded(_safe_float(value, default), lower=lower, upper=upper)


def apply_pm_contextual_calibration(
    holding_control: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    ticker: str,
    side: str,
    horizon_class: str,
    market_regime: str,
    min_confidence: float = 0.35,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a calibrated PM holding control without mutating the input."""
    control = deepcopy(dict(holding_control or {}))
    applied: list[dict[str, Any]] = []
    for row in select_contextual_rule_calibrations(
        rows,
        rule_group="portfolio_manager",
        ticker=ticker,
        side=side,
        horizon_class=horizon_class,
        market_regime=market_regime,
        min_confidence=min_confidence,
    ):
        rules = (_row_rules(row).get("portfolio_manager") or {})
        if not isinstance(rules, Mapping):
            continue
        for key, (path, kind, lower, upper) in PM_RULE_SPECS.items():
            if key not in rules:
                continue
            default_section = control.get(path[0]) if isinstance(control.get(path[0]), dict) else {}
            default = default_section.get(path[1]) if isinstance(default_section, dict) else None
            _set_nested_rule(control, path, _coerce_rule_value(rules.get(key), kind, lower, upper, default))
        applied.append(_applied_summary(row, rules))
    diagnostics = {
        "enabled": True,
        "rule_group": "portfolio_manager",
        "applied": applied,
        "scope": {
            "ticker": ticker,
            "side": side,
            "horizon_class": horizon_class,
            "market_regime": market_regime,
        },
    }
    return control, diagnostics


def apply_auditor_contextual_calibration(
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    ticker: str,
    side: str,
    horizon_class: str,
    market_regime: str,
    reasons: list[str],
    allowed_soften_reasons: set[str],
    min_confidence: float = 0.35,
) -> tuple[set[str], dict[str, Any]]:
    allowed = {str(reason) for reason in (allowed_soften_reasons or set())}
    soften: set[str] = set()
    applied: list[dict[str, Any]] = []
    for row in select_contextual_rule_calibrations(
        rows,
        rule_group="trade_auditor",
        ticker=ticker,
        side=side,
        horizon_class=horizon_class,
        market_regime=market_regime,
        min_confidence=min_confidence,
    ):
        rules = (_row_rules(row).get("trade_auditor") or {})
        if not isinstance(rules, Mapping):
            continue
        for reason in rules.get("soften_hard_block_reasons") or []:
            text = str(reason or "").strip()
            if text and (not allowed or text in allowed):
                soften.add(text)
        applied.append(_applied_summary(row, rules))
    active = sorted(set(reasons or []) & soften)
    return soften, {
        "enabled": True,
        "rule_group": "trade_auditor",
        "softened_reasons": active,
        "applied": applied,
        "scope": {
            "ticker": ticker,
            "side": side,
            "horizon_class": horizon_class,
            "market_regime": market_regime,
        },
    }


def apply_intraday_contextual_calibration(
    intraday_config: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    ticker: str,
    side: str,
    horizon_class: str,
    market_regime: str,
    min_confidence: float = 0.35,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = deepcopy(dict(intraday_config or {}))
    applied: list[dict[str, Any]] = []
    for row in select_contextual_rule_calibrations(
        rows,
        rule_group="intraday_confirmation",
        ticker=ticker,
        side=side,
        horizon_class=horizon_class,
        market_regime=market_regime,
        min_confidence=min_confidence,
    ):
        rules = (_row_rules(row).get("intraday_confirmation") or {})
        if not isinstance(rules, Mapping):
            continue
        for key, (kind, lower, upper) in INTRADAY_RULE_SPECS.items():
            if key in rules:
                config[key] = _coerce_rule_value(rules.get(key), kind, lower, upper, config.get(key))
        applied.append(_applied_summary(row, rules))
    diagnostics = {
        "enabled": True,
        "rule_group": "intraday_confirmation",
        "applied": applied,
        "scope": {
            "ticker": ticker,
            "side": side,
            "horizon_class": horizon_class,
            "market_regime": market_regime,
        },
    }
    return config, diagnostics


def _applied_summary(row: Mapping[str, Any], rules: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "policy_action": row.get("policy_action"),
        "confidence_score": _safe_float(row.get("confidence_score"), 0.0),
        "sample_count": _safe_int(row.get("sample_count"), 0),
        "reason": row.get("reason"),
        "rules": dict(rules),
    }
