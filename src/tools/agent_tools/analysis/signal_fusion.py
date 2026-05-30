from __future__ import annotations

"""Signal-fusion helpers shared by PM, reviewer tests, and future refactors."""

from typing import Any, Iterable, Mapping


ANALYST_ORDER = ("technical", "fundamental", "commodity_news")


def normalize_analyst_name(value: Any) -> str:
    text = str(value or "")
    return "commodity_news" if text == "company_news" else text


def signal_to_text(value: Any) -> str:
    return str(value.value) if hasattr(value, "value") else str(value or "Neutral")


def analyst_signal_combo(analyst_signals: Iterable[Any]) -> tuple[str, str, str]:
    signals = {name: "Neutral" for name in ANALYST_ORDER}
    for signal in analyst_signals or []:
        agent_name = normalize_analyst_name(getattr(signal, "agent_name", ""))
        if agent_name in signals:
            signals[agent_name] = signal_to_text(getattr(signal, "signal", "Neutral"))
    return (signals["technical"], signals["fundamental"], signals["commodity_news"])


def resolve_decision_horizon(analyst_signals: Iterable[Any], target_lots: int) -> str:
    if target_lots == 0:
        return "flat"
    target_signal = "Bullish" if target_lots > 0 else "Bearish"
    horizons = []
    for signal in analyst_signals or []:
        if signal_to_text(getattr(signal, "signal", "Neutral")) != target_signal:
            continue
        horizons.append(
            str(
                getattr(signal, "analyst_horizon", None)
                or getattr(signal, "horizon_class", None)
                or "unknown"
            )
        )
    if "medium" in horizons:
        return "medium"
    if "event_short" in horizons:
        return "event_short"
    if "short" in horizons:
        return "short"
    return "unknown"


def build_horizon_scope(
    analyst_signals: Iterable[Any],
    *,
    decision_horizon: str,
    execution_horizon: str = "short",
    validation_horizon: str | None = None,
) -> dict[str, Any]:
    analyst_horizons: dict[str, dict[str, Any]] = {}
    for signal in analyst_signals or []:
        agent_name = normalize_analyst_name(getattr(signal, "agent_name", ""))
        if not agent_name:
            continue
        analyst_horizons[agent_name] = {
            "analyst_horizon": str(getattr(signal, "analyst_horizon", "") or getattr(signal, "horizon_class", "") or "unknown"),
            "horizon_class": str(getattr(signal, "horizon_class", "") or "unknown"),
            "expected_horizon_days": int(getattr(signal, "expected_horizon_days", 0) or getattr(signal, "horizon_days", 0) or 0),
        }
    return {
        "analyst_horizons": analyst_horizons,
        "decision_horizon": decision_horizon or "unknown",
        "execution_horizon": execution_horizon or "short",
        "validation_horizon": validation_horizon or decision_horizon or "unknown",
    }


def dominant_business_quality(analyst_payloads: Iterable[Mapping[str, Any]], target_signal: str) -> float:
    scores = []
    for payload in analyst_payloads or []:
        if not isinstance(payload, Mapping):
            continue
        if str(payload.get("signal") or "") == target_signal:
            try:
                scores.append(float(payload.get("business_quality_score") or 0.0))
            except Exception:
                scores.append(0.0)
    return max(scores, default=0.0)
