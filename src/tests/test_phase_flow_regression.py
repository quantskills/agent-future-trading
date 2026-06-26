import json
import os
import hashlib
import sqlite3
import sys
import tempfile
import unittest
import yaml
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evaluation import (
    calculate_futures_metrics,
    calculate_futures_transaction_win_rate,
    evaluate_config,
)
from evaluation.evaluation import calculate_learning_usage_metrics, calculate_optimization_acceptance_metrics
from run import plot_config
from agents.decision_team.auditor import TradeAuditor, TradeAuditorInput
from apis.router import Router
from database.sqlite_helper import SQLiteDB
from database.evaluation_helper import EvaluationHelper
from database.build_check_db import rebuild_check_db, validate_check_db_consistency
from database.validate_artifacts import validate_artifacts
from graph.constants import Signal
from graph.schema import AnalystSignal, FuturesAction, FuturesDecision, FuturesRecommendation, FuturesTransaction, Portfolio, Position, RecommendationAction
from graph.schema import RecommendationSourceType, RecommendationStatus, TradingPhase
from agents.decision_team.portfolio_manager import (
    RiskLevel,
    _apply_capital_utilization_control,
    _apply_alpha_setup_ev_position_control,
    _apply_drawdown_and_ticker_loss_control,
    _apply_holding_rebalance_control,
    _apply_position_budget_policy_for_new_entry,
    _alpha_setup_action_value_trace,
    _final_contract_authority,
    _is_lifecycle_exit_required_reason,
    _minimum_real_probe_candidate_ratio,
    _positive_open_action_value_seed,
    _qualified_analyst_tradeable_probe_candidate,
    _qualified_real_probe_release,
    _negative_hold_or_positive_exit_action_value,
    _should_attempt_minimum_real_probe,
    _preserve_existing_lot_when_hold_ratio_survives,
    _apply_trade_frequency_control,
    _apply_winning_template_continuation_control,
    _build_phase1_recommendation,
    _build_pm_decision_context,
    _build_final_action_contract,
    _build_release_block_diagnostics,
    _canonical_action_evidence_contract,
    _validate_required_analyst_signals,
    _pm_new_entry_semantic_block_reason,
    _current_open_evidence_snapshot,
    _scorecard_probe_seed,
    _side_opportunity_state_summary,
    _append_unique_action_values,
    _normalize_alpha_setup_action_value,
    _select_learning_trace_action_values,
    portfolio_agent_futures,
)
from agents.execution_team.trader import (
    _execute_pending_forced_risk_before_strategy,
    _execution_contract_from_snapshot,
    _final_action_contract_from_snapshot,
    _setup_execution_learning_context,
)
from tools.agent_tools.analysis.quality import (
    apply_trade_research_contract,
    build_technical_context,
    summarize_news_events,
)
from tools.agent_tools.analysis.analyst_learning_calibration import calibrate_signal_with_learning_context
from tools.agent_tools.analysis.signal_fusion import build_opportunity_scorecard
from tools.agent_tools.decision.decision_memory_retrieval import retrieve_pm_memory
from run.order import _reconcile_rollover_with_strategy_target, _translate_pre_open_recommendation_to_order
from tools.agent_tools.execution.futures_execution import FuturesExecutionEngine
from tools.agent_tools.execution.futures_settlement import FuturesDailySettlement
from tools.agent_tools.execution.intraday_execution import select_intraday_execution
from tools.agent_tools.execution.entry_timing import phase2_entry_audit
from tools.agent_tools.research.phase4_review import (
    _apply_net_exposure_review,
    _build_daily_transaction_report,
    _build_capital_deployment_diagnostics,
    _collect_recommendation_quality_warnings,
    _data_combo_key,
    _failure_family_actions,
    _learning_mechanisms_from_recommendation,
    _loss_failure_family,
    _execution_result_from_snapshot,
    _validate_recommendation_execution_audit,
)
from tools.agent_tools.research.adaptive_policy_safety import filter_adaptive_policy_state_for_pm
from tools.agent_tools.decision.capital_allocator import adaptive_policy_record
from util.futures_audit import (
    build_audit_payload,
    build_execution_learning_trace,
    calculate_margin_audit,
    categorize_no_trade_reason,
    classify_zero_transaction_day,
    classify_no_trade_reasons,
    infer_target_lots,
    infer_no_trade_reason,
    normalize_no_trade_reason,
    set_execution_result,
)
from util.config_normalizer import normalize_config
from util.futures_trade_pairs import build_completed_trade_pairs, summarize_trade_pairs
from util.trading_calendar import get_previous_trading_day, map_datetime_to_futures_trading_day
from run.validate_phase_flow import _expected_settlement_balance_change
from tools.agent_tools.research.phase4_review import _expected_settlement_equity_change
from tools.agent_tools.execution.execution_exit_policy import evaluate_exit_policy
from tools.agent_tools.execution.order_semantics import (
    build_lot_intent_consistency,
    phase2_order_intent_from_lots,
    recommendation_intent_from_lots,
)
from tools.agent_tools.decision.reason_effects import reason_effect_summary
from apis.pandaai import PandaAIAPI
from graph.workflow import AgentWorkflow


class _FakeRouter:
    def __init__(self, trade_dates):
        self.trade_dates = trade_dates
        self.api = self

    def get_futures_daily_candles_optimized(self, underlying_code, is_main, start_date, end_date):
        quotes = []
        for trade_date in self.trade_dates:
            trade_dt = datetime.strptime(trade_date, "%Y-%m-%d")
            if start_date <= trade_dt <= end_date:
                quotes.append(SimpleNamespace(trade_date=trade_date))
        return quotes


class TradingCalendarRegressionTest(unittest.TestCase):
    def test_previous_trading_day_expands_lookback_window(self):
        router = _FakeRouter(["2025-01-27", "2025-02-05"])
        previous = get_previous_trading_day(
            router=router,
            trading_date="2025-02-05",
            underlying_code="RB",
            lookback_days=3,
            max_lookback_days=20,
        )
        self.assertEqual(previous.strftime("%Y-%m-%d"), "2025-01-27")

    def test_friday_night_session_maps_to_next_monday_trading_day(self):
        router = _FakeRouter(["2025-03-07", "2025-03-10"])
        trading_day = map_datetime_to_futures_trading_day(
            router=router,
            timestamp="2025-03-07 21:15:00",
            underlying_code="RB",
        )

        self.assertEqual(trading_day.strftime("%Y-%m-%d"), "2025-03-10")

    def test_day_session_keeps_calendar_trading_day(self):
        router = _FakeRouter(["2025-03-07", "2025-03-10"])
        trading_day = map_datetime_to_futures_trading_day(
            router=router,
            timestamp="2025-03-07 14:55:00",
            underlying_code="RB",
        )

        self.assertEqual(trading_day.strftime("%Y-%m-%d"), "2025-03-07")


class AgentBoundaryFidelityRegressionTest(unittest.TestCase):
    def test_analyst_to_pm_action_evidence_contract_overrides_raw_signal_state(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.88,
            trigger_valid=True,
            opportunity_state="tradeable_candidate",
            entry_trigger="raw immediate breakdown",
            metadata={
                "action_evidence_contract": {
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "opportunity_state": "watch_for_trigger",
                    "entry_trigger": "break below 3520 with volume confirmation",
                    "invalidation_present": True,
                    "invalidation_condition": "back above 3560",
                }
            },
        )

        contract = _canonical_action_evidence_contract(signal)
        state_summary = _side_opportunity_state_summary([signal], "short")

        self.assertFalse(contract["trigger_valid"])
        self.assertEqual(contract["opportunity_state"], "watch_for_trigger")
        self.assertFalse(state_summary["has_tradeable_support"])
        self.assertTrue(state_summary["has_watch_for_trigger_support"])
        self.assertEqual(state_summary["opportunity_state_counts"], {"watch_for_trigger": 1})

    def test_pm_to_trader_final_contract_preserves_authority_without_rank_execution_rights(self):
        final_contract = {
            "contract_type": "strategy",
            "final_action": "open_probe",
            "current_lots": 0,
            "target_lots": -2,
            "lots_delta": -2,
            "target_position_ratio": -0.012,
            "execution_profile": "intraday_pullback_confirmed",
            "trigger_source": "conditional_monitor",
            "entry_trigger": "trade below 3520 after pullback",
            "invalidation": "back above 3560",
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "authority_type": "conditional_monitor_probe",
            "max_allowed_margin_ratio": 0.015,
            "reason_codes": ["conditional_monitor_probe_seed"],
            "opportunity_rank": 1,
            "opportunity_score": 0.74,
            "opportunity_score_components": {"positive_learning": 0.2},
            "analyst_execution_roles": {
                "technical": {
                    "action_evidence_contract": {
                        "trigger_valid": False,
                        "opportunity_state": "watch_for_trigger",
                        "entry_trigger": "trade below 3520 after pullback",
                    },
                    "learning_scope": {
                        "consumer_scope": "analyst_calibration",
                        "learning_lane": "signal_calibration",
                    },
                }
            },
        }
        snapshot = {"final_action_contract": final_contract}

        contract = _final_action_contract_from_snapshot(snapshot)
        execution_contract = _execution_contract_from_snapshot(snapshot)
        learning_context = _setup_execution_learning_context(snapshot)

        self.assertEqual(contract["target_lots"], -2)
        self.assertEqual(contract["lots_delta"], -2)
        self.assertEqual(learning_context["final_action_contract"]["target_lots"], -2)
        self.assertEqual(learning_context["preferred_side"], "short")
        self.assertEqual(learning_context["consumer_scope"], "trader_execution_learning")
        self.assertEqual(learning_context["learning_lane"], "execution")
        self.assertEqual(execution_contract["execution_profile"], "intraday_pullback_confirmed")
        self.assertEqual(execution_contract["entry_trigger"], "trade below 3520 after pullback")
        self.assertNotIn("opportunity_rank", execution_contract)
        self.assertNotIn("opportunity_score", execution_contract)
        self.assertNotIn("opportunity_score_components", execution_contract)
        self.assertNotIn("target_lots", execution_contract)
        self.assertNotIn("lots_delta", execution_contract)

    def test_trader_to_researcher_execution_result_preserves_no_trade_fact_and_learning_scope(self):
        snapshot = {
            "ticker": "HC",
            "final_action_contract": {
                "contract_type": "strategy",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": -1,
                "lots_delta": -1,
                "execution_profile": "intraday_trigger_confirmed",
                "trigger_reason": "breakdown_trigger",
            },
        }

        set_execution_result(
            snapshot,
            outcome="skipped",
            status=RecommendationStatus.SKIPPED.value,
            transaction_count=0,
            no_trade_reason="intraday_trigger_not_met",
            warning_message="intraday_trigger_not_met",
        )
        expected_trace = build_execution_learning_trace(
            snapshot,
            outcome="skipped",
            status=RecommendationStatus.SKIPPED.value,
            no_trade_reason="intraday_trigger_not_met",
            no_trade_reason_category=snapshot["execution_result"]["no_trade_reason_category"],
            transaction_count=0,
        )

        result = _execution_result_from_snapshot(snapshot)
        trace = result["execution_learning_trace"]

        self.assertEqual(result["status"], RecommendationStatus.SKIPPED.value)
        self.assertEqual(result["transaction_count"], 0)
        self.assertEqual(result["no_trade_reason"], "intraday_trigger_not_met")
        self.assertEqual(trace, expected_trace)
        self.assertEqual(trace["consumer_scope"], "trader_execution_learning")
        self.assertEqual(trace["learning_lane"], "execution")
        self.assertIn("HC|intraday_trigger_confirmed|breakdown_trigger|execution", trace["execution_retrieval_key"])
        self.assertTrue(trace["turn_into_memory"])
        self.assertTrue(trace["not_direction_evidence"])


class Phase1SignalCompletenessRegressionTest(unittest.TestCase):
    def test_pm_rejects_missing_required_analyst_signal(self):
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.BULLISH, confidence=0.7),
            AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.2),
        ]

        with self.assertRaisesRegex(RuntimeError, "analyst signals incomplete"):
            _validate_required_analyst_signals(
                "PB",
                ["technical", "fundamental", "commodity_news"],
                signals,
            )

    def test_workflow_rejects_incomplete_parallel_analyst_outputs_before_pm(self):
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.BULLISH, confidence=0.7),
            AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.2),
        ]

        with self.assertRaisesRegex(RuntimeError, "analyst output incomplete before PM"):
            AgentWorkflow._validate_phase1_analyst_outputs(
                "SR",
                ["technical", "fundamental", "commodity_news"],
                signals,
            )

    def test_company_news_alias_counts_as_commodity_news(self):
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.BULLISH, confidence=0.7),
            AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.2),
            AnalystSignal(agent_name="company_news", signal=Signal.NEUTRAL, confidence=0.2),
        ]

        _validate_required_analyst_signals(
            "SR",
            ["technical", "fundamental", "commodity_news"],
            signals,
        )

    def test_missing_pre_open_reference_builds_complete_no_trade_signals(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        workflow.config = {"execution": {}}
        workflow.trading_date = datetime(2025, 1, 2)
        workflow.workflow_analysts = ["technical", "fundamental", "commodity_news"]

        context = SimpleNamespace(
            base_price=None,
            warning_message="ZN has no previous close available before 2025-01-02",
        )
        signals = workflow._build_missing_pre_open_reference_signals(
            ticker="ZN",
            analysts=workflow.workflow_analysts,
            morning_price_context=context,
        )

        self.assertEqual(len(signals), 3)
        self.assertEqual({signal.agent_name for signal in signals}, {"technical", "fundamental", "commodity_news"})
        self.assertTrue(all(signal.opportunity_state == "no_opportunity" for signal in signals))
        self.assertTrue(all(signal.tradeability_reason == "pre_open_reference_price_unavailable" for signal in signals))
        snapshot = workflow._build_signal_snapshot_from_signals(signals)
        self.assertEqual(set(snapshot), {"technical", "fundamental", "commodity_news"})
        self.assertEqual(snapshot["technical"]["metadata"]["no_trade_category"], "data")

    def test_virtual_phase1_portfolio_uses_final_contract_not_internal_draft(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        portfolio = Portfolio(
            id="p1",
            cashflow=1_000_000,
            positions={"BU": Position(shares=0, value=0)},
        )
        recommendation = FuturesRecommendation(
            config_id="cfg",
            reference_portfolio_id="p1",
            trading_date="2025-03-03",
            source_type=RecommendationSourceType.STRATEGY,
            underlying_code="BU",
            action=RecommendationAction.OPEN_SHORT,
            lots=8,
            base_price=3000.0,
            signal_snapshot={
                "pm_internal_draft": {
                    "target_lots": -8,
                    "reference_price": 2990.0,
                },
                "final_action_contract": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": -1,
                    "lots_delta": -1,
                    "target_position_ratio": -0.01,
                },
            },
        )

        with patch(
            "graph.workflow.FuturesContractInfoCache.get_contract_info",
            return_value={
                "contract_multiplier": 10,
                "margin_rate_long": 0.1,
                "margin_rate_short": 0.1,
            },
        ):
            updated = workflow._apply_virtual_recommendation_to_portfolio(portfolio, recommendation)

        self.assertEqual(updated.positions["BU"].shares, -1)
        self.assertEqual(updated.positions["BU"].entry_price, 3000.0)

    def test_virtual_phase1_portfolio_does_not_apply_strategy_without_final_contract(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        portfolio = Portfolio(
            id="p1",
            cashflow=1_000_000,
            positions={"BU": Position(shares=0, value=0)},
        )
        recommendation = FuturesRecommendation(
            config_id="cfg",
            reference_portfolio_id="p1",
            trading_date="2025-03-03",
            source_type=RecommendationSourceType.STRATEGY,
            underlying_code="BU",
            action=RecommendationAction.OPEN_SHORT,
            lots=8,
            base_price=3000.0,
            signal_snapshot={"pm_internal_draft": {"target_lots": -8}},
        )

        with patch(
            "graph.workflow.FuturesContractInfoCache.get_contract_info",
            return_value={
                "contract_multiplier": 10,
                "margin_rate_long": 0.1,
                "margin_rate_short": 0.1,
            },
        ):
            updated = workflow._apply_virtual_recommendation_to_portfolio(portfolio, recommendation)

        self.assertEqual(updated.positions["BU"].shares, 0)

    def test_workflow_applies_daily_full_market_capital_deployment_to_contracts(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        updates = []

        class _DB:
            def update_futures_recommendation_status(self, recommendation_id, status, signal_snapshot=None, **kwargs):
                updates.append((recommendation_id, status, signal_snapshot, dict(kwargs)))
                return True

        workflow.db = _DB()
        workflow.config = {
            "max_total_margin_ratio": 0.20,
            "position_budget_policy": {
                "min_real_trade_margin_ratio": 0.008,
                "max_single_ticker_margin_ratio": 0.13,
            },
            "capital_utilization_control": {"target_margin_ratio_confirmed": 0.008},
        }
        workflow.init_portfolio = Portfolio(
            id="p1",
            cashflow=5_000_000,
            positions={},
            margin_used=0.0,
            account_equity=5_000_000,
        )
        rec_low = FuturesRecommendation(
            id="low",
            status=RecommendationStatus.PENDING,
            underlying_code="A",
            base_price=3000.0,
            action=RecommendationAction.OPEN_SHORT,
            lots=1,
            signal_snapshot={
                "opportunity_scorecard": {
                    "preferred_side": "short",
                    "short": {"opportunity_score": 0.41, "opportunity_rank": 1},
                },
                "final_action_contract": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": -1,
                    "lots_delta": -1,
                    "target_margin_ratio_estimate": 0.008,
                    "evidence_used": {"opportunity_score": 0.41, "opportunity_rank": 1},
                },
                "active_opportunity_audit": {"opportunity": {"opportunity_rank": 1}},
            },
        )
        rec_high = FuturesRecommendation(
            id="high",
            status=RecommendationStatus.PENDING,
            underlying_code="B",
            base_price=3000.0,
            action=RecommendationAction.OPEN_SHORT,
            lots=2,
            signal_snapshot={
                "opportunity_scorecard": {
                    "preferred_side": "short",
                    "short": {"opportunity_score": 0.73, "opportunity_rank": 2},
                },
                "final_action_contract": {
                    "final_action": "open_real",
                    "current_lots": 0,
                    "target_lots": -2,
                    "lots_delta": -2,
                    "target_margin_ratio_estimate": 0.008,
                    "evidence_used": {"opportunity_score": 0.73, "opportunity_rank": 2},
                },
                "active_opportunity_audit": {"opportunity": {"opportunity_rank": 2}},
            },
        )

        workflow._write_daily_opportunity_ranks([("A", rec_low), ("B", rec_high)])

        self.assertEqual(rec_high.signal_snapshot["opportunity_scorecard"]["short"]["opportunity_rank"], 1)
        self.assertEqual(rec_low.signal_snapshot["opportunity_scorecard"]["short"]["opportunity_rank"], 2)
        self.assertEqual(rec_high.signal_snapshot["final_action_contract"]["target_lots"], -2)
        self.assertEqual(rec_high.signal_snapshot["final_action_contract"]["lots_delta"], -2)
        self.assertEqual(rec_high.signal_snapshot["final_action_contract"]["final_action"], "open_real")
        self.assertEqual(rec_high.action, RecommendationAction.OPEN_SHORT)
        self.assertEqual(rec_high.lots, 2)
        self.assertTrue(rec_high.signal_snapshot["final_action_contract"]["capital_deployment"]["selected_for_capital_deployment"])
        self.assertEqual(rec_high.signal_snapshot["final_action_contract"]["evidence_used"]["opportunity_rank"], 1)
        self.assertEqual(rec_low.signal_snapshot["final_action_contract"]["target_lots"], 0)
        self.assertEqual(rec_low.signal_snapshot["final_action_contract"]["lots_delta"], 0)
        self.assertEqual(rec_low.action, RecommendationAction.HOLD)
        self.assertEqual(rec_low.lots, 0)
        self.assertFalse(rec_low.signal_snapshot["final_action_contract"]["capital_deployment"]["selected_for_capital_deployment"])
        self.assertIn(
            "not_selected_by_full_market_pm_capital_queue",
            rec_low.signal_snapshot["final_action_contract"]["evidence_used"]["capital_allocation_reason"],
        )
        self.assertEqual(rec_low.signal_snapshot["active_opportunity_audit"]["opportunity"]["opportunity_rank"], 2)
        self.assertEqual(len(updates), 2)
        update_by_id = {item[0]: item for item in updates}
        self.assertEqual(update_by_id["high"][3]["action"], RecommendationAction.OPEN_SHORT)
        self.assertEqual(update_by_id["high"][3]["lots"], 2)
        self.assertEqual(update_by_id["low"][3]["action"], RecommendationAction.HOLD)
        self.assertEqual(update_by_id["low"][3]["lots"], 0)

    def test_workflow_learning_rank_changes_final_contract_or_explains_no_effect(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        updates = []

        class _DB:
            def update_futures_recommendation_status(self, recommendation_id, status, signal_snapshot=None, **kwargs):
                updates.append((recommendation_id, status, signal_snapshot, dict(kwargs)))
                return True

        workflow.db = _DB()
        workflow.config = {
            "max_total_margin_ratio": 0.20,
            "position_budget_policy": {
                "min_real_trade_margin_ratio": 0.008,
                "max_single_ticker_margin_ratio": 0.13,
            },
            "capital_utilization_control": {"target_margin_ratio_confirmed": 0.008},
        }
        workflow.init_portfolio = Portfolio(
            id="p1",
            cashflow=1_000_000,
            positions={},
            margin_used=0.0,
            account_equity=1_000_000,
        )
        rec_positive = FuturesRecommendation(
            id="positive-learning",
            status=RecommendationStatus.PENDING,
            underlying_code="EB",
            base_price=3000.0,
            action=RecommendationAction.OPEN_LONG,
            lots=2,
            signal_snapshot={
                "opportunity_scorecard": {
                    "preferred_side": "long",
                    "long": {
                        "opportunity_score": 0.66,
                        "opportunity_score_components": {
                            "positive_learning": 0.14,
                            "negative_learning": 0.0,
                            "execution_profile_learning": 0.04,
                            "recent_tail_loss_penalty": 0.0,
                        },
                    },
                },
                "final_action_contract": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": 2,
                    "lots_delta": 2,
                    "target_margin_ratio_estimate": 0.008,
                    "evidence_used": {
                        "opportunity_score": 0.66,
                        "opportunity_score_components": {
                            "positive_learning": 0.14,
                            "negative_learning": 0.0,
                            "execution_profile_learning": 0.04,
                            "recent_tail_loss_penalty": 0.0,
                        },
                    },
                    "learning_used": {
                        "alpha_setup_action_values": [
                            {
                                "action_preference": "positive_candidate_open",
                                "reward_sum": 4200.0,
                                "reward_mean": 1400.0,
                                "win_rate": 0.67,
                                "reward_source": "trade_episode",
                                "evidence_scope": "exact_real_state",
                                "action_value_lane": "open",
                            }
                        ]
                    },
                },
            },
        )
        rec_negative = FuturesRecommendation(
            id="negative-learning",
            status=RecommendationStatus.PENDING,
            underlying_code="TA",
            base_price=3000.0,
            action=RecommendationAction.OPEN_SHORT,
            lots=2,
            signal_snapshot={
                "opportunity_scorecard": {
                    "preferred_side": "short",
                    "short": {
                        "opportunity_score": 0.48,
                        "opportunity_score_components": {
                            "positive_learning": 0.0,
                            "negative_learning": -0.15,
                            "execution_profile_learning": 0.0,
                            "recent_tail_loss_penalty": -0.08,
                        },
                    },
                },
                "final_action_contract": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": -2,
                    "lots_delta": -2,
                    "target_margin_ratio_estimate": 0.008,
                    "evidence_used": {
                        "opportunity_score": 0.48,
                        "opportunity_score_components": {
                            "positive_learning": 0.0,
                            "negative_learning": -0.15,
                            "execution_profile_learning": 0.0,
                            "recent_tail_loss_penalty": -0.08,
                        },
                    },
                    "learning_used": {
                        "alpha_setup_action_values": [
                            {
                                "action_preference": "tail_loss_protect",
                                "reward_sum": -3600.0,
                                "reward_mean": -1200.0,
                                "win_rate": 0.0,
                                "reward_source": "trade_episode",
                                "evidence_scope": "exact_real_state",
                                "action_value_lane": "open",
                            }
                        ]
                    },
                },
            },
        )

        workflow._write_daily_opportunity_ranks([("EB", rec_positive), ("TA", rec_negative)])

        positive_contract = rec_positive.signal_snapshot["final_action_contract"]
        negative_contract = rec_negative.signal_snapshot["final_action_contract"]
        self.assertEqual(positive_contract["evidence_used"]["opportunity_rank"], 1)
        self.assertEqual(negative_contract["evidence_used"]["opportunity_rank"], 2)
        self.assertEqual(positive_contract["target_lots"], 2)
        self.assertEqual(positive_contract["lots_delta"], 2)
        self.assertEqual(rec_positive.action, RecommendationAction.OPEN_LONG)
        self.assertEqual(negative_contract["target_lots"], 0)
        self.assertEqual(negative_contract["lots_delta"], 0)
        self.assertEqual(rec_negative.action, RecommendationAction.HOLD)
        self.assertIn(
            "not_selected_by_full_market_pm_capital_queue",
            negative_contract["capital_deployment"]["capital_allocation_reason"],
        )
        self.assertEqual(len(updates), 2)


class ResearchLearningMechanismRegressionTest(unittest.TestCase):
    def test_learning_mechanisms_use_final_action_contract_trace(self):
        recommendation = {
            "signal_snapshot": json.dumps(
                {
                    "final_action_contract": {
                        "reason_codes": ["adaptive_policy_cap"],
                        "learning_used": {
                            "exploratory_learning_context": {
                                "memory_trace": {
                                    "selected_memory_refs": [
                                        {
                                            "policy_type": "loss_template_policy",
                                            "policy_action": "cap",
                                            "position_authority": "risk_reduction_conditioned",
                                        }
                                    ]
                                }
                            },
                            "adaptive_policy_state": {
                                "policies": [
                                    {
                                        "policy_type": "learning_mechanism:technical_parameter_calibration",
                                        "policy_action": "protect",
                                    },
                                    {
                                        "policy_type": "tail_loss_sentinel",
                                        "policy_action": "cap",
                                    },
                                ]
                            },
                        },
                    },
                    "technical": {
                        "metadata": {
                            "technical_parameter_calibration": {"applied": [{"id": "cal-1"}]}
                        }
                    },
                }
            )
        }
        mechanisms = set(_learning_mechanisms_from_recommendation(recommendation))
        self.assertIn("loss_template_policy", mechanisms)
        self.assertIn("technical_parameter_calibration", mechanisms)
        self.assertIn("tail_loss_sentinel", mechanisms)

    def test_learning_mechanisms_ignore_pm_internal_draft_trace(self):
        recommendation = {
            "signal_snapshot": json.dumps(
                {
                    "pm_internal_draft": {
                        "strategy_controls": {
                            "reasons": ["adaptive_policy_cap"],
                            "diagnostics": {
                                "exploratory_learning_context": {
                                    "memory_trace": {
                                        "selected_memory_refs": [
                                            {"policy_type": "loss_template_policy"}
                                        ]
                                    }
                                }
                            },
                        },
                    },
                    "final_action_contract": {
                        "reason_codes": [],
                        "learning_used": {},
                    },
                }
            )
        }

        mechanisms = set(_learning_mechanisms_from_recommendation(recommendation))

        self.assertNotIn("loss_template_policy", mechanisms)

    def test_loss_failure_family_is_scope_context_not_product_rule(self):
        data_usage = {
            "analysts": {
                "commodity_news": {
                    "sources": {
                        "finoview_news_txt": {
                            "available": True,
                            "used_in_signal": True,
                            "info_cutoff": "pre_open",
                        }
                    }
                }
            }
        }
        data_combo = _data_combo_key(data_usage)
        family = _loss_failure_family(
            "long_news_event_probe_event_short",
            "event_short",
            "range",
            data_combo,
        )
        actions = _failure_family_actions(family)
        joined = " ".join(actions["analysis"] + actions["trading"]).lower()
        self.assertEqual(family, "news_event_probe_failure")
        self.assertIn("catalyst", joined)
        self.assertNotIn("blacklist", joined)


class AnalystStrategyQualityRegressionTest(unittest.TestCase):
    def test_news_direction_without_strong_catalyst_stays_watch_for_trigger(self):
        news_item = SimpleNamespace(
            title="inventory edges lower",
            content="spot trading remains average and the market is waiting for further confirmation",
            publish_time="2025-01-02 08:30:00",
        )
        context = summarize_news_events([news_item], "BU")
        signal = AnalystSignal(
            agent_name="commodity_news",
            signal=Signal.BULLISH,
            confidence=0.70,
            horizon_class="event_short",
            business_quality_score=0.70,
            entry_trigger="news_event_trigger",
        )

        signal = apply_trade_research_contract(signal, context, analyst="commodity_news", ticker="BU")

        self.assertFalse(context["tradable_event"])
        self.assertEqual(signal.opportunity_state, "watch_for_trigger")
        self.assertIn("news_event_not_tradable_catalyst", signal.current_evidence_conflict)

    def test_technical_trend_in_choppy_state_is_not_tradeable_candidate(self):
        context = build_technical_context(
            "RB",
            {
                "trend": Signal.BULLISH,
                "macd": Signal.BULLISH,
                "adx": Signal.BEARISH,
                "rsi": Signal.BEARISH,
                "mean_reversion": Signal.NEUTRAL,
            },
            {"trend_strength": 19, "volatility": 0.20, "volume_ratio": 0.75, "price_range": 0.04},
        )
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.70,
            horizon_class="short",
            opportunity_type="trend_continuation",
            business_quality_score=0.75,
            invalidation_level=100.0,
        )

        signal = apply_trade_research_contract(signal, context, analyst="technical", ticker="RB")

        self.assertIn(context["market_regime"], {"choppy", "range", "weak_trend"})
        self.assertEqual(signal.opportunity_state, "watch_for_trigger")
        self.assertIn("technical_trend_requires_regime_confirmation", signal.current_evidence_conflict)

    def test_medium_fundamental_anchor_without_short_trigger_is_watch_for_trigger(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BEARISH,
            confidence=0.75,
            horizon_class="medium",
            business_quality_score=0.80,
            opportunity_type="medium_fundamental",
        )
        context = {"tradeability": "high", "risk_flags": [], "factor_group_counts": {"inventory": {"up": 2}}}

        signal = apply_trade_research_contract(signal, context, analyst="fundamental", ticker="J")

        self.assertEqual(signal.opportunity_state, "watch_for_trigger")
        self.assertIn(
            "fundamental_anchor_requires_short_trigger_and_invalidation",
            signal.current_evidence_conflict,
        )

    def test_medium_fundamental_anchor_with_explicit_short_trigger_can_be_tradeable(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BEARISH,
            confidence=0.75,
            horizon_class="medium",
            business_quality_score=0.80,
            opportunity_type="medium_fundamental",
            entry_trigger="short entry only after intraday breakdown below support with volume confirmation",
            would_change_view_if="short invalid if price closes back above the breakdown area",
        )
        context = {
            "tradeability": "high",
            "risk_flags": [],
            "factor_group_counts": {"inventory": {"up": 2}, "basis": {"down": 1}},
            "data_quality": {"supports_fundamental_trade_setup": True},
            "short_term_trigger_confirmed": True,
        }

        signal = apply_trade_research_contract(signal, context, analyst="fundamental", ticker="J")

        self.assertEqual(signal.opportunity_state, "probe_candidate")
        self.assertIn(
            "fundamental_anchor_has_short_trigger_and_invalidation",
            signal.current_evidence_conflict,
        )

    def test_pm_blocks_watch_for_trigger_new_entry_without_hard_product_rule(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.75,
            horizon_class="short",
            opportunity_state="watch_for_trigger",
            business_quality_score=0.70,
        )
        ratio, reasons, _notes, diagnostics = _apply_holding_rebalance_control(
            ticker="EB",
            trading_date="2025-01-03",
            position_ratio=0.05,
            current_ratio=0.0,
            current_position=None,
            analyst_signals=[signal],
            long_scores={"score": 0.70, "confidence": 0.70},
            short_scores={"score": 0.0, "confidence": 0.0},
            market_confirmation={"confirmation_score": 0.70},
            full_config={
                "portfolio_manager": {
                    "holding_rebalance_control": {
                        "enabled": True,
                        "watch_for_trigger_new_entry": {"enabled": True, "allow_probe": False},
                    }
                }
            },
            fusion_context={},
            risk_level=RiskLevel.SAFE,
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("pm_watch_for_trigger_not_tradeable", reasons)
        self.assertTrue(
            diagnostics["holding_rebalance_control"]["opportunity_state_summary"]["opportunity_state_counts"].get(
                "watch_for_trigger"
            )
        )

    def test_analyst_learning_calibration_improves_evidence_quality_without_trade_authority(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.50,
            business_quality_score=0.50,
            factor_alignment_score=0.50,
            horizon_class="short",
        )
        calibrated = calibrate_signal_with_learning_context(
            signal,
            analyst="technical",
            ticker="RB",
            learning_context={
                "alpha_setup_action_values": [
                    {
                        "ticker": "RB",
                        "side": "long",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "setup_type": "trend_breakout_setup",
                        "action_name": "open",
                        "sample_count": 8,
                        "reward_mean": 1200.0,
                        "reward_sum": 9600.0,
                        "win_rate": 0.75,
                        "confidence_score": 0.70,
                        "action_preference": "controlled_open_or_add",
                        "signal_calibration": {
                            "usable_by": ["analysis_team"],
                            "allowed_effects": ["evidence_quality_calibration", "setup_reliability_context"],
                            "forbidden_effects": ["trade_authority", "lots", "margin_ratio", "direction_override"],
                            "source_action_value_lane": "open",
                            "calibration_bias": "positive_evidence_calibration",
                        },
                    }
                ]
            },
        )

        self.assertGreater(calibrated.business_quality_score, 0.50)
        self.assertGreater(calibrated.factor_alignment_score, 0.50)
        self.assertGreater(calibrated.confidence, 0.50)
        self.assertIn("trigger_reliability_positive", calibrated.setup_quality_notes)
        self.assertIn("technical_learning_calibration", calibrated.factor_focus)
        calibration = calibrated.metadata["analyst_learning_calibration"]
        self.assertGreater(calibration["positive_strength"], 0.0)
        self.assertEqual(calibration["negative_strength"], 0.0)
        self.assertIn("no_trade_authority", calibration["authority_boundary"])
        self.assertNotIn("authority_type", calibration)
        self.assertNotIn("lots", calibration)
        self.assertNotIn("margin_ratio", calibration)
        impact = calibrated.learning_impact_summary
        self.assertEqual(impact["contract_version"], "agentquant.analyst_learning_impact.v1")
        self.assertIn("RB:long:trend_breakout_setup:trend:open", impact["historical_support"])
        self.assertEqual(impact["historical_contradiction"], [])
        self.assertIn("no_trade_authority", impact["authority_boundary"])
        self.assertNotIn("lots", impact)
        self.assertNotIn("margin_ratio", impact)
        self.assertEqual(calibrated.metadata["learning_impact_summary"], impact)

    def test_analyst_learning_calibration_marks_negative_same_scope_without_product_ban(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BEARISH,
            confidence=0.62,
            business_quality_score=0.62,
            factor_alignment_score=0.55,
            horizon_class="medium",
        )
        calibrated = calibrate_signal_with_learning_context(
            signal,
            analyst="fundamental",
            ticker="J",
            learning_context={
                "alpha_setup_action_values": [
                    {
                        "ticker": "J",
                        "side": "short",
                        "horizon_class": "medium",
                        "market_regime": "trend",
                        "setup_type": "inventory_basis_short_setup",
                        "action_name": "open",
                        "sample_count": 6,
                        "reward_mean": -1500.0,
                        "reward_sum": -9000.0,
                        "win_rate": 0.20,
                        "confidence_score": 0.65,
                        "action_preference": "cap_revalidate_before_open",
                        "signal_calibration": {
                            "usable_by": ["analysis_team"],
                            "allowed_effects": ["evidence_quality_calibration", "setup_reliability_context"],
                            "forbidden_effects": ["trade_authority", "lots", "margin_ratio", "direction_override"],
                            "source_action_value_lane": "open",
                            "calibration_bias": "negative_evidence_calibration",
                        },
                    },
                    {
                        "ticker": "RB",
                        "side": "short",
                        "horizon_class": "medium",
                        "market_regime": "trend",
                        "setup_type": "inventory_basis_short_setup",
                        "action_name": "open",
                        "sample_count": 12,
                        "reward_mean": 900.0,
                        "reward_sum": 10800.0,
                        "win_rate": 0.70,
                        "confidence_score": 0.75,
                        "action_preference": "controlled_open_or_add",
                        "signal_calibration": {
                            "usable_by": ["analysis_team"],
                            "allowed_effects": ["evidence_quality_calibration", "setup_reliability_context"],
                            "forbidden_effects": ["trade_authority", "lots", "margin_ratio", "direction_override"],
                            "source_action_value_lane": "open",
                            "calibration_bias": "positive_evidence_calibration",
                        },
                    },
                ]
            },
        )

        self.assertLess(calibrated.business_quality_score, 0.62)
        self.assertIn("factor_reliability_negative", calibrated.setup_quality_notes)
        self.assertIn("fundamental_broad_prior_weak_only", calibrated.setup_quality_notes)
        self.assertIn("fundamental_same_scope_negative_learning", calibrated.current_evidence_conflict)
        calibration = calibrated.metadata["analyst_learning_calibration"]
        self.assertEqual(calibration["same_ticker_matched_count"], 1)
        self.assertEqual(calibration["broad_prior_matched_count"], 1)
        self.assertIn("no_trade_authority", calibration["authority_boundary"])
        impact = calibrated.learning_impact_summary
        self.assertIn("J:short:inventory_basis_short_setup:trend:open", impact["historical_contradiction"])
        self.assertIn("RB:short:inventory_basis_short_setup:trend:open", impact["historical_support"])
        self.assertIn("no_trade_authority", impact["authority_boundary"])
        factor_summary = calibrated.factor_calibration_summary
        self.assertEqual(factor_summary["contract_version"], "agentquant.factor_calibration.v1")
        self.assertIn("fundamental_learning_calibration", factor_summary["effective_factors"])
        self.assertIn("fundamental_same_scope_negative_learning", factor_summary["stale_or_conflicting_factors"])
        self.assertIn("no_trade_authority", factor_summary["authority_boundary"])
        self.assertEqual(calibrated.metadata["factor_calibration_summary"], factor_summary)

    def test_analyst_learning_calibration_ignores_raw_action_value_without_signal_calibration(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.50,
            business_quality_score=0.50,
            factor_alignment_score=0.50,
            horizon_class="short",
        )
        calibrated = calibrate_signal_with_learning_context(
            signal,
            analyst="technical",
            ticker="RB",
            learning_context={
                "alpha_setup_action_values": [
                    {
                        "ticker": "RB",
                        "side": "long",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "setup_type": "trend_breakout_setup",
                        "action_name": "open",
                        "sample_count": 8,
                        "reward_mean": 1200.0,
                        "reward_sum": 9600.0,
                        "win_rate": 0.75,
                        "confidence_score": 0.70,
                        "action_preference": "positive_candidate_open",
                    }
                ]
            },
        )

        self.assertEqual(calibrated.business_quality_score, 0.50)
        self.assertEqual(calibrated.factor_alignment_score, 0.50)
        self.assertEqual(calibrated.confidence, 0.50)
        calibration = calibrated.metadata["analyst_learning_calibration"]
        self.assertFalse(calibration["enabled"])
        self.assertEqual(calibration["positive_strength"], 0.0)
        self.assertEqual(calibration["negative_strength"], 0.0)

    def test_news_learning_calibration_writes_event_summary_without_trade_authority(self):
        signal = AnalystSignal(
            agent_name="commodity_news",
            signal=Signal.BULLISH,
            confidence=0.55,
            business_quality_score=0.50,
            factor_alignment_score=0.50,
            horizon_class="event_short",
            event_type="supply_disruption",
            impact_window_days=2,
            trigger_valid=False,
            entry_trigger="wait for price and volume confirmation",
            factor_focus=["supply_disruption_event"],
        )
        calibrated = calibrate_signal_with_learning_context(
            signal,
            analyst="commodity_news",
            ticker="BU",
            learning_context={
                "alpha_setup_action_values": [
                    {
                        "ticker": "BU",
                        "side": "long",
                        "horizon_class": "event_short",
                        "market_regime": "event_window",
                        "setup_type": "news_event_probe",
                        "action_name": "open",
                        "sample_count": 3,
                        "reward_mean": 800.0,
                        "win_rate": 0.67,
                        "confidence_score": 0.55,
                        "signal_calibration": {
                            "usable_by": ["analysis_team"],
                            "allowed_effects": ["evidence_quality_calibration", "setup_reliability_context"],
                            "forbidden_effects": ["trade_authority", "lots", "margin_ratio", "direction_override"],
                            "source_action_value_lane": "open",
                            "calibration_bias": "positive_evidence_calibration",
                        },
                    }
                ]
            },
        )

        event_summary = calibrated.event_calibration_summary
        self.assertEqual(event_summary["contract_version"], "agentquant.event_calibration.v1")
        self.assertIn("supply_disruption_event", event_summary["effective_catalysts"])
        self.assertEqual(event_summary["impact_window_assessment"], "2")
        self.assertTrue(event_summary["price_volume_confirmation_required"])
        self.assertIn("BU:long:news_event_probe:event_window:open", event_summary["supporting_learning_scopes"])
        self.assertIn("no_trade_authority", event_summary["authority_boundary"])
        self.assertNotIn("lots", event_summary)
        self.assertNotIn("margin_ratio", event_summary)
        self.assertEqual(calibrated.metadata["event_calibration_summary"], event_summary)

    def test_pm_opportunity_summary_reads_analyst_opportunity_state(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.58,
            business_quality_score=0.58,
            opportunity_state="probe_candidate",
            entry_trigger="break above opening range with volume",
            invalidation_level=3320.0,
            trigger_valid=True,
            invalidation_present=True,
        )

        summary = _side_opportunity_state_summary([technical], "long")

        self.assertEqual(summary["supporting_signal_count"], 1)
        self.assertEqual(summary["opportunity_state_counts"]["probe_candidate"], 1)
        self.assertTrue(summary["has_probe_candidate_support"])
        self.assertTrue(summary["has_tradeable_support"])

class OpportunityScorecardLearningRegressionTest(unittest.TestCase):
    def _tradeable_signal(self) -> AnalystSignal:
        return AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.72,
            business_quality_score=0.70,
            setup_quality_score=0.76,
            opportunity_state="tradeable_candidate",
            entry_trigger="current breakdown confirmed below support",
            invalidation_level=105.0,
            trigger_valid=True,
            invalidation_present=True,
            metadata={
                "action_evidence_contract": {
                    "setup_quality_ok": True,
                    "trigger_valid": True,
                    "current_trigger_confirmed": True,
                    "opportunity_state": "tradeable_candidate",
                    "entry_trigger": "current breakdown confirmed below support",
                    "invalidation_present": True,
                    "invalidation_condition": "short invalid if price closes above 105",
                }
            },
        )

    def _action_value(
        self,
        *,
        action_preference: str,
        lane: str,
        reward_mean: float,
        reward_sum: float,
        worst_reward: float | None = None,
        reward_source: str = "trade_episode",
        scope: str = "exact_real_state",
        last_sample_date: str = "2025-03-12",
        sample_count: int = 4,
    ) -> dict:
        return {
            "ticker": "TA",
            "side": "short",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "trend_breakout_setup",
            "action_name": lane,
            "sample_count": sample_count,
            "reward_mean": reward_mean,
            "reward_sum": reward_sum,
            "worst_reward": worst_reward if worst_reward is not None else reward_mean,
            "confidence_score": 0.70,
            "action_preference": action_preference,
            "last_sample_date": last_sample_date,
            "payload": {
                "source": "alpha_setup_profile_action_value",
                "action_value_lane": lane,
                "action_preference": action_preference,
                "reward_source": reward_source,
                "amplification_scope_quality": scope,
                "episode_trade_reward_count": sample_count if "episode" in reward_source else 0,
                "real_trade_reward_count": sample_count,
                "last_sample_date": last_sample_date,
            },
        }

    def _scorecard_config(self) -> dict:
        return {
            "tradeable_threshold": 0.58,
            "deployable_threshold": 0.72,
            "weak_confirmation_threshold": 0.45,
            "score_component_weights": {
                "positive_learning": 0.12,
                "negative_learning": 0.16,
                "execution_profile_learning": 0.10,
                "recent_tail_loss_penalty": 0.18,
            },
            "learning_reward_unit": 1000.0,
            "learning_full_weight_sample_count": 3,
            "learning_recency_half_life_days": 10,
            "learning_recency_floor": 0.20,
            "tail_loss_reward_threshold": -1000.0,
        }

    def test_episode_action_values_move_scorecard_rank_without_product_blacklist(self):
        signal = self._tradeable_signal()
        positive = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[
                self._action_value(
                    action_preference="positive_candidate_open",
                    lane="open",
                    reward_mean=1600.0,
                    reward_sum=6400.0,
                )
            ],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )
        negative = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[
                self._action_value(
                    action_preference="tail_loss_protect",
                    lane="open",
                    reward_mean=-1800.0,
                    reward_sum=-7200.0,
                    worst_reward=-2400.0,
                )
            ],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )

        positive_components = positive["short"]["opportunity_score_components"]
        negative_components = negative["short"]["opportunity_score_components"]
        self.assertGreater(positive_components["positive_learning"], 0.0)
        self.assertLess(negative_components["negative_learning"], 0.0)
        self.assertLess(negative_components["recent_tail_loss_penalty"], 0.0)
        self.assertGreater(positive["short"]["opportunity_score"], negative["short"]["opportunity_score"])
        self.assertNotIn("product_blacklist", json.dumps(negative["short"], ensure_ascii=False))

    def test_pm_action_value_normalizer_preserves_canonical_fields_from_payload_or_top_level(self):
        payload_only = {
            "ticker": "TA",
            "side": "short",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "trend_breakout_setup",
            "action_name": "open",
            "sample_count": 3,
            "reward_sum": -3600.0,
            "reward_mean": -1200.0,
            "win_rate": 0.0,
            "last_sample_date": "2025-03-14",
            "payload": {
                "action_preference": "tail_loss_protect",
                "reward_source": "trade_episode",
                "amplification_scope_quality": "exact_real_state",
                "action_value_lane": "open",
            },
        }
        normalized = _normalize_alpha_setup_action_value(payload_only)

        self.assertEqual(normalized["action_preference"], "tail_loss_protect")
        self.assertEqual(normalized["reward_source"], "trade_episode")
        self.assertEqual(normalized["evidence_scope"], "exact_real_state")
        self.assertEqual(normalized["action_value_lane"], "open")
        self.assertEqual(normalized["consumer_scope"], "pm_learning")
        self.assertTrue(normalized["canonical_action_value"])

    def test_pm_action_value_merge_preserves_canonical_researcher_record(self):
        canonical = {
            "id": "av-hc-execution-real",
            "scope_key": "HC|short|short|choppy|breakdown",
            "ticker": "HC",
            "side": "short",
            "horizon_class": "short",
            "market_regime": "choppy",
            "setup_type": "breakdown",
            "action_name": "execution",
            "action_value_lane": "execution",
            "learning_lane": "execution",
            "consumer_scope": "pm_learning",
            "action_preference": "positive_candidate_execution",
            "reward_source": "real_trade",
            "evidence_scope": "exact_real_state",
            "reward_mean": 199.62,
            "reward_sum": 199.62,
            "win_rate": 1.0,
            "sample_count": 1,
        }
        empty_trace = {
            "scope_key": "HC|short|short|choppy|breakdown",
            "ticker": "HC",
            "side": "short",
            "horizon_class": "short",
            "market_regime": "choppy",
            "setup_type": "breakdown",
            "action_name": "execution",
            "action_value_lane": "execution",
            "consumer_scope": "pm_learning",
        }

        cases = (([empty_trace], [canonical]), ([canonical], [empty_trace]))
        for base_rows, extra_rows in cases:
            merged = _append_unique_action_values(base_rows, extra_rows)
            trace = _select_learning_trace_action_values(merged, limit=3)

            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["id"], "av-hc-execution-real")
            self.assertEqual(merged[0]["action_preference"], "positive_candidate_execution")
            self.assertEqual(merged[0]["reward_source"], "real_trade")
            self.assertEqual(merged[0]["evidence_scope"], "exact_real_state")
            self.assertEqual(merged[0]["reward_mean"], 199.62)
            self.assertEqual(trace[0]["id"], "av-hc-execution-real")
            self.assertEqual(trace[0]["action_preference"], "positive_candidate_execution")

    def test_pm_action_value_retrieval_uses_fallback_key_without_scope_drift(self):
        class FakeDB:
            def __init__(self):
                self.calls = []

            def get_alpha_setup_action_values(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("setup_type") == "trend_breakout_setup":
                    return []
                if kwargs.get("horizon_class") == "short" and kwargs.get("setup_type") is None:
                    return [
                        {
                            "scope_key": "TA|short|short|range|execution_pullback_setup|open",
                            "ticker": "TA",
                            "side": "short",
                            "horizon_class": "short",
                            "market_regime": "range",
                            "setup_type": "execution_pullback_setup",
                            "action_name": "open",
                            "sample_count": 2,
                            "reward_sum": -3600.0,
                            "reward_mean": -1800.0,
                            "win_rate": 0.0,
                            "action_preference": "tail_loss_protect",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "action_value_lane": "open",
                            "consumer_scope": "pm_learning",
                            "last_sample_date": "2025-03-03",
                        }
                    ]
                return []

        result = retrieve_pm_memory(
            db=FakeDB(),
            config_id="cfg",
            ticker="TA",
            side="short",
            horizon_class="short",
            market_regime="trend",
            setup_type="trend_breakout_setup",
            trading_date="2025-03-04",
        )
        rows = result["action_values"]
        detail = result["effective_memory_summary"]
        attempts = result["retrieval_attempts"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["consumer_scope"], "pm_learning")
        self.assertEqual(rows[0]["retrieval_match_level"], "same_ticker_side_horizon")
        self.assertEqual(detail["effective_row_count"], 1)
        self.assertEqual(attempts[0]["match_level"], "exact_state")
        self.assertEqual(attempts[0]["row_count"], 0)

    def test_pm_action_value_retrieval_fills_missing_lanes_after_exact_match(self):
        class FakeDB:
            def __init__(self):
                self.calls = []

            def get_alpha_setup_action_values(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("setup_type") == "news_event_setup":
                    return [
                        {
                            "id": "m-open-exact",
                            "scope_key": "M|long|short|trend|news_event_setup|open",
                            "ticker": "M",
                            "side": "long",
                            "horizon_class": "short",
                            "market_regime": "trend",
                            "setup_type": "news_event_setup",
                            "action_name": "open",
                            "sample_count": 1,
                            "reward_sum": 770.34,
                            "reward_mean": 770.34,
                            "win_rate": 1.0,
                            "confidence_score": 0.65,
                            "action_preference": "positive_candidate_open",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "action_value_lane": "open",
                            "consumer_scope": "pm_learning",
                            "learning_lane": "open",
                            "last_sample_date": "2025-03-04",
                        }
                    ]
                if kwargs.get("horizon_class") == "short" and kwargs.get("setup_type") is None:
                    return [
                        {
                            "id": "m-execution-fallback",
                            "scope_key": "M|long|short|trend|execution_pullback_setup|execution",
                            "ticker": "M",
                            "side": "long",
                            "horizon_class": "short",
                            "market_regime": "trend",
                            "setup_type": "execution_pullback_setup",
                            "action_name": "execution",
                            "sample_count": 1,
                            "reward_sum": 770.34,
                            "reward_mean": 770.34,
                            "win_rate": 1.0,
                            "confidence_score": 0.62,
                            "action_preference": "positive_candidate_execution",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "action_value_lane": "execution",
                            "consumer_scope": "pm_learning",
                            "learning_lane": "execution",
                            "last_sample_date": "2025-03-03",
                        }
                    ]
                return []

        result = retrieve_pm_memory(
            db=FakeDB(),
            config_id="cfg",
            ticker="M",
            side="long",
            horizon_class="short",
            market_regime="trend",
            setup_type="news_event_setup",
            trading_date="2025-03-11",
        )
        rows = result["action_values"]
        detail = result["effective_memory_summary"]
        attempts = result["retrieval_attempts"]

        ids = {row["id"] for row in rows}
        self.assertIn("m-open-exact", ids)
        self.assertIn("m-execution-fallback", ids)
        self.assertEqual(attempts[0]["row_count"], 1)
        self.assertIn("exact_state", detail["matched_levels"])
        self.assertIn("same_ticker_side_horizon", detail["matched_levels"])
        self.assertGreaterEqual(len(attempts), 2)

    def test_pm_action_value_retrieval_real_history_not_blocked_by_empty_lane(self):
        class FakeDB:
            def __init__(self):
                self.calls = []

            def get_alpha_setup_action_values(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("setup_type") == "execution_pullback_setup":
                    return []
                if kwargs.get("horizon_class") == "medium" and kwargs.get("setup_type") is None:
                    return [
                        {
                            "scope_key": "BU|short|medium|choppy|execution_breakout_setup|execution",
                            "ticker": "BU",
                            "side": "short",
                            "horizon_class": "medium",
                            "market_regime": "choppy",
                            "setup_type": "execution_breakout_setup",
                            "action_name": "execution",
                            "action_value_lane": "execution",
                            "learning_lane": "execution",
                            "consumer_scope": "pm_learning",
                        },
                        {
                            "scope_key": "BU|short|medium|choppy|news_event_setup|open",
                            "ticker": "BU",
                            "side": "short",
                            "horizon_class": "medium",
                            "market_regime": "choppy",
                            "setup_type": "news_event_setup",
                            "action_name": "open",
                            "action_value_lane": "open",
                            "learning_lane": "open",
                            "consumer_scope": "pm_learning",
                        },
                    ]
                if kwargs.get("horizon_class") is None and kwargs.get("setup_type") is None:
                    return [
                        {
                            "id": "bu-real-execution",
                            "scope_key": "BU|short|short|choppy|execution_pullback_setup|execution",
                            "ticker": "BU",
                            "side": "short",
                            "horizon_class": "short",
                            "market_regime": "choppy",
                            "setup_type": "execution_pullback_setup",
                            "action_name": "execution",
                            "action_value_lane": "execution",
                            "learning_lane": "execution",
                            "consumer_scope": "pm_learning",
                            "action_preference": "positive_candidate_execution",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "reward_sum": 5581.76,
                            "reward_mean": 5581.76,
                            "win_rate": 1.0,
                            "sample_count": 1,
                            "last_sample_date": "2025-03-04",
                        },
                        {
                            "id": "bu-real-open",
                            "scope_key": "BU|short|short|choppy|news_event_setup|open",
                            "ticker": "BU",
                            "side": "short",
                            "horizon_class": "short",
                            "market_regime": "choppy",
                            "setup_type": "news_event_setup",
                            "action_name": "open",
                            "action_value_lane": "open",
                            "learning_lane": "open",
                            "consumer_scope": "pm_learning",
                            "action_preference": "positive_candidate_open",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "reward_sum": 5581.76,
                            "reward_mean": 5581.76,
                            "win_rate": 1.0,
                            "sample_count": 1,
                            "last_sample_date": "2025-03-04",
                        },
                    ]
                return []

        db = FakeDB()
        result = retrieve_pm_memory(
            db=db,
            config_id="cfg",
            ticker="BU",
            side="short",
            horizon_class="medium",
            market_regime="choppy",
            setup_type="execution_pullback_setup",
            trading_date="2025-03-05",
        )
        rows = result["action_values"]
        detail = result["effective_memory_summary"]
        attempts = result["retrieval_attempts"]

        ids = [row.get("id") for row in rows]
        self.assertIn("bu-real-execution", ids)
        self.assertIn("bu-real-open", ids)
        self.assertNotIn("bu-empty-execution", ids)
        self.assertNotIn("bu-empty-open", ids)
        real_execution = next(row for row in rows if row.get("id") == "bu-real-execution")
        self.assertEqual(real_execution["action_preference"], "positive_candidate_execution")
        self.assertEqual(real_execution["retrieval_match_level"], "same_ticker_side")
        self.assertTrue(detail["empty_history_cannot_block_real_history"])
        self.assertGreaterEqual(detail["empty_shell_count"], 2)
        self.assertEqual(attempts[1]["row_count"], 2)
        self.assertEqual(attempts[2]["row_count"], 2)

    def test_scorecard_ignores_non_pm_learning_scope(self):
        signal = self._tradeable_signal()
        pm_row = self._action_value(
            action_preference="tail_loss_protect",
            lane="open",
            reward_mean=-1800.0,
            reward_sum=-7200.0,
            worst_reward=-2400.0,
        )
        trader_row = {
            **pm_row,
            "consumer_scope": "trader_execution_learning",
            "payload": {
                **pm_row.get("payload", {}),
                "consumer_scope": "trader_execution_learning",
            },
        }
        pm_scorecard = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[pm_row],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )
        trader_scope_scorecard = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[trader_row],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )

        self.assertLess(pm_scorecard["short"]["opportunity_score_components"]["negative_learning"], 0.0)
        self.assertEqual(
            trader_scope_scorecard["short"]["opportunity_score_components"]["negative_learning"],
            0.0,
        )

    def test_pm_scorecard_rejects_compressed_trace_as_learning_but_uses_canonical_row(self):
        signal = self._tradeable_signal()
        compressed_trace = {
            "ticker": "TA",
            "side": "short",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "trend_breakout_setup",
            "action_name": "open",
            "sample_count": 4,
            "confidence_score": 0.70,
            "valid_until": "2025-04-01",
            "signal_calibration": {"source_action_value_lane": "open"},
        }
        canonical_tail_loss = {
            **compressed_trace,
            "reward_mean": -1800.0,
            "reward_sum": -7200.0,
            "worst_reward": -2400.0,
            "action_preference": "tail_loss_protect",
            "last_sample_date": "2025-03-14",
            "payload": {
                "action_preference": "tail_loss_protect",
                "reward_source": "trade_episode",
                "amplification_scope_quality": "exact_real_state",
                "action_value_lane": "open",
                "real_trade_reward_count": 4,
            },
        }

        compressed_scorecard = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[compressed_trace],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )
        canonical_scorecard = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[canonical_tail_loss],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )

        self.assertEqual(
            compressed_scorecard["short"]["opportunity_score_components"]["negative_learning"],
            0.0,
        )
        self.assertLess(
            canonical_scorecard["short"]["opportunity_score_components"]["negative_learning"],
            0.0,
        )
        self.assertLess(
            canonical_scorecard["short"]["opportunity_score"],
            compressed_scorecard["short"]["opportunity_score"],
        )

    def test_execution_profile_learning_is_a_score_component_not_trader_authority(self):
        signal = self._tradeable_signal()
        positive_execution = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[
                self._action_value(
                    action_preference="positive_candidate_execution",
                    lane="execution",
                    reward_mean=1200.0,
                    reward_sum=4800.0,
                )
            ],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )
        negative_execution = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[
                self._action_value(
                    action_preference="negative_revalidate",
                    lane="execution",
                    reward_mean=-1200.0,
                    reward_sum=-4800.0,
                    worst_reward=-1600.0,
                )
            ],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )

        self.assertGreater(
            positive_execution["short"]["opportunity_score_components"]["execution_profile_learning"],
            0.0,
        )
        self.assertLess(
            negative_execution["short"]["opportunity_score_components"]["execution_profile_learning"],
            0.0,
        )
        self.assertNotIn("target_lots", positive_execution["short"])
        self.assertNotIn("lots_delta", positive_execution["short"])

    def test_recent_tail_loss_offsets_stale_alpha_profile_bonus(self):
        signal = self._tradeable_signal()
        scorecard = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_profiles=[
                {
                    "side": "short",
                    "setup_type": "trend_breakout_setup",
                    "lifecycle_state": "deployable",
                    "sample_count": 8,
                    "net_pnl": 9000.0,
                    "confidence_score": 0.82,
                }
            ],
            alpha_setup_action_values=[
                self._action_value(
                    action_preference="tail_loss_protect",
                    lane="open",
                    reward_mean=-2500.0,
                    reward_sum=-5000.0,
                    worst_reward=-3200.0,
                    last_sample_date="2025-03-14",
                    sample_count=2,
                )
            ],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )

        components = scorecard["short"]["opportunity_score_components"]
        self.assertLess(components["recent_tail_loss_penalty"], 0.0)
        self.assertLessEqual(components["alpha_profile_adjustment"], 0.03)
        summary = scorecard["short"]["learning_adjustment_summary"]
        self.assertGreater(summary["recent_tail_loss_signal"], 0.0)
        self.assertEqual(summary["not_trade_authority"], True)


class Phase1RecommendationSnapshotRegressionTest(unittest.TestCase):
    class _PMTestDB:
        def get_ticker_performance(self, **kwargs):
            return {}

        def get_futures_transaction_memory(self, *args, **kwargs):
            return []

    def test_existing_short_unchanged_is_hold_not_open_short(self):
        intent = recommendation_intent_from_lots(current_lots=-1, target_lots=-1)

        self.assertEqual(intent["action"], "hold")
        self.assertEqual(intent["lots"], 0)
        self.assertEqual(intent["action_type"], "keep")

    def test_existing_short_increase_uses_delta_open_short(self):
        intent = recommendation_intent_from_lots(current_lots=-1, target_lots=-3)

        self.assertEqual(intent["action"], "open_short")
        self.assertEqual(intent["lots"], 2)
        self.assertEqual(intent["action_type"], "increase")

    def test_phase2_execution_consistency_matches_target_delta(self):
        intent = phase2_order_intent_from_lots(current_lots=2, target_lots=-3)
        diagnostics = build_lot_intent_consistency(
            current_lots=2,
            target_lots=-3,
            action="close_long",
            lots=5,
            mode="phase2_execution",
        )

        self.assertEqual(intent["action"], "close_long")
        self.assertEqual(intent["lots"], 5)
        self.assertTrue(intent["requires_two_step_reversal"])
        self.assertEqual(diagnostics["status"], "ok")

    def test_phase1_recommendation_uses_actual_analyst_signal_artifacts(self):
        portfolio = Portfolio(id="portfolio-1", cashflow=1_000_000, positions={})
        decision = FuturesDecision(ticker="BU", action=FuturesAction.HOLD, lots=0, justification="hold")
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.5),
            AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.6),
        ]

        recommendation = _build_phase1_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="BU",
            trading_date="2025-01-02",
            contract_code="BU2506.SHF",
            decision=decision,
            morning_price_context=None,
            analyst_signals=signals,
            plan_snapshot={"decision_horizon": "medium", "validation_horizon": "medium"},
        )

        header = recommendation.signal_snapshot["artifact_contract"]
        self.assertEqual(
            header["source_artifacts"],
            ["AnalystSignalArtifact:technical", "AnalystSignalArtifact:fundamental"],
        )
        self.assertEqual(
            sorted(recommendation.signal_snapshot["horizon_scope"]["analyst_horizons"]),
            ["fundamental", "technical"],
        )

    def test_release_block_diagnostics_are_observation_only_and_do_not_mutate_contract(self):
        contract = _build_final_action_contract(
            ticker="RB",
            current_lots=0,
            target_lots=0,
            position_ratio=0.0,
            margin_required=0.0,
            account_equity=1_000_000.0,
            lots_to_trade=0,
            lots_to_trade_reason="market_confirmation_below_release_threshold",
            recommendation_intent=recommendation_intent_from_lots(0, 0),
            final_entry_authority={
                "authority_type": "watchlist_only",
                "open_action_evidence": False,
                "watch_for_trigger_block": False,
            },
            control_reasons=["market_confirmation_below_release_threshold"],
            control_diagnostics={},
            opportunity_scorecard={
                "preferred_side": "short",
                "short": {"final_state": "tradeable_candidate", "score": 0.71},
            },
            market_confirmation={"confirmation_score": 0.54, "status": "weak"},
            alpha_setup_action_values=[],
            execution_contract_fields={},
        )
        before = json.loads(json.dumps(contract, sort_keys=True))

        diagnostics = _build_release_block_diagnostics(
            ticker="RB",
            final_action_contract=contract,
            final_entry_authority={
                "authority_type": "watchlist_only",
                "open_action_evidence": False,
                "watch_for_trigger_block": False,
            },
            control_reasons=["market_confirmation_below_release_threshold"],
            lots_to_trade_reason="market_confirmation_below_release_threshold",
            control_diagnostics={},
            opportunity_scorecard={
                "preferred_side": "short",
                "short": {"final_state": "tradeable_candidate", "score": 0.71},
            },
            market_confirmation={"confirmation_score": 0.54, "status": "weak"},
            full_config={
                "position_budget_policy": {
                    "probe_margin_ratio": 0.008,
                    "probe_margin_max_ratio": 0.015,
                    "min_real_trade_margin_ratio": 0.02,
                },
                "portfolio": {
                    "alpha_setup_ev_fusion": {
                        "min_confirmation_score": 0.70,
                        "require_tradeable_support_for_release": True,
                        "require_invalidation_for_release": True,
                    }
                },
            },
        )

        self.assertEqual(contract, before)
        self.assertTrue(diagnostics["observation_only"])
        self.assertTrue(diagnostics["does_not_modify_trade_authority"])
        self.assertEqual(diagnostics["single_source_of_trade_truth_remains"], "final_action_contract")
        serialized = json.dumps(diagnostics, sort_keys=True)
        for forbidden in (
            "authority_type",
            "target_lots",
            "lots_delta",
            "target_position_ratio",
            "open_action_evidence",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_phase1_recommendation_carries_release_diagnostics_outside_final_contract(self):
        portfolio = Portfolio(id="portfolio-1", cashflow=1_000_000, positions={})
        decision = FuturesDecision(ticker="BU", action=FuturesAction.HOLD, lots=0, justification="hold")
        diagnostics = {
            "contract_version": "agentquant.release_block_diagnostics.v1",
            "observation_only": True,
            "does_not_modify_trade_authority": True,
            "single_source_of_trade_truth_remains": "final_action_contract",
            "primary_block_reason": "market_confirmation_below_release_threshold",
        }
        final_contract = _build_final_action_contract(
            ticker="BU",
            current_lots=0,
            target_lots=0,
            position_ratio=0.0,
            margin_required=0.0,
            account_equity=1_000_000.0,
            lots_to_trade=0,
            lots_to_trade_reason="market_confirmation_below_release_threshold",
            recommendation_intent=recommendation_intent_from_lots(0, 0),
            final_entry_authority={"authority_type": "watchlist_only"},
            control_reasons=["market_confirmation_below_release_threshold"],
            control_diagnostics={},
            opportunity_scorecard={},
            market_confirmation={},
            alpha_setup_action_values=[],
            execution_contract_fields={},
        )

        recommendation = _build_phase1_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="BU",
            trading_date="2025-01-02",
            contract_code="BU2506.SHF",
            decision=decision,
            morning_price_context=None,
            analyst_signals=[AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.5)],
            plan_snapshot={
                "decision_horizon": "short",
                "validation_horizon": "short",
                "release_block_diagnostics": diagnostics,
            },
            final_action_contract=final_contract,
        )

        snapshot = recommendation.signal_snapshot
        self.assertEqual(snapshot["release_block_diagnostics"], diagnostics)
        self.assertNotIn("release_block_diagnostics", snapshot["final_action_contract"])

    def test_phase1_recommendation_final_contract_is_explicit_not_pm_draft(self):
        portfolio = Portfolio(id="portfolio-1", cashflow=1_000_000, positions={})
        decision = FuturesDecision(
            ticker="BU",
            action=FuturesAction.OPEN_SHORT,
            lots=1,
            price=3000.0,
            settle_price=3000.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="BU2506.SHF",
            justification="open from explicit final contract",
        )
        explicit_contract = {
            "contract_version": "agentquant.final_action.v1",
            "ticker": "BU",
            "final_action": "open_probe",
            "current_lots": 0,
            "target_lots": -1,
            "lots_delta": -1,
            "lots_delta_abs": 1,
            "reason_codes": "explicit_contract",
            "target_position_ratio": -0.01,
            "authority_type": "exploration_probe",
            "open_action_evidence": False,
            "strong_current_evidence": False,
            "open_action_evidence": True,
            "strong_current_evidence": True,
            "reason_codes": ["explicit_contract"],
            "consistency": {"status": "ok"},
            "single_source_of_trade_truth": True,
        }

        recommendation = _build_phase1_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="BU",
            trading_date="2025-01-02",
            contract_code="BU2506.SHF",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=3000.0,
                base_price_source=None,
                base_price_date="2025-01-02",
                open_price=3000.0,
                prev_close_price=3010.0,
                warning_message=None,
            ),
            analyst_signals=[],
            plan_snapshot={
                "target_lots": -8,
                "target_position_ratio": -0.08,
                "final_action_contract": {
                    "final_action": "open_real",
                    "current_lots": 0,
                    "target_lots": -8,
                    "lots_delta": -8,
                    "reason_codes": "stale_pm_draft",
                    "consistency": {"status": "stale"},
                },
                "recommendation_position_consistency": {"status": "stale"},
                "strategy_controls": {"diagnostics": {}},
            },
            final_action_contract=explicit_contract,
        )

        snapshot = recommendation.signal_snapshot
        self.assertEqual(snapshot["final_action_contract"]["target_lots"], -1)
        self.assertEqual(snapshot["final_action_contract"]["reason_codes"], ["explicit_contract"])
        self.assertIn("recommendation_position_consistency=ok", recommendation.justification)
        self.assertNotIn("stale_pm_draft", recommendation.justification)
        self.assertNotIn("recommendation_position_consistency=stale", recommendation.justification)

    def test_phase1_dynamic_price_abnormal_hold_keeps_analyst_signal_snapshot(self):
        portfolio = Portfolio(
            id="portfolio-1",
            cashflow=1_000_000,
            account_equity=1_000_000,
            cash_available=1_000_000,
            positions={},
            margin_used=0.0,
            margin_available=1_000_000,
            margin_ratio=0.0,
        )
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.5),
            AnalystSignal(agent_name="fundamental", signal=Signal.BEARISH, confidence=0.6),
            AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.4),
        ]
        state = {
            "portfolio": portfolio,
            "ticker": "J",
            "trading_date": datetime(2025, 5, 9),
            "analyst_signals": signals,
            "llm_config": {"provider": "fake", "model": "fake"},
            "num_tickers": 15,
            "enabled_analysts": ["technical", "fundamental", "commodity_news"],
            "config_id": "cfg",
            "phase": "phase1",
            "morning_price_context": SimpleNamespace(
                base_price=500.0,
                base_price_source="t_minus_1_close_fallback",
                base_price_date="2025-05-08",
                open_price=None,
                prev_close_price=1478.0,
                warning_message=None,
            ),
            "config": {"max_total_margin_ratio": 0.20, "max_single_margin_ratio": 0.12},
            "full_config": {
                "learning": {"enabled": False},
                "max_total_margin_ratio": 0.20,
                "max_single_margin_ratio": 0.12,
            },
            "router": None,
        }

        with patch(
            "agents.decision_team.portfolio_manager.get_db",
            return_value=self._PMTestDB(),
        ):
            result = portfolio_agent_futures(state)

        recommendation = result["recommendation"]
        self.assertEqual(recommendation.underlying_code, "J")
        self.assertEqual(recommendation.action.value, "hold")
        snapshot = recommendation.signal_snapshot
        for analyst in ("technical", "fundamental", "commodity_news"):
            self.assertIn(analyst, snapshot)
            self.assertEqual(snapshot[analyst]["agent_name"], analyst)
        self.assertEqual(
            sorted(snapshot["artifact_contract"]["source_artifacts"]),
            [
                "AnalystSignalArtifact:commodity_news",
                "AnalystSignalArtifact:fundamental",
                "AnalystSignalArtifact:technical",
            ],
        )
        self.assertNotIn("pm_internal_draft", snapshot)
        contract = snapshot["final_action_contract"]
        self.assertIn("data_price_anomaly", contract["reason_codes"])
        self.assertEqual(contract["target_lots"], 0)
        self.assertEqual(contract["lots_delta"], 0)
        self.assertIn("data_price_anomaly", recommendation.justification)


class _FailingPreviousCloseAPI:
    def get_futures_daily_candles_optimized(self, underlying_code, is_main, start_date, end_date):
        return [SimpleNamespace(trade_date="2025-03-07")]

    def get_main_contract_quote_on_date(self, underlying_code, trading_date):
        raise RuntimeError("Network error: refused")


class _MissingOpenWithPreviousCloseAPI:
    def get_futures_daily_candles_optimized(self, underlying_code, is_main, start_date, end_date):
        return [SimpleNamespace(trade_date="2025-03-07")]

    def get_main_contract_quote_on_date(self, underlying_code, trading_date):
        if str(trading_date).startswith("2025-03-10"):
            return SimpleNamespace(open_price=None, pre_close_price=3500.0, close_price=None)
        return SimpleNamespace(open_price=3490.0, pre_close_price=3480.0, close_price=3510.0)


class RouterProviderFailureRegressionTest(unittest.TestCase):
    def test_pre_open_reference_price_soft_skips_when_provider_refuses_connection(self):
        router = Router.__new__(Router)
        router.api = _FailingPreviousCloseAPI()
        router.market_type = "china_futures"
        router.config = {}

        basis = router.resolve_pre_open_reference_price("RB", "2025-03-10")

        self.assertIsNone(basis.base_price)
        self.assertIsNone(basis.base_price_source)
        self.assertIn("provider unavailable", basis.warning_message)
        self.assertIn("has no previous close available", basis.warning_message)

    def test_pre_open_reference_price_does_not_mask_exact_quote_failure_with_history(self):
        router = Router.__new__(Router)
        router.api = _FailingPreviousCloseAPI()
        router.market_type = "china_futures"
        router.config = {}

        basis = router.resolve_pre_open_reference_price("RB", "2025-03-10")

        self.assertIsNone(basis.base_price)
        self.assertIsNone(basis.prev_close_price)
        self.assertIsNone(basis.base_price_date)
        self.assertIn("previous close quote provider unavailable", basis.warning_message)
        self.assertNotIn("continuous daily previous close fallback", basis.warning_message)

    def test_phase2_execution_price_does_not_use_previous_close_when_open_missing(self):
        router = Router.__new__(Router)
        router.api = _MissingOpenWithPreviousCloseAPI()
        router.market_type = "china_futures"
        router.config = {}

        basis = router.resolve_morning_execution_base_price("RB", "2025-03-10")

        self.assertIsNone(basis.base_price)
        self.assertIsNone(basis.base_price_source)
        self.assertIsNone(basis.base_price_date)
        self.assertEqual(basis.prev_close_price, 3500.0)
        self.assertIn("missing T-day open price", basis.warning_message)
        self.assertNotIn("previous close fallback", basis.warning_message)


class PandaAIContractNormalizationRegressionTest(unittest.TestCase):
    def test_czce_short_and_full_contract_codes_match_same_quote_row(self):
        api = PandaAIAPI.__new__(PandaAIAPI)
        row = {
            "symbol": "CF2505.CZC",
            "trading_code": "CF505",
            "dominant_id": "CF2505",
        }

        self.assertTrue(
            api._row_matches_contract(row, "CF2505", reference_date=datetime(2024, 12, 31))
        )
        self.assertTrue(
            api._row_matches_contract(row, "CF505", reference_date=datetime(2024, 12, 31))
        )

    def test_token_expiry_reauthenticates_once_before_retry(self):
        # Deterministic unit test: the provider object is a fake and no real
        # PandaAI credential, token, or network call is used here.
        class _FakePandaData:
            def __init__(self):
                self.init_calls = 0
                self.market_calls = 0

            def init_token(self, username, password):
                self.init_calls += 1

            def get_market_data(self, **kwargs):
                self.market_calls += 1
                if self.market_calls == 1:
                    raise RuntimeError("service returned error: code=200004 token expired")
                return [{"symbol": "ZN_DOMINANT.SHF", "date": "20250102", "close": 25265.0}]

        fake = _FakePandaData()
        api = PandaAIAPI.__new__(PandaAIAPI)
        api.username = "user"
        api.password = "password"
        api._panda_data = fake
        api._token_initialized = True
        api._retry_attempts = 1
        api._retry_initial_wait_seconds = 0.0
        api._retry_max_wait_seconds = 0.0
        api._network_retry_initial_wait_seconds = 0.0
        api._network_retry_max_wait_seconds = 0.0
        api._min_request_interval_seconds = 0.0
        original_shared = PandaAIAPI._shared_token_initialized
        PandaAIAPI._shared_token_initialized = True
        try:
            result = api._call_pandaai("get_market_data", symbol="ZN_DOMINANT.SHF")
        finally:
            PandaAIAPI._shared_token_initialized = original_shared

        self.assertEqual(result[0]["close"], 25265.0)
        self.assertEqual(fake.init_calls, 1)
        self.assertEqual(fake.market_calls, 2)


class _FailingSettlementRouter:
    def get_futures_contract_quote_on_date(self, contract_code, trading_date):
        raise RuntimeError("HTTP 403 provider blocked")


class _RolloverSettlementRouter:
    def get_futures_main_contract_quote_on_date(self, ticker, trading_date):
        return SimpleNamespace(ticker=f"{ticker}2601")


class _RolloverSettlementDb:
    def __init__(self):
        self.saved = []

    def get_futures_recommendations_by_effective_date(self, **kwargs):
        return []

    def save_futures_recommendation(self, recommendation):
        self.saved.append(recommendation)
        return "rollover-rec"


class FuturesSettlementStrictPriceRegressionTest(unittest.TestCase):
    def test_settlement_price_provider_failure_does_not_fallback(self):
        engine = FuturesDailySettlement.__new__(FuturesDailySettlement)
        engine.router = _FailingSettlementRouter()

        ledgers = {
            "BU": {
                "batches": [
                    {
                        "contract_code": "BU2506.SHF",
                        "daily_reference_price": 4400.0,
                    }
                ]
            }
        }

        with self.assertRaisesRegex(RuntimeError, "HTTP 403 provider blocked"):
            engine._fetch_contract_settle_prices(
                ledgers=ledgers,
                transactions=[],
                trading_date=datetime(2025, 1, 6),
            )

    @patch("tools.agent_tools.execution.futures_settlement.get_next_trading_day")
    def test_rollover_detected_after_settlement_is_scheduled_for_next_trading_day(self, mock_next_day):
        mock_next_day.return_value = datetime(2025, 3, 4)
        engine = FuturesDailySettlement.__new__(FuturesDailySettlement)
        engine.router = _RolloverSettlementRouter()
        engine.db = _RolloverSettlementDb()

        portfolio = Portfolio(
            id="pf",
            cashflow=1000000.0,
            margin_used=10000.0,
            positions={
                "RB": Position(
                    shares=2,
                    value=50000.0,
                    contract_code="RB2505",
                    margin_used=10000.0,
                )
            },
        )

        engine._detect_rollover_recommendations(
            config_id="cfg",
            portfolio=portfolio,
            trading_date=datetime(2025, 3, 3),
        )

        self.assertEqual(len(engine.db.saved), 1)
        recommendation = engine.db.saved[0]
        self.assertEqual(recommendation.trading_date, "2025-03-03")
        self.assertEqual(recommendation.effective_trade_date, "2025-03-04")
        self.assertEqual(recommendation.source_type, RecommendationSourceType.ROLLOVER)
        self.assertEqual(recommendation.from_contract, "RB2505")
        self.assertEqual(recommendation.to_contract, "RB2601")


class FuturesAuditRegressionTest(unittest.TestCase):
    def test_classify_no_trade_reasons(self):
        self.assertEqual(classify_no_trade_reasons(["neutral_signal_no_trade", "position_matched"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["position_matched", "cooling_period"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["cold_start_small_cap"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["trade_frequency_control", "weak_signal_combo"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["market_confirmation_quality_gate"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["weak_ticker_side_quality_gate"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["news_only_directional_trade"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["strategy_memory_weak_block"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["decision_planner_block"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["intraday_opening_range_incomplete"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["neutral_signal_no_trade", "missing_previous_close"]), "error")
        self.assertEqual(classify_no_trade_reasons([]), "unknown")
        self.assertEqual(classify_no_trade_reasons(["intraday_trigger_not_met"]), "expected")

    def test_no_trade_reason_research_categories(self):
        self.assertEqual(categorize_no_trade_reason("neutral_signal_no_trade")["category"], "signal")
        self.assertEqual(categorize_no_trade_reason("drawdown_control")["category"], "risk")
        self.assertEqual(categorize_no_trade_reason("intraday_trigger_not_met")["category"], "timing")
        self.assertEqual(categorize_no_trade_reason("limit_locked_no_fill")["category"], "execution")
        self.assertEqual(categorize_no_trade_reason("position_matched")["category"], "business")
        self.assertEqual(categorize_no_trade_reason("strategy_memory_weak_block")["category"], "learning")

    def test_infer_no_trade_reason_from_warning(self):
        snapshot = {"execution_result": {"no_trade_reason": "position_matched"}}
        self.assertEqual(infer_no_trade_reason(snapshot), "position_matched")
        stale_draft_snapshot = {"pm_internal_draft": {"reason_codes": "position_matched"}}
        self.assertIsNone(infer_no_trade_reason(stale_draft_snapshot))
        self.assertEqual(
            infer_no_trade_reason({}, warning_message="RB has no previous close available before 2025-02-05"),
            "missing_previous_close",
        )

    def test_legacy_decision_planner_reason_is_canonicalized(self):
        self.assertEqual(normalize_no_trade_reason("decision_planner_block"), "trade_auditor_block")
        snapshot = {"execution_result": {"no_trade_reason": "decision_planner_block"}}
        self.assertEqual(infer_no_trade_reason(snapshot), "trade_auditor_block")


class TradeAuditorRegressionTest(unittest.TestCase):
    def _auditor(self):
        return TradeAuditor(
            {
                "trade_auditor": {
                    "enabled": True,
                    "policy_version": "test_v1",
                    "learning_mode": "audit_only",
                    "attribution_feedback": {
                        "enabled": True,
                        "lookback_trades": 30,
                        "min_samples_soft": 5,
                        "min_samples_hard": 10,
                        "weak_win_rate_below": 0.40,
                        "severe_win_rate_below": 0.30,
                        "weak_total_pnl_below": -1500,
                        "severe_total_pnl_below": -5000,
                        "weak_combo_requires_confirmation_score": 0.60,
                        "block_severe_negative_combo": True,
                    },
                    "cold_start": {
                        "policy": "small_cap",
                        "max_position_ratio_multiplier": 0.50,
                        "block_weak_combo_new_entries": True,
                        "block_conflict_confirmation_below": 0.65,
                    },
                    "quality_gate": {
                        "enabled": True,
                        "min_supporting_analysts": 2,
                        "block_low_tradeability_count": 2,
                        "conflict_block_confirmation_below": 0.50,
                        "no_support_block_confirmation_below": 0.70,
                        "medium_quality_multiplier": 0.50,
                        "qualified_support_min_confidence": 0.45,
                        "protected_ticker_sides": {
                            "BU": {
                                "long": {
                                    "min_confirmation_score": 0.50,
                                    "weak_combo_multiplier": 0.50,
                                    "cold_start_multiplier": 0.50,
                                }
                            }
                        },
                        "weak_ticker_side_rules": {
                            "P": {
                                "long": {
                                    "min_confirmation_score": 0.65,
                                    "block_below_confirmation_score": 0.65,
                                    "min_qualified_supporters": 2,
                                    "cap_multiplier": 0.25,
                                    "block_signal_combos": [["Bearish", "Neutral", "Bullish"]],
                                }
                            }
                        },
                        "news_driver_control": {
                            "enabled": True,
                            "min_news_confidence": 0.60,
                            "min_market_confirmation_score": 0.60,
                            "min_freshness_score": 0.70,
                            "min_relevance_score": 0.70,
                            "block_when_core_opposes": True,
                            "cap_without_fundamental_confirmation": True,
                            "cap_multiplier": 0.50,
                        },
                    },
                },
                "market_confirmation": {
                    "enabled": True,
                    "quality_gate_enabled": True,
                    "min_confirmations_for_new_entry": 1,
                    "min_confirmation_score_for_new_entry": 0.45,
                    "min_confirmation_score_for_weak_combo": 0.60,
                    "max_conflicts_for_new_entry": 3,
                    "weak_signal_strength": 0.25,
                    "quality_gate_block_weak_signal": True,
                    "quality_gate_cap_multiplier": 0.50,
                    "conflict_cap_multiplier": 0.50,
                    "block_weak_conflicting_signal": True,
                    "allow_conflicted_probe_with_strong_confirmation": True,
                    "conflicted_probe_min_confirmation_score": 0.65,
                    "conflicted_probe_min_confirmations": 3,
                },
                "trade_frequency_control": {
                    "weak_cap_multiplier": 0.50,
                    "weak_signal_combos": [[
                        "Bullish",
                        "Bullish",
                        "Neutral",
                    ]],
                },
                "strategy_memory": {
                    "enabled": True,
                    "audit": {
                        "protected_min_confirmation_score": 0.50,
                        "protected_multiplier": 0.50,
                        "protected_min_sample_count": 5,
                        "protected_min_win_rate": 0.60,
                        "protected_min_net_pnl": 1000,
                        "watchlist_min_confirmation_score": 0.60,
                        "watchlist_cap_multiplier": 0.50,
                        "weak_block_min_confirmation_score": 0.65,
                        "weak_block_cap_multiplier": 0.35,
                        "min_qualified_supporters": 2,
                    },
                },
            }
        )

    def test_cold_start_reduces_without_blocking(self):
        output = self._auditor().plan(
            TradeAuditorInput(
                ticker="M",
                signal_combo=["Neutral", "Neutral", "Neutral"],
                raw_position_ratio=0.10,
                current_position_ratio=0.0,
                signal_strength=0.40,
                market_confirmation={"enabled": False},
            )
        )

        self.assertEqual(output.decision, "scale_down")
        self.assertIn("cold_start_small_cap", output.reasons)
        self.assertAlmostEqual(output.position_ratio_multiplier, 0.50)

    def test_severe_ticker_side_performance_limits_new_exposure_to_probe(self):
        output = self._auditor().plan(
            TradeAuditorInput(
                ticker="TA",
                signal_combo=["Bearish", "Neutral", "Bearish"],
                raw_position_ratio=-0.12,
                current_position_ratio=0.0,
                signal_strength=0.60,
                market_confirmation={"enabled": False},
                recent_ticker_side_performance={
                    "total_trades": 12,
                    "win_rate": 0.25,
                    "total_pnl": -6200,
                },
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertIn("side_performance_block", output.reasons)
        self.assertIn("soft_block_converted_to_probe_only", output.reasons)
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_weak_combo_with_strong_confirmation_is_allowed(self):
        output = self._auditor().plan(
            TradeAuditorInput(
                ticker="M",
                signal_combo=["Bullish", "Bullish", "Neutral"],
                raw_position_ratio=0.10,
                current_position_ratio=0.0,
                signal_strength=0.60,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.75,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
                recent_ticker_side_performance={
                    "total_trades": 8,
                    "win_rate": 0.62,
                    "total_pnl": 2500,
                },
                recent_conditional_performance={
                    "total_trades": 8,
                    "win_rate": 0.62,
                    "total_pnl": 2500,
                },
            )
        )

        self.assertEqual(output.decision, "allow")
        self.assertNotIn("weak_signal_combo", output.reasons)

    def test_conflicted_strong_confirmation_probe_is_reduced_not_blocked(self):
        output = self._auditor().plan(
            TradeAuditorInput(
                ticker="BU",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bullish", "confidence": 0.58, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Bullish", "confidence": 0.40, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "commodity_news", "signal": "Neutral", "confidence": 0.62, "metadata": {"tradeability": "medium"}},
                ],
                signal_combo=["Bullish", "Bullish", "Neutral"],
                raw_position_ratio=0.05,
                current_position_ratio=0.0,
                signal_strength=0.18,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.67,
                    "features": [{"feature": "basis"}, {"feature": "ls_ratio"}],
                    "confirmations": [
                        "variety_position_rank",
                        "symbol_position_rank",
                        "ls_ratio",
                        "broker_net_margin_change",
                    ],
                    "conflicts": ["basis", "broker_variety_profit"],
                },
            )
        )

        self.assertEqual(output.decision, "scale_down")
        self.assertIn("market_confirmation_conflict", output.reasons)
        self.assertIn("cold_start_small_cap", output.reasons)

    def test_protected_ticker_side_weak_combo_reduces_instead_of_blocks(self):
        output = self._auditor().plan(
            TradeAuditorInput(
                ticker="BU",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bullish", "confidence": 0.55, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Bullish", "confidence": 0.52, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "commodity_news", "signal": "Neutral", "confidence": 0.55, "metadata": {"tradeability": "medium"}},
                ],
                signal_combo=["Bullish", "Bullish", "Neutral"],
                raw_position_ratio=0.05,
                current_position_ratio=0.0,
                signal_strength=0.35,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.55,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
            )
        )

        self.assertEqual(output.decision, "scale_down")
        self.assertIn("protected_ticker_side_cold_start", output.reasons)
        self.assertNotEqual(output.position_ratio_multiplier, 0.0)

    def test_shallow_protected_memory_does_not_override_weak_combo_block(self):
        config = self._auditor().full_config
        config["trade_frequency_control"]["weak_signal_combos"] = [["Bullish", "Neutral", "Neutral"]]
        config["market_confirmation"]["conflicted_probe_min_confirmation_score"] = 0.90
        auditor = TradeAuditor(config)

        output = auditor.plan(
            TradeAuditorInput(
                ticker="M",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bullish", "confidence": 0.55, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Neutral", "confidence": 0.52, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "commodity_news", "signal": "Neutral", "confidence": 0.55, "metadata": {"tradeability": "medium"}},
                ],
                signal_combo=["Bullish", "Neutral", "Neutral"],
                raw_position_ratio=0.12,
                current_position_ratio=0.0,
                signal_strength=0.45,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.55,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": ["contract_daily_indicators"],
                },
                strategy_memory={
                    "side_memory": {
                        "memory_state": "protected",
                        "sample_count": 3,
                        "win_rate": 1.0,
                        "net_pnl": 2331.97,
                    }
                },
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertNotIn("protected_memory_evidence_rejected", output.reasons)
        self.assertIn("cold_start_weak_combo_block", output.reasons)
        self.assertEqual(
            output.diagnostics.get("research_memory_boundary"),
            "auditor_does_not_consume_research_records",
        )

    def test_weak_ticker_side_rule_limits_latest_bad_p_long_template_to_probe(self):
        output = self._auditor().plan(
            TradeAuditorInput(
                ticker="P",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bearish", "confidence": 0.58, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Neutral", "confidence": 0.35, "metadata": {"tradeability": "medium"}},
                    {
                        "agent_name": "commodity_news",
                        "signal": "Bullish",
                        "confidence": 0.70,
                        "metadata": {
                            "tradeability": "high",
                            "freshness_score": 0.90,
                            "relevance_score": 0.90,
                        },
                    },
                ],
                signal_combo=["Bearish", "Neutral", "Bullish"],
                raw_position_ratio=0.05,
                current_position_ratio=0.0,
                signal_strength=0.35,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.64,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertIn("weak_ticker_side_quality_gate", output.reasons)
        self.assertIn("soft_block_converted_to_probe_only", output.reasons)
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_news_only_directional_trade_limits_when_core_opposes(self):
        output = self._auditor().plan(
            TradeAuditorInput(
                ticker="M",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bearish", "confidence": 0.58, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Neutral", "confidence": 0.40, "metadata": {"tradeability": "medium"}},
                    {
                        "agent_name": "commodity_news",
                        "signal": "Bullish",
                        "confidence": 0.75,
                        "metadata": {
                            "tradeability": "high",
                            "freshness_score": 0.90,
                            "relevance_score": 0.90,
                        },
                    },
                ],
                signal_combo=["Bearish", "Neutral", "Bullish"],
                raw_position_ratio=0.05,
                current_position_ratio=0.0,
                signal_strength=0.35,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.75,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertIn("news_only_directional_trade", output.reasons)
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_strategy_memory_weak_block_limits_new_exposure_to_probe(self):
        output = self._auditor().plan(
            TradeAuditorInput(
                ticker="M",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bullish", "confidence": 0.58, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Neutral", "confidence": 0.40, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "commodity_news", "signal": "Neutral", "confidence": 0.35, "metadata": {"tradeability": "medium"}},
                ],
                signal_combo=["Bullish", "Neutral", "Neutral"],
                raw_position_ratio=0.05,
                current_position_ratio=0.0,
                signal_strength=0.35,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.55,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
                strategy_memory={
                    "combo": {
                        "memory_state": "weak_block",
                        "sample_count": 4,
                        "win_rate": 0.25,
                        "net_pnl": -3200,
                        "signal_combo": "[\"Bullish\", \"Neutral\", \"Neutral\"]",
                    },
                    "side_memory": None,
                    "records": [],
                },
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertNotIn("strategy_memory_weak_block", output.reasons)
        self.assertEqual(
            output.diagnostics.get("research_memory_boundary"),
            "auditor_does_not_consume_research_records",
        )
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_auditor_ignores_legacy_contextual_calibration_research_payload(self):
        config = self._auditor().full_config
        config["learning"] = {
            "contextual_rule_calibration": {
                "enabled": True,
                "min_confidence": 0.35,
                "pm_soft_risk_reasons": ["side_performance_block"],
            }
        }
        auditor = TradeAuditor(config)
        output = auditor.plan(
            TradeAuditorInput(
                ticker="TA",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bearish", "confidence": 0.65, "horizon_class": "short", "market_regime": "trend"},
                ],
                signal_combo=["Bearish", "Neutral", "Bearish"],
                raw_position_ratio=-0.12,
                current_position_ratio=0.0,
                signal_strength=0.60,
                market_confirmation={"enabled": False},
                recent_ticker_side_performance={
                    "total_trades": 12,
                    "win_rate": 0.25,
                    "total_pnl": -6200,
                },
                adaptive_policy_state=[
                    {
                        "id": "cal-auditor",
                        "ticker": "TA",
                        "side": "short",
                        "setup_type": "*",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "policy_type": "contextual_rule_calibration:trade_auditor",
                        "policy_action": "calibrate",
                        "confidence_score": 0.55,
                        "sample_count": 2,
                        "payload": {
                            "rule_adjustments": {
                                "trade_auditor": {
                                    "soften_hard_block_reasons": ["side_performance_block"]
                                }
                            }
                        },
                    }
                ],
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertIn("side_performance_block", output.reasons)
        self.assertNotIn("contextual_rule_calibration", output.diagnostics)
        self.assertNotIn("contextual_rule_calibration", output.reasons)
        self.assertEqual(
            output.diagnostics.get("research_memory_boundary"),
            "auditor_does_not_consume_research_records",
        )

    def test_single_high_quality_analyst_support_is_probe_not_block(self):
        config = self._auditor().full_config
        config["trade_auditor"]["quality_gate"]["allow_single_high_quality_probe"] = True
        auditor = TradeAuditor(config)

        output = auditor.plan(
            TradeAuditorInput(
                ticker="SR",
                analyst_signals=[
                    {
                        "agent_name": "technical",
                        "signal": "Bullish",
                        "confidence": 0.62,
                        "business_quality_score": 0.66,
                        "metadata": {"tradeability": "high"},
                    },
                    {"agent_name": "fundamental", "signal": "Neutral", "confidence": 0.42, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "commodity_news", "signal": "Neutral", "confidence": 0.38, "metadata": {"tradeability": "medium"}},
                ],
                signal_combo=["Bullish", "Neutral", "Neutral"],
                raw_position_ratio=0.03,
                current_position_ratio=0.0,
                signal_strength=0.40,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.50,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertIn("single_high_quality_probe_only", output.reasons)
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_market_confirmation_soft_risk_scales_instead_of_blocking(self):
        config = self._auditor().full_config
        config["market_confirmation"]["quality_gate_block_weak_signal"] = False
        config["market_confirmation"]["block_weak_conflicting_signal"] = False
        auditor = TradeAuditor(config)

        output = auditor.plan(
            TradeAuditorInput(
                ticker="PB",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bullish", "confidence": 0.58, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Bullish", "confidence": 0.56, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "commodity_news", "signal": "Neutral", "confidence": 0.40, "metadata": {"tradeability": "medium"}},
                ],
                signal_combo=["Bullish", "Bullish", "Neutral"],
                raw_position_ratio=0.04,
                current_position_ratio=0.0,
                signal_strength=0.20,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.40,
                    "features": [{"feature": "basis"}],
                    "confirmations": [],
                    "conflicts": ["basis"],
                },
            )
        )

        self.assertNotEqual(output.decision, "block")
        self.assertIn("market_confirmation_quality_gate", output.reasons)
        self.assertIn("market_confirmation_conflict", output.reasons)
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_scorecard_single_high_quality_setup_can_seed_probe(self):
        side, ratio, row = _scorecard_probe_seed(
            opportunity_scorecard={
                "long": {
                    "final_state": "tradeable_candidate",
                    "supporting_signal_count": 1,
                    "score": 0.55,
                    "max_setup_quality": 0.64,
                    "max_business_quality": 0.66,
                    "market_confirmation_score": 0.48,
                    "gating_failures": [],
                },
                "short": {
                    "final_state": "no_opportunity",
                    "supporting_signal_count": 0,
                    "score": 0.0,
                },
            },
            control={
                "watch_for_trigger_new_entry": {
                    "allow_probe": True,
                    "probe_max_ratio": 0.01,
                    "probe_floor_ratio": 0.005,
                    "scorecard_probe_min_supporting_signals": 2,
                    "allow_single_high_quality_probe": True,
                    "single_high_quality_probe_min_score": 0.52,
                    "single_high_quality_probe_min_setup_quality": 0.60,
                    "single_high_quality_probe_min_business_quality": 0.60,
                    "single_high_quality_probe_min_confirmation_score": 0.45,
                }
            },
        )

        self.assertEqual(side, "long")
        self.assertGreater(ratio, 0.0)
        self.assertEqual(row["final_state"], "tradeable_candidate")

    def test_scorecard_confirmed_tradeable_candidate_seeds_probe_even_when_score_is_modest(self):
        """Regression for 2025-04-10 ZN: PM seed must use scorecard layer, not only raw score."""
        side, ratio, row = _scorecard_probe_seed(
            opportunity_scorecard={
                "long": {
                    "final_state": "tradeable_candidate",
                    "supporting_signal_count": 1,
                    "score": 0.51,
                    "max_setup_quality": 0.75,
                    "max_business_quality": 0.72,
                    "market_confirmation_score": 0.75,
                    "single_tradeable_candidate_setup_confirmed": True,
                    "technical_opposes_side": False,
                    "gating_failures": [],
                },
                "short": {
                    "final_state": "no_opportunity",
                    "supporting_signal_count": 0,
                    "score": 0.0,
                },
            },
            control={
                "watch_for_trigger_new_entry": {
                    "allow_probe": True,
                    "probe_max_ratio": 0.01,
                    "probe_floor_ratio": 0.005,
                    "scorecard_probe_min_supporting_signals": 2,
                    "allow_single_high_quality_probe": True,
                    "single_high_quality_probe_min_score": 0.52,
                    "single_high_quality_probe_min_setup_quality": 0.60,
                    "single_high_quality_probe_min_business_quality": 0.60,
                    "single_high_quality_probe_min_confirmation_score": 0.45,
                    "scorecard_tradeable_candidate_probe_min_confirmation_score": 0.68,
                }
            },
        )

        self.assertEqual(side, "long")
        self.assertGreater(ratio, 0.0)
        self.assertEqual(row["final_state"], "tradeable_candidate")

    def test_scorecard_watch_for_trigger_seed_is_conditional_monitor_candidate(self):
        side, ratio, row = _scorecard_probe_seed(
            opportunity_scorecard={
                "short": {
                    "final_state": "watch_for_trigger",
                    "supporting_signal_count": 2,
                    "score": 0.56,
                    "max_setup_quality": 0.68,
                    "max_business_quality": 0.64,
                    "market_confirmation_score": 0.48,
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "invalidation_present": True,
                    "entry_trigger": "wait for post-open break below support",
                    "gating_failures": [],
                }
            },
            control={
                "watch_for_trigger_new_entry": {
                    "allow_probe": True,
                    "probe_max_ratio": 0.01,
                    "probe_floor_ratio": 0.005,
                    "scorecard_probe_min_supporting_signals": 2,
                    "scorecard_probe_min_score": 0.35,
                    "scorecard_probe_block_on_critical_data_gap": True,
                }
            },
        )

        self.assertEqual(side, "short")
        self.assertLess(ratio, 0.0)
        self.assertEqual(row["final_state"], "watch_for_trigger")
        self.assertTrue(row["setup_quality_ok"])
        self.assertFalse(row["trigger_valid"])

    def test_scorecard_confirmed_tradeable_candidate_with_technical_opposition_does_not_seed_probe(self):
        side, ratio, _row = _scorecard_probe_seed(
            opportunity_scorecard={
                "long": {
                    "final_state": "tradeable_candidate",
                    "supporting_signal_count": 1,
                    "score": 0.51,
                    "max_setup_quality": 0.75,
                    "max_business_quality": 0.72,
                    "market_confirmation_score": 0.75,
                    "single_tradeable_candidate_setup_confirmed": False,
                    "technical_opposes_side": True,
                    "gating_failures": [],
                }
            },
            control={
                "watch_for_trigger_new_entry": {
                    "allow_probe": True,
                    "probe_max_ratio": 0.01,
                    "probe_floor_ratio": 0.005,
                    "scorecard_tradeable_candidate_probe_min_confirmation_score": 0.68,
                }
            },
        )

        self.assertEqual(side, "flat")
        self.assertEqual(ratio, 0.0)

    def test_low_quality_news_driver_is_probe_capped_not_hard_blocked(self):
        output = self._auditor().plan(
            TradeAuditorInput(
                ticker="M",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Neutral", "confidence": 0.45, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Neutral", "confidence": 0.42, "metadata": {"tradeability": "medium"}},
                    {
                        "agent_name": "commodity_news",
                        "signal": "Bullish",
                        "confidence": 0.52,
                        "metadata": {
                            "tradeability": "medium",
                            "freshness_score": 0.60,
                            "relevance_score": 0.65,
                        },
                    },
                ],
                signal_combo=["Neutral", "Neutral", "Bullish"],
                raw_position_ratio=0.04,
                current_position_ratio=0.0,
                signal_strength=0.25,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.50,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
            )
        )

        self.assertNotEqual(output.decision, "block")
        self.assertIn("news_only_directional_trade", output.reasons)
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_weak_signal_combo_new_entry_is_probe_capped_not_zeroed(self):
        ratio, reasons, notes, diagnostics = _apply_trade_frequency_control(
            db=None,
            config_id="test-config",
            ticker="PB",
            trading_date="2025-01-02",
            position_ratio=0.04,
            current_ratio=0.0,
            signal_combo=("Bullish", "Bullish", "Neutral"),
            market_confirmation={
                "enabled": True,
                "confirmation_score": 0.45,
                "features": [{"feature": "basis"}],
                "confirmations": ["basis"],
                "conflicts": [],
            },
            full_config={
                "trade_frequency_control": {
                    "enabled": True,
                    "weak_signal_combos": [["Bullish", "Bullish", "Neutral"]],
                    "weak_combo_probe_max_ratio": 0.01,
                    "weak_combo_probe_floor_ratio": 0.005,
                    "weak_cap_multiplier": 0.50,
                },
                "market_confirmation": {
                    "min_confirmations_for_new_entry": 2,
                    "min_confirmation_score_for_weak_combo": 0.60,
                },
            },
        )

        self.assertGreater(ratio, 0.0)
        self.assertLessEqual(abs(ratio), 0.01)
        self.assertIn("weak_signal_combo", reasons)
        self.assertIn("weak_signal_combo_probe_cap", reasons)


class ValidationRegressionTest(unittest.TestCase):
    def test_zero_transaction_day_classification_uses_recommendation_audit(self):
        recommendations = [
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "neutral_signal_no_trade"}},
            },
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "position_matched"}},
            },
        ]
        result = classify_zero_transaction_day(recommendations)
        self.assertEqual(result["classification"], "expected")
        self.assertEqual(sorted(result["reasons"]), ["neutral_signal_no_trade", "position_matched"])
        self.assertEqual(result["reason_categories"], {"signal": 1, "business": 1})

    def test_zero_transaction_day_allows_horizon_timing_gate(self):
        recommendations = [
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "neutral_signal_no_trade"}},
            },
            {
                "source_type": "strategy",
                "signal_snapshot": {
                    "execution_result": {"no_trade_reason": "horizon_consistency_requires_short_timing"}
                },
            },
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "trade_auditor_block"}},
            },
        ]

        result = classify_zero_transaction_day(recommendations)

        self.assertEqual(result["classification"], "expected")
        self.assertIn("horizon_consistency_requires_short_timing", result["reasons"])
        self.assertEqual(result["reason_categories"], {"signal": 1, "timing": 1, "risk": 1})

    def test_zero_transaction_day_allows_learning_watchlist_gate(self):
        recommendations = [
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "weak_ticker_side_history"}},
            },
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "neutral_signal_no_trade"}},
            },
        ]

        result = classify_zero_transaction_day(recommendations)

        self.assertEqual(result["classification"], "expected")
        self.assertIn("weak_ticker_side_history", result["reasons"])
        self.assertEqual(result["reason_categories"], {"learning": 1, "signal": 1})

    def test_zero_transaction_day_allows_provisional_probe_gate(self):
        recommendations = [
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "provisional_policy_probe_only"}},
            }
        ]

        result = classify_zero_transaction_day(recommendations)

        self.assertEqual(result["classification"], "expected")
        self.assertIn("provisional_policy_probe_only", result["reasons"])

    def test_net_exposure_review_allows_small_phase4_drift_with_warning(self):
        warnings = []
        errors = []

        _apply_net_exposure_review(
            trading_date="2025-01-14",
            cfg={"net_exposure_control": {"max_net_exposure": 0.50, "phase4_drift_tolerance": 0.01}},
            net_exposure=0.5085,
            warnings=warnings,
            errors=errors,
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("stayed within tolerance", warnings[0])

    def test_net_exposure_review_rejects_material_phase4_breach(self):
        warnings = []
        errors = []

        _apply_net_exposure_review(
            trading_date="2025-01-14",
            cfg={"net_exposure_control": {"max_net_exposure": 0.50, "phase4_drift_tolerance": 0.01}},
            net_exposure=0.525,
            warnings=warnings,
            errors=errors,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("net exposure exceeds cap", errors[0])

    def test_net_exposure_review_uses_dynamic_alpha_release_cap(self):
        warnings = []
        errors = []

        _apply_net_exposure_review(
            trading_date="2025-02-10",
            cfg={"net_exposure_control": {"max_net_exposure": 0.50, "phase4_drift_tolerance": 0.01}},
            net_exposure=1.4617,
            warnings=warnings,
            errors=errors,
            recommendations=[
                {
                    "underlying_code": "M",
                    "signal_snapshot": json.dumps(
                        {
                            "execution_translation": {
                                "dynamic_net_exposure_control": {
                                    "mode": "alpha_release",
                                    "max_net_exposure": 2.0,
                                }
                            }
                        }
                    ),
                }
            ],
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("dynamic alpha-release cap", warnings[0])

    def test_market_confirmation_quality_warnings_are_aggregated_by_status(self):
        recommendations = [
            {
                "underlying_code": "BU",
                "signal_snapshot": {
                    "market_confirmation": {
                        "data_missing": ["contract_rank"],
                        "feature_status": {"contract_rank": "parameter_error"},
                        "parameter_errors": ["contract_rank"],
                        "data_status_groups": {"parameter_error": ["contract_rank"]},
                    }
                },
            },
            {
                "underlying_code": "RB",
                "signal_snapshot": {
                    "market_confirmation": {
                        "data_missing": ["warehouse_receipt"],
                        "feature_status": {"warehouse_receipt": "no_data"},
                        "no_data": ["warehouse_receipt"],
                        "data_status_groups": {"no_data": ["warehouse_receipt"]},
                    }
                },
            },
            {
                "underlying_code": "M",
                "signal_snapshot": {
                    "market_confirmation": {
                        "data_missing": [],
                        "fallback_covered_missing": ["net_flow_long", "net_flow_short"],
                        "feature_status": {
                            "net_flow_long": "fallback_covered",
                            "net_flow_short": "fallback_covered",
                        },
                        "data_status_groups": {
                            "fallback_covered": ["net_flow_long", "net_flow_short"]
                        },
                    }
                },
            },
        ]

        warnings, summary = _collect_recommendation_quality_warnings(recommendations)

        self.assertTrue(any("market confirmation parameter errors" in item for item in warnings))
        self.assertFalse(any("market confirmation no data" in item for item in warnings))
        self.assertFalse(any("market confirmation fallback covered missing" in item for item in warnings))
        self.assertTrue(any("market confirmation optional no data" in item for item in summary["info_messages"]))
        self.assertTrue(any("market confirmation fallback covered optional missing" in item for item in summary["info_messages"]))
        self.assertFalse(any(": market confirmation data missing:" in item for item in warnings))
        self.assertEqual(summary["missing_by_status"]["parameter_error"], ["BU"])
        self.assertEqual(summary["missing_by_status_feature"]["no_data"]["warehouse_receipt"], ["RB"])
        self.assertEqual(summary["fallback_covered_by_feature"]["net_flow_long"], ["M"])

    def test_reviewer_execution_audit_accepts_hold_recommendation_without_transactions(self):
        errors = []
        counter = _validate_recommendation_execution_audit(
            recommendations=[
                {
                    "id": "rec-hold",
                    "action": "hold",
                    "lots": 0,
                    "status": "executed",
                    "signal_snapshot": json.dumps(
                        {
                            "execution_result": {
                                "transaction_count": 0,
                                "actual_transactions": [],
                                "no_trade_reason": "position_matched",
                            }
                        }
                    ),
                }
            ],
            transactions_by_recommendation={},
            errors=errors,
        )

        self.assertEqual(errors, [])
        self.assertEqual(counter["position_matched"], 1)

    def test_capital_deployment_diagnostics_separates_reasons_and_alpha_candidates(self):
        recommendations = [
            {
                "underlying_code": "BU",
                "signal_snapshot": {
                    "final_action_contract": {
                        "target_position_ratio": 0.05,
                        "current_lots": 2,
                        "target_lots": 5,
                        "lots_delta": 3,
                        "lots_delta_abs": 3,
                        "reason_codes": "capital_release_candidate",
                        "final_action": "scale",
                        "reason_codes": ["capital_utilization_guard"],
                        "authority_type": "allow",
                        "learning_used": {
                            "memory_state": "protected",
                            "capital_utilization_learning": {
                                "protected_memory": {
                                    "memory_state": "protected",
                                    "signal_combo": "Bullish|Bullish|Neutral",
                                }
                            },
                            "capital_utilization_target": {
                                "alpha_release_tier": "boost",
                                "stop_protected": True,
                                "structured_invalidation": True,
                                "alpha_release": {
                                    "specific_signal_combo": True,
                                    "limiting_reasons": [],
                                },
                            },
                        },
                    },
                    "active_opportunity_audit": {"decision": {"authority_type": "allow"}},
                    "market_confirmation": {"confirmation_score": 0.72},
                    "technical": {"signal": "Bullish", "confidence": 0.70},
                    "execution_result": {"no_trade_reason": "capital_release_candidate"},
                    "pm_draft_pm_internal_draft_not_trade_source": True,
                    "pm_draft_for_test_only": {
                        "target_position_ratio": 0.05,
                        "current_ticker_exposure": 0.02,
                        "target_lots": 5,
                        "current_lots_before_open": 2,
                        "lots_delta_abs": 3,
                        "reason_codes": "position_matched",
                        "signal_confidence": 0.70,
                        "rebalance_summary": {
                            "action_type": "increase",
                            "control_reasons": ["capital_utilization_guard"],
                        },
                        "signal_lifecycle": {"invalidation_level": 3200.0},
                        "market_confirmation": {"confirmation_score": 0.72},
                        "strategy_controls": {
                            "diagnostics": {
                                "capital_utilization_learning": {
                                    "protected_memory": {
                                        "memory_state": "protected",
                                        "signal_combo": "Bullish|Bullish|Neutral",
                                    }
                                }
                            }
                        },
                        "trade_auditor": {"decision": "allow", "reasons": ["trade_auditor_allow"]},
                    },
                },
            },
            {
                "underlying_code": "RB",
                "signal_snapshot": {
                    "final_action_contract": {
                        "target_position_ratio": 0.04,
                        "target_lots": 4,
                        "current_lots": 0,
                        "lots_delta": 4,
                        "lots_delta_abs": 0,
                        "reason_codes": "intraday_trigger_not_met",
                        "final_action": "open_real",
                        "reason_codes": [],
                        "authority_type": "allow",
                    },
                    "active_opportunity_audit": {"decision": {"authority_type": "allow"}},
                    "market_confirmation": {"confirmation_score": 0.66},
                    "execution_result": {"no_trade_reason": "intraday_trigger_not_met"},
                },
            },
            {
                "underlying_code": "M",
                "signal_snapshot": {
                    "final_action_contract": {
                        "target_position_ratio": -0.03,
                        "target_lots": -3,
                        "current_lots": 0,
                        "lots_delta": -3,
                        "lots_delta_abs": 0,
                        "reason_codes": "trade_auditor_block",
                        "final_action": "wait",
                        "reason_codes": ["analyst_quality_low_tradeability"],
                        "authority_type": "block",
                    },
                    "active_opportunity_audit": {
                        "decision": {"authority_type": "block"},
                        "reason_codes": ["analyst_quality_low_tradeability"],
                    },
                    "market_confirmation": {"confirmation_score": 0.40},
                    "execution_result": {"no_trade_reason": "trade_auditor_block"},
                },
            },
        ]

        diagnostics = _build_capital_deployment_diagnostics(
            cfg={
                "capital_utilization_control": {
                    "min_confirmation_score_for_scaling": 0.60,
                    "memory_protected_min_confirmation_score": 0.45,
                },
                "execution": {
                    "intraday_confirmation": {
                        "opening_range_minutes": 30,
                        "require_complete_opening_range": True,
                        "max_chase_ratio": 0.015,
                    }
                },
            },
            allocation_tier="under_deployed",
            reason_bucket="intraday_trigger_not_met",
            current_ratio=0.04,
            target_min=0.16,
            target_max=0.20,
            margin_gap_to_min=600000.0,
            strategy_recommendations=recommendations,
            no_trade_reason_counter=Counter(
                {
                    "position_matched": 1,
                    "intraday_trigger_not_met": 1,
                    "trade_auditor_block": 1,
                }
            ),
        )

        self.assertEqual(diagnostics["primary_category"], "execution_timing_gate")
        self.assertEqual(diagnostics["category_counts"]["position_already_matched"], 1)
        self.assertEqual(diagnostics["category_counts"]["execution_timing_gate"], 1)
        self.assertEqual(diagnostics["category_counts"]["auditor_suppression"], 1)
        self.assertEqual(diagnostics["directional_candidate_count"], 3)
        self.assertEqual(diagnostics["blocked_directional_candidate_count"], 3)
        self.assertEqual(diagnostics["capital_path_stage_counts"]["execution_timing"], 1)
        self.assertEqual(diagnostics["capital_path_stage_counts"]["hard_or_auditor_block"], 1)
        self.assertIn("capital_path_cases", diagnostics)
        self.assertEqual(diagnostics["alpha_release_candidate_count"], 1)
        self.assertEqual(diagnostics["alpha_release_candidates"][0]["ticker"], "BU")
        self.assertEqual(diagnostics["alpha_release_candidates"][0]["alpha_release_tier"], "boost")
        self.assertTrue(
            diagnostics["alpha_release_candidates"][0]["alpha_release_requirements"]["stop_protected"]
        )
        self.assertEqual(diagnostics["execution_gate_candidates"][0]["ticker"], "RB")
        self.assertEqual(diagnostics["auditor_suppression_cases"][0]["ticker"], "M")
        self.assertEqual(
            diagnostics["parameter_review"][0]["scope"],
            "execution.intraday_confirmation",
        )

    def test_capital_deployment_marks_recovery_probe_candidates_without_forcing_trade(self):
        recommendations = [
            {
                "underlying_code": "BU",
                "signal_snapshot": {
                    "final_action_contract": {
                        "target_position_ratio": 0.003,
                        "current_lots": 0,
                        "target_lots": 1,
                        "lots_delta": 1,
                        "lots_delta_abs": 1,
                        "reason_codes": "minimum_new_entry_threshold",
                        "final_action": "open_probe",
                        "reason_codes": ["minimum_new_entry_threshold"],
                        "authority_type": "allow",
                        "learning_used": {
                            "memory_state": "protected",
                            "capital_utilization_learning": {
                                "protected_memory": {
                                    "memory_state": "protected",
                                    "signal_combo": "Bullish|Bullish|Neutral",
                                }
                            },
                            "capital_utilization_target": {
                                "alpha_release_tier": "normal",
                                "stop_protected": True,
                                "structured_invalidation": True,
                                "alpha_release": {
                                    "specific_signal_combo": True,
                                    "limiting_reasons": [],
                                },
                            },
                        },
                    },
                    "active_opportunity_audit": {"decision": {"authority_type": "allow"}},
                    "market_confirmation": {"confirmation_score": 0.72},
                    "technical": {"signal": "Bullish", "confidence": 0.72},
                    "execution_result": {"no_trade_reason": "minimum_new_entry_threshold"},
                },
            }
        ]

        diagnostics = _build_capital_deployment_diagnostics(
            cfg={
                "capital_utilization_control": {
                    "min_confirmation_score_for_scaling": 0.60,
                    "memory_protected_min_confirmation_score": 0.45,
                },
                "portfolio_manager": {
                    "holding_rebalance_control": {"min_new_entry_ratio": 0.004},
                },
            },
            allocation_tier="under_deployed",
            reason_bucket="minimum_new_entry_threshold",
            current_ratio=0.0,
            target_min=0.06,
            target_max=0.08,
            margin_gap_to_min=300000.0,
            strategy_recommendations=recommendations,
            no_trade_reason_counter=Counter({"minimum_new_entry_threshold": 1}),
        )

        self.assertEqual(diagnostics["alpha_release_candidate_count"], 0)
        self.assertEqual(diagnostics["recovery_probe_candidate_count"], 1)
        self.assertEqual(diagnostics["recovery_probe_candidates"][0]["ticker"], "BU")
        self.assertEqual(
            diagnostics["recovery_probe_candidates"][0]["blocked_by_reason"],
            "minimum_new_entry_threshold",
        )

    def test_capital_deployment_diagnostics_classifies_horizon_timing_gate(self):
        diagnostics = _build_capital_deployment_diagnostics(
            cfg={},
            allocation_tier="under_deployed",
            reason_bucket="horizon_consistency_requires_short_timing",
            current_ratio=0.0,
            target_min=0.06,
            target_max=0.08,
            margin_gap_to_min=300000.0,
            strategy_recommendations=[],
            no_trade_reason_counter=Counter({"horizon_consistency_requires_short_timing": 5}),
        )

        self.assertEqual(diagnostics["primary_category"], "strategy_timing_gate")
        self.assertEqual(diagnostics["category_counts"]["strategy_timing_gate"], 5)
        self.assertTrue(
            diagnostics["reason_profiles"]["horizon_consistency_requires_short_timing"]["risk_control_normal"]
        )

    def test_capital_deployment_diagnostics_classifies_learning_watchlist_gate(self):
        diagnostics = _build_capital_deployment_diagnostics(
            cfg={},
            allocation_tier="under_deployed",
            reason_bucket="weak_ticker_side_history",
            current_ratio=0.0,
            target_min=0.06,
            target_max=0.08,
            margin_gap_to_min=300000.0,
            strategy_recommendations=[],
            no_trade_reason_counter=Counter({"weak_ticker_side_history": 1}),
        )

        self.assertEqual(diagnostics["primary_category"], "learning_risk_control")
        self.assertEqual(diagnostics["category_counts"]["learning_risk_control"], 1)
        self.assertFalse(diagnostics["reason_profiles"]["weak_ticker_side_history"]["alpha_expansion_allowed"])

    def test_capital_deployment_diagnostics_classifies_provisional_probe_gate(self):
        diagnostics = _build_capital_deployment_diagnostics(
            cfg={},
            allocation_tier="under_deployed",
            reason_bucket="provisional_policy_probe_only",
            current_ratio=0.0,
            target_min=0.06,
            target_max=0.08,
            margin_gap_to_min=300000.0,
            strategy_recommendations=[],
            no_trade_reason_counter=Counter({"provisional_policy_probe_only": 2}),
        )

        self.assertEqual(diagnostics["primary_category"], "learning_risk_control")
        self.assertEqual(diagnostics["category_counts"]["learning_risk_control"], 2)
        self.assertFalse(diagnostics["reason_profiles"]["provisional_policy_probe_only"]["alpha_expansion_allowed"])


class AlphaReleaseCapitalUtilizationRegressionTest(unittest.TestCase):
    def _base_config(self):
        return {
            "capital_utilization_control": {
                "enabled": True,
                "target_margin_ratio_min": 0.06,
                "target_margin_ratio_max": 0.08,
                "target_margin_ratio_confirmed": 0.07,
                "strong_opportunity_target_margin_ratio_min": 0.16,
                "strong_opportunity_target_margin_ratio_max": 0.20,
                "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                "max_margin_ratio_after_scaling": 0.20,
                "min_confirmation_score_for_scaling": 0.60,
                "memory_protected_min_confirmation_score": 0.45,
                "dynamic_concentration_enabled": True,
                "allow_memory_protected_scaling": True,
                "protected_min_sample_count_for_scaling": 5,
                "protected_min_win_rate_for_scaling": 0.60,
                "protected_min_net_pnl_for_scaling": 1000,
                "require_specific_signal_combo_for_strong_scaling": True,
                "require_stop_protection_for_strong_scaling": True,
            }
        }

    def _protected_memory(self, signal_combo="Bullish|Bullish|Neutral"):
        return {
            "combo": {
                "memory_state": "protected",
                "signal_combo": signal_combo,
                "sample_count": 8,
                "win_rate": 0.75,
                "net_pnl": 8000.0,
            }
        }

    def _run_alpha_release_case(self, *, strategy_memory, analyst_signals):
        return _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-03-04",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.01,
            margin_rate=0.10,
            max_position_ratio=0.05,
            market_confirmation={"confirmation_score": 0.82},
            full_config=self._base_config(),
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory=strategy_memory,
            adaptive_policy_state=[],
            analyst_signals=analyst_signals,
        )

    def test_alpha_release_boost_requires_explicit_stop(self):
        ratio, reasons, notes, diagnostics = self._run_alpha_release_case(
            strategy_memory=self._protected_memory(),
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    counter_evidence="basis weakens and price closes below breakout shelf",
                )
            ],
        )

        target = diagnostics["capital_utilization_target"]
        self.assertEqual(target["alpha_release_tier"], "normal")
        self.assertEqual(target["target_mode"], "confirmed_observation")
        self.assertFalse(target["high_quality_memory"])
        self.assertNotIn("capital_utilization_memory_protected", reasons)
        self.assertLessEqual(abs(ratio) * 0.10, 0.08)
        self.assertIn(
            "missing_explicit_stop_for_alpha_release_boost",
            target["alpha_release"]["limiting_reasons"],
        )

    def test_specific_memory_with_stop_can_boost_alpha_release(self):
        ratio, reasons, notes, diagnostics = self._run_alpha_release_case(
            strategy_memory=self._protected_memory(),
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    invalidation_level=3200.0,
                    atr_stop_distance=80.0,
                )
            ],
        )

        target = diagnostics["capital_utilization_target"]
        self.assertEqual(target["alpha_release_tier"], "boost")
        self.assertEqual(target["target_mode"], "alpha_release_boost")
        self.assertTrue(target["high_quality_memory"])
        self.assertGreater(abs(ratio), 0.05)
        self.assertIn("capital_utilization_memory_protected", reasons)
        self.assertIn("alpha_release_boost", reasons)

    def test_soft_risk_tags_do_not_skip_alpha_release_with_same_scope_memory(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-03-04",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.01,
            margin_rate=0.10,
            max_position_ratio=0.05,
            market_confirmation={"confirmation_score": 0.82},
            full_config=self._base_config(),
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory=self._protected_memory(),
            adaptive_policy_state=[],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    invalidation_level=3200.0,
                    atr_stop_distance=80.0,
                )
            ],
            pre_control_reasons=[
                "market_confirmation_quality_gate",
                "single_high_quality_probe_only",
            ],
        )

        self.assertGreater(abs(ratio), 0.05)
        self.assertNotIn("capital_utilization_soft_limit_respected", reasons)
        self.assertIn("capital_utilization_memory_protected", reasons)
        self.assertTrue(diagnostics["capital_utilization_soft_risk_arbiter"]["release_evidence_present"])
        self.assertEqual(diagnostics["capital_utilization_soft_risk_arbiter"]["allowed_to_continue"], True)

    def test_generic_memory_caps_alpha_release_to_normal(self):
        ratio, reasons, notes, diagnostics = self._run_alpha_release_case(
            strategy_memory=self._protected_memory(signal_combo="*"),
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    invalidation_level=3200.0,
                    atr_stop_distance=80.0,
                )
            ],
        )

        target = diagnostics["capital_utilization_target"]
        self.assertEqual(target["alpha_release_tier"], "normal")
        self.assertEqual(target["target_mode"], "confirmed_observation")
        self.assertFalse(target["high_quality_memory"])
        self.assertNotIn("capital_utilization_memory_protected", reasons)
        self.assertIn(
            "generic_memory_cannot_trigger_alpha_release_boost",
            target["alpha_release"]["limiting_reasons"],
        )

    def test_global_learned_underperformance_does_not_demote_specific_protected_memory(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.06,
            current_ratio=0.0,
            current_margin_ratio=0.01,
            margin_rate=0.10,
            max_position_ratio=0.08,
            market_confirmation={"confirmation_score": 0.85},
            full_config=self._base_config(),
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory=self._protected_memory(),
            adaptive_policy_state=[
                {
                    "policy_action": "demote",
                    "policy_type": "learned_vs_unlearned",
                    "multiplier": 0.50,
                    "sample_count": 4,
                    "confidence_score": 0.80,
                    "reason": "learned trades underperformed unlearned benchmark",
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    invalidation_level=3200.0,
                    atr_stop_distance=80.0,
                )
            ],
        )

        self.assertNotIn("learned_underperformance_policy", reasons)
        self.assertIn("capital_utilization_memory_protected", reasons)
        self.assertGreater(ratio, 0.03)

    def test_scoped_learned_underperformance_demotes_matching_template(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.06,
            current_ratio=0.0,
            current_margin_ratio=0.01,
            margin_rate=0.10,
            max_position_ratio=0.08,
            market_confirmation={"confirmation_score": 0.85},
            full_config=self._base_config(),
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory=self._protected_memory(),
            adaptive_policy_state=[
                {
                    "ticker": "ZZ",
                    "side": "long",
                    "setup_type": "long_reversal_confirmed_short",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "policy_action": "demote",
                    "policy_type": "learned_vs_unlearned",
                    "multiplier": 0.50,
                    "sample_count": 4,
                    "confidence_score": 0.80,
                    "reason": "learned alpha-release trades underperformed same-scope benchmark",
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    setup_type="reversal_confirmed",
                    horizon_class="short",
                    market_regime="trend",
                    invalidation_level=3200.0,
                    atr_stop_distance=80.0,
                )
            ],
        )

        self.assertAlmostEqual(ratio, 0.03)
        self.assertIn("learned_underperformance_policy", reasons)
        self.assertEqual(diagnostics["capital_utilization_skip"], "learned_underperformance_policy")

    def test_scoped_fast_loss_sentinel_demotes_matching_template(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="RB",
            trading_date="2025-03-17",
            position_ratio=-0.06,
            current_ratio=0.0,
            current_margin_ratio=0.01,
            margin_rate=0.10,
            max_position_ratio=0.08,
            market_confirmation={"confirmation_score": 0.72},
            full_config=self._base_config(),
            signal_combo=("Bearish", "Neutral", "Neutral"),
            strategy_memory={},
            adaptive_policy_state=[
                {
                    "ticker": "RB",
                    "side": "short",
                    "setup_type": "short_trend_pullback_short",
                    "horizon_class": "medium",
                    "market_regime": "choppy",
                    "policy_action": "cap",
                    "policy_type": "fast_loss_sentinel",
                    "multiplier": 0.50,
                    "sample_count": 2,
                    "confidence_score": 0.80,
                    "reason": "same-scope probe lost quickly twice",
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BEARISH,
                    confidence=0.70,
                    setup_type="trend_pullback",
                    horizon_class="medium",
                    market_regime="choppy",
                    invalidation_level=3600.0,
                    atr_stop_distance=80.0,
                )
            ],
        )

        self.assertAlmostEqual(ratio, -0.03)
        self.assertIn("learned_underperformance_policy", reasons)
        learned = diagnostics["capital_utilization_learning"]["learned_demote_record"]
        self.assertEqual(learned["policy_type"], "fast_loss_sentinel")

    def test_generic_fast_loss_sentinel_is_not_a_blanket_demote(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="RB",
            trading_date="2025-03-17",
            position_ratio=-0.06,
            current_ratio=0.0,
            current_margin_ratio=0.01,
            margin_rate=0.10,
            max_position_ratio=0.08,
            market_confirmation={"confirmation_score": 0.72},
            full_config=self._base_config(),
            signal_combo=("Bearish", "Neutral", "Neutral"),
            strategy_memory={},
            adaptive_policy_state=[
                {
                    "ticker": "*",
                    "side": "short",
                    "setup_type": "short_trend_pullback_short",
                    "policy_action": "cap",
                    "policy_type": "fast_loss_sentinel",
                    "multiplier": 0.50,
                    "sample_count": 2,
                    "confidence_score": 0.80,
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BEARISH,
                    confidence=0.70,
                    setup_type="trend_pullback",
                    horizon_class="medium",
                    market_regime="choppy",
                    invalidation_level=3600.0,
                    atr_stop_distance=80.0,
                )
            ],
        )

        self.assertNotIn("learned_underperformance_policy", reasons)
        self.assertNotEqual(diagnostics.get("capital_utilization_skip"), "learned_underperformance_policy")


class PMExpectancyTradeQualificationRegressionTest(unittest.TestCase):
    def _base_scorecard(self, layer="tradeable_candidate"):
        return {
            "long": {
                "final_state": layer,
                "gating_failures": [],
                "score": 0.62,
                "max_setup_quality": 0.66,
            }
        }

    def test_candidate_adaptive_policy_cannot_release_trade_authority(self):
        rows, trace = filter_adaptive_policy_state_for_pm([
            {
                "ticker": "RB",
                "side": "long",
                "setup_type": "trend_breakout_setup",
                "horizon_class": "short",
                "market_regime": "trend",
                "policy_type": "alpha_promotion",
                "policy_action": "protect",
                "confidence_score": 0.90,
                "sample_count": 9,
                "payload": {
                    "status": "candidate",
                    "next_round_memory_contract": {
                        "status": "candidate",
                        "maturity_state": "candidate",
                        "position_authority": "analysis_or_watchlist_only",
                        "max_position_impact": "no_direct_position_impact",
                    },
                },
            }
        ])

        self.assertEqual(rows, [])
        self.assertEqual(trace["blocked_count"], 1)
        self.assertEqual(trace["blocked_examples"][0]["reason"], "memory_contract_forbids_position_impact")

    def test_fast_candidate_alpha_remains_probe_only(self):
        rows, trace = filter_adaptive_policy_state_for_pm([
            {
                "ticker": "RB",
                "side": "long",
                "setup_type": "trend_breakout_setup",
                "horizon_class": "short",
                "market_regime": "trend",
                "policy_type": "fast_candidate_alpha",
                "policy_action": "probe",
                "confidence_score": 0.80,
                "sample_count": 4,
                "payload": {
                    "next_round_memory_contract": {
                        "status": "candidate",
                        "maturity_state": "fast_candidate_alpha",
                        "position_authority": "probe_or_small_setup_only_after_current_confirmation",
                        "max_position_impact": "same_scope_probe_or_small_trade_only",
                    },
                },
            }
        ])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["adaptive_policy_runtime_decision"]["decision"], "candidate_probe_only")
        self.assertEqual(trace["allowed_count"], 1)

    def test_capital_allocator_filters_unvalidated_adaptive_release_policy(self):
        row = adaptive_policy_record(
            [
                {
                    "ticker": "RB",
                    "side": "long",
                    "setup_type": "trend_breakout_setup",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "policy_type": "alpha_promotion",
                    "policy_action": "allow",
                    "confidence_score": 0.90,
                    "sample_count": 6,
                    "payload": {
                        "status": "candidate",
                        "next_round_memory_contract": {
                            "status": "candidate",
                            "position_authority": "probe_or_small_setup_only_after_current_confirmation",
                        },
                    },
                }
            ],
            {"allow"},
            policy_types={"alpha_promotion"},
        )

        self.assertEqual(row, {})

    def test_adaptive_policy_safety_tolerates_dirty_sample_count(self):
        rows, trace = filter_adaptive_policy_state_for_pm([
            {
                "ticker": "RB",
                "side": "long",
                "setup_type": "trend_breakout_setup",
                "horizon_class": "short",
                "market_regime": "trend",
                "policy_type": "fast_candidate_alpha",
                "policy_action": "probe",
                "confidence_score": 0.75,
                "sample_count": "not-a-number",
                "payload": {
                    "next_round_memory_contract": {
                        "status": "candidate",
                        "maturity_state": "fast_candidate_alpha",
                        "position_authority": "probe_or_small_setup_only_after_current_confirmation",
                        "max_position_impact": "same_scope_probe_or_small_trade_only",
                    },
                },
            }
        ])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["adaptive_policy_runtime_decision"]["sample_count"], 0)
        self.assertEqual(trace["allowed_count"], 1)

    def test_pm_no_new_entry_phrase_blocks_scorecard_probe_semantics(self):
        reason = _pm_new_entry_semantic_block_reason(
            "New entry is not warranted under current technical confirmation; sizing prior to 0."
        )

        self.assertEqual(reason, "pm_text_no_trade_blocks_new_entry")

    def test_final_action_contract_is_single_structured_trade_truth(self):
        contract = _build_final_action_contract(
            ticker="RB",
            current_lots=0,
            target_lots=5,
            position_ratio=0.04,
            margin_required=40000.0,
            account_equity=5000000.0,
            lots_to_trade=5,
            lots_to_trade_reason="tradable",
            recommendation_intent={"action": "open_long", "lots": 5, "action_type": "open"},
            final_entry_authority={
                "authority_type": "real_budget_entry",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "max_allowed_margin_ratio": 0.12,
                "reason_codes": ["qualified_positive_expectancy"],
            },
            control_reasons=["positive_open_action_value_seed", "qualified_positive_expectancy"],
            control_diagnostics={
                "positive_open_action_value_seed": {
                    "target_side": "long",
                    "seed_position_ratio": 0.04,
                    "selected_action_value": {"reward_mean": 1800.0},
                }
            },
            opportunity_scorecard={
                "preferred_side": "long",
                "long": {
                    "final_state": "tradeable_candidate",
                    "score": 0.72,
                    "opportunity_score": 0.72,
                    "opportunity_score_components": {"setup_quality": 0.12},
                    "opportunity_rank": 1,
                    "capital_allocation_reason": "ranked_deployable_candidate_with_complete_current_evidence",
                    "learning_adjustment_summary": {"effect": "boosted"},
                },
            },
            market_confirmation={"confirmation_score": 0.70, "conflicts": []},
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "reward_mean": 1800.0,
                }
            ],
        )

        self.assertEqual(contract["contract_version"], "agentquant.final_action.v1")
        self.assertEqual(contract["final_action"], "open_real")
        self.assertTrue(contract["single_source_of_trade_truth"])
        self.assertTrue(contract["candidate_sources_do_not_bypass_contract"])
        self.assertEqual(contract["authority_type"], "real_budget_entry")
        self.assertEqual(contract["target_lots"], 5)
        self.assertEqual(contract["action_candidates"][0]["source"], "alpha_setup_action_value")
        self.assertEqual(contract["evidence_used"]["opportunity_score"], 0.72)
        self.assertEqual(contract["evidence_used"]["opportunity_rank"], 1)
        self.assertEqual(
            contract["evidence_used"]["capital_allocation_reason"],
            "ranked_deployable_candidate_with_complete_current_evidence",
        )
        self.assertEqual(contract["learning_used"]["learning_adjustment_summary"]["effect"], "boosted")
        self.assertNotIn("opportunity_score", contract)

    def test_final_action_contract_carries_execution_contract_as_trade_truth(self):
        contract = _build_final_action_contract(
            ticker="RB",
            current_lots=0,
            target_lots=-4,
            position_ratio=-0.04,
            margin_required=50000.0,
            account_equity=5000000.0,
            lots_to_trade=4,
            lots_to_trade_reason="tradable",
            recommendation_intent={"action": "open_short", "lots": 4, "action_type": "open"},
            final_entry_authority={
                "authority_type": "real_budget_entry",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "max_allowed_margin_ratio": 0.12,
            },
            control_reasons=["execution_action_value_preference"],
            control_diagnostics={},
            opportunity_scorecard={"preferred_side": "short", "short": {"final_state": "tradeable_candidate"}},
            market_confirmation={"confirmation_score": 0.72, "conflicts": []},
            alpha_setup_action_values=[],
            execution_contract_fields={
                "contract_version": "agentquant.execution_contract.v1",
                "execution_profile": "vwap_confirmed",
                "trigger_source": "execution_action_value_vwap",
                "requires_intraday_confirmation": True,
            },
        )

        self.assertEqual(contract["execution_profile"], "vwap_confirmed")
        self.assertEqual(contract["trigger_source"], "execution_action_value_vwap")
        self.assertTrue(contract["single_source_of_trade_truth"])

    def test_final_action_contract_separates_conditional_monitor_from_scorecard_probe_seed(self):
        contract = _build_final_action_contract(
            ticker="HC",
            current_lots=0,
            target_lots=-1,
            position_ratio=-0.01,
            margin_required=12000.0,
            account_equity=5000000.0,
            lots_to_trade=1,
            lots_to_trade_reason="conditional_monitor",
            recommendation_intent={"action": "open_short", "lots": 1, "action_type": "open"},
            final_entry_authority={
                "authority_type": "exploration_probe",
                "conditional_trigger_authority": True,
                "requires_intraday_confirmation": True,
                "can_execute_without_intraday_trigger": False,
                "max_allowed_margin_ratio": 0.02,
                "reason_codes": ["pm_watch_for_trigger_probe_cap", "conditional_trigger_authority"],
            },
            control_reasons=["pm_watch_for_trigger_probe_cap", "conditional_trigger_authority"],
            control_diagnostics={
                "conditional_monitor_probe_seed": {
                    "side": "short",
                    "ratio": -0.01,
                    "status": "candidate_routed_to_conditional_monitor",
                    "requires_intraday_confirmation": True,
                    "scorecard": {
                        "final_state": "watch_for_trigger",
                        "setup_quality_ok": True,
                        "trigger_valid": False,
                        "current_trigger_confirmed": False,
                        "invalidation_present": True,
                        "entry_trigger": "wait for post-open break below support",
                    },
                }
            },
            opportunity_scorecard={
                "preferred_side": "short",
                "short": {
                    "final_state": "watch_for_trigger",
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "invalidation_present": True,
                    "entry_trigger": "wait for post-open break below support",
                },
            },
            market_confirmation={"confirmation_score": 0.52, "conflicts": []},
            alpha_setup_action_values=[],
        )

        self.assertEqual(contract["final_action"], "open_probe")
        self.assertEqual(contract["action_candidates"][0]["source"], "conditional_monitor")
        self.assertEqual(contract["action_candidates"][0]["action"], "conditional_probe")
        self.assertTrue(contract["action_candidates"][0]["requires_intraday_confirmation"])
        self.assertNotIn("scorecard_current_tradeable_probe_seed", contract["reason_codes"])

    def test_negative_action_value_blocks_repeat_new_entry_without_new_evidence(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="P",
            position_ratio=0.05,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 3,
                    "reward_mean": -1800.0,
                    "reward_sum": -5400.0,
                    "win_rate": 0.0,
                    "confidence_score": 0.60,
                    "action_preference": "cap_reduce_or_revalidate",
                    "max_position_impact": 0.02,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 3,
                        "exact_state_real_trade_sample_count": 3,
                        "amplification_scope_quality": "exact_real_state",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.52),
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation={"confirmation_score": 0.45},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("repeat_loss_watchlist_only", reasons)
        detail = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(detail["repeat_loss_without_new_evidence"])
        self.assertFalse(detail["strong_realtime_evidence"])

    def test_negative_action_value_allows_small_probe_when_new_technical_evidence_exists(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="P",
            position_ratio=0.05,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 3,
                    "reward_mean": -1800.0,
                    "reward_sum": -5400.0,
                    "win_rate": 0.0,
                    "confidence_score": 0.60,
                    "action_preference": "cap_reduce_or_revalidate",
                    "max_position_impact": 0.02,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 3,
                        "exact_state_real_trade_sample_count": 3,
                        "amplification_scope_quality": "exact_real_state",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.62,
                    invalidation_level=9000.0,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="breakout above short-term resistance with volume confirmation",
                    entry_quality="acceptable",
                    metadata={
                        "action_evidence_contract": {
                            "setup_family": "trend_breakout",
                            "opportunity_state": "tradeable_candidate",
                            "opportunity_state": "tradeable_candidate",
                            "trigger_valid": True,
                            "invalidation_present": True,
                            "evidence_role": "entry_timing",
                        },
                        "technical_context": {
                            "dominant_direction": "bullish",
                        }
                    },
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation={"confirmation_score": 0.58},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertGreater(abs(ratio), 0.0)
        self.assertLess(abs(ratio), 0.05)
        self.assertNotIn("repeat_loss_watchlist_only", reasons)
        detail = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(detail["strong_realtime_evidence"])

    def test_positive_action_value_marks_qualified_positive_expectancy(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="RB",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 5,
                    "reward_mean": 450.0,
                    "reward_sum": 2250.0,
                    "win_rate": 0.6,
                    "confidence_score": 0.62,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 5,
                        "exact_state_real_trade_sample_count": 5,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.60,
                    invalidation_level=3300.0,
                    opportunity_state="tradeable_candidate",
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.55),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation={"confirmation_score": 0.62},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertGreater(ratio, 0.02)
        self.assertIn("qualified_positive_expectancy", reasons)
        self.assertTrue(diagnostics["alpha_setup_ev_fusion"]["qualified_positive_expectancy"])

    def test_researcher_open_action_value_can_promote_pm_authority_and_lots(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="RB",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 6,
                    "reward_mean": 520.0,
                    "reward_sum": 3120.0,
                    "win_rate": 0.67,
                    "confidence_score": 0.72,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 6,
                        "exact_state_real_trade_sample_count": 6,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="breakout above opening range with volume confirmation",
                    invalidation_level=3300.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    entry_quality="strong",
                    evidence_role="entry_timing",
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.55),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertGreater(ratio, 0.02)
        self.assertIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(alpha_ev["positive_action_value"])
        self.assertTrue(alpha_ev["qualified_positive_expectancy"])
        self.assertEqual(alpha_ev["matched_action_value_count"], 1)

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )

        self.assertTrue(allowed)
        self.assertEqual(authority["authority_type"], "real_budget_entry")
        self.assertTrue(authority["open_action_evidence"])
        self.assertTrue(authority["strong_current_evidence"])
        self.assertTrue(authority["action_evidence_router"]["open"]["positive_open_action_value"])

    def test_exact_alpha_release_chain_reaches_pm_authority_and_trader_profile(self):
        technical_context = build_technical_context(
            "RB",
            {
                "trend": Signal.BULLISH,
                "macd": Signal.BULLISH,
                "adx": Signal.BULLISH,
                "rsi": Signal.BULLISH,
                "mean_reversion": Signal.NEUTRAL,
            },
            {
                "trend_strength": 31,
                "volatility": 0.18,
                "volume_ratio": 1.15,
                "price_range": 0.03,
            },
        )
        technical_context["current_trigger_confirmed"] = True
        technical_context["action_evidence_contract"] = {
            "entry_trigger": "current breakout above prior high is confirmed",
            "current_trigger_confirmed": True,
            "trigger_valid": True,
            "invalidation_present": True,
        }
        technical = apply_trade_research_contract(
            AnalystSignal(
                agent_name="technical",
                signal=Signal.BULLISH,
                confidence=0.74,
                horizon_class="short",
                business_quality_score=0.78,
                data_coverage_score=0.86,
                opportunity_type="trend_continuation",
                setup_type="rb_trend_breakout_setup",
                price_percentile=0.56,
                invalidation_level=3300.0,
            ),
            technical_context,
            analyst="technical",
            trading_date="2025-03-10",
            ticker="RB",
        )
        self.assertIn(technical.opportunity_state, {"tradeable_candidate", "tradeable_candidate"})
        self.assertTrue(technical.trigger_valid)
        self.assertEqual(technical.entry_timing_signal, "trend_breakout")
        self.assertNotIn("generic_trade_setup", json.dumps(technical.metadata, ensure_ascii=False))
        learning_scope = technical.metadata["action_evidence_contract"]["learning_scope"]
        self.assertEqual(learning_scope["setup_family"], "trend_breakout")
        self.assertEqual(learning_scope["market_regime"], "trend")

        open_action_value = {
            "ticker": "RB",
            "side": "long",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "trend_breakout_setup",
            "action_name": "open",
            "sample_count": 6,
            "reward_mean": 720.0,
            "reward_sum": 4320.0,
            "win_rate": 0.67,
            "confidence_score": 0.74,
            "action_preference": "controlled_open_or_add",
            "max_position_impact": 0.04,
            "payload": {
                "source": "alpha_setup_profile_action_value",
                "real_trade_reward_count": 6,
                "episode_trade_reward_count": 6,
                "exact_state_real_trade_sample_count": 6,
                "amplification_scope_quality": "exact_real_state",
                "action_preference": "positive_candidate_open",
                "reward_source": "trade_episode",
            },
        }
        execution_action_value = {
            "ticker": "RB",
            "side": "long",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "execution_breakout_setup",
            "data_combo": "technical:trend_breakout|execution:breakout",
            "action_name": "execution",
            "sample_count": 3,
            "reward_mean": -900.0,
            "reward_sum": -2700.0,
            "win_rate": 0.0,
            "confidence_score": 0.62,
            "action_preference": "cap_reduce_or_revalidate",
            "payload": {
                "source": "alpha_setup_profile_action_value",
                "real_trade_reward_count": 3,
                "exact_state_real_trade_sample_count": 3,
                "amplification_scope_quality": "exact_real_state",
            },
        }
        scorecard = {
            "preferred_side": "long",
            "long": {
                "final_state": technical.opportunity_state,
                "gating_failures": [],
                "score": 0.72,
                "confidence": 0.74,
                "max_setup_quality": technical.setup_quality_score,
            },
        }
        market_confirmation = {"confirmation_score": 0.72, "conflicts": []}
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="RB",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=scorecard,
            alpha_setup_profiles=[],
            alpha_setup_action_values=[open_action_value, execution_action_value],
            analyst_signals=[
                technical,
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.56),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation=market_confirmation,
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertGreater(ratio, 0.02)
        self.assertIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(alpha_ev["positive_action_value"])
        self.assertTrue(alpha_ev["qualified_positive_expectancy"])
        self.assertEqual(alpha_ev["action_value_stats"]["scope_quality"], "exact_real_state")
        self.assertTrue(alpha_ev["action_value_stats"]["real_amplification_support"])

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )
        self.assertTrue(allowed)
        self.assertEqual(authority["authority_type"], "real_budget_entry")
        self.assertTrue(authority["open_action_evidence"])
        self.assertTrue(authority["strong_current_evidence"])
        self.assertTrue(authority["open_action_evidence"])
        self.assertTrue(authority["strong_current_evidence"])

        plan = _build_pm_decision_context(
            ticker="RB",
            target_lots=4,
            current_price=3500.0,
            position_ratio=ratio,
            risk_level=RiskLevel.SAFE,
            long_scores={"confidence": 0.74},
            short_scores={"confidence": 0.20},
            margin_rate=0.10,
            current_lots=0,
            analyst_signals=[technical],
            final_entry_authority=authority,
            trading_date="2025-03-10",
            recommendation_intent={"action": "open_long"},
            control_reasons=reasons,
            alpha_setup_action_values=[open_action_value, execution_action_value],
        )
        execution_contract = plan
        self.assertEqual(execution_contract["execution_profile"], "pullback")
        self.assertEqual(execution_contract["trigger_source"], "execution_action_value_pullback")
        self.assertIn("execution_action_value_preference", execution_contract["reason_codes"])
        self.assertFalse(execution_contract["can_execute_without_intraday_trigger"])
        self.assertEqual(execution_contract["business_boundary"], "trader_executes_pm_plan_only_no_strategy_creation")

        final_contract = _build_final_action_contract(
            ticker="RB",
            current_lots=0,
            target_lots=4,
            position_ratio=ratio,
            margin_required=140000.0,
            account_equity=5000000.0,
            lots_to_trade=4,
            lots_to_trade_reason="tradable",
            recommendation_intent={"action": "open_long", "lots": 4, "action_type": "open"},
            final_entry_authority=authority,
            control_reasons=reasons,
            control_diagnostics=diagnostics,
            opportunity_scorecard=scorecard,
            market_confirmation=market_confirmation,
            alpha_setup_action_values=[open_action_value, execution_action_value],
            execution_contract_fields=execution_contract,
        )
        self.assertEqual(final_contract["final_action"], "open_real")
        self.assertEqual(final_contract["authority_type"], "real_budget_entry")
        self.assertTrue(final_contract["single_source_of_trade_truth"])

        result = select_intraday_execution(
            signal_bars=[
                {
                    "datetime": "2025-03-10 10:00:00",
                    "open": 3500.0,
                    "high": 3520.0,
                    "low": 3485.0,
                    "close": 3508.0,
                    "volume": 12,
                },
            ],
            execution_bars=[
                {
                    "datetime": "2025-03-10 09:30:00",
                    "open": 3500.0,
                    "high": 3540.0,
                    "low": 3470.0,
                    "close": 3500.0,
                    "volume": 12,
                },
                {
                    "datetime": "2025-03-10 09:31:00",
                    "open": 3500.0,
                    "high": 3540.0,
                    "low": 3470.0,
                    "close": 3500.0,
                    "volume": 12,
                },
                {
                    "datetime": "2025-03-10 10:01:00",
                    "open": 3510.0,
                    "high": 3525.0,
                    "low": 3500.0,
                    "close": 3512.0,
                    "volume": 10,
                },
            ],
            action="open_long",
            config={"opening_range_minutes": 2, "min_execution_volume": 1, "max_chase_ratio": 0.02},
            decision_context={"execution_contract": execution_contract},
        )
        self.assertTrue(result.should_execute)
        self.assertEqual(result.reason, "intraday_pullback_confirmed")
        self.assertEqual(result.features["execution_profile"], "pullback")
        self.assertEqual(result.features["trigger_rule"], "vwap_pullback_support")

    def test_single_exact_positive_open_action_value_becomes_candidate_not_real_budget(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="P",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "range",
                    "setup_type": "fundamental_timing_setup",
                    "action_name": "open",
                    "sample_count": 1,
                    "reward_mean": 1800.0,
                    "reward_sum": 1800.0,
                    "win_rate": 1.0,
                    "confidence_score": 0.16,
                    "action_preference": "positive_candidate_open",
                    "max_position_impact": 0.03,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 1,
                        "exact_state_real_trade_sample_count": 1,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.62,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="breakout above opening range with volume confirmation",
                    invalidation_level=9000.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    entry_quality="strong",
                    evidence_role="entry_timing",
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.58),
            ],
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertGreater(ratio, 0.0)
        self.assertLess(ratio, 0.02)
        self.assertIn("candidate_positive_action_preference", reasons)
        self.assertNotIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(alpha_ev["positive_action_value_candidate"])
        self.assertTrue(alpha_ev["candidate_positive_action_preference"])
        self.assertFalse(alpha_ev["positive_action_value"])
        self.assertFalse(alpha_ev["qualified_positive_expectancy"])

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )

        self.assertTrue(allowed)
        self.assertEqual(authority["authority_type"], "exploration_probe")
        self.assertTrue(authority["strong_current_evidence"])

    def test_positive_open_action_value_can_seed_candidate_from_neutral_pm_with_current_evidence(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
        )
        seed = _positive_open_action_value_seed(
            ticker="RB",
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 6,
                    "reward_mean": 520.0,
                    "reward_sum": 3120.0,
                    "win_rate": 0.67,
                    "confidence_score": 0.72,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 6,
                        "exact_state_real_trade_sample_count": 6,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed["side"], "long")
        self.assertGreater(seed["seed_position_ratio"], 0.0)
        self.assertLessEqual(seed["seed_position_ratio"], 0.05)
        control_diagnostics = {
            "alpha_setup_ev_fusion": {
                **seed["evidence"],
                "qualified_positive_expectancy": True,
                "positive_action_value": True,
            }
        }
        allowed, authority = _final_contract_authority(
            control_reasons=["positive_open_action_value_seed", "qualified_positive_expectancy"],
            control_diagnostics=control_diagnostics,
        )
        self.assertTrue(allowed)
        self.assertEqual(authority["authority_type"], "real_budget_entry")
        self.assertIn("positive_open_action_value_seed", authority["reason_effects"]["release_signals"])

    def test_legacy_action_preference_without_action_preference_cannot_seed_open_candidate(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
        )
        seed = _positive_open_action_value_seed(
            ticker="RB",
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 6,
                    "reward_mean": 520.0,
                    "reward_sum": 3120.0,
                    "win_rate": 0.67,
                    "confidence_score": 0.72,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 6,
                        "exact_state_real_trade_sample_count": 6,
                        "amplification_scope_quality": "exact_real_state",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed, {})

    def test_top_level_canonical_action_preference_can_seed_open_candidate_without_payload_duplicate(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
        )
        seed = _positive_open_action_value_seed(
            ticker="RB",
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 6,
                    "reward_mean": 520.0,
                    "reward_sum": 3120.0,
                    "win_rate": 0.67,
                    "confidence_score": 0.72,
                    "action_preference": "positive_candidate_open",
                    "max_position_impact": 0.04,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 6,
                        "exact_state_real_trade_sample_count": 6,
                        "amplification_scope_quality": "exact_real_state",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed["side"], "long")
        self.assertGreater(seed["seed_position_ratio"], 0.0)
        self.assertEqual(seed["row"]["action_preference"], "positive_candidate_open")

    def test_single_exact_positive_open_action_value_seeds_probe_candidate_only(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
        )
        seed = _positive_open_action_value_seed(
            ticker="P",
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "range",
                    "setup_type": "fundamental_timing_setup",
                    "action_name": "open",
                    "sample_count": 1,
                    "reward_mean": 1800.0,
                    "reward_sum": 1800.0,
                    "win_rate": 1.0,
                    "confidence_score": 0.16,
                    "action_preference": "positive_candidate_open",
                    "max_position_impact": 0.03,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 1,
                        "exact_state_real_trade_sample_count": 1,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                    },
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed["side"], "long")
        self.assertTrue(seed["candidate_positive_action_preference"])
        self.assertFalse(seed["mature_positive_action_value"])

    def test_legacy_positive_add_or_scale_preference_cannot_seed_open_candidate(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
        )

        for legacy_preference in ("positive_candidate_add", "positive_candidate_scale"):
            with self.subTest(legacy_preference=legacy_preference):
                seed = _positive_open_action_value_seed(
                    ticker="RB",
                    alpha_setup_action_values=[
                        {
                            "ticker": "RB",
                            "side": "long",
                            "horizon_class": "short",
                            "market_regime": "trend",
                            "setup_type": "trend_breakout_setup",
                            "action_name": "open",
                            "sample_count": 6,
                            "reward_mean": 520.0,
                            "reward_sum": 3120.0,
                            "win_rate": 0.67,
                            "confidence_score": 0.72,
                            "action_preference": "observe_or_probe",
                            "max_position_impact": 0.04,
                            "payload": {
                                "source": "alpha_setup_profile_action_value",
                                "real_trade_reward_count": 6,
                                "exact_state_real_trade_sample_count": 6,
                                "amplification_scope_quality": "exact_real_state",
                                "action_preference": legacy_preference,
                                "reward_source": "trade_episode",
                            },
                        }
                    ],
                    analyst_signals=[technical],
                    opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
                    market_confirmation={"confirmation_score": 0.70},
                    full_config={},
                    max_position_ratio=0.05,
                )

                self.assertEqual(seed, {})

    def test_sector_fallback_action_value_cannot_seed_open_candidate(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
        )
        seed = _positive_open_action_value_seed(
            ticker="RB",
            alpha_setup_action_values=[
                {
                    "ticker": "*",
                    "side": "long",
                    "action_name": "open",
                    "sample_count": 8,
                    "reward_mean": 1000.0,
                    "reward_sum": 8000.0,
                    "confidence_score": 0.90,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.05,
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.75},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed, {})

    def test_similar_setup_sector_fallback_cannot_seed_open_candidate(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
        )
        seed = _positive_open_action_value_seed(
            ticker="RB",
            alpha_setup_action_values=[
                {
                    "ticker": "*",
                    "side": "long",
                    "action_name": "open",
                    "sample_count": 8,
                    "reward_mean": 1000.0,
                    "reward_sum": 8000.0,
                    "confidence_score": 0.90,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.05,
                    "payload": {
                        "source": "similar_alpha_setup_sql",
                        "strict_no_lookahead": True,
                        "exact_ticker_sample_count": 0,
                    },
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.75},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed, {})

    def test_counterfactual_prior_only_open_action_value_cannot_seed_open_candidate(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
        )
        seed = _positive_open_action_value_seed(
            ticker="RB",
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "action_name": "open",
                    "sample_count": 5,
                    "reward_mean": 800.0,
                    "reward_sum": 4000.0,
                    "confidence_score": 0.80,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.05,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "counterfactual_prior_only": True,
                        "counterfactual_reward_count": 5,
                        "real_trade_reward_count": 0,
                    },
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.75},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed, {})

    def test_similar_setup_coarse_same_ticker_open_action_value_cannot_promote_real_authority(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="J",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "J",
                    "side": "*",
                    "action_name": "open",
                    "sample_count": 12,
                    "reward_mean": 500.0,
                    "reward_sum": 6000.0,
                    "win_rate": 0.80,
                    "confidence_score": 0.68,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "setup_type": "*",
                    "market_regime": "*",
                    "payload": {
                        "source": "similar_alpha_setup_sql",
                        "strict_no_lookahead": True,
                        "exact_ticker_sample_count": 12,
                        "exact_ticker_real_trade_sample_count": 12,
                        "exact_state_real_trade_sample_count": 0,
                        "partial_state_real_trade_sample_count": 12,
                        "similar_real_trade_sample_count": 0,
                        "real_trade_reward_count": 12,
                        "amplification_scope_quality": "partial_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                        "prior_only_no_direct_authority": True,
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="breakout above opening range with volume confirmation",
                    invalidation_level=3300.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    entry_quality="strong",
                    evidence_role="entry_timing",
                )
            ],
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertLessEqual(ratio, 0.02)
        self.assertNotIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertFalse(alpha_ev["positive_action_value"])
        self.assertTrue(alpha_ev["positive_action_value_candidate"])
        self.assertTrue(alpha_ev["action_value_stats"]["exact_ticker_support"])
        self.assertFalse(alpha_ev["action_value_stats"]["real_amplification_support"])
        self.assertEqual(alpha_ev["action_value_stats"]["scope_quality"], "partial_real_state")

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )

        self.assertTrue(allowed)
        self.assertNotEqual(authority["authority_type"], "real_budget_entry")
        self.assertEqual(authority["authority_type"], "exploration_probe")
        self.assertTrue(authority["strong_current_evidence"])

    def test_similar_setup_exact_state_open_action_value_can_promote_authority(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="RB",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "action_name": "open",
                    "sample_count": 5,
                    "reward_mean": 500.0,
                    "reward_sum": 2500.0,
                    "win_rate": 0.80,
                    "confidence_score": 0.68,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "setup_type": "trend_breakout_setup",
                    "market_regime": "trend",
                    "horizon_class": "short",
                    "payload": {
                        "source": "similar_alpha_setup_sql",
                        "strict_no_lookahead": True,
                        "exact_ticker_sample_count": 5,
                        "exact_ticker_real_trade_sample_count": 5,
                        "exact_state_real_trade_sample_count": 5,
                        "partial_state_real_trade_sample_count": 0,
                        "similar_real_trade_sample_count": 0,
                        "real_trade_reward_count": 5,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="breakout above opening range with volume confirmation",
                    invalidation_level=3300.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    entry_quality="strong",
                    evidence_role="entry_timing",
                )
            ],
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertGreater(ratio, 0.02)
        self.assertIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(alpha_ev["positive_action_value"])
        self.assertTrue(alpha_ev["action_value_stats"]["exact_ticker_support"])
        self.assertTrue(alpha_ev["action_value_stats"]["real_amplification_support"])
        self.assertEqual(alpha_ev["action_value_stats"]["scope_quality"], "exact_real_state")

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )

        self.assertTrue(allowed)
        self.assertEqual(authority["authority_type"], "real_budget_entry")

    def test_legacy_or_incomplete_direct_action_value_cannot_promote_real_authority(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="RB",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "action_name": "open",
                    "sample_count": 8,
                    "reward_mean": 700.0,
                    "reward_sum": 5600.0,
                    "win_rate": 0.75,
                    "confidence_score": 0.82,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.05,
                    "setup_type": "tradeable_candidate",
                    "market_regime": "trend",
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 8,
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="breakout above opening range with volume confirmation",
                    invalidation_level=3300.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    entry_quality="strong",
                    evidence_role="entry_timing",
                )
            ],
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertLessEqual(ratio, 0.02)
        self.assertNotIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(alpha_ev["positive_action_value_candidate"])
        self.assertFalse(alpha_ev["positive_action_value"])
        self.assertFalse(alpha_ev["action_value_stats"]["real_amplification_support"])
        self.assertEqual(alpha_ev["action_value_stats"]["scope_quality"], "partial_real_state")

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )

        self.assertTrue(allowed)
        self.assertNotEqual(authority["authority_type"], "real_budget_entry")
        self.assertEqual(authority["authority_type"], "exploration_probe")
        self.assertTrue(authority["strong_current_evidence"])

    def test_exact_state_positive_action_value_with_tail_loss_needs_strong_current_evidence(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="TA",
            position_ratio=-0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="watch_for_trigger"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "TA",
                    "side": "short",
                    "action_name": "open",
                    "sample_count": 6,
                    "reward_mean": 300.0,
                    "reward_sum": 1800.0,
                    "win_rate": 0.67,
                    "confidence_score": 0.72,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "setup_type": "breakdown_setup",
                    "market_regime": "trend",
                    "horizon_class": "short",
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 6,
                        "exact_state_real_trade_sample_count": 6,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                        "loss_reward_count": 1,
                        "tail_loss_count": 1,
                        "worst_reward": -3800.0,
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BEARISH,
                    confidence=0.55,
                    opportunity_state="tradeable_candidate",
                    invalidation_level=5600.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    entry_trigger="current breakdown below support is confirmed",
                    evidence_role="entry_timing",
                    metadata={
                        "action_evidence_contract": {
                            "opportunity_state": "tradeable_candidate",
                            "opportunity_state": "tradeable_candidate",
                            "trigger_valid": True,
                            "invalidation_present": True,
                            "entry_trigger": "current breakdown below support is confirmed",
                        }
                    },
                )
            ],
            market_confirmation={"confirmation_score": 0.46},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertLessEqual(abs(ratio), 0.02)
        self.assertIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(alpha_ev["positive_action_value_candidate"])
        self.assertTrue(alpha_ev["positive_action_value"])
        self.assertFalse(alpha_ev["tail_loss_blocks_real_amplification"])
        self.assertTrue(alpha_ev["qualified_positive_expectancy"])

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )

        self.assertTrue(authority["strong_current_evidence"])
        self.assertEqual(authority["authority_type"], "watchlist_only")
        self.assertIn("negative_expectancy", authority["reason_codes"])
        self.assertNotEqual(authority["authority_type"], "real_budget_entry")

    def test_positive_hold_action_value_does_not_qualify_new_open(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="P",
            position_ratio=0.03,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[
                {
                    "side": "long",
                    "lifecycle_state": "protected",
                    "sample_count": 5,
                    "net_pnl": 6000.0,
                    "profit_factor": 2.0,
                    "win_rate": 0.60,
                    "confidence_score": 0.70,
                    "max_position_impact": 0.04,
                }
            ],
            alpha_setup_action_values=[
                {
                    "side": "long",
                    "action_name": "hold_position",
                    "sample_count": 4,
                    "reward_mean": 1500.0,
                    "reward_sum": 6000.0,
                    "win_rate": 0.75,
                    "confidence_score": 0.72,
                    "action_preference": "controlled_probe_or_hold",
                    "max_position_impact": 0.04,
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.60,
                    invalidation_level=3300.0,
                    opportunity_state="tradeable_candidate",
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.55),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation={"confirmation_score": 0.62},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertLess(ratio, 0.03)
        self.assertNotIn("qualified_positive_expectancy", reasons)
        detail = diagnostics["alpha_setup_ev_fusion"]
        self.assertEqual(detail["intended_action"], "open")
        self.assertTrue(detail["open_action_value_missing"])
        self.assertEqual(detail["matched_action_value_count"], 0)
        self.assertFalse(detail["qualified_positive_expectancy"])

    def test_sql_similar_setup_retrieval_uses_only_past_trading_dates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            rows = [
                ("past-win", "2025-03-05", "RB", "long", "ferrous", "short", "trend", "breakout_setup", "combo", "open_long", 10, 10, 1200.0, 20.0),
                ("past-win-2", "2025-03-07", "RB", "long", "ferrous", "short", "trend", "breakout_setup", "combo", "open_long", 8, 8, 800.0, 15.0),
                ("future-loss", "2025-03-20", "RB", "long", "ferrous", "short", "trend", "breakout_setup", "combo", "open_long", 8, 8, -9000.0, 10.0),
            ]
            for row in rows:
                cursor.execute(
                    """
                    INSERT INTO alpha_setup_sample (
                        id, config_id, trading_date, ticker, side, sector, horizon_class,
                        market_regime, setup_type, data_combo, scope_key, source_type,
                        recommendation_id, action_taken, pm_action, auditor_decision,
                        trader_status, target_lots, executed_lots, net_pnl, commission,
                        holding_days, outcome_label, setup_quality_score, opportunity_state,
                        evidence_json, result_json, created_at, payload_json
                    ) VALUES (?, 'cfg', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'trade', ?, ?, ?, 'pass',
                        'executed', ?, ?, ?, ?, 1, 'observed', 0.7, 'tradeable_candidate', '{}', '{}', ?, '{}')
                    """,
                    (
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                        row[4],
                        row[5],
                        row[6],
                        row[7],
                        row[8],
                        f"{row[2]}|{row[3]}|{row[5]}|{row[6]}|{row[7]}|{row[8]}",
                        row[0],
                        row[9],
                        row[9],
                        row[10],
                        row[11],
                        row[12],
                        row[13],
                        row[1],
                    ),
                )
            conn.commit()
            conn.close()

            values = db.get_similar_alpha_setup_action_values(
                config_id="cfg",
                ticker="RB",
                sector="ferrous",
                side="long",
                horizon_class="short",
                market_regime="trend",
                setup_type="breakout_setup",
                trading_date="2025-03-10",
                limit=3,
            )

        open_value = next(row for row in values if row["action_name"] == "open")
        self.assertEqual(open_value["sample_count"], 2)
        self.assertGreater(open_value["reward_sum"], 0)
        self.assertNotIn("2025-03-20", open_value["payload"]["episode_dates"])
        self.assertEqual(open_value["payload"]["date_filter"], "alpha_setup_sample.trading_date < decision_date")
        self.assertEqual(open_value["payload"]["amplification_scope_quality"], "exact_real_state")
        self.assertEqual(open_value["payload"]["exact_state_real_trade_sample_count"], 2)
        self.assertEqual(open_value["payload"]["partial_state_real_trade_sample_count"], 0)
        self.assertEqual(open_value["action_preference"], "")
        self.assertEqual(open_value["payload"]["prior_role"], "weak_prior_not_action_preference")
        self.assertEqual(open_value["payload"]["canonical_action_preference_source"], "none_for_similar_sql_prior")
        self.assertEqual(open_value["payload"]["action_preference"], "")

    def test_sql_similar_setup_retrieval_counts_counterfactual_as_prior_not_exact_trade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            cursor.execute(
                """
                INSERT INTO alpha_setup_sample (
                    id, config_id, trading_date, ticker, side, sector, horizon_class,
                    market_regime, setup_type, data_combo, scope_key, source_type,
                    recommendation_id, action_taken, pm_action, auditor_decision,
                    trader_status, target_lots, executed_lots, net_pnl, commission,
                    holding_days, outcome_label, setup_quality_score, opportunity_state,
                    evidence_json, result_json, created_at, payload_json
                ) VALUES ('counterfactual-rb-1', 'cfg', '2025-03-05', 'RB', 'long', 'ferrous',
                    'short', 'trend', 'breakout_setup', 'counterfactual_no_trade_missed',
                    'RB|long|short|trend|breakout_setup|counterfactual_no_trade_missed',
                    'counterfactual_missed_alpha', 'counterfactual:1', 'open_long',
                    'counterfactual_counterfactual_open', 'not_executed_counterfactual',
                    'counterfactual_not_executed', 1, 0, 1000.0, 0.0, 3, 'profit',
                    0.0, 'tradeable_candidate', '{}', '{}', '2025-03-10', '{}')
                """
            )
            conn.commit()
            conn.close()

            values = db.get_similar_alpha_setup_action_values(
                config_id="cfg",
                ticker="RB",
                sector="ferrous",
                side="long",
                horizon_class="short",
                market_regime="trend",
                setup_type="breakout_setup",
                trading_date="2025-03-10",
                limit=3,
            )

        open_value = next(row for row in values if row["action_name"] == "open")
        self.assertAlmostEqual(open_value["reward_sum"], 350.0)
        self.assertEqual(open_value["payload"]["exact_ticker_sample_count"], 0)
        self.assertEqual(open_value["payload"]["exact_ticker_counterfactual_sample_count"], 1)
        self.assertEqual(open_value["payload"]["amplification_scope_quality"], "counterfactual_prior")
        self.assertTrue(open_value["payload"]["counterfactual_prior_only"])
        self.assertEqual(open_value["action_preference"], "")
        self.assertEqual(open_value["payload"]["prior_role"], "weak_prior_not_action_preference")
        self.assertEqual(open_value["payload"]["action_preference"], "")

    def test_direct_alpha_setup_action_value_uses_only_past_sample_dates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            rows = [
                ("past-open", "RB|long|past", "2025-03-07", 1200.0, 0.70),
                ("same-day-open", "RB|long|same", "2025-03-10", 9000.0, 0.99),
                ("future-open", "RB|long|future", "2025-03-20", -9000.0, 0.95),
                ("legacy-open", "RB|long|legacy", None, 5000.0, 0.80),
            ]
            for row_id, scope_key, last_sample_date, reward_mean, confidence in rows:
                cursor.execute(
                    """
                    INSERT INTO alpha_setup_action_value (
                        id, config_id, scope_key, ticker, side, horizon_class, market_regime,
                        setup_type, data_combo, action_name, sample_count, reward_sum,
                        reward_mean, win_rate, confidence_score, action_preference,
                        max_position_impact, last_sample_date, created_at, updated_at,
                        valid_until, active, payload_json
                    ) VALUES (?, 'cfg', ?, 'RB', 'long', 'short', 'trend',
                        'tradeable_candidate', 'combo', 'open', 4, ?, ?, 0.75, ?,
                        'controlled_open_or_add', 0.04, ?, '2025-03-10', '2025-03-10',
                        '2025-04-01', 1, '{}')
                    """,
                    (row_id, scope_key, reward_mean * 4, reward_mean, confidence, last_sample_date),
                )
            conn.commit()
            conn.close()

            values = db.get_alpha_setup_action_values(
                config_id="cfg",
                ticker="RB",
                side="long",
                horizon_class="short",
                market_regime="trend",
                trading_date="2025-03-10",
                limit=10,
            )

        ids = {row["id"] for row in values}
        self.assertIn("past-open", ids)
        self.assertNotIn("same-day-open", ids)
        self.assertNotIn("future-open", ids)
        self.assertNotIn("legacy-open", ids)

    def test_direct_alpha_setup_action_value_prioritizes_real_action_preference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            rows = [
                (
                    "weak-high-conf",
                    "RB|long|weak",
                    "trend_breakout_setup",
                    0.99,
                    "",
                    9000.0,
                    {"reward_source": "counterfactual_prior", "amplification_scope_quality": "counterfactual_prior"},
                ),
                (
                    "real-pref",
                    "RB|long|real",
                    "trend_breakout_setup",
                    0.40,
                    "positive_candidate_open",
                    800.0,
                    {
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                        "amplification_scope_quality": "exact_real_state",
                    },
                ),
            ]
            for row_id, scope_key, setup_type, confidence, preference, reward_mean, payload in rows:
                cursor.execute(
                    """
                    INSERT INTO alpha_setup_action_value (
                        id, config_id, scope_key, ticker, side, horizon_class, market_regime,
                        setup_type, data_combo, action_name, sample_count, reward_sum,
                        reward_mean, win_rate, confidence_score, action_preference,
                        max_position_impact, last_sample_date, created_at, updated_at,
                        valid_until, active, payload_json
                    ) VALUES (?, 'cfg', ?, 'RB', 'long', 'short', 'trend',
                        ?, 'combo', 'open', 4, ?, ?, 0.75, ?, ?,
                        0.04, '2025-03-07', '2025-03-07', '2025-03-07',
                        '2025-04-01', 1, ?)
                    """,
                    (row_id, scope_key, setup_type, reward_mean * 4, reward_mean, confidence, preference, json.dumps(payload)),
                )
            conn.commit()
            conn.close()

            values = db.get_alpha_setup_action_values(
                config_id="cfg",
                ticker="RB",
                side="long",
                horizon_class="short",
                market_regime="trend",
                setup_type="trend_breakout_setup",
                trading_date="2025-03-10",
                limit=1,
            )

        self.assertEqual(values[0]["id"], "real-pref")
        self.assertEqual(values[0]["action_preference"], "positive_candidate_open")
        self.assertEqual(values[0]["reward_source"], "trade_episode")
        self.assertEqual(values[0]["evidence_scope"], "exact_real_state")
        self.assertEqual(values[0]["action_value_lane"], "open")

    def test_direct_alpha_setup_action_value_requires_complete_state_for_exact_quality(self):
        from tools.agent_tools.research.alpha_setup import upsert_alpha_setup_sample_and_profile

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            for idx, setup_type in enumerate(("generic_trade_setup", "trend_breakout_setup"), start=1):
                upsert_alpha_setup_sample_and_profile(
                    cursor,
                    cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                    config_id="cfg",
                    trading_date=f"2025-03-0{idx}",
                    sample={
                        "ticker": "RB",
                        "side": "long",
                        "sector": "ferrous",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "setup_type": setup_type,
                        "data_combo": f"combo-{setup_type}",
                        "source_type": "trade",
                        "recommendation_id": f"rec-{idx}",
                        "action_taken": "open_long",
                        "pm_action": "open_long",
                        "target_lots": 1,
                        "current_lots": 0,
                        "executed_lots": 1,
                        "net_pnl": 1200.0,
                        "commission": 20.0,
                        "opportunity_state": "tradeable_candidate",
                    },
                )
            conn.commit()
            cursor.execute(
                """
                SELECT setup_type, payload_json
                FROM alpha_setup_action_value
                WHERE action_name = 'open'
                ORDER BY setup_type
                """
            )
            rows = [dict(row) for row in cursor.fetchall()]
            conn.close()

        payload_by_setup = {
            row["setup_type"]: json.loads(row["payload_json"] or "{}")
            for row in rows
        }
        self.assertEqual(
            payload_by_setup["generic_trade_setup"]["amplification_scope_quality"],
            "partial_real_state",
        )
        self.assertEqual(
            payload_by_setup["generic_trade_setup"]["exact_state_real_trade_sample_count"],
            0,
        )
        self.assertEqual(
            payload_by_setup["generic_trade_setup"]["partial_state_real_trade_sample_count"],
            1,
        )
        self.assertEqual(
            payload_by_setup["trend_breakout_setup"]["amplification_scope_quality"],
            "exact_real_state",
        )
        self.assertEqual(
            payload_by_setup["trend_breakout_setup"]["exact_state_real_trade_sample_count"],
            1,
        )

    def test_adaptive_policy_state_uses_only_past_learning_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            events = [
                ("event-past", "2025-03-07"),
                ("event-same", "2025-03-10"),
                ("event-future", "2025-03-20"),
            ]
            for event_id, event_date in events:
                cursor.execute(
                    """
                    INSERT INTO learning_event_log (
                        id, config_id, trading_date, event_type, agent, scope_type,
                        scope_key, evidence_json, action_json, verifier, created_at, status
                    ) VALUES (?, 'cfg', ?, 'adaptive_policy_state', 'reviewer',
                        'template', 'RB:long', '{}', '{}', 'test', ?, 'applied')
                    """,
                    (event_id, event_date, event_date),
                )
            policies = [
                ("policy-past", "event-past", None, 0.70),
                ("policy-same", "event-same", None, 0.95),
                ("policy-future", "event-future", None, 0.90),
                ("policy-explicit-past", None, "2025-03-05", 0.80),
                ("policy-explicit-same", None, "2025-03-10", 0.85),
                ("policy-unknown", None, None, 0.99),
            ]
            for row_id, event_id, source_trading_date, confidence in policies:
                cursor.execute(
                    """
                    INSERT INTO adaptive_policy_state (
                        id, config_id, ticker, side, setup_type, horizon_class,
                        market_regime, policy_type, policy_action, multiplier,
                        confidence_score, sample_count, reason, source_event_id,
                        source_trading_date, created_at, valid_until, payload_json, active
                    ) VALUES (?, 'cfg', 'RB', 'long', '*', 'short', 'trend',
                        ?, 'protect', 1.0, ?, 4, 'test boundary', ?, ?,
                        '2025-03-10', '2025-04-01', '{}', 1)
                    """,
                    (row_id, f"test_policy_{row_id}", confidence, event_id, source_trading_date),
                )
            conn.commit()
            conn.close()

            rows = db.get_adaptive_policy_state(
                config_id="cfg",
                ticker="RB",
                side="long",
                setup_type="*",
                horizon_class="short",
                market_regime="trend",
                trading_date="2025-03-10",
            )

        ids = {row["id"] for row in rows}
        self.assertIn("policy-past", ids)
        self.assertIn("policy-explicit-past", ids)
        self.assertNotIn("policy-same", ids)
        self.assertNotIn("policy-future", ids)
        self.assertNotIn("policy-explicit-same", ids)
        self.assertNotIn("policy-unknown", ids)

    def test_learning_boundary_excludes_same_day_and_future_overlays_and_profiles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            overlay_rows = [
                ("overlay-past", "2025-03-07", "max_total_margin_ratio", "0.18"),
                ("overlay-same", "2025-03-10", "probe_margin_ratio_same", "0.02"),
                ("overlay-future", "2025-03-20", "probe_margin_ratio_future", "0.03"),
            ]
            for row_id, source_date, key, value in overlay_rows:
                cursor.execute(
                    """
                    INSERT INTO config_learning_overlay (
                        id, config_id, trading_date, param_key, learned_value_json,
                        previous_value_json, scope_type, scope_key, source,
                        confidence_score, sample_count, reason, source_event_id,
                        rollback_value_json, created_at, valid_until, active
                    ) VALUES (?, 'cfg', ?, ?, ?, '0', 'global', '*', 'reviewer',
                        0.8, 2, 'test boundary', NULL, '0', ?, '2025-04-01', 1)
                    """,
                    (row_id, source_date, key, value, source_date),
                )
            profile_rows = [
                ("profile-past", "RB|long|past", "2025-03-07", 0.70),
                ("profile-same", "RB|long|same", "2025-03-10", 0.95),
                ("profile-future", "RB|long|future", "2025-03-20", 0.90),
                ("profile-legacy", "RB|long|legacy", None, 0.99),
            ]
            for row_id, scope_key, last_sample_date, confidence in profile_rows:
                cursor.execute(
                    """
                    INSERT INTO alpha_setup_profile (
                        id, config_id, ticker, side, sector, horizon_class,
                        market_regime, setup_type, data_combo, scope_key,
                        lifecycle_state, profile_state_hint, sample_count, trade_count,
                        win_count, loss_count, net_pnl, confidence_score,
                        max_position_impact, last_sample_date, created_at,
                        updated_at, valid_until, active, payload_json
                    ) VALUES (?, 'cfg', 'RB', 'long', 'ferrous', 'short',
                        'trend', 'tradeable_candidate', 'combo', ?, 'deployable',
                        'open', 4, 4, 3, 1, 2000.0, ?, 0.04, ?,
                        '2025-03-10', '2025-03-10', '2025-04-01', 1, '{}')
                    """,
                    (row_id, scope_key, confidence, last_sample_date),
                )
            conn.commit()
            conn.close()

            overlays = db.get_config_learning_overlay(config_id="cfg", trading_date="2025-03-10")
            profiles = db.get_alpha_setup_profiles(
                config_id="cfg",
                ticker="RB",
                sector="ferrous",
                side="long",
                horizon_class="short",
                market_regime="trend",
                trading_date="2025-03-10",
                limit=10,
            )

        self.assertEqual({row["id"] for row in overlays}, {"overlay-past"})
        profile_ids = {row["id"] for row in profiles}
        self.assertIn("profile-past", profile_ids)
        self.assertNotIn("profile-same", profile_ids)
        self.assertNotIn("profile-future", profile_ids)
        self.assertNotIn("profile-legacy", profile_ids)

    def test_learning_boundary_excludes_same_day_and_future_performance_and_prompt_priors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            events = [
                ("digest-past-event", "2025-03-07"),
                ("digest-same-event", "2025-03-10"),
                ("digest-future-event", "2025-03-20"),
            ]
            for event_id, event_date in events:
                cursor.execute(
                    """
                    INSERT INTO learning_event_log (
                        id, config_id, trading_date, event_type, agent, scope_type,
                        scope_key, evidence_json, action_json, verifier, created_at, status
                    ) VALUES (?, 'cfg', ?, 'analyst_learning_digest', 'reviewer',
                        'analyst', 'technical:RB', '{}', '{}', 'test', ?, 'applied')
                    """,
                    (event_id, event_date, event_date),
                )
            template_rows = [
                ("template-past", "trend_breakout_past", "2025-03-07", 0.70),
                ("template-same", "trend_breakout_same", "2025-03-10", 0.95),
                ("template-future", "trend_breakout_future", "2025-03-20", 0.90),
                ("template-legacy", "trend_breakout_legacy", None, 0.99),
            ]
            for row_id, template, last_sample_date, confidence in template_rows:
                cursor.execute(
                    """
                    INSERT INTO setup_type_performance (
                        id, config_id, ticker, side, setup_type, horizon_class,
                        market_regime, sample_count, win_rate, net_pnl, avg_pnl,
                        profit_factor, confidence_score, last_sample_date,
                        last_updated, valid_until, payload_json
                    ) VALUES (?, 'cfg', 'RB', 'long', ?, 'short',
                        'trend', 4, 0.75, 2000.0, 500.0, 2.0, ?, ?,
                        '2025-03-10', '2025-04-01', '{}')
                    """,
                    (row_id, template, confidence, last_sample_date),
                )
            analyst_rows = [
                ("analyst-past", "long", "2025-03-07", 0.70),
                ("analyst-same", "short", "2025-03-10", 0.95),
                ("analyst-future", "neutral", "2025-03-20", 0.90),
                ("analyst-legacy", "mixed", None, 0.99),
            ]
            for row_id, signal_side, last_sample_date, confidence in analyst_rows:
                cursor.execute(
                    """
                    INSERT INTO analyst_performance (
                        id, config_id, analyst, ticker, sector, horizon_class,
                        signal_side, sample_count, hit_rate, avg_pnl, net_pnl,
                        confidence_score, last_sample_date, last_updated,
                        valid_until, payload_json
                    ) VALUES (?, 'cfg', 'technical', 'RB', 'ferrous', 'short',
                        ?, 4, 0.75, 500.0, 2000.0, ?, ?,
                        '2025-03-10', '2025-04-01', '{}')
                    """,
                    (row_id, signal_side, confidence, last_sample_date),
                )
            digest_rows = [
                ("digest-past", "digest-past-event", 0.70),
                ("digest-same", "digest-same-event", 0.95),
                ("digest-future", "digest-future-event", 0.90),
                ("digest-unknown", None, 0.99),
            ]
            for row_id, event_id, confidence in digest_rows:
                cursor.execute(
                    """
                    INSERT INTO analyst_learning_digest (
                        id, config_id, analyst, ticker, sector, horizon_class,
                        market_regime, digest_text, confidence_score, sample_count,
                        source_event_id, created_at, valid_until, accepted, payload_json
                    ) VALUES (?, 'cfg', 'technical', 'RB', 'ferrous', 'short',
                        'trend', 'test digest', ?, 4, ?, '2025-03-10',
                        '2025-04-01', 1, '{}')
                    """,
                    (row_id, confidence, event_id),
                )
            hypothesis_rows = [
                ("hypothesis-past", "2025-03-07", 0.70),
                ("hypothesis-same", "2025-03-10", 0.95),
                ("hypothesis-future", "2025-03-20", 0.90),
            ]
            for row_id, source_date, confidence in hypothesis_rows:
                cursor.execute(
                    """
                    INSERT INTO exploratory_hypothesis (
                        id, config_id, trading_date, scope_type, scope_key,
                        ticker, sector, side, horizon_class, market_regime,
                        hypothesis_text, evidence_summary, suggested_use,
                        confidence_score, sample_count, status, created_at,
                        valid_until, payload_json
                    ) VALUES (?, 'cfg', ?, 'research', 'RB:long', 'RB',
                        'ferrous', 'long', 'short', 'trend', 'test hypothesis',
                        'test evidence', 'prior only', ?, 4, 'candidate',
                        ?, '2025-04-01', '{}')
                    """,
                    (row_id, source_date, confidence, source_date),
                )
            conn.commit()
            conn.close()

            templates = db.get_setup_type_performance(
                config_id="cfg",
                ticker="RB",
                side="long",
                trading_date="2025-03-10",
                limit=10,
            )
            analysts = db.get_analyst_performance(
                config_id="cfg",
                ticker="RB",
                horizon_class="short",
                trading_date="2025-03-10",
                limit=10,
            )
            digests = db.get_analyst_learning_digest(
                config_id="cfg",
                analyst="technical",
                ticker="RB",
                sector="ferrous",
                horizon_class="short",
                market_regime="trend",
                trading_date="2025-03-10",
                max_items=10,
            )
            hypotheses = db.get_exploratory_hypotheses(
                config_id="cfg",
                ticker="RB",
                sector="ferrous",
                side="long",
                horizon_class="short",
                market_regime="trend",
                trading_date="2025-03-10",
                limit=10,
            )

        self.assertEqual({row["id"] for row in templates}, {"template-past"})
        self.assertEqual({row["id"] for row in analysts}, {"analyst-past"})
        self.assertEqual({row["id"] for row in digests}, {"digest-past"})
        self.assertEqual({row["id"] for row in hypotheses}, {"hypothesis-past"})

    def test_provisional_policy_state_uses_only_past_source_dates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            rows = [
                ("policy-past", "probe_only_past", "2025-03-07", 0.70),
                ("policy-same", "probe_only_same", "2025-03-10", 0.95),
                ("policy-future", "probe_only_future", "2025-03-20", 0.90),
                ("policy-legacy", "probe_only_legacy", None, 0.99),
            ]
            for row_id, policy_action, source_date, confidence in rows:
                cursor.execute(
                    """
                    INSERT INTO provisional_policy_state (
                        id, config_id, ticker, side, setup_type,
                        horizon_class, policy_action, multiplier,
                        confidence_score, event_type, sample_count, reason,
                        source_trading_date, rollback_value_json, created_at,
                        valid_until, active, payload_json
                    ) VALUES (?, 'cfg', 'RB', 'long', 'trend_breakout',
                        'short', ?, 0.35, ?, 'test', 3,
                        'test boundary', ?, '{}', '2025-03-10',
                        '2025-04-01', 1, '{}')
                    """,
                    (row_id, policy_action, confidence, source_date),
                )
            conn.commit()
            conn.close()

            policies = db.get_provisional_policy_state(
                config_id="cfg",
                ticker="RB",
                side="long",
                setup_type="trend_breakout",
                horizon_class="short",
                trading_date="2025-03-10",
            )

        self.assertEqual({row["id"] for row in policies}, {"policy-past"})

    def test_hold_exit_action_value_reduces_profit_when_current_confirmation_weakens(self):
        current_position = SimpleNamespace(margin_used=100000.0, unrealized_pnl=3000.0)
        ratio, reasons, _notes, diagnostics = _apply_winning_template_continuation_control(
            ticker="P",
            position_ratio=0.0,
            current_ratio=0.04,
            current_position=current_position,
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "action_name": "hold_position",
                    "sample_count": 4,
                    "reward_mean": -1200.0,
                    "reward_sum": -4800.0,
                    "confidence_score": 0.65,
                    "action_preference": "cap_reduce_or_revalidate",
                }
            ],
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.45),
            ],
            market_confirmation={"confirmation_score": 0.42},
            opportunity_scorecard={"long": {"final_state": "watch_for_trigger"}},
            full_config={},
        )

        self.assertAlmostEqual(ratio, 0.02)
        self.assertIn("hold_exit_action_value_protection", reasons)
        detail = diagnostics["winning_template_continuation"]
        self.assertEqual(detail["decision"], "learned_hold_exit_reduce")
        self.assertEqual(detail["selected_action_value"]["action_name"], "hold_position")

    def test_single_exact_positive_exit_candidate_protects_profit_when_confirmation_weakens(self):
        current_position = SimpleNamespace(margin_used=100000.0, unrealized_pnl=3000.0)
        ratio, reasons, _notes, diagnostics = _apply_winning_template_continuation_control(
            ticker="P",
            position_ratio=0.04,
            current_ratio=0.04,
            current_position=current_position,
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "range",
                    "setup_type": "news_event_setup",
                    "action_name": "exit",
                    "sample_count": 1,
                    "reward_mean": 2000.0,
                    "reward_sum": 2000.0,
                    "confidence_score": 0.16,
                    "action_preference": "positive_candidate_exit",
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 1,
                        "exact_state_real_trade_sample_count": 1,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_exit",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation={"confirmation_score": 0.42},
            opportunity_scorecard={"long": {"final_state": "watch_for_trigger"}},
            full_config={},
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("hold_exit_action_value_protection", reasons)
        detail = diagnostics["winning_template_continuation"]
        self.assertEqual(detail["decision"], "learned_exit_action_value_protective_exit")
        self.assertEqual(detail["selected_action_value"]["action_preference"], "positive_candidate_exit")

    def test_hold_exit_action_value_does_not_override_strong_profitable_continuation(self):
        current_position = SimpleNamespace(margin_used=100000.0, unrealized_pnl=3000.0)
        ratio, reasons, _notes, diagnostics = _apply_winning_template_continuation_control(
            ticker="P",
            position_ratio=0.0,
            current_ratio=0.04,
            current_position=current_position,
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "action_name": "hold_position",
                    "sample_count": 4,
                    "reward_mean": -1200.0,
                    "reward_sum": -4800.0,
                    "confidence_score": 0.65,
                    "action_preference": "cap_reduce_or_revalidate",
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    opportunity_state="tradeable_candidate",
                    invalidation_level=9000.0,
                    trigger_valid=True,
                    invalidation_present=True,
                )
            ],
            market_confirmation={"confirmation_score": 0.70},
            opportunity_scorecard={"long": {"final_state": "tradeable_candidate"}},
            full_config={},
        )

        self.assertGreaterEqual(ratio, 0.03)
        self.assertIn("winning_template_continuation", reasons)
        self.assertNotIn("hold_exit_action_value_protection", reasons)
        self.assertEqual(
            diagnostics["winning_template_continuation"]["decision"],
            "preserve_profitable_same_scope_position",
        )

    def test_no_continuation_profitable_hold_reduces_without_action_value(self):
        current_position = SimpleNamespace(margin_used=100000.0, unrealized_pnl=3000.0)
        ratio, reasons, _notes, diagnostics = _apply_winning_template_continuation_control(
            ticker="BU",
            position_ratio=0.0,
            current_ratio=-0.06,
            current_position=current_position,
            alpha_setup_action_values=[],
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.45),
            ],
            market_confirmation={"confirmation_score": 0.50},
            opportunity_scorecard={"short": {"final_state": "watch_for_trigger"}},
            full_config={},
        )

        self.assertAlmostEqual(ratio, -0.03)
        self.assertIn("winning_template_continuation_protective_reduce", reasons)
        detail = diagnostics["winning_template_continuation"]
        self.assertEqual(detail["decision"], "protective_reduce_no_continuation")
        self.assertTrue(detail["prevents_position_matched_profit_giveback"])

    def test_profit_protective_reduce_is_not_overridden_by_min_hold_lifecycle(self):
        current_position = SimpleNamespace(
            shares=5,
            margin_used=100000.0,
            unrealized_pnl=3000.0,
            entry_date="2025-03-09",
        )
        ratio, reasons, _notes, diagnostics = _apply_holding_rebalance_control(
            ticker="P",
            trading_date="2025-03-10",
            position_ratio=0.03,
            current_ratio=0.06,
            current_position=current_position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            long_scores={"strength": 0.10},
            short_scores={"strength": 0.05},
            market_confirmation={"confirmation_score": 0.42},
            full_config={},
            fusion_context={},
            risk_level=RiskLevel.SAFE,
            adaptive_policy_state=[],
            prior_control_reasons=["winning_template_continuation_protective_reduce"],
        )

        self.assertAlmostEqual(ratio, 0.03)
        self.assertNotIn("holding_period_control", reasons)
        self.assertEqual(
            diagnostics["holding_rebalance_control"]["decision"],
            "allow_profit_protective_reduce_before_min_hold",
        )

    def test_positive_exit_action_value_can_turn_weak_profitable_hold_into_protective_exit(self):
        current_position = SimpleNamespace(margin_used=100000.0, unrealized_pnl=3000.0)
        ratio, reasons, _notes, diagnostics = _apply_winning_template_continuation_control(
            ticker="P",
            position_ratio=0.04,
            current_ratio=0.04,
            current_position=current_position,
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "action_name": "exit",
                    "sample_count": 4,
                    "reward_mean": 1800.0,
                    "reward_sum": 7200.0,
                    "confidence_score": 0.70,
                    "action_preference": "controlled_probe_or_hold",
                }
            ],
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.45),
            ],
            market_confirmation={"confirmation_score": 0.42},
            opportunity_scorecard={"long": {"final_state": "watch_for_trigger"}},
            full_config={},
        )

        self.assertAlmostEqual(ratio, 0.0)
        self.assertIn("hold_exit_action_value_protection", reasons)
        detail = diagnostics["winning_template_continuation"]
        self.assertEqual(detail["decision"], "learned_exit_action_value_protective_exit")
        self.assertTrue(detail["protective_exit"])

    def test_real_probe_release_blocks_watch_for_trigger_semantic_even_with_strong_current_evidence(self):
        release, detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
                "unknown_alpha_probe",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_invalidation_or_stop": True,
                    "scorecard_state": "watch_for_trigger",
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.70,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(release)
        self.assertIn("alpha_setup_open_action_value_missing", detail["soft_blocks"])
        self.assertFalse(detail["hard_watchlist"])
        self.assertTrue(detail["watch_for_trigger_semantic_block"])

    def test_real_probe_release_allows_tradeable_soft_block_with_current_trigger(self):
        release, detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "horizon_consistency_probe_cap",
                "unknown_alpha_probe",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_invalidation_or_stop": True,
                    "has_tradeable_support": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.70,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertTrue(release)
        self.assertIn("alpha_setup_open_action_value_missing", detail["soft_blocks"])
        self.assertFalse(detail["watch_for_trigger_semantic_block"])

    def test_tradeable_analyst_candidate_soft_gates_to_controlled_probe_not_wait(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.66,
            opportunity_state="tradeable_candidate",
            trigger_valid=True,
            invalidation_present=True,
            metadata={
                "action_evidence_contract": {
                    "opportunity_state": "tradeable_candidate",
                    "opportunity_state": "tradeable_candidate",
                    "trigger_valid": True,
                    "invalidation_present": True,
                }
            },
        )
        reasons = [
            "alpha_setup_ev_fusion",
            "scorecard_current_tradeable_probe_seed",
            "market_confirmation_conflict",
            "single_high_quality_probe_only",
            "unknown_alpha_probe",
        ]
        diagnostics = {
            "alpha_setup_ev_fusion": {
                "scorecard_state": "tradeable_candidate",
                "has_tradeable_support": True,
                "has_invalidation_or_stop": True,
                "current_confirmation_score": 0.58,
                "independent_support_count": 1,
                "negative_action_value": False,
                "negative_profile": False,
                "repeat_loss_without_new_evidence": False,
            }
        }

        candidate, candidate_detail = _qualified_analyst_tradeable_probe_candidate(
            analyst_signals=[signal],
            target_side="long",
            control_reasons=reasons,
            control_diagnostics=diagnostics,
            account_equity=5_000_000.0,
            current_price=5500.0,
            multiplier=10.0,
            margin_rate=0.10,
            margin_available=1_000_000.0,
        )
        candidate_ratio = _minimum_real_probe_candidate_ratio(
            current_ratio=0.0,
            pre_control_ratio=0.008,
            probe_release=False,
            analyst_tradeable_probe=candidate,
        )
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=candidate_ratio,
            control_reasons=[*reasons, "analyst_tradeable_probe_candidate"],
            probe_release=False,
            alpha_ev_blocks_real_probe=False,
            analyst_tradeable_probe=candidate,
        )
        allowed, authority = _final_contract_authority(
            control_reasons=[*reasons, "analyst_tradeable_probe_candidate"],
            control_diagnostics={
                **diagnostics,
                "analyst_tradeable_probe_candidate": candidate_detail,
            },
        )

        self.assertTrue(candidate)
        self.assertEqual(candidate_detail["decision"], "allow_controlled_probe_candidate")
        self.assertGreater(candidate_ratio, 0.0)
        self.assertTrue(should_attempt)
        self.assertTrue(allowed)
        self.assertEqual(authority["authority_type"], "exploration_probe")
        self.assertIn("analyst_tradeable_probe_candidate", authority["reason_codes"])

    def test_watch_for_trigger_analyst_candidate_does_not_use_controlled_probe_channel(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.66,
            opportunity_state="watch_for_trigger",
            trigger_valid=True,
            invalidation_present=True,
            metadata={
                "action_evidence_contract": {
                    "opportunity_state": "watch_for_trigger",
                    "opportunity_state": "watch_for_trigger",
                    "trigger_valid": True,
                    "invalidation_present": True,
                }
            },
        )
        reasons = [
            "alpha_setup_ev_fusion",
            "scorecard_current_tradeable_probe_seed",
            "pm_watch_for_trigger_probe_cap",
            "unknown_alpha_probe",
        ]
        diagnostics = {
            "alpha_setup_ev_fusion": {
                "scorecard_state": "watch_for_trigger",
                "has_tradeable_support": False,
                "has_invalidation_or_stop": True,
                "current_confirmation_score": 0.70,
                "independent_support_count": 1,
            }
        }

        candidate, candidate_detail = _qualified_analyst_tradeable_probe_candidate(
            analyst_signals=[signal],
            target_side="long",
            control_reasons=reasons,
            control_diagnostics=diagnostics,
            account_equity=5_000_000.0,
            current_price=5500.0,
            multiplier=10.0,
            margin_rate=0.10,
            margin_available=1_000_000.0,
        )
        candidate_ratio = _minimum_real_probe_candidate_ratio(
            current_ratio=0.0,
            pre_control_ratio=0.008,
            probe_release=False,
            analyst_tradeable_probe=candidate,
        )
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=candidate_ratio,
            control_reasons=reasons,
            probe_release=False,
            alpha_ev_blocks_real_probe=False,
            analyst_tradeable_probe=candidate,
        )

        self.assertFalse(candidate)
        self.assertIn("no_same_side_tradeable_triggered_analyst", candidate_detail["blocked_reasons"])
        self.assertEqual(candidate_ratio, 0.0)
        self.assertFalse(should_attempt)

    def test_negative_learning_blocks_tradeable_analyst_controlled_probe_candidate(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.70,
            opportunity_state="tradeable_candidate",
            trigger_valid=True,
            invalidation_present=True,
        )
        candidate, detail = _qualified_analyst_tradeable_probe_candidate(
            analyst_signals=[signal],
            target_side="short",
            control_reasons=["alpha_setup_ev_fusion", "negative_expectancy_cap_or_exit"],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "negative_action_value": True,
                }
            },
            account_equity=5_000_000.0,
            current_price=5500.0,
            multiplier=10.0,
            margin_rate=0.10,
            margin_available=1_000_000.0,
        )

        self.assertFalse(candidate)
        self.assertIn("negative_or_tail_loss_present", detail["blocked_reasons"])
        self.assertIn("negative_learning_profile_present", detail["blocked_reasons"])

    def test_watch_for_trigger_release_cannot_enter_minimum_one_lot_path(self):
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=0.004,
            control_reasons=[
                "alpha_setup_ev_fusion",
                "pm_watch_for_trigger_probe_cap",
                "horizon_consistency_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
                "unknown_alpha_probe",
            ],
            probe_release=True,
            alpha_ev_blocks_real_probe=False,
        )

        self.assertFalse(should_attempt)

    def test_tradeable_release_enters_minimum_one_lot_path(self):
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=0.004,
            control_reasons=[
                "alpha_setup_ev_fusion",
                "horizon_consistency_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
                "unknown_alpha_probe",
            ],
            probe_release=True,
            alpha_ev_blocks_real_probe=False,
        )

        self.assertTrue(should_attempt)

    def test_watch_for_trigger_release_does_not_recover_pre_control_direction_after_soft_zero(self):
        candidate_ratio = _minimum_real_probe_candidate_ratio(
            current_ratio=0.0,
            pre_control_ratio=0.008,
            probe_release=True,
        )
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=candidate_ratio,
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
                "unknown_alpha_probe",
            ],
            probe_release=True,
            alpha_ev_blocks_real_probe=False,
        )

        self.assertGreater(candidate_ratio, 0.0)
        self.assertFalse(should_attempt)

    def test_real_probe_without_release_does_not_recover_soft_zero_direction(self):
        candidate_ratio = _minimum_real_probe_candidate_ratio(
            current_ratio=0.0,
            pre_control_ratio=0.008,
            probe_release=False,
        )

        self.assertEqual(candidate_ratio, 0.0)

    def test_real_probe_release_path_still_respects_negative_expectancy(self):
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=0.004,
            control_reasons=[
                "alpha_setup_ev_fusion",
                "negative_expectancy_cap_or_exit",
                "repeat_loss_watchlist_only",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            probe_release=False,
            alpha_ev_blocks_real_probe=False,
        )

        self.assertFalse(should_attempt)

    def test_real_probe_release_does_not_override_negative_expectancy(self):
        release, detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "negative_expectancy_cap_or_exit",
                "repeat_loss_watchlist_only",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "qualified_positive_expectancy": False,
                    "current_confirmation_score": 0.75,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertFalse(release)
        self.assertTrue(detail["hard_watchlist"])

    def test_final_new_entry_gate_blocks_watch_for_trigger_without_authority(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
                "horizon_consistency_probe_cap",
                "market_confirmation_conflict",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.50,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertTrue(detail["requires_authority"])
        self.assertEqual(detail["decision"], "watchlist_only")
        self.assertIn("pm_watch_for_trigger_probe_cap", detail["weak_markers"])
        self.assertIn("pm_watch_for_trigger_probe_cap", detail["reason_effects"]["soft_limits"])
        self.assertFalse(detail["reason_effects"]["hard_zero"])

    def test_reason_effect_summary_separates_hard_soft_learning_and_release(self):
        summary = reason_effect_summary([
            "trade_auditor_block",
            "market_confirmation_quality_gate",
            "adaptive_policy_cap",
            "qualified_positive_expectancy",
            "positive_open_action_value_seed",
            "hold_exit_action_value_protection",
        ])

        self.assertIn("trade_auditor_block", summary["hard_blocks"])
        self.assertIn("market_confirmation_quality_gate", summary["soft_limits"])
        self.assertIn("adaptive_policy_cap", summary["learning_adjustments"])
        self.assertIn("qualified_positive_expectancy", summary["release_signals"])
        self.assertIn("positive_open_action_value_seed", summary["release_signals"])
        self.assertIn("hold_exit_action_value_protection", summary["learning_adjustments"])
        self.assertTrue(summary["hard_zero"])

    def test_learning_policy_blocks_are_soft_not_hard_risk(self):
        summary = reason_effect_summary([
            "adaptive_policy_block",
            "provisional_policy_block",
            "side_performance_block",
            "conditional_performance_block",
            "positive_open_action_value_seed",
        ])

        self.assertEqual(summary["hard_blocks"], [])
        self.assertFalse(summary["hard_zero"])
        self.assertIn("adaptive_policy_block", summary["soft_limits"])
        self.assertIn("provisional_policy_block", summary["soft_limits"])
        self.assertIn("side_performance_block", summary["soft_limits"])
        self.assertIn("conditional_performance_block", summary["soft_limits"])
        self.assertIn("adaptive_policy_block", summary["learning_adjustments"])
        self.assertIn("provisional_policy_block", summary["learning_adjustments"])
        self.assertIn("positive_open_action_value_seed", summary["release_signals"])

    def test_reason_effect_summary_covers_known_exit_reason_codes(self):
        known_exit_reason_codes = [
            "pm_text_no_trade_blocks_new_entry",
            "pm_text_no_entry_trigger_blocks_new_entry",
            "pm_text_watchlist_only_blocks_new_entry",
            "final_contract_authority_not_met",
            "final_contract_authority_not_met",
            "missing_final_contract_authority",
            "final_contract_authority_watchlist_only",
            "final_contract_authority_real_entry_not_allowed",
            "final_action_contract_watch_for_trigger_probe_block",
            "final_contract_authority_probe_lacks_current_evidence",
            "position_budget_authority_not_met",
            "minimum_real_trade_margin_not_reachable",
            "minimum_real_trade_no_feasible_lot",
            "minimum_one_lot_probe_risk_budget_block",
            "minimum_one_lot_probe",
            "minimum_real_trade_margin_floor_applied",
            "exploration_probe_probe_floor_applied",
            "exploration_probe_no_feasible_lot",
            "real_probe_qualification_not_met",
            "market_confirmation_data_gap",
            "market_confirmation_below_probe_threshold",
            "market_confirmation_below_release_threshold",
            "market_confirmation_score_below_probe_threshold",
            "high_quality_learning_evidence_required",
            "confirmation_below_alpha_release_boost_threshold",
            "missing_explicit_stop_for_alpha_release_boost",
            "generic_memory_cannot_trigger_alpha_release_boost",
            "strategy_memory_watchlist_cap",
            "no_analyst_support_for_target",
            "analyst_signal_conflict",
            "protected_memory_evidence_rejected",
            "static_side_cap",
            "trade_frequency_control",
            "trade_churn_cost_control",
            "weak_signal_combo",
            "opportunity_quality_position_sizing",
            "business_quality_deployable",
            "alpha_setup_ev_fusion",
            "learned_underperformance_block",
            "learned_underperformance_policy",
            "capital_utilization_soft_limit_respected",
            "capital_utilization_guard",
            "capital_utilization_memory_protected",
            "capital_utilization_same_side_add_on",
            "drawdown_control",
            "drawdown_recovery_probe",
            "ticker_loss_control",
            "mature_alpha_release",
            "mature_alpha_with_invalidation",
            "high_quality_bearish_short_probe",
            "position_lifecycle_failed",
            "new_position_loss_revalidation_failed",
            "exploration_probe_reconfirm_failed",
            "exploration_probe_reconfirm_reduce",
            "horizon_consistency_failed_losing_hold",
            "position_lifecycle_loss_revalidation_failed",
            "position_lifecycle_probe_expired",
            "fundamental_anchor_rebalance_cap",
            "reverse_requires_stronger_evidence",
            "intraday_trigger_not_met",
            "intraday_waiting_for_trigger",
            "market_rule_block",
            "market_rule_or_execution_block",
            "near_expiry_new_entry_block",
            "delivery_month_new_entry_block",
            "final_contract_authority_source_mismatch",
            "positive_open_action_value_seed",
            "hold_exit_action_value_protection",
            "winning_template_continuation_protective_reduce",
        ]
        summary = reason_effect_summary(known_exit_reason_codes)
        self.assertFalse(summary["unknown_trade_effects"])
        self.assertIn("pm_text_no_trade_blocks_new_entry", summary["hard_blocks"])
        self.assertIn("market_confirmation_data_gap", summary["soft_limits"])
        self.assertIn("drawdown_control", summary["learning_adjustments"])
        self.assertIn("mature_alpha_release", summary["release_signals"])

    def test_final_new_entry_gate_blocks_watch_for_trigger_release_even_with_strong_current_evidence(self):
        release_allowed, release_detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "has_tradeable_support": False,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.70,
                    "independent_support_count": 1,
                }
            },
        )
        strong_allowed, strong_detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_tradeable_support": False,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.60,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertFalse(release_allowed)
        self.assertEqual(release_detail["decision"], "watchlist_only")
        self.assertEqual(release_detail["authority_type"], "watchlist_only")
        self.assertIn("pm_watch_for_trigger_probe_cap", release_detail["reason_effects"]["soft_limits"])
        self.assertTrue(release_detail["watch_for_trigger_semantic_block"])
        self.assertFalse(strong_allowed)
        self.assertEqual(strong_detail["authority_type"], "watchlist_only")
        self.assertTrue(strong_detail["strong_current_evidence"])
        self.assertTrue(strong_detail["watch_for_trigger_semantic_block"])
        self.assertFalse(strong_detail["reason_effects"]["hard_zero"])

    def test_final_new_entry_gate_allows_conditional_watch_for_trigger_probe_contract(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "scorecard_current_tradeable_probe_seed",
                "pm_watch_for_trigger_probe_cap",
                "unknown_alpha_probe",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "has_tradeable_support": True,
                    "setup_quality_ok": True,
                    "has_invalidation_or_stop": True,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": False,
                    "technical_opposes_side": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "negative_action_value": False,
                    "repeat_loss_without_new_evidence": False,
                    "current_confirmation_score": 0.50,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertTrue(allowed)
        self.assertEqual(detail["authority_type"], "exploration_probe")
        self.assertEqual(detail["decision"], "allow_exploration_probe")
        self.assertTrue(detail["conditional_trigger_authority"])
        self.assertTrue(detail["requires_intraday_confirmation"])
        self.assertFalse(detail["can_execute_without_intraday_trigger"])
        self.assertFalse(detail["watch_for_trigger_block"])
        self.assertFalse(detail["watch_for_trigger_semantic_block"])
        self.assertFalse(detail["open_action_evidence"])
        self.assertIn("conditional_trigger_authority", detail["reason_codes"])

    def test_conditional_watch_for_trigger_real_pm_evidence_snapshot_gets_authority(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.62,
            opportunity_state="watch_for_trigger",
            entry_trigger="wait for post-open break below support with volume confirmation",
            invalidation_level=3520.0,
            trigger_valid=False,
            evidence_role="entry_timing",
            metadata={
                "action_evidence_contract": {
                    "opportunity_state": "watch_for_trigger",
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "invalidation_present": True,
                    "entry_trigger": "wait for post-open break below support with volume confirmation",
                }
            },
        )
        alpha_ev = _current_open_evidence_snapshot(
            side="short",
            analyst_signals=[signal],
            opportunity_scorecard={
                "short": {
                    "final_state": "watch_for_trigger",
                    "score": 0.56,
                    "supporting_signal_count": 1,
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "invalidation_present": True,
                    "entry_trigger": "wait for post-open break below support with volume confirmation",
                    "gating_failures": [],
                }
            },
            market_confirmation={"confirmation_score": 0.50},
            ev_cfg={"real_trade_min_analyst_confidence": 0.45},
        )

        self.assertFalse(alpha_ev["has_tradeable_support"])
        self.assertTrue(alpha_ev["has_monitorable_setup"])
        self.assertFalse(alpha_ev["trade_authority"]["watch_for_trigger_without_setup"])

        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "scorecard_current_tradeable_probe_seed",
                "pm_watch_for_trigger_probe_cap",
            ],
            control_diagnostics={"alpha_setup_ev_fusion": alpha_ev},
        )

        self.assertTrue(allowed)
        self.assertTrue(detail["conditional_trigger_authority"])
        self.assertTrue(detail["requires_intraday_confirmation"])
        self.assertFalse(detail["can_execute_without_intraday_trigger"])
        self.assertFalse(detail["watch_for_trigger_semantic_block"])

    def test_final_new_entry_gate_blocks_watch_for_trigger_without_setup_quality(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "scorecard_current_tradeable_probe_seed",
                "pm_watch_for_trigger_probe_cap",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "has_tradeable_support": True,
                    "setup_quality_ok": False,
                    "has_invalidation_or_stop": True,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": False,
                    "technical_opposes_side": False,
                    "current_confirmation_score": 0.50,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["authority_type"], "watchlist_only")
        self.assertFalse(detail["conditional_trigger_authority"])
        self.assertTrue(detail["watch_for_trigger_block"])

    def test_final_new_entry_gate_allows_tradeable_release_or_strong_current_evidence(self):
        release_allowed, release_detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "horizon_consistency_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "has_tradeable_support": True,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.70,
                    "independent_support_count": 1,
                }
            },
        )
        strong_allowed, strong_detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "horizon_consistency_probe_cap",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_tradeable_support": True,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.60,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertTrue(release_allowed)
        self.assertEqual(release_detail["decision"], "allow_exploration_probe")
        self.assertEqual(release_detail["authority_type"], "exploration_probe")
        self.assertFalse(release_detail["watch_for_trigger_semantic_block"])
        self.assertTrue(strong_allowed)
        self.assertEqual(strong_detail["authority_type"], "exploration_probe")
        self.assertTrue(strong_detail["strong_current_evidence"])
        self.assertFalse(strong_detail["watch_for_trigger_semantic_block"])

    def test_final_new_entry_gate_blocks_conflict_probe_without_stronger_confirmation(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "market_confirmation_conflict",
                "pm_watch_for_trigger_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
                "single_high_quality_probe_only",
                "unknown_alpha_probe",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_invalidation_or_stop": True,
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.56,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["authority_type"], "watchlist_only")
        self.assertTrue(detail["weak_conflict_probe"])
        self.assertIn("weak_conflict_probe_requires_stronger_confirmation", detail["reason_codes"])

    def test_conflict_probe_with_tradeable_current_confirmation_can_still_probe(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "market_confirmation_conflict",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_invalidation_or_stop": True,
                    "has_tradeable_support": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.62,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertTrue(allowed)
        self.assertEqual(detail["authority_type"], "exploration_probe")
        self.assertFalse(detail["weak_conflict_probe"])

    def test_hard_risk_reason_still_blocks_even_with_release_signal(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "trade_auditor_block",
                "real_probe_positive_or_strong_confirmation_release",
                "qualified_positive_expectancy",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_tradeable_support": True,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "current_confirmation_score": 0.72,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertTrue(detail["hard_zero"])
        self.assertIn("trade_auditor_block", detail["reason_effects"]["hard_blocks"])
        self.assertIn("qualified_positive_expectancy", detail["reason_effects"]["release_signals"])
        self.assertEqual(detail["authority_type"], "watchlist_only")

    def test_watch_for_trigger_fundamental_news_stack_does_not_release_real_probe(self):
        release, release_detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
                "horizon_consistency_probe_cap",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "fundamental_supports_side": True,
                    "news_supports_side": True,
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.60,
                    "independent_support_count": 2,
                }
            },
        )
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
                "horizon_consistency_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "fundamental_supports_side": True,
                    "news_supports_side": True,
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.60,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertFalse(release)
        self.assertTrue(release_detail["watch_for_trigger_without_confirmation"])
        self.assertFalse(allowed)
        self.assertTrue(detail["requires_authority"])
        self.assertEqual(detail["decision"], "watchlist_only")
        self.assertTrue(detail["watch_for_trigger_without_confirmation"])

    def test_watch_for_trigger_single_fundamental_market_score_does_not_release_real_probe(self):
        """Regression for 2025-04-08 ZN: market score cannot replace tradeable setup."""
        release, release_detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "horizon_consistency_probe_cap",
                "pm_watch_for_trigger_probe_cap",
                "single_high_quality_probe_only",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "technical_supports_side": False,
                    "fundamental_supports_side": True,
                    "news_supports_side": False,
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.70,
                    "independent_support_count": 1,
                }
            },
        )
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "horizon_consistency_probe_cap",
                "market_confirmation_conflict",
                "pm_watch_for_trigger_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
                "single_high_quality_probe_only",
                "unknown_alpha_probe",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "technical_supports_side": False,
                    "fundamental_supports_side": True,
                    "news_supports_side": False,
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.70,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(release)
        self.assertFalse(release_detail["current_trade_authority"])
        self.assertTrue(release_detail["watch_for_trigger_without_confirmation"])
        self.assertFalse(allowed)
        self.assertEqual(detail["decision"], "watchlist_only")

    def test_tradeable_candidate_with_current_confirmation_can_still_probe(self):
        release, release_detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "horizon_consistency_probe_cap",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "has_tradeable_support": True,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": False,
                    "current_confirmation_score": 0.62,
                    "independent_support_count": 2,
                }
            },
        )
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "horizon_consistency_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "has_tradeable_support": True,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": False,
                    "current_confirmation_score": 0.62,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertTrue(release)
        self.assertTrue(release_detail["executable_setup_confirmation"])
        self.assertTrue(allowed)
        self.assertTrue(detail["strong_current_evidence"])
        self.assertIn("horizon_consistency_probe_cap", detail["reason_effects"]["soft_limits"])

    def test_fundamental_tradeable_candidate_with_strong_market_confirmation_can_probe(self):
        """Regression for 2025-04-10 ZN / 2025-04-11 HC over-block.

        A medium-horizon fundamental tradeable setup is not a weak direction-only
        opinion when the current market confirmation is strong and technical is
        not explicitly against the side. It should survive soft probe caps as a
        controlled probe candidate, but it is still not real-budget authority
        without open-action evidence.
        """
        control_reasons = [
            "alpha_setup_ev_fusion",
            "horizon_consistency_probe_cap",
            "single_high_quality_probe_only",
        ]
        diagnostics = {
            "alpha_setup_ev_fusion": {
                "scorecard_state": "tradeable_candidate",
                "strong_realtime_evidence": False,
                "strong_market_confirmation": True,
                "technical_supports_side": False,
                "technical_opposes_side": False,
                "fundamental_supports_side": True,
                "news_supports_side": False,
                "has_tradeable_support": True,
                "has_invalidation_or_stop": True,
                "qualified_positive_expectancy": False,
                "positive_action_value": False,
                "positive_profile": False,
                "current_confirmation_score": 0.75,
                "independent_support_count": 1,
            }
        }

        release, release_detail = _qualified_real_probe_release(
            control_reasons=control_reasons,
            control_diagnostics=diagnostics,
        )
        allowed, detail = _final_contract_authority(
            control_reasons=[
                *control_reasons,
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics=diagnostics,
        )

        self.assertTrue(release)
        self.assertTrue(release_detail["market_confirmation"])
        self.assertTrue(release_detail["executable_setup_confirmation"])
        self.assertTrue(allowed)
        self.assertEqual(detail["decision"], "allow_exploration_probe")
        self.assertEqual(detail["authority_type"], "exploration_probe")
        self.assertTrue(detail["open_action_evidence"])
        self.assertTrue(detail["strong_current_evidence"])

    def test_static_prior_weights_never_create_trade_authority(self):
        allowed, detail = _final_contract_authority(
            control_reasons=["alpha_setup_ev_fusion", "pm_watch_for_trigger_probe_cap"],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "fundamental_supports_side": True,
                    "news_supports_side": True,
                    "has_tradeable_support": False,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": False,
                    "current_confirmation_score": 0.80,
                    "independent_support_count": 2,
                }
            },
            full_config={
                "analyst_weight_policy": {
                    "mode": "evidence_router",
                    "static_weights_mode": "prior_only",
                    "static_weights_can_create_trade_authority": False,
                    "watch_for_trigger_cannot_open_position": True,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["decision"], "watchlist_only")
        self.assertFalse(detail["open_action_evidence"])
        self.assertEqual(detail["analyst_prior_policy"]["static_weights_mode"], "prior_only")
        self.assertEqual(detail["analyst_prior_audit"]["semantic_role"], "cold_start_prior_only")
        self.assertFalse(detail["analyst_prior_audit"]["can_create_trade_authority"])
        self.assertFalse(detail["analyst_prior_audit"]["can_open_position_directly"])
        self.assertIn("portfolio_manager.sector_weights", detail["analyst_prior_audit"]["runtime_compat_fields"])

    def test_watch_for_trigger_probe_config_is_audited_candidate_not_authority(self):
        allowed, detail = _final_contract_authority(
            control_reasons=["alpha_setup_ev_fusion", "pm_watch_for_trigger_probe_cap"],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "watch_for_trigger_without_setup": True,
                    "has_tradeable_support": False,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": False,
                    "strong_market_confirmation": True,
                    "current_confirmation_score": 0.72,
                    "independent_support_count": 2,
                }
            },
            full_config={
                "analyst_weight_policy": {
                    "watch_for_trigger_cannot_open_position": True,
                    "mode": "evidence_router",
                    "static_weights_can_create_trade_authority": False,
                },
                "portfolio_manager": {
                    "holding_rebalance_control": {
                        "watch_for_trigger_new_entry": {
                            "enabled": True,
                            "allow_probe": True,
                            "semantic_role": "observation_candidate_only",
                            "can_create_trade_authority": False,
                            "requires_final_contract_authority": True,
                        }
                    }
                },
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["authority_type"], "watchlist_only")
        audit = detail["watch_for_trigger_semantic_audit"]
        self.assertTrue(audit["allow_probe"])
        self.assertEqual(audit["semantic_role"], "observation_candidate_only")
        self.assertFalse(audit["can_create_trade_authority"])
        self.assertTrue(audit["requires_final_contract_authority"])
        self.assertIn("watch_for_trigger_new_entry", detail["source_parameters"])

    def test_positive_expectancy_without_current_open_evidence_does_not_get_real_budget(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "qualified_positive_expectancy",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "has_tradeable_support": False,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "technical_supports_side": False,
                    "technical_entry_timing_supports_side": False,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "current_confirmation_score": 0.80,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["decision"], "watchlist_only")
        self.assertTrue(detail["qualified_positive"])
        self.assertFalse(detail["open_action_evidence"])

    def test_fundamental_tradeable_candidate_with_technical_opposition_stays_watchlist(self):
        release, detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "horizon_consistency_probe_cap",
                "single_high_quality_probe_only",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "technical_supports_side": False,
                    "technical_opposes_side": True,
                    "fundamental_supports_side": True,
                    "has_tradeable_support": True,
                    "qualified_positive_expectancy": False,
                    "current_confirmation_score": 0.80,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(release)
        self.assertFalse(detail["current_trade_authority"])

    def test_learned_positive_tradeable_candidate_flows_from_scorecard_to_real_probe(self):
        """Full chain: analyst setup -> scorecard -> PM seed -> action-value -> real probe."""
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.NEUTRAL,
            confidence=0.35,
            opportunity_state="no_opportunity",
        )
        fundamental = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.50,
            opportunity_state="tradeable_candidate",
            setup_quality_score=0.76,
            business_quality_score=0.72,
            entry_trigger="enter only if futures hold above support after selloff and basis remains backwardation",
            would_change_view_if="long setup invalid if basis flips to contango or inventory builds for two consecutive weeks",
            horizon_class="medium",
        )
        news = AnalystSignal(
            agent_name="commodity_news",
            signal=Signal.NEUTRAL,
            confidence=0.30,
            opportunity_state="no_opportunity",
        )
        scorecard = build_opportunity_scorecard(
            ticker="ZN",
            analyst_signals=[technical, fundamental, news],
            market_confirmation={"confirmation_score": 0.75},
            data_quality_summary={"critical_gap": False, "fundamental_trade_setup_gap": False},
            config={
                "weak_confirmation_threshold": 0.45,
                "tradeable_threshold": 0.58,
                "min_tradeable_candidate_setup_quality": 0.55,
                "single_tradeable_candidate_setup_confirmation_score": 0.68,
                "single_tradeable_candidate_setup_min_business_quality": 0.60,
                "single_tradeable_candidate_setup_min_confidence": 0.42,
                "technical_opposition_min_confidence": 0.45,
            },
        )
        side, seed_ratio, row = _scorecard_probe_seed(
            opportunity_scorecard=scorecard,
            control={
                "watch_for_trigger_new_entry": {
                    "allow_probe": True,
                    "probe_floor_ratio": 0.005,
                    "probe_max_ratio": 0.010,
                    "scorecard_tradeable_candidate_probe_min_confirmation_score": 0.68,
                }
            },
        )

        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="ZN",
            position_ratio=seed_ratio,
            current_ratio=0.0,
            opportunity_scorecard=scorecard,
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "ZN",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 4,
                    "reward_mean": 600.0,
                    "reward_sum": 2400.0,
                    "win_rate": 0.75,
                    "confidence_score": 0.66,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.03,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 4,
                        "exact_state_real_trade_sample_count": 4,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[technical, fundamental, news],
            market_confirmation={"confirmation_score": 0.75},
            full_config={},
            max_position_ratio=0.05,
        )
        control_reasons = [
            *reasons,
            "horizon_consistency_probe_cap",
        ]
        release, release_detail = _qualified_real_probe_release(
            control_reasons=control_reasons,
            control_diagnostics=diagnostics,
        )
        candidate_ratio = _minimum_real_probe_candidate_ratio(
            current_ratio=0.0,
            pre_control_ratio=ratio,
            probe_release=release,
        )
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=candidate_ratio,
            control_reasons=[
                *control_reasons,
                "real_probe_positive_or_strong_confirmation_release",
            ],
            probe_release=release,
            alpha_ev_blocks_real_probe=False,
        )
        allowed, final_detail = _final_contract_authority(
            control_reasons=[
                *control_reasons,
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics=diagnostics,
        )

        self.assertEqual(scorecard["long"]["final_state"], "probe_candidate")
        self.assertTrue(scorecard["long"]["single_tradeable_candidate_setup_confirmed"])
        self.assertEqual(side, "long")
        self.assertEqual(row["final_state"], "probe_candidate")
        self.assertGreater(seed_ratio, 0.0)
        self.assertGreater(ratio, seed_ratio)
        self.assertIn("qualified_positive_expectancy", reasons)
        self.assertTrue(diagnostics["alpha_setup_ev_fusion"]["qualified_positive_expectancy"])
        self.assertTrue(release)
        self.assertTrue(release_detail["qualified_positive_expectancy"])
        self.assertGreater(candidate_ratio, 0.0)
        self.assertTrue(should_attempt)
        self.assertTrue(allowed)
        self.assertEqual(final_detail["decision"], "allow_real_new_entry")

    def test_pm_trade_decision_matrix_has_single_non_bypassable_outlet(self):
        """Matrix guardrail: the final outlet is permissive for edge, strict for weak/negative ideas."""
        cases = {
            "watch_for_trigger_no_current_or_learned_edge": (
                False,
                ["alpha_setup_ev_fusion", "pm_watch_for_trigger_probe_cap"],
                {
                    "scorecard_state": "watch_for_trigger",
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "current_confirmation_score": 0.50,
                    "independent_support_count": 1,
                },
            ),
            "positive_expectancy_watch_for_trigger_without_current_open_evidence": (
                False,
                ["alpha_setup_ev_fusion", "pm_watch_for_trigger_probe_cap"],
                {
                    "scorecard_state": "watch_for_trigger",
                    "has_tradeable_support": False,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "positive_profile": False,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "current_confirmation_score": 0.45,
                    "independent_support_count": 1,
                },
            ),
            "confirmed_tradeable_candidate_probe": (
                True,
                [
                    "alpha_setup_ev_fusion",
                    "horizon_consistency_probe_cap",
                    "real_probe_positive_or_strong_confirmation_release",
                ],
                {
                    "scorecard_state": "tradeable_candidate",
                    "has_tradeable_support": True,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "technical_supports_side": False,
                    "technical_opposes_side": False,
                    "current_confirmation_score": 0.75,
                    "independent_support_count": 1,
                },
            ),
            "technical_opposition_blocks_single_setup": (
                False,
                [
                    "alpha_setup_ev_fusion",
                    "horizon_consistency_probe_cap",
                    "real_probe_positive_or_strong_confirmation_release",
                ],
                {
                    "scorecard_state": "tradeable_candidate",
                    "has_tradeable_support": True,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "technical_supports_side": False,
                    "technical_opposes_side": True,
                    "current_confirmation_score": 0.80,
                    "independent_support_count": 1,
                },
            ),
            "negative_same_scope_without_new_evidence": (
                False,
                [
                    "alpha_setup_ev_fusion",
                    "negative_expectancy_cap_or_exit",
                    "repeat_loss_watchlist_only",
                    "real_probe_positive_or_strong_confirmation_release",
                ],
                {
                    "scorecard_state": "tradeable_candidate",
                    "has_tradeable_support": True,
                    "qualified_positive_expectancy": False,
                    "negative_action_value": True,
                    "repeat_loss_without_new_evidence": True,
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "current_confirmation_score": 0.80,
                    "independent_support_count": 2,
                },
            ),
        }

        for name, (expected, reasons, alpha_ev) in cases.items():
            with self.subTest(name=name):
                allowed, detail = _final_contract_authority(
                    control_reasons=reasons,
                    control_diagnostics={"alpha_setup_ev_fusion": alpha_ev},
                )
                self.assertEqual(allowed, expected, detail)

    def test_positive_expectancy_watch_for_trigger_without_current_open_evidence_stays_watchlist(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "pm_watch_for_trigger_probe_cap",
                "qualified_positive_expectancy",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "technical_entry_timing_supports_side": False,
                    "has_tradeable_support": False,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "positive_profile": False,
                    "current_confirmation_score": 0.40,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["decision"], "watchlist_only")
        self.assertFalse(detail["strong_current_evidence"])
        self.assertTrue(detail["qualified_positive"])
        self.assertFalse(detail["open_action_evidence"])

    def test_watch_for_trigger_positive_history_and_probe_seed_cannot_bypass_final_authority(self):
        """Regression for 2025-03-13 RB: no-trade rationale must not open via probe seed."""
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "scorecard_current_tradeable_probe_seed",
                "pm_watch_for_trigger_probe_cap",
                "horizon_consistency_probe_cap",
                "market_confirmation_quality_gate",
                "market_confirmation_conflict",
                "qualified_positive_expectancy",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": False,
                    "trigger_valid": False,
                    "event_catalyst_supports_side": False,
                    "has_tradeable_support": False,
                    "has_invalidation_or_stop": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "positive_profile": False,
                    "current_confirmation_score": 0.33,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["decision"], "watchlist_only")
        self.assertEqual(detail["authority_type"], "watchlist_only")
        self.assertTrue(detail["watch_for_trigger_block"])
        self.assertTrue(detail["watch_for_trigger_without_setup"])
        self.assertTrue(detail["qualified_positive"])
        self.assertFalse(detail["open_action_evidence"])
        self.assertEqual(detail["max_allowed_margin_ratio"], 0.0)

    def test_final_new_entry_gate_negative_profile_stays_watchlist(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "qualified_positive_expectancy": False,
                    "negative_action_value": True,
                    "has_invalidation_or_stop": True,
                    "current_confirmation_score": 0.75,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertTrue(detail["negative_profile"])

    def test_final_entry_authority_records_source_parameters_and_real_flags(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "real_probe_positive_or_strong_confirmation_release",
                "qualified_positive_expectancy",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "has_tradeable_support": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_invalidation_or_stop": True,
                    "strong_realtime_evidence": True,
                    "current_confirmation_score": 0.72,
                    "independent_support_count": 2,
                }
            },
            full_config={
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "min_real_trade_margin_abs": 40000,
                    "probe_margin_ratio": 0.008,
                    "probe_margin_max_ratio": 0.015,
                    "normal_trade_margin_max_ratio": 0.06,
                },
                "analyst_weight_policy": {
                    "watch_for_trigger_cannot_open_position": True,
                    "strategic_view_cannot_open_position": True,
                },
                "market_confirmation": {"min_confirmation_score_for_new_entry": 0.55},
                "portfolio_manager": {
                    "quality_aware_fusion": {
                        "opportunity_scorecard": {
                            "single_tradeable_candidate_setup_confirmation_score": 0.68,
                        }
                    }
                },
            },
        )

        self.assertTrue(allowed)
        self.assertEqual(detail["authority_type"], "real_budget_entry")
        self.assertTrue(detail["open_action_evidence"])
        self.assertTrue(detail["strong_current_evidence"])
        self.assertIn("source_parameters", detail)
        self.assertEqual(
            detail["source_parameters"]["position_budget_policy"]["min_real_trade_margin_ratio"],
            0.008,
        )

    def test_position_budget_floor_applies_only_to_real_budget_entry(self):
        common = {
            "ticker": "ZZ",
            "target_lots": 1,
            "current_lots": 0,
            "current_price": 5000.0,
            "multiplier": 10.0,
            "margin_rate": 0.10,
            "account_equity": 5_000_000.0,
            "margin_available": 1_000_000.0,
            "max_position_ratio": 0.15,
            "max_net_exposure": 0.50,
            "current_net_exposure": 0.0,
            "current_ticker_exposure": 0.0,
            "full_config": {
                "position_budget_policy": {
                    "enabled": True,
                    "min_real_trade_margin_ratio": 0.008,
                    "min_real_trade_margin_abs": 40000,
                    "probe_margin_ratio": 0.008,
                    "probe_margin_max_ratio": 0.015,
                    "max_single_ticker_margin_ratio": 0.13,
                    "block_below_min_when_cannot_scale": True,
                }
            },
        }
        real_reasons, real_notes, real_diag = [], [], {}
        real_result = _apply_position_budget_policy_for_new_entry(
            **common,
            final_entry_authority={
                "requires_authority": True,
                "authority_type": "real_budget_entry",
                "strong_current_evidence": True,
                "decision": "allow_real_new_entry",
            },
            control_reasons=real_reasons,
            control_notes=real_notes,
            control_diagnostics=real_diag,
        )
        probe_reasons, probe_notes, probe_diag = [], [], {}
        probe_result = _apply_position_budget_policy_for_new_entry(
            **common,
            final_entry_authority={
                "requires_authority": True,
                "authority_type": "exploration_probe",
                "strong_current_evidence": False,
                "max_allowed_margin_ratio": 0.015,
                "decision": "allow_exploration_probe",
            },
            control_reasons=probe_reasons,
            control_notes=probe_notes,
            control_diagnostics=probe_diag,
        )

        self.assertGreater(abs(real_result[0]), 0)
        self.assertGreater(abs(probe_result[0]), 0)
        self.assertLessEqual(probe_result[2], 5_000_000.0 * 0.015)
        self.assertIn("minimum_real_trade_margin_floor_applied", real_reasons)
        self.assertIn("exploration_probe_probe_floor_applied", probe_reasons)
        self.assertEqual(
            probe_diag["position_budget_policy"]["decision"],
            "exploration_probe_probe_floor_applied",
        )

    def test_action_value_trace_separates_open_hold_exit_execution(self):
        trace = _alpha_setup_action_value_trace([
            {"action_name": "open", "action_preference": "controlled_open_or_add"},
            {"action_name": "hold_position", "action_preference": "controlled_probe_or_hold"},
            {"action_name": "exit", "action_preference": "cap_reduce_or_revalidate"},
            {"action_name": "execution_trigger", "action_preference": "observe_or_probe"},
        ])

        self.assertEqual(len(trace["open_action_value"]), 1)
        self.assertEqual(len(trace["hold_action_value"]), 1)
        self.assertEqual(len(trace["exit_action_value"]), 1)
        self.assertEqual(len(trace["execution_action_value"]), 1)
        self.assertIn("open_hold_exit_execution", trace["action_value_contract"])

    def test_phase1_recommendation_exports_analyst_contract_and_final_authority(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="ZZ",
            action=FuturesAction.OPEN_LONG,
            lots=2,
            price=100.0,
            settle_price=100.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="ZZ2505",
            justification="test",
        )
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.70,
            opportunity_state="tradeable_candidate",
            invalidation_level=98.0,
            entry_quality="acceptable",
            trigger_valid=True,
            invalidation_present=True,
            entry_trigger="breakout above opening range with volume confirmation",
            trend_direction="bullish",
            entry_timing_signal="trend_breakout",
            business_quality_score=0.80,
            data_coverage_score=0.90,
            holding_period_hint="1-3 trading days while breakout remains valid",
            justification="Technical breakout setup with volume confirmation and explicit invalidation.",
            metadata={
                "technical_context": {
                    "dominant_direction": "bullish",
                    "action_evidence_contract": {"setup_family": "trend_breakout"},
                }
            },
        )
        signal = apply_trade_research_contract(
            signal,
            {
                "tradeability": "high",
                "sector": "generic",
                "dominant_direction": "bullish",
                "current_trigger_confirmed": True,
                "action_evidence_contract": {
                    "setup_family": "trend_breakout",
                    "entry_trigger": "current breakout above opening range is confirmed",
                    "current_trigger_confirmed": True,
                    "trigger_valid": True,
                    "invalidation_present": True,
                },
                "market_regime": "trend",
            },
            analyst="technical",
            trading_date="2025-03-03",
            ticker="ZZ",
        )
        recommendation = _build_phase1_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="ZZ",
            trading_date="2025-03-03",
            contract_code="ZZ2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=100.0,
                base_price_source=None,
                base_price_date="2025-03-03",
                open_price=100.0,
                prev_close_price=99.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "strategy_controls": {
                    "diagnostics": {
                        "position_budget_policy": {"decision": "minimum_margin_floor_applied"},
                    }
                }
            },
            final_action_contract={
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "ZZ",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": 2,
                "lots_delta": 2,
                "lots_delta_abs": 2,
                "target_position_ratio": 0.02,
                "authority_type": "real_budget_entry",
                "authority_decision": "allow_real_new_entry",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "max_allowed_margin_ratio": 0.12,
                "reason_codes": ["qualified_positive_expectancy"],
                "analyst_prior_audit": {
                    "semantic_role": "cold_start_prior_only",
                    "can_create_trade_authority": False,
                    "can_open_position_directly": False,
                },
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            full_config={},
        )
        snapshot = recommendation.signal_snapshot

        self.assertEqual(snapshot["technical"]["evidence_role"], "entry_timing")
        self.assertTrue(snapshot["technical"]["trigger_valid"])
        self.assertTrue(snapshot["technical"]["invalidation_present"])
        technical_contract = snapshot["technical"]["metadata"]["trade_research_contract"]
        self.assertIn("action_evidence_contract", technical_contract)
        self.assertIn("product_context", technical_contract)
        self.assertEqual(
            technical_contract["product_context"]["differentiation_role"],
            "technical_setup_selection",
        )
        self.assertEqual(snapshot["final_action_contract"]["authority_type"], "real_budget_entry")
        self.assertEqual(snapshot["pm_raw_rationale"], "test")
        self.assertTrue(snapshot["pm_justification_contract"]["recommendation_justification_is_derived"])
        self.assertIn("PM final structured outlet", recommendation.justification)
        self.assertIn("authority_type=real_budget_entry", recommendation.justification)
        self.assertNotEqual(recommendation.justification, snapshot["pm_raw_rationale"])
        self.assertEqual(
            snapshot["final_action_contract"]["analyst_prior_audit"]["semantic_role"],
            "cold_start_prior_only",
        )
        self.assertFalse(
            snapshot["final_action_contract"]["analyst_prior_audit"]["can_open_position_directly"]
        )
        self.assertEqual(snapshot["position_budget_policy"]["decision"], "minimum_margin_floor_applied")
        self.assertEqual(snapshot["active_opportunity_audit"]["version"], "active_opportunity_audit_v1")
        self.assertEqual(snapshot["active_opportunity_audit"]["purpose"], "visibility_only_not_position_rule")
        self.assertEqual(snapshot["active_opportunity_audit"]["decision"]["action"], FuturesAction.OPEN_LONG.value)
        self.assertEqual(snapshot["active_opportunity_audit"]["decision"]["lots"], 2)
        self.assertTrue(snapshot["active_opportunity_audit"]["decision"]["lands_position"])
        self.assertEqual(snapshot["active_opportunity_audit"]["decision"]["authority_type"], "real_budget_entry")
        self.assertIn(
            "action_evidence_contract",
            snapshot["active_opportunity_audit"]["analyst_candidates"][0],
        )
        self.assertGreaterEqual(
            snapshot["active_opportunity_audit"]["opportunity"]["analyst_candidate_count"],
            1,
        )
        self.assertTrue(snapshot["active_opportunity_audit"]["research_contract"]["no_current_decision_impact"])

    def test_phase1_semantic_no_trade_text_blocks_open_recommendation(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="RB",
            action=FuturesAction.OPEN_SHORT,
            lots=8,
            price=3500.0,
            settle_price=3500.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="rb2505",
            justification=(
                "No position warranted for RB. Technical signal is neutral and "
                "no timing trigger exists; keep this as watchlist only."
            ),
        )
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.NEUTRAL,
            confidence=0.50,
            opportunity_state="watch_for_trigger",
            neutral_trigger_condition="Breakout above range high with volume confirmation",
            counterfactual_side="short",
            neutral_watchlist_priority="medium",
        )
        recommendation = _build_phase1_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="RB",
            trading_date="2025-03-04",
            contract_code="rb2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=3500.0,
                base_price_source=None,
                base_price_date="2025-03-04",
                open_price=3500.0,
                prev_close_price=3480.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "lots_delta_abs": 8,
                "reason_codes": "target_plan",
                "strategy_controls": {
                    "diagnostics": {},
                    "reasons": ["test_probe"],
                },
            },
            final_action_contract={
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "RB",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": -8,
                "lots_delta": -8,
                "lots_delta_abs": 8,
                "target_position_ratio": -0.02,
                "authority_type": "exploration_probe",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "max_allowed_margin_ratio": 0.015,
                "reason_codes": ["test_probe"],
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            full_config={},
        )
        snapshot = recommendation.signal_snapshot

        self.assertEqual(recommendation.action, RecommendationAction.HOLD)
        self.assertEqual(recommendation.lots, 0)
        self.assertIn("No position warranted", snapshot["pm_raw_rationale"])
        self.assertNotIn("No position warranted", recommendation.justification)
        self.assertIn("PM final structured outlet", recommendation.justification)
        self.assertIn("authority_type=watchlist_only", recommendation.justification)
        self.assertEqual(snapshot["final_action_contract"]["authority_type"], "watchlist_only")
        self.assertFalse(snapshot["pm_semantic_consistency_gate"]["passed"])
        self.assertIn(
            snapshot["pm_semantic_consistency_gate"]["block_reason"],
            snapshot["final_action_contract"]["reason_codes"],
        )
        self.assertNotIn("pm_internal_draft", snapshot)
        self.assertEqual(snapshot["final_action_contract"]["target_lots"], 0)
        self.assertEqual(snapshot["final_action_contract"]["lots_delta_abs"], 0)
        self.assertEqual(
            snapshot["final_action_contract"]["consistency"]["status"],
            "ok",
        )
        self.assertFalse(snapshot["active_opportunity_audit"]["decision"]["lands_position"])

    def test_phase1_semantic_gate_does_not_block_triggered_exploration_probe(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="BU",
            action=FuturesAction.OPEN_SHORT,
            lots=2,
            price=3100.0,
            settle_price=3100.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="bu2505",
            justification=(
                "Controlled exploration probe: technical breakdown trigger is valid, "
                "price is below VWAP, invalidation is above the morning range."
            ),
        )
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.70,
            opportunity_state="tradeable_candidate",
            entry_trigger="Breakdown below morning range with volume confirmation",
            invalidation_level=3150.0,
            trigger_valid=True,
            invalidation_present=True,
        )
        recommendation = _build_phase1_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="BU",
            trading_date="2025-03-04",
            contract_code="bu2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=3100.0,
                base_price_source=None,
                base_price_date="2025-03-04",
                open_price=3100.0,
                prev_close_price=3120.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "strategy_controls": {
                    "diagnostics": {}
                }
            },
            final_action_contract={
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "BU",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": -2,
                "lots_delta": -2,
                "lots_delta_abs": 2,
                "target_position_ratio": -0.01,
                "authority_type": "exploration_probe",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "max_allowed_margin_ratio": 0.015,
                "reason_codes": ["test_probe"],
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            full_config={},
        )

        self.assertEqual(recommendation.action, RecommendationAction.OPEN_SHORT)
        self.assertEqual(recommendation.lots, 2)
        self.assertIn("Controlled exploration probe", recommendation.signal_snapshot["pm_raw_rationale"])
        self.assertNotIn("Controlled exploration probe", recommendation.justification)
        self.assertIn("authority_type=exploration_probe", recommendation.justification)
        self.assertNotIn("pm_semantic_consistency_gate", recommendation.signal_snapshot)
        self.assertTrue(recommendation.signal_snapshot["active_opportunity_audit"]["decision"]["lands_position"])

    def test_phase1_structured_authority_blocks_bare_exploration_probe_without_current_evidence(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="SR",
            action=FuturesAction.OPEN_LONG,
            lots=8,
            price=5900.0,
            settle_price=5900.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="sr2505",
            justification=(
                "Raw rationale may discuss a possible probe, but final authority "
                "does not contain current entry evidence."
            ),
        )
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.60,
            opportunity_state="watch_for_trigger",
            entry_trigger="requires technical confirmation",
            invalidation_level=5800.0,
        )
        recommendation = _build_phase1_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="SR",
            trading_date="2025-03-07",
            contract_code="sr2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=5900.0,
                base_price_source=None,
                base_price_date="2025-03-07",
                open_price=5900.0,
                prev_close_price=5880.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "lots_delta_abs": 8,
                "reason_codes": "target_plan",
                "strategy_controls": {
                    "diagnostics": {},
                    "reasons": ["alpha_setup_open_action_value_missing"],
                },
            },
            final_action_contract={
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "SR",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": 8,
                "lots_delta": 8,
                "lots_delta_abs": 8,
                "target_position_ratio": 0.02,
                "authority_type": "exploration_probe",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "max_allowed_margin_ratio": 0.015,
                "reason_codes": ["alpha_setup_open_action_value_missing"],
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            full_config={},
        )

        self.assertEqual(recommendation.action, RecommendationAction.HOLD)
        self.assertEqual(recommendation.lots, 0)
        authority = recommendation.signal_snapshot["final_action_contract"]
        self.assertEqual(authority["authority_type"], "watchlist_only")
        self.assertIn("final_contract_authority_probe_lacks_current_evidence", authority["reason_codes"])
        self.assertFalse(recommendation.signal_snapshot["active_opportunity_audit"]["decision"]["lands_position"])

    def test_phase1_structured_authority_ignores_opposite_side_trigger_evidence(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="BU",
            action=FuturesAction.OPEN_SHORT,
            lots=2,
            price=3100.0,
            settle_price=3100.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="bu2505",
            justification="Raw rationale contains a probe idea, but analyst trigger is opposite side.",
        )
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.75,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3050.0,
            trigger_valid=True,
            invalidation_present=True,
            evidence_role="entry_timing",
            entry_quality="strong",
        )
        recommendation = _build_phase1_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="BU",
            trading_date="2025-03-05",
            contract_code="bu2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=3100.0,
                base_price_source=None,
                base_price_date="2025-03-05",
                open_price=3100.0,
                prev_close_price=3120.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "strategy_controls": {
                    "diagnostics": {}
                }
            },
            final_action_contract={
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "BU",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": -2,
                "lots_delta": -2,
                "lots_delta_abs": 2,
                "target_position_ratio": -0.01,
                "authority_type": "exploration_probe",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "watch_for_trigger_block": False,
                "max_allowed_margin_ratio": 0.015,
                "reason_codes": ["unknown_alpha_probe"],
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            full_config={},
        )

        self.assertEqual(recommendation.action, RecommendationAction.HOLD)
        self.assertEqual(recommendation.lots, 0)
        authority = recommendation.signal_snapshot["final_action_contract"]
        self.assertEqual(authority["authority_type"], "watchlist_only")
        self.assertIn("final_contract_authority_probe_lacks_current_evidence", authority["reason_codes"])

    def test_active_opportunity_audit_tracks_watchlist_without_changing_decision(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="ZZ",
            action=FuturesAction.HOLD,
            lots=0,
            price=100.0,
            settle_price=100.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="ZZ2505",
            justification="watchlist",
        )
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.NEUTRAL,
            confidence=0.45,
            opportunity_state="watch_for_trigger",
            neutral_trigger_condition="breakout above 101 with volume confirmation",
            counterfactual_side="long",
            neutral_watchlist_priority="medium",
            metadata={"learning_scope": {"setup_family": "range_breakout"}},
        )
        recommendation = _build_phase1_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="ZZ",
            trading_date="2025-03-03",
            contract_code="ZZ2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=100.0,
                base_price_source=None,
                base_price_date="2025-03-03",
                open_price=100.0,
                prev_close_price=99.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "reason_codes": "final_contract_authority_not_met",
                "strategy_controls": {
                    "diagnostics": {},
                    "reasons": ["watch_for_trigger_cannot_open_position"],
                },
                "opportunity_scorecard": {
                    "preferred_side": "long",
                    "long": {"final_state": "watch_for_trigger", "score": 0.44},
                },
            },
            final_action_contract={
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "ZZ",
                "final_action": "wait",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "lots_delta_abs": 0,
                "target_position_ratio": 0.0,
                "authority_type": "watchlist_only",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "max_allowed_margin_ratio": 0.0,
                "reason_codes": ["watch_for_trigger_cannot_open_position"],
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            full_config={},
        )

        snapshot = recommendation.signal_snapshot
        audit = snapshot["active_opportunity_audit"]
        self.assertEqual(recommendation.action, RecommendationAction.HOLD)
        self.assertEqual(recommendation.lots, 0)
        self.assertFalse(audit["decision"]["lands_position"])
        self.assertEqual(audit["decision"]["authority_type"], "watchlist_only")
        self.assertEqual(audit["opportunity"]["watchlist_or_counterfactual_count"], 1)
        self.assertIn("track_watchlist_or_counterfactual_forward_outcome", audit["research_follow_up"])
        self.assertTrue(audit["research_contract"]["no_current_decision_impact"])

    def test_active_opportunity_audit_reads_preferred_side_final_state(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="RB",
            action=FuturesAction.HOLD,
            lots=0,
            price=3500.0,
            settle_price=3500.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="rb2505",
            justification="watchlist",
        )
        recommendation = _build_phase1_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="RB",
            trading_date="2025-03-06",
            contract_code="rb2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=3500.0,
                base_price_source=None,
                base_price_date="2025-03-06",
                open_price=3500.0,
                prev_close_price=3480.0,
                warning_message=None,
            ),
            analyst_signals=[],
            plan_snapshot={
                "reason_codes": "scorecard_watchlist",
                "strategy_controls": {
                    "diagnostics": {},
                    "reasons": ["scorecard_watchlist"],
                },
                "opportunity_scorecard": {
                    "preferred_side": "short",
                    "long": {"final_state": "no_opportunity", "score": 0.10},
                    "short": {"final_state": "tradeable_candidate", "score": 0.64},
                },
            },
            final_action_contract={
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "RB",
                "final_action": "wait",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "lots_delta_abs": 0,
                "target_position_ratio": 0.0,
                "authority_type": "watchlist_only",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "max_allowed_margin_ratio": 0.0,
                "reason_codes": ["scorecard_watchlist"],
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            full_config={},
        )

        audit = recommendation.signal_snapshot["active_opportunity_audit"]
        self.assertEqual(audit["opportunity"]["preferred_side"], "short")
        self.assertEqual(audit["opportunity"]["preferred_state"], "tradeable_candidate")
        self.assertNotEqual(audit["opportunity"]["preferred_state"], "unknown")
        self.assertTrue(audit["opportunity"]["high_quality_present"])

    def test_active_opportunity_audit_lists_clean_conditional_monitor_candidate(self):
        portfolio = SimpleNamespace(id="pf1")
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.62,
            opportunity_state="watch_for_trigger",
            entry_trigger="wait for post-open break below support with volume confirmation",
            invalidation_level=3520.0,
            trigger_valid=False,
            evidence_role="entry_timing",
            metadata={
                "action_evidence_contract": {
                    "opportunity_state": "watch_for_trigger",
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "invalidation_present": True,
                    "entry_trigger": "wait for post-open break below support with volume confirmation",
                }
            },
        )
        decision = FuturesDecision(
            ticker="HC",
            action=FuturesAction.HOLD,
            lots=0,
            price=3500.0,
            settle_price=3500.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="hc2505",
            justification="conditional monitor",
        )

        recommendation = _build_phase1_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="HC",
            trading_date="2025-03-05",
            contract_code="hc2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=3500.0,
                base_price_source=None,
                base_price_date="2025-03-05",
                open_price=3500.0,
                prev_close_price=3510.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "reason_codes": "conditional_watch",
                "strategy_controls": {
                    "diagnostics": {},
                    "reasons": ["conditional_watch"],
                },
                "opportunity_scorecard": {
                    "preferred_side": "short",
                    "short": {
                        "final_state": "watch_for_trigger",
                        "score": 0.52,
                        "setup_quality_ok": True,
                        "trigger_valid": False,
                        "current_trigger_confirmed": False,
                        "invalidation_present": True,
                        "entry_trigger": "wait for post-open break below support with volume confirmation",
                    },
                },
            },
            final_action_contract={
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "HC",
                "final_action": "wait",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "lots_delta_abs": 0,
                "target_position_ratio": 0.0,
                "authority_type": "watchlist_only",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "max_allowed_margin_ratio": 0.0,
                "reason_codes": ["pm_watch_for_trigger_probe_cap"],
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            full_config={},
        )

        audit = recommendation.signal_snapshot["active_opportunity_audit"]
        self.assertEqual(audit["opportunity"]["analyst_candidate_count"], 0)
        self.assertEqual(audit["opportunity"]["conditional_monitor_candidate_count"], 1)
        self.assertTrue(audit["opportunity"]["high_quality_present"])
        candidate = audit["conditional_monitor_candidates"][0]
        self.assertTrue(candidate["conditional_monitor_candidate"])
        self.assertTrue(candidate["setup_quality_ok"])
        self.assertFalse(candidate["trigger_valid"])
        self.assertTrue(candidate["invalidation_present"])

    def test_strong_real_budget_entry_passes_phase1_and_phase2(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="RB",
            action=FuturesAction.OPEN_LONG,
            lots=6,
            price=3500.0,
            settle_price=3500.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="rb2505",
            justification="structured real budget entry",
        )
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.76,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3450.0,
            trigger_valid=True,
            invalidation_present=True,
            evidence_role="entry_timing",
            entry_quality="strong",
        )
        recommendation = _build_phase1_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="RB",
            trading_date="2025-03-06",
            contract_code="rb2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=3500.0,
                base_price_source=None,
                base_price_date="2025-03-06",
                open_price=3500.0,
                prev_close_price=3480.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "target_position_ratio": 0.05,
                "target_lots": 6,
                "lots_delta_abs": 6,
                "reason_codes": "tradable",
                "strategy_controls": {
                    "diagnostics": {},
                    "reasons": ["qualified_positive_expectancy"],
                },
                "opportunity_scorecard": {
                    "preferred_side": "long",
                    "long": {"final_state": "tradeable_candidate", "score": 0.70},
                },
            },
            final_action_contract={
                "contract_version": "agentquant.final_action.v1",
                "ticker": "RB",
                "final_action": "open_real",
                "contract_type": "strategy",
                "current_lots": 0,
                "target_lots": 6,
                "lots_delta": 6,
                "lots_delta_abs": 6,
                "reason_codes": "tradable",
                "target_position_ratio": 0.05,
                "authority_type": "real_budget_entry",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "reason_codes": ["qualified_positive_expectancy"],
                "execution_requirement": "intraday_trigger_required",
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            full_config={},
        )

        self.assertEqual(recommendation.action, RecommendationAction.OPEN_LONG)
        self.assertEqual(recommendation.lots, 6)
        snapshot = dict(recommendation.signal_snapshot)
        decision2 = _translate_pre_open_recommendation_to_order(
            recommendation={
                "underlying_code": "RB",
                "contract_code": "rb2505",
                "source_type": RecommendationSourceType.STRATEGY.value,
                "action": recommendation.action.value,
                "lots": recommendation.lots,
                "signal_snapshot": recommendation.signal_snapshot,
            },
            portfolio=Portfolio(id="p1", cashflow=5000000.0, margin_used=0.0, positions={}),
            config={
                "cashflow": 5000000,
                "max_total_margin_ratio": 0.20,
                "risk_control": {
                    "warning_ratio": 0.70,
                    "danger_ratio": 0.50,
                    "emergency_ratio": 0.30,
                    "max_single_position_ratio": {"safe": 0.12},
                },
            },
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision2.action, FuturesAction.OPEN_LONG)
        self.assertEqual(decision2.lots, 6)
        self.assertTrue(snapshot["phase2_execution"]["pm_plan_validation"]["passed"])

    def test_intraday_execution_audit_has_learning_fields(self):
        selection = select_intraday_execution(
            signal_bars=[],
            execution_bars=[],
            action=RecommendationAction.OPEN_LONG.value,
            config={},
            finalize_untriggered=True,
        )
        audit = selection.to_audit_payload()

        self.assertIn("trigger_checked", audit)
        self.assertIn("trigger_passed", audit)
        self.assertIn("price_chase_check", audit)
        self.assertIn("execution_failure_reason", audit)
        self.assertIn("missed_opportunity_flag", audit)
        self.assertTrue(audit["missed_opportunity_flag"])


class HoldingLifecycleRegressionTest(unittest.TestCase):
    def test_new_loss_revalidation_bypasses_cooling_period_deferral(self):
        self.assertTrue(
            _is_lifecycle_exit_required_reason(["new_position_loss_revalidation_failed"])
        )
        self.assertFalse(_is_lifecycle_exit_required_reason(["cooling_period"]))

    def test_losing_position_without_revalidation_exits_generically(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-5000.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.08,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.20, "confidence": 0.40},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.35},
            full_config={},
            fusion_context={},
            risk_level=RiskLevel.SAFE,
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("position_lifecycle_failed", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertEqual(detail["decision"], "force_exit_failed_position")
        self.assertFalse(detail["loss_revalidated"])

    def test_losing_position_with_partial_revalidation_reduces_not_adds(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-2500.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.12,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.20, "confidence": 0.40},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.48},
            full_config={},
            fusion_context={},
            risk_level=RiskLevel.SAFE,
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("new_position_loss_revalidation_failed", reasons)
        self.assertEqual(
            diagnostics["holding_rebalance_control"]["decision"],
            "exit_failed_new_loss_revalidation",
        )

    def test_losing_position_with_current_evidence_can_hold_same_side(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-2500.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.10,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.BULLISH, confidence=0.75, invalidation_level=100.0),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.70, "confidence": 0.70},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.62},
            full_config={},
            fusion_context={},
            risk_level=RiskLevel.SAFE,
        )

        self.assertAlmostEqual(ratio, 0.10)
        self.assertNotIn("position_lifecycle_loss_revalidation_failed", reasons)
        self.assertTrue(diagnostics["holding_rebalance_control"]["loss_revalidated"])

    def test_new_losing_position_requires_same_day_evidence_and_invalidation(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-800.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.10,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.45),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.65, horizon_class="medium"),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            long_scores={"score": 0.30, "confidence": 0.50},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.50},
            full_config={},
            fusion_context={
                "analyst_quality": {
                    "fundamental": {"effective_confidence": 0.65, "tradeability": "medium"}
                }
            },
            risk_level=RiskLevel.SAFE,
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("new_position_loss_revalidation_failed", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertTrue(detail["new_loss_revalidation_failed"])
        self.assertIn("current_signal_neutral_or_absent", detail["new_loss_revalidation_failures"])
        self.assertIn("missing_invalidation_boundary", detail["new_loss_revalidation_failures"])

    def test_medium_horizon_new_entry_caps_to_probe_when_short_timing_is_missing(self):
        position = None
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.08,
            current_ratio=0.0,
            current_position=position,
            analyst_signals=[
                AnalystSignal(
                    agent_name="fundamental",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    horizon_class="medium",
                    invalidation_level=3200.0,
                ),
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40, horizon_class="short"),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            long_scores={"score": 0.45, "confidence": 0.60},
            short_scores={"score": 0.05, "confidence": 0.20},
            market_confirmation={"confirmation_score": 0.60},
            full_config={},
            fusion_context={
                "analyst_quality": {
                    "fundamental": {"effective_confidence": 0.70, "tradeability": "medium"}
                }
            },
            risk_level=RiskLevel.SAFE,
        )

        self.assertAlmostEqual(ratio, 0.008)
        self.assertIn("horizon_consistency_probe_cap", reasons)
        self.assertEqual(
            diagnostics["holding_rebalance_control"]["raw_target_ratio_after_horizon_gate"],
            ratio,
        )

    def test_losing_position_with_fundamental_anchor_still_needs_current_confirmation(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-2500.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.10,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(
                    agent_name="fundamental",
                    signal=Signal.BULLISH,
                    confidence=0.45,
                    business_quality_score=0.65,
                ),
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.20, "confidence": 0.40},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.42},
            full_config={},
            fusion_context={
                "analyst_quality": {
                    "fundamental": {
                        "effective_confidence": 0.45,
                        "tradeability": "medium",
                    }
                }
            },
            risk_level=RiskLevel.SAFE,
        )

        self.assertAlmostEqual(ratio, 0.0)
        self.assertIn("new_position_loss_revalidation_failed", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertTrue(detail["fundamental_supports_current"])
        self.assertFalse(detail["loss_revalidated"])
        self.assertEqual(detail["decision"], "exit_failed_new_loss_revalidation")

    def test_profitable_supported_hold_defers_plain_exit(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=1200.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-05",
            position_ratio=0.0,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.65,
                    invalidation_level=100.0,
                    opportunity_state="tradeable_candidate",
                    setup_quality_score=0.66,
                    entry_quality="acceptable",
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.55, "confidence": 0.60},
            short_scores={"score": 0.18, "confidence": 0.35},
            market_confirmation={"confirmation_score": 0.48},
            full_config={},
            fusion_context={
                "analyst_quality": {
                    "technical": {"effective_confidence": 0.65, "tradeability": "high"}
                },
                "opportunity_scorecard": {
                    "long": {"final_state": "tradeable_candidate", "gating_failures": []}
                }
            },
            risk_level=RiskLevel.SAFE,
        )

        self.assertAlmostEqual(ratio, 0.10)
        self.assertIn("profitable_hold_continuation", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertTrue(detail["profitable_hold_supported"])
        self.assertEqual(detail["decision"], "keep_profitable_supported_exit_deferred")

    def test_profitable_fundamental_anchor_can_defer_plain_exit(self):
        position = SimpleNamespace(
            shares=-1,
            entry_date="2025-04-02",
            margin_used=25000.0,
            unrealized_pnl=210.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="BU",
            trading_date="2025-04-07",
            position_ratio=0.0,
            current_ratio=-0.007,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(
                    agent_name="fundamental",
                    signal=Signal.BEARISH,
                    confidence=0.38,
                    business_quality_score=0.60,
                    tradeability="medium",
                ),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            long_scores={"score": 0.05, "confidence": 0.20},
            short_scores={"score": 0.30, "confidence": 0.40},
            market_confirmation={"confirmation_score": 0.40},
            full_config={},
            fusion_context={
                "analyst_quality": {
                    "fundamental": {
                        "effective_confidence": 0.38,
                        "tradeability": "medium",
                        "business_quality_score": 0.60,
                    }
                }
            },
            risk_level=RiskLevel.SAFE,
        )

        self.assertAlmostEqual(ratio, -0.007)
        self.assertIn("profitable_hold_continuation", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertTrue(detail["profitable_hold_supported"])
        self.assertEqual(detail["decision"], "keep_profitable_supported_exit_deferred")

    def test_existing_lot_hold_ratio_is_not_truncated_to_flat(self):
        target_lots, preserved = _preserve_existing_lot_when_hold_ratio_survives(
            target_lots=0,
            current_lots=1,
            target_ratio=0.008,
            current_ratio=0.008,
            control_reasons=["profitable_hold_continuation"],
        )

        self.assertTrue(preserved)
        self.assertEqual(target_lots, 1)

    def test_profitable_supported_hold_blocks_weak_reversal_but_not_strong_reversal(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=1500.0,
        )
        common = dict(
            ticker="ZZ",
            trading_date="2025-03-05",
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.65,
                    invalidation_level=100.0,
                    opportunity_state="tradeable_candidate",
                    setup_quality_score=0.66,
                    entry_quality="acceptable",
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            market_confirmation={"confirmation_score": 0.50},
            full_config={},
            fusion_context={
                "analyst_quality": {
                    "technical": {"effective_confidence": 0.65, "tradeability": "high"}
                },
                "opportunity_scorecard": {
                    "long": {"final_state": "tradeable_candidate", "gating_failures": []}
                }
            },
            risk_level=RiskLevel.SAFE,
        )
        weak_ratio, weak_reasons, _notes, weak_diag = _apply_holding_rebalance_control(
            position_ratio=-0.08,
            long_scores={"score": 0.55, "confidence": 0.60},
            short_scores={"score": 0.40, "confidence": 0.45},
            **common,
        )
        self.assertAlmostEqual(weak_ratio, 0.10)
        self.assertIn("profitable_hold_continuation", weak_reasons)
        self.assertEqual(
            weak_diag["holding_rebalance_control"]["decision"],
            "keep_profitable_supported_reversal_blocked",
        )

        strong_ratio, strong_reasons, _notes, strong_diag = _apply_holding_rebalance_control(
            position_ratio=-0.08,
            long_scores={"score": 0.20, "confidence": 0.35},
            short_scores={"score": 0.70, "confidence": 0.70},
            **{
                **common,
            "analyst_signals": [
                    AnalystSignal(agent_name="technical", signal=Signal.BEARISH, confidence=0.75, invalidation_level=110.0),
                    AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                    AnalystSignal(agent_name="commodity_news", signal=Signal.BEARISH, confidence=0.65),
                ],
                "market_confirmation": {"confirmation_score": 0.72},
                "fusion_context": {
                    "analyst_quality": {
                        "technical": {"effective_confidence": 0.75, "tradeability": "high"},
                        "commodity_news": {"effective_confidence": 0.65, "tradeability": "high"},
                    },
                    "opportunity_scorecard": {
                        "long": {"final_state": "watch_for_trigger", "gating_failures": []},
                        "short": {"final_state": "tradeable_candidate", "gating_failures": []},
                    }
                },
            },
        )
        self.assertAlmostEqual(strong_ratio, 0.0)
        self.assertNotIn("profitable_hold_continuation", strong_reasons)
        self.assertIn(
            strong_diag["holding_rebalance_control"]["decision"],
            {"downgrade_reversal_to_exit", "exit_failed_exploration_reconfirm"},
        )


class LearningUsageMetricsRegressionTest(unittest.TestCase):
    def test_learning_context_memory_ratio_counts_unique_prompt_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "eval.db"
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE learning_context_budget (
                        config_id TEXT,
                        trading_date TEXT,
                        analyst TEXT,
                        ticker TEXT,
                        trade_episode_count INTEGER,
                        hypothesis_count INTEGER,
                        total_context_chars INTEGER
                    )
                    """
                )
                cursor.executemany(
                    "INSERT INTO learning_context_budget VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("cfg", "2025-03-01", "technical", "BU", 1, 1, 500),
                        ("cfg", "2025-03-01", "fundamental", "BU", 0, 1, 300),
                        ("cfg", "2025-03-01", "portfolio_manager", "BU", 0, 0, 100),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            metrics = calculate_learning_usage_metrics("cfg", str(db_path), "2025-03-01", "2025-03-01")

        self.assertEqual(metrics["learning_context_budget_rows"], 3)
        self.assertEqual(metrics["learning_context_with_episode_rows"], 1)
        self.assertEqual(metrics["learning_context_with_hypothesis_rows"], 2)
        self.assertAlmostEqual(metrics["learning_context_with_memory_ratio"], 2 / 3)


class DailyTransactionReportRegressionTest(unittest.TestCase):
    def test_phase4_status_override_preserves_complete_report_layout(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE trading_day_phase (
                    config_id TEXT,
                    trading_date TEXT,
                    phase TEXT,
                    status TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    message TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL,
                    commission REAL,
                    holding_pnl REAL,
                    new_position_pnl REAL,
                    close_pnl REAL
                )
                """
            )
            cursor.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT, trading_date TEXT)")
            cursor.executemany(
                "INSERT INTO trading_day_phase VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("cfg", "2025-03-07", "phase1", "completed", "s1", "e1", ""),
                    ("cfg", "2025-03-07", "phase2", "completed", "s2", "e2", "transactions=0"),
                    ("cfg", "2025-03-07", "phase3", "completed", "s3", "e3", "balance=1000000.00"),
                    ("cfg", "2025-03-07", "phase4", "running", "s4", "", ""),
                ],
            )
            text = _build_daily_transaction_report(
                cfg={"tickers": ["BU"]},
                config_id="cfg",
                trading_date="2025-03-07",
                cursor=cursor,
                settlement_row={
                    "current_balance": 1_000_000,
                    "current_margin": 0,
                    "previous_balance": 1_000_000,
                    "previous_margin": 0,
                    "daily_pnl": 0,
                    "commission": 0,
                    "margin_ratio": 0,
                    "cash_available": 1_000_000,
                },
                latest_portfolio={"total_assets": 1_000_000, "leverage": 1, "positions": {}},
                strategy_recommendations=[],
                recommendations=[],
                phase2_transactions=[],
                ticker_pnl={},
                phase4_status_override="completed",
                phase4_completed_at_override="e4",
                phase4_message_override="reviewer validation passed",
            )
        finally:
            conn.close()

        self.assertIn("phase4", text)
        self.assertIn("phase4             completed", text)
        self.assertIn("reviewer validation passed", text)
        self.assertNotIn("phase4             running", text)


class ArtifactValidationRegressionTest(unittest.TestCase):
    def test_validate_artifacts_can_scope_by_trading_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "artifact.db"
            existing = root / "existing.json"
            payload = b'{"ok": true}'
            existing.write_bytes(payload)
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE futures_recommendation (
                        id TEXT,
                        trading_date TEXT,
                        signal_snapshot_artifact_path TEXT,
                        signal_snapshot_sha256 TEXT,
                        signal_snapshot_size INTEGER
                    )
                    """
                )
                cursor.executemany(
                    "INSERT INTO futures_recommendation VALUES (?, ?, ?, ?, ?)",
                    [
                        ("old", "2025-02-24", str(root / "missing-old.json"), "sha", 1),
                        ("new", "2025-03-03", str(existing), hashlib.sha256(payload).hexdigest(), len(payload)),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            full_report = validate_artifacts(db_path)
            scoped_report = validate_artifacts(db_path, start_date="2025-03-03", end_date="2025-03-03")

        self.assertFalse(full_report["ok"])
        self.assertEqual(len(full_report["missing"]), 1)
        self.assertTrue(scoped_report["ok"])
        self.assertEqual(scoped_report["checked"], 1)
        self.assertEqual(scoped_report["missing"], [])


class _DrawdownScenarioDB:
    def __init__(self, drawdown: float, *, hard_streak: int = 0, recovery_rows=None):
        self.drawdown = drawdown
        self.hard_streak = hard_streak
        self.recovery_rows = recovery_rows or []

    def get_account_drawdown_state(self, **kwargs):
        return {
            "latest_date": "2025-03-03",
            "latest_equity": 1.0 - self.drawdown,
            "peak_equity": 1.0,
            "drawdown": self.drawdown,
        }

    def get_ticker_loss_state(self, **kwargs):
        return {
            "lookback_days": kwargs.get("lookback_days", 5),
            "trade_days": 0,
            "cumulative_pnl": 0.0,
            "consecutive_loss_days": 0,
        }

    def _get_connection(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE portfolio (
                id TEXT,
                config_id TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE daily_settlement (
                portfolio_id TEXT,
                trading_date TEXT,
                current_balance REAL,
                current_margin REAL,
                created_at TEXT
            )
            """
        )
        for idx in range(self.hard_streak):
            day = f"2025-03-0{idx + 1}"
            cur.execute("INSERT INTO portfolio VALUES (?, ?)", (f"p{idx}", "cfg"))
            cur.execute(
                "INSERT INTO daily_settlement VALUES (?, ?, ?, ?, ?)",
                (f"p{idx}", day, 940000.0, 0.0, "now"),
            )
        cur.execute(
            """
            CREATE TABLE ticker_daily_pnl (
                portfolio_id TEXT,
                trading_date TEXT,
                ticker TEXT,
                daily_pnl REAL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE futures_transactions (
                config_id TEXT,
                trading_date TEXT,
                ticker TEXT,
                action TEXT,
                recommendation_id TEXT,
                created_at TEXT,
                id TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT,
                signal_snapshot TEXT,
                signal_snapshot_artifact_path TEXT,
                signal_snapshot_sha256 TEXT
            )
            """
        )
        for idx, row in enumerate(self.recovery_rows):
            portfolio_id = f"rp{idx}"
            rec_id = f"rr{idx}"
            day = row["trading_date"]
            cur.execute("INSERT INTO portfolio VALUES (?, ?)", (portfolio_id, "cfg"))
            cur.execute(
                "INSERT INTO ticker_daily_pnl VALUES (?, ?, ?, ?)",
                (portfolio_id, day, row.get("ticker", "ZZ"), row.get("daily_pnl", 0.0)),
            )
            cur.execute(
                "INSERT INTO futures_transactions VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("cfg", day, row.get("ticker", "ZZ"), "open_long", rec_id, f"{day}T09:00:00", f"tx{idx}"),
            )
            snapshot = {
                "pm_internal_draft": {
                    "strategy_controls": {
                        "diagnostics": {
                            "drawdown_control": {
                                "mode": "hard_recovery_probe",
                            }
                        }
                    }
                }
            }
            cur.execute(
                "INSERT INTO futures_recommendation VALUES (?, ?, ?, ?)",
                (rec_id, json.dumps(snapshot), None, None),
            )
        conn.commit()
        return conn


class DrawdownProtectionRegressionTest(unittest.TestCase):
    def _base_config(self):
        return {
            "cashflow": 1000000.0,
            "drawdown_control": {
                "enabled": True,
                "warning_drawdown": 0.04,
                "hard_drawdown": 0.05,
                "warning_cap_multiplier": 0.60,
                "warning_target_margin_ratio_max": 0.04,
                "initial_hard_cooldown_days": 1,
                "recovery_probe_min_confirmation_score": 0.65,
                "recovery_probe_min_confirmations": 3,
                "recovery_probe_require_stop_protection": True,
                "recovery_probe_margin_ratio_max": 0.015,
                "recovery_restore_step_margin_ratios": [0.01, 0.02],
            },
            "ticker_loss_control": {"enabled": False},
        }

    def test_warning_drawdown_scales_new_exposure(self):
        ratio, reasons, notes, diagnostics = _apply_drawdown_and_ticker_loss_control(
            db=_DrawdownScenarioDB(0.045),
            config_id="cfg",
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.80,
            current_ratio=0.0,
            current_margin_ratio=0.0,
            margin_rate=0.10,
            market_confirmation={"confirmation_score": 0.70},
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={},
            analyst_signals=[],
            full_config=self._base_config(),
        )

        self.assertAlmostEqual(ratio, 0.40)
        self.assertIn("drawdown_control", reasons)
        self.assertEqual(diagnostics["drawdown_control"]["state"], "warning")

    def test_hard_drawdown_initial_cooldown_blocks_new_exposure(self):
        ratio, reasons, notes, diagnostics = _apply_drawdown_and_ticker_loss_control(
            db=_DrawdownScenarioDB(0.052, hard_streak=0),
            config_id="cfg",
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.20,
            current_ratio=0.0,
            current_margin_ratio=0.0,
            margin_rate=0.10,
            market_confirmation={"confirmation_score": 0.90, "confirmations": ["a", "b", "c"]},
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={},
            analyst_signals=[AnalystSignal(agent_name="technical", signal=Signal.BULLISH, invalidation_level=100.0)],
            full_config=self._base_config(),
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("drawdown_control", reasons)
        self.assertEqual(diagnostics["drawdown_control"]["mode"], "hard_initial_cooldown")
        self.assertTrue(diagnostics["drawdown_control"]["counterfactual_recommendation"])

    def test_hard_drawdown_recovery_probe_allows_only_small_validated_risk(self):
        ratio, reasons, notes, diagnostics = _apply_drawdown_and_ticker_loss_control(
            db=_DrawdownScenarioDB(0.052, hard_streak=2),
            config_id="cfg",
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.50,
            current_ratio=0.0,
            current_margin_ratio=0.0,
            margin_rate=0.10,
            market_confirmation={"confirmation_score": 0.90, "confirmations": ["a", "b", "c"]},
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={},
            analyst_signals=[AnalystSignal(agent_name="technical", signal=Signal.BULLISH, invalidation_level=100.0)],
            full_config=self._base_config(),
        )

        self.assertAlmostEqual(ratio, 0.10)
        self.assertIn("drawdown_recovery_probe", reasons)
        self.assertEqual(diagnostics["drawdown_control"]["mode"], "hard_recovery_probe")
        self.assertFalse(diagnostics["drawdown_control"]["counterfactual_recommendation"])


class IntradayExecutionRegressionTest(unittest.TestCase):
    def test_pm_execution_contract_classifies_technical_pullback(self):
        action_contract = {
            "contract_version": "agentquant.action_evidence.v1",
            "analyst": "technical",
            "learning_scope": {
                "setup_family": "trend_pullback",
                "sector_setup_alignment": "preferred",
            },
            "execution": {
                "trigger_source": "technical",
                "execution_focus": "wait_for_pullback_confirmation",
            },
        }
        plan = _build_pm_decision_context(
            target_lots=2,
            current_price=100.0,
            position_ratio=0.02,
            risk_level=RiskLevel.SAFE,
            long_scores={"confidence": 0.70},
            short_scores={"confidence": 0.20},
            margin_rate=0.10,
            current_lots=0,
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    entry_trigger="Pullback to VWAP support then stabilize",
                    invalidation_level=96.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    metadata={"action_evidence_contract": action_contract},
                ),
            ],
            final_entry_authority={
                "authority_type": "exploration_probe",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "max_allowed_margin_ratio": 0.015,
            },
            trading_date="2025-01-06",
            recommendation_intent={"action": "open_long"},
            control_reasons=["controlled_probe"],
        )

        execution_contract = plan
        self.assertEqual(execution_contract["execution_profile"], "pullback")
        self.assertEqual(execution_contract["trigger_source"], "technical_pullback")
        self.assertTrue(execution_contract["requires_intraday_confirmation"])
        self.assertFalse(execution_contract["can_execute_without_intraday_trigger"])
        technical_role = execution_contract["analyst_execution_roles"]["technical"]
        self.assertEqual(technical_role["learning_scope"]["setup_family"], "trend_pullback")
        learning = _setup_execution_learning_context({
            "final_action_contract": {
                **execution_contract,
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "ZZ",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": 2,
                "lots_delta": 2,
            },
        })
        self.assertIn("technical", learning["analyst_action_evidence_contracts"])
        self.assertEqual(learning["analyst_learning_scopes"]["technical"]["setup_family"], "trend_pullback")

    def test_pm_does_not_treat_fundamental_pending_trigger_as_current_trigger(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.80,
            entry_trigger="long entry only after short-term price confirmation aligns with factors",
            invalidation_level=96.0,
            metadata={
                "action_evidence_contract": {
                    "open": {
                        "role": "context_or_short_trigger",
                        "can_create_trade_authority_alone": False,
                        "requires_technical_or_market_confirmation": True,
                    }
                }
            },
        )
        plan = _build_pm_decision_context(
            target_lots=2,
            current_price=100.0,
            position_ratio=0.02,
            risk_level=RiskLevel.SAFE,
            long_scores={"confidence": 0.70},
            short_scores={"confidence": 0.20},
            margin_rate=0.10,
            current_lots=0,
            analyst_signals=[signal],
            final_entry_authority={
                "authority_type": "exploration_probe",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "max_allowed_margin_ratio": 0.015,
            },
            trading_date="2025-01-06",
            recommendation_intent={"action": "open_long"},
            control_reasons=["controlled_probe"],
        )

        role = plan["analyst_execution_roles"]["fundamental"]
        self.assertFalse(role["trigger_valid"])
        self.assertEqual(role["entry_timing_signal"], "requires_technical_or_market_timing")

    def test_pm_execution_contract_classifies_authorized_event_immediate(self):
        plan = _build_pm_decision_context(
            target_lots=3,
            current_price=100.0,
            position_ratio=0.04,
            risk_level=RiskLevel.SAFE,
            long_scores={"confidence": 0.80},
            short_scores={"confidence": 0.20},
            margin_rate=0.10,
            current_lots=0,
            analyst_signals=[
                AnalystSignal(
                    agent_name="commodity_news",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    entry_trigger="news_event_trigger",
                    event_type="supply_disruption",
                    trigger_valid=True,
                    invalidation_present=True,
                    exit_hint="Catalyst expires or price fails to hold",
                    metadata={
                        "action_evidence_contract": {
                            "opportunity_state": "tradeable_candidate",
                            "opportunity_state": "tradeable_candidate",
                            "trigger_valid": True,
                            "invalidation_present": True,
                            "entry_trigger": "Fresh supply disruption catalyst",
                        }
                    },
                ),
            ],
            final_entry_authority={
                "authority_type": "real_budget_entry",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "max_allowed_margin_ratio": 0.06,
            },
            trading_date="2025-01-06",
            recommendation_intent={"action": "open_long"},
            control_reasons=["qualified_positive_expectancy"],
        )

        execution_contract = plan
        self.assertEqual(execution_contract["execution_profile"], "event_immediate")
        self.assertEqual(execution_contract["trigger_source"], "commodity_news_event")
        self.assertTrue(execution_contract["can_execute_without_intraday_trigger"])

    def test_execution_action_value_changes_authorized_entry_to_pullback_confirmation(self):
        plan = _build_pm_decision_context(
            ticker="BU",
            target_lots=-3,
            current_price=100.0,
            position_ratio=-0.02,
            risk_level=RiskLevel.SAFE,
            long_scores={"confidence": 0.20},
            short_scores={"confidence": 0.72},
            margin_rate=0.10,
            current_lots=0,
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BEARISH,
                    confidence=0.72,
                    entry_trigger="Opening range breakdown",
                    invalidation_level=104.0,
                    trigger_valid=True,
                    invalidation_present=True,
                ),
            ],
            final_entry_authority={
                "authority_type": "exploration_probe",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "max_allowed_margin_ratio": 0.015,
            },
            trading_date="2025-03-24",
            recommendation_intent={"action": "open_short"},
            control_reasons=["controlled_probe"],
            alpha_setup_action_values=[
                {
                    "ticker": "BU",
                    "side": "short",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "action_name": "execution",
                    "setup_type": "execution_breakout_setup",
                    "data_combo": "technical:trend_breakout|execution:breakout",
                    "sample_count": 3,
                    "reward_mean": -1600.0,
                    "reward_sum": -4800.0,
                    "win_rate": 0.0,
                    "confidence_score": 0.62,
                    "action_preference": "cap_reduce_or_revalidate",
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 3,
                        "exact_state_real_trade_sample_count": 3,
                        "amplification_scope_quality": "exact_real_state",
                    },
                }
            ],
        )

        execution_contract = plan
        self.assertEqual(execution_contract["execution_profile"], "pullback")
        self.assertEqual(execution_contract["trigger_source"], "execution_action_value_pullback")
        self.assertIn("execution_action_value_preference", execution_contract["reason_codes"])
        self.assertFalse(execution_contract["can_execute_without_intraday_trigger"])

    def test_vwap_confirmed_profile_requires_vwap_direction_and_chase_check(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 96, "close": 98.0, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 98.1, "high": 99, "low": 97, "close": 98.2, "volume": 10},
        ]

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_short",
            config={"opening_range_minutes": 2, "min_execution_volume": 1, "max_chase_ratio": 0.02},
            decision_context={
                "execution_contract": {
                    "execution_profile": "vwap_confirmed",
                    "entry_trigger": "wait for VWAP directional confirmation",
                    "can_execute_without_intraday_trigger": False,
                }
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.reason, "intraday_vwap_confirmed")
        self.assertEqual(result.features["execution_profile"], "vwap_confirmed")
        self.assertEqual(result.features["trigger_rule"], "vwap_direction_confirmation")
        self.assertTrue(result.to_audit_payload()["trigger_passed"])

    def test_long_trigger_uses_completed_signal_bar_and_next_execution_open(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 104, "low": 99, "close": 103, "volume": 10},
            {"datetime": "2025-01-06 10:15:00", "open": 103, "high": 106, "low": 102, "close": 105, "volume": 12},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 10},
            {"datetime": "2025-01-06 10:16:00", "open": 105, "high": 106, "low": 104, "close": 105, "volume": 10},
        ]
        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={
                "opening_range_minutes": 30,
                "min_execution_volume": 1,
                "max_chase_ratio": 0.02,
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.base_price, 105.0)
        self.assertEqual(result.base_datetime, "2025-01-06 10:16:00")
        self.assertEqual(result.reason, "intraday_trigger_confirmed")

    def test_pullback_profile_uses_vwap_support_without_opening_range_breakout(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 99, "close": 100.6, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101.5, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 101.5, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 100.7, "high": 101, "low": 100, "close": 100.7, "volume": 10},
        ]

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 2, "min_execution_volume": 1, "max_chase_ratio": 0.02},
            decision_context={
                "execution_contract": {
                    "execution_profile": "pullback",
                    "entry_trigger": "pullback to vwap support",
                    "can_execute_without_intraday_trigger": False,
                }
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.reason, "intraday_pullback_confirmed")
        self.assertEqual(result.features["execution_profile"], "pullback")
        self.assertEqual(result.features["trigger_rule"], "vwap_pullback_support")

    def test_event_immediate_requires_pm_authority_to_bypass_intraday_trigger(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 100, "low": 99, "close": 99, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 99, "high": 100, "low": 98, "close": 99, "volume": 10},
        ]

        blocked = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={"execution_contract": {"execution_profile": "event_immediate"}},
            finalize_untriggered=True,
        )
        allowed = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={
                "execution_contract": {
                    "execution_profile": "event_immediate",
                    "can_execute_without_intraday_trigger": True,
                }
            },
            finalize_untriggered=True,
        )

        self.assertFalse(blocked.should_execute)
        self.assertEqual(blocked.reason, "intraday_trigger_not_met")
        self.assertTrue(allowed.should_execute)
        self.assertEqual(allowed.reason, "intraday_event_immediate_execution")
        self.assertTrue(allowed.to_audit_payload()["trigger_passed"])

    def test_untriggered_signal_is_waiting_until_finalized(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 99, "close": 99, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
        ]
        waiting = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 30, "min_execution_volume": 1},
            finalize_untriggered=False,
        )
        finalized = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 30, "min_execution_volume": 1},
            finalize_untriggered=True,
        )

        self.assertEqual(waiting.decision, "wait")
        self.assertEqual(waiting.reason, "intraday_waiting_for_trigger")
        self.assertEqual(finalized.decision, "skip")
        self.assertEqual(finalized.reason, "intraday_trigger_not_met")

    def test_opening_range_must_complete_before_trigger(self):
        signal_bars = [
            {"datetime": "2025-01-06 09:15:00", "open": 118, "high": 121, "low": 117, "close": 120, "volume": 10},
        ]
        execution_bars = [
            {"datetime": f"2025-01-06 09:{minute:02d}:00", "open": 100, "high": 119, "low": 99, "close": 100, "volume": 10}
            for minute in range(1, 31)
        ]
        execution_bars.append(
            {"datetime": "2025-01-06 09:31:00", "open": 120, "high": 121, "low": 119, "close": 120, "volume": 10}
        )

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 30, "min_execution_volume": 1, "max_chase_ratio": 0.02},
            finalize_untriggered=True,
        )

        self.assertFalse(result.should_execute)
        self.assertEqual(result.reason, "intraday_trigger_not_met")
        self.assertEqual(result.features["eligible_signal_bars"], 0)
        self.assertTrue(result.features["opening_range"]["complete"])

    def test_incomplete_opening_range_waits_in_paper_loop(self):
        signal_bars = [
            {"datetime": "2025-01-06 09:15:00", "open": 100, "high": 104, "low": 99, "close": 103, "volume": 10},
        ]
        execution_bars = [
            {"datetime": f"2025-01-06 09:{minute:02d}:00", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 10}
            for minute in range(1, 21)
        ]

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 30, "min_execution_volume": 1},
            finalize_untriggered=False,
        )

        self.assertEqual(result.decision, "wait")
        self.assertEqual(result.reason, "intraday_opening_range_incomplete")
        self.assertFalse(result.features["opening_range"]["complete"])

    def test_cutoff_datetime_prevents_future_minute_bars_from_triggering(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 100, "low": 99, "close": 99, "volume": 10},
            {"datetime": "2025-01-06 14:45:00", "open": 101, "high": 105, "low": 101, "close": 105, "volume": 20},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 99, "high": 100, "low": 98, "close": 99, "volume": 10},
            {"datetime": "2025-01-06 14:46:00", "open": 105, "high": 106, "low": 104, "close": 105, "volume": 20},
        ]

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 2, "min_execution_volume": 1, "max_chase_ratio": 0.02},
            cutoff_datetime=datetime(2025, 1, 6, 10, 5, 0),
            finalize_untriggered=False,
        )

        self.assertFalse(result.should_execute)
        self.assertEqual(result.decision, "wait")
        self.assertEqual(result.reason, "intraday_waiting_for_trigger")
        self.assertEqual(result.features["signal_bars"], 1)
        self.assertEqual(result.features["execution_bars"], 3)

    def test_intraday_execution_rejects_research_memory_parameters(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 99, "close": 100.85, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 100.9, "high": 101, "low": 100, "close": 100.9, "volume": 10},
        ]

        with self.assertRaises(TypeError):
            select_intraday_execution(
                signal_bars=signal_bars,
                execution_bars=execution_bars,
                action="open_long",
                config={"opening_range_minutes": 2, "min_execution_volume": 1},
                strategy_memory={"side_memory": {"memory_state": "protected"}},
            )
        with self.assertRaises(TypeError):
            select_intraday_execution(
                signal_bars=signal_bars,
                execution_bars=execution_bars,
                action="open_long",
                config={"opening_range_minutes": 2, "min_execution_volume": 1},
                adaptive_policy_state=[{"policy_type": "contextual_rule_calibration:intraday_confirmation"}],
            )
        with self.assertRaises(TypeError):
            select_intraday_execution(
                signal_bars=signal_bars,
                execution_bars=execution_bars,
                action="open_long",
                config={"opening_range_minutes": 2, "min_execution_volume": 1},
                market_confirmation={"confirmation_score": 0.75},
            )

    def test_old_memory_fallback_config_cannot_create_execution_trigger(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 99, "close": 100.85, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 100.9, "high": 101, "low": 100, "close": 100.9, "volume": 10},
        ]

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={
                "opening_range_minutes": 2,
                "min_execution_volume": 1,
                "max_chase_ratio": 0.02,
                "allow_confirmed_memory_vwap_fallback": True,
                "confirmed_memory_min_market_confirmation_score": 0.70,
                "confirmed_memory_min_confirmations": 3,
                "confirmed_memory_max_opening_range_miss": 0.002,
                "confirmed_memory_min_sample_count": 5,
            },
            decision_context={
                "execution_contract": {
                    "execution_profile": "breakout",
                    "allow_confirmed_memory_vwap_fallback": False,
                }
            },
        )

        self.assertFalse(result.should_execute)
        self.assertEqual(result.reason, "intraday_trigger_not_met")
        self.assertNotEqual(result.reason, "intraday_confirmed_memory_vwap_fallback")
        self.assertNotEqual(result.features.get("execution_mode"), "confirmed_memory_vwap_fallback")

    def test_pm_must_encode_vwap_execution_profile_in_contract(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 99, "close": 100.85, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 100.9, "high": 101, "low": 100, "close": 100.9, "volume": 10},
        ]

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 2, "min_execution_volume": 1, "max_chase_ratio": 0.02},
            decision_context={
                "execution_contract": {
                    "execution_profile": "vwap_confirmed",
                    "entry_trigger": "wait for VWAP directional confirmation",
                    "can_execute_without_intraday_trigger": False,
                }
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.reason, "intraday_vwap_confirmed")
        self.assertEqual(result.features["execution_profile"], "vwap_confirmed")
        self.assertEqual(result.features["trigger_rule"], "vwap_direction_confirmation")


class MarginAuditRegressionTest(unittest.TestCase):
    def test_close_trade_records_released_margin_without_negative_balance(self):
        audit = calculate_margin_audit(
            action="close_long",
            lots=4,
            current_shares=8,
            current_margin_used=48276.0,
        )

        self.assertAlmostEqual(audit["released_margin"], 24138.0)
        self.assertAlmostEqual(audit["post_trade_margin_used"], 24138.0)
        self.assertAlmostEqual(audit["margin_delta"], -24138.0)
        self.assertGreaterEqual(audit["post_trade_margin_used"], 0.0)


class FuturesTradePairRegressionTest(unittest.TestCase):
    def test_trade_pair_builder_handles_partial_closes(self):
        transactions = [
            {
                "id": "o1",
                "recommendation_id": "r1",
                "trading_date": "2025-10-13",
                "created_at": "2025-10-13T09:00:00",
                "ticker": "M",
                "contract_code": "m2601",
                "action": "open_long",
                "lots": 2,
                "execution_price": 100.0,
                "contract_multiplier": 10.0,
                "commission": 2.0,
            },
            {
                "id": "c1",
                "recommendation_id": "r2",
                "trading_date": "2025-10-14",
                "created_at": "2025-10-14T09:00:00",
                "ticker": "M",
                "contract_code": "m2601",
                "action": "close_long",
                "lots": 1,
                "execution_price": 110.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
            },
            {
                "id": "c2",
                "recommendation_id": "r3",
                "trading_date": "2025-10-15",
                "created_at": "2025-10-15T09:00:00",
                "ticker": "M",
                "contract_code": "m2601",
                "action": "close_long",
                "lots": 1,
                "execution_price": 90.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
            },
        ]

        pairs = build_completed_trade_pairs(transactions)
        summary = summarize_trade_pairs(pairs)

        self.assertEqual(len(pairs), 2)
        self.assertAlmostEqual(pairs[0]["net_pnl"], 98.0)
        self.assertAlmostEqual(pairs[1]["net_pnl"], -102.0)
        self.assertEqual(summary["winning_trades"], 1)
        self.assertEqual(summary["losing_trades"], 1)

    def test_trade_pair_builder_can_exclude_rollover_transactions(self):
        transactions = [
            {
                "id": "o1",
                "recommendation_id": "r1",
                "trading_date": "2025-10-13",
                "created_at": "2025-10-13T09:00:00",
                "ticker": "C",
                "contract_code": "c2601",
                "action": "open_short",
                "lots": 1,
                "execution_price": 2100.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
                "source_type": "strategy",
            },
            {
                "id": "c1",
                "recommendation_id": "r2",
                "trading_date": "2025-10-15",
                "created_at": "2025-10-15T09:00:00",
                "ticker": "C",
                "contract_code": "c2601",
                "action": "close_short",
                "lots": 1,
                "execution_price": 2090.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
                "source_type": "rollover",
            },
        ]

        all_pairs = build_completed_trade_pairs(transactions)
        strategy_only_pairs = build_completed_trade_pairs(transactions, include_rollover=False)

        self.assertEqual(len(all_pairs), 1)
        self.assertTrue(all_pairs[0]["contains_rollover"])
        self.assertEqual(strategy_only_pairs, [])

    def test_trade_pair_builder_excludes_forced_risk_from_strategy_only_view(self):
        transactions = [
            {
                "id": "o1",
                "recommendation_id": "r1",
                "trading_date": "2025-10-13",
                "created_at": "2025-10-13T09:00:00",
                "ticker": "C",
                "contract_code": "c2601",
                "action": "open_short",
                "lots": 1,
                "execution_price": 2100.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
                "source_type": "strategy",
            },
            {
                "id": "c1",
                "recommendation_id": "risk1",
                "trading_date": "2025-10-15",
                "created_at": "2025-10-15T09:00:00",
                "ticker": "C",
                "contract_code": "c2601",
                "action": "close_short",
                "lots": 1,
                "execution_price": 2090.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
                "source_type": "forced_risk",
            },
        ]

        all_pairs = build_completed_trade_pairs(transactions)
        strategy_only_pairs = build_completed_trade_pairs(transactions, include_rollover=False)

        self.assertEqual(len(all_pairs), 1)
        self.assertTrue(all_pairs[0]["contains_forced_risk"])
        self.assertTrue(all_pairs[0]["contains_non_strategy"])
        self.assertEqual(strategy_only_pairs, [])


class ConditionalPerformanceRegressionTest(unittest.TestCase):
    def test_conditional_trade_performance_filters_signal_combo_and_future_rows(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE futures_transactions (
                    id TEXT,
                    config_id TEXT,
                    recommendation_id TEXT,
                    trading_date TEXT,
                    created_at TEXT,
                    ticker TEXT,
                    contract_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    execution_price REAL,
                    price REAL,
                    contract_multiplier REAL,
                    commission REAL,
                    source_type TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT,
                    signal_snapshot TEXT
                )
                """
            )
            cur.executemany(
                "INSERT INTO futures_recommendation VALUES (?, ?)",
                [
                    (
                        "r1",
                        json.dumps(
                            {
                                "technical": {"signal": "Bullish"},
                                "fundamental": {"signal": "Bullish"},
                                "commodity_news": {"signal": "Neutral"},
                            }
                        ),
                    ),
                    (
                        "r2",
                        json.dumps(
                            {
                                "technical": {"signal": "Bearish"},
                                "fundamental": {"signal": "Neutral"},
                                "commodity_news": {"signal": "Bearish"},
                            }
                        ),
                    ),
                    (
                        "r3",
                        json.dumps(
                            {
                                "technical": {"signal": "Bullish"},
                                "fundamental": {"signal": "Bullish"},
                                "commodity_news": {"signal": "Neutral"},
                            }
                        ),
                    ),
                ],
            )
            rows = [
                ("o1", "cfg", "r1", "2025-10-13", "2025-10-13T09:00:00", "M", "m2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("c1", "cfg", "rc1", "2025-10-14", "2025-10-14T09:00:00", "M", "m2601", "close_long", 1, 110.0, 110.0, 10.0, 1.0, "strategy"),
                ("o2", "cfg", "r2", "2025-10-15", "2025-10-15T09:00:00", "M", "m2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("c2", "cfg", "rc2", "2025-10-16", "2025-10-16T09:00:00", "M", "m2601", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                ("o3", "cfg", "r3", "2025-10-20", "2025-10-20T09:00:00", "M", "m2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("c3", "cfg", "rc3", "2025-10-21", "2025-10-21T09:00:00", "M", "m2601", "close_long", 1, 120.0, 120.0, 10.0, 1.0, "strategy"),
            ]
            cur.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            conn.close()

            db = SQLiteDB()
            db.db_path = db_path
            summary = db.get_futures_conditional_trade_performance(
                config_id="cfg",
                ticker="M",
                side="long",
                trading_date="2025-10-17",
                signal_combo=["Bullish", "Bullish", "Neutral"],
                lookback_trades=30,
            )

            self.assertEqual(summary["total_trades"], 1)
            self.assertEqual(summary["winning_trades"], 1)
            self.assertAlmostEqual(summary["total_pnl"], 98.0)
            self.assertEqual(summary["cutoff_trading_date"], "2025-10-17")
        finally:
            os.remove(db_path)

    def test_futures_transaction_memory_uses_only_past_trades(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE futures_transactions (
                    id TEXT,
                    config_id TEXT,
                    recommendation_id TEXT,
                    trading_date TEXT,
                    created_at TEXT,
                    ticker TEXT,
                    contract_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    execution_price REAL,
                    price REAL,
                    contract_multiplier REAL,
                    commission REAL,
                    source_type TEXT
                )
                """
            )
            cur.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("past", "cfg", "r1", "2025-03-03", "2025-03-03T09:00:00", "BU", "bu2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                    ("same", "cfg", "r2", "2025-03-04", "2025-03-04T09:00:00", "BU", "bu2601", "close_long", 1, 120.0, 120.0, 10.0, 1.0, "strategy"),
                    ("future", "cfg", "r3", "2025-03-05", "2025-03-05T09:00:00", "BU", "bu2601", "open_short", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                ],
            )
            conn.commit()
            conn.close()

            db = SQLiteDB()
            db.db_path = db_path
            memory = db.get_futures_transaction_memory("cfg", "BU", limit=10, trading_date="2025-03-04")

            self.assertEqual(memory, ["2025-03-03 open_long 1@100.0"])
        finally:
            os.remove(db_path)

    def test_strategy_memory_refreshes_from_completed_trade_pairs(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE futures_transactions (
                    id TEXT,
                    config_id TEXT,
                    recommendation_id TEXT,
                    trading_date TEXT,
                    created_at TEXT,
                    ticker TEXT,
                    contract_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    execution_price REAL,
                    price REAL,
                    contract_multiplier REAL,
                    commission REAL,
                    source_type TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT,
                    signal_snapshot TEXT
                )
                """
            )
            cur.executemany(
                "INSERT INTO futures_recommendation VALUES (?, ?)",
                [
                    (
                        "bu-r1",
                        json.dumps(
                            {
                                "technical": {"signal": "Bullish"},
                                "fundamental": {"signal": "Bullish"},
                                "commodity_news": {"signal": "Neutral"},
                            }
                        ),
                    ),
                    (
                        "bu-r2",
                        json.dumps(
                            {
                                "technical": {"signal": "Bullish"},
                                "fundamental": {"signal": "Bullish"},
                                "commodity_news": {"signal": "Neutral"},
                            }
                        ),
                    ),
                    (
                        "bu-r3",
                        json.dumps(
                            {
                                "technical": {"signal": "Bullish"},
                                "fundamental": {"signal": "Bullish"},
                                "commodity_news": {"signal": "Neutral"},
                            }
                        ),
                    ),
                    (
                        "p-r1",
                        json.dumps(
                            {
                                "technical": {"signal": "Bearish"},
                                "fundamental": {"signal": "Neutral"},
                                "commodity_news": {"signal": "Bullish"},
                            }
                        ),
                    ),
                    (
                        "p-r2",
                        json.dumps(
                            {
                                "technical": {"signal": "Bearish"},
                                "fundamental": {"signal": "Neutral"},
                                "commodity_news": {"signal": "Bullish"},
                            }
                        ),
                    ),
                ],
            )
            rows = [
                ("bu-o1", "cfg", "bu-r1", "2025-10-13", "2025-10-13T09:00:00", "BU", "bu2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("bu-c1", "cfg", "bu-c1-rec", "2025-10-14", "2025-10-14T14:50:00", "BU", "bu2601", "close_long", 1, 160.0, 160.0, 10.0, 1.0, "strategy"),
                ("bu-o2", "cfg", "bu-r2", "2025-10-15", "2025-10-15T09:00:00", "BU", "bu2601", "open_long", 1, 110.0, 110.0, 10.0, 1.0, "strategy"),
                ("bu-c2", "cfg", "bu-c2-rec", "2025-10-16", "2025-10-16T14:50:00", "BU", "bu2601", "close_long", 1, 170.0, 170.0, 10.0, 1.0, "strategy"),
                ("bu-o3", "cfg", "bu-r3", "2025-10-17", "2025-10-17T09:00:00", "BU", "bu2601", "open_long", 1, 120.0, 120.0, 10.0, 1.0, "strategy"),
                ("bu-c3", "cfg", "bu-c3-rec", "2025-10-20", "2025-10-20T14:50:00", "BU", "bu2601", "close_long", 1, 180.0, 180.0, 10.0, 1.0, "strategy"),
                ("p-o1", "cfg", "p-r1", "2025-10-13", "2025-10-13T09:01:00", "P", "p2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("p-c1", "cfg", "p-c1-rec", "2025-10-14", "2025-10-14T14:51:00", "P", "p2601", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                ("p-o2", "cfg", "p-r2", "2025-10-15", "2025-10-15T09:01:00", "P", "p2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("p-c2", "cfg", "p-c2-rec", "2025-10-16", "2025-10-16T14:51:00", "P", "p2601", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
            ]
            cur.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            conn.close()

            db = SQLiteDB()
            db.db_path = db_path
            refreshed = db.refresh_strategy_memory("cfg", "2025-10-20")
            bu_memory = db.get_strategy_memory(
                "cfg",
                "BU",
                "long",
                "2025-10-21",
                ["Bullish", "Bullish", "Neutral"],
            )
            p_memory = db.get_strategy_memory(
                "cfg",
                "P",
                "long",
                "2025-10-21",
                ["Bearish", "Neutral", "Bullish"],
            )

            self.assertGreaterEqual(refreshed, 4)
            self.assertEqual(bu_memory["side_memory"]["memory_state"], "protected")
            self.assertEqual(bu_memory["combo"]["memory_state"], "protected")
            self.assertEqual(p_memory["side_memory"]["memory_state"], "watchlist")
            self.assertEqual(p_memory["combo"]["memory_state"], "watchlist")
            self.assertEqual(bu_memory["combo"]["payload"]["cutoff_trading_date"], "2025-10-20")

            db.refresh_strategy_memory(
                "cfg",
                "2025-10-20",
                memory_config={"enabled": True, "min_samples_protected": 4},
            )
            stricter_bu_memory = db.get_strategy_memory(
                "cfg",
                "BU",
                "long",
                "2025-10-21",
                ["Bullish", "Bullish", "Neutral"],
            )
            self.assertEqual(stricter_bu_memory["side_memory"]["memory_state"], "recovering")
        finally:
            os.remove(db_path)

    def test_strategy_memory_uses_only_past_source_dates(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = SQLiteDB()
            db.db_path = db_path
            conn = db._get_connection()
            cur = conn.cursor()
            db._ensure_strategy_memory_schema(cur)
            cur.executemany(
                """
                INSERT INTO strategy_memory (
                    id, config_id, ticker, side, signal_combo, memory_state,
                    sample_count, win_rate, net_pnl, avg_pnl, confidence_score,
                    source, reason, source_trading_date, updated_at, valid_until, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "past",
                        "cfg",
                        "BU",
                        "long",
                        "*",
                        "protected",
                        4,
                        0.75,
                        12000.0,
                        3000.0,
                        0.8,
                        "attribution_auto",
                        "past memory",
                        "2025-03-03",
                        "2025-03-03T20:00:00",
                        "2025-04-03",
                        "{}",
                    ),
                    (
                        "same_day",
                        "cfg",
                        "BU",
                        "long",
                        '["Bullish"]',
                        "weak_block",
                        4,
                        0.0,
                        -12000.0,
                        -3000.0,
                        0.9,
                        "same_day",
                        "same day memory",
                        "2025-03-04",
                        "2025-03-04T20:00:00",
                        "2025-04-04",
                        "{}",
                    ),
                    (
                        "unknown_source",
                        "cfg",
                        "BU",
                        "long",
                        '["Bullish"]',
                        "weak_block",
                        4,
                        0.0,
                        -20000.0,
                        -5000.0,
                        1.0,
                        "legacy",
                        "legacy memory",
                        None,
                        "2025-03-01T20:00:00",
                        "2025-04-01",
                        "{}",
                    ),
                ],
            )
            conn.commit()
            conn.close()

            memory = db.get_strategy_memory("cfg", "BU", "long", "2025-03-04", ["Bullish"])

            self.assertEqual(memory["side_memory"]["id"], "past")
            self.assertIsNone(memory["combo"])
            self.assertEqual([row["id"] for row in memory["records"]], ["past"])
        finally:
            os.remove(db_path)


class SettlementAccountingRegressionTest(unittest.TestCase):
    def test_dev_config_catalogs_expand_to_runtime_shape(self):
        config_path = SRC_ROOT / "config" / "dev.yaml"
        with config_path.open("r", encoding="utf-8") as fh:
            cfg = normalize_config(yaml.safe_load(fh), config_path)

        loaded = cfg.get("_config_catalogs_loaded", {})
        self.assertEqual(
            set(loaded),
            {
                "analyst_prior_profiles",
                "data_factor_policy",
                "execution_commission",
                "execution_exit_policy",
                "execution_slippage",
                "learning_policy",
                "portfolio_policy",
            },
        )
        self.assertEqual(cfg["max_total_margin_ratio"], 0.20)
        self.assertEqual(cfg["position_budget_policy"]["hard_max_total_margin_ratio"], 0.20)
        self.assertEqual(cfg["position_budget_policy"]["min_real_trade_margin_ratio"], 0.008)
        self.assertEqual(cfg["position_budget_policy"]["probe_margin_ratio"], 0.008)

        self.assertTrue(cfg["market_confirmation"]["enabled"])
        self.assertTrue(cfg["portfolio_manager"]["holding_rebalance_control"]["enabled"])
        self.assertTrue(cfg["trade_auditor"]["enabled"])
        self.assertTrue(cfg["trade_frequency_control"]["enabled"])
        self.assertTrue(cfg["ticker_performance_control"]["enabled"])
        self.assertTrue(cfg["ticker_loss_control"]["enabled"])
        self.assertTrue(cfg["dynamic_weights"]["enabled"])
        self.assertEqual(
            cfg["_config_parameter_roles"]["trade_auditor"],
            "portfolio_policy_catalog_runtime_expanded",
        )
        self.assertEqual(
            cfg["_config_parameter_roles"]["trade_frequency_control"],
            "portfolio_policy_catalog_runtime_expanded",
        )
        self.assertEqual(
            cfg["_config_parameter_roles"]["ticker_performance_control"],
            "portfolio_policy_catalog_runtime_expanded",
        )
        self.assertEqual(
            cfg["_config_parameter_roles"]["ticker_loss_control"],
            "portfolio_policy_catalog_runtime_expanded",
        )
        self.assertEqual(
            cfg["_config_parameter_roles"]["dynamic_weights"],
            "portfolio_policy_catalog_runtime_expanded",
        )
        self.assertFalse(cfg["analyst_weight_policy"]["static_weights_can_create_trade_authority"])
        self.assertFalse(cfg["analyst_weight_policy"]["allow_static_weights_to_open"])
        self.assertEqual(
            cfg["_config_parameter_roles"]["portfolio_manager.sector_weights"],
            "cold_start_trade_timing_prior_only_not_trade_authority",
        )

        self.assertTrue(cfg["learning_retention"]["enabled"])
        self.assertEqual(cfg["learning_retention"]["detail_retention_days"], 90)
        self.assertEqual(cfg["learning_retention"]["aggregate_retention_days"], 180)
        self.assertTrue(cfg["factor_data"]["finoview_enabled"])
        self.assertTrue(cfg["fundamental_quality_control"]["enabled"])
        self.assertTrue(cfg["pandaai_extra_data"]["enabled"])

        execution = cfg["execution"]
        self.assertIn("commission", execution)
        self.assertIn("BU", execution["commission"]["by_underlying"])
        self.assertIn("slippage_ticks_by_underlying", execution)
        self.assertIn("BU", execution["slippage_ticks_by_underlying"])
        self.assertIn("exit_policy", execution)
        self.assertTrue(execution["intraday_confirmation"]["enabled"])
        self.assertNotIn("allow_confirmed_memory_vwap_fallback", execution["intraday_confirmation"])
        self.assertNotIn("confirmed_memory_min_market_confirmation_score", execution["intraday_confirmation"])
        self.assertNotIn("confirmed_memory_min_confirmations", execution["intraday_confirmation"])

        raw_dev = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        for moved_key in (
            "trade_auditor",
            "trade_frequency_control",
            "ticker_performance_control",
            "ticker_loss_control",
            "dynamic_weights",
        ):
            self.assertNotIn(moved_key, raw_dev)

    def test_analyst_prior_catalog_is_prior_only_trade_authority_policy(self):
        cfg = normalize_config(
            {
                "config_catalogs": {
                    "analyst_prior_profiles": "analyst_prior_profiles.yaml",
                    "portfolio_policy": "portfolio_policy_catalog.yaml",
                    "learning_policy": "learning_policy_catalog.yaml",
                    "data_factor_policy": "data_factor_policy_catalog.yaml",
                },
                "portfolio_manager": {},
                "analyst_weight_policy": {
                    "enabled": True,
                    "strategic_profile": "sector_strategic_view",
                    "trade_timing_profile": "daily_trade_timing",
                },
            },
            SRC_ROOT / "config" / "dev.yaml",
        )

        policy = cfg["analyst_weight_policy"]
        self.assertEqual(policy["static_weights_mode"], "prior_only")
        self.assertFalse(policy["static_weights_can_create_trade_authority"])
        self.assertFalse(policy["allow_static_weights_to_open"])
        self.assertIn("analyst_prior_profiles", cfg["_config_catalogs_loaded"])
        self.assertEqual(
            cfg["_config_parameter_roles"]["portfolio_manager.sector_weights"],
            "cold_start_trade_timing_prior_only_not_trade_authority",
        )
        self.assertEqual(
            cfg["_config_parameter_roles"]["analyst_applicability_profile"],
            "cold_start_applicability_prior_only_not_trade_authority",
        )
        self.assertEqual(
            cfg["analyst_applicability_profile"]["technical"]["horizon_multipliers"]["short"],
            1.15,
        )
        self.assertIn("portfolio_policy", cfg["_config_catalogs_loaded"])
        self.assertEqual(
            cfg["_config_parameter_roles"]["portfolio_manager"],
            "portfolio_policy_catalog_runtime_expanded",
        )
        self.assertEqual(
            cfg["market_confirmation"]["min_confirmation_score_for_new_entry"],
            0.55,
        )
        self.assertTrue(cfg["portfolio_manager"]["quality_aware_fusion"]["enabled"])
        self.assertIn("learning_policy", cfg["_config_catalogs_loaded"])
        self.assertEqual(
            cfg["_config_parameter_roles"]["learning_retention"],
            "learning_policy_catalog_runtime_expanded",
        )
        self.assertTrue(cfg["learning"]["contextual_rule_calibration"]["enabled"])
        self.assertEqual(cfg["learning_retention"]["detail_retention_days"], 90)
        self.assertEqual(
            cfg["_config_parameter_roles"]["signal_quality"],
            "learning_policy_catalog_runtime_expanded",
        )
        self.assertTrue(cfg["signal_quality"]["neutral_accountability"]["enabled"])
        self.assertEqual(cfg["signal_quality"]["neutral_accountability"]["counterfactual_forward_days"], 3)
        self.assertIn("data_factor_policy", cfg["_config_catalogs_loaded"])
        self.assertEqual(
            cfg["_config_parameter_roles"]["factor_data"],
            "data_factor_policy_catalog_runtime_expanded",
        )
        self.assertTrue(cfg["fundamental_quality_control"]["enabled"])
        self.assertTrue(cfg["pandaai_extra_data"]["enabled"])
        self.assertTrue(cfg["factor_data"]["finoview_enabled"])
        self.assertTrue(cfg["factor_data"]["news"]["enabled"])

    def test_settlement_balance_formula_includes_margin_change(self):
        settlement_row = {
            "previous_balance": 1000.0,
            "current_balance": 900.0,
            "previous_margin": 100.0,
            "current_margin": 250.0,
            "daily_pnl": 60.0,
            "commission": 10.0,
            "deposit": 0.0,
            "withdraw": 0.0,
        }

        actual_change = settlement_row["current_balance"] - settlement_row["previous_balance"]
        self.assertAlmostEqual(actual_change, _expected_settlement_balance_change(settlement_row))

    def test_settlement_equity_formula_excludes_margin_transfer(self):
        settlement_row = {
            "previous_balance": 1000.0,
            "current_balance": 900.0,
            "previous_margin": 100.0,
            "current_margin": 250.0,
            "previous_account_equity": 1100.0,
            "current_account_equity": 1150.0,
            "daily_pnl": 60.0,
            "commission": 10.0,
            "deposit": 0.0,
            "withdraw": 0.0,
        }

        equity_change = (
            settlement_row["current_account_equity"]
            - settlement_row["previous_account_equity"]
        )
        self.assertAlmostEqual(equity_change, _expected_settlement_equity_change(settlement_row))

    def test_phase2_execution_moves_margin_between_cash_and_reserved_margin(self):
        engine = FuturesExecutionEngine({"execution": {}}, db=None)
        portfolio = Portfolio(id="p1", cashflow=1000.0, positions={})
        transaction = FuturesTransaction(
            id="tx1",
            portfolio_id="p1",
            config_id="cfg",
            recommendation_id="rec1",
            trading_date="2025-01-02",
            ticker="RB",
            contract_code="rb2505",
            action=FuturesAction.OPEN_LONG,
            lots=1,
            price=100.0,
            execution_price=100.0,
            contract_multiplier=10.0,
            margin_rate=0.1,
            margin_used=100.0,
            commission=2.0,
            execution_phase="phase2",
            created_at="2025-01-02T00:00:00",
        )

        portfolio = engine.apply_transaction_to_portfolio(portfolio, transaction)

        self.assertAlmostEqual(portfolio.cashflow, 898.0)
        self.assertAlmostEqual(portfolio.margin_used, 100.0)
        self.assertAlmostEqual(portfolio.account_equity, 998.0)
        self.assertAlmostEqual(portfolio.margin_available, 898.0)

    def test_hold_or_zero_lots_recommendation_is_skipped_not_executed(self):
        class DummyDB:
            def __init__(self):
                self.status_updates = []

            def update_futures_recommendation_status(self, recommendation_id, status, **kwargs):
                self.status_updates.append((recommendation_id, status, kwargs))

        db = DummyDB()
        engine = FuturesExecutionEngine({"execution": {}}, db=db)
        portfolio = Portfolio(id="p1", cashflow=1000.0, positions={})
        recommendation = {
            "id": "rec-hold",
            "config_id": "cfg",
            "portfolio_id": "p1",
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.HOLD.value,
            "lots": 0,
            "signal_snapshot": {
                "execution_translation": {
                    "rewrite_reasons": ["position_matched"],
                }
            },
        }

        result = engine.execute_recommendation(
            recommendation_id="rec-hold",
            recommendation=recommendation,
            portfolio=portfolio,
            trading_date="2025-01-02",
            execution_phase=TradingPhase.PHASE2,
        )

        self.assertIs(result, portfolio)
        self.assertEqual(db.status_updates[0][1], RecommendationStatus.SKIPPED.value)
        snapshot = db.status_updates[0][2]["signal_snapshot"]
        self.assertEqual(snapshot["execution_result"]["status"], RecommendationStatus.SKIPPED.value)
        self.assertEqual(snapshot["execution_result"]["transaction_count"], 0)
        self.assertEqual(snapshot["execution_result"]["no_trade_reason"], "position_matched")
        execution_trace = snapshot["execution_result"]["execution_learning_trace"]
        self.assertEqual(execution_trace["consumer_scope"], "trader_execution_learning")
        self.assertEqual(execution_trace["learning_lane"], "execution")
        self.assertIn("execution", execution_trace["execution_retrieval_key"])
        self.assertTrue(execution_trace["turn_into_memory"])

    def test_dynamic_margin_missing_provider_raises_when_static_fallback_disabled(self):
        engine = FuturesExecutionEngine(
            {
                "execution": {
                    "dynamic_margin": {
                        "enabled": True,
                        "provider": "pandaai",
                        "fallback_to_static_contract_cache": False,
                    }
                }
            },
            db=None,
        )

        with self.assertRaisesRegex(RuntimeError, "contract margin is unavailable"):
            engine._resolve_dynamic_margin_rate(
                action_value=RecommendationAction.OPEN_LONG.value,
                contract_code="rb2505",
                contract_info={"margin_rate_long": 0.10, "margin_rate_short": 0.12},
                trading_date="2025-01-02",
                contract_detail={},
            )

    def test_dynamic_margin_missing_provider_can_fall_back_when_explicitly_enabled(self):
        engine = FuturesExecutionEngine(
            {
                "execution": {
                    "dynamic_margin": {
                        "enabled": True,
                        "provider": "pandaai",
                        "fallback_to_static_contract_cache": True,
                    }
                }
            },
            db=None,
        )

        margin_rate, audit = engine._resolve_dynamic_margin_rate(
            action_value=RecommendationAction.OPEN_SHORT.value,
            contract_code="rb2505",
            contract_info={"margin_rate_long": 0.10, "margin_rate_short": 0.12},
            trading_date="2025-01-02",
            contract_detail={},
        )

        self.assertAlmostEqual(margin_rate, 0.12)
        self.assertEqual(audit["status"], "fallback_static_no_provider_margin")
        self.assertTrue(audit["fallback_to_static_contract_cache"])


class CheckDbConsistencyRegressionTest(unittest.TestCase):
    def test_rebuilt_check_db_contains_accounting_columns_and_matching_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "agentquant.db"
            target = Path(temp_dir) / "agentquantcheck.db"
            conn = sqlite3.connect(source)
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE daily_settlement (
                        portfolio_id TEXT,
                        trading_date TEXT,
                        previous_balance REAL,
                        current_balance REAL,
                        previous_account_equity REAL,
                        current_account_equity REAL,
                        cash_available REAL,
                        reserved_margin REAL,
                        previous_margin REAL,
                        current_margin REAL,
                        daily_pnl REAL,
                        commission REAL,
                        margin_ratio REAL,
                        is_warning INTEGER,
                        is_liquidation INTEGER,
                        created_at TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT INTO daily_settlement
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "p1",
                        "2025-03-04",
                        100.0,
                        90.0,
                        120.0,
                        110.0,
                        90.0,
                        20.0,
                        20.0,
                        20.0,
                        -10.0,
                        0.0,
                        0.18,
                        0,
                        0,
                        "now",
                    ),
                )
                cur.execute(
                    """
                    CREATE TABLE trading_day_phase (
                        config_id TEXT,
                        trading_date TEXT,
                        phase TEXT,
                        status TEXT,
                        started_at TEXT,
                        completed_at TEXT,
                        message TEXT
                    )
                    """
                )
                cur.execute(
                    "INSERT INTO trading_day_phase VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("cfg", "2025-03-04", "phase3", "completed", "now", "now", ""),
                )
                conn.commit()
            finally:
                conn.close()

            rebuild_check_db(source, target)
            result = validate_check_db_consistency(source, target)

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["row_counts"]["check_daily_settlement"]["source"], 1)


class PlotConfigRegressionTest(unittest.TestCase):
    def test_plot_config_generates_portfolio_and_each_traded_ticker_chart(self):
        calls = []

        class FakePortfolioPlotter:
            def __init__(self, *args, **kwargs):
                calls.append(("portfolio", args, kwargs))

            def run(self):
                return True

        class FakeSingleFuturePlotter:
            def __init__(self, *args, **kwargs):
                calls.append(("ticker", args, kwargs))

            def run(self):
                return True

        original_portfolio = plot_config.PortfolioCurvePlotter
        original_single = plot_config.SingleFutureCurvePlotter
        original_load_config = plot_config.load_config_exp_name
        original_get_config_id = plot_config.get_config_id
        original_load_tickers = plot_config.load_traded_tickers
        try:
            plot_config.PortfolioCurvePlotter = FakePortfolioPlotter
            plot_config.SingleFutureCurvePlotter = FakeSingleFuturePlotter
            plot_config.load_config_exp_name = lambda config_path: "exp"
            plot_config.get_config_id = lambda db_path, exp_name: "cfg"
            plot_config.load_traded_tickers = lambda db_path, config_id: ["BU", "CF", "J"]

            runner = plot_config.ConfigPlotRunner("src/config/dev.yaml")
            self.assertTrue(runner.run())
            self.assertEqual([call[0] for call in calls], ["portfolio", "ticker", "ticker", "ticker"])
            self.assertEqual([call[1][1] for call in calls[1:]], ["BU", "CF", "J"])
        finally:
            plot_config.PortfolioCurvePlotter = original_portfolio
            plot_config.SingleFutureCurvePlotter = original_single
            plot_config.load_config_exp_name = original_load_config
            plot_config.get_config_id = original_get_config_id
            plot_config.load_traded_tickers = original_load_tickers


class OrderTranslationRegressionTest(unittest.TestCase):
    def test_phase2_entry_audit_has_no_pm_internal_draft_target_field(self):
        audit = phase2_entry_audit(
            target_lots=-1,
            current_lots=0,
            price_context=SimpleNamespace(base_price=3000.0, base_price_source="open"),
        )

        self.assertEqual(audit["entry_action_family"], "open_short")
        self.assertEqual(audit["target_lots_source"], "final_action_contract")
        self.assertNotIn("phase1_target_lots", audit)

    @staticmethod
    def _final_entry_authority(authority_type="real_budget_entry", *, current_evidence=False):
        authority = {
            "authority_type": authority_type,
            "open_action_evidence": authority_type == "real_budget_entry",
            "strong_current_evidence": authority_type == "real_budget_entry",
            "max_allowed_margin_ratio": 0.12 if authority_type == "real_budget_entry" else 0.015,
            "reason_codes": ["test_pm_final_trade_authority"],
        }
        if current_evidence:
            authority.update(
                {
                    "open_action_evidence": True,
                    "technical_confirmation": True,
                    "has_invalidation_or_stop": True,
                }
            )
        return authority

    @staticmethod
    def _strategy_contract(
        ticker: str,
        *,
        current_lots: int = 0,
        target_lots: int,
        final_action: str | None = None,
        authority_type: str = "real_budget_entry",
        tradable_reason: str = "tradable",
        reason_codes: list[str] | None = None,
        current_evidence: bool = True,
    ):
        if final_action is None:
            if target_lots == current_lots:
                final_action = "hold" if current_lots else "wait"
            elif current_lots == 0:
                final_action = "open_real"
            elif target_lots == 0:
                final_action = "exit"
            elif (target_lots > 0) == (current_lots > 0):
                final_action = "scale" if abs(target_lots) > abs(current_lots) else "reduce"
            else:
                final_action = "exit"
        authority = OrderTranslationRegressionTest._final_entry_authority(
            authority_type,
            current_evidence=current_evidence,
        )
        if authority_type in {"real_budget_entry", "exploration_probe", "watchlist_only"}:
            authority["authority_type"] = authority_type
        contract = {
            "contract_version": "agentquant.final_action.v1",
            "ticker": ticker,
            "final_action": final_action,
            "contract_type": "strategy",
            "current_lots": int(current_lots),
            "target_lots": int(target_lots),
            "lots_delta": int(target_lots - current_lots),
            "lots_delta_abs": abs(int(target_lots - current_lots)),
            "reason_codes": list(reason_codes or [tradable_reason]),
            "execution_requirement": (
                "intraday_trigger_required"
                if final_action in {"open_probe", "open_real", "scale"}
                else "position_management_or_wait"
            ),
            "single_source_of_trade_truth": True,
            "candidate_sources_do_not_bypass_contract": True,
        }
        contract.update(authority)
        return contract

    def test_transaction_audit_payload_carries_final_trade_contract_mirror(self):
        snapshot = {
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "RB",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": -4,
                "lots_delta": -4,
                "target_margin_ratio_estimate": 0.041,
                "authority_type": "real_budget_entry",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "max_allowed_margin_ratio": 0.12,
                "reason_codes": ["positive_candidate_open", "tradeable_candidate"],
                "execution_requirement": "intraday_trigger_required",
                "execution_profile": "vwap_confirmed",
                "trigger_source": "final_contract_execution_fields",
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
                "learning_used": {
                    "alpha_setup_action_values": [
                        {
                            "action_name": "open",
                            "action_preference": "positive_candidate_open",
                            "sample_scope": "exact_real_state",
                            "memory_quality": "exact_real_state",
                            "reward_mean": 1860.0,
                        }
                    ]
                },
            },
            "pm_internal_draft": {
                "execution_fields": {
                    "execution_profile": "vwap_confirmed",
                }
            },
            "execution_translation": {
                "execution_contract": {
                    "execution_profile": "breakout_confirmed",
                }
            },
            "phase2_execution": {
                "pm_plan_validation": {
                    "passed": True,
                    "reason": "final_trade_authority_present",
                    "business_boundary": "strategy_new_entry_requires_pm_final_trade_authority",
                    "authority_consistency": {
                        "reason": "final_contract_authority_consistent",
                    },
                }
            },
        }

        payload = build_audit_payload(snapshot)

        self.assertEqual(payload["final_action_contract"]["final_action"], "open_real")
        self.assertEqual(payload["final_action_contract"]["authority_type"], "real_budget_entry")
        audit = payload["trade_contract_audit"]
        self.assertTrue(audit["single_source_of_trade_truth"])
        self.assertTrue(audit["candidate_sources_do_not_bypass_contract"])
        self.assertEqual(audit["final_action"], "open_real")
        self.assertEqual(audit["authority_type"], "real_budget_entry")
        self.assertEqual(audit["target_lots"], -4)
        self.assertEqual(audit["lots_delta"], -4)
        self.assertEqual(audit["execution_profile"], "vwap_confirmed")
        self.assertTrue(audit["pm_plan_validation_passed"])
        self.assertEqual(
            audit["authority_consistency_reason"],
            "final_contract_authority_consistent",
        )
        self.assertEqual(
            audit["selected_action_preferences"][0]["action_preference"],
            "positive_candidate_open",
        )
        self.assertIn("transaction audit mirror only", audit["audit_boundary"])

    def test_transaction_audit_does_not_backfill_execution_profile_from_pm_draft(self):
        snapshot = {
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "RB",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": -2,
                "lots_delta": -2,
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            "pm_internal_draft": {
                "execution_fields": {
                    "execution_profile": "breakout",
                    "trigger_source": "stale_pm_internal_draft",
                }
            },
            "execution_translation": {
                "execution_contract": {
                    "execution_profile": "vwap_confirmed",
                    "trigger_source": "translation_copy",
                }
            },
            "phase2_execution": {
                "execution_contract": {
                    "execution_profile": "pullback",
                    "trigger_source": "phase2_copy",
                },
            },
        }

        payload = build_audit_payload(snapshot)

        self.assertIsNone(payload["trade_contract_audit"]["execution_profile"])

    def test_audit_target_lots_come_from_final_contract_not_pm_draft(self):
        recommendation = {
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": {
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "RB",
                    "final_action": "open_real",
                    "current_lots": 0,
                    "target_lots": -1,
                    "lots_delta": -1,
                },
                "pm_internal_draft": {
                    "target_lots": -8,
                    "target_position_ratio": -0.04,
                },
            },
        }

        self.assertEqual(infer_target_lots(recommendation), -1)

    def test_trader_execution_contract_reads_only_final_contract_not_stale_pm_internal_draft(self):
        snapshot = {
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "RB",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": -2,
                "lots_delta": -2,
                "execution_profile": "vwap_confirmed",
                "trigger_source": "final_contract_execution_fields",
            },
            "pm_internal_draft": {
                "execution_fields": {
                    "contract_version": "agentquant.execution_contract.v1",
                    "execution_profile": "breakout",
                    "trigger_source": "stale_pm_internal_draft",
                }
            },
        }

        execution_contract = _execution_contract_from_snapshot(snapshot)

        self.assertEqual(execution_contract["execution_profile"], "vwap_confirmed")
        self.assertEqual(execution_contract["trigger_source"], "final_contract_execution_fields")

        missing_contract_plan = {
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "RB",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": -2,
                "lots_delta": -2,
            },
            "pm_internal_draft": {
                "execution_fields": {
                    "contract_version": "agentquant.execution_contract.v1",
                    "execution_profile": "breakout",
                    "trigger_source": "stale_pm_internal_draft",
                }
            },
        }

        self.assertEqual(_execution_contract_from_snapshot(missing_contract_plan), {})

    def test_strategy_recommendation_without_pm_plan_cannot_execute_raw_action_lots(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": {},
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        self.assertIn(
            "missing_final_action_contract",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        self.assertFalse(snapshot["phase2_execution"]["pm_plan_validation"]["passed"])
        self.assertEqual(
            snapshot["phase2_execution"]["pm_plan_validation"]["business_boundary"],
            "strategy_recommendation_requires_final_action_contract",
        )

    def test_strategy_new_entry_without_final_authority_is_not_translated_to_open(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": {
                "pm_internal_draft": {
                    "target_position_ratio": -0.04,
                    "target_lots": -8,
                    "lots_delta_abs": 8,
                    "reason_codes": "tradable",
                },
                "final_action_contract": self._strategy_contract(
                    "RB",
                    target_lots=-8,
                    authority_type="watchlist_only",
                ),
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(recommendation["signal_snapshot"])

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        self.assertIn(
            "final_contract_authority_not_met",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertFalse(validation["passed"])
        self.assertEqual(validation["original_target_lots"], -8)
        self.assertEqual(validation["target_lots_after_validation"], 0)

    def test_strategy_recommendation_without_final_action_contract_cannot_use_pm_draft_lots(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "pm_internal_draft": {
                "target_position_ratio": -0.04,
                "target_lots": -8,
                "lots_delta_abs": 8,
                "reason_codes": "tradable",
            },
        }
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        self.assertIn(
            "missing_final_action_contract",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertFalse(validation["passed"])
        self.assertEqual(validation["reason"], "missing_final_action_contract")
        self.assertEqual(validation["target_lots_after_validation"], 0)

    def test_strategy_recommendation_with_non_strategy_contract_cannot_use_raw_action_lots(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "operational_rollover",
                "ticker": "RB",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": -8,
                "lots_delta": -8,
            },
        }
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        self.assertIn(
            "unsupported_final_action_contract_type",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertFalse(validation["passed"])
        self.assertEqual(validation["reason"], "unsupported_final_action_contract_type")
        self.assertEqual(validation["target_lots_after_validation"], 0)

    def test_unknown_source_type_cannot_execute_raw_action_lots(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": "legacy_manual",
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": {
                "phase2_execution": {
                    "raw_action_fixture": "must_not_execute",
                }
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(recommendation["signal_snapshot"])

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        self.assertIn(
            "unsupported_raw_action_source_type",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertFalse(validation["passed"])
        self.assertEqual(validation["reason"], "unsupported_raw_action_source_type")
        self.assertEqual(validation["source_type"], "legacy_manual")
        self.assertEqual(validation["target_lots_after_validation"], 0)

    def test_strategy_new_entry_uses_final_contract_authority_fields(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "final_action_contract": self._strategy_contract(
                "RB",
                target_lots=-8,
            ),
        }
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 8)
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertTrue(validation["passed"])
        self.assertEqual(validation["reason"], "final_action_contract_present")

    def test_strategy_hold_contract_cannot_be_retranslated_from_stale_pre_open_target(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=200000.0,
            positions={
                "BU": Position(
                    shares=-10,
                    value=420000.0,
                    margin_used=200000.0,
                    contract_code="bu2506",
                    entry_date="2025-03-07",
                    entry_price=4200.0,
                )
            },
        )
        signal_snapshot = {
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "BU",
                "final_action": "hold",
                "contract_type": "strategy",
                "current_lots": -10,
                "target_lots": -10,
                "lots_delta": 0,
                "lots_delta_abs": 0,
                "reason_codes": "position_matched",
                "authority_type": "not_applicable",
                "reason_codes": ["position_matched"],
                "execution_requirement": "position_management_or_wait",
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            "pm_internal_draft": {
                "target_position_ratio": -0.035,
                "target_lots": -9,
                "lots_delta_abs": 1,
                "reason_codes": "position_matched",
            },
        }
        recommendation = {
            "underlying_code": "BU",
            "contract_code": "bu2506",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.HOLD.value,
            "lots": 0,
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=4200.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        translation = snapshot.get("execution_translation", {})
        self.assertIn("final_action_contract_source", translation)
        self.assertEqual(translation["phase2_order_plan"]["target_lots"], -10)
        self.assertEqual(
            translation["phase2_order_plan"]["consistency_diagnostics"]["expected"]["action"],
            "hold",
        )
        self.assertNotIn("stale_pre_open_target_used", translation)

    def test_strategy_final_contract_executes_without_pm_internal_draft(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "final_action_contract": self._strategy_contract(
                "RB",
                target_lots=-8,
                final_action="open_real",
            ),
        }
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 8)
        translation = snapshot.get("execution_translation", {})
        self.assertEqual(translation["final_action_contract_source"]["source"], "final_action_contract")
        self.assertNotIn("pm_internal_draft", snapshot)

    def test_strategy_final_contract_ignores_stale_pm_internal_draft_target_lots(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "final_action_contract": self._strategy_contract(
                "RB",
                target_lots=-1,
                final_action="open_real",
            ),
            "pm_internal_draft": {
                "target_position_ratio": -0.04,
                "target_lots": -8,
                "lots_delta_abs": 8,
                "reason_codes": "stale_pm_draft",
            },
        }
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 1)
        translation = snapshot.get("execution_translation", {})
        self.assertEqual(translation["final_action_contract_source"]["target_lots"], -1)
        self.assertEqual(translation["phase2_order_plan"]["target_lots"], -1)

    def test_phase2_preserves_phase1_tradable_one_lot_probe_when_ratio_floors_to_zero(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "PB",
            "contract_code": "pb2501",
            "signal_snapshot": {
                "pm_internal_draft": {
                    "target_position_ratio": 0.00001,
                    "target_lots": 1,
                    "lots_delta_abs": 1,
                    "reason_codes": "tradable",
                },
                "final_action_contract": self._strategy_contract(
                    "PB",
                    target_lots=1,
                    final_action="open_probe",
                    authority_type="exploration_probe",
                    current_evidence=True,
                ),
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=19000.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_LONG)
        self.assertEqual(decision.lots, 1)
        translation = snapshot.get("execution_translation", {})
        self.assertEqual(
            translation["final_action_contract_source"]["source"],
            "final_action_contract",
        )
        self.assertEqual(translation["phase2_order_plan"]["target_lots"], 1)

    def test_phase2_bare_exploration_probe_without_current_evidence_is_not_translated_to_open(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "SR",
            "contract_code": "sr2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_LONG.value,
            "lots": 8,
            "signal_snapshot": {
                "pm_internal_draft": {
                    "target_position_ratio": 0.02,
                    "target_lots": 8,
                    "lots_delta_abs": 8,
                    "reason_codes": "tradable",
                },
                "final_action_contract": self._strategy_contract(
                    "SR",
                    target_lots=8,
                    final_action="open_probe",
                    authority_type="exploration_probe",
                    current_evidence=False,
                ),
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=5900.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertFalse(validation["passed"])
        self.assertEqual(validation["reason"], "final_contract_authority_not_met")
        self.assertEqual(validation["target_lots_after_validation"], 0)

    def test_phase2_conditional_probe_contract_can_reach_intraday_trigger_check(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        contract = self._strategy_contract(
            "SR",
            target_lots=1,
            final_action="open_probe",
            authority_type="exploration_probe",
            current_evidence=False,
            reason_codes=["pm_watch_for_trigger_probe_cap", "conditional_trigger_authority"],
        )
        contract.update(
            {
                "conditional_trigger_authority": True,
                "requires_intraday_confirmation": True,
                "can_execute_without_intraday_trigger": False,
                "watch_for_trigger_block": False,
                "execution_profile": "breakout",
                "entry_trigger": "wait for price to break above 5900 after open",
                "invalidation": "below 5840",
            }
        )
        recommendation = {
            "underlying_code": "SR",
            "contract_code": "sr2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_LONG.value,
            "lots": 1,
            "signal_snapshot": {
                "pm_internal_draft": {
                    "target_position_ratio": 0.02,
                    "target_lots": 8,
                    "lots_delta_abs": 8,
                    "reason_codes": "legacy_draft_must_not_expand_contract",
                },
                "final_action_contract": contract,
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=5900.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_LONG)
        self.assertEqual(decision.lots, 1)
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertTrue(validation["passed"])
        self.assertEqual(validation["target_lots"], 1)
        self.assertEqual(
            snapshot["execution_translation"]["final_action_contract_source"]["target_lots"],
            1,
        )

    def test_phase2_exploration_probe_with_current_evidence_can_translate_to_open(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "PB",
            "contract_code": "pb2501",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 1,
            "signal_snapshot": {
                "pm_internal_draft": {
                    "target_position_ratio": -0.005,
                    "target_lots": -1,
                    "lots_delta_abs": 1,
                    "reason_codes": "tradable",
                },
                "final_action_contract": self._strategy_contract(
                    "PB",
                    target_lots=-1,
                    final_action="open_probe",
                    authority_type="exploration_probe",
                    current_evidence=True,
                    reason_codes=["market_confirmation_quality_gate"],
                ),
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=19000.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 1)
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertTrue(validation["passed"])
        self.assertEqual(validation["reason"], "final_action_contract_present")

    def test_phase2_does_not_expand_phase1_target_lots_when_open_price_changes(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5162600.45,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "TA",
            "contract_code": "ta601",
            "signal_snapshot": {
                "pm_internal_draft": {
                    "target_position_ratio": -0.12,
                    "target_lots": -26,
                },
                "final_action_contract": self._strategy_contract(
                    "TA",
                    target_lots=-26,
                ),
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.75,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=4566.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 26)
        self.assertEqual(
            snapshot["execution_translation"]["final_action_contract_source"]["target_lots"],
            -26,
        )

    def test_phase2_records_signal_invalidation_but_does_not_rewrite_final_contract(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5162600.45,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "technical": {
                "signal": "Bearish",
                "horizon_class": "short",
                "expected_horizon_days": 2,
                "invalidation_level": 4400.0,
                "target_return": 0.03,
            },
            "pm_internal_draft": {
                "target_position_ratio": -0.12,
                "target_lots": -26,
            },
            "final_action_contract": self._strategy_contract(
                "TA",
                target_lots=-26,
            ),
        }
        recommendation = {
            "underlying_code": "TA",
            "contract_code": "ta601",
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.75,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=4566.0),
            snapshot=snapshot,
        )

        translation = snapshot.get("execution_translation", {})
        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 26)
        observation = snapshot["phase2_execution"]["contract_execution_observation"]
        self.assertTrue(observation["signal_invalidation_observed"])
        self.assertIn("does not rewrite", observation["business_boundary"])
        self.assertNotIn("signal_invalidation_level", translation.get("rewrite_reasons", []))
        self.assertEqual(translation["phase2_order_plan"]["signal_lifecycle"]["invalidation_level"], 4400.0)

    def test_phase2_ignores_opposing_invalidation_level_for_short_target(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "technical": {
                "signal": "Bearish",
                "horizon_class": "short",
                "expected_horizon_days": 2,
                "entry_trigger": "Break below prior low with volume expansion",
            },
            "fundamental": {
                "signal": "Bullish",
                "horizon_class": "medium",
                "expected_horizon_days": 5,
                "invalidation_level": 2620.0,
            },
            "pm_internal_draft": {
                "target_position_ratio": -0.04,
                "target_lots": -7,
            },
            "final_action_contract": self._strategy_contract(
                "M",
                target_lots=-7,
            ),
        }
        recommendation = {
            "underlying_code": "M",
            "contract_code": "m2505",
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=2660.0),
            snapshot=snapshot,
        )

        translation = snapshot.get("execution_translation", {})
        lifecycle_filter = translation.get("signal_lifecycle_direction_filter", {})
        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 7)
        self.assertNotIn("signal_invalidation_level", translation.get("rewrite_reasons", []))
        self.assertNotIn("invalidation_level", translation.get("signal_lifecycle", {}))
        self.assertNotIn(
            "invalidation_level",
            translation.get("phase2_order_plan", {}).get("signal_lifecycle", {}),
        )
        self.assertEqual(lifecycle_filter.get("target_side"), "short")
        self.assertIsNone(lifecycle_filter.get("effective_invalidation_level"))
        self.assertEqual(lifecycle_filter.get("ignored_opposing", [])[0]["analyst"], "fundamental")

    def test_phase2_ignores_opposing_invalidation_level_for_long_target(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "technical": {
                "signal": "Bullish",
                "horizon_class": "short",
                "expected_horizon_days": 2,
                "entry_trigger": "Break above prior high with volume expansion",
            },
            "fundamental": {
                "signal": "Bearish",
                "horizon_class": "medium",
                "expected_horizon_days": 5,
                "invalidation_level": 3820.0,
            },
            "pm_internal_draft": {
                "target_position_ratio": 0.04,
                "target_lots": 5,
            },
            "final_action_contract": self._strategy_contract(
                "BU",
                target_lots=5,
            ),
        }
        recommendation = {
            "underlying_code": "BU",
            "contract_code": "bu2506",
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3739.0),
            snapshot=snapshot,
        )

        translation = snapshot.get("execution_translation", {})
        lifecycle_filter = translation.get("signal_lifecycle_direction_filter", {})
        self.assertEqual(decision.action, FuturesAction.OPEN_LONG)
        self.assertEqual(decision.lots, 5)
        self.assertNotIn("signal_invalidation_level", translation.get("rewrite_reasons", []))
        self.assertNotIn("invalidation_level", translation.get("signal_lifecycle", {}))
        self.assertNotIn(
            "invalidation_level",
            translation.get("phase2_order_plan", {}).get("signal_lifecycle", {}),
        )
        self.assertEqual(lifecycle_filter.get("target_side"), "long")
        self.assertIsNone(lifecycle_filter.get("effective_invalidation_level"))
        self.assertEqual(lifecycle_filter.get("ignored_opposing", [])[0]["analyst"], "fundamental")

    def test_phase2_no_trade_reason_keeps_signal_invalidation_before_position_matched(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5162600.45,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "technical": {
                "signal": "Bearish",
                "horizon_class": "short",
                "expected_horizon_days": 2,
                "invalidation_level": 4400.0,
            },
            "pm_internal_draft": {
                "target_position_ratio": -0.12,
                "target_lots": -26,
            },
            "final_action_contract": self._strategy_contract(
                "TA",
                target_lots=0,
                final_action="wait",
                authority_type="watchlist_only",
                current_evidence=False,
            ),
        }
        recommendation = {
            "underlying_code": "TA",
            "contract_code": "ta601",
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.75,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=4566.0),
            snapshot=snapshot,
        )

        translation = snapshot.get("execution_translation", {})
        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertIsNone(infer_no_trade_reason(snapshot))
        self.assertFalse(snapshot["phase2_execution"]["contract_execution_observation"]["signal_invalidation_observed"])
        self.assertNotIn("position_matched", translation.get("rewrite_reasons", []))

    def test_time_stop_does_not_flatten_supported_same_direction_hold(self):
        current_position = SimpleNamespace(entry_date="2025-02-10", entry_price=3000.0)

        result = evaluate_exit_policy(
            ticker="M",
            current_price=3020.0,
            current_lots=10,
            target_lots=12,
            lifecycle={"template_state": "protected", "setup_type": "trend_follow"},
            current_position=current_position,
            trading_date="2025-02-20",
            config={
                "execution": {
                    "exit_policy": {
                        "enabled": True,
                        "defaults": {"trend_time_stop_days": 5, "probe_time_stop_days": 2},
                    }
                }
            },
        )

        self.assertFalse(result["exit_required"])
        self.assertEqual(result["target_lots"], 12)
        self.assertTrue(result["same_direction_supported"])

    def test_time_stop_flattens_unvalidated_probe_without_same_direction_support(self):
        current_position = SimpleNamespace(entry_date="2025-02-10", entry_price=3000.0)

        result = evaluate_exit_policy(
            ticker="M",
            current_price=2990.0,
            current_lots=10,
            target_lots=5,
            lifecycle={"template_state": "watchlist", "setup_type": "probe"},
            current_position=current_position,
            trading_date="2025-02-14",
            config={
                "execution": {
                    "exit_policy": {
                        "enabled": True,
                        "defaults": {"trend_time_stop_days": 5, "probe_time_stop_days": 2},
                    }
                }
            },
        )

        self.assertTrue(result["exit_required"])
        self.assertEqual(result["target_lots"], 0)
        self.assertEqual(result["reason"], "time_stop")
        self.assertFalse(result["same_direction_supported"])

    def test_phase2_honors_dynamic_opportunity_budget_from_phase1(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "TA",
            "contract_code": "ta601",
            "signal_snapshot": {
                "technical": {
                    "signal": "Bearish",
                    "invalidation_level": 4800.0,
                },
                "pm_internal_draft": {
                    "target_position_ratio": -1.62,
                    "target_margin_ratio_estimate": 0.162,
                    "target_lots": -162,
                    "strategy_controls": {
                        "diagnostics": {
                            "capital_utilization_target": {
                                "target_mode": "alpha_release_boost",
                                "high_quality_memory": True,
                                "dynamic_allocation_tier": "validated_with_stop",
                                "dynamic_opportunity_margin_ratio_budget": 0.162,
                                "dynamic_opportunity_margin_ratio_cap": 0.162,
                                "stop_protected": True,
                            },
                            "net_exposure_control": {
                                "cap_mode": "alpha_release",
                                "max_net_exposure": 2.0,
                            }
                        }
                    },
                },
                "final_action_contract": self._strategy_contract(
                    "TA",
                    target_lots=-162,
                ),
            },
        }
        config = {
            "cashflow": 5000000,
            "capital_utilization_control": {"max_margin_ratio_after_scaling": 0.20},
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=4566.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertGreaterEqual(decision.lots, 39)
        self.assertNotIn(
            "single_position_cap",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        self.assertNotIn(
            "base_sizing_anchor_cap",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        self.assertEqual(
            snapshot["execution_translation"]["final_action_contract_source"]["target_lots"],
            -162,
        )

    def test_rollover_reconciliation_records_close_only_execution_type(self):
        rollover = {
            "underlying_code": "C",
            "from_contract": "c2511",
            "to_contract": "c2601",
        }
        strategy = {
            "action": "open_long",
            "lots": 0,
            "signal_snapshot": {"pm_internal_draft": {"target_lots": 0}},
        }

        adjusted = _reconcile_rollover_with_strategy_target(
            rollover_recommendation=rollover,
            strategy_recommendation=strategy,
            current_lots=-2,
            config={"rollover": {"mode": "reconcile_with_strategy"}},
        )

        self.assertEqual(adjusted["rollover_execution_type"], "close_only_rollover")
        self.assertEqual(adjusted["rollover_close_lots"], 2)
        self.assertEqual(adjusted["rollover_open_lots"], 0)

    def test_rollover_reconciliation_preserves_strategy_same_direction_target(self):
        rollover = {
            "underlying_code": "C",
            "from_contract": "c2511",
            "to_contract": "c2601",
        }
        strategy = {
            "signal_snapshot": {
                "final_action_contract": {
                    "contract_type": "strategy",
                    "current_lots": -2,
                    "target_lots": -3,
                    "lots_delta": -1,
                    "final_action": "scale",
                }
            }
        }

        adjusted = _reconcile_rollover_with_strategy_target(
            rollover_recommendation=rollover,
            strategy_recommendation=strategy,
            current_lots=-2,
            config={"rollover": {"mode": "reconcile_with_strategy"}},
        )

        self.assertEqual(adjusted["rollover_execution_type"], "full_rollover")
        self.assertEqual(adjusted["rollover_close_lots"], 2)
        self.assertEqual(adjusted["rollover_open_lots"], 3)
        self.assertEqual(adjusted["rollover_strategy_target_lots"], -3)

    def test_forced_risk_orders_execute_before_strategy_phase2_processing(self):
        calls = []

        class FakeExecutionEngine:
            def scan_and_create_intraday_forced_risk_orders(self, *, config_id, trading_date, portfolio, cutoff_datetime=None):
                calls.append(("scan", config_id, trading_date, portfolio, cutoff_datetime))
                return ["risk-rec"]

            def execute_pending_forced_risk_orders(self, *, config_id, trading_date, portfolio, execution_phase):
                calls.append(("execute", config_id, trading_date, portfolio, execution_phase))
                return {"after": "forced_risk"}

        result = _execute_pending_forced_risk_before_strategy(
            execution_engine=FakeExecutionEngine(),
            config_id="cfg",
            trading_date="2025-03-04",
            portfolio={"before": "risk"},
        )

        self.assertEqual(result, {"after": "forced_risk"})
        self.assertEqual(
            calls,
            [
                ("scan", "cfg", "2025-03-04", {"before": "risk"}, None),
                ("execute", "cfg", "2025-03-04", {"before": "risk"}, TradingPhase.PHASE2),
            ],
        )

    def test_intraday_margin_call_creates_forced_risk_close_order(self):
        saved = []

        class FakeDb:
            def get_futures_recommendations_by_effective_date(self, **kwargs):
                return []

            def save_futures_recommendation(self, recommendation):
                saved.append(recommendation.model_dump())
                return "forced-risk-1"

        class FakeRouter:
            def get_china_futures_minute_bars(self, **kwargs):
                return [
                    {"datetime": "2025-03-04 10:00:00", "open": 5.0, "close": 5.0, "volume": 10},
                ]

            def get_futures_contract_quote_on_date(self, contract_code, trading_date):
                return SimpleNamespace(close_price=5.0, settle_price=5.0, open_price=5.0)

        engine = FuturesExecutionEngine(
            {
                "execution": {
                    "forced_risk": {
                        "enabled": True,
                        "intraday_margin_call_ratio": 0.80,
                        "post_reduce_target_margin_ratio": 0.50,
                    },
                    "dynamic_margin": {"enabled": False},
                }
            },
            FakeDb(),
        )
        engine.router = FakeRouter()
        portfolio = Portfolio(
            id="pf-risk",
            cashflow=1000.0,
            margin_used=1000.0,
            positions={
                "RB": Position(
                    shares=10,
                    entry_price=100.0,
                    contract_code="rb2505",
                    contract_multiplier=10,
                    margin_rate=0.1,
                    margin_used=1000.0,
                )
            },
        )

        created = engine.scan_and_create_intraday_forced_risk_orders(
            config_id="cfg",
            trading_date="2025-03-04",
            portfolio=portfolio,
        )

        self.assertEqual(created, ["forced-risk-1"])
        self.assertEqual(saved[0]["source_type"], "forced_risk")
        self.assertEqual(saved[0]["action"], "close_long")
        self.assertEqual(saved[0]["effective_trade_date"], "2025-03-04")
        self.assertGreater(saved[0]["lots"], 0)
        self.assertNotIn("final_action_contract", saved[0].get("signal_snapshot") or {})
        self.assertEqual(
            saved[0]["audit_payload"]["strategy_learning_boundary"],
            "excluded_from_strategy_action_value",
        )

    def test_intraday_margin_below_call_does_not_create_forced_risk_order(self):
        saved = []

        class FakeDb:
            def get_futures_recommendations_by_effective_date(self, **kwargs):
                return []

            def save_futures_recommendation(self, recommendation):
                saved.append(recommendation.model_dump())
                return "forced-risk-1"

        class FakeRouter:
            def get_china_futures_minute_bars(self, **kwargs):
                return [
                    {"datetime": "2025-03-04 10:00:00", "open": 100.0, "close": 100.0, "volume": 10},
                ]

            def get_futures_contract_quote_on_date(self, contract_code, trading_date):
                return SimpleNamespace(close_price=100.0, settle_price=100.0, open_price=100.0)

        engine = FuturesExecutionEngine(
            {
                "execution": {
                    "forced_risk": {
                        "enabled": True,
                        "intraday_margin_call_ratio": 0.80,
                        "post_reduce_target_margin_ratio": 0.50,
                    },
                    "dynamic_margin": {"enabled": False},
                }
            },
            FakeDb(),
        )
        engine.router = FakeRouter()
        portfolio = Portfolio(
            id="pf-safe",
            cashflow=100000.0,
            margin_used=10000.0,
            positions={
                "RB": Position(
                    shares=10,
                    entry_price=100.0,
                    contract_code="rb2505",
                    contract_multiplier=10,
                    margin_rate=0.1,
                    margin_used=10000.0,
                )
            },
        )

        created = engine.scan_and_create_intraday_forced_risk_orders(
            config_id="cfg",
            trading_date="2025-03-04",
            portfolio=portfolio,
        )

        self.assertEqual(created, [])
        self.assertEqual(saved, [])


class EvaluationRegressionTest(unittest.TestCase):
    def _create_minimal_futures_evaluation_db(self, db_path: str):
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE portfolio (
                id TEXT,
                config_id TEXT,
                updated_at TEXT,
                trading_date TEXT,
                cashflow REAL,
                total_assets REAL,
                positions TEXT,
                margin_used REAL,
                leverage REAL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE daily_settlement (
                portfolio_id TEXT,
                trading_date TEXT,
                previous_balance REAL,
                current_balance REAL,
                previous_margin REAL,
                current_margin REAL,
                daily_pnl REAL,
                commission REAL,
                margin_ratio REAL,
                is_warning INTEGER,
                is_liquidation INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE futures_transactions (
                config_id TEXT,
                trading_date TEXT,
                created_at TEXT,
                ticker TEXT,
                contract_code TEXT,
                action TEXT,
                lots INTEGER,
                execution_price REAL,
                price REAL,
                contract_multiplier REAL,
                commission REAL,
                settle_price REAL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE ticker_daily_pnl (
                portfolio_id TEXT,
                trading_date TEXT,
                ticker TEXT,
                daily_pnl REAL,
                commission REAL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE capital_deployment_state (
                config_id TEXT,
                trading_date TEXT,
                capital_allocation_tier TEXT,
                reason_bucket TEXT
            )
            """
        )
        cur.execute(
            "CREATE TABLE strategy_memory (config_id TEXT, memory_state TEXT, net_pnl REAL)"
        )
        cur.execute(
            "CREATE TABLE config_learning_overlay (config_id TEXT, active INTEGER)"
        )
        cur.execute(
            "CREATE TABLE causal_review_candidate (config_id TEXT)"
        )
        cur.execute(
            "CREATE TABLE futures_recommendation (config_id TEXT, signal_snapshot TEXT)"
        )

        positions = {"M": {"shares": 1, "value": 100000.0, "contract_code": "m2601"}}
        portfolios = [
            ("p1", "cfg", "2025-01-02T15:30:00", "2025-01-02", 4999900.0, 5009900.0, json.dumps(positions), 10000.0, 1.0),
            ("p2", "cfg", "2025-01-03T15:30:00", "2025-01-03", 5006500.0, 5016500.0, json.dumps(positions), 10000.0, 1.0),
        ]
        cur.executemany("INSERT INTO portfolio VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", portfolios)
        settlements = [
            ("p1", "2025-01-02", 5000000.0, 4996625.68, 0.0, 3311.0, -50.0, 13.32, 0.0007, 0, 0),
            ("p2", "2025-01-03", 4996625.68, 4993229.93, 3311.0, 13301.0, 6660.0, 66.75, 0.0027, 0, 0),
        ]
        cur.executemany("INSERT INTO daily_settlement VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", settlements)
        transactions = [
            ("cfg", "2025-01-02", "2025-01-02T09:00:00", "M", "m2601", "open_long", 1, 100.0, 100.0, 10.0, 2.0, 100.0),
            ("cfg", "2025-01-03", "2025-01-03T09:00:00", "RB", "rb2601", "open_short", 2, 200.0, 200.0, 10.0, 4.0, 200.0),
        ]
        cur.executemany("INSERT INTO futures_transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", transactions)
        ticker_pnl = [
            ("p1", "2025-01-02", "M", -50.0, 2.0),
            ("p2", "2025-01-03", "M", 100.0, 2.0),
            ("p2", "2025-01-03", "RB", -20.0, 4.0),
        ]
        cur.executemany("INSERT INTO ticker_daily_pnl VALUES (?, ?, ?, ?, ?)", ticker_pnl)
        conn.commit()
        return conn

    def test_futures_transaction_win_rate_uses_completed_round_trips(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE futures_transactions (
                    config_id TEXT,
                    trading_date TEXT,
                    created_at TEXT,
                    ticker TEXT,
                    contract_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    execution_price REAL,
                    price REAL,
                    contract_multiplier REAL,
                    commission REAL
                )
                """
            )
            rows = [
                ("cfg", "2025-10-13", "2025-10-13T09:00:00", "M", "m2601", "open_long", 2, 100.0, 100.0, 10.0, 2.0),
                ("cfg", "2025-10-14", "2025-10-14T09:00:00", "M", "m2601", "close_long", 1, 110.0, 110.0, 10.0, 1.0),
                ("cfg", "2025-10-15", "2025-10-15T09:00:00", "M", "m2601", "close_long", 1, 90.0, 90.0, 10.0, 1.0),
                ("cfg", "2025-10-16", "2025-10-16T09:00:00", "RB", "rb2601", "open_short", 1, 200.0, 200.0, 10.0, 2.0),
                ("cfg", "2025-10-17", "2025-10-17T09:00:00", "RB", "rb2601", "close_short", 1, 180.0, 180.0, 10.0, 2.0),
            ]
            cur.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            conn.close()

            metrics = calculate_futures_transaction_win_rate("cfg", db_path)
            self.assertEqual(metrics["total_trades"], 3)
            self.assertEqual(metrics["winning_trades"], 2)
            self.assertEqual(metrics["losing_trades"], 1)
            self.assertEqual(metrics["flat_trades"], 0)
            self.assertAlmostEqual(metrics["win_rate"], 2 / 3)
            self.assertAlmostEqual(metrics["realized_trade_pnl"], 192.0)
        finally:
            os.remove(db_path)

    def test_subwindow_transaction_win_rate_classifies_inherited_closes(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE futures_transactions (
                    config_id TEXT,
                    trading_date TEXT,
                    created_at TEXT,
                    ticker TEXT,
                    contract_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    execution_price REAL,
                    price REAL,
                    contract_multiplier REAL,
                    commission REAL
                )
                """
            )
            rows = [
                ("cfg", "2025-10-10", "2025-10-10T09:00:00", "M", "m2601", "open_long", 2, 100.0, 100.0, 10.0, 2.0),
                ("cfg", "2025-10-14", "2025-10-14T09:00:00", "M", "m2601", "close_long", 2, 90.0, 90.0, 10.0, 2.0),
            ]
            cur.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            conn.close()

            metrics = calculate_futures_transaction_win_rate("cfg", db_path, start_date="2025-10-13")
            self.assertEqual(metrics["total_trades"], 0)
            self.assertEqual(metrics["unmatched_close_lots"], 2)
            self.assertEqual(metrics["inherited_close_lots"], 2)
        finally:
            os.remove(db_path)

    def test_average_leverage_uses_account_equity_not_total_assets(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE portfolio (
                    id TEXT,
                    config_id TEXT,
                    trading_date TEXT,
                    cashflow REAL,
                    total_assets REAL,
                    positions TEXT,
                    margin_used REAL,
                    leverage REAL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE daily_settlement (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    daily_pnl REAL,
                    margin_ratio REAL,
                    is_warning INTEGER,
                    is_liquidation INTEGER,
                    commission REAL,
                    previous_margin REAL,
                    current_margin REAL,
                    previous_balance REAL,
                    current_balance REAL
                )
                """
            )
            positions = {
                "M": {"value": 500.0, "shares": 1},
                "RB": {"value": 300.0, "shares": -1},
            }
            cur.execute(
                """
                INSERT INTO portfolio
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("p1", "cfg", "2025-10-13", 1000.0, 1800.0, json.dumps(positions), 200.0, 1.0),
            )
            cur.execute(
                """
                INSERT INTO daily_settlement
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("p1", "2025-10-13", 50.0, 0.10, 0, 0, 3.0, 100.0, 200.0, 1050.0, 997.0),
            )
            conn.commit()
            conn.close()

            metrics = calculate_futures_metrics("cfg", db_path)
            self.assertAlmostEqual(metrics["avg_leverage"], 800.0 / 1200.0)
        finally:
            os.remove(db_path)

    def test_futures_evaluation_uses_account_equity_for_risk_metrics(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = self._create_minimal_futures_evaluation_db(db_path)
            conn.close()

            metrics = evaluate_config("cfg", db_path)
            self.assertIsNotNone(metrics)
            self.assertEqual(metrics["risk_metric_status"], "ok")
            self.assertEqual(metrics["account_equity_return_sample_count"], 2)
            self.assertGreater(metrics["volatility"], 0.0)
            self.assertNotEqual(metrics["sharpe_ratio"], 0.0)
            self.assertGreater(metrics["account_equity_max_drawdown"], 0.0)
            self.assertEqual(metrics["margin_return_sample_count"], 1)
            self.assertEqual(metrics["margin_return_volatility"], 0.0)
        finally:
            os.remove(db_path)

    def test_no_completed_round_trips_warns_without_low_win_rate(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = self._create_minimal_futures_evaluation_db(db_path)
            conn.close()

            metrics = evaluate_config("cfg", db_path)
            warning_types = {warning["type"] for warning in metrics["warnings"]}
            self.assertFalse(metrics["win_rate_available"])
            self.assertIn("no_completed_round_trips", warning_types)
            self.assertNotIn("low_win_rate", warning_types)
        finally:
            os.remove(db_path)

    def test_commission_rate_uses_turnover_not_initial_capital(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = self._create_minimal_futures_evaluation_db(db_path)
            conn.close()

            metrics = evaluate_config("cfg", db_path)
            self.assertAlmostEqual(metrics["total_turnover_notional"], 5000.0)
            self.assertAlmostEqual(metrics["commission_rate"], 80.07 / 5000.0)
            self.assertAlmostEqual(metrics["capital_commission_rate"], 80.07 / 5000000.0)
        finally:
            os.remove(db_path)

    def test_futures_evaluation_reports_strategy_quality_metrics(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = self._create_minimal_futures_evaluation_db(db_path)
            conn.executemany(
                "INSERT INTO futures_transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("cfg", "2025-01-03", "2025-01-03T10:00:00", "M", "m2601", "close_long", 1, 110.0, 110.0, 10.0, 2.0, 110.0),
                    ("cfg", "2025-01-03", "2025-01-03T11:00:00", "RB", "rb2601", "close_short", 2, 201.0, 201.0, 10.0, 4.0, 201.0),
                ],
            )
            conn.commit()
            conn.close()

            metrics = evaluate_config("cfg", db_path)
            self.assertGreater(metrics["calmar_ratio"], 0.0)
            self.assertGreater(metrics["profit_factor"], 0.0)
            self.assertGreater(metrics["trade_expectancy"], 0.0)
            self.assertEqual(metrics["max_consecutive_losing_days"], 1)
            self.assertEqual(metrics["profitable_ticker_count"], 1)
            self.assertEqual(metrics["losing_ticker_count"], 1)
            self.assertEqual(metrics["top_profit_ticker"], "M")
            self.assertEqual(metrics["worst_loss_ticker"], "RB")
            expected_margin_returns = [
                (-50.0 - 13.32) / ((0.0 + 3311.0) / 2.0),
                (6660.0 - 66.75) / ((3311.0 + 13301.0) / 2.0),
            ]
            self.assertAlmostEqual(
                metrics["return_on_avg_margin"],
                sum(expected_margin_returns) / len(expected_margin_returns),
            )
            self.assertAlmostEqual(
                metrics["commission_drag_ratio"],
                80.07 / (abs(-50.0) + abs(6660.0)),
            )
        finally:
            os.remove(db_path)

    def test_evaluation_helper_upgrades_config_outcome_without_dropping_rows(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
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
                    margin_call_count INTEGER DEFAULT 0
                )
                """
            )
            cur.execute(
                "INSERT INTO config_outcome (id, config_id, total_return) VALUES (?, ?, ?)",
                ("old", "cfg", 0.01),
            )
            conn.commit()
            conn.close()

            EvaluationHelper(db_path)

            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM config_outcome WHERE id='old'")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("PRAGMA table_info(config_outcome)")
            columns = {row[1] for row in cur.fetchall()}
            self.assertIn("profit_factor", columns)
            self.assertIn("learning_context_budget_rows", columns)
            conn.close()
        finally:
            os.remove(db_path)

    def test_capital_deployment_under_deployed_counts_by_tier(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = self._create_minimal_futures_evaluation_db(db_path)
            cur = conn.cursor()
            cur.executemany(
                "INSERT INTO capital_deployment_state VALUES (?, ?, ?, ?)",
                [
                    ("cfg", "2025-01-02", "under_deployed", "neutral_signal_no_trade"),
                    ("cfg", "2025-01-03", "under_deployed", "intraday_trigger_not_met"),
                    ("cfg", "2025-01-04", "normal", "alpha_capacity_limited"),
                ],
            )
            conn.commit()
            conn.close()

            metrics = calculate_optimization_acceptance_metrics(
                "cfg", db_path, start_date="2025-01-02", end_date="2025-01-03"
            )
            self.assertEqual(metrics["under_deployed_days"], 2)
            self.assertEqual(metrics["system_under_deployed_days"], 2)
            self.assertEqual(metrics["non_alpha_under_deployed_days"], 2)
            self.assertEqual(metrics["under_deployed_reason_counts"]["neutral_signal_no_trade"], 1)
            self.assertEqual(metrics["under_deployed_reason_counts"]["intraday_trigger_not_met"], 1)
            self.assertEqual(metrics["alpha_capacity_limited_days"], 0)
        finally:
            os.remove(db_path)


if __name__ == "__main__":
    unittest.main()

