import re
from typing import Any, Dict, Optional

from apis.router import APISource, Router
from graph.constants import AgentKey, Signal
from graph.schema import AnalystSignal, FundState
from llm.inference import agent_call
from llm.prompt import build_futures_fundamental_prompt
from util.db_helper import get_db
from util.logger import logger
from tools.agent_tools.analysis.analyst_quality import (
    format_fundamental_summary_for_prompt,
    llm_path_label,
    parse_fundamental_factors,
    summarize_pandaai_extra_factors,
)
from tools.agent_tools.analysis.analyst_learning_context import build_learning_context, resolve_config_id
from tools.agent_tools.analysis.analyst_data_usage import build_fundamental_data_usage
from tools.agent_tools.analysis.analyst_output_finalization import (
    build_required_market_data_unavailable_signal,
    finalize_analyst_signal,
    resolve_analyst_llm_config,
)
from tools.agent_tools.analysis.analyst_structured_output import (
    FundamentalAnalystOutput,
)
from tools.agent_tools.analysis.analyst_product_price_behavior_profile import (
    build_profile_usage_contract,
    format_profile_for_fundamental,
    get_product_price_behavior_profile,
)
from util.trading_calendar import get_previous_trading_day


def _build_fundamental_signal_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep structured basis and data-quality diagnostics beside the LLM text."""
    if not metadata:
        return {}

    quality_keys = (
        "configured_indicator_count",
        "loaded_indicator_count",
        "missing_file_count",
        "empty_frame_count",
        "no_data_before_count",
        "missing_like_count",
        "stale_indicator_count",
        "near_stale_indicator_count",
        "coverage_ratio",
        "missing_ratio",
        "stale_ratio",
        "near_stale_ratio",
        "basis_available",
        "low_confidence_indicator_count",
        "low_confidence_indicators",
        "indicator_role_counts",
        "indicator_frequency_counts",
    )
    result = {
        "fundamental_quality": {
            key: metadata.get(key)
            for key in quality_keys
            if key in metadata
        }
    }
    if metadata.get("basis"):
        result["basis"] = metadata["basis"]
    return result


def _build_no_fundamental_data_signal(
    *,
    ticker: str,
    trading_date,
    agent_name: str,
    metadata: Optional[Dict[str, Any]],
    pre_open_only: bool,
    info_cutoff: str,
) -> AnalystSignal:
    trading_date_value = trading_date.strftime("%Y-%m-%d") if hasattr(trading_date, "strftime") else str(trading_date)
    data_usage_summary = build_fundamental_data_usage(
        ticker=ticker,
        trading_date=trading_date,
        fundamentals_metadata=metadata,
        pandaai_extra_context={},
        pre_open_only=pre_open_only,
        info_cutoff=info_cutoff,
    )
    data_usage_summary["data_available"] = False
    signal = AnalystSignal(
        agent_name=agent_name,
        signal=Signal.NEUTRAL,
        confidence=0.0,
        justification=(
            f"{ticker}: no local Finoview fundamental data available before {trading_date_value}; "
            "fundamental analyst emits explicit no_trade instead of inventing directional evidence."
        ),
        data_cutoff=info_cutoff,
        no_lookahead_status="ok",
        determinism_mode="deterministic_data_gap_no_trade",
        horizon_class="medium",
        analyst_horizon="medium",
        expected_horizon_days=0,
        market_regime="data_gap",
        trend_stage="data_gap",
        setup_type="data_unavailable_no_trade",
        data_freshness="missing",
        evidence_quality="low",
        business_quality_score=0.0,
        factor_alignment_score=0.0,
        data_coverage_score=0.0,
        tradeability_reason="local Finoview fundamental data unavailable",
        opportunity_type="no_trade",
        opportunity_state="no_opportunity",
        setup_quality_score=0.0,
        entry_quality="poor",
        setup_quality_notes=["fundamental_data_unavailable"],
        entry_trigger="",
        exit_hint="",
        holding_period_hint="no fundamental trade",
        factor_focus=["fundamental_data_availability"],
        current_evidence_conflict=["missing_fundamental_evidence"],
        neutral_reason="fundamental_data_unavailable",
        missing_evidence=["local_finoview_fundamental_data"],
        would_change_view_if="fresh local Finoview fundamental fields become available before the decision cutoff",
        neutral_opportunity_bucket="evidence_gap",
        neutral_trigger_condition="fresh fundamental evidence plus short-timing confirmation",
        counterfactual_side="flat",
        neutral_watchlist_priority="none",
        do_not_trade_reason="fundamental_data_unavailable",
        metadata={
            **_build_fundamental_signal_metadata(metadata),
            "data_usage_summary": data_usage_summary,
            "analysis_strategy_trace": {
                "analyst": "fundamental",
                "data_gap_no_trade": True,
                "position_authority_boundary": "no_fundamental_data_cannot_authorize_position",
            },
        },
    )
    return signal


def _resolve_pandaai_extra_reference_date(router: Router, ticker: str, trading_date, lag_days: int):
    reference_date = trading_date
    for _ in range(max(1, int(lag_days or 1))):
        reference_date = get_previous_trading_day(
            router=router,
            trading_date=reference_date,
            underlying_code=ticker,
        )
    return reference_date


def apply_confidence_discount(
    signal: AnalystSignal,
    fundamentals: str,
    ticker: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> AnalystSignal:
    """
    Apply a post-processing confidence discount to futures fundamental signals.

    The logic stays aligned with the existing phase1 design:
    - basis strength adjusts confidence without becoming the only driver
    - contradictory basis trend weakens confidence
    - sparse data and mixed signals reduce confidence
    - neutral signals are capped at low confidence
    """
    discount_reasons = []

    basis_pct = None
    if metadata:
        basis = metadata.get("basis") or {}
        if isinstance(basis, dict) and basis.get("latest_pct") is not None:
            try:
                basis_pct = float(basis["latest_pct"])
            except (TypeError, ValueError):
                basis_pct = None

    if basis_pct is None:
        basis_match = re.search(
            r"Basis value:\s*[-+]?[\d.]+\s*\(([-+]?[\d.]+)%\)",
            fundamentals,
            re.IGNORECASE,
        )
        if basis_match:
            basis_pct = float(basis_match.group(1))

    if basis_pct is not None:
        signal_value = str(signal.signal).lower()
        aligned_with_basis = (
            (basis_pct >= 0 and "bullish" in signal_value)
            or (basis_pct < 0 and "bearish" in signal_value)
        )
        abs_basis = abs(basis_pct)
        if abs_basis >= 10:
            if aligned_with_basis:
                signal.confidence = min(0.90, max(signal.confidence, 0.62))
                discount_reasons.append(f"strong confirming basis ({basis_pct:+.2f}%)")
            else:
                signal.confidence *= 0.78
                discount_reasons.append(f"strong basis conflict ({basis_pct:+.2f}%)")
        elif abs_basis >= 5:
            if aligned_with_basis:
                signal.confidence = min(0.80, max(signal.confidence, 0.55))
                discount_reasons.append(f"moderate confirming basis ({basis_pct:+.2f}%)")
            else:
                signal.confidence *= 0.85
                discount_reasons.append(f"moderate basis conflict ({basis_pct:+.2f}%)")
        elif abs_basis >= 1:
            if aligned_with_basis:
                signal.confidence = min(0.72, max(signal.confidence, 0.48))
                discount_reasons.append(f"mild confirming basis ({basis_pct:+.2f}%)")
            else:
                signal.confidence *= 0.92
                discount_reasons.append(f"mild basis conflict ({basis_pct:+.2f}%)")
        else:
            signal.confidence *= 0.95
            discount_reasons.append(f"near-flat basis ({basis_pct:+.2f}%)")

        basis_trend_match = re.search(
            r"Basis trend: .*?\(5d change:\s*([-+]?[\d.]+)%\)",
            fundamentals,
            re.IGNORECASE,
        )
        if basis_trend_match:
            basis_trend = float(basis_trend_match.group(1))
            if (basis_pct > 5 and basis_trend < -1) or (basis_pct < -5 and basis_trend > 1):
                signal.confidence *= 0.85
                discount_reasons.append(
                    f"basis trend conflict (basis {basis_pct:+.2f}%, trend {basis_trend:+.2f}%)"
                )

    lines = fundamentals.splitlines()
    indicator_count = sum(
        1
        for line in lines
        if ":" in line
        and not line.startswith("===")
        and not line.startswith("Basis value")
        and not line.startswith("Basis status")
        and not line.startswith("Basis trend")
        and not line.startswith("Trading implication")
        and not line.startswith("Price components")
        and not line.startswith("Data date")
    )

    if indicator_count < 10:
        signal.confidence *= 0.85
        discount_reasons.append(f"limited data ({indicator_count} indicators)")
    elif indicator_count < 20:
        signal.confidence *= 0.95
        discount_reasons.append(f"partial data ({indicator_count} indicators)")

    if metadata:
        configured = int(metadata.get("configured_indicator_count") or 0)
        stale_count = int(metadata.get("stale_indicator_count") or 0)
        near_stale_count = int(metadata.get("near_stale_indicator_count") or 0)
        low_confidence_count = int(metadata.get("low_confidence_indicator_count") or 0)
        missing_like_count = (
            int(metadata.get("missing_file_count") or 0)
            + int(metadata.get("empty_frame_count") or 0)
            + int(metadata.get("no_data_before_count") or 0)
        )

        if configured > 0:
            missing_ratio = missing_like_count / configured
            stale_ratio = stale_count / configured
            near_stale_ratio = near_stale_count / configured
            low_confidence_ratio = low_confidence_count / configured

            if missing_ratio >= 0.35:
                signal.confidence *= 0.80
                discount_reasons.append(
                    f"raw data coverage penalty ({configured - missing_like_count}/{configured} usable indicators)"
                )
            elif missing_ratio >= 0.15:
                signal.confidence *= 0.90
                discount_reasons.append(
                    f"raw data gaps ({missing_like_count}/{configured} unavailable indicators)"
                )

            if stale_ratio >= 0.20:
                signal.confidence *= 0.88
                discount_reasons.append(
                    f"stale indicators penalty ({stale_count}/{configured} stale)"
                )
            elif near_stale_ratio >= 0.30:
                signal.confidence *= 0.95
                discount_reasons.append(
                    f"aging indicators warning ({near_stale_count}/{configured} near stale)"
                )

            if low_confidence_ratio >= 0.20:
                signal.confidence *= 0.95
                discount_reasons.append(
                    f"low-confidence fundamental inputs ({low_confidence_count}/{configured})"
                )
            elif low_confidence_count > 0:
                signal.confidence *= 0.98
                discount_reasons.append(
                    f"minor low-confidence input discount ({low_confidence_count}/{configured})"
                )

    conflict_keywords = [
        "conflict",
        "contradict",
        "mixed",
        "inconsistent",
        "divergence",
        "矛盾",
        "冲突",
        "分化",
    ]
    justification_lower = signal.justification.lower()
    if any(keyword in justification_lower for keyword in conflict_keywords):
        signal.confidence *= 0.75
        discount_reasons.append("mixed signals")

    if signal.signal == "Neutral":
        signal.confidence = min(signal.confidence, 0.3)
        discount_reasons.append("neutral signal")

    signal.confidence = max(0.1, min(signal.confidence, 1.0))

    return signal


def fundamental_agent(state: FundState):
    """Fundamental analysis specialist for China futures markets."""
    agent_name = AgentKey.FUNDAMENTAL
    ticker = state["ticker"]
    trading_date = state["trading_date"]
    market_type = state.get("market_type", "china_futures")
    pre_open_only = bool(state.get("pre_open_only", False))
    info_cutoff = state.get("info_cutoff") or ("pre_open" if pre_open_only else "unspecified")
    cfg = state.get("config", {}) or {}
    full_config = state.get("full_config", cfg) or {}

    if state.get("pre_open_reference_price_unavailable"):
        signal = build_required_market_data_unavailable_signal(
            analyst="fundamental",
            ticker=ticker,
            trading_date=trading_date,
            full_config=full_config,
            info_cutoff=info_cutoff,
        )
        return {"analyst_signals": [signal]}

    llm_config = resolve_analyst_llm_config(state)

    if market_type != "china_futures":
        message = (
            f"Fundamental analyst only supports china_futures, got market_type={market_type!r}"
        )
        logger.error(message)
        raise RuntimeError(message)

    product_profile = get_product_price_behavior_profile(ticker, full_config)
    product_profile_usage = build_profile_usage_contract(ticker, "fundamental", product_profile)

    db = get_db()

    try:
        router = Router(APISource.PANDAAI, market_type=market_type, config=full_config)
        fundamentals = router.get_china_futures_fundamentals(
            ticker=ticker,
            trading_date=trading_date,
        )
        fundamentals_metadata = getattr(router, "last_fundamentals_metadata", None)

        if not fundamentals:
            logger.warning(f"{ticker}: No fundamental data returned from router; emitting explicit no_trade signal")
            signal = _build_no_fundamental_data_signal(
                ticker=ticker,
                trading_date=trading_date,
                agent_name=agent_name,
                metadata=fundamentals_metadata,
                pre_open_only=pre_open_only,
                info_cutoff=info_cutoff,
            )
            signal = finalize_analyst_signal(
                signal,
                quality_context={
                    "sector": str(product_profile.get("sector") or ""),
                    "tradeability": "low",
                    "risk_flags": ["fundamental_data_unavailable"],
                    "data_quality": {
                        "coverage_ratio": 0.0,
                        "factor_freshness_score": 0.0,
                        "no_lookahead_status": "ok",
                    },
                },
                full_config=full_config,
                analyst="fundamental",
                ticker=ticker,
                trading_date=trading_date,
                learning_context={},
                product_profile=product_profile,
                product_profile_usage=product_profile_usage,
            )
            logger.log_signal(agent_name, ticker, signal)
            return {"analyst_signals": [signal]}

        fundamental_context = parse_fundamental_factors(fundamentals, fundamentals_metadata, ticker)
        fundamental_context["product_profile_evidence"] = product_profile_usage
        pandaai_extra_context = {}
        extra_config = full_config.get("pandaai_extra_data", {}) or {}
        if extra_config.get("enabled", False) and extra_config.get("use_in_fundamental_analyst", True):
            try:
                reference_date = _resolve_pandaai_extra_reference_date(
                    router,
                    ticker,
                    trading_date,
                    int(extra_config.get("reference_lag_days", 1)),
                )
                extra_features = (
                    extra_config.get("fundamental_features")
                    or extra_config.get("features")
                    or {}
                )
                pandaai_extra_snapshot = router.get_pandaai_futures_extra_snapshot(
                    underlying_code=ticker,
                    reference_date=reference_date,
                    lookback_days=int(extra_config.get("lookback_days", 5)),
                    contract_id=state.get("contract_code") or state.get("target_contract_code"),
                    features=extra_features,
                )
                pandaai_extra_context = summarize_pandaai_extra_factors(pandaai_extra_snapshot)
                fundamental_context["pandaai_extra_factors"] = pandaai_extra_context
            except Exception:
                pandaai_extra_context = {
                    "enabled": True,
                    "tradeability": "low",
                    "direction_hint": "neutral",
                    "features": [],
                    "errors": ["pandaai_extra_fundamental_context_unavailable"],
                }
                fundamental_context["pandaai_extra_factors"] = pandaai_extra_context
                logger.warning(f"{ticker}: pandaai_extra_fundamental_context_unavailable")

        fundamentals_for_prompt = fundamentals + format_fundamental_summary_for_prompt(fundamental_context)

        llm_path = llm_path_label(full_config, "fundamental")
        fundamentals_for_prompt += f"\n\n=== LLM Path ===\n{llm_path}\n"

        learning_context = build_learning_context(
            db=db,
            full_config=full_config,
            config_id=resolve_config_id(db, full_config, state.get("config_id")),
            trading_date=trading_date,
            analyst="fundamental",
            ticker=ticker,
            context=fundamental_context,
            horizon_class="medium",
        )
        prompt = build_futures_fundamental_prompt(
            ticker=ticker,
            fundamentals=fundamentals_for_prompt,
            product_profile_context=format_profile_for_fundamental(ticker, product_profile),
            learning_context_text=learning_context.get("text", ""),
        )
    except Exception:
        logger.error(f"{ticker}: fundamental_data_pipeline_failed")
        raise RuntimeError(f"{ticker}: fundamental_data_pipeline_failed") from None

    try:
        signal = agent_call(
            prompt=prompt,
            llm_config=llm_config,
            pydantic_model=FundamentalAnalystOutput,
        )
    except Exception:
        logger.error(f"{ticker}: fundamental_inference_failed")
        raise RuntimeError(f"{ticker}: fundamental_inference_failed") from None

    signal.agent_name = agent_name
    signal.horizon_class = signal.horizon_class if signal.horizon_class != "unknown" else "medium"
    signal.expected_horizon_days = signal.expected_horizon_days or 5
    signal.market_regime = signal.market_regime if signal.market_regime != "unknown" else str(fundamental_context.get("tradeability") or "unknown")
    signal.trend_stage = signal.trend_stage if signal.trend_stage != "unknown" else str(fundamental_context.get("tradeability") or "unknown")
    signal.setup_type = signal.setup_type if signal.setup_type != "unknown" else "fundamental_timing_setup"
    data_usage_summary = build_fundamental_data_usage(
        ticker=ticker,
        trading_date=trading_date,
        fundamentals_metadata=fundamentals_metadata,
        pandaai_extra_context=pandaai_extra_context,
        pre_open_only=pre_open_only,
        info_cutoff=info_cutoff,
    )

    signal = apply_confidence_discount(signal, fundamentals_for_prompt, ticker, metadata=fundamentals_metadata)
    signal.metadata = {
        **(getattr(signal, "metadata", {}) or {}),
        **_build_fundamental_signal_metadata(fundamentals_metadata),
        "llm_path": llm_path,
        "fundamental_context": fundamental_context,
        "product_profile_evidence": product_profile_usage,
        "data_usage_summary": data_usage_summary,
        "reviewer_learning_context": {
            "selected_ids": learning_context.get("selected_ids", []),
            "horizon_class": learning_context.get("horizon_class", "medium"),
            "memory_trace": learning_context.get("memory_trace", {}),
            "current_day_evidence_required": True,
            "candidate_hypothesis_authority": "research_only_not_consumed_validated_prior_only",
        },
        "analysis_strategy_trace": {
            "analyst": "fundamental",
            "market_state_adaptation": {
                "market_regime": fundamental_context.get("market_regime"),
                "tradeability": fundamental_context.get("tradeability"),
                "data_quality": (fundamentals_metadata or {}),
            },
            "product_profile_evidence": {
                "product_profile_id": product_profile_usage.get("product_profile_id"),
                "profile_learning_interaction": product_profile_usage.get("profile_learning_interaction"),
            },
            "short_trigger_required_for_trade": True,
            "neutral_to_opportunity_required": True,
            "position_authority_boundary": "medium_thesis_requires_pm_auditor_trader_confirmation",
        },
    }
    signal = finalize_analyst_signal(
        signal,
        quality_context=fundamental_context,
        full_config=full_config,
        analyst="fundamental",
        ticker=ticker,
        trading_date=trading_date,
        learning_context=learning_context,
        product_profile=product_profile,
        product_profile_usage=product_profile_usage,
    )
    logger.log_signal(agent_name, ticker, signal)

    return {"analyst_signals": [signal]}
