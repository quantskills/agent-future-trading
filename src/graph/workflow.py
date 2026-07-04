from typing import Dict, Any
from langgraph.graph import StateGraph, START, END
from typing import Callable
from typing import List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from graph.schema import (
    AnalystSignal,
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
from graph.constants import AgentKey, Signal
from agents.registry import AgentRegistry
from tools.agent_tools.execution.trader_futures_execution import FuturesExecutionEngine
from tools.agent_tools.analysis.analyst_quality import write_analyst_report
from tools.agent_tools.analysis.analyst_data_usage import prefetch_local_daily_data, prefetch_pandaai_daily_data
from tools.agent_tools.analysis.analyst_signal_fusion import (
    CAPITAL_PRIORITY_RANK_MEANING,
    CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION,
)
from tools.agent_tools.decision.pm_opportunity_ranking import rank_metadata_for_row
from tools.common.final_action_semantics import (
    RANK_CAPITAL_LAYER_FIELDS,
    canonicalize_final_action_contract_for_persistence,
    rank_capital_layer_contract_complete,
)
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

    @staticmethod
    def _scorecard_preferred_row(snapshot: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        scorecard = snapshot.get("opportunity_scorecard") if isinstance(snapshot.get("opportunity_scorecard"), dict) else {}
        preferred_side = str(scorecard.get("preferred_side") or "").lower()
        if preferred_side in {"long", "short"} and isinstance(scorecard.get(preferred_side), dict):
            return preferred_side, scorecard[preferred_side]
        best_side = ""
        best_row: Dict[str, Any] = {}
        best_score = -1.0
        for side in ("long", "short"):
            row = scorecard.get(side)
            if not isinstance(row, dict):
                continue
            try:
                score = float(row.get("opportunity_score", row.get("score", -1.0)) or -1.0)
            except (TypeError, ValueError):
                score = -1.0
            if score > best_score:
                best_side = side
                best_row = row
                best_score = score
        return best_side, best_row

    @staticmethod
    def _rank_metadata_from_snapshot(snapshot: Dict[str, Any], side: str = "") -> Dict[str, str]:
        scorecard = snapshot.get("opportunity_scorecard") if isinstance(snapshot.get("opportunity_scorecard"), dict) else {}
        row = scorecard.get(side) if side in {"long", "short"} and isinstance(scorecard.get(side), dict) else {}
        if not row:
            _, row = AgentWorkflow._scorecard_preferred_row(snapshot)
        if row:
            metadata = rank_metadata_for_row(row)
            if all(metadata.get(field) not in (None, "") for field in RANK_CAPITAL_LAYER_FIELDS):
                return metadata
        else:
            metadata = {}
        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        evidence = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
        deployment = contract.get("capital_deployment") if isinstance(contract.get("capital_deployment"), dict) else {}
        for source in (evidence, deployment):
            recovered = {
                field: source.get(field)
                for field in RANK_CAPITAL_LAYER_FIELDS
                if source.get(field) not in (None, "")
            }
            if all(field in recovered for field in RANK_CAPITAL_LAYER_FIELDS):
                return recovered
        return metadata

    @staticmethod
    def _canonicalize_snapshot_final_contract(
        snapshot: Dict[str, Any],
        *,
        side: str = "",
        rank: int | None = None,
    ) -> bool:
        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        if not contract:
            return False
        rank_metadata = AgentWorkflow._rank_metadata_from_snapshot(snapshot, side)
        canonical = canonicalize_final_action_contract_for_persistence(
            contract,
            rank_metadata=rank_metadata,
            opportunity_rank=rank,
        )
        changed = canonical != contract
        snapshot["final_action_contract"] = canonical
        return changed

    @staticmethod
    def _set_daily_opportunity_rank(snapshot: Dict[str, Any], side: str, rank: int) -> None:
        scorecard = snapshot.get("opportunity_scorecard") if isinstance(snapshot.get("opportunity_scorecard"), dict) else {}
        row = scorecard.get(side) if side in {"long", "short"} and isinstance(scorecard.get(side), dict) else {}
        rank_metadata = rank_metadata_for_row(row) if row else {}
        if row:
            row["opportunity_rank"] = rank
            row.update(rank_metadata)
            row["rank_semantics_version"] = CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION
            row["opportunity_rank_meaning"] = CAPITAL_PRIORITY_RANK_MEANING
            row["rank_is_capital_priority"] = True
            row["rank_is_not_trade_authority"] = True
        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        evidence_used = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
        if evidence_used:
            evidence_used["opportunity_rank"] = rank
            evidence_used.update(rank_metadata)
            evidence_used["rank_semantics_version"] = CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION
            evidence_used["opportunity_rank_meaning"] = CAPITAL_PRIORITY_RANK_MEANING
            evidence_used["rank_is_capital_priority"] = True
            evidence_used["rank_is_not_trade_authority"] = True
        active_audit = snapshot.get("active_opportunity_audit") if isinstance(snapshot.get("active_opportunity_audit"), dict) else {}
        active_opportunity = active_audit.get("opportunity") if isinstance(active_audit.get("opportunity"), dict) else {}
        if active_opportunity:
            active_opportunity["opportunity_rank"] = rank
            active_opportunity.update(rank_metadata)
            active_opportunity["rank_semantics_version"] = CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION
            active_opportunity["opportunity_rank_meaning"] = CAPITAL_PRIORITY_RANK_MEANING
            active_opportunity["rank_is_capital_priority"] = True
            active_opportunity["rank_is_not_trade_authority"] = True
        consistency = (
            snapshot.get("pm_landing_consistency_audit")
            if isinstance(snapshot.get("pm_landing_consistency_audit"), dict)
            else {}
        )
        alignment = (
            consistency.get("opportunity_scorecard_alignment")
            if isinstance(consistency.get("opportunity_scorecard_alignment"), dict)
            else {}
        )
        if alignment:
            alignment["opportunity_rank"] = rank
            alignment.update(rank_metadata)

    @staticmethod
    def _contract_target_lots(snapshot: Dict[str, Any]) -> int:
        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        try:
            return int(contract.get("target_lots") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _contract_current_lots(snapshot: Dict[str, Any]) -> int:
        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        try:
            return int(contract.get("current_lots") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _is_new_or_increasing_risk(snapshot: Dict[str, Any]) -> bool:
        current_lots = AgentWorkflow._contract_current_lots(snapshot)
        target_lots = AgentWorkflow._contract_target_lots(snapshot)
        if target_lots == 0 or target_lots == current_lots:
            return False
        if current_lots == 0:
            return True
        if (current_lots > 0 and target_lots > current_lots) or (current_lots < 0 and target_lots < current_lots):
            return True
        if (current_lots > 0 and target_lots < 0) or (current_lots < 0 and target_lots > 0):
            return True
        return False

    @staticmethod
    def _lots_action_from_target(current_lots: int, target_lots: int) -> Tuple[RecommendationAction, int]:
        current = int(current_lots)
        target = int(target_lots)
        if target == current:
            return RecommendationAction.HOLD, 0
        if current == 0:
            return (
                (RecommendationAction.OPEN_LONG, abs(target))
                if target > 0
                else (RecommendationAction.OPEN_SHORT, abs(target))
            )
        if current > 0:
            if target >= 0:
                return (
                    (RecommendationAction.OPEN_LONG, target - current)
                    if target > current
                    else (RecommendationAction.CLOSE_LONG, current - target)
                )
            return RecommendationAction.CLOSE_LONG, current
        if target <= 0:
            return (
                (RecommendationAction.OPEN_SHORT, abs(target) - abs(current))
                if abs(target) > abs(current)
                else (RecommendationAction.CLOSE_SHORT, abs(current) - abs(target))
            )
        return RecommendationAction.CLOSE_SHORT, abs(current)

    @staticmethod
    def _apply_deployed_target_to_snapshot(
        snapshot: Dict[str, Any],
        *,
        target_lots: int,
        reason: str,
        selected: bool,
        rank: int | None,
    ) -> None:
        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        if not contract:
            return
        current_lots = AgentWorkflow._contract_current_lots(snapshot)
        original_target = int(contract.get("target_lots") or 0)
        original_final_action = str(contract.get("final_action") or "").strip()
        target_lots = int(target_lots)
        lots_delta = target_lots - current_lots
        contract["target_lots"] = target_lots
        contract["lots_delta"] = lots_delta
        contract["lots_delta_abs"] = abs(lots_delta)
        if selected and target_lots == original_target and original_final_action:
            contract["final_action"] = original_final_action
        elif target_lots == current_lots:
            contract["final_action"] = "hold"
        elif current_lots == 0:
            contract["final_action"] = "open_probe"
        elif target_lots == 0:
            contract["final_action"] = "exit"
        elif (current_lots > 0 and target_lots > 0) or (current_lots < 0 and target_lots < 0):
            contract["final_action"] = "scale" if abs(target_lots) > abs(current_lots) else "reduce"
        else:
            contract["final_action"] = "exit"
        reason_codes = contract.get("reason_codes") if isinstance(contract.get("reason_codes"), list) else []
        reason_set = {str(item) for item in reason_codes if item}
        reason_set.add("pm_full_market_capital_deployment")
        if not selected and original_target != target_lots:
            reason_set.add("capital_queue_not_selected")
        contract["reason_codes"] = sorted(reason_set)
        rank_metadata = AgentWorkflow._rank_metadata_from_snapshot(snapshot)
        evidence = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
        evidence["capital_allocation_reason"] = reason
        if rank is not None:
            evidence["opportunity_rank"] = rank
            evidence.update(rank_metadata)
        evidence["rank_semantics_version"] = CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION
        evidence["opportunity_rank_meaning"] = CAPITAL_PRIORITY_RANK_MEANING
        evidence["rank_is_capital_priority"] = True
        evidence["rank_is_not_trade_authority"] = True
        contract["evidence_used"] = evidence
        deployment = {
            "selected_for_capital_deployment": bool(selected),
            "capital_allocation_reason": reason,
            "original_target_lots": int(original_target),
            "deployed_target_lots": int(target_lots),
            "deployed_lots_delta": int(lots_delta),
            "opportunity_rank": rank,
            **rank_metadata,
            "rank_semantics_version": CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION,
            "opportunity_rank_meaning": CAPITAL_PRIORITY_RANK_MEANING,
            "rank_is_capital_priority": True,
            "rank_is_not_trade_authority": True,
            "not_second_contract": True,
            "pm_remains_single_fund_manager": True,
        }
        contract["capital_deployment"] = deployment
        snapshot["final_action_contract"] = contract
        rebalance = snapshot.get("rebalance_summary") if isinstance(snapshot.get("rebalance_summary"), dict) else {}
        if rebalance:
            rebalance["target_lots"] = int(target_lots)
            rebalance["lots_delta"] = int(lots_delta)
            rebalance["capital_allocation_reason"] = reason
            rebalance["capital_deployment"] = deployment
        active = snapshot.get("active_opportunity_audit") if isinstance(snapshot.get("active_opportunity_audit"), dict) else {}
        opportunity = active.get("opportunity") if isinstance(active.get("opportunity"), dict) else {}
        if opportunity:
            opportunity["capital_allocation_reason"] = reason
            opportunity["selected_for_capital_deployment"] = bool(selected)
            opportunity.update(rank_metadata)
            opportunity["rank_semantics_version"] = CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION
            opportunity["opportunity_rank_meaning"] = CAPITAL_PRIORITY_RANK_MEANING
            opportunity["rank_is_capital_priority"] = True
            opportunity["rank_is_not_trade_authority"] = True
        AgentWorkflow._canonicalize_snapshot_final_contract(snapshot, rank=rank)

    @staticmethod
    def _safe_opportunity_rank(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _snapshot_opportunity_rank(snapshot: Dict[str, Any], side: str = "") -> int | None:
        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        deployment = contract.get("capital_deployment") if isinstance(contract.get("capital_deployment"), dict) else {}
        evidence = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
        rank = AgentWorkflow._safe_opportunity_rank(deployment.get("opportunity_rank"))
        if rank is None:
            rank = AgentWorkflow._safe_opportunity_rank(evidence.get("opportunity_rank"))
        scorecard = snapshot.get("opportunity_scorecard") if isinstance(snapshot.get("opportunity_scorecard"), dict) else {}
        if rank is None and side in {"long", "short"}:
            row = scorecard.get(side) if isinstance(scorecard.get(side), dict) else {}
            rank = AgentWorkflow._safe_opportunity_rank(row.get("opportunity_rank"))
        if rank is None:
            active = snapshot.get("active_opportunity_audit") if isinstance(snapshot.get("active_opportunity_audit"), dict) else {}
            opportunity = active.get("opportunity") if isinstance(active.get("opportunity"), dict) else {}
            rank = AgentWorkflow._safe_opportunity_rank(opportunity.get("opportunity_rank"))
        return rank

    @staticmethod
    def _capital_deployment_complete(contract: Dict[str, Any], rank: int | None) -> bool:
        deployment = contract.get("capital_deployment") if isinstance(contract.get("capital_deployment"), dict) else {}
        if not deployment:
            return False
        if rank is not None and not rank_capital_layer_contract_complete(contract):
            return False
        required = {
            "selected_for_capital_deployment",
            "capital_allocation_reason",
            "original_target_lots",
            "deployed_target_lots",
            "deployed_lots_delta",
        }
        if rank is not None:
            required.update({"rank_capital_role", "capital_layer", "capital_ratio_source", "rank_reason"})
        if not required.issubset(deployment.keys()):
            return False
        if rank is not None and deployment.get("opportunity_rank") in (None, ""):
            return False
        return True

    @staticmethod
    def _requires_atomic_capital_deployment(snapshot: Dict[str, Any], contract: Dict[str, Any]) -> bool:
        final_action = str(contract.get("final_action") or "").strip().lower()
        if AgentWorkflow._is_new_or_increasing_risk(snapshot):
            return True
        if final_action in {"open", "open_long", "open_short", "open_probe", "open_real", "add", "scale", "increase", "conditional_probe", "conditional_monitor", "watch_trigger"}:
            return True
        if bool(contract.get("conditional_trigger_authority")) or bool(contract.get("requires_intraday_confirmation")):
            return True
        return False

    @staticmethod
    def _ensure_atomic_capital_deployment_submission(
        snapshot: Dict[str, Any],
        *,
        side: str = "",
    ) -> bool:
        """Make rank, deployment, target lots, and allocation reason one PM fact.

        PM may build ranking and sizing in separate internal steps, but a
        persisted final_action_contract cannot expose a bare rank. If rank has
        reached the final contract or PM diagnostics, the same contract must also
        carry the capital deployment conclusion that downstream agents audit.
        """
        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        if not contract:
            return False
        rank = AgentWorkflow._snapshot_opportunity_rank(snapshot, side)
        evidence = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
        deployment = contract.get("capital_deployment") if isinstance(contract.get("capital_deployment"), dict) else {}
        if rank is None and not deployment and not AgentWorkflow._requires_atomic_capital_deployment(snapshot, contract):
            return False
        if AgentWorkflow._capital_deployment_complete(contract, rank):
            changed = AgentWorkflow._canonicalize_snapshot_final_contract(snapshot, side=side, rank=rank)
            contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
            evidence = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
            deployment = contract.get("capital_deployment") if isinstance(contract.get("capital_deployment"), dict) else {}
            if rank is not None and evidence.get("opportunity_rank") in (None, ""):
                evidence["opportunity_rank"] = rank
                changed = True
            reason = deployment.get("capital_allocation_reason")
            if reason and evidence.get("capital_allocation_reason") in (None, ""):
                evidence["capital_allocation_reason"] = reason
                changed = True
            if changed:
                contract["evidence_used"] = evidence
                snapshot["final_action_contract"] = contract
            return changed

        current_lots = AgentWorkflow._contract_current_lots(snapshot)
        target_lots = AgentWorkflow._contract_target_lots(snapshot)
        lots_changed = target_lots != current_lots
        selected = (
            bool(deployment.get("selected_for_capital_deployment"))
            if deployment and "selected_for_capital_deployment" in deployment
            else lots_changed
        )
        reason = str(
            deployment.get("capital_allocation_reason")
            or evidence.get("capital_allocation_reason")
            or contract.get("capital_allocation_reason")
            or ""
        ).strip()
        if not reason:
            if selected:
                reason = f"selected_by_pm_atomic_contract_submission:rank={rank if rank is not None else 'unranked'}"
            else:
                reason = f"not_selected_by_pm_atomic_contract_submission:rank={rank if rank is not None else 'unranked'}"
        deployed_target = target_lots if selected else current_lots
        AgentWorkflow._apply_deployed_target_to_snapshot(
            snapshot,
            target_lots=deployed_target,
            reason=reason,
            selected=selected,
            rank=rank,
        )
        return True

    def _daily_capital_deployment_config(self) -> Dict[str, float]:
        budget = self.config.get("position_budget_policy", {}) or {}
        capital = self.config.get("capital_utilization_control", {}) or {}
        hard_max = self._safe_positive_ratio(self.config.get("max_total_margin_ratio"), 0.20)
        target = self._safe_positive_ratio(capital.get("target_margin_ratio_confirmed"), 0.10)
        min_probe = self._safe_positive_ratio(budget.get("min_real_trade_margin_ratio"), 0.008)
        max_single = self._safe_positive_ratio(budget.get("max_single_ticker_margin_ratio"), 0.13)
        return {
            "target_margin_ratio": min(target, hard_max),
            "min_probe_margin_ratio": min_probe,
            "max_single_ticker_margin_ratio": min(max_single, hard_max),
            "hard_max_total_margin_ratio": hard_max,
        }

    def _recommended_margin_ratio(self, recommendation: FuturesRecommendation) -> float:
        snapshot = recommendation.signal_snapshot if isinstance(recommendation.signal_snapshot, dict) else {}
        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        try:
            estimate = float(contract.get("target_margin_ratio_estimate") or 0.0)
        except (TypeError, ValueError):
            estimate = 0.0
        if estimate > 0:
            return estimate
        base_price = float(getattr(recommendation, "base_price", None) or 0.0)
        target_lots = abs(self._contract_target_lots(snapshot))
        if base_price <= 0 or target_lots <= 0:
            return 0.0
        info = FuturesContractInfoCache.get_contract_info(recommendation.underlying_code)
        if not info:
            return 0.0
        side_rate = info.get("margin_rate_long") if self._contract_target_lots(snapshot) > 0 else info.get("margin_rate_short")
        margin = base_price * target_lots * float(info.get("contract_multiplier") or 1.0) * float(side_rate or 0.0)
        equity = float(getattr(self.init_portfolio, "account_equity", 0.0) or getattr(self.init_portfolio, "cashflow", 0.0) or 1.0)
        return margin / max(equity, 1.0)

    @staticmethod
    def _float_field(mapping: Dict[str, Any], field: str, default: float = 0.0) -> float:
        try:
            return float(mapping.get(field, default) if mapping.get(field, default) is not None else default)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _capital_rank_eligible(snapshot: Dict[str, Any], row: Dict[str, Any]) -> bool:
        state = str(row.get("final_state") or row.get("opportunity_state") or "").strip().lower()
        if state in {"no_opportunity", "wait", "flat_wait", "blocked", "rejected"}:
            return False
        contract = snapshot.get("final_action_contract") if isinstance(snapshot.get("final_action_contract"), dict) else {}
        final_action = str(contract.get("final_action") or "").strip().lower()
        if final_action in {"reduce", "exit", "close", "close_long", "close_short", "risk_exit"}:
            return False
        current_lots = AgentWorkflow._contract_current_lots(snapshot)
        target_lots = AgentWorkflow._contract_target_lots(snapshot)
        if target_lots == 0 and current_lots == 0 and not (
            bool(contract.get("conditional_trigger_authority")) or bool(contract.get("requires_intraday_confirmation"))
        ):
            return False
        if state in {"tradeable_candidate", "probe_candidate", "watch_for_trigger"}:
            return True
        return AgentWorkflow._float_field(row, "opportunity_score", AgentWorkflow._float_field(row, "score", 0.0)) > 0.0

    @staticmethod
    def _capital_rank_sort_tuple(row: Dict[str, Any]) -> Tuple[int, float, float, float]:
        try:
            tier = int(row.get("capital_priority_tier") or 0)
        except (TypeError, ValueError):
            tier = 0
        priority = AgentWorkflow._float_field(row, "capital_priority_score")
        watch_priority = AgentWorkflow._float_field(row, "watch_priority_score", priority)
        score = AgentWorkflow._float_field(row, "opportunity_score", AgentWorkflow._float_field(row, "score"))
        return tier, watch_priority, priority, score

    def _apply_daily_capital_deployment(self, generated: List[Tuple[str, FuturesRecommendation]]) -> None:
        deployment_cfg = self._daily_capital_deployment_config()
        candidates: List[Tuple[int, float, float, float, str, FuturesRecommendation, str, float]] = []
        updated_ids: set[str] = set()
        for ticker, recommendation in generated:
            if recommendation.status != RecommendationStatus.SKIPPED:
                continue
            source_type = getattr(recommendation.source_type, "value", recommendation.source_type)
            if source_type != RecommendationSourceType.STRATEGY.value:
                continue
            snapshot = recommendation.signal_snapshot if isinstance(recommendation.signal_snapshot, dict) else {}
            side, _ = self._scorecard_preferred_row(snapshot)
            rank = self._snapshot_opportunity_rank(snapshot, side)
            if not self._canonicalize_snapshot_final_contract(snapshot, side=side, rank=rank):
                continue
            recommendation.signal_snapshot = snapshot
            action, lots = self._lots_action_from_target(
                self._contract_current_lots(snapshot),
                self._contract_target_lots(snapshot),
            )
            recommendation.action = action
            recommendation.lots = lots
            self.db.update_futures_recommendation_status(
                recommendation.id,
                recommendation.status,
                action=recommendation.action,
                lots=recommendation.lots,
                signal_snapshot=snapshot,
            )
            updated_ids.add(str(recommendation.id))
        for ticker, recommendation in generated:
            if recommendation.status == RecommendationStatus.SKIPPED:
                continue
            source_type = getattr(recommendation.source_type, "value", recommendation.source_type)
            if source_type != RecommendationSourceType.STRATEGY.value:
                continue
            snapshot = recommendation.signal_snapshot if isinstance(recommendation.signal_snapshot, dict) else {}
            side, row = self._scorecard_preferred_row(snapshot)
            if side not in {"long", "short"} or not row:
                continue
            if not self._capital_rank_eligible(snapshot, row):
                continue
            try:
                score = float(row.get("opportunity_score", row.get("score", 0.0)) or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            if score <= 0:
                continue
            try:
                priority_score = float(row.get("capital_priority_score", score) or score)
            except (TypeError, ValueError):
                priority_score = score
            self._set_daily_opportunity_rank(snapshot, side, 0)
            recommendation.signal_snapshot = snapshot
            margin_ratio = self._recommended_margin_ratio(recommendation)
            tier, watch_priority, _, _ = self._capital_rank_sort_tuple(row)
            candidates.append((tier, watch_priority, priority_score, score, str(ticker).upper(), recommendation, side, margin_ratio))
        candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
        selected_ids: set[str] = set()
        used_margin_ratio = 0.0
        target_margin_ratio = deployment_cfg["target_margin_ratio"]
        min_probe_ratio = deployment_cfg["min_probe_margin_ratio"]
        max_single_ratio = deployment_cfg["max_single_ticker_margin_ratio"]
        for rank, (_, _, priority_score, score, _, recommendation, side, margin_ratio) in enumerate(candidates, start=1):
            snapshot = recommendation.signal_snapshot if isinstance(recommendation.signal_snapshot, dict) else {}
            self._set_daily_opportunity_rank(snapshot, side, rank)
            current_lots = self._contract_current_lots(snapshot)
            target_lots = self._contract_target_lots(snapshot)
            if not self._is_new_or_increasing_risk(snapshot):
                reason = "not_new_or_increasing_risk_preserve_pm_contract"
                self._apply_deployed_target_to_snapshot(
                    snapshot,
                    target_lots=target_lots,
                    reason=reason,
                    selected=True,
                    rank=rank,
                )
                selected_ids.add(str(recommendation.id))
            else:
                capped_margin = min(max(margin_ratio, min_probe_ratio), max_single_ratio)
                can_select = used_margin_ratio + capped_margin <= target_margin_ratio or not selected_ids
                if can_select:
                    used_margin_ratio += capped_margin
                    reason = (
                        "selected_by_full_market_pm_capital_queue:"
                        f"rank={rank};capital_priority_score={priority_score};score={score};"
                        f"target_margin_used={used_margin_ratio:.4f}/{target_margin_ratio:.4f}"
                    )
                    self._apply_deployed_target_to_snapshot(
                        snapshot,
                        target_lots=target_lots,
                        reason=reason,
                        selected=True,
                        rank=rank,
                    )
                    selected_ids.add(str(recommendation.id))
                else:
                    reason = (
                        "not_selected_by_full_market_pm_capital_queue:"
                        f"rank={rank};capital_priority_score={priority_score};"
                        f"capital_target_filled={used_margin_ratio:.4f}/{target_margin_ratio:.4f}"
                    )
                    self._apply_deployed_target_to_snapshot(
                        snapshot,
                        target_lots=current_lots,
                        reason=reason,
                        selected=False,
                        rank=rank,
                    )
            recommendation.signal_snapshot = snapshot
            self._canonicalize_snapshot_final_contract(snapshot, side=side, rank=rank)
            action, lots = self._lots_action_from_target(
                self._contract_current_lots(snapshot),
                self._contract_target_lots(snapshot),
            )
            recommendation.action = action
            recommendation.lots = lots
            self.db.update_futures_recommendation_status(
                recommendation.id,
                recommendation.status,
                action=recommendation.action,
                lots=recommendation.lots,
                signal_snapshot=snapshot,
            )
            updated_ids.add(str(recommendation.id))
        for ticker, recommendation in generated:
            if str(recommendation.id) in updated_ids:
                continue
            if recommendation.status == RecommendationStatus.SKIPPED:
                continue
            source_type = getattr(recommendation.source_type, "value", recommendation.source_type)
            if source_type != RecommendationSourceType.STRATEGY.value:
                continue
            snapshot = recommendation.signal_snapshot if isinstance(recommendation.signal_snapshot, dict) else {}
            side, _ = self._scorecard_preferred_row(snapshot)
            if not self._ensure_atomic_capital_deployment_submission(snapshot, side=side):
                continue
            recommendation.signal_snapshot = snapshot
            self._canonicalize_snapshot_final_contract(
                snapshot,
                side=side,
                rank=self._snapshot_opportunity_rank(snapshot, side),
            )
            action, lots = self._lots_action_from_target(
                self._contract_current_lots(snapshot),
                self._contract_target_lots(snapshot),
            )
            recommendation.action = action
            recommendation.lots = lots
            self.db.update_futures_recommendation_status(
                recommendation.id,
                recommendation.status,
                action=recommendation.action,
                lots=recommendation.lots,
                signal_snapshot=snapshot,
            )

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
            side, _ = self._scorecard_preferred_row(snapshot)
            self._canonicalize_snapshot_final_contract(
                snapshot,
                side=side,
                rank=self._snapshot_opportunity_rank(snapshot, side),
            )
            snapshot["auditor"] = {
                "producer": "auditor",
                "audit_status": audit_output.audit_status,
                "audit_verdict": audit_output.audit_verdict,
                "audit_reason_codes": list(audit_output.audit_reason_codes),
                "audited_at": audit_output.audited_at,
                "independent_auditor_agent": True,
                "pm_risk_gate_is_not_auditor": True,
            }
            recommendation.signal_snapshot = snapshot
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

    def _write_daily_opportunity_ranks(self, generated: List[Tuple[str, FuturesRecommendation]]) -> None:
        """Compatibility wrapper for tests and older callers.

        The current behavior is stronger than rank writeback: PM applies the
        daily full-market capital deployment queue before phase2 can execute.
        """
        self._apply_daily_capital_deployment(generated)

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
        signal_snapshot = final_state.get("signal_snapshot") or {}

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
            signal_snapshot=signal_snapshot,
            warning_message=warning_message,
            status=status,
        )

    def _build_missing_pre_open_reference_signals(
        self,
        ticker: str,
        analysts: list[str],
        morning_price_context,
    ) -> list[AnalystSignal]:
        warning_message = (
            getattr(morning_price_context, "warning_message", None)
            if morning_price_context is not None else None
        )
        reason = warning_message or "pre_open_reference_price_unavailable"
        signals: list[AnalystSignal] = []
        for analyst in analysts:
            normalized_analyst = self._normalize_analyst_name(analyst)
            signal = AnalystSignal(
                agent_name=normalized_analyst,
                signal=Signal.NEUTRAL,
                confidence=0.0,
                justification=(
                    f"{ticker} cannot form a tradable Phase1 setup because the pre-open "
                    f"reference price is unavailable: {reason}"
                ),
                data_cutoff="pre_open",
                no_lookahead_status="ok",
                determinism_mode="deterministic_data_gate",
                horizon_class="flat",
                analyst_horizon="flat",
                decision_horizon="flat",
                execution_horizon="flat",
                validation_horizon="flat",
                expected_horizon_days=0,
                market_regime="unknown",
                setup_type="data_unavailable_no_trade",
                data_freshness="missing",
                evidence_quality="low",
                business_quality_score=0.0,
                data_coverage_score=0.0,
                tradeability_reason="pre_open_reference_price_unavailable",
                opportunity_type="no_trade",
                opportunity_state="no_opportunity",
                setup_quality_score=0.0,
                entry_quality="poor",
                setup_quality_notes=["pre_open_reference_price_unavailable"],
                entry_trigger="none",
                exit_hint="none",
                holding_period_hint="flat",
                factor_focus=["pandaai_market_data"],
                neutral_reason="pre_open_reference_price_unavailable",
                missing_evidence=["pre_open_reference_price"],
                would_change_view_if="PandaAI returns a valid previous trading day close for Phase1 planning",
                neutral_opportunity_bucket="low_tradeability",
                neutral_trigger_condition="valid_pre_open_reference_price",
                counterfactual_side="flat",
                neutral_watchlist_priority="none",
                do_not_trade_reason="pre_open_reference_price_unavailable",
                metadata={
                    "data_usage_summary": {
                        "ticker": ticker,
                        "analyst": normalized_analyst,
                        "pandaai_pre_open_reference": {
                            "available": False,
                            "used_in_signal": True,
                            "reason": reason,
                        },
                    },
                    "no_trade_reason": "pre_open_reference_price_unavailable",
                    "no_trade_category": "data",
                    "phase1_signal_contract": "complete_no_trade_signal",
                    "warning_message": warning_message,
                },
            )
            signals.append(signal)
        self._validate_phase1_analyst_outputs(ticker, analysts, signals)
        return signals

    def _build_signal_snapshot_from_signals(self, analyst_signals: list[AnalystSignal]) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {}
        for signal in analyst_signals or []:
            analyst = self._normalize_analyst_name(getattr(signal, "agent_name", ""))
            snapshot[analyst] = {
                "signal": getattr(getattr(signal, "signal", None), "value", getattr(signal, "signal", "")),
                "confidence": getattr(signal, "confidence", None),
                "horizon_class": getattr(signal, "horizon_class", "unknown"),
                "opportunity_state": getattr(signal, "opportunity_state", "watch_for_trigger"),
                "opportunity_type": getattr(signal, "opportunity_type", "unknown"),
                "setup_type": getattr(signal, "setup_type", "unknown"),
                "trigger_valid": getattr(signal, "trigger_valid", False),
                "tradeability_reason": getattr(signal, "tradeability_reason", ""),
                "neutral_reason": getattr(signal, "neutral_reason", ""),
                "metadata": getattr(signal, "metadata", {}) or {},
            }
        return snapshot

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
        self._validate_phase1_analyst_outputs(
            str(state.get("ticker") or ""),
            list(state.get("enabled_analysts") or self.workflow_analysts),
            list(state.get("analyst_signals") or []),
        )
        collector_output = signal_collector_agent(state)
        state.update(collector_output)
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
        current_shares = int(getattr(position, "shares", 0) or 0)
        source_type = getattr(recommendation.source_type, "value", recommendation.source_type)

        if source_type == RecommendationSourceType.STRATEGY.value:
            final_contract = (
                signal_snapshot.get("final_action_contract")
                if isinstance(signal_snapshot, dict) and isinstance(signal_snapshot.get("final_action_contract"), dict)
                else {}
            )
            if not final_contract:
                logger.warning(
                    f"{ticker}: strategy recommendation without final_action_contract is not "
                    "applied to the virtual Phase1 planning portfolio"
                )
                return portfolio
            target_lots = int(final_contract.get("target_lots") if final_contract.get("target_lots") is not None else current_shares)
        else:
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
                analyst_signals = self._build_missing_pre_open_reference_signals(
                    ticker=ticker,
                    analysts=self.current_analysts.copy(),
                    morning_price_context=morning_price_context,
                )
                missing_basis_state = {
                    "ticker": ticker,
                    "portfolio": portfolio,
                    "trading_date": self.trading_date,
                    "full_config": self.config,
                    "analyst_signals": analyst_signals,
                    "analyst_outputs": [],
                    "signal_snapshot": self._build_signal_snapshot_from_signals(analyst_signals),
                }
                self._timed_call(
                    timings,
                    "save_missing_basis_analyst_outputs",
                    self._save_prefetched_analyst_outputs,
                    missing_basis_state,
                )
                recommendation = self._coerce_phase1_recommendation(
                    ticker=ticker,
                    portfolio=portfolio,
                    decision=None,
                    morning_price_context=morning_price_context,
                    final_state=missing_basis_state,
                )
                recommendation_id = self.db.save_futures_recommendation(recommendation)
                if not recommendation_id:
                    raise RuntimeError(f"Failed to save futures recommendation for {ticker}")
                recommendation.id = recommendation_id
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
            recommendation.id = recommendation_id
            generated_recommendations.append((ticker, recommendation))

            logger.log_portfolio(f"{ticker} phase1 recommendation collected", portfolio)

            if self.planner_mode:
                self.current_analysts = None
            elif prefetched_analysis:
                self.current_analysts = self.workflow_analysts.copy()
            timings[f"{ticker}.total"] = perf_counter() - ticker_started_at

        self._validate_phase1_signal_persistence(portfolio, self.tickers)
        self._apply_daily_capital_deployment(generated_recommendations)
        self._audit_phase1_strategy_recommendations(generated_recommendations)
        portfolio = phase1_planning_portfolio
        for _, recommendation in generated_recommendations:
            if recommendation.status != RecommendationStatus.SKIPPED:
                portfolio = self._apply_virtual_recommendation_to_portfolio(portfolio, recommendation)
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

