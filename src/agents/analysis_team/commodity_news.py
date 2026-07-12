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
    format_news_summary_for_prompt,
    llm_path_label,
    summarize_news_events,
    write_analyst_report,
)
from tools.agent_tools.analysis.analyst_learning_context import build_learning_context, resolve_config_id
from tools.agent_tools.analysis.analyst_data_usage import build_news_data_usage
from tools.agent_tools.analysis.analyst_output_finalization import (
    finalize_analyst_signal,
    persist_analyst_signal,
    resolve_analyst_llm_config,
)
from tools.agent_tools.analysis.analyst_product_price_behavior_profile import (
    build_profile_usage_contract,
    format_profile_for_commodity_news,
    get_product_price_behavior_profile,
)

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


def commodity_news_agent(state: FundState):
    """Commodity news specialist analyzing China futures news to provide a signal."""
    agent_name = AgentKey.COMMODITY_NEWS
    ticker = state["ticker"]
    trading_date = state["trading_date"]
    llm_config = resolve_analyst_llm_config(state)
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

    product_profile = get_product_price_behavior_profile(ticker, full_config)
    product_profile_usage = build_profile_usage_contract(ticker, "commodity_news", product_profile)

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
        signal = finalize_analyst_signal(
            signal,
            quality_context={
                "sector": str(product_profile.get("sector") or ""),
                "tradeability": "low",
                "risk_flags": ["news_data_unavailable"],
                "event_regime": "no_event",
            },
            full_config=full_config,
            analyst="commodity_news",
            ticker=ticker,
            trading_date=trading_date,
            learning_context={},
            product_profile=product_profile,
            product_profile_usage=product_profile_usage,
        )
        prompt = (
            f"{ticker} local futures news unavailable before {trading_date}; "
            "deterministic no_trade artifact produced.\n"
            + format_profile_for_commodity_news(ticker, product_profile)
        )
        report_sections = {
            "Data Usage Summary": signal.metadata.get("data_usage_summary"),
            "Product Price Behavior Profile": signal.metadata.get("product_profile_evidence"),
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
            persist_analyst_signal(
                db,
                portfolio_id=portfolio_id,
                analyst=agent_name,
                ticker=ticker,
                prompt=prompt,
                signal=signal,
            )
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
    news_context["product_profile_evidence"] = product_profile_usage
    llm_path = llm_path_label(full_config, "commodity_news")
    instrument_context = str(product_profile.get("product_context") or "")
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
        product_profile_context=format_profile_for_commodity_news(ticker, product_profile),
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
        "product_profile_evidence": product_profile_usage,
        "data_usage_summary": data_usage_summary,
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
            "product_profile_evidence": {
                "product_profile_id": product_profile_usage.get("product_profile_id"),
                "profile_learning_interaction": product_profile_usage.get("profile_learning_interaction"),
            },
            "neutral_to_opportunity_required": True,
            "position_authority_boundary": "news_signal_requires_pm_auditor_trader_confirmation",
        },
    }
    signal = finalize_analyst_signal(
        signal,
        quality_context=news_context,
        full_config=full_config,
        analyst="commodity_news",
        ticker=ticker,
        trading_date=trading_date,
        learning_context=learning_context,
        product_profile=product_profile,
        product_profile_usage=product_profile_usage,
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
        "Product Price Behavior Profile": signal.metadata.get("product_profile_evidence"),
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
        persist_analyst_signal(
            db,
            portfolio_id=portfolio_id,
            analyst=agent_name,
            ticker=ticker,
            prompt=prompt,
            signal=signal,
        )

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
