from __future__ import annotations

"""Phase4 research learning and future-memory generation helpers.

The researcher runs only after reviewer validation has established the day's
settled facts. It may call an LLM to produce research hypotheses, but it does
not validate accounting, write transactions, or issue trading instructions.
"""

import json
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from database.artifact_store import externalize_json_for_db, load_externalized_json
from llm.prompt import (
    build_researcher_causal_review_prompt,
    build_researcher_exploratory_prompt,
)
from tools.common.learning_contract import CONTRACT_KEY, build_next_round_memory_contract
from tools.common.final_action_semantics import derive_research_fact_state
from tools.common.contracts import validate_final_action_contract
from tools.common.signal_evidence_collection import (
    ANALYST_ORDER,
    validate_action_evidence_contract,
    validate_signal_collection_contract,
)
from tools.common.alpha_setup import (
    build_scope_key as build_alpha_setup_scope_key,
    upsert_alpha_setup_sample_and_profile,
)
from tools.agent_tools.analysis.analyst_data_usage import data_usage_from_snapshot
from tools.agent_tools.research import research_memory_writers
from tools.common.order_semantics import recommendation_intent_from_lots
from tools.common.learning_identity import canonical_market_regime
from util.futures_audit import categorize_no_trade_reason
from util.logger import logger


class CausalReviewLLMOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary_cause: str = Field(default="unknown")
    direction_error: bool = Field(default=False)
    horizon_error: bool = Field(default=False)
    entry_error: bool = Field(default=False)
    exit_error: bool = Field(default=False)
    position_sizing_error: bool = Field(default=False)
    auditor_error: bool = Field(default=False)
    pm_error: bool = Field(default=False)
    missed_factors: List[str] = Field(default_factory=list)
    analyst_lessons: List[str] = Field(default_factory=list)
    do_not_trade_reason: str = Field(default="")
    similar_case_key: str = Field(default="")
    confidence_score: float = Field(default=0.0)
    setup_type: str = Field(default="")
    future_use_scope: str = Field(default="")
    next_analyst_checks: List[str] = Field(default_factory=list)
    pm_action_hint: str = Field(default="")
    position_effect_limit: str = Field(default="candidate_memory_only_no_direct_sizing")
    invalid_if: List[str] = Field(default_factory=list)
    promotion_or_demotion_rule: str = Field(default="")
    expected_trade_behavior_change: str = Field(default="")


class ExploratoryHypothesisItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis_text: str = Field(default="")
    ticker: str = Field(default="*")
    sector: str = Field(default="*")
    side: str = Field(default="*")
    horizon_class: str = Field(default="*")
    market_regime: str = Field(default="*")
    setup_type: str = Field(default="*")
    support_episode_ids: List[str] = Field(default_factory=list)
    evidence_summary: str = Field(default="")
    suggested_use: str = Field(default="structured research hypothesis only; validate with future samples")
    entry_timing_hint: str = Field(default="")
    exit_timing_hint: str = Field(default="")
    holding_period_hint: str = Field(default="")
    invalidation_condition: str = Field(default="")
    validation_plan: str = Field(default="")
    confidence_score: float = Field(default=0.0)


class ExploratoryHypothesisLLMOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypotheses: List[ExploratoryHypothesisItem] = Field(default_factory=list)
    researcher_note: str = Field(default="")

    @property
    def reviewer_note(self) -> str:
        """Backward-compatible alias for older tests/artifacts."""
        return self.researcher_note


from tools.agent_tools.research import research_review_helpers as _review_helpers


_CAUSAL_PM_ACTION_HINTS = {
    "watchlist",
    "probe",
    "open",
    "add",
    "reduce",
    "exit",
    "hold",
    "no_trade",
}
_POSITION_EFFECT_LIMITS = {
    "candidate_memory_only_no_direct_sizing",
    "candidate_memory_only",
    "probe_only_until_validated",
    "reduce_or_exit_bias",
    "may_support_alpha_scaling_after_validation",
}


def _validate_researcher_input_facts(
    *,
    cursor: sqlite3.Cursor,
    config_id: str,
    trading_date: str,
    previous_trading_dates_by_ticker: Mapping[str, str],
    settlement_row: Optional[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    transactions_by_recommendation: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> None:
    """Reject incomplete, future-dated, or unsigned facts before any LLM call/write."""
    errors: list[str] = []
    date_text = str(trading_date or "")[:10]
    transactions_by_recommendation = transactions_by_recommendation or {}
    formal_previous_dates = {
        str(ticker or "").upper(): str(value or "")[:10]
        for ticker, value in (previous_trading_dates_by_ticker or {}).items()
        if str(ticker or "").strip()
    }
    if not isinstance(settlement_row, dict) or not settlement_row:
        errors.append("settlement_missing")
    else:
        settlement_date = str(settlement_row.get("trading_date") or "")[:10]
        if settlement_date != date_text:
            errors.append("settlement_date_mismatch")
    if not strategy_recommendations:
        errors.append("strategy_recommendations_missing")
    for recommendation in strategy_recommendations or []:
        ticker = str(recommendation.get("underlying_code") or "unknown")
        ticker_key = ticker.upper()
        recommendation_id = str(recommendation.get("id") or "")
        if str(recommendation.get("config_id") or "") != str(config_id or ""):
            errors.append(f"recommendation_config_mismatch:{ticker}")
        recommendation_trading_date = str(recommendation.get("trading_date") or "")[:10]
        recommendation_date = str(recommendation.get("effective_trade_date") or "")[:10]
        if recommendation_trading_date != date_text:
            errors.append(f"recommendation_date_mismatch:{ticker}")
        if recommendation_date != date_text:
            errors.append(f"recommendation_effective_date_mismatch:{ticker}")
        reference_portfolio_id = str(recommendation.get("reference_portfolio_id") or "")
        if not reference_portfolio_id:
            errors.append(f"reference_portfolio_id_missing:{ticker}")
        formal_previous_date = formal_previous_dates.get(ticker_key, "")
        if not formal_previous_date:
            errors.append(f"formal_previous_trading_date_missing:{ticker}")
        snapshot = _review_helpers._json_loads(recommendation.get("signal_snapshot")) or {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        scc = snapshot.get("signal_collection_contract")
        contract = snapshot.get("final_action_contract")
        if not isinstance(scc, dict) or not scc:
            errors.append(f"scc_missing:{ticker}")
        else:
            try:
                validate_signal_collection_contract(
                    scc,
                    ticker=ticker,
                    trading_date=recommendation_date,
                    enabled_analysts=ANALYST_ORDER,
                    require_signal_record_ids=True,
                )
            except ValueError:
                errors.append(f"scc_invalid:{ticker}")
            for source in scc.get("source_contracts") or []:
                if not isinstance(source, dict):
                    continue
                analyst = str(source.get("analyst") or "")
                signal_record_id = str(source.get("signal_record_id") or "")
                if not signal_record_id:
                    errors.append(f"signal_record_id_missing:{ticker}:{analyst}")
                    continue
                cursor.execute(
                    """
                    SELECT s.id, s.portfolio_id, s.analyst, s.ticker,
                           s.artifact_json, s.artifact_json_artifact_path,
                           s.artifact_json_sha256,
                           p.config_id,
                           substr(p.trading_date, 1, 10) AS reference_portfolio_date
                    FROM signal s
                    JOIN portfolio p ON p.id = s.portfolio_id
                    WHERE s.id = ?
                    """,
                    (signal_record_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    errors.append(f"signal_record_not_found:{ticker}:{analyst}")
                    continue
                row_map = dict(row)
                if str(row_map.get("analyst") or "") != analyst:
                    errors.append(f"signal_record_analyst_mismatch:{ticker}:{analyst}")
                if str(row_map.get("ticker") or "").upper() != ticker.upper():
                    errors.append(f"signal_record_ticker_mismatch:{ticker}:{analyst}")
                if str(row_map.get("config_id") or "") != str(config_id or ""):
                    errors.append(f"signal_record_config_mismatch:{ticker}:{analyst}")
                if str(row_map.get("portfolio_id") or "") != reference_portfolio_id:
                    errors.append(f"signal_record_reference_portfolio_mismatch:{ticker}:{analyst}")
                if str(row_map.get("reference_portfolio_date") or "") != formal_previous_date:
                    errors.append(f"reference_portfolio_date_mismatch:{ticker}:{analyst}")
                artifact = load_externalized_json(
                    row_map.get("artifact_json"),
                    row_map.get("artifact_json_artifact_path"),
                    row_map.get("artifact_json_sha256"),
                )
                persisted_aec = (
                    (artifact.get("metadata") or {}).get("action_evidence_contract")
                    if isinstance(artifact, dict)
                    else None
                )
                try:
                    persisted_aec = validate_action_evidence_contract(
                        persisted_aec,
                        analyst=analyst,
                    )
                except ValueError:
                    errors.append(f"signal_record_aec_invalid:{ticker}:{analyst}")
                    continue
                data_usage = persisted_aec.get("data_usage_summary") or {}
                if str(data_usage.get("trading_date") or "")[:10] != recommendation_date:
                    errors.append(f"signal_record_aec_date_mismatch:{ticker}:{analyst}")
                if str(data_usage.get("ticker") or "").upper() != ticker_key:
                    errors.append(f"signal_record_aec_ticker_mismatch:{ticker}:{analyst}")
                source_aec = source.get("action_evidence_contract")
                if persisted_aec != source_aec:
                    errors.append(f"signal_record_aec_lineage_mismatch:{ticker}:{analyst}")
        if not isinstance(contract, dict) or not contract:
            errors.append(f"final_action_contract_missing:{ticker}")
        else:
            for error in validate_final_action_contract(contract):
                errors.append(f"final_action_contract_invalid:{ticker}:{error}")
        audit_payload = _review_helpers._json_loads(recommendation.get("audit_payload")) or {}
        if not isinstance(audit_payload, dict) or not audit_payload:
            errors.append(f"audit_fact_missing:{ticker}")
        else:
            for field in (
                "producer",
                "audit_verdict",
                "hard_risk_reasons",
                "soft_risk_reasons",
                "source",
                "boundary",
                "contract_summary",
                "semantic_state",
            ):
                if field not in audit_payload:
                    errors.append(f"audit_payload_field_missing:{ticker}:{field}")
            if audit_payload.get("producer") != "auditor":
                errors.append(f"audit_payload_producer_invalid:{ticker}")
            if str(audit_payload.get("recommendation_id") or "") != recommendation_id:
                errors.append(f"audit_payload_recommendation_mismatch:{ticker}")
        execution_result = snapshot.get("execution_result")
        if not isinstance(execution_result, dict):
            errors.append(f"execution_result_missing:{ticker}")
        else:
            transactions = list(transactions_by_recommendation.get(recommendation_id) or [])
            try:
                transaction_count = int(execution_result.get("transaction_count"))
            except (TypeError, ValueError):
                transaction_count = -1
                errors.append(f"execution_result_transaction_count_invalid:{ticker}")
            if transaction_count != len(transactions):
                errors.append(f"execution_transaction_count_mismatch:{ticker}")
            actual_transactions = execution_result.get("actual_transactions")
            if not isinstance(actual_transactions, list):
                errors.append(f"execution_result_actual_transactions_invalid:{ticker}")
            elif len(actual_transactions) != max(0, transaction_count):
                errors.append(f"execution_result_actual_transactions_mismatch:{ticker}")
            for transaction in transactions:
                if str(transaction.get("recommendation_id") or "") != recommendation_id:
                    errors.append(f"transaction_recommendation_mismatch:{ticker}")
                if str(transaction.get("trading_date") or "")[:10] != recommendation_date:
                    errors.append(f"transaction_date_mismatch:{ticker}")
    if errors:
        raise ValueError("researcher_input_fact_validation_failed:" + ",".join(sorted(set(errors))))


def _validate_causal_llm_output(output: CausalReviewLLMOutput) -> dict:
    payload = output.model_dump()
    confidence = _review_helpers._safe_float(payload.get("confidence_score"), -1.0)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("researcher_causal_confidence_out_of_range")
    if not str(payload.get("primary_cause") or "").strip():
        raise ValueError("researcher_causal_primary_cause_missing")
    action_hint = str(payload.get("pm_action_hint") or "").strip().lower()
    if action_hint and action_hint not in _CAUSAL_PM_ACTION_HINTS:
        raise ValueError("researcher_causal_pm_action_hint_invalid")
    position_limit = str(payload.get("position_effect_limit") or "").strip()
    if position_limit not in _POSITION_EFFECT_LIMITS:
        raise ValueError("researcher_causal_position_effect_limit_invalid")
    payload["confidence_score"] = confidence
    return payload


def _episode_matches_hypothesis_scope(
    episode: Mapping[str, Any],
    hypothesis: Mapping[str, Any],
) -> bool:
    hypothesis_ticker = str(hypothesis.get("ticker") or "*").strip().upper()
    identity_keys = (
        ("ticker",) if hypothesis_ticker not in {"", "*"} else ("sector",)
    ) + ("side", "setup_type", "market_regime")
    for key in identity_keys:
        if key == "ticker":
            expected = hypothesis_ticker
            actual = str(episode.get(key) or "").strip().upper()
        elif key == "market_regime":
            expected = canonical_market_regime(hypothesis.get(key), "*")
            actual = canonical_market_regime(episode.get(key), "unknown")
        else:
            expected = str(hypothesis.get(key) or "*").strip().lower()
            actual = str(episode.get(key) or "").strip().lower()
        if expected not in {"", "*"} and actual != expected:
            return False
    return True


def _validate_exploratory_llm_output(
    output: ExploratoryHypothesisLLMOutput,
    *,
    episodes_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict]:
    validated: list[dict] = []
    forbidden_text = ("target_lots", "lots=", "margin_ratio", "direct trade authority")
    for item in output.hypotheses or []:
        payload = item.model_dump()
        text = str(payload.get("hypothesis_text") or "").strip()
        evidence = str(payload.get("evidence_summary") or "").strip()
        validation_plan = str(payload.get("validation_plan") or "").strip()
        side = str(payload.get("side") or "*").strip().lower()
        confidence = _review_helpers._safe_float(payload.get("confidence_score"), -1.0)
        support_episode_ids = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in (payload.get("support_episode_ids") or [])
                if str(item or "").strip()
            )
        )
        if not text or not evidence or not validation_plan:
            continue
        if side not in {"long", "short", "flat", "*"}:
            continue
        if not 0.0 <= confidence <= 1.0:
            continue
        combined = " ".join(str(value or "") for value in payload.values()).lower()
        if any(token in combined for token in forbidden_text):
            continue
        if episodes_by_id is not None:
            support_episode_ids = [
                episode_id
                for episode_id in support_episode_ids
                if episode_id in episodes_by_id
                and _episode_matches_hypothesis_scope(
                    episodes_by_id[episode_id],
                    payload,
                )
            ]
            if not support_episode_ids:
                continue
        payload["side"] = side
        payload["confidence_score"] = confidence
        payload["support_episode_ids"] = support_episode_ids
        validated.append(payload)
    return validated


def _learning_safe_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return dict(snapshot or {})


def _build_causal_evidence_pack(
    *,
    config_id: str,
    trading_date: str,
    strategy_recommendations: List[Dict[str, Any]],
    settlement_row: Optional[Dict[str, Any]],
    no_trade_reason_counter: Counter,
) -> Dict[str, Any]:
    return {
        "agent_name": "researcher",
        "evidence_pack_id": str(uuid.uuid4()),
        "config_id": config_id,
        "trading_date": trading_date,
        "pre_trade_evidence": [
            {
                "recommendation_id": row.get("id"),
                "ticker": row.get("underlying_code"),
                "action": row.get("action"),
                "lots": row.get("lots"),
                "signal_snapshot": _learning_safe_snapshot(_review_helpers._recommendation_snapshot(row)),
            }
            for row in strategy_recommendations
        ],
        "post_trade_outcome": {
            "daily_pnl": _review_helpers._safe_float((settlement_row or {}).get("daily_pnl")),
            "commission": _review_helpers._safe_float((settlement_row or {}).get("commission")),
            "current_margin_ratio": _review_helpers._safe_float((settlement_row or {}).get("margin_ratio")),
            "no_trade_reasons": dict(no_trade_reason_counter),
            "no_trade_reason_categories": _no_trade_reason_category_counts(no_trade_reason_counter),
        },
    }


def _no_trade_reason_category_counts(no_trade_reason_counter: Counter) -> Dict[str, int]:
    category_counter: Counter = Counter()
    for reason, count in (no_trade_reason_counter or Counter()).items():
        category = categorize_no_trade_reason(reason)["category"]
        category_counter[category] += int(count or 0)
    return {str(key): int(value) for key, value in category_counter.most_common()}


def _ticker_daily_outcome(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    ticker: str,
) -> Dict[str, Any]:
    """Return settled ticker-day outcome for setup learning.

    Futures transactions do not carry the full marked-to-market result for a
    ticker-day.  The accountant writes that fact into ticker_daily_pnl, so
    setup learning must read this table instead of treating transaction
    realized_pnl as the final outcome.
    """

    try:
        cursor.execute(
            """
            SELECT
                SUM(COALESCE(tdp.daily_pnl, 0)) AS daily_pnl,
                SUM(COALESCE(tdp.commission, 0)) AS commission,
                SUM(COALESCE(tdp.holding_pnl, 0)) AS holding_pnl,
                SUM(COALESCE(tdp.new_position_pnl, 0)) AS new_position_pnl,
                SUM(COALESCE(tdp.close_pnl, 0)) AS close_pnl,
                SUM(ABS(COALESCE(tdp.lots, 0))) AS abs_lots,
                COUNT(*) AS row_count
            FROM ticker_daily_pnl tdp
            JOIN portfolio p ON tdp.portfolio_id = p.id
            WHERE p.config_id = ?
              AND tdp.trading_date = ?
              AND UPPER(tdp.ticker) = ?
            """,
            (config_id, str(trading_date)[:10], str(ticker or "").upper()),
        )
        row = cursor.fetchone()
    except Exception:
        row = None
    data = dict(row) if row else {}
    row_count = int(data.get("row_count") or 0)
    return {
        "daily_pnl": float(data.get("daily_pnl") or 0.0),
        "commission": float(data.get("commission") or 0.0),
        "holding_pnl": float(data.get("holding_pnl") or 0.0),
        "new_position_pnl": float(data.get("new_position_pnl") or 0.0),
        "close_pnl": float(data.get("close_pnl") or 0.0),
        "abs_lots": int(float(data.get("abs_lots") or 0.0)),
        "row_count": row_count,
        "source": "ticker_daily_pnl" if row_count else "transactions_or_no_trade",
    }


def _dict_or_empty(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _clean_execution_token(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    if not text:
        text = default
    text = text.replace(" ", "_").replace("/", "_").replace(":", "_")
    return "".join(ch for ch in text if ch.isalnum() or ch in {"_", "-"})


def _valid_execution_retrieval_key(value: Any) -> str:
    key = str(value or "").strip()
    parts = [part.strip() for part in key.split("|")]
    if len(parts) != 4 or parts[-1].lower() != "execution":
        return ""
    if any(part.lower() in {"", "*", "unknown"} for part in parts[:-1]):
        return ""
    return key


def _valid_fac_setup_type(value: Any) -> str:
    setup_type = str(value or "").strip()
    if setup_type.lower() in {"", "*", "unknown", "generic_trade_setup"}:
        return ""
    return setup_type


def _table_columns(cursor: sqlite3.Cursor, table_name: str) -> set[str]:
    try:
        cursor.execute(f"PRAGMA table_info({table_name})")
        return {str(row[1]) for row in cursor.fetchall()}
    except Exception:
        return set()


def _opening_fac_learning_identity(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    ticker: str,
    trading_date: str,
    current_lots: int,
) -> Dict[str, Any]:
    """Resolve the pre-decision position to its complete opening strategy FAC identity."""

    current_side = "long" if int(current_lots or 0) > 0 else "short" if int(current_lots or 0) < 0 else ""
    decision_day = str(trading_date or "")[:10]
    if not config_id or not ticker or not current_side or not decision_day:
        raise RuntimeError("research_opening_fac_context_inputs_missing")
    transaction_columns = _table_columns(cursor, "futures_transactions")
    recommendation_columns = _table_columns(cursor, "futures_recommendation")
    required_transaction_columns = {
        "id",
        "config_id",
        "recommendation_id",
        "trading_date",
        "ticker",
        "action",
        "lots",
        "source_type",
        "created_at",
    }
    required_recommendation_columns = {
        "id",
        "signal_snapshot",
    }
    if not required_transaction_columns.issubset(transaction_columns) or not required_recommendation_columns.issubset(recommendation_columns):
        raise RuntimeError("research_opening_fac_lineage_schema_missing")
    cursor.execute(
        """
        SELECT *
        FROM futures_transactions
        WHERE config_id = ?
          AND UPPER(ticker) = UPPER(?)
          AND substr(trading_date, 1, 10) < ?
        ORDER BY substr(trading_date, 1, 10),
                 created_at,
                 CASE
                     WHEN lower(COALESCE(source_type, '')) = 'rollover'
                          AND lower(action) IN ('close_long', 'close_short') THEN 0
                     WHEN lower(COALESCE(source_type, '')) = 'rollover'
                          AND lower(action) IN ('open_long', 'open_short') THEN 1
                     ELSE 0
                 END,
                 id
        """,
        (config_id, ticker, decision_day),
    )
    active: Dict[str, List[Dict[str, Any]]] = {"long": [], "short": []}
    rollover_transfers: Dict[tuple[str, str], List[Dict[str, Any]]] = {}

    def consume_active(side: str, lots: int) -> List[Dict[str, Any]]:
        consumed_segments: List[Dict[str, Any]] = []
        remaining = lots
        while remaining > 0 and active[side]:
            first = active[side][0]
            consumed = min(remaining, int(first.get("remaining_lots") or 0))
            consumed_segments.append({
                "remaining_lots": consumed,
                "row": dict(first.get("row") or {}),
            })
            first["remaining_lots"] = int(first.get("remaining_lots") or 0) - consumed
            remaining -= consumed
            if int(first.get("remaining_lots") or 0) <= 0:
                active[side].pop(0)
        if remaining > 0:
            raise RuntimeError("research_opening_fac_lineage_missing")
        return consumed_segments

    for raw_row in cursor.fetchall():
        row = dict(raw_row)
        source_type = str(row.get("source_type") or "strategy").strip().lower()
        action = str(row.get("action") or "").strip().lower()
        lots = max(0, abs(_review_helpers._safe_int(row.get("lots"), 0)))
        if lots <= 0:
            continue
        if action in {"open_long", "open_short"}:
            side = "long" if action == "open_long" else "short"
            if source_type == "rollover":
                recommendation_id = str(row.get("recommendation_id") or "").strip()
                transfer_queue = rollover_transfers.get((recommendation_id, side)) or []
                origin_row = dict(
                    (transfer_queue[0].get("row") if transfer_queue else {}) or {}
                )
                if not recommendation_id or not origin_row:
                    raise RuntimeError("research_rollover_open_lineage_missing")
                remaining = lots
                while remaining > 0 and transfer_queue:
                    first = transfer_queue[0]
                    transferred = min(remaining, int(first.get("remaining_lots") or 0))
                    active[side].append({
                        "remaining_lots": transferred,
                        "row": dict(first.get("row") or {}),
                    })
                    first["remaining_lots"] = int(first.get("remaining_lots") or 0) - transferred
                    remaining -= transferred
                    if int(first.get("remaining_lots") or 0) <= 0:
                        transfer_queue.pop(0)
                if remaining > 0:
                    active[side].append({
                        "remaining_lots": remaining,
                        "row": origin_row,
                    })
                continue
            if source_type == "strategy":
                active[side].append({"remaining_lots": lots, "row": row})
            continue
        if action not in {"close_long", "close_short"}:
            continue
        side = "long" if action == "close_long" else "short"
        consumed_segments = consume_active(side, lots)
        if source_type == "rollover":
            recommendation_id = str(row.get("recommendation_id") or "").strip()
            if not recommendation_id:
                raise RuntimeError("research_rollover_recommendation_missing")
            rollover_transfers.setdefault((recommendation_id, side), []).extend(consumed_segments)

    candidates = [item for item in active[current_side] if int(item.get("remaining_lots") or 0) > 0]
    if not candidates:
        raise RuntimeError("research_opening_fac_lineage_missing")
    recommendation_id = str((candidates[0].get("row") or {}).get("recommendation_id") or "").strip()
    if not recommendation_id:
        raise RuntimeError("research_opening_fac_recommendation_missing")
    select_columns = ["signal_snapshot"]
    for column in ("signal_snapshot_artifact_path", "signal_snapshot_sha256"):
        if column in recommendation_columns:
            select_columns.append(column)
    cursor.execute(
        f"SELECT {', '.join(select_columns)} FROM futures_recommendation WHERE id = ?",
        (recommendation_id,),
    )
    raw_recommendation = cursor.fetchone()
    if raw_recommendation is None:
        raise RuntimeError("research_opening_fac_recommendation_missing")
    recommendation = dict(raw_recommendation)
    opening_snapshot = _review_helpers._recommendation_snapshot(recommendation)
    opening_identity = _review_helpers._fac_learning_identity(opening_snapshot)
    if not bool(opening_identity.get("complete")):
        missing = ",".join(opening_identity.get("missing_fields") or [])
        raise RuntimeError(f"research_opening_fac_identity_missing:{missing}")
    return {
        key: opening_identity.get(key)
        for key in (
            "setup_type",
            "horizon_class",
            "expected_horizon_days",
            "market_regime",
        )
    }


def _execution_learning_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Extract Trader execution feedback for Phase4 action-value learning.

    The PM writes an execution plan before Phase2, while Trader writes the
    actual trigger/fill status during Phase2.  Researcher must learn from the
    Trader facts, not merely from the pre-open plan.
    """

    phase2 = _dict_or_empty(snapshot.get("phase2_execution"))
    setup_learning = _dict_or_empty(phase2.get("setup_execution_learning"))
    translation = _dict_or_empty(snapshot.get("execution_translation"))
    execution_result = _dict_or_empty(snapshot.get("execution_result"))
    execution_learning_trace = _dict_or_empty(
        execution_result.get("execution_learning_trace")
    )
    final_contract = _dict_or_empty(snapshot.get("final_action_contract"))

    intraday_selection = (
        _dict_or_empty(phase2.get("intraday_selection"))
        or _dict_or_empty(setup_learning.get("intraday_selection"))
        or _dict_or_empty(translation.get("intraday_selection"))
    )
    execution_contract = (
        _dict_or_empty(phase2.get("execution_contract"))
        or _dict_or_empty(setup_learning.get("execution_contract"))
        or _dict_or_empty(translation.get("execution_contract"))
        or {
            key: final_contract.get(key)
            for key in (
                "execution_profile",
                "trigger_source",
                "entry_trigger",
                "invalidation",
                "valid_until",
                "requires_intraday_confirmation",
                "can_execute_without_intraday_trigger",
                "execution_action_value_preference",
            )
            if final_contract.get(key) not in (None, "", [])
        }
    )
    semantic_state = derive_research_fact_state(final_contract, execution_result)

    has_trader_feedback = bool(phase2 or setup_learning or intraday_selection or execution_result)
    if not has_trader_feedback:
        return {}

    status = (
        phase2.get("status")
        or setup_learning.get("phase2_status")
        or execution_result.get("status")
        or execution_result.get("outcome")
        or "unknown"
    )
    reason = (
        phase2.get("reason")
        or setup_learning.get("no_trade_reason")
        or intraday_selection.get("reason")
        or execution_result.get("reason")
        or execution_result.get("outcome")
        or ""
    )
    profile = (
        execution_contract.get("execution_profile")
        or _dict_or_empty(intraday_selection.get("features")).get("execution_profile")
        or "unknown"
    )
    action_taken = "execution"
    if reason:
        action_taken = f"execution_{_clean_execution_token(reason)}"
    elif profile:
        action_taken = f"execution_{_clean_execution_token(profile)}"
    return {
        "has_execution_feedback": True,
        "action_taken": action_taken,
        "execution_profile": str(profile or "unknown"),
        "phase2_status": str(status or "unknown"),
        "reason": str(reason or ""),
        "execution_contract": execution_contract,
        "setup_execution_learning": setup_learning,
        "intraday_selection": intraday_selection,
        "execution_result": execution_result,
        "execution_retrieval_key": str(
            execution_learning_trace.get("execution_retrieval_key") or ""
        ).strip(),
        "semantic_state": semantic_state,
        "trigger_checked": bool(intraday_selection.get("trigger_checked")),
        "trigger_passed": bool(intraday_selection.get("trigger_passed")),
        "price_chase_check": intraday_selection.get("price_chase_check"),
        "execution_failure_reason": intraday_selection.get("execution_failure_reason") or reason,
        "missed_opportunity_flag": bool(intraday_selection.get("missed_opportunity_flag")),
    }


def _counterfactual_result_value(item: Any) -> float:
    if not isinstance(item, dict):
        return 0.0
    return _review_helpers._safe_float(
        item.get("counterfactual_pnl"),
        _review_helpers._safe_float(item.get("pnl"), _review_helpers._safe_float(item.get("reward"))),
    )


def _latest_counterfactual_result(results: Any) -> Dict[str, Any]:
    if not isinstance(results, list):
        return {}
    candidates = [item for item in results if isinstance(item, dict)]
    if not candidates:
        return {}
    return max(candidates, key=lambda item: int(float(item.get("horizon_days") or 0)))


def _write_counterfactual_no_trade_alpha_setup_samples(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    """Bridge settled no-trade counterfactual outcomes into setup action-value samples.

    The sample date is the review/backfill date, not the original no-trade date,
    so future PM SQL-RAG queries cannot see the counterfactual outcome before it
    was known.  These samples are marked as counterfactual prior only; they can sharpen
    future action preference but cannot grant direct real-budget authority.
    """

    learning_cfg = cfg.get("learning", {}) or {}
    profile_cfg = learning_cfg.get("alpha_setup_profile", {}) or {}
    if not bool(profile_cfg.get("enabled", True)):
        return {"rows": 0, "status": "disabled"}

    trading_day = str(trading_date)[:10]
    max_rows = max(1, _review_helpers._safe_int(profile_cfg.get("max_counterfactual_no_trade_samples_per_day"), 40))
    try:
        cursor.execute(
            """
            SELECT nt.*
            FROM no_trade_opportunity_memory nt
            WHERE config_id = ?
              AND status = 'closed'
              AND classification IN ('missed_opportunity', 'correct_avoidance')
              AND counterfactual_results_json IS NOT NULL
              AND trading_date < ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM alpha_setup_sample s
                  WHERE s.config_id = nt.config_id
                    AND s.recommendation_id = ('counterfactual:' || nt.id)
                    AND s.source_type LIKE 'counterfactual_%'
              )
            ORDER BY trading_date DESC, ticker
            LIMIT ?
            """,
            (config_id, trading_day, max_rows),
        )
        rows = [dict(row) for row in cursor.fetchall()]
    except Exception:
        return {"rows": 0, "status": "no_no_trade_memory_table"}

    inserted = 0
    lifecycle_counts: Counter = Counter()
    samples: List[Dict[str, Any]] = []
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        side = str(row.get("side") or "").lower()
        if not ticker or side not in {"long", "short"}:
            continue
        results = _review_helpers._json_loads(row.get("counterfactual_results_json")) or []
        selected_result = _latest_counterfactual_result(results)
        if not selected_result:
            continue
        counterfactual_pnl = _counterfactual_result_value(selected_result)
        classification = str(row.get("classification") or "")
        source_type = "counterfactual_missed_alpha" if classification == "missed_opportunity" else "counterfactual_reasonable_avoidance"
        memory_payload = _load_episode_payload(row)
        original_snapshot = (
            memory_payload.get("signal_snapshot")
            if isinstance(memory_payload.get("signal_snapshot"), dict)
            else {}
        )
        fac_identity = _review_helpers._fac_learning_identity(original_snapshot)
        if not bool(fac_identity.get("complete")):
            continue
        horizon = str(fac_identity.get("horizon_class") or "")
        regime = str(fac_identity.get("market_regime") or "")
        setup_type = str(fac_identity.get("setup_type") or "")
        signal_combo = row.get("signal_combo")
        if isinstance(signal_combo, str):
            parsed_combo = _review_helpers._json_loads(signal_combo)
            combo_items = parsed_combo if isinstance(parsed_combo, list) else [signal_combo]
        elif isinstance(signal_combo, list):
            combo_items = signal_combo
        else:
            combo_items = []
        combo_key = "_".join(str(item).lower() for item in combo_items[:4]) or "unknown_combo"
        data_combo = f"counterfactual_no_trade_{classification}_{combo_key}"[:160]
        scope_key = build_alpha_setup_scope_key(
            ticker=ticker,
            side=side,
            horizon_class=horizon,
            market_regime=regime,
            setup_type=setup_type,
            data_combo=data_combo,
        )
        action_taken = "open_long" if side == "long" else "open_short"
        sample = {
            "ticker": ticker,
            "side": side,
            "sector": row.get("sector") or _review_helpers._sector_for_ticker(cfg, ticker),
            "horizon_class": horizon,
            "expected_horizon_days": _review_helpers._safe_int(
                fac_identity.get("expected_horizon_days"),
                0,
            ),
            "market_regime": regime,
            "setup_type": setup_type,
            "data_combo": data_combo,
            "scope_key": scope_key,
            "source_type": source_type,
            "recommendation_id": f"counterfactual:{row.get('id')}",
            "action_taken": action_taken,
            "pm_action": "counterfactual_counterfactual_open",
            "auditor_decision": "not_executed_counterfactual",
            "trader_status": "counterfactual_not_executed",
            "target_lots": _review_helpers._safe_int(row.get("counterfactual_lots"), _review_helpers._safe_int(row.get("candidate_lots"), 1)),
            "current_lots": 0,
            "executed_lots": 0,
            "net_pnl": counterfactual_pnl,
            "commission": 0.0,
            "holding_days": _review_helpers._safe_int(selected_result.get("horizon_days")),
            "outcome_label": "profit" if counterfactual_pnl > 0 else "loss" if counterfactual_pnl < 0 else "flat_or_no_trade",
            "setup_quality_score": 0.0,
            "opportunity_state": row.get("opportunity_state") or "watch_for_trigger",
            "evidence": {
                "source": "no_trade_opportunity_memory_counterfactual",
                "memory_id": row.get("id"),
                "original_opportunity_date": str(row.get("trading_date") or "")[:10],
                "classification": classification,
                "pm_reason": row.get("pm_reason"),
                "auditor_reason": row.get("auditor_reason"),
                "execution_reason": row.get("execution_reason"),
                "evidence_summary": row.get("evidence_summary"),
                "counterfactual_entry_price": row.get("counterfactual_entry_price"),
                "counterfactual_boundary": "prior_only_no_direct_authority",
            },
            "result": {
                "counterfactual_results": results,
                "selected_counterfactual_result": selected_result,
                "counterfactual_pnl": counterfactual_pnl,
                "counterfactual_reward_source": "counterfactual_no_trade_memory",
                "sample_date_policy": "review_date_not_original_opportunity_date",
                "no_future_leakage": True,
            },
        }
        result = upsert_alpha_setup_sample_and_profile(
            cursor,
            cfg=cfg,
            config_id=config_id,
            trading_date=trading_day,
            sample=sample,
        )
        if result.get("rows"):
            inserted += 1
            lifecycle_counts[str(result.get("lifecycle_state") or "unknown")] += 1
            samples.append(
                {
                    "ticker": ticker,
                    "side": side,
                    "setup_type": setup_type,
                    "source_type": source_type,
                    "original_opportunity_date": str(row.get("trading_date") or "")[:10],
                    "counterfactual_pnl": counterfactual_pnl,
                    "lifecycle_state": result.get("lifecycle_state"),
                }
            )
    return {
        "rows": inserted,
        "status": "applied" if inserted else "no_ready_counterfactual_samples",
        "lifecycle_counts": dict(lifecycle_counts),
        "sample_preview": samples[:10],
    }


def write_alpha_setup_profiles(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    strategy_recommendations: List[Dict[str, Any]],
    transactions_by_recommendation: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    learning_cfg = cfg.get("learning", {}) or {}
    profile_cfg = learning_cfg.get("alpha_setup_profile", {}) or {}
    if not bool(profile_cfg.get("enabled", True)):
        return {"rows": 0, "status": "disabled"}
    transactions_by_recommendation = transactions_by_recommendation or {}
    rows = 0
    lifecycle_counts: Counter = Counter()
    samples = []
    episode_samples = _episode_alpha_setup_samples(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    for recommendation in strategy_recommendations:
        snapshot = _review_helpers._recommendation_snapshot(recommendation)
        final_contract = _dict_or_empty(snapshot.get("final_action_contract"))
        if not final_contract:
            continue
        ticker = str(recommendation.get("underlying_code") or recommendation.get("ticker") or "").upper()
        if not ticker:
            continue
        side = _review_helpers._recommendation_side(recommendation, snapshot)
        semantic_state = derive_research_fact_state(final_contract, {})
        if side not in {"long", "short"}:
            preferred = str(semantic_state.get("contract_side") or "flat")
            side = preferred if preferred in {"long", "short"} else "flat"
        if side not in {"long", "short"}:
            continue
        rec_id = str(recommendation.get("id") or "")
        txs = transactions_by_recommendation.get(rec_id, [])
        fac_identity = _review_helpers._fac_learning_identity(snapshot)
        if not bool(fac_identity.get("complete")):
            continue
        horizon = str(fac_identity.get("horizon_class") or "")
        regime = str(fac_identity.get("market_regime") or "")
        template = str(fac_identity.get("setup_type") or "")
        data_usage = data_usage_from_snapshot(snapshot)
        data_combo = _review_helpers._data_combo_key(data_usage)
        analyst_payloads = _review_helpers._analyst_payloads(snapshot)
        action_contracts: Dict[str, Any] = {}
        learning_scopes: Dict[str, Any] = {}
        if isinstance(analyst_payloads, dict):
            for analyst_name, payload in analyst_payloads.items():
                if not isinstance(payload, dict):
                    continue
                action_contracts[str(analyst_name)] = payload
                if isinstance(payload.get("learning_scope"), dict):
                    learning_scopes[str(analyst_name)] = payload["learning_scope"]
        scope_tags: List[str] = []
        for analyst_name, scope in learning_scopes.items():
            if not isinstance(scope, dict):
                continue
            for key in (
                "setup_family",
                "sector_setup_alignment",
                "primary_driver_groups",
                "short_trigger_groups",
                "event_regime",
            ):
                value = scope.get(key)
                if isinstance(value, list):
                    scope_tags.extend(f"{analyst_name}:{key}:{item}" for item in value[:4])
                elif value:
                    scope_tags.append(f"{analyst_name}:{key}:{value}")
        if scope_tags:
            data_combo = f"{data_combo}|evidence:{'|'.join(sorted(set(scope_tags))[:8])}"
        opportunity_type = _review_helpers._primary_opportunity_type(snapshot, side)
        opportunity_state = _review_helpers._primary_opportunity_state(snapshot, side)
        opportunity_contract_summary = _review_helpers._opportunity_contract_summary(snapshot)
        learning_identity = {
            key: fac_identity.get(key)
            for key in (
                "setup_type",
                "horizon_class",
                "expected_horizon_days",
                "market_regime",
            )
        }
        sector = _review_helpers._sector_for_ticker(cfg, ticker)
        planned_target_lots = _review_helpers._safe_int(semantic_state.get("target_lots"))
        current_lots = _review_helpers._safe_int(semantic_state.get("current_lots"), 0)
        if current_lots != 0:
            learning_identity = _opening_fac_learning_identity(
                cursor,
                config_id=config_id,
                ticker=ticker,
                trading_date=trading_date,
                current_lots=current_lots,
            )
        setup_type = str(learning_identity.get("setup_type") or "")
        horizon = str(learning_identity.get("horizon_class") or "")
        regime = str(learning_identity.get("market_regime") or "")
        contract_intent = recommendation_intent_from_lots(
            current_lots=current_lots,
            target_lots=planned_target_lots,
        )
        contract_action_taken = str(semantic_state.get("action") or contract_intent.get("action") or "hold")
        executed_lots = 0
        actual_lots_delta = 0
        for tx in txs:
            if not isinstance(tx, dict):
                continue
            tx_lots = abs(_review_helpers._safe_int(tx.get("lots")))
            executed_lots += tx_lots
            raw_tx_action = tx.get("action")
            tx_action = str(
                getattr(raw_tx_action, "value", raw_tx_action)
                or contract_intent.get("action")
                or ""
            ).strip().lower()
            if tx_action in {"open_long", "close_short"}:
                actual_lots_delta += tx_lots
            elif tx_action in {"open_short", "close_long"}:
                actual_lots_delta -= tx_lots
        actual_target_lots = current_lots + actual_lots_delta
        actual_intent = recommendation_intent_from_lots(
            current_lots=current_lots,
            target_lots=actual_target_lots,
        )
        actual_action_taken = str(actual_intent.get("action") or "hold")
        tx_commission = sum(_review_helpers._safe_float(tx.get("commission")) for tx in txs if isinstance(tx, dict))
        ticker_outcome = _ticker_daily_outcome(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            ticker=ticker,
        )
        outcome_lots = _review_helpers._safe_int(ticker_outcome.get("abs_lots"))
        settled_position_matches = bool(
            ticker_outcome.get("row_count")
            and outcome_lots == abs(actual_target_lots)
        )
        planned_hold = bool(
            str(semantic_state.get("canonical_action_family") or "") == "hold"
            and current_lots == planned_target_lots
            and current_lots != 0
        )
        execution_result = _review_helpers._execution_result_from_snapshot(snapshot)
        source_type = (
            "trade"
            if settled_position_matches
            and (
                (executed_lots > 0 and actual_target_lots != current_lots)
                or (planned_hold and executed_lots == 0)
            )
            else "no_trade"
        )
        if source_type != "trade":
            realized_pnl = 0.0
            commission = 0.0
        elif str(actual_intent.get("action_type") or "") == "keep":
            realized_pnl = _review_helpers._safe_float(ticker_outcome.get("holding_pnl"))
            commission = 0.0
        elif str(actual_intent.get("action_type") or "") in {"reduce", "exit", "reverse"}:
            realized_pnl = _review_helpers._safe_float(ticker_outcome.get("close_pnl"))
            commission = tx_commission
        else:
            realized_pnl = _review_helpers._safe_float(ticker_outcome.get("daily_pnl"))
            commission = tx_commission
        outcome_label = "profit" if realized_pnl - commission > 0 else "loss" if realized_pnl - commission < 0 else "flat_or_no_trade"
        scorecard: Dict[str, Any] = {}
        side_scorecard: Dict[str, Any] = {}
        scope_key = build_alpha_setup_scope_key(
            ticker=ticker,
            side=side,
            horizon_class=horizon,
            market_regime=regime,
            setup_type=setup_type,
            data_combo=data_combo,
        )
        sample = {
            "ticker": ticker,
            "side": side,
            "sector": sector,
            "horizon_class": horizon,
            "expected_horizon_days": _review_helpers._safe_int(
                learning_identity.get("expected_horizon_days"),
                0,
            ),
            "market_regime": regime,
            "setup_type": setup_type,
            "data_combo": data_combo,
            "scope_key": scope_key,
            "source_type": source_type,
            "recommendation_id": rec_id,
            "action_taken": actual_action_taken,
            "pm_action": semantic_state.get("action") or contract_action_taken,
            "final_action_semantics": semantic_state,
            "auditor_decision": (
                str(final_contract.get("audit_verdict") or final_contract.get("auditor_decision") or "")
            ),
            "trader_status": execution_result.get("outcome") or execution_result.get("status") or recommendation.get("status"),
            "target_lots": actual_target_lots,
            "current_lots": current_lots,
            "executed_lots": executed_lots,
            "net_pnl": realized_pnl,
            "commission": commission,
            "holding_days": 0,
            "outcome_label": outcome_label,
            "setup_quality_score": _review_helpers._safe_float(side_scorecard.get("max_setup_quality")),
            "opportunity_state": opportunity_state,
            "evidence": {
                "analyst_payloads": analyst_payloads,
                "analyst_action_evidence_contracts": action_contracts,
                "analyst_learning_scopes": learning_scopes,
                "opportunity_states": (
                    opportunity_contract_summary.get("opportunity_states")
                    if isinstance(opportunity_contract_summary, dict)
                    else []
                ),
                "data_usage_summary": data_usage,
                "market_confirmation": _review_helpers._market_confirmation(snapshot),
                "final_action_contract": final_contract,
                "learning_boundary": {
                    "learning_source": "final_action_contract",
                    "strategy_outcome_bound_to_final_action_contract": bool(final_contract),
                },
            },
            "result": {
                "execution_result": execution_result,
                "transaction_count": len(txs),
                "ticker_daily_outcome": ticker_outcome,
                "pnl_source": ticker_outcome.get("source"),
                "no_future_leakage": True,
            },
        }
        result = upsert_alpha_setup_sample_and_profile(
            cursor,
            cfg=cfg,
            config_id=config_id,
            trading_date=trading_date,
            sample=sample,
        )
        if result.get("rows"):
            rows += 1
            lifecycle_counts[str(result.get("lifecycle_state") or "unknown")] += 1
            samples.append({
                "ticker": ticker,
                "side": side,
                "setup_type": setup_type,
                "scope_key": scope_key,
                "lifecycle_state": result.get("lifecycle_state"),
                "profile_state_hint": result.get("profile_state_hint"),
            })
        execution_learning = _execution_learning_from_snapshot(snapshot)
        execution_retrieval_key = _valid_execution_retrieval_key(
            execution_learning.get("execution_retrieval_key")
            if execution_learning
            else ""
        )
        if execution_learning and execution_retrieval_key:
            execution_data_combo = f"execution:{execution_retrieval_key}|{data_combo}"
            execution_scope_key = build_alpha_setup_scope_key(
                ticker=ticker,
                side=side,
                horizon_class=horizon,
                market_regime=regime,
                setup_type=setup_type,
                data_combo=execution_data_combo,
            )
            execution_sample = {
                **sample,
                "source_type": "execution",
                "setup_type": setup_type,
                "data_combo": execution_data_combo,
                "scope_key": execution_scope_key,
                "execution_retrieval_key": execution_retrieval_key,
                "action_taken": execution_learning["action_taken"],
                "pm_action": execution_learning.get("execution_profile") or sample.get("pm_action"),
                "trader_status": ":".join(
                    part
                    for part in (
                        str(execution_learning.get("phase2_status") or ""),
                        str(execution_learning.get("reason") or ""),
                    )
                    if part
                ),
                "evidence": {
                    "execution_contract": execution_learning.get("execution_contract") or {},
                    "setup_execution_learning": execution_learning.get("setup_execution_learning") or {},
                    "analyst_action_evidence_contracts": (
                        execution_learning.get("setup_execution_learning", {}).get("analyst_action_evidence_contracts")
                        if isinstance(execution_learning.get("setup_execution_learning"), dict)
                        else action_contracts
                    ),
                    "analyst_learning_scopes": (
                        execution_learning.get("setup_execution_learning", {}).get("analyst_learning_scopes")
                        if isinstance(execution_learning.get("setup_execution_learning"), dict)
                        else learning_scopes
                    ),
                    "analyst_payloads": analyst_payloads,
                    "market_confirmation": _review_helpers._market_confirmation(snapshot),
                    "final_action_contract": final_contract,
                    "learning_boundary": {
                        "learning_source": "final_action_contract",
                        "execution_outcome_bound_to_final_action_contract": bool(final_contract),
                    },
                },
                "result": {
                    "execution_feedback": {
                        "execution_profile": execution_learning.get("execution_profile"),
                        "phase2_status": execution_learning.get("phase2_status"),
                        "reason": execution_learning.get("reason"),
                        "trigger_checked": execution_learning.get("trigger_checked"),
                        "trigger_passed": execution_learning.get("trigger_passed"),
                        "price_chase_check": execution_learning.get("price_chase_check"),
                        "execution_failure_reason": execution_learning.get("execution_failure_reason"),
                        "missed_opportunity_flag": execution_learning.get("missed_opportunity_flag"),
                        "intraday_selection": execution_learning.get("intraday_selection") or {},
                        "execution_result": execution_learning.get("execution_result") or {},
                    },
                    "ticker_daily_outcome": ticker_outcome,
                    "pnl_source": ticker_outcome.get("source"),
                    "learning_boundary": "execution_action_value_only_same_scope_no_future_leakage",
                    "no_future_leakage": True,
                },
            }
            execution_result = upsert_alpha_setup_sample_and_profile(
                cursor,
                cfg=cfg,
                config_id=config_id,
                trading_date=trading_date,
                sample=execution_sample,
            )
            if execution_result.get("rows"):
                rows += 1
                lifecycle_counts[str(execution_result.get("lifecycle_state") or "unknown")] += 1
                samples.append({
                    "ticker": ticker,
                    "side": side,
                    "setup_type": setup_type,
                    "scope_key": execution_scope_key,
                    "source_type": "execution",
                    "lifecycle_state": execution_result.get("lifecycle_state"),
                    "profile_state_hint": execution_result.get("profile_state_hint"),
                })
    # Each row is already one complete 0 -> position -> 0 lifecycle.  Physical
    # partial-close pairs remain inside the episode payload as economic detail
    # and must not mature the setup as separate open/add samples.
    for episode_sample in episode_samples:
        result_payload = (
            episode_sample.get("result")
            if isinstance(episode_sample.get("result"), dict)
            else {}
        )
        episode_completion_date = str(
            result_payload.get("episode_completion_date")
            or result_payload.get("close_date")
            or trading_date
        )[:10]
        result = upsert_alpha_setup_sample_and_profile(
            cursor,
            cfg=cfg,
            config_id=config_id,
            trading_date=episode_completion_date,
            sample=episode_sample,
        )
        if result.get("rows"):
            rows += 1
            lifecycle_counts[str(result.get("lifecycle_state") or "unknown")] += 1
            samples.append({
                "ticker": episode_sample.get("ticker"),
                "side": episode_sample.get("side"),
                "setup_type": episode_sample.get("setup_type"),
                "scope_key": episode_sample.get("scope_key"),
                "source_type": "trade_episode",
                "trade_episode_memory_id": (
                    (episode_sample.get("result") or {}).get("episode_memory_id")
                    if isinstance(episode_sample.get("result"), dict)
                    else None
                ),
                "lifecycle_state": result.get("lifecycle_state"),
                "profile_state_hint": result.get("profile_state_hint"),
            })
    counterfactual_summary = _write_counterfactual_no_trade_alpha_setup_samples(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    if counterfactual_summary.get("rows"):
        rows += _review_helpers._safe_int(counterfactual_summary.get("rows"))
        for key, value in (counterfactual_summary.get("lifecycle_counts") or {}).items():
            lifecycle_counts[str(key)] += _review_helpers._safe_int(value)
        samples.extend(counterfactual_summary.get("sample_preview") or [])
    return {
        "rows": rows,
        "status": "applied" if rows else "no_samples",
        "lifecycle_counts": dict(lifecycle_counts),
        "sample_preview": samples[:10],
        "counterfactual_no_trade_alpha_setup": counterfactual_summary,
    }


def _load_episode_payload(row: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        payload = load_externalized_json(
            row.get("payload_json"),
            artifact_path=(
                str(row.get("payload_artifact_path"))
                if row.get("payload_artifact_path")
                else None
            ),
            expected_sha256=(
                str(row.get("payload_sha256"))
                if row.get("payload_sha256")
                else None
            ),
        )
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {}


def _episode_alpha_setup_samples(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> List[Dict[str, Any]]:
    try:
        cursor.execute(
            """
            SELECT *
            FROM trade_episode_memory
            WHERE config_id = ?
              AND COALESCE(episode_date, close_date, trading_date) = ?
            ORDER BY COALESCE(episode_date, close_date, trading_date), close_date, last_reviewed_at, id
            """,
            (config_id, str(trading_date)[:10]),
        )
        rows = [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error:
        return []
    samples: List[Dict[str, Any]] = []
    for row in rows:
        payload = _load_episode_payload(row)
        pair = payload.get("pair") if isinstance(payload.get("pair"), dict) else {}
        required_pair_facts = (
            pair.get("open_recommendation_id"),
            pair.get("open_transaction_id"),
            pair.get("close_transaction_id"),
            pair.get("open_date"),
            pair.get("close_date"),
        )
        if not pair or any(value in (None, "") for value in required_pair_facts):
            continue
        if _review_helpers._safe_int(pair.get("lots"), 0) <= 0:
            continue
        row_open_date = str(row.get("open_date") or "")[:10]
        row_close_date = str(row.get("close_date") or row.get("episode_date") or "")[:10]
        if (
            not row_open_date
            or not row_close_date
            or str(pair.get("open_date") or "")[:10] != row_open_date
            or str(pair.get("close_date") or "")[:10] != row_close_date
        ):
            continue
        rec_id = str(pair.get("open_recommendation_id") or "").strip()
        payload_rec_id = str(payload.get("open_recommendation_id") or "").strip()
        if payload_rec_id and payload_rec_id != rec_id:
            continue
        origin_source_type = str(
            pair.get("origin_source_type")
            or pair.get("open_source_type")
            or ""
        ).strip().lower()
        if origin_source_type != "strategy" or bool(pair.get("contains_forced_risk")):
            continue
        ticker = str(row.get("ticker") or "").upper()
        side = str(row.get("side") or "").lower()
        if not ticker or side not in {"long", "short"}:
            continue
        if (
            str(pair.get("ticker") or "").strip().upper() != ticker
            or str(pair.get("side") or "").strip().lower() != side
        ):
            continue
        signal_snapshot = payload.get("signal_snapshot") if isinstance(payload.get("signal_snapshot"), dict) else {}
        final_contract = (
            payload.get("final_action_contract")
            if isinstance(payload.get("final_action_contract"), dict)
            else signal_snapshot.get("final_action_contract")
            if isinstance(signal_snapshot.get("final_action_contract"), dict)
            else {}
        )
        if (
            not final_contract
            or not str(final_contract.get("final_action") or "").strip()
            or "current_lots" not in final_contract
            or "target_lots" not in final_contract
        ):
            continue
        try:
            int(float(final_contract.get("current_lots")))
            int(float(final_contract.get("target_lots")))
        except (TypeError, ValueError):
            continue
        semantic_state = derive_research_fact_state(final_contract, {})
        current_lots = _review_helpers._safe_int(semantic_state.get("current_lots"), 0)
        target_lots = _review_helpers._safe_int(semantic_state.get("target_lots"), current_lots)
        if str(semantic_state.get("canonical_action_family") or "") != "open_add_new_risk":
            continue
        if (side == "long" and target_lots <= 0) or (side == "short" and target_lots >= 0):
            continue
        action_taken = str(
            semantic_state.get("action")
            or final_contract.get("final_action")
            or recommendation_intent_from_lots(
                current_lots=current_lots,
                target_lots=target_lots,
            ).get("action")
            or "hold"
        )
        opportunity_type = str(payload.get("opportunity_type") or "")
        opportunity_state = str(payload.get("opportunity_state") or "")
        setup_type = str(final_contract.get("setup_type") or "").strip()
        if setup_type.lower() in {"", "unknown", "*", "generic_trade_setup"}:
            continue
        entry_trigger = str(
            final_contract.get("entry_trigger")
            or payload.get("entry_trigger")
            or ""
        ).strip()
        trigger_source = str(
            final_contract.get("trigger_source")
            or payload.get("trigger_source")
            or ""
        ).strip()
        data_usage = payload.get("data_usage_summary") if isinstance(payload.get("data_usage_summary"), dict) else {}
        position_lifecycle_trace = (
            payload.get("position_lifecycle_trace")
            if isinstance(payload.get("position_lifecycle_trace"), dict)
            else {}
        )
        lifecycle_daily_facts = (
            position_lifecycle_trace.get("daily_facts")
            if isinstance(position_lifecycle_trace.get("daily_facts"), list)
            else []
        )
        data_combo = _episode_data_combo(data_usage, payload)
        scope_key = build_alpha_setup_scope_key(
            ticker=ticker,
            side=side,
            horizon_class=row.get("horizon_class") or "unknown",
            market_regime=row.get("market_regime") or "unknown",
            setup_type=setup_type,
            data_combo=data_combo,
        )
        net_pnl = float(row.get("net_pnl") or 0.0)
        close_date = str(row.get("close_date") or row.get("episode_date") or trading_date)[:10]
        episode_completion_date = str(
            row.get("episode_date") or row.get("close_date") or trading_date
        )[:10]
        samples.append({
            "ticker": ticker,
            "side": side,
            "sector": row.get("sector") or "unknown",
            "horizon_class": row.get("horizon_class") or "unknown",
            "expected_horizon_days": _review_helpers._safe_int(
                payload.get("expected_horizon_days")
                or final_contract.get("expected_horizon_days"),
                0,
            ),
            "market_regime": row.get("market_regime") or "unknown",
            "setup_type": setup_type,
            "entry_trigger": entry_trigger,
            "trigger_source": trigger_source,
            "data_combo": data_combo,
            "scope_key": scope_key,
            "source_type": "trade_episode",
            "recommendation_id": rec_id,
            "action_taken": action_taken,
            "pm_action": action_taken,
            "final_action_semantics": semantic_state,
            "target_lots": target_lots,
            "current_lots": current_lots,
            "executed_lots": abs(_review_helpers._safe_int(pair.get("lots"), 1)),
            "net_pnl": net_pnl,
            "commission": 0.0,
            "holding_days": int(float(row.get("holding_days") or 0)),
            "outcome_label": "profit" if net_pnl > 0 else "loss" if net_pnl < 0 else "flat_or_no_trade",
            "opportunity_state": opportunity_state or "watch_for_trigger",
            "evidence": {
                "trade_episode_memory_id": row.get("id"),
                "open_recommendation_id": rec_id,
                "close_recommendation_id": pair.get("close_recommendation_id"),
                "open_transaction_id": pair.get("open_transaction_id"),
                "close_transaction_id": pair.get("close_transaction_id"),
                "open_date": row.get("open_date"),
                "close_date": close_date,
                "episode_completion_date": episode_completion_date,
                "episode_date": row.get("episode_date"),
                "lesson_text": row.get("lesson_text"),
                "opportunity_type": opportunity_type,
                "opportunity_state": opportunity_state,
                "analyst_payloads": payload.get("analyst_payloads") or {},
                "data_usage_summary": data_usage,
                "final_action_contract": final_contract,
                "entry_trigger": entry_trigger,
                "trigger_source": trigger_source,
                "position_lifecycle_trace": position_lifecycle_trace,
                "learning_boundary": {
                    "learning_source": "trade_episode_memory",
                    "strategy_episode_only": True,
                    "episode_bound_to_open_final_action_contract": True,
                },
            },
            "result": {
                "episode_net_pnl": net_pnl,
                "episode_reward_source": "trade_episode_memory",
                "episode_memory_id": row.get("id"),
                "open_date": row.get("open_date"),
                "close_date": close_date,
                "holding_days": int(float(row.get("holding_days") or 0)),
                "return_on_notional": float(row.get("return_on_notional") or 0.0),
                "lifecycle_fact_dates": [
                    str(fact.get("trading_date") or "")[:10]
                    for fact in lifecycle_daily_facts
                    if isinstance(fact, dict) and fact.get("trading_date")
                ],
                "episode_completion_date": episode_completion_date,
                "settled_fact_cutoff": episode_completion_date,
                "no_future_leakage": True,
            },
        })
    return samples


def _episode_data_combo(data_usage: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    parts: List[str] = []
    for key, value in sorted((data_usage or {}).items()):
        if isinstance(value, Mapping):
            status = value.get("freshness") or value.get("status") or value.get("source") or "used"
            parts.append(f"{key}:{status}")
        elif value:
            parts.append(f"{key}:{value}")
    contract = payload.get("trade_research_contract_summary")
    if isinstance(contract, Mapping):
        for key in ("setup_type", "setup_family", "evidence_combo"):
            if contract.get(key):
                parts.append(f"contract:{key}:{contract.get(key)}")
    if not parts:
        return "episode_memory"
    return "|".join(str(part) for part in parts[:10])


def _write_alpha_setup_policy_state(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    """Translate settled setup expectancy into future PM policy state.

    This is a lightweight contextual-bandit bridge: same-scope setup outcomes
    become future action tendencies.  It does not issue current-day trades, does
    not use future data, and does not create product blacklists.
    """

    learning_cfg = cfg.get("learning", {}) or {}
    profile_cfg = learning_cfg.get("alpha_setup_profile", {}) or {}
    policy_cfg = learning_cfg.get("alpha_setup_policy_state", {}) or {}
    if not bool(profile_cfg.get("enabled", True)) or not bool(policy_cfg.get("enabled", True)):
        return {"rows": 0, "status": "disabled"}

    research_memory_writers.ensure_research_learning_schema(cursor)
    trading_day = str(trading_date)[:10]
    valid_days = int(
        policy_cfg.get(
            "valid_days",
            profile_cfg.get("valid_days", learning_cfg.get("overlay_expires_after_days", 10)),
        )
        or 10
    )
    valid_until = _review_helpers._valid_until(trading_day, valid_days)
    now = _review_helpers._utc_now()
    max_rows = max(1, _review_helpers._safe_int(policy_cfg.get("max_rows_per_day"), 12))
    cap_multiplier = max(0.0, min(1.0, _review_helpers._safe_float(policy_cfg.get("cap_multiplier"), 0.50)))

    cursor.execute(
        """
        SELECT *
        FROM alpha_setup_profile
        WHERE config_id = ?
          AND active = 1
          AND last_sample_date <= ?
          AND (valid_until IS NULL OR valid_until >= ?)
        ORDER BY
            CASE lifecycle_state
                WHEN 'deployable' THEN 0
                WHEN 'protected' THEN 1
                WHEN 'watchlist' THEN 2
                WHEN 'candidate' THEN 3
                WHEN 'capped' THEN 4
                WHEN 'rejected' THEN 5
                ELSE 6
            END,
            ABS(net_pnl) DESC,
            confidence_score DESC,
            sample_count DESC,
            updated_at DESC
        LIMIT ?
        """,
        (config_id, trading_day, trading_day, max_rows * 3),
    )
    profiles = [dict(row) for row in cursor.fetchall()]
    if not profiles:
        return {"rows": 0, "status": "no_alpha_setup_profiles"}

    inserted = 0
    skipped = 0
    by_type: Counter = Counter()
    for profile in profiles:
        if inserted >= max_rows:
            break
        ticker = str(profile.get("ticker") or "*").upper()
        side = str(profile.get("side") or "*").lower()
        if ticker in {"", "*"} or side not in {"long", "short"}:
            skipped += 1
            continue
        state = str(profile.get("lifecycle_state") or "candidate").lower()
        sample_count = _review_helpers._safe_int(profile.get("sample_count"))
        trade_count = _review_helpers._safe_int(profile.get("trade_count"))
        net_pnl = _review_helpers._safe_float(profile.get("net_pnl"))
        win_rate = _review_helpers._safe_float(profile.get("win_rate"))
        profit_factor = _review_helpers._safe_float(profile.get("profit_factor"))
        confidence = _review_helpers._safe_float(profile.get("confidence_score"))
        max_position_impact = _review_helpers._safe_float(profile.get("max_position_impact"))
        profile_setup_type = _valid_fac_setup_type(profile.get("setup_type"))
        if not profile_setup_type:
            skipped += 1
            continue
        scope = {
            "ticker": ticker,
            "side": side,
            "setup_type": profile_setup_type,
            "horizon_class": str(profile.get("horizon_class") or "*"),
            "market_regime": str(profile.get("market_regime") or "*"),
        }
        evidence = {
            "source": "alpha_setup_profile",
            "scope_key": profile.get("scope_key"),
            "setup_type": profile.get("setup_type"),
            "data_combo": profile.get("data_combo"),
            "lifecycle_state": state,
            "profile_state_hint": profile.get("profile_state_hint"),
            "sample_count": sample_count,
            "trade_count": trade_count,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "net_pnl": net_pnl,
            "max_loss": _review_helpers._safe_float(profile.get("max_loss")),
            "avg_holding_days": _review_helpers._safe_float(profile.get("avg_holding_days")),
            "confidence_score": confidence,
            "max_position_impact": max_position_impact,
            "same_scope_required": True,
            "future_only": True,
            "not_product_blacklist": True,
            "bandit_style_update": True,
        }

        policy_type = ""
        policy_action = ""
        multiplier = 1.0
        reason = ""
        maturity_state = state
        event_type = "alpha_setup_policy_state"
        if state in {"deployable", "protected"}:
            policy_type = "learning_mechanism:alpha_setup_ev"
            policy_action = "protect"
            multiplier = 1.0
            reason = "positive same-scope alpha setup expectancy can be considered by PM"
            maturity_state = "alpha_setup_positive_expectancy"
        elif state in {"capped", "rejected"}:
            policy_type = "learning_mechanism:alpha_setup_ev"
            policy_action = "cap"
            multiplier = cap_multiplier
            reason = "negative same-scope alpha setup expectancy requires cap or repair evidence"
            maturity_state = "alpha_setup_negative_expectancy"
        else:
            skipped += 1
            continue

        payload = research_memory_writers.build_policy_memory_payload(
            policy_type=policy_type,
            policy_action=policy_action,
            reason=reason,
            multiplier=multiplier,
            maturity_state=maturity_state,
            scope=scope,
            evidence=evidence,
        )
        payload["alpha_setup_scope"] = {
            "scope_key": profile.get("scope_key"),
            "setup_type": profile.get("setup_type"),
            "data_combo": profile.get("data_combo"),
            "last_sample_date": profile.get("last_sample_date"),
        }
        event_id = research_memory_writers.insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_day,
            event_type=event_type,
            scope_type="alpha_setup",
            scope_key=str(profile.get("scope_key") or f"{ticker}:{side}"),
            evidence=evidence,
            action={
                "policy_type": policy_type,
                "policy_action": policy_action,
                "multiplier": multiplier,
                "reason": reason,
                CONTRACT_KEY: payload.get(CONTRACT_KEY),
            },
            status="applied",
        )
        research_memory_writers.upsert_alpha_setup_policy_state(
            cursor,
            config_id=config_id,
            ticker=ticker,
            side=side,
            setup_type=profile_setup_type,
            horizon_class=scope["horizon_class"],
            market_regime=scope["market_regime"],
            policy_type=policy_type,
            policy_action=policy_action,
            multiplier=multiplier,
            confidence_score=confidence,
            sample_count=sample_count,
            reason=reason,
            source_event_id=event_id,
            source_trading_date=trading_day,
            created_at=now,
            valid_until=valid_until,
            payload_json=_review_helpers._json_dumps(payload),
        )
        inserted += 1
        by_type[policy_type] += 1

    return {
        "rows": inserted,
        "skipped": skipped,
        "status": "applied" if inserted else "no_eligible_alpha_setup_policy",
        "policy_type_counts": dict(by_type),
    }


def _strategy_recommendations_for_date(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
) -> List[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT *
        FROM futures_recommendation
        WHERE config_id = ?
          AND trading_date = ?
          AND source_type = 'strategy'
        ORDER BY underlying_code, created_at, id
        """,
        (config_id, str(trading_date)[:10]),
    )
    return [dict(row) for row in cursor.fetchall()]


def _transactions_for_date(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
) -> List[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT *
        FROM futures_transactions
        WHERE config_id = ?
          AND trading_date = ?
        ORDER BY ticker, created_at, id
        """,
        (config_id, str(trading_date)[:10]),
    )
    return [dict(row) for row in cursor.fetchall()]


def backfill_alpha_setup_profiles_from_history(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    reset: bool = False,
) -> Dict[str, Any]:
    """Build Alpha Setup profiles from settled historical recommendations.

    This is an explicit research-memory bootstrap.  It never changes
    recommendations, transactions, settlement, reports, or evaluation data; it
    only repopulates alpha_setup_* tables so future trading days can use the
    already-settled history as same-scope setup evidence.
    """

    research_memory_writers.ensure_research_learning_schema(cursor)
    bounds: List[Any] = [config_id]
    where_parts = ["config_id = ?", "source_type = 'strategy'"]
    if start_date:
        where_parts.append("trading_date >= ?")
        bounds.append(str(start_date)[:10])
    if end_date:
        where_parts.append("trading_date <= ?")
        bounds.append(str(end_date)[:10])

    if reset:
        research_memory_writers.reset_alpha_setup_memory(cursor, config_id=config_id)

    cursor.execute(
        f"""
        SELECT DISTINCT trading_date
        FROM futures_recommendation
        WHERE {' AND '.join(where_parts)}
        ORDER BY trading_date
        """,
        tuple(bounds),
    )
    trading_dates = [str(row["trading_date"])[:10] for row in cursor.fetchall()]
    day_results: List[Dict[str, Any]] = []
    lifecycle_counts: Counter = Counter()
    total_rows = 0
    for trading_date in trading_dates:
        recommendations = _strategy_recommendations_for_date(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
        )
        transactions = _transactions_for_date(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
        )
        grouped_transactions = _review_helpers._group_transactions_by_recommendation(transactions)
        result = write_alpha_setup_profiles(
            cursor,
            cfg=cfg,
            config_id=config_id,
            trading_date=trading_date,
            strategy_recommendations=recommendations,
            transactions_by_recommendation=grouped_transactions,
        )
        rows = int(result.get("rows") or 0)
        total_rows += rows
        lifecycle_counts.update(result.get("lifecycle_counts") or {})
        day_results.append(
            {
                "trading_date": trading_date,
                "recommendations": len(recommendations),
                "transactions": len(transactions),
                "alpha_setup_rows": rows,
                "status": result.get("status"),
            }
        )

    cursor.execute(
        "SELECT COUNT(*) AS n FROM alpha_setup_profile WHERE config_id = ?",
        (config_id,),
    )
    profile_count = int((cursor.fetchone() or {"n": 0})["n"] or 0)
    cursor.execute(
        "SELECT COUNT(*) AS n FROM alpha_setup_action_value WHERE config_id = ?",
        (config_id,),
    )
    action_value_count = int((cursor.fetchone() or {"n": 0})["n"] or 0)
    return {
        "status": "applied",
        "reset": bool(reset),
        "date_count": len(trading_dates),
        "sample_rows": total_rows,
        "profile_count": profile_count,
        "action_value_count": action_value_count,
        "lifecycle_counts": dict(lifecycle_counts),
        "first_date": trading_dates[0] if trading_dates else None,
        "last_date": trading_dates[-1] if trading_dates else None,
        "day_preview": day_results[:5],
        "day_tail": day_results[-5:],
    }


def run_researcher_causal_review(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    previous_trading_dates_by_ticker: Mapping[str, str],
    settlement_row: Optional[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    no_trade_reason_counter: Counter,
    transactions_by_recommendation: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> int:
    learning_cfg = cfg.get("learning", {}) or {}
    review_cfg = learning_cfg.get("researcher_causal_review") or {}
    if not bool(review_cfg.get("enabled", False)):
        return 0
    _validate_researcher_input_facts(
        cursor=cursor,
        config_id=config_id,
        trading_date=trading_date,
        previous_trading_dates_by_ticker=previous_trading_dates_by_ticker,
        settlement_row=settlement_row,
        strategy_recommendations=strategy_recommendations,
        transactions_by_recommendation=transactions_by_recommendation,
    )
    evidence = _build_causal_evidence_pack(
        config_id=config_id,
        trading_date=trading_date,
        strategy_recommendations=strategy_recommendations,
        settlement_row=settlement_row,
        no_trade_reason_counter=no_trade_reason_counter,
    )
    prompt = build_researcher_causal_review_prompt(
        _review_helpers._json_dumps(evidence)[:12000]
    )
    if not bool(review_cfg.get("use_llm", False)):
        return 0
    try:
        from llm.inference import agent_call

        output = agent_call(
            prompt=prompt,
            llm_config=cfg.get("llm", {}),
            pydantic_model=CausalReviewLLMOutput,
        )
        candidate_payload = _validate_causal_llm_output(output)
    except Exception:
        logger.warning(f"Researcher LLM causal review rejected on {trading_date}")
        return 0

    note_id = str(uuid.uuid4())
    payload_ext = externalize_json_for_db(
        {"evidence": evidence, "validated_output": candidate_payload},
        category="researcher_llm_notes",
        record_id=note_id,
        field_name="payload",
        config_id=config_id,
        trading_date=trading_date,
    )
    research_memory_writers.insert_researcher_llm_note(
        cursor,
        note_id=note_id,
        config_id=config_id,
        trading_date=trading_date,
        evidence_pack_id=evidence["evidence_pack_id"],
        ticker="*",
        raw_prompt="",
        raw_response="",
        created_at=_review_helpers._utc_now(),
        payload_json=payload_ext.inline_value,
        raw_prompt_artifact_path=None,
        raw_prompt_sha256=None,
        raw_prompt_size=0,
        raw_prompt_summary_json=None,
        raw_response_artifact_path=None,
        raw_response_sha256=None,
        raw_response_size=0,
        raw_response_summary_json=None,
        payload_artifact_path=payload_ext.artifact_path,
        payload_sha256=payload_ext.sha256,
        payload_size=payload_ext.size_bytes,
        payload_summary_json=payload_ext.summary_json,
    )
    candidate_payload["agent_name"] = "researcher"
    research_memory_writers.insert_causal_review_candidate(
        cursor,
        candidate_id=str(uuid.uuid4()),
        config_id=config_id,
        trading_date=trading_date,
        evidence_pack_id=evidence["evidence_pack_id"],
        candidate_type="post_trade_causal_research",
        confidence_score=_review_helpers._safe_float(candidate_payload.get("confidence_score"), 0.0),
        rule_validation_status="notes_only_pending_rule_validation",
        created_at=_review_helpers._utc_now(),
        valid_until=(
            datetime.strptime(str(trading_date)[:10], "%Y-%m-%d") + timedelta(days=10)
        ).strftime("%Y-%m-%d"),
        payload_json=_review_helpers._json_dumps(candidate_payload),
    )
    return 1


def _compact_episode_daily_facts(payload: Mapping[str, Any]) -> Dict[str, Any]:
    trace = (
        payload.get("position_lifecycle_trace")
        if isinstance(payload.get("position_lifecycle_trace"), Mapping)
        else {}
    )
    compact_days: List[Dict[str, Any]] = []
    pair = payload.get("pair") if isinstance(payload.get("pair"), Mapping) else {}
    physical_pairs = (
        pair.get("physical_pairs")
        if isinstance(pair.get("physical_pairs"), list)
        else [pair]
    )
    total_notional = sum(
        abs(
            _review_helpers._safe_float(item.get("open_price"), 0.0)
            * _review_helpers._safe_int(item.get("lots"), 0)
            * _review_helpers._safe_float(item.get("contract_multiplier"), 1.0)
        )
        for item in physical_pairs
        if isinstance(item, Mapping)
    )
    final_return_on_notional = (
        _review_helpers._safe_float(pair.get("return_on_notional"), 0.0)
        if pair.get("return_on_notional") is not None
        else None
    )
    running_net_pnl = 0.0
    peak_net_pnl = 0.0
    for day in trace.get("daily_facts") or []:
        if not isinstance(day, Mapping):
            continue
        fac_facts: List[Dict[str, Any]] = []
        for item in day.get("recommendations") or []:
            if not isinstance(item, Mapping):
                continue
            contract = (
                item.get("final_action_contract")
                if isinstance(item.get("final_action_contract"), Mapping)
                else {}
            )
            if contract:
                evidence = (
                    contract.get("evidence_used")
                    if isinstance(contract.get("evidence_used"), Mapping)
                    else {}
                )
                deployment = (
                    contract.get("capital_deployment")
                    if isinstance(contract.get("capital_deployment"), Mapping)
                    else {}
                )
                fac_facts.append({
                    "final_action": contract.get("final_action"),
                    "target_lots": contract.get("target_lots"),
                    "authority_type": contract.get("authority_type"),
                    "reason_codes": list(contract.get("reason_codes") or []),
                    "opportunity_score": evidence.get("opportunity_score"),
                    "opportunity_rank": deployment.get("opportunity_rank"),
                })
        settlement_facts = [
            dict(item)
            for item in (day.get("ticker_settlement_facts") or [])
            if isinstance(item, Mapping)
        ]
        for item in settlement_facts:
            running_net_pnl += (
                _review_helpers._safe_float(item.get("daily_pnl"), 0.0)
                - _review_helpers._safe_float(item.get("commission"), 0.0)
            )
            peak_net_pnl = max(peak_net_pnl, running_net_pnl)
        compact_days.append({
            "trading_date": day.get("trading_date"),
            "fac_facts": fac_facts,
            "transactions": [
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "action",
                        "lots",
                        "execution_price",
                        "commission",
                    )
                    if key in item
                }
                for item in (day.get("transactions") or [])
                if isinstance(item, Mapping)
            ],
            "ticker_settlement_facts": settlement_facts,
            "evidence_state": dict(day.get("evidence_state") or {}),
            "evidence_change": dict(day.get("evidence_change") or {}),
            "invalidation_state": dict(day.get("invalidation_state") or {}),
            "invalidation_change": dict(day.get("invalidation_change") or {}),
            "cycle_return_on_notional_after_settlement": (
                round(running_net_pnl / total_notional, 8)
                if total_notional > 0
                else None
            ),
            "cycle_peak_return_on_notional_after_settlement": (
                round(peak_net_pnl / total_notional, 8)
                if total_notional > 0
                else None
            ),
            "cycle_profit_drawdown_return_on_notional_after_settlement": (
                round(max(0.0, peak_net_pnl - running_net_pnl) / total_notional, 8)
                if total_notional > 0
                else None
            ),
        })
    final_return = (
        round(running_net_pnl / total_notional, 8)
        if total_notional > 0
        else final_return_on_notional
    )
    peak_return = (
        round(peak_net_pnl / total_notional, 8)
        if total_notional > 0
        else (max(final_return or 0.0, 0.0) if final_return is not None else None)
    )
    return {
        "fact_source": trace.get("fact_source"),
        "open_date": trace.get("open_date"),
        "close_date": trace.get("close_date"),
        "daily_facts": compact_days,
        "learning_economics_basis": "after_fee_return_on_notional",
        "cycle_peak_return_on_notional": peak_return,
        "cycle_final_return_on_notional": final_return,
        "cycle_profit_drawdown_return_on_notional": (
            round(max(0.0, peak_net_pnl - running_net_pnl) / total_notional, 8)
            if total_notional > 0
            else (
                round(max(0.0, (peak_return or 0.0) - (final_return or 0.0)), 8)
                if final_return is not None
                else None
            )
        ),
    }


def _recent_trade_episodes_for_research(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    limit: int,
) -> List[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, ticker, side, sector, setup_type, horizon_class,
               market_regime, open_date, close_date, holding_days,
               return_on_notional, outcome_label, lesson_text,
               payload_json, payload_artifact_path, payload_sha256
        FROM trade_episode_memory
        WHERE config_id = ?
          AND close_date IS NOT NULL
          AND close_date <= ?
          AND return_on_notional IS NOT NULL
        ORDER BY ABS(return_on_notional) DESC, close_date DESC, created_at DESC
        LIMIT ?
        """,
        (config_id, trading_date, int(limit)),
    )
    episodes: List[Dict[str, Any]] = []
    for raw_row in cursor.fetchall():
        row = dict(raw_row)
        payload = load_externalized_json(
            row.pop("payload_json", None),
            row.pop("payload_artifact_path", None),
            row.pop("payload_sha256", None),
        )
        payload = payload if isinstance(payload, Mapping) else {}
        pair = payload.get("pair") if isinstance(payload.get("pair"), Mapping) else {}
        if pair:
            pair = dict(pair)
            pair["return_on_notional"] = row.get("return_on_notional")
            payload = {**dict(payload), "pair": pair}
        row["position_lifecycle_trace"] = _compact_episode_daily_facts(payload)
        episodes.append(row)
    return episodes


def write_exploratory_hypotheses(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    learning_cfg = cfg.get("learning", {}) or {}
    research_cfg = learning_cfg.get("exploratory_research", {}) or {}
    if not bool(research_cfg.get("enabled", True)):
        return {"rows": 0, "status": "disabled"}
    episodes = _recent_trade_episodes_for_research(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        limit=int(research_cfg.get("max_episode_samples", 24) or 24),
    )
    min_episodes = int(research_cfg.get("min_episode_samples", 2) or 2)
    if len(episodes) < min_episodes:
        return {"rows": 0, "status": "insufficient_episode_samples", "episode_count": len(episodes)}
    cursor.execute(
        """
        SELECT id
        FROM trade_episode_memory
        WHERE config_id = ?
          AND close_date IS NOT NULL
          AND close_date <= ?
          AND return_on_notional IS NOT NULL
        """,
        (config_id, trading_date),
    )
    available_episode_ids = {
        str(row["id"] if isinstance(row, sqlite3.Row) else row[0])
        for row in cursor.fetchall()
        if str(row["id"] if isinstance(row, sqlite3.Row) else row[0])
    }
    cursor.execute(
        """
        SELECT evidence_json
        FROM learning_event_log
        WHERE config_id = ? AND event_type = 'exploratory_hypothesis_generation'
        ORDER BY trading_date DESC, created_at DESC, id DESC
        LIMIT 1
        """,
        (config_id,),
    )
    prior_generation = cursor.fetchone()
    prior_episode_ids: set[str] = set()
    if prior_generation is not None:
        try:
            prior_evidence = _review_helpers._json_loads(
                prior_generation["evidence_json"]
                if isinstance(prior_generation, sqlite3.Row)
                else prior_generation[0]
            ) or {}
        except Exception:
            prior_evidence = {}
        prior_episode_ids = {
            str(item or "")
            for item in (prior_evidence.get("available_episode_ids") or [])
            if str(item or "")
        }
    new_episode_ids = sorted(available_episode_ids - prior_episode_ids)
    if prior_generation is not None and not new_episode_ids:
        return {
            "rows": 0,
            "status": "no_new_complete_episode",
            "episode_count": len(episodes),
        }
    research_memory_writers.insert_learning_event(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        event_type="exploratory_hypothesis_generation",
        scope_type="research",
        scope_key="complete_episode_set",
        evidence={
            "available_episode_ids": sorted(available_episode_ids),
            "new_episode_ids": new_episode_ids,
        },
        action={"generation_attempted": True},
        status="applied",
    )

    prompt_episode_limit = int(
        research_cfg.get("max_prompt_episode_chars", 60000) or 60000
    )
    prompt_episodes: List[Dict[str, Any]] = []
    for episode in episodes:
        candidate_pack = {
            "trading_date": trading_date,
            "episodes": prompt_episodes + [episode],
        }
        if (
            prompt_episodes
            and len(_review_helpers._json_dumps(candidate_pack))
            > prompt_episode_limit
        ):
            break
        prompt_episodes.append(episode)
    prompt = build_researcher_exploratory_prompt(
        trading_date=trading_date,
        episodes_json=_review_helpers._json_dumps(
            {"trading_date": trading_date, "episodes": prompt_episodes}
        ),
    )
    if not bool(research_cfg.get("use_llm", True)):
        return {"rows": 0, "status": "llm_disabled", "episode_count": len(episodes)}
    try:
        from llm.inference import agent_call

        output = agent_call(
            prompt=prompt,
            llm_config=cfg.get("llm", {}),
            pydantic_model=ExploratoryHypothesisLLMOutput,
        )
        validated_hypotheses = _validate_exploratory_llm_output(
            output,
            episodes_by_id={str(item.get("id") or ""): item for item in prompt_episodes},
        )
    except Exception:
        logger.warning(f"Researcher exploratory research rejected on {trading_date}")
        return {"rows": 0, "status": "llm_output_rejected", "episode_count": len(episodes)}
    if not validated_hypotheses:
        return {"rows": 0, "status": "no_validated_hypotheses", "episode_count": len(episodes)}

    note_id = str(uuid.uuid4())
    evidence = {"agent_name": "researcher", "trading_date": trading_date, "episodes": episodes}
    payload_ext = externalize_json_for_db(
        {"evidence": evidence, "validated_output": validated_hypotheses},
        category="researcher_llm_notes",
        record_id=note_id,
        field_name="payload",
        config_id=config_id,
        trading_date=trading_date,
    )
    research_memory_writers.insert_researcher_llm_note(
        cursor,
        note_id=note_id,
        config_id=config_id,
        trading_date=trading_date,
        evidence_pack_id=f"exploratory:{note_id}",
        ticker="*",
        raw_prompt="",
        raw_response="",
        created_at=_review_helpers._utc_now(),
        payload_json=payload_ext.inline_value,
        raw_prompt_artifact_path=None,
        raw_prompt_sha256=None,
        raw_prompt_size=0,
        raw_prompt_summary_json=None,
        raw_response_artifact_path=None,
        raw_response_sha256=None,
        raw_response_size=0,
        raw_response_summary_json=None,
        payload_artifact_path=payload_ext.artifact_path,
        payload_sha256=payload_ext.sha256,
        payload_size=payload_ext.size_bytes,
        payload_summary_json=payload_ext.summary_json,
    )

    valid_days = int(research_cfg.get("valid_days", learning_cfg.get("memory_expires_after_days", 30)) or 30)
    valid_until = _review_helpers._valid_until(trading_date, valid_days)
    now = _review_helpers._utc_now()
    max_hypotheses = int(research_cfg.get("max_hypotheses_per_day", 5) or 5)
    rows = 0
    for payload in validated_hypotheses[:max_hypotheses]:
        text = str(payload.get("hypothesis_text") or "").strip()
        if not text:
            continue
        confidence = max(0.0, min(1.0, _review_helpers._safe_float(payload.get("confidence_score"), 0.0)))
        ticker = str(payload.get("ticker") or "*").upper()
        sector = str(payload.get("sector") or "*")
        side = str(payload.get("side") or "*").lower()
        horizon = str(payload.get("horizon_class") or "*")
        regime = str(payload.get("market_regime") or "*")
        setup_type = str(payload.get("setup_type") or "*").strip().lower()
        hypothesis_scope_key = (
            f"{ticker}:{sector}:{side}:{horizon}:{regime}:{setup_type}"
        )
        support_episode_ids = list(payload.get("support_episode_ids") or [])
        support_episodes = [
            item
            for item in prompt_episodes
            if str(item.get("id") or "") in set(support_episode_ids)
        ]
        if not support_episodes:
            continue
        support_sample_count = len(support_episodes)
        cursor.execute(
            """
            SELECT id
            FROM exploratory_hypothesis
            WHERE config_id = ?
              AND lower(scope_key) = lower(?)
              AND lower(hypothesis_text) = lower(?)
            LIMIT 1
            """,
            (config_id, hypothesis_scope_key, text),
        )
        if cursor.fetchone() is not None:
            continue
        suggested_use = str(
            payload.get("suggested_use")
            or "structured research hypothesis only; validate with future samples"
        )
        if "structured research hypothesis" not in suggested_use.lower():
            suggested_use = f"{suggested_use}; structured research hypothesis only until validated"
        hypothesis_contract = build_next_round_memory_contract(
            memory_type="exploratory_hypothesis",
            maturity_state="candidate",
            scope={
                "ticker": ticker,
                "sector": sector,
                "side": side,
                "horizon_class": horizon,
                "market_regime": regime,
                "setup_type": setup_type,
            },
            usable_memory=text,
            analysis_strategy_updates=[
                suggested_use,
                "Use this as a question to test against today's indicators, price stage, and analyst evidence.",
            ],
            trading_strategy_updates=[
                "Translate only into entry, exit, holding, or probe considerations after current confirmation.",
                "Do not use a candidate hypothesis to size up, add, position_match, or continue losing exposure.",
            ],
            validation_plan=[
                payload.get("validation_plan") or "Track same-scope future samples before promotion.",
            ],
            sample_count=support_sample_count,
            confidence_score=confidence,
        )
        event_id = research_memory_writers.insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="exploratory_hypothesis",
            scope_type="research",
            scope_key=hypothesis_scope_key,
            evidence={
                "episode_count": support_sample_count,
                "support_episode_ids": support_episode_ids,
                "note_id": note_id,
                "agent_name": "researcher",
            },
            action={
                "hypothesis_text": text,
                "suggested_use": suggested_use,
                "entry_timing_hint": payload.get("entry_timing_hint"),
                "exit_timing_hint": payload.get("exit_timing_hint"),
                "holding_period_hint": payload.get("holding_period_hint"),
                "invalidation_condition": payload.get("invalidation_condition"),
                "validation_plan": payload.get("validation_plan"),
                CONTRACT_KEY: hypothesis_contract,
            },
            status="candidate",
        )
        hypothesis_payload = {
            **payload,
            "agent_name": "researcher",
            "suggested_use": suggested_use,
            "source_note_id": note_id,
            "source_event_id": event_id,
            "setup_type": setup_type,
            "support_episode_ids": support_episode_ids,
            "support_episode_count": support_sample_count,
            "validation_mode": "research_only_same_scope_future_complete_episode",
            "hard_constraints": {
                "max_total_margin_ratio": cfg.get("max_total_margin_ratio", 0.20),
                "structured_hypothesis_only": True,
                "candidate_hypothesis_cannot_control_position": True,
                "candidate_hypothesis_research_only": True,
            },
            CONTRACT_KEY: hypothesis_contract,
        }
        hypothesis_id = str(uuid.uuid4())
        hypothesis_ext = externalize_json_for_db(
            hypothesis_payload,
            category="exploratory_hypothesis",
            record_id=hypothesis_id,
            field_name="payload",
            config_id=config_id,
            trading_date=trading_date,
        )
        research_memory_writers.insert_exploratory_hypothesis(
            cursor,
            hypothesis_id=hypothesis_id,
            config_id=config_id,
            trading_date=trading_date,
            scope_type="research",
            scope_key=hypothesis_scope_key,
            ticker=ticker,
            sector=sector,
            side=side,
            horizon_class=horizon,
            market_regime=regime,
            hypothesis_text=text,
            evidence_summary=str(payload.get("evidence_summary") or ""),
            suggested_use=suggested_use,
            confidence_score=confidence,
            sample_count=support_sample_count,
            status="candidate",
            created_at=now,
            valid_until=valid_until,
            payload_json=hypothesis_ext.inline_value,
            payload_artifact_path=hypothesis_ext.artifact_path,
            payload_sha256=hypothesis_ext.sha256,
            payload_size=hypothesis_ext.size_bytes,
            payload_summary_json=hypothesis_ext.summary_json,
        )
        rows += 1
    return {
        "rows": rows,
        "status": "applied" if rows else "no_hypotheses",
        "episode_count": len(episodes),
        "prompt_episode_count": len(prompt_episodes),
    }


def validate_exploratory_hypotheses(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    """Validate research hypotheses with future, same-scope real episodes only.

    This is a shadow research transition. It updates exploratory_hypothesis but
    deliberately does not write alpha samples, profiles, policies, or orders.
    """
    learning_cfg = cfg.get("learning", {}) or {}
    research_cfg = learning_cfg.get("exploratory_research", {}) or {}
    if not bool(research_cfg.get("enabled", True)):
        return {"rows": 0, "status": "disabled", "validated": 0, "rejected": 0, "monitoring": 0}

    min_samples = max(
        1,
        int(
            research_cfg.get(
                "validation_min_samples",
                research_cfg.get("min_episode_samples", 2),
            )
            or 2
        ),
    )
    min_mean_return = _review_helpers._safe_float(
        research_cfg.get("validation_min_mean_return_on_notional"),
        0.0,
    )
    cursor.execute(
        """
        SELECT *
        FROM exploratory_hypothesis
        WHERE config_id = ?
          AND trading_date < ?
          AND status IN ('candidate', 'monitoring', 'validated', 'rejected')
        ORDER BY trading_date, created_at, id
        """,
        (config_id, trading_date),
    )
    hypotheses = [dict(row) for row in cursor.fetchall()]
    counts: Counter[str] = Counter()
    transitions: List[Dict[str, Any]] = []
    for hypothesis in hypotheses:
        payload = load_externalized_json(
            hypothesis.get("payload_json"),
            hypothesis.get("payload_artifact_path"),
            hypothesis.get("payload_sha256"),
        )
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        scoped_hypothesis = {
            **hypothesis,
            "setup_type": payload.get("setup_type") or "*",
        }
        cursor.execute(
            """
            SELECT id, ticker, side, sector, setup_type, horizon_class,
                   market_regime, close_date, trading_date,
                   return_on_notional
            FROM trade_episode_memory
            WHERE config_id = ?
              AND COALESCE(close_date, trading_date) > ?
              AND COALESCE(close_date, trading_date) <= ?
              AND return_on_notional IS NOT NULL
            ORDER BY COALESCE(close_date, trading_date), created_at, id
            """,
            (config_id, hypothesis.get("trading_date"), trading_date),
        )
        matched = [
            dict(row)
            for row in cursor.fetchall()
            if _episode_matches_hypothesis_scope(dict(row), scoped_hypothesis)
        ]
        returns = [
            _review_helpers._safe_float(item.get("return_on_notional"), 0.0)
            for item in matched
        ]
        latest_return = returns[-1] if returns else None
        mean_return = sum(returns) / len(returns) if returns else None
        expired = bool(
            hypothesis.get("valid_until")
            and str(hypothesis.get("valid_until"))[:10] < str(trading_date)[:10]
        )
        if len(returns) >= min_samples:
            if mean_return is not None and mean_return <= min_mean_return:
                next_status = "rejected"
                outcome = "future_same_scope_non_positive_expectancy"
            elif latest_return is not None and latest_return < 0.0:
                next_status = "monitoring"
                outcome = "latest_complete_loss_suspends_validated_prior"
            else:
                next_status = "validated"
                outcome = "future_same_scope_positive_expectancy"
        elif expired:
            next_status = "rejected"
            outcome = "expired_without_required_future_samples"
        elif returns:
            next_status = "monitoring"
            outcome = (
                "latest_complete_loss_awaiting_more_future_samples"
                if latest_return is not None and latest_return < 0.0
                else "awaiting_more_future_samples"
            )
        else:
            next_status = "candidate"
            outcome = "awaiting_first_future_sample"

        payload["research_validation"] = {
            "mode": "future_same_scope_complete_episode",
            "economic_basis": "after_fee_return_on_notional",
            "future_only": True,
            "evaluated_through": str(trading_date)[:10],
            "minimum_samples": min_samples,
            "minimum_mean_return_on_notional": min_mean_return,
            "sample_count": len(returns),
            "matched_episode_ids": [str(item.get("id") or "") for item in matched],
            "mean_return_on_notional": mean_return,
            "latest_return_on_notional": latest_return,
            "outcome": outcome,
        }
        contract = payload.get(CONTRACT_KEY)
        if isinstance(contract, Mapping):
            contract = dict(contract)
            contract["maturity_state"] = next_status
            contract["sample_count"] = len(returns)
            contract["position_authority"] = "analysis_prior_only" if next_status == "validated" else "analysis_or_watchlist_only"
            contract["max_position_impact"] = "no_direct_position_impact"
            payload[CONTRACT_KEY] = contract
        payload_ext = externalize_json_for_db(
            payload,
            category="exploratory_hypothesis",
            record_id=str(hypothesis.get("id") or "unknown"),
            field_name="payload",
            config_id=config_id,
            trading_date=str(hypothesis.get("trading_date") or trading_date),
        )
        research_memory_writers.update_exploratory_hypothesis_validation(
            cursor,
            hypothesis_id=str(hypothesis.get("id") or ""),
            config_id=config_id,
            status=next_status,
            sample_count=len(returns),
            payload_json=payload_ext.inline_value,
            payload_artifact_path=payload_ext.artifact_path,
            payload_sha256=payload_ext.sha256,
            payload_size=payload_ext.size_bytes,
            payload_summary_json=payload_ext.summary_json,
        )
        counts[next_status] += 1
        if str(hypothesis.get("status") or "candidate") != next_status:
            transitions.append(
                {
                    "hypothesis_id": hypothesis.get("id"),
                    "from": hypothesis.get("status"),
                    "to": next_status,
                    "outcome": outcome,
                }
            )
    return {
        "rows": len(hypotheses),
        "status": "applied" if hypotheses else "no_hypotheses_to_validate",
        "validated": int(counts.get("validated", 0)),
        "rejected": int(counts.get("rejected", 0)),
        "monitoring": int(counts.get("monitoring", 0)),
        "candidate": int(counts.get("candidate", 0)),
        "transitions": transitions,
    }


def apply_researcher_learning(
    *,
    db: Any,
    cursor: sqlite3.Cursor,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    previous_trading_dates_by_ticker: Mapping[str, str],
    settlement_row: Optional[Dict[str, Any]],
    recommendations: List[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    no_trade_reason_counter: Counter,
    transactions_by_recommendation: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Persist future-only learning after deterministic settled-fact validation."""
    _validate_researcher_input_facts(
        cursor=cursor,
        config_id=config_id,
        trading_date=trading_date,
        previous_trading_dates_by_ticker=previous_trading_dates_by_ticker,
        settlement_row=settlement_row,
        strategy_recommendations=strategy_recommendations,
        transactions_by_recommendation=transactions_by_recommendation,
    )
    cursor.execute("PRAGMA foreign_keys = ON")
    if hasattr(db, "_ensure_reviewer_learning_schema"):
        db._ensure_reviewer_learning_schema(cursor)
    else:
        research_memory_writers.ensure_research_learning_schema(cursor)

    context_rows = research_memory_writers.write_signal_context_history(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        recommendations=strategy_recommendations,
    )
    memory_rows = research_memory_writers.write_strategy_memory_history(
        cursor,
        db=db,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    episode_rows = research_memory_writers.write_trade_episode_memory(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    forecast_evaluation_rows = research_memory_writers.write_analyst_forecast_evaluations(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    perf_counts = research_memory_writers.write_template_and_analyst_learning(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    opportunity_ranking_preference_rows = research_memory_writers.write_opportunity_ranking_learning_events(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
        episode_payloads=getattr(research_memory_writers.write_trade_episode_memory, "last_payloads", []) or [],
    )
    no_trade_memory_rows = research_memory_writers.write_no_trade_opportunity_memory(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        strategy_recommendations=strategy_recommendations,
    )
    no_trade_counterfactual_backfill = research_memory_writers.backfill_no_trade_opportunity_counterfactual_results(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    missed_alpha_accountability = research_memory_writers.write_missed_alpha_accountability_state(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    position_feedback = research_memory_writers.write_research_position_feedback(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        strategy_recommendations=strategy_recommendations,
        transactions_by_recommendation=transactions_by_recommendation or {},
        settlement_row=settlement_row,
        completed_episode_payloads=(
            getattr(research_memory_writers.write_trade_episode_memory, "last_payloads", []) or []
        ),
    )
    adaptive_rows = research_memory_writers.write_adaptive_policy_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    tail_loss_sentinel_rows = research_memory_writers.write_tail_loss_sentinel_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    alpha_promotion_rows = research_memory_writers.write_alpha_promotion_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    contextual_rule_calibration_rows = research_memory_writers.write_contextual_rule_calibration_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
        strategy_recommendations=strategy_recommendations,
        no_trade_reason_counter=no_trade_reason_counter,
    )
    loss_template_observation_rows = research_memory_writers.write_loss_template_observation_research(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    loss_template_policy_rows = int(
        getattr(research_memory_writers.write_loss_template_observation_research, "last_policy_rows", 0) or 0
    )
    fast_loss_sentinel_rows = research_memory_writers.write_fast_loss_sentinel_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    learned_benchmark_policy = research_memory_writers.write_learned_vs_unlearned_policy_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    learning_mechanism_policy = research_memory_writers.write_learning_mechanism_policy_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    alpha_setup_profiles = write_alpha_setup_profiles(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        strategy_recommendations=strategy_recommendations,
        transactions_by_recommendation=transactions_by_recommendation or {},
    )
    alpha_setup_policy_state = _write_alpha_setup_policy_state(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    provisional_rows = research_memory_writers.write_provisional_policy_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    overlay_rows = research_memory_writers.write_config_overlay(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
        settlement_row=settlement_row,
    )
    neutral_accountability = research_memory_writers.write_neutral_accountability_state(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        strategy_recommendations=strategy_recommendations,
    )
    neutral_forward_counterfactual_backfill = research_memory_writers.backfill_neutral_forward_counterfactual_tracking(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    capital_state = research_memory_writers.write_capital_deployment_state(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        settlement_row=settlement_row,
        strategy_recommendations=strategy_recommendations,
        no_trade_reason_counter=no_trade_reason_counter,
    )
    template_prior_path = research_memory_writers.export_template_prior(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    causal_review_candidates = run_researcher_causal_review(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        previous_trading_dates_by_ticker=previous_trading_dates_by_ticker,
        settlement_row=settlement_row,
        strategy_recommendations=strategy_recommendations,
        no_trade_reason_counter=no_trade_reason_counter,
        transactions_by_recommendation=transactions_by_recommendation,
    )
    causal_rule_validation = research_memory_writers.write_validated_causal_policy_rules(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    exploratory_hypothesis_validation = validate_exploratory_hypotheses(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    exploratory_hypotheses = write_exploratory_hypotheses(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    return {
        "researcher_agent": "researcher",
        "signal_context_rows": context_rows,
        "strategy_memory_history_rows": memory_rows,
        "forecast_evaluation_rows": forecast_evaluation_rows,
        **perf_counts,
        "trade_episode_rows": episode_rows,
        "opportunity_ranking_preference_rows": opportunity_ranking_preference_rows,
        "no_trade_opportunity_rows": no_trade_memory_rows,
        "no_trade_counterfactual_backfill": no_trade_counterfactual_backfill,
        "missed_alpha_accountability": missed_alpha_accountability,
        "research_position_feedback": position_feedback,
        "adaptive_policy_rows": adaptive_rows,
        "tail_loss_sentinel_rows": tail_loss_sentinel_rows,
        "alpha_promotion_rows": alpha_promotion_rows,
        "contextual_rule_calibration_rows": contextual_rule_calibration_rows,
        "loss_template_observation_rows": loss_template_observation_rows,
        "loss_template_policy_rows": loss_template_policy_rows,
        "fast_loss_sentinel_rows": fast_loss_sentinel_rows,
        "learned_vs_unlearned_policy": learned_benchmark_policy,
        "learning_mechanism_policy": learning_mechanism_policy,
        "alpha_setup_profiles": alpha_setup_profiles,
        "alpha_setup_policy_state": alpha_setup_policy_state,
        "provisional_policy_rows": provisional_rows,
        "config_overlay_rows": overlay_rows,
        "neutral_accountability": {
            "neutral_ratio": neutral_accountability.get("neutral_ratio", 0.0),
            "accountability_complete_rate": neutral_accountability.get("accountability_complete_rate", 1.0),
            "category_counts": neutral_accountability.get("category_counts", {}),
            "structured_learning_rows": neutral_accountability.get("structured_learning_rows", 0),
            "forward_counterfactual_backfill": neutral_forward_counterfactual_backfill,
        },
        "capital_deployment_state": capital_state,
        "template_prior_path": template_prior_path,
        "causal_review_candidates": causal_review_candidates,
        "validated_causal_rules": causal_rule_validation.get("validated_rules", 0),
        "causal_rule_validation_status_counts": causal_rule_validation.get("status_counts", {}),
        "exploratory_hypothesis_validation": exploratory_hypothesis_validation,
        "exploratory_hypotheses": exploratory_hypotheses,
    }

