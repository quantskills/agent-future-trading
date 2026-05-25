import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.agent_tools.market_confirmation import score_pandaai_extra_records
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

def get_sector(ticker: str) -> str:
    return SECTOR_BY_TICKER.get(str(ticker).upper(), "generic")


def get_sector_guidance(ticker: str, analyst: str) -> str:
    sector = get_sector(ticker)
    guidance = SECTOR_GUIDANCE.get(sector, {})
    return guidance.get(analyst, "Use commodity-specific evidence and prefer Neutral when evidence is mixed.")


def get_analyst_llm_config(full_config: Dict[str, Any], analyst: str) -> Dict[str, Any]:
    cfg = full_config.get("analyst_llm", {}) or {}
    llm_cfg = full_config.get("llm", {}) or {}
    del analyst
    return {
        "mode": "cloud_only",
        "cloud_model": cfg.get("cloud_model") or llm_cfg.get("model"),
        "write_decision_reports": bool(cfg.get("write_decision_reports", True)),
        "force_neutral_low_tradeability": bool(cfg.get("force_neutral_low_tradeability", True)),
        "cap_medium_tradeability_confidence": float(cfg.get("cap_medium_tradeability_confidence", 0.65)),
        "cap_low_tradeability_confidence": float(cfg.get("cap_low_tradeability_confidence", 0.35)),
        "force_neutral_stale_fundamental": bool(cfg.get("force_neutral_stale_fundamental", True)),
        "stale_fundamental_stale_ratio": float(cfg.get("stale_fundamental_stale_ratio", 0.35)),
        "stale_fundamental_freshness_score": float(cfg.get("stale_fundamental_freshness_score", 0.45)),
        "cap_stale_fundamental_confidence": float(cfg.get("cap_stale_fundamental_confidence", 0.30)),
    }


def llm_path_label(full_config: Dict[str, Any], analyst: str) -> str:
    base = get_analyst_llm_config(full_config, analyst)
    return str(base.get("mode") or "cloud_only")


def signal_value(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


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
    required_aligned = 4
    if volatility >= 0.35 or conflict_count >= 2:
        required_aligned = 5
    risk_flags: List[str] = []
    if trend_strength < 18:
        risk_flags.append("weak_adx")
    if conflict_count >= 2:
        risk_flags.append("conflicting_indicators")
    if volatility >= 0.35:
        risk_flags.append("high_volatility")
    if volatility >= 0.35 and aligned < required_aligned:
        risk_flags.append("high_volatility_requires_extra_alignment")
    if volume_ratio < 0.8:
        risk_flags.append("weak_volume_confirmation")
    if dominant_direction == "bullish" and trend_strength < 22 and volume_ratio < 0.9:
        risk_flags.append("bullish_setup_lacks_trend_volume_confirmation")

    if aligned >= required_aligned and conflict_count <= 1 and trend_strength >= 20 and volume_ratio >= 0.8:
        tradeability = "high"
    elif aligned >= 3 and conflict_count <= 2 and trend_strength >= 18:
        tradeability = "medium"
    else:
        tradeability = "low"

    if volatility >= 0.35 and tradeability == "high" and trend_strength < 25:
        tradeability = "medium"
        risk_flags.append("high_volatility_caps_tradeability")
    if dominant_direction == "bullish" and trend_strength < 22 and volume_ratio < 0.9:
        tradeability = "low"

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
    factor_judgment = quality.get("finoview_factor_judgment") or {}
    factor_snapshot = quality.get("finoview_factor_snapshot") or {}
    factor_attribution = quality.get("finoview_factor_attribution") or {}
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

    factor_tradable = bool(factor_judgment.get("tradable_coverage"))
    if risk_flags and ("low_coverage" in risk_flags or "stale_fundamental_inputs" in risk_flags):
        tradeability = "low"
    elif factor_tradable and directional_groups >= 2 and conflicting_groups <= 1:
        tradeability = "high"
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
        "finoview_factor_snapshot": factor_snapshot,
        "finoview_factor_judgment": factor_judgment,
        "finoview_factor_attribution": factor_attribution,
        "data_quality": {
            "configured": configured,
            "loaded": loaded,
            "coverage_ratio": coverage_ratio,
            "missing_ratio": missing_ratio,
            "stale_ratio": stale_ratio,
            "near_stale_ratio": near_stale_ratio,
            "low_confidence_count": low_confidence,
            "factor_coverage_score": float(factor_judgment.get("coverage_score") or 0.0),
            "factor_freshness_score": float(factor_judgment.get("freshness_score") or 0.0),
            "no_lookahead_status": factor_judgment.get("no_lookahead_status", "unchecked"),
        },
        "basis": quality.get("basis"),
        "tradeability": tradeability,
        "risk_flags": risk_flags,
    }


_PANDAAI_FACTOR_GROUPS = {
    "basis": "price_basis",
    "warehouse_receipt": "inventory",
    "net_flow": "capital_flow",
    "variety_position_rank": "positioning",
    "symbol_position_rank": "positioning",
    "ls_ratio": "sentiment_positioning",
    "broker_net_margin_change": "capital_flow",
    "broker_net_margin": "capital_flow",
    "netposi_rank": "positioning",
    "net_cap_change": "capital_flow",
    "contract_daily_indicators": "sentiment_positioning",
    "contract_rank": "sentiment_positioning",
    "broker_variety_profit": "broker_profit",
}


def summarize_pandaai_extra_factors(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize PandaAI futures non-market data as business factor evidence."""
    if not isinstance(snapshot, dict):
        return {
            "enabled": False,
            "tradeability": "low",
            "direction_hint": "neutral",
            "features": [],
            "factor_group_counts": {},
            "record_counts": {},
            "data_missing": [],
            "feature_status": {},
            "feature_diagnostics": {},
            "data_status_groups": {},
            "errors": [],
        }

    records = snapshot.get("records") or {}
    record_counts = snapshot.get("record_counts") or {}
    feature_status = dict(snapshot.get("feature_status") or {})
    features = score_pandaai_extra_records(records)
    directional = [item for item in features if item.get("direction") in {"long", "short"}]
    long_features = [item["feature"] for item in directional if item.get("direction") == "long"]
    short_features = [item["feature"] for item in directional if item.get("direction") == "short"]
    avg_score = (
        sum(float(item.get("score") or 0.0) for item in directional) / len(directional)
        if directional
        else 0.0
    )
    if avg_score > 0.10:
        direction_hint = "long"
    elif avg_score < -0.10:
        direction_hint = "short"
    else:
        direction_hint = "neutral"

    group_counts: Dict[str, int] = {}
    for item in features:
        group = _PANDAAI_FACTOR_GROUPS.get(str(item.get("feature")), "context")
        group_counts[group] = group_counts.get(group, 0) + 1

    normalized_status: Dict[str, str] = {}
    for key, value in (record_counts or {}).items():
        try:
            count = int(value or 0)
        except Exception:
            count = 0
        normalized_status[key] = "ok" if count > 0 else str(feature_status.get(key) or "no_data")
    for key, value in feature_status.items():
        normalized_status.setdefault(str(key), str(value or "unknown"))
    data_missing = [key for key, status in normalized_status.items() if status not in {"ok", "unsupported_feature"}]
    data_status_groups: Dict[str, List[str]] = {}
    for key, status in sorted(normalized_status.items()):
        data_status_groups.setdefault(status, []).append(key)
    errors = snapshot.get("errors") or []
    if len(directional) >= 3 and len(group_counts) >= 3 and len(errors) == 0:
        tradeability = "high"
    elif len(directional) >= 1 and len(errors) <= 2:
        tradeability = "medium"
    else:
        tradeability = "low"

    return {
        "enabled": True,
        "source": "PandaAI",
        "info_cutoff": "T-1_or_earlier",
        "underlying_code": snapshot.get("underlying_code"),
        "contract_symbol": snapshot.get("contract_symbol"),
        "reference_date": snapshot.get("reference_date"),
        "lookback_days": snapshot.get("lookback_days"),
        "tradeability": tradeability,
        "direction_hint": direction_hint,
        "avg_score": avg_score,
        "long_features": long_features,
        "short_features": short_features,
        "features": features,
        "factor_group_counts": group_counts,
        "record_counts": record_counts,
        "data_missing": data_missing,
        "feature_status": normalized_status,
        "feature_diagnostics": snapshot.get("feature_diagnostics") or {},
        "data_status_groups": data_status_groups,
        "parameter_errors": data_status_groups.get("parameter_error", []),
        "no_data": data_status_groups.get("no_data", []),
        "unsupported_features": data_status_groups.get("unsupported_feature", []),
        "errors": errors,
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

    data_quality = quality_context.get("data_quality") if isinstance(quality_context.get("data_quality"), dict) else {}
    stale_ratio = _safe_float(data_quality.get("stale_ratio"), 0.0)
    freshness_score = _safe_float(data_quality.get("factor_freshness_score"), 0.0)
    stale_fundamental_direction = (
        str(analyst) == "fundamental"
        and original_signal != Signal.NEUTRAL.value
        and cfg.get("force_neutral_stale_fundamental", True)
        and (
            "stale_fundamental_inputs" in risk_flags
            or stale_ratio >= float(cfg.get("stale_fundamental_stale_ratio", 0.35))
            or (freshness_score > 0 and freshness_score < float(cfg.get("stale_fundamental_freshness_score", 0.45)))
        )
    )
    if stale_fundamental_direction:
        signal.signal = Signal.NEUTRAL
        signal.confidence = min(original_confidence, _safe_float(cfg.get("cap_stale_fundamental_confidence"), 0.30))
        reason = (
            "stale fundamental inputs; forced Neutral until fresh supply-demand evidence confirms; "
            f"stale_ratio={stale_ratio:.2f}; freshness_score={freshness_score:.2f}; flags={risk_flags}"
        )
        signal.neutral_reason = signal.neutral_reason or "stale fundamental inputs; directional anchor withheld"
        missing = list(getattr(signal, "missing_evidence", []) or [])
        for item in ("fresh supply-demand anchor", "fresh inventory/basis confirmation", "current positioning flow"):
            if item not in missing:
                missing.append(item)
        signal.missing_evidence = missing
        conflicts = list(getattr(signal, "conflicting_factors", []) or [])
        if "stale_fundamental_inputs" not in conflicts:
            conflicts.append("stale_fundamental_inputs")
        signal.conflicting_factors = conflicts
        signal.do_not_trade_reason = signal.do_not_trade_reason or "fundamental_direction_blocked_by_stale_data"

    if tradeability == "low":
        signal.confidence = min(float(signal.confidence or original_confidence), cfg["cap_low_tradeability_confidence"])
        if cfg["force_neutral_low_tradeability"]:
            signal.signal = Signal.NEUTRAL
            reason = reason or f"low tradeability; forced Neutral; flags={risk_flags}"
        else:
            reason = reason or f"low tradeability; confidence capped; flags={risk_flags}"
    elif tradeability == "medium":
        signal.confidence = min(float(signal.confidence or original_confidence), cfg["cap_medium_tradeability_confidence"])
        reason = reason or "medium tradeability; confidence capped"

    if reason:
        signal.justification += (
            f"\n[Analyst quality gate: {original_signal}/{original_confidence:.2f} -> "
            f"{signal_value(signal.signal)}/{signal.confidence:.2f}; {reason}]"
        )

    if stale_fundamental_direction and "stale_fundamental_direction_block" not in risk_flags:
        risk_flags.append("stale_fundamental_direction_block")
    signal.metadata = {
        **(getattr(signal, "metadata", {}) or {}),
        "tradeability": tradeability,
        "risk_flags": risk_flags,
        "quality_gate": {
            "original_signal": original_signal,
            "original_confidence": original_confidence,
            "final_signal": signal_value(signal.signal),
            "final_confidence": float(signal.confidence or 0.0),
            "stale_fundamental_direction_block": stale_fundamental_direction,
            "data_quality": data_quality,
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
        f"PandaAI extra factor context: {_format_json_block(context.get('pandaai_extra_factors'))}\n"
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
