from __future__ import annotations

import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import contextmanager
from contextlib import redirect_stdout
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import src.core.application.cli as main_module
import src.core.application.bootstrap as bootstrap_module
from src.core.services.agent import AgentService
from src.core.config.loader import load_project_config
from src.core.integrations.ilink import Credentials
from src.core.infrastructure.instance_lock import SingleInstanceLock
from src.core.infrastructure.logging import log_interaction
from src.core.application import (
    WeChatBot,
    load_credentials,
    parse_args,
    run_channel_command,
    run_notify_command,
    run_codex_hook_command,
    save_credentials,
)
from src.core.modeling import (
    CanonicalMessage,
    ModelCapabilities,
    ModelError,
    ModelIdentity,
    ModelResponse,
)
from src.core.tooling import ApprovalRequired, FinalAnswer


def text_message(user: str, text: str, **extra):
    message = {
        "message_type": 1,
        "from_user_id": user,
        "context_token": "context-{}".format(user),
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    message.update(extra)
    return message


def image_message(user: str, text: str = ""):
    items = [{"type": 2, "image_item": {"media": {"id": "image"}}}]
    if text:
        items.insert(0, {"type": 1, "text_item": {"text": text}})
    return {
        "message_type": 1,
        "from_user_id": user,
        "context_token": "context-{}".format(user),
        "item_list": items,
    }


class FakeILink:
    def __init__(self) -> None:
        self.sent = []
        self.downloaded = []
        self.typing_events = []

    @contextmanager
    def typing(self, user_id, context_token, on_error=None):
        self.typing_events.append(("start", user_id, context_token))
        try:
            yield
        finally:
            self.typing_events.append(("stop", user_id, context_token))

    def send_text(self, user_id, context_token, text):
        self.sent.append((user_id, context_token, text))

    def download_image(self, image_item):
        self.downloaded.append(image_item)
        return b"image-bytes"


class FakeOllama:
    def __init__(self) -> None:
        self.calls = []

    identity = ModelIdentity("test", "fake", "fake-model")
    capabilities = ModelCapabilities(tools=True, vision=True)

    def complete(self, request):
        self.calls.append(request)
        return ModelResponse(
            CanonicalMessage("assistant", "回答{}".format(len(self.calls))),
            actual_model="fake-model",
        )


class FakeNotificationService:
    def __init__(self, status="sent") -> None:
        self.messages = []
        self.images = []
        self.status = status

    def send_text_to_tenant(self, tenant_id, message):
        self.messages.append((tenant_id, message))

    def send_image_to_tenant(self, tenant_id, source, caption=""):
        self.images.append((tenant_id, source, caption))

    def enqueue_text_to_tenant(self, tenant_id, message, **_kwargs):
        self.messages.append((tenant_id, message))
        return SimpleNamespace(status=self.status)

    def enqueue_image_to_tenant(self, tenant_id, source, caption="", **_kwargs):
        self.images.append((tenant_id, source, caption))
        return SimpleNamespace(status=self.status)


class ManualTimer:
    def __init__(self, delay, callback) -> None:
        self.delay = delay
        self.callback = callback
        self.started = False
        self.cancelled = False
        self.daemon = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.callback()


class MainBotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ilink = FakeILink()
        self.ollama = FakeOllama()
        config = load_project_config(Path(__file__).resolve().parents[1] / "config")
        self.agent_service = AgentService(self.ollama, config.app, config.agents)
        self.logs = []
        self.recipients = []
        self.bot = WeChatBot(
            self.ilink,
            self.agent_service,
            interaction_logger=lambda direction, user, content: self.logs.append(
                (direction, user, content)
            ),
            recipient_recorder=lambda user, context: self.recipients.append(
                (user, context)
            ),
        )

    def test_text_history_is_isolated_by_user_and_limited(self) -> None:
        self.bot.handle_message(text_message("user-a", "第一问"))
        self.bot.handle_message(text_message("user-b", "另一个用户"))
        self.bot.handle_message(text_message("user-a", "第二问"))

        third_call_messages = self.ollama.calls[2].messages
        self.assertIn(CanonicalMessage("user", "第一问"), third_call_messages)
        self.assertNotIn(CanonicalMessage("user", "另一个用户"), third_call_messages)

        for index in range(7):
            self.bot.handle_message(text_message("user-a", "追加{}".format(index)))
        self.assertEqual(len(self.agent_service.histories["user-a"]), 12)
        self.assertEqual(self.ilink.typing_events[0][0], "start")
        self.assertEqual(self.ilink.typing_events[1][0], "stop")

    def test_thinking_is_combined_with_answer_after_typing_stops(self) -> None:
        class ThinkingOllama(FakeOllama):
            capabilities = ModelCapabilities(tools=True, vision=True, reasoning=True)

            def complete(self, request):
                self.calls.append(request)
                return ModelResponse(
                    CanonicalMessage(
                        "assistant",
                        "最终答案",
                        extensions={"thinking": "原始思考"},
                    ),
                    actual_model="fake-model",
                )

        ollama = ThinkingOllama()
        config = load_project_config(Path(__file__).resolve().parents[1] / "config")
        bot = WeChatBot(
            self.ilink,
            AgentService(ollama, config.app, config.agents),
            interaction_logger=lambda *_args: None,
        )
        bot.handle_message(text_message("user-a", "问题"))

        self.assertEqual(
            self.ilink.sent[-1][2],
            "思考过程：\n原始思考\n\n回答：\n最终答案",
        )
        self.assertEqual(
            [event[0] for event in self.ilink.typing_events[-2:]],
            ["start", "stop"],
        )

    def test_model_failure_stops_typing_before_error_reply(self) -> None:
        class FailingOllama(FakeOllama):
            def complete(self, request):
                raise ModelError("模型暂不可用", provider="fake")

        config = load_project_config(Path(__file__).resolve().parents[1] / "config")
        bot = WeChatBot(
            self.ilink,
            AgentService(FailingOllama(), config.app, config.agents),
            interaction_logger=lambda *_args: None,
        )
        bot.handle_message(text_message("user-a", "问题"))

        self.assertEqual(
            [event[0] for event in self.ilink.typing_events[-2:]],
            ["start", "stop"],
        )
        self.assertIn("处理消息失败", self.ilink.sent[-1][2])

    def test_clear_command_clears_only_current_user(self) -> None:
        self.bot.handle_message(text_message("user-a", "你好"))
        self.bot.handle_message(text_message("user-b", "你好"))
        self.bot.handle_message(text_message("user-a", "/clear"))
        self.assertNotIn("user-a", self.agent_service.histories)
        self.assertIn("user-b", self.agent_service.histories)
        self.assertEqual(self.ilink.sent[-1][2], "对话上下文已清空。")

    def test_image_is_downloaded_and_sent_to_ollama_immediately(self) -> None:
        self.bot.handle_message(image_message("user-a"))
        request = self.ollama.calls[0]
        self.assertEqual(request.image, b"image-bytes")
        self.assertEqual(request.messages[-1].content, "请描述这张图片，并识别图片中的文字。")

        self.bot.handle_message(image_message("user-a", "图片里有几个人？"))
        request = self.ollama.calls[1]
        self.assertEqual(request.messages[-1].content, "图片里有几个人？")
        self.assertEqual(request.image, b"image-bytes")

    def test_group_and_bot_messages_are_ignored(self) -> None:
        self.bot.handle_message(text_message("user", "群消息", group_id="group"))
        self.bot.handle_message(
            {
                "message_type": 2,
                "from_user_id": "bot",
                "context_token": "context",
                "item_list": [{"type": 1, "text_item": {"text": "自己"}}],
            }
        )
        self.assertEqual(self.ollama.calls, [])

    def test_agent_help_and_recipient_recording(self) -> None:
        self.bot.handle_message(text_message("user-a", "/agent"))
        self.assertIn("当前 Agent：通用 AI 助手", self.ilink.sent[-1][2])
        self.assertIn("支持能力", self.ilink.sent[-1][2])

        self.bot.handle_message(text_message("user-a", "/help"))
        self.assertIn("/agent", self.ilink.sent[-1][2])
        self.assertIn("/clear", self.ilink.sent[-1][2])
        self.assertEqual(
            self.recipients[-1], ("user-a", "context-user-a")
        )

        self.bot.handle_message(text_message("user-a", "/tools"))
        self.assertIn("未启用本机工具", self.ilink.sent[-1][2])

    def test_model_command_is_scoped_to_current_user(self) -> None:
        self.bot.handle_message(text_message("user-a", "/model"))
        self.assertIn("当前模型模式", self.ilink.sent[-1][2])
        self.bot.handle_message(text_message("user-a", "/model flash"))
        self.assertIn("当前模型模式", self.ilink.sent[-1][2])
        self.bot.handle_message(text_message("user-b", "/model"))
        self.assertIn("auto", self.ilink.sent[-1][2])
        self.bot.handle_message(text_message("user-a", "/model invalid"))
        self.assertIn("未知模型模式", self.ilink.sent[-1][2])
        self.bot.handle_message(text_message("user-a", "/model auto extra"))
        self.assertIn("格式错误", self.ilink.sent[-1][2])

    def test_approval_command_requires_exact_format_and_pending_request(self) -> None:
        self.bot.handle_message(text_message("user-a", "/approve"))
        self.assertIn("格式错误", self.ilink.sent[-1][2])
        self.bot.handle_message(text_message("user-a", "/approve abc123"))
        self.assertIn("没有待确认", self.ilink.sent[-1][2])

    def test_chinese_approval_words_are_exact_and_only_apply_when_pending(self) -> None:
        for word in ("同意", "确认", "不同意", "拒绝", "取消"):
            with self.subTest(word=word), patch.object(
                self.agent_service, "has_pending_approval", return_value=True
            ), patch.object(
                self.agent_service,
                "resolve_pending_approval",
                return_value=FinalAnswer("已处理"),
            ) as resolve:
                self.bot.handle_message(text_message("user-a", "  {}  ".format(word)))
                resolve.assert_called_once_with(
                    "user-a", approved=word in {"同意", "确认"}
                )
                self.assertEqual(self.ilink.sent[-1][2], "已处理")

        calls_before = len(self.ollama.calls)
        self.bot.handle_message(text_message("user-a", "我还没有确认"))
        self.bot.handle_message(text_message("user-a", "确认。"))
        self.bot.handle_message(text_message("user-a", "同意"))
        self.assertEqual(len(self.ollama.calls), calls_before + 3)

    def test_approval_timeout_discards_request_and_notifies_once(self) -> None:
        timers = []

        def timer_factory(delay, callback):
            timer = ManualTimer(delay, callback)
            timers.append(timer)
            return timer

        bot = WeChatBot(
            self.ilink,
            self.agent_service,
            interaction_logger=lambda direction, user, content: self.logs.append(
                (direction, user, content)
            ),
            timer_factory=timer_factory,
        )
        pending = ApprovalRequired(
            "approval-1",
            "需要确认\n回复“同意”或“确认”",
            datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        with patch.object(
            self.agent_service, "chat", return_value=pending
        ) as chat, patch.object(
            self.agent_service, "expire_approval", side_effect=[True, False]
        ) as expire:
            bot.handle_message(text_message("user-a", "创建文件"))
            self.assertTrue(timers[0].started)
            self.assertTrue(timers[0].daemon)
            self.assertEqual(timers[0].delay, 0)

            timers[0].fire()
            timers[0].fire()

        chat.assert_called_once()
        self.assertEqual(expire.call_count, 2)
        timeout_messages = [
            item for item in self.ilink.sent if "确认已超时" in item[2]
        ]
        self.assertEqual(len(timeout_messages), 1)
        self.assertEqual(timeout_messages[0][1], "context-user-a")

    def test_new_pending_request_replaces_timer_and_clear_cancels_it(self) -> None:
        timers = []

        def timer_factory(delay, callback):
            timer = ManualTimer(delay, callback)
            timers.append(timer)
            return timer

        bot = WeChatBot(
            self.ilink,
            self.agent_service,
            interaction_logger=lambda *_args: None,
            timer_factory=timer_factory,
        )
        first = ApprovalRequired(
            "approval-1",
            "第一次确认",
            datetime.now(timezone.utc) + timedelta(seconds=300),
        )
        second = ApprovalRequired(
            "approval-2",
            "第二次确认",
            datetime.now(timezone.utc) + timedelta(seconds=300),
        )
        with patch.object(
            self.agent_service, "chat", side_effect=[first, second]
        ):
            bot.handle_message(text_message("user-a", "第一次"))
            bot.handle_message(text_message("user-a", "第二次"))

        self.assertTrue(timers[0].cancelled)
        self.assertFalse(timers[1].cancelled)
        bot.handle_message(text_message("user-a", "/clear"))
        self.assertTrue(timers[1].cancelled)

    def test_input_and_output_are_logged_and_user_id_is_masked(self) -> None:
        output = StringIO()
        bot = WeChatBot(
            self.ilink,
            self.agent_service,
            interaction_logger=log_interaction,
        )
        user_id = "o9cq800kum_secret@im.wechat"
        with redirect_stdout(output):
            bot.handle_message(text_message(user_id, "你好，机器人"))

        log_text = output.getvalue()
        self.assertIn("微信输入", log_text)
        self.assertIn("你好，机器人", log_text)
        self.assertIn("微信输出", log_text)
        self.assertIn("回答1", log_text)
        self.assertNotIn(user_id, log_text)

    def test_credentials_are_saved_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            expected = Credentials("token", "https://gateway", "bot", "owner")
            save_credentials(expected, path)
            actual = load_credentials(path)
            self.assertEqual(actual, expected)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["token"], "token")

    def test_runtime_data_is_git_ignored(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.assertIn("data/", (project_root / ".gitignore").read_text())

    def test_notify_cli_supports_message_and_stdin_without_changing_text(self) -> None:
        service = FakeNotificationService()
        stdout = StringIO()
        stderr = StringIO()
        result = run_notify_command(
            Namespace(message="命令行通知", stdin=False, user="tenant-a"),
            output_stream=stdout,
            error_stream=stderr,
            service=service,
        )
        self.assertEqual(result, 0)
        self.assertEqual(service.messages, [("tenant-a", "命令行通知")])
        self.assertIn("已发送", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

        result = run_notify_command(
            Namespace(message=None, stdin=True, user="tenant-a"),
            input_stream=StringIO("  第一行\n第二行\n"),
            output_stream=stdout,
            error_stream=stderr,
            service=service,
        )
        self.assertEqual(result, 0)
        self.assertEqual(service.messages[-1], ("tenant-a", "  第一行\n第二行\n"))

        queued = FakeNotificationService(status="queued")
        queued_output = StringIO()
        result = run_notify_command(
            Namespace(message="离线通知", stdin=False, user="tenant-a"),
            output_stream=queued_output,
            service=queued,
        )
        self.assertEqual(result, 0)
        self.assertIn("已保存，等待补发", queued_output.getvalue())

    def test_notify_cli_rejects_empty_input_and_invalid_argument_combinations(self) -> None:
        service = FakeNotificationService()
        stderr = StringIO()
        result = run_notify_command(
            Namespace(message=None, stdin=True, user="tenant-a"),
            input_stream=StringIO(" \n "),
            error_stream=stderr,
            service=service,
        )
        self.assertEqual(result, 1)
        self.assertEqual(service.messages, [])
        self.assertIn("不能为空", stderr.getvalue())

        with self.assertRaises(SystemExit) as missing:
            parse_args(["notify"])
        self.assertEqual(missing.exception.code, 2)
        with self.assertRaises(SystemExit) as both:
            parse_args(["notify", "--user", "tenant-a", "--message", "通知", "--stdin"])
        self.assertEqual(both.exception.code, 2)
        with self.assertRaises(SystemExit) as both_images:
            parse_args(
                [
                    "notify",
                    "--user",
                    "tenant-a",
                    "--image",
                    "one.png",
                    "--image-url",
                    "https://example.test/two.png",
                ]
            )
        self.assertEqual(both_images.exception.code, 2)

    def test_channel_cli_lists_and_reports_configured_channel(self) -> None:
        args = parse_args(["channel", "status"])
        self.assertEqual(args.action, "status")
        output = StringIO()
        result = run_channel_command(
            Namespace(action="list", channel_id=None),
            output_stream=output,
        )
        self.assertEqual(result, 0)
        self.assertIn("wechat-main\twechat_ilink\t启用", output.getvalue())

    def test_notify_cli_supports_local_or_remote_image_with_optional_caption(self) -> None:
        service = FakeNotificationService()
        stdout = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            relative = Path(directory) / "alert.png"
            result = run_notify_command(
                Namespace(
                    message="图片说明",
                    stdin=False,
                    image=str(relative),
                    image_url=None,
                    user="tenant-a",
                ),
                output_stream=stdout,
                service=service,
            )
        self.assertEqual(result, 0)
        tenant_id, source, caption = service.images[-1]
        self.assertEqual(tenant_id, "tenant-a")
        self.assertEqual(source.kind, "path")
        self.assertEqual(Path(source.value).resolve(), relative.resolve())
        self.assertEqual(caption, "图片说明")
        self.assertIn("图片通知已发送", stdout.getvalue())

        result = run_notify_command(
            Namespace(
                message=None,
                stdin=True,
                image=None,
                image_url="http://127.0.0.1/report.png?token=secret",
                user="tenant-a",
            ),
            input_stream=StringIO(" \n "),
            output_stream=stdout,
            service=service,
        )
        self.assertEqual(result, 0)
        tenant_id, source, caption = service.images[-1]
        self.assertEqual(tenant_id, "tenant-a")
        self.assertEqual(source.kind, "url")
        self.assertEqual(source.value, "http://127.0.0.1/report.png?token=secret")
        self.assertEqual(caption, "")

    def test_notify_command_returns_before_model_initialization(self) -> None:
        with patch.object(main_module, "run_notify_command", return_value=0) as notify:
            with patch.object(bootstrap_module, "build_core_services") as build_services:
                result = main_module.main(["notify", "--user", "tenant-a", "--message", "告警"])
        self.assertEqual(result, 0)
        notify.assert_called_once()
        build_services.assert_not_called()

    def test_duplicate_main_exits_before_bot_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "bot.lock"
            stderr = StringIO()
            with SingleInstanceLock(lock_path):
                with patch.object(main_module, "INSTANCE_LOCK_PATH", lock_path):
                    with patch.object(
                        main_module,
                        "run_bot",
                        side_effect=AssertionError("must not initialize"),
                    ) as run_bot:
                        with redirect_stderr(stderr):
                            result = main_module.main([])

            self.assertEqual(result, 1)
            self.assertIn("机器人已启动，请勿重复运行", stderr.getvalue())
            run_bot.assert_not_called()

    def test_notify_bypasses_main_instance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "bot.lock"
            with SingleInstanceLock(lock_path):
                with patch.object(main_module, "INSTANCE_LOCK_PATH", lock_path):
                    with patch.object(
                        main_module, "run_notify_command", return_value=0
                    ) as notify:
                        result = main_module.main(
                            ["notify", "--user", "tenant-a", "--message", "告警"]
                        )

            self.assertEqual(result, 0)
            notify.assert_called_once()

    def test_malformed_codex_hook_is_non_blocking_and_bypasses_lock(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        result = run_codex_hook_command(
            Namespace(stdin=True),
            input_stream=StringIO("{not-json"),
            output_stream=stdout,
            error_stream=stderr,
        )
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {})
        self.assertIn("采集失败", stderr.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "bot.lock"
            with SingleInstanceLock(lock_path):
                with patch.object(main_module, "INSTANCE_LOCK_PATH", lock_path):
                    with patch.object(
                        main_module,
                        "run_codex_hook_command",
                        return_value=0,
                    ) as hook:
                        result = main_module.main(["codex-hook", "--stdin"])
        self.assertEqual(result, 0)
        hook.assert_called_once()


if __name__ == "__main__":
    unittest.main()
