import sys
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.constants import Signal
from graph.schema import AnalystSignal, RecommendationSourceType, RecommendationStatus
from tools.agent_tools.decision.pm_signal_fusion import build_opportunity_scorecard
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
    apply_full_market_capital_deployment,
    rank_metadata_for_row,
    rank_trace_for_row,
)
from tools.agent_tools.decision.pm_lifecycle_action_port import (
    build_lifecycle_transition_diagnostic,
    classify_lifecycle_action_port,
)
from tools.agent_tools.decision.pm_lifecycle_learning_router import route_lifecycle_learning
from tools.agent_tools.decision.pm_ticker_side_selection import (
    SIDE_PRIORITY_MEANING,
    SIDE_PRIORITY_SEMANTICS_VERSION,
    select_ticker_side,
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
        self.assertEqual(contract["producer"], "signal_collector")
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
        self.assertEqual(contract["producer"], "signal_collector")
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
        lifecycle_pos = source.index("primary_lifecycle_action_port = classify_lifecycle_action_port")
        scorecard_pos = source.index("opportunity_scorecard = build_opportunity_scorecard", lifecycle_pos)
        side_selection_pos = source.index("ticker_side_selection_result = select_ticker_side", lifecycle_pos)
        diagnostic_pos = source.index("lifecycle_transition_diagnostic = build_lifecycle_transition_diagnostic")
        router_pos = source.index("lifecycle_learning_router = route_lifecycle_learning")
        self.assertLess(lifecycle_pos, scorecard_pos)
        self.assertLess(lifecycle_pos, side_selection_pos)
        self.assertLess(lifecycle_pos, diagnostic_pos)
        self.assertLess(diagnostic_pos, router_pos)
        self.assertIn("primary_lifecycle_action_port.get", source[router_pos:router_pos + 250])
        self.assertNotIn("lifecycle_transition_diagnostic_port.get", source[router_pos:router_pos + 250])
        self.assertNotIn("_route_pm_scorecard_action_values", source)
        self.assertEqual(source.count("route_lifecycle_learning("), 1)

    def test_pm_main_chain_defers_final_contract_builder_to_step6_finalizer(self):
        source = (SRC_ROOT / "agents" / "decision_team" / "portfolio_manager.py").read_text(encoding="utf-8-sig")
        main_start = source.index("def _run_pm_six_step_decision")
        main_end = source.index("def calculate_long_short_signals", main_start)
        main_chain = source[main_start:main_end]
        signer_start = source.index("def _sign_pm_candidate_recommendation")
        signer_end = source.index("def _release_block_category", signer_start)
        signer = source[signer_start:signer_end]

        self.assertIn("def portfolio_agent_futures", source)
        self.assertIn("return _run_pm_six_step_decision(state)", source)
        self.assertIn("_build_pm_internal_candidate_contract(", main_chain)
        self.assertIn("pm_internal_candidate=pm_internal_candidate", main_chain)
        self.assertNotIn("final_action_contract = _build_final_action_contract(", main_chain)
        self.assertIn("final_action_contract = _build_final_action_contract(**builder_inputs)", signer)
        self.assertIn("snapshot.pop(\"pm_internal_candidate\", None)", signer)
        self.assertIn("snapshot.pop(\"pm_internal_candidate_contract\", None)", signer)
        self.assertIn("snapshot.pop(\"pm_capital_deployment_decision\", None)", signer)
        self.assertNotIn("_build_minimal_final_action_contract", source)
        self.assertNotIn("blocked_internal_candidate", source)
        self.assertNotIn("build_signal_collection_contract(", source)
        self.assertNotIn("from tools.common.signal_evidence_collection import build_signal_collection_contract", source)

    def test_full_market_deployment_writes_decision_not_final_contract_for_internal_candidate(self):
        scorecard = {
            "preferred_side": "long",
            "long": {
                "side": "long",
                "final_state": "watch_for_trigger",
                "opportunity_score": 0.62,
                "score": 0.62,
                "rank_candidate_input_components": {"cold_start_evidence_quality": 0.62},
                "lifecycle_learning_trace": {"trace_version": "test"},
                "learning_impact_delta": {"learning_impact_delta": 0.0},
            },
        }
        snapshot = {
            "opportunity_scorecard": scorecard,
            "pm_internal_candidate": {
                "candidate_contract": {
                    "ticker": "RB",
                    "current_lots": 0,
                    "target_lots": 1,
                    "target_position_ratio": 0.008,
                    "final_action": "open_probe",
                    "reason_codes": [],
                },
                "final_contract_builder_inputs": {
                    "current_lots": 0,
                    "target_lots": 1,
                    "control_reasons": [],
                },
            },
        }
        recommendation = SimpleNamespace(
            status=RecommendationStatus.PENDING,
            source_type=RecommendationSourceType.STRATEGY,
            signal_snapshot=snapshot,
            underlying_code="RB",
            base_price=3500.0,
            action=None,
            lots=0,
        )
        portfolio = SimpleNamespace(account_equity=1_000_000.0, cashflow=1_000_000.0, margin_used=0.0, positions={})

        result = apply_full_market_capital_deployment(
            generated=[("RB", recommendation)],
            config={
                "max_total_margin_ratio": 0.20,
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "max_single_ticker_margin_ratio": 0.13,
                },
                "net_exposure_control": {"max_net_exposure": 0.50},
            },
            portfolio=portfolio,
        )

        self.assertEqual(result["candidate_count"], 1)
        self.assertNotIn("final_action_contract", recommendation.signal_snapshot)
        self.assertIn("pm_capital_deployment_decision", recommendation.signal_snapshot)
        self.assertTrue(
            recommendation.signal_snapshot["pm_capital_deployment_decision"]["selected_for_capital_deployment"]
        )
        self.assertIn("pm_internal_candidate", recommendation.signal_snapshot)

    def test_full_market_deployment_skips_non_new_risk_candidate_without_step5_fact(self):
        snapshot = {
            "opportunity_scorecard": {
                "preferred_side": "long",
                "long": {
                    "side": "long",
                    "final_state": "tradeable_candidate",
                    "opportunity_score": 0.92,
                    "score": 0.92,
                },
            },
            "pm_internal_candidate": {
                "candidate_contract": {
                    "ticker": "RB",
                    "current_lots": 1,
                    "target_lots": 1,
                    "lots_delta": 0,
                    "final_action": "hold",
                    "reason_codes": ["test_hold_candidate"],
                },
                "final_contract_builder_inputs": {
                    "current_lots": 1,
                    "target_lots": 1,
                    "control_reasons": ["test_hold_candidate"],
                },
            },
        }
        recommendation = SimpleNamespace(
            status=RecommendationStatus.PENDING,
            source_type=RecommendationSourceType.STRATEGY,
            signal_snapshot=snapshot,
            underlying_code="RB",
            base_price=3500.0,
            action=None,
            lots=0,
        )
        portfolio = SimpleNamespace(account_equity=1_000_000.0, cashflow=1_000_000.0, margin_used=0.0, positions={})

        result = apply_full_market_capital_deployment(
            generated=[("RB", recommendation)],
            config={"max_total_margin_ratio": 0.20},
            portfolio=portfolio,
        )

        self.assertEqual(result["candidate_count"], 0)
        self.assertNotIn("pm_capital_deployment_decision", recommendation.signal_snapshot)
        self.assertIn("pm_internal_candidate", recommendation.signal_snapshot)

    def test_full_market_deployment_ignores_signed_contract_without_internal_candidate(self):
        scorecard = {
            "preferred_side": "long",
            "long": {
                "side": "long",
                "final_state": "watch_for_trigger",
                "opportunity_score": 0.62,
                "score": 0.62,
                "rank_candidate_input_components": {"cold_start_evidence_quality": 0.62},
                "lifecycle_learning_trace": {"trace_version": "test"},
                "learning_impact_delta": {"learning_impact_delta": 0.0},
            },
        }
        final_contract = {
            "ticker": "RB",
            "current_lots": 0,
            "target_lots": 1,
            "target_position_ratio": 0.008,
            "final_action": "open_probe",
            "reason_codes": ["already_signed"],
            "evidence_used": {"existing": True},
        }
        snapshot = {
            "opportunity_scorecard": scorecard,
            "final_action_contract": dict(final_contract),
        }
        recommendation = SimpleNamespace(
            status=RecommendationStatus.PENDING,
            source_type=RecommendationSourceType.STRATEGY,
            signal_snapshot=snapshot,
            underlying_code="RB",
            base_price=3500.0,
            action=None,
            lots=0,
        )
        portfolio = SimpleNamespace(account_equity=1_000_000.0, cashflow=1_000_000.0, margin_used=0.0, positions={})

        apply_full_market_capital_deployment(
            generated=[("RB", recommendation)],
            config={
                "max_total_margin_ratio": 0.20,
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "max_single_ticker_margin_ratio": 0.13,
                },
                "net_exposure_control": {"max_net_exposure": 0.50},
            },
            portfolio=portfolio,
        )

        self.assertEqual(recommendation.signal_snapshot["final_action_contract"], final_contract)
        self.assertNotIn("pm_capital_deployment_decision", recommendation.signal_snapshot)

    def test_mechanism_pm_step2_documents_lifecycle_port_primary_tool(self):
        text = (PROJECT_ROOT / "docs" / "mechanism_pm.md").read_text(encoding="utf-8-sig")
        step2_start = text.index("## 2. 判断生命周期动作口")
        step3_start = text.index("## 3. 判断单品种方向与候选质量")
        step2 = text[step2_start:step3_start]
        section_start = step2.index("### 2.1 调用工具/模块")
        section_end = step2.index("### 2.2 交易动作分口")
        section = step2[section_start:section_end]

        self.assertIn("主工具：", section)
        self.assertIn("`pm_lifecycle_action_port.py`", section)
        self.assertIn("共享语义依赖：", section)
        self.assertIn("`final_action_semantics`", section)
        self.assertIn("输入辅助：", section)
        self.assertIn("明确禁止：", section)
        self.assertIn("`pm_contract_builder.py` 不参与动作口判断", section)
        self.assertIn("只在第 6 步生成唯一 `final_action_contract`", section)
        self.assertNotIn("- `pm_contract_builder`", section)
        self.assertNotIn("主要用：", section)

    def test_lifecycle_transition_diagnostic_allows_explicit_budget_transition(self):
        primary = classify_lifecycle_action_port({
            "current_lots": 0,
            "target_lots": 1,
            "final_action": "open_probe",
        })
        final = classify_lifecycle_action_port({
            "current_lots": 0,
            "target_lots": 0,
            "final_action": "wait",
            "reason_codes": ["no_rank_or_budget_no_new_exposure"],
        })
        check = build_lifecycle_transition_diagnostic(
            primary_lifecycle_action_port=primary,
            contract_lifecycle_port=final,
            reason_codes=["no_rank_or_budget_no_new_exposure"],
        )
        self.assertTrue(check["ok"])
        self.assertEqual(check["diagnostic_type"], "lifecycle_transition_diagnostic")
        self.assertFalse(check["consistent"])
        self.assertEqual(check["transition_reason"], "no_rank_or_budget_no_new_exposure")
        self.assertTrue(check["diagnostic_only"])
        self.assertTrue(check["not_final_contract_gate"])
        self.assertTrue(check["does_not_route_learning"])

    def test_lifecycle_transition_diagnostic_marks_unexplained_transition(self):
        primary = classify_lifecycle_action_port({
            "current_lots": 0,
            "target_lots": 1,
            "final_action": "open_probe",
        })
        final = classify_lifecycle_action_port({
            "current_lots": 0,
            "target_lots": 0,
            "final_action": "wait",
        })
        check = build_lifecycle_transition_diagnostic(
            primary_lifecycle_action_port=primary,
            contract_lifecycle_port=final,
            reason_codes=[],
        )
        self.assertFalse(check["ok"])
        self.assertEqual(check["transition_reason"], "unexplained_lifecycle_port_transition")

    def test_lifecycle_learning_router_routes_execution_to_trigger_profile(self):
        result = route_lifecycle_learning(
            lifecycle_port="new_risk",
            action_values=[
                {"id": "open-1", "action_value_lane": "open"},
                {"id": "exec-1", "action_value_lane": "execution"},
                {"id": "hold-1", "action_value_lane": "hold"},
            ],
        )
        self.assertEqual([row["id"] for row in result["decision_learning_rows"]], ["open-1"])
        self.assertEqual([row["id"] for row in result["accepted_learning"]], ["open-1"])
        self.assertEqual([row["id"] for row in result["trigger_profile_learning_rows"]], ["exec-1"])
        self.assertEqual([row["id"] for row in result["trigger_profile_learning"]], ["exec-1"])
        self.assertEqual([row["id"] for row in result["rejected_learning_rows"]], ["hold-1"])
        self.assertNotIn("exec-1", {row["id"] for row in result["rejected_learning"]})
        self.assertTrue(result["trigger_profile_learning"][0]["not_rank_learning"])
        self.assertFalse(result["trigger_profile_learning_direct_to_rank"])

    def test_ticker_side_selection_selects_side_without_trade_authority(self):
        signal = _signal("technical", Signal.BULLISH, 0.74)
        raw_scorecard = build_opportunity_scorecard(
            ticker="RB",
            analyst_signals=[signal],
            signal_collection_contract={
                "dominant_side": "long",
                "side_consensus": "single_side",
                "trigger_status": "confirmed",
                "evidence_strength": "high",
                "evidence_conflict_level": "low",
            },
            market_confirmation={"confirmation_score": 0.72},
            config={"weak_confirmation_threshold": 0.45},
        )
        self.assertIn("analyst_direction_evidence", raw_scorecard["long"])
        self.assertIn("candidate_quality", raw_scorecard["long"])
        self.assertNotIn("side_priority", raw_scorecard["long"])
        self.assertNotIn("ticker_side_priority", raw_scorecard["long"])
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

        original_contract = copy.deepcopy(snapshot["final_action_contract"])

        _clear_non_full_market_rank_fields(snapshot)

        contract = snapshot["final_action_contract"]
        evidence = contract["evidence_used"]
        deployment = contract["capital_deployment"]
        scorecard_long = snapshot["opportunity_scorecard"]["long"]
        self.assertEqual(contract, original_contract)
        self.assertIn("opportunity_rank", evidence)
        self.assertIn("opportunity_rank", deployment)
        self.assertNotIn("opportunity_rank", scorecard_long)
        self.assertNotIn("rank_input_components", scorecard_long)
        self.assertEqual(evidence["lifecycle_learning_trace"]["contract_lifecycle_port"], "hold")
        self.assertEqual(evidence["learning_impact_delta"]["hold_decision"], "continue_hold")
        self.assertEqual(scorecard_long["lifecycle_learning_trace"]["contract_lifecycle_port"], "hold")

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
