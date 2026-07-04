import unittest
from pathlib import Path
import sys


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.common.final_action_semantics import (
    ACTION_PREFERENCE_VALUES,
    action_value_matches_contract_memory_requirement,
    audit_pm_memory_consumption,
    authority_allows_entry,
    canonicalize_final_action_contract_for_persistence,
    canonical_action_preference_for_action_value,
    classify_analyst_evidence,
    classify_final_action_contract,
    classify_final_action_reason_codes,
    classify_reason_codes,
    contract_consumes_hold_exit_pm_learning,
    contract_has_full_market_capital_rank,
    derive_memory_requirements,
    derive_accounting_expectation,
    derive_execution_requirement,
    derive_protocol_semantic_checks,
    derive_research_fact_state,
    derive_review_expectation,
    has_active_opportunity_rejection,
    has_open_transaction_blocker,
    has_valid_generic_no_change_explanation,
    has_valid_hold_exit_no_change_explanation,
    filter_action_values_for_contract_learning,
    full_market_rank_gate_errors,
    full_market_rank_source_payload,
    lane_matches_memory_requirement,
    rank_capital_layer_contract_complete,
    rank_capital_layer_contract_errors,
    requires_intraday_result,
    validate_action_value_write_consistency,
    validate_final_action_lot_transition,
    validate_signal_collection,
)


class FinalActionSemanticsTest(unittest.TestCase):
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
        requirement = derive_execution_requirement(contract)

        self.assertEqual(semantics["lifecycle_state"], "conditional_monitor")
        self.assertEqual(semantics["execution_permission"], "monitor_intraday")
        self.assertTrue(semantics["requires_intraday_result"])
        self.assertTrue(requirement["can_monitor_intraday"])
        self.assertFalse(requirement["blocked"])
        self.assertTrue(authority_allows_entry(contract))
        self.assertTrue(requires_intraday_result(contract))

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
                {("conditional_monitor", "short", "trigger_side")},
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

    def test_analyst_and_signal_collector_have_no_trade_authority_fields(self):
        analyst = classify_analyst_evidence({
            "opportunity_state": "tradeable_candidate",
            "trigger_valid": True,
            "current_trigger_confirmed": True,
            "invalidation_present": True,
            "final_action_contract": {},
            "conditional_trigger_authority": True,
        })
        collector = validate_signal_collection({
            "collector_decision_boundary": "no_trade_authority",
            "no_trade_authority": True,
            "target_lots": 1,
        })

        self.assertIn("final_action_contract", analyst["forbidden_trade_authority_fields"])
        self.assertIn("conditional_trigger_authority", analyst["forbidden_trade_authority_fields"])
        self.assertIn("target_lots", collector["forbidden_trade_authority_fields"])

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
            "evidence_used": {"opportunity_rank": 1},
            "capital_deployment": {
                "selected_for_capital_deployment": True,
                "opportunity_rank": 1,
                "capital_allocation_reason": "selected_by_full_market_pm_capital_queue",
            },
        }
        complete = {
            "evidence_used": {
                "opportunity_rank": 1,
                "rank_capital_role": "best_exploration_probe_candidate",
                "capital_layer": "exploration_probe",
                "capital_ratio_source": "probe_margin_ratio_0.008",
                "rank_reason": "best_watch_for_trigger_by_evidence_trigger_learning_and_risk",
                **full_market_rank_source_payload(),
            },
            "capital_deployment": {
                "selected_for_capital_deployment": True,
                "opportunity_rank": 1,
                "capital_allocation_reason": "selected_by_full_market_pm_capital_queue",
                "rank_capital_role": "best_exploration_probe_candidate",
                "capital_layer": "exploration_probe",
                "capital_ratio_source": "probe_margin_ratio_0.008",
                "rank_reason": "best_watch_for_trigger_by_evidence_trigger_learning_and_risk",
                **full_market_rank_source_payload(),
            },
        }

        self.assertIn(
            "capital_deployment.capital_layer_missing",
            rank_capital_layer_contract_errors(incomplete),
        )
        self.assertFalse(rank_capital_layer_contract_complete(incomplete))
        self.assertEqual(rank_capital_layer_contract_errors(complete), [])
        self.assertTrue(rank_capital_layer_contract_complete(complete))

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
                "opportunity_rank": 1,
                "rank_capital_role": "best_exploration_probe_candidate",
                "capital_layer": "exploration_probe",
                "capital_ratio_source": "probe_margin_ratio_0.008",
                "rank_reason": "best_watch_for_trigger_by_evidence_trigger_learning_and_risk",
                **full_market_rank_source_payload(),
            },
            "capital_deployment": {
                "selected_for_capital_deployment": True,
                "capital_allocation_reason": "selected_by_full_market_pm_capital_queue",
                "opportunity_rank": 1,
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

    def test_pm_memory_consumption_audit_checks_declared_and_landed_memory(self):
        contract = {
            "current_lots": 2,
            "target_lots": 0,
            "lots_delta": -2,
            "final_action": "exit",
        }
        requirements = derive_memory_requirements(contract)
        landed_row = {
            "side": "long",
            "learning_lane": "exit",
            "action_value_lane": "exit",
            "memory_side_role": "current_position_side",
            "action_preference": "positive_candidate_exit",
        }
        audited_contract = {
            **contract,
            "learning_used": {
                "memory_requirements": requirements,
                "memory_retrieval": {
                    "requirement_details": [
                        {
                            "side": "long",
                            "lane": "exit",
                            "memory_side_role": "current_position_side",
                            "row_count": 1,
                        }
                    ]
                },
                "alpha_setup_action_values": [landed_row],
            },
        }

        audit = audit_pm_memory_consumption(audited_contract)

        self.assertTrue(audit["ok"], audit)
        self.assertFalse(audit["auditor_reads_research_db"])
        self.assertFalse(audit["trader_reads_pm_action_value"])
        self.assertFalse(audit["accountant_reads_memory"])

    def test_pm_memory_consumption_audit_flags_unlanded_available_memory(self):
        contract = {
            "current_lots": 2,
            "target_lots": 0,
            "lots_delta": -2,
            "final_action": "exit",
        }
        requirements = derive_memory_requirements(contract)
        audited_contract = {
            **contract,
            "learning_used": {
                "memory_requirements": requirements,
                "memory_retrieval": {
                    "requirement_details": [
                        {
                            "side": "long",
                            "lane": "exit",
                            "memory_side_role": "current_position_side",
                            "row_count": 1,
                        }
                    ]
                },
                "alpha_setup_action_values": [],
            },
        }

        audit = audit_pm_memory_consumption(audited_contract)

        self.assertFalse(audit["ok"])
        self.assertIn("pm_required_memory_not_landed_in_alpha_setup_action_values", audit["errors"])

    def test_open_learning_does_not_cover_position_lifecycle_memory(self):
        contract = {
            "current_lots": 2,
            "target_lots": 0,
            "lots_delta": -2,
            "final_action": "exit",
        }
        requirements = derive_memory_requirements(contract)
        audited_contract = {
            **contract,
            "learning_used": {
                "memory_requirements": requirements,
                "memory_retrieval": {
                    "requirement_details": [
                        {
                            "side": "long",
                            "lane": "exit",
                            "memory_side_role": "current_position_side",
                            "row_count": 1,
                        }
                    ]
                },
                "alpha_setup_action_values": [
                    {
                        "side": "long",
                        "learning_lane": "open",
                        "action_value_lane": "open",
                        "memory_side_role": "current_position_side",
                        "action_preference": "positive_candidate_open",
                    }
                ],
            },
        }

        audit = audit_pm_memory_consumption(audited_contract)

        self.assertFalse(audit["ok"])
        self.assertIn("pm_required_memory_not_landed_in_alpha_setup_action_values", audit["errors"])

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
        self.assertTrue(action_value_matches_contract_memory_requirement(contract, rows[2]))
        self.assertFalse(action_value_matches_contract_memory_requirement(contract, rows[0]))
        self.assertFalse(action_value_matches_contract_memory_requirement(contract, rows[1]))

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

    def test_increase_can_use_open_and_current_hold_memory(self):
        contract = {
            "current_lots": 1,
            "target_lots": 3,
            "lots_delta": 2,
            "final_action": "add",
        }
        requirements = derive_memory_requirements(contract)
        audited_contract = {
            **contract,
            "learning_used": {
                "memory_requirements": requirements,
                "memory_retrieval": {
                    "requirement_details": [
                        {
                            "side": "long",
                            "lane": "add",
                            "memory_side_role": "target_side",
                            "row_count": 1,
                        },
                        {
                            "side": "long",
                            "lane": "open",
                            "memory_side_role": "target_side",
                            "row_count": 1,
                        },
                        {
                            "side": "long",
                            "lane": "hold",
                            "memory_side_role": "current_position_side",
                            "row_count": 1,
                        },
                    ]
                },
                "alpha_setup_action_values": [
                    {
                        "side": "long",
                        "learning_lane": "open",
                        "action_value_lane": "open",
                        "memory_side_role": "target_side",
                        "action_preference": "positive_candidate_open",
                    },
                    {
                        "side": "long",
                        "learning_lane": "hold",
                        "action_value_lane": "hold",
                        "memory_side_role": "current_position_side",
                        "action_preference": "positive_candidate_hold",
                    },
                ],
            },
        }

        audit = audit_pm_memory_consumption(audited_contract)

        self.assertTrue(audit["ok"], audit)

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

    def test_full_market_rank_gate_required_only_for_new_risk(self):
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

        self.assertEqual(
            full_market_rank_gate_errors(unranked_open),
            ["new_risk_exposure_missing_full_market_rank"],
        )
        self.assertEqual(
            full_market_rank_gate_errors(unranked_reverse),
            ["new_risk_exposure_missing_full_market_rank"],
        )
        self.assertEqual(full_market_rank_gate_errors(reduce_contract), [])
        self.assertTrue(contract_has_full_market_capital_rank(ranked_open))
        self.assertEqual(full_market_rank_gate_errors(ranked_open), [])

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

        self.assertTrue(has_valid_generic_no_change_explanation(explained))
        self.assertTrue(has_valid_generic_no_change_explanation(with_field))
        self.assertFalse(has_valid_generic_no_change_explanation(unexplained))

    def test_reason_code_classification_drives_active_rejection_and_open_blockers(self):
        classification = classify_final_action_reason_codes({
            "reason_codes": ["pm_watch_for_trigger_probe_cap", "conditional_trigger_authority"],
        })
        self.assertTrue(classification["conditional_monitor_candidate_only"])

        self.assertFalse(
            has_active_opportunity_rejection(
                {"decision": {"reason": "pm_watch_for_trigger_probe_cap", "authority_type": "watchlist_only"}},
                {"reason_codes": ["pm_watch_for_trigger_probe_cap"]},
            )
        )
        self.assertTrue(
            has_active_opportunity_rejection(
                {"decision": {"reason": "market_confirmation_conflict", "authority_type": "watchlist_only"}},
                {"reason_codes": ["market_confirmation_conflict"]},
            )
        )
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
