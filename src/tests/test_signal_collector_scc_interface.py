import inspect
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.decision_team.signal_collector import signal_collector_agent
from agents.decision_team import portfolio_manager
from agents.decision_team.portfolio_manager import finalize_pm_full_market_contracts
from graph.schema import Portfolio
from graph.workflow import AgentWorkflow
from tests.contract_test_fixtures import build_test_aec
from tests.test_pm_atomic_contract_flow import _pm_state
from tools.agent_tools.decision import pm_signal_fusion
from tools.agent_tools.decision.pm_invalidation_policy import (
    _has_structured_invalidation_condition,
)
from tools.common.signal_evidence_collection import (
    build_pm_evidence_signals_from_scc,
    build_signal_collection_contract,
    validate_action_evidence_contract,
    validate_signal_collection_contract,
)


def _formal_signal(
    analyst: str,
    *,
    side: str,
    confidence: float = 0.6,
    trigger_valid: bool = False,
    trigger_confirmed: bool = False,
    signal_record_id: str | None = None,
    raw_signal: str = "Neutral",
    freshness_score: float = 0.82,
    strength_score: float = 0.72,
):
    signal_text = "Bullish" if side == "long" else "Bearish" if side == "short" else "Neutral"
    contract = build_test_aec(
        analyst,
        signal=signal_text,
        side=side,
        confidence=confidence,
        trigger_valid=trigger_valid,
        current_trigger_confirmed=trigger_confirmed,
    )
    contract["product_profile_evidence"] = {
            "product_profile_id": "BU.default",
            "product_profile_used": True,
            "profile_analysis_boundary": "analyst_evidence_calibration_only",
    }
    contract["fusion_evidence"].update(
        {
            "evidence_strength": "strong" if strength_score >= 0.78 else "medium" if strength_score >= 0.58 else "weak",
            "evidence_strength_score": strength_score,
            "evidence_freshness": "fresh" if freshness_score >= 0.78 else "usable" if freshness_score >= 0.50 else "stale",
            "evidence_freshness_score": freshness_score,
        }
    )
    return SimpleNamespace(
        agent_name=analyst,
        signal=raw_signal,
        confidence=0.01,
        trigger_valid=not trigger_valid,
        metadata={
            "action_evidence_contract": contract,
            "signal_record_id": signal_record_id or f"{analyst}-signal-id",
        },
    )


class SignalCollectorSccInterfaceTest(unittest.TestCase):
    def test_collector_reads_formal_contract_not_conflicting_raw_fields(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _formal_signal("technical", side="long", raw_signal="Bearish"),
                _formal_signal("fundamental", side="long", raw_signal="Bearish"),
                _formal_signal("commodity_news", side="flat", raw_signal="Bearish"),
            ],
        )
        self.assertEqual(contract["dominant_side"], "long")
        self.assertEqual(contract["evidence_items"][0]["confidence"], 0.6)

    def test_cross_horizon_opposition_is_position_risk_not_entry_conflict(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _formal_signal("technical", side="long"),
                _formal_signal("fundamental", side="short"),
                _formal_signal("commodity_news", side="flat"),
            ],
        )
        self.assertEqual(contract["dominant_side"], "long")
        self.assertEqual(contract["side_consensus"], "single_analyst_support")
        self.assertEqual(contract["evidence_conflict_level"], "low")
        self.assertEqual(contract["opposing_analysts"], ["fundamental"])
        self.assertEqual(contract["evidence_fusion"]["cross_analyst_conflicts"], [])
        self.assertEqual(
            [row["analyst"] for row in contract["evidence_fusion"]["dominant_opposing_evidence"]],
            ["fundamental"],
        )

    def test_same_horizon_news_opposition_remains_entry_conflict(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _formal_signal("technical", side="long"),
                _formal_signal("fundamental", side="flat"),
                _formal_signal("commodity_news", side="short"),
            ],
        )
        self.assertEqual(contract["dominant_side"], "long")
        self.assertEqual(contract["side_consensus"], "conflicted")
        self.assertEqual(contract["evidence_conflict_level"], "medium")
        self.assertEqual(
            contract["evidence_fusion"]["cross_analyst_conflicts"][0]["analyst"],
            "commodity_news",
        )

    def test_neutral_context_does_not_dilute_technical_entry_consensus(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _formal_signal("technical", side="long", strength_score=0.72),
                _formal_signal("fundamental", side="flat", strength_score=0.0),
                _formal_signal("commodity_news", side="flat", strength_score=0.0),
            ],
        )
        self.assertGreaterEqual(
            contract["evidence_fusion"]["multi_evidence_consensus_score"],
            0.8,
        )

    def test_neutral_context_conflicts_and_missing_do_not_penalize_entry(self):
        technical = _formal_signal("technical", side="long", strength_score=0.72)
        fundamental = _formal_signal("fundamental", side="flat", strength_score=0.0)
        news = _formal_signal("commodity_news", side="flat", strength_score=0.0)
        for signal, marker in ((fundamental, "fundamental_neutral_gap"), (news, "news_neutral_gap")):
            fusion = signal.metadata["action_evidence_contract"]["fusion_evidence"]
            fusion["current_evidence_conflict"] = [marker]
            fusion["missing_evidence"] = [marker]
            fusion["confirmation_requirements"] = [marker]

        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[technical, fundamental, news],
        )

        fusion = contract["evidence_fusion"]
        self.assertGreaterEqual(fusion["multi_evidence_consensus_score"], 0.8)
        self.assertEqual(fusion["cross_analyst_conflicts"], [])
        self.assertEqual(fusion["missing_evidence"], [])
        self.assertEqual(fusion["confirmation_requirements"], [])

    def test_analyst_internal_conflict_still_reaches_pm_risk_without_changing_direction(self):
        technical = _formal_signal("technical", side="long")
        technical.metadata["action_evidence_contract"]["fusion_evidence"][
            "current_evidence_conflict"
        ] = ["false_breakout_risk"]
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                technical,
                _formal_signal("fundamental", side="flat"),
                _formal_signal("commodity_news", side="flat"),
            ],
        )
        self.assertEqual(contract["dominant_side"], "long")
        self.assertEqual(contract["side_consensus"], "single_analyst_support")
        self.assertEqual(
            contract["evidence_fusion"]["cross_analyst_conflicts"],
            [
                {
                    "analyst": "technical",
                    "side": "long",
                    "conflicts": ["false_breakout_risk"],
                }
            ],
        )
        confirmation = pm_signal_fusion.build_scc_market_confirmation(
            contract,
            target_direction="long",
        )
        self.assertTrue(confirmation["conflicts"])
        diagnostics = pm_signal_fusion.build_pm_fusion_diagnostics(contract)
        self.assertEqual(diagnostics["cross_analyst_conflict_count"], 1)
        self.assertTrue(diagnostics["requires_pm_conflict_resolution"])

    def test_trigger_status_only_uses_dominant_side_evidence(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _formal_signal("technical", side="long", trigger_valid=False),
                _formal_signal("fundamental", side="long", trigger_valid=False),
                _formal_signal(
                    "commodity_news",
                    side="short",
                    trigger_valid=True,
                    trigger_confirmed=True,
                ),
            ],
        )
        self.assertEqual(contract["dominant_side"], "long")
        self.assertEqual(contract["trigger_status"], "watch_for_trigger")

    def test_fusion_fields_use_registered_nested_location_only(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _formal_signal("technical", side="long"),
                _formal_signal("fundamental", side="long"),
                _formal_signal("commodity_news", side="flat"),
            ],
        )
        nested_only = {
            "evidence_strength_by_analyst",
            "evidence_freshness_by_analyst",
            "evidence_alignment_state",
            "cross_analyst_conflicts",
            "dominant_opposing_evidence",
            "multi_evidence_consensus_score",
        }
        self.assertTrue(nested_only.issubset(contract["evidence_fusion"]))
        self.assertNotIn("direction_alignment", contract["evidence_fusion"])
        self.assertFalse(nested_only.intersection(contract))
        self.assertEqual(
            contract["evidence_fusion"]["evidence_freshness_by_analyst"],
            {
                "commodity_news": "fresh",
                "fundamental": "fresh",
                "technical": "fresh",
            },
        )
        self.assertGreater(contract["evidence_fusion"]["multi_evidence_consensus_score"], 0.0)

    def test_scc_preserves_each_aec_once_without_profile_or_fusion_copies(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _formal_signal("technical", side="long"),
                _formal_signal("fundamental", side="long"),
                _formal_signal("commodity_news", side="flat"),
            ],
        )
        self.assertEqual(
            set(contract["source_contracts"][0]),
            {"analyst", "action_evidence_contract", "signal_record_id"},
        )
        self.assertNotIn("fusion_evidence", contract["evidence_items"][0])
        self.assertIn(
            "fusion_evidence",
            contract["source_contracts"][0]["action_evidence_contract"],
        )
        self.assertIn(
            "product_profile_evidence",
            contract["source_contracts"][0]["action_evidence_contract"],
        )
        self.assertIn(
            "position_invalidation_level",
            contract["source_contracts"][0]["action_evidence_contract"],
        )
        self.assertNotIn("position_invalidation_level", contract)
        self.assertNotIn("position_invalidation_level", contract["evidence_items"][0])

    def test_scc_data_quality_flags_are_derived_from_nested_source_facts(self):
        technical = _formal_signal("technical", side="flat")
        fundamental = _formal_signal("fundamental", side="flat")
        news = _formal_signal("commodity_news", side="flat")
        technical.metadata["action_evidence_contract"]["data_usage_summary"] = {
            "ticker": "BU",
            "trading_date": "2025-03-25",
            "analyst": "technical",
            "sources": {
                "pandaai_market": {
                    "source": "PandaAI",
                    "dataset": "daily_continuous_candles",
                    "available": False,
                    "used_in_signal": True,
                    "pre_open_only": True,
                    "info_cutoff": "pre_open",
                }
            },
        }
        fundamental.metadata["action_evidence_contract"]["data_usage_summary"] = {
            "ticker": "BU",
            "trading_date": "2025-03-25",
            "analyst": "fundamental",
            "sources": {
                "finoview_fundamental": {
                    "source": "Finoview",
                    "dataset": "local_feather_fundamental",
                    "available": True,
                    "used_in_signal": True,
                    "pre_open_only": True,
                    "info_cutoff": "pre_open",
                    "stale_indicator_count": 2,
                    "supports_trade_setup": False,
                }
            },
        }
        news.metadata["action_evidence_contract"]["data_usage_summary"] = {
            "ticker": "BU",
            "trading_date": "2025-03-25",
            "analyst": "commodity_news",
            "sources": {
                "finoview_news_txt": {
                    "source": "Finoview",
                    "dataset": "local_news_txt",
                    "available": True,
                    "used_in_signal": False,
                    "pre_open_only": True,
                    "info_cutoff": "pre_open",
                }
            },
        }
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[technical, fundamental, news],
        )
        self.assertIn(
            "data_source_unavailable:technical:pandaai_market",
            contract["data_quality_flags"],
        )
        self.assertIn(
            "data_source_stale:fundamental:finoview_fundamental",
            contract["data_quality_flags"],
        )
        self.assertIn(
            "data_source_not_used:commodity_news:finoview_news_txt",
            contract["data_quality_flags"],
        )

    def test_duplicate_and_non_enabled_sources_are_contract_errors(self):
        with self.assertRaisesRegex(ValueError, "duplicate_analyst"):
            build_signal_collection_contract(
                ticker="BU",
                trading_date="2025-03-25",
                analyst_signals=[
                    _formal_signal("technical", side="long"),
                    _formal_signal("technical", side="long"),
                ],
                enabled_analysts=["technical"],
            )
        unexpected = _formal_signal("technical", side="long")
        unexpected.agent_name = "unexpected"
        unexpected.metadata["action_evidence_contract"]["analyst"] = "unexpected"
        with self.assertRaisesRegex(ValueError, "unexpected_analyst"):
            build_signal_collection_contract(
                ticker="BU",
                trading_date="2025-03-25",
                analyst_signals=[unexpected],
                enabled_analysts=["technical"],
            )

    def test_missing_enabled_source_is_preserved_as_missing_evidence(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[_formal_signal("technical", side="flat")],
            enabled_analysts=["technical", "fundamental"],
        )
        self.assertIn("missing_analyst:fundamental", contract["missing_evidence"])

    def test_shared_validator_preserves_lineage_and_rejects_pm_fields(self):
        signals = [
            _formal_signal("technical", side="long"),
            _formal_signal("fundamental", side="long"),
            _formal_signal("commodity_news", side="flat"),
        ]
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=signals,
        )
        checked = validate_signal_collection_contract(
            contract,
            ticker="BU",
            trading_date="2025-03-25",
            enabled_analysts=["technical", "fundamental", "commodity_news"],
            analyst_signals=signals,
            require_signal_record_ids=True,
        )
        self.assertIs(checked, contract)
        self.assertEqual(checked["source_contracts"][0]["signal_record_id"], "technical-signal-id")
        polluted = {**contract, "target_lots": 2}
        with self.assertRaisesRegex(ValueError, "forbidden_trade_field"):
            validate_signal_collection_contract(polluted)
        polluted_source = deepcopy(contract)
        polluted_source["source_contracts"][0]["collector_snapshot"] = {}
        with self.assertRaisesRegex(ValueError, "unregistered_source_contract_field"):
            validate_signal_collection_contract(polluted_source)

        missing_record_id = deepcopy(contract)
        missing_record_id["source_contracts"][0]["signal_record_id"] = None
        with self.assertRaisesRegex(ValueError, "missing_signal_record_id"):
            validate_signal_collection_contract(
                missing_record_id,
                require_signal_record_ids=True,
            )

    def test_shared_action_contract_validator_rejects_semantic_drift(self):
        signal = _formal_signal("technical", side="long")
        action_contract = deepcopy(signal.metadata["action_evidence_contract"])
        self.assertIs(
            validate_action_evidence_contract(action_contract, analyst="technical"),
            action_contract,
        )

        wrong_side = {**action_contract, "side": "short"}
        with self.assertRaisesRegex(ValueError, "signal_side_mismatch"):
            validate_action_evidence_contract(wrong_side, analyst="technical")

        wrong_confidence = {**action_contract, "confidence": 1.2}
        with self.assertRaisesRegex(ValueError, "confidence_out_of_range"):
            validate_action_evidence_contract(wrong_confidence, analyst="technical")

        wrong_state = {**action_contract, "opportunity_state": "ready_to_buy"}
        with self.assertRaisesRegex(ValueError, "invalid_opportunity_state"):
            validate_action_evidence_contract(wrong_state, analyst="technical")

        wrong_entry_invalidation = {
            **action_contract,
            "invalidation_condition": "short_price_gte_invalidation_level",
        }
        with self.assertRaisesRegex(ValueError, "entry_invalidation_not_canonical"):
            validate_action_evidence_contract(
                wrong_entry_invalidation,
                analyst="technical",
            )

        atr_only = {
            **action_contract,
            "invalidation_level": None,
            "invalidation_condition": "",
            "invalidation_present": True,
            "atr_stop_distance": 3.0,
        }
        with self.assertRaisesRegex(ValueError, "invalidation_proof_missing"):
            validate_action_evidence_contract(atr_only, analyst="technical")

        unknown_field = {**action_contract, "private_direction_score": 0.9}
        with self.assertRaisesRegex(ValueError, "unregistered_field"):
            validate_action_evidence_contract(unknown_field, analyst="technical")

    def test_scc_validator_rejects_summary_that_disagrees_with_source_contracts(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _formal_signal("technical", side="long", trigger_valid=True, trigger_confirmed=True),
                _formal_signal("fundamental", side="long"),
                _formal_signal("commodity_news", side="flat"),
            ],
        )

        item_drift = deepcopy(contract)
        item_drift["evidence_items"][0]["side"] = "short"
        with self.assertRaisesRegex(ValueError, "evidence_item_semantic_mismatch"):
            validate_signal_collection_contract(item_drift)

        direction_drift = deepcopy(contract)
        direction_drift["dominant_side"] = "short"
        with self.assertRaisesRegex(ValueError, "dominant_side_mismatch"):
            validate_signal_collection_contract(direction_drift)

        trigger_drift = deepcopy(contract)
        trigger_drift["trigger_status"] = "watch_for_trigger"
        with self.assertRaisesRegex(ValueError, "trigger_status_mismatch"):
            validate_signal_collection_contract(trigger_drift)

    def test_pm_evidence_view_is_built_only_from_scc(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _formal_signal("technical", side="long", raw_signal="Bearish"),
                _formal_signal("fundamental", side="long", raw_signal="Bearish"),
                _formal_signal("commodity_news", side="flat", raw_signal="Bearish"),
            ],
        )
        evidence_signals = build_pm_evidence_signals_from_scc(contract)
        self.assertEqual(evidence_signals[0].agent_name, "technical")
        self.assertEqual(str(evidence_signals[0].signal), "Bullish")
        self.assertEqual(evidence_signals[0].confidence, 0.6)

    def test_same_scc_ignores_changed_raw_signal_semantics_at_pm_boundary(self):
        raw_signals = [
            _formal_signal("technical", side="long", raw_signal="Bearish"),
            _formal_signal("fundamental", side="long", raw_signal="Bearish"),
            _formal_signal("commodity_news", side="flat", raw_signal="Bearish"),
        ]
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=raw_signals,
        )
        before = [signal.model_dump() for signal in build_pm_evidence_signals_from_scc(contract)]
        for signal in raw_signals:
            signal.signal = "Bearish"
            signal.confidence = 0.01
            signal.trigger_valid = True
        validate_signal_collection_contract(
            contract,
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=raw_signals,
        )
        after = [signal.model_dump() for signal in build_pm_evidence_signals_from_scc(contract)]
        self.assertEqual(before, after)

    def test_step6_snapshot_preserves_original_scc_for_reviewer_and_researcher(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _formal_signal("technical", side="flat"),
                _formal_signal("fundamental", side="flat"),
                _formal_signal("commodity_news", side="flat"),
            ],
        )
        state = _pm_state("BU", 0, 0, with_scorecard=False)
        state["signal_collection_contract"] = deepcopy(contract)
        state["execution_contract_fields"]["signal_collection_contract"] = deepcopy(contract)
        signed = finalize_pm_full_market_contracts(
            generated=[("BU", state)],
            config={},
            portfolio=Portfolio(id="portfolio-1", cashflow=1_000_000, positions={}),
        )
        landed = signed[0][1].signal_snapshot["signal_collection_contract"]
        self.assertEqual(landed, contract)
        self.assertEqual(
            landed["source_contracts"][0]["signal_record_id"],
            "technical-signal-id",
        )

    def test_collector_has_one_scc_and_no_internal_snapshot(self):
        state = {
            "ticker": "BU",
            "trading_date": "2025-03-25",
            "enabled_analysts": ["technical", "fundamental", "commodity_news"],
            "analyst_signals": [
                _formal_signal("technical", side="long"),
                _formal_signal("fundamental", side="long"),
                _formal_signal("commodity_news", side="flat"),
            ],
        }
        output = signal_collector_agent(state)
        self.assertEqual(set(output), {"signal_collection_contract"})

    def test_data_unavailable_collector_requires_formal_analyst_signals(self):
        with self.assertRaisesRegex(ValueError, "signal_collection_missing_source_contracts"):
            signal_collector_agent(
                {
                    "ticker": "BU",
                    "trading_date": "2025-03-25",
                    "enabled_analysts": ["technical", "fundamental", "commodity_news"],
                    "analyst_signals": [],
                    "pre_open_reference_price_unavailable": True,
                }
            )

    def test_data_unavailable_workflow_persists_sources_before_pm(self):
        saved_signals = []

        class _DB:
            def save_signal(self, portfolio_id, analyst, ticker, signal):
                saved_signals.append((portfolio_id, analyst, ticker, signal))
                return f"signal-{ticker}-{analyst}"

        workflow = AgentWorkflow.__new__(AgentWorkflow)
        workflow.workflow_analysts = ["technical", "fundamental", "commodity_news"]
        workflow.tickers = ["BU"]
        workflow.config = {}
        workflow.db = _DB()
        captured = {}

        def _pm(state):
            captured.update(state)
            return {"pm_state": {"ticker": state["ticker"]}}

        state = {
            "ticker": "BU",
            "trading_date": "2025-03-25",
            "enabled_analysts": list(workflow.workflow_analysts),
            "portfolio": SimpleNamespace(id="portfolio-1"),
            "analyst_signals": [
                _formal_signal("technical", side="flat", signal_record_id=""),
                _formal_signal("fundamental", side="flat", signal_record_id=""),
                _formal_signal("commodity_news", side="flat", signal_record_id=""),
            ],
            "pre_open_reference_price_unavailable": True,
            "pre_open_reference_price_unavailable_reason": "missing_reference",
        }
        for signal in state["analyst_signals"]:
            signal.metadata.pop("signal_record_id", None)
        workflow._persist_prefetched_analyst_signals(state)
        with patch(
            "agents.decision_team.portfolio_manager.portfolio_agent_futures",
            side_effect=_pm,
        ):
            workflow._run_phase1_portfolio_only(
                state,
                SimpleNamespace(id="portfolio-1"),
            )
        self.assertEqual(len(saved_signals), 3)
        source_ids = [
            row["signal_record_id"]
            for row in captured["signal_collection_contract"]["source_contracts"]
        ]
        self.assertEqual(
            source_ids,
            [
                "signal-BU-technical",
                "signal-BU-fundamental",
                "signal-BU-commodity_news",
            ],
        )

    def test_pm_source_uses_raw_signals_only_for_lineage_validation(self):
        source = inspect.getsource(portfolio_manager._run_pm_six_step_decision)
        self.assertIn('source_analyst_signals = state["analyst_signals"]', source)
        self.assertIn("build_pm_evidence_signals_from_scc", source)
        self.assertNotRegex(source, r'(?m)^\s*analyst_signals = state\["analyst_signals"\]')
        self.assertNotIn("for signal in source_analyst_signals", source)
        self.assertEqual(source.count("source_analyst_signals"), 3)
        collector_source = inspect.getsource(signal_collector_agent)
        self.assertIn("validate_signal_collection_contract", collector_source)
        self.assertIn("validate_signal_collection_contract", source)

    def test_pm_evidence_helpers_do_not_fall_back_to_old_research_contracts(self):
        old_only = SimpleNamespace(
            metadata={
                "trade_research_contract": {"opportunity_state": "tradeable_candidate"}
            },
            invalidation_level=100.0,
            atr_stop_distance=5.0,
        )
        self.assertEqual(
            pm_signal_fusion._contract_value(old_only, "opportunity_state", "missing"),
            "missing",
        )
        self.assertFalse(_has_structured_invalidation_condition([old_only]))
        self.assertNotIn(
            "trade_research_contract",
            inspect.getsource(portfolio_manager._derive_signal_contract_fields),
        )

    def test_pm_market_confirmation_is_derived_only_from_scc(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _formal_signal("technical", side="long"),
                _formal_signal("fundamental", side="long"),
                _formal_signal("commodity_news", side="flat"),
            ],
        )
        confirmation = pm_signal_fusion.build_scc_market_confirmation(
            contract,
            target_direction="long",
        )
        self.assertTrue(confirmation["enabled"])
        self.assertEqual(confirmation["confirmations"], ["fundamental", "technical"])
        self.assertEqual(
            confirmation["confirmation_score"],
            contract["evidence_fusion"]["multi_evidence_consensus_score"],
        )
        pm_source = inspect.getsource(portfolio_manager._run_pm_six_step_decision)
        self.assertIn("build_scc_market_confirmation", pm_source)
        self.assertNotIn("MarketConfirmationEngine", pm_source)
        self.assertNotIn("pandaai_extra_data", pm_source)

    def test_pm_market_confirmation_keeps_cross_horizon_opposition_out_of_entry_conflicts(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _formal_signal("technical", side="long"),
                _formal_signal("fundamental", side="short"),
                _formal_signal("commodity_news", side="flat"),
            ],
        )
        confirmation = pm_signal_fusion.build_scc_market_confirmation(
            contract,
            target_direction="long",
        )
        self.assertEqual(confirmation["status"], "supported")
        self.assertEqual(confirmation["conflicts"], [])
        self.assertIn("fundamental", contract["opposing_analysts"])


if __name__ == "__main__":
    unittest.main()
