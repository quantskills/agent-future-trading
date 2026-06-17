"""Capital utilization and alpha release policy helpers for portfolio decisions."""

from __future__ import annotations

from tools.agent_tools.decision.capital_allocator import (
    conflicting_weak_memory_record as _conflicting_weak_memory_record,
    high_quality_learning_context,
)
from tools.agent_tools.decision.pm_invalidation_policy import (
    _has_explicit_stop_protection,
    _has_structured_invalidation_condition,
)
from tools.agent_tools.decision.position_lifecycle import (
    apply_trade_plan_multiplier as _position_apply_trade_plan_multiplier,
    is_new_or_increasing_exposure as _is_new_or_increasing_exposure,
    same_sign as _same_sign,
    target_side_from_ratio as _target_side_from_ratio,
)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _apply_trade_plan_multiplier(
    *,
    target_ratio: float,
    current_ratio: float,
    multiplier: float,
) -> float:
    """Scale new risk without forcing an existing same-side position to shrink."""
    return _position_apply_trade_plan_multiplier(
        target_ratio=target_ratio,
        current_ratio=current_ratio,
        multiplier=multiplier,
    )


def _memory_signal_combo_is_specific(row: dict, signal_combo: tuple[str, str, str]) -> bool:
    raw_combo = (row or {}).get("signal_combo")
    if isinstance(raw_combo, (list, tuple)):
        row_combo = tuple(str(item).strip() for item in raw_combo)
    else:
        text = str(raw_combo or "").strip()
        if not text or text == "*":
            return False
        if "|" in text:
            row_combo = tuple(part.strip() for part in text.split("|"))
        else:
            return False
    return row_combo == tuple(signal_combo)


_ALPHA_RELEASE_TIER_ORDER = {
    "blocked": 0,
    "probe": 1,
    "normal": 2,
    "boost": 3,
    "max_boost": 4,
}

_SOFT_LIMITED_PRE_CONTROL_REASONS = {
    "business_quality_probe_only",
    "business_quality_observe_or_block",
    "opportunity_scorecard_probe_seed",
    "single_high_quality_probe_only",
    "pm_direction_only_probe_cap",
    "horizon_consistency_probe_cap",
    "market_confirmation_quality_gate",
    "market_confirmation_conflict",
    "weak_signal_combo_probe_cap",
    "side_performance_probe_cap",
    "trade_auditor_soft_probe_floor",
    "trade_auditor_scale_to_zero",
    "missing_pretrade_invalidation",
    "fast_candidate_alpha_probe",
    "unknown_alpha_probe",
    "negative_expectancy_cap_or_exit",
    "soft_block_converted_to_probe_only",
}


def _soft_limited_pre_control_reasons(pre_control_reasons: list[str] | None) -> list[str]:
    """Return prior soft-risk reasons that must not be enlarged by capital allocation."""
    matched: list[str] = []
    for raw in pre_control_reasons or []:
        reason = str(raw or "").strip()
        if not reason:
            continue
        lower = reason.lower()
        if (
            lower in _SOFT_LIMITED_PRE_CONTROL_REASONS
            or lower.endswith("_probe_cap")
            or lower.endswith("_probe_only")
            or lower.endswith("_scale_down")
            or lower.endswith("_cap_or_exit")
        ):
            matched.append(reason)
    return sorted(set(matched))


def _normalize_alpha_release_tier(value, default: str = "normal") -> str:
    text = str(value or default).strip().lower()
    aliases = {
        "none": "blocked",
        "off": "blocked",
        "probe_only": "probe",
        "probe_unverified": "probe",
        "base": "normal",
        "confirmed_observation": "normal",
        "validated": "boost",
        "validated_with_stop": "boost",
        "strong": "boost",
        "strong_opportunity": "boost",
        "exceptional": "max_boost",
        "exceptional_validated": "max_boost",
        "exceptional_validated_with_stop": "max_boost",
        "max": "max_boost",
    }
    normalized = aliases.get(text, text)
    if normalized in _ALPHA_RELEASE_TIER_ORDER:
        return normalized
    return default if default in _ALPHA_RELEASE_TIER_ORDER else "normal"


def _cap_alpha_release_tier(tier: str, max_tier: str) -> str:
    tier = _normalize_alpha_release_tier(tier)
    max_tier = _normalize_alpha_release_tier(max_tier)
    tier_order = min(_ALPHA_RELEASE_TIER_ORDER[tier], _ALPHA_RELEASE_TIER_ORDER[max_tier])
    for candidate, order in _ALPHA_RELEASE_TIER_ORDER.items():
        if order == tier_order:
            return candidate
    return "blocked"


def _resolve_alpha_release_tier(
    *,
    control: dict,
    high_quality_memory: bool,
    confirmation_score: float,
    stop_protected: bool,
    structured_invalidation: bool,
    evidence_row: dict | None,
    signal_combo: tuple[str, str, str],
) -> tuple[str, dict]:
    """Classify learned alpha release without letting generic memory become a blank check."""

    def _float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    evidence = evidence_row or {}
    sample_count = _int(evidence.get("sample_count"))
    win_rate = _float(evidence.get("win_rate"))
    net_pnl = _float(evidence.get("net_pnl"))
    policy_type = str(evidence.get("policy_type") or "").lower()
    evidence_source = str(evidence.get("evidence_source") or "").lower()
    policy_scope_specific = (
        bool(policy_type)
        and str(evidence.get("ticker") or "*") != "*"
        and str(evidence.get("side") or "*") != "*"
        and str(evidence.get("signal_template") or "*") != "*"
    )
    combo_specific = (
        _memory_signal_combo_is_specific(evidence, signal_combo)
        if evidence and evidence_source != "adaptive_policy_state"
        else policy_scope_specific
    )
    require_specific_combo = bool(control.get("require_specific_signal_combo_for_strong_scaling", True))
    boost_min_score = _float(
        control.get("alpha_release_boost_min_confirmation_score"),
        _float(
            control.get("memory_protected_min_confirmation_score"),
            _float(control.get("min_confirmation_score_for_scaling"), 0.60),
        ),
    )
    boost_min_samples = _int(
        control.get("alpha_release_boost_min_sample_count"),
        _int(control.get("protected_min_sample_count_for_scaling"), 5),
    )
    boost_min_win_rate = _float(
        control.get("alpha_release_boost_min_win_rate"),
        _float(control.get("protected_min_win_rate_for_scaling"), 0.60),
    )
    boost_min_net_pnl = _float(
        control.get("alpha_release_boost_min_net_pnl"),
        _float(control.get("protected_min_net_pnl_for_scaling"), 1000.0),
    )
    max_boost_min_score = _float(
        control.get("alpha_release_max_boost_min_confirmation_score"),
        _float(control.get("exceptional_validated_min_confirmation_score"), 0.85),
    )
    max_boost_min_samples = _int(
        control.get("alpha_release_max_boost_min_sample_count"),
        _int(control.get("exceptional_validated_min_sample_count"), 8),
    )
    max_boost_min_win_rate = _float(
        control.get("alpha_release_max_boost_min_win_rate"),
        _float(control.get("exceptional_validated_min_win_rate"), 0.70),
    )
    max_boost_min_net_pnl = _float(
        control.get("alpha_release_max_boost_min_net_pnl"),
        _float(control.get("exceptional_validated_min_net_pnl"), 5000.0),
    )
    boost_evidence_ok = (
        sample_count >= boost_min_samples
        and win_rate >= boost_min_win_rate
        and net_pnl >= boost_min_net_pnl
    )

    limiting_reasons: list[str] = []
    if not high_quality_memory or not boost_evidence_ok:
        limiting_reasons.append("high_quality_learning_evidence_required")
        tier = "probe"
    else:
        max_boost_ok = (
            confirmation_score >= max_boost_min_score
            and sample_count >= max_boost_min_samples
            and win_rate >= max_boost_min_win_rate
            and net_pnl >= max_boost_min_net_pnl
        )
        tier = "max_boost" if max_boost_ok else "boost"

    if confirmation_score < boost_min_score:
        tier = _cap_alpha_release_tier(tier, "normal")
        limiting_reasons.append("confirmation_below_alpha_release_boost_threshold")
    if not structured_invalidation:
        tier = _cap_alpha_release_tier(tier, "probe")
        limiting_reasons.append("missing_pretrade_invalidation")
    if structured_invalidation and not stop_protected:
        tier = _cap_alpha_release_tier(tier, "normal")
        limiting_reasons.append("missing_explicit_stop_for_alpha_release_boost")
    if not combo_specific:
        tier = _cap_alpha_release_tier(tier, "normal")
        limiting_reasons.append("generic_memory_cannot_trigger_alpha_release_boost")

    strong_scaling_allowed = tier in {"boost", "max_boost"}
    return tier, {
        "tier": tier,
        "strong_scaling_allowed": strong_scaling_allowed,
        "release_allowed": tier in {"normal", "boost", "max_boost"},
        "confirmation_score": float(confirmation_score),
        "boost_min_confirmation_score": boost_min_score,
        "sample_count": sample_count,
        "win_rate": win_rate,
        "net_pnl": net_pnl,
        "boost_evidence_ok": boost_evidence_ok,
        "specific_signal_combo": combo_specific,
        "require_specific_signal_combo": require_specific_combo,
        "stop_protected": bool(stop_protected),
        "structured_invalidation": bool(structured_invalidation),
        "limiting_reasons": limiting_reasons,
        "limiting_reason": limiting_reasons[0] if limiting_reasons else "",
    }


def _resolve_dynamic_opportunity_budget(
    *,
    control: dict,
    high_quality_memory: bool,
    alpha_release_tier: str,
    confirmation_score: float,
    stop_protected: bool,
    learning_evidence: dict | None,
    current_margin_ratio: float,
    target_total_margin_ratio: float,
    max_after_scaling: float,
) -> tuple[float, str, dict[str, float]]:
    """Resolve a soft opportunity budget from current evidence."""
    residual_capacity = max(0.0, max_after_scaling - current_margin_ratio)
    if residual_capacity <= 0:
        return 0.0, "no_capacity", {
            "residual_capacity": 0.0,
            "allocation_fraction": 0.0,
            "reserved_for_other_opportunities": 0.0,
        }

    confirmation = max(0.0, min(1.0, confirmation_score))
    evidence = learning_evidence or {}

    def _float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    exceptional_enabled = bool(control.get("exceptional_validated_enabled", True))
    exceptional_requires_stop = bool(control.get("exceptional_validated_requires_stop_protection", True))
    exceptional_candidate = (
        high_quality_memory
        and alpha_release_tier == "max_boost"
        and exceptional_enabled
        and (stop_protected or not exceptional_requires_stop)
        and confirmation >= _float(control.get("exceptional_validated_min_confirmation_score"), 0.85)
        and _int(evidence.get("sample_count")) >= _int(control.get("exceptional_validated_min_sample_count"), 8)
        and _float(evidence.get("win_rate")) >= _float(control.get("exceptional_validated_min_win_rate"), 0.70)
        and _float(evidence.get("net_pnl")) >= _float(control.get("exceptional_validated_min_net_pnl"), 5000.0)
    )

    reserve_key = (
        "exceptional_other_opportunity_reserve_fraction_of_tradable_capital"
        if exceptional_candidate
        else "other_opportunity_reserve_fraction_of_tradable_capital"
    )
    reserve_default = 0.10 if exceptional_candidate else 0.10
    reserve_fraction = max(0.0, min(0.50, float(control.get(reserve_key, reserve_default) or reserve_default)))
    reserve_margin = max_after_scaling * reserve_fraction
    reserved_for_other = max(0.0, reserve_margin - current_margin_ratio)
    usable_after_reserve = max(0.0, residual_capacity - reserved_for_other)

    if not high_quality_memory:
        probe_fraction = max(0.0, min(1.0, float(
            control.get("unverified_probe_fraction_of_remaining_capacity", 0.12) or 0.12
        )))
        allocation_fraction = probe_fraction * max(0.25, confirmation)
        tier = "probe_unverified"
    else:
        if exceptional_candidate:
            min_fraction = max(0.0, min(1.0, float(
                control.get("exceptional_validated_min_fraction_of_remaining_capacity", 0.75) or 0.75
            )))
            max_fraction = max(min_fraction, min(1.0, float(
                control.get("exceptional_validated_max_fraction_of_remaining_capacity", 0.95) or 0.95
            )))
            power = max(0.25, float(control.get("exceptional_confirmation_allocation_power", 1.0) or 1.0))
            tier = "exceptional_validated_with_stop" if stop_protected else "exceptional_validated"
        else:
            min_fraction = max(0.0, min(1.0, float(
                control.get("validated_min_fraction_of_remaining_capacity", 0.35) or 0.35
            )))
            max_fraction = max(min_fraction, min(1.0, float(
                control.get("validated_max_fraction_of_remaining_capacity", 0.90) or 0.90
            )))
            power = max(0.25, float(control.get("confirmation_allocation_power", 1.25) or 1.25))
            tier = "validated"
        evidence_power = confirmation ** power
        allocation_fraction = min_fraction + (max_fraction - min_fraction) * evidence_power
        if stop_protected and not exceptional_candidate:
            bonus = max(0.0, min(1.0, float(
                control.get("stop_protection_allocation_bonus", 0.15) or 0.15
            )))
            allocation_fraction += (1.0 - allocation_fraction) * bonus
            tier = "validated_with_stop"

    allocation_fraction = max(0.0, min(1.0, allocation_fraction))
    budget = min(usable_after_reserve, residual_capacity * allocation_fraction)
    if budget <= 0 and not high_quality_memory:
        budget = min(residual_capacity, residual_capacity * allocation_fraction)
    return max(0.0, budget), tier, {
        "residual_capacity": residual_capacity,
        "allocation_fraction": allocation_fraction,
        "reserved_for_other_opportunities": reserved_for_other,
        "usable_after_reserve": usable_after_reserve,
        "reserve_fraction": reserve_fraction,
        "exceptional_validated": exceptional_candidate,
        "target_total_margin_ratio": target_total_margin_ratio,
    }


def _apply_capital_utilization_control(
    *,
    db,
    config_id: str,
    ticker: str,
    trading_date,
    position_ratio: float,
    current_ratio: float,
    current_margin_ratio: float,
    margin_rate: float,
    max_position_ratio: float,
    market_confirmation: dict,
    full_config: dict,
    signal_combo: tuple[str, str, str],
    strategy_memory: dict | None = None,
    adaptive_policy_state: list | None = None,
    analyst_signals: list | None = None,
    pre_control_reasons: list[str] | None = None,
) -> tuple[float, list[str], list[str], dict]:
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    control = full_config.get("capital_utilization_control", {}) or {}
    if not control.get("enabled", False):
        return position_ratio, reasons, notes, diagnostics
    if margin_rate <= 0 or abs(position_ratio) <= 0:
        return position_ratio, reasons, notes, diagnostics

    side = _target_side_from_ratio(position_ratio)
    soft_limited_reasons = _soft_limited_pre_control_reasons(pre_control_reasons)
    allow_protected_scaling = bool(control.get("allow_memory_protected_scaling", True))
    allow_recovering_scaling = bool(control.get("allow_recovering_template_scaling", False))
    high_quality_memory, learning_diagnostics = high_quality_learning_context(
        strategy_memory=strategy_memory or {},
        adaptive_policy_state=adaptive_policy_state or [],
        allow_memory_protected_scaling=allow_protected_scaling,
        allow_recovering_template_scaling=allow_recovering_scaling,
    )
    learning_evidence = (
        learning_diagnostics.get("protected_memory")
        if isinstance(learning_diagnostics.get("protected_memory"), dict)
        else {}
    )
    learned_demote_record = (
        learning_diagnostics.get("learned_demote_record")
        if isinstance(learning_diagnostics.get("learned_demote_record"), dict)
        else {}
    )
    if learned_demote_record:
        before = position_ratio
        demote_multiplier = max(
            0.0,
            min(1.0, _safe_float(learned_demote_record.get("multiplier"), 0.50)),
        )
        position_ratio = _apply_trade_plan_multiplier(
            target_ratio=position_ratio,
            current_ratio=current_ratio,
            multiplier=demote_multiplier,
        )
        learning_diagnostics["learned_underperformance_block"] = {
            "policy_action": learned_demote_record.get("policy_action"),
            "reason": learned_demote_record.get("reason"),
            "multiplier": demote_multiplier,
            "sample_count": learned_demote_record.get("sample_count"),
            "confidence_score": learned_demote_record.get("confidence_score"),
            "ratio_before": float(before),
            "ratio_after": float(position_ratio),
        }
        diagnostics["capital_utilization_learning"] = learning_diagnostics
        diagnostics["capital_utilization_skip"] = "learned_underperformance_policy"
        reasons.append("learned_underperformance_policy")
        notes.append(
            f"{ticker} learned policy underperformed benchmark; target ratio "
            f"{before:.2%}->{position_ratio:.2%}"
        )
        return position_ratio, reasons, notes, diagnostics
    if high_quality_memory and not learning_evidence:
        adaptive_evidence = (
            learning_diagnostics.get("adaptive_protect_record")
            if isinstance(learning_diagnostics.get("adaptive_protect_record"), dict)
            else {}
        )
        if adaptive_evidence:
            learning_evidence = adaptive_evidence
    weak_conflict = _conflicting_weak_memory_record(strategy_memory or {}, signal_combo)
    protected_row = (
        learning_diagnostics.get("protected_memory")
        if isinstance(learning_diagnostics.get("protected_memory"), dict)
        else {}
    )
    adaptive_protect_row = (
        learning_diagnostics.get("adaptive_protect_record")
        if isinstance(learning_diagnostics.get("adaptive_protect_record"), dict)
        else {}
    )
    evidence_row = protected_row or adaptive_protect_row
    if high_quality_memory and evidence_row:
        min_protected_samples = int(control.get("protected_min_sample_count_for_scaling", 5) or 5)
        min_protected_win_rate = float(control.get("protected_min_win_rate_for_scaling", 0.60) or 0.60)
        min_protected_net_pnl = float(control.get("protected_min_net_pnl_for_scaling", 1000) or 1000)
        require_specific_combo = bool(control.get("require_specific_signal_combo_for_strong_scaling", True))
        combo_specific = _memory_signal_combo_is_specific(evidence_row, signal_combo)
        sample_count = int(evidence_row.get("sample_count") or 0)
        win_rate = float(evidence_row.get("win_rate") or 0.0)
        net_pnl = float(evidence_row.get("net_pnl") or 0.0)
        evidence_ok = (
            sample_count >= min_protected_samples
            and win_rate >= min_protected_win_rate
            and net_pnl >= min_protected_net_pnl
        )
        if not evidence_ok:
            high_quality_memory = False
            learning_evidence = {}
            learning_diagnostics["protected_evidence_rejected"] = {
                "reason": "protected_memory_evidence_below_scaling_threshold",
                "sample_count": sample_count,
                "min_sample_count": min_protected_samples,
                "win_rate": win_rate,
                "min_win_rate": min_protected_win_rate,
                "net_pnl": net_pnl,
                "min_net_pnl": min_protected_net_pnl,
                "specific_signal_combo": combo_specific,
                "require_specific_signal_combo": require_specific_combo,
                "evidence_source": "strategy_memory" if protected_row else "adaptive_policy_state",
            }
            diagnostics["capital_utilization_learning"] = learning_diagnostics
    if weak_conflict:
        learning_diagnostics["conflicting_weak_memory"] = weak_conflict
        if bool(control.get("block_scaling_on_conflicting_weak_memory", True)):
            diagnostics["capital_utilization_learning"] = learning_diagnostics
            diagnostics["capital_utilization_skip"] = "conflicting_weak_memory"
            return position_ratio, reasons, notes, diagnostics
    if high_quality_memory:
        diagnostics["capital_utilization_learning"] = learning_diagnostics

    is_new_or_increasing = _is_new_or_increasing_exposure(position_ratio, current_ratio)
    add_on_tolerance = float(control.get("same_side_add_on_match_tolerance", 0.0005) or 0.0005)
    same_side_matched_add_on = (
        bool(control.get("allow_confirmed_same_side_add_on", True))
        and high_quality_memory
        and not is_new_or_increasing
        and _same_sign(position_ratio, current_ratio)
        and abs(position_ratio) >= abs(current_ratio) - max(1e-12, add_on_tolerance)
    )
    if same_side_matched_add_on:
        diagnostics["capital_utilization_same_side_add_on"] = {
            "current_ratio": float(current_ratio),
            "matched_target_ratio": float(position_ratio),
            "requires_high_quality_memory": True,
        }
    elif not is_new_or_increasing:
        return position_ratio, reasons, notes, diagnostics

    if (
        soft_limited_reasons
        and bool(control.get("respect_pre_control_soft_limits", True))
        and not same_side_matched_add_on
        and not high_quality_memory
    ):
        diagnostics["capital_utilization_skip"] = "pre_control_soft_limit"
        diagnostics["capital_utilization_pre_control_soft_limit"] = {
            "reasons": soft_limited_reasons,
            "kept_ratio": float(position_ratio),
            "current_ratio": float(current_ratio),
            "does_not_block_trade": True,
            "prevents_reinflating_probe_or_cap": True,
            "release_evidence_present": False,
        }
        reasons.append("capital_utilization_soft_limit_respected")
        notes.append(
            f"{ticker} capital utilization kept prior soft-limited ratio "
            f"{position_ratio:.2%}; reasons={soft_limited_reasons}"
        )
        return position_ratio, reasons, notes, diagnostics
    if soft_limited_reasons and high_quality_memory:
        diagnostics["capital_utilization_soft_risk_arbiter"] = {
            "reasons": soft_limited_reasons,
            "release_evidence_present": True,
            "allowed_to_continue": True,
            "boundary": (
                "soft risks do not reinflate weak probes by themselves; "
                "same-scope high-quality learning may continue to normal alpha-release sizing, "
                "still bounded by current confirmation, stop protection, final authority, and hard risk"
            ),
        }

    trade_control = full_config.get("trade_frequency_control", {}) or {}
    weak_combos = [tuple(item) for item in (trade_control.get("weak_signal_combos") or [])]
    if (
        bool(control.get("disable_scaling_when_weak_combo", True))
        and signal_combo in weak_combos
        and not high_quality_memory
    ):
        diagnostics["capital_utilization_skip"] = "weak_signal_combo"
        return position_ratio, reasons, notes, diagnostics

    side_override = ((trade_control.get("side_overrides") or {}).get(ticker) or {})
    override_key = f"{side}_cap_multiplier"
    if (
        bool(control.get("disable_scaling_for_static_capped_side", True))
        and override_key in side_override
        and not high_quality_memory
    ):
        diagnostics["capital_utilization_skip"] = "static_side_cap"
        return position_ratio, reasons, notes, diagnostics

    confirmation_score = float(market_confirmation.get("confirmation_score", 0.0) or 0.0)
    stop_protected = _has_explicit_stop_protection(analyst_signals or [])
    structured_invalidation = stop_protected or _has_structured_invalidation_condition(analyst_signals or [])
    alpha_release_tier, alpha_release_diag = _resolve_alpha_release_tier(
        control=control,
        high_quality_memory=high_quality_memory,
        confirmation_score=confirmation_score,
        stop_protected=stop_protected,
        structured_invalidation=structured_invalidation,
        evidence_row=evidence_row,
        signal_combo=signal_combo,
    )
    if learning_diagnostics or evidence_row:
        learning_diagnostics["alpha_release"] = alpha_release_diag
        diagnostics["capital_utilization_learning"] = learning_diagnostics
    if high_quality_memory and not alpha_release_diag.get("strong_scaling_allowed"):
        high_quality_memory = False
        learning_evidence = {}
        learning_diagnostics["protected_evidence_rejected"] = {
            **(learning_diagnostics.get("protected_evidence_rejected") or {}),
            "reason": alpha_release_diag.get("limiting_reason") or "alpha_release_tier_below_boost",
            "alpha_release_tier": alpha_release_tier,
            "alpha_release_limiting_reasons": alpha_release_diag.get("limiting_reasons") or [],
            "require_stop_protection": bool(control.get("require_stop_protection_for_strong_scaling", True)),
            "stop_protected": stop_protected,
            "structured_invalidation": structured_invalidation,
        }
        diagnostics["capital_utilization_learning"] = learning_diagnostics
    min_score = float(control.get("min_confirmation_score_for_scaling", 0.60))
    if high_quality_memory:
        protected_min_score = float(
            control.get(
                "memory_protected_min_confirmation_score",
                (full_config.get("strategy_memory", {}) or {}).get("audit", {}).get(
                    "protected_min_confirmation_score",
                    min_score,
                ),
            )
        )
        min_score = min(min_score, protected_min_score)
    if confirmation_score < min_score:
        diagnostics["capital_utilization_skip"] = "confirmation_score_below_threshold"
        return position_ratio, reasons, notes, diagnostics

    if (
        bool(control.get("scale_only_when_recent_pnl_positive", True))
        and db
        and config_id
        and not high_quality_memory
    ):
        side_perf = db.get_futures_trade_pair_performance(
            config_id=config_id,
            ticker=ticker,
            side=side,
            trading_date=trading_date,
            lookback_trades=int(control.get("lookback_trades_for_scaling", 10)),
        )
        diagnostics["capital_side_performance"] = side_perf
        total_trades = int(side_perf.get("total_trades", 0) or 0)
        win_rate = float(side_perf.get("win_rate", 0.0) or 0.0)
        total_pnl = float(side_perf.get("total_pnl", 0.0) or 0.0)
        if total_trades < int(control.get("min_completed_trades_for_scaling", 0)):
            diagnostics["capital_utilization_skip"] = "not_enough_completed_trades"
            return position_ratio, reasons, notes, diagnostics
        if win_rate < float(control.get("min_recent_win_rate_for_scaling", 0.0)):
            diagnostics["capital_utilization_skip"] = "recent_side_win_rate_below_threshold"
            return position_ratio, reasons, notes, diagnostics
        if total_pnl <= float(control.get("min_recent_total_pnl_for_scaling", 0.0)):
            diagnostics["capital_utilization_skip"] = "recent_side_pnl_below_threshold"
            return position_ratio, reasons, notes, diagnostics
        if total_trades <= 0 or total_pnl <= 0:
            diagnostics["capital_utilization_skip"] = "recent_side_pnl_not_positive"
            return position_ratio, reasons, notes, diagnostics

    target_min = float(control.get("target_margin_ratio_min", 0.16))
    target_max = float(control.get("target_margin_ratio_max", control.get("max_margin_ratio_after_scaling", 0.20)))
    if high_quality_memory:
        target_min = float(control.get("strong_opportunity_target_margin_ratio_min", target_min))
        target_max = float(control.get("strong_opportunity_target_margin_ratio_max", target_max))
        target_total_margin_ratio = float(control.get("strong_opportunity_target_margin_ratio_confirmed", target_min))
        target_mode = "alpha_release_max_boost" if alpha_release_tier == "max_boost" else "alpha_release_boost"
    else:
        target_total_margin_ratio = float(control.get("target_margin_ratio_confirmed", target_min))
        target_mode = "confirmed_observation"
    target_total_margin_ratio = min(max(target_total_margin_ratio, target_min), target_max)
    max_after_scaling = float(control.get("max_margin_ratio_after_scaling", target_max))
    effective_max_position_ratio = float(max_position_ratio)
    single_position_cap_lifted = False
    dynamic_margin_budget = None
    dynamic_allocation_tier = "base"
    dynamic_budget_diagnostics: dict[str, float] = {}
    opportunity_margin_cap_limited = False
    if bool(control.get("dynamic_concentration_enabled", True)):
        dynamic_margin_budget, dynamic_allocation_tier, dynamic_budget_diagnostics = _resolve_dynamic_opportunity_budget(
            control=control,
            high_quality_memory=high_quality_memory,
            alpha_release_tier=alpha_release_tier,
            confirmation_score=confirmation_score,
            stop_protected=stop_protected,
            learning_evidence=learning_evidence,
            current_margin_ratio=current_margin_ratio,
            target_total_margin_ratio=target_total_margin_ratio,
            max_after_scaling=max_after_scaling,
        )
        if dynamic_margin_budget > 0 and margin_rate > 0:
            effective_max_position_ratio = max(effective_max_position_ratio, dynamic_margin_budget / margin_rate)
    elif high_quality_memory:
        strong_cap_multiplier = float(control.get("strong_opportunity_max_position_ratio_multiplier", 1.0) or 1.0)
        strong_cap = float(control.get("strong_opportunity_max_position_ratio_cap", max_position_ratio) or max_position_ratio)
        risk_caps = ((full_config.get("risk_control") or {}).get("max_single_position_ratio") or {})
        safe_single_cap = float(risk_caps.get("safe", max_position_ratio) or max_position_ratio)
        cap_lift_allowed = max_position_ratio >= safe_single_cap * 0.95
        if cap_lift_allowed and (strong_cap_multiplier > 1.0 or strong_cap > max_position_ratio):
            effective_max_position_ratio = min(strong_cap, max_position_ratio * max(1.0, strong_cap_multiplier))
        single_margin_cap_raw = control.get("strong_opportunity_max_single_margin_ratio_cap")
        if single_margin_cap_raw is not None:
            try:
                dynamic_margin_budget = max(0.0, float(single_margin_cap_raw))
                dynamic_allocation_tier = "legacy_single_margin_cap"
            except (TypeError, ValueError):
                dynamic_margin_budget = None
    if dynamic_margin_budget and margin_rate > 0:
        margin_cap_position_ratio = dynamic_margin_budget / margin_rate
        if margin_cap_position_ratio < effective_max_position_ratio - 1e-12:
            opportunity_margin_cap_limited = True
        effective_max_position_ratio = min(effective_max_position_ratio, margin_cap_position_ratio)
    single_position_cap_lifted = effective_max_position_ratio > max_position_ratio + 1e-12
    diagnostics["capital_utilization_target"] = {
        "target_mode": target_mode,
        "high_quality_memory": high_quality_memory,
        "current_margin_ratio": current_margin_ratio,
        "target_margin_ratio_min": target_min,
        "target_margin_ratio_max": target_max,
        "target_margin_ratio_confirmed": target_total_margin_ratio,
        "base_max_position_ratio": float(max_position_ratio),
        "effective_max_position_ratio": float(effective_max_position_ratio),
        "effective_single_margin_ratio_cap": float(effective_max_position_ratio * margin_rate),
        "dynamic_opportunity_margin_ratio_budget": dynamic_margin_budget,
        "dynamic_opportunity_margin_ratio_cap": dynamic_margin_budget,
        "dynamic_allocation_tier": dynamic_allocation_tier,
        "dynamic_budget_diagnostics": dynamic_budget_diagnostics,
        "alpha_release_tier": alpha_release_tier,
        "alpha_release": alpha_release_diag,
        "stop_protected": stop_protected,
        "structured_invalidation": structured_invalidation,
        "base_position_anchor_lifted": single_position_cap_lifted,
        "single_position_cap_lifted": single_position_cap_lifted,
        "opportunity_margin_cap_limited": opportunity_margin_cap_limited,
        "underutilization_breach": current_margin_ratio < target_min,
        "capital_allocation_tier": (
            "under_deployed"
            if current_margin_ratio < target_min
            else ("over_deployed" if current_margin_ratio > target_max else "target_band")
        ),
        "margin_ratio_gap_to_min": max(0.0, target_min - current_margin_ratio),
    }
    allowed_increment_margin_ratio = max(0.0, min(
        target_total_margin_ratio - current_margin_ratio,
        max_after_scaling - current_margin_ratio,
    ))
    if (
        allowed_increment_margin_ratio <= 0
        and not high_quality_memory
        and bool(control.get("allow_probe_above_observation_target", True))
        and dynamic_margin_budget
        and current_margin_ratio < max_after_scaling
    ):
        allowed_increment_margin_ratio = max(0.0, max_after_scaling - current_margin_ratio)
    if allowed_increment_margin_ratio <= 0:
        diagnostics["capital_utilization_skip"] = (
            "target_margin_already_reached"
            if current_margin_ratio >= target_min
            else "no_margin_capacity_under_target_guard"
        )
        return position_ratio, reasons, notes, diagnostics

    proposed_abs_ratio = min(effective_max_position_ratio, allowed_increment_margin_ratio / margin_rate)
    if proposed_abs_ratio > abs(position_ratio):
        before = position_ratio
        position_ratio = (1.0 if position_ratio > 0 else -1.0) * proposed_abs_ratio
        reasons.append("capital_utilization_guard")
        if high_quality_memory:
            reasons.append("capital_utilization_memory_protected")
            reasons.append(f"alpha_release_{alpha_release_tier}")
        if same_side_matched_add_on:
            reasons.append("capital_utilization_same_side_add_on")
        notes.append(
            f"capital utilization scaled ratio {before:.2%}->{position_ratio:.2%}; "
            f"confirmation_score={confirmation_score:.2f}; "
            f"margin_target={target_min:.0%}-{target_max:.0%}; "
            f"learning_protected={high_quality_memory}"
        )
    elif current_margin_ratio < target_min:
        diagnostics["capital_utilization_skip"] = "candidate_ratio_not_improved"

    return position_ratio, reasons, notes, diagnostics


__all__ = [
    "_apply_capital_utilization_control",
    "_resolve_alpha_release_tier",
    "_resolve_dynamic_opportunity_budget",
]
