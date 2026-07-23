import unittest
from pathlib import Path
import sys


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.common.final_action_semantics import (
    ACTION_PREFERENCE_VALUES,
    authority_allows_entry,
    canonical_action_family,
    canonicalize_final_action_contract_for_persistence,
    canonical_action_preference_for_action_value,
    canonical_action_value_lane,
    classify_final_action_contract,
    classify_final_action_reason_codes,
    classify_reason_codes,
    contract_consumes_hold_exit_pm_learning,
    contract_has_full_market_capital_rank,
    contract_is_unselected_no_new_exposure_candidate,
    contract_requires_conditional_intraday_result,
    derive_memory_requirements,
    derive_accounting_expectation,
    derive_protocol_semantic_checks,
    derive_research_fact_state,
    derive_review_expectation,
    has_open_transaction_blocker,
    has_valid_hold_exit_no_change_explanation,
    filter_action_values_for_contract_learning,
    full_market_rank_gate_errors,
    full_market_rank_source_payload,
    lane_matches_memory_requirement,
    lifecycle_learning_decision_contract_errors,
    rank_capital_layer_contract_errors,
    rank_lifecycle_learning_route_errors,
    requires_intraday_result,
    validate_action_preference_family_consistency,
    validate_action_value_write_consistency,
    validate_final_action_lot_transition,
)


class FinalActionSemanticsTest(unittest.TestCase):
    def _rank_trace(self) -> dict:
        return {
            "rank_input_components": {
                "capital_priority_tier": 1,
                "capital_priority_score": 0.42,
                "watch_priority_score": 0.61,
                "opportunity_score": 0.48,
            },
            "lifecycle_learning_trace": {
                "rank_lifecycle": "open_add_new_risk",
                "allowed_learning_lanes": ["open", "add", "scale", "increase"],
                "blocked_learning_lanes": ["hold", "reduce", "exit", "execution", "conditional_monitor"],
                "used_lanes": ["open"],
                "ignored_lanes": ["execution"],
                "decision_learning_rows": [{"id": "open-1", "learning_lane": "open", "action_name": "open"}],
                "trigger_profile_learning_rows": [],
                "execution_profile_learning_direct_to_rank": False,
                "trigger_profile_learning_direct_to_rank": False,
                "execution_profile_signal_direct_to_rank": False,
                "pm_final_contract_lifecycle_trace": {
                    "trace_version": "agentquant.pm_lifecycle_learning_trace.v1",
                    "contract_lifecycle_port": "open_add_new_risk",
                    "rank_lifecycle": "open_add_new_risk",
                    "allowed_learning_lanes": ["open", "add", "scale", "increase"],
                    "blocked_learning_lanes": ["hold", "reduce", "exit", "execution", "conditional_monitor"],
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
                "positive_learning": 0.04,
                "negative_learning": 0.0,
                "entry_quality_loss_penalty": 0.0,
                "trigger_quality_positive_bonus": 0.02,
                "trigger_quality_loss_penalty": 0.0,
                "net_rank_learning_delta": 0.06,
                "execution_profile_learning_direct_to_rank": False,
            },
        }

    def test_action_value_canonical_family_and_preference_consistency(self):
        self.assertEqual(canonical_action_family("add_or_open"), "open_add_new_risk")
        self.assertEqual(canonical_action_value_lane("add_or_open"), "open")
        self.assertEqual(
            canonical_action_value_lane("add_or_open", current_lots=2, target_lots=3),
            "add",
        )
        self.assertEqual(
            canonical_action_value_lane("close_or_reduce", current_lots=3, target_lots=1),
            "reduce",
        )
        self.assertEqual(canonical_action_value_lane("decrease_position"), "reduce")
        self.assertTrue(validate_action_preference_family_consistency({
            "action_name": "add_or_open",
            "canonical_action_family": "open_add_new_risk",
            "action_value_lane": "open",
            "learning_lane": "open",
            "action_preference": "positive_candidate_open",
        })["ok"])
        self.assertTrue(validate_action_preference_family_consistency({
            "action_name": "reduce_or_exit",
            "canonical_action_family": "reduce_exit",
            "action_value_lane": "exit",
            "learning_lane": "exit",
            "action_preference": "positive_candidate_exit",
        })["ok"])
        self.assertTrue(validate_action_preference_family_consistency({
            "action_name": "execution",
            "canonical_action_family": "execution",
            "action_value_lane": "execution",
            "learning_lane": "execution",
            "action_preference": "positive_candidate_execution",
        })["ok"])

    def test_action_value_family_consistency_hard_fails_bad_or_missing_family(self):
        invalid = validate_action_preference_family_consistency({
            "action_name": "hold",
            "canonical_action_family": "hold",
            "action_value_lane": "hold",
            "learning_lane": "hold",
            "action_preference": "positive_candidate_open",
        })
        self.assertFalse(invalid["ok"])
        self.assertIn("positive_open_family_mismatch", invalid["errors"])

        missing = validate_action_preference_family_consistency({
            "action_name": "add_or_open",
            "action_value_lane": "open",
            "learning_lane": "open",
            "action_preference": "positive_candidate_open",
        })
        self.assertFalse(missing["ok"])
        self.assertIn("missing_canonical_action_family", missing["errors"])

    def _conditional_contract(self) -> dict:
        return {
            "ticker": "SR",
            "current_lots": 0,
            "target_lots": 1,
            "lots_delta": 1,
            "final_action": "open_probe",
            "authority_type": "exploration_probe",
            "open_action_evidence": False,
            "strong_current_evidence": False,
            "conditional_trigger_authority": True,
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "watch_for_trigger_block": False,
            "reason_codes": [
                "pm_watch_for_trigger_probe_cap",
                "real_probe_qualification_not_met",
                "conditional_trigger_authority",
            ],
        }

    def test_real_probe_qualification_not_met_is_soft_limit_only(self):
        summary = classify_reason_codes(["real_probe_qualification_not_met"])

        self.assertIn("real_probe_qualification_not_met", summary["soft_limit_reasons"])
        self.assertNotIn("real_probe_qualification_not_met", summary["hard_block_reasons"])
        self.assertFalse(summary["hard_block"])

    def test_conditional_monitor_with_soft_limit_reaches_intraday_check(self):
        contract = self._conditional_contract()
        semantics = classify_final_action_contract(contract)

        self.assertEqual(semantics["lifecycle_state"], "conditional_monitor")
        self.assertEqual(semantics["execution_permission"], "monitor_intraday")
        self.assertTrue(semantics["requires_intraday_result"])
        self.assertTrue(semantics["can_monitor_intraday"])
        self.assertFalse(semantics["blocked"])
        self.assertTrue(authority_allows_entry(contract))
        self.assertTrue(requires_intraday_result(contract))

    def test_reduced_conditional_probe_authority_allows_intraday_monitoring(self):
        authority = {
            "authority_type": "exploration_probe",
            "authority_decision": "allow_exploration_probe",
            "open_action_evidence": False,
            "strong_current_evidence": False,
            "conditional_trigger_authority": True,
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "watch_for_trigger_block": False,
            "reason_codes": [
                "pm_watch_for_trigger_probe_cap",
                "real_probe_qualification_not_met",
                "conditional_trigger_authority",
            ],
        }

        self.assertTrue(authority_allows_entry(authority))

    def test_bare_probe_authority_without_evidence_does_not_allow_entry(self):
        authority = {
            "authority_type": "exploration_probe",
            "authority_decision": "allow_exploration_probe",
            "open_action_evidence": False,
            "strong_current_evidence": False,
            "conditional_trigger_authority": False,
            "requires_intraday_confirmation": False,
            "can_execute_without_intraday_trigger": False,
            "watch_for_trigger_block": False,
            "reason_codes": ["test_pm_final_trade_authority"],
        }

        self.assertFalse(authority_allows_entry(authority))

    def test_direct_open_add_reduce_and_exit_lifecycle_states(self):
        cases = [
            ("open", 0, 2, "open"),
            ("add", 1, 2, "increase"),
            ("scale", -1, -2, "increase"),
            ("reduce", 3, 1, "decrease"),
            ("exit", -2, 0, "exit"),
        ]
        for action, current_lots, target_lots, expected in cases:
            with self.subTest(action=action):
                contract = {
                    "current_lots": current_lots,
                    "target_lots": target_lots,
                    "lots_delta": target_lots - current_lots,
                    "final_action": action,
                    "authority_type": "real_budget_entry",
                    "open_action_evidence": True,
                    "strong_current_evidence": True,
                    "reason_codes": [],
                }
                semantics = classify_final_action_contract(contract)
                self.assertEqual(semantics["lifecycle_state"], expected)
                self.assertEqual(semantics["execution_permission"], "direct_execute")

    def test_memory_requirements_follow_trade_lifecycle_side_roles(self):
        cases = [
            (
                "open",
                {"current_lots": 0, "target_lots": 2, "lots_delta": 2, "final_action": "open"},
                {("open", "long", "target_side")},
            ),
            (
                "add",
                {"current_lots": 1, "target_lots": 3, "lots_delta": 2, "final_action": "add"},
                {
                    ("add", "long", "target_side"),
                    ("open", "long", "target_side"),
                    ("hold", "long", "current_position_side"),
                },
            ),
            (
                "reduce",
                {"current_lots": -4, "target_lots": -1, "lots_delta": 3, "final_action": "reduce"},
                {
                    ("reduce", "short", "current_position_side"),
                    ("hold", "short", "current_position_side"),
                    ("exit", "short", "current_position_side"),
                },
            ),
            (
                "exit",
                {"current_lots": 13, "target_lots": 0, "lots_delta": -13, "final_action": "exit"},
                {
                    ("exit", "long", "current_position_side"),
                    ("hold", "long", "current_position_side"),
                    ("reduce", "long", "current_position_side"),
                },
            ),
            (
                "conditional",
                {
                    "current_lots": 0,
                    "target_lots": -1,
                    "lots_delta": -1,
                    "final_action": "open_probe",
                    "conditional_trigger_authority": True,
                    "requires_intraday_confirmation": True,
                    "can_execute_without_intraday_trigger": False,
                },
                {("open", "short", "target_side")},
            ),
        ]
        for label, contract, expected in cases:
            with self.subTest(label=label):
                result = derive_memory_requirements(contract)
                got = {
                    (row["lane"], row["side"], row["memory_side_role"])
                    for row in result["must_land_in_pm_contract"]
                }
                self.assertTrue(expected.issubset(got), result)

    def test_conditional_open_uses_open_memory_without_changing_trader_trigger_semantics(self):
        contract = self._conditional_contract()

        memory = derive_memory_requirements(contract)
        execution = classify_final_action_contract(contract)

        self.assertEqual(memory["action_lifecycle"], "open")
        self.assertEqual(
            {
                (row["lane"], row["side"], row["memory_side_role"])
                for row in memory["must_land_in_pm_contract"]
            },
            {("open", "long", "target_side")},
        )
        self.assertEqual(execution["lifecycle_state"], "conditional_monitor")
        self.assertEqual(execution["execution_permission"], "monitor_intraday")
        self.assertTrue(contract_requires_conditional_intraday_result(contract))

        routed = filter_action_values_for_contract_learning(contract, [{
            "id": "open-long-1",
            "action_name": "open",
            "canonical_action_value": True,
            "canonical_action_family": "open_add_new_risk",
            "action_value_lane": "open",
            "learning_lane": "open",
            "consumer_scope": "pm_learning",
            "side": "long",
            "memory_side_role": "target_side",
            "action_preference": "positive_candidate_open",
            "reward_sum": 100.0,
            "reward_mean": 100.0,
        }])
        self.assertEqual([row["id"] for row in routed["rows"]], ["open-long-1"])
        self.assertEqual(routed["rejected_action_values"], [])

    def test_flat_undeployed_conditional_candidate_keeps_monitor_memory_only(self):
        contract = {
            "current_lots": 0,
            "target_lots": 0,
            "lots_delta": 0,
            "final_action": "wait",
            "side": "short",
            "conditional_trigger_authority": True,
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "reason_codes": ["no_rank_or_budget_no_new_exposure"],
        }

        memory = derive_memory_requirements(contract)

        self.assertEqual(memory["action_lifecycle"], "conditional_monitor")
        self.assertEqual(
            {
                (row["lane"], row["side"], row["memory_side_role"])
                for row in memory["must_land_in_pm_contract"]
            },
            {("conditional_monitor", "short", "trigger_side")},
        )
        self.assertFalse(contract_requires_conditional_intraday_result(contract))

    def test_unchanged_existing_position_uses_monitor_memory_only_when_contract_is_pure_monitor(self):
        contract = {
            "current_lots": 2,
            "target_lots": 2,
            "lots_delta": 0,
            "final_action": "hold",
            "side": "long",
            "conditional_trigger_authority": True,
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
        }

        memory = derive_memory_requirements(contract)

        self.assertEqual(memory["action_lifecycle"], "conditional_monitor")
        self.assertEqual(
            {
                (row["lane"], row["side"], row["memory_side_role"])
                for row in memory["must_land_in_pm_contract"]
            },
            {("conditional_monitor", "long", "trigger_side")},
        )

    def test_accountant_reviewer_researcher_pg_read_same_lifecycle(self):
        contract = self._conditional_contract()
        accounting = derive_accounting_expectation(contract, {"actual_transactions": []})
        review = derive_review_expectation(contract, {"actual_transactions": []})
        research = derive_research_fact_state(contract, {"status": "intraday_trigger_not_met"})
        protocol = derive_protocol_semantic_checks(contract)

        self.assertTrue(accounting["no_trigger_no_accounting_mutation"])
        self.assertFalse(accounting["fee_allowed"])
        self.assertEqual(review["lifecycle_state"], "conditional_monitor")
        self.assertEqual(research["lifecycle_state"], "conditional_monitor")
        self.assertTrue(protocol["requires_intraday_result"])

    def test_rank_capital_layer_contract_requires_complete_rank_metadata(self):
        incomplete = {
            "current_lots": 0,
            "target_lots": 1,
            "lots_delta": 1,
            "evidence_used": {"opportunity_rank": 1},
            "capital_deployment": {
                "selected_for_capital_deployment": True,
                "opportunity_rank": 1,
                "capital_allocation_reason": "selected_by_full_market_pm_capital_queue",
            },
        }
        complete = {
            "current_lots": 0,
            "target_lots": 1,
            "lots_delta": 1,
            "evidence_used": {},
            "capital_deployment": {
                "selected_for_capital_deployment": True,
                "opportunity_rank": 1,
                "capital_allocation_reason": "selected_by_full_market_pm_capital_queue",
                "rank_capital_role": "best_exploration_probe_candidate",
                "capital_layer": "exploration_probe",
                "capital_ratio_source": "probe_margin_ratio_0.008",
                "rank_reason": "best_watch_for_trigger_by_evidence_trigger_learning_and_risk",
                **self._rank_trace(),
                **full_market_rank_source_payload(),
            },
        }

        self.assertIn(
            "capital_deployment.capital_layer_missing",
            rank_capital_layer_contract_errors(incomplete),
        )
        self.assertEqual(rank_capital_layer_contract_errors(complete), [])

    def test_canonical_persistence_turns_flat_hold_into_wait(self):
        contract = {
            "final_action": "hold",
            "current_lots": 0,
            "target_lots": 0,
            "lots_delta": 0,
        }

        canonical = canonicalize_final_action_contract_for_persistence(contract)

        self.assertEqual(canonical["final_action"], "wait")
        self.assertEqual(canonical["current_lots"], 0)
        self.assertEqual(canonical["target_lots"], 0)
        self.assertEqual(canonical["lots_delta"], 0)
        self.assertTrue(validate_final_action_lot_transition(canonical)["ok"])

    def test_canonical_persistence_syncs_rank_metadata_to_deployment(self):
        contract = {
            "final_action": "open_probe",
            "current_lots": 0,
            "target_lots": -1,
            "lots_delta": -1,
            "evidence_used": {
            },
            "capital_deployment": {
                "selected_for_capital_deployment": True,
                "capital_allocation_reason": "selected_by_full_market_pm_capital_queue",
                "opportunity_rank": 1,
                "rank_capital_role": "best_exploration_probe_candidate",
                "capital_layer": "exploration_probe",
                "capital_ratio_source": "probe_margin_ratio_0.008",
                "rank_reason": "best_watch_for_trigger_by_evidence_trigger_learning_and_risk",
                **self._rank_trace(),
                **full_market_rank_source_payload(),
            },
        }

        canonical = canonicalize_final_action_contract_for_persistence(contract)

        self.assertEqual(canonical["capital_deployment"]["capital_layer"], "exploration_probe")
        self.assertEqual(
            canonical["capital_deployment"]["capital_ratio_source"],
            "probe_margin_ratio_0.008",
        )
        self.assertEqual(rank_capital_layer_contract_errors(canonical), [])

    def test_canonical_persistence_preserves_lifecycle_trace_when_clearing_non_rank_fields(self):
        contract = {
            "final_action": "hold",
            "current_lots": -3,
            "target_lots": -3,
            "lots_delta": 0,
            "learning_used": {
                "alpha_setup_action_values": [
                    {"learning_lane": "hold", "action_name": "hold"},
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
            "evidence_used": {
                "opportunity_rank": 1,
                "rank_source": "ticker_side_priority",
                "rank_input_components": {"old_local_rank_score": 0.9},
                "capital_layer": "exploration_probe",
            },
            "capital_deployment": {
                "opportunity_rank": 1,
                "rank_source": "ticker_side_priority",
                "rank_input_components": {"old_local_rank_score": 0.9},
                "capital_layer": "exploration_probe",
                "selected_for_capital_deployment": False,
            },
        }

        canonical = canonicalize_final_action_contract_for_persistence(contract)

        evidence = canonical["evidence_used"]
        deployment = canonical["capital_deployment"]
        self.assertNotIn("opportunity_rank", evidence)
        self.assertNotIn("rank_source", evidence)
        self.assertNotIn("rank_input_components", evidence)
        self.assertNotIn("capital_layer", evidence)
        self.assertNotIn("opportunity_rank", deployment)
        self.assertNotIn("rank_source", deployment)
        self.assertNotIn("rank_input_components", deployment)
        self.assertEqual(canonical["learning_used"]["pm_lifecycle_learning_trace"]["contract_lifecycle_port"], "hold")
        self.assertEqual(canonical["learning_used"]["pm_lifecycle_learning_impact_delta"]["hold_decision"], "continue_hold")
        self.assertEqual(lifecycle_learning_decision_contract_errors(canonical), [])

    def test_rank_lifecycle_route_rejects_hold_or_execution_learning_in_open_rank(self):
        contract = {
            "final_action": "open_probe",
            "current_lots": 0,
            "target_lots": 1,
            "lots_delta": 1,
            "evidence_used": {},
            "capital_deployment": {
                "selected_for_capital_deployment": True,
                "opportunity_rank": 1,
                "capital_allocation_reason": "selected_by_full_market_pm_capital_queue",
                "rank_capital_role": "best_exploration_probe_candidate",
                "capital_layer": "exploration_probe",
                "capital_ratio_source": "probe_margin_ratio_0.008",
                "rank_reason": "best_watch_for_trigger_by_evidence_trigger_learning_and_risk",
                **self._rank_trace(),
                **full_market_rank_source_payload(),
            },
            "learning_used": {
                "pm_lifecycle_learning_trace": dict(
                    self._rank_trace()["lifecycle_learning_trace"]["pm_final_contract_lifecycle_trace"]
                ),
            },
        }
        contract["learning_used"]["pm_lifecycle_learning_trace"]["decision_learning_rows"] = [
            {"id": "open-1", "learning_lane": "open", "action_name": "open"},
            {"id": "exec-1", "learning_lane": "execution", "action_name": "execution"},
        ]

        errors = rank_lifecycle_learning_route_errors(contract)

        self.assertTrue(any(error.startswith("open_rank_mixed_forbidden_learning_lanes") for error in errors))

    def test_rank_lifecycle_route_requires_step6_final_trace(self):
        contract = {
            "final_action": "open_probe",
            "current_lots": 0,
            "target_lots": 1,
            "lots_delta": 1,
            "evidence_used": {},
            "capital_deployment": {
                "selected_for_capital_deployment": True,
                "opportunity_rank": 1,
                "capital_allocation_reason": "selected_by_full_market_pm_capital_queue",
                "rank_capital_role": "best_exploration_probe_candidate",
                "capital_layer": "exploration_probe",
                "capital_ratio_source": "probe_margin_ratio_0.008",
                "rank_reason": "best_watch_for_trigger_by_evidence_trigger_learning_and_risk",
                **self._rank_trace(),
                **full_market_rank_source_payload(),
            },
            "learning_used": {
                "pm_lifecycle_learning_trace": dict(
                    self._rank_trace()["lifecycle_learning_trace"]["pm_final_contract_lifecycle_trace"]
                ),
            },
        }
        contract["learning_used"].pop("pm_lifecycle_learning_trace")
        self.assertIn(
            "pm_final_contract_lifecycle_trace_missing",
            rank_lifecycle_learning_route_errors(contract),
        )

        contract["learning_used"]["pm_lifecycle_learning_trace"] = {
            "trace_version": "agentquant.pm_lifecycle_learning_trace.v1",
            "contract_lifecycle_port": "hold",
            "decision_learning_rows": [{"id": "hold-1", "learning_lane": "hold", "action_name": "hold"}],
            "trigger_profile_learning_rows": [],
            "execution_profile_learning_direct_to_rank": False,
            "trigger_profile_learning_direct_to_rank": False,
        }
        self.assertIn(
            "final_lifecycle_trace_port_mismatch:hold:open_add_new_risk",
            rank_lifecycle_learning_route_errors(contract),
        )

    def test_non_rank_lifecycle_learning_requires_trace_and_rejects_mixed_lanes(self):
        contract = {
            "current_lots": -4,
            "target_lots": -4,
            "lots_delta": 0,
            "final_action": "hold",
            "evidence_used": {},
            "learning_used": {
                "alpha_setup_action_values": [
                    {"learning_lane": "hold", "action_name": "hold"},
                ]
            },
        }
        self.assertEqual(
            lifecycle_learning_decision_contract_errors(contract),
            ["lifecycle_learning_trace_missing"],
        )

        contract["learning_used"].update({
            "pm_lifecycle_learning_trace": {
                "contract_lifecycle_port": "hold",
                "used_lanes": ["hold"],
                "decision_learning_rows": [{"id": "hold-1", "learning_lane": "hold", "action_name": "hold"}],
                "trigger_profile_learning_rows": [],
                "execution_profile_learning_direct_to_rank": False,
                "trigger_profile_learning_direct_to_rank": False,
                "execution_profile_signal_direct_to_rank": False,
            },
            "pm_lifecycle_learning_impact_delta": {"hold_decision": "continue_hold"},
        })
        self.assertEqual(lifecycle_learning_decision_contract_errors(contract), [])

        contract["learning_used"]["pm_lifecycle_learning_trace"]["decision_learning_rows"].append(
            {"learning_lane": "open", "action_name": "open"}
        )
        errors = lifecycle_learning_decision_contract_errors(contract)
        self.assertIn("hold_lifecycle_mixed_forbidden_learning_lanes:open", errors)

    def test_reduce_exit_allows_execution_only_in_trigger_profile_rows(self):
        contract = {
            "current_lots": -10,
            "target_lots": -5,
            "lots_delta": 5,
            "final_action": "reduce",
            "evidence_used": {},
            "learning_used": {
                "pm_lifecycle_learning_trace": {
                    "contract_lifecycle_port": "reduce_exit",
                    "used_lanes": ["reduce", "execution"],
                    "decision_learning_rows": [
                        {"id": "reduce-1", "learning_lane": "reduce", "action_name": "reduce"}
                    ],
                    "trigger_profile_learning_rows": [
                        {"id": "exec-1", "learning_lane": "execution", "action_name": "execution"}
                    ],
                    "execution_profile_learning_direct_to_rank": False,
                    "trigger_profile_learning_direct_to_rank": False,
                },
                "pm_lifecycle_learning_impact_delta": {
                    "reduce_exit_decision": "reduce_exposure",
                    "execution_profile_learning_direct_to_rank": False,
                },
                "alpha_setup_action_values": [
                    {"id": "reduce-1", "learning_lane": "reduce", "action_name": "reduce"},
                    {"id": "exec-1", "learning_lane": "execution", "action_name": "execution"},
                ]
            },
        }

        self.assertEqual(lifecycle_learning_decision_contract_errors(contract), [])

    def test_reduce_exit_rejects_execution_in_decision_rows(self):
        contract = {
            "current_lots": -10,
            "target_lots": -5,
            "lots_delta": 5,
            "final_action": "reduce",
            "evidence_used": {},
            "learning_used": {
                "pm_lifecycle_learning_trace": {
                    "contract_lifecycle_port": "reduce_exit",
                    "decision_learning_rows": [
                        {"id": "exec-1", "learning_lane": "execution", "action_name": "execution"}
                    ],
                    "trigger_profile_learning_rows": [],
                    "execution_profile_learning_direct_to_rank": False,
                    "trigger_profile_learning_direct_to_rank": False,
                },
                "pm_lifecycle_learning_impact_delta": {"reduce_exit_decision": "reduce_exposure"},
                "alpha_setup_action_values": [
                    {"id": "exec-1", "learning_lane": "execution", "action_name": "execution"}
                ]
            },
        }

        self.assertIn(
            "reduce_exit_lifecycle_mixed_forbidden_learning_lanes:execution",
            lifecycle_learning_decision_contract_errors(contract),
        )

    def test_open_rank_allows_execution_trigger_profile_but_rejects_decision_execution(self):
        contract = {
            "final_action": "open_probe",
            "current_lots": 0,
            "target_lots": 1,
            "lots_delta": 1,
            "evidence_used": {},
            "capital_deployment": {
                "selected_for_capital_deployment": True,
                "opportunity_rank": 1,
                "capital_allocation_reason": "selected_by_full_market_pm_capital_queue",
                "rank_capital_role": "best_exploration_probe_candidate",
                "capital_layer": "exploration_probe",
                "capital_ratio_source": "probe_margin_ratio_0.008",
                "rank_reason": "best_watch_for_trigger_by_evidence_trigger_learning_and_risk",
                **self._rank_trace(),
                **full_market_rank_source_payload(),
            },
            "learning_used": {
                "pm_lifecycle_learning_trace": dict(
                    self._rank_trace()["lifecycle_learning_trace"]["pm_final_contract_lifecycle_trace"]
                ),
            },
        }
        trigger_row = {"id": "exec-1", "learning_lane": "execution", "action_name": "execution"}
        contract["learning_used"]["pm_lifecycle_learning_trace"]["trigger_profile_learning_rows"] = [trigger_row]

        self.assertEqual(rank_lifecycle_learning_route_errors(contract), [])

        contract["learning_used"]["pm_lifecycle_learning_trace"]["decision_learning_rows"] = [trigger_row]
        self.assertIn(
            "open_rank_mixed_forbidden_learning_lanes:execution",
            rank_lifecycle_learning_route_errors(contract),
        )

    def test_hold_and_conditional_monitor_decision_rows_are_lifecycle_scoped(self):
        hold_contract = {
            "current_lots": 3,
            "target_lots": 3,
            "lots_delta": 0,
            "final_action": "hold",
            "evidence_used": {},
            "learning_used": {
                "pm_lifecycle_learning_trace": {
                    "contract_lifecycle_port": "hold",
                    "decision_learning_rows": [
                        {"id": "hold-1", "learning_lane": "hold", "action_name": "hold"}
                    ],
                    "trigger_profile_learning_rows": [
                        {"id": "exec-1", "learning_lane": "execution", "action_name": "execution"}
                    ],
                    "execution_profile_learning_direct_to_rank": False,
                    "trigger_profile_learning_direct_to_rank": False,
                },
                "pm_lifecycle_learning_impact_delta": {"hold_decision": "continue_hold"},
                "alpha_setup_action_values": [
                    {"id": "hold-1", "learning_lane": "hold", "action_name": "hold"},
                    {"id": "exec-1", "learning_lane": "execution", "action_name": "execution"},
                ]
            },
        }
        self.assertEqual(lifecycle_learning_decision_contract_errors(hold_contract), [])
        hold_contract["learning_used"]["pm_lifecycle_learning_trace"]["decision_learning_rows"] = [
            {"id": "exit-1", "learning_lane": "exit", "action_name": "exit"}
        ]
        self.assertIn(
            "hold_lifecycle_mixed_forbidden_learning_lanes:exit",
            lifecycle_learning_decision_contract_errors(hold_contract),
        )

        conditional_contract = {
            "current_lots": 0,
            "target_lots": 0,
            "lots_delta": 0,
            "final_action": "wait",
            "conditional_trigger_authority": True,
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "evidence_used": {},
            "learning_used": {
                "pm_lifecycle_learning_trace": {
                    "contract_lifecycle_port": "conditional_monitor",
                    "decision_learning_rows": [
                        {
                            "id": "monitor-1",
                            "learning_lane": "conditional_monitor",
                            "action_name": "conditional_monitor",
                        }
                    ],
                    "trigger_profile_learning_rows": [],
                    "execution_profile_learning_direct_to_rank": False,
                    "trigger_profile_learning_direct_to_rank": False,
                },
                "pm_lifecycle_learning_impact_delta": {"conditional_monitor_decision": "watch"},
                "alpha_setup_action_values": [
                    {
                        "id": "monitor-1",
                        "learning_lane": "conditional_monitor",
                        "action_name": "conditional_monitor",
                    }
                ]
            },
        }
        self.assertEqual(lifecycle_learning_decision_contract_errors(conditional_contract), [])
        conditional_contract["learning_used"]["pm_lifecycle_learning_trace"]["decision_learning_rows"] = [
            {"id": "hold-1", "learning_lane": "hold", "action_name": "hold"}
        ]
        self.assertIn(
            "conditional_monitor_mixed_forbidden_learning_lanes:hold",
            lifecycle_learning_decision_contract_errors(conditional_contract),
        )

    def test_lifecycle_trace_requires_decision_and_trigger_profile_rows(self):
        contract = {
            "current_lots": 2,
            "target_lots": 2,
            "lots_delta": 0,
            "final_action": "hold",
            "evidence_used": {},
            "learning_used": {
                "pm_lifecycle_learning_trace": {
                    "contract_lifecycle_port": "hold",
                    "decision_learning_rows": [
                        {"id": "hold-1", "learning_lane": "hold", "action_name": "hold"}
                    ],
                    "execution_profile_learning_direct_to_rank": False,
                    "trigger_profile_learning_direct_to_rank": False,
                },
                "pm_lifecycle_learning_impact_delta": {"hold_decision": "continue_hold"},
                "alpha_setup_action_values": [
                    {"id": "hold-1", "learning_lane": "hold", "action_name": "hold"}
                ]
            },
        }
        self.assertIn("trigger_profile_learning_rows_missing", lifecycle_learning_decision_contract_errors(contract))

        contract["learning_used"]["pm_lifecycle_learning_trace"].pop("decision_learning_rows")
        contract["learning_used"]["pm_lifecycle_learning_trace"]["trigger_profile_learning_rows"] = []
        self.assertIn("decision_learning_rows_missing", lifecycle_learning_decision_contract_errors(contract))

    def test_unselected_conditional_candidate_does_not_require_intraday_result(self):
        contract = {
            "final_action": "wait",
            "current_lots": 0,
            "target_lots": 0,
            "lots_delta": 0,
            "conditional_trigger_authority": True,
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "reason_codes": ["no_rank_no_new_exposure", "conditional_trigger_authority"],
            "capital_deployment": {
                "selected_for_capital_deployment": False,
                "original_target_lots": -4,
                "deployed_target_lots": 0,
                "deployed_lots_delta": 0,
                "capital_allocation_reason": "no_rank_no_new_exposure",
            },
        }

        self.assertTrue(contract_is_unselected_no_new_exposure_candidate(contract))
        self.assertFalse(contract_requires_conditional_intraday_result(contract))
        self.assertFalse(classify_final_action_contract(contract)["requires_intraday_result"])

    def test_deployed_conditional_open_requires_intraday_result(self):
        contract = {
            "final_action": "open_probe",
            "current_lots": 0,
            "target_lots": -1,
            "lots_delta": -1,
            "conditional_trigger_authority": True,
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "capital_deployment": {
                "selected_for_capital_deployment": True,
                "original_target_lots": -1,
                "deployed_target_lots": -1,
                "deployed_lots_delta": -1,
            },
        }

        self.assertFalse(contract_is_unselected_no_new_exposure_candidate(contract))
        self.assertTrue(contract_requires_conditional_intraday_result(contract))
        self.assertTrue(classify_final_action_contract(contract)["requires_intraday_result"])

    def test_contract_learning_filter_rejects_mismatched_open_and_execution_rows_for_hold(self):
        contract = {
            "current_lots": -22,
            "target_lots": -22,
            "lots_delta": 0,
            "final_action": "hold",
        }
        rows = [
            {
                "id": "c-long-open",
                "ticker": "C",
                "side": "long",
                "action_name": "open",
                "canonical_action_family": "open_add_new_risk",
                "learning_lane": "open",
                "action_value_lane": "open",
                "memory_side_role": "target_side",
                "consumer_scope": "pm_learning",
                "action_preference": "positive_candidate_open",
                "reward_source": "real_trade",
                "evidence_scope": "exact_real_state",
                "reward_sum": 1000.0,
                "reward_mean": 1000.0,
            },
            {
                "id": "c-long-execution",
                "ticker": "C",
                "side": "long",
                "action_name": "execution",
                "canonical_action_family": "execution",
                "learning_lane": "execution",
                "action_value_lane": "execution",
                "memory_side_role": "historical_sample_side",
                "consumer_scope": "pm_learning",
                "action_preference": "positive_candidate_execution",
                "reward_source": "real_trade",
                "evidence_scope": "exact_real_state",
                "reward_sum": 1000.0,
                "reward_mean": 1000.0,
            },
            {
                "id": "c-short-hold",
                "ticker": "C",
                "side": "short",
                "action_name": "hold",
                "canonical_action_family": "hold",
                "learning_lane": "hold",
                "action_value_lane": "hold",
                "memory_side_role": "current_position_side",
                "consumer_scope": "pm_learning",
                "action_preference": "negative_hold_revalidate",
                "reward_source": "real_trade",
                "evidence_scope": "exact_real_state",
                "reward_sum": -100.0,
                "reward_mean": -100.0,
            },
        ]

        result = filter_action_values_for_contract_learning(contract, rows)

        self.assertEqual([row["id"] for row in result["rows"]], ["c-short-hold"])
        rejected_ids = {row["id"] for row in result["rejected_action_values"]}
        self.assertIn("c-long-open", rejected_ids)
        self.assertIn("c-long-execution", rejected_ids)

    def test_positive_open_action_value_canonical_preference_is_required(self):
        row = {
            "ticker": "EB",
            "side": "short",
            "action_name": "open",
            "learning_lane": "open",
            "action_value_lane": "open",
            "memory_side_role": "target_side",
            "consumer_scope": "pm_learning",
            "action_preference": "tail_loss_protect",
            "reward_source": "real_trade",
            "evidence_scope": "exact_real_state",
            "reward_sum": 500.0,
            "reward_mean": 500.0,
        }

        self.assertEqual(canonical_action_preference_for_action_value(row), "positive_candidate_open")
        validation = validate_action_value_write_consistency(row)

        self.assertFalse(validation["ok"])
        self.assertIn("positive_open_action_value_not_open_preference", validation["errors"])

    def test_hold_exit_no_change_explanation_requires_lifecycle_reason(self):
        base = {
            "current_lots": 2,
            "target_lots": 2,
            "lots_delta": 0,
            "final_action": "hold",
        }

        self.assertTrue(
            has_valid_hold_exit_no_change_explanation({
                **base,
                "reason_codes": ["holding_period_control", "position_matched"],
            })
        )
        self.assertTrue(
            has_valid_hold_exit_no_change_explanation({
                **base,
                "reason_codes": ["profitable_hold_continuation"],
            })
        )
        self.assertTrue(
            has_valid_hold_exit_no_change_explanation({
                **base,
                "reason_codes": ["position_lifecycle_trend_hold"],
            })
        )
        self.assertTrue(
            has_valid_hold_exit_no_change_explanation({
                **base,
                "reason_codes": ["hold_exit_action_value_protection"],
            })
        )
        self.assertFalse(
            has_valid_hold_exit_no_change_explanation({
                **base,
                "reason_codes": ["position_matched"],
            })
        )
        self.assertFalse(has_valid_hold_exit_no_change_explanation({**base, "reason_codes": []}))

    def test_hold_exit_no_change_explanation_accepts_actual_reduce_and_exit(self):
        reduce_contract = {
            "current_lots": 2,
            "target_lots": 1,
            "lots_delta": -1,
            "final_action": "reduce",
            "reason_codes": ["position_matched"],
        }
        exit_contract = {
            "current_lots": -2,
            "target_lots": 0,
            "lots_delta": 2,
            "final_action": "exit",
            "reason_codes": [],
        }

        self.assertTrue(has_valid_hold_exit_no_change_explanation(reduce_contract))
        self.assertTrue(has_valid_hold_exit_no_change_explanation(exit_contract))

    def test_contract_consumes_hold_exit_pm_learning_uses_pm_scope_and_lifecycle(self):
        self.assertIn("positive_candidate_hold", ACTION_PREFERENCE_VALUES)
        self.assertTrue(
            contract_consumes_hold_exit_pm_learning(
                {
                    "learning_used": {
                        "alpha_setup_action_values": [
                            {
                                "consumer_scope": "pm_learning",
                                "action_preference": "positive_candidate_exit",
                                "action_value_lane": "exit",
                            }
                        ]
                    }
                }
            )
        )
        self.assertTrue(
            contract_consumes_hold_exit_pm_learning(
                {
                    "learning_used": {
                        "alpha_setup_action_values": [
                            {
                                "consumer_scope": "pm_learning",
                                "action_preference": "negative_hold_revalidate",
                                "action_value_lane": "hold",
                            }
                        ]
                    }
                }
            )
        )
        self.assertTrue(
            contract_consumes_hold_exit_pm_learning(
                {
                    "learning_used": {
                        "alpha_setup_action_values": [
                            {
                                "consumer_scope": "pm_learning",
                                "action_preference": "positive_candidate_hold",
                                "action_value_lane": "hold",
                            }
                        ]
                    }
                }
            )
        )
        self.assertFalse(
            contract_consumes_hold_exit_pm_learning(
                {
                    "learning_used": {
                        "alpha_setup_action_values": [
                            {
                                "consumer_scope": "analyst_calibration",
                                "action_preference": "positive_candidate_exit",
                                "action_value_lane": "exit",
                            }
                        ]
                    }
                }
            )
        )
        self.assertFalse(
            contract_consumes_hold_exit_pm_learning(
                {
                    "learning_used": {
                        "alpha_setup_action_values": [
                            {
                                "consumer_scope": "pm_learning",
                                "action_preference": "positive_candidate_open",
                                "action_value_lane": "open",
                            }
                        ]
                    }
                }
            )
        )

    def test_public_lane_matcher_is_the_single_memory_lane_source(self):
        self.assertTrue(lane_matches_memory_requirement("add", "open"))
        self.assertTrue(lane_matches_memory_requirement("increase", "scale"))
        self.assertTrue(lane_matches_memory_requirement("reduce", "hold"))
        self.assertTrue(lane_matches_memory_requirement("exit", "reduce"))
        self.assertFalse(lane_matches_memory_requirement("exit", "open"))
        self.assertFalse(lane_matches_memory_requirement("conditional_monitor", "open"))

    def test_final_action_lot_transition_validation_covers_trade_lifecycle(self):
        cases = [
            ({"current_lots": 0, "target_lots": 0, "lots_delta": 0, "final_action": "wait"}, "wait"),
            ({"current_lots": 2, "target_lots": 2, "lots_delta": 0, "final_action": "hold"}, "hold"),
            ({"current_lots": 0, "target_lots": -1, "lots_delta": -1, "final_action": "open_probe"}, "open"),
            ({"current_lots": -1, "target_lots": -3, "lots_delta": -2, "final_action": "add"}, "increase"),
            ({"current_lots": 3, "target_lots": 1, "lots_delta": -2, "final_action": "reduce"}, "decrease"),
            ({"current_lots": -2, "target_lots": 0, "lots_delta": 2, "final_action": "exit"}, "exit"),
        ]
        for contract, expected_family in cases:
            with self.subTest(contract=contract):
                result = validate_final_action_lot_transition(contract)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["expected_action_family"], expected_family)

        bad = validate_final_action_lot_transition(
            {"current_lots": 0, "target_lots": -1, "lots_delta": -1, "final_action": "hold"}
        )
        self.assertFalse(bad["ok"])
        self.assertIn("final_action_contract_action_mismatch", bad["errors"])

    def test_full_market_rank_gate_required_for_all_incremental_risk(self):
        unranked_open = {
            "current_lots": 0,
            "target_lots": -1,
            "lots_delta": -1,
            "final_action": "open_probe",
        }
        unranked_reverse = {
            "current_lots": 2,
            "target_lots": -1,
            "lots_delta": -3,
            "final_action": "exit",
        }
        unranked_add = {
            "current_lots": 1,
            "target_lots": 2,
            "lots_delta": 1,
            "final_action": "scale",
        }
        reduce_contract = {
            "current_lots": 2,
            "target_lots": 1,
            "lots_delta": -1,
            "final_action": "reduce",
        }
        ranked_open = {
            **unranked_open,
            "evidence_used": {"opportunity_rank": 1, **full_market_rank_source_payload()},
            "capital_deployment": {"opportunity_rank": 1, **full_market_rank_source_payload()},
        }
        ranked_add = {
            **unranked_add,
            "evidence_used": {"opportunity_rank": 2, **full_market_rank_source_payload()},
            "capital_deployment": {"opportunity_rank": 2, **full_market_rank_source_payload()},
        }

        self.assertEqual(
            full_market_rank_gate_errors(unranked_open),
            ["new_risk_exposure_missing_full_market_rank"],
        )
        self.assertEqual(full_market_rank_gate_errors(unranked_reverse), [])
        self.assertEqual(
            full_market_rank_gate_errors(unranked_add),
            ["new_risk_exposure_missing_full_market_rank"],
        )
        self.assertEqual(full_market_rank_gate_errors(reduce_contract), [])
        self.assertTrue(contract_has_full_market_capital_rank(ranked_open))
        self.assertEqual(full_market_rank_gate_errors(ranked_open), [])
        self.assertTrue(contract_has_full_market_capital_rank(ranked_add))
        self.assertEqual(full_market_rank_gate_errors(ranked_add), [])

        ranked_native_hold = {
            "current_lots": 1,
            "target_lots": 1,
            "lots_delta": 0,
            "final_action": "hold",
            "evidence_used": {"opportunity_rank": 1, **full_market_rank_source_payload()},
            "capital_deployment": {
                "opportunity_rank": 1,
                "selected_for_capital_deployment": False,
                "capital_allocation_reason": "non_new_risk_no_capital_rank",
                **full_market_rank_source_payload(),
            },
        }
        self.assertIn(
            "non_increasing_risk_contract_has_full_market_rank",
            rank_capital_layer_contract_errors(ranked_native_hold),
        )

    def test_generic_no_change_explanation_requires_registered_reason_or_explicit_field(self):
        explained = {
            "final_action": "wait",
            "current_lots": 0,
            "target_lots": 0,
            "lots_delta": 0,
            "reason_codes": ["capital_queue_not_selected"],
        }
        with_field = {
            "final_action": "wait",
            "current_lots": 0,
            "target_lots": 0,
            "lots_delta": 0,
            "capital_deployment": {"capital_allocation_reason": "capital_queue_not_selected_after_full_market_ranking"},
        }
        unexplained = {
            "final_action": "wait",
            "current_lots": 0,
            "target_lots": 0,
            "lots_delta": 0,
            "reason_codes": ["diagnostic_only_comment"],
        }

        self.assertTrue(classify_final_action_reason_codes(explained)["rank_no_deployment_explanation"])
        self.assertTrue(classify_final_action_reason_codes(with_field)["capital_allocation_explanation"])
        self.assertFalse(classify_final_action_reason_codes(unexplained)["rank_no_deployment_explanation"])

    def test_reason_code_classification_drives_active_rejection_and_open_blockers(self):
        classification = classify_final_action_reason_codes({
            "reason_codes": ["pm_watch_for_trigger_probe_cap", "conditional_trigger_authority"],
        })
        self.assertTrue(classification["conditional_monitor_candidate_only"])

        rejected = classify_final_action_reason_codes({"reason_codes": ["market_confirmation_conflict"]})
        self.assertIn("market_confirmation_conflict", rejected["active_opportunity_rejection_reasons"])
        self.assertTrue(
            has_open_transaction_blocker({
                "final_action": "open_probe",
                "authority_type": "exploration_probe",
                "reason_codes": ["pm_watch_for_trigger_probe_cap"],
            })
        )
        self.assertFalse(
            has_open_transaction_blocker({
                "final_action": "open_probe",
                "authority_type": "exploration_probe",
                "conditional_trigger_authority": True,
                "requires_intraday_confirmation": True,
                "can_execute_without_intraday_trigger": False,
                "watch_for_trigger_block": False,
                "reason_codes": ["pm_watch_for_trigger_probe_cap", "conditional_trigger_authority"],
            })
        )


if __name__ == "__main__":
    unittest.main()
