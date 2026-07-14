import inspect
import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.control_team.protocol_governor import ProtocolGovernor, protocol_governor_agent
from tools.agent_tools.control.pg_schemas import ProtocolCheckResult, ProtocolGovernorReport


class ProtocolGovernorTest(unittest.TestCase):
    def test_report_uses_only_registered_fields(self):
        report = ProtocolGovernorReport(
            [ProtocolCheckResult.pass_result("environment_and_entry", diagnostic_codes=["optional_path_missing"])]
        ).to_dict()
        self.assertEqual(set(report), {"contract_version", "source_agent", "status", "checks"})
        self.assertEqual(
            set(report["checks"][0]),
            {"check_name", "status", "violation_codes", "diagnostic_codes"},
        )

    def test_failed_check_requires_stable_violation_code(self):
        with self.assertRaises(ValueError):
            ProtocolCheckResult(check_name="data_readiness", status="failed")

    def test_governor_exposes_only_pre_and_daily_modes(self):
        public = {
            name
            for name, value in inspect.getmembers(ProtocolGovernor, predicate=inspect.isfunction)
            if not name.startswith("_")
        }
        self.assertEqual(public, {"run_pre_backtest_acceptance", "audit_daily_results"})

    def test_agent_requires_explicit_sidecar_mode(self):
        with self.assertRaisesRegex(ValueError, "protocol_governor_mode_required"):
            protocol_governor_agent()

    def test_governor_has_no_trade_authority_or_internal_agent_audit_methods(self):
        source = inspect.getsource(ProtocolGovernor)
        for forbidden in (
            "classify_memory_quality",
            "audit_action_preference_landing",
            "audit_cost_budget",
            "classify_exploration",
            "create_task_event",
            "validate_task_lifecycle",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
