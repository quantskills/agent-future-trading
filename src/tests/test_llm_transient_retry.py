from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import BaseModel


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from llm import inference


class Output(BaseModel):
    value: str


class StatusError(RuntimeError):
    def __init__(self, status_code: int, detail: str = "private provider detail"):
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code


def _config() -> dict:
    return {
        "provider": "CodexOpenAI",
        "model": "test-model",
        "max_retries": 3,
        "max_concurrent_calls": 4,
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


def _model_with_side_effect(side_effect) -> Mock:
    model = Mock()
    model.with_structured_output.return_value = model
    model.invoke.side_effect = side_effect
    return model


class LLMTransientRetryTest(unittest.TestCase):
    def test_server_error_retries_past_bounded_limit_until_success(self):
        model = _model_with_side_effect(
            [StatusError(503)] * 10 + [Output(value="ok")]
        )

        with patch.object(inference, "get_model", return_value=model), patch.object(
            inference.time, "sleep"
        ) as sleep:
            result = inference.agent_call("private prompt", _config(), Output)

        self.assertEqual(result.value, "ok")
        self.assertEqual(model.invoke.call_count, 11)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [1, 2, 4, 8, 8, 8, 8, 8, 8, 8],
        )

    def test_rate_limit_retries_past_bounded_limit_until_success(self):
        model = _model_with_side_effect(
            [StatusError(429)] * 4 + [Output(value="ok")]
        )

        with patch.object(inference, "get_model", return_value=model), patch.object(
            inference.time, "sleep"
        ):
            result = inference.agent_call("private prompt", _config(), Output)

        self.assertEqual(result.value, "ok")
        self.assertEqual(model.invoke.call_count, 5)

    def test_timeout_and_connection_reset_are_transient(self):
        cases = (
            TimeoutError("private timeout detail"),
            ConnectionResetError("private connection reset detail"),
            ConnectionAbortedError("private connection aborted detail"),
        )
        for error in cases:
            with self.subTest(error_type=type(error).__name__):
                model = _model_with_side_effect(
                    [error] * 4 + [Output(value="ok")]
                )
                with patch.object(
                    inference, "get_model", return_value=model
                ), patch.object(inference.time, "sleep"):
                    result = inference.agent_call("private prompt", _config(), Output)

                self.assertEqual(result.value, "ok")
                self.assertEqual(model.invoke.call_count, 5)

    def test_every_5xx_status_is_server_error(self):
        for status_code in (500, 501, 502, 503, 504, 520, 599):
            with self.subTest(status_code=status_code):
                self.assertEqual(
                    inference._classify_llm_error(StatusError(status_code)),
                    "server_error",
                )

    def test_fatal_http_errors_fail_on_first_attempt(self):
        for status_code, expected_type in (
            (400, "invalid_request"),
            (401, "auth_error"),
            (403, "auth_error"),
            (404, "invalid_request"),
            (422, "invalid_request"),
        ):
            with self.subTest(status_code=status_code):
                model = _model_with_side_effect(
                    [StatusError(status_code), Output(value="must-not-run")]
                )
                with patch.object(
                    inference, "get_model", return_value=model
                ), patch.object(inference.time, "sleep") as sleep:
                    with self.assertRaisesRegex(
                        RuntimeError, f"llm_inference_failed:{expected_type}"
                    ):
                        inference.agent_call("private prompt", _config(), Output)

                self.assertEqual(model.invoke.call_count, 1)
                sleep.assert_not_called()

    def test_parse_error_remains_bounded(self):
        model = _model_with_side_effect(
            [RuntimeError("output_parsing_failure private response")] * 3
            + [Output(value="must-not-run")]
        )

        with patch.object(inference, "get_model", return_value=model), patch.object(
            inference.time, "sleep"
        ) as sleep:
            with self.assertRaisesRegex(
                RuntimeError, "llm_inference_failed:parse_error"
            ):
                inference.agent_call("private prompt", _config(), Output)

        self.assertEqual(model.invoke.call_count, 3)
        sleep.assert_not_called()

    def test_unknown_error_remains_bounded(self):
        model = _model_with_side_effect(
            [RuntimeError("private unknown response")] * 3
            + [Output(value="must-not-run")]
        )

        with patch.object(inference, "get_model", return_value=model), patch.object(
            inference.time, "sleep"
        ) as sleep:
            with self.assertRaisesRegex(
                RuntimeError, "llm_inference_failed:unknown"
            ):
                inference.agent_call("private prompt", _config(), Output)

        self.assertEqual(model.invoke.call_count, 3)
        sleep.assert_not_called()

    def test_normal_success_invokes_model_once(self):
        model = Mock()
        model.with_structured_output.return_value = model
        model.invoke.return_value = Output(value="ok")

        with patch.object(inference, "get_model", return_value=model), patch.object(
            inference.time, "sleep"
        ) as sleep:
            result = inference.agent_call("private prompt", _config(), Output)

        self.assertEqual(result.value, "ok")
        self.assertEqual(model.invoke.call_count, 1)
        sleep.assert_not_called()

    def test_transient_logs_do_not_leak_private_content(self):
        model = _model_with_side_effect(
            [StatusError(503, "private raw response with secret-token")] * 4
            + [Output(value="ok")]
        )

        with patch.object(inference, "get_model", return_value=model), patch.object(
            inference.time, "sleep"
        ), patch.object(inference.logger, "warning") as warning_log, patch.object(
            inference.logger, "error"
        ) as error_log:
            result = inference.agent_call(
                "private prompt with hidden context", _config(), Output
            )

        self.assertEqual(result.value, "ok")
        logged = " ".join(
            str(call.args[0])
            for mock in (warning_log, error_log)
            for call in mock.call_args_list
        )
        self.assertNotIn("private raw response", logged)
        self.assertNotIn("secret-token", logged)
        self.assertNotIn("private prompt", logged)
        self.assertNotIn("hidden context", logged)


if __name__ == "__main__":
    unittest.main()
