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
    _build_pm_decision_context,
    _enrich_final_authority_with_analyst_evidence,
    _side_opportunity_state_summary,
)
from agents.execution_team.trader import (
    _auditor_verdict_allows_strategy_execution,
    _translate_pre_open_recommendation_to_order,
)
from graph.constants import Signal
from graph.schema import AnalystSignal, FuturesAction, Portfolio, Position
from tests.contract_test_fixtures import build_test_aec
from tools.agent_tools.decision.pm_signal_fusion import build_opportunity_scorecard
from tools.agent_tools.decision.pm_full_market_capital_deployment import _capital_rank_eligible
from tools.agent_tools.decision.pm_lifecycle_action_port import classify_lifecycle_action_port
from tools.agent_tools.execution.trader_intraday_execution import select_intraday_execution


def _analyst_signal(
    *,
    analyst: str = "technical",
    signal: Signal = Signal.BULLISH,
    opportunity_state: str = "tradeable_candidate",
    trigger_valid: bool = True,
    current_trigger_confirmed: bool = True,
    invalidation_present: bool = True,
    entry_trigger: str = "break above the validated range",
) -> AnalystSignal:
    side = "long" if signal == Signal.BULLISH else "short" if signal == Signal.BEARISH else "flat"
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
        entry_trigger=entry_trigger,
        invalidation_condition=(
            "close beyond the validated invalidation boundary"
            if invalidation_present
            else None
        ),
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
        entry_trigger=entry_trigger,
        trigger_valid=trigger_valid,
        invalidation_present=invalidation_present,
        invalidation_level=96.0 if invalidation_present else None,
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
        "entry_trigger": "break above the validated range",
        "invalidation": "close beyond the validated invalidation boundary",
        "invalidation_condition": "close beyond the validated invalidation boundary",
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
    def test_pm_marks_currently_confirmed_technical_trigger_as_direct_execution(self):
        for trigger in (
            "break above the validated range",
            "pullback to VWAP support and stabilize",
        ):
            with self.subTest(trigger=trigger):
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
                    analyst_signals=[_analyst_signal(entry_trigger=trigger)],
                    final_entry_authority=_entry_authority(),
                    trading_date="2025-03-26",
                    recommendation_intent={"action": "open_long"},
                    control_reasons=["test_pm_final_trade_authority"],
                )

                self.assertTrue(plan["can_execute_without_intraday_trigger"])
                self.assertFalse(plan["requires_intraday_confirmation"])

    def test_pm_requires_current_confirmation_invalidation_and_funds_for_direct_execution(self):
        cases = (
            (_analyst_signal(current_trigger_confirmed=False), _entry_authority()),
            (_analyst_signal(invalidation_present=False), _entry_authority()),
            (_analyst_signal(), _entry_authority(allowed=False)),
        )
        for signal, authority in cases:
            with self.subTest(signal=signal.opportunity_state, authority=authority["authority_type"]):
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
                    analyst_signals=[signal],
                    final_entry_authority=authority,
                    trading_date="2025-03-26",
                    recommendation_intent={"action": "open_long"},
                    control_reasons=[],
                )
                self.assertFalse(plan["can_execute_without_intraday_trigger"])

    def test_trader_honors_direct_execution_for_every_canonical_entry_profile(self):
        for profile in ("breakout", "pullback", "vwap_confirmed", "event_immediate"):
            with self.subTest(profile=profile):
                result = select_intraday_execution(
                    signal_bars=_non_triggering_signal_bars(),
                    execution_bars=_execution_bars(),
                    action="open_long",
                    config={"opening_range_minutes": 1, "min_execution_volume": 1},
                    decision_context={
                        "execution_contract": {
                            "execution_profile": profile,
                            "can_execute_without_intraday_trigger": True,
                            "requires_intraday_confirmation": False,
                        }
                    },
                )

                self.assertTrue(result.should_execute)
                self.assertEqual(result.base_price, 100.0)
                self.assertFalse(result.to_audit_payload()["trigger_checked"])

    def test_watch_still_requires_15m_trigger_and_missing_1m_never_fabricates_fill(self):
        watch = select_intraday_execution(
            signal_bars=_non_triggering_signal_bars(),
            execution_bars=_execution_bars(),
            action="open_long",
            config={"opening_range_minutes": 1, "min_execution_volume": 1},
            decision_context={
                "execution_contract": {
                    "execution_profile": "breakout",
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
                    "execution_profile": "breakout",
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
            analyst="fundamental",
            opportunity_state="tradeable_candidate",
            entry_trigger="current price confirms the fundamental setup",
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


if __name__ == "__main__":
    unittest.main()
