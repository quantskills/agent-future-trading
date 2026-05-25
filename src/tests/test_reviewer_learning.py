import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.auditor import TradeAuditor, TradeAuditorInput
from agents.portfolio_manager import (
    _apply_capital_utilization_control,
    _quality_aware_fusion_context,
    _resolve_net_exposure_control,
    get_hard_allocation_margin_ratio,
)
from database.sqlite_setup import _ensure_reviewer_learning_schema, _ensure_strategy_memory_schema
from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.agent_tools.business_quality import apply_business_quality_enrichment
from tools.agent_tools.contracts import attach_snapshot_contract, validate_artifact_header
from tools.agent_tools.template_prior import classify_template_prior_item, load_template_prior_if_enabled
from tools.agent_tools.dynamic_weights import calibrate_weights_by_signal_history
from tools.agent_tools.learning_context import apply_config_learning_overlay, build_learning_context
from tools.agent_tools.neutral_accountability import (
    build_neutral_accountability_summary,
    classify_neutral_signal,
)
from tools.agent_tools.quality import build_technical_context, apply_signal_quality_gate
from tools.agent_tools.reviewer_tools import (
    _horizon_class,
    _learned_vs_unlearned_trade_performance,
    _write_validated_causal_policy_rules,
    _write_config_overlay,
    _write_reviewer_learning_report,
    _write_signal_context_history,
)


class _FakeLearningDB:
    def __init__(self):
        self.budgets = []

    def get_analyst_learning_digest(self, **kwargs):
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
    def test_learning_context_is_bounded_and_budget_logged(self):
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
        self.assertLessEqual(len(context["text"]), 420)
        self.assertEqual(len(db.budgets), 1)
        self.assertGreater(db.budgets[0]["dropped_count"], 0)

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


class AdaptivePolicyAuditorTest(unittest.TestCase):
    def test_auditor_applies_reviewer_adaptive_cap(self):
        auditor = TradeAuditor(
            {
                "trade_auditor": {
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
            TradeAuditorInput(
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

        self.assertEqual(output.decision, "scale_down")
        self.assertAlmostEqual(output.position_ratio_multiplier, 0.5)
        self.assertIn("adaptive_policy_cap", output.reasons)

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
        self.assertEqual(diagnostics["capital_utilization_target"]["target_mode"], "strong_opportunity")
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
        self.assertEqual(rejected.get("reason"), "missing_stop_protection_for_strong_scaling")

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

    def test_strong_opportunity_can_use_configured_net_exposure_weak_param(self):
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
                    "target_mode": "strong_opportunity",
                    "high_quality_memory": True,
                }
            },
        )

        self.assertEqual(max_net, 2.00)
        self.assertTrue(symmetric)
        self.assertEqual(mode, "strong_opportunity")

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
            trigger_type="reversal_confirmed",
            entry_type="initial",
            invalidation_level=3200.0,
        )

        self.assertEqual(signal.horizon_class, "short")
        self.assertEqual(signal.expected_horizon_days, 2)
        self.assertEqual(signal.trigger_type, "reversal_confirmed")

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
                "pre_open_plan": {"target_position_ratio": 0.05},
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
            "pre_open_plan": {"expected_horizon_days": 2},
        }

        self.assertEqual(_horizon_class(2, snapshot), "medium")

    def test_template_prior_loads_into_strategy_memory_at_backtest_start(self):
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
                                "signal_template": "long_breakout_continuation_medium",
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
                            "load_on_backtest_start": True,
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
                            "load_on_backtest_start": True,
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
                        "load_on_backtest_start": True,
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
                    "SELECT ticker, side, memory_state, payload_json FROM strategy_memory WHERE config_id = ?",
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


class ReviewerLearningPersistenceRegressionTest(unittest.TestCase):
    def _connection(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_reviewer_learning_schema(conn.cursor())
        return conn

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

    def _signal_snapshot(self, *, learned: bool = False):
        plan = {
            "analyst_signal_combo": ["Bullish", "Bullish", "Neutral"],
            "decision_horizon": "short",
            "market_regime": "trend",
            "target_position_ratio": 0.08,
        }
        if learned:
            plan["trade_auditor"] = {
                "decision": "allow",
                "reasons": ["adaptive_policy_protect"],
                "diagnostics": {"adaptive_policy_applied": [{"policy_type": "causal_review_rule"}]},
            }
        return {
            "technical": {
                "signal": "Bullish",
                "template_name": "reversal_confirmed",
                "horizon_class": "short",
            },
            "fundamental": {"signal": "Bullish"},
            "commodity_news": {"signal": "Neutral"},
            "pre_open_plan": plan,
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
                INSERT INTO reviewer_llm_notes (
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
                        "reviewer_causal_review": {
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

            summary = _learned_vs_unlearned_trade_performance(
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
                                "trigger_type": "reversal_confirmed",
                                "entry_type": "initial",
                                "invalidation_level": 3220.0,
                            },
                            "pre_open_plan": {
                                "target_position_ratio": 0.08,
                                "target_return": 0.035,
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

    def test_reviewer_learning_report_writes_markdown_and_json(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO signal_template_performance (
                    id, config_id, ticker, side, signal_template, horizon_class, market_regime,
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
                paths = _write_reviewer_learning_report(
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
                self.assertIn("Positive Templates", markdown_text)
                self.assertIn("Neutral Accountability", markdown_text)
                self.assertEqual(payload["positive_templates"][0]["signal_template"], "long_reversal_confirmed_trend")
                self.assertEqual(payload["neutral_accountability"]["neutral_count"], 1)
        finally:
            conn.close()


class _FakeReviewerWeightDB:
    def get_signal_history(self, **kwargs):
        return []

    def get_analyst_performance(self, **kwargs):
        return []

    def get_signal_template_performance(self, **kwargs):
        return [
            {
                "horizon_class": "short",
                "signal_template": "long_reversal_confirmed_trend",
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
                "signal_template": "long_late_chase_range",
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
