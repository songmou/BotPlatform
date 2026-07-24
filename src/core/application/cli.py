"""Command-line interface for BotPlatform."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence, TextIO

from src.core.application.bot import load_credentials
from src.core.application.bootstrap import run_bot
from src.core.config.loader import load_project_config
from src.core.infrastructure.diagnostics import check_configuration, print_report
from src.core.infrastructure.instance_lock import AlreadyRunning, SingleInstanceLock
from src.core.integrations.images import ImageSource
from src.core.paths import CONFIG_DIR, CREDENTIALS_PATH, DATA_DIR, INSTANCE_LOCK_PATH
from src.core.plugins.codex_tasks import CodexHookIngestor, CodexTasksConfig
from src.core.services.notification import (
    NotificationError,
    NotificationService,
    TenantRecipientStore,
)
from src.core.storage.tenants import TenantRegistry, TenantStoreError


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="微信 iLink 多模型机器人")
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
        print("发送微信通知失败：通知内容不能为空。", file=error_stream)
        return 1
    caption = message if isinstance(message, str) and message.strip() else ""

    tenant_id = str(getattr(args, "user", "") or "").strip()
    if not tenant_id:
        print("发送微信通知失败：必须使用 --user 指定目标租户。", file=error_stream)
        return 1
    if service is None:
        try:
            registry = TenantRegistry(DATA_DIR)
            registry.get(tenant_id)
        except TenantStoreError as exc:
            print("发送微信通知失败：{}。".format(exc), file=error_stream)
            return 1
        notification_service = NotificationService(
            credentials_loader=lambda: load_credentials(CREDENTIALS_PATH),
            recipient_store=TenantRecipientStore(registry),
        )
    else:
        notification_service = service
    try:
        if image_path:
            local_path = Path(image_path).expanduser()
            if not local_path.is_absolute():
                local_path = Path.cwd() / local_path
            source = ImageSource.local(local_path)
            notification_service.send_image_to_tenant(
                tenant_id, source, caption=caption
            )
        elif image_url:
            source = ImageSource.remote(image_url)
            notification_service.send_image_to_tenant(
                tenant_id, source, caption=caption
            )
        else:
            notification_service.send_text_to_tenant(tenant_id, message)
    except NotificationError as exc:
        print("发送微信通知失败：{}。".format(exc), file=error_stream)
        return 1

    success_message = "微信图片通知已发送。" if has_image else "微信通知已发送。"
    print(success_message, file=output_stream)
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.command == "notify":
        return run_notify_command(args)

    if args.command == "codex-hook":
        return run_codex_hook_command(args)

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
