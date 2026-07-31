"""Persistence for channel identities, delivery endpoints, and inbound events."""

from __future__ import annotations

import hashlib
import json
import base64
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from src.core.messaging.contracts import DeliveryEndpoint, InboundMessage
from src.core.storage.tenants import TenantContext, TenantRegistry


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return "{}-{}".format(prefix, hashlib.sha256(raw).hexdigest()[:32])


class ChannelBindingError(ValueError):
    """Raised when a cross-channel identity binding cannot be completed."""


class ChannelAddressStore:
    """Map external channel identities to canonical tenants and endpoints."""

    def __init__(self, registry: TenantRegistry) -> None:
        self.registry = registry

    @staticmethod
    def _identity(row: Any) -> str:
        return str(row["identity_id"])

    @staticmethod
    def _binding_hash(code: str) -> str:
        return hashlib.sha256(code.strip().upper().encode("ascii")).hexdigest()

    def issue_binding_code(
        self,
        tenant: TenantContext,
        message: InboundMessage,
        *,
        lifetime_minutes: int = 10,
    ) -> str:
        if message.conversation_type != "direct":
            raise ChannelBindingError("绑定码只能在私聊中生成")
        now = _now()
        expires_at = now + timedelta(minutes=max(1, lifetime_minutes))
        code = base64.b32encode(secrets.token_bytes(7)).decode("ascii").rstrip("=")[:10]
        token_hash = self._binding_hash(code)
        with self.registry.database.transaction(immediate=True) as connection:
            identity = connection.execute(
                "SELECT identity_id, tenant_id FROM channel_identities "
                "WHERE channel_id=? AND account_id=? AND external_user_id=?",
                (message.channel_id, message.account_id, message.sender_id),
            ).fetchone()
            if identity is None or str(identity["tenant_id"]) != tenant.tenant_id:
                raise ChannelBindingError("当前消息身份尚未绑定租户")
            connection.execute(
                "DELETE FROM channel_binding_codes "
                "WHERE expires_at<=? OR used_at IS NOT NULL",
                (_iso(now),),
            )
            connection.execute(
                "INSERT INTO channel_binding_codes("
                "token_hash, tenant_id, identity_id, created_at, expires_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    token_hash,
                    tenant.tenant_id,
                    str(identity["identity_id"]),
                    _iso(now),
                    _iso(expires_at),
                ),
            )
        return code

    def bind_with_code(
        self,
        message: InboundMessage,
        code: str,
        *,
        max_attempts: int = 5,
        attempt_window_minutes: int = 10,
    ) -> TenantContext:
        if message.conversation_type != "direct":
            raise ChannelBindingError("绑定码只能在私聊中使用")
        normalized = code.strip().upper()
        if len(normalized) != 10 or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
            for character in normalized
        ):
            raise ChannelBindingError("绑定码格式无效")
        now = _now()
        cutoff = _iso(now - timedelta(minutes=max(1, attempt_window_minutes)))
        token_hash = self._binding_hash(normalized)
        identity_id = _stable_id(
            "identity",
            message.channel_id,
            message.account_id,
            message.sender_id,
        )
        # Attempt accounting must commit even when the supplied code is invalid.
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM channel_binding_attempts WHERE attempted_at<?",
                (_iso(now - timedelta(days=1)),),
            )
            attempts = connection.execute(
                "SELECT COUNT(*) FROM channel_binding_attempts "
                "WHERE channel_id=? AND account_id=? AND external_user_id=? "
                "AND attempted_at>=?",
                (
                    message.channel_id,
                    message.account_id,
                    message.sender_id,
                    cutoff,
                ),
            ).fetchone()
            if attempts is not None and int(attempts[0]) >= max_attempts:
                raise ChannelBindingError("绑定尝试过于频繁，请稍后再试")
            connection.execute(
                "INSERT INTO channel_binding_attempts("
                "channel_id, account_id, external_user_id, attempted_at"
                ") VALUES (?, ?, ?, ?)",
                (
                    message.channel_id,
                    message.account_id,
                    message.sender_id,
                    _iso(now),
                ),
            )
        with self.registry.database.transaction(immediate=True) as connection:
            binding = connection.execute(
                "SELECT tenant_id FROM channel_binding_codes "
                "WHERE token_hash=? AND used_at IS NULL AND expires_at>?",
                (token_hash, _iso(now)),
            ).fetchone()
            if binding is None:
                raise ChannelBindingError("绑定码无效或已经过期")
            target_tenant_id = str(binding["tenant_id"])
            existing = connection.execute(
                "SELECT tenant_id FROM channel_identities "
                "WHERE channel_id=? AND account_id=? AND external_user_id=?",
                (
                    message.channel_id,
                    message.account_id,
                    message.sender_id,
                ),
            ).fetchone()
            if existing is not None:
                if str(existing["tenant_id"]) != target_tenant_id:
                    raise ChannelBindingError(
                        "当前渠道身份已有独立数据，不能自动合并"
                    )
            else:
                seen_at = message.occurred_at or _iso(now)
                connection.execute(
                    "INSERT INTO channel_identities("
                    "identity_id, tenant_id, channel_id, platform, account_id, "
                    "external_user_id, created_at, last_seen_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        identity_id,
                        target_tenant_id,
                        message.channel_id,
                        message.platform,
                        message.account_id,
                        message.sender_id,
                        seen_at,
                        seen_at,
                    ),
                )
            connection.execute(
                "UPDATE channel_binding_codes SET used_at=? WHERE token_hash=?",
                (_iso(now), token_hash),
            )
        return self.registry.get(target_tenant_id)

    def resolve(self, message: InboundMessage) -> TenantContext:
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT identity_id, tenant_id FROM channel_identities "
                "WHERE channel_id=? AND account_id=? AND external_user_id=?",
                (message.channel_id, message.account_id, message.sender_id),
            ).fetchone()
        if row is not None:
            tenant = self.registry.get(str(row["tenant_id"]))
        else:
            legacy_bot_id = (
                message.account_id
                if message.platform == "wechat_ilink"
                else "{}:{}".format(message.channel_id, message.account_id)
            )
            legacy_user_id = (
                message.sender_id
                if message.platform == "wechat_ilink"
                else "{}:{}".format(message.platform, message.sender_id)
            )
            tenant = self.registry.resolve(legacy_bot_id, legacy_user_id)

        seen_at = message.occurred_at or _iso(_now())
        identity_id = _stable_id(
            "identity",
            message.channel_id,
            message.account_id,
            message.sender_id,
        )
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO channel_identities("
                "identity_id, tenant_id, channel_id, platform, account_id, "
                "external_user_id, created_at, last_seen_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(channel_id, account_id, external_user_id) DO UPDATE SET "
                "last_seen_at=excluded.last_seen_at",
                (
                    identity_id,
                    tenant.tenant_id,
                    message.channel_id,
                    message.platform,
                    message.account_id,
                    message.sender_id,
                    seen_at,
                    seen_at,
                ),
            )
        return tenant

    def record_endpoint(
        self,
        tenant: TenantContext,
        message: InboundMessage,
    ) -> DeliveryEndpoint:
        with self.registry.database.read() as connection:
            identity_row = connection.execute(
                "SELECT identity_id, tenant_id FROM channel_identities "
                "WHERE channel_id=? AND account_id=? AND external_user_id=?",
                (message.channel_id, message.account_id, message.sender_id),
            ).fetchone()
        if identity_row is None or str(identity_row["tenant_id"]) != tenant.tenant_id:
            raise ValueError("消息身份尚未解析或租户不匹配")
        identity_id = str(identity_row["identity_id"])
        endpoint = message.endpoint
        endpoint_id = _stable_id(
            "endpoint",
            endpoint.channel_id,
            endpoint.account_id,
            endpoint.conversation_type,
            endpoint.conversation_id,
            endpoint.recipient_id,
            endpoint.thread_id,
        )
        seen_at = message.occurred_at or _iso(_now())
        payload = json.dumps(
            dict(endpoint.route_context),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO delivery_endpoints("
                "endpoint_id, identity_id, tenant_id, channel_id, platform, "
                "account_id, conversation_type, conversation_id, recipient_id, "
                "thread_id, route_context_json, status, last_seen_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?) "
                "ON CONFLICT("
                "channel_id, account_id, conversation_type, conversation_id, "
                "recipient_id, thread_id"
                ") DO UPDATE SET "
                "identity_id=excluded.identity_id, tenant_id=excluded.tenant_id, "
                "platform=excluded.platform, route_context_json=excluded.route_context_json, "
                "status='active', last_seen_at=excluded.last_seen_at",
                (
                    endpoint_id,
                    identity_id,
                    tenant.tenant_id,
                    endpoint.channel_id,
                    endpoint.platform,
                    endpoint.account_id,
                    endpoint.conversation_type,
                    endpoint.conversation_id,
                    endpoint.recipient_id,
                    endpoint.thread_id,
                    payload,
                    seen_at,
                ),
            )
            if (
                endpoint.platform == "wechat_ilink"
                and endpoint.conversation_type == "direct"
                and endpoint.route_context.get("context_token")
            ):
                connection.execute(
                    "INSERT INTO recipients("
                    "tenant_id, user_id, context_token, updated_at"
                    ") VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(tenant_id) DO UPDATE SET "
                    "user_id=excluded.user_id, "
                    "context_token=excluded.context_token, "
                    "updated_at=excluded.updated_at",
                    (
                        tenant.tenant_id,
                        endpoint.recipient_id,
                        str(endpoint.route_context["context_token"]),
                        seen_at,
                    ),
                )
        return replace(endpoint, endpoint_id=endpoint_id)

    @staticmethod
    def _endpoint_from_row(row: Any) -> DeliveryEndpoint:
        try:
            route = json.loads(str(row["route_context_json"]))
        except (TypeError, ValueError):
            route = {}
        if not isinstance(route, dict):
            route = {}
        return DeliveryEndpoint(
            channel_id=str(row["channel_id"]),
            platform=str(row["platform"]),
            account_id=str(row["account_id"]),
            conversation_type=str(row["conversation_type"]),
            conversation_id=str(row["conversation_id"]),
            recipient_id=str(row["recipient_id"]),
            thread_id=str(row["thread_id"]),
            route_context=route,
            endpoint_id=str(row["endpoint_id"]),
        )

    def latest_endpoint(
        self,
        tenant_id: str,
        channel_id: Optional[str] = None,
        include_non_direct: bool = False,
    ) -> Optional[DeliveryEndpoint]:
        query = (
            "SELECT * FROM delivery_endpoints "
            "WHERE tenant_id=? AND status='active'"
        )
        values: List[Any] = [tenant_id]
        if not include_non_direct:
            query += " AND conversation_type='direct'"
        if channel_id:
            query += " AND channel_id=?"
            values.append(channel_id)
        query += " ORDER BY last_seen_at DESC, endpoint_id DESC LIMIT 1"
        with self.registry.database.read() as connection:
            row = connection.execute(query, values).fetchone()
        return self._endpoint_from_row(row) if row is not None else None

    def endpoint(self, endpoint_id: str) -> Optional[DeliveryEndpoint]:
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_endpoints WHERE endpoint_id=?",
                (endpoint_id,),
            ).fetchone()
        return self._endpoint_from_row(row) if row is not None else None

    def mark_stale(self, endpoint_id: str) -> None:
        if not endpoint_id:
            return
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE delivery_endpoints SET status='stale' WHERE endpoint_id=?",
                (endpoint_id,),
            )


class MessageInboxStore:
    """Durable, idempotent inbox shared by all receive transports."""

    def __init__(self, registry: TenantRegistry) -> None:
        self.registry = registry

    def enqueue(self, message: InboundMessage) -> bool:
        payload = json.dumps(
            message.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self.registry.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO message_inbox("
                "channel_id, event_id, payload_json, status, received_at"
                ") VALUES (?, ?, ?, 'pending', ?)",
                (
                    message.channel_id,
                    message.event_id,
                    payload,
                    _iso(_now()),
                ),
            )
        return cursor.rowcount == 1

    def cleanup(self, retention_days: int = 30) -> int:
        cutoff = _iso(_now() - timedelta(days=max(1, retention_days)))
        with self.registry.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM message_inbox WHERE "
                "status IN ('done', 'ignored', 'failed') "
                "AND received_at<?",
                (cutoff,),
            )
        return int(cursor.rowcount)

    def claim(self, lease_seconds: int = 120) -> Optional[Dict[str, Any]]:
        now = _now()
        now_iso = _iso(now)
        lease = _iso(now + timedelta(seconds=lease_seconds))
        with self.registry.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM message_inbox WHERE "
                "(status='pending' OR "
                "(status='retry' AND (next_attempt_at IS NULL OR next_attempt_at<=?)) OR "
                "(status='processing' AND lease_expires_at<=?)) "
                "ORDER BY inbox_id LIMIT 1",
                (now_iso, now_iso),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE message_inbox SET status='processing', "
                "attempt_count=attempt_count+1, lease_expires_at=? WHERE inbox_id=?",
                (lease, int(row["inbox_id"])),
            )
            claimed = connection.execute(
                "SELECT * FROM message_inbox WHERE inbox_id=?",
                (int(row["inbox_id"]),),
            ).fetchone()
        return dict(claimed)

    @staticmethod
    def decode(row: Mapping[str, Any]) -> InboundMessage:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError("入站消息存储格式无效")
        return InboundMessage.from_dict(payload)

    def finish(
        self,
        inbox_id: int,
        status: str,
        error: str = "",
        retry_seconds: int = 2,
    ) -> None:
        if status not in {"done", "ignored", "failed", "retry"}:
            raise ValueError("未知 inbox 完成状态")
        now = _now()
        next_attempt = (
            _iso(now + timedelta(seconds=retry_seconds))
            if status == "retry"
            else None
        )
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE message_inbox SET status=?, next_attempt_at=?, "
                "lease_expires_at=NULL, processed_at=?, last_error=? "
                "WHERE inbox_id=?",
                (
                    status,
                    next_attempt,
                    _iso(now) if status != "retry" else None,
                    error[:1000] or None,
                    inbox_id,
                ),
            )
