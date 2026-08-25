from __future__ import annotations

import unittest

from src.core.modeling import (
    CanonicalMessage,
    CanonicalToolCall,
    ModelCapabilities,
    ModelError,
    ModelIdentity,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from src.core.modeling.observability import ObservedModelClient


class FakeClient:
    identity = ModelIdentity("profile", "provider", "configured")
    capabilities = ModelCapabilities(tools=True)

    def __init__(self, error=False):
        self.error = error

    def ensure_ready(self):
        pass

    def complete(self, _request):
        if self.error:
            raise ModelError("safe", provider="provider")
        return ModelResponse(
            CanonicalMessage(
                "assistant",
                tool_calls=[CanonicalToolCall("id", "tool", {})],
            ),
            actual_model="actual",
            usage=ModelUsage(10, 2, 12),
            request_id="request",
        )

    def close(self):
        pass


class ObservabilityTests(unittest.TestCase):
    def test_success_and_failure_log_structured_content(self) -> None:
        logs = []

        def logger(*values):
            logs.append(values)

        request = ModelRequest(messages=[CanonicalMessage("user", "private prompt")])
        ObservedModelClient(FakeClient(), logger).complete(request)
        self.assertEqual(logs[0][1], "actual")
        self.assertEqual(logs[0][2], "成功")
        self.assertEqual(logs[0][5], 1)
        self.assertEqual(logs[0][6], "request")
        self.assertEqual(logs[0][11], request)
        self.assertEqual(logs[0][12].actual_model, "actual")

        with self.assertRaises(ModelError):
            ObservedModelClient(FakeClient(error=True), logger).complete(request)
        self.assertEqual(logs[1][2], "失败")
        self.assertEqual(logs[1][11], request)
        self.assertIsNone(logs[1][12])


if __name__ == "__main__":
    unittest.main()
