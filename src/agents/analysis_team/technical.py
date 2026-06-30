import math
try:
    import pandas as pd
    _PANDAS_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - depends on runtime environment
    class _PandasStub:
        DataFrame = object
        Series = object

    pd = _PandasStub()
    _PANDAS_IMPORT_ERROR = exc
from graph.schema import FundState, AnalystSignal
from graph.constants import Signal, AgentKey
from llm.inference import agent_call
from llm.prompt import build_futures_technical_prompt
from apis.router import Router, APISource
from util.db_helper import get_db
from util.logger import logger
from util.text_sanitize import sanitize_visible_text
from typing import Optional, Dict, Any
from tools.agent_tools.analysis.analyst_quality import (
    apply_signal_quality_gate,
    apply_trade_research_contract,
    build_technical_context,
    format_technical_summary_for_prompt,
    llm_path_label,
    signal_value,
    write_analyst_report,
)
from tools.agent_tools.analysis.analyst_business_quality import apply_business_quality_enrichment
from tools.agent_tools.analysis.analyst_learning_calibration import (
    calibrate_signal_with_learning_context,
    retrieve_analyst_policy_calibration,
)
from tools.agent_tools.analysis.analyst_learning_context import build_learning_context, resolve_config_id
from tools.agent_tools.analysis.analyst_data_usage import build_technical_data_usage
from tools.agent_tools.analysis.analyst_technical_parameter_calibration import apply_technical_parameter_calibration
from tools.agent_tools.analysis.analyst_product_price_behavior_profile import (
    apply_profile_usage_to_signal,
    build_profile_usage_contract,
    format_profile_for_technical,
    get_product_price_behavior_profile,
)

def format_signal_compact(signal: Signal) -> str:
    """
    Convert a signal enum into a compact arrow marker for prompts.

    Args:
        signal: Signal enum value

    Returns:
        Short text marker: UP for bullish, DOWN for bearish, FLAT for neutral
    """
    return {
        Signal.BULLISH: "UP",
        Signal.BEARISH: "DOWN",
        Signal.NEUTRAL: "FLAT"
    }.get(signal, "?")

# Technical Thresholds
thresholds = {
    # Futures-specific indicator thresholds.
    "trend": {
        "short": 8,
        "medium": 21,
        "long": 55,
    },
    "mean_reversion": {
        "bollinger_window": 20,
        "rolling_window": 50,
        "z_score_extreme": 2.0,
        "bb_position_threshold": 0.2
    },
    "rsi": {
        "period": 14,
        "bullish": 30,
        "bearish": 70,
    },
    "volatility": {
        "bullish": 0.8,
        "bearish": 1.2,
    },
    "volume": {
        "trend": 20,
        "correlation": 20,
        "unusual_volume": 2.0,
    },
    "support_resistance": {
        "pivot_window": 5,
        "lookback_period": 20,
    },
    # Futures-specific indicator thresholds.
    "open_interest": {
        "trend_window": 10,  # Open-interest trend lookback.
        "change_threshold": 0.05,  # Open-interest change threshold (5%).
    },
    "settlement_price": {
        "ema_short": 8,
        "ema_long": 21,
        "spread_threshold": 0.01,  # Close-vs-settlement spread threshold (1%).
    },
    "gap_analysis": {
        "gap_threshold": 0.02,  # Gap threshold (2%).
    },
    "rollover": {
        "rollover_window": 5,
        "rollover_threshold": 2.0,  # Main-contract volume anomaly multiplier.
    },
    "divergence": {
        "divergence_threshold": 0.005,
    },
    "turnover_value": {
        "trend_window": 20,
        "unusual_turnover_threshold": 1.5,
    },
    "futures_volatility": {
        "window": 21,
    }

}


def _latest_price_data_date(prices_df) -> Optional[pd.Timestamp]:
    """Return the latest date represented in a price DataFrame."""
    if prices_df is None or getattr(prices_df, "empty", True):
        return None
    for candidate in ("date", "trade_date", "tradeDate", "datetime"):
        if candidate in getattr(prices_df, "columns", []):
            latest = pd.to_datetime(prices_df[candidate].iloc[-1], errors="coerce")
            return None if pd.isna(latest) else latest
    index = getattr(prices_df, "index", None)
    if index is None or len(index) == 0:
        return None
    latest = pd.to_datetime(index[-1], errors="coerce")
    return None if pd.isna(latest) else latest


def _validate_pre_open_price_window(ticker: str, trading_date, prices_df, pre_open_only: bool) -> str:
    """Ensure pre-open technical signals only use completed bars before trading_date."""
    latest = _latest_price_data_date(prices_df)
    if latest is None:
        raise RuntimeError(
            f"{ticker}: unable to determine latest technical price data date; "
            "cannot verify no-lookahead boundary"
        )
    latest_day = latest.normalize()
    trading_day = pd.to_datetime(trading_date).normalize()
    if pre_open_only and latest_day >= trading_day:
        raise RuntimeError(
            f"{ticker}: technical pre-open price window includes {latest_day.strftime('%Y-%m-%d')} "
            f"for trading_date={trading_day.strftime('%Y-%m-%d')}; expected latest price date < trading_date"
        )
    return latest_day.strftime("%Y-%m-%d")


def technical_agent(state: FundState):
    """Technical analysis specialist that excels at short to medium-term price movement predictions."""
    agent_name = AgentKey.TECHNICAL
    ticker = state["ticker"]
    trading_date = state["trading_date"]
    llm_config = state["llm_config"]
    portfolio_id = state["portfolio"].id
    market_type = state.get("market_type", "china_futures")
    phase = state.get("phase")
    morning_price_context = state.get("morning_price_context")
    pre_open_only = bool(state.get("pre_open_only", False))
    info_cutoff = state.get("info_cutoff") or ("pre_open" if pre_open_only else "unspecified")
    save_outputs = bool(state.get("save_analyst_outputs", True))

    if market_type != "china_futures":
        message = (
            f"Technical analyst only supports china_futures, got market_type={market_type!r}"
        )
        logger.error(message)
        raise RuntimeError(message)

    # Get db instance
    db = get_db()

    logger.log_agent_status(agent_name, ticker, "Analyzing price data")

    # Futures-only technical analysis uses the configured PandaAI futures feed.
    api_source = APISource.PANDAAI
    router = Router(api_source, market_type=market_type)

    # Get the price data
    try:
        prices_df = router.get_daily_candles_df(
            ticker=ticker,
            trading_date=trading_date
        )

        # Ensure the returned frame contains the core close-price series.
        if prices_df.empty or 'close' not in prices_df.columns:
            columns = prices_df.columns.tolist() if not prices_df.empty else "DataFrame is empty"
            message = f"Price data for {ticker} is empty or missing 'close' column. Columns: {columns}"
            logger.error(message)
            raise RuntimeError(message)

        logger.info(f"Successfully loaded {len(prices_df)} rows of price data for {ticker}")
        cfg = state.get("config", {}) or {}
        full_config = state.get("full_config", cfg) or {}
        if (full_config.get("audit", {}) or {}).get("log_technical_price_window", True):
            try:
                first_date = str(prices_df.index.min())[:10]
                last_date = str(prices_df.index.max())[:10]
                logger.info(
                    f"{ticker}: Technical price window | first={first_date} | last={last_date} | "
                    f"rows={len(prices_df)} | info_cutoff={info_cutoff}"
                )
            except Exception as exc:
                logger.warning(f"{ticker}: Failed to audit technical price window: {exc}")

    except Exception as e:
        logger.error(f"Failed to fetch price data for {ticker}: {e}")
        raise RuntimeError(f"Failed to fetch price data for {ticker}: {e}") from e

    latest_price_data_date = _validate_pre_open_price_window(
        ticker=ticker,
        trading_date=trading_date,
        prices_df=prices_df,
        pre_open_only=pre_open_only,
    )
    logger.info(
        f"{ticker}: Technical no-lookahead boundary ok | latest_price_data_date={latest_price_data_date} | "
        f"trading_date={trading_date} | pre_open_only={pre_open_only}"
    )

    # Compute adaptive market features before building indicator signals.
    features = calculate_market_features(prices_df)
    logger.info(
        f"{ticker}: Market features | volatility={features['volatility']:.2%} | "
        f"trend_strength(ADX)={features['trend_strength']:.2f} | "
        f"price_range={features['price_range']:.2%} | "
        f"volume_ratio={features['volume_ratio']:.2f}"
    )

    adaptive_params = calculate_adaptive_params(features, thresholds)
    technical_calibration_diag = {
        "enabled": False,
        "applied": [],
        "reason": "not_available",
    }
    config_id = resolve_config_id(db, full_config, state.get("config_id"))
    if bool(((full_config.get("learning") or {}).get("contextual_rule_calibration") or {}).get("enabled", True)):
        try:
            technical_probe_context = build_technical_context(
                ticker,
                {
                    "trend": get_trend_signal(prices_df, adaptive_params["trend"]),
                    "macd": get_macd_signal(prices_df, adaptive_params["macd"]),
                    "adx": get_adx_signal(prices_df, adaptive_params["adx"]),
                    "mean_reversion": get_mean_reversion_signal(prices_df, adaptive_params["mean_reversion"]),
                    "rsi": get_rsi_signal(prices_df, adaptive_params["rsi"]),
                    "stochastic": get_stochastic_signal(prices_df, adaptive_params["stochastic"]),
                },
                features,
            )
            policy_rows, policy_safety = retrieve_analyst_policy_calibration(
                db,
                config_id=config_id,
                ticker=ticker,
                side="*",
                horizon_class="short",
                market_regime=technical_probe_context.get("market_regime") or "*",
                trading_date=trading_date,
            )
            adaptive_params, technical_calibration_diag = apply_technical_parameter_calibration(
                adaptive_params,
                policy_rows,
                ticker=ticker,
                side="*",
                horizon_class="short",
                market_regime=technical_probe_context.get("market_regime") or "*",
                min_confidence=float(
                    ((full_config.get("learning") or {}).get("contextual_rule_calibration") or {}).get(
                        "technical_min_confidence",
                        ((full_config.get("learning") or {}).get("contextual_rule_calibration") or {}).get("min_confidence", 0.35),
                    )
                ),
            )
            technical_calibration_diag["policy_safety"] = policy_safety
        except Exception as exc:
            technical_calibration_diag = {
                "enabled": True,
                "applied": [],
                "error": str(exc),
            }
            logger.warning(f"{ticker}: technical parameter calibration skipped: {exc}")
    logger.info(
        f"{ticker}: Adaptive params | EMA short={adaptive_params['trend']['short']} / "
        f"EMA long={adaptive_params['trend']['long']} | "
        f"RSI thresholds={adaptive_params['rsi']['bullish']}/{adaptive_params['rsi']['bearish']} | "
        f"technical_calibration_applied={len(technical_calibration_diag.get('applied') or [])}"
    )

    phase_value = str(getattr(phase, "value", phase)) if phase else ""
    if pre_open_only:
        logger.info(
            f"{ticker}: Pre-open mode active; disabling T-day open-dependent gap analysis"
        )
        gap_analysis = (
            "Unavailable in pre-open mode: T-day open-dependent gap analysis is disabled "
            "until the open-order execution phase."
        )
    elif phase_value == "phase1":
        gap_analysis = get_gap_analysis_phase1(
            prices_df,
            morning_price_context,
            thresholds["gap_analysis"],
        )
    else:
        gap_analysis = get_gap_analysis(prices_df, thresholds["gap_analysis"])

    # Analyze futures-specific technical indicators.
    # Combine primary, confirmation, and filter indicators into one payload.
    signal_results = {
        # Primary futures indicators.
        "trend": get_trend_signal(prices_df, adaptive_params["trend"]),
        "open_interest": get_open_interest_signal(prices_df, thresholds["open_interest"]),
        "settlement_price": get_settlement_price_signal(prices_df, thresholds["settlement_price"]),
        "gap_analysis": gap_analysis,

        "macd": get_macd_signal(prices_df, adaptive_params["macd"]),
        "adx": get_adx_signal(prices_df, adaptive_params["adx"]),
        "futures_volatility": get_futures_volatility(prices_df, thresholds["futures_volatility"]),
        "turnover_value": get_turnover_value_analysis(prices_df, thresholds["turnover_value"]),
        "price_levels": get_support_resistance(prices_df, thresholds["support_resistance"]),

        "mean_reversion": get_mean_reversion_signal(prices_df, adaptive_params["mean_reversion"]),
        "rsi": get_rsi_signal(prices_df, adaptive_params["rsi"]),
        "stochastic": get_stochastic_signal(prices_df, adaptive_params["stochastic"]),
    }

    technical_context = build_technical_context(ticker, signal_results, features)
    product_profile = get_product_price_behavior_profile(ticker, full_config)
    product_profile_usage = build_profile_usage_contract(ticker, "technical", product_profile)
    technical_context["product_profile_evidence"] = product_profile_usage

    llm_path = llm_path_label(full_config, "technical")

    signal_results_compact = {
        k: format_signal_compact(v)
        for k, v in signal_results.items()
        if isinstance(v, Signal)
    }
    # Build the futures-only technical-analysis prompt from the centralized
    # prompt module. Data preparation and learning retrieval remain here.
    prompt = build_futures_technical_prompt(
        ticker=ticker,
        signal_results_compact=signal_results_compact,
        gap_analysis=signal_results.get("gap_analysis", "N/A"),
        technical_summary=format_technical_summary_for_prompt(technical_context),
        product_profile_context=format_profile_for_technical(ticker, product_profile),
        features=features,
        llm_path=llm_path,
    )
    learning_context = build_learning_context(
        db=db,
        full_config=full_config,
        config_id=config_id,
        trading_date=trading_date,
        analyst="technical",
        ticker=ticker,
        context=technical_context,
        horizon_class="short",
    )
    prompt += learning_context.get("text", "")
    prompt += (
        "\n\n=== Learning-to-signal requirement ===\n"
        "When research memories are present, use them only as rebuttable priors. "
        "State whether today's market regime and technical evidence confirm or contradict them. "
        "If the signal is Neutral, specify the concrete technical condition that would convert it "
        "to probe/open and the condition that keeps it on watchlist. Candidate memories cannot "
        "authorize sizing, add-ons, or holding a losing position.\n"
    )

    # Get LLM signal
    signal = agent_call(
        prompt=prompt,
        llm_config=llm_config,
        pydantic_model=AnalystSignal
    )

    # Preserve agent identity explicitly for downstream ordering and auditing.
    signal.agent_name = agent_name
    signal.data_cutoff = info_cutoff
    signal.no_lookahead_status = "ok"
    try:
        close = prices_df["close"].dropna()
        latest_close = float(close.iloc[-1]) if not close.empty else None
        price_percentile = (
            float((close <= latest_close).sum()) / float(len(close))
            if latest_close is not None and len(close) > 0
            else None
        )
    except Exception:
        price_percentile = None
    signal.horizon_class = signal.horizon_class if signal.horizon_class != "unknown" else "short"
    signal.expected_horizon_days = signal.expected_horizon_days or 2
    signal.market_regime = signal.market_regime if signal.market_regime != "unknown" else str(technical_context.get("market_regime") or "unknown")
    signal.trend_stage = signal.trend_stage if signal.trend_stage != "unknown" else str(technical_context.get("market_regime") or technical_context.get("tradeability") or "unknown")
    signal.price_percentile = signal.price_percentile if signal.price_percentile is not None else price_percentile
    signal.setup_type = signal.setup_type if signal.setup_type != "unknown" else str(technical_context.get("setup_type") or "technical_price_setup")
    signal.entry_trigger = signal.entry_trigger if signal.entry_trigger != "unknown" else "wait_for_trigger"
    data_usage_summary = build_technical_data_usage(
        ticker=ticker,
        trading_date=trading_date,
        prices_df=prices_df,
        indicators_used=list(signal_results.keys()),
        pre_open_only=pre_open_only,
        info_cutoff=info_cutoff,
    )
    signal.metadata = {
        **(getattr(signal, "metadata", {}) or {}),
        "llm_path": llm_path,
        "technical_context": technical_context,
        "product_profile_evidence": product_profile_usage,
        "indicators_used": list(signal_results.keys()),
        "adaptive_params": adaptive_params,
        "technical_parameter_calibration": technical_calibration_diag,
        "data_usage_summary": data_usage_summary,
        "reviewer_learning_context": {
            "selected_ids": learning_context.get("selected_ids", []),
            "horizon_class": learning_context.get("horizon_class", "short"),
            "memory_trace": learning_context.get("memory_trace", {}),
            "current_day_evidence_required": True,
            "candidate_hypothesis_authority": "prior_only_no_position_authority",
        },
        "analysis_strategy_trace": {
            "analyst": "technical",
            "market_state_adaptation": {
                "market_regime": technical_context.get("market_regime"),
                "tradeability": technical_context.get("tradeability"),
                "technical_parameter_calibration": technical_calibration_diag,
            },
            "product_profile_evidence": {
                "product_profile_id": product_profile_usage.get("product_profile_id"),
                "profile_role": product_profile_usage.get("profile_role"),
                "profile_learning_interaction": product_profile_usage.get("profile_learning_interaction"),
            },
            "neutral_to_opportunity_required": True,
            "position_authority_boundary": "signal_requires_pm_auditor_trader_confirmation",
        },
    }
    signal = calibrate_signal_with_learning_context(
        signal,
        analyst="technical",
        ticker=ticker,
        learning_context=learning_context,
    )
    signal = apply_signal_quality_gate(signal, technical_context, full_config, "technical")
    signal = apply_business_quality_enrichment(signal, technical_context, full_config, "technical")
    signal = apply_trade_research_contract(
        signal,
        technical_context,
        analyst="technical",
        trading_date=trading_date,
        ticker=ticker,
    )
    signal = apply_profile_usage_to_signal(signal, product_profile_usage)
    signal.justification += (
        f"\n[Audit: pre_open_only={pre_open_only}; info_cutoff={info_cutoff}; "
        f"latest_price_data_date={latest_price_data_date}; "
        f"gap_analysis={'disabled_pre_open' if pre_open_only else 'standard'}; "
        f"llm_path={llm_path}; tradeability={technical_context.get('tradeability')}; "
        f"business_quality={signal.business_quality_score:.2f}; setup_type={signal.setup_type}]"
    )
    signal.justification = sanitize_visible_text(signal.justification)

    report_sections = {
        "llm_path": llm_path,
        "tradeability": technical_context.get("tradeability"),
        "sector": technical_context.get("sector"),
        "Market Regime": technical_context.get("market_regime"),
        "Indicators Used": {
            name: signal_value(value) if isinstance(value, Signal) else str(value)
            for name, value in signal_results.items()
        },
        "Data Usage Summary": data_usage_summary,
        "Technical Parameter Calibration": technical_calibration_diag,
        "Product Price Behavior Profile": product_profile_usage,
        "Structured Technical Context": technical_context,
    }
    if save_outputs:
        report_path = write_analyst_report(
            analyst="technical",
            ticker=ticker,
            trading_date=trading_date,
            signal=signal,
            full_config=full_config,
            sections=report_sections,
        )
        if report_path:
            signal.metadata["decision_report_path"] = report_path

    # save signal
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

def calculate_market_features(prices_df: pd.DataFrame) -> dict:
    """
    Calculate high-level market features used by adaptive technical thresholds.

    Returns:
        A dictionary with volatility, trend strength, price range, and volume ratio.
    """
    returns = prices_df['close'].pct_change()

    # 1. Annualized short-window volatility.
    volatility = returns.tail(20).std() * (252 ** 0.5)

    # 2. ADX-based trend strength.
    adx = _calculate_adx(prices_df, period=14)
    trend_strength = adx.iloc[-1] if len(adx) > 0 else 0

    # 3. Relative price range across the recent window.
    price_range = (prices_df['high'].tail(20).max() -
                   prices_df['low'].tail(20).min()) / prices_df['close'].tail(20).mean()

    # 4. Latest volume relative to its moving average.
    # 4. Latest volume relative to its moving average.
    volume_ma = prices_df['volume'].tail(20).mean()
    volume_ratio = prices_df['volume'].iloc[-1] / volume_ma if volume_ma > 0 else 1

    return {
        "volatility": volatility,
        "trend_strength": trend_strength,
        "price_range": price_range,
        "volume_ratio": volume_ratio
    }

def calculate_adaptive_params(features: dict, base_params: dict) -> dict:
    """
    Adjust indicator parameters from the observed market regime.

    Args:
        features: Output from calculate_market_features.
        base_params: Baseline threshold configuration.

    Returns:
        The adapted parameter dictionary.
    """
    volatility = features["volatility"]
    trend_strength = features["trend_strength"]

    # Adapt EMA windows from the observed volatility regime.
    base_short = base_params["trend"]["short"]
    base_long = base_params["trend"]["long"]

    if volatility > 0.25:
        ema_short = int(base_short * 1.2)
        ema_long = int(base_long * 0.8)
    elif volatility < 0.15:
        ema_short = int(base_short * 0.8)
        ema_long = int(base_long * 1.2)
    else:
        ema_short = base_short
        ema_long = base_long

    # Adjust RSI bands when trend strength is unusually high.
    base_bullish = base_params["rsi"]["bullish"]
    base_bearish = base_params["rsi"]["bearish"]

    if trend_strength > 25:  # In strong trends, keep RSI bands wider for longer.
        rsi_bullish = min(40, base_bullish + 10)
        rsi_bearish = max(60, base_bearish - 10)
    else:  # In range-bound markets, use the standard RSI thresholds.
        rsi_bullish = base_bullish
        rsi_bearish = base_bearish

    # Widen or tighten Bollinger Bands based on recent volatility.
    base_bb_std = base_params["mean_reversion"].get("bollinger_std", 2.0)
    bb_std = max(1.5, min(3.0, base_bb_std * (volatility / 0.2)))

    return {
        "trend": {"short": ema_short, "medium": base_params["trend"]["medium"], "long": ema_long},
        "rsi": {"period": base_params["rsi"]["period"], "bullish": int(rsi_bullish), "bearish": int(rsi_bearish)},
        "mean_reversion": {
            "bollinger_window": base_params["mean_reversion"]["bollinger_window"],
            "rolling_window": base_params["mean_reversion"]["rolling_window"],
            "z_score_extreme": base_params["mean_reversion"]["z_score_extreme"],
            "bb_position_threshold": base_params["mean_reversion"]["bb_position_threshold"],
            "bollinger_std": round(bb_std, 2)
        },
        "macd": {"fast": 12, "slow": 26, "signal": 9},
        "adx": {"period": 14},
        "stochastic": {"k_period": 14, "d_period": 3, "smooth_k": 3}
    }

def get_trend_signal(prices_df, params):
    """Advanced trend following strategy using multiple timeframes and indicators"""

    def _calculate_ema(prices_df, window):
        return prices_df["close"].ewm(span=window, adjust=False).mean()

    # Calculate EMAs for multiple timeframes
    ema_short = _calculate_ema(prices_df, params["short"])
    ema_medium = _calculate_ema(prices_df, params["medium"])
    ema_long = _calculate_ema(prices_df, params["long"])

    # Determine trend direction and strength
    short_trend = ema_short > ema_medium
    medium_trend = ema_medium > ema_long

    if short_trend.iloc[-1] and medium_trend.iloc[-1]:
        signal = Signal.BULLISH
    elif not short_trend.iloc[-1] and not medium_trend.iloc[-1]:
        signal = Signal.BEARISH
    else:
        signal = Signal.NEUTRAL

    return signal

def get_mean_reversion_signal(prices_df, params):
    """Mean reversion strategy using statistical measures and Bollinger Bands"""
    
    def _calculate_bollinger_bands(prices_df: pd.DataFrame, window: int) -> tuple[pd.Series, pd.Series]:
        sma = prices_df["close"].rolling(window).mean()
        std_dev = prices_df["close"].rolling(window).std()
        upper_band = sma + (std_dev * 2)
        lower_band = sma - (std_dev * 2)
        return upper_band, lower_band

    # Calculate Bollinger Bands with configured window
    bb_upper, bb_lower = _calculate_bollinger_bands(prices_df, params["bollinger_window"])

    # Calculate z-score with configured rolling window
    ma = prices_df["close"].rolling(window=params["rolling_window"]).mean()
    std = prices_df["close"].rolling(window=params["rolling_window"]).std()
    z_score = (prices_df["close"] - ma) / std

    # Calculate normalized position within Bollinger Bands
    price_vs_bb = (prices_df["close"].iloc[-1] - bb_lower.iloc[-1]) / (bb_upper.iloc[-1] - bb_lower.iloc[-1])

    # Use threshold values for signal conditions.
    # Negative z-score means price is stretched below its recent mean; positive
    # z-score means price is stretched above it.
    if z_score.iloc[-1] < -params["z_score_extreme"] and price_vs_bb < params["bb_position_threshold"]:
        signal = Signal.BULLISH
    elif z_score.iloc[-1] > params["z_score_extreme"] and price_vs_bb > (1 - params["bb_position_threshold"]):
        signal = Signal.BEARISH
    else:
        signal = Signal.NEUTRAL

    return signal

def get_rsi_signal(prices_df, params):
    """RSI signal that indicate overbought/oversold conditions"""

    def _calculate_rsi(prices_df: pd.DataFrame, period: int) -> pd.Series:
        delta = prices_df["close"].diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    rsi = _calculate_rsi(prices_df, params["period"])
    if rsi.iloc[-1] > params["bearish"]:
        signal = Signal.BEARISH
    elif rsi.iloc[-1] < params["bullish"]:
        signal = Signal.BULLISH
    else:
        signal = Signal.NEUTRAL

    return signal

def get_macd_signal(prices_df: pd.DataFrame, params: dict) -> Signal:
    """
    Build a MACD-based directional signal.

    The logic follows the standard MACD line vs. signal line comparison.
    """
    fast = params.get("fast", 12)
    slow = params.get("slow", 26)
    signal_period = params.get("signal", 9)

    ema_fast = prices_df['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = prices_df['close'].ewm(span=slow, adjust=False).mean()

    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line

    latest_macd = macd_line.iloc[-1]
    latest_signal = signal_line.iloc[-1]

    # Similar to common MACD trend-following rules.
    if latest_macd > 0 and latest_macd > latest_signal:
        signal = Signal.BULLISH
    elif latest_macd < 0 and latest_macd < latest_signal:
        signal = Signal.BEARISH
    else:
        signal = Signal.NEUTRAL

    return signal

def _calculate_adx(prices_df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Calculate the ADX series used by the trend-strength filter.

    Args:
        prices_df: Price history.
        period: ADX lookback period.

    Returns:
        The ADX time series.
    """
    high = prices_df['high']
    low = prices_df['low']
    close = prices_df['close']

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0

    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr)

    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = dx.ewm(span=period, adjust=False).mean()

    return adx

def get_adx_signal(prices_df: pd.DataFrame, params: dict) -> Signal:
    """
    Build a directional signal from ADX trend strength.

    Args:
        prices_df: Price history.
        params: ADX parameter dictionary.

    Returns:
        A Signal enum value.
    """
    period = params.get("period", 14)

    adx = _calculate_adx(prices_df, period)
    latest_adx = adx.iloc[-1]
    latest_plus_di = _calculate_di(prices_df, period, positive=True).iloc[-1]
    latest_minus_di = _calculate_di(prices_df, period, positive=False).iloc[-1]

    # ADX is trend strength, not direction. Direction comes from +/-DI.
    if latest_adx > 25 and latest_plus_di > latest_minus_di:
        signal = Signal.BULLISH
    elif latest_adx > 25 and latest_minus_di > latest_plus_di:
        signal = Signal.BEARISH
    else:
        signal = Signal.NEUTRAL

    return signal


def _calculate_di(prices_df: pd.DataFrame, period: int = 14, *, positive: bool = True) -> pd.Series:
    """Calculate +DI or -DI for directional ADX confirmation."""
    high = prices_df["high"]
    low = prices_df["low"]
    close = prices_df["close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0

    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    dm = plus_dm if positive else minus_dm
    return 100 * (dm.ewm(span=period, adjust=False).mean() / atr)

def get_volatility_signal(prices_df, params):
    """Volatility-based trading strategy"""
    # Calculate various volatility metrics
    returns = prices_df["close"].pct_change()

    # Historical volatility
    hist_vol = returns.rolling(21).std() * math.sqrt(252)

    # Volatility regime detection
    vol_ma = hist_vol.rolling(63).mean()
    vol_regime = hist_vol / vol_ma

    # Volatility mean reversion
    vol_z_score = (hist_vol - vol_ma) / hist_vol.rolling(63).std()

    # Generate signal based on volatility regime
    current_vol_regime = vol_regime.iloc[-1]
    vol_z = vol_z_score.iloc[-1]

    if current_vol_regime < params["bullish"] and vol_z < -1:
        # Low vol regime, potential for expansion
        signal = Signal.BULLISH
    elif current_vol_regime > params["bearish"] and vol_z > 1:
        # High vol regime, potential for contraction
        signal = Signal.BEARISH
    else:
        signal = Signal.NEUTRAL

    return signal

def get_stochastic_signal(prices_df: pd.DataFrame, params: dict) -> Signal:
    """
    Build a directional signal from the stochastic oscillator.

    Args:
        prices_df: Price history.
        params: Stochastic parameter dictionary.

    Returns:
        A Signal enum value.
    """
    k_period = params.get("k_period", 14)
    d_period = params.get("d_period", 3)
    smooth_k = params.get("smooth_k", 3)

    low_min = prices_df['low'].rolling(window=k_period).min()
    high_max = prices_df['high'].rolling(window=k_period).max()

    k_percent = 100 * (prices_df['close'] - low_min) / (high_max - low_min)
    k_smooth = k_percent.rolling(window=smooth_k).mean()
    d_percent = k_smooth.rolling(window=d_period).mean()

    latest_k = k_smooth.iloc[-1]
    latest_d = d_percent.iloc[-1]

    # Short-horizon stochastic crossover logic.
    if latest_k < 20 and latest_k > latest_d:
        signal = Signal.BULLISH  # Oversold rebound setup.
    elif latest_k > 80 and latest_k < latest_d:
        signal = Signal.BEARISH
    else:
        signal = Signal.NEUTRAL

    return signal

def get_volume_analysis(prices_df, params):
    """Analyze volume characteristics"""
    volume = prices_df['volume']
    price = prices_df['close']
    
    # Calculate volume moving average
    vol_ma = volume.rolling(window=params["trend"]).mean()
    
    # Calculate price-volume relationship
    price_volume_corr = price.rolling(window=params["correlation"]).corr(volume)
    
    # Calculate volume trend
    vol_trend = (volume > vol_ma.shift(1)).astype(int)
    
    result = f"- Volume trend: {Signal.BULLISH if vol_trend.iloc[-1] == 1 else Signal.BEARISH}\n"
    result += f"- Price-volume correlation: {price_volume_corr.iloc[-1]}\n"
    result += f"- Unusual volume: {volume.iloc[-1] > (vol_ma.iloc[-1] * params['unusual_volume'])}\n"
    return result

def get_support_resistance(prices_df, params):
    """Calculate support and resistance levels"""
    def _is_level(prices: pd.Series, i: int, level_type: str, pivot_window: int = params["pivot_window"]) -> bool:
        """Check if the price point is a support/resistance level by comparing with surrounding prices"""
        start_idx = max(0, i - pivot_window)
        end_idx = min(len(prices), i + pivot_window + 1)
        window_prices = prices.iloc[start_idx:end_idx]
        current_price = prices.iloc[i]
        
        left_prices = window_prices.iloc[:pivot_window]
        right_prices = window_prices.iloc[pivot_window+1:]
        
        if level_type == 'support':
            return (len(left_prices[left_prices > current_price]) >= 2 and 
                   len(right_prices[right_prices > current_price]) >= 2)
        elif level_type == 'resistance':
            return (len(left_prices[left_prices < current_price]) >= 2 and 
                   len(right_prices[right_prices < current_price]) >= 2)
        # else:
        #     raise ValueError("level_type must be 'support' or 'resistance'")
    
    def _find_levels(prices: pd.Series, lookback_period: int = params["lookback_period"]):
        levels = []
        for i in range(lookback_period, len(prices)):
            if _is_level(prices, i, 'support'):
                levels.append((i, prices.iloc[i]))
            elif _is_level(prices, i, 'resistance'):
                levels.append((i, prices.iloc[i]))
        return levels
    
    price_data = prices_df['close']
    current_price = price_data.iloc[-1]
    levels = _find_levels(price_data)
    
    support_levels = [price for _, price in levels if price < current_price]
    resistance_levels = [price for _, price in levels if price > current_price]
    
    support = max(support_levels) if support_levels else None
    resistance = min(resistance_levels) if resistance_levels else None

    if support is None or resistance is None:
        return "Failed to analyze support and resistance levels"
    else:
        result = f"- Current price: {current_price}\n"
        result += f"- Nearest support: {support}\n"
        result += f"- Nearest resistance: {resistance}\n"
        result += f"- Price to support: {(current_price - support) / support}\n"
        result += f"- Price to resistance: {(resistance - current_price) / current_price}\n"
        return result

# Futures-specific indicators.

def get_open_interest_signal(prices_df, params):
    """
    Analyze open-interest changes as a futures-specific flow signal.

    Rising open interest with rising price is treated as bullish; the opposite
    combinations are interpreted accordingly.
    """
    # Open-interest data is optional in some feeds.
    if 'open_interest' not in prices_df.columns or prices_df['open_interest'].isna().all():
        return Signal.NEUTRAL

    oi = prices_df['open_interest']
    close = prices_df['close']

    # Compare open-interest change with price change.
    oi_change = oi.diff()
    oi_ma = oi.rolling(window=params['trend_window']).mean()

    # Price change series.
    price_change = close.diff()

    # Compare the latest OI and price changes.
    current_oi_change = oi_change.iloc[-1]
    current_price_change = price_change.iloc[-1]
    oi_trend = oi.iloc[-1] > oi_ma.iloc[-1]  # Whether OI is above its recent average.

    # Rising OI with rising price is bullish; the inverse combinations are weaker.
    if current_oi_change > 0 and current_price_change > 0:
        signal = Signal.BULLISH
    elif current_oi_change < 0 and current_price_change < 0:
        signal = Signal.BEARISH
    elif current_oi_change > 0 and current_price_change < 0:
        signal = Signal.BEARISH
    elif current_oi_change < 0 and current_price_change > 0:
        # Falling OI with rising price often signals short covering or weaker downside momentum.
        signal = Signal.NEUTRAL
    else:
        signal = Signal.NEUTRAL

    return signal

def get_settlement_price_signal(prices_df, params):
    """
    Compare close price and settlement price for a futures-specific signal.

    Large close-vs-settlement deviations are combined with the settlement trend.
    """
    # Settlement price is futures-specific and may be absent in some frames.
    if 'settle_price' not in prices_df.columns or prices_df['settle_price'].isna().all():
        return Signal.NEUTRAL

    close = prices_df['close']
    settle = prices_df['settle_price']

    # Measure close-vs-settlement deviation and combine it with the settlement trend.
    spread = (close - settle) / settle
    current_spread = spread.iloc[-1]

    # Use settlement-price EMA trend as the secondary filter.
    settle_ema_short = settle.ewm(span=params['ema_short']).mean()
    settle_ema_long = settle.ewm(span=params['ema_long']).mean()
    settle_trend = settle_ema_short.iloc[-1] > settle_ema_long.iloc[-1]

    # Combine spread magnitude with settlement-trend direction.
    if abs(current_spread) > params['spread_threshold']:
        if current_spread > 0 and settle_trend:
            # Close above settlement plus rising settlement trend is bullish.
            signal = Signal.BULLISH
        elif current_spread < 0 and not settle_trend:
            # Close below settlement plus falling settlement trend is bearish.
            signal = Signal.BEARISH
        else:
            signal = Signal.NEUTRAL
    else:
        if settle_trend:
            signal = Signal.BULLISH
        elif not settle_trend:
            signal = Signal.BEARISH
        else:
            signal = Signal.NEUTRAL

    return signal

def get_gap_analysis(prices_df, params):
    """
    Legacy gap-analysis helper for non-phase1 flows.
    """
    if 'pre_settle_price' not in prices_df.columns or 'open' not in prices_df.columns:
        return "Gap analysis unavailable: missing previous settlement or open-price data"

    valid_data = prices_df[['open', 'pre_settle_price']].dropna()
    if len(valid_data) < 2:
        return "Gap analysis unavailable: insufficient valid observations"

    open_price = valid_data['open']
    pre_settle = valid_data['pre_settle_price']
    gap = (open_price - pre_settle) / pre_settle
    current_gap = gap.iloc[-1]

    direction = 'gap_up' if current_gap > 0 else 'gap_down' if current_gap < 0 else 'flat_open'
    is_significant = abs(current_gap) > params['gap_threshold']

    result = f"- Gap size: {current_gap:.2%}\n"
    result += f"- Gap direction: {direction}\n"
    result += f"- Significant gap: {is_significant}\n"
    return result

def get_gap_analysis_phase1(prices_df, morning_price_context, params):
    """
    Phase1-only gap analysis using today's open context.

    This function intentionally does not use fallback execution prices as
    substitutes for today's open. If T-day open is missing, the gap sub-signal
    is marked unavailable while other historical sub-signals continue to work.
    """
    if morning_price_context is None:
        return "Gap analysis unavailable: missing phase1 morning context"

    open_price = getattr(morning_price_context, "open_price", None)
    prev_close_price = getattr(morning_price_context, "prev_close_price", None)

    if open_price is None or prev_close_price is None or prev_close_price == 0:
        return "Gap analysis unavailable: missing T-day open or previous close"

    gap = (open_price - prev_close_price) / prev_close_price
    is_significant = abs(gap) > params['gap_threshold']
    direction = 'up_gap' if gap > 0 else 'down_gap' if gap < 0 else 'flat_open'

    result = f"- Gap based on T-day open vs T-1 close: {gap:.2%}\n"
    result += f"- Gap direction: {direction}\n"
    result += f"- Significant gap: {is_significant}\n"
    return result

def get_main_contract_rollover(prices_df, params):
    """
    Heuristic rollover detector based on abnormal volume.
    """
    if 'volume' not in prices_df.columns:
        return "Rollover detection unavailable: missing volume data"

    volume = prices_df['volume']
    volume_ma = volume.rolling(window=params['rollover_window']).mean()
    vol_ratio = volume / volume_ma
    current_vol_ratio = vol_ratio.iloc[-1]

    result = f"- Volume ratio: {current_vol_ratio:.2f}\n"
    if current_vol_ratio > params['rollover_threshold']:
        result += "- Possible main-contract rollover window; reduce trust in pure price signals\n"
        rollover_warning = True
    else:
        result += "- No obvious rollover signal\n"
        rollover_warning = False

    return result, rollover_warning

def get_change_divergence(prices_df, params):
    """
    Analyze divergence between close change and settlement change.
    """
    if 'settle_price' not in prices_df.columns or prices_df['settle_price'].isna().all():
        return "Change-divergence analysis unavailable: missing settlement-price data"

    close = prices_df['close']
    settle = prices_df['settle_price']
    close_change = close.pct_change()
    settle_change = settle.pct_change()
    divergence = close_change - settle_change
    current_divergence = divergence.iloc[-1]

    result = f"- Close change: {close_change.iloc[-1]:.2%}\n"
    result += f"- Settlement change: {settle_change.iloc[-1]:.2%}\n"
    result += f"- Divergence: {current_divergence:.2%}\n"
    if abs(current_divergence) > params['divergence_threshold']:
        result += "- Meaningful divergence: late-session sentiment may have shifted sharply\n"

    return result

def get_turnover_value_analysis(prices_df, params):
    """
    Analyze turnover intensity as a confirmation signal.
    """
    if 'turnover' not in prices_df.columns or 'volume' not in prices_df.columns:
        return "Turnover analysis unavailable: missing turnover or volume data"

    turnover = prices_df['turnover']
    volume = prices_df['volume']
    turnover_ma = turnover.rolling(window=params['trend_window']).mean()
    turnover_trend = turnover.iloc[-1] > turnover_ma.iloc[-1]

    result = f"- Turnover trend: {'rising' if turnover_trend else 'falling'}\n"
    if 'open_interest' in prices_df.columns:
        oi = prices_df['open_interest']
        oi_nonzero = oi.replace(0, pd.NA)
        turnover_ratio = (turnover * 10000) / oi_nonzero
        current_ratio = turnover_ratio.iloc[-1]
        ratio_ma = turnover_ratio.rolling(window=params['trend_window']).mean()

        result += f"- Turnover / open-interest ratio: {current_ratio:.2f}\n"
        if not pd.isna(ratio_ma.iloc[-1]):
            result += f"- Ratio vs moving average: {(current_ratio / ratio_ma.iloc[-1] - 1):.2%}\n"
            if current_ratio > ratio_ma.iloc[-1] * params['unusual_turnover_threshold']:
                result += "- Unusually strong turnover: signal may be reinforced by active capital flow\n"

    return result

def get_futures_volatility(prices_df, params):
    """
    Analyze intraday and overnight volatility for futures.
    """
    if 'open' not in prices_df.columns or 'high' not in prices_df.columns or 'low' not in prices_df.columns:
        return "Volatility analysis unavailable: missing OHLC data"

    high = prices_df['high']
    low = prices_df['low']
    close = prices_df['close']
    intraday_vol = (high - low) / close

    result = f"- Intraday volatility: {intraday_vol.iloc[-1]:.2%}\n"
    if 'pre_settle_price' in prices_df.columns:
        valid_data = prices_df[['open', 'pre_settle_price']].dropna()
        if len(valid_data) >= 2:
            overnight_vol = (valid_data['open'] - valid_data['pre_settle_price']) / valid_data['pre_settle_price']
            current_overnight = overnight_vol.iloc[-1]
            result += f"- Overnight volatility: {current_overnight:.2%}\n"
            if intraday_vol.iloc[-1] > 0:
                result += f"- Overnight / intraday ratio: {abs(current_overnight) / intraday_vol.iloc[-1]:.2f}\n"
        else:
            result += "- Overnight volatility: insufficient data\n"
    else:
        result += "- Overnight volatility: unavailable\n"

    return result

# ==================== Legacy enhanced prompt wrapper ====================

def build_futures_technical_prompt_enhanced(ticker: str, signal_results: dict) -> str:
    """Backward-compatible enhanced futures technical prompt builder."""
    signal_results_compact = {k: format_signal_compact(v) for k, v in signal_results.items() if isinstance(v, Signal)}

    base_prompt = f"""You are a futures technical analyst for {ticker}.

Indicator snapshot (UP / DOWN / FLAT):
[Primary] TR:{signal_results_compact.get('trend', '?')} OI:{signal_results_compact.get('open_interest', '?')} ST:{signal_results_compact.get('settlement_price', '?')} GAP:{signal_results.get('gap_analysis', 'N/A')}
[Context] VOL:{signal_results_compact.get('futures_volatility', '?')} TV:{signal_results_compact.get('turnover_value', '?')} PL:{signal_results_compact.get('price_levels', '?')}
[Filters] MR:{signal_results_compact.get('mean_reversion', '?')} RSI:{signal_results_compact.get('rsi', '?')}
"""

    base_prompt += """
Signal priority: market regime fit > primary signals > filter signals

Output format:
- signal: \"Bullish\" / \"Bearish\" / \"Neutral\"
- confidence: 0.0-1.0
- justification: brief explanation referencing the strongest technical evidence

Provide a concise, well-reasoned futures technical view.
"""

    return base_prompt

def build_futures_technical_prompt_enhanced_v2(ticker: str, signal_results: dict,
                                                 features: dict = None,
                                                 technical_context: Optional[Dict[str, Any]] = None,
                                                 llm_path: str = "cloud_only") -> str:
    """Deprecated compatibility wrapper around llm.prompt.build_futures_technical_prompt."""
    signal_results_compact = {k: format_signal_compact(v) for k, v in signal_results.items() if isinstance(v, Signal)}
    return build_futures_technical_prompt(
        ticker=ticker,
        signal_results_compact=signal_results_compact,
        gap_analysis=signal_results.get("gap_analysis", "N/A"),
        technical_summary=(
            format_technical_summary_for_prompt(technical_context)
            if technical_context
            else ""
        ),
        features=features,
        llm_path=llm_path,
    )
