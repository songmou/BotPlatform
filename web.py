#!/usr/bin/env python3
"""Web management panel entry point."""

from __future__ import annotations

import argparse
import sys


def _load_model_env() -> None:
    """Load API keys from data/system/model.env if present."""
    import os
    from src.paths import SYSTEM_DATA_DIR

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

    from src.config.loader import ConfigError, load_project_config
    from src.paths import CONFIG_DIR, DATA_DIR
    from src.modeling.factory import create_model_client
    from src.modeling.router import ModelRouter
    from src.storage.tenants import ConversationStore, TenantRegistry
    from src.integrations.embeddings import EmbeddingClient
    from src.services.knowledge import KnowledgeService
    from src.tooling import ToolRuntime
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

    tool_runtime = (
        ToolRuntime(
            config.tools,
            config.app.timezone,
            tenant_registry=registry,
            knowledge_service=knowledge_service,
        )
        if config.tools.enabled
        else None
    )

    app = create_app(
        config, model_router, registry, conversation_store,
        tool_runtime=tool_runtime,
        knowledge_service=knowledge_service,
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
