from __future__ import annotations

"""Runtime producer-to-consumer coverage for the canonical business chain."""

import argparse
import ast
import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

SRC_ROOT = Path(__file__).resolve().parents[3]
PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.pg_schemas import ProtocolCheckResult, ProtocolGovernorReport


@dataclass(frozen=True)
class RuntimeEvidenceRule:
    module: str
    qualname: str
    description: str


@dataclass(frozen=True)
class DocumentEvidenceRule:
    path: str
    token: str
    description: str


@dataclass(frozen=True)
class ContractCoverageSpec:
    contract: str
    producers: Sequence[RuntimeEvidenceRule]
    physical_landings: Sequence[RuntimeEvidenceRule]
    consumers: Sequence[RuntimeEvidenceRule]
    audits: Sequence[RuntimeEvidenceRule]
    tests: Sequence[RuntimeEvidenceRule]
    documents: Sequence[DocumentEvidenceRule]


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
    dimensions: dict[str, Sequence[RuntimeEvidenceRule | DocumentEvidenceRule]]


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


def _runtime(module: str, qualname: str, description: str) -> RuntimeEvidenceRule:
    return RuntimeEvidenceRule(module, qualname, description)


def _document(path: str, token: str, description: str) -> DocumentEvidenceRule:
    return DocumentEvidenceRule(path, token, description)


FULL_CHAIN = _runtime(
    "tools.agent_tools.control.pg_full_chain_dry_run",
    "run_no_llm_full_chain_dry_run",
    "formal same-database production-chain dry run",
)

ALPHA_SETUP_ACTION_VALUE_REAL_PATH = _runtime(
    "tests.test_reviewer_learning",
    "ReviewerLearningPersistenceRegressionTest.test_real_path_externalized_cross_day_episodes_reach_next_day_pm_retrieval",
    "ordinary settled episode to sample/profile/action-value and next-day PM retrieval regression",
)


CONTRACT_SPECS: Sequence[ContractCoverageSpec] = (
    ContractCoverageSpec(
        "action_evidence_contract",
        producers=(
            _runtime(
                "tools.agent_tools.analysis.analyst_output_finalization",
                "finalize_analyst_signal",
                "analyst finalizer produces validated AEC",
            ),
        ),
        physical_landings=(
            _runtime("database.sqlite_helper", "SQLiteDB.save_signal", "formal signal persistence"),
        ),
        consumers=(
            _runtime(
                "tools.common.signal_evidence_collection",
                "build_signal_collection_contract",
                "SCC builder consumes analyst AEC",
            ),
        ),
        audits=(
            _runtime(
                "tools.common.signal_evidence_collection",
                "validate_action_evidence_contract",
                "shared AEC validator",
            ),
        ),
        tests=(FULL_CHAIN,),
        documents=(
            _document("docs/matrix_chain_contract.md", "action_evidence_contract", "canonical chain matrix"),
        ),
    ),
    ContractCoverageSpec(
        "signal_collection_contract",
        producers=(
            _runtime(
                "agents.decision_team.signal_collector",
                "signal_collector_agent",
                "Signal Collector produces the SCC",
            ),
        ),
        physical_landings=(
            _runtime(
                "database.sqlite_helper",
                "SQLiteDB.save_futures_recommendation",
                "SCC lands inside the formal recommendation snapshot",
            ),
        ),
        consumers=(
            _runtime(
                "agents.decision_team.portfolio_manager",
                "portfolio_agent_futures",
                "PM consumes the SCC",
            ),
        ),
        audits=(
            _runtime(
                "tools.common.signal_evidence_collection",
                "validate_signal_collection_contract",
                "shared SCC validator",
            ),
        ),
        tests=(FULL_CHAIN,),
        documents=(
            _document("docs/matrix_chain_contract.md", "signal_collection_contract", "canonical chain matrix"),
        ),
    ),
    ContractCoverageSpec(
        "final_action_contract",
        producers=(
            _runtime(
                "agents.decision_team.portfolio_manager",
                "finalize_pm_full_market_contracts",
                "PM Step5 and Step6 sign FAC",
            ),
        ),
        physical_landings=(
            _runtime(
                "database.sqlite_helper",
                "SQLiteDB.save_futures_recommendation",
                "FAC lands in the recommendation snapshot",
            ),
        ),
        consumers=(
            _runtime(
                "agents.decision_team.auditor",
                "audit_futures_recommendation",
                "Auditor consumes FAC",
            ),
            _runtime(
                "agents.execution_team.trader",
                "_process_strategy_recommendations",
                "Trader executes against FAC",
            ),
        ),
        audits=(
            _runtime(
                "tools.agent_tools.decision.pm_contract_self_check",
                "check_final_action_contract",
                "PM FAC self-check",
            ),
            _runtime(
                "tools.common.final_action_semantics",
                "validate_final_action_lot_transition",
                "shared signed-lot FAC validator",
            ),
        ),
        tests=(FULL_CHAIN,),
        documents=(
            _document("docs/matrix_chain_contract.md", "final_action_contract", "canonical chain matrix"),
        ),
    ),
    ContractCoverageSpec(
        "audit_verdict",
        producers=(
            _runtime(
                "agents.decision_team.auditor",
                "audit_futures_recommendation",
                "Auditor produces the verdict payload",
            ),
        ),
        physical_landings=(
            _runtime(
                "database.sqlite_helper",
                "SQLiteDB.update_futures_recommendation_status",
                "Auditor payload lands on the recommendation",
            ),
        ),
        consumers=(
            _runtime(
                "agents.execution_team.trader",
                "_process_strategy_recommendations",
                "Trader gates strategy execution by verdict",
            ),
        ),
        audits=(
            _runtime(
                "tools.common.contracts",
                "validate_auditor_artifact_boundary",
                "shared Auditor boundary validator",
            ),
        ),
        tests=(FULL_CHAIN,),
        documents=(
            _document("docs/matrix_chain_contract.md", "audit_verdict", "canonical chain matrix"),
        ),
    ),
    ContractCoverageSpec(
        "execution_result",
        producers=(
            _runtime("util.futures_audit", "set_execution_result", "Trader writes execution_result"),
        ),
        physical_landings=(
            _runtime(
                "database.sqlite_helper",
                "SQLiteDB.update_futures_recommendation_status",
                "execution_result lands on the recommendation",
            ),
        ),
        consumers=(
            _runtime(
                "tools.agent_tools.research.reviewer_phase4_review",
                "run_phase4_review",
                "Reviewer reads execution facts",
            ),
            _runtime(
                "agents.research_team.researcher",
                "researcher_agent",
                "Researcher traces execution facts",
            ),
        ),
        audits=(
            _runtime(
                "tools.common.contracts",
                "validate_execution_artifact_boundary",
                "shared execution artifact boundary",
            ),
        ),
        tests=(FULL_CHAIN,),
        documents=(
            _document("docs/matrix_chain_contract.md", "execution_result", "canonical chain matrix"),
        ),
    ),
    ContractCoverageSpec(
        "daily_settlement",
        producers=(
            _runtime(
                "tools.agent_tools.execution.accountant_futures_settlement",
                "FuturesDailySettlement.run_phase3",
                "enabled Accountant Phase3 settlement entry",
            ),
        ),
        physical_landings=(
            _runtime(
                "database.sqlite_helper",
                "SQLiteDB.save_daily_settlement",
                "formal daily settlement persistence",
            ),
        ),
        consumers=(
            _runtime(
                "tools.agent_tools.research.reviewer_phase4_review",
                "run_phase4_review",
                "Reviewer reads settlement",
            ),
            _runtime(
                "agents.research_team.researcher",
                "researcher_agent",
                "Researcher traces settlement",
            ),
        ),
        audits=(
            _runtime(
                "tools.agent_tools.execution.accountant_futures_settlement",
                "FuturesDailySettlement._assert_accounting_invariants",
                "Accountant accounting invariants",
            ),
        ),
        tests=(FULL_CHAIN,),
        documents=(
            _document("docs/matrix_chain_contract.md", "daily_settlement", "canonical chain matrix"),
        ),
    ),
    ContractCoverageSpec(
        "alpha_setup_action_value",
        producers=(
            _runtime(
                "tools.common.alpha_setup",
                "_upsert_action_values",
                "Researcher action-value production",
            ),
        ),
        physical_landings=(
            _runtime(
                "tools.agent_tools.research.research_memory_writers",
                "upsert_alpha_setup_action_value",
                "formal action-value persistence",
            ),
        ),
        consumers=(
            _runtime(
                "tools.agent_tools.decision.pm_decision_memory_retrieval",
                "retrieve_pm_memory",
                "PM reads canonical action-value memory",
            ),
        ),
        audits=(
            _runtime(
                "tools.common.final_action_semantics",
                "validate_action_value_write_consistency",
                "shared action-value validator",
            ),
        ),
        tests=(ALPHA_SETUP_ACTION_VALUE_REAL_PATH,),
        documents=(
            _document("docs/matrix_action_canonical.md", "canonical_action_family", "canonical action matrix"),
        ),
    ),
    ContractCoverageSpec(
        "protocol_governor_report",
        producers=(
            _runtime(
                "tools.agent_tools.control.pg_schemas",
                "ProtocolGovernorReport",
                "single PG report schema",
            ),
        ),
        physical_landings=(
            _runtime(
                "tools.agent_tools.control.pg_schemas",
                "ProtocolGovernorReport.to_dict",
                "registered report serialization",
            ),
        ),
        consumers=(
            _runtime("run.pre_backtest_test", "main", "pre-backtest gate consumes report"),
            _runtime("run.backtest_daily_test", "main", "daily gate consumes report"),
        ),
        audits=(
            _runtime(
                "tools.agent_tools.control.pg_schemas",
                "ProtocolCheckResult.fail_result",
                "stable violation/diagnostic result schema",
            ),
        ),
        tests=(
            _runtime(
                "tests.test_protocol_governor",
                "ProtocolGovernorTest.test_report_uses_only_registered_fields",
                "report field behavior test",
            ),
        ),
        documents=(
            _document("docs/agent_pg.md", "protocol_governor_report", "PG mechanism definition"),
        ),
    ),
)


def _matrix_dimensions(
    spec: ContractCoverageSpec,
) -> dict[str, Sequence[RuntimeEvidenceRule | DocumentEvidenceRule]]:
    return {
        "producer": spec.producers,
        "physical_landing": spec.physical_landings,
        "consumer": spec.consumers,
        "role_check": spec.audits,
        "real_path_test": spec.tests,
        "mechanism_doc": spec.documents,
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


def _is_live_repo(root: Path) -> bool:
    try:
        return root.resolve() == PROJECT_ROOT.resolve()
    except OSError:
        return False


def _resolve_runtime_rule(rule: RuntimeEvidenceRule) -> object | None:
    try:
        value: object = importlib.import_module(rule.module)
        for part in rule.qualname.split("."):
            value = getattr(value, part)
        return value if callable(value) else None
    except (AttributeError, ImportError, RuntimeError):
        return None


def _runtime_evidence(root: Path, rules: Sequence[RuntimeEvidenceRule]) -> list[str]:
    if not _is_live_repo(root):
        return []
    evidence: list[str] = []
    for rule in rules:
        if _resolve_runtime_rule(rule) is not None:
            evidence.append(f"{rule.module}:{rule.qualname}: {rule.description}")
    return evidence


def _document_evidence(root: Path, rules: Sequence[DocumentEvidenceRule]) -> list[str]:
    evidence: list[str] = []
    for rule in rules:
        path = root / rule.path
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8-sig", errors="strict")
        if rule.token in text:
            evidence.append(f"{rule.path}: {rule.description}")
    return evidence


def _row(root: Path, spec: ContractCoverageSpec) -> ContractCoverageRow:
    row = ContractCoverageRow(
        contract=spec.contract,
        producers=_runtime_evidence(root, spec.producers),
        consumers=_runtime_evidence(root, spec.consumers),
        audits=_runtime_evidence(root, spec.audits),
        tests=_runtime_evidence(root, spec.tests),
    )
    for name, values in (
        ("producer", row.producers),
        ("consumer", row.consumers),
        ("role_check", row.audits),
        ("real_path_test", row.tests),
    ):
        if not values:
            row.uncovered_risks.append(f"{spec.contract}_missing_{name}_runtime_evidence")
    return row


def _matrix_row(root: Path, spec: MatrixChainCoverageSpec) -> MatrixChainCoverageRow:
    dimensions: dict[str, list[str]] = {}
    for name, rules in spec.dimensions.items():
        runtime_rules = [rule for rule in rules if isinstance(rule, RuntimeEvidenceRule)]
        document_rules = [rule for rule in rules if isinstance(rule, DocumentEvidenceRule)]
        dimensions[name] = [
            *_runtime_evidence(root, runtime_rules),
            *_document_evidence(root, document_rules),
        ]
    risks = [
        f"{spec.contract}_missing_matrix_{name}_runtime_evidence"
        for name in MATRIX_CHAIN_DIMENSIONS
        if not dimensions.get(name)
    ]
    return MatrixChainCoverageRow(spec.contract, dimensions, risks)


def _boundary_errors(root: Path) -> list[str]:
    path = root / "src/agents/decision_team/portfolio_manager.py"
    if not path.is_file():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError):
        return ["signal_collection_contract_pm_boundary_unreadable"]
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tools.common.signal_evidence_collection":
            if any(alias.name == "build_signal_collection_contract" for alias in node.names):
                errors.append("signal_collection_contract_pm_imports_builder")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "build_signal_collection_contract":
                errors.append("signal_collection_contract_pm_builds_contract")
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "build_signal_collection_contract":
                errors.append("signal_collection_contract_pm_builds_contract")
    return sorted(set(errors))


def audit_contract_coverage(repo_root: str | Path) -> ContractCoverageAuditReport:
    root = Path(repo_root).resolve()
    matrix = [_row(root, spec) for spec in CONTRACT_SPECS]
    matrix_chain = [_matrix_row(root, spec) for spec in MATRIX_CHAIN_COVERAGE_SPECS]
    violation_codes = [
        f"contract_coverage_uncovered:{risk}"
        for row in matrix
        for risk in row.uncovered_risks
    ]
    violation_codes.extend(
        f"contract_coverage_uncovered:{risk}"
        for row in matrix_chain
        for risk in row.uncovered_risks
    )
    if not _is_live_repo(root):
        violation_codes.append("contract_coverage_runtime_evidence_unavailable")
    violation_codes.extend(_boundary_errors(root))
    return ContractCoverageAuditReport(matrix, matrix_chain, sorted(set(violation_codes)))


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit agent-future-trading runtime contract coverage.")
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
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
