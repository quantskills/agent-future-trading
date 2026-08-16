import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.decision_team.portfolio_manager import (
    RiskLevel,
    _build_pm_decision_context,
    finalize_pm_full_market_contracts,
    portfolio_agent_futures,
)
from graph.schema import AnalystSignal, Portfolio, Position, RecommendationSourceType, RecommendationStatus
from graph.workflow import AgentWorkflow
from tests.contract_test_fixtures import build_test_aec
from tools.agent_tools.decision.pm_contract_self_check import check_final_action_contract
from tools.common.final_action_semantics import full_market_rank_source_payload
from tools.common.signal_evidence_collection import (
    build_pm_evidence_signals_from_scc,
    build_signal_collection_contract,
)


def _signal_collection_contract(ticker: str, side: str = "long") -> dict:
    signal_value = "Bullish" if side == "long" else "Bearish"
    action_contract = build_test_aec(
        "technical",
        ticker=ticker,
        signal=signal_value,
        side=side,
        confidence=0.8,
        invalidation_condition="close_below_trigger",
        entry_trigger=(
            "15分钟收盘价向上突破开盘区间上沿且高于VWAP"
            if side == "long"
            else "15分钟收盘价向下突破开盘区间下沿且低于VWAP"
        ),
        extra={
            "invalidation_level": 2950.0,
            "atr_stop_distance": 40.0,
            "evidence_role": "entry_timing",
            "entry_timing_signal": "breakout",
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


def _pm_state(ticker: str, current_lots: int, target_lots: int, *, with_scorecard: bool) -> dict:
    side = "long" if target_lots > 0 else "short" if target_lots < 0 else "long"
    collection = _signal_collection_contract(ticker, side=side)
    scorecard = {}
    if with_scorecard:
        scorecard = {
            "preferred_side": side,
            side: {
                "side": side,
                "final_state": "watch_for_trigger",
                "opportunity_score": 0.8,
                "score": 0.8,
                "rank_score": 0.8,
                "capital_priority_score": 0.8,
                "watch_priority_score": 0.8,
                "capital_priority_tier": 1,
                "rank_score_components": {
                    "cold_start_evidence_quality": 0.8,
                    "open_add_action_value_delta": 0.0,
                },
                "rank_input_components": {
                    "rank_score": 0.8,
                    "capital_priority_score": 0.8,
                    "opportunity_score": 0.8,
                },
                "rank_capital_role": "exploration_probe",
                "capital_layer": "exploration",
                "capital_ratio_source": "probe_margin_ratio_0.008",
                "rank_reason": "fixture_rank",
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
                **full_market_rank_source_payload(),
            },
        }
    ratio = 0.008 if target_lots else 0.0
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
    authority = {
        "authority_type": "exploration_probe" if target_lots != current_lots else "not_applicable",
        "authority_decision": "fixture_state",
        "decision": "fixture_state",
        "requires_authority": target_lots != current_lots,
        "open_action_evidence": target_lots != current_lots,
        "conditional_trigger_authority": increases_risk,
        "requires_intraday_confirmation": increases_risk,
        "can_execute_without_intraday_trigger": False,
        "max_allowed_margin_ratio": abs(ratio),
        "reason_codes": ["fixture_state"],
    }
    execution_fields = _build_pm_decision_context(
        ticker=ticker,
        target_lots=target_lots,
        current_price=3000.0,
        position_ratio=ratio,
        risk_level=RiskLevel.SAFE,
        long_scores={"confidence": 0.8 if side == "long" else 0.0},
        short_scores={"confidence": 0.8 if side == "short" else 0.0},
        margin_rate=0.10,
        current_lots=current_lots,
        analyst_signals=build_pm_evidence_signals_from_scc(collection),
        final_entry_authority=authority,
        trading_date="2025-03-25",
        recommendation_intent={},
        control_reasons=["fixture_state"],
    )
    return {
        "ticker": ticker,
        "current_lots": current_lots,
        "target_lots": target_lots,
        "lots_delta": target_lots - current_lots,
        "lots_delta_abs": abs(target_lots - current_lots),
        "target_position_ratio": ratio,
        "target_margin_ratio_estimate": abs(ratio),
        "position_ratio": ratio,
        "margin_required": abs(ratio) * 1_000_000.0,
        "account_equity": 1_000_000.0,
        "lots_to_trade": abs(target_lots - current_lots),
        "lots_to_trade_reason": "fixture_state",
        "recommendation_intent": {},
        "final_entry_authority": authority,
        "control_reasons": ["fixture_state"],
        "reason_codes": ["fixture_state"],
        "control_diagnostics": {},
        "opportunity_scorecard": scorecard,
        "market_confirmation": {},
        "alpha_setup_action_values": [],
        "signal_collection_contract": collection,
        "execution_contract_fields": execution_fields,
        "recommendation_context": {
            "config_id": "cfg",
            "reference_portfolio_id": "p1",
            "trading_date": "2025-03-25",
            "effective_trade_date": "2025-03-25",
            "source_type": RecommendationSourceType.STRATEGY,
            "underlying_code": ticker,
            "contract_code": f"{ticker}2505",
            "base_price": 3000.0,
            "status": RecommendationStatus.PENDING,
            "justification": "fixture",
        },
    }


class PMAtomicContractFlowTests(unittest.TestCase):
    def test_pm_self_check_rejects_incomplete_or_misaligned_execution_facts(self):
        state = _pm_state("BU", 0, 1, with_scorecard=True)
        result = finalize_pm_full_market_contracts(
            generated=[("BU", state)],
            config={"max_total_margin_ratio": 0.2},
            portfolio=Portfolio(
                id="p1",
                cashflow=1_000_000.0,
                account_equity=1_000_000.0,
                positions={},
            ),
        )
        contract = result[0][1].signal_snapshot["final_action_contract"]
        mutations = (
            ({"setup_type": "direction_watchlist"}, "new_risk_setup_type_not_formal"),
            ({"execution_profile": "unknown"}, "execution_profile_not_canonical"),
            ({"trigger_source": ""}, "trigger_source_missing"),
            ({"trigger_source": "position_lifecycle"}, "execution_profile_trigger_source_mismatch"),
            ({"entry_trigger": ""}, "new_risk_execution_missing_entry_trigger"),
            (
                {
                    "invalidation": "",
                    "invalidation_level": None,
                },
                "new_risk_execution_missing_entry_invalidation",
            ),
            (
                {
                    "invalidation": "close below support",
                    "invalidation_level": 2950.0,
                },
                "execution_entry_invalidation_condition_invalid",
            ),
            (
                {
                    "position_invalidation_level": None,
                    "atr_stop_distance": None,
                    "exit_hint": "exit when thesis reverses",
                    "expected_horizon_days": 9,
                },
                "new_risk_execution_missing_position_exit_boundary",
            ),
        )
        for mutation, expected_error in mutations:
            with self.subTest(mutation=mutation):
                invalid = {**contract, **mutation}
                self.assertIn(expected_error, check_final_action_contract(invalid)["errors"])

    def test_technical_watch_survives_step5_and_step6_as_one_conditional_contract(self):
        analyst_signals = []
        for analyst in ("technical", "fundamental", "commodity_news"):
            directional = analyst == "technical"
            aec = build_test_aec(
                analyst,
                ticker="M",
                signal="Bullish" if directional else "Neutral",
                side="long" if directional else "flat",
                confidence=0.84 if directional else 0.40,
                opportunity_state="watch_for_trigger" if directional else "no_opportunity",
                setup_type="trend_breakout" if directional else "no_trade",
                trigger_valid=False,
                current_trigger_confirmed=False,
                invalidation_present=directional,
                entry_trigger=(
                    "15分钟收盘价向上突破开盘区间上沿且高于VWAP"
                    if directional
                    else ""
                ),
                invalidation_condition="15m close below 3420" if directional else None,
                extra={
                    "invalidation_level": 3420.0 if directional else None,
                    "atr_stop_distance": 36.0 if directional else None,
                    "evidence_role": (
                        "entry_timing"
                        if analyst == "technical"
                        else "direction_context"
                        if analyst == "fundamental"
                        else "event_catalyst"
                    ),
                    "entry_timing_signal": "breakout" if directional else "",
                },
            )
            analyst_signals.append(
                AnalystSignal(
                    agent_name=analyst,
                    metadata={
                        "action_evidence_contract": aec,
                        "signal_record_id": f"signal-M-{analyst}",
                    },
                )
            )
        scc = build_signal_collection_contract(
            ticker="M",
            trading_date="2025-03-26",
            analyst_signals=analyst_signals,
            enabled_analysts=("technical", "fundamental", "commodity_news"),
        )
        state = _pm_state("M", 0, 1, with_scorecard=True)
        authority = {
            **state["final_entry_authority"],
            "conditional_trigger_authority": True,
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
        }
        execution_fields = _build_pm_decision_context(
            ticker="M",
            target_lots=1,
            current_price=3480.0,
            position_ratio=0.008,
            risk_level=RiskLevel.SAFE,
            long_scores={"confidence": 0.84},
            short_scores={"confidence": 0.0},
            margin_rate=0.10,
            current_lots=0,
            analyst_signals=build_pm_evidence_signals_from_scc(scc),
            final_entry_authority=authority,
            trading_date="2025-03-26",
            recommendation_intent={"action": "open_long"},
            control_reasons=["conditional_trigger_authority"],
        )
        state.update(
            {
                "signal_collection_contract": scc,
                "execution_contract_fields": execution_fields,
                "final_entry_authority": authority,
                "control_reasons": ["conditional_trigger_authority"],
            }
        )

        result = finalize_pm_full_market_contracts(
            generated=[("M", state)],
            config={"max_total_margin_ratio": 0.2},
            portfolio=Portfolio(
                id="p1",
                cashflow=1_000_000.0,
                account_equity=1_000_000.0,
                positions={},
            ),
        )

        contract = result[0][1].signal_snapshot["final_action_contract"]
        self.assertEqual(contract["target_lots"], 1)
        self.assertTrue(contract["capital_deployment"]["selected_for_capital_deployment"])
        self.assertTrue(contract["conditional_trigger_authority"])
        self.assertTrue(contract["requires_intraday_confirmation"])
        self.assertFalse(contract["can_execute_without_intraday_trigger"])
        self.assertEqual(
            contract["entry_trigger"],
            "15分钟收盘价向上突破开盘区间上沿且高于VWAP",
        )
        self.assertEqual(contract["invalidation"], "long_price_lte_invalidation_level")
        self.assertEqual(contract["execution_profile"], "breakout")
        self.assertEqual(contract["trigger_source"], "technical_breakout")
        self.assertEqual(contract["invalidation_level"], 3420.0)
        self.assertEqual(contract["atr_stop_distance"], 36.0)
        self.assertTrue(check_final_action_contract(contract)["ok"])

    def test_step6_contract_has_final_execution_scope_without_internal_duplicates(self):
        state = _pm_state("BU", 0, 1, with_scorecard=True)
        result = finalize_pm_full_market_contracts(
            generated=[("BU", state)],
            config={"max_total_margin_ratio": 0.2},
            portfolio=Portfolio(
                id="p1",
                cashflow=1_000_000.0,
                account_equity=1_000_000.0,
                positions={},
            ),
        )
        contract = result[0][1].signal_snapshot["final_action_contract"]
        self.assertEqual(contract["contract_code"], "BU2505")
        self.assertEqual(contract["setup_type"], "trend_breakout_setup")
        self.assertEqual(contract["horizon_class"], "short")
        self.assertEqual(contract["expected_horizon_days"], 3)
        self.assertEqual(contract["market_regime"], "trend")
        self.assertEqual(contract["invalidation_level"], 2950.0)
        self.assertEqual(contract["atr_stop_distance"], 40.0)
        for duplicate in (
            "recommendation_intent",
            "action_candidates",
            "analyst_execution_roles",
            "risk_flags",
        ):
            self.assertNotIn(duplicate, contract)
        for rank_field in (
            "opportunity_rank",
            "rank_source",
            "rank_scope",
            "capital_layer",
            "capital_allocation_reason",
        ):
            self.assertNotIn(rank_field, contract["evidence_used"])
            self.assertIn(rank_field, contract["capital_deployment"])

    def test_learning_retrieval_starts_only_after_step3_lifecycle_port(self):
        source = (SRC_ROOT / "agents" / "decision_team" / "portfolio_manager.py").read_text(
            encoding="utf-8-sig"
        )
        main_chain = source[
            source.index("def _run_pm_six_step_decision") : source.index("def portfolio_agent_futures")
        ]
        step2_index = main_chain.index("ticker_side_selection_result = select_ticker_side(")
        step3_index = main_chain.index("primary_lifecycle_action_port = classify_lifecycle_action_port(")

        self.assertLess(step2_index, step3_index)
        self.assertGreater(main_chain.index("apply_config_learning_overlay("), step3_index)
        self.assertGreater(main_chain.index("retrieve_pm_memory("), step3_index)
        self.assertNotIn("_retrieve_lifecycle_pm_memory(", main_chain)
        self.assertIn("_audit_frozen_step4_pm_memory(", main_chain)
        freeze_index = main_chain.index('"formal_pool_frozen_after_scorecard"')
        self.assertNotIn("_append_unique_action_values(", main_chain[freeze_index:])
        self.assertNotIn("calibrate_weights_by_signal_history(", main_chain)

        side_selection_call = main_chain[step2_index:step3_index]
        for learning_input in (
            "effective_memory_summary=",
            "adaptive_policy_state=",
            "alpha_setup_profiles=",
            "alpha_setup_action_values=",
        ):
            self.assertNotIn(learning_input, side_selection_call)

    def test_position_sizing_is_built_only_by_step5(self):
        pm_source = (SRC_ROOT / "agents" / "decision_team" / "portfolio_manager.py").read_text(
            encoding="utf-8-sig"
        )
        main_chain = pm_source[
            pm_source.index("def _run_pm_six_step_decision") : pm_source.index("def portfolio_agent_futures")
        ]
        deployment_source = (
            SRC_ROOT / "tools" / "agent_tools" / "decision" / "pm_full_market_capital_deployment.py"
        ).read_text(encoding="utf-8-sig")

        self.assertNotIn("build_position_sizing_result(", main_chain)
        self.assertIn("build_position_sizing_result(", deployment_source)

    def test_pm_runtime_chain_has_no_physical_logger_calls(self):
        pm_source = (SRC_ROOT / "agents" / "decision_team" / "portfolio_manager.py").read_text(
            encoding="utf-8-sig"
        )
        decision_root = SRC_ROOT / "tools" / "agent_tools" / "decision"

        self.assertNotIn("from util.logger import logger", pm_source)
        self.assertNotIn("logger.", pm_source)
        for tool_path in decision_root.glob("pm_*.py"):
            with self.subTest(tool=tool_path.name):
                self.assertNotIn("logger.", tool_path.read_text(encoding="utf-8-sig"))

    def test_final_contract_self_check_accepts_only_the_final_contract(self):
        source = (
            SRC_ROOT / "tools" / "agent_tools" / "decision" / "pm_contract_self_check.py"
        ).read_text(encoding="utf-8-sig")
        signature = source[
            source.index("def check_final_action_contract(") : source.index(
                '    """Check final_action_contract consistency',
                source.index("def check_final_action_contract("),
            )
        ]

        self.assertNotIn("pm_artifact", signature)
        self.assertNotIn("snapshot", signature)

    def test_portfolio_node_returns_only_pm_memory_state_before_step6(self):
        memory_state = {"ticker": "BU", "current_lots": 0, "target_lots": 0}
        with patch(
            "agents.decision_team.portfolio_manager._run_pm_six_step_decision",
            return_value=memory_state,
        ):
            result = portfolio_agent_futures({})

        self.assertEqual(result, {"pm_state": memory_state})
        self.assertNotIn("recommendation", result)
        self.assertNotIn("final_action_contract", result)
        self.assertNotIn("signal_snapshot", result["pm_state"])

    def test_workflow_collects_pm_memory_state_not_recommendation(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        memory_state = {"ticker": "BU", "current_lots": 1, "target_lots": 1}

        observed = workflow._require_pm_memory_state("BU", {"pm_state": memory_state})

        self.assertIs(observed, memory_state)
        with self.assertRaisesRegex(RuntimeError, "missing pm_state"):
            workflow._require_pm_memory_state("BU", {"recommendation": object()})

    def test_steps_1_5_do_not_build_unsigned_recommendation_consistency(self):
        source = (SRC_ROOT / "agents" / "decision_team" / "portfolio_manager.py").read_text(
            encoding="utf-8-sig"
        )
        main_chain = source[
            source.index("def _run_pm_six_step_decision") : source.index("def portfolio_agent_futures")
        ]

        self.assertNotIn('plan_snapshot["recommendation_position_consistency"]', main_chain)

    def test_non_new_risk_skips_step5_and_step6_creates_only_final_output(self):
        state = _pm_state("BU", 1, 1, with_scorecard=False)
        result = finalize_pm_full_market_contracts(
            generated=[("BU", state)],
            config={"max_total_margin_ratio": 0.2},
            portfolio=Portfolio(id="p1", cashflow=1_000_000.0, account_equity=1_000_000.0, positions={}),
        )

        recommendation = result[0][1]
        contract = recommendation.signal_snapshot["final_action_contract"]
        self.assertEqual(contract["final_action"], "hold")
        self.assertNotIn("opportunity_rank", contract["evidence_used"])
        self.assertTrue(recommendation.signal_snapshot["pm_six_step_trace"]["pm_contract_self_check"]["ok"])

    def test_new_risk_runs_step5_before_step6_atomic_signing(self):
        state = _pm_state("BU", 0, 1, with_scorecard=True)
        result = finalize_pm_full_market_contracts(
            generated=[("BU", state)],
            config={
                "max_total_margin_ratio": 0.2,
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "max_single_ticker_margin_ratio": 0.13,
                },
                "net_exposure_control": {"max_net_exposure": 0.5},
            },
            portfolio=Portfolio(id="p1", cashflow=1_000_000.0, account_equity=1_000_000.0, positions={}),
        )

        recommendation = result[0][1]
        contract = recommendation.signal_snapshot["final_action_contract"]
        self.assertEqual(contract["final_action"], "open_probe")
        self.assertEqual(contract["capital_deployment"]["opportunity_rank"], 1)
        self.assertTrue(contract["capital_deployment"]["selected_for_capital_deployment"])
        self.assertEqual(contract["evidence_used"]["position_sizing_result"]["target_lots"], 1)

    def test_step5_rejection_restores_zero_new_exposure_before_step6(self):
        state = _pm_state("BU", 0, 1, with_scorecard=False)
        result = finalize_pm_full_market_contracts(
            generated=[("BU", state)],
            config={"max_total_margin_ratio": 0.2},
            portfolio=Portfolio(id="p1", cashflow=1_000_000.0, account_equity=1_000_000.0, positions={}),
        )

        recommendation = result[0][1]
        contract = recommendation.signal_snapshot["final_action_contract"]
        self.assertEqual(contract["final_action"], "wait")
        self.assertEqual(contract["target_lots"], 0)
        self.assertEqual(contract["lots_delta"], 0)
        self.assertFalse(contract["capital_deployment"]["selected_for_capital_deployment"])
        self.assertEqual(contract["evidence_used"]["position_sizing_result"]["target_lots"], 0)
        self.assertEqual(contract["evidence_used"]["position_sizing_result"]["lots_delta"], 0)

    def test_reverse_exits_old_side_before_any_new_risk_rank(self):
        state = _pm_state("BU", 1, -1, with_scorecard=True)
        result = finalize_pm_full_market_contracts(
            generated=[("BU", state)],
            config={"max_total_margin_ratio": 0.2},
            portfolio=Portfolio(id="p1", cashflow=1_000_000.0, account_equity=1_000_000.0, positions={}),
        )

        contract = result[0][1].signal_snapshot["final_action_contract"]
        self.assertEqual(contract["final_action"], "exit")
        self.assertEqual(contract["target_lots"], 0)
        self.assertNotIn("opportunity_rank", contract["evidence_used"])
        self.assertIn("reverse_exit_first", contract["reason_codes"])

    def test_native_wait_reduce_and_exit_skip_step5_without_rank(self):
        cases = (
            ("WAIT", 0, 0, "wait"),
            ("REDUCE", 2, 1, "reduce"),
            ("EXIT", 1, 0, "exit"),
        )
        for ticker, current_lots, target_lots, expected_action in cases:
            with self.subTest(ticker=ticker):
                state = _pm_state(ticker, current_lots, target_lots, with_scorecard=False)
                result = finalize_pm_full_market_contracts(
                    generated=[(ticker, state)],
                    config={"max_total_margin_ratio": 0.2},
                    portfolio=Portfolio(
                        id="p1",
                        cashflow=1_000_000.0,
                        account_equity=1_000_000.0,
                        positions={},
                    ),
                )
                contract = result[0][1].signal_snapshot["final_action_contract"]
                self.assertEqual(contract["final_action"], expected_action)
                self.assertNotIn("opportunity_rank", contract["evidence_used"])

    def test_add_and_scale_enter_incremental_risk_rank(self):
        cases = (
            ("ADD_LONG", 1, 2),
            ("ADD_SHORT", -1, -2),
        )
        for ticker, current_lots, target_lots in cases:
            with self.subTest(ticker=ticker):
                state = _pm_state(ticker, current_lots, target_lots, with_scorecard=True)
                result = finalize_pm_full_market_contracts(
                    generated=[(ticker, state)],
                    config={"max_total_margin_ratio": 0.2},
                    portfolio=Portfolio(
                        id="p1",
                        cashflow=1_000_000.0,
                        account_equity=1_000_000.0,
                        positions={},
                    ),
                )

                contract = result[0][1].signal_snapshot["final_action_contract"]
                self.assertEqual(contract["final_action"], "scale")
                self.assertEqual(contract["current_lots"], current_lots)
                self.assertEqual(contract["target_lots"], target_lots)
                self.assertEqual(contract["capital_deployment"]["opportunity_rank"], 1)
                self.assertTrue(contract["capital_deployment"]["selected_for_capital_deployment"])

    def test_open_and_add_compete_in_same_full_market_rank(self):
        open_state = _pm_state("OPEN", 0, 1, with_scorecard=True)
        add_state = _pm_state("ADD", 1, 2, with_scorecard=True)
        add_state["opportunity_scorecard"]["long"].update(
            {
                "rank_score": 0.9,
                "capital_priority_score": 0.9,
                "opportunity_score": 0.9,
                "score": 0.9,
            }
        )

        result = finalize_pm_full_market_contracts(
            generated=[("ADD", add_state), ("OPEN", open_state)],
            config={"max_total_margin_ratio": 0.2},
            portfolio=Portfolio(
                id="p1",
                cashflow=1_000_000.0,
                account_equity=1_000_000.0,
                positions={},
            ),
        )

        contracts = {ticker: rec.signal_snapshot["final_action_contract"] for ticker, rec in result}
        self.assertEqual(contracts["ADD"]["capital_deployment"]["opportunity_rank"], 1)
        self.assertEqual(contracts["OPEN"]["capital_deployment"]["opportunity_rank"], 2)

    def test_ranked_add_rejection_restores_existing_position_to_hold(self):
        state = _pm_state("BU", 2, 3, with_scorecard=True)
        result = finalize_pm_full_market_contracts(
            generated=[("BU", state)],
            config={
                "max_total_margin_ratio": 0.2,
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "max_single_ticker_margin_ratio": 0.001,
                },
                "net_exposure_control": {"max_net_exposure": 0.5},
            },
            portfolio=Portfolio(
                id="p1",
                cashflow=900_000.0,
                account_equity=1_000_000.0,
                margin_used=100_000.0,
                positions={
                    "BU": Position(
                        shares=2,
                        value=100_000.0,
                        margin_used=20_000.0,
                        margin_rate=0.1,
                    )
                },
            ),
        )

        contract = result[0][1].signal_snapshot["final_action_contract"]
        self.assertEqual(contract["final_action"], "hold")
        self.assertEqual(contract["current_lots"], 2)
        self.assertEqual(contract["target_lots"], 2)
        self.assertEqual(contract["lots_delta"], 0)
        self.assertEqual(contract["capital_deployment"]["opportunity_rank"], 1)
        self.assertFalse(contract["capital_deployment"]["selected_for_capital_deployment"])

    def test_add_budget_consumes_only_incremental_margin(self):
        state = _pm_state("BU", 1, 2, with_scorecard=True)
        state.update(
            {
                "target_margin_ratio_estimate": 0.06,
                "position_ratio": 0.12,
                "target_position_ratio": 0.12,
                "margin_required": 60_000.0,
            }
        )
        result = finalize_pm_full_market_contracts(
            generated=[("BU", state)],
            config={
                "max_total_margin_ratio": 0.2,
                "capital_utilization_control": {"target_margin_ratio_confirmed": 0.2},
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "max_single_ticker_margin_ratio": 0.13,
                },
                "net_exposure_control": {"max_net_exposure": 0.5},
            },
            portfolio=Portfolio(
                id="p1",
                cashflow=810_000.0,
                account_equity=1_000_000.0,
                margin_used=190_000.0,
                positions={
                    "BU": Position(
                        shares=1,
                        value=100_000.0,
                        margin_used=50_000.0,
                        margin_rate=0.5,
                    )
                },
            ),
        )

        contract = result[0][1].signal_snapshot["final_action_contract"]
        deployment = contract["capital_deployment"]
        self.assertEqual(contract["final_action"], "scale")
        self.assertTrue(deployment["selected_for_capital_deployment"])
        self.assertAlmostEqual(deployment["candidate_margin_ratio"], 0.06)
        self.assertAlmostEqual(deployment["queue_margin_ratio_before"], 0.19)
        self.assertAlmostEqual(deployment["queue_margin_ratio_after_if_selected"], 0.20)

    def test_ranked_candidate_rejected_by_budget_keeps_rank_and_zeroes_new_risk(self):
        state = _pm_state("BU", 0, 1, with_scorecard=True)
        result = finalize_pm_full_market_contracts(
            generated=[("BU", state)],
            config={
                "max_total_margin_ratio": 0.2,
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "max_single_ticker_margin_ratio": 0.001,
                },
                "net_exposure_control": {"max_net_exposure": 0.5},
            },
            portfolio=Portfolio(id="p1", cashflow=1_000_000.0, account_equity=1_000_000.0, positions={}),
        )

        contract = result[0][1].signal_snapshot["final_action_contract"]
        self.assertEqual(contract["final_action"], "wait")
        self.assertEqual(contract["target_lots"], 0)
        self.assertEqual(contract["capital_deployment"]["opportunity_rank"], 1)
        self.assertFalse(contract["capital_deployment"]["selected_for_capital_deployment"])
        self.assertTrue(
            str(contract["capital_deployment"]["capital_allocation_reason"]).startswith(
                "no_rank_or_budget_no_new_exposure"
            )
        )

    def test_alpha_scale_rank_precedes_real_budget_and_consumes_budget_first(self):
        real_state = _pm_state("ZN", 0, 1, with_scorecard=True)
        alpha_state = _pm_state("BU", 0, 1, with_scorecard=True)
        for state in (real_state, alpha_state):
            state["final_entry_authority"].update(
                {
                    "authority_type": "real_budget_entry",
                    "max_allowed_margin_ratio": 0.06,
                }
            )
            row = state["opportunity_scorecard"]["long"]
            row["final_state"] = "tradeable_candidate"
            row["opportunity_score"] = 0.8
            row["score"] = 0.8
        alpha_state["control_diagnostics"] = {
            "capital_utilization_target": {
                "high_quality_memory": True,
                "target_mode": "alpha_release_boost",
            }
        }

        result = finalize_pm_full_market_contracts(
            generated=[("ZN", real_state), ("BU", alpha_state)],
            config={
                "max_total_margin_ratio": 0.2,
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "max_single_ticker_margin_ratio": 0.13,
                },
                "capital_utilization_control": {"target_margin_ratio_confirmed": 0.10},
                "net_exposure_control": {"max_net_exposure": 0.008},
            },
            portfolio=Portfolio(
                id="p1",
                cashflow=1_000_000.0,
                account_equity=1_000_000.0,
                positions={},
            ),
        )

        contracts = {ticker: rec.signal_snapshot["final_action_contract"] for ticker, rec in result}
        self.assertEqual(contracts["BU"]["capital_deployment"]["opportunity_rank"], 1)
        self.assertTrue(contracts["BU"]["capital_deployment"]["selected_for_capital_deployment"])
        self.assertEqual(contracts["ZN"]["capital_deployment"]["opportunity_rank"], 2)
        self.assertFalse(contracts["ZN"]["capital_deployment"]["selected_for_capital_deployment"])

    def test_rank_preserves_probe_and_real_budget_authority(self):
        probe_state = _pm_state("BU", 0, 1, with_scorecard=True)
        real_state = _pm_state("ZN", 0, 1, with_scorecard=True)
        probe_row = probe_state["opportunity_scorecard"]["long"]
        probe_row.update({"final_state": "tradeable_candidate", "opportunity_score": 0.99, "score": 0.99})
        probe_state["final_entry_authority"].update(
            {"authority_type": "exploration_probe", "max_allowed_margin_ratio": 0.015}
        )
        real_row = real_state["opportunity_scorecard"]["long"]
        real_row.update({"final_state": "watch_for_trigger", "opportunity_score": 0.20, "score": 0.20})
        real_state["final_entry_authority"].update(
            {"authority_type": "real_budget_entry", "max_allowed_margin_ratio": 0.06}
        )
        real_state.update(
            {
                "target_position_ratio": 0.03,
                "target_margin_ratio_estimate": 0.03,
                "position_ratio": 0.03,
                "margin_required": 30_000.0,
            }
        )

        result = finalize_pm_full_market_contracts(
            generated=[("BU", probe_state), ("ZN", real_state)],
            config={
                "max_total_margin_ratio": 0.20,
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "max_single_ticker_margin_ratio": 0.13,
                },
                "capital_utilization_control": {"target_margin_ratio_confirmed": 0.10},
                "net_exposure_control": {"max_net_exposure": 0.50},
            },
            portfolio=Portfolio(
                id="p1",
                cashflow=1_000_000.0,
                account_equity=1_000_000.0,
                positions={},
            ),
        )

        contracts = {ticker: rec.signal_snapshot["final_action_contract"] for ticker, rec in result}
        self.assertEqual(contracts["ZN"]["capital_deployment"]["opportunity_rank"], 1)
        self.assertEqual(contracts["ZN"]["capital_deployment"]["capital_layer"], "real_budget_entry")
        self.assertEqual(contracts["BU"]["capital_deployment"]["opportunity_rank"], 2)
        self.assertEqual(contracts["BU"]["capital_deployment"]["capital_layer"], "exploration_probe")
        self.assertEqual(probe_state["final_entry_authority"]["authority_type"], "exploration_probe")
        self.assertEqual(probe_state["final_entry_authority"]["max_allowed_margin_ratio"], 0.015)

    def test_equal_rank_inputs_use_normalized_ticker_as_stable_final_key(self):
        zn_state = _pm_state("zn", 0, 1, with_scorecard=True)
        bu_state = _pm_state("bu", 0, 1, with_scorecard=True)

        result = finalize_pm_full_market_contracts(
            generated=[("zn", zn_state), ("bu", bu_state)],
            config={
                "max_total_margin_ratio": 0.2,
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "max_single_ticker_margin_ratio": 0.13,
                },
                "net_exposure_control": {"max_net_exposure": 0.5},
            },
            portfolio=Portfolio(
                id="p1",
                cashflow=1_000_000.0,
                account_equity=1_000_000.0,
                positions={},
            ),
        )

        contracts = {ticker.upper(): rec.signal_snapshot["final_action_contract"] for ticker, rec in result}
        self.assertEqual(contracts["BU"]["capital_deployment"]["opportunity_rank"], 1)
        self.assertEqual(contracts["ZN"]["capital_deployment"]["opportunity_rank"], 2)


if __name__ == "__main__":
    unittest.main()
