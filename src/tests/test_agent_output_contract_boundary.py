import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.execution_team import trader as trader_agent
from llm.prompt import build_researcher_causal_review_prompt
from tools.agent_tools.research import research_review_helpers
from tools.common.contracts import (
    final_contract_execution_fields,
    validate_accountant_artifact_boundary,
    validate_execution_artifact_boundary,
    validate_researcher_artifact_boundary,
    validate_reviewer_artifact_boundary,
)
from tools.common.evidence_fusion_semantics import build_pm_fusion_diagnostics
from tools.common.final_action_semantics import (
    FORBIDDEN_ANALYST_TRADE_AUTHORITY_KEYS,
    classify_analyst_evidence,
    classify_final_action_contract,
    contract_increases_risk_position,
    derive_accounting_expectation,
    derive_research_fact_state,
    derive_review_expectation,
    validate_final_action_lot_transition,
    validate_signal_collection,
)
from tools.common.signal_evidence_collection import build_signal_collection_contract


LEGACY_PM_LIFECYCLE_FIELDS = {
    "contract_lifecycle_self_check",
    "historical_lifecycle_transition_diagnostic",
    "initial_primary_lifecycle_action_port",
    "primary_lifecycle_action_port",
    "lifecycle_port_transition_reason",
    "lifecycle_transition_diagnostic",
    "lifecycle_transition_reason",
}


def _source(relative_path):
    return (SRC_ROOT / relative_path).read_text(encoding="utf-8")


def _nested_key_paths(value, forbidden, prefix=""):
    paths = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key in forbidden:
                paths.append(path)
            paths.extend(_nested_key_paths(child, forbidden, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            paths.extend(_nested_key_paths(child, forbidden, path))
    return paths


def _analyst_action_contract():
    return {
        "signal": "bullish_breakout_watch",
        "side": "long",
        "confidence": 0.68,
        "opportunity_state": "watch_for_trigger",
        "trigger_valid": True,
        "current_trigger_confirmed": True,
        "entry_trigger": "breakout_above_prior_high",
        "setup_type": "trend_breakout",
        "setup_quality_ok": True,
        "horizon_class": "swing",
        "market_regime": "trend",
        "evidence_quality": "high",
        "current_evidence_conflict": [],
        "missing_evidence": [],
        "invalidation_present": True,
        "invalidation_condition": "close_below_support",
        "no_lookahead_status": "ok",
        "fusion_evidence": {
            "evidence_strength": "high",
            "evidence_freshness": "fresh",
            "confirmation_requirements": ["intraday_breakout"],
        },
        "product_profile_evidence": {
            "product_profile_id": "BU.default",
            "product_profile_used": True,
            "profile_analysis_boundary": "analyst_evidence_calibration_only",
        },
    }


def _analyst_signal(agent_name="technical"):
    contract = _analyst_action_contract()
    return SimpleNamespace(
        agent_name=agent_name,
        signal=contract["signal"],
        confidence=contract["confidence"],
        trigger_valid=contract["trigger_valid"],
        current_trigger_confirmed=contract["current_trigger_confirmed"],
        invalidation_present=contract["invalidation_present"],
        metadata={
            "action_evidence_contract": contract,
            "product_profile_evidence": contract["product_profile_evidence"],
            "fusion_evidence": contract["fusion_evidence"],
            "signal_record_id": f"{agent_name}-fixture",
        },
    )


def _final_action_contract():
    return {
        "contract_version": "agentquant.pm.final_action_contract.v1",
        "contract_type": "final_action_contract",
        "ticker": "BU",
        "final_action": "wait",
        "current_lots": 0,
        "target_lots": 0,
        "lots_delta": 0,
        "reason_codes": ["non_new_risk_no_capital_rank"],
        "capital_deployment": {
            "selected_for_capital_deployment": False,
            "capital_allocation_reason": "non_new_risk_no_capital_rank",
        },
        "evidence_used": {
            "position_sizing_result": {
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "position_sizing_status": "no_new_risk",
            },
            "signal_collection_contract_ref": {
                "source_agent": "signal_collector",
                "collector_decision_boundary": "no_trade_authority",
                "ticker": "BU",
                "trading_date": "2025-03-25",
            },
        },
        "learning_used": {"memory_retrieval": {"memory_count": 1}},
    }


class AgentOutputContractBoundaryTest(unittest.TestCase):
    def test_analyst_output_is_evidence_only_and_common_semantics_can_read_it(self):
        artifact = {
            "producer": "technical",
            "action_evidence_contract": _analyst_action_contract(),
            "product_profile_evidence": _analyst_action_contract()["product_profile_evidence"],
            "fusion_evidence": _analyst_action_contract()["fusion_evidence"],
        }
        self.assertFalse(_nested_key_paths(artifact, FORBIDDEN_ANALYST_TRADE_AUTHORITY_KEYS))

        semantics = classify_analyst_evidence(artifact["action_evidence_contract"])
        self.assertEqual(semantics["semantic_errors"], [])
        self.assertEqual(semantics["forbidden_trade_authority_fields"], [])

    def test_signal_collector_output_has_source_agent_boundary_and_no_trade_authority(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-25",
            analyst_signals=[
                _analyst_signal("technical"),
                _analyst_signal("fundamental"),
                _analyst_signal("commodity_news"),
            ],
        )
        self.assertEqual(contract["source_agent"], "signal_collector")
        self.assertEqual(contract["collector_decision_boundary"], "no_trade_authority")
        self.assertTrue(contract["no_trade_authority"])

        signal_semantics = validate_signal_collection(contract)
        self.assertTrue(signal_semantics["no_trade_authority"])
        self.assertEqual(signal_semantics["forbidden_trade_authority_fields"], [])
        pm_view = build_pm_fusion_diagnostics(contract)
        self.assertTrue(pm_view["pm_fusion_diagnostics"])
        self.assertTrue(pm_view["no_trade_authority"])

    def test_pm_final_contract_contains_no_internal_lifecycle_diagnostics(self):
        contract = _final_action_contract()

        self.assertFalse(_nested_key_paths(contract["evidence_used"], LEGACY_PM_LIFECYCLE_FIELDS))
        self.assertFalse(_nested_key_paths(contract["learning_used"], LEGACY_PM_LIFECYCLE_FIELDS))
        self.assertTrue(validate_final_action_lot_transition(contract)["ok"])
        semantics = classify_final_action_contract(contract)
        self.assertEqual(semantics["execution_permission"], "no_trade")

    def test_trader_output_keeps_execution_artifact_without_full_pm_contract(self):
        contract = deepcopy(_final_action_contract())
        execution_fields = final_contract_execution_fields(contract)
        payload = {
            "phase2_execution": {
                "producer": "trader",
                "execution_result": {
                    "status": "not_submitted",
                    "actual_transactions": [],
                },
                "pm_plan_validation": {
                    "final_contract_execution_fields": execution_fields,
                },
            }
        }
        validate_execution_artifact_boundary(payload)
        self.assertNotIn("final_action_contract", payload["phase2_execution"])
        with self.assertRaisesRegex(ValueError, "execution_artifact_forbidden_pm_fields"):
            validate_execution_artifact_boundary(
                {"phase2_execution": {"final_action_contract": contract}}
            )

    def test_accountant_output_is_settlement_only_and_uses_trade_semantics_readonly(self):
        contract = deepcopy(_final_action_contract())
        expectation = derive_accounting_expectation(contract, {"actual_transactions": []})
        self.assertEqual(expectation["settlement_basis"], "actual_execution_facts_only")

        payload = {
            "daily_settlement": {
                "trading_date": "2025-03-25",
                "daily_pnl": 0.0,
                "commission": 0.0,
                "current_margin": 0.0,
                "current_balance": 5_000_000.0,
            },
            "ticker_daily_pnl": {"BU": 0.0},
        }
        validate_accountant_artifact_boundary(payload)
        with self.assertRaisesRegex(ValueError, "accountant_artifact_forbidden_trade_or_learning_fields"):
            validate_accountant_artifact_boundary({"daily_settlement": {"learning_used": []}})

    def test_reviewer_output_is_review_only_and_uses_trade_semantics_readonly(self):
        contract = deepcopy(_final_action_contract())
        expectation = derive_review_expectation(contract, {"actual_transactions": []})
        self.assertEqual(expectation["settlement_basis"], "actual_execution_facts_only")

        payload = {
            "phase4_validation": {"status": "completed"},
            "trade_log": [{"ticker": "BU", "review_scope": "phase1_to_phase3"}],
            "fact_attribution": {"no_trade_reason": "wait_no_new_risk"},
        }
        validate_reviewer_artifact_boundary(payload)
        with self.assertRaisesRegex(ValueError, "reviewer_artifact_forbidden_research_or_mutation_fields"):
            validate_reviewer_artifact_boundary({"alpha_setup_action_value": {"reward_sum": 1.0}})

    def test_researcher_output_is_future_learning_only_not_same_day_fact_mutation(self):
        payload = {
            "alpha_setup_action_value": {
                "product": "BU",
                "action_value_lane": "hold",
                "memory_side_role": "flat",
                "reward_sum": 0.0,
            },
            "alpha_setup_profile": {
                "setup_type": "trend_breakout",
                "future_memory_only": True,
            },
            "researcher_llm_notes": {
                "summary": "fixture only",
                "consumer_scope": "research_diagnostics",
            },
        }
        validate_researcher_artifact_boundary(payload)
        with self.assertRaisesRegex(ValueError, "researcher_artifact_forbidden_trade_fact_mutation"):
            validate_researcher_artifact_boundary(
                {"modified_final_action_contract": {"target_lots": 9}}
            )

    def test_all_agents_use_declared_upstream_semantic_entrypoints(self):
        analyst_files = [
            "agents/analysis_team/technical.py",
            "agents/analysis_team/fundamental.py",
            "agents/analysis_team/commodity_news.py",
        ]
        for path in analyst_files:
            source = _source(path)
            self.assertIn("build_learning_context", source, path)
            self.assertIn("calibrate_signal_with_learning_context", source, path)
            self.assertNotIn("final_action_contract", source, path)

        technical_source = _source("agents/analysis_team/technical.py")
        self.assertIn("retrieve_analyst_policy_calibration", technical_source)

        signal_collector_source = _source("agents/decision_team/signal_collector.py")
        self.assertIn("build_signal_collection_contract", signal_collector_source)
        signal_contract_source = _source("tools/common/signal_evidence_collection.py")
        self.assertIn('"source_agent": "signal_collector"', signal_contract_source)
        self.assertIn('"collector_decision_boundary": "no_trade_authority"', signal_contract_source)

        pm_source = _source("agents/decision_team/portfolio_manager.py")
        self.assertIn("pm_missing_signal_collection_contract_from_signal_collector", pm_source)
        self.assertNotIn("build_signal_collection_contract", pm_source)
        self.assertIn("retrieve_pm_memory", pm_source)
        self.assertIn("route_lifecycle_learning", pm_source)

        auditor_source = _source("agents/decision_team/auditor.py")
        self.assertIn("final_action_contract_from_snapshot", auditor_source)
        self.assertIn("contract_increases_risk_position", auditor_source)
        self.assertIn("derive_protocol_semantic_checks", auditor_source)
        self.assertNotIn("def _contract_target_lots", auditor_source)
        self.assertNotIn("def _contract_current_lots", auditor_source)

        trader_source = _source("agents/execution_team/trader.py")
        self.assertIn("final_action_contract_from_snapshot", trader_source)
        self.assertIn("final_contract_execution_fields_from_snapshot", trader_source)
        self.assertIn("validate_final_action_contract", trader_source)
        self.assertIn("contract_increases_risk_position", trader_source)
        self.assertIn("contract_requires_conditional_intraday_result", trader_source)
        self.assertIn("phase2_order_intent_from_lots", trader_source)

        accountant_sources = (
            _source("agents/execution_team/accountant.py")
            + _source("tools/agent_tools/execution/accountant_futures_settlement.py")
        )
        for forbidden in ("final_action_contract", "target_lots", "lots_delta", "pm_internal_candidate"):
            self.assertNotIn(forbidden, accountant_sources)

        reviewer_source = (
            _source("tools/agent_tools/research/reviewer_phase4_review.py")
            + _source("tools/agent_tools/research/research_review_helpers.py")
        )
        self.assertIn("final_action_contract_from_snapshot", reviewer_source)
        self.assertIn("derive_review_expectation", reviewer_source)
        self.assertIn("classify_final_action_contract", reviewer_source)
        self.assertIn("build_reviewer_fusion_attribution", reviewer_source)

        researcher_source = (
            _source("tools/agent_tools/research/research_learning.py")
            + _source("tools/agent_tools/research/research_memory_writers.py")
        )
        self.assertIn("derive_research_fact_state", researcher_source)
        for forbidden in (
            "pm_internal_candidate",
            "pm_capital_deployment_decision",
            "primary_lifecycle_action_port",
            "lifecycle_transition_diagnostic",
            "lifecycle_transition_reason",
        ):
            self.assertNotIn(forbidden, researcher_source)

    def test_trader_private_execution_projection_matches_common_semantics(self):
        cases = [
            (0, 0),
            (0, 2),
            (2, 4),
            (4, 2),
            (2, -1),
            (-3, -5),
        ]
        for current_lots, target_lots in cases:
            contract = {
                "current_lots": current_lots,
                "target_lots": target_lots,
                "lots_delta": target_lots - current_lots,
            }
            expected = contract_increases_risk_position(contract)
            self.assertEqual(
                trader_agent._requires_entry_authority(current_lots, target_lots),
                expected,
                (current_lots, target_lots),
            )
            projected = trader_agent._target_lots_without_new_entry(current_lots, target_lots)
            if expected:
                if current_lots == 0:
                    self.assertEqual(projected, 0)
                elif (current_lots > 0) != (target_lots > 0):
                    self.assertEqual(projected, 0)
                else:
                    self.assertEqual(projected, current_lots)
            else:
                self.assertEqual(projected, target_lots)

    def test_reviewer_and_researcher_fact_views_are_common_semantic_views(self):
        contract = deepcopy(_final_action_contract())
        review_view = research_review_helpers._final_action_semantic_view(
            contract,
            {"actual_transactions": []},
        )
        self.assertEqual(review_view["source"], "final_action_semantics")
        self.assertEqual(review_view["review_expectation"]["settlement_basis"], "actual_execution_facts_only")
        self.assertEqual(review_view["action"], classify_final_action_contract(contract)["action"])

        research_view = derive_research_fact_state(contract, {"actual_transactions": []})
        self.assertEqual(research_view["position_effect"]["source"], "final_action_semantics.research_fact_state")
        self.assertEqual(research_view["position_effect"]["final_action"], research_view["action"])
        self.assertFalse(_nested_key_paths(research_view, LEGACY_PM_LIFECYCLE_FIELDS))

    def test_research_llm_inputs_do_not_depend_on_pm_internal_lifecycle_fields(self):
        research_sources = (
            _source("agents/research_team/researcher.py")
            + _source("tools/agent_tools/research/research_learning.py")
            + _source("tools/agent_tools/research/research_memory_writers.py")
            + _source("tools/agent_tools/research/research_review_helpers.py")
        )
        self.assertIn("derive_research_fact_state", research_sources)
        self.assertIn("_final_action_semantic_view", research_sources)
        for forbidden in (
            "pm_internal_candidate",
            "pm_capital_deployment_decision",
            "primary_lifecycle_action_port",
            "lifecycle_transition_diagnostic",
            "lifecycle_transition_reason",
        ):
            self.assertNotIn(forbidden, research_sources)

    def test_researcher_prompt_treats_budget_drift_as_factual_research_input(self):
        prompt = build_researcher_causal_review_prompt("{}")

        self.assertIn("budget_drift_diagnostics", prompt)
        self.assertIn("PM plan budget drift", prompt)
        self.assertIn("factual attribution", prompt)
        self.assertIn("research input", prompt)
        self.assertIn("reviewer_hard_gate=false", prompt)
        self.assertIn("max_net_exposure", prompt)
        self.assertIn("target_margin_ratio_*", prompt)
        self.assertIn("probe_margin_ratio", prompt)
        self.assertIn("strong_opportunity_*", prompt)
        self.assertIn("recovery_*", prompt)
        self.assertIn("not final_action_contract invalidation", prompt)
        self.assertIn("not a day-end trade violation", prompt)
        self.assertIn("not same-day trade authority", prompt)
        self.assertIn("cannot bypass final_action_contract", prompt)
        self.assertIn("research diagnostics only", prompt)
        self.assertIn("Do not use this review to rejudge PM contract legality", prompt)


if __name__ == "__main__":
    unittest.main()
