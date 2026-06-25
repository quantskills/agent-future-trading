from enum import Enum

class AgentKey:
    # analyst keys
    TECHNICAL = "technical"
    FUNDAMENTAL = "fundamental"
    COMMODITY_NEWS = "commodity_news"
    COMPANY_NEWS = COMMODITY_NEWS  # backward-compatible alias for older configs/scripts
    MACROECONOMIC = "macroeconomic"
    POLICY = "policy"
    # workflow keys
    SIGNAL_COLLECTOR = "signal_collector"
    PORTFOLIO = "portfolio manager"
    PLANNER = "analyst planner"
    TRADER = "trader"
    ACCOUNTANT = "accountant"
    SETTLEMENT = ACCOUNTANT  # backward-compatible settlement node alias

class Signal(str, Enum):
    """Signal type"""
    BULLISH = "Bullish"
    BEARISH = "Bearish"
    NEUTRAL = "Neutral"

    def __str__(self) -> str:
        return self.value
