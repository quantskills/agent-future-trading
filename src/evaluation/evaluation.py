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
from database.artifact_store import load_externalized_json
from database.sqlite_setup import DB_PATH
from tools.common.neutral_accountability import build_neutral_accountability_summary
from util.learning_attribution import (
    learning_effect_counts,
    learning_effects_from_context,
    learning_mechanism_counts,
    learning_mechanisms_from_context,
    learning_tags_from_context,
    summarize_pairs_by_learning_effect,
    summarize_pairs_by_learning_mechanism,
)
from util.futures_trade_pairs import (
    build_completed_trade_pairs,
    build_strategy_originated_trade_pairs,
    build_strategy_originated_trade_pairs_with_diagnostics,
    summarize_trade_pairs,
)
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
        return []

    returns = []
    for i in range(1, len(portfolio_values)):
        previous_value = portfolio_values[i - 1]
        if previous_value <= 0:
            continue
        daily_return = (portfolio_values[i] - previous_value) / previous_value
        returns.append(daily_return)
    return returns


def calculate_margin_returns(settlements) -> List[float]:
    """Calculate daily PnL divided by prior margin used for futures diagnostics."""
    returns = []
    for i in range(1, len(settlements)):
        margin = settlements[i - 1]['current_margin'] or 0
        if margin <= 0:
            continue
        daily_pnl = settlements[i]['daily_pnl'] or 0
        returns.append(daily_pnl / margin)
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
    if len(returns) < 2:
        return 0.0
    return float(np.std(returns, ddof=1) * np.sqrt(252))  # Annualize with 252 trading days per year


def calculate_sharpe_ratio(annualized_return: float, volatility: float, risk_free_rate: float = 0.03) -> float:
    """Calculate Sharpe ratio."""
    if volatility == 0:
        return 0.0
    return (annualized_return - risk_free_rate) / volatility


def calculate_optimization_acceptance_metrics(
    config_id: str,
    db_path: str = DB_PATH,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict:
    """Metrics tied to post-backtest acceptance and deployment checks."""
    conn = None
    metrics = {
        "base_capacity_days_8_12": 0,
        "strong_opportunity_days_16_20": 0,
        "margin_utilization_target_day_ratio": 0.0,
        "alpha_capacity_limited_days": 0,
        "system_under_deployed_days": 0,
        "under_deployed_days": 0,
        "non_alpha_under_deployed_days": 0,
        "capital_allocation_tier_counts": {},
        "under_deployed_reason_counts": {},
        "under_deployed_category_counts": {},
        "capital_alpha_release_candidate_count": 0,
        "capital_parameter_review_counts": {},
        "capital_diagnostic_action_counts": {},
        "pm_risk_gate_decision_counts": {},
        "protected_deployable_template_net_pnl": 0.0,
        "weak_block_template_net_pnl": 0.0,
        "learning_overlay_effective_rows": 0,
        "llm_causal_review_candidate_count": 0,
        "validated_causal_rule_count": 0,
        "causal_rule_validation_status_counts": {},
        "learned_trade_count": 0,
        "learned_trade_win_rate": 0.0,
        "learned_trade_net_pnl": 0.0,
        "unlearned_trade_count": 0,
        "unlearned_trade_win_rate": 0.0,
        "unlearned_trade_net_pnl": 0.0,
        "learned_trade_reason_counts": {},
        "learned_trade_effect_counts": {},
        "learned_trade_effect_summary": {},
        "learning_mechanism_counts": {},
        "learning_mechanism_summary": {},
        "neutral_signal_count": 0,
        "neutral_signal_ratio": 0.0,
        "neutral_accountability_complete_rate": 1.0,
        "neutral_category_counts": {},
        "neutral_by_analyst": {},
        "neutral_missing_field_counts": {},
        "neutral_review_examples": [],
        "artifact_contract_validation_pass_rate": 1.0,
        "free_text_control_violation_count": 0,
    }
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        date_filters = ["p.config_id = ?"]
        params: List = [config_id]
        if start_date:
            date_filters.append("substr(ds.trading_date, 1, 10) >= ?")
            params.append(start_date)
        if end_date:
            date_filters.append("substr(ds.trading_date, 1, 10) <= ?")
            params.append(end_date)
        cursor.execute(
            f"""
            SELECT ds.margin_ratio
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE {' AND '.join(date_filters)}
            """,
            tuple(params),
        )
        ratios = [float(row["margin_ratio"] or 0.0) for row in cursor.fetchall()]
        if ratios:
            metrics["base_capacity_days_8_12"] = sum(1 for ratio in ratios if 0.08 <= ratio <= 0.12)
            metrics["strong_opportunity_days_16_20"] = sum(1 for ratio in ratios if 0.16 <= ratio <= 0.20)
            metrics["margin_utilization_target_day_ratio"] = (
                metrics["base_capacity_days_8_12"] + metrics["strong_opportunity_days_16_20"]
            ) / len(ratios)

        try:
            deployment_filters = ["config_id = ?"]
            deployment_params: List = [config_id]
            if start_date:
                deployment_filters.append("substr(trading_date, 1, 10) >= ?")
                deployment_params.append(start_date)
            if end_date:
                deployment_filters.append("substr(trading_date, 1, 10) <= ?")
                deployment_params.append(end_date)
            cursor.execute(
                f"""
                SELECT capital_allocation_tier, reason_bucket, COUNT(*) AS cnt
                FROM capital_deployment_state
                WHERE {' AND '.join(deployment_filters)}
                GROUP BY capital_allocation_tier, reason_bucket
                """,
                tuple(deployment_params),
            )
            tier_counts = {}
            under_deployed_reasons = {}
            for row in cursor.fetchall():
                tier = str(row["capital_allocation_tier"] or "unknown")
                reason = str(row["reason_bucket"] or "")
                cnt = int(row["cnt"] or 0)
                tier_counts[tier] = tier_counts.get(tier, 0) + cnt

                if reason == "alpha_capacity_limited":
                    metrics["alpha_capacity_limited_days"] += cnt

                if tier == "under_deployed":
                    metrics["under_deployed_days"] += cnt
                    under_deployed_reasons[reason or "unknown"] = (
                        under_deployed_reasons.get(reason or "unknown", 0) + cnt
                    )
                    if reason != "alpha_capacity_limited":
                        metrics["system_under_deployed_days"] += cnt
                        metrics["non_alpha_under_deployed_days"] += cnt
                elif reason == "system_under_deployed":
                    metrics["system_under_deployed_days"] += cnt

            metrics["capital_allocation_tier_counts"] = tier_counts
            metrics["under_deployed_reason_counts"] = under_deployed_reasons

            cursor.execute(
                f"""
                SELECT capital_allocation_tier, deployment_plan_json
                FROM capital_deployment_state
                WHERE {' AND '.join(deployment_filters)}
                """,
                tuple(deployment_params),
            )
            category_counts = {}
            parameter_review_counts = {}
            action_counts = {}
            alpha_release_candidate_count = 0
            for row in cursor.fetchall():
                try:
                    deployment_plan = json.loads(row["deployment_plan_json"] or "{}")
                except Exception:
                    deployment_plan = {}
                diagnostics = deployment_plan.get("diagnostics") if isinstance(deployment_plan, dict) else {}
                if not isinstance(diagnostics, dict):
                    continue
                if str(row["capital_allocation_tier"] or "") == "under_deployed":
                    for category, count in (diagnostics.get("category_counts") or {}).items():
                        category_counts[str(category)] = category_counts.get(str(category), 0) + int(count or 0)
                    for action, count in (diagnostics.get("action_counts") or {}).items():
                        action_counts[str(action)] = action_counts.get(str(action), 0) + int(count or 0)
                    for item in diagnostics.get("parameter_review") or []:
                        if not isinstance(item, dict):
                            continue
                        key = str(item.get("scope") or item.get("reason") or "unknown")
                        parameter_review_counts[key] = parameter_review_counts.get(key, 0) + 1
                alpha_release_candidate_count += int(diagnostics.get("alpha_release_candidate_count") or 0)
            metrics["under_deployed_category_counts"] = category_counts
            metrics["capital_alpha_release_candidate_count"] = alpha_release_candidate_count
            metrics["capital_parameter_review_counts"] = parameter_review_counts
            metrics["capital_diagnostic_action_counts"] = action_counts
        except sqlite3.Error:
            pass

        try:
            cursor.execute(
                """
                SELECT memory_state, SUM(net_pnl) AS pnl
                FROM strategy_memory
                WHERE config_id = ?
                GROUP BY memory_state
                """,
                (config_id,),
            )
            for row in cursor.fetchall():
                state = str(row["memory_state"] or "")
                pnl = float(row["pnl"] or 0.0)
                if state in {"protected", "deployable"}:
                    metrics["protected_deployable_template_net_pnl"] += pnl
                if state == "weak_block":
                    metrics["weak_block_template_net_pnl"] += pnl
        except sqlite3.Error:
            pass

        try:
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM config_learning_overlay WHERE config_id = ? AND active = 1",
                (config_id,),
            )
            metrics["learning_overlay_effective_rows"] = int(cursor.fetchone()["cnt"] or 0)
        except sqlite3.Error:
            pass

        try:
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM causal_review_candidate WHERE config_id = ?",
                (config_id,),
            )
            metrics["llm_causal_review_candidate_count"] = int(cursor.fetchone()["cnt"] or 0)
            cursor.execute(
                """
                SELECT rule_validation_status, COUNT(*) AS cnt
                FROM causal_review_candidate
                WHERE config_id = ?
                GROUP BY rule_validation_status
                """,
                (config_id,),
            )
            metrics["causal_rule_validation_status_counts"] = {
                str(row["rule_validation_status"] or "unknown"): int(row["cnt"] or 0)
                for row in cursor.fetchall()
            }
            cursor.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM adaptive_policy_state
                WHERE config_id = ?
                  AND policy_type = 'causal_review_rule'
                  AND active = 1
                """,
                (config_id,),
            )
            metrics["validated_causal_rule_count"] = int(cursor.fetchone()["cnt"] or 0)
        except sqlite3.Error:
            pass

        try:
            recommendation_filters = ["config_id = ?"]
            recommendation_params: List = [config_id]
            if start_date:
                recommendation_filters.append("substr(trading_date, 1, 10) >= ?")
                recommendation_params.append(start_date)
            if end_date:
                recommendation_filters.append("substr(trading_date, 1, 10) <= ?")
                recommendation_params.append(end_date)
            cursor.execute("PRAGMA table_info(futures_recommendation)")
            recommendation_columns = {str(row["name"]) for row in cursor.fetchall()}
            snapshot_artifact_cols = (
                ", signal_snapshot_artifact_path, signal_snapshot_sha256"
                if {"signal_snapshot_artifact_path", "signal_snapshot_sha256"}.issubset(recommendation_columns)
                else ""
            )
            cursor.execute(
                f"""
                SELECT id, underlying_code, signal_snapshot{snapshot_artifact_cols}
                FROM futures_recommendation
                WHERE {' AND '.join(recommendation_filters)}
                """,
                tuple(recommendation_params),
            )
            recommendation_rows = [dict(row) for row in cursor.fetchall()]
            decision_counts = {}
            validation_total = 0
            validation_ok = 0
            free_text_violations = 0
            neutral_recommendations = []
            for row in recommendation_rows:
                snapshot = load_externalized_json(
                    row.get("signal_snapshot"),
                    row.get("signal_snapshot_artifact_path"),
                    row.get("signal_snapshot_sha256"),
                ) or {}
                if not isinstance(snapshot, dict):
                    snapshot = {}
                neutral_recommendations.append(
                    {
                        "id": row.get("id"),
                        "underlying_code": row.get("underlying_code"),
                        "signal_snapshot": snapshot,
                    }
                )
                contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
                decision = str(
                    contract.get("authority_type")
                    or contract.get("final_action")
                    or "none"
                )
                decision_counts[decision] = decision_counts.get(decision, 0) + 1
                if "artifact_validation_errors" in snapshot:
                    validation_total += 1
                    if not snapshot.get("artifact_validation_errors"):
                        validation_ok += 1
                if isinstance(contract.get("strategy_controls"), str):
                    free_text_violations += 1
            metrics["pm_risk_gate_decision_counts"] = decision_counts
            metrics["artifact_contract_validation_pass_rate"] = (
                validation_ok / validation_total if validation_total else 1.0
            )
            metrics["free_text_control_violation_count"] = free_text_violations
            neutral_summary = build_neutral_accountability_summary(neutral_recommendations, {})
            metrics["neutral_signal_count"] = int(neutral_summary.get("neutral_count") or 0)
            metrics["neutral_signal_ratio"] = float(neutral_summary.get("neutral_ratio") or 0.0)
            metrics["neutral_accountability_complete_rate"] = float(
                neutral_summary.get("accountability_complete_rate") or 0.0
            )
            metrics["neutral_category_counts"] = neutral_summary.get("category_counts", {})
            metrics["neutral_by_analyst"] = neutral_summary.get("by_analyst", {})
            metrics["neutral_missing_field_counts"] = neutral_summary.get("missing_field_counts", {})
            metrics["neutral_review_examples"] = neutral_summary.get("examples", [])
        except sqlite3.Error:
            pass

        try:
            tx_filters = ["config_id = ?"]
            tx_params: List = [config_id]
            if end_date:
                tx_filters.append("substr(trading_date, 1, 10) <= ?")
                tx_params.append(end_date)
            cursor.execute(
                f"""
                SELECT *
                FROM futures_transactions
                WHERE {' AND '.join(tx_filters)}
                ORDER BY substr(trading_date, 1, 10), created_at, id
                """,
                tuple(tx_params),
            )
            pairs = build_strategy_originated_trade_pairs([dict(row) for row in cursor.fetchall()])
            if start_date:
                pairs = [pair for pair in pairs if str(pair.get("close_date") or "") >= start_date]
            if end_date:
                pairs = [pair for pair in pairs if str(pair.get("close_date") or "") <= end_date]
            recommendation_ids = sorted(
                {
                    str(pair.get("origin_recommendation_id") or pair.get("open_recommendation_id") or "")
                    for pair in pairs
                    if pair.get("origin_recommendation_id") or pair.get("open_recommendation_id")
                }
            )
            recommendation_lookup = {}
            if recommendation_ids:
                placeholders = ", ".join(["?"] * len(recommendation_ids))
                cursor.execute("PRAGMA table_info(futures_recommendation)")
                recommendation_columns = {str(row["name"]) for row in cursor.fetchall()}
                snapshot_artifact_cols = (
                    ", signal_snapshot_artifact_path, signal_snapshot_sha256"
                    if {"signal_snapshot_artifact_path", "signal_snapshot_sha256"}.issubset(recommendation_columns)
                    else ""
                )
                cursor.execute(
                    f"""
                    SELECT id, signal_snapshot{snapshot_artifact_cols}
                    FROM futures_recommendation
                    WHERE id IN ({placeholders})
                    """,
                    tuple(recommendation_ids),
                )
                recommendation_lookup = {str(row["id"]): dict(row) for row in cursor.fetchall()}

            def _snapshot(row: Dict) -> Dict:
                loaded = load_externalized_json(
                    row.get("signal_snapshot"),
                    row.get("signal_snapshot_artifact_path"),
                    row.get("signal_snapshot_sha256"),
                ) or {}
                return loaded if isinstance(loaded, dict) else {}

            def _collect_reasons(snapshot: Dict) -> List[str]:
                reasons: List[str] = []
                contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
                value = contract.get("reason_codes")
                if isinstance(value, list):
                    reasons.extend(str(item) for item in value if item)
                return reasons

            def _learning_attribution(row: Dict) -> Tuple[List[str], List[str], List[str]]:
                snapshot = _snapshot(row)
                contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
                learning_used = contract.get("learning_used") if isinstance(contract.get("learning_used"), dict) else {}
                evidence_used = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
                auditor_diag = {
                    **learning_used,
                    "opportunity_score_components": (
                        evidence_used.get("opportunity_score_components")
                        if isinstance(evidence_used.get("opportunity_score_components"), dict)
                        else {}
                    ),
                    "pm_lifecycle_learning_impact_delta": (
                        learning_used.get("pm_lifecycle_learning_impact_delta")
                        if isinstance(learning_used.get("pm_lifecycle_learning_impact_delta"), dict)
                        else {}
                    ),
                }
                reasons = _collect_reasons(snapshot)
                return (
                    learning_tags_from_context(reasons, auditor_diag),
                    learning_effects_from_context(reasons, auditor_diag),
                    learning_mechanisms_from_context(reasons, auditor_diag, snapshot=snapshot),
                )

            learned_pairs = []
            unlearned_pairs = []
            reason_counts = {}
            for pair in pairs:
                recommendation = recommendation_lookup.get(
                    str(pair.get("origin_recommendation_id") or pair.get("open_recommendation_id") or "")
                )
                tags, effects, mechanisms = _learning_attribution(recommendation) if recommendation else ([], [], [])
                if tags and effects:
                    item = dict(pair)
                    item["learning_tags"] = tags
                    item["learning_effects"] = effects
                    item["learning_mechanisms"] = mechanisms
                    learned_pairs.append(item)
                    for tag in tags:
                        reason_counts[tag] = reason_counts.get(tag, 0) + 1
                else:
                    unlearned_pairs.append(pair)
            learned_summary = summarize_trade_pairs(learned_pairs)
            unlearned_summary = summarize_trade_pairs(unlearned_pairs)
            metrics["learned_trade_count"] = int(learned_summary.get("total_trades") or 0)
            metrics["learned_trade_win_rate"] = float(learned_summary.get("win_rate") or 0.0)
            metrics["learned_trade_net_pnl"] = float(learned_summary.get("total_pnl") or 0.0)
            metrics["unlearned_trade_count"] = int(unlearned_summary.get("total_trades") or 0)
            metrics["unlearned_trade_win_rate"] = float(unlearned_summary.get("win_rate") or 0.0)
            metrics["unlearned_trade_net_pnl"] = float(unlearned_summary.get("total_pnl") or 0.0)
            metrics["learned_trade_reason_counts"] = reason_counts
            metrics["learned_trade_effect_counts"] = learning_effect_counts(learned_pairs)
            metrics["learned_trade_effect_summary"] = summarize_pairs_by_learning_effect(learned_pairs)
            metrics["learning_mechanism_counts"] = learning_mechanism_counts(learned_pairs)
            metrics["learning_mechanism_summary"] = summarize_pairs_by_learning_mechanism(learned_pairs)
        except sqlite3.Error:
            pass
    except Exception as exc:
        logger.warning(f"Optimization acceptance metrics unavailable: {exc}")
    finally:
        if conn:
            conn.close()
    return metrics


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


def calculate_futures_trade_win_rate(
    config_id: str, db_path: str,
    start_date: str = None, end_date: str = None,
) -> Dict:
    """
    Calculate futures win rate using daily settlement P&L.
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Fetch all daily settlement records for this config
        query = '''
            SELECT ds.daily_pnl, ds.trading_date
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE p.config_id = ?
            AND ds.daily_pnl IS NOT NULL
        '''
        params: list = [config_id]
        if start_date:
            query += ' AND ds.trading_date >= ?'
            params.append(start_date)
        if end_date:
            query += ' AND ds.trading_date <= ?'
            params.append(end_date + 'T23:59:59')
        query += ' ORDER BY ds.trading_date ASC'
        cursor.execute(query, params)

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
        initial_query = '''
            SELECT ds.previous_balance, ds.previous_margin
            FROM daily_settlement ds
            JOIN portfolio p ON ds.portfolio_id = p.id
            WHERE p.config_id = ?
        '''
        initial_params: list = [config_id]
        if start_date:
            initial_query += ' AND ds.trading_date >= ?'
            initial_params.append(start_date)
        if end_date:
            initial_query += ' AND ds.trading_date <= ?'
            initial_params.append(end_date + 'T23:59:59')
        initial_query += ' ORDER BY ds.trading_date ASC LIMIT 1'
        cursor.execute(initial_query, initial_params)

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


def calculate_futures_transaction_win_rate(
    config_id: str, db_path: str,
    start_date: str = None, end_date: str = None,
) -> Dict:
    """
    Calculate futures strategy win rate from strategy-originated positions.

    The matching logic uses FIFO lots by ticker, contract, and direction:
    - open_long is matched by close_long
    - open_short is matched by close_short

    A matched open/close lot segment is counted as one completed trade. PnL is
    calculated net of the matched open commission and close commission.

    Operational transactions retain their own source type.  When rollover or
    forced_risk closes a position opened by strategy, the realized pair still
    belongs to the strategy-originated position and enters strategy metrics.
    Date windows replay all history through end_date, then select completed
    pairs by close date so inherited positions are evaluated correctly.
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
        'inherited_close_lots': 0,
        'rollover_transaction_count': 0,
        'forced_risk_transaction_count': 0,
        'operational_transaction_count': 0,
    }

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("PRAGMA table_info(futures_transactions)")
        transaction_columns = {row[1] for row in cursor.fetchall()}
        select_parts = [
            "id" if "id" in transaction_columns else "NULL AS id",
            (
                "recommendation_id"
                if "recommendation_id" in transaction_columns
                else "NULL AS recommendation_id"
            ),
            "trading_date",
            "created_at" if "created_at" in transaction_columns else "trading_date AS created_at",
            "ticker",
            "contract_code" if "contract_code" in transaction_columns else "ticker AS contract_code",
            "action",
            "lots",
            (
                "execution_price"
                if "execution_price" in transaction_columns
                else "price AS execution_price"
            ),
            "price" if "price" in transaction_columns else "execution_price AS price",
            (
                "contract_multiplier"
                if "contract_multiplier" in transaction_columns
                else "1.0 AS contract_multiplier"
            ),
            "commission" if "commission" in transaction_columns else "0.0 AS commission",
            "source_type" if "source_type" in transaction_columns else "'strategy' AS source_type",
        ]
        params: list = [config_id]
        end_filter = ""
        if end_date:
            end_filter = " AND substr(trading_date, 1, 10) <= ?"
            params.append(end_date)

        cursor.execute(
            f'''
            SELECT {', '.join(select_parts)}
            FROM futures_transactions
            WHERE config_id = ?
              AND action IN ('open_long', 'open_short', 'close_long', 'close_short')
              {end_filter}
            ORDER BY substr(trading_date, 1, 10) ASC, created_at ASC, id ASC
            ''',
            params,
        )
        transactions = [dict(row) for row in cursor.fetchall()]

        if not transactions:
            logger.warning("No futures transaction data available for transaction win-rate calculation")
            return empty_result.copy()

        strategy_pairs, diagnostics = build_strategy_originated_trade_pairs_with_diagnostics(transactions)

        def in_window(value: object) -> bool:
            date_text = str(value or "")[:10]
            if start_date and date_text < start_date:
                return False
            if end_date and date_text > end_date:
                return False
            return True

        strategy_pairs = [pair for pair in strategy_pairs if in_window(pair.get("close_date"))]
        summary = summarize_trade_pairs(strategy_pairs)
        trade_returns = [float(pair.get("return_on_notional") or 0.0) for pair in strategy_pairs]
        window_transactions = [row for row in transactions if in_window(row.get("trading_date"))]
        source_types = [str(row.get("source_type") or "strategy").lower() for row in window_transactions]
        rollover_transaction_count = sum(1 for value in source_types if value == "rollover")
        forced_risk_transaction_count = sum(1 for value in source_types if value == "forced_risk")
        operational_transaction_count = sum(1 for value in source_types if value != "strategy")
        unmatched_close_lots = sum(
            int(row.get("lots") or 0)
            for row in diagnostics.get("unmatched_closes", [])
            if in_window(row.get("close_date"))
        )
        inherited_close_lots = (
            sum(
                int(pair.get("lots") or 0)
                for pair in strategy_pairs
                if str(pair.get("origin_open_date") or "") < start_date
            )
            if start_date
            else 0
        )
        winning_trades = int(summary["winning_trades"])
        losing_trades = int(summary["losing_trades"])
        flat_trades = int(summary["flat_trades"])
        total_trades = int(summary["total_trades"])
        win_rate = float(summary["win_rate"])
        realized_trade_pnl = float(summary["total_pnl"])
        avg_return = float(np.mean(trade_returns)) if trade_returns else 0.0

        logger.info("Futures transaction win-rate statistics:")
        logger.info(f"  Completed trades: {total_trades}")
        logger.info(f"  Winning trades: {winning_trades}")
        logger.info(f"  Losing trades: {losing_trades}")
        logger.info(f"  Flat trades: {flat_trades}")
        logger.info(f"  Transaction win rate: {win_rate:.2%}")
        logger.info(f"  Realized transaction PnL: {realized_trade_pnl:+,.2f}")
        logger.info(
            "  Operational transactions retained for position matching and separately counted: "
            f"rollover={rollover_transaction_count}, forced_risk={forced_risk_transaction_count}"
        )

        return {
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'flat_trades': flat_trades,
            'win_rate': win_rate,
            'avg_return_per_trade': avg_return,
            'total_trades': total_trades,
            'realized_trade_pnl': realized_trade_pnl,
            'unmatched_close_lots': unmatched_close_lots,
            'inherited_close_lots': inherited_close_lots,
            'rollover_transaction_count': rollover_transaction_count,
            'forced_risk_transaction_count': forced_risk_transaction_count,
            'operational_transaction_count': operational_transaction_count,
        }

    except Exception as e:
        logger.error(f"Error calculating futures transaction win rate: {e}")
        import traceback
        traceback.print_exc()
        return empty_result.copy()
    finally:
        if conn:
            conn.close()


def _sqlite_table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _sqlite_columns(cursor: sqlite3.Cursor, table_name: str) -> set:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def _max_negative_streak(values: List[float]) -> int:
    max_streak = 0
    current = 0
    for value in values:
        if value < 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


def calculate_futures_strategy_quality_metrics(
    config_id: str,
    db_path: str,
    *,
    initial_capital: float = 0.0,
    total_return: float = 0.0,
    annualized_return: float = 0.0,
    max_drawdown: float = 0.0,
    start_date: str = None,
    end_date: str = None,
) -> Dict:
    """Calculate futures strategy-quality metrics beyond headline P&L."""
    metrics = {
        "net_settlement_pnl": 0.0,
        "calmar_ratio": 0.0,
        "return_drawdown_ratio": 0.0,
        "profit_factor": 0.0,
        "payoff_ratio": 0.0,
        "trade_expectancy": 0.0,
        "avg_win_pnl": 0.0,
        "avg_loss_pnl": 0.0,
        "max_trade_gain": 0.0,
        "max_trade_loss": 0.0,
        "max_consecutive_losing_trades": 0,
        "max_consecutive_losing_days": 0,
        "return_on_avg_margin": 0.0,
        "commission_drag_ratio": 0.0,
        "margin_cap_violation_days": 0,
        "ticker_abs_contribution_top3_ratio": 0.0,
        "profitable_ticker_count": 0,
        "losing_ticker_count": 0,
        "top_profit_ticker": "",
        "top_profit_ticker_pnl": 0.0,
        "worst_loss_ticker": "",
        "worst_loss_ticker_pnl": 0.0,
        "long_trade_net_pnl": 0.0,
        "short_trade_net_pnl": 0.0,
        "ticker_net_pnl": {},
    }
    metrics["calmar_ratio"] = (
        float(annualized_return) / float(max_drawdown)
        if max_drawdown and max_drawdown > 0
        else 0.0
    )
    metrics["return_drawdown_ratio"] = (
        float(total_return) / float(max_drawdown)
        if max_drawdown and max_drawdown > 0
        else 0.0
    )

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if _sqlite_table_exists(cursor, "daily_settlement"):
            settlement_query = """
                SELECT
                    ds.trading_date,
                    COALESCE(ds.daily_pnl, 0) AS daily_pnl,
                    COALESCE(ds.commission, 0) AS commission,
                    COALESCE(ds.previous_margin, 0) AS previous_margin,
                    COALESCE(ds.current_margin, 0) AS current_margin,
                    COALESCE(ds.margin_ratio, 0) AS margin_ratio
                FROM daily_settlement ds
                JOIN portfolio p ON ds.portfolio_id = p.id
                WHERE p.config_id = ?
            """
            params: List = [config_id]
            if start_date:
                settlement_query += " AND ds.trading_date >= ?"
                params.append(start_date)
            if end_date:
                settlement_query += " AND ds.trading_date <= ?"
                params.append(end_date + "T23:59:59")
            settlement_query += " ORDER BY ds.trading_date ASC"
            cursor.execute(settlement_query, params)
            settlements = cursor.fetchall()
            if settlements:
                daily_pnls = [float(row["daily_pnl"] or 0.0) for row in settlements]
                commissions = [float(row["commission"] or 0.0) for row in settlements]
                margin_bases = [
                    (
                        float(row["current_margin"] or 0.0) + float(row["previous_margin"] or 0.0)
                    ) / 2.0
                    for row in settlements
                ]
                net_settlement_pnl = sum(daily_pnls) - sum(commissions)
                margin_returns = [
                    (daily_pnl - commission) / margin
                    for daily_pnl, commission, margin in zip(daily_pnls, commissions, margin_bases)
                    if margin > 0
                ]
                gross_pnl_activity = sum(abs(value) for value in daily_pnls)

                metrics["net_settlement_pnl"] = net_settlement_pnl
                metrics["max_consecutive_losing_days"] = _max_negative_streak(daily_pnls)
                metrics["return_on_avg_margin"] = float(np.mean(margin_returns)) if margin_returns else 0.0
                metrics["commission_drag_ratio"] = sum(commissions) / gross_pnl_activity if gross_pnl_activity > 0 else 0.0
                metrics["margin_cap_violation_days"] = sum(
                    1 for row in settlements if float(row["margin_ratio"] or 0.0) > 0.20
                )

        if _sqlite_table_exists(cursor, "futures_transactions"):
            tx_columns = _sqlite_columns(cursor, "futures_transactions")
            select_parts = [
                "id" if "id" in tx_columns else "NULL AS id",
                "recommendation_id" if "recommendation_id" in tx_columns else "NULL AS recommendation_id",
                "trading_date",
                "created_at" if "created_at" in tx_columns else "trading_date AS created_at",
                "ticker",
                "contract_code" if "contract_code" in tx_columns else "ticker AS contract_code",
                "action",
                "lots",
                "execution_price" if "execution_price" in tx_columns else "price AS execution_price",
                "price" if "price" in tx_columns else "execution_price AS price",
                "contract_multiplier" if "contract_multiplier" in tx_columns else "1.0 AS contract_multiplier",
                "commission" if "commission" in tx_columns else "0.0 AS commission",
                "source_type" if "source_type" in tx_columns else "'strategy' AS source_type",
            ]
            tx_query = f"""
                SELECT {', '.join(select_parts)}
                FROM futures_transactions
                WHERE config_id = ?
                  AND action IN ('open_long', 'open_short', 'close_long', 'close_short')
            """
            tx_params: List = [config_id]
            if end_date:
                tx_query += " AND substr(trading_date, 1, 10) <= ?"
                tx_params.append(end_date)
            tx_query += " ORDER BY trading_date ASC, created_at ASC"
            cursor.execute(tx_query, tx_params)
            pairs = build_strategy_originated_trade_pairs(cursor.fetchall())
            if start_date:
                pairs = [pair for pair in pairs if str(pair.get("close_date") or "") >= start_date]
            if end_date:
                pairs = [pair for pair in pairs if str(pair.get("close_date") or "") <= end_date]
            if pairs:
                pairs = sorted(pairs, key=lambda row: (row.get("close_date") or "", row.get("open_date") or ""))
                pnls = [float(row.get("net_pnl") or 0.0) for row in pairs]
                wins = [value for value in pnls if value > 0]
                losses = [value for value in pnls if value < 0]
                gross_profit = sum(wins)
                gross_loss = abs(sum(losses))
                avg_win = float(np.mean(wins)) if wins else 0.0
                avg_loss = float(np.mean(losses)) if losses else 0.0

                metrics["profit_factor"] = gross_profit / gross_loss if gross_loss > 0 else 0.0
                metrics["avg_win_pnl"] = avg_win
                metrics["avg_loss_pnl"] = avg_loss
                metrics["payoff_ratio"] = avg_win / abs(avg_loss) if avg_loss < 0 else 0.0
                metrics["trade_expectancy"] = sum(pnls) / len(pnls)
                metrics["max_trade_gain"] = max(pnls)
                metrics["max_trade_loss"] = min(pnls)
                metrics["max_consecutive_losing_trades"] = _max_negative_streak(pnls)
                metrics["long_trade_net_pnl"] = sum(
                    float(row.get("net_pnl") or 0.0) for row in pairs if row.get("side") == "long"
                )
                metrics["short_trade_net_pnl"] = sum(
                    float(row.get("net_pnl") or 0.0) for row in pairs if row.get("side") == "short"
                )

        if _sqlite_table_exists(cursor, "ticker_daily_pnl"):
            tdp_columns = _sqlite_columns(cursor, "ticker_daily_pnl")
            commission_expr = "COALESCE(tdp.commission, 0)" if "commission" in tdp_columns else "0"
            ticker_query = f"""
                SELECT
                    UPPER(tdp.ticker) AS ticker,
                    SUM(COALESCE(tdp.daily_pnl, 0) - {commission_expr}) AS net_pnl
                FROM ticker_daily_pnl tdp
                JOIN portfolio p ON tdp.portfolio_id = p.id
                WHERE p.config_id = ?
            """
            ticker_params: List = [config_id]
            if start_date:
                ticker_query += " AND tdp.trading_date >= ?"
                ticker_params.append(start_date)
            if end_date:
                ticker_query += " AND tdp.trading_date <= ?"
                ticker_params.append(end_date + "T23:59:59")
            ticker_query += " GROUP BY UPPER(tdp.ticker)"
            cursor.execute(ticker_query, ticker_params)
            ticker_pnl = {
                str(row["ticker"] or "").upper(): float(row["net_pnl"] or 0.0)
                for row in cursor.fetchall()
                if row["ticker"]
            }
            if ticker_pnl:
                metrics["ticker_net_pnl"] = ticker_pnl
                metrics["profitable_ticker_count"] = sum(1 for value in ticker_pnl.values() if value > 0)
                metrics["losing_ticker_count"] = sum(1 for value in ticker_pnl.values() if value < 0)
                top_ticker, top_pnl = max(ticker_pnl.items(), key=lambda item: item[1])
                worst_ticker, worst_pnl = min(ticker_pnl.items(), key=lambda item: item[1])
                metrics["top_profit_ticker"] = top_ticker
                metrics["top_profit_ticker_pnl"] = top_pnl
                metrics["worst_loss_ticker"] = worst_ticker
                metrics["worst_loss_ticker_pnl"] = worst_pnl
                abs_values = sorted((abs(value) for value in ticker_pnl.values()), reverse=True)
                abs_total = sum(abs_values)
                metrics["ticker_abs_contribution_top3_ratio"] = (
                    sum(abs_values[:3]) / abs_total if abs_total > 0 else 0.0
                )

    except Exception as exc:
        logger.warning(f"Futures strategy quality metrics unavailable: {exc}")
    finally:
        if conn:
            conn.close()
    return metrics


def calculate_learning_usage_metrics(
    config_id: str,
    db_path: str,
    start_date: str = None,
    end_date: str = None,
) -> Dict:
    """Measure whether free-exploration learning memories are written and reused."""
    metrics = {
        "trade_episode_memory_count": 0,
        "exploratory_hypothesis_count": 0,
        "learning_context_budget_rows": 0,
        "learning_context_with_episode_rows": 0,
        "learning_context_with_hypothesis_rows": 0,
        "learning_context_with_memory_ratio": 0.0,
        "avg_learning_context_chars": 0.0,
    }
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if _sqlite_table_exists(cursor, "trade_episode_memory"):
            columns = _sqlite_columns(cursor, "trade_episode_memory")
            date_expr = "trading_date"
            if {"episode_date", "close_date", "trading_date"}.issubset(columns):
                date_expr = "COALESCE(episode_date, close_date, trading_date)"
            query = f"SELECT COUNT(*) AS cnt FROM trade_episode_memory WHERE config_id = ?"
            params: List = [config_id]
            if start_date:
                query += f" AND {date_expr} >= ?"
                params.append(start_date)
            if end_date:
                query += f" AND {date_expr} <= ?"
                params.append(end_date + "T23:59:59")
            cursor.execute(query, params)
            metrics["trade_episode_memory_count"] = int(cursor.fetchone()["cnt"] or 0)

        if _sqlite_table_exists(cursor, "exploratory_hypothesis"):
            query = "SELECT COUNT(*) AS cnt FROM exploratory_hypothesis WHERE config_id = ?"
            params = [config_id]
            if start_date:
                query += " AND trading_date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND trading_date <= ?"
                params.append(end_date + "T23:59:59")
            cursor.execute(query, params)
            metrics["exploratory_hypothesis_count"] = int(cursor.fetchone()["cnt"] or 0)

        if _sqlite_table_exists(cursor, "learning_context_budget"):
            columns = _sqlite_columns(cursor, "learning_context_budget")
            episode_expr = "trade_episode_count" if "trade_episode_count" in columns else "0"
            hypothesis_expr = "hypothesis_count" if "hypothesis_count" in columns else "0"
            chars_expr = "total_context_chars" if "total_context_chars" in columns else "selected_chars"
            query = f"""
                SELECT
                    COUNT(*) AS rows,
                    SUM(CASE WHEN COALESCE({episode_expr}, 0) > 0 THEN 1 ELSE 0 END) AS episode_rows,
                    SUM(CASE WHEN COALESCE({hypothesis_expr}, 0) > 0 THEN 1 ELSE 0 END) AS hypothesis_rows,
                    SUM(CASE WHEN COALESCE({episode_expr}, 0) > 0 OR COALESCE({hypothesis_expr}, 0) > 0 THEN 1 ELSE 0 END) AS memory_rows,
                    AVG(COALESCE({chars_expr}, 0)) AS avg_chars
                FROM learning_context_budget
                WHERE config_id = ?
            """
            params = [config_id]
            if start_date:
                query += " AND trading_date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND trading_date <= ?"
                params.append(end_date + "T23:59:59")
            cursor.execute(query, params)
            row = cursor.fetchone()
            budget_rows = int(row["rows"] or 0)
            episode_rows = int(row["episode_rows"] or 0)
            hypothesis_rows = int(row["hypothesis_rows"] or 0)
            memory_rows = int(row["memory_rows"] or 0)
            metrics["learning_context_budget_rows"] = budget_rows
            metrics["learning_context_with_episode_rows"] = episode_rows
            metrics["learning_context_with_hypothesis_rows"] = hypothesis_rows
            metrics["avg_learning_context_chars"] = float(row["avg_chars"] or 0.0)
            metrics["learning_context_with_memory_ratio"] = (
                min(1.0, memory_rows / budget_rows) if budget_rows > 0 else 0.0
            )

    except Exception as exc:
        logger.warning(f"Learning usage metrics unavailable: {exc}")
    finally:
        if conn:
            conn.close()
    return metrics


def calculate_futures_metrics(
    config_id: str, db_path: str,
    start_date: str = None, end_date: str = None,
) -> Dict:
    """
    Calculate futures-specific metrics from daily_settlement and portfolio tables.

    Args:
        config_id: The config ID to evaluate
        db_path: Path to the SQLite database
        start_date: Optional start date filter (ISO format)
        end_date: Optional end date filter (ISO format)

    Returns:
        Dictionary containing futures-specific metrics
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Fetch all settlement records for this config
        settlement_query = '''
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
        '''
        params: list = [config_id]
        if start_date:
            settlement_query += ' AND ds.trading_date >= ?'
            params.append(start_date)
        if end_date:
            settlement_query += ' AND ds.trading_date <= ?'
            params.append(end_date + 'T23:59:59')
        settlement_query += ' ORDER BY ds.trading_date ASC'
        cursor.execute(settlement_query, params)

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
        logger.info(f"  {'Day':<4} {'PnL':>10} {'Comm':>8} {'PrevCash':>13} {'CurrCash':>13} {'PrevMgn':>10} {'CurrMgn':>10} {'CashChg':>10} {'ExpCash':>10} {'MgnChg':>10}")
        for i, s in enumerate(settlements):
            daily_change = s['current_balance'] - s['previous_balance']
            margin_change = (s['previous_margin'] or 0) - (s['current_margin'] or 0)
            expected_change = (s['daily_pnl'] or 0) - (s['commission'] or 0) + margin_change
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
                portfolio_metric_query = '''
                    SELECT p.leverage, p.total_assets, p.cashflow, p.margin_used, p.positions
                    FROM portfolio p
                    WHERE p.config_id = ? AND p.trading_date IS NOT NULL
                '''
                portfolio_metric_params: list = [config_id]
                if start_date:
                    portfolio_metric_query += ' AND p.trading_date >= ?'
                    portfolio_metric_params.append(start_date)
                if end_date:
                    portfolio_metric_query += ' AND p.trading_date <= ?'
                    portfolio_metric_params.append(end_date + 'T23:59:59')
                portfolio_metric_query += ' ORDER BY p.trading_date ASC'
                cursor.execute(portfolio_metric_query, portfolio_metric_params)

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


def calculate_futures_trade_metrics(
    config_id: str, db_path: str,
    start_date: str = None, end_date: str = None,
) -> Dict:
    """
    Calculate futures trading statistics from futures_transactions table.

    Args:
        config_id: The config ID to evaluate
        db_path: Path to the SQLite database
        start_date: Optional start date filter (ISO format)
        end_date: Optional end date filter (ISO format)

    Returns:
        Dictionary containing futures trade metrics
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Fetch all futures transactions for this config
        tx_query = '''
            SELECT
                ft.action,
                ft.lots,
                ft.execution_price AS price,
                ft.settle_price,
                ft.contract_multiplier,
                ft.commission,
                ft.ticker
            FROM futures_transactions ft
            WHERE ft.config_id = ?
        '''
        params: list = [config_id]
        if start_date:
            tx_query += ' AND ft.trading_date >= ?'
            params.append(start_date)
        if end_date:
            tx_query += ' AND ft.trading_date <= ?'
            params.append(end_date + 'T23:59:59')
        tx_query += ' ORDER BY ft.trading_date ASC, ft.created_at ASC'
        cursor.execute(tx_query, params)

        transactions = cursor.fetchall()

        if len(transactions) == 0:
            logger.warning(f"No futures_transactions data found for config_id: {config_id}")
            return {
                'total_futures_trades': 0,
                'long_trades': 0,
                'short_trades': 0,
                'active_long_positions': 0,
                'active_short_positions': 0,
                'total_turnover_notional': 0.0,
                'total_transaction_commission': 0.0,
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
        total_turnover_notional = 0.0
        total_transaction_commission = 0.0

        for tx in transactions:
            action = tx['action']
            lots = tx['lots'] or 0
            ticker = tx['ticker']

            if lots == 0:
                continue

            price = float(tx['price'] or tx['settle_price'] or 0.0)
            multiplier = float(tx['contract_multiplier'] or 1.0)
            total_turnover_notional += abs(float(lots) * price * multiplier)
            total_transaction_commission += float(tx['commission'] or 0.0)

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

        # Query the latest portfolio within the evaluation window for actual positions.
        latest_portfolio_query = '''
            SELECT positions
            FROM portfolio
            WHERE config_id = ?
        '''
        latest_portfolio_params: list = [config_id]
        if start_date:
            latest_portfolio_query += ' AND trading_date >= ?'
            latest_portfolio_params.append(start_date)
        if end_date:
            latest_portfolio_query += ' AND trading_date <= ?'
            latest_portfolio_params.append(end_date + 'T23:59:59')
        latest_portfolio_query += ' ORDER BY trading_date DESC, updated_at DESC LIMIT 1'
        cursor.execute(latest_portfolio_query, latest_portfolio_params)
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
            'total_turnover_notional': total_turnover_notional,
            'total_transaction_commission': total_transaction_commission,
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
            'total_turnover_notional': 0.0,
            'total_transaction_commission': 0.0,
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

def evaluate_config(
    config_id: str, db_path: str = DB_PATH,
    start_date: str = None, end_date: str = None,
) -> Optional[Dict]:
    """
    Evaluate a config's performance metrics with futures support.

    Args:
        config_id: The config ID to evaluate
        db_path: Path to the SQLite database
        start_date: Optional start date filter (ISO format, e.g. '2025-01-02')
        end_date: Optional end date filter (ISO format, e.g. '2025-01-31')

    Returns:
        Dictionary containing all evaluation metrics, or None if evaluation fails
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Fetch all portfolios for this config, ordered by trading_date
        portfolio_query = '''
            SELECT id, trading_date, cashflow, total_assets, positions
            FROM portfolio
            WHERE config_id = ? AND trading_date IS NOT NULL
        '''
        portfolio_params: list = [config_id]
        if start_date:
            portfolio_query += ' AND trading_date >= ?'
            portfolio_params.append(start_date)
        if end_date:
            portfolio_query += ' AND trading_date <= ?'
            portfolio_params.append(end_date + 'T23:59:59')
        portfolio_query += ' ORDER BY trading_date ASC'
        cursor.execute(portfolio_query, portfolio_params)

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
        account_equity_curve_values = []
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
            settlement_query = '''
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
            '''
            settlement_params: list = [config_id]
            if start_date:
                settlement_query += ' AND ds.trading_date >= ?'
                settlement_params.append(start_date)
            if end_date:
                settlement_query += ' AND ds.trading_date <= ?'
                settlement_params.append(end_date + 'T23:59:59')
            settlement_query += ' ORDER BY ds.trading_date ASC'
            cursor.execute(settlement_query, settlement_params)

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
                account_equity_curve_values = [initial_capital_from_settlement] + portfolio_values
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
                account_equity_curve_values = portfolio_values.copy()
                annualization_days = len(trading_dates)
                annualization_basis = '组合快照'
        else:
            # For stocks, use total_assets
            for p in portfolios:
                portfolio_values.append(float(p['total_assets']))
                trading_dates.append(p['trading_date'])
            account_equity_curve_values = portfolio_values.copy()

        # Calculate time period
        period_start = trading_dates[0]
        period_end = trading_dates[-1]
        calendar_days = (period_end - period_start).days if len(trading_dates) > 1 else 1
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

        account_returns = calculate_returns(account_equity_curve_values or portfolio_values)
        margin_returns = []
        margin_return_volatility = 0.0
        margin_return_annualized_return = 0.0
        margin_return_sharpe_ratio = 0.0
        if is_futures:
            # Headline risk metrics use account-equity returns. Margin-normalized
            # returns are diagnostics because small margin bases can exaggerate
            # apparent performance.
            margin_returns = calculate_margin_returns(settlements)
            margin_return_volatility = calculate_volatility(margin_returns, len(margin_returns))
            margin_return_annualized_return = calculate_annualized_return(
                sum(margin_returns), len(margin_returns)
            )
            margin_return_sharpe_ratio = calculate_sharpe_ratio(
                margin_return_annualized_return, margin_return_volatility
            )
            annualized_return = calculate_annualized_return(total_return, effective_period_days)
            volatility = calculate_volatility(account_returns, len(account_returns))
            sharpe_ratio = calculate_sharpe_ratio(annualized_return, volatility)
        else:
            annualized_return = calculate_annualized_return(total_return, effective_period_days)
            volatility = calculate_volatility(account_returns, len(account_returns))
            sharpe_ratio = calculate_sharpe_ratio(annualized_return, volatility)
        risk_metric_status = "ok" if len(account_returns) >= 2 else "sample_insufficient"
        account_equity_max_drawdown = calculate_max_drawdown(account_equity_curve_values or portfolio_values)
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
        futures_metrics = calculate_futures_metrics(config_id, db_path, start_date, end_date)
        futures_trade_metrics = calculate_futures_trade_metrics(config_id, db_path, start_date, end_date)
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
        # Headline commission rate is fee / traded notional. The old
        # capital-based fee rate is retained as a separate diagnostic.
        total_turnover_notional = float(futures_trade_metrics.get('total_turnover_notional', 0.0) or 0.0)
        capital_commission_rate = (
            futures_metrics['total_commission'] / initial_capital
            if initial_capital > 0
            else 0.0
        )
        commission_rate = (
            futures_metrics['total_commission'] / total_turnover_notional
            if total_turnover_notional > 0
            else 0.0
        )

        # Calculate futures win rates. The headline win_rate uses completed
        # transaction pairs; daily settlement win rate remains a diagnostic.
        daily_win_rate_metrics = calculate_futures_trade_win_rate(config_id, db_path, start_date, end_date)
        win_rate_metrics = calculate_futures_transaction_win_rate(config_id, db_path, start_date, end_date)
        optimization_metrics = calculate_optimization_acceptance_metrics(config_id, db_path, start_date, end_date)
        quality_metrics = calculate_futures_strategy_quality_metrics(
            config_id,
            db_path,
            initial_capital=initial_capital,
            total_return=total_return,
            annualized_return=annualized_return,
            max_drawdown=max_drawdown,
            start_date=start_date,
            end_date=end_date,
        )
        learning_usage_metrics = calculate_learning_usage_metrics(config_id, db_path, start_date, end_date)
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
        if risk_metric_status == "sample_insufficient":
            warnings.append({
                'type': 'risk_sample_insufficient',
                'message': (
                    f'Only {len(account_returns)} account-equity return sample(s) are available; '
                    'volatility and Sharpe ratio are not statistically meaningful.'
                )
            })
        if win_rate_metrics.get('total_trades', 0) == 0:
            warnings.append({
                'type': 'no_completed_round_trips',
                'message': 'No completed open-close futures trades; transaction win rate is not available yet.'
            })
        elif win_rate_metrics.get('win_rate', 0) < 0.5:
            wr = win_rate_metrics.get('win_rate', 0)
            warnings.append({
                'type': 'low_win_rate',
                'message': f'Win rate is {wr:.2%}, below the 50% threshold.'
            })

        # Compile results (merge all metrics)
        metrics = {
            # Original metrics
            'trading_date_start': period_start.isoformat(),
            'trading_date_end': period_end.isoformat(),
            'is_futures': is_futures,
            'total_return': total_return,
            'annualized_return': annualized_return,
            'sharpe_ratio': sharpe_ratio,
            'max_drawdown': max_drawdown,
            'account_equity_max_drawdown': account_equity_max_drawdown,
            'cash_balance_max_drawdown': cash_balance_max_drawdown,
            'intraday_max_drawdown': intraday_max_drawdown,
            'volatility': volatility,
            'risk_metric_status': risk_metric_status,
            'account_equity_return_sample_count': len(account_returns),
            'margin_return_sample_count': len(margin_returns),
            'margin_return_volatility': margin_return_volatility,
            'margin_return_annualized_return': margin_return_annualized_return,
            'margin_return_sharpe_ratio': margin_return_sharpe_ratio,
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
            'total_turnover_notional': total_turnover_notional,
            'total_transaction_commission': futures_trade_metrics.get('total_transaction_commission', 0.0),
            'ticker_trade_counts': futures_trade_metrics['ticker_trade_counts'],

            # Forced liquidation metrics (kept separate from liquidation_events count)
            'forced_liquidation_count': forced_liquidation_metrics['forced_liquidation_count'],
            'total_liquidation_loss': forced_liquidation_metrics['total_liquidation_loss'],
            # Note: liquidation_events count is from futures_metrics (INTEGER), not from forced_liquidation_metrics (LIST)
            # The forced_liquidation_metrics['liquidation_events'] contains detailed event list for reporting only
            'forced_liquidation_details': forced_liquidation_metrics['liquidation_events'],  # For reporting only, not stored in DB

            # Commission rate
            'commission_rate': commission_rate,
            'capital_commission_rate': capital_commission_rate,

            # Futures trade win rate metrics
            'winning_trades': win_rate_metrics['winning_trades'],
            'losing_trades': win_rate_metrics['losing_trades'],
            'flat_trades': win_rate_metrics['flat_trades'],
            'winning_days': daily_win_rate_metrics['winning_days'],
            'losing_days': daily_win_rate_metrics['losing_days'],
            'flat_days': daily_win_rate_metrics['flat_days'],
            'win_rate': win_rate_metrics['win_rate'],
            'win_rate_available': win_rate_metrics['total_trades'] > 0,
            'daily_win_rate': daily_win_rate_metrics['win_rate'],
            'avg_return_per_trade': win_rate_metrics['avg_return_per_trade'],
            'avg_return_per_day': daily_win_rate_metrics['avg_return_per_day'],
            'total_trades': win_rate_metrics['total_trades'],
            'evaluated_days': daily_win_rate_metrics['evaluated_days'],
            'realized_trade_pnl': win_rate_metrics['realized_trade_pnl'],
            'unmatched_close_lots': win_rate_metrics['unmatched_close_lots'],
            'inherited_close_lots': win_rate_metrics.get('inherited_close_lots', 0),
            'rollover_transaction_count': win_rate_metrics.get('rollover_transaction_count', 0),
            'forced_risk_transaction_count': win_rate_metrics.get('forced_risk_transaction_count', 0),
            'operational_transaction_count': win_rate_metrics.get('operational_transaction_count', 0),

            # Margin call count (default to 0, could be calculated from forced_liquidation_metrics)
            'margin_call_count': forced_liquidation_metrics['forced_liquidation_count'],

            # Optimization-plan acceptance metrics
            **optimization_metrics,
            **quality_metrics,
            **learning_usage_metrics,

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


