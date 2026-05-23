from __future__ import annotations

"""Audit explanation payload builder."""

from typing import Any, Dict, Sequence


def _state_bucket(value: Any, default: str = "unknown") -> str:
    if isinstance(value, dict):
        for key in ("market_state", "regime", "state", "trend_state"):
            if value.get(key):
                return str(value.get(key))
    text = str(value or "").strip()
    return text if text else default


def _confirmation_bucket(market_confirmation: Dict[str, Any]) -> str:
    try:
        score = float((market_confirmation or {}).get("confirmation_score") or 0.0)
    except Exception:
        score = 0.0
    if score >= 0.70:
        return "strong"
    if score >= 0.55:
        return "medium"
    if score > 0:
        return "weak"
    return "none"


def signal_combo_text(signal_combo: Sequence[Any]) -> str:
    values = [str(item) for item in list(signal_combo or [])]
    while len(values) < 3:
        values.append("Neutral")
    return "/".join(values[:3])


def build_audit_state_key(*, ticker: str, target_side: str, signal_combo: Sequence[Any], market_state: Any, market_confirmation: Dict[str, Any]) -> str:
    return "|".join(
        [
            str(ticker).upper(),
            str(target_side),
            signal_combo_text(signal_combo),
            _state_bucket(market_state),
            _confirmation_bucket(market_confirmation or {}),
        ]
    )


def build_audit_payload(
    *,
    policy_version: str,
    learning_mode: str,
    state_key: str,
    decision: str,
    diagnostics: Dict[str, Any],
    memory_reads: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "policy_version": policy_version,
        "learning_mode": learning_mode,
        "pre_open_only": True,
        "info_cutoff": "pre_open",
        "state_key": state_key,
        "action": decision,
        "reward_status": "pending",
        "reward_source": "completed_trade_pair_after_close",
        "target_support": diagnostics.get("target_support", {}),
        "analyst_quality": diagnostics.get("analyst_quality", {}),
        "memory_reads": memory_reads,
    }
