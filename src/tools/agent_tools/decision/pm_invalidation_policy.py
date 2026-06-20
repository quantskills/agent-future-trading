"""Pre-trade invalidation policy helpers for portfolio decisions."""

from __future__ import annotations

from typing import Any

from tools.agent_tools.decision.position_lifecycle import (
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


def _has_explicit_stop_protection(signals: list) -> bool:
    for signal in signals or []:
        if getattr(signal, "invalidation_level", None) is not None:
            return True
        try:
            if float(getattr(signal, "atr_stop_distance", 0.0) or 0.0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _has_structured_invalidation_condition(signals: list) -> bool:
    """Return True when analysts stated what would invalidate the trade idea."""
    structured_fields = (
        "counter_evidence",
        "would_change_view_if",
        "do_not_trade_reason",
    )
    for signal in signals or []:
        metadata = _signal_metadata(signal)
        action_contract = metadata.get("action_evidence_contract")
        if isinstance(action_contract, dict):
            if "invalidation_present" in action_contract:
                if bool(action_contract.get("invalidation_present")):
                    return True
                continue
            contract_condition = action_contract.get("invalidation_condition")
            if isinstance(contract_condition, str) and _specific_invalidation_text(contract_condition):
                return True
            if isinstance(contract_condition, (list, tuple, set)) and any(
                _specific_invalidation_text(item) for item in contract_condition
            ):
                return True
            continue
        if getattr(signal, "invalidation_level", None) is not None:
            return True
        try:
            if float(getattr(signal, "atr_stop_distance", 0.0) or 0.0) > 0:
                return True
        except (TypeError, ValueError):
            pass
        for field in structured_fields:
            value = getattr(signal, field, None)
            if isinstance(value, str) and _specific_invalidation_text(value):
                return True
            if isinstance(value, (list, tuple, set)) and any(_specific_invalidation_text(item) for item in value):
                return True
        for key in ("invalidation_condition", "risk_boundary", "counter_evidence"):
            value = metadata.get(key)
            if isinstance(value, str) and _specific_invalidation_text(value):
                return True
            if isinstance(value, (list, tuple, set)) and any(_specific_invalidation_text(item) for item in value):
                return True
    return False


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
    if _has_structured_invalidation_condition(analyst_signals):
        diagnostics["pretrade_invalidation"] = {"present": True}
        return position_ratio, reasons, notes, diagnostics

    cap_multiplier = max(0.0, min(1.0, float(lifecycle_config.get("missing_invalidation_cap_multiplier", 0.35))))
    probe_cap = max(0.0, float(lifecycle_config.get("missing_invalidation_probe_max_ratio", 0.02)))
    capped_abs = min(abs(position_ratio), abs(max_position_ratio) * cap_multiplier, probe_cap)
    before = position_ratio
    position_ratio = _scale_signed_ratio(position_ratio, capped_abs / abs(position_ratio)) if abs(position_ratio) > 0 else 0.0
    reasons.append("missing_pretrade_invalidation")
    notes.append(
        f"{ticker} new/increased exposure lacks structured invalidation; ratio {before:.2%}->{position_ratio:.2%}"
    )
    diagnostics["pretrade_invalidation"] = {
        "present": False,
        "cap_multiplier": cap_multiplier,
        "probe_max_ratio": probe_cap,
        "ratio_before": float(before),
        "ratio_after": float(position_ratio),
    }
    return position_ratio, reasons, notes, diagnostics


__all__ = [
    "_apply_pretrade_invalidation_control",
    "_has_explicit_stop_protection",
    "_has_structured_invalidation_condition",
]
