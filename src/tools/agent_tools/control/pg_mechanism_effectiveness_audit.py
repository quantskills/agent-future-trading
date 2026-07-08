from __future__ import annotations

"""Read-only mechanism effectiveness audit for completed backtest windows.

The system invariant audit answers whether records violate hard contracts.
This module answers a different question: did the already-designed learning,
ranking, deployment, conditional-monitor, and hold/exit mechanisms actually
connect across persisted artifacts?

It is deliberately side-effect free. It never writes to the database, changes a
contract, creates trade authority, or evaluates strategy profitability as a
pass/fail condition.
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
    is_conditional_monitor_contract,
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
CONDITIONAL_FINAL_ACTIONS = {"conditional_probe", "conditional_monitor", "watch_trigger"}


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


def _float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    try:
        return float(value)
    except Exception:
        return default


def _text_blob(*values: Any) -> str:
    return " ".join(str(value or "") for value in values).lower()


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


def _load_daily_settlements(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "daily_settlement") or not _table_exists(conn, "portfolio"):
        return []
    date_sql, params = _date_filter_sql("ds", start_date, end_date)
    rows = conn.execute(
        f"""
        SELECT ds.*
        FROM daily_settlement ds
        JOIN portfolio p ON ds.portfolio_id = p.id
        WHERE p.config_id = ?{date_sql}
        ORDER BY ds.trading_date ASC
        """,
        (config_id, *params),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_ticker_daily_pnl(
    conn: sqlite3.Connection,
    *,
    config_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "ticker_daily_pnl") or not _table_exists(conn, "portfolio"):
        return []
    date_sql, params = _date_filter_sql("tdp", start_date, end_date)
    rows = conn.execute(
        f"""
        SELECT tdp.*
        FROM ticker_daily_pnl tdp
        JOIN portfolio p ON tdp.portfolio_id = p.id
        WHERE p.config_id = ?{date_sql}
        ORDER BY tdp.trading_date ASC, tdp.ticker ASC
        """,
        (config_id, *params),
    ).fetchall()
    return [dict(row) for row in rows]


def _contract_from_recommendation(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _dict(recommendation.get("signal_snapshot"))
    audit_payload = _dict(recommendation.get("audit_payload"))
    for source in (snapshot, audit_payload):
        contract = _dict(source.get("final_action_contract"))
        if contract:
            return contract
    return {}


def _rank_value(contract: Dict[str, Any]) -> Optional[int]:
    evidence = _dict(contract.get("evidence_used"))
    deployment = _dict(contract.get("capital_deployment"))
    raw = deployment.get("opportunity_rank")
    if raw in (None, ""):
        raw = evidence.get("opportunity_rank")
    try:
        return int(raw)
    except Exception:
        return None


def _capital_layer(contract: Dict[str, Any]) -> str:
    deployment = _dict(contract.get("capital_deployment"))
    evidence = _dict(contract.get("evidence_used"))
    return _lower(deployment.get("capital_layer") or evidence.get("capital_layer"))


def _opportunity_score(contract: Dict[str, Any]) -> Optional[float]:
    evidence = _dict(contract.get("evidence_used"))
    raw = evidence.get("opportunity_score")
    try:
        return float(raw)
    except Exception:
        return None


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


def _is_conditional_monitor(contract: Dict[str, Any]) -> bool:
    return is_conditional_monitor_contract(contract)


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


def _capital_deployment_selected(contract: Dict[str, Any]) -> Optional[bool]:
    deployment = _dict(contract.get("capital_deployment"))
    if not deployment:
        return None
    if "selected_for_capital_deployment" not in deployment:
        return None
    return bool(deployment.get("selected_for_capital_deployment"))


def _capital_allocation_reason(contract: Dict[str, Any]) -> str:
    evidence = _dict(contract.get("evidence_used"))
    deployment = _dict(contract.get("capital_deployment"))
    return str(
        evidence.get("capital_allocation_reason")
        or deployment.get("capital_allocation_reason")
        or contract.get("capital_allocation_reason")
        or ""
    )


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

        score = _opportunity_score(contract)
        rank = _rank_value(contract)
        selected = _capital_deployment_selected(contract)
        reason = _capital_allocation_reason(contract)
        if rank is not None and score is not None and selected is False and rank <= 3:
            diagnostics.append(f"diagnostic_top_rank_not_deployed:{label}:rank={rank}:score={score}:reason={reason or 'missing'}")


def _audit_capital_deployment_diagnostics(
    daily_settlements: List[Dict[str, Any]],
    diagnostics: List[str],
) -> None:
    ratios = [_float(row.get("margin_ratio")) for row in daily_settlements if row.get("margin_ratio") is not None]
    if not ratios:
        return
    average = sum(ratios) / len(ratios)
    if average < 0.008:
        diagnostics.append(f"diagnostic_low_average_margin_utilization:avg={average:.6f}:days={len(ratios)}")


def _audit_rank_pnl_diagnostics(
    recommendations: Dict[str, Dict[str, Any]],
    ticker_daily_pnl: List[Dict[str, Any]],
    diagnostics: List[str],
) -> None:
    pnl_by_day_ticker = {
        (_date10(row.get("trading_date")), str(row.get("ticker") or "").upper()): {
            "daily_pnl": _float(row.get("daily_pnl")),
            "new_position_pnl": _float(row.get("new_position_pnl")),
        }
        for row in ticker_daily_pnl
    }
    top_rank_pnls_by_layer: Dict[str, List[float]] = {}
    low_rank_pnls_by_layer: Dict[str, List[float]] = {}
    for recommendation_id, recommendation in recommendations.items():
        contract = _contract_from_recommendation(recommendation)
        rank = _rank_value(contract)
        if rank is None:
            continue
        scenario = _scenario_for_contract(contract)
        if scenario not in {SCENARIO_OPEN_INCREASE, SCENARIO_CONDITIONAL_MONITOR}:
            continue
        layer = _capital_layer(contract)
        if layer not in {"exploration_probe", "real_budget_entry", "alpha_scale_entry"}:
            continue
        day = _date10(recommendation.get("trading_date"))
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "").upper()
        pnl_row = pnl_by_day_ticker.get((day, ticker))
        if pnl_row is None:
            continue
        pnl = pnl_row["new_position_pnl"]
        if rank <= 3:
            top_rank_pnls_by_layer.setdefault(layer, []).append(pnl)
        elif rank >= 6:
            low_rank_pnls_by_layer.setdefault(layer, []).append(pnl)
    for layer in sorted(set(top_rank_pnls_by_layer) | set(low_rank_pnls_by_layer)):
        top_rank_pnls = top_rank_pnls_by_layer.get(layer, [])
        low_rank_pnls = low_rank_pnls_by_layer.get(layer, [])
        if top_rank_pnls and sum(top_rank_pnls) < 0:
            diagnostics.append(
                "diagnostic_top_rank_bucket_negative_new_position_pnl:"
                f"layer={layer}:pnl={sum(top_rank_pnls):.2f}:count={len(top_rank_pnls)}"
            )
        if top_rank_pnls and low_rank_pnls and sum(top_rank_pnls) < sum(low_rank_pnls):
            diagnostics.append(
                "diagnostic_low_rank_outperformed_top_rank:"
                f"layer={layer}:top={sum(top_rank_pnls):.2f}:low={sum(low_rank_pnls):.2f}"
            )


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
        daily_settlements = _load_daily_settlements(
            conn,
            config_id=resolved_config_id,
            start_date=start_date,
            end_date=end_date,
        )
        ticker_daily_pnl = _load_ticker_daily_pnl(
            conn,
            config_id=resolved_config_id,
            start_date=start_date,
            end_date=end_date,
        )
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
    _audit_capital_deployment_diagnostics(daily_settlements, diagnostics)
    _audit_rank_pnl_diagnostics(recommendations, ticker_daily_pnl, diagnostics)
    counts = {
        "recommendations": len(recommendations),
        "action_values": len(action_values),
        "intraday_decisions": len(intraday_decisions),
        "daily_settlements": len(daily_settlements),
        "ticker_daily_pnl": len(ticker_daily_pnl),
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
