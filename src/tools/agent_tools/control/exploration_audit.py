from __future__ import annotations

"""Classify why an exploration/probe happened."""

from typing import Any, Dict, Iterable, List


def _reason_codes(payload: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    for key in ("reason_codes", "reasons", "learning_used", "evidence_used"):
        value = payload.get(key)
        if isinstance(value, list):
            reasons.extend(str(x) for x in value)
        elif value:
            reasons.append(str(value))
    return reasons


def classify_exploration_intent(final_action_contract: Dict[str, Any]) -> str:
    if not isinstance(final_action_contract, dict):
        return "unknown"
    authority = str(final_action_contract.get("authority_type") or "")
    reasons = " ".join(_reason_codes(final_action_contract)).lower()
    if authority not in {"exploration_probe", "probe_only", "watchlist_only", "direction_only"} and "probe" not in reasons:
        return "not_exploration"
    if "positive_candidate" in reasons or "alpha_promotion" in reasons:
        return "positive_alpha_promotion"
    if "negative_revalidate" in reasons or "tail_loss" in reasons:
        return "negative_setup_revalidation"
    if "shadow" in reasons or authority in {"watchlist_only", "direction_only"}:
        return "shadow_no_trade_observation"
    if "execution" in reasons or "timing" in reasons or "vwap" in reasons:
        return "execution_timing_experiment"
    if "new_setup" in reasons or authority in {"exploration_probe", "probe_only"}:
        return "new_setup_exploration"
    return "unknown"


def summarize_exploration_intents(contracts: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for contract in contracts or []:
        intent = classify_exploration_intent(contract)
        summary[intent] = summary.get(intent, 0) + 1
    return summary
