import sys
import tempfile
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.pg_contract_coverage_audit import (
    ACTIVE_DOC_PATHS,
    CONTRACT_SPECS,
    audit_contract_coverage,
)


def _append_text(root: Path, relative_path: str, text: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(current + "\n" + text, encoding="utf-8")


def _write_minimal_covered_repo(root: Path) -> None:
    for spec in CONTRACT_SPECS:
        for rule in (*spec.producers, *spec.consumers, *spec.audits, *spec.tests):
            _append_text(root, rule.path, "\n".join(rule.patterns))

    for relative_path in ACTIVE_DOC_PATHS:
        _append_text(
            root,
            relative_path,
            "\n".join(
                [
                    "consumer_scope=pm_learning",
                    "consumer_scope=analyst_calibration",
                    "consumer_scope=trader_execution_learning",
                    "SYSTEM_FACT_ENTRY_BOUNDARY",
                    "system_fact_entry_boundary",
                    "ARTIFACT_PHASE_BOUNDARY",
                    "artifact_phase_boundary",
                    "learning_lane",
                    "retrieval_key",
                    "opportunity_score_components",
                    "capital_deployment",
                    "final_action_contract",
                ]
            ),
        )
    _append_text(
        root,
        "src/config/learning_policy_catalog.yaml",
        "\n".join(
            [
                "learning_consumer_scopes",
                "pm_allowed_consumer_scope",
                "trader_direct_research_consumption_allowed",
                "授权事实入口",
            ]
        ),
    )
    _append_text(
        root,
        "src/config/portfolio_policy_catalog.yaml",
        "consumer_scope=pm_learning\nTrader 不读研究记录\n授权事实入口",
    )

    _append_text(
        root,
        "src/agents/decision_team/portfolio_manager.py",
        "retrieve_pm_memory",
    )
    _append_text(
        root,
        "src/tools/agent_tools/decision/pm_decision_memory_retrieval.py",
        'consumer_scope="pm_learning"\n_consumer_scope\nnon_pm_learning_scope',
    )
    _append_text(
        root,
        "docs/unified_field_semantics.md",
        "\n".join(
            [
                "action_evidence_contract",
                "final_action_contract",
                "artifact_phase_boundary",
                "alpha_setup_action_value",
                "execution_learning_trace",
                "opportunity_score_components",
                "learning_used",
                    "execution_result",
                    "signal_collection_contract",
                    "effective_memory_summary",
                    "opportunity_scorecard",
                    "position_sizing_result",
                    "consumer_scope",
                "learning_lane",
                "retrieval_key",
                "uncovered_risks",
                "contract_coverage_audit",
                "signal_collection_contract",
                "effective_memory_summary",
                "opportunity_scorecard",
                "position_sizing_result",
            ]
        ),
    )


class ContractCoverageAuditTest(unittest.TestCase):
    def test_current_repo_contract_coverage_is_clean(self):
        report = audit_contract_coverage(PROJECT_ROOT)

        self.assertTrue(report.ok, report.to_dict())

    def test_contract_coverage_detects_bare_execution_learning_trace_writer(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo = Path(raw_tmp)
            _write_minimal_covered_repo(repo)
            _append_text(
                repo,
                "src/tools/agent_tools/execution/bad_writer.py",
                'snapshot["execution_learning_trace"] = {}',
            )

            report = audit_contract_coverage(repo)

        self.assertFalse(report.ok)
        self.assertIn(
            "bare_contract_write:execution_learning_trace:src/tools/agent_tools/execution/bad_writer.py:2",
            report.errors,
        )

    def test_contract_coverage_accepts_fully_covered_minimal_repo(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo = Path(raw_tmp)
            _write_minimal_covered_repo(repo)

            report = audit_contract_coverage(repo)

        self.assertTrue(report.ok, report.to_dict())


if __name__ == "__main__":
    unittest.main()
