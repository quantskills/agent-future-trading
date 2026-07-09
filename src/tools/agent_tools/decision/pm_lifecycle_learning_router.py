"""PM lifecycle learning router.

This tool records which action-value lanes may affect each PM decision port.
It does not create rank, size positions, deploy capital, write DB rows, or sign
final contracts.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from tools.common.final_action_semantics import (
    ACTION_FAMILY_CONDITIONAL_MONITOR,
    ACTION_FAMILY_EXECUTION,
    ACTION_FAMILY_HOLD,
    ACTION_FAMILY_OPEN_ADD_NEW_RISK,
    ACTION_FAMILY_REDUCE_EXIT,
    validate_action_preference_family_consistency,
)

PORT_ALLOWED_LANES = {
    "new_risk": {"open", "add", "scale", "increase"},
    "open_add_new_risk": {"open", "add", "scale", "increase"},
    "position_hold": {"hold"},
    "hold": {"hold"},
    "capital_release": {"reduce", "exit", "close", "risk_exit"},
    "reduce_exit": {"reduce", "exit", "close", "risk_exit"},
    "conditional_monitor": {"conditional_monitor"},
    "wait": set(),
}
PORT_ALLOWED_FAMILIES = {
    "new_risk": {ACTION_FAMILY_OPEN_ADD_NEW_RISK},
    "open_add_new_risk": {ACTION_FAMILY_OPEN_ADD_NEW_RISK},
    "position_hold": {ACTION_FAMILY_HOLD},
    "hold": {ACTION_FAMILY_HOLD},
    "capital_release": {ACTION_FAMILY_REDUCE_EXIT},
    "reduce_exit": {ACTION_FAMILY_REDUCE_EXIT},
    "conditional_monitor": {ACTION_FAMILY_CONDITIONAL_MONITOR},
    "wait": set(),
}


def _clean(value: Any) -> str:
    return str(value or "").strip().lower()


def _lane(row: Mapping[str, Any]) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    for key in ("learning_lane", "action_value_lane", "lane"):
        value = _clean(row.get(key) or payload.get(key))
        if not value:
            continue
        return value
    return ""


def _family(row: Mapping[str, Any]) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    return _clean(row.get("canonical_action_family") or payload.get("canonical_action_family"))


def route_lifecycle_learning(
    *,
    lifecycle_port: str,
    action_values: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Split action-value learning into PM lifecycle decision, trigger/profile, and rejected lanes."""
    port = _clean(lifecycle_port)
    allowed = PORT_ALLOWED_LANES.get(port, set())
    allowed_families = PORT_ALLOWED_FAMILIES.get(port, set())
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    trigger_profile_rows: list[dict[str, Any]] = []
    accepted_indices: list[int] = []
    rejected_indices: list[int] = []
    trigger_profile_indices: list[int] = []
    for index, row in enumerate(action_values or []):
        if not isinstance(row, Mapping):
            continue
        semantic_validation = validate_action_preference_family_consistency(row)
        lane = _lane(row)
        family = _family(row)
        compact = {
            "source_index": index,
            "id": row.get("id"),
            "ticker": row.get("ticker"),
            "side": row.get("side"),
            "canonical_action_family": family,
            "lane": lane,
            "action_preference": row.get("action_preference"),
            "reward_mean": row.get("reward_mean"),
            "sample_count": row.get("sample_count"),
        }
        if not semantic_validation.get("ok"):
            rejected.append({
                **compact,
                "reason": "action_value_family_consistency_error",
                "errors": list(semantic_validation.get("errors") or []),
            })
            rejected_indices.append(index)
            continue
        if family == ACTION_FAMILY_EXECUTION:
            trigger_profile_rows.append({
                **compact,
                "route": "routed_to_trigger_profile",
                "not_rank_learning": True,
            })
            trigger_profile_indices.append(index)
            continue
        if family in allowed_families and lane in allowed:
            accepted.append(compact)
            accepted_indices.append(index)
        else:
            rejected.append({**compact, "reason": "lifecycle_lane_mismatch"})
            rejected_indices.append(index)
    return {
        "tool": "pm_lifecycle_learning_router",
        "pm_lifecycle_action_port": port,
        "accepted_lanes": sorted(allowed),
        "decision_learning_rows": accepted,
        "accepted_learning": accepted,
        "accepted_indices": accepted_indices,
        "decision_learning_indices": accepted_indices,
        "rejected_learning_rows": rejected,
        "rejected_learning": rejected,
        "rejected_indices": rejected_indices,
        "trigger_profile_learning_rows": trigger_profile_rows,
        "trigger_profile_learning": trigger_profile_rows,
        "trigger_profile_indices": trigger_profile_indices,
        "execution_profile_learning": trigger_profile_rows,
        "execution_profile_indices": trigger_profile_indices,
        "not_rank_learning": True,
        "trigger_profile_learning_direct_to_rank": False,
        "execution_profile_learning_direct_to_rank": False,
        "writes_db": False,
        "writes_contract": False,
        "no_llm": True,
    }
