import unittest
from pathlib import Path
import sys


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.agent_tools.analysis.analyst_output_landing import (
    analyst_output_landing_violations,
    apply_analyst_output_landing_check,
)


class AnalystOutputLandingTest(unittest.TestCase):
    def _signal(self, **kwargs) -> AnalystSignal:
        defaults = {
            "agent_name": "technical",
            "signal": Signal.BULLISH,
            "confidence": 0.7,
            "opportunity_state": "watch_for_trigger",
            "entry_trigger": "break above morning range",
            "invalidation_present": True,
            "trigger_valid": False,
            "metadata": {
                "action_evidence_contract": {
                    "contract_version": "agentquant.action_evidence.v1",
                    "analyst": "technical",
                    "signal": "Bullish",
                    "side": "long",
                    "opportunity_state": "watch_for_trigger",
                    "entry_trigger": "break above morning range",
                    "trigger_valid": False,
                    "invalidation_present": True,
                    "factor_focus": [],
                }
            },
        }
        defaults.update(kwargs)
        return AnalystSignal(**defaults)

    def test_watch_for_trigger_structured_output_is_allowed(self):
        signal = self._signal()
        self.assertEqual(analyst_output_landing_violations(signal), [])

    def test_analyst_output_cannot_land_pm_trade_authority_fields(self):
        signal = self._signal(
            metadata={
                "action_evidence_contract": {
                    "opportunity_state": "tradeable_candidate",
                    "trigger_valid": True,
                    "invalidation_present": True,
                    "target_lots": 3,
                    "authority_type": "real_budget_entry",
                    "reason_codes": ["positive_open_action_value_seed"],
                }
            },
            learning_impact_summary={
                "final_action": "open_real",
                "margin_required": 10000,
                "lots_delta": 1,
            },
        )
        violations = analyst_output_landing_violations(signal)
        self.assertTrue(any("target_lots" in item for item in violations))
        self.assertTrue(any("final_action" in item for item in violations))
        self.assertTrue(any("authority_type" in item for item in violations))
        self.assertTrue(any("reason_codes" in item for item in violations))
        self.assertTrue(any("margin_required" in item for item in violations))

    def test_probe_or_tradeable_candidate_requires_current_trigger_and_invalidation(self):
        signal = self._signal(
            opportunity_state="tradeable_candidate",
            metadata={
                "action_evidence_contract": {
                    "opportunity_state": "tradeable_candidate",
                    "trigger_valid": False,
                    "invalidation_present": False,
                }
            },
        )
        violations = analyst_output_landing_violations(signal)
        self.assertIn("analyst_output_candidate_without_current_trigger", violations)
        self.assertIn("analyst_output_trade_setup_missing_invalidation", violations)

    def test_apply_landing_check_appends_validation_errors(self):
        signal = self._signal(metadata={"action_evidence_contract": {"final_action_contract": {}}})
        checked = apply_analyst_output_landing_check(signal)
        self.assertTrue(
            any(error.startswith("analyst_output_forbidden_trade_authority_field") for error in checked.validation_errors)
        )

    def test_product_learning_calibration_view_is_allowed_when_trade_authority_aliases_are_safe(self):
        signal = self._signal(
            metadata={
                "action_evidence_contract": {
                    "contract_version": "agentquant.action_evidence.v1",
                    "analyst": "technical",
                    "signal": "Bullish",
                    "side": "long",
                    "opportunity_state": "watch_for_trigger",
                    "trigger_valid": False,
                    "invalidation_present": True,
                    "factor_focus": [],
                },
                "reviewer_learning_context": {
                    "memory_trace": {
                        "selected_memory_refs": [
                            {
                                "memory_type": "alpha_setup_profile",
                                "product_learning_calibration_view": {
                                    "performance_scope_key": "EB|short|setup|trigger|evidence|capital_deployed",
                                    "deployment_tier": "capital_deployed",
                                    "historical_pm_rank": 1,
                                    "historical_pm_score": 0.83,
                                    "historical_net_pnl": 3180.0,
                                    "not_trade_authority": True,
                                },
                            }
                        ]
                    }
                },
            }
        )

        self.assertEqual(analyst_output_landing_violations(signal), [])

    def test_analyst_entrypoints_do_not_directly_consume_pm_action_values(self):
        agent_root = SRC_ROOT / "agents" / "analysis_team"
        forbidden_tokens = (
            "get_alpha_setup_action_values",
            "retrieve_pm_memory",
            "pm_learning",
            "alpha_setup_action_values",
        )
        required_tokens = ("build_learning_context", "finalize_analyst_signal")
        for filename in ("technical.py", "fundamental.py", "commodity_news.py"):
            with self.subTest(filename=filename):
                text = (agent_root / filename).read_text(encoding="utf-8")
                for token in forbidden_tokens:
                    self.assertNotIn(token, text)
                for token in required_tokens:
                    self.assertIn(token, text)
        finalizer_source = (
            SRC_ROOT / "tools" / "agent_tools" / "analysis" / "analyst_output_finalization.py"
        ).read_text(encoding="utf-8")
        self.assertIn("calibrate_signal_with_learning_context", finalizer_source)


if __name__ == "__main__":
    unittest.main()
