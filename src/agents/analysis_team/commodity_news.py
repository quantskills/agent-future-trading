from graph.constants import AgentKey
from graph.constants import Signal
from llm.prompt import build_futures_commodity_news_prompt
from graph.schema import FundState, AnalystSignal
from llm.inference import agent_call
from apis.router import Router, APISource
from util.db_helper import get_db
from util.logger import logger
from util.text_sanitize import sanitize_visible_text
from tools.agent_tools.analysis.analyst_quality import (
    apply_signal_quality_gate,
    apply_trade_research_contract,
    format_news_summary_for_prompt,
    get_analyst_llm_config,
    llm_path_label,
    summarize_news_events,
    write_analyst_report,
)
from tools.agent_tools.analysis.analyst_business_quality import apply_business_quality_enrichment
from tools.agent_tools.analysis.analyst_learning_calibration import calibrate_signal_with_learning_context
from tools.agent_tools.analysis.analyst_learning_context import build_learning_context, resolve_config_id
from tools.agent_tools.analysis.analyst_data_usage import build_news_data_usage

# thresholds
thresholds = {
    "news_count": 10,
}


def _build_no_news_signal(
    *,
    ticker: str,
    trading_date,
    agent_name: str,
    news_metadata,
    pre_open_only: bool,
    info_cutoff: str,
) -> AnalystSignal:
    trading_date_value = trading_date.strftime("%Y-%m-%d") if hasattr(trading_date, "strftime") else str(trading_date)
    return AnalystSignal(
        agent_name=agent_name,
        signal=Signal.NEUTRAL,
        confidence=0.0,
        justification=(
            f"{ticker}: no local pre-open futures news available for {trading_date_value}; "
            "commodity news analyst emits explicit no_trade instead of inventing an event catalyst."
        ),
        data_cutoff=info_cutoff,
        no_lookahead_status="ok",
        determinism_mode="deterministic_no_news_no_trade",
        horizon_class="event_short",
        analyst_horizon="event_short",
        expected_horizon_days=0,
        market_regime="no_event",
        trend_stage="no_event",
        setup_type="data_unavailable_no_trade",
        data_freshness="missing",
        event_type="none",
        impact_window_days=0,
        evidence_quality="low",
        business_quality_score=0.0,
        factor_alignment_score=0.0,
        data_coverage_score=0.0,
        tradeability_reason="no local futures news catalyst available",
        opportunity_type="no_trade",
        opportunity_state="no_opportunity",
        setup_quality_score=0.0,
        entry_quality="poor",
        setup_quality_notes=["news_data_unavailable"],
        holding_period_hint="no event trade",
        factor_focus=["news_availability"],
        current_evidence_conflict=["missing_news_catalyst"],
        neutral_reason="news_data_unavailable",
        missing_evidence=["local_futures_news"],
        would_change_view_if="fresh relevant catalyst news appears before the decision cutoff",
        neutral_opportunity_bucket="evidence_gap",
        neutral_trigger_condition="fresh catalyst plus price or volume confirmation",
        counterfactual_side="flat",
        neutral_watchlist_priority="none",
        do_not_trade_reason="news_data_unavailable",
        metadata={
            "data_usage_summary": {
                "ticker": ticker,
                "trading_date": trading_date_value,
                "pre_open_only": pre_open_only,
                "info_cutoff": info_cutoff,
                "data_available": False,
                "data_gap_reason": "local_futures_news_unavailable",
                "metadata": news_metadata or {},
            },
            "analysis_strategy_trace": {
                "analyst": "commodity_news",
                "data_gap_no_trade": True,
                "position_authority_boundary": "no_news_catalyst_cannot_authorize_position",
            },
        },
    )


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
    save_outputs = bool(state.get("save_analyst_outputs", True))
    cfg = state.get("config", {}) or {}
    full_config = state.get("full_config", cfg) or {}

    if market_type != "china_futures":
        message = (
            f"Commodity news analyst only supports china_futures, got market_type={market_type!r}"
        )
        logger.error(message)
        raise RuntimeError(message)

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
        raise RuntimeError(f"Failed to fetch futures news for {ticker}: {exc}") from exc

    if not commodity_news:
        logger.warning(f"{ticker}: No local futures news returned; emitting explicit no_trade signal")
        signal = _build_no_news_signal(
            ticker=ticker,
            trading_date=trading_date,
            agent_name=agent_name,
            news_metadata=getattr(router, "last_news_metadata", None),
            pre_open_only=pre_open_only,
            info_cutoff=info_cutoff,
        )
        prompt = (
            f"{ticker} local futures news unavailable before {trading_date}; "
            "deterministic no_trade artifact produced."
        )
        report_sections = {
            "Data Usage Summary": signal.metadata.get("data_usage_summary"),
            "Reason": "local_futures_news_unavailable",
        }
        if save_outputs:
            report_path = write_analyst_report(
                analyst="commodity_news",
                ticker=ticker,
                trading_date=trading_date,
                signal=signal,
                full_config=full_config,
                sections=report_sections,
            )
            if report_path:
                signal.metadata["decision_report_path"] = report_path
            db.save_signal(portfolio_id, agent_name, ticker, prompt, signal)
        logger.log_signal(agent_name, ticker, signal)
        return {
            "analyst_signals": [signal],
            "analyst_outputs": [
                {
                    "analyst": agent_name,
                    "ticker": ticker,
                    "trading_date": trading_date,
                    "prompt": prompt,
                    "signal": signal,
                    "report_sections": report_sections,
                }
            ],
        }

    news_dict = [item.model_dump_json() for item in commodity_news]
    news_context = summarize_news_events(commodity_news, ticker, trading_date=trading_date)
    llm_path = llm_path_label(full_config, "commodity_news")
    analyst_llm_config = get_analyst_llm_config(full_config, "commodity_news")
    instrument_context = FUTURES_INSTRUMENT_CONTEXT.get(
        ticker,
        f"{ticker} is a China futures contract. Keep the analysis anchored to this domestic commodity and its relevant industrial chain.",
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
    prompt = build_futures_commodity_news_prompt(
        ticker=ticker,
        instrument_context=instrument_context,
        news=news_dict,
        news_summary=format_news_summary_for_prompt(news_context),
        llm_path=llm_path,
        learning_context_text=learning_context.get("text", ""),
    )

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
    signal.setup_type = signal.setup_type if signal.setup_type != "unknown" else "news_event_setup"
    signal.entry_trigger = signal.entry_trigger if signal.entry_trigger != "unknown" else "wait_for_trigger"
    data_usage_summary = build_news_data_usage(
        ticker=ticker,
        trading_date=trading_date,
        news_metadata=getattr(router, "last_news_metadata", None),
        news_context=news_context,
        pre_open_only=pre_open_only,
        info_cutoff=info_cutoff,
    )
    signal.metadata = {
        **(getattr(signal, "metadata", {}) or {}),
        "llm_path": llm_path,
        "news_context": news_context,
        "data_usage_summary": data_usage_summary,
        "cloud_model": analyst_llm_config.get("cloud_model"),
        "reviewer_learning_context": {
            "selected_ids": learning_context.get("selected_ids", []),
            "horizon_class": learning_context.get("horizon_class", "event_short"),
            "memory_trace": learning_context.get("memory_trace", {}),
            "current_day_evidence_required": True,
            "candidate_hypothesis_authority": "prior_only_no_position_authority",
        },
        "analysis_strategy_trace": {
            "analyst": "commodity_news",
            "event_classification_required": True,
            "market_state_adaptation": {
                "event_regime": news_context.get("event_regime"),
                "tradeability": news_context.get("tradeability"),
                "risk_flags": news_context.get("risk_flags"),
            },
            "neutral_to_opportunity_required": True,
            "position_authority_boundary": "news_signal_requires_pm_auditor_trader_confirmation",
        },
    }
    signal = calibrate_signal_with_learning_context(
        signal,
        analyst="commodity_news",
        ticker=ticker,
        learning_context=learning_context,
    )
    signal = apply_signal_quality_gate(signal, news_context, full_config, "commodity_news")
    signal = apply_business_quality_enrichment(signal, news_context, full_config, "commodity_news")
    signal = apply_trade_research_contract(
        signal,
        news_context,
        analyst="commodity_news",
        trading_date=trading_date,
        ticker=ticker,
    )
    trading_date_value = trading_date.strftime("%Y-%m-%d") if hasattr(trading_date, "strftime") else str(trading_date)
    signal.justification += (
        f"\n[Audit: pre_open_only={pre_open_only}; info_cutoff={info_cutoff}; "
        f"news_cutoff={'<' if pre_open_only else '<='}{trading_date_value}; "
        f"llm_path={llm_path}; tradeability={news_context.get('tradeability')}; "
        f"business_quality={signal.business_quality_score:.2f}; setup_type={signal.setup_type}]"
    )
    signal.justification = sanitize_visible_text(signal.justification)

    report_sections = {
        "llm_path": llm_path,
        "tradeability": news_context.get("tradeability"),
        "sector": news_context.get("sector"),
        "Sector Guidance": news_context.get("sector_guidance"),
        "Events Used": news_context.get("events"),
        "Event Type Counts": news_context.get("event_type_counts"),
        "Direction Counts": news_context.get("direction_counts"),
        "Freshness Score": news_context.get("freshness_score"),
        "Relevance Score": news_context.get("relevance_score"),
        "Data Usage Summary": data_usage_summary,
        "Risk Flags": news_context.get("risk_flags"),
    }
    if save_outputs:
        report_path = write_analyst_report(
            analyst="commodity_news",
            ticker=ticker,
            trading_date=trading_date,
            signal=signal,
            full_config=full_config,
            sections=report_sections,
        )
        if report_path:
            signal.metadata["decision_report_path"] = report_path

    logger.log_signal(agent_name, ticker, signal)
    if save_outputs:
        db.save_signal(portfolio_id, agent_name, ticker, prompt, signal)

    return {
        "analyst_signals": [signal],
        "analyst_outputs": [
            {
                "analyst": agent_name,
                "ticker": ticker,
                "trading_date": trading_date,
                "prompt": prompt,
                "signal": signal,
                "report_sections": report_sections,
            }
        ],
    }
