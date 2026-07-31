"""Unit tests for the WeCom intelligent-robot long connection service."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from src.core.integrations.wecom_bot import WeComBotService, _truncate_utf8
from src.core.services.publish import PLATFORM_WECOM, PublishStore


@dataclass
class _Preset:
    id: str
    name: str
    enabled: bool = True
    greeting: str = ""


class _FakeOutcome:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAgentService:
    def __init__(self, agents):
        self.agents = agents
        self.calls = []

    def chat(self, subject, question, image_bytes=None, agent_id=None, source="wechat"):
        self.calls.append((subject, question, agent_id, source))
        return _FakeOutcome("答复：" + question)


@dataclass
class _FakeTenant:
    tenant_id: str
    bot_id: str
    user_id: str


class _FakeRegistry:
    def resolve(self, bot_id, user_id):
        return _FakeTenant("t-" + user_id, bot_id, user_id)


class _FakeConversationStore:
    def __init__(self):
        self.transcript = []

    def append_transcript(self, tenant_id, role, content, image=False):
        self.transcript.append((tenant_id, role, content))


def _msg_callback(text, req_id="req-1", msgid="m-1", chattype="single",
                  userid="zhang", chatid=""):
    body = {
        "msgid": msgid,
        "aibotid": "bot-1",
        "chattype": chattype,
        "from": {"userid": userid},
        "msgtype": "text",
        "text": {"content": text},
    }
    if chatid:
        body["chatid"] = chatid
    return {"cmd": "aibot_msg_callback", "headers": {"req_id": req_id}, "body": body}


class WeComBotServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = PublishStore(Path(self._tmp.name) / "publish.json")
        self.agents = {
            "general": _Preset("general", "通用助手", greeting="你好，我是通用助手"),
            "coder": _Preset("coder", "编程助手"),
        }
        self.agent_service = _FakeAgentService(self.agents)
        self.service = WeComBotService(self.agent_service, self.store)

    def _publish(self, agent_id="general"):
        self.store.publish(PLATFORM_WECOM, agent_id)

    def test_text_callback_classified_as_chat(self):
        self._publish()
        action, reply = self.service.handle_callback(_msg_callback("在吗"))
        self.assertEqual(action, "chat")
        self.assertIsNone(reply)

    def test_duplicate_msgid_ignored(self):
        self._publish()
        self.service.handle_callback(_msg_callback("在吗", msgid="dup"))
        action, _ = self.service.handle_callback(_msg_callback("在吗", msgid="dup"))
        self.assertEqual(action, "ignore")

    def test_disconnected_event_reports_kicked(self):
        payload = {
            "cmd": "aibot_event_callback",
            "headers": {"req_id": "r"},
            "body": {"msgid": "e-1", "msgtype": "event",
                     "event": {"eventtype": "disconnected_event"}},
        }
        action, _ = self.service.handle_callback(payload)
        self.assertEqual(action, "kicked")

    def test_enter_chat_returns_welcome_with_greeting(self):
        self._publish()
        payload = {
            "cmd": "aibot_event_callback",
            "headers": {"req_id": "r-9"},
            "body": {"msgid": "e-2", "msgtype": "event",
                     "event": {"eventtype": "enter_chat"}},
        }
        action, reply = self.service.handle_callback(payload)
        self.assertEqual(action, "send")
        self.assertEqual(reply["cmd"], "aibot_respond_welcome_msg")
        self.assertEqual(reply["headers"]["req_id"], "r-9")
        self.assertEqual(reply["body"]["text"]["content"], "你好，我是通用助手")

    def test_enter_chat_without_binding_returns_nothing(self):
        payload = {
            "cmd": "aibot_event_callback",
            "headers": {"req_id": "r"},
            "body": {"msgid": "e-3", "msgtype": "event",
                     "event": {"eventtype": "enter_chat"}},
        }
        action, reply = self.service.handle_callback(payload)
        self.assertEqual(action, "send")
        self.assertIsNone(reply)

    def test_chat_reply_uses_stream_and_callback_req_id(self):
        self._publish()
        reply = self.service.build_chat_reply(_msg_callback("你好", req_id="req-7"))
        self.assertEqual(reply["cmd"], "aibot_respond_msg")
        self.assertEqual(reply["headers"]["req_id"], "req-7")
        stream = reply["body"]["stream"]
        self.assertTrue(stream["finish"])
        self.assertEqual(stream["content"], "答复：你好")
        self.assertEqual(self.agent_service.calls[-1][3], "wecom")

    def test_group_message_strips_mention_and_routes_to_bound_agent(self):
        self._publish("coder")
        reply = self.service.build_chat_reply(
            _msg_callback("@小助手 帮我写代码", chattype="group", chatid="g-1")
        )
        self.assertEqual(self.agent_service.calls[-1][2], "coder")
        self.assertEqual(self.agent_service.calls[-1][1], "帮我写代码")
        self.assertIsNotNone(reply)

    def test_publish_replaces_bound_agent(self):
        self._publish("general")
        self._publish("coder")
        self.service.build_chat_reply(_msg_callback("你好"))
        self.assertEqual(self.agent_service.calls[-1][2], "coder")

    def test_no_binding_returns_none(self):
        reply = self.service.build_chat_reply(_msg_callback("你好"))
        self.assertIsNone(reply)

    def test_records_transcript_when_store_present(self):
        registry = _FakeRegistry()
        conv = _FakeConversationStore()
        service = WeComBotService(
            self.agent_service, self.store,
            tenant_registry=registry, conversation_store=conv,
        )
        self.store.publish(PLATFORM_WECOM, "general")
        reply = service.build_chat_reply(_msg_callback("在吗", userid="E64abc"))
        self.assertIsNotNone(reply)
        self.assertEqual(
            conv.transcript,
            [("t-E64abc", "user", "在吗"), ("t-E64abc", "assistant", "答复：在吗")],
        )

    def test_chat_failure_returns_safe_error(self):
        self._publish()

        def boom(*args, **kwargs):
            raise RuntimeError("模型挂了")

        self.agent_service.chat = boom
        reply = self.service.build_chat_reply(_msg_callback("你好"))
        self.assertEqual(
            reply["body"]["stream"]["content"], "处理消息失败，请稍后重试。"
        )

    def test_truncate_utf8_respects_limit(self):
        text = "很" * 30000
        result = _truncate_utf8(text, 300)
        self.assertLessEqual(len(result.encode("utf-8")), 310)


if __name__ == "__main__":
    unittest.main()
