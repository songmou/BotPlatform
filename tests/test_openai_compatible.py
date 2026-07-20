from __future__ import annotations

import io
import json
import unittest

import httpx
from PIL import Image

from modeling import (
    CanonicalMessage,
    CanonicalToolCall,
    GenerationOptions,
    ModelCapabilities,
    ModelError,
    ModelRequest,
)
from modeling.adapters.openai_compatible import OpenAICompatibleAdapter


class OpenAICompatibleAdapterTests(unittest.TestCase):
    def adapter(self, handler, **overrides):
        options = dict(
            profile_id="cloud",
            provider="compatible",
            base_url="https://example.com/v1",
            api_key="secret-key",
            model="example-model",
            temperature=0.2,
            max_tokens=100,
            timeout_seconds=30,
            capabilities=ModelCapabilities(tools=True, vision=False),
            request_extra={"top_p": 0.9},
            assistant_passthrough_fields=["reasoning_content"],
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        options.update(overrides)
        return OpenAICompatibleAdapter(**options)

    def test_request_tool_ids_extensions_and_usage_round_trip(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers["Authorization"]
            captured["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                headers={"x-request-id": "req-123"},
                json={
                    "id": "chat-123",
                    "model": "actual-model",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "完成",
                                "reasoning_content": "temporary reasoning",
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 3,
                        "total_tokens": 15,
                    },
                },
            )

        adapter = self.adapter(handler)
        call = CanonicalToolCall("call-1", "clock", {"zone": "Asia/Shanghai"})
        response = adapter.complete(
            ModelRequest(
                messages=[
                    CanonicalMessage("user", "几点？"),
                    CanonicalMessage(
                        "assistant",
                        tool_calls=[call],
                        extensions={"reasoning_content": "keep this"},
                    ),
                    CanonicalMessage(
                        "tool", '{"ok":true}', tool_call_id="call-1"
                    ),
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "clock",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )
        )

        self.assertEqual(captured["url"], "https://example.com/v1/chat/completions")
        self.assertEqual(captured["authorization"], "Bearer secret-key")
        payload = captured["payload"]
        self.assertEqual(payload["top_p"], 0.9)
        self.assertEqual(payload["max_tokens"], 100)
        self.assertEqual(payload["messages"][1]["reasoning_content"], "keep this")
        self.assertEqual(
            json.loads(payload["messages"][1]["tool_calls"][0]["function"]["arguments"]),
            {"zone": "Asia/Shanghai"},
        )
        self.assertEqual(payload["messages"][2]["tool_call_id"], "call-1")
        self.assertNotIn("secret-key", json.dumps(payload))
        self.assertEqual(response.actual_model, "actual-model")
        self.assertEqual(response.request_id, "req-123")
        self.assertEqual(response.finish_reason, "stop")
        self.assertEqual(response.usage.total_tokens, 15)
        self.assertEqual(
            response.message.extensions["reasoning_content"], "temporary reasoning"
        )

    def test_parallel_tool_calls_are_normalized(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "a",
                                        "type": "function",
                                        "function": {"name": "one", "arguments": "{}"},
                                    },
                                    {
                                        "id": "b",
                                        "type": "function",
                                        "function": {
                                            "name": "two",
                                            "arguments": '{"value":2}',
                                        },
                                    },
                                ],
                            }
                        }
                    ]
                },
            )

        response = self.adapter(handler).complete(
            ModelRequest(messages=[CanonicalMessage("user", "run")])
        )
        self.assertEqual([call.call_id for call in response.message.tool_calls], ["a", "b"])
        self.assertEqual(response.message.tool_calls[1].arguments, {"value": 2})

    def test_vision_data_url_and_capability_gate(self) -> None:
        image = io.BytesIO()
        Image.new("RGB", (1, 1), "white").save(image, format="PNG")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )

        disabled = self.adapter(handler)
        with self.assertRaisesRegex(ModelError, "未启用图片能力"):
            disabled.complete(
                ModelRequest(
                    messages=[CanonicalMessage("user", "look")], image=image.getvalue()
                )
            )
        self.assertEqual(captured, {})

        enabled = self.adapter(
            handler, capabilities=ModelCapabilities(tools=True, vision=True)
        )
        enabled.complete(
            ModelRequest(
                messages=[CanonicalMessage("user", "look")], image=image.getvalue()
            )
        )
        image_url = captured["messages"][0]["content"][1]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/png;base64,"))

    def test_tools_disabled_and_provider_errors_are_sanitized(self) -> None:
        captured = {}

        def success(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )

        self.adapter(
            success, capabilities=ModelCapabilities(tools=False, vision=False)
        ).complete(
            ModelRequest(
                messages=[CanonicalMessage("user", "hi")],
                tools=[{"type": "function"}],
            )
        )
        self.assertNotIn("tools", captured)

        def unauthorized(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="secret-key provider detail")

        with self.assertRaises(ModelError) as caught:
            self.adapter(unauthorized).complete(
                ModelRequest(messages=[CanonicalMessage("user", "hi")])
            )
        self.assertIn("认证失败", str(caught.exception))
        self.assertNotIn("secret-key", str(caught.exception))

    def test_reasoning_override_controls_configured_deepseek_thinking(self) -> None:
        payloads = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )

        adapter = self.adapter(
            handler,
            request_extra={
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            },
        )
        adapter.complete(
            ModelRequest(
                messages=[CanonicalMessage("user", "hi")],
                generation=GenerationOptions(reasoning=False),
            )
        )
        self.assertEqual(payloads[-1]["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", payloads[-1])

        adapter.complete(
            ModelRequest(
                messages=[CanonicalMessage("user", "hi")],
                generation=GenerationOptions(reasoning=True),
            )
        )
        self.assertEqual(payloads[-1]["thinking"], {"type": "enabled"})
        self.assertEqual(payloads[-1]["reasoning_effort"], "high")


if __name__ == "__main__":
    unittest.main()
