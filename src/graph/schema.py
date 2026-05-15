import operator
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator
from typing_extensions import Annotated, TypedDict

from graph.constants import Signal


class AnalystSignal(BaseModel):
    """Signal produced by an analyst agent."""

    agent_name: str = Field(default="", description="Analyst agent name")
    signal: Signal = Field(default=Signal.NEUTRAL, description="Bullish / bearish / neutral signal")
    confidence: float = Field(default=0.5, description="Signal confidence from 0.0 to 1.0")
    justification: str = Field(default="No justification provided due to error", description="Signal rationale")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured audit metadata, e.g. basis and data-quality diagnostics",
    )

    @field_validator("metadata", mode="before")
    @classmethod
    def normalize_metadata(cls, value):
        """Treat model-emitted null metadata as an empty audit dictionary."""
        return {} if value is None else value


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
    warning_message: Optional[str] = Field(default=None, description="Audit warning message")
    intraday_audit: Optional[Dict[str, Any]] = Field(default=None, description="Intraday execution audit payload")


class FuturesRecommendation(BaseModel):
    """Phase1 strategy recommendation or next-day rollover recommendation."""

    id: Optional[str] = Field(default=None, description="Recommendation id")
    config_id: str = Field(default="", description="Config id")
    reference_portfolio_id: str = Field(default="", description="Reference settled portfolio id")
    trading_date: str = Field(default="", description="Recommendation creation date")
    effective_trade_date: str = Field(default="", description="Recommendation effective trade date")
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
    trading_date: str = Field(default="", description="Trade date")
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
    llm_prompt: Optional[str] = Field(default=None, description="Prompt audit")
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
    cashflow: float = Field(description="Cash balance")
    positions: Dict[str, Position] = Field(description="Ticker positions")
    margin_used: float = Field(default=0.0, description="Reserved margin")
    margin_available: float = Field(default=0.0, description="Available margin")
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
    decision: FuturesDecision
    recommendation: Optional[FuturesRecommendation]
    deepanalyze_market_state: Optional[Dict[str, Any]]
    deepanalyze_fundamental_trends: Optional[Dict[str, Any]]
