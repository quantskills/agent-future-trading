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
from tools.agent_tools.decision.pm_signal_fusion import build_opportunity_scorecard
from tools.agent_tools.decision.pm_full_market_capital_deployment import (
    _ensure_final_rank_score_fields,
)
from agents.decision_team.portfolio_manager import (
    _apply_capital_utilization_control,
    _normalize_alpha_setup_action_values,
    _quality_aware_fusion_context,
    _resolve_net_exposure_control,
    get_hard_allocation_margin_ratio,
)
from database.sqlite_setup import _ensure_reviewer_learning_schema, _ensure_strategy_memory_schema
from graph.constants import Signal
from graph.schema import AnalystSignal
from llm.prompt import (
    build_futures_commodity_news_prompt,
    build_futures_fundamental_prompt,
    build_futures_technical_prompt,
)
from tools.agent_tools.analysis.analyst_business_quality import apply_business_quality_enrichment
from database.artifact_store import (
    externalize_json_for_db,
    load_externalized_json,
    load_externalized_text,
)
from database.sqlite_helper import SQLiteDB
from tools.common.template_prior import _project_path, classify_template_prior_item, load_template_prior_if_enabled
from tools.agent_tools.analysis.analyst_dynamic_weights import calibrate_weights_by_signal_history
from tools.agent_tools.analysis.analyst_learning_context import (
    apply_config_learning_overlay,
    build_learning_context,
    clear_learning_context_cache,
)
from tools.agent_tools.analysis.analyst_learning_calibration import (
    calibrate_signal_with_learning_context,
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
    _episode_alpha_setup_samples,
    _execution_learning_from_snapshot,
    _write_alpha_setup_policy_state,
    run_researcher_causal_review,
    write_alpha_setup_profiles,
    write_exploratory_hypotheses,
)
from tools.common.alpha_setup import (
    _action_preference_from_stats,
    compact_product_learning_performance_key_for_analyst,
    upsert_alpha_setup_sample_and_profile,
)
from tools.common.execution_trigger_semantics import canonical_entry_trigger
from tools.agent_tools.decision.pm_decision_memory_retrieval import retrieve_pm_memory
from tools.agent_tools.research.reviewer_phase4_review import (
    _final_action_semantic_summary,
    _horizon_class,
    _validate_phase1_signal_persistence,
)
from tools.agent_tools.research.research_review_helpers import _feedback_learning_refs
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
    _write_fast_loss_sentinel_state,
    _write_learning_mechanism_policy_state,
    _write_learned_vs_unlearned_policy_state,
    _write_loss_template_observation_research,
    _write_missed_alpha_accountability_state,
    _write_no_trade_opportunity_memory,
    _write_opportunity_ranking_learning_events,
    _write_research_position_feedback,
    _write_signal_context_history,
    _write_tail_loss_sentinel_state,
    _write_trade_episode_memory,
    _write_validated_causal_policy_rules,
)
from tools.agent_tools.decision.pm_capital_allocator import enriched_policy_evidence
from tools.common.signal_evidence_collection import build_signal_collection_contract
from tests.contract_test_fixtures import build_test_aec, build_test_signal


def _scc_from_analyst_payloads(**payloads):
    return {
        "source_contracts": [
            {
                "analyst": analyst,
                "signal_record_id": f"signal-{analyst}",
                "action_evidence_contract": {
                    "analyst": analyst,
                    **payload,
                },
            }
            for analyst, payload in payloads.items()
        ]
    }


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
                "lesson_text": (
                    "breakout held while inventory and price trend agreed; "
                    "historical_open=3200.0, historical_invalidation=3100.0"
                ),
                "payload": {
                    "pair": {
                        "open_price": 3200.0,
                        "holding_days": 2,
                        "net_pnl": 1250.0,
                    },
                    "position_lifecycle_trace": {
                        "daily_facts": [
                            {
                                "trading_date": "2025-03-10",
                                "invalidation_state": {
                                    "fac_position_invalidation_level": 3100.0,
                                    "fac_atr_stop_distance": 80.0,
                                    "fac_expected_horizon_days": 3,
                                },
                                "recommendations": [],
                            },
                            {
                                "trading_date": "2025-03-12",
                                "invalidation_state": {},
                                "recommendations": [
                                    {
                                        "final_action_contract": {
                                            "final_action": "exit",
                                            "reason_codes": ["technical_position_invalidation_triggered"],
                                        }
                                    }
                                ],
                            },
                        ]
                    },
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
    def __init__(self, action_values=None):
        super().__init__()
        self.action_values = list(action_values or [])

    def get_analyst_learning_digest(self, **kwargs):
        self.digest_calls += 1
        return []

    def get_alpha_setup_profiles(self, **kwargs):
        return []

    def get_alpha_setup_action_values(self, **kwargs):
        return list(self.action_values)

    def get_similar_alpha_setup_action_values(self, **kwargs):
        raise AssertionError("analyst learning context must not read similar trade-decision action-values")


class _RegimeAwareActionValueLearningDB(_ActionValueLearningDB):
    def __init__(self, action_values=None):
        super().__init__(action_values)
        self.action_value_calls = []

    def get_alpha_setup_action_values(self, **kwargs):
        self.action_value_calls.append(dict(kwargs))
        rows = list(self.action_values)
        market_regime = str(kwargs.get("market_regime") or "*")
        if market_regime != "*":
            rows = [
                row
                for row in rows
                if str(row.get("market_regime") or "") in {market_regime, "*"}
            ]
        side = str(kwargs.get("side") or "*")
        if side != "*":
            rows = [row for row in rows if str(row.get("side") or "") in {side, "*"}]
        horizon = str(kwargs.get("horizon_class") or "*")
        if horizon != "*":
            rows = [
                row
                for row in rows
                if str(row.get("horizon_class") or "") in {horizon, "*"}
            ]
        return rows[: int(kwargs.get("limit") or len(rows))]


def _formal_analyst_action_value(
    row_id,
    *,
    canonical=True,
    include_canonical=True,
    consumer_scope="pm_learning",
    ticker="SR",
    last_sample_date="2025-03-07",
    source_quality="exact_real_state",
    calibration_scope="analyst_calibration",
    usable_by=None,
):
    payload = {
        "amplification_scope_quality": source_quality,
        "product_learning_performance_key": {
            "contract_version": "agentquant.product_learning_performance_key.v1",
            "performance_scope_key": (
                f"{ticker}|long|trend_breakout_setup|breakout_above_5678.9|"
                "technical|capital_deployed"
            ),
            "ticker": ticker,
            "side": "long",
            "horizon_class": "short",
            "market_regime": "trend",
            "setup_type": "trend_breakout_setup",
            "action_name": "open",
            "trigger_key": "breakout_above_5678.9",
            "deployment_outcome": {
                "deployment_tier": "capital_deployed",
                "opportunity_rank": 1,
                "opportunity_score": 0.91,
                "target_lots": 3,
                "margin_ratio": 0.12,
            },
            "entry_quality_outcome": {
                "contract_version": "agentquant.entry_quality_outcome.v1",
                "entry_quality_verdict": "entry_quality_supported",
                "trigger_quality_verdict": "trigger_quality_supported",
                "trigger_confirmation_adjustment": "standard_confirmation_supported",
                "trigger_key": "breakout_above_5678.9",
                "entry_trigger": "breakout above historical 5678.9",
                "support_weight": 1.7,
                "penalty_weight": -0.2,
            },
        },
        "signal_calibration": {
            "contract_version": "agentquant.analysis_signal_calibration.v1",
            "source_action_value_contract": "agentquant.research_action_value.v1",
            "source_canonical_action_family": "open_add_new_risk",
            "consumer_scope": calibration_scope,
            "source_action_value_lane": "open",
            "source_action_preference": "positive_candidate_open",
            "source_quality": source_quality,
            "reward_source": "trade_episode",
            "calibration_bias": "positive_evidence_calibration",
            "usable_by": list(usable_by if usable_by is not None else ["analysis_team"]),
            "allowed_effects": ["evidence_quality_calibration", "setup_reliability_context"],
            "forbidden_effects": ["trade_authority", "lots", "margin_ratio", "direction_override"],
            "current_data_must_dominate": True,
        },
    }
    if include_canonical:
        payload["canonical_action_value"] = canonical
    return {
        "id": row_id,
        "scope_key": f"{ticker}|long|short|trend|trend_breakout_setup|technical",
        "ticker": ticker,
        "side": "long",
        "horizon_class": "short",
        "market_regime": "trend",
        "setup_type": "trend_breakout_setup",
        "data_combo": "technical",
        "action_name": "open",
        "canonical_action_family": "open_add_new_risk",
        "sample_count": 4,
        "reward_sum": 1200.0,
        "reward_mean": 300.0,
        "win_rate": 0.75,
        "confidence_score": 0.72,
        "action_preference": "positive_candidate_open",
        "reward_source": "trade_episode",
        "evidence_scope": source_quality,
        "action_value_lane": "open",
        "consumer_scope": consumer_scope,
        "learning_lane": "open",
        "last_sample_date": last_sample_date,
        "valid_until": "2025-04-30",
        "payload": payload,
    }


def _rescope_formal_analyst_action_value(
    row_id,
    *,
    ticker="BU",
    side="short",
    horizon_class="short",
    market_regime="high_volatility_bearish_trend",
    last_sample_date="2025-03-26",
):
    row = _formal_analyst_action_value(
        row_id,
        ticker=ticker,
        last_sample_date=last_sample_date,
    )
    row.update(
        {
            "scope_key": (
                f"{ticker}|{side}|{horizon_class}|{market_regime}|"
                "trend_breakout_setup|technical"
            ),
            "side": side,
            "horizon_class": horizon_class,
            "market_regime": market_regime,
        }
    )
    product_key = row["payload"]["product_learning_performance_key"]
    trigger_key = canonical_entry_trigger("breakout", side)
    product_key.update(
        {
            "performance_scope_key": (
                f"{ticker}|{side}|trend_breakout_setup|opening_range_breakdown|"
                "technical|capital_deployed"
            ),
            "ticker": ticker,
            "side": side,
            "horizon_class": horizon_class,
            "market_regime": market_regime,
            "trigger_key": trigger_key,
        }
    )
    product_key["entry_quality_outcome"]["trigger_key"] = trigger_key
    return row


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
                        "entry_quality_outcome": {
                            "contract_version": "agentquant.entry_quality_outcome.v1",
                            "entry_quality_verdict": "entry_loss_revalidate",
                            "trigger_quality_verdict": "trigger_loss_revalidate",
                            "trigger_confirmation_adjustment": "stronger_confirmation_required",
                            "trigger_key": "opening_range_breakdown",
                            "entry_trigger": "break below historical 3210.5",
                            "support_weight": -0.4,
                            "penalty_weight": 1.4,
                        },
                        "not_trade_authority": True,
                        "future_only": True,
                    }
                },
            }
        ]

    def get_alpha_setup_action_values(self, **kwargs):
        return []

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
        self.assertNotIn("position=current confirmation and invalidation required", context["text"])
        self.assertIn("position=no sizing impact without current confirmati", context["text"])
        self.assertIn("position=probe only until validated", context["text"])
        self.assertIn("rebuttable priors", context["text"])
        self.assertIn("confirms or contradicts", context["text"])
        self.assertIn("entry=wait for trend confirmation", context["text"])
        self.assertIn("invalidation=price falls back below breakout level", context["text"])
        self.assertIn("structure_distance=3.12%", context["text"])
        self.assertIn("raw_atr_distance=2.50%", context["text"])
        self.assertIn("expected_horizon=3d", context["text"])
        self.assertIn("actual_holding=2d", context["text"])
        self.assertIn("final_exit_reason=technical_position_invalidation_triggered", context["text"])
        self.assertIn("net_pnl=1250", context["text"])
        self.assertNotIn("3200.0", context["text"])
        self.assertNotIn("3100.0", context["text"])
        self.assertEqual(len(context["trade_episode_items"]), 1)
        self.assertEqual(len(context["no_trade_opportunity_items"]), 1)
        self.assertEqual(len(context["hypothesis_items"]), 1)
        self.assertEqual(context["candidate_hypothesis_count"], 1)
        self.assertEqual(db.budgets[0]["trade_episode_count"], 1)
        self.assertEqual(db.budgets[0]["hypothesis_count"], 1)
        self.assertGreater(db.budgets[0]["total_context_chars"], db.budgets[0]["selected_chars"])

    def test_learning_context_projects_only_safe_formal_action_values_for_analysts(self):
        db = _ActionValueLearningDB([
            _formal_analyst_action_value("av-explicit"),
            _formal_analyst_action_value(
                "av-derived-canonical",
                include_canonical=False,
            ),
        ])
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
                            "max_action_value_chars": 2000,
                        },
                    },
                },
            },
            config_id="cfg",
            trading_date="2025-03-08",
            analyst="technical",
            ticker="SR",
            context={"sector": "agricultural", "market_regime": "trend"},
            horizon_class="short",
        )

        self.assertEqual(len(context["analyst_calibration_items"]), 2)
        self.assertEqual(context["memory_trace"]["selected_counts"]["alpha_setup_action_value"], 2)
        self.assertIn("Analyst-safe action-value calibration views", context["text"])
        self.assertIn("signal_calibration_bias=positive_evidence_calibration", context["text"])
        self.assertIn("lane=open", context["text"])
        self.assertIn("entry_quality=entry_quality_supported", context["text"])
        self.assertIn("trigger_quality=trigger_quality_supported", context["text"])
        self.assertIn("confirmation=standard_confirmation_supported", context["text"])
        self.assertIn("support=1.00", context["text"])
        self.assertIn("penalty=0.00", context["text"])
        self.assertNotIn("reward_mean=", context["text"])
        self.assertNotIn("reward_sum=", context["text"])
        self.assertNotIn("target_lots", context["text"])
        self.assertNotIn("opportunity_rank", context["text"])
        self.assertNotIn("historical_pm_rank", context["text"])
        self.assertNotIn("historical_pm_score", context["text"])
        self.assertNotIn("capital_deployed", context["text"])
        self.assertNotIn("5678.9", context["text"])
        self.assertNotIn("av-explicit", context["text"])
        self.assertNotIn("av-derived-canonical", context["text"])
        for item in context["analyst_calibration_items"]:
            self.assertEqual(item["signal_calibration"]["consumer_scope"], "analyst_calibration")
            self.assertIn("analysis_team", item["signal_calibration"]["usable_by"])
            self.assertNotIn("reward_sum", item)
            self.assertNotIn("action_preference", item)
            self.assertNotIn("scope_key", item)
            self.assertNotIn("max_position_impact", item)
            entry_view = item["product_learning_calibration_view"][
                "entry_quality_calibration"
            ]
            self.assertEqual(entry_view["support_weight"], 1.0)
            self.assertEqual(entry_view["penalty_weight"], 0.0)
            self.assertEqual(entry_view["trigger_key"], "unknown_trigger")

    def test_learning_context_prefers_exact_regime_before_cross_regime_fallback(self):
        exact = _rescope_formal_analyst_action_value(
            "av-exact-regime",
            market_regime="bearish_trend",
        )
        cross_regime = _rescope_formal_analyst_action_value(
            "av-cross-regime",
            market_regime="high_volatility_bearish_trend",
        )
        db = _RegimeAwareActionValueLearningDB([exact, cross_regime])
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
                            "max_action_value_items": 3,
                            "max_action_value_chars": 1200,
                        },
                    },
                },
            },
            config_id="cfg",
            trading_date="2025-03-28",
            analyst="technical",
            ticker="BU",
            context={
                "sector": "energy",
                "market_regime": "bearish_trend",
                "dominant_direction": "bearish",
            },
            horizon_class="short",
        )

        self.assertEqual(len(db.action_value_calls), 1)
        self.assertEqual(len(context["analyst_calibration_items"]), 1)
        self.assertEqual(
            context["analyst_calibration_items"][0]["market_regime"],
            "bearish_trend",
        )
        self.assertNotIn(
            "retrieval_match_level",
            context["analyst_calibration_items"][0],
        )
        self.assertNotIn("cross-regime same-ticker/side/horizon", context["text"])

    def test_learning_context_cross_regime_fallback_is_safe_and_low_weight(self):
        eligible = _rescope_formal_analyst_action_value(
            "av-cross-regime-eligible",
            market_regime="high_volatility_bearish_trend",
        )
        wrong_side = _rescope_formal_analyst_action_value(
            "av-cross-regime-wrong-side",
            side="long",
            market_regime="high_volatility_bearish_trend",
        )
        wrong_side_exact_regime = _rescope_formal_analyst_action_value(
            "av-exact-regime-wrong-side",
            side="long",
            market_regime="bearish_trend",
        )
        wrong_horizon = _rescope_formal_analyst_action_value(
            "av-cross-regime-wrong-horizon",
            horizon_class="medium",
            market_regime="high_volatility_bearish_trend",
        )
        not_past = _rescope_formal_analyst_action_value(
            "av-cross-regime-not-past",
            market_regime="high_volatility_bearish_trend",
            last_sample_date="2025-03-28",
        )
        missing_scope = _rescope_formal_analyst_action_value(
            "av-cross-regime-missing-scope",
            market_regime="high_volatility_bearish_trend",
        )
        missing_scope["consumer_scope"] = ""
        db = _RegimeAwareActionValueLearningDB(
            [
                eligible,
                wrong_side,
                wrong_side_exact_regime,
                wrong_horizon,
                not_past,
                missing_scope,
            ]
        )
        config = {
            "learning": {"enabled": True},
            "learning_context": {
                "enabled": True,
                "max_items_per_prompt": 5,
                "max_chars_per_prompt": 1800,
                "exploratory_memory": {
                    "enabled": True,
                    "alpha_setup_profile": {
                        "enabled": True,
                        "max_action_value_items": 3,
                        "max_action_value_chars": 1200,
                    },
                },
            },
        }
        context = build_learning_context(
            db=db,
            full_config=config,
            config_id="cfg",
            trading_date="2025-03-28",
            analyst="technical",
            ticker="BU",
            context={
                "sector": "energy",
                "market_regime": "bearish_trend",
                "dominant_direction": "bearish",
            },
            horizon_class="short",
        )

        self.assertEqual(len(db.action_value_calls), 2)
        self.assertEqual(db.action_value_calls[1]["ticker"], "BU")
        self.assertEqual(db.action_value_calls[1]["side"], "short")
        self.assertEqual(db.action_value_calls[1]["horizon_class"], "short")
        self.assertEqual(db.action_value_calls[1]["market_regime"], "*")
        self.assertEqual(len(context["analyst_calibration_items"]), 1)
        item = context["analyst_calibration_items"][0]
        self.assertEqual(
            item["retrieval_match_level"],
            "cross_regime_same_ticker_side_horizon",
        )
        self.assertIn("cross-regime same-ticker/side/horizon", context["text"])
        self.assertIn("low-weight analyst calibration only", context["text"])
        for row_id in (
            "av-cross-regime-eligible",
            "av-cross-regime-wrong-side",
            "av-exact-regime-wrong-side",
            "av-cross-regime-wrong-horizon",
            "av-cross-regime-not-past",
            "av-cross-regime-missing-scope",
        ):
            self.assertNotIn(row_id, context["text"])

        fallback_signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.50,
            business_quality_score=0.50,
            factor_alignment_score=0.50,
            horizon_class="short",
            setup_type="trend_breakout_setup",
            entry_timing_signal="breakout",
        )
        fallback_calibrated = calibrate_signal_with_learning_context(
            fallback_signal,
            analyst="technical",
            ticker="BU",
            learning_context=context,
        )
        exact_item = dict(item)
        exact_item.pop("retrieval_match_level", None)
        exact_signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.50,
            business_quality_score=0.50,
            factor_alignment_score=0.50,
            horizon_class="short",
            setup_type="trend_breakout_setup",
            entry_timing_signal="breakout",
        )
        exact_calibrated = calibrate_signal_with_learning_context(
            exact_signal,
            analyst="technical",
            ticker="BU",
            learning_context={"analyst_calibration_items": [exact_item]},
        )
        fallback_strength = fallback_calibrated.metadata[
            "analyst_learning_calibration"
        ]["positive_strength"]
        exact_strength = exact_calibrated.metadata[
            "analyst_learning_calibration"
        ]["positive_strength"]
        self.assertGreater(fallback_strength, 0.0)
        self.assertLess(fallback_strength, exact_strength)

    def test_learning_context_preserves_registered_trigger_without_exposing_entry_to_fundamental(self):
        row = _formal_analyst_action_value("av-canonical-trigger")
        canonical_trigger = canonical_entry_trigger("breakout", "long")
        product_key = row["payload"]["product_learning_performance_key"]
        product_key["trigger_key"] = canonical_trigger
        product_key["entry_quality_outcome"]["trigger_key"] = canonical_trigger
        db = _ActionValueLearningDB([row])
        config = {
            "learning": {"enabled": True},
            "learning_context": {
                "enabled": True,
                "max_items_per_prompt": 5,
                "max_chars_per_prompt": 1800,
                "exploratory_memory": {
                    "enabled": True,
                    "alpha_setup_profile": {
                        "enabled": True,
                        "max_action_value_items": 1,
                        "max_action_value_chars": 1200,
                    },
                },
            },
        }

        technical = build_learning_context(
            db=db,
            full_config=config,
            config_id="cfg",
            trading_date="2025-03-08",
            analyst="technical",
            ticker="SR",
            context={"sector": "agricultural", "market_regime": "trend"},
            horizon_class="short",
        )
        technical_entry = technical["analyst_calibration_items"][0][
            "product_learning_calibration_view"
        ]["entry_quality_calibration"]
        self.assertNotEqual(technical_entry["trigger_key"], "unknown_trigger")
        self.assertIn("15", technical_entry["trigger_key"])

        fundamental = build_learning_context(
            db=db,
            full_config=config,
            config_id="cfg",
            trading_date="2025-03-08",
            analyst="fundamental",
            ticker="SR",
            context={"sector": "agricultural", "market_regime": "trend"},
            horizon_class="medium",
        )
        fundamental_view = fundamental["analyst_calibration_items"][0][
            "product_learning_calibration_view"
        ]
        self.assertNotIn("entry_quality_calibration", fundamental_view)
        self.assertNotIn("trigger_key", fundamental_view)
        self.assertNotIn("entry_quality=", fundamental["text"])

    def test_learning_context_rejects_unsafe_or_nonpast_action_value_projection(self):
        wrong_calibration_version = _formal_analyst_action_value(
            "av-unsafe-calibration-version"
        )
        wrong_calibration_version["payload"]["signal_calibration"][
            "contract_version"
        ] = "agentquant.analysis_signal_calibration.v0"
        rejected = [
            _formal_analyst_action_value("av-explicit-false", canonical=False),
            _formal_analyst_action_value("av-missing-scope", consumer_scope=""),
            _formal_analyst_action_value("av-future", last_sample_date="2025-03-08"),
            {
                **_formal_analyst_action_value("av-invalid-valid-until"),
                "valid_until": "not-a-date",
            },
            _formal_analyst_action_value("av-other-ticker", ticker="BU"),
            _formal_analyst_action_value(
                "av-unsafe-calibration-scope",
                calibration_scope="pm_learning",
            ),
            wrong_calibration_version,
            _formal_analyst_action_value(
                "av-unsafe-consumer",
                usable_by=["portfolio_manager"],
            ),
            _formal_analyst_action_value(
                "av-weak",
                source_quality="weak_similar_prior",
            ),
            {
                **_formal_analyst_action_value("av-incomplete"),
                "action_preference": "",
            },
            {
                key: value
                for key, value in _formal_analyst_action_value(
                    "av-missing-reward-metrics"
                ).items()
                if key not in {"reward_sum", "reward_mean", "win_rate"}
            },
        ]
        context = build_learning_context(
            db=_ActionValueLearningDB(rejected),
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
                            "max_action_value_items": 8,
                            "max_action_value_chars": 1200,
                        },
                    },
                },
            },
            config_id="cfg",
            trading_date="2025-03-08",
            analyst="technical",
            ticker="SR",
            context={"sector": "agricultural", "market_regime": "trend"},
            horizon_class="short",
        )

        self.assertEqual(context["analyst_calibration_items"], [])
        self.assertEqual(context["memory_trace"]["selected_counts"]["alpha_setup_action_value"], 0)
        self.assertNotIn("Analyst-safe action-value calibration views", context["text"])

    def test_role_prompts_keep_learning_exit_calibration_within_analyst_boundary(self):
        technical_prompt = build_futures_technical_prompt(
            ticker="SR",
            signal_results_compact={"trend": "Bullish"},
            gap_analysis="N/A",
            price_levels="current support and resistance",
            deterministic_atr14=12.5,
        )
        fundamental_prompt = build_futures_fundamental_prompt(
            ticker="SR",
            fundamentals="current supply-demand evidence",
            learning_context_text="historical medium-horizon learning",
        )
        news_prompt = build_futures_commodity_news_prompt(
            ticker="SR",
            instrument_context="sugar futures",
            news=[{"title": "current event evidence"}],
            learning_context_text="historical event learning",
        )

        self.assertIn("today's recalculated indicators and current price structure", technical_prompt)
        self.assertIn("propose a new position_invalidation_level and exit_hint", technical_prompt)
        self.assertIn("Raw ATR14 is a read-only deterministic current-market fact", technical_prompt)
        self.assertIn(
            "Historical entry-quality learning may calibrate only today's canonical profile selection and confirmation discipline",
            technical_prompt,
        )
        self.assertIn("must not reselect direction or create an opportunity", technical_prompt)
        self.assertIn(
            "At the pre-open proposal stage, never claim that a T-day intraday trigger is already confirmed",
            technical_prompt,
        )
        self.assertIn(
            "Fundamental learning may calibrate only the evidence assessment for today's independently formed medium-horizon direction",
            fundamental_prompt,
        )
        self.assertIn("it must not override direction", fundamental_prompt)
        self.assertIn("position_invalidation_level must be null", fundamental_prompt)
        self.assertIn("News learning may calibrate only the event impact window", news_prompt)
        for prompt in (technical_prompt, fundamental_prompt, news_prompt):
            self.assertIn("Historical absolute prices must never be copied", prompt)

    def test_learning_context_does_not_fallback_to_legacy_episode_price_text(self):
        class _LegacyEpisodeDB(_ExploratoryLearningDB):
            def get_trade_episode_memory(self, **kwargs):
                return [
                    {
                        "id": "legacy-episode",
                        "ticker": kwargs.get("ticker", "BU"),
                        "side": "long",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "setup_type": "trend_breakout_setup",
                        "holding_days": 2,
                        "net_pnl": -500.0,
                        "lesson_text": "reuse stop 9876.5 and entry 9999.0",
                        "payload": {
                            CONTRACT_KEY: {
                                "usable_memory": ["historical stop 9876.5"],
                            }
                        },
                    }
                ]

        context = build_learning_context(
            db=_LegacyEpisodeDB(),
            full_config={
                "learning": {"enabled": True},
                "learning_context": {
                    "enabled": True,
                    "max_items_per_prompt": 3,
                    "max_chars_per_prompt": 1200,
                    "exploratory_memory": {
                        "enabled": True,
                        "max_episode_items": 2,
                        "max_episode_chars": 900,
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

        self.assertIn(
            "lifecycle=[actual_holding=2d, net_pnl=-500]",
            context["text"],
        )
        self.assertNotIn("9876.5", context["text"])
        self.assertNotIn("9999.0", context["text"])

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
        self.assertTrue(view["not_trade_authority"])
        entry_view = view["entry_quality_calibration"]
        self.assertEqual(entry_view["trigger_key"], "opening_range_breakdown")
        self.assertEqual(entry_view["entry_quality_verdict"], "entry_loss_revalidate")
        self.assertEqual(entry_view["trigger_quality_verdict"], "trigger_loss_revalidate")
        self.assertEqual(entry_view["trigger_confirmation_adjustment"], "stronger_confirmation_required")
        self.assertEqual(entry_view["support_weight"], 0.0)
        self.assertEqual(entry_view["penalty_weight"], 1.0)
        self.assertIn("Product learning:", context["text"])
        self.assertIn("entry_quality=entry_loss_revalidate", context["text"])
        self.assertIn("confirmation=stronger_confirmation_required", context["text"])
        self.assertNotIn("authority_type", context["text"])
        self.assertNotIn("target_lots", context["text"])
        self.assertNotIn("lots_delta", context["text"])
        self.assertNotIn("final_action_contract", context["text"])
        self.assertNotIn("historical_pm_rank", context["text"])
        self.assertNotIn("historical_pm_score", context["text"])
        self.assertNotIn("capital_deployed", context["text"])
        self.assertNotIn("3210.5", context["text"])
        self.assertEqual(
            context["memory_trace"]["selected_memory_refs"][0]["product_learning_calibration_view"],
            view,
        )

    def test_config_overlay_uses_allowlist(self):
        with patch(
            "tools.agent_tools.analysis.analyst_learning_context.logger.info"
        ) as info_log:
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
        info_log.assert_not_called()

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
            "signal_collection_contract": {
                **_scc_from_analyst_payloads(
                    technical={
                        "signal": "Bullish",
                        "confidence": 0.72,
                        "setup_type": "breakout",
                        "horizon_class": "short",
                        "opportunity_state": "tradeable_candidate",
                        "opportunity_type": "trend_continuation",
                    },
                    fundamental={"signal": "Neutral", "confidence": 0.40, "horizon_class": "medium"},
                    commodity_news={"signal": "Bullish", "confidence": 0.61, "horizon_class": "event_short"},
                ),
                "horizon_scope": {"decision_horizon": "short"},
            },
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "BU",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": 1,
                "lots_delta": 1,
                "target_position_ratio": 0.08,
                "setup_type": "trend_breakout_setup",
                "horizon_class": "medium",
                "expected_horizon_days": 7,
                "market_regime": "trend",
            },
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
        self.assertEqual(item["setup_type"], "trend_breakout_setup")
        self.assertEqual(item["horizon_class"], "medium")
        self.assertEqual(item["market_regime"], "trend")
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

    def test_rollover_extends_strategy_episode_without_creating_extra_sample(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
        cursor.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT PRIMARY KEY, config_id TEXT NOT NULL,
                reference_portfolio_id TEXT NOT NULL, trading_date TEXT NOT NULL,
                effective_trade_date TEXT NOT NULL, source_type TEXT NOT NULL,
                underlying_code TEXT NOT NULL, contract_code TEXT, action TEXT NOT NULL,
                lots INTEGER NOT NULL, signal_snapshot TEXT, status TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE futures_transactions (
                id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL, config_id TEXT,
                recommendation_id TEXT, trading_date TEXT NOT NULL, ticker TEXT NOT NULL,
                contract_code TEXT, action TEXT NOT NULL, lots INTEGER NOT NULL,
                execution_price REAL NOT NULL, settle_price REAL,
                contract_multiplier REAL NOT NULL, margin_rate REAL NOT NULL,
                margin_used REAL NOT NULL, commission REAL DEFAULT 0,
                source_type TEXT, execution_phase TEXT, created_at TEXT NOT NULL
            )
            """
        )
        _ensure_reviewer_learning_schema(cursor)
        snapshot = {
            "signal_collection_contract": _scc_from_analyst_payloads(
                technical={"signal": "Bullish", "setup_type": "breakout"},
                commodity_news={"signal": "Bullish", "setup_type": "event_catalyst"},
            ),
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "RB",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": 2,
                "lots_delta": 2,
                "setup_type": "trend_breakout_setup",
                "entry_trigger": "breakout above opening range",
                "trigger_source": "technical",
                "horizon_class": "short",
                "expected_horizon_days": 3,
                "market_regime": "trend",
            },
        }
        cursor.execute(
            """
            INSERT INTO futures_recommendation VALUES (
                'rec-open', 'cfg', 'pf', '2025-03-10', '2025-03-10',
                'strategy', 'RB', 'rb2505', 'open_long', 2, ?, 'pending',
                '2025-03-10T09:00:00'
            )
            """,
            (json.dumps(snapshot),),
        )
        cursor.executemany(
            """
            INSERT INTO futures_transactions VALUES (
                ?, 'pf', 'cfg', ?, ?, 'RB', ?, ?, ?, ?, ?, 10, 0.1, 1000,
                ?, ?, 'phase2', ?
            )
            """,
            [
                ("tx-open", "rec-open", "2025-03-10", "rb2505", "open_long", 2, 3500.0, 3500.0, 2.0, "strategy", "2025-03-10T09:30:00"),
                ("tx-roll-close", "rec-roll", "2025-03-12", "rb2505", "close_long", 2, 3520.0, 3520.0, 2.0, "rollover", "2025-03-12T14:00:00"),
                ("tx-roll-open", "rec-roll", "2025-03-12", "rb2510", "open_long", 2, 3530.0, 3530.0, 2.0, "rollover", "2025-03-12T14:01:00"),
                ("tx-close", "rec-close", "2025-03-14", "rb2510", "close_long", 2, 3560.0, 3560.0, 2.0, "strategy", "2025-03-14T14:30:00"),
            ],
        )

        rows = _write_trade_episode_memory(
            cursor,
            cfg={"learning": {"trade_episode_memory": {"enabled": True}}},
            config_id="cfg",
            trading_date="2025-03-14",
        )

        self.assertEqual(rows, 1)
        row = dict(cursor.execute("SELECT * FROM trade_episode_memory").fetchone())
        payload = load_externalized_json(row["payload_json"])
        self.assertEqual(row["setup_type"], "trend_breakout_setup")
        self.assertEqual(payload["entry_trigger"], "breakout above opening range")
        self.assertEqual(payload["trigger_source"], "technical")
        self.assertTrue(payload["pair"]["contains_rollover"])
        self.assertEqual(payload["pair"]["physical_pair_count"], 2)
        self.assertEqual(payload["pair"]["net_pnl"], 992.0)
        self.assertEqual(
            payload["position_lifecycle_trace"]["position_cycle_transaction_ids"],
            ["tx-open", "tx-roll-close", "tx-roll-open", "tx-close"],
        )
        profile_result = write_alpha_setup_profiles(
            cursor,
            cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
            config_id="cfg",
            trading_date="2025-03-14",
            strategy_recommendations=[],
            transactions_by_recommendation={},
        )
        self.assertEqual(profile_result["rows"], 1)
        profile = cursor.execute(
            "SELECT sample_count, trade_count, net_pnl FROM alpha_setup_profile"
        ).fetchone()
        self.assertEqual(profile["sample_count"], 1)
        self.assertEqual(profile["trade_count"], 1)
        self.assertEqual(profile["net_pnl"], 992.0)
        self.assertEqual(
            cursor.execute(
                "SELECT COUNT(*) FROM alpha_setup_action_value WHERE action_name='open'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            cursor.execute(
                "SELECT COUNT(*) FROM alpha_setup_action_value WHERE action_name LIKE '%roll%'"
            ).fetchone()[0],
            0,
        )
        conn.close()

    def test_trade_episode_memory_preserves_economics_and_adds_full_fact_trace(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
        cursor.execute("CREATE TABLE portfolio (id TEXT PRIMARY KEY, config_id TEXT NOT NULL)")
        cursor.execute("INSERT INTO portfolio(id, config_id) VALUES ('pf', 'cfg')")
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
                signal_snapshot TEXT,
                status TEXT,
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
                execution_price REAL NOT NULL,
                settle_price REAL,
                contract_multiplier REAL NOT NULL,
                margin_rate REAL NOT NULL,
                margin_used REAL NOT NULL,
                commission REAL DEFAULT 0,
                source_type TEXT,
                execution_phase TEXT,
                created_at TEXT NOT NULL
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
                commission REAL,
                holding_pnl REAL,
                new_position_pnl REAL,
                close_pnl REAL,
                position_type TEXT,
                lots INTEGER,
                entry_price REAL,
                settle_price REAL
            )
            """
        )
        _ensure_reviewer_learning_schema(cursor)

        def snapshot(
            action: str,
            current_lots: int,
            target_lots: int,
            score: float,
            entry_invalidation: float,
            position_invalidation: float,
        ) -> dict:
            return {
                "signal_collection_contract": _scc_from_analyst_payloads(
                    technical={
                        "signal": "Bullish",
                        "confidence": score,
                        "opportunity_state": "tradeable_candidate",
                        "invalidation_present": True,
                        "invalidation_condition": f"before entry trade below {entry_invalidation}",
                        "invalidation_level": entry_invalidation,
                        "position_invalidation_level": position_invalidation,
                        "atr_stop_distance": 80.0,
                        "expected_horizon_days": 5,
                        "exit_hint": f"after entry close below {position_invalidation}",
                    },
                    fundamental={"signal": "Bullish", "confidence": 0.55},
                    commodity_news={"signal": "Neutral", "confidence": 0.40},
                ),
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "BU",
                    "final_action": action,
                    "current_lots": current_lots,
                    "target_lots": target_lots,
                    "lots_delta": target_lots - current_lots,
                    "target_position_ratio": 0.02 if target_lots else 0.0,
                    "setup_type": "trend_breakout_setup",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "invalidation": f"before entry trade below {entry_invalidation}",
                    "invalidation_level": entry_invalidation,
                    "position_invalidation_level": position_invalidation,
                    "atr_stop_distance": 80.0,
                    "expected_horizon_days": 5,
                    "exit_hint": f"after entry close below {position_invalidation}",
                    "evidence_used": {
                        "opportunity_score": score,
                        "market_confirmation_score": score,
                    },
                },
            }

        recommendation_rows = (
            (
                "rec-open",
                "2025-03-10",
                "open_probe",
                2,
                snapshot("open_probe", 0, 2, 0.52, 3100.0, 3050.0),
            ),
            (
                "rec-reduce",
                "2025-03-11",
                "reduce",
                1,
                snapshot("reduce", 2, 1, 0.61, 3120.0, 3070.0),
            ),
            (
                "rec-close",
                "2025-03-12",
                "exit",
                1,
                snapshot("exit", 1, 0, 0.34, 3150.0, 3090.0),
            ),
        )
        cursor.executemany(
            """
            INSERT INTO futures_recommendation (
                id, config_id, reference_portfolio_id, trading_date,
                effective_trade_date, source_type, underlying_code,
                contract_code, action, lots, signal_snapshot, status, created_at
            ) VALUES (?, 'cfg', 'pf', ?, ?, 'strategy', 'BU', 'bu2506', ?, ?, ?, 'pending', ?)
            """,
            [
                (
                    rec_id,
                    trading_date,
                    trading_date,
                    action,
                    lots,
                    json.dumps(payload),
                    f"{trading_date}T09:00:00",
                )
                for rec_id, trading_date, action, lots, payload in recommendation_rows
            ],
        )
        cursor.executemany(
            """
            INSERT INTO futures_transactions (
                id, portfolio_id, config_id, recommendation_id, trading_date,
                ticker, contract_code, action, lots, execution_price, settle_price,
                contract_multiplier, margin_rate, margin_used, commission,
                source_type, execution_phase, created_at
            ) VALUES (?, 'pf', 'cfg', ?, ?, 'BU', 'bu2506', ?, ?, ?, ?, 10, 0.1, 6400, ?,
                'strategy', 'phase2', ?)
            """,
            (
                ("tx-open", "rec-open", "2025-03-10", "open_long", 2, 3200.0, 3220.0, 2.0, "2025-03-10T09:30:00"),
                ("tx-reduce", "rec-reduce", "2025-03-11", "close_long", 1, 3250.0, 3250.0, 1.0, "2025-03-11T14:30:00"),
                ("tx-close", "rec-close", "2025-03-12", "close_long", 1, 3300.0, 3300.0, 1.0, "2025-03-12T14:30:00"),
            ),
        )
        cursor.executemany(
            """
            INSERT INTO ticker_daily_pnl (
                portfolio_id, trading_date, ticker, daily_pnl, commission,
                holding_pnl, new_position_pnl, close_pnl, position_type,
                lots, entry_price, settle_price
            ) VALUES ('pf', ?, 'BU', ?, ?, ?, ?, ?, ?, ?, 3200, ?)
            """,
            (
                ("2025-03-10", 398.0, 2.0, 0.0, 400.0, 0.0, "new", 2, 3220.0),
                ("2025-03-11", 499.0, 1.0, 0.0, 0.0, 500.0, "reduce", 1, 3250.0),
                ("2025-03-12", 999.0, 1.0, 0.0, 0.0, 1000.0, "close", 0, 3300.0),
            ),
        )
        self.assertEqual(
            _write_trade_episode_memory(
                cursor,
                cfg={"learning": {"trade_episode_memory": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-11",
            ),
            0,
        )
        rows = _write_trade_episode_memory(
            cursor,
            cfg={"learning": {"trade_episode_memory": {"enabled": True}}},
            config_id="cfg",
            trading_date="2025-03-12",
        )
        self.assertEqual(rows, 1)
        stored_rows = [
            dict(row)
            for row in cursor.execute(
                "SELECT * FROM trade_episode_memory ORDER BY close_date"
            ).fetchall()
        ]
        self.assertEqual([row["net_pnl"] for row in stored_rows], [1496.0])
        payloads = [load_externalized_json(row["payload_json"]) for row in stored_rows]
        self.assertEqual(payloads[0]["pair"]["physical_pair_count"], 2)
        self.assertEqual(payloads[0]["pair"]["gross_pnl"], 1500.0)
        self.assertEqual(payloads[0]["pair"]["commission"], 4.0)
        self.assertEqual(payloads[0]["pair"]["net_pnl"], 1496.0)
        payload = payloads[0]
        trace = payload["position_lifecycle_trace"]
        self.assertFalse(trace["economic_result_recalculated"])
        self.assertEqual(
            [fact["trading_date"] for fact in trace["daily_facts"]],
            ["2025-03-10", "2025-03-11", "2025-03-12"],
        )
        self.assertEqual(
            [
                fact["recommendations"][0]["final_action_contract"]["final_action"]
                for fact in trace["daily_facts"]
            ],
            ["open_probe", "reduce", "exit"],
        )
        self.assertEqual(
            trace["daily_facts"][0]["transactions"][0]["action"],
            "open_long",
        )
        self.assertEqual(
            trace["daily_facts"][1]["transactions"][0]["action"],
            "close_long",
        )
        self.assertEqual(trace["daily_facts"][2]["transactions"][0]["action"], "close_long")
        self.assertEqual(
            trace["position_cycle_transaction_ids"],
            ["tx-open", "tx-reduce", "tx-close"],
        )
        self.assertEqual(trace["daily_facts"][1]["ticker_settlement_facts"][0]["close_pnl"], 500.0)
        self.assertTrue(trace["daily_facts"][1]["evidence_change"]["changed"])
        self.assertTrue(trace["daily_facts"][1]["invalidation_change"]["changed"])
        self.assertEqual(
            trace["daily_facts"][0]["invalidation_state"]["fac_entry_invalidation_level"],
            3100.0,
        )
        self.assertEqual(
            trace["daily_facts"][0]["invalidation_state"]["fac_position_invalidation_level"],
            3050.0,
        )
        self.assertEqual(
            trace["daily_facts"][0]["invalidation_state"]["technical_entry_invalidation_level"],
            3100.0,
        )
        self.assertEqual(
            trace["daily_facts"][0]["invalidation_state"]["technical_position_invalidation_level"],
            3050.0,
        )
        self.assertEqual(
            trace["daily_facts"][1]["invalidation_change"]["changed_fields"]
            ["fac_position_invalidation_level"],
            {"previous": 3050.0, "current": 3070.0},
        )
        self.assertEqual(
            trace["daily_facts"][0]["recommendations"][0]["signal_collection_contract"]
            ["source_contracts"][0]["action_evidence_contract"]["analyst"],
            "technical",
        )

        self.assertEqual(
            _episode_alpha_setup_samples(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-11",
            ),
            [],
        )
        samples = _episode_alpha_setup_samples(
            cursor,
            cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
            config_id="cfg",
            trading_date="2025-03-12",
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual([sample["net_pnl"] for sample in samples], [1496.0])
        self.assertTrue(
            all(
                sample["result"]["lifecycle_fact_dates"]
                == ["2025-03-10", "2025-03-11", "2025-03-12"]
                for sample in samples
            )
        )
        profile_result = write_alpha_setup_profiles(
            cursor,
            cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
            config_id="cfg",
            trading_date="2025-03-12",
            strategy_recommendations=[],
            transactions_by_recommendation={},
        )
        self.assertEqual(profile_result["rows"], 1)
        stored_samples = cursor.execute(
            """
            SELECT trading_date, result_json
            FROM alpha_setup_sample
            WHERE config_id='cfg' AND source_type='trade_episode'
            ORDER BY trading_date
            """
        ).fetchall()
        self.assertEqual(
            [row["trading_date"] for row in stored_samples],
            ["2025-03-12"],
        )
        self.assertEqual(
            [json.loads(row["result_json"])["close_date"] for row in stored_samples],
            ["2025-03-12"],
        )
        action_value = cursor.execute(
            """
            SELECT sample_count, reward_sum, last_sample_date
            FROM alpha_setup_action_value
            WHERE config_id='cfg' AND canonical_action_family='open_add_new_risk'
            """
        ).fetchone()
        self.assertIsNotNone(action_value)
        self.assertEqual(action_value["sample_count"], 1)
        self.assertEqual(action_value["reward_sum"], 1496.0)
        self.assertEqual(action_value["last_sample_date"], "2025-03-12")
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
                'open long', '{"final_action_contract":{"final_action":"open_probe","current_lots":0,"target_lots":1,"lots_delta":1,"setup_type":"trend_breakout_setup","horizon_class":"short","expected_horizon_days":3,"market_regime":"trend"}}', 'pending',
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
            "final_action_contract": {
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": -1,
                "lots_delta": -1,
                "setup_type": "trend_breakout_setup",
                "horizon_class": "short",
                "market_regime": "trend",
            },
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
            "final_action_contract": {
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": 1,
                "lots_delta": 1,
                "setup_type": "trend_breakout_setup",
                "horizon_class": "short",
                "market_regime": "trend",
            },
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
            "final_action_contract": {
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": 1,
                "lots_delta": 1,
                "setup_type": "trend_breakout_setup",
                "horizon_class": "short",
                "market_regime": "trend",
            },
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
        self.assertEqual(note["raw_prompt"], "")
        self.assertIsNone(note["raw_prompt_artifact_path"])
        self.assertIsNone(note["raw_prompt_sha256"])
        self.assertEqual(payload["invalidation_condition"], "breakout fails before close")
        conn.close()

    def test_researcher_causal_review_prompt_requires_trade_contract(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_reviewer_learning_schema(conn.cursor())
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE portfolio (id TEXT PRIMARY KEY, config_id TEXT, trading_date TEXT)"
        )
        cursor.execute(
            """
            CREATE TABLE signal (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT,
                analyst TEXT,
                ticker TEXT,
                artifact_json TEXT,
                artifact_json_artifact_path TEXT,
                artifact_json_sha256 TEXT
            )
            """
        )
        cursor.execute(
            "INSERT INTO portfolio(id, config_id, trading_date) VALUES ('portfolio-1', 'cfg', '2025-03-12')"
        )
        for analyst in ("technical", "fundamental", "commodity_news"):
            artifact = json.dumps(
                {
                    "metadata": {
                        "action_evidence_contract": build_test_aec(
                            analyst,
                            ticker="BU",
                            trading_date="2025-03-13",
                        ),
                    },
                    "signal_artifact_metadata": {
                        "contract_version": "agentquant.signal_artifact.v1",
                    },
                }
            )
            cursor.execute(
                """
                INSERT INTO signal(
                    id, portfolio_id, analyst, ticker, artifact_json
                ) VALUES (?, 'portfolio-1', ?, 'BU', ?)
                """,
                (f"signal-{analyst}", analyst, artifact),
            )
        scc = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-13",
            analyst_signals=[
                build_test_signal(
                    analyst,
                    signal_record_id=f"signal-{analyst}",
                    ticker="BU",
                    trading_date="2025-03-13",
                )
                for analyst in ("technical", "fundamental", "commodity_news")
            ],
            enabled_analysts=["technical", "fundamental", "commodity_news"],
        )

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
                previous_trading_dates_by_ticker={"BU": "2025-03-12"},
                settlement_row={
                    "trading_date": "2025-03-13",
                    "daily_pnl": -1200,
                    "commission": 25,
                    "margin_ratio": 0.04,
                },
                strategy_recommendations=[
                    {
                        "id": "rec-1",
                        "config_id": "cfg",
                        "reference_portfolio_id": "portfolio-1",
                        "underlying_code": "BU",
                        "trading_date": "2025-03-13",
                        "effective_trade_date": "2025-03-13",
                        "action": "open_long",
                        "lots": 1,
                        "audit_payload": {
                            "producer": "auditor",
                            "recommendation_id": "rec-1",
                            "audit_verdict": "approve",
                            "hard_risk_reasons": [],
                            "soft_risk_reasons": [],
                            "source": {"pm_recommendation_id": "rec-1"},
                            "boundary": {"auditor_does_not_modify_final_action_contract": True},
                            "contract_summary": {"final_action": "open_probe"},
                            "semantic_state": {"lifecycle_state": "open"},
                        },
                        "signal_snapshot": json.dumps(
                            {
                                "signal_collection_contract": scc,
                                "final_action_contract": {
                                    "contract_version": "agentquant.final_action.v1",
                                    "ticker": "BU",
                                    "contract_code": "BU2506",
                                    "final_action": "open_probe",
                                    "current_lots": 0,
                                    "target_lots": 1,
                                    "lots_delta": 1,
                                    "authority_type": "exploration_probe",
                                    "invalidation_condition": "close below validated setup boundary",
                                    "target_margin_ratio_estimate": 0.01,
                                    "requires_intraday_confirmation": True,
                                    "can_execute_without_intraday_trigger": False,
                                },
                                "execution_result": {
                                    "outcome": "not_triggered",
                                    "status": "skipped",
                                    "transaction_count": 0,
                                    "actual_transactions": [],
                                    "no_trade_reason": "intraday_trigger_not_met",
                                },
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
        note = cursor.execute(
            "SELECT raw_prompt, raw_response, raw_prompt_artifact_path, raw_response_artifact_path "
            "FROM researcher_llm_notes WHERE config_id='cfg'"
        ).fetchone()
        self.assertEqual(note["raw_prompt"], "")
        self.assertEqual(note["raw_response"], "")
        self.assertIsNone(note["raw_prompt_artifact_path"])
        self.assertIsNone(note["raw_response_artifact_path"])
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
                SimpleNamespace(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    position_invalidation_level=3200.0,
                    atr_stop_distance=None,
                ),
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
                SimpleNamespace(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    position_invalidation_level=3200.0,
                    atr_stop_distance=None,
                ),
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
                SimpleNamespace(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    position_invalidation_level=3200.0,
                    atr_stop_distance=None,
                ),
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
                SimpleNamespace(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    position_invalidation_level=3200.0,
                    atr_stop_distance=None,
                ),
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
                SimpleNamespace(
                    agent_name="technical",
                    signal=Signal.BULLISH,
                    position_invalidation_level=3200.0,
                    atr_stop_distance=None,
                ),
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
        self.assertEqual(enriched.would_change_view_if, "")
        self.assertLessEqual(enriched.business_quality_score, 0.56)
        self.assertIn("business_quality", enriched.metadata)
        self.assertNotIn("neutral_opportunity_contract", enriched.metadata)
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
                "opportunity_state": "watch_for_trigger",
                "trigger_valid": False,
                "invalidation_present": True,
                "entry_trigger": "long entry only after price breaks above 3200 with volume",
                "invalidation_condition": "long setup invalid if price closes below 3150",
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
            [{
                "id": "rec-1",
                "underlying_code": "BU",
                "signal_snapshot": {
                    "signal_collection_contract": _scc_from_analyst_payloads(
                        technical=snapshot["technical"],
                        fundamental=snapshot["fundamental"],
                        commodity_news=snapshot["commodity_news"],
                    )
                },
            }],
            {},
        )

        self.assertEqual(item["category"], "conditional_watchlist")
        self.assertEqual(item["opportunity_state"], "watch_for_trigger")
        self.assertFalse(item["trigger_valid"])
        self.assertTrue(item["invalidation_present"])
        self.assertEqual(item["counterfactual_side"], "long")
        self.assertNotIn("action_preference", item)
        self.assertNotIn("neutral_opportunity_contract", item)
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
            [{
                "id": "rec-1",
                "underlying_code": "BU",
                "signal_snapshot": {
                    "signal_collection_contract": _scc_from_analyst_payloads(
                        technical=snapshot["technical"],
                        fundamental=snapshot["fundamental"],
                        commodity_news=snapshot["commodity_news"],
                    )
                },
            }],
            {},
        )

        self.assertEqual(risk_item["category"], "reasonable_avoidance")
        self.assertEqual(gap_item["category"], "evidence_gap_conservative")
        self.assertEqual(summary["category_counts"]["reasonable_avoidance"], 1)
        self.assertEqual(summary["category_counts"]["evidence_gap_conservative"], 1)
        self.assertAlmostEqual(summary["accountability_complete_rate"], 1.0)

    def test_reviewer_horizon_prefers_decision_scope_over_short_technical(self):
        snapshot = {
            "signal_collection_contract": {
                "horizon_scope": {
                    "decision_horizon": "medium",
                    "analyst_horizons": {
                        "technical": {"analyst_horizon": "short"},
                        "fundamental": {"analyst_horizon": "medium"},
                        "commodity_news": {"analyst_horizon": "event_short"},
                    },
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
        self.assertNotIn("source_file", json.loads(row["payload_json"]))

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

    def test_fast_loss_sentinel_inherits_opening_fac_setup_type(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT)")
            cursor.execute("INSERT INTO portfolio VALUES ('pf', 'cfg')")
            cursor.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT PRIMARY KEY, config_id TEXT, reference_portfolio_id TEXT,
                    trading_date TEXT, effective_trade_date TEXT, source_type TEXT,
                    underlying_code TEXT, contract_code TEXT, action TEXT, lots INTEGER,
                    execution_price REAL, justification TEXT, signal_snapshot TEXT,
                    signal_snapshot_artifact_path TEXT, signal_snapshot_sha256 TEXT,
                    status TEXT, created_at TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE futures_transactions (
                    id TEXT PRIMARY KEY, portfolio_id TEXT, config_id TEXT,
                    recommendation_id TEXT, trading_date TEXT, ticker TEXT,
                    contract_code TEXT, action TEXT, lots INTEGER, price REAL,
                    execution_price REAL, settle_price REAL, contract_multiplier REAL,
                    margin_rate REAL, margin_used REAL, daily_pnl REAL,
                    commission REAL, source_type TEXT, execution_phase TEXT,
                    audit_payload TEXT, warning_message TEXT,
                    booked_in_settlement BOOLEAN DEFAULT 0, justification TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            snapshot = {
                "technical": {
                    "signal": "Bearish",
                    "setup_type": "analyst_breakout_label",
                    "horizon_class": "short",
                },
                "final_action_contract": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": -1,
                    "lots_delta": -1,
                    "setup_type": "trend_breakout_setup",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "entry_trigger": "breakout below support",
                },
            }
            cursor.execute(
                "INSERT INTO futures_recommendation VALUES (?, 'cfg', 'pf', ?, ?, 'strategy', 'RB', 'rb2505', 'open_short', 1, 3500, 'open', ?, NULL, NULL, 'pending', ?)",
                ("rec-open", "2025-03-03", "2025-03-03", json.dumps(snapshot), "2025-03-03T09:00:00"),
            )
            cursor.executemany(
                """
                INSERT INTO futures_transactions (
                    id, portfolio_id, config_id, recommendation_id, trading_date,
                    ticker, contract_code, action, lots, price, execution_price,
                    settle_price, contract_multiplier, margin_rate, margin_used,
                    daily_pnl, commission, source_type, execution_phase, created_at
                ) VALUES (?, 'pf', 'cfg', ?, ?, 'RB', 'rb2505', ?, 1, ?, ?, ?, 10, 0.1, 3500, 0, 1, 'strategy', 'phase2', ?)
                """,
                [
                    ("tx-open", "rec-open", "2025-03-03", "open_short", 3500.0, 3500.0, 3500.0, "2025-03-03T09:30:00"),
                    ("tx-close", "rec-close", "2025-03-04", "close_short", 3600.0, 3600.0, 3600.0, "2025-03-04T14:30:00"),
                ],
            )

            rows = _write_fast_loss_sentinel_state(
                cursor,
                config_id="cfg",
                trading_date="2025-03-04",
                cfg={"learning": {"fast_loss_sentinel": {
                    "enabled": True,
                    "lookback_days": 5,
                    "min_loss_samples": 1,
                    "min_net_loss_abs": 1,
                    "max_rows_per_day": 6,
                }}},
            )

            self.assertEqual(rows, 1)
            policy = cursor.execute(
                "SELECT setup_type, side, horizon_class, market_regime FROM adaptive_policy_state WHERE policy_type='fast_loss_sentinel'"
            ).fetchone()
            self.assertEqual(policy["setup_type"], "trend_breakout_setup")
            self.assertEqual(policy["side"], "short")
            self.assertEqual(policy["horizon_class"], "short")
            self.assertEqual(policy["market_regime"], "trend")
        finally:
            conn.close()

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
            "setup_type": "trend_breakout_setup",
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
                    "setup_type": "trend_breakout_setup",
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
                    "scope_key": "C|long|short|trend|trend_breakout_setup|execution",
                    "ticker": "C",
                    "side": "long",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
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
                    "execution_retrieval_key": "C|pullback|technical_pullback|execution",
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
        self.assertNotIn("pm_memory_consumption_error_count", summary)
        self.assertFalse(summary["reviewer_writes_action_value"])

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
            "signal_collection_contract": {
                **_scc_from_analyst_payloads(
                    technical={
                        "signal": "Bullish",
                        "setup_type": "reversal_confirmed",
                        "horizon_class": "short",
                    },
                    fundamental={"signal": "Bullish"},
                    commodity_news={"signal": "Neutral"},
                ),
                "horizon_scope": {"decision_horizon": "short"},
            },
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "ZZ",
                "final_action": "open_probe",
                "current_lots": 0,
                "target_lots": 1,
                "lots_delta": 1,
                "target_position_ratio": 0.08,
                "setup_type": "trend_breakout_setup",
                "horizon_class": "short",
                "market_regime": "trend",
                "reason_codes": reason_codes,
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
            self.assertEqual(row["setup_type"], "trend_breakout_setup")
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
                    "trend_breakout_setup",
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
                    ("nt-rev-1", "cfg", "2025-02-20", "ZZ", "long", "trend_breakout_setup", "short", "trend", json.dumps([{"evaluation_date": "2025-02-25", "counterfactual_pnl": 2200.0}]), "now"),
                    ("nt-rev-2", "cfg", "2025-02-21", "ZZ", "long", "trend_breakout_setup", "short", "trend", json.dumps([{"evaluation_date": "2025-02-26", "counterfactual_pnl": 2100.0}]), "now"),
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
            self.assertEqual(old_cap["active"], 0, result)
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
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "BU",
                    "final_action": "open_real",
                    "current_lots": 0,
                    "target_lots": 2,
                    "lots_delta": 2,
                    "target_position_ratio": 0.08,
                    "setup_type": "trend_breakout_setup",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "reason_codes": "target_plan",
                    "reason_codes": ["learning_mechanism:alpha_promotion"],
                    "learning_used": {
                        "alpha_setup_action_values": [
                            {
                                "id": "action-value-1",
                                "ticker": "BU",
                                "side": "long",
                                "setup_type": "trend_breakout_setup",
                                "action_name": "open",
                                "canonical_action_family": "open_add_new_risk",
                                "action_value_lane": "open",
                                "learning_lane": "open",
                                "action_preference": "positive_candidate_open",
                                "canonical_action_value": True,
                                "consumer_scope": "pm_learning",
                                "reward_source": "trade_episode",
                                "evidence_scope": "exact_real_state",
                                "reward_mean": 1200.0,
                                "sample_count": 1,
                            }
                        ],
                        "pm_lifecycle_learning_trace": {
                            "decision_learning_rows": [
                                {
                                    "id": "action-value-1",
                                    "canonical_action_family": "open_add_new_risk",
                                    "action_value_lane": "open",
                                    "learning_lane": "open",
                                    "canonical_action_value": True,
                                    "consumer_scope": "pm_learning",
                                }
                            ]
                        },
                        "adaptive_policy_state": {
                            "policies": [
                                {
                                    "policy_type": "learning_mechanism:alpha_promotion",
                                    "policy_action": "protect",
                                    "ticker": "BU",
                                    "side": "long",
                                    "setup_type": "trend_breakout_setup",
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
                        {"lots": 2, "commission": 12.0}
                    ]
                },
                settlement_row={"daily_pnl": 1200.0},
            )

            self.assertEqual(result["feedback_rows"], 1)
            self.assertEqual(result["digest_rows"], 1)
            row = cursor.execute("SELECT * FROM research_position_feedback").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["ticker"], "BU")
            self.assertEqual(row["feedback_label"], "learning_position_executed_flat")
            payload = json.loads(row["payload_json"])
            self.assertEqual(payload["memory_refs"][0]["id"], "action-value-1")
            self.assertEqual(payload["outcome"]["transaction_pnl"], 0.0)
            self.assertEqual(payload["policy_refs"][0]["policy_type"], "learning_mechanism:alpha_promotion")
            digest = cursor.execute(
                "SELECT * FROM analyst_learning_digest WHERE analyst='portfolio_manager'"
            ).fetchone()
            self.assertIsNotNone(digest)
            self.assertIn("learning-to-position feedback", digest["digest_text"])
        finally:
            conn.close()

    def test_research_position_feedback_backfills_complete_episodes_idempotently(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_feedback_recommendation_table(cursor)
            action_value = {
                "id": "action-value-episode",
                "ticker": "BU",
                "side": "long",
                "setup_type": "trend_breakout_setup",
                "action_name": "open",
                "canonical_action_family": "open_add_new_risk",
                "action_value_lane": "open",
                "learning_lane": "open",
                "action_preference": "positive_candidate_open",
                "canonical_action_value": True,
                "consumer_scope": "pm_learning",
                "reward_source": "trade_episode",
                "evidence_scope": "exact_real_state",
            }
            decision_row = {
                "id": "action-value-episode",
                "canonical_action_family": "open_add_new_risk",
                "action_value_lane": "open",
                "learning_lane": "open",
                "canonical_action_value": True,
                "consumer_scope": "pm_learning",
            }
            final_contract = {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "BU",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": 2,
                "lots_delta": 2,
                "target_position_ratio": 0.08,
                "setup_type": "trend_breakout_setup",
                "horizon_class": "short",
                "market_regime": "trend",
                "reason_codes": ["learning_mechanism:alpha_promotion"],
                "learning_used": {
                    "alpha_setup_action_values": [action_value],
                    "pm_lifecycle_learning_trace": {
                        "decision_learning_rows": [decision_row],
                    },
                },
            }
            snapshot = {
                "final_action_contract": final_contract,
                "execution_result": {"no_trade_reason": ""},
            }
            recommendation = {
                "id": "rec-episode-feedback",
                "config_id": "cfg",
                "underlying_code": "BU",
                "ticker": "BU",
                "action": "open_long",
                "lots": 2,
                "status": "executed",
                "source_type": "strategy",
                "target_position_ratio": 0.08,
                "signal_snapshot": json.dumps(snapshot),
            }
            cursor.execute(
                """
                INSERT INTO futures_recommendation
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recommendation["id"],
                    "cfg",
                    "BU",
                    "BU",
                    "open_long",
                    2,
                    "executed",
                    "strategy",
                    0.08,
                    recommendation["signal_snapshot"],
                ),
            )
            cfg = {
                "learning": {
                    "position_feedback_loop": {
                        "enabled": True,
                        "valid_days": 30,
                        "max_digest_rows_per_day": 4,
                    }
                }
            }
            _write_research_position_feedback(
                cursor,
                cfg=cfg,
                config_id="cfg",
                trading_date="2025-02-28",
                strategy_recommendations=[recommendation],
                transactions_by_recommendation={
                    recommendation["id"]: [{"lots": 2, "commission": 12.0}],
                },
                settlement_row={"daily_pnl": 900.0},
            )

            def episode(
                *,
                close_transaction_id,
                gross_pnl,
                commission,
                net_pnl,
                recommendation_id="rec-episode-feedback",
                contract=final_contract,
            ):
                return {
                    "open_recommendation_id": recommendation_id,
                    "final_action_contract": contract,
                    "pair": {
                        "ticker": "BU",
                        "side": "long",
                        "open_recommendation_id": recommendation_id,
                        "open_transaction_id": "tx-open",
                        "close_transaction_id": close_transaction_id,
                        "open_date": "2025-02-28",
                        "close_date": "2025-03-05",
                        "gross_pnl": gross_pnl,
                        "commission": commission,
                        "net_pnl": net_pnl,
                    },
                }

            first = episode(
                close_transaction_id="tx-close-1",
                gross_pnl=600.0,
                commission=30.0,
                net_pnl=570.0,
            )
            second = episode(
                close_transaction_id="tx-close-2",
                gross_pnl=-250.0,
                commission=20.0,
                net_pnl=-270.0,
            )
            _write_research_position_feedback(
                cursor,
                cfg=cfg,
                config_id="cfg",
                trading_date="2025-03-05",
                strategy_recommendations=[],
                transactions_by_recommendation={},
                settlement_row={"daily_pnl": 0.0},
                completed_episode_payloads=[first, second, dict(first)],
            )
            row = cursor.execute(
                "SELECT * FROM research_position_feedback WHERE recommendation_id=?",
                (recommendation["id"],),
            ).fetchone()
            outcome = json.loads(row["outcome_json"])
            payload = json.loads(row["payload_json"])
            self.assertEqual(outcome["transaction_pnl"], 350.0)
            self.assertEqual(outcome["transaction_commission"], 50.0)
            self.assertEqual(outcome["feedback_label"], "learning_position_executed_profit")
            self.assertEqual(payload["outcome"], outcome)
            baseline = (row["outcome_json"], row["payload_json"], row["feedback_label"])
            event_count = cursor.execute("SELECT COUNT(*) FROM learning_event_log").fetchone()[0]
            digest_count = cursor.execute("SELECT COUNT(*) FROM analyst_learning_digest").fetchone()[0]

            _write_research_position_feedback(
                cursor,
                cfg=cfg,
                config_id="cfg",
                trading_date="2025-03-05",
                strategy_recommendations=[],
                transactions_by_recommendation={},
                settlement_row={"daily_pnl": 0.0},
                completed_episode_payloads=[second, first, first],
            )
            rerun = cursor.execute(
                "SELECT * FROM research_position_feedback WHERE recommendation_id=?",
                (recommendation["id"],),
            ).fetchone()
            self.assertEqual(
                (rerun["outcome_json"], rerun["payload_json"], rerun["feedback_label"]),
                baseline,
            )
            self.assertEqual(cursor.execute("SELECT COUNT(*) FROM learning_event_log").fetchone()[0], event_count)
            self.assertEqual(cursor.execute("SELECT COUNT(*) FROM analyst_learning_digest").fetchone()[0], digest_count)

            mismatched_contract = json.loads(json.dumps(final_contract))
            mismatched_contract["learning_used"]["pm_lifecycle_learning_trace"]["decision_learning_rows"].append({
                **decision_row,
                "id": "unexpected-decision-row",
            })
            rejected_payloads = [
                episode(
                    close_transaction_id="tx-close-mismatch",
                    gross_pnl=100.0,
                    commission=10.0,
                    net_pnl=90.0,
                    contract=mismatched_contract,
                ),
                episode(
                    close_transaction_id="tx-close-no-learning",
                    gross_pnl=100.0,
                    commission=10.0,
                    net_pnl=90.0,
                    contract={**final_contract, "learning_used": {}},
                ),
                episode(
                    close_transaction_id="tx-close-no-feedback",
                    gross_pnl=100.0,
                    commission=10.0,
                    net_pnl=90.0,
                    recommendation_id="rec-without-feedback",
                ),
            ]
            _write_research_position_feedback(
                cursor,
                cfg=cfg,
                config_id="cfg",
                trading_date="2025-03-05",
                strategy_recommendations=[],
                transactions_by_recommendation={},
                settlement_row={"daily_pnl": 0.0},
                completed_episode_payloads=rejected_payloads,
            )
            rejected = cursor.execute(
                "SELECT * FROM research_position_feedback WHERE recommendation_id=?",
                (recommendation["id"],),
            ).fetchone()
            self.assertEqual(
                (rejected["outcome_json"], rejected["payload_json"], rejected["feedback_label"]),
                baseline,
            )
            self.assertEqual(
                cursor.execute("SELECT COUNT(*) FROM research_position_feedback").fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_research_position_feedback_ignores_unconsumed_legacy_memory_refs(self):
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

            self.assertEqual(result["feedback_rows"], 0)
            row = cursor.execute("SELECT * FROM research_position_feedback").fetchone()
            self.assertIsNone(row)
        finally:
            conn.close()

    def test_research_position_feedback_requires_matching_formal_decision_row(self):
        formal = {
            "id": "action-value-formal",
            "ticker": "BU",
            "side": "long",
            "setup_type": "trend_breakout_setup",
            "action_name": "open",
            "canonical_action_family": "open_add_new_risk",
            "action_value_lane": "open",
            "learning_lane": "open",
            "action_preference": "positive_candidate_open",
            "canonical_action_value": True,
            "consumer_scope": "pm_learning",
            "reward_source": "trade_episode",
            "evidence_scope": "exact_real_state",
        }

        def refs_for(decision_rows):
            memory_refs, _ = _feedback_learning_refs(
                {
                    "learning_used": {
                        "alpha_setup_action_values": [formal],
                        "pm_lifecycle_learning_trace": {
                            "decision_learning_rows": decision_rows,
                        },
                    }
                }
            )
            return memory_refs

        matching = {
            "id": "action-value-formal",
            "canonical_action_family": "open_add_new_risk",
            "action_value_lane": "open",
            "learning_lane": "open",
            "canonical_action_value": True,
            "consumer_scope": "pm_learning",
        }
        self.assertEqual(len(refs_for([matching])), 1)
        self.assertEqual(refs_for([]), [])
        self.assertEqual(
            refs_for([{**matching, "canonical_action_value": False}]),
            [],
        )
        self.assertEqual(
            refs_for([{**matching, "consumer_scope": "analyst_calibration"}]),
            [],
        )
        self.assertEqual(
            refs_for([{
                **matching,
                "canonical_action_family": "reduce_exit",
                "action_value_lane": "exit",
                "learning_lane": "exit",
            }]),
            [],
        )

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
                            "signal_collection_contract": _scc_from_analyst_payloads(
                                technical={
                                    "signal": "Neutral",
                                    "neutral_reason": "needs confirmation",
                                    "missing_evidence": ["volume"],
                                    "conflicting_factors": [],
                                    "would_change_view_if": "breakout confirms",
                                },
                                fundamental={"signal": "Bullish", "confidence": 0.70},
                                commodity_news={"signal": "Bullish", "confidence": 0.65},
                            ),
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
                            "signal_collection_contract": _scc_from_analyst_payloads(
                                technical={"signal": "Neutral"},
                                fundamental={"signal": "Bullish", "confidence": 0.70},
                                commodity_news={"signal": "Bullish", "confidence": 0.65},
                            ),
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
                            "signal_collection_contract": _scc_from_analyst_payloads(
                                technical={"signal": "Neutral"},
                                fundamental={"signal": "Bullish", "confidence": 0.70},
                                commodity_news={"signal": "Bullish", "confidence": 0.65},
                            ),
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
                "signal_collection_contract": _scc_from_analyst_payloads(
                    technical={
                        "signal": "Bullish",
                        "confidence": 0.7,
                        "setup_type": "breakout",
                        "horizon_class": "short",
                        "opportunity_type": "trend_continuation",
                        "opportunity_state": "tradeable_candidate",
                        "factor_focus": ["trend"],
                        "current_evidence_conflict": [],
                    },
                    fundamental={"signal": "Bullish", "confidence": 0.6},
                    commodity_news={
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
                        "opportunity_state": "watch_for_trigger",
                        "trigger_valid": False,
                        "invalidation_present": True,
                        "entry_trigger": "long entry only after price confirms upside event follow-through",
                        "invalidation_condition": "long event setup invalid if price closes below its event range",
                    },
                ),
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "BU",
                    "final_action": "wait",
                    "current_lots": 0,
                    "target_lots": 0,
                    "lots_delta": 0,
                    "setup_type": "volatility_breakout_setup",
                    "horizon_class": "medium",
                    "market_regime": "range_breakout",
                    "entry_trigger": "opening_range_breakout_with_volume",
                    "reason_codes": ["intraday_trigger_not_met"],
                    "learning_used": {},
                    "evidence_used": {"scorecard_preferred_side": "long"},
                },
                "execution_result": {"no_trade_reason": "intraday_trigger_not_met"},
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
            self.assertEqual(item["side"], "long")
            self.assertEqual(item["setup_type"], "volatility_breakout_setup")
            self.assertEqual(item["horizon_class"], "medium")
            self.assertEqual(item["market_regime"], "range_breakout")
            self.assertEqual(payload["entry_trigger"], "opening_range_breakout_with_volume")
            self.assertEqual(
                payload[CONTRACT_KEY]["scope"]["entry_trigger"],
                "opening_range_breakout_with_volume",
            )
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
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "RB",
                    "final_action": "open_long",
                    "current_lots": 0,
                    "target_lots": 2,
                    "lots_delta": 2,
                    "setup_type": "breakout_continuation",
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "entry_trigger": "breakout_above_resistance",
                    "reason_codes": ["entry_confirmed"],
                    "learning_used": {},
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

    def test_no_trade_opportunity_memory_rejects_incomplete_fac_identity(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            snapshot = {
                "signal_collection_contract": _scc_from_analyst_payloads(
                    technical={
                        "signal": "Bullish",
                        "setup_type": "analyst_reconstructed_setup",
                        "horizon_class": "short",
                        "market_regime": "trend",
                        "entry_trigger": "analyst_reconstructed_trigger",
                    },
                    fundamental={"signal": "Bullish"},
                    commodity_news={"signal": "Neutral"},
                ),
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "BU",
                    "final_action": "wait",
                    "current_lots": 0,
                    "target_lots": 0,
                    "lots_delta": 0,
                    "reason_codes": ["current_evidence_not_tradeable"],
                    "learning_used": {},
                    "evidence_used": {"scorecard_preferred_side": "long"},
                },
            }

            rows = _write_no_trade_opportunity_memory(
                cursor,
                cfg={"learning": {"no_trade_opportunity_memory": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-11",
                strategy_recommendations=[
                    {
                        "id": "rec-incomplete-fac",
                        "config_id": "cfg",
                        "underlying_code": "BU",
                        "action": "hold",
                        "lots": 0,
                        "base_price": 3200.0,
                        "signal_snapshot": json.dumps(snapshot),
                    }
                ],
            )

            self.assertEqual(rows, 0)
            count = cursor.execute("SELECT COUNT(*) FROM no_trade_opportunity_memory").fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            conn.close()

    def _insert_fast_candidate_counterfactual_memory(
        self,
        cursor,
        *,
        memory_id,
        trading_date,
        fixed_pnl,
        execution_reason="intraday_trigger_not_met",
        complete_execution_basis=True,
        classification="missed_opportunity",
    ):
        fac = {
            "contract_version": "agentquant.final_action.v1",
            "ticker": "RB",
            "final_action": "wait",
            "current_lots": 0,
            "target_lots": 0,
            "lots_delta": 0,
            "setup_type": "volatility_breakout_setup",
            "horizon_class": "short",
            "market_regime": "trend",
            "entry_trigger": "breakout_above_opening_range",
            "execution_profile": "breakout",
            "trigger_source": "technical",
            "invalidation": "long breakout invalid below opening range",
            "invalidation_level": 3400.0,
            "evidence_used": {"scorecard_preferred_side": "long"},
        }
        if not complete_execution_basis:
            fac["invalidation"] = ""
            fac["invalidation_level"] = None
        payload = {
            "final_action_contract": fac,
            "entry_trigger": fac["entry_trigger"],
        }
        results = [
            {"horizon_days": 3, "counterfactual_pnl": 5000.0},
            {"horizon_days": 5, "counterfactual_pnl": fixed_pnl},
            {"horizon_days": 10, "counterfactual_pnl": 7000.0},
        ]
        cursor.execute(
            """
            INSERT INTO no_trade_opportunity_memory (
                id, config_id, trading_date, ticker, side, sector, setup_type,
                signal_combo, horizon_class, market_regime, opportunity_type,
                opportunity_state, candidate_lots, counterfactual_lots,
                counterfactual_entry_price, pm_reason, auditor_reason,
                execution_reason, evidence_summary, status, classification,
                counterfactual_results_json, payload_json, created_at,
                last_reviewed_at
            ) VALUES (?, 'cfg', ?, 'RB', 'long', 'ferrous',
                      'volatility_breakout_setup', '["Bullish"]', 'short',
                      'trend', 'trend_continuation', 'tradeable_candidate',
                      1, 1, 3500.0, 'intraday_trigger_not_met', '', ?, '',
                      'closed', ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                trading_date,
                execution_reason,
                classification,
                json.dumps(results),
                json.dumps(payload),
                f"{trading_date}T16:00:00Z",
                f"{trading_date}T16:00:00Z",
            ),
        )

    def test_fast_candidate_uses_fixed_horizon_and_complete_signed_sample(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._insert_fast_candidate_counterfactual_memory(
                cursor,
                memory_id="nt-positive-1",
                trading_date="2025-03-20",
                fixed_pnl=2200.0,
            )
            self._insert_fast_candidate_counterfactual_memory(
                cursor,
                memory_id="nt-negative",
                trading_date="2025-03-21",
                fixed_pnl=-900.0,
                classification="correct_avoidance",
            )
            self._insert_fast_candidate_counterfactual_memory(
                cursor,
                memory_id="nt-positive-2",
                trading_date="2025-03-24",
                fixed_pnl=1200.0,
            )

            summary = _write_missed_alpha_accountability_state(
                cursor,
                cfg={
                    "learning": {
                        "missed_alpha_accountability": {
                            "enabled": True,
                            "fixed_horizon_days": 5,
                            "min_counterfactual_samples": 3,
                            "min_net_counterfactual_pnl": 2000.0,
                            "min_positive_rate": 0.55,
                        }
                    }
                },
                config_id="cfg",
                trading_date="2025-04-10",
            )

            self.assertEqual(summary["rows"], 1)
            row = cursor.execute(
                "SELECT sample_count, payload_json FROM adaptive_policy_state WHERE policy_type='fast_candidate_alpha'"
            ).fetchone()
            payload = json.loads(row["payload_json"])
            evidence = payload["evidence"]
            self.assertEqual(row["sample_count"], 3)
            self.assertEqual(evidence["fixed_horizon_days"], 5)
            self.assertEqual(evidence["net_counterfactual_pnl"], 2500.0)
            self.assertAlmostEqual(evidence["positive_rate"], 2 / 3)
            self.assertEqual(
                evidence["memory_ids"],
                ["nt-positive-2", "nt-negative", "nt-positive-1"],
            )
        finally:
            conn.close()

    def test_fast_candidate_rejects_invalidated_and_incomplete_execution_basis(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO adaptive_policy_state (
                    id, config_id, ticker, side, setup_type, horizon_class,
                    market_regime, policy_type, policy_action, multiplier,
                    confidence_score, sample_count, reason, created_at, active
                ) VALUES (
                    'old-fast-candidate', 'cfg', 'RB', 'long',
                    'volatility_breakout_setup', 'short', 'trend',
                    'fast_candidate_alpha', 'probe', 0.75, 0.70, 2,
                    'old contaminated policy', '2025-03-25T16:00:00Z', 1
                )
                """
            )
            self._insert_fast_candidate_counterfactual_memory(
                cursor,
                memory_id="nt-valid",
                trading_date="2025-03-20",
                fixed_pnl=2200.0,
            )
            self._insert_fast_candidate_counterfactual_memory(
                cursor,
                memory_id="nt-invalidated",
                trading_date="2025-03-21",
                fixed_pnl=2600.0,
                execution_reason="fac_invalidated_before_entry",
            )
            self._insert_fast_candidate_counterfactual_memory(
                cursor,
                memory_id="nt-no-basis",
                trading_date="2025-03-24",
                fixed_pnl=2800.0,
                complete_execution_basis=False,
            )

            summary = _write_missed_alpha_accountability_state(
                cursor,
                cfg={
                    "learning": {
                        "missed_alpha_accountability": {
                            "enabled": True,
                            "fixed_horizon_days": 5,
                            "min_counterfactual_samples": 2,
                            "min_net_counterfactual_pnl": 1000.0,
                            "min_positive_rate": 0.55,
                        }
                    }
                },
                config_id="cfg",
                trading_date="2025-04-10",
            )

            self.assertEqual(summary["rows"], 0)
            self.assertEqual(summary["deactivated_rows"], 1)
            self.assertEqual(summary["excluded_invalidated"], 1)
            self.assertEqual(summary["excluded_incomplete_execution_basis"], 1)
            count = cursor.execute(
                "SELECT COUNT(*) FROM adaptive_policy_state WHERE policy_type='fast_candidate_alpha' AND active=1"
            ).fetchone()[0]
            self.assertEqual(count, 0)
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
                "final_action_contract": {
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": 1,
                    "lots_delta": 1,
                    "setup_type": "trend_breakout_setup",
                    "horizon_class": "short",
                    "market_regime": "trend",
                },
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
                SELECT ticker, side, setup_type, policy_type, policy_action, multiplier,
                       source_event_id, source_trading_date, payload_json
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
            self.assertEqual(by_ticker["BU"]["setup_type"], "trend_breakout_setup")
            self.assertEqual(by_ticker["BU"]["source_trading_date"], "2025-03-10")
            event_date = cursor.execute(
                "SELECT trading_date FROM learning_event_log WHERE id = ?",
                (by_ticker["BU"]["source_event_id"],),
            ).fetchone()[0]
            self.assertEqual(by_ticker["BU"]["source_trading_date"], event_date)
            self.assertEqual(by_ticker["ZN"]["policy_action"], "cap")
            self.assertEqual(by_ticker["ZN"]["setup_type"], "news_event_setup")
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

    def test_alpha_setup_policy_state_skips_identity_incomplete_profile(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            now = "2025-03-10T00:00:00"
            cursor.execute(
                """
                INSERT INTO alpha_setup_profile (
                    id, config_id, ticker, side, sector, horizon_class, market_regime,
                    setup_type, data_combo, scope_key, lifecycle_state, profile_state_hint,
                    sample_count, trade_count, no_trade_count, win_count, loss_count,
                    gross_profit, gross_loss, net_pnl, total_commission, profit_factor,
                    win_rate, max_loss, avg_holding_days, confidence_score,
                    max_position_impact, last_sample_date, created_at, updated_at,
                    valid_until, active, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    "prof-wildcard", "cfg", "BU", "long", "energy", "short", "trend",
                    "*", "pandaai_price", "BU|long|short|trend|*|pandaai_price",
                    "candidate", "profile_candidate", 2, 2, 0, 2, 0, 2000, 0,
                    2000, 20, 2.0, 1.0, 0, 1.0, 0.7, 0.02,
                    "2025-03-10", now, now, "2025-03-30", json.dumps({}),
                ),
            )

            result = _write_alpha_setup_policy_state(
                cursor,
                cfg={"learning": {"alpha_setup_policy_state": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-10",
            )

            self.assertEqual(result["rows"], 0)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(
                cursor.execute(
                    "SELECT COUNT(*) FROM adaptive_policy_state WHERE config_id='cfg'"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_alpha_setup_policy_state_is_not_retrieved_across_setup_type(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "adaptive-policy.db"
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            _ensure_reviewer_learning_schema(cursor)
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
            now = "2025-03-10T00:00:00"
            cursor.execute(
                """
                INSERT INTO alpha_setup_profile (
                    id, config_id, ticker, side, sector, horizon_class, market_regime,
                    setup_type, data_combo, scope_key, lifecycle_state, profile_state_hint,
                    sample_count, trade_count, no_trade_count, win_count, loss_count,
                    gross_profit, gross_loss, net_pnl, total_commission, profit_factor,
                    win_rate, max_loss, avg_holding_days, confidence_score,
                    max_position_impact, last_sample_date, created_at, updated_at,
                    valid_until, active, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    "prof-exact", "cfg", "BU", "long", "energy", "short", "trend",
                    "trend_breakout_setup", "pandaai_price",
                    "BU|long|short|trend|trend_breakout_setup|pandaai_price",
                    "candidate", "profile_candidate", 2, 2, 0, 2, 0, 2000, 0,
                    2000, 20, 2.0, 1.0, 0, 1.0, 0.7, 0.02,
                    "2025-03-10", now, now, "2025-03-30", json.dumps({}),
                ),
            )
            _write_alpha_setup_policy_state(
                cursor,
                cfg={"learning": {"alpha_setup_policy_state": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-10",
            )
            conn.commit()
            conn.close()

            db = SQLiteDB()
            db.db_path = str(db_path)
            db._runtime_schema_ready = True
            exact_rows = db.get_adaptive_policy_state(
                config_id="cfg", ticker="BU", side="long",
                setup_type="trend_breakout_setup", horizon_class="short",
                market_regime="trend", trading_date="2025-03-11",
            )
            cross_setup_rows = db.get_adaptive_policy_state(
                config_id="cfg", ticker="BU", side="long",
                setup_type="news_event_setup", horizon_class="short",
                market_regime="trend", trading_date="2025-03-11",
            )

            self.assertEqual(len(exact_rows), 1)
            self.assertEqual(exact_rows[0]["setup_type"], "trend_breakout_setup")
            self.assertEqual(cross_setup_rows, [])

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
                "signal_collection_contract": _scc_from_analyst_payloads(
                    technical={
                        "contract_version": "agentquant.action_evidence.v1",
                        "signal": "Bearish",
                        "confidence": 0.68,
                        "evidence_role": "entry_timing",
                        "opportunity_state": "probe_candidate",
                        "opportunity_type": "trend_breakout",
                        "entry_trigger": "opening range breakdown",
                        "invalidation": "recover above VWAP",
                        "market_regime": "trend",
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
                    fundamental={"signal": "Bullish", "evidence_role": "background"},
                    commodity_news={"signal": "Bullish", "evidence_role": "catalyst"},
                ),
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
            snapshot["execution_result"]["execution_learning_trace"] = {
                "execution_retrieval_key": "EB|breakout|technical_breakout|execution",
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
            self.assertEqual(execution_sample["setup_type"], "trend_breakout_setup")
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
                SELECT action_name, canonical_action_family, scope_key, setup_type,
                       execution_retrieval_key, reward_sum, sample_count, payload_json
                FROM alpha_setup_action_value
                WHERE config_id='cfg'
                ORDER BY action_name
                """
            ).fetchall()
            by_action = {row["action_name"]: row for row in action_values}
            self.assertIn("execution", by_action)
            self.assertNotIn("open", by_action)
            self.assertIn("trend_breakout_setup", by_action["execution"]["scope_key"])
            self.assertEqual(by_action["execution"]["setup_type"], "trend_breakout_setup")
            self.assertEqual(
                by_action["execution"]["execution_retrieval_key"],
                "EB|breakout|technical_breakout|execution",
            )
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
                CREATE TABLE futures_recommendation (
                    id TEXT PRIMARY KEY, config_id TEXT, trading_date TEXT,
                    effective_trade_date TEXT, source_type TEXT, underlying_code TEXT,
                    action TEXT, lots INTEGER, signal_snapshot TEXT,
                    signal_snapshot_artifact_path TEXT, signal_snapshot_sha256 TEXT,
                    created_at TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE futures_transactions (
                    id TEXT PRIMARY KEY, config_id TEXT, recommendation_id TEXT,
                    trading_date TEXT, ticker TEXT, contract_code TEXT,
                    action TEXT, lots INTEGER, source_type TEXT, created_at TEXT
                )
                """
            )
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
            opening_snapshot = {
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "BU",
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": -1,
                    "lots_delta": -1,
                    "setup_type": "fundamental_timing_setup",
                    "horizon_class": "short",
                    "market_regime": "range",
                }
            }
            cursor.execute(
                """
                INSERT INTO futures_recommendation (
                    id, config_id, trading_date, effective_trade_date, source_type,
                    underlying_code, action, lots, signal_snapshot, created_at
                ) VALUES ('rec-bu-origin', 'cfg', '2025-03-20', '2025-03-20',
                          'strategy', 'BU', 'open_short', 1, ?, '2025-03-20T09:00:00')
                """,
                (json.dumps(opening_snapshot),),
            )
            cursor.execute(
                """
                INSERT INTO futures_transactions (
                    id, config_id, recommendation_id, trading_date, ticker,
                    contract_code, action, lots, source_type, created_at
                ) VALUES ('tx-bu-origin', 'cfg', 'rec-bu-origin', '2025-03-20',
                          'BU', 'bu2506', 'open_short', 1, 'strategy',
                          '2025-03-20T09:30:00')
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
                SELECT source_type, action_taken, target_lots, executed_lots,
                       net_pnl, commission, payload_json
                FROM alpha_setup_sample
                WHERE config_id='cfg'
                  AND recommendation_id='rec-bu-hold'
                  AND source_type != 'execution'
                """
            ).fetchone()
            self.assertEqual(sample["action_taken"], "hold")
            self.assertEqual(sample["target_lots"], -1)
            self.assertEqual(sample["source_type"], "trade")
            self.assertEqual(sample["executed_lots"], 0)
            self.assertEqual(sample["net_pnl"], 800.0)
            self.assertEqual(sample["commission"], 0.0)
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
                "hold",
                {row["action_name"] for row in action_values},
            )
        finally:
            conn.close()

    def test_researcher_holding_learning_inherits_opening_fac_setup_type(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
            cursor.execute("CREATE TABLE portfolio (id TEXT PRIMARY KEY, config_id TEXT)")
            cursor.execute("INSERT INTO portfolio(id, config_id) VALUES ('pf1', 'cfg')")
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT, trading_date TEXT, ticker TEXT,
                    daily_pnl REAL, commission REAL, holding_pnl REAL,
                    new_position_pnl REAL, close_pnl REAL, lots INTEGER
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT PRIMARY KEY, config_id TEXT, trading_date TEXT,
                    effective_trade_date TEXT, source_type TEXT, underlying_code TEXT,
                    action TEXT, lots INTEGER, signal_snapshot TEXT,
                    signal_snapshot_artifact_path TEXT, signal_snapshot_sha256 TEXT,
                    created_at TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE futures_transactions (
                    id TEXT PRIMARY KEY, config_id TEXT, recommendation_id TEXT,
                    trading_date TEXT, ticker TEXT, contract_code TEXT,
                    action TEXT, lots INTEGER, source_type TEXT, created_at TEXT
                )
                """
            )
            opening_snapshot = {
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "BU",
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": 1,
                    "lots_delta": 1,
                    "setup_type": "trend_breakout_setup",
                    "horizon_class": "short",
                    "market_regime": "trend",
                }
            }
            cursor.execute(
                """
                INSERT INTO futures_recommendation (
                    id, config_id, trading_date, effective_trade_date, source_type,
                    underlying_code, action, lots, signal_snapshot, created_at
                ) VALUES ('rec-open-bu', 'cfg', '2025-03-20', '2025-03-20',
                          'strategy', 'BU', 'open_long', 1, ?, '2025-03-20T09:00:00')
                """,
                (json.dumps(opening_snapshot),),
            )
            cursor.execute(
                """
                INSERT INTO futures_transactions (
                    id, config_id, recommendation_id, trading_date, ticker,
                    contract_code, action, lots, source_type, created_at
                ) VALUES ('tx-open-bu', 'cfg', 'rec-open-bu', '2025-03-20',
                          'BU', 'bu2506', 'open_long', 1, 'strategy',
                          '2025-03-20T09:30:00')
                """
            )
            cursor.execute(
                """
                INSERT INTO futures_transactions (
                    id, config_id, recommendation_id, trading_date, ticker,
                    contract_code, action, lots, source_type, created_at
                ) VALUES ('tx-roll-close-bu', 'cfg', 'roll-bu', '2025-03-20',
                          'BU', 'bu2506', 'close_long', 1, 'rollover',
                          '2025-03-20T14:59:00')
                """
            )
            cursor.execute(
                """
                INSERT INTO futures_transactions (
                    id, config_id, recommendation_id, trading_date, ticker,
                    contract_code, action, lots, source_type, created_at
                ) VALUES ('tx-roll-open-bu', 'cfg', 'roll-bu', '2025-03-20',
                          'BU', 'bu2509', 'open_long', 1, 'rollover',
                          '2025-03-20T14:59:00')
                """
            )
            cursor.execute(
                """
                INSERT INTO ticker_daily_pnl (
                    portfolio_id, trading_date, ticker, daily_pnl, commission,
                    holding_pnl, new_position_pnl, close_pnl, lots
                ) VALUES ('pf1', '2025-03-21', 'BU', 800, 0, 800, 0, 0, 1)
                """
            )
            holding_snapshot = {
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "BU",
                    "final_action": "hold",
                    "current_lots": 1,
                    "target_lots": 1,
                    "lots_delta": 0,
                    "setup_type": "volatility_breakout_setup",
                    "horizon_class": "short",
                    "market_regime": "range",
                },
                "execution_result": {"status": "held", "outcome": "not_filled"},
            }

            write_alpha_setup_profiles(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-21",
                strategy_recommendations=[{
                    "id": "rec-hold-bu",
                    "underlying_code": "BU",
                    "action": "hold",
                    "lots": 0,
                    "status": "held",
                    "signal_snapshot": json.dumps(holding_snapshot),
                }],
                transactions_by_recommendation={"rec-hold-bu": []},
            )

            sample = cursor.execute(
                """
                SELECT setup_type, payload_json
                FROM alpha_setup_sample
                WHERE recommendation_id='rec-hold-bu' AND source_type='trade'
                """
            ).fetchone()
            self.assertEqual(sample["setup_type"], "trend_breakout_setup")
            payload = load_externalized_json(sample["payload_json"])
            self.assertEqual(
                payload["evidence"]["final_action_contract"]["setup_type"],
                "volatility_breakout_setup",
            )
        finally:
            conn.close()

    def test_reduce_exit_learning_uses_real_fills_not_remaining_position_lots(self):
        cases = (
            {
                "label": "unfilled_reduce",
                "final_action": "reduce",
                "target_lots": 3,
                "transactions": [],
                "remaining_lots": 5,
                "holding_pnl": 900.0,
                "close_pnl": 0.0,
                "commission": 0.0,
                "expected_source_type": "no_trade",
                "expected_action": "hold",
                "expected_target_lots": 5,
                "expected_executed_lots": 0,
                "expected_net_pnl": 0.0,
                "expected_action_name": "hold",
                "expected_reward_sum": None,
            },
            {
                "label": "unfilled_exit",
                "final_action": "exit",
                "target_lots": 0,
                "transactions": [],
                "remaining_lots": 5,
                "holding_pnl": -700.0,
                "close_pnl": 0.0,
                "commission": 0.0,
                "expected_source_type": "no_trade",
                "expected_action": "hold",
                "expected_target_lots": 5,
                "expected_executed_lots": 0,
                "expected_net_pnl": 0.0,
                "expected_action_name": "hold",
                "expected_reward_sum": None,
            },
            {
                "label": "partial_exit_becomes_reduce",
                "final_action": "exit",
                "target_lots": 0,
                "transactions": [
                    {
                        "action": "close_long",
                        "lots": 2,
                        "daily_pnl": -300.0,
                        "commission": 7.0,
                    }
                ],
                "remaining_lots": 3,
                "holding_pnl": 600.0,
                "close_pnl": -300.0,
                "commission": 7.0,
                "expected_source_type": "trade",
                "expected_action": "close_long",
                "expected_target_lots": 3,
                "expected_executed_lots": 2,
                "expected_net_pnl": -300.0,
                "expected_action_name": "reduce",
                "expected_reward_sum": -307.0,
            },
            {
                "label": "partial_reduce_uses_actual_close_lots",
                "final_action": "reduce",
                "target_lots": 3,
                "transactions": [
                    {
                        "action": "close_long",
                        "lots": 1,
                        "daily_pnl": 200.0,
                        "commission": 5.0,
                    }
                ],
                "remaining_lots": 4,
                "holding_pnl": 400.0,
                "close_pnl": 200.0,
                "commission": 5.0,
                "expected_source_type": "trade",
                "expected_action": "close_long",
                "expected_target_lots": 4,
                "expected_executed_lots": 1,
                "expected_net_pnl": 200.0,
                "expected_action_name": "reduce",
                "expected_reward_sum": 195.0,
            },
            {
                "label": "full_exit_stays_exit",
                "final_action": "exit",
                "target_lots": 0,
                "transactions": [
                    {
                        "action": "close_long",
                        "lots": 5,
                        "daily_pnl": 500.0,
                        "commission": 9.0,
                    }
                ],
                "remaining_lots": 0,
                "holding_pnl": 0.0,
                "close_pnl": 500.0,
                "commission": 9.0,
                "expected_source_type": "trade",
                "expected_action": "close_long",
                "expected_target_lots": 0,
                "expected_executed_lots": 5,
                "expected_net_pnl": 500.0,
                "expected_action_name": "exit",
                "expected_reward_sum": 491.0,
            },
        )

        for index, case in enumerate(cases):
            with self.subTest(case=case["label"]):
                conn = self._connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
                    cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
                    cursor.execute(
                        """
                        CREATE TABLE futures_recommendation (
                            id TEXT PRIMARY KEY, config_id TEXT, trading_date TEXT,
                            effective_trade_date TEXT, source_type TEXT, underlying_code TEXT,
                            action TEXT, lots INTEGER, signal_snapshot TEXT,
                            signal_snapshot_artifact_path TEXT, signal_snapshot_sha256 TEXT,
                            created_at TEXT
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE futures_transactions (
                            id TEXT PRIMARY KEY, config_id TEXT, recommendation_id TEXT,
                            trading_date TEXT, ticker TEXT, contract_code TEXT,
                            action TEXT, lots INTEGER, source_type TEXT, created_at TEXT
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE portfolio (
                            id TEXT PRIMARY KEY,
                            config_id TEXT
                        )
                        """
                    )
                    cursor.execute("INSERT INTO portfolio(id, config_id) VALUES ('pf1', 'cfg')")
                    opening_snapshot = {
                        "final_action_contract": {
                            "contract_version": "agentquant.final_action.v1",
                            "ticker": "RB",
                            "final_action": "open_probe",
                            "current_lots": 0,
                            "target_lots": 5,
                            "lots_delta": 5,
                            "setup_type": "news_event_setup",
                            "horizon_class": "short",
                            "market_regime": "trend",
                        }
                    }
                    cursor.execute(
                        """
                        INSERT INTO futures_recommendation (
                            id, config_id, trading_date, effective_trade_date, source_type,
                            underlying_code, action, lots, signal_snapshot, created_at
                        ) VALUES (?, 'cfg', '2025-03-21', '2025-03-21', 'strategy',
                                  'RB', 'open_long', 5, ?, '2025-03-21T09:00:00')
                        """,
                        (f"rec-rb-origin-{index}", json.dumps(opening_snapshot)),
                    )
                    cursor.execute(
                        """
                        INSERT INTO futures_transactions (
                            id, config_id, recommendation_id, trading_date, ticker,
                            contract_code, action, lots, source_type, created_at
                        ) VALUES (?, 'cfg', ?, '2025-03-21', 'RB', 'rb2505',
                                  'open_long', 5, 'strategy', '2025-03-21T09:30:00')
                        """,
                        (f"tx-rb-origin-{index}", f"rec-rb-origin-{index}"),
                    )
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
                    daily_pnl = case["holding_pnl"] + case["close_pnl"]
                    cursor.execute(
                        """
                        INSERT INTO ticker_daily_pnl (
                            portfolio_id, trading_date, ticker, daily_pnl, commission,
                            holding_pnl, new_position_pnl, close_pnl, lots
                        ) VALUES (?, '2025-03-24', 'RB', ?, ?, ?, 0, ?, ?)
                        """,
                        (
                            "pf1",
                            daily_pnl,
                            case["commission"],
                            case["holding_pnl"],
                            case["close_pnl"],
                            case["remaining_lots"],
                        ),
                    )
                    rec_id = f"rec-rb-lifecycle-{index}"
                    snapshot = {
                        "final_action_contract": {
                            "contract_version": "agentquant.final_action.v1",
                            "ticker": "RB",
                            "contract_type": "strategy",
                            "final_action": case["final_action"],
                            "current_lots": 5,
                            "target_lots": case["target_lots"],
                            "lots_delta": case["target_lots"] - 5,
                            "side": "long",
                            "horizon_class": "short",
                            "market_regime": "trend",
                            "setup_type": "trend_breakout_setup",
                            "opportunity_state": "tradeable_candidate",
                        },
                        "execution_result": {
                            "status": "executed" if case["transactions"] else "skipped",
                            "outcome": "filled" if case["transactions"] else "not_filled",
                            "transaction_count": len(case["transactions"]),
                            "actual_transactions": case["transactions"],
                            "no_trade_reason": None if case["transactions"] else "intraday_trigger_not_met",
                        },
                    }

                    write_alpha_setup_profiles(
                        cursor,
                        cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                        config_id="cfg",
                        trading_date="2025-03-24",
                        strategy_recommendations=[
                            {
                                "id": rec_id,
                                "underlying_code": "RB",
                                "action": "close_long",
                                "lots": abs(5 - case["target_lots"]),
                                "status": snapshot["execution_result"]["status"],
                                "signal_snapshot": json.dumps(snapshot),
                            }
                        ],
                        transactions_by_recommendation={rec_id: case["transactions"]},
                    )

                    sample = cursor.execute(
                        """
                        SELECT source_type, action_taken, setup_type, target_lots, executed_lots,
                               net_pnl, commission, payload_json
                        FROM alpha_setup_sample
                        WHERE config_id='cfg'
                          AND recommendation_id=?
                          AND source_type != 'execution'
                        """,
                        (rec_id,),
                    ).fetchone()
                    self.assertIsNotNone(sample)
                    self.assertEqual(sample["source_type"], case["expected_source_type"])
                    self.assertEqual(sample["action_taken"], case["expected_action"])
                    self.assertEqual(sample["setup_type"], "news_event_setup")
                    self.assertEqual(sample["target_lots"], case["expected_target_lots"])
                    self.assertEqual(sample["executed_lots"], case["expected_executed_lots"])
                    self.assertAlmostEqual(sample["net_pnl"], case["expected_net_pnl"])
                    self.assertAlmostEqual(
                        sample["commission"],
                        case["commission"] if case["transactions"] else 0.0,
                    )
                    self.assertEqual(
                        load_externalized_json(sample["payload_json"])["action_name"],
                        case["expected_action_name"],
                    )

                    real_rewards = cursor.execute(
                        """
                        SELECT action_name, reward_sum
                        FROM alpha_setup_action_value
                        WHERE config_id='cfg' AND reward_source='real_trade'
                        ORDER BY action_name
                        """
                    ).fetchall()
                    if case["expected_reward_sum"] is None:
                        self.assertEqual(real_rewards, [])
                    else:
                        reward_by_action = {
                            row["action_name"]: row["reward_sum"]
                            for row in real_rewards
                        }
                        self.assertIn(case["expected_action_name"], reward_by_action)
                        self.assertAlmostEqual(
                            reward_by_action[case["expected_action_name"]],
                            case["expected_reward_sum"],
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

    def test_alpha_setup_daily_open_pnl_does_not_create_open_action_value(self):
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
            self.assertIsNone(row)
            daily_sample = cursor.execute(
                """
                SELECT source_type, net_pnl, payload_json
                FROM alpha_setup_sample
                WHERE config_id='cfg'
                  AND recommendation_id='rec-p-1'
                  AND source_type='trade'
                """
            ).fetchone()
            self.assertEqual(daily_sample["source_type"], "trade")
            self.assertEqual(daily_sample["net_pnl"], 2200.0)
            self.assertEqual(
                load_externalized_json(daily_sample["payload_json"])["action_name"],
                "open",
            )
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
                "setup_type": "reversal_confirmed",
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
                "source_type": "trade_episode",
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
                            "opportunity_rank": 1,
                        },
                        "evidence_used": {
                            "opportunity_score": 0.81,
                        },
                    },
                },
                "result": {
                    "pnl_source": "trade_episode_memory",
                    "episode_net_pnl": 3180.0,
                    "episode_reward_source": "trade_episode_memory",
                },
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
                "source_type": "trade_episode",
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
                "result": {
                    "pnl_source": "trade_episode_memory",
                    "episode_net_pnl": -2620.0,
                    "episode_reward_source": "trade_episode_memory",
                },
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
                    "setup_type": "fundamental_timing_setup",
                    "recommendation_id": "rec-sr-execution",
                    "action_taken": "execution_exit_immediate",
                    "execution_retrieval_key": "SR|exit_immediate|profit_protection_exit|execution",
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

            exit_hint, exit_payload = observed["reduce"]
            self.assertEqual(exit_hint, "positive_candidate_exit")
            self.assertEqual(exit_payload["action_preference"], "positive_candidate_exit")
            self.assertEqual(exit_payload["amplification_scope_quality"], "partial_real_state")
            self.assertEqual(exit_payload["real_trade_reward_count"], 1)
            self.assertEqual(exit_payload["reward_source"], "real_trade")
            self.assertEqual(exit_payload["action_value_lane"], "reduce")
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

    def test_real_path_externalized_cross_day_episodes_reach_next_day_pm_retrieval(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "episode_learning.db"
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            _ensure_reviewer_learning_schema(cursor)
            open_snapshot = {
                "technical": {
                    "signal": "Bullish",
                    "metadata": {
                        "technical_context": {"market_regime": "trend"},
                        "action_evidence_contract": {
                            "learning_scope": {"setup_family": "trend_breakout"}
                        },
                    },
                },
                "pm_internal_draft": {
                    "decision_horizon": "short",
                    "market_regime": "trend",
                    "opportunity_state": "tradeable_candidate",
                },
                "final_action_contract": {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": "BU",
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": 2,
                    "lots_delta": 2,
                    "horizon_class": "short",
                    "market_regime": "trend",
                    "setup_type": "trend_breakout_setup",
                    "opportunity_state": "tradeable_candidate",
                },
            }

            def insert_episode(
                *,
                episode_id: str,
                close_date: str,
                close_recommendation_id: str,
                close_transaction_id: str,
                net_pnl: float,
                externalized: bool,
            ) -> None:
                payload = {
                    "open_recommendation_id": "rec-open-bu",
                    "pair": {
                        "ticker": "BU",
                        "side": "long",
                        "lots": 1,
                        "open_recommendation_id": "rec-open-bu",
                        "close_recommendation_id": close_recommendation_id,
                        "open_transaction_id": "tx-open-bu",
                        "close_transaction_id": close_transaction_id,
                        "open_source_type": "strategy",
                        "close_source_type": "strategy",
                        "contains_rollover": False,
                        "contains_forced_risk": False,
                        "contains_non_strategy": False,
                        "open_date": "2025-03-06",
                        "close_date": close_date,
                        "net_pnl": net_pnl,
                    },
                    "signal_snapshot": open_snapshot,
                    "final_action_contract": open_snapshot["final_action_contract"],
                    "opportunity_type": "trend_continuation",
                    "opportunity_state": "tradeable_candidate",
                    "data_usage_summary": {"pandaai": {"freshness": "fresh"}},
                }
                if externalized:
                    with patch.dict(
                        "os.environ",
                        {"AGENTQUANT_ARTIFACT_ROOT": str(Path(temp_dir) / "artifacts")},
                    ):
                        stored = externalize_json_for_db(
                            payload,
                            category="trade_episode_memory",
                            record_id=episode_id,
                            field_name="payload",
                            config_id="cfg",
                            trading_date=close_date,
                            inline_max_bytes=1,
                        )
                    payload_json = stored.inline_value
                    artifact_path = stored.artifact_path
                    payload_sha256 = stored.sha256
                    payload_size = stored.size_bytes
                    payload_summary_json = stored.summary_json
                else:
                    payload_json = json.dumps(payload)
                    artifact_path = None
                    payload_sha256 = None
                    payload_size = None
                    payload_summary_json = None
                cursor.execute(
                    """
                    INSERT INTO trade_episode_memory (
                        id, config_id, trading_date, ticker, side, sector, setup_type,
                        signal_combo, horizon_class, market_regime, episode_date,
                        first_seen_at, last_reviewed_at, open_date, close_date,
                        holding_days, net_pnl, return_on_notional, outcome_label,
                        lesson_text, payload_json, payload_artifact_path, payload_sha256,
                        payload_size, payload_summary_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        episode_id,
                        "cfg",
                        close_date,
                        "BU",
                        "long",
                        "energy",
                        "trend_breakout_setup",
                        "[]",
                        "short",
                        "trend",
                        close_date,
                        "now",
                        "now",
                        "2025-03-06",
                        close_date,
                        3,
                        net_pnl,
                        0.01,
                        "winner" if net_pnl > 0 else "loser",
                        "settled partial-close episode",
                        payload_json,
                        artifact_path,
                        payload_sha256,
                        payload_size,
                        payload_summary_json,
                        "now",
                    ),
                )

            insert_episode(
                episode_id="episode-bu-1",
                close_date="2025-03-11",
                close_recommendation_id="rec-close-bu-1",
                close_transaction_id="tx-close-bu-1",
                net_pnl=700.0,
                externalized=True,
            )
            insert_episode(
                episode_id="episode-bu-2",
                close_date="2025-03-12",
                close_recommendation_id="rec-close-bu-2",
                close_transaction_id="tx-close-bu-2",
                net_pnl=-200.0,
                externalized=False,
            )
            cfg = {"learning": {"alpha_setup_profile": {"enabled": True}}}

            first_loaded = _episode_alpha_setup_samples(
                cursor,
                cfg=cfg,
                config_id="cfg",
                trading_date="2025-03-11",
            )
            self.assertEqual(len(first_loaded), 1)
            self.assertEqual(first_loaded[0]["result"]["episode_memory_id"], "episode-bu-1")
            self.assertEqual(first_loaded[0]["current_lots"], 0)
            self.assertEqual(first_loaded[0]["target_lots"], 2)

            for close_date in ("2025-03-11", "2025-03-12", "2025-03-12"):
                write_alpha_setup_profiles(
                    cursor,
                    cfg=cfg,
                    config_id="cfg",
                    trading_date=close_date,
                    strategy_recommendations=[],
                    transactions_by_recommendation={},
                )

            samples = cursor.execute(
                """
                SELECT trading_date, recommendation_id, action_taken, net_pnl,
                       outcome_label, payload_json
                FROM alpha_setup_sample
                WHERE config_id='cfg' AND source_type='trade_episode'
                ORDER BY trading_date
                """
            ).fetchall()
            self.assertEqual(len(samples), 2)
            self.assertEqual(
                [row["trading_date"] for row in samples],
                ["2025-03-11", "2025-03-12"],
            )
            self.assertTrue(all(row["recommendation_id"] == "rec-open-bu" for row in samples))
            self.assertTrue(all(row["action_taken"] == "open_probe" for row in samples))
            self.assertEqual([row["outcome_label"] for row in samples], ["profit", "loss"])
            self.assertEqual(
                [load_externalized_json(row["payload_json"])["action_name"] for row in samples],
                ["open", "open"],
            )

            profile = cursor.execute(
                """
                SELECT sample_count, trade_count, no_trade_count, win_count,
                       loss_count, net_pnl
                FROM alpha_setup_profile
                WHERE config_id='cfg' AND ticker='BU'
                """
            ).fetchone()
            self.assertEqual(profile["sample_count"], 2)
            self.assertEqual(profile["trade_count"], 2)
            self.assertEqual(profile["no_trade_count"], 0)
            self.assertEqual(profile["win_count"], 1)
            self.assertEqual(profile["loss_count"], 1)
            self.assertEqual(profile["net_pnl"], 500.0)

            action_value = cursor.execute(
                """
                SELECT action_name, action_value_lane, memory_side_role,
                       sample_count, reward_sum, last_sample_date,
                       confidence_score, max_position_impact, payload_json
                FROM alpha_setup_action_value
                WHERE config_id='cfg' AND ticker='BU'
                """
            ).fetchone()
            self.assertEqual(action_value["action_name"], "open")
            self.assertEqual(action_value["action_value_lane"], "open")
            self.assertEqual(action_value["memory_side_role"], "target_side")
            self.assertEqual(action_value["sample_count"], 2)
            self.assertEqual(action_value["reward_sum"], 500.0)
            self.assertEqual(action_value["last_sample_date"], "2025-03-12")
            initial_action_payload = load_externalized_json(action_value["payload_json"])

            latest_episode_payload = load_externalized_json(samples[-1]["payload_json"])
            daily_open_fragment = {
                **latest_episode_payload,
                "source_type": "trade",
                "recommendation_id": "rec-open-fragment-bu",
                "action_taken": "open_probe",
                "pm_action": "open_probe",
                "current_lots": 0,
                "target_lots": 2,
                "executed_lots": 2,
                "net_pnl": 9000.0,
                "commission": 20.0,
                "holding_days": 0,
                "outcome_label": "profit",
                "result": {"pnl_source": "daily_open_fragment"},
            }
            upsert_alpha_setup_sample_and_profile(
                cursor,
                cfg=cfg,
                config_id="cfg",
                trading_date="2025-03-13",
                sample=daily_open_fragment,
            )
            action_value_after_daily_open = cursor.execute(
                """
                SELECT sample_count, reward_sum, last_sample_date,
                       confidence_score, max_position_impact, payload_json
                FROM alpha_setup_action_value
                WHERE config_id='cfg' AND ticker='BU'
                """
            ).fetchone()
            self.assertEqual(action_value_after_daily_open["sample_count"], 2)
            self.assertEqual(action_value_after_daily_open["reward_sum"], 500.0)
            self.assertEqual(action_value_after_daily_open["last_sample_date"], "2025-03-12")
            self.assertEqual(
                action_value_after_daily_open["confidence_score"],
                action_value["confidence_score"],
            )
            self.assertEqual(
                action_value_after_daily_open["max_position_impact"],
                action_value["max_position_impact"],
            )
            action_payload = load_externalized_json(action_value_after_daily_open["payload_json"])
            self.assertEqual(
                action_payload["profile_lifecycle"],
                initial_action_payload["profile_lifecycle"],
            )
            self.assertEqual(
                action_payload["entry_quality_outcome"]["net_pnl"],
                -200.0,
            )
            conn.commit()

            db = SQLiteDB()
            db.db_path = str(db_path)
            same_day_retrieval = retrieve_pm_memory(
                db=db,
                config_id="cfg",
                ticker="BU",
                side="long",
                trading_date="2025-03-12",
                horizon_class="short",
                market_regime="trend",
                setup_type="trend_breakout_setup",
            )
            self.assertEqual(same_day_retrieval["action_values"], [])
            retrieved = retrieve_pm_memory(
                db=db,
                config_id="cfg",
                ticker="BU",
                side="long",
                trading_date="2025-03-13",
                horizon_class="short",
                market_regime="trend",
                setup_type="trend_breakout_setup",
            )
            self.assertEqual(len(retrieved["action_values"]), 1)
            self.assertEqual(retrieved["action_values"][0]["reward_sum"], 500.0)
            self.assertEqual(
                retrieved["action_values"][0]["canonical_action_family"],
                "open_add_new_risk",
            )
            formal_action_values = _normalize_alpha_setup_action_values(
                retrieved["action_values"]
            )
            self.assertTrue(formal_action_values[0]["canonical_action_value"])
            current_signal = AnalystSignal(
                agent_name="technical",
                signal=Signal.BULLISH,
                confidence=0.40,
                business_quality_score=0.40,
                setup_quality_score=0.56,
                opportunity_state="tradeable_candidate",
                entry_trigger="current breakout confirmed",
                invalidation_level=3200.0,
                trigger_valid=True,
                invalidation_present=True,
            )
            scorecard_kwargs = {
                "ticker": "BU",
                "analyst_signals": [current_signal],
                "market_confirmation": {"confirmation_score": 0.30},
                "data_quality_summary": {},
                "decision_date": "2025-03-13",
                "config": {
                    "tradeable_threshold": 0.10,
                    "deployable_threshold": 0.75,
                    "min_tradeable_candidate_setup_quality": 0.55,
                },
            }
            cold_start_scorecard = build_opportunity_scorecard(**scorecard_kwargs)
            learned_scorecard = build_opportunity_scorecard(
                **scorecard_kwargs,
                alpha_setup_action_values=formal_action_values,
            )
            cold_start_row = cold_start_scorecard["long"]
            learned_row = learned_scorecard["long"]
            self.assertIn(
                cold_start_row["final_state"],
                {"probe_candidate", "tradeable_candidate"},
            )
            self.assertGreater(
                learned_row["candidate_quality"],
                cold_start_row["candidate_quality"],
            )
            cold_start_rank = _ensure_final_rank_score_fields(
                dict(cold_start_row),
                config={},
            )
            learned_rank = _ensure_final_rank_score_fields(
                dict(learned_row),
                config={},
            )
            self.assertGreater(
                learned_rank["rank_score"],
                cold_start_rank["rank_score"],
            )

            no_current_opportunity = AnalystSignal(
                agent_name="technical",
                signal=Signal.BULLISH,
                confidence=0.58,
                business_quality_score=0.58,
                setup_quality_score=0.0,
                opportunity_state="no_opportunity",
                entry_trigger="",
                invalidation_level=None,
                trigger_valid=False,
                invalidation_present=False,
            )
            learning_without_current_entry = build_opportunity_scorecard(
                ticker="BU",
                analyst_signals=[no_current_opportunity],
                market_confirmation={"confirmation_score": 0.58},
                data_quality_summary={},
                alpha_setup_action_values=formal_action_values,
                decision_date="2025-03-13",
                config={},
            )
            self.assertNotIn(
                learning_without_current_entry["long"]["final_state"],
                {"probe_candidate", "tradeable_candidate"},
            )
            conn.close()

    def test_episode_learning_rejects_invalid_and_forced_risk_but_accepts_rollover_lineage(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            close_date = "2025-03-14"

            def insert_case(
                *,
                episode_id: str,
                ticker: str,
                pair_overrides: dict,
                setup_type: str = "trend_breakout_setup",
                payload_open_recommendation_id: str | None = None,
                contract_overrides: dict | None = None,
            ) -> None:
                market_regime = "unknown" if setup_type == "generic_trade_setup" else "trend"
                opportunity_state = "unknown" if setup_type == "generic_trade_setup" else "tradeable_candidate"
                pair = {
                    "ticker": ticker,
                    "side": "long",
                    "lots": 1,
                    "open_recommendation_id": f"rec-open-{ticker}",
                    "close_recommendation_id": f"rec-close-{ticker}",
                    "open_transaction_id": f"tx-open-{ticker}",
                    "close_transaction_id": f"tx-close-{ticker}",
                    "open_source_type": "strategy",
                    "close_source_type": "strategy",
                    "contains_rollover": False,
                    "contains_forced_risk": False,
                    "contains_non_strategy": False,
                    "open_date": "2025-03-10",
                    "close_date": close_date,
                    "net_pnl": 300.0,
                }
                pair.update(pair_overrides)
                final_contract = {
                    "contract_version": "agentquant.final_action.v1",
                    "ticker": ticker,
                    "final_action": "open_probe",
                    "current_lots": 0,
                    "target_lots": 1,
                    "lots_delta": 1,
                    "setup_type": setup_type,
                    "horizon_class": "short",
                    "market_regime": market_regime,
                }
                final_contract.update(contract_overrides or {})
                payload = {
                    "open_recommendation_id": (
                        payload_open_recommendation_id
                        if payload_open_recommendation_id is not None
                        else pair.get("open_recommendation_id")
                    ),
                    "pair": pair,
                    "signal_snapshot": {
                        "final_action_contract": final_contract,
                    },
                    "opportunity_state": opportunity_state,
                }
                cursor.execute(
                    """
                    INSERT INTO trade_episode_memory (
                        id, config_id, trading_date, ticker, side, sector, setup_type,
                        signal_combo, horizon_class, market_regime, episode_date,
                        open_date, close_date, holding_days, net_pnl,
                        return_on_notional, outcome_label, lesson_text,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        episode_id,
                        "cfg",
                        close_date,
                        ticker,
                        "long",
                        "test",
                        setup_type,
                        "[]",
                        "short",
                        market_regime,
                        close_date,
                        "2025-03-10",
                        close_date,
                        4,
                        300.0,
                        0.01,
                        "winner",
                        "episode validation case",
                        json.dumps(payload),
                        "now",
                    ),
                )

            insert_case(
                episode_id="episode-valid-incomplete-state",
                ticker="TA",
                pair_overrides={},
                setup_type="generic_trade_setup",
            )
            insert_case(
                episode_id="episode-missing-close-tx",
                ticker="MA",
                pair_overrides={"close_transaction_id": ""},
            )
            insert_case(
                episode_id="episode-nonstrategy",
                ticker="NS",
                pair_overrides={"open_source_type": "counterfactual_replay"},
            )
            insert_case(
                episode_id="episode-rollover",
                ticker="RO",
                pair_overrides={"contains_rollover": True},
            )
            insert_case(
                episode_id="episode-forced-risk",
                ticker="FR",
                pair_overrides={"contains_forced_risk": True},
            )
            insert_case(
                episode_id="episode-top-id-mismatch",
                ticker="ID",
                pair_overrides={},
                payload_open_recommendation_id="rec-wrong-ID",
            )
            insert_case(
                episode_id="episode-invalid-fac-lots",
                ticker="IV",
                pair_overrides={},
                contract_overrides={"target_lots": "not-a-number"},
            )
            insert_case(
                episode_id="episode-pair-row-mismatch",
                ticker="MM",
                pair_overrides={"ticker": "OTHER"},
            )

            loaded = _episode_alpha_setup_samples(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date=close_date,
            )
            self.assertEqual(len(loaded), 1)
            self.assertEqual({row["ticker"] for row in loaded}, {"RO"})

            write_alpha_setup_profiles(
                cursor,
                cfg={"learning": {"alpha_setup_profile": {"enabled": True}}},
                config_id="cfg",
                trading_date=close_date,
                strategy_recommendations=[],
                transactions_by_recommendation={},
            )
            samples = cursor.execute(
                "SELECT ticker FROM alpha_setup_sample WHERE source_type='trade_episode'"
            ).fetchall()
            self.assertEqual({row["ticker"] for row in samples}, {"RO"})
            action_value = cursor.execute(
                "SELECT * FROM alpha_setup_action_value WHERE ticker='TA'"
            ).fetchone()
            self.assertIsNone(action_value)
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
                            "pair": {
                                "ticker": "P",
                                "side": "long",
                                "lots": 1,
                                "open_recommendation_id": "rec-p-episode",
                                "close_recommendation_id": "rec-p-close",
                                "open_transaction_id": "tx-p-open",
                                "close_transaction_id": "tx-p-close",
                                "open_source_type": "strategy",
                                "close_source_type": "strategy",
                                "contains_rollover": False,
                                "contains_forced_risk": False,
                                "contains_non_strategy": False,
                                "open_date": "2025-03-06",
                                "close_date": "2025-03-11",
                                "net_pnl": 14640.0,
                            },
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
                strategy_recommendations=[],
                transactions_by_recommendation={},
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
            self.assertEqual(sample_payload["result"]["episode_net_pnl"], 14640.0)
            self.assertEqual(sample_payload["result"]["episode_reward_source"], "trade_episode_memory")
            self.assertEqual(sample_payload["result"]["return_on_notional"], 0.03)

            row = cursor.execute(
                """
                SELECT action_name, canonical_action_family, action_preference, reward_sum, reward_mean, payload_json
                FROM alpha_setup_action_value
                WHERE config_id='cfg' AND canonical_action_family='open_add_new_risk'
                """
            ).fetchone()
            self.assertEqual(row["action_name"], "open")
            self.assertEqual(row["canonical_action_family"], "open_add_new_risk")
            self.assertEqual(row["action_preference"], "positive_candidate_open")
            self.assertEqual(row["reward_sum"], 14640.0)
            payload = load_externalized_json(row["payload_json"])
            self.assertEqual(payload["action_preference"], "positive_candidate_open")
            self.assertEqual(payload["episode_trade_reward_count"], 1)
            self.assertEqual(payload["real_trade_reward_count"], 1)
            self.assertEqual(payload["mean_return_on_notional"], 0.03)
            self.assertEqual(payload["worst_return_on_notional"], 0.03)
            self.assertEqual(payload["episode_return_on_notional_count"], 1)
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
                            "signal_collection_contract": {
                                "source_contracts": [
                                    {
                                        "analyst": analyst,
                                        "signal_record_id": f"{ticker}-{analyst}",
                                        "action_evidence_contract": {
                                            "analyst": analyst,
                                            "signal": "Neutral",
                                        },
                                    }
                                    for analyst in ("commodity_news", "fundamental", "technical")
                                ]
                            },
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
                            "signal_collection_contract": _scc_from_analyst_payloads(
                                technical={
                                    "signal": "Bullish",
                                    "horizon_class": "short",
                                    "expected_horizon_days": 2,
                                    "trend_stage": "low_position_reversal",
                                    "price_percentile": 0.24,
                                    "entry_trigger": "reversal_confirmed",
                                    "action_name": "initial",
                                    "invalidation_level": 3220.0,
                                },
                            ),
                            "final_action_contract": {
                                "contract_version": "agentquant.final_action.v1",
                                "ticker": "BU",
                                "final_action": "open_probe",
                                "current_lots": 0,
                                "target_lots": 1,
                                "lots_delta": 1,
                                "target_position_ratio": 0.08,
                                "horizon_class": "short",
                                "expected_horizon_days": 2,
                                "entry_trigger": "reversal_confirmed",
                                "invalidation_level": 3220.0,
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
            self.assertIsNone(row["target_return"])
        finally:
            conn.close()

    def test_config_overlay_does_not_write_refresh_without_changed_learned_values(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO config_learning_overlay (
                    id, config_id, trading_date, param_key, learned_value_json,
                    previous_value_json, scope_type, scope_key, source,
                    confidence_score, sample_count, reason, rollback_value_json,
                    created_at, valid_until, active
                ) VALUES (
                    'old-copy-overlay', 'cfg', '2025-02-09',
                    'capital_utilization_control.target_margin_ratio_min',
                    '0.16', '0.16', 'global', '*', 'reviewer', 0.90, 1,
                    'capital utilization hard target is managed as reviewer overlay', '0.16',
                    '2025-02-09T16:00:00Z', '2025-02-20', 1
                )
                """
            )
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

            self.assertEqual(inserted, 0)
            active_overlay_count = cursor.execute(
                "SELECT COUNT(*) FROM config_learning_overlay WHERE active=1"
            ).fetchone()[0]
            refresh_count = cursor.execute(
                "SELECT COUNT(*) FROM learning_event_log WHERE event_type='config_overlay_refresh'"
            ).fetchone()[0]
            self.assertEqual(active_overlay_count, 0)
            self.assertEqual(refresh_count, 0)
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
                            "signal_collection_contract": _scc_from_analyst_payloads(
                                technical={
                                    "signal": "Neutral",
                                    "neutral_reason": "conflicting indicators",
                                    "missing_evidence": ["volume confirmation"],
                                    "conflicting_factors": ["range_bound"],
                                    "would_change_view_if": "breakout confirms",
                                    "metadata": {"risk_flags": ["conflicting_indicators"]},
                                }
                            )
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
        with patch(
            "tools.agent_tools.analysis.analyst_dynamic_weights.logger.info"
        ) as info_log:
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
        info_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
