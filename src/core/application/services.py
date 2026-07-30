"""Shared composition of core services for the bot and web entry points.

Both ``main.py`` (bot) and ``web.py`` (panel) need the same base graph:
tenant registry and stores, embedding + knowledge services, model clients,
and a configured ``ModelRouter``. This module builds that graph in one place
so the two entry points cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from src.core.config.loader import ProjectConfig
from src.core.integrations.embeddings import EmbeddingClient
from src.core.modeling import ModelClient, ModelError, ModelRouter
from src.core.modeling.factory import create_model_client
from src.core.services.knowledge import KnowledgeService
from src.core.services.drive import DriveService
from src.core.services.notification import TenantRecipientStore
from src.core.storage.tenants import (
    ConversationStore,
    ScheduleStore,
    TenantRegistry,
)
from src.core.storage.model_analytics import ModelAnalyticsStore


@dataclass
class CoreServices:
    """The service graph shared by every entry point."""

    clients: Dict[str, ModelClient]
    model_router: ModelRouter
    tenant_registry: TenantRegistry
    conversation_store: ConversationStore
    embedding_client: Optional[EmbeddingClient]
    knowledge_service: KnowledgeService
    drive_service: DriveService
    recipient_store: TenantRecipientStore
    schedule_store: ScheduleStore
    model_analytics_store: ModelAnalyticsStore
    model_warnings: List[str] = field(default_factory=list)

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        if self.embedding_client:
            self.embedding_client.close()


def build_core_services(
    config: ProjectConfig,
    data_dir: Path,
    model_call_logger=None,
    fallback_logger=None,
    strict_models: bool = True,
) -> CoreServices:
    """Assemble the shared service graph.

    With ``strict_models=True`` any enabled model profile that cannot be
    constructed aborts startup (bot behaviour). With ``strict_models=False``
    broken profiles are skipped with a recorded warning and the primary or
    fallback profile falls back to the first available client (web panel
    behaviour). Raises ``TenantStoreError`` or ``ModelError``/``ValueError``;
    every client created before a failure is closed again.
    """
    tenant_registry = TenantRegistry(data_dir)
    model_analytics_store = ModelAnalyticsStore(tenant_registry, config)
    conversation_store = ConversationStore(
        tenant_registry, config.app.history_rounds * 2
    )
    recipient_store = TenantRecipientStore(tenant_registry)
    schedule_store = ScheduleStore(tenant_registry)
    embedding_client = (
        EmbeddingClient(config.embedding) if config.embedding.enabled else None
    )
    knowledge_service = KnowledgeService(tenant_registry, embedding_client)
    knowledge_service.bootstrap_agent_bindings(
        agent.id for agent in config.agents.values() if agent.enabled
    )
    drive_service = DriveService(
        tenant_registry, data_dir / "public", knowledge_service=knowledge_service
    )

    clients: Dict[str, ModelClient] = {}
    warnings: List[str] = []

    def analytics_logger(*values) -> None:
        model_analytics_store.record_model_call(*values)
        if model_call_logger is not None:
            model_call_logger(*values)

    try:
        for profile_id, profile in config.models.items():
            if not profile.enabled:
                continue
            try:
                clients[profile_id] = create_model_client(
                    profile, logger=analytics_logger
                )
            except Exception as exc:
                if strict_models:
                    raise
                warnings.append("模型 {} 初始化失败：{}".format(profile_id, exc))
        if not clients:
            raise ModelError(
                "没有可用的模型档案，请检查 config/models.json 和 API Key 配置"
            )
        primary = config.app.active_model
        fallback = config.app.fallback_model
        if not strict_models:
            if primary not in clients:
                primary = next(iter(clients))
                warnings.append("主模型不可用，已切换到 {}".format(primary))
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
            fallback_logger=fallback_logger,
        )
    except Exception:
        for client in clients.values():
            client.close()
        if embedding_client:
            embedding_client.close()
        raise

    return CoreServices(
        clients=clients,
        model_router=model_router,
        tenant_registry=tenant_registry,
        conversation_store=conversation_store,
        embedding_client=embedding_client,
        knowledge_service=knowledge_service,
        drive_service=drive_service,
        recipient_store=recipient_store,
        schedule_store=schedule_store,
        model_analytics_store=model_analytics_store,
        model_warnings=warnings,
    )
