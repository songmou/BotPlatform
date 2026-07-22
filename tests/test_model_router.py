from __future__ import annotations

import unittest

from src.core.modeling import (
    CanonicalMessage,
    ModelCapabilities,
    ModelError,
    ModelIdentity,
    ModelRequest,
    ModelResponse,
    ModelRouter,
)


class FakeClient:
    def __init__(self, profile_id, provider, model, responses, *, vision=False):
        self.identity = ModelIdentity(profile_id, provider, model)
        self.capabilities = ModelCapabilities(
            tools=True, vision=vision, reasoning=profile_id == "deepseek_pro"
        )
        self.responses = list(responses)
        self.calls = []
        self.ready_error = None
        self.closed = False

    def ensure_ready(self):
        if self.ready_error:
            raise self.ready_error

    def complete(self, request):
        self.calls.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return ModelResponse(
            CanonicalMessage("assistant", result),
            actual_model=self.identity.configured_model,
        )

    def close(self):
        self.closed = True


def request(image=None):
    return ModelRequest(
        messages=[CanonicalMessage("user", "hello")],
        image=image,
    )


class ModelRouterTests(unittest.TestCase):
    def make_router(self, local_responses, flash_responses, pro_responses=None):
        self.now = 100.0
        self.local = FakeClient(
            "ollama_local", "ollama", "local", local_responses, vision=True
        )
        self.flash = FakeClient(
            "deepseek_cloud", "deepseek", "deepseek-v4-flash", flash_responses
        )
        self.pro = FakeClient(
            "deepseek_pro",
            "deepseek",
            "deepseek-v4-pro",
            pro_responses or ["pro"],
        )
        self.logs = []
        return ModelRouter(
            {
                "ollama_local": self.local,
                "deepseek_cloud": self.flash,
                "deepseek_pro": self.pro,
            },
            primary_profile_id="ollama_local",
            fallback_profile_id="deepseek_cloud",
            cooldown_seconds=60,
            monotonic=lambda: self.now,
            fallback_logger=lambda *values: self.logs.append(values),
        )

    def test_auto_fails_over_for_any_model_error_and_recovers_after_cooldown(self):
        router = self.make_router(
            [ModelError("local failed", provider="ollama"), "local recovered"],
            ["flash one", "flash during cooldown"],
        )
        first = router.session("auto")
        self.assertEqual(first.complete(request()).message.content, "flash one")
        self.assertEqual(first.profile_id, "deepseek_cloud")
        self.assertTrue(router.cooling_down)
        self.assertEqual(len(self.logs), 1)

        second = router.session("auto")
        self.assertEqual(second.complete(request()).message.content, "flash during cooldown")
        self.assertEqual(len(self.local.calls), 1)

        self.now += 61
        third = router.session("auto")
        self.assertEqual(third.complete(request()).message.content, "local recovered")
        self.assertEqual(len(self.local.calls), 2)

    def test_manual_modes_are_strict_and_double_failure_surfaces(self):
        router = self.make_router(
            [
                ModelError("local failed", provider="ollama"),
                ModelError("local failed again", provider="ollama"),
            ],
            [ModelError("cloud failed", provider="deepseek")],
        )
        with self.assertRaisesRegex(ModelError, "local failed"):
            router.session("local").complete(request())
        self.assertEqual(self.flash.calls, [])

        with self.assertRaisesRegex(ModelError, "cloud failed"):
            router.session("auto").complete(request())
        self.assertEqual(len(self.flash.calls), 1)
        self.assertEqual(router.session("pro").complete(request()).message.content, "pro")

    def test_images_never_use_deepseek(self):
        router = self.make_router(
            [ModelError("local image failed", provider="ollama")],
            ["must not run"],
        )
        with self.assertRaisesRegex(ModelError, "local image failed"):
            router.session("auto", has_image=True).complete(request(b"image"))
        self.assertEqual(self.flash.calls, [])
        with self.assertRaisesRegex(ModelError, "不支持图片"):
            router.session("flash", has_image=True)
        with self.assertRaisesRegex(ModelError, "不支持图片"):
            router.session("pro", has_image=True)

    def test_startup_local_failure_uses_fallback_and_all_clients_close(self):
        router = self.make_router([], [])
        self.local.ready_error = ModelError("not ready", provider="ollama")
        router.ensure_ready()
        self.assertTrue(router.cooling_down)
        router.close()
        self.assertTrue(self.local.closed)
        self.assertTrue(self.flash.closed)
        self.assertTrue(self.pro.closed)


if __name__ == "__main__":
    unittest.main()
