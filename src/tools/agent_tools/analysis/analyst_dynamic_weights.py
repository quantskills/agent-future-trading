"""
Dynamic weight calculator for futures analysts.

This module adjusts the relative weights of fundamental, technical, and
commodity-news analysts based on:
- basis strength
- market confirmation quality
- structured fundamental-trend context
"""

from typing import Any, Dict, Optional
import re

from tools.agent_tools.analysis.analyst_learning_calibration import retrieve_analyst_policy_calibration
from util.logger import logger


def _bounded_float(value: Any, default: float = 0.0, lower: float = 0.0, upper: float = 1.0) -> float:
    try:
        number = float(value if value is not None else default)
    except Exception:
        number = default
    return max(lower, min(upper, number))


def _horizon_weight_owner(horizon_class: Any, setup_type: Any = None) -> Optional[str]:
    horizon = str(horizon_class or "").lower()
    template = str(setup_type or "").lower()
    if horizon in {"short", "intraday"}:
        return "technical"
    if horizon in {"event_short", "event"} or "event" in template:
        return "commodity_news"
    if horizon in {"medium", "long"}:
        return "fundamental"
    return None


class DynamicWeightCalculator:
    """Calculate dynamic analyst weights for futures portfolio decisions."""

    STRONG_BASIS_WEIGHTS = {
        "fundamental": 0.70,
        "technical": 0.20,
        "commodity_news": 0.10,
    }

    NORMAL_MARKET_WEIGHTS = {
        "fundamental": 0.20,
        "technical": 0.50,
        "commodity_news": 0.30,
    }

    MARKET_STATE_ADJUSTMENTS = {
        "trending": {
            "fundamental": 0.9,
            "technical": 1.4,
            "commodity_news": 1.0,
        },
        "ranging": {
            "fundamental": 1.2,
            "technical": 0.8,
            "commodity_news": 1.0,
        },
        "reversal": {
            "fundamental": 1.0,
            "technical": 0.9,
            "commodity_news": 1.3,
        },
        "slight_down_trend|ranging": {
            "fundamental": 1.1,
            "technical": 0.9,
            "commodity_news": 1.0,
        },
        "slight_up_trend|ranging": {
            "fundamental": 1.1,
            "technical": 0.9,
            "commodity_news": 1.0,
        },
        "upward trending": {
            "fundamental": 0.9,
            "technical": 1.4,
            "commodity_news": 1.0,
        },
        "downward trending": {
            "fundamental": 0.9,
            "technical": 1.4,
            "commodity_news": 1.0,
        },
    }

    def __init__(self, config: Dict[str, Any]):
        """Initialize the dynamic-weight calculator from config."""
        self.config = config
        dynamic_config = config.get("dynamic_weights", {})

        self.enabled = dynamic_config.get("enabled", False)
        self.min_weight = dynamic_config.get("min_weight", 0.05)
        self.max_weight = dynamic_config.get("max_weight", 0.80)

    def calculate(
        self,
        basis_pct: float,
        market_state_analysis: Optional[Dict[str, Any]] = None,
        fundamental_trends_analysis: Optional[Dict[str, Any]] = None,
        fundamental_quality: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, float]:
        """Calculate the final dynamic weights."""
        abs_basis = abs(basis_pct)
        if abs_basis >= 10:
            weights = self.STRONG_BASIS_WEIGHTS.copy()
        else:
            weights = self.NORMAL_MARKET_WEIGHTS.copy()

        if not self.enabled:
            return weights

        if market_state_analysis:
            weights = self._apply_market_state_adjustment(weights, market_state_analysis)

        if fundamental_trends_analysis:
            weights = self._apply_fundamental_adjustment(
                weights, fundamental_trends_analysis, basis_pct
            )

        if fundamental_quality:
            weights = self._apply_fundamental_quality_adjustment(weights, fundamental_quality)

        weights = self._normalize_weights(weights)
        weights = self._apply_weight_constraints(weights)

        return weights

    def _apply_fundamental_quality_adjustment(
        self,
        weights: Dict[str, float],
        quality: Dict[str, Any],
    ) -> Dict[str, float]:
        """Discount fundamental weight when coverage is poor or indicators are stale."""
        quality_config = self.config.get("fundamental_quality_control", {}) or {}
        if not quality_config.get("enabled", True):
            return weights

        configured = int(quality.get("configured_indicator_count") or 0)
        if configured <= 0 or "fundamental" not in weights:
            return weights

        coverage_ratio = float(quality.get("coverage_ratio") or 0.0)
        missing_ratio = quality.get("missing_ratio")
        stale_ratio = quality.get("stale_ratio")
        if missing_ratio is None:
            missing_like = (
                int(quality.get("missing_like_count") or 0)
                or int(quality.get("missing_file_count") or 0)
                + int(quality.get("empty_frame_count") or 0)
                + int(quality.get("no_data_before_count") or 0)
            )
            missing_ratio = missing_like / configured
        if stale_ratio is None:
            stale_ratio = int(quality.get("stale_indicator_count") or 0) / configured

        penalty = 1.0
        if coverage_ratio < float(quality_config.get("min_coverage_ratio", 0.65)):
            penalty *= float(quality_config.get("low_coverage_multiplier", 0.70))

        if float(missing_ratio) >= float(quality_config.get("missing_ratio_severe", 0.40)):
            penalty *= float(quality_config.get("missing_severe_multiplier", 0.65))
        elif float(missing_ratio) >= float(quality_config.get("missing_ratio_warn", 0.20)):
            penalty *= float(quality_config.get("missing_warn_multiplier", 0.85))

        if float(stale_ratio) >= float(quality_config.get("stale_ratio_severe", 0.35)):
            penalty *= float(quality_config.get("stale_severe_multiplier", 0.65))
        elif float(stale_ratio) >= float(quality_config.get("stale_ratio_warn", 0.20)):
            penalty *= float(quality_config.get("stale_warn_multiplier", 0.85))

        penalty = max(float(quality_config.get("min_multiplier", 0.35)), min(1.0, penalty))
        if penalty >= 0.999:
            return weights

        adjusted = weights.copy()
        original = adjusted["fundamental"]
        adjusted["fundamental"] = original * penalty
        return adjusted

    def _get_market_state_adjustment(self, market_state: str) -> Dict[str, float]:
        """Map market-state text to a weight-adjustment profile."""
        if not market_state:
            return self.MARKET_STATE_ADJUSTMENTS["ranging"]

        if market_state in self.MARKET_STATE_ADJUSTMENTS:
            return self.MARKET_STATE_ADJUSTMENTS[market_state]

        state_lower = market_state.lower()

        if "|" in state_lower:
            if "revers" in state_lower:
                return self.MARKET_STATE_ADJUSTMENTS["reversal"]
            if "trend" in state_lower:
                return self.MARKET_STATE_ADJUSTMENTS["trending"]
            return self.MARKET_STATE_ADJUSTMENTS["ranging"]

        if "revers" in state_lower:
            return self.MARKET_STATE_ADJUSTMENTS["reversal"]

        if "trend" in state_lower:
            return self.MARKET_STATE_ADJUSTMENTS["trending"]

        if "range" in state_lower or "sideways" in state_lower:
            return self.MARKET_STATE_ADJUSTMENTS["ranging"]

        logger.warning("analyst_dynamic_weight_market_state_unrecognized")
        return self.MARKET_STATE_ADJUSTMENTS["ranging"]

    def _apply_market_state_adjustment(
        self,
        weights: Dict[str, float],
        market_state_analysis: Dict[str, Any],
    ) -> Dict[str, float]:
        """Adjust weights according to market-state analysis."""
        market_state = market_state_analysis.get("market_state", "ranging")
        confidence = market_state_analysis.get("confidence", 0.5)

        adjustment = self._get_market_state_adjustment(market_state)
        confidence_factor = 0.3 + (confidence * 0.7)

        adjusted_weights: Dict[str, float] = {}
        for analyst, base_weight in weights.items():
            adjust_factor = adjustment.get(analyst, 1.0)
            adjusted_weights[analyst] = base_weight * (
                1 + (adjust_factor - 1) * confidence_factor
            )

        return adjusted_weights

    def _apply_fundamental_adjustment(
        self,
        weights: Dict[str, float],
        fundamental_analysis: Dict[str, Any],
        basis_pct: float,
    ) -> Dict[str, float]:
        """Adjust weights according to structured fundamental-trend context."""
        del basis_pct  # Reserved for future fine-tuning.

        supply_demand = str(
            fundamental_analysis.get("supply_demand_balance", "balanced")
        )
        inventory_trend = str(
            fundamental_analysis.get("inventory_trend", "stable")
        )
        confidence = fundamental_analysis.get("confidence", 0.5)

        supply_demand_lower = supply_demand.lower()
        inventory_trend_lower = inventory_trend.lower()

        adjustments: Dict[str, float] = {}

        if any(
            token in supply_demand_lower
            for token in ["tight", "loose", "shortage", "surplus", "imbalanced"]
        ):
            adjustments["fundamental"] = 1.15

        if any(
            token in inventory_trend_lower
            for token in ["accelerat", "rapid", "sharp", "fast"]
        ):
            current_adj = adjustments.get("fundamental", 1.0)
            adjustments["fundamental"] = current_adj * 1.1

        if adjustments:
            confidence_factor = 0.5 + (confidence * 0.5)
            for analyst, adjust_factor in adjustments.items():
                if analyst in weights:
                    weights[analyst] = weights[analyst] * (
                        1 + (adjust_factor - 1) * confidence_factor
                    )

        return weights

    def _normalize_weights(self, weights: Dict[str, float]) -> Dict[str, float]:
        """Normalize weights so they sum to 1.0."""
        total = sum(weights.values())
        if total == 0:
            return {
                "fundamental": 0.33,
                "technical": 0.34,
                "commodity_news": 0.33,
            }
        return {k: v / total for k, v in weights.items()}

    def _apply_weight_constraints(self, weights: Dict[str, float]) -> Dict[str, float]:
        """Apply min/max constraints to avoid extreme weights."""
        constrained = {
            k: max(self.min_weight, min(self.max_weight, v))
            for k, v in weights.items()
        }
        return self._normalize_weights(constrained)

    @staticmethod
    def extract_basis_from_signal(fundamental_signal: Any) -> float:
        """Extract basis percentage, preferring structured metadata over text."""
        if not fundamental_signal:
            return 0.0

        metadata = getattr(fundamental_signal, "metadata", {}) or {}
        basis = metadata.get("basis") if isinstance(metadata, dict) else None
        if isinstance(basis, dict) and basis.get("latest_pct") is not None:
            try:
                return float(basis["latest_pct"])
            except (TypeError, ValueError):
                pass

        if not hasattr(fundamental_signal, "justification"):
            return 0.0

        justification = str(fundamental_signal.justification)

        patterns = [
            r"Basis[^%\n]*\(([-+]?\d+(?:\.\d+)?)%\)",
            r"basis[^%\n]*\(([-+]?\d+(?:\.\d+)?)%\)",
        ]
        for pattern in patterns:
            match = re.search(pattern, justification)
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    continue

        return 0.0

    @staticmethod
    def extract_quality_from_signal(fundamental_signal: Any) -> Dict[str, Any]:
        """Return structured fundamental quality diagnostics when available."""
        metadata = getattr(fundamental_signal, "metadata", {}) or {}
        if not isinstance(metadata, dict):
            return {}
        quality = metadata.get("fundamental_quality") or {}
        return quality if isinstance(quality, dict) else {}


def calibrate_weights_by_signal_history(
    db: Any,
    config_id: str,
    ticker: str,
    trading_date: Any,
    current_weights: Dict[str, float],
    lookback_days: int = 20,
    skew_threshold: float = 0.70,
    discount_factor: float = 0.70,
) -> Dict[str, float]:
    """Discount analysts whose recent non-neutral outputs are persistently one-sided."""
    if db is None or not config_id:
        return current_weights

    try:
        recent_signals = db.get_signal_history(
            config_id=config_id,
            ticker=ticker,
            trading_date=trading_date,
            lookback_days=lookback_days,
        )
    except Exception:
        logger.warning("analyst_signal_history_calibration_unavailable")
        recent_signals = []

    adjusted = current_weights.copy()
    if recent_signals:
        from collections import Counter

        analyst_aliases = {
            "fundamental": {"fundamental"},
            "technical": {"technical"},
            "commodity_news": {"commodity_news"},
        }
        for analyst, aliases in analyst_aliases.items():
            analyst_signals = [
                signal for signal in recent_signals
                if signal.get("analyst") in aliases and signal.get("signal") not in (None, "Neutral")
            ]
            if len(analyst_signals) < 5:
                continue

            direction_counts = Counter(signal.get("signal") for signal in analyst_signals)
            dominant_ratio = max(direction_counts.values()) / len(analyst_signals)
            if dominant_ratio <= skew_threshold or analyst not in adjusted:
                continue

            original_weight = adjusted[analyst]
            adjusted[analyst] = original_weight * discount_factor

    if hasattr(db, "get_analyst_performance"):
        try:
            performance_rows = db.get_analyst_performance(
                config_id=config_id,
                ticker=ticker,
                trading_date=trading_date,
                limit=30,
            )
        except Exception:
            logger.warning("analyst_performance_calibration_unavailable")
            performance_rows = []

        for row in performance_rows:
            analyst = row.get("analyst")
            if analyst not in adjusted:
                continue
            sample_count = int(row.get("sample_count") or 0)
            if sample_count < 2:
                continue
            hit_rate = float(row.get("hit_rate") or 0.0)
            net_pnl = float(row.get("net_pnl") or 0.0)
            confidence = max(0.0, min(1.0, float(row.get("confidence_score") or 0.0)))
            original_weight = adjusted[analyst]
            if net_pnl > 0 and hit_rate >= 0.55:
                multiplier = 1.0 + min(0.25, 0.20 * confidence)
                adjusted[analyst] = original_weight * multiplier
            elif net_pnl < 0 or hit_rate <= 0.45:
                multiplier = max(0.60, 1.0 - min(0.30, 0.25 * confidence))
                adjusted[analyst] = original_weight * multiplier

    if hasattr(db, "get_setup_type_performance"):
        try:
            template_rows = db.get_setup_type_performance(
                config_id=config_id,
                ticker=ticker,
                trading_date=trading_date,
                limit=30,
            )
        except Exception:
            logger.warning("analyst_signal_template_calibration_unavailable")
            template_rows = []

        for row in template_rows:
            owner = _horizon_weight_owner(row.get("horizon_class"), row.get("setup_type"))
            if owner not in adjusted:
                continue
            sample_count = int(row.get("sample_count") or 0)
            if sample_count < 2:
                continue
            win_rate = _bounded_float(row.get("win_rate"), default=0.0)
            net_pnl = float(row.get("net_pnl") or 0.0)
            confidence = _bounded_float(row.get("confidence_score"), default=0.0)
            original_weight = adjusted[owner]
            if net_pnl > 0 and win_rate >= 0.55:
                multiplier = 1.0 + min(0.18, 0.15 * confidence)
                adjusted[owner] = original_weight * multiplier
            elif net_pnl < 0 or win_rate <= 0.45:
                multiplier = max(0.65, 1.0 - min(0.25, 0.22 * max(confidence, 0.50)))
                adjusted[owner] = original_weight * multiplier

    policy_rows, policy_safety = retrieve_analyst_policy_calibration(
        db,
        config_id=config_id,
        ticker=ticker,
        trading_date=trading_date,
    )
    if policy_safety.get("error"):
        logger.warning("analyst_adaptive_policy_calibration_unavailable")

    for row in policy_rows:
        owner = _horizon_weight_owner(row.get("horizon_class"), row.get("setup_type"))
        if owner not in adjusted:
            continue
        confidence = _bounded_float(row.get("confidence_score"), default=0.0)
        action = str(row.get("policy_action") or "").lower()
        policy_multiplier = _bounded_float(row.get("multiplier"), default=1.0, lower=0.0, upper=2.0)
        original_weight = adjusted[owner]
        if action in {"cap", "block"}:
            multiplier = max(0.70, 1.0 - min(0.25, (1.0 - min(policy_multiplier, 1.0)) * 0.45 * max(confidence, 0.50)))
            adjusted[owner] = original_weight * multiplier
        elif action == "protect":
            multiplier = 1.0 + min(0.12, 0.10 * confidence)
            adjusted[owner] = original_weight * multiplier
        else:
            continue

    total = sum(adjusted.values())
    if total <= 0:
        return current_weights
    return {key: value / total for key, value in adjusted.items()}

