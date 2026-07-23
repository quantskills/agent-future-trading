import copy
import unittest
from pathlib import Path
import sys


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.decision.pm_contract_builder import build_final_action_contract
from tools.agent_tools.decision.pm_contract_self_check import check_final_action_contract
from tools.agent_tools.decision.pm_lifecycle_action_port import classify_lifecycle_action_port
from tools.agent_tools.decision.pm_position_transition import classify_position_transition
from tools.agent_tools.decision.pm_state_transition import classify_pm_decision_state
from tools.common.final_action_semantics import full_market_rank_source_payload
from tools.common.execution_trigger_semantics import (
    canonical_entry_invalidation_condition,
    canonical_entry_trigger,
)


class PMStateTransitionMatrixTest(unittest.TestCase):
    def test_lifecycle_port_requires_rank_for_open_and_same_side_increase(self):
        opening = classify_lifecycle_action_port(
            {"current_lots": 0, "target_lots": 1, "final_action": "open_probe"}
        )
        adding = classify_lifecycle_action_port(
            {"current_lots": 1, "target_lots": 2, "final_action": "scale"}
        )
        reversing = classify_lifecycle_action_port(
            {"current_lots": 1, "target_lots": -1, "final_action": "reverse"}
        )

        self.assertTrue(opening["requires_full_market_rank"])
        self.assertTrue(adding["requires_full_market_rank"])
        self.assertFalse(reversing["requires_full_market_rank"])

    def _non_rank_deployment(self):
        return {
            "selected_for_capital_deployment": False,
            "deployment_required": False,
            "new_risk_rank_required": False,
            "capital_allocation_reason": "non_new_risk_no_capital_rank",
            "original_target_lots": 1,
            "deployed_target_lots": 1,
            "deployed_lots_delta": 0,
            "reason_codes": ["non_new_risk_no_capital_rank"],
            "not_second_contract": True,
            "pm_remains_single_fund_manager": True,
        }

    def _position_sizing(self):
        return {
            "tool": "position_sizing",
            "ticker": "BU",
            "current_lots": 1,
            "target_lots": 1,
            "lots_delta": 0,
            "target_position_ratio": 0.01,
            "no_final_action_authority": True,
        }

    def _complete_contract(self, **overrides):
        sizing_override = overrides.pop("position_sizing_result", None)
        evidence_override = overrides.pop("evidence_used", None)
        contract = {
            "ticker": "BU",
            "final_action": "hold",
            "current_lots": 1,
            "target_lots": 1,
            "lots_delta": 0,
            "reason_codes": ["test_contract"],
            "execution_profile": "hold",
            "trigger_source": "none",
            "entry_trigger": "",
            "invalidation": "",
            "learning_used": {},
            "evidence_used": dict(evidence_override or {}),
            "capital_deployment": self._non_rank_deployment(),
        }
        contract.update(overrides)
        sizing = dict(self._position_sizing() if sizing_override is None else sizing_override)
        if sizing:
            sizing["current_lots"] = contract["current_lots"]
            sizing["target_lots"] = contract["target_lots"]
            sizing["lots_delta"] = contract["lots_delta"]
        contract["evidence_used"]["position_sizing_result"] = sizing
        return contract

    def _rank_trace(self):
        return {
            "opportunity_rank": 1,
            "rank_score": 0.62,
            "rank_input_components": {
                "rank_score": 0.62,
                "capital_priority_score": 0.62,
                "opportunity_score": 0.58,
            },
            "rank_capital_role": "best_exploration_probe_candidate",
            "capital_layer": "exploration_probe",
            "capital_ratio_source": "probe_margin_ratio_0.008",
            "rank_reason": "best_watch_for_trigger_by_evidence_trigger_learning_and_risk",
            "lifecycle_learning_trace": {
                "rank_lifecycle": "open_add_new_risk",
                "used_lanes": ["open"],
                "decision_learning_rows": [{"id": "open-1", "learning_lane": "open", "action_name": "open"}],
                "trigger_profile_learning_rows": [],
                "execution_profile_learning_direct_to_rank": False,
                "trigger_profile_learning_direct_to_rank": False,
                "execution_profile_signal_direct_to_rank": False,
                "pm_final_contract_lifecycle_trace": {
                    "trace_version": "agentquant.pm_lifecycle_learning_trace.v1",
                    "contract_lifecycle_port": "open_add_new_risk",
                    "rank_lifecycle": "open_add_new_risk",
                    "used_lanes": ["open"],
                    "decision_learning_rows": [
                        {"id": "open-1", "learning_lane": "open", "action_name": "open"}
                    ],
                    "trigger_profile_learning_rows": [],
                    "execution_profile_learning_direct_to_rank": False,
                    "trigger_profile_learning_direct_to_rank": False,
                    "execution_profile_signal_direct_to_rank": False,
                },
            },
            "learning_impact_delta": {
                "net_rank_learning_delta": 0.04,
                "execution_profile_learning_direct_to_rank": False,
            },
            **full_market_rank_source_payload(),
        }

    def _ranked_new_risk_contract(self):
        rank_trace = self._rank_trace()
        rank_trace.update(
            {
                "selected_for_capital_deployment": True,
                "capital_allocation_reason": "selected_by_full_market_pm_capital_queue",
                "original_target_lots": 1,
                "deployed_target_lots": 1,
                "deployed_lots_delta": 1,
            }
        )
        pm_trace = {
            "contract_lifecycle_port": "open_add_new_risk",
            "rank_lifecycle": "open_add_new_risk",
            "used_lanes": ["open"],
            "decision_learning_rows": [{"id": "open-1", "learning_lane": "open", "action_name": "open"}],
            "trigger_profile_learning_rows": [],
            "execution_profile_learning_direct_to_rank": False,
            "trigger_profile_learning_direct_to_rank": False,
        }
        return self._complete_contract(
            final_action="open_probe",
            current_lots=0,
            target_lots=1,
            lots_delta=1,
            execution_profile="breakout",
            trigger_source="technical_breakout",
            entry_trigger=canonical_entry_trigger("breakout", "long"),
            invalidation=canonical_entry_invalidation_condition("breakout", "long"),
            invalidation_level=95.0,
            position_invalidation_level=94.0,
            valid_until="2025-03-25 15:00:00",
            learning_used={
                "alpha_setup_action_values": [
                    {
                        "id": "open-1",
                        "canonical_action_value": True,
                        "consumer_scope": "pm_learning",
                        "canonical_action_family": "open_add_new_risk",
                        "action_name": "open",
                        "action_value_lane": "open",
                        "learning_lane": "open",
                        "action_preference": "positive_candidate_open",
                    }
                ],
                "pm_lifecycle_learning_trace": pm_trace,
                "pm_lifecycle_learning_impact_delta": {"net_lifecycle_learning_delta": 0.04},
            },
            evidence_used={},
            capital_deployment=dict(rank_trace),
            position_sizing_result={"target_lots": 1, "target_position_ratio": 0.01},
        )

    def _undeployed_step5_contract(self):
        rank_trace = self._rank_trace()
        rank_trace["opportunity_rank"] = 2
        deployment = {
            **rank_trace,
            "selected_for_capital_deployment": False,
            "capital_allocation_reason": (
                "no_rank_or_budget_no_new_exposure:"
                "not_selected_by_full_market_pm_capital_queue;rank=2"
            ),
            "original_target_lots": 1,
            "deployed_target_lots": 0,
            "deployed_lots_delta": 0,
            "reason_codes": ["no_rank_or_budget_no_new_exposure"],
        }
        pm_trace = {
            "contract_lifecycle_port": "wait",
            "transition_reason": "no_rank_or_budget_no_new_exposure",
            "decision_learning_rows": [],
            "trigger_profile_learning_rows": [],
            "execution_profile_learning_direct_to_rank": False,
            "trigger_profile_learning_direct_to_rank": False,
        }
        contract = self._complete_contract(
            final_action="wait",
            current_lots=0,
            target_lots=0,
            lots_delta=0,
            reason_codes=["no_rank_or_budget_no_new_exposure"],
            learning_used={
                "pm_lifecycle_learning_trace": pm_trace,
                "pm_lifecycle_learning_impact_delta": {"deployment": "not_selected"},
            },
            evidence_used={},
            capital_deployment=deployment,
            position_sizing_result={"target_lots": 0, "target_position_ratio": 0.0},
            conditional_trigger_authority=False,
            requires_intraday_confirmation=False,
            can_execute_without_intraday_trigger=False,
        )
        return contract

    def test_position_transition_matrix_for_open_add_scale_reduce_exit(self):
        self.assertEqual(
            classify_position_transition(current_lots=0, target_lots=1)["final_action"],
            "open_probe",
        )
        self.assertEqual(
            classify_position_transition(
                current_lots=0,
                target_lots=2,
                final_entry_authority={"authority_type": "real_budget_entry"},
            )["final_action"],
            "open_real",
        )
        self.assertEqual(
            classify_position_transition(current_lots=1, target_lots=3)["final_action"],
            "scale",
        )
        self.assertEqual(
            classify_position_transition(current_lots=-3, target_lots=-1)["transition_kind"],
            "reduce",
        )
        self.assertEqual(
            classify_position_transition(current_lots=2, target_lots=0)["final_action"],
            "exit",
        )
        self.assertEqual(
            classify_position_transition(current_lots=2, target_lots=-1)["transition_kind"],
            "exit_then_reenter",
        )

    def test_pm_decision_state_uses_scorecard_without_overriding_risk_reduction(self):
        self.assertEqual(
            classify_pm_decision_state(current_lots=0, target_lots=1, scorecard_state="tradeable_candidate"),
            "tradeable_candidate",
        )
        self.assertEqual(
            classify_pm_decision_state(current_lots=3, target_lots=1, scorecard_state="tradeable_candidate"),
            "risk_reduction_candidate",
        )
        self.assertEqual(
            classify_pm_decision_state(current_lots=0, target_lots=0, scorecard_state="watch_for_trigger"),
            "no_opportunity",
        )

    def test_contract_builder_and_self_check_cover_pm_contract_consistency(self):
        contract = build_final_action_contract(
            ticker="BU",
            current_lots=1,
            target_lots=1,
            position_ratio=0.01,
            margin_required=1000.0,
            account_equity=1_000_000.0,
            lots_to_trade=0,
            lots_to_trade_reason="hold_position",
            recommendation_intent={
                "action": "hold",
                "lots": 0,
                "action_type": "hold",
            },
            final_entry_authority={"authority_type": "probe_entry", "reason_codes": ["minimum_one_lot_probe"]},
            control_reasons=["minimum_one_lot_probe"],
            control_diagnostics={},
            opportunity_scorecard={
                "preferred_side": "long",
                "long": {
                    "final_state": "probe_candidate",
                    "score": 0.7,
                    "rank_input_components": {"old_step3_candidate_rank_score": 0.7},
                },
            },
            market_confirmation={"confirmation_score": 0.7},
            alpha_setup_action_values=[],
            execution_contract_fields={
                "execution_profile": "hold",
                "trigger_source": "none",
            },
        )
        contract["entry_trigger"] = ""
        contract["invalidation"] = ""
        self.assertEqual(contract["final_action"], "hold")
        self.assertEqual(contract["capital_deployment"]["capital_allocation_reason"], "non_new_risk_no_capital_rank")
        self.assertNotIn("rank_input_components", contract["evidence_used"])
        self.assertTrue(check_final_action_contract(contract)["ok"])

        bad = dict(contract)
        bad["lots_delta"] = 99
        self.assertIn("lots_delta_mismatch", check_final_action_contract(bad)["errors"])

    def test_pm_contract_self_check_requires_mechanism_6_7_base_fields(self):
        contract = self._complete_contract()
        contract["evidence_used"].pop("position_sizing_result")

        result = check_final_action_contract(contract)

        self.assertFalse(result["ok"])
        self.assertIn("position_sizing_result_missing", result["errors"])

    def test_pm_contract_self_check_rejects_new_risk_without_full_market_rank(self):
        contract = self._complete_contract(
            final_action="open_probe",
            current_lots=0,
            target_lots=1,
            lots_delta=1,
        )

        result = check_final_action_contract(contract)

        self.assertFalse(result["ok"])
        self.assertIn("new_risk_exposure_missing_full_market_rank", result["errors"])

    def test_pm_contract_self_check_rejects_empty_deployment_and_sizing_objects(self):
        contract = self._complete_contract(capital_deployment={}, position_sizing_result={})

        result = check_final_action_contract(contract)

        self.assertFalse(result["ok"])
        self.assertIn("capital_deployment_missing", result["errors"])
        self.assertIn("position_sizing_result_missing", result["errors"])

    def test_pm_contract_self_check_rejects_non_rank_deployment_without_fixed_reason(self):
        contract = self._complete_contract()
        contract["capital_deployment"] = {
            **contract["capital_deployment"],
            "capital_allocation_reason": "generic_hold_reason",
        }

        result = check_final_action_contract(contract)

        self.assertFalse(result["ok"])
        self.assertIn("non_rank_capital_deployment_reason_invalid", result["errors"])

    def test_pm_contract_self_check_allows_step5_undeployed_new_risk_when_restored_to_wait(self):
        contract = self._undeployed_step5_contract()

        result = check_final_action_contract(contract)

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(contract["target_lots"], 0)
        self.assertEqual(contract["lots_delta"], 0)
        self.assertEqual(contract["final_action"], "wait")
        self.assertFalse(contract["capital_deployment"]["selected_for_capital_deployment"])

    def test_pm_contract_self_check_rejects_undeployed_new_risk_that_still_requires_intraday_trigger(self):
        contract = self._undeployed_step5_contract()
        contract["conditional_trigger_authority"] = True
        contract["requires_intraday_confirmation"] = True
        contract["can_execute_without_intraday_trigger"] = False

        result = check_final_action_contract(contract)

        self.assertFalse(result["ok"])
        self.assertIn("conditional_trigger_without_lot_delta", result["errors"])

    def test_pm_contract_self_check_requires_rank_and_pm_lifecycle_traces(self):
        complete = self._ranked_new_risk_contract()
        self.assertTrue(check_final_action_contract(complete)["ok"])

        missing_rank_trace = copy.deepcopy(complete)
        missing_rank_trace["capital_deployment"].pop("rank_input_components")
        rank_trace_errors = check_final_action_contract(missing_rank_trace)["errors"]
        self.assertTrue(any("rank_input_components_missing" in error for error in rank_trace_errors))

        missing_lifecycle = copy.deepcopy(complete)
        missing_lifecycle["capital_deployment"].pop("lifecycle_learning_trace")
        lifecycle_errors = check_final_action_contract(missing_lifecycle)["errors"]
        self.assertTrue(any("lifecycle_learning_trace_missing" in error for error in lifecycle_errors))

        missing_pm_trace = copy.deepcopy(complete)
        missing_pm_trace["learning_used"].pop("pm_lifecycle_learning_trace")
        pm_errors = check_final_action_contract(missing_pm_trace)["errors"]
        self.assertIn("pm_lifecycle_learning_trace_missing", pm_errors)

    def test_pm_contract_self_check_accepts_complete_rank_on_add_or_scale(self):
        contract = self._ranked_new_risk_contract()
        contract.update(
            {
                "final_action": "scale",
                "current_lots": 1,
                "target_lots": 2,
                "lots_delta": 1,
            }
        )
        contract["evidence_used"]["position_sizing_result"].update(
            {"current_lots": 1, "target_lots": 2, "lots_delta": 1}
        )
        contract["capital_deployment"].update(
            {
                "selected_for_capital_deployment": True,
                "capital_allocation_reason": "selected_by_full_market_pm_capital_queue",
                "original_target_lots": 2,
                "deployed_target_lots": 2,
                "deployed_lots_delta": 1,
            }
        )

        result = check_final_action_contract(contract)

        self.assertTrue(result["ok"], result["errors"])

    def test_pm_contract_self_check_rejects_decision_source_drift_and_execution_pollution(self):
        drifted = self._ranked_new_risk_contract()
        drifted["learning_used"]["alpha_setup_action_values"][0]["id"] = "different-open"
        drift_errors = check_final_action_contract(drifted)["errors"]
        self.assertIn(
            "alpha_setup_action_values_not_from_final_decision_learning_rows",
            drift_errors,
        )

        polluted = self._ranked_new_risk_contract()
        execution_row = {
            "id": "execution-1",
            "canonical_action_value": True,
            "consumer_scope": "pm_learning",
            "canonical_action_family": "execution",
            "action_name": "execution",
            "action_value_lane": "execution",
            "learning_lane": "execution",
            "action_preference": "positive_candidate_execution",
        }
        polluted["learning_used"]["alpha_setup_action_values"] = [execution_row]
        polluted["learning_used"]["pm_lifecycle_learning_trace"]["decision_learning_rows"] = [
            {"id": "execution-1", "canonical_action_family": "execution", "learning_lane": "execution"}
        ]
        pollution_errors = check_final_action_contract(polluted)["errors"]
        self.assertTrue(
            any(error.startswith("execution_profile_in_decision_learning") for error in pollution_errors)
        )

    def test_pm_contract_self_check_rejects_add_without_rank(self):
        contract = self._complete_contract(
            final_action="scale",
            current_lots=1,
            target_lots=2,
            lots_delta=1,
            position_sizing_result={"current_lots": 1, "target_lots": 2, "lots_delta": 1},
        )

        result = check_final_action_contract(contract)

        self.assertFalse(result["ok"])
        self.assertIn("new_risk_exposure_missing_full_market_rank", result["errors"])

    def test_pm_contract_self_check_requires_non_rank_learning_trace_when_learning_consumed(self):
        contract = self._complete_contract(
            learning_used={
                "alpha_setup_action_values": [
                    {
                        "id": "hold-av-1",
                        "canonical_action_value": True,
                        "consumer_scope": "pm_learning",
                        "canonical_action_family": "hold",
                        "learning_lane": "hold",
                        "action_value_lane": "hold",
                        "action_name": "hold",
                        "action_preference": "positive_candidate_hold",
                    },
                ]
            },
        )

        missing = check_final_action_contract(contract)

        self.assertFalse(missing["ok"])
        self.assertIn("lifecycle_learning_trace_missing", missing["errors"])
        self.assertIn("pm_lifecycle_learning_trace_missing", missing["errors"])

        contract["evidence_used"] = {
            "position_sizing_result": self._position_sizing(),
        }
        contract["learning_used"]["pm_lifecycle_learning_trace"] = {
            "contract_lifecycle_port": "hold",
            "used_lanes": ["hold"],
            "decision_learning_rows": [{"id": "hold-av-1", "learning_lane": "hold", "action_name": "hold"}],
            "trigger_profile_learning_rows": [],
            "execution_profile_learning_direct_to_rank": False,
            "trigger_profile_learning_direct_to_rank": False,
        }
        contract["learning_used"]["pm_lifecycle_learning_impact_delta"] = {
            "hold_decision": "continue_hold",
        }
        self.assertTrue(check_final_action_contract(contract)["ok"])

    def test_pm_contract_self_check_rejects_incomplete_prior_in_formal_action_values(self):
        contract = self._complete_contract(
            learning_used={
                "alpha_setup_action_values": [
                    {
                        "ticker": "*",
                        "side": "short",
                        "setup_type": "*",
                        "action_name": "open",
                        "action_value_lane": "open",
                        "learning_lane": "open",
                        "canonical_action_value": False,
                        "canonical_action_value_source": "incomplete_trace_not_for_pm_scoring",
                        "evidence_scope": "similar_sql_prior",
                    },
                ],
                "pm_lifecycle_learning_trace": {
                    "contract_lifecycle_port": "hold",
                    "used_lanes": ["hold"],
                    "decision_learning_rows": [{"id": "hold-1", "learning_lane": "hold", "action_name": "hold"}],
                    "trigger_profile_learning_rows": [],
                    "execution_profile_learning_direct_to_rank": False,
                    "trigger_profile_learning_direct_to_rank": False,
                },
                "pm_lifecycle_learning_impact_delta": {
                    "hold_decision": "continue_hold",
                },
            },
            evidence_used={},
        )

        result = check_final_action_contract(contract)

        self.assertFalse(result["ok"])
        self.assertIn("alpha_setup_action_value_not_canonical:0", result["errors"])
        self.assertIn("alpha_setup_action_value_missing_canonical_action_family:0", result["errors"])
        self.assertIn("alpha_setup_action_value_missing_action_preference:0", result["errors"])
        self.assertIn("alpha_setup_action_value_incomplete_similar_sql_prior:0", result["errors"])
        self.assertIn("alpha_setup_action_value_incomplete_trace_not_for_pm_scoring:0", result["errors"])

if __name__ == "__main__":
    unittest.main()
