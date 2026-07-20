from __future__ import annotations

import base64
import hashlib
import json
import threading
import unittest
from unittest.mock import patch

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from src.integrations.ilink import (
    CANCEL_TYPING_STATUS,
    Credentials,
    ILinkClient,
    PartialDeliveryError,
    SessionExpired,
    TYPING_STATUS,
    build_cdn_download_url,
    build_cdn_upload_url,
    build_headers,
    decrypt_cdn_bytes,
    encrypt_cdn_bytes,
    extract_text_and_image,
    is_private_user_message,
)


class ILinkClientTests(unittest.TestCase):
    def test_headers_include_auth_and_encoded_random_uin(self) -> None:
        with patch("src.integrations.ilink.secrets.randbits", return_value=123):
            headers = build_headers("secret-token")
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertEqual(headers["AuthorizationType"], "ilink_bot_token")
        self.assertEqual(
            headers["X-WECHAT-UIN"], base64.b64encode(b"123").decode("ascii")
        )

    def test_qr_login_handles_wait_scan_and_confirmation(self) -> None:
        statuses = iter(
            [
                {"status": "wait"},
                {"status": "scaned"},
                {
                    "status": "confirmed",
                    "bot_token": "token",
                    "ilink_bot_id": "bot@im.bot",
                    "ilink_user_id": "owner@im.wechat",
                    "baseurl": "https://gateway.test",
                },
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("get_bot_qrcode"):
                self.assertEqual(request.url.params["bot_type"], "3")
                return httpx.Response(
                    200,
                    json={
                        "ret": 0,
                        "qrcode": "qr-token",
                        "qrcode_img_content": "https://qr.test/value",
                    },
                )
            if request.url.path.endswith("get_qrcode_status"):
                return httpx.Response(200, json=next(statuses))
            return httpx.Response(404)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = ILinkClient(client=http, sleep=lambda _seconds: None)
        shown = []
        changed = []
        credentials = client.login(shown.append, status_changed=changed.append)

        self.assertEqual(shown, ["https://qr.test/value"])
        self.assertEqual(changed, ["wait", "scaned", "confirmed"])
        self.assertEqual(credentials.token, "token")
        self.assertEqual(credentials.base_url, "https://gateway.test")

    def test_get_updates_and_send_text_preserve_context(self) -> None:
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            captured.append((request, body))
            if request.url.path.endswith("getupdates"):
                return httpx.Response(
                    200,
                    json={"ret": 0, "msgs": [], "get_updates_buf": "cursor-2"},
                )
            return httpx.Response(200, json={"ret": 0})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        credentials = Credentials("token", "https://gateway.test", "bot", "owner")
        client = ILinkClient(credentials=credentials, client=http)

        updates = client.get_updates("cursor-1")
        client.send_text("user@im.wechat", "context-token", "你好")

        self.assertEqual(updates["get_updates_buf"], "cursor-2")
        self.assertEqual(captured[0][1], {"get_updates_buf": "cursor-1"})
        sent = captured[1][1]["msg"]
        self.assertEqual(sent["to_user_id"], "user@im.wechat")
        self.assertEqual(sent["context_token"], "context-token")
        self.assertEqual(sent["item_list"][0]["text_item"]["text"], "你好")

    def test_typing_ticket_is_cached_and_typing_is_cancelled(self) -> None:
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            captured.append((request.url.path, body))
            if request.url.path.endswith("getconfig"):
                return httpx.Response(
                    200,
                    json={"ret": 0, "typing_ticket": "ticket-1"},
                )
            return httpx.Response(200, json={"ret": 0})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        credentials = Credentials("token", "https://gateway.test", "bot", "owner")
        client = ILinkClient(credentials=credentials, client=http)

        with client.typing("user@im.wechat", "context-token"):
            pass
        with client.typing("user@im.wechat", "context-token"):
            pass

        config_calls = [item for item in captured if item[0].endswith("getconfig")]
        typing_calls = [item for item in captured if item[0].endswith("sendtyping")]
        self.assertEqual(len(config_calls), 1)
        self.assertEqual(
            config_calls[0][1],
            {
                "ilink_user_id": "user@im.wechat",
                "context_token": "context-token",
            },
        )
        self.assertEqual(
            [body["status"] for _path, body in typing_calls],
            [TYPING_STATUS, CANCEL_TYPING_STATUS, TYPING_STATUS, CANCEL_TYPING_STATUS],
        )

    def test_typing_is_refreshed_while_work_is_running(self) -> None:
        statuses = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("getconfig"):
                return httpx.Response(
                    200,
                    json={"ret": 0, "typing_ticket": "ticket-1"},
                )
            body = json.loads(request.content.decode("utf-8"))
            statuses.append(body["status"])
            return httpx.Response(200, json={"ret": 0})

        credentials = Credentials("token", "https://gateway.test", "bot", "owner")
        client = ILinkClient(
            credentials=credentials,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with patch("src.integrations.ilink.TYPING_KEEPALIVE_SECONDS", 0.01):
            with client.typing("user", "context"):
                threading.Event().wait(0.025)

        self.assertEqual(statuses[0], TYPING_STATUS)
        self.assertEqual(statuses[-1], CANCEL_TYPING_STATUS)
        self.assertGreaterEqual(statuses.count(TYPING_STATUS), 2)

    def test_typing_failure_is_best_effort_but_expired_session_propagates(self) -> None:
        errors = []

        def failed(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ret": 1, "errmsg": "typing unavailable"})

        credentials = Credentials("token", "https://gateway.test", "bot", "owner")
        client = ILinkClient(
            credentials=credentials,
            client=httpx.Client(transport=httpx.MockTransport(failed)),
        )
        reached = False
        with client.typing("user", "context", on_error=errors.append):
            reached = True
        self.assertTrue(reached)
        self.assertEqual(len(errors), 1)

        def expired(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ret": 1, "errcode": -14})

        expired_client = ILinkClient(
            credentials=credentials,
            client=httpx.Client(transport=httpx.MockTransport(expired)),
        )
        with self.assertRaises(SessionExpired):
            with expired_client.typing("user", "context"):
                pass

    def test_session_expired_is_reported(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ret": 1, "errcode": -14})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        credentials = Credentials("token", "https://gateway.test", "bot", "owner")
        client = ILinkClient(credentials=credentials, client=http)
        with self.assertRaises(SessionExpired):
            client.get_updates("")

    def test_long_poll_timeout_is_a_normal_empty_update(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("normal long-poll timeout", request=request)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        credentials = Credentials("token", "https://gateway.test", "bot", "owner")
        client = ILinkClient(credentials=credentials, client=http)
        self.assertEqual(
            client.get_updates("cursor"),
            {"ret": 0, "msgs": [], "get_updates_buf": "cursor"},
        )

    def test_image_download_url_and_both_key_encodings(self) -> None:
        key = b"0123456789abcdef"
        plaintext = b"fake-image-bytes"
        encrypted = AES.new(key, AES.MODE_ECB).encrypt(pad(plaintext, AES.block_size))

        raw_base64 = base64.b64encode(key).decode("ascii")
        hex_base64 = base64.b64encode(key.hex().encode("ascii")).decode("ascii")
        self.assertEqual(decrypt_cdn_bytes(encrypted, raw_base64), plaintext)
        self.assertEqual(decrypt_cdn_bytes(encrypted, hex_base64), plaintext)
        self.assertEqual(
            build_cdn_download_url("a+b&c", "https://cdn.test/c2c"),
            "https://cdn.test/c2c/download?encrypted_query_param=a%2Bb%26c",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/c2c/download")
            self.assertEqual(request.url.params["encrypted_query_param"], "a+b&c")
            return httpx.Response(200, content=encrypted)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = ILinkClient(client=http, cdn_base_url="https://cdn.test/c2c")
        result = client.download_image(
            {
                "media": {
                    "encrypt_query_param": "a+b&c",
                    "aes_key": raw_base64,
                }
            }
        )
        self.assertEqual(result, plaintext)

    def test_image_upload_encrypts_and_sends_official_message_shape(self) -> None:
        key = b"0123456789abcdef"
        image = b"valid-image-payload"
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("getuploadurl"):
                body = json.loads(request.content.decode("utf-8"))
                captured.append(("metadata", body))
                return httpx.Response(200, json={"ret": 0, "upload_param": "up+a&b"})
            if request.url.path.endswith("/upload"):
                captured.append(("upload", request))
                return httpx.Response(200, headers={"x-encrypted-param": "download+p"})
            if request.url.path.endswith("sendmessage"):
                body = json.loads(request.content.decode("utf-8"))
                captured.append(("message", body))
                return httpx.Response(200, json={"ret": 0})
            return httpx.Response(404)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        credentials = Credentials("token", "https://gateway.test", "bot", "owner")
        client = ILinkClient(
            credentials=credentials,
            client=http,
            cdn_base_url="https://cdn.test/c2c",
        )
        with patch("src.integrations.ilink.secrets.token_bytes", return_value=key):
            with patch("src.integrations.ilink.secrets.token_hex", return_value="file-key"):
                client.send_image("user", "context", image, caption="图片说明")

        metadata = captured[0][1]
        encrypted = encrypt_cdn_bytes(image, key)
        self.assertEqual(metadata["media_type"], 1)
        self.assertTrue(metadata["no_need_thumb"])
        self.assertEqual(metadata["rawsize"], len(image))
        self.assertEqual(metadata["rawfilemd5"], hashlib.md5(image).hexdigest())
        self.assertEqual(metadata["filesize"], len(encrypted))
        self.assertEqual(metadata["aeskey"], key.hex())

        upload_request = captured[1][1]
        self.assertEqual(upload_request.url.params["encrypted_query_param"], "up+a&b")
        self.assertEqual(upload_request.url.params["filekey"], "file-key")
        self.assertEqual(upload_request.headers["content-type"], "application/octet-stream")
        self.assertEqual(
            unpad(
                AES.new(key, AES.MODE_ECB).decrypt(upload_request.content),
                AES.block_size,
            ),
            image,
        )

        caption_payload = captured[2][1]["msg"]
        self.assertEqual(caption_payload["item_list"][0]["text_item"]["text"], "图片说明")
        image_payload = captured[3][1]["msg"]
        image_item = image_payload["item_list"][0]
        self.assertEqual(image_item["type"], 2)
        self.assertEqual(
            image_item["image_item"]["media"]["encrypt_query_param"],
            "download+p",
        )
        self.assertEqual(
            base64.b64decode(image_item["image_item"]["media"]["aes_key"]),
            key.hex().encode("ascii"),
        )
        self.assertEqual(image_item["image_item"]["media"]["encrypt_type"], 1)
        self.assertEqual(image_item["image_item"]["mid_size"], len(encrypted))
        self.assertEqual(image_payload["context_token"], "context")

    def test_image_caption_partial_delivery_is_reported(self) -> None:
        send_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal send_count
            if request.url.path.endswith("getuploadurl"):
                return httpx.Response(
                    200,
                    json={"ret": 0, "upload_full_url": "https://cdn.test/upload"},
                )
            if request.url.host == "cdn.test":
                return httpx.Response(200, headers={"x-encrypted-param": "download"})
            if request.url.path.endswith("sendmessage"):
                send_count += 1
                return httpx.Response(
                    200,
                    json={"ret": 0} if send_count == 1 else {"ret": 1, "errmsg": "failed"},
                )
            return httpx.Response(404)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        credentials = Credentials("token", "https://gateway.test", "bot", "owner")
        client = ILinkClient(credentials=credentials, client=http)
        with self.assertRaisesRegex(PartialDeliveryError, "文字说明可能已经发送"):
            client.send_image("user", "context", b"image", caption="说明")
        self.assertEqual(send_count, 2)

    def test_cdn_upload_url_encodes_parameters(self) -> None:
        self.assertEqual(
            build_cdn_upload_url("a+b&c", "file/key", "https://cdn.test/c2c"),
            "https://cdn.test/c2c/upload?encrypted_query_param=a%2Bb%26c&filekey=file%2Fkey",
        )

    def test_message_filter_and_content_extraction(self) -> None:
        message = {
            "message_type": 1,
            "from_user_id": "user",
            "context_token": "context",
            "item_list": [
                {"type": 1, "text_item": {"text": "看一下"}},
                {"type": 2, "image_item": {"media": {"aes_key": "key"}}},
            ],
        }
        self.assertTrue(is_private_user_message(message))
        text, image = extract_text_and_image(message)
        self.assertEqual(text, "看一下")
        self.assertIsNotNone(image)

        grouped = dict(message, group_id="group")
        self.assertFalse(is_private_user_message(grouped))


if __name__ == "__main__":
    unittest.main()
