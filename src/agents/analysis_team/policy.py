from graph.schema import FundState


def policy_agent(state: FundState):
    """Retired legacy analyst.

    AgentQuant futures mode reads futures news from local Finoview txt files;
    this legacy external-data policy analyst is intentionally not registered.
    """
    raise RuntimeError(
        "policy_agent is retired. Futures news is loaded by commodity_news from "
        "local Finoview text files."
    )
