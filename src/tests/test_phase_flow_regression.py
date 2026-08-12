import json
import os
import hashlib
import inspect
import sqlite3
import sys
import tempfile
import unittest
import yaml
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evaluation import (
    calculate_futures_metrics,
    calculate_futures_transaction_win_rate,
    evaluate_config,
)
from evaluation.evaluation import calculate_learning_usage_metrics, calculate_optimization_acceptance_metrics
from run import plot_config
from tools.agent_tools.decision.pm_risk_gate import PMRiskGate, PMRiskGateInput
from apis.router import Router
from database.sqlite_helper import SQLiteDB
from database.evaluation_helper import EvaluationHelper
from database.build_check_db import rebuild_check_db, validate_check_db_consistency
from database.validate_artifacts import validate_artifacts
from graph.constants import Signal
from graph.schema import AnalystSignal, FuturesAction, FuturesDecision, FuturesRecommendation, FuturesTransaction, Portfolio, Position, RecommendationAction
from graph.schema import RecommendationSourceType, RecommendationStatus, TradingPhase
from agents.decision_team.portfolio_manager import (
    RiskLevel,
    _apply_capital_utilization_control,
    _apply_alpha_setup_ev_position_control,
    _apply_drawdown_and_ticker_loss_control,
    _apply_holding_rebalance_control,
    _load_opening_fac_context,
    _opening_fac_position_invalidation_breached,
    _apply_position_budget_policy_for_new_entry,
    _alpha_setup_action_value_trace,
    _action_value_can_support_real_amplification,
    _action_value_scope_quality,
    _conditional_monitor_probe_seed_plan,
    _final_contract_authority,
    _finalize_hold_exit_learning_explanation,
    _is_lifecycle_exit_required_reason,
    _minimum_real_probe_candidate_ratio,
    _news_high_quality_override,
    _positive_open_action_value_seed,
    _qualified_analyst_tradeable_probe_candidate,
    _qualified_real_probe_release,
    _negative_hold_or_positive_exit_action_value,
    _should_attempt_minimum_real_probe,
    _preserve_existing_lot_when_hold_ratio_survives,
    _apply_trade_frequency_control,
    _apply_winning_template_continuation_control,
    _build_pm_memory_state,
    _build_blocked_pm_memory_state_update,
    _build_pm_decision_context,
    _build_final_action_contract,
    _build_pm_landing_consistency_audit,
    _contract_safe_learning_to_position_summary,
    _build_release_block_diagnostics,
    _canonical_action_evidence_contract,
    _audit_frozen_step4_pm_memory,
    _validate_required_analyst_signals,
    _current_open_evidence_snapshot,
    _ExplicitPMLearningScopeDBView,
    _final_contract_scope_from_scc,
    _formal_learning_identity_for_side,
    _scorecard_probe_seed,
    _side_opportunity_state_summary,
    _append_unique_action_values,
    _attach_incomplete_prior_diagnostics_to_contract_state,
    _normalize_alpha_setup_action_value,
    _select_learning_trace_action_values,
    _position_pnl_ratio,
    finalize_pm_full_market_contracts,
    _sign_pm_memory_state,
    _to_recommendation_action,
    portfolio_agent_futures,
)
from agents.decision_team.signal_collector import signal_collector_agent
from tools.common.signal_evidence_collection import (
    build_pm_evidence_signals_from_scc,
    build_signal_collection_contract,
)
from tools.common.execution_trigger_semantics import (
    canonical_entry_invalidation_condition,
    canonical_entry_trigger,
    trigger_source_for_analyst_profile,
)
from tests.contract_test_fixtures import build_test_aec
from agents.execution_team.trader import (
    _execute_pending_forced_risk_before_strategy,
    _execution_contract_from_snapshot,
    _final_contract_execution_fields,
    _final_action_contract_from_snapshot,
    _setup_execution_learning_context,
)
from agents.execution_team import trader as trader_module
from tools.agent_tools.analysis.analyst_quality import (
    apply_trade_research_contract,
    build_technical_context,
    summarize_news_events,
)
from tools.agent_tools.analysis.analyst_learning_calibration import calibrate_signal_with_learning_context
from tools.agent_tools.analysis.analyst_learning_context import (
    _safe_analyst_action_value_projection,
)
from tools.common.alpha_setup import (
    _entry_quality_outcome_from_sample,
    _signal_calibration_contract,
)
from tools.agent_tools.decision.pm_signal_fusion import (
    build_opportunity_scorecard,
)
from tools.agent_tools.decision.pm_full_market_capital_deployment import (
    CAPITAL_LAYER_ALPHA_SCALE,
    CAPITAL_LAYER_EXPLORATION,
    CAPITAL_LAYER_REAL_BUDGET,
    CAPITAL_RATIO_SOURCE_EXPLORATION,
    RANK_CAPITAL_ROLE_EXPLORATION,
    RANK_CAPITAL_ROLE_REAL_BUDGET,
    _ensure_final_rank_score_fields,
)
from tools.agent_tools.decision.pm_lifecycle_action_port import classify_lifecycle_action_port
from tools.common.final_action_semantics import full_market_rank_source_payload
from tools.common.contracts import validate_pm_artifact_boundary
from tools.agent_tools.decision.pm_decision_memory_retrieval import retrieve_pm_memory
from run.order import _reconcile_rollover_with_strategy_target, _translate_pre_open_recommendation_to_order
from tools.agent_tools.execution.trader_futures_execution import ExecutionBlocked, FuturesExecutionEngine
from tools.agent_tools.execution.accountant_futures_settlement import FuturesDailySettlement
from tools.agent_tools.execution.trader_intraday_execution import (
    resolve_intraday_execution_basis,
    select_intraday_execution,
)
from tools.agent_tools.execution.trader_entry_timing import phase2_entry_audit
from tools.agent_tools.research.reviewer_phase4_review import (
    _apply_net_exposure_review,
    _build_daily_transaction_report,
    _build_capital_deployment_diagnostics,
    _collect_recommendation_quality_warnings,
    _data_combo_key,
    _failure_family_actions,
    _learning_mechanisms_from_recommendation,
    _loss_failure_family,
    _execution_result_from_snapshot,
    _review_recommendation_execution_facts,
)
from tools.common.adaptive_policy_safety import filter_adaptive_policy_state_for_pm
from tools.agent_tools.decision.pm_capital_allocator import adaptive_policy_record
from util.futures_audit import (
    build_audit_payload,
    build_execution_learning_trace,
    calculate_margin_audit,
    categorize_no_trade_reason,
    classify_zero_transaction_day,
    classify_no_trade_reasons,
    infer_target_lots,
    infer_no_trade_reason,
    normalize_no_trade_reason,
    set_execution_result,
    validate_execution_artifact_boundary,
)
from util.config_normalizer import normalize_config
from util.futures_trade_pairs import build_completed_trade_pairs, summarize_trade_pairs
from util.trading_calendar import get_previous_trading_day, map_datetime_to_futures_trading_day
from run.validate_phase_flow import _expected_settlement_balance_change
from tools.agent_tools.research.research_review_helpers import _expected_settlement_equity_change
from tools.agent_tools.execution.trader_execution_exit_policy import (
    evaluate_exit_policy,
    resolve_atr_protection,
)
from tools.common.order_semantics import (
    build_lot_intent_consistency,
    phase2_order_intent_from_lots,
    recommendation_intent_from_lots,
)
from tools.agent_tools.decision.pm_reason_effects import reason_effect_summary
from apis.pandaai import PandaAIAPI
from graph.workflow import AgentWorkflow


def _canonical_pm_execution_metadata(
    *,
    side: str = "long",
    state: str = "tradeable_candidate",
    confirmed: bool = True,
    ticker: str = "BU",
) -> dict:
    return {
        "action_evidence_contract": build_test_aec(
            "technical",
            ticker=ticker,
            signal="Bullish" if side == "long" else "Bearish",
            side=side,
            opportunity_state=state,
            trigger_valid=confirmed,
            current_trigger_confirmed=confirmed,
            invalidation_present=True,
        )
    }


class _FakeRouter:
    def __init__(self, trade_dates):
        self.trade_dates = trade_dates
        self.api = self

    def get_futures_daily_candles_optimized(self, underlying_code, is_main, start_date, end_date):
        quotes = []
        for trade_date in self.trade_dates:
            trade_dt = datetime.strptime(trade_date, "%Y-%m-%d")
            if start_date <= trade_dt <= end_date:
                quotes.append(SimpleNamespace(trade_date=trade_date))
        return quotes


def _signal_collection_contract_fixture(
    ticker: str = "BU",
    *,
    side: str = "long",
    opportunity_state: str = "watch_for_trigger",
    trigger_valid: bool = False,
    current_trigger_confirmed: bool = False,
    entry_trigger: str | None = None,
    invalidation_condition: str | None = None,
) -> dict:
    signal_value = "Bullish" if side == "long" else "Bearish"
    action_contract = build_test_aec(
        "technical",
        ticker=ticker,
        trading_date="2025-03-25",
        signal=signal_value,
        side=side,
        confidence=0.8,
        opportunity_state=opportunity_state,
        trigger_valid=trigger_valid,
        current_trigger_confirmed=current_trigger_confirmed,
        invalidation_present=True,
        entry_trigger=entry_trigger,
        invalidation_condition=invalidation_condition,
        extra={
            "invalidation_level": 95.0 if side == "long" else 105.0,
            "position_invalidation_level": 94.0 if side == "long" else 106.0,
        },
    )
    signal = SimpleNamespace(
        agent_name="technical",
        metadata={
            "action_evidence_contract": action_contract,
            "signal_record_id": f"signal-{ticker}-technical",
        },
    )
    return build_signal_collection_contract(
        ticker=ticker,
        trading_date="2025-03-25",
        analyst_signals=[signal],
        enabled_analysts=["technical"],
    )


def _scc_from_test_payloads(**payloads) -> dict:
    return {
        "source_contracts": [
            {
                "analyst": analyst,
                "signal_record_id": f"signal-{analyst}",
                "action_evidence_contract": {"analyst": analyst, **payload},
            }
            for analyst, payload in payloads.items()
        ]
    }


def _pm_state_fixture(contract: dict, *, ticker: str = "", scorecard: dict | None = None, execution_fields: dict | None = None) -> dict:
    contract = dict(contract or {})
    current_lots = int(contract.get("current_lots") or 0)
    target_lots = int(contract.get("target_lots") or 0)
    final_action = str(contract.get("final_action") or "").lower()
    authority_type = str(contract.get("authority_type") or "")
    if not authority_type:
        if final_action == "open_real":
            authority_type = "real_budget_entry"
        elif final_action in {"open_probe", "conditional_open"}:
            authority_type = "exploration_probe"
        else:
            authority_type = "not_applicable"
    position_ratio = float(
        contract.get("target_position_ratio")
        if contract.get("target_position_ratio") is not None
        else contract.get("target_margin_ratio_estimate") or 0.0
    )
    if authority_type == "exploration_probe":
        position_ratio = 0.008 if target_lots >= 0 else -0.008
        contract["target_position_ratio"] = position_ratio
        contract["target_margin_ratio_estimate"] = 0.008
    if authority_type:
        contract["authority_type"] = authority_type
    account_equity = 1_000_000.0
    execution_fields = dict(execution_fields or {})
    execution_fields.pop("final_action_contract", None)
    ticker_value = ticker or contract.get("ticker") or "BU"
    target_side = "long" if target_lots > 0 else "short" if target_lots < 0 else "long"
    increases_risk = bool(
        target_lots != 0
        and (
            current_lots == 0
            or (
                (current_lots > 0) == (target_lots > 0)
                and abs(target_lots) > abs(current_lots)
            )
        )
    )
    conditional = bool(
        contract.get("conditional_trigger_authority")
        or contract.get("requires_intraday_confirmation")
    )
    if "signal_collection_contract" not in execution_fields:
        execution_fields["signal_collection_contract"] = _signal_collection_contract_fixture(
            ticker_value,
            side=target_side,
            opportunity_state=(
                "watch_for_trigger"
                if not increases_risk or conditional
                else "tradeable_candidate"
            ),
            trigger_valid=bool(increases_risk and not conditional),
            current_trigger_confirmed=bool(increases_risk and not conditional),
            entry_trigger=None,
            invalidation_condition=str(
                contract.get("invalidation")
                or contract.get("invalidation_condition")
                or canonical_entry_invalidation_condition("breakout", target_side)
            ),
        )
    execution_fields.setdefault("pm_six_step_stage", "steps_1_4_candidate_generated")
    primary_lifecycle_action_port = classify_lifecycle_action_port(contract)
    collection = execution_fields["signal_collection_contract"]
    final_entry_authority = {
        "authority_type": authority_type or "not_applicable",
        "decision": contract.get("authority_decision") or contract.get("decision") or "test_state",
        "authority_decision": contract.get("authority_decision") or contract.get("decision") or "test_state",
        "reason_codes": list(contract.get("reason_codes") or []),
        "requires_authority": bool(target_lots != current_lots and target_lots != 0),
        "open_action_evidence": bool(contract.get("open_action_evidence")),
        "strong_current_evidence": bool(contract.get("strong_current_evidence")),
        "tradeable_state": bool(contract.get("tradeable_state") or authority_type == "real_budget_entry"),
        "conditional_trigger_authority": conditional,
        "requires_intraday_confirmation": conditional,
        "can_execute_without_intraday_trigger": False if conditional else None,
        "max_allowed_margin_ratio": float(
            contract.get("max_allowed_margin_ratio")
            if contract.get("max_allowed_margin_ratio") is not None
            else abs(position_ratio)
        ),
    }
    if not all(
        execution_fields.get(field) not in (None, "")
        for field in ("execution_profile", "trigger_source")
    ):
        generated_execution_fields = _build_pm_decision_context(
            ticker=ticker_value,
            target_lots=target_lots,
            current_price=100.0,
            position_ratio=position_ratio,
            risk_level=RiskLevel.SAFE,
            long_scores={"confidence": 0.8 if target_side == "long" else 0.0},
            short_scores={"confidence": 0.8 if target_side == "short" else 0.0},
            margin_rate=0.10,
            current_lots=current_lots,
            analyst_signals=build_pm_evidence_signals_from_scc(collection),
            final_entry_authority=final_entry_authority,
            trading_date="2025-03-25",
            recommendation_intent=recommendation_intent_from_lots(current_lots, target_lots),
            control_reasons=list(contract.get("reason_codes") or []),
        )
        execution_fields.update(generated_execution_fields)
    return {
        **contract,
        "ticker": ticker or contract.get("ticker") or "",
        "current_lots": current_lots,
        "target_lots": target_lots,
        "position_ratio": position_ratio,
        "margin_required": abs(position_ratio) * account_equity,
        "account_equity": account_equity,
        "lots_to_trade": abs(target_lots - current_lots),
        "lots_to_trade_reason": ",".join(str(item) for item in (contract.get("reason_codes") or []) if item) or "test_pm_state",
        "recommendation_intent": recommendation_intent_from_lots(current_lots, target_lots),
        "final_entry_authority": final_entry_authority,
        "control_reasons": list(contract.get("reason_codes") or []),
        "control_diagnostics": {"primary_lifecycle_action_port": primary_lifecycle_action_port},
        "opportunity_scorecard": dict(scorecard or {}),
        "market_confirmation": {},
        "alpha_setup_action_values": [],
        "signal_collection_contract": collection,
        "execution_contract_fields": execution_fields,
    }


def _pm_state_from_recommendation_fixture(recommendation: FuturesRecommendation) -> dict:
    snapshot = dict(recommendation.signal_snapshot or {})
    contract = snapshot.pop("final_action_contract")
    state = _pm_state_fixture(
        contract,
        ticker=recommendation.underlying_code,
        scorecard=snapshot.get("opportunity_scorecard") if isinstance(snapshot.get("opportunity_scorecard"), dict) else {},
        execution_fields=snapshot,
    )
    state["recommendation_context"] = recommendation.model_dump(
        exclude={"id", "action", "lots", "signal_snapshot"}
    )
    return state


def _build_signed_pm_recommendation(**kwargs):
    state_update = dict(kwargs.pop("pm_state_update", None) or {})
    if not state_update:
        current_lots = 0
        target_lots = 0
        state_update = _build_blocked_pm_memory_state_update(
            ticker=str(kwargs.get("ticker") or ""),
            current_lots=current_lots,
            target_lots=target_lots,
            reason="test_non_new_risk",
            authority_type="not_applicable",
            account_equity=float(getattr(kwargs.get("portfolio"), "account_equity", 0.0) or 1.0),
            signal_collection_contract=_signal_collection_contract_fixture(str(kwargs.get("ticker") or "")),
        )
        kwargs["plan_snapshot"] = {
            **dict(kwargs.get("plan_snapshot") or {}),
            "signal_collection_contract": _signal_collection_contract_fixture(str(kwargs.get("ticker") or "")),
        }
    kwargs["pm_state_update"] = state_update
    state = _build_pm_memory_state(**kwargs)
    state.pop("capital_deployment", None)
    if (
        int(state.get("target_lots") or 0) != int(state.get("current_lots") or 0)
        and classify_lifecycle_action_port(state).get("requires_full_market_rank")
    ):
        state["capital_deployment"] = {
            "selected_for_capital_deployment": True,
            "capital_allocation_reason": "selected_by_full_market_pm_capital_queue:test_step5_deployment",
            "original_target_lots": int(state.get("target_lots") or 0),
            "deployed_target_lots": int(state.get("target_lots") or 0),
            "deployed_lots_delta": int(state.get("target_lots") or 0) - int(state.get("current_lots") or 0),
            "opportunity_rank": 1,
            "rank_capital_role": "best_real_budget_candidate",
            "capital_layer": "real_budget_entry",
            "capital_ratio_source": "normal_trade_margin_ratio",
            "rank_reason": "test_step5_deployment",
            "rank_input_components": {},
            "rank_score": 1.0,
            "rank_source": "full_market_capital_deployment",
            "rank_scope": "daily_full_market_capital_pool",
            "capital_rank_generated_by": "pm_full_market_capital_deployment",
            "lifecycle_learning_trace": {
                "rank_lifecycle": "open_add_new_risk",
                "used_lanes": [],
                "decision_learning_rows": [],
                "trigger_profile_learning_rows": [],
                "execution_profile_learning_direct_to_rank": False,
                "trigger_profile_learning_direct_to_rank": False,
                "execution_profile_signal_direct_to_rank": False,
            },
            "learning_impact_delta": {
                "net_rank_learning_delta": 0.0,
                "execution_profile_learning_direct_to_rank": False,
            },
            "rank_semantics_version": "agentquant.capital_priority_rank.v1",
            "opportunity_rank_meaning": "rank_1_is_current_highest_capital_priority_not_trade_authority",
            "rank_is_capital_priority": True,
            "rank_is_not_trade_authority": True,
        }
    return _sign_pm_memory_state(state)


def _persist_pm_state_fixtures(workflow, generated) -> dict:
    states = [
        (ticker, _pm_state_from_recommendation_fixture(recommendation))
        for ticker, recommendation in generated
    ]
    return dict(workflow._persist_pm_full_market_contracts(states))


class PMLearningSummaryFieldMappingRegressionTest(unittest.TestCase):
    def test_lifecycle_summary_maps_internal_field_names(self):
        summary = _contract_safe_learning_to_position_summary(
            {
                "holding_lifecycle": {
                    "decision": "keep_profitable_supported_exit_deferred",
                    "lifecycle_classification": "normal",
                    "held_days": 4,
                    "current_side": "long",
                    "raw_target_side": "short",
                    "loss_revalidation_due": True,
                    "loss_revalidation_failed": False,
                    "confirmation_score": 0.72,
                    "private_pm_internal_trace": {"must_not_leak": True},
                }
            }
        )

        lifecycle = summary["holding_lifecycle"]
        self.assertEqual(lifecycle["holding_days"], 4)
        self.assertEqual(lifecycle["target_side"], "short")
        self.assertEqual(lifecycle["market_confirmation_score"], 0.72)
        self.assertNotIn("held_days", lifecycle)
        self.assertNotIn("raw_target_side", lifecycle)
        self.assertNotIn("confirmation_score", lifecycle)
        self.assertNotIn("private_pm_internal_trace", lifecycle)


class TradingCalendarRegressionTest(unittest.TestCase):
    def test_previous_trading_day_expands_lookback_window(self):
        router = _FakeRouter(["2025-01-27", "2025-02-05"])
        previous = get_previous_trading_day(
            router=router,
            trading_date="2025-02-05",
            underlying_code="RB",
            lookback_days=3,
            max_lookback_days=20,
        )
        self.assertEqual(previous.strftime("%Y-%m-%d"), "2025-01-27")

    def test_friday_night_session_maps_to_next_monday_trading_day(self):
        router = _FakeRouter(["2025-03-07", "2025-03-10"])
        trading_day = map_datetime_to_futures_trading_day(
            router=router,
            timestamp="2025-03-07 21:15:00",
            underlying_code="RB",
        )

        self.assertEqual(trading_day.strftime("%Y-%m-%d"), "2025-03-10")

    def test_day_session_keeps_calendar_trading_day(self):
        router = _FakeRouter(["2025-03-07", "2025-03-10"])
        trading_day = map_datetime_to_futures_trading_day(
            router=router,
            timestamp="2025-03-07 14:55:00",
            underlying_code="RB",
        )

        self.assertEqual(trading_day.strftime("%Y-%m-%d"), "2025-03-07")


class AgentBoundaryFidelityRegressionTest(unittest.TestCase):
    def test_analyst_to_pm_action_evidence_contract_overrides_raw_signal_state(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.88,
            trigger_valid=True,
            opportunity_state="tradeable_candidate",
            entry_trigger="raw immediate breakdown",
            metadata={
                "action_evidence_contract": {
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "opportunity_state": "watch_for_trigger",
                    "entry_trigger": "break below 3520 with volume confirmation",
                    "invalidation_present": True,
                    "invalidation_condition": "back above 3560",
                }
            },
        )

        contract = _canonical_action_evidence_contract(signal)
        state_summary = _side_opportunity_state_summary([signal], "short")

        self.assertFalse(contract["trigger_valid"])
        self.assertEqual(contract["opportunity_state"], "watch_for_trigger")
        self.assertFalse(state_summary["has_tradeable_support"])
        self.assertTrue(state_summary["has_watch_for_trigger_support"])
        self.assertEqual(state_summary["opportunity_state_counts"], {"watch_for_trigger": 1})

    def test_pm_to_trader_final_contract_preserves_authority_without_rank_execution_rights(self):
        final_contract = {
            "contract_type": "strategy",
            "final_action": "open_probe",
            "current_lots": 0,
            "target_lots": -2,
            "lots_delta": -2,
            "target_position_ratio": -0.012,
            "execution_profile": "intraday_pullback_confirmed",
            "trigger_source": "conditional_monitor",
            "entry_trigger": "trade below 3520 after pullback",
            "invalidation": "back above 3560",
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "authority_type": "conditional_monitor_probe",
            "max_allowed_margin_ratio": 0.015,
            "reason_codes": ["conditional_monitor_probe_seed"],
            "opportunity_rank": 1,
            "opportunity_score": 0.74,
            "opportunity_score_components": {"positive_learning": 0.2},
            "analyst_execution_roles": {
                "technical": {
                    "action_evidence_contract": {
                        "trigger_valid": False,
                        "opportunity_state": "watch_for_trigger",
                        "entry_trigger": "trade below 3520 after pullback",
                    },
                    "learning_scope": {
                        "consumer_scope": "analyst_calibration",
                        "learning_lane": "signal_calibration",
                    },
                }
            },
        }
        snapshot = {"final_action_contract": final_contract}

        contract = _final_action_contract_from_snapshot(snapshot)
        execution_contract = _execution_contract_from_snapshot(snapshot)
        learning_context = _setup_execution_learning_context(snapshot)

        self.assertEqual(contract["target_lots"], -2)
        self.assertEqual(contract["lots_delta"], -2)
        self.assertEqual(learning_context["final_contract_execution_fields"]["target_lots"], -2)
        self.assertEqual(learning_context["preferred_side"], "short")
        self.assertEqual(learning_context["consumer_scope"], "trader_execution_learning")
        self.assertEqual(learning_context["learning_lane"], "execution")
        self.assertEqual(execution_contract["execution_profile"], "intraday_pullback_confirmed")
        self.assertEqual(execution_contract["entry_trigger"], "trade below 3520 after pullback")
        self.assertNotIn("opportunity_rank", execution_contract)
        self.assertNotIn("opportunity_score", execution_contract)
        self.assertNotIn("opportunity_score_components", execution_contract)
        self.assertNotIn("target_lots", execution_contract)
        self.assertNotIn("lots_delta", execution_contract)
        self.assertNotIn("final_action_contract", learning_context)
        self.assertNotIn("opportunity_rank", learning_context["final_contract_execution_fields"])
        self.assertNotIn("opportunity_score", learning_context["final_contract_execution_fields"])
        self.assertNotIn("opportunity_score_components", learning_context["final_contract_execution_fields"])

    def test_trader_to_researcher_execution_result_preserves_no_trade_fact_and_learning_scope(self):
        snapshot = {
            "ticker": "HC",
            "final_action_contract": {
                "contract_type": "strategy",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": -1,
                "lots_delta": -1,
                "execution_profile": "intraday_trigger_confirmed",
                "trigger_reason": "breakdown_trigger",
            },
        }

        set_execution_result(
            snapshot,
            outcome="skipped",
            status=RecommendationStatus.SKIPPED.value,
            transaction_count=0,
            no_trade_reason="intraday_trigger_not_met",
            warning_message="intraday_trigger_not_met",
        )
        expected_trace = build_execution_learning_trace(
            snapshot,
            outcome="skipped",
            status=RecommendationStatus.SKIPPED.value,
            no_trade_reason="intraday_trigger_not_met",
            no_trade_reason_category=snapshot["execution_result"]["no_trade_reason_category"],
            transaction_count=0,
        )

        result = _execution_result_from_snapshot(snapshot)
        trace = result["execution_learning_trace"]

        self.assertEqual(result["status"], RecommendationStatus.SKIPPED.value)
        self.assertEqual(result["transaction_count"], 0)
        self.assertEqual(result["no_trade_reason"], "intraday_trigger_not_met")
        self.assertEqual(trace, expected_trace)
        self.assertEqual(trace["consumer_scope"], "trader_execution_learning")
        self.assertEqual(trace["learning_lane"], "execution")
        self.assertIn("HC|intraday_trigger_confirmed|breakdown_trigger|execution", trace["execution_retrieval_key"])
        self.assertTrue(trace["turn_into_memory"])
        self.assertTrue(trace["not_direction_evidence"])


class Phase1SignalCompletenessRegressionTest(unittest.TestCase):
    def test_pm_rejects_missing_required_analyst_signal(self):
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.BULLISH, confidence=0.7),
            AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.2),
        ]

        with self.assertRaisesRegex(RuntimeError, "analyst signals incomplete"):
            _validate_required_analyst_signals(
                "PB",
                ["technical", "fundamental", "commodity_news"],
                signals,
            )

    def test_workflow_rejects_incomplete_parallel_analyst_signals_before_pm(self):
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.BULLISH, confidence=0.7),
            AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.2),
        ]

        with self.assertRaisesRegex(RuntimeError, "phase1_analyst_signal_set_invalid"):
            AgentWorkflow._validate_phase1_analyst_signals(
                "SR",
                ["technical", "fundamental", "commodity_news"],
                signals,
            )

    def test_commodity_news_counts_as_required_analyst(self):
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.BULLISH, confidence=0.7),
            AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.2),
            AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.2),
        ]

        _validate_required_analyst_signals(
            "SR",
            ["technical", "fundamental", "commodity_news"],
            signals,
        )

    def test_signal_collector_rejects_data_unavailable_state_without_formal_analyst_signals(self):
        with self.assertRaisesRegex(ValueError, "signal_collection_missing_source_contracts"):
            signal_collector_agent({
                "ticker": "ZN",
                "trading_date": datetime(2025, 1, 2),
                "enabled_analysts": ["technical", "fundamental", "commodity_news"],
                "analyst_signals": [],
                "pre_open_reference_price_unavailable": True,
                "pre_open_reference_price_unavailable_reason": (
                    "ZN has no previous close available before 2025-01-02"
                ),
            })

    def test_virtual_phase1_portfolio_uses_final_contract_not_internal_draft(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        portfolio = Portfolio(
            id="p1",
            cashflow=1_000_000,
            positions={"BU": Position(shares=0, value=0)},
        )
        recommendation = FuturesRecommendation(
            config_id="cfg",
            reference_portfolio_id="p1",
            trading_date="2025-03-03",
            source_type=RecommendationSourceType.STRATEGY,
            underlying_code="BU",
            action=RecommendationAction.OPEN_SHORT,
            lots=8,
            base_price=3000.0,
            signal_snapshot={
                "pm_internal_draft": {
                    "target_lots": -8,
                    "reference_price": 2990.0,
                },
                "final_action_contract": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": -1,
                    "lots_delta": -1,
                    "target_position_ratio": -0.01,
                },
            },
        )

        with patch(
            "graph.workflow.FuturesContractInfoCache.get_contract_info",
            return_value={
                "contract_multiplier": 10,
                "margin_rate_long": 0.1,
                "margin_rate_short": 0.1,
            },
        ):
            updated = workflow._project_signed_contract_to_virtual_portfolio(portfolio, recommendation)

        self.assertEqual(updated.positions["BU"].shares, -1)
        self.assertEqual(updated.positions["BU"].entry_price, 3000.0)

    def test_virtual_phase1_portfolio_hard_fails_strategy_without_final_contract(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        portfolio = Portfolio(
            id="p1",
            cashflow=1_000_000,
            positions={"BU": Position(shares=0, value=0)},
        )
        recommendation = FuturesRecommendation(
            config_id="cfg",
            reference_portfolio_id="p1",
            trading_date="2025-03-03",
            source_type=RecommendationSourceType.STRATEGY,
            underlying_code="BU",
            action=RecommendationAction.OPEN_SHORT,
            lots=8,
            base_price=3000.0,
            signal_snapshot={"pm_internal_draft": {"target_lots": -8}},
        )

        with self.assertRaisesRegex(RuntimeError, "missing signed final_action_contract"):
            workflow._project_signed_contract_to_virtual_portfolio(portfolio, recommendation)

    def test_virtual_phase1_portfolio_does_not_infer_lots_from_non_strategy_action(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        portfolio = Portfolio(
            id="p1",
            cashflow=1_000_000,
            positions={"BU": Position(shares=0, value=0)},
        )
        recommendation = FuturesRecommendation(
            config_id="cfg",
            reference_portfolio_id="p1",
            trading_date="2025-03-03",
            source_type=RecommendationSourceType.ROLLOVER,
            underlying_code="BU",
            action=RecommendationAction.OPEN_SHORT,
            lots=8,
            base_price=3000.0,
            signal_snapshot={},
        )

        with patch(
            "graph.workflow.FuturesContractInfoCache.get_contract_info",
            return_value={
                "contract_multiplier": 10,
                "margin_rate_long": 0.1,
                "margin_rate_short": 0.1,
            },
        ):
            updated = workflow._project_signed_contract_to_virtual_portfolio(portfolio, recommendation)

        self.assertEqual(updated.positions["BU"].shares, 0)

    def test_workflow_calls_pm_full_market_finalizer_and_persists_signed_contracts(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        updates = []

        class _DB:
            def save_futures_recommendation(self, recommendation):
                recommendation_id = recommendation.id or f"saved-{len(updates) + 1}"
                updates.append((
                    recommendation_id,
                    recommendation.status,
                    recommendation.signal_snapshot,
                    {"action": recommendation.action, "lots": recommendation.lots},
                ))
                return recommendation_id

            def update_futures_recommendation_status(self, recommendation_id, status, signal_snapshot=None, **kwargs):
                updates.append((recommendation_id, status, signal_snapshot, dict(kwargs)))
                return True

        workflow.db = _DB()
        workflow.config = {
            "max_total_margin_ratio": 0.20,
            "position_budget_policy": {
                "min_real_trade_margin_ratio": 0.008,
                "max_single_ticker_margin_ratio": 0.13,
            },
            "capital_utilization_control": {"target_margin_ratio_confirmed": 0.008},
        }
        workflow.init_portfolio = Portfolio(
            id="p1",
            cashflow=5_000_000,
            positions={},
            margin_used=0.0,
            account_equity=5_000_000,
        )
        rec_low = FuturesRecommendation(
            id="low",
            status=RecommendationStatus.PENDING,
            underlying_code="A",
            base_price=3000.0,
            action=RecommendationAction.OPEN_SHORT,
            lots=1,
            signal_snapshot={
                "opportunity_scorecard": {
                    "preferred_side": "short",
                    "short": {
                        "opportunity_score": 0.82,
                        "capital_priority_score": 0.55,
                        "capital_priority_tier": 1,
                        "opportunity_rank": 1,
                        "final_state": "watch_for_trigger",
                    },
                },
                "final_action_contract": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": -1,
                    "lots_delta": -1,
                    "target_margin_ratio_estimate": 0.008,
                    "evidence_used": {
                        "opportunity_score": 0.82,
                        "capital_priority_score": 0.55,
                        "opportunity_rank": 1,
                    },
                },
            },
        )
        rec_high = FuturesRecommendation(
            id="high",
            status=RecommendationStatus.PENDING,
            underlying_code="B",
            base_price=3000.0,
            action=RecommendationAction.OPEN_SHORT,
            lots=2,
            signal_snapshot={
                "opportunity_scorecard": {
                    "preferred_side": "short",
                    "short": {
                        "opportunity_score": 0.73,
                        "capital_priority_score": 0.91,
                        "capital_priority_tier": 3,
                        "opportunity_rank": 2,
                        "final_state": "tradeable_candidate",
                    },
                },
                "final_action_contract": {
                    "final_action": "open_real",
                    "current_lots": 0,
                    "target_lots": -2,
                    "lots_delta": -2,
                    "target_margin_ratio_estimate": 0.008,
                    "evidence_used": {
                        "opportunity_score": 0.73,
                        "capital_priority_score": 0.91,
                        "opportunity_rank": 2,
                    },
                },
            },
        )

        low_state = _pm_state_from_recommendation_fixture(rec_low)
        open_action_value = {
            "id": "a-short-open",
            "ticker": "A",
            "side": "short",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "breakout_setup",
            "action_name": "open",
            "canonical_action_value": True,
            "canonical_action_family": "open_add_new_risk",
            "consumer_scope": "pm_learning",
            "learning_lane": "open",
            "action_value_lane": "open",
            "memory_side_role": "target_side",
            "action_preference": "positive_candidate_open",
            "canonical_action_value_source": "canonical_action_value",
            "reward_mean": 100.0,
            "reward_sum": 100.0,
            "reward_source": "trade_episode",
            "evidence_scope": "exact_real_state",
            "sample_count": 1,
        }
        frozen_rows, step4_retrieval = _audit_frozen_step4_pm_memory(
            contract=low_state,
            alpha_setup_action_values=[open_action_value],
        )
        step4_retrieval["rejected_or_downgraded"] = [{
            "id": "a-weak-prior",
            "reason": "incomplete_prior_not_pm_scoring_evidence",
            "diagnostic_only": True,
        }]
        low_state["control_diagnostics"].update({
            "final_action_memory_requirements": step4_retrieval["memory_requirements"],
            "final_action_memory_retrieval": step4_retrieval,
        })
        low_state["alpha_setup_action_values"] = frozen_rows
        high_state = _pm_state_from_recommendation_fixture(rec_high)
        signed = dict(workflow._persist_pm_full_market_contracts([
            ("A", low_state),
            ("B", high_state),
        ]))
        rec_low = signed["A"]
        rec_high = signed["B"]

        self.assertNotIn("opportunity_scorecard", rec_high.signal_snapshot)
        self.assertNotIn("opportunity_scorecard", rec_low.signal_snapshot)
        self.assertEqual(rec_high.signal_snapshot["final_action_contract"]["target_lots"], -2)
        self.assertEqual(rec_high.signal_snapshot["final_action_contract"]["lots_delta"], -2)
        self.assertEqual(rec_high.signal_snapshot["final_action_contract"]["final_action"], "open_real")
        self.assertEqual(rec_high.action, RecommendationAction.OPEN_SHORT)
        self.assertEqual(rec_high.lots, 2)
        self.assertTrue(rec_high.signal_snapshot["final_action_contract"]["capital_deployment"]["selected_for_capital_deployment"])
        for snapshot in (rec_high.signal_snapshot, rec_low.signal_snapshot):
            self.assertNotIn("pm_internal_candidate", snapshot)
            self.assertNotIn("pm_internal_candidate_contract", snapshot)
            self.assertNotIn("pm_capital_deployment_decision", snapshot)
        self.assertEqual(
            rec_high.signal_snapshot["final_action_contract"]["capital_deployment"]["rank_capital_role"],
            RANK_CAPITAL_ROLE_REAL_BUDGET,
        )
        self.assertEqual(
            rec_high.signal_snapshot["final_action_contract"]["capital_deployment"]["capital_layer"],
            CAPITAL_LAYER_REAL_BUDGET,
        )
        self.assertEqual(rec_high.signal_snapshot["final_action_contract"]["capital_deployment"]["opportunity_rank"], 1)
        self.assertEqual(
            rec_high.signal_snapshot["final_action_contract"]["capital_deployment"]["rank_semantics_version"],
            "agentquant.capital_priority_rank.v1",
        )
        self.assertEqual(
            rec_high.signal_snapshot["final_action_contract"]["capital_deployment"]["opportunity_rank_meaning"],
            "rank_1_is_current_highest_capital_priority_not_trade_authority",
        )
        self.assertTrue(
            rec_high.signal_snapshot["final_action_contract"]["capital_deployment"]["rank_is_capital_priority"]
        )
        self.assertEqual(rec_low.signal_snapshot["final_action_contract"]["target_lots"], 0)
        self.assertEqual(rec_low.signal_snapshot["final_action_contract"]["lots_delta"], 0)
        self.assertEqual(rec_low.action, RecommendationAction.HOLD)
        self.assertEqual(rec_low.lots, 0)
        self.assertFalse(rec_low.signal_snapshot["final_action_contract"]["capital_deployment"]["selected_for_capital_deployment"])
        self.assertEqual(
            rec_low.signal_snapshot["final_action_contract"]["capital_deployment"]["opportunity_rank"],
            2,
        )
        self.assertEqual(
            rec_low.signal_snapshot["final_action_contract"]["capital_deployment"]["lifecycle_learning_trace"]["rank_lifecycle"],
            "open_add_new_risk",
        )
        self.assertEqual(
            rec_low.signal_snapshot["final_action_contract"]["capital_deployment"]["rank_capital_role"],
            RANK_CAPITAL_ROLE_EXPLORATION,
        )
        self.assertEqual(
            rec_low.signal_snapshot["final_action_contract"]["capital_deployment"]["capital_layer"],
            CAPITAL_LAYER_EXPLORATION,
        )
        self.assertIn(
            "not_selected_by_full_market_pm_capital_queue",
            rec_low.signal_snapshot["final_action_contract"]["capital_deployment"]["capital_allocation_reason"],
        )
        low_learning = rec_low.signal_snapshot["final_action_contract"]["learning_used"]
        low_requirements = low_learning["memory_requirements"]
        low_retrieval = low_learning["memory_retrieval"]
        low_trace = low_learning["pm_lifecycle_learning_trace"]
        self.assertEqual(low_trace["contract_lifecycle_port"], "wait")
        self.assertEqual(low_requirements["action_lifecycle"], "ordinary_hold")
        self.assertEqual(low_requirements["required_memory_lanes"], [])
        self.assertEqual(low_requirements["must_land_in_pm_contract"], [])
        self.assertEqual(low_trace["memory_requirements"], low_requirements)
        self.assertEqual(low_retrieval, step4_retrieval)
        self.assertEqual(low_retrieval["memory_requirements"]["action_lifecycle"], "open")
        self.assertEqual(low_retrieval["status"], "frozen_step4_pool")
        self.assertFalse(low_retrieval["late_retrieval_performed"])
        self.assertEqual(low_retrieval["late_action_value_append_count"], 0)
        self.assertEqual(low_retrieval["lifecycle_matching_row_count"], 1)
        self.assertEqual(low_retrieval["alpha_setup_action_value_count_after_lifecycle"], 1)
        self.assertEqual(low_learning["alpha_setup_action_values"], [])
        self.assertEqual(low_trace["decision_learning_rows"], [])
        self.assertEqual(
            low_learning["pm_lifecycle_learning_impact_delta"]["open_add_rank_score_delta"],
            0.0,
        )
        self.assertEqual(
            [row["id"] for row in low_trace["rejected_learning"]],
            ["a-short-open"],
        )
        self.assertEqual(low_retrieval["rejected_action_values"], [])
        self.assertEqual(
            [row["id"] for row in low_retrieval["rejected_or_downgraded"]],
            ["a-weak-prior"],
        )
        high_learning = rec_high.signal_snapshot["final_action_contract"]["learning_used"]
        self.assertEqual(high_learning["memory_requirements"]["action_lifecycle"], "open")
        self.assertEqual(high_learning["memory_requirements"]["required_memory_lanes"], ["open"])
        self.assertEqual(len(updates), 2)

    def test_pm_step6_signer_requires_single_pm_state_inputs(self):
        with self.assertRaisesRegex(ValueError, "pm_step6_missing_pm_state"):
            _sign_pm_memory_state({})
        with self.assertRaisesRegex(ValueError, "pm_step6_missing_signal_collection_contract"):
            _sign_pm_memory_state({"ticker": "BU", "current_lots": 0, "target_lots": 0})

    def test_pm_step6_new_risk_requires_step5_deployment_decision(self):
        state = _pm_state_fixture(
            {
                "ticker": "BU",
                "current_lots": 0,
                "target_lots": 1,
                "lots_delta": 1,
                "final_action": "open_probe",
                "authority_type": "exploration_probe",
                "reason_codes": ["test_open_candidate"],
            },
            ticker="BU",
        )
        state["recommendation_context"] = {"underlying_code": "BU", "base_price": 3000.0}

        with self.assertRaisesRegex(ValueError, "pm_step6_missing_capital_deployment"):
            _sign_pm_memory_state(state)

    def test_pm_step6_uses_post_gate_candidate_not_stale_primary_lifecycle_rank_trace(self):
        stale_primary_lifecycle_trace = {
            "pm_lifecycle_action_port": "new_risk",
            "requires_full_market_rank": True,
            "current_lots": 0,
            "target_lots": 1,
            "lots_delta": 1,
        }
        state = _pm_state_fixture(
            {
                "ticker": "ZN",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "final_action": "wait",
                "reason_codes": ["risk_gate_flat_target_no_new_exposure"],
            },
            ticker="ZN",
            execution_fields={
                "primary_lifecycle_action_port": stale_primary_lifecycle_trace,
                "lifecycle_transition_diagnostic": {
                    "tool": "pm_lifecycle_action_port",
                    "diagnostic_type": "lifecycle_transition_diagnostic",
                    "primary_lifecycle_action_port": "new_risk",
                    "expected_contract_lifecycle_port": "open_add_new_risk",
                    "actual_contract_lifecycle_port": "wait",
                    "consistent": False,
                    "ok": False,
                    "transition_reason": "unexplained_lifecycle_port_transition",
                },
                "risk_gate_note": "RiskGate received flat target; no new exposure required",
            },
        )
        state["recommendation_context"] = {"underlying_code": "ZN", "base_price": 24200.0}

        recommendation = finalize_pm_full_market_contracts(
            generated=[("ZN", state)],
            config={"max_total_margin_ratio": 0.20},
            portfolio=Portfolio(id="p1", cashflow=5_000_000.0, positions={}, account_equity=5_000_000.0),
        )[0][1]

        snapshot = recommendation.signal_snapshot
        contract = snapshot["final_action_contract"]
        deployment = contract["capital_deployment"]
        self.assertEqual(contract["final_action"], "wait")
        self.assertEqual(contract["target_lots"], 0)
        self.assertEqual(contract["lots_delta"], 0)
        self.assertEqual(deployment["capital_allocation_reason"], "non_new_risk_no_capital_rank")
        self.assertFalse(deployment["selected_for_capital_deployment"])
        generation_check = snapshot["pm_six_step_trace"]["step6_contract_generation_check"]
        self.assertTrue(generation_check["ok"])
        self.assertNotIn("contract_lifecycle_self_check", contract["evidence_used"])
        self.assertNotIn("lifecycle_transition_diagnostic", contract["evidence_used"])
        self.assertNotIn("historical_lifecycle_transition_diagnostic", contract["evidence_used"])
        self.assertNotIn("primary_lifecycle_action_port", contract["evidence_used"])
        self.assertNotIn("initial_primary_lifecycle_action_port", contract["evidence_used"])
        self.assertEqual(
            set(snapshot["pm_six_step_trace"]),
            {"step6_contract_generation_check", "pm_contract_self_check"},
        )
        self.assertTrue(snapshot["pm_six_step_trace"]["pm_contract_self_check"]["ok"])
        self.assertNotIn("pm_capital_deployment_decision", snapshot)
        self.assertNotIn("pm_internal_candidate", snapshot)
        self.assertNotIn("opportunity_rank", contract)
        self.assertNotIn("opportunity_rank", contract["evidence_used"])
        self.assertNotIn("opportunity_rank", deployment)
        self.assertNotIn("rank_input_components", contract["evidence_used"])

    def test_pm_step6_ignores_stale_lifecycle_transition_reason_when_final_candidate_is_flat(self):
        stale_primary_lifecycle_trace = {
            "pm_lifecycle_action_port": "new_risk",
            "requires_full_market_rank": True,
            "current_lots": 0,
            "target_lots": 1,
            "lots_delta": 1,
        }
        state = _pm_state_fixture(
            {
                "ticker": "ZN",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "final_action": "wait",
                "reason_codes": [],
            },
            ticker="ZN",
            execution_fields={
                "primary_lifecycle_action_port": stale_primary_lifecycle_trace,
                "lifecycle_transition_diagnostic": {
                    "tool": "pm_lifecycle_action_port",
                    "diagnostic_type": "lifecycle_transition_diagnostic",
                    "primary_lifecycle_action_port": "new_risk",
                    "expected_contract_lifecycle_port": "open_add_new_risk",
                    "actual_contract_lifecycle_port": "wait",
                    "consistent": False,
                    "ok": False,
                    "transition_reason": "unexplained_lifecycle_port_transition",
                },
            },
        )
        state["recommendation_context"] = {"underlying_code": "ZN", "base_price": 24200.0}

        recommendation = finalize_pm_full_market_contracts(
            generated=[("ZN", state)],
            config={"max_total_margin_ratio": 0.20},
            portfolio=Portfolio(id="p1", cashflow=5_000_000.0, positions={}, account_equity=5_000_000.0),
        )[0][1]

        snapshot = recommendation.signal_snapshot
        contract = snapshot["final_action_contract"]
        self.assertEqual(contract["final_action"], "wait")
        self.assertEqual(contract["target_lots"], 0)
        self.assertEqual(contract["lots_delta"], 0)
        self.assertNotIn("contract_lifecycle_self_check", contract["evidence_used"])
        self.assertNotIn("lifecycle_transition_diagnostic", contract["evidence_used"])
        self.assertTrue(snapshot["pm_six_step_trace"]["step6_contract_generation_check"]["ok"])

    def test_pm_finalizer_requires_every_generated_candidate_to_sign(self):
        recommendation = FuturesRecommendation(
            underlying_code="BU",
            base_price=3000.0,
            signal_snapshot={"opportunity_scorecard": {"preferred_side": "long"}},
        )

        with self.assertRaisesRegex(ValueError, "pm_step6_missing_signal_collection_contract"):
            finalize_pm_full_market_contracts(
                generated=[("BU", {"ticker": "BU", "current_lots": 0, "target_lots": 0})],
                config={"max_total_margin_ratio": 0.20},
                portfolio=Portfolio(id="p1", cashflow=1_000_000.0, positions={}, account_equity=1_000_000.0),
            )

    def test_pm_finalizer_hard_fails_if_signer_returns_false(self):
        state = _pm_state_fixture(
            {
                "ticker": "BU",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "final_action": "wait",
                "reason_codes": ["test_wait_candidate"],
            },
            ticker="BU",
        )
        state["recommendation_context"] = {"underlying_code": "BU", "base_price": 3000.0}
        with patch("agents.decision_team.portfolio_manager._sign_pm_memory_state", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "pm_step6_signer_did_not_create_recommendation:BU"):
                finalize_pm_full_market_contracts(
                    generated=[("BU", state)],
                    config={"max_total_margin_ratio": 0.20},
                    portfolio=Portfolio(id="p1", cashflow=1_000_000.0, positions={}, account_equity=1_000_000.0),
                )

    def test_watch_rank_one_keeps_probe_capital_layer_and_probe_ratio_source(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        updates = []

        class _DB:
            def save_futures_recommendation(self, recommendation):
                recommendation_id = recommendation.id or f"saved-{len(updates) + 1}"
                updates.append((
                    recommendation_id,
                    recommendation.status,
                    recommendation.signal_snapshot,
                    {"action": recommendation.action, "lots": recommendation.lots},
                ))
                return recommendation_id

            def update_futures_recommendation_status(self, recommendation_id, status, signal_snapshot=None, **kwargs):
                updates.append((recommendation_id, status, signal_snapshot, dict(kwargs)))
                return True

        workflow.db = _DB()
        workflow.config = {
            "max_total_margin_ratio": 0.20,
            "position_budget_policy": {
                "min_real_trade_margin_ratio": 0.008,
                "max_single_ticker_margin_ratio": 0.13,
            },
            "capital_utilization_control": {"target_margin_ratio_confirmed": 0.008},
        }
        workflow.init_portfolio = Portfolio(
            id="p1",
            cashflow=5_000_000,
            positions={},
            margin_used=0.0,
            account_equity=5_000_000,
        )
        rec_best_watch = FuturesRecommendation(
            id="best-watch",
            status=RecommendationStatus.PENDING,
            underlying_code="P",
            base_price=7000.0,
            action=RecommendationAction.OPEN_LONG,
            lots=1,
            signal_snapshot={
                "opportunity_scorecard": {
                    "preferred_side": "long",
                    "long": {
                        "opportunity_score": 0.52,
                        "capital_priority_score": 0.42,
                        "capital_priority_tier": 1,
                        "final_state": "watch_for_trigger",
                        "entry_trigger": {"rule": "breakout_confirm"},
                        "invalidation": {"rule": "close_back_below_range"},
                    },
                },
                "final_action_contract": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": 1,
                    "lots_delta": 1,
                    "target_margin_ratio_estimate": 0.008,
                    "evidence_used": {"opportunity_score": 0.52, "capital_priority_score": 0.42},
                },
            },
        )
        rec_other_watch = FuturesRecommendation(
            id="other-watch",
            status=RecommendationStatus.PENDING,
            underlying_code="M",
            base_price=3500.0,
            action=RecommendationAction.OPEN_LONG,
            lots=1,
            signal_snapshot={
                "opportunity_scorecard": {
                    "preferred_side": "long",
                    "long": {
                        "opportunity_score": 0.49,
                        "capital_priority_score": 0.35,
                        "capital_priority_tier": 1,
                        "final_state": "watch_for_trigger",
                    },
                },
                "final_action_contract": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": 1,
                    "lots_delta": 1,
                    "target_margin_ratio_estimate": 0.008,
                    "evidence_used": {"opportunity_score": 0.49, "capital_priority_score": 0.35},
                },
            },
        )

        signed = _persist_pm_state_fixtures(workflow, [("P", rec_best_watch), ("M", rec_other_watch)])
        rec_best_watch = signed["P"]
        rec_other_watch = signed["M"]

        contract = rec_best_watch.signal_snapshot["final_action_contract"]
        deployment = contract["capital_deployment"]
        self.assertEqual(contract["capital_deployment"]["opportunity_rank"], 1)
        self.assertEqual(deployment["rank_capital_role"], RANK_CAPITAL_ROLE_EXPLORATION)
        self.assertEqual(deployment["capital_layer"], CAPITAL_LAYER_EXPLORATION)
        self.assertEqual(deployment["capital_ratio_source"], CAPITAL_RATIO_SOURCE_EXPLORATION)
        self.assertEqual(contract["target_margin_ratio_estimate"], 0.008)
        self.assertNotEqual(deployment["capital_layer"], "real_budget_entry")
        self.assertEqual(rec_best_watch.action, RecommendationAction.OPEN_LONG)
        self.assertEqual(rec_best_watch.lots, 1)
        self.assertEqual(len(updates), 2)

    def test_full_market_rank_consumes_net_exposure_budget_in_rank_order(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        updates = []

        class _DB:
            def save_futures_recommendation(self, recommendation):
                recommendation_id = recommendation.id or f"saved-{len(updates) + 1}"
                updates.append((
                    recommendation_id,
                    recommendation.status,
                    recommendation.signal_snapshot,
                    {"action": recommendation.action, "lots": recommendation.lots},
                ))
                return recommendation_id

            def update_futures_recommendation_status(self, recommendation_id, status, signal_snapshot=None, **kwargs):
                updates.append((recommendation_id, status, signal_snapshot, dict(kwargs)))
                return True

        workflow.db = _DB()
        workflow.config = {
            "max_total_margin_ratio": 0.20,
            "position_budget_policy": {
                "min_real_trade_margin_ratio": 0.008,
                "max_single_ticker_margin_ratio": 0.13,
            },
            "capital_utilization_control": {"target_margin_ratio_confirmed": 0.03},
            "net_exposure_control": {"max_net_exposure": 0.01},
        }
        workflow.init_portfolio = Portfolio(
            id="p1",
            cashflow=5_000_000,
            positions={},
            margin_used=0.0,
            account_equity=5_000_000,
        )

        def _watch_rec(rec_id, ticker, rank_score):
            return FuturesRecommendation(
                id=rec_id,
                status=RecommendationStatus.PENDING,
                underlying_code=ticker,
                base_price=3000.0,
                action=RecommendationAction.OPEN_LONG,
                lots=1,
                signal_snapshot={
                    "opportunity_scorecard": {
                        "preferred_side": "long",
                        "long": {
                            "opportunity_score": rank_score,
                            "capital_priority_score": rank_score,
                            "rank_score": rank_score,
                            "rank_score_components": {
                                "cold_start_evidence_quality": rank_score,
                                "open_add_action_value_delta": 0.0,
                            },
                            "capital_priority_tier": 1,
                            "final_state": "watch_for_trigger",
                        },
                    },
                    "final_action_contract": {
                        "final_action": "open_probe",
                        "current_lots": 0,
                        "target_lots": 1,
                        "lots_delta": 1,
                        "target_margin_ratio_estimate": 0.008,
                        "target_position_ratio": 0.04,
                        "evidence_used": {
                            "opportunity_score": rank_score,
                            "capital_priority_score": rank_score,
                            "rank_score": rank_score,
                        },
                    },
                },
            )

        rec_rank_1 = _watch_rec("rank-1", "P", 0.82)
        rec_rank_2 = _watch_rec("rank-2", "M", 0.74)

        signed = _persist_pm_state_fixtures(workflow, [("P", rec_rank_1), ("M", rec_rank_2)])
        rec_rank_1 = signed["P"]
        rec_rank_2 = signed["M"]

        contract_1 = rec_rank_1.signal_snapshot["final_action_contract"]
        contract_2 = rec_rank_2.signal_snapshot["final_action_contract"]
        deployment_1 = contract_1["capital_deployment"]
        deployment_2 = contract_2["capital_deployment"]
        components_1 = contract_1["capital_deployment"]["rank_input_components"]["rank_score_components"]

        self.assertEqual(contract_1["capital_deployment"]["opportunity_rank"], 1)
        self.assertTrue(deployment_1["selected_for_capital_deployment"])
        self.assertEqual(contract_1["target_lots"], 1)
        self.assertEqual(contract_1["target_margin_ratio_estimate"], 0.008)
        self.assertEqual(deployment_1["rank_budget_sequence"], 1)
        self.assertTrue(deployment_1["net_exposure_budget_ok"])
        self.assertGreater(components_1["capital_efficiency"], 0.0)

        self.assertEqual(contract_2["capital_deployment"]["opportunity_rank"], 2)
        self.assertFalse(deployment_2["selected_for_capital_deployment"])
        self.assertEqual(contract_2["target_lots"], 0)
        self.assertEqual(contract_2["lots_delta"], 0)
        self.assertEqual(contract_2["final_action"], "wait")
        self.assertEqual(deployment_2["rank_budget_sequence"], 2)
        self.assertFalse(deployment_2["net_exposure_budget_ok"])
        self.assertIn("no_rank_or_budget_no_new_exposure", contract_2["reason_codes"])
        self.assertIn(
            "not_selected_by_full_market_pm_capital_queue",
            contract_2["capital_deployment"]["capital_allocation_reason"],
        )

    def test_fifteen_watch_candidates_receive_unique_full_market_rank_one_to_n(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        updates = []

        class _DB:
            def save_futures_recommendation(self, recommendation):
                recommendation_id = recommendation.id or f"saved-{len(updates) + 1}"
                updates.append((
                    recommendation_id,
                    recommendation.status,
                    recommendation.signal_snapshot,
                    {"action": recommendation.action, "lots": recommendation.lots},
                ))
                return recommendation_id

            def update_futures_recommendation_status(self, recommendation_id, status, signal_snapshot=None, **kwargs):
                updates.append((recommendation_id, status, signal_snapshot, dict(kwargs)))
                return True

        workflow.db = _DB()
        workflow.config = {
            "max_total_margin_ratio": 0.20,
            "position_budget_policy": {
                "min_real_trade_margin_ratio": 0.008,
                "max_single_ticker_margin_ratio": 0.13,
            },
            "capital_utilization_control": {"target_margin_ratio_confirmed": 0.20},
            "net_exposure_control": {"max_net_exposure": 0.50},
        }
        workflow.init_portfolio = Portfolio(
            id="p1",
            cashflow=5_000_000,
            positions={},
            margin_used=0.0,
            account_equity=5_000_000,
        )

        tickers = ["BU", "C", "CF", "EB", "HC", "I", "J", "M", "MA", "P", "PB", "RB", "SR", "TA", "ZN"]
        recommendations = []
        for index, ticker in enumerate(tickers):
            rank_score = round(0.90 - index * 0.02, 4)
            recommendations.append(
                (
                    ticker,
                    FuturesRecommendation(
                        id=f"watch-{ticker}",
                        status=RecommendationStatus.PENDING,
                        underlying_code=ticker,
                        base_price=3000.0,
                        action=RecommendationAction.OPEN_LONG,
                        lots=1,
                        signal_snapshot={
                            "opportunity_scorecard": {
                                "preferred_side": "long",
                                "long": {
                                    "opportunity_score": rank_score,
                                    "capital_priority_score": rank_score,
                                    "rank_score": rank_score,
                                    "rank_score_components": {
                                        "cold_start_evidence_quality": rank_score,
                                        "open_add_action_value_delta": 0.0,
                                    },
                                    "capital_priority_tier": 1,
                                    "final_state": "watch_for_trigger",
                                },
                            },
                            "final_action_contract": {
                                "final_action": "open_probe",
                                "current_lots": 0,
                                "target_lots": 1,
                                "lots_delta": 1,
                                "target_margin_ratio_estimate": 0.008,
                                "target_position_ratio": 0.01,
                                "evidence_used": {
                                    "opportunity_score": rank_score,
                                    "capital_priority_score": rank_score,
                                    "rank_score": rank_score,
                                },
                            },
                        },
                    ),
                )
            )

        signed = _persist_pm_state_fixtures(workflow, recommendations)
        recommendations = [(ticker, signed[ticker]) for ticker, _ in recommendations]

        observed = []
        for ticker, recommendation in recommendations:
            contract = recommendation.signal_snapshot["final_action_contract"]
            deployment = contract["capital_deployment"]
            components = contract["capital_deployment"]["rank_input_components"]["rank_score_components"]
            observed.append(contract["capital_deployment"]["opportunity_rank"])
            self.assertEqual(contract["target_margin_ratio_estimate"], 0.008)
            self.assertEqual(deployment["capital_layer"], CAPITAL_LAYER_EXPLORATION)
            self.assertEqual(deployment["rank_capital_role"], RANK_CAPITAL_ROLE_EXPLORATION)
            self.assertEqual(deployment["rank_budget_sequence"], contract["capital_deployment"]["opportunity_rank"])
            self.assertGreater(components["capital_efficiency"], 0.0)

        self.assertEqual(sorted(observed), list(range(1, 16)))
        self.assertEqual(recommendations[0][1].signal_snapshot["final_action_contract"]["capital_deployment"]["opportunity_rank"], 1)
        self.assertEqual(recommendations[-1][1].signal_snapshot["final_action_contract"]["capital_deployment"]["opportunity_rank"], 15)
        self.assertEqual(len(updates), 15)

    def test_workflow_canonicalizes_flat_skipped_contract_to_wait_before_persistence(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        updates = []

        class _DB:
            def save_futures_recommendation(self, recommendation):
                recommendation_id = recommendation.id or f"saved-{len(updates) + 1}"
                updates.append((
                    recommendation_id,
                    recommendation.status,
                    recommendation.signal_snapshot,
                    {"action": recommendation.action, "lots": recommendation.lots},
                ))
                return recommendation_id

            def update_futures_recommendation_status(self, recommendation_id, status, signal_snapshot=None, **kwargs):
                updates.append((recommendation_id, status, signal_snapshot, dict(kwargs)))
                return True

        workflow.db = _DB()
        workflow.config = {
            "max_total_margin_ratio": 0.20,
            "position_budget_policy": {"min_real_trade_margin_ratio": 0.008},
            "capital_utilization_control": {"target_margin_ratio_confirmed": 0.008},
        }
        workflow.init_portfolio = Portfolio(
            id="p1",
            cashflow=5_000_000,
            positions={},
            margin_used=0.0,
            account_equity=5_000_000,
        )
        recommendation = FuturesRecommendation(
            id="flat-skipped",
            status=RecommendationStatus.SKIPPED,
            underlying_code="J",
            base_price=1700.0,
            action=RecommendationAction.HOLD,
            lots=0,
            signal_snapshot={
                "opportunity_scorecard": {"preferred_side": "short", "short": {"final_state": "wait"}},
                "final_action_contract": {
                    "final_action": "hold",
                    "current_lots": 0,
                    "target_lots": 0,
                    "lots_delta": 0,
                },
            },
        )

        recommendation = _persist_pm_state_fixtures(workflow, [("J", recommendation)])["J"]

        contract = recommendation.signal_snapshot["final_action_contract"]
        self.assertEqual(contract["final_action"], "wait")
        self.assertEqual(recommendation.action, RecommendationAction.HOLD)
        self.assertEqual(recommendation.lots, 0)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][2]["final_action_contract"]["final_action"], "wait")

    def test_workflow_has_no_atomic_rank_or_contract_repair_fallback(self):
        source = (SRC_ROOT / "graph" / "workflow.py").read_text(encoding="utf-8")

        self.assertNotIn("_build_missing_pre_open_reference_signals", source)
        self.assertNotIn("_build_signal_snapshot_from_signals", source)
        self.assertNotIn("AnalystSignal", source)
        self.assertNotIn("signal_snapshot =", source)
        self.assertNotIn("pm_full_market_capital_deployment", source)
        self.assertNotIn("_ensure_atomic_capital_deployment_submission", source)
        self.assertNotIn("_apply_daily_capital_deployment", source)
        self.assertNotIn("_apply_deployed_target_to_snapshot", source)
        self.assertNotIn("_apply_virtual_recommendation_to_portfolio", source)
        self.assertNotIn("_clear_non_full_market_rank_fields", source)
        self.assertNotIn("_write_daily_opportunity_ranks", source)
        self.assertNotIn("opportunity_rank", source)
        self.assertNotIn("capital_deployment =", source)
        self.assertNotIn('["capital_deployment"] =', source)
        self.assertNotIn("fallback", source.lower())
        self.assertNotIn("atomic", source.lower())
        self.assertNotIn("canonicalize", source.lower())
        self.assertNotIn("repair", source.lower())
        self.assertIn("_project_signed_contract_to_virtual_portfolio", source)

    def test_workflow_learning_rank_changes_final_contract_or_explains_no_effect(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        updates = []

        class _DB:
            def save_futures_recommendation(self, recommendation):
                recommendation_id = recommendation.id or f"saved-{len(updates) + 1}"
                updates.append((
                    recommendation_id,
                    recommendation.status,
                    recommendation.signal_snapshot,
                    {"action": recommendation.action, "lots": recommendation.lots},
                ))
                return recommendation_id

            def update_futures_recommendation_status(self, recommendation_id, status, signal_snapshot=None, **kwargs):
                updates.append((recommendation_id, status, signal_snapshot, dict(kwargs)))
                return True

        workflow.db = _DB()
        workflow.config = {
            "max_total_margin_ratio": 0.20,
            "position_budget_policy": {
                "min_real_trade_margin_ratio": 0.008,
                "max_single_ticker_margin_ratio": 0.13,
            },
            "capital_utilization_control": {"target_margin_ratio_confirmed": 0.008},
        }
        workflow.init_portfolio = Portfolio(
            id="p1",
            cashflow=1_000_000,
            positions={},
            margin_used=0.0,
            account_equity=1_000_000,
        )
        rec_positive = FuturesRecommendation(
            id="positive-learning",
            status=RecommendationStatus.PENDING,
            underlying_code="EB",
            base_price=3000.0,
            action=RecommendationAction.OPEN_LONG,
            lots=2,
            signal_snapshot={
                "opportunity_scorecard": {
                    "preferred_side": "long",
                    "long": {
                        "opportunity_score": 0.66,
                        "opportunity_score_components": {
                            "positive_learning": 0.14,
                            "negative_learning": 0.0,
                            "execution_profile_learning": 0.04,
                            "recent_tail_loss_penalty": 0.0,
                        },
                    },
                },
                "final_action_contract": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": 2,
                    "lots_delta": 2,
                    "target_margin_ratio_estimate": 0.008,
                    "evidence_used": {
                        "opportunity_score": 0.66,
                        "opportunity_score_components": {
                            "positive_learning": 0.14,
                            "negative_learning": 0.0,
                            "execution_profile_learning": 0.04,
                            "recent_tail_loss_penalty": 0.0,
                        },
                    },
                    "learning_used": {
                        "alpha_setup_action_values": [
                            {
                                "action_preference": "positive_candidate_open",
                                "reward_sum": 4200.0,
                                "reward_mean": 1400.0,
                                "win_rate": 0.67,
                                "reward_source": "trade_episode",
                                "evidence_scope": "exact_real_state",
                                "action_value_lane": "open",
                            }
                        ]
                    },
                },
            },
        )
        rec_negative = FuturesRecommendation(
            id="negative-learning",
            status=RecommendationStatus.PENDING,
            underlying_code="TA",
            base_price=3000.0,
            action=RecommendationAction.OPEN_SHORT,
            lots=2,
            signal_snapshot={
                "opportunity_scorecard": {
                    "preferred_side": "short",
                    "short": {
                        "opportunity_score": 0.48,
                        "opportunity_score_components": {
                            "positive_learning": 0.0,
                            "negative_learning": -0.15,
                            "execution_profile_learning": 0.0,
                            "recent_tail_loss_penalty": -0.08,
                        },
                    },
                },
                "final_action_contract": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": -2,
                    "lots_delta": -2,
                    "target_margin_ratio_estimate": 0.008,
                    "evidence_used": {
                        "opportunity_score": 0.48,
                        "opportunity_score_components": {
                            "positive_learning": 0.0,
                            "negative_learning": -0.15,
                            "execution_profile_learning": 0.0,
                            "recent_tail_loss_penalty": -0.08,
                        },
                    },
                    "learning_used": {
                        "alpha_setup_action_values": [
                            {
                                "action_preference": "tail_loss_protect",
                                "reward_sum": -3600.0,
                                "reward_mean": -1200.0,
                                "win_rate": 0.0,
                                "reward_source": "trade_episode",
                                "evidence_scope": "exact_real_state",
                                "action_value_lane": "open",
                            }
                        ]
                    },
                },
            },
        )

        signed = _persist_pm_state_fixtures(workflow, [("EB", rec_positive), ("TA", rec_negative)])
        rec_positive = signed["EB"]
        rec_negative = signed["TA"]

        positive_contract = rec_positive.signal_snapshot["final_action_contract"]
        negative_contract = rec_negative.signal_snapshot["final_action_contract"]
        self.assertEqual(positive_contract["capital_deployment"]["opportunity_rank"], 1)
        self.assertEqual(negative_contract["capital_deployment"]["opportunity_rank"], 2)
        self.assertEqual(positive_contract["target_lots"], 2)
        self.assertEqual(positive_contract["lots_delta"], 2)
        self.assertEqual(rec_positive.action, RecommendationAction.OPEN_LONG)
        self.assertEqual(negative_contract["target_lots"], 0)
        self.assertEqual(negative_contract["lots_delta"], 0)
        self.assertEqual(rec_negative.action, RecommendationAction.HOLD)
        self.assertIn(
            "not_selected_by_full_market_pm_capital_queue",
            negative_contract["capital_deployment"]["capital_allocation_reason"],
        )
        self.assertEqual(len(updates), 2)

    def test_unranked_new_risk_requires_pm_full_market_rank_gate(self):
        from tools.common.final_action_semantics import full_market_rank_gate_errors

        contract = {
            "final_action": "open_probe",
            "current_lots": 0,
            "target_lots": -11,
            "lots_delta": -11,
            "target_margin_ratio_estimate": 0.008,
            "conditional_trigger_authority": True,
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "evidence_used": {"opportunity_score": 0.0},
            "reason_codes": ["conditional_monitor_probe_seed"],
        }

        self.assertEqual(
            full_market_rank_gate_errors(contract),
            ["new_risk_exposure_missing_full_market_rank"],
        )
    def test_zero_score_watch_open_probe_enters_full_market_rank_queue(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        updates = []

        class _DB:
            def save_futures_recommendation(self, recommendation):
                recommendation_id = recommendation.id or f"saved-{len(updates) + 1}"
                updates.append((
                    recommendation_id,
                    recommendation.status,
                    recommendation.signal_snapshot,
                    {"action": recommendation.action, "lots": recommendation.lots},
                ))
                return recommendation_id

            def update_futures_recommendation_status(self, recommendation_id, status, signal_snapshot=None, **kwargs):
                updates.append((recommendation_id, status, signal_snapshot, dict(kwargs)))
                return True

        workflow.db = _DB()
        workflow.config = {
            "max_total_margin_ratio": 0.20,
            "position_budget_policy": {
                "min_real_trade_margin_ratio": 0.008,
                "max_single_ticker_margin_ratio": 0.13,
            },
            "capital_utilization_control": {"target_margin_ratio_confirmed": 0.008},
        }
        workflow.init_portfolio = Portfolio(
            id="p1",
            cashflow=5_000_000,
            positions={},
            margin_used=0.0,
            account_equity=5_000_000,
        )
        recommendation = FuturesRecommendation(
            id="zero-score-watch",
            status=RecommendationStatus.PENDING,
            underlying_code="HC",
            base_price=3200.0,
            action=RecommendationAction.OPEN_SHORT,
            lots=4,
            signal_snapshot={
                "opportunity_scorecard": {
                    "preferred_side": "short",
                    "short": {
                        "opportunity_score": 0.0,
                        "capital_priority_score": 0.0,
                        "capital_priority_tier": 1,
                        "final_state": "watch_for_trigger",
                    },
                },
                "final_action_contract": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": -4,
                    "lots_delta": -4,
                    "target_margin_ratio_estimate": 0.008,
                    "evidence_used": {"opportunity_score": 0.0, "capital_priority_score": 0.0},
                    "reason_codes": [
                        "conditional_trigger_authority",
                        "exploration_probe_probe_floor_applied",
                    ],
                },
            },
        )

        recommendation = _persist_pm_state_fixtures(workflow, [("HC", recommendation)])["HC"]

        contract = recommendation.signal_snapshot["final_action_contract"]
        deployment = contract["capital_deployment"]
        self.assertEqual(contract["capital_deployment"]["opportunity_rank"], 1)
        self.assertEqual(deployment["opportunity_rank"], 1)
        self.assertEqual(deployment["rank_capital_role"], RANK_CAPITAL_ROLE_EXPLORATION)
        self.assertEqual(deployment["capital_layer"], CAPITAL_LAYER_EXPLORATION)
        self.assertEqual(deployment["capital_ratio_source"], CAPITAL_RATIO_SOURCE_EXPLORATION)
        self.assertEqual(contract["target_lots"], -4)
        self.assertEqual(contract["lots_delta"], -4)
        self.assertEqual(recommendation.action, RecommendationAction.OPEN_SHORT)
        self.assertEqual(recommendation.lots, 4)
        self.assertEqual(len(updates), 1)


class ResearchLearningMechanismRegressionTest(unittest.TestCase):
    def test_learning_mechanisms_use_final_action_contract_trace(self):
        recommendation = {
            "signal_snapshot": json.dumps(
                {
                    "final_action_contract": {
                        "reason_codes": ["adaptive_policy_cap"],
                        "learning_used": {
                            "exploratory_learning_context": {
                                "memory_trace": {
                                    "selected_memory_refs": [
                                        {
                                            "policy_type": "loss_template_policy",
                                            "policy_action": "cap",
                                            "position_authority": "risk_reduction_conditioned",
                                        }
                                    ]
                                }
                            },
                            "adaptive_policy_state": {
                                "policies": [
                                    {
                                        "policy_type": "learning_mechanism:technical_parameter_calibration",
                                        "policy_action": "protect",
                                    },
                                    {
                                        "policy_type": "tail_loss_sentinel",
                                        "policy_action": "cap",
                                    },
                                ]
                            },
                        },
                    },
                    "technical": {
                        "metadata": {
                            "technical_parameter_calibration": {"applied": [{"id": "cal-1"}]}
                        }
                    },
                }
            )
        }
        mechanisms = set(_learning_mechanisms_from_recommendation(recommendation))
        self.assertIn("loss_template_policy", mechanisms)
        self.assertIn("technical_parameter_calibration", mechanisms)
        self.assertIn("tail_loss_sentinel", mechanisms)

    def test_learning_mechanisms_ignore_pm_internal_draft_trace(self):
        recommendation = {
            "signal_snapshot": json.dumps(
                {
                    "pm_internal_draft": {
                        "strategy_controls": {
                            "reasons": ["adaptive_policy_cap"],
                            "diagnostics": {
                                "exploratory_learning_context": {
                                    "memory_trace": {
                                        "selected_memory_refs": [
                                            {"policy_type": "loss_template_policy"}
                                        ]
                                    }
                                }
                            },
                        },
                    },
                    "final_action_contract": {
                        "reason_codes": [],
                        "learning_used": {},
                    },
                }
            )
        }

        mechanisms = set(_learning_mechanisms_from_recommendation(recommendation))

        self.assertNotIn("loss_template_policy", mechanisms)

    def test_loss_failure_family_is_scope_context_not_product_rule(self):
        data_usage = {
            "analysts": {
                "commodity_news": {
                    "sources": {
                        "finoview_news_txt": {
                            "available": True,
                            "used_in_signal": True,
                            "info_cutoff": "pre_open",
                        }
                    }
                }
            }
        }
        data_combo = _data_combo_key(data_usage)
        family = _loss_failure_family(
            "long_news_event_probe_event_short",
            "event_short",
            "range",
            data_combo,
        )
        actions = _failure_family_actions(family)
        joined = " ".join(actions["analysis"] + actions["trading"]).lower()
        self.assertEqual(family, "news_event_probe_failure")
        self.assertIn("catalyst", joined)
        self.assertNotIn("blacklist", joined)


class AnalystStrategyQualityRegressionTest(unittest.TestCase):
    def test_formal_entry_episode_without_notional_return_is_analyst_unusable(self):
        for action_preference in (
            "positive_candidate_open",
            "negative_revalidate",
        ):
            with self.subTest(action_preference=action_preference):
                calibration = _signal_calibration_contract(
                    action_name="open",
                    action_preference=action_preference,
                    amplification_scope_quality="exact_real_state",
                    reward_source="trade_episode",
                    mean_return_on_notional=None,
                )

                self.assertEqual(
                    calibration["calibration_bias"],
                    "neutral_evidence_context",
                )
                self.assertEqual(
                    calibration["learning_economics_basis"],
                    "after_fee_return_on_notional",
                )
                self.assertEqual(calibration["usable_by"], [])
                self.assertEqual(calibration["allowed_effects"], [])
                self.assertEqual(
                    calibration["unusable_reason"],
                    "missing_return_on_notional",
                )
                reward_sign = 1.0 if action_preference.startswith("positive_") else -1.0
                row = {
                    "id": f"missing-ron-{action_preference}",
                    "scope_key": "RB|long|short|trend|trend_breakout_setup|technical",
                    "ticker": "RB",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "canonical_action_family": "open_add_new_risk",
                    "action_value_lane": "open",
                    "learning_lane": "open",
                    "consumer_scope": "pm_learning",
                    "memory_side_role": "target_side",
                    "canonical_action_value": True,
                    "sample_count": 1,
                    "reward_sum": 1000.0 * reward_sign,
                    "reward_mean": 1000.0 * reward_sign,
                    "win_rate": 1.0 if reward_sign > 0 else 0.0,
                    "confidence_score": 0.60,
                    "action_preference": action_preference,
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                    "last_sample_date": "2025-03-14",
                    "valid_until": "2025-04-14",
                    "payload": {
                        "research_output_contract_version": "agentquant.research_action_value.v1",
                        "canonical_action_family": "open_add_new_risk",
                        "action_value_lane": "open",
                        "learning_lane": "open",
                        "consumer_scope": "pm_learning",
                        "memory_side_role": "target_side",
                        "action_preference": action_preference,
                        "reward_source": "trade_episode",
                        "amplification_scope_quality": "exact_real_state",
                        "mean_return_on_notional": None,
                        "signal_calibration": calibration,
                    },
                }
                self.assertIsNone(
                    _safe_analyst_action_value_projection(
                        row,
                        analyst="technical",
                        ticker="RB",
                        trading_date="2025-03-17",
                    )
                )

    def test_formal_entry_episode_without_notional_return_keeps_confirmation_neutral(self):
        for episode_net_pnl in (1000.0, -1000.0):
            with self.subTest(episode_net_pnl=episode_net_pnl):
                outcome = _entry_quality_outcome_from_sample(
                    sample={"net_pnl": episode_net_pnl, "commission": 0.0},
                    result={"episode_net_pnl": episode_net_pnl},
                    action_name="open",
                    entry_trigger="breakout",
                    evidence_combo="technical",
                    deployment={"deployment_tier": "capital_deployed"},
                )

                self.assertEqual(
                    outcome["entry_quality_verdict"],
                    "entry_outcome_neutral",
                )
                self.assertEqual(
                    outcome["trigger_confirmation_adjustment"],
                    "neutral",
                )
                self.assertFalse(outcome["loss_episode"])
                self.assertFalse(outcome["tail_loss_episode"])
                self.assertFalse(outcome["positive_entry_episode"])
                self.assertIsNone(outcome["return_on_notional"])

    def test_news_direction_without_complete_setup_is_no_opportunity(self):
        news_item = SimpleNamespace(
            title="inventory edges lower",
            content="spot trading remains average and the market is waiting for further confirmation",
            publish_time="2025-01-02 08:30:00",
        )
        context = summarize_news_events([news_item], "BU")
        signal = AnalystSignal(
            agent_name="commodity_news",
            signal=Signal.BULLISH,
            confidence=0.70,
            horizon_class="event_short",
            business_quality_score=0.70,
            entry_trigger="news_event_trigger",
        )

        signal = apply_trade_research_contract(signal, context, analyst="commodity_news", ticker="BU")

        self.assertFalse(context["tradable_event"])
        self.assertEqual(signal.opportunity_state, "no_opportunity")
        self.assertIn("news_event_not_tradable_catalyst", signal.current_evidence_conflict)

    def test_technical_trend_in_choppy_state_is_not_tradeable_candidate(self):
        context = build_technical_context(
            "RB",
            {
                "trend": Signal.BULLISH,
                "macd": Signal.BULLISH,
                "adx": Signal.BEARISH,
                "rsi": Signal.BEARISH,
                "mean_reversion": Signal.NEUTRAL,
            },
            {"trend_strength": 19, "volatility": 0.20, "volume_ratio": 0.75, "price_range": 0.04},
        )
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.70,
            horizon_class="short",
            opportunity_type="trend_continuation",
            business_quality_score=0.75,
            invalidation_level=100.0,
        )

        signal = apply_trade_research_contract(signal, context, analyst="technical", ticker="RB")

        self.assertIn(context["market_regime"], {"choppy", "range", "weak_trend"})
        self.assertEqual(signal.opportunity_state, "no_opportunity")
        self.assertIn("technical_trend_requires_regime_confirmation", signal.current_evidence_conflict)

    def test_medium_fundamental_anchor_without_short_trigger_is_no_opportunity(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BEARISH,
            confidence=0.75,
            horizon_class="medium",
            business_quality_score=0.80,
            opportunity_type="medium_fundamental",
        )
        context = {"tradeability": "high", "risk_flags": [], "factor_group_counts": {"inventory": {"up": 2}}}

        signal = apply_trade_research_contract(signal, context, analyst="fundamental", ticker="J")

        self.assertEqual(signal.opportunity_state, "no_opportunity")
        self.assertIn(
            "fundamental_anchor_requires_short_trigger_and_invalidation",
            signal.current_evidence_conflict,
        )

    def test_medium_fundamental_anchor_with_trigger_remains_direction_context(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BEARISH,
            confidence=0.75,
            horizon_class="medium",
            business_quality_score=0.80,
            opportunity_type="medium_fundamental",
            entry_trigger="intraday breakdown below support with volume confirmation is confirmed",
            invalidation_level=3200.0,
            would_change_view_if="short invalid if price closes back above the breakdown area",
        )
        context = {
            "tradeability": "high",
            "risk_flags": [],
            "factor_group_counts": {"inventory": {"up": 2}, "basis": {"down": 1}},
            "data_quality": {"supports_fundamental_trade_setup": True},
            "short_term_trigger_confirmed": True,
        }

        signal = apply_trade_research_contract(signal, context, analyst="fundamental", ticker="J")

        self.assertEqual(signal.signal, Signal.BEARISH)
        self.assertEqual(signal.opportunity_state, "no_opportunity")
        self.assertEqual(signal.evidence_role, "direction_context")
        self.assertEqual(signal.entry_timing_signal, "")
        self.assertEqual(signal.entry_trigger, "")
        self.assertIn(
            "fundamental_anchor_requires_short_trigger_and_invalidation",
            signal.current_evidence_conflict,
        )
        self.assertFalse(signal.invalidation_present)

    def test_pm_blocks_watch_for_trigger_new_entry_without_hard_product_rule(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.75,
            horizon_class="short",
            opportunity_state="watch_for_trigger",
            business_quality_score=0.70,
        )
        ratio, reasons, _notes, diagnostics = _apply_holding_rebalance_control(
            ticker="EB",
            trading_date="2025-01-03",
            position_ratio=0.05,
            current_ratio=0.0,
            current_position=None,
            analyst_signals=[signal],
            long_scores={"score": 0.70, "confidence": 0.70},
            short_scores={"score": 0.0, "confidence": 0.0},
            market_confirmation={"confirmation_score": 0.70},
            full_config={
                "portfolio_manager": {
                    "holding_rebalance_control": {
                        "enabled": True,
                        "watch_for_trigger_new_entry": {"enabled": True, "allow_probe": False},
                    }
                }
            },
            fusion_context={},
            risk_level=RiskLevel.SAFE,
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("pm_watch_for_trigger_not_tradeable", reasons)
        self.assertTrue(
            diagnostics["holding_rebalance_control"]["opportunity_state_summary"]["opportunity_state_counts"].get(
                "watch_for_trigger"
            )
        )

    def test_analyst_learning_calibration_improves_evidence_quality_without_trade_authority(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.50,
            business_quality_score=0.50,
            factor_alignment_score=0.50,
            horizon_class="short",
            setup_type="trend_breakout_setup",
            entry_timing_signal="breakout",
        )
        calibrated = calibrate_signal_with_learning_context(
            signal,
            analyst="technical",
            ticker="RB",
            learning_context={
                "prompt_learning_record_ids": [
                    "digest-1",
                    "action-value-technical-1",
                ],
                "technical_parameter_calibration": {
                    "applied": [
                        {
                            "id": "technical-policy-1",
                            "policy_type": "contextual_rule_calibration:technical_parameters",
                            "policy_action": "calibrate",
                            "ticker": "RB",
                            "side": "*",
                            "setup_type": "*",
                            "horizon_class": "short",
                            "market_regime": "trend",
                            "source_trading_date": "2025-07-01",
                            "valid_until": "2025-07-15",
                            "changed": {
                                "trend.short": {
                                    "from": 10,
                                    "to": 9,
                                    "rule": "trend_short_multiplier",
                                }
                            },
                        }
                    ]
                },
                "analyst_calibration_items": [
                    {
                        "ticker": "RB",
                        "side": "long",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "setup_type": "trend_breakout_setup",
                        "source_learning_record_id": "action-value-technical-1",
                        "action_name": "open",
                        "sample_count": 8,
                        "reward_mean": 1200.0,
                        "reward_sum": 9600.0,
                        "win_rate": 0.75,
                        "confidence_score": 0.70,
                        "action_preference": "controlled_open_or_add",
                        "signal_calibration": {
                            "contract_version": "agentquant.analysis_signal_calibration.v1",
                            "consumer_scope": "analyst_calibration",
                            "usable_by": ["analysis_team"],
                            "allowed_effects": ["evidence_quality_calibration", "setup_reliability_context"],
                            "forbidden_effects": ["trade_authority", "lots", "margin_ratio", "direction_override"],
                            "source_action_value_lane": "open",
                            "calibration_bias": "positive_evidence_calibration",
                        },
                        "product_learning_calibration_view": {
                            "contract_version": "agentquant.product_learning_calibration_view.v1",
                            "setup_type": "trend_breakout_setup",
                            "action_name": "open",
                            "trigger_key": canonical_entry_trigger("breakout", "long"),
                        },
                    }
                ]
            },
        )

        self.assertGreater(calibrated.business_quality_score, 0.50)
        self.assertGreater(calibrated.factor_alignment_score, 0.50)
        self.assertGreater(calibrated.confidence, 0.50)
        self.assertIn("trigger_reliability_positive", calibrated.setup_quality_notes)
        self.assertIn("technical_learning_calibration", calibrated.factor_focus)
        calibration = calibrated.metadata["analyst_learning_calibration"]
        self.assertGreater(calibration["positive_strength"], 0.0)
        self.assertEqual(calibration["negative_strength"], 0.0)
        self.assertIn("no_trade_authority", calibration["authority_boundary"])
        self.assertNotIn("authority_type", calibration)
        self.assertNotIn("lots", calibration)
        self.assertNotIn("margin_ratio", calibration)
        impact = calibrated.learning_impact_summary
        self.assertEqual(impact["contract_version"], "agentquant.analyst_learning_impact.v1")
        self.assertIn("RB:long:trend_breakout_setup:trend:open", impact["historical_support"])
        self.assertEqual(impact["historical_contradiction"], [])
        self.assertTrue(impact["prompt_calibration_applied"])
        self.assertEqual(
            impact["prompt_learning_record_ids"],
            ["digest-1", "action-value-technical-1"],
        )
        self.assertTrue(impact["evidence_calibration_applied"])
        self.assertEqual(
            impact["evidence_calibration_record_ids"],
            ["action-value-technical-1"],
        )
        self.assertTrue(impact["technical_parameter_calibration_applied"])
        self.assertEqual(
            impact["technical_parameter_calibrations"],
            [
                {
                    "policy_id": "technical-policy-1",
                    "policy_type": "contextual_rule_calibration:technical_parameters",
                    "policy_action": "calibrate",
                    "ticker": "RB",
                    "side": "*",
                    "setup_type": "*",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "source_trading_date": "2025-07-01",
                    "valid_until": "2025-07-15",
                    "parameter_changes": {
                        "trend.short": {"from": 10, "to": 9},
                    },
                }
            ],
        )
        self.assertIn("no_trade_authority", impact["authority_boundary"])
        self.assertNotIn("lots", impact)
        self.assertNotIn("margin_ratio", impact)
        self.assertEqual(calibrated.metadata["learning_impact_summary"], impact)
        finalized = apply_trade_research_contract(
            calibrated,
            {"opportunity_state": "tradeable_candidate"},
            analyst="technical",
            trading_date="2025-07-02",
            ticker="RB",
        )
        persisted_summary = finalized.metadata["action_evidence_contract"][
            "learning_impact_summary"
        ]
        self.assertEqual(
            persisted_summary["prompt_learning_record_ids"],
            ["digest-1", "action-value-technical-1"],
        )
        self.assertEqual(
            persisted_summary["technical_parameter_calibrations"],
            impact["technical_parameter_calibrations"],
        )

    def test_analyst_learning_calibration_uses_product_learning_scope_without_trade_authority(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.50,
            business_quality_score=0.50,
            factor_alignment_score=0.50,
            horizon_class="short",
            setup_type="trend_breakout_setup",
            entry_timing_signal="breakout",
        )
        performance_scope = (
            "EB|short|trend_breakout_setup|opening_range_breakdown|"
            "technical:used|fundamental:used|news:fresh|capital_deployed"
        )
        calibrated = calibrate_signal_with_learning_context(
            signal,
            analyst="technical",
            ticker="EB",
            learning_context={
                "alpha_setup_items": [
                    {
                        "ticker": "EB",
                        "side": "short",
                        "horizon_class": "short",
                        "market_regime": "range",
                        "setup_type": "trend_breakout_setup",
                        "action_name": "open",
                        "sample_count": 7,
                        "net_pnl": 9200.0,
                        "win_rate": 0.71,
                        "confidence_score": 0.76,
                        "product_learning_calibration_view": {
                            "contract_version": "agentquant.product_learning_calibration_view.v1",
                            "performance_scope_key": performance_scope,
                            "deployment_tier": "capital_deployed",
                            "historical_pm_rank": 1,
                            "historical_pm_score": 0.83,
                            "historical_net_pnl": 3180.0,
                            "setup_type": "trend_breakout_setup",
                            "action_name": "open",
                            "trigger_key": canonical_entry_trigger("breakout", "short"),
                            "not_trade_authority": True,
                            "future_only": True,
                        },
                    }
                ]
            },
        )

        self.assertGreater(calibrated.business_quality_score, 0.50)
        impact = calibrated.learning_impact_summary
        self.assertIn(performance_scope, impact["historical_support"])
        self.assertIn(performance_scope, impact["product_learning_scopes"])
        self.assertIn("no_trade_authority", impact["authority_boundary"])
        self.assertNotIn("opportunity_rank", impact)
        self.assertNotIn("authority_type", impact)
        self.assertNotIn("target_lots", impact)
        self.assertNotIn("lots_delta", impact)

    def test_latest_complete_episode_loss_cancels_stale_positive_analyst_calibration(
        self,
    ):
        trigger = canonical_entry_trigger("breakout", "long")
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.60,
            business_quality_score=0.60,
            factor_alignment_score=0.60,
            horizon_class="short",
            setup_type="trend_breakout_setup",
            entry_timing_signal="breakout",
        )
        calibrated = calibrate_signal_with_learning_context(
            signal,
            analyst="technical",
            ticker="RB",
            learning_context={
                "alpha_setup_items": [
                    {
                        "ticker": "RB",
                        "side": "long",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "setup_type": "trend_breakout_setup",
                        "action_name": "open",
                        "sample_count": 8,
                        "net_pnl": 18000.0,
                        "win_rate": 0.75,
                        "confidence_score": 0.80,
                        "product_learning_calibration_view": {
                            "contract_version": "agentquant.product_learning_calibration_view.v1",
                            "setup_type": "trend_breakout_setup",
                            "action_name": "open",
                            "trigger_key": trigger,
                        },
                    }
                ],
                "analyst_calibration_items": [
                    {
                        "ticker": "RB",
                        "side": "long",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "setup_type": "trend_breakout_setup",
                        "action_name": "open",
                        "sample_count": 6,
                        "confidence_score": 0.72,
                        "mean_return_on_notional": 0.025,
                        "latest_complete_episode_return_on_notional": -0.001,
                        "latest_complete_episode_outcome": "loss",
                        "signal_calibration": {
                            "contract_version": "agentquant.analysis_signal_calibration.v1",
                            "consumer_scope": "analyst_calibration",
                            "usable_by": ["analysis_team"],
                            "allowed_effects": ["evidence_quality_calibration"],
                            "forbidden_effects": [
                                "trade_authority",
                                "lots",
                                "margin_ratio",
                                "direction_override",
                            ],
                            "source_action_value_lane": "open",
                            "source_quality": "exact_real_state",
                            "calibration_bias": "positive_evidence_calibration",
                            "learning_economics_basis": "after_fee_return_on_notional",
                            "positive_amplification_suspended": True,
                        },
                        "product_learning_calibration_view": {
                            "contract_version": "agentquant.product_learning_calibration_view.v1",
                            "setup_type": "trend_breakout_setup",
                            "action_name": "open",
                            "trigger_key": trigger,
                        },
                    }
                ]
            },
        )

        calibration = calibrated.metadata["analyst_learning_calibration"]
        self.assertEqual(calibration["positive_strength"], 0.0)
        self.assertGreater(calibration["negative_strength"], 0.0)
        self.assertLess(calibration["net_evidence_adjustment"], 0.0)
        self.assertLess(calibrated.business_quality_score, 0.60)
        self.assertIn(
            "technical_same_scope_negative_learning",
            calibrated.current_evidence_conflict,
        )

    def test_analyst_safe_projection_carries_latest_episode_notional_economics(self):
        trigger = canonical_entry_trigger("breakout", "long")
        row = {
            "id": "av-rb-loss",
            "scope_key": "RB|long|short|trend|trend_breakout_setup|technical",
            "ticker": "RB",
            "side": "long",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "trend_breakout_setup",
            "action_name": "open",
            "canonical_action_family": "open_add_new_risk",
            "action_value_lane": "open",
            "learning_lane": "open",
            "consumer_scope": "pm_learning",
            "memory_side_role": "target_side",
            "canonical_action_value": True,
            "sample_count": 4,
            "reward_sum": 5000.0,
            "reward_mean": 1250.0,
            "win_rate": 0.75,
            "confidence_score": 0.70,
            "action_preference": "positive_candidate_open",
            "reward_source": "trade_episode",
            "evidence_scope": "exact_real_state",
            "last_sample_date": "2025-03-14",
            "valid_until": "2025-04-14",
            "payload": {
                "research_output_contract_version": "agentquant.research_action_value.v1",
                "canonical_action_family": "open_add_new_risk",
                "action_value_lane": "open",
                "learning_lane": "open",
                "consumer_scope": "pm_learning",
                "memory_side_role": "target_side",
                "action_preference": "positive_candidate_open",
                "reward_source": "trade_episode",
                "amplification_scope_quality": "exact_real_state",
                "mean_return_on_notional": 0.012,
                "latest_complete_episode_return_on_notional": -0.001,
                "latest_complete_episode_date": "2025-03-14",
                "latest_complete_episode_outcome": "loss",
                "signal_calibration": {
                    "contract_version": "agentquant.analysis_signal_calibration.v1",
                    "source_action_value_contract": "agentquant.research_action_value.v1",
                    "source_canonical_action_family": "open_add_new_risk",
                    "consumer_scope": "analyst_calibration",
                    "source_action_value_lane": "open",
                    "source_quality": "exact_real_state",
                    "reward_source": "trade_episode",
                    "calibration_bias": "negative_evidence_calibration",
                    "learning_economics_basis": "after_fee_return_on_notional",
                    "mean_return_on_notional": 0.012,
                    "latest_complete_episode_return_on_notional": -0.001,
                    "latest_complete_episode_date": "2025-03-14",
                    "latest_complete_episode_outcome": "loss",
                    "positive_amplification_suspended": True,
                    "usable_by": ["analysis_team"],
                    "allowed_effects": ["evidence_quality_calibration"],
                    "forbidden_effects": [
                        "trade_authority",
                        "lots",
                        "margin_ratio",
                        "direction_override",
                    ],
                },
                "product_learning_performance_key": {
                    "contract_version": "agentquant.product_learning_performance_key.v1",
                    "ticker": "RB",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "trigger_key": trigger,
                },
            },
        }

        projection = _safe_analyst_action_value_projection(
            row,
            analyst="technical",
            ticker="RB",
            trading_date="2025-03-17",
        )

        self.assertIsNotNone(projection)
        self.assertEqual(projection["mean_return_on_notional"], 0.012)
        self.assertEqual(
            projection["latest_complete_episode_return_on_notional"],
            -0.001,
        )
        self.assertEqual(
            projection["signal_calibration"]["calibration_bias"],
            "negative_evidence_calibration",
        )
        self.assertNotIn("reward_mean", projection)
        self.assertNotIn("net_pnl", projection)

    def test_analyst_entry_calibration_strength_is_notional_return_invariant(self):
        trigger = canonical_entry_trigger("breakout", "long")

        def calibrate(
            *,
            ticker: str,
            reward_mean: float,
            net_pnl: float,
            mean_return_on_notional: float = 0.008,
        ):
            signal = AnalystSignal(
                agent_name="technical",
                signal=Signal.BULLISH,
                confidence=0.50,
                business_quality_score=0.50,
                factor_alignment_score=0.50,
                horizon_class="short",
                setup_type="trend_breakout_setup",
                entry_timing_signal="breakout",
            )
            return calibrate_signal_with_learning_context(
                signal,
                analyst="technical",
                ticker=ticker,
                learning_context={
                    "analyst_calibration_items": [
                        {
                            "ticker": ticker,
                            "side": "long",
                            "horizon_class": "short",
                            "market_regime": "trend",
                            "setup_type": "trend_breakout_setup",
                            "action_name": "open",
                            "sample_count": 1,
                            "confidence_score": 0.10,
                            "reward_mean": reward_mean,
                            "net_pnl": net_pnl,
                            "mean_return_on_notional": mean_return_on_notional,
                            "latest_complete_episode_return_on_notional": 0.006,
                            "latest_complete_episode_outcome": "profit",
                            "signal_calibration": {
                                "contract_version": "agentquant.analysis_signal_calibration.v1",
                                "consumer_scope": "analyst_calibration",
                                "usable_by": ["analysis_team"],
                                "allowed_effects": ["evidence_quality_calibration"],
                                "forbidden_effects": [
                                    "trade_authority",
                                    "lots",
                                    "margin_ratio",
                                    "direction_override",
                                ],
                                "source_action_value_lane": "open",
                                "calibration_bias": "positive_evidence_calibration",
                                "learning_economics_basis": "after_fee_return_on_notional",
                            },
                            "product_learning_calibration_view": {
                                "contract_version": "agentquant.product_learning_calibration_view.v1",
                                "setup_type": "trend_breakout_setup",
                                "action_name": "open",
                                "trigger_key": trigger,
                            },
                        }
                    ]
                },
            )

        small_cny = calibrate(ticker="RB", reward_mean=500.0, net_pnl=2500.0)
        large_cny = calibrate(ticker="HC", reward_mean=5000.0, net_pnl=25000.0)
        stronger_return = calibrate(
            ticker="I",
            reward_mean=500.0,
            net_pnl=2500.0,
            mean_return_on_notional=0.018,
        )

        self.assertEqual(
            small_cny.metadata["analyst_learning_calibration"]["positive_strength"],
            large_cny.metadata["analyst_learning_calibration"]["positive_strength"],
        )
        self.assertEqual(
            small_cny.metadata["analyst_learning_calibration"]["net_evidence_adjustment"],
            large_cny.metadata["analyst_learning_calibration"]["net_evidence_adjustment"],
        )
        self.assertGreater(
            stronger_return.metadata["analyst_learning_calibration"]["positive_strength"],
            small_cny.metadata["analyst_learning_calibration"]["positive_strength"],
        )

    def test_entry_learning_rejects_wrong_lane_setup_and_trigger_with_zero_effect(self):
        breakout_trigger = canonical_entry_trigger("breakout", "long")
        pullback_trigger = canonical_entry_trigger("pullback", "long")

        def action_value_row(*, lane="open", setup="trend_breakout_setup", trigger=breakout_trigger):
            return {
                "ticker": "RB",
                "side": "long",
                "horizon_class": "short",
                "market_regime": "trend",
                "setup_type": setup,
                "action_name": lane,
                "sample_count": 12,
                "confidence_score": 0.85,
                "signal_calibration": {
                    "contract_version": "agentquant.analysis_signal_calibration.v1",
                    "consumer_scope": "analyst_calibration",
                    "usable_by": ["analysis_team"],
                    "allowed_effects": ["evidence_quality_calibration"],
                    "forbidden_effects": [
                        "trade_authority",
                        "lots",
                        "margin_ratio",
                        "direction_override",
                    ],
                    "source_action_value_lane": lane,
                    "calibration_bias": "positive_evidence_calibration",
                },
                "product_learning_calibration_view": {
                    "contract_version": "agentquant.product_learning_calibration_view.v1",
                    "setup_type": setup,
                    "action_name": lane,
                    "trigger_key": trigger,
                },
            }

        alpha_profile = {
            "ticker": "RB",
            "side": "long",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "trend_breakout_setup",
            "sample_count": 9,
            "net_pnl": 15000.0,
            "win_rate": 0.80,
            "confidence_score": 0.82,
            "product_learning_calibration_view": {
                "contract_version": "agentquant.product_learning_calibration_view.v1",
                "setup_type": "trend_breakout_setup",
                "action_name": "execution",
                "trigger_key": breakout_trigger,
            },
        }
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.50,
            business_quality_score=0.50,
            factor_alignment_score=0.50,
            horizon_class="short",
            setup_type="trend_breakout_setup",
            entry_timing_signal="breakout",
        )

        calibrated = calibrate_signal_with_learning_context(
            signal,
            analyst="technical",
            ticker="RB",
            learning_context={
                "alpha_setup_items": [alpha_profile],
                "analyst_calibration_items": [
                    action_value_row(lane="hold"),
                    action_value_row(lane="reduce"),
                    action_value_row(lane="exit"),
                    action_value_row(lane="execution"),
                    action_value_row(setup="trend_pullback_setup"),
                    action_value_row(trigger=pullback_trigger),
                ],
            },
        )

        self.assertEqual(calibrated.business_quality_score, 0.50)
        self.assertEqual(calibrated.factor_alignment_score, 0.50)
        self.assertEqual(calibrated.confidence, 0.50)
        self.assertNotIn("technical_learning_calibration", calibrated.factor_focus)
        calibration = calibrated.metadata["analyst_learning_calibration"]
        self.assertFalse(calibration["enabled"])
        self.assertEqual(calibration["eligible_entry_learning_count"], 0)
        self.assertEqual(calibration["positive_strength"], 0.0)
        self.assertEqual(calibration["negative_strength"], 0.0)
        self.assertEqual(
            calibration["rejected_entry_learning_reason_counts"],
            {
                "learning_lane_not_open_add": 5,
                "setup_mismatch": 1,
                "canonical_trigger_mismatch": 1,
            },
        )

    def test_fundamental_entry_learning_without_canonical_trigger_remains_cold_start(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BEARISH,
            confidence=0.62,
            business_quality_score=0.62,
            factor_alignment_score=0.55,
            horizon_class="medium",
        )
        calibrated = calibrate_signal_with_learning_context(
            signal,
            analyst="fundamental",
            ticker="J",
            learning_context={
                "analyst_calibration_items": [
                    {
                        "ticker": "J",
                        "side": "short",
                        "horizon_class": "medium",
                        "market_regime": "trend",
                        "setup_type": "inventory_basis_short_setup",
                        "action_name": "open",
                        "sample_count": 6,
                        "reward_mean": -1500.0,
                        "reward_sum": -9000.0,
                        "win_rate": 0.20,
                        "confidence_score": 0.65,
                        "action_preference": "cap_revalidate_before_open",
                        "signal_calibration": {
                            "contract_version": "agentquant.analysis_signal_calibration.v1",
                            "consumer_scope": "analyst_calibration",
                            "usable_by": ["analysis_team"],
                            "allowed_effects": ["evidence_quality_calibration", "setup_reliability_context"],
                            "forbidden_effects": ["trade_authority", "lots", "margin_ratio", "direction_override"],
                            "source_action_value_lane": "open",
                            "calibration_bias": "negative_evidence_calibration",
                        },
                    },
                    {
                        "ticker": "RB",
                        "side": "short",
                        "horizon_class": "medium",
                        "market_regime": "trend",
                        "setup_type": "inventory_basis_short_setup",
                        "action_name": "open",
                        "sample_count": 12,
                        "reward_mean": 900.0,
                        "reward_sum": 10800.0,
                        "win_rate": 0.70,
                        "confidence_score": 0.75,
                        "action_preference": "controlled_open_or_add",
                        "signal_calibration": {
                            "contract_version": "agentquant.analysis_signal_calibration.v1",
                            "consumer_scope": "analyst_calibration",
                            "usable_by": ["analysis_team"],
                            "allowed_effects": ["evidence_quality_calibration", "setup_reliability_context"],
                            "forbidden_effects": ["trade_authority", "lots", "margin_ratio", "direction_override"],
                            "source_action_value_lane": "open",
                            "calibration_bias": "positive_evidence_calibration",
                        },
                    },
                ]
            },
        )

        self.assertEqual(calibrated.business_quality_score, 0.62)
        self.assertEqual(calibrated.factor_alignment_score, 0.55)
        self.assertEqual(calibrated.confidence, 0.62)
        self.assertNotIn("factor_reliability_negative", calibrated.setup_quality_notes)
        self.assertNotIn("fundamental_broad_prior_weak_only", calibrated.setup_quality_notes)
        self.assertNotIn("fundamental_same_scope_negative_learning", calibrated.current_evidence_conflict)
        calibration = calibrated.metadata["analyst_learning_calibration"]
        self.assertFalse(calibration["enabled"])
        self.assertEqual(calibration["same_ticker_matched_count"], 0)
        self.assertEqual(calibration["broad_prior_matched_count"], 0)
        self.assertEqual(
            calibration["rejected_entry_learning_reason_counts"],
            {"setup_identity_missing_or_conflicting": 2},
        )
        self.assertIn("no_trade_authority", calibration["authority_boundary"])
        impact = calibrated.learning_impact_summary
        self.assertEqual(impact["historical_contradiction"], [])
        self.assertEqual(impact["historical_support"], [])
        self.assertIn("no_trade_authority", impact["authority_boundary"])
        factor_summary = calibrated.factor_calibration_summary
        self.assertEqual(factor_summary["contract_version"], "agentquant.factor_calibration.v1")
        self.assertNotIn("fundamental_learning_calibration", factor_summary["effective_factors"])
        self.assertNotIn("fundamental_same_scope_negative_learning", factor_summary["stale_or_conflicting_factors"])
        self.assertIn("no_trade_authority", factor_summary["authority_boundary"])
        self.assertEqual(calibrated.metadata["factor_calibration_summary"], factor_summary)

    def test_analyst_learning_calibration_ignores_raw_action_value_without_signal_calibration(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.50,
            business_quality_score=0.50,
            factor_alignment_score=0.50,
            horizon_class="short",
        )
        calibrated = calibrate_signal_with_learning_context(
            signal,
            analyst="technical",
            ticker="RB",
            learning_context={
                "alpha_setup_action_values": [
                    {
                        "ticker": "RB",
                        "side": "long",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "setup_type": "trend_breakout_setup",
                        "action_name": "open",
                        "sample_count": 8,
                        "reward_mean": 1200.0,
                        "reward_sum": 9600.0,
                        "win_rate": 0.75,
                        "confidence_score": 0.70,
                        "action_preference": "positive_candidate_open",
                    }
                ]
            },
        )

        self.assertEqual(calibrated.business_quality_score, 0.50)
        self.assertEqual(calibrated.factor_alignment_score, 0.50)
        self.assertEqual(calibrated.confidence, 0.50)
        calibration = calibrated.metadata["analyst_learning_calibration"]
        self.assertFalse(calibration["enabled"])
        self.assertEqual(calibration["positive_strength"], 0.0)
        self.assertEqual(calibration["negative_strength"], 0.0)

    def test_analyst_learning_calibration_rejects_wrong_nested_scope_or_version(self):
        base_row = {
            "ticker": "RB",
            "side": "long",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "trend_breakout_setup",
            "action_name": "open",
            "sample_count": 8,
            "confidence_score": 0.70,
            "signal_calibration": {
                "contract_version": "agentquant.analysis_signal_calibration.v1",
                "consumer_scope": "analyst_calibration",
                "usable_by": ["analysis_team"],
                "allowed_effects": ["evidence_quality_calibration"],
                "forbidden_effects": [
                    "trade_authority",
                    "lots",
                    "margin_ratio",
                    "direction_override",
                ],
                "source_action_value_lane": "open",
                "calibration_bias": "positive_evidence_calibration",
            },
        }
        for field, value in (
            ("contract_version", "agentquant.analysis_signal_calibration.v0"),
            ("consumer_scope", "pm_learning"),
        ):
            with self.subTest(field=field):
                row = dict(base_row)
                calibration = dict(base_row["signal_calibration"])
                calibration[field] = value
                row["signal_calibration"] = calibration
                signal = AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.50,
                    business_quality_score=0.50,
                    factor_alignment_score=0.50,
                    horizon_class="short",
                )
                calibrated = calibrate_signal_with_learning_context(
                    signal,
                    analyst="technical",
                    ticker="RB",
                    learning_context={"analyst_calibration_items": [row]},
                )
                self.assertEqual(calibrated.business_quality_score, 0.50)
                self.assertEqual(calibrated.factor_alignment_score, 0.50)
                self.assertEqual(calibrated.confidence, 0.50)
                self.assertFalse(
                    calibrated.metadata["analyst_learning_calibration"]["enabled"]
                )

    def test_news_learning_calibration_writes_event_summary_without_trade_authority(self):
        signal = AnalystSignal(
            agent_name="commodity_news",
            signal=Signal.BULLISH,
            confidence=0.55,
            business_quality_score=0.50,
            factor_alignment_score=0.50,
            horizon_class="event_short",
            event_type="supply_disruption",
            impact_window_days=2,
            trigger_valid=False,
            entry_trigger="wait for price and volume confirmation",
            entry_timing_signal="event_immediate",
            setup_type="news_event_setup",
            factor_focus=["supply_disruption_event"],
        )
        calibrated = calibrate_signal_with_learning_context(
            signal,
            analyst="commodity_news",
            ticker="BU",
            learning_context={
                "analyst_calibration_items": [
                    {
                        "ticker": "BU",
                        "side": "long",
                        "horizon_class": "event_short",
                        "market_regime": "event_window",
                        "setup_type": "news_event_setup",
                        "action_name": "open",
                        "sample_count": 3,
                        "reward_mean": 800.0,
                        "win_rate": 0.67,
                        "confidence_score": 0.55,
                        "signal_calibration": {
                            "contract_version": "agentquant.analysis_signal_calibration.v1",
                            "consumer_scope": "analyst_calibration",
                            "usable_by": ["analysis_team"],
                            "allowed_effects": ["evidence_quality_calibration", "setup_reliability_context"],
                            "forbidden_effects": ["trade_authority", "lots", "margin_ratio", "direction_override"],
                            "source_action_value_lane": "open",
                            "calibration_bias": "positive_evidence_calibration",
                        },
                        "product_learning_calibration_view": {
                            "contract_version": "agentquant.product_learning_calibration_view.v1",
                            "setup_type": "news_event_setup",
                            "action_name": "open",
                            "trigger_key": canonical_entry_trigger("event_immediate", "long"),
                        },
                    }
                ]
            },
        )

        event_summary = calibrated.event_calibration_summary
        self.assertEqual(event_summary["contract_version"], "agentquant.event_calibration.v1")
        self.assertIn("supply_disruption_event", event_summary["effective_catalysts"])
        self.assertEqual(event_summary["impact_window_assessment"], "2")
        self.assertTrue(event_summary["price_volume_confirmation_required"])
        self.assertIn("BU:long:news_event_setup:event_window:open", event_summary["supporting_learning_scopes"])
        self.assertIn("no_trade_authority", event_summary["authority_boundary"])
        self.assertNotIn("lots", event_summary)
        self.assertNotIn("margin_ratio", event_summary)
        self.assertEqual(calibrated.metadata["event_calibration_summary"], event_summary)

    def test_pm_opportunity_summary_reads_analyst_opportunity_state(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.58,
            business_quality_score=0.58,
            opportunity_state="probe_candidate",
            entry_trigger="break above opening range with volume",
            invalidation_level=3320.0,
            trigger_valid=True,
            invalidation_present=True,
        )

        summary = _side_opportunity_state_summary([technical], "long")

        self.assertEqual(summary["supporting_signal_count"], 1)
        self.assertEqual(summary["opportunity_state_counts"]["probe_candidate"], 1)
        self.assertTrue(summary["has_probe_candidate_support"])
        self.assertTrue(summary["has_tradeable_support"])

class OpportunityScorecardLearningRegressionTest(unittest.TestCase):
    def _tradeable_signal(self) -> AnalystSignal:
        return AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.72,
            business_quality_score=0.70,
            setup_quality_score=0.76,
            opportunity_state="tradeable_candidate",
            entry_trigger="current breakdown confirmed below support",
            invalidation_level=105.0,
            trigger_valid=True,
            invalidation_present=True,
            metadata={
                "action_evidence_contract": {
                    "setup_quality_ok": True,
                    "trigger_valid": True,
                    "current_trigger_confirmed": True,
                    "opportunity_state": "tradeable_candidate",
                    "entry_trigger": "current breakdown confirmed below support",
                    "invalidation_present": True,
                    "invalidation_condition": "short invalid if price closes above 105",
                }
            },
        )

    def _action_value(
        self,
        *,
        action_preference: str,
        lane: str,
        reward_mean: float,
        reward_sum: float,
        worst_reward: float | None = None,
        mean_return_on_notional: float | None = None,
        worst_return_on_notional: float | None = None,
        reward_source: str = "trade_episode",
        scope: str = "exact_real_state",
        last_sample_date: str = "2025-03-12",
        sample_count: int = 4,
        latest_complete_episode_return_on_notional: float | None = None,
        latest_complete_episode_date: str | None = None,
    ) -> dict:
        episode_mean_return = (
            float(mean_return_on_notional)
            if mean_return_on_notional is not None
            else float(reward_mean) / 100000.0
        )
        episode_worst_return = (
            float(worst_return_on_notional)
            if worst_return_on_notional is not None
            else float(worst_reward if worst_reward is not None else reward_mean) / 100000.0
        )
        canonical_family = (
            "open_add_new_risk"
            if lane in {"open", "add", "scale", "increase"}
            else "hold"
            if lane == "hold"
            else "reduce_exit"
            if lane in {"reduce", "exit"}
            else "execution"
            if lane == "execution"
            else "conditional_monitor"
        )
        return {
            "ticker": "TA",
            "side": "short",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "trend_breakout_setup",
            "action_name": lane,
            "canonical_action_value": True,
            "canonical_action_family": canonical_family,
            "action_value_lane": lane,
            "learning_lane": lane,
            "consumer_scope": "pm_learning",
            "sample_count": sample_count,
            "reward_mean": reward_mean,
            "reward_sum": reward_sum,
            "worst_reward": worst_reward if worst_reward is not None else reward_mean,
            "confidence_score": 0.70,
            "action_preference": action_preference,
            "last_sample_date": last_sample_date,
            "payload": {
                "source": "alpha_setup_profile_action_value",
                "action_value_lane": lane,
                "action_preference": action_preference,
                "reward_source": reward_source,
                "mean_return_on_notional": episode_mean_return,
                "worst_return_on_notional": episode_worst_return,
                "episode_return_on_notional_count": sample_count,
                "amplification_scope_quality": scope,
                "episode_trade_reward_count": sample_count if "episode" in reward_source else 0,
                "real_trade_reward_count": sample_count,
                "last_sample_date": last_sample_date,
                "latest_complete_episode_return_on_notional": (
                    latest_complete_episode_return_on_notional
                ),
                "latest_complete_episode_date": (
                    latest_complete_episode_date or last_sample_date
                ),
                "latest_complete_episode_outcome": (
                    "loss"
                    if latest_complete_episode_return_on_notional is not None
                    and latest_complete_episode_return_on_notional < 0.0
                    else "profit"
                    if latest_complete_episode_return_on_notional is not None
                    and latest_complete_episode_return_on_notional > 0.0
                    else "flat"
                    if latest_complete_episode_return_on_notional is not None
                    else None
                ),
            },
        }

    def _scorecard_config(self) -> dict:
        return {
            "tradeable_threshold": 0.58,
            "deployable_threshold": 0.72,
            "weak_confirmation_threshold": 0.45,
            "score_component_weights": {
                "positive_learning": 0.12,
                "negative_learning": 0.16,
                "execution_profile_learning": 0.10,
                "recent_tail_loss_penalty": 0.18,
                "entry_quality_loss_penalty": 0.12,
                "trigger_quality_positive_bonus": 0.08,
                "trigger_quality_loss_penalty": 0.10,
            },
            "learning_reward_unit": 1000.0,
            "learning_full_weight_sample_count": 3,
            "learning_recency_half_life_days": 10,
            "learning_recency_floor": 0.20,
            "tail_loss_reward_threshold": -1000.0,
        }

    def test_candidate_quality_does_not_recount_setup_learning_or_conflict(self):
        row = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[self._tradeable_signal()],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[
                self._action_value(
                    action_preference="positive_candidate_open",
                    lane="open",
                    reward_mean=1600.0,
                    reward_sum=6400.0,
                )
            ],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )["short"]

        components = row["candidate_quality_components"]
        self.assertEqual(
            set(components),
            {"opportunity_score", "trigger_quality", "invalidation_quality"},
        )
        self.assertAlmostEqual(components["trigger_quality"], 0.04)
        self.assertAlmostEqual(components["invalidation_quality"], 0.04)
        self.assertAlmostEqual(
            row["candidate_quality"],
            min(1.0, row["opportunity_score"] + 0.08),
        )

    def test_fallback_action_value_keeps_partial_rank_weight_without_exact_promotion(self):
        signal = self._tradeable_signal()
        exact_value = self._action_value(
            action_preference="positive_candidate_open",
            lane="open",
            reward_mean=1600.0,
            reward_sum=6400.0,
        )
        exact_value["retrieval_match_level"] = "exact_state"
        fallback_value = json.loads(json.dumps(exact_value))
        fallback_value["retrieval_match_level"] = "same_ticker_side_horizon"

        exact = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[exact_value],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )["short"]["action_value_learning_summary"]
        fallback = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[fallback_value],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )["short"]["action_value_learning_summary"]

        self.assertGreater(fallback["positive_learning_signal"], 0.0)
        self.assertLess(fallback["positive_learning_signal"], exact["positive_learning_signal"])
        self.assertEqual(fallback["exact_real_count"], 0)
        self.assertEqual(fallback["strongest_positive"]["scope"], "partial_real_state")
        self.assertEqual(exact["exact_real_count"], 1)

    def test_rank_learning_uses_episode_return_not_currency_reward(self):
        signal = self._tradeable_signal()

        def learning_summary(*, reward_mean: float, reward_sum: float, episode_return: float):
            row = self._action_value(
                action_preference="positive_candidate_open",
                lane="open",
                reward_mean=reward_mean,
                reward_sum=reward_sum,
                mean_return_on_notional=episode_return,
                worst_return_on_notional=episode_return,
            )
            return build_opportunity_scorecard(
                ticker="TA",
                analyst_signals=[signal],
                market_confirmation={"confirmation_score": 0.70},
                data_quality_summary={},
                alpha_setup_action_values=[row],
                decision_date="2025-03-15",
                config=self._scorecard_config(),
            )["short"]["action_value_learning_summary"]

        small_cny = learning_summary(
            reward_mean=1000.0,
            reward_sum=4000.0,
            episode_return=0.05,
        )
        large_cny = learning_summary(
            reward_mean=50000.0,
            reward_sum=200000.0,
            episode_return=0.05,
        )
        lower_return = learning_summary(
            reward_mean=50000.0,
            reward_sum=200000.0,
            episode_return=0.01,
        )

        self.assertEqual(
            small_cny["positive_learning_signal"],
            large_cny["positive_learning_signal"],
        )
        self.assertGreater(
            small_cny["positive_learning_signal"],
            lower_return["positive_learning_signal"],
        )

    def test_latest_complete_loss_removes_old_positive_rank_and_profile_lift(self):
        learned = self._action_value(
            action_preference="positive_candidate_open",
            lane="open",
            reward_mean=2500.0,
            reward_sum=10000.0,
            mean_return_on_notional=0.025,
            worst_return_on_notional=-0.001,
            latest_complete_episode_return_on_notional=-0.001,
            latest_complete_episode_date="2025-03-14",
        )
        row = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[self._tradeable_signal()],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_profiles=[{
                "ticker": "TA",
                "side": "short",
                "setup_type": "trend_breakout_setup",
                "lifecycle_state": "deployable",
                "sample_count": 7,
                "confidence_score": 0.80,
                "net_pnl": 10000.0,
            }],
            alpha_setup_action_values=[learned],
            formal_setup_by_side={"short": "trend_breakout_setup", "long": ""},
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )["short"]

        summary = row["action_value_learning_summary"]
        self.assertTrue(summary["latest_complete_episode_loss"])
        self.assertTrue(summary["positive_amplification_suspended"])
        self.assertEqual(summary["positive_learning_signal"], 0.0)
        self.assertGreater(summary["negative_learning_signal"], 0.0)
        self.assertEqual(row["opportunity_score_components"]["positive_learning"], 0.0)
        self.assertLessEqual(
            row["opportunity_score_components"]["alpha_profile_adjustment"],
            0.0,
        )

    def test_latest_profitable_probe_restores_positive_learning_path(self):
        learned = self._action_value(
            action_preference="positive_candidate_open",
            lane="open",
            reward_mean=1200.0,
            reward_sum=4800.0,
            mean_return_on_notional=0.012,
            latest_complete_episode_return_on_notional=0.004,
            latest_complete_episode_date="2025-03-14",
        )
        row = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[self._tradeable_signal()],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[learned],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )["short"]

        summary = row["action_value_learning_summary"]
        self.assertFalse(summary["latest_complete_episode_loss"])
        self.assertFalse(summary["positive_amplification_suspended"])
        self.assertGreater(summary["positive_learning_signal"], 0.0)

    def test_positive_learning_cannot_resolve_current_dominant_opposition(self):
        learned = self._action_value(
            action_preference="positive_candidate_open",
            lane="open",
            reward_mean=5000.0,
            reward_sum=20000.0,
            mean_return_on_notional=0.05,
        )
        row = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[self._tradeable_signal()],
            market_confirmation={"confirmation_score": 0.75},
            data_quality_summary={},
            signal_collection_contract={
                "evidence_fusion": {
                    "multi_evidence_consensus_score": 0.40,
                    "cross_analyst_conflicts": ["technical_vs_fundamental"],
                    "dominant_opposing_evidence": ["fundamental_opposes_short"],
                }
            },
            alpha_setup_action_values=[learned],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )["short"]

        self.assertGreater(row["validated_learning_delta"], 0.0)
        self.assertIn(
            "dominant_opposing_evidence_requires_pm_resolution",
            row["gating_failures"],
        )
        self.assertEqual(row["final_state"], "watch_for_trigger")

    def test_candidate_and_watchlist_profiles_do_not_add_positive_rank_value(self):
        common = {
            "ticker": "TA",
            "analyst_signals": [self._tradeable_signal()],
            "market_confirmation": {"confirmation_score": 0.70},
            "data_quality_summary": {},
            "formal_setup_by_side": {
                "short": "trend_breakout_setup",
                "long": "",
            },
            "decision_date": "2025-03-15",
            "config": self._scorecard_config(),
        }
        for lifecycle_state in ("candidate", "watchlist"):
            row = build_opportunity_scorecard(
                **common,
                alpha_setup_profiles=[{
                    "ticker": "TA",
                    "side": "short",
                    "setup_type": "trend_breakout_setup",
                    "lifecycle_state": lifecycle_state,
                    "sample_count": 20,
                    "confidence_score": 0.95,
                    "net_pnl": 50000.0,
                }],
            )["short"]
            self.assertEqual(
                row["opportunity_score_components"]["alpha_profile_adjustment"],
                0.0,
            )

    def test_scorecard_excludes_cross_setup_profile_from_formal_rank(self):
        signal = self._tradeable_signal()
        profiles = [
            {
                "side": "short",
                "setup_type": "trend_breakout_setup",
                "lifecycle_state": "deployable",
                "sample_count": 4,
                "confidence_score": 0.75,
            },
            {
                "side": "short",
                "setup_type": "volatility_breakout_setup",
                "lifecycle_state": "capped",
                "sample_count": 12,
                "confidence_score": 0.95,
            },
        ]
        scorecard = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_profiles=profiles,
            formal_setup_by_side={"short": "trend_breakout_setup", "long": ""},
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )

        counts = scorecard["short"]["alpha_setup_profile_counts"]
        self.assertEqual(counts["deployable"], 1)
        self.assertEqual(counts["capped_or_rejected"], 0)

    def test_fallback_action_value_cannot_support_real_amplification(self):
        row = self._action_value(
            action_preference="positive_candidate_open",
            lane="open",
            reward_mean=1600.0,
            reward_sum=6400.0,
        )
        row["retrieval_match_level"] = "same_ticker_side_horizon"

        self.assertEqual(
            _action_value_scope_quality(row, ticker="TA", side="short"),
            "partial_real_state",
        )
        self.assertFalse(_action_value_can_support_real_amplification(row, ticker="TA", side="short"))

        row["retrieval_match_level"] = "exact_state"
        self.assertEqual(
            _action_value_scope_quality(row, ticker="TA", side="short"),
            "exact_real_state",
        )
        self.assertTrue(_action_value_can_support_real_amplification(row, ticker="TA", side="short"))

    def test_fallback_does_not_upgrade_counterfactual_scope(self):
        row = self._action_value(
            action_preference="positive_candidate_open",
            lane="open",
            reward_mean=1600.0,
            reward_sum=6400.0,
            scope="counterfactual_prior",
            reward_source="counterfactual_prior",
        )
        row["retrieval_match_level"] = "same_ticker_side_horizon"

        self.assertEqual(
            _action_value_scope_quality(row, ticker="TA", side="short"),
            "counterfactual_prior",
        )
        self.assertFalse(_action_value_can_support_real_amplification(row, ticker="TA", side="short"))

    def test_episode_action_values_move_scorecard_rank_without_product_blacklist(self):
        signal = self._tradeable_signal()
        positive = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[
                self._action_value(
                    action_preference="positive_candidate_open",
                    lane="open",
                    reward_mean=1600.0,
                    reward_sum=6400.0,
                )
            ],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )
        negative = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[
                self._action_value(
                    action_preference="tail_loss_protect",
                    lane="open",
                    reward_mean=-1800.0,
                    reward_sum=-7200.0,
                    worst_reward=-2400.0,
                )
            ],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )

        positive_components = positive["short"]["opportunity_score_components"]
        negative_components = negative["short"]["opportunity_score_components"]
        self.assertGreater(positive_components["positive_learning"], 0.0)
        self.assertLess(negative_components["negative_learning"], 0.0)
        self.assertLess(negative_components["recent_tail_loss_penalty"], 0.0)
        self.assertGreater(
            sum(positive_components.values()),
            sum(negative_components.values()),
        )
        self.assertNotIn("product_blacklist", json.dumps(negative["short"], ensure_ascii=False))

    def test_adaptive_policy_action_is_the_only_positive_or_negative_score_direction(self):
        signal = self._tradeable_signal()
        common = {
            "ticker": "TA",
            "analyst_signals": [signal],
            "market_confirmation": {"confirmation_score": 0.70},
            "data_quality_summary": {},
            "decision_date": "2025-03-15",
            "config": self._scorecard_config(),
        }
        capped = build_opportunity_scorecard(
            **common,
            adaptive_policy_state=[{
                "policy_type": "learning_mechanism:alpha_setup_ev",
                "policy_action": "cap",
            }],
        )["short"]
        protected = build_opportunity_scorecard(
            **common,
            adaptive_policy_state=[{
                "policy_type": "learning_mechanism:alpha_setup_ev",
                "policy_action": "protect",
            }],
        )["short"]

        self.assertEqual(capped["learning_positive_count"], 0)
        self.assertEqual(capped["learning_negative_count"], 1)
        self.assertEqual(capped["opportunity_score_components"]["positive_learning"], 0.0)
        self.assertLess(capped["opportunity_score_components"]["negative_learning"], 0.0)
        self.assertEqual(protected["learning_positive_count"], 1)
        self.assertEqual(protected["learning_negative_count"], 0)
        self.assertGreater(protected["opportunity_score_components"]["positive_learning"], 0.0)
        self.assertEqual(protected["opportunity_score_components"]["negative_learning"], 0.0)

    def test_entry_loss_episode_penalizes_entry_quality_and_capital_priority(self):
        signal = self._tradeable_signal()
        clean = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )
        loss_value = self._action_value(
            action_preference="tail_loss_protect",
            lane="open",
            reward_mean=-1800.0,
            reward_sum=-7200.0,
            worst_reward=-2400.0,
        )
        loss_value["payload"]["product_learning_performance_key"] = {
            "contract_version": "agentquant.product_learning_performance_key.v1",
            "entry_quality_outcome": {
                "contract_version": "agentquant.entry_quality_outcome.v1",
                "entry_quality_verdict": "entry_tail_loss_revalidate",
                "loss_episode": True,
                "tail_loss_episode": True,
                "penalty_weight": 0.55,
                "entry_trigger": "vwap pullback support",
                "trigger_key": "vwap_pullback_support",
                "future_only": True,
                "not_trade_authority": True,
            },
        }
        loss = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[loss_value],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )

        components = loss["short"]["opportunity_score_components"]
        summary = loss["short"]["action_value_learning_summary"]
        self.assertLess(components["entry_quality_loss_penalty"], 0.0)
        self.assertLess(components["trigger_quality_loss_penalty"], 0.0)
        self.assertGreater(summary["entry_quality_loss_signal"], 0.0)
        self.assertGreater(summary["trigger_quality_loss_signal"], 0.0)
        self.assertGreaterEqual(summary["net_trigger_quality_loss_signal"], 0.0)
        clean_rank_row = _ensure_final_rank_score_fields(dict(clean["short"]), config=self._scorecard_config())
        loss_rank_row = _ensure_final_rank_score_fields(dict(loss["short"]), config=self._scorecard_config())
        self.assertLess(loss_rank_row["rank_score"], clean_rank_row["rank_score"])

    def test_entry_quality_outcome_top_level_payload_also_penalizes_entry_quality(self):
        signal = self._tradeable_signal()
        loss_value = self._action_value(
            action_preference="tail_loss_protect",
            lane="open",
            reward_mean=-1200.0,
            reward_sum=-3600.0,
            worst_reward=-1800.0,
        )
        loss_value["payload"]["entry_quality_outcome"] = {
            "contract_version": "agentquant.entry_quality_outcome.v1",
            "entry_quality_verdict": "entry_loss_revalidate",
            "trigger_quality_verdict": "trigger_loss_revalidate",
            "loss_episode": True,
            "tail_loss_episode": False,
            "penalty_weight": 0.45,
            "entry_trigger": "vwap pullback support",
            "trigger_key": "vwap_pullback_support",
            "future_only": True,
            "not_trade_authority": True,
        }

        loss = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[loss_value],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )

        components = loss["short"]["opportunity_score_components"]
        summary = loss["short"]["action_value_learning_summary"]
        self.assertLess(components["entry_quality_loss_penalty"], 0.0)
        self.assertGreater(summary["entry_quality_loss_signal"], 0.0)

    def test_positive_trigger_episode_boosts_trigger_quality_without_second_rank(self):
        signal = self._tradeable_signal()
        clean = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )
        positive_value = self._action_value(
            action_preference="positive_candidate_open",
            lane="open",
            reward_mean=1800.0,
            reward_sum=7200.0,
            worst_reward=900.0,
        )
        positive_value["payload"]["product_learning_performance_key"] = {
            "contract_version": "agentquant.product_learning_performance_key.v1",
            "entry_quality_outcome": {
                "contract_version": "agentquant.entry_quality_outcome.v1",
                "entry_quality_verdict": "entry_quality_supported",
                "trigger_quality_verdict": "trigger_quality_supported",
                "trigger_confirmation_adjustment": "standard_confirmation_supported",
                "positive_entry_episode": True,
                "loss_episode": False,
                "tail_loss_episode": False,
                "support_weight": 0.40,
                "entry_trigger": "opening range breakdown",
                "trigger_key": "opening_range_breakdown",
                "future_only": True,
                "not_trade_authority": True,
            },
        }
        positive = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[positive_value],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )

        components = positive["short"]["opportunity_score_components"]
        summary = positive["short"]["action_value_learning_summary"]
        self.assertGreater(components["trigger_quality_positive_bonus"], 0.0)
        self.assertGreater(summary["trigger_quality_positive_signal"], 0.0)
        self.assertEqual(summary["net_trigger_quality_loss_signal"], 0.0)
        clean_rank_row = _ensure_final_rank_score_fields(dict(clean["short"]), config=self._scorecard_config())
        positive_rank_row = _ensure_final_rank_score_fields(dict(positive["short"]), config=self._scorecard_config())
        self.assertGreater(positive_rank_row["rank_score"], clean_rank_row["rank_score"])
        self.assertNotIn("opportunity_rank", positive["short"])
        self.assertNotIn("side_priority", positive["short"])
        self.assertEqual(
            positive["short"]["direction_evidence_boundary"],
            "fusion_preserves_signal_collector_evidence_no_pm_side_selection",
        )

    def test_pm_action_value_normalizer_preserves_canonical_fields_from_payload_or_top_level(self):
        payload_only = {
            "ticker": "TA",
            "side": "short",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "trend_breakout_setup",
            "action_name": "open",
            "canonical_action_family": "open_add_new_risk",
            "learning_lane": "open",
            "sample_count": 3,
            "reward_sum": -3600.0,
            "reward_mean": -1200.0,
            "win_rate": 0.0,
            "last_sample_date": "2025-03-14",
            "payload": {
                "action_preference": "tail_loss_protect",
                "reward_source": "trade_episode",
                "mean_return_on_notional": -0.018,
                "worst_return_on_notional": -0.024,
                "episode_return_on_notional_count": 4,
                "amplification_scope_quality": "exact_real_state",
                "action_value_lane": "open",
            },
        }
        normalized = _normalize_alpha_setup_action_value(payload_only)

        self.assertEqual(normalized["action_preference"], "tail_loss_protect")
        self.assertEqual(normalized["reward_source"], "trade_episode")
        self.assertEqual(normalized["evidence_scope"], "exact_real_state")
        self.assertEqual(normalized["action_value_lane"], "open")
        self.assertNotIn("consumer_scope", normalized)
        self.assertFalse(normalized["canonical_action_value"])

        scoped_payload = {
            **payload_only,
            "consumer_scope": "pm_learning",
        }
        normalized_scoped = _normalize_alpha_setup_action_value(scoped_payload)
        self.assertEqual(normalized_scoped["consumer_scope"], "pm_learning")
        self.assertTrue(normalized_scoped["canonical_action_value"])

        explicitly_noncanonical = {
            **payload_only,
            "canonical_action_value": False,
            "canonical_action_family": "open_add_new_risk",
            "learning_lane": "open",
        }
        normalized_noncanonical = _normalize_alpha_setup_action_value(explicitly_noncanonical)
        self.assertFalse(normalized_noncanonical["canonical_action_value"])
        self.assertEqual(
            normalized_noncanonical["canonical_action_value_source"],
            "incomplete_trace_not_for_pm_scoring",
        )

    def test_pm_memory_scope_view_rejects_missing_consumer_scope_before_retrieval_normalization(self):
        class FakeDB:
            def get_alpha_setup_action_values(self, **kwargs):
                return [
                    {"id": "missing-scope"},
                    {"id": "explicit-pm", "consumer_scope": "pm_learning"},
                    {"id": "trader", "consumer_scope": "trader_execution_learning"},
                ]

        rows = _ExplicitPMLearningScopeDBView(FakeDB()).get_alpha_setup_action_values()

        self.assertEqual([row["id"] for row in rows], ["explicit-pm"])

    def test_pm_action_value_merge_preserves_canonical_researcher_record(self):
        canonical = {
            "id": "av-hc-execution-real",
            "scope_key": "HC|short|short|choppy|breakdown",
            "ticker": "HC",
            "side": "short",
            "horizon_class": "short",
            "market_regime": "choppy",
            "setup_type": "breakdown",
            "action_name": "execution",
            "canonical_action_family": "execution",
            "action_value_lane": "execution",
            "learning_lane": "execution",
            "consumer_scope": "pm_learning",
            "action_preference": "positive_candidate_execution",
            "reward_source": "real_trade",
            "evidence_scope": "exact_real_state",
            "reward_mean": 199.62,
            "reward_sum": 199.62,
            "win_rate": 1.0,
            "sample_count": 1,
        }
        empty_trace = {
            "scope_key": "HC|short|short|choppy|breakdown",
            "ticker": "HC",
            "side": "short",
            "horizon_class": "short",
            "market_regime": "choppy",
            "setup_type": "breakdown",
            "action_name": "execution",
            "action_value_lane": "execution",
            "consumer_scope": "pm_learning",
        }

        cases = (([empty_trace], [canonical]), ([canonical], [empty_trace]))
        for base_rows, extra_rows in cases:
            merged = _append_unique_action_values(base_rows, extra_rows)
            trace = _select_learning_trace_action_values(merged, limit=3)

            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["id"], "av-hc-execution-real")
            self.assertEqual(merged[0]["action_preference"], "positive_candidate_execution")
            self.assertEqual(merged[0]["reward_source"], "real_trade")
            self.assertEqual(merged[0]["evidence_scope"], "exact_real_state")
            self.assertEqual(merged[0]["reward_mean"], 199.62)
            self.assertEqual(trace[0]["id"], "av-hc-execution-real")
            self.assertEqual(trace[0]["action_preference"], "positive_candidate_execution")

    def test_pm_artifact_excludes_incomplete_prior_from_formal_action_values(self):
        canonical = {
            "id": "av-j-open-real",
            "scope_key": "J|short|short|trend|news_event_setup|open",
            "ticker": "J",
            "side": "short",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "news_event_setup",
            "action_name": "open",
            "canonical_action_family": "open_add_new_risk",
            "action_value_lane": "open",
            "learning_lane": "open",
            "consumer_scope": "pm_learning",
            "action_preference": "positive_candidate_open",
            "reward_source": "real_trade",
            "evidence_scope": "exact_real_state",
            "reward_mean": 880.0,
            "reward_sum": 880.0,
            "win_rate": 1.0,
            "sample_count": 1,
        }
        weak_prior = {
            "scope_key": "*|short|short|trend|news_event_setup|open",
            "ticker": "*",
            "side": "short",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "*",
            "action_name": "open",
            "action_value_lane": "open",
            "learning_lane": "open",
            "consumer_scope": "pm_learning",
            "reward_source": "similar_sql_prior",
            "evidence_scope": "similar_sql_prior",
            "reward_mean": 120.0,
            "reward_sum": 240.0,
            "sample_count": 2,
            "retrieval_match_level": "similar",
            "retrieval_match_reason": "similar_setup_samples",
            "payload": {
                "prior_role": "weak_prior_not_action_preference",
                "canonical_action_preference_source": "none_for_similar_sql_prior",
            },
        }

        formal = _select_learning_trace_action_values([weak_prior, canonical], limit=10)
        updated_state = _attach_incomplete_prior_diagnostics_to_contract_state({
            "alpha_setup_action_values": [weak_prior, canonical],
            "control_diagnostics": {
                "final_action_memory_retrieval": {
                    "tool": "decision_memory_retrieval",
                    "rejected_or_downgraded": [],
                }
            },
        })
        rejected = updated_state["control_diagnostics"]["final_action_memory_retrieval"]["rejected_or_downgraded"]

        self.assertEqual([row["id"] for row in formal], ["av-j-open-real"])
        self.assertEqual(formal[0]["canonical_action_family"], "open_add_new_risk")
        self.assertEqual(formal[0]["action_preference"], "positive_candidate_open")
        self.assertTrue(formal[0]["canonical_action_value"])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["reason"], "incomplete_prior_not_pm_scoring_evidence")
        self.assertTrue(rejected[0]["diagnostic_only"])
        self.assertEqual(rejected[0]["evidence_scope"], "similar_sql_prior")

    def test_pm_artifact_filters_2025_03_26_missing_family_prior_rows(self):
        rows = []
        for ticker in ("I", "J", "RB", "SR"):
            rows.append({
                "id": f"{ticker.lower()}-2025-03-26-similar-prior",
                "scope_key": f"*|short|short|trend|news_event_setup|{ticker}",
                "ticker": ticker,
                "side": "short",
                "horizon_class": "short",
                "market_regime": "trend",
                "setup_type": "*",
                "action_name": "execution",
                "action_value_lane": "execution",
                "learning_lane": "execution",
                "consumer_scope": "pm_learning",
                "reward_source": "similar_sql_prior",
                "evidence_scope": "similar_sql_prior",
                "reward_mean": 1.0,
                "reward_sum": 1.0,
                "sample_count": 1,
                "retrieval_match_level": "similar",
                "retrieval_match_reason": "similar_setup_samples",
                "payload": {
                    "prior_role": "weak_prior_not_action_preference",
                    "canonical_action_preference_source": "none_for_similar_sql_prior",
                },
            })

        formal = _select_learning_trace_action_values(rows, limit=10)
        updated_state = _attach_incomplete_prior_diagnostics_to_contract_state({
            "alpha_setup_action_values": rows,
            "control_diagnostics": {"final_action_memory_retrieval": {"tool": "decision_memory_retrieval"}},
        })
        rejected = updated_state["control_diagnostics"]["final_action_memory_retrieval"]["rejected_or_downgraded"]

        self.assertEqual(formal, [])
        self.assertEqual({row["ticker"] for row in rejected}, {"I", "J", "RB", "SR"})
        self.assertEqual(
            {row["reason"] for row in rejected},
            {"incomplete_prior_not_pm_scoring_evidence"},
        )

    def test_pm_action_value_retrieval_uses_fallback_key_without_scope_drift(self):
        class FakeDB:
            def __init__(self):
                self.calls = []

            def get_alpha_setup_action_values(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("setup_type") == "trend_breakout_setup":
                    return []
                if kwargs.get("horizon_class") == "short" and kwargs.get("setup_type") is None:
                    return [
                        {
                            "scope_key": "TA|short|short|range|execution_pullback_setup|open",
                            "ticker": "TA",
                            "side": "short",
                            "horizon_class": "short",
                            "market_regime": "range",
                            "setup_type": "execution_pullback_setup",
                            "action_name": "open",
                            "canonical_action_value": True,
                            "canonical_action_family": "open_add_new_risk",
                            "sample_count": 2,
                            "reward_sum": -3600.0,
                            "reward_mean": -1800.0,
                            "win_rate": 0.0,
                            "action_preference": "tail_loss_protect",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "action_value_lane": "open",
                            "consumer_scope": "pm_learning",
                            "last_sample_date": "2025-03-03",
                        }
                    ]
                return []

        result = retrieve_pm_memory(
            db=FakeDB(),
            config_id="cfg",
            ticker="TA",
            side="short",
            horizon_class="short",
            market_regime="trend",
            setup_type="trend_breakout_setup",
            trading_date="2025-03-04",
        )
        rows = result["action_values"]
        detail = result["effective_memory_summary"]
        attempts = result["retrieval_attempts"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["consumer_scope"], "pm_learning")
        self.assertEqual(rows[0]["retrieval_match_level"], "same_ticker_side_horizon")
        self.assertEqual(rows[0]["evidence_scope"], "partial_real_state")
        self.assertEqual(detail["effective_row_count"], 1)
        self.assertEqual(attempts[0]["match_level"], "exact_state")
        self.assertEqual(attempts[0]["row_count"], 0)

    def test_pm_action_value_retrieval_fills_missing_lanes_after_exact_match(self):
        class FakeDB:
            def __init__(self):
                self.calls = []

            def get_alpha_setup_action_values(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("setup_type") == "news_event_setup":
                    return [
                        {
                            "id": "m-open-exact",
                            "scope_key": "M|long|short|trend|news_event_setup|open",
                            "ticker": "M",
                            "side": "long",
                            "horizon_class": "short",
                            "market_regime": "trend",
                            "setup_type": "news_event_setup",
                            "action_name": "open",
                            "canonical_action_value": True,
                            "canonical_action_family": "open_add_new_risk",
                            "sample_count": 1,
                            "reward_sum": 770.34,
                            "reward_mean": 770.34,
                            "win_rate": 1.0,
                            "confidence_score": 0.65,
                            "action_preference": "positive_candidate_open",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "action_value_lane": "open",
                            "consumer_scope": "pm_learning",
                            "learning_lane": "open",
                            "last_sample_date": "2025-03-04",
                        }
                    ]
                if kwargs.get("horizon_class") == "short" and kwargs.get("setup_type") is None:
                    return [
                        {
                            "id": "m-execution-fallback",
                            "scope_key": "M|long|short|trend|execution_pullback_setup|execution",
                            "ticker": "M",
                            "side": "long",
                            "horizon_class": "short",
                            "market_regime": "trend",
                            "setup_type": "execution_pullback_setup",
                            "action_name": "execution",
                            "canonical_action_value": True,
                            "canonical_action_family": "execution",
                            "sample_count": 1,
                            "reward_sum": 770.34,
                            "reward_mean": 770.34,
                            "win_rate": 1.0,
                            "confidence_score": 0.62,
                            "action_preference": "positive_candidate_execution",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "action_value_lane": "execution",
                            "consumer_scope": "pm_learning",
                            "learning_lane": "execution",
                            "last_sample_date": "2025-03-03",
                        }
                    ]
                return []

        result = retrieve_pm_memory(
            db=FakeDB(),
            config_id="cfg",
            ticker="M",
            side="long",
            horizon_class="short",
            market_regime="trend",
            setup_type="news_event_setup",
            trading_date="2025-03-11",
        )
        rows = result["action_values"]
        detail = result["effective_memory_summary"]
        attempts = result["retrieval_attempts"]

        ids = {row["id"] for row in rows}
        self.assertIn("m-open-exact", ids)
        self.assertIn("m-execution-fallback", ids)
        scope_by_id = {row["id"]: row["evidence_scope"] for row in rows}
        self.assertEqual(scope_by_id["m-open-exact"], "exact_real_state")
        self.assertEqual(scope_by_id["m-execution-fallback"], "partial_real_state")
        self.assertEqual(attempts[0]["row_count"], 1)
        self.assertIn("exact_state", detail["matched_levels"])
        self.assertIn("same_ticker_side_horizon", detail["matched_levels"])
        self.assertGreaterEqual(len(attempts), 2)

    def test_pm_lifecycle_audit_preserves_frozen_step4_pool_without_late_append(self):
        class FakeDB:
            def get_alpha_setup_action_values(self, **kwargs):
                return [
                    {
                        "id": "c-short-hold",
                        "scope_key": "C|short|short|trend|hold|hold",
                        "ticker": "C",
                        "side": "short",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "setup_type": "hold",
                        "action_name": "hold",
                        "canonical_action_value": True,
                        "canonical_action_family": "hold",
                        "sample_count": 1,
                        "reward_sum": -500.0,
                        "reward_mean": -500.0,
                        "win_rate": 0.0,
                        "action_preference": "negative_hold_revalidate",
                        "reward_source": "real_trade",
                        "evidence_scope": "exact_real_state",
                        "action_value_lane": "hold",
                        "learning_lane": "hold",
                        "consumer_scope": "pm_learning",
                        "memory_side_role": "current_position_side",
                        "last_sample_date": "2025-03-13",
                        "valid_until": "2025-04-13",
                    },
                    {
                        "id": "c-long-open",
                        "scope_key": "C|long|short|trend|news|open",
                        "ticker": "C",
                        "side": "long",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "setup_type": "news",
                        "action_name": "open",
                        "canonical_action_value": True,
                        "canonical_action_family": "open_add_new_risk",
                        "sample_count": 1,
                        "reward_sum": 800.0,
                        "reward_mean": 800.0,
                        "win_rate": 1.0,
                        "action_preference": "positive_candidate_open",
                        "reward_source": "real_trade",
                        "evidence_scope": "exact_real_state",
                        "action_value_lane": "open",
                        "learning_lane": "open",
                        "consumer_scope": "pm_learning",
                        "memory_side_role": "target_side",
                        "last_sample_date": "2025-03-13",
                        "valid_until": "2025-04-13",
                    },
                ]

        existing_rows = [
            FakeDB().get_alpha_setup_action_values()[0],
            {
                "id": "c-old-long-open",
                "ticker": "C",
                "side": "long",
                "action_name": "open",
                "canonical_action_value": True,
                "canonical_action_family": "open_add_new_risk",
                "action_value_lane": "open",
                "learning_lane": "open",
                "consumer_scope": "pm_learning",
                "memory_side_role": "target_side",
                "action_preference": "positive_candidate_open",
                "reward_source": "real_trade",
                "evidence_scope": "exact_real_state",
                "reward_sum": 1000.0,
                "reward_mean": 1000.0,
                "last_sample_date": "2025-03-13",
                "valid_until": "2025-04-13",
            },
            {
                "id": "c-old-execution",
                "ticker": "C",
                "side": "long",
                "action_name": "execution",
                "canonical_action_value": True,
                "canonical_action_family": "execution",
                "action_value_lane": "execution",
                "learning_lane": "execution",
                "consumer_scope": "pm_learning",
                "memory_side_role": "historical_sample_side",
                "action_preference": "positive_candidate_execution",
                "reward_source": "real_trade",
                "evidence_scope": "exact_real_state",
                "reward_sum": 1000.0,
                "reward_mean": 1000.0,
                "last_sample_date": "2025-03-13",
                "valid_until": "2025-04-13",
            },
        ]
        contract = {
            "ticker": "C",
            "current_lots": -22,
            "target_lots": -22,
            "lots_delta": 0,
            "final_action": "hold",
        }

        rows, audit = _audit_frozen_step4_pm_memory(
            contract=contract,
            alpha_setup_action_values=existing_rows,
        )

        self.assertEqual(
            [row["id"] for row in rows],
            ["c-short-hold", "c-old-long-open", "c-old-execution"],
        )
        self.assertFalse(audit["late_retrieval_performed"])
        self.assertEqual(audit["late_action_value_append_count"], 0)
        self.assertEqual(audit["lifecycle_matching_row_count"], 1)
        rejected_ids = {row["id"] for row in audit["rejected_action_values"]}
        self.assertIn("c-old-long-open", rejected_ids)
        self.assertIn("c-old-execution", rejected_ids)

    def test_pm_action_value_retrieval_real_history_not_blocked_by_empty_lane(self):
        class FakeDB:
            def __init__(self):
                self.calls = []

            def get_alpha_setup_action_values(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("setup_type") == "execution_pullback_setup":
                    return []
                if kwargs.get("horizon_class") == "medium" and kwargs.get("setup_type") is None:
                    return [
                        {
                            "scope_key": "BU|short|medium|choppy|execution_breakout_setup|execution",
                            "ticker": "BU",
                            "side": "short",
                            "horizon_class": "medium",
                            "market_regime": "choppy",
                            "setup_type": "execution_breakout_setup",
                            "action_name": "execution",
                            "canonical_action_value": True,
                            "canonical_action_family": "execution",
                            "action_value_lane": "execution",
                            "learning_lane": "execution",
                            "consumer_scope": "pm_learning",
                        },
                        {
                            "scope_key": "BU|short|medium|choppy|news_event_setup|open",
                            "ticker": "BU",
                            "side": "short",
                            "horizon_class": "medium",
                            "market_regime": "choppy",
                            "setup_type": "news_event_setup",
                            "action_name": "open",
                            "canonical_action_value": True,
                            "canonical_action_family": "open_add_new_risk",
                            "action_value_lane": "open",
                            "learning_lane": "open",
                            "consumer_scope": "pm_learning",
                        },
                    ]
                if kwargs.get("horizon_class") is None and kwargs.get("setup_type") is None:
                    return [
                        {
                            "id": "bu-real-execution",
                            "scope_key": "BU|short|short|choppy|execution_pullback_setup|execution",
                            "ticker": "BU",
                            "side": "short",
                            "horizon_class": "short",
                            "market_regime": "choppy",
                            "setup_type": "execution_pullback_setup",
                            "action_name": "execution",
                            "canonical_action_value": True,
                            "canonical_action_family": "execution",
                            "action_value_lane": "execution",
                            "learning_lane": "execution",
                            "consumer_scope": "pm_learning",
                            "action_preference": "positive_candidate_execution",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "reward_sum": 5581.76,
                            "reward_mean": 5581.76,
                            "win_rate": 1.0,
                            "sample_count": 1,
                            "last_sample_date": "2025-03-04",
                        },
                        {
                            "id": "bu-real-open",
                            "scope_key": "BU|short|short|choppy|news_event_setup|open",
                            "ticker": "BU",
                            "side": "short",
                            "horizon_class": "short",
                            "market_regime": "choppy",
                            "setup_type": "news_event_setup",
                            "action_name": "open",
                            "canonical_action_value": True,
                            "canonical_action_family": "open_add_new_risk",
                            "action_value_lane": "open",
                            "learning_lane": "open",
                            "consumer_scope": "pm_learning",
                            "action_preference": "positive_candidate_open",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "reward_sum": 5581.76,
                            "reward_mean": 5581.76,
                            "win_rate": 1.0,
                            "sample_count": 1,
                            "last_sample_date": "2025-03-04",
                        },
                    ]
                return []

        db = FakeDB()
        result = retrieve_pm_memory(
            db=db,
            config_id="cfg",
            ticker="BU",
            side="short",
            horizon_class="medium",
            market_regime="choppy",
            setup_type="execution_pullback_setup",
            trading_date="2025-03-05",
        )
        rows = result["action_values"]
        detail = result["effective_memory_summary"]
        attempts = result["retrieval_attempts"]

        ids = [row.get("id") for row in rows]
        self.assertIn("bu-real-execution", ids)
        self.assertIn("bu-real-open", ids)
        self.assertNotIn("bu-empty-execution", ids)
        self.assertNotIn("bu-empty-open", ids)
        real_execution = next(row for row in rows if row.get("id") == "bu-real-execution")
        self.assertEqual(real_execution["action_preference"], "positive_candidate_execution")
        self.assertEqual(real_execution["retrieval_match_level"], "same_ticker_side")
        self.assertTrue(detail["empty_history_cannot_block_real_history"])
        self.assertGreaterEqual(detail["effective_row_count"], 2)
        self.assertEqual(attempts[1]["row_count"], 2)
        self.assertEqual(attempts[2]["row_count"], 2)

    def test_scorecard_ignores_non_pm_learning_scope(self):
        signal = self._tradeable_signal()
        pm_row = self._action_value(
            action_preference="tail_loss_protect",
            lane="open",
            reward_mean=-1800.0,
            reward_sum=-7200.0,
            worst_reward=-2400.0,
        )
        trader_row = {
            **pm_row,
            "consumer_scope": "trader_execution_learning",
            "payload": {
                **pm_row.get("payload", {}),
                "consumer_scope": "trader_execution_learning",
            },
        }
        pm_scorecard = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[pm_row],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )
        trader_scope_scorecard = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[trader_row],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )

        self.assertLess(pm_scorecard["short"]["opportunity_score_components"]["negative_learning"], 0.0)
        self.assertEqual(
            trader_scope_scorecard["short"]["opportunity_score_components"]["negative_learning"],
            0.0,
        )

    def test_pm_scorecard_rejects_compressed_trace_as_learning_but_uses_canonical_row(self):
        signal = self._tradeable_signal()
        compressed_trace = {
            "ticker": "TA",
            "side": "short",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "trend_breakout_setup",
            "action_name": "open",
            "sample_count": 4,
            "confidence_score": 0.70,
            "valid_until": "2025-04-01",
            "signal_calibration": {"source_action_value_lane": "open"},
        }
        canonical_tail_loss = {
            **compressed_trace,
            "canonical_action_value": True,
            "canonical_action_family": "open_add_new_risk",
            "action_value_lane": "open",
            "learning_lane": "open",
            "consumer_scope": "pm_learning",
            "reward_mean": -1800.0,
            "reward_sum": -7200.0,
            "worst_reward": -2400.0,
            "action_preference": "tail_loss_protect",
            "last_sample_date": "2025-03-14",
            "payload": {
                "action_preference": "tail_loss_protect",
                "reward_source": "trade_episode",
                "mean_return_on_notional": -0.018,
                "worst_return_on_notional": -0.024,
                "episode_return_on_notional_count": 4,
                "amplification_scope_quality": "exact_real_state",
                "action_value_lane": "open",
                "real_trade_reward_count": 4,
            },
        }

        compressed_scorecard = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[compressed_trace],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )
        canonical_scorecard = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[canonical_tail_loss],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )
        explicitly_noncanonical = {
            **canonical_tail_loss,
            "canonical_action_value": False,
        }
        noncanonical_scorecard = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[explicitly_noncanonical],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )

        self.assertEqual(
            compressed_scorecard["short"]["opportunity_score_components"]["negative_learning"],
            0.0,
        )
        self.assertLess(
            canonical_scorecard["short"]["opportunity_score_components"]["negative_learning"],
            0.0,
        )
        self.assertLess(
            sum(
                canonical_scorecard["short"]["opportunity_score_components"].values()
            ),
            sum(
                compressed_scorecard["short"]["opportunity_score_components"].values()
            ),
        )
        self.assertEqual(
            noncanonical_scorecard["short"]["opportunity_score_components"]["negative_learning"],
            0.0,
        )

    def test_execution_profile_learning_is_a_score_component_not_trader_authority(self):
        signal = self._tradeable_signal()
        positive_execution = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[
                self._action_value(
                    action_preference="positive_candidate_execution",
                    lane="execution",
                    reward_mean=1200.0,
                    reward_sum=4800.0,
                )
            ],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )
        negative_execution = build_opportunity_scorecard(
            ticker="TA",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.70},
            data_quality_summary={},
            alpha_setup_action_values=[
                self._action_value(
                    action_preference="negative_revalidate",
                    lane="execution",
                    reward_mean=-1200.0,
                    reward_sum=-4800.0,
                    worst_reward=-1600.0,
                )
            ],
            decision_date="2025-03-15",
            config=self._scorecard_config(),
        )

        self.assertGreater(
            positive_execution["short"]["opportunity_score_components"]["execution_profile_learning"],
            0.0,
        )
        self.assertLess(
            negative_execution["short"]["opportunity_score_components"]["execution_profile_learning"],
            0.0,
        )
        self.assertNotIn("target_lots", positive_execution["short"])
        self.assertNotIn("lots_delta", positive_execution["short"])

    def test_recent_tail_loss_offsets_stale_alpha_profile_bonus(self):
        signal = self._tradeable_signal()
        common = {
            "ticker": "TA",
            "analyst_signals": [signal],
            "market_confirmation": {"confirmation_score": 0.70},
            "data_quality_summary": {},
            "alpha_setup_profiles": [
                {
                    "side": "short",
                    "setup_type": "trend_breakout_setup",
                    "lifecycle_state": "deployable",
                    "sample_count": 8,
                    "net_pnl": 9000.0,
                    "confidence_score": 0.82,
                }
            ],
            "decision_date": "2025-03-15",
            "config": self._scorecard_config(),
        }
        scorecard = build_opportunity_scorecard(
            **common,
            alpha_setup_action_values=[
                self._action_value(
                    action_preference="tail_loss_protect",
                    lane="open",
                    reward_mean=-2500.0,
                    reward_sum=-5000.0,
                    worst_reward=-3200.0,
                    last_sample_date="2025-03-14",
                    sample_count=2,
                )
            ],
        )

        components = scorecard["short"]["opportunity_score_components"]
        self.assertLess(components["recent_tail_loss_penalty"], 0.0)
        summary = scorecard["short"]["learning_adjustment_summary"]
        self.assertGreater(summary["recent_tail_loss_signal"], 0.0)
        self.assertEqual(summary["not_trade_authority"], True)

    def test_positive_mean_with_negative_worst_return_keeps_positive_signal_and_adds_tail_penalty(self):
        signal = self._tradeable_signal()
        common = {
            "ticker": "TA",
            "analyst_signals": [signal],
            "market_confirmation": {"confirmation_score": 0.70},
            "data_quality_summary": {},
            "decision_date": "2025-03-15",
            "config": self._scorecard_config(),
        }
        mixed_history = self._action_value(
            action_preference="positive_candidate_open",
            lane="open",
            reward_mean=4200.0,
            reward_sum=8400.0,
            mean_return_on_notional=0.008977,
            worst_return_on_notional=-0.001146,
            sample_count=2,
        )
        all_positive_history = self._action_value(
            action_preference="positive_candidate_open",
            lane="open",
            reward_mean=4200.0,
            reward_sum=8400.0,
            mean_return_on_notional=0.008977,
            worst_return_on_notional=0.001146,
            sample_count=2,
        )

        mixed_scorecard = build_opportunity_scorecard(
            **common,
            alpha_setup_action_values=[mixed_history],
        )
        all_positive_scorecard = build_opportunity_scorecard(
            **common,
            alpha_setup_action_values=[all_positive_history],
        )

        mixed_components = mixed_scorecard["short"]["opportunity_score_components"]
        mixed_summary = mixed_scorecard["short"]["learning_adjustment_summary"]
        self.assertGreater(mixed_components["positive_learning"], 0.0)
        self.assertEqual(mixed_components["negative_learning"], 0.0)
        self.assertLess(mixed_components["recent_tail_loss_penalty"], 0.0)
        self.assertGreater(mixed_summary["positive_learning_signal"], 0.0)
        self.assertEqual(mixed_summary["negative_learning_signal"], 0.0)
        self.assertGreater(mixed_summary["recent_tail_loss_signal"], 0.0)
        self.assertLess(
            sum(mixed_components.values()),
            sum(all_positive_scorecard["short"]["opportunity_score_components"].values()),
        )


class Phase1RecommendationSnapshotRegressionTest(unittest.TestCase):
    class _PMTestDB:
        def get_ticker_performance(self, **kwargs):
            return {}

        def get_futures_transaction_memory(self, *args, **kwargs):
            return []

        def get_alpha_setup_action_values(self, **kwargs):
            if str(kwargs.get("setup_type") or "") != "breakdown_setup":
                return []
            return [
                {
                    "id": "bu-short-exact-open",
                    "ticker": "BU",
                    "side": "short",
                    "horizon_class": kwargs.get("horizon_class") or "short",
                    "market_regime": kwargs.get("market_regime") or "trend",
                    "setup_type": "breakdown_setup",
                    "action_name": "open",
                    "canonical_action_value": True,
                    "canonical_action_family": "open_add_new_risk",
                    "action_value_lane": "open",
                    "learning_lane": "open",
                    "consumer_scope": "pm_learning",
                    "action_preference": "positive_candidate_open",
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                    "reward_sum": 5200.0,
                    "reward_mean": 5200.0,
                    "mean_return_on_notional": 0.052,
                    "worst_return_on_notional": 0.052,
                    "sample_count": 3,
                    "last_sample_date": "2025-02-28",
                }
            ]

        def get_similar_alpha_setup_action_values(self, **kwargs):
            return [
                {
                    "id": "bu-short-similar-negative",
                    "ticker": "BU",
                    "side": "short",
                    "horizon_class": kwargs.get("horizon_class") or "short",
                    "market_regime": kwargs.get("market_regime") or "trend",
                    "setup_type": "similar_breakdown_setup",
                    "action_name": "open",
                    "canonical_action_value": True,
                    "canonical_action_family": "open_add_new_risk",
                    "action_value_lane": "open",
                    "learning_lane": "open",
                    "consumer_scope": "pm_learning",
                    "action_preference": "negative_revalidate",
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                    "reward_sum": -15000.0,
                    "reward_mean": -15000.0,
                    "mean_return_on_notional": -0.15,
                    "worst_return_on_notional": -0.15,
                    "sample_count": 6,
                    "last_sample_date": "2025-02-28",
                }
            ]

    class _PMNoLearningDB(_PMTestDB):
        def get_alpha_setup_action_values(self, **kwargs):
            return []

        def get_similar_alpha_setup_action_values(self, **kwargs):
            return []

    class _PMExactLearningFailureDB(_PMTestDB):
        def get_alpha_setup_action_values(self, **kwargs):
            if kwargs.get("setup_type"):
                raise RuntimeError("simulated exact setup retrieval failure")
            return []

    def test_pm_exact_learning_failure_is_not_disguised_as_cold_start(self):
        portfolio = Portfolio(
            id="portfolio-prev",
            cashflow=5_000_000.0,
            account_equity=5_000_000.0,
            cash_available=5_000_000.0,
            margin_available=5_000_000.0,
            positions={},
        )
        signals = []
        for index, analyst in enumerate(
            ("technical", "fundamental", "commodity_news"),
            start=1,
        ):
            directional = analyst == "technical"
            aec = build_test_aec(
                analyst,
                ticker="BU",
                trading_date="2025-03-03",
                signal="Bearish" if directional else "Neutral",
                side="short" if directional else "flat",
                confidence=0.72 if directional else 0.35,
                opportunity_state="watch_for_trigger" if directional else "no_opportunity",
                setup_type="breakdown_setup" if directional else "no_trade",
                setup_quality_ok=directional,
                trigger_valid=False,
                current_trigger_confirmed=False,
                invalidation_present=directional,
                entry_trigger=None if directional else "",
                invalidation_condition="15m close above 3520" if directional else None,
                extra=(
                    {
                        "invalidation_level": 3520.0,
                        "position_invalidation_level": 3540.0,
                    }
                    if directional
                    else None
                ),
            )
            signals.append(
                AnalystSignal(
                    agent_name=analyst,
                    signal=Signal.BEARISH if directional else Signal.NEUTRAL,
                    confidence=0.72 if directional else 0.35,
                    opportunity_state="watch_for_trigger" if directional else "no_opportunity",
                    setup_type="breakdown_setup" if directional else "no_trade",
                    entry_trigger=aec["entry_trigger"],
                    trigger_valid=False,
                    invalidation_present=directional,
                    metadata={
                        "signal_record_id": f"signal-failure-{index}",
                        "action_evidence_contract": aec,
                    },
                )
            )
        full_config = {
            "cashflow": 5_000_000.0,
            "max_total_margin_ratio": 0.20,
            "max_single_margin_ratio": 0.12,
            "learning": {"enabled": False},
            "pm_risk_gate": {"enabled": False},
        }
        state = {
            "portfolio": portfolio,
            "ticker": "BU",
            "trading_date": datetime(2025, 3, 3),
            "analyst_signals": signals,
            "num_tickers": 1,
            "enabled_analysts": ["technical", "fundamental", "commodity_news"],
            "config_id": "cfg",
            "phase": TradingPhase.PHASE1,
            "morning_price_context": SimpleNamespace(
                base_price=3500.0,
                base_price_source="t_minus_1_close_fallback",
                base_price_date="2025-02-28",
                open_price=None,
                prev_close_price=3500.0,
                warning_message=None,
                contract_code="BU2506",
                contract_facts={
                    "contract_code": "BU2506",
                    "underlying_code": "BU",
                    "as_of_date": "2025-02-28",
                    "source": "test_visible_contract",
                },
            ),
            "config": full_config,
            "full_config": full_config,
            "router": None,
        }
        state.update(signal_collector_agent(state))

        with patch(
            "agents.decision_team.portfolio_manager.get_db",
            return_value=self._PMExactLearningFailureDB(),
        ), patch(
            "agents.decision_team.portfolio_manager.FuturesContractInfoCache.get_contract_info",
            return_value={
                "contract_multiplier": 10.0,
                "margin_rate_long": 0.10,
                "margin_rate_short": 0.10,
            },
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "BU: pm_exact_setup_learning_retrieval_failed",
            ):
                portfolio_agent_futures(state)

    def test_canonical_watch_survives_pm_prose_and_reaches_nonzero_conditional_fac(self):
        portfolio = Portfolio(
            id="portfolio-prev",
            cashflow=5_000_000.0,
            account_equity=5_000_000.0,
            cash_available=5_000_000.0,
            margin_available=5_000_000.0,
            positions={},
        )
        signals = []
        for index, analyst in enumerate(
            ("technical", "fundamental", "commodity_news"),
            start=1,
        ):
            directional = analyst == "technical"
            aec = build_test_aec(
                analyst,
                ticker="BU",
                trading_date="2025-03-03",
                signal="Bearish" if directional else "Neutral",
                side="short" if directional else "flat",
                confidence=0.72 if directional else 0.35,
                opportunity_state="watch_for_trigger" if directional else "no_opportunity",
                setup_type="breakdown_setup" if directional else "no_trade",
                setup_quality_ok=directional,
                trigger_valid=False,
                current_trigger_confirmed=False,
                invalidation_present=directional,
                entry_trigger=None if directional else "",
                invalidation_condition=(
                    "15m close above 3520"
                    if directional
                    else None
                ),
                extra=(
                    {
                        "invalidation_level": 3520.0,
                        "position_invalidation_level": 3540.0,
                    }
                    if directional
                    else None
                ),
            )
            signals.append(
                AnalystSignal(
                    agent_name=analyst,
                    signal=Signal.BEARISH if directional else Signal.NEUTRAL,
                    confidence=0.72 if directional else 0.35,
                    opportunity_state=(
                        "watch_for_trigger" if directional else "no_opportunity"
                    ),
                    setup_type="breakdown_setup" if directional else "no_trade",
                    entry_trigger=aec["entry_trigger"],
                    trigger_valid=False,
                    invalidation_present=directional,
                    metadata={
                        "signal_record_id": f"signal-{index}",
                        "action_evidence_contract": aec,
                    },
                )
            )
        full_config = {
            "cashflow": 5_000_000.0,
            "max_total_margin_ratio": 0.20,
            "max_single_margin_ratio": 0.12,
            "learning": {"enabled": False},
            "pm_risk_gate": {"enabled": False},
            "portfolio_manager": {
                "holding_rebalance_control": {
                    "watch_for_trigger_new_entry": {
                        "enabled": True,
                        "allow_probe": True,
                        "probe_max_ratio": 0.01,
                        "probe_floor_ratio": 0.005,
                    },
                },
            },
        }
        state = {
            "portfolio": portfolio,
            "ticker": "BU",
            "trading_date": datetime(2025, 3, 3),
            "analyst_signals": signals,
            "num_tickers": 1,
            "enabled_analysts": [
                "technical",
                "fundamental",
                "commodity_news",
            ],
            "config_id": "cfg",
            "phase": TradingPhase.PHASE1,
            "morning_price_context": SimpleNamespace(
                base_price=3500.0,
                base_price_source="t_minus_1_close_fallback",
                base_price_date="2025-02-28",
                open_price=None,
                prev_close_price=3500.0,
                warning_message=None,
                contract_code="BU2506",
                contract_facts={
                    "contract_code": "BU2506",
                    "underlying_code": "BU",
                    "as_of_date": "2025-02-28",
                    "source": "test_visible_contract",
                },
            ),
            "config": full_config,
            "full_config": full_config,
            "router": None,
        }
        state.update(signal_collector_agent(state))

        with patch(
            "agents.decision_team.portfolio_manager.get_db",
            return_value=self._PMTestDB(),
        ), patch(
            "agents.decision_team.portfolio_manager._sanitize_visible_text",
            return_value=(
                "No new position is warranted before the canonical 15m trigger confirms."
            ),
        ), patch(
            "agents.decision_team.portfolio_manager.FuturesContractInfoCache.get_contract_info",
            return_value={
                "contract_multiplier": 10.0,
                "margin_rate_long": 0.10,
                "margin_rate_short": 0.10,
            },
        ):
            pm_result = portfolio_agent_futures(state)

        with patch(
            "agents.decision_team.portfolio_manager.get_db",
            return_value=self._PMNoLearningDB(),
        ), patch(
            "agents.decision_team.portfolio_manager._sanitize_visible_text",
            return_value=(
                "No new position is warranted before the canonical 15m trigger confirms."
            ),
        ), patch(
            "agents.decision_team.portfolio_manager.FuturesContractInfoCache.get_contract_info",
            return_value={
                "contract_multiplier": 10.0,
                "margin_rate_long": 0.10,
                "margin_rate_short": 0.10,
            },
        ):
            no_learning_pm_result = portfolio_agent_futures(state)

        signed = finalize_pm_full_market_contracts(
            generated=[("BU", pm_result["pm_state"])],
            config=full_config,
            portfolio=portfolio,
        )
        no_learning_signed = finalize_pm_full_market_contracts(
            generated=[("BU", no_learning_pm_result["pm_state"])],
            config=full_config,
            portfolio=portfolio,
        )
        fac = signed[0][1].signal_snapshot["final_action_contract"]
        no_learning_fac = no_learning_signed[0][1].signal_snapshot["final_action_contract"]

        scorecard = pm_result["pm_state"]["opportunity_scorecard"]
        short_row = scorecard["short"]
        self.assertEqual(scorecard["preferred_side"], "short")
        self.assertEqual(short_row["side_priority"], 1)
        self.assertGreater(short_row["action_value_learning_summary"]["positive_count"], 0)
        self.assertEqual(short_row["action_value_learning_summary"]["negative_count"], 0)
        self.assertGreater(short_row["rank_score_components"]["open_add_action_value_delta"], 0.0)
        no_learning_short_row = no_learning_pm_result["pm_state"]["opportunity_scorecard"]["short"]
        self.assertEqual(no_learning_short_row["action_value_learning_summary"]["positive_count"], 0)
        self.assertEqual(no_learning_short_row["action_value_learning_summary"]["negative_count"], 0)
        self.assertGreater(short_row["candidate_quality"], no_learning_short_row["candidate_quality"])
        self.assertGreater(short_row["rank_score"], no_learning_short_row["rank_score"])

        self.assertLess(fac["target_lots"], 0, fac)
        self.assertLess(no_learning_fac["target_lots"], 0, no_learning_fac)
        self.assertEqual(no_learning_fac["learning_used"]["alpha_setup_action_values"], [])
        self.assertTrue(fac["conditional_trigger_authority"])
        self.assertTrue(fac["requires_intraday_confirmation"])
        self.assertFalse(fac["can_execute_without_intraday_trigger"])
        self.assertNotIn(
            "bu-short-similar-negative",
            {row.get("id") for row in fac["learning_used"]["alpha_setup_action_values"]},
        )
        self.assertIn(
            "bu-short-similar-negative",
            {
                row.get("id")
                for row in fac["learning_used"]["memory_retrieval"].get(
                    "rejected_or_downgraded", []
                )
            },
        )

        from agents.decision_team.auditor import audit_futures_recommendation
        from tools.common.signal_evidence_collection import build_scc_data_quality_summary

        recommendation = signed[0][1]
        recommendation.id = "rec-watch"
        audit = audit_futures_recommendation(
            recommendation=recommendation.model_dump(),
            hard_risk_config={"max_total_margin_ratio": 0.20},
            account_state={
                "account_equity": 5_000_000.0,
                "margin_used": 0.0,
                "margin_ratio": 0.0,
                "risk_status": "NORMAL",
            },
            position_state={
                "ticker": "BU",
                "current_lots": 0,
                "contract_code": None,
                "margin_used": 0.0,
                "margin_rate": 0.10,
                "contract_multiplier": 10.0,
            },
            contract_state={
                "contract_code": "BU2506",
                "underlying_code": "BU",
                "as_of_date": "2025-02-28",
                "source": "test_visible_contract",
            },
            data_quality=build_scc_data_quality_summary(
                recommendation.signal_snapshot["signal_collection_contract"]
            ),
        )
        self.assertEqual(audit.audit_verdict, "approve")

        one_minute_bars = [
            {
                "datetime": "2025-03-03 09:30:00",
                "open": 3500.0,
                "high": 3510.0,
                "low": 3490.0,
                "close": 3500.0,
                "volume": 10,
            },
            {
                "datetime": "2025-03-03 09:31:00",
                "open": 3500.0,
                "high": 3505.0,
                "low": 3490.0,
                "close": 3495.0,
                "volume": 10,
            },
            {
                "datetime": "2025-03-03 10:01:00",
                "open": 3478.0,
                "high": 3482.0,
                "low": 3470.0,
                "close": 3475.0,
                "volume": 10,
            },
            {
                "datetime": "2025-03-03 10:02:00",
                "open": 3475.0,
                "high": 3478.0,
                "low": 3468.0,
                "close": 3470.0,
                "volume": 10,
            },
            {
                "datetime": "2025-03-03 10:03:00",
                "open": 3470.0,
                "high": 3473.0,
                "low": 3465.0,
                "close": 3468.0,
                "volume": 10,
            },
        ]
        untriggered = select_intraday_execution(
            signal_bars=[
                {
                    "datetime": "2025-03-03 10:00:00",
                    "open": 3500.0,
                    "high": 3505.0,
                    "low": 3495.0,
                    "close": 3500.0,
                    "volume": 10,
                }
            ],
            execution_bars=one_minute_bars,
            action="open_short",
            config={"opening_range_minutes": 2, "min_execution_volume": 1},
            decision_context={"execution_contract": fac},
            finalize_untriggered=True,
        )
        self.assertFalse(untriggered.should_execute)
        self.assertEqual(untriggered.reason, "intraday_trigger_not_met")

        triggered = select_intraday_execution(
            signal_bars=[
                {
                    "datetime": "2025-03-03 10:00:00",
                    "open": 3490.0,
                    "high": 3492.0,
                    "low": 3478.0,
                    "close": 3480.0,
                    "volume": 10,
                },
                {
                    "datetime": "2025-03-03 10:01:00",
                    "open": 3478.0,
                    "high": 3482.0,
                    "low": 3470.0,
                    "close": 3475.0,
                    "volume": 10,
                },
                {
                    "datetime": "2025-03-03 10:02:00",
                    "open": 3475.0,
                    "high": 3478.0,
                    "low": 3468.0,
                    "close": 3470.0,
                    "volume": 10,
                },
            ],
            execution_bars=one_minute_bars,
            action="open_short",
            config={"opening_range_minutes": 2, "min_execution_volume": 1},
            decision_context={"execution_contract": fac},
            finalize_untriggered=True,
        )
        self.assertTrue(triggered.should_execute)
        self.assertEqual(triggered.base_datetime, "2025-03-03 10:03:00")

    def test_existing_short_unchanged_is_hold_not_open_short(self):
        intent = recommendation_intent_from_lots(current_lots=-1, target_lots=-1)

        self.assertEqual(intent["action"], "hold")
        self.assertEqual(intent["lots"], 0)
        self.assertEqual(intent["action_type"], "keep")

    def test_existing_short_increase_uses_delta_open_short(self):
        intent = recommendation_intent_from_lots(current_lots=-1, target_lots=-3)

        self.assertEqual(intent["action"], "open_short")
        self.assertEqual(intent["lots"], 2)
        self.assertEqual(intent["action_type"], "increase")

    def test_phase2_execution_consistency_matches_target_delta(self):
        intent = phase2_order_intent_from_lots(current_lots=2, target_lots=-3)
        diagnostics = build_lot_intent_consistency(
            current_lots=2,
            target_lots=-3,
            action="close_long",
            lots=5,
            mode="phase2_execution",
        )

        self.assertEqual(intent["action"], "close_long")
        self.assertEqual(intent["lots"], 5)
        self.assertTrue(intent["requires_two_step_reversal"])
        self.assertEqual(diagnostics["status"], "ok")

    def test_phase1_recommendation_uses_actual_analyst_signal_artifacts(self):
        portfolio = Portfolio(id="portfolio-1", cashflow=1_000_000, positions={})
        decision = FuturesDecision(ticker="BU", action=FuturesAction.HOLD, lots=0, justification="hold")
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.5),
            AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.6),
        ]

        recommendation = _build_signed_pm_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="BU",
            trading_date="2025-01-02",
            contract_code="BU2506.SHF",
            decision=decision,
            morning_price_context=None,
            analyst_signals=signals,
            plan_snapshot={"decision_horizon": "medium", "validation_horizon": "medium"},
        )

        self.assertNotIn("artifact_contract", recommendation.signal_snapshot)
        self.assertNotIn("horizon_scope", recommendation.signal_snapshot)

    def test_release_block_diagnostics_are_observation_only_and_do_not_mutate_contract(self):
        contract = _build_final_action_contract(
            ticker="RB",
            current_lots=0,
            target_lots=0,
            position_ratio=0.0,
            margin_required=0.0,
            account_equity=1_000_000.0,
            lots_to_trade=0,
            lots_to_trade_reason="market_confirmation_below_release_threshold",
            recommendation_intent=recommendation_intent_from_lots(0, 0),
            final_entry_authority={
                "authority_type": "watchlist_only",
                "open_action_evidence": False,
                "watch_for_trigger_block": False,
            },
            control_reasons=["market_confirmation_below_release_threshold"],
            control_diagnostics={},
            opportunity_scorecard={
                "preferred_side": "short",
                "short": {"final_state": "tradeable_candidate", "score": 0.71},
            },
            market_confirmation={"confirmation_score": 0.54, "status": "weak"},
            alpha_setup_action_values=[],
            execution_contract_fields={},
        )
        before = json.loads(json.dumps(contract, sort_keys=True))

        diagnostics = _build_release_block_diagnostics(
            ticker="RB",
            decision_state={
                "current_lots": contract["current_lots"],
                "target_lots": contract["target_lots"],
            },
            final_entry_authority={
                "authority_type": "watchlist_only",
                "open_action_evidence": False,
                "watch_for_trigger_block": False,
            },
            control_reasons=["market_confirmation_below_release_threshold"],
            lots_to_trade_reason="market_confirmation_below_release_threshold",
            control_diagnostics={},
            opportunity_scorecard={
                "preferred_side": "short",
                "short": {"final_state": "tradeable_candidate", "score": 0.71},
            },
            market_confirmation={"confirmation_score": 0.54, "status": "weak"},
            full_config={
                "position_budget_policy": {
                    "probe_margin_ratio": 0.008,
                    "probe_margin_max_ratio": 0.015,
                    "min_real_trade_margin_ratio": 0.02,
                },
                "portfolio": {
                    "alpha_setup_ev_fusion": {
                        "min_confirmation_score": 0.70,
                        "require_tradeable_support_for_release": True,
                        "require_invalidation_for_release": True,
                    }
                },
            },
        )

        self.assertEqual(contract, before)
        self.assertTrue(diagnostics["observation_only"])
        self.assertTrue(diagnostics["does_not_modify_trade_authority"])
        self.assertEqual(diagnostics["single_source_of_trade_truth_remains"], "final_action_contract")
        serialized = json.dumps(diagnostics, sort_keys=True)
        for forbidden in (
            "authority_type",
            "target_lots",
            "lots_delta",
            "target_position_ratio",
            "open_action_evidence",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_phase1_recommendation_carries_release_diagnostics_outside_final_contract(self):
        portfolio = Portfolio(id="portfolio-1", cashflow=1_000_000, positions={})
        decision = FuturesDecision(ticker="BU", action=FuturesAction.HOLD, lots=0, justification="hold")
        diagnostics = {
            "contract_version": "agentquant.release_block_diagnostics.v1",
            "observation_only": True,
            "does_not_modify_trade_authority": True,
            "single_source_of_trade_truth_remains": "final_action_contract",
            "primary_block_reason": "market_confirmation_below_release_threshold",
        }
        final_contract = _build_final_action_contract(
            ticker="BU",
            current_lots=0,
            target_lots=0,
            position_ratio=0.0,
            margin_required=0.0,
            account_equity=1_000_000.0,
            lots_to_trade=0,
            lots_to_trade_reason="market_confirmation_below_release_threshold",
            recommendation_intent=recommendation_intent_from_lots(0, 0),
            final_entry_authority={"authority_type": "watchlist_only"},
            control_reasons=["market_confirmation_below_release_threshold"],
            control_diagnostics={},
            opportunity_scorecard={},
            market_confirmation={},
            alpha_setup_action_values=[],
            execution_contract_fields={},
        )

        recommendation = _build_signed_pm_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="BU",
            trading_date="2025-01-02",
            contract_code="BU2506.SHF",
            decision=decision,
            morning_price_context=None,
            analyst_signals=[AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.5)],
            plan_snapshot={
                "decision_horizon": "short",
                "validation_horizon": "short",
                "release_block_diagnostics": diagnostics,
            },
            pm_state_update=_pm_state_fixture(final_contract, ticker="BU"),
        )

        snapshot = recommendation.signal_snapshot
        self.assertNotIn("release_block_diagnostics", snapshot)
        self.assertNotIn("release_block_diagnostics", snapshot["final_action_contract"])

    def test_phase1_recommendation_final_contract_is_explicit_not_pm_draft(self):
        portfolio = Portfolio(id="portfolio-1", cashflow=1_000_000, positions={})
        decision = FuturesDecision(
            ticker="BU",
            action=FuturesAction.OPEN_SHORT,
            lots=1,
            price=3000.0,
            settle_price=3000.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="BU2506.SHF",
            justification="open from explicit final contract",
        )
        explicit_contract = {
            "contract_version": "agentquant.final_action.v1",
            "ticker": "BU",
            "final_action": "open_probe",
            "current_lots": 0,
            "target_lots": -1,
            "lots_delta": -1,
            "lots_delta_abs": 1,
            "reason_codes": "explicit_contract",
            "target_position_ratio": -0.01,
            "authority_type": "exploration_probe",
            "open_action_evidence": False,
            "strong_current_evidence": False,
            "open_action_evidence": True,
            "strong_current_evidence": True,
            "reason_codes": ["explicit_contract"],
            "consistency": {"status": "ok"},
            "single_source_of_trade_truth": True,
        }

        recommendation = _build_signed_pm_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="BU",
            trading_date="2025-01-02",
            contract_code="BU2506.SHF",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=3000.0,
                base_price_source=None,
                base_price_date="2025-01-02",
                open_price=3000.0,
                prev_close_price=3010.0,
                warning_message=None,
            ),
            analyst_signals=[],
            plan_snapshot={
                "target_lots": -8,
                "target_position_ratio": -0.08,
                "final_action_contract": {
                    "final_action": "open_real",
                    "current_lots": 0,
                    "target_lots": -8,
                    "lots_delta": -8,
                    "reason_codes": "stale_pm_draft",
                    "consistency": {"status": "stale"},
                },
                "recommendation_position_consistency": {"status": "stale"},
                "strategy_controls": {"diagnostics": {}},
            },
            pm_state_update=_pm_state_fixture(explicit_contract, ticker="BU"),
        )

        snapshot = recommendation.signal_snapshot
        final_contract = snapshot["final_action_contract"]
        self.assertEqual(final_contract["target_lots"], -1)
        self.assertIn("explicit_contract", final_contract["reason_codes"])
        self.assertNotIn("stale_pm_draft", recommendation.justification)
        self.assertNotIn("recommendation_position_consistency=stale", recommendation.justification)

    def test_phase1_dynamic_price_abnormal_hold_keeps_analyst_signal_snapshot(self):
        portfolio = Portfolio(
            id="portfolio-1",
            cashflow=1_000_000,
            account_equity=1_000_000,
            cash_available=1_000_000,
            positions={},
            margin_used=0.0,
            margin_available=1_000_000,
            margin_ratio=0.0,
        )
        signals = [
            AnalystSignal(
                agent_name="technical",
                signal=Signal.NEUTRAL,
                confidence=0.5,
                metadata={
                    "signal_record_id": "signal-J-technical",
                    "action_evidence_contract": build_test_aec(
                        "technical",
                        ticker="J",
                        trading_date="2025-05-09",
                        signal="Neutral",
                        side="flat",
                        confidence=0.5,
                    ),
                },
            ),
            AnalystSignal(
                agent_name="fundamental",
                signal=Signal.BEARISH,
                confidence=0.6,
                metadata={
                    "signal_record_id": "signal-J-fundamental",
                    "action_evidence_contract": build_test_aec(
                        "fundamental",
                        ticker="J",
                        trading_date="2025-05-09",
                        signal="Bearish",
                        side="short",
                        confidence=0.6,
                        opportunity_state="no_opportunity",
                        trigger_valid=False,
                        current_trigger_confirmed=False,
                        invalidation_present=False,
                        entry_trigger="",
                    ),
                },
            ),
            AnalystSignal(
                agent_name="commodity_news",
                signal=Signal.NEUTRAL,
                confidence=0.4,
                metadata={
                    "signal_record_id": "signal-J-commodity_news",
                    "action_evidence_contract": build_test_aec(
                        "commodity_news",
                        ticker="J",
                        trading_date="2025-05-09",
                        signal="Neutral",
                        side="flat",
                        confidence=0.4,
                    ),
                },
            ),
        ]
        state = {
            "portfolio": portfolio,
            "ticker": "J",
            "trading_date": datetime(2025, 5, 9),
            "analyst_signals": signals,
            "llm_config": {"provider": "fake", "model": "fake"},
            "num_tickers": 15,
            "enabled_analysts": ["technical", "fundamental", "commodity_news"],
            "config_id": "cfg",
            "phase": "phase1",
            "morning_price_context": SimpleNamespace(
                base_price=500.0,
                base_price_source="t_minus_1_close_fallback",
                base_price_date="2025-05-08",
                open_price=None,
                prev_close_price=1478.0,
                warning_message=None,
            ),
            "config": {"max_total_margin_ratio": 0.20, "max_single_margin_ratio": 0.12},
            "full_config": {
                "learning": {"enabled": False},
                "max_total_margin_ratio": 0.20,
                "max_single_margin_ratio": 0.12,
            },
            "router": None,
        }
        state.update(signal_collector_agent(state))

        with patch(
            "agents.decision_team.portfolio_manager.get_db",
            return_value=self._PMTestDB(),
        ):
            result = portfolio_agent_futures(state)

        pm_state = result["pm_state"]
        signed = finalize_pm_full_market_contracts(
            generated=[("J", pm_state)],
            config=state["full_config"],
            portfolio=portfolio,
        )
        recommendation = signed[0][1]
        snapshot = recommendation.signal_snapshot
        for analyst in ("technical", "fundamental", "commodity_news"):
            self.assertNotIn(analyst, snapshot)
        self.assertNotIn("artifact_contract", snapshot)
        self.assertNotIn("pm_internal_draft", snapshot)
        contract = snapshot["final_action_contract"]
        self.assertIn("data_price_anomaly", contract["reason_codes"])
        self.assertEqual(contract["target_lots"], 0)
        self.assertEqual(contract["lots_delta"], 0)
        self.assertIn("data_price_anomaly", recommendation.justification)


class _FailingPreviousCloseAPI:
    def get_futures_daily_candles_optimized(self, underlying_code, is_main, start_date, end_date):
        return [SimpleNamespace(trade_date="2025-03-07")]

    def get_main_contract_quote_on_date(self, underlying_code, trading_date):
        raise RuntimeError("Network error: refused")


class _MissingOpenWithPreviousCloseAPI:
    def get_futures_daily_candles_optimized(self, underlying_code, is_main, start_date, end_date):
        return [SimpleNamespace(trade_date="2025-03-07")]

    def get_main_contract_quote_on_date(self, underlying_code, trading_date):
        if str(trading_date).startswith("2025-03-10"):
            return SimpleNamespace(open_price=None, pre_close_price=3500.0, close_price=None)
        return SimpleNamespace(open_price=3490.0, pre_close_price=3480.0, close_price=3510.0)


class RouterProviderFailureRegressionTest(unittest.TestCase):
    def test_pre_open_reference_price_soft_skips_when_provider_refuses_connection(self):
        router = Router.__new__(Router)
        router.api = _FailingPreviousCloseAPI()
        router.market_type = "china_futures"
        router.config = {}

        basis = router.resolve_pre_open_reference_price("RB", "2025-03-10")

        self.assertIsNone(basis.base_price)
        self.assertIsNone(basis.base_price_source)
        self.assertIn("provider unavailable", basis.warning_message)
        self.assertIn("has no previous close available", basis.warning_message)

    def test_pre_open_reference_price_does_not_mask_exact_quote_failure_with_history(self):
        router = Router.__new__(Router)
        router.api = _FailingPreviousCloseAPI()
        router.market_type = "china_futures"
        router.config = {}

        basis = router.resolve_pre_open_reference_price("RB", "2025-03-10")

        self.assertIsNone(basis.base_price)
        self.assertIsNone(basis.prev_close_price)
        self.assertIsNone(basis.base_price_date)
        self.assertIn("previous close quote provider unavailable", basis.warning_message)
        self.assertNotIn("continuous daily previous close fallback", basis.warning_message)

    def test_phase2_execution_price_does_not_use_previous_close_when_open_missing(self):
        router = Router.__new__(Router)
        router.api = _MissingOpenWithPreviousCloseAPI()
        router.market_type = "china_futures"
        router.config = {}

        basis = router.resolve_morning_execution_base_price("RB", "2025-03-10")

        self.assertIsNone(basis.base_price)
        self.assertIsNone(basis.base_price_source)
        self.assertIsNone(basis.base_price_date)
        self.assertEqual(basis.prev_close_price, 3500.0)
        self.assertIn("missing T-day open price", basis.warning_message)
        self.assertNotIn("previous close fallback", basis.warning_message)


class PandaAIContractNormalizationRegressionTest(unittest.TestCase):
    def test_czce_short_and_full_contract_codes_match_same_quote_row(self):
        api = PandaAIAPI.__new__(PandaAIAPI)
        row = {
            "symbol": "CF2505.CZC",
            "trading_code": "CF505",
            "dominant_id": "CF2505",
        }

        self.assertTrue(
            api._row_matches_contract(row, "CF2505", reference_date=datetime(2024, 12, 31))
        )

    def test_token_expiry_reauthenticates_once_before_retry(self):
        # Deterministic unit test: the provider object is a fake and no real
        # PandaAI credential, token, or network call is used here.
        class _FakePandaData:
            def __init__(self):
                self.init_calls = 0
                self.market_calls = 0

            def init_token(self, username, password):
                self.init_calls += 1

            def get_market_data(self, **kwargs):
                self.market_calls += 1
                if self.market_calls == 1:
                    raise RuntimeError("service returned error: code=200004 token expired")
                return [{"symbol": "ZN_DOMINANT.SHF", "date": "20250102", "close": 25265.0}]

        fake = _FakePandaData()
        api = PandaAIAPI.__new__(PandaAIAPI)
        api.username = "user"
        api.password = "password"
        api._panda_data = fake
        api._token_initialized = True
        api._retry_initial_wait_seconds = 0.0
        api._retry_max_wait_seconds = 0.0
        api._network_retry_initial_wait_seconds = 0.0
        api._network_retry_max_wait_seconds = 0.0
        api._min_request_interval_seconds = 0.0
        original_shared = PandaAIAPI._shared_token_initialized
        PandaAIAPI._shared_token_initialized = True
        try:
            with patch("apis.pandaai.api.logger.warning") as warning_log:
                result = api._call_pandaai("get_market_data", symbol="ZN_DOMINANT.SHF")
        finally:
            PandaAIAPI._shared_token_initialized = original_shared

        self.assertEqual(result[0]["close"], 25265.0)
        self.assertEqual(fake.init_calls, 1)
        self.assertEqual(fake.market_calls, 2)
        warning_log.assert_called_once_with("pandaai_token_refresh_required")


class _FailingSettlementRouter:
    def get_futures_contract_quote_on_date(self, contract_code, trading_date):
        raise RuntimeError("HTTP 403 provider blocked")


class _RolloverSettlementRouter:
    def __init__(self, main_contracts):
        self.main_contracts = main_contracts

    def get_futures_main_contract_quote_on_date(self, ticker, trading_date):
        return SimpleNamespace(ticker=self.main_contracts[ticker])


class _RolloverSettlementDb:
    def __init__(self):
        self.saved = []

    def get_futures_recommendations_by_effective_date(self, **kwargs):
        return []

    def save_futures_recommendation(self, recommendation):
        self.saved.append(recommendation)
        return "rollover-rec"


class FuturesSettlementStrictPriceRegressionTest(unittest.TestCase):
    def test_settlement_price_provider_failure_does_not_fallback(self):
        engine = FuturesDailySettlement.__new__(FuturesDailySettlement)
        engine.router = _FailingSettlementRouter()

        ledgers = {
            "BU": {
                "batches": [
                    {
                        "contract_code": "BU2506.SHF",
                        "daily_reference_price": 4400.0,
                    }
                ]
            }
        }

        with self.assertRaisesRegex(RuntimeError, "HTTP 403 provider blocked"):
            engine._fetch_contract_settle_prices(
                ledgers=ledgers,
                transactions=[],
                trading_date=datetime(2025, 1, 6),
            )

    @patch("tools.agent_tools.execution.accountant_futures_settlement.get_next_trading_day")
    def test_same_canonical_contract_does_not_schedule_rollover(self, mock_next_day):
        mock_next_day.return_value = datetime(2025, 3, 4)
        for ticker, contract_code in (("BU", "BU2506"), ("SR", "SR2505")):
            with self.subTest(ticker=ticker):
                engine = FuturesDailySettlement.__new__(FuturesDailySettlement)
                engine.router = _RolloverSettlementRouter({ticker: contract_code})
                engine.db = _RolloverSettlementDb()
                portfolio = Portfolio(
                    id="pf",
                    cashflow=1000000.0,
                    margin_used=10000.0,
                    positions={
                        ticker: Position(
                            shares=2,
                            value=50000.0,
                            contract_code=contract_code,
                            margin_used=10000.0,
                        )
                    },
                )

                engine._detect_rollover_recommendations(
                    config_id="cfg",
                    portfolio=portfolio,
                    trading_date=datetime(2025, 3, 3),
                )

                self.assertEqual(engine.db.saved, [])

    @patch("tools.agent_tools.execution.accountant_futures_settlement.get_next_trading_day")
    def test_rollover_detected_after_settlement_is_scheduled_for_next_trading_day(self, mock_next_day):
        mock_next_day.return_value = datetime(2025, 3, 4)
        engine = FuturesDailySettlement.__new__(FuturesDailySettlement)
        engine.router = _RolloverSettlementRouter({"RB": "RB2601"})
        engine.db = _RolloverSettlementDb()

        portfolio = Portfolio(
            id="pf",
            cashflow=1000000.0,
            margin_used=10000.0,
            positions={
                "RB": Position(
                    shares=2,
                    value=50000.0,
                    contract_code="RB2505",
                    margin_used=10000.0,
                )
            },
        )

        engine._detect_rollover_recommendations(
            config_id="cfg",
            portfolio=portfolio,
            trading_date=datetime(2025, 3, 3),
        )

        self.assertEqual(len(engine.db.saved), 1)
        recommendation = engine.db.saved[0]
        self.assertEqual(recommendation.trading_date, "2025-03-03")
        self.assertEqual(recommendation.effective_trade_date, "2025-03-04")
        self.assertEqual(recommendation.source_type, RecommendationSourceType.ROLLOVER)
        self.assertEqual(recommendation.from_contract, "RB2505")
        self.assertEqual(recommendation.to_contract, "RB2601")


class FuturesAuditRegressionTest(unittest.TestCase):
    def test_classify_no_trade_reasons(self):
        self.assertEqual(classify_no_trade_reasons(["neutral_signal_no_trade", "position_matched"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["position_matched", "cooling_period"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["cold_start_small_cap"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["trade_frequency_control", "weak_signal_combo"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["market_confirmation_quality_gate"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["weak_ticker_side_quality_gate"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["news_only_directional_trade"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["strategy_memory_weak_block"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["pm_risk_gate_block"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["intraday_opening_range_incomplete"]), "expected")
        self.assertEqual(classify_no_trade_reasons(["neutral_signal_no_trade", "missing_previous_close"]), "error")
        self.assertEqual(classify_no_trade_reasons([]), "unknown")
        self.assertEqual(classify_no_trade_reasons(["intraday_trigger_not_met"]), "expected")

    def test_no_trade_reason_research_categories(self):
        self.assertEqual(categorize_no_trade_reason("neutral_signal_no_trade")["category"], "signal")
        self.assertEqual(categorize_no_trade_reason("drawdown_control")["category"], "risk")
        self.assertEqual(categorize_no_trade_reason("intraday_trigger_not_met")["category"], "timing")
        self.assertEqual(categorize_no_trade_reason("limit_locked_no_fill")["category"], "execution")
        self.assertEqual(categorize_no_trade_reason("position_matched")["category"], "business")
        self.assertEqual(categorize_no_trade_reason("strategy_memory_weak_block")["category"], "learning")

    def test_infer_no_trade_reason_from_warning(self):
        snapshot = {"execution_result": {"no_trade_reason": "position_matched"}}
        self.assertEqual(infer_no_trade_reason(snapshot), "position_matched")
        stale_draft_snapshot = {"pm_internal_draft": {"reason_codes": "position_matched"}}
        self.assertIsNone(infer_no_trade_reason(stale_draft_snapshot))
        self.assertEqual(
            infer_no_trade_reason({}, warning_message="RB has no previous close available before 2025-02-05"),
            "missing_previous_close",
        )

    def test_pm_risk_gate_reason_is_canonicalized(self):
        self.assertEqual(normalize_no_trade_reason("pm_risk_gate_block"), "pm_risk_gate_block")
        snapshot = {"execution_result": {"no_trade_reason": "pm_risk_gate_block"}}
        self.assertEqual(infer_no_trade_reason(snapshot), "pm_risk_gate_block")


class PMRiskGateRegressionTest(unittest.TestCase):
    def _auditor(self):
        return PMRiskGate(
            {
                "pm_risk_gate": {
                    "enabled": True,
                    "policy_version": "test_v1",
                    "learning_mode": "audit_only",
                    "attribution_feedback": {
                        "enabled": True,
                        "lookback_trades": 30,
                        "min_samples_soft": 5,
                        "min_samples_hard": 10,
                        "weak_win_rate_below": 0.40,
                        "severe_win_rate_below": 0.30,
                        "weak_total_pnl_below": -1500,
                        "severe_total_pnl_below": -5000,
                        "weak_combo_requires_confirmation_score": 0.60,
                        "block_severe_negative_combo": True,
                    },
                    "cold_start": {
                        "policy": "small_cap",
                        "max_position_ratio_multiplier": 0.50,
                        "block_weak_combo_new_entries": True,
                        "block_conflict_confirmation_below": 0.65,
                    },
                    "quality_gate": {
                        "enabled": True,
                        "min_supporting_analysts": 2,
                        "block_low_tradeability_count": 2,
                        "conflict_block_confirmation_below": 0.50,
                        "no_support_block_confirmation_below": 0.70,
                        "medium_quality_multiplier": 0.50,
                        "qualified_support_min_confidence": 0.45,
                        "protected_ticker_sides": {
                            "BU": {
                                "long": {
                                    "min_confirmation_score": 0.50,
                                    "weak_combo_multiplier": 0.50,
                                    "cold_start_multiplier": 0.50,
                                }
                            }
                        },
                        "weak_ticker_side_rules": {
                            "P": {
                                "long": {
                                    "min_confirmation_score": 0.65,
                                    "block_below_confirmation_score": 0.65,
                                    "min_qualified_supporters": 2,
                                    "cap_multiplier": 0.25,
                                    "block_signal_combos": [["Bearish", "Neutral", "Bullish"]],
                                }
                            }
                        },
                        "news_driver_control": {
                            "enabled": True,
                            "min_news_confidence": 0.60,
                            "min_market_confirmation_score": 0.60,
                            "min_freshness_score": 0.70,
                            "min_relevance_score": 0.70,
                            "block_when_core_opposes": True,
                            "cap_without_fundamental_confirmation": True,
                            "cap_multiplier": 0.50,
                        },
                    },
                },
                "market_confirmation": {
                    "enabled": True,
                    "quality_gate_enabled": True,
                    "min_confirmations_for_new_entry": 1,
                    "min_confirmation_score_for_new_entry": 0.45,
                    "min_confirmation_score_for_weak_combo": 0.60,
                    "max_conflicts_for_new_entry": 3,
                    "weak_signal_strength": 0.25,
                    "quality_gate_block_weak_signal": True,
                    "quality_gate_cap_multiplier": 0.50,
                    "conflict_cap_multiplier": 0.50,
                    "block_weak_conflicting_signal": True,
                    "allow_conflicted_probe_with_strong_confirmation": True,
                    "conflicted_probe_min_confirmation_score": 0.65,
                    "conflicted_probe_min_confirmations": 3,
                },
                "trade_frequency_control": {
                    "weak_cap_multiplier": 0.50,
                    "weak_signal_combos": [[
                        "Bullish",
                        "Bullish",
                        "Neutral",
                    ]],
                },
                "strategy_memory": {
                    "enabled": True,
                    "audit": {
                        "protected_min_confirmation_score": 0.50,
                        "protected_multiplier": 0.50,
                        "protected_min_sample_count": 5,
                        "protected_min_win_rate": 0.60,
                        "protected_min_net_pnl": 1000,
                        "watchlist_min_confirmation_score": 0.60,
                        "watchlist_cap_multiplier": 0.50,
                        "weak_block_min_confirmation_score": 0.65,
                        "weak_block_cap_multiplier": 0.35,
                        "min_qualified_supporters": 2,
                    },
                },
            }
        )

    def _scc_news_payload(
        self,
        *,
        confidence: float,
        freshness_score: float,
        relevance_score: float,
        legacy_freshness_score: float | None = None,
    ) -> dict:
        contract = build_test_aec(
            "commodity_news",
            ticker="M",
            signal="Bullish",
            side="long",
            confidence=confidence,
            opportunity_state="probe_candidate",
            trigger_valid=True,
            current_trigger_confirmed=True,
            invalidation_present=True,
        )
        contract["fusion_evidence"].update(
            {
                "evidence_freshness": "fresh" if freshness_score >= 0.78 else "stale",
                "evidence_freshness_score": freshness_score,
            }
        )
        contract["data_usage_summary"]["sources"]["finoview_news_txt"].update(
            {
                "freshness_score": freshness_score,
                "relevance_score": relevance_score,
            }
        )
        metadata = {"action_evidence_contract": contract}
        if legacy_freshness_score is not None:
            metadata.update(
                {
                    "freshness_score": legacy_freshness_score,
                    "relevance_score": legacy_freshness_score,
                }
            )
        return {
            "agent_name": "commodity_news",
            "signal": "Bullish",
            "confidence": confidence,
            "metadata": metadata,
        }

    def test_cold_start_reduces_without_blocking(self):
        output = self._auditor().plan(
            PMRiskGateInput(
                ticker="M",
                signal_combo=["Neutral", "Neutral", "Neutral"],
                raw_position_ratio=0.10,
                current_position_ratio=0.0,
                signal_strength=0.40,
                market_confirmation={"enabled": False},
            )
        )

        self.assertEqual(output.decision, "scale_down")
        self.assertIn("cold_start_small_cap", output.reasons)
        self.assertAlmostEqual(output.position_ratio_multiplier, 0.50)

    def test_severe_ticker_side_performance_limits_new_exposure_to_probe(self):
        output = self._auditor().plan(
            PMRiskGateInput(
                ticker="TA",
                signal_combo=["Bearish", "Neutral", "Bearish"],
                raw_position_ratio=-0.12,
                current_position_ratio=0.0,
                signal_strength=0.60,
                market_confirmation={"enabled": False},
                recent_ticker_side_performance={
                    "total_trades": 12,
                    "win_rate": 0.25,
                    "total_pnl": -6200,
                },
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertIn("side_performance_block", output.reasons)
        self.assertIn("soft_block_converted_to_probe_only", output.reasons)
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_weak_combo_with_strong_confirmation_is_allowed(self):
        output = self._auditor().plan(
            PMRiskGateInput(
                ticker="M",
                signal_combo=["Bullish", "Bullish", "Neutral"],
                raw_position_ratio=0.10,
                current_position_ratio=0.0,
                signal_strength=0.60,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.75,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
                recent_ticker_side_performance={
                    "total_trades": 8,
                    "win_rate": 0.62,
                    "total_pnl": 2500,
                },
                recent_conditional_performance={
                    "total_trades": 8,
                    "win_rate": 0.62,
                    "total_pnl": 2500,
                },
            )
        )

        self.assertEqual(output.decision, "allow")
        self.assertNotIn("weak_signal_combo", output.reasons)

    def test_conflicted_strong_confirmation_probe_is_reduced_not_blocked(self):
        output = self._auditor().plan(
            PMRiskGateInput(
                ticker="BU",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bullish", "confidence": 0.58, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Bullish", "confidence": 0.40, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "commodity_news", "signal": "Neutral", "confidence": 0.62, "metadata": {"tradeability": "medium"}},
                ],
                signal_combo=["Bullish", "Bullish", "Neutral"],
                raw_position_ratio=0.05,
                current_position_ratio=0.0,
                signal_strength=0.18,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.67,
                    "features": [{"feature": "basis"}, {"feature": "ls_ratio"}],
                    "confirmations": [
                        "variety_position_rank",
                        "symbol_position_rank",
                        "ls_ratio",
                        "broker_net_margin_change",
                    ],
                    "conflicts": ["basis", "broker_variety_profit"],
                },
            )
        )

        self.assertEqual(output.decision, "scale_down")
        self.assertIn("market_confirmation_conflict", output.reasons)
        self.assertIn("cold_start_small_cap", output.reasons)

    def test_protected_ticker_side_weak_combo_reduces_instead_of_blocks(self):
        output = self._auditor().plan(
            PMRiskGateInput(
                ticker="BU",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bullish", "confidence": 0.55, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Bullish", "confidence": 0.52, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "commodity_news", "signal": "Neutral", "confidence": 0.55, "metadata": {"tradeability": "medium"}},
                ],
                signal_combo=["Bullish", "Bullish", "Neutral"],
                raw_position_ratio=0.05,
                current_position_ratio=0.0,
                signal_strength=0.35,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.55,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
            )
        )

        self.assertEqual(output.decision, "scale_down")
        self.assertIn("protected_ticker_side_cold_start", output.reasons)
        self.assertNotEqual(output.position_ratio_multiplier, 0.0)

    def test_shallow_protected_memory_does_not_override_weak_combo_block(self):
        config = self._auditor().full_config
        config["trade_frequency_control"]["weak_signal_combos"] = [["Bullish", "Neutral", "Neutral"]]
        config["market_confirmation"]["conflicted_probe_min_confirmation_score"] = 0.90
        auditor = PMRiskGate(config)

        output = auditor.plan(
            PMRiskGateInput(
                ticker="M",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bullish", "confidence": 0.55, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Neutral", "confidence": 0.52, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "commodity_news", "signal": "Neutral", "confidence": 0.55, "metadata": {"tradeability": "medium"}},
                ],
                signal_combo=["Bullish", "Neutral", "Neutral"],
                raw_position_ratio=0.12,
                current_position_ratio=0.0,
                signal_strength=0.45,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.55,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": ["contract_daily_indicators"],
                },
                strategy_memory={
                    "side_memory": {
                        "memory_state": "protected",
                        "sample_count": 3,
                        "win_rate": 1.0,
                        "net_pnl": 2331.97,
                    }
                },
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertNotIn("protected_memory_evidence_rejected", output.reasons)
        self.assertIn("cold_start_weak_combo_block", output.reasons)
        self.assertEqual(
            output.diagnostics.get("research_memory_boundary"),
            "RiskGate_does_not_consume_research_records",
        )

    def test_weak_ticker_side_rule_limits_latest_bad_p_long_template_to_probe(self):
        output = self._auditor().plan(
            PMRiskGateInput(
                ticker="P",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bearish", "confidence": 0.58, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Neutral", "confidence": 0.35, "metadata": {"tradeability": "medium"}},
                    self._scc_news_payload(
                        confidence=0.70,
                        freshness_score=0.90,
                        relevance_score=0.90,
                    ),
                ],
                signal_combo=["Bearish", "Neutral", "Bullish"],
                raw_position_ratio=0.05,
                current_position_ratio=0.0,
                signal_strength=0.35,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.64,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertIn("weak_ticker_side_quality_gate", output.reasons)
        self.assertIn("soft_block_converted_to_probe_only", output.reasons)
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_news_only_directional_trade_limits_when_core_opposes(self):
        output = self._auditor().plan(
            PMRiskGateInput(
                ticker="M",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bearish", "confidence": 0.58, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Neutral", "confidence": 0.40, "metadata": {"tradeability": "medium"}},
                    self._scc_news_payload(
                        confidence=0.75,
                        freshness_score=0.90,
                        relevance_score=0.90,
                    ),
                ],
                signal_combo=["Bearish", "Neutral", "Bullish"],
                raw_position_ratio=0.05,
                current_position_ratio=0.0,
                signal_strength=0.35,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.75,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertIn("news_only_directional_trade", output.reasons)
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_news_driver_reads_formal_scc_evidence_not_legacy_metadata(self):
        output = self._auditor().plan(
            PMRiskGateInput(
                ticker="M",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Neutral", "confidence": 0.40},
                    {"agent_name": "fundamental", "signal": "Neutral", "confidence": 0.40},
                    self._scc_news_payload(
                        confidence=0.75,
                        freshness_score=0.40,
                        relevance_score=0.45,
                        legacy_freshness_score=0.99,
                    ),
                ],
                signal_combo=["Neutral", "Neutral", "Bullish"],
                raw_position_ratio=0.05,
                current_position_ratio=0.0,
                signal_strength=0.35,
                market_confirmation={"enabled": True, "confirmation_score": 0.75},
            )
        )

        diagnostics = output.diagnostics["news_driver_control"]
        self.assertEqual(diagnostics["freshness_score"], 0.40)
        self.assertEqual(diagnostics["relevance_score"], 0.45)

    def test_pm_news_override_reads_formal_scc_evidence(self):
        control = {
            "news_override_tradeability": "high",
            "news_override_min_confidence": 0.60,
            "news_override_min_freshness": 0.70,
            "news_override_min_relevance": 0.70,
        }
        fresh = self._scc_news_payload(
            confidence=0.75,
            freshness_score=0.90,
            relevance_score=0.90,
            legacy_freshness_score=0.10,
        )
        stale = self._scc_news_payload(
            confidence=0.75,
            freshness_score=0.40,
            relevance_score=0.45,
            legacy_freshness_score=0.99,
        )
        for payload in (fresh, stale):
            payload.update(
                {
                    "side": "long",
                    "effective_confidence": 0.75,
                    "tradeability": "high",
                }
            )
        self.assertTrue(_news_high_quality_override(fresh, "long", control))
        self.assertFalse(_news_high_quality_override(stale, "long", control))

    def test_strategy_memory_weak_block_limits_new_exposure_to_probe(self):
        output = self._auditor().plan(
            PMRiskGateInput(
                ticker="M",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bullish", "confidence": 0.58, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Neutral", "confidence": 0.40, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "commodity_news", "signal": "Neutral", "confidence": 0.35, "metadata": {"tradeability": "medium"}},
                ],
                signal_combo=["Bullish", "Neutral", "Neutral"],
                raw_position_ratio=0.05,
                current_position_ratio=0.0,
                signal_strength=0.35,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.55,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
                strategy_memory={
                    "combo": {
                        "memory_state": "weak_block",
                        "sample_count": 4,
                        "win_rate": 0.25,
                        "net_pnl": -3200,
                        "signal_combo": "[\"Bullish\", \"Neutral\", \"Neutral\"]",
                    },
                    "side_memory": None,
                    "records": [],
                },
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertNotIn("strategy_memory_weak_block", output.reasons)
        self.assertEqual(
            output.diagnostics.get("research_memory_boundary"),
            "RiskGate_does_not_consume_research_records",
        )
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_auditor_ignores_legacy_contextual_calibration_research_payload(self):
        config = self._auditor().full_config
        config["learning"] = {
            "contextual_rule_calibration": {
                "enabled": True,
                "min_confidence": 0.35,
                "pm_soft_risk_reasons": ["side_performance_block"],
            }
        }
        auditor = PMRiskGate(config)
        output = auditor.plan(
            PMRiskGateInput(
                ticker="TA",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bearish", "confidence": 0.65, "horizon_class": "short", "market_regime": "trend"},
                ],
                signal_combo=["Bearish", "Neutral", "Bearish"],
                raw_position_ratio=-0.12,
                current_position_ratio=0.0,
                signal_strength=0.60,
                market_confirmation={"enabled": False},
                recent_ticker_side_performance={
                    "total_trades": 12,
                    "win_rate": 0.25,
                    "total_pnl": -6200,
                },
                adaptive_policy_state=[
                    {
                        "id": "cal-auditor",
                        "ticker": "TA",
                        "side": "short",
                        "setup_type": "*",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "policy_type": "contextual_rule_calibration:pm_risk_gate",
                        "policy_action": "calibrate",
                        "confidence_score": 0.55,
                        "sample_count": 2,
                        "payload": {
                            "rule_adjustments": {
                                "pm_risk_gate": {
                                    "soften_hard_block_reasons": ["side_performance_block"]
                                }
                            }
                        },
                    }
                ],
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertIn("side_performance_block", output.reasons)
        self.assertNotIn("contextual_rule_calibration", output.diagnostics)
        self.assertNotIn("contextual_rule_calibration", output.reasons)
        self.assertEqual(
            output.diagnostics.get("research_memory_boundary"),
            "RiskGate_does_not_consume_research_records",
        )

    def test_single_high_quality_analyst_support_is_probe_not_block(self):
        config = self._auditor().full_config
        config["pm_risk_gate"]["quality_gate"]["allow_single_high_quality_probe"] = True
        auditor = PMRiskGate(config)

        output = auditor.plan(
            PMRiskGateInput(
                ticker="SR",
                analyst_signals=[
                    {
                        "agent_name": "technical",
                        "signal": "Bullish",
                        "confidence": 0.62,
                        "business_quality_score": 0.66,
                        "metadata": {"tradeability": "high"},
                    },
                    {"agent_name": "fundamental", "signal": "Neutral", "confidence": 0.42, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "commodity_news", "signal": "Neutral", "confidence": 0.38, "metadata": {"tradeability": "medium"}},
                ],
                signal_combo=["Bullish", "Neutral", "Neutral"],
                raw_position_ratio=0.03,
                current_position_ratio=0.0,
                signal_strength=0.40,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.50,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertIn("single_high_quality_probe_only", output.reasons)
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_market_confirmation_soft_risk_scales_instead_of_blocking(self):
        config = self._auditor().full_config
        config["market_confirmation"]["quality_gate_block_weak_signal"] = False
        config["market_confirmation"]["block_weak_conflicting_signal"] = False
        auditor = PMRiskGate(config)

        output = auditor.plan(
            PMRiskGateInput(
                ticker="PB",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Bullish", "confidence": 0.58, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Bullish", "confidence": 0.56, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "commodity_news", "signal": "Neutral", "confidence": 0.40, "metadata": {"tradeability": "medium"}},
                ],
                signal_combo=["Bullish", "Bullish", "Neutral"],
                raw_position_ratio=0.04,
                current_position_ratio=0.0,
                signal_strength=0.20,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.40,
                    "features": [{"feature": "basis"}],
                    "confirmations": [],
                    "conflicts": ["basis"],
                },
            )
        )

        self.assertNotEqual(output.decision, "block")
        self.assertIn("market_confirmation_quality_gate", output.reasons)
        self.assertIn("market_confirmation_conflict", output.reasons)
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_scorecard_single_high_quality_setup_can_seed_probe(self):
        side, ratio, row = _scorecard_probe_seed(
            opportunity_scorecard={
                "preferred_side": "long",
                "long": {
                    "final_state": "tradeable_candidate",
                    "supporting_signal_count": 1,
                    "score": 0.55,
                    "max_setup_quality": 0.64,
                    "max_business_quality": 0.66,
                    "market_confirmation_score": 0.48,
                    "gating_failures": [],
                },
                "short": {
                    "final_state": "no_opportunity",
                    "supporting_signal_count": 0,
                    "score": 0.0,
                },
            },
            control={
                "watch_for_trigger_new_entry": {
                    "allow_probe": True,
                    "probe_max_ratio": 0.01,
                    "probe_floor_ratio": 0.005,
                    "scorecard_probe_min_supporting_signals": 2,
                    "allow_single_high_quality_probe": True,
                    "single_high_quality_probe_min_score": 0.52,
                    "single_high_quality_probe_min_setup_quality": 0.60,
                    "single_high_quality_probe_min_business_quality": 0.60,
                    "single_high_quality_probe_min_confirmation_score": 0.45,
                }
            },
        )

        self.assertEqual(side, "long")
        self.assertGreater(ratio, 0.0)
        self.assertEqual(row["final_state"], "tradeable_candidate")

    def test_scorecard_confirmed_tradeable_candidate_seeds_probe_even_when_score_is_modest(self):
        """Regression for 2025-04-10 ZN: PM seed must use scorecard layer, not only raw score."""
        side, ratio, row = _scorecard_probe_seed(
            opportunity_scorecard={
                "preferred_side": "long",
                "long": {
                    "final_state": "tradeable_candidate",
                    "supporting_signal_count": 1,
                    "score": 0.51,
                    "max_setup_quality": 0.75,
                    "max_business_quality": 0.72,
                    "market_confirmation_score": 0.75,
                    "single_tradeable_candidate_setup_confirmed": True,
                    "technical_opposes_side": False,
                    "gating_failures": [],
                },
                "short": {
                    "final_state": "no_opportunity",
                    "supporting_signal_count": 0,
                    "score": 0.0,
                },
            },
            control={
                "watch_for_trigger_new_entry": {
                    "allow_probe": True,
                    "probe_max_ratio": 0.01,
                    "probe_floor_ratio": 0.005,
                    "scorecard_probe_min_supporting_signals": 2,
                    "allow_single_high_quality_probe": True,
                    "single_high_quality_probe_min_score": 0.52,
                    "single_high_quality_probe_min_setup_quality": 0.60,
                    "single_high_quality_probe_min_business_quality": 0.60,
                    "single_high_quality_probe_min_confirmation_score": 0.45,
                    "scorecard_tradeable_candidate_probe_min_confirmation_score": 0.68,
                }
            },
        )

        self.assertEqual(side, "long")
        self.assertGreater(ratio, 0.0)
        self.assertEqual(row["final_state"], "tradeable_candidate")

    def test_scorecard_probe_seed_keeps_step2_side_when_opposite_quality_is_higher(self):
        side, ratio, row = _scorecard_probe_seed(
            opportunity_scorecard={
                "preferred_side": "long",
                "long": {
                    "final_state": "tradeable_candidate",
                    "supporting_signal_count": 2,
                    "score": 0.82,
                    "candidate_quality": 0.60,
                    "max_setup_quality": 0.70,
                    "max_business_quality": 0.70,
                    "market_confirmation_score": 0.72,
                    "gating_failures": [],
                },
                "short": {
                    "final_state": "tradeable_candidate",
                    "supporting_signal_count": 1,
                    "score": 0.70,
                    "candidate_quality": 0.91,
                    "max_setup_quality": 0.70,
                    "max_business_quality": 0.70,
                    "market_confirmation_score": 0.72,
                    "gating_failures": [],
                },
            },
            control={
                "watch_for_trigger_new_entry": {
                    "allow_probe": True,
                    "probe_max_ratio": 0.01,
                    "probe_floor_ratio": 0.005,
                    "scorecard_probe_min_supporting_signals": 1,
                    "scorecard_probe_min_score": 0.35,
                    "scorecard_tradeable_candidate_probe_min_confirmation_score": 0.68,
                }
            },
        )

        self.assertEqual(side, "long")
        self.assertGreater(ratio, 0.0)
        self.assertEqual(row["candidate_quality"], 0.60)

    def test_scorecard_watch_for_trigger_seed_is_conditional_monitor_candidate(self):
        side, ratio, row = _scorecard_probe_seed(
            opportunity_scorecard={
                "preferred_side": "short",
                "short": {
                    "final_state": "watch_for_trigger",
                    "supporting_signal_count": 2,
                    "score": 0.56,
                    "max_setup_quality": 0.68,
                    "max_business_quality": 0.64,
                    "market_confirmation_score": 0.48,
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "invalidation_present": True,
                    "entry_trigger": "wait for post-open break below support",
                    "gating_failures": [],
                }
            },
            control={
                "watch_for_trigger_new_entry": {
                    "allow_probe": True,
                    "probe_max_ratio": 0.01,
                    "probe_floor_ratio": 0.005,
                    "scorecard_probe_min_supporting_signals": 2,
                    "scorecard_probe_min_score": 0.35,
                    "scorecard_probe_block_on_critical_data_gap": True,
                }
            },
        )

        self.assertEqual(side, "short")
        self.assertLess(ratio, 0.0)
        self.assertEqual(row["final_state"], "watch_for_trigger")
        self.assertTrue(row["setup_quality_ok"])
        self.assertFalse(row["trigger_valid"])

    def test_scorecard_confirmed_tradeable_candidate_with_technical_opposition_does_not_seed_probe(self):
        side, ratio, _row = _scorecard_probe_seed(
            opportunity_scorecard={
                "preferred_side": "long",
                "long": {
                    "final_state": "tradeable_candidate",
                    "supporting_signal_count": 1,
                    "score": 0.51,
                    "max_setup_quality": 0.75,
                    "max_business_quality": 0.72,
                    "market_confirmation_score": 0.75,
                    "single_tradeable_candidate_setup_confirmed": False,
                    "technical_opposes_side": True,
                    "gating_failures": [],
                }
            },
            control={
                "watch_for_trigger_new_entry": {
                    "allow_probe": True,
                    "probe_max_ratio": 0.01,
                    "probe_floor_ratio": 0.005,
                    "scorecard_tradeable_candidate_probe_min_confirmation_score": 0.68,
                }
            },
        )

        self.assertEqual(side, "flat")
        self.assertEqual(ratio, 0.0)

    def test_low_quality_news_driver_is_probe_capped_not_hard_blocked(self):
        output = self._auditor().plan(
            PMRiskGateInput(
                ticker="M",
                analyst_signals=[
                    {"agent_name": "technical", "signal": "Neutral", "confidence": 0.45, "metadata": {"tradeability": "medium"}},
                    {"agent_name": "fundamental", "signal": "Neutral", "confidence": 0.42, "metadata": {"tradeability": "medium"}},
                    self._scc_news_payload(
                        confidence=0.52,
                        freshness_score=0.60,
                        relevance_score=0.65,
                    ),
                ],
                signal_combo=["Neutral", "Neutral", "Bullish"],
                raw_position_ratio=0.04,
                current_position_ratio=0.0,
                signal_strength=0.25,
                market_confirmation={
                    "enabled": True,
                    "confirmation_score": 0.50,
                    "features": [{"feature": "basis"}],
                    "confirmations": ["basis"],
                    "conflicts": [],
                },
            )
        )

        self.assertNotEqual(output.decision, "block")
        self.assertIn("news_only_directional_trade", output.reasons)
        self.assertGreater(output.position_ratio_multiplier, 0.0)

    def test_nonempty_provisional_policy_is_applied_without_leaking_into_fac(self):
        gate = PMRiskGate(
            {
                "pm_risk_gate": {
                    "enabled": True,
                    "policy_version": "test_v1",
                    "learning_mode": "audit_only",
                    "attribution_feedback": {"enabled": False},
                    "quality_gate": {"enabled": False},
                },
                "market_confirmation": {"enabled": False},
            }
        )
        provisional_policy = {
            "id": "75e2d9b2-46ec-4d94-8607-68b7dba5f991",
            "ticker": "RB",
            "side": "short",
            "setup_type": "short_trend_breakout_setup_short",
            "horizon_class": "short",
            "policy_action": "probe_only",
            "multiplier": 0.35,
            "confidence": 0.4678433866666667,
            "event_type": "consecutive_setup_losses",
            "sample_count": 3,
            "source_trading_date": "2025-04-21",
            "valid_until": "2025-05-01",
            "reason": "net_pnl=-5446; win_rate=0",
        }
        output = gate.plan(
            PMRiskGateInput(
                ticker="RB",
                trading_date="2025-04-22",
                signal_combo=["Bearish", "Neutral", "Neutral"],
                raw_target_side="short",
                raw_position_ratio=-0.015,
                current_position_ratio=0.0,
                signal_strength=0.60,
                market_confirmation={"enabled": False},
                provisional_policy_state=[provisional_policy],
            )
        )

        self.assertEqual(output.decision, "probe_only")
        self.assertAlmostEqual(output.position_ratio_multiplier, 0.35)
        self.assertIn("provisional_policy_probe_only", output.reasons)
        self.assertEqual(output.diagnostics["provisional_policy_state"], [provisional_policy])
        self.assertEqual(output.diagnostics["provisional_policy_applied"], [provisional_policy])

        landing_audit = _build_pm_landing_consistency_audit(
            ticker="RB",
            current_lots=0,
            target_lots=-1,
            current_position_ratio=0.0,
            final_position_ratio=-0.00525,
            recommendation_intent={"action": "open_short", "action_type": "open_short"},
            lots_to_trade=1,
            lots_to_trade_reason="provisional_policy_probe_only",
            opportunity_scorecard={
                "preferred_side": "short",
                "short": {
                    "final_state": "probe_candidate",
                    "entry_setup_count": 1,
                    "invalidation_count": 1,
                },
            },
            analyst_signals=[],
            pm_learning_audit={},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=[],
            pm_risk_gate_payload=output.model_dump(),
            control_reasons=list(output.reasons),
            margin_required=1000.0,
            margin_available=100000.0,
            market_confirmation={"confirmation_score": 0.60},
        )
        alignment = landing_audit["pm_risk_gate_alignment"]
        self.assertEqual(alignment["decision"], "probe_only")
        self.assertAlmostEqual(alignment["position_ratio_multiplier"], 0.35)
        self.assertIn("provisional_policy_probe_only", alignment["reasons"])
        self.assertNotIn("diagnostics", alignment)
        self.assertNotIn("audit_payload", alignment)
        self.assertNotIn("notes", alignment)
        self.assertNotIn("provisional_policy_state", json.dumps(landing_audit))
        self.assertNotIn("provisional_policy_applied", json.dumps(landing_audit))
        validate_pm_artifact_boundary(
            {
                "final_action_contract": {
                    "learning_used": {
                        "pm_landing_consistency_audit": landing_audit,
                    }
                }
            }
        )

    def test_weak_signal_combo_new_entry_is_probe_capped_not_zeroed(self):
        ratio, reasons, notes, diagnostics = _apply_trade_frequency_control(
            db=None,
            config_id="test-config",
            ticker="PB",
            trading_date="2025-01-02",
            position_ratio=0.04,
            current_ratio=0.0,
            signal_combo=("Bullish", "Bullish", "Neutral"),
            market_confirmation={
                "enabled": True,
                "confirmation_score": 0.45,
                "features": [{"feature": "basis"}],
                "confirmations": ["basis"],
                "conflicts": [],
            },
            full_config={
                "trade_frequency_control": {
                    "enabled": True,
                    "weak_signal_combos": [["Bullish", "Bullish", "Neutral"]],
                    "weak_combo_probe_max_ratio": 0.01,
                    "weak_combo_probe_floor_ratio": 0.005,
                    "weak_cap_multiplier": 0.50,
                },
                "market_confirmation": {
                    "min_confirmations_for_new_entry": 2,
                    "min_confirmation_score_for_weak_combo": 0.60,
                },
            },
        )

        self.assertGreater(ratio, 0.0)
        self.assertLessEqual(abs(ratio), 0.01)
        self.assertIn("weak_signal_combo", reasons)
        self.assertIn("weak_signal_combo_probe_cap", reasons)


class ValidationRegressionTest(unittest.TestCase):
    def test_zero_transaction_day_classification_uses_recommendation_audit(self):
        recommendations = [
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "neutral_signal_no_trade"}},
            },
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "position_matched"}},
            },
        ]
        result = classify_zero_transaction_day(recommendations)
        self.assertEqual(result["classification"], "expected")
        self.assertEqual(sorted(result["reasons"]), ["neutral_signal_no_trade", "position_matched"])
        self.assertEqual(result["reason_categories"], {"signal": 1, "business": 1})

    def test_zero_transaction_day_allows_horizon_timing_gate(self):
        recommendations = [
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "neutral_signal_no_trade"}},
            },
            {
                "source_type": "strategy",
                "signal_snapshot": {
                    "execution_result": {"no_trade_reason": "horizon_consistency_requires_short_timing"}
                },
            },
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "pm_risk_gate_block"}},
            },
        ]

        result = classify_zero_transaction_day(recommendations)

        self.assertEqual(result["classification"], "expected")
        self.assertIn("horizon_consistency_requires_short_timing", result["reasons"])
        self.assertEqual(result["reason_categories"], {"signal": 1, "timing": 1, "risk": 1})

    def test_zero_transaction_day_allows_learning_watchlist_gate(self):
        recommendations = [
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "weak_ticker_side_history"}},
            },
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "neutral_signal_no_trade"}},
            },
        ]

        result = classify_zero_transaction_day(recommendations)

        self.assertEqual(result["classification"], "expected")
        self.assertIn("weak_ticker_side_history", result["reasons"])
        self.assertEqual(result["reason_categories"], {"learning": 1, "signal": 1})

    def test_zero_transaction_day_allows_provisional_probe_gate(self):
        recommendations = [
            {
                "source_type": "strategy",
                "signal_snapshot": {"execution_result": {"no_trade_reason": "provisional_policy_probe_only"}},
            }
        ]

        result = classify_zero_transaction_day(recommendations)

        self.assertEqual(result["classification"], "expected")
        self.assertIn("provisional_policy_probe_only", result["reasons"])

    def test_net_exposure_review_allows_small_phase4_drift_with_warning(self):
        warnings = []
        errors = []
        diagnostics = []

        _apply_net_exposure_review(
            trading_date="2025-01-14",
            cfg={"net_exposure_control": {"max_net_exposure": 0.50, "phase4_drift_tolerance": 0.01}},
            net_exposure=0.5085,
            warnings=warnings,
            errors=errors,
            budget_drift_diagnostics=diagnostics,
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("PM plan budget drift", warnings[0])
        self.assertEqual(len(diagnostics), 1)
        self.assertNotIn("reviewer_hard_gate", diagnostics[0])
        self.assertEqual(diagnostics[0]["planned_budget_parameter"], "net_exposure_control.max_net_exposure")

    def test_net_exposure_review_records_material_budget_drift_without_phase4_error(self):
        warnings = []
        errors = []
        diagnostics = []

        _apply_net_exposure_review(
            trading_date="2025-01-14",
            cfg={"net_exposure_control": {"max_net_exposure": 0.50, "phase4_drift_tolerance": 0.01}},
            net_exposure=0.525,
            warnings=warnings,
            errors=errors,
            budget_drift_diagnostics=diagnostics,
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["realized_value"], 0.525)
        self.assertIn(
            "position_budget_policy.probe_margin_ratio",
            diagnostics[0]["pm_plan_budget_parameters_are_review_context"],
        )

    def test_net_exposure_review_attributes_conditional_leg_budget_drift(self):
        warnings = []
        errors = []
        diagnostics = []

        _apply_net_exposure_review(
            trading_date="2025-03-25",
            cfg={"net_exposure_control": {"max_net_exposure": 0.50}},
            net_exposure=0.5376,
            warnings=warnings,
            errors=errors,
            budget_drift_diagnostics=diagnostics,
            recommendations=[
                {
                    "underlying_code": "BU",
                    "signal_snapshot": json.dumps(
                        {"execution_result": {"no_trade_reason": "intraday_trigger_not_met"}}
                    ),
                }
            ],
        )

        self.assertEqual(errors, [])
        self.assertEqual(diagnostics[0]["drift_reason"], "conditional_leg_not_triggered_caused_realized_budget_drift")
        self.assertEqual(diagnostics[0]["untriggered_conditional_legs"], ["BU"])

    def test_net_exposure_review_uses_dynamic_alpha_release_cap(self):
        warnings = []
        errors = []

        _apply_net_exposure_review(
            trading_date="2025-02-10",
            cfg={"net_exposure_control": {"max_net_exposure": 0.50, "phase4_drift_tolerance": 0.01}},
            net_exposure=1.4617,
            warnings=warnings,
            errors=errors,
            recommendations=[
                {
                    "underlying_code": "M",
                    "signal_snapshot": json.dumps(
                        {
                            "execution_translation": {
                                "dynamic_net_exposure_control": {
                                    "mode": "alpha_release",
                                    "max_net_exposure": 2.0,
                                }
                            }
                        }
                    ),
                }
            ],
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("dynamic alpha-release cap", warnings[0])

    def test_reviewer_does_not_apply_account_margin_hard_gate(self):
        source = (SRC_ROOT / "tools" / "agent_tools" / "research" / "reviewer_phase4_review.py").read_text(
            encoding="utf-8-sig"
        )
        self.assertNotIn("_apply_account_margin_hard_gate", source)

    def test_market_confirmation_quality_warnings_are_aggregated_by_status(self):
        recommendations = [
            {
                "underlying_code": "BU",
                "signal_snapshot": {
                    "market_confirmation": {
                        "data_missing": ["contract_rank"],
                        "feature_status": {"contract_rank": "parameter_error"},
                        "parameter_errors": ["contract_rank"],
                        "data_status_groups": {"parameter_error": ["contract_rank"]},
                    }
                },
            },
            {
                "underlying_code": "RB",
                "signal_snapshot": {
                    "market_confirmation": {
                        "data_missing": ["warehouse_receipt"],
                        "feature_status": {"warehouse_receipt": "no_data"},
                        "no_data": ["warehouse_receipt"],
                        "data_status_groups": {"no_data": ["warehouse_receipt"]},
                    }
                },
            },
            {
                "underlying_code": "M",
                "signal_snapshot": {
                    "market_confirmation": {
                        "data_missing": [],
                        "fallback_covered_missing": ["net_flow_long", "net_flow_short"],
                        "feature_status": {
                            "net_flow_long": "fallback_covered",
                            "net_flow_short": "fallback_covered",
                        },
                        "data_status_groups": {
                            "fallback_covered": ["net_flow_long", "net_flow_short"]
                        },
                    }
                },
            },
        ]

        warnings, summary = _collect_recommendation_quality_warnings(recommendations)

        self.assertEqual(warnings, [])
        self.assertEqual(summary["ticker_count"], 0)
        self.assertEqual(summary["missing_by_status"], {})

    def test_reviewer_execution_audit_accepts_hold_recommendation_without_transactions(self):
        errors = []
        counter = _review_recommendation_execution_facts(
            recommendations=[
                {
                    "id": "rec-hold",
                    "action": "hold",
                    "lots": 0,
                    "status": "executed",
                    "signal_snapshot": json.dumps(
                        {
                            "execution_result": {
                                "transaction_count": 0,
                                "actual_transactions": [],
                                "no_trade_reason": "position_matched",
                            }
                        }
                    ),
                }
            ],
            transactions_by_recommendation={},
            errors=errors,
        )

        self.assertEqual(errors, [])
        self.assertEqual(counter["position_matched"], 1)

    def test_capital_deployment_diagnostics_separates_reasons_and_alpha_candidates(self):
        recommendations = [
            {
                "underlying_code": "BU",
                "signal_snapshot": {
                    "final_action_contract": {
                        "target_position_ratio": 0.05,
                        "current_lots": 2,
                        "target_lots": 5,
                        "lots_delta": 3,
                        "lots_delta_abs": 3,
                        "reason_codes": "capital_release_candidate",
                        "final_action": "scale",
                        "reason_codes": ["capital_utilization_guard"],
                        "authority_type": "real_budget_entry",
                        "authority_decision": "allow_real_new_entry",
                        "evidence_used": {
                            "market_confirmation_score": 0.72,
                        },
                        "learning_used": {
                            "memory_state": "protected",
                            "capital_utilization_learning": {
                                "protected_memory": {
                                    "memory_state": "protected",
                                    "signal_combo": "Bullish|Bullish|Neutral",
                                }
                            },
                            "capital_utilization_target": {
                                "alpha_release_tier": "boost",
                                "stop_protected": True,
                                "structured_invalidation": True,
                                "alpha_release": {
                                    "specific_signal_combo": True,
                                    "limiting_reasons": [],
                                },
                            },
                        },
                    },
                    "execution_result": {"no_trade_reason": "capital_release_candidate"},
                    "pm_draft_pm_internal_draft_not_trade_source": True,
                    "pm_draft_for_test_only": {
                        "target_position_ratio": 0.05,
                        "current_ticker_exposure": 0.02,
                        "target_lots": 5,
                        "current_lots_before_open": 2,
                        "lots_delta_abs": 3,
                        "reason_codes": "position_matched",
                        "signal_confidence": 0.70,
                        "rebalance_summary": {
                            "action_type": "increase",
                            "control_reasons": ["capital_utilization_guard"],
                        },
                        "signal_lifecycle": {"invalidation_level": 3200.0},
                        "market_confirmation": {"confirmation_score": 0.72},
                        "strategy_controls": {
                            "diagnostics": {
                                "capital_utilization_learning": {
                                    "protected_memory": {
                                        "memory_state": "protected",
                                        "signal_combo": "Bullish|Bullish|Neutral",
                                    }
                                }
                            }
                        },
                        "pm_risk_gate": {"decision": "allow", "reasons": ["pm_risk_gate_allow"]},
                    },
                },
            },
            {
                "underlying_code": "RB",
                "signal_snapshot": {
                    "final_action_contract": {
                        "target_position_ratio": 0.04,
                        "target_lots": 4,
                        "current_lots": 0,
                        "lots_delta": 4,
                        "lots_delta_abs": 0,
                        "reason_codes": "intraday_trigger_not_met",
                        "final_action": "open_real",
                        "reason_codes": [],
                        "authority_type": "real_budget_entry",
                        "authority_decision": "allow_real_new_entry",
                        "evidence_used": {
                            "market_confirmation_score": 0.66,
                        },
                    },
                    "execution_result": {"no_trade_reason": "intraday_trigger_not_met"},
                },
            },
            {
                "underlying_code": "M",
                "signal_snapshot": {
                    "final_action_contract": {
                        "target_position_ratio": -0.03,
                        "target_lots": -3,
                        "current_lots": 0,
                        "lots_delta": -3,
                        "lots_delta_abs": 0,
                        "reason_codes": "pm_risk_gate_block",
                        "final_action": "wait",
                        "reason_codes": ["analyst_quality_low_tradeability"],
                        "authority_type": "not_applicable",
                        "authority_decision": "blocked",
                        "evidence_used": {
                            "market_confirmation_score": 0.40,
                        },
                    },
                    "execution_result": {"no_trade_reason": "pm_risk_gate_block"},
                },
            },
        ]

        diagnostics = _build_capital_deployment_diagnostics(
            cfg={
                "capital_utilization_control": {
                    "min_confirmation_score_for_scaling": 0.60,
                    "memory_protected_min_confirmation_score": 0.45,
                },
                "execution": {
                    "intraday_confirmation": {
                        "opening_range_minutes": 30,
                        "require_complete_opening_range": True,
                        "max_chase_ratio": 0.015,
                    }
                },
            },
            allocation_tier="under_deployed",
            reason_bucket="intraday_trigger_not_met",
            current_ratio=0.04,
            target_min=0.16,
            target_max=0.20,
            margin_gap_to_min=600000.0,
            strategy_recommendations=recommendations,
            no_trade_reason_counter=Counter(
                {
                    "position_matched": 1,
                    "intraday_trigger_not_met": 1,
                    "pm_risk_gate_block": 1,
                }
            ),
        )

        self.assertEqual(diagnostics["primary_category"], "execution_timing_gate")
        self.assertEqual(diagnostics["category_counts"]["position_already_matched"], 1)
        self.assertEqual(diagnostics["category_counts"]["execution_timing_gate"], 1)
        self.assertEqual(diagnostics["category_counts"]["pm_risk_gate_suppression"], 1)
        self.assertEqual(diagnostics["directional_candidate_count"], 3)
        self.assertEqual(diagnostics["blocked_directional_candidate_count"], 3)
        self.assertEqual(diagnostics["capital_path_stage_counts"]["execution_timing"], 1)
        self.assertEqual(diagnostics["capital_path_stage_counts"]["hard_or_pm_risk_gate_block"], 1)
        self.assertIn("capital_path_cases", diagnostics)
        self.assertEqual(diagnostics["alpha_release_candidate_count"], 1)
        self.assertEqual(diagnostics["alpha_release_candidates"][0]["ticker"], "BU")
        self.assertEqual(diagnostics["alpha_release_candidates"][0]["alpha_release_tier"], "boost")
        self.assertTrue(
            diagnostics["alpha_release_candidates"][0]["alpha_release_requirements"]["stop_protected"]
        )
        self.assertEqual(diagnostics["execution_gate_candidates"][0]["ticker"], "RB")
        self.assertEqual(diagnostics["pm_risk_gate_suppression_cases"][0]["ticker"], "M")
        self.assertEqual(
            diagnostics["parameter_review"][0]["scope"],
            "execution.intraday_confirmation",
        )

    def test_capital_deployment_marks_recovery_probe_candidates_without_forcing_trade(self):
        recommendations = [
            {
                "underlying_code": "BU",
                "signal_snapshot": {
                    "final_action_contract": {
                        "target_position_ratio": 0.003,
                        "current_lots": 0,
                        "target_lots": 1,
                        "lots_delta": 1,
                        "lots_delta_abs": 1,
                        "reason_codes": "minimum_new_entry_threshold",
                        "final_action": "open_probe",
                        "reason_codes": ["minimum_new_entry_threshold"],
                        "authority_type": "exploration_probe",
                        "authority_decision": "allow_exploration_probe",
                        "evidence_used": {
                            "market_confirmation_score": 0.72,
                        },
                        "learning_used": {
                            "memory_state": "protected",
                            "capital_utilization_learning": {
                                "protected_memory": {
                                    "memory_state": "protected",
                                    "signal_combo": "Bullish|Bullish|Neutral",
                                }
                            },
                            "capital_utilization_target": {
                                "alpha_release_tier": "normal",
                                "stop_protected": True,
                                "structured_invalidation": True,
                                "alpha_release": {
                                    "specific_signal_combo": True,
                                    "limiting_reasons": [],
                                },
                            },
                        },
                    },
                    "execution_result": {"no_trade_reason": "minimum_new_entry_threshold"},
                },
            }
        ]

        diagnostics = _build_capital_deployment_diagnostics(
            cfg={
                "capital_utilization_control": {
                    "min_confirmation_score_for_scaling": 0.60,
                    "memory_protected_min_confirmation_score": 0.45,
                },
                "portfolio_manager": {
                    "holding_rebalance_control": {"min_new_entry_ratio": 0.004},
                },
            },
            allocation_tier="under_deployed",
            reason_bucket="minimum_new_entry_threshold",
            current_ratio=0.0,
            target_min=0.06,
            target_max=0.08,
            margin_gap_to_min=300000.0,
            strategy_recommendations=recommendations,
            no_trade_reason_counter=Counter({"minimum_new_entry_threshold": 1}),
        )

        self.assertEqual(diagnostics["alpha_release_candidate_count"], 0)
        self.assertEqual(diagnostics["recovery_probe_candidate_count"], 1)
        self.assertEqual(diagnostics["recovery_probe_candidates"][0]["ticker"], "BU")
        self.assertEqual(
            diagnostics["recovery_probe_candidates"][0]["blocked_by_reason"],
            "minimum_new_entry_threshold",
        )

    def test_capital_deployment_diagnostics_classifies_horizon_timing_gate(self):
        diagnostics = _build_capital_deployment_diagnostics(
            cfg={},
            allocation_tier="under_deployed",
            reason_bucket="horizon_consistency_requires_short_timing",
            current_ratio=0.0,
            target_min=0.06,
            target_max=0.08,
            margin_gap_to_min=300000.0,
            strategy_recommendations=[],
            no_trade_reason_counter=Counter({"horizon_consistency_requires_short_timing": 5}),
        )

        self.assertEqual(diagnostics["primary_category"], "strategy_timing_gate")
        self.assertEqual(diagnostics["category_counts"]["strategy_timing_gate"], 5)
        self.assertTrue(
            diagnostics["reason_profiles"]["horizon_consistency_requires_short_timing"]["risk_control_normal"]
        )

    def test_capital_deployment_diagnostics_classifies_learning_watchlist_gate(self):
        diagnostics = _build_capital_deployment_diagnostics(
            cfg={},
            allocation_tier="under_deployed",
            reason_bucket="weak_ticker_side_history",
            current_ratio=0.0,
            target_min=0.06,
            target_max=0.08,
            margin_gap_to_min=300000.0,
            strategy_recommendations=[],
            no_trade_reason_counter=Counter({"weak_ticker_side_history": 1}),
        )

        self.assertEqual(diagnostics["primary_category"], "learning_risk_control")
        self.assertEqual(diagnostics["category_counts"]["learning_risk_control"], 1)
        self.assertFalse(diagnostics["reason_profiles"]["weak_ticker_side_history"]["alpha_expansion_allowed"])

    def test_capital_deployment_diagnostics_classifies_provisional_probe_gate(self):
        diagnostics = _build_capital_deployment_diagnostics(
            cfg={},
            allocation_tier="under_deployed",
            reason_bucket="provisional_policy_probe_only",
            current_ratio=0.0,
            target_min=0.06,
            target_max=0.08,
            margin_gap_to_min=300000.0,
            strategy_recommendations=[],
            no_trade_reason_counter=Counter({"provisional_policy_probe_only": 2}),
        )

        self.assertEqual(diagnostics["primary_category"], "learning_risk_control")
        self.assertEqual(diagnostics["category_counts"]["learning_risk_control"], 2)
        self.assertFalse(diagnostics["reason_profiles"]["provisional_policy_probe_only"]["alpha_expansion_allowed"])


class AlphaReleaseCapitalUtilizationRegressionTest(unittest.TestCase):
    def _base_config(self):
        return {
            "capital_utilization_control": {
                "enabled": True,
                "target_margin_ratio_min": 0.06,
                "target_margin_ratio_max": 0.08,
                "target_margin_ratio_confirmed": 0.07,
                "strong_opportunity_target_margin_ratio_min": 0.16,
                "strong_opportunity_target_margin_ratio_max": 0.20,
                "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                "max_margin_ratio_after_scaling": 0.20,
                "min_confirmation_score_for_scaling": 0.60,
                "memory_protected_min_confirmation_score": 0.45,
                "dynamic_concentration_enabled": True,
                "allow_memory_protected_scaling": True,
                "protected_min_sample_count_for_scaling": 5,
                "protected_min_win_rate_for_scaling": 0.60,
                "protected_min_net_pnl_for_scaling": 1000,
                "require_specific_signal_combo_for_strong_scaling": True,
                "require_stop_protection_for_strong_scaling": True,
            }
        }

    def _protected_memory(self, signal_combo="Bullish|Bullish|Neutral"):
        return {
            "combo": {
                "memory_state": "protected",
                "signal_combo": signal_combo,
                "sample_count": 8,
                "win_rate": 0.75,
                "net_pnl": 8000.0,
            }
        }

    def _run_alpha_release_case(self, *, strategy_memory, analyst_signals):
        return _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-03-04",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.01,
            margin_rate=0.10,
            max_position_ratio=0.05,
            market_confirmation={"confirmation_score": 0.82},
            full_config=self._base_config(),
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory=strategy_memory,
            adaptive_policy_state=[],
            analyst_signals=analyst_signals,
        )

    def test_alpha_release_boost_requires_explicit_stop(self):
        ratio, reasons, notes, diagnostics = self._run_alpha_release_case(
            strategy_memory=self._protected_memory(),
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    counter_evidence="basis weakens and price closes below breakout shelf",
                    metadata={
                        "action_evidence_contract": build_test_aec(
                            "technical",
                            ticker="BU",
                            signal="Bullish",
                            side="long",
                            opportunity_state="tradeable_candidate",
                            trigger_valid=True,
                            current_trigger_confirmed=True,
                            invalidation_present=True,
                            extra={
                                "invalidation_level": 3200.0,
                                "position_invalidation_level": None,
                                "exit_hint": "",
                                "atr_stop_distance": None,
                                "expected_horizon_days": 0,
                            },
                        )
                    },
                )
            ],
        )

        target = diagnostics["capital_utilization_target"]
        self.assertEqual(target["alpha_release_tier"], "normal")
        self.assertEqual(target["target_mode"], "confirmed_observation")
        self.assertFalse(target["high_quality_memory"])
        self.assertNotIn("capital_utilization_memory_protected", reasons)
        self.assertLessEqual(abs(ratio) * 0.10, 0.08)
        self.assertIn(
            "missing_explicit_stop_for_alpha_release_boost",
            target["alpha_release"]["limiting_reasons"],
        )

    def test_specific_memory_with_stop_can_boost_alpha_release(self):
        ratio, reasons, notes, diagnostics = self._run_alpha_release_case(
            strategy_memory=self._protected_memory(),
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    invalidation_level=3200.0,
                    atr_stop_distance=80.0,
                )
            ],
        )

        target = diagnostics["capital_utilization_target"]
        self.assertEqual(target["alpha_release_tier"], "boost")
        self.assertEqual(target["target_mode"], "alpha_release_boost")
        self.assertTrue(target["high_quality_memory"])
        self.assertGreater(abs(ratio), 0.05)
        self.assertIn("capital_utilization_memory_protected", reasons)
        self.assertIn("alpha_release_boost", reasons)

    def test_soft_risk_tags_do_not_skip_alpha_release_with_same_scope_memory(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-03-04",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.01,
            margin_rate=0.10,
            max_position_ratio=0.05,
            market_confirmation={"confirmation_score": 0.82},
            full_config=self._base_config(),
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory=self._protected_memory(),
            adaptive_policy_state=[],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    invalidation_level=3200.0,
                    atr_stop_distance=80.0,
                )
            ],
            pre_control_reasons=[
                "market_confirmation_quality_gate",
                "single_high_quality_probe_only",
            ],
        )

        self.assertGreater(abs(ratio), 0.05)
        self.assertNotIn("capital_utilization_soft_limit_respected", reasons)
        self.assertIn("capital_utilization_memory_protected", reasons)
        self.assertTrue(diagnostics["capital_utilization_soft_risk_arbiter"]["release_evidence_present"])
        self.assertEqual(diagnostics["capital_utilization_soft_risk_arbiter"]["allowed_to_continue"], True)

    def test_generic_memory_caps_alpha_release_to_normal(self):
        ratio, reasons, notes, diagnostics = self._run_alpha_release_case(
            strategy_memory=self._protected_memory(signal_combo="*"),
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    invalidation_level=3200.0,
                    atr_stop_distance=80.0,
                )
            ],
        )

        target = diagnostics["capital_utilization_target"]
        self.assertEqual(target["alpha_release_tier"], "normal")
        self.assertEqual(target["target_mode"], "confirmed_observation")
        self.assertFalse(target["high_quality_memory"])
        self.assertNotIn("capital_utilization_memory_protected", reasons)
        self.assertIn(
            "generic_memory_cannot_trigger_alpha_release_boost",
            target["alpha_release"]["limiting_reasons"],
        )

    def test_global_learned_underperformance_does_not_demote_specific_protected_memory(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.06,
            current_ratio=0.0,
            current_margin_ratio=0.01,
            margin_rate=0.10,
            max_position_ratio=0.08,
            market_confirmation={"confirmation_score": 0.85},
            full_config=self._base_config(),
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory=self._protected_memory(),
            adaptive_policy_state=[
                {
                    "policy_action": "demote",
                    "policy_type": "learned_vs_unlearned",
                    "multiplier": 0.50,
                    "sample_count": 4,
                    "confidence_score": 0.80,
                    "reason": "learned trades underperformed unlearned benchmark",
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    invalidation_level=3200.0,
                    atr_stop_distance=80.0,
                )
            ],
        )

        self.assertNotIn("learned_underperformance_policy", reasons)
        self.assertIn("capital_utilization_memory_protected", reasons)
        self.assertGreater(ratio, 0.03)

    def test_scoped_learned_underperformance_demotes_matching_template(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.06,
            current_ratio=0.0,
            current_margin_ratio=0.01,
            margin_rate=0.10,
            max_position_ratio=0.08,
            market_confirmation={"confirmation_score": 0.85},
            full_config=self._base_config(),
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory=self._protected_memory(),
            adaptive_policy_state=[
                {
                    "ticker": "ZZ",
                    "side": "long",
                    "setup_type": "long_reversal_confirmed_short",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "policy_action": "demote",
                    "policy_type": "learned_vs_unlearned",
                    "multiplier": 0.50,
                    "sample_count": 4,
                    "confidence_score": 0.80,
                    "reason": "learned alpha-release trades underperformed same-scope benchmark",
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    setup_type="reversal_confirmed",
                    horizon_class="short",
                    market_regime="trend",
                    invalidation_level=3200.0,
                    atr_stop_distance=80.0,
                )
            ],
        )

        self.assertAlmostEqual(ratio, 0.03)
        self.assertIn("learned_underperformance_policy", reasons)
        self.assertEqual(diagnostics["capital_utilization_skip"], "learned_underperformance_policy")

    def test_scoped_fast_loss_sentinel_demotes_matching_template(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="RB",
            trading_date="2025-03-17",
            position_ratio=-0.06,
            current_ratio=0.0,
            current_margin_ratio=0.01,
            margin_rate=0.10,
            max_position_ratio=0.08,
            market_confirmation={"confirmation_score": 0.72},
            full_config=self._base_config(),
            signal_combo=("Bearish", "Neutral", "Neutral"),
            strategy_memory={},
            adaptive_policy_state=[
                {
                    "ticker": "RB",
                    "side": "short",
                    "setup_type": "short_trend_pullback_short",
                    "horizon_class": "medium",
                    "market_regime": "choppy",
                    "policy_action": "cap",
                    "policy_type": "fast_loss_sentinel",
                    "multiplier": 0.50,
                    "sample_count": 2,
                    "confidence_score": 0.80,
                    "reason": "same-scope probe lost quickly twice",
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BEARISH,
                    confidence=0.70,
                    setup_type="trend_pullback",
                    horizon_class="medium",
                    market_regime="choppy",
                    invalidation_level=3600.0,
                    atr_stop_distance=80.0,
                )
            ],
        )

        self.assertAlmostEqual(ratio, -0.03)
        self.assertIn("learned_underperformance_policy", reasons)
        learned = diagnostics["capital_utilization_learning"]["learned_demote_record"]
        self.assertEqual(learned["policy_type"], "fast_loss_sentinel")

    def test_generic_fast_loss_sentinel_is_not_a_blanket_demote(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="RB",
            trading_date="2025-03-17",
            position_ratio=-0.06,
            current_ratio=0.0,
            current_margin_ratio=0.01,
            margin_rate=0.10,
            max_position_ratio=0.08,
            market_confirmation={"confirmation_score": 0.72},
            full_config=self._base_config(),
            signal_combo=("Bearish", "Neutral", "Neutral"),
            strategy_memory={},
            adaptive_policy_state=[
                {
                    "ticker": "*",
                    "side": "short",
                    "setup_type": "short_trend_pullback_short",
                    "policy_action": "cap",
                    "policy_type": "fast_loss_sentinel",
                    "multiplier": 0.50,
                    "sample_count": 2,
                    "confidence_score": 0.80,
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BEARISH,
                    confidence=0.70,
                    setup_type="trend_pullback",
                    horizon_class="medium",
                    market_regime="choppy",
                    invalidation_level=3600.0,
                    atr_stop_distance=80.0,
                )
            ],
        )

        self.assertNotIn("learned_underperformance_policy", reasons)
        self.assertNotEqual(diagnostics.get("capital_utilization_skip"), "learned_underperformance_policy")


class PMExpectancyTradeQualificationRegressionTest(unittest.TestCase):
    def test_alpha_setup_flat_hold_stays_observe_while_existing_position_hold_stays_hold(self):
        from tools.common.alpha_setup import classify_action

        self.assertEqual(
            classify_action("hold", current_lots=0, target_lots=0),
            "observe",
        )
        self.assertEqual(
            classify_action("hold", current_lots=2, target_lots=2),
            "hold",
        )

    def _base_scorecard(self, layer="tradeable_candidate"):
        return {
            "preferred_side": "long",
            "long": {
                "final_state": layer,
                "gating_failures": [],
                "score": 0.62,
                "max_setup_quality": 0.66,
            }
        }

    @staticmethod
    def _attach_episode_returns(value):
        if isinstance(value, dict):
            payload = value.get("payload")
            if (
                value.get("action_name")
                and value.get("reward_mean") is not None
                and value.get("mean_return_on_notional") is None
                and (
                    not isinstance(payload, dict)
                    or payload.get("mean_return_on_notional") is None
                )
            ):
                reward_mean = float(value.get("reward_mean") or 0.0)
                target = payload if isinstance(payload, dict) else value
                target["mean_return_on_notional"] = reward_mean / 100000.0
                worst_reward = (
                    payload.get("worst_reward")
                    if isinstance(payload, dict)
                    else None
                )
                target["worst_return_on_notional"] = float(
                    worst_reward or value.get("worst_reward") or reward_mean
                ) / 100000.0
            for item in value.values():
                PMExpectancyTradeQualificationRegressionTest._attach_episode_returns(item)
        elif isinstance(value, list):
            for item in value:
                PMExpectancyTradeQualificationRegressionTest._attach_episode_returns(item)

    def setUp(self):
        original_apply = _apply_alpha_setup_ev_position_control
        original_seed = _positive_open_action_value_seed

        def apply_with_canonical_episode_returns(**kwargs):
            self._attach_episode_returns(kwargs.get("alpha_setup_action_values"))
            return original_apply(**kwargs)

        def seed_with_canonical_episode_returns(**kwargs):
            self._attach_episode_returns(kwargs.get("alpha_setup_action_values"))
            return original_seed(**kwargs)

        apply_patcher = patch(
            f"{__name__}._apply_alpha_setup_ev_position_control",
            side_effect=apply_with_canonical_episode_returns,
        )
        seed_patcher = patch(
            f"{__name__}._positive_open_action_value_seed",
            side_effect=seed_with_canonical_episode_returns,
        )
        apply_patcher.start()
        seed_patcher.start()
        self.addCleanup(apply_patcher.stop)
        self.addCleanup(seed_patcher.stop)

    def test_alpha_setup_diagnostics_keep_side_priority_separate_from_full_market_rank(self):
        scorecard = self._base_scorecard(layer="tradeable_candidate")
        scorecard["long"].update(
            {
                "side_priority": 1,
                "ticker_side_priority": 1,
                "side_priority_score": 0.64,
                "candidate_quality": 0.64,
                "candidate_layer_hint": "tradeable_candidate",
                "capital_priority_score": 0.99,
                "capital_priority_tier": 3,
            }
        )

        _ratio, _reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="P",
            position_ratio=0.05,
            current_ratio=0.0,
            opportunity_scorecard=scorecard,
            alpha_setup_profiles=[],
            alpha_setup_action_values=[],
            analyst_signals=[],
            market_confirmation={"confirmation_score": 0.55},
            full_config={},
            max_position_ratio=0.05,
        )

        detail = diagnostics["alpha_setup_ev_fusion"]
        self.assertEqual(detail["side_priority"], 1)
        self.assertEqual(detail["ticker_side_priority"], 1)
        self.assertEqual(detail["side_priority_score"], 0.64)
        self.assertEqual(detail["candidate_quality"], 0.64)
        self.assertEqual(detail["candidate_layer_hint"], "tradeable_candidate")
        self.assertNotIn("capital_priority_score", detail)
        self.assertNotIn("capital_priority_tier", detail)

    def test_hold_exit_learning_no_change_completion_adds_explanation_without_changing_trade(self):
        contract = {
            "ticker": "EB",
            "final_action": "hold",
            "current_lots": -12,
            "target_lots": -12,
            "lots_delta": 0,
            "reason_codes": ["position_matched", "reverse_requires_stronger_evidence"],
            "learning_used": {
                "alpha_setup_action_values": [
                    {
                        "ticker": "EB",
                        "side": "short",
                        "action_name": "exit",
                        "action_preference": "positive_candidate_exit",
                        "action_value_lane": "exit",
                        "learning_lane": "exit",
                        "memory_side_role": "current_position_side",
                        "consumer_scope": "pm_learning",
                    }
                ]
            },
        }

        completed = _finalize_hold_exit_learning_explanation(contract)

        self.assertEqual(completed["final_action"], "hold")
        self.assertEqual(completed["current_lots"], -12)
        self.assertEqual(completed["target_lots"], -12)
        self.assertEqual(completed["lots_delta"], 0)
        self.assertIn("holding_period_control", completed["reason_codes"])
        self.assertIn("position_matched", completed["reason_codes"])

    def test_candidate_adaptive_policy_cannot_release_trade_authority(self):
        rows, trace = filter_adaptive_policy_state_for_pm([
            {
                "ticker": "RB",
                "side": "long",
                "setup_type": "trend_breakout_setup",
                "horizon_class": "short",
                "market_regime": "trend",
                "policy_type": "alpha_promotion",
                "policy_action": "protect",
                "confidence_score": 0.90,
                "sample_count": 9,
                "payload": {
                    "status": "candidate",
                    "next_round_memory_contract": {
                        "status": "candidate",
                        "maturity_state": "candidate",
                        "position_authority": "analysis_or_watchlist_only",
                        "max_position_impact": "no_direct_position_impact",
                    },
                },
            }
        ])

        self.assertEqual(rows, [])
        self.assertEqual(trace["blocked_count"], 1)
        self.assertEqual(trace["blocked_examples"][0]["reason"], "memory_contract_forbids_position_impact")

    def test_fast_candidate_alpha_remains_probe_only(self):
        rows, trace = filter_adaptive_policy_state_for_pm([
            {
                "ticker": "RB",
                "side": "long",
                "setup_type": "trend_breakout_setup",
                "horizon_class": "short",
                "market_regime": "trend",
                "policy_type": "fast_candidate_alpha",
                "policy_action": "probe",
                "confidence_score": 0.80,
                "sample_count": 4,
                "payload": {
                    "evidence": {"source": "missed_opportunity_counterfactual"},
                    "next_round_memory_contract": {
                        "memory_type": "missed_alpha_accountability",
                        "status": "candidate",
                        "maturity_state": "fast_candidate_alpha",
                        "position_authority": "probe_or_small_setup_only_after_current_confirmation",
                        "max_position_impact": "same_scope_probe_or_small_trade_only",
                    },
                },
            }
        ])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["adaptive_policy_runtime_decision"]["decision"], "candidate_probe_only")
        self.assertEqual(trace["allowed_count"], 1)

    def test_capital_allocator_filters_unvalidated_adaptive_release_policy(self):
        row = adaptive_policy_record(
            [
                {
                    "ticker": "RB",
                    "side": "long",
                    "setup_type": "trend_breakout_setup",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "policy_type": "alpha_promotion",
                    "policy_action": "allow",
                    "confidence_score": 0.90,
                    "sample_count": 6,
                    "payload": {
                        "status": "candidate",
                        "next_round_memory_contract": {
                            "status": "candidate",
                            "position_authority": "probe_or_small_setup_only_after_current_confirmation",
                        },
                    },
                }
            ],
            {"allow"},
            policy_types={"alpha_promotion"},
        )

        self.assertEqual(row, {})

    def test_adaptive_policy_safety_tolerates_dirty_sample_count(self):
        rows, trace = filter_adaptive_policy_state_for_pm([
            {
                "ticker": "RB",
                "side": "long",
                "setup_type": "trend_breakout_setup",
                "horizon_class": "short",
                "market_regime": "trend",
                "policy_type": "fast_candidate_alpha",
                "policy_action": "probe",
                "confidence_score": 0.75,
                "sample_count": "not-a-number",
                "payload": {
                    "evidence": {"source": "missed_opportunity_counterfactual"},
                    "next_round_memory_contract": {
                        "memory_type": "missed_alpha_accountability",
                        "status": "candidate",
                        "maturity_state": "fast_candidate_alpha",
                        "position_authority": "probe_or_small_setup_only_after_current_confirmation",
                        "max_position_impact": "same_scope_probe_or_small_trade_only",
                    },
                },
            }
        ])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["adaptive_policy_runtime_decision"]["sample_count"], 0)
        self.assertEqual(trace["allowed_count"], 1)

    def test_fast_candidate_alpha_rejects_profile_origin(self):
        rows, trace = filter_adaptive_policy_state_for_pm([
            {
                "ticker": "RB",
                "side": "long",
                "setup_type": "trend_breakout_setup",
                "horizon_class": "short",
                "market_regime": "trend",
                "policy_type": "fast_candidate_alpha",
                "policy_action": "probe",
                "sample_count": 4,
                "payload": {
                    "evidence": {"source": "alpha_setup_profile"},
                    "next_round_memory_contract": {
                        "memory_type": "fast_candidate_alpha",
                        "status": "candidate",
                        "maturity_state": "alpha_setup_fast_candidate",
                        "position_authority": "analysis_prior_only",
                        "max_position_impact": "no_direct_position_impact",
                    },
                },
            }
        ])

        self.assertEqual(rows, [])
        self.assertEqual(trace["blocked_count"], 1)

    def test_counterfactual_no_trade_cannot_release_mature_alpha(self):
        rows, trace = filter_adaptive_policy_state_for_pm([
            {
                "ticker": "CF",
                "side": "long",
                "setup_type": "trend_breakout_setup",
                "horizon_class": "short",
                "market_regime": "trend",
                "policy_type": "alpha_promotion",
                "policy_action": "protect",
                "sample_count": 8,
                "payload": {
                    "status": "applied",
                    "evidence": {"source": "no_trade_counterfactual_results"},
                    "next_round_memory_contract": {
                        "memory_type": "alpha_promotion",
                        "status": "applied",
                        "maturity_state": "validated_counterfactual_alpha_memory",
                        "position_authority": "pm_auditor_conditioned",
                        "max_position_impact": "may_support_alpha_scaling_inside_20pct_cap",
                    },
                },
            }
        ])

        self.assertEqual(rows, [])
        self.assertEqual(
            trace["blocked_examples"][0]["reason"],
            "counterfactual_no_trade_cannot_create_mature_alpha_promotion",
        )

    def test_final_action_contract_is_single_structured_trade_truth(self):
        contract = _build_final_action_contract(
            ticker="RB",
            current_lots=0,
            target_lots=5,
            position_ratio=0.04,
            margin_required=40000.0,
            account_equity=5000000.0,
            lots_to_trade=5,
            lots_to_trade_reason="tradable",
            recommendation_intent={"action": "open_long", "lots": 5, "action_type": "open"},
            final_entry_authority={
                "authority_type": "real_budget_entry",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "max_allowed_margin_ratio": 0.12,
                "reason_codes": ["qualified_positive_expectancy"],
            },
            control_reasons=["positive_open_action_value_seed", "qualified_positive_expectancy"],
            control_diagnostics={
                "positive_open_action_value_seed": {
                    "target_side": "long",
                    "seed_position_ratio": 0.04,
                    "selected_action_value": {"reward_mean": 1800.0},
                }
            },
            opportunity_scorecard={
                "preferred_side": "long",
                "long": {
                    "final_state": "tradeable_candidate",
                    "score": 0.72,
                    "opportunity_score": 0.72,
                    "opportunity_score_components": {"setup_quality": 0.12},
                    "side_priority": 1,
                    "ticker_side_priority": 1,
                    "capital_allocation_reason": "ranked_deployable_candidate_with_complete_current_evidence",
                    "learning_adjustment_summary": {"effect": "boosted"},
                },
            },
            market_confirmation={"confirmation_score": 0.70, "conflicts": []},
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "canonical_action_value": True,
                    "canonical_action_family": "open_add_new_risk",
                    "consumer_scope": "pm_learning",
                    "action_value_lane": "open",
                    "learning_lane": "open",
                    "memory_side_role": "target_side",
                    "action_preference": "positive_candidate_open",
                    "canonical_action_value_source": "canonical_action_value",
                    "reward_mean": 1800.0,
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                }
            ],
        )

        self.assertEqual(contract["contract_version"], "agentquant.final_action.v1")
        self.assertEqual(contract["final_action"], "open_real")
        self.assertTrue(contract["single_source_of_trade_truth"])
        self.assertTrue(contract["candidate_sources_do_not_bypass_contract"])
        self.assertEqual(contract["authority_type"], "real_budget_entry")
        self.assertEqual(contract["target_lots"], 5)
        self.assertNotIn("action_candidates", contract)
        self.assertEqual(contract["evidence_used"]["opportunity_score"], 0.72)
        self.assertNotIn("opportunity_rank", contract["evidence_used"])
        self.assertEqual(contract["evidence_used"]["side_priority"], 1)
        self.assertNotIn("capital_allocation_reason", contract["evidence_used"])
        self.assertEqual(contract["learning_used"]["learning_adjustment_summary"]["effect"], "boosted")
        self.assertEqual(
            contract["learning_used"]["pm_lifecycle_learning_trace"]["contract_lifecycle_port"],
            "open_add_new_risk",
        )
        self.assertIn("open", contract["learning_used"]["pm_lifecycle_learning_trace"]["used_lanes"])
        self.assertEqual(
            contract["learning_used"]["pm_lifecycle_learning_impact_delta"]["lots_delta"],
            5,
        )
        self.assertNotIn("opportunity_score", contract)

    def test_final_action_contract_carries_execution_contract_as_trade_truth(self):
        contract = _build_final_action_contract(
            ticker="RB",
            current_lots=0,
            target_lots=-4,
            position_ratio=-0.04,
            margin_required=50000.0,
            account_equity=5000000.0,
            lots_to_trade=4,
            lots_to_trade_reason="tradable",
            recommendation_intent={"action": "open_short", "lots": 4, "action_type": "open"},
            final_entry_authority={
                "authority_type": "real_budget_entry",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "max_allowed_margin_ratio": 0.12,
            },
            control_reasons=["execution_action_value_preference"],
            control_diagnostics={},
            opportunity_scorecard={"preferred_side": "short", "short": {"final_state": "tradeable_candidate"}},
            market_confirmation={"confirmation_score": 0.72, "conflicts": []},
            alpha_setup_action_values=[],
            execution_contract_fields={
                "contract_version": "agentquant.execution_contract.v1",
                "execution_profile": "vwap_confirmed",
                "trigger_source": "execution_action_value_vwap",
                "requires_intraday_confirmation": True,
            },
        )

        self.assertEqual(contract["execution_profile"], "vwap_confirmed")
        self.assertEqual(contract["trigger_source"], "execution_action_value_vwap")
        trace = contract["learning_used"]["pm_lifecycle_learning_trace"]
        impact = contract["learning_used"]["pm_lifecycle_learning_impact_delta"]
        self.assertEqual(trace["contract_lifecycle_port"], "open_add_new_risk")
        self.assertFalse(trace["execution_profile_signal_direct_to_rank"])
        self.assertFalse(impact["execution_profile_learning_direct_to_rank"])
        self.assertTrue(contract["single_source_of_trade_truth"])

    def test_final_contract_strictly_separates_decision_and_execution_learning_rows(self):
        common = {
            "ticker": "RB",
            "side": "long",
            "canonical_action_value": True,
            "consumer_scope": "pm_learning",
            "reward_source": "real_trade",
            "evidence_scope": "exact_real_state",
            "reward_mean": 800.0,
            "reward_sum": 800.0,
        }
        contract = _build_final_action_contract(
            ticker="RB",
            current_lots=0,
            target_lots=2,
            position_ratio=0.01,
            margin_required=12000.0,
            account_equity=5000000.0,
            lots_to_trade=2,
            lots_to_trade_reason="tradable",
            recommendation_intent={"action": "open_long", "lots": 2, "action_type": "open"},
            final_entry_authority={"authority_type": "real_budget_entry"},
            control_reasons=[],
            control_diagnostics={},
            opportunity_scorecard={"preferred_side": "long", "long": {"final_state": "tradeable_candidate"}},
            market_confirmation={"confirmation_score": 0.70},
            alpha_setup_action_values=[
                {
                    **common,
                    "id": "rb-open",
                    "action_name": "open",
                    "canonical_action_family": "open_add_new_risk",
                    "action_value_lane": "open",
                    "learning_lane": "open",
                    "memory_side_role": "target_side",
                    "action_preference": "positive_candidate_open",
                },
                {
                    **common,
                    "id": "rb-execution",
                    "action_name": "execution",
                    "canonical_action_family": "execution",
                    "action_value_lane": "execution",
                    "learning_lane": "execution",
                    "memory_side_role": "historical_sample_side",
                    "action_preference": "positive_candidate_execution",
                },
            ],
        )

        learning_used = contract["learning_used"]
        trace = learning_used["pm_lifecycle_learning_trace"]
        self.assertEqual(
            [row["id"] for row in learning_used["alpha_setup_action_values"]],
            ["rb-open"],
        )
        self.assertEqual([row["id"] for row in trace["decision_learning_rows"]], ["rb-open"])
        self.assertEqual(trace["trigger_profile_learning_rows"], [])
        self.assertIn(
            "rb-execution",
            {row.get("id") for row in trace["rejected_learning"]},
        )
        self.assertTrue(
            trace["pm_lifecycle_learning_router"][
                "complete_step4_pool_routed_before_formal_selection"
            ]
        )

    def test_conditional_zero_to_nonzero_open_uses_open_learning_and_keeps_trigger_wait(self):
        contract = _build_final_action_contract(
            ticker="HC",
            current_lots=0,
            target_lots=-1,
            position_ratio=-0.01,
            margin_required=12000.0,
            account_equity=5000000.0,
            lots_to_trade=1,
            lots_to_trade_reason="conditional_monitor",
            recommendation_intent={"action": "open_short", "lots": 1, "action_type": "open"},
            final_entry_authority={
                "authority_type": "exploration_probe",
                "conditional_trigger_authority": True,
                "requires_intraday_confirmation": True,
                "can_execute_without_intraday_trigger": False,
                "max_allowed_margin_ratio": 0.02,
                "reason_codes": ["pm_watch_for_trigger_probe_cap", "conditional_trigger_authority"],
            },
            control_reasons=["pm_watch_for_trigger_probe_cap", "conditional_trigger_authority"],
            control_diagnostics={
                "conditional_monitor_probe_seed": {
                    "side": "short",
                    "ratio": -0.01,
                    "status": "candidate_routed_to_conditional_monitor",
                    "requires_intraday_confirmation": True,
                    "scorecard": {
                        "final_state": "watch_for_trigger",
                        "setup_quality_ok": True,
                        "trigger_valid": False,
                        "current_trigger_confirmed": False,
                        "invalidation_present": True,
                        "entry_trigger": "wait for post-open break below support",
                    },
                },
                "conditional_monitor_probe_plan": {
                    "allowed": True,
                    "decision": "allow_conditional_monitor_probe",
                    "target_lots": -1,
                },
            },
            opportunity_scorecard={
                "preferred_side": "short",
                "short": {
                    "final_state": "watch_for_trigger",
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "invalidation_present": True,
                    "entry_trigger": "wait for post-open break below support",
                },
            },
            market_confirmation={"confirmation_score": 0.52, "conflicts": []},
            alpha_setup_action_values=[
                {
                    "id": "hc-short-open",
                    "ticker": "HC",
                    "side": "short",
                    "action_name": "open",
                    "canonical_action_value": True,
                    "canonical_action_family": "open_add_new_risk",
                    "consumer_scope": "pm_learning",
                    "learning_lane": "open",
                    "action_value_lane": "open",
                    "memory_side_role": "target_side",
                    "action_preference": "positive_candidate_open",
                    "canonical_action_value_source": "canonical_action_value",
                    "reward_mean": 335.46,
                    "reward_sum": 335.46,
                    "reward_source": "real_trade",
                    "evidence_scope": "exact_real_state",
                }
            ],
        )

        self.assertEqual(contract["final_action"], "open_probe")
        self.assertNotIn("action_candidates", contract)
        self.assertEqual(
            contract["learning_used"]["pm_lifecycle_learning_trace"]["contract_lifecycle_port"],
            "open_add_new_risk",
        )
        self.assertEqual(
            [row["id"] for row in contract["learning_used"]["alpha_setup_action_values"]],
            ["hc-short-open"],
        )
        self.assertTrue(contract["requires_intraday_confirmation"])
        self.assertFalse(contract["can_execute_without_intraday_trigger"])
        self.assertIsNone(
            contract["learning_used"]["pm_lifecycle_learning_impact_delta"]["conditional_monitor_decision"]
        )
        self.assertNotIn("scorecard_current_tradeable_probe_seed", contract["reason_codes"])

    def test_final_action_contract_does_not_claim_unchanged_hold_learning(self):
        contract = _build_final_action_contract(
            ticker="EB",
            current_lots=-12,
            target_lots=-12,
            position_ratio=-0.04,
            margin_required=0.0,
            account_equity=5000000.0,
            lots_to_trade=0,
            lots_to_trade_reason="position_matched",
            recommendation_intent={"action": "hold", "lots": 0, "action_type": "keep"},
            final_entry_authority={"authority_type": "not_applicable"},
            control_reasons=["holding_period_control"],
            control_diagnostics={
                "holding_rebalance_control": {
                    "decision": "continue_hold_with_learning_explanation",
                    "pre_control_ratio": -0.04,
                    "final_ratio": -0.04,
                    "lifecycle_classification": "profitable_hold",
                },
            },
            opportunity_scorecard={"preferred_side": "short", "short": {"final_state": "hold_candidate"}},
            market_confirmation={"confirmation_score": 0.55, "conflicts": []},
            alpha_setup_action_values=[
                {
                    "id": "eb-hold-not-causal",
                    "ticker": "EB",
                    "side": "short",
                    "action_name": "hold",
                    "canonical_action_value": True,
                    "canonical_action_family": "hold",
                    "consumer_scope": "pm_learning",
                    "learning_lane": "hold",
                    "action_value_lane": "hold",
                    "memory_side_role": "current_position_side",
                    "action_preference": "positive_candidate_hold",
                    "canonical_action_value_source": "canonical_action_value",
                    "reward_mean": 1000.0,
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                }
            ],
        )

        trace = contract["learning_used"]["pm_lifecycle_learning_trace"]
        impact = contract["learning_used"]["pm_lifecycle_learning_impact_delta"]
        self.assertEqual(contract["final_action"], "hold")
        self.assertEqual(trace["contract_lifecycle_port"], "hold")
        self.assertEqual(trace["used_lanes"], [])
        self.assertEqual(trace["decision_learning_rows"], [])
        self.assertEqual(contract["learning_used"]["alpha_setup_action_values"], [])
        self.assertEqual(
            impact["hold_decision"],
            "continue_hold_with_learning_explanation",
        )
        self.assertFalse(impact["hold_changes_position"])
        self.assertEqual(impact["position_ratio_delta"], 0.0)
        self.assertNotIn("opportunity_rank", contract["evidence_used"])

    def test_final_action_contract_routes_only_causal_reduce_exit_learning(self):
        contract = _build_final_action_contract(
            ticker="SR",
            current_lots=8,
            target_lots=3,
            position_ratio=0.015,
            margin_required=0.0,
            account_equity=5000000.0,
            lots_to_trade=5,
            lots_to_trade_reason="protective_reduce_after_tail_loss",
            recommendation_intent={"action": "close_long", "lots": 5, "action_type": "decrease"},
            final_entry_authority={"authority_type": "not_applicable"},
            control_reasons=["hold_exit_action_value_protection"],
            control_diagnostics={
                "winning_template_continuation": {
                    "decision": "learned_hold_exit_reduce",
                    "selected_action_value": {"id": "sr-reduce-used"},
                    "pre_control_ratio": 0.04,
                    "final_ratio": 0.015,
                },
            },
            opportunity_scorecard={"preferred_side": "long", "long": {"final_state": "risk_reduction_candidate"}},
            market_confirmation={"confirmation_score": 0.42, "conflicts": ["trend_fading"]},
            alpha_setup_action_values=[
                {
                    "id": "sr-reduce-used",
                    "ticker": "SR",
                    "side": "long",
                    "action_name": "reduce",
                    "canonical_action_value": True,
                    "canonical_action_family": "reduce_exit",
                    "consumer_scope": "pm_learning",
                    "learning_lane": "reduce",
                    "action_value_lane": "reduce",
                    "memory_side_role": "current_position_side",
                    "action_preference": "positive_candidate_exit",
                    "canonical_action_value_source": "canonical_action_value",
                    "reward_mean": 1000.0,
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                },
                {
                    "id": "sr-reduce-retrieved-only",
                    "ticker": "SR",
                    "side": "long",
                    "action_name": "reduce",
                    "canonical_action_value": True,
                    "canonical_action_family": "reduce_exit",
                    "consumer_scope": "pm_learning",
                    "learning_lane": "reduce",
                    "action_value_lane": "reduce",
                    "memory_side_role": "current_position_side",
                    "action_preference": "positive_candidate_exit",
                    "canonical_action_value_source": "canonical_action_value",
                    "reward_mean": 500.0,
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                }
            ],
        )

        trace = contract["learning_used"]["pm_lifecycle_learning_trace"]
        impact = contract["learning_used"]["pm_lifecycle_learning_impact_delta"]
        self.assertEqual(contract["final_action"], "reduce")
        self.assertEqual(trace["contract_lifecycle_port"], "reduce_exit")
        self.assertIn("reduce", trace["used_lanes"])
        self.assertEqual(
            [row["id"] for row in trace["decision_learning_rows"]],
            ["sr-reduce-used"],
        )
        self.assertEqual(
            [row["id"] for row in contract["learning_used"]["alpha_setup_action_values"]],
            ["sr-reduce-used"],
        )
        self.assertEqual(
            impact["reduce_exit_decision"],
            "learned_hold_exit_reduce",
        )
        self.assertTrue(impact["reduce_exit_changes_position"])
        self.assertAlmostEqual(impact["position_ratio_delta"], -0.025)
        self.assertNotIn("opportunity_rank", contract["evidence_used"])

    def test_final_action_contract_preserves_causal_negative_hold_id_for_reduce(self):
        action_value = {
            "id": "sr-negative-hold-used",
            "ticker": "SR",
            "side": "long",
            "action_name": "hold",
            "canonical_action_value": True,
            "canonical_action_family": "hold",
            "consumer_scope": "pm_learning",
            "learning_lane": "hold",
            "action_value_lane": "hold",
            "memory_side_role": "current_position_side",
            "action_preference": "negative_hold_revalidate",
            "canonical_action_value_source": "canonical_action_value",
            "reward_mean": -1200.0,
            "reward_source": "trade_episode",
            "evidence_scope": "exact_real_state",
        }
        contract = _build_final_action_contract(
            ticker="SR",
            current_lots=8,
            target_lots=3,
            position_ratio=0.015,
            margin_required=0.0,
            account_equity=5000000.0,
            lots_to_trade=5,
            lots_to_trade_reason="hold_exit_action_value_protection",
            recommendation_intent={
                "action": "close_long",
                "lots": 5,
                "action_type": "decrease",
            },
            final_entry_authority={"authority_type": "not_applicable"},
            control_reasons=["hold_exit_action_value_protection"],
            control_diagnostics={
                "winning_template_continuation": {
                    "decision": "learned_hold_exit_reduce",
                    "selected_action_value": {"id": "sr-negative-hold-used"},
                    "action_value_preference": "bad_hold",
                    "pre_control_ratio": 0.04,
                    "final_ratio": 0.015,
                },
                "holding_rebalance_control": {
                    "decision": "allow_same_side_rebalance",
                    "raw_target_ratio": 0.015,
                    "final_target_ratio": 0.015,
                },
            },
            opportunity_scorecard={
                "preferred_side": "long",
                "long": {"final_state": "risk_reduction_candidate"},
            },
            market_confirmation={
                "confirmation_score": 0.42,
                "conflicts": ["trend_fading"],
            },
            alpha_setup_action_values=[action_value],
        )

        trace = contract["learning_used"]["pm_lifecycle_learning_trace"]
        impact = contract["learning_used"]["pm_lifecycle_learning_impact_delta"]
        self.assertEqual(contract["final_action"], "reduce")
        self.assertEqual(
            [row["id"] for row in trace["decision_learning_rows"]],
            ["sr-negative-hold-used"],
        )
        self.assertEqual(
            [row["id"] for row in contract["learning_used"]["alpha_setup_action_values"]],
            ["sr-negative-hold-used"],
        )
        self.assertIn("hold", trace["accepted_learning_lanes"])
        self.assertEqual(
            trace["reduce_exit_learning_decision"]["decision"],
            "learned_hold_exit_reduce",
        )
        self.assertTrue(impact["reduce_exit_changes_position"])
        self.assertAlmostEqual(impact["position_ratio_delta"], -0.025)

    def test_final_action_contract_hard_exit_does_not_claim_retrieved_learning(self):
        action_value = {
            "id": "sr-exit-not-causal",
            "ticker": "SR",
            "side": "long",
            "action_name": "exit",
            "canonical_action_value": True,
            "canonical_action_family": "reduce_exit",
            "consumer_scope": "pm_learning",
            "learning_lane": "exit",
            "action_value_lane": "exit",
            "memory_side_role": "current_position_side",
            "action_preference": "positive_candidate_exit",
            "canonical_action_value_source": "canonical_action_value",
            "reward_mean": 1000.0,
            "reward_source": "trade_episode",
            "evidence_scope": "exact_real_state",
        }
        cases = (
            ("exit_opening_fac_position_invalidation", "position_lifecycle_failed", 0, 0.0, "exit"),
            ("exit_current_technical_invalidation", "position_lifecycle_failed", 0, 0.0, "exit"),
            ("reduce_fundamental_medium_opposition", "fundamental_medium_opposition", 5, 0.025, "reduce"),
            ("force_exit_failed_position", "position_lifecycle_failed", 0, 0.0, "exit"),
        )
        for decision, hard_reason, target_lots, target_ratio, final_action in cases:
            with self.subTest(decision=decision):
                contract = _build_final_action_contract(
                    ticker="SR",
                    current_lots=8,
                    target_lots=target_lots,
                    position_ratio=target_ratio,
                    margin_required=0.0,
                    account_equity=5000000.0,
                    lots_to_trade=abs(8 - target_lots),
                    lots_to_trade_reason=hard_reason,
                    recommendation_intent={
                        "action": "close_long",
                        "lots": abs(8 - target_lots),
                        "action_type": "close" if target_lots == 0 else "decrease",
                    },
                    final_entry_authority={"authority_type": "not_applicable"},
                    control_reasons=[hard_reason, "hold_exit_action_value_protection"],
                    control_diagnostics={
                        "winning_template_continuation": {
                            "decision": "learned_exit_action_value_protective_exit",
                            "selected_action_value": {"id": "sr-exit-not-causal"},
                            "pre_control_ratio": 0.04,
                            "final_ratio": target_ratio,
                        },
                        "holding_rebalance_control": {
                            "decision": decision,
                            "raw_target_ratio": target_ratio,
                            "final_target_ratio": target_ratio,
                        },
                    },
                    opportunity_scorecard={
                        "preferred_side": "long",
                        "long": {"final_state": "risk_reduction_candidate"},
                    },
                    market_confirmation={"confirmation_score": 0.42, "conflicts": []},
                    alpha_setup_action_values=[action_value],
                )

                trace = contract["learning_used"]["pm_lifecycle_learning_trace"]
                impact = contract["learning_used"]["pm_lifecycle_learning_impact_delta"]
                self.assertEqual(contract["final_action"], final_action)
                self.assertEqual(trace["decision_learning_rows"], [])
                self.assertEqual(contract["learning_used"]["alpha_setup_action_values"], [])
                self.assertEqual(impact["reduce_exit_decision"], decision)
                self.assertTrue(impact["reduce_exit_changes_position"])
                self.assertEqual(impact["position_ratio_delta"], 0.0)

    def test_final_action_contract_attributes_only_effective_alpha_lifecycle_learning(self):
        action_value = {
            "id": "sr-alpha-reduce",
            "ticker": "SR",
            "side": "long",
            "action_name": "reduce",
            "canonical_action_value": True,
            "canonical_action_family": "reduce_exit",
            "consumer_scope": "pm_learning",
            "learning_lane": "reduce",
            "action_value_lane": "reduce",
            "memory_side_role": "current_position_side",
            "action_preference": "positive_candidate_exit",
            "canonical_action_value_source": "canonical_action_value",
            "reward_mean": 1000.0,
            "reward_source": "trade_episode",
            "evidence_scope": "exact_real_state",
        }

        def build(*, action_value_effective: bool):
            return _build_final_action_contract(
                ticker="SR",
                current_lots=8,
                target_lots=4,
                position_ratio=0.02,
                margin_required=0.0,
                account_equity=5000000.0,
                lots_to_trade=4,
                lots_to_trade_reason="alpha_setup_ev_fusion",
                recommendation_intent={
                    "action": "close_long",
                    "lots": 4,
                    "action_type": "decrease",
                },
                final_entry_authority={"authority_type": "not_applicable"},
                control_reasons=["alpha_setup_ev_fusion"],
                control_diagnostics={
                    "alpha_setup_ev_fusion": {
                        "intended_action": "reduce",
                        "selected_action_value": {"id": "sr-alpha-reduce"},
                        "positive_action_value": action_value_effective,
                        "positive_action_value_candidate": False,
                        "negative_action_value": False,
                        "pre_control_ratio": 0.04,
                        "final_ratio": 0.02,
                    },
                    "holding_rebalance_control": {
                        "decision": "allow_same_side_rebalance",
                        "raw_target_ratio": 0.02,
                        "final_target_ratio": 0.02,
                    },
                },
                opportunity_scorecard={
                    "preferred_side": "long",
                    "long": {"final_state": "risk_reduction_candidate"},
                },
                market_confirmation={"confirmation_score": 0.42, "conflicts": []},
                alpha_setup_action_values=[action_value],
            )

        causal = build(action_value_effective=True)
        profile_only = build(action_value_effective=False)
        self.assertEqual(
            [
                row["id"]
                for row in causal["learning_used"]["pm_lifecycle_learning_trace"][
                    "decision_learning_rows"
                ]
            ],
            ["sr-alpha-reduce"],
        )
        self.assertTrue(
            causal["learning_used"]["pm_lifecycle_learning_impact_delta"][
                "reduce_exit_changes_position"
            ]
        )
        self.assertEqual(
            profile_only["learning_used"]["pm_lifecycle_learning_trace"][
                "decision_learning_rows"
            ],
            [],
        )
        self.assertEqual(
            profile_only["learning_used"]["alpha_setup_action_values"],
            [],
        )

    def test_negative_action_value_blocks_repeat_new_entry_without_new_evidence(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="P",
            position_ratio=0.05,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 3,
                    "reward_mean": -1800.0,
                    "reward_sum": -5400.0,
                    "win_rate": 0.0,
                    "confidence_score": 0.60,
                    "action_preference": "cap_reduce_or_revalidate",
                    "max_position_impact": 0.02,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 3,
                        "exact_state_real_trade_sample_count": 3,
                        "amplification_scope_quality": "exact_real_state",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.52),
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation={"confirmation_score": 0.45},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("repeat_loss_watchlist_only", reasons)
        detail = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(detail["repeat_loss_without_new_evidence"])
        self.assertFalse(detail["strong_realtime_evidence"])

    def test_negative_action_value_allows_small_probe_when_new_technical_evidence_exists(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="P",
            position_ratio=0.05,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 3,
                    "reward_mean": -1800.0,
                    "reward_sum": -5400.0,
                    "win_rate": 0.0,
                    "confidence_score": 0.60,
                    "action_preference": "cap_reduce_or_revalidate",
                    "max_position_impact": 0.02,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 3,
                        "exact_state_real_trade_sample_count": 3,
                        "amplification_scope_quality": "exact_real_state",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.62,
                    invalidation_level=9000.0,
                    position_invalidation_level=9000.0,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="breakout above short-term resistance with volume confirmation",
                    entry_quality="acceptable",
                    metadata={
                        "action_evidence_contract": build_test_aec(
                            "technical",
                            ticker="P",
                            signal="Bullish",
                            side="long",
                            confidence=0.62,
                            opportunity_state="tradeable_candidate",
                            trigger_valid=True,
                            current_trigger_confirmed=True,
                            invalidation_present=True,
                        ),
                        "technical_context": {
                            "dominant_direction": "bullish",
                        }
                    },
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation={"confirmation_score": 0.58},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertGreater(abs(ratio), 0.0)
        self.assertLess(abs(ratio), 0.05)
        self.assertNotIn("repeat_loss_watchlist_only", reasons)
        detail = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(detail["strong_realtime_evidence"])

    def test_latest_complete_loss_removes_real_scale_lift_but_keeps_strong_probe(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="RB",
            position_ratio=0.04,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[{
                "ticker": "RB",
                "side": "long",
                "lifecycle_state": "deployable",
                "sample_count": 7,
                "net_pnl": 10000.0,
                "profit_factor": 1.5,
                "win_rate": 0.65,
                "confidence_score": 0.80,
                "max_position_impact": 0.04,
            }],
            alpha_setup_action_values=[{
                "ticker": "RB",
                "side": "long",
                "horizon_class": "short",
                "market_regime": "trend",
                "setup_type": "trend_breakout_setup",
                "action_name": "open",
                "sample_count": 7,
                "reward_mean": 2500.0,
                "reward_sum": 17500.0,
                "win_rate": 0.65,
                "confidence_score": 0.80,
                "action_preference": "positive_candidate_open",
                "max_position_impact": 0.04,
                "reward_source": "trade_episode",
                "evidence_scope": "exact_real_state",
                "mean_return_on_notional": 0.025,
                "worst_return_on_notional": -0.001,
                "latest_complete_episode_return_on_notional": -0.001,
                "latest_complete_episode_date": "2025-03-14",
                "payload": {
                    "reward_source": "trade_episode",
                    "real_trade_reward_count": 7,
                    "exact_state_real_trade_sample_count": 7,
                    "amplification_scope_quality": "exact_real_state",
                    "action_preference": "positive_candidate_open",
                    "mean_return_on_notional": 0.025,
                    "worst_return_on_notional": -0.001,
                    "latest_complete_episode_return_on_notional": -0.001,
                    "latest_complete_episode_date": "2025-03-14",
                },
            }],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.72,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="breakout above opening range with volume confirmation",
                    invalidation_level=3300.0,
                    position_invalidation_level=3300.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    entry_quality="strong",
                    evidence_role="entry_timing",
                    metadata=_canonical_pm_execution_metadata(ticker="RB"),
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.55),
            ],
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        detail = diagnostics["alpha_setup_ev_fusion"]
        self.assertGreater(ratio, 0.0)
        self.assertLess(ratio, 0.04)
        self.assertTrue(detail["latest_complete_episode_loss"])
        self.assertTrue(detail["positive_amplification_suspended"])
        self.assertFalse(detail["positive_action_value"])
        self.assertFalse(detail["positive_profile"])
        self.assertFalse(detail["qualified_positive_expectancy"])
        self.assertNotIn("qualified_positive_expectancy", reasons)

    def test_positive_action_value_marks_qualified_positive_expectancy(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="RB",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 5,
                    "reward_mean": 450.0,
                    "reward_sum": 2250.0,
                    "win_rate": 0.6,
                    "confidence_score": 0.62,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 5,
                        "exact_state_real_trade_sample_count": 5,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.60,
                    invalidation_level=3300.0,
                    position_invalidation_level=3300.0,
                    opportunity_state="tradeable_candidate",
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.55),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation={"confirmation_score": 0.62},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertGreater(ratio, 0.02)
        self.assertIn("qualified_positive_expectancy", reasons)
        self.assertTrue(diagnostics["alpha_setup_ev_fusion"]["qualified_positive_expectancy"])

    def test_researcher_open_action_value_can_promote_pm_authority_and_lots(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="RB",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 6,
                    "reward_mean": 520.0,
                    "reward_sum": 3120.0,
                    "win_rate": 0.67,
                    "confidence_score": 0.72,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 6,
                        "exact_state_real_trade_sample_count": 6,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="breakout above opening range with volume confirmation",
                    invalidation_level=3300.0,
                    position_invalidation_level=3300.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    entry_quality="strong",
                    evidence_role="entry_timing",
                    metadata=_canonical_pm_execution_metadata(ticker="RB"),
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.55),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertGreater(ratio, 0.02)
        self.assertIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(alpha_ev["positive_action_value"])
        self.assertTrue(alpha_ev["qualified_positive_expectancy"])
        self.assertEqual(alpha_ev["matched_action_value_count"], 1)

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )

        self.assertTrue(allowed)
        self.assertEqual(authority["authority_type"], "real_budget_entry")
        self.assertTrue(authority["open_action_evidence"])
        self.assertTrue(authority["strong_current_evidence"])
        self.assertTrue(authority["action_evidence_router"]["open"]["positive_open_action_value"])

    def test_exact_alpha_release_chain_reaches_pm_authority_and_trader_profile(self):
        technical_context = build_technical_context(
            "RB",
            {
                "trend": Signal.BULLISH,
                "macd": Signal.BULLISH,
                "adx": Signal.BULLISH,
                "rsi": Signal.BULLISH,
                "mean_reversion": Signal.NEUTRAL,
            },
            {
                "trend_strength": 31,
                "volatility": 0.18,
                "volume_ratio": 1.15,
                "price_range": 0.03,
            },
        )
        technical_context["current_trigger_confirmed"] = True
        technical_context["action_evidence_contract"] = {
            "entry_trigger": "current breakout above prior high is confirmed",
            "current_trigger_confirmed": True,
            "trigger_valid": True,
            "invalidation_present": True,
        }
        technical = apply_trade_research_contract(
            AnalystSignal(
                agent_name="technical",
                signal=Signal.BULLISH,
                confidence=0.74,
                horizon_class="short",
                business_quality_score=0.78,
                data_coverage_score=0.86,
                opportunity_type="trend_continuation",
                setup_type="rb_trend_breakout_setup",
                entry_timing_signal="breakout",
                entry_trigger="current breakout above prior high is confirmed",
                price_percentile=0.56,
                invalidation_level=3300.0,
                position_invalidation_level=3300.0,
            ),
            technical_context,
            analyst="technical",
            trading_date="2025-03-10",
            ticker="RB",
        )
        self.assertIn(technical.opportunity_state, {"tradeable_candidate", "tradeable_candidate"})
        self.assertTrue(technical.trigger_valid)
        self.assertEqual(technical.entry_timing_signal, "breakout")
        self.assertNotIn("generic_trade_setup", json.dumps(technical.metadata, ensure_ascii=False))
        learning_scope = technical.metadata["action_evidence_contract"]["learning_scope"]
        self.assertEqual(learning_scope["setup_family"], "trend_breakout")
        self.assertEqual(learning_scope["market_regime"], "trend")

        open_action_value = {
            "ticker": "RB",
            "side": "long",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "trend_breakout_setup",
            "action_name": "open",
            "sample_count": 6,
            "reward_mean": 720.0,
            "reward_sum": 4320.0,
            "win_rate": 0.67,
            "confidence_score": 0.74,
            "action_preference": "controlled_open_or_add",
            "max_position_impact": 0.04,
            "payload": {
                "source": "alpha_setup_profile_action_value",
                "real_trade_reward_count": 6,
                "episode_trade_reward_count": 6,
                "exact_state_real_trade_sample_count": 6,
                "amplification_scope_quality": "exact_real_state",
                "action_preference": "positive_candidate_open",
                "reward_source": "trade_episode",
            },
        }
        execution_action_value = {
            "ticker": "RB",
            "side": "long",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "trend_breakout_setup",
            "data_combo": "technical:trend_breakout|execution:breakout",
            "execution_retrieval_key": "RB|breakout|technical_breakout|execution",
            "action_name": "execution",
            "sample_count": 3,
            "reward_mean": -900.0,
            "reward_sum": -2700.0,
            "win_rate": 0.0,
            "confidence_score": 0.62,
            "action_preference": "cap_reduce_or_revalidate",
            "payload": {
                "source": "alpha_setup_profile_action_value",
                "real_trade_reward_count": 3,
                "exact_state_real_trade_sample_count": 3,
                "amplification_scope_quality": "exact_real_state",
            },
        }
        scorecard = {
            "preferred_side": "long",
            "long": {
                "final_state": technical.opportunity_state,
                "gating_failures": [],
                "score": 0.72,
                "confidence": 0.74,
                "max_setup_quality": technical.setup_quality_score,
            },
        }
        market_confirmation = {"confirmation_score": 0.72, "conflicts": []}
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="RB",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=scorecard,
            alpha_setup_profiles=[],
            alpha_setup_action_values=[open_action_value, execution_action_value],
            analyst_signals=[
                technical,
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.56),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation=market_confirmation,
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertGreater(ratio, 0.02)
        self.assertIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(alpha_ev["positive_action_value"])
        self.assertTrue(alpha_ev["qualified_positive_expectancy"])
        self.assertEqual(alpha_ev["action_value_stats"]["scope_quality"], "exact_real_state")
        self.assertTrue(alpha_ev["action_value_stats"]["real_amplification_support"])

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )
        self.assertTrue(allowed)
        self.assertEqual(authority["authority_type"], "real_budget_entry")
        self.assertTrue(authority["open_action_evidence"])
        self.assertTrue(authority["strong_current_evidence"])
        self.assertTrue(authority["open_action_evidence"])
        self.assertTrue(authority["strong_current_evidence"])

        plan = _build_pm_decision_context(
            ticker="RB",
            target_lots=4,
            current_price=3500.0,
            position_ratio=ratio,
            risk_level=RiskLevel.SAFE,
            long_scores={"confidence": 0.74},
            short_scores={"confidence": 0.20},
            margin_rate=0.10,
            current_lots=0,
            analyst_signals=[technical],
            final_entry_authority=authority,
            trading_date="2025-03-10",
            recommendation_intent={"action": "open_long"},
            control_reasons=reasons,
            alpha_setup_action_values=[open_action_value, execution_action_value],
        )
        execution_contract = plan
        self.assertEqual(execution_contract["execution_profile"], "breakout")
        self.assertEqual(execution_contract["trigger_source"], "technical_breakout")
        self.assertEqual(
            execution_contract["execution_action_value_preference"]["execution_profile"],
            "pullback",
        )
        self.assertFalse(execution_contract["can_execute_without_intraday_trigger"])
        self.assertTrue(execution_contract["requires_intraday_confirmation"])
        self.assertEqual(execution_contract["business_boundary"], "trader_executes_pm_plan_only_no_strategy_creation")

        final_contract = _build_final_action_contract(
            ticker="RB",
            current_lots=0,
            target_lots=4,
            position_ratio=ratio,
            margin_required=140000.0,
            account_equity=5000000.0,
            lots_to_trade=4,
            lots_to_trade_reason="tradable",
            recommendation_intent={"action": "open_long", "lots": 4, "action_type": "open"},
            final_entry_authority=authority,
            control_reasons=reasons,
            control_diagnostics=diagnostics,
            opportunity_scorecard=scorecard,
            market_confirmation=market_confirmation,
            alpha_setup_action_values=[open_action_value, execution_action_value],
            execution_contract_fields=execution_contract,
        )
        self.assertEqual(final_contract["final_action"], "open_real")
        self.assertEqual(final_contract["authority_type"], "real_budget_entry")
        self.assertTrue(final_contract["single_source_of_trade_truth"])

        result = select_intraday_execution(
            signal_bars=[
                {
                    "datetime": "2025-03-10 10:00:00",
                    "open": 3500.0,
                    "high": 3560.0,
                    "low": 3485.0,
                    "close": 3550.0,
                    "volume": 12,
                },
            ],
            execution_bars=[
                {
                    "datetime": "2025-03-10 09:30:00",
                    "open": 3500.0,
                    "high": 3540.0,
                    "low": 3470.0,
                    "close": 3500.0,
                    "volume": 12,
                },
                {
                    "datetime": "2025-03-10 09:31:00",
                    "open": 3500.0,
                    "high": 3540.0,
                    "low": 3470.0,
                    "close": 3500.0,
                    "volume": 12,
                },
                {
                    "datetime": "2025-03-10 10:01:00",
                    "open": 3552.0,
                    "high": 3560.0,
                    "low": 3545.0,
                    "close": 3555.0,
                    "volume": 10,
                },
            ],
            action="open_long",
            config={"opening_range_minutes": 2, "min_execution_volume": 1, "max_chase_ratio": 0.02},
            decision_context={"execution_contract": execution_contract},
        )
        self.assertTrue(result.should_execute)
        self.assertEqual(result.reason, "intraday_trigger_confirmed")
        self.assertEqual(result.features["execution_profile"], "breakout")
        self.assertTrue(result.to_audit_payload()["trigger_checked"])

    def test_single_exact_positive_open_action_value_becomes_candidate_not_real_budget(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="P",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "range",
                    "setup_type": "fundamental_timing_setup",
                    "action_name": "open",
                    "sample_count": 1,
                    "reward_mean": 1800.0,
                    "reward_sum": 1800.0,
                    "win_rate": 1.0,
                    "confidence_score": 0.60,
                    "action_preference": "positive_candidate_open",
                    "max_position_impact": 0.03,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 1,
                        "exact_state_real_trade_sample_count": 1,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.62,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="breakout above opening range with volume confirmation",
                    invalidation_level=9000.0,
                    position_invalidation_level=9000.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    entry_quality="strong",
                    evidence_role="entry_timing",
                    metadata=_canonical_pm_execution_metadata(ticker="P"),
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.58),
            ],
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertGreater(ratio, 0.0)
        self.assertLess(ratio, 0.02)
        self.assertIn("candidate_positive_action_preference", reasons)
        self.assertNotIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(alpha_ev["positive_action_value_candidate"])
        self.assertTrue(alpha_ev["candidate_positive_action_preference"])
        self.assertFalse(alpha_ev["positive_action_value"])
        self.assertFalse(alpha_ev["qualified_positive_expectancy"])

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )

        self.assertTrue(allowed)
        self.assertEqual(authority["authority_type"], "exploration_probe")
        self.assertTrue(authority["strong_current_evidence"])

    def test_positive_open_action_value_can_seed_candidate_from_neutral_pm_with_current_evidence(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            position_invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
            metadata=_canonical_pm_execution_metadata(ticker="RB"),
        )
        seed = _positive_open_action_value_seed(
            ticker="RB",
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 6,
                    "reward_mean": 520.0,
                    "reward_sum": 3120.0,
                    "win_rate": 0.67,
                    "confidence_score": 0.72,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 6,
                        "exact_state_real_trade_sample_count": 6,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed["side"], "long")
        self.assertGreater(seed["seed_position_ratio"], 0.0)
        self.assertLessEqual(seed["seed_position_ratio"], 0.05)
        control_diagnostics = {
            "alpha_setup_ev_fusion": {
                **seed["evidence"],
                "qualified_positive_expectancy": True,
                "positive_action_value": True,
            }
        }
        allowed, authority = _final_contract_authority(
            control_reasons=["positive_open_action_value_seed", "qualified_positive_expectancy"],
            control_diagnostics=control_diagnostics,
        )
        self.assertTrue(allowed)
        self.assertEqual(authority["authority_type"], "real_budget_entry")
        self.assertIn("positive_open_action_value_seed", authority["reason_effects"]["release_signals"])

    def test_legacy_action_preference_without_action_preference_cannot_seed_open_candidate(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            position_invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
            metadata=_canonical_pm_execution_metadata(ticker="RB"),
        )
        seed = _positive_open_action_value_seed(
            ticker="RB",
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 6,
                    "reward_mean": 520.0,
                    "reward_sum": 3120.0,
                    "win_rate": 0.67,
                    "confidence_score": 0.72,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 6,
                        "exact_state_real_trade_sample_count": 6,
                        "amplification_scope_quality": "exact_real_state",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed, {})

    def test_top_level_canonical_action_preference_can_seed_open_candidate_without_payload_duplicate(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            position_invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
            metadata=_canonical_pm_execution_metadata(ticker="RB"),
        )
        seed = _positive_open_action_value_seed(
            ticker="RB",
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 6,
                    "reward_mean": 520.0,
                    "reward_sum": 3120.0,
                    "win_rate": 0.67,
                    "confidence_score": 0.72,
                    "action_preference": "positive_candidate_open",
                    "max_position_impact": 0.04,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 6,
                        "exact_state_real_trade_sample_count": 6,
                        "amplification_scope_quality": "exact_real_state",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed["side"], "long")
        self.assertGreater(seed["seed_position_ratio"], 0.0)
        self.assertEqual(seed["row"]["action_preference"], "positive_candidate_open")

        flat_scorecard = self._base_scorecard(layer="tradeable_candidate")
        flat_scorecard["preferred_side"] = "flat"
        flat_seed = _positive_open_action_value_seed(
            ticker="RB",
            alpha_setup_action_values=[seed["row"]],
            analyst_signals=[technical],
            opportunity_scorecard=flat_scorecard,
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )
        self.assertEqual(flat_seed, {})

    def test_positive_open_learning_seed_keeps_step2_side_when_opposite_reward_is_higher(self):
        action_values = [
            {
                "ticker": "RB",
                "side": "long",
                "action_name": "open",
                "sample_count": 4,
                "reward_mean": 300.0,
                "reward_sum": 1200.0,
                "confidence_score": 0.70,
                "action_preference": "positive_candidate_open",
                "max_position_impact": 0.03,
            },
            {
                "ticker": "RB",
                "side": "short",
                "action_name": "open",
                "sample_count": 8,
                "reward_mean": 900.0,
                "reward_sum": 7200.0,
                "confidence_score": 0.90,
                "action_preference": "positive_candidate_open",
                "max_position_impact": 0.05,
            },
        ]
        with patch(
            "agents.decision_team.portfolio_manager._action_value_can_support_real_amplification",
            return_value=True,
        ), patch(
            "agents.decision_team.portfolio_manager._current_open_evidence_snapshot",
            return_value={"trade_authority": {"open_action_evidence": True}},
        ):
            seed = _positive_open_action_value_seed(
                ticker="RB",
                alpha_setup_action_values=action_values,
                analyst_signals=[],
                opportunity_scorecard={"preferred_side": "long"},
                market_confirmation={"confirmation_score": 0.70},
                full_config={},
                max_position_ratio=0.05,
            )

        self.assertEqual(seed["side"], "long")
        self.assertEqual(seed["row"]["reward_sum"], 1200.0)

    def test_single_exact_positive_open_action_value_seeds_probe_candidate_only(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            position_invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
            metadata=_canonical_pm_execution_metadata(ticker="P"),
        )
        seed = _positive_open_action_value_seed(
            ticker="P",
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "range",
                    "setup_type": "fundamental_timing_setup",
                    "action_name": "open",
                    "sample_count": 1,
                    "reward_mean": 1800.0,
                    "reward_sum": 1800.0,
                    "win_rate": 1.0,
                    "confidence_score": 0.16,
                    "action_preference": "positive_candidate_open",
                    "max_position_impact": 0.03,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 1,
                        "exact_state_real_trade_sample_count": 1,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                    },
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed["side"], "long")
        self.assertTrue(seed["candidate_positive_action_preference"])
        self.assertFalse(seed["mature_positive_action_value"])

    def test_legacy_positive_add_or_scale_preference_cannot_seed_open_candidate(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            position_invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
            metadata=_canonical_pm_execution_metadata(ticker="RB"),
        )

        for legacy_preference in ("positive_candidate_add", "positive_candidate_scale"):
            with self.subTest(legacy_preference=legacy_preference):
                seed = _positive_open_action_value_seed(
                    ticker="RB",
                    alpha_setup_action_values=[
                        {
                            "ticker": "RB",
                            "side": "long",
                            "horizon_class": "short",
                            "market_regime": "trend",
                            "setup_type": "trend_breakout_setup",
                            "action_name": "open",
                            "sample_count": 6,
                            "reward_mean": 520.0,
                            "reward_sum": 3120.0,
                            "win_rate": 0.67,
                            "confidence_score": 0.72,
                            "action_preference": "observe_or_probe",
                            "max_position_impact": 0.04,
                            "payload": {
                                "source": "alpha_setup_profile_action_value",
                                "real_trade_reward_count": 6,
                                "exact_state_real_trade_sample_count": 6,
                                "amplification_scope_quality": "exact_real_state",
                                "action_preference": legacy_preference,
                                "reward_source": "trade_episode",
                            },
                        }
                    ],
                    analyst_signals=[technical],
                    opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
                    market_confirmation={"confirmation_score": 0.70},
                    full_config={},
                    max_position_ratio=0.05,
                )

                self.assertEqual(seed, {})

    def test_sector_fallback_action_value_cannot_seed_open_candidate(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            position_invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
            metadata=_canonical_pm_execution_metadata(ticker="RB"),
        )
        seed = _positive_open_action_value_seed(
            ticker="RB",
            alpha_setup_action_values=[
                {
                    "ticker": "*",
                    "side": "long",
                    "action_name": "open",
                    "sample_count": 8,
                    "reward_mean": 1000.0,
                    "reward_sum": 8000.0,
                    "confidence_score": 0.90,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.05,
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.75},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed, {})

    def test_similar_setup_sector_fallback_cannot_seed_open_candidate(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            position_invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
            metadata=_canonical_pm_execution_metadata(ticker="RB"),
        )
        seed = _positive_open_action_value_seed(
            ticker="RB",
            alpha_setup_action_values=[
                {
                    "ticker": "*",
                    "side": "long",
                    "action_name": "open",
                    "sample_count": 8,
                    "reward_mean": 1000.0,
                    "reward_sum": 8000.0,
                    "confidence_score": 0.90,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.05,
                    "payload": {
                        "source": "similar_alpha_setup_sql",
                        "strict_no_lookahead": True,
                        "exact_ticker_sample_count": 0,
                    },
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.75},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed, {})

    def test_counterfactual_prior_only_open_action_value_cannot_seed_open_candidate(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3300.0,
            position_invalidation_level=3300.0,
            trigger_valid=True,
            invalidation_present=True,
            entry_quality="strong",
            evidence_role="entry_timing",
            metadata=_canonical_pm_execution_metadata(ticker="RB"),
        )
        seed = _positive_open_action_value_seed(
            ticker="RB",
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "action_name": "open",
                    "sample_count": 5,
                    "reward_mean": 800.0,
                    "reward_sum": 4000.0,
                    "confidence_score": 0.80,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.05,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "counterfactual_prior_only": True,
                        "counterfactual_reward_count": 5,
                        "real_trade_reward_count": 0,
                    },
                }
            ],
            analyst_signals=[technical],
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            market_confirmation={"confirmation_score": 0.75},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertEqual(seed, {})

    def test_similar_setup_coarse_same_ticker_open_action_value_cannot_promote_real_authority(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="J",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "J",
                    "side": "*",
                    "action_name": "open",
                    "sample_count": 12,
                    "reward_mean": 500.0,
                    "reward_sum": 6000.0,
                    "win_rate": 0.80,
                    "confidence_score": 0.68,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "setup_type": "*",
                    "market_regime": "*",
                    "payload": {
                        "source": "similar_alpha_setup_sql",
                        "strict_no_lookahead": True,
                        "exact_ticker_sample_count": 12,
                        "exact_ticker_real_trade_sample_count": 12,
                        "exact_state_real_trade_sample_count": 0,
                        "partial_state_real_trade_sample_count": 12,
                        "similar_real_trade_sample_count": 0,
                        "real_trade_reward_count": 12,
                        "amplification_scope_quality": "partial_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                        "prior_only_no_direct_authority": True,
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="breakout above opening range with volume confirmation",
                    invalidation_level=3300.0,
                    position_invalidation_level=3300.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    entry_quality="strong",
                    evidence_role="entry_timing",
                    metadata=_canonical_pm_execution_metadata(ticker="J"),
                )
            ],
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertLessEqual(ratio, 0.02)
        self.assertNotIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertFalse(alpha_ev["positive_action_value"])
        self.assertTrue(alpha_ev["positive_action_value_candidate"])
        self.assertTrue(alpha_ev["action_value_stats"]["exact_ticker_support"])
        self.assertFalse(alpha_ev["action_value_stats"]["real_amplification_support"])
        self.assertEqual(alpha_ev["action_value_stats"]["scope_quality"], "partial_real_state")

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )

        self.assertTrue(allowed)
        self.assertNotEqual(authority["authority_type"], "real_budget_entry")
        self.assertEqual(authority["authority_type"], "exploration_probe")
        self.assertTrue(authority["strong_current_evidence"])

    def test_similar_setup_exact_state_open_action_value_can_promote_authority(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="RB",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "action_name": "open",
                    "sample_count": 5,
                    "reward_mean": 500.0,
                    "reward_sum": 2500.0,
                    "win_rate": 0.80,
                    "confidence_score": 0.68,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "setup_type": "trend_breakout_setup",
                    "market_regime": "trend",
                    "horizon_class": "short",
                    "payload": {
                        "source": "similar_alpha_setup_sql",
                        "strict_no_lookahead": True,
                        "exact_ticker_sample_count": 5,
                        "exact_ticker_real_trade_sample_count": 5,
                        "exact_state_real_trade_sample_count": 5,
                        "partial_state_real_trade_sample_count": 0,
                        "similar_real_trade_sample_count": 0,
                        "real_trade_reward_count": 5,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="breakout above opening range with volume confirmation",
                    invalidation_level=3300.0,
                    position_invalidation_level=3300.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    entry_quality="strong",
                    evidence_role="entry_timing",
                    metadata=_canonical_pm_execution_metadata(ticker="RB"),
                )
            ],
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertGreater(ratio, 0.02)
        self.assertIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(alpha_ev["positive_action_value"])
        self.assertTrue(alpha_ev["action_value_stats"]["exact_ticker_support"])
        self.assertTrue(alpha_ev["action_value_stats"]["real_amplification_support"])
        self.assertEqual(alpha_ev["action_value_stats"]["scope_quality"], "exact_real_state")

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )

        self.assertTrue(allowed)
        self.assertEqual(authority["authority_type"], "real_budget_entry")

    def test_legacy_or_incomplete_direct_action_value_cannot_promote_real_authority(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="RB",
            position_ratio=0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "RB",
                    "side": "long",
                    "action_name": "open",
                    "sample_count": 8,
                    "reward_mean": 700.0,
                    "reward_sum": 5600.0,
                    "win_rate": 0.75,
                    "confidence_score": 0.82,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.05,
                    "setup_type": "tradeable_candidate",
                    "market_regime": "trend",
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 8,
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="breakout above opening range with volume confirmation",
                    invalidation_level=3300.0,
                    position_invalidation_level=3300.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    entry_quality="strong",
                    evidence_role="entry_timing",
                    metadata=_canonical_pm_execution_metadata(ticker="RB"),
                )
            ],
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertLessEqual(ratio, 0.02)
        self.assertNotIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(alpha_ev["positive_action_value_candidate"])
        self.assertFalse(alpha_ev["positive_action_value"])
        self.assertFalse(alpha_ev["action_value_stats"]["real_amplification_support"])
        self.assertEqual(alpha_ev["action_value_stats"]["scope_quality"], "partial_real_state")

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )

        self.assertTrue(allowed)
        self.assertNotEqual(authority["authority_type"], "real_budget_entry")
        self.assertEqual(authority["authority_type"], "exploration_probe")
        self.assertTrue(authority["strong_current_evidence"])

    def test_exact_state_positive_action_value_with_tail_loss_needs_strong_current_evidence(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="TA",
            position_ratio=-0.02,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="watch_for_trigger"),
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "TA",
                    "side": "short",
                    "action_name": "open",
                    "sample_count": 6,
                    "reward_mean": 300.0,
                    "reward_sum": 1800.0,
                    "win_rate": 0.67,
                    "confidence_score": 0.72,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.04,
                    "setup_type": "breakdown_setup",
                    "market_regime": "trend",
                    "horizon_class": "short",
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 6,
                        "exact_state_real_trade_sample_count": 6,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                        "loss_reward_count": 1,
                        "tail_loss_count": 1,
                        "worst_reward": -3800.0,
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BEARISH,
                    confidence=0.55,
                    opportunity_state="tradeable_candidate",
                    invalidation_level=5600.0,
                    position_invalidation_level=5600.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    entry_trigger="current breakdown below support is confirmed",
                    evidence_role="entry_timing",
                    metadata={
                        "action_evidence_contract": build_test_aec(
                            "technical",
                            ticker="TA",
                            signal="Bearish",
                            side="short",
                            confidence=0.55,
                            opportunity_state="tradeable_candidate",
                            trigger_valid=True,
                            current_trigger_confirmed=True,
                            invalidation_present=True,
                            invalidation_condition="15m close above 5600",
                        )
                    },
                )
            ],
            market_confirmation={"confirmation_score": 0.46},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertLessEqual(abs(ratio), 0.02)
        self.assertIn("qualified_positive_expectancy", reasons)
        alpha_ev = diagnostics["alpha_setup_ev_fusion"]
        self.assertTrue(alpha_ev["positive_action_value_candidate"])
        self.assertTrue(alpha_ev["positive_action_value"])
        self.assertFalse(alpha_ev["tail_loss_blocks_real_amplification"])
        self.assertTrue(alpha_ev["qualified_positive_expectancy"])

        allowed, authority = _final_contract_authority(
            control_reasons=reasons,
            control_diagnostics=diagnostics,
        )

        self.assertTrue(authority["strong_current_evidence"])
        self.assertEqual(authority["authority_type"], "watchlist_only")
        self.assertIn("negative_expectancy", authority["reason_codes"])
        self.assertNotEqual(authority["authority_type"], "real_budget_entry")

    def test_positive_hold_action_value_does_not_qualify_new_open(self):
        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="P",
            position_ratio=0.03,
            current_ratio=0.0,
            opportunity_scorecard=self._base_scorecard(layer="tradeable_candidate"),
            alpha_setup_profiles=[
                {
                    "side": "long",
                    "lifecycle_state": "protected",
                    "sample_count": 5,
                    "net_pnl": 6000.0,
                    "profit_factor": 2.0,
                    "win_rate": 0.60,
                    "confidence_score": 0.70,
                    "max_position_impact": 0.04,
                }
            ],
            alpha_setup_action_values=[
                {
                    "side": "long",
                    "action_name": "hold_position",
                    "sample_count": 4,
                    "reward_mean": 1500.0,
                    "reward_sum": 6000.0,
                    "win_rate": 0.75,
                    "confidence_score": 0.72,
                    "action_preference": "controlled_probe_or_hold",
                    "max_position_impact": 0.04,
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.60,
                    invalidation_level=3300.0,
                    position_invalidation_level=3300.0,
                    opportunity_state="tradeable_candidate",
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.55),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation={"confirmation_score": 0.62},
            full_config={},
            max_position_ratio=0.05,
        )

        self.assertLess(ratio, 0.03)
        self.assertNotIn("qualified_positive_expectancy", reasons)
        detail = diagnostics["alpha_setup_ev_fusion"]
        self.assertEqual(detail["intended_action"], "open")
        self.assertTrue(detail["open_action_value_missing"])
        self.assertEqual(detail["matched_action_value_count"], 0)
        self.assertFalse(detail["qualified_positive_expectancy"])

    def test_sql_similar_setup_retrieval_uses_only_past_trading_dates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            rows = [
                ("past-win", "2025-03-05", "RB", "long", "ferrous", "short", "trend", "breakout_setup", "combo", "open_long", 10, 10, 1200.0, 20.0, 0),
                ("past-reduce", "2025-03-06", "RB", "long", "ferrous", "short", "trend", "breakout_setup", "combo", "reduce", 2, 1, 400.0, 5.0, 3),
                ("past-win-2", "2025-03-07", "RB", "long", "ferrous", "short", "trend", "breakout_setup", "combo", "open_long", 8, 8, 800.0, 15.0, 0),
                ("past-exit", "2025-03-08", "RB", "long", "ferrous", "short", "trend", "breakout_setup", "combo", "exit", 0, 3, 600.0, 5.0, 3),
                ("future-loss", "2025-03-20", "RB", "long", "ferrous", "short", "trend", "breakout_setup", "combo", "open_long", 8, 8, -9000.0, 10.0, 0),
            ]
            for row in rows:
                cursor.execute(
                    """
                    INSERT INTO alpha_setup_sample (
                        id, config_id, trading_date, ticker, side, sector, horizon_class,
                        market_regime, setup_type, data_combo, scope_key, source_type,
                        recommendation_id, action_taken, pm_action, auditor_decision,
                        trader_status, target_lots, executed_lots, net_pnl, commission,
                        holding_days, outcome_label, setup_quality_score, opportunity_state,
                        evidence_json, result_json, created_at, payload_json
                    ) VALUES (?, 'cfg', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'trade', ?, ?, ?, 'pass',
                        'executed', ?, ?, ?, ?, 1, 'observed', 0.7, 'tradeable_candidate', '{}', '{}', ?, ?)
                    """,
                    (
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                        row[4],
                        row[5],
                        row[6],
                        row[7],
                        row[8],
                        f"{row[2]}|{row[3]}|{row[5]}|{row[6]}|{row[7]}|{row[8]}",
                        row[0],
                        row[9],
                        row[9],
                        row[10],
                        row[11],
                        row[12],
                        row[13],
                        row[1],
                        json.dumps({"current_lots": row[14]}),
                    ),
                )
            conn.commit()
            conn.close()

            values = db.get_similar_alpha_setup_action_values(
                config_id="cfg",
                ticker="RB",
                sector="ferrous",
                side="long",
                horizon_class="short",
                market_regime="trend",
                setup_type="breakout_setup",
                trading_date="2025-03-10",
                limit=3,
            )

        open_value = next(row for row in values if row["action_name"] == "open")
        self.assertEqual(open_value["sample_count"], 2)
        self.assertGreater(open_value["reward_sum"], 0)
        self.assertNotIn("2025-03-20", open_value["payload"]["episode_dates"])
        self.assertEqual(open_value["payload"]["date_filter"], "alpha_setup_sample.trading_date < decision_date")
        self.assertEqual(open_value["payload"]["amplification_scope_quality"], "exact_real_state")
        self.assertEqual(open_value["payload"]["exact_state_real_trade_sample_count"], 2)
        self.assertEqual(open_value["payload"]["partial_state_real_trade_sample_count"], 0)
        self.assertEqual(open_value["action_preference"], "")
        self.assertEqual(open_value["payload"]["prior_role"], "weak_prior_not_action_preference")
        self.assertEqual(open_value["payload"]["canonical_action_preference_source"], "none_for_similar_sql_prior")
        self.assertEqual(open_value["payload"]["action_preference"], "")
        by_action = {row["action_name"]: row for row in values}
        self.assertEqual(set(by_action), {"open", "reduce", "exit"})
        for action_name, expected_family in {
            "open": "open_add_new_risk",
            "reduce": "reduce_exit",
            "exit": "reduce_exit",
        }.items():
            with self.subTest(action_name=action_name):
                row = by_action[action_name]
                self.assertEqual(row["canonical_action_family"], expected_family)
                self.assertEqual(row["action_value_lane"], action_name)
                self.assertEqual(row["learning_lane"], action_name)
                self.assertIs(row["canonical_action_value"], False)
                self.assertEqual(row["action_preference"], "")

        self.assertEqual(_select_learning_trace_action_values(values, limit=10), [])
        diagnostic_state = _attach_incomplete_prior_diagnostics_to_contract_state({
            "alpha_setup_action_values": values,
            "control_diagnostics": {
                "final_action_memory_retrieval": {
                    "tool": "decision_memory_retrieval",
                    "rejected_or_downgraded": [],
                }
            },
        })
        rejected = diagnostic_state["control_diagnostics"]["final_action_memory_retrieval"]["rejected_or_downgraded"]
        self.assertEqual({row["action_name"] for row in rejected}, {"open", "reduce", "exit"})
        self.assertTrue(all(row["diagnostic_only"] for row in rejected))

        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            business_quality_score=0.70,
            setup_quality_score=0.76,
            opportunity_state="tradeable_candidate",
            entry_trigger="current breakout confirmed above resistance",
            invalidation_level=3500.0,
            position_invalidation_level=3500.0,
            trigger_valid=True,
            invalidation_present=True,
        )
        scorecard_kwargs = {
            "ticker": "RB",
            "analyst_signals": [signal],
            "market_confirmation": {"confirmation_score": 0.70},
            "data_quality_summary": {},
            "decision_date": "2025-03-10",
            "config": {},
        }
        clean_scorecard = build_opportunity_scorecard(**scorecard_kwargs)
        similar_scorecard = build_opportunity_scorecard(
            **scorecard_kwargs,
            alpha_setup_action_values=values,
        )
        self.assertEqual(
            similar_scorecard["long"]["opportunity_score_components"]["positive_learning"],
            0.0,
        )
        self.assertEqual(
            similar_scorecard["long"]["opportunity_score_components"]["negative_learning"],
            0.0,
        )
        clean_rank = _ensure_final_rank_score_fields(dict(clean_scorecard["long"]), config={})
        similar_rank = _ensure_final_rank_score_fields(dict(similar_scorecard["long"]), config={})
        self.assertEqual(similar_rank["rank_score"], clean_rank["rank_score"])

    def test_sql_similar_setup_retrieval_counts_counterfactual_as_prior_not_exact_trade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            cursor.execute(
                """
                INSERT INTO alpha_setup_sample (
                    id, config_id, trading_date, ticker, side, sector, horizon_class,
                    market_regime, setup_type, data_combo, scope_key, source_type,
                    recommendation_id, action_taken, pm_action, auditor_decision,
                    trader_status, target_lots, executed_lots, net_pnl, commission,
                    holding_days, outcome_label, setup_quality_score, opportunity_state,
                    evidence_json, result_json, created_at, payload_json
                ) VALUES ('counterfactual-rb-1', 'cfg', '2025-03-05', 'RB', 'long', 'ferrous',
                    'short', 'trend', 'breakout_setup', 'counterfactual_no_trade_missed',
                    'RB|long|short|trend|breakout_setup|counterfactual_no_trade_missed',
                    'counterfactual_missed_alpha', 'counterfactual:1', 'open_long',
                    'counterfactual_counterfactual_open', 'not_executed_counterfactual',
                    'counterfactual_not_executed', 1, 0, 1000.0, 0.0, 3, 'profit',
                    0.0, 'tradeable_candidate', '{}', '{}', '2025-03-10', '{}')
                """
            )
            conn.commit()
            conn.close()

            values = db.get_similar_alpha_setup_action_values(
                config_id="cfg",
                ticker="RB",
                sector="ferrous",
                side="long",
                horizon_class="short",
                market_regime="trend",
                setup_type="breakout_setup",
                trading_date="2025-03-10",
                limit=3,
            )

        open_value = next(row for row in values if row["action_name"] == "open")
        self.assertAlmostEqual(open_value["reward_sum"], 350.0)
        self.assertEqual(open_value["payload"]["exact_ticker_sample_count"], 0)
        self.assertEqual(open_value["payload"]["exact_ticker_counterfactual_sample_count"], 1)
        self.assertEqual(open_value["payload"]["amplification_scope_quality"], "counterfactual_prior")
        self.assertTrue(open_value["payload"]["counterfactual_prior_only"])
        self.assertEqual(open_value["action_preference"], "")
        self.assertEqual(open_value["payload"]["prior_role"], "weak_prior_not_action_preference")
        self.assertEqual(open_value["payload"]["action_preference"], "")

    def test_direct_alpha_setup_action_value_uses_only_past_sample_dates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            rows = [
                ("past-open", "RB|long|past", "2025-03-07", 1200.0, 0.70),
                ("same-day-open", "RB|long|same", "2025-03-10", 9000.0, 0.99),
                ("future-open", "RB|long|future", "2025-03-20", -9000.0, 0.95),
                ("legacy-open", "RB|long|legacy", None, 5000.0, 0.80),
            ]
            for row_id, scope_key, last_sample_date, reward_mean, confidence in rows:
                cursor.execute(
                    """
                    INSERT INTO alpha_setup_action_value (
                        id, config_id, scope_key, ticker, side, horizon_class, market_regime,
                        setup_type, data_combo, action_name, sample_count, reward_sum,
                        reward_mean, win_rate, confidence_score, action_preference,
                        max_position_impact, last_sample_date, created_at, updated_at,
                        valid_until, active, payload_json
                    ) VALUES (?, 'cfg', ?, 'RB', 'long', 'short', 'trend',
                        'tradeable_candidate', 'combo', 'open', 4, ?, ?, 0.75, ?,
                        'controlled_open_or_add', 0.04, ?, '2025-03-10', '2025-03-10',
                        '2025-04-01', 1, '{}')
                    """,
                    (row_id, scope_key, reward_mean * 4, reward_mean, confidence, last_sample_date),
                )
            conn.commit()
            conn.close()

            values = db.get_alpha_setup_action_values(
                config_id="cfg",
                ticker="RB",
                side="long",
                horizon_class="short",
                market_regime="trend",
                trading_date="2025-03-10",
                limit=10,
            )

        ids = {row["id"] for row in values}
        self.assertIn("past-open", ids)
        self.assertNotIn("same-day-open", ids)
        self.assertNotIn("future-open", ids)
        self.assertNotIn("legacy-open", ids)

    def test_sqlite_null_or_empty_consumer_scope_is_rejected_by_pm_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            rows = [
                ("scope-null", None, {}),
                ("scope-empty", "", {}),
                ("scope-payload-pm", None, {"consumer_scope": "pm_learning"}),
                ("scope-top-pm", "pm_learning", {}),
            ]
            for row_id, consumer_scope, payload in rows:
                cursor.execute(
                    """
                    INSERT INTO alpha_setup_action_value (
                        id, config_id, scope_key, ticker, side, horizon_class, market_regime,
                        setup_type, data_combo, action_name, canonical_action_family,
                        sample_count, reward_sum, reward_mean, win_rate, confidence_score,
                        action_preference, reward_source, evidence_scope, action_value_lane,
                        consumer_scope, learning_lane, memory_side_role, last_sample_date,
                        created_at, updated_at, valid_until, active, payload_json
                    ) VALUES (?, 'cfg', ?, 'BU', 'short', 'short', 'trend',
                        'trend_breakout', 'combo', 'open', 'open_add_new_risk',
                        1, 500.0, 500.0, 1.0, 0.7, 'positive_candidate_open',
                        'trade_episode', 'exact_real_state', 'open', ?, 'open',
                        'target_side', '2025-03-04', '2025-03-04', '2025-03-04',
                        '2025-04-04', 1, ?)
                    """,
                    (
                        row_id,
                        f"BU|short|short|trend|trend_breakout|open|{row_id}",
                        consumer_scope,
                        json.dumps({**payload, "canonical_action_value": True}),
                    ),
                )
            conn.commit()
            conn.close()

            result = retrieve_pm_memory(
                db=db,
                config_id="cfg",
                ticker="BU",
                side="short",
                horizon_class="short",
                market_regime="trend",
                setup_type="trend_breakout",
                trading_date="2025-03-05",
                limit=10,
            )

        self.assertEqual(
            {row["id"] for row in result["action_values"]},
            {"scope-payload-pm", "scope-top-pm"},
        )
        rejected_ids = {
            row.get("id")
            for row in result["rejected_or_downgraded"]
            if row.get("reason") == "non_pm_learning_scope"
        }
        self.assertEqual(rejected_ids, {"scope-null", "scope-empty"})

    def test_direct_alpha_setup_action_value_prioritizes_real_action_preference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            rows = [
                (
                    "weak-high-conf",
                    "RB|long|weak",
                    "trend_breakout_setup",
                    0.99,
                    "",
                    9000.0,
                    {"reward_source": "counterfactual_prior", "amplification_scope_quality": "counterfactual_prior"},
                ),
                (
                    "real-pref",
                    "RB|long|real",
                    "trend_breakout_setup",
                    0.40,
                    "positive_candidate_open",
                    800.0,
                    {
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                        "amplification_scope_quality": "exact_real_state",
                    },
                ),
            ]
            for row_id, scope_key, setup_type, confidence, preference, reward_mean, payload in rows:
                cursor.execute(
                    """
                    INSERT INTO alpha_setup_action_value (
                        id, config_id, scope_key, ticker, side, horizon_class, market_regime,
                        setup_type, data_combo, action_name, sample_count, reward_sum,
                        reward_mean, win_rate, confidence_score, action_preference,
                        max_position_impact, last_sample_date, created_at, updated_at,
                        valid_until, active, payload_json
                    ) VALUES (?, 'cfg', ?, 'RB', 'long', 'short', 'trend',
                        ?, 'combo', 'open', 4, ?, ?, 0.75, ?, ?,
                        0.04, '2025-03-07', '2025-03-07', '2025-03-07',
                        '2025-04-01', 1, ?)
                    """,
                    (row_id, scope_key, setup_type, reward_mean * 4, reward_mean, confidence, preference, json.dumps(payload)),
                )
            conn.commit()
            conn.close()

            values = db.get_alpha_setup_action_values(
                config_id="cfg",
                ticker="RB",
                side="long",
                horizon_class="short",
                market_regime="trend",
                setup_type="trend_breakout_setup",
                trading_date="2025-03-10",
                limit=1,
            )

        self.assertEqual(values[0]["id"], "real-pref")
        self.assertEqual(values[0]["action_preference"], "positive_candidate_open")
        self.assertEqual(values[0]["reward_source"], "trade_episode")
        self.assertEqual(values[0]["evidence_scope"], "exact_real_state")
        self.assertEqual(values[0]["action_value_lane"], "open")

    def test_direct_open_daily_fragments_do_not_create_open_action_value(self):
        from tools.common.alpha_setup import upsert_alpha_setup_sample_and_profile

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            for idx, setup_type in enumerate(("generic_trade_setup", "trend_breakout_setup"), start=1):
                upsert_alpha_setup_sample_and_profile(
                    cursor,
                    cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                    config_id="cfg",
                    trading_date=f"2025-03-0{idx}",
                    sample={
                        "ticker": "RB",
                        "side": "long",
                        "sector": "ferrous",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "setup_type": setup_type,
                        "data_combo": f"combo-{setup_type}",
                        "source_type": "trade",
                        "recommendation_id": f"rec-{idx}",
                        "action_taken": "open_long",
                        "pm_action": "open_long",
                        "target_lots": 1,
                        "current_lots": 0,
                        "executed_lots": 1,
                        "net_pnl": 1200.0,
                        "commission": 20.0,
                        "opportunity_state": "tradeable_candidate",
                    },
                )
            conn.commit()
            cursor.execute(
                """
                SELECT setup_type, payload_json
                FROM alpha_setup_action_value
                WHERE action_name = 'open'
                ORDER BY setup_type
                """
            )
            rows = [dict(row) for row in cursor.fetchall()]
            conn.close()

        self.assertEqual(rows, [])

    def test_adaptive_policy_state_uses_only_past_learning_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            events = [
                ("event-past", "2025-03-07"),
                ("event-same", "2025-03-10"),
                ("event-future", "2025-03-20"),
            ]
            for event_id, event_date in events:
                cursor.execute(
                    """
                    INSERT INTO learning_event_log (
                        id, config_id, trading_date, event_type, agent, scope_type,
                        scope_key, evidence_json, action_json, verifier, created_at, status
                    ) VALUES (?, 'cfg', ?, 'adaptive_policy_state', 'reviewer',
                        'template', 'RB:long', '{}', '{}', 'test', ?, 'applied')
                    """,
                    (event_id, event_date, event_date),
                )
            policies = [
                ("policy-past", "event-past", None, 0.70),
                ("policy-same", "event-same", None, 0.95),
                ("policy-future", "event-future", None, 0.90),
                ("policy-explicit-past", None, "2025-03-05", 0.80),
                ("policy-explicit-same", None, "2025-03-10", 0.85),
                ("policy-unknown", None, None, 0.99),
            ]
            for row_id, event_id, source_trading_date, confidence in policies:
                cursor.execute(
                    """
                    INSERT INTO adaptive_policy_state (
                        id, config_id, ticker, side, setup_type, horizon_class,
                        market_regime, policy_type, policy_action, multiplier,
                        confidence_score, sample_count, reason, source_event_id,
                        source_trading_date, created_at, valid_until, payload_json, active
                    ) VALUES (?, 'cfg', 'RB', 'long', '*', 'short', 'trend',
                        ?, 'protect', 1.0, ?, 4, 'test boundary', ?, ?,
                        '2025-03-10', '2025-04-01', '{}', 1)
                    """,
                    (row_id, f"test_policy_{row_id}", confidence, event_id, source_trading_date),
                )
            conn.commit()
            conn.close()

            rows = db.get_adaptive_policy_state(
                config_id="cfg",
                ticker="RB",
                side="long",
                setup_type="*",
                horizon_class="short",
                market_regime="trend",
                trading_date="2025-03-10",
            )

        ids = {row["id"] for row in rows}
        self.assertIn("policy-past", ids)
        self.assertIn("policy-explicit-past", ids)
        self.assertNotIn("policy-same", ids)
        self.assertNotIn("policy-future", ids)
        self.assertNotIn("policy-explicit-same", ids)
        self.assertNotIn("policy-unknown", ids)

    def test_pm_profile_consumers_reject_cross_setup_rows(self):
        class _ProfileDB:
            def get_alpha_setup_profiles(self, **_kwargs):
                return [
                    {"id": "same", "setup_type": "trend_breakout_setup"},
                    {"id": "cross", "setup_type": "volatility_breakout_setup"},
                ]

        scoped = _ExplicitPMLearningScopeDBView(_ProfileDB()).get_alpha_setup_profiles(
            setup_type="trend_breakout_setup"
        )
        self.assertEqual([row["id"] for row in scoped], ["same"])

        ratio, _reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="RB",
            position_ratio=-0.05,
            current_ratio=0.0,
            opportunity_scorecard={
                "short": {
                    "final_state": "tradeable_candidate",
                    "setup_quality_ok": True,
                }
            },
            alpha_setup_profiles=[
                {
                    "side": "short",
                    "setup_type": "volatility_breakout_setup",
                    "lifecycle_state": "deployable",
                    "sample_count": 20,
                    "confidence_score": 0.95,
                }
            ],
            alpha_setup_action_values=[],
            analyst_signals=[],
            market_confirmation={"confirmation_score": 0.70},
            full_config={},
            max_position_ratio=0.15,
            formal_setup_by_side={"short": "trend_breakout_setup"},
        )
        self.assertEqual(ratio, -0.05)
        self.assertEqual(
            diagnostics["alpha_setup_ev_fusion"]["decision"],
            "no_expectancy_evidence",
        )
        self.assertFalse(diagnostics["alpha_setup_ev_fusion"]["positive_profile"])

    def test_learning_boundary_excludes_same_day_and_future_overlays_and_profiles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            overlay_rows = [
                ("overlay-past", "2025-03-07", "max_total_margin_ratio", "0.18"),
                ("overlay-same", "2025-03-10", "probe_margin_ratio_same", "0.02"),
                ("overlay-future", "2025-03-20", "probe_margin_ratio_future", "0.03"),
            ]
            for row_id, source_date, key, value in overlay_rows:
                cursor.execute(
                    """
                    INSERT INTO config_learning_overlay (
                        id, config_id, trading_date, param_key, learned_value_json,
                        previous_value_json, scope_type, scope_key, source,
                        confidence_score, sample_count, reason, source_event_id,
                        rollback_value_json, created_at, valid_until, active
                    ) VALUES (?, 'cfg', ?, ?, ?, '0', 'global', '*', 'reviewer',
                        0.8, 2, 'test boundary', NULL, '0', ?, '2025-04-01', 1)
                    """,
                    (row_id, source_date, key, value, source_date),
                )
            profile_rows = [
                ("profile-past", "RB|long|past", "2025-03-07", 0.70),
                ("profile-same", "RB|long|same", "2025-03-10", 0.95),
                ("profile-future", "RB|long|future", "2025-03-20", 0.90),
                ("profile-legacy", "RB|long|legacy", None, 0.99),
            ]
            for row_id, scope_key, last_sample_date, confidence in profile_rows:
                cursor.execute(
                    """
                    INSERT INTO alpha_setup_profile (
                        id, config_id, ticker, side, sector, horizon_class,
                        market_regime, setup_type, data_combo, scope_key,
                        lifecycle_state, profile_state_hint, sample_count, trade_count,
                        win_count, loss_count, net_pnl, confidence_score,
                        max_position_impact, last_sample_date, created_at,
                        updated_at, valid_until, active, payload_json
                    ) VALUES (?, 'cfg', 'RB', 'long', 'ferrous', 'short',
                        'trend', 'tradeable_candidate', 'combo', ?, 'deployable',
                        'open', 4, 4, 3, 1, 2000.0, ?, 0.04, ?,
                        '2025-03-10', '2025-03-10', '2025-04-01', 1, '{}')
                    """,
                    (row_id, scope_key, confidence, last_sample_date),
                )
            cursor.execute(
                """
                INSERT INTO alpha_setup_profile (
                    id, config_id, ticker, side, sector, horizon_class,
                    market_regime, setup_type, data_combo, scope_key,
                    lifecycle_state, profile_state_hint, sample_count, trade_count,
                    win_count, loss_count, net_pnl, confidence_score,
                    max_position_impact, last_sample_date, created_at,
                    updated_at, valid_until, active, payload_json
                ) VALUES ('profile-other-setup', 'cfg', 'RB', 'long', 'ferrous',
                    'short', 'trend', 'volatility_breakout_setup', 'combo',
                    'RB|long|other', 'deployable', 'open', 4, 4, 3, 1,
                    3000.0, 0.80, 0.04, '2025-03-07', '2025-03-10',
                    '2025-03-10', '2025-04-01', 1, '{}')
                """
            )
            conn.commit()
            conn.close()

            overlays = db.get_config_learning_overlay(config_id="cfg", trading_date="2025-03-10")
            profiles = db.get_alpha_setup_profiles(
                config_id="cfg",
                ticker="RB",
                sector="ferrous",
                side="long",
                horizon_class="short",
                market_regime="trend",
                trading_date="2025-03-10",
                limit=10,
            )
            next_day_overlays = db.get_config_learning_overlay(
                config_id="cfg",
                trading_date="2025-03-11",
            )
            next_day_profiles = db.get_alpha_setup_profiles(
                config_id="cfg",
                ticker="RB",
                sector="ferrous",
                side="long",
                horizon_class="short",
                market_regime="trend",
                trading_date="2025-03-11",
                limit=10,
            )
            exact_profiles = db.get_alpha_setup_profiles(
                config_id="cfg",
                ticker="RB",
                sector="ferrous",
                side="long",
                horizon_class="short",
                market_regime="trend",
                setup_type="tradeable_candidate",
                trading_date="2025-03-10",
                limit=10,
            )
            cross_setup_profiles = db.get_alpha_setup_profiles(
                config_id="cfg",
                ticker="RB",
                sector="ferrous",
                side="long",
                horizon_class="short",
                market_regime="trend",
                setup_type="volatility_breakout_setup",
                trading_date="2025-03-10",
                limit=10,
            )

        self.assertEqual({row["id"] for row in overlays}, {"overlay-past"})
        profile_ids = {row["id"] for row in profiles}
        self.assertIn("profile-past", profile_ids)
        self.assertNotIn("profile-same", profile_ids)
        self.assertNotIn("profile-future", profile_ids)
        self.assertNotIn("profile-legacy", profile_ids)
        self.assertIn("overlay-same", {row["id"] for row in next_day_overlays})
        self.assertIn("profile-same", {row["id"] for row in next_day_profiles})
        self.assertEqual({row["id"] for row in exact_profiles}, {"profile-past"})
        self.assertEqual(
            {row["id"] for row in cross_setup_profiles},
            {"profile-other-setup"},
        )

    def test_learning_boundary_excludes_same_day_and_future_performance_and_prompt_priors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            events = [
                ("digest-past-event", "2025-03-07"),
                ("digest-same-event", "2025-03-10"),
                ("digest-future-event", "2025-03-20"),
            ]
            for event_id, event_date in events:
                cursor.execute(
                    """
                    INSERT INTO learning_event_log (
                        id, config_id, trading_date, event_type, agent, scope_type,
                        scope_key, evidence_json, action_json, verifier, created_at, status
                    ) VALUES (?, 'cfg', ?, 'analyst_learning_digest', 'reviewer',
                        'analyst', 'technical:RB', '{}', '{}', 'test', ?, 'applied')
                    """,
                    (event_id, event_date, event_date),
                )
            template_rows = [
                ("template-past", "trend_breakout_past", "2025-03-07", 0.70),
                ("template-same", "trend_breakout_same", "2025-03-10", 0.95),
                ("template-future", "trend_breakout_future", "2025-03-20", 0.90),
                ("template-legacy", "trend_breakout_legacy", None, 0.99),
            ]
            for row_id, template, last_sample_date, confidence in template_rows:
                cursor.execute(
                    """
                    INSERT INTO setup_type_performance (
                        id, config_id, ticker, side, setup_type, horizon_class,
                        market_regime, sample_count, win_rate, net_pnl, avg_pnl,
                        profit_factor, confidence_score, last_sample_date,
                        last_updated, valid_until, payload_json
                    ) VALUES (?, 'cfg', 'RB', 'long', ?, 'short',
                        'trend', 4, 0.75, 2000.0, 500.0, 2.0, ?, ?,
                        '2025-03-10', '2025-04-01', '{}')
                    """,
                    (row_id, template, confidence, last_sample_date),
                )
            analyst_rows = [
                ("analyst-past", "long", "2025-03-07", 0.70),
                ("analyst-same", "short", "2025-03-10", 0.95),
                ("analyst-future", "neutral", "2025-03-20", 0.90),
                ("analyst-legacy", "mixed", None, 0.99),
            ]
            for row_id, signal_side, last_sample_date, confidence in analyst_rows:
                cursor.execute(
                    """
                    INSERT INTO analyst_performance (
                        id, config_id, analyst, ticker, sector, horizon_class,
                        signal_side, sample_count, hit_rate, avg_pnl, net_pnl,
                        confidence_score, last_sample_date, last_updated,
                        valid_until, payload_json
                    ) VALUES (?, 'cfg', 'technical', 'RB', 'ferrous', 'short',
                        ?, 4, 0.75, 500.0, 2000.0, ?, ?,
                        '2025-03-10', '2025-04-01', '{}')
                    """,
                    (row_id, signal_side, confidence, last_sample_date),
                )
            digest_rows = [
                ("digest-past", "digest-past-event", 0.70),
                ("digest-same", "digest-same-event", 0.95),
                ("digest-future", "digest-future-event", 0.90),
                ("digest-unknown", None, 0.99),
            ]
            for row_id, event_id, confidence in digest_rows:
                cursor.execute(
                    """
                    INSERT INTO analyst_learning_digest (
                        id, config_id, analyst, ticker, sector, horizon_class,
                        market_regime, digest_text, confidence_score, sample_count,
                        source_event_id, created_at, valid_until, accepted, payload_json
                    ) VALUES (?, 'cfg', 'technical', 'RB', 'ferrous', 'short',
                        'trend', 'test digest', ?, 4, ?, '2025-03-10',
                        '2025-04-01', 1, '{}')
                    """,
                    (row_id, confidence, event_id),
                )
            hypothesis_rows = [
                ("hypothesis-past", "2025-03-07", 0.70, "validated"),
                ("hypothesis-candidate", "2025-03-06", 0.99, "candidate"),
                ("hypothesis-same", "2025-03-10", 0.95, "validated"),
                ("hypothesis-future", "2025-03-20", 0.90, "validated"),
            ]
            for row_id, source_date, confidence, status in hypothesis_rows:
                cursor.execute(
                    """
                    INSERT INTO exploratory_hypothesis (
                        id, config_id, trading_date, scope_type, scope_key,
                        ticker, sector, side, horizon_class, market_regime,
                        hypothesis_text, evidence_summary, suggested_use,
                        confidence_score, sample_count, status, created_at,
                        valid_until, payload_json
                    ) VALUES (?, 'cfg', ?, 'research', 'RB:long', 'RB',
                        'ferrous', 'long', 'short', 'trend', 'test hypothesis',
                        'test evidence', 'prior only', ?, 4, ?,
                        ?, '2025-04-01', '{}')
                    """,
                    (row_id, source_date, confidence, status, source_date),
                )
            conn.commit()
            conn.close()

            templates = db.get_setup_type_performance(
                config_id="cfg",
                ticker="RB",
                side="long",
                trading_date="2025-03-10",
                limit=10,
            )
            analysts = db.get_analyst_performance(
                config_id="cfg",
                ticker="RB",
                horizon_class="short",
                trading_date="2025-03-10",
                limit=10,
            )
            digests = db.get_analyst_learning_digest(
                config_id="cfg",
                analyst="technical",
                ticker="RB",
                sector="ferrous",
                horizon_class="short",
                market_regime="trend",
                trading_date="2025-03-10",
                max_items=10,
            )
            hypotheses = db.get_exploratory_hypotheses(
                config_id="cfg",
                ticker="RB",
                sector="ferrous",
                side="long",
                horizon_class="short",
                market_regime="trend",
                trading_date="2025-03-10",
                limit=10,
            )

        self.assertEqual({row["id"] for row in templates}, {"template-past"})
        self.assertEqual({row["id"] for row in analysts}, {"analyst-past"})
        self.assertEqual({row["id"] for row in digests}, {"digest-past"})
        self.assertEqual({row["id"] for row in hypotheses}, {"hypothesis-past"})

    def test_provisional_policy_state_uses_only_past_source_dates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteDB()
            db.db_path = str(Path(tmpdir) / "agentquant_test.db")
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config (id) VALUES ('cfg')")
            db._ensure_reviewer_learning_schema(cursor)
            rows = [
                ("policy-past", "probe_only_past", "2025-03-07", 0.70),
                ("policy-same", "probe_only_same", "2025-03-10", 0.95),
                ("policy-future", "probe_only_future", "2025-03-20", 0.90),
                ("policy-legacy", "probe_only_legacy", None, 0.99),
            ]
            for row_id, policy_action, source_date, confidence in rows:
                cursor.execute(
                    """
                    INSERT INTO provisional_policy_state (
                        id, config_id, ticker, side, setup_type,
                        horizon_class, policy_action, multiplier,
                        confidence_score, event_type, sample_count, reason,
                        source_trading_date, rollback_value_json, created_at,
                        valid_until, active, payload_json
                    ) VALUES (?, 'cfg', 'RB', 'long', 'trend_breakout',
                        'short', ?, 0.35, ?, 'test', 3,
                        'test boundary', ?, '{}', '2025-03-10',
                        '2025-04-01', 1, '{}')
                    """,
                    (row_id, policy_action, confidence, source_date),
                )
            conn.commit()
            conn.close()

            policies = db.get_provisional_policy_state(
                config_id="cfg",
                ticker="RB",
                side="long",
                setup_type="trend_breakout",
                horizon_class="short",
                trading_date="2025-03-10",
            )

        self.assertEqual({row["id"] for row in policies}, {"policy-past"})

    def test_hold_exit_action_value_reduces_profit_when_current_confirmation_weakens(self):
        current_position = SimpleNamespace(margin_used=100000.0, unrealized_pnl=3000.0)
        ratio, reasons, _notes, diagnostics = _apply_winning_template_continuation_control(
            ticker="P",
            position_ratio=0.0,
            current_ratio=0.04,
            current_position=current_position,
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "action_name": "hold_position",
                    "sample_count": 4,
                    "reward_mean": -1200.0,
                    "reward_sum": -4800.0,
                    "confidence_score": 0.65,
                    "action_preference": "cap_reduce_or_revalidate",
                }
            ],
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.45),
            ],
            market_confirmation={"confirmation_score": 0.42},
            opportunity_scorecard={"long": {"final_state": "watch_for_trigger"}},
            full_config={},
        )

        self.assertAlmostEqual(ratio, 0.02)
        self.assertIn("hold_exit_action_value_protection", reasons)
        detail = diagnostics["winning_template_continuation"]
        self.assertEqual(detail["decision"], "learned_hold_exit_reduce")
        self.assertEqual(detail["selected_action_value"]["action_name"], "hold_position")

    def test_single_exact_positive_exit_candidate_protects_profit_when_confirmation_weakens(self):
        current_position = SimpleNamespace(margin_used=100000.0, unrealized_pnl=3000.0)
        ratio, reasons, _notes, diagnostics = _apply_winning_template_continuation_control(
            ticker="P",
            position_ratio=0.04,
            current_ratio=0.04,
            current_position=current_position,
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "range",
                    "setup_type": "news_event_setup",
                    "action_name": "exit",
                    "sample_count": 1,
                    "reward_mean": 2000.0,
                    "reward_sum": 2000.0,
                    "confidence_score": 0.16,
                    "action_preference": "positive_candidate_exit",
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 1,
                        "exact_state_real_trade_sample_count": 1,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_exit",
                    },
                }
            ],
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            market_confirmation={"confirmation_score": 0.42},
            opportunity_scorecard={"long": {"final_state": "watch_for_trigger"}},
            full_config={},
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("hold_exit_action_value_protection", reasons)
        detail = diagnostics["winning_template_continuation"]
        self.assertEqual(detail["decision"], "learned_exit_action_value_protective_exit")
        self.assertEqual(detail["selected_action_value"]["action_preference"], "positive_candidate_exit")

    def test_hold_exit_action_value_does_not_override_strong_profitable_continuation(self):
        current_position = SimpleNamespace(margin_used=100000.0, unrealized_pnl=3000.0)
        ratio, reasons, _notes, diagnostics = _apply_winning_template_continuation_control(
            ticker="P",
            position_ratio=0.0,
            current_ratio=0.04,
            current_position=current_position,
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "action_name": "hold_position",
                    "sample_count": 4,
                    "reward_mean": -1200.0,
                    "reward_sum": -4800.0,
                    "confidence_score": 0.65,
                    "action_preference": "cap_reduce_or_revalidate",
                }
            ],
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    opportunity_state="tradeable_candidate",
                    invalidation_level=9000.0,
                    position_invalidation_level=9000.0,
                    trigger_valid=True,
                    invalidation_present=True,
                )
            ],
            market_confirmation={"confirmation_score": 0.70},
            opportunity_scorecard={"long": {"final_state": "tradeable_candidate"}},
            full_config={},
        )

        self.assertGreaterEqual(ratio, 0.03)
        self.assertIn("winning_template_continuation", reasons)
        self.assertNotIn("hold_exit_action_value_protection", reasons)
        self.assertEqual(
            diagnostics["winning_template_continuation"]["decision"],
            "preserve_profitable_same_scope_position",
        )

    def test_no_continuation_profitable_hold_reduces_without_action_value(self):
        current_position = SimpleNamespace(margin_used=100000.0, unrealized_pnl=3000.0)
        ratio, reasons, _notes, diagnostics = _apply_winning_template_continuation_control(
            ticker="BU",
            position_ratio=0.0,
            current_ratio=-0.06,
            current_position=current_position,
            alpha_setup_action_values=[],
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.45),
            ],
            market_confirmation={"confirmation_score": 0.50},
            opportunity_scorecard={"short": {"final_state": "watch_for_trigger"}},
            full_config={},
        )

        self.assertAlmostEqual(ratio, -0.03)
        self.assertIn("winning_template_continuation_protective_reduce", reasons)
        detail = diagnostics["winning_template_continuation"]
        self.assertEqual(detail["decision"], "protective_reduce_no_continuation")
        self.assertTrue(detail["prevents_position_matched_profit_giveback"])

    def test_frozen_step4_pool_does_not_retrieve_or_append_for_exit(self):
        open_row = {
            "scope_key": "RB|long|open",
            "ticker": "RB",
            "side": "long",
            "action_name": "open",
            "canonical_action_value": True,
            "canonical_action_family": "open_add_new_risk",
            "canonical_action_value_source": "canonical_action_value",
            "canonical_action_value": True,
            "canonical_action_family": "open_add_new_risk",
            "action_value_lane": "open",
            "learning_lane": "open",
            "consumer_scope": "pm_learning",
            "memory_side_role": "target_side",
            "action_preference": "positive_candidate_open",
            "reward_source": "real_trade",
            "evidence_scope": "exact_real_state",
            "reward_sum": 1200.0,
        }
        contract = {
            "ticker": "RB",
            "current_lots": 2,
            "target_lots": 0,
            "lots_delta": -2,
            "final_action": "exit",
        }
        rows, audit = _audit_frozen_step4_pm_memory(
            contract=contract,
            alpha_setup_action_values=[open_row],
        )

        self.assertEqual([row["scope_key"] for row in rows], ["RB|long|open"])
        self.assertEqual(audit["lifecycle_matching_row_count"], 0)
        self.assertFalse(audit["late_retrieval_performed"])
        self.assertEqual(audit["late_action_value_append_count"], 0)

    def test_frozen_step4_pool_routes_open_and_hold_for_add_without_append(self):
        open_row = {
            "scope_key": "RB|long|open",
            "ticker": "RB",
            "side": "long",
            "action_name": "open",
            "canonical_action_value": True,
            "canonical_action_family": "open_add_new_risk",
            "action_value_lane": "open",
            "learning_lane": "open",
            "consumer_scope": "pm_learning",
            "memory_side_role": "target_side",
            "action_preference": "positive_candidate_open",
            "reward_source": "real_trade",
            "evidence_scope": "exact_real_state",
            "reward_sum": 1200.0,
        }
        hold_row = {
            "scope_key": "RB|long|hold",
            "ticker": "RB",
            "side": "long",
            "action_name": "hold",
            "canonical_action_value": True,
            "canonical_action_family": "hold",
            "action_value_lane": "hold",
            "learning_lane": "hold",
            "consumer_scope": "pm_learning",
            "memory_side_role": "current_position_side",
            "action_preference": "positive_candidate_hold",
            "reward_source": "real_trade",
            "evidence_scope": "exact_real_state",
            "reward_sum": 800.0,
        }
        contract = {
            "ticker": "RB",
            "current_lots": 1,
            "target_lots": 3,
            "lots_delta": 2,
            "final_action": "add",
        }
        rows, audit = _audit_frozen_step4_pm_memory(
            contract=contract,
            alpha_setup_action_values=[open_row, hold_row],
        )

        landed_lanes = {row.get("learning_lane") for row in rows}
        self.assertIn("open", landed_lanes)
        self.assertIn("hold", landed_lanes)
        self.assertEqual(audit["lifecycle_matching_row_count"], 2)
        self.assertFalse(audit["late_retrieval_performed"])

    def test_profit_protective_reduce_is_not_overridden_by_min_hold_lifecycle(self):
        current_position = SimpleNamespace(
            shares=5,
            margin_used=100000.0,
            unrealized_pnl=3000.0,
            entry_date="2025-03-09",
        )
        ratio, reasons, _notes, diagnostics = _apply_holding_rebalance_control(
            ticker="P",
            trading_date="2025-03-10",
            position_ratio=0.03,
            current_ratio=0.06,
            current_position=current_position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            long_scores={"strength": 0.10},
            short_scores={"strength": 0.05},
            market_confirmation={"confirmation_score": 0.42},
            full_config={},
            fusion_context={},
            risk_level=RiskLevel.SAFE,
            adaptive_policy_state=[],
            prior_control_reasons=["winning_template_continuation_protective_reduce"],
        )

        self.assertAlmostEqual(ratio, 0.03)
        self.assertNotIn("holding_period_control", reasons)
        self.assertEqual(
            diagnostics["holding_rebalance_control"]["decision"],
            "allow_profit_protective_reduce_before_min_hold",
        )

    def test_positive_exit_action_value_can_turn_weak_profitable_hold_into_protective_exit(self):
        current_position = SimpleNamespace(margin_used=100000.0, unrealized_pnl=3000.0)
        ratio, reasons, _notes, diagnostics = _apply_winning_template_continuation_control(
            ticker="P",
            position_ratio=0.04,
            current_ratio=0.04,
            current_position=current_position,
            alpha_setup_action_values=[
                {
                    "ticker": "P",
                    "side": "long",
                    "action_name": "exit",
                    "sample_count": 4,
                    "reward_mean": 1800.0,
                    "reward_sum": 7200.0,
                    "confidence_score": 0.70,
                    "action_preference": "controlled_probe_or_hold",
                }
            ],
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.45),
            ],
            market_confirmation={"confirmation_score": 0.42},
            opportunity_scorecard={"long": {"final_state": "watch_for_trigger"}},
            full_config={},
        )

        self.assertAlmostEqual(ratio, 0.0)
        self.assertIn("hold_exit_action_value_protection", reasons)
        detail = diagnostics["winning_template_continuation"]
        self.assertEqual(detail["decision"], "learned_exit_action_value_protective_exit")
        self.assertTrue(detail["protective_exit"])

    def test_real_probe_release_blocks_watch_for_trigger_semantic_even_with_strong_current_evidence(self):
        release, detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
                "unknown_alpha_probe",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "scorecard_state": "watch_for_trigger",
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.70,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(release)
        self.assertIn("alpha_setup_open_action_value_missing", detail["soft_blocks"])
        self.assertFalse(detail["hard_watchlist"])
        self.assertTrue(detail["watch_for_trigger_semantic_block"])

    def test_real_probe_release_allows_tradeable_soft_block_with_current_trigger(self):
        release, detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "horizon_consistency_probe_cap",
                "unknown_alpha_probe",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "candidate_quality": 0.50,
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "has_tradeable_support": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.70,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertTrue(release)
        self.assertIn("alpha_setup_open_action_value_missing", detail["soft_blocks"])
        self.assertFalse(detail["watch_for_trigger_semantic_block"])

    def test_tradeable_analyst_candidate_soft_gates_to_controlled_probe_not_wait(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.66,
            opportunity_state="tradeable_candidate",
            trigger_valid=True,
            invalidation_present=True,
            metadata={
                "action_evidence_contract": {
                    "opportunity_state": "tradeable_candidate",
                    "opportunity_state": "tradeable_candidate",
                    "trigger_valid": True,
                    "invalidation_present": True,
                    "invalidation_level": 5400.0,
                    "invalidation_condition": "long_price_lte_invalidation_level",
                    "position_invalidation_level": 5350.0,
                    "expected_horizon_days": 3,
                }
            },
        )
        reasons = [
            "alpha_setup_ev_fusion",
            "scorecard_current_tradeable_probe_seed",
            "market_confirmation_conflict",
            "single_high_quality_probe_only",
            "unknown_alpha_probe",
        ]
        diagnostics = {
            "alpha_setup_ev_fusion": {
                "scorecard_state": "tradeable_candidate",
                "has_tradeable_support": True,
                "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                "current_confirmation_score": 0.58,
                "independent_support_count": 1,
                "negative_action_value": False,
                "negative_profile": False,
                "repeat_loss_without_new_evidence": False,
            }
        }

        candidate, candidate_detail = _qualified_analyst_tradeable_probe_candidate(
            analyst_signals=[signal],
            target_side="long",
            control_reasons=reasons,
            control_diagnostics=diagnostics,
            account_equity=5_000_000.0,
            current_price=5500.0,
            multiplier=10.0,
            margin_rate=0.10,
            margin_available=1_000_000.0,
        )
        candidate_ratio = _minimum_real_probe_candidate_ratio(
            current_ratio=0.0,
            pre_control_ratio=0.008,
            probe_release=False,
            analyst_tradeable_probe=candidate,
        )
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=candidate_ratio,
            control_reasons=[*reasons, "analyst_tradeable_probe_candidate"],
            probe_release=False,
            alpha_ev_blocks_real_probe=False,
            analyst_tradeable_probe=candidate,
        )
        allowed, authority = _final_contract_authority(
            control_reasons=[*reasons, "analyst_tradeable_probe_candidate"],
            control_diagnostics={
                **diagnostics,
                "analyst_tradeable_probe_candidate": candidate_detail,
            },
        )

        self.assertTrue(candidate)
        self.assertEqual(candidate_detail["decision"], "allow_controlled_probe_candidate")
        self.assertGreater(candidate_ratio, 0.0)
        self.assertTrue(should_attempt)
        self.assertTrue(allowed)
        self.assertEqual(authority["authority_type"], "exploration_probe")
        self.assertIn("analyst_tradeable_probe_candidate", authority["reason_codes"])

    def test_watch_for_trigger_analyst_candidate_does_not_use_controlled_probe_channel(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.66,
            opportunity_state="watch_for_trigger",
            trigger_valid=True,
            invalidation_present=True,
            metadata={
                "action_evidence_contract": {
                    "opportunity_state": "watch_for_trigger",
                    "opportunity_state": "watch_for_trigger",
                    "trigger_valid": True,
                    "invalidation_present": True,
                }
            },
        )
        reasons = [
            "alpha_setup_ev_fusion",
            "scorecard_current_tradeable_probe_seed",
            "pm_watch_for_trigger_probe_cap",
            "unknown_alpha_probe",
        ]
        diagnostics = {
            "alpha_setup_ev_fusion": {
                "scorecard_state": "watch_for_trigger",
                "has_tradeable_support": False,
                "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                "current_confirmation_score": 0.70,
                "independent_support_count": 1,
            }
        }

        candidate, candidate_detail = _qualified_analyst_tradeable_probe_candidate(
            analyst_signals=[signal],
            target_side="long",
            control_reasons=reasons,
            control_diagnostics=diagnostics,
            account_equity=5_000_000.0,
            current_price=5500.0,
            multiplier=10.0,
            margin_rate=0.10,
            margin_available=1_000_000.0,
        )
        candidate_ratio = _minimum_real_probe_candidate_ratio(
            current_ratio=0.0,
            pre_control_ratio=0.008,
            probe_release=False,
            analyst_tradeable_probe=candidate,
        )
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=candidate_ratio,
            control_reasons=reasons,
            probe_release=False,
            alpha_ev_blocks_real_probe=False,
            analyst_tradeable_probe=candidate,
        )

        self.assertFalse(candidate)
        self.assertIn("no_same_side_tradeable_triggered_analyst", candidate_detail["blocked_reasons"])
        self.assertEqual(candidate_ratio, 0.0)
        self.assertFalse(should_attempt)

    def test_negative_learning_blocks_tradeable_analyst_controlled_probe_candidate(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.70,
            opportunity_state="tradeable_candidate",
            trigger_valid=True,
            invalidation_present=True,
        )
        candidate, detail = _qualified_analyst_tradeable_probe_candidate(
            analyst_signals=[signal],
            target_side="short",
            control_reasons=["alpha_setup_ev_fusion", "negative_expectancy_cap_or_exit"],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "negative_action_value": True,
                }
            },
            account_equity=5_000_000.0,
            current_price=5500.0,
            multiplier=10.0,
            margin_rate=0.10,
            margin_available=1_000_000.0,
        )

        self.assertFalse(candidate)
        self.assertIn("negative_or_tail_loss_present", detail["blocked_reasons"])
        self.assertIn("negative_learning_profile_present", detail["blocked_reasons"])

    def test_watch_for_trigger_release_cannot_enter_minimum_one_lot_path(self):
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=0.004,
            control_reasons=[
                "alpha_setup_ev_fusion",
                "pm_watch_for_trigger_probe_cap",
                "horizon_consistency_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
                "unknown_alpha_probe",
            ],
            probe_release=True,
            alpha_ev_blocks_real_probe=False,
        )

        self.assertFalse(should_attempt)

    def test_tradeable_release_enters_minimum_one_lot_path(self):
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=0.004,
            control_reasons=[
                "alpha_setup_ev_fusion",
                "horizon_consistency_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
                "unknown_alpha_probe",
            ],
            probe_release=True,
            alpha_ev_blocks_real_probe=False,
        )

        self.assertTrue(should_attempt)

    def test_watch_for_trigger_release_does_not_recover_pre_control_direction_after_soft_zero(self):
        candidate_ratio = _minimum_real_probe_candidate_ratio(
            current_ratio=0.0,
            pre_control_ratio=0.008,
            probe_release=True,
        )
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=candidate_ratio,
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
                "unknown_alpha_probe",
            ],
            probe_release=True,
            alpha_ev_blocks_real_probe=False,
        )

        self.assertGreater(candidate_ratio, 0.0)
        self.assertFalse(should_attempt)

    def test_real_probe_without_release_does_not_recover_soft_zero_direction(self):
        candidate_ratio = _minimum_real_probe_candidate_ratio(
            current_ratio=0.0,
            pre_control_ratio=0.008,
            probe_release=False,
        )

        self.assertEqual(candidate_ratio, 0.0)

    def test_real_probe_release_path_still_respects_negative_expectancy(self):
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=0.004,
            control_reasons=[
                "alpha_setup_ev_fusion",
                "negative_expectancy_cap_or_exit",
                "repeat_loss_watchlist_only",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            probe_release=False,
            alpha_ev_blocks_real_probe=False,
        )

        self.assertFalse(should_attempt)

    def test_real_probe_release_does_not_override_negative_expectancy(self):
        release, detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "negative_expectancy_cap_or_exit",
                "repeat_loss_watchlist_only",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "qualified_positive_expectancy": False,
                    "current_confirmation_score": 0.75,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertFalse(release)
        self.assertTrue(detail["hard_watchlist"])

    def test_final_new_entry_gate_blocks_watch_for_trigger_without_authority(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
                "horizon_consistency_probe_cap",
                "market_confirmation_conflict",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.50,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertTrue(detail["requires_authority"])
        self.assertEqual(detail["decision"], "watchlist_only")
        self.assertNotIn("pm_watch_for_trigger_probe_cap", detail["weak_markers"])
        self.assertIn("pm_watch_for_trigger_probe_cap", detail["reason_effects"]["candidate_reasons"])
        self.assertNotIn("pm_watch_for_trigger_probe_cap", detail["reason_effects"]["soft_limits"])
        self.assertFalse(detail["reason_effects"]["hard_zero"])

    def test_reason_effect_summary_separates_hard_soft_learning_and_release(self):
        summary = reason_effect_summary([
            "pm_risk_gate_block",
            "market_confirmation_quality_gate",
            "adaptive_policy_cap",
            "qualified_positive_expectancy",
            "positive_open_action_value_seed",
            "hold_exit_action_value_protection",
        ])

        self.assertIn("pm_risk_gate_block", summary["hard_blocks"])
        self.assertIn("market_confirmation_quality_gate", summary["soft_limits"])
        self.assertIn("adaptive_policy_cap", summary["learning_adjustments"])
        self.assertIn("qualified_positive_expectancy", summary["release_signals"])
        self.assertIn("positive_open_action_value_seed", summary["release_signals"])
        self.assertIn("hold_exit_action_value_protection", summary["learning_adjustments"])
        self.assertTrue(summary["hard_zero"])
        candidate_summary = reason_effect_summary([
            "pm_watch_for_trigger_probe_cap",
            "scorecard_current_tradeable_probe_seed",
            "conditional_monitor_probe_seed",
            "conditional_trigger_authority",
        ])
        self.assertIn("pm_watch_for_trigger_probe_cap", candidate_summary["candidate_reasons"])
        self.assertIn("scorecard_current_tradeable_probe_seed", candidate_summary["candidate_reasons"])
        self.assertIn("conditional_monitor_probe_seed", candidate_summary["candidate_reasons"])
        self.assertIn("conditional_trigger_authority", candidate_summary["release_signals"])
        self.assertNotIn("pm_watch_for_trigger_probe_cap", candidate_summary["soft_limits"])
        self.assertFalse(candidate_summary["unknown_trade_effects"])

    def test_learning_policy_blocks_are_soft_not_hard_risk(self):
        summary = reason_effect_summary([
            "adaptive_policy_block",
            "provisional_policy_block",
            "side_performance_block",
            "conditional_performance_block",
            "positive_open_action_value_seed",
        ])

        self.assertEqual(summary["hard_blocks"], [])
        self.assertFalse(summary["hard_zero"])
        self.assertIn("adaptive_policy_block", summary["soft_limits"])
        self.assertIn("provisional_policy_block", summary["soft_limits"])
        self.assertIn("side_performance_block", summary["soft_limits"])
        self.assertIn("conditional_performance_block", summary["soft_limits"])
        self.assertIn("adaptive_policy_block", summary["learning_adjustments"])
        self.assertIn("provisional_policy_block", summary["learning_adjustments"])
        self.assertIn("positive_open_action_value_seed", summary["release_signals"])

    def test_reason_effect_summary_covers_known_exit_reason_codes(self):
        known_exit_reason_codes = [
            "final_contract_authority_not_met",
            "final_contract_authority_not_met",
            "missing_final_contract_authority",
            "final_contract_authority_watchlist_only",
            "final_contract_authority_real_entry_not_allowed",
            "final_action_contract_watch_for_trigger_probe_block",
            "final_contract_authority_probe_lacks_current_evidence",
            "position_budget_authority_not_met",
            "minimum_real_trade_margin_not_reachable",
            "minimum_real_trade_no_feasible_lot",
            "minimum_one_lot_probe_risk_budget_block",
            "minimum_one_lot_probe",
            "minimum_real_trade_margin_floor_applied",
            "exploration_probe_probe_floor_applied",
            "exploration_probe_no_feasible_lot",
            "real_probe_qualification_not_met",
            "market_confirmation_data_gap",
            "market_confirmation_below_probe_threshold",
            "market_confirmation_below_release_threshold",
            "market_confirmation_score_below_probe_threshold",
            "high_quality_learning_evidence_required",
            "confirmation_below_alpha_release_boost_threshold",
            "missing_explicit_stop_for_alpha_release_boost",
            "generic_memory_cannot_trigger_alpha_release_boost",
            "strategy_memory_watchlist_cap",
            "no_analyst_support_for_target",
            "analyst_signal_conflict",
            "protected_memory_evidence_rejected",
            "static_side_cap",
            "trade_frequency_control",
            "trade_churn_cost_control",
            "weak_signal_combo",
            "opportunity_quality_position_sizing",
            "business_quality_deployable",
            "alpha_setup_ev_fusion",
            "learned_underperformance_block",
            "learned_underperformance_policy",
            "capital_utilization_soft_limit_respected",
            "capital_utilization_guard",
            "capital_utilization_memory_protected",
            "capital_utilization_same_side_add_on",
            "drawdown_control",
            "drawdown_recovery_probe",
            "ticker_loss_control",
            "mature_alpha_release",
            "mature_alpha_with_invalidation",
            "high_quality_bearish_short_probe",
            "position_lifecycle_failed",
            "new_position_loss_revalidation_failed",
            "exploration_probe_reconfirm_failed",
            "exploration_probe_reconfirm_reduce",
            "horizon_consistency_failed_losing_hold",
            "position_lifecycle_loss_revalidation_failed",
            "position_lifecycle_probe_expired",
            "fundamental_anchor_rebalance_cap",
            "reverse_requires_stronger_evidence",
            "intraday_trigger_not_met",
            "intraday_waiting_for_trigger",
            "market_rule_block",
            "market_rule_or_execution_block",
            "near_expiry_new_entry_block",
            "delivery_month_new_entry_block",
            "final_contract_authority_source_mismatch",
            "positive_open_action_value_seed",
            "hold_exit_action_value_protection",
            "winning_template_continuation_protective_reduce",
        ]
        summary = reason_effect_summary(known_exit_reason_codes)
        self.assertFalse(summary["unknown_trade_effects"])
        self.assertIn("market_confirmation_data_gap", summary["soft_limits"])
        self.assertIn("drawdown_control", summary["learning_adjustments"])
        self.assertIn("mature_alpha_release", summary["release_signals"])

    def test_final_new_entry_gate_blocks_watch_for_trigger_without_monitorable_setup(self):
        release_allowed, release_detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "has_tradeable_support": False,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.70,
                    "independent_support_count": 1,
                }
            },
        )
        strong_allowed, strong_detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_tradeable_support": False,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.60,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertFalse(release_allowed)
        self.assertEqual(release_detail["decision"], "watchlist_only")
        self.assertEqual(release_detail["authority_type"], "watchlist_only")
        self.assertIn("pm_watch_for_trigger_probe_cap", release_detail["reason_effects"]["candidate_reasons"])
        self.assertNotIn("pm_watch_for_trigger_probe_cap", release_detail["reason_effects"]["soft_limits"])
        self.assertTrue(release_detail["watch_for_trigger_semantic_block"])
        self.assertFalse(strong_allowed)
        self.assertEqual(strong_detail["authority_type"], "watchlist_only")
        self.assertTrue(strong_detail["strong_current_evidence"])
        self.assertTrue(strong_detail["watch_for_trigger_semantic_block"])
        self.assertFalse(strong_detail["reason_effects"]["hard_zero"])

    def test_final_new_entry_gate_allows_conditional_watch_for_trigger_probe_contract(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "scorecard_current_tradeable_probe_seed",
                "pm_watch_for_trigger_probe_cap",
                "unknown_alpha_probe",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "has_tradeable_support": False,
                    "has_monitorable_setup": True,
                    "setup_quality_ok": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": False,
                    "technical_opposes_side": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "negative_action_value": False,
                    "repeat_loss_without_new_evidence": False,
                    "current_confirmation_score": 0.50,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertTrue(allowed)
        self.assertEqual(detail["authority_type"], "exploration_probe")
        self.assertEqual(detail["decision"], "allow_exploration_probe")
        self.assertTrue(detail["conditional_trigger_authority"])
        self.assertTrue(detail["requires_intraday_confirmation"])
        self.assertFalse(detail["can_execute_without_intraday_trigger"])
        self.assertFalse(detail["watch_for_trigger_block"])
        self.assertFalse(detail["watch_for_trigger_semantic_block"])
        self.assertFalse(detail["open_action_evidence"])
        self.assertIn("conditional_trigger_authority", detail["reason_codes"])

    def test_final_new_entry_gate_blocks_conditional_authority_when_dominant_opposition_unresolved(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "scorecard_current_tradeable_probe_seed",
                "pm_watch_for_trigger_probe_cap",
                "market_confirmation_conflict",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "scorecard_gating_failures": [
                        "dominant_opposing_evidence_requires_pm_resolution",
                    ],
                    "has_tradeable_support": False,
                    "has_monitorable_setup": True,
                    "setup_quality_ok": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": False,
                    "technical_opposes_side": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "negative_action_value": False,
                    "repeat_loss_without_new_evidence": False,
                    "current_confirmation_score": 0.50,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["authority_type"], "watchlist_only")
        self.assertTrue(detail["unresolved_dominant_opposition"])
        self.assertFalse(detail["conditional_trigger_authority"])
        self.assertFalse(detail["requires_intraday_confirmation"])
        self.assertIsNone(detail["can_execute_without_intraday_trigger"])
        self.assertNotIn("conditional_trigger_authority", detail["reason_codes"])
        self.assertIn("dominant_opposing_evidence_unresolved", detail["reason_codes"])

    def test_pm_watch_for_trigger_candidate_becomes_conditional_final_contract(self):
        reasons = [
            "alpha_setup_ev_fusion",
            "pm_watch_for_trigger_probe_cap",
            "horizon_consistency_probe_cap",
            "market_confirmation_conflict",
        ]
        diagnostics = {
            "conditional_monitor_probe_seed": {
                "side": "short",
                "ratio": -0.008,
                "scorecard": {
                    "final_state": "watch_for_trigger",
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "invalidation_present": True,
                    "entry_trigger": "wait for post-open break below support",
                },
                "requires_intraday_confirmation": True,
            },
            "alpha_setup_ev_fusion": {
                "scorecard_state": "watch_for_trigger",
                "has_tradeable_support": False,
                "has_monitorable_setup": True,
                "setup_quality_ok": True,
                "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                "technical_supports_side": True,
                "technical_entry_timing_supports_side": False,
                "technical_opposes_side": False,
                "strong_realtime_evidence": False,
                "strong_market_confirmation": False,
                "qualified_positive_expectancy": False,
                "positive_action_value": False,
                "negative_action_value": False,
                "repeat_loss_without_new_evidence": False,
                "current_confirmation_score": 0.45,
                "independent_support_count": 1,
            },
        }
        plan = _conditional_monitor_probe_seed_plan(
            ticker="BU",
            current_lots=0,
            target_lots=0,
            target_ratio=-0.008,
            current_ticker_exposure=0.0,
            current_net_exposure=0.0,
            account_equity=5_000_000.0,
            current_price=3500.0,
            multiplier=10.0,
            margin_rate=0.10,
            margin_available=1_000_000.0,
            max_position_ratio=0.12,
            max_net_exposure=0.40,
            morning_price_context={},
            control_reasons=reasons,
            control_diagnostics=diagnostics,
            full_config={},
        )
        self.assertTrue(plan["allowed"], plan)

        target_lots = int(plan["target_lots"])
        target_ratio = float(plan["signed_one_lot_ratio"])
        reasons_with_authority = [*reasons, "conditional_trigger_authority"]
        allowed, authority = _final_contract_authority(
            control_reasons=reasons_with_authority,
            control_diagnostics=diagnostics,
        )
        contract = _build_final_action_contract(
            ticker="BU",
            current_lots=0,
            target_lots=target_lots,
            position_ratio=target_ratio,
            margin_required=float(plan["margin_required"]),
            account_equity=5_000_000.0,
            lots_to_trade=abs(target_lots),
            lots_to_trade_reason="conditional_trigger_authority",
            recommendation_intent={"action": "open_short", "lots": abs(target_lots), "action_type": "open"},
            final_entry_authority=authority,
            control_reasons=reasons_with_authority,
            control_diagnostics=diagnostics,
            opportunity_scorecard={
                "preferred_side": "short",
                "short": diagnostics["conditional_monitor_probe_seed"]["scorecard"],
            },
            market_confirmation={"confirmation_score": 0.45, "conflicts": ["market_confirmation_conflict"]},
            alpha_setup_action_values=[],
            execution_contract_fields={
                "execution_profile": "breakout",
                "entry_trigger": "wait for post-open break below support",
                "invalidation": "above resistance",
                "requires_intraday_confirmation": True,
                "can_execute_without_intraday_trigger": False,
            },
        )

        self.assertTrue(allowed)
        self.assertEqual(target_lots, -1)
        self.assertEqual(contract["final_action"], "open_probe")
        self.assertEqual(contract["target_lots"], -1)
        self.assertEqual(contract["lots_delta"], -1)
        self.assertTrue(contract["conditional_trigger_authority"])
        self.assertTrue(contract["requires_intraday_confirmation"])
        self.assertFalse(contract["can_execute_without_intraday_trigger"])
        self.assertIn("conditional_trigger_authority", contract["reason_codes"])

    def test_conditional_watch_for_trigger_real_pm_evidence_snapshot_gets_authority(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.62,
            opportunity_state="watch_for_trigger",
            entry_trigger="wait for post-open break below support with volume confirmation",
            invalidation_level=3520.0,
            position_invalidation_level=3520.0,
            trigger_valid=False,
            evidence_role="entry_timing",
            metadata=_canonical_pm_execution_metadata(
                side="short",
                state="watch_for_trigger",
                confirmed=False,
                ticker="BU",
            ),
        )
        alpha_ev = _current_open_evidence_snapshot(
            side="short",
            analyst_signals=[signal],
            opportunity_scorecard={
                "short": {
                    "final_state": "watch_for_trigger",
                    "score": 0.56,
                    "supporting_signal_count": 1,
                    "setup_quality_ok": True,
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "invalidation_present": True,
                    "entry_trigger": "wait for post-open break below support with volume confirmation",
                    "gating_failures": [],
                }
            },
            market_confirmation={"confirmation_score": 0.50},
            ev_cfg={"real_trade_min_analyst_confidence": 0.45},
        )

        self.assertFalse(alpha_ev["has_tradeable_support"])
        self.assertTrue(alpha_ev["has_monitorable_setup"])
        self.assertFalse(alpha_ev["trade_authority"]["watch_for_trigger_without_setup"])

        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "scorecard_current_tradeable_probe_seed",
                "pm_watch_for_trigger_probe_cap",
            ],
            control_diagnostics={"alpha_setup_ev_fusion": alpha_ev},
        )

        self.assertTrue(allowed)
        self.assertTrue(detail["conditional_trigger_authority"])
        self.assertTrue(detail["requires_intraday_confirmation"])
        self.assertFalse(detail["can_execute_without_intraday_trigger"])
        self.assertFalse(detail["watch_for_trigger_semantic_block"])

    def test_final_new_entry_gate_blocks_watch_for_trigger_without_setup_quality(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "scorecard_current_tradeable_probe_seed",
                "pm_watch_for_trigger_probe_cap",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "has_tradeable_support": True,
                    "setup_quality_ok": False,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": False,
                    "technical_opposes_side": False,
                    "current_confirmation_score": 0.50,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["authority_type"], "watchlist_only")
        self.assertFalse(detail["conditional_trigger_authority"])
        self.assertTrue(detail["watch_for_trigger_block"])

    def test_final_new_entry_gate_allows_tradeable_release_or_strong_current_evidence(self):
        release_allowed, release_detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "horizon_consistency_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "has_tradeable_support": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.70,
                    "independent_support_count": 1,
                }
            },
        )
        strong_allowed, strong_detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "horizon_consistency_probe_cap",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_tradeable_support": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.60,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertTrue(release_allowed)
        self.assertEqual(release_detail["decision"], "allow_exploration_probe")
        self.assertEqual(release_detail["authority_type"], "exploration_probe")
        self.assertFalse(release_detail["watch_for_trigger_semantic_block"])
        self.assertTrue(strong_allowed)
        self.assertEqual(strong_detail["authority_type"], "exploration_probe")
        self.assertTrue(strong_detail["strong_current_evidence"])
        self.assertFalse(strong_detail["watch_for_trigger_semantic_block"])

    def test_final_new_entry_gate_blocks_conflict_probe_without_stronger_confirmation(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "market_confirmation_conflict",
                "pm_watch_for_trigger_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
                "single_high_quality_probe_only",
                "unknown_alpha_probe",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.56,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["authority_type"], "watchlist_only")
        self.assertTrue(detail["weak_conflict_probe"])
        self.assertIn("weak_conflict_probe_requires_stronger_confirmation", detail["reason_codes"])

    def test_conflict_probe_with_tradeable_current_confirmation_can_still_probe(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "market_confirmation_conflict",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "has_tradeable_support": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.62,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertTrue(allowed)
        self.assertEqual(detail["authority_type"], "exploration_probe")
        self.assertFalse(detail["weak_conflict_probe"])

    def test_hard_risk_reason_still_blocks_even_with_release_signal(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "pm_risk_gate_block",
                "real_probe_positive_or_strong_confirmation_release",
                "qualified_positive_expectancy",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_tradeable_support": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "current_confirmation_score": 0.72,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertTrue(detail["hard_zero"])
        self.assertIn("pm_risk_gate_block", detail["reason_effects"]["hard_blocks"])
        self.assertIn("qualified_positive_expectancy", detail["reason_effects"]["release_signals"])
        self.assertEqual(detail["authority_type"], "watchlist_only")

    def test_watch_for_trigger_fundamental_news_stack_does_not_release_real_probe(self):
        release, release_detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
                "horizon_consistency_probe_cap",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "fundamental_supports_side": True,
                    "news_supports_side": True,
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.60,
                    "independent_support_count": 2,
                }
            },
        )
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "pm_watch_for_trigger_probe_cap",
                "horizon_consistency_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "fundamental_supports_side": True,
                    "news_supports_side": True,
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.60,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertFalse(release)
        self.assertTrue(release_detail["watch_for_trigger_without_confirmation"])
        self.assertFalse(allowed)
        self.assertTrue(detail["requires_authority"])
        self.assertEqual(detail["decision"], "watchlist_only")
        self.assertTrue(detail["watch_for_trigger_without_confirmation"])

    def test_watch_for_trigger_single_fundamental_market_score_does_not_release_real_probe(self):
        """Regression for 2025-04-08 ZN: market score cannot replace tradeable setup."""
        release, release_detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "horizon_consistency_probe_cap",
                "pm_watch_for_trigger_probe_cap",
                "single_high_quality_probe_only",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "technical_supports_side": False,
                    "fundamental_supports_side": True,
                    "news_supports_side": False,
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.70,
                    "independent_support_count": 1,
                }
            },
        )
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "horizon_consistency_probe_cap",
                "market_confirmation_conflict",
                "pm_watch_for_trigger_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
                "single_high_quality_probe_only",
                "unknown_alpha_probe",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "technical_supports_side": False,
                    "fundamental_supports_side": True,
                    "news_supports_side": False,
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "current_confirmation_score": 0.70,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(release)
        self.assertFalse(release_detail["current_trade_authority"])
        self.assertTrue(release_detail["watch_for_trigger_without_confirmation"])
        self.assertFalse(allowed)
        self.assertEqual(detail["decision"], "watchlist_only")

    def test_tradeable_candidate_with_current_confirmation_can_still_probe(self):
        release, release_detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "horizon_consistency_probe_cap",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "has_tradeable_support": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": False,
                    "current_confirmation_score": 0.62,
                    "independent_support_count": 2,
                }
            },
        )
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "horizon_consistency_probe_cap",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "has_tradeable_support": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": False,
                    "current_confirmation_score": 0.62,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertTrue(release)
        self.assertTrue(release_detail["executable_setup_confirmation"])
        self.assertTrue(allowed)
        self.assertTrue(detail["strong_current_evidence"])
        self.assertEqual(detail["capital_layer"], CAPITAL_LAYER_EXPLORATION)
        self.assertIn("horizon_consistency_probe_cap", detail["reason_effects"]["soft_limits"])

    def test_fundamental_tradeable_candidate_with_strong_market_confirmation_can_probe(self):
        """Regression for 2025-04-10 ZN / 2025-04-11 HC over-block.

        A medium-horizon fundamental tradeable setup is not a weak direction-only
        opinion when the current market confirmation is strong and technical is
        not explicitly against the side. It should survive soft probe caps as a
        controlled probe candidate, but it is still not real-budget authority
        without open-action evidence.
        """
        control_reasons = [
            "alpha_setup_ev_fusion",
            "horizon_consistency_probe_cap",
            "single_high_quality_probe_only",
        ]
        diagnostics = {
            "alpha_setup_ev_fusion": {
                "scorecard_state": "tradeable_candidate",
                "strong_realtime_evidence": False,
                "strong_market_confirmation": True,
                "technical_supports_side": False,
                "technical_opposes_side": False,
                "fundamental_supports_side": True,
                "news_supports_side": False,
                "has_tradeable_support": True,
                "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                "qualified_positive_expectancy": False,
                "positive_action_value": False,
                "positive_profile": False,
                "current_confirmation_score": 0.75,
                "independent_support_count": 1,
            }
        }

        release, release_detail = _qualified_real_probe_release(
            control_reasons=control_reasons,
            control_diagnostics=diagnostics,
        )
        allowed, detail = _final_contract_authority(
            control_reasons=[
                *control_reasons,
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics=diagnostics,
        )

        self.assertTrue(release)
        self.assertTrue(release_detail["market_confirmation"])
        self.assertTrue(release_detail["executable_setup_confirmation"])
        self.assertTrue(allowed)
        self.assertEqual(detail["decision"], "allow_exploration_probe")
        self.assertEqual(detail["authority_type"], "exploration_probe")
        self.assertTrue(detail["open_action_evidence"])
        self.assertTrue(detail["strong_current_evidence"])

    def test_static_prior_weights_never_create_trade_authority(self):
        allowed, detail = _final_contract_authority(
            control_reasons=["alpha_setup_ev_fusion", "pm_watch_for_trigger_probe_cap"],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "fundamental_supports_side": True,
                    "news_supports_side": True,
                    "has_tradeable_support": False,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": False,
                    "current_confirmation_score": 0.80,
                    "independent_support_count": 2,
                }
            },
            full_config={
                "analyst_weight_policy": {
                    "mode": "evidence_router",
                    "static_weights_mode": "prior_only",
                    "static_weights_can_create_trade_authority": False,
                    "watch_for_trigger_cannot_open_position": True,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["decision"], "watchlist_only")
        self.assertFalse(detail["open_action_evidence"])
        self.assertEqual(detail["analyst_prior_policy"]["static_weights_mode"], "prior_only")
        self.assertEqual(detail["analyst_prior_audit"]["semantic_role"], "cold_start_prior_only")
        self.assertFalse(detail["analyst_prior_audit"]["can_create_trade_authority"])
        self.assertFalse(detail["analyst_prior_audit"]["can_open_position_directly"])
        self.assertIn("portfolio_manager.sector_weights", detail["analyst_prior_audit"]["runtime_compat_fields"])

    def test_watch_for_trigger_probe_config_is_audited_candidate_not_authority(self):
        allowed, detail = _final_contract_authority(
            control_reasons=["alpha_setup_ev_fusion", "pm_watch_for_trigger_probe_cap"],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "watch_for_trigger_without_setup": True,
                    "has_tradeable_support": False,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": False,
                    "strong_market_confirmation": True,
                    "current_confirmation_score": 0.72,
                    "independent_support_count": 2,
                }
            },
            full_config={
                "analyst_weight_policy": {
                    "watch_for_trigger_cannot_open_position": True,
                    "mode": "evidence_router",
                    "static_weights_can_create_trade_authority": False,
                },
                "portfolio_manager": {
                    "holding_rebalance_control": {
                        "watch_for_trigger_new_entry": {
                            "enabled": True,
                            "allow_probe": True,
                            "semantic_role": "observation_candidate_only",
                            "can_create_trade_authority": False,
                            "requires_final_contract_authority": True,
                        }
                    }
                },
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["authority_type"], "watchlist_only")
        audit = detail["watch_for_trigger_semantic_audit"]
        self.assertTrue(audit["allow_probe"])
        self.assertEqual(audit["semantic_role"], "observation_candidate_only")
        self.assertFalse(audit["can_create_trade_authority"])
        self.assertTrue(audit["requires_final_contract_authority"])
        self.assertIn("watch_for_trigger_new_entry", detail["source_parameters"])

    def test_positive_expectancy_without_current_open_evidence_does_not_get_real_budget(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "qualified_positive_expectancy",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "has_tradeable_support": False,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "technical_supports_side": False,
                    "technical_entry_timing_supports_side": False,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "current_confirmation_score": 0.80,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["decision"], "watchlist_only")
        self.assertTrue(detail["qualified_positive"])
        self.assertFalse(detail["open_action_evidence"])

    def test_fundamental_tradeable_candidate_with_technical_opposition_stays_watchlist(self):
        release, detail = _qualified_real_probe_release(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "horizon_consistency_probe_cap",
                "single_high_quality_probe_only",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "technical_supports_side": False,
                    "technical_opposes_side": True,
                    "fundamental_supports_side": True,
                    "has_tradeable_support": True,
                    "qualified_positive_expectancy": False,
                    "current_confirmation_score": 0.80,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(release)
        self.assertFalse(detail["current_trade_authority"])

    def test_learned_positive_tradeable_candidate_flows_from_scorecard_to_real_probe(self):
        """Full chain: analyst setup -> scorecard -> PM seed -> action-value -> real probe."""
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.50,
            opportunity_state="tradeable_candidate",
            setup_quality_score=0.76,
            business_quality_score=0.72,
            trigger_valid=True,
            invalidation_present=True,
            invalidation_level=21000.0,
            position_invalidation_level=21000.0,
            evidence_role="entry_timing",
            metadata=_canonical_pm_execution_metadata(ticker="ZN"),
        )
        fundamental = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.50,
            opportunity_state="no_opportunity",
            setup_quality_score=0.76,
            business_quality_score=0.72,
            would_change_view_if="long setup invalid if basis flips to contango or inventory builds for two consecutive weeks",
            horizon_class="medium",
        )
        news = AnalystSignal(
            agent_name="commodity_news",
            signal=Signal.NEUTRAL,
            confidence=0.30,
            opportunity_state="no_opportunity",
        )
        scorecard = build_opportunity_scorecard(
            ticker="ZN",
            analyst_signals=[technical, fundamental, news],
            market_confirmation={"confirmation_score": 0.75},
            data_quality_summary={"critical_gap": False, "fundamental_trade_setup_gap": False},
            config={
                "weak_confirmation_threshold": 0.45,
                "tradeable_threshold": 0.58,
                "min_tradeable_candidate_setup_quality": 0.55,
                "single_tradeable_candidate_setup_confirmation_score": 0.68,
                "single_tradeable_candidate_setup_min_business_quality": 0.60,
                "single_tradeable_candidate_setup_min_confidence": 0.42,
                "technical_opposition_min_confidence": 0.45,
            },
        )
        scorecard["preferred_side"] = "long"
        side, seed_ratio, row = _scorecard_probe_seed(
            opportunity_scorecard=scorecard,
            control={
                "watch_for_trigger_new_entry": {
                    "allow_probe": True,
                    "probe_floor_ratio": 0.005,
                    "probe_max_ratio": 0.010,
                    "scorecard_tradeable_candidate_probe_min_confirmation_score": 0.68,
                }
            },
        )

        ratio, reasons, _notes, diagnostics = _apply_alpha_setup_ev_position_control(
            ticker="ZN",
            position_ratio=seed_ratio,
            current_ratio=0.0,
            opportunity_scorecard=scorecard,
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "ticker": "ZN",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "action_name": "open",
                    "sample_count": 4,
                    "reward_mean": 600.0,
                    "reward_sum": 2400.0,
                    "win_rate": 0.75,
                    "confidence_score": 0.66,
                    "action_preference": "controlled_open_or_add",
                    "max_position_impact": 0.03,
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 4,
                        "exact_state_real_trade_sample_count": 4,
                        "amplification_scope_quality": "exact_real_state",
                        "action_preference": "positive_candidate_open",
                        "reward_source": "trade_episode",
                    },
                }
            ],
            analyst_signals=[technical, fundamental, news],
            market_confirmation={"confirmation_score": 0.75},
            full_config={},
            max_position_ratio=0.05,
        )
        control_reasons = [
            *reasons,
            "horizon_consistency_probe_cap",
        ]
        release, release_detail = _qualified_real_probe_release(
            control_reasons=control_reasons,
            control_diagnostics=diagnostics,
        )
        candidate_ratio = _minimum_real_probe_candidate_ratio(
            current_ratio=0.0,
            pre_control_ratio=ratio,
            probe_release=release,
        )
        should_attempt = _should_attempt_minimum_real_probe(
            current_lots=0,
            target_lots=0,
            target_ratio=candidate_ratio,
            control_reasons=[
                *control_reasons,
                "real_probe_positive_or_strong_confirmation_release",
            ],
            probe_release=release,
            alpha_ev_blocks_real_probe=False,
        )
        allowed, final_detail = _final_contract_authority(
            control_reasons=[
                *control_reasons,
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics=diagnostics,
        )

        self.assertEqual(scorecard["long"]["final_state"], "probe_candidate")
        self.assertTrue(scorecard["long"]["single_tradeable_candidate_setup_confirmed"])
        self.assertEqual(side, "long")
        self.assertEqual(row["final_state"], "probe_candidate")
        self.assertGreater(seed_ratio, 0.0)
        self.assertGreater(ratio, seed_ratio)
        self.assertIn("qualified_positive_expectancy", reasons)
        self.assertTrue(diagnostics["alpha_setup_ev_fusion"]["qualified_positive_expectancy"])
        self.assertTrue(release)
        self.assertTrue(release_detail["qualified_positive_expectancy"])
        self.assertGreater(candidate_ratio, 0.0)
        self.assertTrue(should_attempt)
        self.assertTrue(allowed)
        self.assertEqual(final_detail["decision"], "allow_real_new_entry")

    def test_pm_trade_decision_matrix_has_single_non_bypassable_outlet(self):
        """Matrix guardrail: the final outlet is permissive for edge, strict for weak/negative ideas."""
        cases = {
            "watch_for_trigger_no_current_or_learned_edge": (
                False,
                ["alpha_setup_ev_fusion", "pm_watch_for_trigger_probe_cap"],
                {
                    "scorecard_state": "watch_for_trigger",
                    "has_tradeable_support": False,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "current_confirmation_score": 0.50,
                    "independent_support_count": 1,
                },
            ),
            "positive_expectancy_watch_for_trigger_without_current_open_evidence": (
                False,
                ["alpha_setup_ev_fusion", "pm_watch_for_trigger_probe_cap"],
                {
                    "scorecard_state": "watch_for_trigger",
                    "has_tradeable_support": False,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "positive_profile": False,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "current_confirmation_score": 0.45,
                    "independent_support_count": 1,
                },
            ),
            "confirmed_tradeable_candidate_probe": (
                True,
                [
                    "alpha_setup_ev_fusion",
                    "horizon_consistency_probe_cap",
                    "real_probe_positive_or_strong_confirmation_release",
                ],
                {
                    "scorecard_state": "tradeable_candidate",
                    "has_tradeable_support": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "technical_supports_side": False,
                    "technical_opposes_side": False,
                    "current_confirmation_score": 0.75,
                    "independent_support_count": 1,
                },
            ),
            "technical_opposition_blocks_single_setup": (
                False,
                [
                    "alpha_setup_ev_fusion",
                    "horizon_consistency_probe_cap",
                    "real_probe_positive_or_strong_confirmation_release",
                ],
                {
                    "scorecard_state": "tradeable_candidate",
                    "has_tradeable_support": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "technical_supports_side": False,
                    "technical_opposes_side": True,
                    "current_confirmation_score": 0.80,
                    "independent_support_count": 1,
                },
            ),
            "negative_same_scope_without_new_evidence": (
                False,
                [
                    "alpha_setup_ev_fusion",
                    "negative_expectancy_cap_or_exit",
                    "repeat_loss_watchlist_only",
                    "real_probe_positive_or_strong_confirmation_release",
                ],
                {
                    "scorecard_state": "tradeable_candidate",
                    "has_tradeable_support": True,
                    "qualified_positive_expectancy": False,
                    "negative_action_value": True,
                    "repeat_loss_without_new_evidence": True,
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "current_confirmation_score": 0.80,
                    "independent_support_count": 2,
                },
            ),
        }

        for name, (expected, reasons, alpha_ev) in cases.items():
            with self.subTest(name=name):
                allowed, detail = _final_contract_authority(
                    control_reasons=reasons,
                    control_diagnostics={"alpha_setup_ev_fusion": alpha_ev},
                )
                self.assertEqual(allowed, expected, detail)

    def test_positive_expectancy_watch_for_trigger_without_current_open_evidence_stays_watchlist(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "pm_watch_for_trigger_probe_cap",
                "qualified_positive_expectancy",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": False,
                    "technical_entry_timing_supports_side": False,
                    "has_tradeable_support": False,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "positive_profile": False,
                    "current_confirmation_score": 0.40,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["decision"], "watchlist_only")
        self.assertFalse(detail["strong_current_evidence"])
        self.assertTrue(detail["qualified_positive"])
        self.assertFalse(detail["open_action_evidence"])

    def test_watch_for_trigger_positive_history_and_probe_seed_cannot_bypass_final_authority(self):
        """Regression for 2025-03-13 RB: no-trade rationale must not open via probe seed."""
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "scorecard_current_tradeable_probe_seed",
                "pm_watch_for_trigger_probe_cap",
                "horizon_consistency_probe_cap",
                "market_confirmation_quality_gate",
                "market_confirmation_conflict",
                "qualified_positive_expectancy",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": False,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": False,
                    "trigger_valid": False,
                    "event_catalyst_supports_side": False,
                    "has_tradeable_support": False,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "positive_profile": False,
                    "current_confirmation_score": 0.33,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertEqual(detail["decision"], "watchlist_only")
        self.assertEqual(detail["authority_type"], "watchlist_only")
        self.assertTrue(detail["watch_for_trigger_block"])
        self.assertTrue(detail["watch_for_trigger_without_setup"])
        self.assertTrue(detail["qualified_positive"])
        self.assertFalse(detail["open_action_evidence"])
        self.assertEqual(detail["max_allowed_margin_ratio"], 0.0)

    def test_final_new_entry_gate_negative_profile_stays_watchlist(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "alpha_setup_open_action_value_missing",
                "real_probe_positive_or_strong_confirmation_release",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "qualified_positive_expectancy": False,
                    "negative_action_value": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "current_confirmation_score": 0.75,
                    "independent_support_count": 2,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertTrue(detail["negative_profile"])

    def test_final_entry_authority_records_source_parameters_and_real_flags(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "real_probe_positive_or_strong_confirmation_release",
                "qualified_positive_expectancy",
            ],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "candidate_quality": 0.50,
                    "has_tradeable_support": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "strong_realtime_evidence": True,
                    "current_confirmation_score": 0.72,
                    "independent_support_count": 2,
                }
            },
            full_config={
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "min_real_trade_margin_abs": 40000,
                    "probe_margin_ratio": 0.008,
                    "probe_margin_max_ratio": 0.015,
                    "normal_trade_margin_max_ratio": 0.06,
                },
                "analyst_weight_policy": {
                    "watch_for_trigger_cannot_open_position": True,
                    "strategic_view_cannot_open_position": True,
                },
                "market_confirmation": {"min_confirmation_score_for_new_entry": 0.55},
                "portfolio_manager": {
                    "quality_aware_fusion": {
                        "opportunity_scorecard": {
                            "single_tradeable_candidate_setup_confirmation_score": 0.68,
                        }
                    }
                },
            },
        )

        self.assertTrue(allowed)
        self.assertEqual(detail["authority_type"], "real_budget_entry")
        self.assertTrue(detail["open_action_evidence"])
        self.assertTrue(detail["strong_current_evidence"])
        self.assertEqual(detail["capital_layer"], CAPITAL_LAYER_REAL_BUDGET)
        self.assertAlmostEqual(detail["target_margin_ratio"], 0.045)
        self.assertIn("source_parameters", detail)
        self.assertEqual(
            detail["source_parameters"]["position_budget_policy"]["min_real_trade_margin_ratio"],
            0.008,
        )

    def test_mature_positive_learning_and_strong_current_evidence_upgrade_step4_to_scale(self):
        allowed, detail = _final_contract_authority(
            control_reasons=["alpha_setup_ev_fusion", "qualified_positive_expectancy"],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "candidate_quality": 0.75,
                    "has_tradeable_support": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "technical_opposes_side": False,
                    "fundamental_supports_side": True,
                    "fundamental_opposes_side": False,
                    "current_confirmation_score": 0.78,
                    "independent_support_count": 2,
                    "action_value_stats": {
                        "sample_count": 5,
                        "reward_sum": 12000.0,
                        "mean_return_on_notional": 0.024,
                    },
                }
            },
            full_config={
                "position_budget_policy": {
                    "deployable_margin_ratio": 0.060,
                    "deployable_margin_max_ratio": 0.120,
                    "hard_max_total_margin_ratio": 0.20,
                }
            },
        )

        self.assertTrue(allowed)
        self.assertEqual(detail["authority_type"], "real_budget_entry")
        self.assertEqual(detail["capital_layer"], CAPITAL_LAYER_ALPHA_SCALE)
        self.assertAlmostEqual(detail["target_margin_ratio"], 0.105)
        self.assertIn("step4_alpha_scale_release", detail["reason_codes"])

    def test_four_positive_samples_release_real_budget_but_not_alpha_scale(self):
        allowed, detail = _final_contract_authority(
            control_reasons=["alpha_setup_ev_fusion", "qualified_positive_expectancy"],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "candidate_quality": 0.75,
                    "has_tradeable_support": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "technical_opposes_side": False,
                    "fundamental_supports_side": True,
                    "fundamental_opposes_side": False,
                    "current_confirmation_score": 0.78,
                    "independent_support_count": 2,
                    "action_value_stats": {
                        "sample_count": 4,
                        "reward_sum": 9000.0,
                        "mean_return_on_notional": 0.018,
                    },
                }
            },
            full_config={
                "portfolio_manager": {
                    "alpha_setup_ev_fusion": {
                        "real_trade_min_action_value_samples": 2,
                        "alpha_scale_min_action_value_samples": 5,
                    }
                },
                "position_budget_policy": {
                    "normal_trade_margin_ratio": 0.030,
                    "normal_trade_margin_max_ratio": 0.060,
                    "deployable_margin_ratio": 0.060,
                    "deployable_margin_max_ratio": 0.120,
                },
            },
        )

        self.assertTrue(allowed)
        self.assertEqual(detail["authority_type"], "real_budget_entry")
        self.assertEqual(detail["capital_layer"], CAPITAL_LAYER_REAL_BUDGET)
        self.assertFalse(detail["alpha_scale_eligible"])

    def test_pending_technical_setup_can_scale_without_becoming_direct_execution(self):
        allowed, detail = _final_contract_authority(
            control_reasons=[
                "alpha_setup_ev_fusion",
                "qualified_positive_expectancy",
                "pm_watch_for_trigger_probe_cap",
            ],
            control_diagnostics={
                "conditional_monitor_probe_seed": {"side": "long"},
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "watch_for_trigger",
                    "candidate_quality": 0.80,
                    "setup_quality_ok": True,
                    "has_monitorable_setup": True,
                    "has_tradeable_support": False,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "technical_opposes_side": False,
                    "fundamental_supports_side": True,
                    "fundamental_opposes_side": False,
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "current_confirmation_score": 0.78,
                    "independent_support_count": 2,
                    "action_value_stats": {
                        "sample_count": 5,
                        "reward_sum": 12000.0,
                        "mean_return_on_notional": 0.024,
                    },
                },
            },
            full_config={
                "position_budget_policy": {
                    "deployable_margin_ratio": 0.060,
                    "deployable_margin_max_ratio": 0.120,
                    "hard_max_total_margin_ratio": 0.20,
                }
            },
        )

        self.assertTrue(allowed)
        self.assertEqual(detail["authority_type"], "real_budget_entry")
        self.assertEqual(detail["capital_layer"], CAPITAL_LAYER_ALPHA_SCALE)
        self.assertTrue(detail["conditional_trigger_authority"])
        self.assertTrue(detail["requires_intraday_confirmation"])
        self.assertFalse(detail["can_execute_without_intraday_trigger"])

    def test_medium_horizon_fundamental_opposition_prevents_scale_but_not_probe(self):
        allowed, detail = _final_contract_authority(
            control_reasons=["alpha_setup_ev_fusion", "qualified_positive_expectancy"],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "candidate_quality": 0.75,
                    "has_tradeable_support": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": True,
                    "positive_action_value": True,
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "technical_opposes_side": False,
                    "fundamental_opposes_side": True,
                    "current_confirmation_score": 0.78,
                    "independent_support_count": 2,
                    "action_value_stats": {"sample_count": 5, "reward_sum": 12000.0},
                }
            },
        )

        self.assertTrue(allowed)
        self.assertEqual(detail["authority_type"], "exploration_probe")
        self.assertEqual(detail["capital_layer"], CAPITAL_LAYER_EXPLORATION)

    def test_rank_is_not_used_by_step4_to_upgrade_an_unlearned_probe(self):
        allowed, detail = _final_contract_authority(
            control_reasons=["alpha_setup_ev_fusion"],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "opportunity_rank": 1,
                    "rank_is_capital_priority": True,
                    "capital_priority_score": 0.86,
                    "capital_priority_tier": 3,
                    "has_tradeable_support": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "strong_realtime_evidence": True,
                    "strong_market_confirmation": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": True,
                    "technical_opposes_side": False,
                    "current_confirmation_score": 0.72,
                    "independent_support_count": 2,
                }
            },
            full_config={
                "portfolio_manager": {
                    "quality_aware_fusion": {
                        "opportunity_scorecard": {
                            "tradeable_threshold": 0.58,
                        }
                    }
                }
            },
        )

        self.assertTrue(allowed)
        self.assertEqual(detail["authority_type"], "exploration_probe")
        self.assertEqual(detail["capital_layer"], CAPITAL_LAYER_EXPLORATION)
        self.assertNotIn("rank_capital_priority_real_budget_release", detail)
        self.assertNotIn("rank_capital_priority_real_budget_release", detail["reason_codes"])

    def test_step4_probe_keeps_floor_and_preserves_existing_margin_range_for_step5(self):
        def authority_for_quality(quality):
            return _final_contract_authority(
                control_reasons=["alpha_setup_ev_fusion"],
                control_diagnostics={
                    "alpha_setup_ev_fusion": {
                        "scorecard_state": "tradeable_candidate",
                        "candidate_quality": quality,
                        "has_tradeable_support": True,
                        "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                        "positive_action_value": False,
                        "strong_realtime_evidence": True,
                        "strong_market_confirmation": True,
                        "technical_supports_side": True,
                        "technical_entry_timing_supports_side": True,
                        "technical_opposes_side": False,
                        "current_confirmation_score": 0.72,
                        "independent_support_count": 1,
                    }
                },
                full_config={
                    "position_budget_policy": {
                        "probe_margin_ratio": 0.008,
                        "probe_margin_max_ratio": 0.015,
                    }
                },
            )[1]

        low = authority_for_quality(0.0)
        high = authority_for_quality(1.0)
        self.assertEqual(low["capital_layer"], CAPITAL_LAYER_EXPLORATION)
        self.assertAlmostEqual(low["target_margin_ratio"], 0.008)
        self.assertAlmostEqual(high["target_margin_ratio"], 0.008)
        self.assertAlmostEqual(low["max_allowed_margin_ratio"], 0.015)
        self.assertAlmostEqual(high["max_allowed_margin_ratio"], 0.015)

    def test_rank_one_without_current_evidence_does_not_release_real_budget(self):
        allowed, detail = _final_contract_authority(
            control_reasons=["alpha_setup_ev_fusion"],
            control_diagnostics={
                "alpha_setup_ev_fusion": {
                    "scorecard_state": "tradeable_candidate",
                    "opportunity_rank": 1,
                    "rank_is_capital_priority": True,
                    "capital_priority_score": 0.90,
                    "capital_priority_tier": 3,
                    "has_tradeable_support": True,
                    "has_entry_invalidation": False,
                    "has_position_exit_boundary": True,
                    "qualified_positive_expectancy": False,
                    "positive_action_value": False,
                    "positive_profile": False,
                    "strong_realtime_evidence": False,
                    "strong_market_confirmation": True,
                    "technical_supports_side": True,
                    "technical_entry_timing_supports_side": False,
                    "technical_opposes_side": False,
                    "current_confirmation_score": 0.72,
                    "independent_support_count": 1,
                }
            },
        )

        self.assertFalse(allowed)
        self.assertNotEqual(detail["authority_type"], "real_budget_entry")
        self.assertNotIn("rank_capital_priority_real_budget_release", detail)

    def test_position_budget_uses_step4_layer_plan_before_lot_rounding(self):
        common = {
            "ticker": "ZZ",
            "target_lots": 1,
            "current_lots": 0,
            "current_price": 5000.0,
            "multiplier": 10.0,
            "margin_rate": 0.10,
            "account_equity": 5_000_000.0,
            "margin_available": 1_000_000.0,
            "max_net_exposure": 0.50,
            "current_net_exposure": 0.0,
            "current_ticker_exposure": 0.0,
            "full_config": {
                "position_budget_policy": {
                    "enabled": True,
                    "min_real_trade_margin_ratio": 0.008,
                    "min_real_trade_margin_abs": 40000,
                    "probe_margin_ratio": 0.008,
                    "probe_margin_max_ratio": 0.015,
                    "max_single_ticker_margin_ratio": 0.13,
                    "block_below_min_when_cannot_scale": True,
                }
            },
        }
        real_reasons, real_notes, real_diag = [], [], {}
        real_result = _apply_position_budget_policy_for_new_entry(
            **common,
            final_entry_authority={
                "requires_authority": True,
                "authority_type": "real_budget_entry",
                "strong_current_evidence": True,
                "decision": "allow_real_new_entry",
                "capital_layer": CAPITAL_LAYER_REAL_BUDGET,
                "capital_ratio_source": "normal_trade_margin_ratio",
                "candidate_quality": 0.50,
                "target_margin_ratio": 0.045,
                "max_allowed_margin_ratio": 0.060,
            },
            control_reasons=real_reasons,
            control_notes=real_notes,
            control_diagnostics=real_diag,
        )
        probe_reasons, probe_notes, probe_diag = [], [], {}
        probe_result = _apply_position_budget_policy_for_new_entry(
            **common,
            final_entry_authority={
                "requires_authority": True,
                "authority_type": "exploration_probe",
                "strong_current_evidence": False,
                "max_allowed_margin_ratio": 0.015,
                "decision": "allow_exploration_probe",
                "capital_layer": CAPITAL_LAYER_EXPLORATION,
                "capital_ratio_source": "probe_margin_ratio",
                "candidate_quality": 0.50,
                "target_margin_ratio": 0.0115,
            },
            control_reasons=probe_reasons,
            control_notes=probe_notes,
            control_diagnostics=probe_diag,
        )

        self.assertEqual(abs(real_result[0]), 45)
        self.assertEqual(abs(probe_result[0]), 12)
        self.assertLessEqual(probe_result[2], 5_000_000.0 * 0.015)
        self.assertIn("minimum_real_trade_margin_floor_applied", real_reasons)
        self.assertIn("exploration_probe_probe_floor_applied", probe_reasons)
        self.assertEqual(
            probe_diag["position_budget_policy"]["decision"],
            "exploration_probe_probe_floor_applied",
        )
        self.assertAlmostEqual(
            probe_diag["position_budget_policy"]["planned_margin_ratio"],
            0.0115,
        )

    def test_add_sizing_never_turns_an_incremental_candidate_into_a_reduce(self):
        reasons, notes, diagnostics = [], [], {}
        result = _apply_position_budget_policy_for_new_entry(
            ticker="ZZ",
            target_lots=12,
            current_lots=10,
            current_price=5000.0,
            multiplier=10.0,
            margin_rate=0.10,
            account_equity=1_000_000.0,
            margin_available=0.0,
            max_net_exposure=0.50,
            current_net_exposure=0.50,
            current_ticker_exposure=0.50,
            final_entry_authority={
                "requires_authority": True,
                "authority_type": "real_budget_entry",
                "capital_layer": CAPITAL_LAYER_ALPHA_SCALE,
                "capital_ratio_source": "deployable_margin_ratio",
                "target_margin_ratio": 0.06,
                "max_allowed_margin_ratio": 0.12,
            },
            full_config={
                "position_budget_policy": {
                    "enabled": True,
                    "min_real_trade_margin_ratio": 0.008,
                    "min_real_trade_margin_abs": 0.0,
                    "max_single_ticker_margin_ratio": 0.13,
                    "block_below_min_when_cannot_scale": True,
                }
            },
            control_reasons=reasons,
            control_notes=notes,
            control_diagnostics=diagnostics,
        )

        self.assertEqual(result[0], 10)
        self.assertGreaterEqual(abs(result[0]), 10)
        self.assertEqual(result[-1], "step4_add_plan_no_incremental_capacity")

    def test_action_value_trace_separates_open_hold_exit_execution(self):
        trace = _alpha_setup_action_value_trace([
            {"action_name": "open", "action_preference": "controlled_open_or_add"},
            {"action_name": "hold_position", "action_preference": "controlled_probe_or_hold"},
            {"action_name": "exit", "action_preference": "cap_reduce_or_revalidate"},
            {"action_name": "execution_trigger", "action_preference": "observe_or_probe"},
        ])

        self.assertEqual(len(trace["open_action_value"]), 1)
        self.assertEqual(len(trace["hold_action_value"]), 1)
        self.assertEqual(len(trace["exit_action_value"]), 1)
        self.assertEqual(len(trace["execution_action_value"]), 1)
        self.assertIn("open_hold_exit_execution", trace["action_value_contract"])

    def test_phase1_recommendation_exports_analyst_contract_and_final_authority(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="ZZ",
            action=FuturesAction.OPEN_LONG,
            lots=2,
            price=100.0,
            settle_price=100.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="ZZ2505",
            justification="test",
        )
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.70,
            opportunity_state="tradeable_candidate",
            invalidation_level=98.0,
            entry_quality="acceptable",
            trigger_valid=True,
            invalidation_present=True,
            entry_trigger="breakout above opening range with volume confirmation",
            trend_direction="bullish",
            entry_timing_signal="breakout",
            business_quality_score=0.80,
            data_coverage_score=0.90,
            holding_period_hint="1-3 trading days while breakout remains valid",
            justification="Technical breakout setup with volume confirmation and explicit invalidation.",
            metadata={
                "technical_context": {
                    "dominant_direction": "bullish",
                    "action_evidence_contract": {"setup_family": "trend_breakout"},
                }
            },
        )
        signal = apply_trade_research_contract(
            signal,
            {
                "tradeability": "high",
                "sector": "generic",
                "dominant_direction": "bullish",
                "current_trigger_confirmed": True,
                "action_evidence_contract": {
                    "setup_family": "trend_breakout",
                    "entry_trigger": "current breakout above opening range is confirmed",
                    "current_trigger_confirmed": True,
                    "trigger_valid": True,
                    "invalidation_present": True,
                },
                "market_regime": "trend",
            },
            analyst="technical",
            trading_date="2025-03-03",
            ticker="ZZ",
        )
        recommendation = _build_signed_pm_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="ZZ",
            trading_date="2025-03-03",
            contract_code="ZZ2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=100.0,
                base_price_source=None,
                base_price_date="2025-03-03",
                open_price=100.0,
                prev_close_price=99.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "strategy_controls": {
                    "diagnostics": {
                        "position_budget_policy": {"decision": "minimum_margin_floor_applied"},
                    }
                }
            },
            pm_state_update=_pm_state_fixture({
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "ZZ",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": 2,
                "lots_delta": 2,
                "lots_delta_abs": 2,
                "target_position_ratio": 0.02,
                "authority_type": "real_budget_entry",
                "authority_decision": "allow_real_new_entry",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "max_allowed_margin_ratio": 0.12,
                "reason_codes": ["qualified_positive_expectancy"],
                "analyst_prior_audit": {
                    "semantic_role": "cold_start_prior_only",
                    "can_create_trade_authority": False,
                    "can_open_position_directly": False,
                },
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            }, ticker="ZZ"),
            full_config={},
        )
        snapshot = recommendation.signal_snapshot

        self.assertNotIn("technical", snapshot)
        self.assertIn("signal_collection_contract", snapshot)
        self.assertNotIn("pm_raw_rationale", snapshot)
        self.assertNotIn("pm_justification_contract", snapshot)
        self.assertIn("PM final structured outlet", recommendation.justification)
        self.assertIn("authority_type=real_budget_entry", recommendation.justification)
        self.assertNotIn("analyst_prior_audit", snapshot)
        self.assertNotIn("position_budget_policy", snapshot)

    def test_phase1_semantic_no_trade_text_blocks_open_recommendation(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="RB",
            action=FuturesAction.OPEN_SHORT,
            lots=8,
            price=3500.0,
            settle_price=3500.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="rb2505",
            justification=(
                "No position warranted for RB. Technical signal is neutral and "
                "no timing trigger exists; keep this as watchlist only."
            ),
        )
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.NEUTRAL,
            confidence=0.50,
            opportunity_state="watch_for_trigger",
            neutral_trigger_condition="Breakout above range high with volume confirmation",
            counterfactual_side="short",
            neutral_watchlist_priority="medium",
        )
        recommendation = _build_signed_pm_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="RB",
            trading_date="2025-03-04",
            contract_code="rb2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=3500.0,
                base_price_source=None,
                base_price_date="2025-03-04",
                open_price=3500.0,
                prev_close_price=3480.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "lots_delta_abs": 8,
                "reason_codes": "target_plan",
                "strategy_controls": {
                    "diagnostics": {},
                    "reasons": ["test_probe"],
                },
            },
            pm_state_update=_pm_state_fixture({
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "RB",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": -8,
                "lots_delta": -8,
                "lots_delta_abs": 8,
                "target_position_ratio": -0.02,
                "authority_type": "exploration_probe",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "max_allowed_margin_ratio": 0.015,
                "reason_codes": ["test_probe"],
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            }, ticker="RB"),
            full_config={},
        )
        snapshot = recommendation.signal_snapshot

        self.assertEqual(recommendation.action, RecommendationAction.HOLD)
        self.assertEqual(recommendation.lots, 0)
        self.assertNotIn("pm_raw_rationale", snapshot)
        self.assertNotIn("No position warranted", recommendation.justification)
        self.assertIn("PM final structured outlet", recommendation.justification)
        self.assertIn("authority_type=watchlist_only", recommendation.justification)
        blocked_contract = snapshot["final_action_contract"]
        self.assertEqual(blocked_contract["authority_type"], "watchlist_only")
        self.assertIn("final_contract_authority_probe_lacks_current_evidence", blocked_contract["reason_codes"])
        self.assertNotIn("pm_internal_draft", snapshot)
        self.assertEqual(blocked_contract["target_lots"], 0)
        self.assertEqual(blocked_contract["lots_delta_abs"], 0)
        self.assertEqual(blocked_contract["final_action"], "wait")
        self.assertNotIn("active_opportunity_audit", snapshot)

    def test_pm_requires_signal_collection_contract_from_signal_collector(self):
        portfolio = Portfolio(
            id="portfolio-1",
            cashflow=1_000_000,
            account_equity=1_000_000,
            cash_available=1_000_000,
            positions={},
            margin_used=0.0,
            margin_available=1_000_000,
            margin_ratio=0.0,
        )
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.5),
            AnalystSignal(agent_name="fundamental", signal=Signal.BEARISH, confidence=0.6),
            AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.4),
        ]
        state = {
            "portfolio": portfolio,
            "ticker": "J",
            "trading_date": datetime(2025, 5, 9),
            "analyst_signals": signals,
            "llm_config": {"provider": "fake", "model": "fake"},
            "num_tickers": 15,
            "enabled_analysts": ["technical", "fundamental", "commodity_news"],
            "config_id": "cfg",
            "phase": "phase1",
            "morning_price_context": SimpleNamespace(
                base_price=500.0,
                base_price_source="t_minus_1_close_fallback",
                base_price_date="2025-05-08",
                open_price=None,
                prev_close_price=1478.0,
                warning_message=None,
            ),
            "config": {"max_total_margin_ratio": 0.20, "max_single_margin_ratio": 0.12},
            "full_config": {"learning": {"enabled": False}},
            "router": None,
        }

        with self.assertRaisesRegex(RuntimeError, "pm_missing_signal_collection_contract_from_signal_collector"):
            portfolio_agent_futures(state)

        state["signal_collection_contract"] = {
            "contract_version": "agentquant.signal_collection.v1",
            "source_agent": "portfolio_manager",
            "collector_decision_boundary": "no_trade_authority",
            "ticker": "J",
            "trading_date": "2025-05-09",
        }
        with self.assertRaisesRegex(RuntimeError, "pm_invalid_signal_collection_contract_source_agent"):
            portfolio_agent_futures(state)

    def test_phase1_semantic_gate_does_not_block_triggered_exploration_probe(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="BU",
            action=FuturesAction.OPEN_SHORT,
            lots=2,
            price=3100.0,
            settle_price=3100.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="bu2505",
            justification=(
                "Controlled exploration probe: technical breakdown trigger is valid, "
                "price is below VWAP, invalidation is above the morning range."
            ),
        )
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.70,
            opportunity_state="tradeable_candidate",
            entry_trigger="Breakdown below morning range with volume confirmation",
            invalidation_level=3150.0,
            trigger_valid=True,
            invalidation_present=True,
        )
        recommendation = _build_signed_pm_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="BU",
            trading_date="2025-03-04",
            contract_code="bu2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=3100.0,
                base_price_source=None,
                base_price_date="2025-03-04",
                open_price=3100.0,
                prev_close_price=3120.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "strategy_controls": {
                    "diagnostics": {}
                }
            },
            pm_state_update=_pm_state_fixture({
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "BU",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": -2,
                "lots_delta": -2,
                "lots_delta_abs": 2,
                "target_position_ratio": -0.01,
                "authority_type": "exploration_probe",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "max_allowed_margin_ratio": 0.015,
                "reason_codes": ["test_probe"],
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            }, ticker="BU"),
            full_config={},
        )

        self.assertEqual(recommendation.action, RecommendationAction.OPEN_SHORT)
        self.assertEqual(recommendation.lots, 2)
        self.assertNotIn("pm_raw_rationale", recommendation.signal_snapshot)
        self.assertNotIn("Controlled exploration probe", recommendation.justification)
        self.assertIn("authority_type=exploration_probe", recommendation.justification)
        self.assertNotIn("pm_semantic_consistency_gate", recommendation.signal_snapshot)
        self.assertEqual(recommendation.signal_snapshot["final_action_contract"]["final_action"], "open_probe")

    def test_phase1_structured_authority_blocks_bare_exploration_probe_without_current_evidence(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="SR",
            action=FuturesAction.OPEN_LONG,
            lots=8,
            price=5900.0,
            settle_price=5900.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="sr2505",
            justification=(
                "Raw rationale may discuss a possible probe, but final authority "
                "does not contain current entry evidence."
            ),
        )
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.60,
            opportunity_state="watch_for_trigger",
            entry_trigger="requires technical confirmation",
            invalidation_level=5800.0,
        )
        recommendation = _build_signed_pm_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="SR",
            trading_date="2025-03-07",
            contract_code="sr2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=5900.0,
                base_price_source=None,
                base_price_date="2025-03-07",
                open_price=5900.0,
                prev_close_price=5880.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "lots_delta_abs": 8,
                "reason_codes": "target_plan",
                "strategy_controls": {
                    "diagnostics": {},
                    "reasons": ["alpha_setup_open_action_value_missing"],
                },
            },
            pm_state_update=_pm_state_fixture({
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "SR",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": 8,
                "lots_delta": 8,
                "lots_delta_abs": 8,
                "target_position_ratio": 0.02,
                "authority_type": "exploration_probe",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "max_allowed_margin_ratio": 0.015,
                "reason_codes": ["alpha_setup_open_action_value_missing"],
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            }, ticker="SR"),
            full_config={},
        )

        self.assertEqual(recommendation.action, RecommendationAction.HOLD)
        self.assertEqual(recommendation.lots, 0)
        authority = recommendation.signal_snapshot["final_action_contract"]
        self.assertEqual(authority["authority_type"], "watchlist_only")
        self.assertIn("final_contract_authority_probe_lacks_current_evidence", authority["reason_codes"])

    def test_phase1_structured_authority_ignores_opposite_side_trigger_evidence(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="BU",
            action=FuturesAction.OPEN_SHORT,
            lots=2,
            price=3100.0,
            settle_price=3100.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="bu2505",
            justification="Raw rationale contains a probe idea, but analyst trigger is opposite side.",
        )
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.75,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3050.0,
            trigger_valid=True,
            invalidation_present=True,
            evidence_role="entry_timing",
            entry_quality="strong",
        )
        recommendation = _build_signed_pm_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="BU",
            trading_date="2025-03-05",
            contract_code="bu2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=3100.0,
                base_price_source=None,
                base_price_date="2025-03-05",
                open_price=3100.0,
                prev_close_price=3120.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "strategy_controls": {
                    "diagnostics": {}
                }
            },
            pm_state_update=_pm_state_fixture({
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "BU",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": -2,
                "lots_delta": -2,
                "lots_delta_abs": 2,
                "target_position_ratio": -0.01,
                "authority_type": "exploration_probe",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "watch_for_trigger_block": False,
                "max_allowed_margin_ratio": 0.015,
                "reason_codes": ["unknown_alpha_probe"],
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            }, ticker="BU"),
            full_config={},
        )

        self.assertEqual(recommendation.action, RecommendationAction.HOLD)
        self.assertEqual(recommendation.lots, 0)
        authority = recommendation.signal_snapshot["final_action_contract"]
        self.assertEqual(authority["authority_type"], "watchlist_only")
        self.assertIn("final_contract_authority_probe_lacks_current_evidence", authority["reason_codes"])

    def test_strong_real_budget_entry_passes_phase1_and_phase2(self):
        portfolio = SimpleNamespace(id="pf1")
        decision = FuturesDecision(
            ticker="RB",
            action=FuturesAction.OPEN_LONG,
            lots=6,
            price=3500.0,
            settle_price=3500.0,
            margin_rate=0.1,
            contract_multiplier=10.0,
            contract_code="rb2505",
            justification="structured real budget entry",
        )
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.76,
            opportunity_state="tradeable_candidate",
            entry_trigger="breakout above opening range with volume confirmation",
            invalidation_level=3450.0,
            trigger_valid=True,
            invalidation_present=True,
            evidence_role="entry_timing",
            entry_quality="strong",
        )
        recommendation = _build_signed_pm_recommendation(
            config_id="cfg",
            portfolio=portfolio,
            ticker="RB",
            trading_date="2025-03-06",
            contract_code="rb2505",
            decision=decision,
            morning_price_context=SimpleNamespace(
                base_price=3500.0,
                base_price_source=None,
                base_price_date="2025-03-06",
                open_price=3500.0,
                prev_close_price=3480.0,
                warning_message=None,
            ),
            analyst_signals=[signal],
            plan_snapshot={
                "target_position_ratio": 0.05,
                "target_lots": 6,
                "lots_delta_abs": 6,
                "reason_codes": "tradable",
                "strategy_controls": {
                    "diagnostics": {},
                    "reasons": ["qualified_positive_expectancy"],
                },
                "opportunity_scorecard": {
                    "preferred_side": "long",
                    "long": {"final_state": "tradeable_candidate", "score": 0.70},
                },
            },
            pm_state_update=_pm_state_fixture({
                "contract_version": "agentquant.final_action.v1",
                "ticker": "RB",
                "final_action": "open_real",
                "contract_type": "strategy",
                "current_lots": 0,
                "target_lots": 6,
                "lots_delta": 6,
                "lots_delta_abs": 6,
                "reason_codes": "tradable",
                "target_position_ratio": 0.05,
                "authority_type": "real_budget_entry",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "reason_codes": ["qualified_positive_expectancy"],
                "execution_requirement": "intraday_trigger_required",
                "consistency": {"status": "ok"},
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            }, ticker="RB"),
            full_config={},
        )

        self.assertEqual(recommendation.action, RecommendationAction.OPEN_LONG)
        self.assertEqual(recommendation.lots, 6)
        snapshot = dict(recommendation.signal_snapshot)
        decision2 = _translate_pre_open_recommendation_to_order(
            recommendation={
                "underlying_code": "RB",
                "contract_code": "rb2505",
                "source_type": RecommendationSourceType.STRATEGY.value,
                "action": recommendation.action.value,
                "lots": recommendation.lots,
                "signal_snapshot": recommendation.signal_snapshot,
            },
            portfolio=Portfolio(id="p1", cashflow=5000000.0, margin_used=0.0, positions={}),
            config={
                "cashflow": 5000000,
                "max_total_margin_ratio": 0.20,
                "risk_control": {
                    "warning_ratio": 0.70,
                    "danger_ratio": 0.50,
                    "emergency_ratio": 0.30,
                    "max_single_position_ratio": {"safe": 0.12},
                },
            },
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision2.action, FuturesAction.OPEN_LONG)
        self.assertEqual(decision2.lots, 6)
        self.assertTrue(snapshot["phase2_execution"]["pm_plan_validation"]["passed"])

    def test_intraday_execution_audit_has_learning_fields(self):
        selection = select_intraday_execution(
            signal_bars=[],
            execution_bars=[],
            action=RecommendationAction.OPEN_LONG.value,
            config={},
            finalize_untriggered=True,
        )
        audit = selection.to_audit_payload()

        self.assertIn("trigger_checked", audit)
        self.assertIn("trigger_passed", audit)
        self.assertIn("price_chase_check", audit)
        self.assertIn("execution_failure_reason", audit)
        self.assertIn("missed_opportunity_flag", audit)
        self.assertTrue(audit["missed_opportunity_flag"])


class HoldingLifecycleRegressionTest(unittest.TestCase):
    def test_held_position_uses_opening_fac_identity_and_current_exit_boundary(self):
        opening_context = {
            "setup_type": "trend_breakout_setup",
            "horizon_class": "medium",
            "expected_horizon_days": 7,
            "market_regime": "trend",
        }
        identity = _formal_learning_identity_for_side(
            side="short",
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BEARISH,
                    confidence=0.80,
                    horizon_class="short",
                    metadata={
                        "action_evidence_contract": {
                            "setup_type": "volatility_breakout_setup",
                            "side": "short",
                        }
                    },
                )
            ],
            current_lots=-3,
            opening_fac_context=opening_context,
        )
        self.assertEqual(identity["setup_type"], "trend_breakout_setup")
        self.assertEqual(identity["horizon_class"], "medium")
        self.assertEqual(identity["expected_horizon_days"], 7)
        self.assertEqual(identity["market_regime"], "trend")

        scope = _final_contract_scope_from_scc(
            signal_collection_contract={
                "source_contracts": [
                    {
                        "analyst": "technical",
                        "action_evidence_contract": {
                            "side": "short",
                            "confidence": 0.80,
                            "setup_type": "volatility_breakout_setup",
                            "horizon_class": "short",
                            "expected_horizon_days": 3,
                            "market_regime": "volatile",
                            "position_invalidation_level": 105.0,
                            "atr_stop_distance": 2.0,
                        },
                    }
                ]
            },
            current_lots=-3,
            target_lots=-2,
            final_action="reduce",
            opening_fac_context=opening_context,
        )
        self.assertEqual(scope["setup_type"], "trend_breakout_setup")
        self.assertEqual(scope["horizon_class"], "medium")
        self.assertEqual(scope["expected_horizon_days"], 7)
        self.assertEqual(scope["market_regime"], "trend")
        self.assertEqual(scope["position_invalidation_level"], 105.0)

    def test_current_position_traces_opening_fac_by_transaction_recommendation_id(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE futures_transactions (id TEXT, config_id TEXT, ticker TEXT, "
                "trading_date TEXT, action TEXT, lots INTEGER, source_type TEXT, "
                "recommendation_id TEXT, execution_price REAL, price REAL, "
                "contract_multiplier REAL, created_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE futures_recommendation (id TEXT, signal_snapshot TEXT, "
                "signal_snapshot_artifact_path TEXT, signal_snapshot_sha256 TEXT)"
            )
            conn.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT)")
            conn.execute(
                "CREATE TABLE daily_settlement (portfolio_id TEXT, trading_date TEXT)"
            )
            conn.execute(
                "CREATE TABLE ticker_daily_pnl (portfolio_id TEXT, trading_date TEXT, "
                "ticker TEXT, daily_pnl REAL, commission REAL, settle_price REAL)"
            )
            snapshot = json.dumps(
                {
                    "final_action_contract": {
                        "final_action": "open_probe",
                        "expected_horizon_days": 3,
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "invalidation_level": 95.0,
                        "position_invalidation_level": 93.0,
                        "invalidation": "15m close below 95",
                        "atr_stop_distance": 2.0,
                        "setup_type": "technical_pullback",
                    }
                }
            )
            conn.execute(
                "INSERT INTO futures_recommendation VALUES (?, ?, NULL, NULL)",
                ("open-rec", snapshot),
            )
            conn.execute(
                "INSERT INTO futures_transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "tx-open", "cfg", "ZZ", "2025-03-20", "open_long", 2,
                    "strategy", "open-rec", 100.0, 100.0, 10.0,
                    "2025-03-20 09:31:00",
                ),
            )
            conn.execute("INSERT INTO portfolio VALUES (?, ?)", ("pf", "cfg"))
            conn.executemany(
                "INSERT INTO daily_settlement VALUES (?, ?)",
                [("pf", "2025-03-20"), ("pf", "2025-03-21")],
            )
            conn.executemany(
                "INSERT INTO ticker_daily_pnl VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("pf", "2025-03-20", "ZZ", 0.0, 20.0, 100.0),
                    ("pf", "2025-03-21", "ZZ", 600.0, 0.0, 103.0),
                    ("pf", "2025-03-24", "ZZ", 9999.0, 0.0, 150.0),
                ],
            )
            conn.commit()
            conn.close()

            class _DB:
                def _get_connection(self):
                    connection = sqlite3.connect(path)
                    connection.row_factory = sqlite3.Row
                    return connection

                def _deserialize_external_json(self, row, field):
                    return json.loads(row.get(field) or "{}")

            context = _load_opening_fac_context(
                db=_DB(),
                config_id="cfg",
                ticker="ZZ",
                trading_date="2025-03-24",
                current_lots=2,
                current_position=SimpleNamespace(
                    unrealized_pnl=-300.0,
                    margin_used=10000.0,
                ),
            )

            self.assertEqual(context["recommendation_id"], "open-rec")
            self.assertEqual(context["held_trading_days"], 2)
            self.assertEqual(context["expected_horizon_days"], 3)
            self.assertEqual(context["horizon_class"], "short")
            self.assertEqual(context["market_regime"], "trend")
            self.assertEqual(context["position_invalidation_level"], 93.0)
            self.assertNotIn("invalidation_level", context)
            self.assertEqual(context["atr_stop_distance"], 2.0)
            self.assertEqual(context["opening_execution_price"], 100.0)
            self.assertEqual(context["best_prior_settlement_price"], 103.0)
            self.assertEqual(context["settled_cycle_net_pnl"], 580.0)
            self.assertEqual(context["cycle_net_pnl"], 280.0)
            self.assertEqual(context["cycle_peak_net_pnl"], 580.0)
            self.assertEqual(context["cycle_profit_drawdown"], 300.0)
            self.assertEqual(context["cycle_open_notional"], 2000.0)
            self.assertAlmostEqual(context["cycle_return_on_notional"], 0.14)
            self.assertAlmostEqual(context["cycle_peak_return_on_notional"], 0.29)
            self.assertAlmostEqual(
                context["cycle_profit_drawdown_on_notional"],
                0.15,
            )
            self.assertAlmostEqual(context["cycle_pnl_ratio"], 0.14)
            self.assertAlmostEqual(context["cycle_margin_return_ratio"], 0.028)
            self.assertAlmostEqual(
                _position_pnl_ratio(
                    SimpleNamespace(unrealized_pnl=-300.0, margin_used=10000.0),
                ),
                -0.03,
            )
            self.assertAlmostEqual(
                _position_pnl_ratio(
                    SimpleNamespace(unrealized_pnl=-300.0, margin_used=10000.0),
                    context,
                ),
                0.14,
            )

            atr_only_context = dict(context)
            atr_only_context["position_invalidation_level"] = 0.0
            self.assertTrue(
                _opening_fac_position_invalidation_breached(
                    opening_fac_context=atr_only_context,
                    ticker="ZZ",
                    current_side="long",
                    current_price=96.0,
                    current_position=SimpleNamespace(entry_price=100.0),
                    full_config={},
                )
            )

            structure_not_breached = dict(context)
            structure_not_breached["position_invalidation_level"] = 90.0
            self.assertTrue(
                _opening_fac_position_invalidation_breached(
                    opening_fac_context=structure_not_breached,
                    ticker="ZZ",
                    current_side="long",
                    current_price=96.0,
                    current_position=SimpleNamespace(entry_price=100.0),
                    full_config={},
                )
            )

            invalid_structure = dict(context)
            invalid_structure["position_invalidation_level"] = 105.0
            invalid_structure["best_prior_settlement_price"] = 0.0
            self.assertTrue(
                _opening_fac_position_invalidation_breached(
                    opening_fac_context=invalid_structure,
                    ticker="ZZ",
                    current_side="long",
                    current_price=96.0,
                    current_position=SimpleNamespace(entry_price=100.0),
                    full_config={},
                )
            )
            self.assertFalse(
                _opening_fac_position_invalidation_breached(
                    opening_fac_context=invalid_structure,
                    ticker="ZZ",
                    current_side="long",
                    current_price=99.0,
                    current_position=SimpleNamespace(entry_price=100.0),
                    full_config={},
                )
            )

            actual_fill_context = dict(context)
            actual_fill_context["position_invalidation_level"] = 0.0
            self.assertFalse(
                _opening_fac_position_invalidation_breached(
                    opening_fac_context=actual_fill_context,
                    ticker="ZZ",
                    current_side="long",
                    current_price=150.0,
                    current_position=SimpleNamespace(entry_price=200.0),
                    full_config={},
                )
            )

            missing_fill_context = dict(context)
            missing_fill_context["opening_execution_price"] = 0.0
            self.assertFalse(
                _opening_fac_position_invalidation_breached(
                    opening_fac_context=missing_fill_context,
                    ticker="ZZ",
                    current_side="long",
                    current_price=90.0,
                    current_position=SimpleNamespace(entry_price=100.0),
                    full_config={},
                )
            )

            conn = sqlite3.connect(path)
            conn.execute(
                "UPDATE futures_transactions SET execution_price = NULL, price = NULL"
            )
            conn.commit()
            conn.close()
            with self.assertRaisesRegex(
                RuntimeError,
                "pm_opening_fac_execution_price_missing",
            ):
                _load_opening_fac_context(
                    db=_DB(),
                    config_id="cfg",
                    ticker="ZZ",
                    trading_date="2025-03-24",
                    current_lots=2,
                )

            conn = sqlite3.connect(path)
            conn.execute("DELETE FROM futures_transactions")
            conn.commit()
            conn.close()
            with self.assertRaisesRegex(RuntimeError, "pm_opening_fac_lineage_missing"):
                _load_opening_fac_context(
                    db=_DB(),
                    config_id="cfg",
                    ticker="ZZ",
                    trading_date="2025-03-24",
                    current_lots=2,
                )
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_opening_fac_lineage_continues_full_rollover_and_resets_after_close_only(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE futures_transactions (id TEXT, config_id TEXT, ticker TEXT, "
                "trading_date TEXT, action TEXT, lots INTEGER, source_type TEXT, "
                "recommendation_id TEXT, execution_price REAL, price REAL, created_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE futures_recommendation (id TEXT, signal_snapshot TEXT, "
                "signal_snapshot_artifact_path TEXT, signal_snapshot_sha256 TEXT)"
            )
            conn.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT)")
            conn.execute("CREATE TABLE daily_settlement (portfolio_id TEXT, trading_date TEXT)")
            conn.execute(
                "CREATE TABLE ticker_daily_pnl (portfolio_id TEXT, trading_date TEXT, "
                "ticker TEXT, settle_price REAL)"
            )

            def snapshot(setup_type, invalidation):
                return json.dumps({
                    "final_action_contract": {
                        "final_action": "open_probe",
                        "expected_horizon_days": 5,
                        "horizon_class": "medium",
                        "market_regime": "trend",
                        "position_invalidation_level": invalidation,
                        "atr_stop_distance": 10.0,
                        "setup_type": setup_type,
                    }
                })

            conn.executemany(
                "INSERT INTO futures_recommendation VALUES (?, ?, NULL, NULL)",
                [
                    ("old-open", snapshot("old_setup", 110.0)),
                    ("new-open", snapshot("new_setup", 95.0)),
                ],
            )
            conn.executemany(
                "INSERT INTO futures_transactions VALUES (?, 'cfg', 'ZZ', ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("old-open-tx", "2025-03-03", "open_short", 2, "strategy", "old-open", 100.0, 100.0, "2025-03-03T09:31:00"),
                    ("z-roll-close", "2025-03-10", "close_short", 2, "rollover", "roll-full", 98.0, 98.0, "2025-03-10T09:31:00"),
                    ("a-roll-open", "2025-03-10", "open_short", 3, "rollover", "roll-full", 97.0, 97.0, "2025-03-10T09:31:00"),
                    ("close-only", "2025-03-12", "close_short", 3, "rollover", "roll-close-only", 96.0, 96.0, "2025-03-12T09:31:00"),
                    ("new-open-tx", "2025-03-13", "open_short", 2, "strategy", "new-open", 94.0, 94.0, "2025-03-13T09:31:00"),
                ],
            )
            conn.execute("INSERT INTO portfolio VALUES ('pf', 'cfg')")
            conn.executemany(
                "INSERT INTO daily_settlement VALUES ('pf', ?)",
                [(day,) for day in ("2025-03-03", "2025-03-04", "2025-03-05", "2025-03-06", "2025-03-07", "2025-03-10", "2025-03-11", "2025-03-12", "2025-03-13")],
            )
            conn.commit()
            conn.close()

            class _DB:
                def _get_connection(self):
                    connection = sqlite3.connect(path)
                    connection.row_factory = sqlite3.Row
                    return connection

                def _deserialize_external_json(self, row, field):
                    return json.loads(row.get(field) or "{}")

            continued = _load_opening_fac_context(
                db=_DB(), config_id="cfg", ticker="ZZ", trading_date="2025-03-12", current_lots=-3,
            )
            self.assertEqual(continued["recommendation_id"], "old-open")
            self.assertEqual(continued["setup_type"], "old_setup")

            reset = _load_opening_fac_context(
                db=_DB(), config_id="cfg", ticker="ZZ", trading_date="2025-03-14", current_lots=-2,
            )
            self.assertEqual(reset["recommendation_id"], "new-open")
            self.assertEqual(reset["setup_type"], "new_setup")
            self.assertEqual(reset["held_trading_days"], 1)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_new_loss_revalidation_bypasses_cooling_period_deferral(self):
        self.assertTrue(
            _is_lifecycle_exit_required_reason(["new_position_loss_revalidation_failed"])
        )
        self.assertFalse(_is_lifecycle_exit_required_reason(["cooling_period"]))

    def test_complete_cycle_profit_giveback_requires_current_revalidation(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-500.0,
        )
        ratio, reasons, _notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-06",
            position_ratio=0.10,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.20, "confidence": 0.40},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.35},
            full_config={},
            fusion_context={},
            risk_level=RiskLevel.SAFE,
            opening_fac_context={
                "recommendation_id": "open-profit-giveback",
                "held_trading_days": 3,
                "expected_horizon_days": 5,
                "cycle_return_on_notional": -0.001,
                "cycle_peak_return_on_notional": 0.025,
                "position_invalidation_level": 90.0,
                "opening_execution_price": 100.0,
            },
            current_price=99.0,
        )

        self.assertAlmostEqual(ratio, 0.05)
        self.assertIn("profit_giveback_revalidation_failed", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertTrue(detail["profit_giveback_revalidation_due"])
        self.assertTrue(detail["profit_giveback_revalidation_failed"])
        self.assertEqual(
            detail["decision"],
            "reduce_failed_profit_giveback_revalidation",
        )

    def test_new_losing_position_without_same_day_revalidation_exits_at_two_percent(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-5000.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.08,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.20, "confidence": 0.40},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.35},
            full_config={},
            fusion_context={},
            risk_level=RiskLevel.SAFE,
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("new_position_loss_revalidation_failed", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertEqual(detail["decision"], "exit_failed_new_loss_revalidation")
        self.assertFalse(detail["loss_revalidated"])

    def test_opening_fac_numeric_invalidation_can_release_the_position(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-5000.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.08,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.20, "confidence": 0.40},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.35},
            full_config={},
            fusion_context={},
            risk_level=RiskLevel.SAFE,
            opening_fac_context={
                "recommendation_id": "open-fac-1",
                "held_trading_days": 1,
                "expected_horizon_days": 3,
                "position_invalidation_level": 95.0,
                "opening_execution_price": 100.0,
            },
            current_price=94.0,
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("position_lifecycle_failed", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertTrue(detail["opening_position_invalidation_breached"])
        self.assertEqual(detail["opening_recommendation_id"], "open-fac-1")

    def test_entry_invalidation_level_is_not_reused_as_position_exit(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-400.0,
        )
        ratio, reasons, _notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.10,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.20, "confidence": 0.40},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.35},
            full_config={},
            fusion_context={},
            risk_level=RiskLevel.SAFE,
            opening_fac_context={
                "recommendation_id": "open-fac-entry-only",
                "held_trading_days": 1,
                "expected_horizon_days": 3,
                "invalidation_level": 95.0,
                "position_invalidation_level": 90.0,
            },
            current_price=94.0,
        )

        self.assertEqual(ratio, 0.10)
        self.assertNotIn("position_lifecycle_failed", reasons)
        self.assertFalse(
            diagnostics["holding_rebalance_control"][
                "opening_position_invalidation_breached"
            ]
        )

    def test_new_losing_position_without_new_trigger_exits_at_two_percent(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-2500.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.12,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.20, "confidence": 0.40},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.48},
            full_config={},
            fusion_context={},
            risk_level=RiskLevel.SAFE,
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("new_position_loss_revalidation_failed", reasons)
        self.assertEqual(
            diagnostics["holding_rebalance_control"]["decision"],
            "exit_failed_new_loss_revalidation",
        )

    def test_unconfirmed_probe_without_lifecycle_break_is_not_reduced(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-500.0,
        )
        ratio, reasons, _notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-10",
            position_ratio=0.05,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.20, "confidence": 0.40},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.35},
            full_config={},
            fusion_context={
                "opportunity_scorecard": {
                    "long": {"final_state": "watch_for_trigger", "gating_failures": []}
                }
            },
            risk_level=RiskLevel.SAFE,
            opening_fac_context={"held_trading_days": 3, "expected_horizon_days": 5},
        )

        self.assertEqual(ratio, 0.10)
        self.assertNotIn("exploration_probe_reconfirm_reduce", reasons)
        self.assertEqual(
            diagnostics["holding_rebalance_control"]["decision"],
            "keep_current_without_lifecycle_break",
        )

    def test_opposite_raw_target_cannot_skip_current_technical_exit(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=500.0,
        )
        ratio, reasons, _notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-05",
            position_ratio=-0.08,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.BEARISH, confidence=0.75),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.20, "confidence": 0.35},
            short_scores={"score": 0.70, "confidence": 0.70},
            market_confirmation={"confirmation_score": 0.72},
            full_config={},
            fusion_context={},
            risk_level=RiskLevel.SAFE,
            opening_fac_context={"held_trading_days": 2, "expected_horizon_days": 5},
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("position_lifecycle_failed", reasons)
        self.assertEqual(
            diagnostics["holding_rebalance_control"]["decision"],
            "exit_current_technical_invalidation",
        )

    def test_opposite_raw_target_cannot_skip_fundamental_reduce(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=500.0,
        )
        ratio, reasons, _notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-05",
            position_ratio=-0.08,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.BULLISH, confidence=0.70),
                AnalystSignal(
                    agent_name="fundamental",
                    signal=Signal.BEARISH,
                    confidence=0.70,
                    horizon_class="medium",
                    business_quality_score=0.70,
                    metadata={"tradeability": "medium"},
                ),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.60, "confidence": 0.65},
            short_scores={"score": 0.35, "confidence": 0.50},
            market_confirmation={"confirmation_score": 0.55},
            full_config={},
            fusion_context={},
            risk_level=RiskLevel.SAFE,
            opening_fac_context={"held_trading_days": 2, "expected_horizon_days": 5},
        )

        self.assertAlmostEqual(ratio, 0.06)
        self.assertIn("fundamental_medium_opposition", reasons)
        self.assertEqual(
            diagnostics["holding_rebalance_control"]["decision"],
            "reduce_fundamental_medium_opposition",
        )

    def test_losing_position_with_current_evidence_can_hold_same_side(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-2500.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.10,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.75,
                    invalidation_level=100.0,
                    metadata={"action_evidence_contract": {"invalidation_present": True}},
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.70, "confidence": 0.70},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.62},
            full_config={},
            fusion_context={},
            risk_level=RiskLevel.SAFE,
        )

        self.assertAlmostEqual(ratio, 0.10)
        self.assertNotIn("position_lifecycle_loss_revalidation_failed", reasons)
        self.assertTrue(diagnostics["holding_rebalance_control"]["loss_revalidated"])

    def test_ordinary_loss_revalidation_reduces_at_two_percent_without_fac_break(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-2500.0,
        )
        ratio, reasons, _notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-10",
            position_ratio=0.10,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.20, "confidence": 0.40},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.40},
            full_config={
                "portfolio_manager": {
                    "holding_rebalance_control": {
                        "horizon_consistency": {"enabled": False}
                    }
                }
            },
            fusion_context={},
            risk_level=RiskLevel.SAFE,
            opening_fac_context={"held_trading_days": 5, "expected_horizon_days": 7},
        )

        self.assertEqual(ratio, 0.05)
        self.assertIn("position_lifecycle_loss_revalidation_failed", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertFalse(detail["explicit_lifecycle_break"])
        self.assertEqual(detail["decision"], "reduce_failed_loss_revalidation")

    def test_ordinary_loss_revalidation_exits_at_four_percent_without_fac_break(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-4500.0,
        )
        ratio, reasons, _notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-10",
            position_ratio=0.10,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.20, "confidence": 0.40},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.40},
            full_config={
                "portfolio_manager": {
                    "holding_rebalance_control": {
                        "horizon_consistency": {"enabled": False}
                    }
                }
            },
            fusion_context={},
            risk_level=RiskLevel.SAFE,
            opening_fac_context={"held_trading_days": 5, "expected_horizon_days": 7},
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("position_lifecycle_loss_revalidation_failed", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertFalse(detail["explicit_lifecycle_break"])
        self.assertEqual(detail["decision"], "exit_failed_loss_revalidation")

    def test_new_losing_position_reduces_without_same_day_reconfirmation(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-800.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.10,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.45),
                AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.65, horizon_class="medium"),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            long_scores={"score": 0.30, "confidence": 0.50},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.50},
            full_config={},
            fusion_context={
                "analyst_quality": {
                    "fundamental": {"effective_confidence": 0.65, "tradeability": "medium"}
                }
            },
            risk_level=RiskLevel.SAFE,
        )

        self.assertEqual(ratio, 0.05)
        self.assertIn("new_position_loss_revalidation_failed", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertTrue(detail["new_loss_revalidation_failed"])
        self.assertFalse(detail["new_loss_revalidation_exit"])
        self.assertEqual(detail["decision"], "reduce_failed_new_loss_revalidation")
        self.assertIn("current_signal_neutral_or_absent", detail["new_loss_revalidation_failures"])
        self.assertIn(
            "missing_position_exit_boundary",
            detail["new_loss_revalidation_failures"],
        )

    def test_medium_horizon_new_entry_caps_to_probe_when_short_timing_is_missing(self):
        position = None
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.08,
            current_ratio=0.0,
            current_position=position,
            analyst_signals=[
                AnalystSignal(
                    agent_name="fundamental",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    horizon_class="medium",
                    invalidation_level=3200.0,
                ),
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.40, horizon_class="short"),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            long_scores={"score": 0.45, "confidence": 0.60},
            short_scores={"score": 0.05, "confidence": 0.20},
            market_confirmation={"confirmation_score": 0.60},
            full_config={},
            fusion_context={
                "analyst_quality": {
                    "fundamental": {"effective_confidence": 0.70, "tradeability": "medium"}
                }
            },
            risk_level=RiskLevel.SAFE,
        )

        self.assertAlmostEqual(ratio, 0.008)
        self.assertIn("horizon_consistency_probe_cap", reasons)
        self.assertEqual(
            diagnostics["holding_rebalance_control"]["raw_target_ratio_after_horizon_gate"],
            ratio,
        )

    def test_losing_position_with_same_side_fundamental_anchor_is_not_forced_out(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=-2500.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.10,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(
                    agent_name="fundamental",
                    signal=Signal.BULLISH,
                    confidence=0.45,
                    business_quality_score=0.65,
                ),
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.20, "confidence": 0.40},
            short_scores={"score": 0.10, "confidence": 0.30},
            market_confirmation={"confirmation_score": 0.50},
            full_config={},
            fusion_context={
                "analyst_quality": {
                    "fundamental": {
                        "effective_confidence": 0.45,
                        "tradeability": "medium",
                    }
                }
            },
            risk_level=RiskLevel.SAFE,
        )

        self.assertAlmostEqual(ratio, 0.10)
        self.assertNotIn("new_position_loss_revalidation_failed", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertTrue(detail["fundamental_supports_current"])
        self.assertTrue(detail["loss_revalidated"])
        self.assertEqual(detail["decision"], "keep_current_small_delta")

    def test_profitable_supported_hold_defers_plain_exit(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=1200.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="ZZ",
            trading_date="2025-03-05",
            position_ratio=0.0,
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.65,
                    invalidation_level=100.0,
                    opportunity_state="tradeable_candidate",
                    setup_quality_score=0.66,
                    entry_quality="acceptable",
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            long_scores={"score": 0.55, "confidence": 0.60},
            short_scores={"score": 0.18, "confidence": 0.35},
            market_confirmation={"confirmation_score": 0.48},
            full_config={},
            fusion_context={
                "analyst_quality": {
                    "technical": {"effective_confidence": 0.65, "tradeability": "high"}
                },
                "opportunity_scorecard": {
                    "long": {"final_state": "tradeable_candidate", "gating_failures": []}
                }
            },
            risk_level=RiskLevel.SAFE,
        )

        self.assertAlmostEqual(ratio, 0.10)
        self.assertIn("profitable_hold_continuation", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertTrue(detail["profitable_hold_supported"])
        self.assertEqual(detail["decision"], "keep_profitable_supported_exit_deferred")

    def test_profitable_fundamental_anchor_can_defer_plain_exit(self):
        position = SimpleNamespace(
            shares=-1,
            entry_date="2025-04-02",
            margin_used=25000.0,
            unrealized_pnl=210.0,
        )
        ratio, reasons, notes, diagnostics = _apply_holding_rebalance_control(
            ticker="BU",
            trading_date="2025-04-07",
            position_ratio=0.0,
            current_ratio=-0.007,
            current_position=position,
            analyst_signals=[
                AnalystSignal(agent_name="technical", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(
                    agent_name="fundamental",
                    signal=Signal.BEARISH,
                    confidence=0.38,
                    business_quality_score=0.60,
                    tradeability="medium",
                ),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.35),
            ],
            long_scores={"score": 0.05, "confidence": 0.20},
            short_scores={"score": 0.30, "confidence": 0.40},
            market_confirmation={"confirmation_score": 0.40},
            full_config={},
            fusion_context={
                "analyst_quality": {
                    "fundamental": {
                        "effective_confidence": 0.38,
                        "tradeability": "medium",
                        "business_quality_score": 0.60,
                    }
                }
            },
            risk_level=RiskLevel.SAFE,
        )

        self.assertAlmostEqual(ratio, -0.007)
        self.assertIn("profitable_hold_continuation", reasons)
        detail = diagnostics["holding_rebalance_control"]
        self.assertTrue(detail["profitable_hold_supported"])
        self.assertEqual(detail["decision"], "keep_profitable_supported_exit_deferred")

    def test_existing_lot_hold_ratio_is_not_truncated_to_flat(self):
        target_lots, preserved = _preserve_existing_lot_when_hold_ratio_survives(
            target_lots=0,
            current_lots=1,
            target_ratio=0.008,
            current_ratio=0.008,
            control_reasons=["profitable_hold_continuation"],
        )

        self.assertTrue(preserved)
        self.assertEqual(target_lots, 1)

    def test_multi_lot_hold_ratio_is_not_repriced_into_a_reduce(self):
        target_lots, preserved = _preserve_existing_lot_when_hold_ratio_survives(
            target_lots=-16,
            current_lots=-17,
            target_ratio=-0.1210,
            current_ratio=-0.1210,
            control_reasons=["holding_lifecycle_not_invalidated"],
        )

        self.assertTrue(preserved)
        self.assertEqual(target_lots, -17)

    def test_true_reduce_ratio_is_not_preserved_as_hold(self):
        target_lots, preserved = _preserve_existing_lot_when_hold_ratio_survives(
            target_lots=-8,
            current_lots=-17,
            target_ratio=-0.0605,
            current_ratio=-0.1210,
            control_reasons=["holding_lifecycle_not_invalidated"],
        )

        self.assertFalse(preserved)
        self.assertEqual(target_lots, -8)

    def test_profitable_supported_hold_blocks_weak_reversal_but_not_strong_reversal(self):
        position = SimpleNamespace(
            shares=10,
            entry_date="2025-03-03",
            margin_used=100000.0,
            unrealized_pnl=1500.0,
        )
        common = dict(
            ticker="ZZ",
            trading_date="2025-03-05",
            current_ratio=0.10,
            current_position=position,
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.65,
                    invalidation_level=100.0,
                    opportunity_state="tradeable_candidate",
                    setup_quality_score=0.66,
                    entry_quality="acceptable",
                ),
                AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.30),
            ],
            market_confirmation={"confirmation_score": 0.50},
            full_config={},
            fusion_context={
                "analyst_quality": {
                    "technical": {"effective_confidence": 0.65, "tradeability": "high"}
                },
                "opportunity_scorecard": {
                    "long": {"final_state": "tradeable_candidate", "gating_failures": []}
                }
            },
            risk_level=RiskLevel.SAFE,
        )
        weak_ratio, weak_reasons, _notes, weak_diag = _apply_holding_rebalance_control(
            position_ratio=-0.08,
            long_scores={"score": 0.55, "confidence": 0.60},
            short_scores={"score": 0.40, "confidence": 0.45},
            **common,
        )
        self.assertAlmostEqual(weak_ratio, 0.10)
        self.assertIn("profitable_hold_continuation", weak_reasons)
        self.assertEqual(
            weak_diag["holding_rebalance_control"]["decision"],
            "keep_profitable_supported_reversal_blocked",
        )

        strong_ratio, strong_reasons, _notes, strong_diag = _apply_holding_rebalance_control(
            position_ratio=-0.08,
            long_scores={"score": 0.20, "confidence": 0.35},
            short_scores={"score": 0.70, "confidence": 0.70},
            **{
                **common,
            "analyst_signals": [
                    AnalystSignal(agent_name="technical", signal=Signal.BEARISH, confidence=0.75, invalidation_level=110.0),
                    AnalystSignal(agent_name="fundamental", signal=Signal.NEUTRAL, confidence=0.35),
                    AnalystSignal(agent_name="commodity_news", signal=Signal.BEARISH, confidence=0.65),
                ],
                "market_confirmation": {"confirmation_score": 0.72},
                "fusion_context": {
                    "analyst_quality": {
                        "technical": {"effective_confidence": 0.75, "tradeability": "high"},
                        "commodity_news": {"effective_confidence": 0.65, "tradeability": "high"},
                    },
                    "opportunity_scorecard": {
                        "long": {"final_state": "watch_for_trigger", "gating_failures": []},
                        "short": {"final_state": "tradeable_candidate", "gating_failures": []},
                    }
                },
            },
        )
        self.assertAlmostEqual(strong_ratio, 0.0)
        self.assertNotIn("profitable_hold_continuation", strong_reasons)
        self.assertIn(
            strong_diag["holding_rebalance_control"]["decision"],
            {"exit_current_technical_invalidation"},
        )


class LearningUsageMetricsRegressionTest(unittest.TestCase):
    def test_learning_context_memory_ratio_counts_unique_prompt_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "eval.db"
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE learning_context_budget (
                        config_id TEXT,
                        trading_date TEXT,
                        analyst TEXT,
                        ticker TEXT,
                        trade_episode_count INTEGER,
                        hypothesis_count INTEGER,
                        total_context_chars INTEGER
                    )
                    """
                )
                cursor.executemany(
                    "INSERT INTO learning_context_budget VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("cfg", "2025-03-01", "technical", "BU", 1, 1, 500),
                        ("cfg", "2025-03-01", "fundamental", "BU", 0, 1, 300),
                        ("cfg", "2025-03-01", "portfolio_manager", "BU", 0, 0, 100),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            metrics = calculate_learning_usage_metrics("cfg", str(db_path), "2025-03-01", "2025-03-01")

        self.assertEqual(metrics["learning_context_budget_rows"], 3)
        self.assertEqual(metrics["learning_context_with_episode_rows"], 1)
        self.assertEqual(metrics["learning_context_with_hypothesis_rows"], 2)
        self.assertAlmostEqual(metrics["learning_context_with_memory_ratio"], 2 / 3)


class DailyTransactionReportRegressionTest(unittest.TestCase):
    def test_phase4_status_override_preserves_complete_report_layout(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE trading_day_phase (
                    config_id TEXT,
                    trading_date TEXT,
                    phase TEXT,
                    status TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    message TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL,
                    commission REAL,
                    holding_pnl REAL,
                    new_position_pnl REAL,
                    close_pnl REAL
                )
                """
            )
            cursor.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT, trading_date TEXT)")
            cursor.executemany(
                "INSERT INTO trading_day_phase VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("cfg", "2025-03-07", "phase1", "completed", "s1", "e1", ""),
                    ("cfg", "2025-03-07", "phase2", "completed", "s2", "e2", "transactions=0"),
                    ("cfg", "2025-03-07", "phase3", "completed", "s3", "e3", "balance=1000000.00"),
                    ("cfg", "2025-03-07", "phase4", "running", "s4", "", ""),
                ],
            )
            text = _build_daily_transaction_report(
                cfg={"tickers": ["BU"]},
                config_id="cfg",
                trading_date="2025-03-07",
                cursor=cursor,
                settlement_row={
                    "current_balance": 1_000_000,
                    "current_margin": 0,
                    "previous_balance": 1_000_000,
                    "previous_margin": 0,
                    "daily_pnl": 0,
                    "commission": 0,
                    "margin_ratio": 0,
                    "cash_available": 1_000_000,
                },
                latest_portfolio={"total_assets": 1_000_000, "leverage": 1, "positions": {}},
                strategy_recommendations=[],
                recommendations=[],
                phase2_transactions=[],
                ticker_pnl={},
                phase4_status_override="completed",
                phase4_completed_at_override="e4",
                phase4_message_override="reviewer validation passed",
            )
        finally:
            conn.close()

        self.assertIn("phase4", text)
        self.assertIn("phase4             completed", text)
        self.assertIn("reviewer validation passed", text)
        self.assertNotIn("phase4             running", text)


class ArtifactValidationRegressionTest(unittest.TestCase):
    def test_validate_artifacts_can_scope_by_trading_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "artifact.db"
            existing = root / "existing.json"
            payload = b'{"ok": true}'
            existing.write_bytes(payload)
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE futures_recommendation (
                        id TEXT,
                        trading_date TEXT,
                        signal_snapshot_artifact_path TEXT,
                        signal_snapshot_sha256 TEXT,
                        signal_snapshot_size INTEGER
                    )
                    """
                )
                cursor.executemany(
                    "INSERT INTO futures_recommendation VALUES (?, ?, ?, ?, ?)",
                    [
                        ("old", "2025-02-24", str(root / "missing-old.json"), "sha", 1),
                        ("new", "2025-03-03", str(existing), hashlib.sha256(payload).hexdigest(), len(payload)),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            full_report = validate_artifacts(db_path)
            scoped_report = validate_artifacts(db_path, start_date="2025-03-03", end_date="2025-03-03")

        self.assertFalse(full_report["ok"])
        self.assertEqual(len(full_report["missing"]), 1)
        self.assertTrue(scoped_report["ok"])
        self.assertEqual(scoped_report["checked"], 1)
        self.assertEqual(scoped_report["missing"], [])


class _DrawdownScenarioDB:
    def __init__(self, drawdown: float, *, hard_streak: int = 0, recovery_rows=None):
        self.drawdown = drawdown
        self.hard_streak = hard_streak
        self.recovery_rows = recovery_rows or []

    def get_account_drawdown_state(self, **kwargs):
        return {
            "latest_date": "2025-03-03",
            "latest_equity": 1.0 - self.drawdown,
            "peak_equity": 1.0,
            "drawdown": self.drawdown,
        }

    def get_ticker_loss_state(self, **kwargs):
        return {
            "lookback_days": kwargs.get("lookback_days", 5),
            "trade_days": 0,
            "cumulative_pnl": 0.0,
            "consecutive_loss_days": 0,
        }

    def _get_connection(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE portfolio (
                id TEXT,
                config_id TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE daily_settlement (
                portfolio_id TEXT,
                trading_date TEXT,
                current_balance REAL,
                current_margin REAL,
                created_at TEXT
            )
            """
        )
        for idx in range(self.hard_streak):
            day = f"2025-03-0{idx + 1}"
            cur.execute("INSERT INTO portfolio VALUES (?, ?)", (f"p{idx}", "cfg"))
            cur.execute(
                "INSERT INTO daily_settlement VALUES (?, ?, ?, ?, ?)",
                (f"p{idx}", day, 940000.0, 0.0, "now"),
            )
        cur.execute(
            """
            CREATE TABLE ticker_daily_pnl (
                portfolio_id TEXT,
                trading_date TEXT,
                ticker TEXT,
                daily_pnl REAL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE futures_transactions (
                config_id TEXT,
                trading_date TEXT,
                ticker TEXT,
                action TEXT,
                recommendation_id TEXT,
                created_at TEXT,
                id TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT,
                signal_snapshot TEXT,
                signal_snapshot_artifact_path TEXT,
                signal_snapshot_sha256 TEXT
            )
            """
        )
        for idx, row in enumerate(self.recovery_rows):
            portfolio_id = f"rp{idx}"
            rec_id = f"rr{idx}"
            day = row["trading_date"]
            cur.execute("INSERT INTO portfolio VALUES (?, ?)", (portfolio_id, "cfg"))
            cur.execute(
                "INSERT INTO ticker_daily_pnl VALUES (?, ?, ?, ?)",
                (portfolio_id, day, row.get("ticker", "ZZ"), row.get("daily_pnl", 0.0)),
            )
            cur.execute(
                "INSERT INTO futures_transactions VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("cfg", day, row.get("ticker", "ZZ"), "open_long", rec_id, f"{day}T09:00:00", f"tx{idx}"),
            )
            snapshot = {
                "pm_internal_draft": {
                    "strategy_controls": {
                        "diagnostics": {
                            "drawdown_control": {
                                "mode": "hard_recovery_probe",
                            }
                        }
                    }
                }
            }
            cur.execute(
                "INSERT INTO futures_recommendation VALUES (?, ?, ?, ?)",
                (rec_id, json.dumps(snapshot), None, None),
            )
        conn.commit()
        return conn


class DrawdownProtectionRegressionTest(unittest.TestCase):
    def _base_config(self):
        return {
            "cashflow": 1000000.0,
            "drawdown_control": {
                "enabled": True,
                "warning_drawdown": 0.04,
                "hard_drawdown": 0.05,
                "warning_cap_multiplier": 0.60,
                "warning_target_margin_ratio_max": 0.04,
                "initial_hard_cooldown_days": 1,
                "recovery_probe_min_confirmation_score": 0.65,
                "recovery_probe_min_confirmations": 3,
                "recovery_probe_require_stop_protection": True,
                "recovery_probe_margin_ratio_max": 0.015,
                "recovery_restore_step_margin_ratios": [0.01, 0.02],
            },
            "ticker_loss_control": {"enabled": False},
        }

    def test_warning_drawdown_scales_new_exposure(self):
        ratio, reasons, notes, diagnostics = _apply_drawdown_and_ticker_loss_control(
            db=_DrawdownScenarioDB(0.045),
            config_id="cfg",
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.80,
            current_ratio=0.0,
            current_margin_ratio=0.0,
            margin_rate=0.10,
            market_confirmation={"confirmation_score": 0.70},
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={},
            analyst_signals=[],
            full_config=self._base_config(),
        )

        self.assertAlmostEqual(ratio, 0.40)
        self.assertIn("drawdown_control", reasons)
        self.assertEqual(diagnostics["drawdown_control"]["state"], "warning")

    def test_hard_drawdown_initial_cooldown_blocks_new_exposure(self):
        ratio, reasons, notes, diagnostics = _apply_drawdown_and_ticker_loss_control(
            db=_DrawdownScenarioDB(0.052, hard_streak=0),
            config_id="cfg",
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.20,
            current_ratio=0.0,
            current_margin_ratio=0.0,
            margin_rate=0.10,
            market_confirmation={"confirmation_score": 0.90, "confirmations": ["a", "b", "c"]},
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={},
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    invalidation_level=100.0,
                    position_invalidation_level=100.0,
                )
            ],
            full_config=self._base_config(),
        )

        self.assertEqual(ratio, 0.0)
        self.assertIn("drawdown_control", reasons)
        self.assertEqual(diagnostics["drawdown_control"]["mode"], "hard_initial_cooldown")
        self.assertTrue(diagnostics["drawdown_control"]["counterfactual_recommendation"])

    def test_hard_drawdown_recovery_probe_allows_only_small_validated_risk(self):
        ratio, reasons, notes, diagnostics = _apply_drawdown_and_ticker_loss_control(
            db=_DrawdownScenarioDB(0.052, hard_streak=2),
            config_id="cfg",
            ticker="ZZ",
            trading_date="2025-03-04",
            position_ratio=0.50,
            current_ratio=0.0,
            current_margin_ratio=0.0,
            margin_rate=0.10,
            market_confirmation={"confirmation_score": 0.90, "confirmations": ["a", "b", "c"]},
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={},
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    invalidation_level=100.0,
                    position_invalidation_level=100.0,
                )
            ],
            full_config=self._base_config(),
        )

        self.assertAlmostEqual(ratio, 0.10)
        self.assertIn("drawdown_recovery_probe", reasons)
        self.assertEqual(diagnostics["drawdown_control"]["mode"], "hard_recovery_probe")
        self.assertFalse(diagnostics["drawdown_control"]["counterfactual_recommendation"])


class IntradayExecutionRegressionTest(unittest.TestCase):
    @staticmethod
    def _execution_contract(profile: str, side: str, *, direct: bool = False) -> dict:
        analyst = "commodity_news" if profile == "event_immediate" else "technical"
        entry_level = 1.0 if side == "long" else 1_000_000_000.0
        position_level = 0.5 if side == "long" else 2_000_000_000.0
        return {
            "execution_profile": profile,
            "trigger_source": trigger_source_for_analyst_profile(analyst, profile),
            "entry_trigger": canonical_entry_trigger(profile, side),
            "invalidation": canonical_entry_invalidation_condition(profile, side),
            "invalidation_level": entry_level,
            "position_invalidation_level": position_level,
            "valid_until": "2099-12-31 15:00:00",
            "can_execute_without_intraday_trigger": direct,
        }

    @staticmethod
    def _pandaai_minute_router(*, symbol: str, trading_code: str):
        calls = []
        api = PandaAIAPI.__new__(PandaAIAPI)

        def minute_row(timestamp: str, *, open_price: float, high: float, low: float, close: float):
            row_datetime = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
            return {
                "date": row_datetime.strftime("%Y%m%d"),
                "minute": row_datetime.strftime("%H%M%S"),
                "datetime": timestamp,
                "symbol": symbol,
                "dominant_id": "",
                "trading_code": trading_code,
                "underlying_symbol": "SR",
                "exchange": "CZCE",
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": 10,
                "trading_date": "20250326",
            }

        def query_market_min_data(**kwargs):
            calls.append((kwargs["symbol"], kwargs["frequency"]))
            if kwargs["frequency"] == "15m":
                return [
                    minute_row(
                        "2025-03-26 10:00:00",
                        open_price=5800.0,
                        high=5850.0,
                        low=5790.0,
                        close=5840.0,
                    )
                ]
            return [
                minute_row(
                    "2025-03-26 09:30:00",
                    open_price=5800.0,
                    high=5810.0,
                    low=5790.0,
                    close=5800.0,
                ),
                minute_row(
                    "2025-03-26 09:31:00",
                    open_price=5800.0,
                    high=5810.0,
                    low=5790.0,
                    close=5800.0,
                ),
                minute_row(
                    "2025-03-26 10:01:00",
                    open_price=5845.0,
                    high=5855.0,
                    low=5835.0,
                    close=5850.0,
                ),
            ]

        api._query_market_min_data = query_market_min_data
        router = Router.__new__(Router)
        router.api = api
        return router, calls

    def test_pandaai_czce_short_code_reaches_trader_breakout_for_15m_and_1m(self):
        router, calls = self._pandaai_minute_router(
            symbol="SR2505.CZC",
            trading_code="SR505",
        )

        basis, selection = resolve_intraday_execution_basis(
            router=router,
            config={
                "execution": {
                    "intraday_confirmation": {
                        "enabled": True,
                        "decision_frequency": "15m",
                        "execution_frequency": "1m",
                        "opening_range_minutes": 2,
                        "min_execution_volume": 1,
                        "max_chase_ratio": 0.02,
                    }
                }
            },
            underlying_code="SR",
            trading_date="2025-03-26",
            action="open_long",
            contract_code="SR2505",
            decision_context={
                "execution_contract": self._execution_contract("breakout", "long")
            },
        )

        self.assertTrue(selection.should_execute)
        self.assertEqual(selection.reason, "intraday_trigger_confirmed")
        self.assertEqual(basis.base_price, 5845.0)
        self.assertIsNone(basis.warning_message)
        self.assertEqual(
            calls,
            [("SR2505.CZC", "15m"), ("SR2505.CZC", "1m")],
        )

    def test_pandaai_wrong_month_reaches_existing_trader_fetch_failure(self):
        router, calls = self._pandaai_minute_router(
            symbol="SR2506.CZC",
            trading_code="SR506",
        )

        with self.assertRaisesRegex(RuntimeError, "^intraday_market_data_fetch_failed$"):
            resolve_intraday_execution_basis(
                router=router,
                config={
                    "execution": {
                        "intraday_confirmation": {
                            "enabled": True,
                            "decision_frequency": "15m",
                            "execution_frequency": "1m",
                        }
                    }
                },
                underlying_code="SR",
                trading_date="2025-03-26",
                action="open_long",
                contract_code="SR2505",
                decision_context={
                    "execution_contract": self._execution_contract(
                        "event_immediate",
                        "long",
                        direct=True,
                    )
                },
            )

        self.assertEqual(calls, [("SR2505.CZC", "15m")])

    def test_pm_execution_contract_classifies_technical_pullback(self):
        action_contract = build_test_aec(
            "technical",
            signal="Bullish",
            side="long",
            opportunity_state="watch_for_trigger",
            setup_type="trend_pullback",
            trigger_valid=False,
            current_trigger_confirmed=False,
            invalidation_present=True,
            entry_trigger=canonical_entry_trigger("pullback", "long"),
            invalidation_condition="15m close below 96",
            extra={"entry_timing_signal": "pullback"},
        )
        plan = _build_pm_decision_context(
            target_lots=2,
            current_price=100.0,
            position_ratio=0.02,
            risk_level=RiskLevel.SAFE,
            long_scores={"confidence": 0.70},
            short_scores={"confidence": 0.20},
            margin_rate=0.10,
            current_lots=0,
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    confidence=0.70,
                    entry_trigger="Pullback to VWAP support then stabilize",
                    invalidation_level=96.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    metadata={"action_evidence_contract": action_contract},
                ),
            ],
            final_entry_authority={
                "authority_type": "exploration_probe",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "conditional_trigger_authority": True,
                "requires_intraday_confirmation": True,
                "max_allowed_margin_ratio": 0.015,
            },
            trading_date="2025-01-06",
            recommendation_intent={"action": "open_long"},
            control_reasons=["controlled_probe"],
        )

        execution_contract = plan
        self.assertEqual(execution_contract["execution_profile"], "pullback")
        self.assertEqual(execution_contract["trigger_source"], "technical_pullback")
        self.assertTrue(execution_contract["requires_intraday_confirmation"])
        self.assertFalse(execution_contract["can_execute_without_intraday_trigger"])
        self.assertNotIn("analyst_execution_roles", execution_contract)
        learning = _setup_execution_learning_context({
            "final_action_contract": {
                **execution_contract,
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "strategy",
                "ticker": "ZZ",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": 2,
                "lots_delta": 2,
            },
        })
        self.assertNotIn("analyst_action_evidence_contracts", learning)
        self.assertNotIn("analyst_learning_scopes", learning)

    def test_pm_does_not_accept_fundamental_as_execution_source(self):
        action_contract = build_test_aec(
            "fundamental",
            signal="Bullish",
            side="long",
            opportunity_state="no_opportunity",
            setup_type="trend_breakout",
            trigger_valid=False,
            current_trigger_confirmed=False,
            invalidation_present=False,
            entry_trigger="",
        )
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.80,
            opportunity_state="watch_for_trigger",
            entry_trigger="15m close above the factor confirmation level",
            invalidation_level=96.0,
            metadata={"action_evidence_contract": action_contract},
        )
        with self.assertRaisesRegex(ValueError, "pm_execution_evidence_not_found"):
            _build_pm_decision_context(
                target_lots=2,
                current_price=100.0,
                position_ratio=0.02,
                risk_level=RiskLevel.SAFE,
                long_scores={"confidence": 0.70},
                short_scores={"confidence": 0.20},
                margin_rate=0.10,
                current_lots=0,
                analyst_signals=[signal],
                final_entry_authority={
                    "authority_type": "exploration_probe",
                    "open_action_evidence": False,
                    "strong_current_evidence": False,
                    "conditional_trigger_authority": True,
                    "requires_intraday_confirmation": True,
                    "max_allowed_margin_ratio": 0.015,
                },
                trading_date="2025-01-06",
                recommendation_intent={"action": "open_long"},
                control_reasons=["controlled_probe"],
            )

    def test_pm_execution_contract_classifies_authorized_event_immediate(self):
        action_contract = build_test_aec(
            "commodity_news",
            signal="Bullish",
            side="long",
            opportunity_state="tradeable_candidate",
            setup_type="event_catalyst",
            trigger_valid=True,
            current_trigger_confirmed=True,
            invalidation_present=True,
            entry_trigger=None,
            invalidation_condition="catalyst expires or 15m price fails to hold",
            extra={"event_type": "supply_disruption"},
        )
        plan = _build_pm_decision_context(
            target_lots=3,
            current_price=100.0,
            position_ratio=0.04,
            risk_level=RiskLevel.SAFE,
            long_scores={"confidence": 0.80},
            short_scores={"confidence": 0.20},
            margin_rate=0.10,
            current_lots=0,
            analyst_signals=[
                AnalystSignal(
                    agent_name="commodity_news",
                    signal=Signal.BULLISH,
                    confidence=0.80,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="15m price confirms the fresh supply disruption event",
                    event_type="supply_disruption",
                    trigger_valid=True,
                    invalidation_present=True,
                    exit_hint="Catalyst expires or price fails to hold",
                    metadata={"action_evidence_contract": action_contract},
                ),
            ],
            final_entry_authority={
                "authority_type": "real_budget_entry",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "max_allowed_margin_ratio": 0.06,
            },
            trading_date="2025-01-06",
            recommendation_intent={"action": "open_long"},
            control_reasons=["qualified_positive_expectancy"],
        )

        execution_contract = plan
        self.assertEqual(execution_contract["execution_profile"], "event_immediate")
        self.assertEqual(execution_contract["trigger_source"], "commodity_news_event")
        self.assertTrue(execution_contract["can_execute_without_intraday_trigger"])

    def test_execution_action_value_is_advisory_and_does_not_rewrite_execution_contract(self):
        action_contract = build_test_aec(
            "technical",
            signal="Bearish",
            side="short",
            opportunity_state="tradeable_candidate",
            setup_type="trend_breakout",
            trigger_valid=True,
            current_trigger_confirmed=True,
            invalidation_present=True,
            entry_trigger=canonical_entry_trigger("breakout", "short"),
            invalidation_condition="15m close above 104",
        )
        plan = _build_pm_decision_context(
            ticker="BU",
            target_lots=-3,
            current_price=100.0,
            position_ratio=-0.02,
            risk_level=RiskLevel.SAFE,
            long_scores={"confidence": 0.20},
            short_scores={"confidence": 0.72},
            margin_rate=0.10,
            current_lots=0,
            analyst_signals=[
                AnalystSignal(
                    agent_name="technical",
                    signal=Signal.BEARISH,
                    confidence=0.72,
                    opportunity_state="tradeable_candidate",
                    entry_trigger="Opening range breakdown",
                    invalidation_level=104.0,
                    trigger_valid=True,
                    invalidation_present=True,
                    metadata={"action_evidence_contract": action_contract},
                ),
            ],
            final_entry_authority={
                "authority_type": "exploration_probe",
                "open_action_evidence": False,
                "strong_current_evidence": False,
                "max_allowed_margin_ratio": 0.015,
            },
            trading_date="2025-03-24",
            recommendation_intent={"action": "open_short"},
            control_reasons=["controlled_probe"],
            alpha_setup_action_values=[
                {
                    "ticker": "BU",
                    "side": "short",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "action_name": "execution",
                    "setup_type": "trend_breakout_setup",
                    "data_combo": "technical:trend_breakout|execution:breakout",
                    "execution_retrieval_key": "BU|breakout|technical_breakout|execution",
                    "sample_count": 3,
                    "reward_mean": -1600.0,
                    "reward_sum": -4800.0,
                    "win_rate": 0.0,
                    "confidence_score": 0.62,
                    "action_preference": "cap_reduce_or_revalidate",
                    "payload": {
                        "source": "alpha_setup_profile_action_value",
                        "real_trade_reward_count": 3,
                        "exact_state_real_trade_sample_count": 3,
                        "amplification_scope_quality": "exact_real_state",
                    },
                }
            ],
        )

        execution_contract = plan
        self.assertEqual(execution_contract["execution_profile"], "breakout")
        self.assertEqual(execution_contract["trigger_source"], "technical_breakout")
        self.assertEqual(
            execution_contract["execution_action_value_preference"]["execution_profile"],
            "pullback",
        )
        self.assertFalse(execution_contract["can_execute_without_intraday_trigger"])

    def test_vwap_confirmed_profile_requires_vwap_direction_and_chase_check(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 96, "close": 98.0, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 98.1, "high": 99, "low": 97, "close": 98.2, "volume": 10},
        ]

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_short",
            config={"opening_range_minutes": 2, "min_execution_volume": 1, "max_chase_ratio": 0.02},
            decision_context={
                "execution_contract": self._execution_contract(
                    "vwap_confirmed", "short"
                )
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.reason, "intraday_vwap_confirmed")
        self.assertEqual(result.features["execution_profile"], "vwap_confirmed")
        self.assertEqual(result.features["trigger_rule"], "vwap_direction_confirmation")
        self.assertTrue(result.to_audit_payload()["trigger_passed"])

    def test_long_trigger_uses_completed_signal_bar_and_next_execution_open(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 104, "low": 99, "close": 103, "volume": 10},
            {"datetime": "2025-01-06 10:15:00", "open": 103, "high": 106, "low": 102, "close": 105, "volume": 12},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 10},
            {"datetime": "2025-01-06 10:16:00", "open": 105, "high": 106, "low": 104, "close": 105, "volume": 10},
        ]
        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={
                "opening_range_minutes": 30,
                "min_execution_volume": 1,
                "max_chase_ratio": 0.02,
            },
            decision_context={
                "execution_contract": self._execution_contract("breakout", "long")
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.base_price, 105.0)
        self.assertEqual(result.base_datetime, "2025-01-06 10:16:00")
        self.assertEqual(result.reason, "intraday_trigger_confirmed")

    def test_pullback_profile_requires_expansion_retrace_and_reclaim(self):
        signal_bars = [
            {"datetime": "2025-01-06 09:45:00", "open": 101.5, "high": 102.5, "low": 101.4, "close": 102.0, "volume": 10},
            {"datetime": "2025-01-06 10:00:00", "open": 101.8, "high": 102.0, "low": 100.8, "close": 101.5, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:46:00", "open": 102, "high": 102.2, "low": 101.8, "close": 102, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 101.6, "high": 101.8, "low": 101.4, "close": 101.7, "volume": 10},
        ]

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1, "max_chase_ratio": 0.02},
            decision_context={
                "execution_contract": self._execution_contract("pullback", "long")
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.reason, "intraday_pullback_confirmed")
        self.assertEqual(result.features["execution_profile"], "pullback")
        self.assertEqual(
            result.features["trigger_rule"],
            "directional_expansion_then_boundary_or_vwap_reclaim",
        )

    def test_event_immediate_requires_pm_authority_to_bypass_intraday_trigger(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 100, "low": 99, "close": 99, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 99, "high": 100, "low": 98, "close": 99, "volume": 10},
        ]

        blocked = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={
                "execution_contract": self._execution_contract(
                    "event_immediate", "long"
                )
            },
            finalize_untriggered=True,
        )
        allowed = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={
                "execution_contract": self._execution_contract(
                    "event_immediate", "long", direct=True
                )
            },
            finalize_untriggered=True,
        )

        self.assertFalse(blocked.should_execute)
        self.assertEqual(blocked.reason, "intraday_trigger_not_met")
        self.assertTrue(allowed.should_execute)
        self.assertEqual(allowed.reason, "intraday_event_immediate_execution")
        self.assertTrue(allowed.to_audit_payload()["trigger_passed"])

    def test_untriggered_signal_is_waiting_until_finalized(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 99, "close": 99, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
        ]
        waiting = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 30, "min_execution_volume": 1},
            decision_context={
                "execution_contract": self._execution_contract("breakout", "long")
            },
            finalize_untriggered=False,
        )
        finalized = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 30, "min_execution_volume": 1},
            decision_context={
                "execution_contract": self._execution_contract("breakout", "long")
            },
            finalize_untriggered=True,
        )

        self.assertEqual(waiting.decision, "wait")
        self.assertEqual(waiting.reason, "intraday_waiting_for_trigger")
        self.assertEqual(finalized.decision, "skip")
        self.assertEqual(finalized.reason, "intraday_trigger_not_met")

    def test_opening_range_must_complete_before_trigger(self):
        signal_bars = [
            {"datetime": "2025-01-06 09:15:00", "open": 118, "high": 121, "low": 117, "close": 120, "volume": 10},
        ]
        execution_bars = [
            {"datetime": f"2025-01-06 09:{minute:02d}:00", "open": 100, "high": 119, "low": 99, "close": 100, "volume": 10}
            for minute in range(1, 31)
        ]
        execution_bars.append(
            {"datetime": "2025-01-06 09:31:00", "open": 120, "high": 121, "low": 119, "close": 120, "volume": 10}
        )

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 30, "min_execution_volume": 1, "max_chase_ratio": 0.02},
            decision_context={
                "execution_contract": self._execution_contract("breakout", "long")
            },
            finalize_untriggered=True,
        )

        self.assertFalse(result.should_execute)
        self.assertEqual(result.reason, "intraday_trigger_not_met")
        self.assertEqual(result.features["eligible_signal_bars"], 0)
        self.assertTrue(result.features["opening_range"]["complete"])

    def test_incomplete_opening_range_waits_in_paper_loop(self):
        signal_bars = [
            {"datetime": "2025-01-06 09:15:00", "open": 100, "high": 104, "low": 99, "close": 103, "volume": 10},
        ]
        execution_bars = [
            {"datetime": f"2025-01-06 09:{minute:02d}:00", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 10}
            for minute in range(1, 21)
        ]

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 30, "min_execution_volume": 1},
            decision_context={
                "execution_contract": self._execution_contract("breakout", "long")
            },
            finalize_untriggered=False,
        )

        self.assertEqual(result.decision, "wait")
        self.assertEqual(result.reason, "intraday_opening_range_incomplete")
        self.assertFalse(result.features["opening_range"]["complete"])

    def test_cutoff_datetime_prevents_future_minute_bars_from_triggering(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 100, "low": 99, "close": 99, "volume": 10},
            {"datetime": "2025-01-06 14:45:00", "open": 101, "high": 105, "low": 101, "close": 105, "volume": 20},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 99, "high": 100, "low": 98, "close": 99, "volume": 10},
            {"datetime": "2025-01-06 14:46:00", "open": 105, "high": 106, "low": 104, "close": 105, "volume": 20},
        ]

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 2, "min_execution_volume": 1, "max_chase_ratio": 0.02},
            decision_context={
                "execution_contract": self._execution_contract("breakout", "long")
            },
            cutoff_datetime=datetime(2025, 1, 6, 10, 5, 0),
            finalize_untriggered=False,
        )

        self.assertFalse(result.should_execute)
        self.assertEqual(result.decision, "wait")
        self.assertEqual(result.reason, "intraday_waiting_for_trigger")
        self.assertEqual(result.features["signal_bars"], 1)
        self.assertEqual(result.features["execution_bars"], 3)

    def test_intraday_execution_rejects_research_memory_parameters(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 99, "close": 100.85, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 100.9, "high": 101, "low": 100, "close": 100.9, "volume": 10},
        ]

        with self.assertRaises(TypeError):
            select_intraday_execution(
                signal_bars=signal_bars,
                execution_bars=execution_bars,
                action="open_long",
                config={"opening_range_minutes": 2, "min_execution_volume": 1},
                strategy_memory={"side_memory": {"memory_state": "protected"}},
            )
        with self.assertRaises(TypeError):
            select_intraday_execution(
                signal_bars=signal_bars,
                execution_bars=execution_bars,
                action="open_long",
                config={"opening_range_minutes": 2, "min_execution_volume": 1},
                adaptive_policy_state=[{"policy_type": "contextual_rule_calibration:intraday_confirmation"}],
            )
        with self.assertRaises(TypeError):
            select_intraday_execution(
                signal_bars=signal_bars,
                execution_bars=execution_bars,
                action="open_long",
                config={"opening_range_minutes": 2, "min_execution_volume": 1},
                market_confirmation={"confirmation_score": 0.75},
            )

    def test_old_memory_fallback_config_cannot_create_execution_trigger(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 99, "close": 100.85, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 100.9, "high": 101, "low": 100, "close": 100.9, "volume": 10},
        ]

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={
                "opening_range_minutes": 2,
                "min_execution_volume": 1,
                "max_chase_ratio": 0.02,
                "allow_confirmed_memory_vwap_fallback": True,
                "confirmed_memory_min_market_confirmation_score": 0.70,
                "confirmed_memory_min_confirmations": 3,
                "confirmed_memory_max_opening_range_miss": 0.002,
                "confirmed_memory_min_sample_count": 5,
            },
            decision_context={
                "execution_contract": {
                    **self._execution_contract("breakout", "long"),
                    "allow_confirmed_memory_vwap_fallback": False,
                }
            },
        )

        self.assertFalse(result.should_execute)
        self.assertEqual(result.reason, "intraday_trigger_not_met")
        self.assertNotEqual(result.reason, "intraday_confirmed_memory_vwap_fallback")
        self.assertNotEqual(result.features.get("execution_mode"), "confirmed_memory_vwap_fallback")

    def test_pm_must_encode_vwap_execution_profile_in_contract(self):
        signal_bars = [
            {"datetime": "2025-01-06 10:00:00", "open": 100, "high": 101, "low": 99, "close": 100.85, "volume": 10},
        ]
        execution_bars = [
            {"datetime": "2025-01-06 09:30:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 09:31:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
            {"datetime": "2025-01-06 10:01:00", "open": 100.9, "high": 101, "low": 100, "close": 100.9, "volume": 10},
        ]

        result = select_intraday_execution(
            signal_bars=signal_bars,
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 2, "min_execution_volume": 1, "max_chase_ratio": 0.02},
            decision_context={
                "execution_contract": self._execution_contract(
                    "vwap_confirmed", "long"
                )
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.reason, "intraday_vwap_confirmed")
        self.assertEqual(result.features["execution_profile"], "vwap_confirmed")
        self.assertEqual(result.features["trigger_rule"], "vwap_direction_confirmation")


class MarginAuditRegressionTest(unittest.TestCase):
    def test_close_trade_records_released_margin_without_negative_balance(self):
        audit = calculate_margin_audit(
            action="close_long",
            lots=4,
            current_shares=8,
            current_margin_used=48276.0,
        )

        self.assertAlmostEqual(audit["released_margin"], 24138.0)
        self.assertAlmostEqual(audit["post_trade_margin_used"], 24138.0)
        self.assertAlmostEqual(audit["margin_delta"], -24138.0)
        self.assertGreaterEqual(audit["post_trade_margin_used"], 0.0)


class FuturesTradePairRegressionTest(unittest.TestCase):
    def test_trade_pair_builder_handles_partial_closes(self):
        transactions = [
            {
                "id": "o1",
                "recommendation_id": "r1",
                "trading_date": "2025-10-13",
                "created_at": "2025-10-13T09:00:00",
                "ticker": "M",
                "contract_code": "m2601",
                "action": "open_long",
                "lots": 2,
                "execution_price": 100.0,
                "contract_multiplier": 10.0,
                "commission": 2.0,
            },
            {
                "id": "c1",
                "recommendation_id": "r2",
                "trading_date": "2025-10-14",
                "created_at": "2025-10-14T09:00:00",
                "ticker": "M",
                "contract_code": "m2601",
                "action": "close_long",
                "lots": 1,
                "execution_price": 110.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
            },
            {
                "id": "c2",
                "recommendation_id": "r3",
                "trading_date": "2025-10-15",
                "created_at": "2025-10-15T09:00:00",
                "ticker": "M",
                "contract_code": "m2601",
                "action": "close_long",
                "lots": 1,
                "execution_price": 90.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
            },
        ]

        pairs = build_completed_trade_pairs(transactions)
        summary = summarize_trade_pairs(pairs)

        self.assertEqual(len(pairs), 2)
        self.assertAlmostEqual(pairs[0]["net_pnl"], 98.0)
        self.assertAlmostEqual(pairs[1]["net_pnl"], -102.0)
        self.assertEqual(summary["winning_trades"], 1)
        self.assertEqual(summary["losing_trades"], 1)

    def test_trade_pair_builder_can_exclude_rollover_transactions(self):
        transactions = [
            {
                "id": "o1",
                "recommendation_id": "r1",
                "trading_date": "2025-10-13",
                "created_at": "2025-10-13T09:00:00",
                "ticker": "C",
                "contract_code": "c2601",
                "action": "open_short",
                "lots": 1,
                "execution_price": 2100.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
                "source_type": "strategy",
            },
            {
                "id": "c1",
                "recommendation_id": "r2",
                "trading_date": "2025-10-15",
                "created_at": "2025-10-15T09:00:00",
                "ticker": "C",
                "contract_code": "c2601",
                "action": "close_short",
                "lots": 1,
                "execution_price": 2090.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
                "source_type": "rollover",
            },
        ]

        all_pairs = build_completed_trade_pairs(transactions)
        strategy_only_pairs = build_completed_trade_pairs(transactions, include_rollover=False)

        self.assertEqual(len(all_pairs), 1)
        self.assertTrue(all_pairs[0]["contains_rollover"])
        self.assertEqual(strategy_only_pairs, [])

    def test_trade_pair_builder_excludes_forced_risk_from_strategy_only_view(self):
        transactions = [
            {
                "id": "o1",
                "recommendation_id": "r1",
                "trading_date": "2025-10-13",
                "created_at": "2025-10-13T09:00:00",
                "ticker": "C",
                "contract_code": "c2601",
                "action": "open_short",
                "lots": 1,
                "execution_price": 2100.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
                "source_type": "strategy",
            },
            {
                "id": "c1",
                "recommendation_id": "risk1",
                "trading_date": "2025-10-15",
                "created_at": "2025-10-15T09:00:00",
                "ticker": "C",
                "contract_code": "c2601",
                "action": "close_short",
                "lots": 1,
                "execution_price": 2090.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
                "source_type": "forced_risk",
            },
        ]

        all_pairs = build_completed_trade_pairs(transactions)
        strategy_only_pairs = build_completed_trade_pairs(transactions, include_rollover=False)

        self.assertEqual(len(all_pairs), 1)
        self.assertTrue(all_pairs[0]["contains_forced_risk"])
        self.assertTrue(all_pairs[0]["contains_non_strategy"])
        self.assertEqual(strategy_only_pairs, [])


class ConditionalPerformanceRegressionTest(unittest.TestCase):
    def test_conditional_trade_performance_filters_signal_combo_and_future_rows(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE futures_transactions (
                    id TEXT,
                    config_id TEXT,
                    recommendation_id TEXT,
                    trading_date TEXT,
                    created_at TEXT,
                    ticker TEXT,
                    contract_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    execution_price REAL,
                    price REAL,
                    contract_multiplier REAL,
                    commission REAL,
                    source_type TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT,
                    signal_snapshot TEXT
                )
                """
            )
            cur.executemany(
                "INSERT INTO futures_recommendation VALUES (?, ?)",
                [
                    (
                        "r1",
                        json.dumps(
                            {
                                "signal_collection_contract": _scc_from_test_payloads(
                                    technical={"signal": "Bullish"},
                                    fundamental={"signal": "Bullish"},
                                    commodity_news={"signal": "Neutral"},
                                ),
                            }
                        ),
                    ),
                    (
                        "r2",
                        json.dumps(
                            {
                                "signal_collection_contract": _scc_from_test_payloads(
                                    technical={"signal": "Bearish"},
                                    fundamental={"signal": "Neutral"},
                                    commodity_news={"signal": "Bearish"},
                                ),
                            }
                        ),
                    ),
                    (
                        "r3",
                        json.dumps(
                            {
                                "signal_collection_contract": _scc_from_test_payloads(
                                    technical={"signal": "Bullish"},
                                    fundamental={"signal": "Bullish"},
                                    commodity_news={"signal": "Neutral"},
                                ),
                            }
                        ),
                    ),
                ],
            )
            rows = [
                ("o1", "cfg", "r1", "2025-10-13", "2025-10-13T09:00:00", "M", "m2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("c1", "cfg", "rc1", "2025-10-14", "2025-10-14T09:00:00", "M", "m2601", "close_long", 1, 110.0, 110.0, 10.0, 1.0, "strategy"),
                ("o2", "cfg", "r2", "2025-10-15", "2025-10-15T09:00:00", "M", "m2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("c2", "cfg", "rc2", "2025-10-16", "2025-10-16T09:00:00", "M", "m2601", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                ("o3", "cfg", "r3", "2025-10-20", "2025-10-20T09:00:00", "M", "m2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("c3", "cfg", "rc3", "2025-10-21", "2025-10-21T09:00:00", "M", "m2601", "close_long", 1, 120.0, 120.0, 10.0, 1.0, "strategy"),
            ]
            cur.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            conn.close()

            db = SQLiteDB()
            db.db_path = db_path
            summary = db.get_futures_conditional_trade_performance(
                config_id="cfg",
                ticker="M",
                side="long",
                trading_date="2025-10-17",
                signal_combo=["Bullish", "Bullish", "Neutral"],
                lookback_trades=30,
            )

            self.assertEqual(summary["total_trades"], 1)
            self.assertEqual(summary["winning_trades"], 1)
            self.assertAlmostEqual(summary["total_pnl"], 98.0)
            self.assertEqual(summary["cutoff_trading_date"], "2025-10-17")
        finally:
            os.remove(db_path)

    def test_futures_transaction_memory_uses_only_past_trades(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE futures_transactions (
                    id TEXT,
                    config_id TEXT,
                    recommendation_id TEXT,
                    trading_date TEXT,
                    created_at TEXT,
                    ticker TEXT,
                    contract_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    execution_price REAL,
                    price REAL,
                    contract_multiplier REAL,
                    commission REAL,
                    source_type TEXT
                )
                """
            )
            cur.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("past", "cfg", "r1", "2025-03-03", "2025-03-03T09:00:00", "BU", "bu2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                    ("same", "cfg", "r2", "2025-03-04", "2025-03-04T09:00:00", "BU", "bu2601", "close_long", 1, 120.0, 120.0, 10.0, 1.0, "strategy"),
                    ("future", "cfg", "r3", "2025-03-05", "2025-03-05T09:00:00", "BU", "bu2601", "open_short", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                ],
            )
            conn.commit()
            conn.close()

            db = SQLiteDB()
            db.db_path = db_path
            memory = db.get_futures_transaction_memory("cfg", "BU", limit=10, trading_date="2025-03-04")

            self.assertEqual(memory, ["2025-03-03 open_long 1@100.0"])
        finally:
            os.remove(db_path)

    def test_strategy_memory_refreshes_from_completed_trade_pairs(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE futures_transactions (
                    id TEXT,
                    config_id TEXT,
                    recommendation_id TEXT,
                    trading_date TEXT,
                    created_at TEXT,
                    ticker TEXT,
                    contract_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    execution_price REAL,
                    price REAL,
                    contract_multiplier REAL,
                    commission REAL,
                    source_type TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT,
                    signal_snapshot TEXT
                )
                """
            )
            cur.executemany(
                "INSERT INTO futures_recommendation VALUES (?, ?)",
                [
                    (
                        "bu-r1",
                        json.dumps(
                            {
                                "signal_collection_contract": _scc_from_test_payloads(
                                    technical={"signal": "Bullish"}, fundamental={"signal": "Bullish"}, commodity_news={"signal": "Neutral"}
                                ),
                            }
                        ),
                    ),
                    (
                        "bu-r2",
                        json.dumps(
                            {
                                "signal_collection_contract": _scc_from_test_payloads(
                                    technical={"signal": "Bullish"}, fundamental={"signal": "Bullish"}, commodity_news={"signal": "Neutral"}
                                ),
                            }
                        ),
                    ),
                    (
                        "bu-r3",
                        json.dumps(
                            {
                                "signal_collection_contract": _scc_from_test_payloads(
                                    technical={"signal": "Bullish"}, fundamental={"signal": "Bullish"}, commodity_news={"signal": "Neutral"}
                                ),
                            }
                        ),
                    ),
                    (
                        "p-r1",
                        json.dumps(
                            {
                                "signal_collection_contract": _scc_from_test_payloads(
                                    technical={"signal": "Bearish"}, fundamental={"signal": "Neutral"}, commodity_news={"signal": "Bullish"}
                                ),
                            }
                        ),
                    ),
                    (
                        "p-r2",
                        json.dumps(
                            {
                                "signal_collection_contract": _scc_from_test_payloads(
                                    technical={"signal": "Bearish"}, fundamental={"signal": "Neutral"}, commodity_news={"signal": "Bullish"}
                                ),
                            }
                        ),
                    ),
                ],
            )
            rows = [
                ("bu-o1", "cfg", "bu-r1", "2025-10-13", "2025-10-13T09:00:00", "BU", "bu2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("bu-c1", "cfg", "bu-c1-rec", "2025-10-14", "2025-10-14T14:50:00", "BU", "bu2601", "close_long", 1, 160.0, 160.0, 10.0, 1.0, "strategy"),
                ("bu-o2", "cfg", "bu-r2", "2025-10-15", "2025-10-15T09:00:00", "BU", "bu2601", "open_long", 1, 110.0, 110.0, 10.0, 1.0, "strategy"),
                ("bu-c2", "cfg", "bu-c2-rec", "2025-10-16", "2025-10-16T14:50:00", "BU", "bu2601", "close_long", 1, 170.0, 170.0, 10.0, 1.0, "strategy"),
                ("bu-o3", "cfg", "bu-r3", "2025-10-17", "2025-10-17T09:00:00", "BU", "bu2601", "open_long", 1, 120.0, 120.0, 10.0, 1.0, "strategy"),
                ("bu-c3", "cfg", "bu-c3-rec", "2025-10-20", "2025-10-20T14:50:00", "BU", "bu2601", "close_long", 1, 180.0, 180.0, 10.0, 1.0, "strategy"),
                ("p-o1", "cfg", "p-r1", "2025-10-13", "2025-10-13T09:01:00", "P", "p2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("p-c1", "cfg", "p-c1-rec", "2025-10-14", "2025-10-14T14:51:00", "P", "p2601", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                ("p-o2", "cfg", "p-r2", "2025-10-15", "2025-10-15T09:01:00", "P", "p2601", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("p-c2", "cfg", "p-c2-rec", "2025-10-16", "2025-10-16T14:51:00", "P", "p2601", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
            ]
            cur.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            conn.close()

            db = SQLiteDB()
            db.db_path = db_path
            refreshed = db.refresh_strategy_memory("cfg", "2025-10-20")
            bu_memory = db.get_strategy_memory(
                "cfg",
                "BU",
                "long",
                "2025-10-21",
                ["Bullish", "Bullish", "Neutral"],
            )
            p_memory = db.get_strategy_memory(
                "cfg",
                "P",
                "long",
                "2025-10-21",
                ["Bearish", "Neutral", "Bullish"],
            )

            self.assertGreaterEqual(refreshed, 4)
            self.assertEqual(bu_memory["side_memory"]["memory_state"], "protected")
            self.assertEqual(bu_memory["combo"]["memory_state"], "protected")
            self.assertEqual(p_memory["side_memory"]["memory_state"], "watchlist")
            self.assertEqual(p_memory["combo"]["memory_state"], "watchlist")
            self.assertEqual(bu_memory["combo"]["payload"]["cutoff_trading_date"], "2025-10-20")

            db.refresh_strategy_memory(
                "cfg",
                "2025-10-20",
                memory_config={"enabled": True, "min_samples_protected": 4},
            )
            stricter_bu_memory = db.get_strategy_memory(
                "cfg",
                "BU",
                "long",
                "2025-10-21",
                ["Bullish", "Bullish", "Neutral"],
            )
            self.assertEqual(stricter_bu_memory["side_memory"]["memory_state"], "recovering")
        finally:
            os.remove(db_path)

    def test_strategy_memory_uses_only_past_source_dates(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = SQLiteDB()
            db.db_path = db_path
            conn = db._get_connection()
            cur = conn.cursor()
            db._ensure_strategy_memory_schema(cur)
            cur.executemany(
                """
                INSERT INTO strategy_memory (
                    id, config_id, ticker, side, signal_combo, memory_state,
                    sample_count, win_rate, net_pnl, avg_pnl, confidence_score,
                    source, reason, source_trading_date, updated_at, valid_until, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "past",
                        "cfg",
                        "BU",
                        "long",
                        "*",
                        "protected",
                        4,
                        0.75,
                        12000.0,
                        3000.0,
                        0.8,
                        "attribution_auto",
                        "past memory",
                        "2025-03-03",
                        "2025-03-03T20:00:00",
                        "2025-04-03",
                        "{}",
                    ),
                    (
                        "same_day",
                        "cfg",
                        "BU",
                        "long",
                        '["Bullish"]',
                        "weak_block",
                        4,
                        0.0,
                        -12000.0,
                        -3000.0,
                        0.9,
                        "same_day",
                        "same day memory",
                        "2025-03-04",
                        "2025-03-04T20:00:00",
                        "2025-04-04",
                        "{}",
                    ),
                    (
                        "unknown_source",
                        "cfg",
                        "BU",
                        "long",
                        '["Bullish"]',
                        "weak_block",
                        4,
                        0.0,
                        -20000.0,
                        -5000.0,
                        1.0,
                        "legacy",
                        "legacy memory",
                        None,
                        "2025-03-01T20:00:00",
                        "2025-04-01",
                        "{}",
                    ),
                ],
            )
            conn.commit()
            conn.close()

            memory = db.get_strategy_memory("cfg", "BU", "long", "2025-03-04", ["Bullish"])

            self.assertEqual(memory["side_memory"]["id"], "past")
            self.assertIsNone(memory["combo"])
            self.assertEqual([row["id"] for row in memory["records"]], ["past"])
        finally:
            os.remove(db_path)


class SettlementAccountingRegressionTest(unittest.TestCase):
    def test_dev_config_catalogs_expand_to_runtime_shape(self):
        config_path = SRC_ROOT / "config" / "dev.yaml"
        with config_path.open("r", encoding="utf-8") as fh:
            cfg = normalize_config(yaml.safe_load(fh), config_path)

        loaded = cfg.get("_config_catalogs_loaded", {})
        self.assertEqual(
            set(loaded),
            {
                "analyst_prior_profiles",
                "data_factor_policy",
                "execution_commission",
                "execution_exit_policy",
                "execution_slippage",
                "learning_policy",
                "portfolio_policy",
                "product_price_behavior_profiles",
                "rank_score_policy",
            },
        )
        self.assertIn("product_price_behavior_profiles", cfg)
        self.assertEqual(
            cfg["_config_parameter_roles"]["product_price_behavior_profiles"],
            "cold_start_analyst_differentiation_profile_only_not_trade_authority",
        )
        self.assertIn("rank_score_policy", cfg)
        self.assertEqual(
            cfg["_config_parameter_roles"]["rank_score_policy"],
            "full_market_rank_score_weight_catalog_not_trade_authority_not_position_size",
        )
        self.assertEqual(
            cfg["rank_score_policy"]["rank_score"]["open_add_action_value_delta"]["positive_learning_signal"],
            0.18,
        )
        self.assertEqual(cfg["max_total_margin_ratio"], 0.20)
        self.assertEqual(cfg["position_budget_policy"]["hard_max_total_margin_ratio"], 0.20)
        self.assertEqual(cfg["position_budget_policy"]["min_real_trade_margin_ratio"], 0.008)
        self.assertEqual(cfg["position_budget_policy"]["probe_margin_ratio"], 0.008)

        self.assertTrue(cfg["market_confirmation"]["enabled"])
        self.assertTrue(cfg["portfolio_manager"]["holding_rebalance_control"]["enabled"])
        self.assertTrue(cfg["pm_risk_gate"]["enabled"])
        self.assertTrue(cfg["trade_frequency_control"]["enabled"])
        self.assertNotIn("ticker_performance_control", cfg)
        self.assertTrue(cfg["ticker_loss_control"]["enabled"])
        self.assertTrue(cfg["dynamic_weights"]["enabled"])
        self.assertEqual(
            cfg["_config_parameter_roles"]["pm_risk_gate"],
            "portfolio_policy_catalog_runtime_expanded",
        )
        self.assertEqual(
            cfg["_config_parameter_roles"]["trade_frequency_control"],
            "portfolio_policy_catalog_runtime_expanded",
        )
        self.assertNotIn("ticker_performance_control", cfg["_config_parameter_roles"])
        self.assertEqual(
            cfg["_config_parameter_roles"]["ticker_loss_control"],
            "portfolio_policy_catalog_runtime_expanded",
        )
        self.assertEqual(
            cfg["_config_parameter_roles"]["dynamic_weights"],
            "portfolio_policy_catalog_runtime_expanded",
        )
        self.assertFalse(cfg["analyst_weight_policy"]["static_weights_can_create_trade_authority"])
        self.assertNotIn("allow_static_weights_to_open", cfg["analyst_weight_policy"])
        self.assertEqual(
            cfg["_config_parameter_roles"]["portfolio_manager.sector_weights"],
            "cold_start_trade_timing_prior_only_not_trade_authority",
        )

        self.assertTrue(cfg["learning_retention"]["enabled"])
        self.assertEqual(cfg["learning_retention"]["detail_retention_days"], 90)
        self.assertEqual(cfg["learning_retention"]["aggregate_retention_days"], 180)
        self.assertTrue(cfg["factor_data"]["finoview_enabled"])
        self.assertTrue(cfg["fundamental_quality_control"]["enabled"])
        self.assertTrue(cfg["pandaai_extra_data"]["enabled"])

        execution = cfg["execution"]
        self.assertIn("commission", execution)
        self.assertIn("BU", execution["commission"]["by_underlying"])
        self.assertIn("slippage_ticks_by_underlying", execution)
        self.assertIn("BU", execution["slippage_ticks_by_underlying"])
        self.assertIn("exit_policy", execution)
        self.assertTrue(execution["intraday_confirmation"]["enabled"])
        self.assertNotIn("allow_confirmed_memory_vwap_fallback", execution["intraday_confirmation"])
        self.assertNotIn("confirmed_memory_min_market_confirmation_score", execution["intraday_confirmation"])
        self.assertNotIn("confirmed_memory_min_confirmations", execution["intraday_confirmation"])

        raw_dev = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        for moved_key in (
            "pm_risk_gate",
            "trade_frequency_control",
            "ticker_loss_control",
            "dynamic_weights",
        ):
            self.assertNotIn(moved_key, raw_dev)

    def test_analyst_prior_catalog_is_prior_only_trade_authority_policy(self):
        cfg = normalize_config(
            {
                "config_catalogs": {
                    "analyst_prior_profiles": "analyst_prior_profiles.yaml",
                    "portfolio_policy": "portfolio_policy_catalog.yaml",
                    "learning_policy": "learning_policy_catalog.yaml",
                    "data_factor_policy": "data_factor_policy_catalog.yaml",
                },
                "portfolio_manager": {},
                "analyst_weight_policy": {
                    "enabled": True,
                    "strategic_profile": "sector_strategic_view",
                    "trade_timing_profile": "daily_trade_timing",
                },
            },
            SRC_ROOT / "config" / "dev.yaml",
        )

        policy = cfg["analyst_weight_policy"]
        self.assertNotIn("static_weights_mode", policy)
        self.assertNotIn("static_weights_can_create_trade_authority", policy)
        self.assertNotIn("allow_static_weights_to_open", policy)
        self.assertIn("analyst_prior_profiles", cfg["_config_catalogs_loaded"])
        self.assertEqual(
            cfg["_config_parameter_roles"]["portfolio_manager.sector_weights"],
            "cold_start_trade_timing_prior_only_not_trade_authority",
        )
        self.assertEqual(
            cfg["_config_parameter_roles"]["analyst_applicability_profile"],
            "cold_start_applicability_prior_only_not_trade_authority",
        )
        self.assertEqual(
            cfg["analyst_applicability_profile"]["technical"]["horizon_multipliers"]["short"],
            1.15,
        )
        self.assertIn("portfolio_policy", cfg["_config_catalogs_loaded"])
        self.assertEqual(
            cfg["_config_parameter_roles"]["portfolio_manager"],
            "portfolio_policy_catalog_runtime_expanded",
        )
        self.assertEqual(
            cfg["market_confirmation"]["min_confirmation_score_for_new_entry"],
            0.55,
        )
        self.assertTrue(cfg["portfolio_manager"]["quality_aware_fusion"]["enabled"])
        self.assertIn("learning_policy", cfg["_config_catalogs_loaded"])
        self.assertEqual(
            cfg["_config_parameter_roles"]["learning_retention"],
            "learning_policy_catalog_runtime_expanded",
        )
        self.assertTrue(cfg["learning"]["contextual_rule_calibration"]["enabled"])
        self.assertEqual(cfg["learning_retention"]["detail_retention_days"], 90)
        self.assertEqual(
            cfg["_config_parameter_roles"]["signal_quality"],
            "learning_policy_catalog_runtime_expanded",
        )
        self.assertIn("required_neutral_fields", cfg["signal_quality"]["neutral_accountability"])
        self.assertEqual(cfg["signal_quality"]["neutral_accountability"]["counterfactual_forward_days"], 3)
        self.assertIn("data_factor_policy", cfg["_config_catalogs_loaded"])
        self.assertEqual(
            cfg["_config_parameter_roles"]["factor_data"],
            "data_factor_policy_catalog_runtime_expanded",
        )
        self.assertTrue(cfg["fundamental_quality_control"]["enabled"])
        self.assertTrue(cfg["pandaai_extra_data"]["enabled"])
        self.assertTrue(cfg["factor_data"]["finoview_enabled"])
        self.assertTrue(cfg["factor_data"]["news"]["enabled"])

    def test_settlement_balance_formula_includes_margin_change(self):
        settlement_row = {
            "previous_balance": 1000.0,
            "current_balance": 900.0,
            "previous_margin": 100.0,
            "current_margin": 250.0,
            "daily_pnl": 60.0,
            "commission": 10.0,
            "deposit": 0.0,
            "withdraw": 0.0,
        }

        actual_change = settlement_row["current_balance"] - settlement_row["previous_balance"]
        self.assertAlmostEqual(actual_change, _expected_settlement_balance_change(settlement_row))

    def test_settlement_equity_formula_excludes_margin_transfer(self):
        settlement_row = {
            "previous_balance": 1000.0,
            "current_balance": 900.0,
            "previous_margin": 100.0,
            "current_margin": 250.0,
            "previous_account_equity": 1100.0,
            "current_account_equity": 1150.0,
            "daily_pnl": 60.0,
            "commission": 10.0,
            "deposit": 0.0,
            "withdraw": 0.0,
        }

        equity_change = (
            settlement_row["current_account_equity"]
            - settlement_row["previous_account_equity"]
        )
        self.assertAlmostEqual(equity_change, _expected_settlement_equity_change(settlement_row))

    def test_accountant_fixed_sample_settlement_formulas_cover_pnl_fee_margin_and_equity(self):
        engine = FuturesDailySettlement.__new__(FuturesDailySettlement)
        reference_portfolio = Portfolio(
            id="portfolio-settlement-sample",
            cashflow=100000.0,
            account_equity=112000.0,
            cash_available=100000.0,
            margin_used=12000.0,
            margin_available=100000.0,
            margin_ratio=12000.0 / 112000.0,
            positions={
                "BU": Position(
                    shares=2,
                    value=60000.0,
                    entry_price=2950.0,
                    entry_date="2025-03-02",
                    contract_code="bu2506",
                    settle_price=3000.0,
                    margin_used=6000.0,
                    margin_rate=0.1,
                    contract_multiplier=10.0,
                    realized_pnl=100.0,
                ),
                "M": Position(
                    shares=1,
                    value=50000.0,
                    entry_price=4920.0,
                    entry_date="2025-03-02",
                    contract_code="m2505",
                    settle_price=5000.0,
                    margin_used=6000.0,
                    margin_rate=0.12,
                    contract_multiplier=10.0,
                ),
            },
        )
        ledgers = {
            "BU": {
                "starting_realized_pnl": 100.0,
                "realized_from_cost_today": 0.0,
                "breakdown": {
                    "holding_pnl": 0.0,
                    "new_position_pnl": 0.0,
                    "close_pnl": 0.0,
                    "commission": 20.0,
                },
                "batches": [
                    {
                        "origin": "overnight",
                        "side": "long",
                        "lots": 2,
                        "daily_reference_price": 3000.0,
                        "position_entry_price": 2950.0,
                        "position_entry_date": "2025-03-02",
                        "contract_code": "bu2506",
                        "contract_multiplier": 10.0,
                        "margin_rate": 0.1,
                    }
                ],
            },
            "RB": {
                "starting_realized_pnl": 0.0,
                "realized_from_cost_today": 0.0,
                "breakdown": {
                    "holding_pnl": 0.0,
                    "new_position_pnl": 0.0,
                    "close_pnl": 0.0,
                    "commission": 5.0,
                },
                "batches": [
                    {
                        "origin": "today",
                        "side": "long",
                        "lots": 1,
                        "daily_reference_price": 4000.0,
                        "position_entry_price": 4000.0,
                        "position_entry_date": "2025-03-03",
                        "contract_code": "rb2505",
                        "contract_multiplier": 10.0,
                        "margin_rate": 0.1,
                    }
                ],
            },
            "M": {
                "starting_realized_pnl": 0.0,
                "realized_from_cost_today": 1000.0,
                "breakdown": {
                    "holding_pnl": 0.0,
                    "new_position_pnl": 0.0,
                    "close_pnl": 1000.0,
                    "commission": 7.0,
                },
                "batches": [],
            },
        }
        settle_prices = {
            "bu2506": 3100.0,
            "rb2505": 4020.0,
        }

        summary = engine._finalize_ledgers(
            reference_portfolio=reference_portfolio,
            ledgers=ledgers,
            settle_prices=settle_prices,
            trading_date=datetime(2025, 3, 3),
        )

        self.assertAlmostEqual(summary["daily_pnl"], 3200.0)
        self.assertAlmostEqual(summary["commission"], 32.0)
        self.assertAlmostEqual(summary["previous_margin"], 12000.0)
        self.assertAlmostEqual(summary["current_margin"], 10220.0)
        self.assertAlmostEqual(summary["previous_account_equity"], 112000.0)
        self.assertAlmostEqual(summary["current_account_equity"], 115168.0)
        self.assertAlmostEqual(summary["cash_available"], 104948.0)
        self.assertAlmostEqual(summary["reserved_margin"], 10220.0)
        self.assertAlmostEqual(summary["portfolio"].cashflow, 104948.0)
        self.assertAlmostEqual(summary["portfolio"].account_equity, 115168.0)
        self.assertAlmostEqual(summary["portfolio"].margin_used, 10220.0)
        self.assertAlmostEqual(summary["portfolio"].margin_ratio, 10220.0 / 115168.0)
        self.assertAlmostEqual(summary["positions_detail"]["BU"]["holding_pnl"], 2000.0)
        self.assertAlmostEqual(summary["positions_detail"]["BU"]["commission"], 20.0)
        self.assertAlmostEqual(summary["positions_detail"]["BU"]["total_pnl"], 2000.0)
        self.assertEqual(summary["positions_detail"]["BU"]["lots"], 2)
        self.assertAlmostEqual(summary["positions_detail"]["RB"]["new_position_pnl"], 200.0)
        self.assertAlmostEqual(summary["positions_detail"]["RB"]["commission"], 5.0)
        self.assertAlmostEqual(summary["positions_detail"]["RB"]["total_pnl"], 200.0)
        self.assertEqual(summary["positions_detail"]["RB"]["lots"], 1)
        self.assertAlmostEqual(summary["positions_detail"]["M"]["close_pnl"], 1000.0)
        self.assertAlmostEqual(summary["positions_detail"]["M"]["commission"], 7.0)
        self.assertAlmostEqual(summary["positions_detail"]["M"]["total_pnl"], 1000.0)
        self.assertEqual(summary["positions_detail"]["M"]["lots"], 0)
        self.assertNotIn("M", summary["portfolio"].positions)

    def test_phase2_execution_moves_margin_between_cash_and_reserved_margin(self):
        engine = FuturesExecutionEngine({"execution": {}}, db=None)
        portfolio = Portfolio(id="p1", cashflow=1000.0, positions={})
        transaction = FuturesTransaction(
            id="tx1",
            portfolio_id="p1",
            config_id="cfg",
            recommendation_id="rec1",
            trading_date="2025-01-02",
            ticker="RB",
            contract_code="rb2505",
            action=FuturesAction.OPEN_LONG,
            lots=1,
            price=100.0,
            execution_price=100.0,
            contract_multiplier=10.0,
            margin_rate=0.1,
            margin_used=100.0,
            commission=2.0,
            execution_phase="phase2",
            created_at="2025-01-02T00:00:00",
        )

        portfolio = engine.apply_transaction_to_portfolio(portfolio, transaction)

        self.assertAlmostEqual(portfolio.cashflow, 898.0)
        self.assertAlmostEqual(portfolio.margin_used, 100.0)
        self.assertAlmostEqual(portfolio.account_equity, 998.0)
        self.assertAlmostEqual(portfolio.margin_available, 898.0)

    def test_hold_or_zero_lots_recommendation_is_skipped_not_executed(self):
        class DummyDB:
            def __init__(self):
                self.status_updates = []

            def update_futures_recommendation_status(self, recommendation_id, status, **kwargs):
                self.status_updates.append((recommendation_id, status, kwargs))

        db = DummyDB()
        engine = FuturesExecutionEngine({"execution": {}}, db=db)
        portfolio = Portfolio(id="p1", cashflow=1000.0, positions={})
        recommendation = {
            "id": "rec-hold",
            "config_id": "cfg",
            "portfolio_id": "p1",
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.HOLD.value,
            "lots": 0,
            "signal_snapshot": {
                "execution_translation": {
                    "rewrite_reasons": ["position_matched"],
                }
            },
        }

        result = engine.execute_recommendation(
            recommendation_id="rec-hold",
            recommendation=recommendation,
            portfolio=portfolio,
            trading_date="2025-01-02",
            execution_phase=TradingPhase.PHASE2,
        )

        self.assertIs(result, portfolio)
        self.assertEqual(db.status_updates[0][1], RecommendationStatus.SKIPPED.value)
        snapshot = db.status_updates[0][2]["signal_snapshot"]
        self.assertEqual(snapshot["execution_result"]["status"], RecommendationStatus.SKIPPED.value)
        self.assertEqual(snapshot["execution_result"]["transaction_count"], 0)
        self.assertEqual(snapshot["execution_result"]["no_trade_reason"], "position_matched")
        execution_trace = snapshot["execution_result"]["execution_learning_trace"]
        self.assertEqual(execution_trace["consumer_scope"], "trader_execution_learning")
        self.assertEqual(execution_trace["learning_lane"], "execution")
        self.assertIn("execution", execution_trace["execution_retrieval_key"])
        self.assertTrue(execution_trace["turn_into_memory"])

    def test_dynamic_margin_missing_provider_raises_when_static_fallback_disabled(self):
        engine = FuturesExecutionEngine(
            {
                "execution": {
                    "dynamic_margin": {
                        "enabled": True,
                        "provider": "pandaai",
                        "fallback_to_static_contract_cache": False,
                    }
                }
            },
            db=None,
        )

        with self.assertRaisesRegex(RuntimeError, "contract margin is unavailable"):
            engine._resolve_dynamic_margin_rate(
                action_value=RecommendationAction.OPEN_LONG.value,
                contract_code="rb2505",
                contract_info={"margin_rate_long": 0.10, "margin_rate_short": 0.12},
                trading_date="2025-01-02",
                contract_detail={},
            )

    def test_dynamic_margin_missing_provider_can_fall_back_when_explicitly_enabled(self):
        engine = FuturesExecutionEngine(
            {
                "execution": {
                    "dynamic_margin": {
                        "enabled": True,
                        "provider": "pandaai",
                        "fallback_to_static_contract_cache": True,
                    }
                }
            },
            db=None,
        )

        margin_rate, audit = engine._resolve_dynamic_margin_rate(
            action_value=RecommendationAction.OPEN_SHORT.value,
            contract_code="rb2505",
            contract_info={"margin_rate_long": 0.10, "margin_rate_short": 0.12},
            trading_date="2025-01-02",
            contract_detail={},
        )

        self.assertAlmostEqual(margin_rate, 0.12)
        self.assertEqual(audit["status"], "fallback_static_no_provider_margin")
        self.assertTrue(audit["fallback_to_static_contract_cache"])


class Phase2RankedDynamicMarginRegressionTest(unittest.TestCase):
    @staticmethod
    def _contract(
        ticker,
        *,
        current_lots=0,
        target_lots=0,
        final_action=None,
        rank_budget_sequence=None,
        opportunity_rank=None,
        conditional=False,
        max_allowed_margin_ratio=0.01,
    ):
        if final_action is None:
            if current_lots == target_lots:
                final_action = "hold" if current_lots else "wait"
            elif current_lots == 0:
                final_action = "open_probe"
            elif target_lots == 0:
                final_action = "exit"
            elif (current_lots > 0) == (target_lots > 0):
                final_action = "scale" if abs(target_lots) > abs(current_lots) else "reduce"
            else:
                final_action = "exit"
        deployment = {}
        if rank_budget_sequence is not None:
            deployment["rank_budget_sequence"] = rank_budget_sequence
        if opportunity_rank is not None:
            deployment["opportunity_rank"] = opportunity_rank
        return {
            "contract_version": "agentquant.final_action.v1",
            "contract_type": "strategy",
            "ticker": ticker,
            "final_action": final_action,
            "current_lots": int(current_lots),
            "target_lots": int(target_lots),
            "lots_delta": int(target_lots - current_lots),
            "authority_type": "exploration_probe",
            "authority_decision": "allow_probe",
            "open_action_evidence": True,
            "strong_current_evidence": True,
            "max_allowed_margin_ratio": max_allowed_margin_ratio,
            "conditional_trigger_authority": bool(conditional),
            "requires_intraday_confirmation": bool(conditional),
            "can_execute_without_intraday_trigger": not bool(conditional),
            "capital_deployment": deployment,
            "reason_codes": ["test_ranked_phase2_contract"],
            "single_source_of_trade_truth": True,
            "candidate_sources_do_not_bypass_contract": True,
        }

    @classmethod
    def _recommendation(
        cls,
        ticker,
        *,
        current_lots=0,
        target_lots=0,
        final_action=None,
        rank_budget_sequence=None,
        opportunity_rank=None,
        conditional=False,
        created_at=None,
        source_type="strategy",
        action=None,
        lots=None,
        base_price=100.0,
    ):
        contract = cls._contract(
            ticker,
            current_lots=current_lots,
            target_lots=target_lots,
            final_action=final_action,
            rank_budget_sequence=rank_budget_sequence,
            opportunity_rank=opportunity_rank,
            conditional=conditional,
        )
        intent = phase2_order_intent_from_lots(current_lots, target_lots)
        return {
            "id": f"rec-{ticker}",
            "config_id": "cfg",
            "portfolio_id": "pf",
            "underlying_code": ticker,
            "contract_code": f"{ticker}2505",
            "source_type": source_type,
            "action": action or intent["action"],
            "lots": int(intent["lots"] if lots is None else lots),
            "base_price": float(base_price),
            "created_at": created_at or f"2025-03-26T09:00:{ticker[-1:] or '0'}",
            "signal_snapshot": {"final_action_contract": contract},
        }

    @staticmethod
    def _portfolio(*, positions=None, equity=1000.0):
        positions = dict(positions or {})
        margin = sum(float(position.margin_used or 0.0) for position in positions.values())
        return Portfolio(
            id="pf",
            cashflow=float(equity) - margin,
            account_equity=float(equity),
            cash_available=float(equity) - margin,
            margin_used=margin,
            margin_available=float(equity) - margin,
            margin_ratio=margin / float(equity) if equity > 0 else 0.0,
            positions=positions,
        )

    @staticmethod
    def _build_transaction(engine, recommendation, portfolio, *, market_rules=None, margin_rate=1.0):
        contract_info = {
            "contract_multiplier": 1.0,
            "margin_rate_long": 0.1,
            "margin_rate_short": 0.1,
            "minimum_tick": 0.0,
        }
        with (
            patch(
                "tools.agent_tools.execution.trader_futures_execution.FuturesContractInfoCache.get_contract_info",
                return_value=contract_info,
            ),
            patch.object(engine, "_get_execution_quote", return_value=None),
            patch.object(engine, "_get_contract_detail", return_value=None),
            patch.object(engine, "_build_market_rules_audit", return_value=dict(market_rules or {})),
            patch.object(
                engine,
                "_resolve_dynamic_margin_rate",
                return_value=(float(margin_rate), {"selected_margin_rate": float(margin_rate), "status": "test"}),
            ),
            patch.object(engine, "_calculate_commission", return_value=0.0),
        ):
            return engine._build_transaction(
                recommendation,
                portfolio,
                "2025-03-26",
                TradingPhase.PHASE2,
            )

    def test_strategy_scheduler_frontloads_reductions_and_only_reorders_ranked_risk_slots(self):
        recommendations = [
            self._recommendation(
                "WAIT",
                rank_budget_sequence=98,
                created_at="2025-03-26T09:00:01",
            ),
            self._recommendation("R5", target_lots=1, rank_budget_sequence=5, opportunity_rank=1),
            self._recommendation("EXIT", current_lots=2, target_lots=0),
            self._recommendation("R4", target_lots=1, rank_budget_sequence=4, opportunity_rank=2),
            self._recommendation("HOLD", current_lots=2, target_lots=2, rank_budget_sequence=99),
            self._recommendation("REDUCE", current_lots=-3, target_lots=-1),
            self._recommendation("R3", target_lots=1, rank_budget_sequence=3, opportunity_rank=3),
            self._recommendation("R2", target_lots=1, rank_budget_sequence=2, opportunity_rank=4),
            self._recommendation("R1", target_lots=1, rank_budget_sequence=1, opportunity_rank=5),
        ]
        ticker_order = [item["underlying_code"] for item in recommendations]
        rank_payload_before = {
            item["id"]: json.dumps(
                item["signal_snapshot"]["final_action_contract"].get("capital_deployment") or {},
                sort_keys=True,
            )
            for item in recommendations
        }

        scheduled = trader_module._schedule_strategy_recommendations(recommendations, ticker_order)

        self.assertEqual(
            [item["underlying_code"] for item in scheduled],
            ["EXIT", "REDUCE", "WAIT", "R1", "R2", "HOLD", "R3", "R4", "R5"],
        )
        self.assertEqual(
            rank_payload_before,
            {
                item["id"]: json.dumps(
                    item["signal_snapshot"]["final_action_contract"].get("capital_deployment") or {},
                    sort_keys=True,
                )
                for item in recommendations
            },
        )

    def test_strategy_scheduler_accepts_only_non_bool_strict_positive_integer_rank(self):
        recommendations = [
            self._recommendation("VALID2", target_lots=1, rank_budget_sequence=2),
            self._recommendation("BOOL", target_lots=1, rank_budget_sequence=True),
            self._recommendation("VALID1", target_lots=1, rank_budget_sequence=1),
            self._recommendation("FLOAT", target_lots=1, rank_budget_sequence=1.0),
            self._recommendation("STRING", target_lots=1, rank_budget_sequence="1"),
            self._recommendation("ZERO", target_lots=1, rank_budget_sequence=0),
            self._recommendation("NEG", target_lots=1, rank_budget_sequence=-1),
        ]
        ticker_order = [item["underlying_code"] for item in recommendations]

        scheduled = trader_module._schedule_strategy_recommendations(recommendations, ticker_order)

        self.assertEqual(
            [item["underlying_code"] for item in scheduled],
            ["VALID1", "BOOL", "VALID2", "FLOAT", "STRING", "ZERO", "NEG"],
        )

    def test_initial_and_paper_strategy_scheduling_ignore_opportunity_rank_without_budget_sequence(self):
        recommendations = [
            self._recommendation("A", target_lots=1, opportunity_rank=3),
            self._recommendation("B", target_lots=1, opportunity_rank=1),
            self._recommendation("C", target_lots=1, opportunity_rank=2),
        ]
        ticker_order = ["A", "B", "C"]

        initial = trader_module._schedule_strategy_recommendations(list(recommendations), ticker_order)
        paper_loop = trader_module._schedule_strategy_recommendations(list(recommendations), ticker_order)

        self.assertEqual([item["underlying_code"] for item in initial], ["A", "B", "C"])
        self.assertEqual(
            [item["underlying_code"] for item in paper_loop],
            [item["underlying_code"] for item in initial],
        )
        trader_source = inspect.getsource(trader_module.trader_agent)
        self.assertEqual(trader_source.count("_schedule_strategy_recommendations("), 2)
        self.assertEqual(trader_source.count("_sort_recommendations_by_ticker_order("), 1)

    def test_legacy_cross_side_reversal_keeps_stable_slot_and_is_not_frontloaded(self):
        recommendations = [
            self._recommendation("R2", target_lots=1, rank_budget_sequence=2),
            self._recommendation(
                "REVERSAL",
                current_lots=2,
                target_lots=-3,
                final_action="exit",
            ),
            self._recommendation("EXIT", current_lots=2, target_lots=0),
            self._recommendation("R1", target_lots=1, rank_budget_sequence=1),
        ]
        ticker_order = [item["underlying_code"] for item in recommendations]

        scheduled = trader_module._schedule_strategy_recommendations(recommendations, ticker_order)

        self.assertEqual(
            [item["underlying_code"] for item in scheduled],
            ["EXIT", "R1", "REVERSAL", "R2"],
        )

    def test_dynamic_actual_margin_hardline_allows_below_and_equal_but_blocks_above(self):
        engine = FuturesExecutionEngine(
            {"max_total_margin_ratio": 0.20, "execution": {"default_slippage_ticks": 0}},
            db=None,
        )
        existing = Position(
            shares=1,
            entry_price=100.0,
            contract_code="BASE2505",
            contract_multiplier=1.0,
            margin_rate=1.0,
            margin_used=100.0,
        )

        below_portfolio = self._portfolio(positions={"BASE": existing})
        below = self._recommendation("LOW", target_lots=1, rank_budget_sequence=1, base_price=50.0)
        below["signal_snapshot"]["final_action_contract"]["max_allowed_margin_ratio"] = 0.01
        below_transaction = self._build_transaction(engine, below, below_portfolio)
        self.assertEqual(below_transaction.lots, 1)
        self.assertAlmostEqual(below_transaction.margin_used, 50.0)

        equal_portfolio = self._portfolio(positions={"BASE": existing})
        equal = self._recommendation("EQUAL", target_lots=1, rank_budget_sequence=1, base_price=100.0)
        equal_transaction = self._build_transaction(engine, equal, equal_portfolio)
        self.assertAlmostEqual(equal_transaction.margin_used, 100.0)

        above_portfolio = self._portfolio(positions={"BASE": existing})
        above = self._recommendation("ABOVE", target_lots=1, rank_budget_sequence=1, base_price=100.01)
        with self.assertRaises(ExecutionBlocked) as raised:
            self._build_transaction(engine, above, above_portfolio)
        self.assertEqual(raised.exception.reason, "margin_insufficient")
        self.assertEqual(set(above_portfolio.positions), {"BASE"})
        self.assertAlmostEqual(above_portfolio.margin_used, 100.0)

    def test_fractional_actual_margin_mathematically_equal_to_cap_is_allowed(self):
        engine = FuturesExecutionEngine(
            {"max_total_margin_ratio": 0.20, "execution": {"default_slippage_ticks": 0}},
            db=None,
        )
        portfolio = self._portfolio(equity=1.5)
        recommendation = self._recommendation(
            "FRACTIONAL",
            target_lots=3,
            rank_budget_sequence=1,
            base_price=0.1,
        )

        transaction = self._build_transaction(engine, recommendation, portfolio)

        self.assertEqual(transaction.lots, 3)
        self.assertAlmostEqual(transaction.margin_used, 0.3)

    def test_ranked_dynamic_margin_skips_only_over_cap_product_and_continues(self):
        engine = FuturesExecutionEngine(
            {"max_total_margin_ratio": 0.20, "execution": {"default_slippage_ticks": 0}},
            db=None,
        )
        portfolio = self._portfolio()
        accepted = []
        blocked = []

        for rank, (ticker, margin) in enumerate(
            [("R1", 50.0), ("R2", 50.0), ("R3", 50.0), ("R4", 60.0), ("R5", 40.0)],
            start=1,
        ):
            recommendation = self._recommendation(
                ticker,
                target_lots=1,
                rank_budget_sequence=rank,
                base_price=margin,
            )
            try:
                transaction = self._build_transaction(engine, recommendation, portfolio)
            except ExecutionBlocked as exc:
                blocked.append((ticker, exc.reason))
                continue
            accepted.append(ticker)
            portfolio = engine.apply_transaction_to_portfolio(portfolio, transaction)

        self.assertEqual(accepted, ["R1", "R2", "R3", "R5"])
        self.assertEqual(blocked, [("R4", "margin_insufficient")])
        self.assertNotIn("R4", portfolio.positions)
        self.assertAlmostEqual(portfolio.margin_used, 190.0)
        self.assertAlmostEqual(portfolio.margin_ratio, 0.19)

    def test_execution_engine_persists_margin_block_as_zero_transaction_and_returns_final_snapshot(self):
        class FakeDB:
            def __init__(self):
                self.status_updates = []
                self.saved_transactions = []

            def update_futures_recommendation_status(self, recommendation_id, status, **kwargs):
                self.status_updates.append((recommendation_id, status, kwargs))
                return True

            def save_futures_transaction(self, transaction):
                self.saved_transactions.append(transaction)
                return "unexpected"

        db = FakeDB()
        engine = FuturesExecutionEngine(
            {"max_total_margin_ratio": 0.20, "execution": {"default_slippage_ticks": 0}},
            db=db,
        )
        portfolio = self._portfolio(
            positions={
                "BASE": Position(
                    shares=1,
                    contract_code="BASE2505",
                    margin_used=100.0,
                )
            }
        )
        recommendation = self._recommendation(
            "BLOCK",
            target_lots=1,
            rank_budget_sequence=1,
            base_price=101.0,
        )
        recommendation["signal_snapshot"]["phase2_execution"] = {
            "status": "executed",
            "setup_execution_learning": {
                "phase2_status": "executed",
                "no_trade_reason": None,
                "reason_family": "executed_or_hold",
            },
        }
        contract_info = {
            "contract_multiplier": 1.0,
            "margin_rate_long": 0.1,
            "margin_rate_short": 0.1,
            "minimum_tick": 0.0,
        }

        with (
            patch(
                "tools.agent_tools.execution.trader_futures_execution.FuturesContractInfoCache.get_contract_info",
                return_value=contract_info,
            ),
            patch.object(engine, "_get_execution_quote", return_value=None),
            patch.object(engine, "_get_contract_detail", return_value=None),
            patch.object(engine, "_build_market_rules_audit", return_value={}),
            patch.object(
                engine,
                "_resolve_dynamic_margin_rate",
                return_value=(1.0, {"selected_margin_rate": 1.0, "status": "test"}),
            ),
            patch.object(engine, "_calculate_commission", return_value=0.0),
        ):
            result = engine.execute_recommendation(
                recommendation_id=recommendation["id"],
                recommendation=recommendation,
                portfolio=portfolio,
                trading_date="2025-03-26",
                execution_phase=TradingPhase.PHASE2,
            )

        self.assertIs(result, portfolio)
        self.assertEqual(db.saved_transactions, [])
        self.assertEqual(db.status_updates[-1][1], RecommendationStatus.SKIPPED.value)
        snapshot = recommendation["signal_snapshot"]
        self.assertEqual(snapshot["execution_result"]["transaction_count"], 0)
        self.assertEqual(snapshot["execution_result"]["no_trade_reason"], "margin_insufficient")
        self.assertEqual(snapshot["phase2_execution"]["status"], "skipped")
        self.assertEqual(
            snapshot["phase2_execution"]["setup_execution_learning"]["no_trade_reason"],
            "margin_insufficient",
        )
        self.assertEqual(
            snapshot["phase2_execution"]["setup_execution_learning"]["reason_family"],
            "business_execution",
        )
        self.assertEqual(set(portfolio.positions), {"BASE"})
        self.assertAlmostEqual(portfolio.margin_used, 100.0)

    def test_successful_exit_and_reduce_release_actual_margin_before_new_risk_check(self):
        engine = FuturesExecutionEngine(
            {"max_total_margin_ratio": 0.20, "execution": {"default_slippage_ticks": 0}},
            db=None,
        )
        portfolio = self._portfolio(
            positions={
                "EXIT": Position(
                    shares=1,
                    entry_price=60.0,
                    contract_code="EXIT2505",
                    contract_multiplier=1.0,
                    margin_rate=1.0,
                    margin_used=60.0,
                ),
                "REDUCE": Position(
                    shares=2,
                    entry_price=50.0,
                    contract_code="REDUCE2505",
                    contract_multiplier=1.0,
                    margin_rate=1.0,
                    margin_used=100.0,
                ),
                "OTHER": Position(
                    shares=1,
                    entry_price=30.0,
                    contract_code="OTHER2505",
                    contract_multiplier=1.0,
                    margin_rate=1.0,
                    margin_used=30.0,
                ),
            }
        )

        exit_recommendation = self._recommendation("EXIT", current_lots=1, target_lots=0, base_price=60.0)
        exit_transaction = self._build_transaction(engine, exit_recommendation, portfolio)
        portfolio = engine.apply_transaction_to_portfolio(portfolio, exit_transaction)
        self.assertEqual(portfolio.positions["EXIT"].shares, 0)
        self.assertAlmostEqual(portfolio.positions["EXIT"].margin_used, 0.0)
        self.assertAlmostEqual(portfolio.margin_used, 130.0)

        reduce_recommendation = self._recommendation("REDUCE", current_lots=2, target_lots=1, base_price=50.0)
        reduce_transaction = self._build_transaction(engine, reduce_recommendation, portfolio)
        portfolio = engine.apply_transaction_to_portfolio(portfolio, reduce_transaction)
        self.assertEqual(portfolio.positions["REDUCE"].shares, 1)
        self.assertAlmostEqual(portfolio.margin_used, 80.0)

        equal_after_release = self._recommendation(
            "NEW",
            target_lots=1,
            rank_budget_sequence=1,
            base_price=120.0,
        )
        transaction = self._build_transaction(engine, equal_after_release, portfolio)
        portfolio = engine.apply_transaction_to_portfolio(portfolio, transaction)
        self.assertAlmostEqual(portfolio.margin_used, 200.0)
        self.assertAlmostEqual(portfolio.margin_ratio, 0.20)

    def test_unfilled_limit_locked_exit_does_not_release_margin_before_new_risk_check(self):
        engine = FuturesExecutionEngine(
            {"max_total_margin_ratio": 0.20, "execution": {"default_slippage_ticks": 0}},
            db=None,
        )
        portfolio = self._portfolio(
            positions={
                "EXIT": Position(
                    shares=1,
                    entry_price=190.0,
                    contract_code="EXIT2505",
                    contract_multiplier=1.0,
                    margin_rate=1.0,
                    margin_used=190.0,
                )
            }
        )
        exit_recommendation = self._recommendation("EXIT", current_lots=1, target_lots=0, base_price=190.0)

        with self.assertRaises(ExecutionBlocked) as close_block:
            self._build_transaction(
                engine,
                exit_recommendation,
                portfolio,
                market_rules={
                    "limit_lock": {
                        "blocked": True,
                        "reason": "limit_locked_no_fill",
                    }
                },
            )
        self.assertEqual(close_block.exception.reason, "limit_locked_no_fill")
        self.assertAlmostEqual(portfolio.margin_used, 190.0)
        self.assertEqual(portfolio.positions["EXIT"].shares, 1)

        new_risk = self._recommendation("NEW", target_lots=1, rank_budget_sequence=1, base_price=20.0)
        with self.assertRaises(ExecutionBlocked) as margin_block:
            self._build_transaction(engine, new_risk, portfolio)
        self.assertEqual(margin_block.exception.reason, "margin_insufficient")
        self.assertEqual(set(portfolio.positions), {"EXIT"})

    def test_nonpositive_equity_blocks_strategy_risk_but_forced_risk_close_still_executes(self):
        engine = FuturesExecutionEngine(
            {"max_total_margin_ratio": 0.20, "execution": {"default_slippage_ticks": 0}},
            db=None,
        )
        position = Position(
            shares=1,
            entry_price=100.0,
            contract_code="RISK2505",
            contract_multiplier=1.0,
            margin_rate=1.0,
            margin_used=100.0,
        )
        portfolio = Portfolio(
            id="pf",
            cashflow=-100.0,
            account_equity=0.0,
            cash_available=-100.0,
            margin_used=100.0,
            margin_available=-100.0,
            positions={"RISK": position},
        )
        strategy_open = self._recommendation(
            "NEW",
            target_lots=1,
            rank_budget_sequence=1,
            base_price=1.0,
        )
        with self.assertRaises(ExecutionBlocked) as margin_block:
            self._build_transaction(engine, strategy_open, portfolio)
        self.assertEqual(margin_block.exception.reason, "margin_insufficient")

        forced_close = self._recommendation(
            "RISK",
            current_lots=1,
            target_lots=0,
            source_type="forced_risk",
            action="close_long",
            lots=1,
            base_price=100.0,
        )
        forced_transaction = self._build_transaction(engine, forced_close, portfolio)
        self.assertEqual(forced_transaction.action, FuturesAction.CLOSE_LONG)
        self.assertEqual(forced_transaction.source_type, RecommendationSourceType.FORCED_RISK)

    def test_phase2_summary_uses_final_blocked_execution_result_and_continues_lower_rank(self):
        recommendations = [
            self._recommendation(f"R{rank}", target_lots=1, rank_budget_sequence=rank)
            for rank in (5, 4, 3, 2, 1)
        ]
        scheduled = trader_module._schedule_strategy_recommendations(
            recommendations,
            [item["underlying_code"] for item in recommendations],
        )
        calls = []

        class FakeDB:
            def update_futures_recommendation_status(self, *args, **kwargs):
                return True

        class FakeRouter:
            def resolve_morning_execution_base_price(self, **kwargs):
                return SimpleNamespace(
                    base_price=100.0,
                    base_price_source="open",
                    base_price_date="2025-03-26",
                    open_price=100.0,
                    prev_close_price=99.0,
                    warning_message=None,
                    intraday_audit=None,
                )

        class FakeEngine:
            def execute_recommendation(self, *, recommendation, portfolio, **kwargs):
                ticker = recommendation["underlying_code"]
                calls.append(ticker)
                blocked = ticker == "R4"
                snapshot = recommendation["signal_snapshot"]
                snapshot.setdefault("phase2_execution", {})["status"] = "skipped" if blocked else "executed"
                snapshot["execution_result"] = {
                    "outcome": "skipped" if blocked else "executed",
                    "status": "skipped" if blocked else "executed",
                    "transaction_count": 0 if blocked else 1,
                    "no_trade_reason": "margin_insufficient" if blocked else None,
                    "actual_transactions": [] if blocked else [{"action": "open_long", "lots": 1}],
                }
                return portfolio

        def fake_translate(*, recommendation, **kwargs):
            return FuturesDecision(
                ticker=recommendation["underlying_code"],
                action=FuturesAction.OPEN_LONG,
                lots=1,
                price=100.0,
                settle_price=100.0,
                margin_rate=0.1,
                contract_multiplier=1.0,
                contract_code=recommendation["contract_code"],
                justification="test final execution summary",
            )

        with (
            patch.object(trader_module, "_auditor_verdict_allows_strategy_execution", return_value=True),
            patch.object(trader_module, "intraday_confirmation_enabled", return_value=False),
            patch.object(trader_module, "_translate_pre_open_recommendation_to_order", side_effect=fake_translate),
        ):
            _, summary = trader_module._process_strategy_recommendations(
                cfg={"trading_date": "2025-03-26"},
                db=FakeDB(),
                config_id="cfg",
                router=FakeRouter(),
                execution_engine=FakeEngine(),
                portfolio=self._portfolio(),
                strategy_recommendations=scheduled,
                trading_date_value="2025-03-26",
                runtime_mode="backtest_replay",
                cutoff_datetime=None,
                finalize_untriggered=True,
                loop_iteration=1,
            )

        self.assertEqual(calls, ["R1", "R2", "R3", "R4", "R5"])
        self.assertEqual(summary["executed"], 4)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["no_trade_reasons"]["margin_insufficient"], 1)

    def test_strategy_close_without_valid_intraday_basis_stays_unfilled(self):
        recommendation = self._recommendation(
            "EXIT",
            current_lots=1,
            target_lots=0,
            base_price=100.0,
        )
        portfolio = self._portfolio(
            positions={
                "EXIT": Position(
                    shares=1,
                    entry_price=100.0,
                    contract_code="EXIT2505",
                    contract_multiplier=1.0,
                    margin_rate=0.1,
                    margin_used=10.0,
                )
            }
        )

        class FakeDB:
            def __init__(self):
                self.status_updates = []

            def update_futures_recommendation_status(self, recommendation_id, status, **kwargs):
                self.status_updates.append((recommendation_id, status, kwargs))
                return True

        class FakeRouter:
            def resolve_morning_execution_base_price(self, **kwargs):
                return SimpleNamespace(
                    base_price=100.0,
                    base_price_source="open",
                    base_price_date="2025-03-26",
                    open_price=100.0,
                    prev_close_price=99.0,
                    warning_message=None,
                    intraday_audit=None,
                )

        class MissingIntradayBasis:
            should_execute = False
            decision = "skip"
            reason = "intraday_no_valid_bar"
            base_price = None
            base_datetime = None
            signal_datetime = None

            def to_audit_payload(self):
                return {
                    "decision": self.decision,
                    "reason": self.reason,
                    "base_price": None,
                    "base_datetime": None,
                    "signal_datetime": None,
                    "trigger_checked": True,
                    "trigger_passed": False,
                }

        class FakeEngine:
            def __init__(self):
                self.calls = []

            def execute_recommendation(self, **kwargs):
                self.calls.append(kwargs)
                return kwargs["portfolio"]

        def fake_translate(*, recommendation, **kwargs):
            return FuturesDecision(
                ticker=recommendation["underlying_code"],
                action=FuturesAction.CLOSE_LONG,
                lots=1,
                price=100.0,
                settle_price=100.0,
                margin_rate=0.1,
                contract_multiplier=1.0,
                contract_code=recommendation["contract_code"],
                justification="test strategy close without intraday basis",
            )

        missing_basis = SimpleNamespace(base_price=None)
        selection = MissingIntradayBasis()
        db = FakeDB()
        engine = FakeEngine()
        with (
            patch.object(trader_module, "_auditor_verdict_allows_strategy_execution", return_value=True),
            patch.object(trader_module, "intraday_confirmation_enabled", return_value=True),
            patch.object(trader_module, "_translate_pre_open_recommendation_to_order", side_effect=fake_translate),
            patch.object(
                trader_module,
                "_resolve_phase2_execution_basis",
                return_value=(missing_basis, selection),
            ),
        ):
            final_portfolio, summary = trader_module._process_strategy_recommendations(
                cfg={"trading_date": "2025-03-26"},
                db=db,
                config_id="cfg",
                router=FakeRouter(),
                execution_engine=engine,
                portfolio=portfolio,
                strategy_recommendations=[recommendation],
                trading_date_value="2025-03-26",
                runtime_mode="backtest_replay",
                cutoff_datetime=None,
                finalize_untriggered=True,
                loop_iteration=1,
            )

        self.assertIs(final_portfolio, portfolio)
        self.assertEqual(engine.calls, [])
        self.assertEqual(summary["executed"], 0)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["no_trade_reasons"]["intraday_no_valid_bar"], 1)
        persisted_snapshot = db.status_updates[-1][2]["signal_snapshot"]
        self.assertEqual(persisted_snapshot["execution_result"]["transaction_count"], 0)
        self.assertEqual(persisted_snapshot["execution_result"]["actual_transactions"], [])
        self.assertEqual(
            persisted_snapshot["execution_result"]["no_trade_reason"],
            "intraday_no_valid_bar",
        )
        self.assertIsNone(
            persisted_snapshot["phase2_execution"]["intraday_selection"]["base_price"]
        )
        self.assertEqual(
            persisted_snapshot.get("execution_translation", {}).get("translated_orders"),
            [],
        )
        self.assertNotIn(
            "final_execution_basis",
            persisted_snapshot.get("execution_translation", {}),
        )
        self.assertEqual(db.status_updates[-1][1], RecommendationStatus.SKIPPED)
        self.assertEqual(final_portfolio.positions["EXIT"].shares, 1)

    def test_mixed_strategy_actions_waiting_conditions_do_not_freeze_later_rank(self):
        recommendations = [
            self._recommendation("EXIT", current_lots=1, target_lots=0),
            self._recommendation("REDUCE", current_lots=2, target_lots=1),
            self._recommendation("SCALE", current_lots=1, target_lots=2, rank_budget_sequence=2),
            self._recommendation("WAIT1", target_lots=1, rank_budget_sequence=1, conditional=True),
            self._recommendation("WAIT2", target_lots=1, rank_budget_sequence=3, conditional=True),
            self._recommendation("WAIT3", target_lots=1, rank_budget_sequence=4, conditional=True),
            self._recommendation("TRIGGER", target_lots=1, rank_budget_sequence=5, conditional=True),
        ]
        ticker_order = [item["underlying_code"] for item in recommendations]
        scheduled = trader_module._schedule_strategy_recommendations(recommendations, ticker_order)
        translated = []
        executed = []
        margin_before_execution = []

        portfolio = self._portfolio(
            positions={
                "EXIT": Position(shares=1, contract_code="EXIT2505", margin_used=50.0),
                "REDUCE": Position(shares=2, contract_code="REDUCE2505", margin_used=80.0),
                "SCALE": Position(shares=1, contract_code="SCALE2505", margin_used=20.0),
            }
        )

        class FakeDB:
            def __init__(self):
                self.updates = []

            def update_futures_recommendation_status(self, recommendation_id, status, **kwargs):
                self.updates.append((recommendation_id, status, kwargs))
                return True

        class FakeRouter:
            def resolve_morning_execution_base_price(self, **kwargs):
                return SimpleNamespace(
                    base_price=100.0,
                    base_price_source="open",
                    base_price_date="2025-03-26",
                    open_price=100.0,
                    prev_close_price=99.0,
                    warning_message=None,
                    intraday_audit=None,
                )

        class FakeSelection:
            def __init__(self, should_execute, reason):
                self.should_execute = should_execute
                self.reason = reason
                self.decision = "execute" if should_execute else "wait"
                self.base_datetime = "2025-03-26 09:31:00"
                self.signal_datetime = "2025-03-26 09:30:00"
                self.base_price = 100.0

            def to_audit_payload(self):
                return {
                    "decision": self.decision,
                    "reason": self.reason,
                    "base_price": self.base_price,
                    "base_datetime": self.base_datetime,
                    "signal_datetime": self.signal_datetime,
                    "trigger_checked": True,
                    "trigger_passed": self.should_execute,
                }

        class FakeEngine:
            def execute_recommendation(self, *, recommendation_id, recommendation, portfolio, **kwargs):
                ticker = recommendation["underlying_code"]
                action_value = str(recommendation["action"])
                action_value = action_value.split(".")[-1].lower()
                executed.append(ticker)
                margin_before_execution.append(sum(position.margin_used for position in portfolio.positions.values()))
                position = portfolio.positions.get(ticker)
                if action_value in {"close_long", "close_short"} and position is not None:
                    previous_lots = abs(position.shares)
                    closed_lots = min(previous_lots, int(recommendation["lots"]))
                    released = position.margin_used * (closed_lots / previous_lots)
                    position.margin_used -= released
                    position.shares += -closed_lots if position.shares > 0 else closed_lots
                    portfolio.cashflow += released
                    if position.shares == 0:
                        portfolio.positions.pop(ticker)
                elif action_value in {"open_long", "open_short"}:
                    margin = 10.0
                    if position is None:
                        position = Position(shares=0, contract_code=recommendation["contract_code"], margin_used=0.0)
                        portfolio.positions[ticker] = position
                    position.shares += 1 if action_value == "open_long" else -1
                    position.margin_used += margin
                    portfolio.cashflow -= margin
                portfolio.margin_used = sum(item.margin_used for item in portfolio.positions.values())
                portfolio.account_equity = portfolio.cashflow + portfolio.margin_used
                portfolio.margin_ratio = portfolio.margin_used / portfolio.account_equity
                snapshot = recommendation["signal_snapshot"]
                snapshot.setdefault("phase2_execution", {})["status"] = "executed"
                snapshot["execution_result"] = {
                    "outcome": "executed",
                    "status": "executed",
                    "transaction_count": 1,
                    "actual_transactions": [{"action": action_value, "lots": recommendation["lots"]}],
                }
                return portfolio

        def fake_translate(*, recommendation, portfolio, **kwargs):
            ticker = recommendation["underlying_code"]
            translated.append(ticker)
            contract = recommendation["signal_snapshot"]["final_action_contract"]
            current_lots = int(getattr(portfolio.positions.get(ticker), "shares", 0) or 0)
            target_lots = int(contract["target_lots"])
            intent = phase2_order_intent_from_lots(current_lots, target_lots)
            return FuturesDecision(
                ticker=ticker,
                action=FuturesAction(intent["action"]),
                lots=int(intent["lots"]),
                price=100.0,
                settle_price=100.0,
                margin_rate=0.1,
                contract_multiplier=1.0,
                contract_code=recommendation["contract_code"],
                justification="test phase2 scheduling",
            )

        def fake_intraday_basis(*, recommendation, morning_price_context, **kwargs):
            ticker = recommendation["underlying_code"]
            should_execute = ticker not in {"WAIT1", "WAIT2", "WAIT3"}
            return morning_price_context, FakeSelection(
                should_execute,
                "trigger_met" if should_execute else "intraday_trigger_not_met",
            )

        with (
            patch.object(trader_module, "_auditor_verdict_allows_strategy_execution", return_value=True),
            patch.object(trader_module, "intraday_confirmation_enabled", return_value=True),
            patch.object(trader_module, "_translate_pre_open_recommendation_to_order", side_effect=fake_translate),
            patch.object(trader_module, "_resolve_phase2_execution_basis", side_effect=fake_intraday_basis),
        ):
            final_portfolio, summary = trader_module._process_strategy_recommendations(
                cfg={"trading_date": "2025-03-26"},
                db=FakeDB(),
                config_id="cfg",
                router=FakeRouter(),
                execution_engine=FakeEngine(),
                portfolio=portfolio,
                strategy_recommendations=scheduled,
                trading_date_value="2025-03-26",
                runtime_mode="paper_loop",
                cutoff_datetime=datetime(2025, 3, 26, 10, 0),
                finalize_untriggered=False,
                loop_iteration=1,
            )

        self.assertEqual(
            [item["underlying_code"] for item in scheduled],
            ["EXIT", "REDUCE", "WAIT1", "SCALE", "WAIT2", "WAIT3", "TRIGGER"],
        )
        translated_order = [
            ticker
            for index, ticker in enumerate(translated)
            if index == 0 or ticker != translated[index - 1]
        ]
        self.assertEqual(translated_order, ["EXIT", "REDUCE", "WAIT1", "SCALE", "WAIT2", "WAIT3", "TRIGGER"])
        self.assertEqual(executed, ["EXIT", "REDUCE", "SCALE", "TRIGGER"])
        self.assertEqual(margin_before_execution, [150.0, 100.0, 60.0, 70.0])
        self.assertEqual(summary["checked"], 7)
        self.assertEqual(summary["executed"], 4)
        self.assertEqual(summary["waiting"], 3)
        self.assertEqual(summary["skipped"], 0)
        self.assertAlmostEqual(final_portfolio.margin_used, 80.0)


class CheckDbConsistencyRegressionTest(unittest.TestCase):
    def test_rebuilt_check_db_contains_accounting_columns_and_matching_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "agentquant.db"
            target = Path(temp_dir) / "agentquantcheck.db"
            conn = sqlite3.connect(source)
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE daily_settlement (
                        portfolio_id TEXT,
                        trading_date TEXT,
                        previous_balance REAL,
                        current_balance REAL,
                        previous_account_equity REAL,
                        current_account_equity REAL,
                        cash_available REAL,
                        reserved_margin REAL,
                        previous_margin REAL,
                        current_margin REAL,
                        daily_pnl REAL,
                        commission REAL,
                        margin_ratio REAL,
                        is_warning INTEGER,
                        is_liquidation INTEGER,
                        created_at TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT INTO daily_settlement
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "p1",
                        "2025-03-04",
                        100.0,
                        90.0,
                        120.0,
                        110.0,
                        90.0,
                        20.0,
                        20.0,
                        20.0,
                        -10.0,
                        0.0,
                        0.18,
                        0,
                        0,
                        "now",
                    ),
                )
                cur.execute(
                    """
                    CREATE TABLE trading_day_phase (
                        config_id TEXT,
                        trading_date TEXT,
                        phase TEXT,
                        status TEXT,
                        started_at TEXT,
                        completed_at TEXT,
                        message TEXT
                    )
                    """
                )
                cur.execute(
                    "INSERT INTO trading_day_phase VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("cfg", "2025-03-04", "phase3", "completed", "now", "now", ""),
                )
                conn.commit()
            finally:
                conn.close()

            rebuild_check_db(source, target)
            result = validate_check_db_consistency(source, target)

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["row_counts"]["check_daily_settlement"]["source"], 1)


class PlotConfigRegressionTest(unittest.TestCase):
    def test_plot_config_generates_portfolio_and_each_traded_ticker_chart(self):
        calls = []

        class FakePortfolioPlotter:
            def __init__(self, *args, **kwargs):
                calls.append(("portfolio", args, kwargs))

            def run(self):
                return True

        class FakeSingleFuturePlotter:
            def __init__(self, *args, **kwargs):
                calls.append(("ticker", args, kwargs))

            def run(self):
                return True

        original_portfolio = plot_config.PortfolioCurvePlotter
        original_single = plot_config.SingleFutureCurvePlotter
        original_load_config = plot_config.load_config_exp_name
        original_get_config_id = plot_config.get_config_id
        original_load_tickers = plot_config.load_traded_tickers
        try:
            plot_config.PortfolioCurvePlotter = FakePortfolioPlotter
            plot_config.SingleFutureCurvePlotter = FakeSingleFuturePlotter
            plot_config.load_config_exp_name = lambda config_path: "exp"
            plot_config.get_config_id = lambda db_path, exp_name: "cfg"
            plot_config.load_traded_tickers = lambda db_path, config_id: ["BU", "CF", "J"]

            runner = plot_config.ConfigPlotRunner("src/config/dev.yaml")
            self.assertTrue(runner.run())
            self.assertEqual([call[0] for call in calls], ["portfolio", "ticker", "ticker", "ticker"])
            self.assertEqual([call[1][1] for call in calls[1:]], ["BU", "CF", "J"])
        finally:
            plot_config.PortfolioCurvePlotter = original_portfolio
            plot_config.SingleFutureCurvePlotter = original_single
            plot_config.load_config_exp_name = original_load_config
            plot_config.get_config_id = original_get_config_id
            plot_config.load_traded_tickers = original_load_tickers


class OrderTranslationRegressionTest(unittest.TestCase):
    def test_phase2_entry_audit_has_no_pm_internal_draft_target_field(self):
        audit = phase2_entry_audit(
            target_lots=-1,
            current_lots=0,
            price_context=SimpleNamespace(base_price=3000.0, base_price_source="open"),
        )

        self.assertEqual(audit["entry_action_family"], "open_short")
        self.assertEqual(audit["target_lots_source"], "final_action_contract")
        self.assertNotIn("phase1_target_lots", audit)

    @staticmethod
    def _final_entry_authority(authority_type="real_budget_entry", *, current_evidence=False):
        authority = {
            "authority_type": authority_type,
            "open_action_evidence": authority_type == "real_budget_entry",
            "strong_current_evidence": authority_type == "real_budget_entry",
            "max_allowed_margin_ratio": 0.12 if authority_type == "real_budget_entry" else 0.015,
            "reason_codes": ["test_pm_final_trade_authority"],
        }
        if current_evidence:
            authority.update(
                {
                    "open_action_evidence": True,
                    "technical_confirmation": True,
                    "has_entry_invalidation": True,
                    "has_position_exit_boundary": True,
                }
            )
        return authority

    @staticmethod
    def _strategy_contract(
        ticker: str,
        *,
        current_lots: int = 0,
        target_lots: int,
        final_action: str | None = None,
        authority_type: str = "real_budget_entry",
        tradable_reason: str = "tradable",
        reason_codes: list[str] | None = None,
        current_evidence: bool = True,
    ):
        if final_action is None:
            if target_lots == current_lots:
                final_action = "hold" if current_lots else "wait"
            elif current_lots == 0:
                final_action = "open_real"
            elif target_lots == 0:
                final_action = "exit"
            elif (target_lots > 0) == (current_lots > 0):
                final_action = "scale" if abs(target_lots) > abs(current_lots) else "reduce"
            else:
                final_action = "exit"
        authority = OrderTranslationRegressionTest._final_entry_authority(
            authority_type,
            current_evidence=current_evidence,
        )
        if authority_type in {"real_budget_entry", "exploration_probe", "watchlist_only"}:
            authority["authority_type"] = authority_type
        contract = {
            "contract_version": "agentquant.final_action.v1",
            "ticker": ticker,
            "final_action": final_action,
            "contract_type": "strategy",
            "current_lots": int(current_lots),
            "target_lots": int(target_lots),
            "lots_delta": int(target_lots - current_lots),
            "lots_delta_abs": abs(int(target_lots - current_lots)),
            "reason_codes": list(reason_codes or [tradable_reason]),
            "execution_requirement": (
                "intraday_trigger_required"
                if final_action in {"open_probe", "open_real", "scale"}
                else "position_management_or_wait"
            ),
            "single_source_of_trade_truth": True,
            "candidate_sources_do_not_bypass_contract": True,
        }
        contract.update(authority)
        return contract

    def test_transaction_audit_payload_keeps_execution_audit_without_full_contract_mirror(self):
        snapshot = {
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "RB",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": -4,
                "lots_delta": -4,
                "target_margin_ratio_estimate": 0.041,
                "authority_type": "real_budget_entry",
                "open_action_evidence": True,
                "strong_current_evidence": True,
                "max_allowed_margin_ratio": 0.12,
                "reason_codes": ["positive_candidate_open", "tradeable_candidate"],
                "execution_requirement": "intraday_trigger_required",
                "execution_profile": "vwap_confirmed",
                "trigger_source": "final_contract_execution_fields",
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
                "position_sizing_result": {
                    "target_lots": -4,
                    "capital_allocation_reason": {"rank_is_not_trade_authority": True},
                },
                "opportunity_rank": 1,
                "opportunity_score": 0.81,
                "learning_used": {
                    "alpha_setup_action_values": [
                        {
                            "action_name": "open",
                            "action_preference": "positive_candidate_open",
                            "sample_scope": "exact_real_state",
                            "memory_quality": "exact_real_state",
                            "reward_mean": 1860.0,
                        }
                    ]
                },
            },
            "pm_internal_draft": {
                "execution_fields": {
                    "execution_profile": "vwap_confirmed",
                }
            },
            "execution_translation": {
                "execution_contract": {
                    "execution_profile": "breakout_confirmed",
                }
            },
            "phase2_execution": {
                "pm_plan_validation": {
                    "passed": True,
                    "reason": "final_trade_authority_present",
                    "business_boundary": "strategy_new_entry_requires_pm_final_trade_authority",
                    "authority_consistency": {
                        "reason": "final_contract_authority_consistent",
                    },
                }
            },
        }

        payload = build_audit_payload(snapshot)

        self.assertNotIn("final_action_contract", payload)
        audit = payload["trade_contract_audit"]
        self.assertTrue(audit["single_source_of_trade_truth"])
        self.assertTrue(audit["candidate_sources_do_not_bypass_contract"])
        self.assertEqual(audit["final_action"], "open_real")
        self.assertEqual(audit["authority_type"], "real_budget_entry")
        self.assertEqual(audit["target_lots"], -4)
        self.assertEqual(audit["lots_delta"], -4)
        self.assertEqual(audit["execution_profile"], "vwap_confirmed")
        self.assertTrue(audit["pm_plan_validation_passed"])
        self.assertEqual(
            audit["authority_consistency_reason"],
            "final_contract_authority_consistent",
        )
        self.assertNotIn("selected_action_preferences", audit)
        self.assertNotIn("learning_used", audit)
        self.assertNotIn("position_sizing_result", audit)
        self.assertNotIn("capital_allocation_reason", audit)
        self.assertIn("transaction execution audit only", audit["audit_boundary"])

    def test_execution_artifact_boundary_rejects_pm_explanation_fields_before_persist(self):
        payload = {
            "trade_contract_audit": {
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": -2,
                "lots_delta": -2,
            },
            "phase2_execution": {
                "pm_plan_validation": {
                    "target_lots": -2,
                    "position_sizing_result": {"target_lots": -2},
                }
            },
        }

        with self.assertRaisesRegex(ValueError, "execution_artifact_forbidden_pm_fields"):
            validate_execution_artifact_boundary(payload)

    def test_transaction_audit_does_not_backfill_execution_profile_from_pm_draft(self):
        snapshot = {
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "RB",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": -2,
                "lots_delta": -2,
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            "pm_internal_draft": {
                "execution_fields": {
                    "execution_profile": "breakout",
                    "trigger_source": "stale_pm_internal_draft",
                }
            },
            "execution_translation": {
                "execution_contract": {
                    "execution_profile": "vwap_confirmed",
                    "trigger_source": "translation_copy",
                }
            },
            "phase2_execution": {
                "execution_contract": {
                    "execution_profile": "pullback",
                    "trigger_source": "phase2_copy",
                },
            },
        }

        payload = build_audit_payload(snapshot)

        self.assertIsNone(payload["trade_contract_audit"]["execution_profile"])

    def test_audit_target_lots_come_from_final_contract_not_pm_draft(self):
        recommendation = {
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": {
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "RB",
                    "final_action": "open_real",
                    "current_lots": 0,
                    "target_lots": -1,
                    "lots_delta": -1,
                },
                "pm_internal_draft": {
                    "target_lots": -8,
                    "target_position_ratio": -0.04,
                },
            },
        }

        self.assertEqual(infer_target_lots(recommendation), -1)

    def test_trader_execution_contract_reads_only_final_contract_not_stale_pm_internal_draft(self):
        snapshot = {
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "RB",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": -2,
                "lots_delta": -2,
                "execution_profile": "vwap_confirmed",
                "trigger_source": "final_contract_execution_fields",
            },
            "pm_internal_draft": {
                "execution_fields": {
                    "contract_version": "agentquant.execution_contract.v1",
                    "execution_profile": "breakout",
                    "trigger_source": "stale_pm_internal_draft",
                }
            },
        }

        execution_contract = _execution_contract_from_snapshot(snapshot)

        self.assertEqual(execution_contract["execution_profile"], "vwap_confirmed")
        self.assertEqual(execution_contract["trigger_source"], "final_contract_execution_fields")

        missing_contract_plan = {
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "RB",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": -2,
                "lots_delta": -2,
            },
            "pm_internal_draft": {
                "execution_fields": {
                    "contract_version": "agentquant.execution_contract.v1",
                    "execution_profile": "breakout",
                    "trigger_source": "stale_pm_internal_draft",
                }
            },
        }

        self.assertEqual(_execution_contract_from_snapshot(missing_contract_plan), {})

    def test_strategy_recommendation_without_pm_plan_cannot_execute_raw_action_lots(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": {},
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        self.assertIn(
            "missing_final_action_contract",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        self.assertFalse(snapshot["phase2_execution"]["pm_plan_validation"]["passed"])
        self.assertEqual(
            snapshot["phase2_execution"]["pm_plan_validation"]["business_boundary"],
            "strategy_recommendation_requires_final_action_contract",
        )

    def test_strategy_new_entry_without_final_authority_is_not_translated_to_open(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": {
                "pm_internal_draft": {
                    "target_position_ratio": -0.04,
                    "target_lots": -8,
                    "lots_delta_abs": 8,
                    "reason_codes": "tradable",
                },
                "final_action_contract": self._strategy_contract(
                    "RB",
                    target_lots=-8,
                    authority_type="watchlist_only",
                ),
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(recommendation["signal_snapshot"])

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        self.assertIn(
            "final_contract_authority_not_met",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertFalse(validation["passed"])
        self.assertEqual(validation["original_target_lots"], -8)
        self.assertEqual(validation["target_lots_after_validation"], 0)

    def test_strategy_recommendation_without_final_action_contract_cannot_use_pm_draft_lots(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "pm_internal_draft": {
                "target_position_ratio": -0.04,
                "target_lots": -8,
                "lots_delta_abs": 8,
                "reason_codes": "tradable",
            },
        }
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        self.assertIn(
            "missing_final_action_contract",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertFalse(validation["passed"])
        self.assertEqual(validation["reason"], "missing_final_action_contract")
        self.assertEqual(validation["target_lots_after_validation"], 0)

    def test_strategy_recommendation_with_non_strategy_contract_cannot_use_raw_action_lots(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "contract_type": "operational_rollover",
                "ticker": "RB",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": -8,
                "lots_delta": -8,
            },
        }
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        self.assertIn(
            "unsupported_final_action_contract_type",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertFalse(validation["passed"])
        self.assertEqual(validation["reason"], "unsupported_final_action_contract_type")
        self.assertEqual(validation["target_lots_after_validation"], 0)

    def test_unknown_source_type_cannot_execute_raw_action_lots(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": "legacy_manual",
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": {
                "phase2_execution": {
                    "raw_action_fixture": "must_not_execute",
                }
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(recommendation["signal_snapshot"])

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        self.assertIn(
            "unsupported_raw_action_source_type",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertFalse(validation["passed"])
        self.assertEqual(validation["reason"], "unsupported_raw_action_source_type")
        self.assertEqual(validation["source_type"], "legacy_manual")
        self.assertEqual(validation["target_lots_after_validation"], 0)

    def test_strategy_new_entry_uses_final_contract_authority_fields(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "final_action_contract": self._strategy_contract(
                "RB",
                target_lots=-8,
            ),
        }
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 8)
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertTrue(validation["passed"])
        self.assertEqual(validation["reason"], "final_action_contract_present")

    def test_strategy_hold_contract_cannot_be_retranslated_from_stale_pre_open_target(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=200000.0,
            positions={
                "BU": Position(
                    shares=-10,
                    value=420000.0,
                    margin_used=200000.0,
                    contract_code="bu2506",
                    entry_date="2025-03-07",
                    entry_price=4200.0,
                )
            },
        )
        signal_snapshot = {
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "BU",
                "final_action": "hold",
                "contract_type": "strategy",
                "current_lots": -10,
                "target_lots": -10,
                "lots_delta": 0,
                "lots_delta_abs": 0,
                "reason_codes": "position_matched",
                "authority_type": "not_applicable",
                "reason_codes": ["position_matched"],
                "execution_requirement": "position_management_or_wait",
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            "pm_internal_draft": {
                "target_position_ratio": -0.035,
                "target_lots": -9,
                "lots_delta_abs": 1,
                "reason_codes": "position_matched",
            },
        }
        recommendation = {
            "underlying_code": "BU",
            "contract_code": "bu2506",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.HOLD.value,
            "lots": 0,
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=4200.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        translation = snapshot.get("execution_translation", {})
        self.assertIn("final_action_contract_source", translation)
        self.assertEqual(translation["phase2_order_plan"]["target_lots"], -10)
        self.assertEqual(
            translation["phase2_order_plan"]["consistency_diagnostics"]["expected"]["action"],
            "hold",
        )
        self.assertNotIn("stale_pre_open_target_used", translation)

    def test_strategy_final_contract_executes_without_pm_internal_draft(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "final_action_contract": self._strategy_contract(
                "RB",
                target_lots=-8,
                final_action="open_real",
            ),
        }
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 8)
        translation = snapshot.get("execution_translation", {})
        self.assertEqual(translation["final_action_contract_source"]["source"], "final_action_contract")
        self.assertNotIn("pm_internal_draft", snapshot)

    def test_strategy_final_contract_ignores_stale_pm_internal_draft_target_lots(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "final_action_contract": self._strategy_contract(
                "RB",
                target_lots=-1,
                final_action="open_real",
            ),
            "pm_internal_draft": {
                "target_position_ratio": -0.04,
                "target_lots": -8,
                "lots_delta_abs": 8,
                "reason_codes": "stale_pm_draft",
            },
        }
        recommendation = {
            "underlying_code": "RB",
            "contract_code": "rb2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 8,
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3500.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 1)
        translation = snapshot.get("execution_translation", {})
        self.assertEqual(translation["final_action_contract_source"]["target_lots"], -1)
        self.assertEqual(translation["phase2_order_plan"]["target_lots"], -1)

    def test_phase2_preserves_phase1_tradable_one_lot_probe_when_ratio_floors_to_zero(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "PB",
            "contract_code": "pb2501",
            "signal_snapshot": {
                "pm_internal_draft": {
                    "target_position_ratio": 0.00001,
                    "target_lots": 1,
                    "lots_delta_abs": 1,
                    "reason_codes": "tradable",
                },
                "final_action_contract": self._strategy_contract(
                    "PB",
                    target_lots=1,
                    final_action="open_probe",
                    authority_type="exploration_probe",
                    current_evidence=True,
                ),
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=19000.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_LONG)
        self.assertEqual(decision.lots, 1)
        translation = snapshot.get("execution_translation", {})
        self.assertEqual(
            translation["final_action_contract_source"]["source"],
            "final_action_contract",
        )
        self.assertEqual(translation["phase2_order_plan"]["target_lots"], 1)

    def test_phase2_bare_exploration_probe_without_current_evidence_is_not_translated_to_open(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "SR",
            "contract_code": "sr2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_LONG.value,
            "lots": 8,
            "signal_snapshot": {
                "pm_internal_draft": {
                    "target_position_ratio": 0.02,
                    "target_lots": 8,
                    "lots_delta_abs": 8,
                    "reason_codes": "tradable",
                },
                "final_action_contract": self._strategy_contract(
                    "SR",
                    target_lots=8,
                    final_action="open_probe",
                    authority_type="exploration_probe",
                    current_evidence=False,
                ),
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=5900.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertEqual(decision.lots, 0)
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertFalse(validation["passed"])
        self.assertEqual(validation["reason"], "final_contract_authority_not_met")
        self.assertEqual(validation["target_lots_after_validation"], 0)

    def test_phase2_conditional_probe_contract_can_reach_intraday_trigger_check(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        contract = self._strategy_contract(
            "SR",
            target_lots=1,
            final_action="open_probe",
            authority_type="exploration_probe",
            current_evidence=False,
            reason_codes=[
                "pm_watch_for_trigger_probe_cap",
                "real_probe_qualification_not_met",
                "conditional_trigger_authority",
            ],
        )
        contract.update(
            {
                "conditional_trigger_authority": True,
                "requires_intraday_confirmation": True,
                "can_execute_without_intraday_trigger": False,
                "watch_for_trigger_block": False,
                "execution_profile": "breakout",
                "entry_trigger": "wait for price to break above 5900 after open",
                "invalidation": "below 5840",
            }
        )
        recommendation = {
            "underlying_code": "SR",
            "contract_code": "sr2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_LONG.value,
            "lots": 1,
            "signal_snapshot": {
                "pm_internal_draft": {
                    "target_position_ratio": 0.02,
                    "target_lots": 8,
                    "lots_delta_abs": 8,
                    "reason_codes": "legacy_draft_must_not_expand_contract",
                },
                "final_action_contract": contract,
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=5900.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_LONG)
        self.assertEqual(decision.lots, 1)
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertTrue(validation["passed"])
        self.assertEqual(validation["target_lots"], 1)
        self.assertEqual(
            snapshot["execution_translation"]["final_action_contract_source"]["target_lots"],
            1,
        )

    def test_phase2_deferred_conditional_probe_records_intraday_before_order_safety_gate(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        contract = self._strategy_contract(
            "SR",
            target_lots=1,
            final_action="open_probe",
            authority_type="exploration_probe",
            current_evidence=False,
            reason_codes=[
                "pm_watch_for_trigger_probe_cap",
                "real_probe_qualification_not_met",
                "conditional_trigger_authority",
            ],
        )
        contract.update(
            {
                "conditional_trigger_authority": True,
                "requires_intraday_confirmation": True,
                "can_execute_without_intraday_trigger": False,
                "watch_for_trigger_block": True,
                "execution_profile": "breakout",
                "entry_trigger": "wait for price to break above 5900 after open",
                "invalidation": "below 5840",
            }
        )
        recommendation = {
            "underlying_code": "SR",
            "contract_code": "sr2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_LONG.value,
            "lots": 1,
            "signal_snapshot": {"final_action_contract": contract},
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }

        pre_trigger_snapshot = {}
        pre_trigger_decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=5900.0),
            snapshot=pre_trigger_snapshot,
            defer_conditional_entry_authority=True,
        )

        self.assertEqual(pre_trigger_decision.action, FuturesAction.OPEN_LONG)
        self.assertEqual(pre_trigger_decision.lots, 1)
        self.assertEqual(
            pre_trigger_snapshot["phase2_execution"]["entry_authority_gate"]["status"],
            "deferred_until_intraday_trigger",
        )

        post_trigger_snapshot = {}
        post_trigger_decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=5900.0),
            snapshot=post_trigger_snapshot,
        )

        self.assertEqual(post_trigger_decision.action, FuturesAction.HOLD)
        self.assertEqual(post_trigger_decision.lots, 0)
        self.assertEqual(
            post_trigger_snapshot["phase2_execution"]["pm_plan_validation"]["reason"],
            "final_contract_authority_not_met",
        )

    def test_phase2_artifacts_do_not_mirror_pm_explanation_fields(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        contract = self._strategy_contract(
            "SR",
            target_lots=1,
            final_action="open_probe",
            authority_type="exploration_probe",
            current_evidence=True,
            reason_codes=["conditional_trigger_authority"],
        )
        contract.update(
            {
                "requires_intraday_confirmation": True,
                "can_execute_without_intraday_trigger": False,
                "execution_profile": "breakout",
                "entry_trigger": "wait for price to break above 5900 after open",
                "opportunity_rank": 1,
                "opportunity_score": 0.82,
                "opportunity_score_components": {"positive_learning": 0.2},
                "position_sizing_result": {
                    "target_lots": 1,
                    "capital_allocation_reason": {"rank_is_not_trade_authority": True},
                },
                "capital_allocation_reason": {"selected": True},
                "learning_used": {"alpha_setup_action_values": [{"action_preference": "positive_candidate_open"}]},
                "learning_adjustment_summary": {"positive_learning": True},
            }
        )
        recommendation = {
            "id": "rec-sr",
            "underlying_code": "SR",
            "contract_code": "sr2505",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_LONG.value,
            "lots": 1,
            "signal_snapshot": {"final_action_contract": contract},
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=5900.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_LONG)
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        setup_learning = _setup_execution_learning_context(snapshot)
        self.assertTrue(validation["passed"])
        self.assertNotIn("final_action_contract", validation)
        self.assertNotIn("final_action_contract", setup_learning)
        forbidden = {
            "position_sizing_result",
            "capital_allocation_reason",
            "opportunity_rank",
            "opportunity_score",
            "opportunity_score_components",
            "learning_used",
            "learning_adjustment_summary",
        }
        self.assertFalse(forbidden.intersection(validation.get("final_contract_execution_fields", {}).keys()))
        self.assertFalse(forbidden.intersection(setup_learning.get("final_contract_execution_fields", {}).keys()))

    def test_phase2_exploration_probe_with_current_evidence_can_translate_to_open(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "PB",
            "contract_code": "pb2501",
            "source_type": RecommendationSourceType.STRATEGY.value,
            "action": RecommendationAction.OPEN_SHORT.value,
            "lots": 1,
            "signal_snapshot": {
                "pm_internal_draft": {
                    "target_position_ratio": -0.005,
                    "target_lots": -1,
                    "lots_delta_abs": 1,
                    "reason_codes": "tradable",
                },
                "final_action_contract": self._strategy_contract(
                    "PB",
                    target_lots=-1,
                    final_action="open_probe",
                    authority_type="exploration_probe",
                    current_evidence=True,
                    reason_codes=["market_confirmation_quality_gate"],
                ),
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=19000.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 1)
        validation = snapshot["phase2_execution"]["pm_plan_validation"]
        self.assertTrue(validation["passed"])
        self.assertEqual(validation["reason"], "final_action_contract_present")

    def test_phase2_does_not_expand_phase1_target_lots_when_open_price_changes(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5162600.45,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "TA",
            "contract_code": "ta601",
            "signal_snapshot": {
                "pm_internal_draft": {
                    "target_position_ratio": -0.12,
                    "target_lots": -26,
                },
                "final_action_contract": self._strategy_contract(
                    "TA",
                    target_lots=-26,
                ),
            },
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.75,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=4566.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 26)
        self.assertEqual(
            snapshot["execution_translation"]["final_action_contract_source"]["target_lots"],
            -26,
        )

    def test_phase2_preserves_entry_invalidation_without_planned_position_observation(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5162600.45,
            margin_used=0.0,
            positions={},
        )
        final_contract = self._strategy_contract("TA", target_lots=-26)
        final_contract.update(
            {
                "horizon_class": "short",
                "expected_horizon_days": 2,
                "invalidation_level": 4400.0,
                "position_invalidation_level": 4700.0,
            }
        )
        signal_snapshot = {
            "pm_internal_draft": {
                "target_position_ratio": -0.12,
                "target_lots": -26,
            },
            "final_action_contract": final_contract,
        }
        recommendation = {
            "underlying_code": "TA",
            "contract_code": "ta601",
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.75,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=4566.0),
            snapshot=snapshot,
        )

        translation = snapshot.get("execution_translation", {})
        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 26)
        observation = snapshot["phase2_execution"]["contract_execution_observation"]
        self.assertNotIn("signal_invalidation_observed", observation)
        self.assertIn("never treats planned target_lots", observation["business_boundary"])
        self.assertNotIn("signal_invalidation_level", translation.get("rewrite_reasons", []))
        lifecycle = translation["phase2_order_plan"]["signal_lifecycle"]
        self.assertNotIn("invalidation_level", lifecycle)
        self.assertEqual(lifecycle["position_invalidation_level"], 4700.0)

    def test_phase2_ignores_opposing_invalidation_level_for_short_target(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "technical": {
                "signal": "Bearish",
                "horizon_class": "short",
                "expected_horizon_days": 2,
                "entry_trigger": "Break below prior low with volume expansion",
            },
            "fundamental": {
                "signal": "Bullish",
                "horizon_class": "medium",
                "expected_horizon_days": 5,
                "invalidation_level": 2620.0,
            },
            "pm_internal_draft": {
                "target_position_ratio": -0.04,
                "target_lots": -7,
            },
            "final_action_contract": self._strategy_contract(
                "M",
                target_lots=-7,
            ),
        }
        recommendation = {
            "underlying_code": "M",
            "contract_code": "m2505",
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=2660.0),
            snapshot=snapshot,
        )

        translation = snapshot.get("execution_translation", {})
        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertEqual(decision.lots, 7)
        self.assertNotIn("signal_invalidation_level", translation.get("rewrite_reasons", []))
        self.assertNotIn("invalidation_level", translation.get("signal_lifecycle", {}))
        self.assertNotIn(
            "invalidation_level",
            translation.get("phase2_order_plan", {}).get("signal_lifecycle", {}),
        )
        self.assertNotIn("signal_lifecycle_direction_filter", translation)

    def test_phase2_ignores_opposing_invalidation_level_for_long_target(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "technical": {
                "signal": "Bullish",
                "horizon_class": "short",
                "expected_horizon_days": 2,
                "entry_trigger": "Break above prior high with volume expansion",
            },
            "fundamental": {
                "signal": "Bearish",
                "horizon_class": "medium",
                "expected_horizon_days": 5,
                "invalidation_level": 3820.0,
            },
            "pm_internal_draft": {
                "target_position_ratio": 0.04,
                "target_lots": 5,
            },
            "final_action_contract": self._strategy_contract(
                "BU",
                target_lots=5,
            ),
        }
        recommendation = {
            "underlying_code": "BU",
            "contract_code": "bu2506",
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.20,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=3739.0),
            snapshot=snapshot,
        )

        translation = snapshot.get("execution_translation", {})
        self.assertEqual(decision.action, FuturesAction.OPEN_LONG)
        self.assertEqual(decision.lots, 5)
        self.assertNotIn("signal_invalidation_level", translation.get("rewrite_reasons", []))
        self.assertNotIn("invalidation_level", translation.get("signal_lifecycle", {}))
        self.assertNotIn(
            "invalidation_level",
            translation.get("phase2_order_plan", {}).get("signal_lifecycle", {}),
        )
        self.assertNotIn("signal_lifecycle_direction_filter", translation)

    def test_phase2_no_trade_does_not_create_planned_position_invalidation_observation(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5162600.45,
            margin_used=0.0,
            positions={},
        )
        signal_snapshot = {
            "technical": {
                "signal": "Bearish",
                "horizon_class": "short",
                "expected_horizon_days": 2,
                "invalidation_level": 4400.0,
            },
            "pm_internal_draft": {
                "target_position_ratio": -0.12,
                "target_lots": -26,
            },
            "final_action_contract": self._strategy_contract(
                "TA",
                target_lots=0,
                final_action="wait",
                authority_type="watchlist_only",
                current_evidence=False,
            ),
        }
        recommendation = {
            "underlying_code": "TA",
            "contract_code": "ta601",
            "signal_snapshot": signal_snapshot,
        }
        config = {
            "cashflow": 5000000,
            "max_total_margin_ratio": 0.75,
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = dict(signal_snapshot)

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=4566.0),
            snapshot=snapshot,
        )

        translation = snapshot.get("execution_translation", {})
        self.assertEqual(decision.action, FuturesAction.HOLD)
        self.assertIsNone(infer_no_trade_reason(snapshot))
        observation = snapshot["phase2_execution"]["contract_execution_observation"]
        self.assertNotIn("signal_invalidation_observed", observation)
        self.assertIn("never treats planned target_lots", observation["business_boundary"])
        self.assertNotIn("position_matched", translation.get("rewrite_reasons", []))

    def test_time_stop_does_not_flatten_supported_same_direction_hold(self):
        current_position = SimpleNamespace(entry_date="2025-02-10", entry_price=3000.0)

        result = evaluate_exit_policy(
            ticker="M",
            current_price=3020.0,
            current_lots=10,
            target_lots=12,
            lifecycle={"template_state": "protected", "setup_type": "trend_follow"},
            current_position=current_position,
            trading_date="2025-02-20",
            config={
                "execution": {
                    "exit_policy": {
                        "enabled": True,
                        "defaults": {"trend_time_stop_days": 5, "probe_time_stop_days": 2},
                    }
                }
            },
        )

        self.assertFalse(result["exit_required"])
        self.assertEqual(result["target_lots"], 12)
        self.assertTrue(result["same_direction_supported"])

    def test_atr_profit_trailing_uses_best_completed_settlement(self):
        long_protection = resolve_atr_protection(
            current_lots=3,
            current_price=108.0,
            entry_price=100.0,
            atr_distance=10.0,
            atr_multiplier=1.8,
            best_prior_settlement_price=120.0,
        )
        self.assertTrue(long_protection["activated"])
        self.assertEqual(long_protection["initial_stop_level"], 82.0)
        self.assertEqual(long_protection["trailing_stop_level"], 110.0)
        self.assertTrue(long_protection["breached"])
        tighter_long_protection = resolve_atr_protection(
            current_lots=3,
            current_price=112.0,
            entry_price=100.0,
            atr_distance=10.0,
            atr_multiplier=1.8,
            best_prior_settlement_price=125.0,
        )
        self.assertGreater(
            tighter_long_protection["effective_stop_level"],
            long_protection["effective_stop_level"],
        )

        short_protection = resolve_atr_protection(
            current_lots=-3,
            current_price=92.0,
            entry_price=100.0,
            atr_distance=10.0,
            atr_multiplier=1.8,
            best_prior_settlement_price=80.0,
        )
        self.assertTrue(short_protection["activated"])
        self.assertEqual(short_protection["initial_stop_level"], 118.0)
        self.assertEqual(short_protection["trailing_stop_level"], 90.0)
        self.assertTrue(short_protection["breached"])

    def test_atr_profit_trailing_waits_for_one_atr_profit(self):
        protection = resolve_atr_protection(
            current_lots=3,
            current_price=90.0,
            entry_price=100.0,
            atr_distance=10.0,
            atr_multiplier=1.8,
            best_prior_settlement_price=109.0,
        )
        self.assertFalse(protection["activated"])
        self.assertEqual(protection["effective_stop_level"], 82.0)
        self.assertFalse(protection["breached"])

    def test_time_stop_flattens_unvalidated_probe_without_same_direction_support(self):
        current_position = SimpleNamespace(entry_date="2025-02-10", entry_price=3000.0)

        result = evaluate_exit_policy(
            ticker="M",
            current_price=2990.0,
            current_lots=10,
            target_lots=5,
            lifecycle={"template_state": "watchlist", "setup_type": "probe"},
            current_position=current_position,
            trading_date="2025-02-14",
            config={
                "execution": {
                    "exit_policy": {
                        "enabled": True,
                        "defaults": {"trend_time_stop_days": 5, "probe_time_stop_days": 2},
                    }
                }
            },
        )

        self.assertTrue(result["exit_required"])
        self.assertEqual(result["target_lots"], 0)
        self.assertEqual(result["reason"], "time_stop")
        self.assertFalse(result["same_direction_supported"])

    def test_time_stop_uses_opening_fac_authority_instead_of_setup_name(self):
        current_position = SimpleNamespace(entry_date="2025-02-10", entry_price=3000.0)
        config = {
            "execution": {
                "exit_policy": {
                    "enabled": True,
                    "defaults": {"trend_time_stop_days": 5, "probe_time_stop_days": 2},
                }
            }
        }
        probe = evaluate_exit_policy(
            ticker="M",
            current_price=2990.0,
            current_lots=10,
            target_lots=5,
            lifecycle={
                "opening_authority_type": "exploration_probe",
                "template_state": "protected",
                "setup_type": "trend_breakout_setup",
            },
            current_position=current_position,
            trading_date="2025-02-14",
            config=config,
        )
        real = evaluate_exit_policy(
            ticker="M",
            current_price=2990.0,
            current_lots=10,
            target_lots=5,
            lifecycle={
                "opening_authority_type": "real_budget_entry",
                "template_state": "watchlist",
                "setup_type": "probe_like_name",
            },
            current_position=current_position,
            trading_date="2025-02-14",
            config=config,
        )

        self.assertTrue(probe["exit_required"])
        self.assertTrue(probe["is_probe"])
        self.assertFalse(real["exit_required"])
        self.assertFalse(real["is_probe"])

    def test_phase2_honors_dynamic_opportunity_budget_from_phase1(self):
        portfolio = Portfolio(
            id="p1",
            cashflow=5000000.0,
            margin_used=0.0,
            positions={},
        )
        recommendation = {
            "underlying_code": "TA",
            "contract_code": "ta601",
            "signal_snapshot": {
                "technical": {
                    "signal": "Bearish",
                    "invalidation_level": 4800.0,
                },
                "pm_internal_draft": {
                    "target_position_ratio": -1.62,
                    "target_margin_ratio_estimate": 0.162,
                    "target_lots": -162,
                    "strategy_controls": {
                        "diagnostics": {
                            "capital_utilization_target": {
                                "target_mode": "alpha_release_boost",
                                "high_quality_memory": True,
                                "dynamic_allocation_tier": "validated_with_stop",
                                "dynamic_opportunity_margin_ratio_budget": 0.162,
                                "dynamic_opportunity_margin_ratio_cap": 0.162,
                                "stop_protected": True,
                            },
                            "net_exposure_control": {
                                "cap_mode": "alpha_release",
                                "max_net_exposure": 2.0,
                            }
                        }
                    },
                },
                "final_action_contract": self._strategy_contract(
                    "TA",
                    target_lots=-162,
                ),
            },
        }
        config = {
            "cashflow": 5000000,
            "capital_utilization_control": {"max_margin_ratio_after_scaling": 0.20},
            "risk_control": {
                "warning_ratio": 0.70,
                "danger_ratio": 0.50,
                "emergency_ratio": 0.30,
                "max_single_position_ratio": {"safe": 0.12},
            },
        }
        snapshot = {}

        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=config,
            morning_price_context=SimpleNamespace(base_price=4566.0),
            snapshot=snapshot,
        )

        self.assertEqual(decision.action, FuturesAction.OPEN_SHORT)
        self.assertGreaterEqual(decision.lots, 39)
        self.assertNotIn(
            "single_position_cap",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        self.assertNotIn(
            "base_sizing_anchor_cap",
            snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
        )
        self.assertEqual(
            snapshot["execution_translation"]["final_action_contract_source"]["target_lots"],
            -162,
        )

    def test_rollover_reconciliation_records_close_only_execution_type(self):
        rollover = {
            "underlying_code": "C",
            "from_contract": "c2511",
            "to_contract": "c2601",
        }
        strategy = {
            "action": "open_long",
            "lots": 0,
            "signal_snapshot": {"pm_internal_draft": {"target_lots": 0}},
        }

        adjusted = _reconcile_rollover_with_strategy_target(
            rollover_recommendation=rollover,
            strategy_recommendation=strategy,
            current_lots=-2,
            config={"rollover": {"mode": "reconcile_with_strategy"}},
        )

        self.assertEqual(adjusted["rollover_execution_type"], "close_only_rollover")
        self.assertEqual(adjusted["rollover_close_lots"], 2)
        self.assertEqual(adjusted["rollover_open_lots"], 0)

    def test_rollover_reconciliation_preserves_strategy_same_direction_target(self):
        rollover = {
            "underlying_code": "C",
            "from_contract": "c2511",
            "to_contract": "c2601",
        }
        strategy = {
            "signal_snapshot": {
                "final_action_contract": {
                    "contract_type": "strategy",
                    "current_lots": -2,
                    "target_lots": -3,
                    "lots_delta": -1,
                    "final_action": "scale",
                }
            }
        }

        adjusted = _reconcile_rollover_with_strategy_target(
            rollover_recommendation=rollover,
            strategy_recommendation=strategy,
            current_lots=-2,
            config={"rollover": {"mode": "reconcile_with_strategy"}},
        )

        self.assertEqual(adjusted["rollover_execution_type"], "full_rollover")
        self.assertEqual(adjusted["rollover_close_lots"], 2)
        self.assertEqual(adjusted["rollover_open_lots"], 3)
        self.assertEqual(adjusted["rollover_strategy_target_lots"], -3)

    def test_forced_risk_orders_execute_before_strategy_phase2_processing(self):
        calls = []

        class FakeExecutionEngine:
            def scan_and_create_intraday_forced_risk_orders(self, *, config_id, trading_date, portfolio, cutoff_datetime=None):
                calls.append(("scan", config_id, trading_date, portfolio, cutoff_datetime))
                return ["risk-rec"]

            def execute_pending_forced_risk_orders(self, *, config_id, trading_date, portfolio, execution_phase):
                calls.append(("execute", config_id, trading_date, portfolio, execution_phase))
                return {"after": "forced_risk"}

        result = _execute_pending_forced_risk_before_strategy(
            execution_engine=FakeExecutionEngine(),
            config_id="cfg",
            trading_date="2025-03-04",
            portfolio={"before": "risk"},
        )

        self.assertEqual(result, {"after": "forced_risk"})
        self.assertEqual(
            calls,
            [
                ("scan", "cfg", "2025-03-04", {"before": "risk"}, None),
                ("execute", "cfg", "2025-03-04", {"before": "risk"}, TradingPhase.PHASE2),
            ],
        )

    def test_intraday_margin_call_creates_forced_risk_close_order(self):
        saved = []

        class FakeDb:
            def get_futures_recommendations_by_effective_date(self, **kwargs):
                return []

            def save_futures_recommendation(self, recommendation):
                saved.append(recommendation.model_dump())
                return "forced-risk-1"

        class FakeRouter:
            def get_china_futures_minute_bars(self, **kwargs):
                return [
                    {"datetime": "2025-03-04 10:00:00", "open": 5.0, "close": 5.0, "volume": 10},
                ]

            def get_futures_contract_quote_on_date(self, contract_code, trading_date):
                return SimpleNamespace(close_price=5.0, settle_price=5.0, open_price=5.0)

        engine = FuturesExecutionEngine(
            {
                "execution": {
                    "forced_risk": {
                        "enabled": True,
                        "intraday_margin_call_ratio": 0.80,
                        "post_reduce_target_margin_ratio": 0.50,
                    },
                    "dynamic_margin": {"enabled": False},
                }
            },
            FakeDb(),
        )
        engine.router = FakeRouter()
        portfolio = Portfolio(
            id="pf-risk",
            cashflow=1000.0,
            margin_used=1000.0,
            positions={
                "RB": Position(
                    shares=10,
                    entry_price=100.0,
                    contract_code="rb2505",
                    contract_multiplier=10,
                    margin_rate=0.1,
                    margin_used=1000.0,
                )
            },
        )

        created = engine.scan_and_create_intraday_forced_risk_orders(
            config_id="cfg",
            trading_date="2025-03-04",
            portfolio=portfolio,
        )

        self.assertEqual(created, ["forced-risk-1"])
        self.assertEqual(saved[0]["source_type"], "forced_risk")
        self.assertEqual(saved[0]["action"], "close_long")
        self.assertEqual(saved[0]["effective_trade_date"], "2025-03-04")
        self.assertGreater(saved[0]["lots"], 0)
        self.assertNotIn("final_action_contract", saved[0].get("signal_snapshot") or {})
        self.assertEqual(
            saved[0]["audit_payload"]["strategy_learning_boundary"],
            "excluded_from_strategy_action_value",
        )

    def test_intraday_margin_below_call_does_not_create_forced_risk_order(self):
        saved = []

        class FakeDb:
            def get_futures_recommendations_by_effective_date(self, **kwargs):
                return []

            def save_futures_recommendation(self, recommendation):
                saved.append(recommendation.model_dump())
                return "forced-risk-1"

        class FakeRouter:
            def get_china_futures_minute_bars(self, **kwargs):
                return [
                    {"datetime": "2025-03-04 10:00:00", "open": 100.0, "close": 100.0, "volume": 10},
                ]

            def get_futures_contract_quote_on_date(self, contract_code, trading_date):
                return SimpleNamespace(close_price=100.0, settle_price=100.0, open_price=100.0)

        engine = FuturesExecutionEngine(
            {
                "execution": {
                    "forced_risk": {
                        "enabled": True,
                        "intraday_margin_call_ratio": 0.80,
                        "post_reduce_target_margin_ratio": 0.50,
                    },
                    "dynamic_margin": {"enabled": False},
                }
            },
            FakeDb(),
        )
        engine.router = FakeRouter()
        portfolio = Portfolio(
            id="pf-safe",
            cashflow=100000.0,
            margin_used=10000.0,
            positions={
                "RB": Position(
                    shares=10,
                    entry_price=100.0,
                    contract_code="rb2505",
                    contract_multiplier=10,
                    margin_rate=0.1,
                    margin_used=10000.0,
                )
            },
        )

        created = engine.scan_and_create_intraday_forced_risk_orders(
            config_id="cfg",
            trading_date="2025-03-04",
            portfolio=portfolio,
        )

        self.assertEqual(created, [])
        self.assertEqual(saved, [])


class EvaluationRegressionTest(unittest.TestCase):
    def _create_minimal_futures_evaluation_db(self, db_path: str):
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE portfolio (
                id TEXT,
                config_id TEXT,
                updated_at TEXT,
                trading_date TEXT,
                cashflow REAL,
                total_assets REAL,
                positions TEXT,
                margin_used REAL,
                leverage REAL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE daily_settlement (
                portfolio_id TEXT,
                trading_date TEXT,
                previous_balance REAL,
                current_balance REAL,
                previous_margin REAL,
                current_margin REAL,
                daily_pnl REAL,
                commission REAL,
                margin_ratio REAL,
                is_warning INTEGER,
                is_liquidation INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE futures_transactions (
                config_id TEXT,
                trading_date TEXT,
                created_at TEXT,
                ticker TEXT,
                contract_code TEXT,
                action TEXT,
                lots INTEGER,
                execution_price REAL,
                price REAL,
                contract_multiplier REAL,
                commission REAL,
                settle_price REAL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE ticker_daily_pnl (
                portfolio_id TEXT,
                trading_date TEXT,
                ticker TEXT,
                daily_pnl REAL,
                commission REAL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE capital_deployment_state (
                config_id TEXT,
                trading_date TEXT,
                capital_allocation_tier TEXT,
                reason_bucket TEXT
            )
            """
        )
        cur.execute(
            "CREATE TABLE strategy_memory (config_id TEXT, memory_state TEXT, net_pnl REAL)"
        )
        cur.execute(
            "CREATE TABLE config_learning_overlay (config_id TEXT, active INTEGER)"
        )
        cur.execute(
            "CREATE TABLE causal_review_candidate (config_id TEXT)"
        )
        cur.execute(
            "CREATE TABLE futures_recommendation (config_id TEXT, signal_snapshot TEXT)"
        )

        positions = {"M": {"shares": 1, "value": 100000.0, "contract_code": "m2601"}}
        portfolios = [
            ("p1", "cfg", "2025-01-02T15:30:00", "2025-01-02", 4999900.0, 5009900.0, json.dumps(positions), 10000.0, 1.0),
            ("p2", "cfg", "2025-01-03T15:30:00", "2025-01-03", 5006500.0, 5016500.0, json.dumps(positions), 10000.0, 1.0),
        ]
        cur.executemany("INSERT INTO portfolio VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", portfolios)
        settlements = [
            ("p1", "2025-01-02", 5000000.0, 4996625.68, 0.0, 3311.0, -50.0, 13.32, 0.0007, 0, 0),
            ("p2", "2025-01-03", 4996625.68, 4993229.93, 3311.0, 13301.0, 6660.0, 66.75, 0.0027, 0, 0),
        ]
        cur.executemany("INSERT INTO daily_settlement VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", settlements)
        transactions = [
            ("cfg", "2025-01-02", "2025-01-02T09:00:00", "M", "m2601", "open_long", 1, 100.0, 100.0, 10.0, 2.0, 100.0),
            ("cfg", "2025-01-03", "2025-01-03T09:00:00", "RB", "rb2601", "open_short", 2, 200.0, 200.0, 10.0, 4.0, 200.0),
        ]
        cur.executemany("INSERT INTO futures_transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", transactions)
        ticker_pnl = [
            ("p1", "2025-01-02", "M", -50.0, 2.0),
            ("p2", "2025-01-03", "M", 100.0, 2.0),
            ("p2", "2025-01-03", "RB", -20.0, 4.0),
        ]
        cur.executemany("INSERT INTO ticker_daily_pnl VALUES (?, ?, ?, ?, ?)", ticker_pnl)
        conn.commit()
        return conn

    def test_futures_transaction_win_rate_uses_completed_round_trips(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE futures_transactions (
                    config_id TEXT,
                    trading_date TEXT,
                    created_at TEXT,
                    ticker TEXT,
                    contract_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    execution_price REAL,
                    price REAL,
                    contract_multiplier REAL,
                    commission REAL
                )
                """
            )
            rows = [
                ("cfg", "2025-10-13", "2025-10-13T09:00:00", "M", "m2601", "open_long", 2, 100.0, 100.0, 10.0, 2.0),
                ("cfg", "2025-10-14", "2025-10-14T09:00:00", "M", "m2601", "close_long", 1, 110.0, 110.0, 10.0, 1.0),
                ("cfg", "2025-10-15", "2025-10-15T09:00:00", "M", "m2601", "close_long", 1, 90.0, 90.0, 10.0, 1.0),
                ("cfg", "2025-10-16", "2025-10-16T09:00:00", "RB", "rb2601", "open_short", 1, 200.0, 200.0, 10.0, 2.0),
                ("cfg", "2025-10-17", "2025-10-17T09:00:00", "RB", "rb2601", "close_short", 1, 180.0, 180.0, 10.0, 2.0),
            ]
            cur.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            conn.close()

            metrics = calculate_futures_transaction_win_rate("cfg", db_path)
            self.assertEqual(metrics["total_trades"], 3)
            self.assertEqual(metrics["winning_trades"], 2)
            self.assertEqual(metrics["losing_trades"], 1)
            self.assertEqual(metrics["flat_trades"], 0)
            self.assertAlmostEqual(metrics["win_rate"], 2 / 3)
            self.assertAlmostEqual(metrics["realized_trade_pnl"], 192.0)
        finally:
            os.remove(db_path)

    def test_subwindow_transaction_win_rate_classifies_inherited_closes(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE futures_transactions (
                    config_id TEXT,
                    trading_date TEXT,
                    created_at TEXT,
                    ticker TEXT,
                    contract_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    execution_price REAL,
                    price REAL,
                    contract_multiplier REAL,
                    commission REAL
                )
                """
            )
            rows = [
                ("cfg", "2025-10-10", "2025-10-10T09:00:00", "M", "m2601", "open_long", 2, 100.0, 100.0, 10.0, 2.0),
                ("cfg", "2025-10-14", "2025-10-14T09:00:00", "M", "m2601", "close_long", 2, 90.0, 90.0, 10.0, 2.0),
            ]
            cur.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            conn.close()

            metrics = calculate_futures_transaction_win_rate("cfg", db_path, start_date="2025-10-13")
            self.assertEqual(metrics["total_trades"], 1)
            self.assertEqual(metrics["losing_trades"], 1)
            self.assertEqual(metrics["unmatched_close_lots"], 0)
            self.assertEqual(metrics["inherited_close_lots"], 2)
        finally:
            os.remove(db_path)

    def test_average_leverage_uses_account_equity_not_total_assets(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE portfolio (
                    id TEXT,
                    config_id TEXT,
                    trading_date TEXT,
                    cashflow REAL,
                    total_assets REAL,
                    positions TEXT,
                    margin_used REAL,
                    leverage REAL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE daily_settlement (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    daily_pnl REAL,
                    margin_ratio REAL,
                    is_warning INTEGER,
                    is_liquidation INTEGER,
                    commission REAL,
                    previous_margin REAL,
                    current_margin REAL,
                    previous_balance REAL,
                    current_balance REAL
                )
                """
            )
            positions = {
                "M": {"value": 500.0, "shares": 1},
                "RB": {"value": 300.0, "shares": -1},
            }
            cur.execute(
                """
                INSERT INTO portfolio
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("p1", "cfg", "2025-10-13", 1000.0, 1800.0, json.dumps(positions), 200.0, 1.0),
            )
            cur.execute(
                """
                INSERT INTO daily_settlement
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("p1", "2025-10-13", 50.0, 0.10, 0, 0, 3.0, 100.0, 200.0, 1050.0, 997.0),
            )
            conn.commit()
            conn.close()

            metrics = calculate_futures_metrics("cfg", db_path)
            self.assertAlmostEqual(metrics["avg_leverage"], 800.0 / 1200.0)
        finally:
            os.remove(db_path)

    def test_futures_evaluation_uses_account_equity_for_risk_metrics(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = self._create_minimal_futures_evaluation_db(db_path)
            conn.close()

            metrics = evaluate_config("cfg", db_path)
            self.assertIsNotNone(metrics)
            self.assertEqual(metrics["risk_metric_status"], "ok")
            self.assertEqual(metrics["account_equity_return_sample_count"], 2)
            self.assertGreater(metrics["volatility"], 0.0)
            self.assertNotEqual(metrics["sharpe_ratio"], 0.0)
            self.assertGreater(metrics["account_equity_max_drawdown"], 0.0)
            self.assertEqual(metrics["margin_return_sample_count"], 1)
            self.assertEqual(metrics["margin_return_volatility"], 0.0)
        finally:
            os.remove(db_path)

    def test_no_completed_round_trips_warns_without_low_win_rate(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = self._create_minimal_futures_evaluation_db(db_path)
            conn.close()

            metrics = evaluate_config("cfg", db_path)
            warning_types = {warning["type"] for warning in metrics["warnings"]}
            self.assertFalse(metrics["win_rate_available"])
            self.assertIn("no_completed_round_trips", warning_types)
            self.assertNotIn("low_win_rate", warning_types)
        finally:
            os.remove(db_path)

    def test_commission_rate_uses_turnover_not_initial_capital(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = self._create_minimal_futures_evaluation_db(db_path)
            conn.close()

            metrics = evaluate_config("cfg", db_path)
            self.assertAlmostEqual(metrics["total_turnover_notional"], 5000.0)
            self.assertAlmostEqual(metrics["commission_rate"], 80.07 / 5000.0)
            self.assertAlmostEqual(metrics["capital_commission_rate"], 80.07 / 5000000.0)
        finally:
            os.remove(db_path)

    def test_futures_evaluation_reports_strategy_quality_metrics(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = self._create_minimal_futures_evaluation_db(db_path)
            conn.executemany(
                "INSERT INTO futures_transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("cfg", "2025-01-03", "2025-01-03T10:00:00", "M", "m2601", "close_long", 1, 110.0, 110.0, 10.0, 2.0, 110.0),
                    ("cfg", "2025-01-03", "2025-01-03T11:00:00", "RB", "rb2601", "close_short", 2, 201.0, 201.0, 10.0, 4.0, 201.0),
                ],
            )
            conn.commit()
            conn.close()

            metrics = evaluate_config("cfg", db_path)
            self.assertGreater(metrics["calmar_ratio"], 0.0)
            self.assertGreater(metrics["profit_factor"], 0.0)
            self.assertGreater(metrics["trade_expectancy"], 0.0)
            self.assertEqual(metrics["max_consecutive_losing_days"], 1)
            self.assertEqual(metrics["profitable_ticker_count"], 1)
            self.assertEqual(metrics["losing_ticker_count"], 1)
            self.assertEqual(metrics["top_profit_ticker"], "M")
            self.assertEqual(metrics["worst_loss_ticker"], "RB")
            expected_margin_returns = [
                (-50.0 - 13.32) / ((0.0 + 3311.0) / 2.0),
                (6660.0 - 66.75) / ((3311.0 + 13301.0) / 2.0),
            ]
            self.assertAlmostEqual(
                metrics["return_on_avg_margin"],
                sum(expected_margin_returns) / len(expected_margin_returns),
            )
            self.assertAlmostEqual(
                metrics["commission_drag_ratio"],
                80.07 / (abs(-50.0) + abs(6660.0)),
            )
        finally:
            os.remove(db_path)

    def test_evaluate_config_window_includes_position_opened_before_window(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = self._create_minimal_futures_evaluation_db(db_path)
            conn.execute(
                "INSERT INTO futures_transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "cfg",
                    "2025-01-03",
                    "2025-01-03T10:00:00",
                    "M",
                    "m2601",
                    "close_long",
                    1,
                    110.0,
                    110.0,
                    10.0,
                    2.0,
                    110.0,
                ),
            )
            conn.commit()
            conn.close()

            metrics = evaluate_config(
                "cfg",
                db_path,
                start_date="2025-01-03",
                end_date="2025-01-03",
            )

            self.assertEqual(metrics["trading_date_start"], "2025-01-03T00:00:00")
            self.assertEqual(metrics["trading_date_end"], "2025-01-03T00:00:00")
            self.assertEqual(metrics["total_trades"], 1)
            self.assertEqual(metrics["winning_trades"], 1)
            self.assertEqual(metrics["inherited_close_lots"], 1)
            self.assertEqual(metrics["unmatched_close_lots"], 0)
            self.assertAlmostEqual(metrics["realized_trade_pnl"], 96.0)
        finally:
            os.remove(db_path)

    def test_evaluation_helper_upgrades_config_outcome_without_dropping_rows(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE config_outcome (
                    id VARCHAR(36) PRIMARY KEY,
                    config_id VARCHAR(36) NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    trading_date_start TIMESTAMP,
                    trading_date_end TIMESTAMP,
                    total_return DECIMAL(10,4),
                    annualized_return DECIMAL(10,4),
                    sharpe_ratio DECIMAL(10,4),
                    max_drawdown DECIMAL(10,4),
                    win_rate DECIMAL(10,4),
                    total_trades INTEGER,
                    winning_trades INTEGER,
                    losing_trades INTEGER,
                    avg_return_per_trade DECIMAL(10,4),
                    volatility DECIMAL(10,4),
                    initial_capital DECIMAL(15,2),
                    final_capital DECIMAL(15,2),
                    peak_margin_ratio DECIMAL(10,4) DEFAULT 0,
                    avg_margin_ratio DECIMAL(10,4) DEFAULT 0,
                    warning_days INTEGER DEFAULT 0,
                    liquidation_events INTEGER DEFAULT 0,
                    total_commission DECIMAL(15,2) DEFAULT 0,
                    avg_daily_pnl DECIMAL(15,2) DEFAULT 0,
                    total_settlement_pnl DECIMAL(15,2) DEFAULT 0,
                    max_margin_usage DECIMAL(10,4) DEFAULT 0,
                    long_trades INTEGER DEFAULT 0,
                    short_trades INTEGER DEFAULT 0,
                    active_long_positions INTEGER DEFAULT 0,
                    active_short_positions INTEGER DEFAULT 0,
                    commission_rate DECIMAL(10,4) DEFAULT 0,
                    avg_leverage DECIMAL(10,2) DEFAULT 1.0,
                    margin_call_count INTEGER DEFAULT 0
                )
                """
            )
            cur.execute(
                "INSERT INTO config_outcome (id, config_id, total_return) VALUES (?, ?, ?)",
                ("old", "cfg", 0.01),
            )
            conn.commit()
            conn.close()

            EvaluationHelper(db_path)

            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM config_outcome WHERE id='old'")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("PRAGMA table_info(config_outcome)")
            columns = {row[1] for row in cur.fetchall()}
            self.assertIn("profit_factor", columns)
            self.assertIn("learning_context_budget_rows", columns)
            conn.close()
        finally:
            os.remove(db_path)

    def test_capital_deployment_under_deployed_counts_by_tier(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = self._create_minimal_futures_evaluation_db(db_path)
            cur = conn.cursor()
            cur.executemany(
                "INSERT INTO capital_deployment_state VALUES (?, ?, ?, ?)",
                [
                    ("cfg", "2025-01-02", "under_deployed", "neutral_signal_no_trade"),
                    ("cfg", "2025-01-03", "under_deployed", "intraday_trigger_not_met"),
                    ("cfg", "2025-01-04", "normal", "alpha_capacity_limited"),
                ],
            )
            conn.commit()
            conn.close()

            metrics = calculate_optimization_acceptance_metrics(
                "cfg", db_path, start_date="2025-01-02", end_date="2025-01-03"
            )
            self.assertEqual(metrics["under_deployed_days"], 2)
            self.assertEqual(metrics["system_under_deployed_days"], 2)
            self.assertEqual(metrics["non_alpha_under_deployed_days"], 2)
            self.assertEqual(metrics["under_deployed_reason_counts"]["neutral_signal_no_trade"], 1)
            self.assertEqual(metrics["under_deployed_reason_counts"]["intraday_trigger_not_met"], 1)
            self.assertEqual(metrics["alpha_capacity_limited_days"], 0)
        finally:
            os.remove(db_path)


if __name__ == "__main__":
    unittest.main()
