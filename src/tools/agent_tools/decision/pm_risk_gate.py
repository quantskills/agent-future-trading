from __future__ import annotations

"""Deterministic PM risk gate for futures recommendations.

This module is the portfolio manager's internal non-LLM risk gate. It reads
analyst signals, market confirmation, and recent attribution feedback while PM
is still building a draft exposure, then decides whether that draft should be
allowed, reduced, blocked, or held before the PM signs final_action_contract.
It is not the independent Auditor artifact boundary; it does not select analyst
agents, calculate final lots, sign contracts, or place orders.
"""

from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field
from tools.agent_tools.decision.audit_decision_types import hard_block_or_reduce_only, normalize_audit_decision
from tools.agent_tools.decision.audit_explainer import build_audit_payload, build_audit_state_key
from tools.agent_tools.decision.pm_hard_risk_rules import has_hard_block_reason
from tools.agent_tools.decision.pm_reason_effects import reason_effect_summary
from tools.agent_tools.decision.pm_soft_risk_rules import fallback_business_quality_score


class PMRiskGateInput(BaseModel):
    """Input payload for the deterministic PM risk gate."""

    ticker: str
    trading_date: Any = None
    config_id: str = ""
    analyst_signals: List[Dict[str, Any]] = Field(default_factory=list)
    signal_combo: List[str] = Field(default_factory=list)
    raw_long_score: Dict[str, Any] = Field(default_factory=dict)
    raw_short_score: Dict[str, Any] = Field(default_factory=dict)
    raw_target_side: str = "flat"
    raw_position_ratio: float = 0.0
    current_position_ratio: float = 0.0
    signal_strength: float = 0.0
    market_confirmation: Dict[str, Any] = Field(default_factory=dict)
    fundamental_quality: Dict[str, Any] = Field(default_factory=dict)
    account_drawdown_state: Dict[str, Any] = Field(default_factory=dict)
    recent_ticker_side_performance: Dict[str, Any] = Field(default_factory=dict)
    recent_conditional_performance: Dict[str, Any] = Field(default_factory=dict)
    provisional_policy_state: List[Dict[str, Any]] = Field(default_factory=list)
    risk_level: str = ""
    full_config: Dict[str, Any] = Field(default_factory=dict)


class PMRiskGateOutput(BaseModel):
    """PM risk gate verdict consumed only inside portfolio_manager."""

    decision: str = Field(default="allow", description="allow / scale_down / probe_only / reduce_only / block")
    target_side: str = Field(default="flat", description="long / short / flat")
    position_ratio_multiplier: float = Field(default=1.0)
    confidence_multiplier: float = Field(default=1.0)
    cap_multiplier: float = Field(default=1.0)
    reasons: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    audit_payload: Dict[str, Any] = Field(default_factory=dict)
    policy_version: str = Field(default="deterministic_v1")
    learning_mode: str = Field(default="audit_only")


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


def _signal_combo_tuple(signal_combo: Sequence[Any]) -> tuple[str, str, str]:
    values = [str(item) for item in list(signal_combo or [])]
    while len(values) < 3:
        values.append("Neutral")
    return (values[0], values[1], values[2])


def _state_bucket(value: Any, default: str = "unknown") -> str:
    if isinstance(value, dict):
        for key in ("market_state", "regime", "state", "trend_state"):
            if value.get(key):
                return str(value.get(key))
    text = str(value or "").strip()
    return text if text else default


def _confirmation_bucket(market_confirmation: Dict[str, Any]) -> str:
    score = _safe_float(market_confirmation.get("confirmation_score"), 0.0)
    if score >= 0.70:
        return "strong"
    if score >= 0.55:
        return "medium"
    if score > 0:
        return "weak"
    return "none"


def _dedupe(values: Sequence[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        text = str(value)
        if text and text not in result:
            result.append(text)
    return result


def _normalize_agent_name(agent_name: Any) -> str:
    text = str(agent_name or "")
    return "commodity_news" if text == "company_news" else text


def _signal_text(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value or "Neutral")


def _signal_metadata(signal_payload: Dict[str, Any]) -> Dict[str, Any]:
    metadata = signal_payload.get("metadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def _fallback_business_quality_score(tradeability: str, confidence: float) -> float:
    """Estimate score for legacy analyst payloads that predate structured BQ."""
    return fallback_business_quality_score(tradeability, confidence)


def _side_rule(config: Dict[str, Any], ticker: Any, side: str) -> Dict[str, Any]:
    """Return ticker-side rule config while accepting compact YAML shapes."""
    if not isinstance(config, dict):
        return {}
    ticker_key = str(ticker or "").upper()
    ticker_rules = config.get(ticker_key) or config.get(str(ticker or "")) or {}
    if isinstance(ticker_rules, list):
        return {"enabled": side in [str(item).lower() for item in ticker_rules]}
    if not isinstance(ticker_rules, dict):
        return {}
    side_rule = ticker_rules.get(side) or ticker_rules.get(side.lower()) or ticker_rules.get(side.upper()) or {}
    if side_rule is True:
        return {"enabled": True}
    return side_rule if isinstance(side_rule, dict) else {}


def _context_from_metadata(metadata: Dict[str, Any], agent_name: str) -> Dict[str, Any]:
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


def _target_signal_for_side(target_side: str) -> str:
    if target_side == "long":
        return "Bullish"
    if target_side == "short":
        return "Bearish"
    return "Neutral"


def _is_opposite_signal(signal: str, target_signal: str) -> bool:
    return (
        (target_signal == "Bullish" and signal == "Bearish")
        or (target_signal == "Bearish" and signal == "Bullish")
    )


class AttributionFeedbackCalibrator:
    """Rule-based attribution feedback used by the trade RiskGate."""

    def __init__(self, full_config: Dict[str, Any]):
        self.full_config = full_config or {}
        self.risk_gate_config = self.full_config.get("pm_risk_gate") or {}
        self.feedback_config = self.risk_gate_config.get("attribution_feedback", {}) or {}
        self.cold_start_config = self.risk_gate_config.get("cold_start", {}) or {}
        self.quality_config = self.risk_gate_config.get("quality_gate", {}) or {}
        self.trade_config = self.full_config.get("trade_frequency_control", {}) or {}
        self.market_config = self.full_config.get("market_confirmation", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.feedback_config.get("enabled", True))

    def calibrate(self, payload: PMRiskGateInput) -> Dict[str, Any]:
        target_side = _target_side_from_ratio(payload.raw_position_ratio)
        diagnostics: Dict[str, Any] = {
            "ticker_side_performance": payload.recent_ticker_side_performance or {},
            "conditional_performance": payload.recent_conditional_performance or {},
        }
        reasons: List[str] = []
        notes: List[str] = []
        multiplier = 1.0
        block = False

        if not self.enabled or target_side not in {"long", "short"}:
            return {
                "block": False,
                "multiplier": multiplier,
                "reasons": reasons,
                "notes": notes,
                "diagnostics": diagnostics,
            }

        protected_rule = self._protected_side_rule(payload, target_side)
        side_override = ((self.trade_config.get("side_overrides") or {}).get(payload.ticker) or {})
        override_key = f"{target_side}_cap_multiplier"
        if protected_rule.get("rejected"):
            diagnostics["protected_memory_rejected"] = protected_rule
            reasons.append("protected_memory_evidence_rejected")
            notes.append(
                f"{payload.ticker} {target_side} protected memory ignored: "
                f"sample_count={protected_rule.get('sample_count')}<"
                f"{protected_rule.get('min_sample_count')} or "
                f"win_rate={_safe_float(protected_rule.get('win_rate')):.2%}<"
                f"{_safe_float(protected_rule.get('min_win_rate')):.2%} or "
                f"net_pnl={_safe_float(protected_rule.get('net_pnl')):.0f}<"
                f"{_safe_float(protected_rule.get('min_net_pnl')):.0f}"
            )
        protected_rule_active = bool(protected_rule) and not protected_rule.get("rejected")
        if override_key in side_override and not protected_rule_active:
            side_multiplier = max(0.0, _safe_float(side_override.get(override_key), 1.0))
            multiplier = min(multiplier, side_multiplier)
            reasons.append("static_side_cap")
            notes.append(f"{payload.ticker} {target_side} static attribution cap multiplier={side_multiplier:.2f}")
        elif override_key in side_override and protected_rule_active:
            diagnostics["protected_ticker_side"] = {
                "ticker": payload.ticker,
                "side": target_side,
                "skipped_static_cap": side_override.get(override_key),
            }
            notes.append(f"{payload.ticker} {target_side} protected side skipped static attribution cap")

        side_result = self._evaluate_performance(
            performance=payload.recent_ticker_side_performance or {},
            weak_reason="weak_ticker_side_history",
            severe_reason="side_performance_block",
        )
        if side_result["block"]:
            block = True
        multiplier = min(multiplier, side_result["multiplier"])
        reasons.extend(side_result["reasons"])
        notes.extend(side_result["notes"])

        combo_result = self._evaluate_conditional_combo(payload)
        if combo_result["block"]:
            block = True
        multiplier = min(multiplier, combo_result["multiplier"])
        reasons.extend(combo_result["reasons"])
        notes.extend(combo_result["notes"])
        diagnostics.update(combo_result["diagnostics"])

        cold_result = self._evaluate_cold_start(payload)
        if cold_result.get("block"):
            block = True
        multiplier = min(multiplier, cold_result["multiplier"])
        reasons.extend(cold_result["reasons"])
        notes.extend(cold_result["notes"])

        return {
            "block": block,
            "multiplier": 0.0 if block else multiplier,
            "reasons": _dedupe(reasons),
            "notes": notes,
            "diagnostics": diagnostics,
        }

    def _protected_side_rule(self, payload: PMRiskGateInput, target_side: str) -> Dict[str, Any]:
        if target_side not in {"long", "short"}:
            return {}
        rule = _side_rule(self.quality_config.get("protected_ticker_sides") or {}, payload.ticker, target_side)
        if not rule:
            return {}
        performance = payload.recent_ticker_side_performance or {}
        min_samples = _safe_int(rule.get("min_samples_to_check"), 3)
        total_trades = _safe_int(performance.get("total_trades"), 0)
        min_total_pnl = rule.get("min_total_pnl_when_sampled")
        if min_total_pnl is not None and total_trades >= min_samples:
            total_pnl = _safe_float(performance.get("total_pnl"), 0.0)
            if total_pnl < _safe_float(min_total_pnl):
                return {}
        return rule

    def _evaluate_performance(
        self,
        *,
        performance: Dict[str, Any],
        weak_reason: str,
        severe_reason: str,
    ) -> Dict[str, Any]:
        total_trades = _safe_int(performance.get("total_trades"), 0)
        win_rate = _safe_float(performance.get("win_rate"), 0.0)
        total_pnl = _safe_float(performance.get("total_pnl"), 0.0)
        min_soft = _safe_int(self.feedback_config.get("min_samples_soft"), 5)
        min_hard = _safe_int(self.feedback_config.get("min_samples_hard"), 10)
        weak_win = _safe_float(self.feedback_config.get("weak_win_rate_below"), 0.40)
        severe_win = _safe_float(self.feedback_config.get("severe_win_rate_below"), 0.30)
        weak_pnl = self.feedback_config.get("weak_total_pnl_below")
        severe_pnl = self.feedback_config.get("severe_total_pnl_below")
        weak_multiplier = max(0.0, _safe_float(self.trade_config.get("weak_cap_multiplier"), 0.50))

        reasons: List[str] = []
        notes: List[str] = []
        multiplier = 1.0
        block = False

        if total_trades >= min_hard:
            severe_pnl_hit = severe_pnl is not None and total_pnl <= _safe_float(severe_pnl)
            severe_win_hit = win_rate <= severe_win
            if severe_pnl_hit or severe_win_hit:
                block = bool(self.feedback_config.get("block_severe_negative_combo", True))
                multiplier = 0.0 if block else weak_multiplier
                reasons.append(severe_reason)
                notes.append(
                    f"severe recent performance: trades={total_trades}, "
                    f"win_rate={win_rate:.2%}, total_pnl={total_pnl:.0f}"
                )
                return {
                    "block": block,
                    "multiplier": multiplier,
                    "reasons": reasons,
                    "notes": notes,
                }

        if total_trades >= min_soft:
            weak_pnl_hit = weak_pnl is not None and total_pnl <= _safe_float(weak_pnl)
            weak_win_hit = win_rate <= weak_win
            if weak_pnl_hit or weak_win_hit:
                multiplier = weak_multiplier
                reasons.append(weak_reason)
                notes.append(
                    f"weak recent performance: trades={total_trades}, "
                    f"win_rate={win_rate:.2%}, total_pnl={total_pnl:.0f}, "
                    f"multiplier={weak_multiplier:.2f}"
                )

        return {
            "block": block,
            "multiplier": multiplier,
            "reasons": reasons,
            "notes": notes,
        }

    def _evaluate_conditional_combo(self, payload: PMRiskGateInput) -> Dict[str, Any]:
        target_side = _target_side_from_ratio(payload.raw_position_ratio)
        performance = payload.recent_conditional_performance or {}
        total_trades = _safe_int(performance.get("total_trades"), 0)
        confirmation_score = _safe_float((payload.market_confirmation or {}).get("confirmation_score"), 0.0)
        required_score = _safe_float(
            self.feedback_config.get(
                "weak_combo_requires_confirmation_score",
                self.market_config.get("min_confirmation_score_for_weak_combo", 0.60),
            ),
            0.60,
        )
        current_has_position = abs(payload.current_position_ratio) > 1e-12
        result = {
            "block": False,
            "multiplier": 1.0,
            "reasons": [],
            "notes": [],
            "diagnostics": {
                "conditional_confirmation_required": required_score,
                "conditional_sample_count": total_trades,
            },
        }

        perf_result = self._evaluate_performance(
            performance=performance,
            weak_reason="weak_conditional_combo",
            severe_reason="conditional_performance_block",
        )
        result["multiplier"] = min(result["multiplier"], perf_result["multiplier"])
        result["reasons"].extend(perf_result["reasons"])
        result["notes"].extend(perf_result["notes"])
        if perf_result["block"]:
            result["block"] = True

        weak_combos = [tuple(item) for item in (self.trade_config.get("weak_signal_combos") or [])]
        signal_combo = _signal_combo_tuple(payload.signal_combo)
        if signal_combo in weak_combos:
            if not (payload.market_confirmation or {}).get("features"):
                result["diagnostics"]["weak_signal_combo_skip"] = "no_market_confirmation_features"
                return result
            min_confirmations = _safe_int(self.market_config.get("min_confirmations_for_new_entry"), 2)
            confirmations = (payload.market_confirmation or {}).get("confirmations") or []
            if len(confirmations) < min_confirmations or confirmation_score < required_score:
                result["reasons"].append("weak_signal_combo")
                result["notes"].append(
                    f"weak analyst combo {signal_combo} requires confirmations>={min_confirmations} "
                    f"and score>={required_score:.2f}; got confirmations={len(confirmations)}, "
                    f"score={confirmation_score:.2f}"
                )
                protected_rule = self._protected_side_rule(payload, target_side)
                protected_score = _safe_float(protected_rule.get("min_confirmation_score"), 0.50)
                if protected_rule.get("rejected"):
                    result["diagnostics"]["protected_memory_rejected"] = protected_rule
                    result["reasons"].append("protected_memory_evidence_rejected")
                if protected_rule and not protected_rule.get("rejected") and confirmation_score >= protected_score:
                    protected_multiplier = max(
                        0.0,
                        _safe_float(
                            protected_rule.get(
                                "weak_combo_multiplier",
                                self.trade_config.get("weak_cap_multiplier", 0.50),
                            ),
                            0.50,
                        ),
                    )
                    result["reasons"].append("protected_ticker_side_weak_combo")
                    result["multiplier"] = min(result["multiplier"], protected_multiplier)
                    result["diagnostics"]["protected_ticker_side"] = {
                        "ticker": payload.ticker,
                        "side": target_side,
                        "min_confirmation_score": protected_score,
                    }
                    result["notes"].append(
                        f"protected {payload.ticker} {target_side} weak combo reduced instead of blocked: "
                        f"confirmation_score={confirmation_score:.2f}>={protected_score:.2f}, "
                        f"multiplier={protected_multiplier:.2f}"
                    )
                elif current_has_position:
                    result["multiplier"] = min(
                        result["multiplier"],
                        max(0.0, _safe_float(self.trade_config.get("weak_cap_multiplier"), 0.50)),
                    )
                else:
                    result["block"] = True
                    result["multiplier"] = 0.0

        if (
            "weak_conditional_combo" in result["reasons"]
            and confirmation_score < required_score
            and not current_has_position
        ):
            result["block"] = True
            result["multiplier"] = 0.0
            result["notes"].append(
                f"weak conditional combo blocked because confirmation_score={confirmation_score:.2f} "
                f"is below {required_score:.2f}"
            )

        result["reasons"] = _dedupe(result["reasons"])
        return result

    def _evaluate_cold_start(self, payload: PMRiskGateInput) -> Dict[str, Any]:
        policy = str(self.cold_start_config.get("policy", "small_cap"))
        if policy != "small_cap":
            return {"block": False, "multiplier": 1.0, "reasons": [], "notes": []}

        min_soft = _safe_int(self.feedback_config.get("min_samples_soft"), 5)
        side_samples = _safe_int((payload.recent_ticker_side_performance or {}).get("total_trades"), 0)
        combo_samples = _safe_int((payload.recent_conditional_performance or {}).get("total_trades"), 0)
        if side_samples >= min_soft or combo_samples >= min_soft:
            return {"block": False, "multiplier": 1.0, "reasons": [], "notes": []}

        signal_combo = _signal_combo_tuple(payload.signal_combo)
        weak_combos = [tuple(item) for item in (self.trade_config.get("weak_signal_combos") or [])]
        confirmation_score = _safe_float((payload.market_confirmation or {}).get("confirmation_score"), 0.0)
        target_side = _target_side_from_ratio(payload.raw_position_ratio)
        required_score = _safe_float(
            self.cold_start_config.get(
                "block_conflict_confirmation_below",
                self.market_config.get("min_confirmation_score_for_weak_combo", 0.60),
            ),
            0.60,
        )
        current_has_position = abs(payload.current_position_ratio) > 1e-12

        if (
            not current_has_position
            and bool(self.cold_start_config.get("block_weak_combo_new_entries", True))
            and signal_combo in weak_combos
            and confirmation_score < required_score
        ):
            protected_rule = self._protected_side_rule(payload, target_side)
            protected_score = _safe_float(protected_rule.get("min_confirmation_score"), 0.50)
            if protected_rule.get("rejected"):
                return {
                    "block": True,
                    "multiplier": 0.0,
                    "reasons": ["cold_start_weak_combo_block", "protected_memory_evidence_rejected"],
                    "notes": [
                        f"cold start blocked weak combo {signal_combo}: protected memory evidence "
                        f"is too shallow for override; confirmation_score={confirmation_score:.2f}"
                    ],
                }
            if protected_rule and confirmation_score >= protected_score:
                protected_multiplier = max(
                    0.0,
                    _safe_float(
                        protected_rule.get(
                            "cold_start_multiplier",
                            self.cold_start_config.get("max_position_ratio_multiplier", 0.50),
                        ),
                        0.50,
                    ),
                )
                return {
                    "block": False,
                    "multiplier": protected_multiplier,
                    "reasons": ["protected_ticker_side_cold_start"],
                    "notes": [
                        f"protected {payload.ticker} {target_side} cold-start weak combo reduced instead of blocked: "
                        f"confirmation_score={confirmation_score:.2f}>={protected_score:.2f}, "
                        f"multiplier={protected_multiplier:.2f}"
                    ],
                }
            return {
                "block": True,
                "multiplier": 0.0,
                "reasons": ["cold_start_weak_combo_block"],
                "notes": [
                    f"cold start blocked weak combo {signal_combo}: "
                    f"confirmation_score={confirmation_score:.2f}<{required_score:.2f}"
                ],
            }

        multiplier = max(0.0, _safe_float(self.cold_start_config.get("max_position_ratio_multiplier"), 0.50))
        return {
            "block": False,
            "multiplier": multiplier,
            "reasons": ["cold_start_small_cap"],
            "notes": [
                f"cold start: side_samples={side_samples}, combo_samples={combo_samples}, "
                f"multiplier={multiplier:.2f}"
            ],
        }


class PMRiskGate:
    """Deterministic PM risk gate.

    The historical class name is kept as a compatibility alias for tests and
    saved snapshots. Production PM code imports PMRiskGate below.
    """

    def __init__(self, full_config: Optional[Dict[str, Any]] = None):
        self.full_config = full_config or {}
        self.risk_gate_config = self.full_config.get("pm_risk_gate") or {}
        self.market_config = self.full_config.get("market_confirmation", {}) or {}
        self.trade_config = self.full_config.get("trade_frequency_control", {}) or {}
        self.quality_config = self.risk_gate_config.get("quality_gate", {}) or {}
        self.calibrator = AttributionFeedbackCalibrator(self.full_config)

    @property
    def enabled(self) -> bool:
        return bool(self.risk_gate_config.get("enabled", False))

    def plan(self, payload: PMRiskGateInput) -> PMRiskGateOutput:
        target_side = _target_side_from_ratio(payload.raw_position_ratio)
        policy_version = str(self.risk_gate_config.get("policy_version", "deterministic_v1"))
        learning_mode = str(self.risk_gate_config.get("learning_mode", "audit_only"))
        reasons: List[str] = []
        notes: List[str] = []
        diagnostics: Dict[str, Any] = {
            "raw_position_ratio": float(payload.raw_position_ratio or 0.0),
            "current_position_ratio": float(payload.current_position_ratio or 0.0),
            "signal_strength": float(payload.signal_strength or 0.0),
            "signal_combo": list(_signal_combo_tuple(payload.signal_combo)),
            "provisional_policy_state": payload.provisional_policy_state or [],
            "research_memory_boundary": "RiskGate_does_not_consume_research_records",
        }

        if not self.enabled:
            return self._output(
                decision="allow",
                target_side=target_side,
                multiplier=1.0,
                reasons=["pm_risk_gate_disabled"],
                notes=["trade RiskGate disabled; legacy controls may apply"],
                diagnostics=diagnostics,
                payload=payload,
                policy_version=policy_version,
                learning_mode=learning_mode,
            )

        if target_side not in {"long", "short"}:
            return self._output(
                decision="hold",
                target_side=target_side,
                multiplier=0.0,
                reasons=["flat_target"],
                notes=["RiskGate received flat target; no new exposure required"],
                diagnostics=diagnostics,
                payload=payload,
                policy_version=policy_version,
                learning_mode=learning_mode,
            )

        if not _is_new_or_increasing_exposure(payload.raw_position_ratio, payload.current_position_ratio):
            return self._output(
                decision="allow",
                target_side=target_side,
                multiplier=1.0,
                reasons=["not_new_or_increasing_exposure"],
                notes=["RiskGate skipped blocking because target is not new or increasing exposure"],
                diagnostics=diagnostics,
                payload=payload,
                policy_version=policy_version,
                learning_mode=learning_mode,
            )

        quality_result = self._evaluate_analyst_signal_quality(payload)
        market_result = self._evaluate_market_confirmation(payload)
        attribution_result = self.calibrator.calibrate(payload)
        provisional_policy_result = self._evaluate_provisional_policy(payload, target_side)

        reasons.extend(quality_result["reasons"])
        reasons.extend(market_result["reasons"])
        reasons.extend(attribution_result["reasons"])
        reasons.extend(provisional_policy_result["reasons"])
        notes.extend(quality_result["notes"])
        notes.extend(market_result["notes"])
        notes.extend(attribution_result["notes"])
        notes.extend(provisional_policy_result["notes"])
        diagnostics.update(quality_result["diagnostics"])
        diagnostics.update(market_result["diagnostics"])
        diagnostics.update(attribution_result["diagnostics"])
        diagnostics.update(provisional_policy_result["diagnostics"])

        if (
            quality_result["block"]
            or market_result["block"]
            or attribution_result["block"]
            or provisional_policy_result["block"]
        ):
            hard_block = has_hard_block_reason(reasons, softened_reasons=set())
            if hard_block:
                multiplier = 0.0
                decision = hard_block_or_reduce_only(
                    target_ratio=payload.raw_position_ratio,
                    current_ratio=payload.current_position_ratio,
                )
            else:
                multiplier = min(
                    max(0.0, _safe_float((self.full_config.get("analyst_business_quality") or {}).get("probe_multiplier"), 0.25)),
                    max(0.0, _safe_float((self.risk_gate_config.get("cold_start") or {}).get("max_position_ratio_multiplier"), 0.50)),
                )
                decision = "probe_only"
                reasons.append("soft_block_converted_to_probe_only")
        else:
            multiplier = min(
                quality_result["multiplier"],
                market_result["multiplier"],
                attribution_result["multiplier"],
                provisional_policy_result["multiplier"],
            )
            if multiplier < 0.999999:
                decision = "probe_only" if multiplier <= 0.35 or "business_quality_probe_only" in reasons else "scale_down"
            else:
                decision = "allow"

        diagnostics["reason_effects"] = reason_effect_summary(reasons)
        return self._output(
            decision=decision,
            target_side=target_side,
            multiplier=multiplier,
            reasons=_dedupe(reasons) or ["pm_risk_gate_allow"],
            notes=notes,
            diagnostics=diagnostics,
            payload=payload,
            policy_version=policy_version,
            learning_mode=learning_mode,
        )

    def _evaluate_provisional_policy(self, payload: PMRiskGateInput, target_side: str) -> Dict[str, Any]:
        rows = payload.provisional_policy_state or []
        if not rows:
            return {"block": False, "multiplier": 1.0, "reasons": [], "notes": [], "diagnostics": {}}
        block = False
        multiplier = 1.0
        reasons: List[str] = []
        notes: List[str] = []
        applied: List[Dict[str, Any]] = []
        for row in rows:
            action = str(row.get("policy_action") or "").lower()
            row_multiplier = max(0.0, _safe_float(row.get("multiplier"), 1.0))
            if action in {"block", "weak_block"}:
                block = True
                multiplier = 0.0
                reasons.append("provisional_policy_block")
            elif action in {"probe_only", "cap", "reduce"}:
                multiplier = min(multiplier, row_multiplier)
                reasons.append("provisional_policy_probe_only" if action == "probe_only" else "provisional_policy_cap")
            else:
                continue
            applied.append(row)
            notes.append(
                f"provisional policy {action} for {payload.ticker} {target_side}: "
                f"multiplier={row_multiplier:.2f}, reason={row.get('reason') or 'early risk sentinel'}"
            )
        return {
            "block": block,
            "multiplier": 0.0 if block else multiplier,
            "reasons": _dedupe(reasons),
            "notes": notes,
            "diagnostics": {"provisional_policy_applied": applied},
        }

    def _evaluate_analyst_signal_quality(self, payload: PMRiskGateInput) -> Dict[str, Any]:
        reasons: List[str] = []
        notes: List[str] = []
        diagnostics: Dict[str, Any] = {}
        multiplier = 1.0
        block = False

        if not self.quality_config.get("enabled", True) or not payload.analyst_signals:
            return {
                "block": False,
                "multiplier": multiplier,
                "reasons": reasons,
                "notes": notes,
                "diagnostics": diagnostics,
            }

        target_side = _target_side_from_ratio(payload.raw_position_ratio)
        target_signal = _target_signal_for_side(target_side)
        confirmation_score = _safe_float((payload.market_confirmation or {}).get("confirmation_score"), 0.0)
        confirmations = (payload.market_confirmation or {}).get("confirmations") or []
        conflicts = (payload.market_confirmation or {}).get("conflicts") or []

        analyst_quality: Dict[str, Dict[str, Any]] = {}
        supporters: List[str] = []
        high_quality_supporters: List[str] = []
        low_quality_supporters: List[str] = []
        qualified_supporters: List[str] = []
        opposers: List[str] = []
        low_tradeability_count = 0
        min_qualified_confidence = _safe_float(self.quality_config.get("qualified_support_min_confidence"), 0.45)

        for item in payload.analyst_signals:
            if not isinstance(item, dict):
                continue
            agent_name = _normalize_agent_name(item.get("agent_name"))
            if agent_name not in {"technical", "fundamental", "commodity_news"}:
                continue
            signal = _signal_text(item.get("signal"))
            metadata = _signal_metadata(item)
            context = _context_from_metadata(metadata, agent_name)
            tradeability = str(metadata.get("tradeability") or context.get("tradeability") or "unknown").lower()
            risk_flags = metadata.get("risk_flags") or context.get("risk_flags") or []
            risk_flags = [str(flag) for flag in risk_flags] if isinstance(risk_flags, list) else []
            confidence = _safe_float(item.get("confidence"), 0.0)
            raw_business_quality = (
                item.get("business_quality_score")
                or (metadata.get("business_quality") or {}).get("score")
                or metadata.get("business_quality_score")
            )
            business_quality = (
                _safe_float(raw_business_quality, 0.0)
                if raw_business_quality is not None
                else _fallback_business_quality_score(tradeability, confidence)
            )
            analyst_quality[agent_name] = {
                "signal": signal,
                "tradeability": tradeability,
                "risk_flags": risk_flags,
                "confidence": confidence,
                "business_quality_score": business_quality,
                "setup_type": item.get("setup_type") or metadata.get("setup_type") or "unknown",
                "primary_business_driver": item.get("primary_business_driver") or (metadata.get("business_quality") or {}).get("primary_business_driver"),
                "freshness_score": _safe_float(metadata.get("freshness_score") or context.get("freshness_score"), 0.0),
                "relevance_score": _safe_float(metadata.get("relevance_score") or context.get("relevance_score"), 0.0),
            }

            if tradeability == "low":
                low_tradeability_count += 1
            if signal == target_signal:
                supporters.append(agent_name)
                if tradeability == "high":
                    high_quality_supporters.append(agent_name)
                if (
                    tradeability in {"high", "medium"}
                    and confidence >= min_qualified_confidence
                    and business_quality >= _safe_float(self.full_config.get("analyst_business_quality", {}).get("min_score_for_probe"), 0.45)
                ):
                    qualified_supporters.append(agent_name)
                if tradeability == "low":
                    low_quality_supporters.append(agent_name)
            elif _is_opposite_signal(signal, target_signal):
                opposers.append(agent_name)

        diagnostics["analyst_quality"] = analyst_quality
        diagnostics["target_support"] = {
            "target_side": target_side,
            "target_signal": target_signal,
            "supporters": supporters,
            "high_quality_supporters": high_quality_supporters,
            "qualified_supporters": qualified_supporters,
            "low_quality_supporters": low_quality_supporters,
            "opposers": opposers,
            "confirmation_score": confirmation_score,
            "confirmations": confirmations,
            "conflicts": conflicts,
        }

        min_supporters = _safe_int(self.quality_config.get("min_supporting_analysts"), 2)
        block_low_count = _safe_int(self.quality_config.get("block_low_tradeability_count"), 2)
        conflict_score = _safe_float(self.quality_config.get("conflict_block_confirmation_below"), 0.65)
        medium_multiplier = max(0.0, _safe_float(self.quality_config.get("medium_quality_multiplier"), 0.50))
        business_cfg = self.full_config.get("analyst_business_quality", {}) or {}
        business_gate_enabled = bool(business_cfg.get("enabled", False))
        min_business_probe = _safe_float(business_cfg.get("min_score_for_probe"), 0.45)
        min_business_deploy = _safe_float(business_cfg.get("min_score_for_deployable"), 0.60)
        probe_multiplier = max(0.0, _safe_float(business_cfg.get("probe_multiplier"), 0.25))
        best_business_support = max(
            (
                _safe_float(analyst_quality.get(agent, {}).get("business_quality_score"), 0.0)
                for agent in supporters
            ),
            default=0.0,
        )
        diagnostics["target_support"]["best_business_quality_score"] = best_business_support
        allow_single_high_quality_probe = bool(self.quality_config.get("allow_single_high_quality_probe", True))
        single_high_quality_multiplier = max(
            0.0,
            _safe_float(self.quality_config.get("single_high_quality_multiplier"), 0.35),
        )
        single_min_business_quality = _safe_float(
            self.quality_config.get("single_high_quality_min_business_quality"),
            0.60,
        )
        single_min_confidence = _safe_float(
            self.quality_config.get("single_high_quality_min_confidence"),
            0.55,
        )
        single_min_confirmation = _safe_float(
            self.quality_config.get("single_high_quality_min_confirmation_score"),
            0.45,
        )
        single_support_confidence = max(
            (
                _safe_float(analyst_quality.get(agent, {}).get("confidence"), 0.0)
                for agent in supporters
            ),
            default=0.0,
        )
        single_high_quality_probe_allowed = (
            allow_single_high_quality_probe
            and len(supporters) == 1
            and not low_quality_supporters
            and best_business_support >= single_min_business_quality
            and single_support_confidence >= single_min_confidence
            and confirmation_score >= single_min_confirmation
        )
        diagnostics["target_support"]["single_high_quality_probe_allowed"] = single_high_quality_probe_allowed

        if low_tradeability_count >= block_low_count:
            multiplier = min(multiplier, max(0.0, _safe_float(self.quality_config.get("low_tradeability_cap_multiplier"), 0.35)))
            reasons.append("analyst_quality_low_tradeability")
            notes.append(
                f"probe-capped {target_side}: {low_tradeability_count} analyst signals have low tradeability"
            )

        if business_gate_enabled and supporters and best_business_support < min_business_probe:
            multiplier = min(multiplier, probe_multiplier)
            reasons.append("business_quality_below_probe")
            notes.append(
                f"probe-capped {target_side}: best business_quality_score={best_business_support:.2f} "
                f"is below probe threshold {min_business_probe:.2f}"
            )
        elif business_gate_enabled and supporters and best_business_support < min_business_deploy:
            multiplier = min(multiplier, probe_multiplier)
            reasons.append("business_quality_probe_only")
            notes.append(
                f"probe-only {target_side}: best business_quality_score={best_business_support:.2f} "
                f"is below deployable threshold {min_business_deploy:.2f}"
            )

        if not supporters:
            reasons.append("no_analyst_support_for_target")
            if confirmation_score < _safe_float(self.quality_config.get("no_support_block_confirmation_below"), 0.70):
                multiplier = min(multiplier, medium_multiplier)
                notes.append(
                    f"reduced {target_side}: no analyst supports target and confirmation_score={confirmation_score:.2f}"
                )
            else:
                multiplier = min(multiplier, medium_multiplier)
                notes.append(
                    f"reduced {target_side}: no analyst supports target despite strong market confirmation"
                )
        elif len(supporters) < min_supporters:
            reasons.append("insufficient_quality_support")
            if single_high_quality_probe_allowed:
                multiplier = min(multiplier, single_high_quality_multiplier)
                reasons.append("single_high_quality_probe_only")
                notes.append(
                    f"probe-only {target_side}: one high-quality supporter={supporters}, "
                    f"business_quality={best_business_support:.2f}, confidence={single_support_confidence:.2f}, "
                    f"confirmation_score={confirmation_score:.2f}; multiplier={single_high_quality_multiplier:.2f}"
                )
            elif low_quality_supporters:
                multiplier = min(multiplier, single_high_quality_multiplier)
                notes.append(
                    f"probe-capped {target_side}: only {supporters} support target and low-quality supporters={low_quality_supporters}"
                )
            elif opposers and confirmation_score < conflict_score:
                multiplier = min(multiplier, medium_multiplier)
                notes.append(
                    f"reduced {target_side}: single-sided support={supporters}, opposers={opposers}, "
                    f"confirmation_score={confirmation_score:.2f}<{conflict_score:.2f}"
                )
            else:
                multiplier = min(multiplier, medium_multiplier)
                notes.append(
                    f"reduced {target_side}: only {supporters} support target; multiplier={medium_multiplier:.2f}"
                )

        if opposers and supporters and confirmation_score < conflict_score:
            reasons.append("analyst_signal_conflict")
            multiplier = min(multiplier, medium_multiplier)
            notes.append(
                f"reduced {target_side}: analyst conflict supporters={supporters}, opposers={opposers}, "
                f"confirmation_score={confirmation_score:.2f}"
            )

        news_quality = analyst_quality.get("commodity_news") or {}
        if (
            news_quality.get("signal") == target_signal
            and news_quality.get("tradeability") != "high"
            and "technical" in opposers + [name for name in ("technical", "fundamental") if name not in supporters]
            and "fundamental" in opposers + [name for name in ("technical", "fundamental") if name not in supporters]
        ):
            multiplier = min(multiplier, max(0.0, _safe_float(self.quality_config.get("low_quality_news_cap_multiplier"), 0.35)))
            reasons.append("low_quality_news_driven_trade")
            notes.append(
                f"probe-capped {target_side}: commodity_news supports target with tradeability={news_quality.get('tradeability')} "
                "while technical/fundamental do not confirm"
            )

        news_control = self.quality_config.get("news_driver_control") or {}
        if news_control.get("enabled", True):
            news_result = self._evaluate_news_driver_control(
                target_side=target_side,
                target_signal=target_signal,
                analyst_quality=analyst_quality,
                supporters=supporters,
                opposers=opposers,
                confirmation_score=confirmation_score,
                control=news_control,
            )
            if news_result["block"]:
                block = True
            multiplier = min(multiplier, news_result["multiplier"])
            reasons.extend(news_result["reasons"])
            notes.extend(news_result["notes"])
            diagnostics["news_driver_control"] = news_result["diagnostics"]

        strict_sides = self.quality_config.get("strict_ticker_sides") or {}
        strict_targets = [str(side).lower() for side in strict_sides.get(str(payload.ticker).upper(), [])]
        if target_side in strict_targets:
            strict_score = _safe_float(self.quality_config.get("strict_min_confirmation_score"), 0.65)
            strict_high_supporters = _safe_int(self.quality_config.get("strict_min_high_quality_supporters"), 2)
            if len(high_quality_supporters) < strict_high_supporters or confirmation_score < strict_score:
                block = True
                reasons.append("strict_ticker_side_quality_gate")
                notes.append(
                    f"blocked {payload.ticker} {target_side}: strict watchlist requires "
                    f"high_quality_supporters>={strict_high_supporters} and confirmation_score>={strict_score:.2f}; "
                    f"got high_quality_supporters={high_quality_supporters}, score={confirmation_score:.2f}"
                )

        weak_side_result = self._evaluate_weak_ticker_side_rule(
            payload=payload,
            target_side=target_side,
            confirmation_score=confirmation_score,
            qualified_supporters=qualified_supporters,
        )
        if weak_side_result["block"]:
            block = True
        multiplier = min(multiplier, weak_side_result["multiplier"])
        reasons.extend(weak_side_result["reasons"])
        notes.extend(weak_side_result["notes"])
        diagnostics.update(weak_side_result["diagnostics"])

        return {
            "block": block,
            "multiplier": 0.0 if block else multiplier,
            "reasons": _dedupe(reasons),
            "notes": notes,
            "diagnostics": diagnostics,
        }

    def _evaluate_news_driver_control(
        self,
        *,
        target_side: str,
        target_signal: str,
        analyst_quality: Dict[str, Dict[str, Any]],
        supporters: List[str],
        opposers: List[str],
        confirmation_score: float,
        control: Dict[str, Any],
    ) -> Dict[str, Any]:
        reasons: List[str] = []
        notes: List[str] = []
        diagnostics: Dict[str, Any] = {}
        multiplier = 1.0
        block = False

        news_quality = analyst_quality.get("commodity_news") or {}
        if news_quality.get("signal") != target_signal:
            return {"block": False, "multiplier": multiplier, "reasons": reasons, "notes": notes, "diagnostics": diagnostics}

        core_supporters = [name for name in supporters if name in {"technical", "fundamental"}]
        core_opposers = [name for name in opposers if name in {"technical", "fundamental"}]
        tradeability = str(news_quality.get("tradeability") or "unknown").lower()
        confidence = _safe_float(news_quality.get("confidence"), 0.0)
        freshness = _safe_float(news_quality.get("freshness_score"), 0.0)
        relevance = _safe_float(news_quality.get("relevance_score"), 0.0)
        min_confidence = _safe_float(control.get("min_news_confidence"), 0.60)
        min_confirmation = _safe_float(control.get("min_market_confirmation_score"), 0.60)
        min_freshness = _safe_float(control.get("min_freshness_score"), 0.70)
        min_relevance = _safe_float(control.get("min_relevance_score"), 0.70)
        cap_multiplier = max(0.0, _safe_float(control.get("cap_multiplier"), 0.50))

        diagnostics.update(
            {
                "core_supporters": core_supporters,
                "core_opposers": core_opposers,
                "tradeability": tradeability,
                "confidence": confidence,
                "freshness_score": freshness,
                "relevance_score": relevance,
                "confirmation_score": confirmation_score,
            }
        )

        if not core_supporters:
            reasons.append("news_only_directional_trade")
            hard_failures = []
            if core_opposers and bool(control.get("block_when_core_opposes", True)):
                hard_failures.append(f"core_opposers={core_opposers}")
            if tradeability != "high":
                hard_failures.append(f"news_tradeability={tradeability}")
            if confidence < min_confidence:
                hard_failures.append(f"news_confidence={confidence:.2f}<{min_confidence:.2f}")
            if freshness < min_freshness:
                hard_failures.append(f"freshness={freshness:.2f}<{min_freshness:.2f}")
            if relevance < min_relevance:
                hard_failures.append(f"relevance={relevance:.2f}<{min_relevance:.2f}")
            if confirmation_score < min_confirmation:
                hard_failures.append(f"confirmation_score={confirmation_score:.2f}<{min_confirmation:.2f}")
            if hard_failures:
                multiplier = min(multiplier, cap_multiplier)
                notes.append(
                    f"probe-capped {target_side}: commodity_news is the only directional supporter; "
                    + ", ".join(hard_failures)
                )
            else:
                multiplier = min(multiplier, cap_multiplier)
                notes.append(
                    f"reduced {target_side}: commodity_news-only high-quality event, multiplier={cap_multiplier:.2f}"
                )
        elif (
            bool(control.get("cap_without_fundamental_confirmation", True))
            and "fundamental" not in supporters
        ):
            reasons.append("news_without_fundamental_anchor")
            multiplier = min(multiplier, cap_multiplier)
            notes.append(
                f"reduced {target_side}: commodity_news supports target without fundamental anchor, "
                f"multiplier={cap_multiplier:.2f}"
            )

        return {
            "block": block,
            "multiplier": 0.0 if block else multiplier,
            "reasons": _dedupe(reasons),
            "notes": notes,
            "diagnostics": diagnostics,
        }

    def _evaluate_weak_ticker_side_rule(
        self,
        *,
        payload: PMRiskGateInput,
        target_side: str,
        confirmation_score: float,
        qualified_supporters: List[str],
    ) -> Dict[str, Any]:
        rule = _side_rule(self.quality_config.get("weak_ticker_side_rules") or {}, payload.ticker, target_side)
        if not rule:
            return {"block": False, "multiplier": 1.0, "reasons": [], "notes": [], "diagnostics": {}}

        signal_combo = _signal_combo_tuple(payload.signal_combo)
        block_combos = [tuple(item) for item in (rule.get("block_signal_combos") or [])]
        min_score = _safe_float(rule.get("min_confirmation_score"), 0.65)
        min_supporters = _safe_int(rule.get("min_qualified_supporters"), 2)
        block_below = _safe_float(rule.get("block_below_confirmation_score"), min_score)
        cap_multiplier = max(0.0, _safe_float(rule.get("cap_multiplier"), 0.50))
        reasons: List[str] = []
        notes: List[str] = []
        diagnostics = {
            "weak_ticker_side_rule": {
                "ticker": payload.ticker,
                "side": target_side,
                "signal_combo": list(signal_combo),
                "qualified_supporters": qualified_supporters,
                "confirmation_score": confirmation_score,
                "min_confirmation_score": min_score,
                "min_qualified_supporters": min_supporters,
                "block_signal_combo": signal_combo in block_combos,
            }
        }

        if signal_combo in block_combos or confirmation_score < block_below or len(qualified_supporters) < min_supporters:
            reasons.append("weak_ticker_side_quality_gate")
            notes.append(
                f"blocked {payload.ticker} {target_side}: weak ticker-side rule requires "
                f"qualified_supporters>={min_supporters} and confirmation_score>={block_below:.2f}; "
                f"got qualified_supporters={qualified_supporters}, score={confirmation_score:.2f}, "
                f"combo={signal_combo}"
            )
            return {
                "block": True,
                "multiplier": 0.0,
                "reasons": reasons,
                "notes": notes,
                "diagnostics": diagnostics,
            }

        reasons.append("weak_ticker_side_cap")
        notes.append(
            f"capped {payload.ticker} {target_side}: weak ticker-side watchlist, multiplier={cap_multiplier:.2f}"
        )
        return {
            "block": False,
            "multiplier": cap_multiplier,
            "reasons": _dedupe(reasons),
            "notes": notes,
            "diagnostics": diagnostics,
        }

    def _evaluate_market_confirmation(self, payload: PMRiskGateInput) -> Dict[str, Any]:
        market_confirmation = payload.market_confirmation or {}
        reasons: List[str] = []
        notes: List[str] = []
        diagnostics = {"market_confirmation": market_confirmation}
        multiplier = 1.0
        block = False

        if not self.market_config.get("enabled", True) or not market_confirmation.get("enabled"):
            return {
                "block": False,
                "multiplier": multiplier,
                "reasons": reasons,
                "notes": notes,
                "diagnostics": diagnostics,
            }

        target_side = _target_side_from_ratio(payload.raw_position_ratio)
        conflicts = market_confirmation.get("conflicts") or []
        confirmations = market_confirmation.get("confirmations") or []
        features = market_confirmation.get("features") or []
        confirmation_score = _safe_float(market_confirmation.get("confirmation_score"), 0.0)

        if bool(self.market_config.get("quality_gate_enabled", False)):
            if features:
                gate_failures = []
                min_score = _safe_float(self.market_config.get("min_confirmation_score_for_new_entry"), 0.45)
                if confirmation_score < min_score:
                    gate_failures.append(f"score={confirmation_score:.2f}<{min_score:.2f}")

                max_conflicts = self.market_config.get("max_conflicts_for_new_entry")
                if max_conflicts is not None and len(conflicts) > _safe_int(max_conflicts):
                    gate_failures.append(f"conflicts={len(conflicts)}>{_safe_int(max_conflicts)}")

                if gate_failures:
                    weak_signal_strength = _safe_float(
                        self.market_config.get(
                            "quality_gate_weak_signal_strength",
                            self.market_config.get("weak_signal_strength", 0.25),
                        ),
                        0.25,
                    )
                    reasons.append("market_confirmation_quality_gate")
                    if (
                        bool(self.market_config.get("quality_gate_block_weak_signal", True))
                        and payload.signal_strength <= weak_signal_strength
                    ):
                        block = True
                        multiplier = 0.0
                        notes.append(
                            f"blocked weak {target_side} signal by PandaAI quality gate: {', '.join(gate_failures)}"
                        )
                    else:
                        quality_multiplier = max(
                            0.0,
                            _safe_float(self.market_config.get("quality_gate_cap_multiplier"), 0.50),
                        )
                        multiplier = min(multiplier, quality_multiplier)
                        notes.append(
                            f"scaled {target_side} by PandaAI quality gate multiplier={quality_multiplier:.2f}: "
                            f"{', '.join(gate_failures)}"
                        )
            else:
                notes.append("PandaAI quality gate skipped: no usable pre-open confirmation features")

        if conflicts:
            weak_signal_strength = _safe_float(self.market_config.get("weak_signal_strength"), 0.25)
            reasons.append("market_confirmation_conflict")
            allow_conflicted_probe = bool(
                self.market_config.get("allow_conflicted_probe_with_strong_confirmation", False)
            )
            conflicted_probe_score = _safe_float(
                self.market_config.get("conflicted_probe_min_confirmation_score", 0.65),
                0.65,
            )
            conflicted_probe_confirmations = _safe_int(
                self.market_config.get("conflicted_probe_min_confirmations", 3),
                3,
            )
            strong_conflicted_probe = (
                allow_conflicted_probe
                and confirmation_score >= conflicted_probe_score
                and len(confirmations) >= conflicted_probe_confirmations
            )
            if (
                bool(self.market_config.get("block_weak_conflicting_signal", True))
                and payload.signal_strength <= weak_signal_strength
                and not strong_conflicted_probe
            ):
                block = True
                multiplier = 0.0
                notes.append(f"blocked weak {target_side} signal due to PandaAI conflicts={conflicts}")
            else:
                conflict_multiplier = max(
                    0.0,
                    _safe_float(self.market_config.get("conflict_cap_multiplier"), 0.50),
                )
                multiplier = min(multiplier, conflict_multiplier)
                if strong_conflicted_probe and payload.signal_strength <= weak_signal_strength:
                    notes.append(
                        f"scaled conflicted {target_side} probe despite weak signal because "
                        f"confirmation_score={confirmation_score:.2f}>={conflicted_probe_score:.2f} "
                        f"and confirmations={len(confirmations)}>={conflicted_probe_confirmations}: "
                        f"conflicts={conflicts}, multiplier={conflict_multiplier:.2f}"
                    )
                else:
                    notes.append(
                        f"scaled {target_side} by PandaAI conflict multiplier={conflict_multiplier:.2f}: "
                        f"conflicts={conflicts}"
                    )
        elif confirmations:
            notes.append(f"PandaAI confirmation supports {target_side}: {confirmations}")

        return {
            "block": block,
            "multiplier": 0.0 if block else multiplier,
            "reasons": _dedupe(reasons),
            "notes": notes,
            "diagnostics": diagnostics,
        }

    def _output(
        self,
        *,
        decision: str,
        target_side: str,
        multiplier: float,
        reasons: List[str],
        notes: List[str],
        diagnostics: Dict[str, Any],
        payload: PMRiskGateInput,
        policy_version: str,
        learning_mode: str,
    ) -> PMRiskGateOutput:
        multiplier = max(0.0, float(multiplier or 0.0))
        decision = normalize_audit_decision(decision, multiplier)

        state_key = build_audit_state_key(
            ticker=payload.ticker,
            target_side=target_side,
            signal_combo=payload.signal_combo,
            market_state=None,
            market_confirmation=payload.market_confirmation or {},
        )
        audit_payload = build_audit_payload(
            policy_version=policy_version,
            learning_mode=learning_mode,
            state_key=state_key,
            decision=decision,
            diagnostics=diagnostics,
            memory_reads={
                "ticker_side_performance": payload.recent_ticker_side_performance or {},
                "conditional_performance": payload.recent_conditional_performance or {},
                "research_memory": "not_consumed_by_pm_risk_gate",
            },
        )
        return PMRiskGateOutput(
            decision=decision,
            target_side=target_side,
            position_ratio_multiplier=multiplier,
            confidence_multiplier=1.0,
            cap_multiplier=multiplier,
            reasons=_dedupe(reasons),
            notes=notes,
            diagnostics=diagnostics,
            audit_payload=audit_payload,
            policy_version=policy_version,
            learning_mode=learning_mode,
        )


__all__ = [
    "PMRiskGate",
    "PMRiskGateInput",
    "PMRiskGateOutput",
]



