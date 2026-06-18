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
            "expected_horizon_days": int(getattr(signal, "expected_horizon_days", 0) or getattr(signal, "horizon_days", 0) or 0),
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
    contract = getattr(signal, "next_round_memory_contract", None)
    if not isinstance(contract, Mapping):
        contract = getattr(signal, "research_contract", None)
    if isinstance(contract, Mapping):
        return contract.get(key, default)
    return default


def _opportunity_layer(signal: Any) -> str:
    layer = (
        getattr(signal, "opportunity_layer", None)
        or _contract_value(signal, "opportunity_layer")
        or "direction_only"
    )
    return str(layer or "direction_only").strip().lower() or "direction_only"


def _has_invalidation(signal: Any) -> bool:
    invalidation_level = getattr(signal, "invalidation_level", None)
    if invalidation_level is not None:
        return True
    fields = [
        getattr(signal, "would_change_view_if", ""),
        getattr(signal, "invalidation_condition", ""),
        _contract_value(signal, "invalidation_condition", ""),
    ]
    text = " ".join(str(item or "") for item in fields).strip().lower()
    if not text:
        return False
    generic_terms = {
        "requires_current_confirmation",
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
        "requires_current_confirmation",
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
    resulting opportunity layer per side.
    """
    signals = list(analyst_signals or [])
    cfg = config or {}
    deployable_threshold = _safe_float(cfg.get("deployable_threshold"), 0.72)
    tradeable_threshold = _safe_float(cfg.get("tradeable_threshold"), 0.58)
    weak_confirmation_threshold = _safe_float(cfg.get("weak_confirmation_threshold"), 0.45)
    min_deployable_setup_quality = _safe_float(cfg.get("min_deployable_setup_quality"), 0.72)
    min_tradeable_setup_quality = _safe_float(cfg.get("min_tradeable_setup_quality"), 0.55)
    single_tradeable_setup_confirmation_score = _safe_float(
        cfg.get("single_tradeable_setup_confirmation_score"),
        0.68,
    )
    single_tradeable_setup_min_business_quality = _safe_float(
        cfg.get("single_tradeable_setup_min_business_quality"),
        0.60,
    )
    single_tradeable_setup_min_confidence = _safe_float(
        cfg.get("single_tradeable_setup_min_confidence"),
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
        layer_counts: dict[str, int] = {}
        invalidation_count = 0
        setup_count = 0
        quality_scores: list[float] = []
        setup_quality_scores: list[float] = []
        setup_quality_notes: list[str] = []
        confidence_scores: list[float] = []
        analyst_names: list[str] = []
        for signal in supporting:
            layer = _opportunity_layer(signal)
            layer_counts[layer] = layer_counts.get(layer, 0) + 1
            if _has_invalidation(signal):
                invalidation_count += 1
            if _has_entry_setup(signal):
                setup_count += 1
            quality_scores.append(_safe_float(getattr(signal, "business_quality_score", 0.0), 0.0))
            setup_quality_scores.append(_setup_quality(signal))
            setup_quality_notes.extend(_setup_quality_notes(signal))
            confidence_scores.append(_safe_float(getattr(signal, "confidence", 0.0), 0.0))
            analyst_names.append(normalize_analyst_name(getattr(signal, "agent_name", "")))

        support_count = len(supporting)
        tradeable_layers = layer_counts.get("deployable_alpha", 0) + layer_counts.get("tradeable_setup", 0)
        avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0.0
        max_quality = max(quality_scores, default=0.0)
        avg_setup_quality = sum(setup_quality_scores) / len(setup_quality_scores) if setup_quality_scores else 0.0
        max_setup_quality = max(setup_quality_scores, default=0.0)
        avg_confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0.0
        score = 0.0
        score += min(0.28, 0.10 * support_count)
        score += min(0.24, 0.12 * tradeable_layers)
        score += 0.14 * max_quality
        score += 0.16 * max_setup_quality
        score += 0.12 * avg_confidence
        score += 0.12 * confirmation_score
        score += 0.05 * min(1.0, policy_counts.get("positive", 0))
        score -= 0.10 * min(1.0, policy_counts.get("negative", 0))
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
        score += alpha_profile_bonus
        score -= alpha_profile_penalty
        score -= 0.10 * min(1.0, len(confirmation_conflicts) / 3.0)
        score -= 0.12 if critical_gap else 0.0
        score -= 0.06 if fundamental_setup_gap else 0.0
        score = max(0.0, min(1.0, score))

        gating_failures: list[str] = []
        if support_count <= 0:
            gating_failures.append("no_directional_support")
        if tradeable_layers <= 0:
            gating_failures.append("no_tradeable_setup_layer")
        if setup_count <= 0:
            gating_failures.append("missing_entry_setup")
        if invalidation_count <= 0:
            gating_failures.append("missing_invalidation_boundary")
        if max_setup_quality < min_tradeable_setup_quality and support_count > 0:
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
        single_tradeable_setup_confirmed = bool(
            tradeable_layers > 0
            and setup_count > 0
            and invalidation_count > 0
            and max_setup_quality >= min_tradeable_setup_quality
            and max_quality >= single_tradeable_setup_min_business_quality
            and avg_confidence >= single_tradeable_setup_min_confidence
            and confirmation_score >= single_tradeable_setup_confirmation_score
            and not single_tradeable_blocking_failures.intersection(gating_failures)
            and (
                not block_single_setup_on_technical_opposition
                or not technical_opposes
            )
        )
        scorecard_promotion_reasons: list[str] = []
        if single_tradeable_setup_confirmed:
            scorecard_promotion_reasons.append("single_tradeable_setup_with_strong_market_confirmation")
            score = max(score, tradeable_threshold)

        if score >= deployable_threshold and max_setup_quality >= min_deployable_setup_quality and not gating_failures:
            final_layer = "deployable_alpha"
        elif (
            (
                score >= tradeable_threshold
                and max_setup_quality >= min_tradeable_setup_quality
                and "critical_data_gap" not in gating_failures
                and tradeable_layers > 0
                and setup_count > 0
            )
            or single_tradeable_setup_confirmed
        ):
            final_layer = "tradeable_setup"
        elif support_count > 0:
            final_layer = "direction_only"
        else:
            final_layer = "no_trade"

        side_rows[side] = {
            "side": side,
            "score": round(score, 4),
            "final_layer": final_layer,
            "supporting_signal_count": support_count,
            "supporting_analysts": sorted(set(name for name in analyst_names if name)),
            "tradeable_layer_count": tradeable_layers,
            "layer_counts": layer_counts,
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
            "single_tradeable_setup_confirmed": single_tradeable_setup_confirmed,
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
                    or best_alpha_profile.get("deprecated_action_bias_mirror")
                    or best_alpha_profile.get("action_bias")
                ),
                "deprecated_action_bias_mirror": best_alpha_profile.get("action_bias"),
                "sample_count": best_alpha_profile.get("sample_count"),
                "win_rate": best_alpha_profile.get("win_rate"),
                "profit_factor": best_alpha_profile.get("profit_factor"),
                "net_pnl": best_alpha_profile.get("net_pnl"),
                "confidence_score": best_alpha_profile.get("confidence_score"),
                "max_position_impact": best_alpha_profile.get("max_position_impact"),
            } if best_alpha_profile else {},
            "alpha_setup_score_adjustment": round(alpha_profile_bonus - alpha_profile_penalty, 4),
            "gating_failures": gating_failures,
        }

    preferred_side = "flat"
    if side_rows["long"]["score"] > side_rows["short"]["score"] + 0.04:
        preferred_side = "long"
    elif side_rows["short"]["score"] > side_rows["long"]["score"] + 0.04:
        preferred_side = "short"
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
            "min_tradeable_setup_quality": min_tradeable_setup_quality,
            "single_tradeable_setup_confirmation_score": single_tradeable_setup_confirmation_score,
            "single_tradeable_setup_min_business_quality": single_tradeable_setup_min_business_quality,
            "single_tradeable_setup_min_confidence": single_tradeable_setup_min_confidence,
            "technical_opposition_min_confidence": technical_opposition_min_confidence,
        },
        "alpha_setup_profiles_enabled": True,
        "not_product_preference": True,
        "no_future_data": True,
        "hard_margin_cap_not_overridden": True,
    }
