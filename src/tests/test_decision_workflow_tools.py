import sys
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.constants import Signal
from graph.schema import AnalystSignal, RecommendationSourceType, RecommendationStatus
from agents.decision_team.portfolio_manager import _build_execution_contract_fields
from tools.agent_tools.decision.pm_signal_fusion import build_opportunity_scorecard
from tools.agent_tools.decision.pm_invalidation_policy import (
    _apply_pretrade_invalidation_control,
    _has_position_exit_boundary,
    _has_structured_invalidation_condition,
)
from tools.agent_tools.decision.pm_decision_memory_retrieval import retrieve_pm_memory
from tools.agent_tools.decision.pm_full_market_capital_deployment import (
    CAPITAL_LAYER_ALPHA_SCALE,
    CAPITAL_LAYER_EXPLORATION,
    CAPITAL_RATIO_SOURCE_ALPHA_SCALE,
    CAPITAL_RATIO_SOURCE_EXPLORATION,
    RANK_CAPITAL_ROLE_ALPHA_SCALE,
    RANK_CAPITAL_ROLE_EXPLORATION,
    RANK_CAPITAL_ROLE_REAL_BUDGET,
    _capital_rank_sort_tuple,
    _clear_non_full_market_rank_fields,
    _ensure_final_rank_score_fields,
    apply_full_market_capital_deployment,
    rank_metadata_for_row,
    rank_trace_for_row,
)
from tools.agent_tools.decision.pm_lifecycle_action_port import classify_lifecycle_action_port
from tools.agent_tools.decision.pm_lifecycle_learning_router import route_lifecycle_learning
from tools.agent_tools.decision.pm_ticker_side_selection import (
    SIDE_PRIORITY_MEANING,
    SIDE_PRIORITY_SEMANTICS_VERSION,
    select_ticker_side,
)
from tools.agent_tools.decision.pm_position_sizing import build_position_sizing_result
from tools.common.signal_evidence_collection import (
    build_pm_evidence_signals_from_scc,
    build_signal_collection_contract,
)
from tools.common.execution_trigger_semantics import (
    canonical_entry_invalidation_condition,
)
from tests.contract_test_fixtures import build_test_aec
from tests.test_pm_atomic_contract_flow import _pm_state


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
                "action_name": "add_or_open",
                "canonical_action_family": "open_add_new_risk",
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
    side = "long" if signal == Signal.BULLISH else "short" if signal == Signal.BEARISH else "flat"
    executable = side != "flat" and agent_name in {"technical", "commodity_news"}
    contract = build_test_aec(
        agent_name,
        ticker="BU",
        trading_date="2025-03-05",
        signal=signal.value,
        side=side,
        confidence=confidence,
        opportunity_state="tradeable_candidate" if executable else "no_opportunity",
        trigger_valid=executable,
        current_trigger_confirmed=executable,
        invalidation_present=executable,
    )
    contract.update(contract_overrides)
    return AnalystSignal(
        agent_name=agent_name,
        signal=signal,
        confidence=confidence,
        metadata={
            "action_evidence_contract": contract,
            "signal_record_id": f"{agent_name}-fixture",
        },
    )


class DecisionWorkflowToolTest(unittest.TestCase):
    def test_entry_invalidation_and_position_exit_boundary_are_independent(self):
        signal = _signal("technical", Signal.BULLISH, 0.72)
        contract = signal.metadata["action_evidence_contract"]
        contract.update(
            {
                "invalidation_present": True,
                "invalidation_condition": canonical_entry_invalidation_condition(
                    contract["entry_timing_signal"],
                    contract["side"],
                ),
                "invalidation_level": 95.0,
                "position_invalidation_level": None,
                "exit_hint": "",
                "atr_stop_distance": None,
                "expected_horizon_days": 0,
            }
        )

        self.assertTrue(_has_structured_invalidation_condition([signal]))
        self.assertFalse(_has_position_exit_boundary([signal]))
        _ratio, reasons, _notes, diagnostics = _apply_pretrade_invalidation_control(
            ticker="BU",
            position_ratio=0.05,
            current_ratio=0.0,
            max_position_ratio=0.10,
            analyst_signals=[signal],
            full_config={},
        )
        self.assertNotIn("missing_pretrade_invalidation", reasons)
        self.assertIn("missing_position_exit_boundary", reasons)
        self.assertTrue(
            diagnostics["pretrade_invalidation"]["entry_invalidation_present"]
        )
        self.assertFalse(
            diagnostics["pretrade_invalidation"]["position_exit_boundary_present"]
        )

        contract["invalidation_condition"] = "close below entry setup boundary"
        self.assertFalse(_has_structured_invalidation_condition([signal]))

    def test_pm_assembles_position_lifecycle_fields_by_analyst_role(self):
        technical = _signal(
            "technical",
            Signal.BULLISH,
            0.72,
            position_invalidation_level=91.0,
            exit_hint="exit below technical structure",
            atr_stop_distance=7.0,
            expected_horizon_days=0,
        )
        fundamental = _signal(
            "fundamental",
            Signal.BULLISH,
            0.84,
            position_invalidation_level=80.0,
            exit_hint="reduce if medium-term basis reverses",
            atr_stop_distance=99.0,
            horizon_class="medium",
            expected_horizon_days=12,
        )

        fields = _build_execution_contract_fields(
            ticker="BU",
            current_lots=0,
            target_lots=1,
            analyst_signals=[technical, fundamental],
            final_entry_authority={
                "authority_type": "real_budget_entry",
                "conditional_trigger_authority": False,
                "open_action_evidence": True,
            },
            trading_date="2025-03-05",
            recommendation_intent={},
            control_reasons=[],
            reference_price=100.0,
        )

        technical_contract = technical.metadata["action_evidence_contract"]
        self.assertEqual(fields["entry_trigger"], technical_contract["entry_trigger"])
        self.assertEqual(
            fields["invalidation_level"],
            technical_contract["invalidation_level"],
        )
        self.assertEqual(fields["position_invalidation_level"], 91.0)
        self.assertEqual(fields["atr_stop_distance"], 7.0)
        self.assertEqual(fields["horizon_class"], "medium")
        self.assertEqual(fields["expected_horizon_days"], 12)
        self.assertEqual(
            fields["exit_hint"],
            "exit below technical structure",
        )

    def test_pm_horizon_pair_falls_back_atomically_to_entry_evidence(self):
        technical = _signal(
            "technical",
            Signal.BULLISH,
            0.72,
            atr_stop_distance=4.0,
            horizon_class="short",
            expected_horizon_days=3,
        )
        incomplete_fundamental = _signal(
            "fundamental",
            Signal.BULLISH,
            0.84,
            horizon_class="medium",
            expected_horizon_days=0,
        )

        fields = _build_execution_contract_fields(
            ticker="BU",
            current_lots=0,
            target_lots=1,
            analyst_signals=[technical, incomplete_fundamental],
            final_entry_authority={
                "authority_type": "real_budget_entry",
                "conditional_trigger_authority": False,
                "open_action_evidence": True,
            },
            trading_date="2025-03-05",
            recommendation_intent={},
            control_reasons=[],
            reference_price=100.0,
        )

        self.assertEqual(fields["horizon_class"], "short")
        self.assertEqual(fields["expected_horizon_days"], 3)

    def test_event_entry_uses_same_source_structure_and_direction_neutral_technical_atr(self):
        opposite_technical = _signal(
            "technical",
            Signal.BEARISH,
            0.70,
            position_invalidation_level=108.0,
            atr_stop_distance=3.0,
        )
        event = _signal(
            "commodity_news",
            Signal.BULLISH,
            0.82,
            position_invalidation_level=92.0,
            atr_stop_distance=99.0,
        )

        fields = _build_execution_contract_fields(
            ticker="BU",
            current_lots=0,
            target_lots=1,
            analyst_signals=[opposite_technical, event],
            final_entry_authority={
                "authority_type": "real_budget_entry",
                "conditional_trigger_authority": False,
                "open_action_evidence": True,
            },
            trading_date="2025-03-05",
            recommendation_intent={},
            control_reasons=[],
            reference_price=100.0,
        )

        self.assertEqual(fields["execution_profile"], "event_immediate")
        self.assertEqual(fields["position_invalidation_level"], 92.0)
        self.assertEqual(fields["atr_stop_distance"], 3.0)

    def test_pm_uses_scc_rebuilt_exit_facts_not_mutated_raw_analyst_payload(self):
        technical = _signal(
            "technical",
            Signal.BULLISH,
            0.72,
            position_invalidation_level=94.0,
            atr_stop_distance=2.0,
        )
        fundamental = _signal(
            "fundamental",
            Signal.BULLISH,
            0.66,
            horizon_class="medium",
            expected_horizon_days=8,
        )
        news = _signal("commodity_news", Signal.NEUTRAL, 0.40)
        scc = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-05",
            analyst_signals=[technical, fundamental, news],
            enabled_analysts=["technical", "fundamental", "commodity_news"],
        )
        technical.metadata["action_evidence_contract"]["position_invalidation_level"] = 150.0
        technical.metadata["action_evidence_contract"]["atr_stop_distance"] = 999.0

        fields = _build_execution_contract_fields(
            ticker="BU",
            current_lots=0,
            target_lots=1,
            analyst_signals=build_pm_evidence_signals_from_scc(scc),
            final_entry_authority={
                "authority_type": "real_budget_entry",
                "conditional_trigger_authority": False,
                "open_action_evidence": True,
            },
            trading_date="2025-03-05",
            recommendation_intent={},
            control_reasons=[],
            reference_price=100.0,
        )

        self.assertEqual(fields["position_invalidation_level"], 94.0)
        self.assertEqual(fields["atr_stop_distance"], 2.0)

    def test_pretrade_boundaries_ignore_opposite_side_lifecycle(self):
        technical = _signal(
            "technical",
            Signal.BULLISH,
            0.72,
            position_invalidation_level=None,
            exit_hint="",
            atr_stop_distance=None,
            expected_horizon_days=0,
        )
        opposite_fundamental = _signal(
            "fundamental",
            Signal.BEARISH,
            0.90,
            position_invalidation_level=106.0,
            expected_horizon_days=12,
        )

        _ratio, reasons, _notes, diagnostics = _apply_pretrade_invalidation_control(
            ticker="BU",
            position_ratio=0.05,
            current_ratio=0.0,
            max_position_ratio=0.10,
            analyst_signals=[technical, opposite_fundamental],
            full_config={},
        )

        self.assertNotIn("missing_pretrade_invalidation", reasons)
        self.assertIn("missing_position_exit_boundary", reasons)
        self.assertFalse(
            diagnostics["pretrade_invalidation"]["position_exit_boundary_present"]
        )

    def test_signal_collector_preserves_source_evidence_without_trade_authority(self):
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-05",
            analyst_signals=[
                _signal("technical", Signal.BEARISH, 0.72),
                _signal("fundamental", Signal.BEARISH, 0.66),
                _signal("commodity_news", Signal.NEUTRAL, 0.40),
            ],
            enabled_analysts=["technical", "fundamental", "commodity_news"],
        )

        self.assertEqual(contract["contract_version"], "agentquant.signal_collection.v1")
        self.assertEqual(contract["source_agent"], "signal_collector")
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
        self.assertEqual(contract["source_agent"], "signal_collector")
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

    def test_memory_retrieval_requires_explicit_pm_learning_scope(self):
        class ScopeMemoryDB:
            def get_alpha_setup_action_values(self, **kwargs):
                common = {
                    "ticker": kwargs["ticker"],
                    "side": kwargs["side"],
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout",
                    "action_name": "open",
                    "canonical_action_family": "open_add_new_risk",
                    "action_value_lane": "open",
                    "learning_lane": "open",
                    "action_preference": "positive_candidate_open",
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                    "reward_sum": 500.0,
                    "reward_mean": 500.0,
                    "sample_count": 1,
                    "last_sample_date": "2025-03-04",
                    "canonical_action_value": True,
                }
                return [
                    {**common, "id": "scope-missing"},
                    {**common, "id": "scope-empty", "consumer_scope": ""},
                    {
                        **common,
                        "id": "scope-payload-pm",
                        "payload": {"consumer_scope": "pm_learning"},
                    },
                    {**common, "id": "scope-top-pm", "consumer_scope": "pm_learning"},
                ]

        result = retrieve_pm_memory(
            db=ScopeMemoryDB(),
            config_id="cfg",
            ticker="BU",
            side="short",
            trading_date="2025-03-05",
            horizon_class="short",
            market_regime="trend",
            setup_type="trend_breakout",
            limit=10,
        )

        self.assertEqual(
            {row["id"] for row in result["action_values"]},
            {"scope-payload-pm", "scope-top-pm"},
        )
        self.assertTrue(
            all(row["consumer_scope"] == "pm_learning" for row in result["action_values"])
        )
        rejected = {
            row["id"]: row["reason"]
            for row in result["rejected_or_downgraded"]
            if row.get("id") in {"scope-missing", "scope-empty"}
        }
        self.assertEqual(
            rejected,
            {
                "scope-missing": "non_pm_learning_scope",
                "scope-empty": "non_pm_learning_scope",
            },
        )

    def test_pm_main_chain_selects_direction_before_lifecycle_and_learning(self):
        source = (SRC_ROOT / "agents" / "decision_team" / "portfolio_manager.py").read_text(encoding="utf-8-sig")
        lifecycle_pos = source.index("primary_lifecycle_action_port = classify_lifecycle_action_port")
        scorecard_pos = source.index("opportunity_scorecard = build_opportunity_scorecard")
        side_selection_pos = source.index("ticker_side_selection_result = select_ticker_side", scorecard_pos)
        router_pos = source.index("lifecycle_learning_router = route_lifecycle_learning")
        self.assertLess(scorecard_pos, side_selection_pos)
        self.assertLess(side_selection_pos, lifecycle_pos)
        self.assertLess(lifecycle_pos, router_pos)
        self.assertNotIn("build_lifecycle_transition_diagnostic", source)
        self.assertIn("primary_lifecycle_action_port.get", source[router_pos:router_pos + 250])
        self.assertNotIn("lifecycle_transition_diagnostic_port.get", source[router_pos:router_pos + 250])
        self.assertNotIn("_route_pm_scorecard_action_values", source)
        self.assertEqual(source.count("route_lifecycle_learning("), 1)

    def test_pm_main_chain_defers_final_contract_builder_to_step6_finalizer(self):
        source = (SRC_ROOT / "agents" / "decision_team" / "portfolio_manager.py").read_text(encoding="utf-8-sig")
        main_start = source.index("def _run_pm_six_step_decision")
        main_end = source.index("def calculate_long_short_signals", main_start)
        main_chain = source[main_start:main_end]
        signer_start = source.index("def _sign_pm_memory_state")
        signer_end = source.index("def _release_block_category", signer_start)
        signer = source[signer_start:signer_end]

        self.assertIn("def portfolio_agent_futures", source)
        self.assertIn('return {"pm_state": _run_pm_six_step_decision(state)}', source)
        self.assertIn("_build_pm_memory_state_update(", main_chain)
        self.assertIn("pm_state_update=pm_state_update", main_chain)
        self.assertNotIn("final_action_contract = _build_final_action_contract(", main_chain)
        self.assertIn("final_action_contract = _build_final_action_contract(**contract_inputs)", signer)
        self.assertNotIn("candidate_contract", source)
        self.assertNotIn("final_contract_builder_inputs", source)
        self.assertNotIn("pm_capital_deployment_decision", source)
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
                "rank_score_input_components": {"cold_start_evidence_quality": 0.62},
                "lifecycle_learning_trace": {"trace_version": "test"},
                "learning_impact_delta": {"learning_impact_delta": 0.0},
            },
        }
        pm_state = _pm_state("RB", 0, 1, with_scorecard=True)
        pm_state["opportunity_scorecard"] = scorecard
        portfolio = SimpleNamespace(account_equity=1_000_000.0, cashflow=1_000_000.0, margin_used=0.0, positions={})

        result = apply_full_market_capital_deployment(
            generated=[("RB", pm_state)],
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
        self.assertNotIn("final_action_contract", pm_state)
        self.assertNotIn("signal_snapshot", pm_state)
        self.assertTrue(pm_state["capital_deployment"]["selected_for_capital_deployment"])

    def test_full_market_deployment_skips_non_new_risk_candidate_without_step5_fact(self):
        scorecard = {
            "preferred_side": "long",
            "long": {
                "side": "long",
                "final_state": "tradeable_candidate",
                "opportunity_score": 0.92,
                "score": 0.92,
            },
        }
        pm_state = _pm_state("RB", 1, 1, with_scorecard=False)
        pm_state["opportunity_scorecard"] = scorecard
        portfolio = SimpleNamespace(account_equity=1_000_000.0, cashflow=1_000_000.0, margin_used=0.0, positions={})

        result = apply_full_market_capital_deployment(
            generated=[("RB", pm_state)],
            config={"max_total_margin_ratio": 0.20},
            portfolio=portfolio,
        )

        self.assertEqual(result["candidate_count"], 0)
        self.assertNotIn("capital_deployment", pm_state)

    def test_alpha_scale_queue_uses_existing_strong_opportunity_margin_target(self):
        scale = _pm_state("CU", 0, 1, with_scorecard=False)
        scale["final_entry_authority"].update(
            {
                "authority_type": "real_budget_entry",
                "capital_layer": CAPITAL_LAYER_ALPHA_SCALE,
                "max_allowed_margin_ratio": 0.12,
            }
        )
        scale["target_position_ratio"] = 0.12
        scale["position_ratio"] = 0.12
        scale["target_margin_ratio_estimate"] = 0.12
        scale["margin_required"] = 120_000.0
        scale["opportunity_scorecard"] = {
            "preferred_side": "long",
            "long": {
                "side": "long",
                "final_state": "tradeable_candidate",
                "opportunity_score": 0.80,
                "score": 0.80,
                "rank_score_input_components": {"cold_start_evidence_quality": 0.80},
                "opportunity_score_components": {},
            },
        }
        real = copy.deepcopy(scale)
        real["final_entry_authority"]["capital_layer"] = "real_budget_entry"
        portfolio = SimpleNamespace(
            account_equity=1_000_000.0,
            cashflow=1_000_000.0,
            margin_used=0.0,
            positions={},
        )

        apply_full_market_capital_deployment(
            generated=[("CU", scale)],
            config={
                "max_total_margin_ratio": 0.20,
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "max_single_ticker_margin_ratio": 0.13,
                },
                "capital_utilization_control": {
                    "target_margin_ratio_confirmed": 0.10,
                    "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                },
                "net_exposure_control": {"max_net_exposure": 0.50},
            },
            portfolio=portfolio,
        )

        deployment = scale["capital_deployment"]
        self.assertTrue(deployment["selected_for_capital_deployment"])
        self.assertEqual(deployment["target_margin_ratio_budget"], 0.18)

        apply_full_market_capital_deployment(
            generated=[("CU", real)],
            config={
                "max_total_margin_ratio": 0.20,
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "max_single_ticker_margin_ratio": 0.13,
                },
                "capital_utilization_control": {
                    "target_margin_ratio_confirmed": 0.10,
                    "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                },
                "net_exposure_control": {"max_net_exposure": 0.50},
            },
            portfolio=portfolio,
        )

        normal_deployment = real["capital_deployment"]
        self.assertFalse(normal_deployment["selected_for_capital_deployment"])
        self.assertEqual(normal_deployment["target_margin_ratio_budget"], 0.10)

    def test_full_market_deployment_ignores_signed_contract_without_internal_candidate(self):
        scorecard = {
            "preferred_side": "long",
            "long": {
                "side": "long",
                "final_state": "watch_for_trigger",
                "opportunity_score": 0.62,
                "score": 0.62,
                "rank_score_input_components": {"cold_start_evidence_quality": 0.62},
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
        pm_state = _pm_state("RB", 1, 1, with_scorecard=False)
        pm_state["final_action_contract"] = dict(final_contract)
        portfolio = SimpleNamespace(account_equity=1_000_000.0, cashflow=1_000_000.0, margin_used=0.0, positions={})

        apply_full_market_capital_deployment(
            generated=[("RB", pm_state)],
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

        self.assertEqual(pm_state["final_action_contract"], final_contract)
        self.assertNotIn("capital_deployment", pm_state)

    def test_agent_pm_documents_direction_before_lifecycle_port(self):
        text = (PROJECT_ROOT / "docs" / "agent_pm.md").read_text(encoding="utf-8-sig")
        step2_start = text.index("### 2. 判断产品方向")
        step3_start = text.index("### 3. 结合持仓确定交易状态")
        step2 = text[step2_start:step3_start]
        step3 = text[step3_start:text.index("### 4. 读取学习成果修正候选质量", step3_start)]

        self.assertIn("工具：`select_ticker_side`", step2)
        self.assertIn("`side_priority`", step2)
        self.assertIn("`ticker_side_priority`", step2)
        self.assertIn("`classify_lifecycle_action_port`", step3)
        self.assertIn("`primary_lifecycle_action_port`", step3)
        self.assertIn("写回同一个产品候选状态", step2)
        self.assertIn("同一个产品候选状态", step3)

    def test_lifecycle_learning_router_routes_execution_to_trigger_profile(self):
        result = route_lifecycle_learning(
            lifecycle_port="new_risk",
            action_values=[
                {
                    "id": "open-1",
                    "action_name": "add_or_open",
                    "canonical_action_family": "open_add_new_risk",
                    "action_value_lane": "open",
                    "action_preference": "positive_candidate_open",
                },
                {
                    "id": "exec-1",
                    "action_name": "execution",
                    "canonical_action_family": "execution",
                    "action_value_lane": "execution",
                    "action_preference": "positive_candidate_execution",
                },
                {
                    "id": "hold-1",
                    "action_name": "hold",
                    "canonical_action_family": "hold",
                    "action_value_lane": "hold",
                    "action_preference": "negative_hold_revalidate",
                },
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

    def test_lifecycle_learning_router_allows_only_explicit_causal_negative_hold_reduce(self):
        row = {
            "id": "hold-negative-1",
            "action_name": "hold",
            "canonical_action_value": True,
            "canonical_action_family": "hold",
            "consumer_scope": "pm_learning",
            "action_value_lane": "hold",
            "learning_lane": "hold",
            "memory_side_role": "current_position_side",
            "action_preference": "negative_hold_revalidate",
        }
        rejected = route_lifecycle_learning(
            lifecycle_port="reduce_exit",
            action_values=[row],
        )
        self.assertEqual(rejected["decision_learning_rows"], [])

        accepted = route_lifecycle_learning(
            lifecycle_port="reduce_exit",
            action_values=[row],
            causal_negative_hold_reduce_ids=["hold-negative-1"],
        )
        self.assertEqual(
            [item["id"] for item in accepted["decision_learning_rows"]],
            ["hold-negative-1"],
        )
        self.assertIn("hold", accepted["accepted_lanes"])

        for field, value in (
            ("canonical_action_value", False),
            ("consumer_scope", "analyst_calibration"),
            ("action_preference", "positive_candidate_hold"),
        ):
            with self.subTest(field=field):
                invalid = dict(row)
                invalid[field] = value
                result = route_lifecycle_learning(
                    lifecycle_port="reduce_exit",
                    action_values=[invalid],
                    causal_negative_hold_reduce_ids=["hold-negative-1"],
                )
                self.assertEqual(result["decision_learning_rows"], [])

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
            market_confirmation={"confirmation_score": 0.72},
            data_quality_summary={},
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
            market_confirmation={},
            data_quality_summary={},
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
        self.assertIsNone(scorecard["long"]["side_priority"])
        self.assertEqual(scorecard["short"]["side_priority"], 1)
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
            market_confirmation={},
            data_quality_summary={},
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
                    "opportunity_score_components": {"positive_learning": 0.04, "fusion_score_adjustment": 0.0},
                },
                "short": {
                    "side": "short",
                    "score": 0.51,
                    "opportunity_score": 0.51,
                    "capital_priority_score": 0.34,
                    "capital_priority_tier": 1,
                    "final_state": "watch_for_trigger",
                    "opportunity_score_components": {"positive_learning": 0.0, "fusion_score_adjustment": -0.05},
                    "gating_failures": ["missing_invalidation"],
                },
            },
        )

        scorecard = result["opportunity_scorecard"]
        self.assertEqual(scorecard["long"]["side_priority"], 1)
        self.assertIsNone(scorecard["short"]["side_priority"])
        self.assertEqual(scorecard["long"]["candidate_layer_hint"], "watch_for_trigger_candidate")
        self.assertNotIn("rank_score", scorecard["long"])
        self.assertNotIn("capital_layer", scorecard["long"])

    def test_open_action_value_learning_changes_new_capital_priority_only_by_lifecycle(self):
        base_signal = [_signal("technical", Signal.BULLISH, 0.72)]
        positive_scorecard = build_opportunity_scorecard(
            ticker="P",
            analyst_signals=base_signal,
            signal_collection_contract={"dominant_side": "long"},
            market_confirmation={"confirmation_score": 0.65},
            data_quality_summary={},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "canonical_action_value": True,
                    "consumer_scope": "pm_learning",
                    "side": "long",
                    "action_name": "add_or_open",
                    "canonical_action_family": "open_add_new_risk",
                    "action_value_lane": "open",
                    "learning_lane": "open",
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
        negative_scorecard = build_opportunity_scorecard(
            ticker="P",
            analyst_signals=base_signal,
            signal_collection_contract={"dominant_side": "long"},
            market_confirmation={"confirmation_score": 0.65},
            data_quality_summary={},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=[
                {
                    "canonical_action_value": True,
                    "consumer_scope": "pm_learning",
                    "side": "long",
                    "action_name": "add_or_open",
                    "canonical_action_family": "open_add_new_risk",
                    "action_value_lane": "open",
                    "learning_lane": "open",
                    "action_preference": "negative_revalidate",
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                    "reward_sum": -6000,
                    "reward_mean": -6000,
                    "sample_count": 3,
                    "last_sample_date": "2025-03-04",
                },
                {
                    "canonical_action_value": True,
                    "consumer_scope": "pm_learning",
                    "side": "long",
                    "action_name": "hold",
                    "canonical_action_family": "hold",
                    "action_value_lane": "hold",
                    "learning_lane": "hold",
                    "action_preference": "positive_candidate_hold",
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                    "reward_sum": 9000,
                    "reward_mean": 9000,
                    "sample_count": 3,
                    "last_sample_date": "2025-03-04",
                },
                {
                    "canonical_action_value": True,
                    "consumer_scope": "pm_learning",
                    "side": "long",
                    "action_name": "execution",
                    "canonical_action_family": "execution",
                    "action_value_lane": "execution",
                    "learning_lane": "execution",
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

        positive_row = positive_scorecard["long"]
        negative_row = negative_scorecard["long"]
        self.assertNotIn("rank_score", positive_row)
        self.assertNotIn("rank_score_components", positive_row)
        positive_rank_row = _ensure_final_rank_score_fields(dict(positive_row), config={})
        negative_rank_row = _ensure_final_rank_score_fields(dict(negative_row), config={})
        self.assertGreater(positive_rank_row["rank_score"], negative_rank_row["rank_score"])
        self.assertGreater(
            positive_rank_row["rank_score_components"]["open_add_action_value_delta"],
            negative_rank_row["rank_score_components"]["open_add_action_value_delta"],
        )
        self.assertEqual(
            positive_row["rank_score_input_components"]["cold_start_evidence_quality"],
            negative_row["rank_score_input_components"]["cold_start_evidence_quality"],
        )
        self.assertNotIn("rank_candidate_input_components", positive_row)
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

    def test_execution_profile_learning_is_observed_but_does_not_change_candidate_or_rank_score(self):
        base_signal = [_signal("technical", Signal.BULLISH, 0.72)]
        common = {
            "ticker": "P",
            "analyst_signals": base_signal,
            "signal_collection_contract": {"dominant_side": "long"},
            "market_confirmation": {"confirmation_score": 0.65},
            "data_quality_summary": {},
            "adaptive_policy_state": [],
            "alpha_setup_profiles": [],
            "decision_date": "2025-03-05",
            "config": {},
        }
        base_scorecard = build_opportunity_scorecard(
            **common,
            alpha_setup_action_values=[],
        )
        execution_scorecard = build_opportunity_scorecard(
            **common,
            alpha_setup_action_values=[
                {
                    "canonical_action_value": True,
                    "consumer_scope": "pm_learning",
                    "side": "long",
                    "action_name": "execution",
                    "canonical_action_family": "execution",
                    "action_value_lane": "execution",
                    "learning_lane": "execution",
                    "action_preference": "positive_candidate_execution",
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                    "reward_sum": 9000,
                    "reward_mean": 9000,
                    "sample_count": 3,
                    "last_sample_date": "2025-03-04",
                    "payload": {
                        "product_learning_performance_key": {
                            "entry_quality_outcome": {
                                "positive_entry_episode": True,
                                "support_weight": 0.80,
                            }
                        }
                    },
                }
            ],
        )

        base_row = base_scorecard["long"]
        execution_row = execution_scorecard["long"]
        self.assertGreater(
            execution_row["opportunity_score_components"]["execution_profile_learning"],
            0.0,
        )
        self.assertEqual(execution_row["opportunity_score"], base_row["opportunity_score"])
        self.assertEqual(
            set(execution_row["rank_score_input_components"]),
            {"cold_start_evidence_quality"},
        )
        base_rank = _ensure_final_rank_score_fields(dict(base_row), config={})
        execution_rank = _ensure_final_rank_score_fields(dict(execution_row), config={})
        self.assertEqual(
            execution_rank["rank_score_components"]["trigger_execution_quality"],
            0.0,
        )
        self.assertEqual(execution_rank["rank_score"], base_rank["rank_score"])

    def test_similar_and_weak_prior_action_values_do_not_change_candidate_or_rank_score(self):
        base_signal = [_signal("technical", Signal.BULLISH, 0.72)]
        common = {
            "ticker": "P",
            "analyst_signals": base_signal,
            "signal_collection_contract": {"dominant_side": "long"},
            "market_confirmation": {"confirmation_score": 0.65},
            "data_quality_summary": {},
            "adaptive_policy_state": [],
            "alpha_setup_profiles": [],
            "decision_date": "2025-03-05",
            "config": {},
        }
        base_scorecard = build_opportunity_scorecard(**common, alpha_setup_action_values=[])
        prior_scorecard = build_opportunity_scorecard(
            **common,
            alpha_setup_action_values=[
                {
                    "canonical_action_value": True,
                    "consumer_scope": "pm_learning",
                    "retrieval_match_level": "similar",
                    "side": "long",
                    "action_name": "open",
                    "canonical_action_family": "open_add_new_risk",
                    "action_value_lane": "open",
                    "learning_lane": "open",
                    "action_preference": "positive_candidate_open",
                    "reward_source": "trade_episode",
                    "evidence_scope": "exact_real_state",
                    "reward_sum": 12000.0,
                    "reward_mean": 12000.0,
                    "sample_count": 6,
                },
                {
                    "canonical_action_value": True,
                    "consumer_scope": "pm_learning",
                    "side": "long",
                    "action_name": "open",
                    "canonical_action_family": "open_add_new_risk",
                    "action_value_lane": "open",
                    "learning_lane": "open",
                    "action_preference": "negative_revalidate",
                    "reward_source": "counterfactual",
                    "evidence_scope": "counterfactual_prior",
                    "reward_sum": -12000.0,
                    "reward_mean": -12000.0,
                    "sample_count": 6,
                },
            ],
        )

        base_row = base_scorecard["long"]
        prior_row = prior_scorecard["long"]
        self.assertEqual(prior_row["opportunity_score"], base_row["opportunity_score"])
        self.assertEqual(prior_row["candidate_quality"], base_row["candidate_quality"])
        self.assertEqual(
            _ensure_final_rank_score_fields(dict(prior_row), config={})["rank_score"],
            _ensure_final_rank_score_fields(dict(base_row), config={})["rank_score"],
        )
        self.assertEqual(prior_row["action_value_learning_summary"]["positive_count"], 0)
        self.assertEqual(prior_row["action_value_learning_summary"]["negative_count"], 0)

    def test_missing_consumer_scope_never_enters_candidate_quality_or_rank(self):
        common = {
            "ticker": "P",
            "analyst_signals": [_signal("technical", Signal.BULLISH, 0.72)],
            "signal_collection_contract": {"dominant_side": "long"},
            "market_confirmation": {"confirmation_score": 0.65},
            "data_quality_summary": {},
            "adaptive_policy_state": [],
            "alpha_setup_profiles": [],
            "decision_date": "2025-03-05",
            "config": {},
        }
        base = build_opportunity_scorecard(**common, alpha_setup_action_values=[])
        missing_scope = build_opportunity_scorecard(
            **common,
            alpha_setup_action_values=[
                {
                    "canonical_action_value": True,
                    "side": "long",
                    "action_name": "open",
                    "canonical_action_family": "open_add_new_risk",
                    "action_value_lane": "open",
                    "learning_lane": "open",
                    "action_preference": "positive_candidate_open",
                    "reward_source": "real_trade",
                    "evidence_scope": "exact_real_state",
                    "reward_sum": 9000.0,
                    "reward_mean": 9000.0,
                }
            ],
        )

        self.assertEqual(missing_scope["long"]["candidate_quality"], base["long"]["candidate_quality"])
        self.assertEqual(
            _ensure_final_rank_score_fields(dict(missing_scope["long"]), config={})["rank_score"],
            _ensure_final_rank_score_fields(dict(base["long"]), config={})["rank_score"],
        )

    def test_signed_negative_rank_keeps_probe_eligible_after_single_efficiency_component(self):
        pm_state = _pm_state("BU", 0, 1, with_scorecard=False)
        pm_state["opportunity_scorecard"] = {
            "preferred_side": "long",
            "long": {
                "side": "long",
                "final_state": "watch_for_trigger",
                "opportunity_score": 0.10,
                "score": 0.10,
                "candidate_quality": 0.10,
                "rank_score_input_components": {"cold_start_evidence_quality": 0.10},
                "opportunity_score_components": {"fusion_score_adjustment": -0.20},
            },
        }
        portfolio = SimpleNamespace(
            account_equity=1_000_000.0,
            cashflow=1_000_000.0,
            margin_used=0.0,
            positions={},
        )

        result = apply_full_market_capital_deployment(
            generated=[("BU", pm_state)],
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

        row = pm_state["opportunity_scorecard"]["long"]
        self.assertEqual(result["candidate_count"], 1)
        self.assertGreater(row["rank_score_components"]["capital_efficiency"], 0.0)
        self.assertLessEqual(row["rank_score_components"]["capital_efficiency"], 0.02)
        self.assertEqual(len(row["rank_score_components"]), 7)
        self.assertLess(sum(row["rank_score_components"].values()), 0.0)
        self.assertEqual(
            row["rank_score"],
            round(sum(row["rank_score_components"].values()), 6),
        )
        self.assertLess(row["rank_score"], 0.0)
        self.assertTrue(pm_state["capital_deployment"]["selected_for_capital_deployment"])

    def test_all_negative_rank_preserves_learning_order_in_budget_sequence(self):
        stronger = _pm_state("ZN", 0, 1, with_scorecard=False)
        weaker = _pm_state("AL", 0, 1, with_scorecard=False)
        for pm_state, ticker, learning_delta in (
            (stronger, "ZN", -0.02),
            (weaker, "AL", -0.12),
        ):
            pm_state["opportunity_scorecard"] = {
                "preferred_side": "long",
                "long": {
                    "side": "long",
                    "final_state": "watch_for_trigger",
                    "opportunity_score": 0.10,
                    "score": 0.10,
                    "candidate_quality": 0.10,
                    "rank_score_input_components": {"cold_start_evidence_quality": 0.10},
                    "action_value_learning_summary": {
                        "positive_learning_signal": 0.0,
                        "negative_learning_signal": abs(learning_delta),
                    },
                    "opportunity_score_components": {"fusion_score_adjustment": -0.20},
                },
            }
        portfolio = SimpleNamespace(
            account_equity=1_000_000.0,
            cashflow=1_000_000.0,
            margin_used=0.0,
            positions={},
        )

        apply_full_market_capital_deployment(
            generated=[("AL", weaker), ("ZN", stronger)],
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

        stronger_row = stronger["opportunity_scorecard"]["long"]
        weaker_row = weaker["opportunity_scorecard"]["long"]
        self.assertLess(stronger_row["rank_score"], 0.0)
        self.assertLess(weaker_row["rank_score"], 0.0)
        self.assertGreater(stronger_row["rank_score"], weaker_row["rank_score"])
        self.assertEqual(stronger["capital_deployment"]["rank_budget_sequence"], 1)
        self.assertEqual(weaker["capital_deployment"]["rank_budget_sequence"], 2)

    def test_full_market_rank_tie_uses_ticker_not_a_second_quality_sort(self):
        low = _pm_state("AL", 0, 1, with_scorecard=False)
        high = _pm_state("ZN", 0, 1, with_scorecard=False)
        for pm_state, ticker, quality in ((low, "AL", 0.20), (high, "ZN", 0.80)):
            pm_state["opportunity_scorecard"] = {
                "preferred_side": "long",
                "long": {
                    "side": "long",
                    "final_state": "watch_for_trigger",
                    "opportunity_score": 0.40,
                    "score": 0.40,
                    "candidate_quality": quality,
                    "rank_score_input_components": {"cold_start_evidence_quality": 0.40},
                    "opportunity_score_components": {},
                },
            }
        portfolio = SimpleNamespace(
            account_equity=1_000_000.0,
            cashflow=1_000_000.0,
            margin_used=0.0,
            positions={},
        )

        apply_full_market_capital_deployment(
            generated=[("AL", low), ("ZN", high)],
            config={
                "max_total_margin_ratio": 0.008,
                "position_budget_policy": {
                    "min_real_trade_margin_ratio": 0.008,
                    "max_single_ticker_margin_ratio": 0.13,
                },
                "net_exposure_control": {"max_net_exposure": 0.50},
            },
            portfolio=portfolio,
        )

        self.assertEqual(low["capital_deployment"]["opportunity_rank"], 1)
        self.assertTrue(low["capital_deployment"]["selected_for_capital_deployment"])
        self.assertEqual(high["capital_deployment"]["opportunity_rank"], 2)
        self.assertFalse(high["capital_deployment"]["selected_for_capital_deployment"])

    def test_capital_rank_sort_tuple_has_only_the_single_rank_score(self):
        row = {
            "capital_priority_tier": 2,
            "rank_score": 0.50,
            "rank_score_input_components": {"cold_start_evidence_quality": 0.40},
            "candidate_quality": 0.70,
            "rank_score_components": {"capital_efficiency": 0.01},
        }

        self.assertEqual(
            _capital_rank_sort_tuple(row),
            (0.50,),
        )

    def test_rank_policy_catalog_has_no_inactive_execution_or_stale_window_keys(self):
        policy_path = SRC_ROOT / "config" / "rank_score_policy.yaml"
        policy_text = policy_path.read_text(encoding="utf-8-sig")
        policy = yaml.safe_load(policy_text)["rank_score_policy"]["rank_score"]

        self.assertNotIn("execution_profile_learning_weight", policy_text)
        self.assertNotIn("tuning_window", policy_text)
        self.assertNotIn("cold_start_evidence_weight", policy_text)
        self.assertNotIn("capital_layer_priority_bonus", policy_text)
        self.assertEqual(
            set(policy),
            {
                "cold_start_evidence_quality",
                "capital_layer_priority",
                "open_add_action_value_delta",
                "product_setup_trigger_history",
                "trigger_execution_quality",
                "capital_efficiency",
                "conflict_risk_invalidation_penalty",
            },
        )
        self.assertEqual(
            policy["capital_layer_priority"],
            {
                "alpha_scale": 6.0,
                "real_budget": 3.0,
                "exploration_probe": 0.0,
            },
        )
        self.assertEqual(
            policy["trigger_execution_quality"],
            {"current_trigger_quality_weight": 0.08},
        )
        self.assertEqual(
            set(policy["open_add_action_value_delta"]),
            {
                "max_abs_delta",
                "positive_learning_signal",
                "trigger_quality_positive_signal",
                "negative_learning_signal",
                "recent_tail_loss_signal",
                "entry_quality_loss_signal",
                "net_trigger_quality_loss_signal",
            },
        )

    def test_rank_score_policy_catalog_weight_changes_rank_score(self):
        base_signal = [_signal("technical", Signal.BULLISH, 0.72)]
        action_values = [
            {
                "canonical_action_value": True,
                "consumer_scope": "pm_learning",
                "side": "long",
                "action_name": "add_or_open",
                "canonical_action_family": "open_add_new_risk",
                "action_value_lane": "open",
                "learning_lane": "open",
                "action_preference": "positive_candidate_open",
                "reward_source": "trade_episode",
                "evidence_scope": "exact_real_state",
                "reward_sum": 6000,
                "reward_mean": 6000,
                "sample_count": 3,
                "last_sample_date": "2025-03-04",
            }
        ]
        scorecard = build_opportunity_scorecard(
            ticker="P",
            analyst_signals=base_signal,
            signal_collection_contract={"dominant_side": "long"},
            market_confirmation={"confirmation_score": 0.65},
            data_quality_summary={},
            adaptive_policy_state=[],
            alpha_setup_profiles=[],
            alpha_setup_action_values=action_values,
            decision_date="2025-03-05",
            config={},
        )

        default_row = _ensure_final_rank_score_fields(dict(scorecard["long"]), config={})
        boosted_row = _ensure_final_rank_score_fields(
            dict(scorecard["long"]),
            config={
                "rank_score_policy": {
                    "rank_score": {
                        "open_add_action_value_delta": {
                            "positive_learning_signal": 0.30,
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

    def test_step4_capital_layer_is_counted_once_inside_the_single_rank(self):
        scores = {}
        for layer in (
            CAPITAL_LAYER_EXPLORATION,
            "real_budget_entry",
            CAPITAL_LAYER_ALPHA_SCALE,
        ):
            row = {
                "capital_layer": layer,
                "final_state": "tradeable_candidate",
                "rank_score_input_components": {"cold_start_evidence_quality": 0.40},
                "opportunity_score_components": {},
                "action_value_learning_summary": {},
            }
            ranked = _ensure_final_rank_score_fields(row, config={})
            scores[layer] = ranked["rank_score"]
        self.assertGreater(scores[CAPITAL_LAYER_ALPHA_SCALE], scores["real_budget_entry"])
        self.assertGreater(scores["real_budget_entry"], scores[CAPITAL_LAYER_EXPLORATION])

    def test_single_rank_layer_bases_dominate_all_other_valid_component_extremes(self):
        strongest_lower_layer = {
            "final_state": "tradeable_candidate",
            "rank_score_input_components": {"cold_start_evidence_quality": 1.0},
            "trigger_quality_score": 1.0,
            "opportunity_score_components": {"alpha_profile_adjustment": 0.09},
            "action_value_learning_summary": {"positive_learning_signal": 1.0},
        }
        weakest_higher_layer = {
            "final_state": "tradeable_candidate",
            "rank_score_input_components": {"cold_start_evidence_quality": 0.0},
            "trigger_quality_score": 0.0,
            "opportunity_score_components": {
                "alpha_profile_adjustment": -0.08,
                "fusion_score_adjustment": -0.18,
                "market_conflict_penalty": -0.10,
                "critical_data_gap_penalty": -0.12,
                "fundamental_gap_penalty": -0.06,
            },
            "gating_failures": [f"failure_{index}" for index in range(8)],
            "action_value_learning_summary": {"negative_learning_signal": 1.0},
        }
        probe = _ensure_final_rank_score_fields(
            {**strongest_lower_layer, "capital_layer": CAPITAL_LAYER_EXPLORATION},
            config={},
            capital_efficiency_bonus=0.02,
        )
        real = _ensure_final_rank_score_fields(
            {**weakest_higher_layer, "capital_layer": "real_budget_entry"},
            config={},
        )
        strongest_real = _ensure_final_rank_score_fields(
            {**strongest_lower_layer, "capital_layer": "real_budget_entry"},
            config={},
            capital_efficiency_bonus=0.02,
        )
        scale = _ensure_final_rank_score_fields(
            {**weakest_higher_layer, "capital_layer": CAPITAL_LAYER_ALPHA_SCALE},
            config={},
        )

        self.assertGreater(real["rank_score"], probe["rank_score"])
        self.assertGreater(scale["rank_score"], strongest_real["rank_score"])

    def test_rank_budget_sequence_uses_layer_bases_inside_the_only_rank_score(self):
        generated = []
        for ticker, layer, evidence in (
            ("AL", CAPITAL_LAYER_EXPLORATION, 1.0),
            ("ZN", "real_budget_entry", 0.0),
            ("CU", CAPITAL_LAYER_ALPHA_SCALE, 0.0),
        ):
            state = _pm_state(ticker, 0, 1, with_scorecard=False)
            state["final_entry_authority"]["capital_layer"] = layer
            state["opportunity_scorecard"] = {
                "preferred_side": "long",
                "long": {
                    "side": "long",
                    "final_state": "tradeable_candidate",
                    "opportunity_score": evidence,
                    "score": evidence,
                    "candidate_quality": evidence,
                    "rank_score_input_components": {
                        "cold_start_evidence_quality": evidence,
                    },
                    "trigger_quality_score": evidence,
                    "opportunity_score_components": {},
                    "action_value_learning_summary": {},
                },
            }
            generated.append((ticker, state))
        portfolio = SimpleNamespace(
            account_equity=1_000_000.0,
            cashflow=1_000_000.0,
            margin_used=0.0,
            positions={},
        )

        apply_full_market_capital_deployment(
            generated=generated,
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

        by_ticker = {ticker: state for ticker, state in generated}
        self.assertEqual(
            by_ticker["CU"]["capital_deployment"]["rank_budget_sequence"],
            1,
        )
        self.assertEqual(
            by_ticker["ZN"]["capital_deployment"]["rank_budget_sequence"],
            2,
        )
        self.assertEqual(
            by_ticker["AL"]["capital_deployment"]["rank_budget_sequence"],
            3,
        )

    def test_historical_trigger_outcome_and_current_trigger_quality_enter_distinct_rank_components(self):
        base = {
            "capital_layer": CAPITAL_LAYER_EXPLORATION,
            "final_state": "tradeable_candidate",
            "rank_score_input_components": {"cold_start_evidence_quality": 0.5},
            "opportunity_score_components": {
                "trigger_quality_positive_bonus": 0.08,
                "trigger_quality_loss_penalty": -0.10,
            },
            "action_value_learning_summary": {
                "positive_learning_signal": 0.5,
                "trigger_quality_positive_signal": 0.75,
            },
        }
        weak_today = _ensure_final_rank_score_fields(
            {**copy.deepcopy(base), "trigger_quality_score": 0.2},
            config={},
        )
        strong_today = _ensure_final_rank_score_fields(
            {**copy.deepcopy(base), "trigger_quality_score": 0.8},
            config={},
        )

        self.assertEqual(
            weak_today["rank_score_components"]["open_add_action_value_delta"],
            strong_today["rank_score_components"]["open_add_action_value_delta"],
        )
        self.assertEqual(
            weak_today["rank_score_components"]["trigger_execution_quality"],
            0.016,
        )
        self.assertEqual(
            strong_today["rank_score_components"]["trigger_execution_quality"],
            0.064,
        )

    def test_rank_python_uses_catalog_keys_and_registered_rank_input_names(self):
        deployment_source = (
            SRC_ROOT / "tools" / "agent_tools" / "decision" / "pm_full_market_capital_deployment.py"
        ).read_text(encoding="utf-8-sig")
        fusion_source = (
            SRC_ROOT / "tools" / "agent_tools" / "decision" / "pm_signal_fusion.py"
        ).read_text(encoding="utf-8-sig")

        for stale_name in (
            "rank_candidate_input_components",
            "final_rank_score_generated_by",
            "positive_signal_weight",
            "negative_signal_weight",
            "recent_tail_loss_signal_weight",
            "capital_layer_priority_bonus",
            "alpha_profile_adjustment_weight",
            "fusion_conflict_adjustment_weight",
        ):
            self.assertNotIn(stale_name, deployment_source + fusion_source)
        self.assertNotIn('action_preference.startswith("negative")', fusion_source)

        policy = yaml.safe_load(
            (SRC_ROOT / "config" / "rank_score_policy.yaml").read_text(encoding="utf-8-sig")
        )["rank_score_policy"]["rank_score"]
        tuning_keys = set()
        for component, component_policy in policy.items():
            tuning_keys.add(component)
            if isinstance(component_policy, dict):
                tuning_keys.update(component_policy)
        for tuning_key in tuning_keys:
            self.assertIn(f'"{tuning_key}"', deployment_source)

        field_matrix = (PROJECT_ROOT / "docs" / "matrix_field_semantics.md").read_text(encoding="utf-8-sig")
        for registered_name in (
            "rank_score_input_components",
            "positive_learning_signal",
            "negative_learning_signal",
            "recent_tail_loss_signal",
            "alpha_scale_eligible",
        ):
            self.assertIn(f"`{registered_name}`", field_matrix)

        row = {
            "final_state": "tradeable_candidate",
            "opportunity_score": 0.6,
            "direction_evidence_strength": 0.7,
            "setup_quality_score": 0.8,
            "trigger_quality_score": 0.9,
            "rank_score_input_components": {"cold_start_evidence_quality": 0.5},
            "opportunity_score_components": {
                "positive_learning": 0.1,
                "negative_learning": -0.02,
                "entry_quality_loss_penalty": -0.01,
                "trigger_quality_positive_bonus": 0.03,
                "trigger_quality_loss_penalty": -0.02,
            },
        }
        ranked = _ensure_final_rank_score_fields(row, config={})
        rank_inputs = rank_trace_for_row(ranked)["rank_input_components"]
        self.assertIn("positive_learning", rank_inputs)
        self.assertIn("negative_learning", rank_inputs)
        self.assertNotIn("positive_learning_component", rank_inputs)
        self.assertNotIn("negative_learning_component", rank_inputs)

    def test_repeated_alpha_candidate_uses_same_rank_with_alpha_scale_layer(self):
        result = select_ticker_side(
            ticker="EB",
            analyst_signals=[],
            signal_collection_contract={"dominant_side": "short"},
            market_confirmation={},
            data_quality_summary={},
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
                    "alpha_scale_eligible": True,
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
        self.assertNotIn("alpha_scale_eligible", scorecard["short"])
        step5_row = dict(scorecard["short"])
        step5_row["alpha_scale_eligible"] = True
        step5_row["capital_layer"] = CAPITAL_LAYER_ALPHA_SCALE
        metadata = rank_metadata_for_row(_ensure_final_rank_score_fields(step5_row, config={}))
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
                        {
                            "learning_lane": "hold",
                            "action_value_lane": "hold",
                            "action_name": "hold",
                            "canonical_action_family": "hold",
                            "action_preference": "negative_hold_revalidate",
                        },
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
