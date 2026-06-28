import unittest
from pathlib import Path
import sys


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.decision.pm_contract_builder import build_final_action_contract
from tools.agent_tools.decision.pm_contract_self_check import check_final_action_contract
from tools.agent_tools.decision.pm_position_transition import classify_position_transition
from tools.agent_tools.decision.pm_state_transition import (
    classify_new_entry_transition,
    classify_pm_decision_state,
)


class PMStateTransitionMatrixTest(unittest.TestCase):
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
            current_lots=0,
            target_lots=1,
            position_ratio=0.01,
            margin_required=1000.0,
            account_equity=1_000_000.0,
            lots_to_trade=1,
            lots_to_trade_reason="minimum_one_lot_probe",
            recommendation_intent={
                "action": "open_long",
                "lots": 1,
                "action_type": "new_entry",
            },
            final_entry_authority={"authority_type": "probe_entry", "reason_codes": ["minimum_one_lot_probe"]},
            control_reasons=["minimum_one_lot_probe"],
            control_diagnostics={},
            opportunity_scorecard={"preferred_side": "long", "long": {"final_state": "probe_candidate", "score": 0.7}},
            market_confirmation={"confirmation_score": 0.7},
            alpha_setup_action_values=[],
            execution_contract_fields={"execution_profile": "breakout"},
        )
        self.assertEqual(contract["final_action"], "open_probe")
        self.assertTrue(check_final_action_contract(contract)["ok"])

        bad = dict(contract)
        bad["lots_delta"] = 99
        self.assertIn("lots_delta_mismatch", check_final_action_contract(bad)["errors"])


if __name__ == "__main__":
    unittest.main()
