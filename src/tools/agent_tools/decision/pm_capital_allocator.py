from __future__ import annotations

"""Capital deployment helpers for template-aware futures allocation."""

from typing import Any, Iterable, Mapping

from tools.common.adaptive_policy_safety import adaptive_policy_runtime_decision


HIGH_QUALITY_MEMORY_STATES = {"protected", "deployable"}
WEAK_MEMORY_STATES = {"watchlist", "weak_block"}
SCOPED_DEMOTE_POLICY_TYPES = {
    "learned_vs_unlearned",
    "fast_loss_sentinel",
    "tail_loss_sentinel",
    "loss_template_policy",
}


def _is_specific_policy_scope(row: Mapping[str, Any], policy_type: str) -> bool:
    """Return True only when a learned cap/demote is scoped enough to size money."""
    if policy_type not in SCOPED_DEMOTE_POLICY_TYPES:
        return True
    return all(
        str(row.get(key) or "*") not in {"*", "", "unknown"}
        for key in ("ticker", "side", "setup_type")
    )


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


def adaptive_policy_record(
    rows: Iterable[Mapping[str, Any]] | None,
    actions: set[str],
    *,
    policy_types: set[str] | None = None,
    policy_prefixes: tuple[str, ...] = (),
) -> dict[str, Any]:
    best_row: dict[str, Any] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        runtime_decision = adaptive_policy_runtime_decision(row)
        if not bool(runtime_decision.get("allowed")):
            continue
        if str(row.get("policy_action") or "").lower() in actions:
            policy_type = str(row.get("policy_type") or "").lower()
            if policy_types is not None or policy_prefixes:
                type_match = policy_types is not None and policy_type in policy_types
                prefix_match = bool(policy_prefixes) and any(policy_type.startswith(prefix) for prefix in policy_prefixes)
                if not (type_match or prefix_match):
                    continue
            if not _is_specific_policy_scope(row, policy_type):
                continue
            if policy_type == "learned_vs_unlearned" and str(row.get("ticker") or "*") == "*":
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


def enriched_policy_evidence(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """Expose policy payload performance as row-level evidence for PM sizing gates."""
    if not isinstance(row, Mapping) or not row:
        return {}
    result = dict(row)
    payload = result.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping):
        evidence = {}
    summary = evidence.get("summary")
    if not isinstance(summary, Mapping):
        summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
    for target_key, source_keys in {
        "sample_count": ("sample_count", "total_trades"),
        "win_rate": ("win_rate",),
        "net_pnl": ("net_pnl", "total_pnl"),
        "avg_pnl": ("avg_pnl",),
    }.items():
        if result.get(target_key) not in (None, "", 0, 0.0):
            continue
        for source in source_keys:
            if evidence.get(source) not in (None, ""):
                result[target_key] = evidence.get(source)
                break
            if summary.get(source) not in (None, ""):
                result[target_key] = summary.get(source)
                break
    result["payload"] = payload
    if result.get("policy_type"):
        result["evidence_source"] = "adaptive_policy_state"
    return result


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
    learned_demote_record = adaptive_policy_record(
        adaptive_policy_state,
        {"demote", "cap"},
        policy_types=SCOPED_DEMOTE_POLICY_TYPES,
        policy_prefixes=("learning_mechanism:",),
    )
    adaptive_protect_record = {}
    if not learned_demote_record:
        adaptive_protect_record = adaptive_policy_record(adaptive_policy_state, {"protect", "allow"})
    learned_demote_record = enriched_policy_evidence(learned_demote_record)
    adaptive_protect_record = enriched_policy_evidence(adaptive_protect_record)
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

