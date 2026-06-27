import unittest
from pathlib import Path
import sys


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.decision_team.portfolio_manager import (
    _build_final_action_contract,
    _conditional_monitor_probe_seed_plan,
    _final_contract_authority,
)


class PMWatchForTriggerReleaseTest(unittest.TestCase):
    def _conditional_diagnostics(self) -> dict:
        return {
            "conditional_monitor_probe_seed": {
                "side": "short",
                "ratio": -0.008,
                "scorecard": {
                    "final_state": "watch_for_trigger",
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "invalidation_present": True,
                    "entry_trigger": "wait for post-open break below support",
                },
                "requires_intraday_confirmation": True,
            },
            "alpha_setup_ev_fusion": {
                "scorecard_state": "watch_for_trigger",
                "has_tradeable_support": False,
                "has_monitorable_setup": True,
                "setup_quality_ok": True,
                "has_invalidation_or_stop": True,
                "technical_supports_side": True,
                "technical_entry_timing_supports_side": False,
                "technical_opposes_side": False,
                "strong_realtime_evidence": False,
                "strong_market_confirmation": False,
                "qualified_positive_expectancy": False,
                "positive_action_value": False,
                "negative_action_value": False,
                "repeat_loss_without_new_evidence": False,
                "current_confirmation_score": 0.45,
                "independent_support_count": 1,
            },
        }

    def test_watch_for_trigger_candidate_becomes_intraday_conditional_contract(self):
        reasons = [
            "alpha_setup_ev_fusion",
            "pm_watch_for_trigger_probe_cap",
            "horizon_consistency_probe_cap",
            "market_confirmation_conflict",
        ]
        diagnostics = self._conditional_diagnostics()

        plan = _conditional_monitor_probe_seed_plan(
            ticker="BU",
            current_lots=0,
            target_lots=0,
            target_ratio=-0.008,
            current_ticker_exposure=0.0,
            current_net_exposure=0.0,
            account_equity=5_000_000.0,
            current_price=3500.0,
            multiplier=10.0,
            margin_rate=0.10,
            margin_available=1_000_000.0,
            max_position_ratio=0.12,
            max_net_exposure=0.40,
            morning_price_context={},
            control_reasons=reasons,
            control_diagnostics=diagnostics,
            full_config={},
        )
        self.assertTrue(plan["allowed"], plan)

        target_lots = int(plan["target_lots"])
        reasons_with_authority = [*reasons, "conditional_trigger_authority"]
        allowed, authority = _final_contract_authority(
            control_reasons=reasons_with_authority,
            control_diagnostics=diagnostics,
        )
        contract = _build_final_action_contract(
            ticker="BU",
            current_lots=0,
            target_lots=target_lots,
            position_ratio=float(plan["signed_one_lot_ratio"]),
            margin_required=float(plan["margin_required"]),
            account_equity=5_000_000.0,
            lots_to_trade=abs(target_lots),
            lots_to_trade_reason="conditional_trigger_authority",
            recommendation_intent={
                "action": "open_short",
                "lots": abs(target_lots),
                "action_type": "open",
            },
            final_entry_authority=authority,
            control_reasons=reasons_with_authority,
            control_diagnostics=diagnostics,
            opportunity_scorecard={
                "preferred_side": "short",
                "short": diagnostics["conditional_monitor_probe_seed"]["scorecard"],
            },
            market_confirmation={"confirmation_score": 0.45, "conflicts": ["market_confirmation_conflict"]},
            alpha_setup_action_values=[],
            execution_contract_fields={
                "execution_profile": "breakout",
                "entry_trigger": "wait for post-open break below support",
                "invalidation": "above resistance",
                "requires_intraday_confirmation": True,
                "can_execute_without_intraday_trigger": False,
            },
        )

        self.assertTrue(allowed)
        self.assertEqual(contract["final_action"], "open_probe")
        self.assertEqual(contract["target_lots"], -1)
        self.assertEqual(contract["lots_delta"], -1)
        self.assertTrue(contract["conditional_trigger_authority"])
        self.assertTrue(contract["requires_intraday_confirmation"])
        self.assertFalse(contract["can_execute_without_intraday_trigger"])
        self.assertIn("conditional_trigger_authority", contract["reason_codes"])

    def test_watch_for_trigger_without_setup_stays_wait_boundary(self):
        reasons = ["alpha_setup_ev_fusion", "pm_watch_for_trigger_probe_cap"]
        diagnostics = self._conditional_diagnostics()
        diagnostics["alpha_setup_ev_fusion"]["has_monitorable_setup"] = False
        diagnostics["alpha_setup_ev_fusion"]["setup_quality_ok"] = False
        diagnostics.pop("conditional_monitor_probe_seed", None)

        plan = _conditional_monitor_probe_seed_plan(
            ticker="BU",
            current_lots=0,
            target_lots=0,
            target_ratio=-0.008,
            current_ticker_exposure=0.0,
            current_net_exposure=0.0,
            account_equity=5_000_000.0,
            current_price=3500.0,
            multiplier=10.0,
            margin_rate=0.10,
            margin_available=1_000_000.0,
            max_position_ratio=0.12,
            max_net_exposure=0.40,
            morning_price_context={},
            control_reasons=reasons,
            control_diagnostics=diagnostics,
            full_config={},
        )

        self.assertFalse(plan["allowed"])
        self.assertIn("setup_quality_not_met", plan["blocked_reasons"])
        self.assertIn("missing_monitorable_setup", plan["blocked_reasons"])


if __name__ == "__main__":
    unittest.main()
