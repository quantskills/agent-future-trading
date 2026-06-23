from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, List, Dict, Any

class BaseDB(ABC):
    @abstractmethod
    def get_config(self, config_id: str) -> dict:
        pass

    @abstractmethod
    def get_config_id_by_name(self, exp_name: str) -> str:
        pass

    @abstractmethod
    def delete_config_and_portfolios(self, config_id: str) -> bool:
        """Delete a config and all its associated data."""
        pass

    @abstractmethod
    def create_config(self, config: dict) -> str:
        pass

    @abstractmethod
    def get_latest_trading_date(self, config_id: str) -> datetime:
        pass

    @abstractmethod
    def get_latest_portfolio(self, config_id: str) -> dict:
        pass

    @abstractmethod
    def create_portfolio(self, config_id: str, cashflow: float, trading_date: datetime) -> str:
        pass

    @abstractmethod
    def copy_portfolio(self, config_id: str, portfolio: dict, trading_date: datetime) -> str:
        pass

    @abstractmethod
    def get_or_create_portfolio_for_date(self, config_id: str, portfolio: dict, trading_date: datetime) -> dict:
        """
        Get existing portfolio for the given trading date, or create a new one based on the latest portfolio.

        This method prevents duplicate portfolio records for the same trading date.
        """
        pass

    @abstractmethod
    def update_portfolio(self, config_id: str, portfolio: dict, trading_date: datetime) -> bool:
        pass

    @abstractmethod
    def save_signal(self, portfolio_id: str, analyst: str, ticker: str, prompt: str, signal: dict) -> str:
        pass

    # ========== Futures settlement interfaces ==========

    @abstractmethod
    def get_previous_settlement(
        self,
        portfolio_id: str,
        trading_date: datetime
    ) -> Optional["FuturesSettlementRecord"]:
        """Get the latest settlement record before the given trading date."""
        pass

    @abstractmethod
    def save_daily_settlement(
        self,
        portfolio_id: str,
        settlement: "FuturesSettlementRecord"
    ) -> bool:
        """Save a daily settlement record."""
        pass

    # ========== Futures phase1 and phase2 interfaces ==========

    def get_latest_settled_portfolio(self, config_id: str) -> Optional[Dict[str, Any]]:
        """Get the latest settled official portfolio."""
        raise NotImplementedError

    def save_futures_recommendation(self, recommendation: Any) -> Optional[str]:
        """Save a futures recommendation."""
        raise NotImplementedError

    def get_futures_recommendations_by_effective_date(
        self,
        config_id: str,
        effective_trade_date: datetime,
        source_type: Optional[str] = None,
        status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get futures recommendations by effective trading date."""
        raise NotImplementedError

    def update_futures_recommendation_status(
        self,
        recommendation_id: str,
        status: str,
        action: Optional[str] = None,
        lots: Optional[int] = None,
        execution_price: Optional[float] = None,
        warning_message: Optional[str] = None,
        signal_snapshot: Optional[Dict[str, Any]] = None,
        audit_payload: Optional[Dict[str, Any]] = None,
        base_price: Optional[float] = None,
        base_price_source: Optional[str] = None,
        base_price_date: Optional[str] = None,
        open_price: Optional[float] = None,
        prev_close_price: Optional[float] = None,
        slippage_model: Optional[str] = None,
        slippage_ticks: Optional[int] = None,
        slippage_amount: Optional[float] = None,
    ) -> bool:
        """Update futures recommendation execution status."""
        raise NotImplementedError

    def save_futures_intraday_decision(self, decision: Dict[str, Any]) -> Optional[str]:
        """Save an intraday execution-gate audit decision."""
        raise NotImplementedError

    def save_futures_transaction(self, transaction: Any) -> Optional[str]:
        """Save a futures transaction."""
        raise NotImplementedError

    def get_futures_transactions_by_date(
        self,
        config_id: str,
        trading_date: datetime,
        execution_phase: Optional[str] = None,
        booked_in_settlement: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        """Get futures transactions by trading date."""
        raise NotImplementedError

    def mark_futures_transactions_booked(self, transaction_ids: List[str]) -> bool:
        """Mark futures transactions as booked by settlement."""
        raise NotImplementedError

    def update_futures_transactions_settle_prices(
        self,
        settle_price_updates: List[Dict[str, Any]],
    ) -> bool:
        """Backfill transaction settle_price values after phase3 settlement."""
        raise NotImplementedError

    def start_trading_day_phase(
        self,
        config_id: str,
        trading_date: datetime,
        phase: str,
        message: str = ""
    ) -> bool:
        """Start a trading day phase record."""
        raise NotImplementedError

    def complete_trading_day_phase(
        self,
        config_id: str,
        trading_date: datetime,
        phase: str,
        status: str,
        message: str = "",
        memory_config: Optional[Dict[str, Any]] = None,
        retention_config: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Complete a trading day phase record."""
        raise NotImplementedError

    def get_trading_day_phase(
        self,
        config_id: str,
        trading_date: datetime,
        phase: str
    ) -> Optional[Dict[str, Any]]:
        """Get a trading day phase record."""
        raise NotImplementedError

    def get_futures_transaction_memory(
        self,
        config_id: str,
        ticker: str,
        limit: int,
        trading_date=None,
    ) -> List[str]:
        """Get recent futures transaction memory for prompts."""
        raise NotImplementedError

    def get_futures_conditional_trade_performance(
        self,
        config_id: str,
        ticker: str,
        side: str,
        trading_date,
        signal_combo: Optional[List[str]] = None,
        lookback_trades: int = 30,
        include_rollover: bool = False,
    ) -> Dict[str, Any]:
        """Get completed futures trade-pair performance for ticker + side + signal combo."""
        raise NotImplementedError

    def refresh_strategy_memory(
        self,
        config_id: str,
        trading_date,
        memory_config: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Refresh DB-backed strategy memory up to trading_date."""
        raise NotImplementedError

    def get_strategy_memory(
        self,
        config_id: str,
        ticker: str,
        side: str,
        trading_date=None,
        signal_combo: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Get DB-backed strategy memory for ticker + side + optional signal combo."""
        raise NotImplementedError

    def get_trade_episode_memory(
        self,
        config_id: str,
        ticker: str,
        sector: Optional[str] = None,
        side: Optional[str] = None,
        horizon_class: Optional[str] = None,
        market_regime: Optional[str] = None,
        trading_date=None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Get completed trade episodes as non-authoritative learning context."""
        raise NotImplementedError

    def get_alpha_setup_action_values(
        self,
        config_id: str,
        ticker: str,
        side: Optional[str] = None,
        horizon_class: Optional[str] = None,
        market_regime: Optional[str] = None,
        setup_type: Optional[str] = None,
        trading_date=None,
        limit: int = 5,
        consumer_scope: Optional[str] = "pm_learning",
        learning_lane: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get PM-consumable canonical action-value rows, strictly before decision date."""
        raise NotImplementedError

    def get_similar_alpha_setup_action_values(
        self,
        config_id: str,
        ticker: str,
        sector: Optional[str] = None,
        side: Optional[str] = None,
        horizon_class: Optional[str] = None,
        market_regime: Optional[str] = None,
        setup_type: Optional[str] = None,
        trading_date=None,
        limit: int = 6,
    ) -> List[Dict[str, Any]]:
        """Get strictly historical, compact action preferences from similar setup samples."""
        raise NotImplementedError

    def get_exploratory_hypotheses(
        self,
        config_id: str,
        ticker: str,
        sector: Optional[str] = None,
        side: Optional[str] = None,
        horizon_class: Optional[str] = None,
        market_regime: Optional[str] = None,
        trading_date=None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Get reviewer research hypotheses as prompt priors."""
        raise NotImplementedError

    def get_provisional_policy_state(
        self,
        config_id: str,
        ticker: str,
        side: Optional[str] = None,
        setup_type: Optional[str] = None,
        horizon_class: Optional[str] = None,
        trading_date=None,
    ) -> List[Dict[str, Any]]:
        """Get short-lived reviewer risk sentinels."""
        raise NotImplementedError

