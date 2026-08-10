"""Feishu scan-to-create agent app registration manager tests."""

from __future__ import annotations

import time
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from src.core.services.feishu_registration import FeishuRegistrationManager


class _FakeResponse:
    def __init__(self, payload: Any, *, raises: Optional[Exception] = None) -> None:
        self.payload = payload
        self.raises = raises

    def json(self) -> Any:
        if self.raises is not None:
            raise self.raises
        return self.payload


def _begin_payload(**overrides: Any) -> Dict[str, Any]:
    payload = {
        "device_code": "dev-123",
        "user_code": "ABCD-EFGH",
        "verification_uri_complete": (
            "https://open.feishu.cn/page/launcher?user_code=ABCD-EFGH"
        ),
        "expires_in": 3600,
        "interval": 0,
    }
    payload.update(overrides)
    return payload


class _FakeClient:
    def __init__(self, poll_script: Optional[List[Any]] = None) -> None:
        self.poll_script = list(poll_script or [])
        self.calls: List[Dict[str, str]] = []
        self.closed = False
        self.begin_payload: Dict[str, Any] = _begin_payload()
        self.begin_raises: Optional[Exception] = None

    def post(self, url: str, data: Optional[Dict[str, str]] = None, headers=None) -> _FakeResponse:
        params = dict(data or {})
        self.calls.append(params)
        action = params.get("action")
        if action == "begin":
            return _FakeResponse(self.begin_payload, raises=self.begin_raises)
        if action == "poll":
            if not self.poll_script:
                return _FakeResponse({})
            return self.poll_script.pop(0)
        return _FakeResponse({})

    def close(self) -> None:
        self.closed = True


class FeishuRegistrationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved: List[Dict[str, Any]] = []

    def _make(
        self,
        poll_script: Optional[List[Any]] = None,
        **kwargs: Any,
    ) -> FeishuRegistrationManager:
        client = _FakeClient(poll_script)
        self.client = client
        return FeishuRegistrationManager(
            client_factory=lambda: client,
            **kwargs,
        )

    def _wait_final(self, manager: FeishuRegistrationManager) -> Dict[str, Any]:
        for _ in range(1000):
            status = manager.status()
            if status["state"] in {"success", "failed"}:
                return status
            time.sleep(0.02)
        self.fail("注册线程未在预期时间内结束")

    def test_successful_registration_stages_credentials(self) -> None:
        manager = self._make([
            _FakeResponse({"error": "authorization_pending"}),
            _FakeResponse(
                {
                    "client_id": "cli_a5d6",
                    "client_secret": "secret-1",
                    "user_info": {"name": "测试机器人", "open_id": "ou_123"},
                }
            ),
        ])
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "success")
        self.assertEqual(status["app_id"], "cli_a5d6")
        self.assertEqual(status["user_name"], "测试机器人")
        self.assertTrue(status["connected"])
        self.assertTrue(status["qr"] == "")
        self.assertEqual(manager.pending_holder["pending"]["client_id"], "cli_a5d6")
        self.assertEqual(
            manager.pending_holder["pending"]["client_secret"], "secret-1"
        )
        self.assertTrue(self.client.closed)

    def test_successful_registration_without_pending_poll(self) -> None:
        manager = self._make(
            [
                _FakeResponse(
                    {
                        "client_id": "cli_x",
                        "client_secret": "secret-x",
                        "user_info": {},
                    }
                )
            ]
        )
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "success")
        self.assertEqual(manager.pending_holder["pending"]["client_id"], "cli_x")

    def test_credentials_saver_receives_credentials(self) -> None:
        def save(credentials: Dict[str, Any]) -> None:
            self.saved.append(credentials)

        manager = self._make(
            [
                _FakeResponse(
                    {
                        "client_id": "cli_y",
                        "client_secret": "secret-y",
                        "user_info": {"name": "张三"},
                    }
                )
            ],
            credentials_saver=save,
        )
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "success")
        self.assertEqual(
            self.saved,
            [
                {
                    "client_id": "cli_y",
                    "client_secret": "secret-y",
                    "user_info": {"name": "张三"},
                }
            ],
        )

    def test_access_denied_fails(self) -> None:
        manager = self._make([_FakeResponse({"error": "access_denied"})])
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"], "用户拒绝创建飞书应用")
        self.assertFalse(status["connected"])
        self.assertNotIn("pending", manager.pending_holder)

    def test_expired_token_fails(self) -> None:
        manager = self._make([_FakeResponse({"error": "expired_token"})])
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"], "二维码已过期，请刷新后重新扫码")

    def test_slow_down_increases_interval_and_recovers(self) -> None:
        manager = self._make(
            [
                _FakeResponse({"error": "slow_down"}),
                _FakeResponse({"client_id": "cli-z", "client_secret": "secret-z"}),
            ]
        )
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "success")
        self.assertEqual(status["app_id"], "cli-z")

    def test_poll_timeout_fails(self) -> None:
        self.client_holder = {}

        def make_client() -> Any:
            client = _FakeClient()
            client.begin_payload = _begin_payload(expires_in=0)
            self.client_holder["client"] = client
            return client

        manager = FeishuRegistrationManager(client_factory=make_client)
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"], "二维码已过期，请刷新后重新扫码")

    def test_begin_network_error_fails(self) -> None:
        def make_client() -> Any:
            client = _FakeClient()
            client.begin_raises = RuntimeError("连接失败")
            return client

        manager = FeishuRegistrationManager(client_factory=make_client)
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "failed")
        self.assertIn("飞书注册失败", status["error"])

    def test_begin_missing_verification_uri_fails(self) -> None:
        def make_client() -> Any:
            client = _FakeClient()
            client.begin_payload = {"device_code": "dev-1", "interval": 0}
            return client

        manager = FeishuRegistrationManager(client_factory=make_client)
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "failed")
        self.assertIn("未返回二维码地址", status["error"])

    def test_qr_exposed_as_data_url(self) -> None:
        manager = self._make([])
        manager.start()
        for _ in range(100):
            status = manager.status()
            if status["qr"]:
                break
            time.sleep(0.02)
        self.assertTrue(status["qr"].startswith("data:image/png;base64,"))
        self.assertEqual(status["state"], "pending")

    def test_cancel_stops_poll_loop_without_staging(self) -> None:
        manager = self._make([], credentials_saver=lambda c: self.saved.append(c))
        manager.start()
        for _ in range(100):
            if manager.status()["qr"]:
                break
            time.sleep(0.02)
        manager.cancel()
        for _ in range(1000):
            thread = manager._thread
            if thread is None or not thread.is_alive():
                break
            time.sleep(0.02)
        self.assertIsNotNone(manager._thread)
        self.assertFalse(manager._thread.is_alive())
        self.assertEqual(manager.pending_holder, {})
        self.assertEqual(self.saved, [])
        self.assertEqual(manager.status()["state"], "pending")
        self.assertTrue(self.client.closed)

    def test_qr_content_includes_app_name(self) -> None:
        captured: Dict[str, str] = {}

        def fake_qr(content: str) -> str:
            captured["content"] = content
            return "data:image/png;base64,AAAA"

        manager = self._make([])
        with mock.patch(
            "src.core.services.feishu_registration._qr_png_data_url", fake_qr
        ):
            manager.start("我的机器人")
            for _ in range(100):
                if captured:
                    break
                time.sleep(0.02)
        self.assertIn("user_code=ABCD-EFGH", captured["content"])
        self.assertIn("from=sdk", captured["content"])
        self.assertIn("source=python", captured["content"])
        self.assertIn("tp=sdk", captured["content"])
        self.assertIn("name=%E6%88%91%E7%9A%84%E6%9C%BA%E5%99%A8%E4%BA%BA", captured["content"])

    def test_invalid_json_response_fails(self) -> None:
        def make_client() -> Any:
            client = _FakeClient()
            client.begin_raises = ValueError("bad json")
            return client

        manager = FeishuRegistrationManager(client_factory=make_client)
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"], "飞书响应格式无效")

    def test_start_while_previous_thread_alive_does_not_deadlock(self) -> None:
        import threading

        entered = threading.Event()

        class _BlockingClient:
            def __init__(self) -> None:
                self.closed = False

            def post(self, url, data=None, headers=None):
                entered.set()
                time.sleep(30)

            def close(self) -> None:
                self.closed = True

        manager = FeishuRegistrationManager(client_factory=lambda: _BlockingClient())
        manager.start()
        self.assertTrue(entered.wait(timeout=5))
        started_at = time.time()
        result = manager.start()
        self.assertLess(time.time() - started_at, 2.0)
        self.assertEqual(result["state"], "pending")

    def test_connected_checker_overrides_app_id(self) -> None:
        state = {"connected": False}
        manager = self._make([], connected_checker=lambda: state["connected"])
        self.assertFalse(manager.is_connected())
        state["connected"] = True
        self.assertTrue(manager.is_connected())

    def test_connected_checker_errors_read_as_disconnected(self) -> None:
        def broken() -> bool:
            raise RuntimeError("查询失败")

        manager = self._make([], connected_checker=broken)
        self.assertFalse(manager.is_connected())


if __name__ == "__main__":
    unittest.main()
