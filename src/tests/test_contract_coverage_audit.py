import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.pg_contract_coverage_audit import (
    CONTRACT_SPECS,
    MATRIX_CHAIN_COVERAGE_SPECS,
    MATRIX_CHAIN_DIMENSIONS,
    audit_contract_coverage,
)


PROJECT_ROOT = SRC_ROOT.parent


class ContractCoverageAuditTest(unittest.TestCase):
    def test_current_repo_contract_coverage_is_clean(self):
        report = audit_contract_coverage(PROJECT_ROOT)
        self.assertTrue(report.ok, report.violation_codes)

    def test_each_contract_has_all_static_dimensions(self):
        report = audit_contract_coverage(PROJECT_ROOT)
        self.assertEqual({row.contract for row in report.matrix_chain}, {spec.contract for spec in CONTRACT_SPECS})
        for row in report.matrix_chain:
            self.assertEqual(set(row.dimensions), set(MATRIX_CHAIN_DIMENSIONS))
            self.assertTrue(all(row.dimensions.values()), row.uncovered_risks)

    def test_matching_source_strings_are_not_runtime_contract_coverage(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            fake = root / "src" / "tools" / "common" / "signal_evidence_collection.py"
            fake.parent.mkdir(parents=True)
            fake.write_text(
                "def validate_action_evidence_contract(): pass\n"
                "def build_signal_collection_contract(): pass\n",
                encoding="utf-8",
            )
            report = audit_contract_coverage(root)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("runtime_evidence" in code for code in report.violation_codes),
            report.violation_codes,
        )

    def test_daily_settlement_producer_is_the_enabled_phase3_entry(self):
        report = audit_contract_coverage(PROJECT_ROOT)
        settlement = next(row for row in report.matrix if row.contract == "daily_settlement")
        self.assertTrue(
            any("FuturesDailySettlement.run_phase3" in evidence for evidence in settlement.producers),
            settlement.producers,
        )

    def test_missing_producer_is_detected(self):
        from tools.agent_tools.control import pg_contract_coverage_audit as module

        missing = CONTRACT_SPECS[0].producers[0]
        original = module._resolve_runtime_rule

        def resolve(rule):
            return None if rule == missing else original(rule)

        with patch.object(module, "_resolve_runtime_rule", side_effect=resolve):
            report = audit_contract_coverage(PROJECT_ROOT)
        self.assertFalse(report.ok)
        self.assertTrue(any("missing_producer_runtime_evidence" in code for code in report.violation_codes))

    def test_pm_cannot_build_signal_collection_contract(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            path = root / "src/agents/decision_team/portfolio_manager.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                "from tools.common.signal_evidence_collection import build_signal_collection_contract\n"
                "signal_collection_contract = build_signal_collection_contract()\n",
                encoding="utf-8",
            )
            report = audit_contract_coverage(root)
        self.assertIn("signal_collection_contract_pm_imports_builder", report.violation_codes)
        self.assertIn("signal_collection_contract_pm_builds_contract", report.violation_codes)

    def test_daily_pg_is_not_a_role_check_for_pm_internal_contracts(self):
        for spec in CONTRACT_SPECS:
            for rule in spec.audits:
                self.assertNotEqual(rule.module, "tools.agent_tools.control.pg_system_invariants")


if __name__ == "__main__":
    unittest.main()
