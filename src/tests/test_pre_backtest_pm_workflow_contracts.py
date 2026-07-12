import inspect
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.decision_team import portfolio_manager
from agents.decision_team.portfolio_manager import (
    _require_step6_signal_collection_contract,
    finalize_pm_full_market_contracts,
)
from graph.schema import FuturesRecommendation, Portfolio, RecommendationAction
from graph.workflow import AgentWorkflow
from tests.test_pm_atomic_contract_flow import _pm_state, _signal_collection_contract
from tools.agent_tools.decision.pm_contract_self_check import check_final_action_contract


def _portfolio() -> Portfolio:
    return Portfolio(
        id="p1",
        cashflow=1_000_000.0,
        account_equity=1_000_000.0,
        positions={},
    )


class PreBacktestPMWorkflowContractTests(unittest.TestCase):
    def test_three_pm_paths_sign_exactly_one_final_contract(self):
        states = [
            ("HOLD", _pm_state("HOLD", 1, 1, with_scorecard=False)),
            ("OPEN", _pm_state("OPEN", 0, 1, with_scorecard=True)),
            ("REJECT", _pm_state("REJECT", 0, 1, with_scorecard=False)),
        ]
        result = finalize_pm_full_market_contracts(
            generated=states,
            config={
                "max_total_margin_ratio": 0.2,
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "max_single_ticker_margin_ratio": 0.13,
                },
                "net_exposure_control": {"max_net_exposure": 0.5},
            },
            portfolio=_portfolio(),
        )

        signed = dict(result)
        self.assertEqual(set(signed), {"HOLD", "OPEN", "REJECT"})
        self.assertEqual(signed["HOLD"].signal_snapshot["final_action_contract"]["final_action"], "hold")
        self.assertEqual(signed["OPEN"].signal_snapshot["final_action_contract"]["final_action"], "open_probe")
        self.assertEqual(signed["REJECT"].signal_snapshot["final_action_contract"]["final_action"], "wait")
        for recommendation in signed.values():
            snapshot = recommendation.signal_snapshot
            self.assertEqual(
                set(snapshot["pm_six_step_trace"]),
                {"step6_contract_generation_check", "pm_contract_self_check"},
            )

    def test_step6_self_check_reads_only_final_contract(self):
        state = _pm_state("BU", 1, 1, with_scorecard=False)
        result = finalize_pm_full_market_contracts(
            generated=[("BU", state)],
            config={"max_total_margin_ratio": 0.2},
            portfolio=_portfolio(),
        )
        contract = result[0][1].signal_snapshot["final_action_contract"]
        contract = deepcopy(contract)
        contract.setdefault("evidence_used", {})["historical_lifecycle_transition_diagnostic"] = {
            "ok": False,
            "diagnostic_only": True,
        }

        checked = check_final_action_contract(contract)

        self.assertTrue(checked["ok"], checked["errors"])

    def test_workflow_persists_only_step6_recommendations(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        saved = []

        class _DB:
            def save_futures_recommendation(self, recommendation):
                saved.append(recommendation)
                return f"saved-{recommendation.underlying_code}"

        workflow.db = _DB()
        workflow.config = {"max_total_margin_ratio": 0.2}
        workflow.init_portfolio = _portfolio()
        signed = FuturesRecommendation(
            underlying_code="BU",
            action=RecommendationAction.HOLD,
            signal_snapshot={
                "final_action_contract": {"ticker": "BU"},
                "pm_six_step_trace": {
                    "step6_contract_generation_check": {"ok": True},
                    "pm_contract_self_check": {"ok": True},
                },
            },
        )
        with patch(
            "agents.decision_team.portfolio_manager.finalize_pm_full_market_contracts",
            return_value=[("BU", signed)],
        ):
            returned = workflow._persist_pm_full_market_contracts(
                [("BU", _pm_state("BU", 1, 1, with_scorecard=False))]
            )

        self.assertEqual(returned, [("BU", signed)])
        self.assertEqual(saved, [signed])

    def test_workflow_blocks_invalid_step6_batch_before_save(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        saved = []

        class _DB:
            def save_futures_recommendation(self, recommendation):
                saved.append(recommendation)
                return "saved"

        workflow.db = _DB()
        workflow.config = {}
        workflow.init_portfolio = _portfolio()
        invalid = FuturesRecommendation(underlying_code="BU", signal_snapshot={})
        with patch(
            "agents.decision_team.portfolio_manager.finalize_pm_full_market_contracts",
            return_value=[("BU", invalid)],
        ):
            with self.assertRaisesRegex(RuntimeError, "missing signed final_action_contract"):
                workflow._persist_pm_full_market_contracts(
                    [("BU", _pm_state("BU", 1, 1, with_scorecard=False))]
                )

        self.assertEqual(saved, [])

    def test_workflow_blocks_failed_step6_self_check_before_save(self):
        workflow = AgentWorkflow.__new__(AgentWorkflow)
        saved = []

        class _DB:
            def save_futures_recommendation(self, recommendation):
                saved.append(recommendation)
                return "saved"

        workflow.db = _DB()
        workflow.config = {}
        workflow.init_portfolio = _portfolio()
        invalid = FuturesRecommendation(
            underlying_code="BU",
            signal_snapshot={
                "final_action_contract": {"ticker": "BU"},
                "pm_six_step_trace": {
                    "step6_contract_generation_check": {"ok": True},
                    "pm_contract_self_check": {
                        "ok": False,
                        "errors": ["capital_deployment_missing"],
                    },
                },
            },
        )
        with patch(
            "agents.decision_team.portfolio_manager.finalize_pm_full_market_contracts",
            return_value=[("BU", invalid)],
        ):
            with self.assertRaisesRegex(RuntimeError, "self-check not ok"):
                workflow._persist_pm_full_market_contracts(
                    [("BU", _pm_state("BU", 1, 1, with_scorecard=False))]
                )

        self.assertEqual(saved, [])

    def test_scc_source_agent_and_boundary_are_hard_inputs(self):
        valid = _signal_collection_contract("BU")
        self.assertIs(_require_step6_signal_collection_contract(valid), valid)
        with self.assertRaisesRegex(ValueError, "source_agent"):
            _require_step6_signal_collection_contract({**valid, "source_agent": "portfolio_manager"})
        with self.assertRaisesRegex(ValueError, "boundary"):
            _require_step6_signal_collection_contract({**valid, "collector_decision_boundary": "trade_authority"})

    def test_pm_source_has_no_signal_collection_builder_or_old_middle_outputs(self):
        source = inspect.getsource(portfolio_manager)
        self.assertNotIn("build_signal_collection_contract(", source)
        for legacy_name in (
            "pm_internal_candidate",
            "pm_internal_candidate_contract",
            "final_contract_builder_inputs",
            "pm_capital_deployment_decision",
        ):
            self.assertNotIn(legacy_name, source)


if __name__ == "__main__":
    unittest.main()
