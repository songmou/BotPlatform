"""Organization, membership, invitation, and legacy-tenant migration storage."""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.core.storage.admin_users import (
    AdminRoleStore,
    AdminUserStore,
    verify_password,
)
from src.core.storage.tenants import TenantRegistry, TenantStoreError


ORGANIZATION_ROLES = {"owner", "admin", "member"}


class OrganizationError(ValueError):
    """Raised when an organization operation is invalid or unauthorized."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class OrganizationStore:
    """Persist organizations while retaining existing tenant UUIDs."""

    def __init__(self, registry: TenantRegistry) -> None:
        self.registry = registry
        self.database = registry.database
        registry.organization_store = self
        self.cleanup_channel_personal_spaces()
        self.bootstrap_legacy_organizations()

    def cleanup_channel_personal_spaces(self) -> None:
        """Remove obsolete private tenants created for organization channels."""
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT tenant_id FROM tenants WHERE deleting=0 "
                "AND bot_id LIKE 'organization-channel:%' ORDER BY created_at"
            ).fetchall()
        if not isinstance(rows, (list, tuple)) or not rows:
            return

        from src.core.integrations.keychain import (
            KeychainError,
            KeychainReference,
            KeychainService,
        )

        keychain = KeychainService(
            storage_path=self.registry.system_root / "integration_credentials.json"
        )
        for row in rows:
            tenant_id = str(row["tenant_id"])
            with self.database.read() as connection:
                integrations = connection.execute(
                    "SELECT integration_id, metadata_json FROM integrations "
                    "WHERE tenant_id=?",
                    (tenant_id,),
                ).fetchall()
            for integration in integrations:
                try:
                    metadata = json.loads(str(integration["metadata_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                fallback = keychain.reference(
                    tenant_id, str(integration["integration_id"])
                )
                reference = KeychainReference(
                    str(metadata.get("keychain_service") or fallback.service),
                    str(metadata.get("keychain_account") or fallback.account),
                )
                try:
                    keychain.delete_secret(reference)
                except KeychainError as exc:
                    raise OrganizationError(
                        "清理旧渠道个人空间凭据失败"
                    ) from exc

            with self.database.transaction(immediate=True) as connection:
                identities = connection.execute(
                    "SELECT identity.identity_id, identity.active_organization_id "
                    "FROM channel_identities identity "
                    "JOIN organization_channels channel "
                    "ON channel.channel_instance_id=identity.channel_id "
                    "AND channel.organization_id=identity.active_organization_id "
                    "WHERE identity.tenant_id=?",
                    (tenant_id,),
                ).fetchall()
                for identity in identities:
                    identity_id = str(identity["identity_id"])
                    organization_id = str(identity["active_organization_id"])
                    connection.execute(
                        "UPDATE delivery_endpoints SET tenant_id=? "
                        "WHERE identity_id=?",
                        (organization_id, identity_id),
                    )
                    connection.execute(
                        "UPDATE channel_identities SET tenant_id=? "
                        "WHERE identity_id=?",
                        (organization_id, identity_id),
                    )
            self.registry.delete(self.registry.get(tenant_id))

    def bootstrap_legacy_organizations(self) -> None:
        """Turn every pre-v24 tenant into an unclaimed single-owner organization."""
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT tenant_id, user_id, created_at FROM tenants "
                "WHERE deleting=0 AND bot_id NOT LIKE 'member-personal:%' "
                "AND bot_id NOT LIKE 'organization-channel:%' "
                "ORDER BY created_at"
            ).fetchall()
            for row in rows:
                self._ensure_legacy_row(connection, row, timestamp)

    @staticmethod
    def _ensure_legacy_row(connection, row: Any, timestamp: str) -> None:
        organization_id = str(row["tenant_id"])
        legacy_subject = str(row["user_id"])
        name = "个人空间 {}".format(legacy_subject[:24])
        connection.execute(
            "INSERT OR IGNORE INTO organizations("
            "organization_id, name, status, legacy, created_at, updated_at"
            ") VALUES (?, ?, 'active', 1, ?, ?)",
            (
                organization_id,
                name,
                str(row["created_at"]),
                timestamp,
            ),
        )
        existing_owner = connection.execute(
            "SELECT 1 FROM organization_memberships "
            "WHERE organization_id=? AND role='owner'",
            (organization_id,),
        ).fetchone()
        if existing_owner is None:
            connection.execute(
                "INSERT INTO organization_memberships("
                "membership_id, organization_id, user_id, legacy_subject_id, "
                "role, status, created_at, updated_at"
                ") VALUES (?, ?, NULL, ?, 'owner', 'invited', ?, ?)",
                (
                    str(uuid.uuid4()),
                    organization_id,
                    legacy_subject,
                    timestamp,
                    timestamp,
                ),
            )

    def ensure_legacy_organization(self, organization_id: str) -> None:
        """Register one newly created channel tenant without waiting for restart."""
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT tenant_id, user_id, created_at FROM tenants "
                "WHERE tenant_id=? AND deleting=0 "
                "AND bot_id NOT LIKE 'member-personal:%' "
                "AND bot_id NOT LIKE 'organization-channel:%'",
                (organization_id,),
            ).fetchone()
            if row is not None:
                self._ensure_legacy_row(connection, row, _now())

    def sync_users(self, users: AdminUserStore) -> None:
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            for user in users.list_users():
                connection.execute(
                    "INSERT INTO users(user_id, display_name, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                    "display_name=excluded.display_name, updated_at=excluded.updated_at",
                    (
                        user.user_id,
                        user.username,
                        user.created_at,
                        timestamp,
                    ),
                )

    def ensure_user(self, user_id: int, username: str = "") -> None:
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO users("
                "user_id, display_name, created_at, updated_at"
                ") VALUES (?, ?, ?, ?)",
                (user_id, username, timestamp, timestamp),
            )

    def list_organizations(self) -> List[Dict[str, Any]]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT o.organization_id, o.name, o.status, o.legacy, "
                "o.created_at, o.updated_at, COUNT(m.membership_id) AS member_count "
                "FROM organizations o LEFT JOIN organization_memberships m "
                "ON m.organization_id=o.organization_id AND m.status='active' "
                "GROUP BY o.organization_id ORDER BY o.created_at"
            ).fetchall()
            claimed_ids = {
                str(row["organization_id"])
                for row in connection.execute(
                    "SELECT DISTINCT organization_id FROM organization_memberships "
                    "WHERE role='owner' AND user_id IS NOT NULL AND status='active'"
                ).fetchall()
            }
        return [
            {
                **dict(row),
                "legacy": bool(row["legacy"]),
                "member_count": int(row["member_count"] or 0),
                "legacy_claimed": (
                    str(row["organization_id"]) in claimed_ids
                    if bool(row["legacy"])
                    else None
                ),
            }
            for row in rows
        ]

    def get(self, organization_id: str) -> Dict[str, Any]:
        try:
            self.registry.get(organization_id)
        except TenantStoreError as exc:
            raise OrganizationError("组织不存在") from exc
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT organization_id, name, status, legacy, created_at, updated_at "
                "FROM organizations WHERE organization_id=?",
                (organization_id,),
            ).fetchone()
        if row is None:
            raise OrganizationError("组织不存在")
        result = dict(row)
        result["legacy"] = bool(result["legacy"])
        return result

    def create(
        self,
        name: str,
        created_by: int,
        owner_role: str = "owner",
        invitation_hours: int = 72,
    ) -> Tuple[Dict[str, Any], str]:
        normalized = name.strip()
        if not normalized or len(normalized) > 100:
            raise OrganizationError("组织名称长度必须为 1 到 100 个字符")
        if owner_role not in ORGANIZATION_ROLES:
            raise OrganizationError("组织角色无效")
        organization_id = str(uuid.uuid4())
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO tenants(tenant_id, bot_id, user_id, created_at) "
                "VALUES (?, 'organization', ?, ?)",
                (organization_id, "organization:" + organization_id, timestamp),
            )
            connection.execute(
                "INSERT INTO organizations("
                "organization_id, name, status, legacy, created_at, updated_at"
                ") VALUES (?, ?, 'active', 0, ?, ?)",
                (organization_id, normalized, timestamp, timestamp),
            )
        token = self.create_invitation(
            organization_id,
            owner_role,
            created_by,
            lifetime_hours=invitation_hours,
        )
        return self.get(organization_id), token

    def update(self, organization_id: str, name: str) -> Dict[str, Any]:
        self.get(organization_id)
        normalized = name.strip()
        if not normalized or len(normalized) > 100:
            raise OrganizationError("组织名称长度必须为 1 到 100 个字符")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE organizations SET name=?, updated_at=? "
                "WHERE organization_id=?",
                (normalized, _now(), organization_id),
            )
        return self.get(organization_id)

    def set_status(
        self, organization_id: str, status: str
    ) -> Dict[str, Any]:
        if status not in {"active", "suspended"}:
            raise OrganizationError("组织状态无效")
        self.get(organization_id)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE organizations SET status=?, updated_at=? "
                "WHERE organization_id=?",
                (status, _now(), organization_id),
            )
        return self.get(organization_id)

    def backup_organization(self, organization_id: str) -> Path:
        """Create a recoverable snapshot without changing live organization data."""
        organization = self.get(organization_id)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_root = (
            self.registry.system_root
            / "organization_backups"
            / "{}-{}".format(organization_id, stamp)
        )
        backup_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        import sqlite3

        source = self.database.connect()
        target = sqlite3.connect(str(backup_root / "botplatform.sqlite3"))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        shared_root = self.registry.tenant_root(organization_id)
        if shared_root.exists():
            shutil.copytree(
                shared_root,
                backup_root / "organization",
                dirs_exist_ok=True,
            )
        with self.database.read() as connection:
            personal_rows = connection.execute(
                "SELECT tenant_id FROM tenants WHERE bot_id=? AND deleting=0",
                ("member-personal:{}".format(organization_id),),
            ).fetchall()
        for row in personal_rows:
            personal_id = str(row["tenant_id"])
            personal_root = self.registry.tenant_root(personal_id)
            if personal_root.exists():
                shutil.copytree(
                    personal_root,
                    backup_root / "members" / personal_id,
                    dirs_exist_ok=True,
                )
        (backup_root / "manifest.json").write_text(
            json.dumps(
                {
                    "organization_id": organization_id,
                    "organization_name": organization["name"],
                    "created_at": _now(),
                    "member_personal_tenants": [
                        str(row["tenant_id"]) for row in personal_rows
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (backup_root / "botplatform.sqlite3").chmod(0o600)
        (backup_root / "manifest.json").chmod(0o600)
        return backup_root

    def delete_after_backup(self, organization_id: str) -> None:
        """Delete organization data after the caller has completed its backup."""
        self.get(organization_id)
        with self.database.read() as connection:
            personal_rows = connection.execute(
                "SELECT tenant_id FROM tenants WHERE bot_id=? AND deleting=0",
                ("member-personal:{}".format(organization_id),),
            ).fetchall()
        for row in personal_rows:
            self.registry.delete(self.registry.get(str(row["tenant_id"])))
        self.registry.delete(self.registry.get(organization_id))

    def create_invitation(
        self,
        organization_id: str,
        role: str,
        created_by: Optional[int],
        lifetime_hours: int = 72,
    ) -> str:
        self.get(organization_id)
        if role not in ORGANIZATION_ROLES:
            raise OrganizationError("组织角色无效")
        token = secrets.token_urlsafe(32)
        timestamp = datetime.now(timezone.utc)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO organization_invitations("
                "invitation_id, organization_id, token_hash, role, created_by, "
                "created_at, expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    organization_id,
                    _token_hash(token),
                    role,
                    created_by,
                    timestamp.isoformat(),
                    (
                        timestamp
                        + timedelta(hours=max(1, min(lifetime_hours, 168)))
                    ).isoformat(),
                ),
            )
        return token

    def issue_legacy_claim(
        self, organization_id: str, lifetime_minutes: int = 30
    ) -> str:
        self.get(organization_id)
        with self.database.read() as connection:
            owner = connection.execute(
                "SELECT membership_id FROM organization_memberships "
                "WHERE organization_id=? AND role='owner' AND user_id IS NULL",
                (organization_id,),
            ).fetchone()
        if owner is None:
            raise OrganizationError("该组织已经完成认领")
        return self.create_invitation(
            organization_id,
            "owner",
            None,
            lifetime_hours=max(1, (lifetime_minutes + 59) // 60),
        )

    def accept_invitation(
        self,
        token: str,
        username: str,
        password: str,
        users: AdminUserStore,
        roles: AdminRoleStore,
    ) -> Dict[str, Any]:
        if not token or len(password) < 8:
            raise OrganizationError("密码至少需要 8 个字符")
        normalized_username = username.strip()
        if len(normalized_username) < 3:
            raise OrganizationError("用户名至少需要 3 个字符")
        timestamp = _now()
        with self.database.read() as connection:
            invitation = connection.execute(
                "SELECT invitation_id, organization_id, role, expires_at, accepted_at "
                "FROM organization_invitations WHERE token_hash=?",
                (_token_hash(token),),
            ).fetchone()
        if invitation is None or invitation["accepted_at"] is not None:
            raise OrganizationError("邀请无效或已经使用")
        try:
            expires_at = datetime.fromisoformat(str(invitation["expires_at"]))
        except ValueError as exc:
            raise OrganizationError("邀请有效期无效") from exc
        if expires_at <= datetime.now(timezone.utc):
            raise OrganizationError("邀请已经过期")

        user = users.get_by_username(normalized_username)
        if user is None:
            tenant_role = roles.get_by_code("tenant_user")
            user = users.create(normalized_username, password, tenant_role.role_id)
        elif user.disabled:
            raise OrganizationError("账号已被禁用")
        elif not verify_password(password, users.password_hash(user.user_id)):
            raise OrganizationError("用户名或密码错误")
        self.ensure_user(user.user_id, user.username)

        organization_id = str(invitation["organization_id"])
        role = str(invitation["role"])
        with self.database.transaction(immediate=True) as connection:
            current_invitation = connection.execute(
                "SELECT accepted_at, expires_at FROM organization_invitations "
                "WHERE invitation_id=?",
                (str(invitation["invitation_id"]),),
            ).fetchone()
            if (
                current_invitation is None
                or current_invitation["accepted_at"] is not None
            ):
                raise OrganizationError("邀请无效或已经使用")
            existing = connection.execute(
                "SELECT membership_id, user_id FROM organization_memberships "
                "WHERE organization_id=? AND role='owner' ORDER BY created_at LIMIT 1",
                (organization_id,),
            ).fetchone()
            if role == "owner":
                if existing is not None and existing["user_id"] is None:
                    connection.execute(
                        "UPDATE organization_memberships SET user_id=?, "
                        "status='active', updated_at=? WHERE membership_id=?",
                        (
                            user.user_id,
                            timestamp,
                            str(existing["membership_id"]),
                        ),
                    )
                elif existing is not None:
                    raise OrganizationError("该组织已经存在所有者")
                else:
                    connection.execute(
                        "INSERT INTO organization_memberships("
                        "membership_id, organization_id, user_id, role, status, "
                        "created_at, updated_at"
                        ") VALUES (?, ?, ?, 'owner', 'active', ?, ?)",
                        (
                            str(uuid.uuid4()),
                            organization_id,
                            user.user_id,
                            timestamp,
                            timestamp,
                        ),
                    )
            else:
                connection.execute(
                    "INSERT INTO organization_memberships("
                    "membership_id, organization_id, user_id, role, status, "
                    "created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, 'active', ?, ?) "
                    "ON CONFLICT(organization_id, user_id) DO UPDATE SET "
                    "role=excluded.role, status='active', updated_at=excluded.updated_at",
                    (
                        str(uuid.uuid4()),
                        organization_id,
                        user.user_id,
                        role,
                        timestamp,
                        timestamp,
                    ),
                )
            connection.execute(
                "UPDATE organization_invitations SET accepted_at=?, accepted_by=? "
                "WHERE invitation_id=? AND accepted_at IS NULL",
                (timestamp, user.user_id, str(invitation["invitation_id"])),
            )
            connection.execute(
                "UPDATE channel_identities SET user_id=?, "
                "active_organization_id=? WHERE tenant_id=?",
                (user.user_id, organization_id, organization_id),
            )
        return {
            "organization": self.get(organization_id),
            "membership": self.membership(user.user_id, organization_id),
            "username": user.username,
        }

    def list_for_user(self, user_id: int) -> List[Dict[str, Any]]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT o.organization_id, o.name, o.status, o.legacy, "
                "m.membership_id, m.role, m.status AS membership_status "
                "FROM organization_memberships m JOIN organizations o "
                "ON o.organization_id=m.organization_id "
                "WHERE m.user_id=? AND m.status='active' "
                "ORDER BY o.name COLLATE NOCASE",
                (user_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "legacy": bool(row["legacy"]),
            }
            for row in rows
        ]

    def ensure_debug_organization(
        self, user_id: int, username: str
    ) -> str:
        """Return an active organization, creating a private debug one if needed."""
        self.ensure_user(user_id, username)
        choices = self.list_for_user(user_id)
        if choices:
            return str(choices[0]["organization_id"])
        organization_id = str(uuid.uuid4())
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO tenants(tenant_id, bot_id, user_id, created_at) "
                "VALUES (?, 'web-organization', ?, ?)",
                (
                    organization_id,
                    "web-user:{}".format(user_id),
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO organizations("
                "organization_id, name, status, legacy, created_at, updated_at"
                ") VALUES (?, ?, 'active', 0, ?, ?)",
                (
                    organization_id,
                    "{} 的平台调试空间".format(username),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO organization_memberships("
                "membership_id, organization_id, user_id, role, status, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, 'owner', 'active', ?, ?)",
                (
                    str(uuid.uuid4()),
                    organization_id,
                    user_id,
                    timestamp,
                    timestamp,
                ),
            )
        self.registry._initialize_tenant(self.registry.get(organization_id))
        return organization_id

    def list_conversations(
        self, user_id: int, organization_id: str, allow_delegation: bool = False
    ) -> List[Dict[str, Any]]:
        if not allow_delegation:
            self.membership(user_id, organization_id)
        else:
            self.get(organization_id)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT conversation_id AS id, title, organization_id, "
                "creator_user_id, source, channel_instance_id, "
                "external_participant_ref, external_participant_name, status, "
                "created_at, updated_at FROM organization_conversations "
                "WHERE organization_id=? ORDER BY updated_at DESC",
                (organization_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_channel_conversations(
        self,
        user_id: int,
        organization_id: str,
        channel_instance_id: str,
        allow_delegation: bool = False,
    ) -> List[Dict[str, Any]]:
        """List conversations bound to a single message channel.

        Channel conversations are stored with ``source='channel'`` and a
        ``channel_instance_id`` taken from the inbound message's channel id, so
        they are excluded from the web chat list but remain queryable here.
        """
        if allow_delegation:
            self.get(organization_id)
        else:
            self.membership(user_id, organization_id)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT conversation_id AS id, title, organization_id, "
                "creator_user_id, source, channel_instance_id, "
                "external_participant_ref, external_participant_name, status, "
                "created_at, updated_at FROM organization_conversations "
                "WHERE organization_id=? AND channel_instance_id=? "
                "AND source='channel' ORDER BY updated_at DESC",
                (organization_id, channel_instance_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_conversation(
        self,
        user_id: int,
        organization_id: str,
        title: str = "新对话",
        allow_delegation: bool = False,
    ) -> Dict[str, Any]:
        if allow_delegation:
            self.get(organization_id)
        else:
            self.membership(user_id, organization_id)
        conversation_id = str(uuid.uuid4())
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO organization_conversations("
                "conversation_id, organization_id, creator_user_id, source, title, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, 'web', ?, ?, ?)",
                (
                    conversation_id,
                    organization_id,
                    user_id,
                    title,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_conversation(
            user_id, conversation_id, allow_delegation=allow_delegation
        )

    def get_conversation(
        self, user_id: int, conversation_id: str, allow_delegation: bool = False
    ) -> Dict[str, Any]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT conversation_id AS id, title, organization_id, "
                "creator_user_id, source, channel_instance_id, "
                "external_participant_ref, external_participant_name, status, "
                "created_at, updated_at FROM organization_conversations "
                "WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise OrganizationError("对话不存在")
        if not allow_delegation:
            self.membership(user_id, str(row["organization_id"]))
        else:
            self.get(str(row["organization_id"]))
        return dict(row)

    def touch_conversation(
        self,
        user_id: int,
        conversation_id: str,
        user_text: Optional[str],
        allow_delegation: bool = False,
    ) -> None:
        conversation = self.get_conversation(
            user_id, conversation_id, allow_delegation=allow_delegation
        )
        title = str(conversation["title"] or "")
        if user_text and (not title or title == "新对话"):
            title = user_text.strip()[:20] or "新对话"
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE organization_conversations SET title=?, updated_at=? "
                "WHERE conversation_id=?",
                (title, _now(), conversation_id),
            )

    def update_conversation(
        self,
        user_id: int,
        conversation_id: str,
        *,
        title: Optional[str] = None,
        status: Optional[str] = None,
        allow_manage: bool = False,
        allow_delegation: bool = False,
    ) -> Dict[str, Any]:
        conversation = self.get_conversation(
            user_id, conversation_id, allow_delegation=allow_delegation
        )
        if not allow_manage and conversation.get("creator_user_id") != user_id:
            raise OrganizationError("只有会话创建者或组织管理员可以修改会话")
        if status is not None and status not in {"active", "archived"}:
            raise OrganizationError("会话状态无效")
        normalized_title = None
        if title is not None:
            normalized_title = title.strip()[:100]
            if not normalized_title:
                raise OrganizationError("会话名称不能为空")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE organization_conversations SET "
                "title=COALESCE(?, title), status=COALESCE(?, status), updated_at=? "
                "WHERE conversation_id=?",
                (normalized_title, status, _now(), conversation_id),
            )
        return self.get_conversation(
            user_id, conversation_id, allow_delegation=allow_delegation
        )

    def delete_conversation(
        self,
        user_id: int,
        conversation_id: str,
        allow_manage: bool = False,
        allow_delegation: bool = False,
    ) -> Dict[str, Any]:
        conversation = self.get_conversation(
            user_id, conversation_id, allow_delegation=allow_delegation
        )
        creator_user_id = conversation.get("creator_user_id")
        if not allow_manage and creator_user_id != user_id:
            raise OrganizationError("只有会话创建者或组织管理员可以删除会话")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM organization_conversations WHERE conversation_id=?",
                (conversation_id,),
            )
            session_key = "organization:{}".format(conversation_id)
            connection.execute(
                "DELETE FROM conversation_context_messages "
                "WHERE tenant_id=? AND session_key=?",
                (
                    str(conversation["organization_id"]),
                    session_key,
                ),
            )
            connection.execute(
                "DELETE FROM conversation_events WHERE tenant_id=? AND session_key=?",
                (str(conversation["organization_id"]), session_key),
            )
        return conversation

    def membership(self, user_id: int, organization_id: str) -> Dict[str, Any]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT m.membership_id, m.organization_id, m.user_id, m.role, "
                "m.status, m.created_at, m.updated_at "
                "FROM organization_memberships m JOIN organizations o "
                "ON o.organization_id=m.organization_id "
                "WHERE m.user_id=? AND m.organization_id=? "
                "AND m.status='active' AND o.status='active'",
                (user_id, organization_id),
            ).fetchone()
        if row is None:
            raise OrganizationError("当前账号不属于该组织")
        return dict(row)

    def active_organization(self, user_id: int) -> Optional[str]:
        """Return a deterministic fallback; Web selection is URL-scoped."""
        organizations = self.list_for_user(user_id)
        return str(organizations[0]["organization_id"]) if organizations else None

    def list_members(self, organization_id: str) -> List[Dict[str, Any]]:
        self.get(organization_id)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT m.membership_id, m.user_id, m.legacy_subject_id, m.role, "
                "m.status, m.created_at, u.display_name "
                "FROM organization_memberships m LEFT JOIN users u "
                "ON u.user_id=m.user_id WHERE m.organization_id=? "
                "ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 "
                "ELSE 2 END, m.created_at",
                (organization_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_member_role(
        self, organization_id: str, member_user_id: int, role: str
    ) -> Dict[str, Any]:
        if role not in ORGANIZATION_ROLES:
            raise OrganizationError("组织角色无效")
        current = self.membership(member_user_id, organization_id)
        if current["role"] == "owner" or role == "owner":
            if current["role"] == role:
                return current
            raise OrganizationError("所有权变更必须使用所有权转移操作")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE organization_memberships SET role=?, updated_at=? "
                "WHERE organization_id=? AND user_id=?",
                (role, _now(), organization_id, member_user_id),
            )
        return self.membership(member_user_id, organization_id)

    def transfer_ownership(
        self,
        organization_id: str,
        owner_user_id: int,
        new_owner_user_id: int,
    ) -> Dict[str, Any]:
        owner = self.membership(owner_user_id, organization_id)
        if owner["role"] != "owner":
            raise OrganizationError("只有组织所有者可以转移所有权")
        target = self.membership(new_owner_user_id, organization_id)
        if new_owner_user_id == owner_user_id:
            return target
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE organization_memberships SET role='admin', updated_at=? "
                "WHERE organization_id=? AND user_id=?",
                (_now(), organization_id, owner_user_id),
            )
            connection.execute(
                "UPDATE organization_memberships SET role='owner', updated_at=? "
                "WHERE organization_id=? AND user_id=?",
                (_now(), organization_id, new_owner_user_id),
            )
        return self.membership(new_owner_user_id, organization_id)

    def remove_member(
        self, organization_id: str, member_user_id: int
    ) -> None:
        membership = self.membership(member_user_id, organization_id)
        if membership["role"] == "owner":
            raise OrganizationError("组织所有者必须先转移所有权")
        fallback = next(
            (
                str(item["organization_id"])
                for item in self.list_for_user(member_user_id)
                if str(item["organization_id"]) != organization_id
            ),
            None,
        )
        personal_id: Optional[str] = None
        with self.database.transaction(immediate=True) as connection:
            personal = connection.execute(
                "SELECT tenant_id FROM tenants WHERE bot_id=? AND user_id=? "
                "AND deleting=0",
                (
                    "member-personal:{}".format(organization_id),
                    str(member_user_id),
                ),
            ).fetchone()
            personal_id = (
                str(personal["tenant_id"]) if personal is not None else None
            )
            connection.execute(
                "DELETE FROM organization_memberships "
                "WHERE organization_id=? AND user_id=?",
                (organization_id, member_user_id),
            )
            connection.execute(
                "UPDATE channel_identities SET active_organization_id=?, "
                "tenant_id=COALESCE(?, tenant_id) "
                "WHERE user_id=? AND active_organization_id=?",
                (
                    fallback,
                    fallback,
                    member_user_id,
                    organization_id,
                ),
            )
        if personal_id:
            self.registry.delete(self.registry.get(personal_id))

    def channel_user(self, channel_id: str, account_id: str, external_user_id: str) -> Optional[int]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT user_id FROM channel_identities WHERE channel_id=? "
                "AND account_id=? AND external_user_id=?",
                (channel_id, account_id, external_user_id),
            ).fetchone()
        return int(row["user_id"]) if row and row["user_id"] is not None else None

    def set_channel_organization(
        self,
        channel_id: str,
        account_id: str,
        external_user_id: str,
        organization_id: str,
    ) -> None:
        user_id = self.channel_user(channel_id, account_id, external_user_id)
        if user_id is None:
            raise OrganizationError("当前渠道身份尚未认领账号")
        self.membership(user_id, organization_id)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE channel_identities SET tenant_id=?, "
                "active_organization_id=? WHERE channel_id=? AND account_id=? "
                "AND external_user_id=?",
                (
                    organization_id,
                    organization_id,
                    channel_id,
                    account_id,
                    external_user_id,
                ),
            )
