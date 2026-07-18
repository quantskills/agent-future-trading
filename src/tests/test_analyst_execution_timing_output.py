from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.constants import Signal
from graph.schema import AnalystSignal
from graph.workflow import _stable_phase1_failure_code
from llm import inference
from tests.contract_test_fixtures import build_test_aec
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
    @staticmethod
    def _data_usage(analyst: str, ticker: str) -> dict:
        return build_test_aec(
            analyst,
            ticker=ticker,
            trading_date="2025-03-26",
        )["data_usage_summary"]

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
            exit_hint=(
                "15m close below 95 invalidates the setup"
                if invalidation_level is not None
                else ""
            ),
            factor_focus=["trend", "volume"],
            metadata={
                "data_usage_summary": self._data_usage("technical", "RB"),
                **(
                    {"invalidation_condition": "15m close below 95 invalidates the setup"}
                    if invalidation_level is not None
                    else {}
                ),
            },
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
                metadata={"data_usage_summary": self._data_usage("technical", "RB")},
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
            FundamentalAnalystOutput(entry_timing_signal="breakout")

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
