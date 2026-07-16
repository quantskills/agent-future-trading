from __future__ import annotations

"""Neutral-signal accountability diagnostics shared by reviewer and evaluation."""

from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Mapping


ANALYST_KEYS = ("technical", "fundamental", "commodity_news")
_EMPTY_TEXT = {"", "unknown", "none", "n/a", "null", "[]", "{}"}


def _json_signal_value(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value or "Neutral")


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
        return int(value)
    except Exception:
        return default


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text and text.lower() not in _EMPTY_TEXT else []


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, set)):
        return bool(_as_list(value))
    if isinstance(value, Mapping):
        return bool(value)
    return str(value).strip().lower() not in _EMPTY_TEXT


def _nested_dict(payload: Mapping[str, Any], key: str) -> Dict[str, Any]:
    value = payload.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def analyst_payloads_from_snapshot(snapshot: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    payloads: Dict[str, Dict[str, Any]] = {}
    contract = snapshot.get("signal_collection_contract")
    if not isinstance(contract, Mapping):
        return payloads
    for source in contract.get("source_contracts") or []:
        if not isinstance(source, Mapping):
            continue
        analyst = str(source.get("analyst") or "")
        value = source.get("action_evidence_contract")
        if analyst in ANALYST_KEYS and isinstance(value, Mapping):
            payloads[analyst] = dict(value)
    return payloads


def _business_quality(payload: Mapping[str, Any]) -> Dict[str, Any]:
    metadata = _nested_dict(payload, "metadata")
    quality = _nested_dict(metadata, "business_quality")
    return quality


def _data_quality(payload: Mapping[str, Any]) -> Dict[str, Any]:
    metadata = _nested_dict(payload, "metadata")
    quality = _nested_dict(metadata, "data_quality")
    if quality:
        return quality
    for key in ("fundamental_context", "technical_context", "news_context", "commodity_news_context"):
        context = _nested_dict(metadata, key)
        quality = _nested_dict(context, "data_quality")
        if quality:
            return quality
    return {}


def _risk_flags(payload: Mapping[str, Any]) -> List[str]:
    metadata = _nested_dict(payload, "metadata")
    values = _as_list(payload.get("risk_flags")) or _as_list(metadata.get("risk_flags"))
    for key in ("fundamental_context", "technical_context", "news_context", "commodity_news_context"):
        context = _nested_dict(metadata, key)
        values.extend(_as_list(context.get("risk_flags")))
    return sorted(set(values))


def _tradeability(payload: Mapping[str, Any]) -> str:
    metadata = _nested_dict(payload, "metadata")
    quality = _business_quality(payload)
    text = (
        payload.get("tradeability")
        or metadata.get("tradeability")
        or quality.get("tradeability")
        or payload.get("tradeability_reason")
        or ""
    )
    lowered = str(text).lower()
    if "low" in lowered:
        return "low"
    if "high" in lowered:
        return "high"
    if "medium" in lowered:
        return "medium"
    return "unknown"


def _neutral_contract(payload: Mapping[str, Any]) -> Dict[str, Any]:
    bucket = str(payload.get("neutral_opportunity_bucket") or "unknown")
    trigger = str(payload.get("neutral_trigger_condition") or "")
    counterfactual_side = str(payload.get("counterfactual_side") or "flat").lower()
    if counterfactual_side not in {"long", "short", "flat"}:
        counterfactual_side = "flat"
    priority = str(payload.get("neutral_watchlist_priority") or "none")
    opportunity_state = str(payload.get("opportunity_state") or "no_opportunity").lower()
    if opportunity_state not in {
        "no_opportunity",
        "watch_for_trigger",
        "probe_candidate",
        "tradeable_candidate",
        "risk_reduction_candidate",
    }:
        opportunity_state = "no_opportunity"
    return {
        "bucket": bucket,
        "trigger_condition": trigger,
        "counterfactual_side": counterfactual_side,
        "watchlist_priority": priority,
        "tracking_only": True,
        "opportunity_state": opportunity_state,
        "trigger_valid": bool(payload.get("trigger_valid")),
        "invalidation_present": bool(payload.get("invalidation_present")),
        "entry_trigger": str(payload.get("entry_trigger") or ""),
        "invalidation_condition": str(payload.get("invalidation_condition") or ""),
        "invalidation_level": payload.get("invalidation_level"),
        "atr_stop_distance": payload.get("atr_stop_distance"),
    }


def _data_gap_score(payload: Mapping[str, Any]) -> float:
    quality = _data_quality(payload)
    coverage = max(
        _safe_float(payload.get("data_coverage_score")),
        _safe_float(quality.get("coverage_ratio")),
        _safe_float(quality.get("factor_freshness_score")),
        _safe_float(_business_quality(payload).get("data_coverage_score")),
    )
    return coverage


def _has_data_gap(payload: Mapping[str, Any], *, coverage_threshold: float) -> bool:
    metadata = _nested_dict(payload, "metadata")
    quality = _data_quality(payload)
    text = " ".join(
        [
            str(payload.get("neutral_reason") or ""),
            str(payload.get("tradeability_reason") or ""),
            str(payload.get("counter_evidence") or ""),
            " ".join(_as_list(payload.get("missing_evidence"))),
        ]
    ).lower()
    count_keys = (
        "missing_file_count",
        "empty_frame_count",
        "no_data_before_count",
        "stale_indicator_count",
        "near_stale_indicator_count",
        "low_confidence_indicator_count",
    )
    missing_counts = sum(_safe_int(metadata.get(key)) + _safe_int(quality.get(key)) for key in count_keys)
    stale_ratio = max(_safe_float(metadata.get("stale_ratio")), _safe_float(quality.get("stale_ratio")))
    evidence_quality = str(payload.get("evidence_quality") or "").lower()
    return (
        bool(_as_list(payload.get("missing_evidence")))
        or missing_counts > 0
        or stale_ratio >= 0.30
        or (0.0 < _data_gap_score(payload) < coverage_threshold)
        or evidence_quality in {"low", "missing"}
        or any(token in text for token in ("insufficient", "missing", "data", "coverage", "stale", "证据不足", "缺失"))
    )


def _has_risk_or_conflict(payload: Mapping[str, Any]) -> bool:
    text = " ".join(
        [
            str(payload.get("neutral_reason") or ""),
            str(payload.get("counter_evidence") or ""),
            str(payload.get("tradeability_reason") or ""),
            " ".join(_as_list(payload.get("conflicting_factors"))),
            " ".join(_risk_flags(payload)),
        ]
    ).lower()
    reward_risk = payload.get("reward_risk_ratio")
    low_reward_risk = reward_risk is not None and _safe_float(reward_risk, 99.0) < 1.0
    return (
        bool(_as_list(payload.get("conflicting_factors")))
        or bool(_risk_flags(payload))
        or _tradeability(payload) == "low"
        or low_reward_risk
        or any(token in text for token in ("conflict", "mixed", "range", "risk", "reward", "horizon", "low tradeability", "震荡", "冲突"))
    )


def _directional_consensus(snapshot: Mapping[str, Any], neutral_analyst: str) -> Dict[str, Any]:
    payloads = analyst_payloads_from_snapshot(snapshot)
    counts: Counter = Counter()
    supporters: List[str] = []
    for analyst, payload in payloads.items():
        if analyst == neutral_analyst:
            continue
        signal = _json_signal_value(payload.get("signal"))
        if signal not in {"Bullish", "Bearish"}:
            continue
        confidence = max(
            _safe_float(payload.get("effective_confidence")),
            _safe_float(payload.get("confidence")),
            _safe_float(_business_quality(payload).get("score")),
        )
        if confidence < 0.45:
            continue
        counts[signal] += 1
        supporters.append(f"{analyst}:{signal}")
    if not counts:
        return {"signal": "Neutral", "support_count": 0, "supporters": []}
    signal, support_count = counts.most_common(1)[0]
    return {"signal": signal, "support_count": int(support_count), "supporters": supporters}


def _missing_required_fields(payload: Mapping[str, Any], required_fields: Iterable[str]) -> List[str]:
    missing: List[str] = []
    for field in required_fields:
        if not _present(payload.get(field)):
            missing.append(str(field))
    return missing


def classify_neutral_signal(
    *,
    analyst: str,
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    cfg: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    account_cfg = (((cfg or {}).get("signal_quality") or {}).get("neutral_accountability") or {})
    required_fields = account_cfg.get("required_neutral_fields") or (
        "neutral_reason",
        "missing_evidence",
        "conflicting_factors",
        "would_change_view_if",
    )
    coverage_threshold = _safe_float(account_cfg.get("evidence_gap_data_coverage_threshold"), 0.45)
    consensus_threshold = _safe_int(account_cfg.get("consensus_support_threshold"), 2)

    data_gap = _has_data_gap(payload, coverage_threshold=coverage_threshold)
    risk_or_conflict = _has_risk_or_conflict(payload)
    consensus = _directional_consensus(snapshot, analyst)
    contract = _neutral_contract(payload)
    explicit_conditional_watchlist = (
        contract["opportunity_state"] == "watch_for_trigger"
        and contract["trigger_valid"] is False
        and contract["invalidation_present"] is True
        and _present(contract["entry_trigger"])
        and contract["bucket"] in {"watchlist_trigger", "horizon_mismatch", "accountable_observation"}
        and _present(contract["trigger_condition"])
    )
    against_consensus = consensus["signal"] in {"Bullish", "Bearish"} and consensus["support_count"] >= consensus_threshold
    missing_fields = _missing_required_fields(payload, required_fields)
    blocking_missing_fields = [
        field
        for field in missing_fields
        if (
            not explicit_conditional_watchlist
            and (field != "conflicting_factors" or not data_gap)
        )
        or field not in {"missing_evidence", "conflicting_factors"}
    ]

    if blocking_missing_fields:
        category = "unaccountable_neutral"
        rationale = "required neutral accountability fields are missing"
    elif data_gap and not risk_or_conflict:
        category = "evidence_gap_conservative"
        rationale = "Neutral is mainly driven by missing, stale, or low-coverage evidence"
    elif data_gap and against_consensus:
        category = "evidence_gap_conservative"
        rationale = "Neutral has missing evidence while other analysts are directionally aligned"
    elif risk_or_conflict:
        category = "reasonable_avoidance"
        rationale = "Neutral is backed by explicit conflict, risk, low tradeability, or reward/risk limits"
    elif explicit_conditional_watchlist:
        category = "conditional_watchlist"
        rationale = "Neutral defines a conditional opportunity to watch, but does not yet authorize a trade"
    elif against_consensus:
        category = "conservative_against_consensus"
        rationale = "Neutral lacks a clear risk or data-gap reason while other analysts are aligned"
    else:
        category = "accountable_observation"
        rationale = "Neutral is accountable but not yet attributable to risk or data gaps"

    return {
        "analyst": analyst,
        "category": category,
        "rationale": rationale,
        "missing_fields": missing_fields,
        "blocking_missing_fields": blocking_missing_fields,
        "neutral_reason": str(payload.get("neutral_reason") or ""),
        "missing_evidence": _as_list(payload.get("missing_evidence")),
        "conflicting_factors": _as_list(payload.get("conflicting_factors")),
        "would_change_view_if": str(payload.get("would_change_view_if") or ""),
        "opportunity_cost_risk": str(payload.get("opportunity_cost_risk") or ""),
        "neutral_opportunity_bucket": contract["bucket"],
        "neutral_trigger_condition": contract["trigger_condition"],
        "counterfactual_side": contract["counterfactual_side"],
        "neutral_watchlist_priority": contract["watchlist_priority"],
        "opportunity_state": contract["opportunity_state"],
        "trigger_valid": contract["trigger_valid"],
        "invalidation_present": contract["invalidation_present"],
        "entry_trigger": contract["entry_trigger"],
        "invalidation_condition": contract["invalidation_condition"],
        "invalidation_level": contract["invalidation_level"],
        "atr_stop_distance": contract["atr_stop_distance"],
        "business_quality_score": max(
            _safe_float(payload.get("business_quality_score")),
            _safe_float(_business_quality(payload).get("score")),
        ),
        "data_coverage_score": _data_gap_score(payload),
        "tradeability": _tradeability(payload),
        "risk_flags": _risk_flags(payload),
        "directional_consensus": consensus,
    }


def build_neutral_accountability_summary(
    recommendations: Iterable[Mapping[str, Any]],
    cfg: Mapping[str, Any] | None = None,
    *,
    max_examples: int = 12,
) -> Dict[str, Any]:
    total_signals = 0
    neutral_count = 0
    accountability_complete = 0
    category_counts: Counter = Counter()
    missing_field_counts: Counter = Counter()
    by_analyst: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"signal_count": 0, "neutral_count": 0, "category_counts": Counter()}
    )
    examples: List[Dict[str, Any]] = []

    for recommendation in recommendations:
        snapshot = recommendation.get("signal_snapshot") if isinstance(recommendation.get("signal_snapshot"), Mapping) else {}
        payloads = analyst_payloads_from_snapshot(snapshot)
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "")
        rec_id = str(recommendation.get("id") or "")
        for analyst, payload in payloads.items():
            signal = _json_signal_value(payload.get("signal"))
            total_signals += 1
            by_analyst[analyst]["signal_count"] += 1
            if signal != "Neutral":
                continue
            neutral_count += 1
            by_analyst[analyst]["neutral_count"] += 1
            item = classify_neutral_signal(analyst=analyst, payload=payload, snapshot=snapshot, cfg=cfg)
            category = item["category"]
            category_counts[category] += 1
            by_analyst[analyst]["category_counts"][category] += 1
            bucket = str(item.get("neutral_opportunity_bucket") or "")
            if bucket:
                by_analyst[analyst].setdefault("opportunity_bucket_counts", Counter())[bucket] += 1
            if item.get("neutral_watchlist_priority") in {"medium", "high"}:
                by_analyst[analyst].setdefault("conditional_watchlist_count", 0)
                by_analyst[analyst]["conditional_watchlist_count"] += 1
            blocking_missing_fields = item.get("blocking_missing_fields") or []
            if not blocking_missing_fields:
                accountability_complete += 1
            missing_field_counts.update(blocking_missing_fields)
            if len(examples) < max_examples and category in {
                "evidence_gap_conservative",
                "unaccountable_neutral",
                "conservative_against_consensus",
            }:
                examples.append(
                    {
                        "ticker": ticker,
                        "recommendation_id": rec_id,
                        "analyst": analyst,
                        "category": category,
                        "rationale": item["rationale"],
                        "neutral_reason": item["neutral_reason"],
                        "missing_fields": item["missing_fields"],
                        "blocking_missing_fields": blocking_missing_fields,
                        "missing_evidence": item["missing_evidence"][:5],
                        "would_change_view_if": item["would_change_view_if"],
                        "neutral_opportunity_bucket": item["neutral_opportunity_bucket"],
                        "neutral_trigger_condition": item["neutral_trigger_condition"],
                        "counterfactual_side": item["counterfactual_side"],
                        "neutral_watchlist_priority": item["neutral_watchlist_priority"],
                        "opportunity_state": item["opportunity_state"],
                        "trigger_valid": item["trigger_valid"],
                        "invalidation_present": item["invalidation_present"],
                        "entry_trigger": item["entry_trigger"],
                        "invalidation_condition": item["invalidation_condition"],
                        "directional_consensus": item["directional_consensus"],
                    }
                )

    by_analyst_payload = {}
    for analyst, payload in by_analyst.items():
        analyst_neutral_count = int(payload["neutral_count"])
        by_analyst_payload[analyst] = {
            "signal_count": int(payload["signal_count"]),
            "neutral_count": analyst_neutral_count,
            "neutral_ratio": analyst_neutral_count / payload["signal_count"] if payload["signal_count"] else 0.0,
            "category_counts": dict(payload["category_counts"]),
            "opportunity_bucket_counts": dict(payload.get("opportunity_bucket_counts", Counter())),
            "conditional_watchlist_count": int(payload.get("conditional_watchlist_count", 0) or 0),
        }

    action_items: List[str] = []
    if category_counts.get("unaccountable_neutral", 0):
        action_items.append("Fix analyst contract output: Neutral must include reason, missing evidence, conflicts, and change condition.")
    if category_counts.get("evidence_gap_conservative", 0):
        action_items.append("Improve missing/stale evidence coverage before changing risk limits or forcing direction.")
    if category_counts.get("conservative_against_consensus", 0):
        action_items.append("Review analyst prompt/thresholds where Neutral disagrees with aligned directional evidence without a risk reason.")
    if category_counts.get("conditional_watchlist", 0):
        action_items.append("Track conditional Neutral opportunities with forward Counterfactual results; only convert them when today's trigger and invalidation are clear.")

    return {
        "total_signal_count": total_signals,
        "neutral_count": neutral_count,
        "directional_count": total_signals - neutral_count,
        "neutral_ratio": neutral_count / total_signals if total_signals else 0.0,
        "accountability_complete_count": accountability_complete,
        "accountability_complete_rate": accountability_complete / neutral_count if neutral_count else 1.0,
        "category_counts": dict(category_counts),
        "missing_field_counts": dict(missing_field_counts),
        "by_analyst": by_analyst_payload,
        "examples": examples,
        "action_items": action_items,
    }

