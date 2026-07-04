import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.agent_tools.decision.pm_decision_memory_retrieval import retrieve_pm_memory
from tools.agent_tools.decision.pm_opportunity_ranking import (
    CAPITAL_LAYER_ALPHA_SCALE,
    CAPITAL_LAYER_EXPLORATION,
    CAPITAL_RATIO_SOURCE_ALPHA_SCALE,
    CAPITAL_RATIO_SOURCE_EXPLORATION,
    CAPITAL_PRIORITY_RANK_MEANING,
    CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION,
    RANK_CAPITAL_ROLE_ALPHA_SCALE,
    RANK_CAPITAL_ROLE_EXPLORATION,
    RANK_CAPITAL_ROLE_REAL_BUDGET,
    rank_opportunities,
)
from tools.agent_tools.decision.pm_position_sizing import build_position_sizing_result
from tools.common.signal_evidence_collection import build_signal_collection_contract


class FakeMemoryDB:
    def get_alpha_setup_action_values(self, **kwargs):
        return [
            {
                "ticker": kwargs["ticker"],
                "side": kwargs["side"],
                "consumer_scope": "pm_learning",
                "horizon_class": kwargs.get("horizon_class") or "short",
                "market_regime": kwargs.get("market_regime") or "trend",
                "setup_type": kwargs.get("setup_type") or "trend_breakout",
                "action_value_lane": "open",
            },
            {
                "id": "real-bu-short-profit",
                "ticker": kwargs["ticker"],
                "side": kwargs["side"],
                "consumer_scope": "pm_learning",
                "horizon_class": kwargs.get("horizon_class") or "short",
                "market_regime": kwargs.get("market_regime") or "trend",
                "setup_type": kwargs.get("setup_type") or "trend_breakout",
                "action_value_lane": "open",
                "action_preference": "positive_candidate_open",
                "reward_source": "trade_episode",
                "evidence_scope": "exact_real_state",
                "reward_sum": 5200.0,
                "reward_mean": 5200.0,
                "sample_count": 1,
                "last_sample_date": "2025-03-04",
                "canonical_action_value": True,
            },
        ]


def _signal(agent_name: str, signal: Signal, confidence: float, **contract_overrides) -> AnalystSignal:
    contract = {
        "signal": signal.value,
        "side": "long" if signal == Signal.BULLISH else "short" if signal == Signal.BEARISH else "neutral",
        "confidence": confidence,
        "opportunity_state": "tradeable_candidate",
        "trigger_valid": True,
        "current_trigger_confirmed": True,
        "setup_type": "trend_breakout",
        "setup_quality_ok": True,
        "horizon_class": "short",
        "market_regime": "trend",
        "evidence_quality": "high",
        "invalidation_present": True,
        "invalidation_condition": "invalid if price closes back into range",
    }
    contract.update(contract_overrides)
    return AnalystSignal(
        agent_name=agent_name,
        signal=signal,
        confidence=confidence,
        metadata={"action_evidence_contract": contract},
    )


class DecisionWorkflowToolTest(unittest.TestCase):
    def test_signal_collector_preserves_source_evidence_without_trade_authority(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-05",
            analyst_signals=[
                _signal("technical", Signal.BEARISH, 0.72),
                _signal("fundamental", Signal.BEARISH, 0.66),
                _signal("commodity_news", Signal.NEUTRAL, 0.40, opportunity_state="watch_for_trigger"),
            ],
            enabled_analysts=["technical", "fundamental", "commodity_news"],
        )

        self.assertEqual(contract["contract_version"], "agentquant.signal_collection.v1")
        self.assertEqual(contract["collector_decision_boundary"], "no_trade_authority")
        self.assertEqual(contract["dominant_side"], "short")
        self.assertEqual(len(contract["source_contracts"]), 3)
        self.assertEqual(len(contract["evidence_items"]), 3)
        for forbidden in ("target_lots", "lots_delta", "final_action", "target_position_ratio"):
            self.assertNotIn(forbidden, contract)

    def test_signal_collector_does_not_read_action_value_or_generate_trade_action(self):
        source = (SRC_ROOT / "agents" / "decision_team" / "signal_collector.py").read_text(encoding="utf-8-sig")
        collection_source = (SRC_ROOT / "tools" / "common" / "signal_evidence_collection.py").read_text(encoding="utf-8-sig")
        for forbidden in (
            "get_alpha_setup_action_values",
            "get_similar_alpha_setup_action_values",
            "retrieve_pm_memory",
            "final_action_contract",
        ):
            self.assertNotIn(forbidden, source)
        contract = build_signal_collection_contract(
            ticker="RB",
            trading_date="2025-03-04",
            analyst_signals=[_signal("technical", Signal.BULLISH, 0.7)],
            enabled_analysts=["technical", "fundamental", "commodity_news"],
        )
        self.assertTrue(contract["no_trade_authority"])
        self.assertEqual(contract["collector_decision_boundary"], "no_trade_authority")
        for forbidden in ("final_action", "target_lots", "lots_delta", "margin_required", "authority_type"):
            self.assertNotIn(forbidden, contract)
            self.assertNotIn(f'"{forbidden}"', collection_source)

    def test_memory_retrieval_real_history_not_blocked_by_empty_history(self):
        result = retrieve_pm_memory(
            db=FakeMemoryDB(),
            config_id="cfg",
            ticker="BU",
            side="short",
            trading_date="2025-03-05",
            horizon_class="short",
            market_regime="trend",
            setup_type="trend_breakout",
            limit=3,
        )

        selected = result["action_values"]
        self.assertTrue(selected)
        self.assertEqual(selected[0]["id"], "real-bu-short-profit")
        self.assertEqual(selected[0]["action_preference"], "positive_candidate_open")
        self.assertTrue(result["effective_memory_summary"]["empty_history_cannot_block_real_history"])
        self.assertGreaterEqual(result["effective_memory_summary"]["empty_shell_count"], 1)
        self.assertIn(
            "empty_shell_downgraded_not_blocking",
            {item["reason"] for item in result["rejected_or_downgraded"]},
        )

    def test_opportunity_ranking_ranks_without_trade_authority(self):
        signal = _signal("technical", Signal.BULLISH, 0.74)
        result = rank_opportunities(
            ticker="RB",
            analyst_signals=[signal],
            signal_collection_contract={
                "dominant_side": "long",
                "side_consensus": "single_side",
                "trigger_status": "confirmed",
                "evidence_strength": "high",
                "evidence_conflict_level": "low",
            },
            effective_memory_summary={"status": "empty"},
            market_confirmation={"confirmation_score": 0.72},
            data_quality_summary={},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=[],
            decision_date="2025-03-05",
            config={"weak_confirmation_threshold": 0.45},
        )

        self.assertIn("opportunity_scorecard", result)
        self.assertIn("opportunity_rank", result)
        self.assertEqual(result["rank_semantics_version"], CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION)
        self.assertEqual(result["opportunity_rank_meaning"], CAPITAL_PRIORITY_RANK_MEANING)
        self.assertTrue(result["rank_is_capital_priority"])
        self.assertTrue(result["capital_allocation_reason"]["rank_is_not_trade_authority"])
        self.assertTrue(result["capital_allocation_reason"]["rank_is_capital_priority"])
        row = result["opportunity_scorecard"]["long"]
        self.assertEqual(row["rank_semantics_version"], CAPITAL_PRIORITY_RANK_SEMANTICS_VERSION)
        self.assertEqual(row["opportunity_rank_meaning"], CAPITAL_PRIORITY_RANK_MEANING)
        self.assertTrue(row["rank_is_capital_priority"])
        self.assertTrue(row["rank_is_not_trade_authority"])
        self.assertIn("capital_priority_score", row)
        self.assertTrue(result["ranking_tool_trace"]["no_llm"])

    def test_opportunity_ranking_uses_single_capital_priority_rank(self):
        result = rank_opportunities(
            ticker="EB",
            analyst_signals=[],
            signal_collection_contract={"dominant_side": "short"},
            effective_memory_summary={"status": "available"},
            market_confirmation={},
            data_quality_summary={},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=[],
            decision_date="2025-03-05",
            config={},
            prebuilt_scorecard={
                "preferred_side": "short",
                "long": {
                    "side": "long",
                    "score": 0.95,
                    "opportunity_score": 0.95,
                    "capital_priority_score": 0.99,
                    "capital_priority_tier": 1,
                    "final_state": "watch_for_trigger",
                },
                "short": {
                    "side": "short",
                    "score": 0.74,
                    "opportunity_score": 0.74,
                    "capital_priority_score": 0.50,
                    "capital_priority_tier": 3,
                    "final_state": "tradeable_candidate",
                },
            },
        )

        scorecard = result["opportunity_scorecard"]
        self.assertEqual(scorecard["short"]["opportunity_rank"], 1)
        self.assertEqual(scorecard["long"]["opportunity_rank"], 2)
        self.assertEqual(scorecard["short"]["rank_capital_role"], RANK_CAPITAL_ROLE_REAL_BUDGET)
        self.assertEqual(scorecard["long"]["rank_capital_role"], RANK_CAPITAL_ROLE_EXPLORATION)
        self.assertNotIn("deployment_rank", scorecard["short"])
        self.assertNotIn("exploration_rank", scorecard["short"])
        self.assertEqual(
            result["capital_allocation_reason"]["preferred_capital_priority_score"],
            0.50,
        )
        self.assertEqual(result["capital_allocation_reason"]["preferred_capital_layer"], "real_budget_entry")

    def test_all_watch_for_trigger_candidates_rank_by_single_probe_priority(self):
        result = rank_opportunities(
            ticker="P",
            analyst_signals=[],
            signal_collection_contract={"dominant_side": "long"},
            effective_memory_summary={"status": "available"},
            market_confirmation={},
            data_quality_summary={},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=[],
            decision_date="2025-03-05",
            config={},
            prebuilt_scorecard={
                "preferred_side": "long",
                "long": {
                    "side": "long",
                    "score": 0.48,
                    "opportunity_score": 0.48,
                    "capital_priority_score": 0.31,
                    "capital_priority_tier": 1,
                    "final_state": "watch_for_trigger",
                    "trigger_valid": True,
                    "entry_trigger": {"rule": "breakout_confirm"},
                    "invalidation": {"rule": "close_back_below_range"},
                    "opportunity_score_components": {"positive_learning": 0.04, "fusion_conflict_adjustment": 0.0},
                },
                "short": {
                    "side": "short",
                    "score": 0.51,
                    "opportunity_score": 0.51,
                    "capital_priority_score": 0.34,
                    "capital_priority_tier": 1,
                    "final_state": "watch_for_trigger",
                    "opportunity_score_components": {"positive_learning": 0.0, "fusion_conflict_adjustment": -0.05},
                    "gating_failures": ["missing_invalidation"],
                },
            },
        )

        scorecard = result["opportunity_scorecard"]
        self.assertEqual(scorecard["long"]["opportunity_rank"], 1)
        self.assertEqual(scorecard["short"]["opportunity_rank"], 2)
        self.assertEqual(scorecard["long"]["rank_capital_role"], RANK_CAPITAL_ROLE_EXPLORATION)
        self.assertEqual(scorecard["long"]["capital_layer"], CAPITAL_LAYER_EXPLORATION)
        self.assertEqual(scorecard["long"]["capital_ratio_source"], CAPITAL_RATIO_SOURCE_EXPLORATION)
        self.assertEqual(
            scorecard["long"]["rank_reason"],
            "best_watch_for_trigger_by_evidence_trigger_learning_and_risk",
        )

    def test_repeated_alpha_candidate_uses_same_rank_with_alpha_scale_layer(self):
        result = rank_opportunities(
            ticker="EB",
            analyst_signals=[],
            signal_collection_contract={"dominant_side": "short"},
            effective_memory_summary={"status": "available"},
            market_confirmation={},
            data_quality_summary={},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=[],
            decision_date="2025-03-05",
            config={},
            prebuilt_scorecard={
                "preferred_side": "short",
                "short": {
                    "side": "short",
                    "score": 0.77,
                    "opportunity_score": 0.77,
                    "capital_priority_score": 0.89,
                    "capital_priority_tier": 3,
                    "final_state": "tradeable_candidate",
                    "alpha_scale_candidate": True,
                },
                "long": {
                    "side": "long",
                    "score": 0.33,
                    "opportunity_score": 0.33,
                    "capital_priority_score": 0.30,
                    "capital_priority_tier": 1,
                    "final_state": "watch_for_trigger",
                },
            },
        )

        scorecard = result["opportunity_scorecard"]
        self.assertEqual(scorecard["short"]["opportunity_rank"], 1)
        self.assertEqual(scorecard["short"]["rank_capital_role"], RANK_CAPITAL_ROLE_ALPHA_SCALE)
        self.assertEqual(scorecard["short"]["capital_layer"], CAPITAL_LAYER_ALPHA_SCALE)
        self.assertEqual(scorecard["short"]["capital_ratio_source"], CAPITAL_RATIO_SOURCE_ALPHA_SCALE)
        self.assertNotIn("alpha_rank", scorecard["short"])
        self.assertNotIn("deployment_rank", scorecard["short"])

    def test_position_sizing_records_math_without_final_action_authority(self):
        result = build_position_sizing_result(
            ticker="RB",
            current_lots=2,
            target_lots=5,
            target_position_ratio=0.05,
            target_value=100000.0,
            margin_required=12000.0,
            account_equity=200000.0,
            margin_rate=0.12,
            current_net_exposure=0.03,
            projected_net_exposure=0.05,
            current_ticker_exposure=0.02,
            max_position_ratio=0.08,
            max_net_exposure=0.50,
            risk_level="SAFE",
            lots_to_trade_reason="target_plan",
            control_reasons=["scorecard_current_tradeable_probe_seed"],
            capital_allocation_reason={"preferred_side": "long"},
        )

        self.assertEqual(result["lots_delta"], 3)
        self.assertEqual(result["target_lots"], 5)
        self.assertTrue(result["no_final_action_authority"])
        self.assertTrue(result["no_direction_override_authority"])
        self.assertTrue(result["no_llm"])

    def test_portfolio_manager_no_llm_call_site_remains(self):
        text = (PROJECT_ROOT / "src" / "agents" / "decision_team" / "portfolio_manager.py").read_text(encoding="utf-8")
        prompt_text = (PROJECT_ROOT / "src" / "llm" / "prompt.py").read_text(encoding="utf-8")
        retired_pm_llm_mirror = "portfolio" + "_manager" + "_llm"
        forbidden = (
            "agent_call(",
            "FUTURES_PORTFOLIO_PROMPT",
            "RISK_CONTROL_PROMPT",
            "SINGLE_ANALYST_LOGIC",
            "MULTI_ANALYST_LOGIC",
            "build_pm_action_evidence_prompt",
            "llm_audit_metadata",
            retired_pm_llm_mirror,
        )
        for token in forbidden:
            self.assertNotIn(token, text)
            self.assertNotIn(token, prompt_text)


if __name__ == "__main__":
    unittest.main()
