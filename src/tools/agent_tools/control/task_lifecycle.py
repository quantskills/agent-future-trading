from __future__ import annotations

"""Task lifecycle helpers for one trading_date/ticker/config_id chain."""

from typing import Any, Dict, Iterable, List, Optional

from tools.common.contracts import date_text
from tools.agent_tools.control.schemas import TASK_PHASES, TERMINAL_EXECUTION_PHASES, ProtocolCheckResult, TaskLifecycleEvent


def build_context_id(config_id: str, trading_date: Any) -> str:
    return f"{str(config_id or 'default')}:{date_text(trading_date)}"


def build_task_id(config_id: str, trading_date: Any, ticker: str) -> str:
    return f"{build_context_id(config_id, trading_date)}:{str(ticker or '').upper()}"


def create_lifecycle_event(
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
    return TaskLifecycleEvent(
        task_id=build_task_id(config_id, trading_date, ticker),
        context_id=build_context_id(config_id, trading_date),
        phase=str(phase or ""),
        agent_name=str(agent_name or ""),
        trading_date=date_text(trading_date),
        ticker=str(ticker or "").upper(),
        config_id=str(config_id or ""),
        input_artifacts=list(input_artifacts or []),
        output_artifacts=list(output_artifacts or []),
        status=str(status or "ok"),
        reasons=list(reasons or []),
        metadata=dict(metadata or {}),
    )


def validate_lifecycle_sequence(events: Iterable[TaskLifecycleEvent]) -> ProtocolCheckResult:
    event_list = list(events)
    errors: List[str] = []
    warnings: List[str] = []
    last_index = -1
    terminal_execution_seen = False
    task_ids = set()

    for event in event_list:
        task_ids.add(event.task_id)
        if event.phase not in TASK_PHASES:
            errors.append(f"unknown_phase:{event.phase}")
            continue
        phase_index = TASK_PHASES.index(event.phase)
        if phase_index < last_index:
            errors.append(f"phase_regression:{event.phase}")
        if terminal_execution_seen and event.phase in {"executed", "skipped"}:
            errors.append("multiple_terminal_execution_phases")
        if event.phase in TERMINAL_EXECUTION_PHASES:
            terminal_execution_seen = True
        if event.status not in {"ok", "warning", "failed", "skipped"}:
            warnings.append(f"unknown_status:{event.status}")
        last_index = max(last_index, phase_index)

    if len(task_ids) > 1:
        errors.append("mixed_task_ids_in_lifecycle_sequence")
    return ProtocolCheckResult.fail_result(errors, warnings=warnings) if errors else ProtocolCheckResult.pass_result(
        warnings=warnings,
        metadata={"event_count": len(event_list)},
    )
