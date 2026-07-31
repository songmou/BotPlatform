"""WeCom intelligent-robot WebSocket long connection.

Implements the wecom "智能机器人长连接" protocol: subscribe with
BotID/Secret over ``wss://openws.work.weixin.qq.com``, receive message and
event callbacks, answer via single-shot stream replies, keep a 30s
application-level ping. A new connection for the same bot kicks the old
one (``disconnected_event``), so after being kicked we back off before
reconnecting.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from src.core.services.publish import (
    PLATFORM_WECOM,
    AgentBindingResolver,
    PublishStore,
)

WS_URL = "wss://openws.work.weixin.qq.com"
PING_INTERVAL_SECONDS = 30.0
RECONNECT_DELAY_SECONDS = 5.0
# If no frame (not even a ping response) arrives within this window the
# connection is treated as half-dead and is torn down for a reconnect.
IDLE_TIMEOUT_SECONDS = 90.0
# When another subscribe (e.g. the panel's credential check) kicks this
# connection, reclaim it after a short wait instead of going dark for long.
KICKED_BACKOFF_SECONDS = 15.0
MAX_REPLY_BYTES = 20000
DEDUP_LIMIT = 500

_MENTION_PREFIX = re.compile(r"^@\S+\s*")


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


def _truncate_utf8(text: str, limit: int = MAX_REPLY_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore") + "…"


class WeComBotService:
    """Own the long connection lifecycle in a background thread."""

    def __init__(
        self,
        agent_service: Any,
        publish_store: PublishStore,
        tenant_registry: Optional[Any] = None,
        conversation_store: Optional[Any] = None,
        ws_url: str = WS_URL,
    ) -> None:
        self.agent_service = agent_service
        self.store = publish_store
        self.tenant_registry = tenant_registry
        self.conversation_store = conversation_store
        self.resolver = AgentBindingResolver(publish_store)
        self.ws_url = ws_url
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._kicked_until = 0.0
        # Hold references to in-flight reply tasks; without this the event
        # loop may garbage-collect a pending task mid-execution.
        self._tasks: set = set()

    # -- lifecycle ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="wecom-bot", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # noqa: BLE001 - keep shutdown quiet
            print("企业微信长连接线程退出：{}".format(exc), file=sys.stderr)

    async def _run(self) -> None:
        while not self._stop.is_set():
            config = self.store.platform_config(PLATFORM_WECOM)
            bot_id = str(config.get("bot_id") or "")
            secret = str(config.get("secret") or "")
            if not bot_id or not secret or time.time() < self._kicked_until:
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                continue
            try:
                await self._session(bot_id, secret)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                print(
                    "企业微信长连接中断：{}；{:.0f} 秒后重连。".format(
                        exc, RECONNECT_DELAY_SECONDS
                    ),
                    file=sys.stderr,
                )
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _session(self, bot_id: str, secret: str) -> None:
        import websockets

        # Disable protocol-level ping: WeCom expects an application-level
        # {"cmd":"ping"} heartbeat and may ignore WebSocket ping frames, which
        # would otherwise make websockets drop a healthy connection.
        async with websockets.connect(
            self.ws_url, max_size=8 * 1024 * 1024, ping_interval=None
        ) as ws:
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
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if int(first.get("errcode", -1)) != 0:
                raise RuntimeError(
                    "企业微信订阅失败：{} {}".format(
                        first.get("errcode"), first.get("errmsg")
                    )
                )
            print("企业微信智能机器人长连接已建立（bot_id={}）。".format(bot_id))
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                while not self._stop.is_set():
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=IDLE_TIMEOUT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        print(
                            "企业微信长连接 {:.0f} 秒无任何帧，判定假死，重连。".format(
                                IDLE_TIMEOUT_SECONDS
                            ),
                            file=sys.stderr,
                        )
                        return
                    try:
                        payload = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    cmd = payload.get("cmd")
                    if not cmd:
                        # Response frame for one of our sends (ping / respond).
                        errcode = payload.get("errcode")
                        if errcode not in (0, None):
                            print(
                                "企业微信返回错误响应：errcode={} errmsg={}".format(
                                    errcode, payload.get("errmsg")
                                ),
                                file=sys.stderr,
                            )
                        continue
                    msgid = str((payload.get("body") or {}).get("msgid") or "")
                    print(
                        "企业微信收到回调 cmd={} msgid={}".format(cmd, msgid),
                        file=sys.stderr,
                    )
                    action, reply = self.handle_callback(payload)
                    if action == "kicked":
                        self._kicked_until = time.time() + KICKED_BACKOFF_SECONDS
                        print(
                            "企业微信长连接被新连接顶替，{:.0f} 秒后再尝试重连。".format(
                                KICKED_BACKOFF_SECONDS
                            ),
                            file=sys.stderr,
                        )
                        return
                    if action == "chat":
                        task = asyncio.create_task(self._answer(ws, payload))
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
                    elif action == "send" and reply is not None:
                        await ws.send(json.dumps(reply, ensure_ascii=False))
            finally:
                ping_task.cancel()

    async def _ping_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_SECONDS)
            await ws.send(
                json.dumps({"cmd": "ping", "headers": {"req_id": _req_id()}})
            )

    async def _answer(self, ws: Any, payload: Dict[str, Any]) -> None:
        try:
            reply = await asyncio.to_thread(self.build_chat_reply, payload)
            if reply is not None:
                await ws.send(json.dumps(reply, ensure_ascii=False))
                print(
                    "企业微信已发送回复 req_id={}".format(
                        (payload.get("headers") or {}).get("req_id")
                    ),
                    file=sys.stderr,
                )
            else:
                print("企业微信未生成回复（无绑定或空回答）", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - never kill the recv loop
            print("企业微信回复发送失败：{}".format(exc), file=sys.stderr)

    # -- callback handling (synchronous, unit-testable) ---------------

    def handle_callback(
        self, payload: Dict[str, Any]
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Classify one inbound frame.

        Returns (action, reply): action is "kicked" | "chat" | "send" |
        "ignore"; "chat" defers to :meth:`build_chat_reply` off-loop.
        """
        cmd = payload.get("cmd")
        body = payload.get("body") or {}
        msgid = str(body.get("msgid") or "")
        if msgid and not self._first_time(msgid):
            return "ignore", None
        if cmd == "aibot_event_callback":
            event_type = str((body.get("event") or {}).get("eventtype") or "")
            if event_type == "disconnected_event":
                return "kicked", None
            if event_type == "enter_chat":
                return "send", self._welcome_reply(payload)
            return "ignore", None
        if cmd == "aibot_msg_callback":
            if str(body.get("msgtype") or "") != "text":
                return "ignore", None
            return "chat", None
        return "ignore", None

    def _first_time(self, msgid: str) -> bool:
        if msgid in self._seen:
            return False
        self._seen[msgid] = None
        while len(self._seen) > DEDUP_LIMIT:
            self._seen.popitem(last=False)
        return True

    def _default_agent_id(self) -> Optional[str]:
        agents = getattr(self.agent_service, "agents", {}) or {}
        routing = self.resolver.resolve(PLATFORM_WECOM, "__welcome__", "", agents)
        return routing.agent_id

    def _welcome_reply(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        agent_id = self._default_agent_id()
        if agent_id is None:
            return None
        agents = getattr(self.agent_service, "agents", {}) or {}
        preset = agents.get(agent_id)
        greeting = (
            str(getattr(preset, "greeting", "") or "").strip()
            or "你好！我是智能助手，有什么可以帮你的吗？"
        )
        req_id = str((payload.get("headers") or {}).get("req_id") or _req_id())
        return {
            "cmd": "aibot_respond_welcome_msg",
            "headers": {"req_id": req_id},
            "body": {"msgtype": "text", "text": {"content": greeting}},
        }

    def build_chat_reply(
        self, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        body = payload.get("body") or {}
        req_id = str((payload.get("headers") or {}).get("req_id") or _req_id())
        text = str((body.get("text") or {}).get("content") or "").strip()
        text = _MENTION_PREFIX.sub("", text).strip()
        if not text:
            return None
        chat_type = str(body.get("chattype") or "single")
        sender = str((body.get("from") or {}).get("userid") or "wecom-user")
        conversation_key = (
            str(body.get("chatid") or "") if chat_type == "group" else sender
        ) or sender

        agents = getattr(self.agent_service, "agents", {}) or {}
        routing = self.resolver.resolve(
            PLATFORM_WECOM, conversation_key, text, agents
        )
        if routing.reply is not None:
            return self._stream_reply(req_id, routing.reply)
        if routing.agent_id is None:
            return None

        subject: Any = "wecom:{}".format(conversation_key)
        if self.tenant_registry is not None:
            try:
                subject = self.tenant_registry.resolve("wecom", conversation_key)
            except Exception:  # noqa: BLE001 - fall back to string subject
                pass
        tenant = subject if not isinstance(subject, str) else None
        if tenant is not None and self.conversation_store is not None:
            try:
                self.conversation_store.append_transcript(
                    tenant.tenant_id, "user", text
                )
            except Exception as exc:  # noqa: BLE001 - recording is best-effort
                print("企业微信记录用户消息失败：{}".format(exc), file=sys.stderr)
        try:
            outcome = self.agent_service.chat(
                subject, text, agent_id=routing.agent_id, source="wecom"
            )
            answer = str(getattr(outcome, "text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001 - reply with a safe error
            print("企业微信回复生成失败：{}".format(exc), file=sys.stderr)
            answer = "处理消息失败，请稍后重试。"
        if not answer:
            return None
        if tenant is not None and self.conversation_store is not None:
            try:
                self.conversation_store.append_transcript(
                    tenant.tenant_id, "assistant", answer
                )
            except Exception as exc:  # noqa: BLE001 - recording is best-effort
                print("企业微信记录回复失败：{}".format(exc), file=sys.stderr)
        return self._stream_reply(req_id, _truncate_utf8(answer))

    @staticmethod
    def _stream_reply(req_id: str, content: str) -> Dict[str, Any]:
        return {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": "stream",
                "stream": {"id": uuid.uuid4().hex, "finish": True, "content": content},
            },
        }
