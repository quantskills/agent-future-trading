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
from tools.agent_tools.decision.pm_full_market_capital_deployment import (
    CAPITAL_LAYER_ALPHA_SCALE,
    CAPITAL_LAYER_EXPLORATION,
    CAPITAL_RATIO_SOURCE_ALPHA_SCALE,
    CAPITAL_RATIO_SOURCE_EXPLORATION,
    RANK_CAPITAL_ROLE_ALPHA_SCALE,
    RANK_CAPITAL_ROLE_EXPLORATION,
    RANK_CAPITAL_ROLE_REAL_BUDGET,
    _clear_non_full_market_rank_fields,
    _ensure_final_rank_score_fields,
    rank_metadata_for_row,
    rank_trace_for_row,
)
from tools.agent_tools.decision.pm_lifecycle_learning_router import route_lifecycle_learning
from tools.agent_tools.decision.pm_ticker_side_selection import (
    SIDE_PRIORITY_MEANING,
    SIDE_PRIORITY_SEMANTICS_VERSION,
    select_ticker_side,
)
from tools.agent_tools.decision.pm_position_sizing import build_position_sizing_result
from tools.common.final_action_semantics import lifecycle_learning_decision_contract_errors
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

    def test_pm_main_chain_classifies_lifecycle_before_scorecard(self):
        source = (SRC_ROOT / "agents" / "decision_team" / "portfolio_manager.py").read_text(encoding="utf-8-sig")
        lifecycle_pos = source.index("initial_lifecycle_action_port = classify_lifecycle_action_port")
        scorecard_pos = source.index("opportunity_scorecard = build_opportunity_scorecard", lifecycle_pos)
        side_selection_pos = source.index("ticker_side_selection_result = select_ticker_side", lifecycle_pos)
        self.assertLess(lifecycle_pos, scorecard_pos)
        self.assertLess(lifecycle_pos, side_selection_pos)
        self.assertIn("_route_pm_scorecard_action_values", source[lifecycle_pos:scorecard_pos])

    def test_lifecycle_learning_router_routes_execution_to_trigger_profile(self):
        result = route_lifecycle_learning(
            lifecycle_port="new_risk",
            action_values=[
                {"id": "open-1", "action_value_lane": "open"},
                {"id": "exec-1", "action_value_lane": "execution"},
                {"id": "hold-1", "action_value_lane": "hold"},
            ],
        )
        self.assertEqual([row["id"] for row in result["accepted_learning"]], ["open-1"])
        self.assertEqual([row["id"] for row in result["trigger_profile_learning"]], ["exec-1"])
        self.assertNotIn("exec-1", {row["id"] for row in result["rejected_learning"]})
        self.assertTrue(result["trigger_profile_learning"][0]["not_rank_learning"])
        self.assertFalse(result["trigger_profile_learning_direct_to_rank"])

    def test_ticker_side_selection_selects_side_without_trade_authority(self):
        signal = _signal("technical", Signal.BULLISH, 0.74)
        result = select_ticker_side(
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
        self.assertNotIn("opportunity_rank", result)
        self.assertIn("ticker_side_priority", result)
        self.assertEqual(result["side_priority_semantics_version"], SIDE_PRIORITY_SEMANTICS_VERSION)
        self.assertEqual(result["side_priority_meaning"], SIDE_PRIORITY_MEANING)
        self.assertTrue(result["side_priority_is_not_capital_rank"])
        self.assertTrue(result["capital_allocation_reason"]["side_priority_is_not_trade_authority"])
        self.assertTrue(result["capital_allocation_reason"]["side_priority_is_not_capital_rank"])
        row = result["opportunity_scorecard"]["long"]
        self.assertNotIn("opportunity_rank", row)
        self.assertNotIn("rank_score", row)
        self.assertNotIn("capital_priority_score", row)
        self.assertIn("candidate_quality", row)
        self.assertEqual(row["side_priority_semantics_version"], SIDE_PRIORITY_SEMANTICS_VERSION)
        self.assertTrue(row["side_priority_is_not_capital_rank"])
        self.assertTrue(row["side_priority_is_not_trade_authority"])
        self.assertTrue(result["ticker_side_selection_trace"]["no_llm"])

    def test_ticker_side_selection_uses_ticker_side_priority_only(self):
        result = select_ticker_side(
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
        self.assertEqual(scorecard["long"]["side_priority"], 1)
        self.assertEqual(scorecard["short"]["side_priority"], 2)
        self.assertNotIn("opportunity_rank", scorecard["short"])
        self.assertNotIn("rank_score", scorecard["short"])
        self.assertNotIn("capital_layer", scorecard["short"])
        self.assertNotIn("deployment_rank", scorecard["short"])
        self.assertNotIn("exploration_rank", scorecard["short"])
        self.assertEqual(
            result["capital_allocation_reason"]["preferred_candidate_quality"],
            scorecard["short"]["candidate_quality"],
        )
        self.assertEqual(result["capital_allocation_reason"]["preferred_candidate_layer_hint"], "tradeable_candidate")

    def test_all_watch_for_trigger_sides_rank_by_ticker_side_priority(self):
        result = select_ticker_side(
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
        self.assertEqual(scorecard["long"]["side_priority"], 1)
        self.assertEqual(scorecard["short"]["side_priority"], 2)
        self.assertEqual(scorecard["long"]["candidate_layer_hint"], "watch_for_trigger_candidate")
        self.assertNotIn("rank_score", scorecard["long"])
        self.assertNotIn("capital_layer", scorecard["long"])

    def test_open_action_value_learning_changes_new_capital_priority_only_by_lifecycle(self):
        base_signal = [_signal("technical", Signal.BULLISH, 0.72)]
        positive = select_ticker_side(
            ticker="P",
            analyst_signals=base_signal,
            signal_collection_contract={"dominant_side": "long"},
            effective_memory_summary={"status": "available"},
            market_confirmation={"confirmation_score": 0.65},
            data_quality_summary={},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "consumer_scope": "pm_learning",
                    "side": "long",
                    "action_value_lane": "open",
                    "action_preference": "positive_candidate_open",
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                    "reward_sum": 6000,
                    "reward_mean": 6000,
                    "sample_count": 3,
                    "last_sample_date": "2025-03-04",
                }
            ],
            decision_date="2025-03-05",
            config={},
        )
        negative = select_ticker_side(
            ticker="P",
            analyst_signals=base_signal,
            signal_collection_contract={"dominant_side": "long"},
            effective_memory_summary={"status": "available"},
            market_confirmation={"confirmation_score": 0.65},
            data_quality_summary={},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "consumer_scope": "pm_learning",
                    "side": "long",
                    "action_value_lane": "open",
                    "action_preference": "negative_revalidate",
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                    "reward_sum": -6000,
                    "reward_mean": -6000,
                    "sample_count": 3,
                    "last_sample_date": "2025-03-04",
                },
                {
                    "consumer_scope": "pm_learning",
                    "side": "long",
                    "action_value_lane": "hold",
                    "action_preference": "positive_candidate_hold",
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                    "reward_sum": 9000,
                    "reward_mean": 9000,
                    "sample_count": 3,
                    "last_sample_date": "2025-03-04",
                },
                {
                    "consumer_scope": "pm_learning",
                    "side": "long",
                    "action_value_lane": "execution",
                    "action_preference": "positive_candidate_execution",
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                    "reward_sum": 9000,
                    "reward_mean": 9000,
                    "sample_count": 3,
                    "last_sample_date": "2025-03-04",
                },
            ],
            decision_date="2025-03-05",
            config={},
        )

        positive_row = positive["opportunity_scorecard"]["long"]
        negative_row = negative["opportunity_scorecard"]["long"]
        self.assertNotIn("rank_score", positive_row)
        self.assertNotIn("rank_score_components", positive_row)
        positive_rank_row = _ensure_final_rank_score_fields(dict(positive_row), config={})
        negative_rank_row = _ensure_final_rank_score_fields(dict(negative_row), config={})
        self.assertGreater(positive_rank_row["rank_score"], negative_rank_row["rank_score"])
        self.assertGreater(
            positive_rank_row["rank_score_components"]["open_add_action_value_delta"],
            negative_rank_row["rank_score_components"]["open_add_action_value_delta"],
        )
        self.assertGreater(
            positive_row["opportunity_score_components"]["positive_learning"],
            0.0,
        )
        self.assertLess(
            negative_row["opportunity_score_components"]["negative_learning"],
            0.0,
        )
        self.assertGreater(
            negative_row["opportunity_score_components"]["execution_profile_learning"],
            0.0,
        )
        impact = rank_trace_for_row(negative_rank_row)["learning_impact_delta"]
        self.assertFalse(impact["execution_profile_learning_direct_to_rank"])
        trace = rank_trace_for_row(negative_rank_row)["lifecycle_learning_trace"]
        self.assertIn("open", trace["used_lanes"])
        self.assertIn("hold", trace["ignored_lanes"])
        self.assertIn("execution", trace["ignored_lanes"])

    def test_rank_score_policy_catalog_weight_changes_rank_score(self):
        base_signal = [_signal("technical", Signal.BULLISH, 0.72)]
        action_values = [
            {
                "consumer_scope": "pm_learning",
                "side": "long",
                "action_value_lane": "open",
                "action_preference": "positive_candidate_open",
                "reward_source": "trade_episode",
                "evidence_scope": "exact_real_state",
                "reward_sum": 6000,
                "reward_mean": 6000,
                "sample_count": 3,
                "last_sample_date": "2025-03-04",
            }
        ]
        default = select_ticker_side(
            ticker="P",
            analyst_signals=base_signal,
            signal_collection_contract={"dominant_side": "long"},
            effective_memory_summary={"status": "available"},
            market_confirmation={"confirmation_score": 0.65},
            data_quality_summary={},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=action_values,
            decision_date="2025-03-05",
            config={},
        )
        boosted = select_ticker_side(
            ticker="P",
            analyst_signals=base_signal,
            signal_collection_contract={"dominant_side": "long"},
            effective_memory_summary={"status": "available"},
            market_confirmation={"confirmation_score": 0.65},
            data_quality_summary={},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=action_values,
            decision_date="2025-03-05",
            config={
                "rank_score_policy": {
                    "rank_score": {
                        "open_add_action_value_delta": {
                            "positive_signal_weight": 0.30,
                        },
                    },
                },
            },
        )

        default_row = _ensure_final_rank_score_fields(dict(default["opportunity_scorecard"]["long"]), config={})
        boosted_row = _ensure_final_rank_score_fields(
            dict(boosted["opportunity_scorecard"]["long"]),
            config={
                "rank_score_policy": {
                    "rank_score": {
                        "open_add_action_value_delta": {
                            "positive_signal_weight": 0.30,
                        },
                    },
                },
            },
        )
        self.assertGreater(
            boosted_row["rank_score_components"]["open_add_action_value_delta"],
            default_row["rank_score_components"]["open_add_action_value_delta"],
        )
        self.assertGreater(boosted_row["rank_score"], default_row["rank_score"])

    def test_repeated_alpha_candidate_uses_same_rank_with_alpha_scale_layer(self):
        result = select_ticker_side(
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
        self.assertEqual(scorecard["short"]["side_priority"], 1)
        self.assertNotIn("capital_layer", scorecard["short"])
        metadata = rank_metadata_for_row(_ensure_final_rank_score_fields(dict(scorecard["short"]), config={}))
        self.assertEqual(metadata["rank_capital_role"], RANK_CAPITAL_ROLE_ALPHA_SCALE)
        self.assertEqual(metadata["capital_layer"], CAPITAL_LAYER_ALPHA_SCALE)
        self.assertEqual(metadata["capital_ratio_source"], CAPITAL_RATIO_SOURCE_ALPHA_SCALE)
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

    def test_pm_rank_sanitation_preserves_non_rank_lifecycle_trace(self):
        snapshot = {
            "final_action_contract": {
                "final_action": "hold",
                "current_lots": 2,
                "target_lots": 2,
                "lots_delta": 0,
                "learning_used": {
                    "alpha_setup_action_values": [
                        {"learning_lane": "hold", "action_name": "hold"},
                    ],
                },
                "evidence_used": {
                    "opportunity_rank": 1,
                    "rank_source": "ticker_side_priority",
                    "rank_input_components": {"old_local_rank_score": 0.77},
                    "rank_capital_role": "best_exploration_probe_candidate",
                    "capital_layer": "exploration_probe",
                    "lifecycle_learning_trace": {
                        "contract_lifecycle_port": "hold",
                        "used_lanes": ["hold"],
                        "execution_profile_signal_direct_to_rank": False,
                    },
                    "learning_impact_delta": {
                        "hold_decision": "continue_hold",
                        "net_lifecycle_learning_delta": 0.04,
                    },
                    "pm_lifecycle_learning_trace": {
                        "contract_lifecycle_port": "hold",
                        "used_lanes": ["hold"],
                    },
                    "pm_lifecycle_learning_impact_delta": {
                        "hold_decision": "continue_hold",
                    },
                },
                "capital_deployment": {
                    "opportunity_rank": 1,
                    "rank_source": "ticker_side_priority",
                    "rank_input_components": {"old_local_rank_score": 0.77},
                    "capital_layer": "exploration_probe",
                    "selected_for_capital_deployment": False,
                },
            },
            "opportunity_scorecard": {
                "long": {
                    "opportunity_rank": 1,
                    "rank_source": "ticker_side_priority",
                    "rank_input_components": {"old_local_rank_score": 0.77},
                    "lifecycle_learning_trace": {"contract_lifecycle_port": "hold"},
                    "learning_impact_delta": {"hold_decision": "continue_hold"},
                }
            },
        }

        _clear_non_full_market_rank_fields(snapshot)

        contract = snapshot["final_action_contract"]
        evidence = contract["evidence_used"]
        deployment = contract["capital_deployment"]
        scorecard_long = snapshot["opportunity_scorecard"]["long"]
        self.assertNotIn("opportunity_rank", evidence)
        self.assertNotIn("rank_source", evidence)
        self.assertNotIn("rank_input_components", evidence)
        self.assertNotIn("capital_layer", evidence)
        self.assertNotIn("opportunity_rank", deployment)
        self.assertNotIn("rank_source", deployment)
        self.assertNotIn("rank_input_components", deployment)
        self.assertNotIn("opportunity_rank", scorecard_long)
        self.assertNotIn("rank_input_components", scorecard_long)
        self.assertEqual(evidence["lifecycle_learning_trace"]["contract_lifecycle_port"], "hold")
        self.assertEqual(evidence["learning_impact_delta"]["hold_decision"], "continue_hold")
        self.assertEqual(scorecard_long["lifecycle_learning_trace"]["contract_lifecycle_port"], "hold")
        self.assertEqual(lifecycle_learning_decision_contract_errors(contract), [])

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
