import json
import re
from datetime import datetime
from enum import Enum
from graph.constants import AgentKey
from llm.prompt import RISK_CONTROL_PROMPT, SINGLE_ANALYST_LOGIC, MULTI_ANALYST_LOGIC
from graph.schema import (
    FundState,
    PositionRisk,
    FuturesDecision,
    FuturesAction,
    FuturesRecommendation,
    RecommendationAction,
    RecommendationSourceType,
    RecommendationStatus,
    TradingPhase,
)
from llm.inference import agent_call, llm_audit_metadata
from apis.contract_info_cache import FuturesContractInfoCache
from agents.decision_team.auditor import TradeAuditor, TradeAuditorInput
from util.db_helper import get_db
from util.logger import logger
from util.text_sanitize import sanitize_visible_text
from tools.agent_tools.analysis.dynamic_weights import DynamicWeightCalculator, calibrate_weights_by_signal_history
from tools.agent_tools.analysis.learning_context import apply_config_learning_overlay, build_learning_context
from tools.agent_tools.analysis.market_confirmation import MarketConfirmationEngine
from tools.agent_tools.analysis.business_quality import summarize_business_quality
from tools.agent_tools.analysis.data_usage import build_pm_data_quality_summary, write_daily_data_quality_summary
from tools.agent_tools.execution.order_semantics import (
    build_lot_intent_consistency,
    recommendation_intent_from_lots,
)
from tools.agent_tools.contracts import (
    attach_snapshot_contract,
    build_internal_message_contract,
    validate_internal_message_contract,
)
from tools.agent_tools.decision.capital_allocator import (
    conflicting_weak_memory_record as _capital_conflicting_weak_memory_record,
    strategy_memory_record as _capital_strategy_memory_record,
)
from tools.agent_tools.decision.contextual_rule_calibration import apply_pm_contextual_calibration
from tools.agent_tools.decision.position_lifecycle import (
    apply_trade_plan_multiplier as _position_apply_trade_plan_multiplier,
    is_new_or_increasing_exposure as _position_is_new_or_increasing_exposure,
    same_sign as _position_same_sign,
    scale_signed_ratio as _position_scale_signed_ratio,
    target_side_from_ratio as _position_target_side_from_ratio,
)
from tools.agent_tools.decision.pm_capital_policy import (
    _apply_capital_utilization_control,
)
from tools.agent_tools.decision.pm_invalidation_policy import (
    _apply_pretrade_invalidation_control,
    _has_explicit_stop_protection,
    _has_structured_invalidation_condition,
)
from tools.agent_tools.decision.risk_controls import business_quality_position_gate
from tools.agent_tools.analysis.signal_fusion import (
    analyst_signal_combo as _fusion_analyst_signal_combo,
    build_horizon_scope,
    resolve_decision_horizon as _fusion_resolve_decision_horizon,
)

class RiskLevel(Enum):
    """Risk level classification for futures portfolio control."""
    SAFE = "SAFE"
    WARNING = "WARNING"      # Cashflow ratio in the 50%-70% band.
    DANGER = "DANGER"        # Cashflow ratio in the 30%-50% band.
    EMERGENCY = "EMERGENCY"  # Cashflow ratio below 30%.

def _sanitize_visible_text(text: str) -> str:
    """Normalize visible mojibake fragments before they reach logs or persisted justifications."""
    return sanitize_visible_text(text)

def _portfolio_account_equity(portfolio) -> float:
    """Return futures account equity using cash balance plus reserved margin."""
    cash_balance = float(getattr(portfolio, "cashflow", 0.0) or 0.0)
    reserved_margin = float(
        getattr(portfolio, "margin_used", None)
        if getattr(portfolio, "margin_used", None) is not None
        else sum(float(getattr(pos, "margin_used", 0.0) or 0.0) for pos in portfolio.positions.values())
    )
    return cash_balance + reserved_margin


def check_risk_level(portfolio, config) -> tuple[RiskLevel, float]:
    """Return the current risk level and account-equity ratio."""

    # Use configured cashflow as the capital base for risk classification.
    capital_base = config.get('cashflow', 0)
    risk_control = config.get('risk_control', {})
    warning_ratio = risk_control.get('warning_ratio', 0.7)
    danger_ratio = risk_control.get('danger_ratio', 0.5)
    emergency_ratio = risk_control.get('emergency_ratio', 0.3)

    # Futures risk state uses account equity, not cash-only balance or notional total_assets.
    account_equity = _portfolio_account_equity(portfolio)
    cashflow_ratio = account_equity / capital_base if capital_base > 0 else 1.0

    if cashflow_ratio >= warning_ratio:
        return RiskLevel.SAFE, cashflow_ratio
    elif cashflow_ratio >= danger_ratio:
        return RiskLevel.WARNING, cashflow_ratio
    elif cashflow_ratio >= emergency_ratio:
        return RiskLevel.DANGER, cashflow_ratio
    else:
        return RiskLevel.EMERGENCY, cashflow_ratio

def get_position_scaling_factor(risk_level: RiskLevel, config) -> float:
    """
    Return the position-scaling multiplier for the current risk level.

    Args:
        risk_level: Current portfolio risk classification.
        config: Runtime configuration.

    Returns:
        A multiplier applied to the target position ratio.
    """
    risk_control = config.get('risk_control', {})
    position_scaling = risk_control.get('position_scaling', {
        'safe': 1.0,
        'warning': 0.6,
        'danger': 0.3
    })

    return position_scaling.get(risk_level.value.lower(), 1.0)

def get_max_single_position_ratio(risk_level: RiskLevel, config) -> float:
    """
    Return the base per-opportunity sizing anchor for the current risk level.

    Args:
        risk_level: Current portfolio risk classification.
        config: Runtime configuration.

    Returns:
        A starting notional ratio anchor for weak or unverified opportunities.
        Validated opportunities can receive a larger dynamic budget, while the
        portfolio margin ceiling remains the hard capital constraint.
    """
    risk_control = config.get('risk_control', {})
    max_single_position = risk_control.get('max_single_position_ratio', {
        'safe': 0.15,
        'warning': 0.10,
        'danger': 0.05
    })

    return max_single_position.get(risk_level.value.lower(), 0.15)


def get_hard_allocation_margin_ratio(config: dict) -> float:
    """Return the active portfolio margin ceiling for new or increasing exposure.

    ``max_total_margin_ratio`` is the portfolio-level hard gate. Learning
    overlays may make the active target more conservative, but cannot lift the
    hard tradable-capital ceiling above this value.
    """
    def _ratio(value, default):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    hard_cap = _ratio((config or {}).get("max_total_margin_ratio"), 0.20)
    capital_control = (config or {}).get("capital_utilization_control", {}) or {}
    learned_cap = _ratio(capital_control.get("max_margin_ratio_after_scaling"), hard_cap)
    return min(hard_cap, learned_cap)


# Portfolio Manager Thresholds
thresholds = {
    "decision_memory_limit": 10
}

ANALYST_ORDER = ("technical", "fundamental", "commodity_news")

SECTOR_BY_TICKER = {
    "BU": "energy",
    "EB": "chemical",
    "MA": "chemical",
    "TA": "chemical",
    "HC": "ferrous",
    "I": "ferrous",
    "J": "ferrous",
    "RB": "ferrous",
    "PB": "nonferrous",
    "ZN": "nonferrous",
    "C": "agricultural",
    "CF": "agricultural",
    "M": "agricultural",
    "P": "agricultural",
    "SR": "agricultural",
}

DEFAULT_SECTOR_WEIGHTS = {
    "energy": {"technical": 0.40, "fundamental": 0.40, "commodity_news": 0.20},
    "chemical": {"technical": 0.30, "fundamental": 0.50, "commodity_news": 0.20},
    "ferrous": {"technical": 0.30, "fundamental": 0.50, "commodity_news": 0.20},
    "nonferrous": {"technical": 0.35, "fundamental": 0.35, "commodity_news": 0.30},
    "agricultural": {"technical": 0.20, "fundamental": 0.45, "commodity_news": 0.35},
    "generic": {"technical": 0.40, "fundamental": 0.35, "commodity_news": 0.25},
}

DEFAULT_QUALITY_MULTIPLIERS = {
    "high": 1.00,
    "medium": 0.65,
    "low": 0.15,
    "unknown": 0.75,
}

DEFAULT_HOLDING_REBALANCE_CONTROL = {
    "enabled": True,
    "min_rebalance_ratio": 0.008,
    "min_new_entry_ratio": 0.004,
    "max_daily_reduction_ratio": 0.40,
    "reverse_min_signal_strength": 0.55,
    "reverse_min_confirmation_score": 0.65,
    "exit_min_signal_strength": 0.35,
    "exit_min_confirmation_score": 0.45,
    "min_fundamental_anchor_confidence": 0.40,
    "min_signal_support_confidence": 0.35,
    "fundamental_anchor_tradeability": ["high", "medium"],
    "news_override_tradeability": "high",
    "news_override_min_confidence": 0.60,
    "news_override_min_freshness": 0.70,
    "news_override_min_relevance": 0.70,
    "position_lifecycle": {
        "enabled": True,
        "trend_min_profit_ratio": 0.03,
        "trend_min_confirmation_score": 0.60,
        "trend_min_signal_strength": 0.30,
        "failed_loss_ratio": -0.05,
        "failed_max_confirmation_score": 0.45,
        "loss_revalidation_min_hold_days": 1,
        "loss_revalidation_ratio": -0.02,
        "loss_revalidation_min_confirmation_score": 0.55,
        "loss_revalidation_min_signal_strength": 0.25,
        "loss_revalidation_anchor_min_confirmation_score": 0.50,
        "loss_revalidation_anchor_min_signal_strength": 0.20,
        "loss_revalidation_reduction_multiplier": 0.50,
        "loss_revalidation_exit_ratio": -0.04,
        "loss_revalidation_exit_confirmation_score": 0.45,
        "new_loss_revalidation_enabled": True,
        "new_loss_revalidation_max_hold_days": 2,
        "new_loss_revalidation_ratio": -0.005,
        "new_loss_revalidation_exit_ratio": -0.02,
        "new_loss_revalidation_min_confirmation_score": 0.55,
        "new_loss_revalidation_min_signal_strength": 0.25,
        "new_loss_revalidation_reduction_multiplier": 0.50,
        "probe_max_hold_days": 2,
        "probe_min_profit_ratio": 0.01,
        "probe_min_confirmation_score": 0.55,
        "require_pretrade_invalidation_for_new_entry": True,
        "missing_invalidation_cap_multiplier": 0.35,
        "missing_invalidation_probe_max_ratio": 0.02,
    },
    "horizon_consistency": {
        "enabled": True,
        "apply_to_new_entries": True,
        "apply_to_losing_holds": True,
        "medium_requires_short_timing": True,
        "medium_requires_invalidation": True,
        "min_short_timing_confidence": 0.45,
        "min_confirmation_score": 0.55,
        "losing_hold_exit_ratio": -0.02,
        "losing_hold_reduction_multiplier": 0.50,
    },
    "min_hold_days_by_sector": {
        "energy": 3,
        "chemical": 5,
        "ferrous": 5,
        "nonferrous": 4,
        "agricultural": 7,
        "generic": 4,
    },
}

def extract_underlying_code(ticker: str) -> str:
    """Extract the underlying futures code from a ticker."""
    match = re.match(r'^([A-Z]+)', ticker.upper())
    return match.group(1) if match else ticker


def _normalize_agent_name(agent_name: str) -> str:
    return "commodity_news" if agent_name == "company_news" else str(agent_name or "")


def _normalize_weights(weights: dict) -> dict:
    cleaned = {key: max(0.0, float(weights.get(key, 0.0) or 0.0)) for key in ANALYST_ORDER}
    total = sum(cleaned.values())
    if total <= 0:
        return DEFAULT_SECTOR_WEIGHTS["generic"].copy()
    return {key: value / total for key, value in cleaned.items()}


def _sector_for_ticker(ticker: str) -> str:
    return SECTOR_BY_TICKER.get(extract_underlying_code(ticker).upper(), "generic")


def _get_portfolio_manager_config(full_config: dict) -> dict:
    return full_config.get("portfolio_manager", {}) or {}


def _get_holding_rebalance_config(full_config: dict) -> dict:
    configured = (_get_portfolio_manager_config(full_config).get("holding_rebalance_control") or {})
    merged = {
        key: value
        for key, value in DEFAULT_HOLDING_REBALANCE_CONTROL.items()
        if key != "min_hold_days_by_sector"
    }
    merged.update({key: value for key, value in configured.items() if key != "min_hold_days_by_sector"})
    if isinstance(DEFAULT_HOLDING_REBALANCE_CONTROL.get("position_lifecycle"), dict):
        lifecycle_defaults = DEFAULT_HOLDING_REBALANCE_CONTROL["position_lifecycle"].copy()
        lifecycle_defaults.update(configured.get("position_lifecycle") or {})
        merged["position_lifecycle"] = lifecycle_defaults
    if isinstance(DEFAULT_HOLDING_REBALANCE_CONTROL.get("horizon_consistency"), dict):
        horizon_defaults = DEFAULT_HOLDING_REBALANCE_CONTROL["horizon_consistency"].copy()
        horizon_defaults.update(configured.get("horizon_consistency") or {})
        merged["horizon_consistency"] = horizon_defaults
    sector_days = DEFAULT_HOLDING_REBALANCE_CONTROL["min_hold_days_by_sector"].copy()
    sector_days.update(configured.get("min_hold_days_by_sector") or {})
    merged["min_hold_days_by_sector"] = sector_days
    return merged


def _sector_base_weights(ticker: str, full_config: dict) -> dict:
    sector = _sector_for_ticker(ticker)
    configured = (
        (_get_portfolio_manager_config(full_config).get("sector_weights") or {}).get(sector)
        or DEFAULT_SECTOR_WEIGHTS.get(sector)
        or DEFAULT_SECTOR_WEIGHTS["generic"]
    )
    return _normalize_weights(configured)


def _signal_to_text(value) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _signal_metadata(signal) -> dict:
    metadata = getattr(signal, "metadata", {}) or {}
    return metadata if isinstance(metadata, dict) else {}


def _nested_context(metadata: dict, agent_name: str) -> dict:
    for key in (
        f"{agent_name}_context",
        "technical_context",
        "fundamental_context",
        "news_context",
    ):
        value = metadata.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _signal_tradeability(signal, agent_name: str) -> str:
    metadata = _signal_metadata(signal)
    context = _nested_context(metadata, agent_name)
    return str(metadata.get("tradeability") or context.get("tradeability") or "unknown").lower()


def _signal_risk_flags(signal, agent_name: str) -> list:
    metadata = _signal_metadata(signal)
    context = _nested_context(metadata, agent_name)
    flags = metadata.get("risk_flags") or context.get("risk_flags") or []
    return [str(flag) for flag in flags] if isinstance(flags, list) else []


def _quality_multiplier(signal, agent_name: str, full_config: dict) -> tuple[float, str, list]:
    pm_config = _get_portfolio_manager_config(full_config)
    quality_config = pm_config.get("quality_aware_fusion", {}) or {}
    multipliers = {**DEFAULT_QUALITY_MULTIPLIERS, **(quality_config.get("tradeability_multipliers") or {})}
    tradeability = _signal_tradeability(signal, agent_name)
    risk_flags = _signal_risk_flags(signal, agent_name)
    multiplier = float(multipliers.get(tradeability, multipliers.get("unknown", 0.75)))

    severe_flags = set(
        quality_config.get(
            "severe_risk_flags",
            [
                "low_coverage",
                "stale_fundamental_inputs",
                "missing_fundamental_inputs",
                "conflicting_indicators",
                "mixed_news_direction",
                "thin_news_sample",
            ],
        )
    )
    if severe_flags.intersection(risk_flags):
        multiplier *= float(quality_config.get("severe_risk_flag_multiplier", 0.80))

    business_cfg = full_config.get("analyst_business_quality", {}) or {}
    if business_cfg.get("enabled", True):
        business_score = _safe_float(getattr(signal, "business_quality_score", 0.0), 0.0)
        min_probe = _safe_float(business_cfg.get("min_score_for_probe"), 0.45)
        min_deploy = _safe_float(business_cfg.get("min_score_for_deployable"), 0.60)
        if business_score < min_probe:
            multiplier *= 0.10
        elif business_score < min_deploy:
            multiplier *= 0.55

    min_multiplier = float(quality_config.get("min_quality_multiplier", 0.05))
    return max(min_multiplier, multiplier), tradeability, risk_flags


def _effective_signal_confidence(signal, agent_name: str, quality_summary: dict, full_config: dict) -> float:
    raw_confidence = float(getattr(signal, "confidence", 0.0) or 0.0)
    pm_config = _get_portfolio_manager_config(full_config)
    quality_config = pm_config.get("quality_aware_fusion", {}) or {}
    caps = quality_config.get("confidence_caps") or {
        "high": 1.00,
        "medium": 0.65,
        "low": 0.35,
        "unknown": 0.55,
    }
    tradeability = quality_summary.get(agent_name, {}).get("tradeability", "unknown")
    cap = float(caps.get(tradeability, caps.get("unknown", 0.55)))
    return min(raw_confidence, cap)


def _quality_aware_fusion_context(
    ticker: str,
    analyst_signals: list,
    dynamic_weights: dict,
    full_config: dict,
) -> dict:
    pm_config = _get_portfolio_manager_config(full_config)
    adaptive_config = pm_config.get("adaptive_fusion", {}) or {}
    sector = _sector_for_ticker(ticker)
    sector_weights = _sector_base_weights(ticker, full_config)
    dynamic_weights = _normalize_weights(dynamic_weights or sector_weights)
    sector_weight = float(adaptive_config.get("sector_weight", 0.40))
    sector_weight = max(0.0, min(1.0, sector_weight))
    blended = {
        key: sector_weights[key] * sector_weight + dynamic_weights[key] * (1.0 - sector_weight)
        for key in ANALYST_ORDER
    }
    blended = _normalize_weights(blended)

    signals_by_agent = {}
    for signal in analyst_signals:
        if hasattr(signal, "agent_name"):
            agent_name = _normalize_agent_name(signal.agent_name)
            if agent_name in ANALYST_ORDER:
                signals_by_agent[agent_name] = signal

    applicability_profile = full_config.get("analyst_applicability_profile", {}) or {}
    applicability_adjustments = {}
    if applicability_profile.get("enabled", False):
        sector = _sector_for_ticker(ticker)
        for agent_name in ANALYST_ORDER:
            signal = signals_by_agent.get(agent_name)
            profile = (applicability_profile.get(agent_name) or {})
            multiplier = 1.0
            horizon = str(getattr(signal, "horizon_class", "") or profile.get("default_horizon") or "unknown")
            market_regime = str(getattr(signal, "market_regime", "") or "unknown")
            multiplier *= float((profile.get("horizon_multipliers") or {}).get(horizon, 1.0))
            multiplier *= float((profile.get("sector_multipliers") or {}).get(sector, 1.0))
            multiplier *= float((profile.get("market_regime_multipliers") or {}).get(market_regime, 1.0))
            if agent_name == "commodity_news":
                event_window_days = int(profile.get("event_window_days", 3) or 3)
                expected_days = int(getattr(signal, "expected_horizon_days", 0) or 0) if signal else 0
                if expected_days and expected_days > event_window_days:
                    multiplier *= float(profile.get("outside_event_window_multiplier", 0.60))
            if abs(multiplier - 1.0) > 1e-9:
                before = blended.get(agent_name, 0.0)
                blended[agent_name] = before * multiplier
                applicability_adjustments[agent_name] = {
                    "horizon_class": horizon,
                    "market_regime": market_regime,
                    "sector": sector,
                    "multiplier": multiplier,
                    "weight_before": before,
                    "weight_after": blended[agent_name],
                }
        blended = _normalize_weights(blended)

    quality_summary = {}
    adjusted = {}
    for agent_name in ANALYST_ORDER:
        signal = signals_by_agent.get(agent_name)
        if not signal:
            quality_summary[agent_name] = {
                "signal": "Neutral",
                "raw_confidence": 0.0,
                "effective_confidence": 0.0,
                "tradeability": "missing",
                "risk_flags": ["missing_signal"],
                "quality_multiplier": 0.0,
            }
            adjusted[agent_name] = 0.0
            continue

        quality_multiplier, tradeability, risk_flags = _quality_multiplier(signal, agent_name, full_config)
        effective_confidence = _effective_signal_confidence(
            signal=signal,
            agent_name=agent_name,
            quality_summary={agent_name: {"tradeability": tradeability}},
            full_config=full_config,
        )
        adjusted[agent_name] = blended.get(agent_name, 0.0) * quality_multiplier
        quality_summary[agent_name] = {
            "signal": _signal_to_text(getattr(signal, "signal", "Neutral")),
            "raw_confidence": float(getattr(signal, "confidence", 0.0) or 0.0),
            "effective_confidence": effective_confidence,
            "tradeability": tradeability,
            "business_quality_score": _safe_float(getattr(signal, "business_quality_score", 0.0), 0.0),
            "template_name": getattr(signal, "template_name", "unknown"),
            "horizon_class": getattr(signal, "horizon_class", "unknown"),
            "risk_flags": risk_flags,
            "quality_multiplier": quality_multiplier,
        }

    adjusted = _normalize_weights(adjusted)
    return {
        "sector": sector,
        "sector_weights": sector_weights,
        "dynamic_weights": dynamic_weights,
        "quality_adjusted_weights": adjusted,
        "analyst_quality": quality_summary,
        "analyst_applicability_profile": applicability_adjustments,
        "llm_path": {
            "portfolio_manager": "cloud_only",
            "model": (full_config.get("llm") or {}).get("model"),
            "auditor": "non_llm_deterministic",
        },
    }


def _signed_position_ratio(position, total_portfolio_value: float) -> float:
    """Return the current signed portfolio ratio for one ticker."""
    if not position or total_portfolio_value <= 0:
        return 0.0
    position_value = float(getattr(position, "value", 0.0) or 0.0)
    shares = int(getattr(position, "shares", 0) or 0)
    if shares > 0:
        return position_value / total_portfolio_value
    if shares < 0:
        return -(position_value / total_portfolio_value)
    return 0.0


def _current_net_exposure(portfolio, account_equity: float) -> float:
    """Return signed notional exposure divided by futures account equity."""
    if account_equity <= 0:
        return 0.0

    net_exposure = 0.0
    for position in portfolio.positions.values():
        net_exposure += _signed_position_ratio(position, account_equity)
    return net_exposure


def _resolve_net_exposure_control(full_config: dict, control_diagnostics: dict | None = None) -> tuple[float, bool, str]:
    net_exposure_config = full_config.get('net_exposure_control')
    if net_exposure_config is None:
        risk_control = full_config.get('risk_control', {})
        net_exposure_config = risk_control.get('net_exposure_control', {})
    max_net_exposure = float(net_exposure_config.get('max_net_exposure', 0.50))
    symmetric_scaling = bool(net_exposure_config.get('symmetric_scaling', True))
    cap_mode = "base"

    target = (control_diagnostics or {}).get("capital_utilization_target")
    if (
        isinstance(target, dict)
        and target.get("target_mode") in {"strong_opportunity", "alpha_release_boost", "alpha_release_max_boost"}
        and target.get("high_quality_memory")
    ):
        strong_cap = net_exposure_config.get("strong_opportunity_max_net_exposure")
        if strong_cap is not None:
            max_net_exposure = max(max_net_exposure, float(strong_cap))
            cap_mode = "alpha_release"

    return max_net_exposure, symmetric_scaling, cap_mode


def _days_held(entry_date: str, trading_date) -> int:
    """Best-effort holding-period calculation using normalized YYYY-MM-DD strings."""
    if not entry_date or trading_date is None:
        return 999
    try:
        entry_dt = datetime.strptime(str(entry_date)[:10], "%Y-%m-%d")
        trading_dt = datetime.strptime(str(trading_date)[:10], "%Y-%m-%d")
        return max(0, (trading_dt - entry_dt).days)
    except (TypeError, ValueError):
        return 999

def _to_recommendation_action(action) -> RecommendationAction:
    action_value = action.value if hasattr(action, "value") else str(action)
    mapping = {
        FuturesAction.OPEN_LONG.value: RecommendationAction.OPEN_LONG,
        FuturesAction.OPEN_SHORT.value: RecommendationAction.OPEN_SHORT,
        FuturesAction.CLOSE_LONG.value: RecommendationAction.CLOSE_LONG,
        FuturesAction.CLOSE_SHORT.value: RecommendationAction.CLOSE_SHORT,
        FuturesAction.HOLD.value: RecommendationAction.HOLD,
        "open_long": RecommendationAction.OPEN_LONG,
        "open_short": RecommendationAction.OPEN_SHORT,
        "close_long": RecommendationAction.CLOSE_LONG,
        "close_short": RecommendationAction.CLOSE_SHORT,
        "hold": RecommendationAction.HOLD,
    }
    return mapping.get(action_value, RecommendationAction.HOLD)

def _build_phase1_recommendation(
    config_id: str,
    portfolio,
    ticker: str,
    trading_date,
    contract_code: str,
    decision: FuturesDecision,
    morning_price_context,
    analyst_signals,
    plan_snapshot=None,
    market_confirmation=None,
    full_config=None,
):
    trading_date_value = trading_date.strftime("%Y-%m-%d") if hasattr(trading_date, "strftime") else str(trading_date)
    analyst_signals = list(analyst_signals or [])
    signal_snapshot = {}
    source_artifacts = []
    research_contracts = {}
    opportunity_types = []
    opportunity_layers = []
    factor_focus = []
    evidence_conflicts = []
    for idx, signal in enumerate(analyst_signals):
        key = getattr(signal, "agent_name", None) or f"signal_{idx}"
        signal_payload = signal.model_dump() if hasattr(signal, "model_dump") else dict(signal)
        signal_snapshot[key] = signal_payload
        source_artifacts.append(f"AnalystSignalArtifact:{key}")
        contract = (
            (signal_payload.get("metadata") or {}).get("trade_research_contract")
            if isinstance(signal_payload.get("metadata"), dict)
            else None
        )
        if not isinstance(contract, dict):
            contract = {
                "contract_version": signal_payload.get("research_contract_version", "agentquant.research.v1"),
                "opportunity_type": signal_payload.get("opportunity_type", "unknown"),
                "opportunity_layer": signal_payload.get("opportunity_layer", "direction_only"),
                "entry_trigger": signal_payload.get("entry_trigger", ""),
                "exit_hint": signal_payload.get("exit_hint", ""),
                "holding_period_hint": signal_payload.get("holding_period_hint", ""),
                "factor_focus": signal_payload.get("factor_focus") or [],
                "current_evidence_conflict": signal_payload.get("current_evidence_conflict") or [],
                "invalidation_level": signal_payload.get("invalidation_level"),
                "atr_stop_distance": signal_payload.get("atr_stop_distance"),
                "sample_state": "current_day_evidence",
                "maturity": "candidate",
            }
        research_contracts[key] = contract
        if contract.get("opportunity_type"):
            opportunity_types.append(str(contract.get("opportunity_type")))
        if contract.get("opportunity_layer"):
            opportunity_layers.append(str(contract.get("opportunity_layer")))
        factor_focus.extend([str(item) for item in (contract.get("factor_focus") or [])])
        evidence_conflicts.extend([str(item) for item in (contract.get("current_evidence_conflict") or [])])
    if plan_snapshot:
        signal_snapshot["pre_open_plan"] = plan_snapshot
    if market_confirmation:
        signal_snapshot["market_confirmation"] = market_confirmation
    data_quality_summary = build_pm_data_quality_summary(analyst_signals, market_confirmation)
    signal_snapshot["data_quality_summary"] = data_quality_summary
    if isinstance(full_config, dict):
        try:
            data_quality_path = write_daily_data_quality_summary(
                config=full_config,
                config_id=config_id,
                trading_date=trading_date_value,
                ticker=ticker,
                data_quality_summary=data_quality_summary,
            )
            if data_quality_path:
                signal_snapshot["data_quality_summary_path"] = data_quality_path
        except Exception as exc:
            logger.warning(f"{ticker}: failed to write daily data quality summary: {exc}")
    signal_snapshot["horizon_scope"] = build_horizon_scope(
        analyst_signals,
        decision_horizon=(plan_snapshot or {}).get("decision_horizon", "unknown") if plan_snapshot else "unknown",
        execution_horizon="short",
        validation_horizon=(plan_snapshot or {}).get("validation_horizon", "unknown") if plan_snapshot else "unknown",
    )
    signal_snapshot["business_quality_summary"] = summarize_business_quality(analyst_signals)
    signal_snapshot["trade_research_contracts"] = research_contracts
    signal_snapshot["pm_research_contract_summary"] = {
        "contract_version": "agentquant.research.v1",
        "dominant_opportunity_types": sorted(set(opportunity_types)),
        "opportunity_layers": sorted(set(opportunity_layers)),
        "factor_focus": sorted(set(factor_focus))[:12],
        "current_evidence_conflict": sorted(set(evidence_conflicts))[:12],
        "pm_decision_layer": (
            (plan_snapshot or {}).get("pm_decision_layer")
            or (plan_snapshot or {}).get("capital_allocation_tier")
            or "probe_or_observe"
        ),
        "candidate_memory_is_prior_only": True,
        "requires_current_evidence": True,
    }
    message_contract = build_internal_message_contract(
        agent="portfolio_manager",
        trading_date=trading_date_value,
        ticker=ticker,
        message_type="PMDecisionArtifact",
        source_artifacts=source_artifacts,
    )
    signal_snapshot["pm_internal_message_contract"] = message_contract
    signal_snapshot["pm_internal_message_validation_errors"] = validate_internal_message_contract(message_contract)
    signal_snapshot["audit"] = {
        "pre_open_only": True,
        "info_cutoff": "pre_open",
        "phase1_generates_recommendation_only": True,
    }
    signal_snapshot = attach_snapshot_contract(
        signal_snapshot,
        trading_date=trading_date_value,
        ticker=ticker,
        config_id=config_id,
        source_artifacts=source_artifacts,
    )

    return FuturesRecommendation(
        config_id=config_id,
        reference_portfolio_id=portfolio.id,
        trading_date=trading_date_value,
        effective_trade_date=trading_date_value,
        source_type=RecommendationSourceType.STRATEGY,
        underlying_code=ticker,
        contract_code=contract_code,
        action=_to_recommendation_action(decision.action),
        lots=decision.lots,
        base_price=morning_price_context.base_price if morning_price_context else None,
        base_price_source=morning_price_context.base_price_source if morning_price_context else None,
        base_price_date=morning_price_context.base_price_date if morning_price_context else None,
        open_price=morning_price_context.open_price if morning_price_context else None,
        prev_close_price=morning_price_context.prev_close_price if morning_price_context else None,
        slippage_model="tick",
        slippage_ticks=None,
        slippage_amount=None,
        execution_price=None,
        justification=decision.justification,
        signal_snapshot=signal_snapshot,
        warning_message=morning_price_context.warning_message if morning_price_context else None,
        status=RecommendationStatus.PENDING,
    )

def _resolve_pre_open_signal_direction(target_lots: int) -> str:
    if target_lots > 0:
        return "long"
    if target_lots < 0:
        return "short"
    return "flat"

def _resolve_pre_open_signal_confidence(direction: str, long_scores: dict, short_scores: dict) -> float:
    if direction == "long":
        return float(long_scores.get("confidence", 0.0) or 0.0)
    if direction == "short":
        return float(short_scores.get("confidence", 0.0) or 0.0)
    return 0.0


def _resolve_decision_horizon(analyst_signals: list, target_lots: int) -> str:
    return _fusion_resolve_decision_horizon(analyst_signals, target_lots)

def _build_pre_open_plan_snapshot(
    target_lots: int,
    current_price: float,
    position_ratio: float,
    risk_level: RiskLevel,
    long_scores: dict,
    short_scores: dict,
    margin_rate: float = 0.0,
) -> dict:
    direction = _resolve_pre_open_signal_direction(target_lots)
    return {
        "signal_direction": direction,
        "signal_confidence": _resolve_pre_open_signal_confidence(direction, long_scores, short_scores),
        "target_position_ratio": float(position_ratio),
        "target_margin_ratio_estimate": abs(float(position_ratio)) * max(0.0, float(margin_rate or 0.0)),
        "target_lots_estimate": int(target_lots),
        "reference_price": float(current_price),
        "risk_level": risk_level.value,
    }


def _analyst_signal_combo(analyst_signals: list) -> tuple[str, str, str]:
    return _fusion_analyst_signal_combo(analyst_signals)


def _signal_side_text(value) -> str:
    text = _signal_to_text(value)
    if text == "Bullish":
        return "long"
    if text == "Bearish":
        return "short"
    return "flat"


def _market_regime_from_signals(analyst_signals: list, target_side: str) -> str:
    for signal in analyst_signals or []:
        if _signal_side_text(getattr(signal, "signal", None)) == target_side:
            regime = str(getattr(signal, "market_regime", "") or "unknown")
            if regime and regime != "unknown":
                return regime
    for signal in analyst_signals or []:
        regime = str(getattr(signal, "market_regime", "") or "unknown")
        if regime and regime != "unknown":
            return regime
    return "unknown"


def _signal_template_from_signals(target_side: str, analyst_signals: list, signal_combo: tuple[str, str, str]) -> str:
    for signal in analyst_signals or []:
        if _signal_side_text(getattr(signal, "signal", None)) != target_side:
            continue
        template_name = str(getattr(signal, "template_name", "") or "").strip()
        if template_name and template_name != "unknown":
            horizon = str(
                getattr(signal, "analyst_horizon", None)
                or getattr(signal, "horizon_class", None)
                or "unknown"
            )
            return f"{target_side}_{template_name}_{horizon}"[:160]
    normalized_combo = "_".join(str(item).lower() for item in signal_combo)
    return f"{target_side}_{normalized_combo}"[:160]


def _target_side_from_ratio(position_ratio: float) -> str:
    return _position_target_side_from_ratio(position_ratio)


def _same_sign(lhs: float, rhs: float) -> bool:
    return _position_same_sign(lhs, rhs)


def _is_new_or_increasing_exposure(target_ratio: float, current_ratio: float) -> bool:
    return _position_is_new_or_increasing_exposure(target_ratio, current_ratio)


def _scale_signed_ratio(position_ratio: float, multiplier: float) -> float:
    return _position_scale_signed_ratio(position_ratio, multiplier)


def _strategy_memory_record(strategy_memory: dict, states: set[str]) -> dict:
    return _capital_strategy_memory_record(strategy_memory, states)


def _conflicting_weak_memory_record(strategy_memory: dict, signal_combo: tuple[str, str, str]) -> dict:
    return _capital_conflicting_weak_memory_record(strategy_memory, signal_combo)


def _normalize_trading_day_value(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _block_new_or_incremental_exposure(target_ratio: float, current_ratio: float) -> float:
    """Block only new/incremental risk while preserving existing same-side exposure."""
    if abs(current_ratio) > 1e-12 and _same_sign(target_ratio, current_ratio):
        return float(current_ratio)
    return 0.0


def _cap_new_or_incremental_exposure(
    *,
    target_ratio: float,
    current_ratio: float,
    max_abs_target_ratio: float,
) -> float:
    """Cap the target without turning a same-side add-on into a forced reduction."""
    max_abs_target_ratio = max(0.0, float(max_abs_target_ratio or 0.0))
    if abs(target_ratio) <= 1e-12:
        return 0.0
    target_sign = 1.0 if target_ratio > 0 else -1.0
    if abs(current_ratio) > 1e-12 and _same_sign(target_ratio, current_ratio):
        capped_abs = min(abs(target_ratio), max(abs(current_ratio), max_abs_target_ratio))
    else:
        capped_abs = min(abs(target_ratio), max_abs_target_ratio)
    return target_sign * capped_abs


def _cap_by_incremental_margin_budget(
    *,
    target_ratio: float,
    current_ratio: float,
    margin_rate: float,
    allowed_increment_margin_ratio: float,
) -> float:
    if margin_rate <= 0:
        return target_ratio
    allowed_increment_margin_ratio = max(0.0, float(allowed_increment_margin_ratio or 0.0))
    target_sign = 1.0 if target_ratio > 0 else -1.0
    if abs(current_ratio) > 1e-12 and _same_sign(target_ratio, current_ratio):
        current_margin = abs(current_ratio) * margin_rate
        max_target_margin = current_margin + allowed_increment_margin_ratio
    else:
        max_target_margin = allowed_increment_margin_ratio
    max_abs_target_ratio = max_target_margin / margin_rate
    return _cap_new_or_incremental_exposure(
        target_ratio=target_ratio,
        current_ratio=current_ratio,
        max_abs_target_ratio=max_abs_target_ratio,
    )


def _drawdown_hard_streak_state(
    *,
    db,
    config_id: str,
    trading_date,
    initial_capital: float,
    hard_drawdown: float,
) -> dict:
    trading_day_value = _normalize_trading_day_value(trading_date)
    if not db or not config_id or not trading_day_value:
        return {"consecutive_hard_days": 0, "latest_hard_date": None}
    conn = None
    try:
        conn = db._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT ds.trading_date, ds.current_balance, ds.current_margin
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE p.config_id = ?
              AND substr(ds.trading_date, 1, 10) < ?
            ORDER BY substr(ds.trading_date, 1, 10), ds.created_at
            ''',
            (config_id, trading_day_value),
        )
        peak_equity = float(initial_capital or 0.0)
        hard_streak = 0
        latest_hard_date = None
        for row in cursor.fetchall():
            equity = float(row["current_balance"] or 0.0) + float(row["current_margin"] or 0.0)
            peak_equity = max(peak_equity, equity)
            drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            if drawdown >= hard_drawdown:
                hard_streak += 1
                latest_hard_date = _normalize_trading_day_value(row["trading_date"])
            else:
                hard_streak = 0
                latest_hard_date = None
        return {
            "consecutive_hard_days": hard_streak,
            "latest_hard_date": latest_hard_date,
        }
    except Exception as exc:
        logger.warning(f"Drawdown hard-streak state unavailable: {exc}")
        return {"consecutive_hard_days": 0, "latest_hard_date": None, "error": str(exc)}
    finally:
        if conn:
            conn.close()


def _load_drawdown_control_from_recommendation(db, recommendation_row: dict) -> dict:
    try:
        loader = getattr(db, "_deserialize_external_json", None)
        if callable(loader):
            snapshot = loader(recommendation_row, "signal_snapshot")
        else:
            raw_snapshot = recommendation_row.get("signal_snapshot")
            snapshot = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) and raw_snapshot.strip() else {}
    except Exception:
        snapshot = {}
    if not isinstance(snapshot, dict):
        return {}
    plan = snapshot.get("pre_open_plan") if isinstance(snapshot.get("pre_open_plan"), dict) else {}
    controls = plan.get("strategy_controls") if isinstance(plan.get("strategy_controls"), dict) else {}
    diagnostics = controls.get("diagnostics") if isinstance(controls.get("diagnostics"), dict) else {}
    drawdown = diagnostics.get("drawdown_control") if isinstance(diagnostics.get("drawdown_control"), dict) else {}
    return drawdown if isinstance(drawdown, dict) else {}


def _trading_days_between_settlements(
    *,
    db,
    config_id: str,
    after_date: str,
    before_date: str,
) -> int:
    if not after_date or not before_date:
        return 0
    conn = None
    try:
        conn = db._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT COUNT(DISTINCT substr(ds.trading_date, 1, 10)) AS day_count
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE p.config_id = ?
              AND substr(ds.trading_date, 1, 10) > ?
              AND substr(ds.trading_date, 1, 10) < ?
            ''',
            (config_id, after_date, before_date),
        )
        row = cursor.fetchone()
        return int((row["day_count"] if row else 0) or 0)
    except Exception as exc:
        logger.warning(f"Recovery cooldown day count unavailable: {exc}")
        return 0
    finally:
        if conn:
            conn.close()


def _drawdown_recovery_probe_history(
    *,
    db,
    config_id: str,
    trading_date,
    control: dict,
) -> dict:
    trading_day_value = _normalize_trading_day_value(trading_date)
    if not db or not config_id or not trading_day_value:
        return {"probe_days": 0, "loss_count": 0, "consecutive_profit_days": 0, "cooldown_active": False}
    conn = None
    try:
        conn = db._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT tdp.trading_date, tdp.ticker, tdp.daily_pnl
            FROM ticker_daily_pnl tdp
            JOIN portfolio p ON tdp.portfolio_id = p.id
            WHERE p.config_id = ?
              AND substr(tdp.trading_date, 1, 10) < ?
            ''',
            (config_id, trading_day_value),
        )
        ticker_daily_pnl = {
            (_normalize_trading_day_value(row["trading_date"]), str(row["ticker"] or "").upper()):
            float(row["daily_pnl"] or 0.0)
            for row in cursor.fetchall()
        }
        cursor.execute(
            '''
            SELECT ft.trading_date,
                   ft.ticker,
                   ft.action,
                   ft.recommendation_id,
                   fr.signal_snapshot,
                   fr.signal_snapshot_artifact_path,
                   fr.signal_snapshot_sha256
            FROM futures_transactions ft
            JOIN futures_recommendation fr ON fr.id = ft.recommendation_id
            WHERE ft.config_id = ?
              AND substr(ft.trading_date, 1, 10) < ?
              AND ft.action IN ('open_long', 'open_short')
            ORDER BY substr(ft.trading_date, 1, 10), ft.created_at, ft.id
            ''',
            (config_id, trading_day_value),
        )
        probe_by_day: dict[tuple[str, str], float] = {}
        for row in cursor.fetchall():
            record = dict(row)
            drawdown_diag = _load_drawdown_control_from_recommendation(db, record)
            if str(drawdown_diag.get("mode") or "") != "hard_recovery_probe":
                continue
            day = _normalize_trading_day_value(record.get("trading_date"))
            ticker = str(record.get("ticker") or "").upper()
            cumulative_pnl = sum(
                pnl
                for (pnl_day, pnl_ticker), pnl in ticker_daily_pnl.items()
                if pnl_ticker == ticker and day <= pnl_day < trading_day_value
            )
            probe_by_day[(day, ticker)] = cumulative_pnl

        ordered_probe_days = sorted(
            [
                {"trading_date": day, "ticker": ticker, "probe_cumulative_pnl": pnl}
                for (day, ticker), pnl in probe_by_day.items()
            ],
            key=lambda item: (item["trading_date"], item["ticker"]),
        )
        loss_days = [item for item in ordered_probe_days if float(item.get("probe_cumulative_pnl") or 0.0) < 0]
        last_loss_date = loss_days[-1]["trading_date"] if loss_days else None
        consecutive_profit_days = 0
        for item in reversed(ordered_probe_days):
            if last_loss_date and item["trading_date"] <= last_loss_date:
                break
            if float(item.get("probe_cumulative_pnl") or 0.0) > 0:
                consecutive_profit_days += 1
            elif float(item.get("probe_cumulative_pnl") or 0.0) < 0:
                break

        loss_count = len(loss_days)
        first_loss_cooldown = int(control.get("first_probe_loss_cooldown_days", 2))
        second_loss_cooldown = int(control.get("second_probe_loss_cooldown_days", 3))
        increment = int(control.get("cooldown_increment_days", 1))
        cooldown_days = 0
        days_elapsed = 0
        cooldown_active = False
        if last_loss_date:
            if loss_count <= 1:
                cooldown_days = first_loss_cooldown
            elif loss_count == 2:
                cooldown_days = second_loss_cooldown
            else:
                cooldown_days = second_loss_cooldown + (loss_count - 2) * increment
            days_elapsed = _trading_days_between_settlements(
                db=db,
                config_id=config_id,
                after_date=last_loss_date,
                before_date=trading_day_value,
            )
            cooldown_active = days_elapsed < cooldown_days

        return {
            "probe_days": len(ordered_probe_days),
            "loss_count": loss_count,
            "last_loss_date": last_loss_date,
            "cooldown_days": cooldown_days,
            "cooldown_days_elapsed": days_elapsed,
            "cooldown_active": cooldown_active,
            "observation_only": cooldown_active and loss_count >= 2,
            "consecutive_profit_days": consecutive_profit_days,
            "recent_probe_days": ordered_probe_days[-5:],
        }
    except Exception as exc:
        logger.warning(f"Drawdown recovery probe history unavailable: {exc}")
        return {
            "probe_days": 0,
            "loss_count": 0,
            "consecutive_profit_days": 0,
            "cooldown_active": False,
            "error": str(exc),
        }
    finally:
        if conn:
            conn.close()


def _recovery_probe_budget(control: dict, recovery_history: dict) -> float:
    steps = control.get("recovery_restore_step_margin_ratios") or []
    parsed_steps = []
    for item in steps:
        value = _safe_float(item, 0.0)
        if value > 0:
            parsed_steps.append(value)
    if not parsed_steps:
        parsed_steps = [float(control.get("recovery_probe_margin_ratio_max", 0.02))]
    index = min(int(recovery_history.get("consecutive_profit_days", 0) or 0), len(parsed_steps) - 1)
    return max(0.0, parsed_steps[index])


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


def _apply_market_confirmation_control(
    *,
    position_ratio: float,
    current_ratio: float,
    signal_strength: float,
    market_confirmation: dict,
    full_config: dict,
) -> tuple[float, list[str], list[str]]:
    reasons: list[str] = []
    notes: list[str] = []
    if not _is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, reasons, notes

    target_side = _target_side_from_ratio(position_ratio)
    if target_side not in {"long", "short"}:
        return position_ratio, reasons, notes

    control = full_config.get("market_confirmation", {}) or {}
    if not control.get("enabled", True) or not market_confirmation.get("enabled"):
        return position_ratio, reasons, notes

    conflicts = market_confirmation.get("conflicts") or []
    confirmations = market_confirmation.get("confirmations") or []
    features = market_confirmation.get("features") or []
    confirmation_score = float(market_confirmation.get("confirmation_score", 0.0) or 0.0)

    if bool(control.get("quality_gate_enabled", False)):
        if features:
            gate_failures = []
            min_score = float(control.get("min_confirmation_score_for_new_entry", 0.45))
            if confirmation_score < min_score:
                gate_failures.append(f"score={confirmation_score:.2f}<{min_score:.2f}")

            max_conflicts = control.get("max_conflicts_for_new_entry")
            if max_conflicts is not None and len(conflicts) > int(max_conflicts):
                gate_failures.append(f"conflicts={len(conflicts)}>{int(max_conflicts)}")

            if gate_failures:
                original_ratio = position_ratio
                weak_signal_strength = float(
                    control.get("quality_gate_weak_signal_strength", control.get("weak_signal_strength", 0.25))
                )
                if bool(control.get("quality_gate_block_weak_signal", True)) and signal_strength <= weak_signal_strength:
                    position_ratio = 0.0
                    reasons.append("market_confirmation_quality_gate")
                    notes.append(
                        f"blocked weak {target_side} signal by PandaAI quality gate: {', '.join(gate_failures)}"
                    )
                    return position_ratio, reasons, notes

                position_ratio = _scale_signed_ratio(
                    position_ratio,
                    float(control.get("quality_gate_cap_multiplier", 0.50)),
                )
                reasons.append("market_confirmation_quality_gate")
                notes.append(
                    f"scaled {target_side} ratio {original_ratio:.2%}->{position_ratio:.2%} "
                    f"by PandaAI quality gate: {', '.join(gate_failures)}"
                )
        else:
            notes.append("PandaAI quality gate skipped: no usable pre-open confirmation features")

    if conflicts:
        original_ratio = position_ratio
        weak_signal_strength = float(control.get("weak_signal_strength", 0.25))
        allow_conflicted_probe = bool(control.get("allow_conflicted_probe_with_strong_confirmation", False))
        conflicted_probe_score = float(control.get("conflicted_probe_min_confirmation_score", 0.65))
        conflicted_probe_confirmations = int(control.get("conflicted_probe_min_confirmations", 3))
        strong_conflicted_probe = (
            allow_conflicted_probe
            and confirmation_score >= conflicted_probe_score
            and len(confirmations) >= conflicted_probe_confirmations
        )
        if (
            bool(control.get("block_weak_conflicting_signal", True))
            and signal_strength <= weak_signal_strength
            and not strong_conflicted_probe
        ):
            position_ratio = 0.0
            reasons.append("market_confirmation_conflict")
            notes.append(
                f"blocked weak {target_side} signal due to PandaAI confirmation conflicts={conflicts}"
            )
        else:
            position_ratio = _scale_signed_ratio(
                position_ratio,
                float(control.get("conflict_cap_multiplier", 0.50)),
            )
            reasons.append("market_confirmation_conflict")
            if strong_conflicted_probe and signal_strength <= weak_signal_strength:
                notes.append(
                    f"scaled conflicted {target_side} ratio {original_ratio:.2%}->{position_ratio:.2%} "
                    f"despite weak signal because confirmation_score={confirmation_score:.2f}>={conflicted_probe_score:.2f} "
                    f"and confirmations={len(confirmations)}>={conflicted_probe_confirmations}; conflicts={conflicts}"
                )
            else:
                notes.append(
                    f"scaled {target_side} ratio {original_ratio:.2%}->{position_ratio:.2%} "
                    f"due to PandaAI conflicts={conflicts}"
                )
    elif confirmations:
        notes.append(f"PandaAI confirmation supports {target_side}: {confirmations}")

    return position_ratio, reasons, notes


def _apply_market_data_gap_control(
    *,
    ticker: str,
    position_ratio: float,
    current_ratio: float,
    signal_strength: float,
    market_confirmation: dict,
    full_config: dict,
) -> tuple[float, list[str], list[str], dict]:
    """Degrade new risk when PandaAI evidence is incomplete, without stopping the day."""
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    if not _is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, reasons, notes, diagnostics

    target_side = _target_side_from_ratio(position_ratio)
    if target_side not in {"long", "short"}:
        return position_ratio, reasons, notes, diagnostics

    control = full_config.get("market_confirmation", {}) or {}
    if not control.get("enabled", True) or not control.get("missing_data_degradation_enabled", True):
        return position_ratio, reasons, notes, diagnostics
    if not market_confirmation.get("enabled"):
        return position_ratio, reasons, notes, diagnostics

    actionable_missing = [str(item) for item in (market_confirmation.get("data_missing") or [])]
    parameter_errors = [str(item) for item in (market_confirmation.get("parameter_errors") or [])]
    provider_errors = [str(item) for item in (market_confirmation.get("errors") or [])]
    unavailable = [str(item) for item in (market_confirmation.get("data_unavailable") or [])]
    fallback_covered = {str(item) for item in (market_confirmation.get("fallback_covered_missing") or [])}
    actionable_missing = [item for item in actionable_missing if item not in fallback_covered]
    gap_count = len(actionable_missing) + len(parameter_errors) + len(provider_errors)
    features = market_confirmation.get("features") or []
    if gap_count <= 0:
        diagnostics["market_data_gap_control"] = {
            "enabled": True,
            "decision": "no_actionable_gap",
            "fallback_covered_missing": sorted(fallback_covered),
        }
        return position_ratio, reasons, notes, diagnostics

    min_features = int(control.get("min_usable_features_after_data_gap", 1) or 1)
    max_missing = int(control.get("max_actionable_missing_for_normal_entry", 2) or 2)
    cap_multiplier = float(control.get("data_gap_cap_multiplier", 0.50) or 0.50)
    block_without_features = bool(control.get("data_gap_block_when_no_features", True))
    weak_signal_strength = float(control.get("weak_signal_strength", 0.25) or 0.25)
    original_ratio = position_ratio
    decision = "allow_with_gap"

    if len(features) < min_features and block_without_features:
        position_ratio = 0.0
        decision = "block_no_usable_features"
        reasons.append("market_confirmation_data_gap")
        notes.append(
            f"{ticker} {target_side} blocked: PandaAI evidence gap leaves "
            f"{len(features)} usable features; missing={actionable_missing}, errors={parameter_errors or provider_errors}"
        )
    elif gap_count > max_missing or parameter_errors or provider_errors:
        if signal_strength <= weak_signal_strength and bool(control.get("data_gap_block_weak_signal", False)):
            position_ratio = 0.0
            decision = "block_weak_data_gap"
        else:
            position_ratio = _scale_signed_ratio(position_ratio, cap_multiplier)
            decision = "scale_data_gap"
        reasons.append("market_confirmation_data_gap")
        notes.append(
            f"{ticker} {target_side} degraded by PandaAI data gap: "
            f"{original_ratio:.2%}->{position_ratio:.2%}, "
            f"missing={actionable_missing}, parameter_errors={parameter_errors}, provider_errors={provider_errors}"
        )

    diagnostics["market_data_gap_control"] = {
        "enabled": True,
        "decision": decision,
        "actionable_missing": actionable_missing,
        "data_unavailable": unavailable,
        "parameter_errors": parameter_errors,
        "provider_errors": provider_errors,
        "fallback_covered_missing": sorted(fallback_covered),
        "usable_feature_count": len(features),
        "original_ratio": float(original_ratio),
        "final_ratio": float(position_ratio),
    }
    return position_ratio, reasons, notes, diagnostics


def _apply_trade_frequency_control(
    *,
    db,
    config_id: str,
    ticker: str,
    trading_date,
    position_ratio: float,
    current_ratio: float,
    signal_combo: tuple[str, str, str],
    market_confirmation: dict,
    full_config: dict,
) -> tuple[float, list[str], list[str], dict]:
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    control = full_config.get("trade_frequency_control", {}) or {}
    if not control.get("enabled", False):
        return position_ratio, reasons, notes, diagnostics

    target_side = _target_side_from_ratio(position_ratio)
    if target_side not in {"long", "short"}:
        return position_ratio, reasons, notes, diagnostics
    if not _is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, reasons, notes, diagnostics

    original_ratio = position_ratio
    side_override = ((control.get("side_overrides") or {}).get(ticker) or {})
    override_key = f"{target_side}_cap_multiplier"
    if override_key in side_override:
        multiplier = float(side_override.get(override_key, 1.0))
        position_ratio = _scale_signed_ratio(position_ratio, multiplier)
        reasons.append("trade_frequency_control")
        notes.append(
            f"{ticker} {target_side} static attribution cap: {original_ratio:.2%}->{position_ratio:.2%}"
        )

    side_perf = {}
    if db and config_id:
        side_perf = db.get_futures_trade_pair_performance(
            config_id=config_id,
            ticker=ticker,
            side=target_side,
            trading_date=trading_date,
            lookback_trades=int(control.get("lookback_trades", 20)),
        )
    diagnostics["side_performance"] = side_perf

    if int(side_perf.get("total_trades", 0) or 0) >= int(control.get("min_completed_trades", 8)):
        win_rate = float(side_perf.get("win_rate", 0.0) or 0.0)
        total_pnl = float(side_perf.get("total_pnl", 0.0) or 0.0)
        severe_pnl_threshold = control.get("severe_total_pnl_below")
        weak_pnl_threshold = control.get("weak_total_pnl_below")
        if severe_pnl_threshold is not None and total_pnl <= float(severe_pnl_threshold):
            reasons.append("side_performance_block")
            if bool(control.get("severe_block_new_entries", True)):
                notes.append(
                    f"{ticker} {target_side} blocked: recent completed-trade PnL={total_pnl:.0f}"
                )
                position_ratio = 0.0
            else:
                position_ratio = _scale_signed_ratio(position_ratio, float(control.get("weak_cap_multiplier", 0.50)))
        elif win_rate <= float(control.get("severe_win_rate_below", 0.30)):
            reasons.append("side_performance_block")
            if bool(control.get("severe_block_new_entries", True)):
                notes.append(
                    f"{ticker} {target_side} blocked: recent completed-trade win_rate={win_rate:.2%}"
                )
                position_ratio = 0.0
            else:
                position_ratio = _scale_signed_ratio(position_ratio, float(control.get("weak_cap_multiplier", 0.50)))
        elif weak_pnl_threshold is not None and total_pnl <= float(weak_pnl_threshold):
            before = position_ratio
            position_ratio = _scale_signed_ratio(position_ratio, float(control.get("weak_cap_multiplier", 0.50)))
            reasons.append("trade_frequency_control")
            notes.append(
                f"{ticker} {target_side} scaled by recent PnL={total_pnl:.0f}: "
                f"{before:.2%}->{position_ratio:.2%}"
            )
        elif win_rate <= float(control.get("weak_win_rate_below", 0.38)):
            before = position_ratio
            position_ratio = _scale_signed_ratio(position_ratio, float(control.get("weak_cap_multiplier", 0.50)))
            reasons.append("trade_frequency_control")
            notes.append(
                f"{ticker} {target_side} scaled by recent win_rate={win_rate:.2%}: "
                f"{before:.2%}->{position_ratio:.2%}"
            )

    weak_combos = [tuple(item) for item in (control.get("weak_signal_combos") or [])]
    if signal_combo in weak_combos and target_side in {"long", "short"}:
        if not market_confirmation.get("features"):
            diagnostics["weak_signal_combo_skip"] = "no_market_confirmation_features"
            return position_ratio, reasons, notes, diagnostics
        confirmation_control = full_config.get("market_confirmation", {}) or {}
        min_confirmations = int(confirmation_control.get("min_confirmations_for_new_entry", 2))
        min_score = float(confirmation_control.get("min_confirmation_score_for_weak_combo", 0.0) or 0.0)
        confirmations = market_confirmation.get("confirmations") or []
        confirmation_score = float(market_confirmation.get("confirmation_score", 0.0) or 0.0)
        if len(confirmations) < min_confirmations or confirmation_score < min_score:
            before = position_ratio
            if abs(current_ratio) <= 1e-12:
                position_ratio = 0.0
            else:
                position_ratio = _scale_signed_ratio(position_ratio, float(control.get("weak_cap_multiplier", 0.50)))
            reasons.append("weak_signal_combo")
            notes.append(
                f"weak analyst combo {signal_combo} requires {min_confirmations} confirmations "
                f"and score>={min_score:.2f}; got confirmations={len(confirmations)}, "
                f"score={confirmation_score:.2f}, ratio {before:.2%}->{position_ratio:.2%}"
            )

    return position_ratio, reasons, notes, diagnostics


def _apply_drawdown_and_ticker_loss_control(
    *,
    db,
    config_id: str,
    ticker: str,
    trading_date,
    position_ratio: float,
    current_ratio: float,
    current_margin_ratio: float = 0.0,
    margin_rate: float = 0.0,
    market_confirmation: dict | None = None,
    signal_combo: tuple[str, str, str] | None = None,
    strategy_memory: dict | None = None,
    analyst_signals: list | None = None,
    full_config: dict,
) -> tuple[float, list[str], list[str], dict]:
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    if not _is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, reasons, notes, diagnostics

    drawdown_control = full_config.get("drawdown_control", {}) or {}
    if drawdown_control.get("enabled", False) and db and config_id:
        drawdown_state = db.get_account_drawdown_state(
            config_id=config_id,
            trading_date=trading_date,
            initial_capital=float(full_config.get("cashflow", 0.0) or 0.0),
        )
        diagnostics["drawdown_state"] = drawdown_state
        drawdown = _safe_float(drawdown_state.get("drawdown"), 0.0)
        warning_drawdown = _safe_float(
            drawdown_control.get("warning_drawdown", drawdown_control.get("soft_drawdown")),
            0.04,
        )
        hard_drawdown = _safe_float(drawdown_control.get("hard_drawdown"), 0.05)
        confirmation = market_confirmation or {}
        confirmations = confirmation.get("confirmations") or []
        confirmation_score = _safe_float(confirmation.get("confirmation_score"), 0.0)
        stop_protected = _has_explicit_stop_protection(analyst_signals or [])
        target_side = _target_side_from_ratio(position_ratio)
        drawdown_diag = {
            "enabled": True,
            "drawdown": drawdown,
            "warning_drawdown": warning_drawdown,
            "hard_drawdown": hard_drawdown,
            "current_margin_ratio": float(current_margin_ratio or 0.0),
            "margin_rate": float(margin_rate or 0.0),
            "target_side": target_side,
            "stop_protected": bool(stop_protected),
            "confirmation_score": confirmation_score,
            "confirmation_count": len(confirmations),
            "pre_control_ratio": float(position_ratio),
            "current_ratio": float(current_ratio),
        }
        diagnostics["drawdown_control"] = drawdown_diag

        if drawdown >= hard_drawdown:
            hard_streak = _drawdown_hard_streak_state(
                db=db,
                config_id=config_id,
                trading_date=trading_date,
                initial_capital=float(full_config.get("cashflow", 0.0) or 0.0),
                hard_drawdown=hard_drawdown,
            )
            recovery_history = _drawdown_recovery_probe_history(
                db=db,
                config_id=config_id,
                trading_date=trading_date,
                control=drawdown_control,
            )
            initial_cooldown_days = max(0, int(drawdown_control.get("initial_hard_cooldown_days", 1)))
            initial_cooldown_active = (
                initial_cooldown_days > 0
                and int(hard_streak.get("consecutive_hard_days", 0) or 0) <= initial_cooldown_days
            )
            drawdown_diag.update({
                "state": "hard_protection",
                "hard_streak": hard_streak,
                "recovery_history": recovery_history,
                "initial_cooldown_days": initial_cooldown_days,
                "initial_cooldown_active": initial_cooldown_active,
            })

            if initial_cooldown_active or bool(recovery_history.get("cooldown_active")):
                before = position_ratio
                position_ratio = _block_new_or_incremental_exposure(position_ratio, current_ratio)
                reasons.append("drawdown_control")
                mode = "hard_initial_cooldown" if initial_cooldown_active else "hard_observation_only"
                drawdown_diag.update({
                    "mode": mode,
                    "shadow_recommendation": True,
                    "final_ratio": float(position_ratio),
                })
                notes.append(
                    f"hard drawdown {mode}: drawdown={drawdown:.2%}; "
                    f"new/incremental exposure {before:.2%}->{position_ratio:.2%}; "
                    "agents continue analysis and shadow recommendation logging"
                )
            else:
                min_score = _safe_float(drawdown_control.get("recovery_probe_min_confirmation_score"), 0.65)
                min_confirmations = int(drawdown_control.get("recovery_probe_min_confirmations", 3))
                require_stop = bool(drawdown_control.get("recovery_probe_require_stop_protection", True))
                weak_conflict = (
                    _conflicting_weak_memory_record(strategy_memory or {}, signal_combo or ("*", "*", "*"))
                    if bool(drawdown_control.get("recovery_probe_block_weak_memory", True))
                    else {}
                )
                gate_failures = []
                if target_side not in {"long", "short"}:
                    gate_failures.append("no_directional_new_entry")
                if len(confirmations) < min_confirmations:
                    gate_failures.append("insufficient_market_confirmations")
                if confirmation_score < min_score:
                    gate_failures.append("market_confirmation_score_below_probe_threshold")
                if require_stop and not stop_protected:
                    gate_failures.append("missing_stop_or_invalidation_boundary")
                if weak_conflict:
                    gate_failures.append("conflicting_weak_memory")

                drawdown_diag.update({
                    "mode": "hard_recovery_probe_candidate",
                    "recovery_probe_min_confirmation_score": min_score,
                    "recovery_probe_min_confirmations": min_confirmations,
                    "recovery_probe_require_stop_protection": require_stop,
                    "weak_conflict": weak_conflict,
                    "gate_failures": gate_failures,
                })

                if gate_failures:
                    before = position_ratio
                    position_ratio = _block_new_or_incremental_exposure(position_ratio, current_ratio)
                    reasons.append("drawdown_control")
                    drawdown_diag.update({
                        "mode": "hard_recovery_shadow_only",
                        "shadow_recommendation": True,
                        "final_ratio": float(position_ratio),
                    })
                    notes.append(
                        f"hard drawdown recovery shadow-only: failures={gate_failures}; "
                        f"new/incremental exposure {before:.2%}->{position_ratio:.2%}"
                    )
                else:
                    before = position_ratio
                    recovery_budget = _recovery_probe_budget(drawdown_control, recovery_history)
                    recovery_budget = min(
                        recovery_budget,
                        _safe_float(drawdown_control.get("recovery_probe_margin_ratio_max"), recovery_budget or 0.02),
                    )
                    if recovery_budget <= 0:
                        recovery_budget = _safe_float(drawdown_control.get("recovery_probe_margin_ratio_max"), 0.02)
                    allowed_increment_margin = max(0.0, recovery_budget - float(current_margin_ratio or 0.0))
                    if allowed_increment_margin <= 0 and not (
                        abs(current_ratio) > 1e-12 and _same_sign(position_ratio, current_ratio)
                    ):
                        position_ratio = _block_new_or_incremental_exposure(position_ratio, current_ratio)
                        reasons.append("drawdown_control")
                        drawdown_diag.update({
                            "mode": "hard_recovery_shadow_only",
                            "shadow_recommendation": True,
                            "recovery_probe_margin_ratio_budget": recovery_budget,
                            "allowed_increment_margin_ratio": allowed_increment_margin,
                            "gate_failures": ["no_capacity_under_recovery_probe_budget"],
                            "final_ratio": float(position_ratio),
                        })
                        notes.append(
                            f"hard drawdown recovery shadow-only: current_margin={float(current_margin_ratio or 0.0):.2%} "
                            f">= recovery_budget={recovery_budget:.2%}; "
                            f"new/incremental exposure {before:.2%}->{position_ratio:.2%}"
                        )
                    else:
                        position_ratio = _cap_by_incremental_margin_budget(
                            target_ratio=position_ratio,
                            current_ratio=current_ratio,
                            margin_rate=margin_rate,
                            allowed_increment_margin_ratio=allowed_increment_margin,
                        )
                        if abs(position_ratio - before) > 1e-12:
                            reasons.append("drawdown_recovery_probe")
                        drawdown_diag.update({
                            "mode": "hard_recovery_probe",
                            "shadow_recommendation": False,
                            "recovery_probe_margin_ratio_budget": recovery_budget,
                            "allowed_increment_margin_ratio": allowed_increment_margin,
                            "final_ratio": float(position_ratio),
                        })
                        notes.append(
                            f"hard drawdown recovery probe allowed: drawdown={drawdown:.2%}, "
                            f"budget={recovery_budget:.2%}, ratio {before:.2%}->{position_ratio:.2%}"
                        )
        elif drawdown >= warning_drawdown:
            before = position_ratio
            warning_multiplier = _safe_float(
                drawdown_control.get("warning_cap_multiplier", drawdown_control.get("soft_cap_multiplier")),
                0.60,
            )
            position_ratio = _scale_signed_ratio(position_ratio, warning_multiplier)
            warning_target = _safe_float(drawdown_control.get("warning_target_margin_ratio_max"), 0.04)
            if warning_target > 0 and margin_rate > 0:
                allowed_increment_margin = max(0.0, warning_target - float(current_margin_ratio or 0.0))
                if allowed_increment_margin <= 0 and not (
                    abs(current_ratio) > 1e-12 and _same_sign(position_ratio, current_ratio)
                ):
                    position_ratio = _block_new_or_incremental_exposure(position_ratio, current_ratio)
                else:
                    position_ratio = _cap_by_incremental_margin_budget(
                        target_ratio=position_ratio,
                        current_ratio=current_ratio,
                        margin_rate=margin_rate,
                        allowed_increment_margin_ratio=allowed_increment_margin,
                    )
            reasons.append("drawdown_control")
            drawdown_diag.update({
                "state": "warning",
                "mode": "warning_scaled_risk",
                "warning_cap_multiplier": warning_multiplier,
                "warning_target_margin_ratio_max": warning_target,
                "final_ratio": float(position_ratio),
            })
            notes.append(
                f"warning drawdown control: drawdown={drawdown:.2%}, "
                f"ratio {before:.2%}->{position_ratio:.2%}; high-conviction scaling disabled"
            )
        else:
            drawdown_diag.update({
                "state": "normal",
                "mode": "normal",
                "final_ratio": float(position_ratio),
            })

    ticker_loss_control = full_config.get("ticker_loss_control", {}) or {}
    if ticker_loss_control.get("enabled", False) and db and config_id and abs(position_ratio) > 0:
        loss_state = db.get_ticker_loss_state(
            config_id=config_id,
            ticker=ticker,
            trading_date=trading_date,
            lookback_days=int(ticker_loss_control.get("lookback_days", 5)),
        )
        diagnostics["ticker_loss_state"] = loss_state
        loss_threshold = float(ticker_loss_control.get("loss_threshold", -8000))
        consecutive_limit = int(ticker_loss_control.get("block_new_entries_after_consecutive_losses", 3))
        if int(loss_state.get("consecutive_loss_days", 0) or 0) >= consecutive_limit:
            position_ratio = _block_new_or_incremental_exposure(position_ratio, current_ratio)
            reasons.append("ticker_loss_control")
            notes.append(
                f"{ticker} blocked by consecutive loss days={loss_state.get('consecutive_loss_days')}"
            )
        elif float(loss_state.get("cumulative_pnl", 0.0) or 0.0) <= loss_threshold:
            before = position_ratio
            position_ratio = _scale_signed_ratio(position_ratio, float(ticker_loss_control.get("cap_multiplier", 0.50)))
            reasons.append("ticker_loss_control")
            notes.append(
                f"{ticker} recent loss control: cumulative_pnl={loss_state.get('cumulative_pnl'):.0f}, "
                f"ratio {before:.2%}->{position_ratio:.2%}"
            )

    return position_ratio, reasons, notes, diagnostics


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _analyst_signal_payloads(analyst_signals: list, fusion_context=None) -> dict:
    fusion_context = fusion_context or {}
    quality_summary = fusion_context.get("analyst_quality") or {}
    payloads = {}
    for signal in analyst_signals:
        agent_name = _normalize_agent_name(getattr(signal, "agent_name", ""))
        if agent_name not in ANALYST_ORDER:
            continue
        signal_text = _signal_to_text(getattr(signal, "signal", "Neutral"))
        side = "neutral"
        if signal_text.upper() == "BULLISH":
            side = "long"
        elif signal_text.upper() == "BEARISH":
            side = "short"

        metadata = _signal_metadata(signal)
        context = _nested_context(metadata, agent_name)
        business = metadata.get("business_quality") if isinstance(metadata.get("business_quality"), dict) else {}
        quality = quality_summary.get(agent_name, {})
        effective_confidence = _safe_float(
            quality.get("effective_confidence"),
            _safe_float(getattr(signal, "confidence", 0.0), 0.0),
        )
        payloads[agent_name] = {
            "signal": signal_text,
            "side": side,
            "raw_confidence": _safe_float(getattr(signal, "confidence", 0.0), 0.0),
            "effective_confidence": effective_confidence,
            "tradeability": str(
                quality.get("tradeability")
                or metadata.get("tradeability")
                or context.get("tradeability")
                or "unknown"
            ).lower(),
            "business_quality_score": _safe_float(
                getattr(signal, "business_quality_score", business.get("score", 0.0)),
                0.0,
            ),
            "template_name": str(getattr(signal, "template_name", metadata.get("template_name", "unknown")) or "unknown"),
            "horizon_class": str(getattr(signal, "horizon_class", "unknown") or "unknown"),
            "analyst_horizon": str(getattr(signal, "analyst_horizon", getattr(signal, "horizon_class", "unknown")) or "unknown"),
            "primary_business_driver": getattr(signal, "primary_business_driver", business.get("primary_business_driver", "")),
            "counter_evidence": getattr(signal, "counter_evidence", business.get("counter_evidence", "")),
            "risk_flags": quality.get("risk_flags") or metadata.get("risk_flags") or context.get("risk_flags") or [],
            "metadata": metadata,
            "context": context,
        }
    return payloads


def _payload_supports_side(payload, side: str, min_confidence: float) -> bool:
    if not payload or side not in {"long", "short"}:
        return False
    return (
        payload.get("side") == side
        and _safe_float(payload.get("effective_confidence"), 0.0) >= min_confidence
    )


def _fundamental_anchor_supports(payload, side: str, control: dict) -> bool:
    if not payload or side not in {"long", "short"}:
        return False
    allowed_tradeability = {
        str(item).lower()
        for item in (control.get("fundamental_anchor_tradeability") or ["high", "medium"])
    }
    min_confidence = float(control.get("min_fundamental_anchor_confidence", 0.40))
    min_business_quality = float(control.get("min_fundamental_anchor_business_quality", 0.55))
    return (
        _payload_supports_side(payload, side, min_confidence)
        and str(payload.get("tradeability", "unknown")).lower() in allowed_tradeability
        and _safe_float(payload.get("business_quality_score"), 0.0) >= min_business_quality
    )


def _news_high_quality_override(payload, side: str, control: dict) -> bool:
    if not payload or side not in {"long", "short"}:
        return False
    required_tradeability = str(control.get("news_override_tradeability", "high")).lower()
    min_confidence = float(control.get("news_override_min_confidence", 0.60))
    if not _payload_supports_side(payload, side, min_confidence):
        return False
    if str(payload.get("tradeability", "unknown")).lower() != required_tradeability:
        return False

    context = payload.get("context") or {}
    metadata = payload.get("metadata") or {}
    freshness = _safe_float(context.get("freshness_score", metadata.get("freshness_score")), 0.0)
    relevance = _safe_float(context.get("relevance_score", metadata.get("relevance_score")), 0.0)
    return (
        freshness >= float(control.get("news_override_min_freshness", 0.70))
        and relevance >= float(control.get("news_override_min_relevance", 0.70))
    )


def _side_signal_strength(side: str, long_scores: dict, short_scores: dict) -> float:
    scores = long_scores if side == "long" else short_scores if side == "short" else {}
    return _safe_float(scores.get("score"), 0.0) * _safe_float(scores.get("confidence"), 0.0)


def _apply_business_quality_position_gate(
    *,
    position_ratio: float,
    current_ratio: float,
    analyst_signals: list,
    full_config: dict,
) -> tuple[float, list[str], list[str], dict]:
    return business_quality_position_gate(
        position_ratio=position_ratio,
        current_ratio=current_ratio,
        analyst_signals=analyst_signals,
        config=full_config,
    )


def _signed_abs(side: str, abs_ratio: float) -> float:
    return abs(abs_ratio) if side == "long" else -abs(abs_ratio)


def _position_pnl_ratio(current_position) -> float:
    if not current_position:
        return 0.0
    margin_used = _safe_float(getattr(current_position, "margin_used", 0.0), 0.0)
    if margin_used <= 0:
        return 0.0
    return _safe_float(getattr(current_position, "unrealized_pnl", 0.0), 0.0) / margin_used


def _lifecycle_revalidated_by_current_evidence(
    *,
    current_side: str,
    current_strength: float,
    confirmation_score: float,
    fundamental_supports_current: bool,
    technical_supports_current: bool,
    news_supports_current: bool,
    lifecycle_config: dict,
) -> bool:
    if current_side not in {"long", "short"}:
        return False
    anchor_min_confirmation = float(
        lifecycle_config.get(
            "loss_revalidation_anchor_min_confirmation_score",
            lifecycle_config.get("loss_revalidation_min_confirmation_score", 0.55),
        )
    )
    anchor_min_strength = float(
        lifecycle_config.get(
            "loss_revalidation_anchor_min_signal_strength",
            lifecycle_config.get("loss_revalidation_min_signal_strength", 0.25),
        )
    )
    anchor_confirmed = (
        confirmation_score >= anchor_min_confirmation
        or current_strength >= anchor_min_strength
    )
    if (fundamental_supports_current or news_supports_current) and anchor_confirmed:
        return True
    min_confirmation = float(lifecycle_config.get("loss_revalidation_min_confirmation_score", 0.55))
    min_strength = float(lifecycle_config.get("loss_revalidation_min_signal_strength", 0.25))
    return (
        technical_supports_current
        and confirmation_score >= min_confirmation
        and current_strength >= min_strength
    )


def _side_signal_counts(payloads: dict, side: str) -> dict:
    counts = {"same": 0, "opposite": 0, "neutral": 0}
    opposite = "short" if side == "long" else "long" if side == "short" else ""
    for payload in (payloads or {}).values():
        payload_side = str((payload or {}).get("side") or "neutral").lower()
        if payload_side == side:
            counts["same"] += 1
        elif payload_side == opposite:
            counts["opposite"] += 1
        elif payload_side == "neutral":
            counts["neutral"] += 1
    return counts


def _has_short_timing_support(
    *,
    side: str,
    technical_payload: dict | None,
    news_payload: dict | None,
    market_confirmation: dict | None,
    control: dict,
) -> bool:
    if side not in {"long", "short"}:
        return False
    min_confidence = float(control.get("min_short_timing_confidence", 0.45))
    min_confirmation = float(control.get("min_confirmation_score", 0.55))
    confirmation_score = _safe_float((market_confirmation or {}).get("confirmation_score"), 0.0)
    technical_ok = _payload_supports_side(technical_payload, side, min_confidence)
    news_ok = (
        _payload_supports_side(news_payload, side, min_confidence)
        and str((news_payload or {}).get("horizon_class", "")).lower() in {"short", "event_short", "unknown"}
    )
    return bool((technical_ok or news_ok) and confirmation_score >= min_confirmation)


def _horizon_consistency_result(
    *,
    side: str,
    target_lots_hint: int,
    analyst_signals: list,
    technical_payload: dict | None,
    news_payload: dict | None,
    market_confirmation: dict | None,
    control: dict,
    has_invalidation: bool,
) -> dict:
    side_signal = "Bullish" if side == "long" else "Bearish" if side == "short" else "Neutral"
    matching_horizons = []
    for signal in analyst_signals or []:
        if _signal_to_text(getattr(signal, "signal", "Neutral")) != side_signal:
            continue
        analyst_horizon = str(getattr(signal, "analyst_horizon", "") or "").lower()
        horizon_class = str(getattr(signal, "horizon_class", "") or "").lower()
        matching_horizons.append(horizon_class if analyst_horizon in {"", "unknown"} else analyst_horizon)
    decision_horizon = _resolve_decision_horizon(analyst_signals, target_lots_hint)
    if str(decision_horizon).lower() in {"unknown", "flat"}:
        if "medium" in matching_horizons:
            decision_horizon = "medium"
        elif "long" in matching_horizons:
            decision_horizon = "long"
        elif "event_short" in matching_horizons:
            decision_horizon = "event_short"
        elif "short" in matching_horizons:
            decision_horizon = "short"
    detail = {
        "enabled": bool(control.get("enabled", True)),
        "decision_horizon": decision_horizon,
        "requires_short_timing": False,
        "short_timing_support": False,
        "has_invalidation": bool(has_invalidation),
        "fail_reasons": [],
    }
    if not detail["enabled"] or side not in {"long", "short"}:
        detail["passed"] = True
        return detail

    requires_short_timing = (
        bool(control.get("medium_requires_short_timing", True))
        and str(decision_horizon).lower() in {"medium", "long"}
    )
    detail["requires_short_timing"] = bool(requires_short_timing)
    short_timing_support = _has_short_timing_support(
        side=side,
        technical_payload=technical_payload,
        news_payload=news_payload,
        market_confirmation=market_confirmation,
        control=control,
    )
    detail["short_timing_support"] = bool(short_timing_support)
    if requires_short_timing and not short_timing_support:
        detail["fail_reasons"].append("missing_short_timing_confirmation")
    if requires_short_timing and bool(control.get("medium_requires_invalidation", True)) and not has_invalidation:
        detail["fail_reasons"].append("missing_invalidation_boundary")
    detail["passed"] = not detail["fail_reasons"]
    return detail


def _apply_directional_override(
    *,
    ticker: str,
    position_risk: PositionRisk,
    long_scores: dict,
    short_scores: dict,
    max_position_ratio: float,
    risk_level: RiskLevel,
    full_config: dict,
) -> tuple[list[str], list[str], dict]:
    """Let high-quality blended bearish evidence create a small short probe."""
    control = full_config.get("directional_override_control", {}) or {}
    if not control.get("enabled", True):
        return [], [], {}

    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    long_strength = _safe_float(long_scores.get("score"), 0.0) * _safe_float(long_scores.get("confidence"), 0.0)
    short_strength = _safe_float(short_scores.get("score"), 0.0) * _safe_float(short_scores.get("confidence"), 0.0)
    diagnostics["directional_override"] = {
        "long_strength": float(long_strength),
        "short_strength": float(short_strength),
        "raw_position_ratio": float(position_risk.optimal_position_ratio or 0.0),
    }

    if not bool(control.get("allow_high_quality_short", True)):
        return reasons, notes, diagnostics
    if position_risk.optimal_position_ratio < 0:
        return reasons, notes, diagnostics

    min_score = float(control.get("min_short_score", 0.45))
    min_confidence = float(control.get("min_short_confidence", 0.55))
    min_strength = float(control.get("min_short_strength", 0.30))
    min_edge = float(control.get("min_short_edge", 0.08))
    if (
        _safe_float(short_scores.get("score"), 0.0) < min_score
        or _safe_float(short_scores.get("confidence"), 0.0) < min_confidence
        or short_strength < min_strength
        or short_strength - long_strength < min_edge
    ):
        diagnostics["directional_override"]["decision"] = "skip_short_threshold"
        return reasons, notes, diagnostics

    blended_ratio, blended_direction = calculate_position_ratio_with_balance(
        ticker=ticker,
        long_scores=long_scores,
        short_scores=short_scores,
        max_position_ratio=max_position_ratio,
        risk_level=risk_level,
        full_config=full_config,
    )
    if blended_direction != "SHORT" or blended_ratio <= 0:
        diagnostics["directional_override"]["decision"] = "skip_non_short_blend"
        return reasons, notes, diagnostics

    max_probe_ratio = float(control.get("short_probe_max_ratio", 0.03))
    before = float(position_risk.optimal_position_ratio or 0.0)
    position_risk.optimal_position_ratio = -min(abs(blended_ratio), max_probe_ratio, max_position_ratio)
    reasons.append("high_quality_bearish_short_probe")
    notes.append(
        f"{ticker} high-quality bearish blend overrides non-short target: "
        f"{before:.2%}->{position_risk.optimal_position_ratio:.2%}, "
        f"short_strength={short_strength:.2f}, long_strength={long_strength:.2f}"
    )
    position_risk.justification += (
        f"\n[Directional override: high-quality bearish blend created a small SHORT probe; "
        f"short_strength={short_strength:.2f}, long_strength={long_strength:.2f}]"
    )
    diagnostics["directional_override"]["decision"] = "short_probe"
    diagnostics["directional_override"]["final_position_ratio"] = float(position_risk.optimal_position_ratio)
    return reasons, notes, diagnostics


def _apply_holding_rebalance_control(
    *,
    ticker: str,
    trading_date,
    position_ratio: float,
    current_ratio: float,
    current_position,
    analyst_signals: list,
    long_scores: dict,
    short_scores: dict,
    market_confirmation: dict,
    full_config: dict,
    fusion_context: dict,
    risk_level: RiskLevel,
    adaptive_policy_state: list | None = None,
) -> tuple[float, list[str], list[str], dict]:
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    control = _get_holding_rebalance_config(full_config)
    if not control.get("enabled", True):
        return position_ratio, reasons, notes, diagnostics

    current_side = _target_side_from_ratio(current_ratio)
    target_side = _target_side_from_ratio(position_ratio)
    calibration_side = target_side if target_side in {"long", "short"} else current_side
    calibration_horizon = _resolve_decision_horizon(
        analyst_signals,
        1 if calibration_side == "long" else -1 if calibration_side == "short" else 0,
    )
    calibration_regime = _market_regime_from_signals(analyst_signals, calibration_side)
    contextual_cfg = (((full_config.get("learning") or {}).get("contextual_rule_calibration")) or {})
    contextual_diag = {}
    if bool(contextual_cfg.get("enabled", True)) and calibration_side in {"long", "short"}:
        control, contextual_diag = apply_pm_contextual_calibration(
            control,
            adaptive_policy_state or [],
            ticker=ticker,
            side=calibration_side,
            horizon_class=calibration_horizon,
            market_regime=calibration_regime,
            min_confidence=float(contextual_cfg.get("min_confidence", 0.35) or 0.35),
        )
    sector = _sector_for_ticker(ticker)
    min_hold_days = int((control.get("min_hold_days_by_sector") or {}).get(sector, 4))
    min_rebalance_ratio = float(control.get("min_rebalance_ratio", 0.008))
    min_new_entry_ratio = float(control.get("min_new_entry_ratio", 0.004))
    min_support_confidence = float(control.get("min_signal_support_confidence", 0.35))
    confirmation_score = _safe_float(market_confirmation.get("confirmation_score"), 0.0)
    payloads = _analyst_signal_payloads(analyst_signals, fusion_context)
    fundamental_payload = payloads.get("fundamental")
    technical_payload = payloads.get("technical")
    news_payload = payloads.get("commodity_news")
    lifecycle_config = control.get("position_lifecycle") or {}
    horizon_config = control.get("horizon_consistency") or {}
    has_invalidation_boundary = _has_structured_invalidation_condition(analyst_signals)

    current_lots = int(getattr(current_position, "shares", 0) or 0) if current_position else 0
    held_days = (
        _days_held(getattr(current_position, "entry_date", None), trading_date)
        if current_position and current_lots != 0
        else None
    )
    position_pnl_ratio = _position_pnl_ratio(current_position)

    diagnostics = {
        "holding_rebalance_control": {
            "enabled": True,
            "sector": sector,
            "held_days": held_days,
            "min_hold_days": min_hold_days,
            "current_side": current_side,
            "raw_target_side": target_side,
            "current_ratio": float(current_ratio),
            "raw_target_ratio": float(position_ratio),
            "position_pnl_ratio": float(position_pnl_ratio),
            "confirmation_score": float(confirmation_score),
            "min_rebalance_ratio": float(min_rebalance_ratio),
            "min_new_entry_ratio": float(min_new_entry_ratio),
            "has_invalidation_boundary": bool(has_invalidation_boundary),
            "contextual_rule_calibration": contextual_diag,
        }
    }
    detail = diagnostics["holding_rebalance_control"]

    if risk_level in (RiskLevel.DANGER, RiskLevel.EMERGENCY):
        detail["decision"] = "skip_for_risk_state"
        return position_ratio, reasons, notes, diagnostics

    if current_side == "flat":
        if target_side in {"long", "short"} and bool(horizon_config.get("apply_to_new_entries", True)):
            new_entry_horizon = _horizon_consistency_result(
                side=target_side,
                target_lots_hint=1 if target_side == "long" else -1,
                analyst_signals=analyst_signals,
                technical_payload=technical_payload,
                news_payload=news_payload,
                market_confirmation=market_confirmation,
                control=horizon_config,
                has_invalidation=has_invalidation_boundary,
            )
            detail["horizon_consistency"] = new_entry_horizon
            if not new_entry_horizon.get("passed", True):
                reasons.append("horizon_consistency_requires_short_timing")
                notes.append(
                    f"{ticker} new {target_side} entry skipped: horizon={new_entry_horizon.get('decision_horizon')} "
                    f"requires short timing and invalidation; failures={new_entry_horizon.get('fail_reasons', [])}"
                )
                detail["decision"] = "skip_horizon_mismatch_new_entry"
                detail["final_target_ratio"] = 0.0
                return 0.0, reasons, notes, diagnostics
        if target_side in {"long", "short"} and abs(position_ratio) < min_new_entry_ratio:
            reasons.append("minimum_new_entry_threshold")
            notes.append(
                f"{ticker} new {target_side} entry skipped: target ratio "
                f"{position_ratio:.2%} below {min_new_entry_ratio:.2%}"
            )
            detail["decision"] = "skip_small_new_entry"
            detail["final_target_ratio"] = 0.0
            return 0.0, reasons, notes, diagnostics
        detail["decision"] = "no_existing_position"
        detail["final_target_ratio"] = float(position_ratio)
        return position_ratio, reasons, notes, diagnostics

    opposite_side = "short" if current_side == "long" else "long"
    reversal_side = target_side if target_side in {"long", "short"} else opposite_side
    target_strength = _side_signal_strength(reversal_side, long_scores, short_scores)
    current_strength = _side_signal_strength(current_side, long_scores, short_scores)
    fundamental_supports_current = _fundamental_anchor_supports(fundamental_payload, current_side, control)
    fundamental_supports_target = _fundamental_anchor_supports(fundamental_payload, reversal_side, control)
    technical_supports_current = _payload_supports_side(technical_payload, current_side, min_support_confidence)
    technical_supports_target = _payload_supports_side(technical_payload, reversal_side, min_support_confidence)
    news_supports_current = _news_high_quality_override(news_payload, current_side, control)
    news_override = _news_high_quality_override(news_payload, reversal_side, control)
    signal_counts_current = _side_signal_counts(payloads, current_side)
    current_horizon_consistency = _horizon_consistency_result(
        side=current_side,
        target_lots_hint=1 if current_side == "long" else -1,
        analyst_signals=analyst_signals,
        technical_payload=technical_payload,
        news_payload=news_payload,
        market_confirmation=market_confirmation,
        control=horizon_config,
        has_invalidation=has_invalidation_boundary,
    )

    lifecycle_enabled = bool(lifecycle_config.get("enabled", True))
    loss_revalidated = _lifecycle_revalidated_by_current_evidence(
        current_side=current_side,
        current_strength=current_strength,
        confirmation_score=confirmation_score,
        fundamental_supports_current=fundamental_supports_current,
        technical_supports_current=technical_supports_current,
        news_supports_current=news_supports_current,
        lifecycle_config=lifecycle_config,
    )
    trend_position = (
        lifecycle_enabled
        and position_pnl_ratio >= float(lifecycle_config.get("trend_min_profit_ratio", 0.03))
        and confirmation_score >= float(lifecycle_config.get("trend_min_confirmation_score", 0.60))
        and (
            fundamental_supports_current
            or technical_supports_current
            or news_supports_current
            or current_strength >= float(lifecycle_config.get("trend_min_signal_strength", 0.30))
        )
    )
    failed_position = (
        lifecycle_enabled
        and position_pnl_ratio <= float(lifecycle_config.get("failed_loss_ratio", -0.05))
        and not fundamental_supports_current
        and confirmation_score <= float(lifecycle_config.get("failed_max_confirmation_score", 0.45))
    )
    loss_revalidation_due = (
        lifecycle_enabled
        and held_days is not None
        and held_days >= int(lifecycle_config.get("loss_revalidation_min_hold_days", 1))
        and position_pnl_ratio <= float(lifecycle_config.get("loss_revalidation_ratio", -0.02))
        and not trend_position
    )
    loss_revalidation_failed = loss_revalidation_due and not loss_revalidated
    new_loss_revalidation_due = (
        lifecycle_enabled
        and bool(lifecycle_config.get("new_loss_revalidation_enabled", True))
        and current_side in {"long", "short"}
        and held_days is not None
        and held_days <= int(lifecycle_config.get("new_loss_revalidation_max_hold_days", 2))
        and position_pnl_ratio <= float(lifecycle_config.get("new_loss_revalidation_ratio", -0.005))
        and not trend_position
    )
    new_loss_failures: list[str] = []
    if new_loss_revalidation_due:
        min_new_loss_confirmation = float(
            lifecycle_config.get(
                "new_loss_revalidation_min_confirmation_score",
                lifecycle_config.get("loss_revalidation_min_confirmation_score", 0.55),
            )
        )
        min_new_loss_strength = float(
            lifecycle_config.get(
                "new_loss_revalidation_min_signal_strength",
                lifecycle_config.get("loss_revalidation_min_signal_strength", 0.25),
            )
        )
        if signal_counts_current.get("same", 0) <= 0 or not technical_supports_current:
            new_loss_failures.append("current_signal_neutral_or_absent")
        if signal_counts_current.get("opposite", 0) > 0:
            new_loss_failures.append("analyst_conflict")
        if current_strength < min_new_loss_strength or confirmation_score < min_new_loss_confirmation:
            new_loss_failures.append("insufficient_same_day_evidence")
        if not has_invalidation_boundary:
            new_loss_failures.append("missing_invalidation_boundary")
        if bool(horizon_config.get("apply_to_losing_holds", True)) and not current_horizon_consistency.get("passed", True):
            new_loss_failures.extend(
                f"horizon_{reason}" for reason in current_horizon_consistency.get("fail_reasons", [])
            )
    new_loss_revalidated = new_loss_revalidation_due and not new_loss_failures
    new_loss_revalidation_failed = new_loss_revalidation_due and bool(new_loss_failures)
    new_loss_revalidation_exit = (
        new_loss_revalidation_failed
        and (
            position_pnl_ratio <= float(lifecycle_config.get("new_loss_revalidation_exit_ratio", -0.02))
            or (
                "missing_invalidation_boundary" in new_loss_failures
                and "current_signal_neutral_or_absent" in new_loss_failures
            )
        )
    )
    loss_revalidation_exit = (
        loss_revalidation_failed
        and (
            position_pnl_ratio <= float(lifecycle_config.get("loss_revalidation_exit_ratio", -0.04))
            or confirmation_score <= float(lifecycle_config.get("loss_revalidation_exit_confirmation_score", 0.45))
        )
    )
    probe_expired = (
        lifecycle_enabled
        and held_days is not None
        and held_days >= int(lifecycle_config.get("probe_max_hold_days", 2))
        and position_pnl_ratio < float(lifecycle_config.get("probe_min_profit_ratio", 0.01))
        and confirmation_score < float(lifecycle_config.get("probe_min_confirmation_score", 0.55))
        and not trend_position
        and not fundamental_supports_current
    )
    strong_reversal = (
        target_side in {"long", "short"}
        and target_side != current_side
        and target_strength >= float(control.get("reverse_min_signal_strength", 0.55))
        and (
            confirmation_score >= float(control.get("reverse_min_confirmation_score", 0.65))
            or fundamental_supports_target
            or news_override
        )
        and not fundamental_supports_current
        and (fundamental_supports_target or (technical_supports_target and news_override))
    )
    horizon_losing_hold_failed = (
        lifecycle_enabled
        and bool(horizon_config.get("apply_to_losing_holds", True))
        and current_side in {"long", "short"}
        and position_pnl_ratio < 0
        and not current_horizon_consistency.get("passed", True)
        and not trend_position
    )
    horizon_losing_hold_exit = (
        horizon_losing_hold_failed
        and position_pnl_ratio <= float(horizon_config.get("losing_hold_exit_ratio", -0.02))
    )
    strong_exit = (
        target_strength >= float(control.get("exit_min_signal_strength", 0.35))
        and not fundamental_supports_current
        and (
            confirmation_score >= float(control.get("exit_min_confirmation_score", 0.45))
            or fundamental_supports_target
            or news_override
        )
    )
    detail.update({
        "reversal_side": reversal_side,
        "reversal_or_exit_strength": float(target_strength),
        "current_side_strength": float(current_strength),
        "fundamental_supports_current": bool(fundamental_supports_current),
        "fundamental_supports_target": bool(fundamental_supports_target),
        "technical_supports_current": bool(technical_supports_current),
        "technical_supports_target": bool(technical_supports_target),
        "news_supports_current": bool(news_supports_current),
        "news_high_quality_override": bool(news_override),
        "signal_counts_current": signal_counts_current,
        "horizon_consistency": current_horizon_consistency,
        "lifecycle_classification": (
            "failed_position" if failed_position else
            "new_loss_revalidation_failed" if new_loss_revalidation_failed else
            "horizon_consistency_failed" if horizon_losing_hold_failed else
            "loss_revalidation_failed" if loss_revalidation_failed else
            "trend_position" if trend_position else "probe_position" if probe_expired else "normal"
        ),
        "trend_position": bool(trend_position),
        "failed_position": bool(failed_position),
        "loss_revalidation_due": bool(loss_revalidation_due),
        "loss_revalidated": bool(loss_revalidated),
        "loss_revalidation_failed": bool(loss_revalidation_failed),
        "loss_revalidation_exit": bool(loss_revalidation_exit),
        "new_loss_revalidation_due": bool(new_loss_revalidation_due),
        "new_loss_revalidated": bool(new_loss_revalidated),
        "new_loss_revalidation_failed": bool(new_loss_revalidation_failed),
        "new_loss_revalidation_exit": bool(new_loss_revalidation_exit),
        "new_loss_revalidation_failures": new_loss_failures,
        "horizon_losing_hold_failed": bool(horizon_losing_hold_failed),
        "horizon_losing_hold_exit": bool(horizon_losing_hold_exit),
        "probe_expired": bool(probe_expired),
        "strong_reversal": bool(strong_reversal),
        "strong_exit": bool(strong_exit),
    })

    if failed_position:
        reasons.append("position_lifecycle_failed")
        notes.append(
            f"{ticker} {current_side} failed position exit: pnl_ratio={position_pnl_ratio:.2%}, "
            f"confirmation={confirmation_score:.2f}, held_days={held_days}"
        )
        detail["decision"] = "force_exit_failed_position"
        detail["final_target_ratio"] = 0.0
        return 0.0, reasons, notes, diagnostics

    if new_loss_revalidation_failed:
        if new_loss_revalidation_exit:
            reasons.append("new_position_loss_revalidation_failed")
            notes.append(
                f"{ticker} new {current_side} loss revalidation failed; exiting: "
                f"held_days={held_days}, pnl_ratio={position_pnl_ratio:.2%}, "
                f"failures={new_loss_failures}"
            )
            detail["decision"] = "exit_failed_new_loss_revalidation"
            detail["final_target_ratio"] = 0.0
            return 0.0, reasons, notes, diagnostics

        reduction_multiplier = max(
            0.0,
            min(1.0, float(lifecycle_config.get("new_loss_revalidation_reduction_multiplier", 0.50) or 0.50)),
        )
        reduced_ratio = _signed_abs(current_side, abs(current_ratio) * reduction_multiplier)
        if target_side == current_side:
            reduced_ratio = _signed_abs(current_side, min(abs(position_ratio), abs(reduced_ratio)))
        reasons.append("new_position_loss_revalidation_failed")
        notes.append(
            f"{ticker} new {current_side} loss revalidation failed; reducing exposure: "
            f"held_days={held_days}, pnl_ratio={position_pnl_ratio:.2%}, "
            f"failures={new_loss_failures}, ratio {current_ratio:.2%}->{reduced_ratio:.2%}"
        )
        detail["decision"] = "reduce_failed_new_loss_revalidation"
        detail["final_target_ratio"] = float(reduced_ratio)
        return reduced_ratio, reasons, notes, diagnostics

    if horizon_losing_hold_failed:
        if horizon_losing_hold_exit:
            reasons.append("horizon_consistency_failed_losing_hold")
            notes.append(
                f"{ticker} {current_side} losing hold failed horizon consistency; exiting: "
                f"horizon={current_horizon_consistency.get('decision_horizon')}, "
                f"failures={current_horizon_consistency.get('fail_reasons', [])}"
            )
            detail["decision"] = "exit_horizon_mismatch_losing_hold"
            detail["final_target_ratio"] = 0.0
            return 0.0, reasons, notes, diagnostics
        reduction_multiplier = max(
            0.0,
            min(1.0, float(horizon_config.get("losing_hold_reduction_multiplier", 0.50) or 0.50)),
        )
        reduced_ratio = _signed_abs(current_side, abs(current_ratio) * reduction_multiplier)
        if target_side == current_side:
            reduced_ratio = _signed_abs(current_side, min(abs(position_ratio), abs(reduced_ratio)))
        reasons.append("horizon_consistency_failed_losing_hold")
        notes.append(
            f"{ticker} {current_side} losing hold lacks short-horizon confirmation; "
            f"ratio {current_ratio:.2%}->{reduced_ratio:.2%}, "
            f"failures={current_horizon_consistency.get('fail_reasons', [])}"
        )
        detail["decision"] = "reduce_horizon_mismatch_losing_hold"
        detail["final_target_ratio"] = float(reduced_ratio)
        return reduced_ratio, reasons, notes, diagnostics

    if loss_revalidation_failed:
        if loss_revalidation_exit:
            reasons.append("position_lifecycle_loss_revalidation_failed")
            notes.append(
                f"{ticker} {current_side} loss revalidation failed; exiting: "
                f"held_days={held_days}, pnl_ratio={position_pnl_ratio:.2%}, "
                f"confirmation={confirmation_score:.2f}, current_strength={current_strength:.2f}"
            )
            detail["decision"] = "exit_failed_loss_revalidation"
            detail["final_target_ratio"] = 0.0
            return 0.0, reasons, notes, diagnostics

        reduction_multiplier = max(
            0.0,
            min(1.0, float(lifecycle_config.get("loss_revalidation_reduction_multiplier", 0.50) or 0.50)),
        )
        reduced_ratio = _signed_abs(current_side, abs(current_ratio) * reduction_multiplier)
        if target_side == current_side:
            reduced_ratio = _signed_abs(current_side, min(abs(position_ratio), abs(reduced_ratio)))
        reasons.append("position_lifecycle_loss_revalidation_failed")
        notes.append(
            f"{ticker} {current_side} loss revalidation failed; reducing exposure: "
            f"held_days={held_days}, pnl_ratio={position_pnl_ratio:.2%}, "
            f"confirmation={confirmation_score:.2f}, ratio {current_ratio:.2%}->{reduced_ratio:.2%}"
        )
        detail["decision"] = "reduce_failed_loss_revalidation"
        detail["final_target_ratio"] = float(reduced_ratio)
        return reduced_ratio, reasons, notes, diagnostics

    if probe_expired and not trend_position:
        reasons.append("position_lifecycle_probe_expired")
        notes.append(
            f"{ticker} {current_side} probe expired: held_days={held_days}, "
            f"pnl_ratio={position_pnl_ratio:.2%}, confirmation={confirmation_score:.2f}, "
            f"target_side={target_side}"
        )
        detail["decision"] = "exit_unvalidated_probe"
        detail["final_target_ratio"] = 0.0
        return 0.0, reasons, notes, diagnostics

    if target_side == current_side:
        delta = abs(position_ratio - current_ratio)
        if delta < min_rebalance_ratio:
            reasons.append("minimum_rebalance_threshold")
            notes.append(
                f"{ticker} same-side rebalance skipped: ratio change {delta:.2%} "
                f"below {min_rebalance_ratio:.2%}"
            )
            detail["decision"] = "keep_current_small_delta"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics

        reducing = abs(position_ratio) < abs(current_ratio)
        if trend_position and reducing and not strong_exit:
            reasons.append("position_lifecycle_trend_hold")
            notes.append(
                f"{ticker} {current_side} trend position held: pnl_ratio={position_pnl_ratio:.2%}, "
                f"confirmation={confirmation_score:.2f}"
            )
            detail["decision"] = "keep_trend_position"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics

        if reducing and held_days is not None and held_days < min_hold_days and not strong_exit:
            reasons.append("holding_period_control")
            notes.append(
                f"{ticker} {current_side} reduction deferred: held {held_days}d < "
                f"sector min {min_hold_days}d and no strong exit evidence"
            )
            detail["decision"] = "keep_current_min_hold"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics

        if reducing and fundamental_supports_current and not strong_exit:
            max_reduction = float(control.get("max_daily_reduction_ratio", 0.40))
            min_abs_after_reduction = abs(current_ratio) * max(0.0, 1.0 - max_reduction)
            capped_abs = max(abs(position_ratio), min_abs_after_reduction)
            capped_ratio = _signed_abs(current_side, capped_abs)
            if abs(capped_ratio - position_ratio) > 1e-12:
                reasons.append("fundamental_anchor_rebalance_cap")
                notes.append(
                    f"{ticker} {current_side} reduction capped by fundamental anchor: "
                    f"{position_ratio:.2%}->{capped_ratio:.2%}"
                )
                detail["decision"] = "cap_same_side_reduction"
                detail["final_target_ratio"] = float(capped_ratio)
                return capped_ratio, reasons, notes, diagnostics

        detail["decision"] = "allow_same_side_rebalance"
        detail["final_target_ratio"] = float(position_ratio)
        return position_ratio, reasons, notes, diagnostics

    if target_side == "flat":
        if trend_position and not strong_exit:
            reasons.append("position_lifecycle_trend_hold")
            notes.append(
                f"{ticker} {current_side} trend exit deferred: pnl_ratio={position_pnl_ratio:.2%}, "
                f"confirmation={confirmation_score:.2f}"
            )
            detail["decision"] = "keep_trend_position_exit_deferred"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics

        if (held_days is not None and held_days < min_hold_days and not strong_exit) or (
            fundamental_supports_current and not strong_exit
        ):
            reasons.append("holding_period_control")
            notes.append(
                f"{ticker} {current_side} exit deferred: held_days={held_days}, "
                f"sector_min={min_hold_days}, fundamental_anchor={fundamental_supports_current}"
            )
            detail["decision"] = "keep_current_exit_deferred"
            detail["final_target_ratio"] = float(current_ratio)
            return current_ratio, reasons, notes, diagnostics

        detail["decision"] = "allow_exit"
        detail["final_target_ratio"] = 0.0
        return 0.0, reasons, notes, diagnostics

    if target_side != current_side:
        if strong_reversal:
            detail["decision"] = "allow_reversal"
            detail["final_target_ratio"] = float(position_ratio)
            return position_ratio, reasons, notes, diagnostics

        if strong_exit and not fundamental_supports_current:
            reasons.append("reverse_requires_stronger_evidence")
            notes.append(
                f"{ticker} reversal downgraded to flat: target={target_side}, "
                f"strength={target_strength:.2f}, confirmation={confirmation_score:.2f}"
            )
            detail["decision"] = "downgrade_reversal_to_exit"
            detail["final_target_ratio"] = 0.0
            return 0.0, reasons, notes, diagnostics

        reasons.append("reverse_requires_stronger_evidence")
        notes.append(
            f"{ticker} reversal blocked: current={current_side}, target={target_side}, "
            f"held_days={held_days}, strength={target_strength:.2f}, "
            f"fundamental_anchor={fundamental_supports_current}"
        )
        detail["decision"] = "keep_current_reversal_blocked"
        detail["final_target_ratio"] = float(current_ratio)
        return current_ratio, reasons, notes, diagnostics

    detail["decision"] = "allow"
    detail["final_target_ratio"] = float(position_ratio)
    return position_ratio, reasons, notes, diagnostics


def portfolio_agent_futures(state: FundState):
    """Futures portfolio manager for phase1 recommendations."""
    agent_name = AgentKey.PORTFOLIO
    portfolio = state["portfolio"]
    ticker = state["ticker"]
    trading_date = state["trading_date"]
    analyst_signals = state["analyst_signals"]
    llm_config = state["llm_config"]
    num_tickers = state["num_tickers"]
    enabled_analysts = state.get("enabled_analysts", [])
    config_id = state.get("config_id", "")
    phase = state.get("phase")
    morning_price_context = state.get("morning_price_context")
    phase_value = getattr(phase, "value", phase)
    if phase_value != TradingPhase.PHASE1.value:
        raise RuntimeError(
            "portfolio_agent_futures only supports phase1 pre-open recommendation flow."
        )

    cfg = state.get("config", {})
    db = get_db()
    router = state.get("router")

    full_config = state.get("full_config", cfg)
    full_config = apply_config_learning_overlay(
        full_config,
        db=db,
        config_id=config_id,
        trading_date=trading_date,
    )
    state["full_config"] = full_config

    max_total_margin_ratio = get_hard_allocation_margin_ratio(full_config)
    risk_buffer_ratio = full_config.get("risk_buffer_ratio", cfg.get("risk_buffer_ratio", 0.10))
    risk_level, cashflow_ratio = check_risk_level(portfolio, full_config)

    # First apply the risk-level scaling, then decide LONG, SHORT, or NEUTRAL.
    position_scaling = get_position_scaling_factor(risk_level, full_config)
    max_single_margin_ratio = get_max_single_position_ratio(risk_level, full_config)

    # Scale down weak tickers before risk control so persistent laggards cannot dominate capital usage.
    ticker_perf = {}
    performance_control = full_config.get("ticker_performance_control", {}) or {}
    performance_control_enabled = performance_control.get("enabled", True)
    if db and config_id and performance_control_enabled:
        ticker_perf = db.get_ticker_performance(
            config_id=config_id,
            ticker=ticker,
            trading_date=trading_date,
            lookback_days=int(performance_control.get("lookback_days", 20)),
        )
    if performance_control_enabled and ticker_perf.get("trade_days", 0) >= int(performance_control.get("min_trade_days", 5)):
        avg_pnl = float(ticker_perf.get("avg_daily_pnl", 0.0) or 0.0)
        cumulative_pnl = float(ticker_perf.get("cumulative_pnl", 0.0) or 0.0)
        win_rate = float(ticker_perf.get("win_rate", 0.0) or 0.0)
        original_cap = max_single_margin_ratio
        severe_trigger = (
            avg_pnl <= float(performance_control.get("severe_avg_pnl_below", performance_control.get("hard_avg_pnl_below", -800)))
            or (
                cumulative_pnl < 0
                and win_rate <= float(performance_control.get("severe_win_rate_below", performance_control.get("hard_win_rate_below", 0.35)))
            )
        )
        if severe_trigger:
            severe_anchor = float(performance_control.get("severe_anchor_ratio", performance_control.get("hard_cap_ratio", 0.03)))
            max_single_margin_ratio = min(max_single_margin_ratio, severe_anchor)
            logger.info(
                f"Ticker performance severe anchor for {ticker}: avg_daily_pnl={avg_pnl:.0f}, "
                f"win_rate={win_rate:.0%}, cumulative_pnl={cumulative_pnl:.0f}, "
                f"base sizing anchor {original_cap:.2%} -> {max_single_margin_ratio:.2%}"
            )
        elif avg_pnl < float(performance_control.get("soft_avg_pnl_below", -300)):
            max_single_margin_ratio *= float(performance_control.get("soft_cap_multiplier", 0.50))
            logger.info(
                f"Ticker performance scaling for {ticker}: avg_daily_pnl={avg_pnl:.0f}, "
                f"base sizing anchor {original_cap:.2%} -> {max_single_margin_ratio:.2%}"
            )
        elif avg_pnl > float(performance_control.get("recovery_avg_pnl_above", 200)):
            max_single_margin_ratio = min(
                max_single_margin_ratio * float(performance_control.get("recovery_cap_multiplier", 1.10)),
                original_cap,
            )

    # Risk-state logging for the phase1 futures flow.
    if risk_level == RiskLevel.WARNING:
        logger.warning(
            f"WARNING futures risk state: account_equity={_portfolio_account_equity(portfolio):,.0f} "
            f"({cashflow_ratio*100:.1f}%), position_scaling={position_scaling*100:.0f}%, "
            f"base_sizing_anchor={max_single_margin_ratio*100:.0f}%"
        )
    elif risk_level == RiskLevel.DANGER:
        logger.error(
            f"DANGER futures risk state: account_equity={_portfolio_account_equity(portfolio):,.0f} "
            f"({cashflow_ratio*100:.1f}%), position_scaling={position_scaling*100:.0f}%"
        )
    elif risk_level == RiskLevel.EMERGENCY:
        logger.critical(
            f"EMERGENCY futures risk state: account_equity={_portfolio_account_equity(portfolio):,.0f} "
            f"({cashflow_ratio*100:.1f}%), de-risking only."
        )
        # Emergency mode only allows de-risking recommendations.
        logger.warning(
            "Emergency futures risk state detected in phase1. "
            f"ticker={ticker}, recommendation flow is restricted to de-risking actions only."
        )

    # Translate the signed target ratio into lots using current price and multiplier.
    account_equity = _portfolio_account_equity(portfolio)
    if account_equity <= 0:
        account_equity = float(portfolio.cashflow or 0.0)

    # Calculate current portfolio margin usage from all open positions.
    current_margin_used = sum(p.margin_used for p in portfolio.positions.values())
    current_margin_ratio = current_margin_used / account_equity if account_equity > 0 else 0

    max_allowed_margin = account_equity * max_total_margin_ratio
    remaining_margin = max_allowed_margin - current_margin_used
    max_single_margin = account_equity * max_single_margin_ratio

    logger.info(
        f"Current margin usage: {current_margin_used:,.0f} / {max_allowed_margin:,.0f} "
        f"= {current_margin_ratio:.2%}, account_equity={account_equity:,.0f}, "
        f"remaining_margin={remaining_margin:,.0f}"
    )

    # Enter reduce-only mode when margin usage breaches the configured cap.
    if current_margin_ratio >= max_total_margin_ratio:
        logger.warning(
            f"Margin ratio limit reached: {current_margin_ratio:.2%} >= "
            f"{max_total_margin_ratio:.2%}. Switching to reduce-only mode."
        )
        # Once the cap is breached, only position-reducing actions are allowed.
        force_reduce_only = True
    else:
        force_reduce_only = False

    underlying_code = extract_underlying_code(ticker)
    contract_info = FuturesContractInfoCache.get_contract_info(underlying_code)

    if not contract_info:
        logger.error(f"Missing contract info for {ticker}")
        return {
            "decision": FuturesDecision(
                ticker=ticker,
                action=FuturesAction.HOLD,
                lots=0,
                price=0,
                justification=f"Missing contract info for {ticker}; returning HOLD.",
            )
        }

    multiplier = contract_info['contract_multiplier']

    if morning_price_context is None or morning_price_context.base_price is None:
        hold_decision = FuturesDecision(
            ticker=ticker,
            action=FuturesAction.HOLD,
            lots=0,
            price=0,
            justification=f"{ticker} missing phase1 execution basis"
        )
        skipped_recommendation = FuturesRecommendation(
            config_id=config_id,
            reference_portfolio_id=portfolio.id,
            trading_date=trading_date.strftime("%Y-%m-%d") if hasattr(trading_date, "strftime") else str(trading_date),
            effective_trade_date=trading_date.strftime("%Y-%m-%d") if hasattr(trading_date, "strftime") else str(trading_date),
            source_type=RecommendationSourceType.STRATEGY,
            underlying_code=ticker,
            action=RecommendationAction.HOLD,
            lots=0,
            slippage_model="tick",
            slippage_ticks=0,
            slippage_amount=0,
            warning_message=morning_price_context.warning_message if morning_price_context else f"{ticker} missing phase1 execution basis",
            status=RecommendationStatus.SKIPPED,
        )
        return {"decision": hold_decision, "recommendation": skipped_recommendation}

    # Legacy pre-settlement rollover execution was removed.
    # Rollover detection now happens in phase2, and execution happens in next-day phase1.

    try:
        current_price = morning_price_context.base_price
        settle_price = current_price
        contract_code = None  # execution and settlement both need the actual contract code

        # Phase1 stores a pre-open plan rather than a live trade decision.
        existing_position = portfolio.positions.get(ticker)
        contract_code = getattr(existing_position, "contract_code", None) if existing_position is not None else None
        logger.info(
            f"Phase1 basis for {underlying_code}: {current_price} "
            f"({getattr(morning_price_context.base_price_source, 'value', morning_price_context.base_price_source)})"
        )

        price_ranges = {
            "M": (2000, 5000),
            "RB": (2500, 6000),
            "TA": (3500, 7000),
            "Y": (5000, 10000),
            "P": (5000, 12000),
            "A": (3000, 6000),
            "C": (2000, 3500),
            "CF": (12000, 18000),
            "SR": (5000, 8000),
            "MA": (2000, 4000),
            "I": (500, 1200),
            "J": (1500, 3500),
            "JM": (1000, 2500),
            "CU": (50000, 90000),
            "AL": (18000, 25000),
            "ZN": (20000, 35000),
            "NI": (100000, 180000),
            "SC": (400, 800),
            "RU": (12000, 20000),
            "FG": (1200, 2500),
            "SA": (1500, 3500),
        }

        if underlying_code in price_ranges:
            min_price, max_price = price_ranges[underlying_code]
            if current_price < min_price or current_price > max_price:
                logger.error(
                    f"Abnormal futures price for {underlying_code} ({ticker}): {current_price:.2f}, "
                    f"expected range [{min_price}, {max_price}]. Returning HOLD."
                )
                return {
                    "decision": FuturesDecision(
                        ticker=ticker,
                        action=FuturesAction.HOLD,
                        lots=0,
                        price=0,
                        justification=f"{underlying_code} price {current_price:.2f} is outside the expected range [{min_price}, {max_price}]; returning HOLD.",
                    )
                }
            else:
                logger.info(
                    f"Validated futures price for {underlying_code} ({ticker}): {current_price:.2f} "
                    f"within range [{min_price}, {max_price}]"
                )
        else:
            logger.info(
                f"No configured price sanity range for {underlying_code}; using current price {current_price:.2f}"
            )

        if ticker in portfolio.positions:
            position = portfolio.positions[ticker]
            if position.shares != 0:
                reference_price = position.settle_price or position.entry_price
                if position.shares > 0:  # Long position.
                    position.unrealized_pnl = (current_price - reference_price) * position.shares * multiplier
                else:  # Short position.
                    position.unrealized_pnl = (reference_price - current_price) * abs(position.shares) * multiplier

        if ticker in portfolio.positions:
            position = portfolio.positions[ticker]
            if position.shares != 0 and position.margin_used > 0:
                single_loss_ratio = position.unrealized_pnl / position.margin_used

                if single_loss_ratio <= -0.10:
                    logger.warning(
                        f"Single-position loss threshold reached for {ticker}: "
                        f"loss_ratio={single_loss_ratio*100:.1f}%, "
                        f"unrealized_pnl={position.unrealized_pnl:+,.2f}, margin_used={position.margin_used:,.2f}"
                    )

                    if position.shares > 0:
                        logger.warning(
                            f"Forced de-risking for long position in {ticker}: lots={abs(position.shares)}"
                        )
                        return {
                            "decision": FuturesDecision(
                                ticker=ticker,
                                action=FuturesAction.CLOSE_LONG,
                                lots=abs(position.shares),
                                price=current_price,
                                settle_price=settle_price,
                                margin_rate=contract_info['margin_rate_long'],
                                contract_multiplier=multiplier,
                                justification=f"Single-position loss threshold reached ({single_loss_ratio*100:.1f}% <= -10.0%); closing long position.",
                            )
                        }
                    else:
                        logger.warning(
                            f"Forced de-risking for short position in {ticker}: lots={abs(position.shares)}"
                        )
                        return {
                            "decision": FuturesDecision(
                                ticker=ticker,
                                action=FuturesAction.CLOSE_SHORT,
                                lots=abs(position.shares),
                                price=current_price,
                                settle_price=settle_price,
                                margin_rate=contract_info['margin_rate_short'],
                                contract_multiplier=multiplier,
                                justification=f"Single-position loss threshold reached ({single_loss_ratio*100:.1f}% <= -10.0%); closing short position.",
                            )
                        }

    except Exception as e:
        logger.error(f"Error while evaluating futures risk for {ticker}: {e}")
        return {
            "decision": FuturesDecision(
                ticker=ticker,
                action=FuturesAction.HOLD,
                lots=0,
                price=0,
                justification=f"Error while evaluating futures risk for {ticker}: {e}"
            )
        }

    analyst_count = len(enabled_analysts)
    max_position_ratio = 1
    if num_tickers > 1:
        base_max_ratio = round(2 / num_tickers * 20) / 20

        margin_max_ratio = max_single_margin_ratio

        # Apply the tighter of the diversification cap and the margin cap.
        max_position_ratio = min(base_max_ratio, margin_max_ratio)

    if analyst_count == 1:
        decision_logic = SINGLE_ANALYST_LOGIC.format(max_position_ratio=max_position_ratio)
    else:
        decision_logic = MULTI_ANALYST_LOGIC.format(max_position_ratio=max_position_ratio)

    formatted_signals_lines = []

    # Group analyst outputs by agent name for downstream weighting.
    signals_by_agent = {}
    for signal in analyst_signals:
        if hasattr(signal, 'agent_name') and signal.agent_name:
            agent_name = "commodity_news" if signal.agent_name == "company_news" else signal.agent_name
            signals_by_agent[agent_name] = signal

    weights = {}
    fusion_context = {}
    if analyst_count > 1:
        # Extract the basis percentage from the fundamental analyst output.
        fundamental_signal = signals_by_agent.get('fundamental')
        basis_pct = 0.0
        fundamental_quality = {}
        if fundamental_signal:
            basis_pct = DynamicWeightCalculator.extract_basis_from_signal(fundamental_signal)
            fundamental_quality = DynamicWeightCalculator.extract_quality_from_signal(fundamental_signal)

        # Build dynamic weights from the current basis and structured quality context.
        calculator = DynamicWeightCalculator(full_config)
        weights = calculator.calculate(
            basis_pct,
            fundamental_quality=fundamental_quality,
        )
        weights = calibrate_weights_by_signal_history(
            db=db,
            config_id=config_id,
            ticker=ticker,
            trading_date=trading_date,
            current_weights=weights,
        )
        fusion_context = _quality_aware_fusion_context(
            ticker=ticker,
            analyst_signals=analyst_signals,
            dynamic_weights=weights,
            full_config=full_config,
        )
        weights = fusion_context["quality_adjusted_weights"]

        logger.info(
            f"Adaptive quality-aware weights for {ticker}: "
            f"sector={fusion_context['sector']}, "
            f"fundamental={weights['fundamental']:.2%}, "
            f"technical={weights['technical']:.2%}, "
            f"news={weights['commodity_news']:.2%}"
        )

        decision_logic = _build_dynamic_weight_prompt(
            weights=weights,
            max_position_ratio=max_position_ratio,
            basis_pct=basis_pct,
            market_state=None,
            fundamental_trends=None,
            fusion_context=fusion_context,
        )
    for signal in analyst_signals:
        if hasattr(signal, 'agent_name') and signal.agent_name:
            agent_name = "commodity_news" if signal.agent_name == "company_news" else signal.agent_name
            signals_by_agent[agent_name] = signal
        else:
            logger.warning("Signal missing agent_name; using fallback index matching.")

    for analyst_name in enabled_analysts:
        signal = signals_by_agent.get(analyst_name)

        if not signal:
            logger.error(f"No signal found for analyst: {analyst_name}")
            continue

        analyst_display = {
            AgentKey.TECHNICAL: "Technical Analyst",
            AgentKey.FUNDAMENTAL: "Fundamental Analyst",
            AgentKey.COMMODITY_NEWS: "Commodity News Analyst"
        }.get(analyst_name, analyst_name)

        signal_text = _signal_to_text(signal.signal)
        signal_value = {"BULLISH": "+1", "BEARISH": "-1", "NEUTRAL": "0"}.get(signal_text.upper(), "0")

        justification = _sanitize_visible_text(signal.justification)
        if len(justification) > 200:
            justification = justification[:200] + "..."

        quality = (fusion_context.get("analyst_quality") or {}).get(analyst_name, {})
        quality_suffix = (
            f"Tradeability={quality.get('tradeability', 'unknown')}, "
            f"EffectiveConfidence={quality.get('effective_confidence', signal.confidence):.2f}, "
            f"BusinessQuality={getattr(signal, 'business_quality_score', 0.0):.2f}, "
            f"Template={getattr(signal, 'template_name', 'unknown')}, "
            f"Horizon={getattr(signal, 'horizon_class', 'unknown')}, "
            f"RiskFlags={quality.get('risk_flags', [])}"
        )

        formatted_line = (
            f"{analyst_display}: Signal={signal.signal}({signal_value}), "
            f"Confidence={signal.confidence:.2f}, "
            f"{quality_suffix}, "
            f"Justification: {justification}"
        )
        formatted_signals_lines.append(formatted_line)

    formatted_signals = "\n".join(formatted_signals_lines)

    logger.info(f"=== {ticker} Analyst Signals for Risk Control ===\n{formatted_signals}\n{'='*60}")

    decision_memory_lines = []
    if db and config_id:
        decision_memory_lines = db.get_futures_transaction_memory(
            config_id=config_id,
            ticker=ticker,
            limit=thresholds["decision_memory_limit"],
        )
    decision_memory = "\n".join(decision_memory_lines) if decision_memory_lines else "No recent transaction memory."
    pm_learning_context = build_learning_context(
        db=db,
        full_config=full_config,
        config_id=config_id,
        trading_date=trading_date,
        analyst="portfolio_manager",
        ticker=ticker,
        context=fusion_context or {},
        horizon_class="*",
    )
    if pm_learning_context.get("text"):
        decision_memory = (
            decision_memory
            + "\n\n"
            + pm_learning_context["text"]
            + "\n[PM note] Exploratory memories are priors only; do not exceed hard margin/risk controls."
        )
    pm_learning_audit = {
        "enabled": bool(pm_learning_context.get("enabled")),
        "selected_digest_ids": pm_learning_context.get("selected_ids", []),
        "trade_episode_count": len(pm_learning_context.get("trade_episode_items") or []),
        "hypothesis_count": len(pm_learning_context.get("hypothesis_items") or []),
        "candidate_hypothesis_count": int(pm_learning_context.get("candidate_hypothesis_count", 0) or 0),
        "validated_hypothesis_count": int(pm_learning_context.get("validated_hypothesis_count", 0) or 0),
        "hypothesis_status_counts": pm_learning_context.get("hypothesis_status_counts", {}),
        "sector": pm_learning_context.get("sector"),
        "market_regime": pm_learning_context.get("market_regime"),
        "prompt_prior_only": True,
        "candidate_hypothesis_authority": (
            "prior_only_no_sizing_add_position_matched_losing_hold_or_bypass_without_current_evidence"
        ),
        "hard_margin_cap_not_overridden": True,
    }

    risk_prompt = RISK_CONTROL_PROMPT.format(
        enabled_analysts=enabled_analysts,
        analyst_count=analyst_count,
        ticker_signals=formatted_signals,
        decision_memory=decision_memory,
        portfolio=portfolio.model_dump_json(),
        max_position_ratio=max_position_ratio,
        total_portfolio_value=account_equity,
        margin_available=remaining_margin,
        margin_used=current_margin_used,
        current_margin_ratio=current_margin_ratio,
        max_total_margin_ratio=max_total_margin_ratio,
        max_single_margin_ratio=max_single_margin_ratio,
        max_allowed_margin=max_allowed_margin,
        remaining_margin=remaining_margin,
        max_single_margin=max_single_margin,
    ) + decision_logic

    portfolio_llm_config = dict(llm_config)
    pm_llm_config = _get_portfolio_manager_config(full_config)
    if pm_llm_config.get("llm_mode", "cloud_only") == "cloud_only" and pm_llm_config.get("cloud_model"):
        portfolio_llm_config["model"] = pm_llm_config["cloud_model"]

    position_risk = agent_call(
        prompt=risk_prompt,
        llm_config=portfolio_llm_config,
        pydantic_model=PositionRisk,
    )
    position_risk.justification = _sanitize_visible_text(position_risk.justification)

    logger.log_agent_status(agent_name, ticker, "Risk control")
    logger.log_risk(ticker, position_risk)

    # Prefer structured basis metadata; text parsing is only a compatibility fallback.
    fundamental_basis = 0.0
    fundamental_quality = {}
    for signal in analyst_signals:
        if hasattr(signal, 'agent_name') and signal.agent_name == 'fundamental':
            fundamental_basis = DynamicWeightCalculator.extract_basis_from_signal(signal)
            fundamental_quality = DynamicWeightCalculator.extract_quality_from_signal(signal)
            if fundamental_basis:
                logger.info(f"Using structured basis percentage for {ticker}: {fundamental_basis:.2f}%")
            break
    # Reuse the same quality-aware adaptive weights shown to the portfolio LLM.
    dynamic_weights = weights if weights else None
    if not fusion_context:
        calculator = DynamicWeightCalculator(full_config)
        if calculator.enabled:
            dynamic_weights = calculator.calculate(
                fundamental_basis,
                fundamental_quality=fundamental_quality,
            )
        fusion_context = _quality_aware_fusion_context(
            ticker=ticker,
            analyst_signals=analyst_signals,
            dynamic_weights=dynamic_weights,
            full_config=full_config,
        )
        dynamic_weights = fusion_context["quality_adjusted_weights"]

    long_scores, short_scores = calculate_long_short_signals(
        ticker=ticker,
        analyst_signals=analyst_signals,
        fundamental_basis=fundamental_basis,
        weights=dynamic_weights,
        fusion_context=fusion_context,
        full_config=full_config,
    )

    logger.info(
        f"Signal blend for {ticker}: "
        f"LONG={long_scores['score']:.2f}(conf={long_scores['confidence']:.2f}), "
        f"SHORT={short_scores['score']:.2f}(conf={short_scores['confidence']:.2f}), "
        f"basis={fundamental_basis:+.2f}%, sector={fusion_context.get('sector')}"
    )

    # Allow a strong explicit basis signal to override a conflicting LLM direction.
    #

    bullish_strength = long_scores['score'] * long_scores['confidence']
    bearish_strength = short_scores['score'] * short_scores['confidence']

    if long_scores['has_strong_basis'] and position_risk.optimal_position_ratio < 0:
        logger.info(
            f"Strong long basis signal detected for {ticker}: fundamental_basis={fundamental_basis:+.2f}%"
        )
        logger.info(
            f"   weights -> fundamental={weights.get('fundamental', 0):.2%}, technical={weights.get('technical', 0):.2%}, news={weights.get('commodity_news', 0):.2%}"
        )
        logger.info(
            f"   blended strength -> bullish={bullish_strength:.3f}, bearish={bearish_strength:.3f}"
        )
        logger.info("   overriding bearish LLM output because the explicit basis signal is strongly long")

    elif short_scores['has_strong_basis'] and position_risk.optimal_position_ratio > 0:
        logger.info(
            f"Strong short basis signal detected for {ticker}: fundamental_basis={fundamental_basis:+.2f}%"
        )
        logger.info(
            f"   weights -> fundamental={weights.get('fundamental', 0):.2%}, technical={weights.get('technical', 0):.2%}, news={weights.get('commodity_news', 0):.2%}"
        )
        logger.info(
            f"   blended strength -> bullish={bullish_strength:.3f}, bearish={bearish_strength:.3f}"
        )
        logger.info("   overriding bullish LLM output because the explicit basis signal is strongly short")

    directional_reasons, directional_notes, directional_diagnostics = _apply_directional_override(
        ticker=ticker,
        position_risk=position_risk,
        long_scores=long_scores,
        short_scores=short_scores,
        max_position_ratio=max_position_ratio,
        risk_level=risk_level,
        full_config=full_config,
    )
    if directional_notes:
        logger.info(f"{ticker}: Directional override applied: {directional_notes}")

    if position_scaling < 1.0:
        original_ratio = position_risk.optimal_position_ratio
        position_risk.optimal_position_ratio *= position_scaling
        position_risk.justification += f"\n[Risk adjustment: {risk_level.value}; position ratio scaled from {original_ratio:.2%} to {position_risk.optimal_position_ratio:.2%}]"
        logger.info(f"Position ratio scaled under {risk_level.value}: {original_ratio:.2%} * {position_scaling:.2f} = {position_risk.optimal_position_ratio:.2%}")

    # Clamp the signed target ratio to the allowed absolute cap.
    if position_risk.optimal_position_ratio > max_position_ratio:
        position_risk.optimal_position_ratio = max_position_ratio
    elif position_risk.optimal_position_ratio < -max_position_ratio:
        position_risk.optimal_position_ratio = -max_position_ratio

    current_position = portfolio.positions.get(ticker) if ticker in portfolio.positions else None
    current_net_exposure = _current_net_exposure(portfolio, account_equity)
    current_ticker_exposure = _signed_position_ratio(current_position, account_equity)
    current_lots_for_control = int(getattr(current_position, "shares", 0) or 0)

    bq_ratio, bq_reasons, bq_notes, bq_diagnostics = _apply_business_quality_position_gate(
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        analyst_signals=analyst_signals,
        full_config=full_config,
    )
    if bq_reasons or bq_notes:
        before_ratio = position_risk.optimal_position_ratio
        position_risk.optimal_position_ratio = bq_ratio
        position_risk.justification += (
            f"\n[Business quality gate: {before_ratio:.2%}->{bq_ratio:.2%}; "
            f"reasons={bq_reasons}]"
        )
        logger.info(f"{ticker}: Business quality gate applied: {bq_notes or bq_reasons}")

    signal_strength = max(
        float(long_scores.get("score", 0.0) or 0.0) * float(long_scores.get("confidence", 0.0) or 0.0),
        float(short_scores.get("score", 0.0) or 0.0) * float(short_scores.get("confidence", 0.0) or 0.0),
    )
    target_side_for_confirmation = _target_side_from_ratio(position_risk.optimal_position_ratio)
    market_confirmation = {}
    if (full_config.get("pandaai_extra_data", {}) or {}).get("enabled", False):
        try:
            market_confirmation = MarketConfirmationEngine(full_config, router=router).evaluate(
                underlying_code=ticker,
                trading_date=trading_date,
                target_direction=target_side_for_confirmation,
                signal_strength=signal_strength,
                contract_code=contract_code,
            )
        except Exception as exc:
            logger.warning(f"{ticker}: PandaAI market confirmation skipped: {exc}")
            market_confirmation = {
                "enabled": True,
                "target_direction": target_side_for_confirmation,
                "confirmation_score": 0.0,
                "features": [],
                "confirmations": [],
                "conflicts": [],
                "data_missing": [str(exc)],
            }

    control_reasons: list[str] = []
    control_notes: list[str] = []
    control_diagnostics: dict = {}
    control_reasons.extend(directional_reasons)
    control_notes.extend(directional_notes)
    control_diagnostics.update(directional_diagnostics)
    control_reasons.extend(bq_reasons)
    control_notes.extend(bq_notes)
    control_diagnostics.update(bq_diagnostics)
    control_block_reason = None
    pre_control_ratio = position_risk.optimal_position_ratio
    signal_combo = _analyst_signal_combo(analyst_signals)
    auditor_output = None
    strategy_memory = {}
    adaptive_policy_state = []
    provisional_policy_state = []
    trade_auditor = TradeAuditor(full_config)

    if trade_auditor.enabled:
        auditor_config = full_config.get("trade_auditor") or full_config.get("decision_planner", {}) or {}
        feedback_config = auditor_config.get("attribution_feedback", {}) or {}
        legacy_trade_config = full_config.get("trade_frequency_control", {}) or {}
        auditor_lookback = int(
            feedback_config.get(
                "lookback_trades",
                legacy_trade_config.get("lookback_trades", 30),
            )
        )
        auditor_side = _target_side_from_ratio(position_risk.optimal_position_ratio)
        recent_side_performance = {}
        recent_conditional_performance = {}
        if db and config_id and auditor_side in {"long", "short"}:
            decision_horizon = _resolve_decision_horizon(
                analyst_signals,
                1 if auditor_side == "long" else -1,
            )
            market_regime_key = _market_regime_from_signals(analyst_signals, auditor_side)
            signal_template_key = _signal_template_from_signals(
                auditor_side,
                analyst_signals,
                signal_combo,
            )
            recent_side_performance = db.get_futures_trade_pair_performance(
                config_id=config_id,
                ticker=ticker,
                side=auditor_side,
                trading_date=trading_date,
                lookback_trades=auditor_lookback,
            )
            if hasattr(db, "get_futures_conditional_trade_performance"):
                recent_conditional_performance = db.get_futures_conditional_trade_performance(
                    config_id=config_id,
                    ticker=ticker,
                    side=auditor_side,
                    trading_date=trading_date,
                    signal_combo=list(signal_combo),
                    lookback_trades=auditor_lookback,
                    include_rollover=False,
                )
            if (full_config.get("strategy_memory", {}) or {}).get("enabled", False) and hasattr(db, "get_strategy_memory"):
                strategy_memory = db.get_strategy_memory(
                    config_id=config_id,
                    ticker=ticker,
                    side=auditor_side,
                    trading_date=trading_date,
                    signal_combo=list(signal_combo),
                )
            if hasattr(db, "get_adaptive_policy_state"):
                adaptive_policy_state = db.get_adaptive_policy_state(
                    config_id=config_id,
                    ticker=ticker,
                    side=auditor_side,
                    signal_template=signal_template_key,
                    horizon_class=decision_horizon,
                    market_regime=market_regime_key,
                    trading_date=trading_date,
                )
            if hasattr(db, "get_provisional_policy_state"):
                provisional_policy_state = db.get_provisional_policy_state(
                    config_id=config_id,
                    ticker=ticker,
                    side=auditor_side,
                    signal_template=signal_template_key,
                    horizon_class=decision_horizon,
                    trading_date=trading_date,
                )

        analyst_payload = [
            signal.model_dump() if hasattr(signal, "model_dump") else dict(signal)
            for signal in analyst_signals
        ]
        auditor_input = TradeAuditorInput(
            ticker=ticker,
            trading_date=trading_date,
            config_id=config_id,
            analyst_signals=analyst_payload,
            signal_combo=list(signal_combo),
            raw_long_score=long_scores,
            raw_short_score=short_scores,
            raw_target_side=auditor_side,
            raw_position_ratio=position_risk.optimal_position_ratio,
            current_position_ratio=current_ticker_exposure,
            signal_strength=signal_strength,
            market_confirmation=market_confirmation,
            fundamental_quality=fundamental_quality,
            recent_ticker_side_performance=recent_side_performance,
            recent_conditional_performance=recent_conditional_performance,
            strategy_memory=strategy_memory,
            adaptive_policy_state=adaptive_policy_state,
            provisional_policy_state=provisional_policy_state,
            risk_level=risk_level.value,
            full_config=full_config,
        )
        auditor_output = trade_auditor.plan(auditor_input)
        before_ratio = position_risk.optimal_position_ratio
        if auditor_output.decision == "block":
            if _same_sign(before_ratio, current_ticker_exposure):
                position_risk.optimal_position_ratio = current_ticker_exposure
            else:
                position_risk.optimal_position_ratio = 0.0
            control_reasons.extend(auditor_output.reasons)
            control_reasons.append("trade_auditor_block")
            control_notes.extend(auditor_output.notes)
            control_notes.append(
                f"trade auditor blocked new {auditor_output.target_side} exposure: "
                f"{before_ratio:.2%}->{position_risk.optimal_position_ratio:.2%}"
            )
        elif auditor_output.decision == "reduce_only":
            if abs(current_ticker_exposure) > 1e-12 and _same_sign(before_ratio, current_ticker_exposure):
                position_risk.optimal_position_ratio = min(
                    abs(before_ratio),
                    abs(current_ticker_exposure),
                ) * (1.0 if current_ticker_exposure > 0 else -1.0)
            else:
                position_risk.optimal_position_ratio = 0.0
            control_reasons.extend(auditor_output.reasons)
            control_reasons.append("trade_auditor_reduce_only")
            control_notes.extend(auditor_output.notes)
            control_notes.append(
                f"trade auditor reduce-only {auditor_output.target_side}: "
                f"{before_ratio:.2%}->{position_risk.optimal_position_ratio:.2%}"
            )
        elif auditor_output.decision in {"reduce", "scale_down", "probe_only"}:
            position_risk.optimal_position_ratio = _apply_trade_plan_multiplier(
                target_ratio=position_risk.optimal_position_ratio,
                current_ratio=current_ticker_exposure,
                multiplier=auditor_output.position_ratio_multiplier,
            )
            control_reasons.extend(auditor_output.reasons)
            if abs(position_risk.optimal_position_ratio) <= 1e-12 and abs(before_ratio) > 1e-12:
                control_reasons.append("trade_auditor_scale_to_zero")
            control_notes.extend(auditor_output.notes)
            control_notes.append(
                f"trade auditor {auditor_output.decision} {auditor_output.target_side} ratio "
                f"{before_ratio:.2%}->{position_risk.optimal_position_ratio:.2%}"
            )
        else:
            control_reasons.extend(auditor_output.reasons)
            control_notes.extend(auditor_output.notes)
        auditor_payload = (
            auditor_output.model_dump() if hasattr(auditor_output, "model_dump") else dict(auditor_output)
        )
        control_diagnostics["trade_auditor"] = auditor_payload
        control_diagnostics["decision_planner"] = auditor_payload
    else:
        position_risk.optimal_position_ratio, reasons, notes = _apply_market_confirmation_control(
            position_ratio=position_risk.optimal_position_ratio,
            current_ratio=current_ticker_exposure,
            signal_strength=signal_strength,
            market_confirmation=market_confirmation,
            full_config=full_config,
        )
        control_reasons.extend(reasons)
        control_notes.extend(notes)

        position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_trade_frequency_control(
            db=db,
            config_id=config_id,
            ticker=ticker,
            trading_date=trading_date,
            position_ratio=position_risk.optimal_position_ratio,
            current_ratio=current_ticker_exposure,
            signal_combo=signal_combo,
            market_confirmation=market_confirmation,
            full_config=full_config,
        )
        control_reasons.extend(reasons)
        control_notes.extend(notes)
        control_diagnostics.update(diagnostics)

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_market_data_gap_control(
        ticker=ticker,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        signal_strength=signal_strength,
        market_confirmation=market_confirmation,
        full_config=full_config,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    prospective_margin_rate = float(
        contract_info['margin_rate_long']
        if position_risk.optimal_position_ratio >= 0
        else contract_info['margin_rate_short']
    )
    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_drawdown_and_ticker_loss_control(
        db=db,
        config_id=config_id,
        ticker=ticker,
        trading_date=trading_date,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        current_margin_ratio=current_margin_ratio,
        margin_rate=prospective_margin_rate,
        market_confirmation=market_confirmation,
        signal_combo=signal_combo,
        strategy_memory=strategy_memory,
        analyst_signals=analyst_signals,
        full_config=full_config,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
        db=db,
        config_id=config_id,
        ticker=ticker,
        trading_date=trading_date,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        current_margin_ratio=current_margin_ratio,
        margin_rate=prospective_margin_rate,
        max_position_ratio=max_position_ratio,
        market_confirmation=market_confirmation,
        full_config=full_config,
        signal_combo=signal_combo,
        strategy_memory=strategy_memory,
        adaptive_policy_state=adaptive_policy_state,
        analyst_signals=analyst_signals,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_pretrade_invalidation_control(
        ticker=ticker,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        max_position_ratio=max_position_ratio,
        analyst_signals=analyst_signals,
        full_config=full_config,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
        ticker=ticker,
        trading_date=trading_date,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
        current_position=current_position,
        analyst_signals=analyst_signals,
        long_scores=long_scores,
        short_scores=short_scores,
        market_confirmation=market_confirmation,
        full_config=full_config,
        fusion_context=fusion_context,
        risk_level=risk_level,
        adaptive_policy_state=adaptive_policy_state,
    )
    control_reasons.extend(reasons)
    control_notes.extend(notes)
    control_diagnostics.update(diagnostics)

    if control_notes:
        position_risk.justification += "\n" + "\n".join(f"[Strategy control: {note}]" for note in control_notes)
        logger.info(f"{ticker}: Strategy controls applied: {control_notes}")

    if (
        abs(pre_control_ratio) > 1e-12
        and abs(position_risk.optimal_position_ratio) <= 1e-12
        and current_lots_for_control == 0
        and control_reasons
    ):
        control_block_reason = control_reasons[-1]

    target_exposure = position_risk.optimal_position_ratio
    new_net_exposure = current_net_exposure - current_ticker_exposure + target_exposure

    max_net_exposure, symmetric_scaling, net_exposure_cap_mode = _resolve_net_exposure_control(
        full_config,
        control_diagnostics,
    )
    control_diagnostics["net_exposure_control"] = {
        "max_net_exposure": float(max_net_exposure),
        "cap_mode": net_exposure_cap_mode,
        "projected_net_exposure_before_cap": float(new_net_exposure),
    }

    # Apply a symmetric net-exposure cap before translating the target into lots.
    if new_net_exposure > max_net_exposure:
        net_exposure_without_ticker = current_net_exposure - current_ticker_exposure
        scale_factor = (max_net_exposure - net_exposure_without_ticker) / target_exposure if target_exposure > 0 else 0
        original_ratio = position_risk.optimal_position_ratio
        position_risk.optimal_position_ratio *= max(0, scale_factor)
        position_risk.justification += (
            f"\n[Net exposure cap: projected net exposure {new_net_exposure:.1%} exceeds +{max_net_exposure:.1%}; "
            f"position ratio scaled to {position_risk.optimal_position_ratio:.2%}]"
        )
        logger.warning(
            f"Net exposure cap hit on long side: {new_net_exposure:.1%} > {max_net_exposure:.1%}, "
            f"position ratio scaled from {original_ratio:.2%} to {position_risk.optimal_position_ratio:.2%}"
        )
    elif new_net_exposure < -max_net_exposure:
        net_exposure_without_ticker = current_net_exposure - current_ticker_exposure
        scale_factor = (-max_net_exposure - net_exposure_without_ticker) / target_exposure if target_exposure < 0 else 0
        original_ratio = position_risk.optimal_position_ratio

        if symmetric_scaling:
            position_risk.optimal_position_ratio = -abs(original_ratio) * max(0, scale_factor)
        else:
            position_risk.optimal_position_ratio *= min(0, scale_factor)

        position_risk.justification += (
            f"\n[Net exposure cap: projected net exposure {new_net_exposure:.1%} exceeds -{max_net_exposure:.1%}; "
            f"position ratio scaled to {position_risk.optimal_position_ratio:.2%}]"
        )
        logger.warning(
            f"Net exposure cap hit on short side: {new_net_exposure:.1%} < -{max_net_exposure:.1%}, "
            f"position ratio scaled from {original_ratio:.2%} to {position_risk.optimal_position_ratio:.2%}"
        )

    # In DANGER, new exposure is blocked when no position already exists.
    if risk_level == RiskLevel.DANGER:
        current_lots_check = portfolio.positions[ticker].shares if ticker in portfolio.positions else 0
        if current_lots_check == 0:
            # No new exposure is allowed in DANGER without an existing position.
            # Hold is mandatory in DANGER when no position exists.
            logger.warning(f"DANGER risk state with no existing position for {ticker}; forcing HOLD.")
            return {
                "decision": FuturesDecision(
                    ticker=ticker,
                    action=FuturesAction.HOLD,
                    lots=0,
                    price=0,
                    justification=f"DANGER risk state (cashflow ratio={cashflow_ratio*100:.1f}%) with no existing position; forcing HOLD.",
                )
            }

    target_value = account_equity * position_risk.optimal_position_ratio

    target_lots = int(target_value / (current_price * multiplier))

    is_long_target = target_lots >= 0
    margin_rate = contract_info[
        'margin_rate_long' if is_long_target else 'margin_rate_short'
    ]

    # Cap the target by remaining margin availability.
    margin_available = remaining_margin
    margin_required = current_price * abs(target_lots) * multiplier * margin_rate

    if margin_required > margin_available:
        max_lots = int(margin_available / (current_price * multiplier * margin_rate))
        logger.warning(f"Margin availability capped target lots for {ticker}: target={target_lots}, max_allowed={max_lots}")
        target_lots = max_lots if is_long_target else -max_lots

    current_lots = 0
    if ticker in portfolio.positions:
        current_lots = portfolio.positions[ticker].shares

    # Cooling period: block voluntary reductions within two days of opening unless hard loss or risk pressure applies.
    cooling_period_note = ""
    if (
        current_position is not None
        and current_lots != 0
        and risk_level not in (RiskLevel.DANGER, RiskLevel.EMERGENCY)
        and getattr(current_position, "entry_date", None)
    ):
        held_days = _days_held(current_position.entry_date, trading_date)
        reducing_exposure = (
            target_lots == 0
            or (current_lots > 0 and target_lots < current_lots)
            or (current_lots < 0 and target_lots > current_lots)
            or (current_lots > 0 and target_lots < 0)
            or (current_lots < 0 and target_lots > 0)
        )
        lifecycle_exit_required = (
            "position_lifecycle_failed" in control_reasons
            or "position_lifecycle_probe_expired" in control_reasons
            or "position_lifecycle_loss_revalidation_failed" in control_reasons
        )
        if held_days < 2 and reducing_exposure and not lifecycle_exit_required:
            unrealized_loss_ratio = (
                float(getattr(current_position, "unrealized_pnl", 0.0) or 0.0)
                / float(getattr(current_position, "margin_used", 0.0) or 1.0)
                if float(getattr(current_position, "margin_used", 0.0) or 0.0) > 0
                else 0.0
            )
            if unrealized_loss_ratio > -0.05:
                logger.info(
                    f"Cooling period for {ticker}: held {held_days} day(s), "
                    f"loss ratio {unrealized_loss_ratio:.2%}; keeping target at current lots {current_lots}"
                )
                target_lots = current_lots
                current_ratio = _signed_position_ratio(current_position, account_equity)
                position_risk.optimal_position_ratio = current_ratio
                target_value = account_equity * current_ratio
                margin_required = current_price * abs(target_lots) * multiplier * margin_rate
                cooling_period_note = (
                    f"[Cooling period: held {held_days} day(s), voluntary reduction blocked "
                    f"unless loss exceeds 5% of margin used]"
                )
        elif held_days < 2 and reducing_exposure and lifecycle_exit_required:
            logger.info(
                f"Cooling period bypassed for {ticker}: lifecycle exit/reduction reason={control_reasons[-1]}"
            )

    if risk_level == RiskLevel.EMERGENCY:
        if current_lots > 0:
            emergency_decision = FuturesDecision(
                ticker=ticker,
                action=FuturesAction.CLOSE_LONG,
                lots=abs(current_lots),
                price=current_price,
                settle_price=settle_price,
                margin_rate=contract_info['margin_rate_long'],
                contract_multiplier=multiplier,
                contract_code=contract_code,
                justification=f"EMERGENCY risk level: flatten long position in {ticker}"
            )
        elif current_lots < 0:
            emergency_decision = FuturesDecision(
                ticker=ticker,
                action=FuturesAction.CLOSE_SHORT,
                lots=abs(current_lots),
                price=current_price,
                settle_price=settle_price,
                margin_rate=contract_info['margin_rate_short'],
                contract_multiplier=multiplier,
                contract_code=contract_code,
                justification=f"EMERGENCY risk level: flatten short position in {ticker}"
            )
        else:
            emergency_decision = FuturesDecision(
                ticker=ticker,
                action=FuturesAction.HOLD,
                lots=0,
                price=current_price,
                settle_price=settle_price,
                margin_rate=contract_info['margin_rate_long'],
                contract_multiplier=multiplier,
                contract_code=contract_code,
                justification=f"EMERGENCY risk level: block new positions in {ticker}"
            )

        logger.log_decision(ticker, emergency_decision)
        recommendation = _build_phase1_recommendation(
            config_id=config_id,
            full_config=full_config,
            portfolio=portfolio,
            ticker=ticker,
            trading_date=trading_date,
            contract_code=contract_code,
            decision=emergency_decision,
            morning_price_context=morning_price_context,
            analyst_signals=analyst_signals,
        )
        return {"decision": emergency_decision, "recommendation": recommendation}

    lots_to_trade = abs(target_lots - current_lots) if abs(target_lots - current_lots) > 0 else 0

    # Track why no tradable lots remain after risk and margin constraints.
    lots_to_trade_reason = None
    if lots_to_trade == 0:
        if target_lots == 0:
            lots_to_trade_reason = "llm_neutral"
        elif abs(target_lots - current_lots) < 0.01:
            lots_to_trade_reason = "position_matched"
        elif margin_required > margin_available:
            lots_to_trade_reason = "margin_insufficient"
        elif risk_level == RiskLevel.DANGER:
            lots_to_trade_reason = "danger_zone_ban"
        elif abs(new_net_exposure) > max_net_exposure:
            lots_to_trade_reason = "net_exposure_limit"
        else:
            lots_to_trade_reason = "unknown"
        if control_block_reason:
            lots_to_trade_reason = control_block_reason
        elif target_lots == 0 and control_reasons and abs(pre_control_ratio) > 1e-12:
            lots_to_trade_reason = control_reasons[-1]
    if cooling_period_note:
        lots_to_trade_reason = "cooling_period"

    recommendation_intent = recommendation_intent_from_lots(current_lots, target_lots)
    pre_open_action = FuturesAction(recommendation_intent["action"])
    pre_open_lots = int(recommendation_intent["lots"])

    plan_snapshot = _build_pre_open_plan_snapshot(
        target_lots=target_lots,
        current_price=current_price,
        position_ratio=position_risk.optimal_position_ratio,
        risk_level=risk_level,
        long_scores=long_scores,
        short_scores=short_scores,
        margin_rate=margin_rate,
    )
    plan_snapshot["current_lots_before_open"] = int(current_lots)
    plan_snapshot["target_value"] = float(target_value)
    plan_snapshot["account_equity"] = float(account_equity)
    plan_snapshot["current_net_exposure"] = float(current_net_exposure)
    plan_snapshot["current_ticker_exposure"] = float(current_ticker_exposure)
    plan_snapshot["projected_net_exposure"] = float(new_net_exposure)
    plan_snapshot["tradable_lots_if_executed_now"] = int(lots_to_trade)
    plan_snapshot["tradable_lots_reason"] = lots_to_trade_reason
    plan_snapshot["max_position_ratio_after_performance"] = float(max_position_ratio)
    plan_snapshot["analyst_signal_combo"] = list(signal_combo)
    plan_snapshot["adaptive_fusion"] = fusion_context
    plan_snapshot["decision_horizon"] = _resolve_decision_horizon(analyst_signals, target_lots)
    plan_snapshot["execution_horizon"] = "short"
    plan_snapshot["validation_horizon"] = plan_snapshot["decision_horizon"]
    plan_snapshot["business_quality_summary"] = summarize_business_quality(analyst_signals)
    rebalance_action_type = str(recommendation_intent.get("action_type") or "rebalance")
    plan_snapshot["rebalance_summary"] = {
        "action_type": rebalance_action_type,
        "current_lots": int(current_lots),
        "target_lots": int(target_lots),
        "lots_delta": int(target_lots - current_lots),
        "holding_days": (
            _days_held(getattr(current_position, "entry_date", None), trading_date)
            if current_position and current_lots != 0
            else None
        ),
        "turnover_notional_estimate": float(lots_to_trade * current_price * multiplier),
        "reason": lots_to_trade_reason or (control_reasons[-1] if control_reasons else "target_plan"),
        "control_reasons": sorted(set(control_reasons)),
    }
    plan_snapshot["recommendation_position_consistency"] = build_lot_intent_consistency(
        current_lots=current_lots,
        target_lots=target_lots,
        action=pre_open_action,
        lots=pre_open_lots,
        mode="recommendation",
    )
    holding_diagnostics = {}
    if isinstance(control_diagnostics, dict):
        holding_diagnostics = (
            control_diagnostics.get("holding_rebalance_control")
            if isinstance(control_diagnostics.get("holding_rebalance_control"), dict)
            else {}
        )
    if holding_diagnostics:
        plan_snapshot["rebalance_summary"]["lifecycle_decision"] = holding_diagnostics.get("decision")
        plan_snapshot["rebalance_summary"]["lifecycle_classification"] = holding_diagnostics.get("lifecycle_classification")
        plan_snapshot["rebalance_summary"]["position_pnl_ratio"] = holding_diagnostics.get("position_pnl_ratio")
        plan_snapshot["rebalance_summary"]["market_confirmation_score"] = holding_diagnostics.get("confirmation_score")
        plan_snapshot["rebalance_summary"]["current_side_strength"] = holding_diagnostics.get("current_side_strength")
        plan_snapshot["rebalance_summary"]["target_side_strength"] = holding_diagnostics.get("reversal_or_exit_strength")
    alpha_protect_records = [
        row
        for row in (adaptive_policy_state or [])
        if str(row.get("policy_type") or "") == "alpha_promotion"
        and str(row.get("policy_action") or "").lower() in {"protect", "allow"}
    ]
    tail_loss_records = [
        row
        for row in (adaptive_policy_state or [])
        if str(row.get("policy_type") or "") == "tail_loss_sentinel"
    ]
    if target_lots == 0:
        pm_decision_layer = "no_trade"
    elif alpha_protect_records:
        pm_decision_layer = "deployable_alpha"
    elif abs(target_lots) < abs(current_lots):
        pm_decision_layer = "risk_reduction"
    elif current_lots == 0 and target_lots != 0:
        pm_decision_layer = "tradeable_setup"
    elif abs(target_lots) > abs(current_lots):
        pm_decision_layer = "tradeable_setup"
    else:
        pm_decision_layer = "direction_only"
    plan_snapshot["pm_decision_layer"] = pm_decision_layer
    plan_snapshot["research_memory_maturity"] = {
        "candidate_hypothesis_count": pm_learning_audit.get("candidate_hypothesis_count", 0),
        "validated_hypothesis_count": pm_learning_audit.get("validated_hypothesis_count", 0),
        "alpha_promotion_records": len(alpha_protect_records),
        "tail_loss_sentinel_records": len(tail_loss_records),
        "candidate_hypotheses_prior_only": True,
        "mature_alpha_can_scale_only_with_current_confirmation": True,
    }
    if pm_learning_audit.get("enabled"):
        if lots_to_trade_reason == "position_matched" and pm_learning_audit.get("candidate_hypothesis_count", 0):
            pm_learning_audit["position_matched_boundary"] = (
                "candidate_hypotheses_not_counted_as_position_support"
            )
        control_diagnostics["exploratory_learning_context"] = pm_learning_audit
    pm_llm_audit = llm_audit_metadata(portfolio_llm_config)
    plan_snapshot["portfolio_manager_llm"] = {
        "mode": "cloud_only",
        "provider": pm_llm_audit.get("provider"),
        "model": pm_llm_audit.get("model"),
        "reasoning_effort": pm_llm_audit.get("reasoning_effort"),
        "base_url": pm_llm_audit.get("base_url"),
        "api_key_env": pm_llm_audit.get("api_key_env"),
    }
    if auditor_output:
        auditor_payload = (
            auditor_output.model_dump() if hasattr(auditor_output, "model_dump") else dict(auditor_output)
        )
        plan_snapshot["trade_auditor"] = auditor_payload
        plan_snapshot["decision_planner"] = auditor_payload
    if control_reasons or control_notes or control_diagnostics:
        plan_snapshot["strategy_controls"] = {
            "reasons": sorted(set(control_reasons)),
            "notes": control_notes,
            "diagnostics": control_diagnostics,
        }
    if ticker_perf:
        plan_snapshot["ticker_performance"] = ticker_perf

    pre_open_decision = FuturesDecision(
        ticker=ticker,
        action=pre_open_action,
        lots=pre_open_lots,
        price=current_price,
        settle_price=settle_price,
        margin_rate=margin_rate,
        contract_multiplier=multiplier,
        contract_code=contract_code,
        justification=(
            f"{position_risk.justification}\n"
            f"[Pre-open target plan: target_lots={target_lots}, "
            f"target_position_ratio={position_risk.optimal_position_ratio:.2%}, "
            f"tradable_lots_if_executed_now={lots_to_trade} ({lots_to_trade_reason}), "
            f"recommendation_action={pre_open_action.value}, recommendation_lots={pre_open_lots}]"
            + (f"\n{cooling_period_note}" if cooling_period_note else "")
        ),
    )
    recommendation = _build_phase1_recommendation(
        config_id=config_id,
        full_config=full_config,
        portfolio=portfolio,
        ticker=ticker,
        trading_date=trading_date,
        contract_code=contract_code,
        decision=pre_open_decision,
        morning_price_context=morning_price_context,
        analyst_signals=analyst_signals,
        plan_snapshot=plan_snapshot,
        market_confirmation=market_confirmation if market_confirmation.get("enabled") else None,
    )
    return {"decision": pre_open_decision, "recommendation": recommendation}

def calculate_long_short_signals(
    ticker: str,
    analyst_signals: list,
    fundamental_basis: float,
    weights: dict = None,
    fusion_context: dict = None,
    full_config: dict = None,
) -> tuple[dict, dict]:
    """
    Combine analyst outputs into comparable long-side and short-side scores.

    Args:
        ticker: Underlying code.
        analyst_signals: Analyst outputs collected for the ticker.
        fundamental_basis: Parsed basis percentage from the fundamental analyst.
        weights: Optional analyst-weight override.

    Returns:
        A pair of dictionaries for long and short scoring.
    """
    long_score = 0.0
    short_score = 0.0
    long_confidence = 0.0
    short_confidence = 0.0

    fusion_context = fusion_context or {}
    full_config = full_config or {}
    quality_summary = fusion_context.get("analyst_quality") or {}

    # Group signals by analyst for weighted fusion.
    signals_by_agent = {}
    for signal in analyst_signals:
        if hasattr(signal, 'agent_name'):
            agent_name = "commodity_news" if signal.agent_name == "company_news" else signal.agent_name
            signals_by_agent[agent_name] = signal

    # Start from the strongest basis-driven directional bias when available.
    fundamental = signals_by_agent.get('fundamental')
    technical = signals_by_agent.get('technical')
    news = signals_by_agent.get('commodity_news')

    if fundamental:
        fundamental_confidence = _effective_signal_confidence(
            fundamental,
            "fundamental",
            quality_summary,
            full_config,
        )
        if fundamental_basis >= 10:
            long_score = 0.95 * fundamental_confidence
            long_confidence = fundamental_confidence
        elif fundamental_basis <= -10:
            short_score = 0.95 * fundamental_confidence
            short_confidence = fundamental_confidence

    # Fall back to weighted multi-analyst fusion when basis alone is not decisive.
    if long_score < 0.9 or short_score < 0.9:
        if weights is None:
            # Default weights lean on fundamentals when basis is strong; otherwise use the balanced mix.
            if abs(fundamental_basis) >= 10:
                weights = {'fundamental': 0.7, 'technical': 0.2, 'commodity_news': 0.1}
            else:
                weights = {'technical': 0.5, 'commodity_news': 0.3, 'fundamental': 0.2}
        # Otherwise, keep the dynamic weights computed above.

        for agent_name, weight in weights.items():
            signal = signals_by_agent.get(agent_name)
            if signal:
                effective_confidence = _effective_signal_confidence(
                    signal,
                    agent_name,
                    quality_summary,
                    full_config,
                )
                if _signal_to_text(signal.signal).upper() == 'BULLISH':
                    long_score += weight * effective_confidence
                elif _signal_to_text(signal.signal).upper() == 'BEARISH':
                    short_score += weight * effective_confidence

        # Average confidence across the active analyst signals.
        active_signals = [s for s in [fundamental, technical, news] if s]
        if active_signals:
            active_confidences = []
            for signal in active_signals:
                agent_name = _normalize_agent_name(signal.agent_name)
                active_confidences.append(
                    _effective_signal_confidence(signal, agent_name, quality_summary, full_config)
                )
            long_confidence = sum(active_confidences) / len(active_confidences)
            short_confidence = long_confidence

    return {
        'score': max(0, long_score),
        'confidence': long_confidence,
        'has_strong_basis': fundamental_basis >= 10,
        'weights_used': weights or {},
        'fusion_sector': fusion_context.get("sector"),
    }, {
        'score': max(0, short_score),
        'confidence': short_confidence,
        'has_strong_basis': fundamental_basis <= -10,
        'weights_used': weights or {},
        'fusion_sector': fusion_context.get("sector"),
    }

def _build_dynamic_weight_prompt(
    weights: dict,
    max_position_ratio: float,
    basis_pct: float,
    market_state: dict,
    fundamental_trends: dict,
    fusion_context: dict = None,
) -> str:
    """Build the dynamic weight prompt for the futures portfolio manager."""

    market_state_info = ""
    if market_state:
        state_map = {"trending": "trending", "ranging": "ranging", "reversal": "reversal"}
        trend_map = {"up": "up", "down": "down", "sideways": "sideways"}
        vol_map = {"high": "high", "medium": "medium", "low": "low"}

        ms = market_state.get
        market_state_info = f"""
**Market regime context**:
- regime: {state_map.get(ms("market_state"), ms("market_state"))}
- trend: {trend_map.get(ms("trend_direction"), ms("trend_direction"))}
- volatility: {vol_map.get(ms("volatility_level"), ms("volatility_level"))}
"""

    fundamental_info = ""
    if fundamental_trends:
        ft = fundamental_trends
        key_drivers = ft.get("key_drivers", [])
        driver_text = ", ".join(key_drivers) if key_drivers else "none"
        fundamental_info = f"""
**Fundamental context**:
- inventory trend: {ft.get("inventory_trend", "unknown")}
- supply-demand balance: {ft.get("supply_demand_balance", "unknown")}
- key drivers: {driver_text}
- confidence: {ft.get("confidence", 0):.2%}
"""

    fusion_context = fusion_context or {}
    analyst_quality_lines = []
    for analyst, payload in (fusion_context.get("analyst_quality") or {}).items():
        analyst_quality_lines.append(
            f"- {analyst}: signal={payload.get('signal')}, "
            f"tradeability={payload.get('tradeability')}, "
            f"effective_confidence={float(payload.get('effective_confidence', 0.0) or 0.0):.2f}, "
            f"risk_flags={payload.get('risk_flags', [])}"
        )
    analyst_quality_text = "\n".join(analyst_quality_lines) or "- unavailable"

    prompt = f"""
=== DYNAMIC WEIGHT MODE ===

{market_state_info}{fundamental_info}
**Current basis signal**: {basis_pct:+.1f}%
**Commodity sector**: {fusion_context.get('sector', 'generic')}

Current dynamic weights:
- Fundamental: {weights.get("fundamental", 0):.2%}
- Technical: {weights.get("technical", 0):.2%}
- News: {weights.get("commodity_news", 0):.2%}

Analyst quality after structured preprocessing:
{analyst_quality_text}

Decision framework:
1. Keep market-adaptive fusion: adjust the three analyst views by market regime, sector logic, and data quality.
2. Do not let a low-tradeability analyst dominate the direction, even when its raw confidence is high.
3. Use the sector-specific weighting as a prior, then respect the quality-adjusted weights above.
4. Convert the blended signal into LONG, SHORT, or NEUTRAL.
5. Use the base sizing anchor and recommend an initial position ratio no larger than {max_position_ratio:.2f}; dynamic capital-utilization control may resize validated opportunities later.
6. Treat fundamental signals as medium-term anchors, technical signals as timing filters, and news as event shocks only when quality is high.
7. Existing positions should not be flipped or fully closed unless the contrary evidence is materially stronger than the evidence required for a new entry.

Output requirements:
- optimal_position_ratio: a signed float between -{max_position_ratio:.2f} and +{max_position_ratio:.2f}; positive is LONG, negative is SHORT, zero is NEUTRAL
- justification: concise reasoning that references market regime, sector, analyst quality, and the weighted signal
"""

    return prompt

def calculate_position_ratio_with_balance(
    ticker: str,
    long_scores: dict,
    short_scores: dict,
    max_position_ratio: float,
    risk_level: RiskLevel,
    full_config: dict
) -> tuple[float, str]:
    """
    Convert blended long/short scores into a signed target position ratio.

    Args:
        ticker: Underlying code.
        long_scores: Aggregated bullish score payload.
        short_scores: Aggregated bearish score payload.
        max_position_ratio: Base sizing anchor for the initial absolute position size.
        risk_level: Current portfolio risk classification.
        full_config: Runtime configuration.

    Returns:
        A tuple of (absolute_ratio, direction).
    """
    position_scaling = get_position_scaling_factor(risk_level, full_config)

    if long_scores['score'] > short_scores['score']:
        # Long bias.
        direction = 'LONG'
        base_ratio = max_position_ratio

        final_ratio = base_ratio * position_scaling * long_scores['confidence']

        final_ratio = min(final_ratio, 0.30)

    elif short_scores['score'] > long_scores['score']:
        # Short bias.
        direction = 'SHORT'
        base_ratio = max_position_ratio

        final_ratio = base_ratio * position_scaling * short_scores['confidence']

        final_ratio = min(final_ratio, 0.30)

    else:
        # No directional edge remains.
        direction = 'NEUTRAL'
        final_ratio = 0

    return final_ratio, direction
