from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
from pydantic import ValidationError


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.constants import Signal
from graph.schema import AnalystSignal
from graph.workflow import _stable_phase1_failure_code
from llm import inference
from agents.analysis_team import technical
from tests.contract_test_fixtures import build_test_aec, build_test_data_usage
from tools.agent_tools.analysis.analyst_output_finalization import (
    finalize_analyst_signal,
)
from tools.agent_tools.analysis.analyst_product_price_behavior_profile import (
    build_profile_usage_contract,
    get_product_price_behavior_profile,
)
from tools.agent_tools.analysis.analyst_structured_output import (
    ANALYST_EXECUTION_PROFILE_MISSING,
    CommodityNewsAnalystOutput,
    FundamentalAnalystOutput,
    TechnicalAnalystOutput,
)
from tools.common.execution_trigger_semantics import (
    canonical_entry_invalidation_condition,
    entry_invalidation_contract_error,
)


def _llm_config() -> dict:
    return {
        "provider": "CodexOpenAI",
        "model": "test-model",
        "max_retries": 3,
        "structured_output_method": "json_mode",
        "failure_policy": {
            "parse_error": "retry_then_raise",
            "rate_limit": "retry_with_backoff",
            "auth_error": "raise",
            "invalid_request": "raise",
            "server_error": "retry_with_backoff",
            "unknown": "retry_then_raise",
        },
    }


def _technical_payload(*, timing: str = "", trigger: str = "15m close breaks above resistance") -> dict:
    return {
        "signal": "Bullish",
        "confidence": 0.66,
        "opportunity_state": "watch_for_trigger",
        "setup_type": "trend_breakout_setup",
        "entry_timing_signal": timing,
        "entry_trigger": trigger,
        "invalidation_present": True,
        "invalidation_level": 95.0,
        "exit_hint": "15m close below 95 invalidates the setup",
    }


class AnalystExecutionTimingOutputTest(unittest.TestCase):
    def _finalize(self, signal: AnalystSignal, *, ticker: str = "RB") -> AnalystSignal:
        profile = get_product_price_behavior_profile(ticker)
        usage = build_profile_usage_contract(ticker, "technical", profile)
        return finalize_analyst_signal(
            signal,
            quality_context={
                "sector": profile.get("sector", ""),
                "tradeability": "medium",
                "setup_type": "trend_breakout_setup",
                "setup_quality_ok": True,
                "market_regime": "trend",
                "atr_stop_distance": 3.0,
                "position_invalidation_reference_price": 100.0,
                "risk_flags": [],
            },
            full_config={"llm": {"provider": "test", "model": "test-model"}},
            analyst="technical",
            ticker=ticker,
            trading_date="2025-03-26",
            learning_context={},
            product_profile=profile,
            product_profile_usage=usage,
        )

    def test_llm_role_schemas_exclude_deterministic_freshness_and_system_unavailable_setup(self):
        self.assertIn("data_freshness", AnalystSignal.model_json_schema()["properties"])
        self.assertIn(
            "trigger_quality_score",
            AnalystSignal.model_json_schema()["properties"],
        )
        self.assertIn(
            "position_invalidation_level",
            AnalystSignal.model_json_schema()["properties"],
        )
        self.assertIn(
            "atr_stop_distance",
            AnalystSignal.model_json_schema()["properties"],
        )
        for output_model in (
            TechnicalAnalystOutput,
            FundamentalAnalystOutput,
            CommodityNewsAnalystOutput,
        ):
            with self.subTest(output_model=output_model.__name__):
                schema = output_model.model_json_schema()
                self.assertNotIn("data_freshness", schema["properties"])
                self.assertNotIn("atr_stop_distance", schema["properties"])
                setup_schema = schema["properties"]["setup_type"]
                self.assertNotIn(
                    "data_unavailable_no_trade",
                    setup_schema.get("enum", []),
                )
                self.assertIn("trigger_quality_score", schema["properties"])
        self.assertEqual(
            FundamentalAnalystOutput.model_json_schema()["properties"][
                "trigger_quality_score"
            ].get("const"),
            0.0,
        )

    def _technical_signal(self, *, timing: str, trigger: str, invalidation_level=95.0) -> AnalystSignal:
        return AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.66,
            setup_type="trend_breakout_setup",
            opportunity_type="short_timing",
            opportunity_state="watch_for_trigger",
            evidence_role="entry_timing",
            entry_timing_signal=timing,
            entry_trigger=trigger,
            invalidation_present=invalidation_level is not None,
            invalidation_level=invalidation_level,
            position_invalidation_level=92.0,
            atr_stop_distance=3.0,
            exit_hint=(
                "after fill, close below 92 requires position exit"
            ),
            factor_focus=["trend", "volume"],
            metadata={
                "data_usage_summary": build_test_data_usage("technical", "RB"),
            },
        )

    def test_entry_and_position_invalidation_are_distinct_after_finalization(self):
        contract = self._finalize(
            self._technical_signal(
                timing="breakout",
                trigger="15m close breaks above resistance with volume confirmation",
            )
        ).metadata["action_evidence_contract"]

        self.assertEqual(contract["invalidation_level"], 95.0)
        self.assertEqual(contract["position_invalidation_level"], 92.0)
        self.assertEqual(contract["atr_stop_distance"], 3.0)
        self.assertEqual(
            contract["invalidation_condition"],
            canonical_entry_invalidation_condition("breakout", "long"),
        )
        self.assertTrue(contract["invalidation_present"])

    def test_runtime_parameter_calibration_recomputes_prompt_context_without_changing_raw_atr(self):
        frame = pd.DataFrame(
            {
                "open": [100.0, 101.0, 102.0, 103.0, 104.0],
                "high": [102.0, 103.0, 104.0, 105.0, 106.0],
                "low": [98.0, 99.0, 100.0, 101.0, 102.0],
                "close": [101.0, 102.0, 103.0, 104.0, 105.0],
                "volume": [1000, 1100, 1200, 1300, 1400],
            },
            index=pd.bdate_range(end="2025-03-25", periods=5),
        )
        base_params = {"trend": {"short": 8, "medium": 21, "long": 55}}

        def run_with_rule(multiplier: float, record_id: str) -> tuple[str, dict]:
            captured: dict = {}
            router = Mock()
            router.get_daily_candles_df.return_value = frame
            policy_row = {
                "id": record_id,
                "ticker": "RB",
                "side": "*",
                "horizon_class": "short",
                "market_regime": "trend",
                "policy_type": "contextual_rule_calibration:technical_parameters",
                "policy_action": "calibrate",
                "rule_validation_status": "validated_rule_applied",
                "confidence_score": 0.70,
                "sample_count": 5,
                "payload": {
                    "rule_adjustments": {
                        "technical_parameters": {
                            "trend_short_multiplier": multiplier,
                        }
                    }
                },
            }

            def signal_results(_prices, params, gap_analysis):
                short = int(params["trend"]["short"])
                trend = Signal.BULLISH if short < 8 else Signal.BEARISH
                return {"trend": trend, "gap_analysis": gap_analysis}

            def technical_context(_ticker, results, features):
                trend = results["trend"]
                direction = "bullish" if trend == Signal.BULLISH else "bearish"
                return {
                    "ticker": "RB",
                    "sector": "ferrous",
                    "tradeability": "medium",
                    "setup_type": "trend_breakout_setup",
                    "setup_quality_ok": True,
                    "market_regime": "trend",
                    "dominant_direction": direction,
                    "risk_flags": [],
                    "indicator_votes": {"details": {"trend": trend.value}},
                    "features": features,
                }

            def llm_call(*, prompt, **_kwargs):
                captured["prompt"] = prompt
                return TechnicalAnalystOutput(
                    signal=Signal.BULLISH,
                    confidence=0.40,
                    justification="No current executable trigger.",
                    horizon_class="short",
                    expected_horizon_days=2,
                    setup_type="unknown",
                    opportunity_type="no_trade",
                    opportunity_state="no_opportunity",
                    entry_timing_signal="",
                    entry_trigger="",
                    invalidation_present=False,
                    position_invalidation_level=104.5,
                )

            full_config = {
                "llm": {"provider": "test", "model": "test-model"},
                "learning": {
                    "contextual_rule_calibration": {
                        "enabled": True,
                        "technical_min_confidence": 0.35,
                    }
                },
            }
            state = {
                "ticker": "RB",
                "trading_date": datetime(2025, 3, 26),
                "market_type": "china_futures",
                "pre_open_only": True,
                "info_cutoff": "pre_open",
                "config_id": "cfg-technical-calibration",
                "config": full_config,
                "full_config": full_config,
                "llm_config": full_config["llm"],
                "morning_price_context": SimpleNamespace(
                    base_price=104.0,
                    base_price_date="2025-03-25",
                    open_price=None,
                    prev_close_price=104.0,
                ),
            }
            with patch.object(technical, "Router", return_value=router), patch.object(
                technical, "get_db", return_value=Mock()
            ), patch.object(
                technical,
                "calculate_market_features",
                return_value={"volatility": 0.20, "trend_strength": 30.0},
            ), patch.object(
                technical, "calculate_adaptive_params", return_value=deepcopy(base_params)
            ), patch.object(
                technical, "_build_technical_signal_results", side_effect=signal_results
            ), patch.object(
                technical, "build_technical_context", side_effect=technical_context
            ), patch.object(
                technical,
                "retrieve_analyst_policy_calibration",
                return_value=([policy_row], {"status": "validated_past_only"}),
            ), patch.object(
                technical, "resolve_config_id", return_value="cfg-technical-calibration"
            ), patch.object(
                technical,
                "build_learning_context",
                return_value={"text": "", "selected_ids": [], "memory_trace": {}},
            ), patch.object(
                technical, "agent_call", side_effect=llm_call
            ), patch.object(technical.logger, "log_signal"):
                signal = technical.technical_agent(state)["analyst_signals"][0]
            return captured["prompt"], signal.metadata["action_evidence_contract"]

        tighter_prompt, tighter_contract = run_with_rule(
            0.85,
            "private-learning-id-tight",
        )
        wider_prompt, wider_contract = run_with_rule(
            1.15,
            "private-learning-id-wide",
        )

        self.assertIn("TR:UP", tighter_prompt)
        self.assertIn("TR:DOWN", wider_prompt)
        self.assertIn("trend.short: 8 -> 7", tighter_prompt)
        self.assertIn("trend.short: 8 -> 9", wider_prompt)
        self.assertIn("already include these bounded changes", tighter_prompt)
        self.assertNotIn("private-learning-id-tight", tighter_prompt)
        self.assertNotIn("private-learning-id-wide", wider_prompt)
        expected_atr = technical.calculate_raw_atr14(frame)
        self.assertAlmostEqual(tighter_contract["atr_stop_distance"], expected_atr)
        self.assertAlmostEqual(wider_contract["atr_stop_distance"], expected_atr)
        # 104.5 would be valid against the frame's latest close (105) but is
        # invalid against the formal morning base price (104), so it is cleared.
        self.assertIsNone(tighter_contract["position_invalidation_level"])
        self.assertIsNone(wider_contract["position_invalidation_level"])
        self.assertNotIn("position_invalidation_reference_price", tighter_contract)

    def test_exit_hint_and_atr_cannot_prove_pre_fill_invalidation(self):
        signal = self._technical_signal(
            timing="breakout",
            trigger="15m close breaks above resistance with volume confirmation",
            invalidation_level=None,
        )
        contract = self._finalize(signal).metadata["action_evidence_contract"]

        self.assertEqual(contract["opportunity_state"], "no_opportunity")
        self.assertFalse(contract["invalidation_present"])
        self.assertNotIn("invalidation_condition", contract)
        self.assertEqual(contract["position_invalidation_level"], 92.0)
        self.assertEqual(contract["atr_stop_distance"], 3.0)

    def test_entry_invalidation_contract_rejects_nonpositive_and_nonfinite_levels(self):
        condition = canonical_entry_invalidation_condition("breakout", "long")
        for level in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(level=level):
                self.assertEqual(
                    entry_invalidation_contract_error(
                        profile="breakout",
                        side="long",
                        invalidation_condition=condition,
                        invalidation_level=level,
                    ),
                    "execution_entry_invalidation_level_invalid",
                )

    def test_complete_watch_missing_or_invalid_profile_fails_explicitly(self):
        for timing in ("", "range_reversal"):
            with self.subTest(timing=timing or "missing"):
                with self.assertRaisesRegex(
                    ValueError,
                    f"^{ANALYST_EXECUTION_PROFILE_MISSING}$",
                ):
                    self._finalize(
                        self._technical_signal(
                            timing=timing,
                            trigger="15m close breaks above resistance with volume confirmation",
                        )
                    )

    def test_genuine_no_opportunity_boundaries_continue(self):
        signals = (
            AnalystSignal(
                agent_name="technical",
                signal=Signal.NEUTRAL,
                confidence=0.40,
                opportunity_state="no_opportunity",
                metadata={"data_usage_summary": build_test_data_usage("technical", "RB")},
            ),
            self._technical_signal(timing="", trigger=""),
            self._technical_signal(
                timing="",
                trigger="15m close breaks above resistance with volume confirmation",
                invalidation_level=None,
            ),
            self._technical_signal(
                timing="",
                trigger="15m close breaks above resistance with volume confirmation",
            ).model_copy(
                update={"opportunity_state": "no_opportunity"},
                deep=True,
            ),
        )

        for signal in signals:
            with self.subTest(signal=signal.signal.value, trigger=signal.entry_trigger):
                contract = self._finalize(signal).metadata["action_evidence_contract"]
                self.assertEqual(contract["opportunity_state"], "no_opportunity")
                self.assertEqual(contract["entry_timing_signal"], "")
                self.assertFalse(contract["trigger_valid"])
                self.assertFalse(contract["current_trigger_confirmed"])

    def test_role_models_enforce_only_their_existing_profiles(self):
        valid_watch = TechnicalAnalystOutput(
            **_technical_payload(timing="breakout")
        )
        self.assertEqual(valid_watch.entry_timing_signal, "breakout")

        for timing in ("", "range_reversal"):
            with self.subTest(technical_timing=timing or "missing"):
                with self.assertRaises(ValidationError) as raised:
                    TechnicalAnalystOutput(**_technical_payload(timing=timing))
                self.assertEqual(
                    raised.exception.errors()[0]["type"],
                    ANALYST_EXECUTION_PROFILE_MISSING,
                )

        no_trigger = TechnicalAnalystOutput(
            **_technical_payload(timing="", trigger="")
        )
        self.assertEqual(no_trigger.entry_timing_signal, "")
        research_only_payload = _technical_payload()
        research_only_payload["opportunity_state"] = "no_opportunity"
        research_only = TechnicalAnalystOutput(**research_only_payload)
        self.assertEqual(research_only.entry_timing_signal, "")

        self.assertEqual(FundamentalAnalystOutput().entry_timing_signal, "")
        with self.assertRaises(ValidationError):
            FundamentalAnalystOutput(trigger_quality_score=0.5)
        with self.assertRaises(ValidationError):
            FundamentalAnalystOutput(entry_timing_signal="breakout")
        with self.assertRaises(ValidationError):
            FundamentalAnalystOutput(
                invalidation_level=95.0,
                invalidation_present=True,
            )
        with self.assertRaises(ValidationError):
            FundamentalAnalystOutput(
                position_invalidation_level=92.0,
                exit_hint="after fill, fundamental reversal requires position exit",
            )

        news_payload = {
            "signal": "Bearish",
            "opportunity_state": "tradeable_candidate",
            "entry_trigger": "current event and price reaction are confirmed",
            "invalidation_present": True,
            "invalidation_level": 105.0,
        }
        with self.assertRaises(ValidationError) as raised:
            CommodityNewsAnalystOutput(**news_payload)
        self.assertEqual(
            raised.exception.errors()[0]["type"],
            ANALYST_EXECUTION_PROFILE_MISSING,
        )
        valid_news = CommodityNewsAnalystOutput(
            **news_payload,
            entry_timing_signal="event_immediate",
        )
        self.assertEqual(valid_news.entry_timing_signal, "event_immediate")

    def test_existing_parse_retry_recovers_when_profile_becomes_valid(self):
        model = Mock()
        model.with_structured_output.return_value = model
        model.invoke.side_effect = [
            _technical_payload(),
            _technical_payload(timing="range_reversal"),
            _technical_payload(timing="breakout"),
        ]

        with patch.object(inference, "get_model", return_value=model), patch.object(
            inference.time,
            "sleep",
        ):
            result = inference.agent_call(
                "private deterministic prompt",
                _llm_config(),
                TechnicalAnalystOutput,
            )

        self.assertEqual(result.entry_timing_signal, "breakout")
        self.assertEqual(model.invoke.call_count, 3)

    def test_three_identical_profile_errors_return_only_stable_code(self):
        model = Mock()
        model.with_structured_output.return_value = model
        model.invoke.side_effect = [_technical_payload()] * 3

        with patch.object(inference, "get_model", return_value=model), patch.object(
            inference.time,
            "sleep",
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                f"^{ANALYST_EXECUTION_PROFILE_MISSING}$",
            ):
                inference.agent_call(
                    "private deterministic prompt",
                    _llm_config(),
                    TechnicalAnalystOutput,
                )

        self.assertEqual(model.invoke.call_count, 3)

    def test_profile_error_logs_do_not_expose_model_fields(self):
        private_payload = _technical_payload()
        private_payload["entry_trigger"] = (
            "15m close breaks above resistance with private-model-content"
        )
        model = Mock()
        model.with_structured_output.return_value = model
        model.invoke.side_effect = [private_payload] * 3

        with patch.object(inference, "get_model", return_value=model), patch.object(
            inference.time,
            "sleep",
        ), patch.object(inference.logger, "warning") as warning_log, patch.object(
            inference.logger,
            "error",
        ) as error_log:
            with self.assertRaisesRegex(
                RuntimeError,
                f"^{ANALYST_EXECUTION_PROFILE_MISSING}$",
            ):
                inference.agent_call(
                    "private deterministic prompt",
                    _llm_config(),
                    TechnicalAnalystOutput,
                )

        logged = " ".join(
            str(call.args[0])
            for mock in (warning_log, error_log)
            for call in mock.call_args_list
        )
        self.assertNotIn("private-model-content", logged)
        self.assertNotIn("private deterministic prompt", logged)
        self.assertEqual(
            error_log.call_args.args[0],
            ANALYST_EXECUTION_PROFILE_MISSING,
        )

    def test_mixed_parse_errors_do_not_masquerade_as_profile_error(self):
        model = Mock()
        model.with_structured_output.return_value = model
        model.invoke.side_effect = [
            _technical_payload(),
            RuntimeError("output_parsing_failure private content"),
            _technical_payload(),
        ]

        with patch.object(inference, "get_model", return_value=model), patch.object(
            inference.time,
            "sleep",
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "^llm_inference_failed:parse_error$",
            ):
                inference.agent_call(
                    "private deterministic prompt",
                    _llm_config(),
                    TechnicalAnalystOutput,
                )

    def test_workflow_preserves_registered_safe_profile_error(self):
        self.assertEqual(
            _stable_phase1_failure_code(
                RuntimeError(ANALYST_EXECUTION_PROFILE_MISSING),
                default="analyst_phase1_analysis_failed",
            ),
            ANALYST_EXECUTION_PROFILE_MISSING,
        )


if __name__ == "__main__":
    unittest.main()
