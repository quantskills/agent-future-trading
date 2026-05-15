import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from graph.constants import Signal
from graph.schema import AnalystSignal
from util.logger import logger
from util.text_sanitize import sanitize_visible_text


SECTOR_BY_TICKER = {
    "BU": "energy",
    "EB": "chemical",
    "MA": "chemical",
    "TA": "chemical",
    "HC": "ferrous",
    "I": "ferrous",
    "J": "ferrous",
    "RB": "ferrous",
    "PB": "nonferrous",
    "ZN": "nonferrous",
    "C": "agricultural",
    "CF": "agricultural",
    "M": "agricultural",
    "P": "agricultural",
    "SR": "agricultural",
}

SECTOR_GUIDANCE = {
    "energy": {
        "technical": "Emphasize trend, volatility, volume confirmation, and crude-oil cost disturbance. Avoid chasing after sharp cost-driven spikes.",
        "fundamental": "Focus on crude-oil cost, refinery operating rates, bitumen shipments, road-construction demand, refinery inventory, and social inventory.",
        "news": "Treat crude oil, refinery output, road construction, policy, and inventory news as the highest-impact event classes.",
    },
    "chemical": {
        "technical": "Chemical contracts often oscillate around cost, margin, and inventory cycles. Require trend confirmation before following breakouts.",
        "fundamental": "Focus on upstream costs, plant operating rates, processing margins, port or factory inventory, and downstream operating rates.",
        "news": "Prioritize plant outages, maintenance, operating rates, port inventory, feedstock shocks, and downstream order news.",
    },
    "ferrous": {
        "technical": "Ferrous signals should prefer chain confirmation. Isolated single-contract breakouts deserve lower confidence.",
        "fundamental": "Focus on hot metal output, steel margins, mill inventories, social inventories, property/infrastructure demand, and ore/coke cost chains.",
        "news": "Prioritize property, infrastructure, hot metal, steel-mill margins, production restrictions, inventories, and chain-wide policy news.",
    },
    "nonferrous": {
        "technical": "Nonferrous signals are more sensitive to overseas inventory, macro risk, and funding-driven breakouts. Guard against false breakouts in high volatility.",
        "fundamental": "Focus on mine supply, treatment charges, smelter margins, social inventory, SHFE/LME inventory, imports, and downstream operating rates.",
        "news": "Prioritize overseas supply, LME/SHFE inventory, treatment charges, smelter disruptions, macro rates, and FX news.",
    },
    "agricultural": {
        "technical": "Agricultural contracts need stricter confirmation because seasonal, policy, weather, and import news can create noisy short-term reversals.",
        "fundamental": "Focus on production, sales progress, imports, inventories, weather, policy, consumption, crushing/processing margins, and substitution effects.",
        "news": "Prioritize weather, crop progress, import/export, policy, inventory, consumption, and substitution news. De-emphasize stale generic macro headlines.",
    },
}

HIGH_CAUTION_TICKERS = {"P", "RB", "EB", "HC", "J"}
LONG_ENTRY_WATCHLIST = {"MA", "I", "PB"}


def get_sector(ticker: str) -> str:
    return SECTOR_BY_TICKER.get(str(ticker).upper(), "generic")


def get_sector_guidance(ticker: str, analyst: str) -> str:
    sector = get_sector(ticker)
    guidance = SECTOR_GUIDANCE.get(sector, {})
    return guidance.get(analyst, "Use commodity-specific evidence and prefer Neutral when evidence is mixed.")


def get_analyst_llm_config(full_config: Dict[str, Any], analyst: str) -> Dict[str, Any]:
    cfg = full_config.get("analyst_llm", {}) or {}
    llm_cfg = full_config.get("llm", {}) or {}
    analyst_key = str(analyst).lower()
    deep_key = f"enable_deepanalyze_for_{analyst_key}"
    return {
        "mode": cfg.get("mode", "cloud_only"),
        "cloud_model": cfg.get("cloud_model") or llm_cfg.get("model"),
        "enable_deepanalyze": bool(cfg.get(deep_key, False)),
        "write_decision_reports": bool(cfg.get("write_decision_reports", True)),
        "force_neutral_low_tradeability": bool(cfg.get("force_neutral_low_tradeability", True)),
        "cap_medium_tradeability_confidence": float(cfg.get("cap_medium_tradeability_confidence", 0.65)),
        "cap_low_tradeability_confidence": float(cfg.get("cap_low_tradeability_confidence", 0.35)),
    }


def llm_path_label(full_config: Dict[str, Any], analyst: str, deepanalyze_used: bool) -> str:
    base = get_analyst_llm_config(full_config, analyst)
    if deepanalyze_used:
        return "cloud_plus_deepanalyze"
    return str(base.get("mode") or "cloud_only")


def signal_value(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def normalize_trading_date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value)
    return text[:10] if len(text) >= 10 else text


def build_vote_summary(signals: Dict[str, Any]) -> Dict[str, Any]:
    counts = Counter()
    details = {}
    for name, value in signals.items():
        if isinstance(value, Signal):
            direction = signal_value(value)
            details[name] = direction
            counts[direction] += 1
    return {
        "counts": dict(counts),
        "details": details,
        "bullish_count": counts.get(Signal.BULLISH.value, 0),
        "bearish_count": counts.get(Signal.BEARISH.value, 0),
        "neutral_count": counts.get(Signal.NEUTRAL.value, 0),
    }


def build_technical_context(
    ticker: str,
    signal_results: Dict[str, Any],
    features: Dict[str, Any],
) -> Dict[str, Any]:
    votes = build_vote_summary(signal_results)
    bullish = int(votes["bullish_count"])
    bearish = int(votes["bearish_count"])
    neutral = int(votes["neutral_count"])
    conflict_count = min(bullish, bearish)
    dominant_direction = "neutral"
    if bullish > bearish and bullish >= 3:
        dominant_direction = "bullish"
    elif bearish > bullish and bearish >= 3:
        dominant_direction = "bearish"

    trend_strength = float(features.get("trend_strength") or 0.0)
    volatility = float(features.get("volatility") or 0.0)
    volume_ratio = float(features.get("volume_ratio") or 1.0)

    if volatility >= 0.35:
        regime = "high_volatility"
    elif trend_strength < 18:
        regime = "weak_trend"
    elif conflict_count >= 2:
        regime = "choppy"
    elif dominant_direction in {"bullish", "bearish"} and trend_strength >= 25:
        regime = "trend"
    else:
        regime = "range"

    aligned = max(bullish, bearish)
    ticker_upper = str(ticker).upper()
    required_aligned = 5 if ticker_upper in HIGH_CAUTION_TICKERS else 4
    if ticker_upper in LONG_ENTRY_WATCHLIST and dominant_direction == "bullish":
        required_aligned = max(required_aligned, 5)
    risk_flags: List[str] = []
    if ticker_upper in HIGH_CAUTION_TICKERS:
        risk_flags.append("high_caution_ticker")
    if ticker_upper in LONG_ENTRY_WATCHLIST and dominant_direction == "bullish":
        risk_flags.append("long_watchlist_requires_stronger_trend")
    if trend_strength < 18:
        risk_flags.append("weak_adx")
    if conflict_count >= 2:
        risk_flags.append("conflicting_indicators")
    if volatility >= 0.35:
        risk_flags.append("high_volatility")
    if volume_ratio < 0.8:
        risk_flags.append("weak_volume_confirmation")

    if aligned >= required_aligned and conflict_count <= 1 and trend_strength >= 20:
        tradeability = "high"
    elif aligned >= 3 and conflict_count <= 2 and trend_strength >= 18:
        tradeability = "medium"
    else:
        tradeability = "low"

    if ticker_upper in LONG_ENTRY_WATCHLIST and dominant_direction == "bullish":
        if trend_strength < 22 or conflict_count > 0 or volume_ratio < 0.9:
            tradeability = "low"
            if trend_strength < 22:
                risk_flags.append("watchlist_long_weak_trend")
            if conflict_count > 0:
                risk_flags.append("watchlist_long_indicator_conflict")
            if volume_ratio < 0.9:
                risk_flags.append("watchlist_long_weak_volume")
        elif tradeability == "high" and trend_strength < 25:
            tradeability = "medium"
            risk_flags.append("watchlist_long_medium_trend_only")

    return {
        "sector": get_sector(ticker),
        "sector_guidance": get_sector_guidance(ticker, "technical"),
        "market_regime": regime,
        "dominant_direction": dominant_direction,
        "tradeability": tradeability,
        "indicator_votes": votes,
        "features": {
            "volatility": volatility,
            "trend_strength": trend_strength,
            "price_range": float(features.get("price_range") or 0.0),
            "volume_ratio": volume_ratio,
        },
        "risk_flags": risk_flags,
    }


def parse_fundamental_factors(fundamentals: str, metadata: Optional[Dict[str, Any]], ticker: str) -> Dict[str, Any]:
    role_to_group = {
        "inventory": "inventory",
        "supply": "supply",
        "demand": "demand",
        "trade_flow": "import_export",
        "cost_profit": "profit",
        "price_basis": "basis",
        "price_anchor": "basis",
        "macro_downstream": "macro_policy",
        "macro": "macro_policy",
        "context": "context",
    }
    groups: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    pattern = re.compile(
        r"^(?P<name>.+?):\s+.+?\[role:\s*(?P<role>[^;]+);\s*frequency:\s*(?P<freq>[^;]+);\s*last 5 obs trend:\s*(?P<trend>\w+)",
        re.IGNORECASE,
    )
    for line in fundamentals.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        role = match.group("role").strip()
        group = role_to_group.get(role, "context")
        groups[group].append(
            {
                "name": match.group("name").strip(),
                "role": role,
                "frequency": match.group("freq").strip(),
                "trend": match.group("trend").strip().lower(),
            }
        )

    quality = metadata or {}
    configured = int(quality.get("configured_indicator_count") or 0)
    loaded = int(quality.get("loaded_indicator_count") or 0)
    missing_like = int(quality.get("missing_like_count") or 0)
    if not missing_like:
        missing_like = (
            int(quality.get("missing_file_count") or 0)
            + int(quality.get("empty_frame_count") or 0)
            + int(quality.get("no_data_before_count") or 0)
        )
    stale = int(quality.get("stale_indicator_count") or 0)
    near_stale = int(quality.get("near_stale_indicator_count") or 0)
    low_confidence = int(quality.get("low_confidence_indicator_count") or 0)
    coverage_ratio = float(quality.get("coverage_ratio") or (loaded / configured if configured else 0.0))
    missing_ratio = float(quality.get("missing_ratio") or (missing_like / configured if configured else 0.0))
    stale_ratio = float(quality.get("stale_ratio") or (stale / configured if configured else 0.0))
    near_stale_ratio = float(quality.get("near_stale_ratio") or (near_stale / configured if configured else 0.0))

    risk_flags: List[str] = []
    if coverage_ratio < 0.65:
        risk_flags.append("low_coverage")
    if missing_ratio >= 0.25:
        risk_flags.append("missing_fundamental_inputs")
    if stale_ratio >= 0.20:
        risk_flags.append("stale_fundamental_inputs")
    if near_stale_ratio >= 0.30:
        risk_flags.append("near_stale_fundamental_inputs")
    if low_confidence > 0:
        risk_flags.append("low_confidence_inputs")

    group_counts = {
        group: Counter(item["trend"] for item in items)
        for group, items in groups.items()
    }
    directional_groups = 0
    conflicting_groups = 0
    for group, counts in group_counts.items():
        if counts.get("up", 0) or counts.get("down", 0):
            directional_groups += 1
        if counts.get("up", 0) and counts.get("down", 0):
            conflicting_groups += 1

    if risk_flags and ("low_coverage" in risk_flags or "stale_fundamental_inputs" in risk_flags):
        tradeability = "low"
    elif directional_groups >= 3 and conflicting_groups <= 1:
        tradeability = "high"
    elif directional_groups >= 2 and conflicting_groups <= 2:
        tradeability = "medium"
    else:
        tradeability = "low"

    return {
        "sector": get_sector(ticker),
        "sector_guidance": get_sector_guidance(ticker, "fundamental"),
        "factor_groups": {group: items[:8] for group, items in groups.items()},
        "factor_group_counts": {group: dict(counts) for group, counts in group_counts.items()},
        "data_quality": {
            "configured": configured,
            "loaded": loaded,
            "coverage_ratio": coverage_ratio,
            "missing_ratio": missing_ratio,
            "stale_ratio": stale_ratio,
            "near_stale_ratio": near_stale_ratio,
            "low_confidence_count": low_confidence,
        },
        "basis": quality.get("basis"),
        "tradeability": tradeability,
        "risk_flags": risk_flags,
    }


NEWS_EVENT_RULES = {
    "supply": ["产量", "供应", "开工", "检修", "装置", "减产", "增产", "发运", "到港", "矿山"],
    "demand": ["需求", "消费", "成交", "订单", "开工率", "基建", "地产", "饲料", "纺织", "聚酯"],
    "inventory": ["库存", "累库", "去库", "仓单", "库容"],
    "policy": ["政策", "关税", "配额", "限产", "补贴", "监管", "收储", "抛储"],
    "import_export": ["进口", "出口", "到港", "船货", "巴西", "印尼", "马来", "澳洲"],
    "weather": ["天气", "降雨", "干旱", "洪水", "寒潮", "霜冻", "台风"],
    "overseas": ["LME", "美元", "美联储", "海外", "外盘", "国际"],
    "macro": ["宏观", "利率", "汇率", "PMI", "通胀", "美元", "经济"],
}

BULLISH_NEWS_WORDS = ["减产", "去库", "下降", "收紧", "短缺", "上涨", "检修", "限产", "需求改善", "进口减少"]
BEARISH_NEWS_WORDS = ["增产", "累库", "上升", "宽松", "下跌", "进口增加", "需求疲弱", "库存增加", "供应增加"]
STRONG_EVENT_WORDS = ["大幅", "显著", "紧张", "短缺", "创", "暴跌", "暴涨", "政策", "限产", "检修"]


def summarize_news_events(news_items: Iterable[Any], ticker: str) -> Dict[str, Any]:
    seen_titles = set()
    events = []
    direction_counts = Counter()
    type_counts = Counter()
    for item in news_items:
        title = getattr(item, "title", "") or ""
        content = getattr(item, "content", "") or ""
        publish_time = getattr(item, "publish_time", "") or ""
        normalized_title = re.sub(r"\s+", " ", title).strip()
        if not normalized_title or normalized_title in seen_titles:
            continue
        seen_titles.add(normalized_title)
        text = f"{title} {content}"
        event_types = [
            event_type
            for event_type, keywords in NEWS_EVENT_RULES.items()
            if any(keyword.lower() in text.lower() for keyword in keywords)
        ] or ["context"]
        bullish_hits = sum(1 for word in BULLISH_NEWS_WORDS if word in text)
        bearish_hits = sum(1 for word in BEARISH_NEWS_WORDS if word in text)
        if bullish_hits > bearish_hits:
            direction = "bullish"
        elif bearish_hits > bullish_hits:
            direction = "bearish"
        else:
            direction = "neutral"
        strength = "strong" if any(word in text for word in STRONG_EVENT_WORDS) else "medium" if direction != "neutral" else "weak"
        for event_type in event_types:
            type_counts[event_type] += 1
        direction_counts[direction] += 1
        events.append(
            {
                "title": normalized_title,
                "publish_time": str(publish_time),
                "event_types": event_types,
                "direction_hint": direction,
                "strength": strength,
            }
        )

    mixed = bool(direction_counts.get("bullish", 0) and direction_counts.get("bearish", 0))
    strong_direction = max(direction_counts.get("bullish", 0), direction_counts.get("bearish", 0))
    risk_flags: List[str] = []
    if not events:
        risk_flags.append("no_news")
    if mixed:
        risk_flags.append("mixed_news_direction")
    if strong_direction == 0:
        risk_flags.append("no_directional_news")
    if len(events) <= 2:
        risk_flags.append("thin_news_sample")

    if strong_direction >= 3 and not mixed:
        tradeability = "high"
    elif strong_direction >= 2 and len(risk_flags) <= 1:
        tradeability = "medium"
    else:
        tradeability = "low"

    return {
        "sector": get_sector(ticker),
        "sector_guidance": get_sector_guidance(ticker, "news"),
        "events": events[:10],
        "event_type_counts": dict(type_counts),
        "direction_counts": dict(direction_counts),
        "freshness_score": 1.0 if events else 0.0,
        "relevance_score": 0.8 if events else 0.0,
        "tradeability": tradeability,
        "risk_flags": risk_flags,
    }


def apply_signal_quality_gate(
    signal: AnalystSignal,
    quality_context: Dict[str, Any],
    full_config: Dict[str, Any],
    analyst: str,
) -> AnalystSignal:
    cfg = get_analyst_llm_config(full_config, analyst)
    tradeability = str(quality_context.get("tradeability") or "medium")
    risk_flags = list(quality_context.get("risk_flags") or [])
    original_signal = signal_value(signal.signal)
    original_confidence = float(signal.confidence or 0.0)
    reason = None

    if tradeability == "low":
        signal.confidence = min(original_confidence, cfg["cap_low_tradeability_confidence"])
        if cfg["force_neutral_low_tradeability"]:
            signal.signal = Signal.NEUTRAL
            reason = f"low tradeability; forced Neutral; flags={risk_flags}"
        else:
            reason = f"low tradeability; confidence capped; flags={risk_flags}"
    elif tradeability == "medium":
        signal.confidence = min(original_confidence, cfg["cap_medium_tradeability_confidence"])
        reason = "medium tradeability; confidence capped"

    if reason:
        signal.justification += (
            f"\n[Analyst quality gate: {original_signal}/{original_confidence:.2f} -> "
            f"{signal_value(signal.signal)}/{signal.confidence:.2f}; {reason}]"
        )

    signal.metadata = {
        **(getattr(signal, "metadata", {}) or {}),
        "tradeability": tradeability,
        "risk_flags": risk_flags,
        "quality_gate": {
            "original_signal": original_signal,
            "original_confidence": original_confidence,
            "final_signal": signal_value(signal.signal),
            "final_confidence": float(signal.confidence or 0.0),
        },
    }
    return signal


def _format_json_block(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def write_analyst_report(
    *,
    analyst: str,
    ticker: str,
    trading_date: Any,
    signal: AnalystSignal,
    full_config: Dict[str, Any],
    sections: Dict[str, Any],
) -> Optional[str]:
    cfg = get_analyst_llm_config(full_config, analyst)
    if not cfg.get("write_decision_reports", True):
        return None

    trading_date_value = normalize_trading_date(trading_date)
    report_dir = Path(logger.log_dir) / "analyst_decisions" / logger.run_id
    report_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{trading_date_value}_{str(ticker).upper()}_{analyst}.md"
    path = report_dir / file_name

    title = analyst.replace("_", " ").title()
    lines = [
        f"# {title} Decision Report",
        "",
        f"Ticker: {ticker}",
        f"Trading Date: {trading_date_value}",
        f"LLM Path: {sections.get('llm_path', 'cloud_only')}",
        f"Model: {cfg.get('cloud_model')}",
        f"Signal: {signal_value(signal.signal)}",
        f"Confidence: {float(signal.confidence or 0.0):.2f}",
        f"Tradeability: {sections.get('tradeability', signal.metadata.get('tradeability', 'unknown'))}",
        f"Sector: {sections.get('sector', get_sector(ticker))}",
        "",
    ]
    for name, payload in sections.items():
        if name in {"llm_path", "tradeability", "sector"}:
            continue
        lines.extend([f"## {name}", ""])
        if isinstance(payload, str):
            lines.extend([sanitize_visible_text(payload), ""])
        else:
            lines.extend(["```json", sanitize_visible_text(_format_json_block(payload)), "```", ""])
    lines.extend(["## Final Reason", "", sanitize_visible_text(signal.justification), ""])

    path.write_text("\n".join(lines), encoding="utf-8")
    rel_path = os.path.relpath(path, logger.log_dir)
    logger.info(
        f"[ANALYST SUMMARY] {ticker} {analyst} signal={signal_value(signal.signal)} "
        f"confidence={float(signal.confidence or 0.0):.2f} "
        f"tradeability={sections.get('tradeability', signal.metadata.get('tradeability', 'unknown'))} "
        f"report=logs/{rel_path}"
    )
    return str(path)


def format_technical_summary_for_prompt(context: Dict[str, Any]) -> str:
    return (
        "=== Structured Technical Precheck ===\n"
        f"Sector: {context.get('sector')}\n"
        f"Sector guidance: {context.get('sector_guidance')}\n"
        f"Market regime: {context.get('market_regime')}\n"
        f"Dominant direction: {context.get('dominant_direction')}\n"
        f"Tradeability: {context.get('tradeability')}\n"
        f"Indicator votes: {_format_json_block(context.get('indicator_votes'))}\n"
        f"Market features: {_format_json_block(context.get('features'))}\n"
        f"Risk flags: {', '.join(context.get('risk_flags') or []) or 'none'}\n"
        "Decision rule: do not force a directional signal. If tradeability is low, prefer Neutral.\n"
    )


def format_fundamental_summary_for_prompt(context: Dict[str, Any]) -> str:
    return (
        "\n\n=== Structured Fundamental Precheck ===\n"
        f"Sector: {context.get('sector')}\n"
        f"Sector guidance: {context.get('sector_guidance')}\n"
        f"Tradeability: {context.get('tradeability')}\n"
        f"Data quality: {_format_json_block(context.get('data_quality'))}\n"
        f"Factor group counts: {_format_json_block(context.get('factor_group_counts'))}\n"
        f"Basis: {_format_json_block(context.get('basis'))}\n"
        f"Risk flags: {', '.join(context.get('risk_flags') or []) or 'none'}\n"
        "Directional discipline: require at least two independent factor groups supporting a direction. "
        "If factors or data quality are weak, prefer Neutral.\n"
    )


def format_news_summary_for_prompt(context: Dict[str, Any]) -> str:
    return (
        "\n\n=== Structured News Event Precheck ===\n"
        f"Sector: {context.get('sector')}\n"
        f"Sector guidance: {context.get('sector_guidance')}\n"
        f"Tradeability: {context.get('tradeability')}\n"
        f"Freshness score: {context.get('freshness_score')}\n"
        f"Relevance score: {context.get('relevance_score')}\n"
        f"Event type counts: {_format_json_block(context.get('event_type_counts'))}\n"
        f"Direction counts: {_format_json_block(context.get('direction_counts'))}\n"
        f"Risk flags: {', '.join(context.get('risk_flags') or []) or 'none'}\n"
        f"Events: {_format_json_block(context.get('events'))}\n"
        "News discipline: separate informative news from tradable news. "
        "If events are stale, mixed, weak, or weakly related, prefer Neutral.\n"
    )
