"""Panel-driven WeChat (iLink) QR login session manager."""

from __future__ import annotations

import base64
import io
import threading
from typing import Any, Callable, Dict, Optional

from src.core.application.bot import save_credentials
from src.core.integrations.ilink import ILinkClient, ILinkError
from src.core.paths import channel_credentials_path

DEFAULT_CHANNEL_ID = "wechat-main"


def _qr_png_data_url(content: str) -> str:
    import qrcode

    qr = qrcode.QRCode(border=2, box_size=6)
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(
        "ascii"
    )


class WeChatLoginManager:
    """Run one interactive iLink QR login at a time in a background thread.

    States: idle -> pending -> scanned -> success | failed. The QR image is
    exposed as a data URL so the panel can poll and render it inline.
    """

    def __init__(
        self,
        channel_id: str = DEFAULT_CHANNEL_ID,
        client_factory: Callable[[], Any] = ILinkClient,
        credentials_saver: Callable[[Any, Any], None] = save_credentials,
        credentials_path: Optional[Any] = None,
        connected_checker: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.channel_id = channel_id
        self.credentials_path = (
            credentials_path
            if credentials_path is not None
            else channel_credentials_path(channel_id)
        )
        self._client_factory = client_factory
        self._save = credentials_saver
        self._connected_checker = connected_checker
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._state = "idle"
        self._qr_data_url = ""
        self._error = ""
        self._bot_id = ""

    def is_connected(self) -> bool:
        if self._connected_checker is not None:
            try:
                return bool(self._connected_checker())
            except Exception:  # noqa: BLE001 - status polling must not fail
                return False
        return self.credentials_path.exists()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "connected": self.is_connected(),
                "state": self._state,
                "qr": self._qr_data_url,
                "error": self._error,
                "bot_id": self._bot_id,
            }

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            self._state = "pending"
            self._qr_data_url = ""
            self._error = ""
            self._thread = threading.Thread(
                target=self._run, name="wechat-panel-login", daemon=True
            )
            self._thread.start()
        return self.status()

    def _on_qr(self, content: str) -> None:
        data_url = _qr_png_data_url(content)
        with self._lock:
            self._qr_data_url = data_url
            self._state = "pending"

    def _on_status(self, status: str) -> None:
        with self._lock:
            if status == "scaned":
                self._state = "scanned"
            elif status == "expired":
                self._state = "pending"

    def _run(self) -> None:
        client = None
        try:
            client = self._client_factory()
            credentials = client.login(self._on_qr, status_changed=self._on_status)
            self._save(credentials, self.credentials_path)
            with self._lock:
                self._state = "success"
                self._bot_id = str(getattr(credentials, "bot_id", "") or "")
                self._qr_data_url = ""
        except ILinkError as exc:
            with self._lock:
                self._state = "failed"
                self._error = str(exc)
                self._qr_data_url = ""
        except Exception as exc:  # noqa: BLE001 - keep the panel poll loop alive
            with self._lock:
                self._state = "failed"
                self._error = "登录失败：{}".format(exc)
                self._qr_data_url = ""
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
