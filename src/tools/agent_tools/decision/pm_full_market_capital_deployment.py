"""PM-owned full-market capital rank and deployment tool.

This module is the only producer of final all-market ``opportunity_rank``.
It does not write database rows and it is not a workflow fallback. The caller
passes the complete PM candidate set for a trading day; the tool ranks new-risk
candidates, consumes portfolio budgets in rank order, and returns mutated
recommendation objects ready for persistence.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from apis.contract_info_cache import FuturesContractInfoCache
from graph.schema import (
    FuturesRecommendation,
    Portfolio,
    RecommendationAction,
    RecommendationSourceType,
    RecommendationStatus,
)
from tools.common.final_action_semantics import (
    CAPITAL_PRIORITY_RANK_MEANING,
    CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION,
    RANK_CAPITAL_LAYER_FIELDS,
    RANK_CAPITAL_SOURCE_FIELDS,
    canonicalize_final_action_contract_for_persistence,
    full_market_rank_source_payload,
    is_full_market_rank_source,
    rank_capital_layer_contract_complete,
)

RANK_CAPITAL_ROLE_EXPLORATION = "best_exploration_probe_candidate"
RANK_CAPITAL_ROLE_REAL_BUDGET = "best_real_budget_candidate"
RANK_CAPITAL_ROLE_ALPHA_SCALE = "best_alpha_scale_candidate"
CAPITAL_LAYER_EXPLORATION = "exploration_probe"
CAPITAL_LAYER_REAL_BUDGET = "real_budget_entry"
CAPITAL_LAYER_ALPHA_SCALE = "alpha_scale_entry"
CAPITAL_RATIO_SOURCE_EXPLORATION = "probe_margin_ratio_0.008"
CAPITAL_RATIO_SOURCE_REAL_BUDGET = "normal_trade_margin_ratio"
CAPITAL_RATIO_SOURCE_ALPHA_SCALE = "strong_opportunity_target_margin_ratio"
RANK_TRACE_FIELDS = {"rank_input_components"}


def _clean_key(value: Any) -> str:
    return str(value or "").strip().lower()


def rank_metadata_for_row(row: Dict[str, Any]) -> dict[str, str]:
    """Return final full-market rank metadata for a scorecard row."""
    layer = _capital_layer_for_ranked_row(row)
    return {
        "rank_capital_role": _rank_capital_role_for_layer(layer),
        "capital_layer": layer,
        "capital_ratio_source": _capital_ratio_source_for_layer(layer),
        "rank_reason": _rank_reason_for_layer(row, layer),
    }


def rank_trace_for_row(row: Dict[str, Any]) -> dict[str, Any]:
    """Return final full-market rank trace fields for a scorecard row."""
    return {
        "rank_input_components": _rank_input_components_for_row(row),
        "lifecycle_learning_trace": _lifecycle_learning_trace_for_row(row),
        "learning_impact_delta": _learning_impact_delta_for_row(row),
    }


def _safe_positive_ratio(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _bounded(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value or 0.0)))


def _candidate_state(row: Dict[str, Any]) -> str:
    return _clean_key(row.get("final_state") or row.get("opportunity_state"))


def _is_alpha_scale_candidate(row: Dict[str, Any]) -> bool:
    return any(
        bool(row.get(field))
        for field in (
            "alpha_scale_candidate",
            "mature_alpha_candidate",
            "repeated_positive_alpha",
            "strong_opportunity_alpha_scale_candidate",
        )
    )


def _capital_layer_for_ranked_row(row: Dict[str, Any]) -> str:
    if _is_alpha_scale_candidate(row):
        return CAPITAL_LAYER_ALPHA_SCALE
    state = _candidate_state(row)
    if state == "tradeable_candidate":
        return CAPITAL_LAYER_REAL_BUDGET
    if state in {"probe_candidate", "watch_for_trigger"}:
        return CAPITAL_LAYER_EXPLORATION
    if _safe_float(row.get("score"), 0.0) > 0.0 or _safe_float(row.get("opportunity_score"), 0.0) > 0.0:
        return CAPITAL_LAYER_EXPLORATION
    return "not_capital_rank_candidate"


def _rank_capital_role_for_layer(layer: str) -> str:
    value = _clean_key(layer)
    if value == CAPITAL_LAYER_ALPHA_SCALE:
        return RANK_CAPITAL_ROLE_ALPHA_SCALE
    if value == CAPITAL_LAYER_REAL_BUDGET:
        return RANK_CAPITAL_ROLE_REAL_BUDGET
    if value == CAPITAL_LAYER_EXPLORATION:
        return RANK_CAPITAL_ROLE_EXPLORATION
    return "not_capital_rank_candidate"


def _capital_ratio_source_for_layer(layer: str) -> str:
    value = _clean_key(layer)
    if value == CAPITAL_LAYER_ALPHA_SCALE:
        return CAPITAL_RATIO_SOURCE_ALPHA_SCALE
    if value == CAPITAL_LAYER_REAL_BUDGET:
        return CAPITAL_RATIO_SOURCE_REAL_BUDGET
    if value == CAPITAL_LAYER_EXPLORATION:
        return CAPITAL_RATIO_SOURCE_EXPLORATION
    return "not_applicable"


def _rank_reason_for_layer(row: Dict[str, Any], layer: str) -> str:
    value = _clean_key(layer)
    if value == CAPITAL_LAYER_ALPHA_SCALE:
        return "tradeable_candidate_with_repeated_positive_product_setup_trigger_evidence_and_controlled_drawdown"
    if value == CAPITAL_LAYER_REAL_BUDGET:
        return "tradeable_candidate_supported_by_current_evidence_and_product_learning"
    if value == CAPITAL_LAYER_EXPLORATION:
        return "best_watch_for_trigger_by_evidence_trigger_learning_and_risk"
    return f"not_capital_rank_candidate:{_candidate_state(row) or 'unknown'}"


def _rank_score_policy(config: Dict[str, Any] | None) -> Dict[str, Any]:
    cfg = config if isinstance(config, dict) else {}
    policy = cfg.get("rank_score_policy") if isinstance(cfg.get("rank_score_policy"), dict) else {}
    if policy and policy.get("enabled") is False:
        return {}
    return policy


def _policy_section(policy: Dict[str, Any], key: str) -> Dict[str, Any]:
    section = policy.get(key) if isinstance(policy.get(key), dict) else {}
    return section


def _policy_float(section: Dict[str, Any], key: str, default: float) -> float:
    return _safe_float(section.get(key), default)


def _capital_priority_tier_for_state(state: Any) -> int:
    return {
        "tradeable_candidate": 3,
        "probe_candidate": 2,
        "watch_for_trigger": 1,
        "no_opportunity": 0,
    }.get(_clean_key(state), 0)


def _rank_learning_delta(action_value_learning: Dict[str, Any], *, policy: Dict[str, Any]) -> float:
    rank_section = _policy_section(policy, "rank_score")
    action_section = _policy_section(rank_section, "open_add_action_value_delta")
    positive = _safe_float(action_value_learning.get("positive_signal"), 0.0)
    negative = _safe_float(action_value_learning.get("negative_signal"), 0.0)
    tail_loss = _safe_float(action_value_learning.get("recent_tail_loss_signal"), 0.0)
    entry_loss = _safe_float(action_value_learning.get("entry_quality_loss_signal"), 0.0)
    trigger_positive = _safe_float(action_value_learning.get("trigger_quality_positive_signal"), 0.0)
    trigger_loss = _safe_float(action_value_learning.get("net_trigger_quality_loss_signal"), 0.0)
    max_abs_delta = max(0.0, _policy_float(action_section, "max_abs_delta", 0.35))
    return max(
        -max_abs_delta,
        min(
            max_abs_delta,
            _policy_float(action_section, "positive_signal_weight", 0.18) * positive
            + _policy_float(action_section, "trigger_quality_positive_signal_weight", 0.08) * trigger_positive
            + _policy_float(action_section, "negative_signal_weight", -0.18) * negative
            + _policy_float(action_section, "recent_tail_loss_signal_weight", -0.14) * tail_loss
            + _policy_float(action_section, "entry_quality_loss_signal_weight", -0.16) * entry_loss
            + _policy_float(action_section, "net_trigger_quality_loss_signal_weight", -0.10) * trigger_loss,
        ),
    )


def _rank_score_components_for_row(row: Dict[str, Any], *, config: Dict[str, Any]) -> Dict[str, float]:
    policy = _rank_score_policy(config)
    rank_section = _policy_section(policy, "rank_score")
    score_components = row.get("opportunity_score_components") if isinstance(row.get("opportunity_score_components"), dict) else {}
    action_value_learning = row.get("action_value_learning_summary") if isinstance(row.get("action_value_learning_summary"), dict) else {}
    state = _candidate_state(row)
    tier_bonus_cfg = _policy_section(rank_section, "capital_layer_priority_bonus")
    tier_bonus = {
        "tradeable_candidate": 0.18,
        "probe_candidate": 0.10,
        "watch_for_trigger": 0.02,
        "no_opportunity": 0.0,
    }
    if tier_bonus_cfg:
        tier_bonus = {key: _safe_float(tier_bonus_cfg.get(key), value) for key, value in tier_bonus.items()}
    cold_start_evidence = (
        _policy_float(rank_section, "cold_start_evidence_weight", 0.52)
        * _safe_float(row.get("opportunity_score", row.get("score")), 0.0)
    )
    trigger_section = _policy_section(rank_section, "trigger_execution_quality")
    trigger_execution_quality = (
        _policy_float(trigger_section, "execution_profile_learning_weight", 1.0)
        * _safe_float(score_components.get("execution_profile_learning"), 0.0)
        + _policy_float(trigger_section, "trigger_quality_positive_bonus_weight", 1.0)
        * _safe_float(score_components.get("trigger_quality_positive_bonus"), 0.0)
        + _policy_float(trigger_section, "trigger_quality_loss_penalty_weight", 1.0)
        * _safe_float(score_components.get("trigger_quality_loss_penalty"), 0.0)
    )
    history_section = _policy_section(rank_section, "product_setup_trigger_history")
    product_setup_trigger_history = (
        _policy_float(history_section, "alpha_profile_adjustment_weight", 1.0)
        * _safe_float(score_components.get("alpha_profile_adjustment"), 0.0)
    )
    conflict_section = _policy_section(rank_section, "conflict_risk_invalidation_penalty")
    gating_failures = row.get("gating_failures") if isinstance(row.get("gating_failures"), list) else []
    conflict_and_risk_penalty = (
        _policy_float(conflict_section, "fusion_conflict_adjustment_weight", 1.0)
        * abs(min(0.0, _safe_float(score_components.get("fusion_conflict_adjustment"), 0.0)))
        + _policy_float(conflict_section, "market_conflict_penalty_weight", 1.0)
        * abs(min(0.0, _safe_float(score_components.get("market_conflict_penalty"), 0.0)))
        + _policy_float(conflict_section, "critical_data_gap_penalty_weight", 1.0)
        * abs(min(0.0, _safe_float(score_components.get("critical_data_gap_penalty"), 0.0)))
        + _policy_float(conflict_section, "fundamental_gap_penalty_weight", 1.0)
        * abs(min(0.0, _safe_float(score_components.get("fundamental_gap_penalty"), 0.0)))
        + min(
            _policy_float(conflict_section, "gating_failure_penalty_cap", 0.16),
            _policy_float(conflict_section, "gating_failure_penalty_per_item", 0.025)
            * len([item for item in gating_failures if str(item or "").strip()]),
        )
    )
    return {
        "cold_start_evidence_quality": round(cold_start_evidence, 6),
        "capital_layer_priority": round(tier_bonus.get(state, 0.0), 6),
        "open_add_action_value_delta": round(_rank_learning_delta(action_value_learning, policy=policy), 6),
        "product_setup_trigger_history": round(product_setup_trigger_history, 6),
        "trigger_execution_quality": round(trigger_execution_quality, 6),
        "capital_efficiency": 0.0,
        "conflict_risk_invalidation_penalty": round(-conflict_and_risk_penalty, 6),
    }


def _ensure_final_rank_score_fields(row: Dict[str, Any], *, config: Dict[str, Any]) -> Dict[str, Any]:
    components = _rank_score_components_for_row(row, config=config)
    rank_score = round(_bounded(sum(float(value or 0.0) for value in components.values())), 6)
    row["rank_score_components"] = components
    row["rank_score"] = rank_score
    row["capital_priority_score"] = rank_score
    row["capital_priority_tier"] = _capital_priority_tier_for_state(row.get("final_state") or row.get("opportunity_state"))
    return row


def _rank_input_components_for_row(row: Dict[str, Any]) -> dict[str, Any]:
    components = row.get("opportunity_score_components") if isinstance(row.get("opportunity_score_components"), dict) else {}
    rank_score_components = row.get("rank_score_components") if isinstance(row.get("rank_score_components"), dict) else {}
    return {
        "final_state": str(row.get("final_state") or row.get("opportunity_state") or ""),
        "capital_priority_tier": _safe_int(row.get("capital_priority_tier"), 0),
        "rank_score": round(_safe_float(row.get("rank_score"), 0.0), 6),
        "rank_score_components": {
            str(key): round(_safe_float(value), 6)
            for key, value in rank_score_components.items()
        },
        "capital_priority_score": round(_safe_float(row.get("capital_priority_score"), 0.0), 6),
        "watch_priority_score": round(_safe_float(row.get("watch_priority_score"), 0.0), 6),
        "opportunity_score": round(_safe_float(row.get("opportunity_score", row.get("score")), 0.0), 6),
        "evidence_quality_score": round(_safe_float(row.get("evidence_quality_score"), 0.0), 6),
        "setup_quality_score": round(_safe_float(row.get("setup_quality_score", row.get("max_setup_quality")), 0.0), 6),
        "trigger_quality_score": round(_safe_float(row.get("trigger_quality_score"), 0.0), 6),
        "positive_learning_component": round(_safe_float(components.get("positive_learning"), 0.0), 6),
        "negative_learning_component": round(_safe_float(components.get("negative_learning"), 0.0), 6),
        "entry_quality_loss_component": round(_safe_float(components.get("entry_quality_loss_penalty"), 0.0), 6),
        "trigger_quality_positive_component": round(_safe_float(components.get("trigger_quality_positive_bonus"), 0.0), 6),
        "trigger_quality_loss_component": round(_safe_float(components.get("trigger_quality_loss_penalty"), 0.0), 6),
        "execution_profile_learning_component": round(_safe_float(components.get("execution_profile_learning"), 0.0), 6),
    }


def _lifecycle_learning_trace_for_row(row: Dict[str, Any]) -> dict[str, Any]:
    summary = row.get("action_value_learning_summary") if isinstance(row.get("action_value_learning_summary"), dict) else {}
    return {
        "rank_lifecycle": "open_add_new_risk",
        "allowed_learning_lanes": ["open", "add", "scale", "increase"],
        "blocked_learning_lanes": ["hold", "reduce", "exit", "conditional_monitor"],
        "trigger_profile_learning_lanes": ["execution"],
        "used_lanes": list(summary.get("used_lanes") or []),
        "ignored_lanes": list(summary.get("ignored_lanes") or []),
        "positive_count": _safe_int(summary.get("positive_count"), 0),
        "negative_count": _safe_int(summary.get("negative_count"), 0),
        "exact_real_count": _safe_int(summary.get("exact_real_count"), 0),
        "episode_count": _safe_int(summary.get("episode_count"), 0),
        "execution_profile_signal_direct_to_rank": bool(summary.get("execution_profile_signal_direct_to_rank")),
        "strongest_positive": dict(summary.get("strongest_positive") or {}),
        "strongest_negative": dict(summary.get("strongest_negative") or {}),
    }


def _learning_impact_delta_for_row(row: Dict[str, Any]) -> dict[str, Any]:
    components = row.get("opportunity_score_components") if isinstance(row.get("opportunity_score_components"), dict) else {}
    direct_terms = {
        "positive_learning": _safe_float(components.get("positive_learning"), 0.0),
        "negative_learning": _safe_float(components.get("negative_learning"), 0.0),
        "entry_quality_loss_penalty": _safe_float(components.get("entry_quality_loss_penalty"), 0.0),
        "trigger_quality_positive_bonus": _safe_float(components.get("trigger_quality_positive_bonus"), 0.0),
        "trigger_quality_loss_penalty": _safe_float(components.get("trigger_quality_loss_penalty"), 0.0),
    }
    rank_score_components = row.get("rank_score_components") if isinstance(row.get("rank_score_components"), dict) else {}
    return {
        **{key: round(value, 6) for key, value in direct_terms.items()},
        "net_rank_learning_delta": round(sum(direct_terms.values()), 6),
        "rank_score": round(_safe_float(row.get("rank_score"), 0.0), 6),
        "rank_score_open_add_learning_delta": round(
            _safe_float(rank_score_components.get("open_add_action_value_delta"), 0.0),
            6,
        ),
        "execution_profile_learning_direct_to_rank": False,
        "execution_profile_learning_observed": round(_safe_float(components.get("execution_profile_learning"), 0.0), 6),
    }


def _scorecard_preferred_row(snapshot: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    scorecard = snapshot.get("opportunity_scorecard") if isinstance(snapshot.get("opportunity_scorecard"), dict) else {}
    preferred_side = str(scorecard.get("preferred_side") or "").lower()
    if preferred_side in {"long", "short"} and isinstance(scorecard.get(preferred_side), dict):
        return preferred_side, scorecard[preferred_side]
    best_side = ""
    best_row: Dict[str, Any] = {}
    best_score = -1.0
    for side in ("long", "short"):
        row = scorecard.get(side)
        if not isinstance(row, dict):
            continue
        try:
            score = float(row.get("opportunity_score", row.get("score", -1.0)) or -1.0)
        except (TypeError, ValueError):
            score = -1.0
        if score > best_score:
            best_side = side
            best_row = row
            best_score = score
    return best_side, best_row


def _rank_metadata_from_snapshot(snapshot: Dict[str, Any], side: str = "") -> Dict[str, str]:
    scorecard = snapshot.get("opportunity_scorecard") if isinstance(snapshot.get("opportunity_scorecard"), dict) else {}
    row = scorecard.get(side) if side in {"long", "short"} and isinstance(scorecard.get(side), dict) else {}
    if not row:
        _, row = _scorecard_preferred_row(snapshot)
    if row:
        metadata = rank_metadata_for_row(row)
        if all(metadata.get(field) not in (None, "") for field in RANK_CAPITAL_LAYER_FIELDS):
            metadata.update(full_market_rank_source_payload())
            return metadata
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    evidence = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
    deployment = contract.get("capital_deployment") if isinstance(contract.get("capital_deployment"), dict) else {}
    for source in (evidence, deployment):
        if not is_full_market_rank_source(source):
            continue
        recovered = {
            field: source.get(field)
            for field in RANK_CAPITAL_LAYER_FIELDS
            if source.get(field) not in (None, "")
        }
        if all(field in recovered for field in RANK_CAPITAL_LAYER_FIELDS):
            recovered.update(full_market_rank_source_payload())
            return recovered
    return {}


def _rank_trace_from_snapshot(snapshot: Dict[str, Any], side: str = "") -> Dict[str, Any]:
    scorecard = snapshot.get("opportunity_scorecard") if isinstance(snapshot.get("opportunity_scorecard"), dict) else {}
    row = scorecard.get(side) if side in {"long", "short"} and isinstance(scorecard.get(side), dict) else {}
    if not row:
        _, row = _scorecard_preferred_row(snapshot)
    if row:
        return rank_trace_for_row(row)
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    evidence = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
    deployment = contract.get("capital_deployment") if isinstance(contract.get("capital_deployment"), dict) else {}
    for source in (deployment, evidence):
        trace = {
            "rank_input_components": source.get("rank_input_components"),
            "lifecycle_learning_trace": source.get("lifecycle_learning_trace"),
            "learning_impact_delta": source.get("learning_impact_delta"),
        }
        if all(isinstance(value, dict) for value in trace.values()):
            return trace
    return {}


def _canonicalize_snapshot_final_contract(snapshot: Dict[str, Any], *, side: str = "", rank: int | None = None) -> bool:
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    if not contract:
        return False
    canonical = canonicalize_final_action_contract_for_persistence(
        contract,
        rank_metadata=_rank_metadata_from_snapshot(snapshot, side),
        opportunity_rank=rank,
    )
    changed = canonical != contract
    snapshot["final_action_contract"] = canonical
    return changed


def _set_daily_opportunity_rank(snapshot: Dict[str, Any], side: str, rank: int) -> None:
    scorecard = snapshot.get("opportunity_scorecard") if isinstance(snapshot.get("opportunity_scorecard"), dict) else {}
    row = scorecard.get(side) if side in {"long", "short"} and isinstance(scorecard.get(side), dict) else {}
    rank_metadata = rank_metadata_for_row(row) if row else {}
    rank_trace = rank_trace_for_row(row) if row else {}
    rank_metadata.update(full_market_rank_source_payload())
    if row:
        row["opportunity_rank"] = rank
        row.update(rank_metadata)
        row.update(rank_trace)
        row["rank_semantics_version"] = CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION
        row["opportunity_rank_meaning"] = CAPITAL_PRIORITY_RANK_MEANING
        row["rank_is_capital_priority"] = True
        row["rank_is_not_trade_authority"] = True
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    evidence_used = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
    if isinstance(evidence_used, dict):
        evidence_used["opportunity_rank"] = rank
        evidence_used.update(rank_metadata)
        evidence_used.update(rank_trace)
        evidence_used["rank_semantics_version"] = CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION
        evidence_used["opportunity_rank_meaning"] = CAPITAL_PRIORITY_RANK_MEANING
        evidence_used["rank_is_capital_priority"] = True
        evidence_used["rank_is_not_trade_authority"] = True
    active_audit = snapshot.get("active_opportunity_audit") if isinstance(snapshot.get("active_opportunity_audit"), dict) else {}
    active_opportunity = active_audit.get("opportunity") if isinstance(active_audit.get("opportunity"), dict) else {}
    if isinstance(active_opportunity, dict):
        active_opportunity["opportunity_rank"] = rank
        active_opportunity.update(rank_metadata)
        active_opportunity.update(rank_trace)
        active_opportunity["rank_semantics_version"] = CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION
        active_opportunity["opportunity_rank_meaning"] = CAPITAL_PRIORITY_RANK_MEANING
        active_opportunity["rank_is_capital_priority"] = True
        active_opportunity["rank_is_not_trade_authority"] = True
    consistency = snapshot.get("pm_landing_consistency_audit") if isinstance(snapshot.get("pm_landing_consistency_audit"), dict) else {}
    alignment = consistency.get("opportunity_scorecard_alignment") if isinstance(consistency.get("opportunity_scorecard_alignment"), dict) else {}
    if isinstance(alignment, dict):
        alignment["opportunity_rank"] = rank
        alignment.update(rank_metadata)
        alignment.update(rank_trace)


def _clear_non_full_market_rank_fields(snapshot: Dict[str, Any]) -> None:
    """PM-side cleanup of stale local rank fields before final full-market rank."""
    rank_fields = (
        set(RANK_CAPITAL_LAYER_FIELDS)
        | set(RANK_CAPITAL_SOURCE_FIELDS)
        | set(RANK_TRACE_FIELDS)
        | {
            "opportunity_rank",
            "rank_semantics_version",
            "opportunity_rank_meaning",
            "rank_is_capital_priority",
            "rank_is_not_trade_authority",
        }
    )

    def clear_mapping(mapping: Dict[str, Any]) -> None:
        if not isinstance(mapping, dict):
            return
        if is_full_market_rank_source(mapping):
            return
        for field in rank_fields:
            mapping.pop(field, None)

    scorecard = snapshot.get("opportunity_scorecard") if isinstance(snapshot.get("opportunity_scorecard"), dict) else {}
    for side in ("long", "short"):
        row = scorecard.get(side)
        if isinstance(row, dict):
            clear_mapping(row)
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    if contract:
        for field in rank_fields:
            contract.pop(field, None)
        clear_mapping(contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {})
        clear_mapping(contract.get("capital_deployment") if isinstance(contract.get("capital_deployment"), dict) else {})
    active = snapshot.get("active_opportunity_audit") if isinstance(snapshot.get("active_opportunity_audit"), dict) else {}
    opportunity = active.get("opportunity") if isinstance(active.get("opportunity"), dict) else {}
    clear_mapping(opportunity)
    consistency = snapshot.get("pm_landing_consistency_audit") if isinstance(snapshot.get("pm_landing_consistency_audit"), dict) else {}
    alignment = consistency.get("opportunity_scorecard_alignment") if isinstance(consistency.get("opportunity_scorecard_alignment"), dict) else {}
    clear_mapping(alignment)


def _contract_target_lots(snapshot: Dict[str, Any]) -> int:
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    try:
        return int(contract.get("target_lots") or 0)
    except (TypeError, ValueError):
        return 0


def _contract_current_lots(snapshot: Dict[str, Any]) -> int:
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    try:
        return int(contract.get("current_lots") or 0)
    except (TypeError, ValueError):
        return 0


def contract_is_new_or_increasing_risk(snapshot: Dict[str, Any]) -> bool:
    current_lots = _contract_current_lots(snapshot)
    target_lots = _contract_target_lots(snapshot)
    if target_lots == 0 or target_lots == current_lots:
        return False
    if current_lots == 0:
        return True
    if (current_lots > 0 and target_lots > current_lots) or (current_lots < 0 and target_lots < current_lots):
        return True
    if (current_lots > 0 and target_lots < 0) or (current_lots < 0 and target_lots > 0):
        return True
    return False


def _lots_action_from_target(current_lots: int, target_lots: int) -> Tuple[RecommendationAction, int]:
    current = int(current_lots)
    target = int(target_lots)
    if target == current:
        return RecommendationAction.HOLD, 0
    if current == 0:
        return (
            (RecommendationAction.OPEN_LONG, abs(target))
            if target > 0
            else (RecommendationAction.OPEN_SHORT, abs(target))
        )
    if current > 0:
        if target >= 0:
            return (
                (RecommendationAction.OPEN_LONG, target - current)
                if target > current
                else (RecommendationAction.CLOSE_LONG, current - target)
            )
        return RecommendationAction.CLOSE_LONG, current
    if target <= 0:
        return (
            (RecommendationAction.OPEN_SHORT, abs(target) - abs(current))
            if abs(target) > abs(current)
            else (RecommendationAction.CLOSE_SHORT, abs(current) - abs(target))
        )
    return RecommendationAction.CLOSE_SHORT, abs(current)


def _land_pm_deployment_decision_in_snapshot(
    snapshot: Dict[str, Any],
    *,
    target_lots: int,
    reason: str,
    selected: bool,
    rank: int | None,
    deployment_extra: Dict[str, Any] | None = None,
) -> None:
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    if not contract:
        return
    current_lots = _contract_current_lots(snapshot)
    original_target = int(contract.get("target_lots") or 0)
    original_final_action = str(contract.get("final_action") or "").strip()
    target_lots = int(target_lots)
    lots_delta = target_lots - current_lots
    contract["target_lots"] = target_lots
    contract["lots_delta"] = lots_delta
    contract["lots_delta_abs"] = abs(lots_delta)
    if selected and target_lots == original_target and original_final_action:
        contract["final_action"] = original_final_action
    elif target_lots == current_lots:
        contract["final_action"] = "hold" if current_lots else "wait"
    elif current_lots == 0:
        contract["final_action"] = "open_probe"
    elif target_lots == 0:
        contract["final_action"] = "exit"
    elif (current_lots > 0 and target_lots > 0) or (current_lots < 0 and target_lots < 0):
        contract["final_action"] = "scale" if abs(target_lots) > abs(current_lots) else "reduce"
    else:
        contract["final_action"] = "exit"
    reason_codes = contract.get("reason_codes") if isinstance(contract.get("reason_codes"), list) else []
    reason_set = {str(item) for item in reason_codes if item}
    reason_set.add("pm_full_market_capital_deployment")
    if not selected and original_target != target_lots:
        reason_set.add("capital_queue_not_selected")
        reason_set.add("no_rank_or_budget_no_new_exposure")
    contract["reason_codes"] = sorted(reason_set)
    rank_metadata = _rank_metadata_from_snapshot(snapshot) if rank is not None else {}
    rank_trace = _rank_trace_from_snapshot(snapshot) if rank is not None else {}
    evidence = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
    evidence["capital_allocation_reason"] = reason
    if rank is not None:
        evidence["opportunity_rank"] = rank
        evidence.update(rank_metadata)
        evidence.update(rank_trace)
        evidence["rank_semantics_version"] = CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION
        evidence["opportunity_rank_meaning"] = CAPITAL_PRIORITY_RANK_MEANING
        evidence["rank_is_capital_priority"] = True
        evidence["rank_is_not_trade_authority"] = True
    contract["evidence_used"] = evidence
    deployment = {
        "selected_for_capital_deployment": bool(selected),
        "capital_allocation_reason": reason,
        "original_target_lots": int(original_target),
        "deployed_target_lots": int(target_lots),
        "deployed_lots_delta": int(lots_delta),
        "not_second_contract": True,
        "pm_remains_single_fund_manager": True,
    }
    if rank is not None:
        deployment.update(
            {
                "opportunity_rank": rank,
                **rank_metadata,
                **rank_trace,
                "rank_semantics_version": CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION,
                "opportunity_rank_meaning": CAPITAL_PRIORITY_RANK_MEANING,
                "rank_is_capital_priority": True,
                "rank_is_not_trade_authority": True,
            }
        )
    if isinstance(deployment_extra, dict):
        deployment.update(deployment_extra)
    contract["capital_deployment"] = deployment
    snapshot["final_action_contract"] = contract
    rebalance = snapshot.get("rebalance_summary") if isinstance(snapshot.get("rebalance_summary"), dict) else {}
    if rebalance:
        rebalance["target_lots"] = int(target_lots)
        rebalance["lots_delta"] = int(lots_delta)
        rebalance["capital_allocation_reason"] = reason
        rebalance["capital_deployment"] = deployment
    active = snapshot.get("active_opportunity_audit") if isinstance(snapshot.get("active_opportunity_audit"), dict) else {}
    opportunity = active.get("opportunity") if isinstance(active.get("opportunity"), dict) else {}
    if isinstance(opportunity, dict):
        opportunity["capital_allocation_reason"] = reason
        opportunity["selected_for_capital_deployment"] = bool(selected)
        if rank is not None:
            opportunity["opportunity_rank"] = rank
            opportunity.update(rank_metadata)
            opportunity.update(rank_trace)
            opportunity["rank_semantics_version"] = CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION
            opportunity["opportunity_rank_meaning"] = CAPITAL_PRIORITY_RANK_MEANING
            opportunity["rank_is_capital_priority"] = True
            opportunity["rank_is_not_trade_authority"] = True
    _canonicalize_snapshot_final_contract(snapshot, rank=rank)


def _daily_capital_deployment_config(config: Dict[str, Any]) -> Dict[str, float]:
    budget = config.get("position_budget_policy", {}) or {}
    capital = config.get("capital_utilization_control", {}) or {}
    net_control = config.get("net_exposure_control", {}) or {}
    rank_policy = config.get("rank_score_policy") if isinstance(config.get("rank_score_policy"), dict) else {}
    efficiency_policy = (
        rank_policy.get("capital_efficiency")
        if isinstance(rank_policy.get("capital_efficiency"), dict)
        else {}
    )
    hard_max = _safe_positive_ratio(config.get("max_total_margin_ratio"), 0.20)
    target = _safe_positive_ratio(capital.get("target_margin_ratio_confirmed"), 0.10)
    min_probe = _safe_positive_ratio(budget.get("min_real_trade_margin_ratio"), 0.008)
    max_single = _safe_positive_ratio(budget.get("max_single_ticker_margin_ratio"), 0.13)
    max_net = _safe_positive_ratio(net_control.get("max_net_exposure"), 0.50)
    return {
        "target_margin_ratio": min(target, hard_max),
        "min_probe_margin_ratio": min_probe,
        "max_single_ticker_margin_ratio": min(max_single, hard_max),
        "hard_max_total_margin_ratio": hard_max,
        "max_net_exposure": max_net,
        "capital_efficiency_rank_enabled": bool(efficiency_policy.get("enabled", True)),
        "capital_efficiency_rank_max_bonus": _safe_positive_ratio(efficiency_policy.get("max_bonus"), 0.02),
    }


def _portfolio_margin_ratio(portfolio: Portfolio) -> float:
    equity = float(getattr(portfolio, "account_equity", 0.0) or getattr(portfolio, "cashflow", 0.0) or 0.0)
    if equity <= 0:
        return 0.0
    return max(0.0, float(getattr(portfolio, "margin_used", 0.0) or 0.0) / equity)


def _portfolio_current_net_exposure(portfolio: Portfolio) -> float:
    equity = float(getattr(portfolio, "account_equity", 0.0) or getattr(portfolio, "cashflow", 0.0) or 0.0)
    if equity <= 0:
        return 0.0
    exposure = 0.0
    for position in (getattr(portfolio, "positions", {}) or {}).values():
        try:
            shares = int(getattr(position, "shares", 0) or 0)
            value = abs(float(getattr(position, "value", 0.0) or 0.0))
        except (TypeError, ValueError):
            continue
        if shares == 0 or value <= 0:
            continue
        exposure += (1.0 if shares > 0 else -1.0) * value / equity
    return exposure


def _portfolio_ticker_exposure(portfolio: Portfolio, ticker: str) -> float:
    equity = float(getattr(portfolio, "account_equity", 0.0) or getattr(portfolio, "cashflow", 0.0) or 0.0)
    if equity <= 0:
        return 0.0
    position = (getattr(portfolio, "positions", {}) or {}).get(str(ticker).upper())
    if position is None:
        position = (getattr(portfolio, "positions", {}) or {}).get(str(ticker))
    if position is None:
        return 0.0
    try:
        shares = int(getattr(position, "shares", 0) or 0)
        value = abs(float(getattr(position, "value", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0
    if shares == 0 or value <= 0:
        return 0.0
    return (1.0 if shares > 0 else -1.0) * value / equity


def _contract_target_position_ratio(snapshot: Dict[str, Any]) -> float:
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    try:
        value = float(contract.get("target_position_ratio") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value:
        return value
    target_lots = _contract_target_lots(snapshot)
    if target_lots == 0:
        return 0.0
    try:
        margin_ratio = float(contract.get("target_margin_ratio_estimate") or 0.0)
        margin_rate = float(contract.get("margin_rate") or 0.0)
    except (TypeError, ValueError):
        margin_ratio = 0.0
        margin_rate = 0.0
    if margin_ratio > 0 and margin_rate > 0:
        return (1.0 if target_lots > 0 else -1.0) * margin_ratio / margin_rate
    return 0.0


def _recommended_margin_ratio(recommendation: FuturesRecommendation, portfolio: Portfolio) -> float:
    snapshot = recommendation.signal_snapshot if isinstance(recommendation.signal_snapshot, dict) else {}
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    try:
        estimate = float(contract.get("target_margin_ratio_estimate") or 0.0)
    except (TypeError, ValueError):
        estimate = 0.0
    if estimate > 0:
        return estimate
    base_price = float(getattr(recommendation, "base_price", None) or 0.0)
    target_lots = abs(_contract_target_lots(snapshot))
    if base_price <= 0 or target_lots <= 0:
        return 0.0
    info = FuturesContractInfoCache.get_contract_info(recommendation.underlying_code)
    if not info:
        return 0.0
    side_rate = info.get("margin_rate_long") if _contract_target_lots(snapshot) > 0 else info.get("margin_rate_short")
    margin = base_price * target_lots * float(info.get("contract_multiplier") or 1.0) * float(side_rate or 0.0)
    equity = float(getattr(portfolio, "account_equity", 0.0) or getattr(portfolio, "cashflow", 0.0) or 1.0)
    return margin / max(equity, 1.0)


def _float_field(mapping: Dict[str, Any], field: str, default: float = 0.0) -> float:
    try:
        return float(mapping.get(field, default) if mapping.get(field, default) is not None else default)
    except (TypeError, ValueError):
        return default


def _capital_rank_eligible(snapshot: Dict[str, Any], row: Dict[str, Any]) -> bool:
    state = str(row.get("final_state") or row.get("opportunity_state") or "").strip().lower()
    if state in {"no_opportunity", "wait", "flat_wait", "blocked", "rejected"}:
        return False
    contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
    final_action = str(contract.get("final_action") or "").strip().lower()
    if final_action in {"reduce", "exit", "close", "close_long", "close_short", "risk_exit"}:
        return False
    current_lots = _contract_current_lots(snapshot)
    target_lots = _contract_target_lots(snapshot)
    if target_lots == 0 and current_lots == 0 and not (
        bool(contract.get("conditional_trigger_authority")) or bool(contract.get("requires_intraday_confirmation"))
    ):
        return False
    if not contract_is_new_or_increasing_risk(snapshot) and not (
        bool(contract.get("conditional_trigger_authority")) or bool(contract.get("requires_intraday_confirmation"))
    ):
        return False
    if state in {"tradeable_candidate", "probe_candidate", "watch_for_trigger"}:
        return True
    return _float_field(row, "opportunity_score", _float_field(row, "score", 0.0)) > 0.0


def _capital_rank_sort_tuple(row: Dict[str, Any]) -> Tuple[int, float, float, float]:
    try:
        tier = int(row.get("capital_priority_tier") or 0)
    except (TypeError, ValueError):
        tier = 0
    rank_score = _float_field(row, "rank_score")
    priority = _float_field(row, "capital_priority_score")
    watch_priority = _float_field(row, "watch_priority_score", priority)
    score = _float_field(row, "opportunity_score", _float_field(row, "score"))
    if rank_score <= 0:
        rank_score = priority
    return tier, rank_score, watch_priority, score


def _capital_efficiency_rank_bonus(
    margin_ratio: float,
    *,
    min_probe_ratio: float,
    max_single_ratio: float,
    enabled: bool = True,
    max_bonus: float = 0.02,
) -> float:
    if not enabled or max_single_ratio <= 0 or max_bonus <= 0:
        return 0.0
    candidate_margin = max(float(margin_ratio or 0.0), float(min_probe_ratio or 0.0))
    if candidate_margin <= 0:
        return 0.0
    efficiency = max(0.0, min(1.0, (max_single_ratio - candidate_margin) / max_single_ratio))
    return round(float(max_bonus or 0.0) * efficiency, 6)


def _apply_capital_efficiency_to_rank_row(
    row: Dict[str, Any],
    *,
    margin_ratio: float,
    min_probe_ratio: float,
    max_single_ratio: float,
    capital_efficiency_rank_enabled: bool = True,
    capital_efficiency_rank_max_bonus: float = 0.02,
) -> float:
    bonus = _capital_efficiency_rank_bonus(
        margin_ratio,
        min_probe_ratio=min_probe_ratio,
        max_single_ratio=max_single_ratio,
        enabled=capital_efficiency_rank_enabled,
        max_bonus=capital_efficiency_rank_max_bonus,
    )
    components = row.get("rank_score_components")
    components = dict(components) if isinstance(components, dict) else {}
    previous_efficiency = _float_field(components, "capital_efficiency")
    components["capital_efficiency"] = round(previous_efficiency + bonus, 6)
    base_score = _float_field(row, "rank_score", _float_field(row, "capital_priority_score", _float_field(row, "opportunity_score")))
    rank_score = round(max(0.0, min(1.0, base_score + bonus)), 6)
    row["rank_score_components"] = components
    row["rank_score"] = rank_score
    row["capital_priority_score"] = rank_score
    return rank_score


def _capital_deployment_complete(contract: Dict[str, Any], rank: int | None) -> bool:
    deployment = contract.get("capital_deployment") if isinstance(contract.get("capital_deployment"), dict) else {}
    if not deployment:
        return False
    if rank is not None and not rank_capital_layer_contract_complete(contract):
        return False
    required = {
        "selected_for_capital_deployment",
        "capital_allocation_reason",
        "original_target_lots",
        "deployed_target_lots",
        "deployed_lots_delta",
    }
    if rank is not None:
        required.update({"rank_capital_role", "capital_layer", "capital_ratio_source", "rank_reason"})
    if not required.issubset(deployment.keys()):
        return False
    if rank is not None and deployment.get("opportunity_rank") in (None, ""):
        return False
    return True


def apply_full_market_capital_deployment(
    *,
    generated: List[Tuple[str, FuturesRecommendation]],
    config: Dict[str, Any],
    portfolio: Portfolio,
) -> Dict[str, Any]:
    """Apply PM full-market rank and capital deployment to PM candidates."""
    deployment_cfg = _daily_capital_deployment_config(config)
    candidates: List[Tuple[int, float, float, float, str, FuturesRecommendation, str, float, float, float]] = []
    updated_recommendations: list[FuturesRecommendation] = []
    updated_object_ids: set[int] = set()

    def mark_updated(recommendation: FuturesRecommendation) -> None:
        marker = id(recommendation)
        if marker not in updated_object_ids:
            updated_object_ids.add(marker)
            updated_recommendations.append(recommendation)

    for _, recommendation in generated:
        if recommendation.status != RecommendationStatus.SKIPPED:
            continue
        source_type = getattr(recommendation.source_type, "value", recommendation.source_type)
        if source_type != RecommendationSourceType.STRATEGY.value:
            continue
        snapshot = recommendation.signal_snapshot if isinstance(recommendation.signal_snapshot, dict) else {}
        _clear_non_full_market_rank_fields(snapshot)
        side, _ = _scorecard_preferred_row(snapshot)
        if _canonicalize_snapshot_final_contract(snapshot, side=side):
            recommendation.signal_snapshot = snapshot
            action, lots = _lots_action_from_target(_contract_current_lots(snapshot), _contract_target_lots(snapshot))
            recommendation.action = action
            recommendation.lots = lots
            mark_updated(recommendation)

    for ticker, recommendation in generated:
        if recommendation.status == RecommendationStatus.SKIPPED:
            continue
        source_type = getattr(recommendation.source_type, "value", recommendation.source_type)
        if source_type != RecommendationSourceType.STRATEGY.value:
            continue
        snapshot = recommendation.signal_snapshot if isinstance(recommendation.signal_snapshot, dict) else {}
        _clear_non_full_market_rank_fields(snapshot)
        side, row = _scorecard_preferred_row(snapshot)
        if side not in {"long", "short"} or not row:
            continue
        _ensure_final_rank_score_fields(row, config=config)
        if not _capital_rank_eligible(snapshot, row):
            continue
        try:
            score = float(row.get("opportunity_score", row.get("score", 0.0)) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        try:
            priority_score = float(row.get("capital_priority_score", score) or score)
        except (TypeError, ValueError):
            priority_score = score
        rank_score = _float_field(row, "rank_score", priority_score)
        recommendation.signal_snapshot = snapshot
        margin_ratio = _recommended_margin_ratio(recommendation, portfolio)
        rank_score = _apply_capital_efficiency_to_rank_row(
            row,
            margin_ratio=margin_ratio,
            min_probe_ratio=deployment_cfg["min_probe_margin_ratio"],
            max_single_ratio=deployment_cfg["max_single_ticker_margin_ratio"],
            capital_efficiency_rank_enabled=bool(deployment_cfg["capital_efficiency_rank_enabled"]),
            capital_efficiency_rank_max_bonus=float(deployment_cfg["capital_efficiency_rank_max_bonus"]),
        )
        priority_score = rank_score
        target_position_ratio = _contract_target_position_ratio(snapshot)
        current_ticker_exposure = _portfolio_ticker_exposure(portfolio, str(ticker).upper())
        tier, sorted_rank_score, _, _ = _capital_rank_sort_tuple(row)
        if sorted_rank_score > 0:
            rank_score = sorted_rank_score
        candidates.append(
            (
                tier,
                rank_score,
                priority_score,
                score,
                str(ticker).upper(),
                recommendation,
                side,
                margin_ratio,
                target_position_ratio,
                current_ticker_exposure,
            )
        )

    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
    used_margin_ratio = _portfolio_margin_ratio(portfolio)
    running_net_exposure = _portfolio_current_net_exposure(portfolio)
    target_margin_ratio = deployment_cfg["target_margin_ratio"]
    min_probe_ratio = deployment_cfg["min_probe_margin_ratio"]
    max_single_ratio = deployment_cfg["max_single_ticker_margin_ratio"]
    hard_max_margin_ratio = deployment_cfg["hard_max_total_margin_ratio"]
    max_net_exposure = deployment_cfg["max_net_exposure"]
    budget_ceiling = min(target_margin_ratio, hard_max_margin_ratio)

    for rank, (_, rank_score, priority_score, score, _, recommendation, side, margin_ratio, target_position_ratio, current_ticker_exposure) in enumerate(candidates, start=1):
        snapshot = recommendation.signal_snapshot if isinstance(recommendation.signal_snapshot, dict) else {}
        _set_daily_opportunity_rank(snapshot, side, rank)
        current_lots = _contract_current_lots(snapshot)
        target_lots = _contract_target_lots(snapshot)
        if not contract_is_new_or_increasing_risk(snapshot):
            _land_pm_deployment_decision_in_snapshot(
                snapshot,
                target_lots=target_lots,
                reason="not_new_or_increasing_risk_preserve_pm_contract",
                selected=True,
                rank=rank,
                deployment_extra={
                    "rank_budget_sequence": rank,
                    "rank_score": round(float(rank_score or 0.0), 6),
                    "budget_check": "not_new_or_increasing_risk",
                },
            )
        else:
            candidate_margin = max(float(margin_ratio or 0.0), min_probe_ratio)
            single_ok = candidate_margin <= max_single_ratio + 1e-12
            total_ok = used_margin_ratio + candidate_margin <= budget_ceiling + 1e-12
            projected_net_exposure = running_net_exposure - float(current_ticker_exposure or 0.0) + float(target_position_ratio or 0.0)
            net_ok = abs(projected_net_exposure) <= max_net_exposure + 1e-12
            budget_detail = {
                "rank_budget_sequence": rank,
                "rank_score": round(float(rank_score or 0.0), 6),
                "candidate_margin_ratio": round(candidate_margin, 6),
                "queue_margin_ratio_before": round(used_margin_ratio, 6),
                "queue_margin_ratio_after_if_selected": round(used_margin_ratio + candidate_margin, 6),
                "target_margin_ratio_budget": round(budget_ceiling, 6),
                "max_single_ticker_margin_ratio": round(max_single_ratio, 6),
                "current_net_exposure_before": round(running_net_exposure, 6),
                "current_ticker_exposure": round(float(current_ticker_exposure or 0.0), 6),
                "target_position_ratio": round(float(target_position_ratio or 0.0), 6),
                "projected_net_exposure_if_selected": round(projected_net_exposure, 6),
                "max_net_exposure": round(max_net_exposure, 6),
                "single_ticker_budget_ok": bool(single_ok),
                "total_margin_budget_ok": bool(total_ok),
                "net_exposure_budget_ok": bool(net_ok),
            }
            if single_ok and total_ok and net_ok:
                used_margin_ratio += candidate_margin
                running_net_exposure = projected_net_exposure
                reason = (
                    "selected_by_full_market_pm_capital_queue:"
                    f"rank={rank};rank_score={rank_score};capital_priority_score={priority_score};score={score};"
                    f"target_margin_used={used_margin_ratio:.4f}/{budget_ceiling:.4f};"
                    f"net_exposure={running_net_exposure:.4f}/{max_net_exposure:.4f}"
                )
                _land_pm_deployment_decision_in_snapshot(
                    snapshot,
                    target_lots=target_lots,
                    reason=reason,
                    selected=True,
                    rank=rank,
                    deployment_extra=budget_detail,
                )
            else:
                blocked = []
                if not single_ok:
                    blocked.append("single_ticker_budget")
                if not total_ok:
                    blocked.append("total_margin_budget")
                if not net_ok:
                    blocked.append("net_exposure_budget")
                reason = (
                    "not_selected_by_full_market_pm_capital_queue:"
                    f"rank={rank};rank_score={rank_score};capital_priority_score={priority_score};"
                    f"blocked_by={','.join(blocked) or 'unknown'};"
                    f"capital_target_filled={used_margin_ratio:.4f}/{budget_ceiling:.4f};"
                    f"net_exposure={running_net_exposure:.4f}/{max_net_exposure:.4f}"
                )
                _land_pm_deployment_decision_in_snapshot(
                    snapshot,
                    target_lots=current_lots,
                    reason=reason,
                    selected=False,
                    rank=rank,
                    deployment_extra=budget_detail,
                )
        recommendation.signal_snapshot = snapshot
        _canonicalize_snapshot_final_contract(snapshot, side=side, rank=rank)
        action, lots = _lots_action_from_target(_contract_current_lots(snapshot), _contract_target_lots(snapshot))
        recommendation.action = action
        recommendation.lots = lots
        mark_updated(recommendation)

    return {
        "tool": "pm_full_market_capital_deployment",
        "updated_recommendations": updated_recommendations,
        "candidate_count": len(candidates),
        "final_margin_ratio": round(used_margin_ratio, 6),
        "final_net_exposure": round(running_net_exposure, 6),
        "writes_db": False,
        "workflow_fallback": False,
    }
