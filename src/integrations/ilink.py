"""Minimal WeChat iLink HTTP client.

The protocol is implemented directly from Tencent's openclaw-weixin plugin.
Only QR login, inbound polling, text replies, and inbound image download are
included in this first version.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad


DEFAULT_API_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
BOT_TYPE = "3"
USER_MESSAGE_TYPE = 1
BOT_MESSAGE_TYPE = 2
FINISH_MESSAGE_STATE = 2
TYPING_STATUS = 1
CANCEL_TYPING_STATUS = 2
TEXT_ITEM_TYPE = 1
IMAGE_ITEM_TYPE = 2
MAX_IMAGE_BYTES = 20 * 1024 * 1024
TYPING_TICKET_TTL_SECONDS = 24 * 60 * 60
TYPING_KEEPALIVE_SECONDS = 5.0
TYPING_RETRY_INITIAL_SECONDS = 2.0
TYPING_RETRY_MAX_SECONDS = 60 * 60.0


class ILinkError(RuntimeError):
    """Base error raised for iLink protocol failures."""


class ILinkTimeout(ILinkError):
    """Raised when an iLink request reaches its client-side timeout."""


class SessionExpired(ILinkError):
    """Raised when iLink reports that the saved login session expired."""


class PartialDeliveryError(ILinkError):
    """Raised when a caption was sent but its following image failed."""


@dataclass
class Credentials:
    token: str
    base_url: str
    bot_id: str
    user_id: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Credentials":
        token = str(data.get("token", "")).strip()
        base_url = str(data.get("base_url", DEFAULT_API_BASE_URL)).strip()
        bot_id = str(data.get("bot_id", "")).strip()
        user_id = str(data.get("user_id", "")).strip()
        if not token or not base_url or not bot_id:
            raise ValueError("微信凭证缺少 token、base_url 或 bot_id")
        return cls(token=token, base_url=base_url.rstrip("/"), bot_id=bot_id, user_id=user_id)

    def to_dict(self) -> Dict[str, str]:
        return {
            "token": self.token,
            "base_url": self.base_url,
            "bot_id": self.bot_id,
            "user_id": self.user_id,
        }


def _random_wechat_uin() -> str:
    raw = str(secrets.randbits(32)).encode("ascii")
    return base64.b64encode(raw).decode("ascii")


def build_headers(token: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
    }
    if token:
        headers["Authorization"] = "Bearer {}".format(token)
    return headers


def parse_aes_key(value: str) -> bytes:
    """Accept iLink's observed raw-base64 and hex-base64 AES key formats."""

    text = value.strip()
    if len(text) == 32:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass

    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise ILinkError("图片 AES 密钥不是有效的 base64") from exc

    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            return bytes.fromhex(decoded.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            pass
    raise ILinkError("图片 AES 密钥长度无效")


def decrypt_cdn_bytes(encrypted: bytes, aes_key: str) -> bytes:
    key = parse_aes_key(aes_key)
    if not encrypted or len(encrypted) % AES.block_size != 0:
        raise ILinkError("微信图片密文长度无效")
    try:
        padded = AES.new(key, AES.MODE_ECB).decrypt(encrypted)
        return unpad(padded, AES.block_size)
    except ValueError as exc:
        raise ILinkError("微信图片解密失败") from exc


def build_cdn_download_url(encrypt_query_param: str, cdn_base_url: str) -> str:
    return "{}/download?encrypted_query_param={}".format(
        cdn_base_url.rstrip("/"), quote(encrypt_query_param, safe="")
    )


def build_cdn_upload_url(upload_param: str, filekey: str, cdn_base_url: str) -> str:
    return "{}/upload?encrypted_query_param={}&filekey={}".format(
        cdn_base_url.rstrip("/"),
        quote(upload_param, safe=""),
        quote(filekey, safe=""),
    )


def encrypt_cdn_bytes(plaintext: bytes, aes_key: bytes) -> bytes:
    if len(aes_key) != 16:
        raise ILinkError("图片 AES 密钥长度无效")
    return AES.new(aes_key, AES.MODE_ECB).encrypt(pad(plaintext, AES.block_size))


def is_private_user_message(message: Dict[str, Any]) -> bool:
    if message.get("message_type") != USER_MESSAGE_TYPE:
        return False
    # Different plugin releases have used different group/room field names.
    if any(message.get(key) for key in ("group_id", "chatroom_id", "room_id")):
        return False
    if message.get("chat_type") not in (None, "", "direct", 1):
        return False
    return bool(message.get("from_user_id") and message.get("context_token"))


def extract_text_and_image(
    message: Dict[str, Any]
) -> Tuple[str, Optional[Dict[str, Any]]]:
    texts: List[str] = []
    image: Optional[Dict[str, Any]] = None
    for item in message.get("item_list") or []:
        item_type = item.get("type")
        text_item = item.get("text_item") or {}
        if item_type == TEXT_ITEM_TYPE or text_item:
            text = str(text_item.get("text", "")).strip()
            if text:
                texts.append(text)
        image_item = item.get("image_item")
        if image is None and (item_type == IMAGE_ITEM_TYPE or image_item):
            if isinstance(image_item, dict):
                image = image_item
    return "\n".join(texts), image


class ILinkClient:
    def __init__(
        self,
        credentials: Optional[Credentials] = None,
        api_base_url: str = DEFAULT_API_BASE_URL,
        cdn_base_url: str = DEFAULT_CDN_BASE_URL,
        client: Optional[httpx.Client] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.credentials = credentials
        self.api_base_url = api_base_url.rstrip("/")
        self.cdn_base_url = cdn_base_url.rstrip("/")
        self.client = client or httpx.Client(follow_redirects=True, trust_env=False)
        self._owns_client = client is None
        self._sleep = sleep
        self._typing_tickets: Dict[str, Tuple[str, float]] = {}
        self._typing_retries: Dict[str, Tuple[float, float]] = {}
        self._typing_lock = threading.RLock()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "ILinkClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    @property
    def token(self) -> str:
        if not self.credentials:
            raise ILinkError("尚未完成微信扫码登录")
        return self.credentials.token

    @property
    def base_url(self) -> str:
        if self.credentials:
            return self.credentials.base_url.rstrip("/")
        return self.api_base_url

    def get_qr_code(self) -> Dict[str, Any]:
        try:
            response = self.client.get(
                "{}/ilink/bot/get_bot_qrcode".format(self.api_base_url),
                params={"bot_type": BOT_TYPE},
                timeout=15.0,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ILinkError("获取微信登录二维码失败") from exc
        if data.get("ret", 0) != 0 or not data.get("qrcode") or not data.get("qrcode_img_content"):
            raise ILinkError("微信登录二维码响应无效：{}".format(data.get("errmsg", data.get("ret"))))
        return data

    def get_qr_status(self, qrcode_token: str) -> Dict[str, Any]:
        headers = {"iLink-App-ClientVersion": "1"}
        try:
            response = self.client.get(
                "{}/ilink/bot/get_qrcode_status".format(self.api_base_url),
                params={"qrcode": qrcode_token},
                headers=headers,
                timeout=40.0,
            )
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException:
            return {"status": "wait"}
        except (httpx.HTTPError, ValueError) as exc:
            raise ILinkError("查询微信扫码状态失败") from exc

    def login(
        self,
        show_qr: Callable[[str], None],
        max_refreshes: int = 3,
        status_changed: Optional[Callable[[str], None]] = None,
    ) -> Credentials:
        refreshes = 0
        last_status = ""
        while refreshes <= max_refreshes:
            qr_data = self.get_qr_code()
            show_qr(str(qr_data["qrcode_img_content"]))
            qrcode_token = str(qr_data["qrcode"])

            while True:
                status_data = self.get_qr_status(qrcode_token)
                status = str(status_data.get("status", "wait"))
                if status != last_status and status_changed:
                    status_changed(status)
                last_status = status

                if status == "confirmed":
                    credentials = Credentials(
                        token=str(status_data.get("bot_token", "")),
                        base_url=str(status_data.get("baseurl") or self.api_base_url).rstrip("/"),
                        bot_id=str(status_data.get("ilink_bot_id", "")),
                        user_id=str(status_data.get("ilink_user_id", "")),
                    )
                    if not credentials.token or not credentials.bot_id:
                        raise ILinkError("微信确认登录后未返回有效凭证")
                    self.credentials = credentials
                    return credentials
                if status == "expired":
                    refreshes += 1
                    break
                if status not in ("wait", "scaned"):
                    raise ILinkError("未知的微信扫码状态：{}".format(status))
                self._sleep(0.5)

        raise ILinkError("微信登录二维码多次过期，请重新运行程序")

    def _post(self, path: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        try:
            response = self.client.post(
                "{}/{}".format(self.base_url, path.lstrip("/")),
                json=payload,
                headers=build_headers(self.token),
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            raise ILinkTimeout("微信接口等待超时：{}".format(path)) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ILinkError("微信接口调用失败：{}".format(path)) from exc

        if data.get("errcode") == -14:
            raise SessionExpired("微信登录凭证已失效")
        if data.get("ret", 0) != 0:
            raise ILinkError(
                "微信接口返回错误：{}".format(data.get("errmsg") or data.get("errcode") or data.get("ret"))
            )
        return data

    def get_updates(self, cursor: str) -> Dict[str, Any]:
        try:
            return self._post(
                "ilink/bot/getupdates",
                {"get_updates_buf": cursor},
                timeout=40.0,
            )
        except ILinkTimeout:
            return {"ret": 0, "msgs": [], "get_updates_buf": cursor}

    def get_typing_ticket(self, user_id: str, context_token: str) -> str:
        now = time.monotonic()
        with self._typing_lock:
            cached = self._typing_tickets.get(user_id)
            if cached and now < cached[1]:
                return cached[0]
            retry = self._typing_retries.get(user_id)
            if retry and now < retry[0]:
                return ""

        try:
            data = self._post(
                "ilink/bot/getconfig",
                {
                    "ilink_user_id": user_id,
                    "context_token": context_token,
                },
                timeout=10.0,
            )
        except SessionExpired:
            raise
        except ILinkError:
            with self._typing_lock:
                previous = self._typing_retries.get(user_id)
                delay = previous[1] if previous else TYPING_RETRY_INITIAL_SECONDS
                self._typing_retries[user_id] = (
                    now + delay,
                    min(delay * 2, TYPING_RETRY_MAX_SECONDS),
                )
            raise

        ticket = str(data.get("typing_ticket") or "").strip()
        with self._typing_lock:
            if ticket:
                self._typing_tickets[user_id] = (
                    ticket,
                    now + TYPING_TICKET_TTL_SECONDS,
                )
                self._typing_retries.pop(user_id, None)
            else:
                self._typing_retries[user_id] = (
                    now + TYPING_RETRY_INITIAL_SECONDS,
                    TYPING_RETRY_INITIAL_SECONDS * 2,
                )
        return ticket

    def send_typing(self, user_id: str, typing_ticket: str, status: int) -> None:
        self._post(
            "ilink/bot/sendtyping",
            {
                "ilink_user_id": user_id,
                "typing_ticket": typing_ticket,
                "status": status,
            },
            timeout=10.0,
        )

    @contextmanager
    def typing(
        self,
        user_id: str,
        context_token: str,
        on_error: Optional[Callable[[ILinkError], None]] = None,
    ):
        def report(exc: ILinkError) -> None:
            if on_error:
                on_error(exc)

        try:
            ticket = self.get_typing_ticket(user_id, context_token)
        except SessionExpired:
            raise
        except ILinkError as exc:
            report(exc)
            ticket = ""

        if not ticket:
            yield
            return

        try:
            self.send_typing(user_id, ticket, TYPING_STATUS)
        except SessionExpired:
            raise
        except ILinkError as exc:
            report(exc)
            yield
            return

        stop_event = threading.Event()
        session_errors: List[SessionExpired] = []

        def keepalive() -> None:
            while not stop_event.wait(TYPING_KEEPALIVE_SECONDS):
                try:
                    self.send_typing(user_id, ticket, TYPING_STATUS)
                except SessionExpired as exc:
                    session_errors.append(exc)
                    stop_event.set()
                    return
                except ILinkError as exc:
                    report(exc)

        worker = threading.Thread(
            target=keepalive,
            name="ilink-typing-keepalive",
            daemon=True,
        )
        worker.start()
        try:
            yield
        finally:
            stop_event.set()
            worker.join(timeout=10.5)
            if session_errors:
                raise session_errors[0]
            try:
                self.send_typing(user_id, ticket, CANCEL_TYPING_STATUS)
            except SessionExpired:
                raise
            except ILinkError as exc:
                report(exc)

    def send_text(self, to_user_id: str, context_token: str, text: str) -> None:
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": str(uuid.uuid4()),
                "message_type": BOT_MESSAGE_TYPE,
                "message_state": FINISH_MESSAGE_STATE,
                "item_list": [
                    {"type": TEXT_ITEM_TYPE, "text_item": {"text": text}}
                ],
                "context_token": context_token,
            }
        }
        self._post("ilink/bot/sendmessage", payload, timeout=20.0)

    def send_image(
        self,
        to_user_id: str,
        context_token: str,
        image_bytes: bytes,
        caption: str = "",
    ) -> None:
        if not image_bytes:
            raise ILinkError("待发送图片为空")
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise ILinkError("微信图片超过 20MB，暂不发送")

        aes_key = secrets.token_bytes(16)
        filekey = secrets.token_hex(16)
        encrypted = encrypt_cdn_bytes(image_bytes, aes_key)
        upload = self._post(
            "ilink/bot/getuploadurl",
            {
                "filekey": filekey,
                "media_type": 1,
                "to_user_id": to_user_id,
                "rawsize": len(image_bytes),
                "rawfilemd5": hashlib.md5(image_bytes).hexdigest(),
                "filesize": len(encrypted),
                "no_need_thumb": True,
                "aeskey": aes_key.hex(),
            },
            timeout=20.0,
        )
        upload_full_url = str(upload.get("upload_full_url") or "").strip()
        upload_param = str(upload.get("upload_param") or "").strip()
        if upload_full_url:
            upload_url = upload_full_url
        elif upload_param:
            upload_url = build_cdn_upload_url(
                upload_param,
                filekey,
                self.cdn_base_url,
            )
        else:
            raise ILinkError("微信未返回图片 CDN 上传地址")

        try:
            response = self.client.post(
                upload_url,
                content=encrypted,
                headers={"Content-Type": "application/octet-stream"},
                timeout=30.0,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ILinkError("上传微信图片失败") from exc
        download_param = str(response.headers.get("x-encrypted-param") or "").strip()
        if not download_param:
            raise ILinkError("微信图片上传响应缺少下载参数")

        normalized_caption = caption if isinstance(caption, str) else ""
        caption_sent = False
        if normalized_caption.strip():
            self.send_text(to_user_id, context_token, normalized_caption)
            caption_sent = True

        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": str(uuid.uuid4()),
                "message_type": BOT_MESSAGE_TYPE,
                "message_state": FINISH_MESSAGE_STATE,
                "item_list": [
                    {
                        "type": IMAGE_ITEM_TYPE,
                        "image_item": {
                            "media": {
                                "encrypt_query_param": download_param,
                                "aes_key": base64.b64encode(
                                    aes_key.hex().encode("ascii")
                                ).decode("ascii"),
                                "encrypt_type": 1,
                            },
                            "mid_size": len(encrypted),
                        },
                    }
                ],
                "context_token": context_token,
            }
        }
        try:
            self._post("ilink/bot/sendmessage", payload, timeout=20.0)
        except ILinkError as exc:
            if caption_sent:
                raise PartialDeliveryError(
                    "图片发送失败，但文字说明可能已经发送"
                ) from exc
            raise

    def download_image(self, image_item: Dict[str, Any]) -> bytes:
        media = image_item.get("media") or image_item.get("thumb_media") or {}
        if not isinstance(media, dict):
            raise ILinkError("微信图片缺少媒体信息")

        encrypted_query = str(
            media.get("encrypt_query_param")
            or media.get("download_param")
            or image_item.get("encrypt_query_param")
            or ""
        )
        aes_key = str(
            media.get("aes_key")
            or image_item.get("aes_key")
            or image_item.get("aeskey")
            or ""
        )
        full_url = str(
            media.get("full_url")
            or media.get("download_url")
            or image_item.get("url")
            or ""
        )
        if not full_url and not encrypted_query:
            raise ILinkError("微信图片缺少下载地址")
        if not aes_key:
            raise ILinkError("微信图片缺少解密密钥")

        url = full_url or build_cdn_download_url(encrypted_query, self.cdn_base_url)
        try:
            response = self.client.get(url, timeout=30.0)
            response.raise_for_status()
            encrypted = response.content
        except httpx.HTTPError as exc:
            raise ILinkError("下载微信图片失败") from exc
        if len(encrypted) > MAX_IMAGE_BYTES:
            raise ILinkError("微信图片超过 20MB，暂不处理")
        return decrypt_cdn_bytes(encrypted, aes_key)
