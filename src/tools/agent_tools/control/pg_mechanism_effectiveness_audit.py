from __future__ import annotations

"""Read-only mechanism effectiveness audit for completed backtest windows.

The system invariant audit answers whether records violate hard contracts.
This module answers a different question: did the already-designed artifact
chain connect across persisted records?

It is deliberately side-effect free. It never writes to the database, changes a
contract, creates trade authority, or evaluates strategy profitability as a
pass/fail condition. It also does not re-judge PM rank, deployment, or reason
semantics; PM-owned self-check results are the authority for PM contract
validity.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from database.artifact_store import load_externalized_json
from tools.agent_tools.control.pg_schemas import ProtocolCheckResult
from tools.common.final_action_semantics import (
    contract_reduces_or_exits_position,
    contract_requires_conditional_intraday_result,
)


STRATEGY_SOURCE_TYPE = "strategy"
SCENARIO_OPEN_INCREASE = "open_increase"
SCENARIO_CONDITIONAL_MONITOR = "conditional_monitor"
SCENARIO_REDUCE_EXIT = "reduce_exit"
SCENARIO_POSITION_HOLD = "position_hold"
SCENARIO_FLAT_WAIT = "flat_wait"
CHECKED_SCENARIOS = {
    SCENARIO_OPEN_INCREASE: "PM signed an open/add/scale style final_action_contract",
    SCENARIO_CONDITIONAL_MONITOR: "PM signed a conditional-monitor contract that may require Trader intraday result",
    SCENARIO_REDUCE_EXIT: "PM signed a reduce/exit style final_action_contract",
    SCENARIO_POSITION_HOLD: "PM signed a hold style final_action_contract for an existing position",
    SCENARIO_FLAT_WAIT: "PM signed a flat wait/hold final_action_contract",
}
SIGNAL_COLLECTION_FORBIDDEN_PM_FIELDS = {
    "final_action",
    "target_lots",
    "lots_delta",
    "target_position_ratio",
    "opportunity_rank",
    "opportunity_score",
    "opportunity_score_components",
    "rank_score",
    "rank_source",
    "rank_scope",
    "rank_input_components",
    "rank_capital_role",
    "capital_rank_generated_by",
    "capital_layer",
    "capital_deployment",
    "capital_allocation_reason",
    "position_sizing_result",
    "learning_used",
    "final_action_contract",
    "pm_six_step_trace",
}


@dataclass
class MechanismEffectivenessAuditReport:
    ok: bool
    hard_failures: List[str] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_protocol_result(self) -> ProtocolCheckResult:
        if self.ok:
            return ProtocolCheckResult.pass_result(
                warnings=[*self.warnings, *self.diagnostics],
                metadata={"counts": self.counts, **self.metadata},
            )
        return ProtocolCheckResult.fail_result(
            self.hard_failures,
            warnings=[*self.warnings, *self.diagnostics],
            metadata={"counts": self.counts, **self.metadata},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "hard_failures": list(self.hard_failures),
            "diagnostics": list(self.diagnostics),
            "warnings": list(self.warnings),
            "counts": dict(self.counts),
            "metadata": dict(self.metadata),
        }


def _safe_json(value: Any, artifact_path: Optional[str] = None, sha256: Optional[str] = None) -> Any:
    loaded = load_externalized_json(value, artifact_path, sha256)
    if isinstance(loaded, str):
        try:
            return json.loads(loaded)
        except Exception:
            return {}
    return loaded if loaded is not None else {}


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _forbidden_signal_collection_pm_fields(value: Any) -> List[str]:
    hits: List[str] = []

    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                if str(key) in SIGNAL_COLLECTION_FORBIDDEN_PM_FIELDS:
                    hits.append(child_path)
                visit(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{path}[{index}]")

    visit(value, "")
    return sorted(set(hits))


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _date10(value: Any) -> str:
    return str(value or "").strip()[:10]


def _int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    try:
        return int(float(value))
    except Exception:
        return default


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _fetch_config_id(conn: sqlite3.Connection, *, config_id: Optional[str], exp_name: Optional[str]) -> Optional[str]:
    if config_id:
        return str(config_id)
    if not exp_name or not _table_exists(conn, "config"):
        return None
    row = conn.execute("SELECT id FROM config WHERE exp_name = ?", (exp_name,)).fetchone()
    return str(row["id"]) if row else None


def _has_mechanism_records(conn: sqlite3.Connection) -> bool:
    for table_name in (
        "futures_recommendation",
        "alpha_setup_action_value",
        "futures_intraday_decision",
        "daily_settlement",
        "ticker_daily_pnl",
    ):
        if not _table_exists(conn, table_name):
            continue
        try:
            row = conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
        except sqlite3.Error:
            continue
        if row and int(row["count"] or 0) > 0:
            return True
    return False


def _date_filter_sql(alias: str, start_date: Optional[str], end_date: Optional[str]) -> tuple[str, List[Any]]:
    parts: List[str] = []
    params: List[Any] = []
    if start_date:
        parts.append(f"substr({alias}.trading_date, 1, 10) >= ?")
        params.append(start_date)
    if end_date:
        parts.append(f"substr({alias}.trading_date, 1, 10) <= ?")
        params.append(end_date)
    return (" AND " + " AND ".join(parts), params) if parts else ("", params)


def _load_recommendations(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    if not _table_exists(conn, "futures_recommendation"):
        return {}
    date_sql, params = _date_filter_sql("r", start_date, end_date)
    rows = conn.execute(
        f"""
        SELECT *
        FROM futures_recommendation r
        WHERE r.config_id = ?{date_sql}
        ORDER BY r.trading_date ASC, r.created_at ASC
        """,
        (config_id, *params),
    ).fetchall()
    recommendations: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        item["signal_snapshot"] = _safe_json(
            item.get("signal_snapshot"),
            item.get("signal_snapshot_artifact_path"),
            item.get("signal_snapshot_sha256"),
        )
        item["audit_payload"] = _safe_json(
            item.get("audit_payload"),
            item.get("audit_payload_artifact_path"),
            item.get("audit_payload_sha256"),
        )
        recommendations[str(item.get("id"))] = item
    return recommendations


def _load_intraday_decisions(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "futures_intraday_decision"):
        return []
    date_sql, params = _date_filter_sql("d", start_date, end_date)
    rows = conn.execute(
        f"""
        SELECT *
        FROM futures_intraday_decision d
        WHERE d.config_id = ?{date_sql}
        ORDER BY d.trading_date ASC, d.created_at ASC
        """,
        (config_id, *params),
    ).fetchall()
    values: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["features"] = _safe_json(item.get("features_json"))
        values.append(item)
    return values


def _load_action_values(conn: sqlite3.Connection, *, config_id: str) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "alpha_setup_action_value"):
        return []
    rows = conn.execute(
        """
        SELECT *
        FROM alpha_setup_action_value
        WHERE config_id = ? AND active = 1
        """,
        (config_id,),
    ).fetchall()
    values: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["payload"] = _safe_json(item.get("payload_json"))
        values.append(item)
    return values


def _contract_from_recommendation(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _dict(recommendation.get("signal_snapshot"))
    audit_payload = _dict(recommendation.get("audit_payload"))
    for source in (snapshot, audit_payload):
        contract = _dict(source.get("final_action_contract"))
        if contract:
            return contract
    return {}


def _contract_increases_risk(contract: Dict[str, Any]) -> bool:
    current = _int(contract.get("current_lots"))
    target = _int(contract.get("target_lots"))
    if current == 0:
        return target != 0
    return abs(target) > abs(current) or (target and current and (target > 0) != (current > 0))


def _contract_reduces_or_exits_position(contract: Dict[str, Any]) -> bool:
    return contract_reduces_or_exits_position(contract)


def _scenario_for_contract(contract: Dict[str, Any]) -> str:
    if _requires_conditional_intraday_result(contract):
        return SCENARIO_CONDITIONAL_MONITOR
    if _contract_reduces_or_exits_position(contract):
        return SCENARIO_REDUCE_EXIT
    if _contract_increases_risk(contract):
        return SCENARIO_OPEN_INCREASE
    if _int(contract.get("current_lots")) != 0:
        return SCENARIO_POSITION_HOLD
    return SCENARIO_FLAT_WAIT


def _requires_conditional_intraday_result(contract: Dict[str, Any]) -> bool:
    return contract_requires_conditional_intraday_result(contract)


def _has_intraday_decision(intraday_decisions: Iterable[Dict[str, Any]], recommendation_id: str) -> bool:
    for decision in intraday_decisions:
        if str(decision.get("recommendation_id") or "") == str(recommendation_id):
            return bool(_lower(decision.get("decision")) or _lower(decision.get("trigger_reason")))
    return False


def _audit_payload_candidates(recommendation: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for source in (_dict(recommendation.get("audit_payload")), _dict(recommendation.get("signal_snapshot"))):
        if not source:
            continue
        candidates.append(source)
        for key in ("independent_auditor", "auditor", "trade_contract_audit"):
            nested = _dict(source.get(key))
            if nested:
                candidates.append(nested)
    return candidates


def _auditor_verdict_from_recommendation(recommendation: Dict[str, Any]) -> str:
    for payload in _audit_payload_candidates(recommendation):
        verdict = _lower(
            payload.get("audit_verdict")
            or payload.get("verdict")
            or payload.get("audit_status")
            or payload.get("status")
        )
        if verdict:
            if verdict in {"approve", "approved", "pass", "passed"}:
                return "approve"
            if verdict in {"approve_with_warning", "warning", "approved_with_warning"}:
                return "approve_with_warning"
            if verdict in {"block", "blocked", "reject", "rejected"}:
                return "block"
            if verdict in {"require_review", "review_required", "manual_review"}:
                return "require_review"
            return verdict
    return ""


def _auditor_block_reason_present(recommendation: Dict[str, Any]) -> bool:
    reason_keys = {
        "audit_reason",
        "audit_reason_code",
        "audit_reason_codes",
        "hard_risk_reason",
        "hard_risk_reasons",
        "soft_risk_reasons",
        "risk_reasons",
        "block_reason",
        "block_reasons",
        "reason",
        "reason_codes",
    }
    for payload in _audit_payload_candidates(recommendation):
        for key in reason_keys:
            value = payload.get(key)
            if isinstance(value, list) and any(str(item).strip() for item in value):
                return True
            if isinstance(value, str) and value.strip():
                return True
    return False


def _audit_recommendation_mechanisms(
    *,
    recommendations: Dict[str, Dict[str, Any]],
    action_values: List[Dict[str, Any]],
    intraday_decisions: List[Dict[str, Any]],
    hard_failures: List[str],
    diagnostics: List[str],
    scenario_counts: Dict[str, int],
) -> None:
    for recommendation_id, recommendation in recommendations.items():
        if _lower(recommendation.get("source_type") or STRATEGY_SOURCE_TYPE) != STRATEGY_SOURCE_TYPE:
            continue
        snapshot = _dict(recommendation.get("signal_snapshot"))
        contract = _contract_from_recommendation(recommendation)
        if not contract:
            ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "")
            day = _date10(recommendation.get("trading_date") or recommendation.get("effective_trade_date"))
            hard_failures.append(f"mechanism_pm_final_action_contract_missing:{day}:{ticker}:{recommendation_id}")
            continue
        scenario = _scenario_for_contract(contract)
        scenario_counts[scenario] = scenario_counts.get(scenario, 0) + 1
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or contract.get("ticker") or "")
        day = _date10(recommendation.get("trading_date") or recommendation.get("effective_trade_date"))
        label = f"{day}:{ticker}:{recommendation_id}"
        signal_contract = _dict(snapshot.get("signal_collection_contract"))
        if not signal_contract:
            hard_failures.append(f"mechanism_signal_collection_contract_missing:{label}")
        else:
            producer = _lower(signal_contract.get("producer"))
            boundary = _lower(signal_contract.get("collector_decision_boundary"))
            if producer != "signal_collector":
                hard_failures.append(f"mechanism_signal_collection_contract_invalid_producer:{label}:{producer or 'missing'}")
            if boundary != "no_trade_authority":
                hard_failures.append(f"mechanism_signal_collection_contract_invalid_boundary:{label}:{boundary or 'missing'}")
            forbidden_fields = _forbidden_signal_collection_pm_fields(signal_contract)
            if forbidden_fields:
                hard_failures.append(
                    "mechanism_signal_collection_contract_contains_pm_fields:"
                    f"{label}:{','.join(forbidden_fields)}"
                )

        pm_trace = _dict(snapshot.get("pm_six_step_trace"))
        if not pm_trace:
            hard_failures.append(f"mechanism_pm_six_step_trace_missing:{label}")
        else:
            pm_check = _dict(pm_trace.get("pm_contract_self_check"))
            generation_check = _dict(pm_trace.get("step6_contract_generation_check"))
            if not pm_check:
                hard_failures.append(f"mechanism_pm_contract_self_check_missing:{label}")
            elif pm_check.get("ok") is not True:
                hard_failures.append(f"mechanism_pm_contract_self_check_failed:{label}")
            if not generation_check:
                hard_failures.append(f"mechanism_pm_step6_generation_check_missing:{label}")
            elif generation_check.get("ok") is not True:
                hard_failures.append(f"mechanism_pm_step6_generation_check_failed:{label}")

        if _requires_conditional_intraday_result(contract):
            auditor_verdict = _auditor_verdict_from_recommendation(recommendation)
            if auditor_verdict in {"block", "require_review"}:
                if not _auditor_block_reason_present(recommendation):
                    hard_failures.append(f"mechanism_conditional_probe_auditor_block_missing_reason:{label}")
            elif not _has_intraday_decision(intraday_decisions, recommendation_id):
                hard_failures.append(f"mechanism_conditional_probe_missing_intraday_result:{label}")


def audit_mechanism_effectiveness(
    *,
    db_path: str | Path,
    config_id: Optional[str] = None,
    exp_name: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> MechanismEffectivenessAuditReport:
    hard_failures: List[str] = []
    diagnostics: List[str] = []
    warnings: List[str] = []
    metadata: Dict[str, Any] = {
        "audit_boundary": (
            "mechanism_effectiveness_only; read_only; no_strategy_profitability_pass_fail; "
            "does_not_create_trade_authority_or_modify_lots"
        ),
        "classification": {
            "hard_fail": "mechanism_disconnected; block_strategy_evaluation",
            "diagnostic": "mechanism_connected_but_effect_or_quality_needs_strategy_analysis",
        },
        "checked_chain": [
            "signal_collector_contract_to_pm",
            "pm_step6_final_action_contract",
            "pm_step6_generation_check",
            "pm_contract_self_check",
            "conditional_monitor_to_trader_result",
        ],
        "checked_scenarios": dict(CHECKED_SCENARIOS),
    }
    db_path = Path(db_path)
    if not db_path.exists():
        return MechanismEffectivenessAuditReport(
            ok=True,
            warnings=[f"sqlite_missing:{db_path}"],
            counts={},
            metadata={**metadata, "db_path": str(db_path), "record_boundary": "no_records_to_audit"},
        )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        resolved_config_id = _fetch_config_id(conn, config_id=config_id, exp_name=exp_name)
        if not resolved_config_id:
            if not _has_mechanism_records(conn):
                return MechanismEffectivenessAuditReport(
                    ok=True,
                    warnings=[f"config_not_found_empty_db:{exp_name or config_id or 'missing'}"],
                    diagnostics=diagnostics,
                    metadata={
                        **metadata,
                        "db_path": str(db_path),
                        "record_boundary": "empty_db_no_mechanism_records_to_audit",
                    },
                )
            hard_failures.append(f"config_not_found:{exp_name or config_id or 'missing'}")
            return MechanismEffectivenessAuditReport(
                ok=False,
                hard_failures=hard_failures,
                warnings=warnings,
                diagnostics=diagnostics,
                metadata={**metadata, "db_path": str(db_path)},
            )
        recommendations = _load_recommendations(
            conn,
            config_id=resolved_config_id,
            start_date=start_date,
            end_date=end_date,
        )
        intraday_decisions = _load_intraday_decisions(
            conn,
            config_id=resolved_config_id,
            start_date=start_date,
            end_date=end_date,
        )
        action_values = _load_action_values(conn, config_id=resolved_config_id)
    finally:
        conn.close()

    metadata["config_id"] = resolved_config_id
    metadata["db_path"] = str(db_path)
    scenario_counts: Dict[str, int] = {}
    _audit_recommendation_mechanisms(
        recommendations=recommendations,
        action_values=action_values,
        intraday_decisions=intraday_decisions,
        hard_failures=hard_failures,
        diagnostics=diagnostics,
        scenario_counts=scenario_counts,
    )
    counts = {
        "recommendations": len(recommendations),
        "action_values": len(action_values),
        "intraday_decisions": len(intraday_decisions),
        "hard_failures": len(hard_failures),
        "diagnostics": len(diagnostics),
        "scenarios": dict(sorted(scenario_counts.items())),
    }
    return MechanismEffectivenessAuditReport(
        ok=not hard_failures,
        hard_failures=hard_failures,
        diagnostics=diagnostics,
        warnings=warnings,
        counts=counts,
        metadata=metadata,
    )
