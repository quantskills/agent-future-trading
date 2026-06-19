from __future__ import annotations

"""Shared soft risk-control helpers for PM and auditor."""

from typing import Any, Iterable

from tools.agent_tools.decision.position_lifecycle import (
    is_new_or_increasing_exposure,
    scale_signed_ratio,
    target_side_from_ratio,
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _signal_text(value: Any) -> str:
    return str(value.value) if hasattr(value, "value") else str(value or "Neutral")


def business_quality_position_gate(
    *,
    position_ratio: float,
    current_ratio: float,
    analyst_signals: Iterable[Any],
    config: dict[str, Any],
) -> tuple[float, list[str], list[str], dict[str, Any]]:
    cfg = (config or {}).get("analyst_business_quality") or {}
    if not bool(cfg.get("enabled", True)):
        return position_ratio, [], [], {}
    if not is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, [], [], {}
    target_side = target_side_from_ratio(position_ratio)
    if target_side not in {"long", "short"}:
        return position_ratio, [], [], {}

    min_probe = _safe_float(cfg.get("min_score_for_probe"), 0.45)
    min_deploy = _safe_float(cfg.get("min_score_for_deployable"), 0.60)
    probe_multiplier = _safe_float(cfg.get("probe_multiplier"), 0.25)
    low_multiplier = _safe_float(cfg.get("low_quality_multiplier"), 0.0)
    if bool(cfg.get("soft_gate_never_zero", False)) and low_multiplier <= 0:
        low_multiplier = max(0.05, _safe_float(cfg.get("minimum_soft_multiplier"), 0.10))
    target_signal = "Bullish" if target_side == "long" else "Bearish"
    directional_scores: list[float] = []
    all_scores: list[float] = []
    rows = []

    for signal in analyst_signals or []:
        signal_text = _signal_text(getattr(signal, "signal", "Neutral"))
        score = _safe_float(getattr(signal, "business_quality_score", 0.0), 0.0)
        all_scores.append(score)
        rows.append(
            {
                "agent_name": getattr(signal, "agent_name", ""),
                "signal": signal_text,
                "business_quality_score": score,
                "setup_type": getattr(signal, "setup_type", "unknown"),
                "primary_business_driver": getattr(signal, "primary_business_driver", ""),
                "counter_evidence": getattr(signal, "counter_evidence", ""),
            }
        )
        if signal_text == target_signal:
            directional_scores.append(score)

    best_directional = max(directional_scores or [0.0])
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0.0
    diagnostics = {
        "business_quality_gate": {
            "target_side": target_side,
            "best_directional_score": best_directional,
            "avg_score": avg_score,
            "min_probe": min_probe,
            "min_deployable": min_deploy,
            "rows": rows,
        }
    }
    if best_directional < min_probe:
        return (
            scale_signed_ratio(position_ratio, low_multiplier),
            ["business_quality_observe_or_block"],
            [
                f"business quality below probe threshold for {target_side}: "
                f"best_directional={best_directional:.2f} < {min_probe:.2f}"
            ],
            diagnostics,
        )
    if best_directional < min_deploy:
        return (
            scale_signed_ratio(position_ratio, probe_multiplier),
            ["business_quality_probe_only"],
            [
                f"business quality limited {target_side} to probe: "
                f"best_directional={best_directional:.2f} < {min_deploy:.2f}"
            ],
            diagnostics,
        )
    return position_ratio, ["business_quality_deployable"], [], diagnostics

