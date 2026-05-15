import argparse
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import yaml
from dotenv import load_dotenv
from util.db_helper import db_initialize, get_db
from util.logger import logger
from evaluation.evaluation import evaluate_config
from database.evaluation_helper import EvaluationHelper
from database.sqlite_setup_eval import init_evaluation_database

# Load environment variables from .env file
load_dotenv()


def resolve_config_path(config_path: str) -> str:
    path = Path(config_path)
    if path.is_absolute() or path.exists():
        return str(path)

    for candidate in (SRC_ROOT / path, SRC_ROOT.parent / path):
        if candidate.exists():
            return str(candidate)

    return str(path)


def load_config_exp_name(config_path: str) -> str:
    """Load exp_name from configuration file."""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            logger.info(f"Loading configuration from {config_path}")
            cfg = yaml.safe_load(f)
            exp_name = cfg.get('exp_name')
            if not exp_name:
                raise ValueError(f"exp_name not found in configuration file: {config_path}")
            return exp_name
    except FileNotFoundError:
        raise ValueError(f"Configuration file not found: {config_path}")
    except yaml.YAMLError as e:
        raise ValueError(f"Error parsing configuration file: {e}")


def print_evaluation_summary(metrics: dict, config_id: str, exp_name: str):
    """Print a formatted summary of evaluation results with futures support."""
    is_futures = metrics.get('is_futures', False)
    annualization_days = metrics.get('annualization_days')
    annualization_basis = metrics.get('annualization_basis')

    print("\n" + "=" * 80)
    print(f"期货配置评估报告 - {exp_name}")
    print("=" * 80)
    print(f"配置名称 (exp_name): {exp_name}")
    print(f"配置ID: {config_id}")
    print(f"评估周期: {metrics.get('trading_date_start')} 至 {metrics.get('trading_date_end')}")

    # Print data quality warnings if any
    warnings = metrics.get('warnings', [])
    if warnings:
        print("-" * 80)
        print("【数据质量警告】")
        for warning in warnings:
            print(f"  ⚠️  {warning['type']}: {warning['message']}")
    print("-" * 80)

    print("【基础收益指标】")
    print(f"  总收益率:           {metrics.get('total_return', 0):.2%}")
    print(f"  累计结算盈亏:       {metrics.get('total_settlement_pnl', 0):>15,.2f}")
    print(f"  日均盈亏:           {metrics.get('avg_daily_pnl', 0):>15,.2f}")
    if annualization_days and annualization_basis:
        print(
            f"  年化收益率:         {metrics.get('annualized_return', 0):.2%} "
            f"(基于{annualization_days}个{annualization_basis}样本年化)"
        )
    else:
        print(f"  年化收益率:         {metrics.get('annualized_return', 0):.2%} (252个交易日复利)")
    print(f"  夏普比率:           {metrics.get('sharpe_ratio', 0):.4f}")
    if is_futures:
        print(
            f"  最大回撤(账户权益): {metrics.get('account_equity_max_drawdown', metrics.get('max_drawdown', 0)):.2%}"
        )
        cash_balance_max_drawdown = metrics.get('cash_balance_max_drawdown')
        if cash_balance_max_drawdown is None:
            print("  最大回撤(现金余额): 暂无可用现金余额序列")
        else:
            print(f"  最大回撤(现金余额): {cash_balance_max_drawdown:.2%}")
        intraday_max_drawdown = metrics.get('intraday_max_drawdown')
        if intraday_max_drawdown is not None:
            print(f"  最大回撤(日内权益): {intraday_max_drawdown:.2%}")
    else:
        print(f"  最大回撤:           {metrics.get('max_drawdown', 0):.2%}")
    print(f"  波动率:             {metrics.get('volatility', 0):.2%} (年化)")
    print("-" * 80)

    print("【交易统计】")
    if is_futures:
        print(f"  评估交易日数:       {metrics.get('evaluated_days', 0)} 天")
        print(f"  成交流水笔数:       {metrics.get('total_futures_trades', 0)} 笔")
        print(f"  盈利天数:           {metrics.get('winning_days', 0)} 天")
        print(f"  亏损天数:           {metrics.get('losing_days', 0)} 天")
        print(f"  持平天数:           {metrics.get('flat_days', 0)} 天")
        print(f"  日结算胜率:         {metrics.get('daily_win_rate', 0):.2%} (按日结算)")
        print(f"  完整交易对数:       {metrics.get('total_trades', 0)} 笔")
        print(f"  盈利交易:           {metrics.get('winning_trades', 0)} 笔")
        print(f"  亏损交易:           {metrics.get('losing_trades', 0)} 笔")
        print(f"  持平交易:           {metrics.get('flat_trades', 0)} 笔")
        print(f"  交易笔数胜率:       {metrics.get('win_rate', 0):.2%} (按已平仓交易)")
        print(f"  平均每笔收益率:     {metrics.get('avg_return_per_trade', 0):.2%}")
        print(f"  平均单日收益率:     {metrics.get('avg_return_per_day', 0):.2%}")
    else:
        print(f"  完整交易对数:       {metrics.get('total_trades', 0)} (开仓+平仓对)")
        print(f"  盈利交易:           {metrics.get('winning_trades', 0)} 笔")
        print(f"  亏损交易:           {metrics.get('losing_trades', 0)} 笔")
        print(f"  胜率:               {metrics.get('win_rate', 0):.2%}")
        print(f"  平均每笔收益率:     {metrics.get('avg_return_per_trade', 0):.2%}")
    print(f"  累计开多手数:       {metrics.get('long_trades', 0)} 手")
    print(f"  累计开空手数:       {metrics.get('short_trades', 0)} 手")
    print(f"  当前持仓多头:       {metrics.get('active_long_positions', 0)} 手")
    print(f"  当前持仓空头:       {metrics.get('active_short_positions', 0)} 手")
    if metrics.get('ticker_trade_counts'):
        print(f"  各品种交易记录数:   {', '.join(f'{k}:{v}' for k, v in metrics.get('ticker_trade_counts', {}).items())}")
    print("-" * 80)

    print("【保证金风险】")
    print(f"  峰值保证金比例:     {metrics.get('peak_margin_ratio', 0):.2%}")
    print(f"  平均保证金比例:     {metrics.get('avg_margin_ratio', 0):.2%}")
    print(f"  预警天数:           {metrics.get('warning_days', 0)} 天")
    print(f"  强平事件:           {metrics.get('liquidation_events', 0)} 次", end="")
    if metrics.get('liquidation_events', 0) > 0:
        print(f" ⚠️")
    else:
        print()
    if metrics.get('forced_liquidation_count', 0) > 0:
        print(f"  强制平仓次数:       {metrics.get('forced_liquidation_count', 0)} 次")
        print(f"  强平总损失:         {metrics.get('total_liquidation_loss', 0):>15,.2f}")
        if metrics.get('forced_liquidation_details'):
            print("  强平详情:")
            for event in metrics.get('forced_liquidation_details', []):
                print(f"    - {event['date']}: {event['reason']}, 损失={event['loss']:.2f}")
    print("-" * 80)

    print("【成本分析】")
    print(f"  累计手续费:         {metrics.get('total_commission', 0):>15,.2f}")
    print(f"  手续费率:           {metrics.get('commission_rate', 0):.2%}")
    print("-" * 80)

    print("【账户权益变动】")
    print(f"  初始账户权益:       {metrics.get('initial_capital', 0):>15,.2f}")
    print(f"  最终账户权益:       {metrics.get('final_capital', 0):>15,.2f}")
    print(f"  绝对收益:           {metrics.get('final_capital', 0) - metrics.get('initial_capital', 0):>+15,.2f}")
    print("=" * 80 + "\n")


def main():
    """Main entry point for evaluating config performance."""

    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Evaluate AgentQuant config performance metrics"
    )
    parser.add_argument("--config", type=str, required=True, help="Path to configuration file")
    parser.add_argument("--local-db", action="store_true", help="Use local SQLite database")
    parser.add_argument("--update", action="store_true", help="Update existing evaluation instead of creating new record")
    parser.add_argument("--init-db", action="store_true", help="Initialize evaluation database tables")
    args = parser.parse_args()
    args.config = resolve_config_path(args.config)

    # Initialize evaluation database if requested
    if args.init_db:
        logger.info("Initializing evaluation database tables...")
        init_evaluation_database()
        logger.info("Database initialization completed")
        return

    # Initialize the global database connection
    db_initialize(use_local_db=args.local_db)
    db = get_db()

    # Load exp_name from config file
    exp_name = load_config_exp_name(args.config)
    logger.info(f"Evaluating config: {exp_name}")

    # Get config_id from exp_name
    config_id = db.get_config_id_by_name(exp_name)
    if not config_id:
        logger.error(f"Config not found for exp_name: {exp_name}")
        logger.error("Please run the main workflow first to create the config.")
        sys.exit(1)

    logger.info(f"Found config_id: {config_id}")

    # Run evaluation
    logger.info("Calculating performance metrics...")
    metrics = evaluate_config(config_id)

    if metrics is None:
        logger.error("Failed to calculate metrics. Check if portfolio data exists.")
        sys.exit(1)

    # Save or update evaluation results
    eval_helper = EvaluationHelper()

    if args.update:
        logger.info("Updating existing evaluation result...")
        success = eval_helper.update_evaluation_result(config_id, metrics)
        if success:
            logger.info("Evaluation result updated successfully")
        else:
            logger.error("Failed to update evaluation result")
            sys.exit(1)
    else:
        logger.info("Saving new evaluation result...")
        eval_id = eval_helper.save_evaluation_result(config_id, metrics)
        if eval_id:
            logger.info(f"Evaluation result saved with ID: {eval_id}")
        else:
            logger.error("Failed to save evaluation result")
            sys.exit(1)

    # Print summary
    print_evaluation_summary(metrics, config_id, exp_name)

    logger.info("Evaluation completed successfully")


if __name__ == "__main__":
    main()
