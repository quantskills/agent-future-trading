from __future__ import annotations

"""Finoview factor catalog and no-lookahead snapshot helpers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import yaml

from tools.agent_tools.analysis.data_usage import read_finoview_feather_cached


DATE_COLUMNS = ("tradeDate", "date", "datetime", "publish_date", "report_date")

ROLE_KEYWORDS = {
    "inventory": ["stock", "inventory", "warehouse", "port_stock", "factory_stock", "social_stock"],
    "supply": ["yield", "operate_rate", "capacity_utilization", "production", "output"],
    "demand": ["demand", "trade_volume", "shipment", "sales", "consumption", "order"],
    "import_export": ["jk", "ck", "import", "export", "arrivals", "departures", "freight"],
    "cost_profit": ["profit", "cost", "processing_fee", "spread", "oer"],
    "price_basis": ["spot_price", "future_close_price", "basis", "price"],
    "macro_policy": ["macro", "pmi", "dollar", "usdcny", "fed", "cpi", "weather"],
}

FREQUENCY_HINTS = {
    "hf": "high_frequency",
    "weekly": "weekly",
    "monthly": "monthly",
    "month": "monthly",
    "season": "seasonal",
}

FRESHNESS_DAYS = {
    "high_frequency": 3,
    "daily": 7,
    "weekly": 14,
    "monthly": 45,
    "seasonal": 90,
    "unknown": 14,
}

TRADING_UNIVERSE_TICKERS = {"BU", "C", "CF", "EB", "HC", "I", "J", "M", "MA", "P", "PB", "RB", "SR", "TA", "ZN"}

EXPLICIT_FACTOR_GROUP_OVERRIDES = {
    "i_arrivals": "import_export",
    "i_arrivals_port": "import_export",
    "i_expected_arrivals_7d": "import_export",
    "i_expected_arrivals_14d": "import_export",
    "i_departures": "import_export",
    "i_departures_australia": "import_export",
    "i_departures_brazil": "import_export",
    "i_port_stock": "inventory",
    "i_spot_price": "price_basis",
    "i_port_spot_price": "price_basis",
    "i_trade_volume": "demand",
    "i_capacity_utilization_rate": "supply",
    "i_yield": "supply",
    "i_jk_demand": "demand",
    "i_gc_demand": "demand",
    "i_jk_factory_stock": "inventory",
    "bu_demand": "demand",
    "bu_factory_stock": "inventory",
    "bu_refinery_stock": "inventory",
    "bu_social_stock": "inventory",
    "bu_social_stock_company": "inventory",
    "bu_spot_price": "price_basis",
    "bu_spot_price_shandong": "price_basis",
    "bu_future_close_price": "price_basis",
    "bu_operate_rate_shandong": "supply",
    "bu_yield": "supply",
    "bu_shipment": "demand",
    "bu_profit_shandong": "cost_profit",
    "eb_factory_stock": "inventory",
    "eb_port_stock": "inventory",
    "eb_port_stock_hn": "inventory",
    "eb_spot_price": "price_basis",
    "eb_future_close_price": "price_basis",
    "eb_capacity_utilization_rate": "supply",
    "eb_operate_rate": "supply",
    "eb_yield": "supply",
    "eb_weekly_yield": "supply",
    "eb_downstream_operate_rate": "demand",
    "eb_equipment_profit_unit": "cost_profit",
    "eb_equipment_profit_nonunit": "cost_profit",
    "j_factory_stock": "inventory",
    "j_port_stock": "inventory",
    "j_steel_factory_stock": "inventory",
    "j_steel_factory_stock_days": "inventory",
    "j_spot_price_rizhao": "price_basis",
    "j_spot_price_tianjin": "price_basis",
    "j_capacity_utilization_rate": "supply",
    "j_yield": "supply",
    "j_profit": "cost_profit",
    "j_jm_freight_fee": "cost_profit",
    "sr_spot_price": "price_basis",
    "sr_spot_price_kunming": "price_basis",
    "sr_spot_price_liuzhou": "price_basis",
    "sr_spot_price_nanning": "price_basis",
    "sr_future_close_price": "price_basis",
    "sr_trade_volume": "demand",
    "sr_domestic_sales_rate": "demand",
    "sr_yield": "supply",
    "sr_jk_volume_total": "import_export",
    "sr_jk_volume_brazil": "import_export",
    "sr_jk_profit_quota_brazil": "cost_profit",
    "sr_jk_profit_non_quota_brazil": "cost_profit",
    "sr_profit_brazil": "cost_profit",
}


@dataclass(frozen=True)
class FactorCatalogEntry:
    file: str
    ticker: str
    factor_name: str
    factor_group: str
    freq: str
    date_column: Optional[str]
    value_columns: List[str]
    release_lag_days: int
    freshness_threshold_days: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "ticker": self.ticker,
            "factor_name": self.factor_name,
            "factor_group": self.factor_group,
            "freq": self.freq,
            "date_column": self.date_column,
            "value_columns": list(self.value_columns),
            "release_lag_days": self.release_lag_days,
            "freshness_threshold_days": self.freshness_threshold_days,
        }


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_finoview_dir() -> Path:
    return _project_root() / "data" / "Fundamental_data" / "Finoview_data"


def default_catalog_config_path() -> Path:
    return _project_root() / "src" / "config" / "finoview_factor_catalog.yaml"


def load_catalog_config(path: Optional[Path] = None) -> Dict[str, Any]:
    config_path = path or default_catalog_config_path()
    if not config_path.exists():
        return {}
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def infer_ticker_from_file(stem: str) -> str:
    prefix = stem.split("_", 1)[0].upper()
    if prefix in {"IRON"}:
        return "I"
    if prefix in {"A", "Y", "OI", "RM"}:
        return "M"
    if prefix in {"BZ", "ABS", "EPS", "PS", "PE"}:
        return "EB"
    if prefix in {"CS"}:
        return "C"
    if prefix in {"LAND", "HOUSE"}:
        return "RB"
    if prefix in {"WEATHER"}:
        return "P"
    return prefix


def infer_factor_group(name: str) -> str:
    lower = name.lower()
    if lower in EXPLICIT_FACTOR_GROUP_OVERRIDES:
        return EXPLICIT_FACTOR_GROUP_OVERRIDES[lower]
    for group, keywords in ROLE_KEYWORDS.items():
        if any(keyword in lower for keyword in keywords):
            return group
    return "unknown_factor"


def infer_frequency(name: str) -> str:
    lower = name.lower()
    for key, freq in FREQUENCY_HINTS.items():
        if key in lower:
            return freq
    return "daily"


def _date_column(columns: Iterable[str]) -> Optional[str]:
    column_set = {str(column) for column in columns}
    for column in DATE_COLUMNS:
        if column in column_set:
            return column
    return None


def _value_columns(columns: Iterable[str], date_column: Optional[str], stem: str) -> List[str]:
    values = [str(column) for column in columns if str(column) != str(date_column)]
    preferred = [stem, "value", "price", "close"]
    ordered = [column for column in preferred if column in values]
    ordered.extend(column for column in values if column not in ordered)
    return ordered[:5]


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def build_factor_catalog(
    *,
    data_dir: Optional[Path] = None,
    limit_to_tickers: Optional[Iterable[str]] = None,
    catalog_config_path: Optional[Path] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Scan local Finoview feather schemas into a ticker -> factor catalog."""
    root = data_dir or default_finoview_dir()
    catalog_config = load_catalog_config(catalog_config_path)
    ticker_overrides = {
        str(k).lower(): str(v).upper()
        for k, v in (catalog_config.get("ticker_overrides") or {}).items()
    }
    factor_group_overrides = {
        str(k).lower(): str(v)
        for k, v in (catalog_config.get("factor_group_overrides") or {}).items()
    }
    frequency_overrides = {
        str(k).lower(): str(v)
        for k, v in (catalog_config.get("frequency_overrides") or {}).items()
    }
    lag_overrides = {
        str(k).lower(): int(v)
        for k, v in (catalog_config.get("release_lag_days") or {}).items()
    }
    freshness_overrides = {
        str(k).lower(): int(v)
        for k, v in (catalog_config.get("freshness_threshold_days") or {}).items()
    }
    allowed = {str(item).upper() for item in limit_to_tickers or []}
    catalog: Dict[str, List[Dict[str, Any]]] = {}
    if not root.exists():
        return catalog
    for path in sorted(root.glob("*.feather")):
        stem = path.stem
        stem_key = stem.lower()
        ticker = ticker_overrides.get(stem_key) or infer_ticker_from_file(stem)
        if allowed and ticker not in allowed:
            continue
        try:
            df = read_finoview_feather_cached(path)
            columns = list(df.columns)
        except Exception:
            columns = []
        date_column = _date_column(columns)
        freq = frequency_overrides.get(stem_key) or infer_frequency(stem)
        threshold = freshness_overrides.get(stem_key) or FRESHNESS_DAYS.get(freq, FRESHNESS_DAYS["unknown"])
        entry = FactorCatalogEntry(
            file=path.name,
            ticker=ticker,
            factor_name=stem,
            factor_group=factor_group_overrides.get(stem_key) or infer_factor_group(stem),
            freq=freq,
            date_column=date_column,
            value_columns=_value_columns(columns, date_column, stem),
            release_lag_days=lag_overrides.get(stem_key, 1),
            freshness_threshold_days=threshold,
        )
        catalog.setdefault(ticker, []).append(entry.to_dict())
    return catalog


def factor_coverage_audit(catalog: Dict[str, List[Dict[str, Any]]], tickers: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    required_groups = {"price_basis", "inventory", "supply", "demand", "cost_profit", "import_export"}
    targets = {str(item).upper() for item in tickers or TRADING_UNIVERSE_TICKERS}
    rows = {}
    for ticker in sorted(targets):
        entries = catalog.get(ticker, [])
        groups = {str(entry.get("factor_group") or "unknown_factor") for entry in entries}
        rows[ticker] = {
            "factor_count": len(entries),
            "covered_groups": sorted(groups.intersection(required_groups)),
            "missing_required_groups": sorted(required_groups.difference(groups)),
            "tradable_minimum_coverage": len(groups.intersection(required_groups)) >= 4,
        }
    return {
        "required_groups": sorted(required_groups),
        "coverage_tickers": sorted(targets),
        "rows": rows,
        "all_coverage_tickers_ready": all(row["tradable_minimum_coverage"] for row in rows.values()),
    }


def _latest_visible_row(
    df: pd.DataFrame,
    *,
    date_column: Optional[str],
    trade_date: Any,
    release_lag_days: int,
) -> tuple[Optional[pd.Series], Optional[pd.Timestamp], str]:
    if df.empty:
        return None, None, "empty"
    if not date_column or date_column not in df.columns:
        return df.iloc[-1], None, "no_date_column"
    dates = pd.to_datetime(df[date_column], errors="coerce")
    trade_dt = pd.to_datetime(trade_date)
    effective_lag_days = max(1, int(release_lag_days or 1))
    cutoff = trade_dt - pd.Timedelta(days=effective_lag_days)
    # A pre-open factor dated D becomes visible when D + lag_days <= trade_date.
    visible = df.loc[dates <= cutoff].copy()
    if visible.empty:
        return None, None, "no_visible_data"
    latest_idx = visible.index[-1]
    return df.loc[latest_idx], dates.loc[latest_idx], "ok"


def build_factor_snapshot(
    ticker: str,
    trade_date: Any,
    *,
    data_dir: Optional[Path] = None,
    catalog: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    max_factors: int = 80,
) -> Dict[str, Any]:
    """Build a no-lookahead factor snapshot for one ticker and trade date."""
    ticker_upper = str(ticker or "").upper()
    root = data_dir or default_finoview_dir()
    catalog_by_ticker = catalog or build_factor_catalog(data_dir=root, limit_to_tickers=[ticker_upper])
    entries = catalog_by_ticker.get(ticker_upper, [])
    snapshot = {
        "ticker": ticker_upper,
        "trade_date": str(trade_date)[:10],
        "data_dir": str(root),
        "no_lookahead_status": "ok",
        "factor_count": 0,
        "missing_count": 0,
        "stale_count": 0,
        "unknown_factor_count": 0,
        "factor_groups": {},
        "factors": [],
    }
    for entry in entries[:max_factors]:
        path = root / str(entry.get("file"))
        if not path.exists():
            snapshot["missing_count"] += 1
            continue
        try:
            df = read_finoview_feather_cached(path)
        except Exception:
            snapshot["missing_count"] += 1
            continue
        row, data_date, status = _latest_visible_row(
            df,
            date_column=entry.get("date_column"),
            trade_date=trade_date,
            release_lag_days=int(entry.get("release_lag_days") or 0),
        )
        if row is None:
            snapshot["missing_count"] += 1
            continue
        value_column = next((col for col in entry.get("value_columns") or [] if col in df.columns), None)
        value = row.get(value_column) if value_column else None
        lag_days = None
        if data_date is not None and not pd.isna(data_date):
            lag_days = int((pd.to_datetime(trade_date) - data_date).days)
        stale = lag_days is not None and lag_days > int(entry.get("freshness_threshold_days") or 14)
        if stale:
            snapshot["stale_count"] += 1
        if entry.get("factor_group") == "unknown_factor":
            snapshot["unknown_factor_count"] += 1
        factor = {
            **entry,
            "value_column": value_column,
            "latest_value": _json_value(value),
            "data_date": None if data_date is None or pd.isna(data_date) else data_date.strftime("%Y-%m-%d"),
            "lag_days": lag_days,
            "freshness_status": "stale" if stale else status,
        }
        snapshot["factors"].append(factor)
        snapshot["factor_count"] += 1
        group = str(entry.get("factor_group") or "unknown_factor")
        snapshot["factor_groups"].setdefault(group, 0)
        snapshot["factor_groups"][group] += 1
    if snapshot["factor_count"] <= 0:
        snapshot["no_lookahead_status"] = "warning"
    if snapshot["unknown_factor_count"] and snapshot["unknown_factor_count"] == snapshot["factor_count"]:
        snapshot["no_lookahead_status"] = "warning"
    return snapshot


def map_factor_snapshot_to_judgment(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw snapshot into simple deterministic tradeability judgments."""
    groups = snapshot.get("factor_groups") or {}
    factor_count = int(snapshot.get("factor_count") or 0)
    missing = int(snapshot.get("missing_count") or 0)
    stale = int(snapshot.get("stale_count") or 0)
    coverage_score = factor_count / max(1, factor_count + missing)
    freshness_score = 1.0 - (stale / max(1, factor_count))
    required_groups = {"price_basis", "inventory", "supply", "demand", "cost_profit", "import_export"}
    covered_required = sorted(required_groups.intersection(groups))
    tradable_coverage = len(covered_required) >= 3 and coverage_score >= 0.45 and freshness_score >= 0.55
    return {
        "ticker": snapshot.get("ticker"),
        "trade_date": snapshot.get("trade_date"),
        "coverage_score": coverage_score,
        "freshness_score": freshness_score,
        "covered_required_groups": covered_required,
        "tradable_coverage": tradable_coverage,
        "direction_anchor": "unknown",
        "supply_demand_state": "covered" if tradable_coverage else "insufficient_coverage",
        "evidence_quality": "medium" if tradable_coverage else "low",
        "no_lookahead_status": snapshot.get("no_lookahead_status", "unchecked"),
    }


def build_factor_attribution_payload(
    *,
    ticker: str,
    trade_date: Any,
    snapshot: Dict[str, Any],
    judgment: Dict[str, Any],
    recommendation_id: str = "",
) -> Dict[str, Any]:
    factors = snapshot.get("factors") or []
    used_factors = [
        {
            "file": item.get("file"),
            "factor_name": item.get("factor_name"),
            "factor_group": item.get("factor_group"),
            "data_date": item.get("data_date"),
            "lag_days": item.get("lag_days"),
            "freshness_status": item.get("freshness_status"),
            "value_column": item.get("value_column"),
            "latest_value": item.get("latest_value"),
        }
        for item in factors
        if item.get("factor_group") != "unknown_factor"
    ]
    return {
        "artifact_type": "FinoviewFactorAttribution",
        "ticker": str(ticker or "").upper(),
        "trade_date": str(trade_date)[:10],
        "recommendation_id": recommendation_id,
        "no_lookahead_status": snapshot.get("no_lookahead_status", "unchecked"),
        "coverage_score": judgment.get("coverage_score", 0.0),
        "freshness_score": judgment.get("freshness_score", 0.0),
        "tradable_coverage": bool(judgment.get("tradable_coverage", False)),
        "direction_anchor": judgment.get("direction_anchor", "unknown"),
        "supply_demand_state": judgment.get("supply_demand_state", "unknown"),
        "covered_required_groups": judgment.get("covered_required_groups", []),
        "used_factors": used_factors[:40],
    }
