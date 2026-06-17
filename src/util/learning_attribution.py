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


def _json_text(value: Any) -> str:
    try:
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True).lower()
    except Exception:
        return str(value or "").lower()


def _walk_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child)


def _technical_calibration_applied(snapshot: Mapping[str, Any] | None) -> bool:
    for item in _walk_mappings(snapshot or {}):
        calibration = item.get("technical_parameter_calibration")
        if isinstance(calibration, Mapping):
            applied = calibration.get("applied")
            if isinstance(applied, list) and applied:
                return True
    return False


def _policy_rows_from_diagnostics(diagnostics: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    rows: List[Mapping[str, Any]] = []
    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
    for key in (
        "adaptive_policy_applied",
        "provisional_policy_applied",
        "adaptive_policy_state",
        "policy_state",
        "tail_loss_sentinel",
        "alpha_promotion",
        "loss_template_policy",
        "technical_parameter_calibration",
    ):
        rows.extend(_as_mapping_list(diagnostics.get(key)))
    learning = diagnostics.get("capital_utilization_learning")
    if isinstance(learning, Mapping):
        for key in ("adaptive_protect_record", "tail_loss_record", "protected_memory", "recovering_memory"):
            rows.extend(_as_mapping_list(learning.get(key)))
    return rows


def _policy_like_rows_from_anywhere(value: Any) -> List[Mapping[str, Any]]:
    """Collect policy/memory-looking rows from nested PM/Auditor traces.

    Learning attribution is diagnostic, but it must see the same structured
    evidence that PM and Auditor used. This keeps learned/unlearned and
    learning_mechanism:* policies from missing effects that are present in
    learning_to_position_trace rather than only in Auditor diagnostics.
    """

    rows: List[Mapping[str, Any]] = []
    for item in _walk_mappings(value):
        if any(
            key in item
            for key in (
                "policy_type",
                "policy_action",
                "memory_type",
                "memory_state",
                "position_authority",
                "technical_parameter_calibration",
            )
        ):
            rows.append(item)
    return rows


def _add_mechanisms_from_policy_row(mechanisms: set[str], row: Mapping[str, Any]) -> None:
    policy_type = str(row.get("policy_type") or row.get("source") or row.get("memory_type") or "").lower()
    policy_action = str(row.get("policy_action") or row.get("action") or "").lower()
    memory_state = str(row.get("memory_state") or "").lower()
    position_authority = str(row.get("position_authority") or "").lower()
    combined = " ".join(part for part in (policy_type, policy_action, memory_state, position_authority) if part)
    if "alpha_promotion" in combined:
        mechanisms.add("alpha_promotion")
    if "tail_loss_sentinel" in combined:
        mechanisms.add("tail_loss_sentinel")
    if "loss_template" in combined:
        mechanisms.add("loss_template_policy")
    if "learned_vs_unlearned" in combined:
        mechanisms.add("learned_vs_unlearned")
    if "technical_parameter" in combined:
        mechanisms.add("technical_parameter_calibration")
    if "strategy_memory" in combined or memory_state:
        mechanisms.add("strategy_memory")
        if memory_state in {"protected", "deployable"} or "protected" in combined or "deployable" in combined:
            mechanisms.add("strategy_memory_protected")
        elif memory_state == "recovering" or "recovering" in combined:
            mechanisms.add("strategy_memory_recovering")
        elif memory_state == "weak_block" or "weak_block" in combined:
            mechanisms.add("strategy_memory_weak_block")
        elif memory_state == "watchlist" or "watchlist" in combined:
            mechanisms.add("strategy_memory_watchlist")


def learning_mechanisms_from_context(
    reasons: Iterable[Any] | None,
    diagnostics: Mapping[str, Any] | None,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> List[str]:
    """Return fine-grained learning mechanism labels for evaluation.

    These labels are diagnostic only. They split the broad learned/unlearned
    bucket into concrete mechanisms without changing trading behavior.
    """

    mechanisms: set[str] = set()
    reason_texts = [str(reason).lower() for reason in reasons or [] if reason]
    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
    diagnostic_text = _json_text(diagnostics)

    for text in reason_texts:
        if "alpha_promotion" in text or text in {item.lower() for item in CAPITAL_LEARNING_REASONS}:
            mechanisms.add("alpha_promotion")
        if "tail_loss_sentinel" in text:
            mechanisms.add("tail_loss_sentinel")
        if "loss_template" in text:
            mechanisms.add("loss_template_policy")
        if "learned_underperformance" in text or "learned_vs_unlearned" in text:
            mechanisms.add("learned_vs_unlearned")
        if "strategy_memory" in text:
            mechanisms.add("strategy_memory")
            if "weak_block" in text:
                mechanisms.add("strategy_memory_weak_block")
            elif "watchlist" in text:
                mechanisms.add("strategy_memory_watchlist")
            elif any(marker in text for marker in ("protected", "deployable", "recovering")):
                mechanisms.add("strategy_memory_protected")

    for row in _policy_rows_from_diagnostics(diagnostics):
        _add_mechanisms_from_policy_row(mechanisms, row)
    for row in _policy_like_rows_from_anywhere(diagnostics):
        _add_mechanisms_from_policy_row(mechanisms, row)

    strategy_rule = diagnostics.get("strategy_memory_rule")
    if isinstance(strategy_rule, Mapping):
        mechanisms.add("strategy_memory")
        memory_state = str(strategy_rule.get("memory_state") or "").lower()
        if memory_state in {"protected", "deployable"}:
            mechanisms.add("strategy_memory_protected")
        elif memory_state == "recovering":
            mechanisms.add("strategy_memory_recovering")
        elif memory_state == "weak_block":
            mechanisms.add("strategy_memory_weak_block")
        elif memory_state == "watchlist":
            mechanisms.add("strategy_memory_watchlist")

    if _technical_calibration_applied(snapshot):
        mechanisms.add("technical_parameter_calibration")
    if "technical_parameter_calibration" in diagnostic_text:
        mechanisms.add("technical_parameter_calibration")
    if "loss_template_policy" in diagnostic_text:
        mechanisms.add("loss_template_policy")
    if "alpha_promotion" in diagnostic_text:
        mechanisms.add("alpha_promotion")
    if "tail_loss_sentinel" in diagnostic_text:
        mechanisms.add("tail_loss_sentinel")
    if "learned_vs_unlearned" in diagnostic_text:
        mechanisms.add("learned_vs_unlearned")
    has_strategy_memory_text = "strategy_memory" in diagnostic_text
    if has_strategy_memory_text:
        mechanisms.add("strategy_memory")
        if "weak_block" in diagnostic_text:
            mechanisms.add("strategy_memory_weak_block")
        if "watchlist" in diagnostic_text:
            mechanisms.add("strategy_memory_watchlist")
        if (
            "protected" in diagnostic_text
            or "deployable" in diagnostic_text
        ):
            mechanisms.add("strategy_memory_protected")

    return sorted(mechanisms)


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


def summarize_pairs_by_learning_mechanism(pairs: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for pair in pairs or []:
        if not isinstance(pair, Mapping):
            continue
        for mechanism in pair.get("learning_mechanisms") or []:
            grouped[str(mechanism)].append(pair)
    return {mechanism: _effect_trade_summary(rows) for mechanism, rows in sorted(grouped.items())}


def learning_mechanism_counts(pairs: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for pair in pairs or []:
        if isinstance(pair, Mapping):
            counter.update(str(item) for item in pair.get("learning_mechanisms") or [])
    return dict(sorted(counter.items()))


def learning_effect_counts(pairs: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for pair in pairs or []:
        if isinstance(pair, Mapping):
            counter.update(str(effect) for effect in pair.get("learning_effects") or [])
    return dict(sorted(counter.items()))
