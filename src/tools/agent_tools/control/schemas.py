from __future__ import annotations

"""Shared schemas for the protocol-governor sidecar.

These objects are intentionally deterministic and side-effect free. They do
not decide trades; they describe, validate, and audit how existing agents
exchange structured artifacts.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


TASK_PHASES = [
    "data_ready",
    "analyst_done",
    "pm_candidate",
    "final_action_ready",
    "audited",
    "execution_checked",
    "executed",
    "skipped",
    "settled",
    "reviewed",
    "learned",
]

TERMINAL_EXECUTION_PHASES = {"executed", "skipped"}

MEMORY_QUALITY_LEVELS = {
    "exact_real_state",
    "partial_real_state",
    "similar_sql_prior",
    "shadow_prior",
    "stale_or_conflicted_memory",
    "unqualified",
}


@dataclass(frozen=True)
class ProtocolCheckResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def pass_result(
        cls,
        *,
        warnings: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ProtocolCheckResult":
        return cls(
            ok=True,
            errors=[],
            warnings=list(warnings or []),
            metadata=dict(metadata or {}),
        )

    @classmethod
    def fail_result(
        cls,
        errors: Iterable[str],
        *,
        warnings: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ProtocolCheckResult":
        return cls(
            ok=False,
            errors=list(errors or []),
            warnings=list(warnings or []),
            metadata=dict(metadata or {}),
        )

    def merge(self, other: "ProtocolCheckResult") -> "ProtocolCheckResult":
        return ProtocolCheckResult(
            ok=self.ok and other.ok,
            errors=[*self.errors, *other.errors],
            warnings=[*self.warnings, *other.warnings],
            metadata={**self.metadata, **other.metadata},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AgentCapabilityCard:
    agent_name: str
    team: str
    reads: List[str] = field(default_factory=list)
    writes: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    may_call_llm: bool = False
    may_create_trade_authority: bool = False
    may_modify_lots_or_margin: bool = False
    may_execute_orders: bool = False
    may_write_settlement: bool = False
    may_write_future_learning: bool = False
    required_contract_versions: List[str] = field(default_factory=list)
    failure_mode: str = "degrade_to_audit_warning"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "team": self.team,
            "reads": list(self.reads),
            "writes": list(self.writes),
            "outputs": list(self.outputs),
            "may_call_llm": self.may_call_llm,
            "may_create_trade_authority": self.may_create_trade_authority,
            "may_modify_lots_or_margin": self.may_modify_lots_or_margin,
            "may_execute_orders": self.may_execute_orders,
            "may_write_settlement": self.may_write_settlement,
            "may_write_future_learning": self.may_write_future_learning,
            "required_contract_versions": list(self.required_contract_versions),
            "failure_mode": self.failure_mode,
        }


@dataclass(frozen=True)
class TaskLifecycleEvent:
    task_id: str
    context_id: str
    phase: str
    agent_name: str
    trading_date: str
    ticker: str
    config_id: str
    input_artifacts: List[str] = field(default_factory=list)
    output_artifacts: List[str] = field(default_factory=list)
    status: str = "ok"
    reasons: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "context_id": self.context_id,
            "phase": self.phase,
            "agent_name": self.agent_name,
            "trading_date": self.trading_date,
            "ticker": self.ticker,
            "config_id": self.config_id,
            "input_artifacts": list(self.input_artifacts),
            "output_artifacts": list(self.output_artifacts),
            "status": self.status,
            "reasons": list(self.reasons),
            "metadata": dict(self.metadata),
        }
