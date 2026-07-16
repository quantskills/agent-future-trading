import operator
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator
from typing_extensions import Annotated, TypedDict

from graph.constants import Signal


class AnalystSignal(BaseModel):
    """Signal produced by an analyst agent."""

    contract_version: str = Field(default="agentquant.signal.v2", description="Agent artifact contract version")
    agent_name: str = Field(default="", description="Analyst agent name")
    signal: Signal = Field(default=Signal.NEUTRAL, description="Bullish / bearish / neutral signal")
    confidence: float = Field(default=0.5, description="Signal confidence from 0.0 to 1.0")
    justification: str = Field(default="No justification provided due to error", description="Signal rationale")
    data_cutoff: str = Field(default="pre_open", description="Data cutoff used by this signal")
    no_lookahead_status: str = Field(default="unchecked", description="ok / warning / violation / unchecked")
    determinism_mode: str = Field(default="llm_with_deterministic_controls", description="How this artifact was produced")
    llm_provider: str = Field(default="", description="LLM provider used by the analyst when available")
    llm_model: str = Field(default="", description="LLM model used by the analyst when available")
    source_artifacts: List[str] = Field(default_factory=list, description="Upstream artifact ids or descriptors")
    validation_errors: List[str] = Field(default_factory=list, description="Schema or contract validation warnings")
    horizon_class: str = Field(
        default="unknown",
        description="Effective horizon: short / medium / long / event_short / flat / unknown",
    )
    analyst_horizon: str = Field(default="unknown", description="Natural analyst horizon before PM fusion")
    decision_horizon: str = Field(default="unknown", description="PM decision horizon after fusion")
    execution_horizon: str = Field(default="unknown", description="Trader execution horizon")
    validation_horizon: str = Field(default="unknown", description="Reviewer validation horizon")
    expected_horizon_days: int = Field(default=0, description="Expected signal horizon in trading days")
    market_regime: str = Field(default="unknown", description="Market regime used by the analyst")
    trend_stage: str = Field(default="unknown", description="Trend or price-stage classification")
    setup_type: str = Field(default="unknown", description="Canonical setup classification")
    price_percentile: Optional[float] = Field(
        default=None,
        description="Current price percentile in the analyst lookback window, 0.0 to 1.0 when available",
    )
    invalidation_level: Optional[float] = Field(default=None, description="Price level invalidating the signal")
    atr_stop_distance: Optional[float] = Field(default=None, description="ATR-based stop distance when available")
    add_allowed: bool = Field(default=False, description="Whether this signal permits adding to an existing position")
    direction_anchor: str = Field(default="unknown", description="Medium-horizon directional anchor")
    supply_demand_state: str = Field(default="unknown", description="Supply-demand state")
    basis_state: str = Field(default="unknown", description="Basis or spot-futures state")
    inventory_state: str = Field(default="unknown", description="Inventory state")
    warehouse_receipt_state: str = Field(default="unknown", description="Warehouse receipt state")
    position_flow_state: str = Field(default="unknown", description="Position or capital-flow state")
    data_freshness: str = Field(default="unknown", description="fresh / near_stale / stale / missing / unknown")
    event_type: str = Field(default="none", description="News event type")
    impact_window_days: int = Field(default=0, description="Expected event impact window in trading days")
    requires_fundamental_confirmation: bool = Field(
        default=False,
        description="Whether this signal must be confirmed by fundamental evidence before scaling",
    )
    evidence_quality: str = Field(default="unknown", description="high / medium / low / unknown")
    evidence_strength: str = Field(default="unknown", description="strong / medium / weak / unknown analyst evidence strength")
    evidence_freshness: str = Field(default="unknown", description="fresh / usable / stale / unknown evidence freshness")
    evidence_decay_risk: str = Field(default="unknown", description="low / medium / high / unknown risk that evidence decays before execution")
    confirmation_requirements: List[str] = Field(default_factory=list, description="Required confirmations before PM can treat evidence as actionable")
    technical_false_breakout_risk: str = Field(default="not_applicable", description="Technical false-breakout risk label")
    fundamental_opposition_strength: str = Field(default="not_applicable", description="Strength of opposing fundamental evidence")
    news_impact_window: str = Field(default="", description="Commodity-news catalyst impact window")
    one_off_event_risk: str = Field(default="not_applicable", description="News one-off event risk label")
    business_quality_score: float = Field(default=0.0, description="Business-quality score from 0.0 to 1.0")
    primary_business_driver: str = Field(default="", description="Primary business driver behind the signal")
    secondary_confirmation: str = Field(default="", description="Secondary confirmation chain")
    counter_evidence: str = Field(default="", description="Main evidence that could invalidate the signal")
    reward_risk_ratio: Optional[float] = Field(default=None, description="Expected reward/risk ratio")
    factor_alignment_score: float = Field(default=0.0, description="How well factors align with the signal")
    data_coverage_score: float = Field(default=0.0, description="Data coverage score from 0.0 to 1.0")
    tradeability_reason: str = Field(default="", description="Why this signal is or is not tradeable")
    opportunity_type: str = Field(
        default="unknown",
        description=(
            "Opportunity taxonomy: trend_continuation / reversal / range_breakout / "
            "event_driven / medium_fundamental / short_timing / probe / no_trade / unknown"
        ),
    )
    opportunity_state: str = Field(
        default="watch_for_trigger",
        description=(
            "Analyst opportunity state: no_opportunity / watch_for_trigger / "
            "probe_candidate / tradeable_candidate / risk_reduction_candidate"
        ),
    )
    learning_impact_summary: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Structured explanation of how past-only learning affected today's evidence judgment. "
            "It must not contain trade authority, lots, margin, or execution instructions."
        ),
    )
    factor_calibration_summary: Dict[str, Any] = Field(
        default_factory=dict,
        description="Fundamental analyst factor calibration summary; empty for non-fundamental analysts",
    )
    event_calibration_summary: Dict[str, Any] = Field(
        default_factory=dict,
        description="Commodity-news analyst event calibration summary; empty for non-news analysts",
    )
    setup_quality_score: float = Field(default=0.0, description="Trade setup quality from 0.0 to 1.0")
    entry_quality: str = Field(default="unknown", description="entry quality: poor / weak / acceptable / strong / unknown")
    setup_quality_notes: List[str] = Field(default_factory=list, description="Machine-readable setup quality notes")
    entry_trigger: str = Field(default="", description="Structured pre-trade entry or timing trigger")
    exit_hint: str = Field(default="", description="Structured exit, reduction, or invalidation hint")
    holding_period_hint: str = Field(default="", description="Expected holding style/window in plain text")
    evidence_role: str = Field(
        default="",
        description=(
            "How PM should use this signal: entry_timing / direction_context / event_catalyst / "
            "risk_context / execution_context. risk_context is an evidence role, not an agent."
        ),
    )
    direction_context: str = Field(default="", description="Directional background supplied by this analyst")
    trend_direction: str = Field(default="", description="Technical trend direction, separated from entry timing")
    entry_timing_signal: str = Field(default="", description="Technical entry timing classification")
    price_location: str = Field(default="", description="Price location or zone used for entry timing")
    trigger_valid: bool = Field(default=False, description="Whether current trigger is valid for a real trade candidate")
    invalidation_present: bool = Field(default=False, description="Whether invalidation boundary is present")
    factor_focus: List[str] = Field(default_factory=list, description="Primary factor groups or evidence surfaces")
    current_evidence_conflict: List[str] = Field(default_factory=list, description="Current evidence that conflicts with the view")
    research_contract_version: str = Field(default="agentquant.research.v1", description="Trade research contract version")
    message_contract_version: str = Field(default="agentquant.message.v1", description="Internal message contract version")
    neutral_reason: str = Field(default="", description="Required reason when signal is Neutral")
    missing_evidence: List[str] = Field(default_factory=list, description="Evidence missing for a directional call")
    conflicting_factors: List[str] = Field(default_factory=list, description="Factors that conflict with the signal")
    would_change_view_if: str = Field(default="", description="Condition that would change Neutral or directional view")
    opportunity_cost_risk: str = Field(default="", description="Risk of missing a trade by staying Neutral")
    recommended_observation_window: str = Field(default="", description="Suggested observation window for Neutral")
    neutral_opportunity_bucket: str = Field(
        default="unknown",
        description="Neutral opportunity bucket: watchlist_trigger / evidence_gap / conflict_avoidance / low_tradeability / horizon_mismatch / accountable_observation / unaccountable",
    )
    neutral_trigger_condition: str = Field(default="", description="Concrete condition that can move Neutral into a tradeable setup")
    counterfactual_side: str = Field(default="flat", description="Counterfactual direction to track for Neutral: long / short / flat")
    neutral_watchlist_priority: str = Field(default="none", description="none / low / medium / high priority for conditional Neutral observation")
    accountability_tag: str = Field(default="", description="Post-trade accountability label")
    similar_past_cases: List[str] = Field(default_factory=list, description="Reviewer-provided similar past cases")
    do_not_trade_reason: str = Field(default="", description="Reviewer/business reason to avoid trading")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured audit metadata, e.g. basis and data-quality diagnostics",
    )

    @field_validator("metadata", mode="before")
    @classmethod
    def normalize_metadata(cls, value):
        """Treat model-emitted null metadata as an empty audit dictionary."""
        return {} if value is None else value

    @field_validator(
        "learning_impact_summary",
        "factor_calibration_summary",
        "event_calibration_summary",
        mode="before",
    )
    @classmethod
    def normalize_dict_fields(cls, value):
        """Treat model-emitted null structured summaries as empty dictionaries."""
        return value if isinstance(value, dict) else {}

    @field_validator(
        "source_artifacts",
        "validation_errors",
        "missing_evidence",
        "conflicting_factors",
        "similar_past_cases",
        "factor_focus",
        "current_evidence_conflict",
        "setup_quality_notes",
        "confirmation_requirements",
        mode="before",
    )
    @classmethod
    def normalize_list_fields(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [value] if value.strip() else []
        try:
            return list(value)
        except Exception:
            return [str(value)]

    @field_validator(
        "contract_version",
        "data_cutoff",
        "no_lookahead_status",
        "determinism_mode",
        "llm_provider",
        "llm_model",
        "horizon_class",
        "analyst_horizon",
        "decision_horizon",
        "execution_horizon",
        "validation_horizon",
        "market_regime",
        "trend_stage",
        "setup_type",
        "direction_anchor",
        "supply_demand_state",
        "basis_state",
        "inventory_state",
        "warehouse_receipt_state",
        "position_flow_state",
        "data_freshness",
        "event_type",
        "evidence_quality",
        "primary_business_driver",
        "secondary_confirmation",
        "counter_evidence",
        "tradeability_reason",
        "opportunity_type",
        "opportunity_state",
        "entry_quality",
        "entry_trigger",
        "exit_hint",
        "holding_period_hint",
        "research_contract_version",
        "message_contract_version",
        "neutral_reason",
        "would_change_view_if",
        "opportunity_cost_risk",
        "recommended_observation_window",
        "neutral_opportunity_bucket",
        "neutral_trigger_condition",
        "counterfactual_side",
        "neutral_watchlist_priority",
        "accountability_tag",
        "do_not_trade_reason",
        mode="before",
    )
    @classmethod
    def normalize_text_fields(cls, value):
        return "unknown" if value is None or str(value).strip() == "" else str(value)

    @field_validator("expected_horizon_days", "impact_window_days", mode="before")
    @classmethod
    def normalize_horizon_days(cls, value):
        try:
            return max(0, int(value or 0))
        except Exception:
            return 0

    @field_validator("confidence", "business_quality_score", "factor_alignment_score", "data_coverage_score", mode="before")
    @classmethod
    def normalize_score_fields(cls, value):
        try:
            score = float(value if value is not None else 0.0)
        except Exception:
            score = 0.0
        return max(0.0, min(1.0, score))

    @field_validator("setup_quality_score", mode="before")
    @classmethod
    def normalize_setup_quality_score(cls, value):
        try:
            score = float(value if value is not None else 0.0)
        except Exception:
            score = 0.0
        return max(0.0, min(1.0, score))


class ArtifactHeader(BaseModel):
    """Shared audit header for local A2A-inspired agent artifacts."""

    contract_version: str = Field(default="agentquant.artifact.v1")
    agent_name: str = Field(default="")
    trading_date: str = Field(default="")
    ticker: str = Field(default="")
    config_id: str = Field(default="")
    recommendation_id: Optional[str] = Field(default=None)
    evidence_pack_id: Optional[str] = Field(default=None)
    data_cutoff: str = Field(default="pre_open")
    no_lookahead_status: str = Field(default="unchecked")
    determinism_mode: str = Field(default="deterministic")
    llm_provider: str = Field(default="")
    llm_model: str = Field(default="")
    source_artifacts: List[str] = Field(default_factory=list)
    validation_errors: List[str] = Field(default_factory=list)


class AgentArtifact(BaseModel):
    """Minimal structured artifact wrapper used by contract tests and snapshots."""

    artifact_type: str = Field(default="generic")
    header: ArtifactHeader = Field(default_factory=ArtifactHeader)
    payload: Dict[str, Any] = Field(default_factory=dict)


class FuturesAction(str, Enum):
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"
    HOLD = "hold"


class TradingPhase(str, Enum):
    PHASE1 = "phase1"
    PHASE2 = "phase2"
    PHASE3 = "phase3"
    PHASE4 = "phase4"


class BasePriceSource(str, Enum):
    T_OPEN = "t_open"
    T_MINUS_1_CLOSE_FALLBACK = "t_minus_1_close_fallback"
    INTRADAY_NEXT_1M_OPEN = "intraday_next_1m_open"
    INTRADAY_FIRST_VALID_1M_OPEN = "intraday_first_valid_1m_open"


class RecommendationSourceType(str, Enum):
    STRATEGY = "strategy"
    ROLLOVER = "rollover"
    FORCED_RISK = "forced_risk"


class RecommendationStatus(str, Enum):
    PENDING = "pending"
    EXECUTED = "executed"
    SKIPPED = "skipped"
    FAILED = "failed"


class RecommendationAction(str, Enum):
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"
    HOLD = "hold"
    ROLLOVER = "rollover"


class FuturesDecision(BaseModel):
    """Futures trade decision."""

    ticker: str = Field(default="", description="Underlying ticker")
    action: FuturesAction = Field(default=FuturesAction.HOLD, description="Futures action")
    lots: int = Field(default=0, description="Lots to trade")
    price: float = Field(default=0.0, description="Execution reference price")
    settle_price: float = Field(default=0.0, description="Settlement reference price")
    contract_multiplier: float = Field(default=1.0, description="Contract multiplier")
    margin_rate: float = Field(default=0.15, description="Margin rate")
    contract_code: Optional[str] = Field(default=None, description="Concrete contract code")
    daily_pnl: float = Field(default=0.0, description="Daily PnL")
    commission: float = Field(default=0.0, description="Commission")
    justification: str = Field(default="Just hold due to error", description="Decision rationale")


class MorningExecutionBasis(BaseModel):
    """Resolved phase2 execution basis."""

    base_price: Optional[float] = Field(default=None, description="Execution basis price")
    base_price_source: Optional[BasePriceSource] = Field(default=None, description="Basis price source")
    base_price_date: Optional[str] = Field(default=None, description="Basis price trade date")
    open_price: Optional[float] = Field(default=None, description="T-day open price")
    prev_close_price: Optional[float] = Field(default=None, description="Previous close price")
    contract_code: Optional[str] = Field(default=None, description="Concrete contract visible at the basis cutoff")
    contract_facts: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Concrete contract facts visible at the basis cutoff",
    )
    warning_message: Optional[str] = Field(default=None, description="Audit warning message")
    intraday_audit: Optional[Dict[str, Any]] = Field(default=None, description="Intraday execution audit payload")


class FuturesRecommendation(BaseModel):
    """Phase1 strategy recommendation or non-strategy operational recommendation."""

    id: Optional[str] = Field(default=None, description="Recommendation id")
    config_id: str = Field(default="", description="Config id")
    reference_portfolio_id: str = Field(
        default="",
        description="Most recent settled portfolio id for formal Prev(T)",
    )
    trading_date: str = Field(default="", description="Logical futures trading day T")
    effective_trade_date: str = Field(default="", description="Logical futures execution day T")
    source_type: RecommendationSourceType = Field(default=RecommendationSourceType.STRATEGY, description="Source type")
    underlying_code: str = Field(default="", description="Underlying code such as RB/M")
    from_contract: Optional[str] = Field(default=None, description="Rollover from-contract")
    to_contract: Optional[str] = Field(default=None, description="Rollover to-contract")
    contract_code: Optional[str] = Field(default=None, description="Target contract code")
    action: RecommendationAction = Field(default=RecommendationAction.HOLD, description="Recommended action")
    lots: int = Field(default=0, description="Recommended lots")
    base_price: Optional[float] = Field(default=None, description="Planning reference price")
    base_price_source: Optional[BasePriceSource] = Field(default=None, description="Planning price source")
    base_price_date: Optional[str] = Field(default=None, description="Planning price date")
    open_price: Optional[float] = Field(default=None, description="T-day open price")
    prev_close_price: Optional[float] = Field(default=None, description="Previous close price")
    slippage_model: Optional[str] = Field(default=None, description="Slippage model")
    slippage_ticks: Optional[int] = Field(default=None, description="Slippage ticks")
    slippage_amount: Optional[float] = Field(default=None, description="Slippage amount")
    execution_price: Optional[float] = Field(default=None, description="Planned or actual execution price")
    justification: str = Field(default="", description="Recommendation rationale")
    signal_snapshot: Optional[Dict[str, Any]] = Field(default=None, description="Analyst signal snapshot")
    audit_payload: Optional[Dict[str, Any]] = Field(default=None, description="Structured execution audit payload")
    warning_message: Optional[str] = Field(default=None, description="Audit warning message")
    status: RecommendationStatus = Field(default=RecommendationStatus.PENDING, description="Recommendation status")
    created_at: Optional[str] = Field(default=None, description="Created timestamp")


class FuturesTransaction(BaseModel):
    """Phase2 execution transaction."""

    id: Optional[str] = Field(default=None, description="Transaction id")
    portfolio_id: str = Field(default="", description="Reference settled portfolio id")
    config_id: str = Field(default="", description="Config id")
    recommendation_id: Optional[str] = Field(default=None, description="Linked recommendation id")
    trading_date: str = Field(default="", description="Logical futures trading day")
    ticker: str = Field(default="", description="Underlying code")
    contract_code: Optional[str] = Field(default=None, description="Executed contract code")
    action: FuturesAction = Field(default=FuturesAction.HOLD, description="Executed action")
    lots: int = Field(default=0, description="Executed lots")
    execution_price: float = Field(default=0.0, description="Actual execution price")
    settle_price: Optional[float] = Field(default=None, description="T-day settlement price")
    contract_multiplier: float = Field(default=1.0, description="Contract multiplier")
    margin_rate: float = Field(default=0.0, description="Margin rate")
    margin_used: float = Field(default=0.0, description="Margin reserved by this transaction")
    daily_pnl: float = Field(default=0.0, description="Cached daily PnL")
    commission: float = Field(default=0.0, description="Commission")
    source_type: RecommendationSourceType = Field(default=RecommendationSourceType.STRATEGY, description="Transaction source")
    execution_phase: Optional[TradingPhase] = Field(default=None, description="Execution phase; must be set explicitly")
    execution_price_basis: Optional[str] = Field(default=None, description="Execution price composition")
    base_price: Optional[float] = Field(default=None, description="Execution basis price")
    base_price_source: Optional[BasePriceSource] = Field(default=None, description="Execution basis source")
    base_price_date: Optional[str] = Field(default=None, description="Execution basis date")
    open_price: Optional[float] = Field(default=None, description="T-day open price")
    prev_close_price: Optional[float] = Field(default=None, description="Previous close price")
    slippage_model: Optional[str] = Field(default=None, description="Slippage model")
    slippage_ticks: Optional[int] = Field(default=None, description="Slippage ticks")
    slippage_amount: Optional[float] = Field(default=None, description="Slippage amount")
    released_margin: Optional[float] = Field(default=None, description="Margin released by this transaction")
    margin_delta: Optional[float] = Field(default=None, description="Signed margin change")
    post_trade_margin_used: Optional[float] = Field(default=None, description="Position margin used after the trade")
    audit_payload: Optional[Dict[str, Any]] = Field(default=None, description="Structured transaction audit payload")
    warning_message: Optional[str] = Field(default=None, description="Audit warning message")
    booked_in_settlement: bool = Field(default=False, description="Whether settlement has booked this transaction")
    justification: str = Field(default="", description="Transaction rationale")
    created_at: Optional[str] = Field(default=None, description="Created timestamp")


class Position(BaseModel):
    """Unified position model for stock and futures workflows."""

    value: float = Field(default=0.0, description="Position notional value")
    shares: int = Field(default=0, description="Shares for stocks or signed lots for futures")
    entry_price: Optional[float] = Field(default=None, description="Average entry price")
    entry_date: Optional[str] = Field(default=None, description="Entry date")
    contract_code: Optional[str] = Field(default=None, description="Concrete contract code")
    settle_price: Optional[float] = Field(default=None, description="Previous settlement price")
    current_settle_price: Optional[float] = Field(default=None, description="Current settlement price")
    margin_used: float = Field(default=0.0, description="Reserved margin")
    margin_rate: float = Field(default=0.0, description="Margin rate")
    contract_multiplier: Optional[float] = Field(default=None, description="Contract multiplier")
    contract_type: Optional[str] = Field(default=None, description="Contract type")
    unrealized_pnl: float = Field(default=0.0, description="Unrealized PnL")
    realized_pnl: float = Field(default=0.0, description="Realized PnL")

class PositionRisk(BaseModel):
    """Risk assessment for one ticker."""

    optimal_position_ratio: float = Field(
        default=0.0,
        description="Signed target ratio. Positive=long, negative=short, zero=neutral",
    )
    justification: str = Field(
        default="No assessment provided due to insufficient data",
        description="Risk assessment rationale",
    )


class Portfolio(BaseModel):
    """Portfolio state during workflow execution."""

    id: str = Field(description="Portfolio id")
    cashflow: float = Field(description="Cash available after reserved margin")
    account_equity: float = Field(default=0.0, description="Cash balance plus reserved margin")
    cash_available: float = Field(default=0.0, description="Cash available after reserved margin")
    positions: Dict[str, Position] = Field(description="Ticker positions")
    margin_used: float = Field(default=0.0, description="Reserved margin")
    margin_available: float = Field(default=0.0, description="Legacy alias for cash_available")
    margin_ratio: float = Field(default=0.0, description="Margin ratio")
    daily_settlement_pnl: float = Field(default=0.0, description="Daily settlement PnL")
    risk_status: str = Field(default="NORMAL", description="NORMAL / WARNING / LIQUIDATION")
    last_settle_date: Optional[str] = Field(default=None, description="Last settlement date")
    is_settled: bool = Field(default=False, description="Whether the portfolio is settled for the day")

class FundState(TypedDict):
    """Workflow state passed between graph nodes."""

    exp_name: str
    config_id: str
    trading_date: datetime
    ticker: str
    llm_config: Dict[str, Any]
    portfolio: Portfolio
    num_tickers: int
    market_type: str
    enabled_analysts: List[str]
    phase: Optional[TradingPhase]
    morning_price_context: Optional[MorningExecutionBasis]
    pre_open_only: bool
    info_cutoff: Optional[str]
    config: Dict[str, Any]
    full_config: Dict[str, Any]
    router: Any
    analyst_signals: Annotated[List[AnalystSignal], operator.add]
    pre_open_reference_price_unavailable: bool
    pre_open_reference_price_unavailable_reason: str
    signal_collection_contract: Dict[str, Any]
    decision: FuturesDecision
    pm_state: Dict[str, Any]
    recommendation: Optional[FuturesRecommendation]
