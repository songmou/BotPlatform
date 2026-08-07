"""Feishu scan-to-create agent app registration manager.

Implements the device-authorization flow (RFC 8628) used by the official
Lark CLI: begin a registration session to obtain a launcher QR URL, then
poll until the scanned user confirms creating the app, at which point the
server exchanges the device code for the final ``app_id``/``app_secret``.
Feishu never calls back; the server drives the whole flow.
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, Optional

import httpx

from src.core.services.wechat_login import _qr_png_data_url

DEFAULT_DOMAIN = "accounts.feishu.cn"
LARK_DOMAIN = "accounts.larksuite.com"
REGISTRATION_ENDPOINT = "/oauth/v1/app/registration"


class FeishuRegistrationError(ValueError):
    """Raised for unrecoverable registration failures."""


def _poll_error_message(error: str) -> str:
    return {
        "access_denied": "用户拒绝创建飞书应用",
        "expired_token": "二维码已过期，请刷新后重新扫码",
        "slow_down": "飞书服务繁忙，请稍候",
    }.get(error, "飞书注册失败：{}".format(error))


class FeishuRegistrationManager:
    """Run one interactive Feishu app registration at a time.

    States: idle -> pending -> success | failed. The QR image is exposed as
    a data URL so the panel can poll and render it inline.
    """

    def __init__(
        self,
        *,
        domain: str = DEFAULT_DOMAIN,
        client_factory: Optional[Callable[[], Any]] = None,
        credentials_saver: Optional[Callable[[Dict[str, Any]], None]] = None,
        connected_checker: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.domain = domain
        self._client_factory = client_factory or self._default_client_factory
        self._save = credentials_saver
        self._connected_checker = connected_checker
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._state = "idle"
        self._qr_data_url = ""
        self._error = ""
        self._app_id = ""
        self._user_name = ""
        self.pending_holder: Dict[str, Any] = {}

    @staticmethod
    def _default_client_factory() -> httpx.Client:
        return httpx.Client(timeout=20.0)

    def is_connected(self) -> bool:
        if self._connected_checker is not None:
            try:
                return bool(self._connected_checker())
            except Exception:  # noqa: BLE001 - status polling must not fail
                return False
        return bool(self._app_id)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "connected": self.is_connected(),
                "state": self._state,
                "qr": self._qr_data_url,
                "error": self._error,
                "app_id": self._app_id,
                "user_name": self._user_name,
            }

    def start(self, app_name: str = "") -> Dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            self._state = "pending"
            self._qr_data_url = ""
            self._error = ""
            self._app_id = ""
            self._user_name = ""
            self.pending_holder.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(str(app_name or "").strip(),),
                name="feishu-panel-registration",
                daemon=True,
            )
            self._thread.start()
        return self.status()

    def _request(self, client: Any, domain: str, params: Dict[str, str]) -> Any:
        response = client.post(
            "https://{}{}".format(domain, REGISTRATION_ENDPOINT),
            data=params,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            return response.json()
        except ValueError as exc:
            raise FeishuRegistrationError("飞书响应格式无效") from exc

    def _run(self, app_name: str) -> None:
        client = None
        try:
            client = self._client_factory()
            domain = self.domain
            begin = self._request(
                client,
                domain,
                {
                    "action": "begin",
                    "archetype": "PersonalAgent",
                    "auth_method": "client_secret",
                    "request_user_info": "open_id",
                },
            )
            if not isinstance(begin, dict):
                raise FeishuRegistrationError("飞书注册失败：响应格式无效")
            verification = str(
                begin.get("verification_uri_complete")
                or begin.get("verification_uri")
                or ""
            )
            if not verification:
                raise FeishuRegistrationError(
                    "飞书注册失败：{}".format(
                        begin.get("error_description")
                        or begin.get("error")
                        or "未返回二维码地址"
                    )
                )
            separator = "&" if "?" in verification else "?"
            query = "from=sdk&source=python&tp=sdk"
            if app_name:
                query += "&name={}".format(_url_encode(app_name))
            qr_content = "{}{}{}".format(verification, separator, query)
            with self._lock:
                self._qr_data_url = _qr_png_data_url(qr_content)
                self._state = "pending"

            device_code = str(begin.get("device_code") or "")
            if not device_code:
                raise FeishuRegistrationError("飞书注册失败：未返回设备编号")
            interval = _as_float(begin.get("interval"), 5.0)
            expires_in = _as_float(begin.get("expires_in"), 3600.0)
            credentials = self._poll(client, domain, device_code, interval, expires_in)
            self.pending_holder.clear()
            self.pending_holder["pending"] = credentials
            if self._save is not None:
                self._save(credentials)
            user_info = credentials.get("user_info") or {}
            with self._lock:
                self._state = "success"
                self._app_id = str(credentials.get("client_id") or "")
                self._user_name = str(
                    user_info.get("name") or user_info.get("open_id") or ""
                )
                self._qr_data_url = ""
        except FeishuRegistrationError as exc:
            with self._lock:
                self._state = "failed"
                self._error = str(exc)
                self._qr_data_url = ""
        except Exception as exc:  # noqa: BLE001 - keep the panel poll loop alive
            with self._lock:
                self._state = "failed"
                self._error = "飞书注册失败：{}".format(exc)
                self._qr_data_url = ""
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    def _poll(
        self,
        client: Any,
        domain: str,
        device_code: str,
        interval: float,
        expires_in: float,
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + max(expires_in, 1.0)
        domain_switched = False
        while time.monotonic() < deadline:
            poll = self._request(
                client, domain, {"action": "poll", "device_code": device_code}
            )
            if not isinstance(poll, dict):
                raise FeishuRegistrationError("飞书响应格式无效")
            user_info = poll.get("user_info")
            if (
                isinstance(user_info, dict)
                and user_info.get("tenant_brand") == "lark"
                and not domain_switched
            ):
                domain = LARK_DOMAIN
                domain_switched = True
            client_id = str(poll.get("client_id") or "")
            client_secret = str(poll.get("client_secret") or "")
            if client_id and client_secret:
                return {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "user_info": user_info if isinstance(user_info, dict) else {},
                }
            error = str(poll.get("error") or "")
            if error == "authorization_pending":
                pass
            elif error == "slow_down":
                interval += 5.0
            elif error in {"access_denied", "expired_token"}:
                raise FeishuRegistrationError(_poll_error_message(error))
            elif error:
                raise FeishuRegistrationError(_poll_error_message(error))
            time.sleep(max(interval, 1.0))
        raise FeishuRegistrationError("二维码已过期，请刷新后重新扫码")


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _url_encode(value: str) -> str:
    return urllib.parse.quote(value, safe="")
