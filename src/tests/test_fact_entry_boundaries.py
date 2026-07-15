import ast
import re
import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.common.contracts import (
    execution_contract_from_final_action_contract,
    final_contract_execution_fields,
    sanitize_execution_contract,
    validate_accountant_artifact_boundary,
    validate_execution_artifact_boundary,
    validate_final_action_contract,
    validate_pm_artifact_boundary,
    validate_auditor_artifact_boundary,
    validate_researcher_artifact_boundary,
    validate_reviewer_artifact_boundary,
)


PROJECT_ROOT = SRC_ROOT.parent


def _read(relative_path: str) -> str:
    return (SRC_ROOT / relative_path).read_text(encoding="utf-8-sig")


def _production_python_files():
    for path in SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel.startswith("tests/"):
            continue
        yield path


class FactEntryBoundaryTest(unittest.TestCase):
    def test_final_action_contract_parser_validates_lots_delta(self):
        valid_contract = {
            "ticker": "BU",
            "current_lots": 1,
            "target_lots": 3,
            "lots_delta": 2,
            "final_action": "increase_position",
        }
        self.assertEqual(validate_final_action_contract(valid_contract), [])

        invalid_contract = dict(valid_contract, lots_delta=99)
        self.assertIn(
            "final_action_contract_lots_delta_mismatch:current=1:target=3:delta=99",
            validate_final_action_contract(invalid_contract),
        )

    def test_execution_contract_parser_excludes_pm_explanation_fields(self):
        contract = {
            "ticker": "BU",
            "current_lots": 0,
            "target_lots": 2,
            "lots_delta": 2,
            "final_action": "open_long",
            "entry_trigger": {"type": "intraday_breakout"},
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "execution_profile": {"mode": "confirmed"},
            "learning_used": [{"id": "research"}],
            "opportunity_rank": 1,
            "capital_allocation_reason": "best current evidence",
            "position_sizing_result": {"target_lots": 2},
        }
        execution_contract = execution_contract_from_final_action_contract(contract)
        self.assertEqual(execution_contract["entry_trigger"], {"type": "intraday_breakout"})
        self.assertNotIn("final_action", execution_contract)
        self.assertNotIn("target_lots", execution_contract)
        self.assertNotIn("lots_delta", execution_contract)
        self.assertNotIn("learning_used", execution_contract)
        self.assertNotIn("opportunity_rank", execution_contract)
        self.assertNotIn("capital_allocation_reason", execution_contract)
        self.assertNotIn("position_sizing_result", execution_contract)

        execution_fields = final_contract_execution_fields(contract)
        self.assertEqual(execution_fields["final_action"], "open_long")
        self.assertEqual(execution_fields["target_lots"], 2)
        self.assertEqual(execution_fields["lots_delta"], 2)
        self.assertNotIn("learning_used", execution_fields)

    def test_execution_contract_sanitizer_excludes_pm_explanation_fields(self):
        dirty_execution_contract = {
            "execution_profile": "breakout",
            "entry_trigger": {"type": "intraday_breakout"},
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "target_lots": 2,
            "lots_delta": 2,
            "final_action": "open_long",
            "learning_used": [{"id": "research"}],
            "opportunity_rank": 1,
            "capital_allocation_reason": "best current evidence",
            "position_sizing_result": {"target_lots": 2},
        }

        sanitized = sanitize_execution_contract(dirty_execution_contract)
        self.assertEqual(sanitized["execution_profile"], "breakout")
        self.assertEqual(sanitized["entry_trigger"], {"type": "intraday_breakout"})
        self.assertNotIn("target_lots", sanitized)
        self.assertNotIn("lots_delta", sanitized)
        self.assertNotIn("final_action", sanitized)
        self.assertNotIn("learning_used", sanitized)
        self.assertNotIn("opportunity_rank", sanitized)
        self.assertNotIn("capital_allocation_reason", sanitized)
        self.assertNotIn("position_sizing_result", sanitized)

    def test_transaction_audit_fallback_sanitizes_execution_contract(self):
        audit_source = _read("util/futures_audit.py")
        self.assertIn("sanitize_execution_contract(execution_translation.get(\"execution_contract\") or {})", audit_source)
        self.assertNotIn("execution_contract = dict(execution_translation.get(\"execution_contract\")", audit_source)

    def test_execution_artifact_boundary_rejects_pm_explanation_fields(self):
        clean_payload = {
            "phase2_execution": {
                "pm_plan_validation": {
                    "final_contract_execution_fields": {
                        "final_action": "open_long",
                        "current_lots": 0,
                        "target_lots": 2,
                        "lots_delta": 2,
                    }
                }
            }
        }
        validate_execution_artifact_boundary(clean_payload)

        dirty_payload = {
            "phase2_execution": {
                "pm_plan_validation": {
                    "position_sizing_result": {"target_lots": 2},
                }
            }
        }
        with self.assertRaisesRegex(ValueError, "execution_artifact_forbidden_pm_fields"):
            validate_execution_artifact_boundary(dirty_payload)

    def test_stage_artifact_boundaries_reject_cross_stage_fields(self):
        validate_pm_artifact_boundary(
            {
                "final_action_contract": {"final_action": "hold"},
                "learning_used": [{"source": "decision_memory_retrieval"}],
                "opportunity_rank": 1,
            }
        )
        with self.assertRaisesRegex(ValueError, "pm_artifact_forbidden_downstream_fields"):
            validate_pm_artifact_boundary({"execution_result": {"status": "filled"}})
        with self.assertRaisesRegex(ValueError, "pm_artifact_forbidden_downstream_fields"):
            validate_pm_artifact_boundary(
                {
                    "pm_internal_draft": {
                        "opportunity_rank": 1,
                        "target_lots": -11,
                    },
                    "final_action_contract": {
                        "final_action": "open_probe",
                        "current_lots": 0,
                        "target_lots": -11,
                        "lots_delta": -11,
                    },
                }
            )
        for field in (
            "pm_internal_candidate",
            "pm_internal_candidate_contract",
            "pm_capital_deployment_decision",
        ):
            with self.assertRaisesRegex(ValueError, "pm_artifact_forbidden_downstream_fields"):
                validate_pm_artifact_boundary({field: {"not_final_trade_truth": True}})

        validate_auditor_artifact_boundary(
            {
                "producer": "auditor",
                "audit_verdict": "approve",
                "audit_reason_codes": [],
                "contract_summary": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": 1,
                    "lots_delta": 1,
                },
            }
        )
        with self.assertRaisesRegex(ValueError, "auditor_artifact_forbidden_contract_mutation"):
            validate_auditor_artifact_boundary({"new_final_action_contract": {"target_lots": 9}})

        validate_accountant_artifact_boundary(
            {
                "daily_settlement": {
                    "trading_date": "2025-03-03",
                    "daily_pnl": 0.0,
                    "commission": 0.0,
                    "current_margin": 0.0,
                    "current_balance": 1000000.0,
                }
            }
        )
        with self.assertRaisesRegex(ValueError, "accountant_artifact_forbidden_trade_or_learning_fields"):
            validate_accountant_artifact_boundary({"daily_settlement": {"learning_used": []}})

        validate_reviewer_artifact_boundary(
            {
                "phase4_validation": {"status": "completed"},
                "source_artifacts": ["phase1", "phase2", "phase3"],
                "fact_attribution": {"no_trade_reason": "not_triggered"},
            }
        )
        with self.assertRaisesRegex(ValueError, "reviewer_artifact_forbidden_research_or_mutation_fields"):
            validate_reviewer_artifact_boundary({"alpha_setup_action_value": {"reward_sum": 1.0}})

        validate_researcher_artifact_boundary(
            {
                "alpha_setup_action_value": {"reward_sum": 1.0},
                "adaptive_policy_state": {"policy_action": "calibrate"},
            }
        )
        with self.assertRaisesRegex(ValueError, "researcher_artifact_forbidden_trade_fact_mutation"):
            validate_researcher_artifact_boundary({"modified_final_action_contract": {"target_lots": 9}})

    def test_pm_artifact_boundary_allows_research_count_summaries_only(self):
        validate_pm_artifact_boundary(
            {
                "technical": {
                    "metadata": {
                        "reviewer_learning_context": {
                            "memory_trace": {
                                "selected_counts": {
                                    "alpha_setup_action_value": 0,
                                }
                            }
                        }
                    }
                },
                "fundamental": {
                    "metadata": {
                        "reviewer_learning_context": {
                            "memory_trace": {
                                "selected_counts": {
                                    "alpha_setup_action_value": 0,
                                }
                            }
                        }
                    }
                },
                "commodity_news": {
                    "metadata": {
                        "reviewer_learning_context": {
                            "memory_trace": {
                                "selected_counts": {
                                    "alpha_setup_action_value": 0,
                                }
                            }
                        }
                    }
                },
            }
        )

        validate_pm_artifact_boundary(
            {
                "final_action_contract": {
                    "position_sizing_result": {
                        "capital_allocation_reason": {
                            "memory_summary": {
                                "side_summaries": [
                                    {
                                        "source_status": {
                                            "alpha_setup_action_value": "empty",
                                            "adaptive_policy_state": "empty",
                                        },
                                        "source_errors": {
                                            "adaptive_policy_state": "db_unavailable",
                                        },
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        )

        validate_pm_artifact_boundary(
            {
                "final_action_contract": {
                    "position_sizing_result": {
                        "capital_allocation_reason": {
                            "memory_summary": {
                                "source_status": {
                                    "alpha_setup_action_value": "available",
                                    "adaptive_policy_state": [],
                                    "researcher_llm_notes": 0,
                                }
                            }
                        }
                    }
                }
            }
        )

        with self.assertRaisesRegex(ValueError, "pm_artifact_forbidden_downstream_fields"):
            validate_pm_artifact_boundary(
                {
                    "technical": {
                        "metadata": {
                            "reviewer_learning_context": {
                                "memory_trace": {
                                    "selected_counts": {
                                        "alpha_setup_action_value": {"reward": 1.0},
                                    }
                                }
                            }
                        }
                    }
                }
            )

        with self.assertRaisesRegex(ValueError, "pm_artifact_forbidden_downstream_fields"):
            validate_pm_artifact_boundary(
                {
                    "final_action_contract": {
                        "position_sizing_result": {
                            "capital_allocation_reason": {
                                "memory_summary": {
                                    "side_summaries": [
                                        {
                                            "source_status": {
                                                "alpha_setup_action_value": {"reward": 1.0},
                                            }
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            )

        with self.assertRaisesRegex(ValueError, "pm_artifact_forbidden_downstream_fields"):
            validate_pm_artifact_boundary(
                {
                    "final_action_contract": {
                        "position_sizing_result": {
                            "capital_allocation_reason": {
                                "memory_summary": {
                                    "source_status": {
                                        "adaptive_policy_state": [{"policy_action": "widen_trigger"}],
                                    }
                                }
                            }
                        }
                    }
                }
            )

        with self.assertRaisesRegex(ValueError, "pm_artifact_forbidden_downstream_fields"):
            validate_pm_artifact_boundary(
                {
                    "technical": {
                        "metadata": {
                            "alpha_setup_action_value": {"reward": 1.0},
                        }
                    }
                }
            )

    def test_pm_learning_contract_summary_must_not_copy_research_fact_objects(self):
        validate_pm_artifact_boundary(
            {
                "final_action_contract": {
                    "evidence_used": {
                        "rank_input_components": {"rank_score": 0.41},
                        "lifecycle_learning_trace": {
                            "rank_lifecycle": "open_add_new_risk",
                            "used_lanes": ["open"],
                        },
                        "learning_impact_delta": {
                            "net_rank_learning_delta": 0.12,
                        },
                    },
                    "learning_used": {
                        "learning_to_position_summary": {
                            "trace_version": "agentquant.pm_learning_contract_summary.v1",
                            "learning_source_summary": {
                                "adaptive_policy_summary": {
                                    "policy_count": 2,
                                    "policy_type_counts": {"alpha_promotion": 1, "tail_loss_sentinel": 1},
                                    "status": "summary_only_no_policy_rows",
                                },
                                "strategy_memory_summary": {
                                    "status": "summary_only_no_raw_strategy_memory",
                                    "raw_object_omitted": True,
                                },
                                "action_value_summary": {
                                    "action_value_count": 1,
                                    "action_preference_counts": {"positive_candidate_open": 1},
                                },
                            },
                        },
                        "pm_lifecycle_learning_trace": {
                            "contract_lifecycle_port": "open_add_new_risk",
                            "used_lanes": ["open"],
                        },
                        "pm_lifecycle_learning_impact_delta": {
                            "open_add_rank_score_delta": 0.12,
                        },
                    },
                }
            }
        )

        with self.assertRaisesRegex(ValueError, "pm_artifact_forbidden_downstream_fields"):
            validate_pm_artifact_boundary(
                {
                    "final_action_contract": {
                        "learning_used": {
                            "learning_to_position_trace": {
                                "adaptive_policy_state": {"policy_action": "calibrate"},
                                "strategy_memory": {"memory_state": "protected"},
                            }
                        }
                    }
                }
            )

        with self.assertRaisesRegex(ValueError, "pm_artifact_forbidden_downstream_fields"):
            validate_pm_artifact_boundary(
                {
                    "final_action_contract": {
                        "learning_used": {
                            "learning_to_position_summary": {
                                "loss_template_research_trace": {
                                    "adaptive_policy_scope": {
                                        "policies": [{"policy_action": "calibrate"}],
                                    }
                                }
                            }
                        }
                    }
                }
            )

    def test_artifact_boundaries_distinguish_summary_values_from_fact_objects(self):
        validate_execution_artifact_boundary(
            {
                "phase2_execution": {
                    "execution_result": {
                        "research_source_status": {
                            "alpha_setup_action_value": "empty",
                            "adaptive_policy_state": 0,
                            "researcher_llm_notes": [],
                        }
                    }
                }
            }
        )
        with self.assertRaisesRegex(ValueError, "execution_artifact_forbidden_pm_fields"):
            validate_execution_artifact_boundary(
                {
                    "phase2_execution": {
                        "execution_result": {
                            "alpha_setup_action_value": {"reward": 1.0},
                        }
                    }
                }
            )
        with self.assertRaisesRegex(ValueError, "execution_artifact_forbidden_pm_fields"):
            validate_execution_artifact_boundary(
                {
                    "phase2_execution": {
                        "execution_result": {
                            "capital_allocation_reason": "ranked_first",
                        }
                    }
                }
            )

        validate_accountant_artifact_boundary(
            {
                "daily_settlement": {
                    "trading_date": "2025-03-03",
                    "daily_pnl": 0.0,
                    "alpha_setup_action_value": "empty",
                    "adaptive_policy_state": [],
                }
            }
        )
        with self.assertRaisesRegex(ValueError, "accountant_artifact_forbidden_trade_or_learning_fields"):
            validate_accountant_artifact_boundary({"daily_settlement": {"alpha_setup_action_value": {"reward": 1.0}}})

        validate_reviewer_artifact_boundary(
            {
                "phase4_validation": {
                    "status": "completed",
                    "source_status": {
                        "alpha_setup_action_value": "empty",
                        "adaptive_policy_state": 0,
                    },
                }
            }
        )
        with self.assertRaisesRegex(ValueError, "reviewer_artifact_forbidden_research_or_mutation_fields"):
            validate_reviewer_artifact_boundary({"phase4_validation": {"adaptive_policy_state": [{"x": 1}]}})

        validate_researcher_artifact_boundary({"alpha_setup_action_value": {"reward_sum": 1.0}})
        with self.assertRaisesRegex(ValueError, "researcher_artifact_forbidden_trade_fact_mutation"):
            validate_researcher_artifact_boundary({"modified_daily_settlement": {"daily_pnl": 1.0}})

    def test_trader_and_audit_read_pm_contract_through_common_parser(self):
        trader_source = _read("agents/execution_team/trader.py")
        self.assertIn("from tools.common.contracts import", trader_source)
        self.assertIn("return execution_contract_from_snapshot(snapshot)", trader_source)
        self.assertIn("return final_action_contract_from_snapshot(snapshot)", trader_source)
        self.assertIn("return final_contract_execution_fields_from_snapshot(snapshot)", trader_source)
        self.assertNotIn('snapshot.get("final_action_contract")', trader_source)
        self.assertNotIn('snapshot["final_action_contract"]', trader_source)

        audit_source = _read("util/futures_audit.py")
        self.assertIn("from tools.common.contracts import", audit_source)
        self.assertIn("final_action_contract_from_snapshot(snapshot)", audit_source)
        self.assertIn("execution_contract_from_snapshot(snapshot)", audit_source)
        self.assertNotIn('snapshot.get("final_action_contract")', audit_source)
        self.assertNotIn('snapshot["final_action_contract"]', audit_source)

        reviewer_source = _read("tools/agent_tools/research/reviewer_phase4_review.py")
        self.assertIn("from tools.common.contracts import final_action_contract_from_snapshot", reviewer_source)
        self.assertIn("contract = final_action_contract_from_snapshot(snapshot)", reviewer_source)
        self.assertNotIn('snapshot.get("final_action_contract")', reviewer_source)
        self.assertNotIn('snapshot["final_action_contract"]', reviewer_source)

    def test_independent_auditor_and_pm_risk_gate_are_separate(self):
        auditor_source = _read("agents/decision_team/auditor.py")
        pm_risk_gate_source = _read("tools/agent_tools/decision/pm_risk_gate.py")
        portfolio_manager_source = _read("agents/decision_team/portfolio_manager.py")
        trader_source = _read("agents/execution_team/trader.py")

        self.assertIn("class Auditor", auditor_source)
        self.assertIn("audit_verdict", auditor_source)
        self.assertIn("validate_auditor_artifact_boundary", auditor_source)
        self.assertNotIn("class PMRiskGate", auditor_source)
        self.assertNotIn("PMRiskGateInput", auditor_source)
        retired_risk_gate_name = "trade" + "_auditor"
        retired_planner_name = "decision" + "_planner"
        self.assertNotIn(retired_risk_gate_name, auditor_source)
        self.assertNotIn(retired_planner_name, auditor_source)
        self.assertIn("class PMRiskGate", pm_risk_gate_source)
        self.assertIn("internal non-LLM risk gate", pm_risk_gate_source)
        self.assertNotIn("agent_name=\"auditor\"", pm_risk_gate_source)
        self.assertIn("from tools.agent_tools.decision.pm_risk_gate import PMRiskGate, PMRiskGateInput", portfolio_manager_source)
        self.assertNotIn("from agents.decision_team.auditor import PMRiskGate", portfolio_manager_source)
        self.assertIn("_auditor_verdict_allows_strategy_execution", trader_source)
        self.assertIn("auditor_verdict_not_approved", trader_source)
        self.assertIn("from agents.decision_team.auditor import audit_verdict_allows_trader", trader_source)

    def test_current_source_does_not_use_retired_pm_risk_gate_or_planner_names(self):
        retired_risk_gate_name = "trade" + "_auditor"
        retired_risk_gate_class = "Trade" + "Auditor"
        retired_planner_name = "decision" + "_planner"
        retired_planner_field = "planner" + "_decision"
        retired_pm_llm_mirror = "portfolio" + "_manager" + "_llm"
        for path in _production_python_files():
            text = path.read_text(encoding="utf-8-sig")
            rel = path.relative_to(SRC_ROOT).as_posix()
            self.assertNotIn(retired_risk_gate_name, text, rel)
            self.assertNotIn(retired_risk_gate_class, text, rel)
            self.assertNotIn(retired_planner_name, text, rel)
            self.assertNotIn(retired_planner_field, text, rel)
            self.assertNotIn(retired_pm_llm_mirror, text, rel)

    def test_execution_audit_payload_preserves_independent_auditor_summary(self):
        from util.futures_audit import build_audit_payload

        payload = build_audit_payload(
            {
                "final_action_contract": {
                    "current_lots": 0,
                    "target_lots": 0,
                    "lots_delta": 0,
                    "final_action": "wait",
                },
                "auditor": {
                    "producer": "auditor",
                    "audit_status": "approved",
                    "audit_verdict": "approve",
                    "audit_reason_codes": [],
                    "audited_at": "2025-03-03T00:00:00+00:00",
                },
                "execution_result": {"status": "skipped", "no_trade_reason": "position_matched"},
            }
        )
        self.assertEqual(payload["independent_auditor"]["producer"], "auditor")
        self.assertEqual(payload["independent_auditor"]["audit_verdict"], "approve")
        self.assertNotIn("final_action_contract", payload["independent_auditor"])

    def test_auditor_does_not_modify_pm_contract(self):
        from agents.decision_team.auditor import audit_futures_recommendation
        from tools.common.final_action_semantics import derive_memory_requirements

        contract = {
            "current_lots": 0,
            "target_lots": 1,
            "lots_delta": 1,
            "final_action": "open_probe",
            "authority_type": "real_budget_entry",
            "contract_code": "bu2506",
            "open_action_evidence": True,
            "strong_current_evidence": True,
            "invalidation_condition": {"type": "stop"},
            "target_margin_ratio_estimate": 0.01,
        }
        contract["learning_used"] = {
            "memory_requirements": derive_memory_requirements(contract),
            "memory_retrieval": {"requirement_details": []},
            "alpha_setup_action_values": [],
        }
        recommendation = {
            "id": "rec-1",
            "config_id": "cfg",
            "source_type": "strategy",
            "underlying_code": "BU",
            "trading_date": "2025-03-03",
            "effective_trade_date": "2025-03-03",
            "signal_snapshot": {"final_action_contract": dict(contract)},
        }
        output = audit_futures_recommendation(
            recommendation=recommendation,
            hard_risk_config={"max_total_margin_ratio": 0.20},
            account_state={
                "account_equity": 1_000_000.0,
                "margin_used": 0.0,
                "margin_ratio": 0.0,
                "risk_status": "NORMAL",
            },
            position_state={"current_lots": 0, "contract_code": None},
            contract_state={
                "contract_code": "bu2506",
                "underlying_code": "BU",
                "as_of_date": "2025-03-02",
                "source": "test_contract_fact",
            },
            data_quality={"status": "clean", "flags": [], "source": "signal_collection_contract"},
        )
        self.assertEqual(output.audit_verdict, "approve")
        self.assertEqual(recommendation["signal_snapshot"]["final_action_contract"], contract)
        self.assertEqual(output.audit_payload["producer"], "auditor")
        self.assertTrue(output.audit_payload["boundary"]["auditor_does_not_modify_final_action_contract"])
        self.assertTrue(output.audit_payload["boundary"]["research_memory_not_consumed"])
        self.assertEqual(output.audit_payload["semantic_state"]["lifecycle_state"], "open")
        self.assertFalse(output.audit_payload["semantic_state"]["requires_intraday_result"])
        self.assertNotIn("pm_memory_consumption_audit", output.audit_payload)

    def test_auditor_does_not_repeat_pm_memory_self_check(self):
        from agents.decision_team.auditor import audit_futures_recommendation
        from tools.common.final_action_semantics import derive_memory_requirements

        contract = {
            "current_lots": 2,
            "target_lots": 0,
            "lots_delta": -2,
            "final_action": "exit",
            "authority_type": "exit",
            "contract_code": "rb2505",
            "target_margin_ratio_estimate": 0.0,
        }
        contract["learning_used"] = {
            "memory_requirements": derive_memory_requirements(contract),
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
        }
        recommendation = {
            "id": "rec-exit",
            "config_id": "cfg",
            "source_type": "strategy",
            "underlying_code": "RB",
            "trading_date": "2025-03-04",
            "effective_trade_date": "2025-03-04",
            "signal_snapshot": {"final_action_contract": dict(contract)},
        }

        output = audit_futures_recommendation(
            recommendation=recommendation,
            hard_risk_config={"max_total_margin_ratio": 0.20},
            account_state={
                "account_equity": 1_000_000.0,
                "margin_used": 20_000.0,
                "margin_ratio": 0.02,
                "risk_status": "NORMAL",
            },
            position_state={"current_lots": 2, "contract_code": "rb2505"},
            contract_state={},
            data_quality={"status": "clean", "flags": [], "source": "signal_collection_contract"},
        )

        self.assertEqual(output.audit_verdict, "approve")
        self.assertNotIn(
            "pm_required_memory_not_landed_in_alpha_setup_action_values",
            output.hard_risk_reasons,
        )
        self.assertNotIn("pm_memory_consumption_audit", output.audit_payload)

    def test_execution_payload_writers_enforce_artifact_boundary(self):
        futures_execution_source = _read("tools/agent_tools/execution/trader_futures_execution.py")
        self.assertIn("validate_execution_artifact_boundary(payload)", futures_execution_source)
        self.assertIn("validate_execution_artifact_boundary(transaction.audit_payload)", futures_execution_source)

        sqlite_source = _read("database/sqlite_helper.py")
        self.assertIn("validate_pm_artifact_boundary(recommendation_dict.get(\"signal_snapshot\") or {})", sqlite_source)
        self.assertIn("validate_execution_artifact_boundary(transaction_dict.get(\"audit_payload\") or {})", sqlite_source)
        self.assertIn("validate_accountant_artifact_boundary(settlement_payload)", sqlite_source)

        phase4_source = _read("tools/agent_tools/research/reviewer_phase4_review.py")
        self.assertIn("validate_reviewer_artifact_boundary(summary_payload)", phase4_source)

        research_writer_source = _read("tools/agent_tools/research/research_memory_writers.py")
        self.assertIn("validate_researcher_artifact_boundary(value)", research_writer_source)

    def test_phase4_reviewer_does_not_define_research_policy_payload_builders(self):
        phase4_source = _read("tools/agent_tools/research/reviewer_phase4_review.py")
        self.assertNotIn("def _policy_contract_payload(", phase4_source)
        self.assertNotIn("def _loss_template_policy_payload(", phase4_source)
        self.assertNotIn("def _contextual_rule_policy_payload(", phase4_source)
        self.assertNotIn("def _causal_candidate_scope(", phase4_source)
        self.assertNotIn("def _learned_vs_unlearned_trade_performance(", phase4_source)
        self.assertNotIn("def _learned_effect_underperformance_groups(", phase4_source)
        self.assertNotIn("def _causal_rule_validation_summary(", phase4_source)
        self.assertNotIn("def _neutral_counterfactual_tracking_summary(", phase4_source)

        research_writer_source = _read("tools/agent_tools/research/research_memory_writers.py")
        self.assertIn("def _policy_contract_payload(", research_writer_source)
        self.assertIn("def _loss_template_policy_payload(", research_writer_source)
        self.assertIn("def _contextual_rule_policy_payload(", research_writer_source)
        self.assertIn("_causal_candidate_scope = _research_snapshots.causal_candidate_scope", research_writer_source)
        self.assertIn(
            "_learned_vs_unlearned_trade_performance = _research_snapshots.learned_vs_unlearned_trade_performance",
            research_writer_source,
        )

        research_snapshot_source = _read("tools/agent_tools/research/research_snapshot_reports.py")
        self.assertIn("def causal_candidate_scope(", research_snapshot_source)
        self.assertIn("def learned_vs_unlearned_trade_performance(", research_snapshot_source)
        self.assertIn("def learned_effect_underperformance_groups(", research_snapshot_source)
        self.assertIn("def causal_rule_validation_summary(", research_snapshot_source)
        self.assertIn("def neutral_counterfactual_tracking_summary(", research_snapshot_source)

    def test_analyst_learning_context_uses_calibration_items_not_trade_action_values(self):
        learning_context_source = _read("tools/agent_tools/analysis/analyst_learning_context.py")
        analyst_calibration_source = _read("tools/agent_tools/analysis/analyst_learning_calibration.py")
        self.assertIn("\"analyst_calibration_items\": []", learning_context_source)
        self.assertIn("context.get(\"analyst_calibration_items\")", analyst_calibration_source)
        self.assertNotIn("\"alpha_setup_action_values\": []", learning_context_source)
        self.assertNotIn("context.get(\"alpha_setup_action_values\")", analyst_calibration_source)

    def test_non_pm_agents_do_not_read_pm_action_value_memory(self):
        trader_source = _read("agents/execution_team/trader.py")
        trader_engine_source = _read("tools/agent_tools/execution/trader_futures_execution.py")
        accountant_entry_source = _read("agents/execution_team/accountant.py")
        accountant_engine_source = _read("tools/agent_tools/execution/accountant_futures_settlement.py")
        signal_collector_source = _read("agents/decision_team/signal_collector.py")
        signal_collection_source = _read("tools/common/signal_evidence_collection.py")

        for label, source in {
            "trader": trader_source,
            "trader_engine": trader_engine_source,
            "accountant": accountant_entry_source,
            "accountant_engine": accountant_engine_source,
            "signal_collector": signal_collector_source,
            "signal_collection": signal_collection_source,
        }.items():
            self.assertNotIn("get_alpha_setup_action_values", source, label)
            self.assertNotIn("get_similar_alpha_setup_action_values", source, label)
            self.assertNotIn("retrieve_pm_memory", source, label)
            self.assertNotIn("pm_decision_memory_retrieval", source, label)

        self.assertIn("\"no_full_final_action_contract_mirror\": True", trader_source)
        self.assertIn("\"learning_source\": \"final_action_contract\"", trader_source)
        self.assertIn("get_futures_transactions_by_date", accountant_engine_source)
        self.assertIn("save_daily_settlement", accountant_engine_source)
        self.assertIn("collector_decision_boundary", signal_collection_source)
        self.assertIn("no_trade_authority", signal_collection_source)

    def test_research_writer_json_exit_rejects_trade_fact_mutation(self):
        from tools.agent_tools.research import research_memory_writers

        with self.assertRaisesRegex(ValueError, "researcher_artifact_forbidden_trade_fact_mutation"):
            research_memory_writers._json_dumps({"modified_daily_settlement": {"daily_pnl": 999}})

    def test_settlement_fact_is_written_only_by_settlement_entry(self):
        unauthorized = []
        allowed_fragments = {
            "database/interface.py",
            "database/sqlite_helper.py",
            "database/sqlite_setup.py",
            "tools/agent_tools/execution/accountant_futures_settlement.py",
            "run/settlement.py",
        }
        for path in _production_python_files():
            rel = path.relative_to(SRC_ROOT).as_posix()
            text = path.read_text(encoding="utf-8-sig")
            if "save_daily_settlement(" in text and rel not in allowed_fragments:
                unauthorized.append(rel)
            if re.search(r"\.execute\(\s*(?:f)?[\"']{1,3}\s*(?:INSERT INTO|UPDATE)\s+daily_settlement\b", text, re.I):
                if rel not in {"database/sqlite_helper.py", "database/sqlite_setup.py"}:
                    unauthorized.append(rel)
        self.assertEqual(unauthorized, [])

    def test_research_learning_facts_are_written_through_research_memory_writers(self):
        research_learning_source = _read("tools/agent_tools/research/research_learning.py")
        researcher_entry_source = _read("run/research/researcher_learning.py")
        alpha_setup_source = _read("tools/common/alpha_setup.py")

        forbidden_direct_writes = [
            "INSERT INTO causal_review_candidate",
            "INSERT INTO exploratory_hypothesis",
            "INSERT INTO researcher_llm_notes",
            "INSERT INTO alpha_setup_sample",
            "INSERT INTO alpha_setup_profile",
            "INSERT INTO alpha_setup_action_value",
            "INSERT INTO learning_event_log",
        ]
        for forbidden in forbidden_direct_writes:
            self.assertNotIn(forbidden, research_learning_source)
            self.assertNotIn(forbidden, researcher_entry_source)
            self.assertNotIn(forbidden, alpha_setup_source)

        self.assertIn("research_memory_writers.insert_causal_review_candidate", research_learning_source)
        self.assertIn("research_memory_writers.insert_exploratory_hypothesis", research_learning_source)
        self.assertIn("research_memory_writers.insert_researcher_learning_completion_event", researcher_entry_source)
        self.assertIn("research_memory_writers.upsert_alpha_setup_sample", alpha_setup_source)
        self.assertIn("research_memory_writers.upsert_alpha_setup_profile", alpha_setup_source)
        self.assertIn("research_memory_writers.upsert_alpha_setup_action_value", alpha_setup_source)

    def test_core_fact_table_direct_writes_stay_in_authorized_modules(self):
        write_pattern = re.compile(
            r"\.execute\(\s*(?:f)?[\"']{1,3}\s*(?:INSERT INTO|UPDATE)\s+"
            r"(futures_recommendation|futures_transactions|daily_settlement|ticker_daily_pnl|"
            r"alpha_setup_sample|alpha_setup_profile|alpha_setup_action_value|adaptive_policy_state|"
            r"researcher_llm_notes|learning_event_log|causal_review_candidate|exploratory_hypothesis)\b",
            re.I,
        )
        allowed = {
            "database/sqlite_helper.py",
            "database/sqlite_setup.py",
            "tools/agent_tools/research/research_memory_writers.py",
        }
        unauthorized = []
        for path in _production_python_files():
            rel = path.relative_to(SRC_ROOT).as_posix()
            text = path.read_text(encoding="utf-8-sig")
            if write_pattern.search(text) and rel not in allowed:
                unauthorized.append(rel)
        self.assertEqual(sorted(unauthorized), [])

    def test_control_audits_do_not_write_business_tables(self):
        write_pattern = re.compile(r"\.execute\(\s*(?:f)?[\"']{1,3}\s*(?:INSERT INTO|UPDATE|DELETE FROM)\b", re.I)
        isolated_temp_db_writer = "tools/agent_tools/control/pg_full_chain_dry_run.py"
        offenders = []
        for path in (SRC_ROOT / "tools" / "agent_tools" / "control").rglob("*.py"):
            rel = path.relative_to(SRC_ROOT).as_posix()
            text = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(text, filename=str(path))
            if rel == isolated_temp_db_writer:
                entry = next(
                    (
                        node
                        for node in tree.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "run_no_llm_full_chain_dry_run"
                    ),
                    None,
                )
                self.assertIsNotNone(entry)
                self.assertIn("db_path", {arg.arg for arg in entry.args.kwonlyargs})
                self.assertNotIn("src/assets/agentquant.db", text.replace("\\", "/"))
                continue
            if write_pattern.search(text):
                offenders.append(rel)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    attr = func.attr if isinstance(func, ast.Attribute) else ""
                    if attr.startswith("save_") or attr.startswith("update_"):
                        offenders.append(rel)
                        break
        self.assertEqual(sorted(set(offenders)), [])


if __name__ == "__main__":
    unittest.main()
