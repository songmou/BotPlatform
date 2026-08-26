"""WeChat QR login manager tests (migrated from test_publish.py)."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from src.core.integrations.ilink import ILinkError
from src.core.services.wechat_login import WeChatLoginManager


class _FakeCredentials:
    bot_id = "bot-1"


class _FakeLoginClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False

    def login(self, show_qr, status_changed=None, stop_event=None):
        show_qr("qr-content-123")
        if status_changed:
            status_changed("scaned")
        if stop_event is not None and stop_event.is_set():
            return None
        if self.fail:
            raise ILinkError("二维码已过期")
        return _FakeCredentials()

    def close(self):
        self.closed = True


class _BlockingLoginClient:
    """Simulates a long-running QR scan; exits promptly when cancelled."""

    def __init__(self) -> None:
        self.closed = False

    def login(self, show_qr, status_changed=None, stop_event=None):
        show_qr("qr-blocked")
        if stop_event is not None:
            stop_event.wait(timeout=30)
        return None

    def close(self):
        self.closed = True


class WeChatLoginManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cred_path = Path(self._tmp.name) / "credentials.json"
        self.saved = []

    def _make(self, fail: bool = False, **kwargs) -> WeChatLoginManager:
        client = _FakeLoginClient(fail=fail)
        self.client = client
        return WeChatLoginManager(
            client_factory=lambda: client,
            credentials_saver=lambda creds, path: (
                self.saved.append((creds, path)),
                Path(path).write_text("{}", encoding="utf-8"),
            ),
            credentials_path=self.cred_path,
            **kwargs,
        )

    def _wait_final(self, manager: WeChatLoginManager) -> dict:
        for _ in range(100):
            status = manager.status()
            if status["state"] in {"success", "failed"}:
                return status
            time.sleep(0.02)
        self.fail("登录线程未在预期时间内结束")

    def test_successful_login_saves_credentials(self):
        manager = self._make()
        self.assertFalse(manager.is_connected())
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "success")
        self.assertEqual(status["bot_id"], "bot-1")
        self.assertTrue(status["connected"])
        self.assertEqual(self.saved[0][1], self.cred_path)
        self.assertTrue(self.client.closed)

    def test_failed_login_reports_error(self):
        manager = self._make(fail=True)
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "failed")
        self.assertIn("二维码已过期", status["error"])
        self.assertFalse(status["connected"])

    def test_qr_exposed_as_data_url(self):
        manager = self._make(fail=True)
        manager._on_qr("qr-content-123")
        self.assertTrue(manager.status()["qr"].startswith("data:image/png;base64,"))

    def test_connected_checker_overrides_file_probe(self):
        state = {"connected": False}
        manager = self._make(connected_checker=lambda: state["connected"])
        self.assertFalse(manager.is_connected())
        state["connected"] = True
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "success")
        self.assertTrue(status["connected"])
        state["connected"] = False
        self.assertFalse(manager.is_connected())

    def test_connected_checker_errors_read_as_disconnected(self):
        def broken() -> bool:
            raise RuntimeError("查询失败")

        manager = self._make(connected_checker=broken)
        self.assertFalse(manager.is_connected())

    def _make_blocking(self) -> WeChatLoginManager:
        client = _BlockingLoginClient()
        self.blocking_client = client
        return WeChatLoginManager(
            client_factory=lambda: client,
            credentials_saver=lambda creds, path: None,
        )

    def test_start_cancels_previous_thread_and_restarts(self):
        manager = self._make_blocking()
        manager.start()
        self.assertTrue(manager._thread.is_alive())
        first_thread = manager._thread
        status = manager.start()
        self.assertEqual(status["state"], "pending")
        first_thread.join(timeout=5)
        self.assertFalse(first_thread.is_alive())
        self.assertIsNot(first_thread, manager._thread)
        self.assertTrue(manager._thread.is_alive())
        manager.cancel()
        manager._thread.join(timeout=5)

    def test_cancel_stops_login_thread(self):
        manager = self._make_blocking()
        manager.start()
        self.assertTrue(manager._thread.is_alive())
        manager.cancel()
        manager._thread.join(timeout=5)
        self.assertFalse(manager._thread.is_alive())
        self.assertTrue(self.blocking_client.closed)

    def test_active_session_hides_stale_connected(self):
        client = _BlockingLoginClient()
        manager = WeChatLoginManager(
            client_factory=lambda: client,
            credentials_saver=lambda creds, path: None,
            connected_checker=lambda: True,
        )
        # Before any session, connected reflects the persisted credential.
        self.assertTrue(manager.status()["connected"])
        manager.start()
        status = manager.status()
        self.assertTrue(status["active_login"])
        self.assertFalse(status["connected"])
        manager.cancel()
        manager._thread.join(timeout=5)

    def test_start_while_previous_thread_alive_does_not_deadlock(self):
        manager = self._make_blocking()
        manager.start()
        self.assertTrue(manager._thread.is_alive())
        # The previous login thread is still alive; start() must return
        # promptly (it cancels the old thread and joins with a timeout)
        # instead of deadlocking on the state lock.
        started_at = time.time()
        result = manager.start()
        self.assertLess(time.time() - started_at, 2.0)
        self.assertEqual(result["state"], "pending")
        manager.cancel()
        manager._thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
