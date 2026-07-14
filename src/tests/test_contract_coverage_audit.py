import tempfile
import unittest
from pathlib import Path
import sys


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


def _write_covered_repo(root: Path) -> None:
    for spec in CONTRACT_SPECS:
        for rule in (*spec.producers, *spec.consumers, *spec.audits, *spec.tests):
            path = root / rule.path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n" + "\n".join(rule.patterns))
    for spec in MATRIX_CHAIN_COVERAGE_SPECS:
        for rules in spec.dimensions.values():
            for rule in rules:
                path = root / rule.path
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("\n" + "\n".join(rule.patterns))


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

    def test_missing_producer_is_detected(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _write_covered_repo(root)
            producer = root / CONTRACT_SPECS[0].producers[0].path
            producer.write_text("", encoding="utf-8")
            report = audit_contract_coverage(root)
        self.assertFalse(report.ok)
        self.assertTrue(any("missing_producer_coverage" in code for code in report.violation_codes))

    def test_pm_cannot_build_signal_collection_contract(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _write_covered_repo(root)
            path = root / "src/agents/decision_team/portfolio_manager.py"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    "\nfrom tools.common.signal_evidence_collection import build_signal_collection_contract\n"
                    "signal_collection_contract = build_signal_collection_contract()\n"
                )
            report = audit_contract_coverage(root)
        self.assertIn("signal_collection_contract_pm_imports_builder", report.violation_codes)
        self.assertIn("signal_collection_contract_pm_builds_contract", report.violation_codes)

    def test_daily_pg_is_not_a_role_check_for_pm_internal_contracts(self):
        for spec in CONTRACT_SPECS:
            for rule in spec.audits:
                self.assertNotEqual(rule.path, "src/tools/agent_tools/control/pg_system_invariants.py")


if __name__ == "__main__":
    unittest.main()
