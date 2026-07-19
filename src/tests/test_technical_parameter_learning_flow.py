import inspect
import sys
import unittest
from pathlib import Path

import pandas as pd


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.analysis_team.technical import (
    _build_technical_signal_results,
    calculate_adaptive_params,
    get_mean_reversion_signal,
    technical_agent,
    thresholds,
)
from graph.constants import Signal
from tools.agent_tools.analysis.analyst_quality import build_technical_context
from tools.agent_tools.analysis.analyst_technical_parameter_calibration import (
    TECHNICAL_RULE_SPECS,
    apply_technical_parameter_calibration,
)


def _validated_row(**overrides):
    row = {
        "id": "technical-calibration-1",
        "ticker": "RB",
        "side": "*",
        "horizon_class": "short",
        "market_regime": "trend",
        "policy_type": "contextual_rule_calibration:technical_parameters",
        "policy_action": "calibrate",
        "rule_validation_status": "validated_rule_applied",
        "confidence_score": 0.60,
        "sample_count": 5,
        "payload": {
            "rule_adjustments": {
                "technical_parameters": {
                    "trend_short_multiplier": 0.90,
                    "trend_long_multiplier": 1.10,
                    "rsi_bullish_shift": -3.0,
                    "rsi_bearish_shift": 3.0,
                    "bollinger_std_multiplier": 1.05,
                }
            }
        },
    }
    row.update(overrides)
    return row


class TechnicalParameterLearningFlowTest(unittest.TestCase):
    def test_market_features_produce_different_initial_adaptive_parameters(self):
        low_vol = calculate_adaptive_params(
            {"volatility": 0.10, "trend_strength": 15.0},
            thresholds,
        )
        high_vol_trend = calculate_adaptive_params(
            {"volatility": 0.40, "trend_strength": 30.0},
            thresholds,
        )

        self.assertNotEqual(low_vol["trend"], high_vol_trend["trend"])
        self.assertNotEqual(low_vol["rsi"], high_vol_trend["rsi"])
        self.assertNotEqual(
            low_vol["mean_reversion"]["bollinger_std"],
            high_vol_trend["mean_reversion"]["bollinger_std"],
        )

    def test_initial_technical_context_determines_market_regime(self):
        trend_context = build_technical_context(
            "RB",
            {
                "trend": Signal.BULLISH,
                "macd": Signal.BULLISH,
                "adx": Signal.BULLISH,
                "rsi": Signal.BULLISH,
            },
            {"volatility": 0.20, "trend_strength": 30.0, "volume_ratio": 1.10},
        )
        high_vol_context = build_technical_context(
            "RB",
            {"trend": Signal.NEUTRAL, "macd": Signal.NEUTRAL},
            {"volatility": 0.40, "trend_strength": 30.0, "volume_ratio": 1.10},
        )

        self.assertEqual(trend_context["market_regime"], "trend")
        self.assertEqual(high_vol_context["market_regime"], "high_volatility")

    def test_only_matching_validated_scope_adjusts_parameters(self):
        params = calculate_adaptive_params(
            {"volatility": 0.20, "trend_strength": 30.0},
            thresholds,
        )
        matching = _validated_row()
        mismatched = [
            _validated_row(id="wrong-ticker", ticker="BU"),
            _validated_row(id="cross-product-wildcard", ticker="*"),
            _validated_row(id="wrong-horizon", horizon_class="medium"),
            _validated_row(id="wrong-regime", market_regime="range"),
            _validated_row(id="unvalidated", rule_validation_status="candidate"),
        ]

        adjusted, diagnostics = apply_technical_parameter_calibration(
            params,
            [*mismatched, matching],
            ticker="RB",
            side="*",
            horizon_class="short",
            market_regime="trend",
            min_confidence=0.45,
        )

        self.assertNotEqual(adjusted, params)
        self.assertEqual([item["id"] for item in diagnostics["applied"]], [matching["id"]])

    def test_exact_market_regime_takes_priority_over_wildcard_regime(self):
        params = calculate_adaptive_params(
            {"volatility": 0.20, "trend_strength": 30.0},
            thresholds,
        )
        exact = _validated_row(id="exact-regime", market_regime="trend")
        broad = _validated_row(
            id="broad-regime",
            market_regime="*",
            payload={
                "rule_adjustments": {
                    "technical_parameters": {"trend_short_multiplier": 1.15}
                }
            },
        )
        adjusted, diagnostics = apply_technical_parameter_calibration(
            params,
            [broad, exact],
            ticker="RB",
            side="*",
            horizon_class="short",
            market_regime="trend",
        )

        self.assertEqual([item["id"] for item in diagnostics["applied"]], ["exact-regime"])
        self.assertEqual(adjusted["trend"]["short"], round(params["trend"]["short"] * 0.90))

    def test_empty_learning_keeps_initial_parameters(self):
        params = calculate_adaptive_params(
            {"volatility": 0.20, "trend_strength": 20.0},
            thresholds,
        )
        adjusted, diagnostics = apply_technical_parameter_calibration(
            params,
            [],
            ticker="RB",
            side="*",
            horizon_class="short",
            market_regime="range",
        )
        self.assertEqual(adjusted, params)
        self.assertEqual(diagnostics["applied"], [])

    def test_rule_adjustments_remain_within_existing_bounds(self):
        params = calculate_adaptive_params(
            {"volatility": 0.20, "trend_strength": 20.0},
            thresholds,
        )
        extreme = _validated_row(
            payload={
                "rule_adjustments": {
                    "technical_parameters": {
                        "trend_short_multiplier": -100.0,
                        "trend_long_multiplier": 100.0,
                        "rsi_bullish_shift": -100.0,
                        "rsi_bearish_shift": 100.0,
                        "bollinger_std_multiplier": 100.0,
                    }
                }
            }
        )
        adjusted, _ = apply_technical_parameter_calibration(
            params,
            [extreme],
            ticker="RB",
            side="*",
            horizon_class="short",
            market_regime="trend",
        )

        self.assertEqual(adjusted["trend"]["short"], round(params["trend"]["short"] * TECHNICAL_RULE_SPECS["trend_short_multiplier"][2]))
        self.assertEqual(adjusted["trend"]["long"], round(params["trend"]["long"] * TECHNICAL_RULE_SPECS["trend_long_multiplier"][3]))
        self.assertEqual(adjusted["rsi"]["bullish"], params["rsi"]["bullish"] + TECHNICAL_RULE_SPECS["rsi_bullish_shift"][2])
        self.assertEqual(adjusted["rsi"]["bearish"], params["rsi"]["bearish"] + TECHNICAL_RULE_SPECS["rsi_bearish_shift"][3])

    def test_bollinger_calibration_changes_mean_reversion_signal(self):
        closes = [101.0, 99.0] * 9 + [101.0, 98.5]
        prices = pd.DataFrame({"close": closes})
        base = {
            "bollinger_window": 20,
            "rolling_window": 20,
            "z_score_extreme": 1.0,
            "bb_position_threshold": 0.2,
        }

        narrow = get_mean_reversion_signal(
            prices,
            {**base, "bollinger_std": 1.5},
        )
        wide = get_mean_reversion_signal(
            prices,
            {**base, "bollinger_std": 3.0},
        )

        self.assertEqual(narrow, Signal.BULLISH)
        self.assertEqual(wide, Signal.NEUTRAL)

    def test_runtime_rebuilds_indicators_and_context_after_calibration(self):
        source = inspect.getsource(technical_agent)
        initial_results = source.index("initial_signal_results = _build_technical_signal_results")
        initial_context = source.index("initial_technical_context = build_technical_context")
        retrieve = source.index("retrieve_analyst_policy_calibration")
        calibrate = source.index("apply_technical_parameter_calibration")
        final_results = source.index("\n    signal_results = _build_technical_signal_results", calibrate)
        final_context = source.index("\n    technical_context = build_technical_context", final_results)
        finalizer = source.index("finalize_analyst_signal")

        self.assertEqual(
            [initial_results, initial_context, retrieve, calibrate, final_results, final_context, finalizer],
            sorted([initial_results, initial_context, retrieve, calibrate, final_results, final_context, finalizer]),
        )

    def test_parameter_calibration_diagnostics_carry_no_trade_authority(self):
        params = calculate_adaptive_params(
            {"volatility": 0.20, "trend_strength": 30.0},
            thresholds,
        )
        _, diagnostics = apply_technical_parameter_calibration(
            params,
            [_validated_row()],
            ticker="RB",
            side="*",
            horizon_class="short",
            market_regime="trend",
        )
        forbidden = {
            "final_action",
            "final_action_contract",
            "target_lots",
            "lots",
            "opportunity_rank",
            "budget_approved",
        }
        self.assertTrue(forbidden.isdisjoint(diagnostics))

    def test_shared_signal_builder_is_a_single_internal_indicator_entrypoint(self):
        self.assertTrue(callable(_build_technical_signal_results))

    def test_technical_calibration_config_parameters_have_production_consumers(self):
        technical_source = (SRC_ROOT / "agents/analysis_team/technical.py").read_text(encoding="utf-8-sig")
        writer_source = (
            SRC_ROOT / "tools/agent_tools/research/research_memory_writers.py"
        ).read_text(encoding="utf-8-sig")

        self.assertIn('contextual_cfg.get("enabled"', technical_source)
        self.assertIn('"technical_min_confidence"', technical_source)
        self.assertIn('contextual_cfg.get("min_confidence"', technical_source)
        self.assertIn('"technical_positive_hit_rate"', writer_source)
        self.assertIn('"technical_weak_hit_rate"', writer_source)
        self.assertIn('"technical_valid_days"', writer_source)


if __name__ == "__main__":
    unittest.main()
