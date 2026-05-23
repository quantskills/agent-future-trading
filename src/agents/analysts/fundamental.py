import re
from typing import Any, Dict, Optional

from apis.router import APISource, Router
from graph.constants import AgentKey
from graph.schema import AnalystSignal, FundState
from llm.inference import agent_call
from llm.prompt import FUTURES_FUNDAMENTAL_PROMPT
from util.db_helper import get_db
from util.logger import logger
from util.text_sanitize import sanitize_visible_text
from tools.agent_tools.quality import (
    apply_signal_quality_gate,
    format_fundamental_summary_for_prompt,
    llm_path_label,
    parse_fundamental_factors,
    summarize_pandaai_extra_factors,
    write_analyst_report,
)
from tools.agent_tools.business_quality import apply_business_quality_enrichment
from tools.agent_tools.learning_context import build_learning_context, resolve_config_id
from util.trading_calendar import get_previous_trading_day


def _build_fundamental_audit_note(
    trading_date,
    pre_open_only: bool,
    info_cutoff: str,
    metadata: Optional[Dict[str, Any]],
) -> str:
    trading_date_value = trading_date.strftime("%Y-%m-%d") if hasattr(trading_date, "strftime") else str(trading_date)
    audit_parts = [
        f"pre_open_only={pre_open_only}",
        f"info_cutoff={info_cutoff}",
        f"fundamental_cutoff=<{trading_date_value}",
    ]

    if metadata:
        configured = int(metadata.get("configured_indicator_count") or 0)
        loaded = int(metadata.get("loaded_indicator_count") or 0)
        stale = int(metadata.get("stale_indicator_count") or 0)
        low_confidence = int(metadata.get("low_confidence_indicator_count") or 0)
        missing_like = (
            int(metadata.get("missing_file_count") or 0)
            + int(metadata.get("empty_frame_count") or 0)
            + int(metadata.get("no_data_before_count") or 0)
        )
        audit_parts.append(f"raw_loaded={loaded}/{configured}")
        audit_parts.append(f"stale={stale}")
        audit_parts.append(f"low_confidence={low_confidence}")
        audit_parts.append(f"missing_like={missing_like}")

    return f"[Audit: {'; '.join(audit_parts)}]"


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
    original_confidence = signal.confidence
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

    if discount_reasons:
        primary_reason = discount_reasons[0]
        extra_reasons = discount_reasons[1:]
        if extra_reasons:
            logger.info(
                f"{ticker}: Fundamental confidence adjusted: {original_confidence:.2f} -> "
                f"{signal.confidence:.2f}"
            )
            logger.info(f"  Base reason: {primary_reason}")
            logger.info(f"  Extra discounts: {', '.join(extra_reasons)}")
            signal.justification += (
                f"\n[Confidence adjustment: {original_confidence:.2f} -> {signal.confidence:.2f}; "
                f"base reason: {primary_reason}; extra discounts: {', '.join(extra_reasons)}]"
            )
        else:
            logger.info(
                f"{ticker}: Fundamental confidence set: {signal.confidence:.2f} "
                f"(from {original_confidence:.2f})"
            )
            logger.info(f"  Reason: {primary_reason}")
            signal.justification += (
                f"\n[Confidence adjustment: {original_confidence:.2f} -> {signal.confidence:.2f}; "
                f"reason: {primary_reason}]"
            )

    return signal


def fundamental_agent(state: FundState):
    """Fundamental analysis specialist for China futures markets."""
    agent_name = AgentKey.FUNDAMENTAL
    ticker = state["ticker"]
    trading_date = state["trading_date"]
    llm_config = state["llm_config"]
    portfolio_id = state["portfolio"].id
    market_type = state.get("market_type", "china_futures")
    pre_open_only = bool(state.get("pre_open_only", False))
    info_cutoff = state.get("info_cutoff") or ("pre_open" if pre_open_only else "unspecified")
    cfg = state.get("config", {}) or {}
    full_config = state.get("full_config", cfg) or {}

    if market_type != "china_futures":
        logger.error(
            f"Fundamental analyst only supports china_futures, got market_type={market_type!r}"
        )
        return state

    db = get_db()
    logger.log_agent_status(agent_name, ticker, "Analyzing fundamental data")

    try:
        router = Router(APISource.PANDAAI, market_type=market_type, config=full_config)
        fundamentals = router.get_china_futures_fundamentals(
            ticker=ticker,
            trading_date=trading_date,
        )
        fundamentals_metadata = getattr(router, "last_fundamentals_metadata", None)

        if not fundamentals:
            logger.error(f"{ticker}: No fundamental data returned from router")
            return state

        logger.info(f"{ticker}: Got fundamental data from router:\n{fundamentals}")
        fundamental_context = parse_fundamental_factors(fundamentals, fundamentals_metadata, ticker)
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
                logger.info(
                    f"{ticker}: PandaAI extra fundamental factors | "
                    f"direction={pandaai_extra_context.get('direction_hint')} | "
                    f"tradeability={pandaai_extra_context.get('tradeability')} | "
                    f"features={len(pandaai_extra_context.get('features') or [])}"
                )
            except Exception as exc:
                pandaai_extra_context = {
                    "enabled": True,
                    "tradeability": "low",
                    "direction_hint": "neutral",
                    "features": [],
                    "errors": [str(exc)],
                }
                fundamental_context["pandaai_extra_factors"] = pandaai_extra_context
                logger.warning(f"{ticker}: PandaAI extra fundamental factor context skipped: {exc}")

        fundamentals_for_prompt = fundamentals + format_fundamental_summary_for_prompt(fundamental_context)

        llm_path = llm_path_label(full_config, "fundamental")
        fundamentals_for_prompt += f"\n\n=== LLM Path ===\n{llm_path}\n"

        prompt = FUTURES_FUNDAMENTAL_PROMPT.format(
            ticker=ticker,
            fundamentals=fundamentals_for_prompt,
        )
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
        prompt += learning_context.get("text", "")
        logger.info(f"{ticker}: Fundamental prompt created, length={len(prompt)}")
    except Exception as exc:
        logger.error(f"{ticker}: Failed to fetch futures fundamentals: {exc}")
        import traceback

        logger.error(traceback.format_exc())
        return state

    signal = agent_call(
        prompt=prompt,
        llm_config=llm_config,
        pydantic_model=AnalystSignal,
    )

    signal.agent_name = agent_name
    signal.horizon_class = signal.horizon_class if signal.horizon_class != "unknown" else "medium"
    signal.expected_horizon_days = signal.expected_horizon_days or 5
    signal.market_regime = signal.market_regime if signal.market_regime != "unknown" else str(fundamental_context.get("tradeability") or "unknown")
    signal.trend_stage = signal.trend_stage if signal.trend_stage != "unknown" else str(fundamental_context.get("tradeability") or "unknown")
    signal.trigger_type = signal.trigger_type if signal.trigger_type != "unknown" else "fundamental_anchor"
    signal.entry_type = signal.entry_type if signal.entry_type != "unknown" else "direction_anchor"

    signal = apply_confidence_discount(signal, fundamentals_for_prompt, ticker, metadata=fundamentals_metadata)
    signal.metadata = {
        **(getattr(signal, "metadata", {}) or {}),
        **_build_fundamental_signal_metadata(fundamentals_metadata),
        "llm_path": llm_path,
        "fundamental_context": fundamental_context,
        "reviewer_learning_context": {
            "selected_ids": learning_context.get("selected_ids", []),
            "horizon_class": learning_context.get("horizon_class", "medium"),
        },
    }
    signal = apply_signal_quality_gate(signal, fundamental_context, full_config, "fundamental")
    signal = apply_business_quality_enrichment(signal, fundamental_context, full_config, "fundamental")
    signal.justification += "\n" + _build_fundamental_audit_note(
        trading_date=trading_date,
        pre_open_only=pre_open_only,
        info_cutoff=info_cutoff,
        metadata=fundamentals_metadata,
    )
    signal.justification += (
        f"\n[Fundamental context: sector={fundamental_context.get('sector')}; "
        f"tradeability={fundamental_context.get('tradeability')}; llm_path={llm_path}; "
        f"business_quality={signal.business_quality_score:.2f}; template={signal.template_name}]"
    )

    signal.justification = sanitize_visible_text(signal.justification)

    report_path = write_analyst_report(
        analyst="fundamental",
        ticker=ticker,
        trading_date=trading_date,
        signal=signal,
        full_config=full_config,
        sections={
            "llm_path": llm_path,
            "tradeability": fundamental_context.get("tradeability"),
            "sector": fundamental_context.get("sector"),
            "Sector Guidance": fundamental_context.get("sector_guidance"),
            "Factor Groups": fundamental_context.get("factor_groups"),
            "Factor Group Counts": fundamental_context.get("factor_group_counts"),
            "Data Quality": fundamental_context.get("data_quality"),
            "Basis": fundamental_context.get("basis") or "unavailable",
            "PandaAI Extra Factors": fundamental_context.get("pandaai_extra_factors") or "disabled_or_unavailable",
            "Risk Flags": fundamental_context.get("risk_flags"),
        },
    )
    if report_path:
        signal.metadata["decision_report_path"] = report_path

    logger.log_signal(agent_name, ticker, signal)
    db.save_signal(portfolio_id, agent_name, ticker, prompt, signal)

    return {
        "analyst_signals": [signal],
    }
