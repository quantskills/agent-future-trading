import inspect
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.analysis_team import commodity_news
from agents.analysis_team.commodity_news import _build_no_news_signal
from agents.analysis_team.fundamental import _build_no_fundamental_data_signal
from agents.analysis_team.technical import calculate_raw_atr14
from graph.constants import Signal
from graph.schema import AnalystSignal
from llm.prompt import (
    build_futures_commodity_news_prompt,
    build_futures_fundamental_prompt,
    build_futures_technical_prompt,
)
from tools.agent_tools.analysis.analyst_output_finalization import (
    build_required_market_data_unavailable_signal,
    finalize_analyst_signal,
    resolve_analyst_llm_config,
)
from tools.agent_tools.analysis.analyst_structured_output import (
    CommodityNewsAnalystOutput,
    FundamentalAnalystOutput,
    TechnicalAnalystOutput,
)
from tools.agent_tools.analysis.analyst_data_usage import (
    resolve_technical_data_freshness,
)
from tools.agent_tools.analysis.analyst_product_price_behavior_profile import (
    apply_profile_usage_to_signal,
    build_profile_usage_contract,
    get_product_price_behavior_profile,
)
from tools.common.execution_trigger_semantics import (
    canonical_entry_invalidation_condition,
    canonical_entry_trigger,
)
from tools.common.signal_evidence_collection import (
    build_pm_evidence_signals_from_scc,
    build_signal_collection_contract,
    validate_action_evidence_contract,
)
from tests.contract_test_fixtures import build_test_aec, build_test_data_usage


def _finalize_data_gap(signal, *, analyst, ticker):
    profile = get_product_price_behavior_profile(ticker)
    usage = build_profile_usage_contract(ticker, analyst, profile)
    return finalize_analyst_signal(
        signal,
        quality_context={
            "sector": str(profile.get("sector") or ""),
            "tradeability": "low",
            "risk_flags": [f"{analyst}_data_unavailable"],
            "data_quality": {
                "coverage_ratio": 0.0,
                "factor_freshness_score": 0.0,
                "no_lookahead_status": "ok",
            },
        },
        full_config={"llm": {"provider": "test", "model": "test-model"}},
        analyst=analyst,
        ticker=ticker,
        trading_date="2025-03-05",
        learning_context={},
        product_profile=profile,
        product_profile_usage=usage,
    )


class AnalystFinalizationFlowTest(unittest.TestCase):
    def _finalize_directional(self, signal: AnalystSignal, *, analyst: str, ticker: str, context: dict):
        profile = get_product_price_behavior_profile(ticker)
        usage = build_profile_usage_contract(ticker, analyst, profile)
        return finalize_analyst_signal(
            signal,
            quality_context={
                "sector": profile.get("sector", ""),
                "position_invalidation_reference_price": 100.0,
                **context,
            },
            full_config={"llm": {"provider": "test", "model": "test-model"}},
            analyst=analyst,
            ticker=ticker,
            trading_date="2025-03-26",
            learning_context={},
            product_profile=profile,
            product_profile_usage=usage,
        )

    def test_technical_freshness_uses_registered_reference_date_without_calendar_guessing(self):
        self.assertEqual(
            resolve_technical_data_freshness(
                latest_data_date="2025-03-21",
                base_price_date="2025-03-21",
            ),
            (1.0, "fresh"),
        )
        self.assertEqual(
            resolve_technical_data_freshness(
                latest_data_date="2025-03-20",
                base_price_date="2025-03-21",
            ),
            (0.35, "stale"),
        )
        self.assertEqual(
            resolve_technical_data_freshness(
                latest_data_date="2025-03-24",
                base_price_date=None,
            ),
            (0.0, "unknown"),
        )
        self.assertNotIn(
            "get_previous_trading_day",
            inspect.getsource(resolve_technical_data_freshness),
        )

    def test_raw_atr14_uses_completed_ohlc_true_range_ewm(self):
        prices = pd.DataFrame(
            {
                "high": [101.0, 104.0, 106.0, 105.0, 109.0],
                "low": [98.0, 100.0, 101.0, 99.0, 103.0],
                "close": [100.0, 103.0, 102.0, 104.0, 108.0],
            },
            index=pd.bdate_range(end="2025-03-21", periods=5),
        )
        previous_close = prices["close"].shift()
        true_range = pd.concat(
            [
                prices["high"] - prices["low"],
                (prices["high"] - previous_close).abs(),
                (prices["low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        expected = float(true_range.ewm(span=14, adjust=False).mean().iloc[-1])

        self.assertAlmostEqual(calculate_raw_atr14(prices), expected, places=12)

    def test_finalization_overwrites_llm_atr_before_aec_build(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.55,
            opportunity_type="no_trade",
            opportunity_state="no_opportunity",
            setup_type="unknown",
            position_invalidation_level=92.0,
            atr_stop_distance=999.0,
            metadata={"data_usage_summary": build_test_data_usage("technical", "BU")},
        )
        finalized = self._finalize_directional(
            signal,
            analyst="technical",
            ticker="BU",
            context={
                "tradeability": "low",
                "setup_type": "no_trade",
                "setup_quality_ok": False,
                "market_regime": "range",
                "atr_stop_distance": 4.25,
                "risk_flags": [],
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["position_invalidation_level"], 92.0)
        self.assertEqual(contract["atr_stop_distance"], 4.25)
        self.assertNotIn("position_invalidation_reference_price", contract)

    def test_technical_structure_level_is_validated_against_formal_pre_open_reference(self):
        cases = (
            (Signal.BULLISH, 92.0, 92.0),
            (Signal.BULLISH, 100.0, None),
            (Signal.BULLISH, 108.0, None),
            (Signal.BEARISH, 108.0, 108.0),
            (Signal.BEARISH, 100.0, None),
            (Signal.BEARISH, 92.0, None),
        )
        for direction, position_level, expected in cases:
            with self.subTest(direction=direction.value, position_level=position_level):
                signal = AnalystSignal(
                    agent_name="technical",
                    signal=direction,
                    confidence=0.55,
                    opportunity_type="no_trade",
                    opportunity_state="no_opportunity",
                    setup_type="unknown",
                    position_invalidation_level=position_level,
                    atr_stop_distance=999.0,
                    metadata={
                        "data_usage_summary": build_test_data_usage("technical", "BU")
                    },
                )
                finalized = self._finalize_directional(
                    signal,
                    analyst="technical",
                    ticker="BU",
                    context={
                        "tradeability": "low",
                        "setup_type": "no_trade",
                        "setup_quality_ok": False,
                        "market_regime": "range",
                        "atr_stop_distance": 4.25,
                        "risk_flags": [],
                    },
                )
                contract = finalized.metadata["action_evidence_contract"]
                self.assertEqual(contract["position_invalidation_level"], expected)
                self.assertEqual(contract["atr_stop_distance"], 4.25)
                self.assertNotIn("position_invalidation_reference_price", contract)

    def test_finalization_clears_fundamental_atr_and_structure_level(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.NEUTRAL,
            confidence=0.55,
            opportunity_type="no_trade",
            opportunity_state="no_opportunity",
            setup_type="unknown",
            position_invalidation_level=92.0,
            atr_stop_distance=999.0,
            metadata={"data_usage_summary": build_test_data_usage("fundamental", "BU")},
        )
        finalized = self._finalize_directional(
            signal,
            analyst="fundamental",
            ticker="BU",
            context={
                "tradeability": "low",
                "setup_type": "no_trade",
                "setup_quality_ok": False,
                "risk_flags": [],
                "data_quality": {
                    "coverage_ratio": 0.8,
                    "factor_freshness_score": 0.8,
                },
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertIsNone(contract["position_invalidation_level"])
        self.assertIsNone(contract["atr_stop_distance"])

    def test_finalization_validates_immediate_event_structure_without_event_atr(self):
        cases = ((Signal.BULLISH, 92.0, 92.0), (Signal.BULLISH, 108.0, None))
        for direction, position_level, expected in cases:
            with self.subTest(position_level=position_level):
                signal = AnalystSignal(
                    agent_name="commodity_news",
                    signal=direction,
                    confidence=0.55,
                    opportunity_type="no_trade",
                    opportunity_state="no_opportunity",
                    setup_type="unknown",
                    position_invalidation_level=position_level,
                    atr_stop_distance=999.0,
                    metadata={
                        "data_usage_summary": build_test_data_usage(
                            "commodity_news",
                            "BU",
                        )
                    },
                )
                finalized = self._finalize_directional(
                    signal,
                    analyst="commodity_news",
                    ticker="BU",
                    context={
                        "tradeability": "low",
                        "setup_type": "no_trade",
                        "setup_quality_ok": False,
                        "risk_flags": [],
                    },
                )
                contract = finalized.metadata["action_evidence_contract"]
                self.assertEqual(contract["position_invalidation_level"], expected)
                self.assertIsNone(contract["atr_stop_distance"])

    def test_finalization_writes_deterministic_technical_freshness_to_aec(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.NEUTRAL,
            confidence=0.55,
            data_freshness="fresh",
            opportunity_type="no_trade",
            opportunity_state="no_opportunity",
            setup_type="unknown",
            metadata={"data_usage_summary": build_test_data_usage("technical", "BU")},
        )
        finalized = self._finalize_directional(
            signal,
            analyst="technical",
            ticker="BU",
            context={
                "tradeability": "low",
                "setup_type": "no_trade",
                "setup_quality_ok": False,
                "market_regime": "range",
                "risk_flags": [],
                "freshness_score": 0.35,
                "data_freshness": "stale",
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["data_freshness"], "stale")
        self.assertEqual(
            contract["fusion_evidence"]["evidence_freshness_score"],
            0.35,
        )

    def test_formal_unavailable_builder_remains_only_system_unavailable_producer(self):
        signal = build_required_market_data_unavailable_signal(
            analyst="technical",
            ticker="BU",
            trading_date="2025-03-26",
            full_config={"llm": {"provider": "test", "model": "test-model"}},
        )
        contract = signal.metadata["action_evidence_contract"]
        self.assertEqual(contract["setup_type"], "data_unavailable_no_trade")
        self.assertEqual(contract["data_freshness"], "missing")
        self.assertEqual(contract["fusion_evidence"]["evidence_freshness_score"], 0.0)
        self.assertEqual(contract["opportunity_state"], "no_opportunity")

    def test_technical_finalization_replaces_llm_trigger_prose_with_canonical_profile_trigger(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.62,
            setup_type="range_reversal_setup",
            opportunity_type="short_timing",
            opportunity_state="watch_for_trigger",
            evidence_role="entry_timing",
            entry_timing_signal="breakout",
            entry_trigger="15m bespoke model prose about support and volume",
            exit_hint="after fill, close above 104 requires position exit",
            invalidation_level=102.0,
            position_invalidation_level=104.0,
            factor_focus=["range_reversal", "volume"],
            metadata={
                "data_usage_summary": build_test_data_usage("technical", "BU"),
                "invalidation_condition": "legacy free-text must not survive",
            },
        )

        finalized = self._finalize_directional(
            signal,
            analyst="technical",
            ticker="BU",
            context={
                "tradeability": "medium",
                "setup_type": "range_reversal_setup",
                "setup_quality_ok": True,
                "market_regime": "range",
                "risk_flags": [],
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["entry_timing_signal"], "breakout")
        self.assertEqual(
            contract["entry_trigger"],
            "15分钟收盘价向下突破开盘区间下沿且低于VWAP",
        )
        self.assertEqual(contract["setup_type"], "range_reversal_setup")
        self.assertEqual(contract["opportunity_type"], "short_timing")
        self.assertEqual(contract["entry_timing_signal"], "breakout")
        self.assertEqual(contract["opportunity_state"], "watch_for_trigger")
        self.assertEqual(
            contract["invalidation_condition"],
            canonical_entry_invalidation_condition("breakout", "short"),
        )
        self.assertEqual(contract["invalidation_level"], 102.0)
        self.assertEqual(contract["position_invalidation_level"], 104.0)

    def test_direction_watchlist_remains_internal_and_cannot_replace_formal_setup(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.62,
            setup_type="trend_breakout_setup",
            opportunity_type="long_timing",
            opportunity_state="watch_for_trigger",
            evidence_role="entry_timing",
            entry_timing_signal="breakout",
            entry_trigger="",
            exit_hint="15m close below 98 invalidates the setup",
            invalidation_level=98.0,
            position_invalidation_level=97.0,
            factor_focus=["trend", "volume"],
            metadata={
                "data_usage_summary": build_test_data_usage("technical", "BU"),
                "invalidation_condition": "15m close below 98 invalidates the setup",
            },
        )

        finalized = self._finalize_directional(
            signal,
            analyst="technical",
            ticker="BU",
            context={
                "tradeability": "medium",
                "setup_type": "direction_watchlist",
                "setup_quality_ok": True,
                "market_regime": "trend",
                "risk_flags": [],
                "learning_scope": {"setup_family": "direction_watchlist"},
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(finalized.setup_type, "trend_breakout_setup")
        self.assertEqual(contract["setup_type"], "trend_breakout_setup")
        self.assertEqual(
            contract["learning_scope"]["setup_family"],
            "trend_breakout_setup",
        )
        validate_action_evidence_contract(contract, analyst="technical")

        invalid = {**contract, "setup_type": "direction_watchlist"}
        with self.assertRaisesRegex(
            ValueError,
            "action_evidence_contract_technical_setup_type_invalid",
        ):
            validate_action_evidence_contract(invalid, analyst="technical")

    def test_technical_profile_generates_canonical_trigger_before_prose_presence_check(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.62,
            setup_type="trend_breakout_setup",
            opportunity_type="short_timing",
            opportunity_state="watch_for_trigger",
            evidence_role="entry_timing",
            entry_timing_signal="breakout",
            entry_trigger="",
            exit_hint="15m close above 102 invalidates the setup",
            invalidation_level=102.0,
            factor_focus=["trend", "volume"],
            metadata={
                "data_usage_summary": build_test_data_usage("technical", "BU"),
                "invalidation_condition": "15m close above 102 invalidates the setup",
            },
        )

        finalized = self._finalize_directional(
            signal,
            analyst="technical",
            ticker="BU",
            context={
                "tradeability": "medium",
                "setup_type": "trend_breakout_setup",
                "setup_quality_ok": True,
                "market_regime": "trend",
                "risk_flags": [],
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["opportunity_state"], "watch_for_trigger")
        self.assertEqual(contract["entry_timing_signal"], "breakout")
        self.assertEqual(
            contract["entry_trigger"],
            canonical_entry_trigger("breakout", "short"),
        )
        self.assertTrue(contract["invalidation_present"])

    def test_pre_open_technical_cannot_claim_current_trigger_confirmation(self):
        signal = TechnicalAnalystOutput(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.78,
            business_quality_score=0.90,
            data_coverage_score=0.90,
            price_percentile=0.50,
            setup_type="trend_breakout_setup",
            opportunity_type="short_timing",
            opportunity_state="tradeable_candidate",
            evidence_role="entry_timing",
            entry_timing_signal="breakout",
            entry_trigger="current breakout is confirmed by price and volume",
            trigger_valid=True,
            trigger_quality_score=0.83,
            invalidation_present=True,
            invalidation_level=95.0,
            position_invalidation_level=92.0,
            exit_hint="after fill, close below 92 requires position exit",
            holding_period_hint="hold for one to two trading days",
            tradeability_reason="trend, volume, and price location align",
            factor_focus=["trend", "volume"],
            metadata={
                "data_usage_summary": build_test_data_usage("technical", "BU"),
            },
        )
        finalized = self._finalize_directional(
            signal,
            analyst="technical",
            ticker="BU",
            context={
                "tradeability": "high",
                "setup_type": "trend_breakout_setup",
                "setup_quality_ok": True,
                "market_regime": "trend",
                "freshness_score": 0.90,
                "risk_flags": [],
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["opportunity_state"], "watch_for_trigger")
        self.assertEqual(contract["entry_timing_signal"], "breakout")
        self.assertTrue(contract["invalidation_present"])
        self.assertFalse(contract["current_trigger_confirmed"])
        self.assertFalse(contract["trigger_valid"])
        self.assertEqual(contract["trigger_quality_score"], 0.0)
        self.assertEqual(finalized.trigger_quality_score, 0.0)

        scc = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-26",
            analyst_signals=[finalized],
            enabled_analysts=["technical"],
        )
        self.assertEqual(
            scc["source_contracts"][0]["action_evidence_contract"][
                "trigger_quality_score"
            ],
            0.0,
        )
        pm_evidence = build_pm_evidence_signals_from_scc(scc)
        self.assertEqual(len(pm_evidence), 1)
        self.assertEqual(pm_evidence[0].trigger_quality_score, 0.0)

        pending = signal.model_copy(
            update={
                "opportunity_state": "watch_for_trigger",
                "trigger_valid": False,
                "trigger_quality_score": 0.91,
                "entry_trigger": "wait for a breakout above resistance",
            },
            deep=True,
        )
        pending_finalized = self._finalize_directional(
            pending,
            analyst="technical",
            ticker="BU",
            context={
                "tradeability": "high",
                "setup_type": "trend_breakout_setup",
                "setup_quality_ok": True,
                "market_regime": "trend",
                "freshness_score": 0.90,
                "risk_flags": [],
            },
        )
        pending_contract = pending_finalized.metadata["action_evidence_contract"]
        self.assertEqual(pending_contract["opportunity_state"], "watch_for_trigger")
        self.assertFalse(pending_contract["current_trigger_confirmed"])
        self.assertFalse(pending_contract["trigger_valid"])
        self.assertEqual(pending_contract["trigger_quality_score"], 0.0)

    def test_entry_invalidation_level_is_validated_against_pre_open_reference(self):
        signal = TechnicalAnalystOutput(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.78,
            business_quality_score=0.90,
            data_coverage_score=0.90,
            price_percentile=0.50,
            setup_type="trend_breakout_setup",
            opportunity_type="long_timing",
            opportunity_state="watch_for_trigger",
            evidence_role="entry_timing",
            entry_timing_signal="breakout",
            entry_trigger="wait for a breakout above resistance",
            trigger_valid=False,
            invalidation_present=True,
            invalidation_level=101.0,
            position_invalidation_level=92.0,
            exit_hint="after fill, close below 92 requires position exit",
            factor_focus=["trend", "volume"],
            metadata={
                "data_usage_summary": build_test_data_usage("technical", "BU"),
            },
        )

        finalized = self._finalize_directional(
            signal,
            analyst="technical",
            ticker="BU",
            context={
                "tradeability": "high",
                "setup_type": "trend_breakout_setup",
                "setup_quality_ok": True,
                "market_regime": "trend",
                "freshness_score": 0.90,
                "risk_flags": [],
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertIsNone(contract["invalidation_level"])
        self.assertFalse(contract["invalidation_present"])
        self.assertEqual(contract["opportunity_state"], "no_opportunity")
        self.assertEqual(contract["position_invalidation_level"], 92.0)

    def test_all_technical_profiles_generate_canonical_trigger_before_presence_check(self):
        for side, signal_value in (
            ("long", Signal.BULLISH),
            ("short", Signal.BEARISH),
        ):
            for execution_profile in ("breakout", "pullback", "vwap_confirmed"):
                with self.subTest(side=side, execution_profile=execution_profile):
                    signal = AnalystSignal(
                        agent_name="technical",
                        signal=signal_value,
                        confidence=0.62,
                        setup_type="trend_breakout_setup",
                        opportunity_type=f"{side}_timing",
                        opportunity_state="watch_for_trigger",
                        evidence_role="entry_timing",
                        entry_timing_signal=execution_profile,
                        entry_trigger="",
                        exit_hint="15m close beyond the invalidation boundary",
                        invalidation_level=98.0 if side == "long" else 102.0,
                        factor_focus=["trend", "volume"],
                        metadata={
                            "data_usage_summary": build_test_data_usage("technical", "BU"),
                            "invalidation_condition": (
                                "15m close beyond the invalidation boundary"
                            ),
                        },
                    )

                    finalized = self._finalize_directional(
                        signal,
                        analyst="technical",
                        ticker="BU",
                        context={
                            "tradeability": "medium",
                            "setup_type": "trend_breakout_setup",
                            "setup_quality_ok": True,
                            "market_regime": "trend",
                            "risk_flags": [],
                        },
                    )
                    contract = finalized.metadata["action_evidence_contract"]
                    self.assertEqual(contract["opportunity_state"], "watch_for_trigger")
                    self.assertEqual(contract["entry_timing_signal"], execution_profile)
                    self.assertEqual(
                        contract["entry_trigger"],
                        canonical_entry_trigger(execution_profile, side),
                    )
                    self.assertFalse(contract["trigger_valid"])
                    self.assertFalse(contract["current_trigger_confirmed"])

    def test_technical_profile_without_invalidation_remains_no_opportunity(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.62,
            setup_type="trend_breakout_setup",
            opportunity_type="long_timing",
            opportunity_state="watch_for_trigger",
            evidence_role="entry_timing",
            entry_timing_signal="breakout",
            entry_trigger="",
            factor_focus=["trend", "volume"],
            metadata={
                "data_usage_summary": build_test_data_usage("technical", "BU"),
            },
        )

        finalized = self._finalize_directional(
            signal,
            analyst="technical",
            ticker="BU",
            context={
                "tradeability": "medium",
                "setup_type": "trend_breakout_setup",
                "setup_quality_ok": True,
                "market_regime": "trend",
                "risk_flags": [],
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["opportunity_state"], "no_opportunity")
        self.assertEqual(contract["entry_timing_signal"], "")
        self.assertEqual(contract["entry_trigger"], "")
        self.assertFalse(contract["trigger_valid"])
        self.assertFalse(contract["current_trigger_confirmed"])

    def test_technical_prompt_exposes_completed_price_levels_and_canonical_profiles(self):
        price_levels = "- Current price: 3500\n- Nearest support: 3450\n- Nearest resistance: 3560"
        prompt = build_futures_technical_prompt(
            ticker="RB",
            signal_results_compact={
                "trend": "Bearish",
                "futures_volatility": "Volatility is 18.0%",
                "turnover_value": "Turnover intensity is elevated",
            },
            price_levels=price_levels,
            deterministic_atr14=12.5,
        )

        self.assertIn(price_levels, prompt)
        self.assertIn("Volatility is 18.0%", prompt)
        self.assertIn("Turnover intensity is elevated", prompt)
        self.assertIn(canonical_entry_trigger("breakout", "long"), prompt)
        self.assertIn(canonical_entry_trigger("breakout", "short"), prompt)
        self.assertIn(
            canonical_entry_invalidation_condition("breakout", "long"),
            prompt,
        )
        self.assertIn(
            canonical_entry_invalidation_condition("breakout", "short"),
            prompt,
        )
        self.assertIn("T-day open-dependent gap analysis is expected to be unavailable", prompt)
        self.assertIn("System-computed raw ATR14 (read-only market fact): 12.500000", prompt)
        self.assertIn("do not reproduce or modify it as an output field", prompt)

    def test_fundamental_finalization_keeps_direction_but_removes_execution_claim(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.6256,
            setup_type="fundamental_timing_setup",
            opportunity_type="medium_fundamental",
            opportunity_state="tradeable_candidate",
            evidence_role="entry_timing",
            entry_timing_signal="breakout",
            entry_trigger="15m close above a model-selected level",
            trigger_valid=True,
            trigger_quality_score=0.88,
            invalidation_level=96.0,
            exit_hint="fundamental thesis invalidated below 96",
            factor_focus=["inventory", "basis"],
            metadata={
                "data_usage_summary": build_test_data_usage("fundamental", "BU"),
                "invalidation_condition": "fundamental thesis invalidated below 96",
            },
        )

        finalized = self._finalize_directional(
            signal,
            analyst="fundamental",
            ticker="BU",
            context={
                "tradeability": "high",
                "setup_type": "fundamental_timing_setup",
                "setup_quality_ok": True,
                "fundamental_deployable_confirmed": True,
                "data_quality": {
                    "coverage_ratio": 0.9,
                    "supports_fundamental_trade_setup": True,
                },
                "risk_flags": [],
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["signal"], "Bullish")
        self.assertEqual(contract["evidence_role"], "direction_context")
        self.assertEqual(contract["opportunity_state"], "no_opportunity")
        self.assertEqual(contract["entry_timing_signal"], "")
        self.assertEqual(contract["entry_trigger"], "")
        self.assertFalse(contract["trigger_valid"])
        self.assertFalse(contract["current_trigger_confirmed"])
        self.assertEqual(contract["trigger_quality_score"], 0.0)

    def test_news_immediate_event_uses_only_event_immediate_profile(self):
        signal = CommodityNewsAnalystOutput(
            agent_name="commodity_news",
            signal=Signal.BEARISH,
            confidence=0.80,
            setup_type="news_event_setup",
            opportunity_type="event_driven",
            opportunity_state="tradeable_candidate",
            evidence_role="event_catalyst",
            entry_timing_signal="event_immediate",
            entry_trigger="current price and volume confirm the event",
            trigger_valid=True,
            trigger_quality_score=0.76,
            event_type="supply_disruption",
            invalidation_level=105.0,
            exit_hint="event impact invalidated above 105",
            factor_focus=["supply_disruption"],
            metadata={
                "data_usage_summary": build_test_data_usage("commodity_news", "BU"),
                "invalidation_condition": "event impact invalidated above 105",
            },
        )

        finalized = self._finalize_directional(
            signal,
            analyst="commodity_news",
            ticker="BU",
            context={
                "tradeability": "high",
                "setup_type": "news_event_setup",
                "setup_quality_ok": True,
                "tradable_event": True,
                "price_reaction_required": False,
                "price_reaction_confirmed": True,
                "risk_flags": [],
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["entry_timing_signal"], "event_immediate")
        self.assertEqual(
            contract["entry_trigger"],
            "当前事件已满足即时执行边界，使用首根合法1分钟线执行",
        )
        self.assertEqual(contract["opportunity_state"], "probe_candidate")
        self.assertEqual(contract["trigger_quality_score"], 0.76)

    def test_news_agent_uses_registered_reference_for_current_event_structure(self):
        captured = {}
        router = Mock()
        news_item = Mock()
        news_item.model_dump_json.return_value = "{}"
        router.get_china_futures_news.return_value = [news_item]
        news_context = {
            "sector": "energy",
            "tradeability": "high",
            "setup_type": "news_event_setup",
            "setup_quality_ok": True,
            "tradable_event": True,
            "price_reaction_required": False,
            "price_reaction_confirmed": True,
            "event_regime": "supply_disruption",
            "risk_flags": [],
        }

        def llm_call(*, prompt, **_kwargs):
            captured["prompt"] = prompt
            return CommodityNewsAnalystOutput(
                signal=Signal.BEARISH,
                confidence=0.80,
                setup_type="news_event_setup",
                opportunity_type="event_driven",
                opportunity_state="tradeable_candidate",
                evidence_role="event_catalyst",
                entry_timing_signal="event_immediate",
                entry_trigger="current event and price reaction are confirmed",
                trigger_valid=True,
                trigger_quality_score=0.76,
                event_type="supply_disruption",
                invalidation_level=105.0,
                position_invalidation_level=103.0,
                exit_hint="current event structure fails above 103",
            )

        full_config = {"llm": {"provider": "test", "model": "test-model"}}
        state = {
            "ticker": "BU",
            "trading_date": datetime(2025, 3, 26),
            "market_type": "china_futures",
            "pre_open_only": True,
            "info_cutoff": "pre_open",
            "config": full_config,
            "full_config": full_config,
            "llm_config": full_config["llm"],
            "morning_price_context": SimpleNamespace(
                base_price=100.0,
                base_price_date="2025-03-25",
            ),
        }
        with patch.object(commodity_news, "Router", return_value=router), patch.object(
            commodity_news, "get_db", return_value=Mock()
        ), patch.object(
            commodity_news, "summarize_news_events", return_value=news_context
        ), patch.object(
            commodity_news, "format_news_summary_for_prompt", return_value=""
        ), patch.object(
            commodity_news,
            "build_learning_context",
            return_value={"text": "", "selected_ids": [], "memory_trace": {}},
        ), patch.object(
            commodity_news,
            "build_news_data_usage",
            return_value=build_test_data_usage("commodity_news", "BU"),
        ), patch.object(
            commodity_news, "agent_call", side_effect=llm_call
        ), patch.object(commodity_news.logger, "log_signal"):
            signal = commodity_news.commodity_news_agent(state)["analyst_signals"][0]

        contract = signal.metadata["action_evidence_contract"]
        self.assertIn("Current deterministic pre-open reference price is 100", captured["prompt"])
        self.assertEqual(contract["entry_timing_signal"], "event_immediate")
        self.assertEqual(contract["position_invalidation_level"], 103.0)
        self.assertNotIn("position_invalidation_reference_price", contract)

    def test_fundamental_data_gap_forms_strict_scc_compatible_contract(self):
        signal = _build_no_fundamental_data_signal(
            ticker="RB",
            trading_date="2025-03-05",
            agent_name="fundamental",
            metadata={},
            pre_open_only=True,
            info_cutoff="pre_open",
        )
        signal = _finalize_data_gap(signal, analyst="fundamental", ticker="RB")

        contract = signal.metadata["action_evidence_contract"]
        self.assertEqual(contract["contract_version"], "agentquant.action_evidence.v1")
        self.assertEqual(contract["signal"], "Neutral")
        self.assertEqual(contract["side"], "flat")
        self.assertEqual(contract["opportunity_state"], "no_opportunity")
        self.assertFalse(contract["trigger_valid"])
        self.assertFalse(contract["current_trigger_confirmed"])
        self.assertEqual(signal.validation_errors, [])
        scc = build_signal_collection_contract(
            ticker="RB",
            trading_date="2025-03-05",
            analyst_signals=[signal],
            enabled_analysts=["fundamental"],
        )
        self.assertEqual(scc["source_contracts"][0]["action_evidence_contract"], contract)

    def test_news_data_gap_forms_strict_scc_compatible_contract(self):
        signal = _build_no_news_signal(
            ticker="SR",
            trading_date="2025-03-05",
            agent_name="commodity_news",
            news_metadata={},
            pre_open_only=True,
            info_cutoff="pre_open",
        )
        signal = _finalize_data_gap(signal, analyst="commodity_news", ticker="SR")

        contract = signal.metadata["action_evidence_contract"]
        self.assertEqual(contract["signal"], "Neutral")
        self.assertEqual(contract["side"], "flat")
        self.assertEqual(contract["opportunity_state"], "no_opportunity")
        self.assertFalse(contract["trigger_valid"])
        scc = build_signal_collection_contract(
            ticker="SR",
            trading_date="2025-03-05",
            analyst_signals=[signal],
            enabled_analysts=["commodity_news"],
        )
        self.assertEqual(
            scc["source_contracts"][0]["action_evidence_contract"]["product_profile_evidence"],
            contract["product_profile_evidence"],
        )

    def test_profile_is_applied_before_unique_formal_contract_and_stays_in_sync(self):
        profile = get_product_price_behavior_profile("RB")
        usage = build_profile_usage_contract("RB", "technical", profile)
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            horizon_class="short",
            setup_type="trend_breakout_setup",
            entry_trigger="price breakout confirmed by volume and open interest",
            would_change_view_if="price closes below the confirmed breakout level",
            factor_focus=["trend", "volume", "open_interest", "rebar_inventory_confirmation"],
            evidence_quality="high",
            data_freshness="fresh",
            metadata={
                "data_usage_summary": build_test_aec(
                    "technical",
                    ticker="RB",
                    trading_date="2025-03-05",
                )["data_usage_summary"]
            },
        )
        pre_contract = apply_profile_usage_to_signal(signal.model_copy(deep=True), usage)
        self.assertNotIn("action_evidence_contract", pre_contract.metadata)

        finalized = finalize_analyst_signal(
            signal,
            quality_context={
                "ticker": "RB",
                "sector": profile["sector"],
                "tradeability": "high",
                "setup_type": "trend_breakout_setup",
                "setup_quality_ok": True,
                "dominant_direction": "bullish",
                "market_regime": "trend",
                "risk_flags": [],
            },
            full_config={"llm": {"provider": "test", "model": "test-model"}},
            analyst="technical",
            ticker="RB",
            trading_date="2025-03-05",
            learning_context={},
            product_profile=profile,
            product_profile_usage=usage,
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(set(finalized.metadata), {"action_evidence_contract"})
        self.assertEqual(contract["factor_focus"], finalized.factor_focus)
        self.assertIn("product_profile:RB", finalized.factor_focus)
        self.assertTrue(contract["product_profile_evidence"]["profile_supported_evidence"])
        for legacy in ("open", "hold", "exit", "execution", "state_permissions", "money_objective", "has_invalidation"):
            self.assertNotIn(legacy, contract)

    def test_learning_calibration_precedes_quality_profile_and_contract_generation(self):
        source = inspect.getsource(finalize_analyst_signal)
        ordered = [
            "calibrate_signal_with_learning_context",
            "apply_signal_quality_gate",
            "apply_business_quality_enrichment",
            "evaluate_profile_usage_contract",
            "apply_profile_usage_to_signal",
            "apply_trade_research_contract",
            "analyst_output_landing_violations",
        ]
        positions = [source.index(name) for name in ordered]
        self.assertEqual(positions, sorted(positions))

    def test_main_llm_config_switch_is_passed_through_without_analyst_override(self):
        state = {
            "llm_config": {
                "provider": "AlternateProvider",
                "model": "alternate-model",
                "alternate_provider": {"reasoning_effort": "high"},
            },
            "full_config": {"analyst_llm": {"cloud_model": "stale-private-model"}},
        }
        resolved = resolve_analyst_llm_config(state)
        self.assertEqual(resolved, state["llm_config"])
        self.assertEqual(resolved["provider"], "AlternateProvider")
        self.assertEqual(resolved["model"], "alternate-model")

    def test_runtime_prompts_request_evidence_not_formal_contract_or_trade_actions(self):
        technical_prompt = build_futures_technical_prompt(
            ticker="RB",
            signal_results_compact={"trend": "UP"},
            data_recency_score=0.35,
            data_recency_label="stale",
        )
        fundamental_prompt = build_futures_fundamental_prompt(
            ticker="RB", fundamentals="inventory: usable"
        )
        news_prompt = build_futures_commodity_news_prompt(
            ticker="RB", instrument_context="rebar", news=[]
        )
        prompts = (technical_prompt, fundamental_prompt, news_prompt)
        for prompt in prompts:
            self.assertNotIn("data_freshness", prompt)
            self.assertNotIn("data_unavailable_no_trade", prompt)
            self.assertNotIn("position_ratio", prompt)
            self.assertNotIn("action_evidence_contract.open", prompt)
            self.assertNotIn("probe/open", prompt)
            self.assertNotIn("metadata.action_evidence_contract:", prompt)
            self.assertIn("opportunity_state", prompt)
        self.assertIn("breakout / pullback / vwap_confirmed", technical_prompt)
        self.assertIn(canonical_entry_trigger("pullback", "long"), technical_prompt)
        self.assertIn(canonical_entry_trigger("pullback", "short"), technical_prompt)
        self.assertIn("System-computed market-data recency (read-only fact)", technical_prompt)
        self.assertIn("every complete watch_for_trigger", technical_prompt)
        self.assertIn("Direction alone is not watch_for_trigger", technical_prompt)
        self.assertIn("trigger_quality_score measures only the current confirmed trigger", technical_prompt)
        self.assertIn("evidence_role=direction_context", fundamental_prompt)
        self.assertIn("must not output a Trader execution profile", fundamental_prompt)
        self.assertIn("must be an empty string in every fundamental output", fundamental_prompt)
        self.assertIn("trigger_quality_score must be 0.0", fundamental_prompt)
        self.assertIn("entry_timing_signal=event_immediate", news_prompt)
        self.assertIn("news must not create watch_for_trigger", news_prompt)
        self.assertIn("non-zero trigger_quality_score", news_prompt)

    def test_three_analysts_use_shared_finalizer_and_no_news_product_map(self):
        for relative in (
            "agents/analysis_team/technical.py",
            "agents/analysis_team/fundamental.py",
            "agents/analysis_team/commodity_news.py",
        ):
            source = (SRC_ROOT / relative).read_text(encoding="utf-8-sig")
            self.assertIn("resolve_analyst_llm_config", source, relative)
            self.assertIn("finalize_analyst_signal", source, relative)
            self.assertIn("build_required_market_data_unavailable_signal", source, relative)
            self.assertNotIn("persist_analyst_signal", source, relative)
            self.assertNotIn('"prompt": prompt', source, relative)
            self.assertNotIn("llm_config = state[\"llm_config\"]", source, relative)
        news_source = (SRC_ROOT / "agents/analysis_team/commodity_news.py").read_text(encoding="utf-8-sig")
        self.assertNotIn("FUTURES_INSTRUMENT_CONTEXT", news_source)


if __name__ == "__main__":
    unittest.main()
