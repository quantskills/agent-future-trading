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
    MATRIX_CHAIN_COVERAGE_SPECS,
    MATRIX_CHAIN_DIMENSIONS,
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
    for spec in MATRIX_CHAIN_COVERAGE_SPECS:
        for rules in spec.dimensions.values():
            for rule in rules:
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
        "src/tools/common/signal_evidence_collection.py",
        '"source_agent": "signal_collector"',
    )
    _append_text(
        root,
        "src/run/pre_backtest_test.py",
        '"src.tests.test_reviewer_transaction_log_readability"',
    )
    _append_text(
        root,
        "src/tests/test_reviewer_transaction_log_readability.py",
        "\n".join(
            [
                "完整交易日志",
                'output_path.write_text(report_text, encoding="utf-8")',
                "5. Signal Summary",
                "TradingPhase.PHASE4",
            ]
        ),
    )
    _append_text(
        root,
        "src/tools/agent_tools/research/reviewer_phase4_review.py",
        "\n".join(
            [
                "budget_drift_diagnostics",
                '"reviewer_hard_gate": False',
                'output_path.write_text(report_text, encoding="utf-8")',
            ]
        ),
    )
    _append_text(
        root,
        "src/llm/prompt.py",
        "\n".join(
            [
                "budget_drift_diagnostics",
                "reviewer_hard_gate=false",
                "not final_action_contract invalidation",
                "not same-day trade authority",
            ]
        ),
    )
    _append_text(
        root,
        "src/tools/agent_tools/decision/pm_decision_memory_retrieval.py",
        'consumer_scope="pm_learning"\n_consumer_scope\nnon_pm_learning_scope',
    )
    _append_text(
        root,
        "docs/matrix_field_semantics.md",
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
                "rank_capital_layer_contract",
            ]
        ),
    )


class ContractCoverageAuditTest(unittest.TestCase):
    def test_current_repo_contract_coverage_is_clean(self):
        report = audit_contract_coverage(PROJECT_ROOT)

        self.assertTrue(report.ok, report.to_dict())

    def test_contract_coverage_rejects_pm_signal_collection_builder(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo = Path(raw_tmp)
            _write_minimal_covered_repo(repo)
            _append_text(
                repo,
                "src/agents/decision_team/portfolio_manager.py",
                "from tools.common.signal_evidence_collection import build_signal_collection_contract\n"
                "signal_collection_contract = build_signal_collection_contract()",
            )

            report = audit_contract_coverage(repo)

        self.assertFalse(report.ok)
        self.assertIn("signal_collection_contract_pm_imports_builder", report.errors)
        self.assertIn("signal_collection_contract_pm_builds_contract", report.errors)

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

    def test_current_repo_matrix_chain_contract_coverage_is_clean(self):
        report = audit_contract_coverage(PROJECT_ROOT)

        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(
            {row.contract for row in report.matrix_chain},
            {spec.contract for spec in MATRIX_CHAIN_COVERAGE_SPECS},
        )
        for row in report.matrix_chain:
            self.assertEqual(set(row.dimensions), set(MATRIX_CHAIN_DIMENSIONS), row.to_dict())
            for dimension in MATRIX_CHAIN_DIMENSIONS:
                self.assertTrue(row.dimensions[dimension], row.to_dict())

    def test_matrix_chain_contract_coverage_requires_pre_backtest_fixture_gate(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo = Path(raw_tmp)
            _write_minimal_covered_repo(repo)
            fixture_path = repo / "src" / "tools" / "agent_tools" / "control" / "pg_pre_backtest_failure_fixtures.py"
            fixture_path.write_text("", encoding="utf-8")

            report = audit_contract_coverage(repo)

        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.endswith("_missing_matrix_pre_backtest_fixture_gate_coverage") for error in report.errors),
            report.to_dict(),
        )

    def test_learning_used_consumers_follow_shared_semantic_entry(self):
        learning_spec = next(spec for spec in CONTRACT_SPECS if spec.contract == "learning_used")
        consumer_paths = {rule.path for rule in learning_spec.consumers}

        self.assertIn("src/tools/common/final_action_semantics.py", consumer_paths)
        self.assertIn("src/tools/agent_tools/decision/pm_contract_self_check.py", consumer_paths)
        self.assertNotIn("src/tools/agent_tools/control/pg_mechanism_effectiveness_audit.py", consumer_paths)
        self.assertNotIn("src/tools/agent_tools/research/reviewer_phase4_review.py", consumer_paths)

    def test_pm_six_step_trace_has_producer_consumer_audit_and_test_coverage(self):
        spec = next(spec for spec in CONTRACT_SPECS if spec.contract == "pm_six_step_trace")

        producer_paths = {rule.path for rule in spec.producers}
        consumer_paths = {rule.path for rule in spec.consumers}
        audit_paths = {rule.path for rule in spec.audits}
        test_paths = {rule.path for rule in spec.tests}

        self.assertIn("src/agents/decision_team/portfolio_manager.py", producer_paths)
        self.assertIn("src/graph/workflow.py", consumer_paths)
        self.assertIn("src/tools/agent_tools/control/pg_system_invariants.py", audit_paths)
        self.assertIn("src/tests/test_pre_backtest_pm_workflow_contracts.py", test_paths)
        self.assertIn("src/tests/test_system_invariant_audit.py", test_paths)

    def test_rank_capital_layer_contract_stays_pm_owned(self):
        spec = next(spec for spec in CONTRACT_SPECS if spec.contract == "rank_capital_layer_contract")
        consumer_paths = {rule.path for rule in spec.consumers}

        self.assertIn("src/tools/agent_tools/decision/pm_contract_self_check.py", consumer_paths)
        self.assertNotIn("src/tools/agent_tools/control/pg_mechanism_effectiveness_audit.py", consumer_paths)

    def test_contract_coverage_requires_reviewer_log_readability_pre_backtest_gate(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo = Path(raw_tmp)
            _write_minimal_covered_repo(repo)
            (repo / "src" / "run" / "pre_backtest_test.py").write_text(
                "PRE_BACKTEST_TEST_MODULES = []",
                encoding="utf-8",
            )

            report = audit_contract_coverage(repo)

        self.assertFalse(report.ok)
        self.assertTrue(
            any(
                error.startswith(
                    "reviewer_pre_backtest_boundary_missing:src/run/pre_backtest_test.py"
                )
                for error in report.errors
            ),
            report.to_dict(),
        )


if __name__ == "__main__":
    unittest.main()
