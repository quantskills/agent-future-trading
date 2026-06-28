from __future__ import annotations

"""Machine-readable agent capability cards.

The cards are governance metadata, not strategy logic. They make explicit
which agents may affect trade authority, execution, settlement, and learning.
"""

from typing import Dict, List

from tools.agent_tools.control.pg_schemas import AgentCapabilityCard, ProtocolCheckResult


def build_default_agent_cards() -> Dict[str, AgentCapabilityCard]:
    cards = [
        AgentCapabilityCard(
            agent_name="technical",
            team="analysis_team",
            reads=["market_data", "technical_context", "past_learning_context"],
            writes=["AnalystSignalArtifact"],
            outputs=["action_evidence_contract", "trade_research_contract"],
            may_call_llm=True,
            required_contract_versions=["agentquant.message.v1", "agentquant.research.v1"],
            failure_mode="degrade_to_watch_for_trigger_or_neutral",
        ),
        AgentCapabilityCard(
            agent_name="fundamental",
            team="analysis_team",
            reads=["finoview_fundamental_data", "pandaai_factor_context", "past_learning_context"],
            writes=["AnalystSignalArtifact"],
            outputs=["action_evidence_contract", "trade_research_contract"],
            may_call_llm=True,
            required_contract_versions=["agentquant.message.v1", "agentquant.research.v1"],
            failure_mode="degrade_to_background_or_no_trade_data_gap",
        ),
        AgentCapabilityCard(
            agent_name="commodity_news",
            team="analysis_team",
            reads=["local_news", "event_context", "past_learning_context"],
            writes=["AnalystSignalArtifact"],
            outputs=["action_evidence_contract", "trade_research_contract"],
            may_call_llm=True,
            required_contract_versions=["agentquant.message.v1", "agentquant.research.v1"],
            failure_mode="degrade_to_noise_or_risk_only",
        ),
        AgentCapabilityCard(
            agent_name="signal_collector",
            team="decision_team",
            reads=["action_evidence_contract", "AnalystSignalArtifact"],
            writes=["SignalCollectionArtifact"],
            outputs=["signal_collection_contract"],
            may_call_llm=False,
            required_contract_versions=["agentquant.signal_collection.v1", "agentquant.message.v1"],
            failure_mode="emit_structured_missing_evidence_without_trade_authority",
        ),
        AgentCapabilityCard(
            agent_name="portfolio_manager",
            team="decision_team",
            reads=[
                "signal_collection_contract",
                "portfolio_state",
                "effective_memory_summary",
                "opportunity_scorecard",
                "position_sizing_result",
            ],
            writes=["PMDecisionArtifact"],
            outputs=["final_action_contract", "learning_used", "opportunity_scorecard", "position_sizing_result"],
            may_call_llm=False,
            may_create_trade_authority=True,
            may_modify_lots_or_margin=True,
            required_contract_versions=["agentquant.snapshot.v2"],
            failure_mode="hold_or_watchlist_with_audit_reason",
        ),
        AgentCapabilityCard(
            agent_name="auditor",
            team="decision_team",
            reads=["final_action_contract", "risk_state", "contract_state"],
            writes=["AuditVerdictArtifact"],
            outputs=["audit_verdict", "hard_risk_reasons"],
            may_call_llm=False,
            required_contract_versions=["agentquant.snapshot.v2"],
            failure_mode="block_or_reduce_on_hard_risk_uncertainty",
        ),
        AgentCapabilityCard(
            agent_name="trader",
            team="execution_team",
            reads=["final_action_contract", "audit_verdict", "intraday_market_data", "portfolio_margin_state"],
            writes=["ExecutionArtifact"],
            outputs=["orders", "forced_risk_operational_recommendation", "execution_learning_event"],
            may_execute_orders=True,
            required_contract_versions=["agentquant.snapshot.v2"],
            failure_mode="skip_execution_with_reason_or_emit_forced_risk_close_only",
        ),
        AgentCapabilityCard(
            agent_name="accountant",
            team="settlement_team",
            reads=["orders", "fills", "settlement_data", "commission_slippage_catalogs"],
            writes=["SettlementArtifact"],
            outputs=["pnl", "fees", "margin", "position_state"],
            may_write_settlement=True,
            failure_mode="stop_on_unreconciled_facts",
        ),
        AgentCapabilityCard(
            agent_name="reviewer",
            team="research_team",
            reads=["settlement_artifacts", "execution_artifacts", "decision_artifacts"],
            writes=["ReviewerAttributionArtifact", "Phase4ValidationArtifact", "ResearchInputMaterial"],
            outputs=["phase4_validation", "transaction_log", "factual_attribution", "research_input_material"],
            may_call_llm=False,
            may_write_future_learning=False,
            failure_mode="write_phase4_failure_or_factual_attribution_gap",
        ),
        AgentCapabilityCard(
            agent_name="researcher",
            team="research_team",
            reads=["reviewer_artifacts", "settled_trade_episodes", "action_outcomes"],
            writes=["alpha_setup_profile", "alpha_setup_action_value", "adaptive_policy_state"],
            outputs=["future_action_preference", "memory_quality_payload"],
            may_call_llm=True,
            may_write_future_learning=True,
            failure_mode="write_candidate_only_until_evidence_is_complete",
        ),
        AgentCapabilityCard(
            agent_name="protocol_governor",
            team="control_team",
            reads=[
                "capability_cards",
                "task_lifecycle",
                "artifact_lineage",
                "memory_quality",
                "preflight_state",
                "cost_events",
                "tool_access_events",
            ],
            writes=["ProtocolGovernanceArtifact", "ToolAccessAuditArtifact"],
            outputs=["protocol_audit", "preflight_health", "lineage_warnings", "cost_report", "cost_warning"],
            may_call_llm=False,
            failure_mode="report_protocol_error_without_changing_trade_decision",
        ),
    ]
    return {card.agent_name: card for card in cards}


def validate_agent_capability(card: AgentCapabilityCard) -> ProtocolCheckResult:
    errors: List[str] = []

    if not card.agent_name:
        errors.append("agent_name_missing")
    if not card.team:
        errors.append("team_missing")
    if card.may_execute_orders and card.may_create_trade_authority:
        errors.append("agent_cannot_both_create_trade_authority_and_execute_orders")
    if card.may_write_settlement and card.may_execute_orders:
        errors.append("agent_cannot_both_execute_orders_and_write_settlement")
    if card.may_write_future_learning and (
        card.may_create_trade_authority or card.may_execute_orders or card.may_write_settlement
    ):
        errors.append("future_learning_agent_cannot_affect_current_trade_or_settlement")
    if card.agent_name == "protocol_governor":
        if card.may_call_llm:
            errors.append("protocol_governor_must_not_call_llm_by_default")
        if card.may_create_trade_authority:
            errors.append("protocol_governor_must_not_create_trade_authority")
        if card.may_modify_lots_or_margin:
            errors.append("protocol_governor_must_not_modify_lots_or_margin")
        if card.may_execute_orders:
            errors.append("protocol_governor_must_not_execute_orders")
        if card.may_write_settlement:
            errors.append("protocol_governor_must_not_write_settlement")
    return ProtocolCheckResult.fail_result(errors) if errors else ProtocolCheckResult.pass_result()
