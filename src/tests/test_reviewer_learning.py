import json
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.decision.pm_risk_gate import PMRiskGate, PMRiskGateInput
from agents.decision_team.portfolio_manager import (
    _apply_capital_utilization_control,
    _quality_aware_fusion_context,
    _resolve_net_exposure_control,
    get_hard_allocation_margin_ratio,
)
from database.sqlite_setup import _ensure_reviewer_learning_schema, _ensure_strategy_memory_schema
from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.agent_tools.analysis.analyst_business_quality import apply_business_quality_enrichment
from tools.common.contracts import attach_snapshot_contract, validate_artifact_header
from database.artifact_store import load_externalized_json, load_externalized_text
from tools.common.template_prior import _project_path, classify_template_prior_item, load_template_prior_if_enabled
from tools.agent_tools.analysis.analyst_dynamic_weights import calibrate_weights_by_signal_history
from tools.agent_tools.analysis.analyst_learning_context import (
    apply_config_learning_overlay,
    build_learning_context,
    clear_learning_context_cache,
)
from tools.agent_tools.analysis.analyst_technical_parameter_calibration import apply_technical_parameter_calibration
from tools.common.learning_contract import CONTRACT_KEY
from tools.common.neutral_accountability import (
    build_neutral_accountability_summary,
    classify_neutral_signal,
)
from tools.agent_tools.analysis.analyst_quality import build_technical_context, apply_signal_quality_gate
from tools.agent_tools.research.research_learning import (
    CausalReviewLLMOutput,
    ExploratoryHypothesisItem,
    ExploratoryHypothesisLLMOutput,
    _execution_learning_from_snapshot,
    _write_alpha_setup_policy_state,
    run_researcher_causal_review,
    write_alpha_setup_profiles,
    write_exploratory_hypotheses,
)
from tools.common.alpha_setup import (
    _action_preference_from_stats,
    compact_product_learning_performance_key_for_analyst,
    infer_setup_type,
    upsert_alpha_setup_sample_and_profile,
)
from tools.agent_tools.research.reviewer_phase4_review import (
    _final_action_semantic_summary,
    _horizon_class,
    _validate_phase1_signal_persistence,
)
from tools.agent_tools.research.research_snapshot_reports import (
    _write_historical_learning_snapshot_report,
    learned_vs_unlearned_trade_performance,
    neutral_counterfactual_tracking_summary,
)
from tools.agent_tools.research.research_memory_writers import (
    _export_template_prior,
    _backfill_neutral_forward_counterfactual_tracking,
    _backfill_no_trade_opportunity_counterfactual_results,
    _select_representative_episode_payload,
    _write_alpha_promotion_state,
    _write_config_overlay,
    _write_contextual_rule_calibration_state,
    _write_learning_mechanism_policy_state,
    _write_learned_vs_unlearned_policy_state,
    _write_loss_template_observation_research,
    _write_no_trade_opportunity_memory,
    _write_opportunity_ranking_learning_events,
    _write_research_position_feedback,
    _write_signal_context_history,
    _write_tail_loss_sentinel_state,
    _write_trade_episode_memory,
    _write_validated_causal_policy_rules,
)
from tools.agent_tools.decision.pm_capital_allocator import enriched_policy_evidence


class _FakeLearningDB:
    def __init__(self):
        self.budgets = []
        self.digest_calls = 0

    def get_analyst_learning_digest(self, **kwargs):
        self.digest_calls += 1
        return [
            {
                "id": f"digest-{idx}",
                "ticker": kwargs["ticker"],
                "horizon_class": kwargs.get("horizon_class") or "short",
                "market_regime": "trend",
                "sample_count": 3 + idx,
                "confidence_score": 0.4,
                "digest_text": "mature observation " + ("x" * 80),
            }
            for idx in range(10)
        ]

    def save_learning_context_budget(self, **kwargs):
        self.budgets.append(kwargs)
        return True


class _ExploratoryLearningDB(_FakeLearningDB):
    def get_analyst_learning_digest(self, **kwargs):
        self.digest_calls += 1
        return []

    def get_trade_episode_memory(self, **kwargs):
        return [
            {
                "id": "episode-1",
                "ticker": kwargs.get("ticker", "BU"),
                "side": "long",
                "horizon_class": "short",
                "market_regime": "trend",
                "setup_type": "long_breakout_short",
                "holding_days": 2,
                "net_pnl": 1250.0,
                "lesson_text": "breakout held while inventory and price trend agreed",
                "payload": {
                    CONTRACT_KEY: {
                        "usable_memory": ["same-scope breakout episode worked"],
                        "usage_boundary": ["same-scope prior only"],
                        "position_impact_conditions": ["current confirmation and invalidation required"],
                    }
                },
            }
        ]

    def get_no_trade_opportunity_memory(self, **kwargs):
        return [
            {
                "id": "no-trade-1",
                "ticker": kwargs.get("ticker", "BU"),
                "side": "long",
                "horizon_class": "short",
                "market_regime": "trend",
                "setup_type": "long_breakout_short",
                "opportunity_type": "trend_continuation",
                "classification": "missed_opportunity",
                "pm_reason": "intraday trigger not met",
                "counterfactual_results": [{"horizon_days": 3, "counterfactual_pnl": 1800.0}],
                "payload": {
                    "neutral_opportunity_observations": [
                        {
                            "analyst": "technical",
                            "bucket": "watchlist_trigger",
                            "watchlist_priority": "medium",
                            "trigger_condition": "breakout confirms with volume",
                            "counterfactual_side": "long",
                        }
                    ],
                    CONTRACT_KEY: {
                        "usable_memory": ["skipped breakout became a missed opportunity"],
                        "usage_boundary": ["watchlist only until current trigger confirms"],
                        "position_impact_conditions": ["no sizing impact without current confirmation"],
                    },
                },
            }
        ]

    def get_exploratory_hypotheses(self, **kwargs):
        return [
            {
                "id": "hypothesis-1",
                "ticker": kwargs.get("ticker", "BU"),
                "sector": kwargs.get("sector", "energy"),
                "side": "long",
                "horizon_class": "short",
                "market_regime": "trend",
                "hypothesis_text": "BU trend probes need current price confirmation plus explicit invalidation.",
                "suggested_use": "analyst_prior",
                "payload": {
                    "entry_timing_hint": "wait for trend confirmation",
                    "exit_timing_hint": "cut if confirmation fails",
                    "holding_period_hint": "short",
                    "invalidation_condition": "price falls back below breakout level",
                    "validation_plan": "track next similar BU trend probes",
                    CONTRACT_KEY: {
                        "usable_memory": ["BU trend probes need confirmation"],
                        "usage_boundary": ["candidate hypothesis cannot control position"],
                        "position_impact_conditions": ["probe only until validated"],
                    },
                },
                "confidence_score": 0.55,
                "sample_count": 3,
                "status": "candidate",
            }
        ]


class _ActionValueLearningDB(_FakeLearningDB):
    def get_analyst_learning_digest(self, **kwargs):
        self.digest_calls += 1
        return []

    def get_alpha_setup_profiles(self, **kwargs):
        return []

    def get_alpha_setup_action_values(self, **kwargs):
        raise AssertionError("analyst learning context must not read trade-decision action-values")

    def get_similar_alpha_setup_action_values(self, **kwargs):
        raise AssertionError("analyst learning context must not read similar trade-decision action-values")


class _ProductLearningProfileDB(_FakeLearningDB):
    def get_analyst_learning_digest(self, **kwargs):
        self.digest_calls += 1
        return []

    def get_alpha_setup_profiles(self, **kwargs):
        return [
            {
                "id": "profile-eb-short",
                "scope_key": "EB|short|short|range|trend_breakout_setup|technical_news_combo",
                "ticker": "EB",
                "side": "short",
                "sector": "chemical",
                "horizon_class": "short",
                "market_regime": "range",
                "setup_type": "trend_breakout_setup",
                "data_combo": "technical:used|fundamental:used|news:fresh",
                "lifecycle_state": "protected",
                "profile_state_hint": "profile_protected",
                "sample_count": 7,
                "win_rate": 0.71,
                "profit_factor": 1.42,
                "net_pnl": 9200.0,
                "confidence_score": 0.76,
                "payload": {
                    "product_learning_performance_key": {
                        "contract_version": "agentquant.product_learning_performance_key.v1",
                        "performance_scope_key": (
                            "EB|short|trend_breakout_setup|opening_range_breakdown|"
                            "technical:used|fundamental:used|news:fresh|capital_deployed"
                        ),
                        "ticker": "EB",
                        "side": "short",
                        "horizon_class": "short",
                        "market_regime": "range",
                        "setup_type": "trend_breakout_setup",
                        "action_name": "open",
                        "trigger_key": "opening_range_breakdown",
                        "evidence_combo": "technical:used|fundamental:used|news:fresh",
                        "opportunity_state": "tradeable_candidate",
                        "deployment_outcome": {
                            "selected_for_capital_deployment": True,
                            "deployment_tier": "capital_deployed",
                            "authority_type": "real_budget_entry",
                            "final_action": "open_real",
                            "current_lots": 0,
                            "target_lots": -2,
                            "lots_delta": -2,
                            "opportunity_rank": 1,
                            "opportunity_score": 0.83,
                            "capital_allocation_reason": "ranked_deployable_candidate",
                        },
                        "outcome_label": "profit",
                        "net_pnl": 3180.0,
                        "reward_source": "ticker_daily_pnl",
                        "not_trade_authority": True,
                        "future_only": True,
                    }
                },
            }
        ]

    def get_alpha_setup_action_values(self, **kwargs):
        raise AssertionError("analysts must not read PM action-values for product learning")

    def get_similar_alpha_setup_action_values(self, **kwargs):
        raise AssertionError("analysts must not read similar PM action-values for product learning")


class _FallbackLearningDB:
    def __init__(self):
        self.calls = []
        self.budgets = []

    def get_analyst_learning_digest(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("horizon_class") != "*":
            return []
        return [
            {
                "id": "digest-short",
                "ticker": kwargs["ticker"],
                "horizon_class": "short",
                "market_regime": "*",
                "sample_count": 4,
                "confidence_score": 0.7,
                "digest_text": "short-horizon mature observation remains useful as a fallback prior",
            }
        ]

    def save_learning_context_budget(self, **kwargs):
        self.budgets.append(kwargs)
        return True


class _SectorFallbackLearningDB:
    def __init__(self):
        self.calls = []
        self.budgets = []

    def get_analyst_learning_digest(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("ticker") != "*" or kwargs.get("sector") != "ferrous":
            return []
        return [
            {
                "id": "sector-digest",
                "ticker": "HC",
                "sector": "ferrous",
                "horizon_class": "medium",
                "market_regime": "*",
                "sample_count": 5,
                "confidence_score": 0.65,
                "digest_text": "ferrous fundamental evidence worked when inventory and margin agreed",
            }
        ]

    def save_learning_context_budget(self, **kwargs):
        self.budgets.append(kwargs)
        return True


class _FakeOverlayDB:
    def get_config_learning_overlay(self, **kwargs):
        return [
            {
                "id": "ok",
                "param_key": "capital_utilization_control.target_margin_ratio_min",
                "learned_value": 0.16,
                "source": "reviewer",
                "confidence_score": 0.9,
            },
            {
                "id": "blocked",
                "param_key": "llm.model",
                "learned_value": "should-not-apply",
                "source": "reviewer",
                "confidence_score": 0.9,
            },
        ]


class _PriorBootstrapDB:
    def __init__(self, db_path: Path):
        self.db_path = str(db_path)

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_strategy_memory_schema(self, cursor):
        _ensure_strategy_memory_schema(cursor)


class ReviewerLearningContextTest(unittest.TestCase):
    def tearDown(self):
        clear_learning_context_cache()

    def test_learning_context_is_bounded_and_budget_logged(self):
        clear_learning_context_cache()
        db = _FakeLearningDB()
        context = build_learning_context(
            db=db,
            full_config={
                "learning": {"enabled": True},
                "learning_context": {"enabled": True, "max_items_per_prompt": 3, "max_chars_per_prompt": 260},
            },
            config_id="cfg",
            trading_date="2025-02-10",
            analyst="technical",
            ticker="BU",
            context={"sector": "energy", "market_regime": "trend"},
            horizon_class="short",
        )

        self.assertTrue(context["enabled"])
        self.assertLessEqual(len(context["selected_ids"]), 3)
        self.assertLessEqual(len(context["text"]), 520)
        self.assertEqual(len(db.budgets), 1)
        self.assertGreater(db.budgets[0]["dropped_count"], 0)
        self.assertEqual(db.budgets[0]["digest_count"], len(context["selected_ids"]))
        self.assertEqual(db.budgets[0]["trade_episode_count"], 0)
        self.assertEqual(db.budgets[0]["hypothesis_count"], 0)
        self.assertGreater(db.budgets[0]["total_context_chars"], 0)

    def test_learning_context_cache_reuses_same_day_same_scope_result(self):
        clear_learning_context_cache()
        db = _FakeLearningDB()
        full_config = {
            "learning": {"enabled": True},
            "learning_context": {
                "enabled": True,
                "cache": {"enabled": True},
                "max_items_per_prompt": 3,
                "max_chars_per_prompt": 260,
            },
        }
        first = build_learning_context(
            db=db,
            full_config=full_config,
            config_id="cfg",
            trading_date="2025-02-10",
            analyst="technical",
            ticker="BU",
            context={"sector": "energy", "market_regime": "trend"},
            horizon_class="short",
        )
        second = build_learning_context(
            db=db,
            full_config=full_config,
            config_id="cfg",
            trading_date="2025-02-10",
            analyst="technical",
            ticker="BU",
            context={"sector": "energy", "market_regime": "trend"},
            horizon_class="short",
        )

        self.assertEqual(first["selected_ids"], second["selected_ids"])
        self.assertEqual(db.digest_calls, 1)
        self.assertEqual(len(db.budgets), 1)

    def test_learning_context_falls_back_when_requested_horizon_is_missing(self):
        db = _FallbackLearningDB()
        context = build_learning_context(
            db=db,
            full_config={
                "learning": {"enabled": True},
                "learning_context": {"enabled": True, "max_items_per_prompt": 3, "max_chars_per_prompt": 500},
            },
            config_id="cfg",
            trading_date="2025-02-10",
            analyst="fundamental",
            ticker="BU",
            context={"sector": "energy", "market_regime": "trend"},
            horizon_class="medium",
        )

        self.assertTrue(context["enabled"])
        self.assertIn("digest-short", context["selected_ids"])
        self.assertEqual(context["requested_horizon_class"], "medium")
        self.assertIn("short", context["matched_horizon_classes"])
        self.assertIn("any_horizon", context["retrieval_scopes"])

    def test_learning_context_can_use_same_sector_digest_after_ticker_miss(self):
        db = _SectorFallbackLearningDB()
        context = build_learning_context(
            db=db,
            full_config={
                "learning": {"enabled": True},
                "learning_context": {
                    "enabled": True,
                    "max_items_per_prompt": 3,
                    "max_chars_per_prompt": 500,
                    "allow_cross_ticker_sector_fallback": True,
                },
            },
            config_id="cfg",
            trading_date="2025-02-10",
            analyst="fundamental",
            ticker="RB",
            context={"sector": "ferrous", "market_regime": "trend"},
            horizon_class="medium",
        )

        self.assertEqual(context["selected_ids"], ["sector-digest"])
        self.assertIn("same_sector", ",".join(context["retrieval_scopes"]))
        self.assertIn("Scope boundary", context["text"])
        self.assertIn("broad priors only", context["text"])
        self.assertFalse(context["fallback_authority_boundary"]["can_create_trade_authority"])
        self.assertTrue(context["fallback_authority_boundary"]["same_sector_fallback_prior_only"])
        self.assertTrue(
            context["memory_trace"]["fallback_authority_boundary"]["contains_cross_ticker_fallback"]
        )

    def test_learning_context_includes_exploratory_memory_as_prior_only(self):
        db = _ExploratoryLearningDB()
        context = build_learning_context(
            db=db,
            full_config={
                "learning": {"enabled": True},
                "learning_context": {
                    "enabled": True,
                    "max_items_per_prompt": 3,
                    "max_chars_per_prompt": 500,
                    "exploratory_memory": {
                        "enabled": True,
                        "max_episode_items": 2,
                        "max_episode_chars": 900,
                        "max_hypothesis_items": 2,
                        "max_hypothesis_chars": 900,
                    },
                },
            },
            config_id="cfg",
            trading_date="2025-03-12",
            analyst="technical",
            ticker="BU",
            context={"sector": "energy", "market_regime": "trend"},
            horizon_class="short",
        )

        self.assertIn("Similar completed trade episodes", context["text"])
        self.assertIn("No-trade opportunity memories", context["text"])
        self.assertIn("neutral=technical:watchlist_trigger/medium", context["text"])
        self.assertIn("trigger=breakout confirms with volume", context["text"])
        self.assertIn("Exploratory hypotheses under validation", context["text"])
        self.assertIn("not trading authority", context["text"])
        self.assertIn("cannot size, add, justify position_matched", context["text"])
        self.assertIn("Next-round strategy update", context["text"])
        self.assertIn("position=current confirmation and invalidation required", context["text"])
        self.assertIn("position=no sizing impact without current confirmati", context["text"])
        self.assertIn("position=probe only until validated", context["text"])
        self.assertIn("rebuttable priors", context["text"])
        self.assertIn("confirms or contradicts", context["text"])
        self.assertIn("entry=wait for trend confirmation", context["text"])
        self.assertIn("invalidation=price falls back below breakout level", context["text"])
        self.assertEqual(len(context["trade_episode_items"]), 1)
        self.assertEqual(len(context["no_trade_opportunity_items"]), 1)
        self.assertEqual(len(context["hypothesis_items"]), 1)
        self.assertEqual(context["candidate_hypothesis_count"], 1)
        self.assertEqual(db.budgets[0]["trade_episode_count"], 1)
        self.assertEqual(db.budgets[0]["hypothesis_count"], 1)
        self.assertGreater(db.budgets[0]["total_context_chars"], db.budgets[0]["selected_chars"])

    def test_learning_context_does_not_read_trade_decision_action_values_for_analysts(self):
        db = _ActionValueLearningDB()
        context = build_learning_context(
            db=db,
            full_config={
                "learning": {"enabled": True},
                "learning_context": {
                    "enabled": True,
                    "max_items_per_prompt": 5,
                    "max_chars_per_prompt": 1500,
                    "exploratory_memory": {
                        "enabled": True,
                        "alpha_setup_profile": {
                            "enabled": True,
                            "max_action_value_items": 2,
                            "max_action_value_chars": 800,
                        },
                    },
                },
            },
            config_id="cfg",
            trading_date="2025-03-08",
            analyst="technical",
            ticker="SR",
            context={"sector": "agricultural", "market_regime": "trend"},
            horizon_class="flat",
        )

        self.assertEqual(context["analyst_calibration_items"], [])
        self.assertEqual(context["memory_trace"]["selected_counts"]["alpha_setup_action_value"], 0)
        self.assertNotIn("Alpha setup action-value priors", context["text"])
        self.assertNotIn("Analysts may use only the signal_calibration part", context["text"])
        self.assertNotIn("lane=exit", context["text"])
        self.assertNotIn("signal_calibration_bias=questions_same_side_continuation", context["text"])
        self.assertNotIn("positive_candidate_exit", context["text"])
        self.assertNotIn("reward_mean=", context["text"])
        self.assertNotIn("reward_sum=", context["text"])
        self.assertNotIn("hint=", context["text"])

    def test_learning_context_exposes_product_learning_profile_as_safe_calibration_view(self):
        db = _ProductLearningProfileDB()
        context = build_learning_context(
            db=db,
            full_config={
                "learning": {"enabled": True},
                "learning_context": {
                    "enabled": True,
                    "max_items_per_prompt": 5,
                    "max_chars_per_prompt": 1800,
                    "exploratory_memory": {
                        "enabled": True,
                        "alpha_setup_profile": {
                            "enabled": True,
                            "max_items": 2,
                            "max_chars": 900,
                        },
                    },
                },
            },
            config_id="cfg",
            trading_date="2025-03-21",
            analyst="technical",
            ticker="EB",
            context={"sector": "chemical", "market_regime": "range"},
            horizon_class="short",
        )

        self.assertEqual(len(context["alpha_setup_items"]), 1)
        view = context["alpha_setup_items"][0]["product_learning_calibration_view"]
        self.assertEqual(view["performance_scope_key"], (
            "EB|short|trend_breakout_setup|opening_range_breakdown|"
            "technical:used|fundamental:used|news:fresh|capital_deployed"
        ))
        self.assertEqual(view["deployment_tier"], "capital_deployed")
        self.assertEqual(view["historical_pm_rank"], 1)
        self.assertEqual(view["historical_pm_score"], 0.83)
        self.assertTrue(view["not_trade_authority"])
        self.assertIn("Product learning:", context["text"])
        self.assertIn("historical_pm_rank=1", context["text"])
        self.assertIn("historical_pm_score=0.83", context["text"])
        self.assertNotIn("authority_type", context["text"])
        self.assertNotIn("target_lots", context["text"])
        self.assertNotIn("lots_delta", context["text"])
        self.assertNotIn("final_action_contract", context["text"])
        self.assertEqual(
            context["memory_trace"]["selected_memory_refs"][0]["product_learning_calibration_view"],
            view,
        )

    def test_config_overlay_uses_allowlist(self):
        config = apply_config_learning_overlay(
            {"learning": {"enabled": True, "config_overlay": {"enabled": True}}, "llm": {"model": "keep"}},
            db=_FakeOverlayDB(),
            config_id="cfg",
            trading_date="2025-02-10",
        )

        self.assertEqual(config["capital_utilization_control"]["target_margin_ratio_min"], 0.16)
        self.assertEqual(config["llm"]["model"], "keep")
        self.assertEqual(len(config["runtime_learning_overlay"]["applied"]), 1)
        self.assertEqual(len(config["runtime_learning_overlay"]["skipped"]), 1)

    def test_reviewer_writes_trade_episode_memory_from_closed_trade(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
        cursor.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                reference_portfolio_id TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                effective_trade_date TEXT NOT NULL,
                source_type TEXT NOT NULL,
                underlying_code TEXT NOT NULL,
                from_contract TEXT,
                to_contract TEXT,
                contract_code TEXT,
                action TEXT NOT NULL,
                lots INTEGER NOT NULL,
                base_price REAL,
                base_price_source TEXT,
                base_price_date TEXT,
                open_price REAL,
                prev_close_price REAL,
                slippage_model TEXT,
                slippage_ticks INTEGER,
                slippage_amount REAL,
                execution_price REAL,
                justification TEXT,
                signal_snapshot TEXT,
                signal_snapshot_artifact_path TEXT,
                signal_snapshot_sha256 TEXT,
                audit_payload TEXT,
                audit_payload_artifact_path TEXT,
                audit_payload_sha256 TEXT,
                warning_message TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE futures_transactions (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL,
                config_id TEXT,
                recommendation_id TEXT,
                trading_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                contract_code TEXT,
                action TEXT NOT NULL,
                lots INTEGER NOT NULL,
                price REAL,
                execution_price REAL NOT NULL,
                settle_price REAL,
                contract_multiplier REAL NOT NULL,
                margin_rate REAL NOT NULL,
                margin_used REAL NOT NULL,
                daily_pnl REAL DEFAULT 0,
                commission REAL DEFAULT 0,
                source_type TEXT,
                execution_phase TEXT,
                audit_payload TEXT,
                warning_message TEXT,
                booked_in_settlement BOOLEAN DEFAULT 0,
                justification TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_reviewer_learning_schema(cursor)
        snapshot = {
            "technical": {
                "signal": "Bullish",
                "confidence": 0.72,
                "setup_type": "breakout",
                "horizon_class": "short",
                "opportunity_state": "watch_for_trigger",
                "opportunity_type": "unknown",
            },
            "fundamental": {"signal": "Neutral", "confidence": 0.40, "horizon_class": "medium"},
            "commodity_news": {"signal": "Bullish", "confidence": 0.61, "horizon_class": "event_short"},
            "horizon_scope": {"decision_horizon": "short"},
            "opportunity_scorecard": {
                "preferred_side": "long",
                "long": {
                    "final_state": "tradeable_candidate",
                    "dominant_opportunity_type": "trend_continuation",
                    "max_setup_quality": 0.72,
                },
            },
            "pm_research_contract_summary": {
                "contract_version": "agentquant.research.v1",
                "dominant_opportunity_types": ["trend_continuation"],
                "opportunity_states": ["tradeable_candidate"],
                "opportunity_states": ["probe_candidate"],
                "factor_focus": ["trend"],
                "current_evidence_conflict": [],
            },
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "BU",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": 1,
                "lots_delta": 1,
                "target_position_ratio": 0.08,
                "horizon_class": "short",
                "market_regime": "trend",
            },
            "market_confirmation": {"confirmation_score": 0.74},
        }
        cursor.execute(
            """
            INSERT INTO futures_recommendation (
                id, config_id, reference_portfolio_id, trading_date, effective_trade_date,
                source_type, underlying_code, contract_code, action, lots, execution_price,
                justification, signal_snapshot, status, created_at
            ) VALUES (?, 'cfg', 'pf', '2025-03-10', '2025-03-10',
                'strategy', 'BU', 'bu2506', 'open_long', 1, 3200,
                'open long', ?, 'pending', '2025-03-10T09:00:00')
            """,
            ("rec-open", json.dumps(snapshot, ensure_ascii=False)),
        )
        cursor.executemany(
            """
            INSERT INTO futures_transactions (
                id, portfolio_id, config_id, recommendation_id, trading_date, ticker,
                contract_code, action, lots, price, execution_price, settle_price,
                contract_multiplier, margin_rate, margin_used, daily_pnl,
                commission, source_type, execution_phase, created_at
            ) VALUES (?, 'pf', 'cfg', ?, ?, 'BU', 'bu2506', ?, 1, ?, ?, ?,
                10, 0.1, 3200, 0, 1, 'strategy', 'phase2', ?)
            """,
            [
                ("tx-open", "rec-open", "2025-03-10", "open_long", 3200.0, 3200.0, 3200.0, "2025-03-10T09:30:00"),
                ("tx-close", "rec-close", "2025-03-12", "close_long", 3340.0, 3340.0, 3340.0, "2025-03-12T14:30:00"),
            ],
        )

        rows = _write_trade_episode_memory(
            cursor,
            cfg={"learning": {"trade_episode_memory": {"enabled": True}}},
            config_id="cfg",
            trading_date="2025-03-12",
        )

        self.assertEqual(rows, 1)
        cursor.execute("SELECT * FROM trade_episode_memory WHERE config_id='cfg'")
        item = dict(cursor.fetchone())
        self.assertEqual(item["ticker"], "BU")
        self.assertEqual(item["side"], "long")
        self.assertEqual(item["trading_date"], "2025-03-12")
        self.assertEqual(item["episode_date"], "2025-03-12")
        self.assertTrue(item["first_seen_at"])
        self.assertTrue(item["last_reviewed_at"])
        self.assertEqual(item["outcome_label"], "winner")
        self.assertIn("BU long winner", item["lesson_text"])
        payload = load_externalized_json(item["payload_json"])
        contract = payload[CONTRACT_KEY]
        self.assertEqual(contract["memory_type"], "trade_episode_memory")
        self.assertEqual(contract["contract_version"], "next_round_strategy_update_v2")
        self.assertEqual(contract["scope_priority"], "ticker_side_template")
        self.assertIn("analyst_action_items", contract)
        self.assertIn("pm_action_conditions", contract)
        self.assertIn("invalidates_when", contract)
        self.assertEqual(contract["position_authority"], "analysis_or_watchlist_only")
        self.assertEqual(contract["max_position_impact"], "no_direct_position_impact")
        self.assertIn("position_impact_conditions", contract)
        self.assertIn("current-day data", " ".join(contract["usage_boundary"]))
        self.assertEqual(payload["opportunity_state"], "tradeable_candidate")
        self.assertEqual(payload["opportunity_type"], "trend_continuation")
        self.assertIn("opportunity_state=tradeable_candidate", " ".join(contract["usable_memory"]))
        event = cursor.execute(
            "SELECT action_json FROM learning_event_log WHERE event_type='trade_episode_memory'"
        ).fetchone()
        self.assertIn(CONTRACT_KEY, json.loads(event["action_json"]))
        conn.close()

    def test_trade_episode_memory_keeps_original_review_date_on_refresh(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
        cursor.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                reference_portfolio_id TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                effective_trade_date TEXT NOT NULL,
                source_type TEXT NOT NULL,
                underlying_code TEXT NOT NULL,
                contract_code TEXT,
                action TEXT NOT NULL,
                lots INTEGER NOT NULL,
                execution_price REAL,
                justification TEXT,
                signal_snapshot TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE futures_transactions (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL,
                config_id TEXT,
                recommendation_id TEXT,
                trading_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                contract_code TEXT,
                action TEXT NOT NULL,
                lots INTEGER NOT NULL,
                price REAL,
                execution_price REAL NOT NULL,
                settle_price REAL,
                contract_multiplier REAL NOT NULL,
                margin_rate REAL NOT NULL,
                margin_used REAL NOT NULL,
                daily_pnl REAL DEFAULT 0,
                commission REAL DEFAULT 0,
                source_type TEXT,
                execution_phase TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_reviewer_learning_schema(cursor)
        cursor.execute(
            """
            INSERT INTO futures_recommendation (
                id, config_id, reference_portfolio_id, trading_date, effective_trade_date,
                source_type, underlying_code, contract_code, action, lots, execution_price,
                justification, signal_snapshot, status, created_at
            ) VALUES ('rec-open', 'cfg', 'pf', '2025-03-10', '2025-03-10',
                'strategy', 'BU', 'bu2506', 'open_long', 1, 3200,
                'open long', '{"pm_internal_draft":{"market_regime":"trend"}}', 'pending',
                '2025-03-10T09:00:00')
            """
        )
        cursor.executemany(
            """
            INSERT INTO futures_transactions (
                id, portfolio_id, config_id, recommendation_id, trading_date, ticker,
                contract_code, action, lots, price, execution_price, settle_price,
                contract_multiplier, margin_rate, margin_used, daily_pnl,
                commission, source_type, execution_phase, created_at
            ) VALUES (?, 'pf', 'cfg', ?, ?, 'BU', 'bu2506', ?, 1, ?, ?, ?,
                10, 0.1, 3200, 0, 1, 'strategy', 'phase2', ?)
            """,
            [
                ("tx-open", "rec-open", "2025-03-10", "open_long", 3200.0, 3200.0, 3200.0, "2025-03-10T09:30:00"),
                ("tx-close", "rec-close", "2025-03-12", "close_long", 3340.0, 3340.0, 3340.0, "2025-03-12T14:30:00"),
            ],
        )

        _write_trade_episode_memory(
            cursor,
            cfg={"learning": {"trade_episode_memory": {"enabled": True}}},
            config_id="cfg",
            trading_date="2025-03-12",
        )
        cursor.execute("SELECT trading_date, first_seen_at FROM trade_episode_memory WHERE config_id='cfg'")
        first = dict(cursor.fetchone())
        _write_trade_episode_memory(
            cursor,
            cfg={"learning": {"trade_episode_memory": {"enabled": True}}},
            config_id="cfg",
            trading_date="2025-03-20",
        )
        cursor.execute(
            "SELECT trading_date, episode_date, first_seen_at, last_reviewed_at FROM trade_episode_memory WHERE config_id='cfg'"
        )
        refreshed = dict(cursor.fetchone())
        self.assertEqual(refreshed["trading_date"], "2025-03-12")
        self.assertEqual(refreshed["episode_date"], "2025-03-12")
        self.assertEqual(refreshed["first_seen_at"], first["first_seen_at"])
        self.assertTrue(refreshed["last_reviewed_at"])
        conn.close()

    def test_loss_template_observation_is_candidate_memory_only(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
        cursor.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                reference_portfolio_id TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                effective_trade_date TEXT NOT NULL,
                source_type TEXT NOT NULL,
                underlying_code TEXT NOT NULL,
                from_contract TEXT,
                to_contract TEXT,
                contract_code TEXT,
                action TEXT NOT NULL,
                lots INTEGER NOT NULL,
                base_price REAL,
                base_price_source TEXT,
                base_price_date TEXT,
                open_price REAL,
                prev_close_price REAL,
                slippage_model TEXT,
                slippage_ticks INTEGER,
                slippage_amount REAL,
                execution_price REAL,
                justification TEXT,
                signal_snapshot TEXT,
                signal_snapshot_artifact_path TEXT,
                signal_snapshot_sha256 TEXT,
                audit_payload TEXT,
                audit_payload_artifact_path TEXT,
                audit_payload_sha256 TEXT,
                warning_message TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE futures_transactions (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL,
                config_id TEXT,
                recommendation_id TEXT,
                trading_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                contract_code TEXT,
                action TEXT NOT NULL,
                lots INTEGER NOT NULL,
                price REAL,
                execution_price REAL NOT NULL,
                settle_price REAL,
                contract_multiplier REAL NOT NULL,
                margin_rate REAL NOT NULL,
                margin_used REAL NOT NULL,
                daily_pnl REAL DEFAULT 0,
                commission REAL DEFAULT 0,
                source_type TEXT,
                execution_phase TEXT,
                audit_payload TEXT,
                warning_message TEXT,
                booked_in_settlement BOOLEAN DEFAULT 0,
                justification TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_reviewer_learning_schema(cursor)
        snapshot = {
            "technical": {
                "signal": "Bearish",
                "confidence": 0.62,
                "setup_type": "breakdown",
                "horizon_class": "short",
                "metadata": {
                    "data_usage_summary": {
                        "pandaai_daily": {"available": True, "used_in_signal": True}
                    }
                },
            },
            "fundamental": {"signal": "Neutral", "confidence": 0.45, "horizon_class": "medium"},
            "commodity_news": {"signal": "Bearish", "confidence": 0.58, "horizon_class": "event_short"},
            "pm_internal_draft": {"market_regime": "range"},
        }
        cursor.execute(
            """
            INSERT INTO futures_recommendation (
                id, config_id, reference_portfolio_id, trading_date, effective_trade_date,
                source_type, underlying_code, contract_code, action, lots, execution_price,
                justification, signal_snapshot, status, created_at
            ) VALUES (?, 'cfg', 'pf', '2025-01-06', '2025-01-06',
                'strategy', 'TA', 'ta2505', 'open_short', 1, 5800,
                'open short', ?, 'pending', '2025-01-06T09:00:00')
            """,
            ("rec-ta-open", json.dumps(snapshot, ensure_ascii=False)),
        )
        cursor.executemany(
            """
            INSERT INTO futures_transactions (
                id, portfolio_id, config_id, recommendation_id, trading_date, ticker,
                contract_code, action, lots, price, execution_price, settle_price,
                contract_multiplier, margin_rate, margin_used, daily_pnl,
                commission, source_type, execution_phase, created_at
            ) VALUES (?, 'pf', 'cfg', ?, ?, 'TA', 'ta2505', ?, 1, ?, ?, ?,
                5, 0.1, 2900, 0, 1, 'strategy', 'phase2', ?)
            """,
            [
                ("tx-ta-open", "rec-ta-open", "2025-01-06", "open_short", 5800.0, 5800.0, 5800.0, "2025-01-06T09:30:00"),
                ("tx-ta-close", "rec-ta-close", "2025-01-08", "close_short", 5900.0, 5900.0, 5900.0, "2025-01-08T14:30:00"),
            ],
        )

        rows = _write_loss_template_observation_research(
            cursor,
            cfg={
                "learning": {
                    "loss_template_observation": {
                        "enabled": True,
                        "lookback_days": 30,
                        "min_loss_samples": 1,
                        "min_cumulative_loss_abs": 1,
                        "max_rows_per_day": 2,
                    }
                }
            },
            config_id="cfg",
            trading_date="2025-01-08",
        )

        self.assertEqual(rows, 1)
        item = cursor.execute("SELECT * FROM exploratory_hypothesis WHERE config_id='cfg'").fetchone()
        self.assertEqual(item["ticker"], "TA")
        self.assertEqual(item["status"], "candidate")
        payload = load_externalized_json(
            item["payload_json"],
            item["payload_artifact_path"],
            item["payload_sha256"],
        )
        contract = payload[CONTRACT_KEY]
        self.assertEqual(contract["memory_type"], "loss_template_observation")
        self.assertEqual(contract["position_authority"], "analysis_or_watchlist_only")
        self.assertEqual(contract["max_position_impact"], "no_direct_position_impact")
        self.assertTrue(payload["observation_only"])
        event = cursor.execute(
            "SELECT action_json FROM learning_event_log WHERE event_type='loss_template_observation'"
        ).fetchone()
        self.assertIn(CONTRACT_KEY, json.loads(event["action_json"]))
        policy_rows = cursor.execute("SELECT COUNT(*) FROM adaptive_policy_state").fetchone()[0]
        self.assertEqual(policy_rows, 0)
        conn.close()

    def test_repeated_loss_template_promotes_bounded_policy_state(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
        cursor.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT)")
        cursor.execute("INSERT INTO portfolio VALUES ('pf', 'cfg')")
        cursor.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT PRIMARY KEY,
                config_id TEXT,
                reference_portfolio_id TEXT,
                trading_date TEXT,
                effective_trade_date TEXT,
                source_type TEXT,
                underlying_code TEXT,
                contract_code TEXT,
                action TEXT,
                lots INTEGER,
                execution_price REAL,
                justification TEXT,
                signal_snapshot TEXT,
                signal_snapshot_artifact_path TEXT,
                signal_snapshot_sha256 TEXT,
                status TEXT,
                created_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE futures_transactions (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT,
                config_id TEXT,
                recommendation_id TEXT,
                trading_date TEXT,
                ticker TEXT,
                contract_code TEXT,
                action TEXT,
                lots INTEGER,
                price REAL,
                execution_price REAL,
                settle_price REAL,
                contract_multiplier REAL,
                margin_rate REAL,
                margin_used REAL,
                daily_pnl REAL,
                commission REAL,
                source_type TEXT,
                execution_phase TEXT,
                audit_payload TEXT,
                warning_message TEXT,
                booked_in_settlement BOOLEAN DEFAULT 0,
                justification TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_reviewer_learning_schema(cursor)
        snapshot = {
            "technical": {"signal": "Bullish", "setup_type": "breakout", "horizon_class": "short"},
            "fundamental": {"signal": "Neutral", "horizon_class": "medium"},
            "commodity_news": {"signal": "Neutral", "horizon_class": "event_short"},
            "pm_internal_draft": {
                "analyst_signal_combo": ["Bullish", "Neutral", "Neutral"],
                "decision_horizon": "short",
                "market_regime": "trend",
            },
        }
        for idx, day in enumerate(["2025-01-06", "2025-01-07", "2025-01-08"], start=1):
            rec_id = f"rec-bu-open-{idx}"
            close_rec_id = f"rec-bu-close-{idx}"
            cursor.execute(
                """
                INSERT INTO futures_recommendation (
                    id, config_id, reference_portfolio_id, trading_date, effective_trade_date,
                    source_type, underlying_code, contract_code, action, lots, execution_price,
                    justification, signal_snapshot, signal_snapshot_artifact_path, signal_snapshot_sha256,
                    status, created_at
                ) VALUES (?, 'cfg', 'pf', ?, ?, 'strategy', 'BU', 'bu2505',
                    'open_long', 1, 3500, 'open long', ?, NULL, NULL, 'pending', ?)
                """,
                (rec_id, day, day, json.dumps(snapshot, ensure_ascii=False), f"{day}T09:00:00"),
            )
            cursor.executemany(
                """
                INSERT INTO futures_transactions (
                    id, portfolio_id, config_id, recommendation_id, trading_date, ticker,
                    contract_code, action, lots, price, execution_price, settle_price,
                    contract_multiplier, margin_rate, margin_used, daily_pnl,
                    commission, source_type, execution_phase, created_at
                ) VALUES (?, 'pf', 'cfg', ?, ?, 'BU', 'bu2505', ?, 1, ?, ?, ?,
                    10, 0.1, 3500, 0, 1, 'strategy', 'phase2', ?)
                """,
                [
                    (f"tx-bu-open-{idx}", rec_id, day, "open_long", 3500.0, 3500.0, 3500.0, f"{day}T09:30:00"),
                    (f"tx-bu-close-{idx}", close_rec_id, day, "close_long", 3420.0, 3420.0, 3420.0, f"{day}T14:30:00"),
                ],
            )

        rows = _write_loss_template_observation_research(
            cursor,
            cfg={
                "learning": {
                    "loss_template_observation": {
                        "enabled": True,
                        "lookback_days": 30,
                        "min_loss_samples": 1,
                        "min_cumulative_loss_abs": 1,
                        "max_rows_per_day": 2,
                        "policy_promotion": {
                            "enabled": True,
                            "min_loss_samples": 3,
                            "min_cumulative_loss_abs": 2000,
                            "cap_multiplier": 0.35,
                            "valid_days": 10,
                        },
                    }
                }
            },
            config_id="cfg",
            trading_date="2025-01-08",
        )

        self.assertEqual(rows, 1)
        policy = cursor.execute(
            "SELECT * FROM adaptive_policy_state WHERE policy_type='loss_template_policy'"
        ).fetchone()
        self.assertIsNotNone(policy)
        self.assertEqual(policy["policy_action"], "cap")
        self.assertEqual(policy["ticker"], "BU")
        self.assertEqual(policy["side"], "long")
        self.assertEqual(policy["sample_count"], 3)
        payload = load_externalized_json(policy["payload_json"])
        contract = payload[CONTRACT_KEY]
        self.assertEqual(contract["position_authority"], "risk_reduction_conditioned")
        self.assertEqual(contract["max_position_impact"], "may_reduce_or_cap_only_through_pm_auditor")
        self.assertFalse(contract["trigger_valid"])
        self.assertEqual(contract["opportunity_state"], "watch_for_trigger")
        event = cursor.execute(
            "SELECT action_json FROM learning_event_log WHERE event_type='loss_template_policy'"
        ).fetchone()
        self.assertIn(CONTRACT_KEY, json.loads(event["action_json"]))
        conn.close()

    def test_loss_template_policy_guard_blocks_too_short_observation_window(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
        cursor.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT)")
        cursor.execute("INSERT INTO portfolio VALUES ('pf', 'cfg')")
        cursor.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT PRIMARY KEY,
                config_id TEXT,
                reference_portfolio_id TEXT,
                trading_date TEXT,
                effective_trade_date TEXT,
                source_type TEXT,
                underlying_code TEXT,
                contract_code TEXT,
                action TEXT,
                lots INTEGER,
                execution_price REAL,
                justification TEXT,
                signal_snapshot TEXT,
                signal_snapshot_artifact_path TEXT,
                signal_snapshot_sha256 TEXT,
                status TEXT,
                created_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE futures_transactions (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT,
                config_id TEXT,
                recommendation_id TEXT,
                trading_date TEXT,
                ticker TEXT,
                contract_code TEXT,
                action TEXT,
                lots INTEGER,
                price REAL,
                execution_price REAL,
                settle_price REAL,
                contract_multiplier REAL,
                margin_rate REAL,
                margin_used REAL,
                daily_pnl REAL,
                commission REAL,
                source_type TEXT,
                execution_phase TEXT,
                audit_payload TEXT,
                warning_message TEXT,
                booked_in_settlement BOOLEAN DEFAULT 0,
                justification TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_reviewer_learning_schema(cursor)
        snapshot = {
            "technical": {"signal": "Bullish", "setup_type": "breakout", "horizon_class": "short"},
            "fundamental": {"signal": "Neutral", "horizon_class": "medium"},
            "commodity_news": {"signal": "Neutral", "horizon_class": "event_short"},
            "pm_internal_draft": {
                "analyst_signal_combo": ["Bullish", "Neutral", "Neutral"],
                "decision_horizon": "short",
                "market_regime": "trend",
            },
        }
        for idx, day in enumerate(["2025-01-06", "2025-01-07", "2025-01-08"], start=1):
            rec_id = f"guard-bu-open-{idx}"
            cursor.execute(
                """
                INSERT INTO futures_recommendation (
                    id, config_id, reference_portfolio_id, trading_date, effective_trade_date,
                    source_type, underlying_code, contract_code, action, lots, execution_price,
                    justification, signal_snapshot, signal_snapshot_artifact_path, signal_snapshot_sha256,
                    status, created_at
                ) VALUES (?, 'cfg', 'pf', ?, ?, 'strategy', 'BU', 'bu2505',
                    'open_long', 1, 3500, 'open long', ?, NULL, NULL, 'pending', ?)
                """,
                (rec_id, day, day, json.dumps(snapshot, ensure_ascii=False), f"{day}T09:00:00"),
            )
            cursor.executemany(
                """
                INSERT INTO futures_transactions (
                    id, portfolio_id, config_id, recommendation_id, trading_date, ticker,
                    contract_code, action, lots, price, execution_price, settle_price,
                    contract_multiplier, margin_rate, margin_used, daily_pnl,
                    commission, source_type, execution_phase, created_at
                ) VALUES (?, 'pf', 'cfg', ?, ?, 'BU', 'bu2505', ?, 1, ?, ?, ?,
                    10, 0.1, 3500, 0, 1, 'strategy', 'phase2', ?)
                """,
                [
                    (f"guard-bu-open-tx-{idx}", rec_id, day, "open_long", 3500.0, 3500.0, 3500.0, f"{day}T09:30:00"),
                    (f"guard-bu-close-tx-{idx}", f"guard-close-{idx}", day, "close_long", 3420.0, 3420.0, 3420.0, f"{day}T14:30:00"),
                ],
            )

        rows = _write_loss_template_observation_research(
            cursor,
            cfg={
                "learning": {
                    "policy_promotion_guard": {
                        "enabled": True,
                        "min_distinct_trade_days_for_cap": 3,
                        "min_calendar_span_days_for_cap": 5,
                        "max_single_trade_pnl_share": 0.90,
                    },
                    "loss_template_observation": {
                        "enabled": True,
                        "lookback_days": 30,
                        "min_loss_samples": 1,
                        "min_cumulative_loss_abs": 1,
                        "max_rows_per_day": 2,
                        "policy_promotion": {
                            "enabled": True,
                            "min_loss_samples": 3,
                            "min_cumulative_loss_abs": 2000,
                            "cap_multiplier": 0.35,
                            "valid_days": 10,
                        },
                    },
                }
            },
            config_id="cfg",
            trading_date="2025-01-08",
        )

        self.assertEqual(rows, 1)
        policy_rows = cursor.execute("SELECT COUNT(*) FROM adaptive_policy_state WHERE policy_type='loss_template_policy'").fetchone()[0]
        self.assertEqual(policy_rows, 0)
        guard_event = cursor.execute(
            "SELECT action_json FROM learning_event_log WHERE event_type='loss_template_policy_guard'"
        ).fetchone()
        self.assertIsNotNone(guard_event)
        self.assertIn("keep_candidate_observation", guard_event["action_json"])
        conn.close()

    def test_reviewer_exploratory_hypotheses_are_candidate_priors(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
        _ensure_reviewer_learning_schema(cursor)
        cursor.execute(
            """
            INSERT INTO trade_episode_memory (
                id, config_id, trading_date, ticker, side, sector, setup_type,
                signal_combo, horizon_class, market_regime, open_date, close_date,
                holding_days, net_pnl, return_on_notional, outcome_label,
                lesson_text, payload_json, created_at
            ) VALUES
                ('e1', 'cfg', '2025-03-11', 'BU', 'long', 'energy', 'long_breakout_short',
                 '["Bullish","Neutral","Bullish"]', 'short', 'trend', '2025-03-10',
                 '2025-03-11', 1, 1200, 0.02, 'winner', 'BU breakout worked', '{}', 'now'),
                ('e2', 'cfg', '2025-03-12', 'BU', 'long', 'energy', 'long_breakout_short',
                 '["Bullish","Neutral","Bullish"]', 'short', 'trend', '2025-03-11',
                 '2025-03-12', 1, -400, -0.01, 'loser', 'late breakout failed', '{}', 'now')
            """
        )

        def fake_agent_call(**kwargs):
            return ExploratoryHypothesisLLMOutput(
                hypotheses=[
                    ExploratoryHypothesisItem(
                        ticker="BU",
                        sector="energy",
                        side="long",
                        horizon_class="short",
                        market_regime="trend",
                        hypothesis_text="BU long breakouts require current confirmation and explicit invalidation.",
                        evidence_summary="two recent BU episodes",
                        suggested_use="probe_candidate",
                        entry_timing_hint="wait for price confirmation",
                        exit_timing_hint="exit if confirmation disappears",
                        holding_period_hint="short",
                        invalidation_condition="breakout fails before close",
                        validation_plan="validate with future BU trend samples",
                        confidence_score=0.52,
                    )
                ]
            )

        with patch("llm.inference.agent_call", side_effect=fake_agent_call):
            summary = write_exploratory_hypotheses(
                cursor,
                cfg={
                    "llm": {"model": "unit-test"},
                    "max_total_margin_ratio": 0.20,
                    "learning": {
                        "memory_expires_after_days": 30,
                        "exploratory_research": {
                            "enabled": True,
                            "use_llm": True,
                            "min_episode_samples": 2,
                            "max_episode_samples": 4,
                            "max_hypotheses_per_day": 3,
                        },
                    },
                },
                config_id="cfg",
                trading_date="2025-03-12",
            )

        self.assertEqual(summary["rows"], 1)
        cursor.execute("SELECT * FROM exploratory_hypothesis WHERE config_id='cfg'")
        item = dict(cursor.fetchone())
        self.assertEqual(item["status"], "candidate")
        self.assertIn("structured research hypothesis", item["suggested_use"])
        self.assertIn("explicit invalidation", item["hypothesis_text"])
        payload = load_externalized_json(item["payload_json"], item["payload_artifact_path"], item["payload_sha256"])
        self.assertTrue(payload["hard_constraints"]["structured_hypothesis_only"])
        self.assertTrue(payload["hard_constraints"]["candidate_hypothesis_cannot_control_position"])
        self.assertAlmostEqual(payload["hard_constraints"]["max_total_margin_ratio"], 0.20)
        contract = payload[CONTRACT_KEY]
        self.assertEqual(contract["contract_version"], "next_round_strategy_update_v2")
        self.assertEqual(contract["position_authority"], "analysis_or_watchlist_only")
        self.assertEqual(contract["max_position_impact"], "no_direct_position_impact")
        self.assertIn("pm_action_conditions", contract)
        self.assertEqual(payload["entry_timing_hint"], "wait for price confirmation")
        self.assertEqual(payload["agent_name"], "researcher")
        cursor.execute(
            """
            SELECT raw_prompt, raw_prompt_artifact_path, raw_prompt_sha256
            FROM researcher_llm_notes
            WHERE config_id='cfg'
            """
        )
        note = dict(cursor.fetchone())
        raw_prompt = load_externalized_text(
            note["raw_prompt"],
            note["raw_prompt_artifact_path"],
            note["raw_prompt_sha256"],
        )
        self.assertIn("AgentQuant Researcher", raw_prompt)
        self.assertNotIn("AgentQuant Reviewer acting as a research memory curator", raw_prompt)
        self.assertEqual(payload["invalidation_condition"], "breakout fails before close")
        conn.close()

    def test_researcher_causal_review_prompt_requires_trade_contract(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_reviewer_learning_schema(conn.cursor())
        cursor = conn.cursor()

        captured = {}

        def fake_agent_call(*, prompt, llm_config, pydantic_model):
            captured["prompt"] = prompt
            self.assertIs(pydantic_model, CausalReviewLLMOutput)
            return CausalReviewLLMOutput(
                primary_cause="entry timing failed",
                entry_error=True,
                setup_type="trend_breakout_setup",
                future_use_scope="BU:long:short:trend_breakout_setup",
                next_analyst_checks=["confirm breakout hold above trigger"],
                pm_action_hint="probe",
                position_effect_limit="probe_only_until_validated",
                invalid_if=["breakout fails before close"],
                promotion_or_demotion_rule="promote only after same-scope positive samples",
                expected_trade_behavior_change="avoid full-size opens without trigger confirmation",
                confidence_score=0.72,
            )

        with patch("llm.inference.agent_call", side_effect=fake_agent_call):
            rows = run_researcher_causal_review(
                cursor,
                cfg={
                    "llm": {"model": "unit-test"},
                    "learning": {
                        "researcher_causal_review": {
                            "enabled": True,
                            "use_llm": True,
                        }
                    },
                },
                config_id="cfg",
                trading_date="2025-03-13",
                settlement_row={"daily_pnl": -1200, "commission": 25, "margin_ratio": 0.04},
                strategy_recommendations=[
                    {
                        "id": "rec-1",
                        "underlying_code": "BU",
                        "action": "open_long",
                        "lots": 1,
                        "signal_snapshot": json.dumps(
                            {
                                "pm_internal_draft": {
                                    "target_position_ratio": 0.04,
                                    "market_confirmation": {"confirmation_score": 0.58},
                                }
                            }
                        ),
                    }
                ],
                no_trade_reason_counter=Counter({"filled": 1}),
            )

        self.assertEqual(rows, 1)
        prompt = captured["prompt"]
        self.assertIn("future strategy-update contract", prompt)
        self.assertIn("pm_action_hint", prompt)
        self.assertIn("position_effect_limit", prompt)
        self.assertIn("Candidate memories cannot authorize sizing", prompt)
        cursor.execute("SELECT payload_json FROM causal_review_candidate WHERE config_id='cfg'")
        payload = json.loads(cursor.fetchone()["payload_json"])
        self.assertEqual(payload["agent_name"], "researcher")
        self.assertEqual(payload["setup_type"], "trend_breakout_setup")
        self.assertEqual(payload["pm_action_hint"], "probe")
        self.assertEqual(payload["position_effect_limit"], "probe_only_until_validated")
        self.assertEqual(payload["expected_trade_behavior_change"], "avoid full-size opens without trigger confirmation")
        conn.close()


class AdaptivePolicyAuditorTest(unittest.TestCase):
    def test_auditor_ignores_reviewer_adaptive_cap(self):
        auditor = PMRiskGate(
            {
                "pm_risk_gate": {
                    "enabled": True,
                    "policy_version": "test",
                    "quality_gate": {"enabled": False},
                    "attribution_feedback": {"enabled": False},
                },
                "market_confirmation": {"enabled": False},
                "learning": {"adaptive_policy": {"min_policy_confidence": 0.30}},
            }
        )
        output = auditor.plan(
            PMRiskGateInput(
                ticker="BU",
                raw_position_ratio=0.10,
                current_position_ratio=0.0,
                market_confirmation={"confirmation_score": 0.8},
                adaptive_policy_state=[
                    {
                        "policy_action": "cap",
                        "multiplier": 0.5,
                        "confidence_score": 0.6,
                        "reason": "weak mature template",
                    }
                ],
                full_config={"learning": {"adaptive_policy": {"min_policy_confidence": 0.30}}},
            )
        )

        self.assertEqual(output.decision, "allow")
        self.assertAlmostEqual(output.position_ratio_multiplier, 1.0)
        self.assertNotIn("adaptive_policy_cap", output.reasons)
        self.assertEqual(
            output.diagnostics.get("research_memory_boundary"),
            "RiskGate_does_not_consume_research_records",
        )

    def test_learning_overlay_cannot_raise_portfolio_hard_margin_cap(self):
        self.assertAlmostEqual(
            get_hard_allocation_margin_ratio(
                {
                    "max_total_margin_ratio": 0.20,
                    "capital_utilization_control": {"max_margin_ratio_after_scaling": 0.35},
                }
            ),
            0.20,
        )
        self.assertAlmostEqual(
            get_hard_allocation_margin_ratio(
                {
                    "max_total_margin_ratio": 0.20,
                    "capital_utilization_control": {"max_margin_ratio_after_scaling": 0.12},
                }
            ),
            0.12,
        )
        self.assertAlmostEqual(
            get_hard_allocation_margin_ratio(
                {
                    "max_total_margin_ratio": 0.20,
                    "capital_utilization_control": {"max_margin_ratio_after_scaling": "bad"},
                }
            ),
            0.20,
        )

    def test_capital_utilization_scales_protected_memory_template(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-10",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.02,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.50},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.16,
                    "target_margin_ratio_max": 0.20,
                    "target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "memory_protected_min_confirmation_score": 0.45,
                    "disable_scaling_when_weak_combo": True,
                    "protected_min_sample_count_for_scaling": 4,
                },
                "trade_frequency_control": {
                    "weak_signal_combos": [["Bullish", "Bullish", "Neutral"]],
                },
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={
                "combo": {
                    "memory_state": "protected",
                    "signal_combo": "Bullish|Bullish|Neutral",
                    "sample_count": 4,
                    "win_rate": 0.75,
                    "net_pnl": 3200.0,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[
                SimpleNamespace(invalidation_level=3200.0, atr_stop_distance=None),
            ],
        )

        self.assertGreater(ratio * 0.10, 0.08)
        self.assertLess(ratio * 0.10, 0.18)
        self.assertIn("capital_utilization_guard", reasons)
        self.assertIn("capital_utilization_memory_protected", reasons)
        self.assertIn("capital_utilization_learning", diagnostics)

    def test_repeatedly_validated_alpha_gets_more_budget_without_fixed_single_name_cap(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-10",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.03,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.82},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "memory_protected_min_confirmation_score": 0.45,
                    "dynamic_concentration_enabled": True,
                    "other_opportunity_reserve_fraction_of_tradable_capital": 0.10,
                    "validated_min_fraction_of_remaining_capacity": 0.35,
                    "validated_max_fraction_of_remaining_capacity": 0.90,
                    "confirmation_allocation_power": 1.25,
                    "allow_memory_protected_scaling": True,
                }
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={
                "side_memory": {
                    "memory_state": "protected",
                    "signal_combo": "Bullish|Bullish|Neutral",
                    "sample_count": 5,
                    "win_rate": 0.8,
                    "net_pnl": 6000,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[
                SimpleNamespace(invalidation_level=3200.0, atr_stop_distance=None),
            ],
        )

        # This is not a fixed single-name cap: budget is a function of remaining
        # capacity and evidence strength, while reserving capacity for others.
        self.assertGreater(ratio * 0.10, 0.08)
        self.assertLess(ratio * 0.10, 0.17)
        self.assertGreater(ratio, 0.12)
        self.assertIn("capital_utilization_memory_protected", reasons)
        self.assertEqual(diagnostics["capital_utilization_target"]["target_mode"], "alpha_release_boost")
        self.assertEqual(diagnostics["capital_utilization_target"]["alpha_release_tier"], "boost")
        self.assertTrue(diagnostics["capital_utilization_target"]["high_quality_memory"])
        self.assertTrue(diagnostics["capital_utilization_target"]["base_position_anchor_lifted"])
        self.assertEqual(diagnostics["capital_utilization_target"]["dynamic_allocation_tier"], "validated_with_stop")
        budget_diagnostics = diagnostics["capital_utilization_target"]["dynamic_budget_diagnostics"]
        self.assertEqual(budget_diagnostics["reserved_for_other_opportunities"], 0.0)
        self.assertGreater(budget_diagnostics["usable_after_reserve"], 0.0)

    def test_wildcard_protected_memory_cannot_trigger_strong_scaling(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="TA",
            trading_date="2025-02-13",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.037,
            margin_rate=0.07,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.64},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "protected_min_sample_count_for_scaling": 5,
                    "protected_min_win_rate_for_scaling": 0.60,
                    "protected_min_net_pnl_for_scaling": 1000,
                    "require_specific_signal_combo_for_strong_scaling": True,
                    "require_stop_protection_for_strong_scaling": True,
                }
            },
            signal_combo=("Bullish", "Neutral", "Bullish"),
            strategy_memory={
                "side_memory": {
                    "memory_state": "protected",
                    "signal_combo": "*",
                    "sample_count": 5,
                    "win_rate": 0.8,
                    "net_pnl": 2174.0,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[
                SimpleNamespace(invalidation_level=5100.0, atr_stop_distance=None),
            ],
        )

        self.assertLess(ratio * 0.07, 0.08)
        self.assertNotIn("capital_utilization_memory_protected", reasons)
        self.assertEqual(diagnostics["capital_utilization_target"]["target_mode"], "confirmed_observation")
        rejected = diagnostics.get("capital_utilization_learning", {}).get("protected_evidence_rejected", {})
        self.assertFalse(rejected.get("specific_signal_combo"))

    def test_missing_stop_protection_cannot_trigger_strong_scaling(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="TA",
            trading_date="2025-02-13",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.037,
            margin_rate=0.07,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.70},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "protected_min_sample_count_for_scaling": 5,
                    "protected_min_win_rate_for_scaling": 0.60,
                    "protected_min_net_pnl_for_scaling": 1000,
                    "require_specific_signal_combo_for_strong_scaling": True,
                    "require_stop_protection_for_strong_scaling": True,
                }
            },
            signal_combo=("Bullish", "Neutral", "Bullish"),
            strategy_memory={
                "combo": {
                    "memory_state": "protected",
                    "signal_combo": "Bullish|Neutral|Bullish",
                    "sample_count": 8,
                    "win_rate": 0.75,
                    "net_pnl": 8000.0,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[],
        )

        self.assertLess(ratio * 0.07, 0.08)
        self.assertNotIn("capital_utilization_memory_protected", reasons)
        self.assertEqual(diagnostics["capital_utilization_target"]["target_mode"], "confirmed_observation")
        rejected = diagnostics.get("capital_utilization_learning", {}).get("protected_evidence_rejected", {})
        self.assertEqual(rejected.get("reason"), "missing_pretrade_invalidation")
        self.assertEqual(rejected.get("alpha_release_tier"), "probe")

    def test_protected_memory_does_not_scale_when_current_combo_is_weak(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="M",
            trading_date="2025-02-20",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.00,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.82},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "block_scaling_on_conflicting_weak_memory": True,
                    "protected_min_sample_count_for_scaling": 5,
                }
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={
                "combo": {
                    "memory_state": "watchlist",
                    "signal_combo": "Bullish|Bullish|Neutral",
                    "sample_count": 3,
                    "win_rate": 0.0,
                    "net_pnl": -109208,
                },
                "side_memory": {
                    "memory_state": "protected",
                    "signal_combo": "Neutral|Bullish|Neutral",
                    "sample_count": 6,
                    "win_rate": 0.83,
                    "net_pnl": 12000,
                },
            },
            adaptive_policy_state=[],
        )

        self.assertAlmostEqual(ratio, 0.03)
        self.assertEqual(reasons, [])
        self.assertEqual(diagnostics["capital_utilization_skip"], "conflicting_weak_memory")
        self.assertIn("conflicting_weak_memory", diagnostics["capital_utilization_learning"])

    def test_protected_memory_requires_sufficient_samples_before_strong_scaling(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-20",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.03,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.82},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "protected_min_sample_count_for_scaling": 5,
                }
            },
            signal_combo=("Bullish", "Neutral", "Neutral"),
            strategy_memory={
                "combo": {
                    "memory_state": "protected",
                    "signal_combo": "Bullish|Neutral|Neutral",
                    "sample_count": 3,
                    "win_rate": 1.0,
                    "net_pnl": 8000,
                }
            },
            adaptive_policy_state=[],
        )

        self.assertLess(ratio * 0.10, 0.08)
        self.assertIn("capital_utilization_guard", reasons)
        self.assertNotIn("capital_utilization_memory_protected", reasons)
        self.assertEqual(diagnostics["capital_utilization_target"]["target_mode"], "confirmed_observation")
        self.assertIn(
            "protected_evidence_rejected",
            diagnostics.get("capital_utilization_learning", {}),
        )

    def test_adaptive_protect_requires_sufficient_samples_before_strong_scaling(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-20",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.03,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.82},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "protected_min_sample_count_for_scaling": 5,
                    "protected_min_win_rate_for_scaling": 0.60,
                    "protected_min_net_pnl_for_scaling": 1000,
                }
            },
            signal_combo=("Bullish", "Neutral", "Neutral"),
            strategy_memory={},
            adaptive_policy_state=[
                {
                    "policy_action": "protect",
                    "sample_count": 3,
                    "win_rate": 1.0,
                    "net_pnl": 8000,
                    "confidence_score": 0.9,
                }
            ],
        )

        self.assertLess(ratio * 0.10, 0.08)
        self.assertNotIn("capital_utilization_memory_protected", reasons)
        self.assertEqual(diagnostics["capital_utilization_target"]["target_mode"], "confirmed_observation")
        self.assertIn(
            "protected_evidence_rejected",
            diagnostics.get("capital_utilization_learning", {}),
        )

    def test_stop_protected_validated_alpha_gets_more_budget_but_not_all_in(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-10",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.00,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.82},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "memory_protected_min_confirmation_score": 0.45,
                    "dynamic_concentration_enabled": True,
                    "other_opportunity_reserve_fraction_of_tradable_capital": 0.10,
                    "validated_min_fraction_of_remaining_capacity": 0.35,
                    "validated_max_fraction_of_remaining_capacity": 0.90,
                    "confirmation_allocation_power": 1.25,
                    "stop_protection_allocation_bonus": 0.15,
                    "allow_memory_protected_scaling": True,
                }
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={
                "side_memory": {
                    "memory_state": "protected",
                    "signal_combo": "Bullish|Bullish|Neutral",
                    "sample_count": 5,
                    "win_rate": 0.8,
                    "net_pnl": 6000,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[
                SimpleNamespace(invalidation_level=3200.0, atr_stop_distance=None),
            ],
        )

        self.assertGreater(ratio * 0.10, 0.16)
        self.assertLess(ratio * 0.10, 0.20)
        self.assertIn("capital_utilization_memory_protected", reasons)
        self.assertTrue(diagnostics["capital_utilization_target"]["base_position_anchor_lifted"])
        self.assertEqual(diagnostics["capital_utilization_target"]["dynamic_allocation_tier"], "validated_with_stop")
        self.assertTrue(diagnostics["capital_utilization_target"]["stop_protected"])
        self.assertGreater(
            diagnostics["capital_utilization_target"]["dynamic_budget_diagnostics"]["reserved_for_other_opportunities"],
            0.0,
        )

    def test_exceptional_validated_alpha_can_take_most_capacity_but_reserves_some(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-20",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.00,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.92},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "memory_protected_min_confirmation_score": 0.60,
                    "dynamic_concentration_enabled": True,
                    "other_opportunity_reserve_fraction_of_tradable_capital": 0.15,
                    "validated_min_fraction_of_remaining_capacity": 0.25,
                    "validated_max_fraction_of_remaining_capacity": 0.65,
                    "confirmation_allocation_power": 1.75,
                    "stop_protection_allocation_bonus": 0.15,
                    "allow_memory_protected_scaling": True,
                    "protected_min_sample_count_for_scaling": 5,
                    "protected_min_win_rate_for_scaling": 0.60,
                    "protected_min_net_pnl_for_scaling": 1000,
                    "exceptional_validated_enabled": True,
                    "exceptional_validated_requires_stop_protection": True,
                    "exceptional_validated_min_confirmation_score": 0.85,
                    "exceptional_validated_min_sample_count": 8,
                    "exceptional_validated_min_win_rate": 0.70,
                    "exceptional_validated_min_net_pnl": 5000,
                    "exceptional_other_opportunity_reserve_fraction_of_tradable_capital": 0.05,
                    "exceptional_validated_min_fraction_of_remaining_capacity": 0.75,
                    "exceptional_validated_max_fraction_of_remaining_capacity": 0.95,
                    "exceptional_confirmation_allocation_power": 1.00,
                }
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={
                "side_memory": {
                    "memory_state": "protected",
                    "signal_combo": "Bullish|Bullish|Neutral",
                    "sample_count": 9,
                    "win_rate": 0.78,
                    "net_pnl": 12000,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[
                SimpleNamespace(invalidation_level=3200.0, atr_stop_distance=None),
            ],
        )

        margin_ratio = ratio * 0.10
        self.assertGreater(margin_ratio, 0.17)
        self.assertLessEqual(margin_ratio, 0.20)
        self.assertIn("capital_utilization_memory_protected", reasons)
        target = diagnostics["capital_utilization_target"]
        self.assertEqual(target["dynamic_allocation_tier"], "exceptional_validated_with_stop")
        self.assertTrue(target["dynamic_budget_diagnostics"]["exceptional_validated"])
        self.assertGreaterEqual(
            target["dynamic_budget_diagnostics"]["reserved_for_other_opportunities"],
            0.01,
        )

    def test_alpha_release_can_use_configured_net_exposure_weak_param(self):
        max_net, symmetric, mode = _resolve_net_exposure_control(
            {
                "net_exposure_control": {
                    "max_net_exposure": 0.50,
                    "strong_opportunity_max_net_exposure": 2.00,
                    "symmetric_scaling": True,
                }
            },
            {
                "capital_utilization_target": {
                    "target_mode": "alpha_release_boost",
                    "high_quality_memory": True,
                }
            },
        )

        self.assertEqual(max_net, 2.00)
        self.assertTrue(symmetric)
        self.assertEqual(mode, "alpha_release")

    def test_unproven_signal_keeps_base_net_exposure_weak_param(self):
        max_net, symmetric, mode = _resolve_net_exposure_control(
            {
                "net_exposure_control": {
                    "max_net_exposure": 0.50,
                    "strong_opportunity_max_net_exposure": 2.00,
                    "symmetric_scaling": True,
                }
            },
            {
                "capital_utilization_target": {
                    "target_mode": "confirmed_observation",
                    "high_quality_memory": False,
                }
            },
        )

        self.assertEqual(max_net, 0.50)
        self.assertTrue(symmetric)
        self.assertEqual(mode, "base")

    def test_capital_utilization_uses_observation_band_for_unproven_confirmed_signal(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-10",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.03,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.72},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                }
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={},
            adaptive_policy_state=[],
        )

        self.assertLess(ratio * 0.10, 0.04)
        self.assertGreater(ratio * 0.10, 0.0)
        self.assertIn("capital_utilization_guard", reasons)
        self.assertEqual(diagnostics["capital_utilization_target"]["target_mode"], "confirmed_observation")

    def test_capital_utilization_adds_to_matched_high_quality_same_side_position(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-11",
            position_ratio=0.03,
            current_ratio=0.03,
            current_margin_ratio=0.04,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.50},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.16,
                    "target_margin_ratio_max": 0.20,
                    "target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "memory_protected_min_confirmation_score": 0.45,
                    "allow_confirmed_same_side_add_on": True,
                    "protected_min_sample_count_for_scaling": 4,
                }
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={
                "combo": {
                    "memory_state": "deployable",
                    "signal_combo": "Bullish|Bullish|Neutral",
                    "sample_count": 4,
                    "win_rate": 0.75,
                    "net_pnl": 3200.0,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[
                SimpleNamespace(invalidation_level=3200.0, atr_stop_distance=None),
            ],
        )

        self.assertGreater(ratio * 0.10, 0.08)
        self.assertLess(ratio * 0.10, 0.17)
        self.assertIn("capital_utilization_same_side_add_on", reasons)
        self.assertIn("capital_utilization_same_side_add_on", diagnostics)

    def test_capital_utilization_does_not_expand_matched_unproven_position(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-11",
            position_ratio=0.03,
            current_ratio=0.03,
            current_margin_ratio=0.04,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.80},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.16,
                    "target_margin_ratio_max": 0.20,
                    "target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "allow_memory_protected_scaling": True,
                    "allow_confirmed_same_side_add_on": True,
                }
            },
            signal_combo=("Bullish", "Neutral", "Neutral"),
            strategy_memory={},
            adaptive_policy_state=[],
        )

        self.assertAlmostEqual(ratio, 0.03)
        self.assertEqual(reasons, [])
        self.assertNotIn("capital_utilization_same_side_add_on", diagnostics)


class StrictCompletionRegressionTest(unittest.TestCase):
    def test_technical_tradeability_is_evidence_driven_not_product_watchlist_driven(self):
        signal_results = {
            "trend": Signal.BULLISH,
            "macd": Signal.BULLISH,
            "adx": Signal.BULLISH,
            "settlement_trend": Signal.BULLISH,
        }
        features = {"trend_strength": 24.0, "volatility": 0.12, "volume_ratio": 1.05}

        former_watchlist_context = build_technical_context("MA", signal_results, features)
        control_context = build_technical_context("SR", signal_results, features)

        self.assertEqual(former_watchlist_context["tradeability"], control_context["tradeability"])
        self.assertEqual(former_watchlist_context["dominant_direction"], "bullish")
        self.assertNotIn("long_watchlist_requires_stronger_trend", former_watchlist_context["risk_flags"])
        self.assertNotIn("watchlist_long_weak_trend", former_watchlist_context["risk_flags"])
        self.assertNotIn("high_caution_ticker", former_watchlist_context["risk_flags"])

    def test_technical_tradeability_tightens_on_evidence_quality(self):
        signal_results = {
            "trend": Signal.BULLISH,
            "macd": Signal.BULLISH,
            "adx": Signal.BULLISH,
            "settlement_trend": Signal.BULLISH,
        }

        context = build_technical_context(
            "SR",
            signal_results,
            {"trend_strength": 21.0, "volatility": 0.36, "volume_ratio": 1.0},
        )

        self.assertEqual(context["tradeability"], "medium")
        self.assertIn("high_volatility", context["risk_flags"])
        self.assertIn("high_volatility_requires_extra_alignment", context["risk_flags"])

    def test_analyst_signal_has_explicit_context_fields(self):
        signal = AnalystSignal(
            signal=Signal.BULLISH,
            horizon_class="short",
            expected_horizon_days=2,
            market_regime="trending",
            trend_stage="early_trend",
            price_percentile=0.25,
            entry_trigger="reversal_confirmed",
            action_name="initial",
            invalidation_level=3200.0,
        )

        self.assertEqual(signal.horizon_class, "short")
        self.assertEqual(signal.expected_horizon_days, 2)
        self.assertEqual(signal.entry_trigger, "reversal_confirmed")

    def test_analyst_applicability_profile_changes_weights(self):
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.BULLISH, confidence=0.7, horizon_class="short", market_regime="trending"),
            AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.7, horizon_class="medium", market_regime="trending"),
            AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.3, horizon_class="event_short", market_regime="event_driven"),
        ]
        context = _quality_aware_fusion_context(
            ticker="BU",
            analyst_signals=signals,
            dynamic_weights={"technical": 1 / 3, "fundamental": 1 / 3, "commodity_news": 1 / 3},
            full_config={
                "analyst_applicability_profile": {
                    "enabled": True,
                    "technical": {"horizon_multipliers": {"short": 1.5}},
                    "fundamental": {"horizon_multipliers": {"medium": 1.2}},
                    "commodity_news": {"horizon_multipliers": {"event_short": 0.8}},
                }
            },
        )

        self.assertIn("technical", context["analyst_applicability_profile"])
        self.assertGreater(context["quality_adjusted_weights"]["technical"], 0.0)

    def test_analyst_applicability_profile_preserves_horizon_sector_regime_dimensions(self):
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.BULLISH, confidence=0.7, horizon_class="short", market_regime="trending"),
            AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.7, horizon_class="medium", market_regime="ranging"),
            AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.4, horizon_class="event_short", market_regime="event_driven", expected_horizon_days=1),
        ]
        context = _quality_aware_fusion_context(
            ticker="RB",
            analyst_signals=signals,
            dynamic_weights={"technical": 1 / 3, "fundamental": 1 / 3, "commodity_news": 1 / 3},
            full_config={
                "analyst_applicability_profile": {
                    "enabled": True,
                    "technical": {
                        "horizon_multipliers": {"short": 1.2},
                        "sector_multipliers": {"ferrous": 1.1},
                        "market_regime_multipliers": {"trending": 1.2},
                    },
                    "fundamental": {
                        "horizon_multipliers": {"medium": 1.2},
                        "sector_multipliers": {"ferrous": 1.15},
                        "market_regime_multipliers": {"ranging": 1.1},
                    },
                    "commodity_news": {
                        "event_window_days": 3,
                        "outside_event_window_multiplier": 0.5,
                        "horizon_multipliers": {"event_short": 1.25},
                        "sector_multipliers": {"ferrous": 0.95},
                        "market_regime_multipliers": {"event_driven": 1.2},
                    },
                }
            },
        )

        adjustments = context["analyst_applicability_profile"]
        self.assertEqual(context["sector"], "ferrous")
        self.assertEqual(adjustments["technical"]["horizon_class"], "short")
        self.assertEqual(adjustments["technical"]["sector"], "ferrous")
        self.assertEqual(adjustments["technical"]["market_regime"], "trending")
        self.assertEqual(adjustments["fundamental"]["horizon_class"], "medium")
        self.assertEqual(adjustments["commodity_news"]["horizon_class"], "event_short")
        self.assertGreater(adjustments["fundamental"]["multiplier"], 1.0)

    def test_neutral_signal_is_allowed_but_accountable(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.NEUTRAL,
            confidence=0.42,
            horizon_class="short",
            market_regime="range",
        )
        enriched = apply_business_quality_enrichment(
            signal,
            quality_context={
                "tradeability": "medium",
                "market_regime": "range",
                "risk_flags": ["conflicting_indicators"],
                "features": {"trend_strength": 0.2, "volume_ratio": 0.9},
            },
            full_config={"llm": {"provider": "test"}, "analyst_llm": {"cloud_model": "mock-model"}},
            analyst="technical",
        )

        self.assertEqual(enriched.signal, Signal.NEUTRAL)
        self.assertTrue(enriched.neutral_reason)
        self.assertTrue(enriched.missing_evidence)
        self.assertTrue(enriched.would_change_view_if)
        self.assertLessEqual(enriched.business_quality_score, 0.56)
        self.assertIn("business_quality", enriched.metadata)
        self.assertIn("neutral_opportunity_contract", enriched.metadata)
        self.assertEqual(enriched.neutral_opportunity_bucket, "conflict_avoidance")
        self.assertEqual(enriched.neutral_watchlist_priority, "low")

    def test_neutral_conditional_watchlist_is_structured_as_watch_for_trigger(self):
        snapshot = {
            "technical": {
                "signal": "Neutral",
                "neutral_reason": "waiting for breakout confirmation",
                "missing_evidence": [],
                "conflicting_factors": [],
                "would_change_view_if": "price breaks above 3200 with volume",
                "neutral_opportunity_bucket": "watchlist_trigger",
                "neutral_trigger_condition": "price breaks above 3200 with volume",
                "counterfactual_side": "long",
                "neutral_watchlist_priority": "medium",
                "metadata": {
                    "neutral_opportunity_contract": {
                        "bucket": "watchlist_trigger",
                        "trigger_condition": "price breaks above 3200 with volume",
                        "counterfactual_side": "long",
                        "watchlist_priority": "medium",
                        "tracking_only": True,
                        "opportunity_state": "watch_for_trigger",
                        "trigger_valid": False,
                        "action_preference": "watch_for_trigger",
                    }
                },
            },
            "fundamental": {"signal": "Neutral", "confidence": 0.5},
            "commodity_news": {"signal": "Neutral", "confidence": 0.5},
        }

        item = classify_neutral_signal(
            analyst="technical",
            payload=snapshot["technical"],
            snapshot=snapshot,
            cfg={},
        )
        summary = build_neutral_accountability_summary(
            [{"id": "rec-1", "underlying_code": "BU", "signal_snapshot": snapshot}],
            {},
        )

        self.assertEqual(item["category"], "conditional_watchlist")
        contract = item["neutral_opportunity_contract"]
        self.assertTrue(contract["tracking_only"])
        self.assertEqual(contract["opportunity_state"], "watch_for_trigger")
        self.assertFalse(contract["trigger_valid"])
        self.assertEqual(contract["action_preference"], "watch_for_trigger")
        self.assertEqual(summary["category_counts"]["conditional_watchlist"], 1)
        self.assertEqual(summary["by_analyst"]["technical"]["conditional_watchlist_count"], 1)

    def test_stale_fundamental_direction_is_forced_to_accountable_neutral(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.82,
            justification="inventory and demand look supportive",
            horizon_class="medium",
        )

        gated = apply_signal_quality_gate(
            signal,
            quality_context={
                "tradeability": "medium",
                "risk_flags": ["stale_fundamental_inputs"],
                "data_quality": {
                    "stale_ratio": 0.42,
                    "factor_freshness_score": 0.30,
                },
            },
            full_config={
                "analyst_llm": {
                    "force_neutral_stale_fundamental": True,
                    "cap_stale_fundamental_confidence": 0.30,
                }
            },
            analyst="fundamental",
        )

        self.assertEqual(gated.signal, Signal.NEUTRAL)
        self.assertLessEqual(gated.confidence, 0.30)
        self.assertIn("fresh supply-demand anchor", gated.missing_evidence)
        self.assertIn("stale_fundamental_direction_block", gated.metadata["risk_flags"])
        self.assertTrue(gated.metadata["quality_gate"]["stale_fundamental_direction_block"])

    def test_neutral_accountability_distinguishes_risk_avoidance_from_evidence_gap(self):
        snapshot = {
            "technical": {
                "signal": "Neutral",
                "neutral_reason": "conflicting indicators and low reward/risk",
                "missing_evidence": ["volume/open-interest confirmation"],
                "conflicting_factors": ["conflicting_indicators"],
                "would_change_view_if": "breakout confirms with volume",
                "reward_risk_ratio": 0.8,
                "metadata": {"risk_flags": ["conflicting_indicators"], "tradeability": "low"},
            },
            "fundamental": {
                "signal": "Neutral",
                "neutral_reason": "insufficient evidence from stale supply-demand data",
                "missing_evidence": ["fresh supply-demand anchor"],
                "conflicting_factors": [],
                "would_change_view_if": "fresh inventory and basis data align",
                "data_coverage_score": 0.20,
                "metadata": {"data_quality": {"coverage_ratio": 0.20, "stale_ratio": 0.50}},
            },
            "commodity_news": {
                "signal": "Bullish",
                "confidence": 0.70,
                "metadata": {"business_quality": {"score": 0.70}},
            },
        }

        risk_item = classify_neutral_signal(
            analyst="technical",
            payload=snapshot["technical"],
            snapshot=snapshot,
            cfg={},
        )
        gap_item = classify_neutral_signal(
            analyst="fundamental",
            payload=snapshot["fundamental"],
            snapshot=snapshot,
            cfg={},
        )
        summary = build_neutral_accountability_summary(
            [{"id": "rec-1", "underlying_code": "BU", "signal_snapshot": snapshot}],
            {},
        )

        self.assertEqual(risk_item["category"], "reasonable_avoidance")
        self.assertEqual(gap_item["category"], "evidence_gap_conservative")
        self.assertEqual(summary["category_counts"]["reasonable_avoidance"], 1)
        self.assertEqual(summary["category_counts"]["evidence_gap_conservative"], 1)
        self.assertAlmostEqual(summary["accountability_complete_rate"], 1.0)

    def test_snapshot_contract_adds_required_audit_header(self):
        snapshot = attach_snapshot_contract(
            {
                "technical": {"signal": "Bullish"},
                "pm_internal_draft": {"target_position_ratio": 0.05},
            },
            trading_date="2025-02-10",
            ticker="BU",
            config_id="cfg",
            source_artifacts=["technical:BU:2025-02-10"],
        )

        self.assertIn("artifact_contract", snapshot)
        self.assertEqual(validate_artifact_header(snapshot["artifact_contract"]), [])

    def test_reviewer_horizon_prefers_decision_scope_over_short_technical(self):
        snapshot = {
            "horizon_scope": {
                "decision_horizon": "medium",
                "analyst_horizons": {
                    "technical": {"analyst_horizon": "short"},
                    "fundamental": {"analyst_horizon": "medium"},
                    "commodity_news": {"analyst_horizon": "event_short"},
                },
            },
            "pm_internal_draft": {"expected_horizon_days": 2},
        }

        self.assertEqual(_horizon_class(2, snapshot), "medium")

    def test_template_prior_loads_into_strategy_memory_at_research_init(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            prior_path = tmp_path / "template_prior.json"
            prior_path.write_text(
                json.dumps(
                    {
                        "templates": [
                            {
                                "ticker": "I",
                                "side": "long",
                                "setup_type": "long_breakout_continuation_medium",
                                "horizon_class": "medium",
                                "prior_state": "protected",
                                "sample_count": 4,
                                "win_rate": 0.75,
                                "net_pnl": 12000.0,
                                "avg_pnl": 3000.0,
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            db_path = tmp_path / "agentquant.db"
            db = _PriorBootstrapDB(db_path)

            count = load_template_prior_if_enabled(
                {
                    "market_type": "china_futures",
                    "trading_date": "2025-01-02",
                    "learning": {
                        "memory_expires_after_days": 30,
                        "template_prior": {
                            "enabled": True,
                            "load_on_research_init": True,
                            "path": str(prior_path),
                        },
                    },
                },
                db,
                "cfg",
            )
            second_count = load_template_prior_if_enabled(
                {
                    "market_type": "china_futures",
                    "trading_date": "2025-01-03",
                    "learning": {
                        "memory_expires_after_days": 30,
                        "template_prior": {
                            "enabled": True,
                            "load_on_research_init": True,
                            "path": str(prior_path),
                        },
                    },
                },
                db,
                "cfg",
            )

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT * FROM strategy_memory WHERE config_id = ?", ("cfg",)).fetchone()
            finally:
                conn.close()

        self.assertEqual(count, 1)
        self.assertEqual(second_count, 0)
        self.assertEqual(row["ticker"], "I")
        self.assertEqual(row["side"], "long")
        self.assertEqual(row["memory_state"], "protected")
        self.assertEqual(row["source"], "template_prior")
        self.assertEqual(row["source_trading_date"], "2025-01-02")

    def test_template_prior_refreshes_when_source_marker_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            prior_path = tmp_path / "template_prior.json"
            db_path = tmp_path / "agentquant.db"
            db = _PriorBootstrapDB(db_path)
            cfg = {
                "market_type": "china_futures",
                "trading_date": "2025-01-26",
                "strategy_memory": {"weak_block_total_pnl_below": -2500},
                "learning": {
                    "memory_expires_after_days": 30,
                    "template_prior": {
                        "enabled": True,
                        "load_on_research_init": True,
                        "path": str(prior_path),
                    },
                },
            }
            prior_path.write_text(
                json.dumps(
                    {
                        "exported_at_trading_date": "2025-01-24",
                        "templates": [
                            {
                                "ticker": "P",
                                "side": "long",
                                "prior_state": "recovering",
                                "sample_count": 4,
                                "win_rate": 0.25,
                                "net_pnl": -7400,
                                "avg_pnl": -1850,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            first_count = load_template_prior_if_enabled(cfg, db, "cfg")
            same_count = load_template_prior_if_enabled(cfg, db, "cfg")
            prior_path.write_text(
                json.dumps(
                    {
                        "exported_at_trading_date": "2025-01-25",
                        "templates": [
                            {
                                "ticker": "BU",
                                "side": "long",
                                "prior_state": "protected",
                                "sample_count": 4,
                                "win_rate": 1.0,
                                "net_pnl": 9650,
                                "avg_pnl": 2412,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            refreshed_count = load_template_prior_if_enabled(cfg, db, "cfg")

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT ticker, side, memory_state, source_trading_date, payload_json FROM strategy_memory WHERE config_id = ?",
                    ("cfg",),
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(first_count, 1)
        self.assertEqual(same_count, 0)
        self.assertEqual(refreshed_count, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "BU")
        self.assertEqual(rows[0]["memory_state"], "protected")
        self.assertEqual(rows[0]["source_trading_date"], "2025-01-25")
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["source_exported_at_trading_date"], "2025-01-25")

    def test_template_prior_reclassifies_with_strategy_memory_thresholds(self):
        item = {
            "ticker": "MA",
            "side": "long",
            "prior_state": "recovering",
            "sample_count": 5,
            "win_rate": 0.40,
            "net_pnl": -8800,
        }

        state = classify_template_prior_item(
            item,
            {
                "strategy_memory": {
                    "min_samples_weak_block": 4,
                    "weak_block_win_rate_below": 0.30,
                    "weak_block_total_pnl_below": -2500,
                }
            },
        )

        self.assertEqual(state, "weak_block")

    def test_template_prior_loader_uses_project_root_for_relative_path(self):
        path = _project_path("src/logs/attribution/template_prior.json")

        self.assertEqual(path, SRC_ROOT / "logs" / "attribution" / "template_prior.json")
        self.assertNotIn("src\\src", str(path))
        self.assertNotIn("src/src", str(path))

    def test_template_prior_export_uses_project_relative_path_without_src_duplication(self):
        temp_parent = SRC_ROOT / "logs"
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_parent) as tmpdir:
            tmp_path = Path(tmpdir)
            project_root = SRC_ROOT.parent
            export_file = tmp_path / "template_prior.json"
            relative_export_path = export_file.relative_to(project_root)
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
            _ensure_reviewer_learning_schema(cursor)
            cursor.execute(
                """
                INSERT INTO setup_type_performance (
                    id, config_id, ticker, side, setup_type, horizon_class,
                    market_regime, sample_count, win_rate, net_pnl, avg_pnl,
                    profit_factor, confidence_score, last_updated, payload_json
                ) VALUES (
                    'perf', 'cfg', 'BU', 'long', 'long_breakout_short', 'short',
                    'trend', 3, 0.67, 3000, 1000, 1.5, 0.7, 'now', '{}'
                )
                """
            )

            prior_path = _export_template_prior(
                cursor,
                cfg={
                    "learning": {
                        "template_prior": {
                            "enabled": True,
                            "export_on_backtest_end": True,
                            "path": str(relative_export_path),
                        }
                    }
                },
                config_id="cfg",
                trading_date="2025-03-20",
            )
            conn.close()

            self.assertTrue(prior_path)
            self.assertNotIn("src\\src", prior_path)
            self.assertNotIn("src/src", prior_path)
            self.assertTrue(Path(prior_path).exists())


class ReviewerLearningPersistenceRegressionTest(unittest.TestCase):
    def _connection(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_reviewer_learning_schema(conn.cursor())
        return conn

    def _ranking_episode_payload(
        self,
        *,
        rec_id: str,
        net_pnl: float,
        score: float,
        rank: int,
        ticker: str = "RB",
        side: str = "short",
    ):
        return {
            "open_recommendation_id": rec_id,
            "candidate_side": side,
            "opportunity_type": "trend_breakout",
            "opportunity_state": "tradeable_candidate",
            "pair": {
                "ticker": ticker,
                "side": side,
                "net_pnl": net_pnl,
                "open_recommendation_id": rec_id,
            },
            "opportunity_ranking_trace": {
                "opportunity_score": score,
                "opportunity_rank": rank,
                "capital_allocation_reason": "ranking regression sample",
            },
            "signal_snapshot": {
                "market_regime": "trend",
                "final_action_contract": {
                    "evidence_used": {
                        "pm_fusion_diagnostics": {
                            "cross_analyst_conflict_count": 1,
                            "multi_evidence_consensus_score": 0.52,
                        },
                        "pm_conflict_resolution": {"handled": True},
                    }
                },
            },
        }

    def test_representative_episode_selector_uses_effect_specific_sample(self):
        rows = [
            self._ranking_episode_payload(rec_id="rec-high-score", net_pnl=-600.0, score=0.92, rank=1),
            self._ranking_episode_payload(rec_id="rec-largest-loss", net_pnl=-3200.0, score=0.74, rank=2),
            self._ranking_episode_payload(rec_id="rec-largest-gain", net_pnl=2800.0, score=0.81, rank=3),
        ]

        selected, reason = _select_representative_episode_payload(rows, "lower_priority")
        self.assertEqual(selected["open_recommendation_id"], "rec-largest-loss")
        self.assertEqual(reason, "largest_loss_for_lower_priority")

        selected, reason = _select_representative_episode_payload(rows, "raise_priority")
        self.assertEqual(selected["open_recommendation_id"], "rec-largest-gain")
        self.assertEqual(reason, "largest_gain_for_raise_priority")

        selected, reason = _select_representative_episode_payload(rows, "observe")
        self.assertEqual(selected["open_recommendation_id"], "rec-high-score")
        self.assertEqual(reason, "highest_score_for_observe")

    def test_opportunity_ranking_learning_events_write_representative_fusion_attribution(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            rows = [
                self._ranking_episode_payload(rec_id="rec-loss-small", net_pnl=-500.0, score=0.66, rank=1),
                self._ranking_episode_payload(rec_id="rec-loss-largest", net_pnl=-3600.0, score=0.82, rank=2),
                self._ranking_episode_payload(rec_id="rec-loss-mid", net_pnl=-1200.0, score=0.71, rank=3),
            ]

            inserted = _write_opportunity_ranking_learning_events(
                cursor,
                config_id="cfg",
                trading_date="2025-03-06",
                cfg={"opportunity_ranking_learning_policy": {"enabled": True, "min_samples_for_ranking_preference": 3}},
                episode_payloads=rows,
            )

            self.assertEqual(inserted, 1)
            events = cursor.execute(
                "SELECT event_type, evidence_json, action_json FROM learning_event_log ORDER BY event_type"
            ).fetchall()
            self.assertEqual({row["event_type"] for row in events}, {"evidence_fusion_attribution", "opportunity_ranking_preference"})

            preference = next(row for row in events if row["event_type"] == "opportunity_ranking_preference")
            preference_action = json.loads(preference["action_json"])
            self.assertEqual(preference_action["policy_action"], "lower_priority")

            attribution = next(row for row in events if row["event_type"] == "evidence_fusion_attribution")
            attribution_evidence = json.loads(attribution["evidence_json"])
            attribution_action = json.loads(attribution["action_json"])
            self.assertEqual(attribution_evidence["recommendation_id"], "rec-loss-largest")
            self.assertEqual(attribution_evidence["representative_recommendation_id"], "rec-loss-largest")
            self.assertEqual(attribution_evidence["source_episode_count"], 3)
            self.assertEqual(attribution_evidence["aggregation_scope"], "opportunity_ranking_group")
            self.assertEqual(attribution_evidence["attribution_scope"], "representative_episode")
            self.assertEqual(attribution_evidence["representative_selection_reason"], "largest_loss_for_lower_priority")
            self.assertEqual(attribution_evidence["fusion_attribution_label"], "fusion_conflict_handled")
            self.assertTrue(attribution_evidence["not_trade_authority"])
            self.assertTrue(attribution_action["does_not_modify_same_day_trade_facts"])
            self.assertTrue(attribution_action["does_not_create_trade_authority"])
            self.assertEqual(attribution_action["attribution_scope"], "representative_episode")
        finally:
            conn.close()

    def test_researcher_fail_fasts_incomplete_pm_consumable_action_value(self):
        from tools.agent_tools.research import research_memory_writers

        conn = self._connection()
        try:
            cursor = conn.cursor()
            with self.assertRaises(ValueError):
                research_memory_writers.upsert_alpha_setup_action_value(
                    cursor,
                    record={
                        "id": "av-incomplete",
                        "config_id": "cfg",
                        "scope_key": "RB|long|short|trend|setup",
                        "ticker": "RB",
                        "side": "long",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "setup_type": "breakout",
                        "data_combo": "technical",
                        "action_name": "open",
                        "sample_count": 2,
                        "reward_sum": 3000.0,
                        "reward_mean": 1500.0,
                        "win_rate": 0.5,
                        "confidence_score": 0.7,
                        "action_preference": "positive_candidate_open",
                        "reward_source": "real_trade",
                        "evidence_scope": "exact_real_state",
                        "action_value_lane": "open",
                        "consumer_scope": "pm_learning",
                        "learning_lane": "open",
                        "retrieval_key": "rb-long-open",
                        "fallback_retrieval_key": "rb-long",
                        "execution_retrieval_key": "rb-execution",
                        "max_position_impact": 0.02,
                        "last_sample_date": "2025-03-04",
                        "created_at": "2025-03-05T00:00:00+00:00",
                        "updated_at": "2025-03-05T00:00:00+00:00",
                        "valid_until": "2025-04-04",
                        "payload_json": json.dumps(
                            {
                                "action_value_lane": "open",
                                "learning_lane": "open",
                                "consumer_scope": "pm_learning",
                                "last_sample_date": "2025-03-04",
                                "valid_until": "2025-04-04",
                                "reward_source": "real_trade",
                                "evidence_scope": "exact_real_state",
                            }
                        ),
                    },
                )
            row = cursor.execute(
                "SELECT id FROM alpha_setup_action_value WHERE id='av-incomplete'"
            ).fetchone()
            self.assertIsNone(row)
        finally:
            conn.close()

    def test_researcher_canonicalizes_positive_open_action_value_preference(self):
        from tools.agent_tools.research import research_memory_writers

        conn = self._connection()
        try:
            cursor = conn.cursor()
            research_memory_writers.upsert_alpha_setup_action_value(
                cursor,
                record={
                    "id": "av-eb-open-positive",
                    "config_id": "cfg",
                    "scope_key": "EB|short|short|trend|news_event_setup|open",
                    "ticker": "EB",
                    "side": "short",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "news_event_setup",
                    "data_combo": "news",
                    "action_name": "open",
                    "sample_count": 1,
                    "reward_sum": 1200.0,
                    "reward_mean": 1200.0,
                    "win_rate": 1.0,
                    "confidence_score": 0.7,
                    "action_preference": "tail_loss_protect",
                    "reward_source": "real_trade",
                    "evidence_scope": "exact_real_state",
                    "action_value_lane": "open",
                    "consumer_scope": "pm_learning",
                    "learning_lane": "open",
                    "memory_side_role": "target_side",
                    "retrieval_key": "eb-short-open",
                    "fallback_retrieval_key": "eb-short",
                    "execution_retrieval_key": "eb-execution",
                    "max_position_impact": 0.02,
                    "last_sample_date": "2025-03-14",
                    "created_at": "2025-03-15T00:00:00+00:00",
                    "updated_at": "2025-03-15T00:00:00+00:00",
                    "valid_until": "2025-04-14",
                    "payload_json": json.dumps(
                        {
                            "action_preference": "tail_loss_protect",
                            "action_value_lane": "open",
                            "learning_lane": "open",
                            "consumer_scope": "pm_learning",
                            "memory_side_role": "target_side",
                            "last_sample_date": "2025-03-14",
                            "valid_until": "2025-04-14",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                        }
                    ),
                },
            )
            row = cursor.execute(
                "SELECT action_preference, canonical_action_family, consumer_scope, payload_json FROM alpha_setup_action_value WHERE id='av-eb-open-positive'"
            ).fetchone()

            payload = load_externalized_json(row["payload_json"])
            self.assertEqual(row["action_preference"], "positive_candidate_open")
            self.assertEqual(row["canonical_action_family"], "open_add_new_risk")
            self.assertEqual(row["consumer_scope"], "pm_learning")
            self.assertEqual(payload["action_preference"], "positive_candidate_open")
            self.assertEqual(payload["canonical_action_family"], "open_add_new_risk")
            self.assertEqual(payload["original_action_preference"], "tail_loss_protect")
        finally:
            conn.close()

    def test_researcher_keeps_execution_action_value_as_pm_learning_profile(self):
        from tools.agent_tools.research import research_memory_writers

        conn = self._connection()
        try:
            cursor = conn.cursor()
            research_memory_writers.upsert_alpha_setup_action_value(
                cursor,
                record={
                    "id": "av-c-execution",
                    "config_id": "cfg",
                    "scope_key": "C|long|short|trend|execution_pullback_setup|execution",
                    "ticker": "C",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "execution_pullback_setup",
                    "data_combo": "execution",
                    "action_name": "execution",
                    "sample_count": 1,
                    "reward_sum": 900.0,
                    "reward_mean": 900.0,
                    "win_rate": 1.0,
                    "confidence_score": 0.7,
                    "action_preference": "positive_candidate_execution",
                    "reward_source": "real_trade",
                    "evidence_scope": "exact_real_state",
                    "action_value_lane": "execution",
                    "consumer_scope": "pm_learning",
                    "learning_lane": "execution",
                    "memory_side_role": "historical_sample_side",
                    "retrieval_key": "c-long-execution",
                    "fallback_retrieval_key": "c-long",
                    "execution_retrieval_key": "c-execution",
                    "max_position_impact": 0.02,
                    "last_sample_date": "2025-03-14",
                    "created_at": "2025-03-15T00:00:00+00:00",
                    "updated_at": "2025-03-15T00:00:00+00:00",
                    "valid_until": "2025-04-14",
                    "payload_json": json.dumps(
                        {
                            "action_preference": "positive_candidate_execution",
                            "action_value_lane": "execution",
                            "learning_lane": "execution",
                            "consumer_scope": "pm_learning",
                            "memory_side_role": "historical_sample_side",
                            "last_sample_date": "2025-03-14",
                            "valid_until": "2025-04-14",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                        }
                    ),
                },
            )
            row = cursor.execute(
                "SELECT consumer_scope, canonical_action_family, payload_json FROM alpha_setup_action_value WHERE id='av-c-execution'"
            ).fetchone()

            payload = load_externalized_json(row["payload_json"])
            self.assertEqual(row["consumer_scope"], "pm_learning")
            self.assertEqual(row["canonical_action_family"], "execution")
            self.assertEqual(payload["canonical_action_family"], "execution")
            self.assertEqual(payload["action_value_lane"], "execution")
        finally:
            conn.close()

    def test_reviewer_summary_marks_lifecycle_and_pm_learning_influence_without_writing_memory(self):
        from tools.common.final_action_semantics import derive_memory_requirements

        contract = {
            "current_lots": 1,
            "target_lots": 0,
            "lots_delta": -1,
            "final_action": "exit",
            "authority_type": "exit",
        }
        requirements = derive_memory_requirements(contract)
        contract["learning_used"] = {
            "memory_requirements": requirements,
            "memory_retrieval": {
                "requirement_details": [
                    {
                        "side": "long",
                        "lane": "exit",
                        "memory_side_role": "current_position_side",
                        "row_count": 1,
                    }
                ]
            },
            "alpha_setup_action_values": [
                {
                    "side": "long",
                    "learning_lane": "exit",
                    "action_value_lane": "exit",
                    "memory_side_role": "current_position_side",
                    "action_preference": "positive_candidate_exit",
                }
            ],
        }
        summary = _final_action_semantic_summary(
            [
                {
                    "signal_snapshot": {
                        "final_action_contract": contract,
                        "execution_result": {"status": "executed"},
                    }
                }
            ]
        )

        self.assertEqual(summary["lifecycle_counts"].get("exit"), 1, summary)
        self.assertEqual(summary["historical_learning_influenced_contract_counts"].get("exit"), 1, summary)
        self.assertEqual(summary["pm_memory_consumption_error_count"], 0)
        self.assertFalse(summary["reviewer_writes_action_value"])

    def test_infer_setup_type_ignores_pm_draft_pm_internal_draft(self):
        setup_type = infer_setup_type(
            snapshot={
                "pm_internal_draft": {
                    "opportunity_type": "fundamental_inventory_anchor",
                    "pm_decision_layer": "tradeable_candidate",
                    "opportunity_scorecard": {"preferred_side": "short"},
                },
                "technical": {
                    "signal": "Bullish",
                    "setup_type": "trend_breakout",
                    "entry_trigger": "breakout confirmed",
                },
            },
            setup_type="",
            opportunity_type="",
            opportunity_state="",
        )

        self.assertEqual(setup_type, "trend_breakout_setup")

    def _create_trade_tables(self, cursor):
        cursor.execute(
            """
            CREATE TABLE futures_transactions (
                id TEXT,
                config_id TEXT,
                recommendation_id TEXT,
                trading_date TEXT,
                created_at TEXT,
                ticker TEXT,
                contract_code TEXT,
                action TEXT,
                lots INTEGER,
                execution_price REAL,
                price REAL,
                contract_multiplier REAL,
                commission REAL,
                source_type TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT,
                config_id TEXT,
                signal_snapshot TEXT
            )
            """
        )

    def _create_feedback_recommendation_table(self, cursor):
        cursor.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT,
                config_id TEXT,
                underlying_code TEXT,
                ticker TEXT,
                action TEXT,
                lots INTEGER,
                status TEXT,
                source_type TEXT,
                target_position_ratio REAL,
                signal_snapshot TEXT
            )
            """
        )

    def _signal_snapshot(self, *, learned: bool = False, learning_reason: str = "adaptive_policy_protect"):
        learning_used = {}
        reason_codes = []
        if learned:
            reason_codes = [learning_reason]
            learning_used = {"adaptive_policy_applied": [{"policy_type": "causal_review_rule"}]}
        return {
            "technical": {
                "signal": "Bullish",
                "setup_type": "reversal_confirmed",
                "horizon_class": "short",
            },
            "fundamental": {"signal": "Bullish"},
            "commodity_news": {"signal": "Neutral"},
            "horizon_scope": {"decision_horizon": "short"},
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "ZZ",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": 1,
                "lots_delta": 1,
                "target_position_ratio": 0.08,
                "horizon_class": "short",
                "market_regime": "trend",
                "reason_codes": reason_codes,
                "risk_flags": [],
                "learning_used": learning_used,
            },
        }

    def test_notes_only_causal_candidate_becomes_validated_policy_after_samples_mature(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            cursor.executemany(
                "INSERT INTO futures_recommendation VALUES (?, ?, ?)",
                [
                    ("r1", "cfg", json.dumps(self._signal_snapshot())),
                    ("r2", "cfg", json.dumps(self._signal_snapshot())),
                ],
            )
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("o1", "cfg", "r1", "2025-02-03", "2025-02-03T09:00:00", "BU", "bu2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                    ("c1", "cfg", "c1", "2025-02-04", "2025-02-04T14:55:00", "BU", "bu2505", "close_long", 1, 120.0, 120.0, 10.0, 1.0, "strategy"),
                    ("o2", "cfg", "r2", "2025-02-05", "2025-02-05T09:00:00", "BU", "bu2505", "open_long", 1, 110.0, 110.0, 10.0, 1.0, "strategy"),
                    ("c2", "cfg", "c2", "2025-02-06", "2025-02-06T14:55:00", "BU", "bu2505", "close_long", 1, 130.0, 130.0, 10.0, 1.0, "strategy"),
                ],
            )
            cursor.execute(
                """
                INSERT INTO researcher_llm_notes (
                    id, config_id, trading_date, evidence_pack_id, ticker,
                    raw_prompt, raw_response, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "note-1",
                    "cfg",
                    "2025-02-06",
                    "pack-1",
                    "*",
                    "",
                    "",
                    "now",
                    json.dumps(
                        {
                            "pre_trade_evidence": [
                                {"ticker": "BU", "action": "open_long", "signal_snapshot": self._signal_snapshot()}
                            ]
                        }
                    ),
                ),
            )
            cursor.execute(
                """
                INSERT INTO causal_review_candidate (
                    id, config_id, trading_date, evidence_pack_id, ticker, side,
                    candidate_type, confidence_score, rule_validation_status,
                    created_at, valid_until, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "cand-1",
                    "cfg",
                    "2025-02-06",
                    "pack-1",
                    "*",
                    "*",
                    "post_trade_causal_review",
                    0.80,
                    "notes_only_pending_rule_validation",
                    "now",
                    "2025-03-01",
                    json.dumps({"confidence_score": 0.80, "primary_cause": "validated positive template"}),
                ),
            )

            summary = _write_validated_causal_policy_rules(
                cursor,
                cfg={
                    "learning": {
                        "enabled": True,
                        "memory_expires_after_days": 30,
                        "researcher_causal_review": {
                            "enabled": True,
                            "rule_validation": {"min_samples": 2, "min_candidate_confidence": 0.35},
                        },
                    }
                },
                config_id="cfg",
                trading_date="2025-02-06",
            )

            cursor.execute(
                """
                SELECT *
                FROM adaptive_policy_state
                WHERE config_id = ? AND policy_type = 'causal_review_rule'
                """,
                ("cfg",),
            )
            policy = dict(cursor.fetchone())
            cursor.execute("SELECT rule_validation_status, payload_json FROM causal_review_candidate WHERE id = ?", ("cand-1",))
            candidate = dict(cursor.fetchone())

            self.assertEqual(summary["validated_rules"], 1)
            self.assertEqual(policy["ticker"], "BU")
            self.assertEqual(policy["side"], "long")
            self.assertEqual(policy["policy_action"], "protect")
            self.assertEqual(policy["sample_count"], 2)
            policy_payload = json.loads(policy["payload_json"])
            self.assertEqual(policy_payload[CONTRACT_KEY]["contract_version"], "next_round_strategy_update_v2")
            self.assertEqual(policy_payload[CONTRACT_KEY]["position_authority"], "pm_auditor_conditioned")
            self.assertIn("may_support_alpha_scaling", policy_payload[CONTRACT_KEY]["max_position_impact"])
            self.assertEqual(candidate["rule_validation_status"], "validated_rule_applied")
            self.assertEqual(json.loads(candidate["payload_json"])["rule_validation"]["applied_rule_count"], 1)
        finally:
            conn.close()

    def test_learned_vs_unlearned_trade_performance_splits_completed_pairs(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            cursor.executemany(
                "INSERT INTO futures_recommendation VALUES (?, ?, ?)",
                [
                    ("learned-r", "cfg", json.dumps(self._signal_snapshot(learned=True))),
                    ("plain-r", "cfg", json.dumps(self._signal_snapshot(learned=False))),
                ],
            )
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("lo", "cfg", "learned-r", "2025-02-03", "2025-02-03T09:00:00", "BU", "bu2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                    ("lc", "cfg", "lc", "2025-02-04", "2025-02-04T14:55:00", "BU", "bu2505", "close_long", 1, 120.0, 120.0, 10.0, 1.0, "strategy"),
                    ("uo", "cfg", "plain-r", "2025-02-05", "2025-02-05T09:00:00", "BU", "bu2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                    ("uc", "cfg", "uc", "2025-02-06", "2025-02-06T14:55:00", "BU", "bu2505", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                ],
            )

            summary = learned_vs_unlearned_trade_performance(
                cursor,
                config_id="cfg",
                trading_date="2025-02-06",
            )

            self.assertEqual(summary["learned"]["total_trades"], 1)
            self.assertEqual(summary["unlearned"]["total_trades"], 1)
            self.assertGreater(summary["learned"]["net_pnl"], 0)
            self.assertLess(summary["unlearned"]["net_pnl"], 0)
            self.assertEqual(summary["learned_reason_counts"]["adaptive_policy"], 1)
            self.assertEqual(summary["learned_effect_counts"]["alpha_release"], 1)
            self.assertEqual(summary["learned_effect_summary"]["alpha_release"]["total_trades"], 1)
        finally:
            conn.close()

    def test_learned_underperformance_writes_scoped_alpha_demote_policy(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            recommendations = []
            transactions = []
            for idx in range(3):
                rec_id = f"learned-{idx}"
                recommendations.append((rec_id, "cfg", json.dumps(self._signal_snapshot(learned=True))))
                transactions.extend(
                    [
                        (f"lo-{idx}", "cfg", rec_id, f"2025-02-0{idx + 1}", f"2025-02-0{idx + 1}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"lc-{idx}", "cfg", f"lc-{idx}", f"2025-02-1{idx + 1}", f"2025-02-1{idx + 1}T14:55:00", "ZZ", "zz2505", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                    ]
                )
            for idx in range(3):
                rec_id = f"plain-{idx}"
                recommendations.append((rec_id, "cfg", json.dumps(self._signal_snapshot(learned=False))))
                transactions.extend(
                    [
                        (f"uo-{idx}", "cfg", rec_id, f"2025-02-2{idx}", f"2025-02-2{idx}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"uc-{idx}", "cfg", f"uc-{idx}", f"2025-02-2{idx}", f"2025-02-2{idx}T14:55:00", "ZZ", "zz2505", "close_long", 1, 115.0, 115.0, 10.0, 1.0, "strategy"),
                    ]
                )
            cursor.executemany("INSERT INTO futures_recommendation VALUES (?, ?, ?)", recommendations)
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                transactions,
            )

            result = _write_learned_vs_unlearned_policy_state(
                cursor,
                config_id="cfg",
                trading_date="2025-02-28",
                cfg={"learning": {"learned_vs_unlearned_policy": {"enabled": True}}},
            )

            row = cursor.execute(
                """
                SELECT *
                FROM adaptive_policy_state
                WHERE config_id = ? AND policy_type = ?
                """,
                ("cfg", "learned_vs_unlearned"),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["ticker"], "ZZ")
            self.assertEqual(row["side"], "long")
            self.assertEqual(row["setup_type"], "long_reversal_confirmed_short")
            self.assertEqual(row["policy_action"], "demote")
            self.assertEqual(result["status"], "scoped_demote_applied")
        finally:
            conn.close()

    def test_learning_mechanism_policy_promotes_profitable_same_scope_mechanism(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            recommendations = []
            transactions = []
            for idx in range(4):
                rec_id = f"mechanism-alpha-{idx}"
                recommendations.append(
                    (
                        rec_id,
                        "cfg",
                        json.dumps(
                            self._signal_snapshot(
                                learned=True,
                                learning_reason="capital_utilization_memory_protected",
                            )
                        ),
                    )
                )
                transactions.extend(
                    [
                        (f"mo-{idx}", "cfg", rec_id, f"2025-02-0{idx + 1}", f"2025-02-0{idx + 1}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"mc-{idx}", "cfg", f"mc-{idx}", f"2025-02-1{idx + 1}", f"2025-02-1{idx + 1}T14:55:00", "ZZ", "zz2505", "close_long", 1, 120.0, 120.0, 10.0, 1.0, "strategy"),
                    ]
                )
            cursor.executemany("INSERT INTO futures_recommendation VALUES (?, ?, ?)", recommendations)
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                transactions,
            )

            result = _write_learning_mechanism_policy_state(
                cursor,
                config_id="cfg",
                trading_date="2025-02-28",
                cfg={
                    "learning": {
                        "learning_mechanism_policy": {
                            "enabled": True,
                            "min_samples": 4,
                            "min_positive_win_rate": 0.55,
                            "min_positive_net_pnl": 1,
                        }
                    }
                },
            )

            self.assertGreaterEqual(result["rows"], 1)
            row = cursor.execute(
                """
                SELECT *
                FROM adaptive_policy_state
                WHERE config_id = ? AND policy_type = ?
                """,
                ("cfg", "learning_mechanism:alpha_promotion"),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["ticker"], "ZZ")
            self.assertEqual(row["policy_action"], "protect")
            self.assertGreaterEqual(row["sample_count"], 4)
            payload = load_externalized_json(row["payload_json"])
            self.assertEqual(payload["learning_mechanism"], "alpha_promotion")
            self.assertEqual(payload[CONTRACT_KEY]["position_authority"], "pm_auditor_conditioned")
        finally:
            conn.close()

    def test_learning_mechanism_policy_caps_weak_same_scope_mechanism(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            recommendations = []
            transactions = []
            for idx in range(4):
                rec_id = f"mechanism-weak-{idx}"
                recommendations.append(
                    (
                        rec_id,
                        "cfg",
                        json.dumps(
                            self._signal_snapshot(
                                learned=True,
                                learning_reason="strategy_memory_weak_block",
                            )
                        ),
                    )
                )
                transactions.extend(
                    [
                        (f"wo-{idx}", "cfg", rec_id, f"2025-02-0{idx + 1}", f"2025-02-0{idx + 1}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"wc-{idx}", "cfg", f"wc-{idx}", f"2025-02-1{idx + 1}", f"2025-02-1{idx + 1}T14:55:00", "ZZ", "zz2505", "close_long", 1, 80.0, 80.0, 10.0, 1.0, "strategy"),
                    ]
                )
            cursor.executemany("INSERT INTO futures_recommendation VALUES (?, ?, ?)", recommendations)
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                transactions,
            )

            result = _write_learning_mechanism_policy_state(
                cursor,
                config_id="cfg",
                trading_date="2025-02-28",
                cfg={
                    "learning": {
                        "learning_mechanism_policy": {
                            "enabled": True,
                            "min_samples": 4,
                            "max_negative_net_pnl": -1,
                            "cap_multiplier": 0.40,
                        }
                    }
                },
            )

            self.assertGreaterEqual(result["rows"], 1)
            row = cursor.execute(
                """
                SELECT *
                FROM adaptive_policy_state
                WHERE config_id = ? AND policy_type = ?
                """,
                ("cfg", "learning_mechanism:strategy_memory_weak_block"),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["policy_action"], "cap")
            self.assertAlmostEqual(float(row["multiplier"]), 0.40)
            payload = load_externalized_json(row["payload_json"])
            self.assertEqual(payload[CONTRACT_KEY]["position_authority"], "risk_reduction_conditioned")
        finally:
            conn.close()

    def test_learning_mechanism_policy_guard_blocks_concentrated_positive_sample(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            recommendations = []
            transactions = []
            for idx, pnl in enumerate([9000.0, 100.0, 100.0, 100.0]):
                rec_id = f"mechanism-concentrated-{idx}"
                recommendations.append(
                    (
                        rec_id,
                        "cfg",
                        json.dumps(
                            self._signal_snapshot(
                                learned=True,
                                learning_reason="capital_utilization_memory_protected",
                            )
                        ),
                    )
                )
                close_price = 100.0 + pnl / 10.0
                transactions.extend(
                    [
                        (f"co-{idx}", "cfg", rec_id, f"2025-02-0{idx + 1}", f"2025-02-0{idx + 1}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"cc-{idx}", "cfg", f"cc-{idx}", f"2025-02-1{idx + 1}", f"2025-02-1{idx + 1}T14:55:00", "ZZ", "zz2505", "close_long", 1, close_price, close_price, 10.0, 1.0, "strategy"),
                    ]
                )
            cursor.executemany("INSERT INTO futures_recommendation VALUES (?, ?, ?)", recommendations)
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                transactions,
            )

            result = _write_learning_mechanism_policy_state(
                cursor,
                config_id="cfg",
                trading_date="2025-02-28",
                cfg={
                    "learning": {
                        "policy_promotion_guard": {
                            "enabled": True,
                            "min_distinct_trade_days_for_protect": 4,
                            "min_calendar_span_days_for_protect": 10,
                            "max_single_trade_pnl_share": 0.65,
                        },
                        "learning_mechanism_policy": {
                            "enabled": True,
                            "min_samples": 4,
                            "min_positive_win_rate": 0.55,
                            "min_positive_net_pnl": 1,
                        },
                    }
                },
            )

            self.assertEqual(result["rows"], 0)
            self.assertGreaterEqual(result["guarded_rows"], 1)
            row = cursor.execute(
                """
                SELECT *
                FROM adaptive_policy_state
                WHERE config_id = ? AND policy_type = ?
                """,
                ("cfg", "learning_mechanism:alpha_promotion"),
            ).fetchone()
            self.assertIsNone(row)
            guard_event = cursor.execute(
                "SELECT action_json FROM learning_event_log WHERE event_type='learning_mechanism_policy_guard'"
            ).fetchone()
            self.assertIsNotNone(guard_event)
        finally:
            conn.close()

    def test_learning_mechanism_policy_counterfactual_reversal_deactivates_existing_cap(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            cursor.execute(
                """
                INSERT INTO adaptive_policy_state (
                    id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                    policy_type, policy_action, multiplier, confidence_score, sample_count,
                    reason, source_event_id, created_at, valid_until, payload_json, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    "old-cap",
                    "cfg",
                    "ZZ",
                    "long",
                    "long_reversal_confirmed_short",
                    "short",
                    "trend",
                    "learning_mechanism:strategy_memory_weak_block",
                    "cap",
                    0.5,
                    0.7,
                    4,
                    "old weak cap",
                    "event-old",
                    "now",
                    "2025-03-30",
                    "{}",
                ),
            )
            cursor.executemany(
                """
                INSERT INTO no_trade_opportunity_memory (
                    id, config_id, trading_date, ticker, side, setup_type,
                    horizon_class, market_regime, counterfactual_results_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("nt-rev-1", "cfg", "2025-02-20", "ZZ", "long", "long_reversal_confirmed_short", "short", "trend", json.dumps([{"evaluation_date": "2025-02-25", "counterfactual_pnl": 2200.0}]), "now"),
                    ("nt-rev-2", "cfg", "2025-02-21", "ZZ", "long", "long_reversal_confirmed_short", "short", "trend", json.dumps([{"evaluation_date": "2025-02-26", "counterfactual_pnl": 2100.0}]), "now"),
                ],
            )
            recommendations = []
            transactions = []
            for idx in range(4):
                rec_id = f"mechanism-weak-rev-{idx}"
                recommendations.append(
                    (
                        rec_id,
                        "cfg",
                        json.dumps(
                            self._signal_snapshot(
                                learned=True,
                                learning_reason="strategy_memory_weak_block",
                            )
                        ),
                    )
                )
                transactions.extend(
                    [
                        (f"rwo-{idx}", "cfg", rec_id, f"2025-02-0{idx + 1}", f"2025-02-0{idx + 1}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"rwc-{idx}", "cfg", f"rwc-{idx}", f"2025-02-1{idx + 1}", f"2025-02-1{idx + 1}T14:55:00", "ZZ", "zz2505", "close_long", 1, 80.0, 80.0, 10.0, 1.0, "strategy"),
                    ]
                )
            cursor.executemany("INSERT INTO futures_recommendation VALUES (?, ?, ?)", recommendations)
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                transactions,
            )

            result = _write_learning_mechanism_policy_state(
                cursor,
                config_id="cfg",
                trading_date="2025-02-28",
                cfg={
                    "learning": {
                        "policy_promotion_guard": {
                            "enabled": True,
                            "min_distinct_trade_days_for_cap": 3,
                            "min_calendar_span_days_for_cap": 5,
                            "counterfactual_reversal": {"enabled": True, "min_samples": 2, "min_net_pnl": 3000},
                        },
                        "learning_mechanism_policy": {
                            "enabled": True,
                            "min_samples": 4,
                            "max_negative_net_pnl": -1,
                            "cap_multiplier": 0.40,
                        },
                    }
                },
            )

            self.assertEqual(result["rows"], 0)
            self.assertGreaterEqual(result["guarded_rows"], 1)
            old_cap = cursor.execute(
                "SELECT active, reason FROM adaptive_policy_state WHERE id='old-cap'"
            ).fetchone()
            self.assertEqual(old_cap["active"], 0)
            self.assertIn("counterfactual reversal", old_cap["reason"])
        finally:
            conn.close()

    def test_pm_can_read_policy_payload_performance_columns(self):
        row = {
            "policy_type": "learning_mechanism:alpha_promotion",
            "policy_action": "protect",
            "sample_count": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "payload": {
                "evidence": {
                    "sample_count": 5,
                    "win_rate": 0.8,
                    "net_pnl": 2600.0,
                    "summary": {"total_trades": 5, "avg_pnl": 520.0},
                }
            },
        }

        enriched = enriched_policy_evidence(row)

        self.assertEqual(enriched["sample_count"], 5)
        self.assertEqual(enriched["win_rate"], 0.8)
        self.assertEqual(enriched["net_pnl"], 2600.0)
        self.assertEqual(enriched["avg_pnl"], 520.0)

    def test_research_position_feedback_links_memory_to_position_chain(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_feedback_recommendation_table(cursor)
            snapshot = {
                "technical": {"signal": "Bullish", "setup_type": "breakout", "horizon_class": "short"},
                "fundamental": {"signal": "Neutral"},
                "commodity_news": {"signal": "Neutral"},
                "horizon_scope": {"decision_horizon": "short"},
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "BU",
                    "final_action": "open_real",
                    "current_lots": 0,
                    "target_lots": 2,
                    "lots_delta": 2,
                    "target_position_ratio": 0.08,
                    "reason_codes": "target_plan",
                    "reason_codes": ["learning_mechanism:alpha_promotion"],
                    "learning_used": {
                        "learning_context": {
                            "enabled": True,
                            "memory_trace": {
                                "selected_memory_refs": [
                                    {
                                        "memory_type": "trade_episode_memory",
                                        "id": "episode-1",
                                        "ticker": "BU",
                                        "side": "long",
                                        "horizon_class": "short",
                                        "market_regime": "trend",
                                        "setup_type": "long_breakout_short",
                                    }
                                ]
                            },
                        },
                        "adaptive_policy_state": {
                            "policies": [
                                {
                                    "policy_type": "learning_mechanism:alpha_promotion",
                                    "policy_action": "protect",
                                    "ticker": "BU",
                                    "side": "long",
                                    "setup_type": "long_breakout_short",
                                    "horizon_class": "short",
                                    "market_regime": "trend",
                                    "sample_count": 4,
                                    "confidence_score": 0.7,
                                }
                            ]
                        },
                        "position_effect": {
                            "current_lots": 0,
                            "target_lots": 2,
                            "lots_delta": 2,
                            "final_target_position_ratio": 0.08,
                        },
                    },
                },
                "active_opportunity_audit": {
                    "decision": {"authority_type": "real_budget_entry"},
                    "reason_codes": ["learning_mechanism:alpha_promotion"],
                },
                "execution_result": {"no_trade_reason": ""},
            }
            cursor.execute(
                """
                INSERT INTO futures_recommendation
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "rec-feedback",
                    "cfg",
                    "BU",
                    "BU",
                    "open_long",
                    2,
                    "pending",
                    "strategy",
                    0.08,
                    json.dumps(snapshot),
                ),
            )
            result = _write_research_position_feedback(
                cursor,
                cfg={
                    "learning": {
                        "position_feedback_loop": {
                            "enabled": True,
                            "valid_days": 30,
                            "max_digest_rows_per_day": 4,
                        }
                    }
                },
                config_id="cfg",
                trading_date="2025-02-28",
                strategy_recommendations=[
                    {
                        "id": "rec-feedback",
                        "config_id": "cfg",
                        "underlying_code": "BU",
                        "ticker": "BU",
                        "action": "open_long",
                        "lots": 2,
                        "status": "pending",
                        "source_type": "strategy",
                        "target_position_ratio": 0.08,
                        "signal_snapshot": json.dumps(snapshot),
                    }
                ],
                transactions_by_recommendation={
                    "rec-feedback": [
                        {"lots": 2, "realized_pnl": 1200.0, "commission": 12.0}
                    ]
                },
                settlement_row={"daily_pnl": 1200.0},
            )

            self.assertEqual(result["feedback_rows"], 1)
            self.assertEqual(result["digest_rows"], 1)
            row = cursor.execute("SELECT * FROM research_position_feedback").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["ticker"], "BU")
            self.assertEqual(row["feedback_label"], "learning_position_executed_profit")
            payload = json.loads(row["payload_json"])
            self.assertEqual(payload["memory_refs"][0]["id"], "episode-1")
            self.assertEqual(payload["policy_refs"][0]["policy_type"], "learning_mechanism:alpha_promotion")
            digest = cursor.execute(
                "SELECT * FROM analyst_learning_digest WHERE analyst='portfolio_manager'"
            ).fetchone()
            self.assertIsNotNone(digest)
            self.assertIn("learning-to-position feedback", digest["digest_text"])
        finally:
            conn.close()

    def test_research_position_feedback_uses_final_contract_not_legacy_recommendation_fields(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_feedback_recommendation_table(cursor)
            snapshot = {
                "technical": {"signal": "Bullish", "setup_type": "breakout", "horizon_class": "short"},
                "fundamental": {"signal": "Neutral"},
                "commodity_news": {"signal": "Neutral"},
                "horizon_scope": {"decision_horizon": "short"},
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "BU",
                    "final_action": "hold",
                    "current_lots": 0,
                    "target_lots": 0,
                    "lots_delta": 0,
                    "target_position_ratio": 0.0,
                    "reason_codes": "position_matched",
                    "learning_used": {
                        "learning_context": {
                            "enabled": True,
                            "memory_trace": {
                                "selected_memory_refs": [
                                    {
                                        "memory_type": "trade_episode_memory",
                                        "id": "episode-flat",
                                        "ticker": "BU",
                                        "side": "long",
                                    }
                                ]
                            },
                        }
                    },
                },
            }
            result = _write_research_position_feedback(
                cursor,
                cfg={"learning": {"position_feedback_loop": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-02-28",
                strategy_recommendations=[
                    {
                        "id": "rec-legacy-leak",
                        "config_id": "cfg",
                        "underlying_code": "BU",
                        "ticker": "BU",
                        "action": "open_long",
                        "lots": 5,
                        "status": "pending",
                        "source_type": "strategy",
                        "target_position_ratio": 0.08,
                        "signal_snapshot": json.dumps(snapshot),
                    }
                ],
                transactions_by_recommendation={},
                settlement_row={"daily_pnl": 0.0},
            )

            self.assertEqual(result["feedback_rows"], 1)
            row = cursor.execute("SELECT * FROM research_position_feedback").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["side"], "flat")
            self.assertEqual(row["target_lots"], 0)
            self.assertEqual(row["position_delta_lots"], 0)
            self.assertEqual(row["target_position_ratio"], 0.0)
            payload = json.loads(row["payload_json"])
            self.assertEqual(payload["pm_effect"]["target_lots"], 0)
            self.assertEqual(payload["recommendation"]["target_position_ratio"], 0.0)
            self.assertEqual(payload["outcome"]["feedback_label"], "learning_observed_no_position")
        finally:
            conn.close()

    def test_learned_underperformance_writes_scoped_risk_suppression_demote_policy(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            recommendations = []
            transactions = []
            for idx in range(3):
                rec_id = f"learned-risk-{idx}"
                recommendations.append(
                    (
                        rec_id,
                        "cfg",
                        json.dumps(
                            self._signal_snapshot(
                                learned=True,
                                learning_reason="strategy_memory_watchlist_cap",
                            )
                        ),
                    )
                )
                transactions.extend(
                    [
                        (f"lro-{idx}", "cfg", rec_id, f"2025-02-0{idx + 1}", f"2025-02-0{idx + 1}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"lrc-{idx}", "cfg", f"lrc-{idx}", f"2025-02-1{idx + 1}", f"2025-02-1{idx + 1}T14:55:00", "ZZ", "zz2505", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                    ]
                )
            for idx in range(3):
                rec_id = f"plain-risk-{idx}"
                recommendations.append((rec_id, "cfg", json.dumps(self._signal_snapshot(learned=False))))
                transactions.extend(
                    [
                        (f"uro-{idx}", "cfg", rec_id, f"2025-02-2{idx}", f"2025-02-2{idx}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"urc-{idx}", "cfg", f"urc-{idx}", f"2025-02-2{idx}", f"2025-02-2{idx}T14:55:00", "ZZ", "zz2505", "close_long", 1, 115.0, 115.0, 10.0, 1.0, "strategy"),
                    ]
                )
            cursor.executemany("INSERT INTO futures_recommendation VALUES (?, ?, ?)", recommendations)
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                transactions,
            )

            result = _write_learned_vs_unlearned_policy_state(
                cursor,
                config_id="cfg",
                trading_date="2025-02-28",
                cfg={"learning": {"learned_vs_unlearned_policy": {"enabled": True}}},
            )

            row = cursor.execute(
                """
                SELECT *
                FROM adaptive_policy_state
                WHERE config_id = ? AND policy_type = ?
                """,
                ("cfg", "learned_vs_unlearned"),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["ticker"], "ZZ")
            self.assertEqual(row["policy_action"], "demote")
            self.assertIn("risk_suppression", row["reason"])
            self.assertEqual(result["status"], "scoped_demote_applied")
        finally:
            conn.close()

    def test_global_learned_underperformance_without_scoped_alpha_is_diagnostic_only(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            recommendations = []
            transactions = []
            for idx in range(3):
                rec_id = f"learned-{idx}"
                recommendations.append((rec_id, "cfg", json.dumps(self._signal_snapshot(learned=True))))
                ticker = f"Z{idx}"
                transactions.extend(
                    [
                        (f"lo-{idx}", "cfg", rec_id, f"2025-02-0{idx + 1}", f"2025-02-0{idx + 1}T09:00:00", ticker, "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"lc-{idx}", "cfg", f"lc-{idx}", f"2025-02-1{idx + 1}", f"2025-02-1{idx + 1}T14:55:00", ticker, "zz2505", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                    ]
                )
            for idx in range(3):
                rec_id = f"plain-{idx}"
                recommendations.append((rec_id, "cfg", json.dumps(self._signal_snapshot(learned=False))))
                transactions.extend(
                    [
                        (f"uo-{idx}", "cfg", rec_id, f"2025-02-2{idx}", f"2025-02-2{idx}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"uc-{idx}", "cfg", f"uc-{idx}", f"2025-02-2{idx}", f"2025-02-2{idx}T14:55:00", "ZZ", "zz2505", "close_long", 1, 115.0, 115.0, 10.0, 1.0, "strategy"),
                    ]
                )
            cursor.executemany("INSERT INTO futures_recommendation VALUES (?, ?, ?)", recommendations)
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                transactions,
            )

            result = _write_learned_vs_unlearned_policy_state(
                cursor,
                config_id="cfg",
                trading_date="2025-02-28",
                cfg={"learning": {"learned_vs_unlearned_policy": {"enabled": True}}},
            )

            self.assertEqual(result["status"], "global_underperformance_diagnostic_only")
            rows = cursor.execute(
                """
                SELECT *
                FROM adaptive_policy_state
                WHERE config_id = ? AND policy_type = ?
                """,
                ("cfg", "learned_vs_unlearned"),
            ).fetchall()
            self.assertEqual(rows, [])
        finally:
            conn.close()

    def test_scoped_learned_self_loss_demotes_without_benchmark(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            recommendations = []
            transactions = []
            for idx in range(3):
                rec_id = f"learned-self-loss-{idx}"
                recommendations.append((rec_id, "cfg", json.dumps(self._signal_snapshot(learned=True))))
                transactions.extend(
                    [
                        (f"slo-{idx}", "cfg", rec_id, f"2025-02-0{idx + 1}", f"2025-02-0{idx + 1}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"slc-{idx}", "cfg", f"slc-{idx}", f"2025-02-1{idx + 1}", f"2025-02-1{idx + 1}T14:55:00", "ZZ", "zz2505", "close_long", 1, 50.0, 50.0, 10.0, 1.0, "strategy"),
                    ]
                )
            cursor.executemany("INSERT INTO futures_recommendation VALUES (?, ?, ?)", recommendations)
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                transactions,
            )

            result = _write_learned_vs_unlearned_policy_state(
                cursor,
                config_id="cfg",
                trading_date="2025-02-28",
                cfg={
                    "learning": {
                        "learned_vs_unlearned_policy": {
                            "enabled": True,
                            "min_scoped_alpha_samples": 3,
                            "allow_self_loss_demote_without_benchmark": True,
                            "min_self_loss_net_pnl": -1000,
                        }
                    }
                },
            )

            row = cursor.execute(
                """
                SELECT *
                FROM adaptive_policy_state
                WHERE config_id = ? AND policy_type = ?
                """,
                ("cfg", "learned_vs_unlearned"),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(result["status"], "scoped_demote_applied")
            payload = load_externalized_json(row["payload_json"])
            self.assertEqual(
                payload["scoped_underperformance"]["comparison_status"],
                "same_scope_self_loss_without_benchmark",
            )
        finally:
            conn.close()

    def test_neutral_counterfactual_tracking_records_missed_opportunity_without_policy_action(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE portfolio (
                    id TEXT,
                    config_id TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL
                )
                """
            )
            cursor.execute("INSERT INTO portfolio VALUES (?, ?)", ("p1", "cfg"))
            cursor.execute("INSERT INTO ticker_daily_pnl VALUES (?, ?, ?, ?)", ("p1", "2025-02-10", "ZZ", 1200.0))

            summary = neutral_counterfactual_tracking_summary(
                cursor,
                config_id="cfg",
                trading_date="2025-02-10",
                recommendations=[
                    {
                        "id": "rec-neutral",
                        "underlying_code": "ZZ",
                        "signal_snapshot": {
                            "technical": {
                                "signal": "Neutral",
                                "neutral_reason": "needs confirmation",
                                "missing_evidence": ["volume"],
                                "conflicting_factors": [],
                                "would_change_view_if": "breakout confirms",
                            },
                            "fundamental": {"signal": "Bullish", "confidence": 0.70},
                            "commodity_news": {"signal": "Bullish", "confidence": 0.65},
                        },
                    }
                ],
            )

            self.assertEqual(summary["observation_count"], 1)
            self.assertEqual(summary["missed_opportunity_count"], 1)
            self.assertGreater(summary["total_counterfactual_pnl"], 0)
            event = cursor.execute(
                "SELECT action_json FROM learning_event_log WHERE event_type = ?",
                ("neutral_counterfactual_tracking",),
            ).fetchone()
            self.assertIsNone(event)
        finally:
            conn.close()

    def test_neutral_counterfactual_tracking_uses_only_settled_forward_window(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT)")
            cursor.execute(
                """
                CREATE TABLE daily_settlement (
                    portfolio_id TEXT,
                    trading_date TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL
                )
                """
            )
            cursor.execute("INSERT INTO portfolio VALUES (?, ?)", ("p1", "cfg"))
            cursor.executemany(
                "INSERT INTO daily_settlement VALUES (?, ?)",
                [
                    ("p1", "2025-02-11"),
                    ("p1", "2025-02-12"),
                    ("p1", "2025-02-13"),
                    ("p1", "2025-02-14"),
                ],
            )
            cursor.executemany(
                "INSERT INTO ticker_daily_pnl VALUES (?, ?, ?, ?)",
                [
                    ("p1", "2025-02-11", "ZZ", 100.0),
                    ("p1", "2025-02-12", "ZZ", 200.0),
                    ("p1", "2025-02-13", "ZZ", 300.0),
                    ("p1", "2025-02-14", "ZZ", 5000.0),
                ],
            )

            summary = neutral_counterfactual_tracking_summary(
                cursor,
                cfg={"signal_quality": {"neutral_accountability": {"counterfactual_forward_days": 3}}},
                config_id="cfg",
                trading_date="2025-02-10",
                recommendations=[
                    {
                        "id": "rec-neutral",
                        "underlying_code": "ZZ",
                        "signal_snapshot": {
                            "technical": {"signal": "Neutral"},
                            "fundamental": {"signal": "Bullish", "confidence": 0.70},
                            "commodity_news": {"signal": "Bullish", "confidence": 0.65},
                        },
                    }
                ],
            )

            self.assertEqual(summary["forward_status"], "applied")
            self.assertEqual(summary["forward_window_dates"], ["2025-02-11", "2025-02-12", "2025-02-13"])
            self.assertEqual(summary["forward_observation_count"], 1)
            self.assertEqual(summary["forward_total_counterfactual_pnl"], 600.0)
            self.assertEqual(summary["forward_missed_opportunity_count"], 1)
        finally:
            conn.close()

    def test_neutral_forward_counterfactual_backfill_waits_for_future_settlements(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT)")
            cursor.execute("CREATE TABLE daily_settlement (portfolio_id TEXT, trading_date TEXT)")
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT,
                    config_id TEXT,
                    trading_date TEXT,
                    source_type TEXT,
                    underlying_code TEXT,
                    signal_snapshot TEXT,
                    signal_snapshot_artifact_path TEXT,
                    signal_snapshot_sha256 TEXT,
                    created_at TEXT
                )
                """
            )
            cursor.execute("INSERT INTO portfolio VALUES (?, ?)", ("p1", "cfg"))
            cursor.executemany(
                "INSERT INTO daily_settlement VALUES (?, ?)",
                [
                    ("p1", "2025-02-10"),
                    ("p1", "2025-02-11"),
                    ("p1", "2025-02-12"),
                    ("p1", "2025-02-13"),
                ],
            )
            cursor.executemany(
                "INSERT INTO ticker_daily_pnl VALUES (?, ?, ?, ?)",
                [
                    ("p1", "2025-02-11", "ZZ", 100.0),
                    ("p1", "2025-02-12", "ZZ", 200.0),
                    ("p1", "2025-02-13", "ZZ", 300.0),
                ],
            )
            cursor.execute(
                "INSERT INTO futures_recommendation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "rec-neutral",
                    "cfg",
                    "2025-02-10",
                    "strategy",
                    "ZZ",
                    json.dumps(
                        {
                            "technical": {"signal": "Neutral"},
                            "fundamental": {"signal": "Bullish", "confidence": 0.70},
                            "commodity_news": {"signal": "Bullish", "confidence": 0.65},
                        }
                    ),
                    None,
                    None,
                    "now",
                ),
            )

            pending = _backfill_neutral_forward_counterfactual_tracking(
                cursor,
                cfg={"signal_quality": {"neutral_accountability": {"counterfactual_forward_days": 3}}},
                config_id="cfg",
                trading_date="2025-02-12",
            )
            applied = _backfill_neutral_forward_counterfactual_tracking(
                cursor,
                cfg={"signal_quality": {"neutral_accountability": {"counterfactual_forward_days": 3}}},
                config_id="cfg",
                trading_date="2025-02-13",
            )

            self.assertEqual(pending["rows"], 0)
            self.assertEqual(applied["rows"], 1)
            row = cursor.execute(
                "SELECT evidence_json FROM learning_event_log WHERE event_type = ?",
                ("neutral_forward_counterfactual_tracking",),
            ).fetchone()
            evidence = json.loads(row["evidence_json"])
            self.assertEqual(evidence["forward_window_dates"], ["2025-02-11", "2025-02-12", "2025-02-13"])
            self.assertEqual(evidence["forward_total_counterfactual_pnl"], 600.0)
        finally:
            conn.close()

    def test_no_trade_opportunity_memory_backfills_counterfactual_only_after_settlement(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT)")
            cursor.execute("CREATE TABLE daily_settlement (portfolio_id TEXT, trading_date TEXT)")
            cursor.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT,
                    config_id TEXT,
                    reference_portfolio_id TEXT,
                    trading_date TEXT,
                    effective_trade_date TEXT,
                    source_type TEXT,
                    underlying_code TEXT,
                    contract_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    base_price REAL,
                    execution_price REAL,
                    open_price REAL,
                    prev_close_price REAL,
                    signal_snapshot TEXT,
                    signal_snapshot_artifact_path TEXT,
                    signal_snapshot_sha256 TEXT,
                    warning_message TEXT,
                    created_at TEXT
                )
                """
            )
            cursor.execute("INSERT INTO portfolio VALUES (?, ?)", ("p1", "cfg"))
            snapshot = {
                "technical": {
                    "signal": "Bullish",
                    "confidence": 0.7,
                    "setup_type": "breakout",
                    "horizon_class": "short",
                    "opportunity_type": "trend_continuation",
                    "opportunity_state": "tradeable_candidate",
                    "factor_focus": ["trend"],
                    "current_evidence_conflict": [],
                },
                "fundamental": {"signal": "Bullish", "confidence": 0.6},
                "commodity_news": {
                    "signal": "Neutral",
                    "confidence": 0.3,
                    "neutral_reason": "news impact needs price confirmation",
                    "missing_evidence": [],
                    "conflicting_factors": [],
                    "would_change_view_if": "price confirms upside event follow-through",
                    "neutral_opportunity_bucket": "watchlist_trigger",
                    "neutral_trigger_condition": "price confirms upside event follow-through",
                    "counterfactual_side": "long",
                    "neutral_watchlist_priority": "medium",
                    "metadata": {
                        "neutral_opportunity_contract": {
                            "bucket": "watchlist_trigger",
                            "trigger_condition": "price confirms upside event follow-through",
                            "counterfactual_side": "long",
                            "watchlist_priority": "medium",
                            "tracking_only": True,
                            "opportunity_state": "watch_for_trigger",
                            "trigger_valid": False,
                            "action_preference": "watch_for_trigger",
                        }
                    },
                },
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "BU",
                    "final_action": "wait",
                    "current_lots": 0,
                    "target_lots": 0,
                    "lots_delta": 0,
                    "reason_codes": "intraday_trigger_not_met",
                    "reason_codes": ["intraday_trigger_not_met"],
                    "learning_used": {},
                },
                "execution_result": {"no_trade_reason": "intraday_trigger_not_met"},
                "horizon_scope": {"decision_horizon": "short"},
                "trade_research_contracts": {
                    "technical": {
                        "opportunity_type": "trend_continuation",
                        "opportunity_state": "tradeable_candidate",
                        "factor_focus": ["trend"],
                        "current_evidence_conflict": [],
                    }
                },
            }
            cursor.executemany(
                "INSERT INTO daily_settlement VALUES (?, ?)",
                [("p1", "2025-03-04"), ("p1", "2025-03-05"), ("p1", "2025-03-06")],
            )
            cursor.executemany(
                """
                INSERT INTO futures_recommendation VALUES (?, 'cfg', 'p1', ?, ?, 'strategy', 'BU', 'bu2506',
                    ?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL, NULL, ?)
                """,
                [
                    ("rec-nt", "2025-03-03", "2025-03-03", "hold", 0, 3200.0, json.dumps(snapshot), "2025-03-03T09:00:00"),
                    ("rec-p1", "2025-03-04", "2025-03-04", "hold", 0, 3210.0, "{}", "2025-03-04T09:00:00"),
                    ("rec-p2", "2025-03-05", "2025-03-05", "hold", 0, 3220.0, "{}", "2025-03-05T09:00:00"),
                    ("rec-p3", "2025-03-06", "2025-03-06", "hold", 0, 3230.0, "{}", "2025-03-06T09:00:00"),
                ],
            )

            rows = _write_no_trade_opportunity_memory(
                cursor,
                cfg={"learning": {"no_trade_opportunity_memory": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-03",
                strategy_recommendations=[
                    {
                        "id": "rec-nt",
                        "config_id": "cfg",
                        "underlying_code": "BU",
                        "action": "hold",
                        "lots": 0,
                        "base_price": 3200.0,
                        "signal_snapshot": json.dumps(snapshot),
                    }
                ],
            )
            pending = _backfill_no_trade_opportunity_counterfactual_results(
                cursor,
                cfg={"learning": {"no_trade_opportunity_memory": {"enabled": True, "counterfactual_forward_days": [3]}}},
                config_id="cfg",
                trading_date="2025-03-05",
            )
            applied = _backfill_no_trade_opportunity_counterfactual_results(
                cursor,
                cfg={"learning": {"no_trade_opportunity_memory": {"enabled": True, "counterfactual_forward_days": [3]}}},
                config_id="cfg",
                trading_date="2025-03-06",
            )

            self.assertEqual(rows, 1)
            self.assertEqual(pending["status"], "no_ready_rows")
            self.assertEqual(applied["updated_rows"], 1)
            item = cursor.execute("SELECT * FROM no_trade_opportunity_memory").fetchone()
            results = json.loads(item["counterfactual_results_json"])
            payload = load_externalized_json(item["payload_json"])
            self.assertEqual(results[0]["horizon_days"], 3)
            self.assertEqual(item["classification"], "missed_opportunity")
            self.assertEqual(payload["neutral_opportunity_observations"][0]["bucket"], "watchlist_trigger")
            self.assertEqual(payload["no_trade_reason"], "intraday_trigger_not_met")
            self.assertEqual(payload["no_trade_reason_category"]["category"], "timing")
            self.assertEqual(payload["no_trade_reason_category"]["category_label"], "择时")
            self.assertEqual(payload[CONTRACT_KEY]["memory_type"], "no_trade_opportunity_memory")
            self.assertIn("position_impact_conditions", payload[CONTRACT_KEY])
            self.assertIn("not increase size", " ".join(payload[CONTRACT_KEY]["position_impact_conditions"]))
            self.assertIn("neutral_condition=commodity_news:watchlist_trigger", item["evidence_summary"])
            self.assertIn("no_trade_category=择时:timing", item["evidence_summary"])
            event_row = cursor.execute(
                "SELECT evidence_json FROM learning_event_log WHERE event_type = ?",
                ("no_trade_opportunity_memory",),
            ).fetchone()
            event_evidence = json.loads(event_row["evidence_json"])
            self.assertEqual(event_evidence["no_trade_reason_categories"], {"timing": 1})
        finally:
            conn.close()

    def test_limit_locked_skip_writes_timing_opportunity_memory(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            snapshot = {
                "technical": {
                    "signal": "Bullish",
                    "setup_type": "breakout_continuation",
                    "horizon_class": "short",
                },
                "fundamental": {"signal": "Bullish"},
                "commodity_news": {"signal": "Neutral"},
                "pm_internal_draft": {
                    "analyst_signal_combo": ["Bullish", "Bullish", "Neutral"],
                    "signal_direction": "long",
                    "decision_horizon": "short",
                    "market_regime": "trend",
                },
                "execution_translation": {
                    "market_rule_block": {
                        "limit_lock": {
                            "blocked": True,
                            "reason": "limit_locked_no_fill",
                            "side": "buy_like",
                            "execution_price": 3500.0,
                            "limit_price": 3500.0,
                            "limit_up": 3500.0,
                        }
                    }
                },
                "execution_result": {
                    "outcome": "skipped",
                    "status": "skipped",
                    "transaction_count": 0,
                    "no_trade_reason": "limit_locked_no_fill",
                },
            }

            rows = _write_no_trade_opportunity_memory(
                cursor,
                cfg={"learning": {"no_trade_opportunity_memory": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-10",
                strategy_recommendations=[
                    {
                        "id": "rec-limit",
                        "config_id": "cfg",
                        "underlying_code": "RB",
                        "action": "open_long",
                        "lots": 2,
                        "base_price": 3499.0,
                        "execution_price": 3500.0,
                        "signal_snapshot": json.dumps(snapshot),
                    }
                ],
            )

            self.assertEqual(rows, 1)
            item = cursor.execute("SELECT * FROM no_trade_opportunity_memory").fetchone()
            payload = load_externalized_json(item["payload_json"], item["payload_artifact_path"], item["payload_sha256"])
            contract = payload[CONTRACT_KEY]
            self.assertEqual(item["execution_reason"], "limit_locked_no_fill")
            self.assertEqual(item["candidate_lots"], 2)
            self.assertEqual(item["counterfactual_lots"], 1)
            self.assertEqual(payload["execution_no_trade_reason"], "limit_locked_no_fill")
            self.assertEqual(payload["no_trade_reason_category"]["category"], "execution")
            self.assertEqual(payload["no_trade_reason_category"]["category_label"], "执行")
            self.assertEqual(payload["limit_lock_audit"]["reason"], "limit_locked_no_fill")
            self.assertIn("execution_timing_case=limit_locked_no_fill", item["evidence_summary"])
            self.assertIn("no_trade_category=执行:execution", item["evidence_summary"])
            self.assertIn("limit_locked_no_fill timing_case", " ".join(contract["usable_memory"]))
            self.assertIn("No-trade category=execution", " ".join(contract["analysis_strategy_updates"]))
            self.assertIn("entry/exit timing research question", " ".join(contract["analysis_strategy_updates"]))
            self.assertIn("Do not chase at the limit price", " ".join(contract["trading_strategy_updates"]))
            self.assertIn("not increase size", " ".join(contract["position_impact_conditions"]))
        finally:
            conn.close()

    def test_tail_loss_sentinel_and_alpha_promotion_write_adaptive_policy(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT)")
            cursor.execute(
                """
                CREATE TABLE daily_settlement (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    current_account_equity REAL,
                    current_balance REAL,
                    created_at TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL,
                    new_position_pnl REAL,
                    position_type TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT,
                    config_id TEXT,
                    effective_trade_date TEXT,
                    source_type TEXT,
                    underlying_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    signal_snapshot TEXT,
                    signal_snapshot_artifact_path TEXT,
                    signal_snapshot_sha256 TEXT,
                    created_at TEXT
                )
                """
            )
            cursor.execute("INSERT INTO portfolio VALUES (?, ?)", ("p1", "cfg"))
            cursor.execute("INSERT INTO daily_settlement VALUES (?, ?, ?, ?, ?)", ("p1", "2025-03-07", 5000000.0, 4850000.0, "now"))
            cursor.execute("INSERT INTO ticker_daily_pnl VALUES (?, ?, ?, ?, ?, ?)", ("p1", "2025-03-07", "TA", -36000.0, -36000.0, "long"))
            snapshot = {
                "technical": {"signal": "Bullish", "setup_type": "breakout", "horizon_class": "short"},
                "pm_internal_draft": {"analyst_signal_combo": ["Bullish", "Neutral", "Neutral"], "decision_horizon": "short", "market_regime": "trend"},
            }
            cursor.execute(
                "INSERT INTO futures_recommendation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("rec-ta", "cfg", "2025-03-07", "strategy", "TA", "open_long", 1, json.dumps(snapshot), None, None, "now"),
            )
            cursor.execute(
                """
                INSERT INTO setup_type_performance (
                    id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                    sample_count, win_rate, net_pnl, avg_pnl, profit_factor,
                    confidence_score, last_updated, valid_until, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("tpl-alpha", "cfg", "BU", "long", "long_breakout_short", "short", "trend", 5, 0.8, 6000, 1200, 2.1, 0.85, "now", "2025-04-01", "{}"),
            )

            tail_rows = _write_tail_loss_sentinel_state(
                cursor,
                config_id="cfg",
                trading_date="2025-03-07",
                cfg={"learning": {"tail_loss_sentinel": {"enabled": True, "min_abs_loss": 25000, "valid_days": 5}}},
            )
            alpha_rows = _write_alpha_promotion_state(
                cursor,
                config_id="cfg",
                trading_date="2025-03-07",
                cfg={"learning": {"alpha_promotion": {"enabled": True, "min_sample_count": 5, "min_win_rate": 0.6, "min_net_pnl": 1000}}},
            )

            self.assertEqual(tail_rows, 1)
            self.assertEqual(alpha_rows, 1)
            rows = cursor.execute("SELECT policy_type, policy_action, payload_json FROM adaptive_policy_state ORDER BY policy_type").fetchall()
            self.assertEqual([(row["policy_type"], row["policy_action"]) for row in rows], [("alpha_promotion", "protect"), ("tail_loss_sentinel", "cap")])
            alpha_payload = load_externalized_json(rows[0]["payload_json"])
            tail_payload = load_externalized_json(rows[1]["payload_json"])
            self.assertEqual(alpha_payload[CONTRACT_KEY]["position_authority"], "pm_auditor_conditioned")
            self.assertEqual(tail_payload[CONTRACT_KEY]["position_authority"], "risk_reduction_conditioned")
        finally:
            conn.close()

    def test_alpha_setup_policy_state_promotes_and_caps_same_scope_profiles(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            now = "2025-03-10T00:00:00"
            profile_sql = """
                INSERT INTO alpha_setup_profile (
                    id, config_id, ticker, side, sector, horizon_class, market_regime,
                    setup_type, data_combo, scope_key, lifecycle_state, profile_state_hint,
                    sample_count, trade_count, no_trade_count, win_count, loss_count,
                    gross_profit, gross_loss, net_pnl, total_commission, profit_factor,
                    win_rate, max_loss, avg_holding_days, confidence_score,
                    max_position_impact, last_sample_date, created_at, updated_at,
                    valid_until, active, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """
            cursor.execute(
                profile_sql,
                (
                    "prof-good", "cfg", "BU", "long", "energy", "short", "trend",
                    "trend_breakout_setup", "pandaai_price+finoview_inventory",
                    "BU|long|short|trend|trend_breakout_setup|pandaai_price",
                    "protected", "controlled_probe_or_hold",
                    4, 4, 0, 3, 1, 6000, -1200, 4800, 120, 5.0, 0.75,
                    -1200, 2.0, 0.72, 0.03, "2025-03-10", now, now, "2025-03-30",
                    json.dumps({}),
                ),
            )
            cursor.execute(
                profile_sql,
                (
                    "prof-bad", "cfg", "ZN", "short", "nonferrous", "short", "range",
                    "news_event_setup", "pandaai_price+news",
                    "ZN|short|short|range|news_event_setup|news",
                    "capped", "cap_reduce_or_revalidate",
                    2, 2, 0, 0, 2, 0, -9000, -9000, 80, 0.0, 0.0,
                    -7000, 1.0, 0.60, 0.01, "2025-03-10", now, now, "2025-03-30",
                    json.dumps({}),
                ),
            )

            result = _write_alpha_setup_policy_state(
                cursor,
                cfg={"learning": {"alpha_setup_policy_state": {"enabled": True, "valid_days": 7}}},
                config_id="cfg",
                trading_date="2025-03-10",
            )

            self.assertEqual(result["rows"], 2)
            rows = cursor.execute(
                """
                SELECT ticker, side, policy_type, policy_action, multiplier, payload_json
                FROM adaptive_policy_state
                WHERE config_id = ?
                ORDER BY ticker
                """,
                ("cfg",),
            ).fetchall()
            self.assertEqual(len(rows), 2)
            by_ticker = {row["ticker"]: row for row in rows}
            self.assertEqual(by_ticker["BU"]["policy_type"], "learning_mechanism:alpha_setup_ev")
            self.assertEqual(by_ticker["BU"]["policy_action"], "protect")
            self.assertEqual(by_ticker["ZN"]["policy_action"], "cap")
            self.assertLess(by_ticker["ZN"]["multiplier"], 1.0)
            payload = load_externalized_json(by_ticker["BU"]["payload_json"])
            self.assertEqual(payload["alpha_setup_scope"]["setup_type"], "trend_breakout_setup")
            self.assertIn(
                "future results are used only after settlement/backfill",
                payload[CONTRACT_KEY]["anti_overfit_guardrails"],
            )
            self.assertIn("today's signal", " ".join(payload[CONTRACT_KEY]["pm_action_conditions"]).lower())
        finally:
            conn.close()

    def test_researcher_writes_execution_action_value_from_trader_feedback(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
            cursor.execute(
                """
                CREATE TABLE portfolio (
                    id TEXT PRIMARY KEY,
                    config_id TEXT
                )
                """
            )
            cursor.execute("INSERT INTO portfolio(id, config_id) VALUES ('pf1', 'cfg')")
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL,
                    commission REAL,
                    holding_pnl REAL,
                    new_position_pnl REAL,
                    close_pnl REAL,
                    lots INTEGER
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO ticker_daily_pnl (
                    portfolio_id, trading_date, ticker, daily_pnl, commission,
                    holding_pnl, new_position_pnl, close_pnl, lots
                ) VALUES ('pf1', '2025-03-19', 'EB', -1200, 12, 0, -1200, 0, 1)
                """
            )
            snapshot = {
                "technical": {
                    "signal": "Bearish",
                    "confidence": 0.68,
                    "evidence_role": "entry_timing",
                    "entry_trigger": "opening range breakdown",
                    "invalidation": "recover above VWAP",
                    "metadata": {
                        "technical_context": {"market_regime": "trend"},
                        "action_evidence_contract": {
                            "contract_version": "agentquant.action_evidence.v1",
                            "analyst": "technical",
                            "learning_scope": {
                                "setup_family": "trend_breakout",
                                "sector_setup_alignment": "preferred",
                                "market_regime": "trend",
                            },
                            "execution": {
                                "trigger_source": "technical",
                                "execution_focus": "opening_range_breakdown",
                            },
                        },
                    },
                },
                "fundamental": {"signal": "Bullish", "evidence_role": "background"},
                "commodity_news": {"signal": "Bullish", "evidence_role": "catalyst"},
                "pm_internal_draft": {
                    "decision_horizon": "short",
                    "market_regime": "trend",
                    "pm_decision_layer": "exploration_probe",
                    "opportunity_state": "tradeable_candidate",
                    "opportunity_scorecard": {
                        "short": {
                            "final_state": "tradeable_candidate",
                            "max_setup_quality": 0.62,
                        }
                    },
                    "execution_contract": {
                        "execution_profile": "breakout",
                        "trigger_source": "technical_breakout",
                        "entry_trigger": "opening range breakdown",
                        "invalidation": "recover above VWAP",
                    },
                },
                "pm_research_contract_summary": {
                    "contract_version": "agentquant.research.v1",
                    "opportunity_states": ["tradeable_candidate"],
                    "opportunity_states": ["probe_candidate"],
                },
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "EB",
                    "contract_type": "strategy",
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": -1,
                    "lots_delta": -1,
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "opportunity_state": "tradeable_candidate",
                    "execution_contract": {
                        "execution_profile": "breakout",
                        "trigger_source": "technical_breakout",
                    },
                },
                "phase2_execution": {
                    "status": "translated_with_intraday_basis",
                    "execution_contract": {
                        "execution_profile": "breakout",
                        "trigger_source": "technical_breakout",
                    },
                    "intraday_selection": {
                        "decision": "execute",
                        "reason": "intraday_trigger_confirmed",
                        "trigger_checked": True,
                        "trigger_passed": True,
                        "price_chase_check": "within_limit",
                        "execution_failure_reason": None,
                        "missed_opportunity_flag": False,
                        "features": {"execution_profile": "breakout"},
                    },
                    "setup_execution_learning": {
                        "phase2_status": "executed",
                        "reason_family": "executed_or_hold",
                    },
                },
                "execution_result": {"status": "executed", "outcome": "filled"},
            }

            summary = write_alpha_setup_profiles(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-19",
                strategy_recommendations=[
                    {
                        "id": "rec-eb-1",
                        "underlying_code": "EB",
                        "action": "open_short",
                        "lots": 1,
                        "status": "executed",
                        "signal_snapshot": json.dumps(snapshot),
                    }
                ],
                transactions_by_recommendation={"rec-eb-1": [{"lots": 1, "daily_pnl": -1200, "commission": 12}]},
            )

            self.assertEqual(summary["rows"], 2)
            samples = cursor.execute(
                """
                SELECT source_type, action_taken, setup_type, payload_json
                FROM alpha_setup_sample
                WHERE config_id='cfg'
                ORDER BY source_type
                """
            ).fetchall()
            self.assertEqual({row["source_type"] for row in samples}, {"execution", "trade"})
            trade_sample = next(row for row in samples if row["source_type"] == "trade")
            trade_payload = load_externalized_json(trade_sample["payload_json"])
            self.assertIn("probe_candidate", trade_payload["evidence"].get("opportunity_states") or [])
            self.assertNotIn("pm_internal_draft", trade_payload["evidence"])
            self.assertEqual(trade_payload["evidence"]["final_action_contract"]["target_lots"], -1)
            execution_sample = next(row for row in samples if row["source_type"] == "execution")
            self.assertEqual(execution_sample["action_taken"], "execution_intraday_trigger_confirmed")
            self.assertTrue(execution_sample["setup_type"].startswith("execution_breakout"))
            payload = load_externalized_json(execution_sample["payload_json"])
            feedback = payload["result"]["execution_feedback"]
            self.assertTrue(feedback["trigger_checked"])
            self.assertTrue(feedback["trigger_passed"])
            self.assertEqual(feedback["price_chase_check"], "within_limit")
            self.assertIn("technical:setup_family:trend_breakout", payload["data_combo"])
            self.assertIn("analyst_action_evidence_contracts", payload["evidence"])
            self.assertNotIn("pm_internal_draft", payload["evidence"])
            self.assertEqual(payload["evidence"]["final_action_contract"]["target_lots"], -1)

            action_values = cursor.execute(
                """
                SELECT action_name, canonical_action_family, scope_key, reward_sum, sample_count, payload_json
                FROM alpha_setup_action_value
                WHERE config_id='cfg'
                ORDER BY action_name
                """
            ).fetchall()
            by_action = {row["action_name"]: row for row in action_values}
            self.assertIn("add_or_open", by_action)
            self.assertIn("execution", by_action)
            self.assertEqual(by_action["add_or_open"]["canonical_action_family"], "open_add_new_risk")
            self.assertIn("execution_breakout_setup", by_action["execution"]["scope_key"])
            self.assertLess(by_action["execution"]["reward_sum"], 0)
            self.assertEqual(by_action["execution"]["sample_count"], 1)
        finally:
            conn.close()

    def test_researcher_alpha_setup_profiles_require_final_action_contract(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
            cursor.execute(
                """
                CREATE TABLE portfolio (
                    id TEXT PRIMARY KEY,
                    config_id TEXT
                )
                """
            )
            cursor.execute("INSERT INTO portfolio(id, config_id) VALUES ('pf1', 'cfg')")
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL,
                    commission REAL,
                    holding_pnl REAL,
                    new_position_pnl REAL,
                    close_pnl REAL,
                    lots INTEGER
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO ticker_daily_pnl (
                    portfolio_id, trading_date, ticker, daily_pnl, commission,
                    holding_pnl, new_position_pnl, close_pnl, lots
                ) VALUES ('pf1', '2025-03-20', 'ZN', 1800, 10, 0, 1800, 0, 1)
                """
            )
            snapshot = {
                "technical": {
                    "signal": "Bearish",
                    "metadata": {
                        "action_evidence_contract": {
                            "learning_scope": {"setup_family": "trend_breakout"}
                        }
                    },
                },
                "pm_internal_draft": {
                    "target_lots": -1,
                    "target_position_ratio": -0.01,
                    "execution_contract": {"execution_profile": "breakout"},
                },
                "execution_result": {"status": "executed", "outcome": "filled"},
            }

            summary = write_alpha_setup_profiles(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-20",
                strategy_recommendations=[
                    {
                        "id": "rec-legacy-zN",
                        "underlying_code": "ZN",
                        "action": "open_short",
                        "lots": 1,
                        "status": "executed",
                        "signal_snapshot": json.dumps(snapshot),
                    }
                ],
                transactions_by_recommendation={"rec-legacy-zN": [{"lots": 1, "daily_pnl": 1800, "commission": 10}]},
            )

            self.assertEqual(summary["rows"], 0)
            self.assertEqual(summary["status"], "no_samples")
            self.assertIsNone(
                cursor.execute(
                    """
                    SELECT id
                    FROM alpha_setup_sample
                    WHERE config_id='cfg'
                    """
                ).fetchone()
            )
            self.assertIsNone(
                cursor.execute(
                    """
                    SELECT id
                    FROM alpha_setup_action_value
                    WHERE config_id='cfg'
                    """
                ).fetchone()
            )
        finally:
            conn.close()

    def test_researcher_alpha_setup_action_comes_from_final_contract_lots(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
            cursor.execute(
                """
                CREATE TABLE portfolio (
                    id TEXT PRIMARY KEY,
                    config_id TEXT
                )
                """
            )
            cursor.execute("INSERT INTO portfolio(id, config_id) VALUES ('pf1', 'cfg')")
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL,
                    commission REAL,
                    holding_pnl REAL,
                    new_position_pnl REAL,
                    close_pnl REAL,
                    lots INTEGER
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO ticker_daily_pnl (
                    portfolio_id, trading_date, ticker, daily_pnl, commission,
                    holding_pnl, new_position_pnl, close_pnl, lots
                ) VALUES ('pf1', '2025-03-21', 'BU', 800, 8, 800, 0, 0, 1)
                """
            )
            snapshot = {
                "technical": {
                    "signal": "Bearish",
                    "metadata": {
                        "action_evidence_contract": {
                            "learning_scope": {
                                "setup_family": "fundamental_timing",
                                "market_regime": "range",
                            }
                        }
                    },
                },
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "BU",
                    "contract_type": "strategy",
                    "final_action": "hold",
                    "current_lots": -1,
                    "target_lots": -1,
                    "lots_delta": 0,
                    "horizon_class": "short",
                    "market_regime": "range",
                    "setup_type": "fundamental_timing_setup",
                    "opportunity_state": "tradeable_candidate",
                },
                "execution_result": {"status": "held", "outcome": "not_filled"},
            }

            summary = write_alpha_setup_profiles(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-21",
                strategy_recommendations=[
                    {
                        "id": "rec-bu-hold",
                        "underlying_code": "BU",
                        "action": "open_short",
                        "lots": 99,
                        "status": "held",
                        "signal_snapshot": json.dumps(snapshot),
                    }
                ],
                transactions_by_recommendation={"rec-bu-hold": []},
            )

            self.assertGreaterEqual(summary["rows"], 1)
            sample = cursor.execute(
                """
                SELECT action_taken, target_lots, payload_json
                FROM alpha_setup_sample
                WHERE config_id='cfg'
                  AND recommendation_id='rec-bu-hold'
                  AND source_type != 'execution'
                """
            ).fetchone()
            self.assertEqual(sample["action_taken"], "hold")
            self.assertEqual(sample["target_lots"], -1)
            payload = load_externalized_json(sample["payload_json"])
            self.assertEqual(payload["pm_action"], "hold")
            action_values = cursor.execute(
                """
                SELECT action_name, payload_json
                FROM alpha_setup_action_value
                WHERE config_id='cfg'
                """
            ).fetchall()
            self.assertNotIn(
                "open",
                {row["action_name"] for row in action_values},
            )
            self.assertIn(
                "observe",
                {row["action_name"] for row in action_values},
            )
        finally:
            conn.close()

    def test_researcher_execution_learning_does_not_use_pm_internal_draft_execution_profile(self):
        snapshot = {
            "pm_internal_draft": {
                "execution_contract": {
                    "execution_profile": "breakout",
                    "trigger_source": "stale_pm_internal_draft",
                }
            },
            "phase2_execution": {
                "status": "translated_with_intraday_basis",
                "intraday_selection": {
                    "decision": "execute",
                    "reason": "intraday_trigger_confirmed",
                    "trigger_checked": True,
                    "trigger_passed": True,
                },
            },
            "execution_result": {"status": "executed", "outcome": "filled"},
        }

        feedback = _execution_learning_from_snapshot(snapshot)

        self.assertEqual(feedback["action_taken"], "execution_intraday_trigger_confirmed")
        self.assertEqual(feedback["execution_profile"], "unknown")
        self.assertEqual(feedback["execution_contract"], {})

    def test_researcher_execution_learning_does_not_fallback_to_pre_open_final_contract(self):
        snapshot = {
            "pm_internal_draft": {
                "final_action_contract": {
                    "execution_contract": {
                        "execution_profile": "vwap_confirmed",
                        "trigger_source": "stale_pm_draft",
                    }
                }
            },
            "phase2_execution": {
                "status": "translated_with_intraday_basis",
                "intraday_selection": {
                    "decision": "execute",
                    "reason": "intraday_trigger_confirmed",
                    "trigger_checked": True,
                    "trigger_passed": True,
                },
            },
            "execution_result": {"status": "executed", "outcome": "filled"},
        }

        feedback = _execution_learning_from_snapshot(snapshot)

        self.assertEqual(feedback["execution_profile"], "unknown")
        self.assertEqual(feedback["execution_contract"], {})

    def test_counterfactual_no_trade_results_feed_alpha_setup_as_prior_only(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO no_trade_opportunity_memory (
                    id, config_id, trading_date, ticker, side, sector, setup_type,
                    signal_combo, horizon_class, market_regime, opportunity_type,
                    opportunity_state, candidate_lots, counterfactual_lots, counterfactual_entry_price,
                    pm_reason, auditor_reason, execution_reason, evidence_summary,
                    status, classification, counterfactual_results_json, payload_json,
                    created_at, last_reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "counterfactual-bu-1",
                    "cfg",
                    "2025-03-03",
                    "BU",
                    "long",
                    "energy",
                    "long_breakout_short",
                    json.dumps(["Bullish", "Neutral", "Neutral"]),
                    "short",
                    "trend",
                    "probe",
                    "tradeable_candidate",
                    1,
                    1,
                    3500.0,
                    "intraday_trigger_not_met",
                    "",
                    "intraday_trigger_not_met",
                    "missed BU breakout probe",
                    "closed",
                    "missed_opportunity",
                    json.dumps([{"horizon_days": 3, "counterfactual_pnl": 2100.0}]),
                    json.dumps({"source": "test"}),
                    "now",
                    "now",
                ),
            )
            cursor.execute(
                """
                INSERT INTO no_trade_opportunity_memory (
                    id, config_id, trading_date, ticker, side, sector, setup_type,
                    signal_combo, horizon_class, market_regime, opportunity_type,
                    opportunity_state, candidate_lots, counterfactual_lots, counterfactual_entry_price,
                    pm_reason, auditor_reason, execution_reason, evidence_summary,
                    status, classification, counterfactual_results_json, payload_json,
                    created_at, last_reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "counterfactual-bu-same-day",
                    "cfg",
                    "2025-03-10",
                    "BU",
                    "long",
                    "energy",
                    "long_breakout_short",
                    json.dumps(["Bullish"]),
                    "short",
                    "trend",
                    "probe",
                    "tradeable_candidate",
                    1,
                    1,
                    3500.0,
                    "same day should not be visible",
                    "",
                    "",
                    "future boundary",
                    "closed",
                    "missed_opportunity",
                    json.dumps([{"horizon_days": 3, "counterfactual_pnl": 9999.0}]),
                    json.dumps({}),
                    "now",
                    "now",
                ),
            )

            summary = write_alpha_setup_profiles(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-10",
                strategy_recommendations=[],
            )

            self.assertEqual(summary["counterfactual_no_trade_alpha_setup"]["rows"], 1)
            sample = cursor.execute(
                """
                SELECT trading_date, source_type, action_taken, net_pnl, payload_json
                FROM alpha_setup_sample
                WHERE config_id='cfg'
                """
            ).fetchone()
            self.assertEqual(sample["trading_date"], "2025-03-10")
            self.assertEqual(sample["source_type"], "counterfactual_missed_alpha")
            self.assertEqual(sample["action_taken"], "open_long")
            self.assertEqual(sample["net_pnl"], 2100.0)
            payload = load_externalized_json(sample["payload_json"])
            self.assertEqual(payload["result"]["sample_date_policy"], "review_date_not_original_opportunity_date")
            self.assertEqual(payload["evidence"]["original_opportunity_date"], "2025-03-03")

            action_value = cursor.execute(
                """
                SELECT action_name, reward_sum, reward_mean, payload_json
                FROM alpha_setup_action_value
                WHERE config_id='cfg'
                """
            ).fetchone()
            self.assertEqual(action_value["action_name"], "open")
            self.assertAlmostEqual(action_value["reward_sum"], 735.0)
            av_payload = load_externalized_json(action_value["payload_json"])
            self.assertTrue(av_payload["counterfactual_prior_only"])
            self.assertEqual(av_payload["counterfactual_reward_count"], 1)
            self.assertEqual(av_payload["real_trade_reward_count"], 0)

            second_summary = write_alpha_setup_profiles(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-10",
                strategy_recommendations=[],
            )
            self.assertEqual(second_summary["counterfactual_no_trade_alpha_setup"]["rows"], 0)
            sample_count = cursor.execute(
                "SELECT COUNT(*) AS cnt FROM alpha_setup_sample WHERE source_type LIKE 'counterfactual_%'"
            ).fetchone()["cnt"]
            self.assertEqual(sample_count, 1)
        finally:
            conn.close()

    def test_alpha_setup_action_value_writes_candidate_preferences_from_real_rewards(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
            cursor.execute(
                """
                CREATE TABLE portfolio (
                    id TEXT PRIMARY KEY,
                    config_id TEXT
                )
                """
            )
            cursor.execute("INSERT INTO portfolio(id, config_id) VALUES ('pf1', 'cfg')")
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL,
                    commission REAL,
                    holding_pnl REAL,
                    new_position_pnl REAL,
                    close_pnl REAL,
                    lots INTEGER
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO ticker_daily_pnl (
                    portfolio_id, trading_date, ticker, daily_pnl, commission,
                    holding_pnl, new_position_pnl, close_pnl, lots
                ) VALUES ('pf1', '2025-03-10', 'P', 2200, 10, 2200, 0, 0, 1)
                """
            )
            snapshot = {
                "technical": {
                    "signal": "Bullish",
                    "confidence": 0.62,
                    "evidence_role": "entry_timing",
                    "entry_trigger": "opening range breakout",
                    "invalidation": "break below VWAP",
                    "metadata": {
                        "technical_context": {"market_regime": "range"},
                        "action_evidence_contract": {
                            "contract_version": "agentquant.action_evidence.v1",
                            "analyst": "technical",
                            "learning_scope": {
                                "setup_family": "trend_breakout",
                                "market_regime": "range",
                            },
                            "execution": {"trigger_source": "technical"},
                        },
                    },
                },
                "fundamental": {"signal": "Bullish", "evidence_role": "background"},
                "commodity_news": {"signal": "Neutral", "evidence_role": "risk"},
                "pm_internal_draft": {
                    "decision_horizon": "short",
                    "market_regime": "range",
                    "pm_decision_layer": "exploration_probe",
                    "opportunity_state": "tradeable_candidate",
                    "opportunity_scorecard": {
                        "long": {
                            "final_state": "tradeable_candidate",
                            "max_setup_quality": 0.62,
                        }
                    },
                    "execution_contract": {
                        "execution_profile": "breakout",
                        "trigger_source": "technical_breakout",
                    },
                },
                "phase2_execution": {
                    "status": "translated_with_intraday_basis",
                    "intraday_selection": {
                        "decision": "execute",
                        "reason": "intraday_trigger_confirmed",
                        "trigger_checked": True,
                        "trigger_passed": True,
                    },
                },
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "P",
                    "contract_type": "strategy",
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": 1,
                    "lots_delta": 1,
                    "horizon_class": "short",
                    "market_regime": "range",
                    "setup_type": "trend_breakout_setup",
                    "opportunity_state": "tradeable_candidate",
                },
                "execution_result": {"status": "executed", "outcome": "filled"},
            }

            write_alpha_setup_profiles(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-10",
                strategy_recommendations=[
                    {
                        "id": "rec-p-1",
                        "underlying_code": "P",
                        "action": "open_long",
                        "lots": 1,
                        "status": "executed",
                        "signal_snapshot": json.dumps(snapshot),
                    }
                ],
                transactions_by_recommendation={"rec-p-1": [{"lots": 1, "daily_pnl": 2200, "commission": 10}]},
            )

            row = cursor.execute(
                """
                SELECT action_name, canonical_action_family, action_preference, payload_json
                FROM alpha_setup_action_value
                WHERE config_id='cfg' AND canonical_action_family='open_add_new_risk'
                """
            ).fetchone()
            self.assertEqual(row["action_name"], "add_or_open")
            self.assertEqual(row["canonical_action_family"], "open_add_new_risk")
            self.assertEqual(row["action_preference"], "positive_candidate_open")
            payload = load_externalized_json(row["payload_json"])
            self.assertEqual(payload["action_preference"], "positive_candidate_open")
            self.assertEqual(payload["canonical_action_family"], "open_add_new_risk")
            self.assertEqual(payload["amplification_scope_quality"], "exact_real_state")
            self.assertEqual(payload["reward_source"], "real_trade")
            self.assertEqual(payload["sample_source"], "real_trade")
            self.assertEqual(payload["exact_state_real_trade_sample_count"], 1)
        finally:
            conn.close()

    def test_alpha_setup_writes_product_learning_performance_key(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
            sample = {
                "ticker": "EB",
                "side": "short",
                "sector": "chemical",
                "horizon_class": "short",
                "market_regime": "range",
                "setup_type": "trend_breakout_setup",
                "data_combo": "technical:used|fundamental:used|news:fresh",
                "recommendation_id": "rec-eb-rank",
                "action_taken": "open_short",
                "target_lots": -2,
                "current_lots": 0,
                "executed_lots": 2,
                "net_pnl": 3200.0,
                "commission": 20.0,
                "source_type": "trade",
                "opportunity_state": "tradeable_candidate",
                "outcome_label": "profit",
                "evidence": {
                    "analyst_payloads": {
                        "technical": {
                            "signal": "Bearish",
                            "entry_trigger": "opening range breakdown",
                        }
                    },
                    "final_action_contract": {
                        "final_action": "open_real",
                        "current_lots": 0,
                        "target_lots": -2,
                        "lots_delta": -2,
                        "authority_type": "real_budget_entry",
                        "entry_trigger": "opening range breakdown",
                        "capital_deployment": {
                            "selected_for_capital_deployment": True,
                            "capital_allocation_reason": "ranked_deployable_candidate",
                        },
                        "evidence_used": {
                            "opportunity_rank": 1,
                            "opportunity_score": 0.81,
                        },
                    },
                },
                "result": {"pnl_source": "ticker_daily_pnl"},
            }

            result = upsert_alpha_setup_sample_and_profile(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-20",
                sample=sample,
            )

            self.assertEqual(result["rows"], 1)
            profile = cursor.execute(
                "SELECT payload_json FROM alpha_setup_profile WHERE config_id='cfg'"
            ).fetchone()
            profile_payload = load_externalized_json(profile["payload_json"])
            key = profile_payload["product_learning_performance_key"]
            self.assertEqual(key["ticker"], "EB")
            self.assertEqual(key["side"], "short")
            self.assertEqual(key["setup_type"], "trend_breakout_setup")
            self.assertEqual(key["trigger_key"], "opening_range_breakdown")
            self.assertEqual(key["deployment_outcome"]["deployment_tier"], "capital_deployed")
            self.assertEqual(key["deployment_outcome"]["opportunity_rank"], 1)
            self.assertEqual(key["entry_quality_outcome"]["entry_quality_verdict"], "entry_quality_supported")
            self.assertEqual(key["entry_quality_outcome"]["trigger_quality_verdict"], "trigger_quality_supported")
            self.assertEqual(
                key["entry_quality_outcome"]["trigger_confirmation_adjustment"],
                "standard_confirmation_supported",
            )
            self.assertFalse(key["entry_quality_outcome"]["loss_episode"])
            self.assertTrue(key["not_trade_authority"])
            self.assertTrue(key["future_only"])

            action_value = cursor.execute(
                "SELECT payload_json FROM alpha_setup_action_value WHERE config_id='cfg'"
            ).fetchone()
            action_value_payload = load_externalized_json(action_value["payload_json"])
            self.assertEqual(
                action_value_payload["product_learning_performance_key"]["performance_scope_key"],
                key["performance_scope_key"],
            )
            self.assertEqual(
                action_value_payload["entry_quality_outcome"]["entry_quality_verdict"],
                "entry_quality_supported",
            )
        finally:
            conn.close()

    def test_alpha_setup_loss_episode_writes_entry_quality_outcome(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
            sample = {
                "ticker": "TA",
                "side": "long",
                "sector": "chemical",
                "horizon_class": "short",
                "market_regime": "range",
                "setup_type": "pullback_support_setup",
                "data_combo": "technical:used|fundamental:weak|news:none",
                "recommendation_id": "rec-ta-loss",
                "action_taken": "open_long",
                "target_lots": 2,
                "current_lots": 0,
                "executed_lots": 2,
                "net_pnl": -2600.0,
                "commission": 20.0,
                "source_type": "trade",
                "opportunity_state": "tradeable_candidate",
                "outcome_label": "loss",
                "evidence": {
                    "analyst_payloads": {
                        "technical": {
                            "signal": "Bullish",
                            "entry_trigger": "vwap pullback support",
                        }
                    },
                    "final_action_contract": {
                        "final_action": "open_probe",
                        "current_lots": 0,
                        "target_lots": 2,
                        "lots_delta": 2,
                        "authority_type": "exploration_probe",
                        "entry_trigger": "vwap pullback support",
                        "capital_deployment": {
                            "selected_for_capital_deployment": False,
                            "capital_allocation_reason": "ranked_probe_candidate",
                        },
                        "evidence_used": {
                            "opportunity_rank": 1,
                            "opportunity_score": 0.66,
                        },
                    },
                },
                "result": {"pnl_source": "ticker_daily_pnl"},
            }

            result = upsert_alpha_setup_sample_and_profile(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-21",
                sample=sample,
            )

            self.assertEqual(result["rows"], 1)
            row = cursor.execute(
                "SELECT payload_json FROM alpha_setup_action_value WHERE config_id='cfg'"
            ).fetchone()
            payload = load_externalized_json(row["payload_json"])
            outcome = payload["entry_quality_outcome"]
            self.assertEqual(outcome["entry_quality_verdict"], "entry_tail_loss_revalidate")
            self.assertEqual(outcome["trigger_quality_verdict"], "trigger_tail_loss_revalidate")
            self.assertEqual(outcome["trigger_confirmation_adjustment"], "strict_confirmation_required")
            self.assertTrue(outcome["loss_episode"])
            self.assertTrue(outcome["tail_loss_episode"])
            self.assertEqual(outcome["trigger_key"], "vwap_pullback_support")
            self.assertIn("capital_priority_score", outcome["affects"])
            self.assertTrue(outcome["future_only"])
            self.assertEqual(
                payload["product_learning_performance_key"]["entry_quality_outcome"]["entry_quality_verdict"],
                "entry_tail_loss_revalidate",
            )
        finally:
            conn.close()

    def test_partial_real_loss_exit_writes_protective_action_preference(self):
        preference = _action_preference_from_stats(
            action_name="exit",
            reward_mean=-3917.83,
            reward_sum=-3917.83,
            win_rate=0.0,
            real_trade_reward_count=1,
            amplification_scope_quality="partial_real_state",
            loss_reward_count=1,
            tail_loss_count=1,
            worst_reward=-3917.83,
        )

        self.assertEqual(preference, "tail_loss_protect")

    def test_partial_real_positive_exit_writes_exit_action_preference(self):
        preference = _action_preference_from_stats(
            action_name="exit",
            reward_mean=235.0,
            reward_sum=235.0,
            win_rate=1.0,
            real_trade_reward_count=1,
            amplification_scope_quality="partial_real_state",
            loss_reward_count=0,
            tail_loss_count=0,
            worst_reward=235.0,
        )

        self.assertEqual(preference, "positive_candidate_exit")

    def test_partial_real_positive_execution_writes_execution_action_preference(self):
        preference = _action_preference_from_stats(
            action_name="execution",
            reward_mean=235.0,
            reward_sum=235.0,
            win_rate=1.0,
            real_trade_reward_count=1,
            amplification_scope_quality="partial_real_state",
            loss_reward_count=0,
            tail_loss_count=0,
            worst_reward=235.0,
        )

        self.assertEqual(preference, "positive_candidate_execution")

    def test_non_real_prior_does_not_write_action_preference(self):
        preference = _action_preference_from_stats(
            action_name="open",
            reward_mean=80.0,
            reward_sum=80.0,
            win_rate=1.0,
            real_trade_reward_count=0,
            amplification_scope_quality="similar_sql_prior",
            loss_reward_count=0,
            tail_loss_count=0,
            worst_reward=80.0,
        )

        self.assertEqual(preference, "")

    def test_alpha_setup_partial_real_positive_exit_and_execution_do_not_write_weak_prior(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cfg = {"learning": {"alpha_setup_profile": {"enabled": True}}}
            base_sample = {
                "ticker": "SR",
                "side": "long",
                "sector": "agriculture",
                "horizon_class": "flat",
                "market_regime": "unknown",
                "data_combo": "technical+fundamental+news",
                "source_type": "trade",
                "pm_action": "hold",
                "auditor_decision": "allow",
                "trader_status": "executed",
                "current_lots": 10,
                "target_lots": 5,
                "executed_lots": 5,
                "net_pnl": 250.0,
                "commission": 15.0,
                "holding_days": 1,
                "outcome_label": "win",
                "setup_quality_score": 0.7,
                "opportunity_state": "tradeable_candidate",
                "evidence": {"reason_codes": ["profit_protection_exit"]},
                "result": {"reward_source": "real_trade"},
            }
            upsert_alpha_setup_sample_and_profile(
                cursor,
                cfg=cfg,
                config_id="cfg",
                trading_date="2025-03-07",
                sample={
                    **base_sample,
                    "setup_type": "fundamental_timing_setup",
                    "recommendation_id": "rec-sr-exit",
                    "action_taken": "close_long",
                },
            )
            upsert_alpha_setup_sample_and_profile(
                cursor,
                cfg=cfg,
                config_id="cfg",
                trading_date="2025-03-07",
                sample={
                    **base_sample,
                    "setup_type": "execution_exit_immediate_setup",
                    "recommendation_id": "rec-sr-execution",
                    "action_taken": "execution_exit_immediate",
                },
            )

            rows = cursor.execute(
                """
                SELECT setup_type, action_name, action_preference, payload_json
                FROM alpha_setup_action_value
                WHERE config_id='cfg' AND ticker='SR'
                ORDER BY action_name
                """
            ).fetchall()
            self.assertEqual(len(rows), 2)
            observed = {}
            for row in rows:
                payload = load_externalized_json(row["payload_json"])
                observed[row["action_name"]] = (row["action_preference"], payload)

            exit_hint, exit_payload = observed["exit"]
            self.assertEqual(exit_hint, "positive_candidate_exit")
            self.assertEqual(exit_payload["action_preference"], "positive_candidate_exit")
            self.assertEqual(exit_payload["amplification_scope_quality"], "partial_real_state")
            self.assertEqual(exit_payload["real_trade_reward_count"], 1)
            self.assertEqual(exit_payload["reward_source"], "real_trade")
            self.assertEqual(exit_payload["action_value_lane"], "exit")
            self.assertIn("portfolio_manager", exit_payload["usable_by"])
            self.assertIn("protect_profit", exit_payload["allowed_effects"])
            self.assertIn("open_amplification", exit_payload["forbidden_effects"])
            self.assertIn("signal_calibration", exit_payload)
            self.assertIn("analysis_team", exit_payload["signal_calibration"]["usable_by"])
            self.assertIn("trade_authority", exit_payload["signal_calibration"]["forbidden_effects"])

            execution_hint, execution_payload = observed["execution"]
            self.assertEqual(execution_hint, "positive_candidate_execution")
            self.assertEqual(execution_payload["action_preference"], "positive_candidate_execution")
            self.assertEqual(execution_payload["amplification_scope_quality"], "partial_real_state")
            self.assertEqual(execution_payload["real_trade_reward_count"], 1)
            self.assertEqual(execution_payload["reward_source"], "real_trade")
            self.assertEqual(execution_payload["action_value_lane"], "execution")
            self.assertIn("trader", execution_payload["usable_by"])
            self.assertIn("execution_profile_preference", execution_payload["allowed_effects"])
            self.assertIn("change_lots", execution_payload["forbidden_effects"])
            self.assertIn("change_direction", execution_payload["forbidden_effects"])
        finally:
            conn.close()

    def test_alpha_setup_partial_real_loss_exit_does_not_write_weak_prior(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            upsert_alpha_setup_sample_and_profile(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-10",
                sample={
                    "ticker": "BU",
                    "side": "short",
                    "sector": "energy",
                    "horizon_class": "flat",
                    "market_regime": "choppy",
                    "setup_type": "generic_trade_setup",
                    "data_combo": "technical+fundamental+news",
                    "source_type": "trade",
                    "recommendation_id": "rec-bu-exit",
                    "action_taken": "close_short",
                    "pm_action": "no_opportunity",
                    "auditor_decision": "allow",
                    "trader_status": "executed",
                    "current_lots": -10,
                    "target_lots": 0,
                    "executed_lots": 10,
                    "net_pnl": -3900.0,
                    "commission": 17.83,
                    "holding_days": 0,
                    "outcome_label": "loss",
                    "setup_quality_score": 0.6596,
                    "opportunity_state": "watch_for_trigger",
                    "evidence": {"reason_codes": ["exploration_probe_reconfirm_failed"]},
                    "result": {"reward_source": "real_trade"},
                },
            )

            row = cursor.execute(
                """
                SELECT action_preference, payload_json
                FROM alpha_setup_action_value
                WHERE config_id='cfg' AND ticker='BU' AND action_name='exit'
                """
            ).fetchone()
            self.assertIsNotNone(row)
            payload = load_externalized_json(row["payload_json"])
            self.assertEqual(payload["action_preference"], "tail_loss_protect")
            self.assertEqual(row["action_preference"], payload["action_preference"])
            self.assertEqual(payload["amplification_scope_quality"], "partial_real_state")
            self.assertEqual(payload["real_trade_reward_count"], 1)
        finally:
            conn.close()

    def test_alpha_setup_open_reward_uses_complete_episode_before_daily_pnl(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
            cursor.execute(
                """
                CREATE TABLE portfolio (
                    id TEXT PRIMARY KEY,
                    config_id TEXT
                )
                """
            )
            cursor.execute("INSERT INTO portfolio(id, config_id) VALUES ('pf1', 'cfg')")
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL,
                    commission REAL,
                    holding_pnl REAL,
                    new_position_pnl REAL,
                    close_pnl REAL,
                    lots INTEGER
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO ticker_daily_pnl (
                    portfolio_id, trading_date, ticker, daily_pnl, commission,
                    holding_pnl, new_position_pnl, close_pnl, lots
                ) VALUES ('pf1', '2025-03-06', 'P', -840, 26.59, 0, -840, 0, 1)
                """
            )
            snapshot = {
                "technical": {
                    "signal": "Bullish",
                    "confidence": 0.64,
                    "metadata": {
                        "technical_context": {"market_regime": "range"},
                        "action_evidence_contract": {
                            "contract_version": "agentquant.action_evidence.v1",
                            "analyst": "technical",
                            "learning_scope": {
                                "setup_family": "trend_breakout",
                                "market_regime": "range",
                            },
                        },
                    },
                },
                "fundamental": {"signal": "Bullish", "evidence_role": "background"},
                "commodity_news": {"signal": "Neutral", "evidence_role": "risk"},
                "pm_internal_draft": {
                    "decision_horizon": "short",
                    "market_regime": "range",
                    "pm_decision_layer": "exploration_probe",
                    "opportunity_state": "tradeable_candidate",
                    "opportunity_scorecard": {
                        "long": {
                            "final_state": "tradeable_candidate",
                            "max_setup_quality": 0.64,
                        }
                    },
                    "execution_contract": {"execution_profile": "breakout"},
                },
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "P",
                    "contract_type": "strategy",
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": 1,
                    "lots_delta": 1,
                    "horizon_class": "short",
                    "market_regime": "range",
                    "setup_type": "trend_breakout_setup",
                    "opportunity_state": "tradeable_candidate",
                },
                "execution_result": {"status": "executed", "outcome": "filled"},
            }
            cursor.execute(
                """
                INSERT INTO trade_episode_memory (
                    id, config_id, trading_date, ticker, side, sector, setup_type,
                    signal_combo, horizon_class, market_regime, episode_date, open_date,
                    close_date, holding_days, net_pnl, return_on_notional, outcome_label,
                    lesson_text, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "episode-p-1",
                    "cfg",
                    "2025-03-11",
                    "P",
                    "long",
                    "agricultural",
                    "long_breakout_short",
                    json.dumps(["Bullish", "Bullish", "Neutral"]),
                    "short",
                    "range",
                    "2025-03-11",
                    "2025-03-06",
                    "2025-03-11",
                    3,
                    14640.0,
                    0.03,
                    "winner",
                    "P long episode worked despite negative entry day",
                    json.dumps(
                        {
                            "open_recommendation_id": "rec-p-episode",
                            "signal_snapshot": snapshot,
                            "opportunity_type": "trend_continuation",
                            "opportunity_state": "tradeable_candidate",
                            "data_usage_summary": {"pandaai": {"freshness": "current"}},
                        }
                    ),
                    "now",
                ),
            )

            write_alpha_setup_profiles(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-11",
                strategy_recommendations=[
                    {
                        "id": "rec-p-episode",
                        "underlying_code": "P",
                        "action": "open_long",
                        "lots": 1,
                        "status": "executed",
                        "signal_snapshot": json.dumps(snapshot),
                    }
                ],
                transactions_by_recommendation={
                    "rec-p-episode": [{"lots": 1, "daily_pnl": -840, "commission": 26.59}]
                },
            )

            sample = cursor.execute(
                """
                SELECT source_type, net_pnl, commission, holding_days, payload_json
                FROM alpha_setup_sample
                WHERE config_id='cfg'
                  AND recommendation_id='rec-p-episode'
                  AND source_type='trade_episode'
                """
            ).fetchone()
            self.assertEqual(sample["source_type"], "trade_episode")
            self.assertEqual(sample["net_pnl"], 14640.0)
            self.assertEqual(sample["commission"], 0.0)
            self.assertEqual(sample["holding_days"], 3)
            sample_payload = load_externalized_json(sample["payload_json"])
            self.assertEqual(sample_payload["result"]["single_day_net_pnl"], -866.59)
            self.assertTrue(sample_payload["result"]["episode_overrides_single_day_reward"])

            row = cursor.execute(
                """
                SELECT action_name, canonical_action_family, action_preference, reward_sum, reward_mean, payload_json
                FROM alpha_setup_action_value
                WHERE config_id='cfg' AND canonical_action_family='open_add_new_risk'
                """
            ).fetchone()
            self.assertEqual(row["action_name"], "add_or_open")
            self.assertEqual(row["canonical_action_family"], "open_add_new_risk")
            self.assertEqual(row["action_preference"], "positive_candidate_open")
            self.assertEqual(row["reward_sum"], 14640.0)
            payload = load_externalized_json(row["payload_json"])
            self.assertEqual(payload["action_preference"], "positive_candidate_open")
            self.assertEqual(payload["episode_trade_reward_count"], 1)
            self.assertEqual(payload["real_trade_reward_count"], 1)
        finally:
            conn.close()

    def test_alpha_setup_episode_reward_replaces_same_recommendation_daily_trade(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
            snapshot = {
                "technical": {
                    "signal": "Bullish",
                    "metadata": {
                        "technical_context": {"market_regime": "range"},
                        "action_evidence_contract": {
                            "learning_scope": {
                                "setup_family": "trend_breakout",
                                "market_regime": "range",
                            }
                        },
                    },
                },
                "pm_internal_draft": {
                    "decision_horizon": "short",
                    "market_regime": "range",
                    "opportunity_state": "tradeable_candidate",
                    "opportunity_scorecard": {
                        "long": {"final_state": "tradeable_candidate", "max_setup_quality": 0.7}
                    },
                },
            }
            sample = {
                "ticker": "P",
                "side": "long",
                "sector": "agricultural",
                "horizon_class": "short",
                "market_regime": "range",
                "setup_type": "trend_breakout_setup",
                "data_combo": "pandaai:current|evidence:technical:setup_family:trend_breakout",
                "scope_key": "P|long|short|range|trend_breakout_setup|pandaai:current",
                "recommendation_id": "rec-p-double",
                "action_taken": "open_long",
                "target_lots": 1,
                "current_lots": 0,
                "executed_lots": 1,
                "net_pnl": -866.59,
                "commission": 0.0,
                "source_type": "trade",
                "opportunity_state": "tradeable_candidate",
                "evidence": {"pm_internal_draft": snapshot["pm_internal_draft"]},
                "result": {"pnl_source": "daily_open_sample"},
            }
            episode_sample = {
                **sample,
                "source_type": "trade_episode",
                "net_pnl": 14640.0,
                "holding_days": 3,
                "result": {
                    "episode_net_pnl": 14640.0,
                    "episode_reward_source": "trade_episode_memory",
                },
            }
            for trading_date, row in (("2025-03-06", sample), ("2025-03-11", episode_sample)):
                result = upsert_alpha_setup_sample_and_profile(
                    cursor,
                    cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                    config_id="cfg",
                    trading_date=trading_date,
                    sample=row,
                )
                self.assertEqual(result["rows"], 1)

            row = cursor.execute(
                """
                SELECT reward_sum, sample_count, payload_json
                FROM alpha_setup_action_value
                WHERE config_id='cfg' AND action_name='open'
                """
            ).fetchone()
            self.assertEqual(row["sample_count"], 1)
            self.assertEqual(row["reward_sum"], 14640.0)
            payload = load_externalized_json(row["payload_json"])
            self.assertEqual(payload["episode_trade_reward_count"], 1)
            self.assertEqual(payload["action_preference"], "positive_candidate_open")
        finally:
            conn.close()

    def test_contextual_rule_calibration_does_not_write_intraday_execution_policy(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            payload = {"rule": "timing sample"}
            cursor.execute(
                """
                INSERT INTO no_trade_opportunity_memory (
                    id, config_id, trading_date, ticker, side, sector, setup_type,
                    signal_combo, horizon_class, market_regime, opportunity_type,
                    opportunity_state, candidate_lots, counterfactual_lots, counterfactual_entry_price,
                    pm_reason, auditor_reason, execution_reason, evidence_summary,
                    status, classification, counterfactual_results_json, payload_json,
                    created_at, last_reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "nt-1",
                    "cfg",
                    "2025-03-03",
                    "BU",
                    "long",
                    "energy",
                    "long_breakout_short",
                    json.dumps(["Bullish", "Neutral", "Neutral"]),
                    "short",
                    "trend",
                    "probe",
                    "tradeable_candidate",
                    1,
                    1,
                    3500.0,
                    "intraday_trigger_not_met",
                    "",
                    "intraday_trigger_not_met",
                    "timing miss",
                    "closed",
                    "missed_opportunity",
                    json.dumps([{"horizon_days": 3, "counterfactual_pnl": 2500.0}]),
                    json.dumps(payload),
                    "now",
                    "now",
                ),
            )

            rows = _write_contextual_rule_calibration_state(
                cursor,
                config_id="cfg",
                trading_date="2025-03-10",
                cfg={
                    "learning": {
                        "contextual_rule_calibration": {
                            "enabled": True,
                            "valid_days": 5,
                            "min_counterfactual_pnl_for_relaxation": 1000,
                            "relaxed_opening_range_miss": 0.003,
                            "relaxed_intraday_confirmation_score": 0.65,
                        }
                    }
                },
                strategy_recommendations=[],
                no_trade_reason_counter=Counter(),
            )

            self.assertEqual(rows, 0)
            row = cursor.execute(
                """
                SELECT policy_type, ticker, side, horizon_class, market_regime, payload_json
                FROM adaptive_policy_state
                WHERE policy_type = 'contextual_rule_calibration:intraday_confirmation'
                """
            ).fetchone()
            self.assertIsNone(row)
        finally:
            conn.close()

    def test_contextual_rule_calibration_writes_technical_parameter_policy(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO analyst_performance (
                    id, config_id, analyst, ticker, sector, horizon_class, signal_side,
                    sample_count, hit_rate, avg_pnl, net_pnl, confidence_score,
                    last_updated, valid_until, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ap-technical",
                    "cfg",
                    "technical",
                    "BU",
                    "energy",
                    "short",
                    "long",
                    4,
                    0.75,
                    1200.0,
                    4800.0,
                    0.55,
                    "now",
                    "2025-03-30",
                    json.dumps({"sample_count": 4}),
                ),
            )

            rows = _write_contextual_rule_calibration_state(
                cursor,
                config_id="cfg",
                trading_date="2025-03-10",
                cfg={
                    "learning": {
                        "contextual_rule_calibration": {
                            "enabled": True,
                            "valid_days": 5,
                            "max_rows_per_day": 10,
                            "min_analyst_samples": 3,
                            "min_analyst_confidence": 0.35,
                            "technical_positive_hit_rate": 0.60,
                            "technical_weak_hit_rate": 0.40,
                        }
                    }
                },
                strategy_recommendations=[],
                no_trade_reason_counter=Counter(),
            )

            self.assertGreaterEqual(rows, 1)
            row = cursor.execute(
                """
                SELECT policy_type, ticker, side, horizon_class, market_regime, sample_count, payload_json
                FROM adaptive_policy_state
                WHERE policy_type = 'contextual_rule_calibration:technical_parameters'
                """
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["ticker"], "BU")
            self.assertEqual(row["side"], "*")
            self.assertEqual(row["horizon_class"], "short")
            payload = load_externalized_json(row["payload_json"])
            self.assertIn("technical_parameters", payload["rule_adjustments"])
            self.assertIn("trend_short_multiplier", payload["rule_adjustments"]["technical_parameters"])
        finally:
            conn.close()

    def test_technical_parameter_calibration_applies_bounded_adjustments(self):
        params = {
            "trend": {"short": 8, "medium": 21, "long": 55},
            "rsi": {"period": 14, "bullish": 30, "bearish": 70},
            "mean_reversion": {"bollinger_std": 2.0, "bollinger_window": 20, "rolling_window": 50},
        }
        row = {
            "id": "policy-1",
            "ticker": "BU",
            "side": "*",
            "horizon_class": "short",
            "market_regime": "*",
            "policy_type": "contextual_rule_calibration:technical_parameters",
            "policy_action": "calibrate",
            "rule_validation_status": "validated_rule_applied",
            "confidence_score": 0.55,
            "sample_count": 4,
            "reason": "test",
            "payload": {
                "rule_adjustments": {
                    "technical_parameters": {
                        "trend_short_multiplier": 0.50,
                        "trend_long_multiplier": 2.0,
                        "rsi_bullish_shift": -20,
                        "rsi_bearish_shift": 20,
                        "bollinger_std_multiplier": 1.50,
                    }
                }
            },
        }

        adjusted, diagnostics = apply_technical_parameter_calibration(
            params,
            [row],
            ticker="BU",
            horizon_class="short",
            market_regime="trend",
        )

        self.assertEqual(adjusted["trend"]["short"], 7)
        self.assertEqual(adjusted["trend"]["long"], 63)
        self.assertEqual(adjusted["rsi"]["bullish"], 25)
        self.assertEqual(adjusted["rsi"]["bearish"], 75)
        self.assertEqual(adjusted["mean_reversion"]["bollinger_std"], 2.2)
        self.assertEqual(len(diagnostics["applied"]), 1)

    def test_technical_parameter_calibration_ignores_unvalidated_policy(self):
        params = {
            "trend": {"short": 8, "medium": 21, "long": 55},
            "rsi": {"period": 14, "bullish": 30, "bearish": 70},
            "mean_reversion": {"bollinger_std": 2.0, "bollinger_window": 20, "rolling_window": 50},
        }
        row = {
            "id": "policy-candidate",
            "ticker": "BU",
            "side": "*",
            "horizon_class": "short",
            "market_regime": "*",
            "policy_type": "contextual_rule_calibration:technical_parameters",
            "policy_action": "calibrate",
            "rule_validation_status": "candidate",
            "confidence_score": 0.55,
            "sample_count": 4,
            "reason": "test",
            "payload": {
                "rule_adjustments": {
                    "technical_parameters": {
                        "trend_short_multiplier": 0.50,
                    }
                }
            },
        }

        adjusted, diagnostics = apply_technical_parameter_calibration(
            params,
            [row],
            ticker="BU",
            horizon_class="short",
            market_regime="trend",
        )

        self.assertEqual(adjusted["trend"]["short"], 8)
        self.assertEqual(diagnostics["applied"], [])

    def test_phase1_signal_persistence_uses_recommendation_reference_portfolio(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.executescript(
                """
                CREATE TABLE portfolio (
                    id TEXT PRIMARY KEY,
                    config_id TEXT,
                    trading_date TEXT
                );
                CREATE TABLE signal (
                    id TEXT PRIMARY KEY,
                    portfolio_id TEXT,
                    ticker TEXT,
                    analyst TEXT
                );
                """
            )
            cursor.execute("INSERT INTO portfolio VALUES ('phase1-p', 'cfg', '2025-01-01')")
            cursor.execute("INSERT INTO portfolio VALUES ('settled-p', 'cfg', '2025-01-02')")
            for ticker in ("BU", "RB"):
                for analyst in ("commodity_news", "fundamental", "technical"):
                    cursor.execute(
                        "INSERT INTO signal VALUES (?, 'phase1-p', ?, ?)",
                        (f"{ticker}-{analyst}", ticker, analyst),
                    )
            recommendations = []
            for ticker in ("BU", "RB"):
                recommendations.append(
                    {
                        "underlying_code": ticker,
                        "reference_portfolio_id": "phase1-p",
                        "signal_snapshot": {
                            "commodity_news": {"signal": "Neutral"},
                            "fundamental": {"signal": "Neutral"},
                            "technical": {"signal": "Neutral"},
                        },
                    }
                )
            errors = []
            warnings = []

            audit = _validate_phase1_signal_persistence(
                cursor,
                config_id="cfg",
                trading_date="2025-01-02",
                strategy_recommendations=recommendations,
                expected_tickers=2,
                expected_analysts=("commodity_news", "fundamental", "technical"),
                errors=errors,
                warnings=warnings,
            )

            self.assertEqual(errors, [])
            self.assertTrue(audit["verified"])
            self.assertEqual(audit["db_pairs"], 6)
            self.assertEqual(audit["reference_portfolio_ids"], ["phase1-p"])
        finally:
            conn.close()

    def test_signal_context_history_persists_explicit_lifecycle_fields(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            rows = _write_signal_context_history(
                cursor,
                cfg={},
                config_id="cfg",
                trading_date="2025-02-10",
                recommendations=[
                    {
                        "id": "rec-1",
                        "underlying_code": "BU",
                        "signal_snapshot": {
                            "technical": {
                                "signal": "Bullish",
                                "horizon_class": "short",
                                "expected_horizon_days": 2,
                                "trend_stage": "low_position_reversal",
                                "price_percentile": 0.24,
                                "entry_trigger": "reversal_confirmed",
                                "action_name": "initial",
                                "invalidation_level": 3220.0,
                                "target_return": 0.035,
                            },
                            "final_action_contract": {
                                "contract_version": "agentquant.final_action.v1",
                                "ticker": "BU",
                                "final_action": "open_probe",
                                "current_lots": 0,
                                "target_lots": 1,
                                "lots_delta": 1,
                                "target_position_ratio": 0.08,
                            },
                        },
                    }
                ],
            )

            cursor.execute("SELECT * FROM signal_context_history WHERE recommendation_id = ?", ("rec-1",))
            row = dict(cursor.fetchone())
            self.assertEqual(rows, 1)
            self.assertEqual(row["ticker"], "BU")
            self.assertAlmostEqual(row["price_percentile"], 0.24)
            self.assertAlmostEqual(row["invalidation_level"], 3220.0)
            self.assertAlmostEqual(row["target_return"], 0.035)
        finally:
            conn.close()

    def test_config_overlay_persists_previous_and_rollback_values(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            inserted = _write_config_overlay(
                cursor,
                config_id="cfg",
                trading_date="2025-02-10",
                cfg={
                    "learning": {"config_overlay": {"enabled": True}, "overlay_expires_after_days": 5},
                    "capital_utilization_control": {
                        "target_margin_ratio_min": 0.16,
                        "target_margin_ratio_max": 0.20,
                        "target_margin_ratio_confirmed": 0.18,
                    },
                },
                settlement_row={"margin_ratio": 0.08, "current_margin": 400000.0},
            )

            cursor.execute(
                """
                SELECT *
                FROM config_learning_overlay
                WHERE param_key = ?
                """,
                ("capital_utilization_control.target_margin_ratio_min",),
            )
            row = dict(cursor.fetchone())
            self.assertEqual(inserted, 3)
            self.assertEqual(json.loads(row["learned_value_json"]), 0.16)
            self.assertEqual(json.loads(row["previous_value_json"]), 0.16)
            self.assertEqual(json.loads(row["rollback_value_json"]), 0.16)
            self.assertTrue(row["source_event_id"])
        finally:
            conn.close()

    def test_historical_learning_snapshot_report_writes_markdown_and_json(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO setup_type_performance (
                    id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                    sample_count, win_rate, net_pnl, avg_pnl, profit_factor,
                    confidence_score, last_updated, valid_until, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "tpl-1",
                    "cfg",
                    "BU",
                    "long",
                    "long_reversal_confirmed_trend",
                    "short",
                    "trend",
                    4,
                    0.75,
                    2200.0,
                    550.0,
                    2.5,
                    0.8,
                    "2025-02-10T00:00:00Z",
                    "2025-03-01",
                    "{}",
                ),
            )
            cursor.execute(
                """
                INSERT INTO capital_deployment_state (
                    id, config_id, trading_date, current_margin_ratio,
                    target_margin_ratio_min, target_margin_ratio_max, reason_bucket, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("cap-1", "cfg", "2025-02-10", 0.08, 0.16, 0.20, "high_score_signal_shortage", "now"),
            )
            cursor.execute(
                """
                INSERT INTO learning_event_log (
                    id, config_id, trading_date, event_type, scope_type, scope_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("evt-1", "cfg", "2025-02-10", "performance_attribution", "daily", "2025-02-10", "now"),
            )
            cursor.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT,
                    config_id TEXT,
                    trading_date TEXT,
                    source_type TEXT,
                    underlying_code TEXT,
                    created_at TEXT,
                    signal_snapshot TEXT
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO futures_recommendation
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "rec-neutral",
                    "cfg",
                    "2025-02-10",
                    "strategy",
                    "BU",
                    "now",
                    json.dumps(
                        {
                            "technical": {
                                "signal": "Neutral",
                                "neutral_reason": "conflicting indicators",
                                "missing_evidence": ["volume confirmation"],
                                "conflicting_factors": ["range_bound"],
                                "would_change_view_if": "breakout confirms",
                                "metadata": {"risk_flags": ["conflicting_indicators"]},
                            }
                        }
                    ),
                ),
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                paths = _write_historical_learning_snapshot_report(
                    cursor=cursor,
                    cfg={"exp_name": "unit"},
                    config_id="cfg",
                    trading_date="2025-02-10",
                    learning_summary={"template_rows": 1, "config_overlay_rows": 0},
                    output_root=Path(temp_dir),
                    run_id="test-run",
                )
                markdown = Path(paths["markdown"])
                payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))

                self.assertTrue(markdown.exists())
                markdown_text = markdown.read_text(encoding="utf-8")
                self.assertIn("Phase4 Historical Learning Snapshot", markdown_text)
                self.assertIn("read_only_snapshot_for_audit_and_replay", markdown_text)
                self.assertIn("Positive Templates", markdown_text)
                self.assertIn("Neutral Accountability", markdown_text)
                self.assertEqual(payload["report_boundary"], "phase4_read_only_historical_learning_snapshot")
                self.assertTrue(payload["historical_learning_snapshot"]["read_only"])
                self.assertEqual(payload["positive_templates"][0]["setup_type"], "long_reversal_confirmed_trend")
                self.assertEqual(payload["neutral_accountability"]["neutral_count"], 1)
        finally:
            conn.close()


class _FakeReviewerWeightDB:
    def get_signal_history(self, **kwargs):
        return []

    def get_analyst_performance(self, **kwargs):
        return []

    def get_setup_type_performance(self, **kwargs):
        return [
            {
                "horizon_class": "short",
                "setup_type": "long_reversal_confirmed_trend",
                "sample_count": 4,
                "win_rate": 0.75,
                "net_pnl": 1800.0,
                "confidence_score": 0.8,
            }
        ]

    def get_adaptive_policy_state(self, **kwargs):
        return [
            {
                "horizon_class": "medium",
                "setup_type": "long_late_chase_range",
                "policy_action": "cap",
                "multiplier": 0.5,
                "confidence_score": 0.8,
            }
        ]


class ReviewerDynamicWeightsRegressionTest(unittest.TestCase):
    def test_dynamic_weights_consume_template_performance_and_adaptive_policy(self):
        weights = calibrate_weights_by_signal_history(
            db=_FakeReviewerWeightDB(),
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-10",
            current_weights={"technical": 1 / 3, "fundamental": 1 / 3, "commodity_news": 1 / 3},
        )

        self.assertGreater(weights["technical"], 1 / 3)
        self.assertLess(weights["fundamental"], 1 / 3)
        self.assertAlmostEqual(sum(weights.values()), 1.0)


if __name__ == "__main__":
    unittest.main()
