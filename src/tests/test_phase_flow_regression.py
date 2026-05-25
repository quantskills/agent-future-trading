import json
import os
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
from evaluation.evaluation import calculate_optimization_acceptance_metrics
from agents.auditor import TradeAuditor, TradeAuditorInput
from apis.router import Router
from database.sqlite_helper import SQLiteDB
from graph.constants import Signal
from graph.schema import AnalystSignal, FuturesAction, FuturesDecision, Portfolio
from agents.portfolio_manager import _build_phase1_recommendation
from run.order import _reconcile_rollover_with_strategy_target, _translate_pre_open_recommendation_to_order
from tools.agent_tools.futures_settlement import FuturesDailySettlement
from tools.agent_tools.intraday_execution import select_intraday_execution
from tools.agent_tools.reviewer_tools import (
    _apply_net_exposure_review,
    _build_capital_deployment_diagnostics,
    _collect_recommendation_quality_warnings,
    _validate_recommendation_execution_audit,
)
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
from tools.agent_tools.trader_exit_policy import evaluate_exit_policy


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


class Phase1RecommendationSnapshotRegressionTest(unittest.TestCase):
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

    def test_net_exposure_review_uses_dynamic_strong_opportunity_cap(self):
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
                                    "mode": "strong_opportunity",
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
        self.assertIn("dynamic strong-opportunity cap", warnings[0])

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
                        "market_confirmation": {"confirmation_score": 0.72},
                        "strategy_controls": {
                            "diagnostics": {
                                "capital_utilization_learning": {
                                    "protected_memory": {"memory_state": "protected"}
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
        self.assertEqual(diagnostics["execution_gate_candidates"][0]["ticker"], "RB")
        self.assertEqual(diagnostics["auditor_suppression_cases"][0]["ticker"], "M")
        self.assertEqual(
            diagnostics["parameter_review"][0]["scope"],
            "execution.intraday_confirmation",
        )


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
            },
            market_confirmation={
                "confirmation_score": 0.75,
                "confirmations": ["basis", "position_rank", "net_cap"],
            },
            strategy_memory={
                "side_memory": {
                    "memory_state": "protected",
                    "sample_count": 4,
                    "win_rate": 1.0,
                    "net_pnl": 9000,
                }
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.reason, "intraday_confirmed_memory_vwap_fallback")
        self.assertEqual(result.features["execution_mode"], "confirmed_memory_vwap_fallback")

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
                                "target_mode": "strong_opportunity",
                                "high_quality_memory": True,
                                "dynamic_allocation_tier": "validated_with_stop",
                                "dynamic_opportunity_margin_ratio_budget": 0.162,
                                "dynamic_opportunity_margin_ratio_cap": 0.162,
                                "stop_protected": True,
                            },
                            "net_exposure_control": {
                                "cap_mode": "strong_opportunity",
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
