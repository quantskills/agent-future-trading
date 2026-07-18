import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.constants import Signal
from graph.schema import AnalystSignal
from tests.contract_test_fixtures import build_test_aec
from tools.agent_tools.analysis.analyst_quality import apply_trade_research_contract
from tools.agent_tools.analysis.analyst_quality import build_technical_context
from tools.agent_tools.analysis.analyst_quality import parse_fundamental_factors
from tools.agent_tools.analysis.analyst_quality import summarize_news_events
from agents.analysis_team.technical import (
    _validate_pre_open_price_window,
    get_adx_signal,
    get_mean_reversion_signal,
)
from apis.router import Router
from tools.agent_tools.decision.pm_signal_fusion import (
    build_opportunity_scorecard,
)
from tools.agent_tools.decision.pm_ticker_side_selection import (
    SIDE_PRIORITY_MEANING,
    SIDE_PRIORITY_SEMANTICS_VERSION,
    select_ticker_side,
)
from tools.agent_tools.decision.pm_invalidation_policy import _has_structured_invalidation_condition
from tools.common.signal_evidence_collection import validate_action_evidence_contract
from tools.common.contracts import (
    build_internal_message_contract,
    build_trade_research_contract,
    validate_artifact_header,
    validate_internal_message_contract,
    validate_trade_research_contract,
)


class AgentContractFixtureTest(unittest.TestCase):
    def test_all_required_agent_contract_fixtures_are_valid(self):
        fixture_path = SRC_ROOT / "tests" / "fixtures" / "agent_contracts" / "contract_fixtures.json"
        fixtures = json.loads(fixture_path.read_text(encoding="utf-8-sig"))
        required_agents = {
            "technical",
            "fundamental",
            "commodity_news",
            "signal_collector",
            "portfolio_manager",
            "auditor",
            "trader",
            "accountant",
            "reviewer",
        }
        required_artifact_types = {
            "AnalystSignalArtifact",
            "PMDecisionArtifact",
            "AuditVerdictArtifact",
            "ExecutionArtifact",
            "SettlementArtifact",
            "ReviewerAttributionArtifact",
            "Phase4ValidationArtifact",
            "ResearchInputMaterial",
        }
        seen_agents = set()
        seen_artifact_types = set()
        for fixture in fixtures:
            header = fixture.get("header") or {}
            seen_agents.add(header.get("agent_name"))
            seen_artifact_types.add(fixture.get("artifact_type"))
            self.assertEqual(validate_artifact_header(header), [], fixture.get("artifact_type"))
            self.assertTrue(fixture.get("artifact_type"))
            self.assertIsInstance(fixture.get("payload"), dict)
            self.assertTrue(header.get("source_artifacts"), fixture.get("artifact_type"))
        self.assertTrue(required_agents.issubset(seen_agents))
        self.assertTrue(required_artifact_types.issubset(seen_artifact_types))


    def test_internal_message_contract_and_trade_research_contract_are_valid(self):
        message = build_internal_message_contract(
            agent="technical",
            trading_date="2025-03-03",
            ticker="BU",
            message_type="AnalystSignalArtifact",
            source_artifacts=["market_data:BU"],
        )
        research = build_trade_research_contract(
            opportunity_type="trend_continuation",
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout confirmation",
            exit_hint="close below invalidation",
            holding_period_hint="short:2 trading day(s)",
            factor_focus=["trend", "volume"],
            current_evidence_conflict=["basis_flat"],
            invalidation_level=3200,
        )

        self.assertEqual(validate_internal_message_contract(message), [])
        self.assertEqual(validate_trade_research_contract(research), [])

    def test_conditional_entry_trigger_stays_watch_for_trigger(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            entry_trigger="open only after breakout confirmation with volume expansion",
            exit_hint="exit if price closes back below breakout area",
            invalidation_level=3200,
            business_quality_score=0.68,
            factor_alignment_score=0.70,
            conflicting_factors=["inventory_conflict"],
        )

        result = apply_trade_research_contract(
            signal,
            {
                "tradeability": "high",
                "market_regime": "trend",
                "indicator_votes": {"details": {"trend": "Bullish", "macd": "Bullish"}},
                "risk_flags": ["high_volatility"],
            },
            analyst="technical",
            trading_date="2025-03-03",
            ticker="BU",
        )

        self.assertEqual(result.opportunity_type, "range_breakout")
        self.assertEqual(result.opportunity_state, "watch_for_trigger")
        self.assertEqual(result.opportunity_state, "watch_for_trigger")
        self.assertFalse(result.trigger_valid)
        self.assertIn("trade_research_contract", result.metadata)
        self.assertEqual(result.metadata["trade_research_contract"]["opportunity_state"], "watch_for_trigger")
        self.assertEqual(result.metadata["action_evidence_contract"]["opportunity_state"], "watch_for_trigger")
        self.assertFalse(result.metadata["action_evidence_contract"]["trigger_valid"])
        self.assertEqual(result.metadata["action_evidence_contract"]["opportunity_state"], "watch_for_trigger")
        self.assertIn("internal_message_contract", result.metadata)
        self.assertIn("trend", result.factor_focus)
        self.assertIn("high_volatility", result.current_evidence_conflict)
        self.assertIn("conditional_entry_trigger_pending", result.current_evidence_conflict)

    def test_tradeable_only_if_trigger_stays_watch_for_trigger(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BEARISH,
            confidence=0.62,
            entry_trigger=(
                "Short setup becomes tradeable only if price breaks lower or basis weakens "
                "further while inventories continue rising"
            ),
            exit_hint="exit if inventories start drawing and basis strengthens",
            business_quality_score=0.72,
            factor_alignment_score=0.70,
        )

        result = apply_trade_research_contract(
            signal,
            {
                "tradeability": "high",
                "market_regime": "range",
                "sector": "agricultural",
                "risk_flags": [],
            },
            analyst="fundamental",
            trading_date="2025-03-20",
            ticker="C",
        )

        self.assertEqual(result.opportunity_state, "watch_for_trigger")
        self.assertEqual(result.opportunity_state, "watch_for_trigger")
        self.assertFalse(result.trigger_valid)
        self.assertFalse(result.metadata["action_evidence_contract"]["trigger_valid"])
        self.assertEqual(result.metadata["action_evidence_contract"]["opportunity_state"], "watch_for_trigger")

    def test_requires_confirmed_break_after_open_stays_watch_for_trigger(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.70,
            entry_trigger=(
                "In the current range regime, bearish entry timing requires a confirmed break "
                "below the nearest pre-open support/range floor after the open, with MACD/trend "
                "still down and volume ratio staying elevated or settlement-price weakness "
                "persisting; without that confirmation, remain on watch."
            ),
            exit_hint="exit if price closes back above the failed breakdown area",
            invalidation_level=3520.0,
            business_quality_score=0.72,
            data_coverage_score=0.86,
            setup_type="range_breakout",
            opportunity_state="probe_candidate",
            trigger_valid=False,
            invalidation_present=True,
        )

        result = apply_trade_research_contract(
            signal,
            {
                "tradeability": "high",
                "market_regime": "range",
                "dominant_direction": "bearish",
                "setup_type": "range_breakout",
                "setup_quality_ok": True,
                "invalidation_condition": "price closes back above the failed breakdown area",
                "indicator_votes": {"details": {"trend": "Bearish", "macd": "Bearish", "adx": "Neutral"}},
                "risk_flags": [],
            },
            analyst="technical",
            trading_date="2025-03-05",
            ticker="HC",
        )

        action_contract = result.metadata["action_evidence_contract"]
        research_contract = result.metadata["trade_research_contract"]
        self.assertEqual(result.opportunity_state, "watch_for_trigger")
        self.assertFalse(result.trigger_valid)
        self.assertFalse(action_contract["trigger_valid"])
        self.assertFalse(research_contract["trigger_valid"])
        self.assertNotIn("action_evidence_contract", research_contract)
        self.assertEqual(action_contract["opportunity_state"], "watch_for_trigger")
        self.assertIn("conditional_entry_trigger_pending", result.current_evidence_conflict)

    def test_setup_quality_without_current_confirmation_stays_watch_for_trigger(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.74,
            entry_trigger="trend_breakout setup below range floor with volume expansion",
            exit_hint="exit if price closes back above range floor",
            invalidation_level=3520.0,
            business_quality_score=0.74,
            data_coverage_score=0.88,
            setup_type="trend_breakout",
            opportunity_state="tradeable_candidate",
            trigger_valid=False,
            invalidation_present=True,
        )

        result = apply_trade_research_contract(
            signal,
            {
                "tradeability": "high",
                "market_regime": "trend",
                "dominant_direction": "bearish",
                "setup_type": "trend_breakout",
                "setup_quality_ok": True,
                "invalidation_condition": "price closes back above range floor",
                "indicator_votes": {"details": {"trend": "Bearish", "macd": "Bearish", "adx": "Bearish"}},
                "risk_flags": [],
            },
            analyst="technical",
            trading_date="2025-03-06",
            ticker="PB",
        )

        action_contract = result.metadata["action_evidence_contract"]
        self.assertTrue(action_contract["setup_quality_ok"])
        self.assertEqual(result.opportunity_state, "watch_for_trigger")
        self.assertFalse(result.trigger_valid)
        self.assertFalse(action_contract["trigger_valid"])
        self.assertFalse(action_contract["current_trigger_confirmed"])
        self.assertIn("current_entry_trigger_not_confirmed", result.current_evidence_conflict)

    def test_current_trigger_and_invalidation_cannot_be_hidden_as_no_opportunity(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.68,
            entry_trigger="current breakout above opening range is confirmed by volume expansion",
            exit_hint="exit if price closes back below opening range",
            invalidation_level=3310.0,
            business_quality_score=0.72,
            data_coverage_score=0.85,
            setup_type="trend_breakout",
            holding_period_hint="1-3 trading days while breakout remains valid",
        )

        result = apply_trade_research_contract(
            signal,
            {
                "tradeability": "high",
                "market_regime": "trend",
                "dominant_direction": "bullish",
                "setup_type": "trend_breakout",
                "setup_quality_ok": True,
                "sector_setup_alignment": "preferred",
                "required_confirmation": "breakout with volume",
                "invalidation_condition": "price closes back below opening range",
                "current_trigger_confirmed": True,
                "action_evidence_contract": {
                    "setup_type": "trend_breakout",
                    "setup_quality_ok": True,
                    "trigger_valid": True,
                    "invalidation_present": True,
                    "entry_trigger": "current breakout above opening range is confirmed by volume expansion",
                    "invalidation_condition": "price closes back below opening range",
                    "learning_scope": {
                        "setup_family": "trend_breakout",
                        "sector_setup_alignment": "preferred",
                    },
                },
                "indicator_votes": {"details": {"trend": "Bullish", "macd": "Bullish", "adx": "Bullish"}},
                "risk_flags": [],
            },
            analyst="technical",
            trading_date="2025-03-03",
            ticker="RB",
        )

        self.assertIn(result.opportunity_state, {"probe_candidate", "tradeable_candidate"})
        self.assertNotIn(result.opportunity_state, {"no_opportunity", "watch_for_trigger"})
        self.assertIn(result.opportunity_state, {"probe_candidate", "tradeable_candidate"})
        self.assertTrue(result.trigger_valid)
        self.assertTrue(result.invalidation_present)
        self.assertTrue(result.metadata["action_evidence_contract"]["current_trigger_confirmed"])

    def test_generic_trigger_becomes_no_opportunity_without_fabrication(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.62,
            entry_trigger="technical_price_trigger",
            business_quality_score=0.65,
        )

        result = apply_trade_research_contract(
            signal,
            {
                "tradeability": "medium",
                "market_regime": "range",
                "dominant_direction": "bullish",
                "indicator_votes": {"details": {"trend": "Bullish", "macd": "Neutral", "rsi": "Bullish"}},
                "risk_flags": ["trend_continuation_requires_breakout_confirmation"],
            },
            analyst="technical",
            trading_date="2025-03-03",
            ticker="BU",
        )

        self.assertEqual(result.opportunity_state, "no_opportunity")
        self.assertFalse(result.trigger_valid)
        self.assertEqual(result.entry_trigger, "")
        self.assertNotIn("technical_derived_specific_entry_condition", result.setup_quality_notes)

    def test_neutral_tracking_fields_do_not_create_watch_without_setup(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.NEUTRAL,
            confidence=0.42,
            neutral_opportunity_bucket="watchlist_trigger",
            neutral_trigger_condition="break above 3350 with volume expansion",
            counterfactual_side="long",
            neutral_watchlist_priority="high",
            business_quality_score=0.50,
        )

        result = apply_trade_research_contract(
            signal,
            {
                "tradeability": "medium",
                "market_regime": "trend",
                "risk_flags": [],
            },
            analyst="technical",
            trading_date="2025-03-03",
            ticker="RB",
        )

        self.assertEqual(result.opportunity_state, "no_opportunity")
        self.assertEqual(result.metadata["trade_research_contract"]["opportunity_state"], "no_opportunity")
        self.assertEqual(result.metadata["action_evidence_contract"]["opportunity_state"], "no_opportunity")

    def test_trade_research_contract_syncs_learning_impact_opportunity_state(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.62,
            business_quality_score=0.72,
            factor_alignment_score=0.68,
            entry_trigger="break above opening range with volume",
            exit_hint="exit if price closes back below breakout range",
            invalidation_level=3310.0,
            learning_impact_summary={
                "contract_version": "agentquant.analyst_learning_impact.v1",
                "historical_support": ["RB:long:trend_breakout_setup:trend:open"],
                "historical_contradiction": [],
                "current_evidence_confirmed": ["breakout confirmation"],
                "current_evidence_missing": [],
            },
        )
        result = apply_trade_research_contract(
            signal,
            {
                "tradeability": "high",
                "market_regime": "trend",
                "setup_type": "trend_breakout",
                "setup_quality_ok": True,
                "sector_setup_alignment": "aligned",
                "required_confirmation": "breakout with volume",
                "invalidation_condition": "price closes back below breakout range",
                "current_trigger_confirmed": True,
                "action_evidence_contract": {
                    "setup_type": "trend_breakout",
                    "setup_quality_ok": True,
                    "trigger_valid": True,
                    "invalidation_present": True,
                    "entry_trigger": "break above opening range with volume",
                    "invalidation_condition": "price closes back below breakout range",
                    "learning_scope": {
                        "setup_family": "trend_breakout",
                        "sector_setup_alignment": "aligned",
                    },
                },
            },
            analyst="technical",
            ticker="RB",
        )

        summary = result.learning_impact_summary
        self.assertEqual(summary["opportunity_state"], result.opportunity_state)
        self.assertIn("opportunity_state_reason", summary)
        self.assertIn("no_trade_authority", summary["authority_boundary"])
        self.assertEqual(result.metadata["learning_impact_summary"], summary)

    def test_analyst_structured_summaries_reject_free_text(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.NEUTRAL,
            learning_impact_summary="learned something",
            factor_calibration_summary="factor text",
            event_calibration_summary="event text",
        )

        self.assertEqual(signal.learning_impact_summary, {})
        self.assertEqual(signal.factor_calibration_summary, {})
        self.assertEqual(signal.event_calibration_summary, {})

    def test_technical_context_does_not_fabricate_missing_setup_fields(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.68,
            entry_trigger="technical_price_trigger",
            business_quality_score=0.70,
        )

        context = build_technical_context(
            "BU",
            {
                "trend": Signal.BULLISH,
                "macd": Signal.BULLISH,
                "adx": Signal.BULLISH,
                "open_interest": Signal.BULLISH,
            },
            {"trend_strength": 28.0, "volatility": 0.12, "volume_ratio": 1.1},
        )

        result = apply_trade_research_contract(
            signal,
            context,
            analyst="technical",
            trading_date="2025-03-03",
            ticker="BU",
        )

        self.assertEqual(result.entry_trigger, "")
        self.assertEqual(result.opportunity_state, "no_opportunity")
        self.assertFalse(_has_structured_invalidation_condition([result]))
        self.assertNotIn("technical_derived_specific_entry_condition", result.setup_quality_notes)
        contract = result.metadata["action_evidence_contract"]
        self.assertEqual(contract["analyst"], "technical")
        self.assertEqual(contract["learning_scope"]["setup_family"], "trend_breakout")
        self.assertEqual(contract["learning_scope"]["sector_setup_alignment"], "preferred")
        self.assertIn("primary_confirmation", contract["learning_scope"])

    def test_range_context_does_not_replace_generic_trigger(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.66,
            entry_trigger="technical_price_trigger",
            business_quality_score=0.68,
        )

        result = apply_trade_research_contract(
            signal,
            {
                "tradeability": "medium",
                "market_regime": "range",
                "dominant_direction": "bearish",
                "setup_type": "range_reversal",
                "setup_quality_ok": True,
                "required_confirmation": "RSI/Stochastic/mean-reversion signal aligns with resistance",
                "invalidation_condition": "price breaks above resistance with volume",
                "action_evidence_contract": {
                    "setup_type": "range_reversal",
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "invalidation_present": True,
                    "entry_trigger": "RSI/Stochastic/mean-reversion signal aligns with resistance",
                    "invalidation_condition": "price breaks above resistance with volume",
                    "learning_scope": {
                        "setup_family": "range_reversal",
                    },
                },
                "features": {"trend_strength": 19.0, "volume_ratio": 0.95},
                "indicator_votes": {"details": {"rsi": "Bearish", "stochastic": "Bearish", "mean_reversion": "Bearish"}},
                "risk_flags": [],
            },
            analyst="technical",
            trading_date="2025-03-03",
            ticker="SR",
        )

        self.assertEqual(result.entry_trigger, "")
        self.assertEqual(result.exit_hint, "")
        self.assertEqual(result.opportunity_state, "no_opportunity")

    def test_fundamental_context_builds_sector_factor_tree_contract(self):
        fundamentals = "\n".join(
            [
                "RB社会库存: value [role: inventory; frequency: weekly; last 5 obs trend: down]",
                "RB表观需求: value [role: demand; frequency: weekly; last 5 obs trend: up]",
                "RB利润: value [role: cost_profit; frequency: weekly; last 5 obs trend: up]",
            ]
        )
        context = parse_fundamental_factors(
            fundamentals,
            {
                "configured_indicator_count": 3,
                "loaded_indicator_count": 3,
                "finoview_factor_judgment": {"tradable_coverage": True, "coverage_score": 1.0},
                "local_finoview_availability_audit": {"supports_fundamental_trade_setup": True},
            },
            "RB",
        )

        self.assertEqual(context["sector"], "ferrous")
        roles = context["fundamental_driver_roles"]
        self.assertIn("inventory", roles["primary_driver_groups"])
        self.assertIn("demand", roles["primary_driver_groups"])
        self.assertNotIn("action_evidence_contract", context)
        self.assertEqual(context["learning_scope"]["primary_driver_groups"], roles["primary_driver_groups"])

    def test_news_context_classifies_sector_tradable_catalyst(self):
        news_item = type(
            "News",
            (),
            {
                "title": "巴西降雨导致装运受阻，进口供应大幅收紧",
                "content": "豆粕进口到港减少，市场担忧短缺。",
                "publish_time": "2025-03-03 08:30:00",
            },
        )()
        context = summarize_news_events([news_item], "M", trading_date="2025-03-03")

        self.assertEqual(context["sector"], "agricultural")
        self.assertTrue(context["catalyst_classification"]["tradable_catalyst"])
        self.assertGreaterEqual(context["catalyst_classification"]["sector_tradable_event_count"], 1)
        self.assertTrue(context["price_reaction_required"])
        self.assertNotIn("action_evidence_contract", context)

    def test_adx_signal_uses_di_for_direction_not_strength_alone(self):
        up = pd.DataFrame(
            {
                "high": [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25],
                "low": [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24],
                "close": [9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5, 19.5, 20.5, 21.5, 22.5, 23.5, 24.5],
            }
        )
        down = pd.DataFrame(
            {
                "high": list(reversed(up["high"].tolist())),
                "low": list(reversed(up["low"].tolist())),
                "close": list(reversed(up["close"].tolist())),
            }
        )

        self.assertEqual(get_adx_signal(up, {"period": 3}), Signal.BULLISH)
        self.assertEqual(get_adx_signal(down, {"period": 3}), Signal.BEARISH)

    def test_mean_reversion_uses_negative_zscore_for_bullish_reversal(self):
        params = {
            "bollinger_window": 5,
            "rolling_window": 5,
            "z_score_extreme": 1.0,
            "bb_position_threshold": 0.2,
        }
        sold_off = pd.DataFrame(
            {
                "close": [100, 101, 100, 102, 101, 100, 99, 98, 97, 80],
            }
        )
        stretched_up = pd.DataFrame(
            {
                "close": [100, 99, 100, 98, 99, 100, 101, 102, 103, 120],
            }
        )

        self.assertEqual(get_mean_reversion_signal(sold_off, params), Signal.BULLISH)
        self.assertEqual(get_mean_reversion_signal(stretched_up, params), Signal.BEARISH)

    def test_fundamental_anchor_does_not_derive_missing_short_trigger(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.66,
            horizon_class="medium",
            entry_trigger="fundamental_anchor",
            business_quality_score=0.72,
        )

        result = apply_trade_research_contract(
            signal,
            {
                "tradeability": "high",
                "factor_group_counts": {"inventory": {"down": 2}, "demand": {"up": 1}, "basis": {"up": 1}},
                "data_quality": {"supports_fundamental_trade_setup": True, "coverage_ratio": 0.82},
                "risk_flags": [],
            },
            analyst="fundamental",
            trading_date="2025-03-03",
            ticker="BU",
        )

        self.assertEqual(result.entry_trigger, "")
        self.assertEqual(result.opportunity_state, "no_opportunity")
        self.assertFalse(_has_structured_invalidation_condition([result]))
        self.assertNotIn("fundamental_derived_specific_entry_condition", result.setup_quality_notes)

    def test_news_catalyst_requires_price_volume_confirmation_not_fake_tradeable(self):
        signal = AnalystSignal(
            agent_name="commodity_news",
            signal=Signal.BULLISH,
            confidence=0.64,
            entry_trigger="news_event_trigger",
            business_quality_score=0.66,
        )

        result = apply_trade_research_contract(
            signal,
            {
                "tradeability": "medium",
                "tradable_event": True,
                "price_reaction_required": True,
                "price_reaction_confirmed": False,
                "direction_counts": {"bullish": 3, "bearish": 0},
                "event_type_counts": {"supply": 2, "policy": 1},
                "risk_flags": [],
            },
            analyst="commodity_news",
            trading_date="2025-03-03",
            ticker="BU",
        )

        self.assertEqual(result.opportunity_state, "no_opportunity")
        self.assertEqual(result.entry_trigger, "")
        self.assertIn("news_event_requires_price_or_intraday_confirmation", result.current_evidence_conflict)
        self.assertFalse(result.trigger_valid)
        self.assertFalse(result.metadata["action_evidence_contract"]["trigger_valid"])
        self.assertFalse(result.metadata["trade_research_contract"]["trigger_valid"])
        self.assertNotIn("action_evidence_contract", result.metadata["trade_research_contract"])

    def test_pm_invalidation_ignores_generic_would_change_view_text(self):
        weak = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            would_change_view_if="wait_for_trigger",
            metadata={"action_evidence_contract": {"invalidation_present": False}},
        )
        strong = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            would_change_view_if="long technical idea invalid if price closes below trigger area",
            metadata={"action_evidence_contract": {"invalidation_present": True}},
        )

        self.assertFalse(_has_structured_invalidation_condition([weak]))
        self.assertTrue(_has_structured_invalidation_condition([strong]))

    def test_scorecard_does_not_count_generic_invalidation_as_boundary(self):
        weak = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            setup_quality_score=0.72,
            business_quality_score=0.70,
            entry_trigger="open only after price breakout confirms with volume expansion",
            would_change_view_if="wait_for_trigger",
        )
        strong = weak.model_copy(
            update={
                "would_change_view_if": "long setup invalid if price closes below breakout area",
                "invalidation_present": True,
                "metadata": {
                    "action_evidence_contract": {
                        "invalidation_present": True,
                        "invalidation_condition": "long setup invalid if price closes below breakout area",
                    }
                },
            }
        )

        weak_card = build_opportunity_scorecard(
            ticker="BU",
            analyst_signals=[weak],
            market_confirmation={"confirmation_score": 0.65},
            config={"weak_confirmation_threshold": 0.45},
        )
        strong_card = build_opportunity_scorecard(
            ticker="BU",
            analyst_signals=[strong],
            market_confirmation={"confirmation_score": 0.65},
            config={"weak_confirmation_threshold": 0.45},
        )

        self.assertEqual(weak_card["long"]["invalidation_count"], 0)
        self.assertIn("missing_invalidation_boundary", weak_card["long"]["gating_failures"])
        self.assertEqual(strong_card["long"]["invalidation_count"], 1)
        self.assertNotIn("missing_invalidation_boundary", strong_card["long"]["gating_failures"])

    def test_scorecard_preserves_conditional_watch_trigger_fields(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.62,
            opportunity_state="watch_for_trigger",
            setup_quality_score=0.67,
            business_quality_score=0.64,
            entry_trigger=(
                "wait for post-open break below support with volume confirmation"
            ),
            would_change_view_if="short setup invalid if price closes back above range high",
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

        card = build_opportunity_scorecard(
            ticker="HC",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.52},
            config={"weak_confirmation_threshold": 0.45},
        )

        row = card["short"]
        self.assertEqual(row["final_state"], "watch_for_trigger")
        self.assertTrue(row["setup_quality_ok"])
        self.assertFalse(row["trigger_valid"])
        self.assertFalse(row["current_trigger_confirmed"])
        self.assertTrue(row["invalidation_present"])
        self.assertEqual(row["opportunity_state"], "watch_for_trigger")
        self.assertIn("post-open break", row["entry_trigger"])
        self.assertEqual(row["source_analysts"], ["technical"])
        self.assertIn("opportunity_score", row)
        self.assertIn("opportunity_score_components", row)
        self.assertNotIn("opportunity_rank", row)
        self.assertNotIn("side_priority", row)
        self.assertNotIn("ticker_side_priority", row)
        self.assertIn("analyst_direction_evidence", row)
        self.assertEqual(row["analyst_direction_evidence"]["side"], "short")
        self.assertEqual(
            row["direction_evidence_boundary"],
            "fusion_preserves_signal_collector_evidence_no_pm_side_selection",
        )
        self.assertIn("capital_allocation_reason", row)
        self.assertIn("learning_adjustment_summary", row)
        self.assertTrue(row["conditional_monitor_candidate"])
        self.assertEqual(row["capital_allocation_reason"], "monitorable_conditional_candidate_selected_only_if_pm_capital_queue_allows")

    def test_scorecard_prefers_action_evidence_contract_text_over_raw_signal_text(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.62,
            opportunity_state="watch_for_trigger",
            setup_quality_score=0.67,
            business_quality_score=0.64,
            entry_trigger="raw stale trigger text",
            would_change_view_if="raw stale invalidation",
            metadata={
                "action_evidence_contract": {
                    "opportunity_state": "watch_for_trigger",
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "invalidation_present": True,
                    "entry_trigger": "canonical post-open breakdown confirmation",
                    "invalidation_condition": "canonical invalid if price reclaims range high",
                }
            },
        )

        card = build_opportunity_scorecard(
            ticker="HC",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.52},
            config={"weak_confirmation_threshold": 0.45},
        )

        row = card["short"]
        self.assertEqual(row["entry_trigger"], "canonical post-open breakdown confirmation")
        self.assertTrue(row["invalidation_present"])

    def test_scorecard_ranks_stronger_side_above_weaker_side(self):
        long_signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            setup_quality_score=0.76,
            business_quality_score=0.72,
            entry_trigger="current breakout confirmed above resistance",
            trigger_valid=True,
            current_trigger_confirmed=True,
            would_change_view_if="long invalid if price closes below breakout area",
            metadata={
                "action_evidence_contract": {
                    "opportunity_state": "tradeable_candidate",
                    "setup_quality_ok": True,
                    "trigger_valid": True,
                    "current_trigger_confirmed": True,
                    "invalidation_present": True,
                    "entry_trigger": "current breakout confirmed above resistance",
                }
            },
        )
        short_signal = AnalystSignal(
            agent_name="commodity_news",
            signal=Signal.BEARISH,
            confidence=0.35,
            opportunity_state="watch_for_trigger",
            setup_quality_score=0.45,
            business_quality_score=0.40,
            entry_trigger="wait for post-open breakdown",
            would_change_view_if="short invalid if price closes back above range",
            metadata={
                "action_evidence_contract": {
                    "opportunity_state": "watch_for_trigger",
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "invalidation_present": True,
                    "entry_trigger": "wait for post-open breakdown",
                }
            },
        )

        card = build_opportunity_scorecard(
            ticker="BU",
            analyst_signals=[long_signal, short_signal],
            market_confirmation={"confirmation_score": 0.72},
            config={"weak_confirmation_threshold": 0.45},
        )

        self.assertNotIn("opportunity_rank", card["long"])
        self.assertNotIn("opportunity_rank", card["short"])
        self.assertNotIn("side_priority", card["long"])
        self.assertNotIn("ticker_side_priority", card["long"])
        selected = select_ticker_side(
            ticker="BU",
            analyst_signals=[long_signal, short_signal],
            signal_collection_contract={"dominant_side": "long"},
            market_confirmation={"confirmation_score": 0.72},
            data_quality_summary={},
            decision_date="2025-03-05",
            config={"weak_confirmation_threshold": 0.45},
            prebuilt_scorecard=card,
        )
        self.assertEqual(selected["opportunity_scorecard"]["long"]["side_priority"], 1)
        self.assertIsNone(selected["opportunity_scorecard"]["short"]["side_priority"])
        self.assertTrue(selected["opportunity_scorecard"]["long"]["side_priority_is_not_capital_rank"])
        self.assertGreater(card["long"]["opportunity_score"], card["short"]["opportunity_score"])
        self.assertIn("setup_quality", card["long"]["opportunity_score_components"])

    def test_single_complete_fundamental_setup_with_strong_market_confirmation_is_tradeable(self):
        """Regression for PM over-blocking a complete setup as watch_for_trigger."""
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.NEUTRAL,
            confidence=0.35,
            opportunity_state="no_opportunity",
            setup_quality_score=0.30,
            business_quality_score=0.31,
            entry_trigger="bullish reversal would require price trigger confirmation",
            would_change_view_if="wait for price trigger confirmation",
        )
        fundamental = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.46,
            opportunity_state="tradeable_candidate",
            setup_quality_score=0.75,
            business_quality_score=0.72,
            entry_trigger="enter only if futures hold above support after selloff and basis remains backwardation",
            would_change_view_if="long setup invalid if basis flips to contango or inventory builds for two consecutive weeks",
            horizon_class="medium",
            invalidation_present=True,
            metadata={
                "action_evidence_contract": {
                    "opportunity_state": "tradeable_candidate",
                    "setup_quality_ok": True,
                    "trigger_valid": True,
                    "current_trigger_confirmed": True,
                    "invalidation_present": True,
                    "entry_trigger": "enter only if futures hold above support after selloff and basis remains backwardation",
                    "invalidation_condition": "long setup invalid if basis flips to contango or inventory builds for two consecutive weeks",
                }
            },
        )
        news = AnalystSignal(
            agent_name="commodity_news",
            signal=Signal.NEUTRAL,
            confidence=0.30,
            opportunity_state="no_opportunity",
        )

        card = build_opportunity_scorecard(
            ticker="ZN",
            analyst_signals=[technical, fundamental, news],
            market_confirmation={"confirmation_score": 0.75},
            data_quality_summary={"critical_gap": False, "fundamental_trade_setup_gap": False},
            config={
                "weak_confirmation_threshold": 0.45,
                "tradeable_threshold": 0.58,
                "min_tradeable_candidate_setup_quality": 0.55,
                "single_tradeable_candidate_setup_confirmation_score": 0.68,
            },
        )

        self.assertEqual(card["long"]["final_state"], "probe_candidate")
        self.assertTrue(card["long"]["single_tradeable_candidate_setup_confirmed"])
        self.assertIn(
            "single_tradeable_candidate_with_strong_market_confirmation",
            card["long"]["scorecard_promotion_reasons"],
        )
        self.assertNotIn("missing_entry_setup", card["long"]["gating_failures"])
        self.assertNotIn("missing_invalidation_boundary", card["long"]["gating_failures"])

    def test_single_setup_with_technical_opposition_is_not_promoted(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.55,
            opportunity_state="tradeable_candidate",
            setup_quality_score=0.70,
            business_quality_score=0.65,
            entry_trigger="short if breakdown confirms with volume",
            would_change_view_if="short invalid if price closes back above breakdown area",
        )
        fundamental = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.46,
            opportunity_state="tradeable_candidate",
            setup_quality_score=0.75,
            business_quality_score=0.72,
            entry_trigger="enter only if futures hold above support after selloff and basis remains backwardation",
            would_change_view_if="long setup invalid if basis flips to contango or inventory builds for two consecutive weeks",
            horizon_class="medium",
        )

        card = build_opportunity_scorecard(
            ticker="ZN",
            analyst_signals=[technical, fundamental],
            market_confirmation={"confirmation_score": 0.78},
            data_quality_summary={"critical_gap": False, "fundamental_trade_setup_gap": False},
            config={
                "tradeable_threshold": 0.58,
                "min_tradeable_candidate_setup_quality": 0.55,
                "single_tradeable_candidate_setup_confirmation_score": 0.68,
                "technical_opposition_min_confidence": 0.45,
            },
        )

        self.assertFalse(card["long"]["single_tradeable_candidate_setup_confirmed"])
        self.assertTrue(card["long"]["technical_opposes_side"])


class FundamentalFinalizationStateAtomicityTest(unittest.TestCase):
    @staticmethod
    def _finalize(
        *,
        supports_trade_setup: bool,
        trigger_confirmed: bool,
        entry_trigger: str = "15-minute close above 3100 with volume expansion",
        invalidation_condition: str = "setup invalid if price closes below 3050",
        opportunity_state: str = "tradeable_candidate",
    ) -> AnalystSignal:
        seed = build_test_aec(
            "fundamental",
            ticker="M",
            trading_date="2025-03-26",
            signal="Bullish",
            side="long",
            confidence=0.82,
            opportunity_state=opportunity_state,
            setup_type="inventory_tightness",
            setup_quality_ok=True,
            trigger_valid=trigger_confirmed,
            current_trigger_confirmed=trigger_confirmed,
            invalidation_present=bool(invalidation_condition),
            entry_trigger=entry_trigger,
            invalidation_condition=invalidation_condition or None,
        )
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.82,
            entry_trigger=entry_trigger,
            exit_hint=invalidation_condition,
            holding_period_hint="1-3 trading days while the setup remains valid",
            invalidation_level=3050.0 if invalidation_condition else None,
            business_quality_score=0.82,
            factor_alignment_score=0.78,
            data_coverage_score=0.82,
            setup_type="inventory_tightness",
            horizon_class="short",
            opportunity_state=opportunity_state,
            trigger_valid=trigger_confirmed,
            invalidation_present=bool(invalidation_condition),
            metadata={
                "data_usage_summary": seed["data_usage_summary"],
                "invalidation_condition": invalidation_condition,
                "learning_scope": seed["learning_scope"],
                "product_profile_evidence": seed["product_profile_evidence"],
            },
        )
        return apply_trade_research_contract(
            signal,
            {
                "sector": "agricultural",
                "tradeability": "high",
                "setup_quality_ok": True,
                "current_trigger_confirmed": trigger_confirmed,
                "fundamental_deployable_confirmed": True,
                "data_quality": {
                    "supports_fundamental_trade_setup": supports_trade_setup,
                    "coverage_ratio": 0.55 if not supports_trade_setup else 0.82,
                },
                "factor_group_counts": {"inventory": {"down": 2}},
                "risk_flags": [],
            },
            analyst="fundamental",
            trading_date="2025-03-26",
            ticker="M",
        )

    def test_quality_downgrade_atomically_clears_confirmed_trigger_state(self):
        result = self._finalize(
            supports_trade_setup=False,
            trigger_confirmed=True,
        )
        contract = result.metadata["action_evidence_contract"]

        self.assertEqual(result.opportunity_state, "watch_for_trigger")
        validate_action_evidence_contract(contract, analyst="fundamental")
        self.assertFalse(result.trigger_valid)
        self.assertFalse(contract["trigger_valid"])
        self.assertFalse(contract["current_trigger_confirmed"])
        self.assertEqual(
            contract["entry_trigger"],
            "15-minute close above 3100 with volume expansion",
        )
        self.assertEqual(
            contract["invalidation_condition"],
            "setup invalid if price closes below 3050",
        )

    def test_quality_approved_confirmed_candidate_remains_confirmed(self):
        result = self._finalize(
            supports_trade_setup=True,
            trigger_confirmed=True,
        )
        contract = result.metadata["action_evidence_contract"]

        self.assertIn(result.opportunity_state, {"probe_candidate", "tradeable_candidate"})
        self.assertTrue(result.trigger_valid)
        self.assertTrue(contract["trigger_valid"])
        self.assertTrue(contract["current_trigger_confirmed"])
        validate_action_evidence_contract(contract, analyst="fundamental")

    def test_complete_unconfirmed_setup_remains_pending_watch(self):
        result = self._finalize(
            supports_trade_setup=True,
            trigger_confirmed=False,
            entry_trigger="enter only if the 15-minute close breaks above 3100 with volume",
        )
        contract = result.metadata["action_evidence_contract"]

        self.assertEqual(result.opportunity_state, "watch_for_trigger")
        self.assertFalse(result.trigger_valid)
        self.assertFalse(contract["trigger_valid"])
        self.assertFalse(contract["current_trigger_confirmed"])
        validate_action_evidence_contract(contract, analyst="fundamental")

    def test_incomplete_setup_becomes_no_opportunity(self):
        result = self._finalize(
            supports_trade_setup=True,
            trigger_confirmed=False,
            entry_trigger="",
            invalidation_condition="",
        )
        contract = result.metadata["action_evidence_contract"]

        self.assertEqual(result.opportunity_state, "no_opportunity")
        self.assertFalse(result.trigger_valid)
        self.assertFalse(contract["trigger_valid"])
        self.assertFalse(contract["current_trigger_confirmed"])
        validate_action_evidence_contract(contract, analyst="fundamental")

    def test_risk_reduction_state_remains_outside_new_risk_mapping(self):
        result = self._finalize(
            supports_trade_setup=False,
            trigger_confirmed=True,
            opportunity_state="risk_reduction_candidate",
        )

        self.assertEqual(result.opportunity_state, "risk_reduction_candidate")
        self.assertEqual(
            result.metadata["action_evidence_contract"]["opportunity_state"],
            "risk_reduction_candidate",
        )


class TechnicalPreOpenDataBoundaryTest(unittest.TestCase):
    def test_pre_open_technical_window_accepts_completed_t_minus_one_bar(self):
        prices = pd.DataFrame(
            {"close": [100.0, 101.0]},
            index=pd.to_datetime(["2025-03-03", "2025-03-04"]),
        )

        latest = _validate_pre_open_price_window(
            ticker="M",
            trading_date="2025-03-05",
            prices_df=prices,
            pre_open_only=True,
        )

        self.assertEqual(latest, "2025-03-04")

    def test_pre_open_technical_window_rejects_t_day_bar(self):
        prices = pd.DataFrame(
            {"close": [100.0, 101.0]},
            index=pd.to_datetime(["2025-03-04", "2025-03-05"]),
        )

        with self.assertRaisesRegex(RuntimeError, "expected latest price date < trading_date"):
            _validate_pre_open_price_window(
                ticker="M",
                trading_date="2025-03-05",
                prices_df=prices,
                pre_open_only=True,
            )


class FundamentalDataBoundaryTest(unittest.TestCase):
    def _build_router(self):
        router = Router.__new__(Router)
        router.market_type = "china_futures"
        router.config = {}
        router.api = SimpleNamespace(
            get_futures_daily_candles_optimized=lambda **_kwargs: [
                SimpleNamespace(trade_date="2025-03-03"),
                SimpleNamespace(trade_date="2025-03-04"),
            ],
            get_trade_dates=lambda start_date, end_date, underlying_code=None: [],
            get_continuous_candles=lambda underlying_code, start_date, end_date: [],
        )
        router.last_fundamentals_metadata = None
        router.last_news_metadata = None
        return router

    def test_fundamental_loader_skips_undated_indicator_instead_of_using_all_rows(self):
        router = self._build_router()
        df = pd.DataFrame({"bu_future_close_price": [100.0, 999.0]})

        with patch("apis.router.Path.exists", return_value=True), patch(
            "apis.router.read_finoview_feather_cached",
            return_value=df,
        ):
            result = router.get_china_futures_fundamentals("BU", "2025-03-05")

        self.assertIsNone(result)
        metadata = router.last_fundamentals_metadata
        self.assertEqual(metadata["undated_indicator_count"], metadata["configured_indicator_count"])
        self.assertEqual(metadata["loaded_indicator_count"], 0)

    def test_fundamental_loader_rejects_missing_value_column_instead_of_fabricating_zero(self):
        router = self._build_router()
        df = pd.DataFrame(
            {
                "tradeDate": pd.to_datetime(["2025-03-03", "2025-03-04"]),
                "unexpected_column": [7.0, 8.0],
            }
        )

        with patch("apis.router.Path.exists", return_value=True), patch(
            "apis.router.read_finoview_feather_cached",
            return_value=df,
        ):
            result = router.get_china_futures_fundamentals("BU", "2025-03-05")

        self.assertIsNone(result)
        self.assertEqual(router.last_fundamentals_metadata["loaded_indicator_count"], 0)

    def test_fundamental_basis_uses_previous_trading_day_price_not_t_day_close(self):
        router = self._build_router()

        def fake_read(path):
            filename = path.stem
            if filename == "bu_future_close_price":
                return pd.DataFrame(
                    {
                        "tradeDate": pd.to_datetime(["2025-03-03", "2025-03-04"]),
                        filename: [3300.0, 3400.0],
                    }
                )
            if filename == "bu_spot_price":
                return pd.DataFrame(
                    {
                        "tradeDate": pd.to_datetime(["2025-03-04"]),
                        filename: [3500.0],
                    }
                )
            return pd.DataFrame(
                {
                    "tradeDate": pd.to_datetime(["2025-03-04"]),
                    filename: [1.0],
                }
            )

        router.api = SimpleNamespace(
            get_futures_daily_candles_optimized=lambda **_kwargs: [
                SimpleNamespace(trade_date="2025-03-03"),
                SimpleNamespace(trade_date="2025-03-04"),
            ],
            get_trade_dates=lambda start_date, end_date, underlying_code=None: [
                SimpleNamespace(trade_date="2025-03-03"),
                SimpleNamespace(trade_date="2025-03-04"),
                SimpleNamespace(trade_date="2025-03-05"),
            ],
            get_continuous_candles=lambda underlying_code, start_date, end_date: [
                SimpleNamespace(close=3300.0, trade_date="2025-03-03"),
                SimpleNamespace(close=3400.0, trade_date="2025-03-04"),
            ],
        )

        with patch("apis.router.Path.exists", return_value=True), patch(
            "apis.router.read_finoview_feather_cached",
            side_effect=fake_read,
        ), patch(
            "apis.router.get_previous_trading_day",
            return_value=pd.Timestamp("2025-03-04"),
        ), patch("apis.router.logger.info") as info_log:
            result = router.get_china_futures_fundamentals("BU", "2025-03-05")

        self.assertIsNotNone(result)
        basis = router.last_fundamentals_metadata["basis"]
        self.assertEqual(basis["date"], "2025-03-04")
        self.assertEqual(basis["pre_open_reference_date"], "2025-03-04")
        self.assertEqual(basis["futures_price"], 3400.0)
        logged = "\n".join(str(call.args[0]) for call in info_log.call_args_list)
        self.assertNotIn("latest=", logged)
        self.assertNotIn("Basis:", logged)
        self.assertNotIn("Formatted fundamental data", logged)


class NewsDataBoundaryTest(unittest.TestCase):
    def _build_router(self, news_dir: Path):
        router = Router.__new__(Router)
        router.market_type = "china_futures"
        router.config = {"factor_data": {"news": {"data_dir": str(news_dir)}}}
        router.last_fundamentals_metadata = None
        router.last_news_metadata = None
        return router

    def test_news_loader_pre_open_excludes_t_day_and_future_news(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            news_dir = Path(tmpdir)
            (news_dir / "BU.txt").write_text(
                "\n".join(
                    [
                        "2025-03-04",
                        "T minus one catalyst",
                        "库存下降，现货成交改善。",
                        "inventory",
                        "local",
                        "",
                        "2025-03-05",
                        "T day catalyst",
                        "盘中新增政策消息。",
                        "policy",
                        "local",
                        "",
                        "2025-03-06",
                        "Future catalyst",
                        "未来新闻不应进入当前分析。",
                        "policy",
                        "local",
                    ]
                ),
                encoding="utf-8",
            )
            router = self._build_router(news_dir)

            with patch(
                "apis.router.get_previous_trading_day",
                return_value=pd.Timestamp("2025-03-04"),
            ):
                pre_open_news = router.get_china_futures_news(
                    "BU",
                    "2025-03-05",
                    news_count=10,
                    pre_open_only=True,
                )

            self.assertEqual([item.title for item in pre_open_news], ["T minus one catalyst"])
            self.assertEqual(router.last_news_metadata["news_cutoff"], "<=2025-03-04")
            self.assertEqual(router.last_news_metadata["latest_news_date"], "2025-03-04")

    def test_news_loader_non_pre_open_includes_t_day_but_excludes_future_news(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            news_dir = Path(tmpdir)
            (news_dir / "BU.txt").write_text(
                "\n".join(
                    [
                        "2025-03-04",
                        "T minus one catalyst",
                        "库存下降，现货成交改善。",
                        "inventory",
                        "local",
                        "",
                        "2025-03-05",
                        "T day catalyst",
                        "盘中新增政策消息。",
                        "policy",
                        "local",
                        "",
                        "2025-03-06",
                        "Future catalyst",
                        "未来新闻不应进入当前分析。",
                        "policy",
                        "local",
                    ]
                ),
                encoding="utf-8",
            )
            router = self._build_router(news_dir)

            intraday_news = router.get_china_futures_news(
                "BU",
                "2025-03-05",
                news_count=10,
                pre_open_only=False,
            )

            self.assertEqual(
                [item.title for item in intraday_news],
                ["T day catalyst", "T minus one catalyst"],
            )
            self.assertEqual(router.last_news_metadata["news_cutoff"], "<=2025-03-05")
            self.assertEqual(router.last_news_metadata["latest_news_date"], "2025-03-05")

    def test_pre_open_news_stops_at_formal_prev_t_not_weekend_calendar_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            news_dir = Path(tmpdir)
            (news_dir / "BU.txt").write_text(
                "\n".join(
                    [
                        "2025-01-03",
                        "Friday visible news",
                        "Friday fact remains visible for Monday proposal.",
                        "inventory",
                        "local",
                        "",
                        "2025-01-05",
                        "Sunday future-to-cutoff news",
                        "Sunday publication is after formal Prev(T).",
                        "policy",
                        "local",
                    ]
                ),
                encoding="utf-8",
            )
            router = self._build_router(news_dir)

            with patch(
                "apis.router.get_previous_trading_day",
                return_value=pd.Timestamp("2025-01-03"),
            ):
                news = router.get_china_futures_news(
                    "BU",
                    "2025-01-06",
                    news_count=10,
                    pre_open_only=True,
                )

            self.assertEqual([item.title for item in news], ["Friday visible news"])
            self.assertEqual(router.last_news_metadata["news_cutoff"], "<=2025-01-03")


if __name__ == "__main__":
    unittest.main()
