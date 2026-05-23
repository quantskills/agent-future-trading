from graph.constants import AgentKey
from llm.prompt import FUTURES_COMMODITY_NEWS_PROMPT
from graph.schema import FundState, AnalystSignal
from llm.inference import agent_call
from apis.router import Router, APISource
from util.db_helper import get_db
from util.logger import logger
from util.text_sanitize import sanitize_visible_text
from tools.agent_tools.quality import (
    apply_signal_quality_gate,
    format_news_summary_for_prompt,
    get_analyst_llm_config,
    llm_path_label,
    summarize_news_events,
    write_analyst_report,
)
from tools.agent_tools.business_quality import apply_business_quality_enrichment
from tools.agent_tools.learning_context import build_learning_context, resolve_config_id

# thresholds
thresholds = {
    "news_count": 10,
}


FUTURES_INSTRUMENT_CONTEXT = {
    "BU": "BU refers to Shanghai bitumen futures in the China futures market. Keep the analysis anchored to refinery output, asphalt demand, road construction, crude-oil cost, inventories, and regional spot-market conditions.",
    "C": "C refers to Dalian corn futures in the China futures market. Keep the analysis anchored to corn supply, planting and harvest progress, imports, feed demand, starch and alcohol processing demand, inventories, and substitution grains.",
    "CF": "CF refers to Zhengzhou cotton futures in the China futures market. Keep the analysis anchored to cotton, cotton yarn, textile demand, planting area, import quotas, and agricultural supply-demand factors.",
    "EB": "EB refers to Dalian styrene futures in the China futures market. Keep the analysis anchored to styrene, benzene, ethylene, downstream ABS/EPS/PS demand, petrochemical operating rates, and chemical supply-demand factors.",
    "HC": "HC refers to Shanghai hot-rolled coil futures in the China futures market. Keep the analysis anchored to flat steel demand, manufacturing, exports, steel-mill output, hot metal, iron ore, coke, inventories, and steel margins.",
    "I": "I refers to Dalian iron ore futures in the China futures market. Keep the analysis anchored to imported iron ore supply, port arrivals, shipments from Australia and Brazil, steel-mill demand, hot metal output, port inventories, and steel margins.",
    "J": "J refers to Dalian coke futures in the China futures market. Keep the analysis anchored to coke supply, coking-plant margins, steel-mill demand, blast furnace activity, port and plant inventories, and steel-chain demand.",
    "M": "M refers to Dalian soybean meal futures in the China futures market. Keep the analysis anchored to soybean meal, soybeans, crushing margins, feed demand, imports, and agricultural supply-demand factors.",
    "MA": "MA refers to Zhengzhou methanol futures in the China futures market. Keep the analysis anchored to methanol supply, coal and natural-gas costs, port inventories, MTO demand, downstream formaldehyde/MTBE/acetic-acid demand, imports, and operating rates.",
    "P": "P refers to Dalian palm oil futures in the China futures market. Keep the analysis anchored to palm oil imports, Malaysian and Indonesian production and exports, domestic inventories, edible-oil demand, soybean oil substitution, and biodiesel policy.",
    "PB": "PB refers to Shanghai lead futures in the China futures market. Keep the analysis anchored to primary and secondary lead supply, smelter margins, battery demand, social and exchange inventories, imports and exports, and LME lead context.",
    "RB": "RB refers to Shanghai Futures Exchange rebar futures in the China futures market. Do not interpret RB as RBOB gasoline or any overseas energy contract. Keep the analysis anchored to rebar, steel mills, iron ore, coking coal, coke, construction demand, infrastructure, and property-related demand.",
    "SR": "SR refers to Zhengzhou white sugar futures in the China futures market. Keep the analysis anchored to domestic cane and beet sugar output, Guangxi production and sales, sugar imports, Brazil export flows, inventories, consumption, and policy factors.",
    "TA": "TA refers to Zhengzhou PTA futures in the China futures market. Keep the analysis anchored to PTA, polyester demand, PX cost, operating rates, downstream textile demand, and chemical-industry supply-demand factors.",
    "ZN": "ZN refers to Shanghai zinc futures in the China futures market. Keep the analysis anchored to zinc mine supply, treatment charges, smelting margins, refined zinc demand, galvanizing and alloy demand, inventories, imports, and LME zinc context.",
}


def commodity_news_agent(state: FundState):
    """Commodity news specialist analyzing China futures news to provide a signal."""
    agent_name = AgentKey.COMMODITY_NEWS
    ticker = state["ticker"]
    trading_date = state["trading_date"]
    llm_config = state["llm_config"]
    portfolio_id = state["portfolio"].id
    market_type = state.get("market_type", "china_futures")
    pre_open_only = state.get("pre_open_only", True)
    info_cutoff = state.get("info_cutoff") or ("pre_open" if pre_open_only else "unspecified")
    cfg = state.get("config", {}) or {}
    full_config = state.get("full_config", cfg) or {}

    if market_type != "china_futures":
        logger.error(
            f"Commodity news analyst only supports china_futures, got market_type={market_type!r}"
        )
        return state

    db = get_db()
    logger.log_agent_status(agent_name, ticker, "Fetching commodity news")

    router = Router(APISource.PANDAAI, market_type=market_type, config=full_config)
    try:
        commodity_news = router.get_china_futures_news(
            ticker=ticker,
            trading_date=trading_date,
            news_count=thresholds["news_count"],
            pre_open_only=pre_open_only,
        )
    except Exception as exc:
        logger.error(f"Failed to fetch futures news for {ticker}: {exc}")
        return state

    news_dict = [item.model_dump_json() for item in commodity_news]
    news_context = summarize_news_events(commodity_news, ticker)
    llm_path = llm_path_label(full_config, "commodity_news")
    analyst_llm_config = get_analyst_llm_config(full_config, "commodity_news")
    instrument_context = FUTURES_INSTRUMENT_CONTEXT.get(
        ticker,
        f"{ticker} is a China futures contract. Keep the analysis anchored to this domestic commodity and its relevant industrial chain.",
    )
    prompt = FUTURES_COMMODITY_NEWS_PROMPT.format(
        ticker=ticker,
        instrument_context=instrument_context,
        news=news_dict,
    )
    prompt += format_news_summary_for_prompt(news_context)
    prompt += (
        f"\n\n=== LLM Path ===\n{llm_path}\n"
        "Return metadata with event_types, event_strength, tradeability, risk_flags, and llm_path. "
        "Do not force a directional signal when tradeability is low.\n"
    )
    learning_context = build_learning_context(
        db=db,
        full_config=full_config,
        config_id=resolve_config_id(db, full_config, state.get("config_id")),
        trading_date=trading_date,
        analyst="commodity_news",
        ticker=ticker,
        context=news_context,
        horizon_class="event_short",
    )
    prompt += learning_context.get("text", "")

    signal = agent_call(
        prompt=prompt,
        llm_config=llm_config,
        pydantic_model=AnalystSignal,
    )

    signal.agent_name = agent_name
    signal.horizon_class = signal.horizon_class if signal.horizon_class != "unknown" else "event_short"
    signal.expected_horizon_days = signal.expected_horizon_days or 2
    signal.market_regime = signal.market_regime if signal.market_regime != "unknown" else str(news_context.get("event_regime") or "event_driven")
    signal.trend_stage = signal.trend_stage if signal.trend_stage != "unknown" else str(news_context.get("tradeability") or "event_window")
    signal.trigger_type = signal.trigger_type if signal.trigger_type != "unknown" else "news_event_trigger"
    signal.entry_type = signal.entry_type if signal.entry_type != "unknown" else "event_probe"
    signal.metadata = {
        **(getattr(signal, "metadata", {}) or {}),
        "llm_path": llm_path,
        "news_context": news_context,
        "cloud_model": analyst_llm_config.get("cloud_model"),
        "reviewer_learning_context": {
            "selected_ids": learning_context.get("selected_ids", []),
            "horizon_class": learning_context.get("horizon_class", "event_short"),
        },
    }
    signal = apply_signal_quality_gate(signal, news_context, full_config, "commodity_news")
    signal = apply_business_quality_enrichment(signal, news_context, full_config, "commodity_news")
    trading_date_value = trading_date.strftime("%Y-%m-%d") if hasattr(trading_date, "strftime") else str(trading_date)
    signal.justification += (
        f"\n[Audit: pre_open_only={pre_open_only}; info_cutoff={info_cutoff}; "
        f"news_cutoff={'<' if pre_open_only else '<='}{trading_date_value}; "
        f"llm_path={llm_path}; tradeability={news_context.get('tradeability')}; "
        f"business_quality={signal.business_quality_score:.2f}; template={signal.template_name}]"
    )
    signal.justification = sanitize_visible_text(signal.justification)

    report_path = write_analyst_report(
        analyst="commodity_news",
        ticker=ticker,
        trading_date=trading_date,
        signal=signal,
        full_config=full_config,
        sections={
            "llm_path": llm_path,
            "tradeability": news_context.get("tradeability"),
            "sector": news_context.get("sector"),
            "Sector Guidance": news_context.get("sector_guidance"),
            "Events Used": news_context.get("events"),
            "Event Type Counts": news_context.get("event_type_counts"),
            "Direction Counts": news_context.get("direction_counts"),
            "Freshness Score": news_context.get("freshness_score"),
            "Relevance Score": news_context.get("relevance_score"),
            "Risk Flags": news_context.get("risk_flags"),
        },
    )
    if report_path:
        signal.metadata["decision_report_path"] = report_path

    logger.log_signal(agent_name, ticker, signal)
    db.save_signal(portfolio_id, agent_name, ticker, prompt, signal)

    return {"analyst_signals": [signal]}
