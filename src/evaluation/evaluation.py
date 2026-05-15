import sqlite3
import json
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import numpy as np
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from database.sqlite_setup import DB_PATH
from util.logger import logger


def calculate_portfolio_value(portfolio: Dict) -> float:
    """
    Calculate total portfolio value (cash + positions).

    For stocks: value = cashflow + total_positions_value
    For futures: account_equity = cashflow + margin_used + unrealized_pnl
                 Note: Do NOT use total_assets as it double-counts margin
    """
    cashflow = portfolio.get('cashflow', 0)
    positions = portfolio.get('positions', {})
    total_positions_value = sum(pos.get('value', 0) for pos in positions.values())
    return cashflow + total_positions_value


def _is_futures_position(position: Dict) -> bool:
    """Detect whether a portfolio position snapshot looks like a futures position."""
    if not isinstance(position, dict):
        return False

    futures_keys = {
        'contract_code',
        'settle_price',
        'current_settle_price',
        'margin_used',
        'margin_rate',
        'contract_multiplier',
        'entry_price',
    }
    return any(key in position for key in futures_keys)


def calculate_returns(portfolio_values: List[float]) -> List[float]:
    """Calculate daily returns from portfolio values."""
    if len(portfolio_values) < 2:
        return [0.0]

    returns = []
    for i in range(1, len(portfolio_values)):
        daily_return = (portfolio_values[i] - portfolio_values[i-1]) / portfolio_values[i-1]
        returns.append(daily_return)
    return returns


def calculate_annualized_return(total_return: float, days: int) -> float:
    """
    Calculate annualized return using compound annualization.

    Uses 252 trading days per year for futures/stocks.

    Formula: (1 + total_return) ^ (252 / days) - 1

    Args:
        total_return: Total return over the period (as decimal, e.g., 0.01 for 1%)
        days: Number of days in the period

    Returns:
        Annualized return as decimal
    """
    if days <= 0:
        return 0.0

    if total_return <= -1.0:
        # Total loss (-100% or more), return -100%
        return -1.0

    # Use compound annualization with 252 trading days
    # This is the standard method for financial instruments
    return (1 + total_return) ** (252.0 / days) - 1


def calculate_volatility(returns: List[float], trading_days: int) -> float:
    """Calculate annualized volatility."""
    if len(returns) == 0:
        return 0.0
    return np.std(returns) * np.sqrt(252)  # Annualize with 252 trading days per year


def calculate_sharpe_ratio(annualized_return: float, volatility: float, risk_free_rate: float = 0.03) -> float:
    """Calculate Sharpe ratio."""
    if volatility == 0:
        return 0.0
    return (annualized_return - risk_free_rate) / volatility


def calculate_max_drawdown(portfolio_values: List[float]) -> float:
    """
    Calculate maximum drawdown from peak to trough.

    Drawdown = (peak - current_value) / peak

    For futures: This should be calculated from account_equity curve,
    NOT from cashflow/settlement_balance alone (which excludes margin).

    Args:
        portfolio_values: Time series of portfolio/account equity values

    Returns:
        Maximum drawdown as a decimal (e.g., 0.05 for 5%)
    """
    if len(portfolio_values) == 0:
        return 0.0

    peak = portfolio_values[0]
    max_dd = 0.0

    for value in portfolio_values:
        if value > peak:
            peak = value
        drawdown = (peak - value) / peak if peak > 0 else 0.0
        if drawdown > max_dd:
            max_dd = drawdown

    return max_dd


def calculate_optional_max_drawdown(portfolio_values: Optional[List[float]]) -> Optional[float]:
    """Calculate max drawdown when a time series is available."""
    if not portfolio_values:
        return None
    return calculate_max_drawdown(portfolio_values)


def calculate_trade_metrics(decisions: List[Dict], portfolios: List[Dict]) -> Tuple[int, int, int, float]:
    """
    Calculate trade-related metrics (win rate, winning trades, losing trades, avg return per trade).

    Core logic:
    1. Track position status for each ticker
    2. When decision is SELL and position becomes zero, calculate profit/loss for that trade
    3. Count completed trades (buy-sell pairs)

    IMPORTANT:
    - total_trades: Number of completed buy-sell pairs (round trips)
    - For futures, use calculate_futures_transaction_win_rate() instead
    - This function is for stock trading only
    """
    if len(decisions) == 0 or len(portfolios) < 2:
        return 0, 0, 0, 0.0

    # Create mapping from portfolio_id to portfolio
    portfolio_map = {p['id']: p for p in portfolios}

    # Sort all decisions by time
    sorted_decisions = sorted(decisions, key=lambda x: (x.get('trading_date', ''), x.get('updated_at', '')))

    # Track position status for each ticker
    # Structure: {ticker: {'total_shares': int, 'total_cost': float, 'entry_count': int}}
    ticker_positions = {}

    total_trades = 0
    winning_trades = 0
    losing_trades = 0
    trade_returns = []

    for decision in sorted_decisions:
        action = decision.get('action')
        ticker = decision.get('ticker')
        shares = decision.get('shares', 0)
        price = decision.get('price', 0)

        # Only process BUY and SELL (support multiple formats)
        if action == 'Buy' or action == 'BUY':
            # Buy: increase position
            if ticker not in ticker_positions:
                ticker_positions[ticker] = {
                    'total_shares': 0,
                    'total_cost': 0.0,
                    'entry_count': 0
                }

            # Update position (weighted average cost)
            current_shares = ticker_positions[ticker]['total_shares']
            current_cost = ticker_positions[ticker]['total_cost']
            new_shares = current_shares + shares
            new_cost = current_cost + (shares * price)

            ticker_positions[ticker]['total_shares'] = new_shares
            ticker_positions[ticker]['total_cost'] = new_cost
            ticker_positions[ticker]['entry_count'] += 1

        elif action == 'Sell' or action == 'SELL':
            # Sell: decrease position, calculate profit/loss when position is fully closed
            if ticker in ticker_positions and ticker_positions[ticker]['total_shares'] > 0:
                entry_shares = ticker_positions[ticker]['total_shares']
                entry_total_cost = ticker_positions[ticker]['total_cost']
                entry_avg_price = entry_total_cost / entry_shares if entry_shares > 0 else 0

                # Calculate profit/loss for this sell
                if entry_avg_price > 0 and price > 0:
                    trade_return = (price - entry_avg_price) / entry_avg_price
                    trade_returns.append(trade_return)
                    total_trades += 1

                    if trade_return > 0:
                        winning_trades += 1
                    else:
                        losing_trades += 1

                # Update position (support partial closing)
                remaining_shares = entry_shares - shares
                remaining_cost = entry_total_cost * (remaining_shares / entry_shares) if entry_shares > 0 else 0

                if remaining_shares > 0:
                    # Partial position closed, keep remaining position
                    ticker_positions[ticker]['total_shares'] = remaining_shares
                    ticker_positions[ticker]['total_cost'] = remaining_cost
                else:
                    # Position fully closed, remove position record
                    del ticker_positions[ticker]

    # Calculate average return per trade
    avg_return = np.mean(trade_returns) if trade_returns else 0.0

    return total_trades, winning_trades, losing_trades, avg_return


def calculate_win_rate(winning_trades: int, total_trades: int) -> float:
    """Calculate win rate."""
    if total_trades == 0:
        return 0.0
    return winning_trades / total_trades


def calculate_futures_trade_win_rate(config_id: str, db_path: str) -> Dict:
    """
    Calculate futures win rate using daily settlement P&L.

    Methodology:
    - Use daily_settlement.daily_pnl to calculate win rate
    - Each trading day is considered one evaluated day with its P&L
    - This automatically includes all position changes (open, close, hold)

    This approach is:
    - More reliable than FIFO matching (doesn't depend on transaction completeness)
    - Automatically includes forced closure (daily settlement reflects current position P&L)
    - Simpler and more accurate
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Fetch all daily settlement records for this config
        cursor.execute('''
            SELECT ds.daily_pnl, ds.trading_date
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE p.config_id = ?
            AND ds.daily_pnl IS NOT NULL
            ORDER BY ds.trading_date ASC
        ''', (config_id,))

        settlements = cursor.fetchall()

        if not settlements:
            logger.warning("No daily settlement data available for futures win-rate calculation")
            return {
                'winning_days': 0,
                'losing_days': 0,
                'flat_days': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': 0.0,
                'avg_return_per_trade': 0.0,
                'avg_return_per_day': 0.0,
                'total_trades': 0,
                'evaluated_days': 0
            }

        logger.info("Calculating futures trade win rate from daily settlements")
        logger.info(
            f"Settlement period: {settlements[0]['trading_date']} to {settlements[-1]['trading_date']}"
        )
        logger.info(f"Settlement days: {len(settlements)}")
        # For futures: use previous_balance + previous_margin (account equity before first trading day)
        cursor.execute('''
            SELECT ds.previous_balance, ds.previous_margin
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE p.config_id = ?
            ORDER BY ds.trading_date ASC
            LIMIT 1
        ''', (config_id,))

        initial_row = cursor.fetchone()
        initial_capital = None
        if initial_row and initial_row['previous_balance'] is not None and initial_row['previous_margin'] is not None:
            initial_capital = initial_row['previous_balance'] + initial_row['previous_margin']
        else:
            logger.warning(
                "Missing initial settlement equity for futures win-rate calculation; "
                "avg_return_per_trade will be reported as 0.0"
            )

        # Calculate win rate based on daily P&L
        winning_days = 0
        losing_days = 0
        daily_returns = []

        total_pnl = 0
        positive_pnl_days = 0
        negative_pnl_days = 0
        zero_pnl_days = 0

        for settlement in settlements:
            daily_pnl = settlement['daily_pnl']
            total_pnl += daily_pnl

            if daily_pnl > 0:
                winning_days += 1
                positive_pnl_days += 1
            elif daily_pnl < 0:
                losing_days += 1
                negative_pnl_days += 1
            else:
                zero_pnl_days += 1

            # Calculate daily return
            if initial_capital and initial_capital > 0:
                daily_returns.append(daily_pnl / initial_capital)

        logger.info("Daily settlement statistics:")
        logger.info(f"  Winning days: {winning_days}")
        logger.info(f"  Losing days: {losing_days}")
        logger.info(f"  Flat days: {zero_pnl_days}")
        logger.info(f"  Total PnL: {total_pnl:+,.2f}")

        # Calculate final metrics
        total_trades = winning_days + losing_days
        win_rate = winning_days / total_trades if total_trades > 0 else 0.0
        avg_return = np.mean(daily_returns) if daily_returns else 0.0

        logger.info(
            f"Trade-day summary: winning={winning_days}, losing={losing_days}, total={total_trades}"
        )
        logger.info(f"Daily win rate: {win_rate:.2%} ({winning_days}/{total_trades})")
        return {
            'winning_days': winning_days,
            'losing_days': losing_days,
            'flat_days': zero_pnl_days,
            'winning_trades': winning_days,
            'losing_trades': losing_days,
            'win_rate': win_rate,
            'avg_return_per_trade': avg_return,
            'avg_return_per_day': avg_return,
            'total_trades': total_trades,
            'evaluated_days': len(settlements)
        }

    except Exception as e:
        logger.error(f"Error calculating futures trade win rate: {e}")
        import traceback
        traceback.print_exc()
        return {
            'winning_days': 0,
            'losing_days': 0,
            'flat_days': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'win_rate': 0.0,
            'avg_return_per_trade': 0.0,
            'avg_return_per_day': 0.0,
            'total_trades': 0,
            'evaluated_days': 0
        }
    finally:
        if conn:
            conn.close()


def calculate_futures_transaction_win_rate(config_id: str, db_path: str) -> Dict:
    """
    Calculate futures win rate from completed transaction pairs.

    The matching logic uses FIFO lots by ticker, contract, and direction:
    - open_long is matched by close_long
    - open_short is matched by close_short

    A matched open/close lot segment is counted as one completed trade. PnL is
    calculated net of the matched open commission and close commission.
    """
    conn = None
    empty_result = {
        'winning_trades': 0,
        'losing_trades': 0,
        'flat_trades': 0,
        'win_rate': 0.0,
        'avg_return_per_trade': 0.0,
        'total_trades': 0,
        'realized_trade_pnl': 0.0,
        'unmatched_close_lots': 0,
        'rollover_transaction_count': 0,
    }

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("PRAGMA table_info(futures_transactions)")
        transaction_columns = {row[1] for row in cursor.fetchall()}
        source_type_expr = "source_type" if "source_type" in transaction_columns else "'strategy' AS source_type"

        cursor.execute(
            f'''
            SELECT
                trading_date,
                created_at,
                ticker,
                contract_code,
                action,
                lots,
                execution_price,
                price,
                contract_multiplier,
                commission,
                {source_type_expr}
            FROM futures_transactions
            WHERE config_id = ?
              AND action IN ('open_long', 'open_short', 'close_long', 'close_short')
            ORDER BY trading_date ASC, created_at ASC
            ''',
            (config_id,),
        )
        transactions = cursor.fetchall()

        if not transactions:
            logger.warning("No futures transaction data available for transaction win-rate calculation")
            return empty_result.copy()

        open_positions = {}
        trade_returns = []
        winning_trades = 0
        losing_trades = 0
        flat_trades = 0
        realized_trade_pnl = 0.0
        unmatched_close_lots = 0
        rollover_transaction_count = 0

        for row in transactions:
            action = row['action']
            lots = int(row['lots'] or 0)
            execution_price = float(row['execution_price'] or row['price'] or 0.0)
            multiplier = float(row['contract_multiplier'] or 1.0)
            commission = float(row['commission'] or 0.0)
            contract_code = row['contract_code'] or row['ticker']
            if str(row['source_type'] or '').lower() == 'rollover':
                rollover_transaction_count += 1

            if lots <= 0 or execution_price <= 0:
                logger.warning(
                    "Skipping invalid futures transaction for win-rate calculation: "
                    f"ticker={row['ticker']}, action={action}, lots={lots}, price={execution_price}"
                )
                continue

            if action in ('open_long', 'open_short'):
                side = 'long' if action == 'open_long' else 'short'
                key = (row['ticker'], contract_code, side)
                open_positions.setdefault(key, []).append({
                    'remaining_lots': lots,
                    'entry_price': execution_price,
                    'multiplier': multiplier,
                    'remaining_commission': commission,
                })
                continue

            side = 'long' if action == 'close_long' else 'short'
            key = (row['ticker'], contract_code, side)
            queue = open_positions.get(key, [])
            remaining_close_lots = lots

            while remaining_close_lots > 0 and queue:
                open_lot = queue[0]
                matched_lots = min(remaining_close_lots, open_lot['remaining_lots'])
                open_lots_before = open_lot['remaining_lots']

                open_commission = (
                    open_lot['remaining_commission'] * matched_lots / open_lots_before
                    if open_lots_before > 0
                    else 0.0
                )
                close_commission = commission * matched_lots / lots
                trade_multiplier = float(open_lot['multiplier'] or multiplier or 1.0)

                if side == 'long':
                    gross_pnl = (execution_price - open_lot['entry_price']) * matched_lots * trade_multiplier
                else:
                    gross_pnl = (open_lot['entry_price'] - execution_price) * matched_lots * trade_multiplier

                net_pnl = gross_pnl - open_commission - close_commission
                realized_trade_pnl += net_pnl
                notional = open_lot['entry_price'] * matched_lots * trade_multiplier
                if notional > 0:
                    trade_returns.append(net_pnl / notional)

                if net_pnl > 0:
                    winning_trades += 1
                elif net_pnl < 0:
                    losing_trades += 1
                else:
                    flat_trades += 1

                open_lot['remaining_lots'] -= matched_lots
                open_lot['remaining_commission'] -= open_commission
                remaining_close_lots -= matched_lots

                if open_lot['remaining_lots'] <= 0:
                    queue.pop(0)

            if remaining_close_lots > 0:
                unmatched_close_lots += remaining_close_lots
                logger.warning(
                    "Unmatched futures close lots in transaction win-rate calculation: "
                    f"ticker={row['ticker']}, contract={contract_code}, side={side}, lots={remaining_close_lots}"
                )

        total_trades = winning_trades + losing_trades + flat_trades
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
        avg_return = float(np.mean(trade_returns)) if trade_returns else 0.0

        logger.info("Futures transaction win-rate statistics:")
        logger.info(f"  Completed trades: {total_trades}")
        logger.info(f"  Winning trades: {winning_trades}")
        logger.info(f"  Losing trades: {losing_trades}")
        logger.info(f"  Flat trades: {flat_trades}")
        logger.info(f"  Transaction win rate: {win_rate:.2%}")
        logger.info(f"  Realized transaction PnL: {realized_trade_pnl:+,.2f}")
        logger.info(f"  Rollover transactions included in account path: {rollover_transaction_count}")

        return {
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'flat_trades': flat_trades,
            'win_rate': win_rate,
            'avg_return_per_trade': avg_return,
            'total_trades': total_trades,
            'realized_trade_pnl': realized_trade_pnl,
            'unmatched_close_lots': unmatched_close_lots,
            'rollover_transaction_count': rollover_transaction_count,
        }

    except Exception as e:
        logger.error(f"Error calculating futures transaction win rate: {e}")
        import traceback
        traceback.print_exc()
        return empty_result.copy()
    finally:
        if conn:
            conn.close()


def calculate_futures_metrics(config_id: str, db_path: str) -> Dict:
    """
    Calculate futures-specific metrics from daily_settlement and portfolio tables.

    Args:
        config_id: The config ID to evaluate
        db_path: Path to the SQLite database

    Returns:
        Dictionary containing futures-specific metrics
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Fetch all settlement records for this config
        cursor.execute('''
            SELECT
                ds.daily_pnl,
                ds.margin_ratio,
                ds.is_warning,
                ds.is_liquidation,
                ds.commission,
                ds.previous_margin,
                ds.current_margin,
                ds.previous_balance,
                ds.current_balance
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE p.config_id = ?
            ORDER BY ds.trading_date ASC
        ''', (config_id,))

        settlements = cursor.fetchall()

        if len(settlements) == 0:
            logger.warning(f"No daily_settlement data found for config_id: {config_id}")
            return {
                'peak_margin_ratio': 0,
                'avg_margin_ratio': 0,
                'warning_days': 0,
                'liquidation_events': 0,
                'total_commission': 0,
                'avg_daily_pnl': 0,
                'total_settlement_pnl': 0,
                'max_margin_usage': 0,
                'avg_leverage': 0
            }

        # Calculate metrics from settlement data
        margin_ratios = [s['margin_ratio'] for s in settlements if s['margin_ratio'] is not None]
        peak_margin_ratio = max(margin_ratios) if margin_ratios else 0
        avg_margin_ratio = np.mean(margin_ratios) if margin_ratios else 0

        warning_days = sum(1 for s in settlements if s['is_warning'])
        liquidation_events = sum(1 for s in settlements if s['is_liquidation'])

        total_commission = sum(s['commission'] for s in settlements if s['commission'] is not None)

        daily_pnls = [s['daily_pnl'] for s in settlements if s['daily_pnl'] is not None]
        total_settlement_pnl = sum(daily_pnls)
        avg_daily_pnl = total_settlement_pnl / len(daily_pnls) if daily_pnls else 0

        # Debug: print balance changes with margin details
        logger.info(f"Daily settlement analysis ({len(settlements)} days):")
        logger.info(f"  {'Day':<4} {'PnL':>10} {'Comm':>8} {'PrevBal':>13} {'CurrBal':>13} {'PrevMgn':>10} {'CurrMgn':>10} {'Chg':>10} {'Exp':>10} {'MgnChg':>10}")
        for i, s in enumerate(settlements):
            daily_change = s['current_balance'] - s['previous_balance']
            expected_change = (s['daily_pnl'] or 0) - (s['commission'] or 0)
            margin_change = (s['previous_margin'] or 0) - (s['current_margin'] or 0)
            logger.info(f"  {i+1:<4} {s['daily_pnl']:>+10,.2f} {s['commission']:>8,.2f} "
                       f"{s['previous_balance']:>13,.2f} {s['current_balance']:>13,.2f} "
                       f"{s['previous_margin'] or 0:>10,.2f} {s['current_margin'] or 0:>10,.2f} "
                       f"{daily_change:>+10,.2f} {expected_change:>+10,.2f} {margin_change:>+10,.2f}")

        # Calculate total balance change
        if len(settlements) > 0:
            total_balance_change = settlements[-1]['current_balance'] - settlements[0]['previous_balance']
            logger.info(f"Total balance change: {total_balance_change:>+,.2f}")
            logger.info(f"Total settlement_pnl: {total_settlement_pnl:>+,.2f}")
            logger.info(f"Total commission: {total_commission:>+,.2f}")
            logger.info(f"Total margin change: {(settlements[0]['previous_margin'] or 0) - (settlements[-1]['current_margin'] or 0):>+,.2f}")
            logger.info(f"Expected (pnl - comm + mgn_chg): {total_settlement_pnl - total_commission + (settlements[0]['previous_margin'] or 0) - (settlements[-1]['current_margin'] or 0):>+,.2f}")

        # Calculate average leverage from portfolio table (optional, skip if table doesn't have leverage column)
        avg_leverage = 1.0  # Default value
        try:
            # Check if leverage column exists
            cursor.execute("PRAGMA table_info(portfolio)")
            columns = cursor.fetchall()
            column_names = [col[1] for col in columns]

            if 'leverage' in column_names:
                # Fetch leverage data from portfolio table
                cursor.execute('''
                    SELECT p.leverage, p.total_assets, p.cashflow, p.margin_used, p.positions
                    FROM portfolio p
                    WHERE p.config_id = ? AND p.trading_date IS NOT NULL
                    ORDER BY p.trading_date ASC
                ''', (config_id,))

                portfolios = cursor.fetchall()

                if portfolios:
                    # Check if leverage column has meaningful values
                    leverage_values = []
                    for p in portfolios:
                        lev = p['leverage']
                        if lev is not None and lev > 1.0:  # Only include non-default values
                            leverage_values.append(lev)

                    if leverage_values:
                        # Use actual leverage values from database
                        avg_leverage = np.mean(leverage_values)
                        logger.info(f"Average leverage (from DB): {avg_leverage:.2f}x based on {len(leverage_values)} records")
                    else:
                        # Derive leverage from position data if DB values are all default (1.0)
                        logger.info("No meaningful leverage values in DB, deriving from position data...")
                        derived_leverages = []

                        for p in portfolios:
                            try:
                                positions = json.loads(p['positions']) if isinstance(p['positions'], str) else p['positions']
                                if isinstance(positions, dict):
                                    # Calculate total position value across all tickers
                                    total_position_value = sum(
                                        pos.get('value', 0) for pos in positions.values()
                                        if isinstance(pos, dict) and pos.get('shares', 0) != 0
                                    )

                                    account_equity = float(p['cashflow'] or 0.0) + float(p['margin_used'] or 0.0)
                                    if total_position_value > 0 and account_equity > 0:
                                        # Futures exposure leverage uses account equity, not notional-style total_assets.
                                        calc_leverage = total_position_value / account_equity
                                        derived_leverages.append(calc_leverage)
                            except (json.JSONDecodeError, TypeError, ZeroDivisionError) as e:
                                logger.debug(f"Error deriving leverage for portfolio record: {e}")
                                continue

                        if derived_leverages:
                            avg_leverage = np.mean(derived_leverages)
                            logger.info(f"Average leverage (derived): {avg_leverage:.2f}x based on {len(derived_leverages)} position records")
                        else:
                            avg_leverage = 1.0  # No positions, default to no leverage
                            logger.info("No positions found, using default leverage: 1.0x")

            else:
                logger.info("'leverage' column not found in portfolio table, using default value: 1.0x")

        except Exception as e:
            logger.warning(f"Could not calculate leverage: {e}, using default value: 1.0x")
            avg_leverage = 1.0

        logger.info(f"Final average leverage: {avg_leverage:.2f}x")

        return {
            'peak_margin_ratio': peak_margin_ratio,
            'avg_margin_ratio': avg_margin_ratio,
            'warning_days': warning_days,
            'liquidation_events': liquidation_events,
            'total_commission': total_commission,
            'avg_daily_pnl': avg_daily_pnl,
            'total_settlement_pnl': total_settlement_pnl,
            'max_margin_usage': peak_margin_ratio,  # Alias for clarity
            'avg_leverage': avg_leverage
        }

    except Exception as e:
        logger.error(f"Error calculating futures metrics for {config_id}: {e}")
        return {
            'peak_margin_ratio': 0,
            'avg_margin_ratio': 0,
            'warning_days': 0,
            'liquidation_events': 0,
            'total_commission': 0,
            'avg_daily_pnl': 0,
            'total_settlement_pnl': 0,
            'max_margin_usage': 0,
            'avg_leverage': 0
        }
    finally:
        if conn:
            conn.close()


def calculate_futures_trade_metrics(config_id: str, db_path: str) -> Dict:
    """
    Calculate futures trading statistics from futures_transactions table.

    IMPORTANT: Statistical Definition
    - total_futures_trades: Total number of transaction records (opens + closes)
    - long_trades: Total lots of long positions opened
    - short_trades: Total lots of short positions opened
    - active_long_positions: Current net long positions (opens - closes)
    - active_short_positions: Current net short positions (opens - closes)
    - ticker_trade_counts: Number of transaction records per ticker

    NOTE: These metrics count transaction records and opened lots, not completed round trips.
          For win rate on completed trades, use calculate_futures_transaction_win_rate().

    Args:
        config_id: The config ID to evaluate
        db_path: Path to the SQLite database

    Returns:
        Dictionary containing futures trade metrics
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Fetch all futures transactions for this config
        cursor.execute('''
            SELECT
                ft.action,
                ft.lots,
                ft.execution_price AS price,
                ft.settle_price,
                ft.contract_multiplier,
                ft.ticker
            FROM futures_transactions ft
            WHERE ft.config_id = ?
            ORDER BY ft.trading_date ASC, ft.created_at ASC
        ''', (config_id,))

        transactions = cursor.fetchall()

        if len(transactions) == 0:
            logger.warning(f"No futures_transactions data found for config_id: {config_id}")
            return {
                'total_futures_trades': 0,
                'long_trades': 0,
                'short_trades': 0,
                'active_long_positions': 0,
                'active_short_positions': 0,
                'ticker_trade_counts': {}
            }

        # Track positions
        long_opened = 0
        short_opened = 0
        long_closed = 0
        short_closed = 0

        # Track positions by ticker
        ticker_positions = {}  # {ticker: {'long': 0, 'short': 0}}
        ticker_trade_counts = {}  # {ticker: total_trades}

        for tx in transactions:
            action = tx['action']
            lots = tx['lots']
            ticker = tx['ticker']

            if lots == 0:
                continue

            # Initialize ticker tracking
            if ticker not in ticker_positions:
                ticker_positions[ticker] = {'long': 0, 'short': 0}
            if ticker not in ticker_trade_counts:
                ticker_trade_counts[ticker] = 0

            ticker_trade_counts[ticker] += 1

            if action == 'open_long':
                long_opened += lots
                ticker_positions[ticker]['long'] += lots
            elif action == 'open_short':
                short_opened += lots
                ticker_positions[ticker]['short'] += lots
            elif action == 'close_long':
                long_closed += lots
                ticker_positions[ticker]['long'] -= lots
            elif action == 'close_short':
                short_closed += lots
                ticker_positions[ticker]['short'] -= lots

        # Calculate active positions from the last portfolio snapshot
        # This is more reliable than calculating from transaction history
        active_long_positions = 0
        active_short_positions = 0

        # Query the latest portfolio for actual positions
        cursor.execute('''
            SELECT positions
            FROM portfolio
            WHERE config_id = ?
            ORDER BY trading_date DESC, updated_at DESC
            LIMIT 1
        ''', (config_id,))
        latest_portfolio_row = cursor.fetchone()

        if latest_portfolio_row and latest_portfolio_row['positions']:
            try:
                positions = json.loads(latest_portfolio_row['positions'])
                for ticker, pos in positions.items():
                    if isinstance(pos, dict):
                        shares = pos.get('shares', 0)
                    else:
                        shares = 0

                    if shares > 0:
                        active_long_positions += shares
                    elif shares < 0:
                        active_short_positions += abs(shares)
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"Failed to parse latest portfolio positions: {e}")

        return {
            'total_futures_trades': len(transactions),
            'long_trades': long_opened,
            'short_trades': short_opened,
            'active_long_positions': active_long_positions,
            'active_short_positions': active_short_positions,
            'ticker_trade_counts': ticker_trade_counts
        }

    except Exception as e:
        logger.error(f"Error calculating futures trade metrics for {config_id}: {e}")
        return {
            'total_futures_trades': 0,
            'long_trades': 0,
            'short_trades': 0,
            'active_long_positions': 0,
            'active_short_positions': 0,
            'ticker_trade_counts': {}
        }
    finally:
        if conn:
            conn.close()

def calculate_forced_liquidation_metrics(config_id: str, db_path: str) -> Dict:
    """Dual-phase futures no longer uses the legacy portfolio_forced_settlement flow."""
    logger.info(
        "Forced liquidation metrics are disabled for dual-phase futures evaluation; "
        f"legacy portfolio_forced_settlement data is ignored for config {config_id}."
    )
    return {
        'forced_liquidation_count': 0,
        'total_liquidation_loss': 0,
        'liquidation_events': []
    }


def extract_futures_metrics_from_portfolios(portfolios: List[Dict]) -> Dict:
    """Legacy portfolio-snapshot futures fallback is disabled for dual-phase evaluation."""
    logger.warning(
        "Legacy portfolio-snapshot futures fallback is disabled. "
        "Evaluation now requires daily_settlement and futures_transactions."
    )
    return {
        'total_futures_trades': 0,
        'long_trades': 0,
        'short_trades': 0,
        'active_long_positions': 0,
        'active_short_positions': 0,
        'total_settlement_pnl': 0,
        'total_commission': 0,
        'avg_daily_pnl': 0,
        'peak_margin_ratio': 0,
        'avg_margin_ratio': 0,
        'ticker_trade_counts': {}
    }

def evaluate_config(config_id: str, db_path: str = DB_PATH) -> Optional[Dict]:
    """
    Evaluate a config's performance metrics with futures support.

    Args:
        config_id: The config ID to evaluate
        db_path: Path to the SQLite database

    Returns:
        Dictionary containing all evaluation metrics, or None if evaluation fails
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Fetch all portfolios for this config, ordered by trading_date
        cursor.execute('''
            SELECT id, trading_date, cashflow, total_assets, positions
            FROM portfolio
            WHERE config_id = ? AND trading_date IS NOT NULL
            ORDER BY trading_date ASC
        ''', (config_id,))

        portfolio_rows = cursor.fetchall()

        if len(portfolio_rows) == 0:
            logger.warning(f"No portfolio data found for config_id: {config_id}")
            return None

        # Detect whether this config should be evaluated as futures.
        cursor.execute('''
            SELECT COUNT(*)
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE p.config_id = ?
        ''', (config_id,))
        settlement_count = cursor.fetchone()[0]

        cursor.execute('''
            SELECT COUNT(*)
            FROM futures_transactions
            WHERE config_id = ?
        ''', (config_id,))
        futures_transaction_count = cursor.fetchone()[0]

        # Extract portfolio data
        portfolios = []
        portfolio_values = []
        trading_dates = []
        has_futures_positions = False
        first_non_empty_positions = {}

        for row in portfolio_rows:
            positions = json.loads(row['positions']) if row['positions'] else {}
            portfolio_data = {
                'id': row['id'],
                'trading_date': datetime.fromisoformat(row['trading_date']),
                'cashflow': row['cashflow'],
                'total_assets': row['total_assets'],
                'positions': positions
            }
            portfolios.append(portfolio_data)

            if positions and not first_non_empty_positions:
                first_non_empty_positions = positions
            has_futures_positions = has_futures_positions or any(
                _is_futures_position(pos) for pos in positions.values()
            )

        is_futures = has_futures_positions or settlement_count > 0 or futures_transaction_count > 0

        for portfolio_data in portfolios:
            portfolio_data['is_futures'] = is_futures

        # Debug: log first available snapshot
        logger.info("First portfolio detection:")
        logger.info(f"  is_futures: {is_futures}")
        logger.info(f"  settlement_count: {settlement_count}")
        logger.info(f"  futures_transaction_count: {futures_transaction_count}")
        logger.info(f"  positions keys: {list(first_non_empty_positions.keys()) if first_non_empty_positions else []}")
        if first_non_empty_positions:
            first_ticker = next(iter(first_non_empty_positions))
            first_pos = first_non_empty_positions[first_ticker]
            logger.info(f"  First position ({first_ticker}) type: {type(first_pos)}")
            if isinstance(first_pos, dict):
                logger.info(f"  First position keys: {list(first_pos.keys())}")

        settlements = []
        cash_balance_values = []
        account_equity_max_drawdown = None
        cash_balance_max_drawdown = None
        intraday_max_drawdown = None
        annualization_days = None
        annualization_basis = '自然日'

        # For futures, recalculate account values from settlement data
        if portfolios and is_futures:
            logger.info("Recalculating futures account equity from settlement data...")

            # IMPORTANT: Futures Account Equity Definition
            # For futures trading, we need to track account equity correctly:
            #
            # account_equity = initial_balance + cumulative_realized_pnl - cumulative_commission
            #
            # Where:
            # - initial_balance: Starting cash (first day's previous_balance)
            # - cumulative_realized_pnl: Sum of all daily P&L from settlements
            # - cumulative_commission: Sum of all trading fees
            #
            # Key Points:
            # 1. Margin changes are NOT gains/losses - they're just locked funds
            # 2. portfolio.cashflow is misleading because it includes margin changes
            # 3. We use daily_settlement table for accurate P&L tracking
            # 4. This equity curve is used for return/volatility/drawdown calculations

            # Fetch settlement data for this config
            cursor.execute('''
                SELECT
                    ds.trading_date,
                    ds.previous_balance,
                    ds.current_balance,
                    ds.previous_margin,
                    ds.current_margin,
                    ds.daily_pnl,
                    ds.commission
                FROM daily_settlement ds
                JOIN portfolio p ON ds.portfolio_id = p.id
                WHERE p.config_id = ?
                ORDER BY ds.trading_date ASC
            ''', (config_id,))

            settlements = cursor.fetchall()
            if settlements:
                initial_capital_from_settlement = (
                    (settlements[0]['previous_balance'] or 0) +
                    (settlements[0]['previous_margin'] or 0)
                )
                trading_dates = [datetime.fromisoformat(s['trading_date']) for s in settlements]
                portfolio_values = [
                    (s['current_balance'] or 0) + (s['current_margin'] or 0)
                    for s in settlements
                ]
                cash_balance_values = [
                    s['current_balance'] or 0
                    for s in settlements
                ]
                annualization_days = len(settlements)
                annualization_basis = '结算交易日'
                logger.info("Using settlement-based equity calculation:")
                logger.info(f"  Initial capital: {initial_capital_from_settlement:,.2f}")
                logger.info(f"  Final equity: {portfolio_values[-1]:,.2f}")
                logger.info(f"  Total return: {(portfolio_values[-1] / initial_capital_from_settlement - 1):>.2%}")
            else:
                logger.warning(
                    "Futures config has no daily_settlement rows; falling back to "
                    "cashflow + margin_used + unrealized_pnl snapshots."
                )
                for p in portfolios:
                    total_margin = sum(
                        pos.get('margin_used', 0) for pos in p['positions'].values()
                        if isinstance(pos, dict)
                    )
                    total_unrealized_pnl = sum(
                        pos.get('unrealized_pnl', 0) for pos in p['positions'].values()
                        if isinstance(pos, dict)
                    )
                    account_equity = float(p['cashflow']) + total_margin + total_unrealized_pnl
                    portfolio_values.append(account_equity)
                    cash_balance_values.append(float(p['cashflow']))
                    trading_dates.append(p['trading_date'])
                annualization_days = len(trading_dates)
                annualization_basis = '组合快照'
        else:
            # For stocks, use total_assets
            for p in portfolios:
                portfolio_values.append(float(p['total_assets']))
                trading_dates.append(p['trading_date'])

        # Calculate time period
        start_date = trading_dates[0]
        end_date = trading_dates[-1]
        calendar_days = (end_date - start_date).days if len(trading_dates) > 1 else 1
        if annualization_days is None:
            annualization_days = calendar_days
        effective_period_days = annualization_days

        # Calculate returns
        # For futures: use initial_capital_from_settlement (from settlement data)
        # For stocks: use portfolio_values[0]
        if is_futures and 'initial_capital_from_settlement' in locals():
            initial_capital = initial_capital_from_settlement
        else:
            initial_capital = portfolio_values[0]
        final_capital = portfolio_values[-1]
        total_return = (final_capital / initial_capital) - 1 if initial_capital > 0 else 0

        # Debug: print account value changes
        logger.info(f"Account value analysis:")
        logger.info(f"  Initial: {initial_capital:,.2f}")
        logger.info(f"  Final: {final_capital:,.2f}")
        logger.info(f"  Change: {final_capital - initial_capital:>+15,.2f}")
        logger.info(f"  Return: {total_return:>+.2%}")
        logger.info(f"  First portfolio cashflow: {portfolios[0]['cashflow']:,.2f}")
        logger.info(f"  First portfolio total_assets: {portfolios[0]['total_assets']:,.2f}")
        logger.info(f"  Last portfolio cashflow: {portfolios[-1]['cashflow']:,.2f}")
        logger.info(f"  Last portfolio total_assets: {portfolios[-1]['total_assets']:,.2f}")
        logger.info(f"  Annualization basis: {annualization_basis} ({effective_period_days} observations)")

        returns = calculate_returns(portfolio_values)

        # Calculate metrics
        annualized_return = calculate_annualized_return(total_return, effective_period_days)
        volatility = calculate_volatility(returns, len(returns))
        sharpe_ratio = calculate_sharpe_ratio(annualized_return, volatility)
        account_equity_max_drawdown = calculate_max_drawdown(portfolio_values)
        cash_balance_max_drawdown = calculate_optional_max_drawdown(cash_balance_values)
        max_drawdown = account_equity_max_drawdown
        if is_futures and settlements:
            logger.info(
                "Drawdown metrics: "
                f"account_equity={account_equity_max_drawdown:.2%}, "
                f"cash_balance={(cash_balance_max_drawdown or 0):.2%}, "
                "intraday=unavailable"
            )
        warnings = []
        # Calculate futures-specific metrics
        # First, try to use the specialized tables
        futures_metrics = calculate_futures_metrics(config_id, db_path)
        futures_trade_metrics = calculate_futures_trade_metrics(config_id, db_path)
        forced_liquidation_metrics = calculate_forced_liquidation_metrics(config_id, db_path)
        # Debug: log the metrics from futures tables
        logger.info(f"Futures tables metrics:")
        logger.info(f"  total_settlement_pnl: {futures_metrics['total_settlement_pnl']}")
        logger.info(f"  total_commission: {futures_metrics['total_commission']}")
        logger.info(f"  long_trades: {futures_trade_metrics['long_trades']}")
        logger.info(f"  short_trades: {futures_trade_metrics['short_trades']}")
        logger.info(f"  is_futures: {is_futures}")
        if (
            is_futures
            and futures_metrics['total_settlement_pnl'] == 0
            and futures_metrics['total_commission'] == 0
            and futures_trade_metrics['long_trades'] == 0
            and futures_trade_metrics['short_trades'] == 0
        ):
            logger.warning(
                "Futures evaluation found no settlement/transaction records for "
                f"config {config_id}. Legacy portfolio fallback is disabled."
            )
            warnings.append({
                'type': 'data_quality',
                'message': 'Dual-phase futures evaluation requires daily_settlement and futures_transactions. '
                           'Current metrics may be incomplete because those records are missing or zero.'
            })
        # Calculate commission rate: total_commission / initial_capital
        commission_rate = (futures_metrics['total_commission'] / initial_capital) if initial_capital > 0 else 0

        # Calculate futures win rates. The headline win_rate uses completed
        # transaction pairs; daily settlement win rate remains a diagnostic.
        daily_win_rate_metrics = calculate_futures_trade_win_rate(config_id, db_path)
        win_rate_metrics = calculate_futures_transaction_win_rate(config_id, db_path)
        # Generate data quality warnings
        if effective_period_days < 30:
            warnings.append({
                'type': 'short_backtest_window',
                'message': (
                    f'Backtest window is only {effective_period_days} {annualization_basis}; '
                    'evaluation may be noisy.'
                )
            })
        if total_return < 0:
            warnings.append({
                'type': 'negative_total_return',
                'message': f'Total return is {total_return:.2%}; strategy lost money over the test period.'
            })
        if win_rate_metrics.get('win_rate', 0) < 0.5:
            wr = win_rate_metrics.get('win_rate', 0)
            warnings.append({
                'type': 'low_win_rate',
                'message': f'Win rate is {wr:.2%}, below the 50% threshold.'
            })

        # Compile results (merge all metrics)
        metrics = {
            # Original metrics
            'trading_date_start': start_date.isoformat(),
            'trading_date_end': end_date.isoformat(),
            'is_futures': is_futures,
            'total_return': total_return,
            'annualized_return': annualized_return,
            'sharpe_ratio': sharpe_ratio,
            'max_drawdown': max_drawdown,
            'account_equity_max_drawdown': account_equity_max_drawdown,
            'cash_balance_max_drawdown': cash_balance_max_drawdown,
            'intraday_max_drawdown': intraday_max_drawdown,
            'volatility': volatility,
            'initial_capital': initial_capital,
            'final_capital': final_capital,
            'annualization_days': effective_period_days,
            'annualization_basis': annualization_basis,

            # Futures-specific metrics
            'peak_margin_ratio': futures_metrics['peak_margin_ratio'],
            'avg_margin_ratio': futures_metrics['avg_margin_ratio'],
            'warning_days': futures_metrics['warning_days'],
            'liquidation_events': futures_metrics['liquidation_events'],
            'total_commission': futures_metrics['total_commission'],
            'avg_daily_pnl': futures_metrics['avg_daily_pnl'],
            'total_settlement_pnl': futures_metrics['total_settlement_pnl'],
            'max_margin_usage': futures_metrics['max_margin_usage'],
            'avg_leverage': futures_metrics['avg_leverage'],

            # Futures trade metrics
            'total_futures_trades': futures_trade_metrics['total_futures_trades'],
            'long_trades': futures_trade_metrics['long_trades'],
            'short_trades': futures_trade_metrics['short_trades'],
            'active_long_positions': futures_trade_metrics['active_long_positions'],
            'active_short_positions': futures_trade_metrics['active_short_positions'],
            'ticker_trade_counts': futures_trade_metrics['ticker_trade_counts'],

            # Forced liquidation metrics (kept separate from liquidation_events count)
            'forced_liquidation_count': forced_liquidation_metrics['forced_liquidation_count'],
            'total_liquidation_loss': forced_liquidation_metrics['total_liquidation_loss'],
            # Note: liquidation_events count is from futures_metrics (INTEGER), not from forced_liquidation_metrics (LIST)
            # The forced_liquidation_metrics['liquidation_events'] contains detailed event list for reporting only
            'forced_liquidation_details': forced_liquidation_metrics['liquidation_events'],  # For reporting only, not stored in DB

            # Commission rate
            'commission_rate': commission_rate,

            # Futures trade win rate metrics
            'winning_trades': win_rate_metrics['winning_trades'],
            'losing_trades': win_rate_metrics['losing_trades'],
            'flat_trades': win_rate_metrics['flat_trades'],
            'winning_days': daily_win_rate_metrics['winning_days'],
            'losing_days': daily_win_rate_metrics['losing_days'],
            'flat_days': daily_win_rate_metrics['flat_days'],
            'win_rate': win_rate_metrics['win_rate'],
            'daily_win_rate': daily_win_rate_metrics['win_rate'],
            'avg_return_per_trade': win_rate_metrics['avg_return_per_trade'],
            'avg_return_per_day': daily_win_rate_metrics['avg_return_per_day'],
            'total_trades': win_rate_metrics['total_trades'],
            'evaluated_days': daily_win_rate_metrics['evaluated_days'],
            'realized_trade_pnl': win_rate_metrics['realized_trade_pnl'],
            'unmatched_close_lots': win_rate_metrics['unmatched_close_lots'],

            # Margin call count (default to 0, could be calculated from forced_liquidation_metrics)
            'margin_call_count': forced_liquidation_metrics['forced_liquidation_count'],

            # Data quality warnings
            'warnings': warnings
        }

        return metrics

    except Exception as e:
        logger.error(f"Error evaluating config {config_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()


