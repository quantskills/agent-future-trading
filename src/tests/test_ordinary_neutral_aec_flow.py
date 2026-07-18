from __future__ import annotations

import sys
import unittest
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.analysis_team import commodity_news, fundamental, technical
from graph.constants import Signal
from graph.schema import AnalystSignal, Portfolio
from graph.workflow import AgentWorkflow
from llm.prompt import (
    build_futures_commodity_news_prompt,
    build_futures_fundamental_prompt,
    build_futures_technical_prompt,
)
from tests.contract_test_fixtures import build_test_aec, build_test_signal
from tools.agent_tools.analysis.analyst_output_finalization import finalize_analyst_signal
from tools.agent_tools.analysis.analyst_product_price_behavior_profile import (
    build_profile_usage_contract,
    get_product_price_behavior_profile,
)
from tools.agent_tools.control.pg_pre_backtest_acceptance import _TEST_GROUPS
from tools.agent_tools.decision.pm_ticker_side_selection import select_ticker_side
from tools.agent_tools.research.research_review_helpers import (
    _neutral_contract_from_payload,
    _neutral_opportunity_observations,
    _primary_opportunity_state,
)
from tools.common.signal_evidence_collection import (
    build_pm_evidence_signals_from_scc,
    build_signal_collection_contract,
    validate_action_evidence_contract,
    validate_signal_collection_contract,
)
from tools.common.execution_trigger_semantics import canonical_entry_trigger


ANALYSTS = ("technical", "fundamental", "commodity_news")
TRADING_DATE = "2025-03-26"


def _data_usage(analyst: str) -> dict:
    return build_test_aec(
        analyst,
        ticker="BU",
        trading_date=TRADING_DATE,
    )["data_usage_summary"]


def _quality_context(analyst: str) -> dict:
    base = {
        "ticker": "BU",
        "sector": "energy",
        "tradeability": "medium",
        "risk_flags": [],
        "setup_quality_ok": False,
        "market_regime": "range",
    }
    if analyst == "technical":
        base.update(
            {
                "dominant_direction": "neutral",
                "indicator_votes": {"details": {}},
                "features": {},
            }
        )
    elif analyst == "fundamental":
        base.update(
            {
                "factor_group_counts": {"inventory": 1},
                "data_quality": {
                    "coverage_ratio": 1.0,
                    "factor_freshness_score": 1.0,
                    "supports_fundamental_trade_setup": True,
                    "no_lookahead_status": "ok",
                },
            }
        )
    else:
        base.update(
            {
                "tradable_event": False,
                "price_reaction_required": True,
                "price_reaction_confirmed": False,
                "direction_counts": {},
                "event_type_counts": {"inventory": 1},
                "event_regime": "background_news",
            }
        )
    return base


def _ordinary_neutral(analyst: str) -> AnalystSignal:
    return AnalystSignal(
        agent_name=analyst,
        signal=Signal.NEUTRAL,
        confidence=0.42,
        justification="Available evidence is mixed and does not define a trade setup.",
        horizon_class="event_short" if analyst == "commodity_news" else "medium" if analyst == "fundamental" else "short",
        expected_horizon_days=2,
        market_regime="range",
        setup_type="unknown",
        opportunity_type="no_trade",
        opportunity_state="no_opportunity",
        entry_trigger="",
        exit_hint="",
        trigger_valid=False,
        invalidation_present=False,
        neutral_reason="available evidence has no complete setup",
        missing_evidence=["specific_entry_trigger", "canonical_invalidation"],
        conflicting_factors=[],
        would_change_view_if="new evidence changes the current view",
        neutral_opportunity_bucket="evidence_gap",
        neutral_trigger_condition="",
        counterfactual_side="flat",
        neutral_watchlist_priority="none",
        metadata={"data_usage_summary": _data_usage(analyst)},
    )


def _finalize(
    analyst: str,
    signal: AnalystSignal,
    *,
    invalidation_condition: str = "",
) -> AnalystSignal:
    metadata = dict(signal.metadata or {})
    metadata["data_usage_summary"] = _data_usage(analyst)
    if invalidation_condition:
        metadata["invalidation_condition"] = invalidation_condition
    signal.metadata = metadata
    profile = get_product_price_behavior_profile("BU")
    usage = build_profile_usage_contract("BU", analyst, profile)
    return finalize_analyst_signal(
        signal,
        quality_context=_quality_context(analyst),
        full_config={"llm": {"provider": "test", "model": "deterministic-neutral"}},
        analyst=analyst,
        ticker="BU",
        trading_date=TRADING_DATE,
        learning_context={},
        product_profile=profile,
        product_profile_usage=usage,
    )


def _agent_state() -> dict:
    return {
        "ticker": "BU",
        "trading_date": datetime(2025, 3, 26),
        "market_type": "china_futures",
        "pre_open_only": True,
        "info_cutoff": "pre_open",
        "config_id": "cfg",
        "llm_config": {"provider": "test", "model": "deterministic-neutral"},
        "config": {},
        "full_config": {
            "llm": {"provider": "test", "model": "deterministic-neutral"},
            "learning": {"contextual_rule_calibration": {"enabled": False}},
        },
    }


class OrdinaryNeutralAnalystEntryTest(unittest.TestCase):
    def _assert_formal_neutral(self, output: dict, analyst: str) -> None:
        self.assertEqual(set(output), {"analyst_signals"})
        self.assertEqual(len(output["analyst_signals"]), 1)
        signal = output["analyst_signals"][0]
        contract = validate_action_evidence_contract(
            signal.metadata["action_evidence_contract"],
            analyst=analyst,
        )
        self.assertEqual(signal.signal, Signal.NEUTRAL)
        self.assertEqual(contract["signal"], "Neutral")
        self.assertEqual(contract["side"], "flat")
        self.assertEqual(contract["opportunity_state"], "no_opportunity")
        self.assertFalse(contract["trigger_valid"])
        self.assertFalse(contract["invalidation_present"])
        self.assertNotEqual(contract["entry_trigger"], "wait_for_trigger")

    def test_technical_entry_accepts_data_available_ordinary_neutral(self):
        frame = pd.DataFrame(
            {"close": [3000.0, 3010.0, 3005.0]},
            index=pd.to_datetime(["2025-03-21", "2025-03-24", "2025-03-25"]),
        )
        router = Mock()
        router.get_daily_candles_df.return_value = frame
        context = _quality_context("technical")
        with patch.object(technical, "Router", return_value=router), patch.object(
            technical, "get_db", return_value=Mock()
        ), patch.object(
            technical, "_validate_pre_open_price_window", return_value="2025-03-25"
        ), patch.object(
            technical, "calculate_market_features", return_value={"volatility": 0.1}
        ), patch.object(
            technical, "calculate_adaptive_params", return_value={}
        ), patch.object(
            technical,
            "_build_technical_signal_results",
            return_value={"trend": Signal.NEUTRAL, "gap_analysis": "pre_open"},
        ), patch.object(
            technical, "build_technical_context", return_value=context
        ), patch.object(
            technical, "resolve_config_id", return_value="cfg"
        ), patch.object(
            technical,
            "build_learning_context",
            return_value={"text": "", "selected_ids": [], "memory_trace": {}},
        ), patch.object(
            technical, "build_technical_data_usage", return_value=_data_usage("technical")
        ), patch.object(
            technical, "agent_call", return_value=_ordinary_neutral("technical")
        ), patch.object(technical.logger, "log_signal"):
            output = technical.technical_agent(_agent_state())
        self._assert_formal_neutral(output, "technical")

    def test_fundamental_entry_accepts_data_available_ordinary_neutral(self):
        router = Mock()
        router.get_china_futures_fundamentals.return_value = "inventory: available\nbasis: available"
        router.last_fundamentals_metadata = {"configured_indicator_count": 2, "loaded_indicator_count": 2}
        context = _quality_context("fundamental")
        with patch.object(fundamental, "Router", return_value=router), patch.object(
            fundamental, "get_db", return_value=Mock()
        ), patch.object(
            fundamental, "parse_fundamental_factors", return_value=context
        ), patch.object(
            fundamental, "resolve_config_id", return_value="cfg"
        ), patch.object(
            fundamental,
            "build_learning_context",
            return_value={"text": "", "selected_ids": [], "memory_trace": {}},
        ), patch.object(
            fundamental,
            "build_fundamental_data_usage",
            return_value=_data_usage("fundamental"),
        ), patch.object(
            fundamental, "agent_call", return_value=_ordinary_neutral("fundamental")
        ), patch.object(fundamental.logger, "log_signal"):
            output = fundamental.fundamental_agent(_agent_state())
        self._assert_formal_neutral(output, "fundamental")

    def test_news_entry_accepts_data_available_ordinary_neutral(self):
        router = Mock()
        router.get_china_futures_news.return_value = [
            SimpleNamespace(model_dump_json=lambda: '{"event":"inventory update"}')
        ]
        router.last_news_metadata = {"parsed_news_count": 1, "selected_news_count": 1}
        context = _quality_context("commodity_news")
        with patch.object(commodity_news, "Router", return_value=router), patch.object(
            commodity_news, "get_db", return_value=Mock()
        ), patch.object(
            commodity_news, "summarize_news_events", return_value=context
        ), patch.object(
            commodity_news, "resolve_config_id", return_value="cfg"
        ), patch.object(
            commodity_news,
            "build_learning_context",
            return_value={"text": "", "selected_ids": [], "memory_trace": {}},
        ), patch.object(
            commodity_news,
            "build_news_data_usage",
            return_value=_data_usage("commodity_news"),
        ), patch.object(
            commodity_news,
            "agent_call",
            return_value=_ordinary_neutral("commodity_news"),
        ), patch.object(commodity_news.logger, "log_signal"):
            output = commodity_news.commodity_news_agent(_agent_state())
        self._assert_formal_neutral(output, "commodity_news")


class OpportunityFinalizationTest(unittest.TestCase):
    def test_ordinary_neutral_is_no_opportunity_for_all_analysts(self):
        for analyst in ANALYSTS:
            with self.subTest(analyst=analyst):
                signal = _finalize(analyst, _ordinary_neutral(analyst))
                contract = signal.metadata["action_evidence_contract"]
                self.assertEqual(contract["signal"], "Neutral")
                self.assertEqual(contract["opportunity_state"], "no_opportunity")
                self.assertFalse(contract["trigger_valid"])
                self.assertFalse(contract["invalidation_present"])

    def test_only_technical_directional_neutral_can_form_execution_watch(self):
        for analyst in ANALYSTS:
            with self.subTest(analyst=analyst):
                signal = _ordinary_neutral(analyst)
                signal.counterfactual_side = "long"
                signal.neutral_opportunity_bucket = "watchlist_trigger"
                signal.entry_trigger = "long entry only after price closes above 3050 with volume confirmation"
                signal.neutral_trigger_condition = signal.entry_trigger
                if analyst == "technical":
                    signal.entry_timing_signal = "breakout"
                finalized = _finalize(
                    analyst,
                    signal,
                    invalidation_condition="long setup invalid if price closes below 2980",
                )
                contract = finalized.metadata["action_evidence_contract"]
                self.assertEqual(contract["signal"], "Neutral")
                expected_state = "watch_for_trigger" if analyst == "technical" else "no_opportunity"
                self.assertEqual(contract["opportunity_state"], expected_state)
                self.assertFalse(contract["trigger_valid"])
                self.assertFalse(contract["current_trigger_confirmed"])
                self.assertTrue(contract["invalidation_present"])
                if analyst != "technical":
                    self.assertEqual(contract["entry_timing_signal"], "")
                    self.assertEqual(contract["entry_trigger"], "")

    def test_neutral_missing_entry_trigger_is_no_opportunity(self):
        signal = _ordinary_neutral("technical")
        signal.counterfactual_side = "long"
        signal.neutral_opportunity_bucket = "watchlist_trigger"
        finalized = _finalize(
            "technical",
            signal,
            invalidation_condition="long setup invalid if price closes below 2980",
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["opportunity_state"], "no_opportunity")
        self.assertEqual(contract["entry_trigger"], "")

    def test_neutral_missing_canonical_invalidation_is_no_opportunity(self):
        signal = _ordinary_neutral("technical")
        signal.counterfactual_side = "long"
        signal.neutral_opportunity_bucket = "watchlist_trigger"
        signal.entry_trigger = "long entry only after price closes above 3050 with volume confirmation"
        signal.entry_timing_signal = "breakout"
        signal.neutral_trigger_condition = signal.entry_trigger
        signal.would_change_view_if = "view changes if price later closes below 2980"
        finalized = _finalize("technical", signal)
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["opportunity_state"], "no_opportunity")
        self.assertFalse(contract["invalidation_present"])
        self.assertNotIn("invalidation_condition", contract)

    def test_specific_producer_exit_condition_lands_in_canonical_invalidation(self):
        signal = _ordinary_neutral("technical")
        signal.counterfactual_side = "long"
        signal.neutral_opportunity_bucket = "watchlist_trigger"
        signal.entry_trigger = "long entry only after price closes above 3050 with volume confirmation"
        signal.entry_timing_signal = "breakout"
        signal.neutral_trigger_condition = signal.entry_trigger
        signal.exit_hint = "long setup invalid if price closes below 2980"
        finalized = _finalize("technical", signal)
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(
            contract["invalidation_condition"],
            "long setup invalid if price closes below 2980",
        )
        self.assertTrue(contract["invalidation_present"])
        self.assertEqual(contract["opportunity_state"], "watch_for_trigger")

    def test_neutral_is_never_promoted_when_current_trigger_is_marked_true(self):
        signal = _ordinary_neutral("technical")
        signal.counterfactual_side = "long"
        signal.entry_trigger = "price closes above 3050 with volume confirmation"
        signal.trigger_valid = True
        signal.opportunity_state = "tradeable_candidate"
        finalized = _finalize(
            "technical",
            signal,
            invalidation_condition="long setup invalid if price closes below 2980",
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["signal"], "Neutral")
        self.assertEqual(contract["opportunity_state"], "no_opportunity")
        self.assertFalse(contract["trigger_valid"])

    def test_risk_reduction_candidate_is_not_downgraded_by_new_risk_setup_rules(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.65,
            justification="Existing long exposure should be reduced because risk evidence worsened.",
            horizon_class="short",
            expected_horizon_days=1,
            market_regime="risk_off",
            opportunity_type="short_timing",
            opportunity_state="risk_reduction_candidate",
            entry_trigger="",
            exit_hint="reduce existing exposure under the current risk condition",
            trigger_valid=False,
            invalidation_present=False,
            metadata={"data_usage_summary": _data_usage("technical")},
        )
        finalized = _finalize("technical", signal)
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["opportunity_state"], "risk_reduction_candidate")


class SharedContractAndDownstreamTest(unittest.TestCase):
    def test_watch_rejects_placeholder_entry_trigger(self):
        contract = build_test_aec(
            "technical",
            ticker="BU",
            trading_date=TRADING_DATE,
            signal="Neutral",
            side="flat",
            opportunity_state="watch_for_trigger",
            trigger_valid=False,
            invalidation_present=True,
            entry_trigger="wait_for_trigger",
            invalidation_condition="long setup invalid if price closes below 2980",
            extra={
                "counterfactual_side": "long",
                "entry_timing_signal": "breakout",
            },
        )
        with self.assertRaisesRegex(ValueError, "action_evidence_contract_entry_trigger_not_canonical"):
            validate_action_evidence_contract(contract, analyst="technical")

    def test_invalidation_flag_requires_canonical_proof(self):
        contract = build_test_aec(
            "technical",
            ticker="BU",
            trading_date=TRADING_DATE,
            signal="Neutral",
            side="flat",
            opportunity_state="watch_for_trigger",
            trigger_valid=False,
            invalidation_present=True,
            entry_trigger=canonical_entry_trigger("breakout", "long"),
            extra={
                "counterfactual_side": "long",
                "entry_timing_signal": "breakout",
                "would_change_view_if": "view changes if price closes below 2980",
            },
        )
        contract.pop("invalidation_condition", None)
        with self.assertRaisesRegex(ValueError, "action_evidence_contract_invalidation_proof_missing"):
            validate_action_evidence_contract(contract, analyst="technical")

    def test_one_no_opportunity_analyst_does_not_veto_other_candidates(self):
        signals = [
            build_test_signal(
                "technical",
                signal_record_id="signal-technical",
                ticker="BU",
                trading_date=TRADING_DATE,
                signal="Neutral",
                side="flat",
                opportunity_state="no_opportunity",
                invalidation_present=False,
                entry_trigger="",
            ),
            build_test_signal(
                "fundamental",
                signal_record_id="signal-fundamental",
                ticker="BU",
                trading_date=TRADING_DATE,
                signal="Bullish",
                side="long",
                opportunity_state="no_opportunity",
                trigger_valid=False,
                current_trigger_confirmed=False,
                entry_trigger="",
                invalidation_condition="long setup invalid below 2980",
            ),
            build_test_signal(
                "commodity_news",
                signal_record_id="signal-news",
                ticker="BU",
                trading_date=TRADING_DATE,
                signal="Bullish",
                side="long",
                opportunity_state="probe_candidate",
                trigger_valid=True,
                current_trigger_confirmed=True,
                entry_trigger=None,
                invalidation_condition="long event setup invalid below 2980",
            ),
        ]
        scc = build_signal_collection_contract(
            ticker="BU",
            trading_date=TRADING_DATE,
            analyst_signals=signals,
            enabled_analysts=ANALYSTS,
        )
        validate_signal_collection_contract(
            scc,
            ticker="BU",
            trading_date=TRADING_DATE,
            enabled_analysts=ANALYSTS,
            analyst_signals=signals,
            require_signal_record_ids=True,
        )
        pm_signals = build_pm_evidence_signals_from_scc(scc)
        selected = select_ticker_side(
            ticker="BU",
            analyst_signals=pm_signals,
            signal_collection_contract=scc,
            market_confirmation={},
            data_quality_summary={},
            decision_date=TRADING_DATE,
            config={},
        )
        self.assertEqual(scc["dominant_side"], "long")
        self.assertEqual(selected["opportunity_scorecard"]["preferred_side"], "long")
        candidates = selected["ticker_side_selection_trace"]["candidates"]
        self.assertTrue(
            any(
                row["candidate_eligible"]
                for row in candidates
                if row["side"] == "long"
            )
        )

    def test_research_reads_formal_neutral_fields_without_default_watch_or_action(self):
        payload = build_test_aec(
            "technical",
            ticker="BU",
            trading_date=TRADING_DATE,
            signal="Neutral",
            side="flat",
            opportunity_state="no_opportunity",
            invalidation_present=False,
            entry_trigger="",
            extra={
                "neutral_opportunity_bucket": "evidence_gap",
                "neutral_trigger_condition": "price and volume confirmation becomes available",
                "counterfactual_side": "long",
                "neutral_watchlist_priority": "low",
            },
        )
        formal = _neutral_contract_from_payload(payload)
        self.assertEqual(formal["opportunity_state"], "no_opportunity")
        self.assertFalse(formal["trigger_valid"])
        self.assertFalse(formal["invalidation_present"])
        self.assertEqual(formal["entry_trigger"], "")
        self.assertNotIn("action_preference", formal)

        snapshot = {
            "signal_collection_contract": {
                "source_contracts": [
                    {"analyst": "technical", "action_evidence_contract": payload}
                ]
            }
        }
        observations = _neutral_opportunity_observations(snapshot)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["opportunity_state"], "no_opportunity")
        self.assertNotIn("action_preference", observations[0])
        self.assertEqual(_primary_opportunity_state(snapshot), "no_opportunity")
        self.assertEqual(_primary_opportunity_state({}), "no_opportunity")


class WorkflowAndPreBacktestBoundaryTest(unittest.TestCase):
    def test_workflow_exposes_stable_contract_code_and_persists_nothing_on_failure(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        workflow._build_futures_phase1_analysis_state = Mock(
            return_value={"ticker": "BU", "analyst_signals": []}
        )
        workflow.db = Mock()
        workflow.db.phase1_write_scope.return_value = nullcontext()

        def good_agent(_state):
            return {"analyst_signals": [SimpleNamespace(agent_name="technical")]}

        def invalid_agent(_state):
            raise ValueError(
                "analyst_final_output_contract_invalid:private_internal_contract_details"
            )

        agents = {"technical": good_agent, "fundamental": invalid_agent}
        with patch(
            "graph.workflow.AgentRegistry.get_agent_func_by_key",
            side_effect=lambda name: agents[name],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "^analyst_final_output_contract_invalid$",
            ):
                workflow._run_phase1_analysis_only(
                    "BU",
                    Mock(),
                    SimpleNamespace(base_price=3000.0),
                    ["technical", "fundamental"],
                )
        workflow.db.save_signal.assert_not_called()
        workflow.db.save_futures_recommendation.assert_not_called()

    def test_outer_phase1_preserves_stable_contract_failure_and_no_partial_writes(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        portfolio = Portfolio(
            id="portfolio-1",
            cashflow=1_000_000.0,
            account_equity=1_000_000.0,
            positions={},
            risk_status="NORMAL",
        )
        workflow.config = {}
        workflow.init_portfolio = portfolio
        workflow.tickers = ["BU"]
        workflow.workflow_analysts = list(ANALYSTS)
        workflow.current_analysts = None
        workflow.planner_mode = False
        workflow.db = Mock()
        workflow.db.phase1_write_scope.return_value = nullcontext()
        workflow._apply_virtual_pending_rollovers = Mock(return_value=portfolio)
        workflow._prefetch_local_daily_data = Mock(return_value={})
        workflow._prefetch_pandaai_daily_data = Mock(return_value={})
        workflow._prefetch_pre_open_reference_prices = Mock(
            return_value={"BU": SimpleNamespace(base_price=3000.0, contract_facts={})}
        )
        workflow._prefetch_phase1_analysis = Mock(return_value={})
        workflow.load_analysts = Mock()
        workflow._run_phase1_analysis_only = Mock(
            side_effect=RuntimeError("analyst_final_output_contract_invalid")
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "^analyst_final_output_contract_invalid$",
        ):
            workflow._run_futures_phase1()
        workflow.db.save_signal.assert_not_called()
        workflow.db.save_futures_recommendation.assert_not_called()

    def test_prebacktest_runs_ordinary_neutral_behavior_suite(self):
        self.assertIn(
            "src.tests.test_ordinary_neutral_aec_flow",
            _TEST_GROUPS["orchestration_state_and_physical_boundary"],
        )

    def test_no_llm_dry_run_has_data_available_neutral_builder(self):
        from tools.agent_tools.control import pg_full_chain_dry_run as dry_run

        builder = getattr(dry_run, "_build_dry_run_data_available_neutral_signal")
        for analyst in ANALYSTS:
            with self.subTest(analyst=analyst):
                signal = builder(
                    analyst=analyst,
                    ticker="BU",
                    trading_date=datetime(2025, 3, 10),
                    full_config={"llm": {"provider": "test", "model": "test"}},
                )
                contract = validate_action_evidence_contract(
                    signal.metadata["action_evidence_contract"],
                    analyst=analyst,
                )
                self.assertEqual(contract["signal"], "Neutral")
                self.assertEqual(contract["opportunity_state"], "no_opportunity")
                self.assertTrue(contract["data_usage_summary"]["data_available"])

    def test_three_prompts_define_the_same_neutral_boundary(self):
        prompts = (
            build_futures_technical_prompt(
                ticker="BU",
                signal_results_compact={"trend": "FLAT"},
            ),
            build_futures_fundamental_prompt(
                ticker="BU",
                fundamentals="inventory data available",
            ),
            build_futures_commodity_news_prompt(
                ticker="BU",
                instrument_context="bitumen",
                news=[],
            ),
        )
        for prompt_text in prompts:
            prompt_text = prompt_text.lower()
            self.assertIn("ordinary neutral", prompt_text)
            self.assertIn("no_opportunity", prompt_text)
            self.assertIn("concrete entry_trigger", prompt_text)
            self.assertIn("canonical invalidation", prompt_text)
            self.assertIn("must not use watch_for_trigger", prompt_text)


if __name__ == "__main__":
    unittest.main()
