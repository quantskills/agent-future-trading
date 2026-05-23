"""
Single Futures Net Value Curve Plotter

This script generates a contribution net value curve plot for a SINGLE
futures ticker within the portfolio. It tracks the ticker's cumulative
net P&L contribution across the full settlement calendar, and overlays
the futures price curve plus open-position markers when price/trade data
is available.

Usage:
    python src/evaluation/plot_future.py --config src/config/dev.yaml --ticker M

Output:
    Generates a PNG file at: AgentQuant/image/{exp_name}_{ticker}_curve.png
"""

import argparse
import math
import re
import sys
import sqlite3
import yaml
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
from matplotlib import font_manager
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.sqlite_setup import DB_PATH
from apis.router import APISource, Router


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "").strip("._")
    return safe or "config"


def _resolve_config_path(config_path: str) -> str:
    path = Path(config_path)
    if path.is_absolute() or path.exists():
        return str(path)

    src_root = Path(__file__).resolve().parents[1]
    for candidate in (src_root / path, src_root.parent / path):
        if candidate.exists():
            return str(candidate)

    return str(path)


def _configure_matplotlib_fonts() -> None:
    preferred_fonts = [
        "Microsoft YaHei",
        "SimHei",
        "SimSun",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "Arial Unicode MS",
    ]
    installed_fonts = {font.name for font in font_manager.fontManager.ttflist}
    selected_fonts = [font for font in preferred_fonts if font in installed_fonts]

    plt.rcParams["font.sans-serif"] = selected_fonts + ["DejaVu Sans", "Arial"]
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["font.family"] = "sans-serif"


class SingleFutureCurvePlotter:
    """Generates contribution net value curve plots for a single futures ticker."""

    def __init__(
        self,
        config_path: str,
        ticker: str,
        output_dir: Optional[str] = None,
        db_path: Optional[str] = None,
        include_price: bool = True,
    ):
        """
        Initialize the plotter.

        Args:
            config_path: Path to the YAML configuration file
            ticker: Futures ticker symbol (e.g., 'M', 'CU', 'AG')
        """
        self.config_path = _resolve_config_path(config_path)
        self.ticker = ticker.upper()
        self.config = None
        self.exp_name = None
        self.config_id = None
        self.db_path = db_path or DB_PATH
        self.include_price = include_price

        # Output paths
        self.project_root = Path(__file__).resolve().parents[2]
        self.image_dir = Path(output_dir).resolve() if output_dir else self.project_root / "image"

        # Ensure image directory exists
        self.image_dir.mkdir(parents=True, exist_ok=True)

        # Data containers
        self.ticker_pnl_data: Optional[pd.DataFrame] = None
        self.price_data: Optional[pd.DataFrame] = None
        self.transaction_data: Optional[pd.DataFrame] = None

    def load_config(self) -> bool:
        """Load configuration from YAML file."""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)

            self.exp_name = self.config.get('exp_name')

            if not self.exp_name:
                print(f"Error: exp_name not found in configuration file: {self.config_path}")
                return False

            print(f"Loaded config: {self.exp_name}")
            print(f"Target ticker: {self.ticker}")
            return True

        except FileNotFoundError:
            print(f"Error: Configuration file not found: {self.config_path}")
            return False
        except yaml.YAMLError as e:
            print(f"Error parsing configuration file: {e}")
            return False

    def get_db_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_config_id(self) -> Optional[str]:
        """Get config_id from database by exp_name."""
        conn = None
        try:
            conn = self.get_db_connection()
            cursor = conn.cursor()

            cursor.execute('SELECT id FROM config WHERE exp_name = ?', (self.exp_name,))
            row = cursor.fetchone()

            if row:
                config_id = row['id']
                self.config_id = config_id
                print(f"Found config_id: {config_id[:8]}...")
                return config_id
            else:
                print(f"Error: No config found with exp_name '{self.exp_name}'")
                return None

        except Exception as e:
            print(f"Error getting config_id: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def load_ticker_pnl_data(self) -> bool:
        """Load ticker daily P&L data from ticker_daily_pnl table."""
        conn = None
        try:
            config_id = self.config_id or self.get_config_id()
            if not config_id:
                return False

            conn = self.get_db_connection()
            cursor = conn.cursor()

            # Check if required settlement/PnL tables exist.
            cursor.execute('''
                SELECT name FROM sqlite_master
                WHERE type='table' AND name IN ('daily_settlement', 'ticker_daily_pnl')
            ''')
            available_tables = {row['name'] for row in cursor.fetchall()}

            if 'daily_settlement' not in available_tables:
                print("No daily_settlement table found in database")
                return False
            if 'ticker_daily_pnl' not in available_tables:
                print("No ticker_daily_pnl table found in database")
                print("  Please run Phase3 settlement first to populate ticker-level P&L.")
                return False

            # Use the full settlement calendar so the ticker curve stays flat on
            # days without an active position instead of compressing inactive days.
            cursor.execute('''
                SELECT
                    ds.trading_date,
                    ds.previous_balance,
                    ds.previous_margin,
                    COALESCE(tdp.daily_pnl, 0) AS daily_pnl,
                    COALESCE(tdp.commission, 0) AS commission,
                    CASE WHEN tdp.id IS NULL THEN 0 ELSE 1 END AS has_ticker_record
                FROM daily_settlement ds
                JOIN portfolio p ON ds.portfolio_id = p.id
                LEFT JOIN ticker_daily_pnl tdp
                    ON tdp.portfolio_id = ds.portfolio_id
                    AND tdp.trading_date = ds.trading_date
                    AND UPPER(tdp.ticker) = ?
                WHERE p.config_id = ?
                ORDER BY ds.trading_date ASC
            ''', (self.ticker, config_id))

            pnl_records = cursor.fetchall()

            if not pnl_records:
                print(f"No settlement data found for config '{self.exp_name}'")
                return False

            first_row = pnl_records[0]
            previous_balance = first_row['previous_balance']
            previous_margin = first_row['previous_margin']
            if previous_balance is not None and previous_margin is not None:
                initial_capital = float(previous_balance) + float(previous_margin)
            else:
                print("Error: Missing initial settlement equity, cannot normalize single-future net value curve")
                return False
            if initial_capital <= 0:
                print(f"Error: Initial settlement equity must be positive, got {initial_capital:,.2f}")
                return False

            # Convert to DataFrame and calculate cumulative P&L
            ticker_pnl_data = []
            cumulative_gross_pnl = 0.0
            cumulative_commission = 0.0
            cumulative_net_pnl = 0.0
            active_days = 0

            for record in pnl_records:
                trading_date = datetime.fromisoformat(record['trading_date'])
                daily_pnl = float(record['daily_pnl'] or 0.0)
                commission = float(record['commission'] or 0.0)
                daily_net_pnl = daily_pnl - commission
                cumulative_gross_pnl += daily_pnl
                cumulative_commission += commission
                cumulative_net_pnl += daily_net_pnl
                net_value = 1.0 + cumulative_net_pnl / initial_capital
                has_ticker_record = bool(record['has_ticker_record'])
                active_days += int(has_ticker_record)

                ticker_pnl_data.append({
                    'trading_date': trading_date,
                    'daily_pnl': daily_pnl,
                    'commission': commission,
                    'daily_net_pnl': daily_net_pnl,
                    'cumulative_gross_pnl': cumulative_gross_pnl,
                    'cumulative_commission': cumulative_commission,
                    'cumulative_net_pnl': cumulative_net_pnl,
                    'net_value': net_value,
                    'has_ticker_record': has_ticker_record,
                    'initial_capital': initial_capital,
                })

            self.ticker_pnl_data = pd.DataFrame(ticker_pnl_data)

            print(f"\nLoaded {len(self.ticker_pnl_data)} settlement days for {self.ticker}")
            print(f"  First trading date: {self.ticker_pnl_data['trading_date'].iloc[0].date()}")
            print(f"  Last trading date: {self.ticker_pnl_data['trading_date'].iloc[-1].date()}")
            print(f"  Active ticker P&L days: {active_days}")
            print(f"  Gross P&L: {cumulative_gross_pnl:,.2f}")
            print(f"  Commission: {cumulative_commission:,.2f}")
            print(f"  Net P&L: {cumulative_net_pnl:,.2f}")
            print(f"  Final net value: {net_value:.4f}")

            return True

        except Exception as e:
            print(f"Error loading ticker P&L data: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            if conn:
                conn.close()

    def load_transaction_data(self) -> bool:
        """Load executed transactions for this ticker for open-position markers."""
        conn = None
        try:
            config_id = self.config_id or self.get_config_id()
            if not config_id:
                return False

            conn = self.get_db_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='futures_transactions'
            ''')
            if cursor.fetchone() is None:
                print("No futures_transactions table found; price markers will be skipped.")
                self.transaction_data = pd.DataFrame()
                return True

            cursor.execute('''
                SELECT
                    ft.trading_date,
                    ft.ticker,
                    ft.contract_code,
                    ft.action,
                    ft.lots,
                    ft.execution_price,
                    ft.price,
                    ft.execution_price_basis,
                    ft.created_at
                FROM futures_transactions ft
                LEFT JOIN portfolio p ON ft.portfolio_id = p.id
                WHERE COALESCE(ft.config_id, p.config_id) = ?
                  AND UPPER(ft.ticker) = ?
                ORDER BY ft.trading_date ASC, ft.created_at ASC
            ''', (config_id, self.ticker))

            rows = cursor.fetchall()
            if not rows:
                print(f"No executed transactions found for ticker '{self.ticker}'.")
                self.transaction_data = pd.DataFrame()
                return True

            records: List[Dict] = []
            for row in rows:
                execution_price = row['execution_price']
                if execution_price is None:
                    execution_price = row['price']
                records.append({
                    'trading_date': datetime.fromisoformat(row['trading_date']),
                    'ticker': row['ticker'],
                    'contract_code': row['contract_code'],
                    'action': row['action'],
                    'lots': int(row['lots'] or 0),
                    'execution_price': float(execution_price or 0.0),
                    'execution_price_basis': row['execution_price_basis'],
                    'created_at': row['created_at'],
                })

            self.transaction_data = pd.DataFrame(records)
            open_count = int(self.transaction_data['action'].isin(['open_long', 'open_short']).sum())
            print(f"Loaded {len(self.transaction_data)} transactions for {self.ticker}; open markers: {open_count}")
            return True

        except Exception as e:
            print(f"Error loading transaction data: {e}")
            self.transaction_data = pd.DataFrame()
            return True
        finally:
            if conn:
                conn.close()

    @staticmethod
    def _positive_float(value) -> Optional[float]:
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric_value) or numeric_value <= 0:
            return None
        return numeric_value

    def _build_price_frame(self, records: List[Dict]) -> Optional[pd.DataFrame]:
        clean_records: List[Dict] = []
        for record in records:
            price = self._positive_float(record.get('price'))
            if price is None:
                continue
            trading_date = pd.to_datetime(record.get('trading_date'), errors='coerce')
            if pd.isna(trading_date):
                continue

            clean_records.append({
                'trading_date': trading_date,
                'price': price,
                'close': self._positive_float(record.get('close')),
                'settle_price': self._positive_float(record.get('settle_price')),
                'source': record.get('source') or '价格',
                '_source_rank': int(record.get('_source_rank', 0) or 0),
            })

        if not clean_records:
            return None

        df = pd.DataFrame(clean_records)
        df = df.sort_values(['trading_date', '_source_rank'])
        df = df.drop_duplicates('trading_date', keep='first')
        df = df.drop(columns=['_source_rank'])
        return df.reset_index(drop=True)

    def _load_price_data_from_router(self) -> Optional[pd.DataFrame]:
        if self.ticker_pnl_data is None or self.ticker_pnl_data.empty:
            return None

        start_date = self.ticker_pnl_data['trading_date'].min()
        end_date = self.ticker_pnl_data['trading_date'].max()
        query_end_date = pd.to_datetime(end_date) + pd.Timedelta(days=1)
        market_type = (self.config or {}).get('market_type', 'china_futures')

        router = Router(APISource.PANDAAI, market_type=market_type)
        quotes = router.get_china_futures_continuous_candles(
            self.ticker,
            start_date=start_date,
            end_date=query_end_date,
        )
        if not quotes:
            return None

        records: List[Dict] = []
        for quote in quotes:
            settle_price = self._positive_float(quote.settle_price)
            close_price = self._positive_float(quote.close)
            price = settle_price if settle_price is not None else close_price
            if price is None:
                continue
            records.append({
                'trading_date': pd.to_datetime(quote.trade_date),
                'price': price,
                'close': close_price,
                'settle_price': settle_price,
                'source': 'PandaAI主力连续',
            })

        return self._build_price_frame(records)

    def _load_price_data_from_local_fundamentals(self) -> Optional[pd.DataFrame]:
        """Fallback to local active-contract futures close prices when available."""
        if self.ticker_pnl_data is None or self.ticker_pnl_data.empty:
            return None

        data_dir = self.project_root / "data" / "Fundamental_data" / "Finoview_data"
        candidate_stems = [
            f"{self.ticker.lower()}_future_close_price",
            f"{self.ticker.lower()}_futures_close_price",
            f"{self.ticker.lower()}_future_settle_price",
            f"{self.ticker.lower()}_futures_settle_price",
        ]
        start_date = pd.to_datetime(self.ticker_pnl_data['trading_date'].min()).normalize()
        end_date = pd.to_datetime(self.ticker_pnl_data['trading_date'].max()).normalize()

        for stem in candidate_stems:
            file_path = data_dir / f"{stem}.feather"
            if not file_path.exists():
                continue

            df = pd.read_feather(file_path)
            if df.empty:
                continue

            date_col = next(
                (col for col in ['tradeDate', 'date', 'trading_date', 'trade_date', 'datetime'] if col in df.columns),
                None,
            )
            value_col = stem if stem in df.columns else None
            if value_col is None:
                value_col = next(
                    (col for col in ['value', 'close', 'price', 'settle_price'] if col in df.columns),
                    None,
                )
            if date_col is None or value_col is None:
                continue

            local_df = df[[date_col, value_col]].copy()
            local_df[date_col] = pd.to_datetime(local_df[date_col], errors='coerce')
            local_df[value_col] = pd.to_numeric(local_df[value_col], errors='coerce')
            local_df = local_df[
                (local_df[date_col] >= start_date)
                & (local_df[date_col] <= end_date)
            ]

            records = [
                {
                    'trading_date': row[date_col],
                    'price': row[value_col],
                    'close': row[value_col],
                    'settle_price': None,
                    'source': 'Finoview活跃合约收盘价',
                }
                for _, row in local_df.iterrows()
            ]
            price_frame = self._build_price_frame(records)
            if price_frame is not None and not price_frame.empty:
                return price_frame

        return None

    def _load_price_data_from_ticker_pnl(self) -> Optional[pd.DataFrame]:
        """Fallback price points from settlement and transaction records."""
        conn = None
        try:
            config_id = self.config_id or self.get_config_id()
            if not config_id:
                return None

            conn = self.get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT tdp.trading_date, tdp.settle_price
                FROM ticker_daily_pnl tdp
                JOIN portfolio p ON tdp.portfolio_id = p.id
                WHERE p.config_id = ?
                  AND UPPER(tdp.ticker) = ?
                  AND tdp.settle_price IS NOT NULL
                ORDER BY tdp.trading_date ASC
            ''', (config_id, self.ticker))
            records: List[Dict] = []
            for row in cursor.fetchall():
                records.append({
                    'trading_date': datetime.fromisoformat(row['trading_date']),
                    'price': row['settle_price'],
                    'close': None,
                    'settle_price': row['settle_price'],
                    'source': '结算价/成交价快照',
                    '_source_rank': 0,
                })

            cursor.execute('''
                SELECT
                    ft.trading_date,
                    ft.settle_price,
                    ft.execution_price,
                    ft.price,
                    ft.created_at
                FROM futures_transactions ft
                LEFT JOIN portfolio p ON ft.portfolio_id = p.id
                WHERE COALESCE(ft.config_id, p.config_id) = ?
                  AND UPPER(ft.ticker) = ?
                  AND ft.action IN ('open_long', 'open_short', 'close_long', 'close_short')
                ORDER BY ft.trading_date ASC, ft.created_at ASC
            ''', (config_id, self.ticker))
            for row in cursor.fetchall():
                settle_price = self._positive_float(row['settle_price'])
                execution_price = self._positive_float(row['execution_price'])
                raw_price = self._positive_float(row['price'])
                price = settle_price if settle_price is not None else execution_price or raw_price
                records.append({
                    'trading_date': datetime.fromisoformat(row['trading_date']),
                    'price': price,
                    'close': None,
                    'settle_price': settle_price,
                    'source': '结算价/成交价快照',
                    '_source_rank': 1,
                })

            return self._build_price_frame(records)
        except Exception as e:
            print(f"Error loading fallback price data: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def load_price_data(self) -> bool:
        """Load price curve data. Price data is best-effort and not required."""
        if not self.include_price:
            self.price_data = pd.DataFrame()
            return True

        try:
            price_data = self._load_price_data_from_router()
            if price_data is not None and not price_data.empty:
                self.price_data = price_data
                print(
                    f"Loaded {len(self.price_data)} price points for {self.ticker} "
                    f"from {self.price_data['source'].iloc[0]}"
                )
                return True
        except Exception as e:
            print(f"Warning: Could not load full price curve from PandaAI: {e}")

        try:
            local_price = self._load_price_data_from_local_fundamentals()
            if local_price is not None and not local_price.empty:
                self.price_data = local_price
                print(
                    f"Loaded {len(self.price_data)} local futures price points for {self.ticker} "
                    f"from {self.price_data['source'].iloc[0]}"
                )
                return True
        except Exception as e:
            print(f"Warning: Could not load local futures price curve: {e}")

        fallback = self._load_price_data_from_ticker_pnl()
        if fallback is not None and not fallback.empty:
            self.price_data = fallback
            print(
                f"Loaded {len(self.price_data)} fallback settlement/transaction price points for {self.ticker}; "
                "full inactive-day price curve is unavailable."
            )
        else:
            self.price_data = pd.DataFrame()
            print(f"No price data available for {self.ticker}; only net value curve will be plotted.")
        return True

    def create_plot(self) -> bool:
        """Create the net value curve plot for a single ticker."""
        if self.ticker_pnl_data is None or self.ticker_pnl_data.empty:
            print("Error: No ticker P&L data available for plotting")
            return False

        # Configure Chinese fonts before drawing Chinese labels.
        _configure_matplotlib_fonts()

        has_price_data = self.price_data is not None and not self.price_data.empty
        has_transactions = self.transaction_data is not None and not self.transaction_data.empty
        has_price_panel = has_price_data or has_transactions

        # Create figure. Keep net value and price on separate y-axes in one
        # image so their very different scales remain readable.
        if has_price_panel:
            fig, (ax, ax_price) = plt.subplots(
                2,
                1,
                figsize=(14, 10),
                sharex=True,
                gridspec_kw={'height_ratios': [2.0, 1.25], 'hspace': 0.08},
            )
        else:
            fig, ax = plt.subplots(figsize=(14, 8))
            ax_price = None

        # Extract data
        dates = self.ticker_pnl_data['trading_date']
        net_value = self.ticker_pnl_data['net_value']
        daily_net_pnl = self.ticker_pnl_data['daily_net_pnl']

        # Calculate performance metrics
        final_net_value = net_value.iloc[-1]
        total_return = (final_net_value - 1) * 100

        # Find max drawdown
        cumulative_max = net_value.cummax()
        drawdown = (net_value - cumulative_max) / cumulative_max
        max_drawdown = drawdown.min() * 100

        # Calculate net win rate, excluding zero net-P&L days.
        winning_days = (daily_net_pnl > 0).sum()
        losing_days = (daily_net_pnl < 0).sum()
        pnl_days = winning_days + losing_days
        win_rate = (winning_days / pnl_days * 100) if pnl_days > 0 else 0

        # Plot net value curve
        ax.plot(dates, net_value.values,
                label='净值曲线', color='#2E86DE', linewidth=2.5)

        # Add reference line at 1.0
        ax.axhline(y=1.0, color='#95A5A6', linestyle='--', alpha=0.6, linewidth=1.5, label='初始净值')

        # Fill area under curve
        ax.fill_between(dates, 1.0, net_value.values,
                       where=(net_value.values >= 1.0),
                       alpha=0.15, color='#27AE60', label='盈利区域')
        ax.fill_between(dates, 1.0, net_value.values,
                       where=(net_value.values < 1.0),
                       alpha=0.15, color='#E74C3C', label='亏损区域')

        # Set labels and title
        ax.set_xlabel('交易日期', fontsize=12, fontweight='bold')
        ax.set_ylabel('单品种贡献净值', fontsize=12, fontweight='bold')

        title = f"{self.ticker} 期货净值贡献与价格开仓点"
        ax.set_title(title, fontsize=14, fontweight='bold', pad=20)

        # Add legend
        ax.legend(loc='best', fontsize=10, framealpha=0.9)

        # Add grid
        ax.grid(True, alpha=0.3, linestyle='-', linewidth=0.5)

        if ax_price is not None:
            ax.tick_params(labelbottom=False)

        # Add performance metrics text box - show actual P&L amount instead of percentage.
        gross_pnl = self.ticker_pnl_data['daily_pnl'].sum()
        commission = self.ticker_pnl_data['commission'].sum()
        final_pnl = self.ticker_pnl_data['daily_net_pnl'].sum()
        active_days = int(self.ticker_pnl_data['has_ticker_record'].sum())
        metrics_text = (
            f"总净盈亏: {final_pnl:+,.0f} 元\n"
            f"总毛盈亏: {gross_pnl:+,.0f} 元\n"
            f"手续费: {commission:,.0f} 元\n"
            f"贡献收益率: {total_return:.3f}%\n"
            f"最大回撤: {max_drawdown:.2f}%\n"
            f"净胜率: {win_rate:.1f}% ({winning_days}/{pnl_days})\n"
            f"持仓/结算天数: {active_days}/{len(dates)}"
        )
        props = dict(boxstyle='round', facecolor='wheat', alpha=0.8)
        ax.text(0.02, 0.98, metrics_text, transform=ax.transAxes,
                fontsize=11, verticalalignment='top', bbox=props)

        # Set y-axis range. Single-ticker contribution is usually tiny relative
        # to account equity, so avoid a fixed 0.95-1.05 range that hides movement.
        lower = min(1.0, float(net_value.min()))
        upper = max(1.0, float(net_value.max()))
        span = upper - lower
        if span <= 0:
            span = 0.001
        padding = max(span * 0.2, 0.0005)
        y_min = lower - padding
        y_max = upper + padding
        ax.set_ylim(y_min, y_max)
        ax.yaxis.set_major_formatter(mticker.FormatStrFormatter('%.5f'))
        ax.yaxis.get_offset_text().set_visible(False)

        if ax_price is not None:
            if has_price_data:
                price_dates = self.price_data['trading_date']
                price_values = self.price_data['price']
                source = self.price_data['source'].iloc[0] if 'source' in self.price_data else '价格'
                ax_price.plot(
                    price_dates,
                    price_values,
                    label=f'价格曲线 ({source})',
                    color='#34495E',
                    linewidth=1.8,
                )

            if has_transactions:
                tx = self.transaction_data.copy()
                tx = tx[tx['execution_price'] > 0]
                open_tx = tx[tx['action'].isin(['open_long', 'open_short'])]

                marker_styles = {
                    'open_long': ('开多点', '^', '#C0392B'),
                    'open_short': ('开空点', 'v', '#16A085'),
                }
                for action, (label, marker, color) in marker_styles.items():
                    side_tx = open_tx[open_tx['action'] == action]
                    if side_tx.empty:
                        continue
                    ax_price.scatter(
                        side_tx['trading_date'],
                        side_tx['execution_price'],
                        marker=marker,
                        s=90,
                        color=color,
                        edgecolors='white',
                        linewidths=0.8,
                        label=label,
                        zorder=5,
                    )
                    for _, row in side_tx.iterrows():
                        ax_price.annotate(
                            f"{int(row['lots'])}手",
                            (row['trading_date'], row['execution_price']),
                            xytext=(0, 8 if action == 'open_long' else -14),
                            textcoords='offset points',
                            ha='center',
                            fontsize=8,
                            color=color,
                        )

            ax_price.set_ylabel('价格', fontsize=11, fontweight='bold')
            ax_price.set_xlabel('交易日期', fontsize=12, fontweight='bold')
            ax_price.grid(True, alpha=0.3, linestyle='-', linewidth=0.5)
            ax_price.legend(loc='best', fontsize=9, framealpha=0.9)

        date_axis = ax_price if ax_price is not None else ax
        date_axis.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        date_axis.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, len(dates) // 10)))
        plt.setp(date_axis.xaxis.get_majorticklabels(), rotation=45, ha='right')

        # Adjust layout to prevent label cutoff.
        if ax_price is not None:
            fig.subplots_adjust(left=0.08, right=0.98, top=0.92, bottom=0.12, hspace=0.08)
        else:
            plt.tight_layout()

        return True

    def save_plot(self) -> bool:
        """Save the plot to file."""
        try:
            exp_slug = _safe_filename(self.exp_name or "config")
            output_path = self.image_dir / f"{exp_slug}_{self.ticker}_curve.png"
            plt.savefig(output_path, dpi=150, bbox_inches='tight')
            print(f"Plot saved to: {output_path}")
            return True
        except Exception as e:
            print(f"Error saving plot: {e}")
            return False

    def run(self) -> bool:
        """Execute the complete plotting workflow."""
        print("\n" + "="*60)
        print("Single Futures Net Value Curve Plotter")
        print("="*60 + "\n")

        # Load configuration
        if not self.load_config():
            return False

        # Load ticker P&L data
        if not self.load_ticker_pnl_data():
            return False

        # Load transactions and price data for the price/open-marker panel.
        if not self.load_transaction_data():
            return False
        if not self.load_price_data():
            return False

        # Create plot
        if not self.create_plot():
            return False

        # Save plot
        if not self.save_plot():
            return False

        print("\n" + "="*60)
        print("Plot generation completed successfully!")
        print("="*60 + "\n")

        return True


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Generate net value curve plot for a SINGLE futures contract in the portfolio'
    )
    parser.add_argument(
        '--config',
        type=str,
        default='src/config/dev.yaml',
        help='Path to configuration YAML file (default: src/config/dev.yaml)'
    )
    parser.add_argument(
        '--ticker',
        type=str,
        required=True,
        help='Futures ticker symbol (e.g., M, CU, AG, IF, TA)'
    )
    parser.add_argument(
        '--output-dir',
        type=str,
        default=None,
        help='Directory for generated plot images (default: AgentQuant/image)'
    )
    parser.add_argument(
        '--db-path',
        type=str,
        default=None,
        help='SQLite database path (default: DB_PATH from database.sqlite_setup)'
    )
    parser.add_argument(
        '--no-price',
        action='store_true',
        help='Skip PandaAI price loading and only generate the net-value curve'
    )

    args = parser.parse_args()

    plotter = SingleFutureCurvePlotter(
        args.config,
        args.ticker,
        output_dir=args.output_dir,
        db_path=args.db_path,
        include_price=not args.no_price,
    )
    success = plotter.run()

    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
