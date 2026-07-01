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
    contract_increases_risk_position,
    contract_reduces_or_exits_position,
    derive_memory_requirements,
    has_valid_generic_no_change_explanation,
    has_valid_hold_exit_no_change_explanation,
    is_conditional_monitor_contract,
    lane_matches_memory_requirement,
)


STRATEGY_SOURCE_TYPE = "strategy"
ACTION_PREFERENCE_VALUES = {
    "positive_candidate_open",
    "positive_candidate_hold",
    "positive_candidate_exit",
    "positive_candidate_execution",
    "negative_revalidate",
    "negative_hold_revalidate",
    "tail_loss_protect",
}
PROTECTIVE_PREFERENCES = {"tail_loss_protect", "negative_hold_revalidate", "negative_revalidate"}
POSITIVE_PREFERENCES = {
    "positive_candidate_open",
    "positive_candidate_hold",
    "positive_candidate_exit",
    "positive_candidate_execution",
}
REAL_REWARD_SOURCE_MARKERS = {"episode", "real"}
LEARNING_COMPONENT_FIELDS = {
    "positive_learning",
    "negative_learning",
    "execution_profile_learning",
    "recent_tail_loss_penalty",
}
SCENARIO_OPEN_INCREASE = "open_increase"
SCENARIO_CONDITIONAL_MONITOR = "conditional_monitor"
SCENARIO_REDUCE_EXIT = "reduce_exit"
SCENARIO_POSITION_HOLD = "position_hold"
SCENARIO_UNSELECTED_CANDIDATE = "unselected_candidate"
SCENARIO_FLAT_WAIT = "flat_wait"
CHECKED_SCENARIOS = {
    SCENARIO_OPEN_INCREASE: "open/add/scale learning must land in score/rank and then the single final_action_contract",
    SCENARIO_CONDITIONAL_MONITOR: "conditional probe learning must land in score/rank and Trader must record triggered/not_triggered",
    SCENARIO_REDUCE_EXIT: "hold/exit learning must land in target_lots reduction, exit action, or an explicit lifecycle explanation",
    SCENARIO_POSITION_HOLD: "hold/exit learning must either preserve a valid hold or explain why no position change happened",
    SCENARIO_UNSELECTED_CANDIDATE: "ranked candidate not deployed must carry capital_allocation_reason",
    SCENARIO_FLAT_WAIT: "flat/no-action candidate with prior learning must explain why it did not affect deployment",
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


def _payload_or_row_value(row: Dict[str, Any], key: str, *aliases: str) -> Any:
    payload = _dict(row.get("payload"))
    for name in (key, *aliases):
        value = row.get(name)
        if value not in (None, ""):
            return value
    for name in (key, *aliases):
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return None


def _row_preference(row: Dict[str, Any]) -> str:
    return _lower(_payload_or_row_value(row, "action_preference"))


def _row_reward_source(row: Dict[str, Any]) -> str:
    return _lower(_payload_or_row_value(row, "reward_source", "source_reward_type", "reward_kind"))


def _row_evidence_scope(row: Dict[str, Any]) -> str:
    return _lower(_payload_or_row_value(row, "evidence_scope", "amplification_scope_quality"))


def _row_lane(row: Dict[str, Any]) -> str:
    return _lower(_payload_or_row_value(row, "action_value_lane", "source_action_value_lane", "action_name"))


def _row_consumer_scope(row: Dict[str, Any]) -> str:
    return _lower(_payload_or_row_value(row, "consumer_scope", "learning_consumer_scope") or "pm_learning")


def _row_memory_side_role(row: Dict[str, Any]) -> str:
    return _lower(_payload_or_row_value(row, "memory_side_role"))


def _row_has_reward(row: Dict[str, Any]) -> bool:
    return (
        _payload_or_row_value(row, "reward_sum") is not None
        or _payload_or_row_value(row, "reward_mean") is not None
        or _payload_or_row_value(row, "win_rate") is not None
    )


def _row_is_real(row: Dict[str, Any]) -> bool:
    source = _row_reward_source(row)
    return any(marker in source for marker in REAL_REWARD_SOURCE_MARKERS)


def _row_has_pm_canonical_fields(row: Dict[str, Any]) -> bool:
    return bool(
        _row_consumer_scope(row) == "pm_learning"
        and _row_preference(row)
        and _row_reward_source(row)
        and _row_evidence_scope(row)
        and _row_lane(row)
        and _row_has_reward(row)
    )


def _lane_matches_requirement(row: Dict[str, Any], lane: str) -> bool:
    required = _lower(lane)
    if not required:
        return True
    return lane_matches_memory_requirement(required, _row_lane(row))


def _contract_action_value_rows(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    learning = _dict(contract.get("learning_used"))
    rows = learning.get("alpha_setup_action_values")
    if not isinstance(rows, list):
        return []
    return [_dict(row) for row in rows if isinstance(row, dict)]


def _learning_components(contract: Dict[str, Any]) -> Dict[str, float]:
    evidence = _dict(contract.get("evidence_used"))
    components = _dict(evidence.get("opportunity_score_components"))
    return {field: _float(components.get(field)) for field in LEARNING_COMPONENT_FIELDS}


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


def _opportunity_score(contract: Dict[str, Any]) -> Optional[float]:
    evidence = _dict(contract.get("evidence_used"))
    raw = evidence.get("opportunity_score")
    try:
        return float(raw)
    except Exception:
        return None


def _recommendation_side(recommendation: Dict[str, Any], contract: Dict[str, Any]) -> str:
    target = _int(contract.get("target_lots"))
    current = _int(contract.get("current_lots"))
    if target:
        return "long" if target > 0 else "short"
    if current:
        return "long" if current > 0 else "short"
    side = _lower(_dict(contract.get("action_evidence_contract")).get("side") or contract.get("side"))
    return side if side in {"long", "short"} else ""


def _matches_action_value_scope(row: Dict[str, Any], *, ticker: str, side: str, decision_date: str) -> bool:
    if not ticker or side not in {"long", "short"} or not decision_date:
        return False
    row_ticker = str(row.get("ticker") or "").upper()
    row_side = _lower(row.get("side"))
    if row_ticker not in {ticker.upper(), "*"}:
        return False
    if row_side not in {side, "*", "both", "any"}:
        return False
    sample_day = _date10(row.get("last_sample_date"))
    return bool(sample_day and sample_day < decision_date)


def _prior_real_action_values(
    action_values: Iterable[Dict[str, Any]],
    *,
    ticker: str,
    side: str,
    decision_date: str,
    lane: str = "",
    memory_side_role: str = "",
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in action_values:
        if not _matches_action_value_scope(row, ticker=ticker, side=side, decision_date=decision_date):
            continue
        if not _lane_matches_requirement(row, lane):
            continue
        row_role = _row_memory_side_role(row)
        if row_role and memory_side_role and row_role != _lower(memory_side_role):
            continue
        preference = _row_preference(row)
        if preference not in ACTION_PREFERENCE_VALUES:
            continue
        if _row_consumer_scope(row) != "pm_learning":
            continue
        if not _row_is_real(row):
            continue
        rows.append(row)
    return rows


def _dedupe_action_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    fallback = 0
    for row in rows:
        key = (
            str(row.get("id") or _payload_or_row_value(row, "id") or f"row-{fallback}"),
            str(row.get("scope_key") or _payload_or_row_value(row, "scope_key") or ""),
            str(row.get("side") or _payload_or_row_value(row, "side") or ""),
            _row_lane(row),
            _row_preference(row),
        )
        fallback += 1
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _contract_rows_include_prior(contract_rows: Iterable[Dict[str, Any]], prior_rows: Iterable[Dict[str, Any]]) -> bool:
    contract_rows_list = list(contract_rows)
    contract_keys = {
        (
            str(row.get("scope_key") or _payload_or_row_value(row, "scope_key") or ""),
            str(row.get("action_name") or _payload_or_row_value(row, "action_name") or ""),
            _row_preference(row),
        )
        for row in contract_rows_list
    }
    canonical_preferences = {
        _row_preference(row)
        for row in contract_rows_list
        if _row_preference(row) in ACTION_PREFERENCE_VALUES and _row_has_pm_canonical_fields(row)
    }
    for row in prior_rows:
        preference = _row_preference(row)
        key = (
            str(row.get("scope_key") or _payload_or_row_value(row, "scope_key") or ""),
            str(row.get("action_name") or _payload_or_row_value(row, "action_name") or ""),
            preference,
        )
        if key in contract_keys:
            return True
        if preference and preference in canonical_preferences:
            return True
    return False


def _learning_components_nonzero(components: Dict[str, float]) -> bool:
    return any(abs(value) > 1e-12 for value in components.values())


def _contract_lots_changed(contract: Dict[str, Any]) -> bool:
    return _int(contract.get("target_lots")) != _int(contract.get("current_lots")) or _lower(contract.get("final_action")) not in {"", "hold", "wait"}


def _contract_increases_risk(contract: Dict[str, Any]) -> bool:
    return contract_increases_risk_position(contract)


def _contract_reduces_or_exits_position(contract: Dict[str, Any]) -> bool:
    return contract_reduces_or_exits_position(contract)


def _contract_has_no_change_explanation(contract: Dict[str, Any]) -> bool:
    return has_valid_generic_no_change_explanation(contract)


def _scenario_for_contract(contract: Dict[str, Any]) -> str:
    if _is_conditional_monitor(contract):
        return SCENARIO_CONDITIONAL_MONITOR
    if _contract_reduces_or_exits_position(contract):
        return SCENARIO_REDUCE_EXIT
    if _contract_increases_risk(contract):
        return SCENARIO_OPEN_INCREASE
    if _capital_deployment_selected(contract) is False:
        return SCENARIO_UNSELECTED_CANDIDATE
    if _int(contract.get("current_lots")) != 0:
        return SCENARIO_POSITION_HOLD
    return SCENARIO_FLAT_WAIT


def _hold_exit_has_explanation(contract: Dict[str, Any]) -> bool:
    return has_valid_hold_exit_no_change_explanation(contract)


def _has_hold_exit_learning(contract: Dict[str, Any]) -> bool:
    for row in _contract_action_value_rows(contract):
        preference = _row_preference(row)
        lane = _row_lane(row)
        if preference in {"negative_hold_revalidate", "tail_loss_protect", "positive_candidate_exit"}:
            return True
        if lane in {"hold", "exit"} and preference in ACTION_PREFERENCE_VALUES:
            return True
    return False


def _prior_rows_include_hold_exit_learning(rows: Iterable[Dict[str, Any]]) -> bool:
    for row in rows:
        preference = _row_preference(row)
        lane = _row_lane(row)
        if preference in {"negative_hold_revalidate", "tail_loss_protect", "positive_candidate_exit"}:
            return True
        if lane in {"hold", "exit"} and preference in ACTION_PREFERENCE_VALUES:
            return True
    return False


def _hold_exit_landed_in_position(contract: Dict[str, Any]) -> bool:
    current = _int(contract.get("current_lots"))
    if current == 0:
        return True
    return contract_reduces_or_exits_position(contract)


def _is_conditional_monitor(contract: Dict[str, Any]) -> bool:
    return is_conditional_monitor_contract(contract)


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
    rank_pnl_rows: List[tuple[int, float, str]] = []
    for recommendation_id, recommendation in recommendations.items():
        if _lower(recommendation.get("source_type") or STRATEGY_SOURCE_TYPE) != STRATEGY_SOURCE_TYPE:
            continue
        contract = _contract_from_recommendation(recommendation)
        if not contract:
            continue
        scenario = _scenario_for_contract(contract)
        scenario_counts[scenario] = scenario_counts.get(scenario, 0) + 1
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or contract.get("ticker") or "")
        day = _date10(recommendation.get("trading_date") or recommendation.get("effective_trade_date"))
        label = f"{day}:{ticker}:{recommendation_id}"
        side = _recommendation_side(recommendation, contract)
        memory_requirements = derive_memory_requirements(contract)
        required_memory = [
            item for item in (memory_requirements.get("required_pm_memory") or [])
            if isinstance(item, dict) and item.get("must_land_in_pm_contract")
        ]
        if not required_memory and side in {"long", "short"}:
            required_memory = [{
                "side": side,
                "lane": "",
                "memory_side_role": "",
                "must_land_in_pm_contract": True,
            }]
        prior_rows_by_requirement: List[Dict[str, Any]] = []
        for requirement in required_memory:
            req_side = _lower(requirement.get("side")) or side
            if req_side not in {"long", "short"}:
                continue
            req_lane = _lower(requirement.get("lane") or requirement.get("learning_lane"))
            req_role = _lower(requirement.get("memory_side_role"))
            requirement_prior_rows = _prior_real_action_values(
                action_values,
                ticker=ticker,
                side=req_side,
                decision_date=day,
                lane=req_lane,
                memory_side_role=req_role,
            )
            prior_rows_by_requirement.extend(requirement_prior_rows)
        prior_rows = _dedupe_action_rows(prior_rows_by_requirement)
        contract_rows = _contract_action_value_rows(contract)
        components = _learning_components(contract)
        components_nonzero = _learning_components_nonzero(components)

        if prior_rows and not contract_rows:
            hard_failures.append(f"mechanism_action_value_not_read_by_pm:{label}:side={side or 'missing'}")
        elif prior_rows and not _contract_rows_include_prior(contract_rows, prior_rows):
            hard_failures.append(f"mechanism_matching_action_value_not_landed_in_pm:{label}:side={side or 'missing'}")

        for row in contract_rows:
            if _row_consumer_scope(row) != "pm_learning":
                hard_failures.append(
                    f"mechanism_pm_consumed_non_pm_learning:{label}:{_row_consumer_scope(row) or 'missing'}"
                )
            preference = _row_preference(row)
            if preference in ACTION_PREFERENCE_VALUES and not _row_has_pm_canonical_fields(row):
                hard_failures.append(
                    f"mechanism_pm_action_value_missing_canonical_fields:{label}:{preference or 'missing'}"
                )

        prior_hold_exit_learning = _prior_rows_include_hold_exit_learning(prior_rows)
        learning_should_land_in_score = scenario in {SCENARIO_OPEN_INCREASE, SCENARIO_CONDITIONAL_MONITOR}
        if prior_rows and learning_should_land_in_score and not components_nonzero:
            hard_failures.append(f"mechanism_pm_learning_not_in_score:{label}:side={side or 'missing'}")
        if (
            prior_rows
            and not learning_should_land_in_score
            and scenario in {SCENARIO_FLAT_WAIT, SCENARIO_UNSELECTED_CANDIDATE}
            and not components_nonzero
            and not _capital_allocation_reason(contract)
            and not _contract_has_no_change_explanation(contract)
        ):
            hard_failures.append(f"mechanism_pm_learning_not_explained_for_no_deployment:{label}:side={side or 'missing'}")
        if (
            prior_rows
            and prior_hold_exit_learning
            and not components_nonzero
            and not _contract_reduces_or_exits_position(contract)
            and not _hold_exit_has_explanation(contract)
        ):
            hard_failures.append(f"mechanism_hold_exit_learning_not_landed:{label}")

        rank = _rank_value(contract)
        if components_nonzero and rank is None and scenario in {SCENARIO_OPEN_INCREASE, SCENARIO_CONDITIONAL_MONITOR}:
            hard_failures.append(f"mechanism_learning_score_missing_rank:{label}")

        deployment = _dict(contract.get("capital_deployment"))
        reason = _capital_allocation_reason(contract)
        rank_deployment_scenario = scenario in {
            SCENARIO_OPEN_INCREASE,
            SCENARIO_CONDITIONAL_MONITOR,
            SCENARIO_UNSELECTED_CANDIDATE,
        }
        if rank is not None and rank_deployment_scenario and not deployment:
            hard_failures.append(f"mechanism_rank_missing_capital_deployment:{label}:rank={rank}")
        if (
            rank is not None
            and rank_deployment_scenario
            and not _contract_lots_changed(contract)
            and not reason
            and not _contract_has_no_change_explanation(contract)
        ):
            hard_failures.append(f"mechanism_rank_without_contract_effect_or_reason:{label}:rank={rank}")

        selected = _capital_deployment_selected(contract)
        if selected is False and not reason:
            hard_failures.append(f"mechanism_unselected_candidate_missing_capital_reason:{label}:rank={rank or 'missing'}")

        if _has_hold_exit_learning(contract) and not _hold_exit_landed_in_position(contract) and not _hold_exit_has_explanation(contract):
            hard_failures.append(f"mechanism_hold_exit_learning_not_landed:{label}")

        if _is_conditional_monitor(contract):
            auditor_verdict = _auditor_verdict_from_recommendation(recommendation)
            if auditor_verdict in {"block", "require_review"}:
                if not _auditor_block_reason_present(recommendation):
                    hard_failures.append(f"mechanism_conditional_probe_auditor_block_missing_reason:{label}")
            elif not _has_intraday_decision(intraday_decisions, recommendation_id):
                hard_failures.append(f"mechanism_conditional_probe_missing_intraday_result:{label}")

        score = _opportunity_score(contract)
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
        (_date10(row.get("trading_date")), str(row.get("ticker") or "").upper()): _float(row.get("daily_pnl"))
        for row in ticker_daily_pnl
    }
    top_rank_pnls: List[float] = []
    low_rank_pnls: List[float] = []
    for recommendation_id, recommendation in recommendations.items():
        contract = _contract_from_recommendation(recommendation)
        rank = _rank_value(contract)
        if rank is None:
            continue
        day = _date10(recommendation.get("trading_date"))
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "").upper()
        pnl = pnl_by_day_ticker.get((day, ticker))
        if pnl is None:
            continue
        if rank <= 3:
            top_rank_pnls.append(pnl)
        elif rank >= 6:
            low_rank_pnls.append(pnl)
    if top_rank_pnls and sum(top_rank_pnls) < 0:
        diagnostics.append(
            f"diagnostic_top_rank_bucket_negative_pnl:pnl={sum(top_rank_pnls):.2f}:count={len(top_rank_pnls)}"
        )
    if top_rank_pnls and low_rank_pnls and sum(top_rank_pnls) < sum(low_rank_pnls):
        diagnostics.append(
            "diagnostic_low_rank_outperformed_top_rank:"
            f"top={sum(top_rank_pnls):.2f}:low={sum(low_rank_pnls):.2f}"
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
            "action_value_to_pm",
            "pm_learning_to_score",
            "score_to_rank",
            "rank_to_final_action_contract",
            "hold_exit_learning_to_position",
            "conditional_probe_to_trader_result",
            "capital_deployment_explainability",
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
