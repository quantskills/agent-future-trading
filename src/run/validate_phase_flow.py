import argparse
import json
import sqlite3
import sys
from collections import Counter
from math import isclose
from pathlib import Path
from typing import Any, Dict, List

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dotenv import load_dotenv

from graph.schema import RecommendationSourceType, TradingPhase
from util.config import ConfigParser
from util.db_helper import db_initialize, get_db
from util.futures_audit import (
    build_actual_transactions,
    classify_zero_transaction_day,
    infer_no_trade_reason,
    normalize_no_trade_reason,
)
from util.logger import logger


load_dotenv()


def _normalize_date(value) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)


def _fetchone(cursor, query: str, params: tuple):
    cursor.execute(query, params)
    row = cursor.fetchone()
    return dict(row) if row else None


def _futures_account_equity(cash_balance: float, reserved_margin: float) -> float:
    return float(cash_balance or 0.0) + float(reserved_margin or 0.0)


def _expected_settlement_balance_change(settlement_row: Dict[str, Any]) -> float:
    return (
        float(settlement_row.get("daily_pnl") or 0.0)
        - float(settlement_row.get("commission") or 0.0)
        - (
            float(settlement_row.get("current_margin") or 0.0)
            - float(settlement_row.get("previous_margin") or 0.0)
        )
        + float(settlement_row.get("deposit") or 0.0)
        - float(settlement_row.get("withdraw") or 0.0)
    )


def _position_exposures(positions: Dict[str, Any], account_equity: float) -> tuple[float, Dict[str, float]]:
    if account_equity <= 0:
        return 0.0, {}

    net_exposure = 0.0
    single_exposures: Dict[str, float] = {}
    for ticker, position in positions.items():
        shares = int(position.get("shares") or 0)
        value = float(position.get("value") or 0.0)
        if shares == 0:
            continue
        signed_ratio = (1.0 if shares > 0 else -1.0) * value / account_equity
        net_exposure += signed_ratio
        single_exposures[ticker] = abs(value) / account_equity
    return net_exposure, single_exposures


def _group_transactions_by_recommendation(transactions: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for transaction in transactions:
        recommendation_id = transaction.get("recommendation_id")
        if not recommendation_id:
            continue
        grouped.setdefault(recommendation_id, []).append(transaction)
    return grouped


def _extract_execution_result(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = recommendation.get("signal_snapshot")
    if isinstance(snapshot, dict):
        result = snapshot.get("execution_result")
        if isinstance(result, dict):
            return result

    audit_payload = recommendation.get("audit_payload")
    if isinstance(audit_payload, dict):
        result = audit_payload.get("execution_result")
        if isinstance(result, dict):
            return result
    return {}


def _extract_execution_translation(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = recommendation.get("signal_snapshot")
    if isinstance(snapshot, dict):
        result = snapshot.get("execution_translation")
        if isinstance(result, dict):
            return result

    audit_payload = recommendation.get("audit_payload")
    if isinstance(audit_payload, dict):
        result = audit_payload.get("execution_translation")
        if isinstance(result, dict):
            return result
    return {}


def _extract_rollover_policy(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = recommendation.get("signal_snapshot")
    if isinstance(snapshot, dict):
        policy = snapshot.get("rollover_policy")
        if isinstance(policy, dict):
            return policy

    audit_payload = recommendation.get("audit_payload")
    if isinstance(audit_payload, dict):
        policy = audit_payload.get("rollover_policy")
        if isinstance(policy, dict):
            return policy
    return {}


def _resolve_no_trade_reason(recommendation: Dict[str, Any], has_transactions: bool = False) -> str | None:
    snapshot = recommendation.get("signal_snapshot") if isinstance(recommendation.get("signal_snapshot"), dict) else {}
    result = _extract_execution_result(recommendation)
    no_trade_reason = normalize_no_trade_reason(result.get("no_trade_reason")) if isinstance(result, dict) else None
    if no_trade_reason:
        return no_trade_reason
    if has_transactions:
        return None
    return normalize_no_trade_reason(infer_no_trade_reason(
        snapshot,
        warning_message=recommendation.get("warning_message"),
    ))


def _validate_recommendation_execution_audit(
    recommendations: List[Dict[str, Any]],
    transactions_by_recommendation: Dict[str, List[Dict[str, Any]]],
    errors: List[str],
) -> Counter:
    reason_counter: Counter = Counter()

    for recommendation in recommendations:
        recommendation_id = recommendation.get("id")
        recommendation_label = (
            f"{recommendation.get('source_type')}:{recommendation.get('underlying_code')}:{recommendation_id}"
        )
        execution_result = _extract_execution_result(recommendation)
        execution_translation = _extract_execution_translation(recommendation)
        actual_transactions = transactions_by_recommendation.get(recommendation_id, [])
        actual_summary = build_actual_transactions(actual_transactions)

        if not execution_result:
            errors.append(f"recommendation missing execution_result audit: {recommendation_label}")
            continue

        expected_count = int(execution_result.get("transaction_count") or 0)
        if expected_count != len(actual_transactions):
            errors.append(
                f"recommendation transaction_count mismatch for {recommendation_label}: "
                f"audit={expected_count}, actual={len(actual_transactions)}"
            )

        audited_actual = execution_result.get("actual_transactions") or []
        if audited_actual != actual_summary:
            errors.append(
                f"recommendation actual_transactions mismatch for {recommendation_label}: "
                f"audit={audited_actual}, actual={actual_summary}"
            )

        outcome = execution_result.get("outcome")
        no_trade_reason = _resolve_no_trade_reason(
            recommendation,
            has_transactions=bool(actual_transactions),
        )
        if no_trade_reason:
            reason_counter[no_trade_reason] += 1

        if actual_transactions:
            if outcome != "executed":
                errors.append(
                    f"recommendation outcome mismatch for {recommendation_label}: "
                    f"transactions exist but outcome={outcome}"
                )
            if no_trade_reason:
                errors.append(
                    f"recommendation {recommendation_label} recorded no_trade_reason={no_trade_reason} "
                    "despite having transactions"
                )
        else:
            if outcome not in {"executed_without_transaction", "skipped"}:
                errors.append(
                    f"recommendation outcome mismatch for {recommendation_label}: "
                    f"no transactions but outcome={outcome}"
                )
            if not no_trade_reason:
                errors.append(f"recommendation missing no_trade_reason for {recommendation_label}")

        rewrite_reasons = execution_translation.get("rewrite_reasons") or []
        translated_orders = execution_translation.get("translated_orders") or []
        if "two_step_reversal" in rewrite_reasons and len(actual_transactions) < 2:
            errors.append(
                f"recommendation {recommendation_label} expected two-step reversal but has "
                f"{len(actual_transactions)} transaction(s)"
            )
        if translated_orders and not isinstance(translated_orders, list):
            errors.append(f"recommendation translated_orders audit is not a list for {recommendation_label}")

        if recommendation.get("source_type") == RecommendationSourceType.ROLLOVER.value:
            rollover_policy = _extract_rollover_policy(recommendation)
            if rollover_policy:
                execution_type = rollover_policy.get("execution_type")
                close_lots = int(rollover_policy.get("close_lots") or 0)
                open_lots = int(rollover_policy.get("open_lots") or 0)
                if execution_type == "full_rollover" or (close_lots > 0 and open_lots > 0):
                    if len(actual_transactions) < 2:
                        errors.append(
                            f"rollover recommendation {recommendation_label} expected full rollover "
                            f"but has {len(actual_transactions)} transaction(s)"
                        )
                elif execution_type == "close_only_rollover" or (close_lots > 0 and open_lots == 0):
                    if len(actual_transactions) != 1:
                        errors.append(
                            f"rollover recommendation {recommendation_label} expected close-only rollover "
                            f"but has {len(actual_transactions)} transaction(s)"
                        )
                elif execution_type == "skipped_rollover" or (close_lots == 0 and open_lots == 0):
                    if len(actual_transactions) != 0:
                        errors.append(
                            f"rollover recommendation {recommendation_label} expected skipped rollover "
                            f"but has {len(actual_transactions)} transaction(s)"
                        )

    return reason_counter


def _market_confirmation_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    market_confirmation = snapshot.get("market_confirmation")
    if isinstance(market_confirmation, dict):
        return market_confirmation

    plan = snapshot.get("pre_open_plan") if isinstance(snapshot.get("pre_open_plan"), dict) else {}
    auditor = (plan.get("trade_auditor") or plan.get("decision_planner")) if isinstance(plan, dict) else None
    diagnostics = auditor.get("diagnostics") if isinstance(auditor, dict) else None
    market_confirmation = diagnostics.get("market_confirmation") if isinstance(diagnostics, dict) else None
    return market_confirmation if isinstance(market_confirmation, dict) else {}


def _collect_recommendation_quality_warnings(recommendations: List[Dict[str, Any]]) -> List[str]:
    quality_warnings: Counter = Counter()

    for recommendation in recommendations:
        if recommendation.get("source_type") != RecommendationSourceType.STRATEGY.value:
            continue

        ticker = str(recommendation.get("underlying_code") or "UNKNOWN")
        snapshot = recommendation.get("signal_snapshot") if isinstance(recommendation.get("signal_snapshot"), dict) else {}

        fundamental = snapshot.get("fundamental") if isinstance(snapshot.get("fundamental"), dict) else {}
        metadata = fundamental.get("metadata") if isinstance(fundamental.get("metadata"), dict) else {}
        quality = metadata.get("fundamental_quality") if isinstance(metadata.get("fundamental_quality"), dict) else {}
        if quality:
            if quality.get("basis_available") is False:
                quality_warnings[f"{ticker}: fundamental basis unavailable"] += 1
            stale_count = int(quality.get("stale_indicator_count") or 0)
            near_stale_count = int(quality.get("near_stale_indicator_count") or 0)
            coverage_ratio = float(quality.get("coverage_ratio") or 1.0)
            if stale_count:
                quality_warnings[f"{ticker}: stale fundamental indicators={stale_count}"] += 1
            if near_stale_count:
                quality_warnings[f"{ticker}: near-stale fundamental indicators={near_stale_count}"] += 1
            if coverage_ratio < 0.95:
                quality_warnings[f"{ticker}: fundamental coverage={coverage_ratio:.0%}"] += 1

        market_confirmation = _market_confirmation_from_snapshot(snapshot)
        for error in market_confirmation.get("errors") or []:
            quality_warnings[f"{ticker}: PandaAI confirmation error: {error}"] += 1
        missing = [str(item) for item in (market_confirmation.get("data_missing") or []) if item]
        if missing:
            quality_warnings[f"{ticker}: PandaAI missing features={','.join(missing[:5])}"] += 1

    return [
        f"data quality audit: {message} (count={count})"
        for message, count in quality_warnings.most_common(12)
    ]


def _build_summary_payload(
    *,
    cfg: Dict[str, Any],
    trading_date: str,
    phase1: Dict[str, Any] | None,
    phase2: Dict[str, Any] | None,
    phase3: Dict[str, Any] | None,
    phase4_status: str,
    strategy_count: int,
    rollover_count: int,
    phase1_transaction_count: int,
    phase2_transaction_count: int,
    no_trade_reason_counter: Counter,
    settlement_row: Dict[str, Any] | None,
    warnings: List[str],
    errors: List[str],
) -> Dict[str, Any]:
    return {
        "exp_name": cfg["exp_name"],
        "trading_date": trading_date,
        "validation_status": phase4_status,
        "phases": {
            "phase1": phase1.get("status") if phase1 else "missing",
            "phase2": phase2.get("status") if phase2 else "missing",
            "phase3": phase3.get("status") if phase3 else "missing",
            "phase4": phase4_status,
        },
        "recommendation_summary": {
            "strategy_count": strategy_count,
            "rollover_count": rollover_count,
            "total_count": strategy_count + rollover_count,
        },
        "transaction_summary": {
            "phase1_transaction_count": phase1_transaction_count,
            "phase2_transaction_count": phase2_transaction_count,
        },
        "no_trade_reason_counts": dict(no_trade_reason_counter),
        "settlement_summary": {
            "current_balance": settlement_row.get("current_balance") if settlement_row else None,
            "current_margin": settlement_row.get("current_margin") if settlement_row else None,
            "account_equity": (
                _futures_account_equity(
                    settlement_row.get("current_balance"),
                    settlement_row.get("current_margin"),
                )
                if settlement_row
                else None
            ),
            "daily_pnl": settlement_row.get("daily_pnl") if settlement_row else None,
            "commission": settlement_row.get("commission") if settlement_row else None,
        },
        "warnings": warnings,
        "errors": errors,
    }


def _safe_json_loads(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _money(value: Any, digits: int = 2) -> str:
    try:
        number = float(value or 0.0)
    except Exception:
        number = 0.0
    if digits <= 0:
        return f"{number:,.0f}"
    return f"{number:,.{digits}f}"


def _signed_money(value: Any, digits: int = 2) -> str:
    try:
        number = float(value or 0.0)
    except Exception:
        number = 0.0
    return f"{number:+,.{digits}f}"


def _percent(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value or 0.0) * 100:.{digits}f}%"
    except Exception:
        return "0.0%"


def _signal_label(value: Any) -> str:
    mapping = {
        "Bullish": "偏多",
        "Bearish": "偏空",
        "Neutral": "中性",
        "bullish": "偏多",
        "bearish": "偏空",
        "neutral": "中性",
    }
    return mapping.get(str(value), str(value or "未知"))


def _action_text(action: Any, lots: Any) -> str:
    try:
        lot_count = int(lots or 0)
    except Exception:
        lot_count = 0
    mapping = {
        "open_long": f"买入开多 {lot_count} 手",
        "close_long": f"卖出平多 {lot_count} 手",
        "open_short": f"卖出开空 {lot_count} 手",
        "close_short": f"买入平空 {lot_count} 手",
        "hold": "不买不卖",
    }
    return mapping.get(str(action), f"{action or '未知动作'} {lot_count} 手")


def _position_text(shares: Any) -> str:
    try:
        lots = int(shares or 0)
    except Exception:
        lots = 0
    if lots > 0:
        return f"多头 {lots} 手"
    if lots < 0:
        return f"空头 {abs(lots)} 手"
    return "空仓"


def _compact_text(value: Any, *, max_chars: int = 260) -> str:
    text = str(value or "").strip()
    if not text:
        return "未记录详细说明。"
    text = text.split("\n[", 1)[0].strip()
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _date_title(trading_date: str) -> str:
    parts = str(trading_date).split("-")
    if len(parts) == 3:
        return f"{int(parts[1])}月{int(parts[2])}日"
    return trading_date


def _sort_by_config_tickers(items: List[Dict[str, Any]], tickers: List[str]) -> List[Dict[str, Any]]:
    order = {ticker: index for index, ticker in enumerate(tickers)}
    return sorted(items, key=lambda item: (order.get(str(item.get("underlying_code") or item.get("ticker")), 10_000), str(item.get("underlying_code") or item.get("ticker"))))


def _positions_summary(positions: Dict[str, Any]) -> str:
    active = []
    for ticker, position in sorted((positions or {}).items()):
        if not isinstance(position, dict):
            continue
        if position.get("shares") is not None:
            shares = int(position.get("shares") or 0)
        else:
            lots = int(position.get("lots") or 0)
            position_type = str(position.get("position_type") or "").upper()
            shares = -lots if position_type == "SHORT" else lots
        if shares:
            active.append(f"{ticker} {_position_text(shares)}")
    return "；".join(active) if active else "无持仓"


def _technical_threshold_lines(technical_context: Dict[str, Any]) -> List[str]:
    features = technical_context.get("features") if isinstance(technical_context, dict) else {}
    volatility = float((features or {}).get("volatility") or 0.0)
    trend_strength = float((features or {}).get("trend_strength") or 0.0)
    if volatility > 0.25:
        ema_short, ema_long = 9, 44
    elif volatility < 0.15:
        ema_short, ema_long = 6, 66
    else:
        ema_short, ema_long = 8, 55
    rsi_bullish, rsi_bearish = (40, 60) if trend_strength > 25 else (30, 70)
    adx_note = f"本日 ADX 约 {trend_strength:.2f}。" if trend_strength else ""
    return [
        "技术指标看多/看空阈值补充：",
        f"- trend：使用 EMA {ema_short} / EMA 21 / EMA {ema_long}；短中长期 EMA 多头排列判为 Bullish，空头排列判为 Bearish，否则 Neutral。",
        "- open_interest：OI 增且价格涨为 Bullish；OI 减且价格跌，或 OI 增但价格跌为 Bearish；OI 减但价格涨或变化不一致为 Neutral。辅助观察窗口为 10 日。",
        "- settlement_price：使用结算价 EMA 8 / EMA 21，收盘价相对结算价偏离阈值为 1%。价差与结算价趋势同向时判多/空，价差不显著时按结算价 EMA 方向判多空。",
        "- MACD：快线 12、慢线 26、信号线 9；MACD > 0 且 MACD > signal 判 Bullish；MACD < 0 且 MACD < signal 判 Bearish；否则 Neutral。",
        f"- ADX：周期 14；ADX > 25 支持顺势 Bullish，ADX < 20 判 Bearish，20 到 25 为 Neutral。{adx_note}",
        "- mean_reversion：使用 20 日布林带、50 日 z-score、布林位置阈值 0.2；价格位于布林带下方 20% 区间偏 Bullish，z-score > 2.0 且价格位于上方 20% 区间偏 Bearish。",
        f"- RSI：14 日 RSI；本日自适应阈值为 {rsi_bullish}/{rsi_bearish}。RSI < {rsi_bullish} 判 Bullish；RSI > {rsi_bearish} 判 Bearish；区间内为 Neutral。",
        "- stochastic：K=14、D=3、smooth K=3；K < 20 且 K > D 判 Bullish；K > 80 且 K < D 判 Bearish；否则 Neutral。",
    ]


def _agent_signal_line(snapshot: Dict[str, Any], key: str, label: str) -> str:
    analyst = snapshot.get(key) if isinstance(snapshot.get(key), dict) else {}
    signal = _signal_label(analyst.get("signal"))
    confidence = analyst.get("confidence")
    tradeability = (analyst.get("metadata") or {}).get("tradeability") if isinstance(analyst.get("metadata"), dict) else None
    extras = []
    if confidence is not None:
        extras.append(f"confidence={float(confidence):.2f}")
    if tradeability:
        extras.append(f"tradeability={tradeability}")
    suffix = f"（{'，'.join(extras)}）" if extras else ""
    return f"- {label}：{signal}{suffix}。"


def _recommendation_by_id(recommendations: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(item.get("id")): item for item in recommendations if item.get("id")}


def _ticker_daily_pnl_rows(cursor, config_id: str, trading_date: str) -> Dict[str, Dict[str, Any]]:
    cursor.execute(
        """
        SELECT tdp.*
        FROM ticker_daily_pnl tdp
        JOIN portfolio p ON tdp.portfolio_id = p.id
        WHERE p.config_id = ?
          AND substr(tdp.trading_date, 1, 10) = ?
        ORDER BY tdp.ticker
        """,
        (config_id, trading_date),
    )
    return {row["ticker"]: dict(row) for row in cursor.fetchall()}


def _actual_trade_sections(
    *,
    transactions: List[Dict[str, Any]],
    recommendations_by_id: Dict[str, Dict[str, Any]],
    ticker_pnl: Dict[str, Dict[str, Any]],
) -> List[str]:
    sections: List[str] = []
    transactions_by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    for transaction in transactions:
        transactions_by_ticker.setdefault(str(transaction.get("ticker") or "UNKNOWN"), []).append(transaction)

    for ticker in sorted(transactions_by_ticker):
        ticker_transactions = transactions_by_ticker[ticker]
        recommendation = recommendations_by_id.get(str(ticker_transactions[0].get("recommendation_id"))) or {}
        snapshot = recommendation.get("signal_snapshot") if isinstance(recommendation.get("signal_snapshot"), dict) else {}
        plan = snapshot.get("pre_open_plan") if isinstance(snapshot.get("pre_open_plan"), dict) else {}
        technical = snapshot.get("technical") if isinstance(snapshot.get("technical"), dict) else {}
        technical_context = ((technical.get("metadata") or {}).get("technical_context") or {}) if isinstance(technical.get("metadata"), dict) else {}
        indicator_details = (((technical_context.get("indicator_votes") or {}).get("details")) or {})
        action_summary = "；".join(_action_text(tx.get("action"), tx.get("lots")) for tx in ticker_transactions)
        pre_lots = None
        post_lots = None
        first_audit = _safe_json_loads(ticker_transactions[0].get("audit_payload")) or {}
        last_audit = _safe_json_loads(ticker_transactions[-1].get("audit_payload")) or {}
        pre_lots = first_audit.get("pre_trade_shares", plan.get("current_lots_before_open"))
        post_lots = last_audit.get("post_trade_shares", plan.get("target_lots_estimate"))
        target_lots = plan.get("target_lots_estimate", recommendation.get("lots"))
        target_ratio = plan.get("target_position_ratio")

        sections.extend([
            "",
            ticker,
            "",
            f"交易动作：{action_summary}。",
            f"交易前持仓：{_position_text(pre_lots)}。",
            f"交易后持仓：{_position_text(post_lots)}。",
            f"计划上想做：目标持仓 {target_lots if target_lots is not None else '未知'} 手，目标仓位约 {_percent(target_ratio)}。",
            "",
            "实际执行：",
            "",
        ])
        for tx in ticker_transactions:
            sections.extend([
                f"- 品种：{ticker}",
                f"- 合约：{tx.get('contract_code') or recommendation.get('contract_code') or '未知'}",
                f"- 动作：{tx.get('action')}",
                f"- 数量：{int(tx.get('lots') or 0)} 手",
                f"- 执行方式：{tx.get('base_price_source') or recommendation.get('base_price_source') or '未知'}",
                f"- 基准价：{_money(tx.get('base_price'), 2)}",
                f"- 成交价：{_money(tx.get('execution_price'), 2)}",
                f"- 滑点：{_money(tx.get('slippage_amount'), 2)}",
                f"- 手续费：{_money(tx.get('commission'), 2)}",
            ])
        sections.extend([
            "",
            "为什么：",
            "",
            _agent_signal_line(snapshot, "technical", "技术面"),
            "使用的技术指标非常明确：",
        ])
        if indicator_details:
            for name, signal in indicator_details.items():
                sections.append(f"{name} = {signal}")
        else:
            sections.append(_compact_text(technical.get("justification")))
        sections.extend(_technical_threshold_lines(technical_context))
        sections.append(f"也就是说，技术分析师最后给了 {_signal_label(technical.get('signal'))}。")

        fundamental = snapshot.get("fundamental") if isinstance(snapshot.get("fundamental"), dict) else {}
        fundamental_context = ((fundamental.get("metadata") or {}).get("fundamental_context") or {}) if isinstance(fundamental.get("metadata"), dict) else {}
        basis = fundamental_context.get("basis") if isinstance(fundamental_context.get("basis"), dict) else {}
        sections.extend([
            "",
            _agent_signal_line(snapshot, "fundamental", "基本面"),
            "使用的基本面因子：",
            _compact_text(fundamental.get("justification"), max_chars=420),
        ])
        if basis:
            sections.append(
                f"basis = {_money(basis.get('latest'), 2)}，状态为 {basis.get('status') or '未知'}，"
                f"含义：{basis.get('signal') or '未记录'}。"
            )

        news = snapshot.get("commodity_news") if isinstance(snapshot.get("commodity_news"), dict) else {}
        news_context = ((news.get("metadata") or {}).get("news_context") or {}) if isinstance(news.get("metadata"), dict) else {}
        events = news_context.get("events") if isinstance(news_context.get("events"), list) else []
        sections.extend([
            "",
            _agent_signal_line(snapshot, "commodity_news", "新闻面"),
            "使用的新闻片段：",
        ])
        if events:
            for event in events[:4]:
                if isinstance(event, dict):
                    sections.append(f"{event.get('title') or '未命名新闻'}；")
        else:
            sections.append(_compact_text(news.get("justification"), max_chars=420))

        sections.extend([
            "",
            "- 最终为什么会这样交易：",
            _compact_text(recommendation.get("justification"), max_chars=520),
            "",
            "日结结果：",
            "",
        ])
        pnl_row = ticker_pnl.get(ticker, {})
        sections.extend([
            f"- {ticker} 日终保留 {_position_text(pnl_row.get('lots', post_lots))}",
            f"- entry_price = {_money(pnl_row.get('entry_price'), 2)}",
            f"- settle_price = {_money(pnl_row.get('settle_price'), 2)}",
            f"- 当日品种盈亏 {_signed_money(pnl_row.get('daily_pnl'), 2)}",
        ])
    return sections


def _untraded_sections(
    *,
    strategy_recommendations: List[Dict[str, Any]],
    traded_tickers: set[str],
    cfg_tickers: List[str],
) -> List[str]:
    sections: List[str] = []
    for recommendation in _sort_by_config_tickers(strategy_recommendations, cfg_tickers):
        ticker = str(recommendation.get("underlying_code") or "UNKNOWN")
        if ticker in traded_tickers:
            continue
        snapshot = recommendation.get("signal_snapshot") if isinstance(recommendation.get("signal_snapshot"), dict) else {}
        reason = _resolve_no_trade_reason(recommendation, has_transactions=False) or recommendation.get("status") or "未成交"
        signals = "，".join([
            f"技术面{_signal_label((snapshot.get('technical') or {}).get('signal') if isinstance(snapshot.get('technical'), dict) else None)}",
            f"基本面{_signal_label((snapshot.get('fundamental') or {}).get('signal') if isinstance(snapshot.get('fundamental'), dict) else None)}",
            f"新闻面{_signal_label((snapshot.get('commodity_news') or {}).get('signal') if isinstance(snapshot.get('commodity_news'), dict) else None)}",
        ])
        sections.extend([
            "",
            ticker,
            f"交易动作：不买不卖。",
            f"最终不交易原因：{signals}；执行归因为 {reason}。{_compact_text(recommendation.get('justification'), max_chars=260)}",
        ])
    return sections


def _build_daily_transaction_report(
    *,
    cfg: Dict[str, Any],
    trading_date: str,
    settlement_row: Dict[str, Any] | None,
    latest_portfolio: Dict[str, Any] | None,
    strategy_recommendations: List[Dict[str, Any]],
    recommendations: List[Dict[str, Any]],
    phase2_transactions: List[Dict[str, Any]],
    ticker_pnl: Dict[str, Dict[str, Any]],
) -> str:
    positions = latest_portfolio.get("positions") if isinstance(latest_portfolio, dict) else {}
    if (not isinstance(positions, dict) or not positions) and settlement_row:
        positions = _safe_json_loads(settlement_row.get("positions_snapshot")) or {}
    account_equity = (
        _futures_account_equity(settlement_row.get("current_balance"), settlement_row.get("current_margin"))
        if settlement_row else 0.0
    )
    recommendations_by_id = _recommendation_by_id(recommendations)
    traded_tickers = {str(tx.get("ticker") or "UNKNOWN") for tx in phase2_transactions}
    cfg_tickers = [str(ticker) for ticker in cfg.get("tickers", [])]

    lines = [
        f"{_date_title(trading_date)}交易日志报告",
        "",
        "当日总览",
        "",
        (
            f"当天实际有 {len(traded_tickers)} 个品种发生真实交易，"
            "其余品种都是“有交易想法，但最终未成交”。"
            if phase2_transactions
            else "当天没有品种发生真实交易，所有品种最终都未成交。"
        ),
        "",
        "组合日结结果：",
        "",
        f"- 实际成交：{len(phase2_transactions)} 笔",
        f"- 当日结算盈亏：{_signed_money(settlement_row.get('daily_pnl') if settlement_row else 0.0, 2)}",
        f"- 手续费：{_money(settlement_row.get('commission') if settlement_row else 0.0, 2)}",
        f"- 日终资金：{_money(settlement_row.get('current_balance') if settlement_row else 0.0, 2)}",
        f"- 日终保证金：{_money(settlement_row.get('current_margin') if settlement_row else 0.0, 2)}",
        f"- 日终账户权益：{_money(account_equity, 2)}",
        f"- 日终持仓：{_positions_summary(positions)}",
        "",
        "=========实际交易品类==========",
    ]
    if phase2_transactions:
        lines.extend(_actual_trade_sections(
            transactions=phase2_transactions,
            recommendations_by_id=recommendations_by_id,
            ticker_pnl=ticker_pnl,
        ))
    else:
        lines.extend(["", "无"])

    lines.extend([
        "",
        "=========未交易品类==========",
    ])
    lines.extend(_untraded_sections(
        strategy_recommendations=strategy_recommendations,
        traded_tickers=traded_tickers,
        cfg_tickers=cfg_tickers,
    ))

    lines.extend([
        "",
        "=========结论==========",
        "",
        f"{_date_title(trading_date)} 最终真正发生的交易：",
        "",
    ])
    if phase2_transactions:
        for tx in phase2_transactions:
            lines.append(
                f"{tx.get('ticker')} {_action_text(tx.get('action'), tx.get('lots'))}，"
                f"成交价 {_money(tx.get('execution_price'), 2)}。"
            )
    else:
        lines.append("无真实成交。")
    lines.extend([
        "",
        "但如果看“为什么会做出这个结果”，背后仍然是三层筛选：",
        "",
        "- 先由技术、基本面、新闻三位分析师分别给方向",
        "- 再由策略层做加权、仓位目标计算",
        "- 最后由 trade auditor 和质量控制决定放不放行",
        "",
        "这也是为什么看到很多品种“看起来有方向”，最后却还是 买0、卖0。",
        "",
    ])
    return "\n".join(lines)


def _write_daily_transaction_report(
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    cursor,
    settlement_row: Dict[str, Any] | None,
    latest_portfolio: Dict[str, Any] | None,
    recommendations: List[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    phase2_transactions: List[Dict[str, Any]],
) -> Path:
    report_text = _build_daily_transaction_report(
        cfg=cfg,
        trading_date=trading_date,
        settlement_row=settlement_row,
        latest_portfolio=latest_portfolio,
        strategy_recommendations=strategy_recommendations,
        recommendations=recommendations,
        phase2_transactions=phase2_transactions,
        ticker_pnl=_ticker_daily_pnl_rows(cursor, config_id, trading_date),
    )
    output_path = SRC_ROOT / "logs" / f"{trading_date}_transaction.log"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_text, encoding="utf-8")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Validate futures four-phase database flow")
    parser.add_argument("--config", type=str, required=True, help="Path to configuration file")
    parser.add_argument("--trading-date", type=str, required=True, help="Trading date in format YYYY-MM-DD")
    parser.add_argument("--local-db", action="store_true", help="Use local SQLite database")
    args = parser.parse_args()

    cfg = ConfigParser(args).get_config()
    if cfg.get("market_type") != "china_futures":
        raise RuntimeError("validate_phase_flow.py only supports china_futures")
    if not args.local_db:
        raise RuntimeError("validate_phase_flow.py currently requires --local-db")

    db_initialize(use_local_db=True)
    db = get_db()
    config_id = db.get_config_id_by_name(cfg["exp_name"])
    if not config_id:
        raise RuntimeError(f"Config {cfg['exp_name']} does not exist in local database")

    trading_date = _normalize_date(cfg["trading_date"])
    logger.set_context(exp_name=cfg["exp_name"], trading_date=trading_date, phase=TradingPhase.PHASE4.value)
    expected_tickers = len(cfg.get("tickers", []))
    errors: List[str] = []
    warnings: List[str] = []

    phase1 = db.get_trading_day_phase(config_id, trading_date, TradingPhase.PHASE1)
    phase2 = db.get_trading_day_phase(config_id, trading_date, TradingPhase.PHASE2)
    phase3 = db.get_trading_day_phase(config_id, trading_date, TradingPhase.PHASE3)
    phase4 = db.get_trading_day_phase(config_id, trading_date, TradingPhase.PHASE4)
    if phase4 and phase4.get("status") == "completed":
        raise RuntimeError(f"Phase4 already completed for {cfg['exp_name']} on {trading_date}")

    db.start_trading_day_phase(config_id, trading_date, TradingPhase.PHASE4)

    conn = None

    try:
        latest_portfolio = db.get_latest_settled_portfolio(config_id)
        phase1_transactions = db.get_futures_transactions_by_date(
            config_id,
            trading_date,
            execution_phase=TradingPhase.PHASE1,
        )
        phase2_transactions = db.get_futures_transactions_by_date(
            config_id,
            trading_date,
            execution_phase=TradingPhase.PHASE2,
        )
        transactions_by_recommendation = _group_transactions_by_recommendation(phase2_transactions)

        recommendations = db.get_futures_recommendations_by_effective_date(config_id, trading_date)
        strategy_recommendations = [
            recommendation
            for recommendation in recommendations
            if recommendation.get("source_type") == RecommendationSourceType.STRATEGY.value
        ]
        rollover_recommendations = [
            recommendation
            for recommendation in recommendations
            if recommendation.get("source_type") == RecommendationSourceType.ROLLOVER.value
        ]
        same_day_rollovers = [
            recommendation
            for recommendation in rollover_recommendations
            if recommendation.get("trading_date") == trading_date
        ]

        conn = sqlite3.connect(db.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        settlement_row = _fetchone(
            cursor,
            """
            SELECT ds.*, p.trading_date AS portfolio_trading_date
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE p.config_id = ?
              AND substr(ds.trading_date, 1, 10) = ?
            ORDER BY ds.created_at DESC
            LIMIT 1
            """,
            (config_id, trading_date),
        )

        if not phase1 or phase1.get("status") != "completed":
            errors.append(f"phase1 not completed on {trading_date}")

        if len(strategy_recommendations) < expected_tickers:
            errors.append(
                f"strategy recommendations are incomplete: expected at least {expected_tickers}, "
                f"got {len(strategy_recommendations)}"
            )

        if phase1_transactions:
            errors.append(f"phase1 should not write real transactions, but found {len(phase1_transactions)} rows")

        if not phase2 or phase2.get("status") != "completed":
            errors.append(f"phase2 not completed on {trading_date}")
        elif not phase2_transactions:
            zero_transaction_day = classify_zero_transaction_day(strategy_recommendations)
            zero_transaction_class = zero_transaction_day["classification"]
            zero_transaction_reasons = zero_transaction_day["reasons"]
            if zero_transaction_class == "expected":
                warnings.append(
                    f"phase2 completed on {trading_date} with 0 transactions, but all strategy recommendations "
                    f"were expected no-trade cases: {dict(Counter(zero_transaction_reasons))}"
                )
            else:
                errors.append(
                    f"phase2 completed on {trading_date} but no transactions were written; "
                    f"classification={zero_transaction_class}, reasons={dict(Counter(zero_transaction_reasons))}"
                )

        if not phase3 or phase3.get("status") != "completed":
            errors.append(f"phase3 not completed on {trading_date}")
        else:
            if settlement_row is None:
                errors.append(f"phase3 completed on {trading_date} but daily_settlement row is missing")
            if not latest_portfolio or _normalize_date(latest_portfolio.get("trading_date")) != trading_date:
                errors.append(
                    f"phase3 completed on {trading_date} but latest settled portfolio trading_date is "
                    f"{latest_portfolio.get('trading_date') if latest_portfolio else 'None'}"
                )

        commission_from_transactions = round(sum(float(tx.get("commission") or 0) for tx in phase2_transactions), 2)
        if settlement_row is not None:
            settlement_commission = round(float(settlement_row.get("commission") or 0), 2)
            if not isclose(commission_from_transactions, settlement_commission, abs_tol=0.01):
                errors.append(
                    f"commission mismatch: transactions={commission_from_transactions:.2f}, "
                    f"daily_settlement={settlement_commission:.2f}"
                )

            actual_balance_change = (
                float(settlement_row.get("current_balance") or 0.0)
                - float(settlement_row.get("previous_balance") or 0.0)
            )
            expected_balance_change = _expected_settlement_balance_change(settlement_row)
            if not isclose(actual_balance_change, expected_balance_change, abs_tol=0.01):
                errors.append(
                    f"settlement balance formula mismatch: actual_change={actual_balance_change:.2f}, "
                    f"expected_change={expected_balance_change:.2f}"
                )

            if latest_portfolio and _normalize_date(latest_portfolio.get("trading_date")) == trading_date:
                account_equity = _futures_account_equity(
                    settlement_row.get("current_balance"),
                    settlement_row.get("current_margin"),
                )
                portfolio_margin = float(latest_portfolio.get("margin_used") or 0.0)
                portfolio_available = float(latest_portfolio.get("available_cash") or 0.0)
                expected_available = (
                    float(settlement_row.get("current_balance") or 0.0)
                    - float(settlement_row.get("current_margin") or 0.0)
                )
                if not isclose(portfolio_margin, float(settlement_row.get("current_margin") or 0.0), abs_tol=0.01):
                    errors.append(
                        f"portfolio margin mismatch: portfolio={portfolio_margin:.2f}, "
                        f"daily_settlement={float(settlement_row.get('current_margin') or 0.0):.2f}"
                    )
                if not isclose(portfolio_available, expected_available, abs_tol=0.01):
                    errors.append(
                        f"available_cash mismatch: portfolio={portfolio_available:.2f}, "
                        f"expected current_balance-current_margin={expected_available:.2f}"
                    )

                positions = latest_portfolio.get("positions") or {}
                net_exposure, single_exposures = _position_exposures(positions, account_equity)
                net_exposure_config = cfg.get("net_exposure_control") or cfg.get("risk_control", {}).get("net_exposure_control", {})
                max_net_exposure = float(net_exposure_config.get("max_net_exposure", 0.50))
                if abs(net_exposure) > max_net_exposure + 0.001:
                    errors.append(
                        f"net exposure exceeds cap on {trading_date}: "
                        f"{net_exposure:.2%} > {max_net_exposure:.2%}"
                    )

                max_single_config = cfg.get("risk_control", {}).get("max_single_position_ratio", {})
                max_single_position_ratio = max(
                    float(value)
                    for value in (max_single_config or {"safe": 0.12}).values()
                )
                single_breaches = {
                    ticker: ratio
                    for ticker, ratio in single_exposures.items()
                    if ratio > max_single_position_ratio + 0.001
                }
                if single_breaches:
                    formatted = ", ".join(
                        f"{ticker}={ratio:.2%}" for ticker, ratio in sorted(single_breaches.items())
                    )
                    errors.append(
                        f"single-position exposure exceeds base cap {max_single_position_ratio:.2%}: {formatted}"
                    )

        unbooked = [tx["id"] for tx in phase2_transactions if not tx.get("booked_in_settlement")]
        if unbooked:
            errors.append(f"{len(unbooked)} transactions are still unbooked after completed phase3")

        if same_day_rollovers:
            errors.append(
                f"found {len(same_day_rollovers)} same-day rollover recommendation(s) on {trading_date}"
            )

        no_trade_reason_counter = _validate_recommendation_execution_audit(
            recommendations,
            transactions_by_recommendation,
            errors,
        )
        warnings.extend(_collect_recommendation_quality_warnings(recommendations))

        logger.info(
            f"Validation summary for {cfg['exp_name']} on {trading_date}: "
            f"phase1={phase1.get('status') if phase1 else 'missing'}, "
            f"phase2={phase2.get('status') if phase2 else 'missing'}, "
            f"phase3={phase3.get('status') if phase3 else 'missing'}, "
            f"strategy_recommendations={len(strategy_recommendations)}, "
            f"rollover_recommendations={len(rollover_recommendations)}, "
            f"phase1_transactions={len(phase1_transactions)}, "
            f"phase2_transactions={len(phase2_transactions)}"
        )

        for warning in warnings:
            logger.warning(warning)
        for error in errors:
            logger.error(error)

        phase4_status = "failed" if errors else "completed"
        logger.write_daily_summary(
            trading_date,
            _build_summary_payload(
                cfg=cfg,
                trading_date=trading_date,
                phase1=phase1,
                phase2=phase2,
                phase3=phase3,
                phase4_status=phase4_status,
                strategy_count=len(strategy_recommendations),
                rollover_count=len(rollover_recommendations),
                phase1_transaction_count=len(phase1_transactions),
                phase2_transaction_count=len(phase2_transactions),
                no_trade_reason_counter=no_trade_reason_counter,
                settlement_row=settlement_row,
                warnings=warnings,
                errors=errors,
            ),
        )

        try:
            report_path = _write_daily_transaction_report(
                cfg=cfg,
                config_id=config_id,
                trading_date=trading_date,
                cursor=cursor,
                settlement_row=settlement_row,
                latest_portfolio=latest_portfolio,
                recommendations=recommendations,
                strategy_recommendations=strategy_recommendations,
                phase2_transactions=phase2_transactions,
            )
            logger.info(f"Daily transaction report written: {report_path}")
        except Exception as report_exc:
            errors.append(f"daily transaction report generation failed: {report_exc}")
            logger.error(f"Daily transaction report generation failed: {report_exc}")

        if errors:
            raise RuntimeError(f"Phase flow validation failed with {len(errors)} error(s)")

        db.complete_trading_day_phase(
            config_id,
            trading_date,
            TradingPhase.PHASE4,
            "completed",
            "validation passed",
            memory_config=cfg.get("strategy_memory", {}),
        )
        logger.info("Phase flow validation passed")
    except Exception as exc:
        db.complete_trading_day_phase(config_id, trading_date, TradingPhase.PHASE4, "failed", str(exc))
        raise
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
