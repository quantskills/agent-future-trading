"""
Configuration Plot Runner

Generate all visualization charts for one AgentQuant futures config:
- strategy net value curve
- per-traded-ticker net value contribution + price curve + open markers

Usage:
    python src/run/plot_config.py --config src/config/dev.yaml

Output:
    Generates PNG files under AgentQuant/image by default.
"""

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Sequence

import yaml

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from database.sqlite_setup import DB_PATH
from evaluation.plot_future import SingleFutureCurvePlotter
from evaluation.plot_portfolio import PortfolioCurvePlotter


def resolve_config_path(config_path: str) -> str:
    path = Path(config_path)
    if path.is_absolute() or path.exists():
        return str(path)

    for candidate in (SRC_ROOT / path, SRC_ROOT.parent / path):
        if candidate.exists():
            return str(candidate)

    return str(path)


def load_config_exp_name(config_path: str) -> str:
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    exp_name = config.get("exp_name")
    if not exp_name:
        raise ValueError(f"exp_name not found in configuration file: {config_path}")
    return exp_name


def connect_db(db_path: Optional[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def get_config_id(db_path: Optional[str], exp_name: str) -> Optional[str]:
    conn = connect_db(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM config WHERE exp_name = ?", (exp_name,))
        row = cursor.fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def split_tickers(raw_tickers: Optional[str]) -> List[str]:
    if not raw_tickers:
        return []
    tickers = []
    for token in raw_tickers.replace(";", ",").replace(" ", ",").split(","):
        token = token.strip().upper()
        if token:
            tickers.append(token)
    return sorted(set(tickers))


def load_traded_tickers(db_path: Optional[str], config_id: str) -> List[str]:
    conn = connect_db(db_path)
    try:
        cursor = conn.cursor()
        tickers = set()

        if table_exists(cursor, "futures_transactions"):
            cursor.execute(
                """
                SELECT DISTINCT UPPER(ft.ticker) AS ticker
                FROM futures_transactions ft
                LEFT JOIN portfolio p ON ft.portfolio_id = p.id
                WHERE COALESCE(ft.config_id, p.config_id) = ?
                  AND ft.ticker IS NOT NULL
                  AND ft.ticker != ''
                """,
                (config_id,),
            )
            tickers.update(row["ticker"] for row in cursor.fetchall() if row["ticker"])

        if table_exists(cursor, "ticker_daily_pnl"):
            cursor.execute(
                """
                SELECT DISTINCT UPPER(tdp.ticker) AS ticker
                FROM ticker_daily_pnl tdp
                JOIN portfolio p ON tdp.portfolio_id = p.id
                WHERE p.config_id = ?
                  AND tdp.ticker IS NOT NULL
                  AND tdp.ticker != ''
                  AND (
                      ABS(COALESCE(tdp.lots, 0)) > 0
                      OR ABS(COALESCE(tdp.daily_pnl, 0)) > 0
                      OR ABS(COALESCE(tdp.commission, 0)) > 0
                  )
                """,
                (config_id,),
            )
            tickers.update(row["ticker"] for row in cursor.fetchall() if row["ticker"])

        return sorted(tickers)
    finally:
        conn.close()


class ConfigPlotRunner:
    """Run all config-level and traded-ticker plotting tools."""

    def __init__(
        self,
        config_path: str,
        output_dir: Optional[str] = None,
        db_path: Optional[str] = None,
        tickers: Optional[Sequence[str]] = None,
        include_price: bool = True,
        skip_portfolio: bool = False,
        strict: bool = False,
    ):
        self.config_path = resolve_config_path(config_path)
        self.output_dir = output_dir
        self.db_path = db_path
        self.requested_tickers = sorted({ticker.upper() for ticker in tickers or []})
        self.include_price = include_price
        self.skip_portfolio = skip_portfolio
        self.strict = strict
        self.exp_name = load_config_exp_name(self.config_path)
        self.config_id = get_config_id(self.db_path, self.exp_name)

    def _run_portfolio_plot(self) -> bool:
        if self.skip_portfolio:
            print("Skipping strategy net value curve by request.")
            return True

        print("\n[1/2] Generating strategy net value curve...")
        plotter = PortfolioCurvePlotter(
            self.config_path,
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        return plotter.run()

    def _resolve_tickers(self) -> List[str]:
        if self.requested_tickers:
            return self.requested_tickers
        if not self.config_id:
            return []
        return load_traded_tickers(self.db_path, self.config_id)

    def _run_ticker_plots(self, tickers: Sequence[str]) -> List[str]:
        failures = []
        total = len(tickers)
        for index, ticker in enumerate(tickers, start=1):
            print(f"\n[2/2] Generating traded-ticker chart {index}/{total}: {ticker}")
            plotter = SingleFutureCurvePlotter(
                self.config_path,
                ticker,
                output_dir=self.output_dir,
                db_path=self.db_path,
                include_price=self.include_price,
            )
            if not plotter.run():
                failures.append(ticker)
                print(f"Failed to generate chart for {ticker}")
                if self.strict:
                    break
        return failures

    def run(self) -> bool:
        print("\n" + "=" * 72)
        print("AgentQuant Config Plot Runner")
        print("=" * 72)
        print(f"Config: {self.config_path}")
        print(f"exp_name: {self.exp_name}")
        if self.config_id:
            print(f"config_id: {self.config_id}")
        else:
            print("Error: config not found in database. Run the backtest/simulation first.")
            return False

        portfolio_ok = self._run_portfolio_plot()
        tickers = self._resolve_tickers()
        if not tickers:
            print("\nNo traded tickers found. Strategy net value chart is the only generated chart.")
            return portfolio_ok

        print(f"\nTraded tickers: {', '.join(tickers)}")
        failures = self._run_ticker_plots(tickers)

        print("\n" + "=" * 72)
        if portfolio_ok and not failures:
            print("All config charts generated successfully.")
            success = True
        else:
            if not portfolio_ok:
                print("Strategy net value chart failed.")
            if failures:
                print(f"Ticker chart failures: {', '.join(failures)}")
            success = False
        print("=" * 72 + "\n")
        return success


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate strategy and traded-ticker charts for one AgentQuant config"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="src/config/dev.yaml",
        help="Path to configuration YAML file (default: src/config/dev.yaml)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Directory for generated plot images (default: AgentQuant/image)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="SQLite database path (default: DB_PATH from database.sqlite_setup)",
    )
    parser.add_argument(
        "--tickers",
        type=str,
        default=None,
        help="Optional comma/space separated ticker list. Default: all traded tickers.",
    )
    parser.add_argument(
        "--no-price",
        action="store_true",
        help="Skip PandaAI price loading for ticker charts and draw net-value curves only.",
    )
    parser.add_argument(
        "--skip-portfolio",
        action="store_true",
        help="Skip the strategy net value curve.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with failure when any ticker chart fails.",
    )

    args = parser.parse_args()

    runner = ConfigPlotRunner(
        args.config,
        output_dir=args.output_dir,
        db_path=args.db_path,
        tickers=split_tickers(args.tickers),
        include_price=not args.no_price,
        skip_portfolio=args.skip_portfolio,
        strict=args.strict,
    )
    success = runner.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
