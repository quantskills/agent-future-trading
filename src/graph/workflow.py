from typing import Dict, Any
from langgraph.graph import StateGraph, START, END
from typing import Callable
from typing import List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from graph.schema import (
    FundState,
    Portfolio,
    Position,
    FuturesRecommendation,
    RecommendationSourceType,
    RecommendationStatus,
    TradingPhase,
)
from graph.constants import AgentKey
from agents.registry import AgentRegistry
from tools.agent_tools.execution.trader_futures_execution import FuturesExecutionEngine
from tools.agent_tools.analysis.analyst_quality import write_analyst_report
from tools.agent_tools.analysis.analyst_data_usage import prefetch_local_daily_data, prefetch_pandaai_daily_data
from apis.contract_info_cache import FuturesContractInfoCache
from util.db_helper import get_db
from util.logger import logger
from time import perf_counter
from apis.router import APISource, Router
from tools.agent_tools.analysis.analyst_learning_context import clear_learning_context_cache
from agents.decision_team.auditor import audit_futures_recommendation

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
        
        if config.get('planner_mode', False):
            raise RuntimeError(
                "planner_mode is disabled by the fixed multi-agent workflow; "
                "use workflow_analysts and signal_collector instead."
            )
        self.planner_mode = False
        
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

    def _safe_positive_ratio(self, value, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    def _phase1_compat_config(self) -> Dict[str, Any]:
        """Expose legacy Phase1 sizing keys from the current unified config.

        Some analyst/PM state consumers still read ``state["config"]`` for
        historical compatibility. Keep those values aligned with the canonical
        runtime config instead of hard-coded defaults.
        """
        capital_control = self.config.get("capital_utilization_control", {}) or {}
        hard_total = self._safe_positive_ratio(self.config.get("max_total_margin_ratio"), 0.20)
        learned_total = self._safe_positive_ratio(
            capital_control.get("max_margin_ratio_after_scaling"),
            hard_total,
        )
        risk_caps = (self.config.get("risk_control", {}) or {}).get("max_single_position_ratio", {}) or {}
        safe_single_anchor = self._safe_positive_ratio(risk_caps.get("safe"), 0.15)
        budget_policy = self.config.get("position_budget_policy", {}) or {}
        single_ticker_margin_cap = self._safe_positive_ratio(
            budget_policy.get("max_single_ticker_margin_ratio"),
            safe_single_anchor,
        )
        return {
            "max_total_margin_ratio": min(hard_total, learned_total),
            "max_single_margin_ratio": safe_single_anchor,
            "max_single_ticker_margin_ratio": single_ticker_margin_cap,
            "position_budget_policy": budget_policy,
            "capital_utilization_control": capital_control,
        }

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

    def _validate_phase1_signal_persistence(self, portfolio: Portfolio, tickers: list[str]) -> None:
        expected_pairs = len(tickers) * len(self.workflow_analysts)
        if expected_pairs <= 0:
            return
        if not hasattr(self.db, "get_signal_persistence_counts"):
            raise RuntimeError("Database does not expose signal persistence verification")
        counts = self.db.get_signal_persistence_counts(
            portfolio.id,
            tickers=tickers,
            analysts=self.workflow_analysts,
        )
        rows = counts.get("rows") or []
        db_pairs = {
            (str(row.get("ticker") or "").upper(), str(row.get("analyst") or ""))
            for row in rows
        }
        expected = {
            (str(ticker).upper(), str(analyst))
            for ticker in tickers
            for analyst in self.workflow_analysts
        }
        missing = sorted(f"{ticker}:{analyst}" for ticker, analyst in expected - db_pairs)
        extra = sorted(f"{ticker}:{analyst}" for ticker, analyst in db_pairs - expected)
        duplicate_pairs = counts.get("duplicate_pairs") or []
        if missing or extra or duplicate_pairs or int(counts.get("distinct_pairs") or 0) != expected_pairs:
            raise RuntimeError(
                "Phase1 analyst signal persistence incomplete: "
                f"expected={expected_pairs}, distinct={counts.get('distinct_pairs')}, "
                f"rows={counts.get('row_total')}, missing={missing[:12]}, "
                f"extra={extra[:12]}, duplicate={duplicate_pairs[:12]}"
            )

    def _persist_pm_full_market_contracts(self, generated: List[Tuple[str, FuturesRecommendation]]) -> None:
        """Finalize PM contracts and persist each recommendation exactly once."""
        from agents.decision_team.portfolio_manager import finalize_pm_full_market_contracts

        finalize_pm_full_market_contracts(
            generated=generated,
            config=self.config,
            portfolio=self.init_portfolio,
        )
        for ticker, recommendation in generated:
            self._assert_pm_signed_recommendation_for_persistence(ticker, recommendation)
        for _, recommendation in generated:
            recommendation_id = self.db.save_futures_recommendation(recommendation)
            if not recommendation_id:
                raise RuntimeError(f"Failed to save futures recommendation for {recommendation.underlying_code}")
            recommendation.id = recommendation_id

    def _assert_pm_signed_recommendation_for_persistence(
        self,
        ticker: str,
        recommendation: FuturesRecommendation,
    ) -> None:
        """Block persistence unless PM step 6 has signed the final contract."""
        snapshot = recommendation.signal_snapshot
        if not isinstance(snapshot, dict):
            raise RuntimeError(f"{ticker} PM recommendation missing signal_snapshot after step6 finalization")
        final_contract = snapshot.get("final_action_contract")
        if not isinstance(final_contract, dict) or not final_contract:
            raise RuntimeError(f"{ticker} PM recommendation missing signed final_action_contract")
        if "pm_internal_candidate" in snapshot:
            raise RuntimeError(f"{ticker} PM internal candidate remained after step6 signing")
        if "pm_internal_candidate_contract" in snapshot:
            raise RuntimeError(f"{ticker} PM internal candidate contract remained after step6 signing")
        if "pm_capital_deployment_decision" in snapshot:
            raise RuntimeError(f"{ticker} PM capital deployment decision remained after step6 signing")
        trace = snapshot.get("pm_six_step_trace")
        check = trace.get("pm_contract_self_check") if isinstance(trace, dict) else None
        if not isinstance(check, dict) or check.get("ok") is not True:
            raise RuntimeError(f"{ticker} PM final_action_contract self-check not ok before persistence")
        generation_check = trace.get("step6_contract_generation_check") if isinstance(trace, dict) else None
        if not isinstance(generation_check, dict) or generation_check.get("ok") is not True:
            raise RuntimeError(f"{ticker} PM step6 contract generation check not ok before persistence")

    def _audit_phase1_strategy_recommendations(self, generated: List[Tuple[str, FuturesRecommendation]]) -> None:
        """Run the independent Auditor after PM capital deployment finalizes contracts."""
        for ticker, recommendation in generated:
            source_type = getattr(recommendation.source_type, "value", recommendation.source_type)
            if recommendation.status == RecommendationStatus.SKIPPED:
                continue
            if source_type != RecommendationSourceType.STRATEGY.value:
                continue
            recommendation_dict = recommendation.model_dump() if hasattr(recommendation, "model_dump") else dict(recommendation)
            audit_output = audit_futures_recommendation(
                recommendation=recommendation_dict,
                full_config=self.config,
                account_state={
                    "account_equity": getattr(self.init_portfolio, "account_equity", None),
                    "cashflow": getattr(self.init_portfolio, "cashflow", None),
                    "margin_used": getattr(self.init_portfolio, "margin_used", None),
                    "margin_ratio": getattr(self.init_portfolio, "margin_ratio", None),
                },
            )
            snapshot = recommendation.signal_snapshot if isinstance(recommendation.signal_snapshot, dict) else {}
            snapshot["auditor"] = {
                "producer": "auditor",
                "audit_status": audit_output.audit_status,
                "audit_verdict": audit_output.audit_verdict,
                "audit_reason_codes": list(audit_output.audit_reason_codes),
                "audited_at": audit_output.audited_at,
                "independent_auditor_agent": True,
                "pm_risk_gate_is_not_auditor": True,
            }
            setattr(recommendation, "signal_snapshot", snapshot)
            recommendation.audit_payload = audit_output.audit_payload
            if audit_output.audit_verdict in {"block", "require_review"}:
                logger.warning(
                    f"{ticker}: independent Auditor blocked PM contract: "
                    f"reasons={audit_output.audit_reason_codes}"
                )
            updated = self.db.update_futures_recommendation_status(
                recommendation.id,
                recommendation.status,
                action=recommendation.action,
                lots=recommendation.lots,
                signal_snapshot=snapshot,
                audit_payload=audit_output.audit_payload,
            )
            if not updated:
                raise RuntimeError(f"Failed to save Auditor verdict for {ticker}")

    @staticmethod
    def _normalize_analyst_name(name: str) -> str:
        text = str(name or "").strip()
        if text in {AgentKey.COMPANY_NEWS, "company_news"}:
            return AgentKey.COMMODITY_NEWS
        return text

    @classmethod
    def _validate_phase1_analyst_outputs(
        cls,
        ticker: str,
        analysts: list[str],
        analyst_signals: list[Any],
    ) -> None:
        expected = [cls._normalize_analyst_name(name) for name in analysts]
        if not expected:
            return
        seen: dict[str, int] = {}
        for signal in analyst_signals or []:
            analyst = cls._normalize_analyst_name(getattr(signal, "agent_name", ""))
            if analyst:
                seen[analyst] = seen.get(analyst, 0) + 1
        missing = [analyst for analyst in expected if seen.get(analyst, 0) < 1]
        duplicate = [analyst for analyst, count in seen.items() if analyst in expected and count > 1]
        extra = [analyst for analyst in seen if analyst not in expected]
        if missing or duplicate or extra:
            raise RuntimeError(
                f"{ticker} phase1 analyst output incomplete before PM: "
                f"expected={expected}, seen={seen}, missing={missing}, "
                f"duplicate={duplicate}, extra={extra}"
            )

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
        from agents.decision_team.signal_collector import signal_collector_agent
        from agents.decision_team.portfolio_manager import portfolio_agent_futures
        graph.add_node(AgentKey.SIGNAL_COLLECTOR, signal_collector_agent)
        portfolio_agent = portfolio_agent_futures
        graph.add_node(AgentKey.PORTFOLIO, portfolio_agent)

        # create node for each analyst and add edge
        for analyst in self.current_analysts:
            agent_func = AgentRegistry.get_agent_func_by_key(analyst)
            graph.add_node(analyst, agent_func)
            graph.add_edge(START, analyst)
            graph.add_edge(analyst, AgentKey.SIGNAL_COLLECTOR)

        graph.add_edge(AgentKey.SIGNAL_COLLECTOR, AgentKey.PORTFOLIO)
        graph.add_edge(AgentKey.PORTFOLIO, END)

        workflow = graph.compile()

        return workflow 
        

    def load_analysts(self, ticker: str):
        """
        Load all configured analysts for the fixed futures workflow.
        """
        logger.info("Using all verified analysts")
        self.current_analysts = self.workflow_analysts.copy()
            
        logger.info(f"Active analysts for {ticker}: {self.current_analysts}")

    def _require_pm_candidate_recommendation(self, ticker: str, final_state: Dict[str, Any]) -> FuturesRecommendation:
        """Collect PM's per-ticker candidate before full-market step 5/6 finalization."""
        recommendation = final_state.get("recommendation")
        if not recommendation:
            raise RuntimeError(f"{ticker} phase1 PM did not return a FuturesRecommendation candidate")
        if isinstance(recommendation, FuturesRecommendation):
            candidate = recommendation
        else:
            candidate = FuturesRecommendation.model_validate(recommendation)
        snapshot = candidate.signal_snapshot
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("pm_internal_candidate"), dict):
            raise RuntimeError(f"{ticker} phase1 PM recommendation missing pm_internal_candidate")
        return candidate

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
            config=self._phase1_compat_config(),
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
        self._validate_phase1_analyst_outputs(ticker, analysts, analyst_signals)
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
            "config": self._phase1_compat_config(),
            "full_config": self.config,
            "router": self.router,
            "portfolio": portfolio,
            "analyst_signals": analyst_signals,
            "analyst_outputs": analyst_outputs,
        }

    def _run_phase1_portfolio_only(self, analysis_state: Dict[str, Any], portfolio: Portfolio) -> Dict[str, Any]:
        from agents.decision_team.signal_collector import signal_collector_agent
        from agents.decision_team.portfolio_manager import portfolio_agent_futures

        state = dict(analysis_state)
        state["portfolio"] = portfolio
        state["num_tickers"] = len(self.tickers)
        state["decision"] = None
        state["recommendation"] = None
        collector_output = signal_collector_agent(state)
        state.update(collector_output)
        self._validate_phase1_analyst_outputs(
            str(state.get("ticker") or ""),
            list(state.get("enabled_analysts") or self.workflow_analysts),
            list(state.get("analyst_signals") or []),
        )
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

    def _project_signed_contract_to_virtual_portfolio(self, portfolio: Portfolio, recommendation: FuturesRecommendation) -> Portfolio:
        """Project a signed PM contract into the Phase1 planning portfolio.

        This is a read-only projection of ``final_action_contract`` into an
        in-memory planning portfolio. It must not infer strategy, complete a
        missing contract, or derive target lots from recommendation action/lots.
        """
        if recommendation.status == RecommendationStatus.SKIPPED:
            return portfolio

        ticker = recommendation.underlying_code
        source_type = getattr(recommendation.source_type, "value", recommendation.source_type)
        if source_type != RecommendationSourceType.STRATEGY.value:
            return portfolio
        signed_snapshot = recommendation.signal_snapshot or {}

        final_contract = (
            signed_snapshot.get("final_action_contract")
            if isinstance(signed_snapshot, dict) and isinstance(signed_snapshot.get("final_action_contract"), dict)
            else {}
        )
        if not final_contract:
            raise RuntimeError(f"{ticker}: strategy recommendation missing signed final_action_contract")
        if final_contract.get("target_lots") is None:
            raise RuntimeError(f"{ticker}: signed final_action_contract missing target_lots")

        contract_info = FuturesContractInfoCache.get_contract_info(ticker)
        if not contract_info:
            return portfolio

        if ticker not in portfolio.positions:
            portfolio.positions[ticker] = Position(shares=0, value=0)

        position = portfolio.positions[ticker]
        position.contract_multiplier = contract_info.get("contract_multiplier")
        reference_price = float(getattr(recommendation, "base_price", None) or 0.0)
        projected_lots = int(final_contract.get("target_lots") or 0)

        if reference_price <= 0:
            return portfolio

        position.shares = projected_lots
        if projected_lots == 0:
            position.value = 0.0
            position.margin_used = 0.0
            position.entry_price = None
            position.unrealized_pnl = 0.0
            return self._refresh_portfolio_account_fields(portfolio)

        margin_rate = (
            contract_info.get("margin_rate_long")
            if projected_lots > 0 else contract_info.get("margin_rate_short")
        )
        position.entry_price = reference_price
        position.margin_rate = margin_rate
        position.value = abs(projected_lots) * reference_price * contract_info["contract_multiplier"]
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
        phase1_planning_portfolio = portfolio.model_copy(deep=True)
        self._prefetch_local_daily_data(timings)
        self._prefetch_pandaai_daily_data(timings)
        morning_contexts = self._prefetch_pre_open_reference_prices(timings)
        prefetched_analysis = self._prefetch_phase1_analysis(portfolio, morning_contexts, timings)
        generated_recommendations: List[Tuple[str, FuturesRecommendation]] = []

        for ticker in self.tickers:
            ticker_started_at = perf_counter()
            self._timed_call(timings, "load_analysts", self.load_analysts, ticker)
            morning_price_context = morning_contexts.get(ticker)
            if morning_price_context is None or morning_price_context.base_price is None:
                warning_message = (
                    getattr(morning_price_context, "warning_message", None)
                    if morning_price_context is not None else None
                )
                missing_basis_state = self._build_futures_phase1_state(ticker, portfolio, morning_price_context)
                missing_basis_state["pre_open_reference_price_unavailable"] = True
                missing_basis_state["pre_open_reference_price_unavailable_reason"] = (
                    warning_message or "pre_open_reference_price_unavailable"
                )
                missing_basis_state["pre_open_reference_price_unavailable_warning"] = warning_message
                final_state = self._timed_call(
                    timings,
                    "portfolio_manager",
                    self._run_phase1_portfolio_only,
                    missing_basis_state,
                    portfolio,
                )
                self._timed_call(
                    timings,
                    "save_missing_basis_analyst_outputs",
                    self._save_prefetched_analyst_outputs,
                    final_state,
                )
                recommendation = self._require_pm_candidate_recommendation(ticker=ticker, final_state=final_state)
                generated_recommendations.append((ticker, recommendation))
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

            recommendation = self._require_pm_candidate_recommendation(ticker=ticker, final_state=final_state)
            generated_recommendations.append((ticker, recommendation))

            logger.log_portfolio(f"{ticker} phase1 recommendation collected", portfolio)

            if self.planner_mode:
                self.current_analysts = None
            elif prefetched_analysis:
                self.current_analysts = self.workflow_analysts.copy()
            timings[f"{ticker}.total"] = perf_counter() - ticker_started_at

        self._validate_phase1_signal_persistence(portfolio, self.tickers)
        self._persist_pm_full_market_contracts(generated_recommendations)
        self._audit_phase1_strategy_recommendations(generated_recommendations)
        portfolio = phase1_planning_portfolio
        for _, recommendation in generated_recommendations:
            if recommendation.status != RecommendationStatus.SKIPPED:
                portfolio = self._project_signed_contract_to_virtual_portfolio(portfolio, recommendation)
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


