#!/usr/bin/env python3
"""Web management panel entry point."""

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


def main() -> int:
    parser = argparse.ArgumentParser(description="BotPlatform Web 管理面板")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="监听端口 (默认 8080)")
    args = parser.parse_args()

    _load_model_env()

    from src.core.config.loader import ConfigError, load_project_config
    from src.core.paths import CONFIG_DIR, DATA_DIR
    from src.core.modeling.factory import create_model_client
    from src.core.modeling.router import ModelRouter
    from src.core.storage.tenants import ConversationStore, TenantRegistry, ScheduleStore
    from src.core.integrations.embeddings import EmbeddingClient
    from src.core.services.knowledge import KnowledgeService
    from src.core.services.agent import AgentService
    from src.core.services.notification import TenantRecipientStore
    from src.core.services.scheduler import SchedulerService
    from src.core.storage.tool_audit import ToolAuditStore
    from src.core.tooling import ToolRuntime
    from src.core.plugins.registry import build_plugins
    from src.core.plugins.base import PluginContext
    from src.core.paths import PROJECT_ROOT
    from src.api.app import create_app
    from src.api.auth import TOKEN_FILE

    try:
        config = load_project_config(CONFIG_DIR)
    except ConfigError as exc:
        print("配置错误：{}".format(exc), file=sys.stderr)
        return 1

    clients = {}
    for profile_id, profile in config.models.items():
        if profile.enabled:
            try:
                clients[profile_id] = create_model_client(profile)
            except Exception as exc:
                print("警告：模型 {} 初始化失败：{}".format(profile_id, exc), file=sys.stderr)

    if not clients:
        print("错误：没有可用的模型档案，请检查 config/models.json 和 API Key 配置", file=sys.stderr)
        print("提示：将 DEEPSEEK_API_KEY=你的密钥 写入 data/system/model.env", file=sys.stderr)
        return 1

    primary = config.app.active_model
    fallback = config.app.fallback_model
    if primary not in clients:
        primary = next(iter(clients))
        print("提示：主模型不可用，已切换到 {}".format(primary), file=sys.stderr)
    if fallback not in clients:
        fallback = primary

    model_router = ModelRouter(
        clients,
        primary_profile_id=primary,
        fallback_profile_id=fallback,
        local_profile_id=config.app.local_model or None,
        flash_profile_id=config.app.flash_model or None,
        pro_profile_id=config.app.pro_model or None,
        vision_profile_id=config.app.vision_model or None,
        cooldown_seconds=config.app.fallback_cooldown_seconds,
    )

    registry = TenantRegistry(DATA_DIR)
    conversation_store = ConversationStore(registry, max_messages=config.app.history_rounds * 2)

    embedding_client = (
        EmbeddingClient(config.embedding)
        if config.embedding.enabled
        else None
    )
    knowledge_service = KnowledgeService(registry, embedding_client)

    plugin_context = PluginContext(
        project_root=PROJECT_ROOT,
        tenant_registry=registry,
    )
    platform_plugins = (
        build_plugins(config.plugins, context=plugin_context)
        if config.tools.enabled
        else []
    )

    tool_audit_store = ToolAuditStore(registry)

    tool_states = {}
    tool_state_path = DATA_DIR / "tool_state.json"
    if tool_state_path.exists():
        try:
            import json as _json
            tool_states = _json.loads(tool_state_path.read_text(encoding="utf-8")).get("tools", {})
        except Exception:
            tool_states = {}

    mcp_manager = None
    if config.tools.enabled and config.mcp_servers:
        from src.core.tooling.mcp_client import McpClientManager

        mcp_manager = McpClientManager()
        mcp_manager.start()
        mcp_manager.reload(config.mcp_servers)

    tool_runtime = (
        ToolRuntime(
            config.tools,
            config.app.timezone,
            tenant_registry=registry,
            knowledge_service=knowledge_service,
            plugins=platform_plugins,
            tool_audit_store=tool_audit_store,
            tool_states=tool_states,
            mcp_manager=mcp_manager,
        )
        if config.tools.enabled
        else None
    )

    schedule_store = ScheduleStore(registry)
    recipient_store = TenantRecipientStore(registry)
    agent_service = AgentService(
        model_router,
        config.app,
        config.agents,
        tool_runtime=tool_runtime,
        conversation_store=conversation_store,
        knowledge_service=knowledge_service,
        skills=config.skills,
    )

    credentials = None
    try:
        from src.core.application.bot import load_credentials
        credentials = load_credentials()
    except Exception:
        credentials = None

    scheduler = SchedulerService(
        credentials=credentials,
        tasks=config.schedules,
        timezone_name=config.app.timezone,
        agent_service=agent_service,
        recipient_store=recipient_store,
        tenant_registry=registry,
        schedule_store=schedule_store,
        plugins=platform_plugins,
    )
    scheduler.start()

    app = create_app(
        config, model_router, registry, conversation_store,
        tool_runtime=tool_runtime,
        knowledge_service=knowledge_service,
        plugin_context=plugin_context,
        scheduler=scheduler,
        tool_audit_store=tool_audit_store,
    )

    token = app.state.web_token
    print("Web 管理面板已启动：http://{}:{}".format(args.host, args.port))
    print("访问令牌：{}（已保存到 {}）".format(token, TOKEN_FILE))
    print("浏览器打开：http://{}:{}?token={}".format(args.host, args.port, token))

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
