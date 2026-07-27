from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


from agents.decision_team.auditor import audit_futures_recommendation
from agents.decision_team.portfolio_manager import (
    RiskLevel,
    _build_execution_contract_fields,
    _build_final_action_contract,
    _build_pm_decision_context,
    _enrich_final_authority_with_analyst_evidence,
    _scorecard_probe_seed,
    _side_opportunity_state_summary,
)
from agents.execution_team.trader import (
    _auditor_verdict_allows_strategy_execution,
    _mark_intraday_non_execution,
    _resolve_phase2_execution_basis,
    _translate_pre_open_recommendation_to_order,
)
from graph.constants import Signal
from graph.schema import (
    AnalystSignal,
    FuturesAction,
    Portfolio,
    Position,
    RecommendationStatus,
)
from tests.contract_test_fixtures import build_test_aec, build_test_signal
from tools.agent_tools.analysis.analyst_quality import apply_trade_research_contract
from tools.agent_tools.decision.pm_signal_fusion import (
    build_opportunity_scorecard,
    build_scc_market_confirmation,
)
from tools.agent_tools.decision.pm_full_market_capital_deployment import (
    _capital_rank_eligible,
    _ensure_final_rank_score_fields,
)
from tools.agent_tools.decision.pm_lifecycle_action_port import classify_lifecycle_action_port
from tools.agent_tools.decision.pm_ticker_side_selection import select_ticker_side
from tools.agent_tools.execution.trader_intraday_execution import select_intraday_execution
from tools.common.contracts import sanitize_execution_contract
from tools.common.execution_trigger_semantics import (
    canonical_entry_invalidation_condition,
    canonical_entry_trigger,
)
from tools.common.signal_evidence_collection import (
    build_pm_evidence_signals_from_scc,
    build_scc_data_quality_summary,
    build_signal_collection_contract,
    validate_action_evidence_contract,
)


def _analyst_signal(
    *,
    analyst: str = "technical",
    signal: Signal = Signal.BULLISH,
    opportunity_state: str = "tradeable_candidate",
    trigger_valid: bool = True,
    current_trigger_confirmed: bool = True,
    invalidation_present: bool = True,
    entry_trigger: str | None = None,
    entry_timing_signal: str = "breakout",
) -> AnalystSignal:
    side = "long" if signal == Signal.BULLISH else "short" if signal == Signal.BEARISH else "flat"
    formal_timing = (
        entry_timing_signal
        if analyst == "technical"
        else "event_immediate"
        if analyst == "commodity_news"
        and opportunity_state in {"probe_candidate", "tradeable_candidate"}
        else ""
    )
    canonical_trigger = {
        ("breakout", "long"): "15分钟收盘价向上突破开盘区间上沿且高于VWAP",
        ("breakout", "short"): "15分钟收盘价向下突破开盘区间下沿且低于VWAP",
        ("pullback", "long"): "15分钟先向上扩张，随后回踩开盘区间上沿或VWAP，最终收盘重新严格站上被回踩边界",
        ("pullback", "short"): "15分钟先向下扩张，随后回抽开盘区间下沿或VWAP，最终收盘重新严格跌回被回抽边界下方",
        ("vwap_confirmed", "long"): "15分钟收盘价不低于VWAP",
        ("vwap_confirmed", "short"): "15分钟收盘价不高于VWAP",
        ("event_immediate", "long"): "当前事件已满足即时执行边界，使用首根合法1分钟线执行",
        ("event_immediate", "short"): "当前事件已满足即时执行边界，使用首根合法1分钟线执行",
    }.get((formal_timing, side), "")
    resolved_entry_trigger = entry_trigger if entry_trigger is not None else canonical_trigger
    invalidation_condition = (
        canonical_entry_invalidation_condition(formal_timing, side)
        if invalidation_present
        else ""
    )
    aec = build_test_aec(
        analyst,
        signal=signal.value,
        side=side,
        confidence=0.82,
        opportunity_state=opportunity_state,
        setup_type="trend_breakout",
        setup_quality_ok=True,
        trigger_valid=trigger_valid,
        current_trigger_confirmed=current_trigger_confirmed,
        invalidation_present=invalidation_present,
        entry_trigger=resolved_entry_trigger,
        invalidation_condition=invalidation_condition,
        extra={
            "evidence_role": (
                "entry_timing"
                if analyst == "technical"
                else "direction_context"
                if analyst == "fundamental"
                else "event_catalyst"
            ),
            "entry_timing_signal": (
                formal_timing
            ),
        },
    )
    return AnalystSignal(
        agent_name=analyst,
        signal=signal,
        confidence=0.82,
        opportunity_state=opportunity_state,
        setup_type="trend_breakout",
        setup_quality_ok=True,
        business_quality_score=0.82,
        setup_quality_score=0.82,
        evidence_quality="high",
        entry_trigger=resolved_entry_trigger,
        entry_timing_signal=formal_timing,
        trigger_valid=trigger_valid,
        invalidation_present=invalidation_present,
        invalidation_level=96.0 if invalidation_present else None,
        position_invalidation_level=94.0 if invalidation_present else None,
        evidence_role="entry_timing" if analyst == "technical" else "event_catalyst",
        event_type="supply_disruption" if analyst == "commodity_news" else "none",
        metadata={"action_evidence_contract": aec},
    )


def _entry_authority(*, conditional: bool = False, allowed: bool = True) -> dict:
    return {
        "authority_type": "exploration_probe" if allowed else "watchlist_only",
        "authority_decision": "allow" if allowed else "watchlist_only",
        "open_action_evidence": bool(allowed and not conditional),
        "strong_current_evidence": bool(allowed and not conditional),
        "conditional_trigger_authority": bool(conditional and allowed),
        "requires_intraday_confirmation": bool(conditional and allowed),
        "can_execute_without_intraday_trigger": False if conditional else None,
        "max_allowed_margin_ratio": 0.015,
        "reason_codes": ["conditional_trigger_authority"] if conditional else ["test_pm_final_trade_authority"],
    }


def _entry_execution_boundary(side: str) -> dict:
    return {
        "invalidation": canonical_entry_invalidation_condition("breakout", side),
        "invalidation_level": 95.0 if side == "long" else 105.0,
        "valid_until": "2025-03-26 15:00:00",
    }


def _execution_bars() -> list[dict]:
    return [
        {
            "datetime": "2025-03-26 09:30:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10,
        },
        {
            "datetime": "2025-03-26 09:31:00",
            "open": 100.2,
            "high": 101.0,
            "low": 99.5,
            "close": 100.1,
            "volume": 10,
        },
        {
            "datetime": "2025-03-26 10:01:00",
            "open": 99.0,
            "high": 100.0,
            "low": 98.0,
            "close": 99.0,
            "volume": 10,
        },
    ]


def _non_triggering_signal_bars() -> list[dict]:
    return [
        {
            "datetime": "2025-03-26 10:00:00",
            "open": 99.0,
            "high": 100.0,
            "low": 98.0,
            "close": 99.0,
            "volume": 10,
        }
    ]


def _strategy_contract(*, current_lots: int, target_lots: int, final_action: str) -> dict:
    increases_risk = current_lots == 0 or abs(target_lots) > abs(current_lots)
    side = "long" if target_lots > 0 else "short"
    authority_type = "scale" if increases_risk and current_lots else "real_budget_entry"
    if final_action in {"reduce", "exit"}:
        authority_type = final_action
    return {
        "contract_version": "agentquant.final_action.v1",
        "contract_type": "strategy",
        "ticker": "BU",
        "contract_code": "BU2506",
        "final_action": final_action,
        "current_lots": int(current_lots),
        "target_lots": int(target_lots),
        "lots_delta": int(target_lots - current_lots),
        "lots_delta_abs": abs(int(target_lots - current_lots)),
        "authority_type": authority_type,
        "authority_decision": "allow",
        "open_action_evidence": bool(increases_risk),
        "strong_current_evidence": bool(increases_risk),
        "tradeable_state": bool(increases_risk),
        "conditional_trigger_authority": False,
        "requires_intraday_confirmation": False,
        "can_execute_without_intraday_trigger": True,
        "execution_profile": "breakout",
        "trigger_source": "technical_breakout",
        "entry_trigger": (
            "15分钟收盘价向上突破开盘区间上沿且高于VWAP"
            if side == "long"
            else "15分钟收盘价向下突破开盘区间下沿且低于VWAP"
        ),
        "invalidation": canonical_entry_invalidation_condition("breakout", side),
        "invalidation_condition": canonical_entry_invalidation_condition("breakout", side),
        "invalidation_level": 3500.0 if side == "long" else 4500.0,
        "position_invalidation_level": 3400.0 if side == "long" else 4600.0,
        "valid_until": "2025-03-26 15:00:00",
        "target_margin_ratio_estimate": 0.06 if increases_risk else 0.02,
        "max_allowed_margin_ratio": 0.12,
        "reason_codes": ["test_pm_final_trade_authority"],
        "execution_requirement": "direct_execution" if increases_risk else "position_management_or_wait",
        "single_source_of_trade_truth": True,
        "candidate_sources_do_not_bypass_contract": True,
    }


def _recommendation(contract: dict) -> dict:
    delta = int(contract["lots_delta"])
    action = "open_long" if delta > 0 else "open_short" if delta < 0 else "hold"
    if int(contract["current_lots"]) > 0 and delta < 0:
        action = "close_long"
    if int(contract["current_lots"]) < 0 and delta > 0:
        action = "close_short"
    return {
        "id": "rec-BU",
        "config_id": "cfg",
        "underlying_code": "BU",
        "contract_code": "BU2506",
        "source_type": "strategy",
        "trading_date": "2025-03-26",
        "effective_trade_date": "2025-03-26",
        "action": action,
        "lots": abs(delta),
        "signal_snapshot": {"final_action_contract": contract},
        "audit_payload": {
            "producer": "auditor",
            "audit_status": "approved",
            "audit_verdict": "approve",
            "audit_reason_codes": [],
        },
    }


def _portfolio(*, bu_lots: int, bu_margin: float, other_margin: float, total_margin: float) -> Portfolio:
    return Portfolio(
        id="portfolio-prev-t",
        cashflow=1_000_000.0 - total_margin,
        margin_used=total_margin,
        positions={
            "BU": Position(
                shares=bu_lots,
                value=abs(bu_lots) * 40_000.0,
                margin_used=bu_margin,
                contract_code="BU2506",
                entry_date="2025-03-25",
                entry_price=4_000.0,
            ),
            "M": Position(
                shares=1,
                value=other_margin * 10.0,
                margin_used=other_margin,
                contract_code="M2509",
                entry_date="2025-03-25",
                entry_price=3_000.0,
            ),
        },
    )


def _execution_config() -> dict:
    return {
        "cashflow": 1_000_000.0,
        "max_total_margin_ratio": 0.20,
        "risk_control": {
            "warning_ratio": 0.70,
            "danger_ratio": 0.50,
            "emergency_ratio": 0.30,
            "max_single_position_ratio": {"safe": 0.12, "warning": 0.08, "danger": 0.04},
        },
    }


class DirectAndConditionalExecutionPathTest(unittest.TestCase):
    def test_pm_keeps_preopen_technical_entry_conditional_even_when_aec_is_confirmed(self):
        for profile in ("breakout", "pullback"):
            with self.subTest(profile=profile):
                plan = _build_pm_decision_context(
                    ticker="BU",
                    target_lots=2,
                    current_price=100.0,
                    position_ratio=0.02,
                    risk_level=RiskLevel.SAFE,
                    long_scores={"confidence": 0.82},
                    short_scores={"confidence": 0.10},
                    margin_rate=0.10,
                    current_lots=0,
                    analyst_signals=[
                        _analyst_signal(entry_timing_signal=profile)
                    ],
                    final_entry_authority=_entry_authority(),
                    trading_date="2025-03-26",
                    recommendation_intent={"action": "open_long"},
                    control_reasons=["test_pm_final_trade_authority"],
                )

                self.assertFalse(plan["can_execute_without_intraday_trigger"])
                self.assertTrue(plan["requires_intraday_confirmation"])

    def test_pm_requires_current_confirmation_invalidation_and_funds_for_direct_execution(self):
        for signal in (
            _analyst_signal(current_trigger_confirmed=False),
            _analyst_signal(invalidation_present=False),
        ):
            with self.subTest(signal=signal.opportunity_state):
                with self.assertRaisesRegex(ValueError, "pm_execution_evidence_not_found"):
                    _build_pm_decision_context(
                        ticker="BU",
                        target_lots=2,
                        current_price=100.0,
                        position_ratio=0.02,
                        risk_level=RiskLevel.SAFE,
                        long_scores={"confidence": 0.82},
                        short_scores={"confidence": 0.10},
                        margin_rate=0.10,
                        current_lots=0,
                        analyst_signals=[signal],
                        final_entry_authority=_entry_authority(),
                        trading_date="2025-03-26",
                        recommendation_intent={"action": "open_long"},
                        control_reasons=[],
                    )

        plan = _build_pm_decision_context(
                    ticker="BU",
                    target_lots=2,
                    current_price=100.0,
                    position_ratio=0.02,
                    risk_level=RiskLevel.SAFE,
                    long_scores={"confidence": 0.82},
                    short_scores={"confidence": 0.10},
                    margin_rate=0.10,
                    current_lots=0,
                    analyst_signals=[_analyst_signal()],
                    final_entry_authority=_entry_authority(allowed=False),
                    trading_date="2025-03-26",
                    recommendation_intent={"action": "open_long"},
                    control_reasons=[],
                )
        self.assertFalse(plan["can_execute_without_intraday_trigger"])

    def test_watch_still_requires_15m_trigger_and_missing_1m_never_fabricates_fill(self):
        watch = select_intraday_execution(
            signal_bars=_non_triggering_signal_bars(),
            execution_bars=_execution_bars(),
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={
                "execution_contract": {
                    **_entry_execution_boundary("long"),
                    "execution_profile": "breakout",
                    "trigger_source": "technical_breakout",
                    "entry_trigger": "15分钟收盘价向上突破开盘区间上沿且高于VWAP",
                    "can_execute_without_intraday_trigger": False,
                    "requires_intraday_confirmation": True,
                }
            },
        )
        no_bar = select_intraday_execution(
            signal_bars=[],
            execution_bars=[],
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={
                "execution_contract": {
                    **_entry_execution_boundary("long"),
                    "execution_profile": "breakout",
                    "trigger_source": "technical_breakout",
                    "entry_trigger": "15分钟收盘价向上突破开盘区间上沿且高于VWAP",
                    "can_execute_without_intraday_trigger": True,
                    "requires_intraday_confirmation": False,
                }
            },
        )

        self.assertFalse(watch.should_execute)
        self.assertEqual(watch.reason, "intraday_trigger_not_met")
        self.assertTrue(watch.to_audit_payload()["trigger_checked"])
        self.assertFalse(no_bar.should_execute)
        self.assertEqual(no_bar.reason, "intraday_no_valid_bar")

    def test_breakout_requires_strict_boundary_crossing(self):
        execution_bars = _execution_bars()
        cases = (
            (
                "open_long",
                101.0,
                "15分钟收盘价向上突破开盘区间上沿且高于VWAP",
            ),
            (
                "open_short",
                99.0,
                "15分钟收盘价向下突破开盘区间下沿且低于VWAP",
            ),
        )
        for action, boundary_close, trigger in cases:
            with self.subTest(action=action):
                result = select_intraday_execution(
                    signal_bars=[
                        {
                            "datetime": "2025-03-26 10:00:00",
                            "open": boundary_close,
                            "high": boundary_close,
                            "low": boundary_close,
                            "close": boundary_close,
                            "volume": 10,
                        }
                    ],
                    execution_bars=execution_bars,
                    action=action,
                    config={"opening_range_minutes": 1, "min_execution_volume": 1},
                    decision_context={
                        "execution_contract": {
                            **_entry_execution_boundary("long" if action == "open_long" else "short"),
                            "execution_profile": "breakout",
                            "trigger_source": "technical_breakout",
                            "entry_trigger": trigger,
                            "can_execute_without_intraday_trigger": False,
                            "requires_intraday_confirmation": True,
                        }
                    },
                )
                self.assertFalse(result.should_execute)
                self.assertEqual(result.reason, "intraday_trigger_not_met")

    def test_stronger_breakout_waits_for_follow_through_before_execution(self):
        contract = {
            **_entry_execution_boundary("long"),
            "execution_profile": "breakout",
            "trigger_source": "technical_breakout",
            "entry_trigger": canonical_entry_trigger("breakout", "long"),
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "trigger_confirmation_adjustment": "stronger_confirmation_required",
        }
        result = select_intraday_execution(
            signal_bars=[
                {"datetime": "2025-03-26 10:00:00", "open": 101.0, "high": 102.5, "low": 100.8, "close": 102.0, "volume": 10},
                {"datetime": "2025-03-26 10:15:00", "open": 102.0, "high": 102.8, "low": 101.5, "close": 102.2, "volume": 10},
            ],
            execution_bars=[
                {"datetime": "2025-03-26 09:30:00", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10},
                {"datetime": "2025-03-26 10:01:00", "open": 102.0, "high": 102.2, "low": 101.8, "close": 102.0, "volume": 10},
                {"datetime": "2025-03-26 10:16:00", "open": 102.3, "high": 102.5, "low": 102.0, "close": 102.2, "volume": 10},
            ],
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={"execution_contract": contract},
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.base_datetime, "2025-03-26 10:16:00")
        self.assertEqual(result.signal_datetime, "2025-03-26 10:15:00")
        self.assertEqual(
            result.features["execution_contract"]["trigger_confirmation_adjustment"],
            "stronger_confirmation_required",
        )
        self.assertEqual(
            sanitize_execution_contract(contract)["trigger_confirmation_adjustment"],
            "stronger_confirmation_required",
        )

    def test_stronger_vwap_waits_for_next_completed_bar(self):
        result = select_intraday_execution(
            signal_bars=[
                {"datetime": "2025-03-26 10:00:00", "open": 100.5, "high": 101.2, "low": 100.2, "close": 101.0, "volume": 10},
                {"datetime": "2025-03-26 10:15:00", "open": 101.0, "high": 101.5, "low": 100.8, "close": 101.3, "volume": 10},
            ],
            execution_bars=[
                {"datetime": "2025-03-26 09:30:00", "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 10},
                {"datetime": "2025-03-26 10:01:00", "open": 101.0, "high": 101.2, "low": 100.9, "close": 101.0, "volume": 10},
                {"datetime": "2025-03-26 10:16:00", "open": 101.4, "high": 101.5, "low": 101.2, "close": 101.3, "volume": 10},
            ],
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={
                "execution_contract": {
                    **_entry_execution_boundary("long"),
                    "invalidation": canonical_entry_invalidation_condition(
                        "vwap_confirmed", "long"
                    ),
                    "execution_profile": "vwap_confirmed",
                    "trigger_source": "technical_pullback",
                    "entry_trigger": canonical_entry_trigger("vwap_confirmed", "long"),
                    "requires_intraday_confirmation": True,
                    "can_execute_without_intraday_trigger": False,
                    "trigger_confirmation_adjustment": "stronger_confirmation_required",
                }
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.base_datetime, "2025-03-26 10:16:00")
        self.assertEqual(result.signal_datetime, "2025-03-26 10:15:00")

    def test_failed_stronger_breakout_can_only_execute_after_a_fresh_trigger_and_confirmation(self):
        result = select_intraday_execution(
            signal_bars=[
                {"datetime": "2025-03-26 10:00:00", "open": 101.0, "high": 102.5, "low": 100.8, "close": 102.0, "volume": 10},
                {"datetime": "2025-03-26 10:15:00", "open": 102.0, "high": 102.1, "low": 99.5, "close": 100.0, "volume": 10},
                {"datetime": "2025-03-26 10:30:00", "open": 101.0, "high": 103.5, "low": 100.8, "close": 103.0, "volume": 10},
                {"datetime": "2025-03-26 10:45:00", "open": 103.0, "high": 103.6, "low": 102.8, "close": 103.2, "volume": 10},
            ],
            execution_bars=[
                {"datetime": "2025-03-26 09:30:00", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10},
                {"datetime": "2025-03-26 10:01:00", "open": 102.0, "high": 102.2, "low": 101.8, "close": 102.0, "volume": 10},
                {"datetime": "2025-03-26 10:31:00", "open": 103.0, "high": 103.2, "low": 102.8, "close": 103.0, "volume": 10},
                {"datetime": "2025-03-26 10:46:00", "open": 103.3, "high": 103.5, "low": 103.1, "close": 103.3, "volume": 10},
            ],
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={
                "execution_contract": {
                    **_entry_execution_boundary("long"),
                    "execution_profile": "breakout",
                    "trigger_source": "technical_breakout",
                    "entry_trigger": canonical_entry_trigger("breakout", "long"),
                    "requires_intraday_confirmation": True,
                    "can_execute_without_intraday_trigger": False,
                    "trigger_confirmation_adjustment": "strict_confirmation_required",
                }
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.signal_datetime, "2025-03-26 10:45:00")
        self.assertEqual(result.base_datetime, "2025-03-26 10:46:00")

    def test_entry_invalidation_before_trigger_permanently_cancels_fac(self):
        result = select_intraday_execution(
            signal_bars=[
                {
                    "datetime": "2025-03-26 10:00:00",
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                    "volume": 10,
                }
            ],
            execution_bars=[
                {"datetime": "2025-03-26 09:30:00", "open": 99.0, "high": 100.0, "low": 99.0, "close": 100.0, "volume": 10},
                {"datetime": "2025-03-26 09:31:00", "open": 99.0, "high": 99.0, "low": 94.0, "close": 96.0, "volume": 10},
                {"datetime": "2025-03-26 10:01:00", "open": 101.0, "high": 102.0, "low": 94.0, "close": 101.0, "volume": 10},
            ],
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={
                "execution_contract": {
                    **_entry_execution_boundary("long"),
                    "execution_profile": "breakout",
                    "trigger_source": "technical_breakout",
                    "entry_trigger": "15分钟收盘价向上突破开盘区间上沿且高于VWAP",
                    "requires_intraday_confirmation": True,
                    "can_execute_without_intraday_trigger": False,
                }
            },
        )

        self.assertFalse(result.should_execute)
        self.assertEqual(result.reason, "fac_invalidated_before_entry")
        self.assertEqual(result.features["entry_contract_state"], "permanently_invalidated")
        self.assertFalse(result.to_audit_payload()["missed_opportunity_flag"])

    def test_event_immediate_entry_scans_all_bars_before_first_fill_for_invalidation(self):
        result = select_intraday_execution(
            signal_bars=[],
            execution_bars=[
                {
                    "datetime": "2025-03-26 09:30:00",
                    "open": 99.0,
                    "high": 100.0,
                    "low": 94.0,
                    "close": 96.0,
                    "volume": 0,
                },
                {
                    "datetime": "2025-03-26 09:31:00",
                    "open": 101.0,
                    "high": 102.0,
                    "low": 100.0,
                    "close": 101.0,
                    "volume": 10,
                },
            ],
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={
                "execution_contract": {
                    **_entry_execution_boundary("long"),
                    "invalidation": canonical_entry_invalidation_condition(
                        "event_immediate", "long"
                    ),
                    "execution_profile": "event_immediate",
                    "trigger_source": "commodity_news_event",
                    "entry_trigger": "当前事件已满足即时执行边界，使用首根合法1分钟线执行",
                    "requires_intraday_confirmation": False,
                    "can_execute_without_intraday_trigger": True,
                }
            },
            finalize_untriggered=False,
        )

        self.assertFalse(result.should_execute)
        self.assertEqual(result.decision, "skip")
        self.assertEqual(result.reason, "fac_invalidated_before_entry")
        self.assertEqual(result.features["observed_datetime"], "2025-03-26 09:30:00")

        expired_contract = {
            **_entry_execution_boundary("long"),
            "invalidation": canonical_entry_invalidation_condition(
                "event_immediate", "long"
            ),
            "execution_profile": "event_immediate",
            "trigger_source": "commodity_news_event",
            "entry_trigger": "当前事件已满足即时执行边界，使用首根合法1分钟线执行",
            "requires_intraday_confirmation": False,
            "can_execute_without_intraday_trigger": True,
            "valid_until": "2025-03-26 09:30:30",
        }
        expired = select_intraday_execution(
            signal_bars=[],
            execution_bars=[
                {
                    "datetime": "2025-03-26 09:30:00",
                    "open": 99.0,
                    "high": 100.0,
                    "low": 96.0,
                    "close": 99.0,
                    "volume": 0,
                },
                {
                    "datetime": "2025-03-26 09:31:00",
                    "open": 101.0,
                    "high": 102.0,
                    "low": 100.0,
                    "close": 101.0,
                    "volume": 10,
                },
            ],
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={"execution_contract": expired_contract},
            finalize_untriggered=False,
        )

        self.assertFalse(expired.should_execute)
        self.assertEqual(expired.decision, "skip")
        self.assertEqual(expired.reason, "fac_expired_before_entry")

    def test_entry_invalidation_and_expiry_are_terminal_during_paper_loop(self):
        class FakeDB:
            def __init__(self):
                self.status_updates = []

            def update_futures_recommendation_status(self, recommendation_id, status, **kwargs):
                self.status_updates.append((recommendation_id, status, kwargs))

        for reason, contract_state in (
            ("fac_invalidated_before_entry", "permanently_invalidated"),
            ("fac_expired_before_entry", "expired"),
        ):
            with self.subTest(reason=reason):
                db = FakeDB()
                snapshot = {}
                selection = SimpleNamespace(
                    reason=reason,
                    to_audit_payload=lambda reason=reason, contract_state=contract_state: {
                        "decision": "skip",
                        "reason": reason,
                        "features": {"entry_contract_state": contract_state},
                    },
                )
                _mark_intraday_non_execution(
                    db=db,
                    recommendation={"id": "rec-entry-terminal", "underlying_code": "RB"},
                    audit_snapshot=snapshot,
                    selection=selection,
                    finalize_untriggered=False,
                    runtime_mode="paper_loop",
                    cutoff_datetime=None,
                    loop_iteration=1,
                )

                self.assertEqual(db.status_updates[-1][1], RecommendationStatus.SKIPPED)
                self.assertEqual(
                    snapshot["execution_result"]["status"],
                    RecommendationStatus.SKIPPED.value,
                )
                self.assertEqual(snapshot["execution_result"]["no_trade_reason"], reason)
                self.assertEqual(snapshot["phase2_execution"]["status"], "skipped_intraday_trigger_not_met")

    def test_strategy_entry_or_close_without_intraday_basis_is_not_replaced_by_morning_basis(self):
        morning_basis = SimpleNamespace(base_price=100.0)
        unavailable_basis = SimpleNamespace(base_price=None)
        for reason in (
            "fac_invalidated_before_entry",
            "fac_expired_before_entry",
            "intraday_no_valid_bar",
        ):
            with self.subTest(reason=reason):
                selection = SimpleNamespace(reason=reason)
                with patch(
                    "agents.execution_team.trader.intraday_confirmation_enabled",
                    return_value=True,
                ), patch(
                    "agents.execution_team.trader._decision_context_from_recommendation",
                    return_value={
                        "execution_contract": {
                            "execution_profile": "event_immediate",
                            "can_execute_without_intraday_trigger": True,
                        }
                    },
                ), patch(
                    "agents.execution_team.trader.resolve_intraday_execution_basis",
                    return_value=(unavailable_basis, selection),
                ):
                    resolved_basis, resolved_selection = _resolve_phase2_execution_basis(
                        router=object(),
                        cfg={"trading_date": "2025-03-26"},
                        recommendation={"underlying_code": "BU", "contract_code": "BU2506"},
                        decision=SimpleNamespace(
                            action=FuturesAction.OPEN_LONG,
                            lots=1,
                            contract_code="BU2506",
                        ),
                        morning_price_context=morning_basis,
                        cutoff_datetime=None,
                        finalize_untriggered=False,
                    )

                self.assertIs(resolved_basis, unavailable_basis)
                self.assertIs(resolved_selection, selection)

        close_selection = SimpleNamespace(reason="intraday_no_valid_bar")
        with patch(
            "agents.execution_team.trader.intraday_confirmation_enabled",
            return_value=True,
        ), patch(
            "agents.execution_team.trader._decision_context_from_recommendation",
            return_value={"execution_contract": {"execution_profile": "exit_immediate"}},
        ), patch(
            "agents.execution_team.trader.resolve_intraday_execution_basis",
            return_value=(unavailable_basis, close_selection),
        ):
            close_basis, close_result = _resolve_phase2_execution_basis(
                router=object(),
                cfg={"trading_date": "2025-03-26"},
                recommendation={"underlying_code": "BU", "contract_code": "BU2506"},
                decision=SimpleNamespace(
                    action=FuturesAction.CLOSE_LONG,
                    lots=1,
                    contract_code="BU2506",
                ),
                morning_price_context=morning_basis,
                cutoff_datetime=None,
                finalize_untriggered=False,
            )

        self.assertIs(close_basis, unavailable_basis)
        self.assertIs(close_result, close_selection)

    def test_trigger_before_later_invalidation_executes_only_the_open_leg(self):
        result = select_intraday_execution(
            signal_bars=[
                {"datetime": "2025-03-26 10:00:00", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 10}
            ],
            execution_bars=[
                {"datetime": "2025-03-26 09:30:00", "open": 99.0, "high": 100.0, "low": 99.0, "close": 100.0, "volume": 10},
                {"datetime": "2025-03-26 10:01:00", "open": 101.0, "high": 102.0, "low": 94.0, "close": 96.0, "volume": 10},
            ],
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={
                "execution_contract": {
                    **_entry_execution_boundary("long"),
                    "execution_profile": "breakout",
                    "trigger_source": "technical_breakout",
                    "entry_trigger": "15分钟收盘价向上突破开盘区间上沿且高于VWAP",
                    "requires_intraday_confirmation": True,
                    "can_execute_without_intraday_trigger": False,
                }
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.reason, "intraday_trigger_confirmed")
        self.assertEqual(result.base_price, 101.0)

    def test_valid_until_expires_unfilled_entry_contract(self):
        contract = {
            **_entry_execution_boundary("long"),
            "execution_profile": "breakout",
            "trigger_source": "technical_breakout",
            "entry_trigger": "15分钟收盘价向上突破开盘区间上沿且高于VWAP",
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "valid_until": "2025-03-26 09:45:00",
        }
        result = select_intraday_execution(
            signal_bars=[
                {"datetime": "2025-03-26 10:00:00", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 10}
            ],
            execution_bars=[
                {"datetime": "2025-03-26 09:30:00", "open": 99.0, "high": 100.0, "low": 99.0, "close": 100.0, "volume": 10},
                {"datetime": "2025-03-26 10:01:00", "open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0, "volume": 10},
            ],
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={"execution_contract": contract},
        )

        self.assertFalse(result.should_execute)
        self.assertEqual(result.reason, "fac_expired_before_entry")

    def test_pullback_requires_prior_expansion_then_retrace_and_reclaim(self):
        trigger = "15分钟先向上扩张，随后回踩开盘区间上沿或VWAP，最终收盘重新严格站上被回踩边界"
        contract = {
            **_entry_execution_boundary("long"),
            "execution_profile": "pullback",
            "trigger_source": "technical_pullback",
            "entry_trigger": trigger,
            "can_execute_without_intraday_trigger": False,
            "requires_intraday_confirmation": True,
        }
        execution_bars = [
            {
                "datetime": "2025-03-26 09:30:00",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 10,
            },
            {
                "datetime": "2025-03-26 09:46:00",
                "open": 102.0,
                "high": 102.2,
                "low": 101.8,
                "close": 102.0,
                "volume": 10,
            },
            {
                "datetime": "2025-03-26 10:01:00",
                "open": 101.6,
                "high": 101.8,
                "low": 101.4,
                "close": 101.7,
                "volume": 10,
            },
        ]
        no_expansion = select_intraday_execution(
            signal_bars=[
                {
                    "datetime": "2025-03-26 10:00:00",
                    "open": 101.2,
                    "high": 101.8,
                    "low": 100.8,
                    "close": 101.5,
                    "volume": 10,
                }
            ],
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={"execution_contract": contract},
        )
        confirmed = select_intraday_execution(
            signal_bars=[
                {
                    "datetime": "2025-03-26 09:45:00",
                    "open": 101.5,
                    "high": 102.5,
                    "low": 101.4,
                    "close": 102.0,
                    "volume": 10,
                },
                {
                    "datetime": "2025-03-26 10:00:00",
                    "open": 101.8,
                    "high": 102.0,
                    "low": 100.8,
                    "close": 101.5,
                    "volume": 10,
                },
            ],
            execution_bars=execution_bars,
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={"execution_contract": contract},
        )
        self.assertFalse(no_expansion.should_execute)
        self.assertEqual(no_expansion.reason, "intraday_trigger_not_met")
        self.assertTrue(confirmed.should_execute)
        self.assertEqual(confirmed.reason, "intraday_pullback_confirmed")
        self.assertEqual(
            confirmed.features["trigger_rule"],
            "directional_expansion_then_boundary_or_vwap_reclaim",
        )

        short_confirmed = select_intraday_execution(
            signal_bars=[
                {
                    "datetime": "2025-03-26 09:45:00",
                    "open": 98.5,
                    "high": 98.6,
                    "low": 97.5,
                    "close": 98.0,
                    "volume": 10,
                },
                {
                    "datetime": "2025-03-26 10:00:00",
                    "open": 98.2,
                    "high": 99.2,
                    "low": 98.0,
                    "close": 98.5,
                    "volume": 10,
                },
            ],
            execution_bars=[
                execution_bars[0],
                {
                    "datetime": "2025-03-26 09:46:00",
                    "open": 98.0,
                    "high": 98.2,
                    "low": 97.8,
                    "close": 98.0,
                    "volume": 10,
                },
                {
                    "datetime": "2025-03-26 10:01:00",
                    "open": 98.4,
                    "high": 98.6,
                    "low": 98.2,
                    "close": 98.3,
                    "volume": 10,
                },
            ],
            action="open_short",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={
                "execution_contract": {
                    **contract,
                    "entry_trigger": "15分钟先向下扩张，随后回抽开盘区间下沿或VWAP，最终收盘重新严格跌回被回抽边界下方",
                    **_entry_execution_boundary("short"),
                }
            },
        )
        self.assertTrue(short_confirmed.should_execute)
        self.assertEqual(short_confirmed.reason, "intraday_pullback_confirmed")

    def test_stronger_pullback_requires_next_completed_bar_to_hold_reclaimed_references(self):
        result = select_intraday_execution(
            signal_bars=[
                {"datetime": "2025-03-26 09:45:00", "open": 101.5, "high": 103.0, "low": 101.4, "close": 102.5, "volume": 10},
                {"datetime": "2025-03-26 10:00:00", "open": 102.0, "high": 102.2, "low": 100.5, "close": 101.5, "volume": 10},
                {"datetime": "2025-03-26 10:15:00", "open": 101.5, "high": 102.0, "low": 101.2, "close": 101.6, "volume": 10},
            ],
            execution_bars=[
                {"datetime": "2025-03-26 09:30:00", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10},
                {"datetime": "2025-03-26 09:46:00", "open": 102.5, "high": 102.7, "low": 102.3, "close": 102.5, "volume": 10},
                {"datetime": "2025-03-26 10:01:00", "open": 101.5, "high": 101.7, "low": 101.3, "close": 101.5, "volume": 10},
                {"datetime": "2025-03-26 10:16:00", "open": 101.7, "high": 101.9, "low": 101.5, "close": 101.7, "volume": 10},
            ],
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={
                "execution_contract": {
                    **_entry_execution_boundary("long"),
                    "execution_profile": "pullback",
                    "trigger_source": "technical_pullback",
                    "entry_trigger": canonical_entry_trigger("pullback", "long"),
                    "requires_intraday_confirmation": True,
                    "can_execute_without_intraday_trigger": False,
                    "trigger_confirmation_adjustment": "stronger_confirmation_required",
                }
            },
        )

        self.assertTrue(result.should_execute)
        self.assertEqual(result.base_datetime, "2025-03-26 10:16:00")
        self.assertEqual(result.signal_datetime, "2025-03-26 10:15:00")

    def test_trader_rejects_missing_or_invalid_profile_instead_of_defaulting_to_breakout(self):
        for execution_contract in (
            {},
            {
                "execution_profile": "range_reversal",
                "trigger_source": "technical_breakout",
                "entry_trigger": "arbitrary trigger prose",
            },
        ):
            with self.subTest(execution_contract=execution_contract):
                result = select_intraday_execution(
                    signal_bars=_non_triggering_signal_bars(),
                    execution_bars=_execution_bars(),
                    action="open_long",
                    config={"opening_range_minutes": 1, "min_execution_volume": 1},
                    decision_context={"execution_contract": execution_contract},
                )
                self.assertFalse(result.should_execute)
                self.assertEqual(result.reason, "execution_profile_contract_invalid")

    def test_strategy_execution_still_requires_auditor_approval(self):
        self.assertFalse(_auditor_verdict_allows_strategy_execution({"audit_payload": {}}))
        self.assertFalse(
            _auditor_verdict_allows_strategy_execution(
                {"audit_payload": {"producer": "auditor", "audit_verdict": "block"}}
            )
        )
        self.assertTrue(
            _auditor_verdict_allows_strategy_execution(
                {"audit_payload": {"producer": "auditor", "audit_verdict": "approve"}}
            )
        )


class Step6ExecutionEvidenceAlignmentTest(unittest.TestCase):
    @staticmethod
    def _signal(
        analyst: str,
        *,
        side: str = "long",
        opportunity_state: str = "watch_for_trigger",
        trigger_valid: bool = False,
        current_trigger_confirmed: bool = False,
        setup_type: str = "trend_breakout",
        entry_trigger: str = "15m close above 100",
        invalidation: str = "15m close below 96",
        confidence: float = 0.82,
        event_type: str = "none",
        invalidation_level: float = 96.0,
        position_invalidation_level: float | None = 94.0,
        atr_stop_distance: float = 2.0,
        evidence_role: str | None = None,
        entry_timing_signal: str | None = None,
    ) -> AnalystSignal:
        signal = Signal.BULLISH if side == "long" else Signal.BEARISH
        role = evidence_role or (
            "entry_timing"
            if analyst == "technical"
            else "direction_context"
            if analyst == "fundamental"
            else "event_catalyst"
        )
        timing = entry_timing_signal or (
            "breakout"
            if analyst == "technical"
            else "event_immediate"
            if analyst == "commodity_news"
            else ""
        )
        aec = build_test_aec(
            analyst,
            signal=signal.value,
            side=side,
            confidence=confidence,
            opportunity_state=opportunity_state,
            setup_type=setup_type,
            setup_quality_ok=True,
            trigger_valid=trigger_valid,
            current_trigger_confirmed=current_trigger_confirmed,
            invalidation_present=True,
            entry_trigger=entry_trigger,
            invalidation_condition=invalidation,
            extra={
                "invalidation_level": invalidation_level,
                "position_invalidation_level": position_invalidation_level,
                "atr_stop_distance": atr_stop_distance,
                "event_type": event_type,
                "evidence_role": role,
                "entry_timing_signal": timing,
            },
        )
        return AnalystSignal(
            agent_name=analyst,
            signal=signal,
            confidence=confidence,
            opportunity_state=opportunity_state,
            setup_type=setup_type,
            setup_quality_ok=True,
            entry_trigger=entry_trigger,
            trigger_valid=trigger_valid,
            invalidation_present=True,
            invalidation_level=invalidation_level,
            position_invalidation_level=position_invalidation_level,
            event_type=event_type,
            evidence_role=role,
            entry_timing_signal=timing,
            metadata={"action_evidence_contract": aec},
        )

    @staticmethod
    def _fields(
        signals: list[AnalystSignal],
        *,
        conditional: bool,
        authority_type: str = "exploration_probe",
        action_values: list[dict] | None = None,
    ) -> dict:
        authority = _entry_authority(conditional=conditional)
        authority["authority_type"] = authority_type
        return _build_execution_contract_fields(
            ticker="BU",
            current_lots=0,
            target_lots=2,
            analyst_signals=signals,
            final_entry_authority=authority,
            trading_date="2025-03-26",
            recommendation_intent={"action": "open_long"},
            control_reasons=[],
            alpha_setup_action_values=action_values,
            reference_price=100.0,
        )

    def test_bu_prefers_technical_execution_role_before_higher_fundamental_confidence(self):
        fields = self._fields(
            [
                self._signal(
                    "fundamental",
                    confidence=0.6256,
                    entry_trigger="fundamental short-horizon narrative",
                    invalidation="fundamental thesis invalidation",
                ),
                self._signal(
                    "technical",
                    confidence=0.62,
                    entry_timing_signal="breakout",
                    entry_trigger="15分钟收盘价向上突破开盘区间上沿且高于VWAP",
                    invalidation="15m close below 96",
                ),
            ],
            conditional=True,
        )

        self.assertEqual(fields["entry_trigger"], "15分钟收盘价向上突破开盘区间上沿且高于VWAP")
        self.assertEqual(
            fields["invalidation"],
            canonical_entry_invalidation_condition("breakout", "long"),
        )
        self.assertEqual(fields["execution_profile"], "breakout")
        self.assertEqual(fields["trigger_source"], "technical_breakout")

    def test_fundamental_direction_context_cannot_supply_execution_fields(self):
        with self.assertRaisesRegex(ValueError, "pm_execution_evidence_not_found"):
            self._fields(
                [self._signal("fundamental", confidence=0.90)],
                conditional=True,
            )

    def test_technical_and_news_sources_keep_their_existing_profiles(self):
        technical = self._fields(
            [
                self._signal(
                    "technical",
                    setup_type="range_reversal_setup",
                    entry_timing_signal="pullback",
                    entry_trigger="15分钟先向上扩张，随后回踩开盘区间上沿或VWAP，最终收盘重新严格站上被回踩边界",
                )
            ],
            conditional=True,
        )
        news = self._fields(
            [
                self._signal(
                    "commodity_news",
                    opportunity_state="tradeable_candidate",
                    trigger_valid=True,
                    current_trigger_confirmed=True,
                    setup_type="event_catalyst",
                    entry_timing_signal="event_immediate",
                    entry_trigger="当前事件已满足即时执行边界，使用首根合法1分钟线执行",
                    event_type="supply_disruption",
                )
            ],
            conditional=False,
            authority_type="real_budget_entry",
        )

        self.assertEqual(
            (technical["execution_profile"], technical["trigger_source"]),
            ("pullback", "technical_pullback"),
        )
        self.assertEqual(
            (news["execution_profile"], news["trigger_source"]),
            ("event_immediate", "commodity_news_event"),
        )
        self.assertTrue(technical["requires_intraday_confirmation"])
        self.assertFalse(technical["can_execute_without_intraday_trigger"])
        self.assertFalse(news["requires_intraday_confirmation"])
        self.assertTrue(news["can_execute_without_intraday_trigger"])

    def test_structured_weak_conflict_requires_stronger_confirmation_without_reason_parsing(self):
        signal = self._signal(
            "technical",
            entry_trigger=canonical_entry_trigger("breakout", "long"),
        )
        reason_only_authority = _entry_authority(conditional=True)
        reason_only_authority["reason_codes"] = [
            "weak_conflict_probe_requires_stronger_confirmation"
        ]
        reason_only = _build_execution_contract_fields(
            ticker="BU",
            current_lots=0,
            target_lots=2,
            analyst_signals=[signal],
            final_entry_authority=reason_only_authority,
            trading_date="2025-03-26",
            recommendation_intent={"action": "open_long"},
            control_reasons=[],
            alpha_setup_action_values=[],
            reference_price=100.0,
        )
        structured_authority = dict(reason_only_authority)
        structured_authority["weak_conflict_probe"] = True
        structured = _build_execution_contract_fields(
            ticker="BU",
            current_lots=0,
            target_lots=2,
            analyst_signals=[signal],
            final_entry_authority=structured_authority,
            trading_date="2025-03-26",
            recommendation_intent={"action": "open_long"},
            control_reasons=[],
            alpha_setup_action_values=[],
            reference_price=100.0,
        )

        self.assertEqual(reason_only["trigger_confirmation_adjustment"], "not_applicable")
        self.assertEqual(
            structured["trigger_confirmation_adjustment"],
            "stronger_confirmation_required",
        )
        final_contract = _build_final_action_contract(
            ticker="BU",
            current_lots=0,
            target_lots=2,
            position_ratio=0.01,
            margin_required=10_000.0,
            account_equity=1_000_000.0,
            lots_to_trade=2,
            lots_to_trade_reason="test_stronger_confirmation",
            recommendation_intent={"action": "open_long", "lots": 2},
            final_entry_authority=structured_authority,
            control_reasons=[],
            control_diagnostics={},
            opportunity_scorecard={
                "preferred_side": "long",
                "long": {"final_state": "probe_candidate", "score": 0.65},
            },
            market_confirmation={"confirmation_score": 0.65},
            alpha_setup_action_values=[],
            execution_contract_fields=structured,
        )
        self.assertEqual(
            final_contract["trigger_confirmation_adjustment"],
            "stronger_confirmation_required",
        )

    def test_formal_exact_open_learning_can_require_strict_confirmation(self):
        formal = {
            "id": "av-bu-long-breakout-loss",
            "ticker": "BU",
            "side": "long",
            "setup_type": "trend_breakout",
            "action_name": "open",
            "canonical_action_family": "open_add_new_risk",
            "action_value_lane": "open",
            "learning_lane": "open",
            "consumer_scope": "pm_learning",
            "canonical_action_value": True,
            "action_preference": "tail_loss_protect",
            "reward_source": "trade_episode",
            "evidence_scope": "exact_real_state",
            "reward_sum": -4000.0,
            "reward_mean": -2000.0,
            "win_rate": 0.0,
            "payload": {
                "entry_quality_outcome": {
                    "contract_version": "agentquant.entry_quality_outcome.v1",
                    "trigger_confirmation_adjustment": "strict_confirmation_required",
                    "entry_trigger": canonical_entry_trigger("breakout", "long"),
                }
            },
        }
        strict = self._fields(
            [self._signal("technical", entry_trigger=canonical_entry_trigger("breakout", "long"))],
            conditional=True,
            action_values=[formal],
        )
        mismatched = self._fields(
            [self._signal(
                "technical",
                setup_type="trend_breakout_setup",
                entry_timing_signal="pullback",
                entry_trigger=canonical_entry_trigger("pullback", "long"),
            )],
            conditional=True,
            action_values=[formal],
        )
        weak_prior = {
            **formal,
            "canonical_action_value": False,
            "evidence_scope": "similar_sql_prior",
            "retrieval_match_level": "similar",
        }
        rejected = self._fields(
            [self._signal("technical", entry_trigger=canonical_entry_trigger("breakout", "long"))],
            conditional=True,
            action_values=[weak_prior],
        )

        self.assertEqual(strict["trigger_confirmation_adjustment"], "strict_confirmation_required")
        self.assertEqual(mismatched["trigger_confirmation_adjustment"], "not_applicable")
        self.assertEqual(rejected["trigger_confirmation_adjustment"], "not_applicable")

    def test_weak_conflict_vwap_uses_structured_stronger_confirmation(self):
        authority = _entry_authority(conditional=True)
        authority["weak_conflict_probe"] = True
        fields = _build_execution_contract_fields(
            ticker="BU",
            current_lots=0,
            target_lots=2,
            analyst_signals=[
                self._signal(
                    "technical",
                    setup_type="range_reversal_setup",
                    entry_timing_signal="vwap_confirmed",
                    entry_trigger=canonical_entry_trigger("vwap_confirmed", "long"),
                )
            ],
            final_entry_authority=authority,
            trading_date="2025-03-26",
            recommendation_intent={"action": "open_long"},
            control_reasons=[],
            alpha_setup_action_values=[],
            reference_price=100.0,
        )

        self.assertEqual(
            fields["trigger_confirmation_adjustment"],
            "stronger_confirmation_required",
        )

    def test_opposite_evidence_cannot_supply_execution_fields(self):
        fields = self._fields(
            [
                self._signal(
                    "technical",
                    side="short",
                    entry_trigger="15m close below 90",
                    invalidation="15m close above 94",
                    confidence=0.95,
                ),
                self._signal(
                    "technical",
                    confidence=0.70,
                    entry_trigger="15分钟收盘价向上突破开盘区间上沿且高于VWAP",
                ),
            ],
            conditional=True,
        )

        self.assertEqual(fields["entry_trigger"], "15分钟收盘价向上突破开盘区间上沿且高于VWAP")
        self.assertEqual(
            fields["invalidation"],
            canonical_entry_invalidation_condition("breakout", "long"),
        )
        self.assertEqual(fields["trigger_source"], "technical_breakout")

    def test_multiple_sources_are_not_cross_combined(self):
        fields = self._fields(
            [
                self._signal(
                    "technical",
                    entry_timing_signal="breakout",
                    entry_trigger="15分钟收盘价向上突破开盘区间上沿且高于VWAP",
                    invalidation="technical 15m failure",
                    confidence=0.90,
                ),
                self._signal(
                    "fundamental",
                    entry_trigger="fundamental price confirmation",
                    invalidation="fundamental thesis invalidation",
                    confidence=0.80,
                ),
            ],
            conditional=True,
        )

        self.assertEqual(fields["entry_trigger"], "15分钟收盘价向上突破开盘区间上沿且高于VWAP")
        self.assertEqual(
            fields["invalidation"],
            canonical_entry_invalidation_condition("breakout", "long"),
        )
        self.assertEqual(fields["trigger_source"], "technical_breakout")

    def test_invalid_timing_value_never_defaults_to_breakout(self):
        with self.assertRaisesRegex(ValueError, "pm_execution_evidence_not_found"):
            self._fields(
                [
                    self._signal(
                        "technical",
                        setup_type="inventory_dislocation",
                        entry_timing_signal="range_reversal",
                        entry_trigger="15m volume exceeds 1000 contracts",
                    )
                ],
                conditional=True,
            )

    def test_execution_action_value_overlay_cannot_create_source_facts_or_authority(self):
        fields = self._fields(
            [
                self._signal(
                    "technical",
                    opportunity_state="tradeable_candidate",
                    trigger_valid=True,
                    current_trigger_confirmed=True,
                    entry_timing_signal="breakout",
                    entry_trigger="15分钟收盘价向上突破开盘区间上沿且高于VWAP",
                )
            ],
            conditional=False,
            action_values=[
                {
                    "ticker": "BU",
                    "side": "long",
                    "action_name": "execution",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "data_combo": "fundamental:inventory|execution:breakout",
                    "execution_retrieval_key": "BU|breakout|technical_breakout|execution",
                    "sample_count": 3,
                    "reward_mean": -100.0,
                    "reward_sum": -300.0,
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

        self.assertEqual(fields["entry_trigger"], "15分钟收盘价向上突破开盘区间上沿且高于VWAP")
        self.assertEqual(
            fields["invalidation"],
            canonical_entry_invalidation_condition("breakout", "long"),
        )
        self.assertEqual(fields["execution_profile"], "breakout")
        self.assertEqual(fields["trigger_source"], "technical_breakout")
        self.assertTrue(fields["requires_intraday_confirmation"])
        self.assertFalse(fields["can_execute_without_intraday_trigger"])
        self.assertEqual(
            fields["execution_action_value_preference"]["execution_profile"],
            "pullback",
        )
        self.assertFalse(fields["execution_action_value_preference"]["applied"])
        self.assertTrue(fields["execution_action_value_preference"]["diagnostic_only"])
        self.assertTrue(
            fields["execution_action_value_preference"]["does_not_create_trade_authority"]
        )


class IncrementalMarginExecutionPathTest(unittest.TestCase):
    _CONTRACT_INFO = {
        "contract_multiplier": 10.0,
        "margin_rate_long": 0.10,
        "margin_rate_short": 0.10,
    }

    def test_auditor_and_trader_use_incremental_margin_for_long_and_short_scale(self):
        for current_lots, target_lots, expected_action in (
            (10, 15, FuturesAction.OPEN_LONG),
            (-10, -15, FuturesAction.OPEN_SHORT),
        ):
            with self.subTest(current_lots=current_lots, target_lots=target_lots):
                contract = _strategy_contract(
                    current_lots=current_lots,
                    target_lots=target_lots,
                    final_action="scale",
                )
                recommendation = _recommendation(contract)
                audit = audit_futures_recommendation(
                    recommendation=recommendation,
                    hard_risk_config={"max_total_margin_ratio": 0.20},
                    account_state={
                        "account_equity": 1_000_000.0,
                        "margin_used": 150_000.0,
                        "margin_ratio": 0.15,
                        "risk_status": "NORMAL",
                    },
                    position_state={
                        "current_lots": current_lots,
                        "contract_code": "BU2506",
                        "margin_used": 40_000.0,
                    },
                    contract_state={
                        "contract_code": "BU2506",
                        "underlying_code": "BU",
                        "as_of_date": "2025-03-25",
                        "source": "pandaai_main_contract_quote",
                    },
                    data_quality={"status": "clean", "flags": []},
                )
                self.assertEqual(audit.audit_verdict, "approve")
                self.assertAlmostEqual(
                    audit.audit_payload["contract_summary"]["projected_total_margin_ratio"],
                    0.17,
                )

                snapshot = {"final_action_contract": contract}
                with patch(
                    "agents.execution_team.trader.FuturesContractInfoCache.get_contract_info",
                    return_value=self._CONTRACT_INFO,
                ):
                    decision = _translate_pre_open_recommendation_to_order(
                        recommendation=recommendation,
                        portfolio=_portfolio(
                            bu_lots=current_lots,
                            bu_margin=40_000.0,
                            other_margin=110_000.0,
                            total_margin=150_000.0,
                        ),
                        config=_execution_config(),
                        morning_price_context=SimpleNamespace(base_price=4_000.0),
                        snapshot=snapshot,
                    )

                self.assertEqual(decision.action, expected_action)
                self.assertEqual(decision.lots, 5)
                self.assertNotIn(
                    "margin_adjustment_to_no_new_entry",
                    snapshot.get("execution_translation", {}).get("rewrite_reasons", []),
                )

    def test_reduce_and_exit_are_not_blocked_by_new_risk_margin_check(self):
        for target_lots, final_action, expected_action, expected_lots in (
            (5, "reduce", FuturesAction.CLOSE_LONG, 5),
            (0, "exit", FuturesAction.CLOSE_LONG, 10),
        ):
            with self.subTest(final_action=final_action):
                contract = _strategy_contract(
                    current_lots=10,
                    target_lots=target_lots,
                    final_action=final_action,
                )
                recommendation = _recommendation(contract)
                snapshot = {"final_action_contract": contract}
                with patch(
                    "agents.execution_team.trader.FuturesContractInfoCache.get_contract_info",
                    return_value=self._CONTRACT_INFO,
                ):
                    decision = _translate_pre_open_recommendation_to_order(
                        recommendation=recommendation,
                        portfolio=_portfolio(
                            bu_lots=10,
                            bu_margin=100_000.0,
                            other_margin=110_000.0,
                            total_margin=210_000.0,
                        ),
                        config=_execution_config(),
                        morning_price_context=SimpleNamespace(base_price=4_000.0),
                        snapshot=snapshot,
                    )

                self.assertEqual(decision.action, expected_action)
                self.assertEqual(decision.lots, expected_lots)


class RiskReductionIsolationTest(unittest.TestCase):
    def test_risk_reduction_evidence_is_preserved_but_not_tradeable_support(self):
        signal = _analyst_signal(opportunity_state="risk_reduction_candidate")

        summary = _side_opportunity_state_summary([signal], "long")

        self.assertEqual(summary["risk_reduction_support_count"], 1)
        self.assertEqual(summary["supporting_signal_count"], 0)
        self.assertEqual(summary["tradeable_support_count"], 0)
        self.assertFalse(summary["has_tradeable_support"])

    def test_risk_reduction_cannot_create_open_authority(self):
        signal = _analyst_signal(opportunity_state="risk_reduction_candidate")

        authority = _enrich_final_authority_with_analyst_evidence(
            _entry_authority(allowed=True) | {
                "open_action_evidence": False,
                "strong_current_evidence": False,
            },
            [signal],
            target_side="long",
        )

        self.assertFalse(authority["open_action_evidence"])
        self.assertFalse(authority["strong_current_evidence"])

    def test_empty_position_risk_reduction_does_not_enter_new_risk_scorecard(self):
        risk_reduction = _analyst_signal(opportunity_state="risk_reduction_candidate")

        scorecard = build_opportunity_scorecard(
            ticker="BU",
            analyst_signals=[risk_reduction],
            market_confirmation={"confirmation_score": 0.90},
            data_quality_summary={},
            config={},
        )

        self.assertEqual(scorecard["long"]["final_state"], "no_opportunity")
        self.assertEqual(scorecard["long"]["supporting_signal_count"], 0)
        self.assertEqual(scorecard["long"]["tradeable_opportunity_state_count"], 0)

    def test_risk_reduction_is_not_a_veto_for_a_separate_tradeable_candidate(self):
        risk_reduction = _analyst_signal(opportunity_state="risk_reduction_candidate")
        tradeable = _analyst_signal(
            analyst="commodity_news",
            opportunity_state="tradeable_candidate",
        )

        scorecard = build_opportunity_scorecard(
            ticker="BU",
            analyst_signals=[risk_reduction, tradeable],
            market_confirmation={"confirmation_score": 0.90},
            data_quality_summary={},
            config={},
        )

        self.assertIn(scorecard["long"]["final_state"], {"probe_candidate", "tradeable_candidate"})
        self.assertEqual(scorecard["long"]["supporting_signal_count"], 1)
        self.assertEqual(
            scorecard["long"]["opportunity_state_counts"]["risk_reduction_candidate"],
            1,
        )

    def test_risk_reduction_never_enters_rank_and_existing_position_stays_lifecycle_only(self):
        new_risk_state = {
            "current_lots": 0,
            "target_lots": 2,
            "final_action": "open_real",
            "final_entry_authority": {"authority_type": "real_budget_entry"},
        }
        risk_reduction_row = {
            "final_state": "risk_reduction_candidate",
            "opportunity_score": 0.95,
        }

        self.assertFalse(_capital_rank_eligible(new_risk_state, risk_reduction_row))
        for target_lots, expected_port in ((10, "position_hold"), (5, "capital_release"), (0, "capital_release")):
            with self.subTest(target_lots=target_lots):
                lifecycle = classify_lifecycle_action_port(
                    {
                        "current_lots": 10,
                        "target_lots": target_lots,
                        "final_action": "hold" if target_lots == 10 else "reduce",
                    }
                )
                self.assertEqual(lifecycle["pm_lifecycle_action_port"], expected_port)
                self.assertFalse(lifecycle["requires_full_market_rank"])


class OpportunityPathSemanticRepairTest(unittest.TestCase):
    _ANALYSTS = ("technical", "fundamental", "commodity_news")

    @staticmethod
    def _collector_signal(
        analyst: str,
        *,
        signal_record_id: str,
        signal: str = "Neutral",
        side: str = "flat",
        opportunity_state: str = "no_opportunity",
        trigger_valid: bool = False,
        current_trigger_confirmed: bool = False,
        entry_trigger: str = "",
        invalidation_present: bool = False,
        confidence: float = 0.45,
        missing_evidence: list[str] | None = None,
    ) -> SimpleNamespace:
        executable = opportunity_state in {
            "watch_for_trigger",
            "probe_candidate",
            "tradeable_candidate",
        }
        timing = (
            "breakout"
            if analyst == "technical" and executable
            else "event_immediate"
            if analyst == "commodity_news"
            and opportunity_state in {"probe_candidate", "tradeable_candidate"}
            else ""
        )
        canonical_trigger = (
            "15分钟收盘价向上突破开盘区间上沿且高于VWAP"
            if timing == "breakout" and side == "long"
            else "15分钟收盘价向下突破开盘区间下沿且低于VWAP"
            if timing == "breakout" and side == "short"
            else "当前事件已满足即时执行边界，使用首根合法1分钟线执行"
            if timing == "event_immediate"
            else ""
        )
        resolved_entry_trigger = entry_trigger or canonical_trigger
        return build_test_signal(
            analyst,
            signal_record_id=signal_record_id,
            trading_date="2025-03-26",
            signal=signal,
            side=side,
            confidence=confidence,
            opportunity_state=opportunity_state,
            setup_type="trend_breakout" if side in {"long", "short"} else "no_trade",
            setup_quality_ok=side in {"long", "short"},
            trigger_valid=trigger_valid,
            current_trigger_confirmed=current_trigger_confirmed,
            invalidation_present=invalidation_present,
            entry_trigger=resolved_entry_trigger,
            invalidation_condition=(
                "close beyond the validated invalidation boundary"
                if invalidation_present
                else None
            ),
            extra={
                "missing_evidence": list(missing_evidence or []),
                "business_quality_score": 0.82 if side in {"long", "short"} else 0.0,
                "setup_quality_score": 0.82 if side in {"long", "short"} else 0.0,
                "evidence_role": (
                    "entry_timing"
                    if analyst == "technical"
                    else "direction_context"
                    if analyst == "fundamental"
                    else "event_catalyst"
                ),
                "entry_timing_signal": timing,
            },
        )

    def _scc(self, signals: list[SimpleNamespace]) -> dict:
        return build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-26",
            analyst_signals=signals,
            enabled_analysts=self._ANALYSTS,
        )

    def test_step2_preferred_side_comes_from_resolved_scc_direction(self):
        scorecard = {
            "preferred_side": "flat",
            "long": {
                "side": "long",
                "final_state": "watch_for_trigger",
                "opportunity_score": 0.32,
                "direction_evidence_strength": 0.40,
            },
            "short": {
                "side": "short",
                "final_state": "no_opportunity",
                "opportunity_score": 0.0,
                "direction_evidence_strength": 0.0,
            },
        }

        result = select_ticker_side(
            ticker="BU",
            analyst_signals=[],
            signal_collection_contract={
                "dominant_side": "long",
                "side_consensus": "single_analyst_support",
                "evidence_fusion": {"evidence_alignment_state": "single_source"},
            },
            market_confirmation={},
            data_quality_summary={},
            decision_date="2025-03-26",
            config={},
            prebuilt_scorecard=scorecard,
        )

        self.assertEqual(result["opportunity_scorecard"]["preferred_side"], "long")
        self.assertEqual(result["side_priority"]["long"], 1)
        self.assertNotIn("opportunity_rank", result["opportunity_scorecard"]["long"])

    def test_step2_keeps_flat_for_mixed_or_conflicted_scc(self):
        for dominant_side, side_consensus in (("mixed", "conflicted"), ("long", "conflicted")):
            with self.subTest(dominant_side=dominant_side, side_consensus=side_consensus):
                result = select_ticker_side(
                    ticker="BU",
                    analyst_signals=[],
                    signal_collection_contract={
                        "dominant_side": dominant_side,
                        "side_consensus": side_consensus,
                        "evidence_fusion": {"evidence_alignment_state": "conflicted"},
                    },
                    market_confirmation={},
                    data_quality_summary={},
                    decision_date="2025-03-26",
                    config={},
                    prebuilt_scorecard={
                        "preferred_side": "long",
                        "long": {"final_state": "tradeable_candidate", "opportunity_score": 0.9},
                        "short": {"final_state": "probe_candidate", "opportunity_score": 0.8},
                    },
                )
                scorecard = result["opportunity_scorecard"]
                self.assertEqual(scorecard["preferred_side"], "flat")
                side, ratio, row = _scorecard_probe_seed(
                    opportunity_scorecard=scorecard,
                    control={
                        "watch_for_trigger_new_entry": {
                            "allow_probe": True,
                            "probe_max_ratio": 0.01,
                            "probe_floor_ratio": 0.008,
                            "scorecard_probe_min_supporting_signals": 1,
                            "scorecard_probe_min_score": 0.0,
                        }
                    },
                )
                self.assertEqual((side, ratio, row), ("flat", 0.0, {}))

    def test_missing_evidence_lowers_quality_without_becoming_hard_data_gap(self):
        signals = [
            self._collector_signal(
                "technical",
                signal_record_id="sig-tech",
                signal="Bullish",
                side="long",
                opportunity_state="watch_for_trigger",
                entry_trigger="15分钟收盘价向上突破开盘区间上沿且高于VWAP",
                invalidation_present=True,
                missing_evidence=[f"pending_confirmation_{index}" for index in range(8)],
            ),
            self._collector_signal("fundamental", signal_record_id="sig-fund"),
            self._collector_signal("commodity_news", signal_record_id="sig-news"),
        ]
        scc = self._scc(signals)
        confirmation = build_scc_market_confirmation(scc, target_direction="long")
        scorecard = build_opportunity_scorecard(
            ticker="BU",
            analyst_signals=build_pm_evidence_signals_from_scc(scc),
            market_confirmation=confirmation,
            data_quality_summary=build_scc_data_quality_summary(scc),
            signal_collection_contract=scc,
            config={},
        )

        self.assertEqual(confirmation["data_missing"], [])
        self.assertFalse(scorecard["long"]["critical_data_gap"])
        self.assertNotIn("critical_data_gap", scorecard["long"]["gating_failures"])
        self.assertEqual(scorecard["long"]["final_state"], "watch_for_trigger")

    def test_only_shared_hard_fail_quality_status_creates_critical_gap(self):
        signal = _analyst_signal(
            opportunity_state="tradeable_candidate",
            trigger_valid=True,
            current_trigger_confirmed=True,
        )
        warning = build_opportunity_scorecard(
            ticker="BU",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.50},
            data_quality_summary={
                "status": "warning",
                "flags": ["data_source_stale:fundamental:finoview_fundamental"],
            },
            config={},
        )
        hard_fail = build_opportunity_scorecard(
            ticker="BU",
            analyst_signals=[signal],
            market_confirmation={"confirmation_score": 0.50},
            data_quality_summary={
                "status": "hard_fail",
                "flags": ["required_market_data_unavailable"],
            },
            config={},
        )

        self.assertFalse(warning["long"]["critical_data_gap"])
        self.assertTrue(hard_fail["long"]["critical_data_gap"])
        self.assertIn("critical_data_gap", hard_fail["long"]["gating_failures"])

    def test_single_confirmed_source_reaches_rank_seed_with_native_low_score(self):
        single_signals = [
            self._collector_signal(
                "technical",
                signal_record_id="sig-tech",
                signal="Bullish",
                side="long",
                opportunity_state="tradeable_candidate",
                trigger_valid=True,
                current_trigger_confirmed=True,
                entry_trigger="15分钟收盘价向上突破开盘区间上沿且高于VWAP",
                invalidation_present=True,
                confidence=0.82,
            ),
            self._collector_signal("fundamental", signal_record_id="sig-fund"),
            self._collector_signal("commodity_news", signal_record_id="sig-news"),
        ]
        aligned_signals = [
            self._collector_signal(
                "technical",
                signal_record_id="sig-technical",
                signal="Bullish",
                side="long",
                opportunity_state="tradeable_candidate",
                trigger_valid=True,
                current_trigger_confirmed=True,
                invalidation_present=True,
                confidence=0.82,
            ),
            self._collector_signal(
                "fundamental",
                signal_record_id="sig-fundamental",
                signal="Bullish",
                side="long",
                opportunity_state="no_opportunity",
                confidence=0.82,
            ),
            self._collector_signal(
                "commodity_news",
                signal_record_id="sig-commodity_news",
                signal="Bullish",
                side="long",
                opportunity_state="tradeable_candidate",
                trigger_valid=True,
                current_trigger_confirmed=True,
                invalidation_present=True,
                confidence=0.82,
            ),
        ]

        def scorecard_for(signals: list[SimpleNamespace]) -> dict:
            scc = self._scc(signals)
            scorecard = build_opportunity_scorecard(
                ticker="BU",
                analyst_signals=build_pm_evidence_signals_from_scc(scc),
                market_confirmation=build_scc_market_confirmation(scc, target_direction="long"),
                data_quality_summary=build_scc_data_quality_summary(scc),
                signal_collection_contract=scc,
                config={},
            )
            return select_ticker_side(
                ticker="BU",
                analyst_signals=build_pm_evidence_signals_from_scc(scc),
                signal_collection_contract=scc,
                market_confirmation=build_scc_market_confirmation(scc, target_direction="long"),
                data_quality_summary=build_scc_data_quality_summary(scc),
                decision_date="2025-03-26",
                config={},
                prebuilt_scorecard=scorecard,
            )["opportunity_scorecard"]

        single = scorecard_for(single_signals)
        aligned = scorecard_for(aligned_signals)
        side, ratio, row = _scorecard_probe_seed(
            opportunity_scorecard=single,
            control={
                "watch_for_trigger_new_entry": {
                    "allow_probe": True,
                    "probe_max_ratio": 0.01,
                    "probe_floor_ratio": 0.005,
                    "scorecard_probe_min_supporting_signals": 2,
                    "scorecard_probe_min_score": 0.35,
                    "allow_single_high_quality_probe": True,
                    "single_high_quality_probe_min_score": 0.52,
                    "single_high_quality_probe_min_setup_quality": 0.60,
                    "single_high_quality_probe_min_business_quality": 0.60,
                    "single_high_quality_probe_min_confirmation_score": 0.45,
                    "scorecard_tradeable_candidate_probe_min_confirmation_score": 0.68,
                }
            },
        )
        single_rank = _ensure_final_rank_score_fields(dict(single["long"]), config={})
        aligned_rank = _ensure_final_rank_score_fields(dict(aligned["long"]), config={})

        self.assertEqual(single["long"]["supporting_signal_count"], 1)
        self.assertEqual(
            single["long"]["final_state"],
            "probe_candidate",
            msg=single["long"],
        )
        self.assertEqual(side, "long")
        self.assertGreater(ratio, 0.0)
        self.assertTrue(row["trigger_valid"])
        self.assertLess(single_rank["rank_score"], aligned_rank["rank_score"])

    def test_directional_no_opportunity_does_not_become_watch_support(self):
        signals = [
            self._collector_signal(
                "technical",
                signal_record_id="sig-tech",
                signal="Bearish",
                side="short",
                opportunity_state="no_opportunity",
                confidence=0.75,
            ),
            self._collector_signal(
                "fundamental",
                signal_record_id="sig-fund",
                signal="Bearish",
                side="short",
                opportunity_state="no_opportunity",
                confidence=0.65,
            ),
            self._collector_signal(
                "commodity_news",
                signal_record_id="sig-news",
                signal="Bullish",
                side="long",
                opportunity_state="tradeable_candidate",
                trigger_valid=True,
                current_trigger_confirmed=True,
                invalidation_present=True,
                confidence=0.45,
            ),
        ]
        scc = self._scc(signals)
        scorecard = build_opportunity_scorecard(
            ticker="BU",
            analyst_signals=build_pm_evidence_signals_from_scc(scc),
            market_confirmation=build_scc_market_confirmation(scc, target_direction="short"),
            data_quality_summary=build_scc_data_quality_summary(scc),
            signal_collection_contract=scc,
            config={},
        )

        self.assertEqual(scc["dominant_side"], "short")
        self.assertEqual(scc["trigger_status"], "not_applicable")
        self.assertEqual(scorecard["short"]["supporting_signal_count"], 0)
        self.assertEqual(scorecard["short"]["final_state"], "no_opportunity")

    def test_shared_trigger_semantics_rejects_bare_or_future_only_watch(self):
        for entry_trigger in (
            "No current entry trigger is established",
            "Wait for the next weekly inventory report before deciding",
            "The directional thesis remains constructive",
        ):
            with self.subTest(entry_trigger=entry_trigger):
                contract = build_test_aec(
                    "technical",
                    trading_date="2025-03-26",
                    signal="Bullish",
                    side="long",
                    opportunity_state="watch_for_trigger",
                    trigger_valid=False,
                    current_trigger_confirmed=False,
                    invalidation_present=True,
                    entry_trigger=entry_trigger,
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "action_evidence_contract_entry_trigger_not_canonical",
                ):
                    validate_action_evidence_contract(contract, analyst="technical")

        observable = build_test_aec(
            "technical",
            trading_date="2025-03-26",
            signal="Bullish",
            side="long",
            opportunity_state="watch_for_trigger",
            trigger_valid=False,
            current_trigger_confirmed=False,
            invalidation_present=True,
            entry_trigger="15分钟收盘价向上突破开盘区间上沿且高于VWAP",
        )
        validate_action_evidence_contract(observable, analyst="technical")

    def test_candidate_requires_current_confirmation_and_watch_must_be_pending(self):
        candidate = build_test_aec(
            "technical",
            trading_date="2025-03-26",
            signal="Bullish",
            side="long",
            opportunity_state="probe_candidate",
            trigger_valid=True,
            current_trigger_confirmed=False,
            invalidation_present=True,
            entry_trigger="15分钟收盘价向上突破开盘区间上沿且高于VWAP",
        )
        with self.assertRaisesRegex(
            ValueError,
            "action_evidence_contract_candidate_without_current_confirmation",
        ):
            validate_action_evidence_contract(candidate, analyst="technical")

        watch = build_test_aec(
            "technical",
            trading_date="2025-03-26",
            signal="Bullish",
            side="long",
            opportunity_state="watch_for_trigger",
            trigger_valid=True,
            current_trigger_confirmed=True,
            invalidation_present=True,
            entry_trigger="15分钟收盘价向上突破开盘区间上沿且高于VWAP",
        )
        with self.assertRaisesRegex(
            ValueError,
            "action_evidence_contract_watch_trigger_already_confirmed",
        ):
            validate_action_evidence_contract(watch, analyst="technical")

    def test_finalization_does_not_turn_bare_text_without_profile_into_watch(self):
        signal = _analyst_signal(
            opportunity_state="watch_for_trigger",
            trigger_valid=False,
            current_trigger_confirmed=False,
            entry_trigger="No current entry trigger is established",
            entry_timing_signal="",
        )
        signal.metadata = {
            **signal.metadata,
            "data_usage_summary": signal.metadata["action_evidence_contract"]["data_usage_summary"],
        }
        result = apply_trade_research_contract(
            signal,
            {
                "sector": "energy",
                "tradeability": "high",
                "setup_quality_ok": True,
                "market_regime": "trend",
            },
            analyst="technical",
            trading_date="2025-03-26",
            ticker="BU",
        )

        self.assertEqual(result.opportunity_state, "no_opportunity")
        self.assertEqual(result.entry_trigger, "")


if __name__ == "__main__":
    unittest.main()
