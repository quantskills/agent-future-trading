import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evaluation import calculate_futures_metrics, calculate_futures_transaction_win_rate
from agents.auditor import TradeAuditor, TradeAuditorInput
from database.sqlite_helper import SQLiteDB
from graph.schema import FuturesAction, Portfolio
from run.order import _reconcile_rollover_with_strategy_target, _translate_pre_open_recommendation_to_order
from tools.agent_tools.intraday_execution import select_intraday_execution
from util.futures_audit import (
    calculate_margin_audit,
    classify_zero_transaction_day,
    classify_no_trade_reasons,
    infer_no_trade_reason,
    normalize_no_trade_reason,
)
from util.futures_trade_pairs import build_completed_trade_pairs, summarize_trade_pairs
from util.trading_calendar import get_previous_trading_day
from run.validate_phase_flow import _expected_settlement_balance_change


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

        self.assertEqual(output.decision, "reduce")
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

        self.assertEqual(output.decision, "reduce")
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

        self.assertEqual(output.decision, "reduce")
        self.assertIn("protected_ticker_side_cold_start", output.reasons)
        self.assertNotEqual(output.position_ratio_multiplier, 0.0)

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


if __name__ == "__main__":
    unittest.main()
