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

from src.core.messaging.credentials import (
    ChannelCredentialError,
    validate_channel_credentials,
)
from src.core.services.credentials import CredentialError, CredentialService
from src.core.services.organization_controls import (
    OrganizationControlError,
    OrganizationControlStore,
)
from src.core.storage.organizations import OrganizationStore


PLATFORM_CHANNEL_TYPES = {
    "wechat": "wechat_ilink",
    "wecom": "wecom_aibot",
    "feishu": "feishu",
}


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
        # A user may only have one active connection per platform: creating a
        # new one pauses the others so the runtime never runs two adapters
        # that share a single SDK connection (e.g. feishu long connection).
        self._disable_other_platform_connections(user_id, platform)
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
        self._repair_single_active_per_platform(user_id)
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

    def _repair_single_active_per_platform(self, user_id: int) -> None:
        """Pause duplicate active connections per platform (legacy data).

        The SDK for some platforms (feishu) only supports one live connection
        per process, so keep at most the most recently updated connection
        enabled per platform and pause the rest.
        """
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT c.connection_id, c.organization_id, "
                "c.channel_instance_id, c.platform, c.updated_at "
                "FROM personal_channel_connections c "
                "JOIN organization_channels o "
                "ON o.channel_instance_id = c.channel_instance_id "
                "WHERE c.user_id=? AND o.enabled=1",
                (int(user_id),),
            ).fetchall()
        by_platform: Dict[str, List[Any]] = {}
        for row in rows:
            by_platform.setdefault(str(row["platform"]), []).append(row)
        for items in by_platform.values():
            if len(items) <= 1:
                continue
            items.sort(key=lambda row: str(row["updated_at"] or ""), reverse=True)
            for extra in items[1:]:
                try:
                    channel_id = self._channel_id_of(
                        str(extra["channel_instance_id"])
                    )
                    self.controls.set_channel_enabled(
                        str(extra["organization_id"]),
                        channel_id,
                        False,
                        int(user_id),
                    )
                except OrganizationControlError:
                    continue

    def _disable_other_platform_connections(
        self,
        user_id: int,
        platform: str,
        keep_connection_id: Optional[str] = None,
    ) -> None:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT connection_id, organization_id, channel_instance_id "
                "FROM personal_channel_connections "
                "WHERE user_id=? AND platform=?",
                (int(user_id), platform),
            ).fetchall()
        for row in rows:
            connection_id = str(row["connection_id"])
            if keep_connection_id and connection_id == keep_connection_id:
                continue
            try:
                channel_id = self._channel_id_of(str(row["channel_instance_id"]))
                self.controls.set_channel_enabled(
                    str(row["organization_id"]), channel_id, False, int(user_id)
                )
            except OrganizationControlError:
                continue

    def set_enabled(
        self, connection_id: str, user_id: int, enabled: bool
    ) -> Dict[str, Any]:
        row = self._require_owner(connection_id, user_id)
        if enabled:
            # One active connection per platform: pausing the others before
            # enabling this one keeps the runtime free of SDK conflicts.
            self._disable_other_platform_connections(
                user_id, row["platform"], keep_connection_id=connection_id
            )
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

    def save_feishu_credentials(
        self,
        connection_id: str,
        credentials_payload: Dict[str, Any],
        allow_delegation: bool = False,
    ) -> Dict[str, Any]:
        """Persist credentials produced by a successful Feishu QR registration."""
        row = self._get_row(connection_id)
        if row["platform"] != "feishu":
            raise PersonalConnectionError("该连接不是飞书")
        # The registration protocol returns client_id/client_secret plus extra
        # fields (user_info); the channel credential format only stores
        # app_id/app_secret, so map the payload before validation.
        payload = {
            "app_id": str(
                credentials_payload.get("client_id")
                or credentials_payload.get("app_id")
                or ""
            ),
            "app_secret": str(
                credentials_payload.get("client_secret")
                or credentials_payload.get("app_secret")
                or ""
            ),
        }
        try:
            normalized = validate_channel_credentials("feishu", payload)
        except ChannelCredentialError as exc:
            raise PersonalConnectionError(str(exc)) from exc
        channel_id = self._channel_id_of(row["channel_instance_id"])
        self.credentials.put(
            row["organization_id"],
            self.credential_id_for(channel_id),
            actor_user_id=row["user_id"],
            scope="personal",
            allow_platform_delegation=allow_delegation,
            resource_type="channels",
            resource_id=row["channel_instance_id"],
            label="个人飞书应用",
            secret=json.dumps(normalized, ensure_ascii=False),
        )
        self.set_bot_account(connection_id, normalized["app_id"])
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
