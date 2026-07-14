from __future__ import annotations

"""Static producer-to-consumer coverage for the finalized business chain."""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.pg_schemas import ProtocolCheckResult, ProtocolGovernorReport


@dataclass(frozen=True)
class ContractEvidenceRule:
    path: str
    patterns: Sequence[str]
    description: str


@dataclass(frozen=True)
class ContractCoverageSpec:
    contract: str
    producers: Sequence[ContractEvidenceRule]
    consumers: Sequence[ContractEvidenceRule]
    audits: Sequence[ContractEvidenceRule]
    tests: Sequence[ContractEvidenceRule]


MATRIX_CHAIN_DIMENSIONS = (
    "producer",
    "physical_landing",
    "consumer",
    "role_check",
    "real_path_test",
    "mechanism_doc",
)


@dataclass(frozen=True)
class MatrixChainCoverageSpec:
    contract: str
    dimensions: dict[str, Sequence[ContractEvidenceRule]]


@dataclass
class ContractCoverageRow:
    contract: str
    producers: list[str] = field(default_factory=list)
    consumers: list[str] = field(default_factory=list)
    audits: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    uncovered_risks: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.uncovered_risks


@dataclass
class MatrixChainCoverageRow:
    contract: str
    dimensions: dict[str, list[str]] = field(default_factory=dict)
    uncovered_risks: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.uncovered_risks


@dataclass
class ContractCoverageAuditReport:
    matrix: list[ContractCoverageRow]
    matrix_chain: list[MatrixChainCoverageRow]
    violation_codes: list[str]

    @property
    def ok(self) -> bool:
        return not self.violation_codes


def _rule(path: str, patterns: Sequence[str], description: str) -> ContractEvidenceRule:
    return ContractEvidenceRule(path, tuple(patterns), description)


CONTRACT_SPECS: Sequence[ContractCoverageSpec] = (
    ContractCoverageSpec(
        "action_evidence_contract",
        producers=(
            _rule("src/tools/agent_tools/analysis/analyst_quality.py", ("def _build_action_evidence_contract",), "analysts build AEC"),
        ),
        consumers=(
            _rule("src/tools/common/signal_evidence_collection.py", ("def validate_action_evidence_contract", "def build_signal_collection_contract"), "collector validates and consumes AEC"),
        ),
        audits=(
            _rule("docs/matrix_field_semantics.md", ("action_evidence_contract",), "field matrix fixes AEC semantics"),
        ),
        tests=(
            _rule("src/tests/test_analyst_output_landing.py", ("action_evidence_contract",), "analyst landing tests AEC"),
        ),
    ),
    ContractCoverageSpec(
        "signal_collection_contract",
        producers=(
            _rule("src/agents/decision_team/signal_collector.py", ("signal_collection_contract", "build_signal_collection_contract"), "collector publishes SCC"),
        ),
        consumers=(
            _rule("src/agents/decision_team/portfolio_manager.py", ("validate_signal_collection_contract", "build_pm_evidence_signals_from_scc"), "PM consumes SCC"),
        ),
        audits=(
            _rule("src/tools/common/signal_evidence_collection.py", ("def validate_signal_collection_contract",), "shared SCC validator"),
        ),
        tests=(
            _rule("src/tests/test_decision_workflow_tools.py", ("signal_collection_contract",), "collector-to-PM tests"),
        ),
    ),
    ContractCoverageSpec(
        "final_action_contract",
        producers=(
            _rule("src/agents/decision_team/portfolio_manager.py", ("_build_final_action_contract", 'snapshot["final_action_contract"]'), "PM Step6 signs one contract"),
        ),
        consumers=(
            _rule("src/agents/decision_team/auditor.py", ("final_action_contract",), "Auditor reads final contract"),
            _rule("src/agents/execution_team/trader.py", ("_final_action_contract_from_snapshot",), "Trader reads final contract"),
        ),
        audits=(
            _rule("src/tools/agent_tools/decision/pm_contract_self_check.py", ("def check_final_action_contract",), "PM validates its signed contract"),
        ),
        tests=(
            _rule("src/tests/test_pre_backtest_pm_workflow_contracts.py", ("test_three_pm_paths_sign_exactly_one_final_contract",), "real PM Step6 test"),
        ),
    ),
    ContractCoverageSpec(
        "audit_verdict",
        producers=(
            _rule("src/agents/decision_team/auditor.py", ("def audit_futures_recommendation", '"audit_verdict"'), "Auditor produces verdict"),
        ),
        consumers=(
            _rule("src/agents/execution_team/trader.py", ("audit_verdict_allows_trader",), "Trader consumes verdict"),
        ),
        audits=(
            _rule("docs/matrix_field_semantics.md", ("audit_verdict",), "field matrix fixes verdict"),
        ),
        tests=(
            _rule("src/tests/test_fact_entry_boundaries.py", ("audit_futures_recommendation",), "fact-entry tests cover verdict"),
        ),
    ),
    ContractCoverageSpec(
        "execution_result",
        producers=(
            _rule("src/util/futures_audit.py", ("def set_execution_result",), "Trader execution helper writes result"),
        ),
        consumers=(
            _rule("src/tools/agent_tools/research/reviewer_phase4_review.py", ("execution_result",), "Reviewer reads result"),
            _rule("src/tools/agent_tools/research/research_learning.py", ("execution_result",), "Researcher reads result"),
        ),
        audits=(
            _rule("docs/matrix_field_semantics.md", ("execution_result",), "field matrix fixes execution result"),
        ),
        tests=(
            _rule("src/tests/test_phase_flow_regression.py", ("execution_result",), "phase flow covers execution result"),
        ),
    ),
    ContractCoverageSpec(
        "daily_settlement",
        producers=(
            _rule("src/tools/agent_tools/execution/accountant_futures_settlement.py", ("def daily_settlement", "current_account_equity"), "Accountant produces settlement"),
        ),
        consumers=(
            _rule("src/tools/agent_tools/research/reviewer_phase4_review.py", ("daily_settlement",), "Reviewer reads settlement"),
        ),
        audits=(
            _rule("src/tools/agent_tools/execution/accountant_futures_settlement.py", ("def _assert_accounting_invariants",), "Accountant checks formulas"),
        ),
        tests=(
            _rule("src/tests/test_accountant_settlement_formulas.py", ("current_account_equity",), "settlement formula tests"),
        ),
    ),
    ContractCoverageSpec(
        "alpha_setup_action_value",
        producers=(
            _rule("src/tools/agent_tools/research/research_memory_writers.py", ("alpha_setup_action_value", "researcher_learning_completed"), "Researcher writes future learning"),
        ),
        consumers=(
            _rule("src/tools/agent_tools/decision/pm_decision_memory_retrieval.py", ("alpha_setup_action_values",), "PM Step4 reads decision learning"),
        ),
        audits=(
            _rule("docs/matrix_action_canonical.md", ("action_preference", "canonical_action_family"), "action matrix fixes learning semantics"),
        ),
        tests=(
            _rule("src/tests/test_reviewer_learning.py", ("alpha_setup_action_value",), "research learning tests"),
        ),
    ),
    ContractCoverageSpec(
        "protocol_governor_report",
        producers=(
            _rule("src/tools/agent_tools/control/pg_schemas.py", ("class ProtocolGovernorReport", "violation_codes", "diagnostic_codes"), "PG owns one report schema"),
        ),
        consumers=(
            _rule("src/run/pre_backtest_test.py", ("ProtocolGovernor", 'report["status"]'), "pre-backtest runner consumes report"),
            _rule("src/run/backtest_daily_test.py", ("audit_daily_results", 'report["status"]'), "daily runner consumes report"),
        ),
        audits=(
            _rule("docs/matrix_field_semantics.md", ("check_name", "violation_codes", "diagnostic_codes"), "PG report fields are registered"),
        ),
        tests=(
            _rule("src/tests/test_protocol_governor.py", ("ProtocolGovernor",), "PG entrypoint tests"),
        ),
    ),
)


def _matrix_dimensions(spec: ContractCoverageSpec) -> dict[str, Sequence[ContractEvidenceRule]]:
    if spec.contract == "protocol_governor_report":
        landing_rule = _rule("docs/agent_pg.md", ("回测前报告", "每日检测"), "PG document records report boundary")
        doc_rule = _rule("docs/agent_pg.md", ("固定字段与动作边界",), "PG mechanism fixes report semantics")
    else:
        landing_rule = _rule("docs/workflow.md", (spec.contract,), "workflow records physical or in-memory landing")
        doc_rule = _rule("docs/matrix_chain_contract.md", (spec.contract,), "chain matrix records contract")
    return {
        "producer": spec.producers,
        "physical_landing": (landing_rule,),
        "consumer": spec.consumers,
        "role_check": spec.audits,
        "real_path_test": spec.tests,
        "mechanism_doc": (doc_rule,),
    }


MATRIX_CHAIN_COVERAGE_SPECS: Sequence[MatrixChainCoverageSpec] = tuple(
    MatrixChainCoverageSpec(spec.contract, _matrix_dimensions(spec)) for spec in CONTRACT_SPECS
)

ACTIVE_DOC_PATHS = (
    "docs/workflow.md",
    "docs/matrix_chain_contract.md",
    "docs/matrix_field_semantics.md",
    "docs/matrix_action_canonical.md",
    "docs/agent_pg.md",
)


def _read(root: Path, relative: str) -> str:
    path = root / relative
    return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""


def _match_rules(root: Path, rules: Sequence[ContractEvidenceRule]) -> list[str]:
    evidence: list[str] = []
    for rule in rules:
        text = _read(root, rule.path)
        if text and all(pattern in text for pattern in rule.patterns):
            evidence.append(f"{rule.path}: {rule.description}")
    return evidence


def _row(root: Path, spec: ContractCoverageSpec) -> ContractCoverageRow:
    row = ContractCoverageRow(
        contract=spec.contract,
        producers=_match_rules(root, spec.producers),
        consumers=_match_rules(root, spec.consumers),
        audits=_match_rules(root, spec.audits),
        tests=_match_rules(root, spec.tests),
    )
    for name, values in (
        ("producer", row.producers),
        ("consumer", row.consumers),
        ("role_check", row.audits),
        ("real_path_test", row.tests),
    ):
        if not values:
            row.uncovered_risks.append(f"{spec.contract}_missing_{name}_coverage")
    return row


def _matrix_row(root: Path, spec: MatrixChainCoverageSpec) -> MatrixChainCoverageRow:
    dimensions = {name: _match_rules(root, rules) for name, rules in spec.dimensions.items()}
    risks = [
        f"{spec.contract}_missing_matrix_{name}_coverage"
        for name in MATRIX_CHAIN_DIMENSIONS
        if not dimensions.get(name)
    ]
    return MatrixChainCoverageRow(spec.contract, dimensions, risks)


def _boundary_errors(root: Path) -> list[str]:
    errors: list[str] = []
    pm_source = _read(root, "src/agents/decision_team/portfolio_manager.py")
    if "from tools.common.signal_evidence_collection import build_signal_collection_contract" in pm_source:
        errors.append("signal_collection_contract_pm_imports_builder")
    if "signal_collection_contract = build_signal_collection_contract(" in pm_source:
        errors.append("signal_collection_contract_pm_builds_contract")
    return errors


def audit_contract_coverage(repo_root: str | Path) -> ContractCoverageAuditReport:
    root = Path(repo_root).resolve()
    matrix = [_row(root, spec) for spec in CONTRACT_SPECS]
    matrix_chain = [_matrix_row(root, spec) for spec in MATRIX_CHAIN_COVERAGE_SPECS]
    violation_codes = [f"contract_coverage_uncovered:{risk}" for row in matrix for risk in row.uncovered_risks]
    violation_codes.extend(f"contract_coverage_uncovered:{risk}" for row in matrix_chain for risk in row.uncovered_risks)
    violation_codes.extend(_boundary_errors(root))
    return ContractCoverageAuditReport(matrix, matrix_chain, violation_codes)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit AgentQuant static contract coverage.")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[4]))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = audit_contract_coverage(args.repo_root)
    check = (
        ProtocolCheckResult.fail_result("field_action_and_role_unification", result.violation_codes)
        if result.violation_codes
        else ProtocolCheckResult.pass_result("field_action_and_role_unification")
    )
    report = ProtocolGovernorReport([check]).to_dict()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report["status"])
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
