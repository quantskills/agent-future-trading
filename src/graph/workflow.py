from typing import Dict, Any
from langgraph.graph import StateGraph, START, END
from graph.schema import (
    FundState,
    Portfolio,
    FuturesDecision,
    Position,
    FuturesRecommendation,
    RecommendationAction,
    RecommendationSourceType,
    RecommendationStatus,
    TradingPhase,
)
from graph.constants import AgentKey
from agents.registry import AgentRegistry
from agents.planner import planner_agent
from tools.agent_tools.futures_execution import FuturesExecutionEngine
from apis.contract_info_cache import FuturesContractInfoCache
from util.db_helper import get_db
from util.logger import logger
from time import perf_counter
from apis.router import APISource, Router

class AgentWorkflow:
    """Trading Decision Workflow."""

    def __init__(self, config: Dict[str, Any], config_id: str):
        self.config = config  # Keep the full runtime config for downstream agents.
        self.llm_config = config['llm']
        self.tickers = config['tickers']
        self.exp_name = config['exp_name']
        self.trading_date = config['trading_date']
        self.market_type = config.get('market_type', 'china_futures')
        self.config_id = config_id
        self.db = get_db()
        if self.market_type != "china_futures":
            raise RuntimeError("AgentWorkflow now supports china_futures only.")
        self.router = Router(APISource.PANDAAI, market_type="china_futures", config=self.config)
        self.execution_engine = FuturesExecutionEngine(config, self.db)

        portfolio = self.db.get_latest_settled_portfolio(config_id)
        if not portfolio:
            raise RuntimeError(f"Failed to find settled portfolio for config {self.exp_name}")
        self.init_portfolio = Portfolio(**portfolio)
        logger.info(f"Portfolio ID: {self.init_portfolio.id}")
        
        # Initialize workflow configuration
        self.planner_mode = config.get('planner_mode', False)
        
        # Verify workflow analysts
        if not config.get('workflow_analysts'):
            raise ValueError("workflow_analysts must be provided in config")
            
        # Validate analysts and remove invalid ones
        self.workflow_analysts = config['workflow_analysts']
        invalid_analysts = [a for a in self.workflow_analysts if not AgentRegistry.check_agent_key(a)]
        if invalid_analysts:
            logger.warning(f"Invalid analyst keys removed: {invalid_analysts}")
            self.workflow_analysts = [a for a in self.workflow_analysts if a not in invalid_analysts]
            
        if not self.workflow_analysts:
            raise ValueError("No valid analysts remaining after validation")

    def build(self) -> StateGraph:
        """Build the workflow"""
        graph = StateGraph(FundState)
        market_type = self.config.get('market_type', 'china_futures')
        if market_type != "china_futures":
            raise RuntimeError("AgentWorkflow.build() now supports china_futures only.")
        from agents.portfolio_manager import portfolio_agent_futures
        portfolio_agent = portfolio_agent_futures
        graph.add_node(AgentKey.PORTFOLIO, portfolio_agent)

        # create node for each analyst and add edge
        for analyst in self.current_analysts:
            agent_func = AgentRegistry.get_agent_func_by_key(analyst)
            graph.add_node(analyst, agent_func)
            graph.add_edge(START, analyst)
            graph.add_edge(analyst, AgentKey.PORTFOLIO)

        graph.add_edge(AgentKey.PORTFOLIO, END)

        workflow = graph.compile()

        return workflow 
        

    def load_analysts(self, ticker: str):
        """
        Load the analysts for processing:
        - If planner_mode is True: use planner to select from verified workflow_analysts
        - If planner_mode is False: use all verified workflow_analysts
        """
        if self.planner_mode:
            logger.info("Using planner agent to select analysts from verified list")
            self.current_analysts = planner_agent(ticker, self.llm_config, self.workflow_analysts)
            if not self.current_analysts:
                raise ValueError("No analysts selected by planner")
        else:
            logger.info("Using all verified analysts")
            self.current_analysts = self.workflow_analysts.copy()
            
        logger.info(f"Active analysts for {ticker}: {self.current_analysts}")

    def _coerce_phase1_recommendation(self, ticker: str, portfolio: Portfolio, decision: FuturesDecision, morning_price_context, final_state: Dict[str, Any]) -> FuturesRecommendation:
        recommendation = final_state.get("recommendation")
        if recommendation:
            if isinstance(recommendation, FuturesRecommendation):
                return recommendation
            return FuturesRecommendation(**recommendation)

        trading_date_value = self.trading_date.strftime("%Y-%m-%d") if hasattr(self.trading_date, "strftime") else str(self.trading_date)
        warning_message = morning_price_context.warning_message if morning_price_context else None
        status = RecommendationStatus.PENDING
        action = RecommendationAction.HOLD
        lots = 0
        contract_code = getattr(decision, "contract_code", None)

        if decision is not None:
            action_map = {
                "open_long": RecommendationAction.OPEN_LONG,
                "open_short": RecommendationAction.OPEN_SHORT,
                "close_long": RecommendationAction.CLOSE_LONG,
                "close_short": RecommendationAction.CLOSE_SHORT,
                "hold": RecommendationAction.HOLD,
            }
            decision_action = getattr(decision.action, "value", decision.action)
            action = action_map.get(str(decision_action), RecommendationAction.HOLD)
            lots = getattr(decision, "lots", 0)

        if morning_price_context is None or morning_price_context.base_price is None:
            status = RecommendationStatus.SKIPPED
            action = RecommendationAction.HOLD
            lots = 0

        return FuturesRecommendation(
            config_id=self.config_id,
            reference_portfolio_id=portfolio.id,
            trading_date=trading_date_value,
            effective_trade_date=trading_date_value,
            source_type=RecommendationSourceType.STRATEGY,
            underlying_code=ticker,
            contract_code=contract_code,
            action=action,
            lots=lots,
            base_price=morning_price_context.base_price if morning_price_context else None,
            base_price_source=morning_price_context.base_price_source if morning_price_context else None,
            base_price_date=morning_price_context.base_price_date if morning_price_context else None,
            open_price=morning_price_context.open_price if morning_price_context else None,
            prev_close_price=morning_price_context.prev_close_price if morning_price_context else None,
            slippage_model=self.config.get("execution", {}).get("slippage_model", "tick"),
            slippage_ticks=None,
            slippage_amount=None,
            execution_price=None,
            justification=getattr(decision, "justification", "") if decision else "",
            signal_snapshot={},
            warning_message=warning_message,
            status=status,
        )

    def _build_futures_phase1_state(self, ticker: str, portfolio: Portfolio, morning_price_context):
        return FundState(
            ticker=ticker,
            exp_name=self.exp_name,
            config_id=self.config_id,
            trading_date=self.trading_date,
            llm_config=self.llm_config,
            portfolio=portfolio,
            num_tickers=len(self.tickers),
            market_type="china_futures",
            enabled_analysts=self.current_analysts.copy(),
            phase=TradingPhase.PHASE1,
            morning_price_context=morning_price_context,
            pre_open_only=True,
            info_cutoff="pre_open",
            recommendation=None,
            config={
                'max_total_margin_ratio': self.config.get('max_total_margin_ratio', 0.20),
                'max_single_margin_ratio': self.config.get('max_single_margin_ratio', 0.12),
            },
            full_config=self.config,
            router=self.router,
        )

    def _apply_virtual_pending_rollovers(self, portfolio: Portfolio) -> Portfolio:
        recommendations = self.db.get_futures_recommendations_by_effective_date(
            config_id=self.config_id,
            effective_trade_date=self.trading_date,
            source_type=RecommendationSourceType.ROLLOVER,
            status=RecommendationStatus.PENDING,
        )

        if recommendations:
            logger.info(
                f"Applying {len(recommendations)} pending rollover recommendation(s) "
                "to the pre-open planning portfolio"
            )

        for recommendation in recommendations:
            ticker = recommendation.get("underlying_code")
            to_contract = recommendation.get("to_contract")
            position = portfolio.positions.get(ticker)
            if position is None or position.shares == 0 or not to_contract:
                continue
            position.contract_code = to_contract

        return portfolio

    def _apply_virtual_recommendation_to_portfolio(self, portfolio: Portfolio, recommendation: FuturesRecommendation) -> Portfolio:
        if recommendation.status == RecommendationStatus.SKIPPED:
            return portfolio

        signal_snapshot = recommendation.signal_snapshot or {}
        pre_open_plan = signal_snapshot.get("pre_open_plan") if isinstance(signal_snapshot, dict) else None
        ticker = recommendation.underlying_code

        contract_info = FuturesContractInfoCache.get_contract_info(ticker)
        if not contract_info:
            return portfolio

        if ticker not in portfolio.positions:
            portfolio.positions[ticker] = Position(shares=0, value=0)

        position = portfolio.positions[ticker]
        position.contract_multiplier = contract_info.get("contract_multiplier")
        reference_price = float(getattr(recommendation, "base_price", None) or 0.0)
        target_lots = None

        if pre_open_plan:
            target_lots = int(pre_open_plan.get("target_lots_estimate") or 0)
            reference_price = float(pre_open_plan.get("reference_price") or reference_price)
        else:
            current_shares = int(getattr(position, "shares", 0) or 0)
            action = getattr(recommendation.action, "value", recommendation.action)
            lots = int(recommendation.lots or 0)
            if action == RecommendationAction.CLOSE_LONG.value:
                target_lots = max(0, current_shares - lots)
            elif action == RecommendationAction.CLOSE_SHORT.value:
                target_lots = min(0, current_shares + lots)
            elif action == RecommendationAction.OPEN_LONG.value:
                target_lots = lots
            elif action == RecommendationAction.OPEN_SHORT.value:
                target_lots = -lots
            else:
                target_lots = current_shares

        if reference_price <= 0:
            return portfolio

        position.shares = target_lots
        if target_lots == 0:
            position.value = 0.0
            position.margin_used = 0.0
            position.entry_price = None
            position.unrealized_pnl = 0.0
            return portfolio

        margin_rate = (
            contract_info.get("margin_rate_long")
            if target_lots > 0 else contract_info.get("margin_rate_short")
        )
        position.entry_price = reference_price
        position.margin_rate = margin_rate
        position.value = abs(target_lots) * reference_price * contract_info["contract_multiplier"]
        position.margin_used = position.value * margin_rate
        portfolio.margin_used = sum(getattr(pos, "margin_used", 0.0) for pos in portfolio.positions.values())
        portfolio.margin_available = portfolio.cashflow - portfolio.margin_used
        denominator = portfolio.cashflow + sum(getattr(pos, "value", 0.0) for pos in portfolio.positions.values())
        portfolio.margin_ratio = portfolio.margin_used / denominator if denominator > 0 else 0.0
        return portfolio

    def _run_futures_phase1(self) -> float:
        start_time = perf_counter()
        portfolio = self.init_portfolio

        if not hasattr(portfolio, 'margin_used'):
            portfolio.margin_used = 0
        if not hasattr(portfolio, 'margin_available'):
            portfolio.margin_available = portfolio.cashflow - portfolio.margin_used
        if not hasattr(portfolio, 'margin_ratio'):
            total_value = portfolio.cashflow + sum(p.value for p in portfolio.positions.values())
            portfolio.margin_ratio = portfolio.margin_used / total_value if total_value > 0 else 0
        if not hasattr(portfolio, 'risk_status'):
            portfolio.risk_status = "NORMAL"
        if not hasattr(portfolio, 'last_settle_date'):
            portfolio.last_settle_date = None
        if not hasattr(portfolio, 'is_settled'):
            portfolio.is_settled = False

        portfolio = self._apply_virtual_pending_rollovers(portfolio)

        for ticker in self.tickers:
            self.load_analysts(ticker)
            morning_price_context = self.router.resolve_pre_open_reference_price(
                underlying_code=ticker,
                trading_date=self.trading_date,
            )
            if morning_price_context is None or morning_price_context.base_price is None:
                recommendation = self._coerce_phase1_recommendation(
                    ticker=ticker,
                    portfolio=portfolio,
                    decision=None,
                    morning_price_context=morning_price_context,
                    final_state={},
                )
                recommendation_id = self.db.save_futures_recommendation(recommendation)
                if not recommendation_id:
                    raise RuntimeError(f"Failed to save futures recommendation for {ticker}")
                logger.warning(f"{ticker} phase1 skipped: {recommendation.warning_message}")
                logger.log_portfolio(f"{ticker} phase1 position update", portfolio)
                if self.planner_mode:
                    self.current_analysts = None
                continue

            state = self._build_futures_phase1_state(ticker, portfolio, morning_price_context)

            workflow = self.build()
            logger.info(f"{ticker} phase1 workflow compiled successfully")
            try:
                final_state = workflow.invoke(state)
            except Exception as e:
                logger.error(f"Error running futures phase1 workflow: {e}")
                raise RuntimeError(f"Failed to generate futures phase1 recommendation for {ticker}")

            decision = final_state.get("decision")
            recommendation = self._coerce_phase1_recommendation(
                ticker=ticker,
                portfolio=portfolio,
                decision=decision,
                morning_price_context=morning_price_context,
                final_state=final_state,
            )
            recommendation_id = self.db.save_futures_recommendation(recommendation)
            if not recommendation_id:
                raise RuntimeError(f"Failed to save futures recommendation for {ticker}")

            if recommendation.status != RecommendationStatus.SKIPPED:
                portfolio = self._apply_virtual_recommendation_to_portfolio(portfolio, recommendation)

            logger.log_portfolio(f"{ticker} phase1 position update", portfolio)

            if self.planner_mode:
                self.current_analysts = None

        logger.log_portfolio("Phase1 Intraday Portfolio", portfolio)
        return perf_counter() - start_time
    
    def run(self, config_id: str) -> float:
        """Run the workflow."""
        market_type = self.config.get('market_type', 'china_futures')
        if market_type != "china_futures":
            raise RuntimeError("AgentWorkflow.run() now supports china_futures only.")
        return self._run_futures_phase1()

