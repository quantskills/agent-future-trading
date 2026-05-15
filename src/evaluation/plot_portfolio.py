"""
Portfolio Net Value Curve Plotter

This script generates a net value curve plot for the ENTIRE portfolio.
It reads settlement data from the daily_settlement table and creates a clean visualization.

Usage:
    python src/evaluation/plot_portfolio.py --config src/config/dev.yaml

Output:
    Generates a PNG file at: AgentQuant/image/{exp_name}_portfolio_curve.png
"""

import argparse
import re
import sys
import sqlite3
import yaml
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.sqlite_setup import DB_PATH


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


class PortfolioCurvePlotter:
    """Generates net value curve plots for the entire portfolio."""

    def __init__(
        self,
        config_path: str,
        output_dir: Optional[str] = None,
        db_path: Optional[str] = None,
    ):
        """
        Initialize the plotter.

        Args:
            config_path: Path to the YAML configuration file
        """
        self.config_path = _resolve_config_path(config_path)
        self.config = None
        self.exp_name = None
        self.db_path = db_path or DB_PATH

        # Output paths
        self.project_root = Path(__file__).resolve().parents[2]
        self.image_dir = Path(output_dir).resolve() if output_dir else self.project_root / "image"

        # Ensure image directory exists
        self.image_dir.mkdir(parents=True, exist_ok=True)

        # Data containers
        self.settlement_data: Optional[pd.DataFrame] = None

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

    def load_settlement_data(self) -> bool:
        """Load settlement data from daily_settlement table."""
        conn = None
        try:
            config_id = self.get_config_id()
            if not config_id:
                return False

            conn = self.get_db_connection()
            cursor = conn.cursor()

            # Check if daily_settlement table exists
            cursor.execute('''
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='daily_settlement'
            ''')
            has_settlement_table = cursor.fetchone() is not None

            if not has_settlement_table:
                print("No daily_settlement table found in database")
                return False

            # Fetch settlement data for this config
            cursor.execute('''
                SELECT
                    ds.trading_date,
                    ds.previous_balance,
                    ds.previous_margin,
                    ds.current_balance,
                    ds.current_margin,
                    ds.daily_pnl,
                    ds.commission
                FROM daily_settlement ds
                JOIN portfolio p ON ds.portfolio_id = p.id
                WHERE p.config_id = ?
                ORDER BY ds.trading_date ASC
            ''', (config_id,))

            settlements = cursor.fetchall()

            if not settlements:
                print("No settlement data found in database")
                return False

            # Account equity uses the same futures settlement basis as evaluation.py:
            # account_equity = current_balance + current_margin. This is
            # equivalent to initial equity plus cumulative P&L minus commission,
            # while also preserving future deposit/withdraw effects.
            cumulative_pnl = 0.0
            cumulative_commission = 0.0

            # Use the first day's previous_balance + previous_margin as initial capital (same as evaluation module)
            initial_capital = (
                (settlements[0]['previous_balance'] or 0) +
                (settlements[0]['previous_margin'] or 0)
            ) if settlements else 0

            if initial_capital <= 0:
                print(f"Error: Initial settlement equity must be positive, got {initial_capital:,.2f}")
                return False

            settlement_equity_data = []
            for s in settlements:
                cumulative_pnl += (s['daily_pnl'] or 0)
                cumulative_commission += (s['commission'] or 0)

                current_balance = float(s['current_balance'] or 0.0)
                current_margin = float(s['current_margin'] or 0.0)
                account_equity = current_balance + current_margin

                settlement_equity_data.append({
                    'trading_date': datetime.fromisoformat(s['trading_date']),
                    'account_equity': account_equity,
                    'current_balance': current_balance,
                    'current_margin': current_margin,
                    'daily_pnl': s['daily_pnl'] or 0,
                    'commission': s['commission'] or 0,
                    'cumulative_pnl': cumulative_pnl,
                    'cumulative_commission': cumulative_commission,
                    'initial_capital': initial_capital  # Store for later use
                })

            self.settlement_data = pd.DataFrame(settlement_equity_data)

            print(f"Loaded {len(self.settlement_data)} settlement records")
            print(f"  First trading date: {self.settlement_data['trading_date'].iloc[0].date()}")
            print(f"  Last trading date: {self.settlement_data['trading_date'].iloc[-1].date()}")
            print(f"  Initial capital: {initial_capital:,.2f}")
            print(f"  Final equity: {settlement_equity_data[-1]['account_equity']:,.2f}")
            print(f"  Total return: {(settlement_equity_data[-1]['account_equity'] / initial_capital - 1):>.2%}")

            return True

        except Exception as e:
            print(f"Error loading settlement data: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            if conn:
                conn.close()

    def create_plot(self) -> bool:
        """Create the portfolio net value curve plot."""
        if self.settlement_data is None or self.settlement_data.empty:
            print("Error: No settlement data available for plotting")
            return False

        # Configure Chinese fonts before drawing Chinese labels.
        _configure_matplotlib_fonts()

        # Create figure
        fig, ax = plt.subplots(figsize=(14, 8))

        # Extract data
        dates = self.settlement_data['trading_date']
        account_equity = self.settlement_data['account_equity']
        daily_pnl = self.settlement_data['daily_pnl']

        # Calculate net value (normalized to initial value = 1.0)
        # IMPORTANT: Use the actual initial capital for normalization (same as evaluation module)
        # NOT the first day's account_equity (which already has commission deducted)
        initial_capital = self.settlement_data['initial_capital'].iloc[0]
        net_value = account_equity / initial_capital

        # Calculate performance metrics
        final_net_value = net_value.iloc[-1]
        total_return = (final_net_value - 1) * 100

        # Find max drawdown
        cumulative_max = net_value.cummax()
        drawdown = (net_value - cumulative_max) / cumulative_max
        max_drawdown = drawdown.min() * 100

        # Calculate settlement win rate, excluding zero P&L days.
        winning_days = (daily_pnl > 0).sum()
        losing_days = (daily_pnl < 0).sum()
        pnl_days = winning_days + losing_days
        win_rate = (winning_days / pnl_days * 100) if pnl_days > 0 else 0

        # Calculate Sharpe ratio using the same settlement-day annualization
        # basis as evaluation.evaluate_config for futures.
        if len(account_equity) > 1:
            import numpy as np
            returns = []
            for i in range(1, len(account_equity)):
                daily_return = (account_equity.iloc[i] - account_equity.iloc[i-1]) / account_equity.iloc[i-1]
                returns.append(daily_return)

            annualization_days = len(dates)
            total_return_decimal = final_net_value - 1
            annualized_return = (
                (1 + total_return_decimal) ** (252.0 / annualization_days) - 1
                if annualization_days > 0 and total_return_decimal > -1
                else 0
            )
            volatility = np.std(returns) * np.sqrt(252) if returns else 0

            risk_free_rate = 0.03
            sharpe_ratio = (annualized_return - risk_free_rate) / volatility if volatility > 0 else 0
        else:
            annualization_days = len(dates)
            annualized_return = 0
            volatility = 0
            sharpe_ratio = 0

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
        ax.set_ylabel('净值 (归一化)', fontsize=12, fontweight='bold')

        title = "策略净值曲线"
        ax.set_title(title, fontsize=16, fontweight='bold', pad=20)

        # Add legend
        ax.legend(loc='best', fontsize=10, framealpha=0.9)

        # Add grid
        ax.grid(True, alpha=0.3, linestyle='-', linewidth=0.5)

        # Format x-axis dates
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, len(dates) // 10)))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right')

        # Add performance metrics text box
        metrics_text = (
            f"总收益: {total_return:+.2f}%\n"
            f"年化收益: {annualized_return * 100:.2f}%\n"
            f"最大回撤: {max_drawdown:.2f}%\n"
            f"夏普比率: {sharpe_ratio:.2f}\n"
            f"波动率: {volatility * 100:.2f}%\n"
            f"日结算胜率: {win_rate:.1f}% ({winning_days}/{pnl_days})\n"
            f"结算交易日: {len(dates)}"
        )
        props = dict(boxstyle='round', facecolor='wheat', alpha=0.8)
        ax.text(0.02, 0.98, metrics_text, transform=ax.transAxes,
                fontsize=11, verticalalignment='top', bbox=props)

        # Set y-axis range. Futures account returns can be small over short
        # windows, so avoid a fixed 0.95-1.05 range that hides movement.
        lower = min(1.0, float(net_value.min()))
        upper = max(1.0, float(net_value.max()))
        span = upper - lower
        if span <= 0:
            span = 0.001
        padding = max(span * 0.2, 0.0005)
        y_min = lower - padding
        y_max = upper + padding
        ax.set_ylim(y_min, y_max)

        # Adjust layout to prevent label cutoff
        plt.tight_layout()

        return True

    def save_plot(self) -> bool:
        """Save the plot to file."""
        try:
            exp_slug = _safe_filename(self.exp_name or "config")
            output_path = self.image_dir / f"{exp_slug}_portfolio_curve.png"
            plt.savefig(output_path, dpi=150, bbox_inches='tight')
            print(f"Plot saved to: {output_path}")
            return True
        except Exception as e:
            print(f"Error saving plot: {e}")
            return False

    def run(self) -> bool:
        """Execute the complete plotting workflow."""
        print("\n" + "="*60)
        print("Portfolio Net Value Curve Plotter")
        print("="*60 + "\n")

        # Load configuration
        if not self.load_config():
            return False

        # Load settlement data
        if not self.load_settlement_data():
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
        description='Generate net value curve plot for the entire portfolio'
    )
    parser.add_argument(
        '--config',
        type=str,
        default='src/config/dev.yaml',
        help='Path to configuration YAML file (default: src/config/dev.yaml)'
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

    args = parser.parse_args()

    plotter = PortfolioCurvePlotter(
        args.config,
        output_dir=args.output_dir,
        db_path=args.db_path,
    )
    success = plotter.run()

    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
