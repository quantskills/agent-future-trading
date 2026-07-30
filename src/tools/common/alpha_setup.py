from __future__ import annotations

"""Alpha setup profile and action-value helpers.

This module turns post-settlement research facts into future-usable setup
profiles. It deliberately avoids product blacklists and future leakage: every
profile is scoped by ticker/side/horizon/regime/setup/data combo, written only
after Phase4, and consumed as a rebuttable prior.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from tools.common.learning_contract import (
    CONTRACT_KEY,
    build_next_round_memory_contract,
)
from tools.common.final_action_semantics import (
    canonical_action_family,
    canonical_action_value_lane,
    contract_final_learning_lifecycle,
    validate_action_preference_family_consistency,
)
from tools.common.execution_trigger_semantics import CANONICAL_ENTRY_TRIGGERS
from tools.common.learning_identity import canonical_market_regime
from tools.agent_tools.research import research_memory_writers


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_until(trading_date: str, days: int) -> str:
    return (
        datetime.strptime(str(trading_date)[:10], "%Y-%m-%d")
        + timedelta(days=max(1, int(days or 1)))
    ).strftime("%Y-%m-%d")


def _clean_token(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    if not text or text in {"none", "null", "nan"}:
        return default
    text = text.replace(" ", "_").replace("/", "_")
    return "".join(ch for ch in text if ch.isalnum() or ch in {"_", "-", "*"})


def _compact_text(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any, default: str = "unknown") -> str:
    for value in values:
        text = _compact_text(value, 180)
        if text and text.lower() not in {"none", "null", "nan", "unknown"}:
            return text
    return default


def build_scope_key(
    *,
    ticker: str,
    side: str,
    horizon_class: str,
    market_regime: str,
    setup_type: str,
    data_combo: str,
) -> str:
    return "|".join(
        [
            _clean_token(ticker, "*").upper(),
            _clean_token(side, "*"),
            _clean_token(horizon_class, "unknown"),
            canonical_market_regime(market_regime, "unknown"),
            _clean_token(setup_type, "unknown"),
            _clean_token(data_combo, "unknown")[:160],
        ]
    )


def _analyst_entry_trigger(evidence: Mapping[str, Any]) -> str:
    analyst_payloads = _mapping_or_empty(evidence.get("analyst_payloads"))
    for analyst in ("technical", "fundamental", "commodity_news"):
        payload = _mapping_or_empty(analyst_payloads.get(analyst))
        trigger = _first_text(payload.get("entry_trigger"), default="")
        if trigger:
            return trigger
    contracts = _mapping_or_empty(evidence.get("analyst_action_evidence_contracts"))
    for contract in contracts.values():
        contract_map = _mapping_or_empty(contract)
        trigger = _first_text(contract_map.get("entry_trigger"), default="")
        if trigger:
            return trigger
    return ""


def _deployment_outcome_from_contract(
    *,
    contract: Mapping[str, Any],
    sample: Mapping[str, Any],
) -> Dict[str, Any]:
    capital_deployment = _mapping_or_empty(contract.get("capital_deployment"))
    evidence_used = _mapping_or_empty(contract.get("evidence_used"))
    current_lots = _safe_int(contract.get("current_lots"), _safe_int(sample.get("current_lots")))
    target_lots = _safe_int(contract.get("target_lots"), _safe_int(sample.get("target_lots")))
    lots_delta = _safe_int(contract.get("lots_delta"), target_lots - current_lots)
    selected = bool(capital_deployment.get("selected_for_capital_deployment"))
    authority_type = _first_text(contract.get("authority_type"), default="unknown")
    final_action = _first_text(contract.get("final_action"), sample.get("pm_action"), default="unknown")
    if selected:
        deployment_tier = "capital_deployed"
    elif authority_type in {"real_budget_entry", "tradeable_candidate"}:
        deployment_tier = "real_budget_entry_candidate"
    elif authority_type in {"exploration_probe", "conditional_trigger_authority"} or final_action in {
        "open_probe",
        "conditional_probe",
        "watch_trigger",
    }:
        deployment_tier = "exploration_or_conditional_probe"
    elif lots_delta:
        deployment_tier = "position_changed_without_capital_queue_selection"
    else:
        deployment_tier = "not_selected_or_no_change"
    return {
        "selected_for_capital_deployment": selected,
        "deployment_tier": deployment_tier,
        "authority_type": authority_type,
        "final_action": final_action,
        "current_lots": current_lots,
        "target_lots": target_lots,
        "lots_delta": lots_delta,
        "opportunity_rank": capital_deployment.get("opportunity_rank"),
        "opportunity_score": evidence_used.get("opportunity_score"),
        "capital_allocation_reason": (
            capital_deployment.get("capital_allocation_reason") or ""
        ),
    }


def _entry_quality_outcome_from_sample(
    *,
    sample: Mapping[str, Any],
    result: Mapping[str, Any],
    action_name: str,
    entry_trigger: str,
    evidence_combo: str,
    deployment: Mapping[str, Any],
) -> Dict[str, Any]:
    """Bind settled entry outcome back to the original setup and trigger."""
    action_lane = _action_value_lane(action_name)
    episode_reward = result.get("episode_net_pnl") if isinstance(result, Mapping) else None
    net_pnl = (
        _safe_float(episode_reward)
        if episode_reward is not None
        else _safe_float(sample.get("net_pnl")) - _safe_float(sample.get("commission"))
    )
    is_entry_action = action_lane == "open"
    deployed = str(deployment.get("deployment_tier") or "").lower() in {
        "capital_deployed",
        "exploration_or_conditional_probe",
        "position_changed_without_capital_queue_selection",
    }
    loss_episode = bool(is_entry_action and deployed and net_pnl < 0)
    tail_loss_episode = bool(loss_episode and net_pnl <= -1000.0)
    positive_entry_episode = bool(is_entry_action and deployed and net_pnl > 0)
    penalty_weight = 0.0
    support_weight = 0.0
    if loss_episode:
        penalty_weight = min(1.0, max(0.10, abs(net_pnl) / 10000.0))
    if tail_loss_episode:
        penalty_weight = min(1.0, max(penalty_weight, 0.55))
    if positive_entry_episode:
        support_weight = min(1.0, max(0.10, net_pnl / 10000.0))
    if tail_loss_episode:
        verdict = "entry_tail_loss_revalidate"
        trigger_verdict = "trigger_tail_loss_revalidate"
        trigger_confirmation_adjustment = "strict_confirmation_required"
    elif loss_episode:
        verdict = "entry_loss_revalidate"
        trigger_verdict = "trigger_loss_revalidate"
        trigger_confirmation_adjustment = "stronger_confirmation_required"
    elif positive_entry_episode:
        verdict = "entry_quality_supported"
        trigger_verdict = "trigger_quality_supported"
        trigger_confirmation_adjustment = "standard_confirmation_supported"
    elif is_entry_action:
        verdict = "entry_outcome_neutral"
        trigger_verdict = "trigger_outcome_neutral"
        trigger_confirmation_adjustment = "neutral"
    else:
        verdict = "not_entry_action"
        trigger_verdict = "not_entry_action"
        trigger_confirmation_adjustment = "not_applicable"
    return {
        "contract_version": "agentquant.entry_quality_outcome.v1",
        "entry_quality_verdict": verdict,
        "entry_action": is_entry_action,
        "deployed": deployed,
        "loss_episode": loss_episode,
        "tail_loss_episode": tail_loss_episode,
        "positive_entry_episode": positive_entry_episode,
        "net_pnl": net_pnl,
        "penalty_weight": round(penalty_weight, 4),
        "support_weight": round(support_weight, 4),
        "trigger_quality_verdict": trigger_verdict,
        "trigger_confirmation_adjustment": trigger_confirmation_adjustment,
        "entry_trigger": entry_trigger,
        "trigger_key": _clean_token(entry_trigger, "unknown_trigger")[:120],
        "evidence_combo": evidence_combo,
        "deployment_tier": deployment.get("deployment_tier"),
        "affects": [
            "entry_quality_score",
            "trigger_quality_score",
            "capital_priority_score",
            "real_budget_entry_qualification",
        ],
        "not_trade_authority": True,
        "future_only": True,
    }


def build_product_learning_performance_key(
    *,
    scope_key: str,
    sample: Mapping[str, Any],
    action_name: str,
    evidence: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build the product-level learning identity used by future ranking."""
    evidence = _mapping_or_empty(evidence)
    result = _mapping_or_empty(result)
    contract = _mapping_or_empty(evidence.get("final_action_contract"))
    execution_feedback = _mapping_or_empty(result.get("execution_feedback"))
    entry_trigger = _first_text(
        contract.get("entry_trigger"),
        execution_feedback.get("reason"),
        _analyst_entry_trigger(evidence),
        sample.get("entry_trigger"),
    )
    trigger_key = _clean_token(entry_trigger, "unknown_trigger")[:120]
    data_combo = _first_text(sample.get("data_combo"), default="data_unknown")
    deployment = _deployment_outcome_from_contract(contract=contract, sample=sample)
    entry_quality_outcome = _entry_quality_outcome_from_sample(
        sample=sample,
        result=result,
        action_name=action_name,
        entry_trigger=entry_trigger,
        evidence_combo=data_combo,
        deployment=deployment,
    )
    ticker = str(sample.get("ticker") or "*").upper()
    side = _clean_token(sample.get("side"), "*")
    setup_type = _clean_token(sample.get("setup_type"), "unknown")
    performance_scope_key = "|".join(
        [
            _clean_token(ticker, "*").upper(),
            side,
            setup_type,
            trigger_key,
            _clean_token(data_combo, "data_unknown")[:120],
            deployment["deployment_tier"],
        ]
    )
    return {
        "contract_version": PRODUCT_LEARNING_PERFORMANCE_KEY_VERSION,
        "scope_key": scope_key,
        "performance_scope_key": performance_scope_key,
        "ticker": ticker,
        "side": side,
        "horizon_class": _clean_token(sample.get("horizon_class"), "unknown"),
        "expected_horizon_days": _safe_int(sample.get("expected_horizon_days")),
        "market_regime": canonical_market_regime(sample.get("market_regime"), "unknown"),
        "setup_type": setup_type,
        "action_name": _clean_token(action_name, "unknown"),
        "entry_trigger": entry_trigger,
        "trigger_key": trigger_key,
        "evidence_combo": data_combo,
        "opportunity_state": _first_text(sample.get("opportunity_state"), default="watch_for_trigger"),
        "deployment_outcome": deployment,
        "entry_quality_outcome": entry_quality_outcome,
        "source_type": _first_text(sample.get("source_type"), default="unknown"),
        "outcome_label": _first_text(sample.get("outcome_label"), default="observed"),
        "net_pnl": _safe_float(sample.get("net_pnl")) - _safe_float(sample.get("commission")),
        "reward_source": _first_text(
            result.get("reward_source"),
            result.get("episode_reward_source"),
            result.get("pnl_source"),
            default="unknown",
        ),
        "not_trade_authority": True,
        "future_only": True,
    }


def classify_action(action: Any, *, target_lots: int = 0, current_lots: int = 0) -> str:
    text = _clean_token(action, "")
    explicit_family = canonical_action_family(text)
    if explicit_family == "execution":
        return "execution"
    current = _safe_int(current_lots)
    target = _safe_int(target_lots)
    lifecycle = contract_final_learning_lifecycle(
        {
            "final_action": text,
            "current_lots": current,
            "target_lots": target,
            "lots_delta": target - current,
        }
    )
    if lifecycle == "open_add_new_risk":
        if current and target and current * target > 0 and abs(target) > abs(current):
            return "add"
        return "open"
    if lifecycle == "reduce_exit":
        if target == 0 or (current and target and current * target < 0):
            return "exit"
        return "reduce"
    if lifecycle == "hold":
        return "hold"
    if lifecycle == "conditional_monitor":
        return "conditional_monitor"
    if current == 0 and target == 0 and explicit_family in {"hold", "no_trade"}:
        return "observe"
    if explicit_family == "reduce_exit":
        return "reduce" if canonical_action_value_lane(text, current, target) == "reduce" else "exit"
    if explicit_family == "open_add_new_risk":
        return "add" if canonical_action_value_lane(text, current, target) == "add" else "open"
    if explicit_family == "hold":
        return "hold"
    if explicit_family == "conditional_monitor":
        return "conditional_monitor"
    return "observe"


COUNTERFACTUAL_SOURCE_TYPES = {
    "counterfactual_missed_alpha",
    "counterfactual_reasonable_avoidance",
    "counterfactual_correct_avoidance",
}
COUNTERFACTUAL_REWARD_WEIGHT = 0.35
RESEARCH_ACTION_VALUE_CONTRACT_VERSION = "agentquant.research_action_value.v1"
PRODUCT_LEARNING_PERFORMANCE_KEY_VERSION = "agentquant.product_learning_performance_key.v1"
INCOMPLETE_SETUP_TYPES = {
    "",
    "*",
    "unknown",
    "generic_trade_setup",
}
INCOMPLETE_STATE_TOKENS = {"", "*", "unknown"}


def _action_value_lane(action_name: Any) -> str:
    return canonical_action_value_lane(action_name)


def _memory_side_role_for_action(action_name: Any) -> str:
    lane = _action_value_lane(action_name)
    if lane in {"open", "add", "scale", "increase"}:
        return "target_side"
    if lane in {"hold", "reduce", "exit"}:
        return "current_position_side"
    if lane == "conditional_monitor":
        return "trigger_side"
    if lane == "execution":
        return "historical_sample_side"
    return "historical_sample_side"


def _learning_consumer_scope(action_name: Any) -> str:
    # alpha_setup_action_value is PM-consumed learning. Trader execution
    # diagnostics use separate trader_execution_learning traces.
    return "pm_learning"


def _learning_retrieval_keys(
    *,
    profile_scope: Mapping[str, Any],
    action_name: Any,
    action_value_lane: str,
) -> Dict[str, str]:
    ticker = str(profile_scope.get("ticker") or "*").strip().upper() or "*"
    side = _clean_token(profile_scope.get("side"), "*")
    horizon = _clean_token(profile_scope.get("horizon_class"), "*")
    regime = canonical_market_regime(profile_scope.get("market_regime"), "*")
    setup_type = _clean_token(profile_scope.get("setup_type"), "*")
    lane = _clean_token(action_value_lane or action_name, "*")
    exact_execution_retrieval_key = str(
        profile_scope.get("execution_retrieval_key") or ""
    ).strip()
    return {
        "retrieval_key": "|".join([ticker, side, horizon, regime, setup_type, lane]),
        "fallback_retrieval_key": "|".join([ticker, side, horizon, lane]),
        "execution_retrieval_key": (
            exact_execution_retrieval_key
            if lane == "execution"
            else ""
        ),
    }


def _action_value_usage_boundary(
    *,
    action_name: str,
    action_preference: str,
    amplification_scope_quality: str,
    reward_source: str,
) -> Dict[str, Any]:
    lane = _action_value_lane(action_name)
    family = canonical_action_family(action_name)
    forbidden_common = [
        "direct_trade_authority",
        "bypass_final_action_contract",
        "bypass_auditor",
        "bypass_trader",
        "same_day_decision_use",
    ]
    if family == "open_add_new_risk":
        allowed = ["open_preference", "probe_candidate"]
        if (
            action_preference == "positive_candidate_open"
            and amplification_scope_quality == "exact_real_state"
            and ("real" in reward_source or "episode" in reward_source)
        ):
            allowed.extend(["real_budget_entry_candidate", "scale_candidate"])
        return {
            "contract_version": RESEARCH_ACTION_VALUE_CONTRACT_VERSION,
            "canonical_action_family": family,
            "lane": lane,
            "usable_by": ["portfolio_manager", "auditor", "protocol_governor"],
            "allowed_effects": allowed,
            "forbidden_effects": forbidden_common
            + ["trader_execution_profile", "analyst_signal_override"],
            "source_quality": amplification_scope_quality,
            "reward_source": reward_source,
            "must_flow_through_final_action_contract": True,
            "does_not_create_trade_authority": True,
        }
    if family == "hold":
        return {
            "contract_version": RESEARCH_ACTION_VALUE_CONTRACT_VERSION,
            "canonical_action_family": family,
            "lane": lane,
            "usable_by": ["portfolio_manager", "auditor", "protocol_governor"],
            "allowed_effects": ["hold_preference", "position_lifecycle_preference", "profit_giveback_context"],
            "forbidden_effects": forbidden_common
            + ["open_amplification", "real_budget_entry", "scale_position", "trader_execution_profile"],
            "source_quality": amplification_scope_quality,
            "reward_source": reward_source,
            "must_flow_through_final_action_contract": True,
            "does_not_create_trade_authority": True,
        }
    if family == "reduce_exit":
        return {
            "contract_version": RESEARCH_ACTION_VALUE_CONTRACT_VERSION,
            "canonical_action_family": family,
            "lane": lane,
            "usable_by": ["portfolio_manager", "auditor", "protocol_governor"],
            "allowed_effects": ["protect_profit", "reduce_or_exit_preference", "stop_or_revalidation_context"],
            "forbidden_effects": forbidden_common
            + [
                "open_amplification",
                "real_budget_entry",
                "scale_position",
                "change_direction",
                "change_lots",
                "change_target_lots",
                "change_margin_ratio",
                "trader_execution_profile",
            ],
            "source_quality": amplification_scope_quality,
            "reward_source": reward_source,
            "must_flow_through_final_action_contract": True,
            "does_not_create_trade_authority": True,
        }
    if family == "execution":
        return {
            "contract_version": RESEARCH_ACTION_VALUE_CONTRACT_VERSION,
            "canonical_action_family": family,
            "lane": lane,
            "usable_by": ["trader", "portfolio_manager", "auditor", "protocol_governor"],
            "allowed_effects": ["execution_profile_preference", "trigger_method_preference", "execution_quality_context"],
            "forbidden_effects": forbidden_common
            + [
                "open_amplification",
                "real_budget_entry",
                "scale_position",
                "change_direction",
                "change_lots",
                "change_target_lots",
                "change_margin_ratio",
                "create_trade_authority",
            ],
            "source_quality": amplification_scope_quality,
            "reward_source": reward_source,
            "must_flow_through_final_action_contract": True,
            "does_not_create_trade_authority": True,
        }
    return {
        "contract_version": RESEARCH_ACTION_VALUE_CONTRACT_VERSION,
        "canonical_action_family": family,
        "lane": lane,
        "usable_by": ["analysis_team", "portfolio_manager", "protocol_governor"],
        "allowed_effects": ["evidence_quality_context"],
        "forbidden_effects": forbidden_common
        + ["real_budget_entry", "scale_position", "change_lots", "change_direction"],
        "source_quality": amplification_scope_quality,
        "reward_source": reward_source,
        "must_flow_through_final_action_contract": True,
        "does_not_create_trade_authority": True,
    }


def _signal_calibration_contract(
    *,
    action_name: str,
    action_preference: str,
    amplification_scope_quality: str,
    reward_source: str,
) -> Dict[str, Any]:
    lane = _action_value_lane(action_name)
    family = canonical_action_family(action_name)
    preference_text = str(action_preference or "").lower()
    if lane in {"exit", "reduce"} and preference_text.startswith("positive_"):
        calibration_bias = "questions_same_side_continuation"
    elif lane == "execution":
        calibration_bias = "execution_context_only"
    elif any(token in preference_text for token in {"negative", "tail_loss", "protect", "cap", "reduce", "exit"}):
        calibration_bias = "negative_evidence_calibration"
    elif preference_text.startswith("positive_") or "controlled_open" in preference_text:
        calibration_bias = "positive_evidence_calibration"
    else:
        calibration_bias = "neutral_evidence_context"
    return {
        "contract_version": "agentquant.analysis_signal_calibration.v1",
        "source_action_value_contract": RESEARCH_ACTION_VALUE_CONTRACT_VERSION,
        "source_canonical_action_family": family,
        "consumer_scope": "analyst_calibration",
        "source_action_value_lane": lane,
        "source_action_preference": action_preference,
        "source_quality": amplification_scope_quality,
        "reward_source": reward_source,
        "calibration_bias": calibration_bias,
        "usable_by": ["analysis_team"],
        "allowed_effects": ["evidence_quality_calibration", "setup_reliability_context", "fresh_data_questioning"],
        "forbidden_effects": [
            "trade_authority",
            "lots",
            "margin_ratio",
            "direction_override",
            "bypass_pm",
            "bypass_auditor",
            "bypass_trader",
        ],
        "current_data_must_dominate": True,
    }


def _is_counterfactual_source(source_type: Any) -> bool:
    text = str(source_type or "").strip().lower()
    return text in COUNTERFACTUAL_SOURCE_TYPES or text.startswith("counterfactual_")


def _alpha_state_completeness(profile_scope: Mapping[str, Any], action_name: str) -> Dict[str, Any]:
    ticker = str(profile_scope.get("ticker") or "").strip().upper()
    side = _clean_token(profile_scope.get("side"), "*")
    horizon = _clean_token(profile_scope.get("horizon_class"), "unknown")
    regime = canonical_market_regime(profile_scope.get("market_regime"), "unknown")
    setup_type = _clean_token(profile_scope.get("setup_type"), "unknown")
    action = _clean_token(action_name, "unknown")
    missing: List[str] = []
    if not ticker or ticker == "*":
        missing.append("ticker")
    if side in INCOMPLETE_STATE_TOKENS:
        missing.append("side")
    if horizon in INCOMPLETE_STATE_TOKENS:
        missing.append("horizon_class")
    if regime in INCOMPLETE_STATE_TOKENS:
        missing.append("market_regime")
    if setup_type in INCOMPLETE_SETUP_TYPES:
        missing.append("setup_type")
    if action in INCOMPLETE_STATE_TOKENS:
        missing.append("action_name")
    return {
        "complete": not missing,
        "missing_fields": missing,
        "state_key": {
            "ticker": ticker or "*",
            "side": side,
            "horizon_class": horizon,
            "market_regime": regime,
            "setup_type": setup_type,
            "action_name": action,
        },
    }


def _action_preference_from_stats(
    *,
    action_name: str,
    reward_mean: float,
    reward_sum: float,
    win_rate: float,
    real_trade_reward_count: int,
    amplification_scope_quality: str,
    loss_reward_count: int,
    tail_loss_count: int,
    worst_reward: float,
) -> str:
    """Convert settled reward facts into an action preference, not a new gate."""
    action = _clean_token(action_name, "unknown")
    family = canonical_action_family(action)
    lane = canonical_action_value_lane(action)
    exact_real = amplification_scope_quality == "exact_real_state" and real_trade_reward_count > 0
    has_real_reward = real_trade_reward_count > 0
    positive = reward_sum > 0 and reward_mean > 0 and win_rate > 0
    negative = reward_sum < 0 or reward_mean < 0 or loss_reward_count > 0
    tail_loss = tail_loss_count > 0 or worst_reward <= -1000.0
    if positive and has_real_reward:
        if family == "open_add_new_risk" and lane in {"open", "add", "scale", "increase"}:
            return "positive_candidate_open"
        if exact_real and family == "hold":
            return "positive_candidate_hold"
        if family == "reduce_exit" and lane in {"reduce", "exit"}:
            return "positive_candidate_exit"
        if family == "execution" and lane == "execution":
            return "positive_candidate_execution"
    if has_real_reward and tail_loss:
        return "tail_loss_protect"
    if has_real_reward and negative:
        if family in {"hold", "observe", "no_trade"}:
            return "negative_hold_revalidate"
        return "negative_revalidate"
    return ""


def _reward_signal_for_row(row: Mapping[str, Any]) -> tuple[float | None, str]:
    """Return reward contribution and source class for action-value learning.

    Real executed trades keep full weight.  counterfactual no-trade outcomes are
    counterfactual and therefore enter only as a weak prior; they must not
    mature a setup into deployable authority by themselves.
    """

    result = row.get("result") if isinstance(row.get("result"), Mapping) else {}
    episode_reward = result.get("episode_net_pnl")
    reward = (
        _safe_float(episode_reward)
        if episode_reward is not None
        else _safe_float(row.get("net_pnl")) - _safe_float(row.get("commission"))
    )
    source_type = str(row.get("source_type") or "").strip().lower()
    if source_type in {"trade_episode", "episode_trade"}:
        return reward, "episode_trade"
    action_name = classify_action(
        _sample_row_value(row, "action_taken"),
        target_lots=_safe_int(_sample_row_value(row, "target_lots")),
        current_lots=_safe_int(_sample_row_value(row, "current_lots")),
    )
    action_family = canonical_action_family(action_name)
    current_lots = _safe_int(_sample_row_value(row, "current_lots"))
    target_lots = _safe_int(_sample_row_value(row, "target_lots"))
    executed_lots = _safe_int(row.get("executed_lots"))
    if (
        source_type == "trade"
        and action_family == "open_add_new_risk"
        and episode_reward is None
    ):
        return None, "open_add_waiting_for_complete_episode"
    if (
        action_family == "hold"
        and source_type == "trade"
        and executed_lots == 0
        and current_lots != 0
        and current_lots == target_lots
    ):
        return reward, "real_trade"
    if action_family == "reduce_exit" and source_type == "trade" and executed_lots > 0:
        return reward, "real_trade"
    if action_family == "execution" and source_type in {"execution", "trade"} and executed_lots > 0:
        return reward, "real_trade"
    if _is_counterfactual_source(source_type):
        return reward * COUNTERFACTUAL_REWARD_WEIGHT, "counterfactual_prior"
    return None, "ignored"


def _episode_return_on_notional_for_row(row: Mapping[str, Any]) -> float | None:
    source_type = str(row.get("source_type") or "").strip().lower()
    if source_type not in {"trade_episode", "episode_trade"}:
        return None
    result_value = _sample_row_value(row, "result", {})
    result = result_value if isinstance(result_value, Mapping) else {}
    value = result.get("return_on_notional")
    if value is None:
        return None
    return _safe_float(value)


def _prefer_episode_reward_rows(rows: List[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    episode_recommendations = {
        str(row.get("recommendation_id") or "")
        for row in rows
        if str(row.get("source_type") or "").strip().lower() in {"trade_episode", "episode_trade"}
        and str(row.get("recommendation_id") or "")
    }
    if not episode_recommendations:
        return rows
    filtered: List[Mapping[str, Any]] = []
    for row in rows:
        source_type = str(row.get("source_type") or "").strip().lower()
        recommendation_id = str(row.get("recommendation_id") or "")
        if source_type == "trade" and recommendation_id in episode_recommendations:
            continue
        filtered.append(row)
    return filtered


def _sample_row_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = row.get("payload")
    if isinstance(payload, Mapping):
        return payload
    raw = row.get("payload_json")
    if isinstance(raw, Mapping):
        return raw
    if raw:
        try:
            parsed = json.loads(str(raw))
            if isinstance(parsed, Mapping):
                return parsed
        except Exception:
            pass
    return {}


def _sample_row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = row.get(key)
    if value is not None and value != "":
        return value
    return _sample_row_payload(row).get(key, default)


def _profile_thresholds(cfg: Mapping[str, Any]) -> Dict[str, Any]:
    learning_cfg = (cfg or {}).get("learning", {}) or {}
    profile_cfg = learning_cfg.get("alpha_setup_profile", {}) or {}
    return {
        "valid_days": int(profile_cfg.get("valid_days", learning_cfg.get("memory_expires_after_days", 30)) or 30),
        "lookback_days": int(profile_cfg.get("lookback_days", 90) or 90),
        "min_samples_watchlist": int(profile_cfg.get("min_samples_watchlist", 2) or 2),
        "min_samples_protected": int(profile_cfg.get("min_samples_protected", 4) or 4),
        "min_samples_deployable": int(profile_cfg.get("min_samples_deployable", 7) or 7),
        "protected_min_win_rate": float(profile_cfg.get("protected_min_win_rate", 0.52) or 0.52),
        "deployable_min_win_rate": float(profile_cfg.get("deployable_min_win_rate", 0.56) or 0.56),
        "protected_min_profit_factor": float(profile_cfg.get("protected_min_profit_factor", 1.08) or 1.08),
        "deployable_min_profit_factor": float(profile_cfg.get("deployable_min_profit_factor", 1.25) or 1.25),
        "protected_min_net_pnl": float(profile_cfg.get("protected_min_net_pnl", 800.0) or 800.0),
        "deployable_min_net_pnl": float(profile_cfg.get("deployable_min_net_pnl", 2500.0) or 2500.0),
        "cap_min_samples": int(profile_cfg.get("cap_min_samples", 2) or 2),
        "cap_min_loss_abs": abs(float(profile_cfg.get("cap_min_loss_abs", 8000.0) or 8000.0)),
        "cap_net_loss_abs": abs(float(profile_cfg.get("cap_net_loss_abs", 1500.0) or 1500.0)),
        "cap_profit_factor_below": float(profile_cfg.get("cap_profit_factor_below", 0.90) or 0.90),
        "reject_min_samples": int(profile_cfg.get("reject_min_samples", 5) or 5),
        "reject_min_loss_abs": abs(float(profile_cfg.get("reject_min_loss_abs", 20000.0) or 20000.0)),
        "reject_profit_factor_below": float(profile_cfg.get("reject_profit_factor_below", 0.65) or 0.65),
        "candidate_max_position_impact": float(profile_cfg.get("candidate_max_position_impact", 0.010) or 0.010),
        "watchlist_max_position_impact": float(profile_cfg.get("watchlist_max_position_impact", 0.015) or 0.015),
        "protected_max_position_impact": float(profile_cfg.get("protected_max_position_impact", 0.030) or 0.030),
        "deployable_max_position_impact": float(profile_cfg.get("deployable_max_position_impact", 0.045) or 0.045),
    }


def _stats_from_rows(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    row_list = [row for row in rows if isinstance(row, Mapping)]
    trade_rows = [
        row
        for row in row_list
        if str(row.get("source_type") or "").strip().lower()
        in {"trade_episode", "episode_trade"}
    ]
    no_trade_rows = [
        row
        for row in row_list
        if str(row.get("source_type") or "").strip().lower() == "no_trade"
        or _is_counterfactual_source(row.get("source_type"))
    ]
    pnl_values = [_safe_float(row.get("net_pnl")) - _safe_float(row.get("commission")) for row in trade_rows]
    gross_profit = sum(value for value in pnl_values if value > 0)
    gross_loss = sum(value for value in pnl_values if value < 0)
    win_count = sum(1 for value in pnl_values if value > 0)
    loss_count = sum(1 for value in pnl_values if value < 0)
    sample_count = len(row_list)
    trade_count = len(trade_rows)
    win_rate = (win_count / trade_count) if trade_count else 0.0
    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss < 0 else (99.0 if gross_profit > 0 else 0.0)
    holding_days = [_safe_int(row.get("holding_days")) for row in trade_rows if _safe_int(row.get("holding_days")) > 0]
    return {
        "sample_count": sample_count,
        "trade_count": trade_count,
        "no_trade_count": len(no_trade_rows),
        "win_count": win_count,
        "loss_count": loss_count,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_pnl": sum(pnl_values),
        "total_commission": sum(_safe_float(row.get("commission")) for row in trade_rows),
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "max_loss": min(pnl_values) if pnl_values else 0.0,
        "avg_holding_days": (sum(holding_days) / len(holding_days)) if holding_days else 0.0,
    }


def classify_lifecycle(stats: Mapping[str, Any], cfg: Mapping[str, Any]) -> Dict[str, Any]:
    t = _profile_thresholds(cfg)
    sample_count = _safe_int(stats.get("sample_count"))
    trade_count = _safe_int(stats.get("trade_count"))
    win_rate = _safe_float(stats.get("win_rate"))
    net_pnl = _safe_float(stats.get("net_pnl"))
    profit_factor = _safe_float(stats.get("profit_factor"))
    max_loss = _safe_float(stats.get("max_loss"))

    state = "candidate"
    reason = "insufficient_samples_or_edge"
    if (
        sample_count >= t["reject_min_samples"]
        and net_pnl <= -t["reject_min_loss_abs"]
        and profit_factor <= t["reject_profit_factor_below"]
    ):
        state = "rejected"
        reason = "same_scope_negative_expectancy"
    elif (
        sample_count >= t["cap_min_samples"]
        and (
            net_pnl <= -t["cap_min_loss_abs"]
            or max_loss <= -t["cap_min_loss_abs"]
            or (trade_count >= t["cap_min_samples"] and net_pnl <= -t["cap_net_loss_abs"] and profit_factor < t["cap_profit_factor_below"])
        )
    ):
        state = "capped"
        reason = "same_scope_loss_cap"
    elif (
        trade_count >= t["min_samples_deployable"]
        and win_rate >= t["deployable_min_win_rate"]
        and profit_factor >= t["deployable_min_profit_factor"]
        and net_pnl >= t["deployable_min_net_pnl"]
    ):
        state = "deployable"
        reason = "same_scope_positive_expectancy"
    elif (
        trade_count >= t["min_samples_protected"]
        and win_rate >= t["protected_min_win_rate"]
        and profit_factor >= t["protected_min_profit_factor"]
        and net_pnl >= t["protected_min_net_pnl"]
    ):
        state = "protected"
        reason = "same_scope_positive_but_not_deployable"
    elif sample_count >= t["min_samples_watchlist"]:
        state = "watchlist"
        reason = "same_scope_watchlist_under_validation"

    confidence = min(
        0.95,
        0.12
        + min(0.34, sample_count / max(1.0, t["min_samples_deployable"] * 1.5))
        + min(0.24, abs(win_rate - 0.5))
        + min(0.24, abs(net_pnl) / 50000.0)
    )
    profile_state_hint = {
        "deployable": "profile_deployable",
        "protected": "profile_protected",
        "watchlist": "profile_watchlist",
        "candidate": "profile_candidate",
        "capped": "profile_capped",
        "rejected": "profile_rejected",
    }.get(state, "profile_observe")
    max_position_impact = {
        "deployable": t["deployable_max_position_impact"],
        "protected": t["protected_max_position_impact"],
        "watchlist": t["watchlist_max_position_impact"],
        "candidate": t["candidate_max_position_impact"],
        "capped": t["candidate_max_position_impact"],
        "rejected": 0.0,
    }.get(state, 0.0)
    return {
        "lifecycle_state": state,
        "reason": reason,
        "profile_state_hint": profile_state_hint,
        "profile_state_hint_boundary": "profile lifecycle hint only; not an action preference or trade command",
        "confidence_score": confidence,
        "max_position_impact": max_position_impact,
    }


def upsert_alpha_setup_sample_and_profile(
    cursor: sqlite3.Cursor,
    *,
    cfg: Mapping[str, Any],
    config_id: str,
    trading_date: str,
    sample: Mapping[str, Any],
) -> Dict[str, Any]:
    learning_cfg = (cfg or {}).get("learning", {}) or {}
    profile_cfg = learning_cfg.get("alpha_setup_profile", {}) or {}
    if not bool(profile_cfg.get("enabled", True)):
        return {"rows": 0, "status": "disabled"}

    now = _utc_now()
    ticker = str(sample.get("ticker") or "*").upper()
    side = _clean_token(sample.get("side"), "*")
    sector = str(sample.get("sector") or "unknown")
    horizon = _clean_token(sample.get("horizon_class"), "unknown")
    regime = canonical_market_regime(sample.get("market_regime"), "unknown")
    setup_type = _clean_token(sample.get("setup_type"), "unknown")
    data_combo = _clean_token(sample.get("data_combo"), "unknown")[:180]
    scope_key = str(sample.get("scope_key") or build_scope_key(
        ticker=ticker,
        side=side,
        horizon_class=horizon,
        market_regime=regime,
        setup_type=setup_type,
        data_combo=data_combo,
    ))
    recommendation_id = str(sample.get("recommendation_id") or "")
    sample_id = str(uuid.uuid4())
    source_type = str(sample.get("source_type") or "trade")
    action_name = classify_action(
        sample.get("action_taken"),
        target_lots=_safe_int(sample.get("target_lots")),
        current_lots=_safe_int(sample.get("current_lots")),
    )
    evidence = sample.get("evidence") if isinstance(sample.get("evidence"), Mapping) else {}
    result = sample.get("result") if isinstance(sample.get("result"), Mapping) else {}
    product_learning_performance_key = build_product_learning_performance_key(
        scope_key=scope_key,
        sample=sample,
        action_name=action_name,
        evidence=evidence,
        result=result,
    )
    payload = {
        **dict(sample),
        "scope_key": scope_key,
        "action_name": action_name,
        "product_learning_performance_key": product_learning_performance_key,
        "anti_overfit_boundary": {
            "same_scope_required": True,
            "not_product_blacklist": True,
            "future_only": True,
            "candidate_prior_only": True,
        },
    }
    research_memory_writers.upsert_alpha_setup_sample(
        cursor,
        record={
            "id": sample_id,
            "config_id": config_id,
            "trading_date": str(trading_date)[:10],
            "ticker": ticker,
            "side": side,
            "sector": sector,
            "horizon_class": horizon,
            "market_regime": regime,
            "setup_type": setup_type,
            "data_combo": data_combo,
            "scope_key": scope_key,
            "source_type": source_type,
            "recommendation_id": recommendation_id or None,
            "action_taken": str(sample.get("action_taken") or ""),
            "pm_action": str(sample.get("pm_action") or ""),
            "auditor_decision": str(sample.get("auditor_decision") or ""),
            "trader_status": str(sample.get("trader_status") or ""),
            "target_lots": _safe_int(sample.get("target_lots")),
            "executed_lots": _safe_int(sample.get("executed_lots")),
            "net_pnl": _safe_float(sample.get("net_pnl")),
            "commission": _safe_float(sample.get("commission")),
            "holding_days": _safe_int(sample.get("holding_days")),
            "outcome_label": str(sample.get("outcome_label") or "observed"),
            "setup_quality_score": _safe_float(sample.get("setup_quality_score")),
            "opportunity_state": str(sample.get("opportunity_state") or "watch_for_trigger"),
            "evidence_json": _json_dumps(evidence),
            "result_json": _json_dumps(result),
            "created_at": now,
            "payload_json": _json_dumps(payload),
        },
    )

    lookback_days = _profile_thresholds(cfg)["lookback_days"]
    lookback_start = (
        datetime.strptime(str(trading_date)[:10], "%Y-%m-%d") - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")
    cursor.execute(
        """
        SELECT *
        FROM alpha_setup_sample
        WHERE config_id = ?
          AND scope_key = ?
          AND trading_date >= ?
          AND trading_date <= ?
        ORDER BY trading_date, created_at
        """,
        (config_id, scope_key, lookback_start, str(trading_date)[:10]),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    stats = _stats_from_rows(rows)
    lifecycle = classify_lifecycle(stats, cfg)
    valid_until = _valid_until(str(trading_date)[:10], _profile_thresholds(cfg)["valid_days"])
    contract = build_next_round_memory_contract(
        memory_type="alpha_setup_profile",
        maturity_state=lifecycle["lifecycle_state"],
        scope={
            "ticker": ticker,
            "sector": sector,
            "side": side,
            "horizon_class": horizon,
            "expected_horizon_days": _safe_int(sample.get("expected_horizon_days")),
            "market_regime": regime,
            "setup_type": setup_type,
            "data_combo": data_combo,
        },
        usable_memory=[
            f"setup={setup_type}; state={lifecycle['lifecycle_state']}; profile_state_hint={lifecycle['profile_state_hint']}",
            f"n={stats['sample_count']}; trades={stats['trade_count']}; win_rate={stats['win_rate']:.2f}; pf={stats['profit_factor']:.2f}; net_pnl={stats['net_pnl']:.0f}",
        ],
        analysis_strategy_updates=[
            "Compare today's setup, market state, data combo, trigger, and invalidation with this profile.",
            "Compare today's product-learning performance key before changing evidence strength.",
            "Use positive profiles to sharpen opportunity identification; use weak profiles to ask for stronger evidence, not to blacklist products.",
        ],
        trading_strategy_updates=[
            "PM may translate deployable/protected profiles into controlled sizing only with current confirmation and invalidation.",
            "PM ranking and capital deployment may use this product-level performance key only through the final_action_contract.",
            "Candidate/watchlist profiles can guide probe/watchlist decisions; capped/rejected profiles require repair evidence before new risk.",
        ],
        pm_action_conditions=[
            "Never override the 20% total margin hard cap, Auditor, Trader execution feasibility, or current-day data confirmation.",
            "Do not use candidate profiles to add, position_match, or continue losing exposure.",
        ],
        invalidates_when=[
            "Same-scope future samples turn expectancy negative or contradict the profile.",
            "Today's data combo, market regime, trigger, or invalidation differs materially from the profile scope.",
        ],
        validation_plan=[
            "Update after every settled same-scope trade/no-trade sample; demote if action impact performs poorly.",
        ],
        position_authority=(
            "controlled_pm_prior"
            if lifecycle["lifecycle_state"] in {"deployable", "protected"}
            else "analysis_or_watchlist_only"
        ),
        max_position_impact=lifecycle["max_position_impact"],
        sample_count=stats["sample_count"],
        confidence_score=lifecycle["confidence_score"],
    )
    profile_payload = {
        "stats": stats,
        "classification": lifecycle,
        "profile_state_hint": lifecycle["profile_state_hint"],
        "profile_state_hint_boundary": "profile lifecycle hint only; not an action preference or trade command",
        "last_sample": payload,
        "product_learning_performance_key": product_learning_performance_key,
        "lookback_days": lookback_days,
        "not_product_blacklist": True,
        CONTRACT_KEY: contract,
    }
    research_memory_writers.upsert_alpha_setup_profile(
        cursor,
        record={
            "id": str(uuid.uuid4()),
            "config_id": config_id,
            "ticker": ticker,
            "side": side,
            "sector": sector,
            "horizon_class": horizon,
            "market_regime": regime,
            "setup_type": setup_type,
            "data_combo": data_combo,
            "scope_key": scope_key,
            "lifecycle_state": lifecycle["lifecycle_state"],
            "profile_state_hint": lifecycle["profile_state_hint"],
            "sample_count": stats["sample_count"],
            "trade_count": stats["trade_count"],
            "no_trade_count": stats["no_trade_count"],
            "win_count": stats["win_count"],
            "loss_count": stats["loss_count"],
            "gross_profit": stats["gross_profit"],
            "gross_loss": stats["gross_loss"],
            "net_pnl": stats["net_pnl"],
            "total_commission": stats["total_commission"],
            "profit_factor": stats["profit_factor"],
            "win_rate": stats["win_rate"],
            "max_loss": stats["max_loss"],
            "avg_holding_days": stats["avg_holding_days"],
            "confidence_score": lifecycle["confidence_score"],
            "max_position_impact": lifecycle["max_position_impact"],
            "last_sample_date": str(trading_date)[:10],
            "created_at": now,
            "updated_at": now,
            "valid_until": valid_until,
            "payload_json": _json_dumps(profile_payload),
        },
    )
    _upsert_action_values(
        cursor,
        cfg=cfg,
        config_id=config_id,
        scope_key=scope_key,
        profile_scope={
            "ticker": ticker,
            "side": side,
            "horizon_class": horizon,
            "expected_horizon_days": _safe_int(sample.get("expected_horizon_days")),
            "market_regime": regime,
            "setup_type": setup_type,
            "data_combo": data_combo,
            "execution_retrieval_key": str(
                sample.get("execution_retrieval_key") or ""
            ).strip(),
        },
        trading_date=str(trading_date)[:10],
        rows=rows,
        profile_lifecycle=lifecycle,
        product_learning_performance_key=product_learning_performance_key,
        now=now,
    )
    return {
        "rows": 1,
        "status": "applied",
        "scope_key": scope_key,
        "lifecycle_state": lifecycle["lifecycle_state"],
        "profile_state_hint": lifecycle["profile_state_hint"],
        "confidence_score": lifecycle["confidence_score"],
        "stats": stats,
    }


def _upsert_action_values(
    cursor: sqlite3.Cursor,
    *,
    cfg: Mapping[str, Any],
    config_id: str,
    scope_key: str,
    profile_scope: Mapping[str, Any],
    trading_date: str,
    rows: List[Mapping[str, Any]],
    profile_lifecycle: Mapping[str, Any],
    product_learning_performance_key: Mapping[str, Any],
    now: str,
) -> None:
    grouped: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        action_name = classify_action(
            _sample_row_value(row, "action_taken"),
            target_lots=_safe_int(_sample_row_value(row, "target_lots")),
            current_lots=_safe_int(_sample_row_value(row, "current_lots")),
        )
        grouped.setdefault(action_name, []).append(row)
    for action_name, raw_action_rows in grouped.items():
        action_rows = _prefer_episode_reward_rows(raw_action_rows)
        reward_values: List[float] = []
        return_on_notional_values: List[float] = []
        reward_rows: List[Mapping[str, Any]] = []
        real_trade_reward_count = 0
        episode_trade_reward_count = 0
        counterfactual_reward_count = 0
        counterfactual_source_types = set()
        for row in action_rows:
            reward, reward_source = _reward_signal_for_row(row)
            if reward is None:
                continue
            reward_values.append(reward)
            reward_rows.append(row)
            if reward_source == "episode_trade":
                real_trade_reward_count += 1
                episode_trade_reward_count += 1
                episode_return = _episode_return_on_notional_for_row(row)
                if episode_return is not None:
                    return_on_notional_values.append(episode_return)
            elif reward_source == "real_trade":
                real_trade_reward_count += 1
            elif reward_source == "counterfactual_prior":
                counterfactual_reward_count += 1
                counterfactual_source_types.add(str(row.get("source_type") or "counterfactual"))
        if canonical_action_family(action_name) == "open_add_new_risk" and not reward_values:
            continue
        reward_sum = sum(reward_values)
        sample_count = len(reward_values)
        reward_mean = reward_sum / len(reward_values) if reward_values else 0.0
        win_rate = (sum(1 for value in reward_values if value > 0) / len(reward_values)) if reward_values else 0.0
        loss_reward_count = sum(1 for value in reward_values if value < 0)
        tail_loss_count = sum(1 for value in reward_values if value <= -1000.0)
        worst_reward = min(reward_values) if reward_values else 0.0
        mean_return_on_notional = (
            sum(return_on_notional_values) / len(return_on_notional_values)
            if return_on_notional_values
            else None
        )
        worst_return_on_notional = (
            min(return_on_notional_values)
            if return_on_notional_values
            else None
        )
        state_completeness = _alpha_state_completeness(profile_scope, action_name)
        if episode_trade_reward_count > 0 and not bool(state_completeness.get("complete")):
            continue
        action_profile_lifecycle = profile_lifecycle
        if canonical_action_family(action_name) == "open_add_new_risk":
            action_profile_lifecycle = classify_lifecycle(_stats_from_rows(reward_rows), cfg)
        latest_reward_row = max(
            reward_rows,
            key=lambda item: (
                str(item.get("trading_date") or "")[:10],
                str(item.get("created_at") or ""),
            ),
            default=None,
        )
        latest_reward_payload = (
            _sample_row_payload(latest_reward_row)
            if isinstance(latest_reward_row, Mapping)
            else {}
        )
        action_product_learning_performance_key = (
            latest_reward_payload.get("product_learning_performance_key")
            if isinstance(latest_reward_payload.get("product_learning_performance_key"), Mapping)
            else product_learning_performance_key
        )
        action_last_sample_date = (
            str(latest_reward_row.get("trading_date") or trading_date)[:10]
            if isinstance(latest_reward_row, Mapping)
            else str(trading_date)[:10]
        )
        action_valid_until = _valid_until(
            action_last_sample_date,
            _profile_thresholds(cfg)["valid_days"],
        )
        if real_trade_reward_count > 0 and bool(state_completeness.get("complete")):
            amplification_scope_quality = "exact_real_state"
            exact_state_real_trade_sample_count = real_trade_reward_count
            partial_state_real_trade_sample_count = 0
        elif real_trade_reward_count > 0:
            amplification_scope_quality = "partial_real_state"
            exact_state_real_trade_sample_count = 0
            partial_state_real_trade_sample_count = real_trade_reward_count
        elif counterfactual_reward_count > 0:
            amplification_scope_quality = "counterfactual_prior"
            exact_state_real_trade_sample_count = 0
            partial_state_real_trade_sample_count = 0
        else:
            amplification_scope_quality = "unqualified"
            exact_state_real_trade_sample_count = 0
            partial_state_real_trade_sample_count = 0
        if episode_trade_reward_count > 0:
            reward_source = "trade_episode"
        elif real_trade_reward_count > 0:
            reward_source = "real_trade"
        elif counterfactual_reward_count > 0:
            reward_source = "counterfactual_prior"
        else:
            reward_source = "unqualified"
        confidence = min(
            0.95,
            _safe_float(action_profile_lifecycle.get("confidence_score"))
            * min(1.0, max(0.25, sample_count / max(1, _profile_thresholds(cfg)["min_samples_deployable"]))),
        )
        action_preference = _action_preference_from_stats(
            action_name=action_name,
            reward_mean=reward_mean,
            reward_sum=reward_sum,
            win_rate=win_rate,
            real_trade_reward_count=real_trade_reward_count,
            amplification_scope_quality=amplification_scope_quality,
            loss_reward_count=loss_reward_count,
            tail_loss_count=tail_loss_count,
            worst_reward=worst_reward,
        )
        canonical_family = canonical_action_family(action_name)
        action_value_lane = canonical_action_value_lane(action_name)
        consumer_scope = _learning_consumer_scope(action_name)
        memory_side_role = _memory_side_role_for_action(action_name)
        retrieval_keys = _learning_retrieval_keys(
            profile_scope=profile_scope,
            action_name=action_name,
            action_value_lane=action_value_lane,
        )
        usage_boundary = _action_value_usage_boundary(
            action_name=action_name,
            action_preference=action_preference,
            amplification_scope_quality=amplification_scope_quality,
            reward_source=reward_source,
        )
        signal_calibration = _signal_calibration_contract(
            action_name=action_name,
            action_preference=action_preference,
            amplification_scope_quality=amplification_scope_quality,
            reward_source=reward_source,
        )
        payload = {
            "research_output_contract_version": RESEARCH_ACTION_VALUE_CONTRACT_VERSION,
            "scope_key": scope_key,
            "expected_horizon_days": _safe_int(
                profile_scope.get("expected_horizon_days")
            ),
            "action_name": action_name,
            "canonical_action_family": canonical_family,
            "action_value_lane": action_value_lane,
            "consumer_scope": consumer_scope,
            "learning_lane": action_value_lane,
            "memory_side_role": memory_side_role,
            **retrieval_keys,
            "last_sample_date": action_last_sample_date,
            "sample_count": sample_count,
            "reward_sum": reward_sum,
            "reward_mean": reward_mean,
            "mean_return_on_notional": mean_return_on_notional,
            "worst_return_on_notional": worst_return_on_notional,
            "episode_return_on_notional_count": len(return_on_notional_values),
            "win_rate": win_rate,
            "profile_lifecycle": dict(action_profile_lifecycle),
            "source": "alpha_setup_profile_action_value",
            "product_learning_performance_key": action_product_learning_performance_key,
            "action_preference": action_preference,
            "canonical_action_preference_source": "payload.action_preference",
            "prior_role": "" if action_preference else "weak_prior_not_action_preference",
            "action_preference_boundary": (
                "candidate preferences guide PM action arbitration; they do not create "
                "real_budget_entry without current evidence and final authority"
            ),
            "usage_boundary": usage_boundary,
            "usable_by": usage_boundary["usable_by"],
            "allowed_effects": usage_boundary["allowed_effects"],
            "forbidden_effects": usage_boundary["forbidden_effects"],
            "signal_calibration": signal_calibration,
            "real_trade_reward_count": real_trade_reward_count,
            "episode_trade_reward_count": episode_trade_reward_count,
            "exact_state_real_trade_sample_count": exact_state_real_trade_sample_count,
            "partial_state_real_trade_sample_count": partial_state_real_trade_sample_count,
            "similar_real_trade_sample_count": 0,
            "amplification_scope_quality": amplification_scope_quality,
            "reward_source": reward_source,
            "sample_source": reward_source,
            "state_completeness": state_completeness,
            "entry_quality_outcome": (
                action_product_learning_performance_key.get("entry_quality_outcome")
                if isinstance(action_product_learning_performance_key.get("entry_quality_outcome"), Mapping)
                else {}
            ),
            "counterfactual_reward_count": counterfactual_reward_count,
            "loss_reward_count": loss_reward_count,
            "tail_loss_count": tail_loss_count,
            "worst_reward": worst_reward,
            "counterfactual_reward_weight": COUNTERFACTUAL_REWARD_WEIGHT,
            "counterfactual_source_types": sorted(counterfactual_source_types),
            "has_counterfactual_samples": counterfactual_reward_count > 0,
            "counterfactual_prior_only": counterfactual_reward_count > 0 and real_trade_reward_count <= 0,
            "not_rl_black_box": True,
            "bandit_style_update": True,
            "future_only": True,
        }
        consistency = validate_action_preference_family_consistency(
            {
                "action_name": action_name,
                "canonical_action_family": canonical_family,
                "action_value_lane": action_value_lane,
                "learning_lane": action_value_lane,
                "action_preference": action_preference,
            }
        )
        if not consistency.get("ok"):
            raise ValueError(
                "alpha_setup_action_value_semantic_contract_failed:"
                + ",".join(str(error) for error in consistency.get("errors") or [])
            )
        research_memory_writers.upsert_alpha_setup_action_value(
            cursor,
            record={
                "id": str(uuid.uuid4()),
                "config_id": config_id,
                "scope_key": scope_key,
                "ticker": str(profile_scope.get("ticker") or "*").upper(),
                "side": str(profile_scope.get("side") or "*"),
                "horizon_class": str(profile_scope.get("horizon_class") or "*"),
                "market_regime": str(profile_scope.get("market_regime") or "*"),
                "setup_type": str(profile_scope.get("setup_type") or "*"),
                "data_combo": str(profile_scope.get("data_combo") or "*"),
                "action_name": action_name,
                "canonical_action_family": canonical_family,
                "sample_count": sample_count,
                "reward_sum": reward_sum,
                "reward_mean": reward_mean,
                "win_rate": win_rate,
                "confidence_score": confidence,
                "action_preference": action_preference,
                "reward_source": reward_source,
                "evidence_scope": amplification_scope_quality,
                "action_value_lane": action_value_lane,
                "consumer_scope": consumer_scope,
                "learning_lane": action_value_lane,
                "memory_side_role": memory_side_role,
                "retrieval_key": retrieval_keys["retrieval_key"],
                "fallback_retrieval_key": retrieval_keys["fallback_retrieval_key"],
                "execution_retrieval_key": retrieval_keys["execution_retrieval_key"],
                "max_position_impact": _safe_float(action_profile_lifecycle.get("max_position_impact")),
                "last_sample_date": action_last_sample_date,
                "created_at": now,
                "updated_at": now,
                "valid_until": action_valid_until,
                "payload_json": _json_dumps(payload),
            },
        )


def profile_prompt_line(
    profile: Mapping[str, Any],
    *,
    include_entry_calibration: bool = True,
) -> str:
    state = str(profile.get("lifecycle_state") or "candidate")
    hint = str(
        profile.get("profile_state_hint")
        or "profile_observe"
    )
    product_view = compact_product_learning_performance_key_for_analyst(profile)
    product_suffix = ""
    if product_view:
        entry_view = product_view.get("entry_quality_calibration")
        if include_entry_calibration and isinstance(entry_view, Mapping) and entry_view:
            product_suffix = (
                " Product learning: "
                f"trigger={entry_view.get('trigger_key')}, "
                f"entry_quality={entry_view.get('entry_quality_verdict')}, "
                f"trigger_quality={entry_view.get('trigger_quality_verdict')}, "
                f"confirmation={entry_view.get('trigger_confirmation_adjustment')}, "
                f"support={_safe_float(entry_view.get('support_weight')):.2f}, "
                f"penalty={_safe_float(entry_view.get('penalty_weight')):.2f}. "
                "This is bounded historical entry calibration only."
            )
    return (
        f"{profile.get('ticker')}/{profile.get('side')}/{profile.get('horizon_class')}/"
        f"{profile.get('market_regime')}: setup={profile.get('setup_type')}, "
        f"state={state}, profile_state_hint={hint}, n={_safe_int(profile.get('sample_count'))}, "
        f"wr={_safe_float(profile.get('win_rate')):.2f}, pf={_safe_float(profile.get('profit_factor')):.2f}, "
        f"pnl={_safe_float(profile.get('net_pnl')):.0f}. "
        "Use as rebuttable profile-state prior only; it is not an action preference or trade command; "
        f"current evidence and invalidation are required.{product_suffix}"
    )


def analyst_signal_calibration_prompt_line(action_value: Mapping[str, Any]) -> str:
    payload = action_value.get("payload") if isinstance(action_value.get("payload"), Mapping) else {}
    signal_calibration = (
        payload.get("signal_calibration")
        if isinstance(payload.get("signal_calibration"), Mapping)
        else action_value.get("signal_calibration")
        if isinstance(action_value.get("signal_calibration"), Mapping)
        else {}
    )
    lane = str(
        signal_calibration.get("source_action_value_lane")
        or payload.get("action_value_lane")
        or action_value.get("action_name")
        or "unknown"
    )
    allowed = ",".join(signal_calibration.get("allowed_effects") or [])
    forbidden = ",".join(signal_calibration.get("forbidden_effects") or [])
    bias = str(signal_calibration.get("calibration_bias") or "neutral_evidence_context")
    source_quality = str(signal_calibration.get("source_quality") or payload.get("amplification_scope_quality") or "unknown")
    sample_count = _safe_int(action_value.get("sample_count"))
    confidence = _safe_float(action_value.get("confidence_score"))
    product_view = (
        action_value.get("product_learning_calibration_view")
        if isinstance(action_value.get("product_learning_calibration_view"), Mapping)
        else {}
    )
    entry_view = (
        product_view.get("entry_quality_calibration")
        if isinstance(product_view.get("entry_quality_calibration"), Mapping)
        else {}
    )
    entry_suffix = ""
    if entry_view:
        entry_suffix = (
            f" entry_trigger_key={entry_view.get('trigger_key')}, "
            f"entry_quality={entry_view.get('entry_quality_verdict')}, "
            f"trigger_quality={entry_view.get('trigger_quality_verdict')}, "
            f"confirmation={entry_view.get('trigger_confirmation_adjustment')}, "
            f"support={_safe_float(entry_view.get('support_weight')):.2f}, "
            f"penalty={_safe_float(entry_view.get('penalty_weight')):.2f}."
        )
    return (
        f"{action_value.get('ticker')}/{action_value.get('side')}/"
        f"{action_value.get('horizon_class')}/{action_value.get('market_regime')}: "
        f"setup={action_value.get('setup_type')}, action={action_value.get('action_name')}, lane={lane}, "
        f"signal_calibration_bias={bias}, source_quality={source_quality}, n={sample_count}, "
        f"conf={confidence:.2f}, analyst_allowed={allowed or 'evidence_quality_calibration'}, "
        f"analyst_forbidden={forbidden or 'trade_authority,lots,margin_ratio,direction_override'}."
        f"{entry_suffix} "
        "Analyst use only: calibrate evidence quality, setup reliability, and fresh-data questions; "
        "no trade authority/lots/margin/direction override; "
        "do not infer trade authority, lots, margin, target position, direction override, PM decision, or Trader execution."
    )


def _analyst_signal_calibration_view(signal_calibration: Mapping[str, Any]) -> Dict[str, Any]:
    allowed_keys = {
        "contract_version",
        "source_action_value_contract",
        "source_action_value_lane",
        "source_quality",
        "reward_source",
        "calibration_bias",
        "usable_by",
        "allowed_effects",
        "forbidden_effects",
        "current_data_must_dominate",
    }
    return {
        key: signal_calibration.get(key)
        for key in allowed_keys
        if key in signal_calibration
    }


def _product_learning_key_from_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
    key = payload.get("product_learning_performance_key")
    if isinstance(key, Mapping):
        return key
    key = value.get("product_learning_performance_key")
    if isinstance(key, Mapping):
        return key
    return {}


def _bounded_analyst_weight(value: Any) -> float:
    return round(min(1.0, max(0.0, _safe_float(value))), 4)


def _analyst_safe_trigger_key(value: Any) -> str:
    token = _clean_token(value, "unknown_trigger")[:120]
    canonical_tokens = {
        _clean_token(trigger, "")[:120]
        for trigger in CANONICAL_ENTRY_TRIGGERS
    }
    if token in canonical_tokens:
        return token
    # Unregistered numeric text may encode a historical absolute level, so it
    # cannot cross the analyst-safe boundary.
    if any(ch.isdigit() for ch in token):
        return "unknown_trigger"
    return token


def _entry_quality_calibration_view(value: Mapping[str, Any]) -> Dict[str, Any]:
    key = _product_learning_key_from_payload(value)
    payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
    outcome = key.get("entry_quality_outcome")
    if not isinstance(outcome, Mapping):
        outcome = payload.get("entry_quality_outcome")
    if not isinstance(outcome, Mapping):
        return {}
    if outcome.get("contract_version") != "agentquant.entry_quality_outcome.v1":
        return {}
    return {
        "contract_version": "agentquant.entry_quality_calibration_view.v1",
        "trigger_key": _analyst_safe_trigger_key(
            outcome.get("trigger_key") or key.get("trigger_key")
        ),
        "entry_quality_verdict": _clean_token(
            outcome.get("entry_quality_verdict"), "entry_outcome_neutral"
        ),
        "trigger_quality_verdict": _clean_token(
            outcome.get("trigger_quality_verdict"), "trigger_outcome_neutral"
        ),
        "trigger_confirmation_adjustment": _clean_token(
            outcome.get("trigger_confirmation_adjustment"), "neutral"
        ),
        "support_weight": _bounded_analyst_weight(outcome.get("support_weight")),
        "penalty_weight": _bounded_analyst_weight(outcome.get("penalty_weight")),
        "not_trade_authority": True,
        "future_only": True,
    }


def compact_product_learning_performance_key_for_analyst(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Return an analyst-safe view of the product learning performance key.

    The raw key may contain PM contract field names such as opportunity_rank or
    authority_type. Analysts may use only a historical evidence-calibration view,
    so this function deliberately renames PM facts and drops trade-authority
    fields.
    """
    key = _product_learning_key_from_payload(value)
    if not key:
        return {}
    view = {
        "contract_version": "agentquant.product_learning_calibration_view.v1",
        "source_contract_version": key.get("contract_version"),
        "ticker": key.get("ticker"),
        "side": key.get("side"),
        "horizon_class": key.get("horizon_class"),
        "market_regime": key.get("market_regime"),
        "setup_type": key.get("setup_type"),
        "action_name": key.get("action_name"),
        "trigger_key": _analyst_safe_trigger_key(key.get("trigger_key")),
        "evidence_combo": key.get("evidence_combo"),
        "outcome_label": key.get("outcome_label"),
        "reward_source": key.get("reward_source"),
        "not_trade_authority": True,
        "future_only": True,
        "analyst_usage_boundary": (
            "entry_evidence_calibration_only_no_trade_authority_no_lots_no_margin_no_pm_rank"
        ),
    }
    entry_quality = _entry_quality_calibration_view(value)
    if entry_quality:
        view["entry_quality_calibration"] = entry_quality
    return view


def compact_profile_for_trace(
    profile: Mapping[str, Any],
    *,
    include_entry_calibration: bool = True,
) -> Dict[str, Any]:
    profile_state_hint = (
        profile.get("profile_state_hint")
        or "profile_observe"
    )
    product_view = compact_product_learning_performance_key_for_analyst(profile)
    if product_view and not include_entry_calibration:
        product_view = dict(product_view)
        product_view.pop("entry_quality_calibration", None)
        product_view.pop("trigger_key", None)
    return {
        "scope_key": profile.get("scope_key"),
        "ticker": profile.get("ticker"),
        "side": profile.get("side"),
        "horizon_class": profile.get("horizon_class"),
        "market_regime": profile.get("market_regime"),
        "setup_type": profile.get("setup_type"),
        "data_combo": _compact_text(profile.get("data_combo"), 90),
        "lifecycle_state": profile.get("lifecycle_state"),
        "profile_state_hint": profile_state_hint,
        "profile_state_hint_boundary": "profile lifecycle hint only; not an action preference or trade command",
        "sample_count": profile.get("sample_count"),
        "trade_count": profile.get("trade_count"),
        "win_rate": profile.get("win_rate"),
        "profit_factor": profile.get("profit_factor"),
        "net_pnl": profile.get("net_pnl"),
        "confidence_score": profile.get("confidence_score"),
        "max_position_impact": profile.get("max_position_impact"),
        "valid_until": profile.get("valid_until"),
        "product_learning_calibration_view": product_view,
    }


def compact_action_value_for_analyst_trace(action_value: Mapping[str, Any]) -> Dict[str, Any]:
    payload = action_value.get("payload") if isinstance(action_value.get("payload"), Mapping) else {}
    signal_calibration = payload.get("signal_calibration")
    if not isinstance(signal_calibration, Mapping):
        signal_calibration = {}
    analyst_signal_calibration = _analyst_signal_calibration_view(signal_calibration)
    product_view = compact_product_learning_performance_key_for_analyst(action_value)
    return {
        "ticker": action_value.get("ticker"),
        "side": action_value.get("side"),
        "horizon_class": action_value.get("horizon_class"),
        "market_regime": action_value.get("market_regime"),
        "setup_type": action_value.get("setup_type"),
        "data_combo": _compact_text(action_value.get("data_combo"), 90),
        "action_name": action_value.get("action_name"),
        "sample_count": action_value.get("sample_count"),
        "confidence_score": action_value.get("confidence_score"),
        "valid_until": action_value.get("valid_until"),
        "research_output_contract_version": payload.get("research_output_contract_version"),
        "action_value_lane": (
            analyst_signal_calibration.get("source_action_value_lane")
            or payload.get("action_value_lane")
            or action_value.get("action_name")
        ),
        "signal_calibration": analyst_signal_calibration,
        "product_learning_calibration_view": product_view,
        "analyst_usage_boundary": (
            "signal_calibration_only_no_trade_authority_no_lots_no_margin_no_direction_override"
        ),
    }




