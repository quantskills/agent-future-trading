from __future__ import annotations

"""Signal-fusion helpers shared by PM, reviewer tests, and future refactors."""

from datetime import date, datetime
from typing import Any, Iterable, Mapping

from tools.common.evidence_fusion_semantics import build_pm_fusion_diagnostics
from tools.common.final_action_semantics import (
    ACTION_FAMILY_EXECUTION,
    ACTION_FAMILY_OPEN_ADD_NEW_RISK,
    validate_action_preference_family_consistency,
)


ANALYST_ORDER = ("technical", "fundamental", "commodity_news")

def normalize_analyst_name(value: Any) -> str:
    text = str(value or "")
    return "commodity_news" if text == "company_news" else text


def signal_to_text(value: Any) -> str:
    return str(value.value) if hasattr(value, "value") else str(value or "Neutral")


def analyst_signal_combo(analyst_signals: Iterable[Any]) -> tuple[str, str, str]:
    signals = {name: "Neutral" for name in ANALYST_ORDER}
    for signal in analyst_signals or []:
        agent_name = normalize_analyst_name(getattr(signal, "agent_name", ""))
        if agent_name in signals:
            signals[agent_name] = signal_to_text(getattr(signal, "signal", "Neutral"))
    return (signals["technical"], signals["fundamental"], signals["commodity_news"])


def resolve_decision_horizon(analyst_signals: Iterable[Any], target_lots: int) -> str:
    if target_lots == 0:
        return "flat"
    target_signal = "Bullish" if target_lots > 0 else "Bearish"
    horizons = []
    for signal in analyst_signals or []:
        if signal_to_text(getattr(signal, "signal", "Neutral")) != target_signal:
            continue
        horizons.append(
            str(
                getattr(signal, "analyst_horizon", None)
                or getattr(signal, "horizon_class", None)
                or "unknown"
            )
        )
    if "medium" in horizons:
        return "medium"
    if "event_short" in horizons:
        return "event_short"
    if "short" in horizons:
        return "short"
    return "unknown"


def build_horizon_scope(
    analyst_signals: Iterable[Any],
    *,
    decision_horizon: str,
    execution_horizon: str = "short",
    validation_horizon: str | None = None,
) -> dict[str, Any]:
    analyst_horizons: dict[str, dict[str, Any]] = {}
    for signal in analyst_signals or []:
        agent_name = normalize_analyst_name(getattr(signal, "agent_name", ""))
        if not agent_name:
            continue
        analyst_horizons[agent_name] = {
            "analyst_horizon": str(getattr(signal, "analyst_horizon", "") or getattr(signal, "horizon_class", "") or "unknown"),
            "horizon_class": str(getattr(signal, "horizon_class", "") or "unknown"),
            "expected_horizon_days": int(getattr(signal, "expected_horizon_days", 0) or 0),
        }
    return {
        "analyst_horizons": analyst_horizons,
        "decision_horizon": decision_horizon or "unknown",
        "execution_horizon": execution_horizon or "short",
        "validation_horizon": validation_horizon or decision_horizon or "unknown",
    }


def dominant_business_quality(analyst_payloads: Iterable[Mapping[str, Any]], target_signal: str) -> float:
    scores = []
    for payload in analyst_payloads or []:
        if not isinstance(payload, Mapping):
            continue
        if str(payload.get("signal") or "") == target_signal:
            try:
                scores.append(float(payload.get("business_quality_score") or 0.0))
            except Exception:
                scores.append(0.0)
    return max(scores, default=0.0)


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


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple) or isinstance(value, set):
        return list(value)
    return [value]


def _signal_side(signal: Any) -> str:
    text = signal_to_text(getattr(signal, "signal", "Neutral")).lower()
    if text == "bullish":
        return "long"
    if text == "bearish":
        return "short"
    return "flat"


def _contract_value(signal: Any, key: str, default: Any = None) -> Any:
    metadata = getattr(signal, "metadata", None)
    contract = None
    if isinstance(metadata, Mapping):
        contract = metadata.get("action_evidence_contract")
        if not isinstance(contract, Mapping):
            research_contract = metadata.get("trade_research_contract")
            if isinstance(research_contract, Mapping):
                contract = research_contract.get("action_evidence_contract")
        if not isinstance(contract, Mapping):
            contract = metadata.get("trade_research_contract")
    if not isinstance(contract, Mapping):
        contract = getattr(signal, "next_round_memory_contract", None)
    if not isinstance(contract, Mapping):
        contract = getattr(signal, "research_contract", None)
    if isinstance(contract, Mapping):
        return contract.get(key, default)
    return default


def _opportunity_state(signal: Any) -> str:
    state = (
        getattr(signal, "opportunity_state", None)
        or _contract_value(signal, "opportunity_state")
        or "watch_for_trigger"
    )
    return str(state or "watch_for_trigger").strip().lower() or "watch_for_trigger"


def _signal_bool(signal: Any, attr: str, contract_key: str | None = None) -> bool:
    key = contract_key or attr
    contract_value = _contract_value(signal, key, None)
    if contract_value is not None:
        return bool(contract_value)
    if hasattr(signal, attr):
        value = getattr(signal, attr)
        if value is not None:
            return bool(value)
    return False


def _signal_text(signal: Any, attr: str, contract_key: str | None = None) -> str:
    value = _contract_value(signal, contract_key or attr, "")
    if value:
        return str(value)
    value = getattr(signal, attr, None)
    return str(value or "")


def _has_invalidation(signal: Any) -> bool:
    contract_present = _contract_value(signal, "invalidation_present", None)
    if contract_present is not None:
        return bool(contract_present)
    contract_condition = _contract_value(signal, "invalidation_condition", "")
    if contract_condition:
        text = str(contract_condition or "").strip().lower()
        if text and not (text.endswith("_trigger") or text.endswith("_anchor")):
            return True
    invalidation_level = getattr(signal, "invalidation_level", None)
    if invalidation_level is not None:
        return True
    fields = [
        getattr(signal, "invalidation_condition", ""),
        getattr(signal, "would_change_view_if", ""),
    ]
    text = " ".join(str(item or "") for item in fields).strip().lower()
    if not text:
        return False
    generic_terms = {
        "wait_for_trigger",
        "technical_price_trigger",
        "fundamental_anchor",
        "news_event_trigger",
        "direction_anchor",
        "initial_or_rebalance",
        "unknown",
        "none",
        "n/a",
    }
    if text in generic_terms or text.endswith("_trigger") or text.endswith("_anchor"):
        return False
    return bool(
        any(
            token in text
            for token in (
                "invalid",
                "fails",
                "failure",
                "below",
                "above",
                "close",
                "stop",
                "exit",
                "reduce",
                "contradict",
                "conflict",
                "reversal",
                "reverses",
                "loses",
                "price fails",
                "volume fails",
                "regime flips",
                "basis",
                "inventory",
                "catalyst expires",
                "失效",
                "止损",
                "跌破",
                "突破失败",
                "反向",
            )
        )
    )


def _has_entry_setup(signal: Any) -> bool:
    fields = [
        getattr(signal, "entry_trigger", ""),
        getattr(signal, "trade_setup", ""),
        _contract_value(signal, "entry_trigger", ""),
        _contract_value(signal, "pm_action_condition", ""),
    ]
    text = " ".join(str(item or "") for item in fields).lower()
    if not text.strip():
        return False
    weak_terms = {
        "unknown",
        "none",
        "n/a",
        "wait",
        "observe only",
        "tracking only",
        "wait_for_trigger",
        "technical_price_trigger",
        "fundamental_anchor",
        "news_event_trigger",
    }
    stripped = text.strip()
    if stripped in weak_terms or stripped.endswith("_trigger") or stripped.endswith("_anchor"):
        return False
    return any(
        token in text
        for token in [
            "trigger",
            "break",
            "confirm",
            "entry",
            "enter",
            "open",
            "probe",
            "vwap",
            "volume",
            "pullback",
            "hold above",
            "holding above",
            "hold below",
            "holding below",
            "stabilization",
            "stabilize",
            "support",
            "resistance",
            "basis remains",
            "backwardation",
            "contango",
            "反证",
            "触发",
            "确认",
            "入场",
            "企稳",
            "站上",
            "跌破",
            "支撑",
            "压力",
        ]
    )


def _setup_quality_ok(signal: Any) -> bool:
    value = _contract_value(signal, "setup_quality_ok", None)
    if value is not None:
        return bool(value)
    return _setup_quality(signal) >= 0.42


def _source_analysts(signal: Any) -> list[str]:
    agent = normalize_analyst_name(getattr(signal, "agent_name", ""))
    return [agent] if agent else []


def _setup_quality(signal: Any) -> float:
    try:
        return max(0.0, min(1.0, float(getattr(signal, "setup_quality_score", 0.0) or 0.0)))
    except Exception:
        return 0.0


def _setup_quality_notes(signal: Any) -> list[str]:
    value = getattr(signal, "setup_quality_notes", [])
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    try:
        return [str(item) for item in value if str(item).strip()]
    except Exception:
        return [str(value)]


def _market_regime_from_signals(analyst_signals: Iterable[Any]) -> str:
    for signal in analyst_signals or []:
        regime = str(getattr(signal, "market_regime", "") or getattr(signal, "trend_stage", "") or "").strip()
        if regime:
            return regime
    return "unknown"


def _agent_name(signal: Any) -> str:
    return normalize_analyst_name(getattr(signal, "agent_name", ""))


def _technical_opposes_side(signals: Iterable[Any], side: str, min_confidence: float) -> bool:
    opposite = "short" if side == "long" else "long" if side == "short" else ""
    if not opposite:
        return False
    for signal in signals or []:
        if _agent_name(signal) != "technical":
            continue
        if _signal_side(signal) != opposite:
            continue
        if _safe_float(getattr(signal, "confidence", 0.0), 0.0) >= min_confidence:
            return True
    return False


def _score_weight(config: Mapping[str, Any], key: str, default: float) -> float:
    weights = config.get("score_component_weights") if isinstance(config.get("score_component_weights"), Mapping) else {}
    return _safe_float(weights.get(key), default)


def _score_cap(config: Mapping[str, Any], key: str, default: float) -> float:
    caps = config.get("score_component_caps") if isinstance(config.get("score_component_caps"), Mapping) else {}
    return _safe_float(caps.get(key), default)


def _clean_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _row_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _row_value(row: Mapping[str, Any], payload: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except Exception:
        return None


def _bounded(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _mapping_weight(
    config: Mapping[str, Any],
    mapping_key: str,
    value: Any,
    default_weights: Mapping[str, float],
    default: float,
) -> float:
    text = _clean_key(value)
    weights = config.get(mapping_key) if isinstance(config.get(mapping_key), Mapping) else {}
    if text in weights:
        return _safe_float(weights.get(text), default)
    return _safe_float(default_weights.get(text), default)


def _learning_recency_weight(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    decision_date: Any,
    config: Mapping[str, Any],
) -> float:
    decision = _parse_date(decision_date)
    source_date = _parse_date(
        _row_value(
            row,
            payload,
            "last_sample_date",
            "sample_last_trading_date",
            "source_trading_date",
            "trading_date",
            "updated_at",
            "created_at",
        )
    )
    if not decision or not source_date:
        return _bounded(_safe_float(config.get("learning_unknown_recency_weight"), 0.65), 0.0, 1.0)
    days = (decision - source_date).days
    if days < 0:
        return 0.0
    half_life = max(1.0, _safe_float(config.get("learning_recency_half_life_days"), 12.0))
    floor = _bounded(_safe_float(config.get("learning_recency_floor"), 0.20), 0.0, 1.0)
    return max(floor, 0.5 ** (days / half_life))


_POSITIVE_ACTION_PREFERENCES = {
    "positive_candidate_open",
    "positive_candidate_hold",
    "positive_candidate_exit",
    "positive_candidate_execution",
}
_NEGATIVE_ACTION_PREFERENCES = {
    "negative_revalidate",
    "negative_hold_revalidate",
    "tail_loss_protect",
}


def _action_value_learning_summary(
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    side: str,
    config: Mapping[str, Any],
    decision_date: Any = None,
    decision_lifecycle: str = "open_add_new_risk",
) -> dict[str, Any]:
    default_scope_weights = {
        "exact_real_state": 1.0,
        "partial_real_state": 0.65,
        "similar_sql_prior": 0.35,
        "observation_only": 0.20,
        "counterfactual_prior": 0.20,
        "unknown": 0.25,
    }
    default_source_weights = {
        "trade_episode": 1.15,
        "episode_trade": 1.15,
        "real_trade": 1.0,
        "trade_pair": 0.85,
        "counterfactual_prior": 0.35,
        "observation_only": 0.25,
        "unknown": 0.45,
    }
    reward_unit = max(1.0, _safe_float(config.get("learning_reward_unit"), 4000.0))
    full_sample_count = max(1, _safe_int(config.get("learning_full_weight_sample_count"), 3))
    tail_loss_threshold = _safe_float(config.get("tail_loss_reward_threshold"), -1000.0)
    positive_signal = 0.0
    negative_signal = 0.0
    execution_signal = 0.0
    recent_tail_loss_signal = 0.0
    entry_quality_loss_signal = 0.0
    trigger_quality_positive_signal = 0.0
    trigger_quality_loss_signal = 0.0
    positive_count = 0
    negative_count = 0
    exact_real_count = 0
    episode_count = 0
    strongest_positive: dict[str, Any] = {}
    strongest_negative: dict[str, Any] = {}
    used_lanes: set[str] = set()
    ignored_lanes: set[str] = set()
    lifecycle = _clean_key(decision_lifecycle) or "open_add_new_risk"
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        payload = _row_payload(row)
        semantic_validation = validate_action_preference_family_consistency({**dict(row), "payload": payload})
        if not semantic_validation.get("ok"):
            ignored_lanes.add("semantic_contract_error")
            continue
        product_key = payload.get("product_learning_performance_key")
        if not isinstance(product_key, Mapping):
            product_key = {}
        entry_quality_outcome = product_key.get("entry_quality_outcome")
        if not isinstance(entry_quality_outcome, Mapping):
            entry_quality_outcome = payload.get("entry_quality_outcome")
        if not isinstance(entry_quality_outcome, Mapping):
            entry_quality_outcome = {}
        consumer_scope = _clean_key(
            _row_value(row, payload, "consumer_scope", "learning_consumer_scope", default="pm_learning")
        )
        if consumer_scope and consumer_scope != "pm_learning":
            continue
        row_side = _clean_key(_row_value(row, payload, "side", default="*"))
        if row_side not in {side, "*", "both", "any"}:
            continue
        action_preference = _clean_key(
            _row_value(row, payload, "action_preference", "policy_action", default="")
        )
        if not action_preference:
            continue
        family = _clean_key(_row_value(row, payload, "canonical_action_family", "source_canonical_action_family", default=""))
        lane = _clean_key(_row_value(row, payload, "learning_lane", "action_value_lane", default=""))
        lane_is_execution_profile = lifecycle == "open_add_new_risk" and family == ACTION_FAMILY_EXECUTION
        if lifecycle == "open_add_new_risk" and family != ACTION_FAMILY_OPEN_ADD_NEW_RISK and not lane_is_execution_profile:
            ignored_lanes.add(lane or family or "unknown")
            continue
        scope = _clean_key(
            _row_value(row, payload, "amplification_scope_quality", "source_quality", "evidence_scope", default="unknown")
        )
        reward_source = _clean_key(
            _row_value(row, payload, "reward_source", "sample_source", default="unknown")
        )
        scope_weight = _mapping_weight(
            config,
            "learning_scope_weights",
            scope,
            default_scope_weights,
            default_scope_weights["unknown"],
        )
        source_weight = _mapping_weight(
            config,
            "learning_reward_source_weights",
            reward_source,
            default_source_weights,
            default_source_weights["unknown"],
        )
        recency_weight = _learning_recency_weight(
            row,
            payload,
            decision_date=decision_date,
            config=config,
        )
        sample_count = max(
            1,
            _safe_int(
                _row_value(
                    row,
                    payload,
                    "sample_count",
                    "real_trade_reward_count",
                    "episode_trade_reward_count",
                    "exact_state_real_trade_sample_count",
                    default=1,
                ),
                1,
            ),
        )
        sample_weight = _bounded(sample_count / full_sample_count, 0.35, 1.0)
        confidence_weight = _bounded(
            _safe_float(_row_value(row, payload, "confidence_score", "confidence", default=0.5), 0.5),
            0.25,
            1.0,
        )
        reward_mean = _safe_float(_row_value(row, payload, "reward_mean", "avg_reward", default=0.0), 0.0)
        reward_sum = _safe_float(_row_value(row, payload, "reward_sum", "total_reward", default=0.0), 0.0)
        worst_reward = _safe_float(_row_value(row, payload, "worst_reward", "min_reward", default=reward_mean), reward_mean)
        reward_magnitude = max(abs(reward_mean), abs(reward_sum) / max(1, sample_count), abs(worst_reward))
        magnitude_weight = _bounded(reward_magnitude / reward_unit, 0.25, 1.0)
        strength = scope_weight * source_weight * recency_weight * sample_weight * confidence_weight * magnitude_weight
        tail_loss_count = _safe_int(_row_value(row, payload, "tail_loss_count", default=0), 0)
        is_tail_loss = (
            action_preference == "tail_loss_protect"
            or tail_loss_count > 0
            or worst_reward <= tail_loss_threshold
            or reward_mean <= tail_loss_threshold
        )
        is_positive = action_preference in _POSITIVE_ACTION_PREFERENCES
        is_negative = action_preference in _NEGATIVE_ACTION_PREFERENCES or action_preference.startswith("negative")
        if lane_is_execution_profile:
            ignored_lanes.add("execution")
            if action_preference == "positive_candidate_execution":
                execution_signal += strength
            elif is_negative:
                execution_signal -= strength * (1.20 if is_tail_loss else 1.0)
            if entry_quality_outcome:
                entry_penalty_weight = _safe_float(entry_quality_outcome.get("penalty_weight"), 0.0)
                trigger_support_weight = _safe_float(entry_quality_outcome.get("support_weight"), 0.0)
                if bool(entry_quality_outcome.get("positive_entry_episode")):
                    trigger_quality_positive_signal += strength * max(0.25, trigger_support_weight)
                if bool(entry_quality_outcome.get("loss_episode")):
                    trigger_quality_loss_signal += strength * max(0.35, entry_penalty_weight)
            continue
        used_lanes.add(lane or "unknown")
        if scope == "exact_real_state":
            exact_real_count += 1
        if reward_source in {"trade_episode", "episode_trade"}:
            episode_count += 1
        summary_ref = {
            "action_preference": action_preference,
            "lane": lane or "unknown",
            "scope": scope or "unknown",
            "reward_source": reward_source or "unknown",
            "sample_count": sample_count,
            "reward_mean": round(reward_mean, 4),
            "weight": round(strength, 4),
        }
        if is_positive:
            positive_count += 1
            positive_signal += strength
            if not strongest_positive or strength > _safe_float(strongest_positive.get("weight"), 0.0):
                strongest_positive = summary_ref
        if is_negative:
            negative_count += 1
            negative_strength = strength * (1.30 if is_tail_loss else 1.0)
            if action_preference == "negative_hold_revalidate":
                negative_strength *= 0.75
            negative_signal += negative_strength
            if is_tail_loss:
                recent_tail_loss_signal += strength * 1.35
            if not strongest_negative or negative_strength > _safe_float(strongest_negative.get("weight"), 0.0):
                strongest_negative = {
                    **summary_ref,
                    "weight": round(negative_strength, 4),
                    "tail_loss": is_tail_loss,
                }
        if entry_quality_outcome:
            entry_penalty_weight = _safe_float(entry_quality_outcome.get("penalty_weight"), 0.0)
            trigger_support_weight = _safe_float(entry_quality_outcome.get("support_weight"), 0.0)
            if bool(entry_quality_outcome.get("positive_entry_episode")):
                trigger_quality_positive_signal += strength * max(0.25, trigger_support_weight)
            if bool(entry_quality_outcome.get("loss_episode")):
                entry_quality_loss_signal += strength * max(0.35, entry_penalty_weight)
            if bool(entry_quality_outcome.get("tail_loss_episode")):
                trigger_quality_loss_signal += strength * max(0.55, entry_penalty_weight)
                recent_tail_loss_signal += strength * 0.35
    positive_signal = _bounded(positive_signal)
    negative_signal = _bounded(negative_signal)
    execution_signal = _bounded(execution_signal, -1.0, 1.0)
    recent_tail_loss_signal = _bounded(recent_tail_loss_signal)
    entry_quality_loss_signal = _bounded(entry_quality_loss_signal)
    trigger_quality_positive_signal = _bounded(trigger_quality_positive_signal)
    trigger_quality_loss_signal = _bounded(trigger_quality_loss_signal)
    net_trigger_quality_loss_signal = _bounded(
        max(0.0, trigger_quality_loss_signal - trigger_quality_positive_signal * 0.50)
    )
    return {
        "positive_signal": positive_signal,
        "negative_signal": negative_signal,
        "execution_profile_signal": execution_signal,
        "recent_tail_loss_signal": recent_tail_loss_signal,
        "entry_quality_loss_signal": entry_quality_loss_signal,
        "trigger_quality_positive_signal": trigger_quality_positive_signal,
        "trigger_quality_loss_signal": trigger_quality_loss_signal,
        "net_trigger_quality_loss_signal": net_trigger_quality_loss_signal,
        "positive_count": positive_count,
        "negative_count": negative_count,
        "exact_real_count": exact_real_count,
        "episode_count": episode_count,
        "strongest_positive": strongest_positive,
        "strongest_negative": strongest_negative,
        "decision_lifecycle": lifecycle,
        "used_lanes": sorted(used_lanes),
        "ignored_lanes": sorted(ignored_lanes),
        "execution_profile_signal_direct_to_rank": False,
    }


def _capital_allocation_reason(*, row: Mapping[str, Any], deployable_threshold: float, tradeable_threshold: float) -> str:
    state = str(row.get("final_state") or "unknown")
    score = _safe_float(row.get("opportunity_score", row.get("score")), 0.0)
    failures = {str(item) for item in (row.get("gating_failures") or [])}
    if state == "tradeable_candidate" and score >= deployable_threshold:
        return "ranked_deployable_candidate_with_complete_current_evidence"
    if state == "probe_candidate":
        return "ranked_probe_candidate_selected_only_if_pm_capital_queue_allows"
    if state == "watch_for_trigger" and bool(row.get("conditional_monitor_candidate")):
        return "monitorable_conditional_candidate_selected_only_if_pm_capital_queue_allows"
    if "missing_invalidation_boundary" in failures:
        return "not_allocated_missing_invalidation_boundary"
    if "critical_data_gap" in failures:
        return "not_allocated_critical_data_gap"
    if "same_scope_alpha_setup_capped_or_rejected" in failures:
        return "rank_lowered_by_same_scope_learning"
    if score < tradeable_threshold:
        return "rank_lowered_by_insufficient_opportunity_score"
    return "ranked_candidate_requires_pm_final_contract_authority"


def _count_items(value: Any) -> int:
    if isinstance(value, list):
        return len([item for item in value if item])
    if isinstance(value, Mapping):
        return len(value)
    if value in (None, "", False):
        return 0
    return 1


def _candidate_layer_hint(final_state: str) -> str:
    state = str(final_state or "").strip().lower()
    if state == "tradeable_candidate":
        return "tradeable_candidate"
    if state == "probe_candidate":
        return "exploration_probe_candidate"
    if state == "watch_for_trigger":
        return "watch_for_trigger_candidate"
    return "not_candidate"


def _candidate_quality_components(
    *,
    opportunity_score: float,
    score_components: Mapping[str, Any],
    gating_failures: Iterable[Any],
    setup_quality: float,
    trigger_valid: bool,
    invalidation_count: int,
) -> dict[str, float]:
    components = score_components if isinstance(score_components, Mapping) else {}
    trigger_quality = 0.04 if trigger_valid else 0.0
    invalidation_quality = 0.04 if invalidation_count > 0 else 0.0
    product_profile_support = (
        _safe_float(components.get("product_profile_alignment"), 0.0)
        + _safe_float(components.get("alpha_profile_adjustment"), 0.0)
        + _safe_float(components.get("positive_learning"), 0.0)
    )
    conflict_penalty = (
        abs(min(0.0, _safe_float(components.get("fusion_conflict_adjustment"), 0.0)))
        + abs(min(0.0, _safe_float(components.get("negative_learning"), 0.0)))
        + 0.02 * _count_items(list(gating_failures or []))
    )
    return {
        "opportunity_score": round(_safe_float(opportunity_score), 6),
        "setup_quality": round(_safe_float(setup_quality), 6),
        "trigger_quality": round(trigger_quality, 6),
        "invalidation_quality": round(invalidation_quality, 6),
        "product_profile_support": round(product_profile_support, 6),
        "conflict_penalty": round(-conflict_penalty, 6),
    }


def _candidate_quality_score(components: Mapping[str, Any]) -> float:
    return round(_bounded(sum(_safe_float(value) for value in (components or {}).values())), 6)


def _learning_adjustment_summary(
    *,
    policy_counts: Mapping[str, int],
    action_value_learning: Mapping[str, Any],
    alpha_profile_bonus: float,
    alpha_profile_penalty: float,
    best_alpha_profile: Mapping[str, Any],
    capped_profiles: list[Mapping[str, Any]],
) -> dict[str, Any]:
    net_adjustment = alpha_profile_bonus - alpha_profile_penalty
    return {
        "positive_policy_count": int(policy_counts.get("positive", 0) or 0),
        "negative_policy_count": int(policy_counts.get("negative", 0) or 0),
        "positive_action_value_count": int(action_value_learning.get("positive_count", 0) or 0),
        "negative_action_value_count": int(action_value_learning.get("negative_count", 0) or 0),
        "exact_real_action_value_count": int(action_value_learning.get("exact_real_count", 0) or 0),
        "episode_action_value_count": int(action_value_learning.get("episode_count", 0) or 0),
        "positive_learning_signal": round(_safe_float(action_value_learning.get("positive_signal"), 0.0), 4),
        "negative_learning_signal": round(_safe_float(action_value_learning.get("negative_signal"), 0.0), 4),
        "execution_profile_learning_signal": round(
            _safe_float(action_value_learning.get("execution_profile_signal"), 0.0),
            4,
        ),
        "recent_tail_loss_signal": round(
            _safe_float(action_value_learning.get("recent_tail_loss_signal"), 0.0),
            4,
        ),
        "entry_quality_loss_signal": round(
            _safe_float(action_value_learning.get("entry_quality_loss_signal"), 0.0),
            4,
        ),
        "trigger_quality_positive_signal": round(
            _safe_float(action_value_learning.get("trigger_quality_positive_signal"), 0.0),
            4,
        ),
        "trigger_quality_loss_signal": round(
            _safe_float(action_value_learning.get("trigger_quality_loss_signal"), 0.0),
            4,
        ),
        "net_trigger_quality_loss_signal": round(
            _safe_float(action_value_learning.get("net_trigger_quality_loss_signal"), 0.0),
            4,
        ),
        "strongest_positive_action_value": action_value_learning.get("strongest_positive") or {},
        "strongest_negative_action_value": action_value_learning.get("strongest_negative") or {},
        "alpha_setup_score_adjustment": round(net_adjustment, 4),
        "best_profile_state": best_alpha_profile.get("lifecycle_state") if best_alpha_profile else None,
        "best_profile_scope_key": best_alpha_profile.get("scope_key") if best_alpha_profile else None,
        "capped_or_rejected_profile_count": len(capped_profiles),
        "effect": (
            "boosted"
            if net_adjustment > 0 or _safe_float(action_value_learning.get("positive_signal"), 0.0) > _safe_float(action_value_learning.get("negative_signal"), 0.0)
            else "penalized"
            if net_adjustment < 0 or _safe_float(action_value_learning.get("negative_signal"), 0.0) > 0
            else "neutral"
        ),
        "not_trade_authority": True,
    }


def build_opportunity_scorecard(
    *,
    ticker: str,
    analyst_signals: Iterable[Any],
    market_confirmation: Mapping[str, Any] | None = None,
    data_quality_summary: Mapping[str, Any] | None = None,
    adaptive_policy_state: Iterable[Mapping[str, Any]] | None = None,
    alpha_setup_profiles: Iterable[Mapping[str, Any]] | None = None,
    alpha_setup_action_values: Iterable[Mapping[str, Any]] | None = None,
    signal_collection_contract: Mapping[str, Any] | None = None,
    decision_date: Any = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic opportunity scorecard for PM and Researcher.

    It is not a trading rule or product preference. It is an auditable summary of
    current-day evidence, learning support, execution/data penalties, and the
    resulting opportunity state per side.
    """
    signals = list(analyst_signals or [])
    cfg = config or {}
    deployable_threshold = _safe_float(cfg.get("deployable_threshold"), 0.72)
    tradeable_threshold = _safe_float(cfg.get("tradeable_threshold"), 0.58)
    weak_confirmation_threshold = _safe_float(cfg.get("weak_confirmation_threshold"), 0.45)
    min_deployable_setup_quality = _safe_float(cfg.get("min_deployable_setup_quality"), 0.72)
    min_tradeable_candidate_setup_quality = _safe_float(cfg.get("min_tradeable_candidate_setup_quality"), 0.55)
    single_tradeable_candidate_setup_confirmation_score = _safe_float(
        cfg.get("single_tradeable_candidate_setup_confirmation_score"),
        0.68,
    )
    single_tradeable_candidate_setup_min_business_quality = _safe_float(
        cfg.get("single_tradeable_candidate_setup_min_business_quality"),
        0.60,
    )
    single_tradeable_candidate_setup_min_confidence = _safe_float(
        cfg.get("single_tradeable_candidate_setup_min_confidence"),
        0.42,
    )
    technical_opposition_min_confidence = _safe_float(
        cfg.get("technical_opposition_min_confidence"),
        0.45,
    )
    block_single_setup_on_technical_opposition = bool(
        cfg.get("block_single_setup_on_technical_opposition", True)
    )
    confirmation = market_confirmation or {}
    data_quality = data_quality_summary or {}
    pm_fusion_diagnostics = build_pm_fusion_diagnostics(signal_collection_contract)
    fusion_adjustment = _safe_float(pm_fusion_diagnostics.get("fusion_score_adjustment"), 0.0)
    fusion_consensus = _safe_float(pm_fusion_diagnostics.get("multi_evidence_consensus_score"), 0.0)
    confirmation_score = _safe_float(confirmation.get("confirmation_score"), 0.0)
    confirmation_features = _as_list(confirmation.get("features"))
    confirmation_conflicts = _as_list(confirmation.get("conflicts"))
    data_missing = _as_list(confirmation.get("data_missing")) + _as_list(confirmation.get("data_unavailable"))
    critical_gap = bool(data_quality.get("critical_gap") or data_quality.get("has_critical_gap"))
    fundamental_setup_gap = bool(data_quality.get("fundamental_trade_setup_gap"))
    if len(data_missing) >= 4:
        critical_gap = True

    policy_counts: dict[str, int] = {}
    for row in adaptive_policy_state or []:
        if not isinstance(row, Mapping):
            continue
        action = str(row.get("policy_action") or "").lower()
        ptype = str(row.get("policy_type") or "unknown")
        if action in {"protect", "allow", "probe"} or ptype in {"alpha_promotion", "fast_candidate_alpha"} or ptype.startswith("learning_mechanism:"):
            policy_counts["positive"] = policy_counts.get("positive", 0) + 1
        if action in {"cap", "demote", "block"} or ptype in {"fast_loss_sentinel", "tail_loss_sentinel", "loss_template_policy"}:
            policy_counts["negative"] = policy_counts.get("negative", 0) + 1

    alpha_profiles_by_side: dict[str, list[Mapping[str, Any]]] = {"long": [], "short": []}
    for profile in alpha_setup_profiles or []:
        if not isinstance(profile, Mapping):
            continue
        profile_side = str(profile.get("side") or "*").lower()
        if profile_side in {"long", "short"}:
            alpha_profiles_by_side[profile_side].append(profile)
        elif profile_side == "*":
            alpha_profiles_by_side["long"].append(profile)
            alpha_profiles_by_side["short"].append(profile)

    side_rows: dict[str, dict[str, Any]] = {}
    for side in ("long", "short"):
        supporting = [signal for signal in signals if _signal_side(signal) == side]
        opportunity_state_counts: dict[str, int] = {}
        invalidation_count = 0
        setup_count = 0
        quality_scores: list[float] = []
        setup_quality_scores: list[float] = []
        setup_quality_notes: list[str] = []
        confidence_scores: list[float] = []
        analyst_names: list[str] = []
        trigger_valid_count = 0
        current_trigger_confirmed_count = 0
        setup_quality_ok_count = 0
        source_analysts: list[str] = []
        entry_triggers: list[str] = []
        opportunity_states: list[str] = []
        for signal in supporting:
            state = _opportunity_state(signal)
            opportunity_states.append(state)
            opportunity_state_counts[state] = opportunity_state_counts.get(state, 0) + 1
            if _has_invalidation(signal):
                invalidation_count += 1
            if _has_entry_setup(signal):
                setup_count += 1
            if _signal_bool(signal, "trigger_valid"):
                trigger_valid_count += 1
            if _signal_bool(signal, "current_trigger_confirmed"):
                current_trigger_confirmed_count += 1
            if _setup_quality_ok(signal):
                setup_quality_ok_count += 1
            trigger_text = _signal_text(signal, "entry_trigger")
            if trigger_text:
                entry_triggers.append(trigger_text)
            quality_scores.append(_safe_float(getattr(signal, "business_quality_score", 0.0), 0.0))
            setup_quality_scores.append(_setup_quality(signal))
            setup_quality_notes.extend(_setup_quality_notes(signal))
            confidence_scores.append(_safe_float(getattr(signal, "confidence", 0.0), 0.0))
            analyst_name = normalize_analyst_name(getattr(signal, "agent_name", ""))
            analyst_names.append(analyst_name)
            source_analysts.extend(_source_analysts(signal))

        support_count = len(supporting)
        tradeable_states = (
            opportunity_state_counts.get("tradeable_candidate", 0)
            + opportunity_state_counts.get("probe_candidate", 0)
        )
        avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0.0
        max_quality = max(quality_scores, default=0.0)
        avg_setup_quality = sum(setup_quality_scores) / len(setup_quality_scores) if setup_quality_scores else 0.0
        max_setup_quality = max(setup_quality_scores, default=0.0)
        avg_confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0.0
        action_value_learning = _action_value_learning_summary(
            alpha_setup_action_values,
            side=side,
            config=cfg,
            decision_date=decision_date,
        )
        policy_positive_signal = min(1.0, float(policy_counts.get("positive", 0) or 0)) * 0.55
        policy_negative_signal = min(1.0, float(policy_counts.get("negative", 0) or 0)) * 0.75
        positive_learning_signal = max(
            _safe_float(action_value_learning.get("positive_signal"), 0.0),
            policy_positive_signal,
        )
        negative_learning_signal = max(
            _safe_float(action_value_learning.get("negative_signal"), 0.0),
            policy_negative_signal,
        )
        execution_profile_signal = _safe_float(action_value_learning.get("execution_profile_signal"), 0.0)
        recent_tail_loss_signal = _safe_float(action_value_learning.get("recent_tail_loss_signal"), 0.0)
        entry_quality_loss_signal = _safe_float(action_value_learning.get("entry_quality_loss_signal"), 0.0)
        trigger_quality_positive_signal = _safe_float(action_value_learning.get("trigger_quality_positive_signal"), 0.0)
        trigger_quality_loss_signal = _safe_float(action_value_learning.get("trigger_quality_loss_signal"), 0.0)
        net_trigger_quality_loss_signal = _safe_float(action_value_learning.get("net_trigger_quality_loss_signal"), trigger_quality_loss_signal)
        action_value_signal_present = bool(
            int(action_value_learning.get("positive_count", 0) or 0)
            or int(action_value_learning.get("negative_count", 0) or 0)
            or abs(execution_profile_signal) > 1e-9
            or recent_tail_loss_signal > 1e-9
            or entry_quality_loss_signal > 1e-9
            or trigger_quality_loss_signal > 1e-9
            or trigger_quality_positive_signal > 1e-9
        )
        score_components = {
            "directional_support": min(
                _score_cap(cfg, "directional_support", 0.24),
                _score_weight(cfg, "directional_support_per_signal", 0.08) * support_count,
            ),
            "tradeable_state": min(
                _score_cap(cfg, "tradeable_state", 0.16),
                _score_weight(cfg, "tradeable_state_per_signal", 0.08) * tradeable_states,
            ),
            "business_quality": _score_weight(cfg, "business_quality", 0.12) * max_quality,
            "setup_quality": _score_weight(cfg, "setup_quality", 0.12) * max_setup_quality,
            "confidence": _score_weight(cfg, "confidence", 0.08) * avg_confidence,
            "market_confirmation": _score_weight(cfg, "market_confirmation", 0.12) * confirmation_score,
            "positive_learning": _score_weight(cfg, "positive_learning", 0.12) * positive_learning_signal,
            "negative_learning": -abs(_score_weight(cfg, "negative_learning", 0.16)) * negative_learning_signal,
            "execution_profile_learning": _score_weight(cfg, "execution_profile_learning", 0.10) * execution_profile_signal,
            "recent_tail_loss_penalty": -abs(_score_weight(cfg, "recent_tail_loss_penalty", 0.18)) * recent_tail_loss_signal,
            "entry_quality_loss_penalty": -abs(_score_weight(cfg, "entry_quality_loss_penalty", 0.12)) * entry_quality_loss_signal,
            "trigger_quality_positive_bonus": _score_weight(cfg, "trigger_quality_positive_bonus", 0.08) * trigger_quality_positive_signal,
            "trigger_quality_loss_penalty": -abs(_score_weight(cfg, "trigger_quality_loss_penalty", 0.10)) * net_trigger_quality_loss_signal,
            "fusion_consensus": _score_weight(cfg, "fusion_consensus", 0.08) * fusion_consensus,
            "fusion_conflict_adjustment": fusion_adjustment,
        }
        score = sum(score_components.values())
        side_profiles = alpha_profiles_by_side.get(side, [])
        deployable_profiles = [p for p in side_profiles if str(p.get("lifecycle_state") or "").lower() == "deployable"]
        protected_profiles = [p for p in side_profiles if str(p.get("lifecycle_state") or "").lower() == "protected"]
        watchlist_profiles = [p for p in side_profiles if str(p.get("lifecycle_state") or "").lower() == "watchlist"]
        capped_profiles = [p for p in side_profiles if str(p.get("lifecycle_state") or "").lower() in {"capped", "rejected"}]
        best_alpha_profile = max(
            side_profiles,
            key=lambda p: (
                float(p.get("confidence_score") or 0.0),
                int(p.get("sample_count") or 0),
                float(p.get("net_pnl") or 0.0),
            ),
            default={},
        )
        alpha_profile_bonus = 0.0
        if deployable_profiles:
            alpha_profile_bonus = 0.09
        elif protected_profiles:
            alpha_profile_bonus = 0.06
        elif watchlist_profiles:
            alpha_profile_bonus = 0.025
        if not action_value_signal_present:
            alpha_profile_bonus = min(
                alpha_profile_bonus,
                _safe_float(cfg.get("profile_prior_only_bonus_cap_without_action_value"), 0.015),
            )
        alpha_profile_penalty = 0.08 if capped_profiles else 0.0
        if recent_tail_loss_signal > 0 and alpha_profile_bonus > 0:
            alpha_profile_bonus *= max(0.0, 1.0 - recent_tail_loss_signal)
        elif negative_learning_signal > 0 and alpha_profile_bonus > 0:
            alpha_profile_bonus *= max(0.25, 1.0 - negative_learning_signal * 0.5)
        score_components["alpha_profile_adjustment"] = alpha_profile_bonus - alpha_profile_penalty
        score_components["market_conflict_penalty"] = -abs(
            _score_weight(cfg, "market_conflict_penalty", 0.10)
        ) * min(1.0, len(confirmation_conflicts) / 3.0)
        score_components["critical_data_gap_penalty"] = -abs(
            _score_weight(cfg, "critical_data_gap_penalty", 0.12)
        ) if critical_gap else 0.0
        score_components["fundamental_gap_penalty"] = -abs(
            _score_weight(cfg, "fundamental_gap_penalty", 0.06)
        ) if fundamental_setup_gap else 0.0
        score = sum(score_components.values())
        score = max(0.0, min(1.0, score))

        gating_failures: list[str] = []
        if support_count <= 0:
            gating_failures.append("no_directional_support")
        if tradeable_states <= 0:
            gating_failures.append("no_tradeable_opportunity_state")
        if setup_count <= 0:
            gating_failures.append("missing_entry_setup")
        if invalidation_count <= 0:
            gating_failures.append("missing_invalidation_boundary")
        if max_setup_quality < min_tradeable_candidate_setup_quality and support_count > 0:
            gating_failures.append("weak_entry_setup_quality")
        if side == "long" and "late_long_entry_price_near_upper_range" in setup_quality_notes:
            gating_failures.append("late_long_entry_price_location")
        if side == "short" and "late_short_entry_price_near_lower_range" in setup_quality_notes:
            gating_failures.append("late_short_entry_price_location")
        if critical_gap:
            gating_failures.append("critical_data_gap")
        if fundamental_setup_gap:
            gating_failures.append("fundamental_data_not_enough_for_standalone_setup")
        if confirmation_score < weak_confirmation_threshold:
            gating_failures.append("weak_market_confirmation")
        if capped_profiles:
            gating_failures.append("same_scope_alpha_setup_capped_or_rejected")
        if (
            pm_fusion_diagnostics.get("requires_pm_conflict_resolution")
            and pm_fusion_diagnostics.get("dominant_opposing_evidence_count", 0)
            and score < tradeable_threshold
        ):
            gating_failures.append("dominant_opposing_evidence_requires_pm_resolution")

        technical_opposes = _technical_opposes_side(signals, side, technical_opposition_min_confidence)
        single_tradeable_blocking_failures = {
            "critical_data_gap",
            "fundamental_data_not_enough_for_standalone_setup",
            "missing_entry_setup",
            "missing_invalidation_boundary",
            "weak_entry_setup_quality",
            "late_long_entry_price_location",
            "late_short_entry_price_location",
            "same_scope_alpha_setup_capped_or_rejected",
        }
        single_tradeable_candidate_setup_confirmed = bool(
            tradeable_states > 0
            and setup_count > 0
            and invalidation_count > 0
            and max_setup_quality >= min_tradeable_candidate_setup_quality
            and max_quality >= single_tradeable_candidate_setup_min_business_quality
            and avg_confidence >= single_tradeable_candidate_setup_min_confidence
            and confirmation_score >= single_tradeable_candidate_setup_confirmation_score
            and not single_tradeable_blocking_failures.intersection(gating_failures)
            and (
                not block_single_setup_on_technical_opposition
                or not technical_opposes
            )
        )
        single_tradeable_candidate_setup_promoted = bool(
            single_tradeable_candidate_setup_confirmed
            and negative_learning_signal < _safe_float(cfg.get("single_candidate_negative_learning_soft_cap"), 0.45)
            and recent_tail_loss_signal < _safe_float(cfg.get("single_candidate_tail_loss_soft_cap"), 0.35)
            and entry_quality_loss_signal < _safe_float(cfg.get("single_candidate_entry_quality_loss_soft_cap"), 0.35)
            and net_trigger_quality_loss_signal < _safe_float(cfg.get("single_candidate_trigger_quality_loss_soft_cap"), 0.35)
        )
        scorecard_promotion_reasons: list[str] = []
        if single_tradeable_candidate_setup_promoted:
            scorecard_promotion_reasons.append("single_tradeable_candidate_with_strong_market_confirmation")
            score = max(score, tradeable_threshold)
        elif single_tradeable_candidate_setup_confirmed:
            scorecard_promotion_reasons.append("single_tradeable_candidate_rank_lowered_by_recent_learning")

        if score >= deployable_threshold and max_setup_quality >= min_deployable_setup_quality and not gating_failures:
            final_state = "tradeable_candidate"
        elif (
            (
                score >= tradeable_threshold
                and max_setup_quality >= min_tradeable_candidate_setup_quality
                and "critical_data_gap" not in gating_failures
                and tradeable_states > 0
                and setup_count > 0
            )
            or single_tradeable_candidate_setup_promoted
        ):
            final_state = "probe_candidate"
        elif support_count > 0:
            final_state = "watch_for_trigger"
        else:
            final_state = "no_opportunity"

        opportunity_score = round(score, 4)
        candidate_quality_components = _candidate_quality_components(
            opportunity_score=opportunity_score,
            score_components=score_components,
            gating_failures=gating_failures,
            setup_quality=max_setup_quality,
            trigger_valid=bool(trigger_valid_count > 0),
            invalidation_count=invalidation_count,
        )
        candidate_quality = _candidate_quality_score(candidate_quality_components)
        side_rows[side] = {
            "side": side,
            "score": opportunity_score,
            "opportunity_score": opportunity_score,
            "candidate_quality": candidate_quality,
            "candidate_quality_components": candidate_quality_components,
            "direction_evidence_strength": candidate_quality,
            "direction_evidence_components": {
                "opportunity_score": opportunity_score,
                "candidate_quality": candidate_quality,
                "supporting_signal_count": support_count,
                "supporting_analysts": sorted(set(name for name in analyst_names if name)),
                "setup_quality": round(max_setup_quality, 4),
                "trigger_valid": bool(trigger_valid_count > 0),
                "invalidation_present": bool(invalidation_count > 0),
                "conflict_count": len([item for item in gating_failures if str(item or "").strip()]),
            },
            "analyst_direction_evidence": {
                "side": side,
                "source": "pm_signal_fusion",
                "boundary": "structured_direction_evidence_not_pm_side_selection",
                "supporting_signal_count": support_count,
                "supporting_analysts": sorted(set(name for name in analyst_names if name)),
                "candidate_quality": candidate_quality,
                "candidate_layer_hint": _candidate_layer_hint(final_state),
                "opportunity_score": opportunity_score,
            },
            "direction_evidence_boundary": "fusion_preserves_signal_collector_evidence_no_pm_side_selection",
            "candidate_layer_hint": _candidate_layer_hint(final_state),
            "rank_candidate_input_components": {
                "cold_start_evidence_quality": round(opportunity_score, 4),
                "product_setup_trigger_history": round(_safe_float(score_components.get("alpha_profile_adjustment"), 0.0), 4),
                "trigger_execution_quality": round(
                    _safe_float(score_components.get("execution_profile_learning"), 0.0)
                    + _safe_float(score_components.get("trigger_quality_positive_bonus"), 0.0)
                    + _safe_float(score_components.get("trigger_quality_loss_penalty"), 0.0),
                    4,
                ),
                "open_add_action_value_signals": {
                    "positive_signal": round(_safe_float(action_value_learning.get("positive_signal"), 0.0), 4),
                    "negative_signal": round(_safe_float(action_value_learning.get("negative_signal"), 0.0), 4),
                    "recent_tail_loss_signal": round(_safe_float(action_value_learning.get("recent_tail_loss_signal"), 0.0), 4),
                    "entry_quality_loss_signal": round(_safe_float(action_value_learning.get("entry_quality_loss_signal"), 0.0), 4),
                    "net_trigger_quality_loss_signal": round(_safe_float(action_value_learning.get("net_trigger_quality_loss_signal"), 0.0), 4),
                },
                "conflict_risk_invalidation_inputs": {
                    "gating_failure_count": len([item for item in gating_failures if str(item or "").strip()]),
                    "fusion_conflict_adjustment": round(_safe_float(score_components.get("fusion_conflict_adjustment"), 0.0), 4),
                    "market_conflict_penalty": round(_safe_float(score_components.get("market_conflict_penalty"), 0.0), 4),
                    "critical_data_gap_penalty": round(_safe_float(score_components.get("critical_data_gap_penalty"), 0.0), 4),
                    "fundamental_gap_penalty": round(_safe_float(score_components.get("fundamental_gap_penalty"), 0.0), 4),
                },
                "final_rank_score_generated_by": "pm_full_market_capital_deployment",
            },
            "opportunity_score_components": {
                key: round(float(value or 0.0), 4)
                for key, value in score_components.items()
            },
            "final_state": final_state,
            "supporting_signal_count": support_count,
            "supporting_analysts": sorted(set(name for name in analyst_names if name)),
            "tradeable_opportunity_state_count": tradeable_states,
            "opportunity_state_counts": opportunity_state_counts,
            "opportunity_state": final_state,
            "setup_quality_ok": bool(setup_quality_ok_count > 0),
            "trigger_valid": bool(trigger_valid_count > 0),
            "current_trigger_confirmed": bool(current_trigger_confirmed_count > 0),
            "invalidation_present": bool(invalidation_count > 0),
            "entry_trigger": entry_triggers[0] if entry_triggers else "",
            "source_analysts": sorted(set(name for name in source_analysts if name)),
            "entry_setup_count": setup_count,
            "invalidation_count": invalidation_count,
            "avg_business_quality": round(avg_quality, 4),
            "max_business_quality": round(max_quality, 4),
            "avg_setup_quality": round(avg_setup_quality, 4),
            "max_setup_quality": round(max_setup_quality, 4),
            "setup_quality_notes": sorted(set(setup_quality_notes))[:12],
            "avg_confidence": round(avg_confidence, 4),
            "market_confirmation_score": round(confirmation_score, 4),
            "market_confirmation_features": [str(item) for item in confirmation_features[:8]],
            "market_confirmation_conflicts": [str(item) for item in confirmation_conflicts[:8]],
            "data_missing_count": len(data_missing),
            "critical_data_gap": critical_gap,
            "single_tradeable_candidate_setup_confirmed": single_tradeable_candidate_setup_confirmed,
            "single_tradeable_candidate_setup_promoted": single_tradeable_candidate_setup_promoted,
            "technical_opposes_side": technical_opposes,
            "scorecard_promotion_reasons": scorecard_promotion_reasons,
            "learning_positive_count": policy_counts.get("positive", 0),
            "learning_negative_count": policy_counts.get("negative", 0),
            "action_value_learning_summary": {
                "positive_signal": round(_safe_float(action_value_learning.get("positive_signal"), 0.0), 4),
                "negative_signal": round(_safe_float(action_value_learning.get("negative_signal"), 0.0), 4),
                "execution_profile_signal": round(_safe_float(action_value_learning.get("execution_profile_signal"), 0.0), 4),
                "recent_tail_loss_signal": round(_safe_float(action_value_learning.get("recent_tail_loss_signal"), 0.0), 4),
                "entry_quality_loss_signal": round(_safe_float(action_value_learning.get("entry_quality_loss_signal"), 0.0), 4),
                "trigger_quality_positive_signal": round(_safe_float(action_value_learning.get("trigger_quality_positive_signal"), 0.0), 4),
                "trigger_quality_loss_signal": round(_safe_float(action_value_learning.get("trigger_quality_loss_signal"), 0.0), 4),
                "net_trigger_quality_loss_signal": round(_safe_float(action_value_learning.get("net_trigger_quality_loss_signal"), 0.0), 4),
                "positive_count": action_value_learning.get("positive_count", 0),
                "negative_count": action_value_learning.get("negative_count", 0),
                "exact_real_count": action_value_learning.get("exact_real_count", 0),
                "episode_count": action_value_learning.get("episode_count", 0),
                "strongest_positive": action_value_learning.get("strongest_positive") or {},
                "strongest_negative": action_value_learning.get("strongest_negative") or {},
                "decision_lifecycle": action_value_learning.get("decision_lifecycle") or "open_add_new_risk",
                "used_lanes": list(action_value_learning.get("used_lanes") or []),
                "ignored_lanes": list(action_value_learning.get("ignored_lanes") or []),
                "execution_profile_signal_direct_to_rank": bool(
                    action_value_learning.get("execution_profile_signal_direct_to_rank")
                ),
            },
            "alpha_setup_profile_counts": {
                "deployable": len(deployable_profiles),
                "protected": len(protected_profiles),
                "watchlist": len(watchlist_profiles),
                "capped_or_rejected": len(capped_profiles),
            },
            "best_alpha_setup_profile": {
                "scope_key": best_alpha_profile.get("scope_key"),
                "setup_type": best_alpha_profile.get("setup_type"),
                "lifecycle_state": best_alpha_profile.get("lifecycle_state"),
                "profile_state_hint": (
                    best_alpha_profile.get("profile_state_hint")
                    or "profile_observe"
                ),
                "sample_count": best_alpha_profile.get("sample_count"),
                "win_rate": best_alpha_profile.get("win_rate"),
                "profit_factor": best_alpha_profile.get("profit_factor"),
                "net_pnl": best_alpha_profile.get("net_pnl"),
                "confidence_score": best_alpha_profile.get("confidence_score"),
                "max_position_impact": best_alpha_profile.get("max_position_impact"),
            } if best_alpha_profile else {},
            "alpha_setup_score_adjustment": round(alpha_profile_bonus - alpha_profile_penalty, 4),
            "learning_adjustment_summary": _learning_adjustment_summary(
                policy_counts=policy_counts,
                action_value_learning=action_value_learning,
                alpha_profile_bonus=alpha_profile_bonus,
                alpha_profile_penalty=alpha_profile_penalty,
                best_alpha_profile=best_alpha_profile,
                capped_profiles=capped_profiles,
            ),
            "gating_failures": gating_failures,
            "pm_fusion_diagnostics": pm_fusion_diagnostics,
            "pm_conflict_resolution": {
                "handled": not bool(pm_fusion_diagnostics.get("requires_pm_conflict_resolution"))
                or final_state in {"watch_for_trigger", "no_opportunity", "probe_candidate"},
                "resolution_effect": (
                    "downgrade_or_monitor"
                    if pm_fusion_diagnostics.get("requires_pm_conflict_resolution")
                    and final_state in {"watch_for_trigger", "probe_candidate"}
                    else "no_material_conflict"
                    if not pm_fusion_diagnostics.get("requires_pm_conflict_resolution")
                    else "tradeable_requires_auditor_review"
                ),
                "confirmation_requirements_addressed": bool(
                    not pm_fusion_diagnostics.get("requires_pm_confirmation_explanation")
                    or confirmation_score >= weak_confirmation_threshold
                    or final_state in {"watch_for_trigger", "no_opportunity"}
                ),
                "no_trade_authority": True,
            },
        }
        side_rows[side]["conditional_monitor_candidate"] = bool(
            final_state == "watch_for_trigger"
            and side_rows[side]["setup_quality_ok"]
            and not side_rows[side]["trigger_valid"]
            and side_rows[side]["invalidation_present"]
            and bool(side_rows[side]["entry_trigger"])
        )
        side_rows[side]["capital_allocation_reason"] = _capital_allocation_reason(
            row=side_rows[side],
            deployable_threshold=deployable_threshold,
            tradeable_threshold=tradeable_threshold,
        )

    preferred_side = "flat"
    if side_rows["long"]["score"] > side_rows["short"]["score"] + 0.04:
        preferred_side = "long"
    elif side_rows["short"]["score"] > side_rows["long"]["score"] + 0.04:
        preferred_side = "short"
    return {
        "version": "opportunity_scorecard_v1",
        "ticker": ticker,
        "preferred_side": preferred_side,
        "direction_evidence_boundary": "fusion_preserves_signal_collector_evidence_no_pm_side_selection",
        "direction_evidence_summary": {
            side: {
                "direction_evidence_strength": side_rows[side].get("direction_evidence_strength"),
                "candidate_quality": side_rows[side].get("candidate_quality"),
                "opportunity_score": side_rows[side].get("opportunity_score"),
                "supporting_signal_count": side_rows[side].get("supporting_signal_count"),
                "candidate_layer_hint": side_rows[side].get("candidate_layer_hint"),
            }
            for side in ("long", "short")
        },
        "market_regime": _market_regime_from_signals(signals),
        "long": side_rows["long"],
        "short": side_rows["short"],
        "policy_counts": policy_counts,
        "thresholds": {
            "deployable_threshold": deployable_threshold,
            "tradeable_threshold": tradeable_threshold,
            "weak_confirmation_threshold": weak_confirmation_threshold,
            "min_deployable_setup_quality": min_deployable_setup_quality,
            "min_tradeable_candidate_setup_quality": min_tradeable_candidate_setup_quality,
            "single_tradeable_candidate_setup_confirmation_score": single_tradeable_candidate_setup_confirmation_score,
            "single_tradeable_candidate_setup_min_business_quality": single_tradeable_candidate_setup_min_business_quality,
            "single_tradeable_candidate_setup_min_confidence": single_tradeable_candidate_setup_min_confidence,
            "technical_opposition_min_confidence": technical_opposition_min_confidence,
        },
        "alpha_setup_profiles_enabled": True,
        "not_product_preference": True,
        "no_future_data": True,
        "hard_margin_cap_not_overridden": True,
    }
