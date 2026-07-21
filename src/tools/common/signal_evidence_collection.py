"""Deterministic signal collection for the decision team.

The signal collector is not an analyst and does not call an LLM.  It preserves
the structured evidence produced by analysts and emits a single
``signal_collection_contract`` for PM consumption.
"""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.common.execution_trigger_semantics import (
    TECHNICAL_ENTRY_PROFILES,
    canonical_entry_trigger,
    is_canonical_entry_trigger,
    normalize_execution_profile,
)
from tools.common.evidence_fusion_semantics import build_signal_collection_fusion_summary
from tools.common.final_action_semantics import FORBIDDEN_ANALYST_TRADE_AUTHORITY_KEYS


ANALYST_ORDER = ("technical", "fundamental", "commodity_news")
SCC_CONTRACT_VERSION = "agentquant.signal_collection.v1"
ACTION_EVIDENCE_CONTRACT_VERSION = "agentquant.action_evidence.v1"
ACTION_EVIDENCE_SIGNALS = {"Bullish": "long", "Bearish": "short", "Neutral": "flat"}
ACTION_EVIDENCE_OPPORTUNITY_STATES = {
    "no_opportunity",
    "watch_for_trigger",
    "probe_candidate",
    "tradeable_candidate",
    "risk_reduction_candidate",
}
ACTION_EVIDENCE_EXCLUDED_SIGNAL_FIELDS = {
    "contract_version",
    "agent_name",
    "justification",
    "determinism_mode",
    "llm_provider",
    "llm_model",
    "source_artifacts",
    "validation_errors",
    "decision_horizon",
    "execution_horizon",
    "validation_horizon",
    "research_contract_version",
    "message_contract_version",
    "similar_past_cases",
    "metadata",
}
ACTION_EVIDENCE_EXTRA_FIELDS = {
    "contract_version",
    "analyst",
    "sector",
    "side",
    "setup_quality_ok",
    "current_trigger_confirmed",
    "invalidation_condition",
    "learning_scope",
    "product_profile_evidence",
    "fusion_evidence",
    "data_usage_summary",
}
ACTION_EVIDENCE_ALLOWED_FIELDS = (
    set(AnalystSignal.model_fields)
    - ACTION_EVIDENCE_EXCLUDED_SIGNAL_FIELDS
    | ACTION_EVIDENCE_EXTRA_FIELDS
)
ACTION_EVIDENCE_REQUIRED_FIELDS = {
    "contract_version",
    "analyst",
    "side",
    "signal",
    "confidence",
    "opportunity_type",
    "opportunity_state",
    "setup_type",
    "setup_quality_ok",
    "trigger_valid",
    "current_trigger_confirmed",
    "invalidation_present",
    "entry_trigger",
    "entry_timing_signal",
    "evidence_role",
    "exit_hint",
    "horizon_class",
    "expected_horizon_days",
    "market_regime",
    "evidence_quality",
    "evidence_strength",
    "evidence_freshness",
    "confirmation_requirements",
    "missing_evidence",
    "current_evidence_conflict",
    "factor_focus",
    "no_lookahead_status",
    "data_usage_summary",
    "learning_scope",
    "product_profile_evidence",
    "fusion_evidence",
}
ACTION_EVIDENCE_TEXT_FIELDS = {
    "contract_version",
    "analyst",
    "side",
    "signal",
    "opportunity_type",
    "opportunity_state",
    "setup_type",
    "entry_trigger",
    "entry_timing_signal",
    "evidence_role",
    "exit_hint",
    "horizon_class",
    "market_regime",
    "evidence_quality",
    "evidence_strength",
    "evidence_freshness",
    "no_lookahead_status",
}
ACTION_EVIDENCE_BOOLEAN_FIELDS = {
    "setup_quality_ok",
    "trigger_valid",
    "current_trigger_confirmed",
    "invalidation_present",
}
ACTION_EVIDENCE_LIST_FIELDS = {
    "confirmation_requirements",
    "missing_evidence",
    "current_evidence_conflict",
    "factor_focus",
}
ACTION_EVIDENCE_MAPPING_FIELDS = {
    "data_usage_summary",
    "learning_scope",
    "product_profile_evidence",
    "fusion_evidence",
}
ACTION_EVIDENCE_FORBIDDEN_INTERNAL_FIELDS = {
    "prompt",
    "raw_prompt",
    "response",
    "raw_response",
    "internal_state",
    "internal_reasoning",
    "hidden_context",
    "intermediate_state",
    "unvalidated_tool_result",
    "report_sections",
    "llm_path",
    "adaptive_params",
    "technical_parameter_calibration",
    "reviewer_learning_context",
    "file_path",
    "encoding",
    "fetcher_index_map_path",
    "local_feather_dir",
    "catalog_path",
    "index_map_parse_errors",
    "analyst_pm_instruction",
}
ACTION_EVIDENCE_DATA_SOURCE_BASE_FIELDS = {
    "source",
    "dataset",
    "available",
    "used_in_signal",
    "pre_open_only",
    "info_cutoff",
}
ACTION_EVIDENCE_DATA_SOURCE_FIELDS = {
    "pandaai_market": ACTION_EVIDENCE_DATA_SOURCE_BASE_FIELDS
    | {"latest_data_date", "row_count", "fields_used", "indicators_used"},
    "finoview_fundamental": ACTION_EVIDENCE_DATA_SOURCE_BASE_FIELDS
    | {
        "configured_indicator_count",
        "loaded_indicator_count",
        "missing_like_count",
        "stale_indicator_count",
        "near_stale_indicator_count",
        "coverage_ratio",
        "stale_ratio",
        "factor_groups",
        "freshness_score",
        "no_lookahead_status",
        "local_availability_audit",
        "coverage_status",
        "supports_trade_setup",
        "runtime_data_boundary",
    },
    "pandaai_extra": ACTION_EVIDENCE_DATA_SOURCE_BASE_FIELDS
    | {
        "reference_date",
        "lookback_days",
        "feature_count",
        "record_counts",
        "feature_status",
        "data_missing",
        "error_count",
    },
    "finoview_news_txt": ACTION_EVIDENCE_DATA_SOURCE_BASE_FIELDS
    | {
        "news_cutoff",
        "raw_block_count",
        "parsed_news_count",
        "selected_news_count",
        "latest_news_date",
        "freshness_score",
        "relevance_score",
    },
    "pandaai_pre_open_reference": ACTION_EVIDENCE_DATA_SOURCE_BASE_FIELDS
    | {"missing_data", "data_quality_flags", "reason"},
}
ACTION_EVIDENCE_DATA_SOURCE_IDENTITIES = {
    "pandaai_market": ("PandaAI", "daily_continuous_candles"),
    "finoview_fundamental": ("Finoview", "local_feather_fundamental"),
    "pandaai_extra": ("PandaAI", "futures_derivative_snapshot"),
    "finoview_news_txt": ("Finoview", "local_news_txt"),
    "pandaai_pre_open_reference": (
        "PandaAI",
        "previous_trading_day_main_contract_quote",
    ),
}
ACTION_EVIDENCE_DATA_USAGE_FIELDS = {
    "ticker",
    "trading_date",
    "analyst",
    "data_available",
    "sources",
}
ACTION_EVIDENCE_DATA_SOURCE_INTEGER_FIELDS = {
    "row_count",
    "configured_indicator_count",
    "loaded_indicator_count",
    "missing_like_count",
    "stale_indicator_count",
    "near_stale_indicator_count",
    "lookback_days",
    "feature_count",
    "error_count",
    "raw_block_count",
    "parsed_news_count",
    "selected_news_count",
}
ACTION_EVIDENCE_DATA_SOURCE_NUMBER_FIELDS = {
    "coverage_ratio",
    "stale_ratio",
    "freshness_score",
    "relevance_score",
}
ACTION_EVIDENCE_DATA_SOURCE_LIST_FIELDS = {
    "fields_used",
    "indicators_used",
    "data_missing",
    "missing_data",
    "data_quality_flags",
}
ACTION_EVIDENCE_DATA_SOURCE_MAPPING_FIELDS = {
    "factor_groups",
    "local_availability_audit",
    "record_counts",
    "feature_status",
}
FINOVIEW_AVAILABILITY_ALLOWED_FIELDS = {
    "runtime_data_boundary",
    "index_declared_count",
    "local_feather_count",
    "catalog_entry_count",
    "known_catalog_factor_count",
    "required_groups",
    "covered_required_groups",
    "missing_required_groups",
    "missing_feather_from_index_map_count",
    "local_feather_not_in_index_map_count",
    "catalog_missing_or_unknown_count",
    "local_vs_index_ratio",
    "known_catalog_ratio",
    "coverage_status",
    "supports_fundamental_trade_setup",
    "no_future_data",
    "not_product_rule",
    "index_map_parse_error_count",
}

SCC_ALLOWED_TOP_LEVEL_FIELDS = {
    "contract_version",
    "source_agent",
    "ticker",
    "trading_date",
    "source_contracts",
    "evidence_items",
    "dominant_side",
    "side_consensus",
    "trigger_status",
    "supporting_analysts",
    "opposing_analysts",
    "neutral_analysts",
    "evidence_strength",
    "evidence_conflict_level",
    "confirmation_requirements",
    "missing_evidence",
    "data_quality_flags",
    "setup_types",
    "horizon_scope",
    "invalidation_summary",
    "evidence_fusion",
    "collector_decision_boundary",
}
SCC_SOURCE_CONTRACT_FIELDS = {
    "analyst",
    "action_evidence_contract",
    "signal_record_id",
}
SCC_EVIDENCE_ITEM_FIELDS = {
    "analyst",
    "side",
    "confidence",
    "signal",
    "opportunity_state",
    "trigger_valid",
    "current_trigger_confirmed",
    "trigger_status",
    "entry_trigger",
    "setup_type",
    "setup_quality_ok",
    "horizon_class",
    "market_regime",
    "evidence_quality",
    "current_evidence_conflict",
    "missing_evidence",
    "evidence_strength",
    "evidence_freshness",
    "confirmation_requirements",
    "product_profile_id",
    "product_profile_used",
    "product_profile_analysis_boundary",
}
SCC_EVIDENCE_FUSION_FIELDS = {
    "contract_version",
    "evidence_strength_by_analyst",
    "evidence_freshness_by_analyst",
    "evidence_alignment_state",
    "cross_analyst_conflicts",
    "dominant_opposing_evidence",
    "confirmation_requirements",
    "missing_evidence",
    "multi_evidence_consensus_score",
    "fusion_boundary",
}

SCC_FORBIDDEN_TRADE_FIELDS = FORBIDDEN_ANALYST_TRADE_AUTHORITY_KEYS | {
    "opportunity_score",
    "rank_score",
    "position_sizing_result",
    "capital_deployment",
    "pm_six_step_trace",
}


class _SCCEvidenceSignal(SimpleNamespace):
    """Read-only-shaped PM view reconstructed only from formal SCC evidence."""

    def model_dump(self) -> dict:
        return dict(vars(self))


def _agent_name(signal: Any) -> str:
    return str(getattr(signal, "agent_name", "") or "").strip()


def _metadata(signal: Any) -> dict:
    value = getattr(signal, "metadata", {}) or {}
    return dict(value) if isinstance(value, Mapping) else {}


def _action_contract(signal: Any) -> dict:
    metadata = _metadata(signal)
    value = metadata.get("action_evidence_contract")
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "ok"}
    return bool(value)


def _list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [value]


def _side_from_contract(contract: Mapping[str, Any]) -> str:
    side = _text(contract.get("side")).lower()
    if side in {"long", "short", "flat"}:
        return side
    raise ValueError(f"signal_collection_invalid_action_evidence_side:{side or 'missing'}")


def _confidence(contract: Mapping[str, Any]) -> float:
    value = contract.get("confidence", 0.0)
    try:
        parsed = float(value if value is not None else 0.0)
    except (TypeError, ValueError):
        parsed = 0.0
    return max(0.0, min(1.0, parsed))


_NON_ENTRY_TRIGGER_VALUES = {
    "",
    "unknown",
    "none",
    "n/a",
    "null",
    "wait",
    "wait_for_trigger",
    "technical_price_trigger",
    "fundamental_anchor",
    "news_event_trigger",
    "direction_anchor",
    "initial_or_rebalance",
}


_NON_ENTRY_TRIGGER_PHRASES = (
    "no current entry trigger",
    "no active entry trigger",
    "no valid entry trigger",
    "no specific entry trigger",
    "no concrete entry trigger",
    "entry trigger is not established",
    "entry trigger is not defined",
    "entry trigger is unavailable",
    "entry trigger is absent",
    "entry trigger is missing",
    "without an entry trigger",
    "without entry trigger",
    "尚无入场触发",
    "没有入场触发",
    "未形成入场触发",
)

_FUTURE_DATA_TRIGGER_MARKERS = (
    "next weekly",
    "next monthly",
    "weekly report",
    "weekly data",
    "monthly report",
    "monthly data",
    "inventory report",
    "government report",
    "future release",
    "future data",
    "下周数据",
    "下月数据",
    "周度报告",
    "月度报告",
    "未来发布",
)

_TRADER_OBSERVABLE_TRIGGER_MARKERS = (
    "15-minute",
    "15 minute",
    "15m",
    "price",
    "close",
    "open",
    "break",
    "breakout",
    "breakdown",
    "above",
    "below",
    "cross",
    "hold",
    "pullback",
    "rebound",
    "reversal",
    "stabiliz",
    "vwap",
    "volume",
    "open interest",
    "support",
    "resistance",
    "moving average",
    "momentum",
    "macd",
    "rsi",
    "adx",
    "high",
    "low",
    "settlement",
    "价格",
    "收盘",
    "开盘",
    "突破",
    "跌破",
    "站上",
    "企稳",
    "回踩",
    "反转",
    "成交量",
    "持仓量",
    "支撑",
    "阻力",
    "均线",
)


def has_concrete_entry_trigger(value: Any) -> bool:
    """Return whether an entry condition is concrete and observable by Trader."""
    if is_canonical_entry_trigger(value):
        return True
    text = _text(value).strip().lower()
    if text in _NON_ENTRY_TRIGGER_VALUES:
        return False
    if text.endswith("_trigger") or text.endswith("_anchor"):
        return False
    observable = any(marker in text for marker in _TRADER_OBSERVABLE_TRIGGER_MARKERS)
    if any(phrase in text for phrase in _NON_ENTRY_TRIGGER_PHRASES) and not observable:
        return False
    if any(marker in text for marker in _FUTURE_DATA_TRIGGER_MARKERS) and not observable:
        return False
    return observable


def _optional_contract_number(
    contract: Mapping[str, Any],
    field: str,
    *,
    positive: bool = False,
) -> bool:
    value = contract.get(field)
    if value is None:
        return False
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"action_evidence_contract_invalid_number:{field}")
    if positive and float(value) <= 0.0:
        raise ValueError(f"action_evidence_contract_invalid_positive_number:{field}")
    return True


def _has_canonical_invalidation_proof(contract: Mapping[str, Any]) -> bool:
    condition = _text(contract.get("invalidation_condition")).strip().lower()
    condition_present = condition not in {"", "unknown", "none", "n/a", "null"}
    level_present = _optional_contract_number(contract, "invalidation_level")
    atr_present = _optional_contract_number(
        contract,
        "atr_stop_distance",
        positive=True,
    )
    return condition_present or level_present or atr_present


def validate_action_evidence_contract(
    contract: Any,
    *,
    analyst: str | None = None,
) -> dict:
    """Validate the one analyst evidence contract shared by all three boundaries."""
    if not isinstance(contract, dict) or not contract:
        raise ValueError("action_evidence_contract_missing")
    forbidden = _nested_forbidden_paths(contract)
    if forbidden:
        raise ValueError(
            "action_evidence_contract_forbidden_trade_field:" + ",".join(forbidden)
        )
    internal = _nested_forbidden_paths(
        contract,
        forbidden_keys=ACTION_EVIDENCE_FORBIDDEN_INTERNAL_FIELDS,
    )
    if internal:
        raise ValueError(
            "action_evidence_contract_forbidden_internal_field:" + ",".join(internal)
        )
    if contract.get("contract_version") != ACTION_EVIDENCE_CONTRACT_VERSION:
        raise ValueError("action_evidence_contract_invalid_version")
    extras = sorted(set(contract) - ACTION_EVIDENCE_ALLOWED_FIELDS)
    if extras:
        raise ValueError("action_evidence_contract_unregistered_field:" + ",".join(extras))
    missing_required = sorted(ACTION_EVIDENCE_REQUIRED_FIELDS - set(contract))
    if missing_required:
        raise ValueError(
            "action_evidence_contract_missing_required_field:" + ",".join(missing_required)
        )
    for field in ACTION_EVIDENCE_TEXT_FIELDS:
        if not isinstance(contract.get(field), str):
            raise ValueError(f"action_evidence_contract_invalid_text:{field}")
    for field in ACTION_EVIDENCE_BOOLEAN_FIELDS:
        if not isinstance(contract.get(field), bool):
            raise ValueError(f"action_evidence_contract_invalid_boolean:{field}")
    for field in ACTION_EVIDENCE_LIST_FIELDS:
        if not isinstance(contract.get(field), list):
            raise ValueError(f"action_evidence_contract_invalid_list:{field}")
    for field in ACTION_EVIDENCE_MAPPING_FIELDS:
        if not isinstance(contract.get(field), Mapping):
            raise ValueError(f"action_evidence_contract_invalid_mapping:{field}")
    expected_horizon_days = contract.get("expected_horizon_days")
    if isinstance(expected_horizon_days, bool) or not isinstance(expected_horizon_days, int):
        raise ValueError("action_evidence_contract_invalid_integer:expected_horizon_days")
    contract_analyst = _text(contract.get("analyst"))
    if contract_analyst not in ANALYST_ORDER:
        raise ValueError("action_evidence_contract_invalid_analyst")
    if analyst is not None and contract_analyst != _text(analyst):
        raise ValueError("action_evidence_contract_analyst_mismatch")

    signal = _text(contract.get("signal"))
    side = _text(contract.get("side")).lower()
    expected_side = ACTION_EVIDENCE_SIGNALS.get(signal)
    if expected_side is None:
        raise ValueError("action_evidence_contract_invalid_signal")
    if side != expected_side:
        raise ValueError("action_evidence_contract_signal_side_mismatch")

    try:
        confidence = float(contract.get("confidence"))
    except (TypeError, ValueError):
        raise ValueError("action_evidence_contract_invalid_confidence") from None
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("action_evidence_contract_confidence_out_of_range")

    opportunity_state = _text(contract.get("opportunity_state")).lower()
    if opportunity_state not in ACTION_EVIDENCE_OPPORTUNITY_STATES:
        raise ValueError("action_evidence_contract_invalid_opportunity_state")
    entry_trigger_present = has_concrete_entry_trigger(contract.get("entry_trigger"))
    evidence_role = _text(contract.get("evidence_role"))
    entry_timing_signal = normalize_execution_profile(
        contract.get("entry_timing_signal")
    )
    raw_entry_timing_signal = _text(contract.get("entry_timing_signal")).lower()
    if raw_entry_timing_signal and not entry_timing_signal:
        raise ValueError("action_evidence_contract_invalid_entry_timing_signal")
    if contract_analyst == "technical" and evidence_role != "entry_timing":
        raise ValueError("action_evidence_contract_technical_role_invalid")
    if contract_analyst == "fundamental" and evidence_role != "direction_context":
        raise ValueError("action_evidence_contract_fundamental_role_invalid")
    if contract_analyst == "commodity_news" and evidence_role != "event_catalyst":
        raise ValueError("action_evidence_contract_news_role_invalid")
    if opportunity_state != "risk_reduction_candidate":
        executable_state = opportunity_state in {
            "watch_for_trigger",
            "probe_candidate",
            "tradeable_candidate",
        }
        execution_side = (
            _text(contract.get("counterfactual_side")).lower()
            if signal == Signal.NEUTRAL.value
            else side
        )
        if contract_analyst == "fundamental":
            if executable_state or entry_timing_signal or _text(contract.get("entry_trigger")):
                raise ValueError(
                    "action_evidence_contract_fundamental_execution_claim_forbidden"
                )
        elif contract_analyst == "technical":
            if executable_state:
                if entry_timing_signal not in TECHNICAL_ENTRY_PROFILES:
                    raise ValueError(
                        "action_evidence_contract_technical_profile_missing"
                    )
                if _text(contract.get("entry_trigger")) != canonical_entry_trigger(
                    entry_timing_signal,
                    execution_side,
                ):
                    raise ValueError(
                        "action_evidence_contract_entry_trigger_not_canonical"
                    )
            elif entry_timing_signal or _text(contract.get("entry_trigger")):
                raise ValueError(
                    "action_evidence_contract_no_opportunity_execution_claim"
                )
        elif contract_analyst == "commodity_news":
            if executable_state:
                if opportunity_state == "watch_for_trigger":
                    raise ValueError(
                        "action_evidence_contract_news_watch_execution_forbidden"
                    )
                if entry_timing_signal != "event_immediate":
                    raise ValueError(
                        "action_evidence_contract_news_profile_invalid"
                    )
                if _text(contract.get("entry_trigger")) != canonical_entry_trigger(
                    entry_timing_signal,
                    execution_side,
                ):
                    raise ValueError(
                        "action_evidence_contract_entry_trigger_not_canonical"
                    )
            elif entry_timing_signal or _text(contract.get("entry_trigger")):
                raise ValueError(
                    "action_evidence_contract_no_opportunity_execution_claim"
                )
    invalidation_proof = _has_canonical_invalidation_proof(contract)
    if contract.get("invalidation_present") is True and not invalidation_proof:
        raise ValueError("action_evidence_contract_invalidation_proof_missing")
    if contract.get("invalidation_present") is False and invalidation_proof:
        raise ValueError("action_evidence_contract_invalidation_flag_mismatch")
    if signal == Signal.NEUTRAL.value and opportunity_state in {
        "probe_candidate",
        "tradeable_candidate",
    }:
        raise ValueError("action_evidence_contract_neutral_candidate_invalid")
    if opportunity_state in {"probe_candidate", "tradeable_candidate"}:
        if not entry_trigger_present:
            raise ValueError("action_evidence_contract_candidate_missing_entry_trigger")
        if contract.get("trigger_valid") is not True:
            raise ValueError("action_evidence_contract_candidate_without_current_trigger")
        if contract.get("current_trigger_confirmed") is not True:
            raise ValueError("action_evidence_contract_candidate_without_current_confirmation")
        if contract.get("invalidation_present") is not True:
            raise ValueError("action_evidence_contract_trade_setup_missing_invalidation")
    if opportunity_state == "watch_for_trigger":
        if not entry_trigger_present:
            raise ValueError("action_evidence_contract_watch_missing_entry_trigger")
        if contract.get("invalidation_present") is not True:
            raise ValueError("action_evidence_contract_trade_setup_missing_invalidation")
        if contract.get("trigger_valid") is not False or contract.get("current_trigger_confirmed") is not False:
            raise ValueError("action_evidence_contract_watch_trigger_already_confirmed")
        if signal == Signal.NEUTRAL.value:
            if _text(contract.get("counterfactual_side")).lower() not in {"long", "short"}:
                raise ValueError("action_evidence_contract_neutral_watch_missing_side")
    data_usage = contract["data_usage_summary"]
    usage_extras = sorted(set(data_usage) - ACTION_EVIDENCE_DATA_USAGE_FIELDS)
    if usage_extras:
        raise ValueError(
            "action_evidence_contract_unregistered_data_usage_field:"
            + ",".join(usage_extras)
        )
    for field in ("ticker", "trading_date", "analyst", "sources"):
        if field not in data_usage:
            raise ValueError(f"action_evidence_contract_data_usage_missing_field:{field}")
    if _text(data_usage.get("analyst")) != contract_analyst:
        raise ValueError("action_evidence_contract_data_usage_analyst_mismatch")
    for field in ("ticker", "trading_date", "analyst"):
        if not isinstance(data_usage.get(field), str) or not data_usage[field].strip():
            raise ValueError(f"action_evidence_contract_invalid_data_usage_text:{field}")
    if "data_available" in data_usage and not isinstance(
        data_usage.get("data_available"), bool
    ):
        raise ValueError("action_evidence_contract_invalid_data_available")
    sources = data_usage.get("sources")
    if not isinstance(sources, Mapping) or not sources:
        raise ValueError("action_evidence_contract_data_usage_sources_missing")
    for source_name, source in sources.items():
        if not _text(source_name) or not isinstance(source, Mapping):
            raise ValueError("action_evidence_contract_invalid_data_source")
        allowed_source_fields = ACTION_EVIDENCE_DATA_SOURCE_FIELDS.get(str(source_name))
        if allowed_source_fields is None:
            raise ValueError(
                f"action_evidence_contract_unregistered_data_source:{source_name}"
            )
        source_extras = sorted(set(source) - allowed_source_fields)
        if source_extras:
            raise ValueError(
                f"action_evidence_contract_unregistered_data_source_field:{source_name}:"
                + ",".join(source_extras)
            )
        for field in (
            "source",
            "dataset",
            "available",
            "used_in_signal",
            "pre_open_only",
            "info_cutoff",
        ):
            if field not in source:
                raise ValueError(
                    f"action_evidence_contract_data_source_missing_field:{source_name}:{field}"
                )
        for field in ("available", "used_in_signal", "pre_open_only"):
            if not isinstance(source.get(field), bool):
                raise ValueError(
                    f"action_evidence_contract_invalid_data_source_boolean:{source_name}:{field}"
                )
        expected_source, expected_dataset = ACTION_EVIDENCE_DATA_SOURCE_IDENTITIES[str(source_name)]
        if source.get("source") != expected_source or source.get("dataset") != expected_dataset:
            raise ValueError(
                f"action_evidence_contract_data_source_identity_mismatch:{source_name}"
            )
        if not isinstance(source.get("info_cutoff"), str) or not source["info_cutoff"].strip():
            raise ValueError(
                f"action_evidence_contract_invalid_data_source_text:{source_name}:info_cutoff"
            )
        for field in ACTION_EVIDENCE_DATA_SOURCE_INTEGER_FIELDS.intersection(source):
            value = source.get(field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(
                    f"action_evidence_contract_invalid_data_source_integer:{source_name}:{field}"
                )
        for field in ACTION_EVIDENCE_DATA_SOURCE_NUMBER_FIELDS.intersection(source):
            value = source.get(field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                raise ValueError(
                    f"action_evidence_contract_invalid_data_source_number:{source_name}:{field}"
                )
        for field in ACTION_EVIDENCE_DATA_SOURCE_LIST_FIELDS.intersection(source):
            if not isinstance(source.get(field), list):
                raise ValueError(
                    f"action_evidence_contract_invalid_data_source_list:{source_name}:{field}"
                )
        for field in ACTION_EVIDENCE_DATA_SOURCE_MAPPING_FIELDS.intersection(source):
            if not isinstance(source.get(field), Mapping):
                raise ValueError(
                    f"action_evidence_contract_invalid_data_source_mapping:{source_name}:{field}"
                )
        if source_name == "finoview_fundamental" and "local_availability_audit" in source:
            availability = source.get("local_availability_audit")
            if not isinstance(availability, Mapping):
                raise ValueError("action_evidence_contract_invalid_finoview_availability_audit")
            availability_extras = sorted(
                set(availability) - FINOVIEW_AVAILABILITY_ALLOWED_FIELDS
            )
            if availability_extras:
                raise ValueError(
                    "action_evidence_contract_unregistered_finoview_availability_field:"
                    + ",".join(availability_extras)
                )
    return contract


def _evidence_quality_score(value: Any) -> float:
    text = _text(value, "unknown").lower()
    if text in {"high", "strong", "good"}:
        return 1.0
    if text in {"medium", "acceptable", "ok"}:
        return 0.6
    if text in {"low", "weak", "poor"}:
        return 0.25
    return 0.4


def _quality_label(score: float) -> str:
    if score >= 0.72:
        return "high"
    if score >= 0.45:
        return "medium"
    if score > 0:
        return "low"
    return "unknown"


def _nested_forbidden_paths(
    value: Any,
    *,
    path: str = "",
    forbidden_keys: set[str] | None = None,
) -> list[str]:
    forbidden_keys = forbidden_keys or SCC_FORBIDDEN_TRADE_FIELDS
    hits: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if key_text in forbidden_keys:
                hits.append(child_path)
            hits.extend(
                _nested_forbidden_paths(
                    child,
                    path=child_path,
                    forbidden_keys=forbidden_keys,
                )
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            hits.extend(
                _nested_forbidden_paths(
                    child,
                    path=child_path,
                    forbidden_keys=forbidden_keys,
                )
            )
    return hits


def _source_analyst_names(contract: Mapping[str, Any]) -> list[str]:
    return [
        _text(row.get("analyst"))
        for row in contract.get("source_contracts") or []
        if isinstance(row, Mapping)
    ]


def _evidence_item_from_source(source: Mapping[str, Any]) -> dict:
    analyst = _text(source.get("analyst"))
    contract = dict(source.get("action_evidence_contract") or {})
    side = _side_from_contract(contract)
    trigger_valid = _bool(contract.get("trigger_valid"))
    trigger_confirmed = _bool(contract.get("current_trigger_confirmed"))
    opportunity_state = _text(contract.get("opportunity_state"), "unknown")
    trigger_status = (
        "not_applicable"
        if opportunity_state in {"no_opportunity", "risk_reduction_candidate"}
        else "confirmed"
        if trigger_valid and trigger_confirmed
        else "valid_unconfirmed"
        if trigger_valid
        else "watch_for_trigger"
    )
    product_profile_evidence = contract.get("product_profile_evidence")
    product_profile_evidence = (
        dict(product_profile_evidence)
        if isinstance(product_profile_evidence, Mapping)
        else {}
    )
    fusion_evidence = contract.get("fusion_evidence")
    fusion_evidence = dict(fusion_evidence) if isinstance(fusion_evidence, Mapping) else {}
    return {
        "analyst": analyst,
        "side": side,
        "confidence": _confidence(contract),
        "signal": _text(contract.get("signal")),
        "opportunity_state": opportunity_state,
        "trigger_valid": trigger_valid,
        "current_trigger_confirmed": trigger_confirmed,
        "trigger_status": trigger_status,
        "entry_trigger": _text(contract.get("entry_trigger")),
        "setup_type": _text(contract.get("setup_type"), "unknown"),
        "setup_quality_ok": _bool(contract.get("setup_quality_ok")),
        "horizon_class": _text(contract.get("horizon_class"), "unknown"),
        "market_regime": _text(contract.get("market_regime"), "unknown"),
        "evidence_quality": _text(contract.get("evidence_quality"), "unknown"),
        "current_evidence_conflict": _list(contract.get("current_evidence_conflict")),
        "missing_evidence": _list(contract.get("missing_evidence")),
        "evidence_strength": _text(
            fusion_evidence.get("evidence_strength") or contract.get("evidence_strength")
        ),
        "evidence_freshness": _text(fusion_evidence.get("evidence_freshness")),
        "confirmation_requirements": _list(
            fusion_evidence.get("confirmation_requirements")
            or contract.get("confirmation_requirements")
        ),
        "product_profile_id": _text(product_profile_evidence.get("product_profile_id")),
        "product_profile_used": _bool(product_profile_evidence.get("product_profile_used")),
        "product_profile_analysis_boundary": _text(
            product_profile_evidence.get("profile_analysis_boundary")
        ),
    }


def _fusion_summary_inputs(
    evidence_items: Iterable[Mapping[str, Any]],
    source_contracts: Iterable[Mapping[str, Any]],
) -> list[dict]:
    fusion_by_analyst: dict[str, dict] = {}
    for source in source_contracts:
        if not isinstance(source, Mapping):
            continue
        analyst = _text(source.get("analyst"))
        action_contract = source.get("action_evidence_contract")
        action_contract = action_contract if isinstance(action_contract, Mapping) else {}
        fusion = action_contract.get("fusion_evidence")
        fusion_by_analyst[analyst] = dict(fusion) if isinstance(fusion, Mapping) else {}

    inputs: list[dict] = []
    for item in evidence_items:
        if not isinstance(item, Mapping):
            continue
        merged = dict(item)
        merged["fusion_evidence"] = dict(fusion_by_analyst.get(_text(item.get("analyst"))) or {})
        inputs.append(merged)
    return inputs


def _data_quality_flags_from_usage(analyst: str, data_usage: Any) -> list[str]:
    """Summarize registered nested source facts without copying source payloads."""
    if not isinstance(data_usage, Mapping):
        return []
    flags: list[str] = []
    sources = data_usage.get("sources")
    if not isinstance(sources, Mapping):
        return flags
    for source_name, source_payload in sources.items():
        if not isinstance(source_payload, Mapping):
            continue
        prefix = f"{analyst}:{source_name}"
        if source_payload.get("available") is False:
            flags.append(f"data_source_unavailable:{prefix}")
        if source_payload.get("used_in_signal") is False:
            flags.append(f"data_source_not_used:{prefix}")
        stale_count = source_payload.get("stale_indicator_count")
        try:
            stale = int(stale_count or 0) > 0
        except (TypeError, ValueError):
            stale = False
        try:
            stale = stale or float(source_payload.get("stale_ratio") or 0.0) > 0.0
        except (TypeError, ValueError):
            pass
        if stale:
            flags.append(f"data_source_stale:{prefix}")
        for item in _list(source_payload.get("data_missing")):
            if _text(item):
                flags.append(f"data_source_missing:{prefix}:{_text(item)}")
        for item in _list(source_payload.get("missing_data")):
            if _text(item):
                flags.append(f"data_source_missing:{prefix}:{_text(item)}")
        for item in _list(source_payload.get("data_quality_flags")):
            if _text(item):
                flags.append(_text(item))
        for item in _list(source_payload.get("errors")):
            if _text(item):
                flags.append(f"data_source_error:{prefix}:{_text(item)}")
    return flags


def _direction_summary(evidence_items: list[Mapping[str, Any]]) -> dict:
    side_counts: Counter[str] = Counter()
    side_confidence: Counter[str] = Counter()
    trigger_states_by_side: dict[str, Counter[str]] = {
        "long": Counter(),
        "short": Counter(),
        "flat": Counter(),
    }
    for item in evidence_items:
        side = _text(item.get("side"), "flat").lower()
        side_counts[side] += 1
        side_confidence[side] += float(item.get("confidence") or 0.0)
        trigger_states_by_side[side][_text(item.get("trigger_status"), "watch_for_trigger")] += 1
    long_key = (side_counts.get("long", 0), side_confidence.get("long", 0.0))
    short_key = (side_counts.get("short", 0), side_confidence.get("short", 0.0))
    if long_key == short_key and long_key[0] > 0:
        dominant_side = "mixed"
    elif long_key > short_key:
        dominant_side = "long"
    elif short_key > long_key:
        dominant_side = "short"
    else:
        dominant_side = "flat"
    supporting = [
        _text(item.get("analyst"))
        for item in evidence_items
        if item.get("side") == dominant_side and dominant_side != "flat"
    ]
    opposing_side = "short" if dominant_side == "long" else "long" if dominant_side == "short" else ""
    opposing = [
        _text(item.get("analyst"))
        for item in evidence_items
        if item.get("side") == opposing_side
    ]
    neutral = [
        _text(item.get("analyst"))
        for item in evidence_items
        if item.get("side") == "flat"
    ]
    consensus = (
        "conflicted"
        if dominant_side == "mixed" or opposing
        else "no_direction"
        if dominant_side == "flat"
        else "multi_analyst_support"
        if len(set(supporting)) >= 2
        else "single_analyst_support"
    )
    strength_scores = [
        float(item.get("confidence") or 0.0)
        * _evidence_quality_score(item.get("evidence_quality"))
        for item in evidence_items
        if item.get("side") == dominant_side and dominant_side != "flat"
    ]
    strength = _quality_label(sum(strength_scores) / len(strength_scores)) if strength_scores else "unknown"
    dominant_trigger_states = trigger_states_by_side.get(dominant_side, Counter())
    trigger_status = (
        "not_applicable"
        if dominant_side in {"flat", "mixed"}
        else "confirmed"
        if dominant_trigger_states.get("confirmed")
        else "valid_unconfirmed"
        if dominant_trigger_states.get("valid_unconfirmed")
        else "watch_for_trigger"
        if dominant_trigger_states.get("watch_for_trigger")
        else "not_applicable"
    )
    return {
        "dominant_side": dominant_side,
        "side_consensus": consensus,
        "trigger_status": trigger_status,
        "supporting_analysts": sorted(set(supporting)),
        "opposing_analysts": sorted(set(opposing)),
        "neutral_analysts": sorted(set(neutral)),
        "evidence_strength": strength,
        "evidence_conflict_level": "high" if len(opposing) >= 2 else "medium" if opposing else "low",
    }


def validate_signal_collection_contract(
    contract: Any,
    *,
    ticker: str | None = None,
    trading_date: Any = None,
    enabled_analysts: Iterable[str] | None = None,
    analyst_signals: Iterable[Any] | None = None,
    require_signal_record_ids: bool = False,
) -> dict:
    """Validate the one SCC contract at producer and PM-consumer boundaries."""
    if not isinstance(contract, dict) or not contract:
        raise ValueError("signal_collection_contract_missing")
    forbidden = _nested_forbidden_paths(contract)
    if forbidden:
        raise ValueError(f"signal_collection_forbidden_trade_field:{','.join(forbidden)}")
    extras = sorted(set(contract) - SCC_ALLOWED_TOP_LEVEL_FIELDS)
    if extras:
        raise ValueError(f"signal_collection_unregistered_top_level_field:{','.join(extras)}")
    if contract.get("contract_version") != SCC_CONTRACT_VERSION:
        raise ValueError("signal_collection_invalid_contract_version")
    if _text(contract.get("source_agent")) != "signal_collector":
        raise ValueError("signal_collection_invalid_source_agent")
    if _text(contract.get("collector_decision_boundary")) != "no_trade_authority":
        raise ValueError("signal_collection_invalid_decision_boundary")
    if ticker is not None and _text(contract.get("ticker")).upper() != _text(ticker).upper():
        raise ValueError("signal_collection_ticker_mismatch")
    if trading_date is not None and _text(contract.get("trading_date"))[:10] != _text(trading_date)[:10]:
        raise ValueError("signal_collection_trading_date_mismatch")
    source_contracts = contract.get("source_contracts")
    evidence_items = contract.get("evidence_items")
    if not isinstance(source_contracts, list) or not source_contracts:
        raise ValueError("signal_collection_missing_source_contracts")
    if not isinstance(evidence_items, list) or not evidence_items:
        raise ValueError("signal_collection_missing_evidence_items")
    for source in source_contracts:
        if not isinstance(source, dict):
            raise ValueError("signal_collection_invalid_source_contract")
        source_extras = sorted(set(source) - SCC_SOURCE_CONTRACT_FIELDS)
        if source_extras:
            raise ValueError(
                f"signal_collection_unregistered_source_contract_field:{','.join(source_extras)}"
            )
        if require_signal_record_ids and not _text(source.get("signal_record_id")):
            raise ValueError(
                f"signal_collection_missing_signal_record_id:{_text(source.get('analyst'))}"
            )
    for item in evidence_items:
        if not isinstance(item, dict):
            raise ValueError("signal_collection_invalid_evidence_item")
        item_extras = sorted(set(item) - SCC_EVIDENCE_ITEM_FIELDS)
        if item_extras:
            raise ValueError(
                f"signal_collection_unregistered_evidence_item_field:{','.join(item_extras)}"
            )
    source_names = _source_analyst_names(contract)
    if any(not name for name in source_names):
        raise ValueError("signal_collection_missing_source_analyst")
    duplicates = sorted(name for name, count in Counter(source_names).items() if count > 1)
    if duplicates:
        raise ValueError(f"signal_collection_duplicate_analyst:{','.join(duplicates)}")
    item_names = [
        _text(row.get("analyst"))
        for row in evidence_items
        if isinstance(row, Mapping)
    ]
    if item_names != source_names:
        raise ValueError("signal_collection_evidence_source_order_mismatch")
    expected = [_text(name) for name in (enabled_analysts or []) if _text(name)]
    unexpected = sorted(set(source_names) - set(expected)) if expected else []
    if unexpected:
        raise ValueError(f"signal_collection_unexpected_analyst:{','.join(unexpected)}")
    missing = sorted(set(expected) - set(source_names)) if expected else []
    missing_evidence = {_text(value) for value in contract.get("missing_evidence") or []}
    for name in missing:
        if f"missing_analyst:{name}" not in missing_evidence:
            raise ValueError(f"signal_collection_missing_analyst_not_recorded:{name}")
    fusion = contract.get("evidence_fusion")
    if not isinstance(fusion, dict):
        raise ValueError("signal_collection_missing_evidence_fusion")
    fusion_extras = sorted(set(fusion) - SCC_EVIDENCE_FUSION_FIELDS)
    if fusion_extras:
        raise ValueError(
            f"signal_collection_unregistered_evidence_fusion_field:{','.join(fusion_extras)}"
        )
    expected_items: list[dict] = []
    for source in source_contracts:
        source_name = _text(source.get("analyst")) if isinstance(source, Mapping) else ""
        action_contract = source.get("action_evidence_contract") if isinstance(source, Mapping) else None
        try:
            validate_action_evidence_contract(action_contract, analyst=source_name)
        except ValueError as exc:
            raise ValueError(f"signal_collection_invalid_action_evidence_contract:{source_name}:{exc}") from exc
        expected_items.append(_evidence_item_from_source(source))
    for expected_item, actual_item in zip(expected_items, evidence_items):
        analyst = expected_item["analyst"]
        for field, expected_value in expected_item.items():
            if actual_item.get(field) != expected_value:
                raise ValueError(
                    f"signal_collection_evidence_item_semantic_mismatch:{analyst}:{field}"
                )
    expected_direction = _direction_summary(expected_items)
    for field, expected_value in expected_direction.items():
        if contract.get(field) != expected_value:
            raise ValueError(f"signal_collection_{field}_mismatch")
    expected_fusion = build_signal_collection_fusion_summary(
        _fusion_summary_inputs(expected_items, source_contracts),
        dominant_side=expected_direction["dominant_side"],
    )
    if fusion != expected_fusion:
        raise ValueError("signal_collection_evidence_fusion_semantic_mismatch")
    if list(contract.get("confirmation_requirements") or []) != list(
        expected_fusion.get("confirmation_requirements") or []
    ):
        raise ValueError("signal_collection_confirmation_requirements_mismatch")
    expected_setup_types = sorted(
        {
            _text(item.get("setup_type"))
            for item in expected_items
            if _text(item.get("setup_type")) not in {"", "unknown"}
        }
    )
    if list(contract.get("setup_types") or []) != expected_setup_types:
        raise ValueError("signal_collection_setup_types_mismatch")
    expected_horizon_scope = sorted(
        {
            _text(item.get("horizon_class"))
            for item in expected_items
            if _text(item.get("horizon_class")) not in {"", "unknown"}
        }
    )
    if list(contract.get("horizon_scope") or []) != expected_horizon_scope:
        raise ValueError("signal_collection_horizon_scope_mismatch")
    if analyst_signals is not None:
        raw_by_agent: dict[str, Any] = {}
        for signal in analyst_signals or []:
            name = _agent_name(signal)
            if name in raw_by_agent:
                raise ValueError(f"signal_collection_duplicate_analyst:{name}")
            raw_by_agent[name] = signal
        if set(raw_by_agent) != set(source_names):
            raise ValueError("signal_collection_source_lineage_mismatch")
        for source in source_contracts:
            name = _text(source.get("analyst"))
            raw = raw_by_agent[name]
            if _action_contract(raw) != source.get("action_evidence_contract"):
                raise ValueError(f"signal_collection_action_contract_lineage_mismatch:{name}")
            raw_id = _metadata(raw).get("signal_record_id")
            source_id = source.get("signal_record_id")
            if raw_id not in (None, "") and source_id != raw_id:
                raise ValueError(f"signal_collection_record_id_lineage_mismatch:{name}")
    return contract


def build_scc_data_quality_summary(contract: Mapping[str, Any]) -> dict:
    """Project one validated SCC into the canonical Auditor quality input."""
    validated = validate_signal_collection_contract(dict(contract))
    flags = sorted({_text(item) for item in validated.get("data_quality_flags") or [] if _text(item)})
    missing_evidence = sorted(
        {_text(item) for item in validated.get("missing_evidence") or [] if _text(item)}
    )
    hard_flags = {
        "pre_open_reference_price_unavailable",
        "required_market_data_unavailable",
        "future_leak",
    }
    hard_failure = any(
        flag in hard_flags
        or flag.startswith("no_lookahead_status:violation")
        or flag.startswith("no_lookahead_status:future_leak")
        for flag in flags
    )
    return {
        "status": "hard_fail" if hard_failure else "warning" if flags else "clean",
        "flags": flags,
        "missing_evidence": missing_evidence,
        "source": "signal_collection_contract",
    }


def build_pm_evidence_signals_from_scc(contract: Mapping[str, Any]) -> list[Any]:
    """Build PM's internal evidence view solely from the already validated SCC."""
    validate_signal_collection_contract(dict(contract))
    evidence_signals: list[Any] = []
    for source in contract.get("source_contracts") or []:
        analyst = _text(source.get("analyst"))
        action_contract = dict(source.get("action_evidence_contract") or {})
        side = _side_from_contract(action_contract)
        payload = dict(action_contract)
        payload["agent_name"] = analyst
        payload["signal"] = (
            Signal.BULLISH
            if side == "long"
            else Signal.BEARISH
            if side == "short"
            else Signal.NEUTRAL
        )
        payload["metadata"] = {
            "action_evidence_contract": action_contract,
            "product_profile_evidence": dict(action_contract.get("product_profile_evidence") or {}),
            "fusion_evidence": dict(action_contract.get("fusion_evidence") or {}),
            "signal_record_id": source.get("signal_record_id"),
            "data_usage_summary": dict(action_contract.get("data_usage_summary") or {}),
        }
        evidence_signals.append(_SCCEvidenceSignal(**payload))
    return evidence_signals


def scc_news_quality_scores_from_metadata(metadata: Mapping[str, Any] | None) -> tuple[float, float]:
    """Read registered news quality scores from a PM view rebuilt from SCC."""
    values = metadata if isinstance(metadata, Mapping) else {}
    action_contract = values.get("action_evidence_contract")
    action_contract = action_contract if isinstance(action_contract, Mapping) else {}
    if _text(action_contract.get("analyst")) != "commodity_news":
        return 0.0, 0.0
    fusion = action_contract.get("fusion_evidence")
    fusion = fusion if isinstance(fusion, Mapping) else {}
    data_usage = action_contract.get("data_usage_summary")
    data_usage = data_usage if isinstance(data_usage, Mapping) else {}
    sources = data_usage.get("sources")
    sources = sources if isinstance(sources, Mapping) else {}
    news_source = sources.get("finoview_news_txt")
    news_source = news_source if isinstance(news_source, Mapping) else {}
    return (
        _confidence({"confidence": fusion.get("evidence_freshness_score")}),
        _confidence({"confidence": news_source.get("relevance_score")}),
    )


def build_signal_collection_contract(
    *,
    ticker: str,
    trading_date: Any,
    analyst_signals: Iterable[Any],
    enabled_analysts: Iterable[str] | None = None,
) -> dict:
    """Build the PM-facing signal collection contract.

    The result is evidence only.  It never contains lots, rank, action, or
    position authority.
    """
    analyst_signals = list(analyst_signals or [])
    enabled = [_text(name) for name in (enabled_analysts or ANALYST_ORDER) if _text(name)]
    duplicate_enabled = sorted(name for name, count in Counter(enabled).items() if count > 1)
    if duplicate_enabled:
        raise ValueError(f"signal_collection_duplicate_enabled_analyst:{','.join(duplicate_enabled)}")
    evidence_items: list[dict] = []
    source_contracts: list[dict] = []
    missing_evidence: list[str] = []
    data_quality_flags: list[str] = []
    invalidation_summary: list[dict] = []
    setup_types: list[str] = []
    horizons: list[str] = []
    seen_agents: set[str] = set()
    for signal in analyst_signals or []:
        agent = _agent_name(signal)
        if not agent:
            raise ValueError("signal_collection_missing_analyst_name")
        if agent not in enabled:
            raise ValueError(f"signal_collection_unexpected_analyst:{agent}")
        if agent in seen_agents:
            raise ValueError(f"signal_collection_duplicate_analyst:{agent}")
        seen_agents.add(agent)
        metadata_fields = set(_metadata(signal))
        forbidden_metadata = sorted(
            metadata_fields - {"action_evidence_contract", "signal_record_id"}
        )
        if forbidden_metadata:
            raise ValueError(
                f"signal_collection_forbidden_source_metadata:{agent}:"
                f"{','.join(forbidden_metadata)}"
            )
        contract = _action_contract(signal)
        if not contract:
            raise ValueError(f"signal_collection_missing_action_evidence_contract:{agent}")
        if contract.get("contract_version") != ACTION_EVIDENCE_CONTRACT_VERSION:
            raise ValueError(f"signal_collection_invalid_action_evidence_contract_version:{agent}")
        if _text(contract.get("analyst")) != agent:
            raise ValueError(f"signal_collection_action_contract_analyst_mismatch:{agent}")
        side = _side_from_contract(contract)
        confidence = _confidence(contract)
        trigger_valid = _bool(contract.get("trigger_valid"))
        trigger_confirmed = _bool(contract.get("current_trigger_confirmed"))
        opportunity_state = _text(contract.get("opportunity_state"), "unknown")
        trigger_status = (
            "not_applicable"
            if opportunity_state in {"no_opportunity", "risk_reduction_candidate"}
            else "confirmed"
            if trigger_valid and trigger_confirmed
            else "valid_unconfirmed"
            if trigger_valid
            else "watch_for_trigger"
        )

        setup_type = _text(contract.get("setup_type"), "unknown")
        if setup_type and setup_type != "unknown":
            setup_types.append(setup_type)
        horizon = _text(contract.get("horizon_class"), "unknown")
        if horizon and horizon != "unknown":
            horizons.append(horizon)

        missing_evidence.extend(str(item) for item in _list(contract.get("missing_evidence")) if str(item))
        data_usage = contract.get("data_usage_summary") or {}
        data_quality_flags.extend(_data_quality_flags_from_usage(agent, data_usage))
        no_lookahead_status = _text(contract.get("no_lookahead_status"), "unchecked")
        if no_lookahead_status not in {"ok", "pass", "clean"}:
            data_quality_flags.append(f"no_lookahead_status:{no_lookahead_status}")

        if _bool(contract.get("invalidation_present")):
            invalidation_summary.append({
                "analyst": agent,
                "condition": _text(contract.get("invalidation_condition")),
                "level": contract.get("invalidation_level"),
            })
        product_profile_evidence = contract.get("product_profile_evidence")
        product_profile_evidence = (
            dict(product_profile_evidence)
            if isinstance(product_profile_evidence, Mapping)
            else {}
        )
        fusion_evidence = contract.get("fusion_evidence")
        fusion_evidence = dict(fusion_evidence) if isinstance(fusion_evidence, Mapping) else {}

        item = {
            "analyst": agent,
            "side": side,
            "confidence": confidence,
            "signal": _text(contract.get("signal")),
            "opportunity_state": opportunity_state,
            "trigger_valid": trigger_valid,
            "current_trigger_confirmed": trigger_confirmed,
            "trigger_status": trigger_status,
            "entry_trigger": _text(contract.get("entry_trigger")),
            "setup_type": setup_type,
            "setup_quality_ok": _bool(contract.get("setup_quality_ok")),
            "horizon_class": horizon,
            "market_regime": _text(contract.get("market_regime"), "unknown"),
            "evidence_quality": _text(contract.get("evidence_quality"), "unknown"),
            "current_evidence_conflict": _list(contract.get("current_evidence_conflict")),
            "missing_evidence": _list(contract.get("missing_evidence")),
            "evidence_strength": _text(fusion_evidence.get("evidence_strength") or contract.get("evidence_strength")),
            "evidence_freshness": _text(fusion_evidence.get("evidence_freshness")),
            "confirmation_requirements": _list(fusion_evidence.get("confirmation_requirements") or contract.get("confirmation_requirements")),
            "product_profile_id": _text(product_profile_evidence.get("product_profile_id")),
            "product_profile_used": _bool(product_profile_evidence.get("product_profile_used")),
            "product_profile_analysis_boundary": _text(product_profile_evidence.get("profile_analysis_boundary")),
        }
        evidence_items.append(item)
        source_contracts.append({
            "analyst": agent,
            "action_evidence_contract": contract,
            "signal_record_id": _metadata(signal).get("signal_record_id"),
        })

    missing_agents = [name for name in enabled if name not in seen_agents]
    missing_evidence.extend(f"missing_analyst:{name}" for name in missing_agents)

    direction_summary = _direction_summary(evidence_items)
    dominant_side = direction_summary["dominant_side"]
    fusion_summary = build_signal_collection_fusion_summary(
        _fusion_summary_inputs(evidence_items, source_contracts),
        dominant_side=dominant_side,
    )
    merged_missing_evidence = sorted(set(missing_evidence) | set(fusion_summary.get("missing_evidence") or []))

    result = {
        "contract_version": SCC_CONTRACT_VERSION,
        "source_agent": "signal_collector",
        "ticker": ticker,
        "trading_date": str(trading_date),
        "source_contracts": source_contracts,
        "evidence_items": evidence_items,
        "dominant_side": dominant_side,
        "side_consensus": direction_summary["side_consensus"],
        "trigger_status": direction_summary["trigger_status"],
        "supporting_analysts": direction_summary["supporting_analysts"],
        "opposing_analysts": direction_summary["opposing_analysts"],
        "neutral_analysts": direction_summary["neutral_analysts"],
        "evidence_strength": direction_summary["evidence_strength"],
        "evidence_conflict_level": direction_summary["evidence_conflict_level"],
        "confirmation_requirements": fusion_summary.get("confirmation_requirements") or [],
        "missing_evidence": merged_missing_evidence,
        "data_quality_flags": sorted(set(data_quality_flags)),
        "setup_types": sorted(set(setup_types)),
        "horizon_scope": sorted(set(horizons)),
        "invalidation_summary": invalidation_summary,
        "evidence_fusion": fusion_summary,
        "collector_decision_boundary": "no_trade_authority",
    }
    return validate_signal_collection_contract(
        result,
        ticker=ticker,
        trading_date=trading_date,
        enabled_analysts=enabled,
        analyst_signals=analyst_signals,
    )
