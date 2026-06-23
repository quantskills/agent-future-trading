from __future__ import annotations

"""Protocol Governor for AgentQuant.

The protocol governor is a deterministic control-team sidecar. It is not a
portfolio manager, auditor, trader, researcher, or new gate. Its job is to make
agent boundaries, task lifecycle, artifact lineage, memory quality, and
action-preference landing auditable before expensive backtests or simulation.
"""

from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from tools.agent_tools.control.action_preference_audit import audit_action_preference_landing
from tools.agent_tools.control.agent_cards import build_default_agent_cards, validate_agent_capability
from tools.agent_tools.control.artifact_lineage import (
    build_protocol_artifact_header,
    validate_protocol_artifact,
)
from tools.agent_tools.control.cost_budget_audit import CostBudgetLimits, audit_cost_budget
from tools.agent_tools.control.exploration_audit import classify_exploration_intent, summarize_exploration_intents
from tools.agent_tools.control.memory_quality import classify_memory_payload
from tools.agent_tools.control.mechanism_effectiveness_audit import audit_mechanism_effectiveness
from tools.agent_tools.control.parity import compare_contract_interpretation
from tools.agent_tools.control.pre_backtest_acceptance import run_pre_backtest_acceptance
from tools.agent_tools.control.preflight import run_preflight_checks
from tools.agent_tools.control.schemas import AgentCapabilityCard, ProtocolCheckResult, TaskLifecycleEvent
from tools.agent_tools.control.system_invariants import audit_system_invariants
from tools.agent_tools.control.task_lifecycle import create_lifecycle_event, validate_lifecycle_sequence
from tools.agent_tools.control.tool_access_policy import (
    ToolAccessPolicy,
    audit_tool_access,
    build_default_tool_access_policy,
    validate_tool_policy_against_capabilities,
)


class ProtocolGovernor:
    agent_name = "protocol_governor"
    contract_version = "agentquant.protocol_governor.v1"

    def __init__(self, capability_cards: Optional[Dict[str, AgentCapabilityCard]] = None):
        self._capability_cards = capability_cards or build_default_agent_cards()

    @property
    def capability_cards(self) -> Dict[str, AgentCapabilityCard]:
        return dict(self._capability_cards)

    def validate_capability_cards(self) -> ProtocolCheckResult:
        result = ProtocolCheckResult.pass_result(metadata={"agent_count": len(self._capability_cards)})
        for name, card in self._capability_cards.items():
            result = result.merge(validate_agent_capability(card))
            if name != card.agent_name:
                result = result.merge(ProtocolCheckResult.fail_result([f"capability_card_key_mismatch:{name}"]))
        return result

    def validate_tool_policy(self, policy: Optional[ToolAccessPolicy] = None) -> ProtocolCheckResult:
        return validate_tool_policy_against_capabilities(self._capability_cards, policy=policy)

    def audit_tool_access(
        self,
        events: Iterable[Dict[str, Any]],
        *,
        policy: Optional[ToolAccessPolicy] = None,
    ) -> ProtocolCheckResult:
        return audit_tool_access(events, policy=policy or build_default_tool_access_policy())

    def create_task_event(
        self,
        *,
        trading_date: Any,
        ticker: str,
        config_id: str,
        phase: str,
        agent_name: str,
        input_artifacts: Optional[Iterable[str]] = None,
        output_artifacts: Optional[Iterable[str]] = None,
        status: str = "ok",
        reasons: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TaskLifecycleEvent:
        return create_lifecycle_event(
            trading_date=trading_date,
            ticker=ticker,
            config_id=config_id,
            phase=phase,
            agent_name=agent_name,
            input_artifacts=input_artifacts,
            output_artifacts=output_artifacts,
            status=status,
            reasons=reasons,
            metadata=metadata,
        )

    def validate_task_lifecycle(self, events: Iterable[TaskLifecycleEvent]) -> ProtocolCheckResult:
        return validate_lifecycle_sequence(list(events))

    def build_lineage_header(self, **kwargs: Any) -> Dict[str, Any]:
        return build_protocol_artifact_header(**kwargs)

    def validate_artifact_lineage(self, artifact: Dict[str, Any]) -> ProtocolCheckResult:
        errors = validate_protocol_artifact(artifact)
        return ProtocolCheckResult.fail_result(errors) if errors else ProtocolCheckResult.pass_result()

    def classify_memory_quality(self, memory_payload: Dict[str, Any]) -> Dict[str, Any]:
        return classify_memory_payload(memory_payload)

    def audit_action_preference_landing(
        self,
        *,
        research_preferences: Iterable[Dict[str, Any]],
        pm_snapshot: Dict[str, Any],
        trader_snapshot: Optional[Dict[str, Any]] = None,
        settlement_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return audit_action_preference_landing(
            research_preferences=research_preferences,
            pm_snapshot=pm_snapshot,
            trader_snapshot=trader_snapshot,
            settlement_snapshot=settlement_snapshot,
        )

    def run_preflight(
        self,
        *,
        repo_root: Optional[Path] = None,
        sqlite_paths: Optional[Iterable[Path]] = None,
        writable_dirs: Optional[Iterable[Path]] = None,
        required_files: Optional[Iterable[Path]] = None,
        deepfund_python: Optional[Path] = None,
        llm_config: Optional[Dict[str, Any]] = None,
        check_llm_auth: bool = False,
    ) -> ProtocolCheckResult:
        return run_preflight_checks(
            repo_root=repo_root,
            sqlite_paths=sqlite_paths,
            writable_dirs=writable_dirs,
            required_files=required_files,
            deepfund_python=deepfund_python,
            llm_config=llm_config,
            check_llm_auth=check_llm_auth,
        )

    def compare_backtest_simulation_parity(
        self,
        *,
        backtest_contract: Dict[str, Any],
        simulation_contract: Dict[str, Any],
    ) -> ProtocolCheckResult:
        return compare_contract_interpretation(backtest_contract, simulation_contract)

    def classify_exploration(self, final_action_contract: Dict[str, Any]) -> str:
        return classify_exploration_intent(final_action_contract)

    def summarize_exploration(self, contracts: Iterable[Dict[str, Any]]) -> Dict[str, int]:
        return summarize_exploration_intents(contracts)

    def audit_cost_budget(
        self,
        events: Iterable[Dict[str, Any]],
        *,
        limits: Optional[CostBudgetLimits] = None,
    ) -> Dict[str, Any]:
        return audit_cost_budget(events, limits=limits).to_dict()

    def audit_system_invariants(
        self,
        *,
        db_path: str | Path,
        config_id: Optional[str] = None,
        exp_name: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> ProtocolCheckResult:
        return audit_system_invariants(
            db_path=db_path,
            config_id=config_id,
            exp_name=exp_name,
            start_date=start_date,
            end_date=end_date,
        ).to_protocol_result()

    def audit_mechanism_effectiveness(
        self,
        *,
        db_path: str | Path,
        config_id: Optional[str] = None,
        exp_name: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> ProtocolCheckResult:
        return audit_mechanism_effectiveness(
            db_path=db_path,
            config_id=config_id,
            exp_name=exp_name,
            start_date=start_date,
            end_date=end_date,
        ).to_protocol_result()

    def run_pre_backtest_acceptance(
        self,
        *,
        config_path: str | Path,
        db_path: str | Path,
        repo_root: str | Path,
        assets_dir: str | Path,
        deepfund_python: str | Path,
        config_id: Optional[str] = None,
        exp_name: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        check_llm_auth: bool = False,
    ) -> Dict[str, Any]:
        return run_pre_backtest_acceptance(
            config_path=config_path,
            db_path=db_path,
            repo_root=repo_root,
            assets_dir=assets_dir,
            deepfund_python=deepfund_python,
            config_id=config_id,
            exp_name=exp_name,
            start_date=start_date,
            end_date=end_date,
            check_llm_auth=check_llm_auth,
        ).to_dict()


def build_protocol_governor() -> ProtocolGovernor:
    return ProtocolGovernor()


def protocol_governor_agent(**kwargs: Any) -> Dict[str, Any]:
    """Return a compact governance report for orchestration code.

    The function is intentionally observational: it returns checks and never
    mutates trade recommendations or execution plans.
    """

    governor = build_protocol_governor()
    report = {"agent_name": governor.agent_name, "contract_version": governor.contract_version}
    report["capability_cards"] = {k: v.to_dict() for k, v in governor.capability_cards.items()}
    report["capability_validation"] = governor.validate_capability_cards().to_dict()
    report["tool_policy_validation"] = governor.validate_tool_policy().to_dict()
    if kwargs.get("memory_payload") is not None:
        report["memory_quality"] = governor.classify_memory_quality(kwargs["memory_payload"])
    if kwargs.get("contracts") is not None:
        report["exploration_summary"] = governor.summarize_exploration(kwargs["contracts"])
    if kwargs.get("cost_events") is not None:
        report["cost_budget_audit"] = governor.audit_cost_budget(kwargs["cost_events"])
    if kwargs.get("tool_events") is not None:
        report["tool_access_audit"] = governor.audit_tool_access(kwargs["tool_events"]).to_dict()
    return report
