import sqlite3
import sys
from pathlib import Path

import yaml

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evaluation.plot_future import SingleFutureCurvePlotter


def _write_config(tmp_path, exp_name="plot-price-test"):
    config_path = tmp_path / "dev.yaml"
    config_path.write_text(
        yaml.safe_dump({"exp_name": exp_name, "market_type": "china_futures"}),
        encoding="utf-8",
    )
    return config_path


def _create_plot_db(tmp_path, exp_name="plot-price-test"):
    db_path = tmp_path / "agentquant.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE config (
                id TEXT PRIMARY KEY,
                exp_name TEXT NOT NULL
            );
            CREATE TABLE portfolio (
                id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL
            );
            CREATE TABLE ticker_daily_pnl (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                settle_price REAL
            );
            CREATE TABLE futures_transactions (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT,
                config_id TEXT,
                trading_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                settle_price REAL,
                execution_price REAL,
                price REAL,
                created_at TEXT
            );
            """
        )
        conn.execute("INSERT INTO config (id, exp_name) VALUES (?, ?)", ("cfg", exp_name))
        conn.execute("INSERT INTO portfolio (id, config_id) VALUES (?, ?)", ("pf", "cfg"))
        conn.executemany(
            """
            INSERT INTO ticker_daily_pnl
                (id, portfolio_id, trading_date, ticker, settle_price)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("pnl-1", "pf", "2025-01-09", "BU", 3623.0),
                ("pnl-2", "pf", "2025-01-21", "BU", 0.0),
            ],
        )
        conn.executemany(
            """
            INSERT INTO futures_transactions
                (id, portfolio_id, config_id, trading_date, ticker, action,
                 settle_price, execution_price, price, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("tx-1", "pf", "cfg", "2025-01-09", "BU", "open_long", 3623.0, 3627.0, 3627.0, "2025-01-09T09:00:00"),
                ("tx-2", "pf", "cfg", "2025-01-21", "BU", "close_long", 3745.0, 3747.0, 3747.0, "2025-01-21T09:00:00"),
            ],
        )
    return db_path


def test_ticker_pnl_fallback_filters_zero_settle_and_uses_transaction_settle(tmp_path):
    config_path = _write_config(tmp_path)
    db_path = _create_plot_db(tmp_path)

    plotter = SingleFutureCurvePlotter(str(config_path), "BU", db_path=str(db_path))
    assert plotter.load_config()
    assert plotter.get_config_id() == "cfg"

    price_data = plotter._load_price_data_from_ticker_pnl()

    assert price_data is not None
    assert [day.strftime("%Y-%m-%d") for day in price_data["trading_date"]] == [
        "2025-01-09",
        "2025-01-21",
    ]
    assert price_data["price"].tolist() == [3623.0, 3745.0]
    assert (price_data["price"] > 0).all()


def test_router_price_frame_drops_non_positive_prices(tmp_path):
    config_path = _write_config(tmp_path)
    plotter = SingleFutureCurvePlotter(str(config_path), "BU")

    price_data = plotter._build_price_frame(
        [
            {"trading_date": "2025-01-09", "price": 3623.0, "source": "test"},
            {"trading_date": "2025-01-10", "price": 0.0, "source": "test"},
            {"trading_date": "2025-01-13", "price": None, "source": "test"},
            {"trading_date": "2025-01-14", "price": 3738.0, "source": "test"},
        ]
    )

    assert price_data is not None
    assert [day.strftime("%Y-%m-%d") for day in price_data["trading_date"]] == [
        "2025-01-09",
        "2025-01-14",
    ]
    assert price_data["price"].tolist() == [3623.0, 3738.0]
