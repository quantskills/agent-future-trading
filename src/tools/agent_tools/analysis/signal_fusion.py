from __future__ import annotations

"""Signal-fusion helpers shared by PM, reviewer tests, and future refactors."""

from typing import Any, Iterable, Mapping


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


def _learning_adjustment_summary(
    *,
    policy_counts: Mapping[str, int],
    alpha_profile_bonus: float,
    alpha_profile_penalty: float,
    best_alpha_profile: Mapping[str, Any],
    capped_profiles: list[Mapping[str, Any]],
) -> dict[str, Any]:
    net_adjustment = alpha_profile_bonus - alpha_profile_penalty
    return {
        "positive_policy_count": int(policy_counts.get("positive", 0) or 0),
        "negative_policy_count": int(policy_counts.get("negative", 0) or 0),
        "alpha_setup_score_adjustment": round(net_adjustment, 4),
        "best_profile_state": best_alpha_profile.get("lifecycle_state") if best_alpha_profile else None,
        "best_profile_scope_key": best_alpha_profile.get("scope_key") if best_alpha_profile else None,
        "capped_or_rejected_profile_count": len(capped_profiles),
        "effect": (
            "boosted"
            if net_adjustment > 0
            else "penalized"
            if net_adjustment < 0
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
        score_components = {
            "directional_support": min(
                _score_cap(cfg, "directional_support", 0.28),
                _score_weight(cfg, "directional_support_per_signal", 0.10) * support_count,
            ),
            "tradeable_state": min(
                _score_cap(cfg, "tradeable_state", 0.24),
                _score_weight(cfg, "tradeable_state_per_signal", 0.12) * tradeable_states,
            ),
            "business_quality": _score_weight(cfg, "business_quality", 0.14) * max_quality,
            "setup_quality": _score_weight(cfg, "setup_quality", 0.16) * max_setup_quality,
            "confidence": _score_weight(cfg, "confidence", 0.12) * avg_confidence,
            "market_confirmation": _score_weight(cfg, "market_confirmation", 0.12) * confirmation_score,
            "positive_learning": _score_weight(cfg, "positive_learning", 0.05) * min(1.0, policy_counts.get("positive", 0)),
            "negative_learning": -abs(_score_weight(cfg, "negative_learning", 0.10)) * min(1.0, policy_counts.get("negative", 0)),
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
        alpha_profile_penalty = 0.08 if capped_profiles else 0.0
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
        scorecard_promotion_reasons: list[str] = []
        if single_tradeable_candidate_setup_confirmed:
            scorecard_promotion_reasons.append("single_tradeable_candidate_with_strong_market_confirmation")
            score = max(score, tradeable_threshold)

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
            or single_tradeable_candidate_setup_confirmed
        ):
            final_state = "probe_candidate"
        elif support_count > 0:
            final_state = "watch_for_trigger"
        else:
            final_state = "no_opportunity"

        opportunity_score = round(score, 4)
        side_rows[side] = {
            "side": side,
            "score": opportunity_score,
            "opportunity_score": opportunity_score,
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
            "technical_opposes_side": technical_opposes,
            "scorecard_promotion_reasons": scorecard_promotion_reasons,
            "learning_positive_count": policy_counts.get("positive", 0),
            "learning_negative_count": policy_counts.get("negative", 0),
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
                alpha_profile_bonus=alpha_profile_bonus,
                alpha_profile_penalty=alpha_profile_penalty,
                best_alpha_profile=best_alpha_profile,
                capped_profiles=capped_profiles,
            ),
            "gating_failures": gating_failures,
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
    ranked_sides = sorted(
        (
            side
            for side in ("long", "short")
            if side_rows[side]["supporting_signal_count"] > 0
            or side_rows[side]["final_state"] in {"watch_for_trigger", "probe_candidate", "tradeable_candidate"}
        ),
        key=lambda item: (
            _safe_float(side_rows[item].get("opportunity_score"), 0.0),
            int(side_rows[item].get("supporting_signal_count") or 0),
            _safe_float(side_rows[item].get("max_setup_quality"), 0.0),
        ),
        reverse=True,
    )
    for rank, side in enumerate(ranked_sides, start=1):
        side_rows[side]["opportunity_rank"] = rank
    for side in ("long", "short"):
        side_rows[side].setdefault("opportunity_rank", None)
    return {
        "version": "opportunity_scorecard_v1",
        "ticker": ticker,
        "preferred_side": preferred_side,
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
