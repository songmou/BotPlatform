#!/usr/bin/env python3
"""Web management panel entry point.

Default mode runs the WeChat bot and the web panel in one process sharing a
single service graph; ``--panel-only`` keeps the previous panel-only mode.
"""

from __future__ import annotations

import argparse
import sys


def _load_model_env() -> None:
    """Load API keys from data/system/model.env if present."""
    import os
    from src.core.paths import SYSTEM_DATA_DIR

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


def _load_tool_states(data_dir):
    """Read persisted per-tool enable/disable switches, tolerating bad JSON."""
    tool_state_path = data_dir / "tool_state.json"
    if not tool_state_path.exists():
        return {}
    try:
        import json as _json

        return _json.loads(
            tool_state_path.read_text(encoding="utf-8")
        ).get("tools", {})
    except Exception as exc:
        print(
            "警告：解析 {} 失败，已忽略工具开关状态：{}".format(tool_state_path, exc),
            file=sys.stderr,
        )
        return {}


def _build_admin_auth(registry):
    """Create admin auth stores and bootstrap the default admin account."""
    from src.core.paths import SYSTEM_DATA_DIR
    from src.core.services.auth import AdminAuthService
    from src.core.storage.admin_users import (
        AdminRoleStore,
        AdminSessionStore,
        AdminUserStore,
        load_or_create_session_secret,
    )

    admin_user_store = AdminUserStore(registry.database)
    admin_role_store = AdminRoleStore(registry.database)
    admin_session_store = AdminSessionStore(
        registry.database, load_or_create_session_secret(SYSTEM_DATA_DIR)
    )
    admin_auth = AdminAuthService(
        admin_user_store, admin_role_store, admin_session_store, SYSTEM_DATA_DIR
    )
    initial_password = admin_auth.bootstrap_default_admin()
    return admin_auth, admin_user_store, admin_role_store, initial_password


def _print_panel_banner(args, initial_password) -> None:
    from src.core.paths import SYSTEM_DATA_DIR

    print("Web 管理面板已启动：http://{}:{}".format(args.host, args.port))
    if initial_password:
        print(
            "已生成默认管理员账号 admin，初始密码：{}".format(initial_password),
            file=sys.stderr,
        )
        print(
            "初始密码已保存到 {}，请登录后立即修改并删除该文件。".format(
                SYSTEM_DATA_DIR / "admin_initial_password"
            ),
            file=sys.stderr,
        )
    print("浏览器打开 http://{}:{}/login 登录".format(args.host, args.port))


def _run_combined(args) -> int:
    """Run the WeChat bot (main thread) plus the panel (uvicorn thread)."""
    import threading

    from src.api.app import create_app
    from src.core.application.bootstrap import (
        _install_sigterm_handler,
        build_bot_runtime,
        run_channel_loop,
    )
    from src.core.application.services import build_core_services
    from src.core.config.loader import ConfigError, load_project_config
    from src.core.infrastructure.instance_lock import (
        AlreadyRunning,
        SingleInstanceLock,
    )
    from src.core.infrastructure.logging import log_model_call, log_model_fallback
    from src.core.modeling import ModelError
    from src.core.paths import CONFIG_DIR, DATA_DIR, INSTANCE_LOCK_PATH
    from src.core.storage.tenants import TenantStoreError
    from src.core.storage.tool_audit import ToolAuditStore
    from src.core.storage.drive_audit import DriveAuditStore

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
        try:
            config = load_project_config(CONFIG_DIR)
        except ConfigError as exc:
            print("配置错误：{}".format(exc), file=sys.stderr)
            return 1

        try:
            services = build_core_services(
                config,
                DATA_DIR,
                model_call_logger=log_model_call,
                fallback_logger=log_model_fallback,
                strict_models=False,
            )
        except TenantStoreError as exc:
            print("租户数据加载失败：{}".format(exc), file=sys.stderr)
            return 1
        except ModelError as exc:
            print("错误：{}".format(exc), file=sys.stderr)
            print(
                "提示：将 DEEPSEEK_API_KEY=你的密钥 写入 data/system/model.env",
                file=sys.stderr,
            )
            return 1
        for warning in services.model_warnings:
            print("警告：{}".format(warning), file=sys.stderr)

        try:
            registry = services.tenant_registry
            (
                admin_auth,
                admin_user_store,
                admin_role_store,
                initial_password,
            ) = _build_admin_auth(registry)
            tool_audit_store = ToolAuditStore(registry)
            drive_audit_store = DriveAuditStore(registry)
            runtime = build_bot_runtime(
                config,
                services,
                tool_audit_store=tool_audit_store,
                tool_states=_load_tool_states(DATA_DIR),
                drive_audit_store=drive_audit_store,
            )
        except (ModelError, ValueError) as exc:
            services.close()
            print("模型客户端创建失败：{}".format(exc), file=sys.stderr)
            return 1

        try:
            app = create_app(
                config,
                services.model_router,
                registry,
                services.conversation_store,
                tool_runtime=runtime.tool_runtime,
                knowledge_service=services.knowledge_service,
                drive_service=services.drive_service,
                drive_audit_store=drive_audit_store,
                plugin_context=runtime.plugin_context,
                plugin_manager=runtime.plugin_manager,
                scheduler=runtime.scheduler,
                tool_audit_store=tool_audit_store,
                model_analytics_store=services.model_analytics_store,
                admin_auth=admin_auth,
                admin_user_store=admin_user_store,
                admin_role_store=admin_role_store,
                script_service=runtime.script_service,
                script_registry=runtime.external_script_registry,
                script_schedule_service=runtime.script_schedule_service,
                channel_statuses=runtime.channel_statuses,
                secure_cookies=args.behind_https,
                owns_services=False,
            )

            import uvicorn

            server = uvicorn.Server(
                uvicorn.Config(
                    app, host=args.host, port=args.port, log_level="warning"
                )
            )
            # uvicorn skips signal handler installation off the main thread,
            # so KeyboardInterrupt/SIGTERM stay with the channel loop below.
            server_thread = threading.Thread(
                target=server.run, name="web-panel", daemon=True
            )
            server_thread.start()
            _print_panel_banner(args, initial_password)

            _install_sigterm_handler()
            try:
                services.model_router.ensure_ready()
            except ModelError as exc:
                print(
                    "警告：模型暂不可用，机器人将降级运行：{}".format(exc),
                    file=sys.stderr,
                )
            runtime.start()
            try:
                return run_channel_loop(runtime, config)
            finally:
                server.should_exit = True
                server_thread.join(timeout=5.0)
        finally:
            runtime.shutdown()
            services.close()
    finally:
        instance_lock.release()


def _run_panel_only(args) -> int:
    """Previous behaviour: web panel without the WeChat message channels."""
    from src.core.application.services import build_core_services
    from src.core.config.loader import ConfigError, load_project_config
    from src.core.modeling import ModelError
    from src.core.paths import CONFIG_DIR, DATA_DIR
    from src.core.services.agent import AgentService
    from src.core.services.scheduler import SchedulerService
    from src.core.services.script import ScriptService
    from src.core.services.script_registry import ExternalScriptRegistry
    from src.core.services.script_schedule import ScriptScheduleService
    from src.core.storage.tenants import TenantStoreError
    from src.core.storage.tool_audit import ToolAuditStore
    from src.core.storage.drive_audit import DriveAuditStore
    from src.core.tooling import ToolRuntime
    from src.core.plugins.registry import build_plugin_manager
    from src.core.plugins.base import PluginContext
    from src.core.services.notification import NotificationService
    from src.core.paths import PROJECT_ROOT, SYSTEM_DATA_DIR
    from src.api.app import create_app

    try:
        config = load_project_config(CONFIG_DIR)
    except ConfigError as exc:
        print("配置错误：{}".format(exc), file=sys.stderr)
        return 1

    try:
        services = build_core_services(config, DATA_DIR, strict_models=False)
    except TenantStoreError as exc:
        print("租户数据加载失败：{}".format(exc), file=sys.stderr)
        return 1
    except ModelError as exc:
        print("错误：{}".format(exc), file=sys.stderr)
        print("提示：将 DEEPSEEK_API_KEY=你的密钥 写入 data/system/model.env", file=sys.stderr)
        return 1
    for warning in services.model_warnings:
        print("警告：{}".format(warning), file=sys.stderr)

    model_router = services.model_router
    registry = services.tenant_registry
    conversation_store = services.conversation_store
    knowledge_service = services.knowledge_service
    model_analytics_store = services.model_analytics_store
    schedule_store = services.schedule_store
    recipient_store = services.recipient_store

    credentials = None
    try:
        from src.core.application.bot import load_credentials
        credentials = load_credentials()
    except Exception as exc:
        print(
            "警告：加载微信登录凭证失败，定时任务将无法推送微信消息：{}".format(exc),
            file=sys.stderr,
        )

    external_script_registry = ExternalScriptRegistry(
        SYSTEM_DATA_DIR / "script_registry.json",
        SYSTEM_DATA_DIR / "scripts.env",
    )
    script_service = ScriptService(
        config.scripts,
        credentials,
        recipient_store,
        PROJECT_ROOT,
        registry,
        external_registry=external_script_registry,
    )
    script_schedule_service = ScriptScheduleService(
        registry,
        script_service,
        config.app.timezone,
    )

    notification_service = NotificationService(
        credentials_loader=lambda: credentials,
        recipient_store=recipient_store,
    )
    plugin_context = PluginContext(
        project_root=PROJECT_ROOT,
        tenant_registry=registry,
        notification_service=notification_service,
        timezone=config.app.timezone,
        data_root=DATA_DIR / "plugins",
    )
    plugin_manager = build_plugin_manager(
        config.plugins if config.tools.enabled else {},
        context=plugin_context,
    )

    tool_audit_store = ToolAuditStore(registry)
    drive_audit_store = DriveAuditStore(registry)
    tool_states = _load_tool_states(DATA_DIR)

    mcp_manager = None
    if config.tools.enabled and config.mcp_servers:
        from src.core.tooling.mcp_client import McpClientManager

        mcp_manager = McpClientManager()
        mcp_manager.start()
        mcp_manager.reload(config.mcp_servers)

    from src.core.services.ocr import OcrService

    ocr_service = OcrService(config.tools.ocr) if config.tools.enabled else None
    tool_runtime = (
        ToolRuntime(
            config.tools,
            config.app.timezone,
            tenant_registry=registry,
            knowledge_service=knowledge_service,
            plugin_manager=plugin_manager,
            script_service=script_service,
            script_schedule_service=script_schedule_service,
            tool_audit_store=tool_audit_store,
            tool_states=tool_states,
            mcp_manager=mcp_manager,
            drive_service=services.drive_service,
            drive_audit_store=drive_audit_store,
            ocr_service=ocr_service,
        )
        if config.tools.enabled
        else None
    )

    agent_service = AgentService(
        model_router,
        config.app,
        config.agents,
        tool_runtime=tool_runtime,
        conversation_store=conversation_store,
        knowledge_service=knowledge_service,
        model_analytics_store=model_analytics_store,
        skills=config.skills,
        ocr_service=ocr_service,
    )
    if ocr_service is not None and config.tools.ocr.enabled:
        ocr_available, ocr_reason = ocr_service.availability()
        if not ocr_available:
            print("警告：OCR 工具不可用：{}".format(ocr_reason), file=sys.stderr)

    scheduler = SchedulerService(
        credentials=credentials,
        tasks=config.schedules,
        timezone_name=config.app.timezone,
        agent_service=agent_service,
        recipient_store=recipient_store,
        script_service=script_service,
        script_schedule_service=script_schedule_service,
        tenant_registry=registry,
        schedule_store=schedule_store,
        plugin_manager=plugin_manager,
        notification_service=notification_service,
    )
    plugin_manager.start()
    scheduler.start()

    admin_auth, admin_user_store, admin_role_store, initial_password = (
        _build_admin_auth(registry)
    )

    app = create_app(
        config, model_router, registry, conversation_store,
        tool_runtime=tool_runtime,
        knowledge_service=knowledge_service,
        drive_service=services.drive_service,
        drive_audit_store=drive_audit_store,
        plugin_context=plugin_context,
        plugin_manager=plugin_manager,
        scheduler=scheduler,
        tool_audit_store=tool_audit_store,
        model_analytics_store=model_analytics_store,
        admin_auth=admin_auth,
        admin_user_store=admin_user_store,
        admin_role_store=admin_role_store,
        script_service=script_service,
        script_registry=external_script_registry,
        script_schedule_service=script_schedule_service,
        secure_cookies=args.behind_https,
    )

    _print_panel_banner(args, initial_password)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BotPlatform Web 管理面板")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="监听端口 (默认 8080)")
    parser.add_argument(
        "--behind-https",
        action="store_true",
        help="部署在 HTTPS 反向代理之后时启用，会给会话 Cookie 加 Secure 标记",
    )
    parser.add_argument(
        "--panel-only",
        action="store_true",
        help="仅启动 Web 管理面板，不启动微信消息渠道",
    )
    args = parser.parse_args()

    _load_model_env()

    if args.panel_only:
        return _run_panel_only(args)
    return _run_combined(args)


if __name__ == "__main__":
    raise SystemExit(main())
