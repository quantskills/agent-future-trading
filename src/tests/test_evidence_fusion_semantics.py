import unittest
import sys
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.constants import Signal
from graph.schema import AnalystSignal
from agents.decision_team.auditor import audit_futures_recommendation
from tools.agent_tools.analysis.analyst_quality import apply_trade_research_contract
from tools.agent_tools.decision.pm_signal_fusion import build_opportunity_scorecard
from tools.agent_tools.decision.pm_ticker_side_selection import select_ticker_side
from tools.agent_tools.decision.pm_contract_builder import build_final_action_contract
from tools.agent_tools.decision.pm_lifecycle_learning_router import route_lifecycle_learning
from tools.agent_tools.decision.pm_lifecycle_action_port import classify_lifecycle_action_port
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
        self.assertIn("cross_analyst_conflicts", contract["evidence_fusion"])
        self.assertIn("evidence_strength_by_analyst", contract["evidence_fusion"])
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
        self.assertIn("pm_fusion_diagnostics", side_row)
        self.assertIn("pm_conflict_resolution", side_row)
        self.assertIn("analyst_direction_evidence", side_row)
        self.assertIn("direction_evidence_strength", side_row)
        self.assertNotIn("side_priority", side_row)
        self.assertNotIn("opportunity_rank", side_row)
        selected = select_ticker_side(
            ticker="RB",
            analyst_signals=signals,
            signal_collection_contract=collection,
            market_confirmation={"confirmation_score": 0.74, "features": ["trend"], "conflicts": []},
            data_quality_summary={},
            decision_date="2025-05-06",
            config={},
            prebuilt_scorecard=scorecard,
        )
        self.assertIn("side_priority", selected["opportunity_scorecard"]["long"])
        selected["opportunity_scorecard"]["long"]["capital_priority_score"] = 0.99
        selected["opportunity_scorecard"]["long"]["capital_priority_tier"] = 3
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
            opportunity_scorecard=selected["opportunity_scorecard"],
            market_confirmation={"confirmation_score": 0.74},
            alpha_setup_action_values=[],
        )
        self.assertIn("pm_fusion_diagnostics", contract["evidence_used"])
        self.assertIn("analyst_direction_evidence", contract["evidence_used"])
        self.assertIn("side_priority", contract["evidence_used"])
        self.assertIn("candidate_quality", contract["evidence_used"])
        self.assertIn("candidate_layer_hint", contract["evidence_used"])
        self.assertNotIn("capital_priority_score", contract["evidence_used"])
        self.assertNotIn("capital_priority_tier", contract["evidence_used"])
        self.assertNotIn("opportunity_rank", contract["evidence_used"])
        self.assertNotIn("rank_capital_role", contract["evidence_used"])
        self.assertNotIn("capital_layer", contract["evidence_used"])
        self.assertNotIn("capital_ratio_source", contract["evidence_used"])
        self.assertNotIn("rank_reason", contract["evidence_used"])
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

    def test_final_contract_preserves_execution_trigger_profile_learning_route(self):
        action_values = [
            {
                "id": "hold-1",
                "ticker": "RB",
                "side": "long",
                "action_name": "hold",
                "canonical_action_family": "hold",
                "action_value_lane": "hold",
                "action_preference": "positive_candidate_hold",
                "reward_mean": 0.11,
                "sample_count": 3,
            },
            {
                "id": "open-1",
                "ticker": "RB",
                "side": "long",
                "action_name": "add_or_open",
                "canonical_action_family": "open_add_new_risk",
                "action_value_lane": "open",
                "action_preference": "positive_candidate_open",
                "reward_mean": 0.18,
                "sample_count": 6,
            },
            {
                "id": "exec-1",
                "ticker": "RB",
                "side": "long",
                "action_name": "execution",
                "canonical_action_family": "execution",
                "action_value_lane": "execution",
                "action_preference": "positive_candidate_execution",
                "reward_mean": 0.09,
                "sample_count": 4,
            },
        ]
        router = route_lifecycle_learning(lifecycle_port="hold", action_values=action_values)
        primary_port = classify_lifecycle_action_port({
            "current_lots": 1,
            "target_lots": 1,
            "final_action": "hold",
        })
        contract = build_final_action_contract(
            ticker="RB",
            current_lots=0,
            target_lots=1,
            position_ratio=0.008,
            margin_required=8000,
            account_equity=5000000,
            lots_to_trade=1,
            lots_to_trade_reason="unit_test",
            recommendation_intent={"action": "open_long", "lots": 1},
            final_entry_authority={"authority_type": "exploration_probe", "decision": "allow_exploration_probe"},
            control_reasons=[],
            control_diagnostics={
                "primary_lifecycle_action_port": primary_port,
                "pm_lifecycle_learning_router": router,
                "alpha_setup_ev_fusion": {
                    "rank_score_open_add_learning_delta": 0.031,
                    "learning_impact_delta": 0.031,
                },
            },
            opportunity_scorecard={
                "preferred_side": "long",
                "long": {
                    "score": 0.62,
                    "opportunity_score": 0.62,
                    "final_state": "probe_candidate",
                    "opportunity_score_components": {
                        "positive_learning": 0.031,
                        "execution_profile_learning": 0.07,
                    },
                },
            },
            market_confirmation={"confirmation_score": 0.70},
            alpha_setup_action_values=action_values,
        )
        trace = contract["learning_used"]["pm_lifecycle_learning_trace"]
        self.assertEqual(trace["contract_lifecycle_port"], "open_add_new_risk")
        self.assertNotIn("primary_lifecycle_action_port", trace)
        self.assertNotIn("contract_lifecycle_self_check", trace)
        self.assertNotIn("lifecycle_port_transition_reason", trace)
        self.assertNotIn("lifecycle_transition_diagnostic", trace)
        self.assertNotIn("lifecycle_transition_reason", trace)
        self.assertEqual([row["id"] for row in trace["decision_learning_rows"]], ["open-1"])
        self.assertEqual([row["id"] for row in trace["trigger_profile_learning"]], ["exec-1"])
        self.assertNotIn("hold-1", {row.get("id") for row in trace["decision_learning_rows"]})
        self.assertNotIn("exec-1", {row.get("id") for row in trace["rejected_learning"]})
        self.assertFalse(trace["execution_profile_learning_direct_to_rank"])
        self.assertEqual(
            contract["learning_used"]["pm_lifecycle_learning_router"]["pm_lifecycle_action_port"],
            "open_add_new_risk",
        )
        impact = contract["learning_used"]["pm_lifecycle_learning_impact_delta"]
        self.assertEqual(impact["open_add_rank_score_delta"], 0.031)
        self.assertFalse(impact["execution_profile_learning_direct_to_rank"])

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
