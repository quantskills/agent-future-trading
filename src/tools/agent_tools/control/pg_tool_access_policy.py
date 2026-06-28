from __future__ import annotations

"""Agent-to-tool access policy for protocol governance.

The policy is an audit contract. It does not dispatch tools, block trades,
or change strategy outputs. Its purpose is to detect agent/tool drift such as
Trader calling decision tools or Researcher writing current-day trade actions.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

from tools.agent_tools.control.pg_schemas import ProtocolCheckResult


TOOL_NAMESPACE_BY_PREFIX = {
    "tools.agent_tools.analysis": "analysis",
    "tools.agent_tools.decision": "decision",
    "tools.agent_tools.execution": "execution",
    "tools.agent_tools.research": "research",
    "tools.agent_tools.control": "control",
    "apis": "data",
    "tools.data_fetch": "data",
    "database": "database",
    "llm": "llm",
}


@dataclass(frozen=True)
class ToolAccessPolicy:
    agent_allowed_namespaces: Dict[str, List[str]] = field(default_factory=dict)
    namespace_by_prefix: Dict[str, str] = field(default_factory=lambda: dict(TOOL_NAMESPACE_BY_PREFIX))

    def allowed_namespaces(self, agent_name: str) -> List[str]:
        return list(self.agent_allowed_namespaces.get(str(agent_name or ""), []))


def build_default_tool_access_policy() -> ToolAccessPolicy:
    return ToolAccessPolicy(
        agent_allowed_namespaces={
            "technical": ["analysis", "data", "database", "llm"],
            "fundamental": ["analysis", "data", "database", "llm"],
            "commodity_news": ["analysis", "data", "database", "llm"],
            "signal_collector": ["decision"],
            "portfolio_manager": ["analysis", "decision", "database"],
            "auditor": ["decision", "execution", "database"],
            "trader": ["execution", "database"],
            "accountant": ["execution", "database"],
            "reviewer": ["research", "database"],
            "researcher": ["research", "analysis", "database", "llm"],
            "protocol_governor": ["control", "database"],
        }
    )


def classify_tool_namespace(tool_ref: str, policy: Optional[ToolAccessPolicy] = None) -> str:
    policy = policy or build_default_tool_access_policy()
    text = str(tool_ref or "")
    for prefix, namespace in sorted(policy.namespace_by_prefix.items(), key=lambda item: len(item[0]), reverse=True):
        if text == prefix or text.startswith(f"{prefix}."):
            return namespace
    return "unknown"


def audit_tool_access(
    events: Iterable[Mapping[str, Any]],
    *,
    policy: Optional[ToolAccessPolicy] = None,
) -> ProtocolCheckResult:
    policy = policy or build_default_tool_access_policy()
    errors: List[str] = []
    warnings: List[str] = []
    metadata: Dict[str, Any] = {"checked_calls": 0, "by_agent": {}}

    for raw_event in events or []:
        if not isinstance(raw_event, Mapping):
            warnings.append("tool_access_event_not_mapping")
            continue
        agent_name = str(raw_event.get("agent_name") or raw_event.get("agent") or "")
        tool_ref = str(raw_event.get("tool") or raw_event.get("tool_ref") or raw_event.get("function") or "")
        namespace = str(raw_event.get("namespace") or classify_tool_namespace(tool_ref, policy))
        metadata["checked_calls"] += 1
        metadata["by_agent"].setdefault(agent_name or "unknown", {})
        agent_bucket = metadata["by_agent"][agent_name or "unknown"]
        agent_bucket[namespace] = int(agent_bucket.get(namespace, 0)) + 1

        if not agent_name:
            errors.append("tool_access_agent_missing")
            continue
        if not tool_ref and namespace == "unknown":
            errors.append(f"tool_access_tool_missing:{agent_name}")
            continue

        allowed = policy.allowed_namespaces(agent_name)
        if not allowed:
            errors.append(f"tool_access_agent_without_policy:{agent_name}")
            continue
        if namespace == "unknown":
            warnings.append(f"tool_access_unknown_namespace:{agent_name}:{tool_ref}")
            continue
        if namespace not in allowed:
            errors.append(f"tool_access_denied:{agent_name}:{namespace}:{tool_ref}")

    return ProtocolCheckResult.fail_result(errors, warnings=warnings, metadata=metadata) if errors else ProtocolCheckResult.pass_result(
        warnings=warnings,
        metadata=metadata,
    )


def validate_tool_policy_against_capabilities(
    capability_cards: Mapping[str, Any],
    *,
    policy: Optional[ToolAccessPolicy] = None,
) -> ProtocolCheckResult:
    policy = policy or build_default_tool_access_policy()
    errors: List[str] = []
    warnings: List[str] = []

    for agent_name, card in capability_cards.items():
        allowed = set(policy.allowed_namespaces(agent_name))
        if not allowed:
            errors.append(f"missing_tool_policy_for_agent:{agent_name}")
            continue
        may_call_llm = bool(getattr(card, "may_call_llm", False))
        if "llm" in allowed and not may_call_llm:
            errors.append(f"tool_policy_llm_not_allowed_by_capability:{agent_name}")
        if bool(getattr(card, "may_execute_orders", False)) and "execution" not in allowed:
            errors.append(f"tool_policy_execution_agent_missing_execution_namespace:{agent_name}")
        if bool(getattr(card, "may_write_future_learning", False)) and "research" not in allowed:
            errors.append(f"tool_policy_learning_agent_missing_research_namespace:{agent_name}")
        if bool(getattr(card, "may_create_trade_authority", False)) and "decision" not in allowed:
            errors.append(f"tool_policy_trade_authority_agent_missing_decision_namespace:{agent_name}")
        if agent_name == "protocol_governor" and allowed - {"control", "database"}:
            errors.append("protocol_governor_tool_policy_must_remain_control_only")

    for agent_name in policy.agent_allowed_namespaces:
        if agent_name not in capability_cards:
            warnings.append(f"tool_policy_agent_not_in_capability_cards:{agent_name}")

    return ProtocolCheckResult.fail_result(errors, warnings=warnings) if errors else ProtocolCheckResult.pass_result(
        warnings=warnings
    )
