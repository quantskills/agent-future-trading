from typing import Dict, Any
from langgraph.graph import StateGraph, START, END
from typing import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from agents.control_team.planner import planner_agent
from tools.agent_tools.execution.futures_execution import FuturesExecutionEngine
from tools.agent_tools.analysis.quality import write_analyst_report
from tools.agent_tools.analysis.data_usage import prefetch_local_daily_data, prefetch_pandaai_daily_data
from apis.contract_info_cache import FuturesContractInfoCache
from util.db_helper import get_db
from util.logger import logger
from time import perf_counter
from apis.router import APISource, Router
from tools.agent_tools.analysis.learning_context import clear_learning_context_cache

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
        self.runtime_cfg = config.get("runtime", {}) or {}
        self.phase1_runtime_cfg = self.runtime_cfg.get("phase1", {}) or {}
        self.allow_analyst_db_writes = bool(
            self.phase1_runtime_cfg.get("allow_parallel_analyst_db_writes", False)
        )
        self._compiled_workflows: Dict[tuple[str, ...], Callable] = {}

    def _phase1_acceleration_enabled(self) -> bool:
        return bool(self.phase1_runtime_cfg.get("enable_analysis_parallelism", True))

    def _phase1_prefetch_enabled(self) -> bool:
        return bool(self.phase1_runtime_cfg.get("prefetch_pre_open_reference_prices", True))

    def _phase1_timing_enabled(self) -> bool:
        return bool(self.phase1_runtime_cfg.get("log_timing_breakdown", True))

    def _phase1_max_workers(self) -> int:
        configured = self.phase1_runtime_cfg.get("max_parallel_analysis_tickers")
        try:
            max_workers = int(configured)
        except (TypeError, ValueError):
            max_workers = 0
        if max_workers <= 0:
            max_workers = min(len(self.tickers), 4)
        return max(1, min(max_workers, len(self.tickers)))

    def _timed_call(self, timings: Dict[str, float], label: str, func: Callable, *args, **kwargs):
        started_at = perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            timings[label] = timings.get(label, 0.0) + (perf_counter() - started_at)

    def _save_prefetched_analyst_outputs(self, analysis_state: Dict[str, Any]) -> None:
        if self.allow_analyst_db_writes:
            return
        portfolio = analysis_state.get("portfolio")
        portfolio_id = getattr(portfolio, "id", "") if portfolio is not None else ""
        ticker = analysis_state.get("ticker")
        outputs_by_agent = {
            str(output.get("analyst")): output
            for output in analysis_state.get("analyst_outputs", []) or []
            if isinstance(output, dict)
        }
        for signal in analysis_state.get("analyst_signals", []) or []:
            output = outputs_by_agent.get(getattr(signal, "agent_name", ""))
            prompt = output.get("prompt") if output else ""
            if output:
                report_path = write_analyst_report(
                    analyst=output.get("analyst") or getattr(signal, "agent_name", "unknown"),
                    ticker=output.get("ticker") or ticker,
                    trading_date=output.get("trading_date") or analysis_state.get("trading_date"),
                    signal=signal,
                    full_config=analysis_state.get("full_config") or self.config,
                    sections=output.get("report_sections") or {},
                )
                if report_path:
                    metadata = getattr(signal, "metadata", {}) or {}
                    metadata["decision_report_path"] = report_path
                    signal.metadata = metadata
            signal_id = self.db.save_signal(
                portfolio_id,
                getattr(signal, "agent_name", "unknown"),
                ticker,
                prompt or "[parallel phase1 analysis prompt unavailable]",
                signal,
            )
            if signal_id:
                metadata = getattr(signal, "metadata", {}) or {}
                metadata["signal_record_id"] = signal_id
                metadata["parallel_phase1_saved_by"] = "AgentWorkflow"
                signal.metadata = metadata

    def _get_compiled_workflow(self, analysts: list[str]):
        key = tuple(analysts)
        workflow = self._compiled_workflows.get(key)
        if workflow is not None:
            return workflow
        previous_analysts = getattr(self, "current_analysts", None)
        self.current_analysts = list(analysts)
        workflow = self.build()
        self.current_analysts = previous_analysts
        self._compiled_workflows[key] = workflow
        return workflow

    def build(self) -> StateGraph:
        """Build the workflow"""
        graph = StateGraph(FundState)
        market_type = self.config.get('market_type', 'china_futures')
        if market_type != "china_futures":
            raise RuntimeError("AgentWorkflow.build() now supports china_futures only.")
        from agents.decision_team.portfolio_manager import portfolio_agent_futures
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

    def _build_futures_phase1_analysis_state(
        self,
        ticker: str,
        portfolio: Portfolio,
        morning_price_context,
        analysts: list[str],
    ):
        state = self._build_futures_phase1_state(ticker, portfolio, morning_price_context)
        state["enabled_analysts"] = list(analysts)
        return state

    def _run_phase1_analysis_only(
        self,
        ticker: str,
        portfolio: Portfolio,
        morning_price_context,
        analysts: list[str],
    ) -> Dict[str, Any]:
        state = self._build_futures_phase1_analysis_state(
            ticker=ticker,
            portfolio=portfolio,
            morning_price_context=morning_price_context,
            analysts=analysts,
        )
        state["portfolio"] = portfolio
        analyst_signals = []
        analyst_outputs = []
        started_at = perf_counter()
        with ThreadPoolExecutor(max_workers=max(1, len(analysts))) as executor:
            futures = {
                executor.submit(AgentRegistry.get_agent_func_by_key(analyst), state): analyst
                for analyst in analysts
            }
            for future in as_completed(futures):
                analyst = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.error(f"{ticker}: {analyst} phase1 analysis failed: {exc}")
                    raise RuntimeError(f"Failed to run {analyst} analysis for {ticker}") from exc
                analyst_signals.extend((result or {}).get("analyst_signals", []) or [])
                analyst_outputs.extend((result or {}).get("analyst_outputs", []) or [])

        order = {name: idx for idx, name in enumerate(analysts)}
        analyst_signals.sort(key=lambda signal: order.get(getattr(signal, "agent_name", ""), 999))
        logger.info(
            f"{ticker} phase1 analyst fanout completed: analysts={analysts}, "
            f"signals={len(analyst_signals)}, elapsed={perf_counter() - started_at:.2f}s"
        )
        return {
            "ticker": ticker,
            "exp_name": self.exp_name,
            "config_id": self.config_id,
            "trading_date": self.trading_date,
            "llm_config": self.llm_config,
            "market_type": "china_futures",
            "enabled_analysts": list(analysts),
            "phase": TradingPhase.PHASE1,
            "morning_price_context": morning_price_context,
            "pre_open_only": True,
            "info_cutoff": "pre_open",
            "num_tickers": len(self.tickers),
            "save_analyst_outputs": self.allow_analyst_db_writes,
            "config": {
                'max_total_margin_ratio': self.config.get('max_total_margin_ratio', 0.20),
                'max_single_margin_ratio': self.config.get('max_single_margin_ratio', 0.12),
            },
            "full_config": self.config,
            "router": self.router,
            "portfolio": portfolio,
            "analyst_signals": analyst_signals,
            "analyst_outputs": analyst_outputs,
        }

    def _run_phase1_portfolio_only(self, analysis_state: Dict[str, Any], portfolio: Portfolio) -> Dict[str, Any]:
        from agents.decision_team.portfolio_manager import portfolio_agent_futures

        state = dict(analysis_state)
        state["portfolio"] = portfolio
        state["num_tickers"] = len(self.tickers)
        state["decision"] = None
        state["recommendation"] = None
        return portfolio_agent_futures(state)

    def _prefetch_pre_open_reference_prices(self, timings: Dict[str, float]) -> Dict[str, Any]:
        if not self._phase1_prefetch_enabled() or len(self.tickers) <= 1:
            contexts = {}
            for ticker in self.tickers:
                contexts[ticker] = self._timed_call(
                    timings,
                    "pre_open_reference_price",
                    self.router.resolve_pre_open_reference_price,
                    underlying_code=ticker,
                    trading_date=self.trading_date,
                )
            return contexts

        started_at = perf_counter()
        contexts: Dict[str, Any] = {}
        max_workers = self._phase1_max_workers()
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self.router.resolve_pre_open_reference_price,
                    underlying_code=ticker,
                    trading_date=self.trading_date,
                ): ticker
                for ticker in self.tickers
            }
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    contexts[ticker] = future.result()
                except Exception as exc:
                    logger.error(f"{ticker}: pre-open reference price prefetch failed: {exc}")
                    contexts[ticker] = None
        timings["pre_open_reference_price_prefetch"] = timings.get("pre_open_reference_price_prefetch", 0.0) + (
            perf_counter() - started_at
        )
        return contexts

    def _prefetch_local_daily_data(self, timings: Dict[str, float]) -> Dict[str, Any]:
        started_at = perf_counter()
        result = prefetch_local_daily_data(self.config, self.tickers)
        timings["local_daily_data_prefetch"] = timings.get("local_daily_data_prefetch", 0.0) + (
            perf_counter() - started_at
        )
        if result.get("enabled"):
            logger.info(
                "Local daily data cache prefetch completed: "
                f"finoview={result.get('finoview_files_loaded')}, "
                f"news={result.get('news_files_loaded')}, "
                f"failed={result.get('finoview_files_failed', 0) + result.get('news_files_failed', 0)}"
            )
        return result

    def _prefetch_pandaai_daily_data(self, timings: Dict[str, float]) -> Dict[str, Any]:
        started_at = perf_counter()
        result = prefetch_pandaai_daily_data(self.router, self.config, self.tickers, self.trading_date)
        timings["pandaai_daily_data_prefetch"] = timings.get("pandaai_daily_data_prefetch", 0.0) + (
            perf_counter() - started_at
        )
        if result.get("enabled"):
            logger.info(
                "PandaAI daily data cache prefetch completed: "
                f"market={result.get('market_requests')}, extra={result.get('extra_requests')}, "
                f"failed={result.get('market_failed', 0) + result.get('extra_failed', 0)}"
            )
        return result

    def _prefetch_phase1_analysis(
        self,
        portfolio: Portfolio,
        morning_contexts: Dict[str, Any],
        timings: Dict[str, float],
    ) -> Dict[str, Dict[str, Any]]:
        if not self._phase1_acceleration_enabled() or self.planner_mode or len(self.tickers) <= 1:
            return {}

        max_workers = self._phase1_max_workers()
        analysis_results: Dict[str, Dict[str, Any]] = {}
        started_at = perf_counter()
        self.current_analysts = self.workflow_analysts.copy()
        logger.info(
            f"Phase1 analysis prefetch enabled: tickers={len(self.tickers)}, max_workers={max_workers}, "
            f"analysts={self.workflow_analysts}"
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for ticker in self.tickers:
                morning_price_context = morning_contexts.get(ticker)
                if morning_price_context is None or morning_price_context.base_price is None:
                    continue
                futures[
                    executor.submit(
                        self._run_phase1_analysis_only,
                        ticker,
                        portfolio.model_copy(deep=True),
                        morning_price_context,
                        self.workflow_analysts.copy(),
                    )
                ] = ticker
            for future in as_completed(futures):
                ticker = futures[future]
                analysis_results[ticker] = future.result()
        timings["analysis_prefetch"] = timings.get("analysis_prefetch", 0.0) + (perf_counter() - started_at)
        if not self.allow_analyst_db_writes:
            logger.info(
                "Phase1 parallel analyst DB/report writes are centralized after prefetch "
                "to avoid SQLite write contention and duplicate artifacts."
            )
        return analysis_results

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
            return self._refresh_portfolio_account_fields(portfolio)
            return portfolio

        margin_rate = (
            contract_info.get("margin_rate_long")
            if target_lots > 0 else contract_info.get("margin_rate_short")
        )
        position.entry_price = reference_price
        position.margin_rate = margin_rate
        position.value = abs(target_lots) * reference_price * contract_info["contract_multiplier"]
        position.margin_used = position.value * margin_rate
        return self._refresh_portfolio_account_fields(portfolio)

    def _refresh_portfolio_account_fields(self, portfolio: Portfolio) -> Portfolio:
        portfolio.margin_used = sum(getattr(pos, "margin_used", 0.0) for pos in portfolio.positions.values())
        portfolio.account_equity = getattr(portfolio, "account_equity", 0.0) or (
            portfolio.cashflow + portfolio.margin_used
        )
        portfolio.cash_available = portfolio.account_equity - portfolio.margin_used
        portfolio.margin_available = portfolio.cash_available
        denominator = portfolio.account_equity
        portfolio.margin_ratio = portfolio.margin_used / denominator if denominator > 0 else 0.0
        return portfolio

    def _run_futures_phase1(self) -> float:
        start_time = perf_counter()
        timings: Dict[str, float] = {}
        clear_learning_context_cache()
        portfolio = self.init_portfolio

        if not hasattr(portfolio, 'margin_used'):
            portfolio.margin_used = 0
        portfolio.cash_available = getattr(portfolio, "cash_available", portfolio.cashflow)
        portfolio.account_equity = getattr(portfolio, "account_equity", 0.0) or (
            portfolio.cashflow + portfolio.margin_used
        )
        if not hasattr(portfolio, 'margin_available'):
            portfolio.margin_available = portfolio.cash_available
        if not hasattr(portfolio, 'margin_ratio'):
            total_value = portfolio.account_equity
            portfolio.margin_ratio = portfolio.margin_used / total_value if total_value > 0 else 0
        if not hasattr(portfolio, 'risk_status'):
            portfolio.risk_status = "NORMAL"
        if not hasattr(portfolio, 'last_settle_date'):
            portfolio.last_settle_date = None
        if not hasattr(portfolio, 'is_settled'):
            portfolio.is_settled = False

        portfolio = self._timed_call(
            timings,
            "pending_rollovers",
            self._apply_virtual_pending_rollovers,
            portfolio,
        )
        self._prefetch_local_daily_data(timings)
        self._prefetch_pandaai_daily_data(timings)
        morning_contexts = self._prefetch_pre_open_reference_prices(timings)
        prefetched_analysis = self._prefetch_phase1_analysis(portfolio, morning_contexts, timings)

        for ticker in self.tickers:
            ticker_started_at = perf_counter()
            self._timed_call(timings, "load_analysts", self.load_analysts, ticker)
            morning_price_context = morning_contexts.get(ticker)
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
                elif prefetched_analysis:
                    self.current_analysts = self.workflow_analysts.copy()
                timings[f"{ticker}.total"] = perf_counter() - ticker_started_at
                continue

            try:
                if ticker in prefetched_analysis:
                    self._timed_call(
                        timings,
                        "save_prefetched_analyst_outputs",
                        self._save_prefetched_analyst_outputs,
                        prefetched_analysis[ticker],
                    )
                    final_state = self._timed_call(
                        timings,
                        "portfolio_manager",
                        self._run_phase1_portfolio_only,
                        prefetched_analysis[ticker],
                        portfolio,
                    )
                else:
                    state = self._build_futures_phase1_state(ticker, portfolio, morning_price_context)
                    workflow = self._timed_call(
                        timings,
                        "workflow_compile_or_reuse",
                        self._get_compiled_workflow,
                        self.current_analysts,
                    )
                    logger.info(f"{ticker} phase1 workflow ready")
                    final_state = self._timed_call(timings, "workflow_invoke", workflow.invoke, state)
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
            elif prefetched_analysis:
                self.current_analysts = self.workflow_analysts.copy()
            timings[f"{ticker}.total"] = perf_counter() - ticker_started_at

        logger.log_portfolio("Phase1 Intraday Portfolio", portfolio)
        elapsed = perf_counter() - start_time
        if self._phase1_timing_enabled():
            summary_keys = [key for key in sorted(timings) if not key.endswith(".total")]
            timing_summary = ", ".join(f"{key}={timings[key]:.2f}s" for key in summary_keys)
            ticker_summary = ", ".join(
                f"{key[:-6]}={timings[key]:.2f}s" for key in sorted(timings) if key.endswith(".total")
            )
            logger.info(f"Phase1 timing summary: total={elapsed:.2f}s | {timing_summary}")
            logger.info(f"Phase1 ticker timing summary: {ticker_summary}")
        return elapsed
    
    def run(self, config_id: str) -> float:
        """Run the workflow."""
        market_type = self.config.get('market_type', 'china_futures')
        if market_type != "china_futures":
            raise RuntimeError("AgentWorkflow.run() now supports china_futures only.")
        return self._run_futures_phase1()

