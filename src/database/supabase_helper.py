import os
from datetime import datetime, timezone
from typing import Dict, List, Optional
from graph.schema import AnalystSignal
from database.interface import BaseDB
from supabase import create_client
from util.logger import logger

class SupabaseDB(BaseDB):
    def __init__(self):
        # Supabase configuration
        self.url = os.getenv("SUPABASE_URL")
        self.key = os.getenv("SUPABASE_KEY")

        if not self.url or not self.key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env file")

        self.client = create_client(self.url, self.key)


    def get_config(self, config_id: str) -> Optional[Dict]:
        """Get config by id."""
        try:
            response = self.client.table('config').select('*').eq('id', config_id).execute()
            return response.data[0] if response.data else None  
        except Exception as e:
            logger.error(f"Config not found: {e}")
            return None

    def get_config_id_by_name(self, exp_name: str) -> Optional[str]:
        """Get config id by experiment name."""
        try:
            response = self.client.table('config') \
                .select('id') \
                .eq('exp_name', exp_name) \
                .execute()

            if response.data and len(response.data) > 0:
                return response.data[0]['id']
            return None
        except Exception as e:
            logger.error(f"Config not found: {e}")
            return None

    def delete_config_and_portfolios(self, config_id: str) -> bool:
        """Delete a config and all its associated data."""
        try:
            # Supabase handles cascading deletes if foreign keys are set up
            # This is a placeholder implementation
            logger.warning("delete_config_and_portfolios not fully implemented for Supabase")
            return False
        except Exception as e:
            logger.error(f"Error deleting config: {e}")
            return False

    def create_config(self, config: Dict) -> Optional[str]:
        """Create a new config entry."""
        try:
            data = {
                'exp_name': config['exp_name'],
                'tickers': config['tickers'],
                'has_planner': config['planner_mode'],
                'llm_model': config['llm']['model'],
                'llm_provider': config['llm']['provider'],
            }
            
            response = self.client.table('config').insert(data).execute()
            
            if response.data and len(response.data) > 0:
                return response.data[0]['id']
            return None
        except Exception as e:
            logger.error(f"Error creating config: {e}")
            return None

    def get_latest_trading_date(self, config_id: str) -> Optional[datetime]:
        """Get the latest trading date for a config."""
        try:
            response = self.client.table('portfolio') \
                .select('trading_date') \
                .eq('config_id', config_id) \
                .not_.is_("trading_date", None) \
                .order('updated_at', desc=True) \
                .execute()
        
            if response.data and len(response.data) > 0:
                dt = datetime.fromisoformat(response.data[0]['trading_date'])
                return dt.replace(tzinfo=None)
            return None
        except Exception as e:
            logger.error(f"Error getting latest trading date: {e}")
            return None

    def get_latest_portfolio(self, config_id: str) -> Optional[Dict]:
        """Get the latest portfolio for a config."""
        try:
            response = self.client.table('portfolio') \
                .select('id, cashflow, positions') \
                .eq('config_id', config_id) \
                .not_.is_("trading_date", None) \
                .order('updated_at', desc=True) \
                .limit(1) \
                .execute()
            
            if response.data and len(response.data) > 0:
                portfolio = response.data[0]
                return {
                    'id': portfolio['id'],
                    'cashflow': float(portfolio['cashflow']),  # Convert Decimal to float
                    'positions': portfolio['positions']  # Already JSON in Supabase
                }
            return None
        except Exception as e:
            logger.error(f"Portfolio not found: {e}")
            return None

    def create_portfolio(self, config_id: str, cashflow: float, trading_date: datetime) -> Optional[Dict]:
        """Create a new portfolio."""
        try:
            data = {
                'config_id': config_id,
                'cashflow': cashflow,
                'total_assets': cashflow,
                'positions': {},
                'trading_date': trading_date.isoformat()
            }
            
            response = self.client.table('portfolio').insert(data).execute()
            if response.data and len(response.data) > 0:
                portfolio = response.data[0]
                return {
                    'id': portfolio['id'],  
                    'cashflow': float(portfolio['cashflow']),  # Convert Decimal to float
                    'positions': {},
                }
            return None
        except Exception as e:
            logger.error(f"Error creating portfolio: {e}")
            return None
        
    def copy_portfolio(self, config_id: str, portfolio: Dict, trading_date: datetime) -> Optional[Dict]:
        """Copy a portfolio."""
        try:
            total_assets = portfolio['cashflow'] + sum(position['value'] for position in portfolio['positions'].values())
            data = {
                'config_id': config_id,
                'trading_date': trading_date.isoformat(),
                'cashflow': portfolio['cashflow'],
                'total_assets': total_assets,
                'positions': portfolio['positions']
            }

            response = self.client.table('portfolio').insert(data).execute()
            if response.data and len(response.data) > 0:
                return response.data[0]
            return None
        except Exception as e:
            logger.error(f"Error copying portfolio: {e}")
            return None

    def get_or_create_portfolio_for_date(self, config_id: str, portfolio: Dict, trading_date: datetime) -> Optional[Dict]:
        """
        Get existing portfolio for the given trading date, or create a new one based on the latest portfolio.

        This method prevents duplicate portfolio records for the same trading date.
        """
        try:
            # 首先检查是否已存在该交易日期的 portfolio 记录
            response = self.client.table('portfolio') \
                .select('id, cashflow, positions') \
                .eq('config_id', config_id) \
                .eq('trading_date', trading_date.isoformat()) \
                .order('updated_at', desc=True) \
                .limit(1) \
                .execute()

            if response.data and len(response.data) > 0:
                # 已存在该日期的记录，返回它用于更新
                existing = response.data[0]
                logger.info(f"Found existing portfolio {existing['id'][:8]}... for trading date {trading_date.isoformat()}")
                return {
                    'id': existing['id'],
                    'cashflow': float(existing['cashflow']),
                    'positions': existing['positions'] if existing['positions'] else {}
                }

            # 不存在，则创建新的 portfolio 记录
            total_assets = portfolio['cashflow'] + sum(position['value'] for position in portfolio['positions'].values())
            data = {
                'config_id': config_id,
                'trading_date': trading_date.isoformat(),
                'cashflow': portfolio['cashflow'],
                'total_assets': total_assets,
                'positions': portfolio['positions']
            }

            response = self.client.table('portfolio').insert(data).execute()
            if response.data and len(response.data) > 0:
                new_portfolio = response.data[0]
                logger.info(f"Created new portfolio {new_portfolio['id'][:8]}... for trading date {trading_date.isoformat()}")
                return {
                    'id': new_portfolio['id'],
                    'cashflow': float(new_portfolio['cashflow']),
                    'positions': new_portfolio['positions'] if new_portfolio['positions'] else {}
                }
            return None
        except Exception as e:
            logger.error(f"Error in get_or_create_portfolio_for_date: {e}")
            return None

    def update_portfolio(self, config_id: str, portfolio: Dict, trading_date: datetime) -> bool:
        """Update portfolio."""
        try:
            total_assets = portfolio['cashflow'] + sum(position['value'] for position in portfolio['positions'].values())
            data = {
                'config_id': config_id,
                'updated_at': datetime.now(timezone.utc).isoformat(),
                'trading_date': trading_date.isoformat(),
                'cashflow': portfolio['cashflow'],
                'total_assets': total_assets,
                'positions': portfolio['positions']
            }
            
            response = self.client.table('portfolio').update(data).eq('id', portfolio['id']).execute()
            
            return bool(response.data)
        except Exception as e:
            logger.error(f"Error updating portfolio: {e}")
            return False

    def save_signal(self, portfolio_id: str, analyst: str, ticker: str, prompt: str, signal: AnalystSignal) -> Optional[str]:
        """Save a new signal."""
        try:
            data = {
                'portfolio_id': portfolio_id,
                'ticker': ticker,
                'llm_prompt': prompt,
                'analyst': analyst,
                'signal': str(signal.signal),
                'justification': signal.justification,
                'artifact_json': signal.model_dump() if hasattr(signal, "model_dump") else {},
                'business_quality_score': float(getattr(signal, "business_quality_score", 0.0) or 0.0),
                'horizon_class': str(getattr(signal, "horizon_class", "unknown") or "unknown"),
                'template_name': str(getattr(signal, "template_name", "unknown") or "unknown"),
            }
            
            response = self.client.table('signal').insert(data).execute()
            
            if response.data and len(response.data) > 0:
                return response.data[0]['id']
            return None
        except Exception as e:
            logger.error(f"Error saving signal: {e}")
            return None

# Initialize global instance
# db = SupabaseDB() 
