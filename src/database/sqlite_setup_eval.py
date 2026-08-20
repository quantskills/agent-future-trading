import os
import sqlite3
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables from .env file
# Find .env file in parent directories if not in current directory
env_path = Path(__file__).resolve().parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path)

# Get DB_PATH and convert to absolute path relative to project root
db_path_relative = os.getenv("DB_PATH", "src/assets/agentquant.db")
project_root = Path(__file__).resolve().parent.parent.parent
DB_PATH = str(project_root / db_path_relative)

# Ensure database directory exists
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def init_evaluation_database():
    """Initialize the config_outcome table for storing evaluation metrics."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 删除旧表（如果存在）以确保使用新的schema
    cursor.execute('DROP TABLE IF EXISTS config_outcome')
    print("✓ 已删除旧的 config_outcome 表")

    # Create config_outcome table with futures-specific fields
    cursor.execute('''
    CREATE TABLE config_outcome (
        id VARCHAR(36) PRIMARY KEY,
        config_id VARCHAR(36) NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        trading_date_start TIMESTAMP,
        trading_date_end TIMESTAMP,
        total_return DECIMAL(10,4),
        annualized_return DECIMAL(10,4),
        sharpe_ratio DECIMAL(10,4),
        max_drawdown DECIMAL(10,4),
        win_rate DECIMAL(10,4),
        total_trades INTEGER,
        winning_trades INTEGER,
        losing_trades INTEGER,
        avg_return_per_trade DECIMAL(10,4),
        volatility DECIMAL(10,4),
        initial_capital DECIMAL(15,2),
        final_capital DECIMAL(15,2),
        -- 期货特有指标
        peak_margin_ratio DECIMAL(10,4) DEFAULT 0,
        avg_margin_ratio DECIMAL(10,4) DEFAULT 0,
        warning_days INTEGER DEFAULT 0,
        liquidation_events INTEGER DEFAULT 0,
        total_commission DECIMAL(15,2) DEFAULT 0,
        avg_daily_pnl DECIMAL(15,2) DEFAULT 0,
        total_settlement_pnl DECIMAL(15,2) DEFAULT 0,
        max_margin_usage DECIMAL(10,4) DEFAULT 0,
        long_trades INTEGER DEFAULT 0,
        short_trades INTEGER DEFAULT 0,
        active_long_positions INTEGER DEFAULT 0,
        active_short_positions INTEGER DEFAULT 0,
        commission_rate DECIMAL(10,4) DEFAULT 0,
        avg_leverage DECIMAL(10,2) DEFAULT 1.0,
        margin_call_count INTEGER DEFAULT 0,
        net_settlement_pnl DECIMAL(15,2) DEFAULT 0,
        account_gross_pnl DECIMAL(15,2) DEFAULT 0,
        account_net_pnl DECIMAL(15,2) DEFAULT 0,
        account_net_win_days INTEGER DEFAULT 0,
        account_net_loss_days INTEGER DEFAULT 0,
        account_net_flat_days INTEGER DEFAULT 0,
        calmar_ratio DECIMAL(10,4) DEFAULT 0,
        return_drawdown_ratio DECIMAL(10,4) DEFAULT 0,
        profit_factor DECIMAL(10,4) DEFAULT 0,
        payoff_ratio DECIMAL(10,4) DEFAULT 0,
        trade_expectancy DECIMAL(15,2) DEFAULT 0,
        avg_win_pnl DECIMAL(15,2) DEFAULT 0,
        avg_loss_pnl DECIMAL(15,2) DEFAULT 0,
        max_trade_gain DECIMAL(15,2) DEFAULT 0,
        max_trade_loss DECIMAL(15,2) DEFAULT 0,
        max_consecutive_losing_trades INTEGER DEFAULT 0,
        max_consecutive_losing_days INTEGER DEFAULT 0,
        return_on_avg_margin DECIMAL(10,4) DEFAULT 0,
        commission_drag_ratio DECIMAL(10,4) DEFAULT 0,
        margin_cap_violation_days INTEGER DEFAULT 0,
        ticker_abs_contribution_top3_ratio DECIMAL(10,4) DEFAULT 0,
        profitable_ticker_count INTEGER DEFAULT 0,
        losing_ticker_count INTEGER DEFAULT 0,
        top_profit_ticker TEXT,
        top_profit_ticker_pnl DECIMAL(15,2) DEFAULT 0,
        worst_loss_ticker TEXT,
        worst_loss_ticker_pnl DECIMAL(15,2) DEFAULT 0,
        long_trade_net_pnl DECIMAL(15,2) DEFAULT 0,
        short_trade_net_pnl DECIMAL(15,2) DEFAULT 0,
        strategy_gross_pnl DECIMAL(15,2) DEFAULT 0,
        strategy_commission DECIMAL(15,2) DEFAULT 0,
        strategy_total_trades INTEGER DEFAULT 0,
        strategy_winning_trades INTEGER DEFAULT 0,
        strategy_losing_trades INTEGER DEFAULT 0,
        strategy_flat_trades INTEGER DEFAULT 0,
        strategy_win_rate DECIMAL(10,4) DEFAULT 0,
        strategy_net_pnl DECIMAL(15,2) DEFAULT 0,
        strategy_profit_factor DECIMAL(10,4) DEFAULT 0,
        trade_episode_memory_count INTEGER DEFAULT 0,
        exploratory_hypothesis_count INTEGER DEFAULT 0,
        learning_context_budget_rows INTEGER DEFAULT 0,
        learning_context_with_episode_rows INTEGER DEFAULT 0,
        learning_context_with_hypothesis_rows INTEGER DEFAULT 0,
        learning_context_with_memory_ratio DECIMAL(10,4) DEFAULT 0,
        avg_learning_context_chars DECIMAL(10,2) DEFAULT 0,
        FOREIGN KEY (config_id) REFERENCES config(id)
    )
    ''')

    # Create indices for better query performance
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_config_outcome_config_id ON config_outcome(config_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_config_outcome_updated_at ON config_outcome(updated_at)')

    conn.commit()
    conn.close()
    print(f"Config outcome table initialized at {DB_PATH}")

if __name__ == "__main__":
    init_evaluation_database()
