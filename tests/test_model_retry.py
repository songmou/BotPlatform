from __future__ import annotations

import unittest

from src.core.modeling import (
    CanonicalMessage,
    ModelError,
    ModelResponse,
)
from src.core.modeling.retry import complete_with_retry


def _response(text: str) -> ModelResponse:
    return ModelResponse(CanonicalMessage("assistant", text))


class CompleteWithRetryTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def test_success_first_try_does_not_sleep(self):
        result = complete_with_retry(
            lambda: _response("好的"), profile_id="p", sleep=self._sleep
        )
        self.assertEqual(result.message.content, "好的")
        self.assertEqual(self.sleeps, [])

    def test_retries_transient_error_with_backoff(self):
        outcomes = [
            ModelError("超时", provider="deepseek", retryable=True),
            ModelError("超时", provider="deepseek", retryable=True),
            _response("恢复"),
        ]

        def call():
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        result = complete_with_retry(call, profile_id="p", sleep=self._sleep)
        self.assertEqual(result.message.content, "恢复")
        self.assertEqual(self.sleeps, [0.5, 1.0])

    def test_non_retryable_error_raises_immediately(self):
        calls = []

        def call():
            calls.append(1)
            raise ModelError("认证失败", provider="deepseek", retryable=False)

        with self.assertRaises(ModelError):
            complete_with_retry(call, profile_id="p", sleep=self._sleep)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_exhausted_attempts_reraises_last_error(self):
        calls = []

        def call():
            calls.append(1)
            raise ModelError("超时", provider="deepseek", retryable=True)

        with self.assertRaises(ModelError) as ctx:
            complete_with_retry(call, profile_id="p", attempts=3, sleep=self._sleep)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.sleeps, [0.5, 1.0])
        self.assertTrue(ctx.exception.retryable)

    def test_delay_is_capped(self):
        attempts = 6
        calls = []

        def call():
            calls.append(1)
            raise ModelError("超时", provider="deepseek", retryable=True)

        with self.assertRaises(ModelError):
            complete_with_retry(
                call, profile_id="p", attempts=attempts, sleep=self._sleep
            )
        self.assertEqual(len(calls), attempts)
        self.assertEqual(self.sleeps, [0.5, 1.0, 2.0, 4.0, 4.0])


if __name__ == "__main__":
    unittest.main()
