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

from pydantic import BaseModel, Field

from database.artifact_store import externalize_json_for_db, externalize_text_for_db, load_externalized_json
from llm.prompt import (
    build_researcher_causal_review_prompt,
    build_researcher_exploratory_prompt,
)
from tools.common.learning_contract import CONTRACT_KEY, build_next_round_memory_contract
from tools.common.final_action_semantics import derive_research_fact_state
from tools.common.alpha_setup import (
    build_scope_key as build_alpha_setup_scope_key,
    infer_setup_type,
    upsert_alpha_setup_sample_and_profile,
)
from tools.agent_tools.analysis.analyst_data_usage import data_usage_from_snapshot
from tools.agent_tools.research import research_memory_writers
from tools.common.order_semantics import recommendation_intent_from_lots
from util.futures_audit import categorize_no_trade_reason
from util.logger import logger


class CausalReviewLLMOutput(BaseModel):
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
    hypothesis_text: str = Field(default="")
    ticker: str = Field(default="*")
    sector: str = Field(default="*")
    side: str = Field(default="*")
    horizon_class: str = Field(default="*")
    market_regime: str = Field(default="*")
    evidence_summary: str = Field(default="")
    suggested_use: str = Field(default="structured research hypothesis only; validate with future samples")
    entry_timing_hint: str = Field(default="")
    exit_timing_hint: str = Field(default="")
    holding_period_hint: str = Field(default="")
    invalidation_condition: str = Field(default="")
    validation_plan: str = Field(default="")
    confidence_score: float = Field(default=0.0)


class ExploratoryHypothesisLLMOutput(BaseModel):
    hypotheses: List[ExploratoryHypothesisItem] = Field(default_factory=list)
    researcher_note: str = Field(default="")

    @property
    def reviewer_note(self) -> str:
        """Backward-compatible alias for older tests/artifacts."""
        return self.researcher_note


from tools.agent_tools.research import research_review_helpers as _review_helpers


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
        horizon = str(row.get("horizon_class") or "unknown")
        regime = str(row.get("market_regime") or "unknown")
        setup_type = str(row.get("setup_type") or "")
        signal_combo = row.get("signal_combo")
        if isinstance(signal_combo, str):
            parsed_combo = _review_helpers._json_loads(signal_combo)
            combo_items = parsed_combo if isinstance(parsed_combo, list) else [signal_combo]
        elif isinstance(signal_combo, list):
            combo_items = signal_combo
        else:
            combo_items = []
        setup_type = infer_setup_type(
            setup_type=setup_type,
            opportunity_type=row.get("opportunity_type"),
            opportunity_state=row.get("opportunity_state"),
        )
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
            "market_regime": regime,
            "setup_type": setup_type,
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
    episode_samples_by_recommendation = _episode_alpha_setup_samples(
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
        if side not in {"long", "short"}:
            target_lots_for_side = _review_helpers._safe_int(final_contract.get("target_lots"))
            preferred = "long" if target_lots_for_side > 0 else "short" if target_lots_for_side < 0 else "flat"
            side = preferred if preferred in {"long", "short"} else "flat"
        if side not in {"long", "short"}:
            continue
        rec_id = str(recommendation.get("id") or "")
        txs = transactions_by_recommendation.get(rec_id, [])
        combo = _review_helpers._signal_combo_from_snapshot(snapshot)
        horizon = _review_helpers._horizon_class(_review_helpers._expected_horizon_days(snapshot, side), snapshot)
        regime = _review_helpers._market_regime(snapshot)
        template = _review_helpers._setup_type(side, combo, snapshot)
        data_usage = data_usage_from_snapshot(snapshot)
        data_combo = _review_helpers._data_combo_key(data_usage)
        analyst_payloads = _review_helpers._analyst_payloads(snapshot)
        action_contracts: Dict[str, Any] = {}
        learning_scopes: Dict[str, Any] = {}
        if isinstance(analyst_payloads, dict):
            for analyst_name, payload in analyst_payloads.items():
                if not isinstance(payload, dict):
                    continue
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                contract = metadata.get("action_evidence_contract")
                if isinstance(contract, dict):
                    action_contracts[str(analyst_name)] = contract
                    if isinstance(contract.get("learning_scope"), dict):
                        learning_scopes[str(analyst_name)] = contract["learning_scope"]
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
        setup_type = infer_setup_type(
            snapshot=snapshot,
            setup_type=template,
            opportunity_type=opportunity_type,
            opportunity_state=opportunity_state,
        )
        sector = _review_helpers._sector_for_ticker(cfg, ticker)
        target_lots = _review_helpers._safe_int(final_contract.get("target_lots"))
        current_lots = _review_helpers._safe_int(final_contract.get("current_lots"), 0)
        contract_intent = recommendation_intent_from_lots(
            current_lots=current_lots,
            target_lots=target_lots,
        )
        contract_action_taken = str(contract_intent.get("action") or "hold")
        executed_lots = sum(abs(_review_helpers._safe_int(tx.get("lots"))) for tx in txs if isinstance(tx, dict))
        tx_daily_pnl = sum(_review_helpers._safe_float(tx.get("daily_pnl")) for tx in txs if isinstance(tx, dict))
        tx_commission = sum(_review_helpers._safe_float(tx.get("commission")) for tx in txs if isinstance(tx, dict))
        ticker_outcome = _ticker_daily_outcome(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            ticker=ticker,
        )
        outcome_lots = _review_helpers._safe_int(ticker_outcome.get("abs_lots"))
        realized_pnl = (
            _review_helpers._safe_float(ticker_outcome.get("daily_pnl"))
            if ticker_outcome.get("row_count")
            else tx_daily_pnl
        )
        commission = (
            _review_helpers._safe_float(ticker_outcome.get("commission"))
            if ticker_outcome.get("row_count")
            else tx_commission
        )
        executed_lots = max(executed_lots, outcome_lots)
        execution_result = _review_helpers._execution_result_from_snapshot(snapshot)
        source_type = "trade" if executed_lots > 0 or ticker_outcome.get("row_count") else "no_trade"
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
        episode_sample = episode_samples_by_recommendation.get(rec_id)
        episode_result = (
            dict(episode_sample.get("result") or {})
            if isinstance(episode_sample, dict) and isinstance(episode_sample.get("result"), dict)
            else {}
        )
        episode_net_pnl = episode_result.get("episode_net_pnl")
        sample = {
            "ticker": ticker,
            "side": side,
            "sector": sector,
            "horizon_class": horizon,
            "market_regime": regime,
            "setup_type": template,
            "setup_type": setup_type,
            "data_combo": data_combo,
            "scope_key": scope_key,
            "source_type": "trade_episode" if episode_sample and source_type == "trade" else source_type,
            "recommendation_id": rec_id,
            "action_taken": contract_action_taken,
            "pm_action": final_contract.get("final_action") or contract_action_taken,
            "auditor_decision": (
                str(final_contract.get("audit_verdict") or final_contract.get("auditor_decision") or "")
            ),
            "trader_status": execution_result.get("outcome") or execution_result.get("status") or recommendation.get("status"),
            "target_lots": target_lots,
            "current_lots": current_lots,
            "executed_lots": executed_lots,
            "net_pnl": (
                _review_helpers._safe_float(episode_net_pnl)
                if episode_net_pnl is not None and source_type == "trade"
                else realized_pnl
            ),
            "commission": 0.0 if episode_net_pnl is not None and source_type == "trade" else commission,
            "holding_days": (
                _review_helpers._safe_int(episode_sample.get("holding_days"))
                if episode_sample and source_type == "trade"
                else 0
            ),
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
                "market_confirmation": snapshot.get("market_confirmation") if isinstance(snapshot.get("market_confirmation"), dict) else {},
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
        if episode_sample and source_type == "trade":
            sample["result"].update(episode_result)
            sample["result"]["reward_source"] = "complete_trade_episode"
            sample["result"]["single_day_net_pnl"] = realized_pnl - commission
            sample["result"]["episode_overrides_single_day_reward"] = True
            sample["evidence"]["episode_action_ledger"] = episode_sample.get("evidence", {})
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
        if execution_learning:
            execution_setup_type = (
                "execution_"
                + _clean_execution_token(execution_learning.get("execution_profile"), "timing")
                + "_setup"
            )
            execution_data_combo = f"{data_combo}|execution:{_clean_execution_token(execution_learning.get('execution_profile'), 'unknown')}"
            execution_scope_key = build_alpha_setup_scope_key(
                ticker=ticker,
                side=side,
                horizon_class=horizon,
                market_regime=regime,
                setup_type=execution_setup_type,
                data_combo=execution_data_combo,
            )
            execution_sample = {
                **sample,
                "source_type": "execution",
                "setup_type": execution_setup_type,
                "data_combo": execution_data_combo,
                "scope_key": execution_scope_key,
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
                    "market_confirmation": snapshot.get("market_confirmation") if isinstance(snapshot.get("market_confirmation"), dict) else {},
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
                    "setup_type": execution_setup_type,
                    "scope_key": execution_scope_key,
                    "source_type": "execution",
                    "lifecycle_state": execution_result.get("lifecycle_state"),
                    "profile_state_hint": execution_result.get("profile_state_hint"),
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


def _json_loads_safe(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(str(value))
    except Exception:
        return None


def _load_episode_payload(row: Mapping[str, Any]) -> Dict[str, Any]:
    inline = _json_loads_safe(row.get("payload_json"))
    if isinstance(inline, dict):
        return inline
    artifact_path = row.get("payload_artifact_path")
    if artifact_path:
        try:
            payload = load_externalized_json(None, artifact_path=str(artifact_path))
            if isinstance(payload, dict):
                return payload
        except Exception:
            return {}
    return {}


def _episode_alpha_setup_samples(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Dict[str, Dict[str, Any]]:
    try:
        cursor.execute(
            """
            SELECT *
            FROM trade_episode_memory
            WHERE config_id = ?
              AND COALESCE(close_date, episode_date, trading_date) <= ?
            ORDER BY COALESCE(close_date, episode_date, trading_date), last_reviewed_at
            """,
            (config_id, str(trading_date)[:10]),
        )
        rows = [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error:
        return {}
    samples: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        payload = _load_episode_payload(row)
        rec_id = str(payload.get("open_recommendation_id") or "")
        if not rec_id:
            rec_id = str(((payload.get("pair") or {}) if isinstance(payload.get("pair"), dict) else {}).get("open_recommendation_id") or "")
        if not rec_id:
            continue
        ticker = str(row.get("ticker") or "").upper()
        side = str(row.get("side") or "").lower()
        if not ticker or side not in {"long", "short"}:
            continue
        signal_snapshot = payload.get("signal_snapshot") if isinstance(payload.get("signal_snapshot"), dict) else {}
        opportunity_type = str(payload.get("opportunity_type") or "")
        opportunity_state = str(payload.get("opportunity_state") or "")
        setup_type = infer_setup_type(
            snapshot=signal_snapshot,
            setup_type=row.get("setup_type"),
            opportunity_type=opportunity_type,
            opportunity_state=opportunity_state,
        )
        data_usage = payload.get("data_usage_summary") if isinstance(payload.get("data_usage_summary"), dict) else {}
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
        samples[rec_id] = {
            "ticker": ticker,
            "side": side,
            "sector": row.get("sector") or "unknown",
            "horizon_class": row.get("horizon_class") or "unknown",
            "market_regime": row.get("market_regime") or "unknown",
            "setup_type": row.get("setup_type") or "*",
            "setup_type": setup_type,
            "data_combo": data_combo,
            "scope_key": scope_key,
            "source_type": "trade_episode",
            "recommendation_id": rec_id,
            "action_taken": "open_long" if side == "long" else "open_short",
            "target_lots": 0,
            "current_lots": 0,
            "executed_lots": 1,
            "net_pnl": net_pnl,
            "commission": 0.0,
            "holding_days": int(float(row.get("holding_days") or 0)),
            "outcome_label": "profit" if net_pnl > 0 else "loss" if net_pnl < 0 else "flat_or_no_trade",
            "opportunity_state": opportunity_state or "watch_for_trigger",
            "evidence": {
                "trade_episode_memory_id": row.get("id"),
                "open_date": row.get("open_date"),
                "close_date": row.get("close_date"),
                "episode_date": row.get("episode_date"),
                "lesson_text": row.get("lesson_text"),
                "opportunity_type": opportunity_type,
                "opportunity_state": opportunity_state,
            },
            "result": {
                "episode_net_pnl": net_pnl,
                "episode_reward_source": "trade_episode_memory",
                "episode_memory_id": row.get("id"),
                "open_date": row.get("open_date"),
                "close_date": row.get("close_date"),
                "holding_days": int(float(row.get("holding_days") or 0)),
                "return_on_notional": float(row.get("return_on_notional") or 0.0),
                "no_future_leakage": True,
            },
        }
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
    min_candidate_net_pnl = _review_helpers._safe_float(policy_cfg.get("min_candidate_net_pnl"), 1000.0)
    min_candidate_confidence = _review_helpers._safe_float(policy_cfg.get("min_candidate_confidence"), 0.30)
    min_candidate_trade_count = max(1, _review_helpers._safe_int(policy_cfg.get("min_candidate_trade_count"), 1))
    cap_multiplier = max(0.0, min(1.0, _review_helpers._safe_float(policy_cfg.get("cap_multiplier"), 0.50)))
    probe_multiplier = max(0.0, min(1.0, _review_helpers._safe_float(policy_cfg.get("probe_multiplier"), 0.75)))

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
        scope = {
            "ticker": ticker,
            "side": side,
            "setup_type": "*",
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
        elif (
            state in {"candidate", "watchlist"}
            and trade_count >= min_candidate_trade_count
            and net_pnl >= min_candidate_net_pnl
            and confidence >= min_candidate_confidence
        ):
            policy_type = "fast_candidate_alpha"
            policy_action = "probe"
            multiplier = probe_multiplier
            reason = "early positive alpha setup can receive future same-scope tiny probe"
            maturity_state = "alpha_setup_fast_candidate"
            event_type = "alpha_setup_fast_candidate"
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
            horizon_class=scope["horizon_class"],
            market_regime=scope["market_regime"],
            policy_type=policy_type,
            policy_action=policy_action,
            multiplier=multiplier,
            confidence_score=confidence,
            sample_count=sample_count,
            reason=reason,
            source_event_id=event_id,
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
    settlement_row: Optional[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    no_trade_reason_counter: Counter,
) -> int:
    learning_cfg = cfg.get("learning", {}) or {}
    review_cfg = learning_cfg.get("researcher_causal_review") or {}
    if not bool(review_cfg.get("enabled", False)):
        return 0
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
    raw_response = ""
    output = CausalReviewLLMOutput()
    if bool(review_cfg.get("use_llm", False)):
        try:
            from llm.inference import agent_call

            output = agent_call(
                prompt=prompt,
                llm_config=cfg.get("llm", {}),
                pydantic_model=CausalReviewLLMOutput,
            )
            raw_response = _review_helpers._json_dumps(output.model_dump())
        except Exception as exc:
            raw_response = f"llm_causal_research_failed: {exc}"
            logger.warning(f"Researcher LLM causal review failed on {trading_date}: {exc}")
    else:
        raw_response = "llm disabled; deterministic candidate only"

    note_id = str(uuid.uuid4())
    prompt_ext = externalize_text_for_db(
        prompt,
        category="researcher_llm_notes",
        record_id=note_id,
        field_name="raw_prompt",
        config_id=config_id,
        trading_date=trading_date,
    )
    response_ext = externalize_text_for_db(
        raw_response,
        category="researcher_llm_notes",
        record_id=note_id,
        field_name="raw_response",
        config_id=config_id,
        trading_date=trading_date,
    )
    payload_ext = externalize_json_for_db(
        evidence,
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
        raw_prompt=prompt_ext.inline_value,
        raw_response=response_ext.inline_value,
        created_at=_review_helpers._utc_now(),
        payload_json=payload_ext.inline_value,
        raw_prompt_artifact_path=prompt_ext.artifact_path,
        raw_prompt_sha256=prompt_ext.sha256,
        raw_prompt_size=prompt_ext.size_bytes,
        raw_prompt_summary_json=prompt_ext.summary_json,
        raw_response_artifact_path=response_ext.artifact_path,
        raw_response_sha256=response_ext.sha256,
        raw_response_size=response_ext.size_bytes,
        raw_response_summary_json=response_ext.summary_json,
        payload_artifact_path=payload_ext.artifact_path,
        payload_sha256=payload_ext.sha256,
        payload_size=payload_ext.size_bytes,
        payload_summary_json=payload_ext.summary_json,
    )
    candidate_payload = output.model_dump() if hasattr(output, "model_dump") else {}
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
               market_regime, open_date, close_date, holding_days, net_pnl,
               return_on_notional, outcome_label, lesson_text
        FROM trade_episode_memory
        WHERE config_id = ?
          AND (close_date IS NULL OR close_date <= ?)
        ORDER BY ABS(net_pnl) DESC, close_date DESC, created_at DESC
        LIMIT ?
        """,
        (config_id, trading_date, int(limit)),
    )
    return [dict(row) for row in cursor.fetchall()]


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

    prompt = build_researcher_exploratory_prompt(
        trading_date=trading_date,
        episodes_json=_review_helpers._json_dumps({"trading_date": trading_date, "episodes": episodes})[:12000],
    )
    output = ExploratoryHypothesisLLMOutput()
    raw_response = ""
    if bool(research_cfg.get("use_llm", True)):
        try:
            from llm.inference import agent_call

            output = agent_call(
                prompt=prompt,
                llm_config=cfg.get("llm", {}),
                pydantic_model=ExploratoryHypothesisLLMOutput,
            )
            raw_response = _review_helpers._json_dumps(output.model_dump())
        except Exception as exc:
            raw_response = f"llm_exploratory_research_failed: {exc}"
            logger.warning(f"Researcher exploratory research failed on {trading_date}: {exc}")
    else:
        raw_response = "llm disabled; no exploratory hypotheses generated"

    note_id = str(uuid.uuid4())
    prompt_ext = externalize_text_for_db(
        prompt,
        category="researcher_llm_notes",
        record_id=note_id,
        field_name="raw_prompt",
        config_id=config_id,
        trading_date=trading_date,
    )
    response_ext = externalize_text_for_db(
        raw_response,
        category="researcher_llm_notes",
        record_id=note_id,
        field_name="raw_response",
        config_id=config_id,
        trading_date=trading_date,
    )
    evidence = {"agent_name": "researcher", "trading_date": trading_date, "episodes": episodes}
    payload_ext = externalize_json_for_db(
        evidence,
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
        raw_prompt=prompt_ext.inline_value,
        raw_response=response_ext.inline_value,
        created_at=_review_helpers._utc_now(),
        payload_json=payload_ext.inline_value,
        raw_prompt_artifact_path=prompt_ext.artifact_path,
        raw_prompt_sha256=prompt_ext.sha256,
        raw_prompt_size=prompt_ext.size_bytes,
        raw_prompt_summary_json=prompt_ext.summary_json,
        raw_response_artifact_path=response_ext.artifact_path,
        raw_response_sha256=response_ext.sha256,
        raw_response_size=response_ext.size_bytes,
        raw_response_summary_json=response_ext.summary_json,
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
    for item in (output.hypotheses or [])[:max_hypotheses]:
        payload = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        text = str(payload.get("hypothesis_text") or "").strip()
        if not text:
            continue
        confidence = max(0.0, min(1.0, _review_helpers._safe_float(payload.get("confidence_score"), 0.0)))
        ticker = str(payload.get("ticker") or "*").upper()
        sector = str(payload.get("sector") or "*")
        side = str(payload.get("side") or "*").lower()
        horizon = str(payload.get("horizon_class") or "*")
        regime = str(payload.get("market_regime") or "*")
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
            sample_count=len(episodes),
            confidence_score=confidence,
        )
        event_id = research_memory_writers.insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="exploratory_hypothesis",
            scope_type="research",
            scope_key=f"{ticker}:{sector}:{side}:{horizon}:{regime}",
            evidence={"episode_count": len(episodes), "note_id": note_id, "agent_name": "researcher"},
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
            "hard_constraints": {
                "max_total_margin_ratio": cfg.get("max_total_margin_ratio", 0.20),
                "structured_hypothesis_only": True,
                "candidate_hypothesis_cannot_control_position": True,
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
            scope_key=f"{ticker}:{sector}:{side}:{horizon}:{regime}",
            ticker=ticker,
            sector=sector,
            side=side,
            horizon_class=horizon,
            market_regime=regime,
            hypothesis_text=text,
            evidence_summary=str(payload.get("evidence_summary") or ""),
            suggested_use=suggested_use,
            confidence_score=confidence,
            sample_count=len(episodes),
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
    return {"rows": rows, "status": "applied" if rows else "no_hypotheses", "episode_count": len(episodes)}


def apply_researcher_learning(
    *,
    db: Any,
    cursor: sqlite3.Cursor,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    settlement_row: Optional[Dict[str, Any]],
    recommendations: List[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    no_trade_reason_counter: Counter,
    transactions_by_recommendation: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Persist Phase4 learning after reviewer validation passes."""
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
    perf_counts = research_memory_writers.write_template_and_analyst_learning(
        cursor,
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
        settlement_row=settlement_row,
        strategy_recommendations=strategy_recommendations,
        no_trade_reason_counter=no_trade_reason_counter,
    )
    causal_rule_validation = research_memory_writers.write_validated_causal_policy_rules(
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
        "exploratory_hypotheses": exploratory_hypotheses,
    }

