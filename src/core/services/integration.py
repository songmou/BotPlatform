"""Interactive per-tenant external-integration credential setup."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from src.core.integrations.keychain import KeychainError, KeychainReference, KeychainService
from src.core.storage.tenants import IntegrationStore, TenantContext


SUPPORTED_INTEGRATIONS = {
    "ctsehr": "CTS EHR",
    "ctsoa": "CTS OA",
    "autogen": "悟空 AI",
}


@dataclass
class PendingIntegrationSetup:
    integration_id: str
    step: str
    expires_at: datetime
    account: str = ""


class IntegrationService:
    def __init__(
        self,
        store: IntegrationStore,
        keychain: Optional[KeychainService] = None,
        ttl_seconds: int = 300,
    ) -> None:
        self.store = store
        self.keychain = keychain or KeychainService()
        self.ttl_seconds = ttl_seconds
        self._pending: Dict[str, PendingIntegrationSetup] = {}
        self._lock = threading.RLock()

    def _stored_reference(
        self, tenant: TenantContext, integration_id: str
    ) -> KeychainReference:
        metadata = self.store.get(tenant.tenant_id, integration_id) or {}
        service = str(metadata.get("keychain_service", ""))
        account = str(metadata.get("keychain_account", "credential"))
        if service:
            return KeychainReference(service, account)
        return self.keychain.reference(tenant.tenant_id, integration_id)

    def setup(self, tenant: TenantContext, integration_id: str) -> str:
        integration_id = integration_id.strip().lower()
        if integration_id not in SUPPORTED_INTEGRATIONS:
            raise ValueError(
                "仅支持以下集成：{}".format("、".join(sorted(SUPPORTED_INTEGRATIONS)))
            )
        with self._lock:
            self._pending[tenant.tenant_id] = PendingIntegrationSetup(
                integration_id=integration_id,
                step="account",
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds),
            )
        return "正在配置 {}。请直接回复登录账号；回复“取消”可终止。".format(
            SUPPORTED_INTEGRATIONS[integration_id]
        )

    def has_pending(self, tenant: TenantContext) -> bool:
        with self._lock:
            pending = self._pending.get(tenant.tenant_id)
            if pending and datetime.now(timezone.utc) >= pending.expires_at:
                self._pending.pop(tenant.tenant_id, None)
                return False
            return pending is not None

    def consume(self, tenant: TenantContext, text: str) -> Tuple[bool, str]:
        """Consume setup input. These messages must bypass logs and transcripts."""
        with self._lock:
            pending = self._pending.get(tenant.tenant_id)
            if pending is None:
                return False, ""
            if datetime.now(timezone.utc) >= pending.expires_at:
                self._pending.pop(tenant.tenant_id, None)
                return True, "集成配置已超时，未保存任何凭据。"
            value = text.strip()
            if value in {"取消", "/cancel"}:
                self._pending.pop(tenant.tenant_id, None)
                return True, "已取消集成配置。"
            if not value:
                return True, "输入不能为空；请重新回复，或回复“取消”。"
            if pending.step == "account":
                if len(value) > 200:
                    return True, "账号过长，请重新输入。"
                pending.account = value
                pending.step = "secret"
                return True, "请回复密码。密码仅写入受限权限凭证文件，不会进入日志、聊天历史或模型。"
            reference = self.keychain.reference(
                tenant.tenant_id, pending.integration_id
            )
            try:
                self.keychain.set_secret(reference, value)
            except KeychainError:
                self._pending.pop(tenant.tenant_id, None)
                return True, "凭证文件写入失败，未保存集成配置。"
            self.store.set(
                tenant.tenant_id,
                pending.integration_id,
                {
                    "account": pending.account,
                    "keychain_service": reference.service,
                    "keychain_account": reference.account,
                    "configured_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            name = SUPPORTED_INTEGRATIONS[pending.integration_id]
            self._pending.pop(tenant.tenant_id, None)
            return True, "{} 凭据已安全保存。".format(name)

    def status(self, tenant: TenantContext, integration_id: str = "") -> str:
        ids = [integration_id.lower()] if integration_id else sorted(SUPPORTED_INTEGRATIONS)
        lines = []
        for item in ids:
            if item not in SUPPORTED_INTEGRATIONS:
                raise ValueError("未知集成：{}".format(item))
            metadata = self.store.get(tenant.tenant_id, item)
            configured = False
            if metadata:
                reference = self._stored_reference(tenant, item)
                configured = self.keychain.exists(reference)
            lines.append(
                "- {}（{}）：{}".format(
                    SUPPORTED_INTEGRATIONS[item],
                    item,
                    "已配置" if configured else "未配置",
                )
            )
        return "集成状态：\n" + "\n".join(lines)

    def delete(self, tenant: TenantContext, integration_id: str) -> str:
        integration_id = integration_id.lower()
        if integration_id not in SUPPORTED_INTEGRATIONS:
            raise ValueError("未知集成：{}".format(integration_id))
        reference = self._stored_reference(tenant, integration_id)
        try:
            self.keychain.delete_secret(reference)
        except KeychainError as exc:
            raise ValueError("无法删除集成凭据") from exc
        self.store.delete(tenant.tenant_id, integration_id)
        with self._lock:
            pending = self._pending.get(tenant.tenant_id)
            if pending and pending.integration_id == integration_id:
                self._pending.pop(tenant.tenant_id, None)
        return "已删除 {} 的账号配置和凭据。".format(
            SUPPORTED_INTEGRATIONS[integration_id]
        )

    def delete_all(self, tenant: TenantContext) -> None:
        for integration_id in SUPPORTED_INTEGRATIONS:
            try:
                self.keychain.delete_secret(
                    self._stored_reference(tenant, integration_id)
                )
            except KeychainError:
                pass
        with self._lock:
            self._pending.pop(tenant.tenant_id, None)
