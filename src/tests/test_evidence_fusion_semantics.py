import unittest
import sys
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.constants import Signal
from graph.schema import AnalystSignal
from agents.decision_team.auditor import audit_futures_recommendation
from agents.decision_team.portfolio_manager import (
    _collect_applied_adaptive_policies,
    _technical_policy_applications_from_signals,
)
from tools.agent_tools.analysis.analyst_quality import apply_trade_research_contract
from tools.agent_tools.decision.pm_signal_fusion import build_opportunity_scorecard
from tools.agent_tools.decision.pm_ticker_side_selection import select_ticker_side
from tools.agent_tools.decision.pm_contract_builder import build_final_action_contract
from tools.agent_tools.decision.pm_lifecycle_learning_router import route_lifecycle_learning
from tools.agent_tools.decision.pm_lifecycle_action_port import classify_lifecycle_action_port
from tools.agent_tools.research.research_review_helpers import _feedback_learning_refs
from tools.common.evidence_fusion_semantics import (
    build_analyst_fusion_evidence,
    build_reviewer_fusion_attribution,
)
from tools.common.execution_trigger_semantics import (
    canonical_entry_invalidation_condition,
)
from tools.common.signal_evidence_collection import (
    build_pm_evidence_signals_from_scc,
    build_signal_collection_contract,
)
from tests.contract_test_fixtures import build_test_aec


class EvidenceFusionSemanticsTest(unittest.TestCase):
    def _analyst_signal(self, *, analyst: str, signal: Signal, confidence: float = 0.72) -> AnalystSignal:
        entry_timing_signal = {
            "technical": "breakout",
            "commodity_news": "event_immediate",
        }.get(analyst, "")
        sig = AnalystSignal(
            agent_name=analyst,
            signal=signal,
            confidence=confidence,
            business_quality_score=0.74,
            evidence_quality="high",
            entry_timing_signal=entry_timing_signal,
            entry_trigger="current price/volume confirms directional setup",
            exit_hint="setup invalid if price closes back through trigger and opposite evidence appears",
            would_change_view_if="setup invalid if price closes back through trigger and opposite evidence appears",
            setup_type="trend_breakout",
            factor_focus=["price", "inventory"],
            current_evidence_conflict=[],
            invalidation_level=(
                95.0
                if analyst in {"technical", "commodity_news"}
                and signal == Signal.BULLISH
                else 105.0
                if analyst in {"technical", "commodity_news"}
                and signal == Signal.BEARISH
                else None
            ),
            position_invalidation_level=(
                94.0 if signal == Signal.BULLISH else 106.0 if signal == Signal.BEARISH else None
            ),
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
        result = apply_trade_research_contract(
            sig,
            context,
            analyst=analyst,
            trading_date="2025-05-06",
            ticker="RB",
        )
        current_contract = result.metadata["action_evidence_contract"]
        complete_contract = build_test_aec(
            analyst,
            ticker="RB",
            trading_date="2025-05-06",
            signal=signal.value,
            side="long" if signal == Signal.BULLISH else "short" if signal == Signal.BEARISH else "flat",
            confidence=confidence,
        )
        complete_contract.update(current_contract)
        result.metadata = {
            "action_evidence_contract": complete_contract,
            "signal_record_id": f"{analyst}-fixture",
        }
        return result

    def test_analyst_landing_adds_fusion_evidence_without_trade_authority(self):
        signal = self._analyst_signal(analyst="technical", signal=Signal.BULLISH)
        contract = signal.metadata["action_evidence_contract"]
        fusion = contract["fusion_evidence"]
        self.assertEqual(fusion["contract_version"], "agentquant.evidence_fusion.v1")
        self.assertIn(signal.evidence_strength, {"strong", "medium", "weak", "unknown"})
        self.assertTrue(signal.confirmation_requirements)
        forbidden = {"target_lots", "lots_delta", "final_action", "final_action_contract", "authority_type"}
        self.assertFalse(forbidden.intersection(fusion))

    def test_formal_freshness_sources_are_used_without_risk_flag_pollution(self):
        technical = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.66,
            business_quality_score=0.62,
            setup_quality_score=0.64,
            evidence_quality="medium",
        )
        technical_fusion = build_analyst_fusion_evidence(
            technical,
            {"risk_flags": ["choppy", "conflicting_indicators", "false_breakout"]},
            analyst="technical",
            ticker="BU",
        )
        self.assertEqual(technical_fusion["evidence_freshness_score"], 0.0)
        self.assertEqual(technical_fusion["evidence_freshness"], "unknown")

        technical.data_freshness = "fresh"
        untrusted_text_fusion = build_analyst_fusion_evidence(
            technical,
            {},
            analyst="technical",
            ticker="BU",
        )
        self.assertEqual(untrusted_text_fusion["evidence_freshness_score"], 0.0)
        self.assertEqual(untrusted_text_fusion["evidence_freshness"], "unknown")

        stale_technical_fusion = build_analyst_fusion_evidence(
            technical,
            {"freshness_score": 0.35},
            analyst="technical",
            ticker="BU",
        )
        self.assertEqual(stale_technical_fusion["evidence_freshness_score"], 0.35)
        self.assertEqual(stale_technical_fusion["evidence_freshness"], "stale")

        fundamental = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.NEUTRAL,
            confidence=0.45,
            business_quality_score=0.50,
            evidence_quality="medium",
            metadata={
                "data_usage_summary": {
                    "sources": {
                        "finoview_fundamental": {"freshness_score": 0.41},
                    }
                }
            },
        )
        fundamental_fusion = build_analyst_fusion_evidence(
            fundamental,
            {"data_quality": {"factor_freshness_score": 0.41}},
            analyst="fundamental",
            ticker="BU",
        )
        self.assertEqual(fundamental_fusion["evidence_freshness_score"], 0.41)
        self.assertEqual(fundamental_fusion["evidence_freshness"], "stale")

        news = AnalystSignal(
            agent_name="commodity_news",
            signal=Signal.BULLISH,
            confidence=0.70,
            business_quality_score=0.70,
            evidence_quality="high",
            metadata={
                "data_usage_summary": {
                    "sources": {
                        "finoview_news_txt": {"freshness_score": 0.88},
                    }
                }
            },
        )
        news_fusion = build_analyst_fusion_evidence(
            news,
            {},
            analyst="commodity_news",
            ticker="BU",
        )
        self.assertEqual(news_fusion["evidence_freshness_score"], 0.88)
        self.assertEqual(news_fusion["evidence_freshness"], "fresh")

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
        for signal in signals:
            analyst = signal.agent_name
            source_fusion = signal.metadata["action_evidence_contract"]["fusion_evidence"]
            self.assertEqual(
                contract["evidence_fusion"]["evidence_strength_by_analyst"][analyst],
                source_fusion["evidence_strength"],
            )
            self.assertEqual(
                contract["evidence_fusion"]["evidence_freshness_by_analyst"][analyst],
                source_fusion["evidence_freshness"],
            )
        self.assertGreater(contract["evidence_fusion"]["multi_evidence_consensus_score"], 0.0)
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
        self.assertEqual(selected["opportunity_scorecard"]["preferred_side"], "long")
        self.assertEqual(
            collection["evidence_fusion"]["dominant_opposing_evidence"][0]["analyst"],
            "fundamental",
        )
        self.assertNotIn(
            "same_horizon_direction_opposition",
            {
                str(conflict)
                for row in collection["evidence_fusion"]["cross_analyst_conflicts"]
                for conflict in row.get("conflicts", [])
            },
        )
        self.assertEqual(contract["evidence_used"]["side_priority"], 1)
        self.assertTrue(
            contract["evidence_used"]["side_priority_is_not_capital_rank"]
        )
        self.assertIn("candidate_quality", contract["evidence_used"])
        self.assertIn("candidate_layer_hint", contract["evidence_used"])
        self.assertNotIn("capital_priority_score", contract["evidence_used"])
        self.assertNotIn("capital_priority_tier", contract["evidence_used"])
        self.assertNotIn("opportunity_rank", contract["evidence_used"])
        self.assertNotIn("rank_capital_role", contract["evidence_used"])
        self.assertNotIn("capital_layer", contract["evidence_used"])
        self.assertNotIn("capital_ratio_source", contract["evidence_used"])
        self.assertNotIn("rank_reason", contract["evidence_used"])
        self.assertNotIn("rank_capital_priority_real_budget_release", contract["evidence_used"])
        self.assertNotIn("rank_capital_priority_release_detail", contract["evidence_used"])
        recommendation = {
            "id": "rec-1",
            "source_type": "strategy",
            "underlying_code": "RB",
            "effective_trade_date": "2025-05-06",
            "signal_snapshot": {"final_action_contract": contract},
        }
        audit = audit_futures_recommendation(
            recommendation=recommendation,
            hard_risk_config={"max_total_margin_ratio": 0.20},
            account_state={
                "account_equity": 5_000_000,
                "margin_used": 0.0,
                "margin_ratio": 0.0,
                "risk_status": "NORMAL",
            },
            position_state={"current_lots": 0, "contract_code": None},
            contract_state={},
            data_quality={"status": "clean", "source": "signal_collection_contract"},
        )
        self.assertNotIn("pm_fusion_explanation_audit", audit.audit_payload)
        self.assertNotIn("pm_memory_consumption_audit", audit.audit_payload)

    def test_auditor_accepts_numeric_invalidation_from_signed_contract(self):
        recommendation = {
            "id": "rec-numeric-invalidation",
            "source_type": "strategy",
            "underlying_code": "RB",
            "effective_trade_date": "2025-05-06",
            "signal_snapshot": {
                "final_action_contract": {
                    "current_lots": 0,
                    "target_lots": 1,
                    "lots_delta": 1,
                    "final_action": "open_probe",
                    "contract_code": "rb2510",
                    "execution_profile": "breakout",
                    "invalidation": canonical_entry_invalidation_condition(
                        "breakout",
                        "long",
                    ),
                    "invalidation_level": 3100.0,
                    "target_margin_ratio_estimate": 0.01,
                }
            },
        }

        audit = audit_futures_recommendation(
            recommendation=recommendation,
            hard_risk_config={"max_total_margin_ratio": 0.20},
            account_state={
                "account_equity": 5_000_000,
                "margin_used": 0.0,
                "margin_ratio": 0.0,
                "risk_status": "NORMAL",
            },
            position_state={"current_lots": 0, "contract_code": None},
            contract_state={
                "contract_code": "RB2510",
                "underlying_code": "RB",
                "as_of_date": "2025-05-06",
                "source": "test_contract_cache",
            },
            data_quality={"status": "clean", "source": "signal_collection_contract"},
        )

        self.assertNotIn("missing_invalidation_condition", audit.hard_risk_reasons)
        self.assertEqual(audit.audit_verdict, "approve")

    def test_final_contract_preserves_execution_trigger_profile_learning_route(self):
        action_values = [
            {
                "id": "hold-1",
                "ticker": "RB",
                "side": "long",
                "action_name": "hold",
                "canonical_action_family": "hold",
                "action_value_lane": "hold",
                "learning_lane": "hold",
                "action_preference": "positive_candidate_hold",
                "canonical_action_value": True,
                "consumer_scope": "pm_learning",
                "memory_side_role": "current_position_side",
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
                "learning_lane": "open",
                "action_preference": "positive_candidate_open",
                "canonical_action_value": True,
                "consumer_scope": "pm_learning",
                "memory_side_role": "target_side",
                "reward_mean": 0.18,
                "sample_count": 6,
            },
            {
                "id": "open-opposite-side",
                "ticker": "RB",
                "side": "short",
                "action_name": "open",
                "canonical_action_family": "open_add_new_risk",
                "action_value_lane": "open",
                "learning_lane": "open",
                "action_preference": "positive_candidate_open",
                "canonical_action_value": True,
                "consumer_scope": "pm_learning",
                "memory_side_role": "target_side",
                "reward_mean": 0.22,
                "sample_count": 7,
            },
            {
                "id": "exec-1",
                "ticker": "RB",
                "side": "long",
                "action_name": "execution",
                "canonical_action_family": "execution",
                "action_value_lane": "execution",
                "learning_lane": "execution",
                "action_preference": "positive_candidate_execution",
                "canonical_action_value": True,
                "consumer_scope": "pm_learning",
                "memory_side_role": "historical_sample_side",
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
                    "rank_score_open_add_learning_delta": 0.999,
                    "learning_impact_delta": 0.999,
                },
                "holding_rebalance_control": {"decision": "unrelated_hold"},
                "winning_template_continuation": {"decision": "unrelated_reduce"},
                "conditional_monitor_probe_plan": {"decision": "unrelated_monitor"},
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
            adaptive_policy_applied=[
                {
                    "id": "policy-1",
                    "policy_type": "fast_loss_sentinel",
                    "policy_action": "cap",
                    "ticker": "RB",
                    "side": "long",
                    "setup_type": "trend_breakout",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "source_trading_date": "2025-05-05",
                    "valid_until": "2025-05-12",
                }
            ],
            execution_contract_fields={
                "execution_profile": "pullback",
                "execution_action_value_preference": {
                    "enabled": True,
                    "base_execution_profile": "breakout",
                    "execution_profile": "pullback",
                },
                "capital_deployment": {
                    "selected_for_capital_deployment": True,
                    "learning_impact_delta": {
                        "rank_score_open_add_learning_delta": 0.031,
                    },
                },
            },
        )
        trace = contract["learning_used"]["pm_lifecycle_learning_trace"]
        self.assertEqual(trace["contract_lifecycle_port"], "open_add_new_risk")
        self.assertNotIn("primary_lifecycle_action_port", trace)
        self.assertNotIn("contract_lifecycle_self_check", trace)
        self.assertNotIn("lifecycle_port_transition_reason", trace)
        self.assertNotIn("lifecycle_transition_diagnostic", trace)
        self.assertNotIn("lifecycle_transition_reason", trace)
        self.assertEqual([row["id"] for row in trace["decision_learning_rows"]], ["open-1"])
        self.assertTrue(trace["decision_learning_rows"][0]["canonical_action_value"])
        self.assertEqual(
            trace["decision_learning_rows"][0]["consumer_scope"],
            "pm_learning",
        )
        self.assertEqual([row["id"] for row in trace["trigger_profile_learning"]], ["exec-1"])
        self.assertNotIn("hold-1", {row.get("id") for row in trace["decision_learning_rows"]})
        self.assertNotIn(
            "open-opposite-side",
            {row.get("id") for row in trace["decision_learning_rows"]},
        )
        self.assertNotIn(
            "open-opposite-side",
            {row.get("id") for row in contract["learning_used"]["alpha_setup_action_values"]},
        )
        self.assertNotIn("exec-1", {row.get("id") for row in trace["rejected_learning"]})
        self.assertFalse(trace["execution_profile_learning_direct_to_rank"])
        self.assertEqual(
            contract["learning_used"]["pm_lifecycle_learning_router"]["pm_lifecycle_action_port"],
            "open_add_new_risk",
        )
        self.assertEqual(
            [row["id"] for row in contract["learning_used"]["adaptive_policy_applied"]],
            ["policy-1"],
        )
        impact = contract["learning_used"]["pm_lifecycle_learning_impact_delta"]
        self.assertEqual(impact["open_add_rank_score_delta"], 0.031)
        self.assertIsNone(impact["hold_decision"])
        self.assertIsNone(impact["reduce_exit_decision"])
        self.assertIsNone(impact["conditional_monitor_decision"])
        self.assertTrue(trace["open_add_learning_decision"])
        self.assertEqual(trace["hold_learning_decision"], {})
        self.assertEqual(trace["reduce_exit_learning_decision"], {})
        self.assertEqual(trace["conditional_monitor_learning_decision"], {})
        self.assertFalse(impact["execution_profile_learning_direct_to_rank"])

    def test_position_feedback_refs_require_formal_and_final_decision_row_match(self):
        formal = {
            "id": "open-1",
            "ticker": "RB",
            "side": "long",
            "setup_type": "trend_breakout",
            "action_name": "open",
            "canonical_action_family": "open_add_new_risk",
            "action_value_lane": "open",
            "learning_lane": "open",
            "action_preference": "positive_candidate_open",
            "canonical_action_value": True,
            "consumer_scope": "pm_learning",
            "reward_source": "trade_episode",
            "evidence_scope": "exact_real_state",
            "reward_mean": 1250.0,
            "sample_count": 3,
        }
        decision = {
            "id": "open-1",
            "canonical_action_family": "open_add_new_risk",
            "lane": "open",
            "canonical_action_value": True,
            "consumer_scope": "pm_learning",
        }
        refs, policies = _feedback_learning_refs({
            "learning_used": {
                "alpha_setup_action_values": [formal],
                "pm_lifecycle_learning_trace": {
                    "decision_learning_rows": [decision],
                },
                "learning_context": {
                    "memory_trace": {
                        "selected_memory_refs": [{"id": "legacy-must-not-leak"}],
                    },
                },
            },
        })

        self.assertEqual([row["id"] for row in refs], ["open-1"])
        self.assertEqual(policies, [])
        self.assertTrue(refs[0]["canonical_action_value"])
        self.assertEqual(refs[0]["consumer_scope"], "pm_learning")

        unmatched_refs, _ = _feedback_learning_refs({
            "learning_used": {
                "alpha_setup_action_values": [formal],
                "pm_lifecycle_learning_trace": {
                    "decision_learning_rows": [{**decision, "lane": "exit"}],
                },
                "learning_context": {
                    "memory_trace": {
                        "selected_memory_refs": [{"id": "legacy-must-not-leak"}],
                    },
                },
            },
        })
        self.assertEqual(unmatched_refs, [])
        for decision_override in (
            {"canonical_action_value": False},
            {"consumer_scope": "analysis_calibration"},
        ):
            with self.subTest(decision_override=decision_override):
                invalid_refs, _ = _feedback_learning_refs({
                    "learning_used": {
                        "alpha_setup_action_values": [formal],
                        "pm_lifecycle_learning_trace": {
                            "decision_learning_rows": [
                                {**decision, **decision_override}
                            ],
                        },
                    },
                })
                self.assertEqual(invalid_refs, [])

        policy_only_refs, policy_only_policies = _feedback_learning_refs({
            "learning_used": {
                "adaptive_policy_applied": [{
                    "id": "policy-only-1",
                    "policy_type": "learning_mechanism:alpha_setup_ev",
                    "policy_action": "cap",
                    "ticker": "RB",
                    "side": "long",
                    "setup_type": "trend_breakout",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "source_trading_date": "2025-05-05",
                    "valid_until": "2025-05-12",
                }],
            },
        })
        self.assertEqual(policy_only_refs, [])
        self.assertEqual([row["id"] for row in policy_only_policies], ["policy-only-1"])

    def test_actual_policy_collection_uses_existing_signal_and_control_paths(self):
        technical_signal = type(
            "TechnicalSignal",
            (),
            {
                "learning_impact_summary": {
                    "technical_parameter_calibration_applied": True,
                    "technical_parameter_calibrations": [
                        {
                            "policy_id": "technical-policy-1",
                            "policy_type": "contextual_rule_calibration:technical_parameters",
                            "policy_action": "calibrate",
                            "ticker": "RB",
                            "side": "*",
                            "setup_type": "*",
                            "horizon_class": "short",
                            "market_regime": "trend",
                            "source_trading_date": "2025-05-05",
                            "valid_until": "2025-05-20",
                            "parameter_changes": {
                                "trend.short": {"from": 10, "to": 9},
                            },
                        }
                    ],
                }
            },
        )()
        score_policy = {
            "id": "score-policy-1",
            "policy_type": "alpha_promotion",
            "policy_action": "protect",
            "ticker": "RB",
            "side": "long",
            "setup_type": "trend_breakout",
            "horizon_class": "short",
            "market_regime": "trend",
            "source_trading_date": "2025-05-01",
            "valid_until": "2025-05-30",
        }
        cap_policy = {
            **score_policy,
            "id": "cap-policy-1",
            "policy_type": "fast_loss_sentinel",
            "policy_action": "cap",
        }
        retrieved_only = {
            **score_policy,
            "id": "retrieved-only",
            "policy_action": "watchlist",
        }
        applied = _collect_applied_adaptive_policies(
            analyst_signals=[technical_signal],
            control_diagnostics={
                "adaptive_policy_position_control": {
                    "decision": "cap_applied",
                    "pre_control_ratio": 0.02,
                    "final_ratio": 0.01,
                    "applied_policy": cap_policy,
                }
            },
            control_reasons=["fast_loss_sentinel"],
            opportunity_scorecard={
                "preferred_side": "long",
                "long": {
                    "opportunity_score_components": {"positive_learning": 0.066},
                    "action_value_learning_summary": {
                        "positive_learning_signal": 0.10,
                        "negative_learning_signal": 0.0,
                        "positive_amplification_suspended": False,
                    },
                },
            },
            adaptive_policy_state=[score_policy, cap_policy, retrieved_only],
            final_position_ratio=0.01,
        )

        self.assertEqual(
            [row["id"] for row in applied],
            ["technical-policy-1", "score-policy-1", "cap-policy-1"],
        )
        self.assertTrue(
            all(
                set(row)
                == {
                    "id",
                    "policy_type",
                    "policy_action",
                    "ticker",
                    "side",
                    "setup_type",
                    "horizon_class",
                    "market_regime",
                    "source_trading_date",
                    "valid_until",
                }
                for row in applied
            )
        )

    def test_technical_policy_application_survives_aec_scc_pm_round_trip(self):
        signals = [
            self._analyst_signal(analyst="technical", signal=Signal.BULLISH),
            self._analyst_signal(analyst="fundamental", signal=Signal.BULLISH),
            self._analyst_signal(analyst="commodity_news", signal=Signal.BULLISH),
        ]
        impact = {
            "technical_parameter_calibration_applied": True,
            "technical_parameter_calibrations": [
                {
                    "policy_id": "technical-policy-round-trip",
                    "policy_type": "contextual_rule_calibration:technical_parameters",
                    "policy_action": "calibrate",
                    "ticker": "RB",
                    "side": "*",
                    "setup_type": "*",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "source_trading_date": "2025-05-05",
                    "valid_until": "2025-05-20",
                    "parameter_changes": {"trend.short": {"from": 10, "to": 9}},
                }
            ],
        }
        signals[0].learning_impact_summary = impact
        signals[0].metadata["action_evidence_contract"]["learning_impact_summary"] = impact
        collection = build_signal_collection_contract(
            ticker="RB",
            trading_date="2025-05-06",
            analyst_signals=signals,
            enabled_analysts=["technical", "fundamental", "commodity_news"],
        )

        pm_signals = build_pm_evidence_signals_from_scc(collection)
        applied = _technical_policy_applications_from_signals(pm_signals)

        self.assertEqual(
            [row["id"] for row in applied],
            ["technical-policy-round-trip"],
        )

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
