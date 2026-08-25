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
from src.core.modeling import (
    EmbeddingClient,
    ModelClient,
    ModelError,
    ModelRouter,
    RerankClient,
)
from src.core.modeling.factory import (
    create_embedding_client,
    create_model_client,
    create_rerank_client,
)
from src.core.services.knowledge import KnowledgeService
from src.core.services.drive import DriveService
from src.core.services.notification import TenantRecipientStore
from src.core.services.resources import ScopedResourceStore
from src.core.services.organization_controls import OrganizationControlStore
from src.core.services.credentials import CredentialService
from src.core.integrations.keychain import KeychainService
from src.core.storage.organizations import OrganizationStore
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
    rerank_client: Optional[RerankClient]
    knowledge_service: KnowledgeService
    drive_service: DriveService
    recipient_store: TenantRecipientStore
    schedule_store: ScheduleStore
    model_analytics_store: ModelAnalyticsStore
    project_config: Optional[ProjectConfig] = None
    model_warnings: List[str] = field(default_factory=list)
    organization_store: Optional[OrganizationStore] = None
    resource_store: Optional[ScopedResourceStore] = None
    organization_control_store: Optional[OrganizationControlStore] = None
    credential_service: Optional[CredentialService] = None

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        if self.embedding_client:
            self.embedding_client.close()
        if self.rerank_client:
            self.rerank_client.close()


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
    organization_store = OrganizationStore(tenant_registry)
    resource_store = ScopedResourceStore(organization_store, config)
    config = resource_store.build_project_config(config)
    organization_control_store = OrganizationControlStore(
        organization_store, resource_store, config
    )
    credential_service = CredentialService(
        organization_store,
        KeychainService(
            storage_path=data_dir / "system" / "organization_credentials.json"
        ),
        KeychainService(
            storage_path=data_dir / "system" / "integration_credentials.json"
        ),
    )
    model_analytics_store = ModelAnalyticsStore(tenant_registry, config)
    conversation_store = ConversationStore(
        tenant_registry, config.app.history_rounds * 2
    )
    recipient_store = TenantRecipientStore(tenant_registry)
    schedule_store = ScheduleStore(tenant_registry)
    warnings: List[str] = []

    def _build_role_client(binding: str, builder, kind: str):
        if not binding:
            return None
        profile = config.models.get(binding)
        if profile is None or not profile.enabled:
            return None
        try:
            return builder(profile)
        except Exception as exc:
            if strict_models:
                raise
            warnings.append("{} {} 初始化失败：{}".format(kind, binding, exc))
            return None

    embedding_client = _build_role_client(
        config.app.embedding_model, create_embedding_client, "向量模型"
    )
    rerank_client = _build_role_client(
        config.app.rerank_model, create_rerank_client, "重排模型"
    )
    knowledge_service = KnowledgeService(
        tenant_registry, embedding_client, rerank_client
    )
    knowledge_service.bootstrap_agent_bindings(
        agent.id for agent in config.agents.values() if agent.enabled
    )
    drive_service = DriveService(
        tenant_registry, data_dir / "public", knowledge_service=knowledge_service
    )

    clients: Dict[str, ModelClient] = {}

    def analytics_logger(*values) -> None:
        model_analytics_store.record_model_call(*values)
        if model_call_logger is not None:
            model_call_logger(*values)

    try:
        for profile_id, profile in config.models.items():
            if not profile.enabled or profile.modality != "chat":
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
        if rerank_client:
            rerank_client.close()
        raise

    return CoreServices(
        clients=clients,
        project_config=config,
        model_router=model_router,
        tenant_registry=tenant_registry,
        conversation_store=conversation_store,
        embedding_client=embedding_client,
        rerank_client=rerank_client,
        knowledge_service=knowledge_service,
        drive_service=drive_service,
        recipient_store=recipient_store,
        schedule_store=schedule_store,
        model_analytics_store=model_analytics_store,
        organization_store=organization_store,
        resource_store=resource_store,
        organization_control_store=organization_control_store,
        credential_service=credential_service,
        model_warnings=warnings,
    )
