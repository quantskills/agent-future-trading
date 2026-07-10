import copy
import unittest
from pathlib import Path
import sys


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.decision.pm_contract_builder import build_final_action_contract
from tools.agent_tools.decision.pm_contract_self_check import check_final_action_contract
from tools.agent_tools.decision.pm_lifecycle_action_port import (
    build_lifecycle_transition_diagnostic,
    classify_lifecycle_action_port,
)
from tools.agent_tools.decision.pm_position_transition import classify_position_transition
from tools.agent_tools.decision.pm_state_transition import (
    classify_new_entry_transition,
    classify_pm_decision_state,
)
from tools.common.final_action_semantics import full_market_rank_source_payload


class PMStateTransitionMatrixTest(unittest.TestCase):
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

    def _lifecycle_transition_diagnostic(self, contract, primary_contract=None, reason_codes=None):
        primary = classify_lifecycle_action_port(primary_contract or contract)
        final = classify_lifecycle_action_port(contract)
        return build_lifecycle_transition_diagnostic(
            primary_lifecycle_action_port=primary,
            contract_lifecycle_port=final,
            reason_codes=reason_codes or contract.get("reason_codes") or [],
            control_reasons=reason_codes or contract.get("reason_codes") or [],
        )

    def _complete_contract(self, **overrides):
        contract = {
            "ticker": "BU",
            "final_action": "hold",
            "current_lots": 1,
            "target_lots": 1,
            "lots_delta": 0,
            "reason_codes": ["test_contract"],
            "execution_profile": "position_management",
            "entry_trigger": "",
            "invalidation": "",
            "learning_used": {},
            "evidence_used": {},
            "capital_deployment": self._non_rank_deployment(),
            "position_sizing_result": self._position_sizing(),
        }
        contract.update(overrides)
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
            learning_used={
                "pm_lifecycle_learning_trace": pm_trace,
                "pm_lifecycle_learning_impact_delta": {"net_lifecycle_learning_delta": 0.04},
            },
            evidence_used=dict(rank_trace),
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
            evidence_used={
                **rank_trace,
                "pm_lifecycle_learning_trace": pm_trace,
                "pm_lifecycle_learning_impact_delta": {"deployment": "not_selected"},
            },
            capital_deployment=deployment,
            position_sizing_result={"target_lots": 0, "target_position_ratio": 0.0},
            conditional_trigger_authority=False,
            requires_intraday_confirmation=False,
            can_execute_without_intraday_trigger=False,
        )
        diagnostic = self._lifecycle_transition_diagnostic(
            contract,
            primary_contract={
                "current_lots": 0,
                "target_lots": 1,
                "final_action": "open_probe",
            },
            reason_codes=["no_rank_or_budget_no_new_exposure"],
        )
        self.assertTrue(diagnostic["diagnostic_only"])
        return contract

    def test_watch_for_trigger_complete_setup_becomes_conditional_contract_candidate(self):
        result = classify_new_entry_transition(
            opportunity_state="watch_for_trigger",
            setup_complete=True,
            entry_trigger_present=True,
            invalidation_present=True,
            current_trigger_confirmed=False,
            risk_budget_ok=True,
        )
        self.assertEqual(result["decision"], "conditional_trigger_contract")
        self.assertTrue(result["requires_intraday_confirmation"])
        self.assertFalse(result["can_execute_without_intraday_trigger"])
        self.assertEqual(result["final_action_hint"], "open_probe")

    def test_watch_for_trigger_missing_setup_stays_wait(self):
        result = classify_new_entry_transition(
            opportunity_state="watch_for_trigger",
            setup_complete=False,
            entry_trigger_present=True,
            invalidation_present=True,
            current_trigger_confirmed=False,
            risk_budget_ok=True,
        )
        self.assertEqual(result["decision"], "wait")
        self.assertIn("setup", result["missing_required_fields"])

    def test_probe_candidate_requires_current_confirmation_and_budget(self):
        blocked = classify_new_entry_transition(
            opportunity_state="probe_candidate",
            setup_complete=True,
            entry_trigger_present=True,
            invalidation_present=True,
            current_trigger_confirmed=False,
            risk_budget_ok=True,
        )
        allowed = classify_new_entry_transition(
            opportunity_state="probe_candidate",
            setup_complete=True,
            entry_trigger_present=True,
            invalidation_present=True,
            current_trigger_confirmed=True,
            risk_budget_ok=True,
        )
        self.assertEqual(blocked["decision"], "watch_for_trigger")
        self.assertEqual(allowed["decision"], "open_probe")

    def test_tradeable_candidate_can_open_real_or_scale_only_after_confirmation(self):
        pending = classify_new_entry_transition(
            opportunity_state="tradeable_candidate",
            setup_complete=True,
            entry_trigger_present=True,
            invalidation_present=True,
            current_trigger_confirmed=False,
            risk_budget_ok=True,
            evidence_strength="strong",
        )
        real = classify_new_entry_transition(
            opportunity_state="tradeable_candidate",
            setup_complete=True,
            entry_trigger_present=True,
            invalidation_present=True,
            current_trigger_confirmed=True,
            risk_budget_ok=True,
            evidence_strength="strong",
        )
        scale = classify_new_entry_transition(
            opportunity_state="tradeable_candidate",
            setup_complete=True,
            entry_trigger_present=True,
            invalidation_present=True,
            current_trigger_confirmed=True,
            risk_budget_ok=True,
            evidence_strength="strong",
            positive_learning=True,
            rank_priority=True,
            scale_allowed=True,
        )
        self.assertEqual(pending["decision"], "watch_for_trigger")
        self.assertEqual(real["decision"], "open_real")
        self.assertEqual(scale["decision"], "scale")

    def test_hard_and_negative_blocks_override_release_inputs(self):
        hard = classify_new_entry_transition(
            opportunity_state="tradeable_candidate",
            setup_complete=True,
            entry_trigger_present=True,
            invalidation_present=True,
            current_trigger_confirmed=True,
            risk_budget_ok=True,
            evidence_strength="strong",
            hard_block=True,
            positive_learning=True,
        )
        negative = classify_new_entry_transition(
            opportunity_state="tradeable_candidate",
            setup_complete=True,
            entry_trigger_present=True,
            invalidation_present=True,
            current_trigger_confirmed=True,
            risk_budget_ok=True,
            evidence_strength="strong",
            negative_learning_block=True,
        )
        self.assertEqual(hard["decision"], "wait")
        self.assertEqual(negative["decision"], "wait")

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
            execution_contract_fields={"execution_profile": "breakout"},
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
        contract.pop("position_sizing_result")

        result = check_final_action_contract(contract)

        self.assertFalse(result["ok"])
        self.assertIn("missing_final_action_contract_position_sizing_result", result["errors"])

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
        missing_rank_trace["evidence_used"].pop("rank_input_components")
        missing_rank_trace["capital_deployment"].pop("rank_input_components")
        rank_trace_errors = check_final_action_contract(missing_rank_trace)["errors"]
        self.assertTrue(any("rank_input_components_missing" in error for error in rank_trace_errors))

        missing_lifecycle = copy.deepcopy(complete)
        missing_lifecycle["evidence_used"].pop("lifecycle_learning_trace")
        missing_lifecycle["capital_deployment"].pop("lifecycle_learning_trace")
        lifecycle_errors = check_final_action_contract(missing_lifecycle)["errors"]
        self.assertTrue(any("lifecycle_learning_trace_missing" in error for error in lifecycle_errors))

        missing_pm_trace = copy.deepcopy(complete)
        missing_pm_trace["learning_used"].pop("pm_lifecycle_learning_trace")
        pm_errors = check_final_action_contract(missing_pm_trace)["errors"]
        self.assertIn("pm_lifecycle_learning_trace_missing", pm_errors)

    def test_pm_contract_self_check_requires_non_rank_learning_trace_when_learning_consumed(self):
        contract = self._complete_contract(
            learning_used={
                "alpha_setup_action_values": [
                    {"learning_lane": "hold", "action_name": "hold", "action_preference": "hold"},
                ]
            },
        )

        missing = check_final_action_contract(contract)

        self.assertFalse(missing["ok"])
        self.assertIn("lifecycle_learning_trace_missing", missing["errors"])
        self.assertIn("pm_lifecycle_learning_trace_missing", missing["errors"])

        contract["evidence_used"] = {
            "lifecycle_learning_trace": {
                "contract_lifecycle_port": "hold",
                "used_lanes": ["hold"],
                "decision_learning_rows": [{"id": "hold-1", "learning_lane": "hold", "action_name": "hold"}],
                "trigger_profile_learning_rows": [],
                "execution_profile_learning_direct_to_rank": False,
                "trigger_profile_learning_direct_to_rank": False,
                "execution_profile_signal_direct_to_rank": False,
            },
            "learning_impact_delta": {"hold_decision": "continue_hold"},
        }
        contract["learning_used"]["pm_lifecycle_learning_trace"] = {
            "contract_lifecycle_port": "hold",
            "used_lanes": ["hold"],
            "decision_learning_rows": [{"id": "hold-1", "learning_lane": "hold", "action_name": "hold"}],
            "trigger_profile_learning_rows": [],
            "execution_profile_learning_direct_to_rank": False,
            "trigger_profile_learning_direct_to_rank": False,
        }
        contract["learning_used"]["pm_lifecycle_learning_impact_delta"] = {
            "hold_decision": "continue_hold",
        }
        diagnostic = self._lifecycle_transition_diagnostic(contract)
        self.assertTrue(diagnostic["diagnostic_only"])
        self.assertTrue(check_final_action_contract(contract)["ok"])

    def test_pm_contract_self_check_rejects_raw_research_objects_in_pm_artifact(self):
        contract = self._complete_contract()

        result = check_final_action_contract(
            contract,
            pm_artifact={
                "final_action_contract": contract,
                "adaptive_policy_scope": {"policies": [{"policy_action": "raw"}]},
            },
        )

        self.assertFalse(result["ok"])
        self.assertTrue(
            any(error.startswith("pm_artifact_boundary_violation:") for error in result["errors"])
        )


if __name__ == "__main__":
    unittest.main()
