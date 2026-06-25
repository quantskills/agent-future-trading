from __future__ import annotations

"""Read-only version-level contract coverage audit.

This audit checks whether core AgentQuant contracts have a visible production,
consumption, audit, and test path in the current codebase. It does not inspect
strategy profitability, read trade records, write the database, or modify any
contract.
"""

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Dict, Iterable, List, Mapping, Sequence


CONTRACT_COVERAGE_VERSION = "agentquant.contract_coverage_audit.v1"


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


@dataclass
class ContractCoverageRow:
    contract: str
    producers: List[str] = field(default_factory=list)
    consumers: List[str] = field(default_factory=list)
    audits: List[str] = field(default_factory=list)
    tests: List[str] = field(default_factory=list)
    uncovered_risks: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.uncovered_risks

    def to_dict(self) -> Dict[str, object]:
        return {
            "contract": self.contract,
            "ok": self.ok,
            "producers": list(self.producers),
            "consumers": list(self.consumers),
            "audits": list(self.audits),
            "tests": list(self.tests),
            "uncovered_risks": list(self.uncovered_risks),
        }


@dataclass
class ContractCoverageAuditReport:
    ok: bool
    matrix: List[ContractCoverageRow]
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, object] = field(default_factory=dict)
    agent_name: str = "protocol_governor"
    contract_version: str = CONTRACT_COVERAGE_VERSION

    def to_dict(self) -> Dict[str, object]:
        return {
            "agent_name": self.agent_name,
            "contract_version": self.contract_version,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "matrix": [row.to_dict() for row in self.matrix],
            "metadata": dict(self.metadata),
        }


def _rule(path: str, patterns: Sequence[str], description: str) -> ContractEvidenceRule:
    return ContractEvidenceRule(path=path, patterns=tuple(patterns), description=description)


CONTRACT_SPECS: Sequence[ContractCoverageSpec] = (
    ContractCoverageSpec(
        contract="signal_collection_contract",
        producers=(
            _rule(
                "src/tools/agent_tools/decision/signal_evidence_collection.py",
                ("def build_signal_collection_contract", "collector_decision_boundary"),
                "signal collector builds the PM-facing structured evidence package",
            ),
            _rule(
                "src/agents/decision_team/signal_collector.py",
                ("signal_collection_contract", "build_signal_collection_contract"),
                "decision-team agent publishes the signal collection contract",
            ),
        ),
        consumers=(
            _rule(
                "src/agents/decision_team/portfolio_manager.py",
                ("signal_collection_contract", "build_signal_collection_contract"),
                "PM consumes the signal collection contract before signing final authority",
            ),
        ),
        audits=(
            _rule(
                "docs/mechanism_multiagents.md",
                ("signal_collection_contract", "signal_collector_no_trade_authority"),
                "mechanism document fixes signal collector authority boundaries",
            ),
        ),
        tests=(
            _rule(
                "src/tests/test_decision_workflow_tools.py",
                ("test_signal_collector_preserves_source_evidence_without_trade_authority",),
                "decision workflow tests cover signal collector evidence preservation and no-trade-authority boundary",
            ),
        ),
    ),
    ContractCoverageSpec(
        contract="action_evidence_contract",
        producers=(
            _rule(
                "src/tools/agent_tools/analysis/quality.py",
                ("def _build_action_evidence_contract", "apply_trade_research_contract"),
                "analysis quality builds canonical analyst action evidence",
            ),
            _rule(
                "src/tools/common/contracts.py",
                ("build_trade_research_contract", "action_evidence_contract"),
                "trade research contract carries the same action evidence",
            ),
        ),
        consumers=(
            _rule(
                "src/agents/decision_team/portfolio_manager.py",
                ("_canonical_action_evidence_contract",),
                "PM consumes canonical analyst evidence",
            ),
            _rule(
                "src/tools/agent_tools/analysis/signal_fusion.py",
                ("_contract_from_signal", "_signal_bool", "_signal_text"),
                "signal fusion reads the contract before raw signal fields",
            ),
        ),
        audits=(
            _rule(
                "src/tools/agent_tools/control/system_invariants.py",
                ("_audit_action_evidence_trigger_consistency", "action_evidence_contract_pending_trigger_marked_valid"),
                "daily audit checks trigger/evidence consistency",
            ),
        ),
        tests=(
            _rule(
                "src/tests/test_agent_contracts.py",
                ("action_evidence_contract", "trigger_valid"),
                "agent contract tests cover analyst evidence",
            ),
            _rule(
                "src/tests/test_phase_flow_regression.py",
                ("test_analyst_to_pm_action_evidence_contract_overrides_raw_signal_state",),
                "phase flow tests prove Analyst-to-PM evidence contract fields override raw signal drift",
            ),
            _rule(
                "src/tests/test_system_invariant_audit.py",
                ("action_evidence_contract_pending_trigger_marked_valid",),
                "system audit tests cover evidence contradictions",
            ),
        ),
    ),
    ContractCoverageSpec(
        contract="final_action_contract",
        producers=(
            _rule(
                "src/agents/decision_team/portfolio_manager.py",
                ("def _build_final_action_contract", "def _build_minimal_final_action_contract"),
                "PM is the only strategy final contract producer",
            ),
        ),
        consumers=(
            _rule(
                "src/agents/execution_team/trader.py",
                ("_final_action_contract_from_snapshot", "missing_final_action_contract"),
                "Trader executes only the final contract",
            ),
            _rule(
                "src/tools/agent_tools/research/phase4_review.py",
                ("final_action_contract", "learning_source"),
                "Reviewer binds learning to the final contract",
            ),
        ),
        audits=(
            _rule(
                "src/tools/agent_tools/control/system_invariants.py",
                ("recommendation_final_action_contract", "transaction_not_derived_from_final_action_contract"),
                "daily audit checks the single trade truth",
            ),
        ),
        tests=(
            _rule(
                "src/tests/test_phase_flow_regression.py",
                ("test_final_action_contract_is_single_structured_trade_truth",),
                "phase flow tests cover final contract construction",
            ),
            _rule(
                "src/tests/test_phase_flow_regression.py",
                ("test_pm_to_trader_final_contract_preserves_authority_without_rank_execution_rights",),
                "phase flow tests prove PM-to-Trader contract transfer preserves target authority without rank execution rights",
            ),
            _rule(
                "src/tests/test_system_invariant_audit.py",
                ("recommendation_top_level_action_lots_mismatch_final_action_contract",),
                "system audit tests lock top-level recommendation parity",
            ),
        ),
    ),
    ContractCoverageSpec(
        contract="alpha_setup_action_value",
        producers=(
            _rule(
                "src/tools/agent_tools/research/alpha_setup.py",
                ("upsert_alpha_setup_sample_and_profile", "INSERT INTO alpha_setup_action_value"),
                "Researcher writes structured action-value rows",
            ),
            _rule(
                "src/database/sqlite_setup.py",
                ("CREATE TABLE IF NOT EXISTS alpha_setup_action_value", "consumer_scope"),
                "SQLite schema carries canonical action-value columns",
            ),
        ),
        consumers=(
            _rule(
                "src/database/sqlite_helper.py",
                ("def get_alpha_setup_action_values", "consumer_scope"),
                "DB reader filters and orders action-value rows",
            ),
            _rule(
                "src/agents/decision_team/portfolio_manager.py",
                ("_normalize_alpha_setup_action_value", "_is_pm_learning_action_value"),
                "PM normalizes and consumes only PM-scoped learning",
            ),
        ),
        audits=(
            _rule(
                "src/tools/agent_tools/control/system_invariants.py",
                ("pm_learning_components_zero_despite_prior_real_action_value", "pm_consumed_non_pm_learning_action_value"),
                "daily audit checks action-value landing into PM scoring",
            ),
            _rule(
                "src/tools/agent_tools/control/mechanism_effectiveness_audit.py",
                ("mechanism_pm_learning_not_in_score",),
                "mechanism audit checks PM learning-to-score connectivity",
            ),
        ),
        tests=(
            _rule(
                "src/tests/test_phase_flow_regression.py",
                ("test_direct_alpha_setup_action_value_prioritizes_real_action_preference",),
                "phase flow tests cover action-value retrieval priority",
            ),
            _rule(
                "src/tests/test_phase_flow_regression.py",
                (
                    "test_pm_action_value_merge_preserves_canonical_researcher_record",
                    "test_pm_action_value_retrieval_real_history_not_blocked_by_empty_lane",
                    "_append_unique_action_values",
                    "_select_learning_trace_action_values",
                ),
                "phase flow tests prove Researcher-to-PM action-value fields survive merge, retrieval, and trace compaction",
            ),
            _rule(
                "src/tests/test_reviewer_learning.py",
                ("alpha_setup_action_value", "action_preference"),
                "reviewer/researcher tests cover action-value writes",
            ),
        ),
    ),
    ContractCoverageSpec(
        contract="effective_memory_summary",
        producers=(
            _rule(
                "src/tools/agent_tools/decision/decision_memory_retrieval.py",
                ("def retrieve_pm_memory", "empty_history_cannot_block_real_history"),
                "memory retrieval tool returns quality-first PM memory summary",
            ),
        ),
        consumers=(
            _rule(
                "src/agents/decision_team/portfolio_manager.py",
                ("retrieve_pm_memory", "effective_memory_summary"),
                "PM reads decision memory only through the retrieval tool output",
            ),
        ),
        audits=(
            _rule(
                "docs/mechanism_multiagents.md",
                ("decision_memory_retrieval", "empty_history_cannot_block_real_history"),
                "mechanism document fixes memory retrieval boundary",
            ),
        ),
        tests=(
            _rule(
                "src/tests/test_decision_workflow_tools.py",
                ("test_memory_retrieval_real_history_not_blocked_by_empty_history",),
                "decision workflow tests lock empty-history cannot block real profitable history",
            ),
        ),
    ),
    ContractCoverageSpec(
        contract="execution_learning_trace",
        producers=(
            _rule(
                "src/util/futures_audit.py",
                ("def build_execution_learning_trace", "consumer_scope"),
                "single builder creates execution learning traces",
            ),
            _rule(
                "src/tools/agent_tools/execution/futures_execution.py",
                ("build_execution_learning_trace",),
                "execution helper uses the trace builder",
            ),
            _rule(
                "src/agents/execution_team/trader.py",
                ("build_execution_learning_trace",),
                "Trader execution paths use the trace builder",
            ),
        ),
        consumers=(
            _rule(
                "src/tools/agent_tools/research/phase4_review.py",
                ("execution_learning_trace", "build_execution_learning_trace"),
                "Reviewer preserves or builds execution trace for Researcher",
            ),
        ),
        audits=(
            _rule(
                "src/tools/agent_tools/control/system_invariants.py",
                ("trader_execution_learning_trace_missing_scope", "trader_execution_learning_trace_wrong_scope"),
                "daily audit rejects bare execution learning traces",
            ),
        ),
        tests=(
            _rule(
                "src/tests/test_system_invariant_audit.py",
                ("test_system_invariant_audit_rejects_bare_execution_learning_trace",),
                "system audit test catches bare execution learning trace",
            ),
            _rule(
                "src/tests/test_phase_flow_regression.py",
                ("test_trader_to_researcher_execution_result_preserves_no_trade_fact_and_learning_scope",),
                "phase flow tests prove Trader-to-Researcher execution learning scope and no-trade fact survive",
            ),
            _rule(
                "src/tests/test_phase_flow_regression.py",
                ("consumer_scope", "trader_execution_learning"),
                "phase flow test covers hold/zero-lots execution trace path",
            ),
        ),
    ),
    ContractCoverageSpec(
        contract="opportunity_score_components",
        producers=(
            _rule(
                "src/tools/agent_tools/analysis/signal_fusion.py",
                ("opportunity_score_components", "positive_learning"),
                "signal fusion builds PM score components",
            ),
            _rule(
                "src/agents/decision_team/portfolio_manager.py",
                ("opportunity_score_components",),
                "PM carries score components into final contract evidence",
            ),
        ),
        consumers=(
            _rule(
                "src/graph/workflow.py",
                ("opportunity_rank", "_apply_daily_capital_deployment"),
                "workflow uses scores/ranks in capital deployment pass",
            ),
            _rule(
                "src/evaluation/analyze_strategy_attribution.py",
                ("by_opportunity_learning_component", "opportunity_score_components"),
                "evaluation reads learning component attribution",
            ),
        ),
        audits=(
            _rule(
                "src/tools/agent_tools/control/system_invariants.py",
                ("learning_components_only_inside_opportunity_score_components",),
                "daily audit keeps learning components diagnostic only",
            ),
        ),
        tests=(
            _rule(
                "src/tests/test_phase_flow_regression.py",
                ("positive_learning", "negative_learning", "execution_profile_learning"),
                "phase flow tests cover learning components",
            ),
            _rule(
                "src/tests/test_strategy_attribution_report.py",
                ("by_opportunity_learning_component",),
                "evaluation tests cover learning component attribution",
            ),
        ),
    ),
    ContractCoverageSpec(
        contract="opportunity_scorecard",
        producers=(
            _rule(
                "src/tools/agent_tools/decision/opportunity_ranking.py",
                ("def rank_opportunities", "opportunity_scorecard"),
                "opportunity ranking tool wraps reproducible scorecard and rank output",
            ),
        ),
        consumers=(
            _rule(
                "src/agents/decision_team/portfolio_manager.py",
                ("rank_opportunities", "opportunity_ranking"),
                "PM consumes ranking tool output before sizing",
            ),
        ),
        audits=(
            _rule(
                "docs/mechanism_multiagents.md",
                ("opportunity_ranking", "rank_is_not_trade_authority"),
                "mechanism document fixes ranking tool boundary",
            ),
        ),
        tests=(
            _rule(
                "src/tests/test_decision_workflow_tools.py",
                ("test_opportunity_ranking_ranks_without_trade_authority",),
                "decision workflow tests cover deterministic rank without trade authority",
            ),
        ),
    ),
    ContractCoverageSpec(
        contract="position_sizing_result",
        producers=(
            _rule(
                "src/tools/agent_tools/decision/position_sizing.py",
                ("def build_position_sizing_result", "no_final_action_authority"),
                "position sizing tool records deterministic sizing math",
            ),
        ),
        consumers=(
            _rule(
                "src/agents/decision_team/portfolio_manager.py",
                ("build_position_sizing_result", "position_sizing_result"),
                "PM consumes sizing tool output and then signs the unique contract",
            ),
        ),
        audits=(
            _rule(
                "docs/mechanism_multiagents.md",
                ("position_sizing", "no_final_action_authority"),
                "mechanism document fixes sizing tool no-authority boundary",
            ),
        ),
        tests=(
            _rule(
                "src/tests/test_decision_workflow_tools.py",
                ("test_position_sizing_records_math_without_final_action_authority",),
                "decision workflow tests cover sizing result boundary",
            ),
        ),
    ),
    ContractCoverageSpec(
        contract="learning_used",
        producers=(
            _rule(
                "src/agents/decision_team/portfolio_manager.py",
                ('"learning_used"', "_select_learning_trace_action_values"),
                "PM writes learning_used into the final contract",
            ),
        ),
        consumers=(
            _rule(
                "src/tools/agent_tools/research/phase4_review.py",
                ("learning_used", "final_action_contract"),
                "Reviewer uses final-contract learning diagnostics",
            ),
            _rule(
                "src/tools/agent_tools/control/mechanism_effectiveness_audit.py",
                ("learning_used", "learning_adjustment_summary"),
                "mechanism audit checks learning impact",
            ),
        ),
        audits=(
            _rule(
                "src/tools/agent_tools/control/system_invariants.py",
                ("pm_learning_signal_without_contract_effect_or_explanation",),
                "daily audit checks learning signals affect or explain the contract",
            ),
        ),
        tests=(
            _rule(
                "src/tests/test_system_invariant_audit.py",
                ("pm_learning_signal_without_contract_effect_or_explanation",),
                "system audit tests cover learning-used contract effect",
            ),
        ),
    ),
    ContractCoverageSpec(
        contract="execution_result",
        producers=(
            _rule(
                "src/util/futures_audit.py",
                ("def ensure_execution_result", "def set_execution_result"),
                "shared execution result helpers write execution output",
            ),
            _rule(
                "src/tools/agent_tools/execution/futures_execution.py",
                ("set_execution_result",),
                "execution helper writes execution result through helper",
            ),
            _rule(
                "src/agents/execution_team/trader.py",
                ("set_execution_result", "ensure_execution_result"),
                "Trader writes execution results through helpers",
            ),
        ),
        consumers=(
            _rule(
                "src/tools/agent_tools/research/research_learning.py",
                ("execution_result", "final_action_contract"),
                "Researcher reads execution result for episode learning",
            ),
            _rule(
                "src/tools/agent_tools/research/phase4_review.py",
                ("_execution_result_from_snapshot",),
                "Reviewer normalizes execution result for review",
            ),
        ),
        audits=(
            _rule(
                "src/tools/agent_tools/control/system_invariants.py",
                ("transaction_not_derived_from_final_action_contract", "trader_execution_learning_trace_missing_scope"),
                "daily audit checks execution result and transaction lineage",
            ),
        ),
        tests=(
            _rule(
                "src/tests/test_phase_flow_regression.py",
                ("test_hold_or_zero_lots_recommendation_is_skipped_not_executed",),
                "phase flow tests cover non-executed result path",
            ),
            _rule(
                "src/tests/test_phase_flow_regression.py",
                ("test_trader_to_researcher_execution_result_preserves_no_trade_fact_and_learning_scope",),
                "phase flow tests cover Trader-to-Reviewer/Researcher execution result fidelity",
            ),
            _rule(
                "src/tests/test_system_invariant_audit.py",
                ("test_system_invariant_audit_accepts_execution_result_without_learning_trace",),
                "system audit tests avoid false positives on plain execution results",
            ),
        ),
    ),
)


ACTIVE_DOC_PATHS = (
    "AGENTS.md",
    "README.md",
    "src/llm/prompt.py",
    "src/config/dev.yaml",
    "src/config/learning_policy_catalog.yaml",
    "src/config/portfolio_policy_catalog.yaml",
    "docs/mechanism_data_model.md",
    "docs/mechanism_future_trade.md",
    "docs/mechanism_multiagents.md",
    "docs/mechanism_research.md",
)


def _read_text(repo_root: Path, relative_path: str) -> str:
    path = repo_root / relative_path
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return ""


def _rule_matched(repo_root: Path, rule: ContractEvidenceRule) -> bool:
    text = _read_text(repo_root, rule.path)
    return bool(text) and all(pattern in text for pattern in rule.patterns)


def _evidence(rule: ContractEvidenceRule) -> str:
    return f"{rule.path}: {rule.description}"


def _row_for_spec(repo_root: Path, spec: ContractCoverageSpec) -> ContractCoverageRow:
    row = ContractCoverageRow(contract=spec.contract)
    sections = {
        "producer": (spec.producers, row.producers),
        "consumer": (spec.consumers, row.consumers),
        "audit": (spec.audits, row.audits),
        "test": (spec.tests, row.tests),
    }
    for section, (rules, evidence_list) in sections.items():
        for rule in rules:
            if _rule_matched(repo_root, rule):
                evidence_list.append(_evidence(rule))
        if not evidence_list:
            row.uncovered_risks.append(f"{spec.contract}_missing_{section}_coverage")
    return row


def _iter_python_files(repo_root: Path) -> Iterable[Path]:
    src_root = repo_root / "src"
    if not src_root.exists():
        return []
    excluded = {
        "src/tools/agent_tools/control/contract_coverage_audit.py",
        "src/run/control/contract_coverage_audit.py",
    }
    candidates: List[Path] = []
    for path in src_root.rglob("*.py"):
        rel = path.relative_to(repo_root).as_posix()
        if "src/tests" in rel or rel in excluded or "__pycache__" in path.parts:
            continue
        candidates.append(path)
    return candidates


def _line_matches_any(line: str, patterns: Sequence[str]) -> bool:
    return any(re.search(pattern, line) for pattern in patterns)


def _scan_bare_writes(repo_root: Path) -> List[str]:
    errors: List[str] = []
    bare_patterns: Mapping[str, Sequence[str]] = {
        "execution_learning_trace": (
            r"\[[\"']execution_learning_trace[\"']\]\s*=\s*\{",
            r"[\"']execution_learning_trace[\"']\s*:\s*\{",
        ),
        "final_action_contract": (
            r"\[[\"']final_action_contract[\"']\]\s*=\s*\{",
            r"\bfinal_action_contract\s*=\s*\{",
        ),
        "action_evidence_contract": (
            r"\[[\"']action_evidence_contract[\"']\]\s*=\s*\{",
        ),
        "execution_result": (
            r"\[[\"']execution_result[\"']\]\s*=\s*\{",
        ),
    }
    allowed_by_contract = {
        "execution_learning_trace": {
            "src/util/futures_audit.py",
            "src/agents/execution_team/trader.py",
            "src/tools/agent_tools/execution/futures_execution.py",
            "src/tools/agent_tools/research/phase4_review.py",
        },
        "final_action_contract": {
            "src/agents/decision_team/portfolio_manager.py",
        },
        "action_evidence_contract": {
            "src/tools/agent_tools/analysis/quality.py",
            "src/tools/common/contracts.py",
        },
        "execution_result": {
            "src/util/futures_audit.py",
        },
    }
    for path in _iter_python_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, 1):
            for contract, patterns in bare_patterns.items():
                if rel in allowed_by_contract.get(contract, set()):
                    continue
                if _line_matches_any(line, patterns):
                    errors.append(f"bare_contract_write:{contract}:{rel}:{line_no}")
    return errors


def _scan_alpha_setup_action_value_insert_paths(repo_root: Path) -> List[str]:
    errors: List[str] = []
    allowed = {
        "src/tools/agent_tools/research/alpha_setup.py",
        "src/database/sqlite_setup.py",
    }
    pattern = "INSERT INTO alpha_setup_action_value"
    for path in _iter_python_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        if rel in allowed:
            continue
        text = _read_text(repo_root, rel)
        if pattern in text:
            errors.append(f"unexpected_alpha_setup_action_value_writer:{rel}")
    return errors


def _scan_scope_boundaries(repo_root: Path) -> List[str]:
    errors: List[str] = []
    trader_paths = (
        "src/agents/execution_team/trader.py",
        "src/tools/agent_tools/execution/futures_execution.py",
        "src/tools/agent_tools/execution/intraday_execution.py",
    )
    for rel in trader_paths:
        text = _read_text(repo_root, rel)
        if "pm_learning" in text:
            errors.append(f"trader_scope_reads_pm_learning:{rel}")
    pm_text = _read_text(repo_root, "src/agents/decision_team/portfolio_manager.py")
    memory_tool_text = _read_text(repo_root, "src/tools/agent_tools/decision/decision_memory_retrieval.py")
    if "retrieve_pm_memory" not in pm_text:
        errors.append("pm_missing_decision_memory_retrieval")
    if "consumer_scope=\"pm_learning\"" not in memory_tool_text and 'consumer_scope="pm_learning"' not in memory_tool_text:
        errors.append("decision_memory_retrieval_missing_pm_learning_scope")
    if "_consumer_scope" not in memory_tool_text or "non_pm_learning_scope" not in memory_tool_text:
        errors.append("decision_memory_retrieval_missing_non_pm_learning_filter")
    return errors


def _scan_active_docs_for_old_trade_contract_terms(repo_root: Path) -> List[str]:
    errors: List[str] = []
    forbidden = (
        "pre_open_plan_json",
        "target_lots_estimate",
        "final_new_entry_trade_authority",
    )
    for rel in ACTIVE_DOC_PATHS:
        text = _read_text(repo_root, rel)
        for token in forbidden:
            if token in text:
                errors.append(f"active_doc_or_config_old_contract_term:{rel}:{token}")
    return errors


def _scan_field_table(repo_root: Path) -> List[str]:
    text = _read_text(repo_root, "docs/unified_field_semantics.md")
    errors: List[str] = []
    for spec in CONTRACT_SPECS:
        if spec.contract not in text:
            errors.append(f"field_table_missing_contract:{spec.contract}")
    return errors


def _scan_config_prompt_alignment(repo_root: Path) -> List[str]:
    errors: List[str] = []
    checks = {
        "src/config/learning_policy_catalog.yaml": (
            "learning_consumer_scopes",
            "pm_allowed_consumer_scope",
            "trader_allowed_consumer_scope",
        ),
        "src/config/portfolio_policy_catalog.yaml": (
            "consumer_scope=pm_learning",
            "trader_execution_learning",
        ),
        "src/llm/prompt.py": (
            "consumer_scope=pm_learning",
            "consumer_scope=trader_execution_learning",
            "consumer_scope=analyst_calibration",
        ),
    }
    for rel, patterns in checks.items():
        text = _read_text(repo_root, rel)
        for pattern in patterns:
            if pattern not in text:
                errors.append(f"config_prompt_contract_boundary_missing:{rel}:{pattern}")
    return errors


def audit_contract_coverage(repo_root: str | Path) -> ContractCoverageAuditReport:
    repo_root = Path(repo_root).resolve()
    matrix = [_row_for_spec(repo_root, spec) for spec in CONTRACT_SPECS]
    errors: List[str] = []
    warnings: List[str] = []
    for row in matrix:
        for risk in row.uncovered_risks:
            errors.append(f"contract_coverage_uncovered:{risk}")
    errors.extend(_scan_field_table(repo_root))
    errors.extend(_scan_bare_writes(repo_root))
    errors.extend(_scan_alpha_setup_action_value_insert_paths(repo_root))
    errors.extend(_scan_scope_boundaries(repo_root))
    errors.extend(_scan_active_docs_for_old_trade_contract_terms(repo_root))
    errors.extend(_scan_config_prompt_alignment(repo_root))

    metadata: Dict[str, object] = {
        "repo_root": str(repo_root),
        "audit_boundary": (
            "version_level_static_contract_coverage_only; read_only; "
            "does_not_read_trade_records_or_modify_strategy"
        ),
        "contracts_checked": [spec.contract for spec in CONTRACT_SPECS],
        "checked_dimensions": [
            "producer_coverage",
            "consumer_coverage",
            "audit_coverage",
            "test_coverage",
            "producer_consumer_fidelity_tests",
            "bare_contract_writes",
            "consumer_scope_boundaries",
            "field_table_registration",
            "config_prompt_doc_alignment",
        ],
    }
    return ContractCoverageAuditReport(
        ok=not errors,
        matrix=matrix,
        errors=errors,
        warnings=warnings,
        metadata=metadata,
    )


def dumps_contract_coverage_report(report: ContractCoverageAuditReport) -> str:
    import json

    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
