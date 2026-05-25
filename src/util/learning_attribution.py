from __future__ import annotations

"""Helpers for attributing learning-driven trade interventions."""

from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Mapping

from util.futures_trade_pairs import summarize_trade_pairs


CAPITAL_LEARNING_REASONS = {
    "capital_utilization_memory_protected",
    "capital_utilization_same_side_add_on",
}

EVIDENCE_REJECTION_REASONS = {
    "protected_memory_evidence_rejected",
    "protected_evidence_rejected",
    "conflicting_weak_memory",
}

RISK_SUPPRESSION_MARKERS = (
    "weak",
    "watchlist",
    "block",
    "cap",
    "probe",
    "scale_down",
    "reduce",
    "reject",
    "conflict",
)

ALPHA_RELEASE_MARKERS = (
    "protect",
    "allow",
    "deployable",
    "recovering",
)


def _as_mapping_list(value: Any) -> List[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def learning_tags_from_context(
    reasons: Iterable[Any] | None,
    diagnostics: Mapping[str, Any] | None,
) -> List[str]:
    tags: set[str] = set()
    for reason in reasons or []:
        text = str(reason)
        if text.startswith("adaptive_policy_"):
            tags.add("adaptive_policy")
        elif text.startswith("strategy_memory_"):
            tags.add("strategy_memory")
        elif text.startswith("provisional_policy_"):
            tags.add("provisional_policy")
        elif text in CAPITAL_LEARNING_REASONS:
            tags.add("capital_learning")
        elif text in EVIDENCE_REJECTION_REASONS:
            tags.add("strategy_memory")

    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
    if diagnostics.get("adaptive_policy_applied"):
        tags.add("adaptive_policy")
    if diagnostics.get("provisional_policy_applied"):
        tags.add("provisional_policy")
    if diagnostics.get("strategy_memory_rule"):
        tags.add("strategy_memory")
    return sorted(tags)


def learning_effects_from_context(
    reasons: Iterable[Any] | None,
    diagnostics: Mapping[str, Any] | None,
) -> List[str]:
    """Classify whether learning released alpha risk or suppressed weak risk.

    The key distinction is intentional: a learned trade can lose money while the
    learning system still helped if the intervention was a cap/probe/block that
    reduced exposure. Reports should therefore not mix release and suppression
    outcomes into one opaque learned bucket.
    """

    effects: set[str] = set()
    reason_texts = [str(reason) for reason in reasons or [] if reason]

    for text in reason_texts:
        lower = text.lower()
        if text in CAPITAL_LEARNING_REASONS:
            effects.add("alpha_release")
        if text in EVIDENCE_REJECTION_REASONS or "evidence_rejected" in lower:
            effects.add("evidence_rejection")
            effects.add("risk_suppression")
        if text.startswith(("strategy_memory_", "adaptive_policy_", "provisional_policy_")):
            if any(marker in lower for marker in RISK_SUPPRESSION_MARKERS):
                effects.add("risk_suppression")
            elif any(marker in lower for marker in ALPHA_RELEASE_MARKERS):
                effects.add("alpha_release")

    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}

    strategy_rule = diagnostics.get("strategy_memory_rule")
    if isinstance(strategy_rule, Mapping):
        memory_state = str(strategy_rule.get("memory_state") or "").lower()
        if memory_state in {"watchlist", "weak_block"}:
            effects.add("risk_suppression")
        elif memory_state in {"protected", "deployable", "recovering"} and "alpha_release" in effects:
            # Protected reads only count as release when another control reason
            # shows the memory actually changed capacity or execution.
            effects.add("alpha_release")

    for key in ("adaptive_policy_applied", "provisional_policy_applied"):
        for row in _as_mapping_list(diagnostics.get(key)):
            action = str(row.get("policy_action") or row.get("action") or row.get("decision") or "").lower()
            if action in {"protect", "allow"}:
                effects.add("alpha_release")
            elif action in {"block", "cap", "probe", "probe_only", "scale_down", "reduce", "reduce_only"}:
                effects.add("risk_suppression")

    return sorted(effects)


def _effect_trade_summary(pairs: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    summary = summarize_trade_pairs([dict(pair) for pair in pairs])
    return {
        "total_trades": int(summary.get("total_trades") or 0),
        "winning_trades": int(summary.get("winning_trades") or 0),
        "losing_trades": int(summary.get("losing_trades") or 0),
        "flat_trades": int(summary.get("flat_trades") or 0),
        "win_rate": float(summary.get("win_rate") or 0.0),
        "net_pnl": float(summary.get("total_pnl") or 0.0),
        "avg_pnl": float(summary.get("avg_pnl") or 0.0),
        "avg_return": float(summary.get("avg_return") or 0.0),
    }


def summarize_pairs_by_learning_effect(pairs: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for pair in pairs or []:
        effects = pair.get("learning_effects") if isinstance(pair, Mapping) else []
        for effect in effects or []:
            grouped[str(effect)].append(pair)
    return {effect: _effect_trade_summary(rows) for effect, rows in sorted(grouped.items())}


def learning_effect_counts(pairs: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for pair in pairs or []:
        if isinstance(pair, Mapping):
            counter.update(str(effect) for effect in pair.get("learning_effects") or [])
    return dict(sorted(counter.items()))
