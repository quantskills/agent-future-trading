from typing import Dict, Callable, List
from agents.analysts import *
from graph.constants import AgentKey

class AgentRegistry:
    """Registry for all agents."""
    
    # Initialize as actual dictionaries, not just type annotations
    agent_func_mapping: Dict[str, Callable] = {}
    agent_doc_mapping: Dict[str, str] = {}

    # Analyst KEYs
    ANALYST_KEYS = [
        AgentKey.TECHNICAL,
        AgentKey.FUNDAMENTAL,
        AgentKey.COMMODITY_NEWS,
        AgentKey.MACROECONOMIC,
        AgentKey.POLICY
    ]

    @classmethod
    def get_agent_func_by_key(cls, key: str) -> Callable:
        """Get agent function by key."""
        return cls.agent_func_mapping.get(key)

    @classmethod
    def get_all_analyst_keys(cls) -> List[str]:
        """Get all analyst keys."""
        return cls.ANALYST_KEYS
    
    @classmethod
    def check_agent_key(cls, key: str) -> bool:
        """Check if an agent key is valid."""
        return key in cls.ANALYST_KEYS

    @classmethod
    def get_analyst_info(cls, key: str) -> str:
        """Get analyst info."""
        return cls.agent_doc_mapping[key]

    @classmethod
    def register_agent(cls, key: str, agent_func: Callable, agent_doc: str) -> None:
        """
        Register a new agent.
        
        Args:
            key: Unique identifier for the agent
            agent_func: Function that implements the agent logic
            agent_doc: short description of the agent
        """
        cls.agent_func_mapping[key] = agent_func
        cls.agent_doc_mapping[key] = agent_doc

    @classmethod
    def run_registry(cls):
        """Run the registry."""

        cls.register_agent(
            key=AgentKey.FUNDAMENTAL,
            agent_func=fundamental_agent,
            agent_doc="Fundamental analysis specialist focusing on company financial health and valuation."
        )

        cls.register_agent(
            key=AgentKey.COMMODITY_NEWS,
            agent_func=commodity_news_agent,
            agent_doc="Commodity news specialist analyzing futures and commodity-market news."
        )
                
        cls.register_agent(
            key=AgentKey.TECHNICAL,
            agent_func=technical_agent,
            agent_doc="Technical analysis specialist using multiple technical analysis strategies."
        )

        cls.register_agent(
            key=AgentKey.MACROECONOMIC,
            agent_func=macroeconomic_agent,
            agent_doc="Macroeconomic analysis specialist focusing on economic indicators, interest rates, inflation and market trends."
        )

        cls.register_agent(
            key=AgentKey.POLICY,
            agent_func=policy_agent,
            agent_doc="Policy analysis specialist focusing on fiscal and monetary policy."
        )
