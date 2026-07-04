import unittest

from graph.constants import Signal
from graph.schema import AnalystSignal
from agents.decision_team.auditor import audit_futures_recommendation
from tools.agent_tools.analysis.analyst_quality import apply_trade_research_contract
from tools.agent_tools.analysis.analyst_signal_fusion import build_opportunity_scorecard
from tools.agent_tools.decision.pm_contract_builder import build_final_action_contract
from tools.common.evidence_fusion_semantics import build_reviewer_fusion_attribution
from tools.common.signal_evidence_collection import build_signal_collection_contract


class EvidenceFusionSemanticsTest(unittest.TestCase):
    def _analyst_signal(self, *, analyst: str, signal: Signal, confidence: float = 0.72) -> AnalystSignal:
        sig = AnalystSignal(
            agent_name=analyst,
            signal=signal,
            confidence=confidence,
            business_quality_score=0.74,
            evidence_quality="high",
            entry_trigger="current price/volume confirms directional setup",
            would_change_view_if="setup invalid if price closes back through trigger and opposite evidence appears",
            setup_type="trend_breakout",
            factor_focus=["price", "inventory"],
            current_evidence_conflict=[],
        )
        context = {
            "tradeability": "high",
            "setup_quality_ok": True,
            "freshness_score": 0.82,
            "market_regime": "trend",
            "sector": "ferrous",
            "required_confirmation": "same-day price and volume confirmation",
            "data_quality": {"coverage_ratio": 0.88, "freshness_score": 0.82},
        }
        if analyst == "technical":
            context.update({"dominant_direction": "bullish" if signal == Signal.BULLISH else "bearish"})
        if analyst == "fundamental":
            context.update({"fundamental_deployable_confirmed": True})
        if analyst == "commodity_news":
            context.update({"tradable_event": True, "price_reaction_confirmed": True, "impact_window": "event_short"})
        return apply_trade_research_contract(sig, context, analyst=analyst, trading_date="2025-05-06", ticker="RB")

    def test_analyst_landing_adds_fusion_evidence_without_trade_authority(self):
        signal = self._analyst_signal(analyst="technical", signal=Signal.BULLISH)
        contract = signal.metadata["action_evidence_contract"]
        fusion = contract["fusion_evidence"]
        self.assertEqual(fusion["contract_version"], "agentquant.evidence_fusion.v1")
        self.assertIn(signal.evidence_strength, {"strong", "medium", "weak", "unknown"})
        self.assertTrue(signal.confirmation_requirements)
        forbidden = {"target_lots", "lots_delta", "final_action", "final_action_contract", "authority_type"}
        self.assertFalse(forbidden.intersection(fusion))

    def test_signal_collector_outputs_fusion_without_trade_authority(self):
        signals = [
            self._analyst_signal(analyst="technical", signal=Signal.BULLISH),
            self._analyst_signal(analyst="fundamental", signal=Signal.BEARISH, confidence=0.68),
            self._analyst_signal(analyst="commodity_news", signal=Signal.BULLISH, confidence=0.64),
        ]
        contract = build_signal_collection_contract(
            ticker="RB",
            trading_date="2025-05-06",
            analyst_signals=signals,
            enabled_analysts=["technical", "fundamental", "commodity_news"],
        )
        self.assertEqual(contract["collector_decision_boundary"], "no_trade_authority")
        self.assertIn("evidence_fusion", contract)
        self.assertIn("cross_analyst_conflicts", contract)
        self.assertIn("evidence_strength_by_analyst", contract)
        forbidden = {"opportunity_score", "opportunity_rank", "target_lots", "lots_delta", "final_action_contract"}
        self.assertFalse(forbidden.intersection(contract))

    def test_pm_scorecard_and_auditor_preserve_fusion_boundary(self):
        signals = [
            self._analyst_signal(analyst="technical", signal=Signal.BULLISH),
            self._analyst_signal(analyst="fundamental", signal=Signal.BEARISH, confidence=0.68),
            self._analyst_signal(analyst="commodity_news", signal=Signal.BULLISH, confidence=0.64),
        ]
        collection = build_signal_collection_contract(
            ticker="RB",
            trading_date="2025-05-06",
            analyst_signals=signals,
            enabled_analysts=["technical", "fundamental", "commodity_news"],
        )
        scorecard = build_opportunity_scorecard(
            ticker="RB",
            analyst_signals=signals,
            market_confirmation={"confirmation_score": 0.74, "features": ["trend"], "conflicts": []},
            data_quality_summary={},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=[],
            signal_collection_contract=collection,
            decision_date="2025-05-06",
            config={},
        )
        side_row = scorecard["long"]
        side_row.update({
            "opportunity_rank": 1,
            "rank_capital_role": "best_exploration_probe_candidate",
            "capital_layer": "exploration_probe",
            "capital_ratio_source": "probe_margin_ratio_0.008",
            "rank_reason": "best_watch_for_trigger_by_evidence_trigger_learning_and_risk",
        })
        self.assertIn("pm_fusion_diagnostics", side_row)
        self.assertIn("pm_conflict_resolution", side_row)
        contract = build_final_action_contract(
            ticker="RB",
            current_lots=0,
            target_lots=1,
            position_ratio=0.01,
            margin_required=10000,
            account_equity=5000000,
            lots_to_trade=1,
            lots_to_trade_reason="unit_test",
            recommendation_intent={"action": "open_long", "lots": 1},
            final_entry_authority={
                "authority_type": "exploration_probe",
                "decision": "allow_exploration_probe",
                "rank_capital_priority_real_budget_release": False,
                "rank_capital_priority_release_detail": {
                    "decision": "reject",
                    "opportunity_rank": 1,
                    "scorecard_state": "watch_for_trigger",
                },
            },
            control_reasons=[],
            control_diagnostics={},
            opportunity_scorecard=scorecard,
            market_confirmation={"confirmation_score": 0.74},
            alpha_setup_action_values=[],
        )
        self.assertIn("pm_fusion_diagnostics", contract["evidence_used"])
        self.assertEqual(contract["evidence_used"]["rank_capital_role"], "best_exploration_probe_candidate")
        self.assertEqual(contract["evidence_used"]["capital_layer"], "exploration_probe")
        self.assertEqual(contract["evidence_used"]["capital_ratio_source"], "probe_margin_ratio_0.008")
        self.assertEqual(
            contract["evidence_used"]["rank_reason"],
            "best_watch_for_trigger_by_evidence_trigger_learning_and_risk",
        )
        self.assertFalse(contract["evidence_used"]["rank_capital_priority_real_budget_release"])
        self.assertEqual(
            contract["evidence_used"]["rank_capital_priority_release_detail"]["decision"],
            "reject",
        )
        recommendation = {
            "id": "rec-1",
            "source_type": "strategy",
            "underlying_code": "RB",
            "effective_trade_date": "2025-05-06",
            "signal_snapshot": {"final_action_contract": contract},
        }
        audit = audit_futures_recommendation(recommendation=recommendation, full_config={"max_total_margin_ratio": 0.20})
        self.assertIn("pm_fusion_explanation_audit", audit.audit_payload)
        self.assertTrue(audit.audit_payload["pm_fusion_explanation_audit"]["auditor_boundary"].startswith("audit_pm_contract"))

    def test_reviewer_fusion_attribution_is_read_only_learning_context(self):
        attribution = build_reviewer_fusion_attribution(
            {
                "final_action_contract": {
                    "evidence_used": {
                        "pm_fusion_diagnostics": {
                            "cross_analyst_conflict_count": 1,
                            "multi_evidence_consensus_score": 0.52,
                        },
                        "pm_conflict_resolution": {"handled": True},
                    }
                }
            }
        )
        self.assertEqual(attribution["fusion_attribution_label"], "fusion_conflict_handled")
        self.assertEqual(
            attribution["reviewer_boundary"],
            "read_only_attribution_no_action_value_write_no_trade_mutation",
        )

    def test_trader_and_accountant_do_not_directly_read_fusion_tool(self):
        paths = [
            "src/agents/execution_team/trader.py",
            "src/tools/agent_tools/execution/trader_futures_execution.py",
            "src/agents/execution_team/accountant.py",
            "src/tools/agent_tools/execution/accountant_futures_settlement.py",
        ]
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        for rel in paths:
            text = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn("evidence_fusion_semantics", text, rel)
            self.assertNotIn("build_pm_fusion_diagnostics", text, rel)


if __name__ == "__main__":
    unittest.main()
