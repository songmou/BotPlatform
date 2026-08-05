"""WeCom intelligent-robot credential verification handshake.

Performs a real ``aibot_subscribe`` over ``wss://openws.work.weixin.qq.com``
to validate a Bot ID / Secret pair before saving it. Note that a subscribe
with the same bot kicks any live long connection for that bot, so callers
must skip verification when the stored credentials are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import uuid

WS_URL = "wss://openws.work.weixin.qq.com"


def _req_id() -> str:
    return uuid.uuid4().hex


class WeComVerifyError(ValueError):
    """Raised when WeCom credential verification fails."""


async def _verify_async(bot_id: str, secret: str, ws_url: str, timeout: float) -> None:
    import websockets

    async with websockets.connect(ws_url, max_size=1024 * 1024) as ws:
        await ws.send(
            json.dumps(
                {
                    "cmd": "aibot_subscribe",
                    "headers": {"req_id": _req_id()},
                    "body": {"bot_id": bot_id, "secret": secret},
                },
                ensure_ascii=False,
            )
        )
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    data = json.loads(raw)
    errcode = int(data.get("errcode", -1))
    if errcode != 0:
        raise WeComVerifyError(
            str(data.get("errmsg") or "凭证校验失败（errcode={}）".format(errcode))
        )


def verify_wecom_credentials(
    bot_id: str,
    secret: str,
    ws_url: str = WS_URL,
    timeout: float = 10.0,
) -> None:
    """Validate Bot ID / Secret by performing a real subscribe handshake.

    Raises WeComVerifyError when the credentials are rejected or the server
    is unreachable. Runs its own event loop, so it is safe to call from a
    synchronous request handler (which executes in a worker thread).
    """
    bot_id = (bot_id or "").strip()
    secret = (secret or "").strip()
    if not bot_id or not secret:
        raise WeComVerifyError("请填写 Bot ID 和 Secret")
    try:
        asyncio.run(_verify_async(bot_id, secret, ws_url, timeout))
    except WeComVerifyError:
        raise
    except asyncio.TimeoutError as exc:
        raise WeComVerifyError("企业微信服务器无响应，请稍后重试") from exc
    except Exception as exc:  # noqa: BLE001 - surface a readable reason
        raise WeComVerifyError("无法连接企业微信服务器：{}".format(exc)) from exc
