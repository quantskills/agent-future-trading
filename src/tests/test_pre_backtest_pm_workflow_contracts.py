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

from agents.decision_team import portfolio_manager
from agents.decision_team.signal_collector import signal_collector_agent
from agents.decision_team.portfolio_manager import (
    _require_step6_signal_collection_contract,
    finalize_pm_full_market_contracts,
    portfolio_agent_futures,
)
from graph.constants import Signal
from graph.schema import AnalystSignal, FuturesRecommendation, Portfolio, RecommendationAction, TradingPhase
from graph.workflow import AgentWorkflow
from tests.contract_test_fixtures import build_test_aec
from tests.test_pm_atomic_contract_flow import _pm_state, _signal_collection_contract
from tools.agent_tools.decision.pm_contract_self_check import check_final_action_contract
from tools.common.evidence_fusion_semantics import build_analyst_fusion_evidence


def _portfolio() -> Portfolio:
    return Portfolio(
        id="p1",
        cashflow=1_000_000.0,
        account_equity=1_000_000.0,
        positions={},
    )


class PreBacktestPMWorkflowContractTests(unittest.TestCase):
    class _PMTestDB:
        def get_ticker_performance(self, **kwargs):
            return {}

        def get_futures_transaction_memory(self, *args, **kwargs):
            return []

        def get_alpha_setup_action_values(self, **kwargs):
            return []

        def get_similar_alpha_setup_action_values(self, **kwargs):
            return []

    def _freshness_signals(self, ticker: str, freshness_score: float) -> list[AnalystSignal]:
        signals = []
        for index, analyst in enumerate(("technical", "fundamental", "commodity_news"), start=1):
            technical = analyst == "technical"
            contract = build_test_aec(
                analyst,
                ticker=ticker,
                trading_date="2025-03-25",
                signal="Bullish",
                side="long",
                confidence=0.78,
                opportunity_state="tradeable_candidate" if technical else "no_opportunity",
                setup_type="trend_breakout_setup" if technical else "direction_context",
                setup_quality_ok=technical,
                trigger_valid=technical,
                current_trigger_confirmed=technical,
                invalidation_present=True,
                entry_trigger=None if technical else "",
                invalidation_condition="close_below_current_setup_boundary",
                extra={
                    "business_quality_score": 0.76,
                    "setup_quality_score": 0.76 if technical else 0.0,
                    "entry_timing_signal": "breakout" if technical else "",
                },
            )
            source = next(iter(contract["data_usage_summary"]["sources"].values()))
            if analyst in {"fundamental", "commodity_news"}:
                source["freshness_score"] = freshness_score
            if analyst == "commodity_news":
                source["relevance_score"] = 0.90
            source_signal = AnalystSignal(
                agent_name=analyst,
                signal=Signal.BULLISH,
                confidence=0.78,
                business_quality_score=0.76,
                setup_quality_score=0.76 if technical else 0.0,
                evidence_quality="high",
                metadata={"action_evidence_contract": contract},
            )
            fusion = build_analyst_fusion_evidence(
                source_signal,
                {"freshness_score": freshness_score},
                analyst=analyst,
                ticker=ticker,
            )
            contract["fusion_evidence"] = fusion
            contract["evidence_strength"] = fusion["evidence_strength"]
            contract["evidence_freshness"] = fusion["evidence_freshness"]
            contract["confirmation_requirements"] = fusion["confirmation_requirements"]
            signals.append(
                AnalystSignal(
                    agent_name=analyst,
                    signal=Signal.BULLISH,
                    confidence=0.78,
                    metadata={
                        "signal_record_id": f"signal-{index}-{freshness_score}",
                        "action_evidence_contract": contract,
                    },
                )
            )
        return signals

    def _freshness_pm_state(
        self,
        ticker: str,
        freshness_score: float,
        confirmation_floor: float,
    ) -> tuple[Portfolio, dict]:
        portfolio = Portfolio(
            id=f"portfolio-{freshness_score}",
            cashflow=5_000_000.0,
            account_equity=5_000_000.0,
            cash_available=5_000_000.0,
            margin_available=5_000_000.0,
            positions={},
        )
        signals = self._freshness_signals(ticker, freshness_score)
        full_config = {
            "cashflow": 5_000_000.0,
            "max_total_margin_ratio": 0.20,
            "max_single_margin_ratio": 0.15,
            "learning": {"enabled": False},
            "pm_risk_gate": {"enabled": False},
            "market_confirmation": {
                "enabled": True,
                "quality_gate_enabled": True,
                "min_confirmation_score_for_new_entry": confirmation_floor,
                "quality_gate_block_weak_signal": False,
                "quality_gate_cap_multiplier": 0.50,
            },
        }
        state = {
            "portfolio": portfolio,
            "ticker": ticker,
            "trading_date": datetime(2025, 3, 25),
            "analyst_signals": signals,
            "num_tickers": 1,
            "enabled_analysts": ["technical", "fundamental", "commodity_news"],
            "config_id": "cfg-freshness-chain",
            "phase": TradingPhase.PHASE1,
            "morning_price_context": SimpleNamespace(
                base_price=3500.0,
                base_price_source="t_minus_1_close_fallback",
                base_price_date="2025-03-24",
                open_price=None,
                prev_close_price=3500.0,
                warning_message=None,
                contract_code=f"{ticker}2506",
                contract_facts={
                    "contract_code": f"{ticker}2506",
                    "underlying_code": ticker,
                    "as_of_date": "2025-03-24",
                    "source": "test_visible_contract",
                },
            ),
            "config": full_config,
            "full_config": full_config,
            "router": None,
        }
        state.update(signal_collector_agent(state))
        return portfolio, state

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

    def test_freshness_reaches_scc_rank_and_final_target_lots(self):
        portfolio, fresh_state = self._freshness_pm_state("BU", 0.90, 0.0)
        _, stale_state = self._freshness_pm_state("C", 0.30, 0.0)
        fresh_consensus = fresh_state["signal_collection_contract"]["evidence_fusion"]["multi_evidence_consensus_score"]
        stale_consensus = stale_state["signal_collection_contract"]["evidence_fusion"]["multi_evidence_consensus_score"]
        self.assertGreater(fresh_consensus, stale_consensus)
        confirmation_floor = (fresh_consensus + stale_consensus) / 2.0
        fresh_state["full_config"]["market_confirmation"]["min_confirmation_score_for_new_entry"] = confirmation_floor
        stale_state["full_config"]["market_confirmation"]["min_confirmation_score_for_new_entry"] = confirmation_floor

        def run_pm(state):
            with patch(
                "agents.decision_team.portfolio_manager.get_db",
                return_value=self._PMTestDB(),
            ), patch(
                "agents.decision_team.portfolio_manager._sanitize_visible_text",
                return_value="Freshness chain deterministic regression.",
            ), patch(
                "agents.decision_team.portfolio_manager.FuturesContractInfoCache.get_contract_info",
                return_value={
                    "contract_multiplier": 10.0,
                    "margin_rate_long": 0.10,
                    "margin_rate_short": 0.10,
                },
            ):
                return portfolio_agent_futures(state)["pm_state"]

        fresh_pm = run_pm(fresh_state)
        stale_pm = run_pm(stale_state)
        signed = finalize_pm_full_market_contracts(
            generated=[("BU", fresh_pm), ("C", stale_pm)],
            config=fresh_state["full_config"],
            portfolio=portfolio,
        )
        contracts = {
            ticker: recommendation.signal_snapshot["final_action_contract"]
            for ticker, recommendation in signed
        }
        fresh_contract = contracts["BU"]
        stale_contract = contracts["C"]
        fresh_row = fresh_pm["opportunity_scorecard"]["long"]
        stale_row = stale_pm["opportunity_scorecard"]["long"]
        self.assertGreater(fresh_row["market_confirmation_score"], stale_row["market_confirmation_score"])
        self.assertGreater(
            fresh_row["rank_score_input_components"]["cold_start_evidence_quality"],
            stale_row["rank_score_input_components"]["cold_start_evidence_quality"],
        )
        self.assertGreater(fresh_row["rank_score"], stale_row["rank_score"])
        self.assertEqual(fresh_contract["capital_deployment"]["rank_budget_sequence"], 1)
        self.assertEqual(stale_contract["capital_deployment"]["rank_budget_sequence"], 2)
        self.assertLess(stale_contract["target_lots"], fresh_contract["target_lots"])
        self.assertGreater(fresh_contract["target_lots"], 0)

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
