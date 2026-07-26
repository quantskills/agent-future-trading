"""Deterministic decision memory retrieval helper.

This tool is deterministic and quality-first.  It collects visible history
before ranking it, so an empty shell trace cannot occupy the slot of a real
profitable action-value.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from tools.common.adaptive_policy_safety import filter_adaptive_policy_state_for_pm
from tools.common.final_action_semantics import validate_action_preference_family_consistency


_MATCH_PRIORITY = {
    "exact_state": 0,
    "same_ticker_side_horizon": 1,
    "same_ticker_side": 2,
    "similar": 3,
    "weak_prior": 4,
}


def _text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _payload(row: Mapping[str, Any]) -> dict:
    value = row.get("payload")
    return dict(value) if isinstance(value, Mapping) else {}


def _consumer_scope(row: Mapping[str, Any]) -> str:
    payload = _payload(row)
    return _text(row.get("consumer_scope") or payload.get("consumer_scope")).lower()


def _canonical(row: Mapping[str, Any]) -> bool:
    payload = _payload(row)
    return bool(row.get("canonical_action_value") or payload.get("canonical_action_value") or row.get("id"))


def _has_reward(row: Mapping[str, Any]) -> bool:
    return any(
        key in row and row.get(key) not in (None, "")
        for key in ("reward_sum", "reward_mean", "sample_count", "win_count", "loss_count")
    )


def _has_preference(row: Mapping[str, Any]) -> bool:
    return bool(_text(row.get("action_preference") or _payload(row).get("action_preference")))


def _is_empty_shell(row: Mapping[str, Any]) -> bool:
    return not _canonical(row) and not _has_reward(row) and not _has_preference(row)


def _key(row: Mapping[str, Any]) -> tuple:
    payload = _payload(row)
    return (
        _text(row.get("id") or payload.get("id")),
        _text(row.get("ticker") or payload.get("ticker")).upper(),
        _text(row.get("side") or payload.get("side")).lower(),
        _text(row.get("horizon_class") or payload.get("horizon_class")).lower(),
        _text(row.get("market_regime") or payload.get("market_regime")).lower(),
        _text(row.get("setup_type") or payload.get("setup_type")).lower(),
        _text(row.get("canonical_action_family") or payload.get("canonical_action_family")).lower(),
        _text(row.get("learning_lane") or row.get("action_value_lane") or payload.get("learning_lane") or payload.get("action_value_lane")).lower(),
    )


def _quality_rank(row: Mapping[str, Any]) -> tuple:
    match = _text(row.get("retrieval_match_level"), "weak_prior").lower()
    reward_source = _text(row.get("reward_source") or _payload(row).get("reward_source")).lower()
    evidence_scope = _text(row.get("evidence_scope") or _payload(row).get("evidence_scope")).lower()
    return (
        1 if _is_empty_shell(row) else 0,
        0 if _canonical(row) else 1,
        0 if any(token in reward_source for token in ("episode", "real_trade", "complete_episode")) else 1,
        0 if evidence_scope == "exact_real_state" else 1 if evidence_scope == "partial_real_state" else 2 if evidence_scope == "similar_sql_prior" else 3,
        0 if _has_preference(row) else 1,
        _MATCH_PRIORITY.get(match, 9),
        -abs(_float(row.get("reward_sum"))),
        -_int(row.get("sample_count")),
        _text(row.get("last_sample_date") or row.get("sample_end_date")),
    )


def _normalize(row: Mapping[str, Any], *, match_level: str, match_reason: str) -> dict:
    normalized = dict(row)
    payload = _payload(normalized)
    consumer_scope = _consumer_scope(normalized)
    if consumer_scope:
        normalized["consumer_scope"] = consumer_scope
    normalized["retrieval_match_level"] = match_level
    normalized["retrieval_match_reason"] = match_reason
    stored_scope = _text(
        normalized.get("evidence_scope")
        or normalized.get("amplification_scope_quality")
        or payload.get("evidence_scope")
        or payload.get("amplification_scope_quality")
    ).lower()
    if (
        match_level in {"same_ticker_side_horizon", "same_ticker_side"}
        and stored_scope == "exact_real_state"
    ):
        normalized["evidence_scope"] = "partial_real_state"
        normalized["amplification_scope_quality"] = "partial_real_state"
    if payload:
        payload["retrieval_match_level"] = match_level
        payload["retrieval_match_reason"] = match_reason
        if (
            match_level in {"same_ticker_side_horizon", "same_ticker_side"}
            and stored_scope == "exact_real_state"
        ):
            payload["evidence_scope"] = "partial_real_state"
            payload["amplification_scope_quality"] = "partial_real_state"
        normalized["payload"] = payload
    return normalized


def _family_consistency_errors(row: Mapping[str, Any]) -> list[str]:
    validation = validate_action_preference_family_consistency(row)
    return list(validation.get("errors") or [])


def _merge_quality_first(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    best: dict[tuple, dict] = {}
    fallback_counter = 0
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        key = _key(row)
        if not any(key):
            key = ("row", fallback_counter)
            fallback_counter += 1
        existing = best.get(key)
        if existing is None or _quality_rank(row) < _quality_rank(existing):
            best[key] = row
    result = list(best.values())
    result.sort(key=_quality_rank)
    return result


def _safe_call(method: Any, **kwargs) -> tuple[list | dict, str]:
    if not callable(method):
        return [], "method_unavailable"
    try:
        return method(**kwargs), ""
    except TypeError:
        legacy_kwargs = dict(kwargs)
        legacy_kwargs.pop("consumer_scope", None)
        legacy_kwargs.pop("learning_lane", None)
        try:
            return method(**legacy_kwargs), "legacy_signature"
        except Exception as exc:
            return [], str(exc)
    except Exception as exc:
        return [], str(exc)


def _memory_status(value: Any) -> str:
    if isinstance(value, Mapping):
        return "available" if value else "empty"
    if isinstance(value, list):
        return "available" if value else "empty"
    return "empty"


def retrieve_pm_memory(
    *,
    db: Any,
    config_id: str,
    ticker: str,
    side: str,
    trading_date: Any,
    horizon_class: str | None = None,
    market_regime: str | None = None,
    setup_type: str | None = None,
    sector: str | None = None,
    signal_combo: list | None = None,
    include_similar: bool = False,
    include_profiles: bool = False,
    include_strategy_memory: bool = False,
    include_adaptive_policy_state: bool = False,
    include_provisional_policy_state: bool = False,
    full_config: Mapping[str, Any] | None = None,
    limit: int = 12,
) -> dict:
    attempts: list[dict] = []
    collected: list[dict] = []
    if not db or not hasattr(db, "get_alpha_setup_action_values"):
        return {
            "effective_memory_summary": {"status": "unavailable", "reason": "db_missing_get_alpha_setup_action_values"},
            "action_values": [],
            "alpha_setup_profiles": [],
            "strategy_memory": {},
            "adaptive_policy_state": [],
            "adaptive_policy_safety_trace": {},
            "provisional_policy_state": [],
            "rejected_or_downgraded": [],
            "retrieval_attempts": attempts,
        }

    layers = []
    if _text(setup_type):
        layers.append(
            (
                "exact_state",
                horizon_class,
                market_regime,
                setup_type,
                "ticker_side_horizon_regime_setup",
            )
        )
    layers.extend(
        [
            ("same_ticker_side_horizon", horizon_class, None, None, "ticker_side_horizon_fallback"),
            ("same_ticker_side", None, None, None, "ticker_side_fallback"),
        ]
    )
    rejected: list[dict] = []
    for match_level, layer_horizon, layer_regime, layer_setup, reason in layers:
        try:
            rows = db.get_alpha_setup_action_values(
                config_id=config_id,
                ticker=ticker,
                side=side,
                horizon_class=layer_horizon,
                market_regime=layer_regime,
                setup_type=layer_setup,
                trading_date=trading_date,
                limit=limit,
                consumer_scope="pm_learning",
            )
        except TypeError:
            rows = db.get_alpha_setup_action_values(
                config_id=config_id,
                ticker=ticker,
                side=side,
                horizon_class=layer_horizon,
                market_regime=layer_regime,
                setup_type=layer_setup,
                trading_date=trading_date,
                limit=limit,
            )
        except Exception as exc:
            attempts.append({
                "match_level": match_level,
                "match_reason": reason,
                "row_count": 0,
                "error": str(exc),
            })
            continue

        kept: list[dict] = []
        for row in rows or []:
            if not isinstance(row, Mapping):
                continue
            if _consumer_scope(row) != "pm_learning":
                rejected.append({"id": row.get("id"), "reason": "non_pm_learning_scope"})
                continue
            normalized = _normalize(row, match_level=match_level, match_reason=reason)
            if _is_empty_shell(normalized):
                rejected.append({"id": normalized.get("id"), "reason": "empty_shell_downgraded_not_blocking"})
                kept.append(normalized)
                continue
            semantic_errors = _family_consistency_errors(normalized)
            if semantic_errors:
                rejected.append({
                    "id": normalized.get("id"),
                    "reason": "action_value_family_consistency_error",
                    "errors": semantic_errors,
                })
                continue
            kept.append(normalized)
        attempts.append({
            "match_level": match_level,
            "match_reason": reason,
            "fetched_row_count": len(rows or []),
            "row_count": len(kept),
        })
        collected.extend(kept)

    effective = _merge_quality_first(collected)
    effective_non_empty = [row for row in effective if not _is_empty_shell(row)]
    selected = (effective_non_empty or effective)[: int(limit)]
    profiles: list = []
    profile_error = ""
    if include_profiles and hasattr(db, "get_alpha_setup_profiles"):
        profiles_raw, profile_error = _safe_call(
            db.get_alpha_setup_profiles,
            config_id=config_id,
            ticker=ticker,
            sector=sector,
            side=side,
            horizon_class=horizon_class,
            market_regime=market_regime,
            trading_date=trading_date,
            limit=max(1, min(int(limit), 8)),
        )
        profiles = list(profiles_raw or []) if isinstance(profiles_raw, list) else []

    similar_values: list = []
    similar_error = ""
    if include_similar and hasattr(db, "get_similar_alpha_setup_action_values"):
        similar_raw, similar_error = _safe_call(
            db.get_similar_alpha_setup_action_values,
            config_id=config_id,
            ticker=ticker,
            sector=sector,
            side=side,
            horizon_class=horizon_class,
            market_regime=market_regime,
            setup_type=setup_type,
            trading_date=trading_date,
            limit=max(1, min(int(limit), 8)),
        )
        similar_values = []
        for row in (similar_raw or []) if isinstance(similar_raw, list) else []:
            if not isinstance(row, Mapping):
                continue
            if _consumer_scope(row) != "pm_learning":
                rejected.append({"id": row.get("id"), "reason": "non_pm_learning_scope"})
                continue
            similar_values.append(
                _normalize(row, match_level="similar", match_reason="similar_setup_samples")
            )
        if similar_values:
            selected = _merge_quality_first(list(selected) + similar_values)[: int(limit)]

    strategy_memory: dict = {}
    strategy_memory_error = ""
    if include_strategy_memory and hasattr(db, "get_strategy_memory"):
        strategy_raw, strategy_memory_error = _safe_call(
            db.get_strategy_memory,
            config_id=config_id,
            ticker=ticker,
            side=side,
            trading_date=trading_date,
            signal_combo=signal_combo,
        )
        strategy_memory = dict(strategy_raw or {}) if isinstance(strategy_raw, Mapping) else {}

    adaptive_policy_state: list = []
    adaptive_policy_safety_trace: dict = {}
    adaptive_policy_error = ""
    if include_adaptive_policy_state and hasattr(db, "get_adaptive_policy_state"):
        adaptive_raw, adaptive_policy_error = _safe_call(
            db.get_adaptive_policy_state,
            config_id=config_id,
            ticker=ticker,
            side=side,
            setup_type=setup_type,
            horizon_class=horizon_class,
            market_regime=market_regime,
            trading_date=trading_date,
        )
        adaptive_policy_state = list(adaptive_raw or []) if isinstance(adaptive_raw, list) else []
        adaptive_policy_state, adaptive_policy_safety_trace = filter_adaptive_policy_state_for_pm(adaptive_policy_state)

    provisional_policy_state: list = []
    provisional_policy_error = ""
    if include_provisional_policy_state and hasattr(db, "get_provisional_policy_state"):
        provisional_raw, provisional_policy_error = _safe_call(
            db.get_provisional_policy_state,
            config_id=config_id,
            ticker=ticker,
            side=side,
            setup_type=setup_type,
            horizon_class=horizon_class,
            trading_date=trading_date,
        )
        provisional_policy_state = list(provisional_raw or []) if isinstance(provisional_raw, list) else []

    source_status = {
        "alpha_setup_action_value": _memory_status(selected),
        "similar_alpha_setup_action_value": _memory_status(similar_values),
        "alpha_setup_profile": _memory_status(profiles),
        "strategy_memory": _memory_status(strategy_memory),
        "adaptive_policy_state": _memory_status(adaptive_policy_state),
        "provisional_policy_state": _memory_status(provisional_policy_state),
    }
    source_errors = {
        key: value
        for key, value in {
            "alpha_setup_profile": profile_error,
            "similar_alpha_setup_action_value": similar_error,
            "strategy_memory": strategy_memory_error,
            "adaptive_policy_state": adaptive_policy_error,
            "provisional_policy_state": provisional_policy_error,
        }.items()
        if value
    }
    summary = {
        "status": "available" if selected else "empty",
        "consumer_scope": "pm_learning",
        "visible_row_count": len(collected),
        "effective_row_count": len(selected),
        "profile_count": len(profiles),
        "similar_row_count": len(similar_values),
        "strategy_memory_status": source_status["strategy_memory"],
        "adaptive_policy_state_count": len(adaptive_policy_state),
        "provisional_policy_state_count": len(provisional_policy_state),
        "empty_shell_count": sum(1 for row in collected if _is_empty_shell(row)),
        "quality_first": True,
        "empty_history_cannot_block_real_history": True,
        "matched_levels": sorted({_text(row.get("retrieval_match_level"), "unknown") for row in selected}),
        "action_preferences": sorted({_text(row.get("action_preference")) for row in selected if _text(row.get("action_preference"))}),
        "reward_sources": sorted({_text(row.get("reward_source")) for row in selected if _text(row.get("reward_source"))}),
        "source_status": source_status,
        "source_errors": source_errors,
        "tool_boundary": "pm_research_memory_single_entrypoint",
        "does_not_output_trade_authority": True,
    }
    return {
        "effective_memory_summary": summary,
        "action_values": selected,
        "alpha_setup_profiles": profiles,
        "strategy_memory": strategy_memory,
        "adaptive_policy_state": adaptive_policy_state,
        "adaptive_policy_safety_trace": adaptive_policy_safety_trace,
        "provisional_policy_state": provisional_policy_state,
        "rejected_or_downgraded": rejected,
        "retrieval_attempts": attempts,
    }
