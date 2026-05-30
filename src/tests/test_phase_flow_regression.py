import json
import os
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

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
from graph.schema import AnalystSignal, FuturesAction, FuturesDecision, FuturesTransaction, Portfolio
from agents.decision_team.portfolio_manager import (
    RiskLevel,
    _apply_capital_utilization_control,
    _apply_drawdown_and_ticker_loss_control,
    _apply_holding_rebalance_control,
    _build_phase1_recommendation,
)
from run.order import _reconcile_rollover_with_strategy_target, _translate_pre_open_recommendation_to_order
from tools.agent_tools.execution.futures_execution import FuturesExecutionEngine
from tools.agent_tools.execution.futures_settlement import FuturesDailySettlement
from tools.agent_tools.execution.intraday_execution import select_intraday_execution
from tools.agent_tools.research.reviewer_tools import (
    _apply_net_exposure_review,
    _build_daily_transaction_report,
    _build_capital_deployment_diagnostics,
    _collect_recommendation_quality_warnings,
    _validate_recommendation_execution_audit,
)
from util.futures_audit import (
    calculate_margin_audit,
    categorize_no_trade_reason,
    classify_zero_transaction_day,
    classify_no_trade_reasons,
    infer_no_trade_reason,
    normalize_no_trade_reason,
)
from util.futures_trade_pairs import build_completed_trade_pairs, summarize_trade_pairs
from util.trading_calendar import get_previous_trading_day, map_datetime_to_futures_trading_day
from run.validate_phase_flow import _expected_settlement_balance_change
from tools.agent_tools.research.reviewer_tools import _expected_settlement_equity_change
from tools.agent_tools.execution.trader_exit_policy import evaluate_exit_policy
from tools.agent_tools.execution.order_semantics import (
    build_lot_intent_consistency,
    phase2_order_intent_from_lots,
    recommendation_intent_from_lots,
)


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


class Phase1RecommendationSnapshotRegressionTest(unittest.TestCase):
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


class _FailingPreviousCloseAPI:
    def get_futures_daily_candles_optimized(self, underlying_code, is_main, start_date, end_date):
        return [SimpleNamespace(trade_date="2025-03-07")]

    def get_main_contract_quote_on_date(self, underlying_code, trading_date):
        raise RuntimeError("Network error: refused")


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


class _FailingSettlementRouter:
    def get_futures_contract_quote_on_date(self, contract_code, trading_date):
        raise RuntimeError("HTTP 403 provider blocked")


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


class FuturesAuditRegressionTest(unittest.TestCase):
    def test_classify_no_trade_reasons(self):
        self.assertEqual(classify_no_trade_reasons(["llm_neutral", "position_matched"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["position_matched", "cooling_period"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["cold_start_small_cap"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["trade_frequency_control", "weak_signal_combo"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["market_confirmation_quality_gate"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["weak_ticker_side_quality_gate"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["news_only_directional_trade"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["strategy_memory_weak_block"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["decision_planner_block"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["intraday_opening_range_incomplete"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["llm_neutral", "missing_previous_close"]), "error")
        self.assertEqual(classify_no_trade_reasons([]), "unknown")
        self.assertEqual(classify_no_trade_reasons(["intraday_trigger_not_met"]), "expected")

    def test_no_trade_reason_research_categories(self):
        self.assertEqual(categorize_no_trade_reason("llm_neutral")["category"], "signal")
        self.assertEqual(categorize_no_trade_reason("drawdown_control")["category"], "risk")
        self.assertEqual(categorize_no_trade_reason("intraday_trigger_not_met")["category"], "timing")
        self.assertEqual(categorize_no_trade_reason("limit_locked_no_fill")["category"], "execution")
        self.assertEqual(categorize_no_trade_reason("position_matched")["category"], "business")
        self.assertEqual(categorize_no_trade_reason("strategy_memory_weak_block")["category"], "learning")

    def test_infer_no_trade_reason_from_warning(self):
        snapshot = {"pre_open_plan": {"tradable_lots_reason": "position_matched"}}
        self.assertEqual(infer_no_trade_reason(snapshot), "position_matched")
        self.assertEqual(
            infer_no_trade_reason({}, warning_message="RB has no previous close available before 2025-02-05"),
            "missing_previous_close",
        )

    def test_legacy_decision_planner_reason_is_canonicalized(self):
        self.assertEqual(normalize_no_trade_reason("decision_planner_block"), "trade_auditor_block")
        snapshot = {"pre_open_plan": {"tradable_lots_reason": "decision_planner_block"}}
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

    def test_severe_ticker_side_performance_blocks_new_exposure(self):
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

        self.assertEqual(output.decision, "block")
        self.assertIn("side_performance_block", output.reasons)

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
        self.assertIn("protected_memory_evidence_rejected", output.reasons)
        self.assertIn("cold_start_weak_combo_block", output.reasons)

    def test_weak_ticker_side_rule_blocks_latest_bad_p_long_template(self):
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

        self.assertEqual(output.decision, "block")
        self.assertIn("weak_ticker_side_quality_gate", output.reasons)

    def test_news_only_directional_trade_blocks_when_core_opposes(self):
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

        self.assertEqual(output.decision, "block")
        self.assertIn("news_only_directional_trade", output.reasons)

    def test_strategy_memory_weak_block_blocks_new_exposure(self):
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

        self.assertEqual(output.decision, "block")
        self.assertIn("strategy_memory_weak_block", output.reasons)

    def test_contextual_calibration_can_soften_same_scope_auditor_history_block_to_probe(self):
        config = self._auditor().full_config
        config["learning"] = {
            "contextual_rule_calibration": {
                "enabled": True,
                "min_confidence": 0.35,
                "softenable_hard_block_reasons": ["side_performance_block"],
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
                        "signal_template": "*",
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
        self.assertIn("soft_block_converted_to_probe_only", output.reasons)
        self.assertEqual(
            output.diagnostics["contextual_rule_calibration"]["softened_reasons"],
            ["side_performance_block"],
        )


class ValidationRegressionTest(unittest.TestCase):
    def test_zero_transaction_day_classification_uses_recommendation_audit(self):
        recommendations = [
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "llm_neutral"}},
            },
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "position_matched"}},
            },
        ]
        result = classify_zero_transaction_day(recommendations)
        self.assertEqual(result["classification"], "expected")
        self.assertEqual(sorted(result["reasons"]), ["llm_neutral", "position_matched"])
        self.assertEqual(result["reason_categories"], {"signal": 1, "business": 1})

    def test_zero_transaction_day_allows_horizon_timing_gate(self):
        recommendations = [
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "llm_neutral"}},
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
                "signal_snapshot": {"execution_result": {"no_trade_reason": "llm_neutral"}},
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
                    "pre_open_plan": {
                        "market_confirmation": {
                            "data_missing": ["contract_rank"],
                            "feature_status": {"contract_rank": "parameter_error"},
                            "parameter_errors": ["contract_rank"],
                            "data_status_groups": {"parameter_error": ["contract_rank"]},
                        }
                    }
                },
            },
            {
                "underlying_code": "RB",
                "signal_snapshot": {
                    "pre_open_plan": {
                        "market_confirmation": {
                            "data_missing": ["warehouse_receipt"],
                            "feature_status": {"warehouse_receipt": "no_data"},
                            "no_data": ["warehouse_receipt"],
                            "data_status_groups": {"no_data": ["warehouse_receipt"]},
                        }
                    }
                },
            },
            {
                "underlying_code": "M",
                "signal_snapshot": {
                    "pre_open_plan": {
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
                    "pre_open_plan": {
                        "target_position_ratio": 0.05,
                        "current_ticker_exposure": 0.02,
                        "target_lots_estimate": 5,
                        "current_lots_before_open": 2,
                        "tradable_lots_if_executed_now": 3,
                        "tradable_lots_reason": "position_matched",
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
                    "execution_result": {"no_trade_reason": "position_matched"},
                },
            },
            {
                "underlying_code": "RB",
                "signal_snapshot": {
                    "pre_open_plan": {
                        "target_position_ratio": 0.04,
                        "target_lots_estimate": 4,
                        "tradable_lots_reason": "intraday_trigger_not_met",
                        "market_confirmation": {"confirmation_score": 0.66},
                    },
                    "execution_result": {"no_trade_reason": "intraday_trigger_not_met"},
                },
            },
            {
                "underlying_code": "M",
                "signal_snapshot": {
                    "pre_open_plan": {
                        "target_position_ratio": -0.03,
                        "target_lots_estimate": -3,
                        "tradable_lots_reason": "trade_auditor_block",
                        "market_confirmation": {"confirmation_score": 0.40},
                        "trade_auditor": {
                            "decision": "block",
                            "reasons": ["analyst_quality_low_tradeability"],
                        },
                    },
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
                    "pre_open_plan": {
                        "target_position_ratio": 0.003,
                        "current_ticker_exposure": 0.0,
                        "target_lots_estimate": 1,
                        "tradable_lots_if_executed_now": 1,
                        "tradable_lots_reason": "minimum_new_entry_threshold",
                        "signal_confidence": 0.72,
                        "rebalance_summary": {
                            "action_type": "new_entry",
                            "control_reasons": ["minimum_new_entry_threshold"],
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
                    "signal_template": "long_reversal_confirmed_short",
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
                    template_name="reversal_confirmed",
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


class HoldingLifecycleRegressionTest(unittest.TestCase):
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

    def test_medium_horizon_new_entry_needs_short_timing_confirmation(self):
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

        self.assertEqual(ratio, 0.0)
        self.assertIn("horizon_consistency_requires_short_timing", reasons)
        self.assertEqual(
            diagnostics["holding_rebalance_control"]["decision"],
            "skip_horizon_mismatch_new_entry",
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
                phase4_message_override="reviewer validation and researcher learning passed",
            )
        finally:
            conn.close()

        for section in (
            "一、账户总览",
            "二、当日交易执行",
            "三、交易原因详述",
            "四、未交易品种原因详述",
            "五、信号汇总",
            "六、系统决策流程",
            "七、收盘持仓",
            "八、当日关键特征",
            "追溯信息",
        ):
            self.assertIn(section, text)
        self.assertIn("phase4             completed", text)
        self.assertIn("reviewer validation and researcher learning passed", text)
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
                "pre_open_plan": {
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
                "recovery_probe_margin_ratio_max": 0.02,
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
        self.assertTrue(diagnostics["drawdown_control"]["shadow_recommendation"])

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
        self.assertFalse(diagnostics["drawdown_control"]["shadow_recommendation"])


class IntradayExecutionRegressionTest(unittest.TestCase):
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

    def test_protected_confirmed_memory_can_use_vwap_fallback_without_breakout(self):
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
            market_confirmation={
                "confirmation_score": 0.75,
                "confirmations": ["basis", "position_rank", "net_cap"],
            },
            strategy_memory={
                "side_memory": {
                    "memory_state": "protected",
                    "sample_count": 5,
                    "win_rate": 1.0,
                    "net_pnl": 9000,
                }
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.reason, "intraday_confirmed_memory_vwap_fallback")
        self.assertEqual(result.features["execution_mode"], "confirmed_memory_vwap_fallback")
        self.assertTrue(result.features["strategy_memory"]["passed"])

    def test_vwap_fallback_does_not_apply_without_high_quality_memory(self):
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
                "allow_confirmed_memory_vwap_fallback": True,
                "confirmed_memory_min_market_confirmation_score": 0.70,
                "confirmed_memory_min_confirmations": 3,
                "confirmed_memory_max_opening_range_miss": 0.002,
            },
            market_confirmation={
                "confirmation_score": 0.75,
                "confirmations": ["basis", "position_rank", "net_cap"],
            },
            strategy_memory={
                "side_memory": {
                    "memory_state": "watchlist",
                    "sample_count": 4,
                    "win_rate": 0.25,
                    "net_pnl": -5000,
                }
            },
        )

        self.assertFalse(result.should_execute)
        self.assertEqual(result.reason, "intraday_trigger_not_met")

    def test_vwap_fallback_does_not_apply_with_small_sample_protected_memory(self):
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
                "allow_confirmed_memory_vwap_fallback": True,
                "confirmed_memory_min_market_confirmation_score": 0.70,
                "confirmed_memory_min_confirmations": 3,
                "confirmed_memory_max_opening_range_miss": 0.002,
                "confirmed_memory_min_sample_count": 5,
            },
            market_confirmation={
                "confirmation_score": 0.75,
                "confirmations": ["basis", "position_rank", "net_cap"],
            },
            strategy_memory={
                "side_memory": {
                    "memory_state": "protected",
                    "sample_count": 3,
                    "win_rate": 1.0,
                    "net_pnl": 9000,
                }
            },
        )

        self.assertFalse(result.should_execute)
        self.assertEqual(result.reason, "intraday_trigger_not_met")

    def test_contextual_calibration_can_relax_intraday_memory_fallback_boundary(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 99, "close": 100.70, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 100.75, "high": 101, "low": 100, "close": 100.75, "volume": 10},
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
            market_confirmation={
                "confirmation_score": 0.68,
                "confirmations": ["basis", "position_rank", "net_cap"],
            },
            strategy_memory={
                "side_memory": {
                    "memory_state": "protected",
                    "sample_count": 6,
                    "win_rate": 0.75,
                    "net_pnl": 9000,
                }
            },
            adaptive_policy_state=[
                {
                    "id": "cal-intraday",
                    "ticker": "BU",
                    "side": "long",
                    "signal_template": "*",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "policy_type": "contextual_rule_calibration:intraday_confirmation",
                    "policy_action": "calibrate",
                    "confidence_score": 0.55,
                    "sample_count": 2,
                    "payload": {
                        "rule_adjustments": {
                            "intraday_confirmation": {
                                "confirmed_memory_max_opening_range_miss": 0.0035,
                                "confirmed_memory_min_market_confirmation_score": 0.65,
                            }
                        }
                    },
                }
            ],
            decision_context={"ticker": "BU", "horizon_class": "short", "market_regime": "trend"},
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.reason, "intraday_confirmed_memory_vwap_fallback")
        self.assertEqual(
            result.features["contextual_rule_calibration"]["applied"][0]["id"],
            "cal-intraday",
        )


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


class SettlementAccountingRegressionTest(unittest.TestCase):
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
                "pre_open_plan": {
                    "target_position_ratio": -0.12,
                    "target_lots_estimate": -26,
                }
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
        self.assertIn(
            "phase1_target_lots_cap",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )

    def test_phase2_blocks_open_when_signal_invalidation_level_is_breached(self):
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
            "pre_open_plan": {
                "target_position_ratio": -0.12,
                "target_lots_estimate": -26,
            },
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
        self.assertEqual(decision.lots, 0)
        self.assertIn("signal_invalidation_level", translation.get("rewrite_reasons", []))
        self.assertEqual(translation["phase2_order_plan"]["signal_lifecycle"]["invalidation_level"], 4400.0)

    def test_time_stop_does_not_flatten_supported_same_direction_hold(self):
        current_position = SimpleNamespace(entry_date="2025-02-10", entry_price=3000.0)

        result = evaluate_exit_policy(
            ticker="M",
            current_price=3020.0,
            current_lots=10,
            target_lots=12,
            lifecycle={"template_state": "protected", "template_name": "trend_follow"},
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
            lifecycle={"template_state": "watchlist", "template_name": "probe"},
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
                "pre_open_plan": {
                    "target_position_ratio": -1.62,
                    "target_margin_ratio_estimate": 0.162,
                    "target_lots_estimate": -162,
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
            snapshot["execution_translation"]["dynamic_opportunity_budget_control"]["mode"],
            "validated_with_stop",
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
            "signal_snapshot": {"pre_open_plan": {"target_lots_estimate": 0}},
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
                    ("cfg", "2025-01-02", "under_deployed", "llm_neutral"),
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
            self.assertEqual(metrics["under_deployed_reason_counts"]["llm_neutral"], 1)
            self.assertEqual(metrics["under_deployed_reason_counts"]["intraday_trigger_not_met"], 1)
            self.assertEqual(metrics["alpha_capacity_limited_days"], 0)
        finally:
            os.remove(db_path)


if __name__ == "__main__":
    unittest.main()
