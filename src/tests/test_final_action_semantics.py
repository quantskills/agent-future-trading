import unittest
from pathlib import Path
import sys


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.common.final_action_semantics import (
    authority_allows_entry,
    classify_analyst_evidence,
    classify_final_action_contract,
    classify_reason_codes,
    derive_accounting_expectation,
    derive_execution_requirement,
    derive_protocol_semantic_checks,
    derive_research_fact_state,
    derive_review_expectation,
    requires_intraday_result,
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


if __name__ == "__main__":
    unittest.main()
