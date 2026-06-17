from __future__ import annotations

"""Operational cost and resource-budget audit helpers.

This module audits resource use only. It must not create trade authority,
change lots, alter margin, block orders, or decide exit/reduce actions.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


TRADE_ACTION_KEYS = {
    "authority_type",
    "can_open_real_position",
    "can_apply_min_real_floor",
    "lots",
    "target_lots",
    "margin_ratio",
    "max_allowed_margin_ratio",
    "final_action",
    "no_trade",
    "block",
    "cap",
    "reduce",
    "exit",
}

ALLOWED_COST_ACTIONS = {
    "cost_report",
    "cost_warning",
    "retry_suggestion",
    "investigation_priority",
}


@dataclass(frozen=True)
class CostBudgetLimits:
    max_llm_calls_per_day: Optional[int] = None
    max_llm_calls_per_agent: Optional[int] = None
    max_retry_calls_per_day: Optional[int] = None
    max_pandaai_calls_per_day: Optional[int] = None
    max_sql_rag_queries_per_day: Optional[int] = None
    max_reflection_calls_per_day: Optional[int] = None
    max_reflection_calls_per_scope: Optional[int] = None
    max_total_tokens_per_day: Optional[int] = None


@dataclass(frozen=True)
class CostAuditReport:
    ok: bool
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    totals: Dict[str, Any] = field(default_factory=dict)
    by_agent: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    by_scope: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    allowed_actions: List[str] = field(default_factory=lambda: sorted(ALLOWED_COST_ACTIONS))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "totals": dict(self.totals),
            "by_agent": {k: dict(v) for k, v in self.by_agent.items()},
            "by_scope": {k: dict(v) for k, v in self.by_scope.items()},
            "allowed_actions": list(self.allowed_actions),
        }


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _scope_key(event: Dict[str, Any]) -> str:
    parts = [
        str(event.get("trading_date") or ""),
        str(event.get("ticker") or "").upper(),
        str(event.get("setup_type") or event.get("scope") or ""),
    ]
    return ":".join(part for part in parts if part) or "global"


def _add_metrics(bucket: Dict[str, Any], event: Dict[str, Any]) -> None:
    bucket["event_count"] = _int_value(bucket.get("event_count")) + 1
    bucket["llm_calls"] = _int_value(bucket.get("llm_calls")) + _int_value(event.get("llm_calls"))
    bucket["retry_calls"] = _int_value(bucket.get("retry_calls")) + _int_value(event.get("retry_calls"))
    bucket["pandaai_calls"] = _int_value(bucket.get("pandaai_calls")) + _int_value(event.get("pandaai_calls"))
    bucket["sql_rag_queries"] = _int_value(bucket.get("sql_rag_queries")) + _int_value(event.get("sql_rag_queries"))
    bucket["reflection_calls"] = _int_value(bucket.get("reflection_calls")) + _int_value(event.get("reflection_calls"))
    bucket["prompt_tokens"] = _int_value(bucket.get("prompt_tokens")) + _int_value(event.get("prompt_tokens"))
    bucket["completion_tokens"] = _int_value(bucket.get("completion_tokens")) + _int_value(event.get("completion_tokens"))
    bucket["total_tokens"] = _int_value(bucket.get("total_tokens")) + _int_value(
        event.get("total_tokens", _int_value(event.get("prompt_tokens")) + _int_value(event.get("completion_tokens")))
    )


def _check_limit(warnings: List[str], label: str, value: int, limit: Optional[int]) -> None:
    if limit is not None and limit >= 0 and value > limit:
        warnings.append(f"{label}_over_budget:{value}>{limit}")


def audit_cost_budget(
    events: Iterable[Dict[str, Any]],
    *,
    limits: Optional[CostBudgetLimits] = None,
) -> CostAuditReport:
    limits = limits or CostBudgetLimits()
    warnings: List[str] = []
    errors: List[str] = []
    totals: Dict[str, Any] = {}
    by_agent: Dict[str, Dict[str, Any]] = {}
    by_scope: Dict[str, Dict[str, Any]] = {}

    for raw_event in events or []:
        if not isinstance(raw_event, dict):
            warnings.append("cost_event_not_dict")
            continue
        if set(raw_event).intersection(TRADE_ACTION_KEYS):
            errors.append("cost_audit_event_contains_trade_action_field")
        _add_metrics(totals, raw_event)
        agent = str(raw_event.get("agent_name") or raw_event.get("agent") or "unknown")
        scope = _scope_key(raw_event)
        _add_metrics(by_agent.setdefault(agent, {}), raw_event)
        _add_metrics(by_scope.setdefault(scope, {}), raw_event)

    _check_limit(warnings, "llm_calls_per_day", _int_value(totals.get("llm_calls")), limits.max_llm_calls_per_day)
    _check_limit(warnings, "retry_calls_per_day", _int_value(totals.get("retry_calls")), limits.max_retry_calls_per_day)
    _check_limit(
        warnings,
        "pandaai_calls_per_day",
        _int_value(totals.get("pandaai_calls")),
        limits.max_pandaai_calls_per_day,
    )
    _check_limit(
        warnings,
        "sql_rag_queries_per_day",
        _int_value(totals.get("sql_rag_queries")),
        limits.max_sql_rag_queries_per_day,
    )
    _check_limit(
        warnings,
        "reflection_calls_per_day",
        _int_value(totals.get("reflection_calls")),
        limits.max_reflection_calls_per_day,
    )
    _check_limit(
        warnings,
        "total_tokens_per_day",
        _int_value(totals.get("total_tokens")),
        limits.max_total_tokens_per_day,
    )

    if limits.max_llm_calls_per_agent is not None:
        for agent, metrics in by_agent.items():
            _check_limit(
                warnings,
                f"llm_calls_for_agent:{agent}",
                _int_value(metrics.get("llm_calls")),
                limits.max_llm_calls_per_agent,
            )
    if limits.max_reflection_calls_per_scope is not None:
        for scope, metrics in by_scope.items():
            _check_limit(
                warnings,
                f"reflection_calls_for_scope:{scope}",
                _int_value(metrics.get("reflection_calls")),
                limits.max_reflection_calls_per_scope,
            )

    return CostAuditReport(
        ok=not errors,
        warnings=warnings,
        errors=errors,
        totals=totals,
        by_agent=by_agent,
        by_scope=by_scope,
    )


def assert_cost_audit_is_non_trading(report: Dict[str, Any]) -> List[str]:
    """Return violations if a cost report tries to carry trade actions."""

    if not isinstance(report, dict):
        return ["cost_report_missing"]
    violations: List[str] = []
    if set(report).intersection(TRADE_ACTION_KEYS):
        violations.append("cost_report_contains_trade_action_field")
    for action in report.get("allowed_actions") or []:
        if action not in ALLOWED_COST_ACTIONS:
            violations.append(f"cost_report_disallowed_action:{action}")
    return violations
