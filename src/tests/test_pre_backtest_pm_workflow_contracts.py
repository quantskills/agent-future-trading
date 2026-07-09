import inspect
import sys
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.constants import Signal
from graph.schema import (
    AnalystSignal,
    FuturesRecommendation,
    Portfolio,
    RecommendationAction,
    RecommendationStatus,
)
from graph.workflow import AgentWorkflow
from agents.decision_team import portfolio_manager
from agents.decision_team.portfolio_manager import (
    _sign_pm_candidate_recommendation,
    finalize_pm_full_market_contracts,
    portfolio_agent_futures,
)
from tools.agent_tools.decision.pm_contract_self_check import check_final_action_contract
from tools.agent_tools.decision.pm_full_market_capital_deployment import (
    CAPITAL_LAYER_EXPLORATION,
    RANK_CAPITAL_ROLE_EXPLORATION,
)
from tools.agent_tools.decision.pm_lifecycle_action_port import classify_lifecycle_action_port
from tools.common.final_action_semantics import full_market_rank_source_payload
from tools.common.order_semantics import recommendation_intent_from_lots


def _signal_collection_contract(ticker: str = "BU") -> dict:
    return {
        "contract_version": "agentquant.signal_collection.v1",
        "producer": "signal_collector",
        "collector_decision_boundary": "no_trade_authority",
        "ticker": ticker,
        "trading_date": "2025-03-25",
        "source_contracts": [],
    }


def _rank_scorecard(ticker: str = "BU", score: float = 0.72) -> dict:
    return {
        "preferred_side": "long",
        "long": {
            "final_state": "watch_for_trigger",
            "opportunity_score": score,
            "score": score,
            "rank_score": score,
            "capital_priority_score": score,
            "watch_priority_score": score,
            "rank_score_components": {
                "cold_start_evidence_quality": score,
                "open_add_action_value_delta": 0.0,
            },
            "capital_priority_tier": 1,
            "rank_input_components": {
                "rank_score": score,
                "capital_priority_score": score,
                "opportunity_score": score,
            },
            "rank_capital_role": RANK_CAPITAL_ROLE_EXPLORATION,
            "capital_layer": CAPITAL_LAYER_EXPLORATION,
            "capital_ratio_source": "probe_margin_ratio_0.008",
            "rank_reason": f"{ticker}_best_watch_for_trigger_fixture",
            "lifecycle_learning_trace": {
                "rank_lifecycle": "open_add_new_risk",
                "used_lanes": ["open"],
                "execution_profile_signal_direct_to_rank": False,
            },
            "learning_impact_delta": {
                "net_rank_learning_delta": 0.0,
                "execution_profile_learning_direct_to_rank": False,
            },
            **full_market_rank_source_payload(),
        },
    }


def _pm_internal_candidate(
    contract: dict,
    *,
    ticker: str = "BU",
    scorecard: dict | None = None,
    execution_fields: dict | None = None,
    control_diagnostics: dict | None = None,
) -> dict:
    contract = dict(contract or {})
    current_lots = int(contract.get("current_lots") or 0)
    target_lots = int(contract.get("target_lots") or 0)
    position_ratio = float(
        contract.get("target_position_ratio")
        if contract.get("target_position_ratio") is not None
        else contract.get("target_margin_ratio_estimate") or 0.0
    )
    if contract.get("authority_type") == "exploration_probe" and not position_ratio:
        position_ratio = 0.008 if target_lots >= 0 else -0.008
        contract["target_position_ratio"] = position_ratio
        contract["target_margin_ratio_estimate"] = 0.008
    primary = classify_lifecycle_action_port(contract)
    diagnostics = dict(control_diagnostics or {})
    diagnostics.setdefault("primary_lifecycle_action_port", primary)
    fields = dict(execution_fields or {})
    fields.setdefault("pm_six_step_stage", "steps_1_4_candidate_generated")
    fields.setdefault("primary_lifecycle_action_port", primary)
    fields.setdefault("signal_collection_contract", _signal_collection_contract(ticker))
    authority = {
        "authority_type": contract.get("authority_type") or "not_applicable",
        "decision": contract.get("authority_decision") or contract.get("decision") or "fixture_candidate",
        "authority_decision": contract.get("authority_decision") or contract.get("decision") or "fixture_candidate",
        "reason_codes": list(contract.get("reason_codes") or []),
        "requires_authority": bool(target_lots != current_lots and target_lots != 0),
        "open_action_evidence": bool(contract.get("open_action_evidence") or target_lots != current_lots),
        "strong_current_evidence": bool(contract.get("strong_current_evidence")),
        "tradeable_state": bool(contract.get("tradeable_state")),
        "conditional_trigger_authority": bool(contract.get("conditional_trigger_authority")),
        "requires_intraday_confirmation": bool(contract.get("requires_intraday_confirmation")),
        "can_execute_without_intraday_trigger": bool(contract.get("can_execute_without_intraday_trigger")),
        "max_allowed_margin_ratio": float(contract.get("max_allowed_margin_ratio") or abs(position_ratio) or 0.0),
    }
    return {
        "schema": "agentquant.pm_internal_candidate.v1",
        "stage": "steps_1_4_complete_pending_full_market_deployment",
        "candidate_status": "normal",
        "candidate_contract": contract,
        "final_contract_builder_inputs": {
            "ticker": ticker,
            "current_lots": current_lots,
            "target_lots": target_lots,
            "position_ratio": position_ratio,
            "margin_required": abs(position_ratio) * 1_000_000.0,
            "account_equity": 1_000_000.0,
            "lots_to_trade": abs(target_lots - current_lots),
            "lots_to_trade_reason": ",".join(str(item) for item in (contract.get("reason_codes") or []) if item)
            or "fixture_candidate",
            "recommendation_intent": recommendation_intent_from_lots(current_lots, target_lots),
            "final_entry_authority": authority,
            "control_reasons": list(contract.get("reason_codes") or []),
            "control_diagnostics": diagnostics,
            "opportunity_scorecard": dict(scorecard or {}),
            "market_confirmation": {},
            "alpha_setup_action_values": [],
            "execution_contract_fields": fields,
        },
        "final_action_contract_signed": False,
    }


def _recommendation(
    ticker: str,
    contract: dict,
    *,
    scorecard: dict | None = None,
    execution_fields: dict | None = None,
    control_diagnostics: dict | None = None,
) -> FuturesRecommendation:
    return FuturesRecommendation(
        id=f"rec-{ticker}",
        underlying_code=ticker,
        trading_date="2025-03-25",
        base_price=3000.0,
        status=RecommendationStatus.PENDING,
        action=RecommendationAction.OPEN_LONG if int(contract.get("target_lots") or 0) > 0 else RecommendationAction.HOLD,
        lots=abs(int(contract.get("target_lots") or 0) - int(contract.get("current_lots") or 0)),
        signal_snapshot={
            "opportunity_scorecard": dict(scorecard or {}),
            "signal_collection_contract": _signal_collection_contract(ticker),
            "pm_internal_candidate": _pm_internal_candidate(
                contract,
                ticker=ticker,
                scorecard=scorecard,
                execution_fields=execution_fields,
                control_diagnostics=control_diagnostics,
            ),
        },
    )


def _portfolio() -> Portfolio:
    return Portfolio(
        id="p1",
        cashflow=5_000_000.0,
        account_equity=5_000_000.0,
        cash_available=5_000_000.0,
        positions={},
        margin_used=0.0,
    )


def _config(**overrides) -> dict:
    config = {
        "max_total_margin_ratio": 0.20,
        "position_budget_policy": {
            "min_real_trade_margin_ratio": 0.008,
            "max_single_ticker_margin_ratio": 0.13,
        },
        "capital_utilization_control": {"target_margin_ratio_confirmed": 0.20},
        "net_exposure_control": {"max_net_exposure": 0.50},
    }
    config.update(overrides)
    return config


class PreBacktestPMWorkflowContractGateTest(unittest.TestCase):
    def test_pm_three_contract_matrix_non_rank_deployed_and_undeployed_paths(self):
        non_rank = _recommendation(
            "BU",
            {
                "ticker": "BU",
                "current_lots": 1,
                "target_lots": 1,
                "lots_delta": 0,
                "final_action": "hold",
                "reason_codes": ["fixture_hold"],
            },
        )
        deployed = _recommendation(
            "P",
            {
                "ticker": "P",
                "current_lots": 0,
                "target_lots": 1,
                "lots_delta": 1,
                "final_action": "open_probe",
                "authority_type": "exploration_probe",
                "reason_codes": ["fixture_open_probe"],
                "target_margin_ratio_estimate": 0.008,
                "target_position_ratio": 0.008,
                "requires_intraday_confirmation": False,
            },
            scorecard=_rank_scorecard("P", 0.85),
        )
        undeployed = _recommendation(
            "M",
            {
                "ticker": "M",
                "current_lots": 0,
                "target_lots": 1,
                "lots_delta": 1,
                "final_action": "open_probe",
                "authority_type": "exploration_probe",
                "reason_codes": ["fixture_open_probe"],
                "target_margin_ratio_estimate": 0.008,
                "target_position_ratio": 0.008,
                "requires_intraday_confirmation": True,
                "conditional_trigger_authority": True,
            },
            scorecard=_rank_scorecard("M", 0.70),
        )

        finalize_pm_full_market_contracts(
            generated=[("BU", non_rank), ("P", deployed), ("M", undeployed)],
            config=_config(
                capital_utilization_control={"target_margin_ratio_confirmed": 0.008},
                net_exposure_control={"max_net_exposure": 0.008},
            ),
            portfolio=_portfolio(),
        )

        non_rank_contract = non_rank.signal_snapshot["final_action_contract"]
        self.assertEqual(
            non_rank_contract["capital_deployment"]["capital_allocation_reason"],
            "non_new_risk_no_capital_rank",
        )
        self.assertFalse(non_rank_contract["capital_deployment"]["new_risk_rank_required"])
        self.assertNotIn("opportunity_rank", non_rank_contract["evidence_used"])
        self.assertTrue(non_rank.signal_snapshot["pm_six_step_trace"]["pm_contract_self_check"]["ok"])
        self.assertEqual(non_rank.signal_snapshot["signal_collection_contract"]["producer"], "signal_collector")
        self.assertEqual(
            non_rank.signal_snapshot["signal_collection_contract"]["collector_decision_boundary"],
            "no_trade_authority",
        )

        deployed_contract = deployed.signal_snapshot["final_action_contract"]
        deployed_evidence = deployed_contract["evidence_used"]
        deployed_deployment = deployed_contract["capital_deployment"]
        self.assertTrue(deployed_deployment["selected_for_capital_deployment"])
        for field in (
            "opportunity_rank",
            "rank_source",
            "rank_input_components",
            "rank_capital_role",
            "capital_layer",
            "capital_ratio_source",
            "rank_reason",
        ):
            self.assertIn(field, deployed_evidence)
            self.assertIn(field, deployed_deployment)
        self.assertIn("rank_score", deployed_evidence["rank_input_components"])
        self.assertIn("rank_score", deployed_deployment["rank_input_components"])
        self.assertTrue(deployed.signal_snapshot["pm_six_step_trace"]["step6_contract_generation_check"]["ok"])

        undeployed_contract = undeployed.signal_snapshot["final_action_contract"]
        undeployed_deployment = undeployed_contract["capital_deployment"]
        self.assertIn(undeployed_contract["final_action"], {"wait", "hold"})
        self.assertEqual(undeployed_contract["target_lots"], undeployed_contract["current_lots"])
        self.assertEqual(undeployed_contract["lots_delta"], 0)
        self.assertFalse(undeployed_contract.get("requires_intraday_confirmation"))
        self.assertFalse(undeployed_contract.get("conditional_trigger_authority"))
        self.assertFalse(undeployed_deployment["selected_for_capital_deployment"])
        self.assertTrue(
            str(undeployed_deployment["capital_allocation_reason"]).startswith(
                "no_rank_or_budget_no_new_exposure"
            )
            or str(undeployed_deployment["capital_allocation_reason"]).startswith("no_rank_no_new_exposure")
        )
        self.assertTrue(undeployed.signal_snapshot["pm_six_step_trace"]["step6_contract_generation_check"]["ok"])

    def test_step6_lifecycle_uses_final_candidate_and_keeps_old_check_historical(self):
        old_primary = {
            "pm_lifecycle_action_port": "new_risk",
            "requires_full_market_rank": True,
            "current_lots": 0,
            "target_lots": 1,
            "lots_delta": 1,
        }
        old_failed_check = {
            "tool": "pm_lifecycle_action_port",
            "diagnostic_type": "lifecycle_transition_diagnostic",
            "ok": False,
            "transition_reason": "unexplained_lifecycle_port_transition",
        }
        recommendation = _recommendation(
            "ZN",
            {
                "ticker": "ZN",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "final_action": "wait",
                "reason_codes": ["risk_gate_flat_target_no_new_exposure"],
            },
            execution_fields={
                "primary_lifecycle_action_port": old_primary,
                "lifecycle_transition_diagnostic": old_failed_check,
            },
        )

        self.assertTrue(_sign_pm_candidate_recommendation(recommendation))

        contract = recommendation.signal_snapshot["final_action_contract"]
        evidence = contract["evidence_used"]
        self.assertEqual(
            contract["capital_deployment"]["capital_allocation_reason"],
            "non_new_risk_no_capital_rank",
        )
        self.assertNotIn("historical_lifecycle_transition_diagnostic", evidence)
        self.assertNotIn("contract_lifecycle_self_check", evidence)
        self.assertNotIn("lifecycle_transition_diagnostic", evidence)
        self.assertTrue(recommendation.signal_snapshot["pm_six_step_trace"]["step6_contract_generation_check"]["ok"])
        self.assertNotIn("pm_capital_deployment_decision", recommendation.signal_snapshot)

    def test_final_contract_self_check_does_not_require_legacy_lifecycle_self_check(self):
        base = _recommendation(
            "BU",
            {
                "ticker": "BU",
                "current_lots": 1,
                "target_lots": 1,
                "lots_delta": 0,
                "final_action": "hold",
                "reason_codes": ["fixture_hold"],
            },
        )
        self.assertTrue(_sign_pm_candidate_recommendation(base))
        contract = deepcopy(base.signal_snapshot["final_action_contract"])
        contract["evidence_used"].pop("lifecycle_transition_diagnostic", None)
        missing = check_final_action_contract(contract)
        self.assertTrue(missing["ok"], missing["errors"])

        contract["evidence_used"]["lifecycle_transition_diagnostic"] = {
            "tool": "pm_lifecycle_action_port",
            "diagnostic_type": "lifecycle_transition_diagnostic",
            "ok": False,
            "transition_reason": "unexplained_lifecycle_port_transition",
        }
        failed = check_final_action_contract(contract)
        self.assertTrue(failed["ok"], failed["errors"])

    def test_workflow_persistence_gate_blocks_invalid_batch_before_any_save(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        saved = []

        class _DB:
            def save_futures_recommendation(self, recommendation):
                saved.append(recommendation)
                return f"saved-{len(saved)}"

        workflow.db = _DB()
        workflow.config = _config()
        workflow.init_portfolio = _portfolio()
        valid = _recommendation(
            "BU",
            {
                "ticker": "BU",
                "current_lots": 1,
                "target_lots": 1,
                "lots_delta": 0,
                "final_action": "hold",
                "reason_codes": ["fixture_hold"],
            },
        )
        invalid = _recommendation(
            "M",
            {
                "ticker": "M",
                "current_lots": 1,
                "target_lots": 1,
                "lots_delta": 0,
                "final_action": "hold",
                "reason_codes": ["fixture_hold"],
            },
        )

        def fake_finalize(*, generated, config, portfolio):
            for _, recommendation in generated:
                recommendation.signal_snapshot = {
                    "final_action_contract": {
                        "final_action": "hold",
                        "current_lots": 1,
                        "target_lots": 1,
                        "lots_delta": 0,
                    },
                    "pm_six_step_trace": {
                        "pm_contract_self_check": {"ok": True},
                        "step6_contract_generation_check": {"ok": True},
                    },
                }
            invalid.signal_snapshot["pm_internal_candidate"] = {"leaked": True}

        with patch(
            "agents.decision_team.portfolio_manager.finalize_pm_full_market_contracts",
            side_effect=fake_finalize,
        ):
            with self.assertRaisesRegex(RuntimeError, "PM internal candidate remained"):
                workflow._persist_pm_full_market_contracts([("BU", valid), ("M", invalid)])
        self.assertEqual(saved, [])

    def test_workflow_persistence_gate_rejects_missing_contract_middle_state_and_failed_self_check(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        cases = [
            ("missing", {}, "missing signed final_action_contract"),
            (
                "candidate",
                {
                    "final_action_contract": {"final_action": "hold"},
                    "pm_internal_candidate": {"leaked": True},
                    "pm_six_step_trace": {
                        "pm_contract_self_check": {"ok": True},
                        "step6_contract_generation_check": {"ok": True},
                    },
                },
                "PM internal candidate remained",
            ),
            (
                "deployment",
                {
                    "final_action_contract": {"final_action": "hold"},
                    "pm_capital_deployment_decision": {"leaked": True},
                    "pm_six_step_trace": {
                        "pm_contract_self_check": {"ok": True},
                        "step6_contract_generation_check": {"ok": True},
                    },
                },
                "PM capital deployment decision remained",
            ),
            (
                "failed_check",
                {
                    "final_action_contract": {"final_action": "hold"},
                    "pm_six_step_trace": {
                        "pm_contract_self_check": {"ok": False},
                        "step6_contract_generation_check": {"ok": True},
                    },
                },
                "self-check not ok",
            ),
            (
                "failed_generation_check",
                {
                    "final_action_contract": {"final_action": "hold"},
                    "pm_six_step_trace": {
                        "pm_contract_self_check": {"ok": True},
                        "step6_contract_generation_check": {"ok": False},
                    },
                },
                "step6 contract generation check not ok",
            ),
        ]
        for ticker, snapshot, pattern in cases:
            with self.subTest(ticker=ticker):
                rec = FuturesRecommendation(underlying_code=ticker, signal_snapshot=deepcopy(snapshot))
                with self.assertRaisesRegex(RuntimeError, pattern):
                    workflow._assert_pm_signed_recommendation_for_persistence(ticker, rec)

    def test_pm_requires_signal_collection_contract_from_signal_collector(self):
        portfolio = Portfolio(
            id="portfolio-1",
            cashflow=1_000_000.0,
            account_equity=1_000_000.0,
            cash_available=1_000_000.0,
            positions={},
            margin_used=0.0,
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
            "morning_price_context": SimpleNamespace(base_price=500.0, prev_close_price=500.0),
            "config": {"max_total_margin_ratio": 0.20, "max_single_margin_ratio": 0.12},
            "full_config": {"learning": {"enabled": False}},
            "router": None,
        }

        with self.assertRaisesRegex(RuntimeError, "pm_missing_signal_collection_contract_from_signal_collector"):
            portfolio_agent_futures(state)

        state["signal_collection_contract"] = {
            **_signal_collection_contract("J"),
            "producer": "portfolio_manager",
        }
        with self.assertRaisesRegex(RuntimeError, "pm_invalid_signal_collection_contract_producer"):
            portfolio_agent_futures(state)

        state["signal_collection_contract"] = {
            **_signal_collection_contract("J"),
            "collector_decision_boundary": "trade_authority",
        }
        with self.assertRaisesRegex(RuntimeError, "pm_invalid_signal_collection_contract_boundary"):
            portfolio_agent_futures(state)

    def test_pm_does_not_import_or_call_signal_collection_builder(self):
        source = inspect.getsource(portfolio_manager)
        self.assertNotIn("from tools.common.signal_evidence_collection import build_signal_collection_contract", source)
        self.assertNotIn("build_signal_collection_contract(", source)

    def test_rank_and_middle_state_field_boundaries_are_hard_checked(self):
        non_rank = _recommendation(
            "BU",
            {
                "ticker": "BU",
                "current_lots": 1,
                "target_lots": 1,
                "lots_delta": 0,
                "final_action": "hold",
                "reason_codes": ["fixture_hold"],
            },
        )
        self.assertTrue(_sign_pm_candidate_recommendation(non_rank))
        non_rank_contract = non_rank.signal_snapshot["final_action_contract"]
        for field in ("opportunity_rank", "rank_input_components", "rank_score"):
            self.assertNotIn(field, non_rank_contract)
            self.assertNotIn(field, non_rank_contract["evidence_used"])
            self.assertNotIn(field, non_rank_contract["capital_deployment"])
        for field in ("pm_internal_candidate", "pm_internal_candidate_contract", "pm_capital_deployment_decision"):
            self.assertNotIn(field, non_rank.signal_snapshot)

        ranked = _recommendation(
            "P",
            {
                "ticker": "P",
                "current_lots": 0,
                "target_lots": 1,
                "lots_delta": 1,
                "final_action": "open_probe",
                "authority_type": "exploration_probe",
                "reason_codes": ["fixture_open_probe"],
                "target_margin_ratio_estimate": 0.008,
                "target_position_ratio": 0.008,
            },
            scorecard=_rank_scorecard("P", 0.85),
        )
        finalize_pm_full_market_contracts(
            generated=[("P", ranked)],
            config=_config(),
            portfolio=_portfolio(),
        )
        missing_rank_trace = deepcopy(ranked.signal_snapshot["final_action_contract"])
        missing_rank_trace["evidence_used"].pop("rank_input_components", None)
        missing_rank_trace["capital_deployment"].pop("rank_input_components", None)
        result = check_final_action_contract(missing_rank_trace)
        self.assertFalse(result["ok"])
        self.assertTrue(any("rank_input_components_missing" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
