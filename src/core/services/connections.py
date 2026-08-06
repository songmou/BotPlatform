"""Personal messaging channel connections owned by individual users.

A personal connection is an organization channel instance plus an ownership
row: the runtime treats it exactly like any other organization channel
(messages are pinned to the owning organization), while credentials live in
the personal scope of the credential keychain so organization admins cannot
see or overwrite them.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.core.services.credentials import CredentialError, CredentialService
from src.core.services.organization_controls import (
    OrganizationControlError,
    OrganizationControlStore,
)
from src.core.storage.organizations import OrganizationStore


PLATFORM_CHANNEL_TYPES = {"wechat": "wechat_ilink", "wecom": "wecom_aibot"}


class PersonalConnectionError(ValueError):
    """Raised for invalid personal connection operations."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PersonalConnectionService:
    """Manage personal channel connections on top of organization channels."""

    def __init__(
        self,
        organizations: OrganizationStore,
        controls: OrganizationControlStore,
        credentials: CredentialService,
    ) -> None:
        self.organizations = organizations
        self.controls = controls
        self.credentials = credentials
        self.database = organizations.database

    @staticmethod
    def credential_id_for(channel_id: str) -> str:
        return "channel:{}".format(channel_id)

    def _row_dict(self, row: Any) -> Dict[str, Any]:
        return {
            "connection_id": str(row["connection_id"]),
            "user_id": int(row["user_id"]),
            "organization_id": str(row["organization_id"]),
            "channel_instance_id": str(row["channel_instance_id"]),
            "platform": str(row["platform"]),
            "bot_account_id": str(row["bot_account_id"] or ""),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _get_row(self, connection_id: str) -> Dict[str, Any]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM personal_channel_connections WHERE connection_id=?",
                (connection_id,),
            ).fetchone()
        if row is None:
            raise PersonalConnectionError("连接不存在")
        return self._row_dict(row)

    def _require_owner(self, connection_id: str, user_id: int) -> Dict[str, Any]:
        row = self._get_row(connection_id)
        if row["user_id"] != int(user_id):
            raise PersonalConnectionError("连接不存在")
        return row

    def _generate_channel_id(
        self, organization_id: str, platform: str, user_id: int
    ) -> str:
        taken = {
            item["id"] for item in self.controls.list_channels(organization_id)
        }
        while True:
            candidate = "pc-{}-u{}-{}".format(
                platform, int(user_id), secrets.token_hex(2)
            )
            if candidate not in taken:
                return candidate

    def _detail(
        self, row: Dict[str, Any], organization_name: str = ""
    ) -> Dict[str, Any]:
        channel = self.controls.get_channel(
            row["organization_id"],
            self._channel_id_of(row["channel_instance_id"]),
        )
        detail = dict(row)
        detail.update(
            {
                "channel": channel,
                "organization_name": organization_name,
                "enabled": channel["enabled"],
                "agent_id": channel["agent_id"],
                "credential_configured": channel["credential_configured"],
            }
        )
        return detail

    def _channel_id_of(self, channel_instance_id: str) -> str:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT channel_id, organization_id FROM organization_channels "
                "WHERE channel_instance_id=?",
                (channel_instance_id,),
            ).fetchone()
        if row is None:
            raise PersonalConnectionError("连接不存在")
        return str(row["channel_id"])

    def create(
        self,
        *,
        user_id: int,
        organization_id: str,
        platform: str,
        agent_id: str,
        bot_account_id: str = "",
        allow_delegation: bool = False,
    ) -> Dict[str, Any]:
        if platform not in PLATFORM_CHANNEL_TYPES:
            raise PersonalConnectionError("不支持的连接平台")
        if allow_delegation:
            self.organizations.get(organization_id)
        else:
            self.organizations.membership(user_id, organization_id)
        channel_id = self._generate_channel_id(organization_id, platform, user_id)
        try:
            channel = self.controls.upsert_channel(
                organization_id,
                channel_id,
                {
                    "type": PLATFORM_CHANNEL_TYPES[platform],
                    "agent_id": agent_id,
                    "enabled": True,
                    "settings": {"group_policy": "private_only"},
                },
                int(user_id),
            )
        except OrganizationControlError as exc:
            raise PersonalConnectionError(str(exc)) from exc
        connection_id = str(uuid.uuid4())
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO personal_channel_connections("
                "connection_id, user_id, organization_id, channel_instance_id, "
                "platform, bot_account_id, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    connection_id,
                    int(user_id),
                    organization_id,
                    str(channel["channel_instance_id"]),
                    platform,
                    str(bot_account_id or ""),
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(connection_id, user_id)

    def get(self, connection_id: str, user_id: int) -> Dict[str, Any]:
        row = self._require_owner(connection_id, user_id)
        organization = self.organizations.get(row["organization_id"])
        return self._detail(row, str(organization.get("name") or ""))

    def list_for_user(self, user_id: int) -> List[Dict[str, Any]]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM personal_channel_connections "
                "WHERE user_id=? ORDER BY created_at DESC",
                (int(user_id),),
            ).fetchall()
        result = []
        for row in rows:
            item = self._row_dict(row)
            try:
                organization = self.organizations.get(item["organization_id"])
                result.append(
                    self._detail(item, str(organization.get("name") or ""))
                )
            except Exception:
                continue
        return result

    def set_enabled(
        self, connection_id: str, user_id: int, enabled: bool
    ) -> Dict[str, Any]:
        row = self._require_owner(connection_id, user_id)
        channel_id = self._channel_id_of(row["channel_instance_id"])
        self.controls.set_channel_enabled(
            row["organization_id"], channel_id, bool(enabled), int(user_id)
        )
        return self.get(connection_id, user_id)

    def change_agent(
        self, connection_id: str, user_id: int, agent_id: str
    ) -> Dict[str, Any]:
        row = self._require_owner(connection_id, user_id)
        channel_id = self._channel_id_of(row["channel_instance_id"])
        channel = self.controls.get_channel(row["organization_id"], channel_id)
        try:
            self.controls.upsert_channel(
                row["organization_id"],
                channel_id,
                {
                    "type": channel["type"],
                    "agent_id": agent_id,
                    "enabled": channel["enabled"],
                    "settings": channel.get("settings") or {"group_policy": "private_only"},
                },
                int(user_id),
            )
        except OrganizationControlError as exc:
            raise PersonalConnectionError(str(exc)) from exc
        return self.get(connection_id, user_id)

    def set_bot_account(
        self, connection_id: str, bot_account_id: str
    ) -> Dict[str, Any]:
        row = self._get_row(connection_id)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE personal_channel_connections SET bot_account_id=?, "
                "updated_at=? WHERE connection_id=?",
                (str(bot_account_id or ""), _now(), row["connection_id"]),
            )
        return self._get_row(connection_id)

    def put_wecom_credentials(
        self,
        connection_id: str,
        user_id: int,
        bot_id: str,
        secret: str,
        allow_delegation: bool = False,
    ) -> Dict[str, Any]:
        row = self._require_owner(connection_id, user_id)
        if row["platform"] != "wecom":
            raise PersonalConnectionError("该连接不是企业微信 Bot")
        channel_id = self._channel_id_of(row["channel_instance_id"])
        self.credentials.put(
            row["organization_id"],
            self.credential_id_for(channel_id),
            actor_user_id=int(user_id),
            scope="personal",
            resource_type="channels",
            resource_id=row["channel_instance_id"],
            label="个人企微 Bot",
            secret=json.dumps(
                {"bot_id": bot_id, "secret": secret}, ensure_ascii=False
            ),
            allow_platform_delegation=allow_delegation,
        )
        self.set_bot_account(connection_id, bot_id)
        self.controls.bump_channels_revision(row["organization_id"])
        return self.get(connection_id, user_id)

    def current_wecom_secret(
        self, row: Dict[str, Any]
    ) -> Optional[Dict[str, str]]:
        try:
            raw = self.credentials.secret_for_resource(
                row["organization_id"], "channels", row["channel_instance_id"]
            )
        except CredentialError:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        return {
            "bot_id": str(parsed.get("bot_id") or ""),
            "secret": str(parsed.get("secret") or ""),
        }

    def save_wechat_credentials(
        self,
        connection_id: str,
        credentials_payload: Dict[str, Any],
        allow_delegation: bool = False,
    ) -> Dict[str, Any]:
        """Persist credentials produced by a successful WeChat QR login."""
        row = self._get_row(connection_id)
        if row["platform"] != "wechat":
            raise PersonalConnectionError("该连接不是微信")
        channel_id = self._channel_id_of(row["channel_instance_id"])
        self.credentials.put(
            row["organization_id"],
            self.credential_id_for(channel_id),
            actor_user_id=row["user_id"],
            scope="personal",
            allow_platform_delegation=allow_delegation,
            resource_type="channels",
            resource_id=row["channel_instance_id"],
            label="个人微信登录",
            secret=json.dumps(credentials_payload, ensure_ascii=False),
        )
        self.set_bot_account(connection_id, str(credentials_payload.get("bot_id") or ""))
        self.controls.bump_channels_revision(row["organization_id"])
        return self._get_row(connection_id)

    def delete(self, connection_id: str, user_id: int) -> None:
        row = self._require_owner(connection_id, user_id)
        channel_id = self._channel_id_of(row["channel_instance_id"])
        try:
            self.credentials.delete(
                row["organization_id"],
                self.credential_id_for(channel_id),
                int(user_id),
            )
        except CredentialError:
            pass
        # Deleting the channel row cascades to personal_channel_connections.
        self.controls.delete_channel(row["organization_id"], channel_id)
