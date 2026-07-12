import inspect
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.decision_team.signal_collector import signal_collector_agent
from agents.decision_team import portfolio_manager
from agents.decision_team.portfolio_manager import finalize_pm_full_market_contracts
from graph.schema import Portfolio
from tests.test_pm_atomic_contract_flow import _pm_state
from tools.agent_tools.decision import pm_signal_fusion
from tools.agent_tools.decision.pm_invalidation_policy import (
    _has_structured_invalidation_condition,
)
from tools.agent_tools.decision.signal_collection_data_unavailable import (
    build_data_unavailable_signal_package,
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
):
    signal_text = "Bullish" if side == "long" else "Bearish" if side == "short" else "Neutral"
    contract = {
        "contract_version": "agentquant.action_evidence.v1",
        "analyst": analyst,
        "signal": signal_text,
        "side": side,
        "confidence": confidence,
        "opportunity_type": "trend_continuation" if side != "flat" else "no_trade",
        "opportunity_state": (
            "tradeable_candidate"
            if side != "flat" and trigger_valid
            else "watch_for_trigger"
            if side != "flat"
            else "no_opportunity"
        ),
        "setup_type": "trend_continuation" if side != "flat" else "no_trade",
        "setup_quality_ok": side != "flat",
        "trigger_valid": trigger_valid,
        "current_trigger_confirmed": trigger_confirmed,
        "entry_trigger": "breakout" if side != "flat" else "none",
        "horizon_class": "short" if side != "flat" else "flat",
        "market_regime": "trend" if side != "flat" else "unknown",
        "evidence_quality": "high" if side != "flat" else "low",
        "current_evidence_conflict": [],
        "missing_evidence": [],
        "invalidation_present": side != "flat",
        "invalidation_condition": "close_beyond_invalidation" if side != "flat" else "",
        "no_lookahead_status": "ok",
        "data_usage_summary": {"data_quality_flags": []},
        "fusion_evidence": {
            "evidence_strength": "strong" if side != "flat" else "weak",
            "evidence_strength_score": confidence,
            "evidence_freshness": "fresh",
            "confirmation_requirements": [],
            "missing_evidence": [],
        },
        "product_profile_evidence": {
            "product_profile_id": "BU.default",
            "product_profile_used": True,
            "profile_analysis_boundary": "analyst_evidence_calibration_only",
        },
    }
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

    def test_equal_long_short_evidence_is_mixed(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _formal_signal("technical", side="long"),
                _formal_signal("fundamental", side="short"),
                _formal_signal("commodity_news", side="flat"),
            ],
        )
        self.assertEqual(contract["dominant_side"], "mixed")
        self.assertEqual(contract["side_consensus"], "conflicted")

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
            "direction_alignment",
            "cross_analyst_conflicts",
            "dominant_opposing_evidence",
            "multi_evidence_consensus_score",
        }
        self.assertTrue(nested_only.issubset(contract["evidence_fusion"]))
        self.assertFalse(nested_only.intersection(contract))

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
        with self.assertRaisesRegex(ValueError, "unexpected_analyst"):
            build_signal_collection_contract(
                ticker="BU",
                trading_date="2025-03-25",
                analyst_signals=[_formal_signal("unexpected", side="long")],
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

    def test_data_unavailable_path_keeps_trace_without_duplicate_snapshot(self):
        output = build_data_unavailable_signal_package(
            ticker="BU",
            trading_date="2025-03-25",
            enabled_analysts=["technical", "fundamental", "commodity_news"],
            reason="pre_open_reference_price_unavailable",
        )
        self.assertIn("signal_collection_contract", output)
        self.assertIn("analyst_signals", output)
        self.assertNotIn("signal_collection_contracts", output)
        self.assertNotIn("signal_snapshot", output)
        contract = output["signal_collection_contract"]
        self.assertNotIn("collection_status", contract)
        self.assertNotIn("data_unavailable_reason", contract)
        self.assertIn("pre_open_reference_price_unavailable", contract["data_quality_flags"])
        validate_signal_collection_contract(contract, ticker="BU", trading_date="2025-03-25")

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


if __name__ == "__main__":
    unittest.main()
