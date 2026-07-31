"""Write-only organization and member credential metadata."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.core.integrations.keychain import (
    KeychainError,
    KeychainReference,
    KeychainService,
)
from src.core.storage.organizations import OrganizationStore


CREDENTIAL_ID = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,127}$")
CREDENTIAL_RESOURCE_TYPES = {
    "models",
    "mcp",
    "plugins",
    "channels",
    "integrations",
}


class CredentialError(ValueError):
    """Raised for invalid credential metadata or access."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CredentialService:
    """Store secret values outside SQLite and never return plaintext."""

    def __init__(
        self,
        organizations: OrganizationStore,
        keychain: Optional[KeychainService] = None,
        legacy_integration_keychain: Optional[KeychainService] = None,
    ) -> None:
        self.organizations = organizations
        self.database = organizations.database
        self.keychain = keychain or KeychainService()
        self.legacy_integration_keychain = legacy_integration_keychain

    @staticmethod
    def _row(row: Any, configured: bool) -> Dict[str, Any]:
        return {
            "credential_id": str(row["credential_id"]),
            "organization_id": str(row["organization_id"]),
            "user_id": (
                int(row["user_id"]) if row["user_id"] is not None else None
            ),
            "scope": str(row["credential_scope"]),
            "resource_type": str(row["resource_type"]),
            "resource_id": str(row["resource_id"]),
            "label": str(row["label"]),
            "configured": configured,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _reference(
        self, organization_id: str, credential_id: str
    ) -> KeychainReference:
        return KeychainReference(
            "com.botplatform.organization.{}.{}".format(
                organization_id, credential_id
            ),
            "credential",
        )

    def list_for_user(
        self, organization_id: str, user_id: int
    ) -> List[Dict[str, Any]]:
        self.organizations.membership(user_id, organization_id)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM credential_metadata WHERE organization_id=? "
                "AND (credential_scope='organization' OR user_id=?) "
                "ORDER BY credential_scope, resource_type, resource_id",
                (organization_id, user_id),
            ).fetchall()
        result = []
        for row in rows:
            reference = KeychainReference(
                str(row["secret_service"]), str(row["secret_account"])
            )
            result.append(self._row(row, self.keychain.exists(reference)))
        return result

    def put(
        self,
        organization_id: str,
        credential_id: str,
        *,
        actor_user_id: int,
        scope: str,
        resource_type: str,
        resource_id: str,
        label: str,
        secret: str,
    ) -> Dict[str, Any]:
        membership = self.organizations.membership(
            actor_user_id, organization_id
        )
        if not CREDENTIAL_ID.fullmatch(credential_id):
            raise CredentialError("凭据编号格式无效")
        if scope not in {"organization", "personal"}:
            raise CredentialError("凭据范围无效")
        if scope == "organization" and membership["role"] not in {
            "owner",
            "admin",
        }:
            raise CredentialError("只有 Owner 或 Admin 可以管理组织凭据")
        if resource_type not in CREDENTIAL_RESOURCE_TYPES:
            raise CredentialError("凭据资源类型无效")
        if not CREDENTIAL_ID.fullmatch(resource_id):
            raise CredentialError("关联资源编号格式无效")
        if not isinstance(secret, str) or not secret:
            raise CredentialError("凭据内容不能为空")
        with self.database.read() as connection:
            existing = connection.execute(
                "SELECT organization_id, user_id, credential_scope "
                "FROM credential_metadata WHERE organization_id=? "
                "AND credential_id=?",
                (organization_id, credential_id),
            ).fetchone()
        if existing is not None:
            if (
                existing["credential_scope"] == "personal"
                and int(existing["user_id"]) != actor_user_id
            ):
                raise CredentialError("凭据不存在")
        reference = self._reference(organization_id, credential_id)
        try:
            self.keychain.set_secret(reference, secret)
        except KeychainError as exc:
            raise CredentialError("凭据保存失败") from exc
        timestamp = _now()
        owner_user_id = actor_user_id if scope == "personal" else None
        try:
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO credential_metadata("
                    "credential_id, organization_id, user_id, credential_scope, "
                    "resource_type, resource_id, label, secret_service, "
                    "secret_account, created_by, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(organization_id, credential_id) DO UPDATE SET "
                    "user_id=excluded.user_id, credential_scope=excluded.credential_scope, "
                    "resource_type=excluded.resource_type, "
                    "resource_id=excluded.resource_id, label=excluded.label, "
                    "secret_service=excluded.secret_service, "
                    "secret_account=excluded.secret_account, "
                    "updated_at=excluded.updated_at "
                    "WHERE credential_metadata.organization_id=excluded.organization_id",
                    (
                        credential_id,
                        organization_id,
                        owner_user_id,
                        scope,
                        resource_type,
                        resource_id,
                        label.strip()[:100],
                        reference.service,
                        reference.account,
                        actor_user_id,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM credential_metadata "
                    "WHERE credential_id=? AND organization_id=?",
                    (credential_id, organization_id),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            self.keychain.delete_secret(reference)
            raise CredentialError("同一资源已经配置凭据") from exc
        except Exception:
            self.keychain.delete_secret(reference)
            raise
        if row is None:
            self.keychain.delete_secret(reference)
            raise CredentialError("凭据保存失败")
        return self._row(row, True)

    def delete(
        self,
        organization_id: str,
        credential_id: str,
        actor_user_id: int,
    ) -> None:
        membership = self.organizations.membership(
            actor_user_id, organization_id
        )
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM credential_metadata "
                "WHERE credential_id=? AND organization_id=?",
                (credential_id, organization_id),
            ).fetchone()
        if row is None:
            raise CredentialError("凭据不存在")
        if row["credential_scope"] == "personal":
            if int(row["user_id"]) != actor_user_id:
                raise CredentialError("凭据不存在")
        elif membership["role"] not in {"owner", "admin"}:
            raise CredentialError("只有 Owner 或 Admin 可以删除组织凭据")
        reference = KeychainReference(
            str(row["secret_service"]), str(row["secret_account"])
        )
        try:
            self.keychain.delete_secret(reference)
        except KeychainError as exc:
            raise CredentialError("凭据删除失败") from exc
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM credential_metadata "
                "WHERE credential_id=? AND organization_id=?",
                (credential_id, organization_id),
            )

    def _delete_rows(self, rows: List[Any]) -> None:
        for row in rows:
            try:
                self.keychain.delete_secret(
                    KeychainReference(
                        str(row["secret_service"]),
                        str(row["secret_account"]),
                    )
                )
            except KeychainError as exc:
                raise CredentialError("凭据删除失败") from exc
        with self.database.transaction(immediate=True) as connection:
            connection.executemany(
                "DELETE FROM credential_metadata "
                "WHERE organization_id=? AND credential_id=?",
                [
                    (
                        str(row["organization_id"]),
                        str(row["credential_id"]),
                    )
                    for row in rows
                ],
            )

    def _delete_legacy_integrations(self, tenant_ids: List[str]) -> None:
        if self.legacy_integration_keychain is None or not tenant_ids:
            return
        placeholders = ",".join("?" for _ in tenant_ids)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT tenant_id, integration_id FROM integrations "
                "WHERE tenant_id IN ({})".format(placeholders),
                tenant_ids,
            ).fetchall()
        for row in rows:
            try:
                reference = self.legacy_integration_keychain.reference(
                    str(row["tenant_id"]), str(row["integration_id"])
                )
                self.legacy_integration_keychain.delete_secret(reference)
            except KeychainError as exc:
                raise CredentialError("历史集成凭据删除失败") from exc

    def delete_personal(
        self, organization_id: str, user_id: int
    ) -> None:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT organization_id, credential_id, secret_service, "
                "secret_account "
                "FROM credential_metadata WHERE organization_id=? "
                "AND credential_scope='personal' AND user_id=?",
                (organization_id, user_id),
            ).fetchall()
        self._delete_rows(list(rows))
        with self.database.read() as connection:
            personal = connection.execute(
                "SELECT tenant_id FROM tenants WHERE bot_id=? AND user_id=? "
                "AND deleting=0",
                (
                    "member-personal:{}".format(organization_id),
                    str(user_id),
                ),
            ).fetchone()
        if personal is not None:
            self._delete_legacy_integrations([str(personal["tenant_id"])])

    def delete_all(self, organization_id: str) -> None:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT organization_id, credential_id, secret_service, "
                "secret_account "
                "FROM credential_metadata WHERE organization_id=?",
                (organization_id,),
            ).fetchall()
        self._delete_rows(list(rows))
        with self.database.read() as connection:
            personal_rows = connection.execute(
                "SELECT tenant_id FROM tenants WHERE bot_id=? AND deleting=0",
                ("member-personal:{}".format(organization_id),),
            ).fetchall()
        self._delete_legacy_integrations(
            [organization_id]
            + [str(row["tenant_id"]) for row in personal_rows]
        )
