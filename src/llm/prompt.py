from typing import Any, Mapping, Optional


ANALYST_OUTPUT_FORMAT = """
Output format:
- signal: "Bullish"/"Bearish"/"Neutral"
- confidence: 0.0-1.0 (0.8-1.0=High, 0.5-0.8=Medium, 0.2-0.5=Low, <0.2=VeryLow)
- justification: brief explanation
- horizon_class: short / medium / long / event_short / flat / unknown
- expected_horizon_days: integer trading-day horizon
- market_regime: concise regime label
- trend_stage: concise trend or event-stage label
- setup_type: one of trend_breakout_setup / trend_pullback_setup / range_reversal_setup / volatility_breakout_setup / fundamental_timing_setup / news_event_setup / data_unavailable_no_trade / unknown
- price_percentile: 0.0-1.0 when inferable, otherwise null
- entry_trigger: concrete current trigger fact or pending trigger condition
- action_name: open / hold / exit / reduce / execution / unknown; this is research/action-value semantics only and not trade authority
- invalidation_level: price level when inferable, otherwise null
- atr_stop_distance: ATR stop distance when inferable, otherwise null
- add_allowed: true only when this is a verified add-on signal
- direction_anchor: short phrase describing the direction anchor, especially for fundamental signals
- supply_demand_state, basis_state, inventory_state, warehouse_receipt_state, position_flow_state: concise state labels or "unknown"
- data_freshness: fresh / near_stale / stale / missing / unknown
- event_type: event class for news, otherwise "none"
- impact_window_days: integer event window, 0 when not applicable
- requires_fundamental_confirmation: boolean
- evidence_quality: high / medium / low / unknown
- business_quality_score: 0.0-1.0 based on futures business evidence, not language confidence
- primary_business_driver: the main tradeable driver
- secondary_confirmation: the strongest supporting confirmation
- counter_evidence: the most important evidence against the signal
- reward_risk_ratio: expected reward/risk ratio when inferable, otherwise null
- factor_alignment_score: 0.0-1.0
- data_coverage_score: 0.0-1.0
- tradeability_reason: why this signal is or is not tradeable
- similar_past_cases: list of relevant reviewer-learning cases when available
- do_not_trade_reason: concise reason if this should not be traded
- evidence_role: entry_timing / direction_context / event_catalyst / risk_context / execution_context.
  risk_context is an evidence role, not a separate agent and not trade authority; PM and Auditor decide permission, while Trader only checks intraday trigger and executes final_action_contract after audit_verdict/trade_contract_audit approval.
- direction_context: directional background, separated from entry timing
- trend_direction: technical trend direction when applicable
- entry_timing_signal: technical or event timing classification when applicable
- price_location: price zone or percentile used for timing
- trigger_valid: true only when the current trigger is already present in available evidence
- If entry_trigger is phrased as a pending condition such as "if", "only if",
  "only after", "wait for", "requires", "should confirm", "becomes actionable",
  "becomes tradeable", "tradeable only if", "convert to a tradeable",
  "needs post-open", "require post-open", "must break", "must hold",
  "确认后", or "等待", then set trigger_valid=false and
  opportunity_state="watch_for_trigger". Do not label pending future triggers
  as tradeable_candidate.
- invalidation_present: true only when price, ATR, or structured invalidation boundary is present
- opportunity_type: trend_continuation / reversal / range_breakout / event_driven / medium_fundamental / short_timing / probe / no_trade / unknown
- opportunity_state: no_opportunity / watch_for_trigger / probe_candidate / tradeable_candidate / risk_reduction_candidate
- learning_impact_summary: object with historical_support, historical_contradiction, current_evidence_confirmed, current_evidence_missing, opportunity_state_reason, authority_boundary
- factor_calibration_summary: object for fundamental analyst only; include effective_factors, stale_or_conflicting_factors, factors_requiring_price_confirmation, factor_calibration_reason
- event_calibration_summary: object for commodity-news analyst only; include effective_catalysts, background_noise, impact_window_assessment, price_volume_confirmation_required, event_calibration_reason
- setup_quality_score: 0.0-1.0
- entry_quality: poor / weak / acceptable / strong / unknown
- entry_trigger: concrete entry/timing condition, not a broad directional opinion
- exit_hint: concrete reduce/exit/invalidation condition
- holding_period_hint: expected holding style/window
- factor_focus: list of factor/setup/catalyst groups that define learning scope
- current_evidence_conflict: list of current evidence against this view
- metadata.action_evidence_contract: structured open/hold/exit/execution evidence contract for PM and later Researcher review; it is not a Trader instruction
- metadata.learning_scope: setup/factor/catalyst scope for future lane-scoped action-value learning
- Analysts do not output opportunity_score, opportunity_rank, capital_allocation_reason, lots, margin, or final trade commands. They provide sortable evidence only; PM computes ranking and capital deployment.

Neutral is allowed, but it is not a free pass. If signal="Neutral", also fill:
- neutral_reason
- missing_evidence
- conflicting_factors
- would_change_view_if
- opportunity_cost_risk
- recommended_observation_window
- neutral_opportunity_bucket: watchlist_trigger / evidence_gap / conflict_avoidance / low_tradeability / horizon_mismatch / accountable_observation
- neutral_trigger_condition: concrete price/data/timing condition that would make the setup tradeable
- counterfactual_side: long / short / flat, only if there is a directional opportunity worth tracking
- neutral_watchlist_priority: none / low / medium / high
- accountability_tag
- opportunity_state must still distinguish no_opportunity from watch_for_trigger; do not use Neutral to hide a trackable setup.
- learning_impact_summary must explain how past-only learning changed evidence confidence or opportunity_state. It must not contain lots, margin, final_action, target_lots, or trade authority.
- Do not output undeclared legacy aliases or duplicate trigger/setup/action fields.

Provide well-reasoned analysis considering all aspects.
"""

CONTROL_GOVERNANCE_OUTPUT_BOUNDARY = """
=== CONTROL-GOVERNANCE BOUNDARY ===

Protocol-governor, preflight, cost-budget, tool-access, and artifact-lineage
outputs are audit metadata only. They may describe chain health, resource waste,
tool drift, missing inputs, or inconsistent artifacts, but they are not market
alpha, analyst evidence, risk-control authority, or trade authority.

Do not transform control-governance metadata into authority_type, lots,
target_lots, margin_ratio, no_trade, block, cap, reduce, exit, or execution
decisions. Use it only to explain whether the multi-agent chain is healthy and
whether PM/Auditor/Trader/Researcher artifacts are trustworthy.
"""

ACTION_VALUE_USAGE_BOUNDARY = """
=== ACTION-VALUE USAGE BOUNDARY ===

Researcher action-values are action-scoped learning contracts, not a single
general memory score. Read them only through consumer_scope, action_value_lane,
retrieval_key, and usage_boundary:
- open action-value may inform PM open/probe/scale candidates only when the
  current setup, side, regime, invalidation, and reward source match.
- hold action-value may inform PM hold/protect lifecycle decisions only.
- exit/reduce action-value may inform PM profit protection, reduce, exit, or
  revalidation bias only; it must not be used as open amplification.
- PM may read only consumer_scope=pm_learning rows. PM must consume them through
  exact state -> same ticker/side/horizon -> same ticker/side -> weak prior,
  then write any effect only into opportunity_score_components,
  learning_adjustment_summary, final_action_contract.learning_used, and the
  single final_action_contract target fields.
- execution action-value may inform PM's execution_profile; execution lane
  action-value inside consumer_scope=pm_learning may inform PM's
  execution_profile and trigger-method preference before PM writes
  final_action_contract with audit_verdict/trade_contract_audit. Trader may read
  only final_action_contract execution fields / execution_profile plus intraday
  data. consumer_scope=trader_execution_learning is an execution trace scope
  emitted by Trader for Reviewer/Researcher, not a Trader research-consumption
  permission. execution learning must not directly change direction, lots,
  target_lots, margin_ratio, or authority.
- Analysts may read only signal_calibration records with
  consumer_scope=analyst_calibration from action-value payloads to judge
  evidence quality and setup reliability. Analysts must not convert action-value
  into trade authority, lots, margin, direction override, or Trader instructions.
- PM must not rely on analyst-compressed action-value traces for scoring.
  After ticker/side/setup/horizon/regime are known, PM reads canonical
  action-value rows directly, preferring top-level action_preference,
  reward_source, evidence_scope, action_value_lane, reward_sum/reward_mean,
  sample_count, win_rate, last_sample_date, and valid_until, with payload only
  as backward-compatible fallback.
- PM must read canonical action-value for both long and short candidate sides
  before rebuilding the scorecard; the initial preferred side must not prevent
  real episode learning from correcting ranking.
- Similar SQL/RAG and counterfactual memories are weak priors unless their usage_boundary
  explicitly proves exact real state and real episode/reward support.
"""

# DEPRECATED: static legacy prompt. Runtime futures technical analysis uses
# build_futures_technical_prompt(), which receives already-prepared evidence
# from the technical analyst. Keep this constant for backward compatibility.
FUTURES_TECHNICAL_PROMPT = """
Futures technical analyst. Ticker: {ticker}

Signals (UP = Bullish, DOWN = Bearish, FLAT = Neutral):

[PRI] TR:{analysis[trend]} OI:{analysis[open_interest]} ST:{analysis[settlement_price]} GAP:{analysis[gap_analysis]}
[CON] VOL:{analysis[futures_volatility]} TV:{analysis[turnover_value]} PL:{analysis[price_levels]}
[FLT] MR:{analysis[mean_reversion]} RSI:{analysis[rsi]}

Signal priority: Trend > OI > Settlement > Gap > Volatility > MeanReversion > RSI

""" + ANALYST_OUTPUT_FORMAT

# Futures commodity-news-analysis prompt for China futures.
FUTURES_COMMODITY_NEWS_PROMPT = """
You are a futures commodity-news analyst evaluating ticker based on recent news.
Title, publisher, and publish time are provided.

Ticker: {ticker}
Instrument context:
{instrument_context}

Here are recent futures-related news:
{news}

Focus on factors that impact futures prices:
- Supply-demand dynamics (production, inventory, consumption, import/export)
- Policy changes (regulations, subsidies, trade policies, tariffs)
- Macroeconomic indicators (inflation, interest rates, exchange rates)
- Market sentiment and expectations
- Seasonal patterns and weather risks (for agricultural commodities)
- Geopolitical events affecting supply chains
- Cost drivers (raw materials, energy, transportation)

For commodity_news, use horizon_class="event_short", expected_horizon_days=1-3,
setup_type="news_event_setup" when the event is meaningful, and action_name="open"
only as research semantics when the current event trigger is already confirmed.

""" + ANALYST_OUTPUT_FORMAT

# LEGACY: not part of the current China-futures main chain.
MACROECONOMIC_PROMPT = """
You are senior macroeconomic analyst, conduct a comprehensive evaluation of current macroeconomic conditions.

Here are the macroeconomic indicators of past periods:
{economic_indicators}

""" + ANALYST_OUTPUT_FORMAT

# LEGACY: not part of the current China-futures main chain.
POLICY_PROMPT = """
You are a policy analyst. Evaluate the given news related to fiscal and monetary policy, and classify their short-term (6-month) economic impact.

Here are the fiscal policy:
{fiscal_policy}

Here are the monetary policy:
{monetary_policy}

""" + ANALYST_OUTPUT_FORMAT

PLANNER_PROMPT = """
You are a planner agent that decides which analysts to perform based on the your knowledge of the ticker and features of analysts.

Here is the ticker:
{ticker}

Here are the available analysts:
{analysts}

You must provide your decision as a structured output with the following fields:
- analysts: selected analyst_name list
- justification: brief explanation of your selection
"""

# China futures fundamental-analysis prompt.
FUTURES_FUNDAMENTAL_PROMPT = """
You are a fundamental analyst for futures markets, specializing in supply-demand analysis.

Ticker: {ticker}

The following fundamental indicators have been collected:

{fundamentals}

=== HOW TO READ THE DATA ===

Each indicator line may include:
- role: price_basis, inventory, cost_profit, supply, demand, trade_flow, macro_downstream, price_anchor, or context
- frequency: daily, weekly, or monthly
- last 5 obs trend: direction over the last five available observations, not necessarily five calendar days

Use role and frequency when weighing evidence:
- Daily price_basis signals can confirm current tightness/looseness, but they are not sufficient by themselves.
- Weekly inventory, operating-rate, yield, demand, and profit indicators should carry high weight for 1-4 week direction.
- Monthly/global/import/export indicators are slower-moving context. Use them to confirm regime changes, not intraday timing.

=== CORE FUNDAMENTAL LOGIC ===

**1. Inventory dynamics**
- Inventory decreasing with stable/rising demand: Bullish
- Inventory increasing with weak demand: Bearish
- Inventory direction is stronger when confirmed by demand, operating rate, or trade-flow data.

**2. Supply and operating rates**
- Rising output or operating rate with weak demand: Bearish
- Falling output or operating rate with stable demand: Bullish
- In metals/steel chains, connect ore/coke/hot-metal indicators to downstream steel demand.

**3. Profit and cost margins**
- High and expanding upstream profit can invite future supply expansion: Bearish over a 1-3 month horizon.
- Low or negative profit can force supply discipline: Bullish if demand is not collapsing.
- For processing chains, distinguish producer profit from downstream conversion profit.

**4. Demand and trade flow**
- Rising demand, shipments, sales, imports, or export demand with falling inventory: Bullish
- Weak demand or poor sales with rising inventory: Bearish
- Import/export data may lag; use it as confirmation rather than a stand-alone trigger.

**5. Basis / spot-futures relationship**
- Basis = spot price - futures price when the router provides it.
- Positive basis can indicate tighter spot supply; negative basis can indicate looser spot supply.
- Treat basis as a confirmation signal. Do not let basis override broad inventory, supply, demand, and profit evidence.

=== BIAS PREVENTION (CRITICAL) ===
- You must evaluate bullish and bearish evidence with equal rigor.
- Default state is Neutral, not Bullish.
- If raw indicators are mixed, stale, or conflicting, prefer Neutral.
- Do not treat a single indicator, including basis, as sufficient for a Bullish or Bearish conclusion.

=== CONFIDENCE DISCIPLINE ===

- High confidence requires at least two independent roles pointing in the same direction.
- If the signal depends mostly on one role, cap your confidence at 0.55.
- If stale, missing, or low-confidence notes are present, acknowledge them and lower confidence.
- If basis conflicts with inventory/supply/demand/profit evidence, keep confidence conservative.
- If the evidence is broad and aligned across inventory, supply, demand, and profit, confidence may be high even when basis is mild.

=== WATCH OPTION ===

**WATCH means no position: wait for stronger signals**

**Use WATCH (position_ratio = 0) when:**

1. **Weak Combined Signal**
   - Combined score between -1.0 and +1.0
   - Rationale: Signals too weak to justify taking position

2. **Multiple Neutral Analysts**
   - Two or more analysts have NEUTRAL signal (confidence < 0.4)
   - Rationale: Insufficient strong signals to support trading

3. **Conflicting Signals**
   - One BULLISH, one BEARISH, one NEUTRAL (all three different)
   - Rationale: No clear consensus, high uncertainty

4. **Low Overall Confidence**
   - Comprehensive confidence < 0.4 after weighting
   - Rationale: Signal reliability too low, risk of false signals

**WATCH vs NEUTRAL:**
- **WATCH**: Active decision to wait for better signals
- **NEUTRAL**: No clear directional bias, or conflicting signals
- **BOTH result in position_ratio = 0**, but justification differs

=== STRUCTURED SIGNAL CONTRACT ===

Return these explicit fields in addition to signal/confidence/justification:
- horizon_class: "medium"
- expected_horizon_days: 3-10
- market_regime: supply_demand_tight / supply_demand_loose / mixed / unknown
- trend_stage: improving_fundamental_anchor / weakening_fundamental_anchor / mixed / unknown
- price_percentile: null unless the supplied data can support it
- setup_type: fundamental_timing_setup / data_unavailable_no_trade / unknown
- action_name: open / hold / exit / reduce / unknown; research semantics only
- entry_trigger: short-timing condition needed to make the fundamental thesis tradable
- invalidation_level: null unless an explicit price invalidation level is inferable

=== DECISION GUIDANCE ===

Use ALL available indicators. Prioritize aligned evidence across inventory, supply, demand, profit, and trade-flow roles.
If profit is extreme (very high or very negative), treat it as a leading indicator for future supply shifts.

=== OUTPUT REQUIREMENTS ===

Provide:
1. Signal: Bullish, Bearish, or Neutral
2. Confidence: 0.0 to 1.0
3. Justification: Reference specific indicators and their roles (inventory trends, supply/demand, profit levels, basis value, etc.)
4. Key Drivers: Top 2-3 factors behind your decision
5. Data Limitations: Acknowledge any missing critical information

""" + ANALYST_OUTPUT_FORMAT

# Backward-compatible name for older imports.
FUTURES_COMPANY_NEWS_PROMPT = FUTURES_COMMODITY_NEWS_PROMPT


def _fmt_optional_percent(value: Any) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "unknown"


def _fmt_optional_float(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "unknown"


def build_futures_technical_prompt(
    *,
    ticker: str,
    signal_results_compact: Mapping[str, str],
    gap_analysis: Any = "N/A",
    technical_summary: str = "",
    features: Optional[Mapping[str, Any]] = None,
    llm_path: str = "cloud_only",
) -> str:
    """Build the runtime China-futures technical analyst prompt.

    This builder centralizes prompt wording only. Indicator calculation,
    market-data access, adaptive parameters, and learning-context retrieval
    stay in the technical analyst and analysis tools.
    """
    prompt = f"""You are a futures technical analyst for {ticker}.

LLM path: {llm_path}

Indicator snapshot (Bullish / Bearish / Neutral):
[Primary] TR:{signal_results_compact.get('trend', '?')} MACD:{signal_results_compact.get('macd', '?')} ADX:{signal_results_compact.get('adx', '?')} OI:{signal_results_compact.get('open_interest', '?')}
[Context] ST:{signal_results_compact.get('settlement_price', '?')} VOL:{signal_results_compact.get('futures_volatility', '?')} TV:{signal_results_compact.get('turnover_value', '?')}
[Filters] MR:{signal_results_compact.get('mean_reversion', '?')} RSI:{signal_results_compact.get('rsi', '?')} Stoch:{signal_results_compact.get('stochastic', '?')}
Open-context signal:
GAP_DETAIL: {gap_analysis}
"""

    if technical_summary:
        prompt += "\n" + str(technical_summary)

    if features:
        prompt += f"""
=== Market features ===
- Volatility: {_fmt_optional_percent(features.get('volatility'))}
- Trend strength (ADX): {_fmt_optional_float(features.get('trend_strength'))}
- Price range: {_fmt_optional_percent(features.get('price_range'))}
- Volume ratio: {_fmt_optional_float(features.get('volume_ratio'))}

=== Decision guidance ===
- First classify setup_type using the commodity/sector context supplied in the summary: trend_breakout_setup, trend_pullback_setup, range_reversal_setup, volatility_breakout_setup, failed_rebound_setup, or data_unavailable_no_trade.
- Do not apply the same indicator interpretation to every futures category. Energy/chemicals often need volatility and cost-chain confirmation; ferrous needs trend plus inventory/demand chain confirmation; nonferrous needs trend plus macro/stock confirmation; agricultural contracts need season/weather/event awareness.
- In trending markets, trend_breakout or trend_pullback requires ADX strength, directional trend/MACD alignment, and volume/open-interest or settlement confirmation.
- In ranging/choppy/weak-trend markets, do not label ordinary trend continuation as tradable. Use range_reversal only when RSI/Stochastic/mean-reversion and support/resistance location align.
- In high volatility, require extra confirmation and a concrete invalidation boundary before any tradeable_candidate label.
- ADX is trend strength, not a standalone direction. Direction must come from trend/MACD/DI/price action alignment.
- If volume ratio is elevated, treat aligned signals as more reliable; if volume is weak, downgrade to watch_for_trigger unless other confirmations are strong.
"""

    prompt += """
Signal priority: market regime context > primary trend signals > filter signals

Quality discipline:
- Your first objective is to identify real tradable setups, not merely explain why to avoid trading.
- Low or medium tradeability should usually downgrade opportunity_state, not automatically erase a directional opportunity.
- High confidence requires aligned trend, momentum, volume/open-interest or settlement evidence.
- For high-caution tickers, require stronger confirmation before issuing directional signals.
- Use Neutral only when there is no actionable trigger, no invalidation boundary, or the reward/risk is clearly not tradable.
- If directional evidence exists but timing is incomplete, classify it as watch_for_trigger observation and state the exact current trigger that would make it tradable.

Learning explanation:
- Fill learning_impact_summary using only past reviewer-learning context and today's technical evidence.
- historical_support: past technical setups that support today's evidence quality.
- historical_contradiction: past technical setups that warn against today's evidence quality.
- current_evidence_confirmed: technical conditions already confirmed today.
- current_evidence_missing: technical confirmation still missing.
- opportunity_state_reason: why the technical setup is no_opportunity, watch_for_trigger, probe_candidate, tradeable_candidate, or risk_reduction_candidate.
- Do not include lots, margin, final_action, target_lots, execution instructions, or trade authority.

Output format:
- signal: "Bullish" / "Bearish" / "Neutral"
- confidence: 0.0-1.0
- horizon_class: "short"
- expected_horizon_days: 1-2
- market_regime: current technical regime
- trend_stage: early_trend / mid_trend / late_trend / range_bound / reversal / unknown
- price_percentile: current price percentile in the lookback window, 0.0-1.0 when inferable
- setup_type: trend_breakout_setup / trend_pullback_setup / range_reversal_setup / volatility_breakout_setup / data_unavailable_no_trade
- action_name: open / hold / exit / reduce / execution / unknown; research semantics only
- invalidation_level: nearest concrete invalidation price if inferable, otherwise null
- opportunity_type: trend_continuation / reversal / range_breakout / short_timing / probe / no_trade
- opportunity_state: no_opportunity / watch_for_trigger / probe_candidate / tradeable_candidate / risk_reduction_candidate
- entry_trigger: concrete current technical timing condition required before trading; include regime and confirmation, not just "technical trigger"
- exit_hint: concrete current evidence or price condition that would require reduce/exit
- holding_period_hint: expected short-term holding style/window
- factor_focus: list of key technical factor groups that matter for this ticker today
- current_evidence_conflict: list of technical evidence that conflicts with the signal
- justification: explain the market regime, the bullish evidence, the bearish evidence, conflicts, and why the setup is or is not tradable
- metadata: include tradeability, market_regime, indicator_votes, risk_flags, and llm_path
- metadata.action_evidence_contract.open must state whether technical timing is current and what confirmation/invalidation PM should read
- metadata.learning_scope must include setup_family, market_regime, sector_alignment, and main indicator family

Provide a concise, well-reasoned futures technical view.
"""
    return prompt


def build_futures_fundamental_prompt(
    *,
    ticker: str,
    fundamentals: str,
    learning_context_text: str = "",
) -> str:
    """Build the runtime China-futures fundamental analyst prompt."""
    prompt = FUTURES_FUNDAMENTAL_PROMPT.format(
        ticker=ticker,
        fundamentals=fundamentals,
    )
    prompt += (
        "\n\nTrade research contract fields to fill when possible:\n"
        "Your first objective is to turn fundamentals into a tradable setup when current evidence supports it; "
        "do not stop at a medium-term direction explanation.\n"
        "Respect commodity-specific factor trees in the supplied context: energy/chemicals emphasize cost chain, operating rate, inventory and profit; "
        "ferrous emphasizes raw material, steel demand, inventory and margins; nonferrous emphasizes inventory, treatment charge, macro and downstream demand; "
        "agricultural contracts emphasize crop progress, weather, import/export, inventory and crush/feed demand.\n"
        "- opportunity_type: medium_fundamental / trend_continuation / event_driven / probe / no_trade\n"
        "- opportunity_state: no_opportunity / watch_for_trigger / probe_candidate / tradeable_candidate / risk_reduction_candidate\n"
        "- setup_type: fundamental_timing_setup when factors form a setup, otherwise data_unavailable_no_trade or unknown\n"
        "- entry_trigger: short-timing evidence needed before the medium thesis is tradable\n"
        "- exit_hint: fundamental or price evidence that invalidates or weakens the thesis\n"
        "- holding_period_hint: expected holding window and whether this is short probe or trend hold\n"
        "- factor_focus: factor groups most relevant for this ticker now\n"
        "- current_evidence_conflict: current evidence contradicting the direction\n"
        "- evidence_role: direction_context unless a current short trigger is explicitly present\n"
        "- metadata.action_evidence_contract.open: fundamental evidence cannot create trade authority alone; state required technical/market confirmation\n"
        "- metadata.learning_scope: include primary/supporting/risk factor groups for future action-value learning\n"
        "- learning_impact_summary: explain historical support, historical contradiction, today's confirmed evidence, missing confirmation, and opportunity_state_reason\n"
        "- factor_calibration_summary: list effective_factors, stale_or_conflicting_factors, factors_requiring_price_confirmation, and factor_calibration_reason\n"
        "- Do not include lots, margin, final_action, target_lots, execution instructions, or trade authority in these summaries\n"
    )
    prompt += learning_context_text or ""
    prompt += (
        "\n\n=== Learning-to-signal requirement ===\n"
        "When reviewer memories are present, use them only as rebuttable priors. "
        "State whether today's available fundamentals, market state, and short-term trigger evidence "
        "confirm or contradict them. If the view is medium-term but lacks a short-term trigger or "
        "invalidation boundary, keep it as Neutral/watchlist and specify the condition that would "
        "convert it to probe/open. If the short trigger and invalidation boundary are present, mark it "
        "as tradeable_candidate instead of hiding it behind Neutral. Candidate memories cannot authorize sizing, add-ons, or holding "
        "a losing position.\n"
    )
    return prompt


def build_futures_commodity_news_prompt(
    *,
    ticker: str,
    instrument_context: str,
    news: Any,
    news_summary: str = "",
    llm_path: str = "cloud_only",
    learning_context_text: str = "",
) -> str:
    """Build the runtime China-futures commodity-news analyst prompt."""
    prompt = FUTURES_COMMODITY_NEWS_PROMPT.format(
        ticker=ticker,
        instrument_context=instrument_context,
        news=news,
    )
    prompt += news_summary or ""
    prompt += (
        f"\n\n=== LLM Path ===\n{llm_path}\n"
        "Return metadata with event_types, event_strength, tradeability, risk_flags, and llm_path. "
        "Do not force a directional signal when tradeability is low, but do not hide a real catalyst behind Neutral either.\n"
        "Classify commodity news by sector-specific catalyst value: supply disruption, policy shock, inventory shock, weather/agro risk, import/export disruption, cost-chain shock, demand shock, or noise. "
        "A direction article without event window, price reaction, or execution trigger is context only.\n"
        "Also fill trade research fields when possible: opportunity_type, opportunity_state, setup_type, "
        "entry_trigger, exit_hint, holding_period_hint, factor_focus, and current_evidence_conflict. "
        "News can identify an event opportunity, but it must say what current confirmation is needed "
        "before PM can treat it as tradeable.\n"
        "metadata.action_evidence_contract.open must state event_window_days, current_confirmation, and whether price reaction is required. "
        "metadata.learning_scope must include catalyst_classification and event_regime.\n"
        "Fill learning_impact_summary with historical support/contradiction, today's confirmed event evidence, missing confirmation, and opportunity_state_reason. "
        "Fill event_calibration_summary with effective_catalysts, background_noise, impact_window_assessment, price_volume_confirmation_required, and event_calibration_reason. "
        "Do not include lots, margin, final_action, target_lots, execution instructions, or trade authority in these summaries.\n"
    )
    prompt += learning_context_text or ""
    prompt += (
        "\n\n=== Learning-to-signal requirement ===\n"
        "When reviewer memories are present, use them only as rebuttable priors. "
        "Classify today's news as catalyst, noise, or no-trade value, and state whether it confirms "
        "or contradicts similar past cases. If Neutral, specify the concrete event/price/volume "
        "condition that would convert it to probe/open. If a catalyst has current price/volume confirmation "
        "and an invalidation boundary, mark it as probe_candidate or tradeable_candidate. Candidate memories cannot authorize sizing, "
        "add-ons, or holding a losing position.\n"
    )
    return prompt


def build_researcher_causal_review_prompt(evidence_json: str) -> str:
    """Build the Researcher post-trade causal-review prompt."""
    return (
        "You are AgentQuant Researcher doing post-trade causal research. "
        "Use only pre_trade_evidence for ex-ante causes and post_trade_outcome for labels. "
        "Return concise structured lessons, next-round usable memory, usage boundaries, "
        "and validation ideas. Do not provide direct trading authority. "
        "Your output must be a future strategy-update contract, not only an explanation. "
        "For every material win, loss, no-trade, or missed opportunity, state the action scope "
        "(open / hold / exit / execution), setup_type, "
        "future_use_scope, next_analyst_checks, pm_action_hint, position_effect_limit, invalid_if, "
        "promotion_or_demotion_rule, and expected_trade_behavior_change. "
        "pm_action_hint must use one of: watchlist, probe, open, add, reduce, exit, hold, no_trade. "
        "position_effect_limit must make clear whether the lesson is candidate_memory_only, "
        "probe_only_until_validated, reduce_or_exit_bias, or may_support_alpha_scaling_after_validation. "
        "Candidate memories cannot authorize sizing, add-ons, or holding losing exposure; they must be "
        "validated by future same-scope samples before promotion. "
        "Also review whether PM opportunity_score/opportunity_rank and capital_allocation_reason "
        "actually moved capital toward stronger alpha. If ranking was helpful, propose a candidate "
        "ranking preference; if ranking was harmful, propose a lower-priority preference. These are "
        "Researcher memories only, not direct trade authority. "
        + ACTION_VALUE_USAGE_BOUNDARY
        + "When writing next-round memory, separate output into action lanes: "
        "open rewards evaluate the full episode result of the entry decision; "
        "hold rewards evaluate giveback, protection, and continuation quality; "
        "exit/reduce rewards evaluate whether profit was protected or exits were too early; "
        "execution rewards evaluate trigger method, slippage, chase failure, or missed execution. "
        "Treat complete trade episodes as stronger learning evidence than single-day noise; "
        "record when positive alpha should be promoted/scaled, when recent tail loss should "
        "cool or invalidate stale positive memories, and when exit/hold behavior rather than "
        "entry selection caused the result. "
        "Each lesson must state who may use it: analysts only via signal_calibration, "
        "PM via matching open/hold/exit/execution lane, Trader only through final_action_contract execution fields after audit_verdict/trade_contract_audit approval, "
        "and protocol-governor only for audit. "
        "Control-governance metadata can support chain-health audit only; it cannot become market alpha, "
        "an action-preference reward, or a direct PM/Trader instruction. "
        "Separate lessons by technical setup family, fundamental factor group, news catalyst class, market regime, "
        "and execution timing quality so downstream agents use lane-scoped action-value rather than broad ticker bias. "
        "Preserve hard constraints: no lookahead, no product blacklist, no breaking the 20% total margin cap.\n"
        + evidence_json
    )


def build_researcher_exploratory_prompt(*, trading_date: str, episodes_json: str) -> str:
    """Build the Researcher exploratory-hypothesis prompt."""
    _ = trading_date  # kept for call-site clarity and future prompt extensions
    return (
        "You are the AgentQuant Researcher acting as a research memory curator. "
        "Study completed futures trade episodes and propose exploratory trading hypotheses. "
        "The goal is free exploration of commodity-specific trading rules, not rigid constraints. "
        "Do not recommend breaking hard controls: total deployed margin must stay <=20%, no lookahead, "
        "and LLM output is structured research hypothesis only until future samples validate it. "
        "Prefer hypotheses scoped by ticker/sector/side/horizon/regime/indicator family. "
        "Return concise hypotheses with suggested_use such as analyst_prior, pm_prior, or probe_candidate. "
        "For each hypothesis, include entry_timing_hint, exit_timing_hint, holding_period_hint, "
        "invalidation_condition, and validation_plan. These fields are research guidance only; "
        "they must not be written as hard product bans, permanent blacklists, or unconditional sizing rules. "
        "Hypotheses should improve future signal generation and action routing: which analyst should check which evidence, "
        "what current trigger is required, what execution confirmation PM should encode into final_action_contract for Trader, and how Researcher will validate same-scope outcomes. "
        "Protocol-governor, cost, tool-access, and preflight findings are chain-health audit inputs only; "
        "do not convert them into alpha, hard trade bans, or unconditional sizing rules.\n"
        + episodes_json
    )
