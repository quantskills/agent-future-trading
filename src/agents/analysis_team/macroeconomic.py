from graph.schema import FundState


def macroeconomic_agent(state: FundState):
    """Retired legacy analyst.

    AgentQuant futures mode only uses PandaAI futures data plus local Finoview
    fundamentals/news. This legacy external-data analyst is intentionally not
    registered.
    """
    raise RuntimeError(
        "macroeconomic_agent is retired. AgentQuant futures mode uses technical, "
        "fundamental, and commodity_news analysts only."
    )
