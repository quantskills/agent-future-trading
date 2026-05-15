from __future__ import annotations

import argparse
import ast
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SRC_ROOT = Path(__file__).resolve().parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import pandas as pd


DEFAULT_TICKERS = ["BU", "C", "CF", "EB", "HC", "I", "J", "M", "MA", "P", "PB", "RB", "SR", "TA", "ZN"]
DEFAULT_START_DATE = "2025-01-01"
DEFAULT_END_DATE = "2025-01-31"
FRESHNESS_OVERRIDES = {
    "cf_textile_order_days": 60,
    "land_trade_volume": 60,
    "house_trade_volume": 60,
    "sr_balance": 60,
    "sr_jk_volume_brazil": 60,
    "sr_export_volume_brazil": 60,
    "y_us_stock": 60,
}
LOW_CONFIDENCE_INDICATORS = {
    "c_sorghum_spot_price": "flat during the current validation window; downweighted in router output",
    "c_barley_spot_price": "low variation during the current validation window; replacement-grain context only",
    "pb_processing_fee": "low variation during the current validation window; processing-cost context only",
    "sr_spot_price": "low variation during the current validation window; regional SR spot prices are preferred",
}


@dataclass
class IndicatorAudit:
    ticker: str
    indicator: str
    file_name: str
    file_exists: bool
    in_fetch_map: bool
    status: str
    selected_days: int
    stale_days: int
    max_age_days: int | None
    max_date: str
    total_rows: int
    selected_rows: int
    unique_selected_values: int | None
    null_rate: float | None
    duplicate_dates: int
    flags: list[str]
    detail: str


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parse_args() -> argparse.Namespace:
    root = repo_root()
    parser = argparse.ArgumentParser(
        description="Audit local Finoview fundamental data before running futures backtests."
    )
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=DEFAULT_END_DATE)
    parser.add_argument("--data-dir", default=str(root / "data" / "Fundamental_data" / "Finoview_data"))
    parser.add_argument("--router", default=str(root / "src" / "apis" / "router.py"))
    parser.add_argument("--finoview", default=str(root / "src" / "tools" / "data_fetch" / "Finoview_data.py"))
    parser.add_argument("--db", default=str(root / "src" / "assets" / "agentquant.db"))
    parser.add_argument("--output", default=str(root / "docs" / "fundamental_preflight_report.md"))
    parser.add_argument(
        "--severe-stale-ratio",
        type=float,
        default=0.5,
        help="A series is severely stale when stale trading days reach this share of the window.",
    )
    parser.add_argument(
        "--fail-on-p0",
        action="store_true",
        help="Exit with code 2 if missing/severely stale/unusable P0 items are found.",
    )
    return parser.parse_args()


def literal_assignment_from_file(path: Path, name: str) -> Any:
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def target_matches(target: ast.AST) -> bool:
        if isinstance(target, ast.Name):
            return target.id == name
        if isinstance(target, ast.Attribute):
            return target.attr == name
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if target_matches(target):
                    return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign):
            if target_matches(node.target):
                return ast.literal_eval(node.value)
    raise ValueError(f"Could not find literal assignment {name!r} in {path}")


def normalize_indicator_map(raw: dict[str, Any]) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            normalized[key.upper()] = [str(item) for item in value.values()]
        elif isinstance(value, (list, tuple, set)):
            normalized[key.upper()] = [str(item) for item in value]
        else:
            raise TypeError(f"Unsupported indicator map value for {key}: {type(value).__name__}")
    return normalized


def max_days_for_indicator(indicator: str) -> int:
    lower_name = indicator.lower()
    for pattern, max_days in FRESHNESS_OVERRIDES.items():
        if pattern in lower_name:
            return max_days

    monthly_keywords = [
        "arrivals",
        "battery_operate_rate",
        "departures",
        "demand",
        "domestic_sales_rate",
        "electric_yield",
        "feed_yield",
        "galvanize_operate_rate",
        "alloy_operate_rate",
        "oxide_operate_rate",
        "usda",
        "is_ratio",
        "planting",
        "harvest",
        "jk_volume",
        "ck_volume",
        "jk_profit",
        "import",
        "export",
        "monthly",
        "production",
        "consumption",
        "balance",
        "global",
        "season",
        "oer_malaysia",
        "pet_downstream_stock",
        "recycle_yield",
        "smm_",
        "stock_days",
        "stock_indonesia",
        "stock_malaysia",
        "yield_indonesia",
        "yield_malaysia",
        "sow",
        "hog",
        "pmi",
        "order_days",
        "pb_yield",
        "sr_yield",
        "sr_trade_volume",
        "usa_stock",
        "y_usa_stock",
        "zn_trade_volume",
    ]
    weekly_keywords = [
        "stock",
        "operate_rate",
        "capacity",
        "profit",
        "yield",
        "shipment",
        "arrival",
        "sales_progress",
        "trade_volume",
    ]
    daily_keywords = [
        "spot_price",
        "basis",
        "futures_price",
        "volume",
        "open_interest",
        "price",
    ]

    if any(keyword in lower_name for keyword in monthly_keywords):
        return 45
    if any(keyword in lower_name for keyword in weekly_keywords):
        return 14
    if any(keyword in lower_name for keyword in daily_keywords):
        return 7
    return 14


def date_column(columns: pd.Index) -> str | None:
    preferred = ["date", "tradeDate", "trading_date", "trade_date", "datetime"]
    lowered = {str(col).lower(): col for col in columns}
    for candidate in preferred:
        if candidate.lower() in lowered:
            return str(lowered[candidate.lower()])
    return None


def value_column(columns: pd.Index, indicator: str) -> str | None:
    exact = {str(col): col for col in columns}
    if indicator in exact:
        return str(exact[indicator])

    preferred = ["value", "close", "price", "index", "data", "num"]
    lowered = {str(col).lower(): col for col in columns}
    for candidate in preferred:
        if candidate in lowered:
            return str(lowered[candidate])
    metadata_columns = {
        "date",
        "tradedate",
        "trading_date",
        "trade_date",
        "datetime",
        "time",
        "year",
        "isflag",
        "viewtag",
        "recordtime",
    }
    for col in columns:
        if str(col).lower() in metadata_columns:
            continue
        return str(col)
    return None


def trading_days(db_path: Path, start_date: str, end_date: str) -> list[pd.Timestamp]:
    if db_path.exists():
        try:
            with sqlite3.connect(db_path) as conn:
                rows = pd.read_sql_query(
                    """
                    select distinct trading_date
                    from daily_settlement
                    where trading_date between ? and ?
                    order by trading_date
                    """,
                    conn,
                    params=[start_date, end_date],
                )
            if not rows.empty:
                return [pd.Timestamp(day).normalize() for day in rows["trading_date"].tolist()]
        except Exception:
            pass
    return [day.normalize() for day in pd.bdate_range(start_date, end_date)]


def last_available_date(dates: pd.Series, trade_day: pd.Timestamp) -> pd.Timestamp | None:
    available = dates[dates <= trade_day]
    if available.empty:
        return None
    return pd.Timestamp(available.max()).normalize()


def audit_indicator(
    *,
    ticker: str,
    indicator: str,
    data_dir: Path,
    fetch_map: set[str],
    days: list[pd.Timestamp],
    severe_stale_ratio: float,
) -> IndicatorAudit:
    file_name = f"{indicator}.feather"
    file_path = data_dir / file_name
    in_fetch_map = indicator in fetch_map

    if not file_path.exists():
        return IndicatorAudit(
            ticker=ticker,
            indicator=indicator,
            file_name=file_name,
            file_exists=False,
            in_fetch_map=in_fetch_map,
            status="MISSING_FILE",
            selected_days=0,
            stale_days=len(days),
            max_age_days=None,
            max_date="",
            total_rows=0,
            selected_rows=0,
            unique_selected_values=None,
            null_rate=None,
            duplicate_dates=0,
            flags=["missing_file"],
            detail="Local feather file does not exist.",
        )

    try:
        frame = pd.read_feather(file_path)
    except Exception as exc:
        return IndicatorAudit(
            ticker=ticker,
            indicator=indicator,
            file_name=file_name,
            file_exists=True,
            in_fetch_map=in_fetch_map,
            status="READ_ERROR",
            selected_days=0,
            stale_days=len(days),
            max_age_days=None,
            max_date="",
            total_rows=0,
            selected_rows=0,
            unique_selected_values=None,
            null_rate=None,
            duplicate_dates=0,
            flags=["read_error"],
            detail=str(exc),
        )

    total_rows = len(frame)
    if total_rows == 0:
        return IndicatorAudit(
            ticker=ticker,
            indicator=indicator,
            file_name=file_name,
            file_exists=True,
            in_fetch_map=in_fetch_map,
            status="EMPTY",
            selected_days=0,
            stale_days=len(days),
            max_age_days=None,
            max_date="",
            total_rows=0,
            selected_rows=0,
            unique_selected_values=None,
            null_rate=None,
            duplicate_dates=0,
            flags=["empty"],
            detail="File has zero rows.",
        )

    date_col = date_column(frame.columns)
    if date_col is None:
        return IndicatorAudit(
            ticker=ticker,
            indicator=indicator,
            file_name=file_name,
            file_exists=True,
            in_fetch_map=in_fetch_map,
            status="NO_DATE_COLUMN",
            selected_days=0,
            stale_days=len(days),
            max_age_days=None,
            max_date="",
            total_rows=total_rows,
            selected_rows=0,
            unique_selected_values=None,
            null_rate=None,
            duplicate_dates=0,
            flags=["no_date_column"],
            detail=f"Columns: {', '.join(map(str, frame.columns))}",
        )

    val_col = value_column(frame.columns, indicator)
    if val_col is None:
        return IndicatorAudit(
            ticker=ticker,
            indicator=indicator,
            file_name=file_name,
            file_exists=True,
            in_fetch_map=in_fetch_map,
            status="NO_VALUE_COLUMN",
            selected_days=0,
            stale_days=len(days),
            max_age_days=None,
            max_date="",
            total_rows=total_rows,
            selected_rows=0,
            unique_selected_values=None,
            null_rate=None,
            duplicate_dates=0,
            flags=["no_value_column"],
            detail=f"Columns: {', '.join(map(str, frame.columns))}",
        )

    work = frame.copy()
    work["_audit_date"] = pd.to_datetime(work[date_col], errors="coerce").dt.normalize()
    work = work.dropna(subset=["_audit_date"]).sort_values("_audit_date")
    if work.empty:
        return IndicatorAudit(
            ticker=ticker,
            indicator=indicator,
            file_name=file_name,
            file_exists=True,
            in_fetch_map=in_fetch_map,
            status="NO_VALID_DATE",
            selected_days=0,
            stale_days=len(days),
            max_age_days=None,
            max_date="",
            total_rows=total_rows,
            selected_rows=0,
            unique_selected_values=None,
            null_rate=None,
            duplicate_dates=0,
            flags=["no_valid_date"],
            detail="Date column could not be parsed.",
        )

    max_date = pd.Timestamp(work["_audit_date"].max()).normalize()
    duplicate_dates = int(work["_audit_date"].duplicated().sum())
    threshold_days = max_days_for_indicator(indicator)
    stale_days = 0
    selected_dates: list[pd.Timestamp] = []
    ages: list[int] = []

    for day in days:
        last_date = last_available_date(work["_audit_date"], day)
        if last_date is None:
            stale_days += 1
            continue
        selected_dates.append(last_date)
        age = int((day - last_date).days)
        ages.append(age)
        if age > threshold_days:
            stale_days += 1

    if not selected_dates:
        status = "NO_DATA_BEFORE_WINDOW"
    else:
        severe_cutoff = max(1, math.ceil(len(days) * severe_stale_ratio))
        if stale_days >= severe_cutoff:
            status = "SEVERELY_STALE"
        elif stale_days > 0:
            status = "PARTLY_STALE"
        elif ages and max(ages) > threshold_days * 0.8:
            status = "NEAR_STALE_ONLY"
        else:
            status = "OK"

    selected = work[work["_audit_date"].isin(set(selected_dates))]
    selected_values = pd.to_numeric(selected[val_col], errors="coerce")
    unique_selected_values = int(selected_values.dropna().nunique()) if not selected.empty else 0
    null_rate = float(pd.to_numeric(work[val_col], errors="coerce").isna().mean())
    flags: list[str] = []
    if duplicate_dates:
        flags.append("duplicate_dates")
    if not in_fetch_map:
        flags.append("exists_not_in_fetch_map")
    if selected_dates and len(set(selected_dates)) >= 10 and unique_selected_values <= 1:
        flags.append("constant_selected")
    if null_rate >= 0.5:
        flags.append("high_null_rate")

    return IndicatorAudit(
        ticker=ticker,
        indicator=indicator,
        file_name=file_name,
        file_exists=True,
        in_fetch_map=in_fetch_map,
        status=status,
        selected_days=len(set(selected_dates)),
        stale_days=stale_days,
        max_age_days=max(ages) if ages else None,
        max_date=max_date.strftime("%Y-%m-%d"),
        total_rows=total_rows,
        selected_rows=len(selected),
        unique_selected_values=unique_selected_values,
        null_rate=null_rate,
        duplicate_dates=duplicate_dates,
        flags=flags,
        detail=f"date_column={date_col}; value_column={val_col}; freshness_threshold_days={threshold_days}",
    )


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    def clean(value: Any) -> str:
        text = "" if value is None else str(value)
        return text.replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(clean(value) for value in row) + " |")
    return "\n".join(lines)


def status_action(audit: IndicatorAudit) -> str:
    if audit.status == "MISSING_FILE":
        return "Add replacement data, remap indicator, or remove it from router."
    if audit.status in {"EMPTY", "NO_DATE_COLUMN", "NO_VALUE_COLUMN", "NO_VALID_DATE", "READ_ERROR"}:
        return "Fix file schema/content before rerun."
    if audit.status == "SEVERELY_STALE":
        return "Replace source or confirm a longer frequency threshold."
    if audit.indicator in LOW_CONFIDENCE_INDICATORS:
        return "Already downweighted; keep as weak context only."
    if "constant_selected" in audit.flags:
        return "Verify source values; downweight or replace if it is a dead series."
    if "exists_not_in_fetch_map" in audit.flags:
        return "Add to active Finoview fetch map or document as manual-only data."
    return "Watch in next rerun."


def p0_items(audits: list[IndicatorAudit]) -> list[IndicatorAudit]:
    bad_statuses = {
        "MISSING_FILE",
        "EMPTY",
        "NO_DATE_COLUMN",
        "NO_VALUE_COLUMN",
        "NO_VALID_DATE",
        "READ_ERROR",
        "NO_DATA_BEFORE_WINDOW",
        "SEVERELY_STALE",
    }
    return [
        audit
        for audit in audits
        if audit.status in bad_statuses
        or ("constant_selected" in audit.flags and audit.indicator not in LOW_CONFIDENCE_INDICATORS)
        or audit.null_rate == 1.0
    ]


def p1_items(audits: list[IndicatorAudit]) -> list[IndicatorAudit]:
    p0_ids = {(audit.ticker, audit.indicator) for audit in p0_items(audits)}
    return [
        audit
        for audit in audits
        if (audit.ticker, audit.indicator) not in p0_ids
        and (
            audit.status in {"PARTLY_STALE", "NEAR_STALE_ONLY"}
            or "duplicate_dates" in audit.flags
            or "exists_not_in_fetch_map" in audit.flags
            or "high_null_rate" in audit.flags
            or "constant_selected" in audit.flags
        )
    ]


def build_report(
    *,
    audits: list[IndicatorAudit],
    indicator_map: dict[str, list[str]],
    fetch_map: set[str],
    data_dir: Path,
    router_path: Path,
    finoview_path: Path,
    db_path: Path,
    days: list[pd.Timestamp],
    start_date: str,
    end_date: str,
) -> str:
    local_files = sorted(data_dir.glob("*.feather"))
    generated = datetime.now().isoformat(timespec="seconds")
    lines = [
        "# Fundamental Data Preflight Report",
        "",
        f"- Generated: {generated}",
        f"- Window: {start_date} to {end_date}",
        f"- Trading days checked: {len(days)}",
        f"- Router: `{router_path}`",
        f"- Finoview map: `{finoview_path}`",
        f"- Data dir: `{data_dir}`",
        f"- DB trading calendar: `{db_path}`",
        f"- Local feather files: {len(local_files)}",
        f"- Active Finoview fetch-map entries: {len(fetch_map)}",
        "",
        "## Summary By Ticker",
        "",
    ]

    summary_rows: list[list[Any]] = []
    for ticker in sorted(indicator_map):
        ticker_audits = [audit for audit in audits if audit.ticker == ticker]
        counts = Counter(audit.status for audit in ticker_audits)
        configured = len(ticker_audits)
        existing = sum(1 for audit in ticker_audits if audit.file_exists)
        p0_count = len(p0_items(ticker_audits))
        summary_rows.append(
            [
                ticker,
                configured,
                existing,
                f"{existing / configured:.1%}" if configured else "0.0%",
                counts.get("OK", 0),
                counts.get("MISSING_FILE", 0),
                counts.get("SEVERELY_STALE", 0),
                counts.get("PARTLY_STALE", 0),
                counts.get("NEAR_STALE_ONLY", 0),
                p0_count,
            ]
        )
    lines.append(
        markdown_table(
            [
                "Ticker",
                "Configured",
                "Files",
                "Coverage",
                "OK",
                "Missing",
                "Severe stale",
                "Partial stale",
                "Near stale",
                "P0",
            ],
            summary_rows,
        )
    )

    blockers = sorted(
        p0_items(audits),
        key=lambda item: (item.ticker, item.status, item.indicator),
    )
    lines.extend(["", "## P0 Blockers", ""])
    if blockers:
        lines.append(
            markdown_table(
                [
                    "Ticker",
                    "Indicator",
                    "Status",
                    "Max date",
                    "Stale days",
                    "Max age",
                    "Flags",
                    "Action",
                ],
                [
                    [
                        audit.ticker,
                        audit.indicator,
                        audit.status,
                        audit.max_date,
                        audit.stale_days,
                        audit.max_age_days,
                        ", ".join(audit.flags),
                        status_action(audit),
                    ]
                    for audit in blockers
                ],
            )
        )
    else:
        lines.append("No P0 blockers found.")

    watchlist = sorted(
        p1_items(audits),
        key=lambda item: (item.ticker, item.status, item.indicator),
    )
    lines.extend(["", "## P1 Watchlist", ""])
    if watchlist:
        lines.append(
            markdown_table(
                [
                    "Ticker",
                    "Indicator",
                    "Status",
                    "Max date",
                    "Stale days",
                    "Max age",
                    "Flags",
                    "Detail",
                ],
                [
                    [
                        audit.ticker,
                        audit.indicator,
                        audit.status,
                        audit.max_date,
                        audit.stale_days,
                        audit.max_age_days,
                        ", ".join(audit.flags),
                        audit.detail,
                    ]
                    for audit in watchlist
                ],
            )
        )
    else:
        lines.append("No P1 watchlist items found.")

    lines.extend(["", "## Fetch Map Gaps", ""])
    map_gaps = [audit for audit in audits if audit.file_exists and not audit.in_fetch_map]
    if map_gaps:
        grouped: dict[str, list[str]] = defaultdict(list)
        for audit in map_gaps:
            grouped[audit.ticker].append(audit.indicator)
        rows = [[ticker, ", ".join(sorted(values))] for ticker, values in sorted(grouped.items())]
        lines.append(markdown_table(["Ticker", "Existing files absent from active fetch map"], rows))
    else:
        lines.append("All configured files are present in the active Finoview fetch map.")

    lines.extend(["", "## Low-Confidence Overrides", ""])
    low_confidence_rows = [
        [audit.ticker, audit.indicator, LOW_CONFIDENCE_INDICATORS[audit.indicator], ", ".join(audit.flags)]
        for audit in audits
        if audit.indicator in LOW_CONFIDENCE_INDICATORS
    ]
    if low_confidence_rows:
        lines.append(markdown_table(["Ticker", "Indicator", "Reason", "Flags"], low_confidence_rows))
    else:
        lines.append("No low-confidence overrides are active for this ticker set.")

    lines.extend(
        [
            "",
            "## Suggested Gate",
            "",
            "- Do not start a performance rerun while P0 blockers remain, unless the indicator is intentionally removed from the router.",
            "- Review P1 items after each data refresh; many weekly/monthly series are acceptable only if their expected frequency is documented.",
            "- After P0 is cleared, rerun the futures-only backtest and compare with the latest `agentquant.db` settlement and transaction tables.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    tickers = [ticker.upper() for ticker in args.tickers]
    data_dir = Path(args.data_dir)
    router_path = Path(args.router)
    finoview_path = Path(args.finoview)
    db_path = Path(args.db)
    output_path = Path(args.output)

    raw_indicator_map = literal_assignment_from_file(router_path, "indicator_map")
    indicator_map = normalize_indicator_map(raw_indicator_map)
    filtered_indicator_map = {
        ticker: indicator_map.get(ticker, [])
        for ticker in tickers
    }
    finoview_index_map = literal_assignment_from_file(finoview_path, "index_map")
    fetch_map = set(finoview_index_map.keys())
    days = trading_days(db_path, args.start_date, args.end_date)

    audits: list[IndicatorAudit] = []
    for ticker, indicators in filtered_indicator_map.items():
        for indicator in indicators:
            audits.append(
                audit_indicator(
                    ticker=ticker,
                    indicator=indicator,
                    data_dir=data_dir,
                    fetch_map=fetch_map,
                    days=days,
                    severe_stale_ratio=args.severe_stale_ratio,
                )
            )

    report = build_report(
        audits=audits,
        indicator_map=filtered_indicator_map,
        fetch_map=fetch_map,
        data_dir=data_dir,
        router_path=router_path,
        finoview_path=finoview_path,
        db_path=db_path,
        days=days,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")

    blockers = p0_items(audits)
    print(f"Fundamental preflight report: {output_path}")
    print(f"Checked indicators: {len(audits)}")
    print(f"P0 blockers: {len(blockers)}")
    for ticker in tickers:
        ticker_audits = [audit for audit in audits if audit.ticker == ticker]
        counts = Counter(audit.status for audit in ticker_audits)
        print(
            f"{ticker}: configured={len(ticker_audits)}, "
            f"missing={counts.get('MISSING_FILE', 0)}, "
            f"severe_stale={counts.get('SEVERELY_STALE', 0)}, "
            f"partial_stale={counts.get('PARTLY_STALE', 0)}, "
            f"ok={counts.get('OK', 0)}"
        )

    if blockers and args.fail_on_p0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
