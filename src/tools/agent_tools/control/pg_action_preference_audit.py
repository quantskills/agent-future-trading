from __future__ import annotations

"""Audit whether learned action preferences reached PM/Trader decisions."""

from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _extract_final_contract(pm_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(pm_snapshot, dict):
        return {}
    value = pm_snapshot.get("final_action_contract")
    if isinstance(value, dict):
        return value
    nested = pm_snapshot.get("signal_snapshot")
    if isinstance(nested, dict):
        value = nested.get("final_action_contract")
        if isinstance(value, dict):
            return value
    return {}


def audit_action_preference_landing(
    *,
    research_preferences: Iterable[Dict[str, Any]],
    pm_snapshot: Dict[str, Any],
    trader_snapshot: Optional[Dict[str, Any]] = None,
    settlement_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return an observational audit report without mutating inputs."""

    pm_snapshot_copy = deepcopy(pm_snapshot)
    prefs = [p for p in research_preferences or [] if isinstance(p, dict)]
    final_contract = _extract_final_contract(pm_snapshot)
    reason_codes = set(_as_list(final_contract.get("reason_codes")) + _as_list(pm_snapshot.get("reason_codes")))
    learning_used = _as_list(final_contract.get("learning_used")) + _as_list(pm_snapshot.get("learning_used"))
    evidence_used = _as_list(final_contract.get("evidence_used")) + _as_list(pm_snapshot.get("evidence_used"))

    preference_names = {
        str(p.get("action_preference") or "")
        for p in prefs
    }
    pm_learning_text = " ".join(str(x) for x in [*learning_used, *evidence_used, *reason_codes])
    pm_read_preference = any(name and name in pm_learning_text for name in preference_names)
    pm_changed_trade_terms = any(
        final_contract.get(key) is not None
        for key in ("authority_type", "target_lots", "lots_delta", "margin_ratio", "max_allowed_margin_ratio")
    )

    trader_snapshot = trader_snapshot or {}
    settlement_snapshot = settlement_snapshot or {}
    report = {
        "researcher_wrote_preference": bool(prefs),
        "preference_count": len(prefs),
        "pm_read_preference": pm_read_preference,
        "pm_changed_trade_terms": pm_changed_trade_terms,
        "authority_type": final_contract.get("authority_type"),
        "target_lots": final_contract.get("target_lots"),
        "lots_delta": final_contract.get("lots_delta"),
        "margin_ratio": final_contract.get("margin_ratio") or final_contract.get("max_allowed_margin_ratio"),
        "trader_triggered": str(trader_snapshot.get("execution_status") or "").lower()
        in {"triggered", "executed", "filled"},
        "execution_reason": trader_snapshot.get("execution_reason") or trader_snapshot.get("skip_reason"),
        "reward_observed": settlement_snapshot.get("pnl") is not None or settlement_snapshot.get("daily_pnl") is not None,
        "reward": settlement_snapshot.get("pnl", settlement_snapshot.get("daily_pnl")),
        "input_mutated": pm_snapshot != pm_snapshot_copy,
    }
    return report
