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
from llm.inference import agent_call
from apis.contract_info_cache import FuturesContractInfoCache
from agents.auditor import TradeAuditor, TradeAuditorInput
from util.db_helper import get_db
from util.logger import logger
from util.text_sanitize import sanitize_visible_text
from tools.agent_tools.dynamic_weights import DynamicWeightCalculator, calibrate_weights_by_signal_history
from tools.agent_tools.market_confirmation import MarketConfirmationEngine

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
    Return the maximum single-position ratio for the current risk level.

    Args:
        risk_level: Current portfolio risk classification.
        config: Runtime configuration.

    Returns:
        The per-instrument position cap.
    """
    risk_control = config.get('risk_control', {})
    max_single_position = risk_control.get('max_single_position_ratio', {
        'safe': 0.15,
        'warning': 0.10,
        'danger': 0.05
    })

    return max_single_position.get(risk_level.value.lower(), 0.15)

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
        "probe_max_hold_days": 2,
        "probe_min_profit_ratio": 0.01,
        "probe_min_confirmation_score": 0.55,
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
):
    trading_date_value = trading_date.strftime("%Y-%m-%d") if hasattr(trading_date, "strftime") else str(trading_date)
    signal_snapshot = {}
    for idx, signal in enumerate(analyst_signals):
        key = getattr(signal, "agent_name", None) or f"signal_{idx}"
        signal_snapshot[key] = signal.model_dump() if hasattr(signal, "model_dump") else dict(signal)
    if plan_snapshot:
        signal_snapshot["pre_open_plan"] = plan_snapshot
    if market_confirmation:
        signal_snapshot["market_confirmation"] = market_confirmation
    signal_snapshot["audit"] = {
        "pre_open_only": True,
        "info_cutoff": "pre_open",
        "phase1_generates_recommendation_only": True,
    }

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

def _build_pre_open_plan_snapshot(
    target_lots: int,
    current_price: float,
    position_ratio: float,
    risk_level: RiskLevel,
    long_scores: dict,
    short_scores: dict,
) -> dict:
    direction = _resolve_pre_open_signal_direction(target_lots)
    return {
        "signal_direction": direction,
        "signal_confidence": _resolve_pre_open_signal_confidence(direction, long_scores, short_scores),
        "target_position_ratio": float(position_ratio),
        "target_lots_estimate": int(target_lots),
        "reference_price": float(current_price),
        "risk_level": risk_level.value,
    }


def _analyst_signal_combo(analyst_signals: list) -> tuple[str, str, str]:
    signals = {
        "technical": "Neutral",
        "fundamental": "Neutral",
        "commodity_news": "Neutral",
    }
    for signal in analyst_signals:
        agent_name = getattr(signal, "agent_name", None)
        if agent_name == "company_news":
            agent_name = "commodity_news"
        if agent_name in signals:
            value = getattr(signal, "signal", "Neutral")
            signals[agent_name] = getattr(value, "value", value)
    return (signals["technical"], signals["fundamental"], signals["commodity_news"])


def _target_side_from_ratio(position_ratio: float) -> str:
    if position_ratio > 0:
        return "long"
    if position_ratio < 0:
        return "short"
    return "flat"


def _same_sign(lhs: float, rhs: float) -> bool:
    return (lhs > 0 and rhs > 0) or (lhs < 0 and rhs < 0)


def _is_new_or_increasing_exposure(target_ratio: float, current_ratio: float) -> bool:
    if abs(target_ratio) <= 1e-12:
        return False
    if abs(current_ratio) <= 1e-12:
        return True
    if not _same_sign(target_ratio, current_ratio):
        return True
    return abs(target_ratio) > abs(current_ratio)


def _scale_signed_ratio(position_ratio: float, multiplier: float) -> float:
    return (1.0 if position_ratio >= 0 else -1.0) * abs(position_ratio) * max(0.0, multiplier)


def _apply_trade_plan_multiplier(
    *,
    target_ratio: float,
    current_ratio: float,
    multiplier: float,
) -> float:
    """Scale new risk without forcing an existing same-side position to shrink."""
    scaled_ratio = _scale_signed_ratio(target_ratio, multiplier)
    if _same_sign(target_ratio, current_ratio) and abs(current_ratio) > abs(scaled_ratio):
        return current_ratio
    return scaled_ratio


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
        drawdown = float(drawdown_state.get("drawdown", 0.0) or 0.0)
        if drawdown >= float(drawdown_control.get("hard_drawdown", 0.025)):
            position_ratio = 0.0
            reasons.append("drawdown_control")
            notes.append(f"hard drawdown control: drawdown={drawdown:.2%}, new exposure blocked")
        elif drawdown >= float(drawdown_control.get("soft_drawdown", 0.015)):
            before = position_ratio
            position_ratio = _scale_signed_ratio(position_ratio, float(drawdown_control.get("soft_cap_multiplier", 0.70)))
            reasons.append("drawdown_control")
            notes.append(
                f"soft drawdown control: drawdown={drawdown:.2%}, ratio {before:.2%}->{position_ratio:.2%}"
            )

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
            position_ratio = 0.0
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
) -> tuple[float, list[str], list[str], dict]:
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    control = full_config.get("capital_utilization_control", {}) or {}
    if not control.get("enabled", False):
        return position_ratio, reasons, notes, diagnostics
    if not _is_new_or_increasing_exposure(position_ratio, current_ratio):
        return position_ratio, reasons, notes, diagnostics
    if margin_rate <= 0 or abs(position_ratio) <= 0:
        return position_ratio, reasons, notes, diagnostics

    side = _target_side_from_ratio(position_ratio)
    trade_control = full_config.get("trade_frequency_control", {}) or {}
    weak_combos = [tuple(item) for item in (trade_control.get("weak_signal_combos") or [])]
    if bool(control.get("disable_scaling_when_weak_combo", True)) and signal_combo in weak_combos:
        diagnostics["capital_utilization_skip"] = "weak_signal_combo"
        return position_ratio, reasons, notes, diagnostics

    side_override = ((trade_control.get("side_overrides") or {}).get(ticker) or {})
    override_key = f"{side}_cap_multiplier"
    if bool(control.get("disable_scaling_for_static_capped_side", True)) and override_key in side_override:
        diagnostics["capital_utilization_skip"] = "static_side_cap"
        return position_ratio, reasons, notes, diagnostics

    confirmation_score = float(market_confirmation.get("confirmation_score", 0.0) or 0.0)
    min_score = float(control.get("min_confirmation_score_for_scaling", 0.60))
    if confirmation_score < min_score:
        diagnostics["capital_utilization_skip"] = "confirmation_score_below_threshold"
        return position_ratio, reasons, notes, diagnostics

    if bool(control.get("scale_only_when_recent_pnl_positive", True)) and db and config_id:
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
        if (
            total_trades <= 0
            or total_pnl <= 0
        ):
            diagnostics["capital_utilization_skip"] = "recent_side_pnl_not_positive"
            return position_ratio, reasons, notes, diagnostics

    target_total_margin_ratio = float(control.get("target_margin_ratio_confirmed", 0.12))
    max_after_scaling = float(control.get("max_margin_ratio_after_scaling", 0.20))
    allowed_increment_margin_ratio = max(0.0, min(
        target_total_margin_ratio - current_margin_ratio,
        max_after_scaling - current_margin_ratio,
    ))
    if allowed_increment_margin_ratio <= 0:
        diagnostics["capital_utilization_skip"] = "target_margin_already_reached"
        return position_ratio, reasons, notes, diagnostics

    proposed_abs_ratio = min(max_position_ratio, allowed_increment_margin_ratio / margin_rate)
    if proposed_abs_ratio > abs(position_ratio):
        before = position_ratio
        position_ratio = (1.0 if position_ratio > 0 else -1.0) * proposed_abs_ratio
        reasons.append("capital_utilization_guard")
        notes.append(
            f"capital utilization scaled ratio {before:.2%}->{position_ratio:.2%}; "
            f"confirmation_score={confirmation_score:.2f}"
        )

    return position_ratio, reasons, notes, diagnostics


def _safe_float(value, default: float = 0.0) -> float:
    try:
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
    return (
        _payload_supports_side(payload, side, min_confidence)
        and str(payload.get("tradeability", "unknown")).lower() in allowed_tradeability
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


def _signed_abs(side: str, abs_ratio: float) -> float:
    return abs(abs_ratio) if side == "long" else -abs(abs_ratio)


def _position_pnl_ratio(current_position) -> float:
    if not current_position:
        return 0.0
    margin_used = _safe_float(getattr(current_position, "margin_used", 0.0), 0.0)
    if margin_used <= 0:
        return 0.0
    return _safe_float(getattr(current_position, "unrealized_pnl", 0.0), 0.0) / margin_used


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
) -> tuple[float, list[str], list[str], dict]:
    reasons: list[str] = []
    notes: list[str] = []
    diagnostics: dict = {}
    control = _get_holding_rebalance_config(full_config)
    if not control.get("enabled", True):
        return position_ratio, reasons, notes, diagnostics

    current_side = _target_side_from_ratio(current_ratio)
    target_side = _target_side_from_ratio(position_ratio)
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
        }
    }
    detail = diagnostics["holding_rebalance_control"]

    if risk_level in (RiskLevel.DANGER, RiskLevel.EMERGENCY):
        detail["decision"] = "skip_for_risk_state"
        return position_ratio, reasons, notes, diagnostics

    if current_side == "flat":
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

    lifecycle_enabled = bool(lifecycle_config.get("enabled", True))
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
        "lifecycle_classification": (
            "failed_position" if failed_position else "trend_position" if trend_position else "probe_position" if probe_expired else "normal"
        ),
        "trend_position": bool(trend_position),
        "failed_position": bool(failed_position),
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

    if probe_expired and target_side == current_side:
        reasons.append("position_lifecycle_probe_expired")
        notes.append(
            f"{ticker} {current_side} probe expired: held_days={held_days}, "
            f"pnl_ratio={position_pnl_ratio:.2%}, confirmation={confirmation_score:.2f}"
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

    max_total_margin_ratio = cfg.get("max_total_margin_ratio", 0.40)
    risk_buffer_ratio = cfg.get("risk_buffer_ratio", 0.10)

    full_config = state.get("full_config", cfg)
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
        hard_trigger = (
            avg_pnl <= float(performance_control.get("hard_avg_pnl_below", -800))
            or (
                cumulative_pnl < 0
                and win_rate <= float(performance_control.get("hard_win_rate_below", 0.35))
            )
        )
        if hard_trigger:
            hard_cap = float(performance_control.get("hard_cap_ratio", 0.03))
            max_single_margin_ratio = min(max_single_margin_ratio, hard_cap)
            logger.info(
                f"Ticker performance hard cap for {ticker}: avg_daily_pnl={avg_pnl:.0f}, "
                f"win_rate={win_rate:.0%}, cumulative_pnl={cumulative_pnl:.0f}, "
                f"single-position cap {original_cap:.2%} -> {max_single_margin_ratio:.2%}"
            )
        elif avg_pnl < float(performance_control.get("soft_avg_pnl_below", -300)):
            max_single_margin_ratio *= float(performance_control.get("soft_cap_multiplier", 0.50))
            logger.info(
                f"Ticker performance scaling for {ticker}: avg_daily_pnl={avg_pnl:.0f}, "
                f"single-position cap {original_cap:.2%} -> {max_single_margin_ratio:.2%}"
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
            f"max_single_margin_ratio={max_single_margin_ratio*100:.0f}%"
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
        # Pull optional DeepAnalyze summaries when multiple analysts are enabled.
        market_state_analysis = state.get("deepanalyze_market_state")
        fundamental_trends_analysis = state.get("deepanalyze_fundamental_trends")

        # Extract the basis percentage from the fundamental analyst output.
        fundamental_signal = signals_by_agent.get('fundamental')
        basis_pct = 0.0
        fundamental_quality = {}
        if fundamental_signal:
            basis_pct = DynamicWeightCalculator.extract_basis_from_signal(fundamental_signal)
            fundamental_quality = DynamicWeightCalculator.extract_quality_from_signal(fundamental_signal)

        # Build dynamic weights from the current basis and DeepAnalyze context.
        calculator = DynamicWeightCalculator(full_config)
        weights = calculator.calculate(
            basis_pct,
            market_state_analysis,
            fundamental_trends_analysis,
            fundamental_quality,
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
            market_state=market_state_analysis,
            fundamental_trends=fundamental_trends_analysis,
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
    # Pull optional DeepAnalyze context for dynamic weighting and prompt shaping.
    market_state_analysis = state.get("deepanalyze_market_state")
    fundamental_trends_analysis = state.get("deepanalyze_fundamental_trends")

    # Reuse the same quality-aware adaptive weights shown to the portfolio LLM.
    dynamic_weights = weights if weights else None
    if not fusion_context:
        calculator = DynamicWeightCalculator(full_config)
        if calculator.enabled:
            dynamic_weights = calculator.calculate(
                fundamental_basis,
                market_state_analysis,
                fundamental_trends_analysis,
                fundamental_quality,
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
    control_block_reason = None
    pre_control_ratio = position_risk.optimal_position_ratio
    signal_combo = _analyst_signal_combo(analyst_signals)
    auditor_output = None
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
        strategy_memory = {}
        if db and config_id and auditor_side in {"long", "short"}:
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
            deepanalyze_market_state=market_state_analysis,
            deepanalyze_fundamental_trends=fundamental_trends_analysis,
            recent_ticker_side_performance=recent_side_performance,
            recent_conditional_performance=recent_conditional_performance,
            strategy_memory=strategy_memory,
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
        elif auditor_output.decision == "reduce":
            position_risk.optimal_position_ratio = _apply_trade_plan_multiplier(
                target_ratio=position_risk.optimal_position_ratio,
                current_ratio=current_ticker_exposure,
                multiplier=auditor_output.position_ratio_multiplier,
            )
            control_reasons.extend(auditor_output.reasons)
            if abs(position_risk.optimal_position_ratio) <= 1e-12 and abs(before_ratio) > 1e-12:
                control_reasons.append("trade_auditor_reduce_to_zero")
            control_notes.extend(auditor_output.notes)
            control_notes.append(
                f"trade auditor reduced {auditor_output.target_side} ratio "
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

    position_risk.optimal_position_ratio, reasons, notes, diagnostics = _apply_drawdown_and_ticker_loss_control(
        db=db,
        config_id=config_id,
        ticker=ticker,
        trading_date=trading_date,
        position_ratio=position_risk.optimal_position_ratio,
        current_ratio=current_ticker_exposure,
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

    net_exposure_config = full_config.get('net_exposure_control')
    if net_exposure_config is None:
        risk_control = full_config.get('risk_control', {})
        net_exposure_config = risk_control.get('net_exposure_control', {})
    max_net_exposure = net_exposure_config.get('max_net_exposure', 0.50)
    symmetric_scaling = net_exposure_config.get('symmetric_scaling', True)

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
        if held_days < 2 and reducing_exposure:
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

    pre_open_action = FuturesAction.HOLD
    if target_lots > 0:
        pre_open_action = FuturesAction.OPEN_LONG
    elif target_lots < 0:
        pre_open_action = FuturesAction.OPEN_SHORT

    plan_snapshot = _build_pre_open_plan_snapshot(
        target_lots=target_lots,
        current_price=current_price,
        position_ratio=position_risk.optimal_position_ratio,
        risk_level=risk_level,
        long_scores=long_scores,
        short_scores=short_scores,
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
    if current_lots == target_lots:
        rebalance_action_type = "keep"
    elif current_lots == 0 and target_lots != 0:
        rebalance_action_type = "new_entry"
    elif target_lots == 0 and current_lots != 0:
        rebalance_action_type = "exit"
    elif current_lots * target_lots < 0:
        rebalance_action_type = "reverse"
    elif abs(target_lots) > abs(current_lots):
        rebalance_action_type = "increase"
    elif abs(target_lots) < abs(current_lots):
        rebalance_action_type = "reduce"
    else:
        rebalance_action_type = "rebalance"
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
    plan_snapshot["portfolio_manager_llm"] = {
        "mode": "cloud_only",
        "provider": portfolio_llm_config.get("provider"),
        "model": portfolio_llm_config.get("model"),
        "use_deepanalyze": False,
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
        lots=abs(target_lots),
        price=current_price,
        settle_price=settle_price,
        margin_rate=margin_rate,
        contract_multiplier=multiplier,
        contract_code=contract_code,
        justification=(
            f"{position_risk.justification}\n"
            f"[Pre-open target plan: target_lots={target_lots}, "
            f"target_position_ratio={position_risk.optimal_position_ratio:.2%}, "
            f"tradable_lots_if_executed_now={lots_to_trade} ({lots_to_trade_reason})]"
            + (f"\n{cooling_period_note}" if cooling_period_note else "")
        ),
    )
    recommendation = _build_phase1_recommendation(
        config_id=config_id,
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
5. Respect the risk cap and recommend an optimal position ratio no larger than {max_position_ratio:.2f}.
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
        max_position_ratio: Upper bound for absolute position size.
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

