"""Command-line interface for BotPlatform."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence, TextIO

from src.core.application.bot import (
    delete_credentials,
    display_qr_code,
    load_credentials,
    print_login_status,
    save_credentials,
)
from src.core.application.bootstrap import run_bot
from src.core.config.loader import load_project_config
from src.core.infrastructure.diagnostics import check_configuration, print_report
from src.core.infrastructure.instance_lock import AlreadyRunning, SingleInstanceLock
from src.core.integrations.images import ImageSource
from src.core.integrations.ilink import ILinkClient, ILinkError
from src.core.messaging import ChannelAddressStore, MessageRouter
from src.core.messaging.adapters import WeChatILinkAdapter
from src.core.paths import (
    CONFIG_DIR,
    DATA_DIR,
    INSTANCE_LOCK_PATH,
    SYSTEM_DATA_DIR,
    channel_credentials_path,
)
from src.core.plugins.codex_tasks import CodexHookIngestor, CodexTasksConfig
from src.core.services.notification import (
    NotificationEnqueueResult,
    NotificationError,
    NotificationService,
    TenantRecipientStore,
)
from src.core.storage.tenants import TenantRegistry, TenantStoreError


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BotPlatform 多渠道模型机器人")
    parser.add_argument(
        "--logout",
        action="store_true",
        help="删除已保存的微信登录凭证并重新扫码",
    )
    subparsers = parser.add_subparsers(dest="command")
    notify_parser = subparsers.add_parser(
        "notify",
        help="向指定租户发送一条微信通知",
    )
    notify_parser.add_argument("--user", required=True, help="目标租户编号")
    notify_parser.add_argument(
        "--channel",
        default="auto",
        help="消息渠道实例编号；默认 auto 使用最后活跃渠道",
    )
    notify_input = notify_parser.add_mutually_exclusive_group()
    notify_input.add_argument("--message", help="要发送的通知文本")
    notify_input.add_argument(
        "--stdin",
        action="store_true",
        help="从标准输入读取通知文本",
    )
    notify_image = notify_parser.add_mutually_exclusive_group()
    notify_image.add_argument("--image", help="要发送的本地图片路径")
    notify_image.add_argument("--image-url", help="要下载并发送的 HTTP(S) 图片 URL")
    subparsers.add_parser(
        "check-config",
        help="检查环境和配置，不启动机器人",
    )
    channel_parser = subparsers.add_parser(
        "channel",
        help="查看或管理消息渠道",
    )
    channel_parser.add_argument(
        "action",
        choices=("list", "status", "login", "logout"),
    )
    channel_parser.add_argument("channel_id", nargs="?")
    hook_parser = subparsers.add_parser(
        "codex-hook",
        help=argparse.SUPPRESS,
    )
    hook_parser.add_argument(
        "--stdin",
        action="store_true",
        required=True,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if args.command is not None and args.logout:
        parser.error("--logout 不能与子命令同时使用")
    if args.command == "notify" and not any(
        (args.message is not None, args.stdin, args.image, args.image_url)
    ):
        parser.error("notify 至少需要 --message、--stdin、--image 或 --image-url 之一")
    if args.command == "channel":
        if args.action in {"login", "logout"} and not args.channel_id:
            parser.error("channel {} 必须指定渠道实例编号".format(args.action))
        if args.action == "list" and args.channel_id:
            parser.error("channel list 不接受渠道实例编号")
    return args


def run_notify_command(
    args: argparse.Namespace,
    input_stream: Optional[TextIO] = None,
    output_stream: Optional[TextIO] = None,
    error_stream: Optional[TextIO] = None,
    service: Optional[NotificationService] = None,
) -> int:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    error_stream = error_stream or sys.stderr

    if args.stdin:
        try:
            message = input_stream.read()
        except OSError as exc:
            print("读取通知内容失败：{}".format(exc), file=error_stream)
            return 1
    else:
        message = args.message

    image_path = getattr(args, "image", None)
    image_url = getattr(args, "image_url", None)
    has_image = bool(image_path or image_url)
    if not has_image and (not isinstance(message, str) or not message.strip()):
        print("发送消息通知失败：通知内容不能为空。", file=error_stream)
        return 1
    caption = message if isinstance(message, str) and message.strip() else ""
    requested_channel = str(getattr(args, "channel", "auto") or "auto")
    attempt_immediately = requested_channel == "auto"

    tenant_id = str(getattr(args, "user", "") or "").strip()
    if not tenant_id:
        print("发送消息通知失败：必须使用 --user 指定目标租户。", file=error_stream)
        return 1
    if service is None:
        try:
            registry = TenantRegistry(DATA_DIR)
            registry.get(tenant_id)
            notification_service = NotificationService(
                credentials_loader=None,
                recipient_store=TenantRecipientStore(registry),
                message_router=MessageRouter(
                    [
                        WeChatILinkAdapter(
                            ILinkClient(
                                credentials=load_credentials(
                                    channel_credentials_path(channel.id)
                                )
                            ),
                            channel_id=channel.id,
                        )
                        for channel in load_project_config(
                            CONFIG_DIR
                        ).channels.values()
                        if channel.enabled
                    ]
                ),
                address_store=ChannelAddressStore(registry),
            )
        except (TenantStoreError, ILinkError, OSError, ValueError) as exc:
            print("发送消息通知失败：{}。".format(exc), file=error_stream)
            return 1
    else:
        notification_service = service
    try:
        enqueue_image = getattr(
            notification_service, "enqueue_image_to_tenant", None
        )
        enqueue_text = getattr(
            notification_service, "enqueue_text_to_tenant", None
        )
        if image_path:
            local_path = Path(image_path).expanduser()
            if not local_path.is_absolute():
                local_path = Path.cwd() / local_path
            source = ImageSource.local(local_path)
            if callable(enqueue_image):
                queued = enqueue_image(
                    tenant_id,
                    source,
                    caption=caption,
                    source_type="cli",
                    attempt_immediately=attempt_immediately,
                )
            else:
                notification_service.send_image_to_tenant(
                    tenant_id, source, caption=caption
                )
                queued = None
        elif image_url:
            source = ImageSource.remote(image_url)
            if callable(enqueue_image):
                queued = enqueue_image(
                    tenant_id,
                    source,
                    caption=caption,
                    source_type="cli",
                    attempt_immediately=attempt_immediately,
                )
            else:
                notification_service.send_image_to_tenant(
                    tenant_id, source, caption=caption
                )
                queued = None
        else:
            if callable(enqueue_text):
                queued = enqueue_text(
                    tenant_id,
                    message,
                    source_type="cli",
                    attempt_immediately=attempt_immediately,
                )
            else:
                notification_service.send_text_to_tenant(tenant_id, message)
                queued = None
    except NotificationError as exc:
        print("发送消息通知失败：{}。".format(exc), file=error_stream)
        return 1

    if (
        queued is not None
        and requested_channel != "auto"
        and hasattr(notification_service, "pin_channel")
    ):
        try:
            notification_service.pin_channel(
                queued.notification_ids,
                tenant_id,
                requested_channel,
            )
            notification_service._attempt_immediate()
            queued = NotificationEnqueueResult(
                queued.notification_ids,
                notification_service.outbox.status(queued.notification_ids),
            )
        except NotificationError as exc:
            print("发送消息通知失败：{}。".format(exc), file=error_stream)
            return 1

    if queued is None or queued.status == "sent":
        success_message = "图片通知已发送。" if has_image else "消息通知已发送。"
    else:
        success_message = (
            "图片通知已保存，等待补发。"
            if has_image
            else "消息通知已保存，等待补发。"
        )
    print(success_message, file=output_stream)
    return 0


def run_channel_command(
    args: argparse.Namespace,
    output_stream: Optional[TextIO] = None,
    error_stream: Optional[TextIO] = None,
) -> int:
    output_stream = output_stream or sys.stdout
    error_stream = error_stream or sys.stderr
    try:
        config = load_project_config(CONFIG_DIR)
    except Exception as exc:
        print("读取渠道配置失败：{}。".format(exc), file=error_stream)
        return 1

    if args.action == "list":
        for channel in config.channels.values():
            state = "启用" if channel.enabled else "停用"
            print(
                "{}\t{}\t{}".format(channel.id, channel.type, state),
                file=output_stream,
            )
        return 0

    channel_id = str(args.channel_id or "")
    if args.action == "status" and not channel_id:
        for item in config.channels.values():
            credential_state = (
                "已保存凭证"
                if load_credentials(channel_credentials_path(item.id))
                else "需要登录"
            )
            print(
                "{}：{}，{}。".format(
                    item.id,
                    "已启用" if item.enabled else "未启用",
                    credential_state if item.type == "wechat_ilink" else "未检查",
                ),
                file=output_stream,
            )
        return 0
    channel = config.channels.get(channel_id)
    if channel is None:
        print("未知消息渠道：{}。".format(channel_id), file=error_stream)
        return 1
    if args.action == "status":
        credential_path = channel_credentials_path(channel.id)
        credential_state = (
            "已保存凭证" if load_credentials(credential_path) else "需要登录"
        )
        print(
            "{}：{}，{}。".format(
                channel.id,
                "已启用" if channel.enabled else "未启用",
                credential_state,
            ),
            file=output_stream,
        )
        return 0
    if args.action == "logout":
        delete_credentials(channel_credentials_path(channel.id))
        print("已清除渠道 {} 的登录凭证。".format(channel.id), file=output_stream)
        return 0
    try:
        credential_path = channel_credentials_path(channel.id)
        existing = load_credentials(credential_path)
        if existing is not None:
            print(
                "渠道 {} 已有登录凭证，bot_id={}。".format(
                    channel.id, existing.bot_id
                ),
                file=output_stream,
            )
            return 0
        with ILinkClient() as client:
            credentials = client.login(
                display_qr_code,
                status_changed=print_login_status,
            )
            save_credentials(credentials, credential_path)
    except (ILinkError, OSError) as exc:
        print("渠道登录失败：{}。".format(exc), file=error_stream)
        return 1
    print("渠道 {} 登录成功。".format(channel.id), file=output_stream)
    return 0


def run_codex_hook_command(
    args: argparse.Namespace,
    input_stream: Optional[TextIO] = None,
    output_stream: Optional[TextIO] = None,
    error_stream: Optional[TextIO] = None,
) -> int:
    """Ingest a lifecycle hook without taking the bot's single-instance lock."""

    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    error_stream = error_stream or sys.stderr
    payload: object = {}
    try:
        payload = json.loads(input_stream.read())
        if not isinstance(payload, dict):
            raise ValueError("hook 输入必须是 JSON 对象")
        project_config = load_project_config(CONFIG_DIR)
        plugin_config = project_config.plugins.get("codex_tasks")
        if plugin_config is None or not plugin_config.enabled:
            raise ValueError("codex_tasks 插件未启用")
        registry = TenantRegistry(DATA_DIR)
        config = CodexTasksConfig.from_mapping(
            plugin_config.settings,
            CONFIG_DIR.parent,
        )
        CodexHookIngestor(config, registry).ingest(payload)
    except Exception as exc:
        safe_error = str(exc).strip().splitlines()[0] or type(exc).__name__
        print(
            "Codex hook 采集失败：{}".format(safe_error[:1000]),
            file=error_stream,
        )

    event_name = (
        str(payload.get("hook_event_name") or "")
        if isinstance(payload, dict)
        else ""
    )
    response = {"continue": True} if event_name in {"UserPromptSubmit", "Stop"} else {}
    print(json.dumps(response, ensure_ascii=False), file=output_stream)
    return 0


def _load_model_env() -> None:
    """Load API keys from data/system/model.env if present."""
    import os

    env_file = SYSTEM_DATA_DIR / "model.env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and value and not os.environ.get(key):
                os.environ[key] = value


def main(argv: Optional[Sequence[str]] = None) -> int:
    _load_model_env()
    args = parse_args(argv)
    if args.command == "notify":
        return run_notify_command(args)

    if args.command == "codex-hook":
        return run_codex_hook_command(args)

    if args.command == "channel":
        return run_channel_command(args)

    if args.command == "check-config":
        report = check_configuration(CONFIG_DIR)
        print_report(report, sys.stdout)
        return 0 if report.ok else 1

    instance_lock = SingleInstanceLock(INSTANCE_LOCK_PATH)
    try:
        instance_lock.acquire()
    except AlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print("启动失败：无法获取机器人运行锁：{}".format(exc), file=sys.stderr)
        return 1

    try:
        report = check_configuration(CONFIG_DIR)
        print_report(report, sys.stdout)
        if not report.ok or report.config is None:
            return 1
        return run_bot(args, report.config)
    finally:
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
