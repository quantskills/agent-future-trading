from __future__ import annotations

"""Capital deployment helpers for template-aware futures allocation."""

from typing import Any, Iterable, Mapping


HIGH_QUALITY_MEMORY_STATES = {"protected", "deployable"}
WEAK_MEMORY_STATES = {"watchlist", "weak_block"}


def _normalize_combo(value: Any) -> tuple[str, ...]:
    if value is None:
        return tuple()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    text = str(value).strip()
    if not text or text == "*":
        return (text,) if text == "*" else tuple()
    if "|" in text:
        return tuple(part.strip() for part in text.split("|"))
    return (text,)


def _record_matches_combo(row: Mapping[str, Any], signal_combo: tuple[str, ...] | None) -> bool:
    if not signal_combo:
        return True
    row_combo = _normalize_combo(row.get("signal_combo"))
    if not row_combo:
        return True
    if row_combo == ("*",):
        return True
    return row_combo == tuple(signal_combo)


def conflicting_weak_memory_record(
    strategy_memory: Mapping[str, Any] | None,
    signal_combo: tuple[str, ...] | None,
) -> dict[str, Any]:
    if not isinstance(strategy_memory, Mapping):
        return {}
    for key in ("combo", "side_memory"):
        row = strategy_memory.get(key)
        if (
            isinstance(row, Mapping)
            and str(row.get("memory_state") or "") in WEAK_MEMORY_STATES
            and _record_matches_combo(row, signal_combo)
        ):
            return dict(row)
    for row in strategy_memory.get("records") or []:
        if (
            isinstance(row, Mapping)
            and str(row.get("memory_state") or "") in WEAK_MEMORY_STATES
            and _record_matches_combo(row, signal_combo)
        ):
            return dict(row)
    return {}


def strategy_memory_record(strategy_memory: Mapping[str, Any] | None, states: set[str]) -> dict[str, Any]:
    if not isinstance(strategy_memory, Mapping):
        return {}
    for key in ("combo", "side_memory"):
        row = strategy_memory.get(key)
        if isinstance(row, Mapping) and str(row.get("memory_state") or "") in states:
            return dict(row)
    for row in strategy_memory.get("records") or []:
        if isinstance(row, Mapping) and str(row.get("memory_state") or "") in states:
            return dict(row)
    return {}


def adaptive_policy_record(rows: Iterable[Mapping[str, Any]] | None, actions: set[str]) -> dict[str, Any]:
    best_row: dict[str, Any] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("policy_action") or "").lower() in actions:
            if (
                str(row.get("policy_type") or "").lower() == "learned_vs_unlearned"
                and str(row.get("ticker") or "*") == "*"
            ):
                # Global learned-vs-unlearned diagnostics are useful for reports,
                # but PM sizing must not treat them as a blanket product/template cap.
                continue
            candidate = dict(row)
            if not best_row:
                best_row = candidate
                continue
            candidate_score = (
                float(candidate.get("sample_count") or 0),
                float(candidate.get("confidence_score") or 0.0),
            )
            best_score = (
                float(best_row.get("sample_count") or 0),
                float(best_row.get("confidence_score") or 0.0),
            )
            if candidate_score > best_score:
                best_row = candidate
    return best_row


def has_adaptive_policy_action(rows: Iterable[Mapping[str, Any]] | None, actions: set[str]) -> bool:
    return bool(adaptive_policy_record(rows, actions))


def high_quality_learning_context(
    *,
    strategy_memory: Mapping[str, Any] | None,
    adaptive_policy_state: Iterable[Mapping[str, Any]] | None,
    allow_memory_protected_scaling: bool,
    allow_recovering_template_scaling: bool = False,
) -> tuple[bool, dict[str, Any]]:
    protected_memory = strategy_memory_record(strategy_memory, HIGH_QUALITY_MEMORY_STATES)
    recovering_memory = strategy_memory_record(strategy_memory, {"recovering"})
    learned_demote_record = adaptive_policy_record(adaptive_policy_state, {"demote", "cap"})
    adaptive_protect_record = {}
    if not learned_demote_record:
        adaptive_protect_record = adaptive_policy_record(adaptive_policy_state, {"protect", "allow"})
    adaptive_protect = bool(adaptive_protect_record)
    high_quality_memory = bool(allow_memory_protected_scaling and protected_memory)
    high_quality_memory = high_quality_memory or bool(allow_recovering_template_scaling and recovering_memory)
    high_quality_memory = high_quality_memory or bool(allow_memory_protected_scaling and adaptive_protect)
    diagnostics = {
        "protected_memory": protected_memory,
        "recovering_memory": recovering_memory,
        "learned_demote_record": learned_demote_record,
        "adaptive_protect": adaptive_protect,
        "adaptive_protect_record": adaptive_protect_record,
    }
    return high_quality_memory, diagnostics
