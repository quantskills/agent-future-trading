"""Pre-trade invalidation policy helpers for portfolio decisions."""

from __future__ import annotations

from typing import Any

from tools.common.execution_trigger_semantics import (
    is_canonical_entry_invalidation_condition,
)
from tools.common.position_lifecycle import (
    is_new_or_increasing_exposure as _is_new_or_increasing_exposure,
    scale_signed_ratio as _scale_signed_ratio,
    target_side_from_ratio as _target_side_from_ratio,
)


_DEFAULT_POSITION_LIFECYCLE_CONTROL = {
    "require_pretrade_invalidation_for_new_entry": True,
    "missing_invalidation_cap_multiplier": 0.35,
    "missing_invalidation_probe_max_ratio": 0.02,
}


def _signal_metadata(signal: Any) -> dict:
    metadata = getattr(signal, "metadata", {}) or {}
    return metadata if isinstance(metadata, dict) else {}


def _specific_invalidation_text(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text or text in {"unknown", "none", "n/a", "null"}:
        return False
    generic = {
        "exit/reduce if current confirmation fails",
        "wait_for_trigger",
        "primary driver and secondary confirmation align with acceptable reward/risk",
    }
    if text in generic:
        return False
    return any(
        token in text
        for token in (
            "invalid",
            "fails",
            "failure",
            "breaks",
            "below",
            "above",
            "close",
            "stop",
            "exit",
            "reduce",
            "contradict",
            "conflict",
            "reverses",
            "loses",
            "price fails",
            "volume fails",
            "regime flips",
            "basis",
            "inventory",
        )
    )


def _positive_number(value: Any) -> bool:
    if isinstance(value, bool) or value in (None, ""):
        return False
    try:
        return float(value) > 0.0
    except (TypeError, ValueError):
        return False


def _fact_side(getter) -> str:
    side = str(getter("side") or "").strip().lower()
    if side in {"long", "short"}:
        return side
    signal_value = getter("signal")
    signal_text = str(getattr(signal_value, "value", signal_value) or "").strip().lower()
    if signal_text == "bullish":
        return "long"
    if signal_text == "bearish":
        return "short"
    if signal_text == "neutral":
        counterfactual_side = str(getter("counterfactual_side") or "").strip().lower()
        if counterfactual_side in {"long", "short"}:
            return counterfactual_side
    return ""


def _fact_matches_target_side(getter, target_side: str | None) -> bool:
    normalized_target = str(target_side or "").strip().lower()
    return normalized_target not in {"long", "short"} or _fact_side(getter) == normalized_target


def _has_explicit_stop_protection(
    signals: list,
    target_side: str | None = None,
) -> bool:
    """Return whether post-entry position protection is explicit.

    This deliberately excludes ``invalidation_level``.  That field belongs to
    the unfilled entry setup and can only cancel the same-day FAC before its
    first fill.  Position protection uses the separately landed position
    boundary, ATR distance, exit condition, or declared holding horizon.
    """
    for signal in signals or []:
        metadata = _signal_metadata(signal)
        action_contract = metadata.get("action_evidence_contract")
        facts = action_contract if isinstance(action_contract, dict) else signal
        getter = facts.get if isinstance(facts, dict) else lambda key: getattr(facts, key, None)
        if not _fact_matches_target_side(getter, target_side):
            continue
        if _positive_number(getter("position_invalidation_level")):
            return True
        if _positive_number(getter("atr_stop_distance")):
            return True
        if _specific_invalidation_text(getter("exit_hint")):
            return True
        if _positive_number(getter("expected_horizon_days")):
            return True
    return False


def _has_structured_invalidation_condition(
    signals: list,
    target_side: str | None = None,
) -> bool:
    """Return whether the unfilled entry setup has a canonical cancel boundary."""
    for signal in signals or []:
        metadata = _signal_metadata(signal)
        action_contract = metadata.get("action_evidence_contract")
        facts = action_contract if isinstance(action_contract, dict) else signal
        getter = facts.get if isinstance(facts, dict) else lambda key: getattr(facts, key, None)
        if not _fact_matches_target_side(getter, target_side):
            continue
        if not bool(getter("invalidation_present")):
            continue
        if not _positive_number(getter("invalidation_level")):
            continue
        side = _fact_side(getter)
        profile = getter("entry_timing_signal") or getter("execution_profile")
        if is_canonical_entry_invalidation_condition(
            getter("invalidation_condition") or getter("invalidation"),
            profile=profile,
            side=side,
        ):
            return True
    return False


def _has_position_exit_boundary(
    signals: list,
    target_side: str | None = None,
) -> bool:
    """Return whether the proposed position has a post-fill lifecycle basis."""
    return _has_explicit_stop_protection(signals, target_side=target_side)


def _position_lifecycle_config(full_config: dict | None) -> dict:
    pm_config = ((full_config or {}).get("portfolio_manager") or {})
    configured = (
        (pm_config.get("holding_rebalance_control") or {})
        .get("position_lifecycle", {})
        or {}
    )
    return {**_DEFAULT_POSITION_LIFECYCLE_CONTROL, **configured}


def _apply_pretrade_invalidation_control(
    *,
    ticker: str,
    position_ratio: float,
    current_ratio: float,
    max_position_ratio: float,
    analyst_signals: list,
    full_config: dict,
) -> tuple[float, list[str], list[str], dict]:
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    if not _is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, reasons, notes, diagnostics
    if _target_side_from_ratio(position_ratio) not in {"long", "short"}:
        return position_ratio, reasons, notes, diagnostics

    lifecycle_config = _position_lifecycle_config(full_config)
    if not bool(lifecycle_config.get("require_pretrade_invalidation_for_new_entry", True)):
        return position_ratio, reasons, notes, diagnostics
    target_side = _target_side_from_ratio(position_ratio)
    entry_invalidation_present = _has_structured_invalidation_condition(
        analyst_signals,
        target_side=target_side,
    )
    position_exit_boundary_present = _has_position_exit_boundary(
        analyst_signals,
        target_side=target_side,
    )
    if entry_invalidation_present and position_exit_boundary_present:
        diagnostics["pretrade_invalidation"] = {
            "entry_invalidation_present": True,
            "position_exit_boundary_present": True,
        }
        return position_ratio, reasons, notes, diagnostics

    cap_multiplier = max(0.0, min(1.0, float(lifecycle_config.get("missing_invalidation_cap_multiplier", 0.35))))
    probe_cap = max(0.0, float(lifecycle_config.get("missing_invalidation_probe_max_ratio", 0.02)))
    capped_abs = min(abs(position_ratio), abs(max_position_ratio) * cap_multiplier, probe_cap)
    before = position_ratio
    position_ratio = _scale_signed_ratio(position_ratio, capped_abs / abs(position_ratio)) if abs(position_ratio) > 0 else 0.0
    if not entry_invalidation_present:
        reasons.append("missing_pretrade_invalidation")
    if not position_exit_boundary_present:
        reasons.append("missing_position_exit_boundary")
    notes.append(
        f"{ticker} new/increased exposure lacks complete entry/position lifecycle boundaries; "
        f"entry_invalidation={entry_invalidation_present}, "
        f"position_exit={position_exit_boundary_present}; ratio {before:.2%}->{position_ratio:.2%}"
    )
    diagnostics["pretrade_invalidation"] = {
        "entry_invalidation_present": bool(entry_invalidation_present),
        "position_exit_boundary_present": bool(position_exit_boundary_present),
        "cap_multiplier": cap_multiplier,
        "probe_max_ratio": probe_cap,
        "ratio_before": float(before),
        "ratio_after": float(position_ratio),
    }
    return position_ratio, reasons, notes, diagnostics


__all__ = [
    "_apply_pretrade_invalidation_control",
    "_has_explicit_stop_protection",
    "_has_position_exit_boundary",
    "_has_structured_invalidation_condition",
]
